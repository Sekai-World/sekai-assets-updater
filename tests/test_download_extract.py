# ruff: noqa: E402

import asyncio
import logging
import sys
from pathlib import Path as StdPath
from typing import Any, Dict, Optional, cast

PROJECT_ROOT = StdPath(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp
import json_compat as json
from anyio import Path, open_file

from constants import NUVERSE_REGIONS
from crypto import unpack
from helpers import (
    ensure_dir_exists,
    format_url_template,
    refresh_cookie,
    setup_logging_queue,
)
from main import DownloadItem, do_download
from model import ConfigLike

logger = logging.getLogger("asset_updater")


config: Optional[ConfigLike] = None


def require_config() -> ConfigLike:
    if config is None:
        raise ImportError(
            "Config module not loaded. Please run the script with the config argument."
        )
    return config


async def _load_json_cache(path: Path) -> Dict[str, Any]:
    async with await open_file(path) as f:
        data = json.loads(await f.read())
    if not isinstance(data, dict):
        raise ValueError(f"Invalid JSON cache: {path}")
    return data


async def _get_assetbundle_host_hash(
    cfg: ConfigLike,
    headers: Dict[str, str],
    game_version_json: Dict[str, Any],
) -> str | None:
    if not cfg.GAME_VERSION_URL:
        logger.warning(
            "GAME_VERSION_URL is not set in the config, assuming that the assetbundleHostHash is not needed"
        )
        return None

    app_hash = game_version_json.get("appHash")
    if not app_hash:
        raise ValueError("appHash must be set in game version json")

    game_version_url = format_url_template(
        cfg.GAME_VERSION_URL,
        appVersion=game_version_json["appVersion"],
        appHash=app_hash,
    )
    async with aiohttp.ClientSession(proxy=cfg.PROXY_URL) as session:
        async with session.get(game_version_url, headers=headers) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Failed to fetch assetbundle host hash from {game_version_url}"
                )

            result = await response.read()
            json_result = unpack(cfg.AES_KEY, cfg.AES_IV, result)
            if (
                not isinstance(json_result, dict)
                or "assetbundleHostHash" not in json_result
            ):
                raise ValueError(f"Invalid result from {game_version_url}")

            assetbundle_host_hash = json_result["assetbundleHostHash"]

    logger.debug(
        "Current assetbundleHostHash: %s, assetHash: %s, game version url: %s",
        assetbundle_host_hash,
        game_version_json.get("assetHash"),
        game_version_url,
    )
    return assetbundle_host_hash


async def _get_asset_ver(
    cfg: ConfigLike,
    headers: Dict[str, str],
    game_version_json: Dict[str, Any],
) -> str | None:
    if cfg.REGION not in NUVERSE_REGIONS:
        return None

    if not cfg.ASSET_VER_URL:
        raise ValueError("ASSET_VER_URL is not set in the config")

    asset_ver_url = cfg.ASSET_VER_URL.format(
        appVersion=(cfg.APP_VERSION_OVERRIDE or game_version_json["appVersion"])
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(asset_ver_url, headers=headers) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"Failed to fetch asset version from {asset_ver_url}"
                )
            return (await response.read()).decode()


def _build_download_url(
    cfg: ConfigLike,
    bundle: Dict[str, Any],
    asset_bundle_info: Dict[str, Any],
    game_version_json: Dict[str, Any],
    assetbundle_host_hash: str | None,
    asset_ver: str | None,
) -> str:
    if asset_ver:
        app_version = (
            cfg.APP_VERSION_OVERRIDE or game_version_json.get("appVersion") or ""
        )
        if not app_version:
            raise ValueError("App version must be set in game version json or config")

        return format_url_template(
            cfg.ASSET_BUNDLE_URL,
            appVersion=app_version,
            bundleName=bundle.get("bundleName"),
            downloadPath=bundle.get("downloadPath"),
        )

    version = asset_bundle_info.get("version")
    if not version:
        raise ValueError("Version must be set in asset bundle info")

    asset_bundle_url_args = {
        "assetbundleHostHash": assetbundle_host_hash,
        "version": version,
    }
    asset_hash = game_version_json.get("assetHash")
    if asset_hash:
        asset_bundle_url_args["assetHash"] = asset_hash

    return format_url_template(
        cfg.ASSET_BUNDLE_URL,
        **asset_bundle_url_args,
        bundleName=bundle.get("bundleName"),
    )


async def main(bundle_name: str):
    cfg = require_config()

    await ensure_dir_exists(cfg.DL_LIST_CACHE_PATH.parent)
    await ensure_dir_exists(cfg.ASSET_BUNDLE_INFO_CACHE_PATH.parent)
    await ensure_dir_exists(cfg.GAME_VERSION_JSON_CACHE_PATH.parent)

    asset_bundle_info = await _load_json_cache(cfg.ASSET_BUNDLE_INFO_CACHE_PATH)
    game_version_json = await _load_json_cache(cfg.GAME_VERSION_JSON_CACHE_PATH)

    bundles = asset_bundle_info.get("bundles", {})
    if not isinstance(bundles, dict):
        raise ValueError("bundles must be set in asset bundle info")

    bundle_list = [
        bundle
        for bundle in bundles.values()
        if bundle.get("bundleName", "").startswith(bundle_name)
    ]
    if not bundle_list:
        raise ValueError(
            f"Bundles starting with {bundle_name} not found in asset bundle info"
        )

    headers: Dict[str, str] = {
        "Accept": "*/*",
        "X-Unity-Version": cfg.UNITY_VERSION,
    }
    if cfg.USER_AGENT:
        headers["User-Agent"] = cfg.USER_AGENT

    cookie = None
    if cfg.GAME_COOKIE_URL:
        headers, cookie = await refresh_cookie(cfg, headers)

    assetbundle_host_hash = await _get_assetbundle_host_hash(
        cfg, headers, game_version_json
    )
    asset_ver = await _get_asset_ver(cfg, headers, game_version_json)

    dl_infos: list[DownloadItem] = [
        (
            _build_download_url(
                cfg,
                bundle,
                asset_bundle_info,
                game_version_json,
                assetbundle_host_hash,
                asset_ver,
            ),
            bundle,
        )
        for bundle in bundle_list
    ]

    is_success = await do_download(
        dl_infos,
        config=cfg,
        headers=headers,
        cookie=cookie,
    )
    if not is_success:
        raise RuntimeError("Download or extraction failed")


def cli():
    import argparse
    import importlib.util
    import sys

    parser = argparse.ArgumentParser(
        description="Download and extract matching asset bundles from cached metadata."
    )
    parser.add_argument(
        "bundle_name",
        type=str,
        help="Bundle name prefix to download.",
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
    args = parser.parse_args()

    global config

    spec = importlib.util.spec_from_file_location("config", args.config)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load config module from {args.config}")

    loaded_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded_config)
    sys.modules["config"] = loaded_config
    config = cast(ConfigLike, loaded_config)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    setup_logging_queue()

    asyncio.run(main(args.bundle_name))


if __name__ == "__main__":
    cli()
