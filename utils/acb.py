"""ACB/AWB track extraction backed by the native cridecoder library.

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

__all__ = ["extract_acb"]


def extract_acb(
    acb_file: Union[BytesIO, BufferedReader],
    target_dir: str,
    acb_file_path: str,
    cue_name: Optional[str] = None,
) -> list[str]:
    """Extract audio tracks from an ACB to ``target_dir``.

    ``acb_file`` is an open binary stream of the ACB; ``acb_file_path`` is the
    path the ACB logically lives at — its parent directory is used to resolve
    external streaming ``.awb`` archives. ``cue_name`` optionally restricts the
    output to a single track (by cue name), matching the previous behaviour.

    Returns the list of written track file paths.
    """
    acb_file.seek(0)
    acb_bytes = acb_file.read()

    # cridecoder needs an on-disk path, and resolves external ".awb" archives
    # relative to the ACB's directory. Stage the bytes next to acb_file_path so
    # sibling streaming AWBs are found.
    parent = os.path.dirname(acb_file_path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".acb", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(acb_bytes)
        outputs = cridecoder.extract_acb(tmp_path, target_dir) or []
    finally:
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
