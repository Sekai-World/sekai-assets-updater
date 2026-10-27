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
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path as StdPath
from typing import Any, Callable, Mapping, Sequence

from updater.extract.glb.contracts import (
    DIAG_ARTIFACT_INCOMPLETE,
    safe_relative_path,
)
from updater.extract.glb.gltf_writer import content_hash
from updater.extract.glb.timeline import TIMELINE_ARTIFACT_FIELDS

PACKAGE_MANIFEST_VERSION = 1
TIMELINE_PLAYABLE_URL = "timeline.playable.json"
PACKAGE_MANIFEST_URL = "manifest.json"
TIMELINE_ARTIFACT_KIND = "timeline_playable_document"

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
    track_count = len(timeline_document.get("tracks", ()))
    clip_count = sum(len(track.get("clips", ())) for track in timeline_document["tracks"])
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
            "timeline_name": timeline_document.get("timeline_name", ""),
            "track_count": track_count,
            "clip_count": clip_count,
            "animation_reference_count": len(timeline_document.get("animation_references", ())),
        },
        "glb_roots": [dict(record) for record in glb_roots],
        "conversion_metadata": dict(conversion_metadata),
        "diagnostics": [dict(diagnostic) for diagnostic in diagnostics],
    }
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
    if not isinstance(value["publishable"], bool):
        raise ValueError("package manifest.publishable must be a boolean")
    reasons = value["incomplete_reasons"]
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise ValueError("package manifest.incomplete_reasons must be string list")
    if not value["publishable"] and not reasons:
        raise ValueError("unpublishable packages must record incomplete_reasons")
    if value["publishable"] and reasons:
        raise ValueError("publishable packages must not record incomplete_reasons")
    _validate_artifact_records(value["artifacts"])
    timeline = value["timeline"]
    if not isinstance(timeline, dict) or set(timeline) != TIMELINE_SECTION_FIELDS:
        raise ValueError(
            f"package manifest.timeline must have fields {sorted(TIMELINE_SECTION_FIELDS)}"
        )
    if timeline["url"] != TIMELINE_PLAYABLE_URL:
        raise ValueError(f"timeline section must point at {TIMELINE_PLAYABLE_URL}")
    if not isinstance(value["glb_roots"], list):
        raise ValueError("package manifest.glb_roots must be a list")
    for record in value["glb_roots"]:
        if not isinstance(record, dict) or set(record) != GLB_ROOT_SECTION_FIELDS:
            raise ValueError(f"glb_root records must have fields {sorted(GLB_ROOT_SECTION_FIELDS)}")
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
        if not isinstance(record["byte_length"], int) or record["byte_length"] <= 0:
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
        if len(payload) != record["byte_length"]:
            problems.append(
                f"{url}: byte_length {len(payload)} != recorded {record['byte_length']}"
            )
        if content_hash(payload) != record["sha256"]:
            problems.append(f"{url}: sha256 mismatch")
    if TIMELINE_PLAYABLE_URL not in declared_urls:
        problems.append(f"{TIMELINE_PLAYABLE_URL}: not declared in artifacts")
    return PackageCompleteness(publishable=not problems, problems=problems)


def validate_package_references(
    manifest: Mapping[str, Any], timeline_document: Mapping[str, Any]
) -> list[str]:
    """Referential integrity between the manifest and its playable document.

    Every animation reference in the playable document must map to a
    declared animation artifact URL, and the manifest's timeline summary
    must agree with the document's actual counts.
    """

    problems: list[str] = []
    declared = {record["url"] for record in manifest["artifacts"]}
    for reference in timeline_document.get("animation_references", ()):
        url = animation_artifact_url(reference)
        if url not in declared:
            problems.append(
                f"animation reference {reference.get('path_id')} maps to undeclared artifact {url}"
            )
    timeline = manifest["timeline"]
    actual = {
        "track_count": len(timeline_document.get("tracks", ())),
        "clip_count": sum(
            len(track.get("clips", ())) for track in timeline_document.get("tracks", ())
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
    root = StdPath(os.fspath(directory))
    root.mkdir(parents=True, exist_ok=True)
    for artifact in sorted(artifacts, key=lambda a: (a.url == TIMELINE_PLAYABLE_URL, a.url)):
        record = declared[artifact.url]
        if record["sha256"] != artifact.sha256:
            raise ValueError(f"artifact {artifact.url} does not match its manifest hash")
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
    disagreement — never calls the uploader.
    """

    validated = validate_package_manifest(dict(manifest))
    if not validated["publishable"]:
        raise PublicationRefused(validated["incomplete_reasons"])
    declared = {record["url"]: record for record in validated["artifacts"]}
    if {artifact.url for artifact in artifacts} != set(declared):
        raise ValueError("publish artifacts must match the manifest exactly")
    for artifact in artifacts:
        if declared[artifact.url]["sha256"] != artifact.sha256:
            raise PublicationRefused([f"{artifact.url}: payload does not match its manifest hash"])
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
    composite is publishable only when every child is.
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
        "publishable": not incomplete and not conflicts,
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
    if not isinstance(value["publishable"], bool):
        raise ValueError("composite publishable must be a boolean")
    if not value["publishable"] and not value["incomplete_reasons"]:
        raise ValueError("unpublishable composites must record incomplete_reasons")
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
