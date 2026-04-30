import asyncio
import logging
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Union

import aiohttp
from anyio import Path

from bundle import download_deobfuscate_bundle, extract_asset_bundle
from helpers import (
    DownloadDiskSpaceGate,
    get_request_timeout,
    refresh_cookie,
    upload_to_storage,
)

logger = logging.getLogger("asset_updater")

DownloadItem = Tuple[str, Dict[str, Any]]


_QUEUE_SENTINEL = object()


@dataclass
class PipelineArtifact:
    url: str
    bundle: Dict[str, Any]
    bundle_save_path: Path
    extracted_save_path: Path | None = None
    exported_list: List[Path] | None = None
    tmp_bundle_save_file: Any = None
    tmp_extracted_save_dir: tempfile.TemporaryDirectory | None = None
    remove_bundle_after_extract: bool = False
    remove_extracted_after_upload: bool = False


def _sanitize_concurrency(value, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, default)


def get_download_stage_concurrency(config) -> int:
    return _sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_DOWNLOADS",
            getattr(config, "MAX_CONCURRENCY", 1),
        )
    )


def get_extract_stage_concurrency(config) -> int:
    return _sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_EXTRACTS",
            getattr(config, "MAX_CONCURRENCY", 1),
        )
    )


def get_upload_stage_concurrency(config) -> int:
    return _sanitize_concurrency(
        getattr(config, "MAX_CONCURRENCY_UPLOAD_STAGE", 1)
    )


def get_stage_queue_size(config, downstream_concurrency: int) -> int:
    return _sanitize_concurrency(
        getattr(
            config,
            "PIPELINE_STAGE_QUEUE_SIZE",
            downstream_concurrency,
        ),
        default=downstream_concurrency,
    )


