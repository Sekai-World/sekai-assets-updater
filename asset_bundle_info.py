import logging
from dataclasses import dataclass
from typing import Any, Dict

import aiohttp

from constants import NUVERSE_REGIONS
from crypto import unpack
from helpers import format_url_template, get_request_timeout, refresh_cookie
from model import ConfigLike

logger = logging.getLogger("asset_updater")


@dataclass
class AssetBundleInfoFetchResult:
    game_version_json: Dict[str, Any]
    asset_bundle_info: Dict[str, Any]
    headers: Dict[str, str]
    cookie: str | None
    asset_ver: str | None
    assetbundle_host_hash: str | None


async def build_request_headers(
    config: ConfigLike,
) -> tuple[Dict[str, str], str | None]:
    headers: Dict[str, str] = {
        "Accept": "*/*",
        "X-Unity-Version": config.UNITY_VERSION,
    }
    if config.USER_AGENT:
        headers["User-Agent"] = config.USER_AGENT

    cookie = None
    if config.GAME_COOKIE_URL:
        headers, cookie = await refresh_cookie(config, headers)

    return headers, cookie


async def fetch_asset_bundle_info(
    config: ConfigLike,
    headers: Dict[str, str] | None = None,
    cookie: str | None = None,
) -> AssetBundleInfoFetchResult:
    if headers is None:
        headers, cookie = await build_request_headers(config)

    request_timeout = get_request_timeout(config)

    game_version_json = None
    if config.GAME_VERSION_JSON_URL:
        async with aiohttp.ClientSession(timeout=request_timeout) as session:
            async with session.get(config.GAME_VERSION_JSON_URL) as response:
                if response.status == 200:
                    game_version_json = await response.json(content_type="text/plain")
                    if (
                        not isinstance(game_version_json, dict)
                        or "appVersion" not in game_version_json
                    ):
                        raise ValueError(
                            f"Invalid JSON from {config.GAME_VERSION_JSON_URL}"
                        )
                else:
                    raise RuntimeError(
                        "Failed to fetch game version json from "
                        f"{config.GAME_VERSION_JSON_URL}"
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
    if config.GAME_VERSION_URL:
        app_hash = game_version_json.get("appHash")
        if not app_hash:
            raise ValueError("appHash must be set in game version json")
        game_version_url = format_url_template(
            config.GAME_VERSION_URL,
            appVersion=game_version_json["appVersion"],
            appHash=app_hash,
        )
        async with aiohttp.ClientSession(
            proxy=config.PROXY_URL,
            timeout=request_timeout,
        ) as session:
            async with session.get(game_version_url, headers=headers) as response:
                if response.status == 200:
                    result = await response.read()
                    json_result = unpack(config.AES_KEY, config.AES_IV, result)
                    if (
                        not isinstance(json_result, dict)
                        or "assetbundleHostHash" not in json_result
                    ):
                        raise ValueError(f"Invalid result from {game_version_url}")
                    assetbundle_host_hash = json_result["assetbundleHostHash"]
                else:
                    raise RuntimeError(
                        "Failed to fetch assetbundle host hash from "
                        f"{game_version_url}"
                    )
            logger.debug(
                "Current assetbundleHostHash: %s, assetHash: %s, game version url: %s",
                assetbundle_host_hash,
                game_version_json.get("assetHash"),
                game_version_url,
            )
    else:
        logger.warning(
            "GAME_VERSION_URL is not set in the config, assuming that the "
            "assetbundleHostHash is not needed"
        )

    asset_ver = None
    if config.REGION in NUVERSE_REGIONS:
        if config.ASSET_VER_URL:
            asset_ver_url = config.ASSET_VER_URL.format(
                appVersion=(
                    getattr(config, "APP_VERSION_OVERRIDE", None)
                    or game_version_json["appVersion"]
                )
            )
            async with aiohttp.ClientSession(timeout=request_timeout) as session:
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
    if config.ASSET_BUNDLE_INFO_URL:
        if config.REGION in NUVERSE_REGIONS:
            asset_bundle_info_url = config.ASSET_BUNDLE_INFO_URL.format(
                appVersion=(
                    getattr(config, "APP_VERSION_OVERRIDE", None)
                    or game_version_json["appVersion"]
                ),
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
                config.ASSET_BUNDLE_INFO_URL,
                **asset_bundle_info_url_args,
            )
        async with aiohttp.ClientSession(timeout=request_timeout) as session:
            async with session.get(asset_bundle_info_url, headers=headers) as response:
                if response.status == 200:
                    result = await response.read()
                    asset_bundle_info = unpack(config.AES_KEY, config.AES_IV, result)
                    if not isinstance(asset_bundle_info, dict):
                        raise ValueError(f"Invalid json from {asset_bundle_info_url}")
                else:
                    result = await response.read()
                    logger.error(
                        "Failed to fetch asset bundle info from %s, status: %s, "
                        "response: %s, request headers: %s",
                        asset_bundle_info_url,
                        response.status,
                        result.decode(),
                        headers,
                    )
                    raise RuntimeError(
                        "Failed to fetch asset bundle info from "
                        f"{asset_bundle_info_url}"
                    )
    else:
        raise ValueError("ASSET_BUNDLE_INFO_URL is not set in the config")
    logger.debug(
        "Current assetBundleInfoVersion: %s, bundles length: %d",
        asset_bundle_info["version"],
        len(asset_bundle_info["bundles"]),
    )

    return AssetBundleInfoFetchResult(
        game_version_json=game_version_json,
        asset_bundle_info=asset_bundle_info,
        headers=headers,
        cookie=cookie,
        asset_ver=asset_ver,
        assetbundle_host_hash=assetbundle_host_hash,
    )
