"""Tests for explicit Live2D output-record construction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from updater.live2d.contracts import (
    MODEL_OUTPUT_SCHEMA_VERSION,
    ModelOutputRecord,
    SharedMotionSetRecord,
)
from updater.live2d.index_adapter import (
    Live2DIndexAdapterError,
    _discover_model3_paths,
    build_model_output_record,
    build_shared_motion_set_record,
)

METADATA_VERSION = "6.8.0.10"


def write_model3(directory: Path, *, name: str = "model.model3.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {
                    "Moc": "model.moc3",
                    "Textures": ["texture_0.png", "texture_1.png"],
                    "Physics": "model.physics3.json",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def model_bundle(**values: object) -> dict[str, object]:
    return {"bundleName": "live2d/model/ichika", "hash": "manifest-hash", **values}


def motion_bundle(**values: object) -> dict[str, object]:
    return {"bundleName": "live2d/motion/ichika", "hash": "manifest-hash", **values}


def build_model(root: Path, *, bundle: dict[str, object] | None = None) -> ModelOutputRecord:
    write_model3(root / "chosen-output" / "nested")
    return build_model_output_record(
        output_root=root,
        output_path="chosen-output",
        model_output_id="caller-model-id",
        bundle=bundle or model_bundle(),
        metadata_version=METADATA_VERSION,
    )


def test_builds_model_record_from_one_recursive_model3_and_preserves_selection(
    tmp_path: Path,
) -> None:
    record = build_model(tmp_path)

    assert isinstance(record, ModelOutputRecord)
    assert record.model_output_id == "caller-model-id"
    assert record.output_path == "chosen-output"
    assert record.model3_path == "nested/model.model3.json"
    assert record.schema_version == MODEL_OUTPUT_SCHEMA_VERSION
    assert record.model_bundle.name == "live2d/model/ichika"
    assert record.model_bundle.checksum == "hash:manifest-hash"
    assert record.file_references.moc == "model.moc3"
    assert record.file_references.textures == ("texture_0.png", "texture_1.png")
    assert record.file_references.physics == "model.physics3.json"
    assert record.metadata_version == METADATA_VERSION


def test_explicit_adapter_path_matching_remains_case_sensitive(tmp_path: Path) -> None:
    write_model3(tmp_path / "Actual-Output")

    exact = build_model_output_record(
        output_root=tmp_path,
        output_path="Actual-Output",
        model_output_id="exact",
        bundle=model_bundle(),
        metadata_version=METADATA_VERSION,
    )
    assert exact.output_path == "Actual-Output"

    with pytest.raises(Live2DIndexAdapterError, match="referenced directory is missing"):
        build_model_output_record(
            output_root=tmp_path,
            output_path="actual_output",
            model_output_id="case-sensitive",
            bundle=model_bundle(),
            metadata_version=METADATA_VERSION,
        )


def test_automatic_path_discovery_rejects_ambiguous_casefold_matches(tmp_path: Path) -> None:
    (tmp_path / "model").mkdir()
    with pytest.raises(Live2DIndexAdapterError, match="referenced directory is missing"):
        _discover_model3_paths(tmp_path, "model/missing")

    (tmp_path / "model" / "FOO").mkdir(parents=True)
    try:
        (tmp_path / "model" / "Foo").mkdir()
    except FileExistsError:
        pytest.skip("case-insensitive filesystems cannot represent an ambiguous collision")

    with pytest.raises(Live2DIndexAdapterError, match="ambiguous case-insensitive path component"):
        _discover_model3_paths(tmp_path, "model/foo")


def test_declared_sibling_model3_paths_share_one_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "model"
    write_model3(output, name="first.model3.json")
    write_model3(output, name="second.model3.json")
    bundle = model_bundle()

    first = build_model_output_record(
        output_root=tmp_path,
        output_path="model",
        model_output_id="first",
        model3_path="first.model3.json",
        bundle=bundle,
        metadata_version=METADATA_VERSION,
    )
    second = build_model_output_record(
        output_root=tmp_path,
        output_path="model",
        model_output_id="second",
        model3_path="second.model3.json",
        bundle=bundle,
        metadata_version=METADATA_VERSION,
    )

    assert first.model3_path == "first.model3.json"
    assert second.model3_path == "second.model3.json"


def test_hash_is_preferred_and_crc_is_used_when_hash_is_unusable(tmp_path: Path) -> None:
    hash_record = build_model(tmp_path / "hash", bundle=model_bundle(hash="hash-value", crc=9))
    crc_record = build_model(
        tmp_path / "crc",
        bundle=model_bundle(hash="", crc=12345),
    )

    assert hash_record.model_bundle.checksum == "hash:hash-value"
    assert crc_record.model_bundle.checksum == "crc:12345"


def test_missing_bundle_checksum_is_rejected(tmp_path: Path) -> None:
    write_model3(tmp_path / "model")

    with pytest.raises(Live2DIndexAdapterError, match="usable hash or crc"):
        build_model_output_record(
            output_root=tmp_path,
            output_path="model",
            model_output_id="model",
            bundle={"bundleName": "live2d/model/missing-checksum"},
            metadata_version=METADATA_VERSION,
        )


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("missing", "no .*model3.json"),
        ("multiple", "exactly one"),
        ("malformed", "malformed JSON"),
    ],
)
def test_model3_cardinality_and_json_errors(
    tmp_path: Path,
    setup: str,
    message: str,
) -> None:
    output = tmp_path / "model"
    output.mkdir()
    if setup == "multiple":
        write_model3(output, name="one.model3.json")
        write_model3(output / "nested", name="two.model3.json")
    elif setup == "malformed":
        (output / "bad.model3.json").write_text("{not json", encoding="utf-8")

    bundle = model_bundle()
    with pytest.raises(Live2DIndexAdapterError, match=message):
        build_model_output_record(
            output_root=tmp_path,
            output_path="model",
            model_output_id="model",
            bundle=bundle,
            metadata_version=METADATA_VERSION,
        )


def test_model_output_path_traversal_and_symlink_are_rejected(tmp_path: Path) -> None:
    write_model3(tmp_path / "model")
    traversal_bundle = model_bundle()
    with pytest.raises(Live2DIndexAdapterError, match="relative POSIX path"):
        build_model_output_record(
            output_root=tmp_path,
            output_path="../model",
            model_output_id="model",
            bundle=traversal_bundle,
            metadata_version=METADATA_VERSION,
        )

    outside = tmp_path / "outside"
    write_model3(outside)
    symlink = tmp_path / "linked-model"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    symlink_bundle = model_bundle()
    with pytest.raises(Live2DIndexAdapterError, match="symlink"):
        build_model_output_record(
            output_root=tmp_path,
            output_path="linked-model",
            model_output_id="model",
            bundle=symlink_bundle,
            metadata_version=METADATA_VERSION,
        )


def write_motion_outputs(root: Path) -> None:
    (root / "bundle").mkdir(parents=True)
    (root / "motion-files").mkdir()
    (root / "facial-files").mkdir()
    (root / "bundle" / "BuildMotionData.json").write_text(
        json.dumps({"motions": ["walk", "idle"], "expressions": ["smile"]}),
        encoding="utf-8",
    )
    for clip in ("walk", "idle"):
        (root / "motion-files" / f"{clip}.motion3.json").write_text("{}", encoding="utf-8")
    (root / "facial-files" / "smile.motion3.json").write_text("{}", encoding="utf-8")


def build_motion(root: Path, *, bundle: dict[str, object] | None = None) -> SharedMotionSetRecord:
    write_motion_outputs(root)
    return build_shared_motion_set_record(
        output_root=root,
        motion_bundle_output_path="bundle",
        motion_output_path="motion-files",
        facial_output_path="facial-files",
        motion_set_id="caller-motion-set",
        bundle=bundle or motion_bundle(),
        metadata_version=METADATA_VERSION,
    )


def test_builds_motion_record_from_build_motion_data_and_listed_files(tmp_path: Path) -> None:
    record = build_motion(tmp_path)

    assert isinstance(record, SharedMotionSetRecord)
    assert record.motion_set_id == "caller-motion-set"
    assert record.motion_bundle.checksum == "hash:manifest-hash"
    assert record.motion_output_path == "motion-files"
    assert record.facial_output_path == "facial-files"
    assert record.known_clips.motions == ("idle", "walk")
    assert record.known_clips.facials == ("smile",)


def test_motion_record_accepts_internal_spaces_in_clip_filenames(tmp_path: Path) -> None:
    (tmp_path / "bundle").mkdir()
    (tmp_path / "motion").mkdir()
    (tmp_path / "facial").mkdir()
    (tmp_path / "bundle" / "BuildMotionData.json").write_text(
        json.dumps({"motions": ["walk fast"], "expressions": ["face_ worry_01"]}),
        encoding="utf-8",
    )
    (tmp_path / "motion" / "walk fast.motion3.json").write_text("{}", encoding="utf-8")
    (tmp_path / "facial" / "face_ worry_01.motion3.json").write_text("{}", encoding="utf-8")

    record = build_shared_motion_set_record(
        output_root=tmp_path,
        motion_bundle_output_path="bundle",
        motion_output_path="motion",
        facial_output_path="facial",
        motion_set_id="spaces",
        bundle=motion_bundle(),
        metadata_version=METADATA_VERSION,
    )

    assert record.known_clips.motions == ("walk fast",)
    assert record.known_clips.facials == ("face_ worry_01",)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing", "is missing"),
        ("malformed", "malformed JSON"),
        ("non-array", "must be JSON arrays"),
        ("missing-clip", "referenced file is missing"),
        ("invalid-clip", "invalid clip names"),
    ],
)
def test_motion_output_validation(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    write_motion_outputs(tmp_path)
    build_data = tmp_path / "bundle" / "BuildMotionData.json"
    if mutation == "missing":
        build_data.unlink()
    elif mutation == "malformed":
        build_data.write_text("{not json", encoding="utf-8")
    elif mutation == "non-array":
        build_data.write_text(
            json.dumps({"motions": {"idle": True}, "expressions": []}),
            encoding="utf-8",
        )
    elif mutation == "missing-clip":
        (tmp_path / "motion-files" / "idle.motion3.json").unlink()
    elif mutation == "invalid-clip":
        build_data.write_text(
            json.dumps({"motions": ["../escape"], "expressions": []}),
            encoding="utf-8",
        )

    bundle = motion_bundle()
    with pytest.raises(Live2DIndexAdapterError, match=message):
        build_shared_motion_set_record(
            output_root=tmp_path,
            motion_bundle_output_path="bundle",
            motion_output_path="motion-files",
            facial_output_path="facial-files",
            motion_set_id="motion",
            bundle=bundle,
            metadata_version=METADATA_VERSION,
        )


def test_motion_and_facial_outputs_must_be_separate(tmp_path: Path) -> None:
    write_motion_outputs(tmp_path)
    bundle = motion_bundle()
    with pytest.raises(Live2DIndexAdapterError, match="physically separate"):
        build_shared_motion_set_record(
            output_root=tmp_path,
            motion_bundle_output_path="bundle",
            motion_output_path="motion-files",
            facial_output_path="motion-files",
            motion_set_id="motion",
            bundle=bundle,
            metadata_version=METADATA_VERSION,
        )


def test_motion_clip_symlink_is_rejected_without_scanning_unlisted_files(tmp_path: Path) -> None:
    write_motion_outputs(tmp_path)
    target = tmp_path / "motion-files" / "walk.motion3.json"
    target.unlink()
    try:
        target.symlink_to(tmp_path / "outside.motion3.json")
    except OSError:
        pytest.skip("symlinks are unavailable")

    bundle = motion_bundle()
    with pytest.raises(Live2DIndexAdapterError, match="symlink"):
        build_shared_motion_set_record(
            output_root=tmp_path,
            motion_bundle_output_path="bundle",
            motion_output_path="motion-files",
            facial_output_path="facial-files",
            motion_set_id="motion",
            bundle=bundle,
            metadata_version=METADATA_VERSION,
        )
