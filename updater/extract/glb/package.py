"""Browser-facing Streaming Live package manifests and publication (#40).

A package directory holds one ``manifest.json`` (the root contract), one
``timeline.playable.json`` (from :mod:`updater.extract.glb.timeline`), and
the artifacts the manifest references by relative URL — GLB exports,
animation artifacts, and their export manifests.  The manifest records
every artifact's SHA-256 and byte length, the source Bundle checksums, and
conversion metadata; ``manifest.json`` is not an artifact (it cannot hash
itself) and is written and uploaded last.  A package marked
``publishable=false`` never reaches remote storage.  Composite packages
union child manifests while preserving child purpose boundaries and never
duplicating physical artifact files.

The publisher validates consistency, not provenance. Call it only with
artifacts produced by the trusted extraction workflow; manifest fields and
SHA-256 hashes are self-reported and are not authenticity proofs.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path as StdPath
from typing import Any, Callable, Mapping, Sequence

from updater.extract.glb.contracts import (
    DIAG_ANIMATION_UNSUPPORTED_BINDING,
    DIAG_ARTIFACT_INCOMPLETE,
    SEVERITIES,
    safe_relative_path,
)
from updater.extract.glb.gltf_writer import content_hash, validate_glb_bytes
from updater.extract.glb.timeline import (
    TIMELINE_ARTIFACT_FIELDS,
    timeline_reference_problems,
    validate_timeline_document,
)

PACKAGE_MANIFEST_VERSION = 1
TIMELINE_PLAYABLE_URL = "timeline.playable.json"
PACKAGE_MANIFEST_URL = "manifest.json"
TIMELINE_ARTIFACT_KIND = "timeline_playable_document"
GLB_ARTIFACT_KIND = "glb"
GLB_EXPORT_MANIFEST_KIND = "glb_export_manifest"
ANIMATION_ARTIFACT_KIND = "animation_artifact"
COMPOSITE_PUBLISH_DISABLED_REASON = (
    "composite publication is not implemented; no namespace-safe composite publisher exists"
)

PACKAGE_MANIFEST_FIELDS = {
    "manifest_version",
    "package_id",
    "purpose",
    "profile",
    "unity_version",
    "adapter_version",
    "source_bundles",
    "publishable",
    "incomplete_reasons",
    "artifacts",
    "timeline",
    "glb_roots",
    "conversion_metadata",
    "diagnostics",
}

COMPOSITE_MANIFEST_FIELDS = {
    "manifest_version",
    "package_id",
    "composite_kind",
    "child_packages",
    "source_bundles",
    "publishable",
    "incomplete_reasons",
    "artifacts",
    "timelines",
    "conversion_metadata",
}

ARTIFACT_FIELDS = {"url", "kind", "sha256", "byte_length"}
TIMELINE_SECTION_FIELDS = {
    "url",
    "timeline_name",
    "track_count",
    "clip_count",
    "animation_reference_count",
}
GLB_ROOT_SECTION_FIELDS = {
    "root_id",
    "bundle_name",
    "file_index",
    "path_id",
    "container_path",
    "glb_url",
    "export_manifest_url",
}


class PublicationRefused(Exception):
    """A package or composite must not reach remote storage."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = list(reasons)
        super().__init__(
            f"{DIAG_ARTIFACT_INCOMPLETE}: package is not publishable: " + "; ".join(self.reasons)
        )


@dataclass(frozen=True, slots=True)
class PackageArtifact:
    """One file the package manifest references by relative URL."""

    url: str
    kind: str
    payload: bytes

    @property
    def sha256(self) -> str:
        return content_hash(self.payload)

    @property
    def byte_length(self) -> int:
        return len(self.payload)

    def record(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True, slots=True)
class PackageCompleteness:
    """Offline validation verdict for one materialized package directory."""

    publishable: bool
    problems: list[str]

    def summary(self) -> dict[str, Any]:
        return {
            "publishable": self.publishable,
            "problem_count": len(self.problems),
            "problems": list(self.problems),
        }


def _slug(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)) or "unnamed"


def animation_artifact_url(reference: Mapping[str, Any]) -> str:
    """Deterministic package URL for one animation artifact reference."""

    return (
        f"animation/{_slug(reference.get('bundle_name'))}"
        f"/{reference.get('file_index')}/{reference.get('path_id')}"
        f"/{_slug(reference.get('clip_name') or 'clip')}.json"
    )


def glb_artifact_url(root_name: str) -> str:
    return f"glb/{_slug(root_name)}.glb"


def glb_export_manifest_url(root_name: str) -> str:
    return f"export_manifests/{_slug(root_name)}.json"


def _artifact_records(artifacts: Sequence[PackageArtifact]) -> list[dict[str, Any]]:
    return [artifact.record() for artifact in sorted(artifacts, key=lambda a: a.url)]


