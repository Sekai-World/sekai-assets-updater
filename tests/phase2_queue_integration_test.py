from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio import Path as AnyioPath

import worker


def _config(extracted_root: Path | None) -> SimpleNamespace:
    return SimpleNamespace(
        ASSET_LOCAL_BUNDLE_CACHE_DIR=None,
        ASSET_LOCAL_EXTRACTED_DIR=AnyioPath(extracted_root) if extracted_root else None,
        ASSET_REMOTE_STORAGE=[
            {"type": "normal", "base": "remote", "program": "uploader", "args": []}
        ],
        UNITY_VERSION=None,
        MAX_CONCURRENCY_DOWNLOADS=1,
        MAX_CONCURRENCY_EXTRACTS=1,
        MAX_CONCURRENCY_UPLOAD_STAGE=1,
        PIPELINE_STAGE_QUEUE_SIZE=1,
        MAX_CONCURRENCY_UPLOADS=1,
        REQUEST_TIMEOUT=1,
    )


def _install_pipeline_fakes(
    monkeypatch: pytest.MonkeyPatch,
    contents: dict[str, bytes],
    uploads: list[tuple[str, Path, Path, bytes]],
    *,
    failing_url: str | None = None,
):
    async def fake_download(_url, root, relative, **_kwargs):
        await root.joinpath(relative).write_bytes(b"synthetic bundle")

    async def fake_extract(bundle_path, bundle, output_root, **_kwargs):
        output = output_root / "shared.txt"
        await output.write_bytes(contents[bundle["bundleName"]])
        return [output]

    async def fake_upload(files, root, *_args, **_kwargs):
        assert len(files) == 1
        exported_file = files[0]
        root_path = Path((await root.resolve()).as_posix())
        exported_path = Path((await exported_file.resolve()).as_posix())
        assert exported_path.parent == root_path
        data = await exported_file.read_bytes()
        uploads.append((exported_path.name, root_path, exported_path, data))
        if uploads[-1][1].name and failing_url is not None:
            if data == contents["failed"]:
                raise RuntimeError("synthetic upload failure")

    monkeypatch.setattr(worker, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(worker, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(worker, "upload_to_storage", fake_upload)


@pytest.mark.parametrize("configured", [False, True], ids=["temporary", "configured"])
def test_run_pipeline_isolates_same_name_outputs_and_finishes_all_stage_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []
    contents = {"first": b"first bytes", "second": b"second bytes"}
    _install_pipeline_fakes(monkeypatch, contents, uploads)
    sentinel_calls: list[tuple[object, int]] = []
    original_put_sentinels = worker._put_sentinels

    async def recording_put_sentinels(queue, count):
        sentinel_calls.append((queue, count))
        await original_put_sentinels(queue, count)

    monkeypatch.setattr(worker, "_put_sentinels", recording_put_sentinels)
    config = _config(tmp_path / "configured" if configured else None)
    items = [
        ("first-url", {"bundleName": "first"}),
        ("second-url", {"bundleName": "second"}),
    ]

    failed = worker.asyncio.run(worker.run_pipeline(items, config, {}))

    assert failed == []
    assert [(name, data) for name, _root, _file, data in uploads] == [
        ("shared.txt", b"first bytes"),
        ("shared.txt", b"second bytes"),
    ]
    assert uploads[0][1] != uploads[1][1]
    assert uploads[0][2].parent == uploads[0][1]
    assert uploads[1][2].parent == uploads[1][1]
    assert [count for _queue, count in sentinel_calls] == [1, 1, 1]

    if configured:
        assert uploads[0][1].is_relative_to((tmp_path / "configured").resolve())
        assert uploads[1][1].is_relative_to((tmp_path / "configured").resolve())
        assert uploads[0][1].exists()
        assert uploads[1][1].exists()
        assert uploads[0][1] != uploads[1][1]
    else:
        assert not uploads[0][1].exists()
        assert not uploads[1][1].exists()


def test_run_pipeline_upload_failure_cleans_temporary_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []
    contents = {"failed": b"failed bytes", "second": b"second bytes"}
    _install_pipeline_fakes(monkeypatch, contents, uploads, failing_url="failed-url")
    config = _config(None)
    items = [
        ("failed-url", {"bundleName": "failed"}),
        ("second-url", {"bundleName": "second"}),
    ]

    failed = worker.asyncio.run(worker.run_pipeline(items, config, {}))

    assert failed == [("failed-url", {"bundleName": "failed"})]
    failed_upload = next(item for item in uploads if item[3] == b"failed bytes")
    successful_upload = next(item for item in uploads if item[3] == b"second bytes")
    assert not failed_upload[1].exists()
    assert not successful_upload[1].exists()
    assert failed_upload[1] != successful_upload[1]
    assert failed_upload[2].parent == failed_upload[1]
    assert successful_upload[2].parent == successful_upload[1]


def test_run_pipeline_propagates_unexpected_stage_worker_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloaded = asyncio.Event()
    captured_path: list[Path] = []

    async def fake_download(_url, root, relative, **_kwargs):
        path = root.joinpath(relative)
        await path.write_bytes(b"synthetic bundle")
        captured_path.append(Path(path.as_posix()))
        downloaded.set()

    async def crashing_extract_stage(*_args, **_kwargs):
        await downloaded.wait()
        raise RuntimeError("unexpected extract worker failure")

    monkeypatch.setattr(worker, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(worker, "_extract_stage", crashing_extract_stage)

    pipeline = worker.run_pipeline([("url", {"bundleName": "bundle"})], _config(None), {})
    with pytest.raises(RuntimeError, match="unexpected extract worker failure"):
        worker.asyncio.run(pipeline)

    assert captured_path
    assert not captured_path[0].exists()
