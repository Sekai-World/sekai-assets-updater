"""Transactional rollout support for the additive Live2D association index.

The legacy Live2D publisher owns ``live2d/model_list.json`` and the files below
``live2d/``.  This module deliberately has a different ownership boundary:
``live2d-associated/v1`` contains candidate generations and an atomic
``current.json`` pointer document.  A candidate is complete before the pointer
can reference it, and the pointer is never changed while index or output
validation is in progress.

There is intentionally no master-data loader here.  Callers must provide a
``Live2DIndex`` (or its already-sanitized mapping form) and the output tree it
references.  In particular, an empty index is not a valid rollout input.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

from updater import state
from updater.live2d.contracts import (
    CandidateStatus,
    Live2DIndex,
    canonical_json_bytes,
    to_json_dict,
    validate_index,
)
from updater.live2d.keys import Live2DKeys, changed_keys, compute_keys
from updater.live2d.publication import validate_live2d_outputs

LIVE2D_ASSOCIATED_NAMESPACE = "live2d-associated/v1"
LIVE2D_ASSOCIATED_NAMESPACE_PARTS = ("live2d-associated", "v1")
LIVE2D_ASSOCIATED_STATE_FILENAME = "live2d_associated_state.json"
ROLLOUT_SCHEMA_VERSION = 1
LIVE2D_ASSOCIATED_CANDIDATES_DIRECTORY = "candidates"
LIVE2D_ASSOCIATED_INDEX_FILENAME = "index.json"
LIVE2D_ASSOCIATED_CURRENT_FILENAME = "current.json"
LIVE2D_ASSOCIATED_CURRENT_POINTER = LIVE2D_ASSOCIATED_CURRENT_FILENAME

_CANDIDATES_DIRECTORY = "candidates"
_INDEX_FILENAME = "index.json"
_CURRENT_FILENAME = "current.json"
_CANDIDATE_FILENAME = "candidate.json"
_CURRENT_POINTER = _CURRENT_FILENAME
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

PathInput: TypeAlias = str | os.PathLike[str]
IndexInput: TypeAlias = Live2DIndex | Mapping[str, object]

__all__ = [
    "CandidatePointer",
    "Candidate",
    "CurrentPointer",
    "Current",
    "IndexInput",
    "LIVE2D_ASSOCIATED_NAMESPACE",
    "LIVE2D_ASSOCIATED_NAMESPACE_PARTS",
    "LIVE2D_ASSOCIATED_STATE_FILENAME",
    "LIVE2D_ASSOCIATED_CANDIDATES_DIRECTORY",
    "LIVE2D_ASSOCIATED_CURRENT_POINTER",
    "LIVE2D_ASSOCIATED_INDEX_FILENAME",
    "LIVE2D_ASSOCIATED_CURRENT_FILENAME",
    "Live2DAssociatedRolloutError",
    "Live2DRolloutError",
    "Live2DKeys",
    "RolloutComparison",
    "RolloutState",
    "ROLLOUT_SCHEMA_VERSION",
    "associated_namespace_path",
    "associated_state_path",
    "canonical_index_bytes",
    "canonical_index_json_bytes",
    "canonical_indices_equal",
    "candidate_id_for_index",
    "candidate_directory",
    "candidate_path",
    "compare_canonical_json_and_keys",
    "compare_index_keys",
    "compare_live2d_indices",
    "compare_indexes",
    "canonical_json_equal",
    "compare_keys",
    "compute_rollout_keys",
    "disable_live2d_associated",
    "disable_rollout",
    "index_checksum",
    "index_path",
    "live2d_associated_namespace_path",
    "live2d_associated_state_path",
    "live2d_associated_state_path_from_cache",
    "load_current_index",
    "load_current_pointer",
    "load_live2d_index",
    "load_rollout_state",
    "load_state",
    "mark_uploaded_storages",
    "namespace_path",
    "publish_candidate",
    "publish_index",
    "publish_live2d_associated_index",
    "record_uploaded_storages",
    "read_current",
    "rollback",
    "rollback_current",
    "rollback_live2d_associated",
    "storage_receipt_key",
    "validate_candidate_pointer",
    "validate_current_pointer",
    "validate_publishable_index",
    "validate_rollout_state",
    "current_metadata_path",
    "current_pointer_path",
    "disable_association",
    "disable",
    "get_live2d_associated_namespace",
    "publish_associated_index",
    "rollback_candidate",
]


class Live2DAssociatedRolloutError(ValueError):
    """Raised when an associated-index candidate cannot be safely rolled out."""


def _as_path(value: PathInput, field_name: str) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(value)))
    except (TypeError, ValueError) as exc:
        raise Live2DAssociatedRolloutError(f"{field_name}: expected a filesystem path") from exc


def live2d_associated_namespace_path(root: PathInput) -> Path:
    """Return the versioned associated-output namespace below ``root``."""

    return (
        Path(os.fspath(root))
        / LIVE2D_ASSOCIATED_NAMESPACE_PARTS[0]
        / LIVE2D_ASSOCIATED_NAMESPACE_PARTS[1]
    )


def associated_namespace_path(root: PathInput) -> Path:
    """Short alias for :func:`live2d_associated_namespace_path`."""

    return live2d_associated_namespace_path(root)


def live2d_associated_state_path(config_or_root: object) -> Path:
    """Return the independent rollout state path.

    A config object uses the directory beside ``DL_LIST_CACHE_PATH``.  A
    path-like argument is always an explicit directory root.  Cache-file
    callers must pass a config object or use
    :func:`live2d_associated_state_path_from_cache`; suffix heuristics are
    deliberately avoided because valid directory names may contain dots.
    """

    configured_cache = getattr(config_or_root, "DL_LIST_CACHE_PATH", None)
    if configured_cache is not None:
        return _as_path(configured_cache, "DL_LIST_CACHE_PATH").parent / (
            LIVE2D_ASSOCIATED_STATE_FILENAME
        )
    if hasattr(config_or_root, "DL_LIST_CACHE_PATH"):
        raise Live2DAssociatedRolloutError(
            "DL_LIST_CACHE_PATH must be configured for associated rollout state"
        )
    return _as_path(config_or_root, "state_root") / LIVE2D_ASSOCIATED_STATE_FILENAME


def live2d_associated_state_path_from_cache(cache_path: PathInput) -> Path:
    """Return associated state beside an explicitly supplied cache file."""

    return _as_path(cache_path, "DL_LIST_CACHE_PATH").parent / LIVE2D_ASSOCIATED_STATE_FILENAME


def associated_state_path(config_or_root: object) -> Path:
    """Short alias for :func:`live2d_associated_state_path`."""

    return live2d_associated_state_path(config_or_root)


def namespace_path(root: PathInput) -> Path:
    """Generic alias for the versioned associated namespace helper."""

    return live2d_associated_namespace_path(root)


def get_live2d_associated_namespace(root: PathInput) -> Path:
    """Descriptive alias for :func:`live2d_associated_namespace_path`."""

    return live2d_associated_namespace_path(root)


def candidate_directory(namespace_root: PathInput, candidate_id: str) -> Path:
    """Return one candidate directory below a namespace."""

    _namespace, candidates, _link, _index = _namespace_paths(namespace_root)
    return _candidate_path(candidates, candidate_id)


def current_pointer_path(namespace_root: PathInput) -> Path:
    """Return the atomically replaced ``current.json`` path for a namespace."""

    namespace, _candidates, link, _index = _namespace_paths(namespace_root)
    return namespace / link.name


def index_path(namespace_root: PathInput) -> Path:
    """Return the namespace-level ``index.json`` path."""

    _namespace, _candidates, _link, index = _namespace_paths(namespace_root)
    return index


def current_metadata_path(namespace_root: PathInput) -> Path:
    """Return the namespace-level ``current.json`` metadata path."""

    namespace, _candidates, _link, _index = _namespace_paths(namespace_root)
    return namespace / _CURRENT_FILENAME


def canonical_index_bytes(index: IndexInput) -> bytes:
    """Return the compact canonical contract bytes used for keys and equality."""

    return canonical_json_bytes(validate_index(index))


def canonical_index_json_bytes(index: IndexInput) -> bytes:
    """Return deterministic, human-readable bytes for ``index.json``.

    The contract's compact :func:`canonical_json_bytes` remains the identity
    representation for key computation.  The on-disk index uses the same
    sorted object/array content with two-space indentation and a final newline.
    """

    validated = validate_index(index)
    return (
        json.dumps(
            to_json_dict(validated),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def index_checksum(index: IndexInput) -> str:
    """Return the SHA-256 checksum of the persisted canonical ``index.json``."""

    return hashlib.sha256(canonical_index_json_bytes(index)).hexdigest()


def compute_rollout_keys(index: IndexInput) -> Live2DKeys:
    """Compute the existing model, motion-set, and index keys for an index."""

    return compute_keys(validate_index(index))


@dataclass(frozen=True, slots=True)
class RolloutComparison:
    """Canonical and incremental comparison results for two valid indexes."""

    canonical_equal: bool
    keys_equal: bool
    changed_keys: tuple[str, ...]
    current_index_key: str
    candidate_index_key: str

    @property
    def unchanged(self) -> bool:
        return self.canonical_equal and self.keys_equal

    def __bool__(self) -> bool:
        return self.unchanged


def compare_live2d_indices(current: IndexInput, candidate: IndexInput) -> RolloutComparison:
    """Compare canonical JSON and the existing Live2D key snapshots."""

    current_index = validate_index(current)
    candidate_index = validate_index(candidate)
    current_keys = compute_keys(current_index)
    candidate_keys = compute_keys(candidate_index)
    return RolloutComparison(
        canonical_equal=canonical_index_bytes(current_index)
        == canonical_index_bytes(candidate_index),
        keys_equal=current_keys == candidate_keys,
        changed_keys=changed_keys(candidate_keys, current_keys),
        current_index_key=current_keys.index_key,
        candidate_index_key=candidate_keys.index_key,
    )


def compare_canonical_json_and_keys(
    current: IndexInput, candidate: IndexInput
) -> RolloutComparison:
    """Explicitly named alias for :func:`compare_live2d_indices`."""

    return compare_live2d_indices(current, candidate)


def canonical_indices_equal(current: IndexInput, candidate: IndexInput) -> bool:
    """Return whether two valid indexes have identical canonical contract bytes."""

    return canonical_index_bytes(current) == canonical_index_bytes(candidate)


def compare_index_keys(
    current: IndexInput | Live2DKeys,
    previous: IndexInput | Live2DKeys,
) -> tuple[str, ...]:
    """Return the existing ``keys.changed_keys`` result for two snapshots."""

    return changed_keys(current, previous)


def _validate_sha(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise Live2DAssociatedRolloutError(f"{field_name} must be a lowercase SHA-256 hex string")
    return value


def _validate_key_mapping(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise Live2DAssociatedRolloutError(f"{field_name} must be an object")
    result: dict[str, str] = {}
    for key, digest in value.items():
        if not isinstance(key, str) or not key:
            raise Live2DAssociatedRolloutError(f"{field_name} keys must be non-empty strings")
        result[key] = _validate_sha(digest, f"{field_name}[{key!r}]")
    return dict(sorted(result.items()))


def _validate_storage_receipts(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise Live2DAssociatedRolloutError(f"{field_name} must be an object")
    result: dict[str, str] = {}
    for storage_key, candidate_id in value.items():
        _validate_sha(storage_key, f"{field_name} key")
        if not isinstance(candidate_id, str) or not _ID_RE.fullmatch(candidate_id):
            raise Live2DAssociatedRolloutError(
                f"{field_name}[{storage_key!r}] must contain a safe candidate id"
            )
        result[storage_key] = candidate_id
    return dict(sorted(result.items()))


def _validate_relative_file_key(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise Live2DAssociatedRolloutError(f"{field_name} must be a non-empty relative path")
    if value.startswith(("/", "\\", "~")) or "\\" in value or ":" in value:
        raise Live2DAssociatedRolloutError(f"{field_name} is not a safe relative path: {value!r}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise Live2DAssociatedRolloutError(f"{field_name} is not a safe relative path: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class CandidatePointer:
    """Integrity metadata stored beside one complete candidate index."""

    candidate_id: str
    index_key: str
    index_sha256: str
    canonical_index_sha256: str
    model_keys: Mapping[str, str]
    motion_set_keys: Mapping[str, str]
    output_checksums: Mapping[str, str]
    namespace: str = LIVE2D_ASSOCIATED_NAMESPACE
    schema_version: int = ROLLOUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != ROLLOUT_SCHEMA_VERSION:
            raise Live2DAssociatedRolloutError(f"schema_version must be {ROLLOUT_SCHEMA_VERSION}")
        if self.namespace != LIVE2D_ASSOCIATED_NAMESPACE:
            raise Live2DAssociatedRolloutError(f"namespace must be {LIVE2D_ASSOCIATED_NAMESPACE!r}")
        if not isinstance(self.candidate_id, str) or not _ID_RE.fullmatch(self.candidate_id):
            raise Live2DAssociatedRolloutError("candidate_id must be a safe non-empty identifier")
        _validate_sha(self.index_key, "index_key")
        _validate_sha(self.index_sha256, "index_sha256")
        _validate_sha(self.canonical_index_sha256, "canonical_index_sha256")
        object.__setattr__(self, "model_keys", _validate_key_mapping(self.model_keys, "model_keys"))
        object.__setattr__(
            self,
            "motion_set_keys",
            _validate_key_mapping(self.motion_set_keys, "motion_set_keys"),
        )
        if not isinstance(self.output_checksums, Mapping):
            raise Live2DAssociatedRolloutError("output_checksums must be an object")
        output_checksums: dict[str, str] = {}
        for path, digest in self.output_checksums.items():
            safe_path = _validate_relative_file_key(path, "output_checksums path")
            output_checksums[safe_path] = _validate_sha(digest, f"output_checksums[{safe_path!r}]")
        object.__setattr__(self, "output_checksums", dict(sorted(output_checksums.items())))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "candidate_id": self.candidate_id,
            "index_key": self.index_key,
            "index_sha256": self.index_sha256,
            "canonical_index_sha256": self.canonical_index_sha256,
            "model_keys": dict(self.model_keys),
            "motion_set_keys": dict(self.motion_set_keys),
            "output_checksums": dict(self.output_checksums),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CandidatePointer:
        if not isinstance(value, Mapping):
            raise Live2DAssociatedRolloutError("candidate pointer must be an object")
        allowed = {
            "schema_version",
            "namespace",
            "candidate_id",
            "index_key",
            "index_sha256",
            "canonical_index_sha256",
            "model_keys",
            "motion_set_keys",
            "output_checksums",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise Live2DAssociatedRolloutError(
                f"candidate pointer contains unknown fields: {unknown}"
            )
        return cls(
            candidate_id=value.get("candidate_id"),  # type: ignore[arg-type]
            index_key=value.get("index_key"),  # type: ignore[arg-type]
            index_sha256=value.get("index_sha256"),  # type: ignore[arg-type]
            canonical_index_sha256=value.get("canonical_index_sha256"),  # type: ignore[arg-type]
            model_keys=value.get("model_keys", {}),  # type: ignore[arg-type]
            motion_set_keys=value.get("motion_set_keys", {}),  # type: ignore[arg-type]
            output_checksums=value.get("output_checksums", {}),  # type: ignore[arg-type]
            namespace=value.get("namespace", LIVE2D_ASSOCIATED_NAMESPACE),  # type: ignore[arg-type]
            schema_version=value.get("schema_version", ROLLOUT_SCHEMA_VERSION),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CurrentPointer(CandidatePointer):
    """The candidate metadata stored in the atomic namespace ``current.json``."""


@dataclass(frozen=True, slots=True)
class RolloutState:
    """Independent durable state for associated-index rollout acceptance."""

    enabled: bool
    current: CurrentPointer | None
    namespace: str = LIVE2D_ASSOCIATED_NAMESPACE
    schema_version: int = ROLLOUT_SCHEMA_VERSION
    uploaded_storages: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise Live2DAssociatedRolloutError("rollout state enabled must be a bool")
        if type(self.schema_version) is not int or self.schema_version != ROLLOUT_SCHEMA_VERSION:
            raise Live2DAssociatedRolloutError(
                f"rollout state schema_version must be {ROLLOUT_SCHEMA_VERSION}"
            )
        if self.namespace != LIVE2D_ASSOCIATED_NAMESPACE:
            raise Live2DAssociatedRolloutError(
                f"rollout state namespace must be {LIVE2D_ASSOCIATED_NAMESPACE!r}"
            )
        if self.current is not None and not isinstance(self.current, CurrentPointer):
            raise Live2DAssociatedRolloutError("rollout state current must be a current pointer")
        if self.current is None and self.uploaded_storages:
            raise Live2DAssociatedRolloutError(
                "rollout state without a current pointer must not retain upload receipts"
            )
        object.__setattr__(
            self,
            "uploaded_storages",
            _validate_storage_receipts(self.uploaded_storages, "uploaded_storages"),
        )

    def to_dict(self) -> dict[str, object]:
        current = self.current.to_dict() if self.current is not None else {}
        return {
            "schema_version": self.schema_version,
            "namespace": self.namespace,
            "enabled": self.enabled,
            "current": self.current.to_dict() if self.current is not None else None,
            "candidate_id": current.get("candidate_id"),
            "index_key": current.get("index_key"),
            "index_sha256": current.get("index_sha256"),
            "canonical_index_sha256": current.get("canonical_index_sha256"),
            "model_keys": current.get("model_keys", {}),
            "motion_set_keys": current.get("motion_set_keys", {}),
            "output_checksums": current.get("output_checksums", {}),
            "uploaded_storages": dict(self.uploaded_storages),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RolloutState:
        if not isinstance(value, Mapping):
            raise Live2DAssociatedRolloutError("rollout state must be an object")
        allowed = {
            "schema_version",
            "namespace",
            "enabled",
            "current",
            "candidate_id",
            "index_key",
            "index_sha256",
            "canonical_index_sha256",
            "model_keys",
            "motion_set_keys",
            "output_checksums",
            "uploaded_storages",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise Live2DAssociatedRolloutError(f"rollout state contains unknown fields: {unknown}")
        raw_current = value.get("current")
        if raw_current is None:
            current = None
        elif isinstance(raw_current, Mapping):
            current = CurrentPointer.from_dict(raw_current)
        else:
            raise Live2DAssociatedRolloutError("rollout state current must be an object or null")
        enabled = value.get("enabled")
        uploaded_storages = _validate_storage_receipts(
            value.get("uploaded_storages", {}), "rollout state uploaded_storages"
        )
        if type(enabled) is not bool:
            raise Live2DAssociatedRolloutError("rollout state enabled must be a bool")
        if not enabled and current is not None:
            raise Live2DAssociatedRolloutError(
                "disabled rollout state must not retain a current pointer"
            )
        if current is None and uploaded_storages:
            raise Live2DAssociatedRolloutError(
                "rollout state without a current pointer must not retain upload receipts"
            )
        if any(
            field in value
            for field in allowed - {"schema_version", "namespace", "enabled", "current"}
        ):
            expected = RolloutState(
                enabled=enabled,
                current=current,
                namespace=value.get("namespace", LIVE2D_ASSOCIATED_NAMESPACE),  # type: ignore[arg-type]
                schema_version=value.get("schema_version", ROLLOUT_SCHEMA_VERSION),  # type: ignore[arg-type]
                uploaded_storages=uploaded_storages,
            ).to_dict()
            for field in allowed - {"schema_version", "namespace", "enabled", "current"}:
                if field in value and value[field] != expected[field]:
                    raise Live2DAssociatedRolloutError(
                        f"rollout state {field} does not match current pointer"
                    )
        return cls(
            enabled=value.get("enabled"),  # type: ignore[arg-type]
            current=current,
            namespace=value.get("namespace", LIVE2D_ASSOCIATED_NAMESPACE),  # type: ignore[arg-type]
            schema_version=value.get("schema_version", ROLLOUT_SCHEMA_VERSION),  # type: ignore[arg-type]
            uploaded_storages=uploaded_storages,
        )


def validate_candidate_pointer(value: object) -> dict[str, object]:
    """Validate and normalize a candidate/current metadata mapping."""

    if isinstance(value, CandidatePointer):
        pointer = value
    elif isinstance(value, Mapping):
        pointer = CandidatePointer.from_dict(value)
    else:
        raise Live2DAssociatedRolloutError("candidate pointer must be an object")
    return pointer.to_dict()


def validate_current_pointer(value: object) -> dict[str, object]:
    """Validate a current pointer document using the same strict structure."""

    if isinstance(value, CurrentPointer):
        pointer = value
    elif isinstance(value, Mapping):
        pointer = CurrentPointer.from_dict(value)
    else:
        raise Live2DAssociatedRolloutError("current pointer must be an object")
    return pointer.to_dict()


def validate_rollout_state(value: object) -> dict[str, object]:
    """Validator suitable for :func:`updater.state.atomic_write_json`."""

    if isinstance(value, RolloutState):
        rollout_state = value
    elif isinstance(value, Mapping):
        rollout_state = RolloutState.from_dict(value)
    else:
        raise Live2DAssociatedRolloutError("rollout state must be an object")
    return rollout_state.to_dict()


def candidate_id_for_index(index: IndexInput) -> str:
    """Return a deterministic filesystem-safe candidate identity."""

    return f"index-{compute_keys(validate_index(index)).index_key}"


def load_live2d_index(path: PathInput) -> Live2DIndex:
    """Load and validate an explicit JSON index; never synthesize one."""

    target = _as_path(path, "index_path")
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise Live2DAssociatedRolloutError(
            f"cannot read association index {target}: {exc}"
        ) from exc
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise Live2DAssociatedRolloutError(
            f"association index is not valid JSON: {target}"
        ) from exc
    try:
        return validate_index(decoded)
    except Exception as exc:
        if isinstance(exc, Live2DAssociatedRolloutError):
            raise
        raise Live2DAssociatedRolloutError(
            f"association index is invalid: {target}: {exc}"
        ) from exc


def _validate_publishable_index(index: IndexInput) -> Live2DIndex:
    try:
        validated = validate_index(index)
    except Exception as exc:
        raise Live2DAssociatedRolloutError(f"association index is invalid: {exc}") from exc

    # A rollout must be based on real model records.  A caller that has not yet
    # built the association index must fail closed rather than publishing an
    # empty placeholder.
    if not validated.model_outputs or not validated.models:
        raise Live2DAssociatedRolloutError(
            "association index is empty; build a real Live2DIndex before publication"
        )

    ambiguous = [
        f"{model.model_output_id}:{candidate.motion_set_id}"
        for model in validated.models
        for candidate in model.motion_sets
        if candidate.status == CandidateStatus.AMBIGUOUS.value
    ]
    if ambiguous:
        raise Live2DAssociatedRolloutError(
            "association index contains ambiguous candidates: " + ", ".join(sorted(ambiguous))
        )

    errors = [
        diagnostic.path or diagnostic.code
        for diagnostic in validated.diagnostics
        if diagnostic.severity == "error"
    ]
    if errors:
        raise Live2DAssociatedRolloutError(
            "association index contains validation errors: " + ", ".join(sorted(errors))
        )
    return validated


def validate_publishable_index(index: IndexInput) -> Live2DIndex:
    """Validate an explicit index against rollout acceptance rules."""

    return _validate_publishable_index(index)


def _namespace_paths(namespace_root: PathInput) -> tuple[Path, Path, Path, Path]:
    namespace = _as_path(namespace_root, "namespace_root")
    return (
        namespace,
        namespace / _CANDIDATES_DIRECTORY,
        namespace / _CURRENT_POINTER,
        namespace / _INDEX_FILENAME,
    )


def _prepare_namespace(namespace: Path, candidates: Path) -> None:
    try:
        namespace_stat = namespace.lstat()
    except FileNotFoundError:
        namespace_stat = None
    except OSError as exc:
        raise Live2DAssociatedRolloutError(f"cannot inspect namespace {namespace}: {exc}") from exc
    if namespace_stat is not None:
        if stat.S_ISLNK(namespace_stat.st_mode) or not stat.S_ISDIR(namespace_stat.st_mode):
            raise Live2DAssociatedRolloutError(
                f"namespace_root must be a real directory: {namespace}"
            )
    try:
        state.prepare_state_directory(candidates)
    except state.StateError as exc:
        raise Live2DAssociatedRolloutError(f"cannot prepare namespace {namespace}: {exc}") from exc


def _ensure_regular_target(path: Path, field_name: str) -> None:
    try:
        target_stat = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise Live2DAssociatedRolloutError(f"cannot inspect {field_name} {path}: {exc}") from exc
    if stat.S_ISLNK(target_stat.st_mode):
        raise Live2DAssociatedRolloutError(f"{field_name} may not be a symlink: {path}")
    if not stat.S_ISREG(target_stat.st_mode):
        raise Live2DAssociatedRolloutError(f"{field_name} must be a regular file: {path}")


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Durably replace bytes using the same sibling-temp pattern as state.py."""

    parent = path.parent
    if not parent.is_dir():
        raise Live2DAssociatedRolloutError(f"parent directory does not exist: {parent}")
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        state._fsync_parent(parent)
    except state.StateError as exc:
        raise Live2DAssociatedRolloutError(f"failed to atomically write {path}: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise Live2DAssociatedRolloutError(f"failed to atomically write {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_write_contract(path: Path, index: Live2DIndex) -> None:
    _ensure_regular_target(path, "index target")
    _atomic_write_bytes(path, canonical_index_json_bytes(index))


def _read_json(path: Path, field_name: str) -> object:
    _ensure_regular_target(path, field_name)
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError as exc:
        raise Live2DAssociatedRolloutError(f"{field_name} is missing: {path}") from exc
    except (OSError, TypeError, ValueError) as exc:
        raise Live2DAssociatedRolloutError(f"{field_name} is invalid JSON: {path}") from exc


def _referenced_files(index: Live2DIndex) -> tuple[tuple[str, str], ...]:
    references: list[tuple[str, str]] = []
    for record in index.model_outputs:
        for relative in (
            record.file_references.moc,
            *record.file_references.textures,
            *(
                (record.file_references.physics,)
                if record.file_references.physics is not None
                else ()
            ),
        ):
            references.append((record.output_path, relative))
    for record in index.motion_sets:
        references.extend(
            (record.motion_output_path, f"{clip}.motion3.json")
            for clip in record.known_clips.motions
        )
        references.extend(
            (record.facial_output_path, f"{clip}.motion3.json")
            for clip in record.known_clips.facials
        )
    return tuple(references)


def _reject_legacy_index_references(index: Live2DIndex) -> None:
    for _directory, relative in _referenced_files(index):
        if relative.casefold() == "model_list.json":
            raise Live2DAssociatedRolloutError(
                "associated publication cannot write or reinterpret the legacy live2d/model_list.json"
            )


def _copy_referenced_outputs(index: Live2DIndex, source_root: Path, candidate_root: Path) -> None:
    directories = (
        {record.output_path for record in index.model_outputs}
        | {record.motion_output_path for record in index.motion_sets}
        | {record.facial_output_path for record in index.motion_sets}
    )
    for directory in directories:
        (candidate_root / directory).mkdir(parents=True, exist_ok=True)

    for directory, relative in _referenced_files(index):
        source = source_root / directory / relative
        destination = candidate_root / directory / relative
        if destination.is_symlink():
            raise Live2DAssociatedRolloutError(f"candidate output is a symlink: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, destination)
        except OSError as exc:
            raise Live2DAssociatedRolloutError(
                f"cannot copy referenced Live2D output {source}: {exc}"
            ) from exc


def _file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        raise Live2DAssociatedRolloutError(f"cannot checksum output {path}: {exc}") from exc
    return digest.hexdigest()


def _output_checksums(index: Live2DIndex, root: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for directory, relative in _referenced_files(index):
        key = f"{directory}/{relative}"
        checksums[key] = _file_checksum(root / directory / relative)
    return dict(sorted(checksums.items()))


def _pointer_for_index(index: Live2DIndex, candidate_id: str, root: Path) -> CandidatePointer:
    keys = compute_keys(index)
    return CandidatePointer(
        candidate_id=candidate_id,
        index_key=keys.index_key,
        index_sha256=index_checksum(index),
        canonical_index_sha256=hashlib.sha256(canonical_index_bytes(index)).hexdigest(),
        model_keys=dict(keys.model_keys),
        motion_set_keys=dict(keys.motion_set_keys),
        output_checksums=_output_checksums(index, root),
    )


def _write_pointer_files(candidate_root: Path, pointer: CandidatePointer) -> None:
    payload = pointer.to_dict()
    state.atomic_write_json(
        candidate_root / _CANDIDATE_FILENAME,
        payload,
        validate_candidate_pointer,
    )


def _verify_candidate(
    candidate_root: Path, expected_index: Live2DIndex | None = None
) -> tuple[CurrentPointer, Live2DIndex]:
    index_path = candidate_root / _INDEX_FILENAME
    _ensure_regular_target(index_path, "candidate index")
    index = load_live2d_index(index_path)
    if expected_index is not None and canonical_index_bytes(index) != canonical_index_bytes(
        expected_index
    ):
        raise Live2DAssociatedRolloutError("candidate index differs from the requested index")
    try:
        validate_live2d_outputs(index, candidate_root)
    except Exception as exc:
        raise Live2DAssociatedRolloutError(
            f"candidate referenced outputs are invalid: {exc}"
        ) from exc
    candidate_payload = _read_json(candidate_root / _CANDIDATE_FILENAME, "candidate.json")
    if not isinstance(candidate_payload, Mapping):
        raise Live2DAssociatedRolloutError("candidate.json must be an object")
    pointer = CurrentPointer.from_dict(candidate_payload)
    expected_pointer = _pointer_for_index(index, pointer.candidate_id, candidate_root)
    if pointer.to_dict() != expected_pointer.to_dict():
        raise Live2DAssociatedRolloutError(
            f"candidate.json checksum or key metadata does not match {index_path}"
        )
    return pointer, index


def _candidate_path(candidates: Path, candidate_id: str) -> Path:
    if not isinstance(candidate_id, str) or not _ID_RE.fullmatch(candidate_id):
        raise Live2DAssociatedRolloutError("candidate_id must be a safe non-empty identifier")
    return candidates / candidate_id


def _stage_candidate(
    index: Live2DIndex,
    source_root: Path,
    candidates: Path,
    candidate_id: str,
) -> tuple[Path, CurrentPointer]:
    final = _candidate_path(candidates, candidate_id)
    try:
        final_stat = final.lstat()
    except FileNotFoundError:
        final_stat = None
    except OSError as exc:
        raise Live2DAssociatedRolloutError(f"cannot inspect candidate {final}: {exc}") from exc

    if final_stat is not None:
        if stat.S_ISLNK(final_stat.st_mode) or not stat.S_ISDIR(final_stat.st_mode):
            raise Live2DAssociatedRolloutError(f"candidate path is not a real directory: {final}")
        pointer, existing_index = _verify_candidate(final)
        if canonical_index_bytes(existing_index) != canonical_index_bytes(index):
            raise Live2DAssociatedRolloutError(
                f"candidate_id already contains a different index: {candidate_id}"
            )
        return final, pointer

    temporary: Path | None = Path(tempfile.mkdtemp(prefix=f".{candidate_id}.", dir=candidates))
    try:
        _copy_referenced_outputs(index, source_root, temporary)
        _atomic_write_contract(temporary / _INDEX_FILENAME, index)
        pointer = _pointer_for_index(index, candidate_id, temporary)
        _write_pointer_files(temporary, pointer)
        _verify_candidate(temporary, expected_index=index)
        try:
            assert temporary is not None
            os.rename(temporary, final)
        except FileExistsError as exc:
            raise Live2DAssociatedRolloutError(
                f"candidate was created concurrently: {candidate_id}"
            ) from exc
        temporary = None
        try:
            state._fsync_parent(candidates)
        except state.StateError as exc:
            raise Live2DAssociatedRolloutError(
                f"candidate directory publication is not durable: {exc}"
            ) from exc
        return final, CurrentPointer.from_dict(pointer.to_dict())
    except state.StateError as exc:
        raise Live2DAssociatedRolloutError(f"candidate staging failed: {exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _read_current(namespace: Path) -> tuple[CurrentPointer, Live2DIndex] | None:
    current_path = namespace / _CURRENT_FILENAME
    try:
        current_stat = current_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Live2DAssociatedRolloutError(
            f"cannot inspect namespace current.json: {current_path}"
        ) from exc
    if stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISREG(current_stat.st_mode):
        raise Live2DAssociatedRolloutError(
            f"namespace current.json must be a regular file: {current_path}"
        )

    current_payload = _read_json(current_path, "namespace current.json")
    if not isinstance(current_payload, Mapping):
        raise Live2DAssociatedRolloutError("namespace current.json must be an object")
    current_pointer = CurrentPointer.from_dict(current_payload)
    candidate = _candidate_path(namespace / _CANDIDATES_DIRECTORY, current_pointer.candidate_id)
    if not candidate.is_dir() or candidate.is_symlink():
        raise Live2DAssociatedRolloutError(
            f"namespace current.json references a missing candidate: {current_pointer.candidate_id}"
        )
    candidate_pointer, index = _verify_candidate(candidate)
    if current_pointer.to_dict() != candidate_pointer.to_dict():
        raise Live2DAssociatedRolloutError(
            "namespace current.json does not match its candidate metadata"
        )

    root_index = namespace / _INDEX_FILENAME
    if root_index.exists():
        _ensure_regular_target(root_index, "namespace index")
        try:
            root_index_data = load_live2d_index(root_index)
        except Live2DAssociatedRolloutError:
            raise
        if canonical_index_bytes(root_index_data) != canonical_index_bytes(index):
            raise Live2DAssociatedRolloutError("namespace index.json does not match current")
    return current_pointer, index


def load_current_pointer(namespace_root: PathInput) -> CurrentPointer | None:
    """Validate and return the current pointer, or ``None`` when disabled."""

    namespace, _candidates, _link, _index = _namespace_paths(namespace_root)
    current = _read_current(namespace)
    return None if current is None else current[0]


def load_current_index(namespace_root: PathInput) -> Live2DIndex | None:
    """Validate and return the index reached through the current pointer."""

    namespace, _candidates, _link, _index = _namespace_paths(namespace_root)
    current = _read_current(namespace)
    return None if current is None else current[1]


def read_current(namespace_root: PathInput) -> CurrentPointer | None:
    """Alias for :func:`load_current_pointer`."""

    return load_current_pointer(namespace_root)


def storage_receipt_key(storage: Mapping[str, object]) -> str:
    """Return a stable identity for one associated remote storage target.

    The receipt records the exact configured target, program, and arguments
    that completed.  Changing any of those values intentionally invalidates
    the receipt and causes the next unchanged-index run to upload again.
    """

    if not isinstance(storage, Mapping):
        raise Live2DAssociatedRolloutError("associated storage must be an object")
    try:
        payload = json.dumps(
            dict(storage),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Live2DAssociatedRolloutError(
            f"associated storage is not serializable: {exc}"
        ) from exc
    return hashlib.sha256(payload).hexdigest()


def record_uploaded_storages(
    namespace_root: PathInput,
    candidate_id: str,
    storage_keys: list[str],
    *,
    state_path: PathInput | None = None,
) -> RolloutState:
    """Durably record successful uploads for the current candidate.

    Upload receipts are deliberately written only after every requested remote
    upload succeeds.  They are an optimization guard, not a remote
    reconciliation protocol; a changed storage configuration receives a new
    receipt key and is uploaded again.
    """

    namespace, _candidates, _current, _index = _namespace_paths(namespace_root)
    current = _read_current(namespace)
    if current is None:
        raise Live2DAssociatedRolloutError("cannot record uploads without an associated current")
    pointer = current[0]
    if pointer.candidate_id != candidate_id:
        raise Live2DAssociatedRolloutError(
            f"cannot record uploads for non-current candidate: {candidate_id}"
        )
    normalized_keys = sorted({_validate_sha(key, "storage receipt key") for key in storage_keys})
    target_state = _state_target(state_path, namespace)
    stored = load_rollout_state(target_state)
    if stored is not None and stored.current is not None and stored.current != pointer:
        raise Live2DAssociatedRolloutError(
            "associated rollout state does not match current while recording uploads"
        )
    receipts = dict(stored.uploaded_storages) if stored is not None else {}
    receipts.update({key: candidate_id for key in normalized_keys})
    updated = RolloutState(enabled=True, current=pointer, uploaded_storages=receipts)
    state.prepare_state_directory(target_state.parent)
    state.atomic_write_json(target_state, updated.to_dict(), validate_rollout_state)
    return updated


def _atomic_current_file(namespace: Path, pointer: CurrentPointer) -> None:
    current_path = namespace / _CURRENT_FILENAME
    _ensure_regular_target(current_path, "current metadata target")
    try:
        state.atomic_write_json(current_path, pointer.to_dict(), validate_current_pointer)
    except Live2DAssociatedRolloutError:
        raise
    except state.StateError as exc:
        raise Live2DAssociatedRolloutError(
            f"failed to atomically publish current.json: {exc}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise Live2DAssociatedRolloutError(
            f"failed to atomically publish current.json: {exc}"
        ) from exc


def _snapshot_file(path: Path) -> bytes | None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Live2DAssociatedRolloutError(f"cannot inspect {path}: {exc}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise Live2DAssociatedRolloutError(f"rollback target must be a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise Live2DAssociatedRolloutError(f"cannot read {path}: {exc}") from exc


def _restore_file(path: Path, snapshot: bytes | None) -> None:
    if snapshot is None:
        try:
            path.unlink(missing_ok=True)
            state._fsync_parent(path.parent)
        except state.StateError as exc:
            raise Live2DAssociatedRolloutError(
                f"cannot remove rollback target {path}: {exc}"
            ) from exc
        except OSError as exc:
            raise Live2DAssociatedRolloutError(
                f"cannot remove rollback target {path}: {exc}"
            ) from exc
    else:
        _atomic_write_bytes(path, snapshot)


def _state_target(state_path: PathInput | None, namespace: Path) -> Path:
    target = (
        _as_path(state_path, "state_path")
        if state_path is not None
        else namespace.parent.parent / LIVE2D_ASSOCIATED_STATE_FILENAME
    )
    if target.name.casefold() == "live2d_motion_state.json":
        raise Live2DAssociatedRolloutError(
            "associated rollout state must not reuse live2d_motion_state.json"
        )
    if target in {
        namespace / _INDEX_FILENAME,
        namespace / _CURRENT_FILENAME,
    } or target.is_relative_to(namespace / _CANDIDATES_DIRECTORY):
        raise Live2DAssociatedRolloutError(
            "associated rollout state must be outside the publication namespace"
        )
    return target


def _write_current_transaction(
    namespace: Path,
    index: Live2DIndex,
    pointer: CurrentPointer,
    state_path: Path,
) -> None:
    root_index = namespace / _INDEX_FILENAME
    root_current = namespace / _CURRENT_FILENAME
    old_index = _snapshot_file(root_index)
    old_current = _snapshot_file(root_current)
    old_state = _snapshot_file(state_path)
    new_state = RolloutState(enabled=True, current=pointer).to_dict()
    try:
        _atomic_write_contract(root_index, index)
        state.prepare_state_directory(state_path.parent)
        state.atomic_write_json(state_path, new_state, validate_rollout_state)
        # ``current.json`` is the single pointer document and is deliberately
        # replaced last, after the candidate, root index, and state are valid.
        _atomic_current_file(namespace, pointer)
    except Exception as exc:
        restore_errors: list[str] = []
        try:
            _restore_file(root_index, old_index)
        except Exception as restore_exc:  # pragma: no cover - catastrophic filesystem failure
            restore_errors.append(f"index: {restore_exc}")
        try:
            _restore_file(state_path, old_state)
        except Exception as restore_exc:  # pragma: no cover - catastrophic filesystem failure
            restore_errors.append(f"state: {restore_exc}")
        try:
            # Restore the pointer document last as well, so readers never
            # observe a new current value with old index/state metadata.
            _restore_file(root_current, old_current)
        except Exception as restore_exc:  # pragma: no cover - catastrophic filesystem failure
            restore_errors.append(f"current metadata: {restore_exc}")
        detail = f"; rollback incomplete ({', '.join(restore_errors)})" if restore_errors else ""
        if isinstance(exc, Live2DAssociatedRolloutError):
            raise Live2DAssociatedRolloutError(
                f"associated publication failed: {exc}{detail}"
            ) from exc
        raise Live2DAssociatedRolloutError(f"associated publication failed: {exc}{detail}") from exc


def publish_candidate(
    index: IndexInput,
    output_root: PathInput,
    namespace_root: PathInput,
    *,
    state_path: PathInput | None = None,
    candidate_id: str | None = None,
) -> CurrentPointer:
    """Validate, stage, and atomically make an associated candidate current.

    Validation of the supplied index and every referenced source output happens
    before the namespace index, state, or current pointer is touched.  Outputs
    are copied into the candidate so the current generation is self-contained
    and cannot accidentally become a view of legacy ``live2d/`` files.
    """

    validated = _validate_publishable_index(index)
    source = _as_path(output_root, "output_root")
    namespace, candidates, _link, _root_index = _namespace_paths(namespace_root)
    _reject_legacy_index_references(validated)
    target_state = _state_target(state_path, namespace)
    try:
        validate_live2d_outputs(validated, source)
    except Exception as exc:
        raise Live2DAssociatedRolloutError(f"referenced Live2D outputs are invalid: {exc}") from exc
    _prepare_namespace(namespace, candidates)
    requested_id = candidate_id or candidate_id_for_index(validated)
    previous = _read_current(namespace)

    _candidate, pointer = _stage_candidate(
        validated,
        source,
        candidates,
        requested_id,
    )
    if previous is not None:
        comparison = compare_live2d_indices(previous[1], validated)
        if comparison.unchanged and previous[0].candidate_id == pointer.candidate_id:
            return previous[0]

    current_pointer = CurrentPointer.from_dict(pointer.to_dict())
    _write_current_transaction(
        namespace,
        validated,
        current_pointer,
        target_state,
    )
    return current_pointer


def publish_live2d_associated_index(
    index: IndexInput,
    output_root: PathInput,
    namespace_root: PathInput,
    *,
    state_path: PathInput | None = None,
    candidate_id: str | None = None,
) -> CurrentPointer:
    """Descriptive alias for :func:`publish_candidate`."""

    return publish_candidate(
        index,
        output_root,
        namespace_root,
        state_path=state_path,
        candidate_id=candidate_id,
    )


def rollback_live2d_associated(
    namespace_root: PathInput,
    candidate_id: str,
    *,
    state_path: PathInput | None = None,
) -> CurrentPointer:
    """Atomically replace ``current.json`` with a validated candidate pointer."""

    namespace, candidates, _link, _root_index = _namespace_paths(namespace_root)
    candidate = _candidate_path(candidates, candidate_id)
    if not candidate.is_dir() or candidate.is_symlink():
        raise Live2DAssociatedRolloutError(f"rollback candidate does not exist: {candidate_id}")
    pointer, index = _verify_candidate(candidate)
    publishable = _validate_publishable_index(index)
    previous = _read_current(namespace)
    if previous is not None and previous[0].candidate_id == candidate_id:
        return previous[0]
    target_state = _state_target(state_path, namespace)
    _write_current_transaction(
        namespace,
        publishable,
        CurrentPointer.from_dict(pointer.to_dict()),
        target_state,
    )
    return CurrentPointer.from_dict(pointer.to_dict())


def rollback(
    namespace_root: PathInput,
    candidate_id: str,
    *,
    state_path: PathInput | None = None,
) -> CurrentPointer:
    """Short alias for :func:`rollback_live2d_associated`."""

    return rollback_live2d_associated(
        namespace_root,
        candidate_id,
        state_path=state_path,
    )


def disable_live2d_associated(
    namespace_root: PathInput,
    *,
    state_path: PathInput | None = None,
) -> None:
    """Disable the associated track by removing its current pointer and index.

    Candidate directories are retained for a later explicit rollback or
    re-enable.  Legacy ``live2d/`` files and its model list are never inspected
    or modified.
    """

    namespace, candidates, _link, root_index = _namespace_paths(namespace_root)
    root_current = namespace / _CURRENT_FILENAME
    _prepare_namespace(namespace, candidates)
    old_index = _snapshot_file(root_index)
    old_current = _snapshot_file(root_current)
    target_state = _state_target(state_path, namespace)
    old_state = _snapshot_file(target_state)
    try:
        state.prepare_state_directory(target_state.parent)
        state.atomic_write_json(
            target_state,
            RolloutState(enabled=False, current=None).to_dict(),
            validate_rollout_state,
        )
        _restore_file(root_index, None)
        # Removing the authoritative current document is the final operation.
        _restore_file(root_current, None)
    except Exception as exc:
        try:
            _restore_file(root_index, old_index)
            _restore_file(target_state, old_state)
            _restore_file(root_current, old_current)
        except Exception as restore_exc:  # pragma: no cover - catastrophic filesystem failure
            raise Live2DAssociatedRolloutError(
                f"associated disable failed and rollback was incomplete: {restore_exc}"
            ) from exc
        raise Live2DAssociatedRolloutError(f"associated disable failed: {exc}") from exc


def disable_rollout(
    namespace_root: PathInput,
    *,
    state_path: PathInput | None = None,
) -> None:
    """Short alias for :func:`disable_live2d_associated`."""

    disable_live2d_associated(namespace_root, state_path=state_path)


def load_rollout_state(path_or_config: object) -> RolloutState | None:
    """Load independent rollout state, returning ``None`` only when absent.

    Path-like arguments are explicit state-file paths.  Pass a config object
    when the state path should be derived from ``DL_LIST_CACHE_PATH``.
    """

    target = (
        live2d_associated_state_path(path_or_config)
        if hasattr(path_or_config, "DL_LIST_CACHE_PATH")
        else _as_path(path_or_config, "state_path")
    )
    _ensure_regular_target(target, "rollout state")
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise Live2DAssociatedRolloutError(f"cannot read rollout state {target}: {exc}") from exc
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise Live2DAssociatedRolloutError(f"rollout state is not valid JSON: {target}") from exc
    return (
        RolloutState.from_dict(decoded)
        if isinstance(decoded, Mapping)
        else RolloutState.from_dict({})
    )


# Keep the public vocabulary easy to discover without introducing duplicate
# implementations.  The descriptive names above remain the canonical APIs.
Candidate = CandidatePointer
Current = CurrentPointer
Live2DRolloutError = Live2DAssociatedRolloutError
candidate_path = candidate_directory
publish_index = publish_live2d_associated_index
publish_associated_index = publish_live2d_associated_index
compare_indexes = compare_live2d_indices
canonical_json_equal = canonical_indices_equal
compare_keys = compare_index_keys
rollback_current = rollback_live2d_associated
rollback_candidate = rollback_live2d_associated
disable_association = disable_live2d_associated
disable = disable_live2d_associated
load_state = load_rollout_state
mark_uploaded_storages = record_uploaded_storages