def _diagnostic_problems(
    diagnostics: Any,
    owner: str,
    *,
    legacy_warning_codes: frozenset[str] = frozenset(),
) -> list[str]:
    if not isinstance(diagnostics, (list, tuple)):
        return [f"{owner}: diagnostics must be a list"]
    problems = []
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, Mapping):
            problems.append(f"{owner}: invalid diagnostic {diagnostic!r}")
            continue
        if not isinstance(diagnostic.get("code"), str) or not diagnostic.get("code"):
            problems.append(f"{owner}: diagnostic has invalid code: {diagnostic!r}")
        if not isinstance(diagnostic.get("message"), str):
            problems.append(f"{owner}: diagnostic has invalid message: {diagnostic!r}")
        severity = diagnostic.get("severity")
        if "severity" not in diagnostic and diagnostic.get("code") in legacy_warning_codes:
            severity = "warning"
        if severity == "error":
            problems.append(
                f"{owner}: error diagnostic {diagnostic.get('code')}: "
                f"{diagnostic.get('message', '')}"
            )
        elif severity not in ("info", "warning"):
            problems.append(f"{owner}: diagnostic has missing or invalid severity: {diagnostic!r}")
    return problems


def _is_glb_artifact(artifact: PackageArtifact) -> bool:
    return artifact.kind == GLB_ARTIFACT_KIND or artifact.url.lower().endswith(".glb")


def _is_export_manifest_artifact(artifact: PackageArtifact) -> bool:
    return artifact.kind == GLB_EXPORT_MANIFEST_KIND or (
        artifact.url.startswith("export_manifests/") and artifact.url.lower().endswith(".json")
    )


