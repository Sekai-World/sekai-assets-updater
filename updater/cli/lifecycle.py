"""Run completion flows: download, metadata-only writes, cache restore."""

import asyncio
import copy
import logging
import time
from typing import Any, Dict, List

import orjson as json
from anyio import open_file

from updater.cli.pending import (
    _pending_items_outside_mode,
    _read_json_cache,
    _write_json_cache,
)
from updater.model import ConfigLike
from updater.modes import (
    filter_bundles_for_mode,
    filter_download_items_for_mode,
    get_enabled_specialized_modes,
    is_chart_score_bundle,
)
from updater.net.disk_space import build_download_disk_space_gate
from updater.net.plan import (
    DownloadItem,
    _sanitize_asset_bundle_info,
    dedupe_download_items,
    get_bundle_checksum,
    get_regional_music_selection_for_config,
    select_bundles_for_download,
)
from updater.pipeline import run_pipeline
from updater.postprocess.dispatch import run_specialized_postprocess
from updater.postprocess.live2d_models import recover_live2d_model_outputs
from updater.sanitize import sanitize_http_log_value
from updater.state import (
    StatePaths,
    atomic_write_json,
    create_journal,
    durable_unlink,
    replay_journal,
    validate_asset_metadata,
    validate_game_version,
    validate_pending_queue,
)

logger = logging.getLogger("asset_updater")
_RUN_COMPLETED_LOG = "RUN | status=completed | duration_sec=%.2f"
_PROVENANCE_KEY = "_sekai_assets_updater"


def _download_item_identity(item: DownloadItem) -> tuple[str, str, str | None, str] | None:
    url, bundle = item
    if not isinstance(url, str) or not isinstance(bundle, dict):
        return None
    bundle_name = bundle.get("bundleName")
    if not isinstance(bundle_name, str) or not bundle_name:
        return None
    checksum_field, checksum_value = get_bundle_checksum(bundle)
    return url, bundle_name, checksum_field, checksum_value


def _completed_download_items(
    download_list: List[DownloadItem], failed_tasks: List[DownloadItem]
) -> List[DownloadItem]:
    """Return only task inputs not reported failed by a normally completed pipeline.

    ``run_pipeline`` reports download, extract, and upload failures using the
    original URL and bundle record. Treat an unrecognized or over-reported
    failure as ambiguous and fail closed rather than infer success from files.
    """
    remaining: Dict[tuple[str, str, str | None, str], int] = {}
    for item in download_list:
        identity = _download_item_identity(item)
        if identity is None:
            return []
        remaining[identity] = remaining.get(identity, 0) + 1

    for item in failed_tasks:
        identity = _download_item_identity(item)
        if identity is None or remaining.get(identity, 0) == 0:
            return []
        remaining[identity] -= 1

    completed: List[DownloadItem] = []
    for item in download_list:
        identity = _download_item_identity(item)
        assert identity is not None
        if remaining[identity] > 0:
            completed.append(item)
            remaining[identity] -= 1
    return completed


def _metadata_after_partial_success(
    asset_metadata: Dict[str, Any],
    provenance_candidates: List[DownloadItem],
    completed_items: List[DownloadItem],
    failed_items: List[DownloadItem],
    withheld_provenance_names: set[str] | None = None,
) -> Dict[str, Any]:
    """Mark only current manifest candidates that completed every pipeline stage."""
    result = copy.deepcopy(asset_metadata)
    bundles = result.get("bundles")
    if not isinstance(bundles, dict):
        return result
    withheld_provenance_names = withheld_provenance_names or set()
    for bundle_name in withheld_provenance_names:
        current_bundle = bundles.get(bundle_name)
        if isinstance(current_bundle, dict):
            # A prior marker may have been carried onto this checksum by the
            # current plan. Remove it too: temporary post-processing inputs
            # disappear after this partial run, so this candidate must retry.
            current_bundle.pop(_PROVENANCE_KEY, None)

    completed_identities = {
        identity
        for item in completed_items
        if (identity := _download_item_identity(item)) is not None
    }
    failed_names = {
        item[1].get("bundleName")
        for item in failed_items
        if isinstance(item[1], dict) and isinstance(item[1].get("bundleName"), str)
    }
    candidate_counts: Dict[str, int] = {}
    for _, candidate in provenance_candidates:
        name = candidate.get("bundleName")
        if isinstance(name, str):
            candidate_counts[name] = candidate_counts.get(name, 0) + 1

    for candidate in provenance_candidates:
        identity = _download_item_identity(candidate)
        if identity is None:
            continue
        _, bundle_name, checksum_field, checksum_value = identity
        if (
            identity not in completed_identities
            or bundle_name in failed_names
            or bundle_name in withheld_provenance_names
            or candidate_counts.get(bundle_name) != 1
            or checksum_field not in {"hash", "crc"}
            or not checksum_value
        ):
            continue
        current_bundle = bundles.get(bundle_name)
        if not isinstance(current_bundle, dict):
            continue
        if get_bundle_checksum(current_bundle) != (checksum_field, checksum_value):
            continue
        current_bundle[_PROVENANCE_KEY] = {
            "processed_checksum": {"field": checksum_field, "value": checksum_value}
        }
    return result


