from enum import Enum
from typing import Any, List, Optional, Protocol

from anyio import Path


class ConfigLike(Protocol):
    UPDATER_MODE: str
    ENABLE_LIVE2D_POSTPROCESS: bool
    ENABLE_CHARTS_POSTPROCESS: bool
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
    EXTERNAL_PROCESS_TIMEOUT: int | float
    DOWNLOAD_MAX_RETRIES: int
    DOWNLOAD_RETRY_BASE_DELAY: int | float
    DOWNLOAD_RETRY_MAX_DELAY: int | float
    MIN_FREE_DISK_BYTES: int
    DOWNLOAD_DISK_SPACE_CHECK_INTERVAL: int | float
    MAX_CONCURRENCY_VIDEO_TRANSCODES: int
    MAX_CONCURRENCY_USM_DEMUXES: int
    MAX_CONCURRENCY_UPLOADS: int
    TEXTURE_OUTPUT_FORMATS: tuple[str, ...]
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
    ASSET_LOCAL_EXTRACTED_DIR: Optional[Path]
    ASSET_LOCAL_BUNDLE_CACHE_DIR: Optional[Path]
    LIVE2D_BUNDLE_CACHE_DIR: Optional[Path]
    CHART_DATA_SERVER: Optional[str]
    CHART_JACKET_BASE_URL: Optional[str]
    ASSET_REMOTE_STORAGE: Optional[list[dict[str, Any]]]


class SekaiServerRegion(Enum):
    JP = "jp"
    EN = "en"
    TW = "tw"
    KR = "kr"
    CN = "cn"