def _package_payload_problems(
    manifest: Mapping[str, Any],
    artifacts: Sequence[PackageArtifact],
    *,
    expected_timeline_document: Mapping[str, Any] | None = None,
) -> list[str]:
    """Verify the payload-backed references required before a package is ready."""

    from updater.extract.glb.animation import validate_animation_artifact
    from updater.extract.glb.export import _validate_export_manifest

    problems = _diagnostic_problems(manifest.get("diagnostics", ()), "package")
    artifact_by_url: dict[str, PackageArtifact] = {}
    for artifact in artifacts:
        if artifact.url in artifact_by_url:
            problems.append(f"duplicate payload artifact URL: {artifact.url}")
        else:
            artifact_by_url[artifact.url] = artifact
    source_records = manifest.get("source_bundles", ())
    if not isinstance(source_records, (list, tuple)):
        problems.append("source_bundles must be a list")
        source_records = ()
    source_bundle_names = {
        record.get("bundle_name")
        for record in source_records
        if isinstance(record, Mapping)
        and isinstance(record.get("bundle_name"), str)
        and record.get("bundle_name")
    }
    manifest_artifacts = manifest.get("artifacts", ())
    if not isinstance(manifest_artifacts, (list, tuple)):
        problems.append("package manifest artifact records must be a list")
        manifest_artifacts = ()
    records_by_url = {
        record.get("url"): record
        for record in manifest_artifacts
        if isinstance(record, Mapping) and isinstance(record.get("url"), str)
    }
    for artifact in artifacts:
        record = records_by_url.get(artifact.url)
        if record is None:
            problems.append(f"payload artifact is not declared in manifest: {artifact.url}")
            continue
        if record.get("kind") != artifact.kind:
            problems.append(f"{artifact.url}: payload kind does not match manifest")
        if record.get("byte_length") != artifact.byte_length:
            problems.append(
                f"{artifact.url}: byte_length {artifact.byte_length} "
                f"!= recorded {record.get('byte_length')}"
            )
        if record.get("sha256") != artifact.sha256:
            problems.append(f"{artifact.url}: payload sha256 does not match manifest")

    animation_documents: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if artifact.kind != ANIMATION_ARTIFACT_KIND and not artifact.url.startswith("animation/"):
            continue
        if artifact.kind != ANIMATION_ARTIFACT_KIND:
            problems.append(f"animation artifact {artifact.url}: unexpected artifact kind")
            continue
        try:
            animation_document = validate_animation_artifact(json.loads(artifact.payload))
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            problems.append(f"animation artifact {artifact.url}: invalid artifact: {exc}")
            continue
        animation_documents[artifact.url] = animation_document
        if animation_artifact_url(animation_document) != artifact.url:
            problems.append(f"animation artifact {artifact.url}: URL does not match its identity")
        problems.extend(
            _diagnostic_problems(
                animation_document.get("diagnostics", ()),
                f"animation artifact {artifact.url}",
                legacy_warning_codes=frozenset({DIAG_ANIMATION_UNSUPPORTED_BINDING}),
            )
        )

    timeline_artifact = artifact_by_url.get(TIMELINE_PLAYABLE_URL)
    timeline_document: dict[str, Any] | None = None
    if timeline_artifact is None:
        problems.append(f"{TIMELINE_PLAYABLE_URL}: missing required timeline artifact")
    elif timeline_artifact.kind != TIMELINE_ARTIFACT_KIND:
        problems.append(f"{TIMELINE_PLAYABLE_URL}: unexpected artifact kind")
    else:
        try:
            timeline_document = validate_timeline_document(json.loads(timeline_artifact.payload))
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            problems.append(f"{TIMELINE_PLAYABLE_URL}: invalid timeline artifact: {exc}")
        if timeline_document is not None:
            if timeline_document["package_id"] != manifest["package_id"]:
                problems.append(f"{TIMELINE_PLAYABLE_URL}: package_id does not match manifest")
            timeline_bundle_name = timeline_document.get("bundle_name")
            if (
                not isinstance(timeline_bundle_name, str)
                or timeline_bundle_name not in source_bundle_names
            ):
                problems.append(
                    f"{TIMELINE_PLAYABLE_URL}: timeline bundle is absent from source_bundles"
                )
            if expected_timeline_document is not None:
                expected_json = json.dumps(
                    dict(expected_timeline_document), sort_keys=True, ensure_ascii=False
                )
                actual_json = json.dumps(timeline_document, sort_keys=True, ensure_ascii=False)
                if actual_json != expected_json:
                    problems.append(
                        f"{TIMELINE_PLAYABLE_URL}: payload differs from the supplied timeline document"
                    )
            problems.extend(validate_package_references(manifest, timeline_document))
            problems.extend(
                _diagnostic_problems(
                    timeline_document.get("diagnostics", ()), TIMELINE_PLAYABLE_URL
                )
            )

            for track in timeline_document.get("tracks", ()):
                if not isinstance(track, Mapping):
                    continue
                for clip in track.get("clips", ()):
                    if not isinstance(clip, Mapping):
                        continue
                    asset = clip.get("asset")
                    if (
                        isinstance(asset, Mapping)
                        and asset.get("resolved") is True
                        and (
                            not isinstance(asset.get("bundle_name"), str)
                            or asset.get("bundle_name") not in source_bundle_names
                        )
                    ):
                        problems.append(
                            f"clip asset bundle {asset.get('bundle_name')!r} "
                            "is absent from source_bundles"
                        )
                    for reference in clip.get("resolved_references", ()):
                        if (
                            isinstance(reference, Mapping)
                            and reference.get("resolved") is True
                            and (
                                not isinstance(reference.get("bundle_name"), str)
                                or reference.get("bundle_name") not in source_bundle_names
                            )
                        ):
                            problems.append(
                                f"resolved reference bundle {reference.get('bundle_name')!r} "
                                "is absent from source_bundles"
                            )
            for reference in timeline_document.get("animation_references", ()):
                if isinstance(reference, Mapping) and (
                    not isinstance(reference.get("bundle_name"), str)
                    or reference.get("bundle_name") not in source_bundle_names
                ):
                    problems.append(
                        f"animation reference bundle {reference.get('bundle_name')!r} "
                        "is absent from source_bundles"
                    )

            for reference in timeline_document.get("animation_references", ()):
                if not isinstance(reference, Mapping):
                    continue  # timeline_reference_problems records the malformed entry.
                url = animation_artifact_url(reference)
                animation_artifact = artifact_by_url.get(url)
                if animation_artifact is None:
                    continue  # validate_package_references already reports the missing URL.
                animation_document = animation_documents.get(url)
                if animation_document is None:
                    continue
                identity = (
                    animation_document.get("bundle_name"),
                    animation_document.get("file_index"),
                    animation_document.get("path_id"),
                )
                expected_identity = (
                    reference.get("bundle_name"),
                    reference.get("file_index"),
                    reference.get("path_id"),
                )
                if identity != expected_identity:
                    problems.append(f"animation reference {url}: artifact identity mismatch")

    roots = manifest.get("glb_roots", ())
    if not isinstance(roots, (list, tuple)):
        problems.append("GLB root records must be a list")
        roots = ()
    glb_reference_counts: Counter[str] = Counter()
    export_reference_counts: Counter[str] = Counter()
    root_ids: set[str] = set()
    for root in roots:
        if not isinstance(root, Mapping):
            problems.append(f"invalid GLB root record: {root!r}")
            continue
        root_id = root.get("root_id")
        if not isinstance(root_id, str) or not root_id:
            problems.append(f"GLB root has invalid root_id: {root!r}")
            root_id = "<unknown>"
        elif root_id in root_ids:
            problems.append(f"duplicate GLB root_id: {root_id}")
        root_ids.add(root_id)

        glb_url = root.get("glb_url")
        export_manifest_url = root.get("export_manifest_url")
        if isinstance(glb_url, str):
            glb_reference_counts[glb_url] += 1
        else:
            problems.append(f"GLB root {root_id}: invalid glb_url")
        if isinstance(export_manifest_url, str):
            export_reference_counts[export_manifest_url] += 1
        else:
            problems.append(f"GLB root {root_id}: invalid export_manifest_url")

        bundle_name = root.get("bundle_name")
        file_index = root.get("file_index")
        path_id = root.get("path_id")
        if (
            not isinstance(bundle_name, str)
            or not bundle_name
            or type(file_index) is not int
            or type(path_id) is not int
        ):
            problems.append(f"GLB root {root_id}: incomplete root identity")
        if isinstance(bundle_name, str) and bundle_name not in source_bundle_names:
            problems.append(f"GLB root {root_id}: root bundle is absent from source_bundles")

        glb_artifact = artifact_by_url.get(glb_url) if isinstance(glb_url, str) else None
        export_artifact = (
            artifact_by_url.get(export_manifest_url)
            if isinstance(export_manifest_url, str)
            else None
        )
        if glb_artifact is None:
            problems.append(f"GLB root {root_id}: missing GLB artifact {glb_url}")
            continue
        if glb_artifact.kind != GLB_ARTIFACT_KIND:
            problems.append(f"GLB root {root_id}: unexpected GLB artifact kind")
        try:
            validate_glb_bytes(glb_artifact.payload)
        except Exception as exc:  # NOSONAR - invalid GLB bytes must fail package readiness
            problems.append(f"GLB root {root_id}: invalid GLB payload: {exc}")
        if export_artifact is None:
            problems.append(f"GLB root {root_id}: missing export manifest {export_manifest_url}")
            continue
        if export_artifact.kind != GLB_EXPORT_MANIFEST_KIND:
            problems.append(f"GLB root {root_id}: unexpected export manifest artifact kind")
        try:
            export_manifest = _validate_export_manifest(json.loads(export_artifact.payload))
        except Exception as exc:  # NOSONAR - invalid export metadata must fail closed
            problems.append(f"GLB root {root_id}: invalid export manifest: {exc}")
            continue

        if export_manifest["package_id"] != manifest["package_id"]:
            problems.append(f"GLB root {root_id}: export package_id mismatch")
        export_roots = export_manifest.get("roots")
        if not isinstance(export_roots, list) or len(export_roots) != 1:
            problems.append(f"GLB root {root_id}: export manifest must identify exactly one root")
        else:
            export_root = export_roots[0]
            if (
                not isinstance(export_root, Mapping)
                or not isinstance(export_root.get("bundle_name"), str)
                or not export_root.get("bundle_name")
                or type(export_root.get("file_index")) is not int
                or type(export_root.get("path_id")) is not int
            ):
                problems.append(f"GLB root {root_id}: export manifest root identity is incomplete")
            elif (
                export_root["bundle_name"],
                export_root["file_index"],
                export_root["path_id"],
            ) != (bundle_name, file_index, path_id):
                problems.append(f"GLB root {root_id}: export manifest root identity mismatch")
        if export_manifest["content_hash"] != content_hash(glb_artifact.payload):
            problems.append(f"GLB root {root_id}: export manifest GLB hash mismatch")
        if export_manifest["glb_byte_length"] != len(glb_artifact.payload):
            problems.append(f"GLB root {root_id}: export manifest GLB length mismatch")

        report_summary = export_manifest.get("report_summary")
        if not isinstance(report_summary, Mapping) or report_summary.get("publishable") is not True:
            problems.append(f"GLB root {root_id}: export report is not publishable")
        report_roots = report_summary.get("roots") if isinstance(report_summary, Mapping) else None
        if (
            not isinstance(report_roots, list)
            or any(not isinstance(item, str) or not item for item in report_roots)
            or root_id not in report_roots
        ):
            problems.append(f"GLB root {root_id}: export report does not identify this root")
        findings = (
            report_summary.get("findings_by_code") if isinstance(report_summary, Mapping) else None
        )
        findings_by_severity = (
            report_summary.get("findings_by_severity")
            if isinstance(report_summary, Mapping)
            else None
        )
        if not isinstance(findings, Mapping) or not isinstance(findings_by_severity, Mapping):
            problems.append(f"GLB root {root_id}: export report findings are invalid")
        else:
            for code, count in findings.items():
                severity_counts = findings_by_severity.get(code)
                if (
                    not isinstance(code, str)
                    or type(count) is not int
                    or count < 0
                    or not isinstance(severity_counts, Mapping)
                ):
                    problems.append(f"GLB root {root_id}: invalid finding count for {code}")
                    continue
                total = 0
                for severity, severity_count in severity_counts.items():
                    if (
                        severity not in SEVERITIES
                        or type(severity_count) is not int
                        or severity_count < 0
                    ):
                        problems.append(
                            f"GLB root {root_id}: invalid finding severity count for {code}"
                        )
                        continue
                    total += severity_count
                    if severity == "error" and severity_count:
                        problems.append(f"GLB root {root_id}: error finding {code} present")
                if total != count:
                    problems.append(
                        f"GLB root {root_id}: finding severity totals disagree for {code}"
                    )
            for code in set(findings_by_severity) - set(findings):
                problems.append(
                    f"GLB root {root_id}: severity summary has undeclared finding {code}"
                )
        problems.extend(
            _diagnostic_problems(
                export_manifest.get("diagnostics", ()), f"GLB root {root_id} export manifest"
            )
        )

    for artifact in artifacts:
        if _is_glb_artifact(artifact):
            if artifact.kind != GLB_ARTIFACT_KIND:
                problems.append(f"GLB artifact {artifact.url}: unexpected artifact kind")
            if glb_reference_counts[artifact.url] != 1:
                problems.append(f"unreferenced GLB artifact: {artifact.url}")
        if _is_export_manifest_artifact(artifact):
            if artifact.kind != GLB_EXPORT_MANIFEST_KIND:
                problems.append(f"export manifest {artifact.url}: unexpected artifact kind")
            if export_reference_counts[artifact.url] != 1:
                problems.append(f"unreferenced export manifest artifact: {artifact.url}")
    for url, count in glb_reference_counts.items():
        if count != 1:
            problems.append(f"GLB artifact URL {url} is referenced {count} times")
    for url, count in export_reference_counts.items():
        if count != 1:
            problems.append(f"export manifest URL {url} is referenced {count} times")

    return list(dict.fromkeys(problems))


