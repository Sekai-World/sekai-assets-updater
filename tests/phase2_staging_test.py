from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio import Path as AnyioPath

from updater import worker
from updater.postprocess import dispatch


def _config(
    extracted_root: Path | None,
    bundle_cache_root: Path | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        ASSET_LOCAL_EXTRACTED_DIR=AnyioPath(extracted_root) if extracted_root else None,
        ASSET_LOCAL_BUNDLE_CACHE_DIR=(AnyioPath(bundle_cache_root) if bundle_cache_root else None),
        ASSET_REMOTE_STORAGE=[],
        UNITY_VERSION=None,
        MAX_CONCURRENCY_UPLOADS=1,
    )


def _run_extract_stage(
    artifact,
    config,
    monkeypatch,
    output_name: str,
    content: bytes | None = None,
    extract_calls: list[dict] | None = None,
):
    async def fake_extract(_bundle_path, _bundle, output_root, **kwargs):
        if extract_calls is not None:
            extract_calls.append(kwargs)
        output = output_root / output_name
        await output.parent.mkdir(parents=True, exist_ok=True)
        await output.write_bytes(content if content is not None else output_name.encode())
        return [output]

    monkeypatch.setattr(worker, "extract_asset_bundle", fake_extract)
    extract_queue: asyncio.Queue = asyncio.Queue()
    upload_queue: asyncio.Queue = asyncio.Queue()
    extract_queue.put_nowait(artifact)
    extract_queue.put_nowait(worker._QUEUE_SENTINEL)
    failures: list = []

    async def run() -> None:
        await worker._extract_stage(
            "phase2",
            "extract",
            extract_queue,
            upload_queue,
            config,
            failures,
            asyncio.Lock(),
        )

    asyncio.run(run())
    return upload_queue.get_nowait(), failures


def test_extract_stage_passes_configured_bundle_cache_root(tmp_path: Path, monkeypatch) -> None:
    cache_root = tmp_path / "bundle-cache"
    calls: list[dict] = []
    artifact = worker.PipelineArtifact(
        "a",
        {"bundleName": "music/song"},
        AnyioPath(tmp_path / "bundle"),
    )

    output_artifact, failures = _run_extract_stage(
        artifact,
        _config(None, cache_root),
        monkeypatch,
        "song.txt",
        extract_calls=calls,
    )

    assert not failures
    assert output_artifact.extracted_save_path is not None
    assert calls[0]["bundle_cache_root"] == AnyioPath(cache_root)
    assert calls[0]["bundle_cache_root"] != AnyioPath(cache_root / "music/song")