async def do_download(
    dl_list: List[DownloadItem],
    config: ConfigLike,
    headers: Dict[str, str],
    cookie,
    paths: StatePaths | None = None,
    *,
    extracted_dir_is_temporary: bool = False,
    failed_items_out: List[DownloadItem] | None = None,
    completed_items_out: List[DownloadItem] | None = None,
    defer_failed_queue_write: bool = False,
) -> bool:
    """
    Download the files in the download list using asyncio and aiohttp.
    The download list is a list of tuples containing the url and the bundle name.
    The function will use a queue to manage the download tasks.
    """
    logger.info("RUN | step=4/4 | action=pipeline_start | items=%d", len(dl_list))
    download_disk_space_gate = build_download_disk_space_gate(config)
    if download_disk_space_gate is not None:
        logger.debug(
            "Download disk space gate enabled for %s with min free bytes=%d",
            download_disk_space_gate.target_path,
            download_disk_space_gate.min_free_bytes,
        )

    pipeline_error = None
    failed_tasks: List[DownloadItem] = []
    try:
        failed_tasks = await run_pipeline(
            dl_list,
            config,
            headers,
            cookie=cookie,
            download_disk_space_gate=download_disk_space_gate,
            extracted_dir_is_temporary=extracted_dir_is_temporary,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "ERROR | stage=pipeline | action=crash | preserve_pending=true | items=%d",
            len(dl_list),
        )
        pipeline_error = RuntimeError(sanitize_http_log_value(str(exc)))

    if pipeline_error is not None:
        raise pipeline_error

    if failed_items_out is not None:
        failed_items_out[:] = failed_tasks
    if completed_items_out is not None:
        completed_items_out[:] = _completed_download_items(dl_list, failed_tasks)

    # Replace the original download list with the failed tasks
    if failed_tasks:
        failed_path = paths.queue if paths is not None else config.DL_LIST_CACHE_PATH
        if not defer_failed_queue_write:
            if paths is not None:
                atomic_write_json(
                    failed_path, [list(item) for item in failed_tasks], validate_pending_queue
                )
            else:
                async with await open_file(failed_path, "wb") as f:
                    await f.write(json.dumps(failed_tasks, option=json.OPT_INDENT_2))
        logger.warning(
            "RUN | result=partial_failure | failed=%d | retry_list=%s",
            len(failed_tasks),
            failed_path,
        )

        return False
    else:
        logger.info("RUN | result=success | completed=%d", len(dl_list))
        return True


