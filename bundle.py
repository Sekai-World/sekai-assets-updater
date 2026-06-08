"""This module contains functions to download, deobfuscate, and extract asset bundles."""

import atexit
import asyncio
import logging
import os
import re
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from io import BytesIO
from pathlib import Path as StdPath
from pathlib import PurePosixPath
from typing import Any, Dict, List, Tuple

import aiohttp
import json_compat as json
from anyio import Path, open_file
from PIL import Image

from assetstudio_ffi import (
    AssetStudioRead,
    export_assetstudio_objects,
    image_from_payload,
    safe_payload_bundle_path,
)
from constants import (
    UNITY_FS_BUILT_IN_ALT_CONTAINER_BASE,
    UNITY_FS_BUILT_IN_CONTAINER_BASE,
    UNITY_FS_CONTAINER_BASE,
)
from helpers import get_download_max_retries, get_request_timeout
from utils.acb import extract_acb
from utils.hca import decode_hca_file
from utils.playable import extract_playable_from_objects

logger = logging.getLogger("live2d")

_ffmpeg_video_encoder_cache: tuple[str | None, list[str]] | None = None
_audio_file_semaphore_cache: tuple[int, asyncio.Semaphore] | None = None
_hca_decode_semaphore_cache: tuple[int, asyncio.Semaphore] | None = None
_audio_encoder_semaphore_cache: tuple[int, asyncio.Semaphore] | None = None
_video_transcode_semaphore_cache: tuple[int, asyncio.Semaphore] | None = None
_extract_process_pool_cache: tuple[int, ProcessPoolExecutor] | None = None
_audio_process_pool_cache: tuple[int, ProcessPoolExecutor] | None = None
_vgmstream_cli_cache: str | None = None
_vgmstream_cli_checked = False
_hca_decoder_reported: str | None = None


def _sanitize_concurrency(value) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _get_legacy_audio_transcode_concurrency(config) -> int:
    return _sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_AUDIO_TRANSCODES",
            getattr(config, "MAX_CONCURRENCY", 1),
        )
    )


def _get_max_concurrent_audio_files(config) -> int:
    return _sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENT_AUDIO_FILES",
            _get_legacy_audio_transcode_concurrency(config),
        )
    )


def _get_hca_decode_concurrency(config) -> int:
    return _sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_HCA_DECODES",
            _get_legacy_audio_transcode_concurrency(config),
        )
    )


def _get_audio_encoder_concurrency(config) -> int:
    return _sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_AUDIO_ENCODERS",
            _get_legacy_audio_transcode_concurrency(config),
        )
    )


def _get_video_transcode_concurrency(config) -> int:
    return _sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_VIDEO_TRANSCODES",
            getattr(config, "MAX_CONCURRENCY", 1),
        )
    )


def _get_extract_process_concurrency(config) -> int:
    return _sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_EXTRACTS",
            getattr(config, "MAX_CONCURRENCY", 1),
        )
    )


def _get_texture_output_formats(config) -> tuple[str, ...]:
    value = getattr(config, "TEXTURE_OUTPUT_FORMATS", ("png", "webp"))
    if isinstance(value, str):
        formats = [part.strip().lower().removeprefix(".") for part in value.split(",")]
    else:
        formats = [
            str(part).strip().lower().removeprefix(".")
            for part in value
        ]

    valid_formats = []
    for image_format in formats:
        if image_format in {"png", "webp"} and image_format not in valid_formats:
            valid_formats.append(image_format)

    if not valid_formats and formats:
        logger.warning("No valid TEXTURE_OUTPUT_FORMATS found, skipping texture export")

    return tuple(valid_formats)


def _get_assetstudio_config(config) -> dict[str, Any]:
    return {
        "ASSET_STUDIO_FFI_LIBRARY_PATH": getattr(
            config,
            "ASSET_STUDIO_FFI_LIBRARY_PATH",
            None,
        ),
        "ASSET_STUDIO_FFI_WORKER_PATH": getattr(
            config,
            "ASSET_STUDIO_FFI_WORKER_PATH",
            None,
        ),
        "ASSET_STUDIO_FFI_READ_BATCH_SIZE": getattr(
            config,
            "ASSET_STUDIO_FFI_READ_BATCH_SIZE",
            64,
        ),
    }


def _get_shared_audio_file_semaphore(config) -> asyncio.Semaphore:
    global _audio_file_semaphore_cache

    concurrency = _get_max_concurrent_audio_files(config)
    if (
        _audio_file_semaphore_cache is None
        or _audio_file_semaphore_cache[0] != concurrency
    ):
        _audio_file_semaphore_cache = (
            concurrency,
            asyncio.Semaphore(concurrency),
        )
    return _audio_file_semaphore_cache[1]


