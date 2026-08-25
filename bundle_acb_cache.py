"""Recover referenced ACB text assets from cached Unity bundles."""

import logging
from pathlib import Path, PurePosixPath

import UnityPy
import UnityPy.classes
import UnityPy.config

from security import (
    SecurityError,
    atomic_write_bytes,
    validate_contained_file,
    validate_output_target,
)

logger = logging.getLogger("live2d")


def extract_acb_from_cached_bundles(
    bundle_save_path: Path,
    acb_textasset_filename: str,
    acb_output_path: Path,
    unity_version: str | None,
    bundle_cache_root: Path | None = None,
) -> bool:
    if bundle_cache_root is None:
        return False

    bundle_cache_root = bundle_cache_root.resolve(strict=False)
    if not bundle_cache_root.is_dir():
        return False
    bundle_path = bundle_save_path.resolve(strict=False)
    output_root = acb_output_path.parent.resolve(strict=False)
    validate_output_target(output_root, acb_output_path)

    expected_textasset_name = acb_textasset_filename.lower()
    for cached_bundle_path in bundle_cache_root.rglob("*"):
        if cached_bundle_path.is_dir():
            continue
        try:
            cached_bundle_path.resolve().relative_to(output_root)
        except ValueError:
            pass
        else:
            logger.debug("Skipping artifact output while scanning cache: %s", cached_bundle_path)
            continue
        try:
            cached_bundle_path = validate_contained_file(
                bundle_cache_root,
                cached_bundle_path.relative_to(bundle_cache_root).as_posix(),
            )
        except (OSError, ValueError, SecurityError):
            logger.warning("Ignoring unsafe cached bundle path %s", cached_bundle_path)
            continue
        if cached_bundle_path.resolve() == bundle_path:
            continue

        try:
            UnityPy.config.FALLBACK_UNITY_VERSION = unity_version
            cached_unity_file = UnityPy.load(cached_bundle_path.as_posix())
            if not cached_unity_file:
                continue
        except Exception:
            continue

        for unityfs_path, unityfs_obj in cached_unity_file.container.items():
            if unityfs_obj.type.name != "TextAsset":
                continue
            if PurePosixPath(unityfs_path).name.lower() != expected_textasset_name:
                continue

            data = unityfs_obj.read()
            if not isinstance(data, UnityPy.classes.TextAsset):
                continue

            atomic_write_bytes(
                acb_output_path,
                data.m_Script.encode("utf-8", "surrogateescape"),
            )
            logger.debug(
                "Extracted %s from cached bundle %s to %s",
                acb_textasset_filename,
                cached_bundle_path.relative_to(bundle_cache_root),
                acb_output_path,
            )
            return True

    return False
