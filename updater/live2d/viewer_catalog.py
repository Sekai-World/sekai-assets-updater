"""Build and stage the public Live2D viewer projection.

The association index is deliberately richer than the public viewer contract.
This module is the boundary between those two representations: it validates
the selected output tree, copies only viewer assets into a clean staging tree,
and writes the legacy-shaped ``model_list.json`` that is uploaded last.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from updater import state
from updater.live2d.contracts import CandidateStatus, Live2DIndex, validate_index
from updater.live2d.publication import validate_live2d_outputs

PathInput: TypeAlias = str | os.PathLike[str]
IndexInput: TypeAlias = Live2DIndex | Mapping[str, object]

PUBLIC_MODEL_LIST_FILENAME = "model_list.json"
# Keep the catalog vocabulary available to callers while making the public
# filename explicit: the remote representation is intentionally not an audit
# catalog or a versioned manifest.
VIEWER_CATALOG_FILENAME = PUBLIC_MODEL_LIST_FILENAME
MODEL3_SUFFIX = ".model3.json"
MOTION3_SUFFIX = ".motion3.json"
PUBLISHABLE_CANDIDATE_STATUSES = frozenset(
    (CandidateStatus.VERIFIED.value, CandidateStatus.DERIVED.value)
)

__all__ = [
    "IndexInput",
    "Live2DViewerCatalogError",
    "MODEL3_SUFFIX",
    "MOTION3_SUFFIX",
    "PUBLIC_MODEL_LIST_FILENAME",
    "PUBLISHABLE_CANDIDATE_STATUSES",
    "VIEWER_CATALOG_FILENAME",
    "build_public_model_list",
    "build_viewer_catalog",
    "collect_viewer_asset_files",
    "copy_viewer_assets",
    "find_model3_file",
    "stage_viewer_projection",
    "validate_viewer_catalog",
    "viewer_asset_directories",
    "write_viewer_catalog",
]


class Live2DViewerCatalogError(ValueError):
    """Raised when a public Live2D viewer projection is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class _Root:
    lexical: Path
    resolved: Path


def _absolute_path(value: PathInput, field_name: str) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(value)))
    except (TypeError, ValueError) as exc:
        raise Live2DViewerCatalogError(f"{field_name}: expected a filesystem path") from exc


def _prepare_root(value: PathInput, field_name: str = "output_root") -> _Root:
    root = _absolute_path(value, field_name)
    try:
        root_stat = root.lstat()
    except FileNotFoundError as exc:
        raise Live2DViewerCatalogError(f"{field_name}: directory does not exist: {root}") from exc
    except OSError as exc:
        raise Live2DViewerCatalogError(f"{field_name}: cannot inspect {root}: {exc}") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise Live2DViewerCatalogError(f"{field_name}: expected a real directory: {root}")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Live2DViewerCatalogError(f"{field_name}: cannot resolve {root}: {exc}") from exc
    return _Root(root, resolved)


def _prepare_destination_root(value: PathInput) -> _Root:
    root = _absolute_path(value, "destination_root")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Live2DViewerCatalogError(f"destination_root: cannot create {root}: {exc}") from exc
    return _prepare_root(root, "destination_root")


