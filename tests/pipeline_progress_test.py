from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from conftest import pipeline_config

from updater import pipeline


@pytest.mark.parametrize("mode", ["fixed", "adaptive"])
def test_pipeline_progress_reports_bundle_outcomes_for_each_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    async def fake_download(url, root, relative, **_kwargs):
        if url == "download-failure":
            raise RuntimeError("synthetic download failure")
        await root.joinpath(relative).write_bytes(url.encode())

    async def fake_extract(_bundle_path, bundle, output_root, **_kwargs):
        name = bundle["bundleName"]
        if name == "extract-failure":
            raise RuntimeError("synthetic extraction failure")
        output = output_root / "out.txt"
        await output.write_bytes(name.encode())
        return [output]

    async def fake_upload(files, _root, *_args, **_kwargs):
        if await files[0].read_bytes() == b"upload-failure":
            raise RuntimeError("synthetic upload failure")

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(pipeline, "upload_to_storage", fake_upload)
    config = pipeline_config(tmp_path / "extracted")
    config.EXTRACT_SCHEDULER_MODE = mode
    config.MAX_CONCURRENCY_DOWNLOADS = 2
    config.MAX_CONCURRENCY_EXTRACTS = 2
    config.MAX_CONCURRENCY_UPLOAD_STAGE = 2
    items = [
        ("download-failure", {"bundleName": "download-failure"}),
        ("extract-url", {"bundleName": "extract-failure"}),
        ("upload-url", {"bundleName": "upload-failure"}),
        ("success-url", {"bundleName": "success"}),
    ]

    with caplog.at_level(logging.INFO, logger="asset_updater"):
        failed = asyncio.run(pipeline.run_pipeline(items, config, {}))

    assert {bundle["bundleName"] for _url, bundle in failed} == {
        "download-failure",
        "extract-failure",
        "upload-failure",
    }
    progress_records = [
        record.getMessage() for record in caplog.records if "stage=progress" in record.getMessage()
    ]
    initial = next(record for record in progress_records if "status=initial" in record)
    completed = next(record for record in progress_records if "status=completed" in record)
    assert "unit=bundles" in initial
    assert "total=4 | succeeded=0 | failed=0 | remaining=4" in initial
    assert "download_waiting=4 | download_running=0" in initial
    assert "total=4 | succeeded=1 | failed=3 | remaining=0" in completed
    assert "download_waiting=0 | download_running=0" in completed
    assert "extract_waiting=0 | extract_running=0" in completed
    assert "upload_waiting=0 | upload_running=0" in completed


@pytest.mark.parametrize("mode", ["fixed", "adaptive"])
def test_pipeline_progress_periodically_reports_concurrent_stage_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    download_gate = asyncio.Event()
    extract_gate = asyncio.Event()
    upload_gate = asyncio.Event()
    download_started = asyncio.Event()
    extract_started = asyncio.Event()
    upload_started = asyncio.Event()
    active = {"download": 0, "extract": 0, "upload": 0}

    async def fake_download(_url, root, relative, **_kwargs):
        active["download"] += 1
        if active["download"] >= 2:
            download_started.set()
        await download_gate.wait()
        active["download"] -= 1
        await root.joinpath(relative).write_bytes(b"bundle")

    async def fake_extract(_bundle_path, bundle, output_root, **_kwargs):
        active["extract"] += 1
        if active["extract"] >= 2:
            extract_started.set()
        await extract_gate.wait()
        active["extract"] -= 1
        output = output_root / "out.txt"
        await output.write_bytes(bundle["bundleName"].encode())
        return [output]

    async def fake_upload(_files, _root, *_args, **_kwargs):
        active["upload"] += 1
        if active["upload"] >= 2:
            upload_started.set()
        await upload_gate.wait()
        active["upload"] -= 1

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(pipeline, "upload_to_storage", fake_upload)
    monkeypatch.setattr(pipeline, "_PIPELINE_PROGRESS_SAMPLE_INTERVAL_SEC", 0.005)
    config = pipeline_config(tmp_path / "extracted")
    config.MAX_CONCURRENCY_DOWNLOADS = 2
    config.MAX_CONCURRENCY_EXTRACTS = 2
    config.MAX_CONCURRENCY_UPLOAD_STAGE = 2
    config.PIPELINE_STAGE_QUEUE_SIZE = 1
    config.EXTRACT_SCHEDULER_MODE = mode
    items = [(f"url-{index}", {"bundleName": f"bundle-{index}"}) for index in range(3)]

    async def wait_for_log(text: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2
        while text not in caplog.text:
            if loop.time() >= deadline:
                raise AssertionError(f"progress log did not contain {text!r}: {caplog.text}")
            await asyncio.sleep(0.002)

    async def scenario() -> None:
        run_task = asyncio.create_task(pipeline.run_pipeline(items, config, {}))
        try:
            await asyncio.wait_for(download_started.wait(), timeout=2)
            await wait_for_log("download_waiting=1 | download_running=2")
            download_gate.set()

            await asyncio.wait_for(extract_started.wait(), timeout=2)
            await wait_for_log("extract_running=2")
            extract_gate.set()

            await asyncio.wait_for(upload_started.wait(), timeout=2)
            await wait_for_log("upload_running=2")
            upload_gate.set()
            assert await asyncio.wait_for(run_task, timeout=2) == []
        finally:
            download_gate.set()
            extract_gate.set()
            upload_gate.set()
            if not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)

    with caplog.at_level(logging.INFO, logger="asset_updater"):
        asyncio.run(scenario())