def _get_shared_hca_decode_semaphore(config) -> asyncio.Semaphore:
    global _hca_decode_semaphore_cache

    concurrency = _get_hca_decode_concurrency(config)
    if (
        _hca_decode_semaphore_cache is None
        or _hca_decode_semaphore_cache[0] != concurrency
    ):
        _hca_decode_semaphore_cache = (
            concurrency,
            asyncio.Semaphore(concurrency),
        )
    return _hca_decode_semaphore_cache[1]


def _get_shared_audio_encoder_semaphore(config) -> asyncio.Semaphore:
    global _audio_encoder_semaphore_cache

    concurrency = _get_audio_encoder_concurrency(config)
    if (
        _audio_encoder_semaphore_cache is None
        or _audio_encoder_semaphore_cache[0] != concurrency
    ):
        _audio_encoder_semaphore_cache = (
            concurrency,
            asyncio.Semaphore(concurrency),
        )
    return _audio_encoder_semaphore_cache[1]


def _get_shared_video_transcode_semaphore(config) -> asyncio.Semaphore:
    global _video_transcode_semaphore_cache

    concurrency = _get_video_transcode_concurrency(config)
    if (
        _video_transcode_semaphore_cache is None
        or _video_transcode_semaphore_cache[0] != concurrency
    ):
        _video_transcode_semaphore_cache = (
            concurrency,
            asyncio.Semaphore(concurrency),
        )
    return _video_transcode_semaphore_cache[1]


def _shutdown_audio_process_pool() -> None:
    global _audio_process_pool_cache

    if _audio_process_pool_cache is None:
        return

    _, executor = _audio_process_pool_cache
    _audio_process_pool_cache = None
    executor.shutdown(wait=False, cancel_futures=False)


def _shutdown_extract_process_pool() -> None:
    global _extract_process_pool_cache

    if _extract_process_pool_cache is None:
        return

    _, executor = _extract_process_pool_cache
    _extract_process_pool_cache = None
    executor.shutdown(wait=False, cancel_futures=False)


atexit.register(_shutdown_extract_process_pool)
atexit.register(_shutdown_audio_process_pool)


def _get_shared_extract_process_pool(config) -> ProcessPoolExecutor:
    global _extract_process_pool_cache

    concurrency = _get_extract_process_concurrency(config)
    if (
        _extract_process_pool_cache is None
        or _extract_process_pool_cache[0] != concurrency
    ):
        if _extract_process_pool_cache is not None:
            _extract_process_pool_cache[1].shutdown(
                wait=False,
                cancel_futures=False,
            )
        _extract_process_pool_cache = (
            concurrency,
            ProcessPoolExecutor(max_workers=concurrency),
        )
    return _extract_process_pool_cache[1]


def _get_shared_audio_process_pool(config) -> ProcessPoolExecutor:
    global _audio_process_pool_cache

    concurrency = _get_hca_decode_concurrency(config)
    if (
        _audio_process_pool_cache is None
        or _audio_process_pool_cache[0] != concurrency
    ):
        if _audio_process_pool_cache is not None:
            _audio_process_pool_cache[1].shutdown(wait=False, cancel_futures=False)
        _audio_process_pool_cache = (
            concurrency,
            ProcessPoolExecutor(max_workers=concurrency),
        )
    return _audio_process_pool_cache[1]


def _get_hca_decode_backend(config) -> str:
    backend = str(getattr(config, "HCA_DECODE_BACKEND", "auto")).strip().lower()
    if backend in {"auto", "python", "vgmstream"}:
        return backend

    logger.warning("Unknown HCA_DECODE_BACKEND=%r, falling back to auto", backend)
    return "auto"


def _get_vgmstream_cli() -> str | None:
    global _vgmstream_cli_cache, _vgmstream_cli_checked

    if _vgmstream_cli_checked:
        return _vgmstream_cli_cache

    candidates: list[str] = []
    env_candidate = os.environ.get("VGMSTREAM_CLI")
    if env_candidate:
        candidates.append(env_candidate)
    candidates.append("vgmstream-cli")

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            _vgmstream_cli_cache = resolved
            _vgmstream_cli_checked = True
            return _vgmstream_cli_cache
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            _vgmstream_cli_cache = candidate
            _vgmstream_cli_checked = True
            return _vgmstream_cli_cache

    _vgmstream_cli_checked = True
    return None


def _report_hca_decoder(message: str) -> None:
    global _hca_decoder_reported

    if _hca_decoder_reported == message:
        return

    _hca_decoder_reported = message
    logger.info(message)


def _discard_exported_file(exported_files: list[Path], file_path: Path) -> None:
    try:
        exported_files.remove(file_path)
        return
    except ValueError:
        pass

    file_path_lower = file_path.with_name(file_path.name.lower())
    try:
        exported_files.remove(file_path_lower)
    except ValueError:
        logger.debug("%s not tracked in exported_files, skip removal", file_path)


