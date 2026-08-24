"""Low-level HCA decoding and FFmpeg audio encoding."""

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor
from pathlib import Path as StdPath
from typing import Literal

from anyio import Path

from security import (
    SecurityError,
    resolve_secure_path,
    secure_existing_output,
    validate_output_target,
)

logger = logging.getLogger("live2d")

HcaDecodeBackend = Literal["auto", "python", "vgmstream"]
Communicate = Callable[[object, float], Awaitable[tuple[bytes, bytes]]]
Wait = Callable[[object, float], Awaitable[int]]
SetOutputPaths = Callable[[object, StdPath, StdPath], None]
CleanupOutput = Callable[..., None]


class AudioRuntime:
    def __init__(self) -> None:
        self._vgmstream_cli: str | None = None
        self._vgmstream_cli_checked = False
        self._reported_decoder: str | None = None

    @staticmethod
    def decode_backend(config) -> HcaDecodeBackend:
        backend = str(getattr(config, "HCA_DECODE_BACKEND", "auto")).strip().lower()
        if backend in {"auto", "python", "vgmstream"}:
            return backend  # type: ignore[return-value]
        logger.warning("Unknown HCA_DECODE_BACKEND=%r, falling back to auto", backend)
        return "auto"

    def vgmstream_cli(self) -> str | None:
        if self._vgmstream_cli_checked:
            return self._vgmstream_cli

        candidates: list[str] = []
        if env_candidate := os.environ.get("VGMSTREAM_CLI"):
            candidates.append(env_candidate)
        candidates.append("vgmstream-cli")

        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                self._vgmstream_cli = resolved
                break
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                self._vgmstream_cli = candidate
                break

        self._vgmstream_cli_checked = True
        return self._vgmstream_cli

    def report_decoder(self, message: str) -> None:
        if self._reported_decoder != message:
            self._reported_decoder = message
            logger.info(message)


async def run_hca_with_cridecoder(
    input_path: Path,
    output_path: Path,
    *,
    process_pool: Executor,
    concurrency: int,
    report_decoder: Callable[[str], None],
    decoder: Callable[[str, str], None],
) -> bool:
    staging_dir: StdPath | None = None
    try:
        report_decoder(
            f"Using cridecoder for HCA decoding via process pool ({concurrency} workers)"
        )
        output_root = StdPath(output_path.parent.as_posix())
        staging_dir = StdPath(tempfile.mkdtemp(prefix=".hca-", dir=output_root))
        staged_output = staging_dir / output_path.name
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            process_pool,
            decoder,
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


async def run_hca_with_vgmstream(
    input_path: Path,
    output_path: Path,
    *,
    executable: str | None,
    report_decoder: Callable[[str], None],
    communicate: Communicate,
    timeout: float,
    set_output_paths: SetOutputPaths,
) -> bool:
    if executable is None:
        return False
    report_decoder(f"Using vgmstream-cli for HCA decoding: {executable}")

    staging_dir: StdPath | None = None
    try:
        output_root = StdPath(output_path.parent.as_posix())
        staging_dir = StdPath(tempfile.mkdtemp(prefix=".hca-", dir=output_root))
        staged_output = staging_dir / output_path.name
        validate_output_target(output_root, output_path)
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
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
        set_output_paths(process, staged_output, staging_dir)
        stdout, stderr = await communicate(process, timeout)
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


async def run_ffmpeg_audio_encode(
    input_path: Path,
    output_path: Path,
    *,
    semaphore: asyncio.Semaphore,
    wait: Wait,
    timeout: float,
    set_output_paths: SetOutputPaths,
    cleanup_output: CleanupOutput,
) -> bool:
    async with semaphore:
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
        set_output_paths(process, staged_output, staging_dir)
        try:
            if await wait(process, timeout) != 0:
                return False
            secure_existing_output(staging_dir, staged_output)
            validate_output_target(output_path.parent, output_path)
            os.replace(staged_output, output_path)
        except (FileNotFoundError, ValueError, SecurityError):
            return False
        finally:
            cleanup_output(process)
        return True


runtime = AudioRuntime()
