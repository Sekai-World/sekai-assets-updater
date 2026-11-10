"""GLB Phase 6 tests: package manifests, offline validation, publication (#40)."""

from __future__ import annotations

import pytest

from updater.extract.glb.animation import ANIMATION_ARTIFACT_VERSION, _artifact_hash
from updater.extract.glb.gltf_writer import GlbScene, content_hash
from updater.extract.glb.package import (
    PACKAGE_MANIFEST_URL,
    TIMELINE_PLAYABLE_URL,
    PackageArtifact,
    PublicationRefused,
    _package_payload_problems,
    animation_artifact_url,
    build_composite_manifest,
    build_release_manifest,
    glb_artifact_url,
    glb_export_manifest_url,
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
        "source_tracks": [{"file_index": 0, "path_id": 202}],
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
                        "asset": {
                            "file_id": 0,
                            "path_id": 301,
                            "resolved": True,
                            "bundle_name": "timeline/bundle",
                            "target_file_index": 0,
                            "target_path_id": 301,
                            "target_class": "CharacterSpawnClip",
                        },
                        "asset_data": {"m_Clip": {"m_FileID": 1, "m_PathID": 400}},
                        "resolved_references": (
                            [
                                {
                                    "field_path": "m_Clip",
                                    "file_id": 1,
                                    "path_id": 400,
                                    "resolved": True,
                                    "bundle_name": "motion/bundle",
                                    "target_file_index": 1,
                                    "target_path_id": 400,
                                    "target_class": "AnimationClip",
                                }
                            ]
                            if with_reference
                            else []
                        ),
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
    document["content_hash"] = _artifact_hash(document)
    return json.dumps(document, sort_keys=True).encode("utf-8")


def _glb_bytes() -> bytes:
    scene = GlbScene()
    root = scene.add_node({"name": "root"})
    return scene.encode(root_nodes=[root], generator="test")


def _export_manifest_bytes(
    glb_bytes: bytes,
    *,
    publishable: bool = True,
    findings_by_code: dict[str, int] | None = None,
    findings_by_severity: dict[str, dict[str, int]] | None = None,
    diagnostics: list[dict] | None = None,
    root_bundle_name: str = "timeline/bundle",
    root_file_index: int = 0,
    root_path_id: int = 100,
) -> bytes:
    import json

    document = {
        "export_version": 1,
        "package_id": "pkg/mc_x",
        "collection_id": "collection/test",
        "roots": [
            {
                "file_index": root_file_index,
                "path_id": root_path_id,
                "bundle_name": root_bundle_name,
            }
        ],
        "conversion_rules": {"handedness_conversion": "mirror_x"},
        "content_hash": content_hash(glb_bytes),
        "glb_byte_length": len(glb_bytes),
        "node_count": 1,
        "mesh_count": 0,
        "mappings": {"nodes": [], "meshes": [], "materials": [], "skins": []},
        "diagnostics": diagnostics or [],
        "report_summary": {
            "roots": ["stage_root"],
            "visited_objects": 1,
            "findings_by_code": findings_by_code or {},
            "findings_by_severity": (
                findings_by_severity
                if findings_by_severity is not None
                else {code: {"error": count} for code, count in (findings_by_code or {}).items()}
            ),
            "publishable": publishable,
        },
    }
    return json.dumps(document, sort_keys=True).encode("utf-8")


def _artifacts(
    *,
    with_animation: bool = True,
    timeline_document=None,
    export_manifest_document: bytes | None = None,
):
    import json

    timeline_document = timeline_document or _timeline_document()
    timeline_bytes = json.dumps(timeline_document, ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    glb_bytes = _glb_bytes()
    artifacts = [
        PackageArtifact(url=glb_artifact_url("root_a"), kind="glb", payload=glb_bytes),
        PackageArtifact(
            url=glb_export_manifest_url("root_a"),
            kind="glb_export_manifest",
            payload=export_manifest_document or _export_manifest_bytes(glb_bytes),
        ),
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
    timeline_document = overrides.get("timeline_document", _timeline_document())
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
        artifacts=overrides.get("artifacts", _artifacts(timeline_document=timeline_document)),
        timeline_document=timeline_document,
        glb_roots=[
            {
                "root_id": "stage_root",
                "bundle_name": "timeline/bundle",
                "file_index": 0,
                "path_id": 100,
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


def test_payload_validation_preserves_diagnostic_order() -> None:
    manifest = _manifest()
    glb = next(artifact for artifact in _artifacts() if artifact.kind == "glb")
    duplicate = PackageArtifact(url=glb.url, kind="unexpected", payload=glb.payload)

    assert _package_payload_problems(manifest, [*_artifacts(), duplicate]) == [
        f"duplicate payload artifact URL: {glb.url}",
        f"{glb.url}: payload kind does not match manifest",
        f"GLB artifact {glb.url}: unexpected artifact kind",
    ]


def test_manifest_validation_preserves_schema_error_precedence() -> None:
    manifest = _manifest()
    manifest["package_id"] = ""
    manifest["source_bundles"] = {}

    with pytest.raises(ValueError, match="package manifest.package_id must be a non-empty string"):
        validate_package_manifest(manifest)


def test_build_derives_unpublishable_state_from_export_manifest_errors() -> None:
    export_manifest = _export_manifest_bytes(
        _glb_bytes(), findings_by_code={"unresolved_external": 1}
    )
    manifest = _manifest(artifacts=_artifacts(export_manifest_document=export_manifest))

    assert manifest["publishable"] is False
    assert any(
        "error finding unresolved_external" in reason for reason in manifest["incomplete_reasons"]
    )
    assert any(diagnostic.get("severity") == "error" for diagnostic in manifest["diagnostics"])


def test_build_honors_unpublishable_export_manifest() -> None:
    export_manifest = _export_manifest_bytes(_glb_bytes(), publishable=False)

    manifest = _manifest(artifacts=_artifacts(export_manifest_document=export_manifest))

    assert manifest["publishable"] is False
    assert any(
        "export report is not publishable" in reason for reason in manifest["incomplete_reasons"]
    )


def test_optional_warning_severity_is_not_upgraded_by_diagnostic_code() -> None:
    export_manifest = _export_manifest_bytes(
        _glb_bytes(),
        findings_by_code={"unresolved_external": 1},
        findings_by_severity={"unresolved_external": {"warning": 1}},
        diagnostics=[
            {
                "code": "unresolved_external",
                "severity": "warning",
                "message": "optional reference outside this package",
            }
        ],
    )

    manifest = _manifest(
        artifacts=_artifacts(export_manifest_document=export_manifest),
        diagnostics=[
            {
                "code": "unresolved_external",
                "severity": "warning",
                "message": "optional package reference",
            }
        ],
    )

    assert manifest["publishable"] is True
    assert manifest["incomplete_reasons"] == []


@pytest.mark.parametrize(
    ("root_file_index", "root_path_id"),
    [(99, 100), (0, 999)],
)
def test_package_root_must_match_exported_file_path_identity(root_file_index, root_path_id) -> None:
    export_manifest = _export_manifest_bytes(
        _glb_bytes(), root_file_index=root_file_index, root_path_id=root_path_id
    )

    manifest = _manifest(artifacts=_artifacts(export_manifest_document=export_manifest))

    assert manifest["publishable"] is False
    assert any("root identity mismatch" in reason for reason in manifest["incomplete_reasons"])


def test_package_root_bundle_must_be_in_declared_source_bundles() -> None:
    manifest = _manifest(source_bundles=[{"bundle_name": "motion/bundle", "sha256": "b" * 64}])

    assert manifest["publishable"] is False
    assert any(
        "root bundle is absent from source_bundles" in reason
        for reason in manifest["incomplete_reasons"]
    )


def test_exported_root_bundle_must_match_package_root_identity() -> None:
    export_manifest = _export_manifest_bytes(_glb_bytes(), root_bundle_name="motion/bundle")

    manifest = _manifest(artifacts=_artifacts(export_manifest_document=export_manifest))

    assert manifest["publishable"] is False
    assert any("root identity mismatch" in reason for reason in manifest["incomplete_reasons"])


def test_resolved_timeline_reference_bundle_must_be_in_package_inputs() -> None:
    document = _timeline_document()
    document["tracks"][0]["clips"][0]["resolved_references"][0]["bundle_name"] = (
        "outside/unloaded_bundle"
    )
    document["content_hash"] = _document_hash(document)
    artifacts = _artifacts(timeline_document=document)

    manifest = _manifest(timeline_document=document, artifacts=artifacts)

    assert manifest["publishable"] is False
    assert any(
        "resolved reference bundle 'outside/unloaded_bundle' is absent from source_bundles"
        in reason
        for reason in manifest["incomplete_reasons"]
    )


def test_build_rejects_removed_raw_pptr_derivatives_and_animation_summary() -> None:
    document = _timeline_document()
    clip = document["tracks"][0]["clips"][0]
    clip["resolved_references"].clear()
    document["animation_references"].clear()
    document["content_hash"] = _document_hash(document)
    artifacts = _artifacts(timeline_document=document, with_animation=False)

    manifest = _manifest(timeline_document=document, artifacts=artifacts)

    assert manifest["publishable"] is False
    assert any(
        "do not exactly cover asset_data PPtrs" in reason
        for reason in manifest["incomplete_reasons"]
    )


def test_build_rejects_unreferenced_glb_and_export_manifest_artifacts() -> None:
    manifest = _manifest(glb_roots=[])

    assert manifest["publishable"] is False
    assert any("unreferenced GLB artifact" in reason for reason in manifest["incomplete_reasons"])
    assert any(
        "unreferenced export manifest artifact" in reason
        for reason in manifest["incomplete_reasons"]
    )


def test_build_does_not_trust_empty_incomplete_reasons_with_error_diagnostics() -> None:
    manifest = _manifest(
        diagnostics=[
            {
                "code": "unresolved_external",
                "severity": "error",
                "message": "external object did not resolve",
            }
        ]
    )

    assert manifest["publishable"] is False
    assert any(
        "external object did not resolve" in reason for reason in manifest["incomplete_reasons"]
    )


def test_build_fails_closed_when_a_glb_export_manifest_is_missing() -> None:
    export_url = glb_export_manifest_url("root_a")
    artifacts = [artifact for artifact in _artifacts() if artifact.url != export_url]

    manifest = _manifest(artifacts=artifacts)

    assert manifest["publishable"] is False
    assert any("missing export manifest" in reason for reason in manifest["incomplete_reasons"])


def test_unresolved_required_clip_asset_blocks_build_and_readiness(tmp_path) -> None:
    document = _timeline_document()
    document["tracks"][0]["clips"][0]["asset"]["resolved"] = False
    document["content_hash"] = _document_hash(document)
    artifacts = _artifacts(timeline_document=document)

    manifest = _manifest(timeline_document=document, artifacts=artifacts)

    assert manifest["publishable"] is False
    assert any(
        "unresolved required clip asset" in reason for reason in manifest["incomplete_reasons"]
    )
    assert any(
        "unresolved required clip asset" in problem
        for problem in validate_package_references(manifest, document)
    )
    output = tmp_path / "package"
    with pytest.raises(ValueError, match="unresolved required clip asset"):
        materialize_package(manifest, artifacts, output)
    assert not output.exists()


def test_publish_rechecks_timeline_payload_even_if_manifest_claims_publishable() -> None:
    valid_manifest = _manifest()
    document = _timeline_document()
    document["tracks"][0]["clips"][0]["asset"]["resolved"] = False
    document["content_hash"] = _document_hash(document)
    broken_artifacts = _artifacts(timeline_document=document)
    timeline_artifact = next(
        artifact for artifact in broken_artifacts if artifact.url == TIMELINE_PLAYABLE_URL
    )
    timeline_record = next(
        record for record in valid_manifest["artifacts"] if record["url"] == TIMELINE_PLAYABLE_URL
    )
    timeline_record["sha256"] = timeline_artifact.sha256
    timeline_record["byte_length"] = timeline_artifact.byte_length
    uploaded: list[str] = []

    with pytest.raises(PublicationRefused, match="unresolved required clip asset"):
        publish_package(
            valid_manifest, broken_artifacts, lambda url, _payload: uploaded.append(url)
        )
    assert uploaded == []


def test_publish_rechecks_export_manifest_errors_even_if_manifest_claims_publishable() -> None:
    export_manifest = _export_manifest_bytes(
        _glb_bytes(), findings_by_code={"unresolved_external": 1}
    )
    broken_artifacts = _artifacts(export_manifest_document=export_manifest)
    forged_manifest = _manifest(artifacts=broken_artifacts)
    forged_manifest["publishable"] = True
    forged_manifest["incomplete_reasons"] = []
    forged_manifest["diagnostics"] = []
    uploaded: list[str] = []

    with pytest.raises(PublicationRefused, match="error finding unresolved_external"):
        publish_package(
            forged_manifest,
            broken_artifacts,
            lambda url, _payload: uploaded.append(url),
        )
    assert uploaded == []


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


def test_publish_refuses_glb_artifacts_when_root_records_are_removed() -> None:
    manifest = _manifest()
    manifest["glb_roots"] = []
    uploaded: list[str] = []

    with pytest.raises(PublicationRefused, match="unreferenced or mis-typed"):
        publish_package(manifest, _artifacts(), lambda url, _payload: uploaded.append(url))
    assert uploaded == []


@pytest.mark.parametrize(
    ("root_field", "wrong_url"),
    [
        ("glb_url", "glb/unrelated.glb"),
        ("export_manifest_url", "export_manifests/unrelated.json"),
    ],
)
def test_publish_refuses_root_references_without_matching_artifacts(root_field, wrong_url) -> None:
    manifest = _manifest()
    manifest["glb_roots"][0][root_field] = wrong_url
    uploaded: list[str] = []

    with pytest.raises(PublicationRefused):
        publish_package(manifest, _artifacts(), lambda url, _payload: uploaded.append(url))
    assert uploaded == []


def test_materialize_validates_export_payloads_before_writing(tmp_path) -> None:
    manifest = _manifest()
    export_manifest = _export_manifest_bytes(_glb_bytes(), publishable=False)
    artifacts = _artifacts(export_manifest_document=export_manifest)
    record = next(
        record
        for record in manifest["artifacts"]
        if record["url"] == glb_export_manifest_url("root_a")
    )
    export_artifact = next(
        artifact for artifact in artifacts if artifact.url == glb_export_manifest_url("root_a")
    )
    record["sha256"] = export_artifact.sha256
    record["byte_length"] = export_artifact.byte_length

    output = tmp_path / "package"
    with pytest.raises(ValueError, match="export report is not publishable"):
        materialize_package(manifest, artifacts, output)
    assert not output.exists()


def test_publish_checks_recorded_byte_length_before_upload() -> None:
    manifest = _manifest()
    timeline_record = next(
        record for record in manifest["artifacts"] if record["url"] == TIMELINE_PLAYABLE_URL
    )
    timeline_record["byte_length"] += 1
    uploaded: list[str] = []

    with pytest.raises(PublicationRefused, match="byte_length"):
        publish_package(manifest, _artifacts(), lambda url, _payload: uploaded.append(url))
    assert uploaded == []


def test_materialize_checks_recorded_byte_length_before_writing(tmp_path) -> None:
    manifest = _manifest()
    timeline_record = next(
        record for record in manifest["artifacts"] if record["url"] == TIMELINE_PLAYABLE_URL
    )
    timeline_record["byte_length"] += 1
    output = tmp_path / "package"

    with pytest.raises(ValueError, match="byte_length"):
        materialize_package(manifest, _artifacts(), output)
    assert not output.exists()


def test_manifest_url_is_reserved_and_cannot_be_an_artifact() -> None:
    manifest = _manifest()
    manifest_artifact = PackageArtifact(url=PACKAGE_MANIFEST_URL, kind="manifest", payload=b"x")
    manifest["artifacts"].append(manifest_artifact.record())
    artifacts = _artifacts() + [manifest_artifact]
    uploaded: list[str] = []

    with pytest.raises(PublicationRefused, match="manifest.json.*reserved"):
        publish_package(manifest, artifacts, lambda url, _payload: uploaded.append(url))
    assert uploaded == []


def test_animation_artifact_internal_hash_is_verified() -> None:
    import json

    from updater.extract.glb.animation import validate_animation_artifact

    broken = json.loads(_animation_artifact_bytes())
    broken["content_hash"] = "0" * 64

    with pytest.raises(ValueError, match="content_hash does not match"):
        validate_animation_artifact(broken)


def test_publish_refuses_animation_error_diagnostic_even_with_valid_outer_hash() -> None:
    import json

    manifest = _manifest()
    artifacts = _artifacts()
    original = _animation_artifact_bytes()
    animation_document = json.loads(original)
    animation_document["diagnostics"] = [
        {"code": "animation_unsupported_binding", "severity": "error", "message": "bad"}
    ]
    animation_document["content_hash"] = _artifact_hash(animation_document)
    payload = json.dumps(animation_document, sort_keys=True).encode("utf-8")
    animation_url = next(
        artifact.url for artifact in artifacts if artifact.kind == "animation_artifact"
    )
    artifact = PackageArtifact(url=animation_url, kind="animation_artifact", payload=payload)
    artifacts = [artifact if current.url == animation_url else current for current in artifacts]
    record = next(record for record in manifest["artifacts"] if record["url"] == animation_url)
    record["sha256"] = artifact.sha256
    record["byte_length"] = artifact.byte_length
    uploaded: list[str] = []

    with pytest.raises(PublicationRefused, match="error diagnostic animation_unsupported_binding"):
        publish_package(manifest, artifacts, lambda url, _payload: uploaded.append(url))
    assert uploaded == []


@pytest.mark.parametrize(
    ("corruption", "reason"),
    [
        ("hash", "content_hash does not match"),
        ("diagnostic", "error diagnostic invalid_animation"),
        ("structure", "streams must contain streamed/dense/constant"),
        ("nested_structure", "streams.streamed must be a list"),
    ],
)
def test_publish_validates_unreferenced_animation_artifacts(corruption, reason) -> None:
    import json

    manifest = _manifest()
    artifacts = _artifacts()
    unreferenced_document = json.loads(_animation_artifact_bytes())
    unreferenced_document["path_id"] = 401
    if corruption == "diagnostic":
        unreferenced_document["diagnostics"] = [
            {"code": "invalid_animation", "message": "bad data", "severity": "error"}
        ]
    elif corruption == "structure":
        unreferenced_document["streams"] = {}
    elif corruption == "nested_structure":
        unreferenced_document["streams"]["streamed"] = {}
    if corruption != "hash":
        unreferenced_document["content_hash"] = _artifact_hash(unreferenced_document)
    unreferenced_url = animation_artifact_url(unreferenced_document)
    unreferenced_payload = json.dumps(unreferenced_document, sort_keys=True).encode("utf-8")
    unreferenced = PackageArtifact(
        url=unreferenced_url,
        kind="animation_artifact",
        payload=unreferenced_payload,
    )
    artifacts.append(unreferenced)
    manifest["artifacts"].append(unreferenced.record())
    uploaded: list[str] = []

    with pytest.raises(PublicationRefused, match=reason):
        publish_package(manifest, artifacts, lambda url, _payload: uploaded.append(url))
    assert uploaded == []


def test_publish_accepts_legacy_v1_animation_warning_shape() -> None:
    import json

    manifest = _manifest()
    artifacts = _artifacts()
    animation_url = next(item.url for item in artifacts if item.kind == "animation_artifact")
    document = json.loads(_animation_artifact_bytes())
    document["diagnostics"] = [
        {"code": "animation_unsupported_binding", "message": "legacy cache warning"}
    ]
    document["content_hash"] = _artifact_hash(document)
    payload = json.dumps(document, sort_keys=True).encode("utf-8")
    legacy_artifact = PackageArtifact(animation_url, "animation_artifact", payload)
    artifacts = [legacy_artifact if item.url == animation_url else item for item in artifacts]
    record = next(item for item in manifest["artifacts"] if item["url"] == animation_url)
    record.update(legacy_artifact.record())
    uploaded: list[str] = []

    publish_package(manifest, artifacts, lambda url, _payload: uploaded.append(url))

    assert uploaded[-1] == PACKAGE_MANIFEST_URL


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
    assert composite["publishable"] is False
    assert any(
        "composite publication is not implemented" in reason
        for reason in composite["incomplete_reasons"]
    )
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

    forged_publishable = dict(composite)
    forged_publishable["publishable"] = True
    forged_publishable["incomplete_reasons"] = []
    with pytest.raises(ValueError, match="composite publication is not implemented"):
        from updater.extract.glb.package import validate_composite_manifest

        validate_composite_manifest(forged_publishable)


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
