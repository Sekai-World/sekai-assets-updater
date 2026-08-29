"""The loaded-config cell, config validation, and state-path resolution."""

import logging
import os
import shutil
from typing import Any, Optional

from updater.model import ConfigLike
from updater.modes import (
    get_enabled_specialized_modes,
)
from updater.state import (
    StatePaths,
)

logger = logging.getLogger("asset_updater")


config: Optional[ConfigLike] = None


class _StatePathConfig:
    """Read-through config view with the active mode's durable state paths."""

    def __init__(self, base: ConfigLike, paths: StatePaths) -> None:
        self._base = base
        path_type = type(base.DL_LIST_CACHE_PATH)
        self.DL_LIST_CACHE_PATH = path_type(paths.queue)
        self.ASSET_BUNDLE_INFO_CACHE_PATH = path_type(paths.asset_metadata)
        self.GAME_VERSION_JSON_CACHE_PATH = path_type(paths.game_version)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def require_config() -> ConfigLike:
    if config is None:
        raise ImportError(
            "Config module not loaded. Please run the script with the config argument."
        )
    return config


def _validate_positive_settings(cfg: ConfigLike, errors: list[str]) -> None:
    concurrency_names = (
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
    for name in concurrency_names:
        value = getattr(cfg, name, None)
        if type(value) is not int or value <= 0:
            errors.append(f"{name} must be a positive integer (got {value!r})")

    max_retries = getattr(cfg, "DOWNLOAD_MAX_RETRIES", None)
    if type(max_retries) is not int or max_retries < 1:
        errors.append(
            f"DOWNLOAD_MAX_RETRIES must be an integer of at least 1 (got {max_retries!r})"
        )


def _validate_external_process_timeout(cfg: ConfigLike, errors: list[str]) -> None:
    timeout = getattr(cfg, "EXTERNAL_PROCESS_TIMEOUT", None)
    try:
        valid_timeout = float(timeout) > 0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        valid_timeout = False
    if not valid_timeout:
        errors.append(f"EXTERNAL_PROCESS_TIMEOUT must be a positive number (got {timeout!r})")


def _validate_encryption_settings(cfg: ConfigLike, errors: list[str]) -> None:
    key = getattr(cfg, "AES_KEY", None)
    iv = getattr(cfg, "AES_IV", None)
    if not isinstance(key, bytes) or len(key) not in {16, 24, 32}:
        errors.append("AES_KEY must be bytes with length 16, 24, or 32")
    if not isinstance(iv, bytes) or len(iv) != 16:
        errors.append("AES_IV must be bytes with length 16")


def _require_program(program: object, label: str, errors: list[str]) -> None:
    if not program:
        errors.append(f"{label} executable is not configured")
    elif not isinstance(program, str):
        errors.append(f"{label} executable must be a string")
    elif shutil.which(program) is None and not (
        os.path.isfile(program) and os.access(program, os.X_OK)
    ):
        errors.append(f"{label} executable not found: {program}")


def _validate_program_settings(cfg: ConfigLike, mode: str, errors: list[str]) -> None:
    _require_program("ffmpeg", "ffmpeg", errors)
    backend = str(getattr(cfg, "HCA_DECODE_BACKEND", "auto")).strip().lower()
    if backend == "vgmstream":
        _require_program(os.environ.get("VGMSTREAM_CLI", "vgmstream-cli"), "vgmstream-cli", errors)

    storage_targets = getattr(cfg, "ASSET_REMOTE_STORAGE", None) or []
    for index, storage in enumerate(storage_targets):
        if storage.get("type") == "normal":
            _require_program(storage.get("program"), f"upload storage {index}", errors)

    enabled_specialized_modes = get_enabled_specialized_modes(mode, cfg)
    for specialized_mode in enabled_specialized_modes:
        for index, storage in enumerate(storage_targets):
            if storage.get("type") == specialized_mode:
                _require_program(
                    storage.get("program"),
                    f"{specialized_mode} upload storage {index}",
                    errors,
                )

    _validate_specialized_settings(cfg, storage_targets, enabled_specialized_modes, errors)


def _validate_specialized_settings(
    cfg: ConfigLike,
    storage_targets: list,
    enabled_specialized_modes: tuple[str, ...],
    errors: list[str],
) -> None:
    if "live2d" in enabled_specialized_modes:
        unity_version = getattr(cfg, "UNITY_VERSION", None)
        if not isinstance(unity_version, str) or not unity_version.strip():
            errors.append("LIVE2D post-processing requires UNITY_VERSION")

    if (
        "charts" in enabled_specialized_modes
        and getattr(cfg, "ASSET_LOCAL_EXTRACTED_DIR", None) is None
    ):
        if not any(storage.get("type") == "normal" for storage in storage_targets):
            errors.append(
                "Charts post-processing requires ASSET_LOCAL_EXTRACTED_DIR or a normal "
                "ASSET_REMOTE_STORAGE target for chart sources"
            )


def validate_config(cfg: ConfigLike, mode: str = "assets") -> None:
    """Reject unsafe or unusable runtime settings before starting the pipeline."""
    errors: list[str] = []
    _validate_positive_settings(cfg, errors)
    _validate_external_process_timeout(cfg, errors)
    _validate_encryption_settings(cfg, errors)
    _validate_program_settings(cfg, mode, errors)
    if errors:
        raise ValueError("Invalid configuration:\n- " + "\n- ".join(errors))
