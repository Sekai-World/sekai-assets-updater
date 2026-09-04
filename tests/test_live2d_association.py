"""Focused tests for L2D-1 master-table joins and motion candidates."""

from __future__ import annotations

import json
from pathlib import Path

from updater.live2d.association import LIVE2D_TABLE_NAMES, build_live2d_index
from updater.live2d.contracts import (
    CandidateStatus,
    DiagnosticCode,
    ModelOutputRecord,
    SharedMotionSetRecord,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "live2d" / "contracts_6.8.0.10.json"


def fixture_data() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def fixture_records(model_ids: tuple[str, ...], motion_ids: tuple[str, ...]):
    data = fixture_data()
    models = [
        ModelOutputRecord.from_dict(row)
        for row in data["model_outputs"]
        if row["model_output_id"] in model_ids
    ]
    motions = [
        SharedMotionSetRecord.from_dict(row)
        for row in data["motion_sets"]
        if row["motion_set_id"] in motion_ids
    ]
    return models, motions


def business_tables() -> dict[str, list[dict[str, object]]]:
    tables = {
        table_name: [
            {
                "id": 9000 + index,
                "characterId": 1,
                "motion": f"{table_name}_idle",
                "expression": f"{table_name}_smile",
            }
        ]
        for index, table_name in enumerate(LIVE2D_TABLE_NAMES)
    }
    tables["character2ds"].extend(
        [
            {"id": 101, "characterId": 1, "assetName": "ichika"},
            {"id": 202, "characterId": 20, "assetName": "mizuki"},
        ]
    )
    tables["costume2ds"].extend(
        [
            {"id": 1001, "character2dId": 101, "assetName": "v2_01ichika_unit"},
            {"id": 2001, "character2dId": 202, "assetName": "v2_20mizuki_unit"},
        ]
    )
    return tables


def synthetic_motion_set(motion_set_id: str, bundle_name: str) -> SharedMotionSetRecord:
    return SharedMotionSetRecord(
        motion_set_id=motion_set_id,
        motion_bundle={"name": bundle_name, "checksum": f"sha256:{motion_set_id}"},
        motion_output_path=f"motion/{motion_set_id}",
        facial_output_path=f"facial/{motion_set_id}",
        known_clips={"motions": ["idle"], "facials": ["smile"]},
        metadata_version="6.8.0.10",
    )


def test_exact_costume_character_join_and_normal_candidate_provenance() -> None:
    models, motions = fixture_records(("ichika-unit",), ("ichika-base",))
    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motions,
        tables=business_tables(),
    )

    association = index.models[0]
    assert association.character2d_id == 101
    assert association.character_id == 1
    assert len(index.models) == 1
    assert len(index.model_outputs) == 1
    assert len(index.motion_sets) == 1
    candidate = association.motion_sets[0]
    assert candidate.status == CandidateStatus.DERIVED.value
    assert candidate.motion_bundle == motions[0].motion_bundle

    role_evidence = [
        evidence
        for evidence in candidate.evidence
        if evidence.source_table in LIVE2D_TABLE_NAMES and "motion" in evidence.source_row
    ]
    assert {evidence.source_table for evidence in role_evidence} == set(LIVE2D_TABLE_NAMES)
    assert all(evidence.source_row["expression"] for evidence in role_evidence)
    assert all("Expressions" not in evidence.source_row for evidence in role_evidence)
    assert all("business facial clip" in evidence.rule for evidence in role_evidence)
    assert all(".exp3" in evidence.rule for evidence in role_evidence)


def test_exact_bundle_name_costume_shape_is_supported() -> None:
    models, motions = fixture_records(("ichika-unit",), ("ichika-base",))
    tables = business_tables()
    tables["costume2ds"] = [
        {"id": 1001, "character2dId": 101, "bundleName": models[0].model_bundle.name}
    ]

    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motions,
        tables=tables,
    )

    assert index.models[0].character2d_id == 101
    assert index.models[0].character_id == 1
    assert index.models[0].motion_sets[0].status == CandidateStatus.DERIVED.value


def test_missing_join_is_explicit_and_keeps_naming_candidate_ambiguous() -> None:
    models, motions = fixture_records(("ichika-april2025",), ("ichika-base",))
    tables = business_tables()
    tables["costume2ds"] = []
    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motions,
        tables=tables,
    )

    association = index.models[0]
    assert association.character2d_id is None
    assert association.character_id is None
    assert association.motion_sets[0].status == CandidateStatus.AMBIGUOUS.value
    assert any(
        diagnostic.code == DiagnosticCode.LIVE2D_JOIN_MISSING
        and diagnostic.path == "models/ichika-april2025"
        for diagnostic in index.diagnostics
    )


