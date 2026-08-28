import asyncio
import inspect
import logging
import os
import re
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Mapping, Tuple

from anyio import Path, open_file

from updater.external_process import (
    EXTERNAL_PROCESS_TERMINATE_GRACE,
    TERMINATE_TASK_ATTRIBUTE,
    terminate_process,
    wait_for_process,
)
from updater.external_process import (
    get_external_process_timeout as _get_external_process_timeout,
)
from updater.modes import (  # noqa: F401  (re-exported until helpers.py is dissolved)
    filter_bundles_for_mode,
    filter_download_items_for_mode,
    get_mode_bundle_prefixes,
)
from updater.net.urls import format_url_template, get_template_placeholders
from updater.sanitize import (
    sanitize_url,
)
from updater.security import derive_remote_key, validate_contained_file
from updater.state import (
    StateNotFoundError,
    StateValidationError,
    load_asset_metadata,
    load_game_version,
)

logger = logging.getLogger("asset_updater")

DEFAULT_DOWNLOAD_MAX_RETRIES = 3
DEFAULT_DOWNLOAD_RETRY_BASE_DELAY = 1.0
DEFAULT_DOWNLOAD_RETRY_MAX_DELAY = 30.0
DEFAULT_MIN_FREE_DISK_BYTES = 1024 * 1024 * 1024
DEFAULT_DOWNLOAD_DISK_SPACE_CHECK_INTERVAL = 5.0


@dataclass(frozen=True)
class DownloadPlan:
    """In-memory selection result; construction performs no persistence."""

    candidates: List[Tuple[str, Dict]]
    asset_metadata: Dict
    game_version: Dict


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


async def _terminate_process(process) -> None:
    await terminate_process(process, EXTERNAL_PROCESS_TERMINATE_GRACE)


async def _wait_for_process(process, timeout: float) -> int:
    return await wait_for_process(
        process,
        timeout,
        _terminate_process,
        task_attribute=TERMINATE_TASK_ATTRIBUTE,
        logger=logger,
    )


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
        except StateValidationError:
            # Cache formats are intentionally strict. An older or malformed
            # metadata cache is not authoritative; refresh it from the already
            # fetched network response by treating it as absent.
            logger.warning(
                "Ignoring incompatible asset metadata cache: %s",
                config.ASSET_BUNDLE_INFO_CACHE_PATH,
            )
        try:
            cached_game_version_json = load_game_version(config.GAME_VERSION_JSON_CACHE_PATH)
        except StateNotFoundError:
            pass
        except StateValidationError:
            # Only tolerate validation failures while reading a prior cache.
            # Validation of the current fetched response still happens when it
            # is committed below the lifecycle layer.
            logger.warning(
                "Ignoring incompatible game version cache: %s",
                config.GAME_VERSION_JSON_CACHE_PATH,
            )

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
            # Colorful Palette servers. changed_bundles was already computed via
            # select_changed_bundles above, which performs the same checksum
            # comparison and additionally selects unchanged bundles whose
            # configured cache file is missing (matching the assetver branch).

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

    metadata_source = (
        asset_bundle_info if asset_bundle_info_for_cache is None else asset_bundle_info_for_cache
    )
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
            not include_list or any(re.match(pattern, bundle_name) for pattern in include_list)
        ) and not any(re.match(pattern, bundle_name) for pattern in (exclude_list or []))
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


async def deobfuscate(data: bytes) -> bytes:
    """Deobfuscate the bundle data"""
    if data[:4] == b"\x20\x00\x00\x00":
        data = data[4:]
    elif data[:4] == b"\x10\x00\x00\x00":
        data = data[4:]
        header = bytes(
            a ^ b for a, b in zip(data[:128], (b"\xff" * 5 + b"\x00" * 3) * 16, strict=True)
        )
        data = header + data[128:]
    return data


def _validate_upload_sources(
    exported_list: List[Path],
    extracted_save_path: Path,
) -> tuple[str, list[tuple[object, str]]]:
    """Validate every source file is contained below the extraction root.

    Returns the absolute root and ``(validated_path, relative_key)`` pairs,
    where ``relative_key`` is the normalized POSIX path below the root.
    """
    root_path = os.path.abspath(os.fspath(extracted_save_path))
    validated_sources: list[tuple[object, str]] = []
    for file_path in exported_list:
        source_path = os.path.abspath(os.fspath(file_path))
        relative_path = os.path.relpath(source_path, root_path)
        relative_key = relative_path.replace(os.sep, "/")
        validated_path = validate_contained_file(root_path, relative_key)
        validated_sources.append((validated_path, relative_key))
    return root_path, validated_sources


_OPENDAL_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024


