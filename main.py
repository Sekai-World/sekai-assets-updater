import asyncio
import logging
import os
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple, cast

import orjson as json
from anyio import open_file

from asset_bundle_info import build_request_headers, fetch_asset_bundle_info
from helpers import (
    build_download_disk_space_gate,
    DownloadPlan,
    ensure_dir_exists,
    filter_bundles_for_mode,
    filter_download_items_for_mode,
    get_mode_bundle_prefixes,
    get_download_list,
    dedupe_download_items,
    sanitize_http_log_value,
    select_bundles_for_download,
    setup_logging_queue,
)
from model import ConfigLike
from specialized import (
    get_enabled_specialized_modes,
    get_required_bundle_prefixes,
    mode_uses_bundle_pipeline,
    needs_live2d_bundle_cache,
    needs_shared_workspace,
    run_specialized_postprocess,
)
from worker import get_bundle_cache_path, run_pipeline
from state import (
    StateLock,
    StateNotFoundError,
    StatePaths,
    atomic_write_json,
    create_journal,
    derive_state_paths,
    durable_unlink,
    load_pending_queue,
    prepare_state_directory,
    replay_journal,
    validate_asset_metadata,
    validate_game_version,
    validate_pending_queue,
)

logger = logging.getLogger("asset_updater")

DownloadItem = Tuple[str, Dict[str, Any]]


config: Optional[ConfigLike] = None


def _pending_items_outside_mode(items: List[DownloadItem], mode: str) -> List[DownloadItem]:
    """Keep pending entries for other bundle namespaces when rewriting the cache."""
    prefixes = get_mode_bundle_prefixes(mode)
    if not prefixes:
        return []
    return [item for item in items if not (item[1].get("bundleName") or "").startswith(prefixes)]


def require_config() -> ConfigLike:
    if config is None:
        raise ImportError(
            "Config module not loaded. Please run the script with the config argument."
        )
    return config


def validate_config(cfg: ConfigLike) -> None:
    """Reject unsafe or unusable runtime settings before starting the pipeline."""
    concurrency_names = (
        "MAX_CONCURRENCY",
        "MAX_CONCURRENCY_DOWNLOADS",
        "MAX_CONCURRENCY_EXTRACTS",
        "MAX_CONCURRENCY_UPLOAD_STAGE",
        "PIPELINE_STAGE_QUEUE_SIZE",
        "MAX_CONCURRENT_AUDIO_FILES",
        "MAX_CONCURRENCY_HCA_DECODES",
        "MAX_CONCURRENCY_AUDIO_ENCODERS",
        "MAX_CONCURRENCY_AUDIO_TRANSCODES",
        "MAX_CONCURRENCY_VIDEO_TRANSCODES",
        "MAX_CONCURRENCY_USM_DEMUXES",
        "MAX_CONCURRENCY_UPLOADS",
    )
    errors: list[str] = []
    for name in concurrency_names:
        value = getattr(cfg, name, None)
        if type(value) is not int or value <= 0:
            errors.append(f"{name} must be a positive integer (got {value!r})")

    max_retries = getattr(cfg, "DOWNLOAD_MAX_RETRIES", None)
    if type(max_retries) is not int or max_retries < 1:
        errors.append(
            f"DOWNLOAD_MAX_RETRIES must be an integer of at least 1 (got {max_retries!r})"
        )

    timeout = getattr(cfg, "EXTERNAL_PROCESS_TIMEOUT", None)
    try:
        valid_timeout = float(timeout) > 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        valid_timeout = False
    if not valid_timeout:
        errors.append(f"EXTERNAL_PROCESS_TIMEOUT must be a positive number (got {timeout!r})")

    key = getattr(cfg, "AES_KEY", None)
    iv = getattr(cfg, "AES_IV", None)
    if not isinstance(key, bytes) or len(key) not in {16, 24, 32}:
        errors.append("AES_KEY must be bytes with length 16, 24, or 32")
    if not isinstance(iv, bytes) or len(iv) != 16:
        errors.append("AES_IV must be bytes with length 16")

    def require_program(program: object, label: str) -> None:
        if not program:
            errors.append(f"{label} executable is not configured")
        elif not isinstance(program, str):
            errors.append(f"{label} executable must be a string")
        elif shutil.which(program) is None and not (
            os.path.isfile(program) and os.access(program, os.X_OK)
        ):
            errors.append(f"{label} executable not found: {program}")

    require_program("ffmpeg", "ffmpeg")
    backend = str(getattr(cfg, "HCA_DECODE_BACKEND", "auto")).strip().lower()
    if backend == "vgmstream":
        require_program(os.environ.get("VGMSTREAM_CLI", "vgmstream-cli"), "vgmstream-cli")
    if getattr(cfg, "ASSET_REMOTE_STORAGE", None):
        for index, storage in enumerate(cfg.ASSET_REMOTE_STORAGE):
            if storage.get("type") == "normal":
                require_program(storage.get("program"), f"upload storage {index}")

    if errors:
        raise ValueError("Invalid configuration:\n- " + "\n- ".join(errors))


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


