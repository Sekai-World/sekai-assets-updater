"""GLB Phase 6 tests: package manifests, offline validation, publication (#40)."""

from __future__ import annotations

import pytest

from updater.extract.glb.animation import ANIMATION_ARTIFACT_VERSION
from updater.extract.glb.gltf_writer import GlbScene, content_hash
from updater.extract.glb.package import (
    PACKAGE_MANIFEST_URL,
    TIMELINE_PLAYABLE_URL,
    PackageArtifact,
    PublicationRefused,
    animation_artifact_url,
    build_composite_manifest,
    build_release_manifest,
    glb_artifact_url,
    load_release_manifest,
    materialize_package,
    persist_release_manifest,
    publish_package,
    validate_package_completeness,
    validate_package_manifest,
    validate_package_references,
)
from updater.extract.glb.timeline import _document_hash

CONVERSION_METADATA = {
    "conversion_rules": {"handedness_conversion": "mirror_x"},
    "generator": "test/1",
}


def _timeline_document(*, with_reference: bool = True):
    reference = {
        "bundle_name": "motion/bundle",
        "file_index": 1,
        "path_id": 400,
        "clip_name": "dance_loop",
        "source_track_path_ids": [202],
    }
    document = {
        "artifact_version": 1,
        "kind": "timeline_playable",
        "package_id": "pkg/mc_x",
        "bundle_name": "timeline/bundle",
        "file_index": 0,
        "path_id": 100,
        "container_path": "ondemand/mc_x.playable",
        "timeline_name": "mc_x",
        "duration_mode": 1,
        "fixed_duration": 4.5,
        "duration": 2.5,
        "characters": [],
        "tracks": [
            {
                "track_class": "AnimationTrack",
                "track_name": "dance",
                "file_index": 0,
                "path_id": 202,
                "supported": True,
                "clips": [
                    {
                        "start": 0.0,
                        "duration": 2.5,
                        "end": 2.5,
                        "clip_in": 0.0,
                        "time_scale": 1.0,
                        "display_name": "Dance",
                        "asset": {"file_id": 0, "path_id": 301, "resolved": True},
                        "asset_data": {},
                        "resolved_references": [],
                    }
                ],
            }
        ],
        "animation_references": [reference] if with_reference else [],
        "diagnostics": [],
    }
    document["content_hash"] = _document_hash(document)
    return document


def _animation_artifact_bytes() -> bytes:
    import json

    document = {
        "artifact_version": ANIMATION_ARTIFACT_VERSION,
        "bundle_name": "motion/bundle",
        "file_index": 1,
        "path_id": 400,
        "clip_name": "dance_loop",
        "sample_rate": 60.0,
        "duration": 2.5,
        "curves": [],
        "streams": {
            "streamed": [],
            "dense": {
                "curve_count": 0,
                "frame_count": 0,
                "sample_rate": 0.0,
                "begin_time": 0.0,
                "frames": [],
            },
            "constant": [],
        },
        "diagnostics": [],
    }
    document["content_hash"] = content_hash(json.dumps(document).encode("utf-8"))
    return json.dumps(document, sort_keys=True).encode("utf-8")


def _glb_bytes() -> bytes:
    scene = GlbScene()
    root = scene.add_node({"name": "root"})
    return scene.encode(root_nodes=[root], generator="test")


