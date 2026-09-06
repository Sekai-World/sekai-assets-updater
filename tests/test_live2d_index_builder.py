"""Tests for explicit Live2D provider/output/index composition."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from updater.live2d.association import LIVE2D_TABLE_NAMES
from updater.live2d.contracts import DiagnosticCode, Live2DContractError
from updater.live2d.index_builder import (
    Live2DIndexBuilderError,
    ModelOutputSelection,
    SharedMotionSetSelection,
    build_live2d_association_index,
)
from updater.live2d.master_data import (
    Live2DMasterDataSnapshot,
    LocalMasterDataProvider,
)

METADATA_VERSION = "6.8.0.10"
MASTER_DB_VERSION = "master-db-test"


def write_tables(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for table_name in LIVE2D_TABLE_NAMES:
        (root / f"{table_name}.json").write_text("[]", encoding="utf-8")


def write_model3(directory: Path, *, filename: str = "selected.model3.json") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(
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


def write_motion_outputs(root: Path, name: str) -> None:
    bundle_directory = root / "motion-bundles" / name
    motion_directory = root / "motions" / name
    facial_directory = root / "facials" / name
    bundle_directory.mkdir(parents=True, exist_ok=True)
    motion_directory.mkdir(parents=True, exist_ok=True)
    facial_directory.mkdir(parents=True, exist_ok=True)
    (bundle_directory / "BuildMotionData.json").write_text(
        json.dumps({"motions": ["idle", "wave"], "expressions": ["smile"]}),
        encoding="utf-8",
    )
    for clip_name in ("idle", "wave"):
        (motion_directory / f"{clip_name}.motion3.json").write_text("{}", encoding="utf-8")
    (facial_directory / "smile.motion3.json").write_text("{}", encoding="utf-8")


def empty_snapshot() -> Live2DMasterDataSnapshot:
    return Live2DMasterDataSnapshot(
        master_db_version=MASTER_DB_VERSION,
        tables={table_name: [] for table_name in LIVE2D_TABLE_NAMES},
    )


@dataclass
class CountingProvider:
    delegate: LocalMasterDataProvider | None = None
    snapshot: Live2DMasterDataSnapshot | None = None
    calls: int = 0

    def load_live2d_snapshot(self) -> Live2DMasterDataSnapshot:
        self.calls += 1
        if self.delegate is not None:
            return self.delegate.load_live2d_snapshot()
        assert self.snapshot is not None
        return self.snapshot


def selections(root: Path) -> tuple[list[ModelOutputSelection], list[SharedMotionSetSelection]]:
    models = [
        ModelOutputSelection(
            output_root=root,
            output_path="restored/models/second",
            model_output_id="caller-model-z",
            bundle={"bundleName": "arbitrary/model-z", "hash": "model-z-hash"},
        ),
        ModelOutputSelection(
            output_root=root,
            output_path="restored/models/first",
            model_output_id="caller-model-a",
            bundle={"bundleName": "arbitrary/model-a", "hash": "model-a-hash"},
        ),
    ]
    motions = [
        SharedMotionSetSelection(
            output_root=root,
            motion_bundle_output_path=Path("motion-bundles/second"),
            motion_output_path="motions/second",
            facial_output_path="facials/second",
            motion_set_id="caller-motion-z",
            bundle={"bundleName": "arbitrary/motion-z", "hash": "motion-z-hash"},
        ),
        SharedMotionSetSelection(
            output_root=root,
            motion_bundle_output_path=Path("motion-bundles/first"),
            motion_output_path="motions/first",
            facial_output_path="facials/first",
            motion_set_id="caller-motion-a",
            bundle={"bundleName": "arbitrary/motion-a", "hash": "motion-a-hash"},
        ),
    ]
    return models, motions


def prepare_artifacts(
    root: Path,
) -> tuple[list[ModelOutputSelection], list[SharedMotionSetSelection], CountingProvider]:
    write_tables(root)
    write_model3(root / "restored/models/second/nested")
    write_model3(root / "restored/models/first/nested")
    write_motion_outputs(root, "second")
    write_motion_outputs(root, "first")
    model_selections, motion_selections = selections(root)
    provider = CountingProvider(
        delegate=LocalMasterDataProvider(root=root, master_db_version=MASTER_DB_VERSION)
    )
    return model_selections, motion_selections, provider


def test_builds_full_explicit_provider_adapter_association_path(tmp_path: Path) -> None:
    model_selections, motion_selections, provider = prepare_artifacts(tmp_path)
    original_models = list(model_selections)
    original_motions = list(motion_selections)

    index = build_live2d_association_index(
        provider=provider,
        metadata_version=METADATA_VERSION,
        model_outputs=model_selections,
        motion_sets=motion_selections,
    )

    assert provider.calls == 1
    assert index.metadata_version == METADATA_VERSION
    assert index.master_db_version == MASTER_DB_VERSION
    assert [record.model_output_id for record in index.model_outputs] == [
        "caller-model-a",
        "caller-model-z",
    ]
    assert [record.motion_set_id for record in index.motion_sets] == [
        "caller-motion-a",
        "caller-motion-z",
    ]

    models_by_id = {record.model_output_id: record for record in index.model_outputs}
    assert models_by_id["caller-model-a"].output_path == "restored/models/first"
    assert models_by_id["caller-model-z"].output_path == "restored/models/second"
    assert models_by_id["caller-model-a"].model_bundle.name == "arbitrary/model-a"
    assert models_by_id["caller-model-z"].model_bundle.name == "arbitrary/model-z"

    motions_by_id = {record.motion_set_id: record for record in index.motion_sets}
    assert motions_by_id["caller-motion-a"].motion_output_path == "motions/first"
    assert motions_by_id["caller-motion-a"].facial_output_path == "facials/first"
    assert motions_by_id["caller-motion-z"].motion_output_path == "motions/second"
    assert motions_by_id["caller-motion-z"].facial_output_path == "facials/second"
    assert motions_by_id["caller-motion-a"].motion_bundle.name == "arbitrary/motion-a"
    assert motions_by_id["caller-motion-z"].motion_bundle.name == "arbitrary/motion-z"

    # None of the arbitrary names/IDs supplies an association candidate.
    assert len(index.models) == 2
    assert all(model.motion_sets == () for model in index.models)
    assert all(
        diagnostic.code == DiagnosticCode.LIVE2D_JOIN_MISSING for diagnostic in index.diagnostics
    )
    assert {diagnostic.path for diagnostic in index.diagnostics} == {
        "models/caller-model-a",
        "models/caller-model-z",
    }
    assert model_selections == original_models
    assert motion_selections == original_motions


def test_multiple_model_records_can_share_one_output_path(tmp_path: Path) -> None:
    write_tables(tmp_path)
    output = tmp_path / "shared-model"
    write_model3(output, filename="first.model3.json")
    write_model3(output, filename="second.model3.json")
    bundle = {"bundleName": "arbitrary/model-shared", "hash": "model-shared-hash"}
    models = [
        ModelOutputSelection(
            output_root=tmp_path,
            output_path="shared-model",
            model_output_id="model-first",
            bundle=bundle,
            model3_path="first.model3.json",
        ),
        ModelOutputSelection(
            output_root=tmp_path,
            output_path="shared-model",
            model_output_id="model-second",
            bundle=bundle,
            model3_path="second.model3.json",
        ),
    ]
    index = build_live2d_association_index(
        provider=CountingProvider(snapshot=empty_snapshot()),
        metadata_version=METADATA_VERSION,
        model_outputs=models,
        motion_sets=[],
    )

    assert [record.model3_path for record in index.model_outputs] == [
        "first.model3.json",
        "second.model3.json",
    ]
    assert {record.output_path for record in index.model_outputs} == {"shared-model"}


def test_reordered_sequences_build_the_same_deterministic_index(tmp_path: Path) -> None:
    model_selections, motion_selections, provider = prepare_artifacts(tmp_path)

    first = build_live2d_association_index(
        provider=provider,
        metadata_version=METADATA_VERSION,
        model_outputs=model_selections,
        motion_sets=motion_selections,
    )
    second = build_live2d_association_index(
        provider=provider,
        metadata_version=METADATA_VERSION,
        model_outputs=list(reversed(model_selections)),
        motion_sets=list(reversed(motion_selections)),
    )

    assert first.canonical_json_bytes() == second.canonical_json_bytes()
    assert provider.calls == 2


@pytest.mark.parametrize(
    "invalid",
    ["not-a-selection-sequence", {"selection": "mapping"}, b"bytes"],
)
def test_selection_inputs_must_be_sequences(
    invalid: object,
) -> None:
    provider = CountingProvider(snapshot=empty_snapshot())

    with pytest.raises(Live2DIndexBuilderError, match="model_outputs"):
        build_live2d_association_index(
            provider=provider,
            metadata_version=METADATA_VERSION,
            model_outputs=invalid,  # type: ignore[arg-type]
            motion_sets=[],
        )
    assert provider.calls == 0


def test_selection_items_must_use_the_explicit_selection_types() -> None:
    provider = CountingProvider(snapshot=empty_snapshot())
    invalid_selection = object()

    with pytest.raises(Live2DIndexBuilderError, match=r"model_outputs\[0\]"):
        build_live2d_association_index(
            provider=provider,
            metadata_version=METADATA_VERSION,
            model_outputs=[invalid_selection],  # type: ignore[list-item]
            motion_sets=[],
        )
    assert provider.calls == 0


def test_invalid_provider_snapshot_is_rejected_after_one_provider_call() -> None:
    class InvalidProvider:
        calls = 0

        def load_live2d_snapshot(self) -> object:
            self.calls += 1
            return object()

    provider = InvalidProvider()
    with pytest.raises(Live2DIndexBuilderError, match="Live2DMasterDataSnapshot"):
        build_live2d_association_index(
            provider=provider,  # type: ignore[arg-type]
            metadata_version=METADATA_VERSION,
            model_outputs=[],
            motion_sets=[],
        )
    assert provider.calls == 1


def test_provider_exception_is_not_hidden() -> None:
    class RaisingProvider:
        def load_live2d_snapshot(self) -> Live2DMasterDataSnapshot:
            raise RuntimeError("provider failure")

    provider = RaisingProvider()
    with pytest.raises(RuntimeError, match="provider failure"):
        build_live2d_association_index(
            provider=provider,  # type: ignore[arg-type]
            metadata_version=METADATA_VERSION,
            model_outputs=[],
            motion_sets=[],
        )


def test_duplicate_model_ids_and_bundle_names_are_rejected_by_index_contract(
    tmp_path: Path,
) -> None:
    model_selections, motion_selections, provider = prepare_artifacts(tmp_path)
    duplicate_id = replace(
        model_selections[1],
        model_output_id=model_selections[0].model_output_id,
    )
    with pytest.raises(Live2DContractError, match="index.model_outputs"):
        build_live2d_association_index(
            provider=provider,
            metadata_version=METADATA_VERSION,
            model_outputs=[model_selections[0], duplicate_id],
            motion_sets=motion_selections,
        )

    model_selections, motion_selections, provider = prepare_artifacts(tmp_path / "bundles")
    duplicate_bundle = replace(
        model_selections[1],
        bundle=model_selections[0].bundle,
    )
    with pytest.raises(Live2DContractError, match="bundles"):
        build_live2d_association_index(
            provider=provider,
            metadata_version=METADATA_VERSION,
            model_outputs=[model_selections[0], duplicate_bundle],
            motion_sets=motion_selections,
        )
