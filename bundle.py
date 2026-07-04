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
from typing import Dict, List, Literal, assert_never

import orjson as json

import aiohttp
import cridecoder
import UnityPy
import UnityPy.classes
import UnityPy.config
from PIL import Image
from UnityPy.enums.ClassIDType import ClassIDType
from UnityPy.enums.SpritePackingRotation import SpritePackingRotation
from UnityPy.export.SpriteHelper import SpriteSettings, get_image
from anyio import Path, open_file

from constants import (
    UNITY_FS_BUILT_IN_ALT_CONTAINER_BASE,
    UNITY_FS_BUILT_IN_CONTAINER_BASE,
    UNITY_FS_CONTAINER_BASE,
)
from helpers import get_download_max_retries, get_request_timeout
from utils.acb import extract_acb
from utils.hca import decode_hca_file
from utils.playable import extract_playable

logger = logging.getLogger("live2d")

_ffmpeg_video_encoder_cache: tuple[str | None, list[str]] | None = None
_audio_file_semaphore_cache: tuple[int, asyncio.Semaphore] | None = None
_hca_decode_semaphore_cache: tuple[int, asyncio.Semaphore] | None = None
_audio_encoder_semaphore_cache: tuple[int, asyncio.Semaphore] | None = None
_video_transcode_semaphore_cache: tuple[int, asyncio.Semaphore] | None = None
_extract_process_pool_cache: tuple[int, ProcessPoolExecutor] | None = None
_audio_process_pool_cache: tuple[int, ProcessPoolExecutor] | None = None
_usm_process_pool_cache: tuple[int, ProcessPoolExecutor] | None = None
_vgmstream_cli_cache: str | None = None
_vgmstream_cli_checked = False
_hca_decoder_reported: str | None = None
HcaDecodeBackend = Literal["auto", "python", "vgmstream"]


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


def _get_usm_demux_concurrency(config) -> int:
    return _sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_USM_DEMUXES",
            _get_video_transcode_concurrency(config),
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
        formats = [str(part).strip().lower().removeprefix(".") for part in value]

    valid_formats = []
    for image_format in formats:
        if image_format in {"png", "webp"} and image_format not in valid_formats:
            valid_formats.append(image_format)

    if not valid_formats and formats:
        logger.warning("No valid TEXTURE_OUTPUT_FORMATS found, skipping texture export")

    return tuple(valid_formats)


def _get_shared_audio_file_semaphore(config) -> asyncio.Semaphore:
    global _audio_file_semaphore_cache

    concurrency = _get_max_concurrent_audio_files(config)
    if _audio_file_semaphore_cache is None or _audio_file_semaphore_cache[0] != concurrency:
        _audio_file_semaphore_cache = (
            concurrency,
            asyncio.Semaphore(concurrency),
        )
    return _audio_file_semaphore_cache[1]


def _get_shared_hca_decode_semaphore(config) -> asyncio.Semaphore:
    global _hca_decode_semaphore_cache

    concurrency = _get_hca_decode_concurrency(config)
    if _hca_decode_semaphore_cache is None or _hca_decode_semaphore_cache[0] != concurrency:
        _hca_decode_semaphore_cache = (
            concurrency,
            asyncio.Semaphore(concurrency),
        )
    return _hca_decode_semaphore_cache[1]


def _get_shared_audio_encoder_semaphore(config) -> asyncio.Semaphore:
    global _audio_encoder_semaphore_cache

    concurrency = _get_audio_encoder_concurrency(config)
    if _audio_encoder_semaphore_cache is None or _audio_encoder_semaphore_cache[0] != concurrency:
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


def _shutdown_usm_process_pool() -> None:
    global _usm_process_pool_cache

    if _usm_process_pool_cache is None:
        return

    _, executor = _usm_process_pool_cache
    _usm_process_pool_cache = None
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
atexit.register(_shutdown_usm_process_pool)


def _get_shared_extract_process_pool(config) -> ProcessPoolExecutor:
    global _extract_process_pool_cache

    concurrency = _get_extract_process_concurrency(config)
    if _extract_process_pool_cache is None or _extract_process_pool_cache[0] != concurrency:
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
    if _audio_process_pool_cache is None or _audio_process_pool_cache[0] != concurrency:
        if _audio_process_pool_cache is not None:
            _audio_process_pool_cache[1].shutdown(wait=False, cancel_futures=False)
        _audio_process_pool_cache = (
            concurrency,
            ProcessPoolExecutor(max_workers=concurrency),
        )
    return _audio_process_pool_cache[1]


