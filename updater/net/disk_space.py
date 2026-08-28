"""Free-disk-space admission gate for new downloads."""

import asyncio
import logging
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from anyio import Path

logger = logging.getLogger("asset_updater")

DEFAULT_MIN_FREE_DISK_BYTES = 1024 * 1024 * 1024
DEFAULT_DOWNLOAD_DISK_SPACE_CHECK_INTERVAL = 5.0


def get_min_free_disk_bytes(config=None) -> int:
    value = getattr(config, "MIN_FREE_DISK_BYTES", DEFAULT_MIN_FREE_DISK_BYTES)
    try:
        min_free_bytes = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid MIN_FREE_DISK_BYTES=%r, falling back to %d",
            value,
            DEFAULT_MIN_FREE_DISK_BYTES,
        )
        min_free_bytes = DEFAULT_MIN_FREE_DISK_BYTES
    return max(0, min_free_bytes)


def get_download_disk_space_check_interval(config=None) -> float:
    value = getattr(
        config,
        "DOWNLOAD_DISK_SPACE_CHECK_INTERVAL",
        DEFAULT_DOWNLOAD_DISK_SPACE_CHECK_INTERVAL,
    )
    try:
        check_interval = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid DOWNLOAD_DISK_SPACE_CHECK_INTERVAL=%r, falling back to %s",
            value,
            DEFAULT_DOWNLOAD_DISK_SPACE_CHECK_INTERVAL,
        )
        check_interval = DEFAULT_DOWNLOAD_DISK_SPACE_CHECK_INTERVAL
    return max(0.1, check_interval)


def get_download_target_path(config) -> Path:
    bundle_dir = getattr(config, "ASSET_LOCAL_BUNDLE_CACHE_DIR", None)
    if isinstance(bundle_dir, Path):
        return bundle_dir
    return Path(tempfile.gettempdir())


def _resolve_disk_usage_path(path: Path) -> str:
    candidate = path.as_posix()
    while not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if not parent or parent == candidate:
            break
        candidate = parent
    return candidate


class DownloadDiskSpaceGate:
    def __init__(
        self,
        target_path: Path,
        min_free_bytes: int,
        check_interval: float,
    ):
        self.target_path = target_path
        self.min_free_bytes = max(0, min_free_bytes)
        self.check_interval = max(0.1, check_interval)
        self._disk_usage_path = _resolve_disk_usage_path(target_path)
        self._reserved_bytes = 0
        self._condition = asyncio.Condition()

    @property
    def reserved_bytes(self) -> int:
        return self._reserved_bytes

    def _get_free_bytes(self) -> int:
        return shutil.disk_usage(self._disk_usage_path).free

    async def _acquire(self, required_bytes: int, label: str) -> None:
        required_bytes = max(0, required_bytes)
        required_free_bytes = self.min_free_bytes + required_bytes
        last_wait_log_at = 0.0

        async with self._condition:
            while True:
                free_bytes = self._get_free_bytes()
                available_bytes = free_bytes - self._reserved_bytes
                if available_bytes >= required_free_bytes:
                    self._reserved_bytes += required_bytes
                    logger.debug(
                        "Reserved %d bytes for %s on %s (free=%d reserved=%d)",
                        required_bytes,
                        label,
                        self._disk_usage_path,
                        free_bytes,
                        self._reserved_bytes,
                    )
                    return

                now = time.monotonic()
                if now - last_wait_log_at >= 30:
                    logger.warning(
                        "Waiting for free disk space before downloading %s: free=%d reserved=%d required=%d path=%s",
                        label,
                        free_bytes,
                        self._reserved_bytes,
                        required_free_bytes,
                        self._disk_usage_path,
                    )
                    last_wait_log_at = now

                try:
                    await asyncio.wait_for(
                        self._condition.wait(),
                        timeout=self.check_interval,
                    )
                except asyncio.TimeoutError:
                    continue

    async def _release(self, required_bytes: int) -> None:
        required_bytes = max(0, required_bytes)
        async with self._condition:
            self._reserved_bytes = max(0, self._reserved_bytes - required_bytes)
            self._condition.notify_all()

    @asynccontextmanager
    async def reserve(
        self,
        required_bytes: int,
        label: str,
    ) -> AsyncIterator[None]:
        await self._acquire(required_bytes, label)
        try:
            yield
        finally:
            await self._release(required_bytes)


def build_download_disk_space_gate(config) -> DownloadDiskSpaceGate | None:
    min_free_bytes = get_min_free_disk_bytes(config)
    if min_free_bytes <= 0:
        return None

    return DownloadDiskSpaceGate(
        target_path=get_download_target_path(config),
        min_free_bytes=min_free_bytes,
        check_interval=get_download_disk_space_check_interval(config),
    )
