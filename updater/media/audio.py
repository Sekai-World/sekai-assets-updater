"""Low-level HCA decoding and FFmpeg audio encoding."""

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path as StdPath
from typing import Literal, assert_never

from anyio import Path

from updater.external_process import (
    get_external_process_timeout as _get_external_process_timeout,
)
from updater.extract.paths import canonical_root as _canonical_root
from updater.media.hca import decode_hca_to_wav_bytes
from updater.media.process import (
    _cleanup_process_output as _cleanup_process_output,
)
from updater.media.process import (
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


_audio_runtime = runtime
_get_shared_hca_decode_semaphore = _shared_runtime.hca_decode_semaphore
_get_shared_audio_encoder_semaphore = _shared_runtime.audio_encoder_semaphore


def _get_hca_decode_backend(config) -> HcaDecodeBackend:
    return _audio_runtime.decode_backend(config)


def _get_vgmstream_cli() -> str | None:
    return _audio_runtime.vgmstream_cli()


def _report_hca_decoder(message: str) -> None:
    _audio_runtime.report_decoder(message)


async def _run_hca_to_wav_with_cridecoder(
    input_path: Path,
    output_path: Path,
    config,
) -> bool:
    return await run_hca_with_cridecoder(
        input_path,
        output_path,
        report_decoder=_report_hca_decoder,
        decode_bytes=decode_hca_to_wav_bytes,
    )


async def _run_hca_to_wav_with_vgmstream(
    input_path: Path,
    output_path: Path,
    config,
) -> bool:
    return await run_hca_with_vgmstream(
        input_path,
        output_path,
        executable=_get_vgmstream_cli(),
        report_decoder=_report_hca_decoder,
        communicate=_communicate_with_process,
        timeout=_get_external_process_timeout(config),
        set_output_paths=_set_process_output_paths,
    )


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
    return await run_ffmpeg_audio_encode(
        input_path,
        output_path,
        semaphore=_get_shared_audio_encoder_semaphore(config),
        wait=_wait_for_process,
        timeout=_get_external_process_timeout(config),
        set_output_paths=_set_process_output_paths,
        cleanup_output=_cleanup_process_output,
    )


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
