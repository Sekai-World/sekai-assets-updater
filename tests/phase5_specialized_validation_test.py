from types import SimpleNamespace
from typing import Any

import pytest

import main


def _valid_config(**overrides) -> SimpleNamespace:
    values: dict[str, Any] = {
        name: 1
        for name in (
            "MAX_CONCURRENCY",
            "MAX_CONCURRENCY_DOWNLOADS",
            "MAX_CONCURRENCY_EXTRACTS",
            "MAX_CONCURRENCY_UPLOAD_STAGE",
            "PIPELINE_STAGE_QUEUE_SIZE",
            "MAX_CONCURRENT_AUDIO_FILES",
            "MAX_CONCURRENCY_HCA_DECODES",
            "MAX_CONCURRENCY_AUDIO_ENCODERS",
            "MAX_CONCURRENCY_AUDIO_TRANSCODES",
            "MAX_CONCURRENCY_VIDEO_TRANSCODES",
            "MAX_CONCURRENCY_USM_DEMUXES",
            "MAX_CONCURRENCY_UPLOADS",
        )
    }
    values.update(
        EXTERNAL_PROCESS_TIMEOUT=10,
        DOWNLOAD_MAX_RETRIES=3,
        AES_KEY=b"0123456789012345",
        AES_IV=b"0123456789012345",
        HCA_DECODE_BACKEND="auto",
        ASSET_REMOTE_STORAGE=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_validate_config_keeps_normal_only_mode_compatible(monkeypatch) -> None:
    monkeypatch.setattr(main.shutil, "which", lambda _program: "/usr/bin/fake")

    main.validate_config(_valid_config())  # type: ignore[arg-type]


def test_validate_config_checks_enabled_live2d_storage_and_prerequisite(monkeypatch) -> None:
    monkeypatch.setattr(
        main.shutil, "which", lambda program: None if program == "missing" else "/bin/true"
    )
    config = _valid_config(
        ENABLE_LIVE2D_POSTPROCESS=True,
        UNITY_VERSION="",
        ASSET_REMOTE_STORAGE=[{"type": "live2d", "program": "missing"}],
    )

    with pytest.raises(ValueError) as caught:
        main.validate_config(config)  # type: ignore[arg-type]

    assert "live2d upload storage 0 executable not found: missing" in str(caught.value)
    assert "LIVE2D post-processing requires UNITY_VERSION" in str(caught.value)


def test_validate_config_checks_forced_charts_storage_and_source_prerequisite(monkeypatch) -> None:
    monkeypatch.setattr(main.shutil, "which", lambda _program: None)
    config = _valid_config(ASSET_REMOTE_STORAGE=[{"type": "charts", "program": "missing"}])

    with pytest.raises(ValueError) as caught:
        main.validate_config(config, mode="charts")  # type: ignore[arg-type]

    assert "charts upload storage 0 executable not found: missing" in str(caught.value)
    assert "Charts post-processing requires ASSET_LOCAL_EXTRACTED_DIR or a normal" in str(
        caught.value
    )


def test_validate_config_allows_charts_with_configured_extraction_directory(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(main.shutil, "which", lambda _program: "/usr/bin/fake")
    config = _valid_config(ASSET_LOCAL_EXTRACTED_DIR=tmp_path)

    main.validate_config(config, mode="charts")  # type: ignore[arg-type]
