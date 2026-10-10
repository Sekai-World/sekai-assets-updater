"""Config accessors for post-processing storage, regions, and jackets."""

from pathlib import Path

from updater.modes import SPECIALIZED_MODES

DEFAULT_CHART_JACKET_BASE_URL = "https://storage.sekai.best/sekai-{region}-assets/music/jacket"
# Searched (recursively) for chart fonts when neither CHART_FONT_PATHS nor
# CHART_FONT_DIRS is configured.
DEFAULT_CHART_FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    "C:\\Windows\\Fonts",
)


def get_specialized_storage(config, mode: str) -> list[dict]:
    """Return asset storage targets configured for a specialized mode."""
    if mode not in SPECIALIZED_MODES:
        raise ValueError(f"Unsupported specialized mode: {mode}")
    return [
        storage
        for storage in (getattr(config, "ASSET_REMOTE_STORAGE", None) or [])
        if storage.get("type") == mode
    ]


def get_normal_storage_candidates(config) -> list[dict]:
    """Return extracted-asset mirrors usable as chart source fallbacks."""
    return [
        storage
        for storage in (getattr(config, "ASSET_REMOTE_STORAGE", None) or [])
        if storage.get("type") == "normal"
    ]


def _region_name(config) -> str:
    region = getattr(config, "REGION", None)
    return getattr(region, "name", str(getattr(region, "value", region))).lower()


def get_chart_data_server(config) -> str:
    """Return the master-data server used while rendering charts.

    This is normally the asset region, but can differ when a region's master
    data is published under a distinct server name (for example, TC vs. TW).
    """
    return getattr(config, "CHART_DATA_SERVER", None) or _region_name(config)


def get_chart_jacket_url(config, region: str, music_id: int) -> str:
    """Build a chart jacket URL from the configured or legacy base URL."""
    jacket_base_url = _resolve_chart_jacket_base_url(config, region)
    padded_id = str(music_id).zfill(3)
    jacket_name = f"jacket_s_{padded_id}.png"
    return f"{jacket_base_url.rstrip('/')}/jacket_s_{padded_id}/{jacket_name}"


def _resolve_chart_jacket_base_url(config, region: str) -> str:
    """Return the effective jacket base URL for *region*."""
    jacket_base_url = getattr(config, "CHART_JACKET_BASE_URL", None)
    if not jacket_base_url:
        jacket_base_url = DEFAULT_CHART_JACKET_BASE_URL.format(region=region)
    return jacket_base_url


def get_chart_font_kwargs(config) -> dict[str, list[str]]:
    """Return the ``font_paths`` / ``font_dirs`` arguments for chart PNG rendering.

    pjsekai-scores-rs 0.6+ only draws text with the fonts it is given, so when
    nothing is configured the existing system font directories are passed.
    """
    font_paths = [str(path) for path in getattr(config, "CHART_FONT_PATHS", None) or []]
    font_dirs = [str(path) for path in getattr(config, "CHART_FONT_DIRS", None) or []]
    if not font_paths and not font_dirs:
        font_dirs = [path for path in DEFAULT_CHART_FONT_DIRS if Path(path).is_dir()]
    kwargs: dict[str, list[str]] = {}
    if font_paths:
        kwargs["font_paths"] = font_paths
    if font_dirs:
        kwargs["font_dirs"] = font_dirs
    return kwargs