async def _resolve_existing_path(
    expected_path: Path,
    save_dir: Path,
    expected_suffix: str | None = None,
) -> Path:
    if await expected_path.exists():
        return expected_path

    expected_name_lower = expected_path.name.lower()
    expected_path_lower = expected_path.with_name(expected_name_lower)
    if await expected_path_lower.exists():
        logger.debug("Found %s instead of %s", expected_path_lower, expected_path.name)
        return expected_path_lower

    candidate_paths = [
        path
        async for path in save_dir.iterdir()
        if path.name.lower() == expected_name_lower
        and (expected_suffix is None or path.suffix.lower() == expected_suffix.lower())
    ]
    if len(candidate_paths) == 1:
        logger.debug(
            "Found %s instead of %s via case-insensitive lookup",
            candidate_paths[0],
            expected_path.name,
        )
        return candidate_paths[0]

    raise FileNotFoundError(f"{expected_path} not found in {save_dir}")


async def _resolve_existing_usm_path(expected_path: Path, save_dir: Path) -> Path:
    try:
        return await _resolve_existing_path(expected_path, save_dir, ".usm")
    except FileNotFoundError:
        pass

    candidate_paths = [
        path async for path in save_dir.iterdir() if path.suffix.lower() == ".usm"
    ]
    if len(candidate_paths) == 1:
        logger.warning(
            "Expected %s in %s, falling back to discovered usm %s",
            expected_path.name,
            save_dir,
            candidate_paths[0].name,
        )
        return candidate_paths[0]

    raise FileNotFoundError(f"{expected_path} not found in {save_dir}")


async def _get_ffmpeg_video_encoder() -> tuple[str | None, list[str]]:
    """Detect a usable hardware H.264 encoder for the current platform."""
    global _ffmpeg_video_encoder_cache

    if _ffmpeg_video_encoder_cache is not None:
        return _ffmpeg_video_encoder_cache

    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-encoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        _ffmpeg_video_encoder_cache = (None, [])
        return _ffmpeg_video_encoder_cache

    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.warning(
            "Failed to inspect ffmpeg encoders, falling back to software encoding: %s",
            stderr.decode(errors="ignore").strip(),
        )
        _ffmpeg_video_encoder_cache = (None, [])
        return _ffmpeg_video_encoder_cache

    available_encoders = stdout.decode(errors="ignore")
    candidates: list[tuple[str, list[str]]] = []

    if sys.platform == "darwin":
        candidates.append(("h264_videotoolbox", ["-c:v", "h264_videotoolbox"]))
    elif sys.platform.startswith("linux"):
        if await Path("/dev/nvidia0").exists() or await Path("/dev/nvidiactl").exists():
            candidates.append(("h264_nvenc", ["-c:v", "h264_nvenc"]))
        if await Path("/dev/dri/renderD128").exists():
            candidates.append(
                (
                    "h264_vaapi",
                    [
                        "-vaapi_device",
                        "/dev/dri/renderD128",
                        "-vf",
                        "format=nv12,hwupload",
                        "-c:v",
                        "h264_vaapi",
                    ],
                )
            )
    elif sys.platform == "win32":
        candidates.extend(
            [
                ("h264_nvenc", ["-c:v", "h264_nvenc"]),
                ("h264_amf", ["-c:v", "h264_amf"]),
            ]
        )

    for encoder_name, encoder_args in candidates:
        if encoder_name in available_encoders:
            logger.info("Using ffmpeg hardware video encoder: %s", encoder_name)
            _ffmpeg_video_encoder_cache = (encoder_name, encoder_args)
            return _ffmpeg_video_encoder_cache

    logger.info("No usable ffmpeg hardware video encoder detected, using software encoding")
    _ffmpeg_video_encoder_cache = (None, [])
    return _ffmpeg_video_encoder_cache


def _disable_ffmpeg_video_encoder() -> None:
    global _ffmpeg_video_encoder_cache
    _ffmpeg_video_encoder_cache = (None, [])


async def _run_ffmpeg_usm_to_mp4(
    input_path: Path,
    output_path: Path,
) -> tuple[asyncio.subprocess.Process, str | None]:
    encoder_name, encoder_args = await _get_ffmpeg_video_encoder()
    command = [
        "ffmpeg",
        "-loglevel",
        "panic",
        "-y",
        "-i",
        input_path.as_posix(),
    ]

    if encoder_args:
        command.extend(encoder_args)
    else:
        command.extend(["-tune", "animation"])

    command.append(output_path.as_posix())
    process = await asyncio.create_subprocess_exec(*command)
    return process, encoder_name


async def _run_hca_to_wav_with_python(
    input_path: Path,
    output_path: Path,
    config,
) -> bool:
    try:
        _report_hca_decoder(
            "Using Python HCA decoder via process pool "
            f"({_get_hca_decode_concurrency(config)} workers)"
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _get_shared_audio_process_pool(config),
            decode_hca_file,
            input_path.as_posix(),
            output_path.as_posix(),
        )
    except Exception:
        logger.exception("Failed to decode %s with the Python HCA decoder", input_path)
        return False
    return True


