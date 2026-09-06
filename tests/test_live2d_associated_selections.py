"""Focused tests for production Live2D association-selection integration."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from updater.cli import lifecycle, runner
from updater.live2d.association import LIVE2D_TABLE_NAMES
from updater.live2d.automatic_selections import (
    build_automatic_live2d_associated_selections,
    expand_automatic_live2d_model_selections,
)
from updater.live2d.contracts import Live2DIndex
from updater.live2d.index_builder import build_live2d_association_index
from updater.live2d.rollout import (
    Live2DAssociatedRolloutError,
    live2d_associated_namespace_path,
    load_live2d_index,
)
from updater.postprocess import dispatch
from updater.postprocess.live2d_associated_selections import (
    Live2DAssociatedSelectionsError,
    load_live2d_associated_manifest,
    load_live2d_associated_selections,
)


def _write_master_tables(root: Path, *, with_join: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    rows = {table_name: [] for table_name in LIVE2D_TABLE_NAMES}
    if with_join:
        rows["character2ds"] = [{"id": 1, "characterId": 1, "assetName": "model"}]
        rows["costume2ds"] = [{"id": 1, "character2dId": 1, "live2dAssetbundleName": "model"}]
    for table_name, value in rows.items():
        (root / f"{table_name}.json").write_text(json.dumps(value), encoding="utf-8")


def _manifest(master_root: Path, *, model_path: str = "model/selected") -> dict:
    return {
        "schema_version": 1,
        "master_data": {"root": str(master_root), "master_db_version": "master-v1"},
        "model_outputs": [{"id": "model-id", "bundle": "model-key", "output_path": model_path}],
        "motion_sets": [
            {
                "id": "motion-id",
                "bundle": "motion-key",
                "motion_bundle_output_path": "motion/base",
                "motion_output_path": "motion/base/motion",
                "facial_output_path": "motion/base/facial",
            }
        ],
    }


def _bundle_metadata() -> dict[str, dict[str, str]]:
    return {
        "model-key": {"bundleName": "live2d/model/model", "hash": "model-hash"},
        "motion-key": {
            "bundleName": "live2d/motion/v2_1model_motion_base",
            "hash": "motion-hash",
        },
    }


def _write_motion_outputs(root: Path, relative_path: str = "motion/base") -> None:
    motion_bundle = root / relative_path
    motion_bundle.mkdir(parents=True, exist_ok=True)
    (motion_bundle / "BuildMotionData.json").write_text(
        json.dumps({"motions": ["idle"], "expressions": ["smile"]}), encoding="utf-8"
    )
    motion = motion_bundle / "motion"
    facial = motion_bundle / "facial"
    motion.mkdir(exist_ok=True)
    facial.mkdir(exist_ok=True)
    (motion / "idle.motion3.json").write_text("{}", encoding="utf-8")
    (facial / "smile.motion3.json").write_text("{}", encoding="utf-8")


def _write_outputs(root: Path) -> None:
    model = root / "model" / "selected"
    model.mkdir(parents=True, exist_ok=True)
    (model / "selected.model3.json").write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {
                    "Moc": "selected.moc3",
                    "Textures": ["textures/selected.png"],
                    "Physics": "selected.physics3.json",
                },
            }
        ),
        encoding="utf-8",
    )
    (model / "selected.moc3").write_bytes(b"moc")
    (model / "selected.physics3.json").write_bytes(b"physics")
    (model / "textures").mkdir()
    (model / "textures" / "selected.png").write_bytes(b"texture")

    _write_motion_outputs(root)


def test_manifest_motion_readiness_accepts_internal_spaces_and_rejects_unsafe_names(
    tmp_path: Path,
) -> None:
    motion_directory = tmp_path / "motion"
    facial_directory = tmp_path / "facial"
    motion_directory.mkdir()
    facial_directory.mkdir()
    motion_name = "walk fast"
    facial_name = "face_ worry_01"
    (motion_directory / f"{motion_name}.motion3.json").write_text("{}", encoding="utf-8")
    (facial_directory / f"{facial_name}.motion3.json").write_text("{}", encoding="utf-8")

    assert dispatch._manifest_motion_clips_exist(
        {"motions": [motion_name], "expressions": [facial_name]},
        motion_directory,
        facial_directory,
    )
    for unsafe_name in (
        " leading",
        "trailing ",
        ".",
        "..",
        "../escape",
        r"nested\name",
        "name.motion3.json",
        "name.exp3.json",
        "name\x00with-control",
    ):
        assert not dispatch._manifest_motion_clip_name_is_safe(unsafe_name)


def _write_automatic_outputs(
    root: Path,
    motion_relative_path: str = "motion/v2/main/base",
) -> None:
    model = root / "model" / "v1" / "main" / "model"
    model.mkdir(parents=True, exist_ok=True)
    (model / "model.model3.json").write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {
                    "Moc": "model.moc3",
                    "Textures": ["textures/model.png"],
                    "Physics": "model.physics3.json",
                },
            }
        ),
        encoding="utf-8",
    )
    (model / "model.moc3").write_bytes(b"moc")
    (model / "model.physics3.json").write_bytes(b"physics")
    (model / "textures").mkdir()
    (model / "textures" / "model.png").write_bytes(b"texture")

    _write_motion_outputs(root, motion_relative_path)


def _config(root: Path, manifest_path: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        ASSET_LOCAL_EXTRACTED_DIR=root / "extracted",
        LIVE2D_BUNDLE_CACHE_DIR=root / "bundle-cache",
        ASSET_REMOTE_STORAGE=[
            {
                "type": "live2d-associated",
                "base": "remote/live2d",
                "program": "rclone",
                "args": ["copy"],
            }
        ],
        DL_LIST_CACHE_PATH=root / "cache" / "dl.json",
        UNITY_VERSION="2022.3",
        LIVE2D_ASSOCIATION_SELECTIONS_PATH=manifest_path,
    )


def test_manifest_loader_preserves_paths_and_resolves_exact_bundle_keys(tmp_path: Path) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    manifest_path = tmp_path / "selections.json"
    manifest_path.write_text(
        json.dumps(_manifest(master_root, model_path="model/selected")), encoding="utf-8"
    )
    bundles = _bundle_metadata()

    manifest = load_live2d_associated_manifest(manifest_path)
    selections = load_live2d_associated_selections(
        manifest_path,
        output_root=tmp_path / "output",
        live2d_bundles=bundles,
    )

    assert manifest.model_outputs[0].output_path == "model/selected"
    assert selections.provider.root == master_root
    assert selections.provider.master_db_version == "master-v1"
    assert selections.model_outputs[0].model_output_id == "model-id"
    assert selections.model_outputs[0].output_path == "model/selected"
    assert selections.model_outputs[0].bundle is bundles["model-key"]
    assert selections.motion_sets[0].motion_set_id == "motion-id"
    assert selections.motion_sets[0].motion_output_path == "motion/base/motion"
    assert selections.motion_sets[0].facial_output_path == "motion/base/facial"
    assert selections.motion_sets[0].bundle is bundles["motion-key"]


@pytest.mark.parametrize(
    "unsafe_path", ["../escape", "/absolute", "model\\selected", "model//selected"]
)
def test_manifest_loader_rejects_unsafe_output_paths(tmp_path: Path, unsafe_path: str) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    manifest_path = tmp_path / "selections.json"
    manifest_path.write_text(
        json.dumps(_manifest(master_root, model_path=unsafe_path)), encoding="utf-8"
    )

    with pytest.raises(Live2DAssociatedSelectionsError, match="output_path"):
        load_live2d_associated_manifest(manifest_path)


def test_dispatch_builds_manifest_index_and_publishes_to_associated_namespace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extracted" / "live2d"
    _write_outputs(root)
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    manifest_path = tmp_path / "selections.json"
    manifest_path.write_text(json.dumps(_manifest(master_root)), encoding="utf-8")
    config = _config(tmp_path, manifest_path)

    with patch.object(dispatch, "_upload_live2d_associated_projection", new=AsyncMock()) as upload:
        asyncio.run(
            dispatch.run_specialized_postprocess(
                "live2d-associated",
                config,
                live2d_bundles=_bundle_metadata(),
                asset_metadata_version="asset-v1",
            )
        )

    namespace = live2d_associated_namespace_path(config.ASSET_LOCAL_EXTRACTED_DIR)
    index = load_live2d_index(namespace / "index.json")
    assert index is not None
    assert index.metadata_version == "asset-v1"
    assert index.master_db_version == "master-v1"
    assert index.model_outputs[0].output_path == "model/selected"
    assert index.models[0].motion_sets[0].motion_set_id == "motion-id"
    assert not (namespace / "candidates").exists()
    assert not (namespace / "current.json").exists()
    assert not (config.DL_LIST_CACHE_PATH.parent / "live2d_associated_state.json").exists()
    upload.assert_awaited_once()


def test_dispatch_automatically_discovers_current_bundles_without_manifest_or_index(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "extracted" / "live2d"
    _write_automatic_outputs(source_root)
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    config = _config(tmp_path)
    config.LIVE2D_ASSOCIATION_MASTER_DATA_DIR = master_root
    bundles = {
        "model-key": {
            "bundleName": "live2d/model/model",
            "paths": ["StartApp/live2d/model/v1/main/model"],
            "hash": "model-hash",
        },
        "motion-key": {
            "bundleName": "live2d/motion/base",
            "paths": ["StartApp/live2d/motion/v2/main/base"],
            "hash": "motion-hash",
        },
        "ignored-key": {"bundleName": "live2d/other/not-selected", "hash": "ignored"},
    }

    with patch.object(dispatch, "_upload_live2d_associated_projection", new=AsyncMock()) as upload:
        asyncio.run(
            dispatch.run_specialized_postprocess(
                "live2d-associated",
                config,
                live2d_bundles=bundles,
                asset_metadata_version="asset-v1",
            )
        )

    namespace = live2d_associated_namespace_path(config.ASSET_LOCAL_EXTRACTED_DIR)
    index = load_live2d_index(namespace / "index.json")
    assert index is not None
    assert [record.model_bundle.name for record in index.model_outputs] == ["live2d/model/model"]
    assert index.model_outputs[0].output_path == "model/v1/main/model"
    assert [record.motion_bundle.name for record in index.motion_sets] == ["live2d/motion/base"]
    assert index.motion_sets[0].motion_output_path == "motion/v2/main/base/motion"
    assert index.motion_sets[0].facial_output_path == "motion/v2/main/base/facial"
    assert not (tmp_path / "selections.json").exists()
    assert not (namespace / "candidates").exists()
    assert not (namespace / "current.json").exists()
    upload.assert_awaited_once()


def test_automatic_dispatch_recovers_missing_models_before_index_build(tmp_path: Path) -> None:
    source_root = tmp_path / "extracted" / "live2d"
    _write_automatic_outputs(source_root)
    shutil.rmtree(source_root / "model")
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    config = _config(tmp_path)
    config.LIVE2D_ASSOCIATION_MASTER_DATA_DIR = master_root
    bundles = {
        "model-key": {
            "bundleName": "live2d/model/model",
            "paths": ["StartApp/live2d/model/v1/main/model"],
            "hash": "model-hash",
        },
        "motion-key": {
            "bundleName": "live2d/motion/base",
            "paths": ["StartApp/live2d/motion/v2/main/base"],
            "hash": "motion-hash",
        },
    }
    events: list[str] = []

    async def recover_models(*_args, **_kwargs) -> None:
        events.append("recover")
        _write_automatic_outputs(source_root)

    real_build = dispatch.build_live2d_association_index

    def build_index(*args, **kwargs):
        events.append("build")
        return real_build(*args, **kwargs)

    with (
        patch.object(dispatch, "recover_live2d_model_outputs", new=recover_models),
        patch.object(dispatch, "build_live2d_association_index", side_effect=build_index),
        patch.object(dispatch, "_upload_live2d_associated_projection", new=AsyncMock()),
    ):
        asyncio.run(
            dispatch.run_specialized_postprocess(
                "live2d-associated",
                config,
                live2d_bundles=bundles,
                asset_metadata_version="asset-v1",
            )
        )

    assert events == ["recover", "build"]


def test_automatic_dispatch_uses_restored_versioned_motion_path(
    tmp_path: Path,
) -> None:
    versioned_motion_path = "motion/v1/collabo/21_miku/clb01_21miku_motion_base"
    source_root = tmp_path / "extracted" / "live2d"
    _write_automatic_outputs(source_root, versioned_motion_path)
    shutil.rmtree(source_root / "motion")
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    config = _config(tmp_path)
    config.LIVE2D_ASSOCIATION_MASTER_DATA_DIR = master_root
    selected_cache = tmp_path / "bundle-cache" / "live2d" / "motion" / "clb01_21miku_motion_base"
    selected_cache.parent.mkdir(parents=True)
    selected_cache.write_bytes(b"cached-motion")
    bundles = {
        "model-key": {
            "bundleName": "live2d/model/model",
            "paths": ["StartApp/live2d/model/v1/main/model"],
            "hash": "model-hash",
        },
        "motion-key": {
            "bundleName": "live2d/motion/clb01_21miku_motion_base",
            "paths": [
                "StartApp/live2d/motion/clb01_21miku_motion_base",
                "StartApp/live2d/" + versioned_motion_path,
            ],
            "hash": "motion-hash",
        },
    }

    async def restore_selected(*_args, **kwargs) -> None:
        assert kwargs["bundle_paths"] == [selected_cache]
        _write_motion_outputs(source_root, versioned_motion_path)

    with (
        patch.object(dispatch, "collect_param_id_map", new=AsyncMock(return_value={})),
        patch.object(
            dispatch,
            "restore_live2d_motions",
            new=AsyncMock(side_effect=restore_selected),
        ) as restore,
        patch.object(dispatch, "_upload_live2d_associated_projection", new=AsyncMock()),
    ):
        asyncio.run(
            dispatch.run_specialized_postprocess(
                "live2d-associated",
                config,
                live2d_bundles=bundles,
                asset_metadata_version="asset-v1",
            )
        )

    namespace = live2d_associated_namespace_path(config.ASSET_LOCAL_EXTRACTED_DIR)
    index = load_live2d_index(namespace / "index.json")
    assert index is not None
    restore.assert_awaited_once()
    assert index.motion_sets[0].motion_output_path == f"{versioned_motion_path}/motion"
    assert index.motion_sets[0].facial_output_path == f"{versioned_motion_path}/facial"


def test_associated_dispatch_restores_indexed_versioned_motion_bundle(
    tmp_path: Path,
) -> None:
    versioned_motion_path = "motion/v1/collabo/21_miku/clb01_21miku_motion_base"
    source_root = tmp_path / "extracted" / "live2d"
    _write_automatic_outputs(source_root, versioned_motion_path)
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    config = _config(tmp_path)
    selected_cache = tmp_path / "bundle-cache" / "live2d" / "motion" / "clb01_21miku_motion_base"
    selected_cache.parent.mkdir(parents=True)
    selected_cache.write_bytes(b"cached-motion")
    bundles = {
        "model-key": {
            "bundleName": "live2d/model/model",
            "paths": ["StartApp/live2d/model/v1/main/model"],
            "hash": "model-hash",
        },
        "motion-key": {
            "bundleName": "live2d/motion/clb01_21miku_motion_base",
            "paths": [
                "StartApp/live2d/motion/clb01_21miku_motion_base",
                "StartApp/live2d/" + versioned_motion_path,
            ],
            "hash": "motion-hash",
        },
    }
    selections = build_automatic_live2d_associated_selections(
        bundles,
        output_root=source_root,
        master_data_root=master_root,
    )
    selections = expand_automatic_live2d_model_selections(selections)
    index = build_live2d_association_index(
        provider=selections.provider,
        metadata_version="asset-v1",
        model_outputs=selections.model_outputs,
        motion_sets=selections.motion_sets,
    )
    shutil.rmtree(source_root / "motion")

    async def restore_selected(*_args, **kwargs) -> None:
        assert kwargs["bundle_paths"] == [selected_cache]
        _write_motion_outputs(source_root, versioned_motion_path)

    with (
        patch.object(dispatch, "collect_param_id_map", new=AsyncMock(return_value={})),
        patch.object(
            dispatch,
            "restore_live2d_motions",
            new=AsyncMock(side_effect=restore_selected),
        ) as restore,
    ):
        asyncio.run(dispatch._ensure_associated_motion_outputs(config, index, source_root))

    restore.assert_awaited_once()
    assert (source_root / versioned_motion_path / "motion" / "idle.motion3.json").is_file()
    assert not (source_root / "motion" / "clb01_21miku_motion_base").exists()


def test_cold_manifest_dispatch_restores_selected_motion_before_build_and_publish(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "extracted" / "live2d"
    _write_outputs(source_root)
    shutil.rmtree(source_root / "motion")
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    manifest_path = tmp_path / "selections.json"
    manifest_path.write_text(json.dumps(_manifest(master_root)), encoding="utf-8")
    config = _config(tmp_path, manifest_path)
    selected_cache = tmp_path / "bundle-cache" / "live2d" / "motion" / "v2_1model_motion_base"
    selected_cache.parent.mkdir(parents=True)
    selected_cache.write_bytes(b"cached-motion")
    events: list[str] = []

    async def restore_selected(*_args, **kwargs) -> None:
        events.append("restore")
        assert kwargs["bundle_paths"] == [selected_cache]
        _write_motion_outputs(source_root)

    real_build = dispatch.build_live2d_association_index

    def build_index(*args, **kwargs):
        events.append("build")
        return real_build(*args, **kwargs)

    real_publish = dispatch.publish_latest_associated_index

    def publish_index(*args, **kwargs):
        events.append("publish")
        return real_publish(*args, **kwargs)

    with (
        patch.object(dispatch, "collect_param_id_map", new=AsyncMock(return_value={})),
        patch.object(
            dispatch,
            "restore_live2d_motions",
            new=AsyncMock(side_effect=restore_selected),
        ) as restore,
        patch.object(dispatch, "build_live2d_association_index", side_effect=build_index),
        patch.object(dispatch, "publish_latest_associated_index", side_effect=publish_index),
        patch.object(dispatch, "_upload_live2d_associated_projection", new=AsyncMock()),
    ):
        asyncio.run(
            dispatch.run_specialized_postprocess(
                "live2d-associated",
                config,
                live2d_bundles=_bundle_metadata(),
                asset_metadata_version="asset-v1",
            )
        )

    assert events == ["restore", "build", "publish"]
    restore.assert_awaited_once()


def test_explicit_prebuilt_index_path_takes_precedence_over_manifest(tmp_path: Path) -> None:
    from tests.test_live2d_rollout import materialize_outputs, publishable_index_data

    index = Live2DIndex.from_dict(publishable_index_data())
    source_root = tmp_path / "prebuilt-live2d"
    materialize_outputs(source_root, index)
    index_path = tmp_path / "prebuilt-index.json"
    index_path.write_bytes(index.canonical_json_bytes())
    config = _config(tmp_path, tmp_path / "manifest-does-not-exist.json")

    with patch.object(dispatch, "_upload_live2d_associated_projection", new=AsyncMock()):
        asyncio.run(
            dispatch.run_specialized_postprocess(
                "live2d-associated",
                config,
                association_index_path=index_path,
                association_output_root=source_root,
                live2d_bundles=None,
            )
        )

    namespace = live2d_associated_namespace_path(config.ASSET_LOCAL_EXTRACTED_DIR)
    assert load_live2d_index(namespace / "index.json") == index
    assert not (namespace / "candidates").exists()
    assert not (namespace / "current.json").exists()


@pytest.mark.parametrize("skip_missing_sources", [False, True])
def test_manifest_build_errors_follow_forced_or_optional_policy(
    tmp_path: Path, skip_missing_sources: bool
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root, with_join=False)
    manifest_path = tmp_path / "selections.json"
    manifest_path.write_text(json.dumps(_manifest(master_root)), encoding="utf-8")
    config = _config(tmp_path, manifest_path)

    call = dispatch.run_specialized_postprocess(
        "live2d-associated",
        config,
        live2d_bundles={"model-key": _bundle_metadata()["model-key"]},
        asset_metadata_version="asset-v1",
        skip_missing_sources=skip_missing_sources,
    )
    if skip_missing_sources:
        asyncio.run(call)
    else:
        with pytest.raises(Live2DAssociatedRolloutError, match="index preparation"):
            asyncio.run(call)


@pytest.mark.parametrize("skip_missing_sources", [False, True])
def test_manifest_missing_selected_motion_cache_follows_forced_or_optional_policy(
    tmp_path: Path, skip_missing_sources: bool
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    manifest_path = tmp_path / "selections.json"
    manifest_path.write_text(json.dumps(_manifest(master_root)), encoding="utf-8")
    config = _config(tmp_path, manifest_path)

    call = dispatch.run_specialized_postprocess(
        "live2d-associated",
        config,
        live2d_bundles=_bundle_metadata(),
        asset_metadata_version="asset-v1",
        skip_missing_sources=skip_missing_sources,
    )
    if skip_missing_sources:
        asyncio.run(call)
    else:
        with pytest.raises(
            Live2DAssociatedRolloutError,
            match="selected Live2D motion bundle cache is missing",
        ):
            asyncio.run(call)


def test_missing_master_data_is_mapped_to_associated_failure_policy(tmp_path: Path) -> None:
    manifest_path = tmp_path / "selections.json"
    manifest_path.write_text(json.dumps(_manifest(tmp_path / "missing-master")), encoding="utf-8")
    config = _config(tmp_path, manifest_path)
    bundles = _bundle_metadata()

    asyncio.run(
        dispatch.run_specialized_postprocess(
            "live2d-associated",
            config,
            live2d_bundles=bundles,
            asset_metadata_version="asset-v1",
            skip_missing_sources=True,
        )
    )
    call = dispatch.run_specialized_postprocess(
        "live2d-associated",
        config,
        live2d_bundles=bundles,
        asset_metadata_version="asset-v1",
    )
    with pytest.raises(Live2DAssociatedRolloutError, match="index preparation"):
        asyncio.run(call)


def test_lifecycle_propagates_asset_metadata_version_to_associated_dispatch() -> None:
    config = SimpleNamespace(
        ENABLE_LIVE2D_POSTPROCESS=False,
        ENABLE_LIVE2D_ASSOCIATED_PIPELINE=True,
        ENABLE_CHARTS_POSTPROCESS=False,
        DL_INCLUDE_LIST=[],
    )
    bundles = {"model-key": {"bundleName": "live2d/model/model", "hash": "hash"}}

    with (
        patch.object(lifecycle, "recover_live2d_model_outputs", new=AsyncMock()),
        patch.object(lifecycle, "run_specialized_postprocess", new=AsyncMock()) as process,
    ):
        asyncio.run(
            lifecycle._run_enabled_specialized_postprocess(
                "assets",
                config,
                False,
                bundles,
                asset_metadata_version="asset-v1",
            )
        )

    assert process.await_args is not None
    assert process.await_args.kwargs["asset_metadata_version"] == "asset-v1"
    assert process.await_args.kwargs["live2d_bundles"] is bundles


def test_runner_passes_asset_metadata_version_from_fetched_metadata(tmp_path: Path) -> None:
    config = SimpleNamespace()
    fetch_result = SimpleNamespace(
        asset_bundle_info={"version": " asset-v1 ", "bundles": {}},
        game_version_json={},
        asset_ver=None,
        assetbundle_host_hash=None,
    )

    async def fake_build(*_args, **_kwargs):
        return [], object()

    async def fake_pending(*_args, **_kwargs):
        return [], []

    with (
        patch.object(runner, "_build_new_download_list", new=fake_build),
        patch.object(runner, "_load_pending_download_lists", new=fake_pending),
        patch.object(runner, "_complete_with_empty_download_list", new=AsyncMock()) as complete,
    ):
        asyncio.run(
            runner._run_full_download_pipeline(
                config,
                "assets",
                False,
                False,
                (),
                fetch_result,
                0,
            )
        )

    assert complete.await_args is not None
    assert complete.await_args.kwargs["asset_metadata_version"] == "asset-v1"
