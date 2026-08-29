"""Tests for the in-process OpenDAL upload backend."""

import asyncio
from pathlib import Path

import pytest

from updater.security import SecurityError
from updater.storage.opendal import upload_to_storage_opendal


def _fs_storage(remote_root: Path, **extra) -> dict:
    return {
        "type": "normal",
        "backend": "opendal",
        "scheme": "fs",
        "options": {"root": str(remote_root)},
        **extra,
    }


def test_opendal_upload_preserves_relative_keys(tmp_path: Path) -> None:
    source_root = tmp_path / "extracted"
    (source_root / "nested").mkdir(parents=True)
    (source_root / "first.mp3").write_bytes(b"first")
    (source_root / "nested" / "second.webp").write_bytes(b"second")
    remote_root = tmp_path / "remote"
    remote_root.mkdir()

    asyncio.run(
        upload_to_storage_opendal(
            [source_root / "first.mp3", source_root / "nested" / "second.webp"],
            source_root,
            _fs_storage(remote_root),
        )
    )

    assert (remote_root / "first.mp3").read_bytes() == b"first"
    assert (remote_root / "nested" / "second.webp").read_bytes() == b"second"


def test_opendal_upload_applies_prefix(tmp_path: Path) -> None:
    source_root = tmp_path / "extracted"
    source_root.mkdir()
    (source_root / "asset.png").write_bytes(b"pixels")
    remote_root = tmp_path / "remote"
    remote_root.mkdir()

    asyncio.run(
        upload_to_storage_opendal(
            [source_root / "asset.png"],
            source_root,
            _fs_storage(remote_root, prefix="assets/jp"),
        )
    )

    assert (remote_root / "assets" / "jp" / "asset.png").read_bytes() == b"pixels"


def test_opendal_upload_streams_large_files(tmp_path: Path) -> None:
    source_root = tmp_path / "extracted"
    source_root.mkdir()
    payload = bytes(range(256)) * (5 * 1024 * 1024 // 256)  # 5MB, above one chunk
    (source_root / "movie.mp4").write_bytes(payload)
    remote_root = tmp_path / "remote"
    remote_root.mkdir()

    asyncio.run(
        upload_to_storage_opendal(
            [source_root / "movie.mp4"],
            source_root,
            _fs_storage(remote_root),
        )
    )

    assert (remote_root / "movie.mp4").read_bytes() == payload


def test_opendal_upload_rejects_outside_source_before_writing(tmp_path: Path) -> None:
    source_root = tmp_path / "extracted"
    source_root.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    remote_root = tmp_path / "remote"
    remote_root.mkdir()

    operation = upload_to_storage_opendal([outside], source_root, _fs_storage(remote_root))
    with pytest.raises(SecurityError):
        asyncio.run(operation)
    assert list(remote_root.iterdir()) == []


def test_opendal_upload_requires_valid_scheme_and_options(tmp_path: Path) -> None:
    source_root = tmp_path / "extracted"
    source_root.mkdir()
    (source_root / "asset.png").write_bytes(b"pixels")

    invalid_scheme = upload_to_storage_opendal(
        [source_root / "asset.png"],
        source_root,
        {"backend": "opendal", "options": {}},
    )
    with pytest.raises(ValueError, match="scheme"):
        asyncio.run(invalid_scheme)
    invalid_options = upload_to_storage_opendal(
        [source_root / "asset.png"],
        source_root,
        {"backend": "opendal", "scheme": "fs", "options": {"root": 5}},
    )
    with pytest.raises(ValueError, match="options"):
        asyncio.run(invalid_options)


def test_opendal_upload_aggregates_failures(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "extracted"
    source_root.mkdir()
    (source_root / "a.bin").write_bytes(b"a")
    (source_root / "b.bin").write_bytes(b"b")

    import opendal

    class _FailingOperator:
        def layer(self, _layer):
            return self

        async def open(self, _key, _mode):
            raise RuntimeError("remote unavailable")

    monkeypatch.setattr(opendal, "AsyncOperator", lambda *_a, **_k: _FailingOperator())

    operation = upload_to_storage_opendal(
        [source_root / "a.bin", source_root / "b.bin"],
        source_root,
        _fs_storage(tmp_path / "remote"),
    )
    with pytest.raises(RuntimeError, match=r"2 upload\(s\) failed"):
        asyncio.run(operation)
