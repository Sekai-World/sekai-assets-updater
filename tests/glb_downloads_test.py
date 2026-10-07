"""GLB Phase 2 tests: closure/download integration and package readiness (#36)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from updater.extract.glb import (
    DIAG_BUNDLE_DOWNLOAD_FAILED,
    DIAG_CACHE_MISSING,
    SCENE3D_PURPOSE,
    TIMELINE3D_PURPOSE,
    association_bytes,
    canonical_json_bytes,
    download_failure_diagnostic,
    evaluate_package_readiness,
    load_plan_associations,
    persist_plan_associations,
    plan_download_requirements,
    requirement_names,
)
from updater.extract.glb.planner import (
    build_extraction_plan,
    scene3d_profile,
    select_stage_roots,
    select_timeline_roots,
    timeline3d_profile,
)
from updater.net.plan import (
    get_bundle_checksum,
    get_download_list,
    select_bundles_for_download,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "streaming_live"
STAGE_ROOT = "scene3d/stage/base_007_sp_live"
TIMELINE_ROOT = "streaming_live/timeline/0006_lon_vbs_01"
SHARED_SHADER = "scene3d/shader/custom_lil_001"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def stage_plan(metadata: dict | None = None):
    metadata = metadata if metadata is not None else load_fixture("stage_graph.json")
    return build_extraction_plan(
        metadata,
        scene3d_profile(),
        select_stage_roots(metadata, [STAGE_ROOT]),
        package_id="streaming_live/0006_lon_vbs_01/stage",
    )


def timeline_plan(metadata: dict | None = None):
    metadata = metadata if metadata is not None else load_fixture("timeline_graph.json")
    return build_extraction_plan(
        metadata,
        timeline3d_profile(),
        select_timeline_roots(metadata, [TIMELINE_ROOT]),
        package_id="streaming_live/0006_lon_vbs_01",
    )


def test_union_requirements_dedupe_shared_dependencies() -> None:
    requirements = plan_download_requirements([stage_plan(), timeline_plan()])
    names = [ref.bundle_name for ref in requirements]

    assert names == sorted(set(names))
    assert names.count(SHARED_SHADER) == 1
    assert requirement_names([stage_plan(), timeline_plan()]) == tuple(names)


def test_required_names_bypass_include_and_exclude_filters() -> None:
    bundles = load_fixture("stage_graph.json")
    excluded_by_user = select_bundles_for_download(
        bundles,
        include_list=[r"^scene3d/stage/"],
        exclude_list=[r"^scene3d/shader/"],
    )
    assert SHARED_SHADER not in excluded_by_user

    with_required = select_bundles_for_download(
        bundles,
        include_list=[r"^scene3d/stage/"],
        exclude_list=[r"^scene3d/shader/"],
        required_bundle_names=[SHARED_SHADER],
    )
    assert SHARED_SHADER in with_required
    # Standard filtering still applies to everything else.
    assert "scene3d/texture/tex_stage_common" not in with_required


def test_default_selection_is_unchanged_without_required_names() -> None:
    bundles = load_fixture("stage_graph.json")

    assert select_bundles_for_download(bundles) == select_bundles_for_download(
        bundles, required_bundle_names=[]
    )


def _incremental_config(tmp_path: Path):
    from types import SimpleNamespace

    return SimpleNamespace(
        ASSET_BUNDLE_INFO_CACHE_PATH=tmp_path / "metadata.json",
        GAME_VERSION_JSON_CACHE_PATH=tmp_path / "version.json",
        ASSET_BUNDLE_URL="https://example.invalid/{bundleName}",
        APP_VERSION_OVERRIDE=None,
    )


async def _noop_resolver(_bundle):
    return None


@pytest.mark.parametrize(
    "missing_cache_bundle", [SHARED_SHADER, "scene3d/texture/tex_stage_common"]
)
def test_changed_or_missing_closure_member_downloads_without_unrelated_bundles(
    tmp_path: Path, missing_cache_bundle: str
) -> None:
    metadata = load_fixture("stage_graph.json")
    # Cached metadata matches current except the closure member under test,
    # whose checksum changed upstream.  Every cached record carries a provenance
    # marker proving it was already processed at its current checksum, so only
    # genuine changes trigger a download.
    cached = json.loads(json.dumps(metadata))
    for bundle in cached.values():
        field, value = get_bundle_checksum(bundle)
        bundle["_sekai_assets_updater"] = {"processed_checksum": {"field": field, "value": value}}
    if missing_cache_bundle == SHARED_SHADER:
        cached[SHARED_SHADER]["hash"] = "stale-hash"

    config = _incremental_config(tmp_path)
    (tmp_path / "metadata.json").write_text(json.dumps({"bundles": cached}))
    (tmp_path / "version.json").write_text(json.dumps({"appVersion": "6.8.0"}))

    def resolver(bundle):
        name = bundle.get("bundleName")
        cache_file = tmp_path / "cache" / f"{name}.bundle"
        if name == missing_cache_bundle:
            # Absent on disk: reported as changed and re-downloaded.
            return cache_file
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"cached payload")
        return cache_file

    plan = asyncio.run(
        get_download_list(
            {"bundles": metadata, "version": "", "os": ""},
            {"appVersion": "6.8.0"},
            config=config,
            bundle_cache_path_resolver=resolver,
            required_bundle_names=requirement_names([stage_plan(metadata)]),
        )
    )
    names = [item[1]["bundleName"] for item in plan.candidates]

    assert names == [missing_cache_bundle]


def test_readiness_ready_when_all_closure_members_cached_and_unchanged(
    tmp_path: Path,
) -> None:
    metadata = load_fixture("stage_graph.json")
    plan = stage_plan(metadata)

    def resolver(name: str) -> Path:
        return tmp_path / "cache" / f"{name.replace('/', '_')}.bundle"

    for name in plan.bundle_names():
        resolver(name).parent.mkdir(parents=True, exist_ok=True)
        resolver(name).write_bytes(b"payload")

    readiness = evaluate_package_readiness(
        plan, current_bundles=metadata, cache_path_resolver=resolver
    )

    assert readiness.package_id == plan.package_id
    assert readiness.collection_id == plan.collection_id()
    assert readiness.ready
    assert readiness.missing_bundles == ()
    assert readiness.changed_bundles == ()
    assert readiness.diagnostics == ()


def test_readiness_reports_missing_cache_files(tmp_path: Path) -> None:
    metadata = load_fixture("stage_graph.json")
    plan = stage_plan(metadata)

    readiness = evaluate_package_readiness(
        plan, current_bundles=metadata, cache_path_resolver=lambda _name: None
    )

    assert not readiness.ready
    assert set(readiness.missing_bundles) == set(plan.bundle_names())
    assert all(
        d.code == DIAG_CACHE_MISSING and d.severity == "error" for d in readiness.diagnostics
    )


def test_readiness_reports_changed_checksums_as_redownload_required(
    tmp_path: Path,
) -> None:
    metadata = load_fixture("stage_graph.json")
    plan = stage_plan(metadata)
    stale = json.loads(json.dumps(metadata))
    stale[SHARED_SHADER]["hash"] = "moved-on"

    def resolver(name: str) -> Path:
        path = tmp_path / "cache" / f"{name.replace('/', '_')}.bundle"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"payload")
        return path

    readiness = evaluate_package_readiness(
        plan, current_bundles=stale, cache_path_resolver=resolver
    )

    assert not readiness.ready
    assert readiness.changed_bundles == (SHARED_SHADER,)
    assert readiness.missing_bundles == ()
    assert readiness.diagnostics[0].code == DIAG_BUNDLE_DOWNLOAD_FAILED


def test_readiness_absent_from_current_metadata_is_missing() -> None:
    metadata = load_fixture("stage_graph.json")
    plan = stage_plan(metadata)
    shrunken = {name: record for name, record in metadata.items() if name != SHARED_SHADER}

    readiness = evaluate_package_readiness(
        plan, current_bundles=shrunken, cache_path_resolver=lambda _name: None
    )

    assert not readiness.ready
    assert SHARED_SHADER in readiness.missing_bundles


def test_failed_download_leaves_retryable_diagnostic_and_no_publication(tmp_path: Path) -> None:
    metadata = load_fixture("stage_graph.json")
    plan = stage_plan(metadata)

    diagnostic = download_failure_diagnostic(plan, SHARED_SHADER)
    assert diagnostic.code == DIAG_BUNDLE_DOWNLOAD_FAILED
    assert diagnostic.severity == "error"
    assert diagnostic.phase == "download"
    assert SHARED_SHADER in diagnostic.message
    assert plan.package_id in diagnostic.message

    # The failed download keeps the package not-ready; retrying the run
    # re-evaluates the same readiness contract.
    readiness = evaluate_package_readiness(
        plan, current_bundles=metadata, cache_path_resolver=lambda _name: None
    )
    assert not readiness.ready


def test_plan_associations_round_trip_and_survive_cache_invalidation(
    tmp_path: Path,
) -> None:
    plans = (stage_plan(), timeline_plan())
    association_path = tmp_path / "state" / "glb-plans.json"
    association_path.parent.mkdir(parents=True, exist_ok=True)

    persist_plan_associations(plans, association_path)
    loaded = load_plan_associations(association_path)

    assert loaded is not None
    assert loaded["association_version"] == 1
    assert set(loaded["plans"]) == {plan.package_id for plan in plans}
    for plan in plans:
        record = loaded["plans"][plan.package_id]
        assert record["collection_id"] == plan.collection_id()
        assert record["bundle_names"] == list(plan.bundle_names())
    # The association document is deterministic and stored beside, never
    # inside, the Bundle cache metadata.
    assert association_bytes(plans) == canonical_json_bytes(loaded)
    assert "bundles" not in loaded["plans"][plans[0].package_id]


def test_load_plan_associations_handles_missing_and_corrupt_files(
    tmp_path: Path,
) -> None:
    assert load_plan_associations(tmp_path / "absent.json") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert load_plan_associations(corrupt) is None

    wrong_version = tmp_path / "wrong.json"
    wrong_version.write_text(json.dumps({"association_version": 99, "plans": {}}), encoding="utf-8")
    assert load_plan_associations(wrong_version) is None


def test_package_purposes_flow_into_associations(tmp_path: Path) -> None:
    plans = (stage_plan(), timeline_plan())
    association_path = tmp_path / "glb-plans.json"

    persist_plan_associations(plans, association_path)
    loaded = load_plan_associations(association_path)

    assert loaded["plans"][plans[0].package_id]["purpose"] == SCENE3D_PURPOSE
    assert loaded["plans"][plans[1].package_id]["purpose"] == TIMELINE3D_PURPOSE
