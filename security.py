"""Small security primitives for filesystem and remote-path boundaries."""

from __future__ import annotations

import ntpath
import os
import stat
import tempfile
from pathlib import Path
from typing import TypeAlias


PathLike: TypeAlias = str | os.PathLike[str]


class SecurityError(ValueError):
    """Raised when an untrusted path cannot be safely used."""


def _path_text(value: PathLike, *, label: str) -> str:
    try:
        text = os.fspath(value)
    except TypeError as exc:
        raise SecurityError(f"{label} must be a path-like string") from exc

    if not isinstance(text, str):
        raise SecurityError(f"{label} must be a text path")
    if "\x00" in text:
        raise SecurityError(f"{label} contains a NUL byte")
    return text


def _validate_relative_path(value: PathLike, *, label: str) -> str:
    text = _path_text(value, label=label)
    if not text:
        raise SecurityError(f"{label} must not be empty")
    if "\\" in text:
        raise SecurityError(f"{label} contains a backslash separator")
    if text.startswith("/") or ntpath.isabs(text) or ntpath.splitdrive(text)[0]:
        raise SecurityError(f"{label} must be relative")

    components = text.split("/")
    if any(component in ("", ".", "..") for component in components):
        raise SecurityError(f"{label} contains an invalid path component")
    return "/".join(components)


def _validate_remote_prefix(prefix: PathLike) -> str:
    text = _path_text(prefix, label="remote_prefix")
    if not text:
        return ""

    # A trailing slash is presentation, not a path component.  Strip it only
    # after checking that the prefix is otherwise a valid relative POSIX path.
    while text.endswith("/"):
        text = text[:-1]
    if not text:
        raise SecurityError("remote_prefix must not be an absolute path")
    return _validate_relative_path(text, label="remote_prefix")


def _reject_existing_symlinks(path: Path) -> None:
    """Reject symlinks in the existing portion of an absolute local path."""

    absolute_path = path.absolute()
    current = Path(absolute_path.anchor)
    for component in absolute_path.parts[1:]:
        current /= component
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise SecurityError(f"symlink traversal is not allowed: {current}")


def resolve_secure_path(root: PathLike, relative_path: PathLike) -> Path:
    """Resolve a safe relative path below an existing, non-symlink root.

    Both POSIX ``/`` and local path inputs are accepted, while backslashes,
    drive-qualified paths, empty components, and dot components are rejected
    rather than interpreted differently on another platform.  Existing
    symlinks in the root or any existing path component are rejected.
    """

    root_path = Path(_path_text(root, label="root")).absolute()
    _reject_existing_symlinks(root_path.parent)
    try:
        root_mode = os.lstat(root_path).st_mode
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"root directory does not exist: {root_path}") from exc
    if stat.S_ISLNK(root_mode):
        raise SecurityError(f"root directory must not be a symlink: {root_path}")
    if not stat.S_ISDIR(root_mode):
        raise NotADirectoryError(f"root is not a directory: {root_path}")

    relative_text = _validate_relative_path(relative_path, label="relative_path")
    candidate = root_path
    for component in relative_text.split("/"):
        candidate /= component
        try:
            mode = os.lstat(candidate).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise SecurityError(f"symlink traversal is not allowed: {candidate}")
        if candidate != root_path and component != relative_text.split("/")[-1]:
            if not stat.S_ISDIR(mode):
                raise NotADirectoryError(f"path component is not a directory: {candidate}")

    return candidate


def prepare_secure_directory(root: PathLike) -> Path:
    """Validate ancestors, then create and return a local directory.

    This checks the complete existing ancestor chain before ``mkdir`` creates
    any missing component.  It protects against pre-existing symlink
    traversal; it does not claim to close races with concurrent filesystem
    mutation.
    """

    root_path = Path(_path_text(root, label="root")).absolute()
    _reject_existing_symlinks(root_path)
    root_path.mkdir(parents=True, exist_ok=True)
    _reject_existing_symlinks(root_path)
    try:
        mode = os.lstat(root_path).st_mode
    except FileNotFoundError as exc:  # pragma: no cover - mkdir succeeded or raised
        raise FileNotFoundError(f"directory was not created: {root_path}") from exc
    if stat.S_ISLNK(mode):
        raise SecurityError(f"directory must not be a symlink: {root_path}")
    if not stat.S_ISDIR(mode):
        raise NotADirectoryError(f"path is not a directory: {root_path}")
    return root_path


