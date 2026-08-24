"""This module contains functions to download, deobfuscate, and extract asset bundles."""

import asyncio
import logging
import os
import random
import re
import shutil
import tempfile
from functools import partial
from io import BytesIO
from pathlib import Path as StdPath
from typing import Dict, List, Literal, assert_never

import aiohttp
import cridecoder as cridecoder
import orjson as json
import UnityPy
import UnityPy.classes
import UnityPy.config
from anyio import Path, open_file

from bundle_acb_cache import (
    extract_acb_from_cached_bundles as _extract_acb_from_cached_bundles_sync,
)
from bundle_images import (
    render_image_asset as _render_image_asset,
)
from bundle_images import (
    save_image_formats as _save_image_formats,
)
from bundle_integrity import (
    DownloadIntegrityError,
    RetryableDownloadError,
)
from bundle_integrity import (
    validate_unityfs_bundle as _validate_unityfs_bundle,
)
from bundle_paths import (
    build_unityfs_save_path as _build_unityfs_save_path,
)
from bundle_paths import (
    canonical_root as _canonical_root,
)
from bundle_paths import (
    discard_exported_file as _discard_exported_file_sync,
)
from bundle_paths import (
    replace_suffix_secure as _replace_suffix_secure,
)
from bundle_paths import (
    resolve_existing_path as _resolve_existing_path_sync,
)
from bundle_paths import (
    resolve_existing_usm_path as _resolve_existing_usm_path_sync,
)
from bundle_paths import (
    resolve_generated_child_path as _resolve_generated_child_path,
)
from bundle_paths import (
    resolve_local_audio_outputs as _resolve_local_audio_outputs_sync,
)
from bundle_paths import (
    resolve_shared_audio_outputs as _resolve_shared_audio_outputs_sync,
)
from bundle_paths import (
    stream_files as _stream_files,
)
from bundle_runtime import (
    get_hca_decode_concurrency as _get_hca_decode_concurrency,
)
from bundle_runtime import (
    runtime as _bundle_runtime,
)
from bundle_video import (
    demux_usm_to_m2v,
    run_ffmpeg_video_to_mp4,
)
from bundle_video import (
    runtime as _video_runtime,
)
from external_process import (
    cleanup_process_output,
    terminate_process,
    wait_for_process,
)
from external_process import (
    set_process_output_paths as _set_process_output_paths,
)
from helpers import (
    get_download_http_session_options,
    get_download_max_retries,
    get_download_retry_base_delay,
    get_download_retry_max_delay,
    sanitize_http_log_value,
    sanitize_url,
)
from security import (
    SecurityError,
    atomic_write_bytes,
    atomic_write_stream,
    resolve_secure_path,
    secure_existing_output,
    validate_contained_file,
    validate_output_target,
)
from utils.acb import extract_acb
from utils.hca import decode_hca_file
from utils.live2d import (
    correct_param_ids,
    extract_params_ids_from_moc3,
    restore_unity_object_to_motion3,
)
from utils.playable import extract_playable

logger = logging.getLogger("live2d")

_vgmstream_cli_cache: str | None = None
_vgmstream_cli_checked = False
_hca_decoder_reported: str | None = None
HcaDecodeBackend = Literal["auto", "python", "vgmstream"]
_EXTERNAL_PROCESS_TERMINATE_GRACE = 2.0


def is_live2d_bundle(bundle: Dict[str, str]) -> bool:
    """Return whether this individual bundle belongs to the Live2D namespace."""
    return (bundle.get("bundleName") or "").startswith("live2d/")


def is_chart_score_bundle(bundle: Dict[str, str]) -> bool:
    """Return whether this individual bundle contains chart score assets."""
    return (bundle.get("bundleName") or "").startswith("music/music_score/")


def _get_external_process_timeout(config) -> float:
    value = getattr(config, "EXTERNAL_PROCESS_TIMEOUT", 300)
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"EXTERNAL_PROCESS_TIMEOUT must be positive, got {value!r}") from None
    if timeout <= 0:
        raise ValueError(f"EXTERNAL_PROCESS_TIMEOUT must be positive, got {value!r}")
    return timeout