def test_extract_stage_passes_no_bundle_cache_root_without_cache_configuration(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[dict] = []
    artifact = worker.PipelineArtifact(
        "a",
        {"bundleName": "music/song"},
        AnyioPath(tmp_path / "bundle"),
    )

    output_artifact, failures = _run_extract_stage(
        artifact,
        _config(None),
        monkeypatch,
        "song.txt",
        extract_calls=calls,
    )

    assert not failures
    assert output_artifact.extracted_save_path is not None
    assert calls[0]["bundle_cache_root"] is None


def test_configured_bundle_outputs_use_distinct_retained_roots(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "extracted"
    first = worker.PipelineArtifact("a", {"bundleName": "music/song"}, AnyioPath(tmp_path / "a"))
    second = worker.PipelineArtifact("b", {"bundleName": "music/song"}, AnyioPath(tmp_path / "b"))

    first_out, first_failures = _run_extract_stage(first, _config(root), monkeypatch, "a.txt")
    second_out, second_failures = _run_extract_stage(second, _config(root), monkeypatch, "b.txt")

    assert not first_failures and not second_failures
    assert first_out.extracted_save_path != second_out.extracted_save_path
    assert (
        first_out.extracted_save_path.parent.parent == second_out.extracted_save_path.parent.parent
    )
    assert asyncio.run(first_out.extracted_save_path.exists())
    assert asyncio.run(second_out.extracted_save_path.exists())
    assert first_out.exported_list[0].parent == first_out.extracted_save_path
    assert second_out.exported_list[0].parent == second_out.extracted_save_path


def test_live2d_postprocess_outputs_use_the_shared_extraction_root(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "extracted"
    artifact = worker.PipelineArtifact(
        "a", {"bundleName": "live2d/model/base"}, AnyioPath(tmp_path / "bundle")
    )
    config = _config(root)
    config.UPDATER_MODE = "live2d"

    output_artifact, failures = _run_extract_stage(
        artifact,
        config,
        monkeypatch,
        "live2d/model/base/base.model3.json",
    )

    assert not failures
    assert output_artifact.extracted_save_path == AnyioPath(root)
    assert asyncio.run(AnyioPath(root / "live2d/model/base/base.model3.json").exists())


def test_chart_score_postprocess_outputs_use_the_shared_extraction_root(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "extracted"
    artifact = worker.PipelineArtifact(
        "a", {"bundleName": "music/music_score/001_song"}, AnyioPath(tmp_path / "bundle")
    )
    config = _config(root)
    config.UPDATER_MODE = "assets"
    config.ENABLE_CHARTS_POSTPROCESS = True

    output_artifact, failures = _run_extract_stage(
        artifact, config, monkeypatch, "music/music_score/001_song/master.txt"
    )

    assert not failures
    assert output_artifact.extracted_save_path == AnyioPath(root)


def test_specialized_bundle_stays_isolated_without_matching_postprocess(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "extracted"
    artifact = worker.PipelineArtifact(
        "a", {"bundleName": "music/music_score/001_song"}, AnyioPath(tmp_path / "bundle")
    )
    config = _config(root)

    output_artifact, failures = _run_extract_stage(artifact, config, monkeypatch, "score.txt")

    assert not failures
    assert output_artifact.extracted_save_path != AnyioPath(root)
    assert output_artifact.extracted_save_path.is_relative_to(AnyioPath(root))


def test_live2d_postprocess_uses_model_tree_from_pathlib_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "live2d-workspace"
    artifact = worker.PipelineArtifact(
        "a",
        {
            "bundleName": "live2d/model/08shizuku_cloth001",
            "paths": ["StartApp/live2d/model/v1/main/08_shizuku/08shizuku_cloth001"],
            "cacheFileName": "193307e96f564d5666c08b7cc218263d",
        },
        AnyioPath(tmp_path / "bundle"),
    )
    config = _config(root)
    config.ASSET_LOCAL_EXTRACTED_DIR = root
    config.UPDATER_MODE = "live2d"
    config.LIVE2D_BUNDLE_CACHE_DIR = tmp_path / "live2d-bundles"
    config.DL_LIST_CACHE_PATH = root / "cache" / "dl_list.json"
    (config.LIVE2D_BUNDLE_CACHE_DIR / "live2d" / "motion").mkdir(parents=True)
    restored: list[tuple[AnyioPath, AnyioPath, AnyioPath]] = []

    async def fake_restore(motion_root, motion_output, model_root, *_args, **_kwargs):
        restored.append((motion_root, motion_output, model_root))

    monkeypatch.setattr(dispatch, "restore_live2d_motions", fake_restore)
    _run_extract_stage(
        artifact,
        config,
        monkeypatch,
        "live2d/model/08_shizuku/08shizuku_cloth001.model3.json",
    )

    asyncio.run(dispatch.run_specialized_postprocess("live2d", config))

    assert restored == [
        (
            AnyioPath(tmp_path / "live2d-bundles/live2d/motion"),
            AnyioPath(root / "live2d/motion"),
            AnyioPath(root / "live2d/model"),
        )
    ]
    assert (root / "live2d/model/08_shizuku/08shizuku_cloth001.model3.json").is_file()


def test_configured_output_is_retained_and_upload_uses_only_its_root(
    tmp_path: Path, monkeypatch
) -> None:
    artifact = worker.PipelineArtifact(
        "a", {"bundleName": "music/song"}, AnyioPath(tmp_path / "bundle")
    )
    output_artifact, failures = _run_extract_stage(
        artifact, _config(tmp_path / "root"), monkeypatch, "song.txt"
    )
    seen: list[tuple[list[Path], Path]] = []

    async def fake_upload(files, root, *_args, **_kwargs):
        seen.append((files, root))

    monkeypatch.setattr(worker, "upload_to_storage", fake_upload)
    output_artifact.exported_list = [output_artifact.extracted_save_path / "song.txt"]
    asyncio.run(output_artifact.extracted_save_path.joinpath("song.txt").write_bytes(b"song"))
    output_artifact.bundle["remote"] = True
    config = _config(output_artifact.extracted_save_path.parent.parent.parent)
    config.ASSET_REMOTE_STORAGE = [{"type": "normal", "base": "x", "program": "p", "args": []}]
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(output_artifact)
    queue.put_nowait(worker._QUEUE_SENTINEL)

    async def run() -> None:
        await worker._upload_stage("phase2", "upload", queue, config, failures, asyncio.Lock())

    asyncio.run(run())
    assert seen == [(output_artifact.exported_list, output_artifact.extracted_save_path)]
    assert asyncio.run(output_artifact.extracted_save_path.exists())


def test_temporary_artifact_cleanup_is_scoped_to_its_own_stage(tmp_path: Path, monkeypatch) -> None:
    first = worker.PipelineArtifact("a", {"bundleName": "a"}, AnyioPath(tmp_path / "a"))
    second = worker.PipelineArtifact("b", {"bundleName": "b"}, AnyioPath(tmp_path / "b"))
    first_out, _ = _run_extract_stage(first, _config(None), monkeypatch, "a.txt")
    second_out, _ = _run_extract_stage(second, _config(None), monkeypatch, "b.txt")
    first_root = first_out.extracted_save_path
    second_root = second_out.extracted_save_path
    assert asyncio.run(first_root.exists()) and asyncio.run(second_root.exists())

    asyncio.run(worker._cleanup_artifact(first_out, remove_extracted=True))
    assert not asyncio.run(first_root.exists())
    assert asyncio.run(second_root.exists())


def test_upload_success_cleans_only_temporary_artifact_root(tmp_path: Path, monkeypatch) -> None:
    first = worker.PipelineArtifact("a", {"bundleName": "a"}, AnyioPath(tmp_path / "a"))
    second = worker.PipelineArtifact("b", {"bundleName": "b"}, AnyioPath(tmp_path / "b"))
    first_out, failures = _run_extract_stage(first, _config(None), monkeypatch, "same.txt")
    second_out, _ = _run_extract_stage(second, _config(None), monkeypatch, "same.txt")
    first_root = first_out.extracted_save_path
    second_root = second_out.extracted_save_path

    async def fake_upload(*_args, **_kwargs):
        return None

    monkeypatch.setattr(worker, "upload_to_storage", fake_upload)
    config = _config(None)
    config.ASSET_REMOTE_STORAGE = [{"type": "normal", "base": "x", "program": "p", "args": []}]
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(first_out)
    queue.put_nowait(worker._QUEUE_SENTINEL)

    async def run() -> None:
        await worker._upload_stage("phase2", "upload", queue, config, failures, asyncio.Lock())

    asyncio.run(run())
    assert not asyncio.run(first_root.exists())
    assert asyncio.run(second_root.exists())
    assert not failures


def test_upload_stage_isolates_same_relative_output_and_content(
    tmp_path: Path, monkeypatch
) -> None:
    first = worker.PipelineArtifact("a", {"bundleName": "a"}, AnyioPath(tmp_path / "a"))
    second = worker.PipelineArtifact("b", {"bundleName": "b"}, AnyioPath(tmp_path / "b"))
    first_out, failures = _run_extract_stage(
        first, _config(None), monkeypatch, "same.txt", b"first bytes"
    )
    second_out, second_failures = _run_extract_stage(
        second, _config(None), monkeypatch, "same.txt", b"second bytes"
    )
    expected = {
        first_out.extracted_save_path.name: b"first bytes",
        second_out.extracted_save_path.name: b"second bytes",
    }
    seen: list[tuple[Path, Path, bytes]] = []

    async def fake_upload(files, root, *_args, **_kwargs):
        assert len(files) == 1
        exported_file = files[0]
        assert await exported_file.resolve() == (await root.resolve()) / "same.txt"
        contents = await exported_file.read_bytes()
        assert root.name in expected
        assert contents == expected[root.name]
        seen.append((root, exported_file, contents))

    monkeypatch.setattr(worker, "upload_to_storage", fake_upload)
    config = _config(None)
    config.ASSET_REMOTE_STORAGE = [{"type": "normal", "base": "x", "program": "p", "args": []}]
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(first_out)
    queue.put_nowait(second_out)
    queue.put_nowait(worker._QUEUE_SENTINEL)

    async def run() -> None:
        await worker._upload_stage("phase2", "upload", queue, config, failures, asyncio.Lock())

    asyncio.run(run())
    assert not failures and not second_failures
    assert [(root, path.name, contents) for root, path, contents in seen] == [
        (first_out.extracted_save_path, "same.txt", b"first bytes"),
        (second_out.extracted_save_path, "same.txt", b"second bytes"),
    ]
    assert not asyncio.run(first_out.extracted_save_path.exists())
    assert not asyncio.run(second_out.extracted_save_path.exists())


def test_upload_failure_cleans_temporary_root_and_records_failure(
    tmp_path: Path, monkeypatch
) -> None:
    failing = worker.PipelineArtifact("a", {"bundleName": "a"}, AnyioPath(tmp_path / "a"))
    sibling = worker.PipelineArtifact("b", {"bundleName": "b"}, AnyioPath(tmp_path / "b"))
    failing_out, _ = _run_extract_stage(failing, _config(None), monkeypatch, "same.txt", b"failure")
    sibling_out, _ = _run_extract_stage(sibling, _config(None), monkeypatch, "same.txt", b"sibling")
    failing_root = failing_out.extracted_save_path
    sibling_root = sibling_out.extracted_save_path

    async def failing_upload(*_args, **_kwargs):
        root = _args[1]
        if root == failing_root:
            raise RuntimeError("upload failed")
        assert root == sibling_root
        assert await _args[0][0].read_bytes() == b"sibling"

    monkeypatch.setattr(worker, "upload_to_storage", failing_upload)
    config = _config(None)
    config.ASSET_REMOTE_STORAGE = [{"type": "normal", "base": "x", "program": "p", "args": []}]
    failures: list = []
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(failing_out)
    queue.put_nowait(sibling_out)
    queue.put_nowait(worker._QUEUE_SENTINEL)

    async def run() -> None:
        await worker._upload_stage("phase2", "upload", queue, config, failures, asyncio.Lock())

    asyncio.run(run())
    assert not asyncio.run(failing_root.exists())
    assert not asyncio.run(sibling_root.exists())
    assert failures == [("a", {"bundleName": "a"})]


def test_extraction_failure_cleans_temporary_root(tmp_path: Path, monkeypatch) -> None:
    failing = worker.PipelineArtifact("a", {"bundleName": "a"}, AnyioPath(tmp_path / "a"))
    sibling = worker.PipelineArtifact("b", {"bundleName": "b"}, AnyioPath(tmp_path / "b"))
    failing_root = None

    async def extract_with_one_failure(_bundle_path, bundle, output_root, **_kwargs):
        nonlocal failing_root
        output = output_root / "same.txt"
        await output.parent.mkdir(parents=True, exist_ok=True)
        if bundle["bundleName"] == "a":
            failing_root = output_root
            await output.write_bytes(b"partial failure")
            raise RuntimeError("extract failed")
        await output.write_bytes(b"sibling")
        return [output]

    monkeypatch.setattr(worker, "extract_asset_bundle", extract_with_one_failure)
    extract_queue: asyncio.Queue = asyncio.Queue()
    upload_queue: asyncio.Queue = asyncio.Queue()
    extract_queue.put_nowait(failing)
    extract_queue.put_nowait(sibling)
    extract_queue.put_nowait(worker._QUEUE_SENTINEL)
    failures: list = []

    async def run() -> None:
        await worker._extract_stage(
            "phase2",
            "extract",
            extract_queue,
            upload_queue,
            _config(None),
            failures,
            asyncio.Lock(),
        )

    asyncio.run(run())
    assert failures == [("a", {"bundleName": "a"})]
    assert failing_root is not None
    assert not asyncio.run(failing_root.exists())
    sibling_out = upload_queue.get_nowait()
    assert sibling_out.url == "b"
    assert asyncio.run(sibling_out.extracted_save_path.exists())
    assert (
        asyncio.run(sibling_out.extracted_save_path.joinpath("same.txt").read_bytes()) == b"sibling"
    )


def test_extract_cancellation_cleans_owned_temporary_artifacts(tmp_path: Path, monkeypatch) -> None:
    started = asyncio.Event()
    artifact = worker.PipelineArtifact("a", {"bundleName": "a"}, AnyioPath(tmp_path / "bundle"))
    artifact.remove_bundle_after_extract = True
    asyncio.run(artifact.bundle_save_path.write_bytes(b"bundle"))

    async def blocked_extract(_bundle_path, _bundle, output_root, **_kwargs):
        output = output_root / "partial.txt"
        await output.write_bytes(b"partial")
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "extract_asset_bundle", blocked_extract)
    extract_queue: asyncio.Queue = asyncio.Queue()
    upload_queue: asyncio.Queue = asyncio.Queue()
    extract_queue.put_nowait(artifact)

    async def run() -> None:
        task = asyncio.create_task(
            worker._extract_stage(
                "phase2", "extract", extract_queue, upload_queue, _config(None), [], asyncio.Lock()
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert artifact.extracted_save_path is not None
    assert not asyncio.run(artifact.extracted_save_path.exists())
    assert not asyncio.run(artifact.bundle_save_path.exists())
    assert upload_queue.empty()


def test_extract_cancellation_while_put_blocked_cleans_owned_root(
    tmp_path: Path, monkeypatch
) -> None:
    started = asyncio.Event()
    artifact = worker.PipelineArtifact("a", {"bundleName": "a"}, AnyioPath(tmp_path / "bundle"))
    artifact.remove_bundle_after_extract = True
    asyncio.run(artifact.bundle_save_path.write_bytes(b"bundle"))

    async def fake_extract(_bundle_path, _bundle, output_root, **_kwargs):
        output = output_root / "ready.txt"
        await output.write_bytes(b"ready")
        return [output]

    class BlockingQueue(asyncio.Queue):
        async def put(self, item):
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(worker, "extract_asset_bundle", fake_extract)
    extract_queue: asyncio.Queue = asyncio.Queue()
    upload_queue = BlockingQueue()
    extract_queue.put_nowait(artifact)

    async def run() -> None:
        task = asyncio.create_task(
            worker._extract_stage(
                "phase2", "extract", extract_queue, upload_queue, _config(None), [], asyncio.Lock()
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert artifact.extracted_save_path is not None
    assert not asyncio.run(artifact.extracted_save_path.exists())
    assert not asyncio.run(artifact.bundle_save_path.exists())
