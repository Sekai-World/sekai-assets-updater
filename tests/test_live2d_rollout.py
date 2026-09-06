"""Transactional tests for the isolated Live2D association rollout."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from updater.cli import lifecycle
from updater.live2d.contracts import Live2DIndex
from updater.live2d.rollout import (
    LIVE2D_ASSOCIATED_NAMESPACE,
    Live2DAssociatedRolloutError,
    canonical_index_bytes,
    canonical_index_json_bytes,
    compare_live2d_indices,
    disable_live2d_associated,
    live2d_associated_namespace_path,
    live2d_associated_state_path,
    load_current_index,
    load_current_pointer,
    load_live2d_index,
    load_rollout_state,
    publish_candidate,
    rollback_live2d_associated,
)
from updater.modes import (
    filter_bundles_for_mode,
    get_enabled_specialized_modes,
    get_required_bundle_prefixes,
    mode_uses_bundle_pipeline,
)
from updater.postprocess import dispatch

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "live2d" / "contracts_6.8.0.10.json"


def fixture_data() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def publishable_index_data() -> dict:
    data = fixture_data()
    for model in data["models"]:
        for candidate in model["motion_sets"]:
            candidate["status"] = "derived"
    return data


def materialize_outputs(root: Path, index: Live2DIndex) -> None:
    for record in index.model_outputs:
        directory = root / record.output_path
        directory.mkdir(parents=True, exist_ok=True)
        model3_relative = (
            Path(record.model3_path)
            if record.model3_path is not None
            else Path(f"{directory.name}.model3.json")
        )
        model3_path = directory / model3_relative
        model3_path.parent.mkdir(parents=True, exist_ok=True)
        model3_path.write_text(
            json.dumps({"Version": 3, "FileReferences": {"Moc": record.file_references.moc}}),
            encoding="utf-8",
        )
        reference_directory = model3_path.parent
        references = record.file_references
        for relative in (
            references.moc,
            *references.textures,
            *((references.physics,) if references.physics is not None else ()),
        ):
            path = reference_directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode("utf-8"))
    for record in index.motion_sets:
        motion = root / record.motion_output_path
        facial = root / record.facial_output_path
        motion.mkdir(parents=True, exist_ok=True)
        facial.mkdir(parents=True, exist_ok=True)
        for clip in record.known_clips.motions:
            (motion / f"{clip}.motion3.json").write_bytes(b"motion")
        for clip in record.known_clips.facials:
            (facial / f"{clip}.motion3.json").write_bytes(b"facial")


def materialize_motion_outputs(root: Path, index: Live2DIndex) -> None:
    for record in index.motion_sets:
        motion = root / record.motion_output_path
        facial = root / record.facial_output_path
        motion.mkdir(parents=True, exist_ok=True)
        facial.mkdir(parents=True, exist_ok=True)
        for clip in record.known_clips.motions:
            (motion / f"{clip}.motion3.json").write_bytes(b"motion")
        for clip in record.known_clips.facials:
            (facial / f"{clip}.motion3.json").write_bytes(b"facial")


def associated_storage(base: str = "remote/live2d") -> dict[str, object]:
    return {
        "type": "live2d-associated",
        "base": base,
        "program": "rclone",
        "args": ["copy"],
    }


def associated_config(
    tmp_path: Path, storages: list[dict[str, object]] | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        ASSET_LOCAL_EXTRACTED_DIR=tmp_path / "extracted",
        LIVE2D_BUNDLE_CACHE_DIR=tmp_path / "bundle-cache",
        ASSET_REMOTE_STORAGE=storages if storages is not None else [associated_storage()],
        DL_LIST_CACHE_PATH=tmp_path / "cache" / "dl.json",
        UNITY_VERSION="6.8.0.10",
    )


def test_associated_flag_and_mode_select_the_live2d_scope_independently() -> None:
    config = SimpleNamespace(
        ENABLE_LIVE2D_POSTPROCESS=True,
        ENABLE_LIVE2D_ASSOCIATED_PIPELINE=True,
        ENABLE_CHARTS_POSTPROCESS=False,
    )
    bundles = {
        "model": {"bundleName": "live2d/model/example"},
        "motion": {"bundleName": "live2d/motion/example"},
        "other": {"bundleName": "music/example"},
    }

    assert get_enabled_specialized_modes("assets", config) == (
        "live2d",
        "live2d-associated",
    )
    assert mode_uses_bundle_pipeline("live2d-associated")
    assert get_required_bundle_prefixes("live2d-associated", config) == ("live2d/",)
    assert filter_bundles_for_mode(bundles, "live2d-associated") == {
        "model": bundles["model"],
        "motion": bundles["motion"],
    }


def test_namespace_state_and_canonical_serialization_are_versioned(tmp_path: Path) -> None:
    index = Live2DIndex.from_dict(publishable_index_data())
    config = SimpleNamespace(DL_LIST_CACHE_PATH=tmp_path / "cache" / "dl.json")

    assert live2d_associated_namespace_path(tmp_path) == (tmp_path / "live2d-associated" / "v1")
    assert LIVE2D_ASSOCIATED_NAMESPACE == "live2d-associated/v1"
    assert live2d_associated_state_path(config) == (
        tmp_path / "cache" / "live2d_associated_state.json"
    )
    dotted_root = tmp_path / "cache.v1"
    assert live2d_associated_state_path(dotted_root) == (
        dotted_root / "live2d_associated_state.json"
    )
    assert canonical_index_json_bytes(index) == canonical_index_json_bytes(
        Live2DIndex.from_json_bytes(canonical_index_bytes(index))
    )


def test_publish_copies_only_referenced_outputs_and_writes_atomic_current(
    tmp_path: Path,
) -> None:
    index = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, index)
    model_list = source / "model_list.json"
    model_list.write_bytes(b"legacy-authoritative\n")
    namespace = live2d_associated_namespace_path(tmp_path)
    state_path = tmp_path / "cache" / "live2d_associated_state.json"

    pointer = publish_candidate(index, source, namespace, state_path=state_path)

    assert not (namespace / "current").exists()
    assert (namespace / "current.json").is_file()
    assert not (namespace / "current.json").is_symlink()
    assert load_current_index(namespace) == index
    assert (namespace / "index.json").read_bytes() == canonical_index_json_bytes(index)
    assert json.loads((namespace / "current.json").read_bytes())["index_sha256"]
    assert model_list.read_bytes() == b"legacy-authoritative\n"
    assert list((namespace / "candidates" / pointer.candidate_id).rglob("*.model3.json"))
    assert not list((namespace / "candidates" / pointer.candidate_id).rglob("model_list.json"))
    assert not (namespace / "candidates" / pointer.candidate_id / "current.json").exists()
    assert load_rollout_state(state_path).current == pointer  # type: ignore[union-attr]


def test_publish_uses_declared_model3_parent_for_nested_references(tmp_path: Path) -> None:
    data = publishable_index_data()
    selected_model = next(
        model for model in data["model_outputs"] if model["model_output_id"] == "ichika-april2025"
    )
    selected_model["model3_path"] = "nested/selected.model3.json"
    selected_model["schema_version"] = 2
    index = Live2DIndex.from_dict(data)
    source = tmp_path / "live2d"
    materialize_outputs(source, index)

    record = next(record for record in index.model_outputs if record.model3_path is not None)
    output_directory = source / record.output_path
    (output_directory / "legacy.model3.json").write_text(
        json.dumps({"Version": 3, "FileReferences": {"Moc": "wrong.moc3"}}),
        encoding="utf-8",
    )
    namespace = live2d_associated_namespace_path(tmp_path)

    pointer = publish_candidate(index, source, namespace)
    candidate = namespace / "candidates" / pointer.candidate_id
    nested = candidate / record.output_path / "nested"

    assert (nested / "selected.model3.json").is_file()
    assert (nested / record.file_references.moc).is_file()
    assert not (candidate / record.output_path / "legacy.model3.json").exists()
    assert not (candidate / record.output_path / record.file_references.moc).exists()


def test_publish_copies_two_model_records_sharing_one_output_path(tmp_path: Path) -> None:
    data = publishable_index_data()
    shared_models = {
        "ichika-unit": ("first.model3.json", "first.moc3"),
        "ichika-april2025": ("second.model3.json", "second.moc3"),
    }
    for model in data["model_outputs"]:
        model_output_id = model["model_output_id"]
        if model_output_id not in shared_models:
            continue
        model3_name, moc_name = shared_models[model_output_id]
        model["output_path"] = "model/shared"
        model["model3_path"] = model3_name
        model["schema_version"] = 2
        model["file_references"] = {"Moc": moc_name, "Textures": []}

    index = Live2DIndex.from_dict(data)
    source = tmp_path / "live2d"
    materialize_outputs(source, index)
    namespace = live2d_associated_namespace_path(tmp_path)

    pointer = publish_candidate(index, source, namespace)
    candidate = namespace / "candidates" / pointer.candidate_id / "model" / "shared"

    assert (candidate / "first.model3.json").is_file()
    assert (candidate / "first.moc3").is_file()
    assert (candidate / "second.model3.json").is_file()
    assert (candidate / "second.moc3").is_file()


def test_ambiguous_or_empty_index_is_rejected_before_namespace_mutation(tmp_path: Path) -> None:
    source = tmp_path / "live2d"
    ambiguous = Live2DIndex.from_dict(fixture_data())
    materialize_outputs(source, ambiguous)
    namespace = live2d_associated_namespace_path(tmp_path)
    empty = Live2DIndex(index_version=1, metadata_version="v", master_db_version="v")

    with pytest.raises(Live2DAssociatedRolloutError, match="ambiguous"):
        publish_candidate(ambiguous, source, namespace)
    assert not namespace.exists()

    with pytest.raises(Live2DAssociatedRolloutError, match="empty"):
        publish_candidate(empty, source, namespace)
    assert not namespace.exists()


def test_failed_pointer_replacement_preserves_current_index_state_and_legacy_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import updater.live2d.rollout as rollout

    first = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, first)
    legacy = source / "model_list.json"
    legacy.write_bytes(b"legacy\n")
    namespace = live2d_associated_namespace_path(tmp_path)
    state_path = tmp_path / "cache" / "live2d_associated_state.json"
    first_pointer = publish_candidate(first, source, namespace, state_path=state_path)
    old_index = (namespace / "index.json").read_bytes()
    old_state = state_path.read_bytes()

    changed_data = publishable_index_data()
    checksum = "sha256:" + ("9" * 64)
    changed_data["model_outputs"][0]["model_bundle"]["checksum"] = checksum
    changed_data["models"][0]["model_bundle"]["checksum"] = checksum
    second = Live2DIndex.from_dict(changed_data)

    old_current = (namespace / "current.json").read_bytes()

    def fail_current(_namespace, _pointer):
        raise OSError("injected current-pointer failure")

    monkeypatch.setattr(rollout, "_atomic_current_file", fail_current)
    with pytest.raises(Live2DAssociatedRolloutError, match="publication failed"):
        publish_candidate(second, source, namespace, state_path=state_path)

    assert load_current_pointer(namespace) == first_pointer
    assert (namespace / "index.json").read_bytes() == old_index
    assert (namespace / "current.json").read_bytes() == old_current
    assert state_path.read_bytes() == old_state
    assert legacy.read_bytes() == b"legacy\n"


def test_changed_keys_and_rollback_disable_are_isolated(tmp_path: Path) -> None:
    first_data = publishable_index_data()
    first = Live2DIndex.from_dict(first_data)
    source = tmp_path / "live2d"
    materialize_outputs(source, first)
    namespace = live2d_associated_namespace_path(tmp_path)
    state_path = tmp_path / "cache" / "live2d_associated_state.json"
    first_pointer = publish_candidate(first, source, namespace, state_path=state_path)

    changed_data = copy.deepcopy(first_data)
    changed_checksum = "sha256:" + ("8" * 64)
    changed_data["motion_sets"][0]["motion_bundle"]["checksum"] = changed_checksum
    for model in changed_data["models"]:
        for candidate in model["motion_sets"]:
            if candidate["motion_set_id"] == changed_data["motion_sets"][0]["motion_set_id"]:
                candidate["motion_bundle"]["checksum"] = changed_checksum
    changed = Live2DIndex.from_dict(changed_data)
    second_pointer = publish_candidate(
        changed,
        source,
        namespace,
        state_path=state_path,
        candidate_id="second",
    )
    comparison = compare_live2d_indices(first, changed)
    assert not comparison.unchanged
    assert "motion_set:ichika-base" in comparison.changed_keys
    assert second_pointer.candidate_id == "second"

    assert (
        rollback_live2d_associated(namespace, first_pointer.candidate_id, state_path=state_path)
        == first_pointer
    )
    assert load_current_pointer(namespace) == first_pointer
    disable_live2d_associated(namespace, state_path=state_path)
    assert load_current_pointer(namespace) is None
    assert not (namespace / "index.json").exists()
    assert (namespace / "candidates" / first_pointer.candidate_id).is_dir()
    assert load_rollout_state(state_path).enabled is False  # type: ignore[union-attr]


def test_associated_dispatch_accepts_explicit_mapping_without_calling_legacy_processor(
    tmp_path: Path,
) -> None:
    index = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, index)
    legacy_model_list = source / "model_list.json"
    legacy_model_list.write_bytes(b"legacy\n")
    config = SimpleNamespace(
        ASSET_LOCAL_EXTRACTED_DIR=tmp_path / "extracted",
        LIVE2D_BUNDLE_CACHE_DIR=tmp_path / "bundle-cache",
        ASSET_REMOTE_STORAGE=[associated_storage()],
        DL_LIST_CACHE_PATH=tmp_path / "cache" / "dl.json",
    )

    from unittest.mock import AsyncMock, patch

    with (
        patch.object(dispatch, "_process_live2d", new=AsyncMock()) as legacy,
        patch.object(dispatch, "_upload_live2d_associated_projection", new=AsyncMock()) as upload,
    ):
        asyncio.run(
            dispatch.run_specialized_postprocess(
                "live2d-associated",
                config,
                association_index=index.to_dict(),
                association_output_root=source,
                association_namespace_root=live2d_associated_namespace_path(tmp_path),
            )
        )

    legacy.assert_not_awaited()
    upload.assert_awaited_once()
    assert upload.await_args.args[0] == index
    assert Path(str(upload.await_args.args[1])) == source
    assert legacy_model_list.read_bytes() == b"legacy\n"
    namespace = live2d_associated_namespace_path(tmp_path)
    assert load_live2d_index(namespace / "index.json") == index
    assert not (namespace / "candidates").exists()
    assert not (namespace / "current.json").exists()
    assert not (config.DL_LIST_CACHE_PATH.parent / "live2d_associated_state.json").exists()


def test_forced_associated_dispatch_rejects_missing_explicit_index(tmp_path: Path) -> None:
    config = SimpleNamespace(
        ASSET_LOCAL_EXTRACTED_DIR=tmp_path / "extracted",
        LIVE2D_BUNDLE_CACHE_DIR=tmp_path / "bundle-cache",
        ASSET_REMOTE_STORAGE=[associated_storage()],
    )
    call = dispatch.run_specialized_postprocess("live2d-associated", config)
    with pytest.raises(ValueError, match="explicit validated association index"):
        asyncio.run(call)


def test_standalone_associated_dispatch_restores_missing_motions_once(tmp_path: Path) -> None:
    index = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, index)
    for path in source.rglob("*.motion3.json"):
        path.unlink()
    motion_cache = tmp_path / "bundle-cache" / "live2d" / "motion"
    motion_cache.mkdir(parents=True)
    (motion_cache / "motion.bundle").write_bytes(b"cached-motion")
    config = associated_config(tmp_path)

    async def restore_missing_motions(*_args, **_kwargs) -> None:
        materialize_motion_outputs(source, index)

    from unittest.mock import AsyncMock, patch

    with (
        patch.object(
            dispatch,
            "collect_param_id_map",
            new=AsyncMock(return_value={"A": "B"}),
        ) as collect,
        patch.object(
            dispatch,
            "restore_live2d_motions",
            new=AsyncMock(side_effect=restore_missing_motions),
        ) as restore,
        patch.object(dispatch, "_upload_live2d_associated_projection", new=AsyncMock()) as upload,
    ):
        asyncio.run(
            dispatch.run_specialized_postprocess(
                "live2d-associated",
                config,
                association_index=index,
                association_output_root=source,
                association_namespace_root=live2d_associated_namespace_path(tmp_path),
            )
        )

    collect.assert_awaited_once()
    restore.assert_awaited_once()
    assert restore.await_args.kwargs["param_id_map"] == {"A": "B"}
    upload.assert_awaited_once()
    namespace = live2d_associated_namespace_path(tmp_path)
    assert load_live2d_index(namespace / "index.json") == index
    assert not (namespace / "candidates").exists()
    assert not (namespace / "current.json").exists()


def test_assets_new_flag_only_recovers_models_and_runs_associated_mode(tmp_path: Path) -> None:
    config = SimpleNamespace(
        ENABLE_LIVE2D_POSTPROCESS=False,
        ENABLE_LIVE2D_ASSOCIATED_PIPELINE=True,
        ENABLE_CHARTS_POSTPROCESS=False,
        DL_INCLUDE_LIST=[],
    )
    bundles = {"model": {"bundleName": "live2d/model/example"}}
    from unittest.mock import AsyncMock, patch

    with (
        patch.object(lifecycle, "recover_live2d_model_outputs", new=AsyncMock()) as recover,
        patch.object(lifecycle, "run_specialized_postprocess", new=AsyncMock()) as process,
    ):
        asyncio.run(
            lifecycle._run_enabled_specialized_postprocess("assets", config, False, bundles)
        )

    recover.assert_awaited_once_with(config, bundles)
    process.assert_awaited_once()
    assert process.await_args.args[0] == "live2d-associated"
    assert process.await_args.kwargs["motion_outputs_ready"] is False


def test_assets_both_flags_recover_once_and_do_not_repeat_motion_restore(tmp_path: Path) -> None:
    config = SimpleNamespace(
        ENABLE_LIVE2D_POSTPROCESS=True,
        ENABLE_LIVE2D_ASSOCIATED_PIPELINE=True,
        ENABLE_CHARTS_POSTPROCESS=False,
        DL_INCLUDE_LIST=[],
    )
    bundles = {"model": {"bundleName": "live2d/model/example"}}
    from unittest.mock import AsyncMock, patch

    with (
        patch.object(lifecycle, "recover_live2d_model_outputs", new=AsyncMock()) as recover,
        patch.object(
            lifecycle,
            "run_specialized_postprocess",
            new=AsyncMock(side_effect=[True, None]),
        ) as process,
    ):
        asyncio.run(
            lifecycle._run_enabled_specialized_postprocess("assets", config, False, bundles)
        )

    recover.assert_awaited_once_with(config, bundles)
    assert [call.args[0] for call in process.await_args_list] == [
        "live2d",
        "live2d-associated",
    ]
    assert process.await_args_list[1].kwargs["motion_outputs_ready"] is True


def test_associated_pipeline_without_storage_refuses_local_only_publish(tmp_path: Path) -> None:
    index = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, index)
    config = associated_config(tmp_path, [])
    namespace = live2d_associated_namespace_path(tmp_path)

    call = dispatch.run_specialized_postprocess(
        "live2d-associated",
        config,
        association_index=index,
        association_output_root=source,
        association_namespace_root=namespace,
        skip_missing_sources=True,
    )
    with pytest.raises(ValueError, match="no matching live2d-associated storage"):
        asyncio.run(call)
    assert not namespace.exists()


def test_assets_upload_failure_is_not_swallowed_and_leaves_no_local_audit(
    tmp_path: Path,
) -> None:
    index = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, index)
    config = associated_config(
        tmp_path,
        [associated_storage("remote/one"), associated_storage("remote/two")],
    )
    namespace = live2d_associated_namespace_path(tmp_path)
    from unittest.mock import AsyncMock, patch

    upload_error = RuntimeError("second remote upload failed")
    with patch.object(
        dispatch,
        "_upload_live2d_associated_projection",
        new=AsyncMock(side_effect=[None, upload_error]),
    ) as upload:
        call = dispatch.run_specialized_postprocess(
            "live2d-associated",
            config,
            association_index=index,
            association_output_root=source,
            association_namespace_root=namespace,
            skip_missing_sources=True,
        )
        with pytest.raises(RuntimeError, match="second remote upload failed"):
            asyncio.run(call)

    assert upload.await_count == 2
    assert not (namespace / "candidates").exists()
    assert not (namespace / "current.json").exists()
    assert not (namespace / "index.json").exists()
    assert not (config.DL_LIST_CACHE_PATH.parent / "live2d_associated_state.json").exists()


def test_upload_failure_does_not_create_local_rollout_history(tmp_path: Path) -> None:
    index = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, index)
    namespace = live2d_associated_namespace_path(tmp_path)
    state_path = tmp_path / "cache" / "live2d_associated_state.json"
    config = associated_config(tmp_path)
    from unittest.mock import AsyncMock, patch

    with patch.object(
        dispatch,
        "_upload_live2d_associated_projection",
        new=AsyncMock(side_effect=RuntimeError("upload failed")),
    ):
        call = dispatch.run_specialized_postprocess(
            "live2d-associated",
            config,
            association_index=index,
            association_output_root=source,
            association_namespace_root=namespace,
            association_state_path=state_path,
            skip_missing_sources=True,
        )
        with pytest.raises(RuntimeError, match="upload failed"):
            asyncio.run(call)
    assert not namespace.exists()
    assert not state_path.exists()


def test_associated_dispatch_always_uploads_latest_projection_to_each_storage(
    tmp_path: Path,
) -> None:
    index = Live2DIndex.from_dict(publishable_index_data())
    source = tmp_path / "live2d"
    materialize_outputs(source, index)
    namespace = live2d_associated_namespace_path(tmp_path)
    config = associated_config(tmp_path)
    from unittest.mock import AsyncMock, patch

    with patch.object(dispatch, "_upload_live2d_associated_projection", new=AsyncMock()) as upload:
        for _ in range(2):
            asyncio.run(
                dispatch.run_specialized_postprocess(
                    "live2d-associated",
                    config,
                    association_index=index,
                    association_output_root=source,
                    association_namespace_root=namespace,
                )
            )
        assert upload.await_count == 2

        changed_target_config = associated_config(tmp_path, [associated_storage("remote/changed")])
        asyncio.run(
            dispatch.run_specialized_postprocess(
                "live2d-associated",
                changed_target_config,
                association_index=index,
                association_output_root=source,
                association_namespace_root=namespace,
            )
        )
        assert upload.await_count == 3