def _get_bundle_file_size(bundle: Dict[str, Any]) -> int:
    value = bundle.get("fileSize", 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


async def _cleanup_artifact(
    artifact: PipelineArtifact,
    *,
    remove_bundle: bool = False,
    remove_extracted: bool = False,
) -> None:
    if remove_bundle and artifact.remove_bundle_after_extract:
        try:
            if artifact.tmp_bundle_save_file:
                artifact.tmp_bundle_save_file.close()
                artifact.tmp_bundle_save_file = None
            else:
                await artifact.bundle_save_path.unlink(missing_ok=True)
            logger.debug("Removed temporary bundle %s", artifact.bundle_save_path)
        except OSError:
            logger.exception(
                "Failed to remove temporary bundle %s",
                artifact.bundle_save_path,
            )
        finally:
            artifact.remove_bundle_after_extract = False
    elif artifact.tmp_bundle_save_file:
        artifact.tmp_bundle_save_file.close()
        artifact.tmp_bundle_save_file = None

    if (
        remove_extracted
        and artifact.tmp_extracted_save_dir
        and artifact.remove_extracted_after_upload
    ):
        try:
            artifact.tmp_extracted_save_dir.cleanup()
            logger.debug(
                "Removed temporary extracted dir %s",
                artifact.extracted_save_path,
            )
        except OSError:
            logger.exception(
                "Failed to remove temporary extracted dir %s",
                artifact.extracted_save_path,
            )
        finally:
            artifact.tmp_extracted_save_dir = None


async def _put_sentinels(queue: asyncio.Queue, count: int) -> None:
    for _ in range(count):
        await queue.put(_QUEUE_SENTINEL)


async def _download_stage(
    name: str,
    input_queue: asyncio.Queue,
    extract_queue: asyncio.Queue,
    config,
    headers: Dict[str, str],
    cookie: str | None,
    failed_tasks: List[DownloadItem],
    failed_lock: asyncio.Lock,
    download_disk_space_gate: DownloadDiskSpaceGate | None,
    session: aiohttp.ClientSession,
) -> None:
    worker_headers = headers.copy()
    worker_cookie = cookie

    while True:
        item = await input_queue.get()
        try:
            if item is _QUEUE_SENTINEL:
                return

            url, bundle = item
            label = bundle.get("bundleName", url)
            logger.debug("%s downloading %s", name, label)

            required_download_bytes = _get_bundle_file_size(bundle)
            bundle_save_path: Union[Path, None] = None
            tmp_bundle_save_file = None
            remove_bundle_after_extract = False

            try:
                if worker_cookie:
                    worker_headers, worker_cookie = await refresh_cookie(
                        config,
                        worker_headers,
                        worker_cookie,
                    )

                if isinstance(config.ASSET_LOCAL_BUNDLE_CACHE_DIR, Path):
                    bundle_save_path = (
                        config.ASSET_LOCAL_BUNDLE_CACHE_DIR / bundle["bundleName"]
                    )
                    await bundle_save_path.parent.mkdir(parents=True, exist_ok=True)
                else:
                    tmp_bundle_save_file = tempfile.NamedTemporaryFile()
                    bundle_save_path = Path(tmp_bundle_save_file.name)
                    remove_bundle_after_extract = True

                if download_disk_space_gate is not None:
                    async with download_disk_space_gate.reserve(
                        required_download_bytes,
                        label,
                    ):
                        await download_deobfuscate_bundle(
                            url,
                            bundle_save_path,
                            headers=worker_headers,
                            config=config,
                            session=session,
                        )
                else:
                    await download_deobfuscate_bundle(
                        url,
                        bundle_save_path,
                        headers=worker_headers,
                        config=config,
                        session=session,
                    )

                await extract_queue.put(
                    PipelineArtifact(
                        url=url,
                        bundle=bundle,
                        bundle_save_path=bundle_save_path,
                        tmp_bundle_save_file=tmp_bundle_save_file,
                        remove_bundle_after_extract=remove_bundle_after_extract,
                    )
                )
            except Exception:
                if tmp_bundle_save_file:
                    tmp_bundle_save_file.close()
                if bundle_save_path and remove_bundle_after_extract:
                    await bundle_save_path.unlink(missing_ok=True)
                logger.exception("Failed to download %s", label)
                async with failed_lock:
                    failed_tasks.append((url, bundle))
        finally:
            input_queue.task_done()


async def _extract_stage(
    name: str,
    extract_queue: asyncio.Queue,
    upload_queue: asyncio.Queue,
    config,
    failed_tasks: List[DownloadItem],
    failed_lock: asyncio.Lock,
) -> None:
    while True:
        item = await extract_queue.get()
        try:
            if item is _QUEUE_SENTINEL:
                return

            artifact = item
            label = artifact.bundle.get("bundleName", artifact.url)
            logger.debug("%s extracting %s", name, label)

            try:
                if isinstance(config.ASSET_LOCAL_EXTRACTED_DIR, Path):
                    extracted_save_path = config.ASSET_LOCAL_EXTRACTED_DIR
                    await extracted_save_path.parent.mkdir(parents=True, exist_ok=True)
                    remove_extracted_after_upload = False
                else:
                    tmp_extracted_save_dir = tempfile.TemporaryDirectory(delete=False)
                    extracted_save_path = Path(tmp_extracted_save_dir.name)
                    artifact.tmp_extracted_save_dir = tmp_extracted_save_dir
                    remove_extracted_after_upload = True

                artifact.extracted_save_path = extracted_save_path
                artifact.remove_extracted_after_upload = remove_extracted_after_upload
                artifact.exported_list = await extract_asset_bundle(
                    artifact.bundle_save_path,
                    artifact.bundle,
                    extracted_save_path,
                    unity_version=config.UNITY_VERSION,
                    config=config,
                )
                logger.debug("Extracted %s to %s", label, artifact.exported_list)

                await _cleanup_artifact(artifact, remove_bundle=True)
                await upload_queue.put(artifact)
            except Exception:
                logger.exception("Failed to extract %s", label)
                async with failed_lock:
                    failed_tasks.append((artifact.url, artifact.bundle))
                await _cleanup_artifact(
                    artifact,
                    remove_bundle=True,
                    remove_extracted=True,
                )
        finally:
            extract_queue.task_done()


async def _upload_stage(
    name: str,
    upload_queue: asyncio.Queue,
    config,
    failed_tasks: List[DownloadItem],
    failed_lock: asyncio.Lock,
) -> None:
    while True:
        item = await upload_queue.get()
        try:
            if item is _QUEUE_SENTINEL:
                return

            artifact = item
            label = artifact.bundle.get("bundleName", artifact.url)
            logger.debug("%s uploading %s", name, label)

            try:
                if config.ASSET_REMOTE_STORAGE:
                    if artifact.extracted_save_path is None:
                        raise ValueError(f"Extracted path is not set for {label}")
                    exported_list = artifact.exported_list or []
                    for storage in config.ASSET_REMOTE_STORAGE:
                        if storage["type"] == "normal":
                            await upload_to_storage(
                                exported_list,
                                artifact.extracted_save_path,
                                storage["base"],
                                storage["program"],
                                storage["args"],
                                max_concurrent_uploads=config.MAX_CONCURRENCY_UPLOADS,
                            )
                logger.debug("Finished processing %s", label)
            except Exception:
                logger.exception("Failed to upload %s", label)
                async with failed_lock:
                    failed_tasks.append((artifact.url, artifact.bundle))
            finally:
                await _cleanup_artifact(
                    artifact,
                    remove_bundle=True,
                    remove_extracted=True,
                )
        finally:
            upload_queue.task_done()


async def run_pipeline(
    dl_list: List[DownloadItem],
    config,
    headers: Dict[str, str],
    cookie: str | None = None,
    download_disk_space_gate: DownloadDiskSpaceGate | None = None,
) -> List[DownloadItem]:
    download_concurrency = get_download_stage_concurrency(config)
    extract_concurrency = get_extract_stage_concurrency(config)
    upload_concurrency = get_upload_stage_concurrency(config)
    extract_queue_size = get_stage_queue_size(config, extract_concurrency)
    upload_queue_size = get_stage_queue_size(config, upload_concurrency)

    download_queue: asyncio.Queue = asyncio.Queue()
    extract_queue: asyncio.Queue = asyncio.Queue(maxsize=extract_queue_size)
    upload_queue: asyncio.Queue = asyncio.Queue(maxsize=upload_queue_size)
    failed_tasks: List[DownloadItem] = []
    failed_lock = asyncio.Lock()

    for item in dl_list:
        await download_queue.put(item)
    await _put_sentinels(download_queue, download_concurrency)

    logger.info(
        "Pipeline concurrency: downloads=%d extracts=%d uploads=%d extract_queue=%d upload_queue=%d",
        download_concurrency,
        extract_concurrency,
        upload_concurrency,
        extract_queue_size,
        upload_queue_size,
    )

    async with aiohttp.ClientSession(timeout=get_request_timeout(config)) as session:
        download_tasks = [
            asyncio.create_task(
                _download_stage(
                    f"download_worker-{worker_id}",
                    download_queue,
                    extract_queue,
                    config,
                    headers,
                    cookie,
                    failed_tasks,
                    failed_lock,
                    download_disk_space_gate,
                    session,
                )
            )
            for worker_id in range(download_concurrency)
        ]
        extract_tasks = [
            asyncio.create_task(
                _extract_stage(
                    f"extract_worker-{worker_id}",
                    extract_queue,
                    upload_queue,
                    config,
                    failed_tasks,
                    failed_lock,
                )
            )
            for worker_id in range(extract_concurrency)
        ]
        upload_tasks = [
            asyncio.create_task(
                _upload_stage(
                    f"upload_worker-{worker_id}",
                    upload_queue,
                    config,
                    failed_tasks,
                    failed_lock,
                )
            )
            for worker_id in range(upload_concurrency)
        ]

        await download_queue.join()
        await _put_sentinels(extract_queue, extract_concurrency)
        await extract_queue.join()
        await _put_sentinels(upload_queue, upload_concurrency)
        await upload_queue.join()

        await asyncio.gather(
            *download_tasks,
            *extract_tasks,
            *upload_tasks,
            return_exceptions=False,
        )

    return failed_tasks


async def worker(
    name: str,
    dl_info: DownloadItem,
    config,
    headers: Dict[str, str],
    cookie: str = None,
    download_disk_space_gate: DownloadDiskSpaceGate | None = None,
) -> None:
    failed_tasks = await run_pipeline(
        [dl_info],
        config,
        headers,
        cookie=cookie,
        download_disk_space_gate=download_disk_space_gate,
    )
    if failed_tasks:
        _, bundle = dl_info
        raise RuntimeError(
            f"{name} failed processing {bundle.get('bundleName', dl_info[0])}"
        )
