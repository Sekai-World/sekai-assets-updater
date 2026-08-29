"""aiohttp session/timeout options and request-header builders."""

import logging

import aiohttp

logger = logging.getLogger("asset_updater")

DEFAULT_REQUEST_TIMEOUT = 30 * 60


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


def get_download_http_session_options(config=None) -> dict[str, object]:
    """Build direct-CDN session options while retaining configured timeouts."""
    return {"timeout": get_request_timeout(config)}


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