def build_release_manifest(
    *,
    package_id: str,
    purpose: str,
    profile: str,
    unity_version: str,
    adapter_version: str,
    source_bundles: Sequence[Mapping[str, Any]],
    artifacts: Sequence[PackageArtifact],
    timeline_document: Mapping[str, Any],
    glb_roots: Sequence[Mapping[str, Any]],
    conversion_metadata: Mapping[str, Any],
    diagnostics: Sequence[Mapping[str, Any]],
    incomplete_reasons: Sequence[str],
) -> dict[str, Any]:
    """Assemble one package manifest from verified inputs.

    The timeline playable document must already be part of ``artifacts``
    under ``timeline.playable.json``; the manifest's timeline section
    summarizes the same document so consumers can read the summary without
    fetching the full artifact.  ``manifest.json`` itself is never an
    artifact — it cannot hash itself.
    """

    timeline_urls = [a.url for a in artifacts if a.url == TIMELINE_PLAYABLE_URL]
    if len(timeline_urls) != 1:
        raise ValueError(
            "package artifacts must contain exactly one timeline.playable.json,"
            f" got {len(timeline_urls)}"
        )
    for artifact in artifacts:
        safe_relative_path(artifact.url)
    validated_timeline = validate_timeline_document(dict(timeline_document))
    timeline_summary = timeline_document_summary(validated_timeline)
    manifest: dict[str, Any] = {
        "manifest_version": PACKAGE_MANIFEST_VERSION,
        "package_id": package_id,
        "purpose": purpose,
        "profile": profile,
        "unity_version": unity_version,
        "adapter_version": adapter_version,
        "source_bundles": [
            dict(record)
            for record in sorted(source_bundles, key=lambda r: str(r.get("bundle_name")))
        ],
        "publishable": not incomplete_reasons,
        "incomplete_reasons": list(incomplete_reasons),
        "artifacts": _artifact_records(artifacts),
        "timeline": {
            "url": TIMELINE_PLAYABLE_URL,
            **timeline_summary,
        },
        "glb_roots": [dict(record) for record in glb_roots],
        "conversion_metadata": dict(conversion_metadata),
        "diagnostics": [dict(diagnostic) for diagnostic in diagnostics],
    }
    payload_problems = _package_payload_problems(
        manifest,
        artifacts,
        expected_timeline_document=validated_timeline,
    )
    incomplete = list(incomplete_reasons)
    for problem in payload_problems:
        if problem not in incomplete:
            incomplete.append(problem)
    manifest["incomplete_reasons"] = incomplete
    manifest["publishable"] = not incomplete
    manifest["diagnostics"].extend(
        {
            "code": DIAG_ARTIFACT_INCOMPLETE,
            "severity": "error",
            "phase": "package_validation",
            "message": problem,
        }
        for problem in payload_problems
    )
    return validate_package_manifest(manifest)