async def _terminate_process(process) -> None:
    await terminate_process(process, _EXTERNAL_PROCESS_TERMINATE_GRACE)


async def _wait_for_process(process, timeout: float) -> int:
    return await wait_for_process(
        process,
        timeout,
        _terminate_process,
        task_attribute="_bundle_terminate_task",
        logger=logger,
    )


async def _communicate_with_process(process, timeout: float) -> tuple[bytes, bytes]:
    return await wait_for_process(
        process,
        timeout,
        _terminate_process,
        task_attribute="_bundle_terminate_task",
        logger=logger,
        communicate=True,
    )


def _cleanup_process_output(process, *, remove_direct_output: bool = False) -> None:
    cleanup_process_output(process, remove_direct_output=remove_direct_output, logger=logger)


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


_get_shared_audio_file_semaphore = _bundle_runtime.audio_file_semaphore
_get_shared_hca_decode_semaphore = _bundle_runtime.hca_decode_semaphore
_get_shared_audio_encoder_semaphore = _bundle_runtime.audio_encoder_semaphore
_get_shared_video_transcode_semaphore = _bundle_runtime.video_transcode_semaphore
_get_shared_extract_process_pool = _bundle_runtime.extract_process_pool
_get_shared_audio_process_pool = _bundle_runtime.audio_process_pool
_get_shared_usm_process_pool = _bundle_runtime.usm_process_pool


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


async def _get_ffmpeg_video_encoder(config) -> tuple[str | None, list[str]]:
    return await _video_runtime.get_encoder(
        config,
        communicate=_communicate_with_process,
        timeout=_get_external_process_timeout(config),
    )


def _disable_ffmpeg_video_encoder() -> None:
    _video_runtime.disable_encoder()


async def _demux_usm_to_m2v(usm_path: Path, config) -> Path | None:
    """Demux a USM into its raw .m2v video stream via cridecoder.

    Returns the path to the extracted video stream, or ``None`` if no video
    stream was produced. Audio streams are not exported here — the video
    pipeline only needs the elementary video for ffmpeg to transcode.
    """
    return await demux_usm_to_m2v(
        usm_path,
        process_pool=_get_shared_usm_process_pool(config),
        canonical_root=_canonical_root,
        resolve_generated_child_path=_resolve_generated_child_path,
    )


async def _run_ffmpeg_video_to_mp4(
    input_path: Path,
    output_path: Path,
    config,
) -> tuple[asyncio.subprocess.Process, str | None]:
    return await run_ffmpeg_video_to_mp4(
        input_path,
        output_path,
        config,
        get_encoder=_get_ffmpeg_video_encoder,
        set_output_paths=_set_process_output_paths,
    )


async def _run_hca_to_wav_with_cridecoder(
    input_path: Path,
    output_path: Path,
    config,
) -> bool:
    staging_dir: StdPath | None = None
    try:
        _report_hca_decoder(
            "Using cridecoder for HCA decoding via process pool "
            f"({_get_hca_decode_concurrency(config)} workers)"
        )
        output_root = StdPath(output_path.parent.as_posix())
        staging_dir = StdPath(tempfile.mkdtemp(prefix=".hca-", dir=output_root))
        staged_output = staging_dir / output_path.name
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _get_shared_audio_process_pool(config),
            decode_hca_file,
            input_path.as_posix(),
            staged_output.as_posix(),
        )
        produced = secure_existing_output(staging_dir, staged_output)
        validate_output_target(output_root, output_path)
        os.replace(produced, output_path)
        shutil.rmtree(staging_dir, ignore_errors=True)
    except Exception:
        logger.exception("Failed to decode %s with cridecoder", input_path)
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        return False
    return True