def _artifacts(*, with_animation: bool = True):
    import json

    timeline_bytes = json.dumps(_timeline_document(), ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    artifacts = [
        PackageArtifact(url=glb_artifact_url("root_a"), kind="glb", payload=_glb_bytes()),
        PackageArtifact(
            url=TIMELINE_PLAYABLE_URL, kind="timeline_playable_document", payload=timeline_bytes
        ),
    ]
    if with_animation:
        artifacts.append(
            PackageArtifact(
                url=animation_artifact_url(
                    {
                        "bundle_name": "motion/bundle",
                        "file_index": 1,
                        "path_id": 400,
                        "clip_name": "dance_loop",
                    }
                ),
                kind="animation_artifact",
                payload=_animation_artifact_bytes(),
            )
        )
    return artifacts


def _manifest(**overrides):
    kwargs = dict(
        package_id="pkg/mc_x",
        purpose="timeline3d",
        profile="timeline3d",
        unity_version="2022.3.21f1",
        adapter_version="test-1",
        source_bundles=[
            {"bundle_name": "timeline/bundle", "sha256": "a" * 64},
            {"bundle_name": "motion/bundle", "sha256": "b" * 64},
        ],
        artifacts=_artifacts(),
        timeline_document=_timeline_document(),
        glb_roots=[
            {
                "root_id": "stage_root",
                "bundle_name": "timeline/bundle",
                "container_path": "ondemand/mc_x.playable",
                "glb_url": glb_artifact_url("root_a"),
                "export_manifest_url": "export_manifests/root_a.json",
            }
        ],
        conversion_metadata=CONVERSION_METADATA,
        diagnostics=[],
        incomplete_reasons=[],
    )
    kwargs.update(overrides)
    return build_release_manifest(**kwargs)


def test_release_manifest_builds_and_validates_with_timeline_summary() -> None:
    manifest = _manifest()

    assert manifest["publishable"] is True
    assert manifest["incomplete_reasons"] == []
    validate_package_manifest(manifest)
    timeline = manifest["timeline"]
    assert timeline["url"] == TIMELINE_PLAYABLE_URL
    assert timeline["timeline_name"] == "mc_x"
    assert timeline["track_count"] == 1
    assert timeline["clip_count"] == 1
    assert timeline["animation_reference_count"] == 1
    urls = [record["url"] for record in manifest["artifacts"]]
    assert urls == sorted(urls)  # deterministic order
    # manifest.json itself is never an artifact — it cannot hash itself.
    assert PACKAGE_MANIFEST_URL not in urls


def test_release_manifest_requires_the_playable_artifact() -> None:
    artifacts = [a for a in _artifacts() if a.url != TIMELINE_PLAYABLE_URL]
    with pytest.raises(ValueError, match="exactly one timeline.playable.json"):
        build_release_manifest(
            package_id="pkg",
            purpose="timeline3d",
            profile="timeline3d",
            unity_version="2022.3.21f1",
            adapter_version="test-1",
            source_bundles=[],
            artifacts=artifacts,
            timeline_document=_timeline_document(),
            glb_roots=[],
            conversion_metadata=CONVERSION_METADATA,
            diagnostics=[],
            incomplete_reasons=[],
        )


def test_release_manifest_rejects_unsafe_and_duplicate_urls() -> None:
    from updater.extract.glb import PlanValidationError

    manifest = _manifest()
    escaped = dict(manifest)
    escaped["artifacts"] = manifest["artifacts"] + [
        {"url": "../evil.bin", "kind": "glb", "sha256": "a" * 64, "byte_length": 1}
    ]
    with pytest.raises(PlanValidationError):
        validate_package_manifest(escaped)
    duplicated = dict(manifest)
    duplicated["artifacts"] = manifest["artifacts"] + [manifest["artifacts"][0]]
    with pytest.raises(ValueError, match="duplicate artifact URL"):
        validate_package_manifest(duplicated)


def test_materialize_and_offline_validator_accept_complete_package(tmp_path) -> None:
    manifest = _manifest()
    artifacts = _artifacts()

    materialize_package(manifest, artifacts, tmp_path)

    completeness = validate_package_completeness(manifest, tmp_path)
    assert completeness.publishable, completeness.problems
    assert completeness.problems == []
    assert (tmp_path / PACKAGE_MANIFEST_URL).is_file()
    assert (tmp_path / "glb/root_a.glb").is_file()
    # manifest.json is the root contract, not a listed artifact.
    urls = {record["url"] for record in manifest["artifacts"]}
    assert PACKAGE_MANIFEST_URL not in urls


def test_offline_validator_rejects_corruption_and_missing_files(tmp_path) -> None:
    manifest = _manifest()
    artifacts = _artifacts()
    materialize_package(manifest, artifacts, tmp_path)

    glb_path = tmp_path / "glb/root_a.glb"
    payload = bytearray(glb_path.read_bytes())
    payload[-1] ^= 0xFF
    glb_path.write_bytes(bytes(payload))
    problems = validate_package_completeness(manifest, tmp_path).problems
    assert any("sha256 mismatch" in problem for problem in problems)

    (tmp_path / TIMELINE_PLAYABLE_URL).unlink()
    problems = validate_package_completeness(manifest, tmp_path).problems
    assert any("missing from the package directory" in problem for problem in problems)
    assert not validate_package_completeness(manifest, tmp_path).publishable


def test_manifest_round_trip_rejects_corruption(tmp_path) -> None:
    manifest = _manifest()
    path = tmp_path / PACKAGE_MANIFEST_URL
    persist_release_manifest(path, manifest)
    assert load_release_manifest(path) == manifest
    assert load_release_manifest(tmp_path / "absent.json") is None
    path.write_text("{broken", encoding="utf-8")
    assert load_release_manifest(path) is None


def test_publication_uploads_artifacts_then_manifests_last() -> None:
    manifest = _manifest()
    artifacts = _artifacts()
    uploaded: list[str] = []

    publish_package(manifest, artifacts, lambda url, _payload: uploaded.append(url))

    assert uploaded[-1] == PACKAGE_MANIFEST_URL
    assert uploaded[-2] == TIMELINE_PLAYABLE_URL
    assert set(uploaded[:-2]) == {a.url for a in artifacts if a.url not in (TIMELINE_PLAYABLE_URL,)}


def test_unpublishable_package_never_reaches_remote_storage() -> None:
    manifest = _manifest(incomplete_reasons=["missing character GLB for track dance"])
    uploaded: list[str] = []
    with pytest.raises(PublicationRefused, match="missing character GLB"):
        publish_package(manifest, _artifacts(), lambda url, _payload: uploaded.append(url))
    assert uploaded == []

    # A payload/hash disagreement refuses publication too.
    tampered = PackageArtifact(url=glb_artifact_url("root_a"), kind="glb", payload=b"tampered")
    artifacts = [tampered if a.url == tampered.url else a for a in _artifacts()]
    with pytest.raises(PublicationRefused, match="does not match its manifest hash"):
        publish_package(_manifest(), artifacts, lambda url, _payload: uploaded.append(url))
    assert uploaded == []


def test_reference_validation_catches_undeclared_animation_artifacts() -> None:
    manifest = _manifest()
    document = _timeline_document()
    assert validate_package_references(manifest, document) == []

    stripped = build_release_manifest(
        package_id="pkg/mc_x",
        purpose="timeline3d",
        profile="timeline3d",
        unity_version="2022.3.21f1",
        adapter_version="test-1",
        source_bundles=[{"bundle_name": "timeline/bundle", "sha256": "a" * 64}],
        artifacts=[a for a in _artifacts() if a.kind != "animation_artifact"],
        timeline_document=document,
        glb_roots=[],
        conversion_metadata=CONVERSION_METADATA,
        diagnostics=[],
        incomplete_reasons=[],
    )
    problems = validate_package_references(stripped, document)
    assert any("undeclared artifact" in problem for problem in problems)

    wrong_summary = dict(document)
    wrong_summary["tracks"] = []
    wrong_summary["content_hash"] = _document_hash(wrong_summary)
    problems = validate_package_references(manifest, wrong_summary)
    assert any("disagrees" in problem for problem in problems)


def test_composite_unions_children_without_duplicating_artifacts() -> None:
    first = _manifest()
    second = build_release_manifest(
        package_id="pkg/mc_y",
        purpose="timeline3d",
        profile="timeline3d",
        unity_version="2022.3.21f1",
        adapter_version="test-1",
        source_bundles=[
            {"bundle_name": "timeline/bundle", "sha256": "a" * 64},
            {"bundle_name": "other/bundle", "sha256": "c" * 64},
        ],
        artifacts=_artifacts(),  # shares every artifact URL and content
        timeline_document=_timeline_document(),
        glb_roots=[],
        conversion_metadata=CONVERSION_METADATA,
        diagnostics=[],
        incomplete_reasons=[],
    )

    composite = build_composite_manifest(
        package_id="pkg/composite",
        child_manifests=[first, second],
        conversion_metadata=CONVERSION_METADATA,
    )

    urls = [record["url"] for record in composite["artifacts"]]
    assert len(urls) == len(set(urls))  # no duplicate physical files
    assert len(urls) == len(first["artifacts"])
    assert composite["publishable"] is True
    child_ids = [child["package_id"] for child in composite["child_packages"]]
    assert child_ids == ["pkg/mc_x", "pkg/mc_y"]
    purposes = {child["package_id"]: child["purpose"] for child in composite["child_packages"]}
    assert purposes == {"pkg/mc_x": "timeline3d", "pkg/mc_y": "timeline3d"}
    shared = next(
        record
        for record in composite["source_bundles"]
        if record["bundle_name"] == "timeline/bundle"
    )
    assert shared["provided_by"] == ["pkg/mc_x", "pkg/mc_y"]
    assert [t["package_id"] for t in composite["timelines"]] == ["pkg/mc_x", "pkg/mc_y"]


def test_composite_conflicts_and_unpublishable_children_propagate() -> None:
    first = _manifest()
    conflicting = build_release_manifest(
        package_id="pkg/mc_y",
        purpose="timeline3d",
        profile="timeline3d",
        unity_version="2022.3.21f1",
        adapter_version="test-1",
        source_bundles=[],
        artifacts=[
            PackageArtifact(url=glb_artifact_url("root_a"), kind="glb", payload=b"different"),
            PackageArtifact(
                url=TIMELINE_PLAYABLE_URL,
                kind="timeline_playable_document",
                payload=__import__("json").dumps(_timeline_document()).encode("utf-8"),
            ),
        ],
        timeline_document=_timeline_document(),
        glb_roots=[],
        conversion_metadata=CONVERSION_METADATA,
        diagnostics=[],
        incomplete_reasons=[],
    )
    composite = build_composite_manifest(
        package_id="pkg/composite",
        child_manifests=[first, conflicting],
        conversion_metadata=CONVERSION_METADATA,
    )
    assert composite["publishable"] is False
    assert any("conflicts" in reason for reason in composite["incomplete_reasons"])

    incomplete_child = _manifest(package_id="pkg/mc_z", incomplete_reasons=["stage GLB refused"])
    composite = build_composite_manifest(
        package_id="pkg/composite",
        child_manifests=[first, incomplete_child],
        conversion_metadata=CONVERSION_METADATA,
    )
    assert composite["publishable"] is False
    assert "pkg/mc_z: stage GLB refused" in composite["incomplete_reasons"]

    with pytest.raises(ValueError, match="unique"):
        build_composite_manifest(
            package_id="pkg/composite",
            child_manifests=[first, first],
            conversion_metadata=CONVERSION_METADATA,
        )
