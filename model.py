from enum import Enum
from typing import Any, List, Optional, Protocol

from anyio import Path


class ConfigLike(Protocol):
    MAX_CONCURRENCY: int
    MAX_CONCURRENCY_DOWNLOADS: int
    MAX_CONCURRENCY_EXTRACTS: int
    MAX_CONCURRENCY_UPLOAD_STAGE: int
    PIPELINE_STAGE_QUEUE_SIZE: int
    MAX_CONCURRENT_AUDIO_FILES: int
    MAX_CONCURRENCY_HCA_DECODES: int
    MAX_CONCURRENCY_AUDIO_ENCODERS: int
    MAX_CONCURRENCY_AUDIO_TRANSCODES: int
    HCA_DECODE_BACKEND: str
    REQUEST_TIMEOUT: int | float | None
    DOWNLOAD_MAX_RETRIES: int
    MIN_FREE_DISK_BYTES: int
    DOWNLOAD_DISK_SPACE_CHECK_INTERVAL: int | float
    MAX_CONCURRENCY_VIDEO_TRANSCODES: int
    TEXTURE_OUTPUT_FORMATS: tuple[str, ...]
    ASSET_STUDIO_FFI_LIBRARY_PATH: Optional[str]
    ASSET_STUDIO_FFI_WORKER_PATH: Optional[str]
    ASSET_STUDIO_FFI_READ_BATCH_SIZE: int
    PROXY_URL: Optional[str]
    USER_AGENT: Optional[str]
    UNITY_VERSION: str
    AES_KEY: bytes
    AES_IV: bytes
    GAME_VERSION_JSON_URL: Optional[str]
    GAME_COOKIE_URL: Optional[str]
    GAME_VERSION_URL: Optional[str]
    ASSET_VER_URL: Optional[str]
    ASSET_BUNDLE_INFO_URL: Optional[str]
    ASSET_BUNDLE_URL: str
    APP_VERSION_OVERRIDE: Optional[str]
    REGION: Any
    DL_LIST_CACHE_PATH: Path
    ASSET_BUNDLE_INFO_CACHE_PATH: Path
    GAME_VERSION_JSON_CACHE_PATH: Path
    DL_INCLUDE_LIST: Optional[List[str]]
    DL_EXCLUDE_LIST: Optional[List[str]]
    DL_PRIORITY_LIST: Optional[List[str]]


class SekaiServerRegion(Enum):
    JP = 'jp'
    EN = 'en'
    TW = 'tw'
    KR = 'kr'
    CN = 'cn'