async def _write_metadata_only_cache(
    cfg: ConfigLike,
    mode: str,
    automatic_prefixes,
    asset_bundle_info: Dict[str, Any],
    game_version_json,
    start_time: float,
    paths: StatePaths | None = None,
) -> None:
    logger.info("RUN | step=2/2 | action=write_metadata_cache")
    sanitized_metadata = _sanitize_asset_bundle_info(asset_bundle_info)
    current_bundles: Dict[str, Dict] = sanitized_metadata.get("bundles", {})
    if not current_bundles:
        raise ValueError("bundles must be set in asset bundle info")

    scoped_bundles = filter_bundles_for_mode(current_bundles, mode)
    selection_kwargs = {
        "include_list": cfg.DL_INCLUDE_LIST,
        "exclude_list": cfg.DL_EXCLUDE_LIST,
        "automatic_prefixes": automatic_prefixes,
    }
    regional_music_selection = get_regional_music_selection_for_config(cfg, mode=mode)
    if regional_music_selection is None:
        current_bundles = select_bundles_for_download(scoped_bundles, **selection_kwargs)
        selected_before_regional = current_bundles
    else:
        selected_before_regional = select_bundles_for_download(scoped_bundles, **selection_kwargs)
        current_bundles = select_bundles_for_download(
            scoped_bundles,
            **selection_kwargs,
            music_selection=regional_music_selection,
        )
    if not current_bundles and not selected_before_regional:
        raise ValueError("No bundles found after filtering")

    metadata = {
        "version": sanitized_metadata.get("version", ""),
        "os": sanitized_metadata.get("os", ""),
        "bundles": current_bundles,
    }
    if paths is None:
        await _write_json_cache(cfg.ASSET_BUNDLE_INFO_CACHE_PATH, metadata)
        await _write_json_cache(cfg.GAME_VERSION_JSON_CACHE_PATH, game_version_json)
    else:
        observed_metadata_path = paths.asset_metadata.with_name(
            f"{paths.asset_metadata.stem}.observed{paths.asset_metadata.suffix}"
        )
        observed_version_path = paths.game_version.with_name(
            f"{paths.game_version.stem}.observed{paths.game_version.suffix}"
        )
        state_members = {
            path.resolve(strict=False)
            for path in (
                paths.queue,
                paths.asset_metadata,
                paths.game_version,
                paths.journal,
                paths.lock,
            )
        }
        observed_paths = {
            observed_metadata_path.resolve(strict=False),
            observed_version_path.resolve(strict=False),
        }
        overlap = observed_paths & state_members
        if overlap:
            raise RuntimeError(
                "observational state path aliases normal state target(s): "
                + ", ".join(str(path) for path in sorted(overlap))
            )
        atomic_write_json(observed_metadata_path, metadata, validate_asset_metadata)
        atomic_write_json(observed_version_path, game_version_json, validate_game_version)
    logger.info(
        "RUN | result=metadata_updated | path=%s | filtered_bundles=%d",
        cfg.ASSET_BUNDLE_INFO_CACHE_PATH,
        len(current_bundles),
    )
    logger.info(_RUN_COMPLETED_LOG, time.monotonic() - start_time)


async def _run_enabled_specialized_postprocess(
    mode: str,
    cfg: ConfigLike,
    extracted_dir_is_temporary: bool,
    live2d_bundles: Dict[str, Dict[str, Any]] | None = None,
    *,
    asset_metadata_version: str | None = None,
) -> None:
    enabled_modes = get_enabled_specialized_modes(mode, cfg)
    # Live2D post-processing in assets mode may run with no downloads (and its
    # extracted workspace may consequently be empty). Rebuild the model tree
    # from the configured raw bundle cache instead of requiring this run to
    # download the model bundles again. Forced live2d mode keeps its existing
    # fail-fast behavior; optional assets mode follows skip_missing_sources.
    if {"live2d", "live2d-associated"} & set(enabled_modes) and live2d_bundles is not None:
        try:
            await recover_live2d_model_outputs(cfg, live2d_bundles)
        except RuntimeError as exc:
            if mode != "assets":
                raise
            logger.warning("Skipping optional Live2D cache recovery: %s", exc)
    motion_outputs_ready = False
    for specialized_mode in enabled_modes:
        if specialized_mode == "charts":
            await run_specialized_postprocess(
                specialized_mode,
                cfg,
                extracted_dir_is_temporary=extracted_dir_is_temporary,
                skip_missing_sources=mode == "assets",
                score_include_list=cfg.DL_INCLUDE_LIST if mode == "assets" else None,
            )
        elif specialized_mode == "live2d-associated":
            await run_specialized_postprocess(
                specialized_mode,
                cfg,
                extracted_dir_is_temporary=extracted_dir_is_temporary,
                skip_missing_sources=mode == "assets",
                motion_outputs_ready=motion_outputs_ready,
                live2d_bundles=live2d_bundles,
                asset_metadata_version=asset_metadata_version,
            )
        else:
            result = await run_specialized_postprocess(
                specialized_mode,
                cfg,
                extracted_dir_is_temporary=extracted_dir_is_temporary,
                skip_missing_sources=mode == "assets",
            )
            motion_outputs_ready = result is True