def validate_package_manifest(value: Any) -> dict[str, Any]:
    """Structurally validate one package manifest document."""

    if not isinstance(value, dict):
        raise ValueError("package manifest must be an object")
    if set(value) != PACKAGE_MANIFEST_FIELDS:
        raise ValueError(
            f"package manifest fields must be exactly"
            f" {sorted(PACKAGE_MANIFEST_FIELDS)}, got {sorted(value)}"
        )
    if value["manifest_version"] != PACKAGE_MANIFEST_VERSION:
        raise ValueError("package manifest_version must be 1")
    for key in ("package_id", "purpose", "profile", "unity_version", "adapter_version"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"package manifest.{key} must be a non-empty string")
    if not isinstance(value["source_bundles"], list):
        raise ValueError("package manifest.source_bundles must be a list")
    source_bundle_names: set[str] = set()
    for source in value["source_bundles"]:
        if (
            not isinstance(source, Mapping)
            or not isinstance(source.get("bundle_name"), str)
            or not source.get("bundle_name")
        ):
            raise ValueError(f"invalid package source bundle record: {source!r}")
        source_bundle_names.add(source["bundle_name"])
    if not isinstance(value["publishable"], bool):
        raise ValueError("package manifest.publishable must be a boolean")
    reasons = value["incomplete_reasons"]
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise ValueError("package manifest.incomplete_reasons must be string list")
    if not value["publishable"] and not reasons:
        raise ValueError("unpublishable packages must record incomplete_reasons")
    if value["publishable"] and reasons:
        raise ValueError("publishable packages must not record incomplete_reasons")
    diagnostics = value["diagnostics"]
    if not isinstance(diagnostics, list):
        raise ValueError("package manifest.diagnostics must be a list")
    if value["publishable"]:
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, Mapping):
                raise ValueError(f"publishable package has invalid diagnostic: {diagnostic!r}")
            if diagnostic.get("severity") not in SEVERITIES:
                raise ValueError(
                    f"publishable package diagnostic lacks valid severity: {diagnostic!r}"
                )
            if diagnostic["severity"] == "error":
                raise ValueError("publishable package must not contain error diagnostics")
    _validate_artifact_records(value["artifacts"])
    artifacts_by_url = {record["url"]: record for record in value["artifacts"]}
    if value["publishable"]:
        timeline_artifact = artifacts_by_url.get(TIMELINE_PLAYABLE_URL)
        if timeline_artifact is None:
            raise ValueError(f"publishable package is missing {TIMELINE_PLAYABLE_URL}")
        if timeline_artifact["kind"] != TIMELINE_ARTIFACT_KIND:
            raise ValueError(f"{TIMELINE_PLAYABLE_URL} has an unexpected artifact kind")
    timeline = value["timeline"]
    if not isinstance(timeline, dict) or set(timeline) != TIMELINE_SECTION_FIELDS:
        raise ValueError(
            f"package manifest.timeline must have fields {sorted(TIMELINE_SECTION_FIELDS)}"
        )
    if timeline["url"] != TIMELINE_PLAYABLE_URL:
        raise ValueError(f"timeline section must point at {TIMELINE_PLAYABLE_URL}")
    if not isinstance(value["glb_roots"], list):
        raise ValueError("package manifest.glb_roots must be a list")
    glb_references: Counter[str] = Counter()
    export_references: Counter[str] = Counter()
    root_ids: set[str] = set()
    for record in value["glb_roots"]:
        if not isinstance(record, dict) or set(record) != GLB_ROOT_SECTION_FIELDS:
            raise ValueError(f"glb_root records must have fields {sorted(GLB_ROOT_SECTION_FIELDS)}")
        if not isinstance(record["root_id"], str) or not record["root_id"]:
            raise ValueError("GLB root root_id must be a non-empty string")
        if record["root_id"] in root_ids:
            raise ValueError(f"duplicate GLB root_id: {record['root_id']}")
        root_ids.add(record["root_id"])
        if not isinstance(record["bundle_name"], str) or not record["bundle_name"]:
            raise ValueError("GLB root bundle_name must be a non-empty string")
        if type(record["file_index"]) is not int or type(record["path_id"]) is not int:
            raise ValueError("GLB root file_index and path_id must be integers")
        safe_relative_path(record["glb_url"])
        safe_relative_path(record["export_manifest_url"])
        glb_references[record["glb_url"]] += 1
        export_references[record["export_manifest_url"]] += 1
        if value["publishable"]:
            glb_artifact = artifacts_by_url.get(record["glb_url"])
            export_manifest = artifacts_by_url.get(record["export_manifest_url"])
            if glb_artifact is None or export_manifest is None:
                raise ValueError(
                    f"publishable GLB root {record['root_id']} must reference declared GLB "
                    "and export-manifest artifacts"
                )
            if glb_artifact["kind"] != GLB_ARTIFACT_KIND:
                raise ValueError(
                    f"GLB root {record['root_id']} has an unexpected GLB artifact kind"
                )
            if export_manifest["kind"] != GLB_EXPORT_MANIFEST_KIND:
                raise ValueError(
                    f"GLB root {record['root_id']} has an unexpected export manifest kind"
                )
            if record["bundle_name"] not in source_bundle_names:
                raise ValueError(
                    f"GLB root {record['root_id']} bundle is absent from source_bundles"
                )
    if value["publishable"]:
        for artifact in value["artifacts"]:
            is_glb = artifact["kind"] == GLB_ARTIFACT_KIND or artifact["url"].lower().endswith(
                ".glb"
            )
            is_export_manifest = artifact["kind"] == GLB_EXPORT_MANIFEST_KIND or (
                artifact["url"].startswith("export_manifests/")
                and artifact["url"].lower().endswith(".json")
            )
            if is_glb and (
                artifact["kind"] != GLB_ARTIFACT_KIND or glb_references[artifact["url"]] != 1
            ):
                raise ValueError(
                    f"publishable package has unreferenced or mis-typed GLB artifact "
                    f"{artifact['url']}"
                )
            if is_export_manifest and (
                artifact["kind"] != GLB_EXPORT_MANIFEST_KIND
                or export_references[artifact["url"]] != 1
            ):
                raise ValueError(
                    f"publishable package has unreferenced or mis-typed export manifest "
                    f"{artifact['url']}"
                )
    return value


