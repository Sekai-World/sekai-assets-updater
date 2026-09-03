"""Focused tests for the local raw Live2D master-data provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from updater.live2d.association import LIVE2D_TABLE_NAMES
from updater.live2d.master_data import (
    Live2DMasterDataError,
    Live2DMasterDataFileError,
    Live2DMasterDataJSONError,
    Live2DMasterDataShapeError,
    LocalMasterDataProvider,
)


def write_tables(root: Path) -> None:
    rows = {
        "character2ds": [
            {
                "id": 101,
                "characterId": 1,
                "assetName": "ichika",
                "dialog": "must not cross the provider boundary",
            }
        ],
        "costume2ds": [
            {
                "id": 1001,
                "character2dId": 101,
                "live2dAssetbundleName": "v2_01ichika_unit",
                "voice": "must not cross the provider boundary",
            }
        ],
        "systemLive2ds": [
            {
                "id": 2001,
                "characterId": 1,
                "motion": "system_idle",
                "expression": "system_smile",
                "url": "https://must-not-cross.example",
            }
        ],
        "bondsLive2ds": [
            {
                "id": 2002,
                "characterId": 1,
                "motion": "bonds_idle",
                "expression": "bonds_smile",
                "payload": "must not cross the provider boundary",
            }
        ],
        "bondsRankUpLive2ds": [
            {
                "id": 2003,
                "characterId": 1,
                "motion": "rank_idle",
                "expression": "rank_smile",
                "transport": "must not cross the provider boundary",
            }
        ],
        "loginBonusLive2ds": [
            {
                "id": 2004,
                "characterId": 1,
                "motion": "login_idle",
                "expression": "login_smile",
                "assetList": "must not cross the provider boundary",
            }
        ],
    }
    for table_name in LIVE2D_TABLE_NAMES:
        (root / f"{table_name}.json").write_text(
            json.dumps(rows[table_name]),
            encoding="utf-8",
        )


def test_loads_all_six_tables_and_normalizes_without_raw_fields(tmp_path: Path) -> None:
    write_tables(tmp_path)

    snapshot = LocalMasterDataProvider(tmp_path, "6.8.0.10").load_live2d_snapshot()

    assert snapshot.master_db_version == "6.8.0.10"
    assert set(snapshot.tables) == set(LIVE2D_TABLE_NAMES)
    assert snapshot.tables["character2ds"][0] == {
        "id": 101,
        "characterId": 1,
        "assetName": "ichika",
    }
    assert snapshot.tables["costume2ds"][0] == {
        "id": 1001,
        "character2dId": 101,
        "assetName": "v2_01ichika_unit",
    }
    assert snapshot.tables["systemLive2ds"][0] == {
        "id": 2001,
        "characterId": 1,
        "motion": "system_idle",
        "expression": "system_smile",
    }
    assert all(
        set(row).issubset({"id", "characterId", "motion", "expression"})
        for table_name in (
            "systemLive2ds",
            "bondsLive2ds",
            "bondsRankUpLive2ds",
            "loginBonusLive2ds",
        )
        for row in snapshot.tables[table_name]
    )
    serialized = repr(snapshot.tables).casefold()
    assert "must not cross" not in serialized
    assert "https://must-not-cross.example" not in serialized
    assert "live2dassetbundlename" not in snapshot.tables["costume2ds"][0]


def test_snapshot_tables_are_immutable(tmp_path: Path) -> None:
    write_tables(tmp_path)
    snapshot = LocalMasterDataProvider(tmp_path, "6.8.0.10").load_live2d_snapshot()

    with pytest.raises(TypeError):
        snapshot.tables["character2ds"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        snapshot.tables["character2ds"][0]["id"] = 999  # type: ignore[index]


def test_matching_raw_asset_name_is_accepted(tmp_path: Path) -> None:
    write_tables(tmp_path)
    costume_path = tmp_path / "costume2ds.json"
    costume_path.write_text(
        json.dumps(
            [
                {
                    "id": 1001,
                    "character2dId": 101,
                    "live2dAssetbundleName": "v2_01ichika_unit",
                    "assetName": "v2_01ichika_unit",
                }
            ]
        ),
        encoding="utf-8",
    )

    snapshot = LocalMasterDataProvider(tmp_path, "6.8.0.10").load_live2d_snapshot()

    assert snapshot.tables["costume2ds"][0]["assetName"] == "v2_01ichika_unit"


def test_disagreeing_raw_asset_name_is_rejected(tmp_path: Path) -> None:
    write_tables(tmp_path)
    costume_path = tmp_path / "costume2ds.json"
    costume_path.write_text(
        json.dumps(
            [
                {
                    "id": 1001,
                    "character2dId": 101,
                    "live2dAssetbundleName": "v2_01ichika_unit",
                    "assetName": "different_name",
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        Live2DMasterDataError,
        match=r"costume2ds\[0\].*assetName.*live2dAssetbundleName",
    ):
        LocalMasterDataProvider(tmp_path, "6.8.0.10").load_live2d_snapshot()


def test_missing_table_is_rejected(tmp_path: Path) -> None:
    write_tables(tmp_path)
    (tmp_path / "loginBonusLive2ds.json").unlink()

    with pytest.raises(Live2DMasterDataFileError, match="loginBonusLive2ds.*missing"):
        LocalMasterDataProvider(tmp_path, "6.8.0.10").load_live2d_snapshot()


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    write_tables(tmp_path)
    (tmp_path / "systemLive2ds.json").write_text("not json", encoding="utf-8")

    with pytest.raises(Live2DMasterDataJSONError, match="systemLive2ds.*invalid JSON"):
        LocalMasterDataProvider(tmp_path, "6.8.0.10").load_live2d_snapshot()


def test_non_array_table_root_is_rejected(tmp_path: Path) -> None:
    write_tables(tmp_path)
    (tmp_path / "bondsLive2ds.json").write_text("{}", encoding="utf-8")

    with pytest.raises(Live2DMasterDataShapeError, match="bondsLive2ds.*root must be an array"):
        LocalMasterDataProvider(tmp_path, "6.8.0.10").load_live2d_snapshot()


def test_missing_master_db_version_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(Live2DMasterDataError, match="master_db_version"):
        LocalMasterDataProvider(tmp_path, "")
