"""Updater-mode policy: which bundles and post-processors each mode covers."""

from typing import Dict, List, Tuple

SPECIALIZED_MODES = ("live2d", "live2d-associated", "charts")
LIVE2D_BUNDLE_PREFIX = "live2d/"
SPECIALIZED_BUNDLE_PREFIXES = {
    "live2d": (LIVE2D_BUNDLE_PREFIX,),
    "live2d-associated": (LIVE2D_BUNDLE_PREFIX,),
    "charts": (),
}
MODE_BUNDLE_PREFIXES = {
    "assets": (),
    "live2d": (LIVE2D_BUNDLE_PREFIX,),
    "live2d-associated": (LIVE2D_BUNDLE_PREFIX,),
    "charts": (),
}

# Map specialized mode names to their independent config flags.
# ``live2d-associated`` uses ``ENABLE_LIVE2D_ASSOCIATED_PIPELINE`` rather than
# the ``ENABLE_*_POSTPROCESS`` pattern so the deprecated legacy flag and the
# new pipeline flag remain explicitly distinguishable.
SPECIALIZED_MODE_FLAGS = {
    "live2d": "ENABLE_LIVE2D_POSTPROCESS",
    "live2d-associated": "ENABLE_LIVE2D_ASSOCIATED_PIPELINE",
    "charts": "ENABLE_CHARTS_POSTPROCESS",
}


def is_live2d_bundle(bundle: Dict[str, str]) -> bool:
    """Return whether this individual bundle belongs to the Live2D namespace."""
    return (bundle.get("bundleName") or "").startswith(LIVE2D_BUNDLE_PREFIX)


def is_chart_score_bundle(bundle: Dict[str, str]) -> bool:
    """Return whether this individual bundle contains chart score assets."""
    return (bundle.get("bundleName") or "").startswith("music/music_score/")


def mode_uses_bundle_pipeline(mode: str) -> bool:
    """Whether a mode fetches and processes game asset bundles."""
    if mode not in ("assets", "live2d", "live2d-associated", "charts"):
        raise ValueError(f"Unsupported updater mode: {mode}")
    return mode != "charts"


def get_enabled_specialized_modes(mode: str, config) -> tuple[str, ...]:
    """Select specialized processors from mode and independent config flags."""
    if mode in SPECIALIZED_MODES:
        return (mode,)
    if mode != "assets":
        raise ValueError(f"Unsupported updater mode: {mode}")
    return tuple(
        specialized_mode
        for specialized_mode in SPECIALIZED_MODES
        if getattr(config, SPECIALIZED_MODE_FLAGS[specialized_mode], False)
    )


def get_required_bundle_prefixes(mode: str, config) -> tuple[str, ...]:
    """Return prefixes that enabled post-processors must always download."""
    return tuple(
        prefix
        for specialized_mode in get_enabled_specialized_modes(mode, config)
        for prefix in SPECIALIZED_BUNDLE_PREFIXES[specialized_mode]
    )


def needs_shared_workspace(mode: str, config) -> bool:
    """Whether specialized processing needs a run-scoped extracted workspace."""
    return (
        bool(get_enabled_specialized_modes(mode, config))
        and getattr(config, "ASSET_LOCAL_EXTRACTED_DIR", None) is None
    )


def needs_live2d_bundle_cache(mode: str, config) -> bool:
    """Whether Live2D needs a run-scoped bundle cache root."""
    return (
        bool({"live2d", "live2d-associated"} & set(get_enabled_specialized_modes(mode, config)))
        and getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None) is None
    )


def retains_live2d_extracted_outputs(config) -> bool:
    """Whether Live2D extraction must remain available for post-processing."""
    return bool(
        {"live2d", "live2d-associated"}
        & set(get_enabled_specialized_modes(getattr(config, "UPDATER_MODE", "assets"), config))
    )


def get_mode_bundle_prefixes(mode: str) -> tuple[str, ...]:
    try:
        return MODE_BUNDLE_PREFIXES[mode]
    except KeyError as exc:
        raise ValueError(f"Unknown updater mode: {mode}") from exc


def filter_bundles_for_mode(bundles: Dict[str, Dict], mode: str = "assets") -> Dict[str, Dict]:
    prefixes = get_mode_bundle_prefixes(mode)
    return (
        bundles
        if not prefixes
        else {
            key: value
            for key, value in bundles.items()
            if (value.get("bundleName") or "").startswith(prefixes)
        }
    )


def filter_download_items_for_mode(
    items: List[Tuple[str, Dict]], mode: str
) -> List[Tuple[str, Dict]]:
    prefixes = get_mode_bundle_prefixes(mode)
    return (
        items
        if not prefixes
        else [item for item in items if (item[1].get("bundleName") or "").startswith(prefixes)]
    )
