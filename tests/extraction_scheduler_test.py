from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio import Path as AnyioPath
from conftest import install_pipeline_fakes, pipeline_config

from updater import pipeline
from updater.pipeline.scheduler import (
    LIGHT_COST_CLASS,
    MEDIA_COST_CLASS,
    ExtractionScheduler,
    classify_bundle_cost,
)

MEDIA_HINTS = (re.compile(r"^media/"),)


def _artifact(name: str) -> pipeline.PipelineArtifact:
    return pipeline.PipelineArtifact(
        url=f"{name}-url",
        bundle={"bundleName": name},
        bundle_save_path=AnyioPath(f"{name}.bundle"),
    )


def test_classify_bundle_cost_uses_hints_and_defaults_to_light() -> None:
    assert classify_bundle_cost({"bundleName": "media/song"}, MEDIA_HINTS) == MEDIA_COST_CLASS
    assert classify_bundle_cost({"bundleName": "music/song"}, MEDIA_HINTS) == LIGHT_COST_CLASS
    assert classify_bundle_cost({}, MEDIA_HINTS) == LIGHT_COST_CLASS
    assert classify_bundle_cost({"bundleName": "media/song"}, ()) == LIGHT_COST_CLASS


def test_scheduler_claims_pending_artifacts_in_fifo_order() -> None:
    async def run() -> None:
        scheduler = ExtractionScheduler(capacity=4, media_slots=1)
        artifacts = [_artifact("a"), _artifact("b"), _artifact("c")]
        for artifact in artifacts:
            await scheduler.put(artifact)

        claimed = [await scheduler.claim() for _ in range(3)]

        assert claimed == artifacts
        assert scheduler.qsize() == 0
        for artifact in claimed:
            await scheduler.release(artifact)

    asyncio.run(run())


def test_scheduler_skips_capped_media_artifacts_for_light_ones() -> None:
    async def run() -> None:
        scheduler = ExtractionScheduler(capacity=4, media_slots=1, media_hints=MEDIA_HINTS)
        media_one = _artifact("media/one")
        media_two = _artifact("media/two")
        light = _artifact("light/one")
        for artifact in (media_one, media_two, light):
            await scheduler.put(artifact)

        first = await scheduler.claim()
        second = await scheduler.claim()

        assert first is media_one
        assert scheduler.active_media_count() == 1
        assert second is light

        blocked = asyncio.create_task(scheduler.claim())
        await asyncio.sleep(0.01)
        shielded = asyncio.shield(blocked)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(shielded, timeout=0.05)

        await scheduler.release(media_one)
        assert await asyncio.wait_for(blocked, timeout=1) is media_two

        await scheduler.release(light)
        await scheduler.release(media_two)
        assert scheduler.active_count() == 0

    asyncio.run(run())


def test_scheduler_claim_stamps_artifact_cost_class() -> None:
    async def run() -> None:
        scheduler = ExtractionScheduler(capacity=4, media_slots=2, media_hints=MEDIA_HINTS)
        media = _artifact("media/one")
        light = _artifact("light/one")
        await scheduler.put(media)
        await scheduler.put(light)

        claimed_media = await scheduler.claim()
        claimed_light = await scheduler.claim()

        assert claimed_media is media
        assert media.cost_class == MEDIA_COST_CLASS
        assert claimed_light is light
        assert light.cost_class == LIGHT_COST_CLASS

        await scheduler.release(media)
        await scheduler.release(light)
        assert scheduler.active_count() == 0
        assert scheduler.active_media_count() == 0

    asyncio.run(run())


def test_scheduler_put_blocks_while_pending_queue_is_full() -> None:
    async def run() -> None:
        scheduler = ExtractionScheduler(capacity=1, media_slots=1)
        first = _artifact("a")
        second = _artifact("b")
        await scheduler.put(first)

        blocked_put = asyncio.create_task(scheduler.put(second))
        await asyncio.sleep(0.01)
        shielded_put = asyncio.shield(blocked_put)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(shielded_put, timeout=0.05)

        claimed = await scheduler.claim()
        assert claimed is first
        await asyncio.wait_for(blocked_put, timeout=1)
        assert scheduler.qsize() == 1

    asyncio.run(run())