async def _restore_pending_cache_on_failure(
    cfg: ConfigLike,
    mode: str,
    pending_items_outside_mode: List[DownloadItem],
    paths: StatePaths | None = None,
) -> None:
    """Keep other-mode pending items when the current mode partially fails."""
    if not pending_items_outside_mode:
        return

    failed_current_list: List[DownloadItem] = []
    if await cfg.DL_LIST_CACHE_PATH.exists():
        failed_current_list = filter_download_items_for_mode(
            await _read_json_cache(cfg.DL_LIST_CACHE_PATH), mode
        )
    queue_items = dedupe_download_items(pending_items_outside_mode + failed_current_list)
    if paths is None:
        await _write_json_cache(cfg.DL_LIST_CACHE_PATH, queue_items)
    else:
        atomic_write_json(paths.queue, [list(item) for item in queue_items], validate_pending_queue)


async def _cleanup_pending_cache_on_success(
    cfg: ConfigLike,
    download_list: List[DownloadItem],
    pending_items_outside_mode: List[DownloadItem],
    paths: StatePaths | None = None,
) -> None:
    """Drop current-mode pending items while retaining other modes' queue."""
    if not download_list:
        return

    if pending_items_outside_mode:
        if paths is None:
            await _write_json_cache(cfg.DL_LIST_CACHE_PATH, pending_items_outside_mode)
        else:
            atomic_write_json(
                paths.queue,
                [list(item) for item in pending_items_outside_mode],
                validate_pending_queue,
            )
    else:
        if paths is None:
            await cfg.DL_LIST_CACHE_PATH.unlink()
        else:
            durable_unlink(paths.queue)
    logger.debug(
        "Cleanup complete: removed pending list cache %s",
        cfg.DL_LIST_CACHE_PATH,
    )


async def _commit_successful_selection(
    cfg: ConfigLike,
    pending_items_outside_mode: List[DownloadItem],
    paths: StatePaths | None,
    asset_metadata: Dict[str, Any],
    game_version: Dict[str, Any],
) -> None:
    """Commit processed-selection provenance only after download success.

    The final queue and metadata are journaled together, so a crash cannot
    publish the new snapshot while restoring an old pending queue (or vice
    versa). The earlier pre-pipeline journal retains the prior snapshot.
    """
    queue_items = [list(item) for item in pending_items_outside_mode]
    if paths is not None:
        journal = create_journal(paths, queue_items, asset_metadata, game_version)
        replay_journal(paths, _verified_envelope=journal)
        if not queue_items:
            durable_unlink(paths.queue)
        return

    await _write_json_cache(cfg.ASSET_BUNDLE_INFO_CACHE_PATH, asset_metadata)
    await _write_json_cache(cfg.GAME_VERSION_JSON_CACHE_PATH, game_version)
    if queue_items:
        await _write_json_cache(cfg.DL_LIST_CACHE_PATH, queue_items)
    else:
        await cfg.DL_LIST_CACHE_PATH.unlink(missing_ok=True)


async def _complete_with_empty_download_list(
    cfg: ConfigLike,
    mode: str,
    pending_items_outside_mode: List[DownloadItem],
    extracted_dir_is_temporary: bool,
    start_time: float,
    paths: StatePaths | None = None,
    live2d_bundles: Dict[str, Dict[str, Any]] | None = None,
    *,
    asset_metadata_version: str | None = None,
    successful_asset_metadata: Dict[str, Any] | None = None,
    successful_game_version: Dict[str, Any] | None = None,
) -> None:
    # An assets run can legitimately have no current downloads. Specialized
    # processors still need to run: Live2D may restore its inputs from the raw
    # bundle cache, while each processor handles unavailable sources safely.
    should_postprocess = bool(get_enabled_specialized_modes(mode, cfg))
    logger.info(
        "RUN | result=noop | reason=no_items | postprocess=%s",
        should_postprocess,
    )
    if successful_asset_metadata is not None and successful_game_version is not None:
        await _commit_successful_selection(
            cfg,
            pending_items_outside_mode,
            paths,
            successful_asset_metadata,
            successful_game_version,
        )
    elif pending_items_outside_mode:
        if paths is None:
            await _write_json_cache(cfg.DL_LIST_CACHE_PATH, pending_items_outside_mode)
        else:
            atomic_write_json(
                paths.queue,
                [list(item) for item in pending_items_outside_mode],
                validate_pending_queue,
            )
    elif paths is None:
        await cfg.DL_LIST_CACHE_PATH.unlink(missing_ok=True)
    else:
        durable_unlink(paths.queue)
    if should_postprocess:
        await _run_enabled_specialized_postprocess(
            mode,
            cfg,
            extracted_dir_is_temporary,
            live2d_bundles,
            asset_metadata_version=asset_metadata_version,
        )
    logger.info(_RUN_COMPLETED_LOG, time.monotonic() - start_time)


