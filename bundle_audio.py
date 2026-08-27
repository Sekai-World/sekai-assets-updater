"""Low-level HCA decoding and FFmpeg audio encoding."""

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path as StdPath
from typing import Literal

from anyio import Path

from security import (
    SecurityError,
    atomic_write_bytes,
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
    report_decoder: Callable[[str], None],
    decode_bytes: Callable[[bytes], bytes],
) -> bool:
    """Decode one HCA fully in memory on a worker thread.

    cridecoder releases the GIL during decoding (0.3.5+), so a thread is
    enough for parallelism and the payload never crosses a process boundary
    or touches a staging directory.
    """
    try:
        report_decoder("Using cridecoder for in-memory HCA decoding")
        secure_output = Path(resolve_secure_path(output_path.parent, output_path.name).as_posix())
        hca_data = await Path(str(input_path)).read_bytes()
        wav_data = await asyncio.to_thread(decode_bytes, hca_data)
        atomic_write_bytes(StdPath(secure_output.as_posix()), wav_data)
    except Exception:
        logger.exception("Failed to decode %s with cridecoder", input_path)
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
