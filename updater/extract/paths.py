"""Secure local path helpers for bundle extraction."""

import logging
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from updater.constants import (
    UNITY_FS_BUILT_IN_ALT_CONTAINER_BASE,
    UNITY_FS_BUILT_IN_CONTAINER_BASE,
    UNITY_FS_CONTAINER_BASE,
)
from updater.security import SecurityError, resolve_secure_path, validate_contained_file

logger = logging.getLogger("live2d")


def build_unityfs_save_path(unityfs_path: str, extracted_save_path: Path) -> Path:
    if not isinstance(unityfs_path, str) or not unityfs_path:
        raise ValueError(f"Invalid UnityFS path: {unityfs_path!r}")
    if "\\" in unityfs_path or "\x00" in unityfs_path:
        raise ValueError(f"Invalid UnityFS path: {unityfs_path!r}")

    source_path = PurePosixPath(unityfs_path)
    base_paths = (
        PurePosixPath(UNITY_FS_CONTAINER_BASE.as_posix()),
        PurePosixPath(UNITY_FS_BUILT_IN_CONTAINER_BASE.as_posix()),
        PurePosixPath(UNITY_FS_BUILT_IN_ALT_CONTAINER_BASE.as_posix()),
    )

    for index, base_path in enumerate(base_paths):
        prefix = f"{base_path.as_posix()}/"
        if not unityfs_path.startswith(prefix):
            continue

        raw_relative_path = unityfs_path[len(prefix) :]
        if not raw_relative_path or any(
            component in ("", ".", "..") for component in raw_relative_path.split("/")
        ):
            raise ValueError(f"Invalid UnityFS path: {unityfs_path!r}")

        try:
            relpath = source_path.relative_to(base_path)
        except ValueError:
            continue

        if index == 0:
            relpath = PurePosixPath(*relpath.parts[1:])
        return Path(resolve_secure_path(extracted_save_path, relpath.as_posix()).as_posix())

    raise ValueError(f"Failed to get relative path for {unityfs_path}")


def resolve_generated_child_path(root: Path, name: str, suffix: str = "") -> Path:
    """Resolve an untrusted generated filename below a local extraction root."""
    return Path(resolve_secure_path(root, f"{name}{suffix}").as_posix())


def canonical_root(path: Path) -> Path:
    return path.resolve(strict=False)


def replace_suffix_secure(root: Path, name: str, suffix: str) -> Path:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise SecurityError("generated name must be a non-empty filename")
    if "/" in name or "\\" in name or name in (".", ".."):
        raise SecurityError("generated name must be a single relative filename")
    return resolve_generated_child_path(root, Path(name).with_suffix(suffix).name)


def stream_files(paths: list[Path], chunk_size: int = 1024 * 1024):
    with ExitStack() as stack:
        files = [stack.enter_context(path.open("rb")) for path in paths]
        for file in files:
            while chunk := file.read(chunk_size):
                yield chunk


def discard_exported_file(exported_files: list[Path], file_path: Path) -> None:
    try:
        exported_files.remove(file_path)
        return
    except ValueError:
        pass

    file_path_lower = file_path.with_name(file_path.name.lower())
    try:
        exported_files.remove(file_path_lower)
    except ValueError:
        logger.debug("%s not tracked in exported_files, skip removal", file_path)


def resolve_existing_path(
    expected_path: Path,
    save_dir: Path,
    expected_suffix: str | None = None,
) -> Path:
    try:
        relative_path = expected_path.relative_to(save_dir).as_posix()
    except ValueError as exc:
        raise ValueError(f"Expected path is outside save directory: {expected_path}") from exc
    expected_path = Path(resolve_secure_path(save_dir, relative_path).as_posix())

    if expected_path.exists():
        return validate_contained_file(save_dir, relative_path)

    expected_name_lower = expected_path.name.lower()
    expected_path_lower = expected_path.with_name(expected_name_lower)
    if expected_path_lower.exists():
        logger.debug("Found %s instead of %s", expected_path_lower, expected_path.name)
        return validate_contained_file(
            save_dir, expected_path_lower.relative_to(save_dir).as_posix()
        )

    candidate_paths = [
        path
        for path in save_dir.iterdir()
        if path.name.lower() == expected_name_lower
        and (expected_suffix is None or path.suffix.lower() == expected_suffix.lower())
        and path.is_file()
        and not path.is_symlink()
    ]
    if len(candidate_paths) == 1:
        logger.debug(
            "Found %s instead of %s via case-insensitive lookup",
            candidate_paths[0],
            expected_path.name,
        )
        return validate_contained_file(
            save_dir, candidate_paths[0].relative_to(save_dir).as_posix()
        )

    raise FileNotFoundError(f"{expected_path} not found in {save_dir}")


def resolve_shared_audio_outputs(
    output_root: Path, save_dir: Path, cue_sheet_name: str
) -> list[Path]:
    expected_names = {
        f"{cue_sheet_name}{suffix}".lower() for suffix in (".wav", ".mp3", ".flac", ".hca")
    }
    return [
        validate_contained_file(output_root, path.relative_to(output_root).as_posix())
        for path in output_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.parent != save_dir
        and path.name.lower() in expected_names
    ]


def resolve_local_audio_outputs(save_dir: Path, cue_sheet_name: str) -> list[Path]:
    expected_names = {
        f"{cue_sheet_name}{suffix}".lower() for suffix in (".wav", ".mp3", ".flac", ".hca")
    }
    return [
        path
        for path in save_dir.iterdir()
        if path.is_file() and not path.is_symlink() and path.name.lower() in expected_names
    ]


def resolve_existing_usm_path(expected_path: Path, save_dir: Path) -> Path:
    try:
        return resolve_existing_path(expected_path, save_dir, ".usm")
    except FileNotFoundError:
        pass

    candidate_paths = [
        path
        for path in save_dir.iterdir()
        if path.suffix.lower() == ".usm" and path.is_file() and not path.is_symlink()
    ]
    if len(candidate_paths) == 1:
        logger.warning(
            "Expected %s in %s, falling back to discovered usm %s",
            expected_path.name,
            save_dir,
            candidate_paths[0].name,
        )
        return validate_contained_file(
            save_dir, candidate_paths[0].relative_to(save_dir).as_posix()
        )

    raise FileNotFoundError(f"{expected_path} not found in {save_dir}")
