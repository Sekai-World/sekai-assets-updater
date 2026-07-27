import asyncio
import base64
import inspect
import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookies import SimpleCookie
from logging.handlers import QueueHandler, QueueListener
from queue import SimpleQueue
from string import Formatter
from typing import AsyncIterator, Dict, List, Mapping, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import orjson as json
from anyio import Path, open_file

from security import derive_remote_key, validate_contained_file
from state import (
    StateNotFoundError,
    load_asset_metadata,
    load_game_version,
)

logger = logging.getLogger("asset_updater")

DEFAULT_REQUEST_TIMEOUT = 30 * 60
DEFAULT_DOWNLOAD_MAX_RETRIES = 3
DEFAULT_DOWNLOAD_RETRY_BASE_DELAY = 1.0
DEFAULT_DOWNLOAD_RETRY_MAX_DELAY = 30.0
DEFAULT_MIN_FREE_DISK_BYTES = 1024 * 1024 * 1024
DEFAULT_DOWNLOAD_DISK_SPACE_CHECK_INTERVAL = 5.0
DEFAULT_EXTERNAL_PROCESS_TIMEOUT = 300.0
_EXTERNAL_PROCESS_TERMINATE_GRACE = 2.0

_SENSITIVE_HEADER_RE = re.compile(
    r"(?:^|[-_])(authorization|cookie|set-cookie|api[-_]?key|api[-_]?token|"
    r"access[-_]?token|refresh[-_]?token|auth[-_]?token|client[-_]?secret|"
    r"secret|credential)(?:$|[-_])",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?:^|[-_])(signature|sig|token|access[-_]?token|auth|authorization|"
    r"api[-_]?key|secret|credential|policy|key|key[-_]?id|access[-_]?key|"
    r"aws[-_]?access[-_]?key[-_]?id|expires|date)(?:$|[-_])|"
    r"^(?:x[-_]amz|cloudfront)[-_]",
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"]+")
_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?<![\w-])"
    r"(?P<prefix>(?:token|api[-_]key|api[-_]token|access[-_]token|refresh[-_]token|"
    r"auth[-_]token|authorization|proxy[-_]authorization|client[-_]secret|"
    r"secret|credential|body|response[-_]body)\b\s*[:=]\s*)"
    r"(?:(?P<single>'(?P<single_value>[^']*)')|"
    r"(?P<double>\"(?P<double_value>[^\"]*)\")|"
    r"(?P<bare>[^\s,;]+))",
    re.IGNORECASE,
)
_TOKEN_HEADER_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\bX[-_](?:Api[-_]Token|Access[-_]Token)\b\s*[:=]\s*)"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
    re.IGNORECASE,
)
_API_KEY_HEADER_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\bX[-_]Api[-_]Key\b\s*[:=]\s*)"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
    re.IGNORECASE,
)
_AUTHORIZATION_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\b(?:authorization|proxy[-_]authorization)\b\s*[:=]\s*)"
    r"(?:(?:Bearer|Basic)\s+)?"
    r"(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
    re.IGNORECASE,
)
_COOKIE_MORSEL_RE = re.compile(
    r"(?P<name>[^\s=;,]+)(?P<separator>\s*=\s*)"
    r"(?:(?P<single>'[^']*')|(?P<double>\"[^\"]*\")|(?P<bare>[^;\s,]+))",
)
_COOKIE_TEXT_RE = re.compile(
    r"(?P<prefix>\bCookie\b\s*[:=]\s*)(?P<value>[^\r\n]*)",
    re.IGNORECASE,
)
_REDACTED = "<redacted>"


@dataclass(frozen=True)
class DownloadPlan:
    """In-memory selection result; construction performs no persistence."""

    candidates: List[Tuple[str, Dict]]
    asset_metadata: Dict
    game_version: Dict


