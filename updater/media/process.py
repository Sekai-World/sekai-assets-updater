"""Subprocess wait wrappers for media jobs, bound to the bundle logger."""

import logging

from updater.external_process import (
    EXTERNAL_PROCESS_TERMINATE_GRACE,
    TERMINATE_TASK_ATTRIBUTE,
    cleanup_process_output,
    set_process_output_paths,
    terminate_process,
    wait_for_process,
)

logger = logging.getLogger("live2d")

_set_process_output_paths = set_process_output_paths


async def _terminate_process(process) -> None:
    await terminate_process(process, EXTERNAL_PROCESS_TERMINATE_GRACE)


async def _wait_for_process(process, time_budget: float) -> int:
    return await wait_for_process(
        process,
        time_budget,
        _terminate_process,
        task_attribute=TERMINATE_TASK_ATTRIBUTE,
        logger=logger,
    )


async def _communicate_with_process(process, time_budget: float) -> tuple[bytes, bytes]:
    return await wait_for_process(
        process,
        time_budget,
        _terminate_process,
        task_attribute=TERMINATE_TASK_ATTRIBUTE,
        logger=logger,
        communicate=True,
    )


def _cleanup_process_output(process, *, remove_direct_output: bool = False) -> None:
    cleanup_process_output(process, remove_direct_output=remove_direct_output, logger=logger)
