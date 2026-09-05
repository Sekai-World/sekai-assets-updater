"""Offline tests for online Live2D master-data preparation."""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from updater.live2d import master_data
from updater.live2d.association import LIVE2D_TABLE_NAMES
from updater.live2d.automatic_selections import (
    Live2DAutomaticSelectionsError,
    build_automatic_live2d_associated_selections,
)
from updater.live2d.master_data import (
    Live2DMasterDataArchiveError,
    LocalMasterDataProvider,
    OnlineMasterDataProvider,
    prepare_online_master_data,
)
from updater.postprocess import dispatch


def _archive_bytes(*, unsafe_name: str | None = None, symlink: bool = False) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        directory = tarfile.TarInfo("sekai-master-main/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        if unsafe_name is not None:
            unsafe = tarfile.TarInfo(unsafe_name)
            unsafe.size = 0
            archive.addfile(unsafe)
        for table_name in LIVE2D_TABLE_NAMES:
            name = f"sekai-master-main/{table_name}.json"
            info = tarfile.TarInfo(name)
            payload = b"[]"
            info.size = len(payload)
            if symlink and table_name == "character2ds":
                info.type = tarfile.SYMTYPE
                info.linkname = "/outside/character2ds.json"
                info.size = 0
                archive.addfile(info)
            else:
                archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


class _ArchiveResponse(io.BytesIO):
    status = 200

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload))}


def test_online_preparation_downloads_one_github_archive_and_locates_tables(
    tmp_path: Path,
) -> None:
    payload = _archive_bytes()
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_urlopen(url: str, **kwargs: object) -> _ArchiveResponse:
        calls.append((url, kwargs))
        return _ArchiveResponse(payload)

    with patch.object(master_data, "urlopen", side_effect=fake_urlopen):
        with prepare_online_master_data("https://github.com/example/sekai-master") as prepared:
            assert prepared.archive_url == (
                "https://codeload.github.com/example/sekai-master/tar.gz/refs/heads/main"
            )
            assert prepared.root.name == "sekai-master-main"
            assert set(prepared.provider.load_live2d_snapshot().tables) == set(LIVE2D_TABLE_NAMES)
            temporary_root = prepared.root.parents[1]
            assert temporary_root.exists()

    assert calls == [
        (
            "https://codeload.github.com/example/sekai-master/tar.gz/refs/heads/main",
            {"timeout": 180.0},
        )
    ]
    assert not temporary_root.exists()


def test_online_provider_prepares_and_downloads_at_most_once() -> None:
    payload = _archive_bytes()

    def fake_urlopen(_url: str, **_kwargs: object) -> _ArchiveResponse:
        return _ArchiveResponse(payload)

    with (
        patch.object(master_data, "urlopen", side_effect=fake_urlopen),
        patch.object(
            master_data, "prepare_online_master_data", wraps=prepare_online_master_data
        ) as prepare,
    ):
        provider = OnlineMasterDataProvider("https://github.com/example/sekai-master")
        first = provider.load_live2d_snapshot()
        second = provider.load_live2d_snapshot()

    assert first is second
    assert prepare.call_count == 1


def test_automatic_dispatch_prepares_one_online_snapshot_off_the_event_loop(
    tmp_path: Path,
) -> None:
    payload = _archive_bytes()
    calls: list[str] = []

    def fake_urlopen(url: str, **_kwargs: object) -> _ArchiveResponse:
        calls.append(url)
        return _ArchiveResponse(payload)

    config = SimpleNamespace(
        LIVE2D_ASSOCIATION_MASTER_DATA_DIR=None,
        LIVE2D_ASSOCIATION_MASTER_DATA_URL="https://github.com/example/sekai-master",
        LIVE2D_ASSOCIATION_MASTER_DATA_BRANCH="main",
        LIVE2D_ASSOCIATION_MASTER_DB_VERSION="local",
        REQUEST_TIMEOUT=30,
    )
    with patch.object(master_data, "urlopen", side_effect=fake_urlopen):
        index = asyncio.run(
            dispatch._build_associated_index_automatically(
                config,
                tmp_path / "output",
                {},
                "asset-v1",
            )
        )

    assert index.master_db_version == "latest:main"
    assert calls == ["https://codeload.github.com/example/sekai-master/tar.gz/refs/heads/main"]


def test_local_master_data_precedes_configured_online_url(tmp_path: Path) -> None:
    master_root = tmp_path / "master"
    master_root.mkdir()
    for table_name in LIVE2D_TABLE_NAMES:
        (master_root / f"{table_name}.json").write_text("[]", encoding="utf-8")

    selections = build_automatic_live2d_associated_selections(
        {},
        output_root=tmp_path / "output",
        master_data_root=master_root,
        master_data_url="https://github.com/example/does-not-download",
    )

    assert isinstance(selections.provider, LocalMasterDataProvider)
    assert selections.provider.root == master_root


def test_missing_automatic_master_data_source_mentions_local_and_online_options(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        Live2DAutomaticSelectionsError,
        match="LIVE2D_ASSOCIATION_MASTER_DATA_DIR.*LIVE2D_ASSOCIATION_MASTER_DATA_URL",
    ):
        build_automatic_live2d_associated_selections(
            {},
            output_root=tmp_path / "output",
        )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"unsafe_name": "../escape"}, "unsafe path"),
        ({"symlink": True}, "symbolic or hard link"),
    ],
)
def test_online_preparation_rejects_unsafe_archive_members(
    kwargs: dict[str, object], message: str
) -> None:
    payload = _archive_bytes(**kwargs)

    def fake_urlopen(_url: str, **_kwargs: object) -> _ArchiveResponse:
        return _ArchiveResponse(payload)

    with patch.object(master_data, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(Live2DMasterDataArchiveError, match=message):
            prepare_online_master_data("https://github.com/example/sekai-master")
