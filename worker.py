import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Union

import aiohttp
from anyio import Path

from bundle import (
    download_deobfuscate_bundle,
    extract_asset_bundle,
    is_chart_score_bundle,
    is_live2d_bundle,
)
from helpers import (
    DownloadDiskSpaceGate,
    build_cdn_headers,
    get_download_http_session_options,
    sanitize_log_label,
    upload_to_storage,
    upload_to_storage_opendal,
)
from security import prepare_secure_directory, resolve_secure_path, validate_contained_file
from specialized import get_enabled_specialized_modes

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


def get_bundle_cache_root(config, bundle: Dict[str, Any]):
    """Select the cache root without allowing specialized roots to leak."""
    if is_live2d_bundle(bundle):
        return getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None)
    return getattr(config, "ASSET_LOCAL_BUNDLE_CACHE_DIR", None)


def get_bundle_cache_path(config, bundle: Dict[str, Any]):
    root = get_bundle_cache_root(config, bundle)
    bundle_name = bundle.get("bundleName") or ""
    if root is None or not bundle_name:
        return None
    return root / bundle_name


def _uses_aggregate_workspace(bundle: Dict[str, Any], config) -> bool:
    """Route only specialized bundle outputs into the run workspace."""
    enabled_modes = get_enabled_specialized_modes(getattr(config, "UPDATER_MODE", "assets"), config)
    return ("live2d" in enabled_modes and is_live2d_bundle(bundle)) or (
        "charts" in enabled_modes and is_chart_score_bundle(bundle)
    )


def _configured_path(value) -> Path | None:
    """Normalize configured filesystem paths from either pathlib or anyio."""
    if isinstance(value, (str, os.PathLike)):
        return Path(os.fspath(value))
    return None


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
    return _sanitize_concurrency(getattr(config, "MAX_CONCURRENCY_UPLOAD_STAGE", 1))


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
    """Return an optional manifest size only for disk-space reservation."""
    for field in ("fileSize", "size"):
        value = bundle.get(field)
        if type(value) is int and value >= 0:
            return value
    return 0


def _bundle_staging_identity(bundle_name: Any) -> str:
    """Return a deterministic, filesystem-safe identity for a bundle name."""

    if not isinstance(bundle_name, str) or not bundle_name:
        raise ValueError("bundleName must be a non-empty string")
    digest = hashlib.sha256(bundle_name.encode("utf-8", "surrogatepass")).hexdigest()
    return f"bundle-{digest[:24]}"