def test_missing_character2d_row_does_not_silently_complete_the_join() -> None:
    models, motions = fixture_records(("ichika-unit",), ("ichika-base",))
    tables = business_tables()
    tables["character2ds"] = [row for row in tables["character2ds"] if row.get("id") != 101]
    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motions,
        tables=tables,
    )

    association = index.models[0]
    assert association.character2d_id == 101
    assert association.character_id is None
    assert any(
        diagnostic.code == DiagnosticCode.LIVE2D_JOIN_MISSING
        and diagnostic.details.get("character2d_id") == 101
        for diagnostic in index.diagnostics
    )


def test_duplicate_character2d_rows_are_not_resolved_by_input_order() -> None:
    models, motions = fixture_records(("ichika-unit",), ("ichika-base",))
    tables = business_tables()
    tables["character2ds"].append({"id": 101, "characterId": 1, "assetName": "ichika_duplicate"})
    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motions,
        tables=tables,
    )

    association = index.models[0]
    assert association.character2d_id == 101
    assert association.character_id is None
    assert association.motion_sets[0].status == CandidateStatus.AMBIGUOUS.value
    assert any(
        diagnostic.code == DiagnosticCode.LIVE2D_MAPPING_AMBIGUOUS
        and diagnostic.details.get("character2d_id") == 101
        for diagnostic in index.diagnostics
    )


def test_missing_joined_character_id_keeps_candidate_ambiguous() -> None:
    models, motions = fixture_records(("ichika-unit",), ("ichika-base",))
    tables = business_tables()
    tables["character2ds"] = [row for row in tables["character2ds"] if row.get("id") != 101]
    tables["character2ds"].append({"id": 101, "assetName": "ichika"})
    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motions,
        tables=tables,
    )

    association = index.models[0]
    assert association.character2d_id == 101
    assert association.character_id is None
    assert association.motion_sets[0].status == CandidateStatus.AMBIGUOUS.value
    assert any(
        diagnostic.code == DiagnosticCode.LIVE2D_JOIN_MISSING
        and diagnostic.details.get("character2d_id") == 101
        for diagnostic in index.diagnostics
    )


def test_duplicate_costume_matches_are_ambiguous_without_choosing_a_row() -> None:
    models, motions = fixture_records(("ichika-unit",), ("ichika-base",))
    tables = business_tables()
    tables["costume2ds"].append({"id": 1002, "character2dId": 101, "assetName": "v2_01ichika_unit"})
    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motions,
        tables=tables,
    )

    association = index.models[0]
    assert association.character2d_id is None
    assert association.character_id is None
    assert association.motion_sets[0].status == CandidateStatus.AMBIGUOUS.value
    assert any(
        diagnostic.code == DiagnosticCode.LIVE2D_MAPPING_AMBIGUOUS
        and diagnostic.path == "models/ichika-unit"
        for diagnostic in index.diagnostics
    )
    assert any(
        diagnostic.details.get("reason") == "duplicate_character2d_id"
        for diagnostic in index.diagnostics
    )
    assert not any(
        diagnostic.details.get("reason") == "missing_character2d_id"
        for diagnostic in index.diagnostics
    )


def test_back_and_still_candidates_remain_ambiguous_and_protected() -> None:
    models, _ = fixture_records(("ichika-unit",), ())
    motion_rows = [
        SharedMotionSetRecord(
            motion_set_id="ichika-back",
            motion_bundle={
                "name": "motion/v2_01ichika_back_motion_base",
                "checksum": "sha256:back",
            },
            motion_output_path="motion/ichika-back",
            facial_output_path="facial/ichika-back",
            known_clips={"motions": ["back_idle"], "facials": ["back_smile"]},
            metadata_version="6.8.0.10",
        ),
        SharedMotionSetRecord(
            motion_set_id="ichika-still",
            motion_bundle={
                "name": "motion/v2_01ichika_still_01_motion_base",
                "checksum": "sha256:still",
            },
            motion_output_path="motion/ichika-still",
            facial_output_path="facial/ichika-still",
            known_clips={"motions": ["still_idle"], "facials": ["still_smile"]},
            metadata_version="6.8.0.10",
        ),
    ]
    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motion_rows,
        tables=business_tables(),
    )

    candidates = index.models[0].motion_sets
    assert {candidate.status for candidate in candidates} == {CandidateStatus.AMBIGUOUS.value}
    protected_rules = {
        evidence.rule
        for candidate in candidates
        for evidence in candidate.evidence
        if evidence.source == "naming"
    }
    assert any("back/still" in rule for rule in protected_rules)
    assert (
        sum(
            diagnostic.code == DiagnosticCode.LIVE2D_MAPPING_AMBIGUOUS
            for diagnostic in index.diagnostics
        )
        >= 2
    )


