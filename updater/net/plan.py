"""Download-list planning: fetch, change-detect, select, dedupe, and sort."""

import copy
import inspect
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

from updater.net.urls import format_url_template, get_template_placeholders
from updater.state import (
    StateNotFoundError,
    StatePersistenceError,
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
    successful_asset_metadata: Dict | None = None


_PROVENANCE_KEY = "_sekai_assets_updater"


def _sanitize_asset_bundle_info(asset_bundle_info: Dict) -> Dict:
    """Copy a manifest while discarding server-supplied local provenance."""
    source_bundles = asset_bundle_info.get("bundles", {})
    if isinstance(source_bundles, dict):
        bundles = {}
        for key, bundle in source_bundles.items():
            sanitized_bundle = copy.deepcopy(bundle)
            if isinstance(sanitized_bundle, dict):
                sanitized_bundle.pop(_PROVENANCE_KEY, None)
            bundles[key] = sanitized_bundle
    else:
        bundles = copy.deepcopy(source_bundles)
    return {
        "version": copy.deepcopy(asset_bundle_info.get("version", "")),
        "os": copy.deepcopy(asset_bundle_info.get("os", "")),
        "bundles": bundles,
    }


def _processed_checksum(bundle: Dict) -> Dict[str, str] | None:
    field, value = get_bundle_checksum(bundle)
    if field not in {"hash", "crc"} or not value:
        return None
    return {"field": field, "value": value}


def _bundle_has_current_provenance(bundle: Dict, observed_bundle: Dict) -> bool:
    provenance = bundle.get(_PROVENANCE_KEY)
    if not isinstance(provenance, dict):
        return False
    marker = provenance.get("processed_checksum")
    return marker == _processed_checksum(observed_bundle)


def _merge_trusted_provenance(current_metadata: Dict, cached_metadata: Dict | None) -> Dict:
    """Carry validated local markers onto the current sanitized manifest."""
    merged = copy.deepcopy(current_metadata)
    cached_bundles = (cached_metadata or {}).get("bundles") or {}
    for key, bundle in merged["bundles"].items():
        cached_bundle = cached_bundles.get(key)
        if isinstance(bundle, dict) and isinstance(cached_bundle, dict):
            marker = cached_bundle.get(_PROVENANCE_KEY)
            if marker is not None:
                bundle[_PROVENANCE_KEY] = copy.deepcopy(marker)
    return merged


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


def _load_cached_metadata(config, force_full_download: bool) -> tuple[Dict | None, Dict | None]:
    if force_full_download:
        return None, None

    cached_asset_bundle_info = None
    cached_game_version_json = None
    try:
        cached_asset_bundle_info = load_asset_metadata(config.ASSET_BUNDLE_INFO_CACHE_PATH)
    except StateNotFoundError:
        cached_asset_bundle_info = None
    except StateValidationError:
        logger.warning(
            "Ignoring incompatible asset metadata cache: %s",
            config.ASSET_BUNDLE_INFO_CACHE_PATH,
        )
    try:
        cached_game_version_json = load_game_version(config.GAME_VERSION_JSON_CACHE_PATH)
    except StateNotFoundError:
        cached_game_version_json = None
    except StateValidationError:
        logger.warning(
            "Ignoring incompatible game version cache: %s",
            config.GAME_VERSION_JSON_CACHE_PATH,
        )
    return cached_asset_bundle_info, cached_game_version_json


async def _select_changed_bundles(
    current_bundles: Dict[str, Dict],
    cached_bundles: Dict[str, Dict],
    bundle_cache_path_resolver,
) -> list[Dict]:
    changed_bundles = []
    for bundle in current_bundles.values():
        cached_bundle = cached_bundles.get(bundle.get("bundleName", ""), {})
        if bundle_has_changed(bundle, cached_bundle):
            changed_bundles.append(bundle)
            continue
        if not _bundle_has_current_provenance(cached_bundle, bundle):
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


def _build_incremental_download_list(
    config,
    changed_bundles: list[Dict],
    game_version_json: Dict,
    asset_bundle_info: Dict,
    assetver: str | None,
    assetbundle_host_hash: str | None,
    placeholders: set[str],
) -> list[DownloadItem]:
    if assetver:
        app_version = (
            getattr(config, "APP_VERSION_OVERRIDE", None)
            or game_version_json.get("appVersion")
            or ""
        )
        assert app_version, "App version must be set in game version json or config"
        return [
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

    asset_hash = game_version_json.get("assetHash", "")
    url_args = {"assetbundleHostHash": assetbundle_host_hash}
    if "version" in placeholders:
        version = asset_bundle_info.get("version")
        assert version, "Version must be set in asset bundle info"
        url_args["version"] = version
    if asset_hash:
        url_args["assetHash"] = asset_hash
    return [
        (
            format_url_template(
                config.ASSET_BUNDLE_URL,
                **url_args,
                bundleName=bundle.get("bundleName"),
            ),
            bundle,
        )
        for bundle in changed_bundles
    ]


def _build_full_download_list(
    config,
    current_bundles: Dict[str, Dict],
    game_version_json: Dict,
    asset_bundle_info: Dict,
    assetbundle_host_hash: str | None,
    placeholders: set[str],
) -> list[DownloadItem]:
    asset_hash = game_version_json.get("assetHash", "")
    app_version = (
        getattr(config, "APP_VERSION_OVERRIDE", None) or game_version_json.get("appVersion") or ""
    )
    assert app_version, "App version must be set in game version json or config"
    url_args = {
        "assetbundleHostHash": assetbundle_host_hash,
        "appVersion": app_version,
    }
    if "version" in placeholders:
        version = asset_bundle_info.get("version")
        assert version, "Version must be set in asset bundle info"
        url_args["version"] = version
    if asset_hash:
        url_args["assetHash"] = asset_hash
    return [
        (
            format_url_template(
                config.ASSET_BUNDLE_URL,
                **url_args,
                bundleName=bundle.get("bundleName"),
                downloadPath=bundle.get("downloadPath"),
            ),
            bundle,
        )
        for bundle in current_bundles.values()
    ]


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

    assert config, "Config must be provided to get_download_list"
    assert config.ASSET_BUNDLE_INFO_CACHE_PATH, "ASSET_BUNDLE_INFO_CACHE_PATH must be set in config"
    assert config.GAME_VERSION_JSON_CACHE_PATH, "GAME_VERSION_JSON_CACHE_PATH must be set in config"
    cached_asset_bundle_info, cached_game_version_json = _load_cached_metadata(
        config, force_full_download
    )
    provenance_metadata = cached_asset_bundle_info
    if force_full_download:
        # Force-full skips cached checksums, but retaining a valid prior
        # snapshot keeps successful selection history without changing which
        # bundles force-full downloads.
        try:
            provenance_metadata = load_asset_metadata(config.ASSET_BUNDLE_INFO_CACHE_PATH)
        except (StateNotFoundError, StatePersistenceError, StateValidationError):
            provenance_metadata = None

    if assetver is not None:
        game_version_json = dict(game_version_json)
        game_version_json["assetver"] = assetver

    sanitized_selection_info = _sanitize_asset_bundle_info(asset_bundle_info)
    current_bundles: Dict[str, Dict] = sanitized_selection_info.get("bundles", {})
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

    metadata_source = (
        asset_bundle_info if asset_bundle_info_for_cache is None else asset_bundle_info_for_cache
    )
    normalized_metadata = _merge_trusted_provenance(
        _sanitize_asset_bundle_info(metadata_source), provenance_metadata
    )

    provenance_bundles = (provenance_metadata or {}).get("bundles") or {}
    legacy_selected = [
        bundle
        for bundle in current_bundles.values()
        if not isinstance(provenance_bundles.get(bundle.get("bundleName", "")), dict)
        or _PROVENANCE_KEY not in provenance_bundles.get(bundle.get("bundleName", ""), {})
    ]
    if provenance_metadata is not None and legacy_selected:
        logger.warning(
            "Initializing bundle processing provenance for %d selected bundle(s)",
            len(legacy_selected),
        )

    if cached_asset_bundle_info and cached_game_version_json:
        cached_bundles: Dict[str, Dict] = cached_asset_bundle_info.get("bundles") or {}
        changed_bundles = await _select_changed_bundles(
            current_bundles,
            cached_bundles,
            bundle_cache_path_resolver,
        )
        download_list = _build_incremental_download_list(
            config,
            changed_bundles,
            game_version_json,
            asset_bundle_info,
            assetver,
            assetbundle_host_hash,
            asset_bundle_url_placeholders,
        )

    else:
        download_list = _build_full_download_list(
            config,
            current_bundles,
            game_version_json,
            asset_bundle_info,
            assetbundle_host_hash,
            asset_bundle_url_placeholders,
        )

    if download_list:
        download_list = sort_download_list(
            download_list,
            priority_list=priority_list,
        )

    successful_asset_metadata = copy.deepcopy(normalized_metadata)
    successful_bundles = successful_asset_metadata["bundles"]
    for _, candidate in download_list:
        bundle_name = candidate.get("bundleName")
        bundle = successful_bundles.get(bundle_name)
        if not isinstance(bundle, dict):
            continue
        checksum = _processed_checksum(bundle)
        if checksum is not None:
            bundle[_PROVENANCE_KEY] = {"processed_checksum": checksum}

    return DownloadPlan(
        download_list,
        normalized_metadata,
        game_version_json,
        successful_asset_metadata,
    )


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


def sort_download_list(
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
