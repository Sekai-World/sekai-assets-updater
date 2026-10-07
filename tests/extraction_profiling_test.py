from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio import Path as AnyioPath
from conftest import install_pipeline_fakes, pipeline_config

from updater import pipeline


def test_extract_single_bundle_populates_artifact_standalone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extracted_root = tmp_path / "extracted"

    async def fake_extract(bundle_path, bundle, output_root, **_kwargs):
        output = output_root / "texture.png"
        await output.write_bytes(b"texture-bytes")
        return [output]

    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    config = pipeline_config(extracted_root)
    artifact = pipeline.PipelineArtifact(
        url="url",
        bundle={"bundleName": "first"},
        bundle_save_path=AnyioPath(tmp_path / "first.bundle"),
    )

    result = asyncio.run(pipeline.extract_single_bundle(artifact, config))

    assert result is artifact
    assert result.extracted_save_path is not None
    staging = Path(result.extracted_save_path.as_posix())
    assert staging.is_relative_to(extracted_root.resolve())
    assert [Path(path.as_posix()).name for path in result.exported_list] == ["texture.png"]
    assert result.remove_extracted_after_upload is False
    assert result.tmp_extracted_save_dir is None


def test_extract_single_bundle_uses_temporary_staging_without_configured_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_extract(_bundle_path, _bundle, output_root, **_kwargs):
        output = output_root / "file.txt"
        await output.write_bytes(b"data")
        return [output]

    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    config = pipeline_config(None)
    artifact = pipeline.PipelineArtifact(
        url="url",
        bundle={"bundleName": "first"},
        bundle_save_path=AnyioPath(tmp_path / "first.bundle"),
    )

    result = asyncio.run(pipeline.extract_single_bundle(artifact, config))

    assert result.remove_extracted_after_upload is True
    assert result.tmp_extracted_save_dir is not None
    staging = Path(result.extracted_save_path.as_posix())
    assert staging.exists()
    assert (staging / "file.txt").exists()

    result.tmp_extracted_save_dir.cleanup()
    assert not staging.exists()


def test_extract_single_bundle_propagates_failure_without_queue_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_extract(*_args, **_kwargs):
        raise RuntimeError("synthetic extraction failure")

    monkeypatch.setattr(pipeline, "extract_asset_bundle", failing_extract)
    config = pipeline_config(tmp_path / "extracted")
    artifact = pipeline.PipelineArtifact(
        url="url",
        bundle={"bundleName": "first"},
        bundle_save_path=AnyioPath(tmp_path / "first.bundle"),
    )

    extract_coro = pipeline.extract_single_bundle(artifact, config)
    with pytest.raises(RuntimeError, match="synthetic extraction failure"):
        asyncio.run(extract_coro)

    assert artifact.exported_list is None
    assert artifact.extracted_save_path is not None
    assert artifact.tmp_extracted_save_dir is None


def test_extraction_profiler_aggregates_and_logs_summary(caplog: pytest.LogCaptureFixture) -> None:
    profiler = pipeline.ExtractionProfiler("testpipe", None)
    profiler.record_bundle("bundle-a", 0.5, 3, 300)
    profiler.record_bundle("bundle-b", 1.0, 2, 200)
    profiler.record_bundle_failure("bundle-c", 0.25, RuntimeError("secret failure details"))
    profiler.record_worker_idle(0.75)
    profiler.record_queue_depth(2)
    profiler.record_queue_depth(4)

    with caplog.at_level(logging.INFO, logger="asset_updater"):
        profiler.log_summary()
        profiler.finish("completed")

    assert "status=summary" in caplog.text
    assert "bundles=3" in caplog.text
    assert "failed=1" in caplog.text
    assert "extraction_sec=1.750" in caplog.text
    assert "idle_sec=0.750" in caplog.text
    assert "outputs=5" in caplog.text
    assert "output_bytes=500" in caplog.text
    assert "queue_depth_avg=3.00" in caplog.text
    assert "queue_depth_max=4" in caplog.text