async def _run_hca_to_wav_with_vgmstream(
    input_path: Path,
    output_path: Path,
    config,
) -> bool:
    vgmstream_cli = _get_vgmstream_cli()
    if vgmstream_cli is None:
        return False

    _report_hca_decoder(f"Using vgmstream-cli for HCA decoding: {vgmstream_cli}")

    staging_dir: StdPath | None = None
    try:
        output_root = StdPath(output_path.parent.as_posix())
        staging_dir = StdPath(tempfile.mkdtemp(prefix=".hca-", dir=output_root))
        staged_output = staging_dir / output_path.name
        validate_output_target(output_root, output_path)

        try:
            process = await asyncio.create_subprocess_exec(
                vgmstream_cli,
                "-i",
                "-o",
                staged_output.as_posix(),
                input_path.as_posix(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        _set_process_output_paths(process, staged_output, staging_dir)
        stdout, stderr = await _communicate_with_process(
            process, _get_external_process_timeout(config)
        )
    except (asyncio.CancelledError, asyncio.TimeoutError):
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    except Exception:
        logger.exception("Failed to decode %s with vgmstream-cli", input_path)
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        return False

    try:
        if process.returncode != 0 or not staged_output.exists():
            error_output = (
                stderr.decode(errors="ignore").strip() or stdout.decode(errors="ignore").strip()
            )
            logger.warning(
                "vgmstream-cli failed to decode %s: %s",
                input_path,
                error_output or f"exit code {process.returncode}",
            )
            return False

        secure_existing_output(staging_dir, staged_output)
        os.replace(staged_output, output_path)
        return True
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


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
                return await _run_hca_to_wav_with_vgmstream(input_path, output_path, config)
            case "python":
                return await _run_hca_to_wav_with_cridecoder(
                    input_path,
                    output_path,
                    config,
                )
            case "vgmstream":
                if await _run_hca_to_wav_with_vgmstream(input_path, output_path, config):
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
        output_path = Path(resolve_secure_path(output_path.parent, output_path.name).as_posix())
        staging_dir = StdPath(tempfile.mkdtemp(prefix=".ffmpeg-", dir=output_path.parent))
        staged_output = staging_dir / output_path.name
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-loglevel",
                "panic",
                "-y",
                "-i",
                input_path.as_posix(),
                staged_output.as_posix(),
            )
        except BaseException:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        _set_process_output_paths(process, staged_output, staging_dir)
        try:
            returncode = await _wait_for_process(process, _get_external_process_timeout(config))
            if returncode != 0:
                return False
            secure_existing_output(staging_dir, staged_output)
            validate_output_target(output_path.parent, output_path)
            os.replace(staged_output, output_path)
        except (FileNotFoundError, ValueError, SecurityError):
            return False
        finally:
            _cleanup_process_output(process)
        return True


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
            save_root = _canonical_root(StdPath(save_dir.as_posix()))
            validate_contained_file(
                save_root,
                StdPath(extracted_audio_file).resolve().relative_to(save_root).as_posix(),
            )
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
                strict=True,
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
        except (asyncio.CancelledError, asyncio.TimeoutError):
            raise
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
    ffmpeg_process = None

    try:
        m2v_path = await _demux_usm_to_m2v(usm_output_path, config)
        if m2v_path is None:
            logger.warning("Failed to demux %s", usm_output_path)
        else:
            async with video_transcode_semaphore:
                ffmpeg_process, encoder_name = await _run_ffmpeg_video_to_mp4(
                    m2v_path,
                    video_output_path,
                    config,
                )
                await _wait_for_process(ffmpeg_process, _get_external_process_timeout(config))

                if ffmpeg_process.returncode != 0 and encoder_name:
                    _cleanup_process_output(ffmpeg_process)
                    logger.warning(
                        "Failed to convert %s to mp4 with %s, falling back to software encoding",
                        usm_output_path,
                        encoder_name,
                    )
                    _disable_ffmpeg_video_encoder()
                    ffmpeg_process, _ = await _run_ffmpeg_video_to_mp4(
                        m2v_path,
                        video_output_path,
                        config,
                    )
                    await _wait_for_process(ffmpeg_process, _get_external_process_timeout(config))

                if ffmpeg_process.returncode != 0:
                    _cleanup_process_output(ffmpeg_process)
                    logger.warning("Failed to convert %s to mp4", usm_output_path)
                else:
                    staged_output = getattr(ffmpeg_process, "_bundle_output_path", None)
                    if staged_output is not None:
                        secure_existing_output(staged_output.parent, staged_output)
                        validate_output_target(video_output_path.parent, video_output_path)
                        os.replace(staged_output, video_output_path)
                        staging_dir = getattr(ffmpeg_process, "_bundle_staging_dir", None)
                        if staging_dir is not None:
                            shutil.rmtree(staging_dir, ignore_errors=True)
                    logger.debug("Converted %s to mp4", usm_output_path)
                    exported_video_files.append(video_output_path)
    except (OSError, SecurityError, ValueError):
        if ffmpeg_process is not None:
            _cleanup_process_output(ffmpeg_process)
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


def _extract_bundle_files_sync(
    bundle_save_path: str,
    bundle: Dict[str, str],
    extracted_save_path: str,
    unity_version: str | None,
    texture_output_formats: tuple[str, ...],
    bundle_cache_root: str | None = None,
    *,
    live2d_bundle: bool = False,
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
    additional_motion_jobs = []
    param_id_map: dict[str, str] = {}

    for unityfs_path, unityfs_obj in unity_file.container.items():
        try:
            save_path = _build_unityfs_save_path(unityfs_path, output_root)
        except Exception as e:
            logger.exception("Failed to get relative path for %s", unityfs_path)
            raise e

        save_path = save_path.with_name(save_path.name.strip())
        if live2d_bundle and "motion" in save_path.parts:
            logger.debug("Skipping live2d motion asset %s for post-processing", unityfs_path)
            continue
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

                    atomic_write_bytes(save_path, json.dumps(tree, option=json.OPT_INDENT_2))
                    exported_files.append(save_path)

                    if (
                        live2d_bundle
                        and isinstance(tree, dict)
                        and tree.get("AdditionalMotionData")
                    ):
                        additional_motion_jobs.append((unityfs_obj.read(), save_dir))

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
                        data_bytes = data.m_Script.encode("utf-8", "surrogateescape")
                        atomic_write_bytes(save_path, data_bytes)
                        if live2d_bundle and save_path.suffix == ".moc3":
                            param_id_map.update(extract_params_ids_from_moc3(data_bytes))
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
                            sample_path = _resolve_generated_child_path(save_dir, filename)
                            logger.debug("Saving audio clip %s to %s", filename, sample_path)
                            atomic_write_bytes(sample_path, sample_data)
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
                    atomic_write_bytes(save_path, json.dumps(tree, option=json.OPT_INDENT_2))
                    exported_files.append(save_path)
        except (ValueError, TypeError, AttributeError, OSError) as e:
            logger.exception("Failed to extract %s: %s", unityfs_path, e)
            raise e

    if live2d_bundle:
        for mono_behaviour, save_dir in additional_motion_jobs:
            motions = [
                restore_unity_object_to_motion3(motion)
                for motion in mono_behaviour.AdditionalMotionData
            ]
            motions = [motion for motion in motions if motion is not None]
            correct_param_ids(motions, param_id_map)
            motion_dir = save_dir / "motions"
            motion_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(
                motion_dir / "BuildMotionData.json",
                json.dumps(
                    {"motions": [name for name, _ in motions]},
                    option=json.OPT_INDENT_2,
                ),
            )
            exported_files.append(motion_dir / "BuildMotionData.json")
            for name, motion in motions:
                motion_path = _resolve_generated_child_path(motion_dir, name, ".motion3.json")
                atomic_write_bytes(motion_path, json.dumps(motion, option=json.OPT_INDENT_2))
                exported_files.append(motion_path)

    logger.debug(
        "Extracted %d files from %s, list: %s",
        len(exported_files),
        bundle_save_path,
        exported_files,
    )

    for save_dir, acb_files in post_process_acb_files:
        for acb_file in acb_files:
            acb_cue_sheet_name: str = acb_file["cueSheetName"]
            acb_output_path = _replace_suffix_secure(save_dir, acb_cue_sheet_name, ".acb")

            if acb_file["formatType"] == 0 or acb_file["spilitFileNum"] == 0:
                acb_textasset_filename: str = acb_file["assetBundleFileName"]
                logger.debug("Try to find %s in %s", acb_textasset_filename, save_dir)
                expected_acb_textasset_path = _resolve_generated_child_path(
                    save_dir,
                    acb_textasset_filename.removesuffix(".bytes").removesuffix(".acb"),
                    ".acb",
                )
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
                        StdPath(bundle_cache_root) if bundle_cache_root else None,
                    ):
                        pass
                    else:
                        shared_audio_paths = _resolve_shared_audio_outputs_sync(
                            output_root,
                            save_dir,
                            acb_cue_sheet_name,
                        )
                        if not shared_audio_paths:
                            local_audio_paths = _resolve_local_audio_outputs_sync(
                                save_dir,
                                acb_cue_sheet_name,
                            )
                            if local_audio_paths:
                                logger.debug(
                                    "ACB textasset %s not found, but audio for %s already extracted locally, skipping ACB processing",
                                    acb_textasset_filename,
                                    acb_cue_sheet_name,
                                )
                                continue
                            logger.warning(
                                "ACB textasset %s not found in %s and no shared/local audio available for %s; "
                                "the ACB data likely resides in a separate bundle. "
                                "Skipping this acbFile entry — the audio will be extracted when that bundle is processed",
                                acb_textasset_filename,
                                save_dir,
                                acb_cue_sheet_name,
                            )
                            continue

                        for shared_audio_path in shared_audio_paths:
                            copied_audio_path = _resolve_generated_child_path(
                                save_dir, shared_audio_path.name
                            )
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
                        local_audio_paths = _resolve_local_audio_outputs_sync(
                            save_dir,
                            acb_cue_sheet_name,
                        )
                        if local_audio_paths:
                            logger.debug(
                                "ACB textasset %s not found, but audio for %s already extracted locally, skipping ACB processing",
                                acb_textasset_filename,
                                acb_cue_sheet_name,
                            )
                            continue
                        logger.warning(
                            "ACB textasset %s not found in %s and no shared/local audio available for %s; "
                            "the ACB data likely resides in a separate bundle. "
                            "Skipping this acbFile entry — the audio will be extracted when that bundle is processed",
                            acb_textasset_filename,
                            save_dir,
                            acb_cue_sheet_name,
                        )
                        continue

                    for shared_audio_path in shared_audio_paths:
                        copied_audio_path = _resolve_generated_child_path(
                            save_dir, shared_audio_path.name
                        )
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
                            _resolve_generated_child_path(
                                save_dir, acb_textasset_filename.removesuffix(".bytes")
                            ),
                            save_dir,
                        )
                        for acb_textasset_filename in acb_textasset_filenames
                    ]
                except FileNotFoundError:
                    logger.error("%s not found in %s", acb_textasset_filenames, save_dir)
                    continue

                atomic_write_stream(
                    acb_output_path,
                    _stream_files(acb_textasset_paths),
                )
                for acb_textasset_path in acb_textasset_paths:
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
                    acb_stage_dir = StdPath(tempfile.mkdtemp(prefix=".acb-", dir=save_dir))
                    promoted_audio = []
                    try:
                        staged_acb = acb_stage_dir / acb_output_path.name
                        staged_acb.write_bytes(acb_data)
                        extracted_audio_files = extract_acb(
                            BytesIO(acb_data),
                            acb_stage_dir.as_posix(),
                            staged_acb.as_posix(),
                            cue_name,
                        )

                        for extracted_audio_file in extracted_audio_files:
                            produced = secure_existing_output(acb_stage_dir, extracted_audio_file)
                            final_audio = _resolve_generated_child_path(save_dir, produced.name)
                            validate_output_target(save_dir, final_audio)
                            os.replace(produced, final_audio)
                            promoted_audio.append(final_audio.as_posix())
                    finally:
                        shutil.rmtree(acb_stage_dir, ignore_errors=True)

                acb_output_path.unlink()
                logger.debug("Removed %s", acb_output_path)
                _discard_exported_file_sync(exported_files, acb_output_path)
                audio_jobs.append((save_dir.as_posix(), promoted_audio))
            else:
                logger.warning("%s not found in %s", acb_output_path, save_dir)

    for save_dir, movie_bundles in post_process_movie_bundles:
        if len(movie_bundles) == 1:
            movie_bundle = movie_bundles[0]
            usm_output_name = movie_bundle["usmFileName"].removesuffix(".bytes")
            usm_output_path = _replace_suffix_secure(save_dir, usm_output_name, ".usm")
            usm_output_path = _resolve_existing_usm_path_sync(usm_output_path, save_dir)
        elif len(movie_bundles) > 1:
            pattern = re.compile(r"-\d{3}.usm.bytes")
            usm_output_name = pattern.sub(".usm", movie_bundles[0]["usmFileName"])
            usm_output_path = _replace_suffix_secure(save_dir, usm_output_name, ".usm")
            usm_split_filenames: list[str] = [x["usmFileName"] for x in movie_bundles]
            usm_split_paths = [
                _resolve_generated_child_path(save_dir, usm_split_filename.removesuffix(".bytes"))
                for usm_split_filename in usm_split_filenames
            ]

            resolved_usm_split_paths = []
            for usm_split_path in usm_split_paths:
                if not usm_split_path.exists():
                    usm_split_path_lower = usm_split_path.with_name(usm_split_path.name.lower())
                    if usm_split_path_lower.exists():
                        usm_split_path = validate_contained_file(
                            save_dir,
                            usm_split_path_lower.relative_to(save_dir).as_posix(),
                        )
                        logger.debug("Found %s instead of %s", usm_split_path, usm_split_paths)
                    else:
                        raise FileNotFoundError(f"{usm_split_path} not found in {save_dir}")
                else:
                    usm_split_path = validate_contained_file(
                        save_dir,
                        usm_split_path.relative_to(save_dir).as_posix(),
                    )
                resolved_usm_split_paths.append(usm_split_path)

            atomic_write_stream(
                usm_output_path,
                _stream_files(resolved_usm_split_paths),
            )
            for usm_split_path in resolved_usm_split_paths:
                _discard_exported_file_sync(exported_files, usm_split_path)
                usm_split_path.unlink()

            logger.debug("Merged %s to %s", usm_split_filenames, usm_output_name)
            exported_files.append(usm_output_path)
        else:
            logger.warning("Empty movieBundleDatas in %s", save_dir)
            continue

        if usm_output_path.exists():
            video_jobs.append(usm_output_path.as_posix())

    validated_exported_files = [
        validate_contained_file(
            output_root,
            path.relative_to(output_root).as_posix(),
        )
        for path in exported_files
    ]
    return (
        [path.as_posix() for path in validated_exported_files],
        audio_jobs,
        video_jobs,
    )


