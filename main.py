import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple, cast

import aiohttp
import orjson as json
from anyio import open_file

from constants import NUVERSE_REGIONS
from crypto import unpack
from helpers import (
    ensure_dir_exists,
    filter_bundles,
    format_url_template,
    get_download_list,
    refresh_cookie,
    setup_logging_queue,
)
from model import ConfigLike
from worker import worker

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
    logger.info("Starting download...")
    # Create a queue to manage tasks
    queue = asyncio.Queue()

    # Populate the queue with download tasks
    for url, bundle in dl_list:
        await queue.put((url, bundle))

    # List to track failed tasks
    failed_tasks = []

    async def worker_task(worker_id):
        nonlocal failed_tasks
        while not queue.empty():
            url, bundle = await queue.get()
            try:
                await worker(
                    f"download_worker-{worker_id}",
                    (url, bundle),
                    config,
                    headers,
                    cookie=cookie,
                )
            except Exception as e:
                # Log the error and add the task to failed_tasks
                logger.exception("Failed to download %s: %s", url, e)
                failed_tasks.append((url, bundle))
            finally:
                queue.task_done()

    # Create and run worker tasks
    workers = [
        asyncio.create_task(worker_task(worker_id))
        for worker_id in range(config.MAX_CONCURRENCY)
    ]
    await queue.join()

    # Wait for all workers to finish
    await asyncio.gather(*workers, return_exceptions=True)

    # Replace the original download list with the failed tasks
    if failed_tasks:
        failed_path = config.DL_LIST_CACHE_PATH
        async with await open_file(failed_path, "wb") as f:
            await f.write(json.dumps(failed_tasks, option=json.OPT_INDENT_2))
        logger.info("Failed tasks saved to %s", failed_path)

        return False
    else:
        logger.info("All tasks completed successfully")
        return True


