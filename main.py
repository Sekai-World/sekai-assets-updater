import asyncio
import logging
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple, cast

import orjson as json
from anyio import open_file

from asset_bundle_info import build_request_headers, fetch_asset_bundle_info
from helpers import (
    build_download_disk_space_gate,
    ensure_dir_exists,
    filter_bundles_for_mode,
    filter_download_items_for_mode,
    get_mode_bundle_prefixes,
    get_download_list,
    dedupe_download_items,
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

logger = logging.getLogger("asset_updater")

DownloadItem = Tuple[str, Dict[str, Any]]


config: Optional[ConfigLike] = None


def _pending_items_outside_mode(
    items: List[DownloadItem], mode: str
) -> List[DownloadItem]:
    """Keep pending entries for other bundle namespaces when rewriting the cache."""
    prefixes = get_mode_bundle_prefixes(mode)
    if not prefixes:
        return []
    return [
        item
        for item in items
        if not (item[1].get("bundleName") or "").startswith(prefixes)
    ]


def require_config() -> ConfigLike:
    if config is None:
        raise ImportError(
            "Config module not loaded. Please run the script with the config argument."
        )
    return config


async def do_download(
    dl_list: List[DownloadItem],
    config: ConfigLike,
    headers: Dict[str, str],
    cookie,
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

    try:
        failed_tasks = await run_pipeline(
            dl_list,
            config,
            headers,
            cookie=cookie,
            download_disk_space_gate=download_disk_space_gate,
        )
    except Exception:
        logger.exception(
            "ERROR | stage=pipeline | action=crash | preserve_pending=true | items=%d",
            len(dl_list),
        )
        failed_tasks = dl_list

    # Replace the original download list with the failed tasks
    if failed_tasks:
        failed_path = config.DL_LIST_CACHE_PATH
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


async def _run_main(
    update_asset_bundle_info_only: bool = False,
    force_full_download: bool = False,
    mode: str = "assets",
    extracted_dir_is_temporary: bool = False,
):
    cfg = require_config()
    cfg.UPDATER_MODE = mode
    start_time = time.monotonic()
    automatic_prefixes = get_required_bundle_prefixes(mode, cfg)

    run_mode = "metadata-only" if update_asset_bundle_info_only else "full-pipeline"
    logger.info(
        "RUN | status=start | mode=%s | force_full_download=%s",
        run_mode,
        force_full_download,
    )

    # ensure required directories exist
    await ensure_dir_exists(cfg.DL_LIST_CACHE_PATH.parent)
    await ensure_dir_exists(cfg.ASSET_BUNDLE_INFO_CACHE_PATH.parent)
    await ensure_dir_exists(cfg.GAME_VERSION_JSON_CACHE_PATH.parent)
    headers, cookie = await build_request_headers(cfg)

    if force_full_download:
        logger.info(
            "RUN | option=force_full_download | cache_metadata=false | cache_pending=false"
        )

    logger.info("RUN | step=1/4 | action=fetch_metadata")
    fetch_result = await fetch_asset_bundle_info(cfg, headers=headers, cookie=cookie)
    headers = fetch_result.headers
    cookie = fetch_result.cookie
    game_version_json = fetch_result.game_version_json
    asset_ver = fetch_result.asset_ver
    assetbundle_host_hash = fetch_result.assetbundle_host_hash
    asset_bundle_info = fetch_result.asset_bundle_info

    logger.info(
        "RUN | action=metadata_fetched | asset_ver=%s | bundle_count=%d",
        asset_ver,
        len(asset_bundle_info.get("bundles", {})),
    )

    if update_asset_bundle_info_only:
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

        async with await open_file(cfg.ASSET_BUNDLE_INFO_CACHE_PATH, "wb") as f:
            await f.write(
                json.dumps(
                    {
                        "version": asset_bundle_info.get("version", ""),
                        "os": asset_bundle_info.get("os", ""),
                        "bundles": current_bundles,
                    },
                    option=json.OPT_INDENT_2,
                )
            )
        async with await open_file(cfg.GAME_VERSION_JSON_CACHE_PATH, "wb") as f:
            await f.write(json.dumps(game_version_json, option=json.OPT_INDENT_2))
        logger.info(
            "RUN | result=metadata_updated | path=%s | filtered_bundles=%d",
            cfg.ASSET_BUNDLE_INFO_CACHE_PATH,
            len(current_bundles),
        )
        logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)
        return

    # Charts consume extracted scores from local/normal remote storage and do
    # not participate in the asset-bundle download pipeline at all.
    # Generate the download list from the latest version info
    logger.info("RUN | step=2/4 | action=build_download_list")
    # get_download_list applies the user filters and writes the metadata cache;
    # the mandatory mode scope is applied both before and after it so it cannot
    # be bypassed by a cached queue or a broad include expression.
    asset_bundle_info = dict(asset_bundle_info)
    asset_bundle_info["bundles"] = filter_bundles_for_mode(
        asset_bundle_info.get("bundles", {}), mode
    )
    new_download_list: List[DownloadItem] = await get_download_list(
        asset_bundle_info,
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
    )
    new_download_list = filter_download_items_for_mode(new_download_list, mode)
    logger.debug("New download candidates: %d item(s)", len(new_download_list))

    # If there are pending items from a previous interrupted run, merge them:
    # existing pending items come first so they are retried, new items follow.
    # Items that already appear in the pending list are not duplicated.
    cached_pending_list: List[DownloadItem] = []
    pending_list: List[DownloadItem] = []
    if (not force_full_download) and await cfg.DL_LIST_CACHE_PATH.exists():
        async with await open_file(cfg.DL_LIST_CACHE_PATH, "r") as f:
            cached_pending_list = json.loads(await f.read())
            pending_list = filter_download_items_for_mode(cached_pending_list, mode)
        logger.info(
            "RUN | action=load_pending | count=%d | path=%s",
            len(pending_list),
            cfg.DL_LIST_CACHE_PATH,
        )

    pending_items_outside_mode = _pending_items_outside_mode(cached_pending_list, mode)

    if pending_list and new_download_list:
        pending_bundle_names = {
            bundle.get("bundleName") for _, bundle in pending_list
        }
        deduped_new = [
            item for item in new_download_list
            if item[1].get("bundleName") not in pending_bundle_names
        ]
        download_list: List[DownloadItem] = dedupe_download_items(
            pending_list + deduped_new
        )
        logger.info(
            "RUN | action=merge_download_list | pending=%d | new=%d | total=%d",
            len(pending_list),
            len(deduped_new),
            len(download_list),
        )
    elif pending_list:
        download_list = pending_list
        logger.info(
            "RUN | action=retry_pending_only | count=%d", len(pending_list)
        )
    else:
        download_list = new_download_list

    if not download_list:
        logger.info("RUN | result=noop | reason=no_items | postprocess=true")
        if pending_items_outside_mode:
            async with await open_file(cfg.DL_LIST_CACHE_PATH, "wb") as f:
                await f.write(
                    json.dumps(pending_items_outside_mode, option=json.OPT_INDENT_2)
                )
        is_success = await do_download([], config=cfg, headers=headers, cookie=cookie)
        if is_success:
            for specialized_mode in get_enabled_specialized_modes(mode, cfg):
                await run_specialized_postprocess(
                    specialized_mode,
                    cfg,
                    extracted_dir_is_temporary=extracted_dir_is_temporary,
                )
        logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)
        return

    logger.info("RUN | action=download_list_ready | count=%d", len(download_list))

    # Persist the (merged) list so a mid-run crash can be resumed
    logger.info("RUN | step=3/4 | action=persist_queue | path=%s", cfg.DL_LIST_CACHE_PATH)
    async with await open_file(cfg.DL_LIST_CACHE_PATH, "wb") as f:
        await f.write(
            json.dumps(
                dedupe_download_items(pending_items_outside_mode + download_list),
                option=json.OPT_INDENT_2,
            )
        )

    is_success = await do_download(
        download_list, config=cfg, headers=headers, cookie=cookie
    )

    if not is_success and pending_items_outside_mode:
        failed_current_list: List[DownloadItem] = []
        if await cfg.DL_LIST_CACHE_PATH.exists():
            async with await open_file(cfg.DL_LIST_CACHE_PATH, "r") as f:
                failed_current_list = filter_download_items_for_mode(
                    json.loads(await f.read()), mode
                )
        async with await open_file(cfg.DL_LIST_CACHE_PATH, "wb") as f:
            await f.write(
                json.dumps(
                    dedupe_download_items(
                        pending_items_outside_mode + failed_current_list
                    ),
                    option=json.OPT_INDENT_2,
                )
            )

    if is_success:
        for specialized_mode in get_enabled_specialized_modes(mode, cfg):
            await run_specialized_postprocess(
                specialized_mode,
                cfg,
                extracted_dir_is_temporary=extracted_dir_is_temporary,
            )

    # remove the cached download list
    if is_success and len(download_list) > 0:
        if pending_items_outside_mode:
            async with await open_file(cfg.DL_LIST_CACHE_PATH, "wb") as f:
                await f.write(
                    json.dumps(
                        pending_items_outside_mode, option=json.OPT_INDENT_2
                    )
                )
        else:
            await cfg.DL_LIST_CACHE_PATH.unlink()
        logger.debug(
            "Cleanup complete: removed pending list cache %s",
            cfg.DL_LIST_CACHE_PATH,
        )

    logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)


async def main(
    update_asset_bundle_info_only: bool = False,
    force_full_download: bool = False,
    mode: str = "assets",
):
    """Run the updater with only the specialized run-scoped storage it needs."""
    cfg = require_config()
    if not mode_uses_bundle_pipeline(mode):
        if update_asset_bundle_info_only:
            logger.info(
                "RUN | result=noop | reason=metadata_only_not_applicable_to_charts"
            )
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

    parser = argparse.ArgumentParser(
        description="Start the asset updater with given config."
    )
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
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging."
    )
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
