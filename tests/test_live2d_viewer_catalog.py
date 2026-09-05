"""Focused tests for the public Live2D viewer projection."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_live2d_rollout import (
    associated_storage,
    materialize_outputs,
    publishable_index_data,
)
from updater.live2d.viewer_catalog import (
    Live2DViewerCatalogError,
    build_viewer_catalog,
)
from updater.postprocess import dispatch


def test_public_catalog_has_only_viewer_fields_and_resolves_model3_and_motion_paths(
    tmp_path: Path,
) -> None:
    from updater.live2d.contracts import Live2DIndex

    index = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, index)

    catalog = build_viewer_catalog(index, source)

    assert catalog
    assert [entry["modelPath"] for entry in catalog] == sorted(
        entry["modelPath"] for entry in catalog
    )
    for entry in catalog:
        assert set(entry) == {"modelName", "modelBase", "modelPath", "modelFile", "motionSets"}
        model_file = source / entry["modelPath"] / entry["modelFile"]
        assert model_file.is_file()
        assert entry["modelFile"].endswith(".model3.json")
        for motion_set in entry["motionSets"]:
            assert set(motion_set) == {
                "motionSetId",
                "motionPath",
                "motionFiles",
                "facialPath",
                "facialFiles",
            }
            assert all(
                (source / motion_set["motionPath"] / filename).is_file()
                for filename in motion_set["motionFiles"]
            )
            assert all(
                (source / motion_set["facialPath"] / filename).is_file()
                for filename in motion_set["facialFiles"]
            )
            assert not any(
                key in motion_set
                for key in ("status", "evidence", "rule_code", "checksum", "diagnostics")
            )


@pytest.mark.parametrize("model3_count", [0, 2])
def test_catalog_requires_exactly_one_regular_model3_document(
    tmp_path: Path, model3_count: int
) -> None:
    from updater.live2d.contracts import Live2DIndex

    index = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, index)
    model_directory = source / index.model_outputs[0].output_path
    model_files = sorted(model_directory.glob("*.model3.json"))
    if model3_count == 0:
        model_files[0].unlink()
    else:
        (model_directory / "extra.model3.json").write_text("{}", encoding="utf-8")

    with pytest.raises(Live2DViewerCatalogError, match="expected exactly one regular"):
        build_viewer_catalog(index, source)


def test_upload_stages_only_assets_and_model_list_last_without_touching_legacy_list(
    tmp_path: Path,
) -> None:
    from updater.live2d.contracts import Live2DIndex

    index = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, index)
    legacy_model_list = source / "model_list.json"
    legacy_model_list.write_bytes(b"legacy-authoritative\n")
    uploads: list[tuple[str, list[str], list[str]]] = []

    async def capture_upload(source_path, remote_path, *_args, **_kwargs) -> None:
        staged = Path(str(source_path))
        files = sorted(
            path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file()
        )
        uploads.append((str(remote_path), files, [path.name for path in staged.iterdir()]))

    with patch.object(dispatch, "upload_directory", new=AsyncMock(side_effect=capture_upload)):
        asyncio.run(
            dispatch._upload_live2d_associated_projection(
                index,
                source,
                associated_storage(),
                SimpleNamespace(),
            )
        )

    assert uploads
    assert uploads[-1][0] == "remote/live2d/live2d-associated/v1"
    assert uploads[-1][1] == ["model_list.json"]
    assert uploads[-1][2] == ["model_list.json"]
    assert legacy_model_list.read_bytes() == b"legacy-authoritative\n"
    for remote_path, files, _entries in uploads[:-1]:
        assert "/candidates/" not in remote_path
        assert "/revisions/" not in remote_path
        assert remote_path.startswith("remote/live2d/live2d-associated/v1/")
        assert all(
            audit_name not in files
            for audit_name in (
                "index.json",
                "current.json",
                "candidate.json",
                "viewer-catalog.json",
            )
        )
        assert all("candidate" not in path.split("/") for path in files)