def test_progress_snapshot_preserves_counts_and_terminal_outcomes() -> None:
    progress = pipeline.PipelineProgress("invariants", 2)
    stage_fields = (
        "download_waiting",
        "download_running",
        "extract_waiting",
        "extract_running",
        "upload_waiting",
        "upload_running",
    )

    def assert_invariants() -> dict[str, int]:
        snapshot = progress.snapshot()
        assert snapshot["total"] == (
            snapshot["succeeded"] + snapshot["failed"] + snapshot["remaining"]
        )
        assert sum(snapshot[field] for field in stage_fields) == snapshot["remaining"]
        assert all(snapshot[field] >= 0 for field in (*stage_fields, "succeeded", "failed"))
        return snapshot

    def transition(token: int, current: str, next_state: str) -> None:
        progress.transition(token, current, next_state)
        assert_invariants()

    assert assert_invariants()["download_waiting"] == 2
    transition(0, "download_waiting", "download_running")
    transition(0, "download_running", "extract_waiting")
    transition(0, "extract_waiting", "extract_running")
    transition(0, "extract_running", "upload_waiting")
    transition(0, "upload_waiting", "upload_running")
    progress.finish(0, "succeeded")
    successful = assert_invariants()
    progress.finish(0, "failed")  # Terminal state cannot be counted twice or changed.
    assert assert_invariants() == successful
    progress.finish(1, "failed")
    failed = assert_invariants()
    progress.finish(1, "failed")
    assert assert_invariants() == failed

    snapshot = assert_invariants()
    assert snapshot["succeeded"] == 1
    assert snapshot["failed"] == 1
    assert snapshot["remaining"] == 0