def _relative_parts(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise Live2DViewerCatalogError(f"{field_name}: expected a non-empty relative POSIX path")
    if "\x00" in value or value.startswith(("/", "\\", "~")):
        raise Live2DViewerCatalogError(f"{field_name}: unsafe relative POSIX path: {value!r}")
    if "\\" in value or ":" in value:
        raise Live2DViewerCatalogError(f"{field_name}: unsafe relative POSIX path: {value!r}")
    parts = tuple(value.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise Live2DViewerCatalogError(f"{field_name}: unsafe relative POSIX path: {value!r}")
    return parts


def _relative_text(parts: Sequence[str]) -> str:
    return "/".join(parts)


def _checked_entry(
    root: _Root,
    parts: tuple[str, ...],
    field_name: str,
    expected: str,
) -> Path:
    current = root.lexical
    final_stat: os.stat_result | None = None
    relative = _relative_text(parts)
    for index, part in enumerate(parts):
        current /= part
        try:
            entry_stat = current.lstat()
        except FileNotFoundError as exc:
            raise Live2DViewerCatalogError(
                f"{field_name}: referenced path is missing: {relative!r}"
            ) from exc
        except NotADirectoryError as exc:
            raise Live2DViewerCatalogError(
                f"{field_name}: path component is not a directory: {relative!r}"
            ) from exc
        except OSError as exc:
            raise Live2DViewerCatalogError(
                f"{field_name}: cannot inspect referenced path {relative!r}: {exc}"
            ) from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise Live2DViewerCatalogError(
                f"{field_name}: symlink path components are not allowed: {relative!r}"
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(entry_stat.st_mode):
            raise Live2DViewerCatalogError(
                f"{field_name}: path component is not a directory: {relative!r}"
            )
        final_stat = entry_stat

    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root.resolved)
    except ValueError as exc:
        raise Live2DViewerCatalogError(
            f"{field_name}: resolved path escapes output_root: {relative!r}"
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise Live2DViewerCatalogError(
            f"{field_name}: cannot resolve referenced path {relative!r}: {exc}"
        ) from exc

    assert final_stat is not None
    if expected == "directory" and not stat.S_ISDIR(final_stat.st_mode):
        raise Live2DViewerCatalogError(f"{field_name}: expected a directory: {relative!r}")
    if expected == "file" and not stat.S_ISREG(final_stat.st_mode):
        raise Live2DViewerCatalogError(f"{field_name}: expected a regular file: {relative!r}")
    return current


def _checked_directory(root: _Root, relative: str, field_name: str) -> Path:
    return _checked_entry(root, _relative_parts(relative, field_name), field_name, "directory")


def _checked_file(root: _Root, directory: str, relative: str, field_name: str) -> Path:
    parts = _relative_parts(directory, f"{field_name}.directory") + _relative_parts(
        relative, field_name
    )
    return _checked_entry(root, parts, field_name, "file")


def _validate_source_index(index: IndexInput, output_root: PathInput) -> tuple[Live2DIndex, _Root]:
    try:
        validated = validate_index(index)
    except Exception as exc:
        raise Live2DViewerCatalogError(f"association index is invalid: {exc}") from exc
    root = _prepare_root(output_root)
    try:
        validate_live2d_outputs(validated, root.lexical)
    except Exception as exc:
        raise Live2DViewerCatalogError(f"referenced Live2D outputs are invalid: {exc}") from exc
    return validated, root


def find_model3_file(output_root: PathInput, output_path: str) -> Path:
    """Find the one regular model3 document beneath one selected output."""

    root = _prepare_root(output_root)
    output_directory = _checked_directory(root, output_path, "model output")
    pending = [output_directory]
    found: list[Path] = []
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise Live2DViewerCatalogError(
                f"model output: cannot inspect directory {directory}: {exc}"
            ) from exc
        for entry in entries:
            try:
                entry_stat = entry.lstat()
            except OSError as exc:
                raise Live2DViewerCatalogError(
                    f"model output: cannot inspect entry {entry}: {exc}"
                ) from exc
            if stat.S_ISLNK(entry_stat.st_mode):
                raise Live2DViewerCatalogError(
                    f"model output: symlink entries are not allowed: {entry}"
                )
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append(entry)
            elif stat.S_ISREG(entry_stat.st_mode) and entry.name.endswith(MODEL3_SUFFIX):
                found.append(entry)

    if len(found) != 1:
        detail = ", ".join(path.as_posix() for path in found)
        raise Live2DViewerCatalogError(
            f"model output: expected exactly one regular {MODEL3_SUFFIX} beneath "
            f"{output_path!r}, found {len(found)}{f': {detail}' if detail else ''}"
        )
    return found[0]


def _public_relative(root: _Root, path: Path, field_name: str) -> str:
    try:
        relative = path.relative_to(root.lexical)
    except ValueError as exc:
        raise Live2DViewerCatalogError(
            f"{field_name}: path is outside the output root: {path}"
        ) from exc
    return "/".join(relative.parts)


def _asset_files(index: Live2DIndex, root: _Root) -> tuple[tuple[str, Path], ...]:
    files: dict[str, Path] = {}

    def add_file(key: str, source: Path) -> None:
        previous = files.get(key)
        if previous is not None and previous != source:
            raise Live2DViewerCatalogError(
                f"public asset path is selected by multiple sources: {key!r}"
            )
        files[key] = source

    for record in index.model_outputs:
        model3 = find_model3_file(root.lexical, record.output_path)
        model3_key = _public_relative(root, model3, "model3")
        add_file(model3_key, model3)
        references = record.file_references
        model_references = (
            references.moc,
            *references.textures,
            *((references.physics,) if references.physics is not None else ()),
        )
        for relative in model_references:
            source = _checked_file(
                root,
                record.output_path,
                relative,
                f"model_outputs[{record.model_output_id!r}].file_references",
            )
            add_file(_public_relative(root, source, "model reference"), source)

    for record in index.motion_sets:
        for directory, clips in (
            (record.motion_output_path, record.known_clips.motions),
            (record.facial_output_path, record.known_clips.facials),
        ):
            for clip in clips:
                relative = f"{clip}{MOTION3_SUFFIX}"
                source = _checked_file(
                    root,
                    directory,
                    relative,
                    f"motion_sets[{record.motion_set_id!r}].known_clips",
                )
                add_file(_public_relative(root, source, "motion reference"), source)

    return tuple(sorted(files.items(), key=lambda item: item[0]))


def collect_viewer_asset_files(
    index: IndexInput, output_root: PathInput
) -> tuple[tuple[str, Path], ...]:
    """Return all selected public asset files, including each model3 document."""

    validated, root = _validate_source_index(index, output_root)
    return _asset_files(validated, root)


def viewer_asset_directories(index: IndexInput) -> tuple[str, ...]:
    """Return deterministic output directories used by the public projection."""

    try:
        validated = validate_index(index)
    except Exception as exc:
        raise Live2DViewerCatalogError(f"association index is invalid: {exc}") from exc
    directories = (
        {record.output_path for record in validated.model_outputs}
        | {record.motion_output_path for record in validated.motion_sets}
        | {record.facial_output_path for record in validated.motion_sets}
    )
    return tuple(sorted(directories))


def _ensure_destination_directory(root: _Root, relative: str) -> Path:
    parts = _relative_parts(relative, "destination path")
    current = root.lexical
    for part in parts:
        current /= part
        try:
            entry_stat = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
            except OSError as exc:
                raise Live2DViewerCatalogError(
                    f"destination path: cannot create directory {current}: {exc}"
                ) from exc
            entry_stat = current.lstat()
        except OSError as exc:
            raise Live2DViewerCatalogError(
                f"destination path: cannot inspect {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(entry_stat.st_mode) or not stat.S_ISDIR(entry_stat.st_mode):
            raise Live2DViewerCatalogError(
                f"destination path: directory component is unsafe: {current}"
            )
    return current


def copy_viewer_assets(
    index: IndexInput,
    source_root: PathInput,
    destination_root: PathInput,
) -> None:
    """Copy only validated model, motion, and facial files to a clean tree."""

    validated, source = _validate_source_index(index, source_root)
    destination = _prepare_destination_root(destination_root)
    for directory in viewer_asset_directories(validated):
        _ensure_destination_directory(destination, directory)

    for relative, source_path in _asset_files(validated, source):
        destination_path = destination.lexical / Path(*relative.split("/"))
        _ensure_destination_directory(destination, "/".join(relative.split("/")[:-1]))
        try:
            existing = destination_path.lstat()
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise Live2DViewerCatalogError(
                f"destination path: cannot inspect {destination_path}: {exc}"
            ) from exc
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
        ):
            raise Live2DViewerCatalogError(
                f"destination path: file target is unsafe: {destination_path}"
            )
        try:
            shutil.copy2(source_path, destination_path, follow_symlinks=False)
        except OSError as exc:
            raise Live2DViewerCatalogError(
                f"cannot copy public Live2D asset {source_path}: {exc}"
            ) from exc


def _safe_filename(value: object, field_name: str, suffix: str | None = None) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise Live2DViewerCatalogError(f"{field_name}: expected a non-empty filename")
    if "\x00" in value or "/" in value or "\\" in value or ":" in value:
        raise Live2DViewerCatalogError(f"{field_name}: unsafe filename: {value!r}")
    if suffix is not None and not value.endswith(suffix):
        raise Live2DViewerCatalogError(f"{field_name}: expected suffix {suffix!r}")
    return value


def _safe_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or "\\" in value:
        raise Live2DViewerCatalogError(f"{field_name}: expected a safe identifier")
    return value


def _validate_motion_entry(value: object, field_name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise Live2DViewerCatalogError(f"{field_name}: expected an object")
    expected = {"motionSetId", "motionPath", "motionFiles", "facialPath", "facialFiles"}
    if set(value) != expected:
        raise Live2DViewerCatalogError(f"{field_name}: fields must be exactly {sorted(expected)!r}")
    motions = value["motionFiles"]
    facials = value["facialFiles"]
    if not isinstance(motions, Sequence) or isinstance(motions, (str, bytes)):
        raise Live2DViewerCatalogError(f"{field_name}.motionFiles: expected an array")
    if not isinstance(facials, Sequence) or isinstance(facials, (str, bytes)):
        raise Live2DViewerCatalogError(f"{field_name}.facialFiles: expected an array")
    return {
        "motionSetId": _safe_identifier(value["motionSetId"], f"{field_name}.motionSetId"),
        "motionPath": _relative_text(
            _relative_parts(value["motionPath"], f"{field_name}.motionPath")
        ),
        "motionFiles": sorted(
            _safe_filename(item, f"{field_name}.motionFiles[{index}]", MOTION3_SUFFIX)
            for index, item in enumerate(motions)
        ),
        "facialPath": _relative_text(
            _relative_parts(value["facialPath"], f"{field_name}.facialPath")
        ),
        "facialFiles": sorted(
            _safe_filename(item, f"{field_name}.facialFiles[{index}]", MOTION3_SUFFIX)
            for index, item in enumerate(facials)
        ),
    }


def validate_viewer_catalog(value: object) -> list[dict[str, object]]:
    """Validate and normalize the intentionally small public model-list shape."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise Live2DViewerCatalogError("viewer catalog must be an array")
    result: list[dict[str, object]] = []
    seen_models: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        field_name = f"catalog[{index}]"
        if not isinstance(item, Mapping):
            raise Live2DViewerCatalogError(f"{field_name}: expected an object")
        expected = {"modelName", "modelBase", "modelPath", "modelFile", "motionSets"}
        if set(item) != expected:
            raise Live2DViewerCatalogError(
                f"{field_name}: fields must be exactly {sorted(expected)!r}"
            )
        motion_sets = item["motionSets"]
        if not isinstance(motion_sets, Sequence) or isinstance(motion_sets, (str, bytes)):
            raise Live2DViewerCatalogError(f"{field_name}.motionSets: expected an array")
        model_path = _relative_text(_relative_parts(item["modelPath"], f"{field_name}.modelPath"))
        model_file = _safe_filename(item["modelFile"], f"{field_name}.modelFile", MODEL3_SUFFIX)
        model_key = (model_path, model_file)
        if model_key in seen_models:
            raise Live2DViewerCatalogError(f"{field_name}: duplicate model path")
        seen_models.add(model_key)
        normalized_motion_sets = [
            _validate_motion_entry(motion, f"{field_name}.motionSets[{motion_index}]")
            for motion_index, motion in enumerate(motion_sets)
        ]
        motion_ids = [entry["motionSetId"] for entry in normalized_motion_sets]
        if len(set(motion_ids)) != len(motion_ids):
            raise Live2DViewerCatalogError(f"{field_name}: duplicate motionSetId")
        result.append(
            {
                "modelName": _safe_filename(item["modelName"], f"{field_name}.modelName"),
                "modelBase": _safe_filename(item["modelBase"], f"{field_name}.modelBase"),
                "modelPath": model_path,
                "modelFile": model_file,
                "motionSets": sorted(
                    normalized_motion_sets, key=lambda entry: entry["motionSetId"]
                ),
            }
        )
    return sorted(result, key=lambda item: (item["modelPath"], item["modelFile"]))


def _publishable_index(index: IndexInput) -> Live2DIndex:
    try:
        validated = validate_index(index)
    except Exception as exc:
        raise Live2DViewerCatalogError(f"association index is invalid: {exc}") from exc
    if not validated.model_outputs or not validated.models:
        raise Live2DViewerCatalogError("association index is empty")
    ambiguous = [
        f"{model.model_output_id}:{candidate.motion_set_id}"
        for model in validated.models
        for candidate in model.motion_sets
        if candidate.status not in PUBLISHABLE_CANDIDATE_STATUSES
    ]
    if ambiguous:
        raise Live2DViewerCatalogError(
            "association index contains non-publishable candidates: " + ", ".join(sorted(ambiguous))
        )
    if any(diagnostic.severity == "error" for diagnostic in validated.diagnostics):
        raise Live2DViewerCatalogError("association index contains validation errors")
    return validated


def build_viewer_catalog(index: IndexInput, output_root: PathInput) -> list[dict[str, object]]:
    """Project a validated association index into the public viewer model list."""

    validated = _publishable_index(index)
    _, root = _validate_source_index(validated, output_root)
    # Validate every selected model output, including a model that currently has
    # no joined motion candidate.  A public model entry must never hide a bad
    # model3 selection merely because its association list is empty.
    _asset_files(validated, root)
    association_by_id = {
        association.model_output_id: association for association in validated.models
    }
    motion_by_id = {record.motion_set_id: record for record in validated.motion_sets}
    result: list[dict[str, object]] = []
    for model_record in validated.model_outputs:
        association = association_by_id.get(model_record.model_output_id)
        model3 = find_model3_file(root.lexical, model_record.output_path)
        model_path = Path(_public_relative(root, model3, "model3")).parent.as_posix()
        motion_entries: list[dict[str, object]] = []
        if association is not None:
            for candidate in association.motion_sets:
                if candidate.status not in PUBLISHABLE_CANDIDATE_STATUSES:
                    continue
                motion_record = motion_by_id[candidate.motion_set_id]
                motion_entries.append(
                    {
                        "motionSetId": candidate.motion_set_id,
                        "motionPath": motion_record.motion_output_path,
                        "motionFiles": [
                            f"{clip}{MOTION3_SUFFIX}" for clip in motion_record.known_clips.motions
                        ],
                        "facialPath": motion_record.facial_output_path,
                        "facialFiles": [
                            f"{clip}{MOTION3_SUFFIX}" for clip in motion_record.known_clips.facials
                        ],
                    }
                )
        result.append(
            {
                "modelName": model3.name.removesuffix(MODEL3_SUFFIX),
                "modelBase": model3.parent.name,
                "modelPath": model_path,
                "modelFile": model3.name,
                "motionSets": motion_entries,
            }
        )
    return validate_viewer_catalog(result)


def write_viewer_catalog(
    destination_root: PathInput,
    index: IndexInput,
    *,
    source_root: PathInput | None = None,
) -> list[dict[str, object]]:
    """Write the public model list atomically without touching legacy output."""

    destination = _prepare_destination_root(destination_root)
    payload = build_viewer_catalog(index, source_root or destination.lexical)
    try:
        state.atomic_write_json(
            destination.lexical / PUBLIC_MODEL_LIST_FILENAME,
            payload,
            validate_viewer_catalog,
        )
    except Exception as exc:
        raise Live2DViewerCatalogError(
            f"cannot write public {PUBLIC_MODEL_LIST_FILENAME}: {exc}"
        ) from exc
    return payload


def stage_viewer_projection(
    index: IndexInput,
    source_root: PathInput,
    destination_root: PathInput,
) -> list[dict[str, object]]:
    """Stage only public assets and ``model_list.json`` in a temporary tree."""

    validated = _publishable_index(index)
    copy_viewer_assets(validated, source_root, destination_root)
    return write_viewer_catalog(destination_root, validated)


build_public_model_list = build_viewer_catalog
