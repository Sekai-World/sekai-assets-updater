"""ACB/AWB track decoding backed by the native cridecoder library.

This module previously bundled a pure-Python ACB parser (derived from
VGMToolbox). It is now a thin wrapper around cridecoder (Rust), keeping the same
public ``extract_acb`` signature so the rest of the codebase is unchanged.
"""

from __future__ import annotations

import os
import tempfile
from io import BufferedReader, BytesIO
from pathlib import Path
from typing import Optional, Union

import cridecoder

__all__ = ["decode_acb_bytes", "extract_acb"]


def decode_acb_bytes(
    acb_data: bytes,
    cue_name: Optional[str] = None,
) -> list[tuple[str, bytes]]:
    """Decode ACB bytes fully in memory to ``(filename, wav_bytes)`` pairs.

    Only the embedded AWB can be resolved from bytes; external streaming
    ``.awb`` archives require the path-based :func:`extract_acb`. Raises on
    invalid input and returns an empty list only when the ACB has no tracks,
    so callers can fall back to the path-based decoder on failure.
    """
    tracks = cridecoder.decode_acb_to_wav_bytes(acb_data, None)
    outputs: list[tuple[str, bytes]] = []
    for track in tracks:
        name = track["name"]
        if cue_name is not None and name != cue_name:
            continue
        extension = (track["extension"] or "wav").lstrip(".")
        outputs.append((f"{name}.{extension}", track["data"]))
    if not tracks:
        raise ValueError("in-memory ACB decode produced no tracks")
    return outputs


def extract_acb(
    acb_file: Union[BytesIO, BufferedReader],
    target_dir: str,
    acb_file_path: str,
    cue_name: Optional[str] = None,
) -> list[str]:
    """Decode audio tracks from an ACB to WAV files in ``target_dir``.

    ``acb_file`` is an open binary stream of the ACB; ``acb_file_path`` is the
    path the ACB logically lives at — its parent directory is used to resolve
    external streaming ``.awb`` archives. ``cue_name`` optionally restricts the
    output to a single decoded WAV track, matching the previous behaviour.

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
        outputs = cridecoder.decode_acb_to_wav(decode_path, target_dir, None) or []
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if cue_name is None:
        return list(outputs)

    kept: list[str] = []
    for output in outputs:
        if Path(output).stem == cue_name:
            kept.append(output)
        else:
            # Drop tracks that don't match the requested cue so they aren't left
            # behind on disk for later stages to pick up.
            try:
                os.remove(output)
            except OSError:
                pass
    return kept
