"""Shared concurrency primitives and process pools for bundle processing."""

import asyncio
import atexit
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor

# Advisory cost-class vocabulary shared by the extraction scheduler (which
# classifies artifacts) and the pool selector below (which routes them).
LIGHT_COST_CLASS = "light"
MEDIA_COST_CLASS = "media"


def sanitize_concurrency(value) -> int:
    try:
        concurrency = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"concurrency must be a positive integer, got {value!r}") from None
    if concurrency <= 0:
        raise ValueError(f"concurrency must be a positive integer, got {value!r}")
    return concurrency


def get_legacy_audio_transcode_concurrency(config) -> int:
    return sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_AUDIO_TRANSCODES",
            getattr(config, "MAX_CONCURRENCY", 1),
        )
    )


def get_max_concurrent_audio_files(config) -> int:
    return sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENT_AUDIO_FILES",
            get_legacy_audio_transcode_concurrency(config),
        )
    )


def get_hca_decode_concurrency(config) -> int:
    return sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_HCA_DECODES",
            get_legacy_audio_transcode_concurrency(config),
        )
    )


def get_audio_encoder_concurrency(config) -> int:
    return sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_AUDIO_ENCODERS",
            get_legacy_audio_transcode_concurrency(config),
        )
    )


def get_video_transcode_concurrency(config) -> int:
    return sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_VIDEO_TRANSCODES",
            getattr(config, "MAX_CONCURRENCY", 1),
        )
    )


def get_usm_demux_concurrency(config) -> int:
    return sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_USM_DEMUXES",
            get_video_transcode_concurrency(config),
        )
    )


def get_extract_process_concurrency(config) -> int:
    return sanitize_concurrency(
        getattr(
            config,
            "MAX_CONCURRENCY_EXTRACTS",
            getattr(config, "MAX_CONCURRENCY", 1),
        )
    )


def get_extract_core_concurrency(config) -> int:
    """Concurrency of the core extract pool (light and unclassified bundles)."""

    fallback = get_extract_process_concurrency(config)
    concurrency = getattr(config, "EXTRACT_CORE_CONCURRENCY", fallback)
    return fallback if concurrency is None else sanitize_concurrency(concurrency)


def get_extract_media_concurrency(config) -> int:
    """Concurrency of the media extract pool (media-classified bundles)."""

    fallback = get_extract_process_concurrency(config)
    concurrency = getattr(config, "EXTRACT_MEDIA_CONCURRENCY", fallback)
    return fallback if concurrency is None else sanitize_concurrency(concurrency)


def get_extract_executor_kind(config) -> str:
    """Which executor runs bundle extraction: "process" or "thread".

    unity-rs (0.5+), cridecoder (0.3.5+) and PIL all release the GIL during
    their heavy work, so threads match process throughput on the extract
    workload while sharing one interpreter. Processes remain the default
    because they isolate a native decoder crash to one worker.
    """
    kind = str(getattr(config, "EXTRACT_EXECUTOR", "process")).strip().lower()
    if kind in {"process", "thread"}:
        return kind
    raise ValueError(f'EXTRACT_EXECUTOR must be "process" or "thread", got {kind!r}')


