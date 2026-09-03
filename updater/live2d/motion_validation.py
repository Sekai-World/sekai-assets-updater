"""Filesystem validation for restored Live2D motion-set output.

The validator deliberately operates on the emitted files only.  It does not
load Unity data and it does not rewrite ``.exp3.json`` files into another
format.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

_UNAVAILABLE = object()
_MOTION3_SUFFIX = ".motion3.json"
_EXP3_SUFFIX = ".exp3.json"
_VALID_CURVE_TARGETS = frozenset({"Model", "Parameter", "PartOpacity"})
_COUNT_FIELDS = (
    "CurveCount",
    "UserDataCount",
    "TotalSegmentCount",
    "TotalPointCount",
    "TotalUserDataSize",
)


@dataclass(frozen=True, slots=True)
class MotionDiagnostic:
    """A deterministic validation diagnostic.

    ``path`` is always relative to the validated root and ``code`` is a
    stable, machine-readable identifier.  The dataclass is frozen so callers
    can safely retain diagnostics as a report snapshot.
    """

    code: str
    path: str
    message: str

    def as_tuple(self) -> tuple[str, str, str]:
        """Return the stable ``(code, path, message)`` representation."""
        return self.code, self.path, self.message


@dataclass(frozen=True, slots=True)
class MotionValidationReport:
    """Result of :func:`validate_motion_output`."""

    ok: bool
    diagnostics: tuple[MotionDiagnostic, ...]
    facial_count: int
    motion_count: int
    constant_curve_facial_count: int = 0
    dynamic_curve_facial_count: int = 0


# Short aliases make the public result vocabulary convenient without creating
# a second diagnostic representation.
Diagnostic = MotionDiagnostic
ValidationReport = MotionValidationReport


@dataclass(frozen=True, slots=True)
class _CurveSummary:
    segment_count: int
    point_count: int
    values: tuple[int | float, ...]

    @property
    def is_constant(self) -> bool:
        return len(set(self.values)) <= 1


@dataclass(frozen=True, slots=True)
class _MotionSummary:
    segment_count: int
    point_count: int
    curve_is_constant: tuple[bool, ...]

    @property
    def is_constant_facial(self) -> bool:
        """Classify a valid facial document for reporting only."""
        return all(self.curve_is_constant)


def validate_motion_output(
    root: Path,
    *,
    expected_facials: int | None = None,
    expected_motions: int | None = None,
) -> MotionValidationReport:
    """Validate one extracted motion-set directory.

    The manifest's ``expressions`` entries are facial clip *base names* and
    are matched exclusively to ``facial/<name>.motion3.json``.  In particular,
    an ``.exp3.json`` file never satisfies a facial entry.
    """

    diagnostics: list[MotionDiagnostic] = []
    try:
        root_path = Path(root)
    except (TypeError, ValueError):
        diagnostics.append(
            MotionDiagnostic("root_invalid", ".", "Validation root is not a valid path.")
        )
        return _report(diagnostics, 0, 0)

    try:
        if not root_path.exists():
            diagnostics.append(MotionDiagnostic("root_missing", ".", "Validation root is missing."))
            return _report(diagnostics, 0, 0)
        if not root_path.is_dir():
            diagnostics.append(
                MotionDiagnostic("root_not_directory", ".", "Validation root is not a directory.")
            )
            return _report(diagnostics, 0, 0)
    except (OSError, ValueError):
        diagnostics.append(
            MotionDiagnostic("root_unreadable", ".", "Validation root could not be inspected.")
        )
        return _report(diagnostics, 0, 0)

    manifest_path = root_path / "BuildMotionData.json"
    manifest = _read_json_file(
        manifest_path,
        "BuildMotionData.json",
        diagnostics,
        missing_code="build_motion_data_missing",
        missing_message="BuildMotionData.json is missing.",
        not_file_code="build_motion_data_not_file",
        not_file_message="BuildMotionData.json is not a regular file.",
        read_code="build_motion_data_unreadable",
        read_message="BuildMotionData.json could not be read.",
        invalid_code="build_motion_data_invalid_json",
        invalid_message="BuildMotionData.json is not valid JSON.",
    )
    if manifest is _UNAVAILABLE:
        return _report(diagnostics, 0, 0)
    if not isinstance(manifest, dict):
        diagnostics.append(
            MotionDiagnostic(
                "build_motion_data_not_object",
                "BuildMotionData.json",
                "BuildMotionData.json must contain a JSON object.",
            )
        )
        return _report(diagnostics, 0, 0)

    facial_names, facial_count = _read_manifest_names(manifest, "expressions", diagnostics)
    motion_names, motion_count = _read_manifest_names(manifest, "motions", diagnostics)

    _check_expected_count(
        expected_facials,
        facial_count,
        "facial_count_mismatch",
        "facial",
        diagnostics,
    )
    _check_expected_count(
        expected_motions,
        motion_count,
        "motion_count_mismatch",
        "motion",
        diagnostics,
    )

    facial_available, facial_files = _inspect_group_directory(
        root_path / "facial", "facial", facial_names, diagnostics
    )
    motion_available, motion_files = _inspect_group_directory(
        root_path / "motion", "motion", motion_names, diagnostics
    )

    constant_facial_count, dynamic_facial_count = _validate_group_files(
        root_path / "facial",
        "facial",
        facial_names,
        facial_available,
        facial_files,
        diagnostics,
    )
    _validate_group_files(
        root_path / "motion",
        "motion",
        motion_names,
        motion_available,
        motion_files,
        diagnostics,
    )

    return _report(
        diagnostics,
        facial_count,
        motion_count,
        constant_curve_facial_count=constant_facial_count,
        dynamic_curve_facial_count=dynamic_facial_count,
    )


def _report(
    diagnostics: list[MotionDiagnostic],
    facial_count: int,
    motion_count: int,
    *,
    constant_curve_facial_count: int = 0,
    dynamic_curve_facial_count: int = 0,
) -> MotionValidationReport:
    return MotionValidationReport(
        ok=not diagnostics,
        diagnostics=tuple(diagnostics),
        facial_count=facial_count,
        motion_count=motion_count,
        constant_curve_facial_count=constant_curve_facial_count,
        dynamic_curve_facial_count=dynamic_curve_facial_count,
    )


def _read_json_file(
    path: Path,
    relative_path: str,
    diagnostics: list[MotionDiagnostic],
    *,
    missing_code: str,
    missing_message: str,
    not_file_code: str,
    not_file_message: str,
    read_code: str,
    read_message: str,
    invalid_code: str,
    invalid_message: str,
) -> object:
    try:
        if not path.exists():
            diagnostics.append(MotionDiagnostic(missing_code, relative_path, missing_message))
            return _UNAVAILABLE
        if path.is_symlink():
            diagnostics.append(
                MotionDiagnostic(
                    f"{read_code.removesuffix('_unreadable')}_symlink_unsupported",
                    relative_path,
                    "Output files must not be symbolic links.",
                )
            )
            return _UNAVAILABLE
        if not path.is_file():
            diagnostics.append(MotionDiagnostic(not_file_code, relative_path, not_file_message))
            return _UNAVAILABLE
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        diagnostics.append(MotionDiagnostic(invalid_code, relative_path, invalid_message))
        return _UNAVAILABLE
    except (OSError, ValueError):
        diagnostics.append(MotionDiagnostic(read_code, relative_path, read_message))
        return _UNAVAILABLE

    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError):
        diagnostics.append(MotionDiagnostic(invalid_code, relative_path, invalid_message))
        return _UNAVAILABLE


def _read_manifest_names(
    manifest: dict[str, Any],
    field: str,
    diagnostics: list[MotionDiagnostic],
) -> tuple[tuple[str, ...], int]:
    path = "BuildMotionData.json"
    if field not in manifest:
        diagnostics.append(
            MotionDiagnostic(
                "manifest_field_missing",
                path,
                f"BuildMotionData.json is missing the {field} array.",
            )
        )
        return (), 0

    value = manifest[field]
    if not isinstance(value, list):
        diagnostics.append(
            MotionDiagnostic(
                "manifest_field_not_array",
                path,
                f"BuildMotionData.json field {field} must be an array.",
            )
        )
        return (), 0

    names: list[str] = []
    seen: set[str] = set()
    for index, name in enumerate(value):
        if not isinstance(name, str):
            diagnostics.append(
                MotionDiagnostic(
                    "manifest_name_invalid",
                    path,
                    f"{field}[{index}] must be a non-empty clip name.",
                )
            )
            continue
        name_error = _clip_name_error(name)
        if name_error is not None:
            diagnostics.append(
                MotionDiagnostic(
                    name_error,
                    path,
                    f"{field}[{index}] is not a safe clip name.",
                )
            )
            continue
        if name in seen:
            diagnostics.append(
                MotionDiagnostic(
                    "manifest_name_duplicate",
                    path,
                    f"{field}[{index}] duplicates a clip name.",
                )
            )
            continue
        seen.add(name)
        names.append(name)

    return tuple(names), len(value)


def _clip_name_error(name: str) -> str | None:
    if name == "..":
        return "manifest_name_traversal"
    if not name.strip() or name == ".":
        return "manifest_name_invalid"
    if (
        "\x00" in name
        or "/" in name
        or "\\" in name
        or Path(name).is_absolute()
        or bool(PureWindowsPath(name).drive)
    ):
        return "manifest_name_traversal"
    if name.casefold().endswith((_MOTION3_SUFFIX, _EXP3_SUFFIX)):
        return "manifest_name_wrong_extension"
    return None


def _check_expected_count(
    expected: int | None,
    actual: int,
    mismatch_code: str,
    group: str,
    diagnostics: list[MotionDiagnostic],
) -> None:
    if expected is None:
        return
    if type(expected) is not int or expected < 0:
        diagnostics.append(
            MotionDiagnostic(
                "expected_count_invalid",
                "BuildMotionData.json",
                f"Expected {group} count must be a non-negative integer.",
            )
        )
        return
    if expected != actual:
        diagnostics.append(
            MotionDiagnostic(
                mismatch_code,
                "BuildMotionData.json",
                f"Expected {group} count does not match the manifest.",
            )
        )


def _inspect_group_directory(
    directory: Path,
    group: str,
    names: tuple[str, ...],
    diagnostics: list[MotionDiagnostic],
) -> tuple[bool, dict[str, Path]]:
    relative_directory = group
    try:
        if not directory.exists():
            diagnostics.append(
                MotionDiagnostic(
                    "directory_missing", relative_directory, f"{group} directory is missing."
                )
            )
            return False, {}
        if directory.is_symlink():
            diagnostics.append(
                MotionDiagnostic(
                    "directory_symlink_unsupported",
                    relative_directory,
                    f"{group} directory must not be a symbolic link.",
                )
            )
            return False, {}
        if not directory.is_dir():
            diagnostics.append(
                MotionDiagnostic(
                    "directory_not_directory",
                    relative_directory,
                    f"{group} output path is not a directory.",
                )
            )
            return False, {}
        entries = list(directory.rglob("*"))
    except (OSError, ValueError):
        diagnostics.append(
            MotionDiagnostic(
                "directory_unreadable",
                relative_directory,
                f"{group} directory could not be inspected.",
            )
        )
        return False, {}

    entries.sort(key=lambda entry: entry.relative_to(directory).as_posix())
    expected_files = {f"{name}{_MOTION3_SUFFIX}" for name in names}
    matching_files: dict[str, Path] = {}
    for entry in entries:
        relative_entry = f"{group}/{entry.relative_to(directory).as_posix()}"
        try:
            is_directory = entry.is_dir()
            is_file = entry.is_file()
        except OSError:
            diagnostics.append(
                MotionDiagnostic(
                    "filesystem_entry_unreadable",
                    relative_entry,
                    "Output entry could not be inspected.",
                )
            )
            continue

        if is_directory:
            diagnostics.append(
                MotionDiagnostic(
                    "unexpected_directory",
                    relative_entry,
                    "Nested output directories are not allowed.",
                )
            )
            continue
        if not is_file:
            diagnostics.append(
                MotionDiagnostic(
                    "unexpected_filesystem_entry",
                    relative_entry,
                    "Output entry is not a regular file.",
                )
            )
            continue

        is_direct_entry = "/" not in entry.relative_to(directory).as_posix()
        if is_direct_entry and entry.name in expected_files:
            matching_files[entry.name] = entry
        elif entry.name.casefold().endswith(_MOTION3_SUFFIX):
            diagnostics.append(
                MotionDiagnostic(
                    "extra_motion3_file",
                    relative_entry,
                    f"Unlisted {group} motion3 file is present.",
                )
            )
        elif entry.name.casefold().endswith(_EXP3_SUFFIX):
            code = "facial_exp3_unsupported" if group == "facial" else "exp3_unsupported"
            message = (
                "Facial output must use .motion3.json; .exp3.json is not accepted."
                if group == "facial"
                else ".exp3.json is not a supported motion output."
            )
            diagnostics.append(MotionDiagnostic(code, relative_entry, message))
        else:
            diagnostics.append(
                MotionDiagnostic(
                    "unexpected_extension",
                    relative_entry,
                    f"{group} output must use .motion3.json.",
                )
            )

    return True, matching_files


def _validate_group_files(
    directory: Path,
    group: str,
    names: tuple[str, ...],
    available: bool,
    files: dict[str, Path],
    diagnostics: list[MotionDiagnostic],
) -> tuple[int, int]:
    if not available:
        return 0, 0

    constant_count = 0
    dynamic_count = 0
    for name in names:
        filename = f"{name}{_MOTION3_SUFFIX}"
        path = files.get(filename)
        relative_path = f"{group}/{filename}"
        if path is None:
            diagnostics.append(
                MotionDiagnostic(
                    f"{group}_motion3_missing",
                    relative_path,
                    f"Listed {group} clip has no .motion3.json file.",
                )
            )
            continue

        before = len(diagnostics)
        document = _read_json_file(
            path,
            relative_path,
            diagnostics,
            missing_code=f"{group}_motion3_missing",
            missing_message=f"Listed {group} clip has no .motion3.json file.",
            not_file_code=f"{group}_motion3_not_file",
            not_file_message=f"Listed {group} output is not a regular file.",
            read_code=f"{group}_motion3_unreadable",
            read_message=f"Listed {group} motion3 file could not be read.",
            invalid_code=f"{group}_motion3_invalid_json",
            invalid_message=f"Listed {group} motion3 file is not valid JSON.",
        )
        if document is _UNAVAILABLE:
            continue

        summary = _validate_motion3_document(document, relative_path, diagnostics)
        if group == "facial" and len(diagnostics) == before and summary is not None:
            if summary.is_constant_facial:
                constant_count += 1
            else:
                dynamic_count += 1

    return constant_count, dynamic_count


def _validate_motion3_document(
    document: object,
    relative_path: str,
    diagnostics: list[MotionDiagnostic],
) -> _MotionSummary | None:
    if not isinstance(document, dict):
        diagnostics.append(
            MotionDiagnostic(
                "motion3_not_object", relative_path, "Motion3 document must be a JSON object."
            )
        )
        return None

    version = document.get("Version", _UNAVAILABLE)
    if version is _UNAVAILABLE:
        diagnostics.append(
            MotionDiagnostic("motion3_version_missing", relative_path, "Version is missing.")
        )
    elif type(version) is not int or version != 3:
        diagnostics.append(
            MotionDiagnostic("motion3_version_invalid", relative_path, "Version must be 3.")
        )

    meta = document.get("Meta", _UNAVAILABLE)
    meta_counts: dict[str, int] = {}
    if not isinstance(meta, dict):
        diagnostics.append(
            MotionDiagnostic("motion3_meta_invalid", relative_path, "Meta must be an object.")
        )
    else:
        for field in ("Duration", "Fps"):
            value = meta.get(field, _UNAVAILABLE)
            if value is _UNAVAILABLE:
                diagnostics.append(
                    MotionDiagnostic(
                        "motion3_meta_field_missing", relative_path, f"Meta.{field} is missing."
                    )
                )
            elif not _is_number(value):
                diagnostics.append(
                    MotionDiagnostic(
                        "motion3_meta_number_invalid",
                        relative_path,
                        f"Meta.{field} must be numeric.",
                    )
                )

        for field in _COUNT_FIELDS:
            value = meta.get(field, _UNAVAILABLE)
            if value is _UNAVAILABLE:
                diagnostics.append(
                    MotionDiagnostic(
                        "motion3_meta_count_missing", relative_path, f"Meta.{field} is missing."
                    )
                )
            elif not _is_count(value):
                diagnostics.append(
                    MotionDiagnostic(
                        "motion3_meta_count_invalid",
                        relative_path,
                        f"Meta.{field} must be a non-negative integer.",
                    )
                )
            else:
                meta_counts[field] = value

    curves_value = document.get("Curves", _UNAVAILABLE)
    curve_summaries: list[_CurveSummary] = []
    if not isinstance(curves_value, list):
        diagnostics.append(
            MotionDiagnostic("motion3_curves_invalid", relative_path, "Curves must be an array.")
        )
        curve_count: int | None = None
    else:
        curve_count = len(curves_value)
        seen_ids: set[str] = set()
        for index, curve in enumerate(curves_value):
            summary = _validate_curve(curve, index, relative_path, diagnostics, seen_ids)
            if summary is not None:
                curve_summaries.append(summary)

    user_data_value = document.get("UserData", _UNAVAILABLE)
    user_data_size = 0
    user_data_valid = True
    if not isinstance(user_data_value, list):
        diagnostics.append(
            MotionDiagnostic(
                "motion3_user_data_invalid", relative_path, "UserData must be an array."
            )
        )
        user_data_count: int | None = None
    else:
        user_data_count = len(user_data_value)
        for index, item in enumerate(user_data_value):
            if not isinstance(item, dict):
                diagnostics.append(
                    MotionDiagnostic(
                        "motion3_user_data_entry_invalid",
                        relative_path,
                        f"UserData[{index}] must be an object.",
                    )
                )
                user_data_valid = False
                continue
            time = item.get("Time", _UNAVAILABLE)
            value = item.get("Value", _UNAVAILABLE)
            if not _is_number(time):
                diagnostics.append(
                    MotionDiagnostic(
                        "motion3_user_data_time_invalid",
                        relative_path,
                        f"UserData[{index}].Time must be numeric.",
                    )
                )
                user_data_valid = False
            if not isinstance(value, str):
                diagnostics.append(
                    MotionDiagnostic(
                        "motion3_user_data_value_invalid",
                        relative_path,
                        f"UserData[{index}].Value must be a string.",
                    )
                )
                user_data_valid = False
            else:
                user_data_size += len(value)

    if "CurveCount" in meta_counts and curve_count is not None:
        _check_document_count(
            meta_counts["CurveCount"], curve_count, "CurveCount", relative_path, diagnostics
        )
    if "UserDataCount" in meta_counts and user_data_count is not None:
        _check_document_count(
            meta_counts["UserDataCount"],
            user_data_count,
            "UserDataCount",
            relative_path,
            diagnostics,
        )

    all_curves_valid = isinstance(curves_value, list) and len(curve_summaries) == len(curves_value)
    if all_curves_valid:
        total_segment_count = sum(summary.segment_count for summary in curve_summaries)
        total_point_count = sum(summary.point_count for summary in curve_summaries)
        _check_document_count(
            meta_counts.get("TotalSegmentCount"),
            total_segment_count,
            "TotalSegmentCount",
            relative_path,
            diagnostics,
        )
        _check_document_count(
            meta_counts.get("TotalPointCount"),
            total_point_count,
            "TotalPointCount",
            relative_path,
            diagnostics,
        )
    else:
        total_segment_count = 0
        total_point_count = 0
    if isinstance(user_data_value, list) and user_data_valid:
        _check_document_count(
            meta_counts.get("TotalUserDataSize"),
            user_data_size,
            "TotalUserDataSize",
            relative_path,
            diagnostics,
        )

    return _MotionSummary(
        segment_count=total_segment_count,
        point_count=total_point_count,
        curve_is_constant=tuple(summary.is_constant for summary in curve_summaries),
    )


def _check_document_count(
    declared: int | None,
    actual: int,
    field: str,
    relative_path: str,
    diagnostics: list[MotionDiagnostic],
) -> None:
    if declared is not None and declared != actual:
        diagnostics.append(
            MotionDiagnostic(
                "motion3_count_mismatch",
                relative_path,
                f"Meta.{field} does not match the emitted document.",
            )
        )


def _validate_curve(
    curve: object,
    index: int,
    relative_path: str,
    diagnostics: list[MotionDiagnostic],
    seen_ids: set[str],
) -> _CurveSummary | None:
    if not isinstance(curve, dict):
        diagnostics.append(
            MotionDiagnostic(
                "motion3_curve_invalid", relative_path, f"Curves[{index}] must be an object."
            )
        )
        return None

    curve_id = curve.get("Id", _UNAVAILABLE)
    target = curve.get("Target", _UNAVAILABLE)
    valid = True
    if not isinstance(curve_id, str) or not curve_id.strip():
        diagnostics.append(
            MotionDiagnostic(
                "motion3_curve_id_invalid", relative_path, f"Curves[{index}].Id is invalid."
            )
        )
        valid = False
    elif curve_id in seen_ids:
        diagnostics.append(
            MotionDiagnostic(
                "motion3_curve_id_duplicate", relative_path, f"Curves[{index}].Id is duplicated."
            )
        )
        valid = False
    else:
        seen_ids.add(curve_id)

    if not isinstance(target, str) or not target.strip():
        diagnostics.append(
            MotionDiagnostic(
                "motion3_curve_target_invalid", relative_path, f"Curves[{index}].Target is invalid."
            )
        )
        valid = False
    elif target not in _VALID_CURVE_TARGETS:
        diagnostics.append(
            MotionDiagnostic(
                "motion3_curve_target_invalid", relative_path, f"Curves[{index}].Target is invalid."
            )
        )
        valid = False

    segment_summary = _validate_segments(
        curve.get("Segments", _UNAVAILABLE), index, relative_path, diagnostics
    )
    if segment_summary is None:
        valid = False
    if not valid:
        return None
    return segment_summary


def _validate_segments(
    segments: object,
    curve_index: int,
    relative_path: str,
    diagnostics: list[MotionDiagnostic],
) -> _CurveSummary | None:
    prefix = f"Curves[{curve_index}].Segments"
    if not isinstance(segments, list) or len(segments) < 2:
        diagnostics.append(
            MotionDiagnostic(
                "motion3_segments_invalid", relative_path, f"{prefix} has an invalid layout."
            )
        )
        return None
    if type(segments[0]) is not int or segments[0] != 0 or not _is_number(segments[1]):
        diagnostics.append(
            MotionDiagnostic(
                "motion3_segments_invalid", relative_path, f"{prefix} must start with [0, value]."
            )
        )
        return None

    cursor = 2
    segment_count = 1
    point_count = 1
    values: list[int | float] = [segments[1]]
    previous_time: int | float | None = None
    widths = {0: 3, 1: 7, 2: 3, 3: 3}
    point_deltas = {0: 1, 1: 3, 2: 1, 3: 1}

    while cursor < len(segments):
        segment_type = segments[cursor]
        if type(segment_type) is not int or segment_type not in widths:
            diagnostics.append(
                MotionDiagnostic(
                    "motion3_segments_invalid",
                    relative_path,
                    f"{prefix} contains an unsupported segment type.",
                )
            )
            return None
        width = widths[segment_type]
        if cursor + width > len(segments):
            diagnostics.append(
                MotionDiagnostic(
                    "motion3_segments_invalid",
                    relative_path,
                    f"{prefix} contains a truncated segment.",
                )
            )
            return None

        payload = segments[cursor + 1 : cursor + width]
        if not all(_is_number(value) for value in payload):
            diagnostics.append(
                MotionDiagnostic(
                    "motion3_segments_invalid",
                    relative_path,
                    f"{prefix} contains a non-numeric segment value.",
                )
            )
            return None

        endpoint_time = payload[-2]
        if previous_time is not None and endpoint_time < previous_time:
            diagnostics.append(
                MotionDiagnostic(
                    "motion3_segments_invalid",
                    relative_path,
                    f"{prefix} times must be non-decreasing.",
                )
            )
            return None
        previous_time = endpoint_time
        if segment_type == 1:
            values.extend((payload[1], payload[3], payload[5]))
        else:
            values.append(payload[1])
        segment_count += 1
        point_count += point_deltas[segment_type]
        cursor += width

    return _CurveSummary(segment_count, point_count, tuple(values))


def _is_number(value: object) -> bool:
    return (type(value) is int) or (type(value) is float and math.isfinite(value))


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


__all__ = [
    "Diagnostic",
    "MotionDiagnostic",
    "MotionValidationReport",
    "ValidationReport",
    "validate_motion_output",
]
