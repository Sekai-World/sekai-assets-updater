from enum import Enum
from typing import Any, List, Optional, Protocol

from anyio import Path


class ConfigLike(Protocol):
    MAX_CONCURRENCY: int
    MAX_CONCURRENT_AUDIO_FILES: int
    MAX_CONCURRENCY_HCA_DECODES: int
    MAX_CONCURRENCY_AUDIO_ENCODERS: int
    MAX_CONCURRENCY_AUDIO_TRANSCODES: int
    HCA_DECODE_BACKEND: str
    MAX_CONCURRENCY_VIDEO_TRANSCODES: int
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
