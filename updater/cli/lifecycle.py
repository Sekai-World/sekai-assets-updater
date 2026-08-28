"""Run completion flows: download, metadata-only writes, cache restore."""

import asyncio
import logging
import time
from typing import Any, Dict, List

import orjson as json
from anyio import open_file

from updater.cli.pending import _read_json_cache, _write_json_cache
from updater.model import ConfigLike
from updater.modes import (
    filter_bundles_for_mode,
    filter_download_items_for_mode,
    get_enabled_specialized_modes,
)
from updater.net.disk_space import build_download_disk_space_gate
from updater.net.plan import (
    DownloadItem,
    dedupe_download_items,
    select_bundles_for_download,
)
from updater.pipeline import run_pipeline
from updater.postprocess.dispatch import run_specialized_postprocess
from updater.postprocess.live2d_models import recover_live2d_model_outputs
from updater.sanitize import sanitize_http_log_value
from updater.state import (
    StatePaths,
    atomic_write_json,
    durable_unlink,
    validate_asset_metadata,
    validate_game_version,
    validate_pending_queue,
)

logger = logging.getLogger("asset_updater")


async def do_download(
    dl_list: List[DownloadItem],
    config: ConfigLike,
    headers: Dict[str, str],
    cookie,
    paths: StatePaths | None = None,
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

    # Replace the original download list with the failed tasks
    if failed_tasks:
        failed_path = paths.queue if paths is not None else config.DL_LIST_CACHE_PATH
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
    current_bundles: Dict[str, Dict] = asset_bundle_info.get("bundles", {})
    if not current_bundles:
        raise ValueError("bundles must be set in asset bundle info")

    current_bundles = select_bundles_for_download(
        filter_bundles_for_mode(current_bundles, mode),
        include_list=cfg.DL_INCLUDE_LIST,
        exclude_list=cfg.DL_EXCLUDE_LIST,
        automatic_prefixes=automatic_prefixes,
    )
    if not current_bundles:
        raise ValueError("No bundles found after filtering")

    metadata = {
        "version": asset_bundle_info.get("version", ""),
        "os": asset_bundle_info.get("os", ""),
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
    logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)


async def _run_enabled_specialized_postprocess(
    mode: str,
    cfg: ConfigLike,
    extracted_dir_is_temporary: bool,
    live2d_bundles: Dict[str, Dict[str, Any]] | None = None,
) -> None:
    enabled_modes = get_enabled_specialized_modes(mode, cfg)
    # Live2D post-processing in assets mode may run with no downloads (and its
    # extracted workspace may consequently be empty). Rebuild the model tree
    # from the configured raw bundle cache instead of requiring this run to
    # download the model bundles again. Forced live2d mode keeps its existing
    # fail-fast behavior; optional assets mode follows skip_missing_sources.
    if "live2d" in enabled_modes and live2d_bundles is not None:
        try:
            await recover_live2d_model_outputs(cfg, live2d_bundles)
        except RuntimeError as exc:
            if mode != "assets":
                raise
            logger.warning("Skipping optional Live2D cache recovery: %s", exc)
    for specialized_mode in enabled_modes:
        if specialized_mode == "charts":
            await run_specialized_postprocess(
                specialized_mode,
                cfg,
                extracted_dir_is_temporary=extracted_dir_is_temporary,
                skip_missing_sources=mode == "assets",
                score_include_list=cfg.DL_INCLUDE_LIST if mode == "assets" else None,
            )
        else:
            await run_specialized_postprocess(
                specialized_mode,
                cfg,
                extracted_dir_is_temporary=extracted_dir_is_temporary,
                skip_missing_sources=mode == "assets",
            )


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


async def _complete_with_empty_download_list(
    cfg: ConfigLike,
    mode: str,
    pending_items_outside_mode: List[DownloadItem],
    extracted_dir_is_temporary: bool,
    start_time: float,
    paths: StatePaths | None = None,
    live2d_bundles: Dict[str, Dict[str, Any]] | None = None,
) -> None:
    # An assets run can legitimately have no current downloads. Specialized
    # processors still need to run: Live2D may restore its inputs from the raw
    # bundle cache, while each processor handles unavailable sources safely.
    should_postprocess = bool(get_enabled_specialized_modes(mode, cfg))
    logger.info(
        "RUN | result=noop | reason=no_items | postprocess=%s",
        should_postprocess,
    )
    if pending_items_outside_mode:
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
        if live2d_bundles is None:
            await _run_enabled_specialized_postprocess(mode, cfg, extracted_dir_is_temporary)
        else:
            await _run_enabled_specialized_postprocess(
                mode, cfg, extracted_dir_is_temporary, live2d_bundles
            )
    logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)


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
) -> None:
    logger.info("RUN | action=download_list_ready | count=%d", len(download_list))

    # Persist the (merged) list so a mid-run crash can be resumed
    logger.info("RUN | step=3/4 | action=persist_queue | path=%s", cfg.DL_LIST_CACHE_PATH)
    queue_items = dedupe_download_items(pending_items_outside_mode + download_list)
    if paths is not None:
        atomic_write_json(paths.queue, [list(item) for item in queue_items], validate_pending_queue)
    else:
        await _write_json_cache(cfg.DL_LIST_CACHE_PATH, queue_items)

    is_success = await do_download(
        download_list, config=cfg, headers=headers, cookie=cookie, paths=paths
    )
    if not is_success:
        await _restore_pending_cache_on_failure(cfg, mode, pending_items_outside_mode, paths)
    else:
        await _cleanup_pending_cache_on_success(
            cfg, download_list, pending_items_outside_mode, paths
        )
        if live2d_bundles is None:
            await _run_enabled_specialized_postprocess(mode, cfg, extracted_dir_is_temporary)
        else:
            await _run_enabled_specialized_postprocess(
                mode, cfg, extracted_dir_is_temporary, live2d_bundles
            )

    logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)
