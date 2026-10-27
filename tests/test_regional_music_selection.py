from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest

import updater.music_selection as regional_music
from updater.model import SekaiServerRegion
from updater.music_selection import (
    RegionalMusicSelection,
    build_regional_music_selection,
    get_regional_music_selection,
    load_regional_music_selection,
    prepare_regional_music_config,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "regional_music" / "real_regressions.json"


def _real_rows() -> dict[str, Any]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _music(music_id: int, asset_name: str | None = None) -> dict[str, Any]:
    return {
        "id": music_id,
        "assetbundleName": asset_name or f"jacket_s_{music_id:03d}",
    }


def _vocal(
    row_id: int,
    music_id: int,
    asset_name: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": row_id,
        "musicId": music_id,
        "assetbundleName": asset_name,
        **extra,
    }


def _tables(**overrides: Any) -> dict[str, Any]:
    tables = {
        "region_musics": [_music(1)],
        "region_vocals": [_vocal(11, 1, "vs_0001_01")],
        "jp_musics": [_music(1)],
    }
    tables.update(overrides)
    return tables


def _build(**overrides: Any) -> RegionalMusicSelection:
    return build_regional_music_selection(**_tables(**overrides))


def test_real_en_502_english_vocal_is_not_selected_for_shared_song() -> None:
    rows = _real_rows()
    selection = build_regional_music_selection(
        region_musics=rows["en_musics"],
        region_vocals=rows["en_vocals"],
        jp_musics=rows["jp_musics"],
    )

    assert selection.exclusive_music_ids == frozenset()
    assert selection.vocal_assetbundle_names == frozenset()
    assert selection.jacket_assetbundle_names == frozenset()
    assert not selection.allows_bundle("music/short/vs_0502_02")
    assert not selection.allows_bundle("music/jacket/jacket_s_502")
    assert not selection.allows_bundle("music/music_score/0502_score")


def test_real_cn_76_shared_song_alias_is_not_selected() -> None:
    rows = _real_rows()
    selection = build_regional_music_selection(
        region_musics=rows["cn_musics"],
        region_vocals=rows["cn_vocals"],
        jp_musics=rows["jp_musics"],
    )

    assert selection.exclusive_music_ids == frozenset()
    assert selection.vocal_assetbundle_names == frozenset()


def test_real_cn_660_singer_replacement_is_not_selected_for_shared_song() -> None:
    rows = _real_rows()
    selection = build_regional_music_selection(
        region_musics=rows["cn_musics"],
        region_vocals=rows["cn_vocals"],
        jp_musics=rows["jp_musics"],
    )

    assert selection.exclusive_music_ids == frozenset()
    assert selection.vocal_assetbundle_names == frozenset()
    assert not hasattr(selection, "vocal_collision_keys")


def test_real_kr_707_708_instrumental_renames_are_not_selected() -> None:
    rows = _real_rows()
    selection = build_regional_music_selection(
        region_musics=rows["kr_musics"],
        region_vocals=rows["kr_vocals"],
        jp_musics=rows["jp_musics"],
    )

    assert selection.exclusive_music_ids == frozenset()
    assert selection.vocal_assetbundle_names == frozenset()


def test_common_song_vocals_need_no_type_or_character_metadata_and_are_excluded() -> None:
    selection = _build(
        region_vocals=[_vocal(999, 1, "new_shared_vocal")],
    )

    assert selection.vocal_assetbundle_names == frozenset()


def test_shared_song_instrumental_bundle_rename_is_not_selected() -> None:
    selection = _build(
        region_vocals=[
            _vocal(
                12,
                1,
                "in_0001_region",
                musicVocalType="instrumental",
                characters=[{"characterType": "game_character", "characterId": 2}],
            ),
        ],
    )

    assert selection.vocal_assetbundle_names == frozenset()


def test_exclusive_song_selects_its_jacket_score_and_all_vocals_only() -> None:
    selection = _build(
        region_musics=[_music(1), _music(2, "jacket_s_002")],
        region_vocals=[
            _vocal(11, 1, "vs_0001_reg"),
            _vocal(12, 2, "vs_0002_01"),
            _vocal(
                13,
                2,
                "vs_0002_02",
                musicVocalType="another_vocal",
                characters=[{"characterType": "game_character", "characterId": 2}],
            ),
        ],
        jp_musics=[_music(1)],
    )

    assert selection.exclusive_music_ids == frozenset({2})
    assert selection.jacket_assetbundle_names == frozenset({"jacket_s_002"})
    assert selection.vocal_assetbundle_names == frozenset({"vs_0002_01", "vs_0002_02"})
    assert selection.allows_bundle("music/jacket/jacket_s_002")
    assert not selection.allows_bundle("music/jacket/jacket_s_001")
    assert selection.allows_bundle("music/music_score/0002_score")
    assert not selection.allows_bundle("music/music_score/0001_score")


def test_bundle_matching_is_exact_and_unknown_music_paths_fail_closed() -> None:
    selection = RegionalMusicSelection(
        exclusive_music_ids=frozenset({1}),
        vocal_assetbundle_names=frozenset({"vs_0001_01"}),
        jacket_assetbundle_names=frozenset({"jacket_s_001"}),
    )

    assert selection.allows_bundle("music/short/vs_0001_01")
    assert selection.allows_bundle("music/long/vs_0001_01")
    assert not selection.allows_bundle("music/short/vs_0001_010")
    assert selection.allows_bundle("music/jacket/jacket_s_001")
    assert not selection.allows_bundle("music/jacket/jacket_s_001_extra")
    assert selection.allows_bundle("music/music_score/0001_score")
    assert selection.allows_bundle("music/music_score/001_score")
    assert not selection.allows_bundle("music/music_score/１_score")
    assert not selection.allows_bundle("music/music_score/00010_score")
    assert not selection.allows_bundle("music/music_score/not-a-score")
    assert not selection.allows_bundle("music/unknown/vs_0001_01")
    assert not selection.allows_bundle("music/short/vs_0001_01/extra")
    assert selection.allows_bundle("streaming_live/music/event_music")
    assert selection.allows_bundle("character/character3d/ichika")


@pytest.mark.parametrize(
    ("table_name", "invalid_value"),
    [
        ("region_musics", []),
        ("region_vocals", []),
        ("jp_musics", []),
        ("region_musics", ()),
        ("jp_musics", [None]),
    ],
)
def test_empty_or_malformed_tables_fail_closed(table_name: str, invalid_value: object) -> None:
    with pytest.raises(ValueError):
        _build(**{table_name: invalid_value})


@pytest.mark.parametrize(
    ("table_name", "row"),
    [
        ("region_musics", {"id": True, "assetbundleName": "jacket_s_001"}),
        ("region_musics", {"id": 1, "assetbundleName": "../unsafe"}),
        (
            "region_vocals",
            {
                "id": 11,
                "musicId": 1,
            },
        ),
        (
            "region_vocals",
            {
                "id": 11,
                "musicId": 1,
                "assetbundleName": "../unsafe",
            },
        ),
        ("region_vocals", {"id": 11, "musicId": 2, "assetbundleName": "vs_1"}),
    ],
)
def test_bad_ids_assets_and_orphan_vocals_fail_closed(
    table_name: str,
    row: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        _build(**{table_name: [row]})


def test_duplicate_ids_fail_closed_for_music_and_vocal_tables() -> None:
    duplicate_music_rows = [_music(1), _music(1, "jacket_other")]
    with pytest.raises(ValueError, match="duplicate id"):
        _build(region_musics=duplicate_music_rows)

    duplicate_vocal_rows = [_vocal(11, 1, "vs_1"), _vocal(11, 1, "vs_2")]
    with pytest.raises(ValueError, match="duplicate id"):
        _build(region_vocals=duplicate_vocal_rows)


def test_jp_table_cannot_be_empty_or_every_region_song_looks_exclusive() -> None:
    with pytest.raises(ValueError, match="jp_musics"):
        _build(jp_musics=[])


def test_disabled_and_jp_config_skip_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_session(*args: Any, **kwargs: Any) -> None:
        pytest.fail("network session must not be created")

    monkeypatch.setattr(regional_music.aiohttp, "ClientSession", forbidden_session)
    disabled = SimpleNamespace(REGION="en", ENABLE_REGION_EXCLUSIVE_MUSIC=False)
    default_disabled = SimpleNamespace(REGION="en")
    jp = SimpleNamespace(REGION=SekaiServerRegion.JP, ENABLE_REGION_EXCLUSIVE_MUSIC=True)

    assert asyncio.run(load_regional_music_selection(disabled)) is None
    assert asyncio.run(prepare_regional_music_config(disabled)) is disabled
    assert asyncio.run(load_regional_music_selection(default_disabled)) is None
    assert asyncio.run(prepare_regional_music_config(default_disabled)) is default_disabled
    assert asyncio.run(load_regional_music_selection(jp)) is None
    assert asyncio.run(prepare_regional_music_config(jp)) is jp
    assert get_regional_music_selection(disabled) is None
    assert get_regional_music_selection(jp) is None


@pytest.mark.parametrize("invalid_flag", [1, "true", None])
def test_enabled_flag_must_be_boolean(invalid_flag: object) -> None:
    config = SimpleNamespace(REGION="en", ENABLE_REGION_EXCLUSIVE_MUSIC=invalid_flag)
    with pytest.raises(ValueError, match="must be a bool"):
        get_regional_music_selection(config)


def test_tw_fetch_uses_tc_tables_and_shared_http_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = _real_rows()
    requested_urls: list[str] = []
    session_options: list[dict[str, Any]] = []

    class Response:
        def __init__(self, body: object) -> None:
            self.body = body

        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            return None

        async def json(self) -> object:
            return self.body

    class Session:
        def __init__(self, **options: Any) -> None:
            session_options.append(options)

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> Response:
            requested_urls.append(url)
            if "sekai-master-db-diff/" in url:
                return Response(rows["jp_musics"])
            if url.endswith("/musics.json"):
                return Response(rows["cn_musics"])
            return Response(rows["cn_vocals"])

    monkeypatch.setattr(regional_music.aiohttp, "ClientSession", Session)
    config = SimpleNamespace(
        REGION=SekaiServerRegion.TW,
        ENABLE_REGION_EXCLUSIVE_MUSIC=True,
        PROXY_URL="http://proxy.example",
        REQUEST_TIMEOUT=12,
    )

    selection = asyncio.run(load_regional_music_selection(config))

    assert selection is not None
    assert len(requested_urls) == 3
    assert set(requested_urls) == {
        "https://sekai-world.github.io/sekai-master-db-diff/musics.json",
        "https://sekai-world.github.io/sekai-master-db-tc-diff/musics.json",
        "https://sekai-world.github.io/sekai-master-db-tc-diff/musicVocals.json",
    }
    assert len(session_options) == 1
    assert session_options[0]["proxy"] == "http://proxy.example"
    assert isinstance(session_options[0]["timeout"], aiohttp.ClientTimeout)
    assert session_options[0]["timeout"].total == 12


def test_fetch_error_propagates_without_attaching_partial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        async def __aenter__(self) -> Response:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def raise_for_status(self) -> None:
            raise aiohttp.ClientError("public master data unavailable")

        async def json(self) -> object:
            pytest.fail("failed response must not be decoded")

    class Session:
        def __init__(self, **options: Any) -> None:
            pass

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> Response:
            return Response()

    monkeypatch.setattr(regional_music.aiohttp, "ClientSession", Session)
    config = SimpleNamespace(REGION="en", ENABLE_REGION_EXCLUSIVE_MUSIC=True)
    preparation = prepare_regional_music_config(config)

    with pytest.raises(aiohttp.ClientError, match="unavailable"):
        asyncio.run(preparation)
    assert not hasattr(config, "_regional_music_selection")
    with pytest.raises(ValueError, match="must be prepared"):
        get_regional_music_selection(config)


def test_prepare_is_idempotent_and_does_not_mutate_or_globally_cache_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = RegionalMusicSelection(
        exclusive_music_ids=frozenset({2}),
        vocal_assetbundle_names=frozenset({"vs_0002_01"}),
        jacket_assetbundle_names=frozenset({"jacket_s_002"}),
    )
    calls: list[object] = []

    async def fake_load(config: object) -> RegionalMusicSelection:
        calls.append(config)
        return selection

    monkeypatch.setattr(regional_music, "load_regional_music_selection", fake_load)
    base = SimpleNamespace(REGION="cn", ENABLE_REGION_EXCLUSIVE_MUSIC=True)
    prepared = asyncio.run(prepare_regional_music_config(base))

    assert prepared is not base
    assert get_regional_music_selection(prepared) is selection
    assert not hasattr(base, "_regional_music_selection")
    assert asyncio.run(prepare_regional_music_config(prepared)) is prepared
    assert len(calls) == 1

    separate = SimpleNamespace(REGION="cn", ENABLE_REGION_EXCLUSIVE_MUSIC=True)
    separate_prepared = asyncio.run(prepare_regional_music_config(separate))
    assert separate_prepared is not prepared
    assert len(calls) == 2
    assert not hasattr(separate, "_regional_music_selection")
