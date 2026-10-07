"""GLB Phase 0 contract, fixture, and feature-flag tests (#34)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from updater.extract.glb import (
    COMPOSITE_PURPOSE,
    DIAG_DUPLICATE_BUNDLE,
    DIAG_EDGE_UNKNOWN_BUNDLE,
    DIAG_ROOT_BUNDLE_MISSING,
    DIAG_UNKNOWN_PURPOSE,
    DIAG_UNSAFE_PATH,
    EDGE_METADATA,
    EDGE_SHADER,
    EDGE_TEXTURE,
    EXTRACTION_PURPOSES,
    FLAG_NAMES,
    SCENE3D_PURPOSE,
    SEVERITIES,
    TIMELINE3D_PURPOSE,
    BundleRef,
    DependencyEdge,
    ExtractionPlan,
    GLBFlags,
    PlanDiagnostic,
    PlanValidationError,
    RootRef,
    canonical_json_bytes,
    log_plan_built,
    log_plan_diagnostics,
    resolve_glb_flags,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "streaming_live"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def stage_plan_from_fixture() -> ExtractionPlan:
    graph = load_fixture("stage_graph.json")
    root = "scene3d/stage/base_007_sp_live"
    closure = {root}
    changed = True
    while changed:
        changed = False
        for name in sorted(closure):
            for dependency in graph[name]["dependencies"]:
                if dependency not in closure:
                    closure.add(dependency)
                    changed = True
    return ExtractionPlan(
        package_id="streaming_live/0006_lon_vbs_01/stage",
        purpose=SCENE3D_PURPOSE,
        profile="streaming_live_v1",
        roots=(RootRef(root_id="stage", kind="stage", bundle_name=root),),
        bundles=tuple(
            BundleRef(
                bundle_name=name,
                checksum=graph[name]["hash"],
                source="metadata",
                role="root" if name == root else "dependency",
            )
            for name in closure
        ),
        dependency_edges=tuple(
            DependencyEdge(source=parent, target=dependency, reason=EDGE_METADATA)
            for parent in closure
            for dependency in graph[parent]["dependencies"]
        ),
    )


def test_plan_rejects_unknown_purpose() -> None:
    with pytest.raises(PlanValidationError, match="purpose") as exc_info:
        ExtractionPlan(
            package_id="pkg/one",
            purpose="motion",
            profile="p",
            roots=(RootRef(root_id="r", kind="stage", bundle_name="b"),),
            bundles=(BundleRef(bundle_name="b"),),
        )
    assert exc_info.value.code == DIAG_UNKNOWN_PURPOSE


def test_plan_rejects_duplicate_bundle_identities() -> None:
    with pytest.raises(PlanValidationError, match="duplicate bundle") as exc_info:
        ExtractionPlan(
            package_id="pkg/one",
            purpose=SCENE3D_PURPOSE,
            profile="p",
            roots=(RootRef(root_id="r", kind="stage", bundle_name="b"),),
            bundles=(
                BundleRef(bundle_name="b", checksum="1"),
                BundleRef(bundle_name="b", checksum="2"),
            ),
        )
    assert exc_info.value.code == DIAG_DUPLICATE_BUNDLE


def test_plan_rejects_duplicate_root_ids() -> None:
    with pytest.raises(PlanValidationError, match="duplicate root"):
        ExtractionPlan(
            package_id="pkg/one",
            purpose=SCENE3D_PURPOSE,
            profile="p",
            roots=(
                RootRef(root_id="stage", kind="stage", bundle_name="b"),
                RootRef(root_id="stage", kind="stage", bundle_name="c"),
            ),
            bundles=(BundleRef(bundle_name="b"), BundleRef(bundle_name="c")),
        )


def test_plan_rejects_root_without_owning_bundle() -> None:
    with pytest.raises(PlanValidationError) as exc_info:
        ExtractionPlan(
            package_id="pkg/one",
            purpose=SCENE3D_PURPOSE,
            profile="p",
            roots=(RootRef(root_id="r", kind="stage", bundle_name="missing"),),
            bundles=(BundleRef(bundle_name="b"),),
        )
    assert exc_info.value.code == DIAG_ROOT_BUNDLE_MISSING


@pytest.mark.parametrize(
    "package_id",
    [
        "/absolute/path",
        "trailing/",
        "../escape",
        "a/../b",
        "back\\slash",
        "a//b",
        "",
        None,
        "control\x00char",
    ],
)
def test_plan_rejects_unsafe_package_paths(package_id) -> None:
    with pytest.raises(PlanValidationError) as exc_info:
        ExtractionPlan(
            package_id=package_id,
            purpose=SCENE3D_PURPOSE,
            profile="p",
            roots=(RootRef(root_id="r", kind="stage", bundle_name="b"),),
            bundles=(BundleRef(bundle_name="b"),),
        )
    assert exc_info.value.code == DIAG_UNSAFE_PATH


def test_plan_rejects_edge_referencing_unknown_bundle() -> None:
    with pytest.raises(PlanValidationError) as exc_info:
        ExtractionPlan(
            package_id="pkg/one",
            purpose=SCENE3D_PURPOSE,
            profile="p",
            roots=(RootRef(root_id="r", kind="stage", bundle_name="b"),),
            bundles=(BundleRef(bundle_name="b"),),
            dependency_edges=(DependencyEdge(source="b", target="ghost", reason=EDGE_METADATA),),
        )
    assert exc_info.value.code == DIAG_EDGE_UNKNOWN_BUNDLE


def test_plan_rejects_unknown_edge_reason_and_severity() -> None:
    with pytest.raises(PlanValidationError, match="reason"):
        DependencyEdge(source="a", target="b", reason="vibes")
    with pytest.raises(PlanValidationError, match="severity"):
        PlanDiagnostic(code="c", severity="fatal", phase="plan")


def test_stage_fixture_builds_expected_closure() -> None:
    plan = stage_plan_from_fixture()

    assert plan.bundle_names() == (
        "scene3d/shader/custom_lil_001",
        "scene3d/stage/base_007_sp_live",
        "scene3d/stage/shared_base_stage",
        "scene3d/texture/tex_stage_common",
    )
    edge_pairs = {(edge.source, edge.target) for edge in plan.dependency_edges}
    assert ("scene3d/stage/base_007_sp_live", "scene3d/shader/custom_lil_001") in edge_pairs
    assert ("scene3d/stage/shared_base_stage", "scene3d/shader/custom_lil_001") in edge_pairs


def test_fixture_plan_is_byte_deterministic_across_runs_and_orderings() -> None:
    first = stage_plan_from_fixture()
    second = stage_plan_from_fixture()
    assert first.canonical_json_bytes() == second.canonical_json_bytes()

    graph = load_fixture("stage_graph.json")
    root = "scene3d/stage/base_007_sp_live"
    names = sorted(graph)
    reversed_plan = ExtractionPlan(
        package_id="streaming_live/0006_lon_vbs_01/stage",
        purpose=SCENE3D_PURPOSE,
        profile="streaming_live_v1",
        roots=(RootRef(root_id="stage", kind="stage", bundle_name=root),),
        bundles=tuple(
            BundleRef(
                bundle_name=name,
                checksum=graph[name]["hash"],
                source="metadata",
                role="root" if name == root else "dependency",
            )
            for name in names[::-1]
        ),
        dependency_edges=tuple(
            DependencyEdge(source=parent, target=dependency, reason=EDGE_METADATA)
            for parent in names[::-1]
            for dependency in graph[parent]["dependencies"][::-1]
        ),
    )
    assert first.canonical_json_bytes() == reversed_plan.canonical_json_bytes()
    assert first.collection_id() != ""
    assert canonical_json_bytes(first.to_dict()) == first.canonical_json_bytes()


def test_collection_id_ignores_diagnostics_but_tracks_inputs() -> None:
    base = ExtractionPlan(
        package_id="pkg/one",
        purpose=TIMELINE3D_PURPOSE,
        profile="p",
        roots=(RootRef(root_id="r", kind="timeline", bundle_name="b"),),
        bundles=(BundleRef(bundle_name="b", checksum="1"),),
    )
    warned = ExtractionPlan(
        package_id="pkg/one",
        purpose=TIMELINE3D_PURPOSE,
        profile="p",
        roots=(RootRef(root_id="r", kind="timeline", bundle_name="b"),),
        bundles=(BundleRef(bundle_name="b", checksum="1"),),
        diagnostics=(
            PlanDiagnostic(
                code="dependency_cycle", severity="warning", phase="plan", message="cycle"
            ),
        ),
    )
    changed = ExtractionPlan(
        package_id="pkg/one",
        purpose=TIMELINE3D_PURPOSE,
        profile="p",
        roots=(RootRef(root_id="r", kind="timeline", bundle_name="b"),),
        bundles=(BundleRef(bundle_name="b", checksum="2"),),
    )

    assert base.collection_id() == warned.collection_id()
    assert base.collection_id() != changed.collection_id()


@pytest.mark.parametrize(
    "name",
    [
        "stage_graph.json",
        "timeline_graph.json",
        "missing_dependency_graph.json",
        "cycle_graph.json",
    ],
)
def test_fixtures_load_and_cover_required_cases(name: str) -> None:
    graph = load_fixture(name)

    assert graph
    for record in graph.values():
        assert record["bundleName"] in graph
        assert isinstance(record["dependencies"], list)
        assert record["bundleName"] not in record["dependencies"]

    if name == "timeline_graph.json":
        # Character dependency and shared stage/shader closure are present.
        assert "character/model/char_001_unit" in graph
        assert "scene3d/shader/custom_lil_001" in graph
    if name == "missing_dependency_graph.json":
        referenced = graph["scene3d/stage/base_broken_shader"]["dependencies"]
        assert "scene3d/shader/removed_from_index" in referenced
        assert "scene3d/shader/removed_from_index" not in graph
    if name == "cycle_graph.json":
        dependencies = {name: set(record["dependencies"]) for name, record in graph.items()}
        assert dependencies["scene3d/stage/cycle_c"] & {"scene3d/stage/cycle_a"}


def test_fixtures_contain_no_transport_material() -> None:
    for path in FIXTURE_DIR.glob("*.json"):
        text = path.read_text(encoding="utf-8").lower()
        for marker in ("http", "://", "signature", "authorization", "cookie", "token", "secret"):
            assert marker not in text, f"{path.name} contains {marker!r}"


def test_plan_and_diagnostic_logging_is_structured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    plan = ExtractionPlan(
        package_id="streaming_live/0006_lon_vbs_01/stage",
        purpose=SCENE3D_PURPOSE,
        profile="streaming_live_v1",
        roots=(RootRef(root_id="stage", kind="stage", bundle_name="b"),),
        bundles=(BundleRef(bundle_name="b"), BundleRef(bundle_name="shared")),
        dependency_edges=(DependencyEdge(source="b", target="shared", reason=EDGE_SHADER),),
        missing_dependencies=(),
        diagnostics=(
            PlanDiagnostic(
                code="dependency_excluded",
                severity="warning",
                phase="plan",
                message="texture excluded by profile",
                root_id="stage",
                bundle_name="shared",
            ),
        ),
    )

    with caplog.at_level(logging.INFO, logger="asset_updater"):
        log_plan_built(plan)
        log_plan_diagnostics(plan)

    lines = [record.getMessage() for record in caplog.records]
    assert any("phase=plan" in line and "action=plan_built" in line for line in lines)
    built = next(line for line in lines if "plan_built" in line)
    assert "package=streaming_live/0006_lon_vbs_01/stage" in built
    assert "purpose=scene3d" in built
    assert "collection_id=" in built
    diagnostic_line = next(line for line in lines if "dependency_excluded" in line)
    assert "severity=warning" in diagnostic_line
    assert "root=stage" in diagnostic_line
    assert "bundle=shared" in diagnostic_line


def test_all_known_purposes_and_severities_are_declared() -> None:
    assert EXTRACTION_PURPOSES == {"standard", "live2d", "scene3d", "timeline3d", "composite"}
    assert COMPOSITE_PURPOSE in EXTRACTION_PURPOSES
    assert SEVERITIES == ("info", "warning", "error")
    assert EDGE_TEXTURE == "texture"


def test_default_flags_are_all_off() -> None:
    flags = resolve_glb_flags(SimpleNamespace())

    assert flags == GLBFlags()
    assert not flags.any_enabled


def test_config_example_disables_every_glb_flag() -> None:
    import importlib.util

    example_path = Path(__file__).resolve().parents[1] / "config.example.py"
    spec = importlib.util.spec_from_file_location("config_example", example_path)
    assert spec is not None and spec.loader is not None
    example_config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(example_config)

    for name in FLAG_NAMES:
        assert getattr(example_config, name, None) is False, f"{name} must default to False"


@pytest.mark.parametrize("flag", ["ENABLE_EXTRACTION_PLANS", "ALLOW_INCOMPLETE_EXTRACTION"])
def test_sub_flag_without_master_flag_fails_fast(flag: str) -> None:
    config = SimpleNamespace(**{flag: True})
    with pytest.raises(PlanValidationError, match="ENABLE_STREAMING_LIVE_GLB_PREPROCESSING"):
        resolve_glb_flags(config)


@pytest.mark.parametrize(
    "enabled",
    [
        {"ENABLE_STATIC_GLB_EXPORT": True},
        {"ENABLE_GLB_MATERIALS": True, "ENABLE_STATIC_GLB_EXPORT": True},
        {"ENABLE_TIMELINE_MANIFEST": True},
        {"ENABLE_MULTIBUNDLE_COLLECTIONS": True, "ENABLE_GLB_ANIMATIONS": True},
    ],
)
def test_flag_prerequisite_ordering_fails_fast(enabled: dict[str, bool]) -> None:
    config = SimpleNamespace(ENABLE_STREAMING_LIVE_GLB_PREPROCESSING=True, **enabled)

    with pytest.raises(PlanValidationError, match="earlier GLB phases"):
        resolve_glb_flags(config)


def test_ordered_enablement_resolves() -> None:
    config = SimpleNamespace(
        ENABLE_STREAMING_LIVE_GLB_PREPROCESSING=True,
        ENABLE_EXTRACTION_PLANS=True,
        ENABLE_MULTIBUNDLE_COLLECTIONS=True,
        ENABLE_STATIC_GLB_EXPORT=True,
        ALLOW_INCOMPLETE_EXTRACTION=True,
    )
    flags = resolve_glb_flags(config)

    assert flags.preprocessing
    assert flags.extraction_plans
    assert flags.multibundle_collections
    assert flags.static_glb_export
    assert not flags.glb_materials
    assert not flags.glb_animations
    assert not flags.timeline_manifest
    assert flags.allow_incomplete
