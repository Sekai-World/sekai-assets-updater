"""ACB/AWB track decoding backed by the native cridecoder library.

This module previously bundled a pure-Python ACB parser (derived from
VGMToolbox). It is now a thin wrapper around cridecoder (Rust), keeping the same
public ``extract_acb`` signature so the rest of the codebase is unchanged.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from io import BufferedReader, BytesIO
from pathlib import Path
from typing import Optional, Union

import cridecoder

__all__ = ["decode_acb_bytes", "extract_acb"]


def _validate_cue_name(cue_name: str) -> None:
    if (
        not cue_name
        or cue_name in (".", "..")
        or "/" in cue_name
        or "\\" in cue_name
        or ":" in cue_name
        or "\x00" in cue_name
    ):
        raise ValueError("cue_name must be a safe filename stem")


def _requested_output_names(cue_name: str, extensions: list[str]) -> list[str]:
    _validate_cue_name(cue_name)
    if any("/" in ext or "\\" in ext or ":" in ext or "\x00" in ext for ext in extensions):
        raise ValueError("decoder returned an unsafe output extension")
    return [
        f"{cue_name}.{extension}" if index == 1 else f"{cue_name}-{index}.{extension}"
        for index, extension in enumerate(extensions, start=1)
    ]


def _resolve_decoded_output(output: str, target_dir: Path) -> Path:
    source = Path(output)
    if not source.is_absolute():
        target_relative_source = target_dir / source
        if target_relative_source.exists():
            source = target_relative_source
    if source.is_symlink():
        raise ValueError(f"decoded ACB output must not be a symlink: {output!r}")
    try:
        resolved = source.resolve(strict=True)
        resolved.relative_to(target_dir.resolve())
    except (OSError, ValueError) as exc:
        raise ValueError(f"decoded ACB output is outside target_dir: {output!r}") from exc
    if not resolved.is_file():
        raise ValueError(f"decoded ACB output is not a file: {output!r}")
    return resolved


def _validate_and_group_destinations(
    sources: list[Path], destinations: list[Path]
) -> dict[Path, list[Path]]:
    if len(set(destinations)) != len(destinations):
        raise ValueError("requested ACB output filenames are not unique")

    source_set = set(sources)
    for destination in destinations:
        if destination.exists() and destination not in source_set:
            raise FileExistsError(f"ACB output target already exists: {destination}")
        if destination.is_symlink():
            raise ValueError(f"ACB output target must not be a symlink: {destination}")

    destination_groups: dict[Path, list[Path]] = {}
    for source, destination in zip(sources, destinations, strict=True):
        destination_groups.setdefault(source, []).append(destination)
    return destination_groups


def _stage_decoded_outputs(
    destination_groups: dict[Path, list[Path]],
    target_root: Path,
    staged: dict[Path, Path],
) -> None:
    for source in destination_groups:
        fd, temporary_name = tempfile.mkstemp(prefix=".acb-", dir=target_root)
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            os.replace(source, temporary_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        staged[source] = temporary_path


def _move_staged_outputs(
    destination_groups: dict[Path, list[Path]],
    staged: dict[Path, Path],
    moved_destinations: list[tuple[Path, Path]],
    copied_destinations: list[Path],
) -> None:
    for source, group in destination_groups.items():
        temporary_path = staged[source]
        destination = group[0]
        os.replace(temporary_path, destination)
        moved_destinations.append((destination, temporary_path))
        for duplicate_destination in group[1:]:
            shutil.copy2(destination, duplicate_destination)
            copied_destinations.append(duplicate_destination)


def _rollback_decoded_outputs(
    staged: dict[Path, Path],
    moved_destinations: list[tuple[Path, Path]],
    copied_destinations: list[Path],
) -> None:
    for destination in reversed(copied_destinations):
        try:
            destination.unlink()
        except OSError:
            pass
    for destination, temporary_path in reversed(moved_destinations):
        try:
            os.replace(destination, temporary_path)
        except OSError:
            pass
    for source, temporary_path in staged.items():
        if temporary_path.exists():
            try:
                os.replace(temporary_path, source)
            except OSError:
                pass


def _rename_decoded_outputs(outputs: list[str], target_dir: str, cue_name: str) -> list[str]:
    target_path = Path(target_dir)
    target_root = target_path.resolve()
    sources = [_resolve_decoded_output(output, target_path) for output in outputs]
    extensions = [source.suffix.lstrip(".") or "wav" for source in sources]
    filenames = _requested_output_names(cue_name, extensions)
    destinations = [target_root / filename for filename in filenames]
    destination_groups = _validate_and_group_destinations(sources, destinations)

    staged: dict[Path, Path] = {}
    moved_destinations: list[tuple[Path, Path]] = []
    copied_destinations: list[Path] = []
    try:
        _stage_decoded_outputs(destination_groups, target_root, staged)
        _move_staged_outputs(destination_groups, staged, moved_destinations, copied_destinations)
    except Exception:
        _rollback_decoded_outputs(staged, moved_destinations, copied_destinations)
        raise

    return [os.fspath(target_path / filename) for filename in filenames]


def decode_acb_bytes(
    acb_data: bytes,
    cue_name: Optional[str] = None,
) -> list[tuple[str, bytes]]:
    """Decode ACB bytes fully in memory to ``(filename, wav_bytes)`` pairs.

    Only the embedded AWB can be resolved from bytes; external streaming
    ``.awb`` archives require the path-based :func:`extract_acb`. Raises on
    invalid input and raises when the ACB has no tracks, so callers can fall
    back to the path-based decoder on failure. When ``cue_name`` is supplied,
    every decoded track is retained under a stable requested filename.
    """
    tracks = cridecoder.decode_acb_to_wav_bytes(acb_data, None) or []
    if not tracks:
        raise ValueError("in-memory ACB decode produced no tracks")
    decoded: list[tuple[str, str, bytes]] = []
    for track in tracks:
        name = str(track["name"])
        extension_value = track["extension"]
        extension = (str(extension_value) if extension_value else "wav").lstrip(".")
        data = track["data"]
        if not isinstance(data, bytes):
            raise TypeError(f"cridecoder returned non-bytes track data for {name!r}")
        decoded.append((name, extension, data))

    if cue_name is None:
        return [(f"{name}.{extension}", data) for name, extension, data in decoded]

    filenames = _requested_output_names(cue_name, [extension for _, extension, _ in decoded])
    return [
        (filename, data)
        for filename, (_name, _extension, data) in zip(filenames, decoded, strict=True)
    ]


def extract_acb(
    acb_file: Union[BytesIO, BufferedReader],
    target_dir: str,
    acb_file_path: str,
    cue_name: Optional[str] = None,
) -> list[str]:
    """Decode audio tracks from an ACB to WAV files in ``target_dir``.

    ``acb_file`` is an open binary stream of the ACB; ``acb_file_path`` is the
    path the ACB logically lives at — its parent directory is used to resolve
    external streaming ``.awb`` archives. ``cue_name`` optionally supplies a
    stable filename prefix for every decoded track; it does not filter tracks
    based on cridecoder's generated names.

    Returns the list of written WAV file paths.
    """
    existing_acb_path = os.fspath(acb_file_path)
    tmp_path: str | None = None
    if os.path.exists(existing_acb_path):
        decode_path = existing_acb_path
    else:
        acb_file.seek(0)
        acb_bytes = acb_file.read()

        parent = os.path.dirname(existing_acb_path) or "."
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=".acb", dir=parent)
        with os.fdopen(fd, "wb") as fh:
            fh.write(acb_bytes)
        decode_path = tmp_path

    try:
        outputs = list(cridecoder.decode_acb_to_wav(decode_path, target_dir, None) or [])
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if not outputs:
        raise ValueError("path-based ACB decode produced no tracks")
    if cue_name is None:
        return outputs
    return _rename_decoded_outputs(outputs, target_dir, cue_name)
