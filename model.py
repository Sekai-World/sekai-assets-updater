from enum import Enum
from typing import Any, List, Optional, Protocol

from anyio import Path


class ConfigLike(Protocol):
    MAX_CONCURRENCY: int
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
