#!/usr/bin/env python3
# usm.py: Extracting USM video (and audio) streams, backed by cridecoder.
#
# This module previously bundled a pure-Python USM demuxer. It is now a thin
# wrapper around cridecoder (Rust). The video pipeline uses cridecoder directly
# (see bundle.py); this wrapper is kept for the standalone CLI and any callers
# relying on the historical ``extract_usm`` entry point.

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Optional, Union

import cridecoder

__all__ = ["extract_usm"]


def extract_usm(
    usm: Union[bytes, str, os.PathLike[str]],
    target_dir: str,
    fallback_name: bytes = b"",
    *args,
    export_audio: bool = True,
) -> list[str]:
    """Demux a USM into its video (and optionally audio) streams via cridecoder.

    ``usm`` may be the raw USM bytes or a path to a ``.usm`` file. An optional
    decryption key may be passed positionally (``args[0]``) for encrypted USMs.
    Returns the list of written stream file paths.
    """
    key: Optional[int] = int(args[0]) if args else None

    tmp_path: Optional[str] = None
    try:
        if isinstance(usm, (bytes, bytearray)):
            fd, tmp_path = tempfile.mkstemp(suffix=".usm")
            with os.fdopen(fd, "wb") as fh:
                fh.write(usm)
            usm_path = tmp_path
        else:
            usm_path = os.fspath(usm)

        os.makedirs(target_dir, exist_ok=True)
        return list(cridecoder.extract_usm(usm_path, target_dir, key, export_audio))
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def main(invocation, usm_file, target_dir, *args):
    # args[0] = key (decimal)
    for output in extract_usm(Path(usm_file), target_dir, b"", *args):
        print(output)


if __name__ == "__main__":
    main(*sys.argv)