async def upload_to_storage_opendal(
    exported_list: List[Path],
    extracted_save_path: Path,
    storage: Mapping,
    max_concurrent_uploads: int = 5,
    config=None,
):
    """Upload extracted assets through an in-process OpenDAL operator.

    ``storage`` is one ``ASSET_REMOTE_STORAGE`` entry with
    ``backend: "opendal"``: ``scheme`` names the OpenDAL service (e.g.
    ``"s3"``, ``"fs"``), ``options`` carries the service configuration
    (bucket, endpoint, credentials, root, ...), and the optional ``prefix``
    is prepended to every object key. Files stream in chunks, so large
    outputs never load fully into memory, and transient failures retry via
    OpenDAL's retry layer. No subprocess is involved.
    """
    import opendal

    scheme = storage.get("scheme")
    if not isinstance(scheme, str) or not scheme:
        raise ValueError("opendal storage requires a non-empty 'scheme'")
    options = storage.get("options") or {}
    if not isinstance(options, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in options.items()
    ):
        raise ValueError("opendal storage 'options' must map strings to strings")
    prefix = storage.get("prefix") or ""

    _, validated_sources = _validate_upload_sources(exported_list, extracted_save_path)
    validated_uploads = [
        (validated_path, derive_remote_key(relative_key, prefix))
        for validated_path, relative_key in validated_sources
    ]
    if not validated_uploads:
        return

    operator = opendal.AsyncOperator(scheme, **dict(options)).layer(
        opendal.layers.RetryLayer(max_times=3)
    )
    semaphore = asyncio.Semaphore(max(1, int(max_concurrent_uploads)))

    async def upload_file(file_path: object, remote_key: str) -> None:
        async with semaphore:
            logger.debug("Uploading %s to opendal %s:%s", file_path, scheme, remote_key)
            writer = await operator.open(remote_key, "wb")
            try:
                async with await open_file(os.fspath(file_path), "rb") as source:
                    while chunk := await source.read(_OPENDAL_UPLOAD_CHUNK_BYTES):
                        await writer.write(chunk)
            finally:
                await writer.close()
            logger.info("Successfully uploaded %s to opendal %s:%s", file_path, scheme, remote_key)

    results = await asyncio.gather(
        *(upload_file(file_path, remote_key) for file_path, remote_key in validated_uploads),
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


def _is_batchable_rclone_upload(upload_program: str, upload_args: List[str]) -> bool:
    """Whether this target can take one batched ``rclone copy`` per artifact.

    Only the documented ``["copy", "src", "dst", ...]`` template is batched:
    ``copy`` with ``--files-from-raw`` reproduces exactly the per-file
    destination keys (relative paths are preserved under the destination), so
    spawning one process replaces one process per exported file.  Any other
    verb (``copyto``, ``sync``, custom programs) keeps the per-file path.
    """
    program_name = os.path.basename(upload_program)
    return (
        program_name in ("rclone", "rclone.exe")
        and len(upload_args) >= 3
        and upload_args[0] == "copy"
        and "src" in upload_args
        and "dst" in upload_args
    )


async def _upload_batch_with_rclone(
    validated_uploads: list[tuple[object, str]],
    root_path: str,
    remote_base: str,
    upload_program: str,
    upload_args: List[str],
    max_concurrent_uploads: int,
    config,
) -> None:
    relative_keys = [relative_key for _, relative_key in validated_uploads]
    args = upload_args[:]
    args[args.index("src")] = root_path
    args[args.index("dst")] = remote_base

    list_descriptor, list_path = tempfile.mkstemp(prefix=".upload-batch-", suffix=".txt")
    try:
        with os.fdopen(list_descriptor, "w", encoding="utf-8") as list_file:
            list_file.write("\n".join(relative_keys) + "\n")
        args.extend(
            [
                "--files-from-raw",
                list_path,
                "--transfers",
                str(max(1, int(max_concurrent_uploads))),
            ]
        )
        # One process moves the whole artifact; scale the hang timeout with the
        # batch so large artifacts are not killed mid-transfer.
        batch_timeout = _get_external_process_timeout(config) * max(
            1, -(-len(relative_keys) // max(1, int(max_concurrent_uploads)))
        )
        logger.debug(
            "Uploading %d files from %s to %s in one batch",
            len(relative_keys),
            root_path,
            sanitize_url(remote_base),
        )
        upload_process = await asyncio.create_subprocess_exec(upload_program, *args)
        await _wait_for_process(upload_process, batch_timeout)
        if upload_process.returncode != 0:
            safe_remote_base = sanitize_url(remote_base)
            logger.error("Failed to batch-upload %s to %s", root_path, safe_remote_base)
            raise RuntimeError(
                f"1 upload(s) failed: batch upload of {len(relative_keys)} files "
                f"to {safe_remote_base} exited with {upload_process.returncode}"
            )
        logger.info(
            "Successfully uploaded %d files to %s",
            len(relative_keys),
            sanitize_url(remote_base),
        )
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass


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

    root_path, validated_sources = _validate_upload_sources(exported_list, extracted_save_path)
    validated_uploads: list[tuple[object, str]] = [
        (validated_path, _derive_storage_remote_path(remote_base, relative_key))
        for validated_path, relative_key in validated_sources
    ]
    validated_batch_keys: list[tuple[object, str]] = [
        (validated_path, derive_remote_key(relative_key))
        for validated_path, relative_key in validated_sources
    ]

    if validated_uploads and _is_batchable_rclone_upload(upload_program, upload_args):
        await _upload_batch_with_rclone(
            validated_batch_keys,
            root_path,
            remote_base,
            upload_program,
            upload_args,
            max_concurrent_uploads,
            config,
        )
        return

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
            await _wait_for_process(upload_process, process_timeout)
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
    await _wait_for_process(process, _get_external_process_timeout(config))
    if process.returncode != 0:
        raise RuntimeError(f"Failed to upload directory {source_path} to {safe_remote_path}")
    logger.info("Successfully uploaded directory %s to %s", source_path, safe_remote_path)
