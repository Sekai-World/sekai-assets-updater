from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio import Path as AnyioPath
from conftest import pipeline_config

from updater import pipeline
from updater.cli import lifecycle
from updater.modes import retains_live2d_extracted_outputs


def _workspace_config(root: Path):
    config = pipeline_config(root)
    config.UPDATER_MODE = "assets"
    config.ENABLE_LIVE2D_POSTPROCESS = True
    config.ENABLE_CHARTS_POSTPROCESS = True
    return config


def _install_workspace_fakes(
    monkeypatch,
    *,
    fail_once_for: str | None = None,
    blocked_bundle: str | None = None,
    upload_started: asyncio.Event | None = None,
):
    uploads: list[tuple[str, Path, Path]] = []
    failure = {"bundle": fail_once_for}

    async def fake_download(_url, root, relative, **_kwargs):
        await root.joinpath(relative).write_bytes(b"synthetic bundle")

    async def fake_extract(_bundle_path, bundle, output_root, **_kwargs):
        bundle_name = bundle["bundleName"]
        output = output_root / bundle_name / "source.bin"
        await output.parent.mkdir(parents=True, exist_ok=True)
        await output.write_bytes(bundle_name.encode())
        return [output]

    async def fake_upload(files, root, *_args, **_kwargs):
        assert len(files) == 1
        exported_file = files[0]
        bundle_name = (await exported_file.read_bytes()).decode()
        uploads.append(
            (
                bundle_name,
                Path((await root.resolve()).as_posix()),
                Path((await exported_file.resolve()).as_posix()),
            )
        )
        if bundle_name == blocked_bundle:
            assert upload_started is not None
            upload_started.set()
            await asyncio.Event().wait()
        if bundle_name == failure["bundle"]:
            failure["bundle"] = None
            raise RuntimeError("synthetic upload failure")

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(pipeline, "upload_to_storage", fake_upload)
    return uploads


def test_temporary_shared_workspace_cleans_only_non_aggregate_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "automatic-workspace"
    config = _workspace_config(root)
    assert retains_live2d_extracted_outputs(config)
    assert pipeline._uses_aggregate_workspace({"bundleName": "live2d/model/base"}, config)
    assert pipeline._uses_aggregate_workspace({"bundleName": "music/music_score/001_song"}, config)
    assert not pipeline._uses_aggregate_workspace({"bundleName": "music/background"}, config)
    uploads = _install_workspace_fakes(monkeypatch)
    items = [
        ("ordinary-url", {"bundleName": "music/background"}),
        ("live2d-url", {"bundleName": "live2d/model/base"}),
        ("chart-url", {"bundleName": "music/music_score/001_song"}),
    ]

    failed = asyncio.run(pipeline.run_pipeline(items, config, {}, extracted_dir_is_temporary=True))

    assert failed == []
    assert {name for name, _root, _file in uploads} == {
        "music/background",
        "live2d/model/base",
        "music/music_score/001_song",
    }
    upload_roots = {name: upload_root for name, upload_root, _file in uploads}
    upload_files = {name: exported_file for name, _root, exported_file in uploads}
    assert upload_roots["music/background"] != root.resolve()
    assert not upload_roots["music/background"].exists()
    for aggregate_bundle in ("live2d/model/base", "music/music_score/001_song"):
        assert upload_roots[aggregate_bundle] == root.resolve()
        assert upload_files[aggregate_bundle].is_file()
        assert upload_files[aggregate_bundle].read_bytes() == aggregate_bundle.encode()
    assert root.is_dir()


def test_persistent_extraction_root_keeps_independent_bundle_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "persistent-extraction"
    config = _workspace_config(root)
    uploads = _install_workspace_fakes(monkeypatch)
    items = [
        ("ordinary-url", {"bundleName": "music/background"}),
        ("live2d-url", {"bundleName": "live2d/model/base"}),
    ]

    failed = asyncio.run(pipeline.run_pipeline(items, config, {}))

    assert failed == []
    ordinary_root = next(
        upload_root for name, upload_root, _ in uploads if name == "music/background"
    )
    ordinary_file = next(exported for name, _, exported in uploads if name == "music/background")
    assert ordinary_root != root.resolve()
    assert ordinary_root.is_dir()
    assert ordinary_file.is_file()
    assert (root / "live2d/model/base/source.bin").is_file()
    assert root.is_dir()


