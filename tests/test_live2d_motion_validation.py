"""Focused tests for the filesystem-only Live2D motion validator."""

from __future__ import annotations

import json
from pathlib import Path

from updater.live2d.motion_validation import validate_motion_output


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