def _validate_artifact_records(records: Any) -> None:
    if not isinstance(records, list) or not records:
        raise ValueError("package manifest.artifacts must be a non-empty list")
    seen_urls: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != ARTIFACT_FIELDS:
            raise ValueError(f"artifact records must have fields {sorted(ARTIFACT_FIELDS)}")
        url = record["url"]
        safe_relative_path(url)
        if url == PACKAGE_MANIFEST_URL:
            raise ValueError(f"{PACKAGE_MANIFEST_URL} is reserved and cannot be an artifact URL")
        if not isinstance(record["kind"], str) or not record["kind"]:
            raise ValueError(f"artifact {url} kind must be a non-empty string")
        if url in seen_urls:
            raise ValueError(f"duplicate artifact URL: {url}")
        seen_urls.add(url)
        sha256 = record["sha256"]
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in sha256)
        ):
            raise ValueError(f"artifact {url} sha256 must be 64 lowercase hex chars")
        if type(record["byte_length"]) is not int or record["byte_length"] <= 0:
            raise ValueError(f"artifact {url} byte_length must be a positive integer")


def serialize_package_manifest(manifest: Mapping[str, Any]) -> bytes:
    """Canonical bytes for the package's ``manifest.json``."""

    validated = validate_package_manifest(dict(manifest))
    return json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")


