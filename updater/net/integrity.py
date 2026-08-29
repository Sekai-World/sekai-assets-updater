"""Integrity checks for downloaded Unity asset bundles."""

import struct
from pathlib import Path


class DownloadIntegrityError(ValueError):
    """Raised when a downloaded bundle fails its wire or content checks."""


class RetryableDownloadError(DownloadIntegrityError):
    """A transient download failure that may be retried."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


_UNITYFS_SIGNATURE = b"UnityFS\0"
_UNITYFS_FIELD_LIMIT = 1024
_UNITYFS_FIXED_HEADER_SIZE = 8 + 4 + 8 + 8 + 8 + 4 + 4 + 4


def validate_unityfs_bundle(path: Path, stored_bytes: int) -> None:
    """Validate the bounded UnityFS header and its declared file offsets."""
    with path.open("rb") as stream:
        header = stream.read(_UNITYFS_FIXED_HEADER_SIZE + 2 * _UNITYFS_FIELD_LIMIT)
    if len(header) < len(_UNITYFS_SIGNATURE) or not header.startswith(_UNITYFS_SIGNATURE):
        raise DownloadIntegrityError("stored bundle does not begin with UnityFS")

    offset = len(_UNITYFS_SIGNATURE)
    if len(header) < offset + 4:
        raise DownloadIntegrityError("incomplete UnityFS format header")
    format_version = struct.unpack_from(">I", header, offset)[0]
    offset += 4
    if format_version == 0:
        raise DownloadIntegrityError("invalid UnityFS format version")

    for field_name in ("unity version", "revision"):
        end = header.find(b"\0", offset, offset + _UNITYFS_FIELD_LIMIT + 1)
        if end < 0 or end == offset + _UNITYFS_FIELD_LIMIT:
            raise DownloadIntegrityError(f"incomplete UnityFS {field_name} field")
        if end == offset:
            raise DownloadIntegrityError(f"empty UnityFS {field_name} field")
        offset = end + 1

    if len(header) < offset + 20:
        raise DownloadIntegrityError("incomplete UnityFS fixed header")
    declared_size, compressed_info_size, _uncompressed_info_size, _flags = struct.unpack_from(
        ">QIII", header, offset
    )
    offset += 20
    if declared_size != stored_bytes or declared_size < offset:
        raise DownloadIntegrityError("UnityFS declared file size is invalid")
    if compressed_info_size > declared_size - offset:
        raise DownloadIntegrityError("UnityFS block info offset is invalid")
