"""Cancellation-safe lifecycle helpers for external processes."""

import asyncio
import logging
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

TerminateProcess = Callable[[object], Coroutine[Any, Any, None]]


async def terminate_process(process, grace_period: float) -> None:
    """Terminate a child, kill it if needed, and wait until it has exited."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), grace_period)
    except asyncio.TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def ensure_process_terminated(
    process,
    terminate: TerminateProcess,
    *,
    task_attribute: str,
    logger: logging.Logger,
) -> BaseException | None:
    """Run one termination/reap sequence, even if the waiter is cancelled repeatedly."""
    task = getattr(process, task_attribute, None)
    if task is None:
        task = asyncio.create_task(terminate(process))
        setattr(process, task_attribute, task)

    cancellation_seen = False
    cleanup_error: BaseException | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancellation_seen = True
            if task.done():
                break
        except Exception as exc:
            cleanup_error = exc
            break

    if task.done():
        if task.cancelled():
            cancellation_seen = True
        elif cleanup_error is None:
            cleanup_error = task.exception()

    if cancellation_seen:
        if cleanup_error is not None:
            logger.error(
                "Process termination cleanup failed while propagating cancellation: %s",
                cleanup_error,
            )
        raise asyncio.CancelledError() from None
    return cleanup_error


def set_process_output_paths(process, output_path: Path, staging_dir: Path) -> None:
    """Associate an external process with its private output staging area."""
    process._bundle_output_path = output_path
    process._bundle_staging_dir = staging_dir


def cleanup_process_output(
    process, *, remove_direct_output: bool = False, logger: logging.Logger
) -> None:
    """Remove process artifacts after the process has been fully reaped."""
    staging_dir = getattr(process, "_bundle_staging_dir", None)
    if staging_dir is not None:
        shutil.rmtree(staging_dir, ignore_errors=True)

    if remove_direct_output:
        output_path = getattr(process, "_bundle_output_path", None)
        if output_path is not None:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Failed to remove failed process output %s", output_path)


async def wait_for_process(
    process,
    timeout: float,
    terminate: TerminateProcess,
    *,
    task_attribute: str,
    logger: logging.Logger,
    communicate: bool = False,
):
    original_error: BaseException | None = None
    cancellation = False
    try:
        async with asyncio.timeout(timeout):
            if communicate:
                return await process.communicate()
            return await process.wait()
    except asyncio.CancelledError as exc:
        original_error = exc
        cancellation = True
    except asyncio.TimeoutError as exc:
        original_error = exc

    cleanup_error = await ensure_process_terminated(
        process,
        terminate,
        task_attribute=task_attribute,
        logger=logger,
    )
    cleanup_process_output(process, remove_direct_output=True, logger=logger)
    if cancellation:
        raise asyncio.CancelledError() from None
    if cleanup_error is not None:
        raise cleanup_error from None
    raise original_error