def validate_package_completeness(manifest: Mapping[str, Any], directory) -> PackageCompleteness:
    """Offline validator: every artifact URL exists inside the directory and
    matches its recorded hash; timeline animation references map to
    declared animation artifacts."""

    root = StdPath(os.fspath(directory))
    validated = validate_package_manifest(dict(manifest))
    problems: list[str] = []
    declared_urls = {record["url"]: record for record in validated["artifacts"]}
    materialized: list[PackageArtifact] = []
    root_real = root.resolve()
    for record in validated["artifacts"]:
        url = record["url"]
        target = root / url
        if not target.resolve().is_relative_to(root_real):
            problems.append(f"{url}: escapes the package directory")
            continue
        if not target.is_file():
            problems.append(f"{url}: missing from the package directory")
            continue
        payload = target.read_bytes()
        materialized.append(PackageArtifact(url=url, kind=record["kind"], payload=payload))
        if len(payload) != record["byte_length"]:
            problems.append(
                f"{url}: byte_length {len(payload)} != recorded {record['byte_length']}"
            )
        if content_hash(payload) != record["sha256"]:
            problems.append(f"{url}: sha256 mismatch")
    if TIMELINE_PLAYABLE_URL not in declared_urls:
        problems.append(f"{TIMELINE_PLAYABLE_URL}: not declared in artifacts")
    if not validated["publishable"]:
        problems.append(
            "manifest is marked unpublishable: " + "; ".join(validated["incomplete_reasons"])
        )
    problems.extend(_package_payload_problems(validated, materialized))
    problems = list(dict.fromkeys(problems))
    return PackageCompleteness(publishable=not problems, problems=problems)


def validate_package_references(
    manifest: Mapping[str, Any], timeline_document: Mapping[str, Any]
) -> list[str]:
    """Referential integrity between the manifest and its playable document.

    Every animation reference in the playable document must map to a
    declared animation artifact URL, and the manifest's timeline summary
    must agree with the document's actual counts.
    """

    problems: list[str] = timeline_reference_problems(timeline_document)
    declared = {record["url"] for record in manifest["artifacts"]}
    for reference in timeline_document.get("animation_references", ()):
        if not isinstance(reference, Mapping):
            continue
        url = animation_artifact_url(reference)
        if url not in declared:
            problems.append(
                f"animation reference {reference.get('path_id')} maps to undeclared artifact {url}"
            )
    timeline = manifest["timeline"]
    tracks = [track for track in timeline_document.get("tracks", ()) if isinstance(track, Mapping)]
    actual = {
        "track_count": len(timeline_document.get("tracks", ())),
        "clip_count": sum(
            len(track.get("clips", ()))
            for track in tracks
            if isinstance(track.get("clips", ()), (list, tuple))
        ),
        "animation_reference_count": len(timeline_document.get("animation_references", ())),
        "timeline_name": timeline_document.get("timeline_name", ""),
    }
    for key, value in actual.items():
        if timeline.get(key) != value:
            problems.append(f"timeline summary {key} disagrees with the document")
    return problems


def materialize_package(
    manifest: Mapping[str, Any],
    artifacts: Sequence[PackageArtifact],
    directory,
) -> None:
    """Write the package directory atomically: artifacts first, the playable
    document next, ``manifest.json`` last.

    A crash mid-write therefore never leaves a complete-looking package
    missing its artifacts or current.
    """

    from updater.security import atomic_write_bytes

    validated = validate_package_manifest(dict(manifest))
    declared = {record["url"]: record for record in validated["artifacts"]}
    if {artifact.url for artifact in artifacts} != set(declared):
        raise ValueError("materialize artifacts must match the manifest exactly")
    payload_problems = _package_payload_problems(validated, artifacts)
    if payload_problems:
        raise ValueError("materialize payload validation failed: " + "; ".join(payload_problems))
    root = StdPath(os.fspath(directory))
    root.mkdir(parents=True, exist_ok=True)
    for artifact in sorted(artifacts, key=lambda a: (a.url == TIMELINE_PLAYABLE_URL, a.url)):
        target = root / artifact.url
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, artifact.payload)
    atomic_write_bytes(root / PACKAGE_MANIFEST_URL, serialize_package_manifest(manifest))


def publish_package(
    manifest: Mapping[str, Any],
    artifacts: Sequence[PackageArtifact],
    uploader: Callable[[str, bytes], None],
) -> None:
    """Package-aware publication: artifacts first, manifests last.

    ``uploader`` receives ``(url, bytes)`` per artifact.  A package marked
    unpublishable — structurally, by recorded reasons, or by payload/hash
    disagreement — never calls the uploader. This check is not an authenticity
    boundary: callers must supply outputs from a trusted extraction workflow;
    a self-reported manifest and matching hashes do not establish provenance.
    """

    try:
        validated = validate_package_manifest(dict(manifest))
    except ValueError as exc:
        raise PublicationRefused([str(exc)]) from exc
    if not validated["publishable"]:
        raise PublicationRefused(validated["incomplete_reasons"])
    declared = {record["url"]: record for record in validated["artifacts"]}
    if {artifact.url for artifact in artifacts} != set(declared):
        raise ValueError("publish artifacts must match the manifest exactly")
    for artifact in artifacts:
        if declared[artifact.url]["sha256"] != artifact.sha256:
            raise PublicationRefused([f"{artifact.url}: payload does not match its manifest hash"])
    payload_problems = _package_payload_problems(validated, artifacts)
    if payload_problems:
        raise PublicationRefused(payload_problems)
    for artifact in sorted(artifacts, key=lambda a: (a.url == TIMELINE_PLAYABLE_URL, a.url)):
        uploader(artifact.url, artifact.payload)
    uploader(PACKAGE_MANIFEST_URL, serialize_package_manifest(manifest))


