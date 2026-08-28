from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from updater import security
from updater.bundle import pipeline as bundle


def test_resolve_secure_path_accepts_nested_relative_path(tmp_path: Path) -> None:
    assert security.resolve_secure_path(tmp_path, "assets/music/song.bytes") == (
        tmp_path / "assets" / "music" / "song.bytes"
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        ".",
        "..",
        "../outside",
        "assets/../outside",
        "/absolute",
        "assets//song",
        "assets/",
        "assets\\song",
        "C:/outside",
    ],
)
def test_resolve_secure_path_rejects_unsafe_relative_paths(
    tmp_path: Path, relative_path: str
) -> None:
    with pytest.raises(security.SecurityError):
        security.resolve_secure_path(tmp_path, relative_path)


def test_resolve_secure_path_rejects_existing_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(security.SecurityError):
        security.resolve_secure_path(tmp_path, "link/secret.txt")


def test_resolve_secure_path_checks_intermediate_repeated_component(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"not a directory")

    with pytest.raises(NotADirectoryError):
        security.resolve_secure_path(tmp_path, "a/b/a")


def test_resolve_secure_path_rejects_symlinked_root_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    root = real_parent / "root"
    root.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(security.SecurityError):
        security.resolve_secure_path(linked_parent / "root", "asset.bytes")


def test_validate_contained_file_requires_existing_regular_file(tmp_path: Path) -> None:
    file_path = tmp_path / "asset.bytes"
    file_path.write_bytes(b"asset")

    assert security.validate_contained_file(tmp_path, "asset.bytes") == file_path
    with pytest.raises(FileNotFoundError):
        security.validate_contained_file(tmp_path, "missing.bytes")

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ValueError):
        security.validate_contained_file(tmp_path, "directory")


def test_validate_contained_file_rejects_symlink_file(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-file"
    outside.write_bytes(b"outside")
    (tmp_path / "asset.bytes").symlink_to(outside)

    with pytest.raises(security.SecurityError):
        security.validate_contained_file(tmp_path, "asset.bytes")


@pytest.mark.parametrize(
    ("relative_path", "prefix", "expected"),
    [
        ("music/song.mp3", "assets", "assets/music/song.mp3"),
        ("music/song.mp3", "assets/", "assets/music/song.mp3"),
        ("music/song.mp3", "", "music/song.mp3"),
    ],
)
def test_derive_remote_key_validates_and_joins_posix_paths(
    relative_path: str, prefix: str, expected: str
) -> None:
    assert security.derive_remote_key(relative_path, prefix) == expected


@pytest.mark.parametrize("value", ["/remote", "../remote", "remote/../other", "remote\\other", "."])
def test_derive_remote_key_rejects_prefix_escape(value: str) -> None:
    with pytest.raises(security.SecurityError):
        security.derive_remote_key("music/song.mp3", value)


def test_atomic_write_bytes_replaces_target_without_leaking_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "payload.bin"
    target.write_bytes(b"old")

    assert security.atomic_write_bytes(target, b"new") == target
    assert target.read_bytes() == b"new"
    assert list(tmp_path.glob(".payload.bin.*.tmp")) == []


def test_atomic_write_bytes_cleans_temp_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "payload.bin"
    target.write_bytes(b"old")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(security.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        security.atomic_write_bytes(target, b"new")

    assert target.read_bytes() == b"old"
    assert list(tmp_path.glob(".payload.bin.*.tmp")) == []


def test_save_image_formats_uses_closed_race_safe_tempfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_path = tmp_path / "texture.asset"
    original_mkstemp = bundle.tempfile.mkstemp
    closed_descriptors: list[int] = []

    def record_mkstemp(*args, **kwargs):
        descriptor, temporary_name = original_mkstemp(*args, **kwargs)
        return descriptor, temporary_name

    original_fdopen = bundle.os.fdopen

    @contextmanager
    def record_fdopen(*args, **kwargs):
        descriptor = args[0]
        with original_fdopen(*args, **kwargs) as temporary_file:
            yield temporary_file
        closed_descriptors.append(descriptor)

    monkeypatch.setattr(bundle.tempfile, "mkstemp", record_mkstemp)
    monkeypatch.setattr(bundle.os, "fdopen", record_fdopen)
    monkeypatch.setattr(
        bundle.tempfile,
        "mktemp",
        lambda *args, **kwargs: pytest.fail("insecure tempfile.mktemp was called"),
    )

    with Image.new("RGBA", (1, 1), (255, 0, 0, 255)) as image:
        saved_paths = bundle._save_image_formats(image, save_path, ("PNG",))

    assert saved_paths == [tmp_path / "texture.PNG"]
    assert len(closed_descriptors) == 1
    assert saved_paths[0].read_bytes()
    assert list(tmp_path.glob(".texture.PNG.*.tmp")) == []