async def download_deobfuscate_bundle(
    url: str,
    trusted_root: Path,
    relative_destination: str,
    headers: Dict[str, str],
    config=None,
    session: aiohttp.ClientSession | None = None,
) -> None:
    """Download, validate, and atomically promote a bundle.

    ``hash`` is intentionally not used here: it selects changed bundles, but
    is not a byte-integrity format.  Likewise, ``crc`` is not asserted here:
    the Project Sekai CRC input convention is not verified.
    """
    bundle_save_path = Path(resolve_secure_path(trusted_root, relative_destination).as_posix())
    validate_output_target(trusted_root, bundle_save_path)
    max_retries = get_download_max_retries(config)
    retry_base_delay = get_download_retry_base_delay(config)
    retry_max_delay = get_download_retry_max_delay(config)

    async def fetch_once(active_session: aiohttp.ClientSession) -> None:
        async with active_session.get(url, headers=headers) as response:
            if response.status != 200:
                if response.status in (408, 429) or 500 <= response.status <= 599:
                    retry_after = None
                    if response.status in (429, 503):
                        value = response.headers.get("Retry-After")
                        try:
                            if value is not None:
                                retry_after = max(0.0, float(value))
                        except (TypeError, ValueError):
                            retry_after = None
                    raise RetryableDownloadError(
                        f"download returned retryable HTTP status {response.status}",
                        retry_after,
                    )
                raise DownloadIntegrityError(f"download returned HTTP status {response.status}")
            temporary_path: StdPath | None = None
            descriptor: int | None = None
            raw_bytes = 0
            stored_bytes = 0
            header = bytearray()
            header_mode: str | None = None

            def transform_chunk(raw_chunk: bytes) -> list[bytes]:
                """Strip and decode the transport header without buffering the body."""
                nonlocal header_mode
                if not raw_chunk:
                    return []

                output_chunks: list[bytes] = []
                pending = raw_chunk
                if header_mode is None:
                    needed = 4 - len(header)
                    header.extend(pending[:needed])
                    pending = pending[needed:]
                    if len(header) < 4:
                        return output_chunks
                    marker = bytes(header)
                    if marker == b"\x20\x00\x00\x00":
                        header_mode = "plain"
                    elif marker == b"\x10\x00\x00\x00":
                        header_mode = "obfuscated"
                    elif marker == b"Unit":
                        header_mode = "raw"
                        output_chunks.append(bytes(header))
                    else:
                        raise DownloadIntegrityError("unknown bundle transport header")

                if header_mode in ("plain", "raw"):
                    if pending:
                        output_chunks.append(pending)
                    return output_chunks

                if len(header) < 132:
                    header_bytes_needed = 132 - len(header)
                    header.extend(pending[:header_bytes_needed])
                    pending = pending[header_bytes_needed:]
                    if len(header) < 132:
                        return output_chunks
                    output_chunks.append(
                        bytes(
                            a ^ b
                            for a, b in zip(
                                header[4:132],
                                (b"\xff" * 5 + b"\x00" * 3) * 16,
                                strict=True,
                            )
                        )
                    )
                if pending:
                    output_chunks.append(pending)
                return output_chunks

            try:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{bundle_save_path.name}.", suffix=".tmp", dir=bundle_save_path.parent
                )
                temporary_path = StdPath(temporary_name)
                async with await open_file(descriptor, "wb") as output:
                    descriptor = None
                    async for raw_chunk in response.content.iter_chunked(1024 * 1024):
                        raw_bytes += len(raw_chunk)
                        for output_chunk in transform_chunk(raw_chunk):
                            await output.write(output_chunk)
                            stored_bytes += len(output_chunk)
                    await output.flush()
                    await asyncio.to_thread(os.fsync, output.wrapped.fileno())
                if header_mode == "obfuscated" and len(header) < 132:
                    raise DownloadIntegrityError("truncated obfuscated header")
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    if not isinstance(content_length, str) or not content_length.isdigit():
                        raise DownloadIntegrityError("Content-Length is not a valid integer")
                    if int(content_length) != raw_bytes:
                        raise DownloadIntegrityError("Content-Length does not match response bytes")
                if stored_bytes == 0:
                    raise DownloadIntegrityError("downloaded bundle is empty")
                _validate_unityfs_bundle(temporary_path, stored_bytes)
                validate_output_target(trusted_root, bundle_save_path)
                os.replace(temporary_path, StdPath(bundle_save_path.as_posix()))
                temporary_path = None
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass

    for attempt in range(1, max_retries + 1):
        try:
            if session is not None:
                await fetch_once(session)
            else:
                async with aiohttp.ClientSession(
                    **get_download_http_session_options(config)
                ) as retry_session:
                    await fetch_once(retry_session)
            return
        except asyncio.CancelledError:
            raise
        except (
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientPayloadError,
        ) as exc:
            retry_error = RetryableDownloadError(sanitize_http_log_value(str(exc)))
        except RetryableDownloadError as exc:
            retry_error = RetryableDownloadError(
                sanitize_http_log_value(str(exc)),
                exc.retry_after,
            )
        else:
            return

        if attempt >= max_retries:
            raise retry_error from None
        exponential_cap = min(
            retry_max_delay,
            retry_base_delay * (2 ** (attempt - 1)),
        )
        if retry_error.retry_after is not None:
            delay_cap = min(retry_max_delay, retry_error.retry_after)
        else:
            delay_cap = exponential_cap
        delay = random.uniform(0.0, delay_cap)
        logger.warning(
            "Download attempt %d/%d failed for %s (%s: %s), retrying in %.3fs...",
            attempt,
            max_retries,
            sanitize_url(url),
            type(retry_error).__name__,
            sanitize_http_log_value(str(retry_error)),
            delay,
        )
        await asyncio.sleep(delay)


