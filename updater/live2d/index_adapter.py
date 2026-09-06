"""Build Live2D output records from explicitly selected local artifacts.

This adapter is deliberately narrower than the association builder.  Callers
select the bundle metadata, output directories, and record identifiers; this
module only validates those selections and observes the files needed by the
contracts.  It does not derive a model/motion relationship or rewrite bundle
names.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from updater.live2d.contracts import (
    MODEL_OUTPUT_SCHEMA_VERSION,
    BundleIdentity,
    KnownClips,
    Model3FileReferences,
    ModelOutputRecord,
    SharedMotionSetRecord,
)
from updater.net.plan import get_bundle_checksum

PathInput: TypeAlias = str | os.PathLike[str]
BundleMetadata: TypeAlias = Mapping[str, object]
_MODEL3_SUFFIX = ".model3.json"

__all__ = [
    "BundleMetadata",
    "Live2DIndexAdapterError",
    "PathInput",
    "build_model_output_record",
    "build_shared_motion_set_record",
]


class Live2DIndexAdapterError(ValueError):
    """Raised when selected Live2D metadata or output is not safe to record."""


@dataclass(frozen=True, slots=True)
class _Root:
    lexical: Path
    resolved: Path


@dataclass(frozen=True, slots=True)
class _CheckedEntry:
    lexical: Path
    resolved: Path
    relative_parts: tuple[str, ...]


def _absolute_path(value: PathInput, field_name: str) -> Path:
    try:
        text = os.fspath(value)
    except (TypeError, ValueError) as exc:
        raise Live2DIndexAdapterError(f"{field_name}: expected a filesystem path") from exc
    if not isinstance(text, str):
        raise Live2DIndexAdapterError(f"{field_name}: expected a text filesystem path")
    if "\x00" in text:
        raise Live2DIndexAdapterError(f"{field_name}: path contains a NUL byte")
    try:
        return Path(os.path.abspath(text))
    except (OSError, ValueError) as exc:
        raise Live2DIndexAdapterError(f"{field_name}: invalid filesystem path") from exc


def _reject_existing_symlink_components(path: Path, field_name: str) -> None:
    """Reject symlinks in every existing component of an absolute path."""

    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        except OSError as exc:
            raise Live2DIndexAdapterError(
                f"{field_name}: cannot inspect path component {current}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise Live2DIndexAdapterError(
                f"{field_name}: symlink path components are not allowed: {current}"
            )


def _prepare_root(value: PathInput, field_name: str) -> _Root:
    root = _absolute_path(value, field_name)
    _reject_existing_symlink_components(root, field_name)
    try:
        mode = os.lstat(root).st_mode
    except FileNotFoundError as exc:
        raise Live2DIndexAdapterError(f"{field_name}: directory does not exist: {root}") from exc
    except OSError as exc:
        raise Live2DIndexAdapterError(f"{field_name}: cannot inspect directory: {root}") from exc
    if stat.S_ISLNK(mode):  # Defensive; the component check above also catches this.
        raise Live2DIndexAdapterError(f"{field_name}: directory must not be a symlink: {root}")
    if not stat.S_ISDIR(mode):
        raise Live2DIndexAdapterError(f"{field_name}: expected a directory: {root}")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Live2DIndexAdapterError(f"{field_name}: cannot resolve directory: {root}") from exc
    return _Root(lexical=root, resolved=resolved)


def _relative_parts(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise Live2DIndexAdapterError(f"{field_name}: expected a non-empty relative POSIX path")
    if "\x00" in value:
        raise Live2DIndexAdapterError(f"{field_name}: path contains a NUL byte")
    if value.startswith(("/", "\\", "~")) or "\\" in value or ":" in value:
        raise Live2DIndexAdapterError(f"{field_name}: unsafe relative POSIX path: {value!r}")
    parts = tuple(value.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise Live2DIndexAdapterError(f"{field_name}: unsafe relative POSIX path: {value!r}")
    return parts


def _ensure_physical_containment(root: _Root, path: Path, field_name: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Live2DIndexAdapterError(
            f"{field_name}: cannot resolve referenced path: {path}"
        ) from exc
    try:
        resolved.relative_to(root.resolved)
    except ValueError as exc:
        raise Live2DIndexAdapterError(
            f"{field_name}: resolved path escapes the output root: {path}"
        ) from exc
    return resolved


def _entry_mode(
    path: Path,
    relative: str,
    field_name: str,
    expected: str,
) -> int:
    try:
        return os.lstat(path).st_mode
    except FileNotFoundError as exc:
        raise Live2DIndexAdapterError(
            f"{field_name}: referenced {expected} is missing: {relative!r}"
        ) from exc
    except NotADirectoryError as exc:
        raise Live2DIndexAdapterError(
            f"{field_name}: path component is not a directory: {relative!r}"
        ) from exc
    except OSError as exc:
        raise Live2DIndexAdapterError(
            f"{field_name}: cannot inspect referenced path: {path}"
        ) from exc


def _inspect_entry_components(
    root: _Root,
    parts: tuple[str, ...],
    field_name: str,
    expected: str,
    *,
    case_insensitive: bool = False,
) -> tuple[Path, int, tuple[str, ...]]:
    relative = "/".join(parts)
    current = root.lexical
    final_mode: int | None = None
    actual_parts: list[str] = []
    for index, part in enumerate(parts):
        current, mode = _resolve_entry_component(
            current,
            part,
            relative,
            field_name,
            expected,
            case_insensitive=case_insensitive,
        )
        if stat.S_ISLNK(mode):
            raise Live2DIndexAdapterError(
                f"{field_name}: symlink path components are not allowed: {current}"
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(mode):
            raise Live2DIndexAdapterError(
                f"{field_name}: path component is not a directory: {relative!r}"
            )
        final_mode = mode
        actual_parts.append(current.name)

    assert final_mode is not None
    return current, final_mode, tuple(actual_parts)


def _resolve_entry_component(
    current: Path,
    part: str,
    relative: str,
    field_name: str,
    expected: str,
    *,
    case_insensitive: bool,
) -> tuple[Path, int]:
    candidate = current / part
    if not case_insensitive:
        return candidate, _entry_mode(candidate, relative, field_name, expected)

    # ``lstat`` on a case-insensitive filesystem can succeed for a differently
    # cased spelling.  Enumerating the real directory first lets automatic
    # discovery recover the on-disk spelling instead of retaining metadata's
    # spelling.  Exact entry names still win, preserving the strict path rule.
    try:
        with os.scandir(current) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError:
        return candidate, _entry_mode(candidate, relative, field_name, expected)

    exact_matches = [name for name in names if name == part]
    if not exact_matches:
        matches = [name for name in names if name.casefold() == part.casefold()]
        if len(matches) > 1:
            raise Live2DIndexAdapterError(
                f"{field_name}: ambiguous case-insensitive path component {part!r} "
                f"under {current}: {', '.join(matches)}"
            )
        if matches:
            candidate = current / matches[0]
    return candidate, _entry_mode(candidate, relative, field_name, expected)


def _validate_entry_type(
    mode: int,
    parts: tuple[str, ...],
    field_name: str,
    expected: str,
) -> None:
    relative = "/".join(parts)
    if expected == "directory" and not stat.S_ISDIR(mode):
        raise Live2DIndexAdapterError(f"{field_name}: expected a regular directory: {relative!r}")
    if expected == "file" and not stat.S_ISREG(mode):
        raise Live2DIndexAdapterError(f"{field_name}: expected a regular file: {relative!r}")


def _checked_entry(
    root: _Root,
    parts: tuple[str, ...],
    field_name: str,
    expected: str,
    *,
    case_insensitive: bool = False,
) -> _CheckedEntry:
    if not parts:
        if expected != "directory":  # pragma: no cover - all generated files have a name
            raise Live2DIndexAdapterError(f"{field_name}: expected a path below the output root")
        return _CheckedEntry(root.lexical, root.resolved, parts)

    current, final_mode, actual_parts = _inspect_entry_components(
        root,
        parts,
        field_name,
        expected,
        case_insensitive=case_insensitive,
    )
    resolved = _ensure_physical_containment(root, current, field_name)
    _validate_entry_type(final_mode, actual_parts, field_name, expected)
    return _CheckedEntry(current, resolved, actual_parts)


def _checked_directory(
    root: _Root,
    value: str,
    field_name: str,
    *,
    case_insensitive: bool = False,
) -> _CheckedEntry:
    return _checked_entry(
        root,
        _relative_parts(value, field_name),
        field_name,
        "directory",
        case_insensitive=case_insensitive,
    )


def _checked_explicit_directory(
    root: _Root,
    value: PathInput,
    field_name: str,
) -> _CheckedEntry:
    """Check a caller-selected directory, accepting a relative or contained absolute path."""

    try:
        raw_text = os.fspath(value)
    except (TypeError, ValueError) as exc:
        raise Live2DIndexAdapterError(f"{field_name}: expected a filesystem path") from exc
    if not isinstance(raw_text, str):
        raise Live2DIndexAdapterError(f"{field_name}: expected a text filesystem path")
    if not raw_text:
        raise Live2DIndexAdapterError(f"{field_name}: expected a non-empty directory path")

    candidate = Path(raw_text)
    if not candidate.is_absolute():
        return _checked_directory(root, raw_text, field_name)

    absolute = _absolute_path(raw_text, field_name)
    try:
        relative = absolute.relative_to(root.lexical)
    except ValueError as exc:
        raise Live2DIndexAdapterError(
            f"{field_name}: absolute path must be contained by the output root"
        ) from exc
    parts = relative.parts
    return _checked_entry(root, parts, field_name, "directory")


def _checked_child_file(
    root: _Root,
    directory: _CheckedEntry,
    filename: str,
    field_name: str,
) -> _CheckedEntry:
    filename_parts = _relative_parts(filename, f"{field_name}.filename")
    return _checked_entry(root, directory.relative_parts + filename_parts, field_name, "file")


def _scan_directory_entries(directory: Path, field_name: str) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return sorted(entries, key=lambda entry: entry.name)
    except OSError as exc:
        raise Live2DIndexAdapterError(
            f"{field_name}: cannot inspect output directory: {directory}"
        ) from exc


def _scan_entry_mode(root: _Root, path: Path, field_name: str) -> int:
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise Live2DIndexAdapterError(f"{field_name}: cannot inspect output path: {path}") from exc
    if stat.S_ISLNK(mode):
        raise Live2DIndexAdapterError(
            f"{field_name}: symlink files and directories are not allowed: {path}"
        )
    _ensure_physical_containment(root, path, field_name)
    return mode


def _require_regular_model3_file(path: Path, mode: int, field_name: str) -> None:
    if not stat.S_ISREG(mode):
        raise Live2DIndexAdapterError(f"{field_name}: model3 output is not a regular file: {path}")


def _scan_model3_files(
    root: _Root,
    output_directory: _CheckedEntry,
    field_name: str,
) -> list[Path]:
    """Recursively observe model3 files while rejecting every symlink encountered."""

    found: list[Path] = []
    pending: list[tuple[Path, tuple[str, ...]]] = [
        (output_directory.lexical, output_directory.relative_parts)
    ]

    while pending:
        directory, directory_parts = pending.pop()
        children = _scan_directory_entries(directory, field_name)

        for entry in children:
            child = directory / entry.name
            child_parts = directory_parts + (entry.name,)
            mode = _scan_entry_mode(root, child, field_name)
            if stat.S_ISDIR(mode):
                pending.append((child, child_parts))
            elif entry.name.endswith(".model3.json"):
                _require_regular_model3_file(child, mode, field_name)
                found.append(child)

    return sorted(found, key=lambda path: path.as_posix())


def _discover_model3_paths(
    output_root: PathInput,
    output_path: str,
) -> tuple[str, tuple[str, ...]]:
    """Return an actual output path and safe model3 paths beneath it.

    Automatic metadata paths are allowed to differ from the extracted tree only
    by component case.  The resolved lexical path is returned so later strict
    publication and viewer validation uses the spelling that exists on disk.
    """

    root = _prepare_root(output_root, "output_root")
    output_directory = _checked_directory(
        root,
        output_path,
        "output_path",
        case_insensitive=True,
    )
    model3_files = _scan_model3_files(root, output_directory, "model output")
    actual_output_path = "/".join(output_directory.relative_parts)
    return actual_output_path, tuple(
        path.relative_to(output_directory.lexical).as_posix() for path in model3_files
    )


def _select_model3_file(
    root: _Root,
    output_directory: _CheckedEntry,
    model3_path: str | None,
) -> tuple[Path, str]:
    if model3_path is None:
        model3_files = _scan_model3_files(root, output_directory, "model output")
        if not model3_files:
            raise Live2DIndexAdapterError(
                f"model output: no *{_MODEL3_SUFFIX} file found under "
                f"{'/'.join(output_directory.relative_parts)!r}"
            )
        if len(model3_files) != 1:
            paths = ", ".join(path.as_posix() for path in model3_files)
            raise Live2DIndexAdapterError(
                f"model output: expected exactly one *{_MODEL3_SUFFIX} file, "
                f"found {len(model3_files)}: {paths}"
            )
        selected = model3_files[0]
        return selected, selected.relative_to(output_directory.lexical).as_posix()

    if not model3_path.endswith(_MODEL3_SUFFIX):
        raise Live2DIndexAdapterError("model3_path: must name a .model3.json file")
    model3_parts = _relative_parts(model3_path, "model3_path")
    selected = _checked_entry(
        root,
        output_directory.relative_parts + model3_parts,
        "model3_path",
        "file",
    )
    return selected.lexical, "/".join(model3_parts)


def _read_json(path: Path, field_name: str) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise Live2DIndexAdapterError(f"{field_name}: cannot read JSON file: {path}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise Live2DIndexAdapterError(f"{field_name}: malformed JSON: {path}") from exc


def _bundle_identity(bundle: BundleMetadata, field_name: str) -> BundleIdentity:
    if not isinstance(bundle, Mapping):
        raise Live2DIndexAdapterError(f"{field_name}: expected bundle metadata object")

    bundle_name = bundle.get("bundleName")
    if bundle_name is None:
        raise Live2DIndexAdapterError(f"{field_name}: bundleName is required")
    if not isinstance(bundle_name, str):
        raise Live2DIndexAdapterError(f"{field_name}: bundleName must be a string")
    try:
        checksum_kind, checksum_value = get_bundle_checksum(dict(bundle))
    except (TypeError, ValueError) as exc:
        raise Live2DIndexAdapterError(f"{field_name}: invalid bundle metadata") from exc
    if checksum_kind is None:
        raise Live2DIndexAdapterError(
            f"{field_name}: bundle metadata must contain a usable hash or crc"
        )

    # BundleIdentity has one opaque token rather than separate hash/crc fields.
    # Retain the source kind so equal manifest values from different fields do
    # not silently acquire the same identity.
    checksum = f"{checksum_kind}:{checksum_value}"
    try:
        return BundleIdentity(name=bundle_name, checksum=checksum)
    except (TypeError, ValueError) as exc:
        raise Live2DIndexAdapterError(f"{field_name}: invalid BundleIdentity") from exc


def _model_references(model3_path: Path) -> Model3FileReferences:
    raw_model3 = _read_json(model3_path, "model3")
    try:
        return Model3FileReferences.from_model3_json(raw_model3)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise Live2DIndexAdapterError(f"model3: invalid FileReferences in {model3_path}") from exc


def build_model_output_record(
    *,
    output_root: PathInput,
    output_path: str,
    model_output_id: str,
    bundle: BundleMetadata,
    metadata_version: str,
    model3_path: str | None = None,
) -> ModelOutputRecord:
    """Build one model record from one explicitly selected model3 document."""

    root = _prepare_root(output_root, "output_root")
    output_directory = _checked_directory(root, output_path, "output_path")
    selected_model3, selected_model3_path = _select_model3_file(
        root,
        output_directory,
        model3_path,
    )
    references = _model_references(selected_model3)
    model_bundle = _bundle_identity(bundle, "model bundle")
    try:
        return ModelOutputRecord(
            model_output_id=model_output_id,
            model_bundle=model_bundle,
            output_path=output_path,
            model3_path=selected_model3_path,
            file_references=references,
            metadata_version=metadata_version,
            schema_version=MODEL_OUTPUT_SCHEMA_VERSION,
        )
    except (TypeError, ValueError) as exc:
        raise Live2DIndexAdapterError(f"model output record is invalid: {exc}") from exc


def _known_clips(
    root: _Root,
    motion_bundle_directory: _CheckedEntry,
    motion_directory: _CheckedEntry,
    facial_directory: _CheckedEntry,
) -> KnownClips:
    build_motion_data = _checked_child_file(
        root,
        motion_bundle_directory,
        "BuildMotionData.json",
        "motion BuildMotionData",
    )
    raw_data = _read_json(build_motion_data.lexical, "motion BuildMotionData")
    if not isinstance(raw_data, Mapping):
        raise Live2DIndexAdapterError(
            "motion BuildMotionData: expected a JSON object with motions and expressions arrays"
        )
    if "motions" not in raw_data or "expressions" not in raw_data:
        raise Live2DIndexAdapterError(
            "motion BuildMotionData: motions and expressions arrays are required"
        )

    motions = raw_data["motions"]
    expressions = raw_data["expressions"]
    if not isinstance(motions, list) or not isinstance(expressions, list):
        raise Live2DIndexAdapterError(
            "motion BuildMotionData: motions and expressions must be JSON arrays"
        )
    if any(not isinstance(name, str) for name in motions):
        raise Live2DIndexAdapterError("motion BuildMotionData: motions must contain only strings")
    if any(not isinstance(name, str) for name in expressions):
        raise Live2DIndexAdapterError(
            "motion BuildMotionData: expressions must contain only strings"
        )

    motion_names = tuple(cast(str, name) for name in motions)
    facial_names = tuple(cast(str, name) for name in expressions)
    try:
        known_clips = KnownClips(motions=motion_names, facials=facial_names)
    except (TypeError, ValueError) as exc:
        raise Live2DIndexAdapterError(f"motion BuildMotionData: invalid clip names: {exc}") from exc

    for index, clip_name in enumerate(known_clips.motions):
        _checked_child_file(
            root,
            motion_directory,
            f"{clip_name}.motion3.json",
            f"motion clips[{index}]",
        )
    for index, clip_name in enumerate(known_clips.facials):
        _checked_child_file(
            root,
            facial_directory,
            f"{clip_name}.motion3.json",
            f"facial clips[{index}]",
        )
    return known_clips


def build_shared_motion_set_record(
    *,
    output_root: PathInput,
    motion_bundle_output_path: PathInput,
    motion_output_path: str,
    facial_output_path: str,
    motion_set_id: str,
    bundle: BundleMetadata,
    metadata_version: str,
) -> SharedMotionSetRecord:
    """Build one shared motion-set record from explicit restored directories.

    ``motion_bundle_output_path`` identifies the directory containing
    ``BuildMotionData.json``.  It may be relative to ``output_root`` or an
    absolute path already contained by that root.  The two record paths remain
    exactly the caller-provided relative POSIX strings.
    """

    root = _prepare_root(output_root, "output_root")
    motion_bundle_directory = _checked_explicit_directory(
        root,
        motion_bundle_output_path,
        "motion_bundle_output_path",
    )
    motion_directory = _checked_directory(root, motion_output_path, "motion_output_path")
    facial_directory = _checked_directory(root, facial_output_path, "facial_output_path")
    if motion_directory.resolved == facial_directory.resolved:
        raise Live2DIndexAdapterError(
            "motion output: motion and facial output directories must be physically separate"
        )

    known_clips = _known_clips(
        root,
        motion_bundle_directory,
        motion_directory,
        facial_directory,
    )
    motion_bundle = _bundle_identity(bundle, "motion bundle")
    try:
        return SharedMotionSetRecord(
            motion_set_id=motion_set_id,
            motion_bundle=motion_bundle,
            motion_output_path=motion_output_path,
            facial_output_path=facial_output_path,
            known_clips=known_clips,
            metadata_version=metadata_version,
        )
    except (TypeError, ValueError) as exc:
        raise Live2DIndexAdapterError(f"motion-set record is invalid: {exc}") from exc