async def _complete_with_download_list(
    cfg: ConfigLike,
    mode: str,
    headers: Dict[str, str],
    cookie,
    download_list: List[DownloadItem],
    pending_items_outside_mode: List[DownloadItem],
    extracted_dir_is_temporary: bool,
    start_time: float,
    paths: StatePaths | None = None,
    live2d_bundles: Dict[str, Dict[str, Any]] | None = None,
    *,
    asset_metadata_version: str | None = None,
    asset_metadata: Dict[str, Any] | None = None,
    provenance_candidates: List[DownloadItem] | None = None,
    successful_asset_metadata: Dict[str, Any] | None = None,
    successful_game_version: Dict[str, Any] | None = None,
) -> None:
    logger.info("RUN | action=download_list_ready | count=%d", len(download_list))

    # Persist the (merged) list so a mid-run crash can be resumed
    logger.info("RUN | step=3/4 | action=persist_queue | path=%s", cfg.DL_LIST_CACHE_PATH)
    queue_items = dedupe_download_items(pending_items_outside_mode + download_list)
    if paths is not None:
        atomic_write_json(paths.queue, [list(item) for item in queue_items], validate_pending_queue)
    else:
        await _write_json_cache(cfg.DL_LIST_CACHE_PATH, queue_items)

    can_commit_partial_success = (
        paths is not None
        and asset_metadata is not None
        and provenance_candidates is not None
        and successful_game_version is not None
    )
    failed_items: List[DownloadItem] = []
    completed_items: List[DownloadItem] = []
    is_success = await do_download(
        download_list,
        config=cfg,
        headers=headers,
        cookie=cookie,
        paths=paths,
        extracted_dir_is_temporary=extracted_dir_is_temporary,
        failed_items_out=failed_items if can_commit_partial_success else None,
        completed_items_out=completed_items if can_commit_partial_success else None,
        defer_failed_queue_write=can_commit_partial_success,
    )
    if not is_success:
        if can_commit_partial_success and failed_items:
            assert paths is not None
            assert asset_metadata is not None
            assert provenance_candidates is not None
            assert successful_game_version is not None
            failed_current_items = filter_download_items_for_mode(failed_items, mode)
            attempted_outside_mode = _pending_items_outside_mode(download_list, mode)
            final_pending = dedupe_download_items(
                pending_items_outside_mode + attempted_outside_mode + failed_current_items
            )
            withheld_provenance_names = None
            if (
                mode == "assets"
                and extracted_dir_is_temporary
                and "charts" in get_enabled_specialized_modes(mode, cfg)
            ):
                withheld_provenance_names = {
                    bundle_name
                    for _, candidate in download_list
                    if is_chart_score_bundle(candidate)
                    and isinstance(bundle_name := candidate.get("bundleName"), str)
                }
            partial_metadata = _metadata_after_partial_success(
                asset_metadata,
                provenance_candidates,
                completed_items,
                failed_items,
                withheld_provenance_names=withheld_provenance_names,
            )
            await _commit_successful_selection(
                cfg,
                final_pending,
                paths,
                partial_metadata,
                successful_game_version,
            )
        else:
            await _restore_pending_cache_on_failure(cfg, mode, pending_items_outside_mode, paths)
    else:
        if successful_asset_metadata is not None and successful_game_version is not None:
            await _commit_successful_selection(
                cfg,
                pending_items_outside_mode,
                paths,
                successful_asset_metadata,
                successful_game_version,
            )
        else:
            await _cleanup_pending_cache_on_success(
                cfg, download_list, pending_items_outside_mode, paths
            )
        await _run_enabled_specialized_postprocess(
            mode,
            cfg,
            extracted_dir_is_temporary,
            live2d_bundles,
            asset_metadata_version=asset_metadata_version,
        )

    logger.info(_RUN_COMPLETED_LOG, time.monotonic() - start_time)
