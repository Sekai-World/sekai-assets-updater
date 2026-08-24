"""Shared concurrency primitives and process pools for bundle processing."""

import asyncio
import atexit
from concurrent.futures import ProcessPoolExecutor


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


class BundleRuntime:
    """Own shared semaphores and process pools used by bundle pipelines."""

    def __init__(self) -> None:
        self._audio_file_semaphore: tuple[int, asyncio.Semaphore] | None = None
        self._hca_decode_semaphore: tuple[int, asyncio.Semaphore] | None = None
        self._audio_encoder_semaphore: tuple[int, asyncio.Semaphore] | None = None
        self._video_transcode_semaphore: tuple[int, asyncio.Semaphore] | None = None
        self._extract_process_pool: tuple[int, ProcessPoolExecutor] | None = None
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

    def extract_process_pool(self, config) -> ProcessPoolExecutor:
        self._extract_process_pool = self._process_pool(
            self._extract_process_pool, get_extract_process_concurrency(config)
        )
        return self._extract_process_pool[1]

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
        cache: tuple[int, ProcessPoolExecutor] | None,
        *,
        wait: bool,
        cancel_futures: bool,
    ) -> None:
        if cache is not None:
            cache[1].shutdown(wait=wait, cancel_futures=cancel_futures)

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = False) -> None:
        self._shutdown_pool(self._extract_process_pool, wait=wait, cancel_futures=cancel_futures)
        self._shutdown_pool(self._audio_process_pool, wait=wait, cancel_futures=cancel_futures)
        self._shutdown_pool(self._usm_process_pool, wait=wait, cancel_futures=cancel_futures)
        self._extract_process_pool = None
        self._audio_process_pool = None
        self._usm_process_pool = None


runtime = BundleRuntime()
atexit.register(runtime.shutdown)