def secure_existing_output(root: PathLike, output: PathLike) -> Path:
    """Validate an existing output as a regular non-symlink file below root."""

    root_path = Path(_path_text(root, label="root")).absolute()
    output_path = Path(_path_text(output, label="output")).absolute()
    return validate_contained_file(
        root_path,
        output_path.relative_to(root_path).as_posix(),
    )


def validate_output_target(root: PathLike, output: PathLike) -> Path:
    """Validate a not-yet-written file target below a trusted artifact root."""

    root_path = Path(_path_text(root, label="root")).absolute()
    output_path = Path(_path_text(output, label="output")).absolute()
    relative_path = output_path.relative_to(root_path).as_posix()
    canonical_root = root_path.resolve(strict=False)
    resolve_secure_path(canonical_root, relative_path)
    try:
        if stat.S_ISLNK(os.lstat(output_path).st_mode):
            raise SecurityError(f"output must not be a symlink: {output_path}")
    except FileNotFoundError:
        pass
    return output_path


def validate_contained_file(root: PathLike, relative_path: PathLike) -> Path:
    """Return an existing regular file safely contained below ``root``."""

    candidate = resolve_secure_path(root, relative_path)
    try:
        mode = os.lstat(candidate).st_mode
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"contained file does not exist: {candidate}") from exc
    if stat.S_ISLNK(mode):
        raise SecurityError(f"contained file must not be a symlink: {candidate}")
    if not stat.S_ISREG(mode):
        raise ValueError(f"contained path is not a regular file: {candidate}")
    return candidate


def derive_remote_key(relative_path: PathLike, remote_prefix: PathLike = "") -> str:
    """Return a validated POSIX key for ``relative_path`` under a prefix."""

    relative_text = _validate_relative_path(relative_path, label="relative_path")
    prefix_text = _validate_remote_prefix(remote_prefix)
    return f"{prefix_text}/{relative_text}" if prefix_text else relative_text


def atomic_write_bytes(target_path: PathLike, data: bytes) -> Path:
    """Atomically replace ``target_path`` with ``data``.

    The temporary file is created in the destination directory, so ``os.replace``
    remains atomic on the same filesystem.  Existing symlink components and a
    symlink destination are rejected, and temporary files are removed on every
    success or failure path.
    """

    target = Path(_path_text(target_path, label="target_path")).absolute()
    if target.name in ("", ".", ".."):
        raise SecurityError("target_path must name a file")
    _reject_existing_symlinks(target.parent)
    if not target.parent.is_dir():
        raise NotADirectoryError(f"target directory does not exist: {target.parent}")
    try:
        target_mode = os.lstat(target).st_mode
    except FileNotFoundError:
        target_mode = None
    if target_mode is not None and stat.S_ISLNK(target_mode):
        raise SecurityError(f"target_path must not be a symlink: {target}")

    if not isinstance(data, bytes):
        raise TypeError("data must be bytes")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        return target
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def atomic_write_stream(target_path: PathLike, chunks) -> Path:
    """Atomically write an iterable of byte chunks without buffering it."""

    target = Path(_path_text(target_path, label="target_path")).absolute()
    _reject_existing_symlinks(target.parent)
    if not target.parent.is_dir():
        raise NotADirectoryError(f"target directory does not exist: {target.parent}")
    try:
        if stat.S_ISLNK(os.lstat(target).st_mode):
            raise SecurityError(f"target_path must not be a symlink: {target}")
    except FileNotFoundError:
        pass

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("stream chunks must be bytes")
                temporary_file.write(chunk)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
        return target
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
