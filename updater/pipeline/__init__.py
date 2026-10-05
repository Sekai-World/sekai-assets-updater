"""The asynchronous download -> extract -> upload stage pipeline.

Bundles flow through three bounded stages (fetch -> plan happens in
updater.net, post-processing in updater.postprocess): the download stage
streams and deobfuscates bundles, the extract stage fans out to the
process pool and media jobs, and the upload stage pushes artifacts to the
configured storage backends. This is the staged engine of the
updater/pipeline/ package; the adaptive extraction-worker roadmap Phase 2
scheduler lives in updater/pipeline/scheduler.py.

Note: never import refresh_cookie into this namespace — historic tests
patched a nonexistent "worker.refresh_cookie" as a no-op, and a real
binding here would silently activate similarly-shaped patches.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path as StdPath
from typing import Any, Dict, List

import aiohttp
from anyio import Path

from updater.extract.bundle import extract_asset_bundle
from updater.net.disk_space import DownloadDiskSpaceGate
from updater.net.download import download_deobfuscate_bundle
from updater.net.http import build_cdn_headers, get_download_http_session_options
from updater.net.plan import DownloadItem
from updater.pipeline.scheduler import ExtractionScheduler
from updater.sanitize import sanitize_log_label
from updater.security import prepare_secure_directory, resolve_secure_path, validate_contained_file
from updater.storage.opendal import upload_to_storage_opendal
from updater.storage.rclone import upload_to_storage
from updater.workspace import (
    bundle_staging_identity as _bundle_staging_identity,
)
from updater.workspace import (
    configured_path as _configured_path,
)
from updater.workspace import (
    get_bundle_cache_root,
)
from updater.workspace import (
    uses_aggregate_workspace as _uses_aggregate_workspace,
)

logger = logging.getLogger("asset_updater")


_QUEUE_SENTINEL = object()

# Extraction-worker roadmap Phase 0: queue-depth sampling cadence and the
# versioned extraction profile record format (docs/EXTRACTION_PROFILING.md).
_QUEUE_DEPTH_SAMPLE_INTERVAL_SEC = 1.0
_EXTRACTION_PROFILE_VERSION = 1


def _reserve_temporary_bundle_path() -> str:
    """Create and close an empty temp file, reserving a unique download path."""
    descriptor, name = tempfile.mkstemp()
    os.close(descriptor)
    return name


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


def get_extract_scheduler_mode(config) -> str:
    """Resolve the extraction scheduling mode (extraction-worker roadmap Phase 2)."""

    mode = getattr(config, "EXTRACT_SCHEDULER_MODE", "fixed")
    if mode not in ("fixed", "adaptive"):
        raise ValueError(f"Unsupported extract scheduler mode: {mode!r}")
    return mode


def get_adaptive_extract_worker_count(config) -> int:
    """Adaptive extract workers never exceed EXTRACT_MAX_WORKERS or the fixed width."""

    extract_concurrency = get_extract_stage_concurrency(config)
    configured = getattr(config, "EXTRACT_MAX_WORKERS", None)
    if configured is None:
        return extract_concurrency
    return min(extract_concurrency, _sanitize_concurrency(configured, extract_concurrency))


def get_adaptive_media_slots(config, worker_count: int) -> int:
    """Concurrent media-classified extraction slots; at least one light slot is kept."""

    configured = getattr(config, "EXTRACT_ADAPTIVE_MEDIA_SLOTS", None)
    if configured is None:
        return max(1, worker_count // 2)
    return min(worker_count, max(1, _sanitize_concurrency(configured, worker_count)))


def get_media_bundle_hints(config) -> tuple[re.Pattern[str], ...]:
    """Compile the advisory media bundle-name hints; invalid patterns fail fast."""

    patterns = getattr(config, "EXTRACT_MEDIA_BUNDLE_HINTS", None) or ()
    return tuple(re.compile(pattern) for pattern in patterns)


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


def _resolve_extraction_profile_path(config, pipeline_id: str) -> StdPath | None:
    """Return the JSON-lines extraction profile path, or None when disabled."""

    if not getattr(config, "EXTRACTION_PROFILING", False):
        return None
    explicit = getattr(config, "EXTRACTION_PROFILE_PATH", None)
    if explicit:
        return StdPath(os.fspath(explicit))
    configured_root = _configured_path(getattr(config, "ASSET_LOCAL_EXTRACTED_DIR", None))
    file_name = f"extraction-profile-{pipeline_id}.jsonl"
    if configured_root is not None:
        return StdPath(configured_root.as_posix()) / file_name
    return StdPath(file_name)


class ExtractionProfiler:
    """Aggregate extraction metrics for one pipeline run.

    Aggregation and the per-bundle DEBUG timing logs are always active; setting
    ``EXTRACTION_PROFILING=True`` additionally appends one JSON record per
    bundle plus a run summary record to the profile file. Profile writing is
    advisory: any IO failure degrades to a warning and never affects extraction
    correctness (extraction-worker roadmap Phase 0, docs/EXTRACTION_PROFILING.md).
    """

    def __init__(self, pipeline_id: str, profile_path: StdPath | None) -> None:
        self.pipeline_id = pipeline_id
        self.profile_path = profile_path
        self.bundle_count = 0
        self.failed_count = 0
        self.total_extraction_sec = 0.0
        self.total_idle_sec = 0.0
        self.output_count = 0
        self.output_bytes = 0
        self.queue_depth_samples: List[int] = []
        self._profile_file = None
        self._profile_broken = False
        self._finished = False

    def record_bundle(
        self,
        label: str,
        duration_sec: float,
        output_count: int,
        output_bytes: int,
    ) -> None:
        self.bundle_count += 1
        self.total_extraction_sec += duration_sec
        self.output_count += output_count
        self.output_bytes += output_bytes
        self._write_record(
            {
                "record": "bundle",
                "version": _EXTRACTION_PROFILE_VERSION,
                "pipeline_id": self.pipeline_id,
                "ts": round(time.time(), 3),
                "item": label,
                "status": "ok",
                "duration_sec": round(duration_sec, 6),
                "output_count": output_count,
                "output_bytes": output_bytes,
            }
        )

    def record_bundle_failure(self, label: str, duration_sec: float, error: BaseException) -> None:
        self.bundle_count += 1
        self.failed_count += 1
        self.total_extraction_sec += duration_sec
        # Only the exception class is recorded; messages can embed URLs or paths.
        self._write_record(
            {
                "record": "bundle",
                "version": _EXTRACTION_PROFILE_VERSION,
                "pipeline_id": self.pipeline_id,
                "ts": round(time.time(), 3),
                "item": label,
                "status": "error",
                "duration_sec": round(duration_sec, 6),
                "output_count": 0,
                "output_bytes": 0,
                "error_class": type(error).__name__,
            }
        )

    def record_worker_idle(self, idle_sec: float) -> None:
        self.total_idle_sec += idle_sec

    def record_queue_depth(self, depth: int) -> None:
        self.queue_depth_samples.append(depth)

    def queue_depth_avg(self) -> float:
        if not self.queue_depth_samples:
            return 0.0
        return sum(self.queue_depth_samples) / len(self.queue_depth_samples)

    def queue_depth_max(self) -> int:
        return max(self.queue_depth_samples, default=0)

    def log_summary(self) -> None:
        logger.info(
            "PIPELINE | id=%s | stage=extract | status=summary | bundles=%d | failed=%d | extraction_sec=%.3f | idle_sec=%.3f | outputs=%d | output_bytes=%d | queue_depth_avg=%.2f | queue_depth_max=%d",
            self.pipeline_id,
            self.bundle_count,
            self.failed_count,
            self.total_extraction_sec,
            self.total_idle_sec,
            self.output_count,
            self.output_bytes,
            self.queue_depth_avg(),
            self.queue_depth_max(),
        )

    def finish(self, status: str) -> None:
        """Append the summary record and release the profile file. Never raises."""

        if self._finished:
            return
        self._finished = True
        summary = {
            "record": "summary",
            "version": _EXTRACTION_PROFILE_VERSION,
            "pipeline_id": self.pipeline_id,
            "ts": round(time.time(), 3),
            "status": status,
            "bundles": self.bundle_count,
            "failed": self.failed_count,
            "total_extraction_sec": round(self.total_extraction_sec, 6),
            "total_idle_sec": round(self.total_idle_sec, 6),
            "output_count": self.output_count,
            "output_bytes": self.output_bytes,
            "queue_depth_avg": round(self.queue_depth_avg(), 4),
            "queue_depth_max": self.queue_depth_max(),
        }
        try:
            handle = self._open_profile_file()
            if handle is not None:
                handle.write(json.dumps(summary) + "\n")
                handle.flush()
        except OSError:
            logger.warning(
                "PIPELINE | id=%s | stage=extract | status=profile_write_failed | item=%s",
                self.pipeline_id,
                self.profile_path,
            )
        finally:
            if self._profile_file is not None:
                self._profile_file.close()
                self._profile_file = None

    def _open_profile_file(self):
        if self._profile_broken or self.profile_path is None:
            return None
        if self._profile_file is None:
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            self._profile_file = open(self.profile_path, "a", encoding="utf-8")
        return self._profile_file

    def _write_record(self, record: Dict[str, Any]) -> None:
        if self.profile_path is None or self._finished:
            return
        try:
            handle = self._open_profile_file()
            if handle is not None:
                handle.write(json.dumps(record) + "\n")
                handle.flush()
        except OSError:
            self._profile_broken = True
            if self._profile_file is not None:
                try:
                    self._profile_file.close()
                except OSError:
                    pass
                self._profile_file = None
            logger.warning(
                "PIPELINE | id=%s | stage=extract | status=profile_write_failed | item=%s",
                self.pipeline_id,
                self.profile_path,
            )


def _get_bundle_file_size(bundle: Dict[str, Any]) -> int:
    """Return an optional manifest size only for disk-space reservation."""
    for field in ("fileSize", "size"):
        value = bundle.get(field)
        if type(value) is int and value >= 0:
            return value
    return 0


def _stage_error_summary(exc: BaseException) -> str:
    """Return a one-line, sanitized cause for a failed pipeline item."""
    return sanitize_log_label(f"{type(exc).__name__}: {exc}")


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


async def extract_single_bundle(artifact: PipelineArtifact, config) -> PipelineArtifact:
    """Extract one downloaded artifact into its own staging directory.

    This is the single-bundle extraction boundary (extraction-worker roadmap
    Phase 1): the artifact must have ``bundle_save_path`` populated, and it is
    returned with ``extracted_save_path`` and ``exported_list`` populated.
    Queue management, failure tracking, and cleanup policy stay with callers
    so the staged pipeline and future extraction-worker scheduling share the
    same boundary without nested pipeline queues.
    """

    configured_bundle_cache_root = _configured_path(get_bundle_cache_root(config, artifact.bundle))
    bundle_cache_root = (
        None
        if configured_bundle_cache_root is None
        else Path(prepare_secure_directory(configured_bundle_cache_root).as_posix())
    )
    artifact.extracted_save_path = _prepare_extraction_destination(artifact, config)
    extracted_outputs = await extract_asset_bundle(
        artifact.bundle_save_path,
        artifact.bundle,
        artifact.extracted_save_path,
        unity_version=config.UNITY_VERSION,
        config=config,
        bundle_cache_root=bundle_cache_root,
    )
    artifact.exported_list = _validate_artifact_outputs(
        artifact.extracted_save_path,
        extracted_outputs,
    )
    return artifact


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


async def _prepare_download_destination(
    config,
    bundle: Dict[str, Any],
) -> tuple[Path, Path, str, bool]:
    bundle_cache_root = _configured_path(get_bundle_cache_root(config, bundle))
    if bundle_cache_root is not None:
        bundle_cache_root = Path(prepare_secure_directory(bundle_cache_root).as_posix())
        bundle_save_path = Path(
            resolve_secure_path(bundle_cache_root, bundle["bundleName"]).as_posix()
        )
        await bundle_save_path.parent.mkdir(parents=True, exist_ok=True)
        return (
            bundle_save_path,
            bundle_cache_root,
            bundle_save_path.relative_to(bundle_cache_root).as_posix(),
            False,
        )

    bundle_save_path = Path(await asyncio.to_thread(_reserve_temporary_bundle_path))
    return bundle_save_path, bundle_save_path.parent, bundle_save_path.name, True


async def _download_with_reservation(
    url: str,
    download_root: Path,
    download_relative_path: str,
    config,
    cookie: str | None,
    session: aiohttp.ClientSession,
    required_download_bytes: int,
    label: str,
    download_disk_space_gate: DownloadDiskSpaceGate | None,
) -> None:
    if download_disk_space_gate is not None:
        async with download_disk_space_gate.reserve(required_download_bytes, label):
            await download_deobfuscate_bundle(
                url,
                download_root,
                download_relative_path,
                headers=build_cdn_headers(cookie),
                config=config,
                session=session,
            )
        return
    await download_deobfuscate_bundle(
        url,
        download_root,
        download_relative_path,
        headers=build_cdn_headers(cookie),
        config=config,
        session=session,
    )


async def _download_one_item(
    pipeline_id: str,
    name: str,
    item: DownloadItem,
    extract_queue: asyncio.Queue,
    config,
    cookie: str | None,
    failed_tasks: List[DownloadItem],
    failed_lock: asyncio.Lock,
    download_disk_space_gate: DownloadDiskSpaceGate | None,
    session: aiohttp.ClientSession,
) -> None:
    url, bundle = item
    label = sanitize_log_label(bundle.get("bundleName", url))
    logger.debug(
        "PIPELINE | id=%s | worker=%s | stage=download | action=start_item | item=%s",
        pipeline_id,
        name,
        label,
    )
    required_download_bytes = _get_bundle_file_size(bundle)
    bundle_save_path: Path | None = None
    remove_bundle_after_extract = False
    try:
        (
            bundle_save_path,
            download_root,
            download_relative_path,
            remove_bundle_after_extract,
        ) = await _prepare_download_destination(config, bundle)
        await _download_with_reservation(
            url,
            download_root,
            download_relative_path,
            config,
            cookie,
            session,
            required_download_bytes,
            label,
            download_disk_space_gate,
        )
        await extract_queue.put(
            PipelineArtifact(
                url=url,
                bundle=bundle,
                bundle_save_path=bundle_save_path,
                remove_bundle_after_extract=remove_bundle_after_extract,
            )
        )
    except asyncio.CancelledError:
        if bundle_save_path is not None and remove_bundle_after_extract:
            await bundle_save_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        if bundle_save_path is not None and remove_bundle_after_extract:
            await bundle_save_path.unlink(missing_ok=True)
        logger.error(
            "ERROR | pipeline_id=%s | worker=%s | stage=download | item=%s | error=%s",
            pipeline_id,
            name,
            label,
            _stage_error_summary(exc),
        )
        async with failed_lock:
            failed_tasks.append(item)


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
    del headers
    while True:
        item = await input_queue.get()
        try:
            if item is _QUEUE_SENTINEL:
                return

            await _download_one_item(
                pipeline_id,
                name,
                item,
                extract_queue,
                config,
                cookie,
                failed_tasks,
                failed_lock,
                download_disk_space_gate,
                session,
            )
        finally:
            input_queue.task_done()


def _prepare_extraction_destination(artifact: PipelineArtifact, config) -> Path:
    configured_extracted_root = _configured_path(config.ASSET_LOCAL_EXTRACTED_DIR)
    if configured_extracted_root is not None:
        configured_root = Path(prepare_secure_directory(configured_extracted_root).as_posix())
        if _uses_aggregate_workspace(artifact.bundle, config):
            extracted_save_path = configured_root
        else:
            identity_root = configured_root / _bundle_staging_identity(
                artifact.bundle.get("bundleName")
            )
            extracted_save_path = Path(
                prepare_secure_directory(identity_root / uuid.uuid4().hex).as_posix()
            )
        artifact.remove_extracted_after_upload = False
        return extracted_save_path

    tmp_extracted_save_dir = tempfile.TemporaryDirectory(delete=False)
    artifact.tmp_extracted_save_dir = tmp_extracted_save_dir
    artifact.remove_extracted_after_upload = True
    return Path(tmp_extracted_save_dir.name)


async def _total_output_bytes(exported_list: List[Path] | None) -> int:
    total = 0
    for path in exported_list or []:
        try:
            stat = await path.stat()
        except OSError:
            continue
        total += stat.st_size
    return total


async def _extract_one_artifact(
    pipeline_id: str,
    name: str,
    artifact: PipelineArtifact,
    upload_queue: asyncio.Queue,
    config,
    failed_tasks: List[DownloadItem],
    failed_lock: asyncio.Lock,
    profiler: ExtractionProfiler,
) -> None:
    label = sanitize_log_label(artifact.bundle.get("bundleName", artifact.url))
    logger.debug(
        "PIPELINE | id=%s | worker=%s | stage=extract | action=start_item | item=%s",
        pipeline_id,
        name,
        label,
    )
    handed_to_upload = False
    loop = asyncio.get_running_loop()
    extract_started = loop.time()
    try:
        await extract_single_bundle(artifact, config)
        duration_sec = loop.time() - extract_started
        output_bytes = await _total_output_bytes(artifact.exported_list)
        logger.debug(
            "PIPELINE | id=%s | worker=%s | stage=extract | action=bundle_timing | item=%s | duration_sec=%.3f | outputs=%d | output_bytes=%d",
            pipeline_id,
            name,
            label,
            duration_sec,
            len(artifact.exported_list or []),
            output_bytes,
        )
        profiler.record_bundle(label, duration_sec, len(artifact.exported_list or []), output_bytes)
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
            await _cleanup_artifact(artifact, remove_bundle=True, remove_extracted=True)
        raise
    except Exception as exc:
        profiler.record_bundle_failure(label, loop.time() - extract_started, exc)
        logger.error(
            "ERROR | pipeline_id=%s | worker=%s | stage=extract | item=%s | error=%s",
            pipeline_id,
            name,
            label,
            _stage_error_summary(exc),
        )
        async with failed_lock:
            failed_tasks.append((artifact.url, artifact.bundle))
        await _cleanup_artifact(
            artifact,
            remove_bundle=True,
            remove_extracted=True,
        )


async def _extract_stage(
    pipeline_id: str,
    name: str,
    extract_queue: asyncio.Queue,
    upload_queue: asyncio.Queue,
    config,
    failed_tasks: List[DownloadItem],
    failed_lock: asyncio.Lock,
    profiler: ExtractionProfiler,
) -> None:
    loop = asyncio.get_running_loop()
    while True:
        claim_started = loop.time()
        item = await extract_queue.get()
        profiler.record_worker_idle(loop.time() - claim_started)
        try:
            if item is _QUEUE_SENTINEL:
                return

            await _extract_one_artifact(
                pipeline_id,
                name,
                item,
                upload_queue,
                config,
                failed_tasks,
                failed_lock,
                profiler,
            )
        finally:
            extract_queue.task_done()


async def _adaptive_extract_stage(
    pipeline_id: str,
    name: str,
    scheduler: ExtractionScheduler,
    upload_queue: asyncio.Queue,
    config,
    failed_tasks: List[DownloadItem],
    failed_lock: asyncio.Lock,
    profiler: ExtractionProfiler,
) -> None:
    """Extract stage worker dispatched by the Phase 2 admission scheduler."""

    loop = asyncio.get_running_loop()
    while True:
        claim_started = loop.time()
        artifact = await scheduler.claim()
        profiler.record_worker_idle(loop.time() - claim_started)
        if artifact is None:
            return

        try:
            await _extract_one_artifact(
                pipeline_id,
                name,
                artifact,
                upload_queue,
                config,
                failed_tasks,
                failed_lock,
                profiler,
            )
        finally:
            await scheduler.release(artifact)


async def _upload_artifact_to_storages(artifact: PipelineArtifact, config, label: str) -> None:
    if not config.ASSET_REMOTE_STORAGE:
        return
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


async def _upload_one_artifact(
    pipeline_id: str,
    name: str,
    artifact: PipelineArtifact,
    config,
    failed_tasks: List[DownloadItem],
    failed_lock: asyncio.Lock,
) -> None:
    label = sanitize_log_label(artifact.bundle.get("bundleName", artifact.url))
    logger.debug(
        "PIPELINE | id=%s | worker=%s | stage=upload | action=start_item | item=%s",
        pipeline_id,
        name,
        label,
    )
    try:
        await _upload_artifact_to_storages(artifact, config, label)
        logger.debug(
            "PIPELINE | id=%s | worker=%s | stage=upload | action=done_item | item=%s",
            pipeline_id,
            name,
            label,
        )
    except asyncio.CancelledError:
        await _cleanup_artifact(artifact, remove_bundle=True, remove_extracted=True)
        raise
    except Exception as exc:
        logger.error(
            "ERROR | pipeline_id=%s | worker=%s | stage=upload | item=%s | error=%s",
            pipeline_id,
            name,
            label,
            _stage_error_summary(exc),
        )
        async with failed_lock:
            failed_tasks.append((artifact.url, artifact.bundle))
    finally:
        await _cleanup_artifact(
            artifact,
            remove_bundle=True,
            remove_extracted=True,
        )


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

            await _upload_one_artifact(
                pipeline_id,
                name,
                item,
                config,
                failed_tasks,
                failed_lock,
            )
        finally:
            upload_queue.task_done()


async def _sample_queue_depths(
    profiler: ExtractionProfiler,
    extract_queue: asyncio.Queue,
    interval: float = _QUEUE_DEPTH_SAMPLE_INTERVAL_SEC,
) -> None:
    """Sample the extraction queue depth at a fixed interval until cancelled."""

    while True:
        profiler.record_queue_depth(extract_queue.qsize())
        await asyncio.sleep(interval)


async def run_pipeline(
    dl_list: List[DownloadItem],
    config,
    headers: Dict[str, str],
    cookie: str | None = None,
    download_disk_space_gate: DownloadDiskSpaceGate | None = None,
) -> List[DownloadItem]:
    start_time = asyncio.get_running_loop().time()
    pipeline_id = uuid.uuid4().hex[:8]
    profiler = ExtractionProfiler(
        pipeline_id, _resolve_extraction_profile_path(config, pipeline_id)
    )
    total_items = len(dl_list)
    download_concurrency = get_download_stage_concurrency(config)
    scheduler_mode = get_extract_scheduler_mode(config)
    if scheduler_mode == "adaptive":
        extract_concurrency = get_adaptive_extract_worker_count(config)
    else:
        extract_concurrency = get_extract_stage_concurrency(config)
    upload_concurrency = get_upload_stage_concurrency(config)
    extract_queue_size = get_stage_queue_size(config, extract_concurrency)
    upload_queue_size = get_stage_queue_size(config, upload_concurrency)

    download_queue: asyncio.Queue = asyncio.Queue()
    extract_queue: asyncio.Queue | ExtractionScheduler
    if scheduler_mode == "adaptive":
        extract_queue = ExtractionScheduler(
            capacity=extract_queue_size,
            media_slots=get_adaptive_media_slots(config, extract_concurrency),
            media_hints=get_media_bundle_hints(config),
        )
    else:
        extract_queue = asyncio.Queue(maxsize=extract_queue_size)
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
        if isinstance(extract_queue, ExtractionScheduler):
            extract_tasks = [
                asyncio.create_task(
                    _adaptive_extract_stage(
                        pipeline_id,
                        f"extract_worker-{worker_id}",
                        extract_queue,
                        upload_queue,
                        config,
                        failed_tasks,
                        failed_lock,
                        profiler,
                    )
                )
                for worker_id in range(extract_concurrency)
            ]
        else:
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
                        profiler,
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
        profile_sampler = asyncio.create_task(_sample_queue_depths(profiler, extract_queue))
        try:
            await _await_with_worker_monitor(download_queue.join(), worker_monitor)
            logger.info("PIPELINE | id=%s | stage=download | status=completed", pipeline_id)
            if isinstance(extract_queue, ExtractionScheduler):
                await _await_with_worker_monitor(extract_queue.wait_idle(), worker_monitor)
                await _await_with_worker_monitor(extract_queue.close(), worker_monitor)
            else:
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
            profile_sampler.cancel()
            await asyncio.gather(profile_sampler, return_exceptions=True)
            for task in all_tasks:
                task.cancel()
            worker_monitor.cancel()
            await asyncio.gather(*all_tasks, worker_monitor, return_exceptions=True)
            if isinstance(extract_queue, ExtractionScheduler):
                for artifact in await extract_queue.drain_for_cleanup():
                    await _cleanup_artifact(artifact, remove_bundle=True, remove_extracted=True)
            else:
                await _cleanup_queued_artifacts(extract_queue)
            await _cleanup_queued_artifacts(upload_queue)
            profiler.finish("aborted")
            raise

        profile_sampler.cancel()
        await asyncio.gather(profile_sampler, return_exceptions=True)
        profiler.log_summary()
        profiler.finish("completed")

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