async def _write_json_cache(path, data) -> None:
    async with await open_file(path, "wb") as f:
        await f.write(json.dumps(data, option=json.OPT_INDENT_2))


async def _read_json_cache(path):
    async with await open_file(path, "r") as f:
        return json.loads(await f.read())


async def _ensure_run_cache_dirs(cfg: ConfigLike) -> None:
    await ensure_dir_exists(cfg.DL_LIST_CACHE_PATH.parent)
    await ensure_dir_exists(cfg.ASSET_BUNDLE_INFO_CACHE_PATH.parent)
    await ensure_dir_exists(cfg.GAME_VERSION_JSON_CACHE_PATH.parent)


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


async def _build_new_download_list(
    cfg: ConfigLike,
    mode: str,
    automatic_prefixes,
    asset_bundle_info: Dict[str, Any],
    game_version_json,
    asset_ver,
    assetbundle_host_hash,
    force_full_download: bool,
) -> tuple[List[DownloadItem], DownloadPlan]:
    logger.info("RUN | step=2/4 | action=build_download_list")
    # get_download_list applies the user filters and writes the metadata cache;
    # the mandatory mode scope is applied both before and after it so it cannot
    # be bypassed by a cached queue or a broad include expression.
    scoped_info = dict(asset_bundle_info)
    scoped_info["bundles"] = filter_bundles_for_mode(scoped_info.get("bundles", {}), mode)
    plan: DownloadPlan = await get_download_list(
        scoped_info,
        game_version_json,
        config=cfg,
        assetver=asset_ver,
        assetbundle_host_hash=assetbundle_host_hash,
        include_list=cfg.DL_INCLUDE_LIST,
        exclude_list=cfg.DL_EXCLUDE_LIST,
        priority_list=cfg.DL_PRIORITY_LIST,
        force_full_download=force_full_download,
        automatic_prefixes=automatic_prefixes,
        bundle_cache_path_resolver=lambda bundle: get_bundle_cache_path(cfg, bundle),
        asset_bundle_info_for_cache=asset_bundle_info,
    )
    new_download_list = filter_download_items_for_mode(plan.candidates, mode)
    logger.debug("New download candidates: %d item(s)", len(new_download_list))
    return new_download_list, plan


async def _load_pending_download_lists(
    cfg: ConfigLike,
    mode: str,
    force_full_download: bool,
) -> Tuple[List[DownloadItem], List[DownloadItem]]:
    """Load cached pending items and the subset belonging to the current mode."""
    if not await cfg.DL_LIST_CACHE_PATH.exists():
        return [], []

    try:
        cached_pending_list = cast(List[DownloadItem], load_pending_queue(cfg.DL_LIST_CACHE_PATH))
    except StateNotFoundError:
        return [], []
    if force_full_download:
        return cached_pending_list, []

    pending_list = filter_download_items_for_mode(cached_pending_list, mode)
    logger.info(
        "RUN | action=load_pending | count=%d | path=%s",
        len(pending_list),
        cfg.DL_LIST_CACHE_PATH,
    )
    return cached_pending_list, pending_list


def _merge_pending_and_new_download_lists(
    pending_list: List[DownloadItem],
    new_download_list: List[DownloadItem],
) -> List[DownloadItem]:
    """Merge pending retries ahead of new candidates without duplicates."""
    if pending_list and new_download_list:
        current_by_name = {
            bundle.get("bundleName"): (url, bundle) for url, bundle in new_download_list
        }
        ordered_pending = [
            current_by_name.get(bundle.get("bundleName"), (url, bundle))
            for url, bundle in pending_list
        ]
        pending_bundle_names = {bundle.get("bundleName") for _, bundle in ordered_pending}
        deduped_new = [
            item
            for item in new_download_list
            if item[1].get("bundleName") not in pending_bundle_names
        ]
        download_list = dedupe_download_items(ordered_pending + deduped_new)
        logger.info(
            "RUN | action=merge_download_list | pending=%d | new=%d | total=%d",
            len(pending_list),
            len(deduped_new),
            len(download_list),
        )
        return download_list
    if pending_list:
        logger.info("RUN | action=retry_pending_only | count=%d", len(pending_list))
        return pending_list
    return new_download_list


