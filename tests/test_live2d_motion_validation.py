"""Focused tests for the filesystem-only Live2D motion validator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from updater.live2d.motion_validation import validate_motion_output

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "live2d" / "motion_counts_6.8.0.10.json"


def _motion3(
    *,
    curve_id: str = "ParamA",
    segments: list[int | float] | None = None,
    user_data: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    segments = [0, 1] if segments is None else segments
    user_data = [] if user_data is None else user_data
    segment_count = 1
    point_count = 1
    cursor = 2
    widths = {0: 3, 1: 7, 2: 3, 3: 3}
    point_deltas = {0: 1, 1: 3, 2: 1, 3: 1}
    while cursor < len(segments):
        segment_type = int(segments[cursor])
        segment_count += 1
        point_count += point_deltas[segment_type]
        cursor += widths[segment_type]

    return {
        "Version": 3,
        "Meta": {
            "Duration": 1,
            "Fps": 60,
            "CurveCount": 1,
            "UserDataCount": len(user_data),
            "TotalSegmentCount": segment_count,
            "TotalPointCount": point_count,
            "TotalUserDataSize": sum(len(item["Value"]) for item in user_data),
        },
        "Curves": [{"Target": "Parameter", "Id": curve_id, "Segments": segments}],
        "UserData": user_data,
    }


def _write_set(
    root: Path,
    *,
    expressions: list[str] | None = None,
    motions: list[str] | None = None,
    facial_documents: dict[str, dict[str, object]] | None = None,
    motion_documents: dict[str, dict[str, object]] | None = None,
) -> None:
    expressions = ["smile"] if expressions is None else expressions
    motions = ["idle"] if motions is None else motions
    facial_documents = (
        {name: _motion3() for name in expressions} if facial_documents is None else facial_documents
    )
    motion_documents = (
        {name: _motion3() for name in motions} if motion_documents is None else motion_documents
    )

    (root / "facial").mkdir(parents=True)
    (root / "motion").mkdir()
    (root / "BuildMotionData.json").write_text(
        json.dumps({"expressions": expressions, "motions": motions}), encoding="utf-8"
    )
    for name, document in facial_documents.items():
        (root / "facial" / f"{name}.motion3.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
    for name, document in motion_documents.items():
        (root / "motion" / f"{name}.motion3.json").write_text(
            json.dumps(document), encoding="utf-8"
        )


def _load_motion_count_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _placeholder_names(spec: dict[str, Any]) -> list[str]:
    return [f"{spec['prefix']}{index:03d}" for index in range(1, spec["count"] + 1)]


def _write_sanitized_count_set(
    root: Path, motion_set: dict[str, Any]
) -> tuple[list[str], list[str], list[str], list[str]]:
    observed = motion_set["observed"]
    known_clips = observed["known_clips"]
    generation = motion_set["generation"]
    known_curve_types = generation["known_facial_curve_types"]
    placeholders = generation["generated_placeholder_names"]

    constant_facials = [
        *known_curve_types["constant"],
        *_placeholder_names(placeholders["facials"]["constant"]),
    ]
    dynamic_facials = [
        *known_curve_types["dynamic"],
        *_placeholder_names(placeholders["facials"]["dynamic"]),
    ]
    expressions = [*constant_facials, *dynamic_facials]
    motions = [
        *known_clips["motions"],
        *_placeholder_names(placeholders["motions"]),
    ]
    facial_documents = {
        name: _motion3(
            curve_id=f"Param{index}",
            segments=[0, 7, 0, 1, 7] if name in constant_facials else [0, 0, 0, 1, 1],
        )
        for index, name in enumerate(expressions)
    }
    _write_set(
        root,
        expressions=expressions,
        motions=motions,
        facial_documents=facial_documents,
        motion_documents={name: _motion3() for name in motions},
    )
    return expressions, motions, constant_facials, dynamic_facials


def _codes(report) -> set[str]:
    return {diagnostic.code for diagnostic in report.diagnostics}


def test_valid_set_uses_motion3_for_facial_and_reports_curve_classification(tmp_path: Path) -> None:
    dynamic_segments = [0, 0, 0, 0.5, 1, 0, 1, 0]
    _write_set(
        tmp_path,
        expressions=["constant", "dynamic"],
        facial_documents={
            "constant": _motion3(curve_id="ParamConstant"),
            "dynamic": _motion3(curve_id="ParamDynamic", segments=dynamic_segments),
        },
        motion_documents={
            "idle": _motion3(user_data=[{"Time": 0.5, "Value": "event"}]),
        },
    )

    report = validate_motion_output(tmp_path, expected_facials=2, expected_motions=1)

    assert report.ok
    assert report.diagnostics == ()
    assert (report.facial_count, report.motion_count) == (2, 1)
    assert report.constant_curve_facial_count == 1
    assert report.dynamic_curve_facial_count == 1


def test_facial_constancy_uses_emitted_values_not_point_count(tmp_path: Path) -> None:
    _write_set(
        tmp_path,
        expressions=["ramp", "writer_constant"],
        facial_documents={
            "ramp": _motion3(segments=[0, 0, 0, 1, 1]),
            "writer_constant": _motion3(segments=[0, 7, 0, 1, 7]),
        },
    )

    report = validate_motion_output(tmp_path)

    assert report.ok
    assert report.constant_curve_facial_count == 1
    assert report.dynamic_curve_facial_count == 1


def test_mixed_facial_curves_are_dynamic_when_one_curve_changes(tmp_path: Path) -> None:
    constant_curve = _motion3(curve_id="ParamConstant", segments=[0, 7, 0, 1, 7])
    dynamic_curve = _motion3(curve_id="ParamDynamic", segments=[0, 0, 0, 1, 1])
    mixed_document = _motion3()
    mixed_document["Curves"] = [constant_curve["Curves"][0], dynamic_curve["Curves"][0]]
    mixed_document["Meta"] = {
        "Duration": 1,
        "Fps": 60,
        "CurveCount": 2,
        "UserDataCount": 0,
        "TotalSegmentCount": 4,
        "TotalPointCount": 4,
        "TotalUserDataSize": 0,
    }
    _write_set(
        tmp_path,
        expressions=["mixed"],
        facial_documents={"mixed": mixed_document},
    )

    report = validate_motion_output(tmp_path)

    assert report.ok
    assert report.constant_curve_facial_count == 0
    assert report.dynamic_curve_facial_count == 1


def test_count_expectations_are_diagnosed_without_changing_manifest_counts(tmp_path: Path) -> None:
    _write_set(tmp_path)

    report = validate_motion_output(tmp_path, expected_facials=2, expected_motions=3)

    assert not report.ok
    assert (report.facial_count, report.motion_count) == (1, 1)
    assert {"facial_count_mismatch", "motion_count_mismatch"} <= _codes(report)


def test_missing_and_extra_files_are_reported(tmp_path: Path) -> None:
    _write_set(tmp_path)
    (tmp_path / "motion" / "idle.motion3.json").unlink()
    (tmp_path / "facial" / "extra.motion3.json").write_text("{}", encoding="utf-8")
    (tmp_path / "facial" / "smile.motion3").write_text("{}", encoding="utf-8")

    report = validate_motion_output(tmp_path)

    assert not report.ok
    assert {"motion_motion3_missing", "extra_motion3_file", "unexpected_extension"} <= _codes(
        report
    )


def test_duplicate_manifest_entries_are_diagnosed(tmp_path: Path) -> None:
    _write_set(tmp_path, expressions=["smile", "smile"])

    report = validate_motion_output(tmp_path)

    assert not report.ok
    assert report.facial_count == 2
    assert "manifest_name_duplicate" in _codes(report)


def test_malformed_motion3_document_is_reported(tmp_path: Path) -> None:
    _write_set(tmp_path)
    (tmp_path / "facial" / "smile.motion3.json").write_text(
        json.dumps(
            {
                "Version": 2,
                "Meta": {"Duration": "one", "Fps": 60, "CurveCount": 0},
                "Curves": [{"Target": "", "Id": "", "Segments": [0]}],
                "UserData": [],
            }
        ),
        encoding="utf-8",
    )

    report = validate_motion_output(tmp_path)

    assert not report.ok
    assert {
        "motion3_version_invalid",
        "motion3_meta_number_invalid",
        "motion3_meta_count_missing",
        "motion3_curve_id_invalid",
        "motion3_curve_target_invalid",
        "motion3_segments_invalid",
    } <= _codes(report)


def test_malformed_json_has_a_stable_diagnostic(tmp_path: Path) -> None:
    _write_set(tmp_path)
    (tmp_path / "motion" / "idle.motion3.json").write_text("{", encoding="utf-8")

    first = validate_motion_output(tmp_path)
    second = validate_motion_output(tmp_path)

    assert not first.ok
    assert [item.as_tuple() for item in first.diagnostics] == [
        item.as_tuple() for item in second.diagnostics
    ]
    assert all(not item.path.startswith(str(tmp_path)) for item in first.diagnostics)
    assert "motion_motion3_invalid_json" in _codes(first)


def test_facial_exp3_is_never_accepted_or_required(tmp_path: Path) -> None:
    _write_set(tmp_path)
    (tmp_path / "facial" / "smile.motion3.json").unlink()
    (tmp_path / "facial" / "smile.exp3.json").write_text("{}", encoding="utf-8")

    report = validate_motion_output(tmp_path)

    assert not report.ok
    assert "facial_exp3_unsupported" in _codes(report)
    assert "facial_motion3_missing" in _codes(report)
    assert report.constant_curve_facial_count == 0


def test_manifest_traversal_name_is_rejected(tmp_path: Path) -> None:
    _write_set(tmp_path, expressions=["../outside"])

    report = validate_motion_output(tmp_path)

    assert not report.ok
    assert "manifest_name_traversal" in _codes(report)
    assert not (tmp_path.parent / "outside.motion3.json").exists()


def test_sanitized_roadmap_l2d2_counts_from_versioned_fixture(tmp_path: Path) -> None:
    fixture = _load_motion_count_fixture()

    assert fixture["fixture_status"] == "sanitized"
    assert fixture["sanitized"] is True
    assert fixture["metadata_version"] == "6.8.0.10"
    assert fixture["provenance"]["raw_proprietary_assets_included"] is False

    for motion_set in fixture["motion_sets"]:
        character = motion_set["character"]
        observed = motion_set["observed"]
        expected_facials = observed["facial_count"]
        expected_motions = observed["motion_count"]
        expected_constant = observed["constant_facial_count"]
        expected_dynamic = observed["dynamic_facial_count"]
        root = tmp_path / character.lower()
        facial_names, motion_names, constant_names, dynamic_names = _write_sanitized_count_set(
            root, motion_set
        )

        assert set(observed["known_clips"]["facials"]) <= set(facial_names)
        assert set(observed["known_clips"]["motions"]) <= set(motion_names)
        assert len(facial_names) == expected_facials
        assert len(motion_names) == expected_motions
        assert len(constant_names) == expected_constant
        assert len(dynamic_names) == expected_dynamic

        report = validate_motion_output(
            root,
            expected_facials=expected_facials,
            expected_motions=expected_motions,
        )

        assert report.ok, character
        assert (report.facial_count, report.motion_count) == (
            expected_facials,
            expected_motions,
        )
        assert (
            report.constant_curve_facial_count,
            report.dynamic_curve_facial_count,
        ) == (expected_constant, expected_dynamic)

        manifest = json.loads((root / "BuildMotionData.json").read_text(encoding="utf-8"))
        assert manifest["expressions"] == facial_names
        assert manifest["motions"] == motion_names
        facial_files = list((root / "facial").iterdir())
        motion_files = list((root / "motion").iterdir())
        assert {path.name.removesuffix(".motion3.json") for path in facial_files} == set(
            facial_names
        )
        assert {path.name.removesuffix(".motion3.json") for path in motion_files} == set(
            motion_names
        )
        assert all(path.name.endswith(".motion3.json") for path in facial_files)
        assert not list(root.rglob("*.exp3.json"))
