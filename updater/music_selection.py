"""Regional-only music and vocal asset selection."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiohttp

from updater.model import ConfigLike
from updater.net.http import get_http_session_options

_MASTER_DB_REGION = {"en": "en", "tw": "tc", "kr": "kr", "cn": "cn"}
_MASTER_DB_BASE = "https://sekai-world.github.io/sekai-master-db-{region}-diff"
_JP_MASTER_DB_BASE = "https://sekai-world.github.io/sekai-master-db-diff"
_ASSET_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_MUSIC_SCORE_RE = re.compile(r"([0-9]+)_.+\Z")


@dataclass(frozen=True)
class RegionalMusicSelection:
    exclusive_music_ids: frozenset[int]
    vocal_assetbundle_names: frozenset[str]
    jacket_assetbundle_names: frozenset[str]

    def allows_bundle(self, name: str) -> bool:
        if not isinstance(name, str) or not name:
            return False

        parts = name.split("/")
        if parts[0] != "music":
            return True
        if len(parts) != 3 or any(not part for part in parts):
            return False

        namespace, category, asset_name = parts
        if namespace != "music":
            return True
        if category == "jacket":
            return asset_name in self.jacket_assetbundle_names
        if category in {"short", "long"}:
            return asset_name in self.vocal_assetbundle_names
        if category == "music_score":
            match = _MUSIC_SCORE_RE.fullmatch(asset_name)
            return match is not None and int(match.group(1)) in self.exclusive_music_ids
        return False

    def allows_music_id(self, music_id: int) -> bool:
        return music_id in self.exclusive_music_ids


class _RegionalMusicSelectionError(ValueError):
    """Raised when regional master data cannot be trusted for filtering."""


def _require_positive_int(value: object, description: str) -> int:
    if type(value) is not int or value <= 0:
        raise _RegionalMusicSelectionError(f"{description} must be a positive integer")
    return value


def _require_asset_name(value: object, description: str) -> str:
    if (
        not isinstance(value, str)
        or _ASSET_NAME_RE.fullmatch(value) is None
        or value in {".", ".."}
    ):
        raise _RegionalMusicSelectionError(f"{description} must be a safe asset name")
    return value


def _require_table(value: object, table_name: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not value:
        raise _RegionalMusicSelectionError(f"{table_name} must be a non-empty list")

    rows: list[Mapping[str, Any]] = []
    seen_ids: set[int] = set()
    for index, row in enumerate(value):
        label = f"{table_name}[{index}]"
        if not isinstance(row, Mapping):
            raise _RegionalMusicSelectionError(f"{label} must be an object")
        row_id = _require_positive_int(row.get("id"), f"{label}.id")
        if row_id in seen_ids:
            raise _RegionalMusicSelectionError(f"{table_name} contains duplicate id {row_id}")
        seen_ids.add(row_id)
        rows.append(row)
    return rows


def _validate_musics(value: object, table_name: str) -> dict[int, str]:
    rows = _require_table(value, table_name)
    musics: dict[int, str] = {}
    for index, row in enumerate(rows):
        label = f"{table_name}[{index}]"
        music_id = _require_positive_int(row.get("id"), f"{label}.id")
        jacket_name = _require_asset_name(row.get("assetbundleName"), f"{label}.assetbundleName")
        musics[music_id] = jacket_name
    return musics


def _validate_vocals(
    value: object,
    table_name: str,
    music_ids: set[int],
) -> list[tuple[int, str]]:
    rows = _require_table(value, table_name)
    vocals: list[tuple[int, str]] = []
    for index, row in enumerate(rows):
        label = f"{table_name}[{index}]"
        music_id = _require_positive_int(row.get("musicId"), f"{label}.musicId")
        if music_id not in music_ids:
            raise _RegionalMusicSelectionError(
                f"{label}.musicId references unknown music id {music_id}"
            )
        asset_name = _require_asset_name(row.get("assetbundleName"), f"{label}.assetbundleName")
        vocals.append((music_id, asset_name))
    return vocals


def build_regional_music_selection(
    *,
    region_musics: object,
    region_vocals: object,
    jp_musics: object,
) -> RegionalMusicSelection:
    """Build a fail-closed difference between one region and JP master data."""
    regional_music = _validate_musics(region_musics, "region_musics")
    japanese_music = _validate_musics(jp_musics, "jp_musics")
    regional_vocals = _validate_vocals(region_vocals, "region_vocals", set(regional_music))

    exclusive_music_ids = frozenset(set(regional_music) - set(japanese_music))
    jacket_names = frozenset(regional_music[music_id] for music_id in exclusive_music_ids)
    vocal_names = frozenset(
        asset_name for music_id, asset_name in regional_vocals if music_id in exclusive_music_ids
    )

    return RegionalMusicSelection(
        exclusive_music_ids=exclusive_music_ids,
        vocal_assetbundle_names=vocal_names,
        jacket_assetbundle_names=jacket_names,
    )


def _enabled(config: object) -> bool:
    enabled = getattr(config, "ENABLE_REGION_EXCLUSIVE_MUSIC", False)
    if not isinstance(enabled, bool):
        raise ValueError("ENABLE_REGION_EXCLUSIVE_MUSIC must be a bool")
    return enabled


def _region_code(config: object) -> str:
    region = getattr(config, "REGION", None)
    if isinstance(region, Enum):
        value = region.value
        if not isinstance(value, str):
            value = region.name
    elif isinstance(region, str):
        value = region
    else:
        value = getattr(region, "value", None)
        if not isinstance(value, str):
            value = getattr(region, "name", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("REGION must be a supported enum or region string")
    return value.strip().lower()


async def _fetch_table(session: aiohttp.ClientSession, url: str) -> object:
    async with session.get(url) as response:
        response.raise_for_status()
        return await response.json()


async def load_regional_music_selection(
    config: object,
) -> RegionalMusicSelection | None:
    """Fetch the current JP and regional music tables and compute their delta."""
    if not _enabled(config):
        return None

    region = _region_code(config)
    if region == "jp":
        return None
    db_region = _MASTER_DB_REGION.get(region)
    if db_region is None:
        raise ValueError(f"unsupported REGION for regional music selection: {region}")

    regional_base = _MASTER_DB_BASE.format(region=db_region)
    urls = (
        f"{_JP_MASTER_DB_BASE}/musics.json",
        f"{regional_base}/musics.json",
        f"{regional_base}/musicVocals.json",
    )
    async with aiohttp.ClientSession(**get_http_session_options(config)) as session:
        jp_musics, region_musics, region_vocals = await asyncio.gather(
            *(_fetch_table(session, url) for url in urls)
        )

    return build_regional_music_selection(
        region_musics=region_musics,
        region_vocals=region_vocals,
        jp_musics=jp_musics,
    )


class _RegionalMusicConfigProxy:
    __slots__ = ("_base", "_regional_music_selection")

    def __init__(
        self,
        base: ConfigLike,
        selection: RegionalMusicSelection,
    ) -> None:
        self._base = base
        self._regional_music_selection = selection

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_base"), name)


async def prepare_regional_music_config(config: ConfigLike) -> ConfigLike:
    """Return an immutable per-run config proxy with its regional selection."""
    if not _enabled(config):
        return config
    region = _region_code(config)
    if region == "jp":
        return config

    if isinstance(config, _RegionalMusicConfigProxy):
        selection = getattr(config, "_regional_music_selection", None)
        if isinstance(selection, RegionalMusicSelection):
            return config

    selection = await load_regional_music_selection(config)
    if not isinstance(selection, RegionalMusicSelection):
        raise ValueError("regional music selection was not prepared")
    return _RegionalMusicConfigProxy(config, selection)  # type: ignore[return-value]


def get_regional_music_selection(config: object) -> RegionalMusicSelection | None:
    """Get the prepared per-run selection, failing closed if integration omitted it."""
    if not _enabled(config):
        return None
    region = _region_code(config)
    if region == "jp":
        return None
    if not isinstance(config, _RegionalMusicConfigProxy):
        raise ValueError("regional music config must be prepared before use")

    selection = getattr(config, "_regional_music_selection", None)
    if not isinstance(selection, RegionalMusicSelection):
        raise ValueError("prepared config has no regional music selection")
    return selection