@pytest.mark.parametrize("blocked_stage", ["extract", "upload"])
def test_full_queue_put_is_waiting_and_cancellation_converges(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    blocked_stage: str,
) -> None:
    put_blocked = asyncio.Event()
    extract_started = asyncio.Event()
    upload_started = asyncio.Event()
    extract_gate = asyncio.Event()
    upload_gate = asyncio.Event()
    download_paths: list[Path] = []
    extraction_roots: list[Path] = []
    original_queue_put = asyncio.Queue.put

    async def observe_full_queue_put(queue, item):
        if (
            isinstance(item, pipeline.PipelineArtifact)
            and item.bundle["bundleName"] == "bundle-2"
            and (item.exported_list is None) == (blocked_stage == "extract")
        ):
            assert queue.maxsize == 1
            assert queue.full()
            put_blocked.set()
        return await original_queue_put(queue, item)

    async def fake_download(_url, root, relative, **_kwargs):
        await root.joinpath(relative).write_bytes(b"bundle")
        download_paths.append(Path(root.as_posix()) / relative)

    async def fake_extract(_bundle_path, bundle, output_root, **_kwargs):
        extraction_roots.append(Path(output_root.as_posix()))
        if blocked_stage == "extract":
            extract_started.set()
            await extract_gate.wait()
        output = output_root / "out.txt"
        await output.write_bytes(bundle["bundleName"].encode())
        return [output]

    async def fake_upload(files, _root, *_args, **_kwargs):
        if await files[0].read_bytes() == b"bundle-0":
            upload_started.set()
            await upload_gate.wait()

    monkeypatch.setattr(asyncio.Queue, "put", observe_full_queue_put)
    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(pipeline, "upload_to_storage", fake_upload)
    monkeypatch.setattr(pipeline, "_PIPELINE_PROGRESS_SAMPLE_INTERVAL_SEC", 0.001)
    config = pipeline_config(None)
    config.MAX_CONCURRENCY_DOWNLOADS = 1
    config.MAX_CONCURRENCY_EXTRACTS = 1
    config.MAX_CONCURRENCY_UPLOAD_STAGE = 1
    config.PIPELINE_STAGE_QUEUE_SIZE = 1
    items = [(f"url-{index}", {"bundleName": f"bundle-{index}"}) for index in range(3)]

    async def wait_for_log(text: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2
        while text not in caplog.text:
            if loop.time() >= deadline:
                raise AssertionError(f"progress log did not contain {text!r}: {caplog.text}")
            await asyncio.sleep(0.001)

    async def scenario() -> None:
        run_task = asyncio.create_task(pipeline.run_pipeline(items, config, {}))
        try:
            await asyncio.wait_for(put_blocked.wait(), timeout=2)
            if blocked_stage == "extract":
                await asyncio.wait_for(extract_started.wait(), timeout=2)
                await wait_for_log(
                    "total=3 | succeeded=0 | failed=0 | remaining=3 | "
                    "download_waiting=0 | download_running=0 | "
                    "extract_waiting=2 | extract_running=1"
                )
            else:
                await asyncio.wait_for(upload_started.wait(), timeout=2)
                await wait_for_log(
                    "total=3 | succeeded=0 | failed=0 | remaining=3 | "
                    "download_waiting=0 | download_running=0 | "
                    "extract_waiting=0 | extract_running=0 | "
                    "upload_waiting=2 | upload_running=1"
                )

            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(run_task, timeout=2)
            assert not [
                task
                for task in asyncio.all_tasks()
                if task.get_name().startswith("pipeline-progress-")
            ]
        finally:
            extract_gate.set()
            upload_gate.set()
            if not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)

    with caplog.at_level(logging.INFO, logger="asset_updater"):
        asyncio.run(scenario())

    expected_waiting = f"{blocked_stage}_waiting=2 | {blocked_stage}_running=1"
    assert any(
        "total=3 | succeeded=0 | failed=0 | remaining=3" in record.getMessage()
        and expected_waiting in record.getMessage()
        for record in caplog.records
        if "stage=progress" in record.getMessage()
    )
    cancelled = next(
        record.getMessage()
        for record in caplog.records
        if "stage=progress" in record.getMessage() and "status=cancelled" in record.getMessage()
    )
    assert "total=3 | succeeded=0 | failed=0 | remaining=3" in cancelled
    assert all(not path.exists() for path in download_paths)
    assert all(not root.exists() for root in extraction_roots)


def test_zero_item_pipeline_logs_observable_initial_and_final_progress(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = pipeline_config(tmp_path / "extracted")

    with caplog.at_level(logging.INFO, logger="asset_updater"):
        assert asyncio.run(pipeline.run_pipeline([], config, {})) == []

    records = [
        record.getMessage() for record in caplog.records if "stage=progress" in record.getMessage()
    ]
    assert any(
        "status=initial" in record and "total=0 | succeeded=0 | failed=0 | remaining=0" in record
        for record in records
    )
    assert any(
        "status=completed" in record and "total=0 | succeeded=0 | failed=0 | remaining=0" in record
        for record in records
    )


def test_cancelled_pipeline_logs_final_counts_and_stops_progress_sampler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    download_started = asyncio.Event()

    async def blocked_download(*_args, **_kwargs):
        download_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", blocked_download)
    monkeypatch.setattr(pipeline, "_PIPELINE_PROGRESS_SAMPLE_INTERVAL_SEC", 0.005)
    config = pipeline_config(tmp_path / "extracted")

    async def scenario() -> None:
        run_task = asyncio.create_task(
            pipeline.run_pipeline([("url", {"bundleName": "bundle"})], config, {})
        )
        await asyncio.wait_for(download_started.wait(), timeout=2)
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task
        assert not [
            task for task in asyncio.all_tasks() if task.get_name().startswith("pipeline-progress-")
        ]

    with caplog.at_level(logging.INFO, logger="asset_updater"):
        asyncio.run(scenario())

    cancelled = next(
        record.getMessage()
        for record in caplog.records
        if "stage=progress" in record.getMessage() and "status=cancelled" in record.getMessage()
    )
    assert "total=1 | succeeded=0 | failed=0 | remaining=1" in cancelled
    assert "download_waiting=" not in cancelled


def test_pipeline_progress_logging_errors_are_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    progress = pipeline.PipelineProgress("test", 1)

    def broken_info(*_args, **_kwargs):
        raise RuntimeError("synthetic logger failure")

    monkeypatch.setattr(pipeline.logger, "info", broken_info)

    pipeline._log_pipeline_progress(progress, "initial")

    async def run_sampler() -> None:
        task = asyncio.create_task(pipeline._sample_pipeline_progress(progress, interval=0.001))
        await asyncio.sleep(0.01)
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)

    asyncio.run(run_sampler())