def _get_shared_usm_process_pool(config) -> ProcessPoolExecutor:
    global _usm_process_pool_cache

    concurrency = _get_usm_demux_concurrency(config)
    if _usm_process_pool_cache is None or _usm_process_pool_cache[0] != concurrency:
        if _usm_process_pool_cache is not None:
            _usm_process_pool_cache[1].shutdown(wait=False, cancel_futures=False)
        _usm_process_pool_cache = (
            concurrency,
            ProcessPoolExecutor(max_workers=concurrency),
        )
    return _usm_process_pool_cache[1]


def _get_hca_decode_backend(config) -> HcaDecodeBackend:
    backend = str(getattr(config, "HCA_DECODE_BACKEND", "auto")).strip().lower()
    match backend:
        case "auto":
            return "auto"
        case "python":
            return "python"
        case "vgmstream":
            return "vgmstream"

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

    candidate_paths = [path async for path in save_dir.iterdir() if path.suffix.lower() == ".usm"]
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


async def _demux_usm_to_m2v(usm_path: Path, config) -> Path | None:
    """Demux a USM into its raw .m2v video stream via cridecoder.

    Returns the path to the extracted video stream, or ``None`` if no video
    stream was produced. Audio streams are not exported here — the video
    pipeline only needs the elementary video for ffmpeg to transcode.
    """
    output_dir = usm_path.parent
    loop = asyncio.get_running_loop()
    try:
        outputs = await loop.run_in_executor(
            _get_shared_usm_process_pool(config),
            cridecoder.extract_usm,
            usm_path.as_posix(),
            output_dir.as_posix(),
            None,
            False,
        )
    except Exception:
        logger.exception("Failed to demux %s with cridecoder", usm_path)
        return None

    for output in outputs:
        if output.lower().endswith(".m2v"):
            return Path(output)

    # Fall back to the first produced stream if the extension differs.
    if outputs:
        return Path(outputs[0])

    logger.warning("cridecoder produced no video stream for %s", usm_path)
    return None


