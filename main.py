import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, cast

from asset_bundle_info import build_request_headers, fetch_asset_bundle_info
from helpers import (
    build_download_disk_space_gate,
    DownloadPlan,
    ensure_dir_exists,
    filter_bundles,
    get_download_list,
    setup_logging_queue,
)
from model import ConfigLike
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
    paths: StatePaths,
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
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "ERROR | stage=pipeline | action=crash | preserve_pending=true | items=%d",
            len(dl_list),
        )
        raise

    # Replace the original download list with the failed tasks
    if failed_tasks:
        failed_path = paths.queue
        atomic_write_json(
            failed_path,
            [list(item) for item in failed_tasks],
            validate_pending_queue,
        )
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
            atomic_write_json(
                observed_metadata_path,
                {
                    "version": asset_bundle_info.get("version", ""),
                    "os": asset_bundle_info.get("os", ""),
                    "bundles": current_bundles,
                },
                validate_asset_metadata,
            )
            atomic_write_json(observed_version_path, game_version_json, validate_game_version)
            logger.info(
                "RUN | result=metadata_updated | path=%s | filtered_bundles=%d",
                observed_metadata_path,
                len(current_bundles),
            )
            logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)
            return

        logger.info("RUN | step=2/4 | action=build_download_list")
        plan: DownloadPlan = await get_download_list(
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
        logger.debug("New download candidates: %d item(s)", len(plan.candidates))

        pending_list: List[DownloadItem] = []
        if not force_full_download:
            try:
                pending_list = cast(List[DownloadItem], load_pending_queue(paths.queue))
            except StateNotFoundError:
                pass
            logger.info(
                "RUN | action=load_pending | count=%d | path=%s",
                len(pending_list),
                paths.queue,
            )

        current_by_name = {bundle.get("bundleName"): (url, bundle) for url, bundle in plan.candidates}
        pending_names = {bundle.get("bundleName") for _, bundle in pending_list}
        ordered_pending = [
            current_by_name.get(bundle.get("bundleName"), (url, bundle))
            for url, bundle in pending_list
        ]
        deduped_new = [item for item in plan.candidates if item[1].get("bundleName") not in pending_names]
        download_list: List[DownloadItem] = ordered_pending + deduped_new
        if pending_list:
            logger.info(
                "RUN | action=merge_download_list | pending=%d | new=%d | total=%d",
                len(pending_list),
                len(deduped_new),
                len(download_list),
            )

        logger.info("RUN | step=3/4 | action=commit_state | path=%s", paths.queue)
        create_journal(
            paths,
            [list(item) for item in download_list],
            plan.asset_metadata,
            plan.game_version,
        )
        replay_journal(paths)

        if not download_list:
            logger.info("RUN | result=noop | reason=no_items")
            durable_unlink(paths.queue)
            logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)
            return

        logger.info("RUN | action=download_list_ready | count=%d", len(download_list))

        is_success = await do_download(
            download_list, config=cfg, headers=headers, cookie=cookie, paths=paths
        )

        if is_success and len(download_list) > 0:
            durable_unlink(paths.queue)
            logger.debug("Cleanup complete: removed pending list cache %s", paths.queue)

        logger.info("RUN | status=completed | duration_sec=%.2f", time.monotonic() - start_time)
    finally:
        lock.release()


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