async def _run_hca_to_wav_with_vgmstream(
    input_path: Path,
    output_path: Path,
) -> bool:
    vgmstream_cli = _get_vgmstream_cli()
    if vgmstream_cli is None:
        return False

    _report_hca_decoder(f"Using vgmstream-cli for HCA decoding: {vgmstream_cli}")

    try:
        if await output_path.exists():
            await output_path.unlink()

        process = await asyncio.create_subprocess_exec(
            vgmstream_cli,
            "-i",
            "-o",
            output_path.as_posix(),
            input_path.as_posix(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except Exception:
        logger.exception("Failed to decode %s with vgmstream-cli", input_path)
        return False

    if process.returncode != 0 or not await output_path.exists():
        error_output = stderr.decode(errors="ignore").strip() or stdout.decode(
            errors="ignore"
        ).strip()
        logger.warning(
            "vgmstream-cli failed to decode %s: %s",
            input_path,
            error_output or f"exit code {process.returncode}",
        )
        return False

    return True


async def _run_hca_to_wav(
    input_path: Path,
    output_path: Path,
    config,
) -> bool:
    async with _get_shared_hca_decode_semaphore(config):
        backend = _get_hca_decode_backend(config)

        if backend in {"auto", "vgmstream"}:
            if await _run_hca_to_wav_with_vgmstream(input_path, output_path):
                return True
            if backend == "vgmstream":
                logger.warning(
                    "Falling back to the Python HCA decoder for %s after vgmstream failure",
                    input_path,
                )

        return await _run_hca_to_wav_with_python(input_path, output_path, config)


async def _run_ffmpeg_audio_encode(
    input_path: Path,
    output_path: Path,
    config,
) -> bool:
    async with _get_shared_audio_encoder_semaphore(config):
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel",
            "panic",
            "-y",
            "-i",
            input_path.as_posix(),
            output_path.as_posix(),
        )
        await process.wait()
        return process.returncode == 0


async def _process_extracted_audio_file(
    extracted_audio_file: str,
    save_dir: Path,
    config,
    file_semaphore: asyncio.Semaphore,
) -> list[Path]:
    async with file_semaphore:
        extracted_audio_file_path = Path(extracted_audio_file)
        exported_audio_files: list[Path] = []

        try:
            if not await extracted_audio_file_path.exists():
                logger.warning(
                    "%s not found in %s", extracted_audio_file_path, save_dir
                )
                return exported_audio_files

            if (await extracted_audio_file_path.stat()).st_size == 0:
                logger.warning("%s is empty, skipping", extracted_audio_file_path)
                return exported_audio_files

            wav_path = extracted_audio_file_path.with_suffix(".wav")

            # hca -> wav
            if not await _run_hca_to_wav(extracted_audio_file_path, wav_path, config):
                logger.warning("Failed to convert %s to wav", extracted_audio_file_path)
                return exported_audio_files

            await extracted_audio_file_path.unlink()
            logger.debug(
                "Converted %s to wav and removed the original file",
                extracted_audio_file_path,
            )
            exported_audio_files.append(wav_path)

            encode_tasks = []

            mp3_path = extracted_audio_file_path.with_suffix(".mp3")
            encode_tasks.append(
                (
                    "mp3",
                    mp3_path,
                    _run_ffmpeg_audio_encode(wav_path, mp3_path, config),
                )
            )

            if "music" in save_dir.parts:
                flac_path = extracted_audio_file_path.with_suffix(".flac")
                encode_tasks.append(
                    (
                        "flac",
                        flac_path,
                        _run_ffmpeg_audio_encode(wav_path, flac_path, config),
                    )
                )

            encode_results = await asyncio.gather(
                *(task for _, _, task in encode_tasks),
                return_exceptions=True,
            )
            for (format_name, output_path, _), result in zip(
                encode_tasks,
                encode_results,
            ):
                if isinstance(result, Exception):
                    logger.error(
                        "Failed to convert %s to %s",
                        wav_path,
                        format_name,
                        exc_info=(type(result), result, result.__traceback__),
                    )
                elif not result:
                    logger.warning("Failed to convert %s to %s", wav_path, format_name)
                else:
                    logger.debug("Converted %s to %s", wav_path, format_name)
                    exported_audio_files.append(output_path)
        except Exception as exc:
            logger.exception(
                "Failed processing extracted audio %s: %s",
                extracted_audio_file_path,
                exc,
            )

        return exported_audio_files


def _build_unityfs_save_path(unityfs_path: str, extracted_save_path: StdPath) -> StdPath:
    source_path = PurePosixPath(unityfs_path)
    base_paths = (
        PurePosixPath(UNITY_FS_CONTAINER_BASE.as_posix()),
        PurePosixPath(UNITY_FS_BUILT_IN_CONTAINER_BASE.as_posix()),
        PurePosixPath(UNITY_FS_BUILT_IN_ALT_CONTAINER_BASE.as_posix()),
    )

    for index, base_path in enumerate(base_paths):
        try:
            relpath = source_path.relative_to(base_path)
        except ValueError:
            continue

        if index == 0:
            relpath = PurePosixPath(*relpath.parts[1:])
        return extracted_save_path.joinpath(*relpath.parts)

    raise ValueError(f"Failed to get relative path for {unityfs_path}")


def _discard_exported_file_sync(exported_files: list[StdPath], file_path: StdPath) -> None:
    try:
        exported_files.remove(file_path)
        return
    except ValueError:
        pass

    file_path_lower = file_path.with_name(file_path.name.lower())
    try:
        exported_files.remove(file_path_lower)
    except ValueError:
        logger.debug("%s not tracked in exported_files, skip removal", file_path)


def _resolve_existing_path_sync(
    expected_path: StdPath,
    save_dir: StdPath,
    expected_suffix: str | None = None,
) -> StdPath:
    if expected_path.exists():
        return expected_path

    expected_name_lower = expected_path.name.lower()
    expected_path_lower = expected_path.with_name(expected_name_lower)
    if expected_path_lower.exists():
        logger.debug("Found %s instead of %s", expected_path_lower, expected_path.name)
        return expected_path_lower

    candidate_paths = [
        path
        for path in save_dir.iterdir()
        if path.name.lower() == expected_name_lower
        and (expected_suffix is None or path.suffix.lower() == expected_suffix.lower())
    ]
    if len(candidate_paths) == 1:
        logger.debug(
            "Found %s instead of %s via case-insensitive lookup",
            candidate_paths[0],
            expected_path.name,
        )
        return candidate_paths[0]

    raise FileNotFoundError(f"{expected_path} not found in {save_dir}")


def _resolve_existing_usm_path_sync(expected_path: StdPath, save_dir: StdPath) -> StdPath:
    try:
        return _resolve_existing_path_sync(expected_path, save_dir, ".usm")
    except FileNotFoundError:
        pass

    candidate_paths = [
        path for path in save_dir.iterdir() if path.suffix.lower() == ".usm"
    ]
    if len(candidate_paths) == 1:
        logger.warning(
            "Expected %s in %s, falling back to discovered usm %s",
            expected_path.name,
            save_dir,
            candidate_paths[0].name,
        )
        return candidate_paths[0]

    raise FileNotFoundError(f"{expected_path} not found in {save_dir}")


def _save_image_formats(
    image: Image.Image,
    save_path: StdPath,
    texture_output_formats: tuple[str, ...],
) -> list[StdPath]:
    saved_paths: list[StdPath] = []
    for image_format in texture_output_formats:
        output_path = save_path.with_suffix(f".{image_format}")
        logger.debug("Saving texture to %s", output_path)
        image.save(output_path)
        saved_paths.append(output_path)
    return saved_paths


def _asset_container_path(read: AssetStudioRead) -> str:
    return (
        read.asset.get("container")
        or read.asset.get("name")
        or f"{read.asset.get('path_id', 'asset')}"
    )


def _asset_type(read: AssetStudioRead) -> str:
    return str(read.asset.get("type") or read.asset.get("asset_type") or "")


def _asset_save_path(read: AssetStudioRead, output_root: StdPath) -> StdPath:
    save_path = _build_unityfs_save_path(_asset_container_path(read), output_root)
    return save_path.with_name(save_path.name.strip())


def _write_payload_bundle(target: StdPath, payload: bytes) -> list[StdPath]:
    written: list[StdPath] = []
    for name, data in safe_payload_bundle_path_payloads(payload):
        entry_target = target.parent / target.stem / name
        entry_target.parent.mkdir(parents=True, exist_ok=True)
        entry_target.write_bytes(data)
        written.append(entry_target)
    return written


def safe_payload_bundle_path_payloads(payload: bytes) -> list[tuple[StdPath, bytes]]:
    from assetstudio_ffi import parse_payload_bundle

    return [
        (StdPath(safe_payload_bundle_path(name)), data)
        for name, data in parse_payload_bundle(payload).items()
    ]


def _write_assetstudio_read(
    read: AssetStudioRead,
    output_root: StdPath,
    texture_output_formats: tuple[str, ...],
    all_objects: dict[int, dict[str, Any]],
) -> tuple[list[StdPath], dict[str, Any] | None]:
    save_path = _asset_save_path(read, output_root)
    save_dir = save_path.parent
    save_dir.mkdir(parents=True, exist_ok=True)

    asset_type = _asset_type(read)
    payload_kind = read.response.get("payload_kind") or ""
    suggested_extension = read.response.get("suggested_extension") or ""

    if payload_kind == "typetree_json":
        tree = json.loads(read.payload)
        if _asset_type(read) == "MonoScript":
            return [], tree
        is_playable = _asset_container_path(read).endswith(".playable")
        if is_playable:
            if not isinstance(tree, dict) or "m_Tracks" not in tree:
                return [], tree
            tree = extract_playable_from_objects(
                all_objects,
                int(read.asset.get("path_id") or 0),
                _asset_container_path(read),
                tree,
            )
        if not is_playable:
            save_path = save_path.with_suffix(".json")
        save_path.write_bytes(json.dumps(tree, option=json.OPT_INDENT_2))
        return [save_path], tree

    if payload_kind == "text_bytes":
        if save_path.suffix == ".bytes":
            save_path = save_path.with_suffix("")
        save_path.write_bytes(read.payload)
        return [save_path], None

    if payload_kind in {"image_raw_rgba", "image_bmp"}:
        image = image_from_payload(read.payload)
        return _save_image_formats(image, save_path, texture_output_formats), None

    if payload_kind == "image_array_bundle_raw_rgba":
        written: list[StdPath] = []
        for name, data in safe_payload_bundle_path_payloads(read.payload):
            image = image_from_payload(data)
            written.extend(
                _save_image_formats(image, save_path.parent / save_path.stem / name, texture_output_formats)
            )
        return written, None

    if payload_kind.startswith("image_array_bundle_"):
        return _write_payload_bundle(save_path, read.payload), None

    extension = suggested_extension or save_path.suffix or ".bin"
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    save_path = save_path.with_suffix(extension)
    save_path.write_bytes(read.payload)
    return [save_path], None


def _extract_bundle_files_sync(
    bundle_save_path: str,
    bundle: Dict[str, str],
    extracted_save_path: str,
    unity_version: str | None,
    texture_output_formats: tuple[str, ...],
    assetstudio_config: dict[str, Any],
) -> tuple[list[str], list[tuple[str, list[str]]], list[str]]:
    bundle_path = StdPath(bundle_save_path)
    output_root = StdPath(extracted_save_path)
    reads = export_assetstudio_objects(bundle_path, unity_version, assetstudio_config)

    logger.debug("Loaded bundle %s from %s", bundle.get("bundleName"), bundle_save_path)

    exported_files: list[StdPath] = []
    post_process_acb_files: list[tuple[StdPath, list[Dict]]] = []
    post_process_movie_bundles: list[tuple[StdPath, list[Dict]]] = []
    audio_jobs: list[tuple[str, list[str]]] = []
    video_jobs: list[str] = []
    all_objects: dict[int, dict[str, Any]] = {}
    for read in reads:
        if read.response.get("payload_kind") != "typetree_json" or not read.payload:
            continue
        try:
            all_objects[int(read.asset["path_id"])] = {
                "type": _asset_type(read),
                "data": json.loads(read.payload),
            }
        except (KeyError, TypeError, ValueError):
            logger.debug("Skipping invalid typetree object metadata: %s", read.asset)

    for read in reads:
        try:
            written_files, tree = _write_assetstudio_read(
                read,
                output_root,
                texture_output_formats,
                all_objects,
            )
            exported_files.extend(written_files)

            if tree is None:
                continue

            save_dir = _asset_save_path(read, output_root).parent
            unityfs_path = _asset_container_path(read)
            if "acbFiles" in tree:
                post_process_acb_files.append((save_dir, tree["acbFiles"]))
                logger.debug("Found acbFiles in %s: %s", unityfs_path, tree["acbFiles"])
            elif "movieBundleDatas" in tree:
                post_process_movie_bundles.append((save_dir, tree["movieBundleDatas"]))
                logger.debug(
                    "Found movieBundleDatas in %s: %s",
                    unityfs_path,
                    tree["movieBundleDatas"],
                )
        except (ValueError, TypeError, AttributeError, OSError) as e:
            logger.exception("Failed to extract %s: %s", _asset_container_path(read), e)
            raise e

    logger.debug(
        "Extracted %d files from %s, list: %s",
        len(exported_files),
        bundle_save_path,
        exported_files,
    )

    for save_dir, acb_files in post_process_acb_files:
        for acb_file in acb_files:
            acb_cue_sheet_name: str = acb_file["cueSheetName"]
            acb_output_path = (save_dir / acb_cue_sheet_name).with_suffix(".acb")

            if acb_file["formatType"] == 0 or acb_file["spilitFileNum"] == 0:
                acb_textasset_filename: str = acb_file["assetBundleFileName"]
                logger.debug("Try to find %s in %s", acb_textasset_filename, save_dir)
                expected_acb_textasset_path = (
                    save_dir / acb_textasset_filename.removesuffix(".bytes")
                ).with_suffix(".acb")
                assert (
                    expected_acb_textasset_path == acb_output_path
                ), f"Path mismatch: {expected_acb_textasset_path} != {acb_output_path}"
                try:
                    acb_textasset_path = _resolve_existing_path_sync(
                        expected_acb_textasset_path,
                        save_dir,
                        ".acb",
                    )
                except FileNotFoundError:
                    logger.error("%s not found in %s", acb_textasset_filename, save_dir)
                    continue

                if acb_textasset_path != acb_output_path:
                    acb_textasset_path.rename(acb_output_path)
                    _discard_exported_file_sync(exported_files, acb_textasset_path)
                    exported_files.append(acb_output_path)
                    logger.debug(
                        "Renamed %s to %s to match cue sheet name",
                        acb_textasset_path,
                        acb_output_path,
                    )
            else:
                pattern = re.compile(r"{(\d)\:D(\d)}")
                acb_textasset_filenames = [
                    pattern.sub(r"{\1:0\2d}", acb_file["assetBundleFileName"])
                    .format(i)
                    .lower()
                    for i in range(1, acb_file["spilitFileNum"] + 1)
                ]

                try:
                    acb_textasset_paths = [
                        _resolve_existing_path_sync(
                            save_dir / acb_textasset_filename.removesuffix(".bytes"),
                            save_dir,
                        )
                        for acb_textasset_filename in acb_textasset_filenames
                    ]
                except FileNotFoundError:
                    logger.error("%s not found in %s", acb_textasset_filenames, save_dir)
                    continue

                with acb_output_path.open("wb") as outfile:
                    for acb_textasset_path in acb_textasset_paths:
                        with acb_textasset_path.open("rb") as infile:
                            shutil.copyfileobj(infile, outfile)
                        _discard_exported_file_sync(exported_files, acb_textasset_path)
                        acb_textasset_path.unlink()

                logger.debug("Merged %s to %s.acb", acb_textasset_filenames, acb_cue_sheet_name)

            if acb_output_path.exists():
                with acb_output_path.open("rb") as f:
                    acb_data = f.read()
                    extracted_audio_files = extract_acb(
                        BytesIO(acb_data),
                        save_dir.as_posix(),
                        acb_output_path.as_posix(),
                    )

                acb_output_path.unlink()
                logger.debug("Removed %s", acb_output_path)
                _discard_exported_file_sync(exported_files, acb_output_path)
                audio_jobs.append((save_dir.as_posix(), extracted_audio_files))
            else:
                logger.warning("%s not found in %s", acb_output_path, save_dir)

    for save_dir, movie_bundles in post_process_movie_bundles:
        if len(movie_bundles) == 1:
            movie_bundle = movie_bundles[0]
            usm_output_name = movie_bundle["usmFileName"].removesuffix(".bytes")
            usm_output_path = (save_dir / usm_output_name).with_suffix(".usm")
            usm_output_path = _resolve_existing_usm_path_sync(usm_output_path, save_dir)
        elif len(movie_bundles) > 1:
            pattern = re.compile(r"-\d{3}.usm.bytes")
            usm_output_name = pattern.sub(".usm", movie_bundles[0]["usmFileName"])
            usm_output_path = save_dir / usm_output_name
            usm_split_filenames: list[str] = [x["usmFileName"] for x in movie_bundles]
            usm_split_paths = [
                save_dir / usm_split_filename.removesuffix(".bytes")
                for usm_split_filename in usm_split_filenames
            ]

            with usm_output_path.open("wb") as outfile:
                for usm_split_path in usm_split_paths:
                    if not usm_split_path.exists():
                        usm_split_path_lower = usm_split_path.with_name(
                            usm_split_path.name.lower()
                        )
                        if usm_split_path_lower.exists():
                            usm_split_path = usm_split_path_lower
                            logger.debug("Found %s instead of %s", usm_split_path, usm_split_paths)
                        else:
                            raise FileNotFoundError(
                                f"{usm_split_path} not found in {save_dir}"
                            )
                    with usm_split_path.open("rb") as infile:
                        shutil.copyfileobj(infile, outfile)
                    _discard_exported_file_sync(exported_files, usm_split_path)
                    usm_split_path.unlink()

            logger.debug("Merged %s to %s", usm_split_filenames, usm_output_name)
            exported_files.append(usm_output_path)
        else:
            logger.warning("Empty movieBundleDatas in %s", save_dir)
            continue

        if usm_output_path.exists():
            video_jobs.append(usm_output_path.as_posix())

    return (
        [path.as_posix() for path in exported_files],
        audio_jobs,
        video_jobs,
    )


async def download_deobfuscate_bundle(
    url: str,
    bundle_save_path: Path,
    headers: Dict[str, str],
    config=None,
    session: aiohttp.ClientSession | None = None,
) -> None:
    """Download and deobfuscate the bundle, retrying on transient network errors."""
    max_retries = get_download_max_retries(config)

    async def fetch_once(active_session: aiohttp.ClientSession) -> None:
        async with active_session.get(url, headers=headers) as response:
            if response.status == 200:
                async with await open_file(bundle_save_path, "wb") as f:
                    try:
                        header = await response.content.readexactly(4)
                    except asyncio.IncompleteReadError as exc:
                        header = exc.partial

                    if header == b"\x20\x00\x00\x00":
                        chunk = b""
                    elif header == b"\x10\x00\x00\x00":
                        try:
                            obfuscated_header = await response.content.readexactly(128)
                        except asyncio.IncompleteReadError as exc:
                            obfuscated_header = exc.partial
                        chunk = bytes(
                            a ^ b
                            for a, b in zip(
                                obfuscated_header,
                                (b"\xff" * 5 + b"\x00" * 3) * 16,
                            )
                        )
                    else:
                        chunk = header

                    if chunk:
                        await f.write(chunk)
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        if chunk:
                            await f.write(chunk)
                return

            logger.debug(
                "Failed to download %s: %s, response: %s",
                url,
                response.status,
                await response.text(),
            )
            raise aiohttp.ClientError(f"Failed to download {url}")

    for attempt in range(1, max_retries + 1):
        try:
            if session is not None:
                await fetch_once(session)
            else:
                async with aiohttp.ClientSession(
                    timeout=get_request_timeout(config)
                ) as retry_session:
                    await fetch_once(retry_session)
            return
        except (
            asyncio.TimeoutError,
            asyncio.CancelledError,
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientPayloadError,
        ) as exc:
            if attempt < max_retries:
                logger.warning(
                    "Download attempt %d/%d failed for %s (%s: %s), retrying...",
                    attempt,
                    max_retries,
                    url,
                    type(exc).__name__,
                    exc,
                )
            else:
                logger.error(
                    "Download failed after %d attempts for %s: %s",
                    max_retries,
                    url,
                    exc,
                )
                raise


async def extract_asset_bundle(
    bundle_save_path: Path,
    bundle: Dict[str, str],
    extracted_save_path: Path,
    unity_version: str = None,
    config=None,
) -> List[Path]:
    """Extract the asset bundle to the specified directory."""
    loop = asyncio.get_running_loop()
    exported_paths, audio_jobs, video_jobs = await loop.run_in_executor(
        _get_shared_extract_process_pool(config),
        _extract_bundle_files_sync,
        bundle_save_path.as_posix(),
        bundle,
        extracted_save_path.as_posix(),
        unity_version,
        _get_texture_output_formats(config),
        _get_assetstudio_config(config),
    )

    exported_files: List[Path] = [Path(path) for path in exported_paths]

    audio_file_semaphore = _get_shared_audio_file_semaphore(config)
    for save_dir_path, extracted_audio_files in audio_jobs:
        save_dir = Path(save_dir_path)
        audio_tasks = [
            asyncio.create_task(
                _process_extracted_audio_file(
                    extracted_audio_file,
                    save_dir,
                    config,
                    audio_file_semaphore,
                )
            )
            for extracted_audio_file in extracted_audio_files
        ]
        audio_results = await asyncio.gather(*audio_tasks)
        for audio_files in audio_results:
            exported_files.extend(audio_files)

    video_transcode_semaphore = _get_shared_video_transcode_semaphore(config)
    for usm_output_path_text in video_jobs:
        usm_output_path = Path(usm_output_path_text)
        if await usm_output_path.exists():
            async with video_transcode_semaphore:
                video_output_path = usm_output_path.with_suffix(".mp4")
                ffmpeg_process, encoder_name = await _run_ffmpeg_usm_to_mp4(
                    usm_output_path,
                    video_output_path,
                )
                await ffmpeg_process.wait()

                if ffmpeg_process.returncode != 0 and encoder_name:
                    logger.warning(
                        "Failed to convert %s to mp4 with %s, falling back to software encoding",
                        usm_output_path,
                        encoder_name,
                    )
                    _disable_ffmpeg_video_encoder()
                    ffmpeg_process, _ = await _run_ffmpeg_usm_to_mp4(
                        usm_output_path,
                        video_output_path,
                    )
                    await ffmpeg_process.wait()

                if ffmpeg_process.returncode != 0:
                    logger.warning("Failed to convert %s to mp4", usm_output_path)
                else:
                    logger.debug("Converted %s to mp4", usm_output_path)
                    exported_files.append(video_output_path)

            await usm_output_path.unlink()
            logger.debug("Removed %s", usm_output_path)
            _discard_exported_file(exported_files, usm_output_path)

    for file in exported_files[:]:
        if file.suffix in [".bytes", ".acb", ".usm"]:
            await file.unlink()
            logger.debug("Removed %s in cleanup stage", file)
            exported_files.remove(file)

    return exported_files
