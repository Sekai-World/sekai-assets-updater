"""Low-level USM demuxing and FFmpeg video process creation."""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor
from pathlib import Path as StdPath

import cridecoder
from anyio import Path

from updater.external_process import (
    get_external_process_timeout as _get_external_process_timeout,
)
from updater.extract.paths import canonical_root as _canonical_root
from updater.extract.paths import (
    resolve_generated_child_path as _resolve_generated_child_path,
)
from updater.media.process import (
    _cleanup_process_output,
    _communicate_with_process,
    _set_process_output_paths,
    _wait_for_process,
)
from updater.runtime import runtime as _shared_runtime
from updater.security import (
    SecurityError,
    atomic_write_bytes,
    resolve_secure_path,
    secure_existing_output,
    validate_contained_file,
    validate_output_target,
)

logger = logging.getLogger("live2d")

Communicate = Callable[[object, float], Awaitable[tuple[bytes, bytes]]]
GetEncoder = Callable[[object], Awaitable[tuple[str | None, list[str]]]]
SetOutputPaths = Callable[[object, StdPath, StdPath], None]


class VideoRuntime:
    def __init__(self) -> None:
        self._encoder: tuple[str | None, list[str]] | None = None

    async def get_encoder(
        self,
        config,
        *,
        communicate: Communicate,
        timeout: float,
    ) -> tuple[str | None, list[str]]:
        """Detect a usable hardware H.264 encoder for the current platform."""
        if self._encoder is not None:
            return self._encoder

        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-encoders",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self._encoder = (None, [])
            return self._encoder

        stdout, stderr = await communicate(process, timeout)
        if process.returncode != 0:
            logger.warning(
                "Failed to inspect ffmpeg encoders, falling back to software encoding: %s",
                stderr.decode(errors="ignore").strip(),
            )
            self._encoder = (None, [])
            return self._encoder

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
                self._encoder = (encoder_name, encoder_args)
                return self._encoder

        logger.info("No usable ffmpeg hardware video encoder detected, using software encoding")
        self._encoder = (None, [])
        return self._encoder

    def disable_encoder(self) -> None:
        self._encoder = (None, [])


async def demux_usm_to_m2v(
    usm_path: Path,
    *,
    process_pool: Executor,
    canonical_root: Callable[[StdPath], StdPath],
    resolve_generated_child_path: Callable[[StdPath, str], StdPath],
) -> Path | None:
    output_root = canonical_root(StdPath(usm_path.parent.as_posix()))
    staging_dir = StdPath(tempfile.mkdtemp(prefix=".usm-", dir=output_root))
    loop = asyncio.get_running_loop()
    try:
        outputs = await loop.run_in_executor(
            process_pool,
            cridecoder.extract_usm,
            usm_path.as_posix(),
            staging_dir.as_posix(),
            None,
            False,
        )
        selected_output = None
        for output in outputs:
            try:
                candidate = validate_contained_file(
                    staging_dir,
                    StdPath(output).resolve().relative_to(staging_dir.resolve()).as_posix(),
                )
            except (ValueError, FileNotFoundError, SecurityError):
                logger.warning("Ignoring unsafe decoder output %s", output)
                continue
            if str(output).lower().endswith(".m2v"):
                selected_output = candidate
                break
            if selected_output is None:
                selected_output = candidate

        if selected_output is None:
            logger.warning("cridecoder produced no usable video stream for %s", usm_path)
            return None

        final_output = resolve_generated_child_path(output_root, selected_output.name)
        validate_output_target(output_root, final_output)
        os.replace(selected_output, final_output)
        return Path(final_output.as_posix())
    except (asyncio.CancelledError, asyncio.TimeoutError):
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    except Exception:
        logger.exception("Failed to demux %s with cridecoder", usm_path)
        return None
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


async def run_ffmpeg_video_to_mp4(
    input_path: Path,
    output_path: Path,
    config,
    *,
    get_encoder: GetEncoder,
    set_output_paths: SetOutputPaths,
) -> tuple[asyncio.subprocess.Process, str | None]:
    encoder_name, encoder_args = await get_encoder(config)
    output_path = Path(resolve_secure_path(output_path.parent, output_path.name).as_posix())
    command = ["ffmpeg", "-loglevel", "panic", "-y", "-i", input_path.as_posix()]
    if encoder_args:
        command.extend(encoder_args)
    else:
        command.extend(["-tune", "animation"])

    staging_dir = StdPath(tempfile.mkdtemp(prefix=".ffmpeg-", dir=output_path.parent))
    staged_output = staging_dir / output_path.name
    command.append(staged_output.as_posix())
    try:
        process = await asyncio.create_subprocess_exec(*command)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    set_output_paths(process, staged_output, staging_dir)
    return process, encoder_name


runtime = VideoRuntime()


_video_runtime = runtime
_get_shared_usm_process_pool = _shared_runtime.usm_process_pool
_get_shared_video_transcode_semaphore = _shared_runtime.video_transcode_semaphore


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

    usm_source: Path | None = None
    try:
        if usm_output_path.suffix.lower() == ".m2v":
            # The extract stage already demuxed the USM in memory; the job
            # carries the elementary video stream directly.
            m2v_path = usm_output_path
        else:
            usm_source = usm_output_path
            m2v_path = await _demux_usm_to_m2v(usm_source, config)
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
        for discarded_file in (m2v_path, usm_source):
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


# Above this size, USM demuxing falls back to the disk-streaming path so that
# concurrent extract workers do not hold whole movies in memory.
DEFAULT_USM_IN_MEMORY_MAX_BYTES = 64 * 1024 * 1024


def _get_usm_in_memory_limit(config) -> int:
    value = getattr(config, "USM_IN_MEMORY_MAX_BYTES", DEFAULT_USM_IN_MEMORY_MAX_BYTES)
    try:
        limit = int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid USM_IN_MEMORY_MAX_BYTES=%r, using default", value)
        return DEFAULT_USM_IN_MEMORY_MAX_BYTES
    return max(0, limit)


def _demux_usm_sources_in_memory(
    source_paths: list[StdPath],
    usm_output_path: StdPath,
    save_dir: StdPath,
    limit_bytes: int,
) -> StdPath | None:
    """Demux USM parts fully in memory to one ``.m2v`` next to the sources.

    Returns the written video-stream path, or ``None`` when the sources exceed
    the in-memory budget or the demux fails — callers then use the existing
    disk-streaming merge + file demux path.
    """
    try:
        total_bytes = sum(path.stat().st_size for path in source_paths)
    except OSError:
        return None
    if total_bytes > limit_bytes:
        return None

    try:
        usm_data = b"".join(path.read_bytes() for path in source_paths)
        streams = cridecoder.extract_usm_bytes(usm_data, None, False)
        selected = None
        for stream in streams:
            extension = str(stream.get("extension") or "").lower().lstrip(".")
            if extension == "m2v":
                selected = stream
                break
            if selected is None:
                selected = stream
        if selected is None:
            logger.warning("cridecoder produced no usable video stream for %s", usm_output_path)
            return None
        stream_data = selected["data"]
        if not isinstance(stream_data, bytes):
            raise TypeError("cridecoder returned non-bytes stream data")
        m2v_path = _resolve_generated_child_path(save_dir, f"{usm_output_path.stem}.m2v")
        validate_output_target(save_dir, m2v_path)
        atomic_write_bytes(m2v_path, stream_data)
        return m2v_path
    except Exception:
        logger.warning(
            "In-memory USM demux failed for %s, falling back to file demux",
            usm_output_path,
            exc_info=True,
        )
        return None