def test_extraction_profiler_writes_jsonl_profile(tmp_path: Path) -> None:
    profile_path = tmp_path / "profiles" / "run.jsonl"
    profiler = pipeline.ExtractionProfiler("testpipe", profile_path)
    profiler.record_bundle("bundle-a", 0.5, 3, 300, cost_class="media")
    profiler.record_bundle_failure(
        "bundle-b", 0.25, RuntimeError("secret failure details"), cost_class="light"
    )
    profiler.record_bundle("bundle-c", 0.1, 1, 100)
    profiler.record_queue_depth(1)
    profiler.finish("completed")

    raw = profile_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    assert [record["record"] for record in records] == ["bundle", "bundle", "bundle", "summary"]
    ok_record, error_record, unclassified_record, summary = records
    assert ok_record["status"] == "ok"
    assert ok_record["item"] == "bundle-a"
    assert ok_record["output_count"] == 3
    assert ok_record["output_bytes"] == 300
    assert ok_record["version"] == 3
    assert ok_record["cost_class"] == "media"
    assert error_record["status"] == "error"
    assert error_record["error_class"] == "RuntimeError"
    assert error_record["cost_class"] == "light"
    assert unclassified_record["status"] == "ok"
    assert "cost_class" not in unclassified_record
    assert "secret" not in raw
    assert summary["status"] == "completed"
    assert summary["bundles"] == 3
    assert summary["failed"] == 1
    assert summary["total_extraction_sec"] == pytest.approx(0.85)
    assert summary["queue_depth_max"] == 1


def test_run_pipeline_logs_extraction_summary_and_writes_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []
    contents = {"first": b"first bytes", "second": b"second bytes"}
    install_pipeline_fakes(monkeypatch, contents, uploads)
    config = pipeline_config(tmp_path / "extracted")
    config.EXTRACTION_PROFILING = True
    items = [
        ("first-url", {"bundleName": "first"}),
        ("second-url", {"bundleName": "second"}),
    ]

    with caplog.at_level(logging.INFO, logger="asset_updater"):
        failed = asyncio.run(pipeline.run_pipeline(items, config, {}))

    assert failed == []
    assert "status=summary" in caplog.text
    assert "bundles=2" in caplog.text

    profiles = list((tmp_path / "extracted").glob("extraction-profile-*.jsonl"))
    assert len(profiles) == 1
    records = [json.loads(line) for line in profiles[0].read_text().splitlines()]
    bundle_records = [record for record in records if record["record"] == "bundle"]
    summary_records = [record for record in records if record["record"] == "summary"]
    assert {record["item"] for record in bundle_records} == {"first", "second"}
    assert all(record["status"] == "ok" for record in bundle_records)
    assert sum(record["output_bytes"] for record in bundle_records) == sum(
        len(content) for content in contents.values()
    )
    assert len(summary_records) == 1
    assert summary_records[0]["status"] == "completed"
    assert summary_records[0]["bundles"] == 2
    assert summary_records[0]["failed"] == 0


def test_run_pipeline_writes_no_profile_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []
    install_pipeline_fakes(monkeypatch, {"only": b"only bytes"}, uploads)
    config = pipeline_config(tmp_path / "extracted")

    failed = asyncio.run(pipeline.run_pipeline([("only-url", {"bundleName": "only"})], config, {}))

    assert failed == []
    assert list((tmp_path / "extracted").glob("extraction-profile-*.jsonl")) == []


def test_profiler_records_wait_distribution_and_class_statistics(tmp_path: Path) -> None:
    profiler = pipeline.ExtractionProfiler("testpipe", None)
    profiler.record_bundle("a", 0.5, 1, 100, cost_class="media", queue_wait_sec=2.0)
    profiler.record_bundle("b", 0.25, 1, 50, cost_class="media", queue_wait_sec=0.5)
    profiler.record_bundle("c", 1.0, 1, 10, queue_wait_sec=0.25)
    profiler.record_bundle_failure("d", 0.1, RuntimeError("x"), cost_class="light")
    profiler.record_scheduler_sample(1, 1)
    profiler.record_scheduler_sample(2, 2)

    snapshot = profiler.performance_snapshot(2, 4.0)

    assert snapshot["cost_classes"]["media"] == {
        "bundles": 2,
        "extraction_sec": 0.75,
        "bundles_per_sec": pytest.approx(2.6667, abs=1e-3),
    }
    assert snapshot["cost_classes"]["light"]["bundles"] == 1
    assert snapshot["cost_classes"]["unclassified"]["bundles"] == 1
    assert snapshot["wait_sec_avg"] == pytest.approx(0.9167, abs=1e-3)
    assert snapshot["wait_sec_max"] == 2.0
    assert snapshot["worker_utilisation"] == pytest.approx((0.5 + 0.25 + 1.0 + 0.1) / 8, abs=1e-4)
    assert snapshot["media_active_max"] == 2


