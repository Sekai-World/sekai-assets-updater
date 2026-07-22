from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from anyio import Path as AnyioPath

import bundle
import security
import worker


def test_unityfs_mapping_rejects_absolute_traversal_and_backslash_names(tmp_path: Path) -> None:
    bad_paths = (
        "/outside.asset",
        "assets/sekai/assetbundle/resources/../outside.asset",
        "assets/sekai/assetbundle/resources/sub\\outside.asset",
    )
    for unityfs_path in bad_paths:
        with pytest.raises((ValueError, security.SecurityError)):
            bundle._build_unityfs_save_path(unityfs_path, tmp_path)


def test_unityfs_mapping_rejects_precreated_extraction_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(security.SecurityError):
        bundle._build_unityfs_save_path(
            "assets/sekai/assetbundle/resources/characters/link/asset.bytes",
            tmp_path,
        )


def test_audio_clip_sample_names_are_contained_and_reject_escape(tmp_path: Path) -> None:
    assert bundle._resolve_generated_child_path(tmp_path, "voice.wav") == tmp_path / "voice.wav"
    for filename in ("/outside.wav", "../outside.wav", "voice\\outside.wav"):
        with pytest.raises(security.SecurityError):
            bundle._resolve_generated_child_path(tmp_path, filename)


def test_acb_cue_and_textasset_names_are_contained(tmp_path: Path) -> None:
    assert bundle._resolve_generated_child_path(tmp_path, "voice", ".acb") == (
        tmp_path / "voice.acb"
    )
    for cue_name in ("../escape", "/escape", "cue\\escape"):
        with pytest.raises(security.SecurityError):
            bundle._resolve_generated_child_path(tmp_path, cue_name, ".acb")
    with pytest.raises(security.SecurityError):
        bundle._resolve_generated_child_path(tmp_path, "../textasset.bytes")


def test_usm_expected_and_fallback_paths_are_contained(tmp_path: Path) -> None:
    expected = tmp_path / "movie.usm"
    expected.write_bytes(b"usm")
    assert bundle._resolve_existing_usm_path_sync(expected, tmp_path) == expected

    fallback = tmp_path / "actual.usm"
    fallback.write_bytes(b"usm")
    expected.unlink()
    assert bundle._resolve_existing_usm_path_sync(expected, tmp_path) == fallback

    escape = tmp_path / "link.usm"
    escape.symlink_to(tmp_path.parent / "outside.usm")
    with pytest.raises(security.SecurityError):
        bundle._resolve_existing_usm_path_sync(escape, tmp_path)


def test_worker_rejects_bundle_name_before_download_and_cache_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "bundle-cache"
    cache_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (cache_root / "escape").symlink_to(outside, target_is_directory=True)

    download_mock = AsyncMock()
    monkeypatch.setattr(worker, "download_deobfuscate_bundle", download_mock)
    config = SimpleNamespace(
        ASSET_LOCAL_BUNDLE_CACHE_DIR=AnyioPath(cache_root),
        ASSET_LOCAL_EXTRACTED_DIR=None,
    )
    failed_tasks: list = []
    input_queue: asyncio.Queue = asyncio.Queue()
    extract_queue: asyncio.Queue = asyncio.Queue()
    input_queue.put_nowait(("http://example.test/bundle", {"bundleName": "escape/file"}))
    input_queue.put_nowait(worker._QUEUE_SENTINEL)

    async def run() -> None:
        await worker._download_stage(
            "test", "download", input_queue, extract_queue, config, {}, None,
            failed_tasks, asyncio.Lock(), None, AsyncMock()
        )

    asyncio.run(run())
    download_mock.assert_not_awaited()
    assert len(failed_tasks) == 1
    assert not (outside / "file").exists()


def test_worker_rejects_precreated_extraction_root_symlink(tmp_path: Path) -> None:
    real_root = tmp_path / "real-extracted"
    real_root.mkdir()
    linked_root = tmp_path / "extracted"
    linked_root.symlink_to(real_root, target_is_directory=True)
    config = SimpleNamespace(
        ASSET_LOCAL_BUNDLE_CACHE_DIR=None,
        ASSET_LOCAL_EXTRACTED_DIR=AnyioPath(linked_root),
        UNITY_VERSION=None,
    )
    artifact = worker.PipelineArtifact(
        "http://example.test/bundle", {"bundleName": "music/example"},
        AnyioPath(tmp_path / "bundle")
    )
    extract_queue: asyncio.Queue = asyncio.Queue()
    upload_queue: asyncio.Queue = asyncio.Queue()
    extract_queue.put_nowait(artifact)
    extract_queue.put_nowait(worker._QUEUE_SENTINEL)
    failed_tasks: list = []

    async def run() -> None:
        await worker._extract_stage(
            "test", "extract", extract_queue, upload_queue, config,
            failed_tasks, asyncio.Lock()
        )

    asyncio.run(run())
    assert len(failed_tasks) == 1
    assert not (real_root / "music").exists()


def test_worker_disk_gate_supports_temporary_bundle_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    download_mock = AsyncMock()
    monkeypatch.setattr(worker, "download_deobfuscate_bundle", download_mock)

    class Gate:
        def reserve(self, _size, _label):
            class Reservation:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_args):
                    return False

            return Reservation()

    config = SimpleNamespace(ASSET_LOCAL_BUNDLE_CACHE_DIR=None)
    input_queue: asyncio.Queue = asyncio.Queue()
    extract_queue: asyncio.Queue = asyncio.Queue()
    item = ("http://example.test/bundle", {"bundleName": "music/example"})
    input_queue.put_nowait(item)
    input_queue.put_nowait(worker._QUEUE_SENTINEL)
    failed_tasks: list = []

    async def run() -> None:
        await worker._download_stage(
            "test", "download", input_queue, extract_queue, config, {}, None,
            failed_tasks, asyncio.Lock(), Gate(), AsyncMock()
        )

    asyncio.run(run())
    download_mock.assert_awaited_once()
    _, root, relative, *_ = download_mock.await_args.args
    assert Path(root).is_dir()
    assert relative.startswith("tmp")
    assert Path(root) / relative == Path(root) / relative
    assert not failed_tasks