async def _run_enabled_specialized_postprocess(
    mode: str,
    cfg: ConfigLike,
    extracted_dir_is_temporary: bool,
) -> None:
    for specialized_mode in get_enabled_specialized_modes(mode, cfg):
        await run_specialized_postprocess(
            specialized_mode,
            cfg,
            extracted_dir_is_temporary=extracted_dir_is_temporary,
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
    headers: Dict[str, str],
    cookie,
    pending_items_outside_mode: List[DownloadItem],
    extracted_dir_is_temporary: bool,
    start_time: float,
    paths: StatePaths | None = None,
) -> None:
    logger.info("RUN | result=noop | reason=no_items | postprocess=true")
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
    await _run_enabled_specialized_postprocess(mode, cfg, extracted_dir_is_temporary)
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
        await _run_enabled_specialized_postprocess(mode, cfg, extracted_dir_is_temporary)
        await _cleanup_pending_cache_on_success(
            cfg, download_list, pending_items_outside_mode, paths
        )

    logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)


async def _run_full_download_pipeline(
    cfg: ConfigLike,
    mode: str,
    force_full_download: bool,
    extracted_dir_is_temporary: bool,
    automatic_prefixes,
    fetch_result,
    start_time: float,
    paths: StatePaths | None = None,
) -> None:
    # Charts consume extracted scores from local/normal remote storage and do
    # not participate in the asset-bundle download pipeline at all.
    new_download_list, plan = await _build_new_download_list(
        cfg,
        mode,
        automatic_prefixes,
        fetch_result.asset_bundle_info,
        fetch_result.game_version_json,
        fetch_result.asset_ver,
        fetch_result.assetbundle_host_hash,
        force_full_download,
    )
    cached_pending_list, pending_list = await _load_pending_download_lists(
        cfg, mode, force_full_download
    )
    pending_items_outside_mode = _pending_items_outside_mode(cached_pending_list, mode)
    download_list = _merge_pending_and_new_download_lists(pending_list, new_download_list)

    if paths is not None and not paths.journal.exists():
        queue_items = dedupe_download_items(pending_items_outside_mode + download_list)
        create_journal(
            paths, [list(item) for item in queue_items], plan.asset_metadata, plan.game_version
        )
        replay_journal(paths)

    if not download_list:
        await _complete_with_empty_download_list(
            cfg,
            mode,
            fetch_result.headers,
            fetch_result.cookie,
            pending_items_outside_mode,
            extracted_dir_is_temporary,
            start_time,
            paths,
        )
        return

    await _complete_with_download_list(
        cfg,
        mode,
        fetch_result.headers,
        fetch_result.cookie,
        download_list,
        pending_items_outside_mode,
        extracted_dir_is_temporary,
        start_time,
        paths,
    )


async def _run_main(
    update_asset_bundle_info_only: bool = False,
    force_full_download: bool = False,
    mode: str = "assets",
    extracted_dir_is_temporary: bool = False,
):
    cfg = require_config()
    setattr(cfg, "UPDATER_MODE", mode)
    start_time = time.monotonic()
    automatic_prefixes = get_required_bundle_prefixes(mode, cfg)

    run_mode = "metadata-only" if update_asset_bundle_info_only else "full-pipeline"
    logger.info(
        "RUN | status=start | mode=%s | force_full_download=%s",
        run_mode,
        force_full_download,
    )

    await _ensure_run_cache_dirs(cfg)
    shared_dir = cfg.DL_LIST_CACHE_PATH.parent
    prepare_state_directory(shared_dir)
    paths = derive_state_paths(
        cfg.DL_LIST_CACHE_PATH,
        cfg.ASSET_BUNDLE_INFO_CACHE_PATH,
        cfg.GAME_VERSION_JSON_CACHE_PATH,
    )
    lock = StateLock(paths.lock)
    lock.acquire()
    try:
        replay_journal(paths)
        await _run_main_locked(
            cfg,
            update_asset_bundle_info_only,
            force_full_download,
            mode,
            extracted_dir_is_temporary,
            paths,
        )
    finally:
        lock.release()


async def _run_main_locked(
    cfg: ConfigLike,
    update_asset_bundle_info_only: bool,
    force_full_download: bool,
    mode: str,
    extracted_dir_is_temporary: bool,
    paths: StatePaths,
) -> None:
    start_time = time.monotonic()
    automatic_prefixes = get_required_bundle_prefixes(mode, cfg)
    headers, cookie = await build_request_headers(cfg)

    if force_full_download:
        logger.info("RUN | option=force_full_download | cache_metadata=false | cache_pending=false")

    logger.info("RUN | step=1/4 | action=fetch_metadata")
    fetch_result = await fetch_asset_bundle_info(cfg, headers=headers, cookie=cookie)
    logger.info(
        "RUN | action=metadata_fetched | asset_ver=%s | bundle_count=%d",
        fetch_result.asset_ver,
        len(fetch_result.asset_bundle_info.get("bundles", {})),
    )

    if update_asset_bundle_info_only:
        await _write_metadata_only_cache(
            cfg,
            mode,
            automatic_prefixes,
            fetch_result.asset_bundle_info,
            fetch_result.game_version_json,
            start_time,
            paths,
        )
        return

    await _run_full_download_pipeline(
        cfg,
        mode,
        force_full_download,
        extracted_dir_is_temporary,
        automatic_prefixes,
        fetch_result,
        start_time,
        paths=paths,
    )