def test_profiler_zero_duration_class_reports_null_throughput() -> None:
    profiler = pipeline.ExtractionProfiler("testpipe", None)
    profiler.record_bundle("a", 0.0, 0, 0, cost_class="light")

    snapshot = profiler.performance_snapshot()

    assert snapshot["cost_classes"]["light"]["bundles_per_sec"] is None


def test_profiler_writes_prometheus_textfile(tmp_path: Path) -> None:
    profiler = pipeline.ExtractionProfiler("testpipe", None)
    profiler.record_bundle("a", 0.5, 1, 100, cost_class="media", queue_wait_sec=0.1)
    profiler.record_bundle("b", 0.5, 1, 100, cost_class="light", queue_wait_sec=0.2)
    profiler.record_queue_depth(3)
    profiler.record_scheduler_sample(2, 1)

    metrics_path = tmp_path / "collector" / "sekai_updater.prom"
    profiler.write_prometheus_textfile(metrics_path, 2, 4.0)

    text = metrics_path.read_text(encoding="utf-8")
    assert 'sekai_updater_extraction_bundles_total{cost_class="light"} 1' in text
    assert 'sekai_updater_extraction_bundles_total{cost_class="media"} 1' in text
    assert "sekai_updater_extraction_failed_total 0" in text
    assert "sekai_updater_extraction_seconds_total 1.000000" in text
    assert "sekai_updater_queue_depth_max 3" in text
    assert "sekai_updater_media_active_max 1" in text
    assert "sekai_updater_worker_utilisation_ratio" in text
    assert "item=" not in text and "pipeline_id" not in text  # metrics stay label-free


def test_profiler_metrics_write_failure_is_swallowed(tmp_path: Path) -> None:
    profiler = pipeline.ExtractionProfiler("testpipe", None)
    profiler.record_bundle("a", 0.5, 1, 100)

    blocked_path = tmp_path / "occupied"
    blocked_path.write_bytes(b"not a directory")
    # Writing through an existing regular file as a parent directory fails.
    profiler.write_prometheus_textfile(blocked_path / "nested" / "m.prom", 1, 1.0)

    assert profiler.bundle_count == 1


def test_run_pipeline_records_queue_wait_input_bytes_and_class_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []
    contents = {"media/one": b"media bytes", "light/one": b"light bytes"}
    install_pipeline_fakes(monkeypatch, contents, uploads)
    config = pipeline_config(tmp_path / "extracted")
    config.EXTRACTION_PROFILING = True
    config.EXTRACT_SCHEDULER_MODE = "adaptive"
    config.EXTRACT_MEDIA_BUNDLE_HINTS = [r"^media/"]
    items = [
        ("media-url", {"bundleName": "media/one"}),
        ("light-url", {"bundleName": "light/one"}),
    ]

    with caplog.at_level(logging.INFO, logger="asset_updater"):
        failed = asyncio.run(pipeline.run_pipeline(items, config, {}))

    assert failed == []
    assert "status=report" in caplog.text
    assert "worker_utilisation=" in caplog.text

    profiles = list((tmp_path / "extracted").glob("extraction-profile-*.jsonl"))
    records = [json.loads(line) for line in profiles[0].read_text().splitlines()]
    bundle_records = [record for record in records if record["record"] == "bundle"]
    assert all(record["version"] == 3 for record in records)
    assert all("queue_wait_sec" in record for record in bundle_records)
    assert all(record["queue_wait_sec"] >= 0 for record in bundle_records)
    assert all(record["input_bytes"] == len(b"synthetic bundle") for record in bundle_records)
    assert {record["cost_class"] for record in bundle_records} == {"media", "light"}

    summary = next(record for record in records if record["record"] == "summary")
    assert set(summary["cost_classes"]) == {"light", "media"}
    # Saturation maxima need a run long enough to be sampled; the field must
    # simply exist (and stay 0) for sub-second test runs.
    assert summary["media_active_max"] >= 0
    assert 0 <= summary["worker_utilisation"] <= 1