class LocalQueueHandler(QueueHandler):
    def emit(self, record: logging.LogRecord) -> None:
        # Removed the call to self.prepare(), handle task cancellation
        try:
            self.enqueue(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.handleError(record)


def setup_logging_queue() -> None:
    """Move log handlers to a separate thread.

    Replace handlers on the root logger with a LocalQueueHandler,
    and start a logging.QueueListener holding the original
    handlers.

    """
    queue = SimpleQueue()
    root = logging.getLogger()

    handlers: List[logging.Handler] = []

    handler = LocalQueueHandler(queue)
    root.addHandler(handler)
    for h in root.handlers[:]:
        if h is not handler:
            root.removeHandler(h)
            handlers.append(h)

    listener = QueueListener(queue, *handlers, respect_handler_level=True)
    listener.start()


async def ensure_dir_exists(dir_path: Path):
    """Ensure the directory exists, create it if not."""
    if not await dir_path.exists():
        await dir_path.mkdir(parents=True, exist_ok=True)

    if not await dir_path.is_dir():
        raise NotADirectoryError(
            f"Failed to create directory {dir_path}, path exists but is not a directory"
        )


def get_bundle_checksum(bundle: Dict) -> Tuple[str | None, str]:
    """Return the best available checksum field for a bundle.

    Colorful Palette servers currently expose `hash`, while tc/cn/kr may leave
    `hash` empty and require `crc` for change detection.
    """
    bundle_hash = bundle.get("hash")
    if bundle_hash:
        return "hash", str(bundle_hash)

    bundle_crc = bundle.get("crc")
    if bundle_crc not in (None, ""):
        return "crc", str(bundle_crc)

    return None, ""


def bundle_has_changed(bundle: Dict, cached_bundle: Dict | None) -> bool:
    """Compare two bundle records using the checksum fields they actually expose."""
    cached_bundle = cached_bundle or {}

    bundle_hash = bundle.get("hash")
    cached_hash = cached_bundle.get("hash")
    if bundle_hash and cached_hash:
        return str(bundle_hash) != str(cached_hash)

    bundle_crc = bundle.get("crc")
    cached_crc = cached_bundle.get("crc")
    if bundle_crc not in (None, "") and cached_crc not in (None, ""):
        return str(bundle_crc) != str(cached_crc)

    return get_bundle_checksum(bundle) != get_bundle_checksum(cached_bundle)


def get_template_placeholders(template: str) -> set[str]:
    return {
        field_name.split(".", 1)[0].split("[", 1)[0]
        for _, field_name, _, _ in Formatter().parse(template)
        if field_name
    }


def format_url_template(template: str, **values: str | None) -> str:
    placeholders = get_template_placeholders(template)
    missing_placeholders = [
        name for name in placeholders if name not in values or values[name] is None
    ]
    if missing_placeholders:
        missing_fields = ", ".join(sorted(missing_placeholders))
        raise ValueError(f"Missing format values for {missing_fields}: {template}")

    normalized_values = {}
    for name in placeholders:
        value = values[name]
        if isinstance(value, str):
            normalized_values[name] = value.strip()
        else:
            normalized_values[name] = value
    return template.format(**normalized_values)


def sanitize_headers(headers: Mapping | None) -> dict[str, str]:
    """Return headers safe to include in logs.

    Header names are treated case-insensitively.  In particular, this avoids
    accidentally exposing credentials when a caller uses a differently cased
    spelling of ``Cookie`` or an API key header.
    """
    if not headers:
        return {}
    sanitized = {}
    for name, value in headers.items():
        name_text = str(name)
        if name_text.casefold() == "cookie":
            sanitized[name_text] = _sanitize_cookie_value(str(value))
        else:
            sanitized[name_text] = (
                _REDACTED if _SENSITIVE_HEADER_RE.search(name_text) else str(value)
            )
    return sanitized


def _sanitize_cookie_value(value: str) -> str:
    sanitized = _COOKIE_MORSEL_RE.sub(
        lambda match: f"{match.group('name')}{match.group('separator')}{_REDACTED}",
        value,
    )
    return sanitized if sanitized != value else _REDACTED


def _sanitize_assignment(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{_REDACTED}"


def sanitize_url(url: str) -> str:
    """Redact signed and credential-bearing query values without hiding the URL."""
    try:
        parts = urlsplit(str(url))
        query = urlencode(
            [
                (key, _REDACTED if _SENSITIVE_QUERY_RE.search(key) else value)
                for key, value in parse_qsl(parts.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))
    except (TypeError, ValueError):
        # Logging must not turn an otherwise useful error into a second error.
        return "<invalid-url>"


def sanitize_http_log_value(value):
    """Sanitize common HTTP values before they are passed to a logger."""
    if isinstance(value, Mapping):
        return sanitize_headers(value)
    if isinstance(value, str) and "://" in value:
        value = _HTTP_URL_RE.sub(lambda match: sanitize_url(match.group(0)), value)
    if isinstance(value, str):
        value = _COOKIE_TEXT_RE.sub(
            lambda match: f"{match.group('prefix')}{_sanitize_cookie_value(match.group('value'))}",
            value,
        )
        value = _API_KEY_HEADER_ASSIGNMENT_RE.sub(_sanitize_assignment, value)
        value = _TOKEN_HEADER_ASSIGNMENT_RE.sub(_sanitize_assignment, value)
        value = _AUTHORIZATION_ASSIGNMENT_RE.sub(_sanitize_assignment, value)
        value = _SENSITIVE_ASSIGNMENT_RE.sub(_sanitize_assignment, value)
    return value


def sanitize_log_label(value) -> str:
    """Return a safe, printable label for pipeline diagnostics."""
    return str(sanitize_http_log_value(str(value)))


def get_request_timeout(config=None) -> aiohttp.ClientTimeout:
    timeout_value = getattr(config, "REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)

    if timeout_value in (None, 0, 0.0):
        return aiohttp.ClientTimeout(total=None)

    try:
        timeout_seconds = float(timeout_value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid REQUEST_TIMEOUT=%r, falling back to %ss",
            timeout_value,
            DEFAULT_REQUEST_TIMEOUT,
        )
        timeout_seconds = float(DEFAULT_REQUEST_TIMEOUT)

    if timeout_seconds <= 0:
        return aiohttp.ClientTimeout(total=None)

    return aiohttp.ClientTimeout(total=timeout_seconds)


def get_http_session_options(config=None) -> dict[str, object]:
    """Build the common aiohttp session options for configured HTTP requests."""
    return {
        "proxy": getattr(config, "PROXY_URL", None),
        "timeout": get_request_timeout(config),
    }


def _get_external_process_timeout(config=None) -> float:
    value = getattr(config, "EXTERNAL_PROCESS_TIMEOUT", DEFAULT_EXTERNAL_PROCESS_TIMEOUT)
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"EXTERNAL_PROCESS_TIMEOUT must be positive, got {value!r}") from None
    if timeout <= 0:
        raise ValueError(f"EXTERNAL_PROCESS_TIMEOUT must be positive, got {value!r}")
    return timeout


async def _terminate_process(process) -> None:
    """Terminate a child, kill it after the grace period, and await exit."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), _EXTERNAL_PROCESS_TERMINATE_GRACE)
    except asyncio.TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def _ensure_process_terminated(process) -> tuple[BaseException | None, bool]:
    """Run one termination/reap sequence, even if the waiter is cancelled repeatedly.

    Cancellation is remembered while the child is being reaped and reported to
    the caller only after cleanup has completed.  This keeps cancellation from
    being swallowed without allowing it to interrupt the termination sequence.
    """
    task = getattr(process, "_helpers_terminate_task", None)
    if task is None:
        task = asyncio.create_task(_terminate_process(process))
        process._helpers_terminate_task = task

    cancellation_seen = False
    cleanup_error: BaseException | None = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            # Keep waiting on the same shielded task. Never start a second
            # termination sequence while the first one is still in progress.
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
            # ``Task.exception`` returns the original exception object without
            # catching it, so its traceback and cancellation state are not
            # replaced by this cleanup bookkeeping.
            cleanup_error = task.exception()

    if cancellation_seen:
        if cleanup_error is not None:
            logger.error(
                "Process termination cleanup failed while propagating cancellation: %s",
                cleanup_error,
            )
        return None, True
    return cleanup_error, False


async def _wait_for_process(process, timeout: float) -> int:
    original_error: BaseException | None = None
    cancellation = False
    try:
        return await asyncio.wait_for(process.wait(), timeout)
    except asyncio.CancelledError as exc:
        original_error = exc
        cancellation = True
    except asyncio.TimeoutError as exc:
        original_error = exc

    cleanup_error, cleanup_cancelled = await _ensure_process_terminated(process)
    if cancellation or cleanup_cancelled:
        raise asyncio.CancelledError() from None
    if cleanup_error is not None:
        raise cleanup_error from None
    raise original_error


def build_metadata_headers(config) -> dict[str, str]:
    """Headers allowed on metadata and game API requests."""
    headers = {
        "Accept": "*/*",
        "X-Unity-Version": config.UNITY_VERSION,
    }
    if config.USER_AGENT:
        headers["User-Agent"] = config.USER_AGENT
    return headers


def build_cookie_request_headers() -> dict[str, str]:
    """Cookie acquisition must not receive public or credential-bearing headers."""
    return {}


def build_cdn_headers(cookie: str | None = None) -> dict[str, str]:
    """Build headers for a CDN request without adding public API headers.

    This is intentionally separate from :func:`build_metadata_headers` so
    future download callers cannot accidentally send Unity/API headers to a
    signed CDN endpoint.
    """
    return {"Cookie": cookie} if cookie else {}


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


async def get_download_list(
    asset_bundle_info: Dict,
    game_version_json: Dict,
    config=None,
    assetver: str | None = None,
    assetbundle_host_hash: str | None = None,
    include_list: List[str] | None = None,
    exclude_list: List[str] | None = None,
    priority_list: List[str] | None = None,
    force_full_download: bool = False,
    automatic_prefixes: tuple[str, ...] = (),
    bundle_cache_path_resolver=None,
    asset_bundle_info_for_cache: Dict | None = None,
) -> DownloadPlan:
    """Generate the download list for the asset bundles.

    Args:
        asset_bundle_info (Dict): current asset bundle info
        game_version_json (Dict): current game version json
        config (Module, optional): configurations. Defaults to None.
        assetver (str, optional): asset ver used by nuverse servers. Defaults to None.
        assetbundle_host_hash (str, optional): host hash used by colorful palette servers. Defaults to None.

    Returns:
        List[Tuple[str, Dict]]: download list of asset bundles
    """

    cached_asset_bundle_info = None
    cached_game_version_json = None
    assert config, "Config must be provided to get_download_list"
    assert config.ASSET_BUNDLE_INFO_CACHE_PATH, "ASSET_BUNDLE_INFO_CACHE_PATH must be set in config"
    assert config.GAME_VERSION_JSON_CACHE_PATH, "GAME_VERSION_JSON_CACHE_PATH must be set in config"
    if not force_full_download:
        try:
            cached_asset_bundle_info = load_asset_metadata(config.ASSET_BUNDLE_INFO_CACHE_PATH)
        except StateNotFoundError:
            pass
        try:
            cached_game_version_json = load_game_version(config.GAME_VERSION_JSON_CACHE_PATH)
        except StateNotFoundError:
            pass

    if assetver is not None:
        game_version_json = dict(game_version_json)
        game_version_json["assetver"] = assetver

    download_list = []
    current_bundles: Dict[str, Dict] = asset_bundle_info.get("bundles", {})
    assert current_bundles, "bundles must be set in asset bundle info"
    asset_bundle_url_placeholders = get_template_placeholders(config.ASSET_BUNDLE_URL)
    current_bundles = select_bundles_for_download(
        current_bundles,
        include_list=include_list,
        exclude_list=exclude_list,
        automatic_prefixes=automatic_prefixes,
    )
    if not current_bundles:
        raise ValueError("No bundles found after filtering")

    async def select_changed_bundles(cached_bundles: Dict[str, Dict]) -> list[Dict]:
        changed_bundles = []
        for bundle in current_bundles.values():
            if bundle_has_changed(bundle, cached_bundles.get(bundle.get("bundleName", ""), {})):
                changed_bundles.append(bundle)
                continue
            if bundle_cache_path_resolver is None:
                continue
            cache_path = bundle_cache_path_resolver(bundle)
            if cache_path is None:
                continue
            exists = cache_path.exists()
            if inspect.isawaitable(exists):
                exists = await exists
            if not exists:
                changed_bundles.append(bundle)
        return changed_bundles

    if cached_asset_bundle_info and cached_game_version_json:
        cached_bundles: Dict[str, Dict] = cached_asset_bundle_info.get("bundles") or {}
        changed_bundles = await select_changed_bundles(cached_bundles)

        if assetver:
            # Generate the download list from changed bundles
            app_version: str = (
                getattr(config, "APP_VERSION_OVERRIDE", None)
                or game_version_json.get("appVersion")
                or ""
            )
            assert app_version, "App version must be set in game version json or config"
            download_list = [
                (
                    format_url_template(
                        config.ASSET_BUNDLE_URL,
                        appVersion=app_version,
                        bundleName=bundle.get("bundleName"),
                        downloadPath=bundle.get("downloadPath"),
                    ),
                    bundle,
                )
                for bundle in changed_bundles
            ]
        else:
            # Colorful Palette servers
            cached_bundles: Dict[str, Dict] = cached_asset_bundle_info.get("bundles") or {}

            # Compare each bundle checksum and include new bundles as well.
            changed_bundles = [
                bundle
                for bundle in current_bundles.values()
                if bundle_has_changed(
                    bundle,
                    cached_bundles.get(bundle.get("bundleName", ""), {}),
                )
            ]

            # Generate the download list from changed bundles
            asset_hash: str = game_version_json.get("assetHash", "")
            asset_bundle_url_args = {
                "assetbundleHostHash": assetbundle_host_hash,
            }
            if "version" in asset_bundle_url_placeholders:
                version = asset_bundle_info.get("version")
                assert version, "Version must be set in asset bundle info"
                asset_bundle_url_args["version"] = version
            if asset_hash:
                asset_bundle_url_args["assetHash"] = asset_hash
            download_list = [
                (
                    format_url_template(
                        config.ASSET_BUNDLE_URL,
                        **asset_bundle_url_args,
                        bundleName=bundle.get("bundleName"),
                    ),
                    bundle,
                )
                for bundle in changed_bundles
            ]

    else:
        # Get the download list for a full download
        asset_hash: str = game_version_json.get("assetHash", "")
        app_version: str = (
            getattr(config, "APP_VERSION_OVERRIDE", None)
            or game_version_json.get("appVersion")
            or ""
        )
        assert app_version, "App version must be set in game version json or config"
        asset_bundle_url_args = {
            "assetbundleHostHash": assetbundle_host_hash,
            "appVersion": app_version,
        }
        if "version" in asset_bundle_url_placeholders:
            version = asset_bundle_info.get("version")
            assert version, "Version must be set in asset bundle info"
            asset_bundle_url_args["version"] = version
        if asset_hash:
            asset_bundle_url_args["assetHash"] = asset_hash

        download_list = [
            (
                format_url_template(
                    config.ASSET_BUNDLE_URL,
                    **asset_bundle_url_args,
                    bundleName=bundle.get("bundleName"),
                    downloadPath=bundle.get("downloadPath"),
                ),
                bundle,
            )
            for bundle in current_bundles.values()
        ]

    if download_list:
        download_list = await sort_download_list(
            download_list,
            priority_list=priority_list,
        )

    metadata_source = asset_bundle_info if asset_bundle_info_for_cache is None else asset_bundle_info_for_cache
    normalized_metadata = {
        "version": metadata_source.get("version", ""),
        "os": metadata_source.get("os", ""),
        "bundles": metadata_source.get("bundles", {}),
    }
    return DownloadPlan(download_list, normalized_metadata, game_version_json)


def select_bundles_for_download(
    bundles: Dict[str, Dict],
    include_list: List[str] | None = None,
    exclude_list: List[str] | None = None,
    automatic_prefixes: tuple[str, ...] = (),
) -> Dict[str, Dict]:
    """Select user bundles and merge mandatory specialized bundles."""
    selected: Dict[str, Dict] = {}
    selected_names: set[str] = set()
    for key, value in bundles.items():
        bundle_name = value.get("bundleName") or ""
        user_selected = (
            (not include_list or any(re.match(pattern, bundle_name) for pattern in include_list))
            and not any(re.match(pattern, bundle_name) for pattern in (exclude_list or []))
        )
        automatic_selected = bundle_name.startswith(automatic_prefixes)
        if (user_selected or automatic_selected) and bundle_name not in selected_names:
            selected[key] = value
            selected_names.add(bundle_name)
    return selected


async def filter_bundles(
    bundles: Dict[str, Dict],
    include_list: List[str] | None = None,
    exclude_list: List[str] | None = None,
) -> Dict[str, Dict]:
    """Filter and sort the bundles based on include, exclude, and priority lists."""
    if include_list:
        bundles = {
            key: value
            for key, value in bundles.items()
            if any(re.match(test_name, value.get("bundleName") or "") for test_name in include_list)
        }

    if exclude_list:
        bundles = {
            key: value
            for key, value in bundles.items()
            if not any(
                re.match(test_name, value.get("bundleName") or "") for test_name in exclude_list
            )
        }

    return bundles


def dedupe_download_items(items: List[Tuple[str, Dict]]) -> List[Tuple[str, Dict]]:
    result: List[Tuple[str, Dict]] = []
    seen_names: set[str] = set()
    for item in items:
        bundle_name = item[1].get("bundleName") or ""
        if bundle_name not in seen_names:
            result.append(item)
            seen_names.add(bundle_name)
    return result


MODE_BUNDLE_PREFIXES = {"assets": (), "live2d": ("live2d/",), "charts": ()}


def get_mode_bundle_prefixes(mode: str) -> tuple[str, ...]:
    try:
        return MODE_BUNDLE_PREFIXES[mode]
    except KeyError as exc:
        raise ValueError(f"Unknown updater mode: {mode}") from exc


def filter_bundles_for_mode(bundles: Dict[str, Dict], mode: str = "assets") -> Dict[str, Dict]:
    prefixes = get_mode_bundle_prefixes(mode)
    return (
        bundles
        if not prefixes
        else {
            key: value
            for key, value in bundles.items()
            if (value.get("bundleName") or "").startswith(prefixes)
        }
    )


def filter_download_items_for_mode(items: List[Tuple[str, Dict]], mode: str) -> List[Tuple[str, Dict]]:
    prefixes = get_mode_bundle_prefixes(mode)
    return (
        items
        if not prefixes
        else [item for item in items if (item[1].get("bundleName") or "").startswith(prefixes)]
    )


async def sort_download_list(
    download_list: List[Tuple[str, Dict]],
    priority_list: List[str] | None = None,
) -> List[Tuple[str, Dict]]:
    """Sort the download list alphabetically and then based on priority list."""
    download_list = sorted(
        download_list,
        key=lambda item: item[1].get("bundleName") or "",
    )

    # If a priority list is provided, sort matching groups in declaration order
    # and leave unmatched bundles at the end.  The initial name sort provides a
    # deterministic order for bundles in the same group.
    if priority_list:
        download_list = sorted(
            download_list,
            key=lambda item: next(
                (
                    index
                    for index, test_name in enumerate(priority_list)
                    if re.match(test_name, item[1].get("bundleName") or "")
                ),
                len(priority_list),
            ),
        )

    return download_list


def build_cookie_header(set_cookie_headers: List[str]) -> str:
    """Convert Set-Cookie headers to a request Cookie header."""
    cookie = SimpleCookie()
    for header in set_cookie_headers:
        cookie.load(header)

    return "; ".join(f"{key}={morsel.value}" for key, morsel in cookie.items() if morsel.value)


def get_cookie_value(cookie_header: str, cookie_name: str) -> str | None:
    prefix = f"{cookie_name}="
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(prefix):
            return part[len(prefix) :]
    return None


def get_cookie_expire_time(cookie_header: str) -> int | None:
    """Extract the CloudFront policy expiry from a Cookie header."""
    policy_value = get_cookie_value(cookie_header, "CloudFront-Policy")
    if not policy_value:
        return None

    padded_value = policy_value.rstrip("_")
    padded_value += "=" * (-len(padded_value) % 4)
    try:
        decoded_policy = base64.urlsafe_b64decode(padded_value).decode("utf-8")
        policy_json = json.loads(decoded_policy)
    except Exception:
        logger.warning("Failed to parse CloudFront-Policy cookie, forcing refresh")
        return None

    statements = policy_json.get("Statement") or []
    if not statements:
        return None

    return statements[0].get("Condition", {}).get("DateLessThan", {}).get("AWS:EpochTime")


def _derive_storage_remote_path(remote_base: str, relative_key: str) -> str:
    """Append one validated object key to an opaque configured storage target.

    ``remote_base`` is an rclone destination, not a local filesystem path.  It
    is therefore deliberately preserved byte-for-byte (apart from choosing the
    separator when it has no trailing slash), which supports named remotes,
    local absolute-path remotes, and on-the-fly remote syntax.  Only
    ``relative_key`` is parsed and validated; it must be a normalized POSIX
    path relative to the extraction root.
    """
    if not isinstance(remote_base, str):
        raise TypeError("remote_base must be a text storage target")
    if "\x00" in remote_base:
        raise ValueError("remote_base contains a NUL byte")

    remote_key = derive_remote_key(relative_key)
    separator = "" if remote_base.endswith("/") else "/"
    return f"{remote_base}{separator}{remote_key}"


async def refresh_cookie(
    config,
    headers: Dict[str, str],
    cookie: str | None = None,
) -> Tuple[Dict[str, str], str]:
    """Refresh the cookie using the GAME_COOKIE_URL."""
    if cookie:
        cookie_expire_time = get_cookie_expire_time(cookie)
        if isinstance(cookie_expire_time, int) and cookie_expire_time > int(time.time()) + 3600:
            headers["Cookie"] = cookie
            return headers, cookie

    # If the cookie is expired or not set, fetch a new one
    if config.GAME_COOKIE_URL:
        transport_error = None
        try:
            async with aiohttp.ClientSession(**get_http_session_options(config)) as session:
                async with session.post(
                    config.GAME_COOKIE_URL, headers=build_cookie_request_headers()
                ) as response:
                    if response.status == 200:
                        cookie = build_cookie_header(response.headers.getall("Set-Cookie", []))
                        assert cookie, "Cookie is empty"
                        headers["Cookie"] = cookie
                    else:
                        raise RuntimeError(
                            f"Failed to fetch cookie from {sanitize_url(config.GAME_COOKIE_URL)}"
                        )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            transport_error = RuntimeError(
                "Failed to fetch cookie from "
                f"{sanitize_url(config.GAME_COOKIE_URL)} ({type(exc).__name__})"
            )
        if transport_error is not None:
            raise transport_error
    else:
        raise ValueError("GAME_COOKIE_URL is not set in the config")

    return headers, cookie


async def deobfuscate(data: bytes) -> bytes:
    """Deobfuscate the bundle data"""
    if data[:4] == b"\x20\x00\x00\x00":
        data = data[4:]
    elif data[:4] == b"\x10\x00\x00\x00":
        data = data[4:]
        header = bytes(a ^ b for a, b in zip(data[:128], (b"\xff" * 5 + b"\x00" * 3) * 16))
        data = header + data[128:]
    return data


async def upload_to_storage(
    exported_list: List[Path],
    extracted_save_path: Path,
    remote_base: str,
    upload_program: str,
    upload_args: List[str],
    max_concurrent_uploads: int = 5,
    config=None,
):
    """Upload the extracted assets to remote storage with concurrency"""

    root_path = os.path.abspath(os.fspath(extracted_save_path))
    validated_uploads: list[tuple[object, str]] = []
    for file_path in exported_list:
        source_path = os.path.abspath(os.fspath(file_path))
        relative_path = os.path.relpath(source_path, root_path)
        relative_key = relative_path.replace(os.sep, "/")
        validated_path = validate_contained_file(root_path, relative_key)
        validated_uploads.append(
            (validated_path, _derive_storage_remote_path(remote_base, relative_key))
        )

    semaphore = asyncio.Semaphore(max_concurrent_uploads)

    async def upload_file(file_path: object, remote_path: str):
        """Upload a single file to remote storage"""
        async with semaphore:
            # Construct the upload command
            program: str = upload_program
            args: list[str] = upload_args[:]
            args[args.index("src")] = str(file_path)
            args[args.index("dst")] = remote_path
            process_timeout = _get_external_process_timeout(config)
            logger.debug(
                "Uploading %s to %s",
                file_path,
                sanitize_url(remote_path),
            )

            # Execute the command
            upload_process = await asyncio.create_subprocess_exec(program, *args)
            try:
                await _wait_for_process(
                    upload_process,
                    process_timeout,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                raise
            if upload_process.returncode != 0:
                safe_remote_path = sanitize_url(remote_path)
                logger.error("Failed to upload %s to %s", file_path, safe_remote_path)
                raise RuntimeError(f"Failed to upload {file_path} to {safe_remote_path}")
            else:
                logger.info(
                    "Successfully uploaded %s to %s",
                    file_path,
                    sanitize_url(remote_path),
                )

    # Run uploads concurrently and fail the worker if any upload fails.
    results = await asyncio.gather(
        *(upload_file(file_path, remote_path) for file_path, remote_path in validated_uploads),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, asyncio.CancelledError):
            raise result
    errors = [result for result in results if isinstance(result, Exception)]
    if errors:
        raise RuntimeError(f"{len(errors)} upload(s) failed; first error: {errors[0]}") from errors[
            0
        ]


async def upload_directory(
    source_dir: Path,
    remote_path: Path,
    upload_program: str,
    upload_args: List[str],
    config=None,
) -> None:
    """Upload a complete specialized output directory in one storage operation."""
    source_path = os.path.abspath(os.fspath(source_dir))
    if not os.path.isdir(source_path):
        raise ValueError(f"Directory upload source does not exist: {source_path}")

    args = upload_args[:]
    args[args.index("src")] = source_path
    args[args.index("dst")] = str(remote_path)
    safe_remote_path = sanitize_url(str(remote_path))
    logger.debug("Uploading directory %s to %s", source_path, safe_remote_path)

    process = await asyncio.create_subprocess_exec(upload_program, *args)
    try:
        await _wait_for_process(process, _get_external_process_timeout(config))
    except (asyncio.CancelledError, asyncio.TimeoutError):
        raise
    if process.returncode != 0:
        raise RuntimeError(
            f"Failed to upload directory {source_path} to {safe_remote_path}"
        )
    logger.info("Successfully uploaded directory %s to %s", source_path, safe_remote_path)
