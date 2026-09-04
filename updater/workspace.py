"""Bundle cache and run-workspace path policy."""

import hashlib
import os
from typing import Any, Dict

from anyio import Path

from updater.modes import get_enabled_specialized_modes, is_chart_score_bundle, is_live2d_bundle


async def ensure_dir_exists(dir_path: Path):
    """Ensure the directory exists, create it if not."""
    if not await dir_path.exists():
        await dir_path.mkdir(parents=True, exist_ok=True)

    if not await dir_path.is_dir():
        raise NotADirectoryError(
            f"Failed to create directory {dir_path}, path exists but is not a directory"
        )


def get_bundle_cache_root(config, bundle: Dict[str, Any]):
    """Select the cache root without allowing specialized roots to leak."""
    if is_live2d_bundle(bundle):
        return getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None)
    return getattr(config, "ASSET_LOCAL_BUNDLE_CACHE_DIR", None)


def get_bundle_cache_path(config, bundle: Dict[str, Any]):
    root = get_bundle_cache_root(config, bundle)
    bundle_name = bundle.get("bundleName") or ""
    if root is None or not bundle_name:
        return None
    return root / bundle_name


def uses_aggregate_workspace(bundle: Dict[str, Any], config) -> bool:
    """Route only specialized bundle outputs into the run workspace."""
    enabled_modes = get_enabled_specialized_modes(getattr(config, "UPDATER_MODE", "assets"), config)
    return (
        bool({"live2d", "live2d-associated"} & set(enabled_modes)) and is_live2d_bundle(bundle)
    ) or ("charts" in enabled_modes and is_chart_score_bundle(bundle))


def configured_path(value) -> Path | None:
    """Normalize configured filesystem paths from either pathlib or anyio."""
    if isinstance(value, (str, os.PathLike)):
        return Path(os.fspath(value))
    return None


def bundle_staging_identity(bundle_name: Any) -> str:
    """Return a deterministic, filesystem-safe identity for a bundle name."""

    if not isinstance(bundle_name, str) or not bundle_name:
        raise ValueError("bundleName must be a non-empty string")
    digest = hashlib.sha256(bundle_name.encode("utf-8", "surrogatepass")).hexdigest()
    return f"bundle-{digest[:24]}"
