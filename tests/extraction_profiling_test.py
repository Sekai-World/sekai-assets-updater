from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

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

    with pytest.raises(RuntimeError, match="synthetic extraction failure"):
        asyncio.run(pipeline.extract_single_bundle(artifact, config))

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
    profiler.record_bundle("bundle-a", 0.5, 3, 300)
    profiler.record_bundle_failure("bundle-b", 0.25, RuntimeError("secret failure details"))
    profiler.record_queue_depth(1)
    profiler.finish("completed")

    raw = profile_path.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    assert [record["record"] for record in records] == ["bundle", "bundle", "summary"]
    ok_record, error_record, summary = records
    assert ok_record["status"] == "ok"
    assert ok_record["item"] == "bundle-a"
    assert ok_record["output_count"] == 3
    assert ok_record["output_bytes"] == 300
    assert ok_record["pipeline_id"] == "testpipe"
    assert error_record["status"] == "error"
    assert error_record["error_class"] == "RuntimeError"
    assert "secret" not in raw
    assert summary["status"] == "completed"
    assert summary["bundles"] == 2
    assert summary["failed"] == 1
    assert summary["total_extraction_sec"] == pytest.approx(0.75)
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
