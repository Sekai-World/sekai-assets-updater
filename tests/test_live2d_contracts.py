"""Contract and fixture tests for the independent Live2D association track."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from updater.live2d.contracts import (
    DIAGNOSTIC_CODES,
    BundleIdentity,
    CandidateEvidence,
    CandidateStatus,
    Diagnostic,
    DiagnosticCode,
    DiagnosticSeverity,
    KnownClips,
    Live2DIndex,
    Model3FileReferences,
    ModelOutputRecord,
    SharedMotionSetRecord,
    canonical_json_bytes,
    is_live2d_scope,
    to_json_dict,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "live2d" / "contracts_6.8.0.10.json"
EXPECTED_BUNDLES = {
    "model/v2_01ichika_unit",
    "model/v2_01ichika_april2025",
    "motion/v2_01ichika_motion_base",
    "model/v2_20mizuki_unit",
    "motion/v2_20mizuki_motion_base",
}


def load_fixture_data() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def load_fixture() -> Live2DIndex:
    return Live2DIndex.from_dict(load_fixture_data())


def test_fixture_has_exact_verified_bundle_names_and_candidate_only_links() -> None:
    data = load_fixture_data()
    bundle_names = {
        *[row["model_bundle"]["name"] for row in data["model_outputs"]],
        *[row["motion_bundle"]["name"] for row in data["motion_sets"]],
    }
    assert bundle_names == EXPECTED_BUNDLES
    assert all(
        candidate["status"] == "ambiguous"
        for model in data["models"]
        for candidate in model["motion_sets"]
    )
    assert all("motion_bundle" not in model for model in data["models"])
    assert all(
        "model_bundle" not in candidate
        for model in data["models"]
        for candidate in model["motion_sets"]
    )


def test_fixture_loads_and_preserves_business_rows_and_versions() -> None:
    index = load_fixture()

    assert index.metadata_version == "6.8.0.10"
    assert index.master_db_version == "6.8.0.10"
    assert any(
        evidence.source_table == "costume2ds"
        for model in index.models
        for evidence in model.join_evidence
    )
    business_evidence = [
        evidence
        for model in index.models
        for candidate in model.motion_sets
        for evidence in candidate.evidence
    ]
    assert any(evidence.source_table == "systemLive2ds" for evidence in business_evidence)
    assert any(
        evidence.source_row.get("expression") == "ichika_smile"
        for evidence in business_evidence
    )
    april = next(model for model in index.models if model.model_output_id == "ichika-april2025")
    assert april.character2d_id is None
    assert april.character_id is None
    assert any(
        diagnostic.code == "live2d_join_missing"
        and diagnostic.path == "models/ichika-april2025"
        for diagnostic in index.diagnostics
    )
    assert any(
        "no variant join" in evidence.rule
        for candidate in april.motion_sets
        for evidence in candidate.evidence
    )


def test_canonical_serialization_is_independent_of_equivalent_input_order() -> None:
    original = load_fixture_data()
    reordered = copy.deepcopy(original)
    reordered["model_outputs"].reverse()
    reordered["motion_sets"].reverse()
    reordered["models"].reverse()
    reordered["diagnostics"].reverse()
    for model in reordered["models"]:
        model["motion_sets"].reverse()
        model["join_evidence"].reverse()
        for candidate in model["motion_sets"]:
            candidate["evidence"].reverse()

    first = Live2DIndex.from_dict(original)
    second = Live2DIndex.from_dict(reordered)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert to_json_dict(first) == json.loads(canonical_json_bytes(first))
    assert Live2DIndex.from_json_bytes(canonical_json_bytes(first)) == first

    first_evidence = CandidateEvidence(
        source="naming",
        rule="stable evidence",
        source_row={"first": 1, "second": 2},
    )
    second_evidence = CandidateEvidence(
        source="naming",
        rule="stable evidence",
        source_row={"second": 2, "first": 1},
    )
    assert first_evidence.evidence_id == second_evidence.evidence_id


def test_canonical_output_contains_no_sensitive_transport_or_raw_bundle_data() -> None:
    serialized = canonical_json_bytes(load_fixture()).decode("utf-8").casefold()

    for forbidden in (
        "http://",
        "https://",
        "x-amz-",
        "access_token",
        "cookie",
        "authorization",
        "unityfs",
        "assetlist",
        "sekai-master-db-diff",
    ):
        assert forbidden not in serialized


def test_model3_file_references_preserve_observed_and_future_fields() -> None:
    references = load_fixture().model_outputs[0].file_references

    assert set(references.to_dict()) == {"Moc", "Textures", "Physics"}
    assert "Motions" not in references.to_dict()
    assert "Expressions" not in references.to_dict()
    additional = Model3FileReferences.from_dict(
        {
            "Moc": "model.moc3",
            "Textures": ["z.png", "a.png"],
            "Physics": "model.physics3.json",
            "Pose": "model.pose3.json",
            "DisplayInfo": "model.cdi3.json",
            "HitAreas": [{"Id": "Head", "Name": "Head"}],
        }
    )
    assert additional.textures == ("z.png", "a.png")
    assert additional.to_dict()["Pose"] == "model.pose3.json"
    assert additional.to_dict()["DisplayInfo"] == "model.cdi3.json"
    assert additional.to_dict()["HitAreas"] == [{"Id": "Head", "Name": "Head"}]
    assert additional.pose == "model.pose3.json"
    assert additional.display_info == "model.cdi3.json"
    assert additional.hit_areas == ({"Id": "Head", "Name": "Head"},)
    with pytest.raises(ValueError, match="forbidden fields"):
        Model3FileReferences.from_dict(
            {
                "Moc": "model.moc3",
                "Textures": [],
                "Physics": "model.physics3.json",
                "Motions": {"idle": []},
            }
        )
    with pytest.raises(ValueError, match="forbidden fields"):
        Model3FileReferences.from_dict(
            {
                "Moc": "model.moc3",
                "Textures": [],
                "Expressions": [],
            }
        )
    with pytest.raises(ValueError, match="duplicate identity"):
        Model3FileReferences(moc="model.moc3", textures=("same.png", "same.png"))


def test_statuses_and_diagnostic_vocabulary_are_validated() -> None:
    assert {
        candidate.status
        for model in load_fixture().models
        for candidate in model.motion_sets
    } == {CandidateStatus.AMBIGUOUS.value}
    assert {
        DiagnosticCode.LIVE2D_SCOPE_MISMATCH.value,
        DiagnosticCode.LIVE2D_JOIN_MISSING.value,
        DiagnosticCode.LIVE2D_MAPPING_AMBIGUOUS.value,
        DiagnosticCode.LIVE2D_BUILD_MOTION_INVALID.value,
        DiagnosticCode.LIVE2D_FACIAL_FORMAT_INVALID.value,
        DiagnosticCode.LIVE2D_INDEX_INTEGRITY.value,
    } <= DIAGNOSTIC_CODES
    for code in DIAGNOSTIC_CODES:
        Diagnostic(code=code, severity=DiagnosticSeverity.INFO, message="stable diagnostic")

    with pytest.raises(ValueError, match="candidate.status"):
        load_fixture().models[0].motion_sets[0].__class__(
            motion_set_id="bad-status",
            motion_bundle={"name": "motion/base", "checksum": "sha256:abc"},
            status="pending",
        )
    with pytest.raises(ValueError, match="diagnostic.severity"):
        Diagnostic(
            code=DiagnosticCode.LIVE2D_SCOPE_MISMATCH,
            severity="fatal",
            message="bad severity",
        )
    with pytest.raises(ValueError, match="diagnostic.code"):
        Diagnostic(code="unknown_live2d_code", severity="error", message="bad code")


def test_unsafe_paths_duplicate_identities_and_malformed_clips_are_rejected() -> None:
    bundle = {"name": "model/base", "checksum": "sha256:abc"}
    refs = {"Moc": "model.moc3", "Textures": ["texture.png"]}
    with pytest.raises(ValueError, match="unsafe path"):
        ModelOutputRecord(
            model_output_id="model",
            model_bundle=bundle,
            output_path="../outside",
            file_references=refs,
            metadata_version="6.8.0.10",
        )
    with pytest.raises(ValueError, match="relative POSIX path"):
        ModelOutputRecord(
            model_output_id="model",
            model_bundle=bundle,
            output_path="/absolute",
            file_references=refs,
            metadata_version="6.8.0.10",
        )
    with pytest.raises(ValueError, match="must not be empty"):
        ModelOutputRecord(
            model_output_id="model",
            model_bundle=bundle,
            output_path="",
            file_references=refs,
            metadata_version="6.8.0.10",
        )
    with pytest.raises(ValueError, match="duplicate identity"):
        KnownClips(motions=("idle", "idle"))
    duplicate_data = load_fixture_data()
    duplicate_data["model_outputs"].append(copy.deepcopy(duplicate_data["model_outputs"][0]))
    with pytest.raises(ValueError, match="duplicate identity"):
        Live2DIndex.from_dict(duplicate_data)
    dangling_data = load_fixture_data()
    dangling_data["models"][0]["motion_sets"][0]["motion_set_id"] = "missing-set"
    with pytest.raises(ValueError, match="live2d_index_integrity"):
        Live2DIndex.from_dict(dangling_data)
    missing_model_records = load_fixture_data()
    missing_model_records["model_outputs"] = []
    with pytest.raises(ValueError, match="live2d_index_integrity"):
        Live2DIndex.from_dict(missing_model_records)
    missing_motion_records = load_fixture_data()
    missing_motion_records["motion_sets"] = []
    with pytest.raises(ValueError, match="live2d_index_integrity"):
        Live2DIndex.from_dict(missing_motion_records)
    missing_both_records = load_fixture_data()
    missing_both_records["model_outputs"] = []
    missing_both_records["motion_sets"] = []
    with pytest.raises(ValueError, match="live2d_index_integrity"):
        Live2DIndex.from_dict(missing_both_records)
    with pytest.raises(ValueError, match="clip name"):
        KnownClips(facials=("../smile",))
    with pytest.raises(ValueError, match="clip name"):
        KnownClips(facials=("smile.motion3.json",))
    with pytest.raises(ValueError, match="obsolete assetList"):
        CandidateEvidence(
            source="metadata",
            source_table="assetList",
            rule="not allowed",
        )
    with pytest.raises(ValueError, match="sensitive transport"):
        CandidateEvidence(
            source="metadata",
            rule="not allowed",
            source_row={"token": "not-persisted"},
        )
    with pytest.raises(ValueError, match="sensitive transport"):
        CandidateEvidence(
            source="metadata",
            rule="not allowed",
            source_row={"signed_url": "not-persisted"},
        )
    with pytest.raises(ValueError, match="sensitive transport"):
        CandidateEvidence(
            source="metadata",
            rule="not allowed",
            source_row={"payload": "not-persisted"},
        )

    future_row = CandidateEvidence(
        source="metadata",
        rule="future-safe fields",
        source_row={
            "databaseId": 42,
            "author": "sanitized",
            "assetUri": "assets/live2d/model.json",
        },
    )
    assert future_row.source_row["databaseId"] == 42
    assert future_row.source_row["author"] == "sanitized"
    assert future_row.source_row["assetUri"] == "assets/live2d/model.json"


def test_shared_motion_and_facial_outputs_cannot_share_a_path() -> None:
    with pytest.raises(ValueError, match="physically separate"):
        SharedMotionSetRecord(
            motion_set_id="base",
            motion_bundle={"name": "motion/base", "checksum": "sha256:abc"},
            motion_output_path="motion/base",
            facial_output_path="motion/base",
            known_clips={"motions": ["idle"], "facials": ["smile"]},
            metadata_version="6.8.0.10",
        )


def test_checksums_remain_opaque_and_case_preserving() -> None:
    bundle = BundleIdentity(name="model/base", checksum="SHA256:AbC")

    assert bundle.checksum == "SHA256:AbC"


def test_scope_is_explicit_and_does_not_follow_generic_motion_paths() -> None:
    assert is_live2d_scope({"bundleName": "live2d/model/base"})
    assert is_live2d_scope({"bundleName": "live2d/motion/base"})
    assert not is_live2d_scope({"bundleName": "character/motion/base"})
    assert not is_live2d_scope({"bundleName": "music/music_score/base"})
    assert not is_live2d_scope({"bundleName": "assets/live2d/model/base"})
