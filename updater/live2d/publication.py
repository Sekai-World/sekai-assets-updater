"""Validate and atomically publish the additive Live2D association index.

The public API in this module deliberately does not build or copy any Live2D
assets.  ``validate_live2d_outputs`` checks that an already-sanitized index
points at the existing output tree, while ``publish_live2d_index`` performs the
same checks and then replaces only the requested index path.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from updater import state
from updater.live2d.contracts import (
    Live2DIndex,
    ModelOutputRecord,
    SharedMotionSetRecord,
    to_json_dict,
    validate_index,
)

PathInput: TypeAlias = str | os.PathLike[str]
IndexInput: TypeAlias = Live2DIndex | Mapping[str, object]

__all__ = [
    "Live2DPublicationError",
    "publish_live2d_index",
    "validate_live2d_outputs",
]


class Live2DPublicationError(ValueError):
    """Raised when an index cannot be safely published for the output tree."""


@dataclass(frozen=True, slots=True)
class _OutputRoot:
    """The lexical and physical identities of a validated output root."""

    lexical: Path
    resolved: Path


@dataclass(frozen=True, slots=True)
class _CheckedPath:
    """A validated path and its physical identity."""

    lexical: Path
    resolved: Path


@dataclass(frozen=True, slots=True)
class _OutputDirectoryOwner:
    """One indexed record's physical output directory."""

    role: str
    field_name: str
    checked: _CheckedPath


def _absolute_path(value: PathInput, field_name: str) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise Live2DPublicationError(f"{field_name}: expected a filesystem path") from exc

    # ``abspath`` normalizes ``.`` and ``..`` without resolving symlinks.  The
    # latter is important: resolving first would hide a symlink component that
    # the physical validation must reject.
    return Path(os.path.abspath(os.fspath(path)))


def _prepare_output_root(value: PathInput) -> _OutputRoot:
    root = _absolute_path(value, "output_root")
    try:
        root_stat = root.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise Live2DPublicationError(f"output_root: directory does not exist: {root}") from exc
    except OSError as exc:
        raise Live2DPublicationError(f"output_root: cannot inspect {root}: {exc}") from exc

    if stat.S_ISLNK(root_stat.st_mode):
        raise Live2DPublicationError(f"output_root: symlink directories are not allowed: {root}")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise Live2DPublicationError(f"output_root: expected a directory: {root}")

    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Live2DPublicationError(f"output_root: cannot resolve {root}: {exc}") from exc
    return _OutputRoot(lexical=root, resolved=resolved)


