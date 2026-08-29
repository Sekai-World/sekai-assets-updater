"""Shared upload-source validation and remote object-key derivation."""

import os
from typing import List

from anyio import Path

from updater.security import derive_remote_key, validate_contained_file


def derive_storage_remote_path(remote_base: str, relative_key: str) -> str:
    """Append one validated object key to an opaque configured storage target.

    ``remote_base`` is an rclone destination, not a local filesystem path.  It
    is therefore deliberately preserved byte-for-byte (apart from choosing the
    separator when it has no trailing slash), which supports named remotes,
    local absolute-path remotes, and on-the-fly remote syntax.  Only
    ``relative_key`` is parsed and validated; it must be a normalized POSIX
    path relative to the extraction root.
    """
    if not isinstance(remote_base, str):
        raise TypeError("remote_base must be a text storage target")
    if "\x00" in remote_base:
        raise ValueError("remote_base contains a NUL byte")

    remote_key = derive_remote_key(relative_key)
    separator = "" if remote_base.endswith("/") else "/"
    return f"{remote_base}{separator}{remote_key}"


def validate_upload_sources(
    exported_list: List[Path],
    extracted_save_path: Path,
) -> tuple[str, list[tuple[object, str]]]:
    """Validate every source file is contained below the extraction root.

    Returns the absolute root and ``(validated_path, relative_key)`` pairs,
    where ``relative_key`` is the normalized POSIX path below the root.
    """
    root_path = os.path.abspath(os.fspath(extracted_save_path))
    validated_sources: list[tuple[object, str]] = []
    for file_path in exported_list:
        source_path = os.path.abspath(os.fspath(file_path))
        relative_path = os.path.relpath(source_path, root_path)
        relative_key = relative_path.replace(os.sep, "/")
        validated_path = validate_contained_file(root_path, relative_key)
        validated_sources.append((validated_path, relative_key))
    return root_path, validated_sources