def build_composite_manifest(
    *,
    package_id: str,
    child_manifests: Sequence[Mapping[str, Any]],
    conversion_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Union child packages into a composite, preserving boundaries.

    Artifacts deduplicate by URL — two children claiming the same URL with
    different content is a hard conflict, never a silent overwrite, so a
    shared character/texture/animation artifact stays one physical file.  A
    Composite manifests are local-only until namespace-safe artifact routing
    and a dedicated publisher are implemented; they always fail closed.
    """

    if not child_manifests:
        raise ValueError("composite requires at least one child package")
    children = [validate_package_manifest(dict(child)) for child in child_manifests]
    child_ids = [child["package_id"] for child in children]
    if len(set(child_ids)) != len(child_ids):
        raise ValueError(f"composite child package ids must be unique: {child_ids}")

    artifact_by_url: dict[str, dict[str, Any]] = {}
    conflicts: list[str] = []
    for child in children:
        for record in child["artifacts"]:
            existing = artifact_by_url.get(record["url"])
            if existing is None:
                artifact_by_url[record["url"]] = dict(record)
            elif existing["sha256"] != record["sha256"]:
                conflicts.append(
                    f"{record['url']}: {child['package_id']} conflicts with an earlier child"
                )
    bundle_records: dict[str, dict[str, Any]] = {}
    for child in children:
        for record in child["source_bundles"]:
            key = str(record.get("bundle_name"))
            checksum = record.get("sha256", record.get("checksum_value"))
            entry = bundle_records.setdefault(key, {**dict(record), "provided_by": []})
            if entry.get("sha256", entry.get("checksum_value")) != checksum:
                conflicts.append(f"bundle {key}: checksum differs between children")
            if child["package_id"] not in entry["provided_by"]:
                entry["provided_by"].append(child["package_id"])
    incomplete = [
        f"{child['package_id']}: {reason}"
        for child in children
        for reason in child["incomplete_reasons"]
    ]
    incomplete.insert(0, COMPOSITE_PUBLISH_DISABLED_REASON)
    manifest: dict[str, Any] = {
        "manifest_version": PACKAGE_MANIFEST_VERSION,
        "package_id": package_id,
        "composite_kind": "union",
        "child_packages": [
            {
                "package_id": child["package_id"],
                "purpose": child["purpose"],
                "profile": child["profile"],
            }
            for child in children
        ],
        "source_bundles": [bundle_records[key] for key in sorted(bundle_records)],
        "publishable": False,
        "incomplete_reasons": incomplete + conflicts,
        "artifacts": [artifact_by_url[url] for url in sorted(artifact_by_url)],
        "timelines": [
            {
                "package_id": child["package_id"],
                "url": child["timeline"]["url"],
                "timeline_name": child["timeline"]["timeline_name"],
            }
            for child in children
        ],
        "conversion_metadata": dict(conversion_metadata),
    }
    return validate_composite_manifest(manifest)


def validate_composite_manifest(value: Any) -> dict[str, Any]:
    """Structurally validate one composite manifest document."""

    if not isinstance(value, dict):
        raise ValueError("composite manifest must be an object")
    if set(value) != COMPOSITE_MANIFEST_FIELDS:
        raise ValueError(
            f"composite manifest fields must be exactly"
            f" {sorted(COMPOSITE_MANIFEST_FIELDS)}, got {sorted(value)}"
        )
    if value["manifest_version"] != PACKAGE_MANIFEST_VERSION:
        raise ValueError("composite manifest_version must be 1")
    if value["composite_kind"] != "union":
        raise ValueError("composite_kind must be 'union'")
    if not isinstance(value["child_packages"], list) or not value["child_packages"]:
        raise ValueError("composite child_packages must be a non-empty list")
    _validate_artifact_records(value["artifacts"])
    if value.get("publishable") is not False:
        raise ValueError("composite publication is not implemented")
    reasons = value.get("incomplete_reasons")
    if (
        not isinstance(reasons, list)
        or not all(isinstance(reason, str) for reason in reasons)
        or COMPOSITE_PUBLISH_DISABLED_REASON not in reasons
    ):
        raise ValueError("unpublishable composites must record the publication limitation")
    return value


def serialize_composite_manifest(manifest: Mapping[str, Any]) -> bytes:
    """Canonical bytes for a composite's ``manifest.json``."""

    validated = validate_composite_manifest(dict(manifest))
    return json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")


def persist_release_manifest(path, manifest: Mapping[str, Any]) -> None:
    """Atomically persist one package manifest."""

    from updater.security import atomic_write_bytes

    target = StdPath(os.fspath(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, serialize_package_manifest(manifest))


def load_release_manifest(path) -> dict[str, Any] | None:
    """Load one persisted package manifest, or ``None`` when absent/corrupt."""

    target = StdPath(os.fspath(path))
    if not target.exists():
        return None
    try:
        return validate_package_manifest(json.loads(target.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return None


def timeline_document_summary(document: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the manifest-level summary fields from a playable document."""

    missing = set(TIMELINE_ARTIFACT_FIELDS) - set(document)
    if missing:
        raise ValueError(f"not a timeline document: missing {sorted(missing)}")
    return {
        "timeline_name": document.get("timeline_name", ""),
        "track_count": len(document.get("tracks", ())),
        "clip_count": sum(len(track.get("clips", ())) for track in document.get("tracks", ())),
        "animation_reference_count": len(document.get("animation_references", ())),
    }
