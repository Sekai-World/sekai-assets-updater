"""GLB Phase 1 planner tests: profiles, closure BFS, classification, validation (#35)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from updater.extract.glb import (
    DIAG_DEPENDENCY_CYCLE,
    DIAG_DEPENDENCY_EXCLUDED,
    DIAG_META_BUNDLE_MISSING,
    EDGE_ANIMATION,
    EDGE_SHADER,
    STANDARD_PURPOSE,
    ExtractionPlan,
    PlanValidationError,
    resolve_glb_flags,
)
from updater.extract.glb.planner import (
    build_extraction_plan,
    composite_profile,
    ensure_plan_valid,
    live2d_profile,
    scene3d_profile,
    select_composite_roots,
    select_stage_roots,
    select_timeline_roots,
    standard_profile,
    timeline3d_profile,
    union_requirements,
    validate_plan,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "streaming_live"
STAGE_ROOT = "scene3d/stage/base_007_sp_live"
TIMELINE_ROOT = "streaming_live/timeline/0006_lon_vbs_01"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def build_stage_plan(metadata: dict | None = None, profile=None, **kwargs) -> ExtractionPlan:
    metadata = metadata if metadata is not None else load_fixture("stage_graph.json")
    profile = profile or scene3d_profile(**kwargs)
    roots = select_stage_roots(metadata, [STAGE_ROOT])
    return build_extraction_plan(
        metadata, profile, roots, package_id="streaming_live/0006_lon_vbs_01/stage"
    )


def test_stage_fixture_closure_contains_expected_dependency_classes() -> None:
    plan = build_stage_plan()

    assert set(plan.bundle_names()) == {
        STAGE_ROOT,
        "scene3d/stage/shared_base_stage",
        "scene3d/light/stage_light_rig",
        "scene3d/shader/custom_lil_001",
        "scene3d/texture/tex_stage_common",
        "scene3d/camera/stage_camera_decoration",
    }
    roles = {ref.bundle_name: ref.role for ref in plan.bundles}
    assert roles[STAGE_ROOT] == "root"
    assert roles["scene3d/shader/custom_lil_001"] == "dependency"
    checksums = {ref.bundle_name: ref.checksum for ref in plan.bundles}
    assert checksums["scene3d/shader/custom_lil_001"] == "2b3c4d5e6f708192a3b4c5d6e7f8091a2b3"


def test_planner_is_deterministic_and_validates_clean() -> None:
    first = build_stage_plan()
    second = build_stage_plan()

    assert first.canonical_json_bytes() == second.canonical_json_bytes()
    assert validate_plan(first, scene3d_profile()) == ()
    ensure_plan_valid(first, scene3d_profile())


def test_reason_overrides_record_shader_edges() -> None:
    profile = scene3d_profile(reason_overrides=((re.compile(r"scene3d/shader/"), EDGE_SHADER),))
    plan = build_stage_plan(profile=profile)

    shader_edges = {
        edge.source
        for edge in plan.dependency_edges
        if edge.target == "scene3d/shader/custom_lil_001"
    }
    assert shader_edges == {
        STAGE_ROOT,
        "scene3d/stage/shared_base_stage",
    }
    shader_reasons = {
        edge.reason
        for edge in plan.dependency_edges
        if edge.target == "scene3d/shader/custom_lil_001"
    }
    assert shader_reasons == {EDGE_SHADER}


def test_timeline_fixture_closure_spans_stage_character_and_media() -> None:
    metadata = load_fixture("timeline_graph.json")
    profile = timeline3d_profile()
    roots = select_timeline_roots(metadata, [TIMELINE_ROOT])
    plan = build_extraction_plan(
        metadata, profile, roots, package_id="streaming_live/0006_lon_vbs_01"
    )

    names = set(plan.bundle_names())
    assert STAGE_ROOT in names
    assert "character/model/char_001_unit" in names
    assert "streaming_live/audio/0006_bgm" in names
    assert "scene3d/camera/stage_camera_decoration" in names
    assert validate_plan(plan, profile) == ()


def test_missing_dependency_fails_validation_before_extraction() -> None:
    metadata = load_fixture("missing_dependency_graph.json")
    plan = build_extraction_plan(
        metadata,
        scene3d_profile(),
        select_stage_roots(metadata, ["scene3d/stage/base_broken_shader"]),
        package_id="streaming_live/broken/stage",
    )

    assert [(m.bundle_name, m.referenced_by) for m in plan.missing_dependencies] == [
        ("scene3d/shader/removed_from_index", "scene3d/stage/base_broken_shader")
    ]
    problems = validate_plan(plan, scene3d_profile())
    assert problems and problems[0].code == DIAG_META_BUNDLE_MISSING
    with pytest.raises(PlanValidationError, match="missing dependency"):
        ensure_plan_valid(plan, scene3d_profile())


def test_required_exclusion_fails_but_optional_exclusion_warns() -> None:
    profile = scene3d_profile(exclusions=((re.compile(r"scene3d/texture/"), True, "texture"),))
    plan = build_stage_plan(profile=profile)
    assert [item.required for item in plan.excluded_dependencies] == [True]
    problems = validate_plan(plan, profile)
    assert problems and problems[0].code == DIAG_DEPENDENCY_EXCLUDED
    with pytest.raises(PlanValidationError, match="required dependencies excluded"):
        ensure_plan_valid(plan, profile)

    optional_profile = scene3d_profile(
        exclusions=((re.compile(r"scene3d/texture/"), False, "texture"),)
    )
    optional_plan = build_stage_plan(profile=optional_profile)
    assert "scene3d/texture/tex_stage_common" not in set(optional_plan.bundle_names())
    assert validate_plan(optional_plan, optional_profile) == ()
    assert any(
        d.code == DIAG_DEPENDENCY_EXCLUDED and d.severity == "warning"
        for d in optional_plan.diagnostics
    )


def test_cycle_terminates_and_is_recorded_exactly_once() -> None:
    metadata = load_fixture("cycle_graph.json")
    plan = build_extraction_plan(
        metadata,
        scene3d_profile(),
        select_stage_roots(metadata, ["scene3d/stage/cycle_a"]),
        package_id="streaming_live/cycle/stage",
    )

    assert set(plan.bundle_names()) == {
        "scene3d/stage/cycle_a",
        "scene3d/stage/cycle_b",
        "scene3d/stage/cycle_c",
    }
    cycles = [d for d in plan.diagnostics if d.code == DIAG_DEPENDENCY_CYCLE]
    assert len(cycles) == 1
    assert "cycle_a" in cycles[0].message and "cycle_c" in cycles[0].message
    assert validate_plan(plan, scene3d_profile()) == ()
    fatal_profile = scene3d_profile(cycles_fatal=True)
    assert validate_plan(plan, fatal_profile)
    with pytest.raises(PlanValidationError, match="cycle"):
        ensure_plan_valid(plan, fatal_profile)


def test_self_edge_is_a_cycle_diagnostic() -> None:
    metadata = {"solo": {"bundleName": "solo", "dependencies": ["solo"], "hash": "h"}}
    plan = build_extraction_plan(
        metadata,
        scene3d_profile(),
        select_stage_roots(metadata, ["solo"]),
        package_id="streaming_live/solo/stage",
    )

    assert [d.code for d in plan.diagnostics] == [DIAG_DEPENDENCY_CYCLE]
    assert validate_plan(plan, scene3d_profile()) == ()


def test_two_roots_share_one_physical_bundle_requirement() -> None:
    metadata = load_fixture("stage_graph.json")
    roots = select_stage_roots(metadata, [STAGE_ROOT, "scene3d/stage/shared_base_stage"])
    plan = build_extraction_plan(
        metadata, scene3d_profile(), roots, package_id="streaming_live/two_roots/stage"
    )

    assert len(plan.roots) == 2
    bundle_names = plan.bundle_names()
    assert len(bundle_names) == len(set(bundle_names))
    assert bundle_names.count("scene3d/shader/custom_lil_001") == 1


def test_union_requirements_merges_and_dedupes() -> None:
    stage_plan = build_stage_plan()
    metadata = load_fixture("timeline_graph.json")
    timeline_plan = build_extraction_plan(
        metadata,
        timeline3d_profile(),
        select_timeline_roots(metadata, [TIMELINE_ROOT]),
        package_id="streaming_live/0006_lon_vbs_01",
    )

    union = union_requirements([stage_plan, timeline_plan])
    names = [ref.bundle_name for ref in union]
    assert names == sorted(set(names))
    assert "scene3d/shader/custom_lil_001" in names
    assert "character/model/char_001_unit" in names


def test_standard_profile_keeps_single_bundle_without_closure() -> None:
    metadata = load_fixture("stage_graph.json")
    plan = build_extraction_plan(
        metadata,
        standard_profile(),
        select_stage_roots(metadata, [STAGE_ROOT]),
        package_id="standard/scene3d/stage/base_007_sp_live",
    )

    assert plan.bundle_names() == (STAGE_ROOT,)
    assert plan.dependency_edges == ()
    assert plan.purpose == STANDARD_PURPOSE


def test_live2d_profile_allows_explicit_inputs_without_roots() -> None:
    metadata = load_fixture("stage_graph.json")
    plan = build_extraction_plan(
        metadata,
        live2d_profile(),
        (),
        package_id="live2d/explicit/inputs",
    )

    assert plan.roots == ()
    assert plan.bundles == ()
    assert plan.purpose == "live2d"


def test_composite_roots_union_closures_with_explicit_specs() -> None:
    metadata = load_fixture("timeline_graph.json")
    roots = select_composite_roots(
        metadata,
        [
            ("stage", "stage", STAGE_ROOT),
            ("timeline", "timeline", TIMELINE_ROOT),
        ],
    )
    plan = build_extraction_plan(
        metadata, composite_profile(), roots, package_id="streaming_live/0006/composite"
    )

    assert plan.purpose == "composite"
    assert len(plan.roots) == 2
    assert "character/model/char_001_unit" in set(plan.bundle_names())
    assert validate_plan(plan, composite_profile()) == ()


def test_selectors_reject_missing_and_partial_bundle_names() -> None:
    metadata = load_fixture("stage_graph.json")

    # Exact names only: a substring of the real bundle name must not select.
    with pytest.raises(PlanValidationError, match="base_007"):
        build_extraction_plan(
            metadata,
            scene3d_profile(),
            select_stage_roots(metadata, ["base_007"]),
            package_id="streaming_live/substring/stage",
        )
    with pytest.raises(PlanValidationError, match="absent from the metadata index"):
        build_extraction_plan(
            metadata,
            scene3d_profile(),
            select_stage_roots(metadata, ["scene3d/stage/does_not_exist"]),
            package_id="streaming_live/missing/stage",
        )


def test_composite_root_spec_requires_exact_bundle_too() -> None:
    metadata = load_fixture("timeline_graph.json")
    roots = select_composite_roots(metadata, [("timeline", "timeline", TIMELINE_ROOT)])
    with pytest.raises(PlanValidationError, match="absent from the metadata index"):
        build_extraction_plan(
            metadata,
            composite_profile(),
            (*roots, *select_composite_roots(metadata, [("x", "stage", "scene3d/stage/nope")])),
            package_id="streaming_live/composite_bad/stage",
        )


def test_disabled_flags_mean_planner_never_runs_in_pipeline() -> None:
    assert not resolve_glb_flags(__import__("types").SimpleNamespace()).any_enabled
    # The planner is a pure library: the pipeline imports none of it, and the
    # standard per-Bundle path is exercised unchanged by the existing suites.
    import updater.pipeline as pipeline_module

    source = Path(pipeline_module.__file__).read_text(encoding="utf-8")
    assert "glb" not in source


def test_animation_edge_reason_is_declared_for_timeline_tracks() -> None:
    assert EDGE_ANIMATION == "animation"
    profile = timeline3d_profile(
        reason_overrides=((re.compile(r"character/model/"), EDGE_ANIMATION),)
    )
    metadata = load_fixture("timeline_graph.json")
    plan = build_extraction_plan(
        metadata,
        profile,
        select_timeline_roots(metadata, [TIMELINE_ROOT]),
        package_id="streaming_live/0006_lon_vbs_01",
    )
    reasons = {
        edge.reason
        for edge in plan.dependency_edges
        if edge.target == "character/model/char_001_unit"
    }
    assert reasons == {EDGE_ANIMATION}


def test_planner_logs_structured_plan_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="asset_updater"):
        build_stage_plan()

    assert any("action=plan_built" in record.getMessage() for record in caplog.records)