def test_scheduler_close_drains_pending_artifacts_then_exits_workers() -> None:
    async def run() -> None:
        scheduler = ExtractionScheduler(capacity=2, media_slots=1)
        artifact = _artifact("a")
        await scheduler.put(artifact)
        await scheduler.close()

        unsent = _artifact("b")
        with pytest.raises(RuntimeError, match="closed"):
            await scheduler.put(unsent)

        claimed = await scheduler.claim()
        assert claimed is artifact
        await scheduler.release(claimed)
        assert await scheduler.claim() is None

    asyncio.run(run())


def test_scheduler_wait_idle_resolves_only_after_release() -> None:
    async def run() -> None:
        scheduler = ExtractionScheduler(capacity=2, media_slots=1)
        artifact = _artifact("a")
        await scheduler.put(artifact)
        claimed = await scheduler.claim()

        idle = asyncio.create_task(scheduler.wait_idle())
        await asyncio.sleep(0.01)
        shielded_idle = asyncio.shield(idle)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(shielded_idle, timeout=0.05)

        await scheduler.release(claimed)
        await asyncio.wait_for(idle, timeout=1)

    asyncio.run(run())


def test_adaptive_worker_count_never_exceeds_explicit_maximum() -> None:
    config = SimpleNamespace(MAX_CONCURRENCY_EXTRACTS=8, EXTRACT_MAX_WORKERS=3)
    assert pipeline.get_adaptive_extract_worker_count(config) == 3

    config.EXTRACT_MAX_WORKERS = None
    assert pipeline.get_adaptive_extract_worker_count(config) == 8

    config.EXTRACT_MAX_WORKERS = 20
    assert pipeline.get_adaptive_extract_worker_count(config) == 8


def test_adaptive_media_slots_default_keeps_a_light_slot() -> None:
    config = SimpleNamespace()
    assert pipeline.get_adaptive_media_slots(config, 4) == 2
    assert pipeline.get_adaptive_media_slots(config, 1) == 1

    config.EXTRACT_ADAPTIVE_MEDIA_SLOTS = 10
    assert pipeline.get_adaptive_media_slots(config, 4) == 4


def test_run_pipeline_rejects_unknown_scheduler_mode(tmp_path: Path) -> None:
    config = pipeline_config(tmp_path / "extracted")
    config.EXTRACT_SCHEDULER_MODE = "turbo"

    run_coro = pipeline.run_pipeline([("url", {"bundleName": "b"})], config, {})
    with pytest.raises(ValueError, match="scheduler mode"):
        asyncio.run(run_coro)


def test_adaptive_mode_limits_media_concurrency_and_keeps_light_moving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = pipeline_config(tmp_path / "extracted")
    config.MAX_CONCURRENCY_EXTRACTS = 3
    config.PIPELINE_STAGE_QUEUE_SIZE = 4
    config.EXTRACT_SCHEDULER_MODE = "adaptive"
    config.EXTRACT_ADAPTIVE_MEDIA_SLOTS = 1
    config.EXTRACT_MEDIA_BUNDLE_HINTS = [r"^media/"]

    media_active = {"count": 0, "max": 0}
    light_order: list[str] = []

    async def fake_download(_url, root, relative, **_kwargs):
        await root.joinpath(relative).write_bytes(b"synthetic bundle")

    async def fake_extract(_bundle_path, bundle, output_root, **_kwargs):
        name = bundle["bundleName"]
        if name.startswith("media/"):
            media_active["count"] += 1
            media_active["max"] = max(media_active["max"], media_active["count"])
            await asyncio.sleep(0.05)
            media_active["count"] -= 1
        else:
            light_order.append(name)
        output = output_root / "out.txt"
        await output.write_bytes(name.encode())
        return [output]

    async def fake_upload(_files, _root, *_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(pipeline, "upload_to_storage", fake_upload)

    names = ["media/one", "media/two", "media/three", "light/one"]
    items = [(f"{name}-url", {"bundleName": name}) for name in names]

    failed = asyncio.run(pipeline.run_pipeline(items, config, {}))

    assert failed == []
    assert media_active["max"] == 1
    assert "light/one" in light_order


def test_adaptive_mode_stamps_cost_class_and_fixed_mode_leaves_it_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str | None] = {}

    async def fake_extract(_bundle_path, bundle, output_root, cost_class=None, **_kwargs):
        seen[bundle["bundleName"]] = cost_class
        output = output_root / "out.txt"
        await output.write_bytes(b"ok")
        return [output]

    async def fake_download(_url, root, relative, **_kwargs):
        await root.joinpath(relative).write_bytes(b"synthetic bundle")

    async def fake_upload(_files, _root, *_args, **_kwargs):
        return None

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(pipeline, "upload_to_storage", fake_upload)

    items = [("media-one-url", {"bundleName": "media/one"}), ("light-url", {"bundleName": "l"})]
    adaptive_config = pipeline_config(tmp_path / "adaptive")
    adaptive_config.EXTRACT_SCHEDULER_MODE = "adaptive"
    adaptive_config.EXTRACT_MEDIA_BUNDLE_HINTS = [r"^media/"]

    assert asyncio.run(pipeline.run_pipeline(items, adaptive_config, {})) == []
    assert seen == {"media/one": MEDIA_COST_CLASS, "l": LIGHT_COST_CLASS}

    seen.clear()
    fixed_config = pipeline_config(tmp_path / "fixed")
    assert asyncio.run(pipeline.run_pipeline(items, fixed_config, {})) == []
    assert seen == {"media/one": None, "l": None}


