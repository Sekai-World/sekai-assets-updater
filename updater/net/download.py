"""Streaming bundle download with retry/backoff and XOR header deobfuscation."""

import asyncio
import logging
import os
import random
import tempfile
from pathlib import Path as StdPath
from typing import Dict

import aiohttp
from anyio import Path, open_file

from updater.net.http import get_download_http_session_options
from updater.net.integrity import (
    DownloadIntegrityError,
    RetryableDownloadError,
)
from updater.net.integrity import (
    validate_unityfs_bundle as _validate_unityfs_bundle,
)
from updater.sanitize import sanitize_http_log_value, sanitize_url
from updater.security import resolve_secure_path, validate_output_target

# Historic logger name for download records, preserved so existing log
# routing/filtering keeps seeing these events on the same channel.
logger = logging.getLogger("live2d")

DEFAULT_DOWNLOAD_MAX_RETRIES = 3
DEFAULT_DOWNLOAD_RETRY_BASE_DELAY = 1.0
DEFAULT_DOWNLOAD_RETRY_MAX_DELAY = 30.0


def get_download_max_retries(config=None) -> int:
    value = getattr(config, "DOWNLOAD_MAX_RETRIES", DEFAULT_DOWNLOAD_MAX_RETRIES)
    try:
        retries = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid DOWNLOAD_MAX_RETRIES=%r, falling back to %d",
            value,
            DEFAULT_DOWNLOAD_MAX_RETRIES,
        )
        retries = DEFAULT_DOWNLOAD_MAX_RETRIES
    return max(1, retries)


def get_download_retry_base_delay(config=None) -> float:
    value = getattr(config, "DOWNLOAD_RETRY_BASE_DELAY", DEFAULT_DOWNLOAD_RETRY_BASE_DELAY)
    try:
        delay = float(value)
    except (TypeError, ValueError):
        delay = DEFAULT_DOWNLOAD_RETRY_BASE_DELAY
    return max(0.0, delay)


def get_download_retry_max_delay(config=None) -> float:
    value = getattr(config, "DOWNLOAD_RETRY_MAX_DELAY", DEFAULT_DOWNLOAD_RETRY_MAX_DELAY)
    try:
        delay = float(value)
    except (TypeError, ValueError):
        delay = DEFAULT_DOWNLOAD_RETRY_MAX_DELAY
    return max(0.0, delay)


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
