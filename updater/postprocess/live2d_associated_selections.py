"""Load explicit Live2D association selections for the production caller."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from updater.live2d.index_builder import (
    ModelOutputSelection,
    SharedMotionSetSelection,
)
from updater.live2d.master_data import LocalMasterDataProvider

MANIFEST_SCHEMA_VERSION = 1
PathInput: TypeAlias = str | os.PathLike[str]

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")

__all__ = [
    "Live2DAssociatedManifest",
    "Live2DAssociatedSelections",
    "Live2DAssociatedSelectionsError",
    "MANIFEST_SCHEMA_VERSION",
    "build_live2d_associated_selections",
    "load_live2d_associated_manifest",
    "load_live2d_associated_selections",
]


class Live2DAssociatedSelectionsError(ValueError):
    """Raised when an explicit association-selection manifest is unusable."""


@dataclass(frozen=True, slots=True)
class _ModelOutputManifestEntry:
    model_output_id: str
    bundle_key: str
    output_path: str
    model3_path: str | None


@dataclass(frozen=True, slots=True)
class _MotionSetManifestEntry:
    motion_set_id: str
    bundle_key: str
    motion_bundle_output_path: str
    motion_output_path: str
    facial_output_path: str


@dataclass(frozen=True, slots=True)
class Live2DAssociatedManifest:
    """Validated, unresolved contents of an association-selection manifest."""

    master_data_root: str
    master_db_version: str
    model_outputs: tuple[_ModelOutputManifestEntry, ...]
    motion_sets: tuple[_MotionSetManifestEntry, ...]


@dataclass(frozen=True, slots=True)
class Live2DAssociatedSelections:
    """Builder inputs resolved from one explicit manifest and current metadata."""

    provider: LocalMasterDataProvider
    model_outputs: tuple[ModelOutputSelection, ...]
    motion_sets: tuple[SharedMotionSetSelection, ...]


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise Live2DAssociatedSelectionsError(f"{field_name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise Live2DAssociatedSelectionsError(f"{field_name} must use string field names")
    return value


def _reject_unknown_keys(value: Mapping[str, object], allowed: set[str], field_name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise Live2DAssociatedSelectionsError(
            f"{field_name} contains unsupported fields: {', '.join(unknown)}"
        )


def _text(value: object, field_name: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value:
        raise Live2DAssociatedSelectionsError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise Live2DAssociatedSelectionsError(
            f"{field_name} must not have leading or trailing whitespace"
        )
    if len(value) > max_length:
        raise Live2DAssociatedSelectionsError(
            f"{field_name} must be at most {max_length} characters"
        )
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise Live2DAssociatedSelectionsError(f"{field_name} contains a control character")
    return value


def _identifier(value: object, field_name: str) -> str:
    identifier = _text(value, field_name, max_length=256)
    if not _ID_RE.fullmatch(identifier):
        raise Live2DAssociatedSelectionsError(f"{field_name} must be a stable identifier token")
    return identifier


def _relative_path(value: object, field_name: str) -> str:
    path = _text(value, field_name, max_length=1024)
    if path.startswith(("/", "\\", "~")) or "\\" in path or ":" in path:
        raise Live2DAssociatedSelectionsError(f"{field_name} is not a safe relative POSIX path")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise Live2DAssociatedSelectionsError(f"{field_name} is not a safe relative POSIX path")
    return path


def _sequence(value: object, field_name: str) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise Live2DAssociatedSelectionsError(f"{field_name} must be an array")
    return tuple(value)


def _parse_model_entry(value: object, index: int) -> _ModelOutputManifestEntry:
    field_name = f"model_outputs[{index}]"
    mapping = _mapping(value, field_name)
    _reject_unknown_keys(mapping, {"id", "bundle", "output_path", "model3_path"}, field_name)
    for required in ("id", "bundle", "output_path"):
        if required not in mapping:
            raise Live2DAssociatedSelectionsError(f"{field_name}.{required} is required")
    return _ModelOutputManifestEntry(
        model_output_id=_identifier(mapping["id"], f"{field_name}.id"),
        bundle_key=_text(mapping["bundle"], f"{field_name}.bundle", max_length=1024),
        output_path=_relative_path(mapping["output_path"], f"{field_name}.output_path"),
        model3_path=(
            None
            if "model3_path" not in mapping
            else _relative_path(mapping["model3_path"], f"{field_name}.model3_path")
        ),
    )


def _parse_motion_entry(value: object, index: int) -> _MotionSetManifestEntry:
    field_name = f"motion_sets[{index}]"
    mapping = _mapping(value, field_name)
    _reject_unknown_keys(
        mapping,
        {
            "id",
            "bundle",
            "motion_bundle_output_path",
            "motion_output_path",
            "facial_output_path",
        },
        field_name,
    )
    required = (
        "id",
        "bundle",
        "motion_bundle_output_path",
        "motion_output_path",
        "facial_output_path",
    )
    for name in required:
        if name not in mapping:
            raise Live2DAssociatedSelectionsError(f"{field_name}.{name} is required")
    return _MotionSetManifestEntry(
        motion_set_id=_identifier(mapping["id"], f"{field_name}.id"),
        bundle_key=_text(mapping["bundle"], f"{field_name}.bundle", max_length=1024),
        motion_bundle_output_path=_relative_path(
            mapping["motion_bundle_output_path"],
            f"{field_name}.motion_bundle_output_path",
        ),
        motion_output_path=_relative_path(
            mapping["motion_output_path"], f"{field_name}.motion_output_path"
        ),
        facial_output_path=_relative_path(
            mapping["facial_output_path"], f"{field_name}.facial_output_path"
        ),
    )


def _validate_unique_ids(
    entries: tuple[_ModelOutputManifestEntry, ...] | tuple[_MotionSetManifestEntry, ...],
    field_name: str,
) -> None:
    seen: set[str] = set()
    for entry in entries:
        entry_id = (
            entry.model_output_id
            if isinstance(entry, _ModelOutputManifestEntry)
            else entry.motion_set_id
        )
        if entry_id in seen:
            raise Live2DAssociatedSelectionsError(
                f"{field_name} contains duplicate id {entry_id!r}"
            )
        seen.add(entry_id)


def _parse_manifest(value: object) -> Live2DAssociatedManifest:
    mapping = _mapping(value, "manifest")
    _reject_unknown_keys(
        mapping,
        {"schema_version", "master_data", "model_outputs", "motion_sets"},
        "manifest",
    )
    if (
        type(mapping.get("schema_version")) is not int
        or mapping["schema_version"] != MANIFEST_SCHEMA_VERSION
    ):
        raise Live2DAssociatedSelectionsError(
            f"manifest.schema_version must be {MANIFEST_SCHEMA_VERSION}"
        )
    for required in ("master_data", "model_outputs", "motion_sets"):
        if required not in mapping:
            raise Live2DAssociatedSelectionsError(f"manifest.{required} is required")

    master_data = _mapping(mapping["master_data"], "master_data")
    _reject_unknown_keys(master_data, {"root", "master_db_version"}, "master_data")
    for required in ("root", "master_db_version"):
        if required not in master_data:
            raise Live2DAssociatedSelectionsError(f"master_data.{required} is required")

    model_outputs = tuple(
        _parse_model_entry(item, index)
        for index, item in enumerate(_sequence(mapping["model_outputs"], "model_outputs"))
    )
    motion_sets = tuple(
        _parse_motion_entry(item, index)
        for index, item in enumerate(_sequence(mapping["motion_sets"], "motion_sets"))
    )
    _validate_unique_ids(model_outputs, "model_outputs")
    _validate_unique_ids(motion_sets, "motion_sets")
    return Live2DAssociatedManifest(
        master_data_root=_text(master_data["root"], "master_data.root", max_length=4096),
        master_db_version=_identifier(
            master_data["master_db_version"], "master_data.master_db_version"
        ),
        model_outputs=model_outputs,
        motion_sets=motion_sets,
    )


def _manifest_path(value: PathInput) -> Path:
    try:
        raw = os.fspath(value)
    except (TypeError, ValueError) as exc:
        raise Live2DAssociatedSelectionsError("manifest_path must be a filesystem path") from exc
    if not isinstance(raw, str) or not raw:
        raise Live2DAssociatedSelectionsError("manifest_path must be a non-empty text path")
    if "\x00" in raw:
        raise Live2DAssociatedSelectionsError("manifest_path contains a NUL byte")
    return Path(raw)


def load_live2d_associated_manifest(path: PathInput) -> Live2DAssociatedManifest:
    """Read and validate one explicit association-selection manifest."""

    target = _manifest_path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise Live2DAssociatedSelectionsError(
            f"cannot read Live2D association selections manifest {target}: {exc}"
        ) from exc
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise Live2DAssociatedSelectionsError(
            f"Live2D association selections manifest is not valid JSON: {target}"
        ) from exc
    return _parse_manifest(decoded)


def _bundle_metadata(
    live2d_bundles: Mapping[str, object], bundle_key: str, field_name: str
) -> Mapping[str, object]:
    try:
        bundle = live2d_bundles[bundle_key]
    except KeyError as exc:
        raise Live2DAssociatedSelectionsError(
            f"{field_name}.bundle does not exist in current live2d_bundles: {bundle_key!r}"
        ) from exc
    if not isinstance(bundle, Mapping):
        raise Live2DAssociatedSelectionsError(
            f"current live2d_bundles[{bundle_key!r}] must be an object"
        )
    return bundle


def build_live2d_associated_selections(
    manifest: Live2DAssociatedManifest,
    *,
    output_root: PathInput,
    live2d_bundles: Mapping[str, object],
) -> Live2DAssociatedSelections:
    """Resolve manifest bundle keys against current metadata without inference."""

    if not isinstance(manifest, Live2DAssociatedManifest):
        raise Live2DAssociatedSelectionsError(
            "manifest must be a validated Live2DAssociatedManifest"
        )
    if not isinstance(live2d_bundles, Mapping):
        raise Live2DAssociatedSelectionsError("live2d_bundles must be an object mapping")

    model_outputs = tuple(
        ModelOutputSelection(
            output_root=output_root,
            output_path=entry.output_path,
            model_output_id=entry.model_output_id,
            bundle=_bundle_metadata(live2d_bundles, entry.bundle_key, f"model_outputs[{index}]"),
            model3_path=entry.model3_path,
        )
        for index, entry in enumerate(manifest.model_outputs)
    )
    motion_sets = tuple(
        SharedMotionSetSelection(
            output_root=output_root,
            motion_bundle_output_path=entry.motion_bundle_output_path,
            motion_output_path=entry.motion_output_path,
            facial_output_path=entry.facial_output_path,
            motion_set_id=entry.motion_set_id,
            bundle=_bundle_metadata(live2d_bundles, entry.bundle_key, f"motion_sets[{index}]"),
        )
        for index, entry in enumerate(manifest.motion_sets)
    )
    return Live2DAssociatedSelections(
        provider=LocalMasterDataProvider(
            root=manifest.master_data_root,
            master_db_version=manifest.master_db_version,
        ),
        model_outputs=model_outputs,
        motion_sets=motion_sets,
    )


def load_live2d_associated_selections(
    path: PathInput,
    *,
    output_root: PathInput,
    live2d_bundles: Mapping[str, object],
) -> Live2DAssociatedSelections:
    """Load a manifest and resolve its selections against current bundle metadata."""

    return build_live2d_associated_selections(
        load_live2d_associated_manifest(path),
        output_root=output_root,
        live2d_bundles=live2d_bundles,
    )