def test_adaptive_mode_without_hints_produces_fixed_mode_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs_per_mode: list[list[tuple[str, bytes]]] = []
    for mode in ("fixed", "adaptive"):
        uploads: list[tuple[str, Path, Path, bytes]] = []
        contents = {"first": b"first bytes", "second": b"second bytes"}
        install_pipeline_fakes(monkeypatch, contents, uploads)
        config = pipeline_config(tmp_path / mode)
        config.EXTRACT_SCHEDULER_MODE = mode

        failed = asyncio.run(
            pipeline.run_pipeline(
                [("first-url", {"bundleName": "first"}), ("second-url", {"bundleName": "second"})],
                config,
                {},
            )
        )

        assert failed == []
        outputs_per_mode.append([(name, data) for name, _root, _file, data in uploads])

    assert outputs_per_mode[0] == outputs_per_mode[1]


def test_adaptive_mode_records_failed_task_and_keeps_processing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []

    async def fake_download(_url, root, relative, **_kwargs):
        await root.joinpath(relative).write_bytes(b"synthetic bundle")

    async def fake_extract(_bundle_path, bundle, output_root, **_kwargs):
        if bundle["bundleName"] == "first":
            raise RuntimeError("synthetic extraction failure")
        output = output_root / "out.txt"
        await output.write_bytes(b"ok")
        return [output]

    async def fake_upload(files, _root, *_args, **_kwargs):
        uploads.append((files[0].name, b"ok"))

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(pipeline, "upload_to_storage", fake_upload)
    config = pipeline_config(tmp_path / "extracted")
    config.EXTRACT_SCHEDULER_MODE = "adaptive"

    failed = asyncio.run(
        pipeline.run_pipeline(
            [("first-url", {"bundleName": "first"}), ("second-url", {"bundleName": "second"})],
            config,
            {},
        )
    )

    assert failed == [("first-url", {"bundleName": "first"})]
    assert uploads == [("out.txt", b"ok")]


def test_adaptive_mode_cleans_pending_artifacts_on_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reserved: list[str] = []
    original_reserve = pipeline._reserve_temporary_bundle_path

    def recording_reserve() -> str:
        name = original_reserve()
        reserved.append(name)
        return name

    extract_started = asyncio.Event()

    async def fake_download(_url, root, relative, **_kwargs):
        await root.joinpath(relative).write_bytes(b"synthetic bundle")

    async def blocked_extract(_bundle_path, _bundle, output_root, **_kwargs):
        output = output_root / "partial.txt"
        await output.write_bytes(b"partial")
        extract_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pipeline, "_reserve_temporary_bundle_path", recording_reserve)
    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", blocked_extract)
    config = pipeline_config(None)
    config.EXTRACT_SCHEDULER_MODE = "adaptive"
    config.PIPELINE_STAGE_QUEUE_SIZE = 1
    items = [
        ("first-url", {"bundleName": "first"}),
        ("second-url", {"bundleName": "second"}),
    ]

    async def scenario() -> None:
        run_task = asyncio.create_task(pipeline.run_pipeline(items, config, {}))
        await extract_started.wait()
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task

    asyncio.run(scenario())

    assert reserved
    assert all(not Path(path).exists() for path in reserved)