async def _run_ffmpeg_video_to_mp4(
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


async def _run_hca_to_wav_with_cridecoder(
    input_path: Path,
    output_path: Path,
    config,
) -> bool:
    try:
        _report_hca_decoder(
            "Using cridecoder for HCA decoding via process pool "
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
        logger.exception("Failed to decode %s with cridecoder", input_path)
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
        error_output = (
            stderr.decode(errors="ignore").strip() or stdout.decode(errors="ignore").strip()
        )
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

        match backend:
            case "auto":
                if await _run_hca_to_wav_with_cridecoder(input_path, output_path, config):
                    return True
                logger.warning(
                    "Falling back to vgmstream-cli for %s after cridecoder failure",
                    input_path,
                )
                return await _run_hca_to_wav_with_vgmstream(input_path, output_path)
            case "python":
                return await _run_hca_to_wav_with_cridecoder(
                    input_path,
                    output_path,
                    config,
                )
            case "vgmstream":
                if await _run_hca_to_wav_with_vgmstream(input_path, output_path):
                    return True
                logger.warning(
                    "Falling back to cridecoder for %s after vgmstream-cli failure",
                    input_path,
                )
                return await _run_hca_to_wav_with_cridecoder(
                    input_path,
                    output_path,
                    config,
                )
            case unreachable:
                assert_never(unreachable)


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
                logger.warning("%s not found in %s", extracted_audio_file_path, save_dir)
                return exported_audio_files

            if (await extracted_audio_file_path.stat()).st_size == 0:
                logger.warning("%s is empty, skipping", extracted_audio_file_path)
                return exported_audio_files

            if extracted_audio_file_path.suffix.lower() == ".wav":
                wav_path = extracted_audio_file_path
            else:
                wav_path = extracted_audio_file_path.with_suffix(".wav")
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

            mp3_path = wav_path.with_suffix(".mp3")
            encode_tasks.append(
                (
                    "mp3",
                    mp3_path,
                    _run_ffmpeg_audio_encode(wav_path, mp3_path, config),
                )
            )

            if "music" in save_dir.parts:
                flac_path = wav_path.with_suffix(".flac")
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


async def _process_video_job(
    usm_output_path_text: str,
    config,
    video_transcode_semaphore: asyncio.Semaphore,
) -> tuple[list[Path], list[Path]]:
    usm_output_path = Path(usm_output_path_text)
    if not await usm_output_path.exists():
        return [], []

    exported_video_files: list[Path] = []
    discarded_video_files: list[Path] = []
    video_output_path = usm_output_path.with_suffix(".mp4")
    m2v_path: Path | None = None

    try:
        m2v_path = await _demux_usm_to_m2v(usm_output_path, config)
        if m2v_path is None:
            logger.warning("Failed to demux %s", usm_output_path)
        else:
            async with video_transcode_semaphore:
                ffmpeg_process, encoder_name = await _run_ffmpeg_video_to_mp4(
                    m2v_path,
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
                    ffmpeg_process, _ = await _run_ffmpeg_video_to_mp4(
                        m2v_path,
                        video_output_path,
                    )
                    await ffmpeg_process.wait()

                if ffmpeg_process.returncode != 0:
                    logger.warning("Failed to convert %s to mp4", usm_output_path)
                else:
                    logger.debug("Converted %s to mp4", usm_output_path)
                    exported_video_files.append(video_output_path)
    except OSError:
        logger.exception("Failed to process video %s", usm_output_path)
    finally:
        for discarded_file in (m2v_path, usm_output_path):
            if discarded_file is None:
                continue
            try:
                if await discarded_file.exists():
                    await discarded_file.unlink()
                    logger.debug("Removed %s", discarded_file)
                    discarded_video_files.append(discarded_file)
            except OSError:
                logger.exception(
                    "Failed to remove video intermediate %s",
                    discarded_file,
                )

    return exported_video_files, discarded_video_files


async def _process_video_jobs(
    video_jobs: list[str],
    config,
) -> list[tuple[list[Path], list[Path]]]:
    video_transcode_semaphore = _get_shared_video_transcode_semaphore(config)
    video_tasks = [
        asyncio.create_task(
            _process_video_job(
                usm_output_path_text,
                config,
                video_transcode_semaphore,
            )
        )
        for usm_output_path_text in video_jobs
    ]
    return await asyncio.gather(*video_tasks)


def _should_fallback_sprite_render(exc: Exception) -> bool:
    if isinstance(exc, ValueError):
        return "Coordinate 'lower' is less than 'upper'" in str(exc)
    if isinstance(exc, StopIteration):
        return True
    return isinstance(exc, RuntimeError) and isinstance(exc.__cause__, StopIteration)


def _get_sprite_atlas_data(data: UnityPy.classes.Sprite):
    atlas = None
    if data.m_SpriteAtlas:
        atlas = data.m_SpriteAtlas.read()
    elif data.m_AtlasTags:
        for obj in data.assets_file.objects.values():
            if obj.type != ClassIDType.SpriteAtlas:
                continue
            atlas = obj.read()
            if atlas.m_Name == data.m_AtlasTags[0]:
                break
            atlas = None

    if not atlas:
        return data.m_RD

    sprite_atlas_data = next(
        (value for key, value in atlas.m_RenderDataMap if key == data.m_RenderDataKey),
        None,
    )
    if sprite_atlas_data is None:
        logger.warning(
            "Sprite atlas render data missing for %s, falling back to embedded render data",
            data.m_Name or data.path_id,
        )
        return data.m_RD
    return sprite_atlas_data


def _render_sprite_with_fallback(data: UnityPy.classes.Sprite) -> Image.Image:
    """Render a sprite, falling back to its texture rect when tight mesh export fails."""
    try:
        return data.image
    except (ValueError, RuntimeError, StopIteration) as exc:
        if not _should_fallback_sprite_render(exc):
            raise

    sprite_atlas_data = _get_sprite_atlas_data(data)

    texture_rect = sprite_atlas_data.textureRect
    if texture_rect.width <= 0 or texture_rect.height <= 0:
        raise ValueError(
            f"Invalid sprite texture rect {texture_rect} for {data.m_Name or data.path_id}"
        )

    image = get_image(
        data,
        sprite_atlas_data.texture,
        sprite_atlas_data.alphaTexture,
    ).crop(
        (
            texture_rect.x,
            texture_rect.y,
            texture_rect.x + texture_rect.width,
            texture_rect.y + texture_rect.height,
        )
    )

    settings = SpriteSettings(sprite_atlas_data.settingsRaw)
    if settings.packed == 1:
        rotation = settings.packingRotation
        if rotation == SpritePackingRotation.kSPRFlipHorizontal:
            image = image.transpose(Image.FLIP_LEFT_RIGHT)
        elif rotation == SpritePackingRotation.kSPRFlipVertical:
            image = image.transpose(Image.FLIP_TOP_BOTTOM)
        elif rotation == SpritePackingRotation.kSPRRotate180:
            image = image.transpose(Image.ROTATE_180)
        elif rotation == SpritePackingRotation.kSPRRotate90:
            image = image.transpose(Image.ROTATE_270)

    logger.warning(
        "Falling back to texture rect export for sprite %s",
        data.m_Name or data.path_id,
    )
    return image.transpose(Image.FLIP_TOP_BOTTOM)


def _render_image_asset(
    data: UnityPy.classes.Texture2D | UnityPy.classes.Sprite,
) -> Image.Image:
    if isinstance(data, UnityPy.classes.Sprite):
        return _render_sprite_with_fallback(data)
    return data.image


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


def _resolve_shared_audio_outputs_sync(
    output_root: StdPath,
    save_dir: StdPath,
    cue_sheet_name: str,
) -> list[StdPath]:
    expected_names = {
        f"{cue_sheet_name}{suffix}".lower() for suffix in (".wav", ".mp3", ".flac", ".hca")
    }
    return [
        path
        for path in output_root.rglob("*")
        if path.is_file() and path.parent != save_dir and path.name.lower() in expected_names
    ]


def _extract_acb_from_cached_bundles_sync(
    bundle_save_path: StdPath,
    acb_textasset_filename: str,
    acb_output_path: StdPath,
    unity_version: str | None,
) -> bool:
    bundle_cache_root = bundle_save_path
    for _ in bundle_save_path.parts:
        if bundle_cache_root.name == "bundle":
            break
        if bundle_cache_root.parent == bundle_cache_root:
            return False
        bundle_cache_root = bundle_cache_root.parent

    expected_textasset_name = acb_textasset_filename.lower()
    for cached_bundle_path in bundle_cache_root.rglob("*"):
        if not cached_bundle_path.is_file() or cached_bundle_path == bundle_save_path:
            continue

        try:
            UnityPy.config.FALLBACK_UNITY_VERSION = unity_version
            cached_unity_file = UnityPy.load(cached_bundle_path.as_posix())
            if not cached_unity_file:
                continue
        except Exception:
            continue

        for unityfs_path, unityfs_obj in cached_unity_file.container.items():
            if unityfs_obj.type.name != "TextAsset":
                continue
            if PurePosixPath(unityfs_path).name.lower() != expected_textasset_name:
                continue

            data = unityfs_obj.read()
            if not isinstance(data, UnityPy.classes.TextAsset):
                continue

            acb_output_path.write_bytes(data.m_Script.encode("utf-8", "surrogateescape"))
            logger.debug(
                "Extracted %s from cached bundle %s to %s",
                acb_textasset_filename,
                cached_bundle_path.relative_to(bundle_cache_root),
                acb_output_path,
            )
            return True

    return False


def _resolve_existing_usm_path_sync(expected_path: StdPath, save_dir: StdPath) -> StdPath:
    try:
        return _resolve_existing_path_sync(expected_path, save_dir, ".usm")
    except FileNotFoundError:
        pass

    candidate_paths = [path for path in save_dir.iterdir() if path.suffix.lower() == ".usm"]
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


def _extract_bundle_files_sync(
    bundle_save_path: str,
    bundle: Dict[str, str],
    extracted_save_path: str,
    unity_version: str | None,
    texture_output_formats: tuple[str, ...],
) -> tuple[list[str], list[tuple[str, list[str]]], list[str]]:
    UnityPy.config.FALLBACK_UNITY_VERSION = unity_version

    bundle_path = StdPath(bundle_save_path)
    output_root = StdPath(extracted_save_path)
    unity_file = UnityPy.load(bundle_path.as_posix())
    if not unity_file:
        raise ValueError(f"Failed to load {bundle_save_path}")

    logger.debug("Loaded bundle %s from %s", bundle.get("bundleName"), bundle_save_path)

    exported_files: list[StdPath] = []
    post_process_acb_files: list[tuple[StdPath, list[Dict]]] = []
    post_process_movie_bundles: list[tuple[StdPath, list[Dict]]] = []
    audio_jobs: list[tuple[str, list[str]]] = []
    video_jobs: list[str] = []

    for unityfs_path, unityfs_obj in unity_file.container.items():
        try:
            save_path = _build_unityfs_save_path(unityfs_path, output_root)
        except Exception as e:
            logger.exception("Failed to get relative path for %s", unityfs_path)
            raise e

        save_path = save_path.with_name(save_path.name.strip())
        save_dir = save_path.parent
        save_dir.mkdir(parents=True, exist_ok=True)

        try:
            match unityfs_obj.type.name:
                case "MonoBehaviour":
                    tree = None
                    try:
                        if unityfs_obj.serialized_type.node:
                            tree = unityfs_obj.read_typetree()
                    except AttributeError:
                        tree = unityfs_obj.read_typetree()
                    logger.debug("Saving MonoBehaviour %s to %s", unityfs_path, save_path)

                    if unityfs_path.endswith(".playable"):
                        tree = extract_playable(unity_file, unityfs_path)

                    save_path.write_bytes(json.dumps(tree, option=json.OPT_INDENT_2))
                    exported_files.append(save_path)

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
                case "TextAsset":
                    data = unityfs_obj.read()
                    if isinstance(data, UnityPy.classes.TextAsset):
                        if save_path.suffix == ".bytes":
                            save_path = save_path.with_suffix("")
                        save_path.write_bytes(data.m_Script.encode("utf-8", "surrogateescape"))
                        exported_files.append(save_path)
                    else:
                        raise TypeError(f"Expected TextAsset, got {type(data)} for {unityfs_path}")
                case "Texture2D" | "Sprite":
                    data = unityfs_obj.read()
                    if isinstance(data, UnityPy.classes.Texture2D) or isinstance(
                        data, UnityPy.classes.Sprite
                    ):
                        image = _render_image_asset(data)
                        exported_files.extend(
                            _save_image_formats(image, save_path, texture_output_formats)
                        )
                    else:
                        raise TypeError(
                            f"Expected Texture2D or Sprite, got {type(data)} for {unityfs_path}"
                        )
                case "Texture2DArray":
                    data = unityfs_obj.read()
                    if isinstance(data, UnityPy.classes.Texture2DArray):
                        for i, image in enumerate(data.images):
                            texture_path = save_path.with_name(f"{save_path.stem}_{i}")
                            exported_files.extend(
                                _save_image_formats(
                                    image,
                                    texture_path,
                                    texture_output_formats,
                                )
                            )
                    else:
                        raise TypeError(
                            f"Expected Texture2DArray, got {type(data)} for {unityfs_path}"
                        )
                case "AudioClip":
                    data = unityfs_obj.read()
                    if isinstance(data, UnityPy.classes.AudioClip):
                        for filename, sample_data in data.samples.items():
                            sample_path = save_path.with_name(filename)
                            logger.debug("Saving audio clip %s to %s", filename, sample_path)
                            sample_path.write_bytes(sample_data)
                            exported_files.append(sample_path)
                    else:
                        raise TypeError(f"Expected AudioClip, got {type(data)} for {unityfs_path}")
                case "Mesh":
                    logger.warning("Mesh data is not supported yet, skipping %s", unityfs_path)
                    continue
                case "Cubemap":
                    logger.warning("Cubemap data is not supported yet, skipping %s", unityfs_path)
                    continue
                case _:
                    logger.warning(
                        "Unknowen type %s of %s, extracting typetree",
                        unityfs_obj.type.name,
                        unityfs_path,
                    )
                    tree = unityfs_obj.read_typetree()
                    try:
                        json.dumps(tree)
                    except (ValueError, TypeError):
                        logger.warning("Failed to serialize %s, skipping", tree)
                    save_path.write_bytes(json.dumps(tree, option=json.OPT_INDENT_2))
                    exported_files.append(save_path)
        except (ValueError, TypeError, AttributeError, OSError) as e:
            logger.exception("Failed to extract %s: %s", unityfs_path, e)
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
                try:
                    acb_textasset_path = _resolve_existing_path_sync(
                        expected_acb_textasset_path,
                        save_dir,
                        ".acb",
                    )
                except FileNotFoundError:
                    if _extract_acb_from_cached_bundles_sync(
                        bundle_path,
                        acb_textasset_filename,
                        acb_output_path,
                        unity_version,
                    ):
                        pass
                    else:
                        shared_audio_paths = _resolve_shared_audio_outputs_sync(
                            output_root,
                            save_dir,
                            acb_cue_sheet_name,
                        )
                        if not shared_audio_paths:
                            raise FileNotFoundError(
                                f"{acb_textasset_filename} not found in {save_dir}"
                            )

                        for shared_audio_path in shared_audio_paths:
                            copied_audio_path = save_dir / shared_audio_path.name
                            shutil.copy2(shared_audio_path, copied_audio_path)
                            exported_files.append(copied_audio_path)
                        logger.debug(
                            "Copied shared audio outputs for %s from %s to %s",
                            acb_cue_sheet_name,
                            shared_audio_paths[0].parent,
                            save_dir,
                        )
                        continue
                else:
                    if acb_textasset_path != acb_output_path:
                        acb_textasset_path.rename(acb_output_path)
                        _discard_exported_file_sync(exported_files, acb_textasset_path)
                        exported_files.append(acb_output_path)
                        logger.debug(
                            "Renamed %s to %s to match cue sheet name",
                            acb_textasset_path,
                            acb_output_path,
                        )

                if not acb_output_path.exists():
                    shared_audio_paths = _resolve_shared_audio_outputs_sync(
                        output_root,
                        save_dir,
                        acb_cue_sheet_name,
                    )
                    if not shared_audio_paths:
                        raise FileNotFoundError(f"{acb_textasset_filename} not found in {save_dir}")

                    for shared_audio_path in shared_audio_paths:
                        copied_audio_path = save_dir / shared_audio_path.name
                        shutil.copy2(shared_audio_path, copied_audio_path)
                        exported_files.append(copied_audio_path)
                    logger.debug(
                        "Copied shared audio outputs for %s from %s to %s",
                        acb_cue_sheet_name,
                        shared_audio_paths[0].parent,
                        save_dir,
                    )
                    continue
            else:
                pattern = re.compile(r"{(\d)\:D(\d)}")
                acb_textasset_filenames = [
                    pattern.sub(r"{\1:0\2d}", acb_file["assetBundleFileName"]).format(i).lower()
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
                    acb_asset_name = (
                        acb_file["assetBundleFileName"].removesuffix(".bytes").removesuffix(".acb")
                    )
                    cue_name = acb_cue_sheet_name if acb_cue_sheet_name != acb_asset_name else None
                    extracted_audio_files = extract_acb(
                        BytesIO(acb_data),
                        save_dir.as_posix(),
                        acb_output_path.as_posix(),
                        cue_name,
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
                        usm_split_path_lower = usm_split_path.with_name(usm_split_path.name.lower())
                        if usm_split_path_lower.exists():
                            usm_split_path = usm_split_path_lower
                            logger.debug("Found %s instead of %s", usm_split_path, usm_split_paths)
                        else:
                            raise FileNotFoundError(f"{usm_split_path} not found in {save_dir}")
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

    for video_files, discarded_files in await _process_video_jobs(video_jobs, config):
        exported_files.extend(video_files)
        for discarded_file in discarded_files:
            _discard_exported_file(exported_files, discarded_file)

    for file in exported_files[:]:
        if file.suffix in [".bytes", ".acb", ".usm"]:
            await file.unlink()
            logger.debug("Removed %s in cleanup stage", file)
            exported_files.remove(file)

    return exported_files