def _validate_artifact_outputs(extracted_root: Path, exported_paths: List[Path]) -> List[Path]:
    """Ensure extraction output is contained regular files for this artifact."""

    root = extracted_root
    root_std = __import__("pathlib").Path(root.as_posix()).resolve()
    validated: List[Path] = []
    for path in exported_paths:
        candidate = __import__("pathlib").Path(path.as_posix())
        relative_path = candidate.resolve().relative_to(root_std).as_posix()
        validated.append(Path(validate_contained_file(root_std, relative_path).as_posix()))
    return validated


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
            logger.error(
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
            logger.error(
                "Failed to remove temporary extracted dir %s",
                artifact.extracted_save_path,
            )
        finally:
            artifact.tmp_extracted_save_dir = None


async def _cleanup_queued_artifacts(queue: asyncio.Queue) -> None:
    """Remove durable temporary artifacts that cannot survive a cancelled run."""
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            if isinstance(item, PipelineArtifact):
                await _cleanup_artifact(item, remove_bundle=True, remove_extracted=True)
        finally:
            queue.task_done()


async def _put_sentinels(queue: asyncio.Queue, count: int) -> None:
    for _ in range(count):
        await queue.put(_QUEUE_SENTINEL)


async def _monitor_worker_failures(worker_tasks: List[asyncio.Task]) -> None:
    """Wait for pipeline workers and re-raise an unexpected worker failure."""

    pending = set(worker_tasks)
    while pending:
        done, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task.cancelled():
                raise asyncio.CancelledError
            exception = task.exception()
            if exception is not None:
                raise exception


async def _await_with_worker_monitor(
    awaitable,
    worker_monitor: asyncio.Task,
) -> None:
    """Await a pipeline operation without hiding a failed worker behind it."""

    operation = asyncio.create_task(awaitable)
    try:
        done, _ = await asyncio.wait(
            {operation, worker_monitor},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker_monitor in done:
            await worker_monitor
        await operation
    except BaseException:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        raise


async def _download_stage(
    pipeline_id: str,
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
    while True:
        item = await input_queue.get()
        try:
            if item is _QUEUE_SENTINEL:
                return

            url, bundle = item
            label = sanitize_log_label(bundle.get("bundleName", url))
            logger.debug(
                "PIPELINE | id=%s | worker=%s | stage=download | action=start_item | item=%s",
                pipeline_id,
                name,
                label,
            )

            required_download_bytes = _get_bundle_file_size(bundle)
            bundle_save_path: Union[Path, None] = None
            tmp_bundle_save_file = None
            remove_bundle_after_extract = False
            download_root: Path
            download_relative_path: str

            try:
                bundle_cache_root = _configured_path(get_bundle_cache_root(config, bundle))
                if bundle_cache_root is not None:
                    bundle_cache_root = Path(prepare_secure_directory(bundle_cache_root).as_posix())
                    bundle_save_path = Path(
                        resolve_secure_path(
                            bundle_cache_root,
                            bundle["bundleName"],
                        ).as_posix()
                    )
                    await bundle_save_path.parent.mkdir(parents=True, exist_ok=True)
                    download_root = bundle_cache_root
                    download_relative_path = bundle_save_path.relative_to(
                        bundle_cache_root
                    ).as_posix()
                else:
                    tmp_bundle_save_file = tempfile.NamedTemporaryFile(delete=False)
                    bundle_save_path = Path(tmp_bundle_save_file.name)
                    tmp_bundle_save_file.close()
                    tmp_bundle_save_file = None
                    remove_bundle_after_extract = True
                    download_root = bundle_save_path.parent
                    download_relative_path = bundle_save_path.name

                if download_disk_space_gate is not None:
                    async with download_disk_space_gate.reserve(
                        required_download_bytes,
                        label,
                    ):
                        await download_deobfuscate_bundle(
                            url,
                            download_root,
                            download_relative_path,
                            headers=build_cdn_headers(cookie),
                            config=config,
                            session=session,
                        )
                else:
                    await download_deobfuscate_bundle(
                        url,
                        download_root,
                        download_relative_path,
                        headers=build_cdn_headers(cookie),
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
            except asyncio.CancelledError:
                if tmp_bundle_save_file:
                    tmp_bundle_save_file.close()
                if bundle_save_path and remove_bundle_after_extract:
                    await bundle_save_path.unlink(missing_ok=True)
                raise
            except Exception:
                if tmp_bundle_save_file:
                    tmp_bundle_save_file.close()
                if bundle_save_path and remove_bundle_after_extract:
                    await bundle_save_path.unlink(missing_ok=True)
                logger.error(
                    "ERROR | pipeline_id=%s | worker=%s | stage=download | item=%s",
                    pipeline_id,
                    name,
                    label,
                )
                async with failed_lock:
                    failed_tasks.append((url, bundle))
        finally:
            input_queue.task_done()


async def _extract_stage(
    pipeline_id: str,
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
            label = sanitize_log_label(artifact.bundle.get("bundleName", artifact.url))
            logger.debug(
                "PIPELINE | id=%s | worker=%s | stage=extract | action=start_item | item=%s",
                pipeline_id,
                name,
                label,
            )

            handed_to_upload = False
            try:
                bundle_cache_root = None
                configured_bundle_cache_root = _configured_path(
                    get_bundle_cache_root(config, artifact.bundle)
                )
                if configured_bundle_cache_root is not None:
                    bundle_cache_root = Path(
                        prepare_secure_directory(configured_bundle_cache_root).as_posix()
                    )

                configured_extracted_root = _configured_path(config.ASSET_LOCAL_EXTRACTED_DIR)
                if configured_extracted_root is not None:
                    configured_root = Path(
                        prepare_secure_directory(configured_extracted_root).as_posix()
                    )
                    if _uses_aggregate_workspace(artifact.bundle, config):
                        # Motion restoration consumes the aggregate live2d/model
                        # tree after the pipeline completes. Keep those outputs in
                        # the run workspace rather than per-bundle staging roots.
                        extracted_save_path = configured_root
                    else:
                        identity_root = configured_root / _bundle_staging_identity(
                            artifact.bundle.get("bundleName")
                        )
                        extracted_save_path = Path(
                            prepare_secure_directory(identity_root / uuid.uuid4().hex).as_posix()
                        )
                    remove_extracted_after_upload = False
                else:
                    tmp_extracted_save_dir = tempfile.TemporaryDirectory(delete=False)
                    extracted_save_path = Path(tmp_extracted_save_dir.name)
                    artifact.tmp_extracted_save_dir = tmp_extracted_save_dir
                    remove_extracted_after_upload = True

                artifact.extracted_save_path = extracted_save_path
                artifact.remove_extracted_after_upload = remove_extracted_after_upload
                extracted_outputs = await extract_asset_bundle(
                    artifact.bundle_save_path,
                    artifact.bundle,
                    extracted_save_path,
                    unity_version=config.UNITY_VERSION,
                    config=config,
                    bundle_cache_root=bundle_cache_root,
                )
                artifact.exported_list = _validate_artifact_outputs(
                    extracted_save_path,
                    extracted_outputs,
                )
                logger.debug(
                    "PIPELINE | id=%s | worker=%s | stage=extract | action=done_item | item=%s | outputs=%s",
                    pipeline_id,
                    name,
                    label,
                    artifact.exported_list,
                )

                await _cleanup_artifact(artifact, remove_bundle=True)
                await upload_queue.put(artifact)
                handed_to_upload = True
            except asyncio.CancelledError:
                if not handed_to_upload:
                    await _cleanup_artifact(
                        artifact,
                        remove_bundle=True,
                        remove_extracted=True,
                    )
                raise
            except Exception:
                logger.error(
                    "ERROR | pipeline_id=%s | worker=%s | stage=extract | item=%s",
                    pipeline_id,
                    name,
                    label,
                )
                upload_succeeded = True
                async with failed_lock:
                    failed_tasks.append((artifact.url, artifact.bundle))
                await _cleanup_artifact(
                    artifact,
                    remove_bundle=True,
                    remove_extracted=upload_succeeded,
                )
        finally:
            extract_queue.task_done()


async def recover_live2d_model_outputs(config, bundles: Dict[str, Dict[str, Any]]) -> None:
    """Transactionally rebuild the aggregate Live2D model tree from raw cache.

    Motion bundles are intentionally not extracted by forced Live2D runs.  Their
    raw-cache directory is therefore the source consumed by motion restoration;
    model bundles, on the other hand, must be extracted into the aggregate
    workspace using their current manifest metadata.
    """
    cache_root = _configured_path(getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None))
    extracted_root = _configured_path(getattr(config, "ASSET_LOCAL_EXTRACTED_DIR", None))
    if cache_root is None or extracted_root is None:
        raise RuntimeError(
            "Live2D recovery unavailable: bundle cache or extracted workspace is not configured"
        )

    cache_root = Path(prepare_secure_directory(cache_root).as_posix())
    motion_source = cache_root / "live2d" / "motion"
    if not await motion_source.is_dir():
        raise RuntimeError(
            f"Live2D recovery unavailable: cached motion source is missing: {motion_source}"
        )

    model_bundles = [
        bundle
        for bundle in bundles.values()
        if isinstance(bundle, dict) and (bundle.get("bundleName") or "").startswith("live2d/model/")
    ]
    if not model_bundles:
        raise RuntimeError(
            "Live2D recovery unavailable: current metadata contains no Live2D model bundles"
        )

    # Validate the whole current manifest cache before touching output. A
    # missing later bundle must not leave a partial aggregate model tree.
    cached_model_bundles = []
    for bundle in model_bundles:
        bundle_path = get_bundle_cache_path(config, bundle)
        if bundle_path is None or not await Path(str(bundle_path)).is_file():
            raise RuntimeError(
                "Live2D recovery unavailable: cached model bundle file is missing: "
                f"{bundle.get('bundleName', '<unknown>')}"
            )
        cached_model_bundles.append((bundle, Path(str(bundle_path))))

    extracted_root_std = __import__("pathlib").Path(extracted_root.as_posix())
    staging_root_std = __import__("pathlib").Path(
        tempfile.mkdtemp(prefix=".sekai-live2d-recovery-", dir=extracted_root_std.parent)
    )
    staging_root = Path(staging_root_std.as_posix())
    try:
        for bundle, bundle_path in cached_model_bundles:
            try:
                exported = await extract_asset_bundle(
                    bundle_path,
                    bundle,
                    staging_root,
                    unity_version=config.UNITY_VERSION,
                    config=config,
                    bundle_cache_root=cache_root,
                )
            except Exception as exc:
                raise RuntimeError(
                    "Live2D recovery failed extracting cached model bundle "
                    f"{bundle.get('bundleName', '<unknown>')}: {exc}"
                ) from exc
            if not exported:
                raise RuntimeError(
                    "Live2D recovery failed: cached model bundle produced no outputs: "
                    f"{bundle.get('bundleName', '<unknown>')}"
                )

        staged_model_std = staging_root_std / "live2d" / "model"
        if not staged_model_std.is_dir() or not any(staged_model_std.rglob("*.model3.json")):
            raise RuntimeError(
                "Live2D recovery failed: cached model bundles produced no model files"
            )

        target_model_std = extracted_root_std / "live2d" / "model"
        target_model_std.parent.mkdir(parents=True, exist_ok=True)
        backup_model_std = target_model_std.with_name(f".model-backup-{uuid.uuid4().hex}")
        had_existing_target = target_model_std.exists()
        try:
            if had_existing_target:
                os.replace(target_model_std, backup_model_std)
            os.replace(staged_model_std, target_model_std)
        except Exception as exc:
            if had_existing_target and backup_model_std.exists() and not target_model_std.exists():
                os.replace(backup_model_std, target_model_std)
            raise RuntimeError(f"Live2D recovery failed promoting model outputs: {exc}") from exc
        finally:
            if backup_model_std.exists():
                shutil.rmtree(backup_model_std)
    finally:
        shutil.rmtree(staging_root_std, ignore_errors=True)


async def _upload_stage(
    pipeline_id: str,
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
            label = sanitize_log_label(artifact.bundle.get("bundleName", artifact.url))
            logger.debug(
                "PIPELINE | id=%s | worker=%s | stage=upload | action=start_item | item=%s",
                pipeline_id,
                name,
                label,
            )

            try:
                if config.ASSET_REMOTE_STORAGE:
                    if artifact.extracted_save_path is None:
                        raise ValueError(f"Extracted path is not set for {label}")
                    exported_list = artifact.exported_list or []
                    for storage in config.ASSET_REMOTE_STORAGE:
                        if storage["type"] != "normal":
                            continue
                        if storage.get("backend") == "opendal":
                            await upload_to_storage_opendal(
                                exported_list,
                                artifact.extracted_save_path,
                                storage,
                                max_concurrent_uploads=config.MAX_CONCURRENCY_UPLOADS,
                                config=config,
                            )
                        else:
                            await upload_to_storage(
                                exported_list,
                                artifact.extracted_save_path,
                                storage["base"],
                                storage["program"],
                                storage["args"],
                                max_concurrent_uploads=config.MAX_CONCURRENCY_UPLOADS,
                                config=config,
                            )
                logger.debug(
                    "PIPELINE | id=%s | worker=%s | stage=upload | action=done_item | item=%s",
                    pipeline_id,
                    name,
                    label,
                )
            except asyncio.CancelledError:
                await _cleanup_artifact(artifact, remove_bundle=True, remove_extracted=True)
                raise
            except Exception:
                logger.error(
                    "ERROR | pipeline_id=%s | worker=%s | stage=upload | item=%s",
                    pipeline_id,
                    name,
                    label,
                )
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
    start_time = asyncio.get_running_loop().time()
    pipeline_id = uuid.uuid4().hex[:8]
    total_items = len(dl_list)
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
        "PIPELINE | status=start | id=%s | items=%d | downloads=%d | extracts=%d | uploads=%d",
        pipeline_id,
        total_items,
        download_concurrency,
        extract_concurrency,
        upload_concurrency,
    )
    logger.debug(
        "PIPELINE | id=%s | queue_sizes | extract_queue=%d | upload_queue=%d",
        pipeline_id,
        extract_queue_size,
        upload_queue_size,
    )

    async with aiohttp.ClientSession(**get_download_http_session_options(config)) as session:
        download_tasks = [
            asyncio.create_task(
                _download_stage(
                    pipeline_id,
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
                    pipeline_id,
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
                    pipeline_id,
                    f"upload_worker-{worker_id}",
                    upload_queue,
                    config,
                    failed_tasks,
                    failed_lock,
                )
            )
            for worker_id in range(upload_concurrency)
        ]

        all_tasks = download_tasks + extract_tasks + upload_tasks
        worker_monitor = asyncio.create_task(_monitor_worker_failures(all_tasks))
        try:
            await _await_with_worker_monitor(download_queue.join(), worker_monitor)
            logger.info("PIPELINE | id=%s | stage=download | status=completed", pipeline_id)
            await _await_with_worker_monitor(
                _put_sentinels(extract_queue, extract_concurrency),
                worker_monitor,
            )
            await _await_with_worker_monitor(extract_queue.join(), worker_monitor)
            logger.info("PIPELINE | id=%s | stage=extract | status=completed", pipeline_id)
            await _await_with_worker_monitor(
                _put_sentinels(upload_queue, upload_concurrency),
                worker_monitor,
            )
            await _await_with_worker_monitor(upload_queue.join(), worker_monitor)
            logger.info("PIPELINE | id=%s | stage=upload | status=completed", pipeline_id)

            await asyncio.gather(*all_tasks, worker_monitor, return_exceptions=False)
        except BaseException:
            for task in all_tasks:
                task.cancel()
            worker_monitor.cancel()
            await asyncio.gather(*all_tasks, worker_monitor, return_exceptions=True)
            await _cleanup_queued_artifacts(extract_queue)
            await _cleanup_queued_artifacts(upload_queue)
            raise

    succeeded = total_items - len(failed_tasks)
    logger.info(
        "PIPELINE | status=completed | id=%s | succeeded=%d | failed=%d | total=%d | duration_sec=%.2f",
        pipeline_id,
        succeeded,
        len(failed_tasks),
        total_items,
        asyncio.get_running_loop().time() - start_time,
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
            f"{sanitize_log_label(name)} failed processing "
            f"{sanitize_log_label(bundle.get('bundleName', dl_info[0]))}"
        )
