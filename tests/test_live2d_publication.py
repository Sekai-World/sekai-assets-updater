"""Filesystem publication tests for the additive Live2D association index."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from updater.live2d.contracts import Live2DIndex, canonical_json_bytes
from updater.live2d.publication import (
    Live2DPublicationError,
    publish_live2d_index,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "live2d" / "contracts_6.8.0.10.json"


def fixture_data() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def fixture_index() -> Live2DIndex:
    return Live2DIndex.from_dict(fixture_data())


def materialize_outputs(root: Path, index: Live2DIndex) -> None:
    for record in index.model_outputs:
        output_directory = root / record.output_path
        output_directory.mkdir(parents=True, exist_ok=True)
        references = record.file_references
        for relative_path in (
            references.moc,
            *references.textures,
            *((references.physics,) if references.physics is not None else ()),
        ):
            path = output_directory / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"live2d-output")

    for record in index.motion_sets:
        motion_directory = root / record.motion_output_path
        facial_directory = root / record.facial_output_path
        motion_directory.mkdir(parents=True, exist_ok=True)
        facial_directory.mkdir(parents=True, exist_ok=True)
        for clip_name in record.known_clips.motions:
            (motion_directory / f"{clip_name}.motion3.json").write_bytes(b"motion")
        for clip_name in record.known_clips.facials:
            (facial_directory / f"{clip_name}.motion3.json").write_bytes(b"facial")


def temp_siblings(root: Path, index_path: Path) -> list[Path]:
    return list(root.glob(f".{index_path.name}.*.tmp"))


def test_two_models_share_one_motion_set_without_copied_motion_files(tmp_path: Path) -> None:
    index = fixture_index()
    materialize_outputs(tmp_path, index)
    index_path = tmp_path / "association-index.json"

    shared_models = [
        model
        for model in index.models
        if any(candidate.motion_set_id == "ichika-base" for candidate in model.motion_sets)
    ]
    assert len(shared_models) == 2
    assert {
        candidate.motion_set_id for model in shared_models for candidate in model.motion_sets
    } == {"ichika-base"}
    assert (tmp_path / "motion/ichika-base").is_dir()
    assert all(
        not (tmp_path / model_output.output_path / "motion").exists()
        for model_output in index.model_outputs
    )
    assert publish_live2d_index(index, tmp_path, index_path) == index
    assert Live2DIndex.from_json_bytes(index_path.read_bytes()) == index


def test_successful_publication_is_atomic_canonical_and_leaves_model_list_unchanged(
    tmp_path: Path,
) -> None:
    index = fixture_index()
    materialize_outputs(tmp_path, index)
    model_list = tmp_path / "model_list.json"
    model_list_bytes = b'{"authoritative":true}\n'
    model_list.write_bytes(model_list_bytes)
    index_path = tmp_path / "association-index.json"

    published = publish_live2d_index(index, tmp_path, index_path)

    assert published == index
    reloaded = Live2DIndex.from_json_bytes(index_path.read_bytes())
    assert reloaded == index
    assert canonical_json_bytes(reloaded) == canonical_json_bytes(index)
    assert index_path.read_bytes() == json.dumps(
        index.to_dict(), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    assert model_list.read_bytes() == model_list_bytes
    assert not temp_siblings(tmp_path, index_path)


@pytest.mark.parametrize("missing_reference", ["model", "motion", "motion_clip", "facial_clip"])
def test_missing_references_are_rejected_without_publishing(
    tmp_path: Path,
    missing_reference: str,
) -> None:
    index = fixture_index()
    materialize_outputs(tmp_path, index)
    index_path = tmp_path / "association-index.json"

    if missing_reference == "model":
        shutil.rmtree(tmp_path / index.model_outputs[0].output_path)
    elif missing_reference == "motion":
        shutil.rmtree(tmp_path / index.motion_sets[0].motion_output_path)
    elif missing_reference == "motion_clip":
        record = index.motion_sets[0]
        (
            tmp_path / record.motion_output_path / f"{record.known_clips.motions[0]}.motion3.json"
        ).unlink()
    else:
        record = index.motion_sets[0]
        (
            tmp_path / record.facial_output_path / f"{record.known_clips.facials[0]}.motion3.json"
        ).unlink()

    with pytest.raises(Live2DPublicationError, match="missing"):
        publish_live2d_index(index, tmp_path, index_path)

    assert not index_path.exists()
    assert not temp_siblings(tmp_path, index_path)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink support is unavailable")
def test_symlink_output_directory_is_rejected(tmp_path: Path) -> None:
    index = fixture_index()
    materialize_outputs(tmp_path, index)
    model_record = index.model_outputs[0]
    model_directory = tmp_path / model_record.output_path
    outside_directory = tmp_path / "outside-model"
    shutil.move(str(model_directory), str(outside_directory))
    try:
        model_directory.symlink_to(outside_directory, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"cannot create symlink in test environment: {exc}")

    with pytest.raises(Live2DPublicationError, match="symlink"):
        publish_live2d_index(index, tmp_path, tmp_path / "association-index.json")


@pytest.mark.parametrize("alias_kind", ["equal", "ancestor"])
def test_model_output_cannot_alias_or_contain_motion_output(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    data = fixture_data()
    if alias_kind == "equal":
        data["motion_sets"][0]["motion_output_path"] = data["model_outputs"][0]["output_path"]
    else:
        data["model_outputs"][0]["output_path"] = "shared-model-root"
        data["motion_sets"][0]["motion_output_path"] = "shared-model-root/motions"
    index = Live2DIndex.from_dict(data)
    materialize_outputs(tmp_path, index)
    index_path = tmp_path / "association-index.json"

    with pytest.raises(Live2DPublicationError, match="must not contain motion or facial"):
        publish_live2d_index(index, tmp_path, index_path)

    assert not index_path.exists()
    assert not temp_siblings(tmp_path, index_path)


@pytest.mark.parametrize("duplicate_field", ["motion_output_path", "facial_output_path"])
def test_motion_set_output_directories_cannot_be_reused_across_records(
    tmp_path: Path,
    duplicate_field: str,
) -> None:
    data = fixture_data()
    data["motion_sets"][1][duplicate_field] = data["motion_sets"][0][duplicate_field]
    index = Live2DIndex.from_dict(data)
    materialize_outputs(tmp_path, index)
    index_path = tmp_path / "association-index.json"

    with pytest.raises(Live2DPublicationError, match="must be distinct"):
        publish_live2d_index(index, tmp_path, index_path)

    assert not index_path.exists()
    assert not temp_siblings(tmp_path, index_path)


def test_invalid_new_index_preserves_existing_bytes_and_leaves_no_temp_sibling(
    tmp_path: Path,
) -> None:
    index_path = tmp_path / "association-index.json"
    existing_bytes = b'{"previous":"valid-byte-sequence"}\n'
    index_path.write_bytes(existing_bytes)
    model_list = tmp_path / "model_list.json"
    model_list_bytes = b'{"models":["authoritative"]}\n'
    model_list.write_bytes(model_list_bytes)

    invalid = fixture_data()
    invalid["models"][0]["model_output_id"] = "missing-model-output"

    with pytest.raises(ValueError, match="dangling model output"):
        publish_live2d_index(invalid, tmp_path, index_path)

    assert index_path.read_bytes() == existing_bytes
    assert model_list.read_bytes() == model_list_bytes
    assert not temp_siblings(tmp_path, index_path)


@pytest.mark.parametrize("target_name", ["model_list.json", "MODEL_LIST.JSON"])
def test_model_list_path_cannot_be_used_as_index_target(
    tmp_path: Path,
    target_name: str,
) -> None:
    index = fixture_index()
    materialize_outputs(tmp_path, index)
    model_list = tmp_path / "model_list.json"
    original = b"authoritative model list\n"
    model_list.write_bytes(original)
    target = tmp_path / target_name

    with pytest.raises(Live2DPublicationError, match="model_list.json"):
        publish_live2d_index(index, tmp_path, target)

    assert model_list.read_bytes() == original
    if target != model_list:
        if target.exists():
            assert os.path.samefile(target, model_list)
