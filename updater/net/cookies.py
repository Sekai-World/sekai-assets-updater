"""Set-Cookie parsing, CloudFront policy expiry, and cookie refresh."""

import asyncio
import base64
import logging
import time
from http.cookies import SimpleCookie
from typing import Dict, List, Tuple

import aiohttp
import orjson as json

from updater.net.http import (
    build_cookie_request_headers,
    get_http_session_options,
)
from updater.sanitize import sanitize_url

logger = logging.getLogger("asset_updater")


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