async def _append_audio_outputs(
    exported_files: List[Path],
    audio_jobs: list[tuple[str, list[str]]],
    extracted_save_path: Path,
    config,
) -> None:
    audio_file_semaphore = _get_shared_audio_file_semaphore(config)
    extracted_root = StdPath(extracted_save_path.as_posix())
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
            for audio_file in audio_files:
                exported_files.append(
                    Path(
                        validate_contained_file(
                            extracted_root,
                            StdPath(audio_file.as_posix()).relative_to(extracted_root).as_posix(),
                        ).as_posix()
                    )
                )


async def _append_video_outputs(
    exported_files: List[Path],
    video_jobs: list[str],
    extracted_save_path: Path,
    config,
) -> None:
    extracted_root = StdPath(extracted_save_path.as_posix())
    for video_files, discarded_files in await _process_video_jobs(video_jobs, config):
        for video_file in video_files:
            exported_files.append(
                Path(
                    validate_contained_file(
                        extracted_root,
                        StdPath(video_file.as_posix()).relative_to(extracted_root).as_posix(),
                    ).as_posix()
                )
            )
        for discarded_file in discarded_files:
            _discard_exported_file(exported_files, discarded_file)


async def _cleanup_extracted_files(
    exported_files: List[Path],
    extracted_save_path: Path,
) -> None:
    extracted_root = StdPath(extracted_save_path.as_posix())
    for file in exported_files[:]:
        validate_contained_file(
            extracted_root,
            StdPath(file.as_posix()).relative_to(extracted_root).as_posix(),
        )
        if file.suffix in [".bytes", ".acb", ".usm"]:
            await file.unlink()
            logger.debug("Removed %s in cleanup stage", file)
            exported_files.remove(file)


