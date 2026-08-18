import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict

import aiohttp

from constants import NUVERSE_REGIONS
from crypto import unpack
from helpers import (
    build_metadata_headers,
    format_url_template,
    get_http_session_options,
    refresh_cookie,
    sanitize_headers,
    sanitize_url,
)
from model import ConfigLike

logger = logging.getLogger("asset_updater")


def normalize_asset_bundle_info(
    asset_bundle_info: Dict[str, Any], *, fallback_asset_ver: str | None = None
) -> Dict[str, Any]:
    """Normalize API compatibility values without inventing integrity data.

    CN, KR, and TC metadata can represent the absent ``hash`` value as JSON
    ``null`` while supplying ``crc`` as the bundle's usable checksum. State
    records encode that known absence as an empty string paired with CRC. Do
    not normalize a null hash when no CRC was supplied: the strict state
    validation path must reject bundles with no integrity identifier.

    Those endpoints can also omit the metadata revision despite returning an
    authoritative asset-version response. Only use that fetched value when the
    metadata revision is not already a non-empty string. All other malformed
    values continue to reach the strict state-validation boundary unchanged.
    """
    normalized_info = dict(asset_bundle_info)
    version = normalized_info.get("version")
    if isinstance(version, str) and version.strip():
        pass
    elif isinstance(fallback_asset_ver, str) and fallback_asset_ver.strip():
        normalized_info["version"] = fallback_asset_ver
    bundles = normalized_info.get("bundles")
    if not isinstance(bundles, dict):
        return normalized_info

    normalized_bundles: Dict[str, Any] = {}
    for bundle_name, bundle in bundles.items():
        if not isinstance(bundle, dict):
            normalized_bundles[bundle_name] = bundle
            continue
        normalized_bundle = dict(bundle)
        if normalized_bundle.get("hash") is None and normalized_bundle.get("crc") not in (None, ""):
            normalized_bundle["hash"] = ""
        normalized_bundles[bundle_name] = normalized_bundle

    normalized_info["bundles"] = normalized_bundles
    return normalized_info


def _transport_error(operation: str, url: str, exc: BaseException) -> RuntimeError:
    return RuntimeError(f"{operation} failed for {sanitize_url(url)} ({type(exc).__name__})")


@asynccontextmanager
async def _safe_http_request(session, url: str, headers, operation: str):
    transport_error = None
    try:
        async with session.get(url, headers=headers) as response:
            yield response
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        transport_error = _transport_error(operation, url, exc)
    if transport_error is not None:
        raise transport_error


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
    headers = build_metadata_headers(config)

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

    game_version_json = None
    if config.GAME_VERSION_JSON_URL:
        try:
            async with aiohttp.ClientSession(**get_http_session_options(config)) as session:
                async with _safe_http_request(
                    session,
                    config.GAME_VERSION_JSON_URL,
                    headers,
                    "Failed to fetch game version json",
                ) as response:
                    if response.status == 200:
                        game_version_json = await response.json(content_type="text/plain")
                        if (
                            not isinstance(game_version_json, dict)
                            or "appVersion" not in game_version_json
                        ):
                            raise ValueError(
                                f"Invalid JSON from {sanitize_url(config.GAME_VERSION_JSON_URL)}"
                            )
                    else:
                        raise RuntimeError(
                            "Failed to fetch game version json from "
                            f"{sanitize_url(config.GAME_VERSION_JSON_URL)}"
                        )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise _transport_error(
                "Failed to fetch game version json", config.GAME_VERSION_JSON_URL, exc
            ) from None
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
        async with aiohttp.ClientSession(**get_http_session_options(config)) as session:
            async with _safe_http_request(
                session, game_version_url, headers, "Failed to fetch assetbundle host hash"
            ) as response:
                if response.status == 200:
                    result = await response.read()
                    json_result = unpack(config.AES_KEY, config.AES_IV, result)
                    if (
                        not isinstance(json_result, dict)
                        or "assetbundleHostHash" not in json_result
                    ):
                        raise ValueError(f"Invalid result from {sanitize_url(game_version_url)}")
                    assetbundle_host_hash = json_result["assetbundleHostHash"]
                else:
                    raise RuntimeError(
                        "Failed to fetch assetbundle host hash from %s, status: %s, "
                        "response headers: %s, request headers: %s"
                        % (
                            sanitize_url(game_version_url),
                            response.status,
                            sanitize_headers(response.headers),
                            sanitize_headers(headers),
                        )
                    )
            logger.debug(
                "Current assetbundleHostHash: %s, assetHash: %s, game version url: %s",
                assetbundle_host_hash,
                game_version_json.get("assetHash"),
                sanitize_url(game_version_url),
            )
    else:
        logger.warning(
            "GAME_VERSION_URL is not set in the config, assuming that the "
            "assetbundleHostHash is not needed"
        )

    asset_ver = None
    if config.REGION in NUVERSE_REGIONS:
        if config.ASSET_VER_URL:
            asset_ver_url = format_url_template(
                config.ASSET_VER_URL,
                appVersion=(
                    getattr(config, "APP_VERSION_OVERRIDE", None)
                    or game_version_json.get("appVersion")
                ),
            )
            async with aiohttp.ClientSession(**get_http_session_options(config)) as session:
                async with _safe_http_request(
                    session, asset_ver_url, headers, "Failed to fetch asset version"
                ) as response:
                    if response.status == 200:
                        result = await response.read()
                        asset_ver = result.decode()
                    else:
                        raise RuntimeError(
                            f"Failed to fetch asset version from {sanitize_url(asset_ver_url)}"
                        )
        else:
            raise ValueError("ASSET_VER_URL is not set in the config")

    asset_bundle_info = None
    if config.ASSET_BUNDLE_INFO_URL:
        if config.REGION in NUVERSE_REGIONS:
            asset_bundle_info_url = format_url_template(
                config.ASSET_BUNDLE_INFO_URL,
                appVersion=(
                    getattr(config, "APP_VERSION_OVERRIDE", None)
                    or game_version_json.get("appVersion")
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
        async with aiohttp.ClientSession(**get_http_session_options(config)) as session:
            async with _safe_http_request(
                session,
                asset_bundle_info_url,
                headers,
                "Failed to fetch asset bundle info",
            ) as response:
                if response.status == 200:
                    result = await response.read()
                    asset_bundle_info = unpack(config.AES_KEY, config.AES_IV, result)
                    if not isinstance(asset_bundle_info, dict):
                        raise ValueError(f"Invalid json from {sanitize_url(asset_bundle_info_url)}")
                    asset_bundle_info = normalize_asset_bundle_info(
                        asset_bundle_info, fallback_asset_ver=asset_ver
                    )
                else:
                    logger.error(
                        "Failed to fetch asset bundle info from %s, status: %s, "
                        "request headers: %s",
                        sanitize_url(asset_bundle_info_url),
                        response.status,
                        sanitize_headers(dict(headers)),
                    )
                    raise RuntimeError(
                        "Failed to fetch asset bundle info from "
                        f"{sanitize_url(asset_bundle_info_url)}"
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