async def main(
    update_asset_bundle_info_only: bool = False,
    force_full_download: bool = False,
    mode: str = "assets",
):
    """Run the updater with only the specialized run-scoped storage it needs."""
    cfg = require_config()
    if not mode_uses_bundle_pipeline(mode):
        if update_asset_bundle_info_only:
            logger.info("RUN | result=noop | reason=metadata_only_not_applicable_to_charts")
            return

        needs_extracted_workspace = needs_shared_workspace(mode, cfg)
        if not needs_extracted_workspace:
            return await run_specialized_postprocess("charts", cfg)

    needs_extracted_workspace = needs_shared_workspace(mode, cfg)
    needs_live2d_cache = needs_live2d_bundle_cache(mode, cfg)
    if not needs_extracted_workspace and not needs_live2d_cache:
        return await _run_main(update_asset_bundle_info_only, force_full_download, mode)

    original_extracted_dir = cfg.ASSET_LOCAL_EXTRACTED_DIR
    original_live2d_dir = getattr(cfg, "LIVE2D_BUNDLE_CACHE_DIR", None)
    needs_run_temp_root = needs_extracted_workspace or needs_live2d_cache
    if not needs_run_temp_root:
        await run_specialized_postprocess("charts", cfg)
        return

    with tempfile.TemporaryDirectory(prefix="sekai-assets-") as temp_dir:
        from anyio import Path

        root = Path(temp_dir)
        if needs_live2d_cache:
            cfg.LIVE2D_BUNDLE_CACHE_DIR = root / "live2d-bundle"
        if needs_extracted_workspace:
            cfg.ASSET_LOCAL_EXTRACTED_DIR = root / "extracted"
        try:
            if mode == "charts":
                return await run_specialized_postprocess(
                    "charts",
                    cfg,
                    extracted_dir_is_temporary=needs_extracted_workspace,
                )
            return await _run_main(
                update_asset_bundle_info_only,
                force_full_download,
                mode,
                extracted_dir_is_temporary=needs_extracted_workspace,
            )
        finally:
            cfg.ASSET_LOCAL_EXTRACTED_DIR = original_extracted_dir
            cfg.LIVE2D_BUNDLE_CACHE_DIR = original_live2d_dir


def cli():
    # Accept command line arguments
    import argparse

    parser = argparse.ArgumentParser(description="Start the asset updater with given config.")
    parser.add_argument(
        "--mode",
        choices=("assets", "live2d", "charts"),
        default="assets",
        help="Processing scope (default: assets).",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        help="Path to the config python file.",
        required=True,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging.")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Only output warnings and errors.",
    )
    parser.add_argument(
        "--update-asset-bundle-info-only",
        action="store_true",
        help=(
            "Fetch and update asset_bundle_info.json only; do not generate dl_list.json "
            "and do not start download tasks."
        ),
    )
    parser.add_argument(
        "--force-full-download",
        action="store_true",
        help=(
            "Ignore cached json metadata and cached dl_list.json, rebuild a full "
            "dl_list.json from current metadata, then download/process all matched bundles."
        ),
    )
    args = parser.parse_args()

    # Load the config python file as dynamic module
    import importlib.util
    import sys

    global config

    spec = importlib.util.spec_from_file_location("config", args.config)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load config module from {args.config}")

    loaded_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded_config)
    sys.modules["config"] = loaded_config
    config = cast(ConfigLike, loaded_config)
    validate_config(config)

    # Set the logging level
    log_level = logging.INFO
    if args.quiet:
        log_level = logging.WARNING
    elif args.verbose:
        log_level = logging.DEBUG

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    setup_logging_queue()

    logger.debug(
        "CLI options | config=%s | log_level=%s | mode=%s | force_full_download=%s",
        args.config,
        logging.getLevelName(log_level),
        "metadata-only" if args.update_asset_bundle_info_only else "full-pipeline",
        args.force_full_download,
    )

    # Run the main function
    asyncio.run(
        main(
            update_asset_bundle_info_only=args.update_asset_bundle_info_only,
            force_full_download=args.force_full_download,
            mode=args.mode,
        )
    )


if __name__ == "__main__":
    cli()