class BundleRuntime:
    """Own shared semaphores and process pools used by bundle pipelines."""

    def __init__(self) -> None:
        self._audio_file_semaphore: tuple[int, asyncio.Semaphore] | None = None
        self._hca_decode_semaphore: tuple[int, asyncio.Semaphore] | None = None
        self._audio_encoder_semaphore: tuple[int, asyncio.Semaphore] | None = None
        self._video_transcode_semaphore: tuple[int, asyncio.Semaphore] | None = None
        self._extract_process_pool: tuple[tuple[str, int], Executor] | None = None
        self._media_extract_pool: tuple[tuple[str, int], Executor] | None = None
        self._audio_process_pool: tuple[int, ProcessPoolExecutor] | None = None
        self._usm_process_pool: tuple[int, ProcessPoolExecutor] | None = None

    @staticmethod
    def _semaphore(
        cache: tuple[int, asyncio.Semaphore] | None, concurrency: int
    ) -> tuple[int, asyncio.Semaphore]:
        if cache is None or cache[0] != concurrency:
            return concurrency, asyncio.Semaphore(concurrency)
        return cache

    def audio_file_semaphore(self, config) -> asyncio.Semaphore:
        self._audio_file_semaphore = self._semaphore(
            self._audio_file_semaphore, get_max_concurrent_audio_files(config)
        )
        return self._audio_file_semaphore[1]

    def hca_decode_semaphore(self, config) -> asyncio.Semaphore:
        self._hca_decode_semaphore = self._semaphore(
            self._hca_decode_semaphore, get_hca_decode_concurrency(config)
        )
        return self._hca_decode_semaphore[1]

    def audio_encoder_semaphore(self, config) -> asyncio.Semaphore:
        self._audio_encoder_semaphore = self._semaphore(
            self._audio_encoder_semaphore, get_audio_encoder_concurrency(config)
        )
        return self._audio_encoder_semaphore[1]

    def video_transcode_semaphore(self, config) -> asyncio.Semaphore:
        self._video_transcode_semaphore = self._semaphore(
            self._video_transcode_semaphore, get_video_transcode_concurrency(config)
        )
        return self._video_transcode_semaphore[1]

    @staticmethod
    def _process_pool(
        cache: tuple[int, ProcessPoolExecutor] | None, concurrency: int
    ) -> tuple[int, ProcessPoolExecutor]:
        if cache is not None and cache[0] == concurrency:
            return cache
        if cache is not None:
            cache[1].shutdown(wait=False, cancel_futures=False)
        return concurrency, ProcessPoolExecutor(max_workers=concurrency)

    @staticmethod
    def _kinded_extract_pool(
        cache: tuple[tuple[str, int], Executor] | None,
        kind: str,
        concurrency: int,
        thread_name_prefix: str,
    ) -> tuple[tuple[str, int], Executor]:
        cache_key = (kind, concurrency)
        if cache is not None and cache[0] == cache_key:
            return cache
        if cache is not None:
            cache[1].shutdown(wait=False, cancel_futures=False)
        if kind == "thread":
            executor: Executor = ThreadPoolExecutor(
                max_workers=concurrency, thread_name_prefix=thread_name_prefix
            )
        else:
            executor = ProcessPoolExecutor(max_workers=concurrency)
        return cache_key, executor

    def extract_process_pool(self, config) -> Executor:
        self._extract_process_pool = self._kinded_extract_pool(
            self._extract_process_pool,
            get_extract_executor_kind(config),
            get_extract_core_concurrency(config),
            "extract",
        )
        return self._extract_process_pool[1]

    def media_extract_pool(self, config) -> Executor:
        """Extract pool reserved for media-classified bundles in adaptive mode."""

        self._media_extract_pool = self._kinded_extract_pool(
            self._media_extract_pool,
            get_extract_executor_kind(config),
            get_extract_media_concurrency(config),
            "extract-media",
        )
        return self._media_extract_pool[1]

    def extract_pool_for(self, config, cost_class: str | None) -> Executor:
        """Route an extraction to the media or core pool by its cost class.

        ``cost_class`` uses the scheduler's classification vocabulary
        (``MEDIA_COST_CLASS``); anything else — including ``None`` from the
        fixed stage or standalone callers — stays on the legacy shared pool.
        """

        if cost_class == MEDIA_COST_CLASS:
            return self.media_extract_pool(config)
        return self.extract_process_pool(config)

    def audio_process_pool(self, config) -> ProcessPoolExecutor:
        self._audio_process_pool = self._process_pool(
            self._audio_process_pool, get_hca_decode_concurrency(config)
        )
        return self._audio_process_pool[1]

    def usm_process_pool(self, config) -> ProcessPoolExecutor:
        self._usm_process_pool = self._process_pool(
            self._usm_process_pool, get_usm_demux_concurrency(config)
        )
        return self._usm_process_pool[1]

    @staticmethod
    def _shutdown_pool(
        cache: tuple[object, Executor] | None,
        *,
        wait: bool,
        cancel_futures: bool,
    ) -> None:
        if cache is not None:
            cache[1].shutdown(wait=wait, cancel_futures=cancel_futures)

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = False) -> None:
        self._shutdown_pool(self._extract_process_pool, wait=wait, cancel_futures=cancel_futures)
        self._shutdown_pool(self._media_extract_pool, wait=wait, cancel_futures=cancel_futures)
        self._shutdown_pool(self._audio_process_pool, wait=wait, cancel_futures=cancel_futures)
        self._shutdown_pool(self._usm_process_pool, wait=wait, cancel_futures=cancel_futures)
        self._extract_process_pool = None
        self._media_extract_pool = None
        self._audio_process_pool = None
        self._usm_process_pool = None


runtime = BundleRuntime()
atexit.register(runtime.shutdown)


def shutdown_process_pools(*, wait: bool = True, cancel_futures: bool = True) -> None:
    """Shut down all cached process pools used by bundle processing."""
    runtime.shutdown(wait=wait, cancel_futures=cancel_futures)