def _relative_parts(value: str, field_name: str) -> tuple[str, ...]:
    """Re-check the contract's relative POSIX path policy at the filesystem edge."""

    if not isinstance(value, str) or not value:
        raise Live2DPublicationError(f"{field_name}: expected a non-empty relative POSIX path")
    if value.startswith(("/", "\\", "~")) or "\\" in value or ":" in value:
        raise Live2DPublicationError(f"{field_name}: unsafe relative POSIX path: {value!r}")

    parts = tuple(value.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise Live2DPublicationError(f"{field_name}: unsafe relative POSIX path: {value!r}")
    return parts


def _entry_stat(
    path: Path,
    relative: str,
    field_name: str,
    expected: str,
) -> os.stat_result:
    try:
        entry_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise Live2DPublicationError(
            f"{field_name}: referenced {expected} is missing: {relative!r}"
        ) from exc
    except NotADirectoryError as exc:
        raise Live2DPublicationError(
            f"{field_name}: path component is not a directory: {relative!r}"
        ) from exc
    except OSError as exc:
        raise Live2DPublicationError(
            f"{field_name}: cannot inspect referenced path {relative!r}: {exc}"
        ) from exc

    if stat.S_ISLNK(entry_stat.st_mode):
        raise Live2DPublicationError(
            f"{field_name}: symlink path components are not allowed: {relative!r}"
        )
    return entry_stat


def _inspect_entry_components(
    root: _OutputRoot,
    parts: tuple[str, ...],
    field_name: str,
    expected: str,
) -> tuple[Path, os.stat_result | None]:
    relative = "/".join(parts)
    current = root.lexical
    final_stat: os.stat_result | None = None
    for index, part in enumerate(parts):
        current /= part
        entry_stat = _entry_stat(current, relative, field_name, expected)
        if index < len(parts) - 1 and not stat.S_ISDIR(entry_stat.st_mode):
            raise Live2DPublicationError(
                f"{field_name}: path component is not a directory: {relative!r}"
            )
        final_stat = entry_stat
    return current, final_stat


def _resolve_entry(root: _OutputRoot, current: Path, relative: str, field_name: str) -> Path:
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root.resolved)
    except ValueError as exc:
        raise Live2DPublicationError(
            f"{field_name}: resolved path escapes output_root: {relative!r}"
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise Live2DPublicationError(
            f"{field_name}: cannot resolve referenced path {relative!r}: {exc}"
        ) from exc
    return resolved


def _validate_entry_type(
    entry_stat: os.stat_result,
    relative: str,
    field_name: str,
    expected: str,
) -> None:
    if expected == "directory" and not stat.S_ISDIR(entry_stat.st_mode):
        raise Live2DPublicationError(f"{field_name}: expected a regular directory: {relative!r}")
    if expected == "file" and not stat.S_ISREG(entry_stat.st_mode):
        raise Live2DPublicationError(f"{field_name}: expected a regular file: {relative!r}")


def _checked_entry(
    root: _OutputRoot,
    parts: tuple[str, ...],
    field_name: str,
    expected: str,
) -> _CheckedPath:
    relative = "/".join(parts)
    current, final_stat = _inspect_entry_components(root, parts, field_name, expected)

    # All components were lstat'ed above.  Resolving again gives a physical
    # containment check for roots whose parent path contains a symlink and
    # guards against a lexical path escaping the requested root.
    resolved = _resolve_entry(root, current, relative, field_name)

    assert final_stat is not None  # ``parts`` is always non-empty after validation.
    _validate_entry_type(final_stat, relative, field_name, expected)
    return _CheckedPath(lexical=current, resolved=resolved)


def _checked_directory(root: _OutputRoot, relative: str, field_name: str) -> _CheckedPath:
    return _checked_entry(root, _relative_parts(relative, field_name), field_name, "directory")


def _checked_file(
    root: _OutputRoot,
    base_relative: str,
    relative: str,
    field_name: str,
) -> _CheckedPath:
    base_parts = _relative_parts(base_relative, f"{field_name}.directory")
    child_parts = _relative_parts(relative, field_name)
    return _checked_entry(root, base_parts + child_parts, field_name, "file")


def _validate_output_directory_aliases(
    directories: list[_OutputDirectoryOwner],
) -> None:
    model_directories = [directory for directory in directories if directory.role == "model"]
    shared_directories = [directory for directory in directories if directory.role != "model"]

    for model_directory in model_directories:
        model_path = model_directory.checked.resolved
        for shared_directory in shared_directories:
            shared_path = shared_directory.checked.resolved
            if shared_path == model_path or shared_path.is_relative_to(model_path):
                raise Live2DPublicationError(
                    f"{shared_directory.field_name}: physical output directory "
                    f"{shared_path} is equal to or nested under model output "
                    f"{model_directory.field_name} ({model_path}); model output "
                    "directories must not contain motion or facial outputs"
                )

    seen: dict[Path, _OutputDirectoryOwner] = {}
    for directory in directories:
        resolved = directory.checked.resolved
        previous = seen.get(resolved)
        if previous is not None:
            raise Live2DPublicationError(
                f"{directory.field_name}: physical output directory {resolved} is already "
                f"used by {previous.field_name}; output directories must be distinct"
            )
        seen[resolved] = directory


def _collect_model_output_directories(
    index: Live2DIndex,
    root: _OutputRoot,
) -> tuple[set[Path], list[_OutputDirectoryOwner]]:
    protected_directories: set[Path] = set()
    output_directories: list[_OutputDirectoryOwner] = []
    for record in index.model_outputs:
        field = f"model_outputs[{record.model_output_id!r}]"
        output_directory = _checked_directory(
            root,
            record.output_path,
            f"{field}.output_path",
        )
        protected_directories.add(output_directory.resolved)
        output_directories.append(
            _OutputDirectoryOwner(
                role="model",
                field_name=f"{field}.output_path",
                checked=output_directory,
            )
        )
    return protected_directories, output_directories


def _validate_motion_directory_pair(
    field: str,
    motion_directory: _CheckedPath,
    facial_directory: _CheckedPath,
) -> None:
    try:
        same_directory = os.path.samefile(
            motion_directory.lexical,
            facial_directory.lexical,
        )
    except OSError as exc:
        raise Live2DPublicationError(
            f"{field}: cannot compare motion and facial output directories: {exc}"
        ) from exc
    if same_directory:
        raise Live2DPublicationError(
            f"{field}: motion and facial output directories must be physically separate"
        )


def _collect_motion_output_directories(
    index: Live2DIndex,
    root: _OutputRoot,
) -> tuple[set[Path], list[_OutputDirectoryOwner]]:
    protected_directories: set[Path] = set()
    output_directories: list[_OutputDirectoryOwner] = []
    for record in index.motion_sets:
        field = f"motion_sets[{record.motion_set_id!r}]"
        motion_directory = _checked_directory(
            root,
            record.motion_output_path,
            f"{field}.motion_output_path",
        )
        facial_directory = _checked_directory(
            root,
            record.facial_output_path,
            f"{field}.facial_output_path",
        )
        _validate_motion_directory_pair(field, motion_directory, facial_directory)
        output_directories.extend(
            (
                _OutputDirectoryOwner(
                    role="motion",
                    field_name=f"{field}.motion_output_path",
                    checked=motion_directory,
                ),
                _OutputDirectoryOwner(
                    role="facial",
                    field_name=f"{field}.facial_output_path",
                    checked=facial_directory,
                ),
            )
        )
        protected_directories.update((motion_directory.resolved, facial_directory.resolved))
    return protected_directories, output_directories


def _validate_model_record_references(root: _OutputRoot, record: ModelOutputRecord) -> None:
    field = f"model_outputs[{record.model_output_id!r}]"
    references = record.file_references
    _checked_file(
        root,
        record.output_path,
        references.moc,
        f"{field}.file_references.Moc",
    )
    for texture_index, texture in enumerate(references.textures):
        _checked_file(
            root,
            record.output_path,
            texture,
            f"{field}.file_references.Textures[{texture_index}]",
        )
    if references.physics is not None:
        _checked_file(
            root,
            record.output_path,
            references.physics,
            f"{field}.file_references.Physics",
        )


def _validate_model_references(index: Live2DIndex, root: _OutputRoot) -> None:
    for record in index.model_outputs:
        _validate_model_record_references(root, record)


def _validate_motion_clip_files(
    root: _OutputRoot,
    directory: str,
    clip_names: tuple[str, ...],
    field: str,
    clip_kind: str,
) -> None:
    for clip_index, clip_name in enumerate(clip_names):
        _checked_file(
            root,
            directory,
            f"{clip_name}.motion3.json",
            f"{field}.known_clips.{clip_kind}[{clip_index}]",
        )


def _validate_motion_record_references(
    root: _OutputRoot,
    record: SharedMotionSetRecord,
) -> None:
    field = f"motion_sets[{record.motion_set_id!r}]"
    _validate_motion_clip_files(
        root,
        record.motion_output_path,
        record.known_clips.motions,
        field,
        "motions",
    )
    _validate_motion_clip_files(
        root,
        record.facial_output_path,
        record.known_clips.facials,
        field,
        "facials",
    )


def _validate_motion_references(index: Live2DIndex, root: _OutputRoot) -> None:
    for record in index.motion_sets:
        _validate_motion_record_references(root, record)


def _validate_physical_references(index: Live2DIndex, root: _OutputRoot) -> set[Path]:
    protected_directories, output_directories = _collect_model_output_directories(index, root)
    motion_protected, motion_directories = _collect_motion_output_directories(index, root)
    protected_directories.update(motion_protected)
    output_directories.extend(motion_directories)
    _validate_output_directory_aliases(output_directories)
    _validate_model_references(index, root)
    _validate_motion_references(index, root)
    return protected_directories


def validate_live2d_outputs(index: IndexInput, output_root: PathInput) -> Live2DIndex:
    """Validate an index contract and every referenced output without writing.

    ``index`` may be a ``Live2DIndex`` or a mapping already sanitized for that
    contract.  The returned value is the immutable, canonical ``Live2DIndex``
    produced by contract validation.
    """

    validated = validate_index(index)
    root = _prepare_output_root(output_root)
    _validate_physical_references(validated, root)
    return validated


def _validate_index_target(index_path: PathInput, protected_directories: set[Path]) -> Path:
    target = _absolute_path(index_path, "index_path")
    if target.name.casefold() == "model_list.json":
        raise Live2DPublicationError(
            "index_path: model_list.json is authoritative and cannot be replaced"
        )

    try:
        target_stat = target.stat(follow_symlinks=False)
    except FileNotFoundError:
        target_stat = None
    except OSError as exc:
        raise Live2DPublicationError(f"index_path: cannot inspect {target}: {exc}") from exc
    if target_stat is not None and stat.S_ISLNK(target_stat.st_mode):
        raise Live2DPublicationError(f"index_path: symlink targets are not allowed: {target}")

    try:
        resolved_target = target.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise Live2DPublicationError(f"index_path: cannot resolve {target}: {exc}") from exc
    for protected_directory in protected_directories:
        try:
            resolved_target.relative_to(protected_directory)
        except ValueError:
            continue
        raise Live2DPublicationError(
            f"index_path: cannot publish inside a referenced output directory: {target}"
        )
    return target


def _validate_published_index(value: IndexInput) -> dict[str, object]:
    """Retain the contract validator at the ``atomic_write_json`` boundary."""

    return to_json_dict(validate_index(value))


def publish_live2d_index(
    index: IndexInput,
    output_root: PathInput,
    index_path: PathInput,
) -> Live2DIndex:
    """Validate and atomically publish one additive Live2D association index.

    No model output or ``model_list.json`` path is written.  Validation is
    complete before ``updater.state.atomic_write_json`` is called, and that
    primitive performs the final contract validation and temporary-sibling
    replacement.  The validated immutable index is returned after publication.
    """

    validated = validate_index(index)
    root = _prepare_output_root(output_root)
    protected_directories = _validate_physical_references(validated, root)
    target = _validate_index_target(index_path, protected_directories)
    payload = to_json_dict(validated)
    state.atomic_write_json(target, payload, _validate_published_index)
    return validated