def test_failed_upload_is_retryable_without_removing_aggregate_sources(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "automatic-workspace"
    config = _workspace_config(root)
    uploads = _install_workspace_fakes(monkeypatch, fail_once_for="music/background")
    ordinary = ("ordinary-url", {"bundleName": "music/background"})
    aggregate = ("live2d-url", {"bundleName": "live2d/model/base"})

    failed = asyncio.run(
        pipeline.run_pipeline([ordinary, aggregate], config, {}, extracted_dir_is_temporary=True)
    )

    assert failed == [ordinary]
    first_ordinary_root = next(
        upload_root for name, upload_root, _ in uploads if name == "music/background"
    )
    aggregate_file = root / "live2d/model/base/source.bin"
    assert not first_ordinary_root.exists()
    assert aggregate_file.is_file()

    retried = asyncio.run(
        pipeline.run_pipeline(failed, config, {}, extracted_dir_is_temporary=True)
    )

    assert retried == []
    ordinary_roots = [upload_root for name, upload_root, _ in uploads if name == "music/background"]
    assert len(ordinary_roots) == 2
    assert ordinary_roots[0] != ordinary_roots[1]
    assert all(not upload_root.exists() for upload_root in ordinary_roots)
    assert aggregate_file.is_file()
    assert root.is_dir()


def test_cancelled_upload_cleans_only_its_temporary_artifact(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "automatic-workspace"
    config = _workspace_config(root)
    started = asyncio.Event()
    uploads = _install_workspace_fakes(
        monkeypatch,
        blocked_bundle="music/background",
        upload_started=started,
    )
    artifact = pipeline.PipelineArtifact(
        "ordinary-url",
        {"bundleName": "music/background"},
        AnyioPath(tmp_path / "bundle"),
        extracted_dir_is_temporary=True,
    )
    sibling = pipeline.PipelineArtifact(
        "sibling-url",
        {"bundleName": "music/background"},
        AnyioPath(tmp_path / "sibling-bundle"),
        extracted_dir_is_temporary=True,
    )
    aggregate_file = root / "live2d/model/base/source.bin"

    async def cancel_upload() -> None:
        await pipeline.extract_single_bundle(artifact, config)
        await pipeline.extract_single_bundle(sibling, config)
        aggregate_file.parent.mkdir(parents=True, exist_ok=True)
        aggregate_file.write_bytes(b"live2d source")
        task = asyncio.create_task(
            pipeline._upload_one_artifact("test", "upload", artifact, config, [], asyncio.Lock())
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_upload())

    assert len(uploads) == 1
    assert artifact.extracted_save_path is not None
    assert not asyncio.run(artifact.extracted_save_path.exists())
    assert sibling.extracted_save_path is not None
    assert asyncio.run(sibling.extracted_save_path.exists())
    assert aggregate_file.read_bytes() == b"live2d source"
    assert root.is_dir()


def test_cancelled_pipeline_cleans_artifacts_waiting_in_upload_queue(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "automatic-workspace"
    config = _workspace_config(root)
    config.PIPELINE_STAGE_QUEUE_SIZE = 2
    upload_started = asyncio.Event()
    queued_aggregate = asyncio.Event()
    uploads = _install_workspace_fakes(
        monkeypatch,
        blocked_bundle="music/background",
        upload_started=upload_started,
    )
    extraction_roots: dict[str, Path] = {}

    async def recording_extract(_bundle_path, bundle, output_root, **_kwargs):
        bundle_name = bundle["bundleName"]
        output = output_root / bundle_name / "source.bin"
        await output.parent.mkdir(parents=True, exist_ok=True)
        await output.write_bytes(bundle_name.encode())
        extraction_roots[bundle_name] = Path(output_root.as_posix())
        return [output]

    monkeypatch.setattr(pipeline, "extract_asset_bundle", recording_extract)
    original_extract_one = pipeline._extract_one_artifact

    async def signal_aggregate_queued(
        pipeline_id, name, artifact, upload_queue, config, failed_tasks, failed_lock, profiler
    ):
        await original_extract_one(
            pipeline_id,
            name,
            artifact,
            upload_queue,
            config,
            failed_tasks,
            failed_lock,
            profiler,
        )
        if artifact.bundle["bundleName"] == "live2d/model/base":
            # The first artifact is held by the upload worker; both remaining
            # completed artifacts must now be waiting in the bounded queue.
            assert upload_queue.qsize() == 2
            queued_aggregate.set()

    monkeypatch.setattr(pipeline, "_extract_one_artifact", signal_aggregate_queued)
    items = [
        ("blocking-url", {"bundleName": "music/background"}),
        ("queued-url", {"bundleName": "music/other"}),
        ("aggregate-url", {"bundleName": "live2d/model/base"}),
    ]

    async def cancel_pipeline() -> None:
        task = asyncio.create_task(
            pipeline.run_pipeline(items, config, {}, extracted_dir_is_temporary=True)
        )
        await upload_started.wait()
        await asyncio.wait_for(queued_aggregate.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_pipeline())

    assert [name for name, _upload_root, _file in uploads] == ["music/background"]
    for ordinary_bundle in ("music/background", "music/other"):
        artifact_root = extraction_roots[ordinary_bundle]
        assert artifact_root != root.resolve()
        assert not artifact_root.exists()
    aggregate_root = extraction_roots["live2d/model/base"]
    assert aggregate_root == root.resolve()
    assert (root / "live2d/model/base/source.bin").read_bytes() == b"live2d/model/base"
    assert root.is_dir()


def test_do_download_forwards_temporary_workspace_ownership(monkeypatch) -> None:
    config = SimpleNamespace(MIN_FREE_DISK_BYTES=0)
    forwarded: list[bool] = []

    async def fake_run_pipeline(_items, _config, _headers, **kwargs):
        forwarded.append(kwargs["extracted_dir_is_temporary"])
        return []

    monkeypatch.setattr(lifecycle, "run_pipeline", fake_run_pipeline)

    succeeded = asyncio.run(
        lifecycle.do_download([], config, {}, None, extracted_dir_is_temporary=True)
    )

    assert succeeded
    assert forwarded == [True]
