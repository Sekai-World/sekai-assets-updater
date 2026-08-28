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

from updater.security import (
    SecurityError,
    resolve_secure_path,
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
