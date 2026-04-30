import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, cast

import orjson as json
from anyio import open_file

from asset_bundle_info import build_request_headers, fetch_asset_bundle_info
from helpers import (
    build_download_disk_space_gate,
    ensure_dir_exists,
    filter_bundles,
    get_download_list,
    setup_logging_queue,
)
from model import ConfigLike
from worker import run_pipeline

logger = logging.getLogger("asset_updater")

DownloadItem = Tuple[str, Dict[str, Any]]


config: Optional[ConfigLike] = None


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


async def main(
    update_asset_bundle_info_only: bool = False,
    force_full_download: bool = False,
):
    cfg = require_config()
    start_time = time.monotonic()

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

        current_bundles = await filter_bundles(
            current_bundles,
            include_list=cfg.DL_INCLUDE_LIST,
            exclude_list=cfg.DL_EXCLUDE_LIST,
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

    # Generate the download list from the latest version info
    logger.info("RUN | step=2/4 | action=build_download_list")
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
    )
    logger.debug("New download candidates: %d item(s)", len(new_download_list))

    # If there are pending items from a previous interrupted run, merge them:
    # existing pending items come first so they are retried, new items follow.
    # Items that already appear in the pending list are not duplicated.
    pending_list: List[DownloadItem] = []
    if (not force_full_download) and await cfg.DL_LIST_CACHE_PATH.exists():
        async with await open_file(cfg.DL_LIST_CACHE_PATH, "r") as f:
            pending_list = json.loads(await f.read())
        logger.info(
            "RUN | action=load_pending | count=%d | path=%s",
            len(pending_list),
            cfg.DL_LIST_CACHE_PATH,
        )

    if pending_list and new_download_list:
        pending_bundle_names = {
            bundle.get("bundleName") for _, bundle in pending_list
        }
        deduped_new = [
            item for item in new_download_list
            if item[1].get("bundleName") not in pending_bundle_names
        ]
        download_list: List[DownloadItem] = pending_list + deduped_new
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
        logger.info("RUN | result=noop | reason=no_items")
        logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)
        return

    logger.info("RUN | action=download_list_ready | count=%d", len(download_list))

    # Persist the (merged) list so a mid-run crash can be resumed
    logger.info("RUN | step=3/4 | action=persist_queue | path=%s", cfg.DL_LIST_CACHE_PATH)
    async with await open_file(cfg.DL_LIST_CACHE_PATH, "wb") as f:
        await f.write(json.dumps(download_list, option=json.OPT_INDENT_2))

    is_success = await do_download(
        download_list, config=cfg, headers=headers, cookie=cookie
    )

    # remove the cached download list
    if is_success and len(download_list) > 0:
        await cfg.DL_LIST_CACHE_PATH.unlink()
        logger.debug(
            "Cleanup complete: removed pending list cache %s",
            cfg.DL_LIST_CACHE_PATH,
        )

    logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)


def cli():
    # Accept command line arguments
    import argparse

    parser = argparse.ArgumentParser(
        description="Start the asset updater with given config."
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
        )
    )


if __name__ == "__main__":
    cli()