async def main(update_asset_bundle_info_only: bool = False):
    cfg = require_config()

    # ensure required directories exist
    await ensure_dir_exists(cfg.DL_LIST_CACHE_PATH.parent)
    await ensure_dir_exists(cfg.ASSET_BUNDLE_INFO_CACHE_PATH.parent)
    await ensure_dir_exists(cfg.GAME_VERSION_JSON_CACHE_PATH.parent)

    headers: Dict[str, str] = {
        "Accept": "*/*",
        "X-Unity-Version": cfg.UNITY_VERSION,
    }
    if cfg.USER_AGENT:
        headers["User-Agent"] = cfg.USER_AGENT

    cookie = None

    # Cookie must be filled if GAME_COOKIE_URL is set in the config
    if cfg.GAME_COOKIE_URL:
        headers, cookie = await refresh_cookie(cfg, headers)

    if (not update_asset_bundle_info_only) and await cfg.DL_LIST_CACHE_PATH.exists():
        logger.info(
            "Cache file %s exists, loading from cache", cfg.DL_LIST_CACHE_PATH
        )
        is_success = False
        # Load the dl_list from the cache and start downloading
        async with await open_file(cfg.DL_LIST_CACHE_PATH, "r") as f:
            dl_list: List[DownloadItem] = json.loads(await f.read())
            logger.info("%d items to download", len(dl_list))
            is_success = await do_download(
                dl_list, config=cfg, headers=headers, cookie=cookie
            )

        # remove the cache file
        if is_success:
            await cfg.DL_LIST_CACHE_PATH.unlink()
        return

    game_version_json = None
    # Download, parse and cache the game version json from GAME_VERSION_JSON_URL
    if cfg.GAME_VERSION_JSON_URL:
        async with aiohttp.ClientSession() as session:
            async with session.get(cfg.GAME_VERSION_JSON_URL) as response:
                if response.status == 200:
                    game_version_json = await response.json(content_type="text/plain")
                    # Check if the json is valid
                    if (
                        not isinstance(game_version_json, dict)
                        or "appVersion" not in game_version_json
                    ):
                        raise ValueError(f"Invalid JSON from {cfg.GAME_VERSION_JSON_URL}")
                else:
                    raise RuntimeError(
                        f"Failed to fetch game version json from {cfg.GAME_VERSION_JSON_URL}"
                    )
    else:
        raise ValueError("GAME_VERSION_JSON_URL is not set in the config")
    logger.debug(
        "Current appVersion: %s, dataVersion: %s, assetVersion: %s",
        game_version_json["appVersion"],
        game_version_json["dataVersion"],
        game_version_json["assetVersion"],
    )

    assetbundle_host_hash = None
    # Format GAME_VERSION_URL using the appVersion and appHash from the game version json
    if cfg.GAME_VERSION_URL:
        app_hash = game_version_json.get("appHash")
        if not app_hash:
            raise ValueError("appHash must be set in game version json")
        game_version_url = format_url_template(
            cfg.GAME_VERSION_URL,
            appVersion=game_version_json["appVersion"],
            appHash=app_hash,
        )
        # This request needs to be proxied
        async with aiohttp.ClientSession(proxy=cfg.PROXY_URL) as session:
            async with session.get(game_version_url, headers=headers) as response:
                if response.status == 200:
                    result = await response.read()
                    json_result = unpack(cfg.AES_KEY, cfg.AES_IV, result)
                    # Check if the json is valid
                    if (
                        not isinstance(json_result, dict)
                        or "assetbundleHostHash" not in json_result
                    ):
                        raise ValueError(f"Invalid result from {game_version_url}")
                    assetbundle_host_hash = json_result["assetbundleHostHash"]
                else:
                    raise RuntimeError(
                        f"Failed to fetch assetbundle host hash from {game_version_url}"
                    )
            logger.debug(
                "Current assetbundleHostHash: %s, assetHash: %s, game version url: %s",
                assetbundle_host_hash,
                game_version_json.get("assetHash"),
                game_version_url,
            )
    else:
        logger.warning(
            "GAME_VERSION_URL is not set in the config, assuming that the assetbundleHostHash is not needed"
        )
        
    asset_ver = None
    # Format ASSET_VER_URL using the appVersion from the game version json
    if cfg.REGION in NUVERSE_REGIONS:
        if cfg.ASSET_VER_URL:
            asset_ver_url = cfg.ASSET_VER_URL.format(
                appVersion=(cfg.APP_VERSION_OVERRIDE or game_version_json["appVersion"])
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(asset_ver_url, headers=headers) as response:
                    if response.status == 200:
                        result = await response.read()
                        asset_ver = result.decode()
                    else:
                        raise RuntimeError(
                            f"Failed to fetch asset version from {asset_ver_url}"
                        )
        else:
            raise ValueError("ASSET_VER_URL is not set in the config")

    asset_bundle_info = None
    # Format ASSET_BUNDLE_INFO_URL using the information above
    if cfg.ASSET_BUNDLE_INFO_URL:
        if cfg.REGION in NUVERSE_REGIONS:
            asset_bundle_info_url = cfg.ASSET_BUNDLE_INFO_URL.format(
                appVersion=(cfg.APP_VERSION_OVERRIDE or game_version_json["appVersion"]),
                assetVer=asset_ver,
            )
        else:
            asset_bundle_info_url_args = {
                "assetbundleHostHash": assetbundle_host_hash,
                "assetVersion": game_version_json["assetVersion"],
            }
            asset_hash = game_version_json.get("assetHash")
            if asset_hash:
                asset_bundle_info_url_args["assetHash"] = asset_hash
            asset_bundle_info_url = format_url_template(
                cfg.ASSET_BUNDLE_INFO_URL,
                **asset_bundle_info_url_args,
            )
        async with aiohttp.ClientSession() as session:
            async with session.get(asset_bundle_info_url, headers=headers) as response:
                if response.status == 200:
                    result = await response.read()
                    asset_bundle_info = unpack(cfg.AES_KEY, cfg.AES_IV, result)
                    # Check if the json is valid
                    if not isinstance(asset_bundle_info, dict):
                        raise ValueError(f"Invalid json from {asset_bundle_info_url}")
                else:
                    result = await response.read()
                    logger.error(
                        f"Failed to fetch asset bundle info from {asset_bundle_info_url}, status: {response.status}, response: {result.decode()}, request headers: {headers}"
                    )
                    raise RuntimeError(
                        f"Failed to fetch asset bundle info from {asset_bundle_info_url}"
                    )
    else:
        raise ValueError("ASSET_BUNDLE_INFO_URL is not set in the config")
    logger.debug(
        "Current assetBundleInfoVersion: %s, bundles length: %d",
        asset_bundle_info["version"],
        len(asset_bundle_info["bundles"]),
    )

    if update_asset_bundle_info_only:
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
        logger.info(
            "Updated asset bundle info cache only: %s",
            cfg.ASSET_BUNDLE_INFO_CACHE_PATH,
        )
        return

    # Generate the download list
    download_list: List[DownloadItem] = await get_download_list(
        asset_bundle_info,
        game_version_json,
        config=cfg,
        assetver=asset_ver,
        assetbundle_host_hash=assetbundle_host_hash,
        include_list=cfg.DL_INCLUDE_LIST,
        exclude_list=cfg.DL_EXCLUDE_LIST,
        priority_list=cfg.DL_PRIORITY_LIST,
    )
    logger.info("Download list generated, %d items to download", len(download_list))

    is_success = await do_download(
        download_list, config=cfg, headers=headers, cookie=cookie
    )

    # remove the cached download list
    if is_success and len(download_list) > 0:
        await cfg.DL_LIST_CACHE_PATH.unlink()


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
        "--update-asset-bundle-info-only",
        action="store_true",
        help=(
            "Fetch and update asset_bundle_info.json only; do not generate dl_list.json "
            "and do not start download tasks."
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
    if args.verbose:
        logging.basicConfig(
            level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s"
        )
    else:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
        )

    setup_logging_queue()

    # Run the main function
    asyncio.run(
        main(update_asset_bundle_info_only=args.update_asset_bundle_info_only)
    )


if __name__ == "__main__":
    cli()