async def extract_asset_bundle(
    bundle_save_path: Path,
    bundle: Dict[str, str],
    extracted_save_path: Path,
    unity_version: str = None,
    config=None,
    bundle_cache_root: Path | None = None,
) -> List[Path]:
    """Extract the asset bundle to the specified directory."""
    live2d_bundle = is_live2d_bundle(bundle)
    if getattr(config, "UPDATER_MODE", "assets") == "live2d" and bundle.get(
        "bundleName", ""
    ).startswith("live2d/motion/"):
        return []
    loop = asyncio.get_running_loop()
    exported_paths, audio_jobs, video_jobs = await loop.run_in_executor(
        _get_shared_extract_process_pool(config),
        partial(
            _extract_bundle_files_sync,
            bundle_save_path.as_posix(),
            bundle,
            extracted_save_path.as_posix(),
            unity_version,
            _get_texture_output_formats(config),
            bundle_cache_root.as_posix() if bundle_cache_root is not None else None,
            live2d_bundle=live2d_bundle,
        ),
    )

    exported_files: List[Path] = [Path(path) for path in exported_paths]

    await _append_audio_outputs(exported_files, audio_jobs, extracted_save_path, config)
    await _append_video_outputs(exported_files, video_jobs, extracted_save_path, config)
    await _cleanup_extracted_files(exported_files, extracted_save_path)

    return exported_files