def test_joined_character_id_prefix_mismatch_is_auditable() -> None:
    models, _ = fixture_records(("ichika-unit",), ())
    mismatched_motion = synthetic_motion_set(
        "ichika-wrong-prefix", "motion/v2_20ichika_motion_base"
    )
    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=[mismatched_motion],
        tables=business_tables(),
    )

    assert index.models[0].motion_sets == ()
    assert any(
        diagnostic.code == DiagnosticCode.LIVE2D_MAPPING_AMBIGUOUS
        and diagnostic.severity == "warning"
        and diagnostic.details.get("reason") == "character_id_prefix_mismatch"
        and diagnostic.details.get("character_id") == 1
        and diagnostic.details.get("bundle_prefix") == "20"
        for diagnostic in index.diagnostics
    )


def test_non_base_normal_variant_is_candidate_only() -> None:
    models, _ = fixture_records(("ichika-unit",), ())
    variant_motion = synthetic_motion_set("ichika-event", "motion/v2_01ichika_event_motion_base")
    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=[variant_motion],
        tables=business_tables(),
    )

    candidate = index.models[0].motion_sets[0]
    assert candidate.status == CandidateStatus.AMBIGUOUS.value
    assert all(
        candidate.status != CandidateStatus.VERIFIED.value
        for candidate in index.models[0].motion_sets
    )


def test_malformed_role_rows_are_diagnosed_once_and_not_emitted_as_evidence() -> None:
    models, motions = fixture_records(("ichika-unit",), ("ichika-base",))
    tables = business_tables()
    tables["systemLive2ds"].append({"id": 9999, "characterId": 1, "motion": "missing_expression"})
    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motions,
        tables=tables,
    )

    malformed = [
        diagnostic
        for diagnostic in index.diagnostics
        if diagnostic.details.get("reason") == "malformed_role_row"
    ]
    assert len(malformed) == 1
    assert all(
        evidence.source_row.get("motion") != "missing_expression"
        for evidence in index.models[0].motion_sets[0].evidence
    )
    assert all(
        candidate.status != CandidateStatus.VERIFIED.value
        for model in index.models
        for candidate in model.motion_sets
    )


def test_all_six_tables_and_reordered_inputs_build_identical_indexes() -> None:
    models, motions = fixture_records(
        ("ichika-unit", "mizuki-unit"), ("ichika-base", "mizuki-base")
    )
    tables = business_tables()
    tables["character2ds"].append(
        {"id": 1003, "characterId": 1, "motion": "extra_idle", "expression": "extra_face"}
    )
    tables["costume2ds"].append(
        {
            "id": 1004,
            "character2dId": 999,
            "assetName": "unrelated",
            "characterId": 1,
            "motion": "extra_idle",
            "expression": "extra_face",
        }
    )
    tables["systemLive2ds"][0]["token"] = "ignored-secret"
    tables["bondsLive2ds"][0]["payload"] = "ignored-payload"
    tables["bondsRankUpLive2ds"][0]["url"] = "https://ignored.example"
    tables["loginBonusLive2ds"][0]["assetList"] = "obsolete"

    reordered_tables = {
        name: list(reversed(rows)) for name, rows in reversed(tuple(tables.items()))
    }
    first = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motions,
        tables=tables,
    )
    second = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=list(reversed(models)),
        motion_sets=list(reversed(motions)),
        tables=reordered_tables,
    )

    assert first.canonical_json_bytes() == second.canonical_json_bytes()
    serialized = first.canonical_json_bytes().decode("utf-8").casefold()
    assert "ignored-secret" not in serialized
    assert "ignored-payload" not in serialized
    assert "https://ignored.example" not in serialized
    assert "obsolete" not in serialized
    assert {
        evidence.source_table
        for model in first.models
        for candidate in model.motion_sets
        for evidence in candidate.evidence
        if evidence.source_table in LIVE2D_TABLE_NAMES
    } == set(LIVE2D_TABLE_NAMES)


def test_missing_required_table_is_an_error_diagnostic_not_a_successful_empty_join() -> None:
    models, motions = fixture_records(("ichika-unit",), ("ichika-base",))
    tables = business_tables()
    del tables["loginBonusLive2ds"]

    index = build_live2d_index(
        metadata_version="6.8.0.10",
        master_db_version="6.8.0.10",
        model_outputs=models,
        motion_sets=motions,
        tables=tables,
    )

    assert any(
        diagnostic.code == DiagnosticCode.LIVE2D_JOIN_MISSING
        and diagnostic.path == "tables/loginBonusLive2ds"
        and diagnostic.severity == "error"
        for diagnostic in index.diagnostics
    )
