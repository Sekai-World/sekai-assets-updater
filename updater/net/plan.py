"""Download-list planning: fetch, change-detect, select, dedupe, and sort."""

import inspect
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from updater.net.urls import format_url_template, get_template_placeholders
from updater.state import (
    StateNotFoundError,
    StateValidationError,
    load_asset_metadata,
    load_game_version,
)

logger = logging.getLogger("asset_updater")


DownloadItem = Tuple[str, Dict]


@dataclass(frozen=True)
class DownloadPlan:
    """In-memory selection result; construction performs no persistence."""

    candidates: List[Tuple[str, Dict]]
    asset_metadata: Dict
    game_version: Dict


def get_bundle_checksum(bundle: Dict) -> Tuple[str | None, str]:
    """Return the best available checksum field for a bundle.

    Colorful Palette servers currently expose `hash`, while tc/cn/kr may leave
    `hash` empty and require `crc` for change detection.
    """
    bundle_hash = bundle.get("hash")
    if bundle_hash:
        return "hash", str(bundle_hash)

    bundle_crc = bundle.get("crc")
    if bundle_crc not in (None, ""):
        return "crc", str(bundle_crc)

    return None, ""


def bundle_has_changed(bundle: Dict, cached_bundle: Dict | None) -> bool:
    """Compare two bundle records using the checksum fields they actually expose."""
    cached_bundle = cached_bundle or {}

    bundle_hash = bundle.get("hash")
    cached_hash = cached_bundle.get("hash")
    if bundle_hash and cached_hash:
        return str(bundle_hash) != str(cached_hash)

    bundle_crc = bundle.get("crc")
    cached_crc = cached_bundle.get("crc")
    if bundle_crc not in (None, "") and cached_crc not in (None, ""):
        return str(bundle_crc) != str(cached_crc)

    return get_bundle_checksum(bundle) != get_bundle_checksum(cached_bundle)


async def get_download_list(
    asset_bundle_info: Dict,
    game_version_json: Dict,
    config=None,
    assetver: str | None = None,
    assetbundle_host_hash: str | None = None,
    include_list: List[str] | None = None,
    exclude_list: List[str] | None = None,
    priority_list: List[str] | None = None,
    force_full_download: bool = False,
    automatic_prefixes: tuple[str, ...] = (),
    bundle_cache_path_resolver=None,
    asset_bundle_info_for_cache: Dict | None = None,
) -> DownloadPlan:
    """Generate the download list for the asset bundles.

    Args:
        asset_bundle_info (Dict): current asset bundle info
        game_version_json (Dict): current game version json
        config (Module, optional): configurations. Defaults to None.
        assetver (str, optional): asset ver used by nuverse servers. Defaults to None.
        assetbundle_host_hash (str, optional): host hash used by colorful palette servers. Defaults to None.

    Returns:
        List[Tuple[str, Dict]]: download list of asset bundles
    """

    cached_asset_bundle_info = None
    cached_game_version_json = None
    assert config, "Config must be provided to get_download_list"
    assert config.ASSET_BUNDLE_INFO_CACHE_PATH, "ASSET_BUNDLE_INFO_CACHE_PATH must be set in config"
    assert config.GAME_VERSION_JSON_CACHE_PATH, "GAME_VERSION_JSON_CACHE_PATH must be set in config"
    if not force_full_download:
        try:
            cached_asset_bundle_info = load_asset_metadata(config.ASSET_BUNDLE_INFO_CACHE_PATH)
        except StateNotFoundError:
            pass
        except StateValidationError:
            # Cache formats are intentionally strict. An older or malformed
            # metadata cache is not authoritative; refresh it from the already
            # fetched network response by treating it as absent.
            logger.warning(
                "Ignoring incompatible asset metadata cache: %s",
                config.ASSET_BUNDLE_INFO_CACHE_PATH,
            )
        try:
            cached_game_version_json = load_game_version(config.GAME_VERSION_JSON_CACHE_PATH)
        except StateNotFoundError:
            pass
        except StateValidationError:
            # Only tolerate validation failures while reading a prior cache.
            # Validation of the current fetched response still happens when it
            # is committed below the lifecycle layer.
            logger.warning(
                "Ignoring incompatible game version cache: %s",
                config.GAME_VERSION_JSON_CACHE_PATH,
            )

    if assetver is not None:
        game_version_json = dict(game_version_json)
        game_version_json["assetver"] = assetver

    download_list = []
    current_bundles: Dict[str, Dict] = asset_bundle_info.get("bundles", {})
    assert current_bundles, "bundles must be set in asset bundle info"
    asset_bundle_url_placeholders = get_template_placeholders(config.ASSET_BUNDLE_URL)
    current_bundles = select_bundles_for_download(
        current_bundles,
        include_list=include_list,
        exclude_list=exclude_list,
        automatic_prefixes=automatic_prefixes,
    )
    if not current_bundles:
        raise ValueError("No bundles found after filtering")

    async def select_changed_bundles(cached_bundles: Dict[str, Dict]) -> list[Dict]:
        changed_bundles = []
        for bundle in current_bundles.values():
            if bundle_has_changed(bundle, cached_bundles.get(bundle.get("bundleName", ""), {})):
                changed_bundles.append(bundle)
                continue
            if bundle_cache_path_resolver is None:
                continue
            cache_path = bundle_cache_path_resolver(bundle)
            if cache_path is None:
                continue
            exists = cache_path.exists()
            if inspect.isawaitable(exists):
                exists = await exists
            if not exists:
                changed_bundles.append(bundle)
        return changed_bundles

    if cached_asset_bundle_info and cached_game_version_json:
        cached_bundles: Dict[str, Dict] = cached_asset_bundle_info.get("bundles") or {}
        changed_bundles = await select_changed_bundles(cached_bundles)

        if assetver:
            # Generate the download list from changed bundles
            app_version: str = (
                getattr(config, "APP_VERSION_OVERRIDE", None)
                or game_version_json.get("appVersion")
                or ""
            )
            assert app_version, "App version must be set in game version json or config"
            download_list = [
                (
                    format_url_template(
                        config.ASSET_BUNDLE_URL,
                        appVersion=app_version,
                        bundleName=bundle.get("bundleName"),
                        downloadPath=bundle.get("downloadPath"),
                    ),
                    bundle,
                )
                for bundle in changed_bundles
            ]
        else:
            # Colorful Palette servers. changed_bundles was already computed via
            # select_changed_bundles above, which performs the same checksum
            # comparison and additionally selects unchanged bundles whose
            # configured cache file is missing (matching the assetver branch).

            # Generate the download list from changed bundles
            asset_hash: str = game_version_json.get("assetHash", "")
            asset_bundle_url_args = {
                "assetbundleHostHash": assetbundle_host_hash,
            }
            if "version" in asset_bundle_url_placeholders:
                version = asset_bundle_info.get("version")
                assert version, "Version must be set in asset bundle info"
                asset_bundle_url_args["version"] = version
            if asset_hash:
                asset_bundle_url_args["assetHash"] = asset_hash
            download_list = [
                (
                    format_url_template(
                        config.ASSET_BUNDLE_URL,
                        **asset_bundle_url_args,
                        bundleName=bundle.get("bundleName"),
                    ),
                    bundle,
                )
                for bundle in changed_bundles
            ]

    else:
        # Get the download list for a full download
        asset_hash: str = game_version_json.get("assetHash", "")
        app_version: str = (
            getattr(config, "APP_VERSION_OVERRIDE", None)
            or game_version_json.get("appVersion")
            or ""
        )
        assert app_version, "App version must be set in game version json or config"
        asset_bundle_url_args = {
            "assetbundleHostHash": assetbundle_host_hash,
            "appVersion": app_version,
        }
        if "version" in asset_bundle_url_placeholders:
            version = asset_bundle_info.get("version")
            assert version, "Version must be set in asset bundle info"
            asset_bundle_url_args["version"] = version
        if asset_hash:
            asset_bundle_url_args["assetHash"] = asset_hash

        download_list = [
            (
                format_url_template(
                    config.ASSET_BUNDLE_URL,
                    **asset_bundle_url_args,
                    bundleName=bundle.get("bundleName"),
                    downloadPath=bundle.get("downloadPath"),
                ),
                bundle,
            )
            for bundle in current_bundles.values()
        ]

    if download_list:
        download_list = await sort_download_list(
            download_list,
            priority_list=priority_list,
        )

    metadata_source = (
        asset_bundle_info if asset_bundle_info_for_cache is None else asset_bundle_info_for_cache
    )
    normalized_metadata = {
        "version": metadata_source.get("version", ""),
        "os": metadata_source.get("os", ""),
        "bundles": metadata_source.get("bundles", {}),
    }
    return DownloadPlan(download_list, normalized_metadata, game_version_json)


