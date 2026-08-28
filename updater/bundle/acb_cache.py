"""Recover referenced ACB text assets from cached Unity bundles."""

import logging
import threading
from collections import OrderedDict
from pathlib import Path, PurePosixPath

from updater.security import (
    SecurityError,
    atomic_write_bytes,
    validate_contained_file,
    validate_output_target,
)
from updater.unity_rs_adapter import load_bundle, read_text_bytes

logger = logging.getLogger("live2d")

# Process-local memory of which cached bundle produced a given ACB text asset.
# Positive results only: the cache grows while a pipeline downloads new
# bundles, so a "not found" answer can become stale within one run. Guarded by
# a lock because extraction may run on a shared thread pool.
_FOUND_BUNDLE_CACHE: OrderedDict[tuple[str, str], str] = OrderedDict()
_FOUND_BUNDLE_CACHE_LIMIT = 256
_FOUND_BUNDLE_CACHE_LOCK = threading.Lock()


def _remember_found_bundle(cache_key: tuple[str, str], bundle_path: Path) -> None:
    with _FOUND_BUNDLE_CACHE_LOCK:
        _FOUND_BUNDLE_CACHE[cache_key] = str(bundle_path)
        _FOUND_BUNDLE_CACHE.move_to_end(cache_key)
        while len(_FOUND_BUNDLE_CACHE) > _FOUND_BUNDLE_CACHE_LIMIT:
            _FOUND_BUNDLE_CACHE.popitem(last=False)


def _extract_from_one_bundle(
    cached_bundle_path: Path,
    expected_textasset_name: str,
    acb_output_path: Path,
    unity_version: str | None,
) -> bool:
    try:
        cached_unity_file = load_bundle(cached_bundle_path, unity_version)
    except Exception:
        return False

    for unityfs_path, unityfs_obj in cached_unity_file.container.items():
        if unityfs_obj.type.name != "TextAsset":
            continue
        if PurePosixPath(unityfs_path).name.lower() != expected_textasset_name:
            continue

        atomic_write_bytes(acb_output_path, read_text_bytes(unityfs_obj))
        return True

    return False


def _candidate_priority(candidate: Path, bundle_path: Path, expected_stem: str):
    """Order candidates: exact bundle-name matches, then closest directories.

    Split-ACB text assets almost always live in a sibling bundle of the one
    referencing them, frequently one named after the asset itself.  Trying
    those first turns the previous whole-cache scan into a handful of loads in
    the common case while still falling back to every cached bundle.
    """
    shared_parts = 0
    for ours, theirs in zip(candidate.parent.parts, bundle_path.parent.parts, strict=False):
        if ours != theirs:
            break
        shared_parts += 1
    name_matches = candidate.name.lower() == expected_stem
    return (not name_matches, -shared_parts)


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
    expected_stem = expected_textasset_name.removesuffix(".bytes").removesuffix(".acb")
    cache_key = (str(bundle_cache_root), expected_textasset_name)

    with _FOUND_BUNDLE_CACHE_LOCK:
        remembered = _FOUND_BUNDLE_CACHE.get(cache_key)
    if remembered is not None:
        remembered_path = Path(remembered)
        if remembered_path.is_file() and _extract_from_one_bundle(
            remembered_path, expected_textasset_name, acb_output_path, unity_version
        ):
            logger.debug(
                "Extracted %s from remembered cached bundle %s",
                acb_textasset_filename,
                remembered_path,
            )
            return True
        with _FOUND_BUNDLE_CACHE_LOCK:
            _FOUND_BUNDLE_CACHE.pop(cache_key, None)

    candidates = []
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
        candidates.append(cached_bundle_path)

    candidates.sort(key=lambda path: _candidate_priority(path, bundle_path, expected_stem))

    for cached_bundle_path in candidates:
        if _extract_from_one_bundle(
            cached_bundle_path, expected_textasset_name, acb_output_path, unity_version
        ):
            _remember_found_bundle(cache_key, cached_bundle_path)
            logger.debug(
                "Extracted %s from cached bundle %s to %s",
                acb_textasset_filename,
                cached_bundle_path.relative_to(bundle_cache_root),
                acb_output_path,
            )
            return True

    return False
