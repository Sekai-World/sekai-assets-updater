"""Incremental chart and Live2D state: fingerprints, hashes, validation."""

import hashlib
import logging
from pathlib import Path as StdPath

import orjson as json

from updater.postprocess.charts import collect_score_files
from updater.postprocess.config import (
    _region_name,
    _resolve_chart_jacket_base_url,
    get_chart_data_server,
)

logger = logging.getLogger("asset_updater")


_CHART_STATE_SCHEMA_VERSION = 1


def chart_state_path(config) -> StdPath:
    """Return the filesystem path for persisted chart incremental state."""
    return StdPath(config.DL_LIST_CACHE_PATH).parent / "chart_state.json"


def chart_fingerprint(config) -> dict[str, str]:
    """Build a fingerprint dict from the current chart configuration."""
    region = _region_name(config)
    return {
        "region": region,
        "data_server": get_chart_data_server(config),
        "jacket_base_url": _resolve_chart_jacket_base_url(config, region),
    }


def hash_score_file(path: StdPath) -> str:
    """Return the SHA-256 hex digest of the file at *path*."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_score_hashes(
    extracted_dir: StdPath, include_list: list[str] | None = None
) -> dict[str, str]:
    """Return ``{relative_posix_path: sha256_hex}`` for every score file."""
    score_root = extracted_dir / "music" / "music_score"
    result: dict[str, str] = {}
    for path in collect_score_files(extracted_dir, include_list):
        rel = path.relative_to(score_root).as_posix()
        result[rel] = hash_score_file(path)
    return result


def validate_chart_state(value: object) -> dict:
    """Strict validator for ``chart_state.json`` compatible with ``atomic_write_json``."""
    if not isinstance(value, dict):
        raise ValueError("chart state must be an object")
    allowed_top = {"schema_version", "fingerprint", "scores"}
    unknown = set(value) - allowed_top
    if unknown:
        raise ValueError(f"chart state contains unknown fields: {sorted(unknown)}")
    if value.get("schema_version") != _CHART_STATE_SCHEMA_VERSION:
        raise ValueError(
            f"chart state schema_version must be {_CHART_STATE_SCHEMA_VERSION}, "
            f"got {value.get('schema_version')!r}"
        )
    fp = value.get("fingerprint")
    if not isinstance(fp, dict):
        raise ValueError("chart state fingerprint must be an object")
    for field in ("region", "data_server", "jacket_base_url"):
        if not isinstance(fp.get(field), str):
            raise ValueError(f"chart state fingerprint.{field} must be a string")
    scores = value.get("scores")
    if not isinstance(scores, dict):
        raise ValueError("chart state scores must be an object")
    for key, val in scores.items():
        if not isinstance(key, str):
            raise ValueError("chart state score keys must be strings")
        if not isinstance(val, str) or len(val) != 64 or val != val.lower():
            raise ValueError(
                f"chart state score hash for {key!r} must be a 64-char lowercase hex string"
            )
    return {
        "schema_version": _CHART_STATE_SCHEMA_VERSION,
        "fingerprint": {k: fp[k] for k in ("region", "data_server", "jacket_base_url")},
        "scores": dict(scores),
    }


def load_chart_state(path: StdPath) -> dict | None:
    """Load chart state from *path*, returning ``None`` when absent or corrupt."""
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        return validate_chart_state(json.loads(raw))
    except Exception:
        logger.warning("Corrupt chart state at %s; falling back to full rebuild", path)
        return None


def pending_score_paths(current: dict[str, str], stored: dict[str, str]) -> list[str]:
    """Sorted relative paths that are new or whose content hash changed."""
    return sorted(path for path, digest in current.items() if digest != stored.get(path))


_LIVE2D_STATE_SCHEMA_VERSION = 1


def live2d_state_path(config) -> StdPath:
    """Return the filesystem path for persisted Live2D motion incremental state."""
    return StdPath(config.DL_LIST_CACHE_PATH).parent / "live2d_motion_state.json"


def live2d_associated_state_path(config) -> StdPath:
    """Return the independent Live2D-associated rollout state path."""
    return StdPath(config.DL_LIST_CACHE_PATH).parent / "live2d_associated_state.json"


def compute_motion_bundle_hashes(motion_source: StdPath) -> dict[str, str]:
    """Return ``{file_name: sha256_hex}`` over every file in *motion_source*."""
    result: dict[str, str] = {}
    for path in sorted(p for p in motion_source.glob("*") if p.is_file()):
        result[path.name] = hash_score_file(path)
    return result


def compute_live2d_fingerprint(config, model_dir: StdPath) -> dict[str, str]:
    """Build a fingerprint dict from the current Live2D configuration.

    The fingerprint includes the Unity version and a composite hash over all
    ``*.moc3`` files under *model_dir* so that any moc3 content change
    invalidates the cached state.
    """
    # Build a deterministic composite hash of all moc3 files
    moc3_parts: list[str] = []
    for moc3_path in sorted(model_dir.rglob("*.moc3")):
        rel = moc3_path.relative_to(model_dir).as_posix()
        h = hash_score_file(moc3_path)
        moc3_parts.append(f"{rel}:{h}")
    model_hash = hashlib.sha256("\n".join(moc3_parts).encode()).hexdigest()
    return {
        "unity_version": getattr(config, "UNITY_VERSION", "") or "",
        "model_hash": model_hash,
    }


def validate_live2d_state(value: object) -> dict:
    """Strict validator for ``live2d_motion_state.json`` compatible with ``atomic_write_json``."""
    if not isinstance(value, dict):
        raise ValueError("live2d motion state must be an object")
    allowed_top = {"schema_version", "fingerprint", "motions"}
    unknown = set(value) - allowed_top
    if unknown:
        raise ValueError(f"live2d motion state contains unknown fields: {sorted(unknown)}")
    if value.get("schema_version") != _LIVE2D_STATE_SCHEMA_VERSION:
        raise ValueError(
            f"live2d motion state schema_version must be {_LIVE2D_STATE_SCHEMA_VERSION}, "
            f"got {value.get('schema_version')!r}"
        )
    fp = value.get("fingerprint")
    if not isinstance(fp, dict):
        raise ValueError("live2d motion state fingerprint must be an object")
    for field in ("unity_version", "model_hash"):
        if not isinstance(fp.get(field), str):
            raise ValueError(f"live2d motion state fingerprint.{field} must be a string")
    motions = value.get("motions")
    if not isinstance(motions, dict):
        raise ValueError("live2d motion state motions must be an object")
    for key, val in motions.items():
        if not isinstance(key, str):
            raise ValueError("live2d motion state motion keys must be strings")
        if not isinstance(val, str) or len(val) != 64 or val != val.lower():
            raise ValueError(
                f"live2d motion state hash for {key!r} must be a 64-char lowercase hex string"
            )
    return {
        "schema_version": _LIVE2D_STATE_SCHEMA_VERSION,
        "fingerprint": {k: fp[k] for k in ("unity_version", "model_hash")},
        "motions": dict(motions),
    }


def load_live2d_state(path: StdPath) -> dict | None:
    """Load Live2D motion state from *path*, returning ``None`` when absent or corrupt."""
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        return validate_live2d_state(json.loads(raw))
    except Exception:
        logger.warning("Corrupt Live2D motion state at %s; falling back to full rebuild", path)
        return None


def pending_motion_bundles(current: dict[str, str], stored: dict[str, str]) -> list[str]:
    """Sorted file names that are new or whose content hash changed."""
    return sorted(name for name, digest in current.items() if digest != stored.get(name))