def test_run_pipeline_ignores_corrupt_existing_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []
    install_pipeline_fakes(monkeypatch, {"only": b"only bytes"}, uploads)
    profile_path = tmp_path / "stale" / "profile.jsonl"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text('{"record": "bundle", "version": 1, TRUNCATED', encoding="utf-8")
    config = pipeline_config(tmp_path / "extracted")
    config.EXTRACTION_PROFILING = True
    config.EXTRACTION_PROFILE_PATH = profile_path

    failed = asyncio.run(pipeline.run_pipeline([("only-url", {"bundleName": "only"})], config, {}))

    assert failed == []
    lines = profile_path.read_text().splitlines()
    records = [json.loads(line) for line in lines[1:]]  # the corrupt first line is left untouched
    assert [record["record"] for record in records] == ["bundle", "summary"]
    assert records[-1]["status"] == "completed"


def test_run_pipeline_survives_unwritable_profile_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []
    install_pipeline_fakes(monkeypatch, {"only": b"only bytes"}, uploads)
    blocked = tmp_path / "occupied"
    blocked.write_bytes(b"regular file, not a directory")
    config = pipeline_config(tmp_path / "extracted")
    config.EXTRACTION_PROFILING = True
    config.EXTRACTION_PROFILE_PATH = blocked / "nested" / "profile.jsonl"

    with caplog.at_level(logging.WARNING, logger="asset_updater"):
        failed = asyncio.run(
            pipeline.run_pipeline([("only-url", {"bundleName": "only"})], config, {})
        )

    assert failed == []
    assert "profile_write_failed" in caplog.text


def test_run_pipeline_skips_prometheus_export_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []
    install_pipeline_fakes(monkeypatch, {"only": b"only bytes"}, uploads)
    config = pipeline_config(tmp_path / "extracted")

    failed = asyncio.run(pipeline.run_pipeline([("only-url", {"bundleName": "only"})], config, {}))

    assert failed == []
    assert list(tmp_path.rglob("*.prom")) == []


def test_run_pipeline_writes_prometheus_textfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uploads: list[tuple[str, Path, Path, bytes]] = []
    install_pipeline_fakes(monkeypatch, {"only": b"only bytes"}, uploads)
    config = pipeline_config(tmp_path / "extracted")
    config.PROMETHEUS_METRICS_PATH = tmp_path / "collector" / "sekai_updater.prom"

    failed = asyncio.run(pipeline.run_pipeline([("only-url", {"bundleName": "only"})], config, {}))

    assert failed == []
    text = (tmp_path / "collector" / "sekai_updater.prom").read_text(encoding="utf-8")
    assert 'sekai_updater_extraction_bundles_total{cost_class="unclassified"} 1' in text
    assert "sekai_updater_worker_utilisation_ratio" in text


def test_apply_profile_option_enables_timestamped_path() -> None:
    from updater.cli.entry import apply_profile_option

    config = SimpleNamespace(EXTRACTION_PROFILING=False, EXTRACTION_PROFILE_PATH=None)
    apply_profile_option(config, True)

    assert config.EXTRACTION_PROFILING is True
    assert config.EXTRACTION_PROFILE_PATH.startswith("extraction-profile-")
    assert config.EXTRACTION_PROFILE_PATH.endswith(".jsonl")


def test_apply_profile_option_respects_explicit_path_and_noop() -> None:
    from updater.cli.entry import apply_profile_option

    explicit = SimpleNamespace(EXTRACTION_PROFILING=False, EXTRACTION_PROFILE_PATH="keep.jsonl")
    apply_profile_option(explicit, True)
    assert explicit.EXTRACTION_PROFILING is True
    assert explicit.EXTRACTION_PROFILE_PATH == "keep.jsonl"

    disabled = SimpleNamespace(EXTRACTION_PROFILING=False, EXTRACTION_PROFILE_PATH=None)
    apply_profile_option(disabled, False)
    assert disabled.EXTRACTION_PROFILING is False
    assert disabled.EXTRACTION_PROFILE_PATH is None
