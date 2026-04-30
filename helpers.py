import asyncio
import base64
import logging
import re
import time
from http.cookies import SimpleCookie
from logging.handlers import QueueHandler, QueueListener
from queue import SimpleQueue
from string import Formatter
from typing import Dict, List, Tuple

import aiohttp
import orjson as json
from anyio import Path, open_file

logger = logging.getLogger("asset_updater")

DEFAULT_REQUEST_TIMEOUT = 30 * 60
DEFAULT_DOWNLOAD_MAX_RETRIES = 3


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


def format_url_template(template: str, **values: str) -> str:
    placeholders = get_template_placeholders(template)
    missing_placeholders = [
        name for name in placeholders if name not in values or values[name] is None
    ]
    if missing_placeholders:
        missing_fields = ", ".join(sorted(missing_placeholders))
        raise ValueError(f"Missing format values for {missing_fields}: {template}")

    normalized_values = {
        name: values[name].strip() if isinstance(values[name], str) else values[name]
        for name in placeholders
    }
    return template.format(**normalized_values)


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
) -> List[Tuple[str, Dict]]:
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
    assert config.ASSET_BUNDLE_INFO_CACHE_PATH, (
        "ASSET_BUNDLE_INFO_CACHE_PATH must be set in config"
    )
    assert config.GAME_VERSION_JSON_CACHE_PATH, (
        "GAME_VERSION_JSON_CACHE_PATH must be set in config"
    )
    if (not force_full_download) and await config.ASSET_BUNDLE_INFO_CACHE_PATH.exists():
        async with await open_file(config.ASSET_BUNDLE_INFO_CACHE_PATH) as f:
            cached_asset_bundle_info = json.loads(await f.read())
    if (not force_full_download) and await config.GAME_VERSION_JSON_CACHE_PATH.exists():
        async with await open_file(config.GAME_VERSION_JSON_CACHE_PATH) as f:
            cached_game_version_json = json.loads(await f.read())

    download_list = []
    current_bundles: Dict[str, Dict] = asset_bundle_info.get("bundles", {})
    assert current_bundles, "bundles must be set in asset bundle info"
    asset_bundle_url_placeholders = get_template_placeholders(config.ASSET_BUNDLE_URL)
    current_bundles = await filter_bundles(
        current_bundles,
        include_list=include_list,
        exclude_list=exclude_list,
    )
    assert current_bundles, "No bundles found after filtering"
    if cached_asset_bundle_info and cached_game_version_json:
        if assetver:
            cached_assetver = cached_game_version_json.get("assetver", None)
            if cached_assetver != assetver:
                game_version_json["assetver"] = assetver

                cached_bundles: Dict[str, Dict] = (
                    cached_asset_bundle_info.get("bundles") or {}
                )

                changed_bundles = [
                    bundle
                    for bundle in current_bundles.values()
                    if bundle_has_changed(
                        bundle,
                        cached_bundles.get(bundle.get("bundleName", ""), {}),
                    )
                ]

                # Generate the download list from changed bundles
                app_version: str = (
                    config.APP_VERSION_OVERRIDE
                    or game_version_json.get("appVersion")
                    or ""
                )
                assert app_version, (
                    "App version must be set in game version json or config"
                )
                download_list = [
                    (
                        config.ASSET_BUNDLE_URL.format(
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
            cached_bundles: Dict[str, Dict] = (
                cached_asset_bundle_info.get("bundles") or {}
            )

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
        if assetver:
            game_version_json["assetver"] = assetver

        # Get the download list for a full download
        asset_hash: str = game_version_json.get("assetHash", "")
        app_version: str = getattr(config, "APP_VERSION_OVERRIDE", None) or game_version_json.get("appVersion") or ""
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

    # Cache the asset bundle info
    async with await open_file(config.ASSET_BUNDLE_INFO_CACHE_PATH, "wb") as f:
        await f.write(json.dumps({
            "version": asset_bundle_info.get("version", ""),
            "os": asset_bundle_info.get("os", ""),
            "bundles": current_bundles
        }, option=json.OPT_INDENT_2))

    # Cache the game version json
    async with await open_file(config.GAME_VERSION_JSON_CACHE_PATH, "wb") as f:
        await f.write(json.dumps(game_version_json, option=json.OPT_INDENT_2))

    return download_list


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
            if any(
                re.match(test_name, value.get("bundleName") or "")
                for test_name in include_list
            )
        }

    if exclude_list:
        bundles = {
            key: value
            for key, value in bundles.items()
            if not any(
                re.match(test_name, value.get("bundleName") or "")
                for test_name in exclude_list
            )
        }

    return bundles


async def sort_download_list(
    download_list: List[Tuple[str, Dict]],
    priority_list: List[str] | None = None,
) -> List[Tuple[str, Dict]]:
    """Sort the download list alphabetically and then based on priority list."""
    download_list = sorted(
        download_list,
        key=lambda item: item[1].get("bundleName") or "",
    )

    # If a priority list is provided, sort the download list based on it
    if priority_list:
        download_list = sorted(
            download_list,
            key=lambda item: [
                i
                for i, test_name in enumerate(priority_list)
                if re.match(test_name, item[1].get("bundleName") or "")
            ],
        )

    return download_list


def build_cookie_header(set_cookie_headers: List[str]) -> str:
    """Convert Set-Cookie headers to a request Cookie header."""
    cookie = SimpleCookie()
    for header in set_cookie_headers:
        cookie.load(header)

    return "; ".join(
        f"{key}={morsel.value}" for key, morsel in cookie.items() if morsel.value
    )


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

    return (
        statements[0]
        .get("Condition", {})
        .get("DateLessThan", {})
        .get("AWS:EpochTime")
    )


async def refresh_cookie(
    config,
    headers: Dict[str, str],
    cookie: str | None = None,
) -> Tuple[Dict[str, str], str]:
    """Refresh the cookie using the GAME_COOKIE_URL."""
    if cookie:
        cookie_expire_time = get_cookie_expire_time(cookie)
        if (
            isinstance(cookie_expire_time, int)
            and cookie_expire_time > int(time.time()) + 3600
        ):
            headers["Cookie"] = cookie
            return headers, cookie

    # If the cookie is expired or not set, fetch a new one
    if config.GAME_COOKIE_URL:
        async with aiohttp.ClientSession(timeout=get_request_timeout(config)) as session:
            async with session.post(
                config.GAME_COOKIE_URL, headers=headers
            ) as response:
                if response.status == 200:
                    cookie = build_cookie_header(
                        response.headers.getall("Set-Cookie", [])
                    )
                    assert cookie, "Cookie is empty"
                    headers["Cookie"] = cookie
                else:
                    raise RuntimeError(
                        f"Failed to fetch cookie from {config.GAME_COOKIE_URL}"
                    )
    else:
        raise ValueError("GAME_COOKIE_URL is not set in the config")

    return headers, cookie


async def deobfuscate(data: bytes) -> bytes:
    """Deobfuscate the bundle data"""
    if data[:4] == b"\x20\x00\x00\x00":
        data = data[4:]
    elif data[:4] == b"\x10\x00\x00\x00":
        data = data[4:]
        header = bytes(
            a ^ b for a, b in zip(data[:128], (b"\xff" * 5 + b"\x00" * 3) * 16)
        )
        data = header + data[128:]
    return data


async def upload_to_storage(
    exported_list: List[Path],
    extracted_save_path: Path,
    remote_base: str,
    upload_program: str,
    upload_args: List[str],
    max_concurrent_uploads: int = 5,
):
    """Upload the extracted assets to remote storage with concurrency"""

    semaphore = asyncio.Semaphore(max_concurrent_uploads)

    async def upload_file(file_path: Path):
        """Upload a single file to remote storage"""
        async with semaphore:
            # Construct the remote path
            remote_path = Path(remote_base) / file_path.relative_to(extracted_save_path)

            # Construct the upload command
            program: str = upload_program
            args: list[str] = upload_args[:]
            args[args.index("src")] = str(file_path)
            args[args.index("dst")] = str(remote_path)
            logger.debug(
                "Uploading %s to %s using command: %s %s",
                file_path,
                remote_path,
                program,
                " ".join(args),
            )

            # Execute the command
            upload_process = await asyncio.create_subprocess_exec(program, *args)
            await upload_process.wait()
            if upload_process.returncode != 0:
                logger.error("Failed to upload %s to %s", file_path, remote_path)
                raise RuntimeError(
                    f"Failed to upload {file_path} to {remote_path} using command: {program} {' '.join(args)}"
                )
            else:
                logger.info("Successfully uploaded %s to %s", file_path, remote_path)

    # Run uploads concurrently and fail the worker if any upload fails.
    results = await asyncio.gather(
        *(upload_file(file_path) for file_path in exported_list),
        return_exceptions=True,
    )
    errors = [result for result in results if isinstance(result, Exception)]
    if errors:
        raise RuntimeError(
            f"{len(errors)} upload(s) failed; first error: {errors[0]}"
        ) from errors[0]