def select_bundles_for_download(
    bundles: Dict[str, Dict],
    include_list: List[str] | None = None,
    exclude_list: List[str] | None = None,
    automatic_prefixes: tuple[str, ...] = (),
) -> Dict[str, Dict]:
    """Select user bundles and merge mandatory specialized bundles."""
    selected: Dict[str, Dict] = {}
    selected_names: set[str] = set()
    for key, value in bundles.items():
        bundle_name = value.get("bundleName") or ""
        user_selected = (
            not include_list or any(re.match(pattern, bundle_name) for pattern in include_list)
        ) and not any(re.match(pattern, bundle_name) for pattern in (exclude_list or []))
        automatic_selected = bundle_name.startswith(automatic_prefixes)
        if (user_selected or automatic_selected) and bundle_name not in selected_names:
            selected[key] = value
            selected_names.add(bundle_name)
    return selected


def dedupe_download_items(items: List[Tuple[str, Dict]]) -> List[Tuple[str, Dict]]:
    result: List[Tuple[str, Dict]] = []
    seen_names: set[str] = set()
    for item in items:
        bundle_name = item[1].get("bundleName") or ""
        if bundle_name not in seen_names:
            result.append(item)
            seen_names.add(bundle_name)
    return result


async def sort_download_list(
    download_list: List[Tuple[str, Dict]],
    priority_list: List[str] | None = None,
) -> List[Tuple[str, Dict]]:
    """Sort the download list alphabetically and then based on priority list."""
    download_list = sorted(
        download_list,
        key=lambda item: item[1].get("bundleName") or "",
    )

    # If a priority list is provided, sort matching groups in declaration order
    # and leave unmatched bundles at the end.  The initial name sort provides a
    # deterministic order for bundles in the same group.
    if priority_list:
        download_list = sorted(
            download_list,
            key=lambda item: next(
                (
                    index
                    for index, test_name in enumerate(priority_list)
                    if re.match(test_name, item[1].get("bundleName") or "")
                ),
                len(priority_list),
            ),
        )

    return download_list
