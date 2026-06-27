"""HCA decoding backed by the native cridecoder library.

This module previously bundled a pure-Python port of vgmstream's clHCA
implementation. It is now a thin wrapper around cridecoder (Rust), keeping the
same public ``decode_hca_file`` signature so the rest of the codebase is
unchanged.
"""

from __future__ import annotations

import os

import cridecoder

__all__ = ["decode_hca_file", "decode_hca_to_wav_bytes"]


def decode_hca_file(
    input_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
) -> None:
    """Decode an HCA file to a WAV file using cridecoder.

    The Project Sekai HCA assets are unencrypted (cipher type 0/1), so no
    keycode is supplied.
    """
    cridecoder.decode_hca(os.fspath(input_path), os.fspath(output_path))


def decode_hca_to_wav_bytes(data: bytes) -> bytes:
    """Decode HCA bytes to WAV bytes in memory using cridecoder."""
    return cridecoder.decode_hca_bytes(data)
