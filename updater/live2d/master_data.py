"""Bounded loading and normalization of raw Live2D master-data tables."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from updater.live2d.association import LIVE2D_TABLE_NAMES

__all__ = [
    "LIVE2D_TABLE_NAMES",
    "Live2DMasterDataError",
    "Live2DMasterDataFileError",
    "Live2DMasterDataJSONError",
    "Live2DMasterDataShapeError",
    "Live2DMasterDataSnapshot",
    "LocalMasterDataProvider",
    "MasterDataProvider",
]


class Live2DMasterDataError(ValueError):
    """Base error for invalid or unavailable raw Live2D master data."""


class Live2DMasterDataFileError(Live2DMasterDataError):
    """Raised when a required Live2D master-data file cannot be read."""


class Live2DMasterDataJSONError(Live2DMasterDataError):
    """Raised when a required Live2D master-data file is not valid JSON."""


class Live2DMasterDataShapeError(Live2DMasterDataError):
    """Raised when a required Live2D table has an unsupported JSON shape."""


def _freeze_value(value: object) -> object:
    """Copy JSON containers into immutable equivalents without validating values."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(child) for child in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(child) for child in value)
    return value


def _freeze_tables(
    tables: Mapping[str, object],
) -> Mapping[str, tuple[Mapping[str, object], ...]]:
    frozen: dict[str, tuple[Mapping[str, object], ...]] = {}
    for table_name in LIVE2D_TABLE_NAMES:
        rows = tables[table_name]
        if not isinstance(rows, (list, tuple)):
            raise Live2DMasterDataShapeError(
                f"normalized Live2D table '{table_name}' must be a sequence of row mappings"
            )

        frozen_rows: list[Mapping[str, object]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise Live2DMasterDataShapeError(
                    f"normalized Live2D table '{table_name}' contains a non-object row"
                )
            frozen_row = {key: _freeze_value(value) for key, value in row.items()}
            frozen_rows.append(MappingProxyType(frozen_row))
        frozen[table_name] = tuple(frozen_rows)
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class Live2DMasterDataSnapshot:
    """Immutable, normalized input for :func:`build_live2d_index`."""

    master_db_version: str
    tables: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.master_db_version, str) or not self.master_db_version.strip():
            raise Live2DMasterDataError("master_db_version must be provided as a non-empty string")
        if not isinstance(self.tables, Mapping):
            raise Live2DMasterDataShapeError(
                "normalized Live2D tables must be a mapping of the six required tables"
            )

        missing = [table_name for table_name in LIVE2D_TABLE_NAMES if table_name not in self.tables]
        if missing:
            raise Live2DMasterDataShapeError(
                "normalized Live2D tables are missing: " + ", ".join(missing)
            )
        unknown = sorted(set(self.tables) - set(LIVE2D_TABLE_NAMES))
        if unknown:
            raise Live2DMasterDataShapeError(
                "unsupported normalized Live2D tables: " + ", ".join(unknown)
            )

        object.__setattr__(self, "tables", _freeze_tables(self.tables))


@runtime_checkable
class MasterDataProvider(Protocol):
    """Synchronous source of the normalized Live2D master-data snapshot."""

    def load_live2d_snapshot(self) -> Live2DMasterDataSnapshot:
        """Load all six required Live2D business tables."""

        ...


_TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "character2ds": ("id", "characterId", "assetName"),
    "costume2ds": ("id", "character2dId"),
    "systemLive2ds": ("id", "characterId", "motion", "expression"),
    "bondsLive2ds": ("id", "characterId", "motion", "expression"),
    "bondsRankUpLive2ds": ("id", "characterId", "motion", "expression"),
    "loginBonusLive2ds": ("id", "characterId", "motion", "expression"),
}


def _normalize_row(table_name: str, row: object, row_index: int) -> dict[str, object]:
    if not isinstance(row, Mapping):
        # Keep malformed rows visible to association.py, which owns row-level
        # diagnostics, while ensuring no raw value crosses this boundary.
        return {}

    normalized = {
        field_name: row[field_name] for field_name in _TABLE_FIELDS[table_name] if field_name in row
    }
    if table_name != "costume2ds":
        return normalized

    live2d_asset_name_present = "live2dAssetbundleName" in row
    if live2d_asset_name_present:
        live2d_asset_name = row["live2dAssetbundleName"]
        if "assetName" in row and row["assetName"] != live2d_asset_name:
            raise Live2DMasterDataError(
                f"{table_name}[{row_index}] has conflicting asset names: "
                "assetName does not agree with live2dAssetbundleName"
            )
        normalized["assetName"] = live2d_asset_name
    return normalized


def _read_table(root: Path, table_name: str) -> list[dict[str, object]]:
    table_path = root / f"{table_name}.json"
    if not table_path.exists():
        raise Live2DMasterDataFileError(
            f"Live2D master-data table '{table_name}' is missing: {table_path}"
        )
    if not table_path.is_file():
        raise Live2DMasterDataFileError(
            f"Live2D master-data table '{table_name}' is not a file: {table_path}"
        )

    try:
        raw_table = json.loads(table_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Live2DMasterDataJSONError(
            f"Live2D master-data table '{table_name}' contains invalid JSON: {table_path}"
        ) from exc

    if not isinstance(raw_table, list):
        raise Live2DMasterDataShapeError(
            f"Live2D master-data table '{table_name}' JSON root must be an array: {table_path}"
        )
    return [_normalize_row(table_name, row, row_index) for row_index, row in enumerate(raw_table)]


@dataclass(frozen=True, slots=True)
class LocalMasterDataProvider:
    """Load the six raw Live2D tables from a local master-data directory."""

    root: Path | str
    master_db_version: str

    def __post_init__(self) -> None:
        try:
            root = Path(self.root)
        except TypeError as exc:
            raise Live2DMasterDataFileError("master-data root must be a filesystem path") from exc
        object.__setattr__(self, "root", root)

        if not isinstance(self.master_db_version, str) or not self.master_db_version.strip():
            raise Live2DMasterDataError("master_db_version must be provided as a non-empty string")

    def load_live2d_snapshot(self) -> Live2DMasterDataSnapshot:
        """Read and normalize exactly the six required Live2D table files."""

        root = Path(self.root)
        tables = {table_name: _read_table(root, table_name) for table_name in LIVE2D_TABLE_NAMES}
        return Live2DMasterDataSnapshot(
            master_db_version=self.master_db_version,
            tables=tables,
        )
