from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import install_pipeline_fakes, pipeline_config

from updater import pipeline


@pytest.mark.parametrize("configured", [False, True], ids=["temporary", "configured"])
def test_run_pipeline_isolates_same_name_outputs_and_finishes_all_stage_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []
    contents = {"first": b"first bytes", "second": b"second bytes"}
    install_pipeline_fakes(monkeypatch, contents, uploads)
    sentinel_calls: list[tuple[object, int]] = []
    original_put_sentinels = pipeline._put_sentinels

    async def recording_put_sentinels(queue, count):
        sentinel_calls.append((queue, count))
        await original_put_sentinels(queue, count)

    monkeypatch.setattr(pipeline, "_put_sentinels", recording_put_sentinels)
    config = pipeline_config(tmp_path / "configured" if configured else None)
    items = [
        ("first-url", {"bundleName": "first"}),
        ("second-url", {"bundleName": "second"}),
    ]

    failed = pipeline.asyncio.run(pipeline.run_pipeline(items, config, {}))

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
    install_pipeline_fakes(monkeypatch, contents, uploads, failing_url="failed-url")
    config = pipeline_config(None)
    items = [
        ("failed-url", {"bundleName": "failed"}),
        ("second-url", {"bundleName": "second"}),
    ]

    failed = pipeline.asyncio.run(pipeline.run_pipeline(items, config, {}))

    assert failed == [("failed-url", {"bundleName": "failed"})]
    failed_upload = next(item for item in uploads if item[3] == b"failed bytes")
    successful_upload = next(item for item in uploads if item[3] == b"second bytes")
    assert not failed_upload[1].exists()
    assert not successful_upload[1].exists()
    assert failed_upload[1] != successful_upload[1]
    assert failed_upload[2].parent == failed_upload[1]
    assert successful_upload[2].parent == successful_upload[1]


def test_run_pipeline_download_workers_reuse_the_supplied_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = pipeline_config(None)
    config.MAX_CONCURRENCY_DOWNLOADS = 2
    contents = {"first": b"first bytes", "second": b"second bytes"}
    uploads: list[tuple[str, Path, Path, bytes]] = []
    install_pipeline_fakes(monkeypatch, contents, uploads)

    download_headers: list[dict[str, str]] = []
    original_download = pipeline.download_deobfuscate_bundle

    async def record_download(*args, **kwargs):
        download_headers.append(kwargs["headers"])
        await original_download(*args, **kwargs)

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", record_download)

    failed = pipeline.asyncio.run(
        pipeline.run_pipeline(
            [("first-url", {"bundleName": "first"}), ("second-url", {"bundleName": "second"})],
            config,
            {"User-Agent": "public-agent"},
            cookie="pipeline-cookie",
        )
    )

    assert failed == []
    assert download_headers == [
        {"Cookie": "pipeline-cookie"},
        {"Cookie": "pipeline-cookie"},
    ]


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
        raise RuntimeError("unexpected extract pipeline failure")

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "_extract_stage", crashing_extract_stage)

    run_coro = pipeline.run_pipeline([("url", {"bundleName": "bundle"})], pipeline_config(None), {})
    with pytest.raises(RuntimeError, match="unexpected extract pipeline failure"):
        asyncio.run(run_coro)

    assert captured_path
    assert not captured_path[0].exists()
