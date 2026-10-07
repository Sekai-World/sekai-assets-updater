"""Multi-Bundle collection loading, cross-file PPtr resolution, and L2
reachability validation for streaming Live GLB packages (#37).

A logical package's validated Bundles are loaded as one Unity collection.
Cross-file references are resolved through the collection's actual file
identity table (the ``AssetBundle`` dependency order of each input), never by
position or filename guessing.  Reachability validation walks the object
graph from the plan's roots and classifies every reference as resolved,
null, missing, unresolved-external, mismatched, or unsupported.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

from updater.extract.glb.contracts import (
    DIAG_MISSING_TARGET,
    DIAG_NULL_REFERENCE,
    DIAG_ROOT_OBJECT_MISSING,
    DIAG_TYPE_MISMATCH,
    DIAG_UNRESOLVED_EXTERNAL,
    DIAG_UNSUPPORTED_REFERENCE,
    SEVERITIES,
    ExtractionPlan,
    PlanDiagnostic,
)
from updater.state import atomic_write_json
from updater.unity_rs_adapter import (
    RESOLVED_EXTERNAL_REFERENCE,
    SAME_FILE_REFERENCE,
    UNKNOWN_EXTERNAL_REFERENCE,
    CollectionFileTable,
    UnityRsEnvironment,
    UnityRsObject,
    adapter_version,
    build_collection_file_table,
    load_collection,
)

logger = logging.getLogger("asset_updater")

PPTR_KEYS = frozenset({"m_FileID", "m_PathID"})

# Severity per finding kind.  Nulls (m_FileID == 0, m_PathID == 0) are normal
# in Unity data (empty texture slots and the like) and unsupported references
# keep working as structured warnings; only genuine breakage is fatal for
# publishing.
FINDING_SEVERITIES: Mapping[str, str] = {
    DIAG_ROOT_OBJECT_MISSING: "error",
    DIAG_MISSING_TARGET: "error",
    DIAG_TYPE_MISMATCH: "error",
    DIAG_UNRESOLVED_EXTERNAL: "error",
    DIAG_NULL_REFERENCE: "info",
    DIAG_UNSUPPORTED_REFERENCE: "warning",
}


@dataclass(frozen=True, slots=True)
class PptrLocation:
    """One PPtr encountered inside a reachable object's type tree."""

    field_path: str
    file_id: int
    path_id: int


@dataclass(frozen=True, slots=True)
class ReferenceFinding:
    """One classified reference problem, with source and target identities."""

    code: str
    severity: str
    source_file_index: int
    source_path_id: int
    source_class: str
    field_path: str
    target_file_index: int | None = None
    target_path_id: int | None = None
    target_class: str | None = None
    dependency_name: str | None = None

    def __post_init__(self) -> None:
        if self.code not in FINDING_SEVERITIES:
            raise ValueError(f"unknown reference finding code: {self.code}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown reference finding severity: {self.severity}")

    @property
    def sort_key(self) -> tuple:
        return (
            self.code,
            self.source_file_index,
            self.source_path_id,
            self.field_path,
            self.target_file_index if self.target_file_index is not None else -1,
            self.target_path_id if self.target_path_id is not None else -1,
        )

    def as_diagnostic(self) -> PlanDiagnostic:
        """Project the finding onto the shared plan diagnostic vocabulary."""

        target = (
            self.dependency_name
            if self.dependency_name is not None
            else f"({self.target_file_index},{self.target_path_id})"
        )
        return PlanDiagnostic(
            code=self.code,
            severity=self.severity,
            phase="extract",
            message=(
                f"{self.source_class}({self.source_file_index},{self.source_path_id})"
                f" field {self.field_path} -> {target}"
            ),
        )


@dataclass(frozen=True, slots=True)
class ReachabilityReport:
    """Result of walking a collection from the plan's selected roots."""

    roots: tuple[str, ...]
    findings: tuple[ReferenceFinding, ...] = field(default=())
    visited_objects: int = 0

    @property
    def publishable(self) -> bool:
        return not any(finding.severity == "error" for finding in self.findings)

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.code] = counts.get(finding.code, 0) + 1
        return {
            "roots": list(self.roots),
            "visited_objects": self.visited_objects,
            "findings_by_code": dict(sorted(counts.items())),
            "publishable": self.publishable,
        }


@dataclass(frozen=True, slots=True)
class CrossFileResolution:
    """Outcome of resolving one PPtr against the collection identity table."""

    status: str
    object: UnityRsObject | None = None
    target_file_index: int | None = None
    dependency_name: str | None = None


def iter_pptr_locations(typetree: Any, field_path: str = "") -> Iterator[PptrLocation]:
    """Yield every PPtr-shaped node in a type tree with its field path.

    A PPtr is a mapping with exactly the ``m_FileID``/``m_PathID`` integer
    fields; anything else (including subclasses carrying more data) is left
    to the caller's own handling.
    """

    if isinstance(typetree, Mapping):
        if set(typetree) == PPTR_KEYS:
            file_id = typetree["m_FileID"]
            path_id = typetree["m_PathID"]
            if isinstance(file_id, int) and isinstance(path_id, int):
                yield PptrLocation(field_path or "$", file_id, path_id)
            return
        for key in sorted(typetree):
            child = typetree[key]
            child_path = f"{field_path}.{key}" if field_path else str(key)
            yield from iter_pptr_locations(child, child_path)
    elif isinstance(typetree, (list, tuple)):
        for index, item in enumerate(typetree):
            yield from iter_pptr_locations(item, f"{field_path}[{index}]")


def resolve_pptr(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    source_file_index: int,
    file_id: int,
    path_id: int,
) -> CrossFileResolution:
    """Resolve one PPtr through the collection's actual identity table."""

    if file_id == 0:
        target = environment.object_by_identity(source_file_index, path_id)
        return CrossFileResolution(
            status=SAME_FILE_REFERENCE,
            object=target,
            target_file_index=source_file_index,
        )
    external = table.resolve_file_id(source_file_index, file_id)
    if external.kind != RESOLVED_EXTERNAL_REFERENCE:
        return CrossFileResolution(
            status=UNKNOWN_EXTERNAL_REFERENCE,
            dependency_name=external.dependency_name,
        )
    target = environment.object_by_identity(external.target_file_index, path_id)
    return CrossFileResolution(
        status=RESOLVED_EXTERNAL_REFERENCE,
        object=target,
        target_file_index=external.target_file_index,
        dependency_name=external.dependency_name,
    )


def _finding(
    code: str,
    source: UnityRsObject,
    location: PptrLocation,
    *,
    target_file_index: int | None = None,
    target_path_id: int | None = None,
    target_class: str | None = None,
    dependency_name: str | None = None,
) -> ReferenceFinding:
    return ReferenceFinding(
        code=code,
        severity=FINDING_SEVERITIES[code],
        source_file_index=source.file_index,
        source_path_id=source.path_id,
        source_class=source.type.name,
        field_path=location.field_path,
        target_file_index=target_file_index,
        target_path_id=target_path_id,
        target_class=target_class,
        dependency_name=dependency_name,
    )


def _resolve_root_objects(
    environment: UnityRsEnvironment,
    roots: Sequence[Any],
) -> tuple[list[UnityRsObject], list[ReferenceFinding]]:
    objects: list[UnityRsObject] = []
    findings: list[ReferenceFinding] = []
    for root in roots:
        obj = None
        if (
            getattr(root, "file_index", None) is not None
            and getattr(root, "path_id", None) is not None
        ):
            obj = environment.object_by_identity(root.file_index, root.path_id)
        if obj is None and getattr(root, "container_path", None):
            obj = environment.container.get(root.container_path)
        if obj is None:
            findings.append(
                ReferenceFinding(
                    code=DIAG_ROOT_OBJECT_MISSING,
                    severity=FINDING_SEVERITIES[DIAG_ROOT_OBJECT_MISSING],
                    source_file_index=-1,
                    source_path_id=-1,
                    source_class="root",
                    field_path=getattr(root, "root_id", "<unknown>"),
                    dependency_name=getattr(root, "bundle_name", None),
                )
            )
            continue
        objects.append(obj)
    return objects, findings


def _classify_and_enqueue(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    source: UnityRsObject,
    location: PptrLocation,
    expected_classes: Mapping[str, frozenset[int]] | None,
    optional_dependencies: frozenset[str],
) -> tuple[ReferenceFinding | None, UnityRsObject | None]:
    if location.file_id == 0 and location.path_id == 0:
        return (
            _finding(DIAG_NULL_REFERENCE, source, location),
            None,
        )
    resolution = resolve_pptr(
        environment, table, source.file_index, location.file_id, location.path_id
    )
    if resolution.status == UNKNOWN_EXTERNAL_REFERENCE:
        severity_override = None
        if resolution.dependency_name in optional_dependencies:
            severity_override = "warning"
        finding = _finding(
            DIAG_UNRESOLVED_EXTERNAL,
            source,
            location,
            dependency_name=resolution.dependency_name,
        )
        if severity_override is not None:
            finding = ReferenceFinding(
                code=finding.code,
                severity=severity_override,
                source_file_index=finding.source_file_index,
                source_path_id=finding.source_path_id,
                source_class=finding.source_class,
                field_path=finding.field_path,
                target_file_index=finding.target_file_index,
                target_path_id=finding.target_path_id,
                target_class=finding.target_class,
                dependency_name=finding.dependency_name,
            )
        return finding, None
    target = resolution.object
    if target is None:
        return (
            _finding(
                DIAG_MISSING_TARGET,
                source,
                location,
                target_file_index=resolution.target_file_index,
                target_path_id=location.path_id,
            ),
            None,
        )
    if expected_classes:
        expected = expected_classes.get(location.field_path)
        if expected is not None and target.class_id not in expected:
            return (
                _finding(
                    DIAG_TYPE_MISMATCH,
                    source,
                    location,
                    target_file_index=target.file_index,
                    target_path_id=target.path_id,
                    target_class=target.type.name,
                ),
                None,
            )
    return None, target


def validate_collection_reachability(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    roots: Sequence[Any],
    *,
    expected_classes: Mapping[str, frozenset[int]] | None = None,
    optional_dependencies: Iterable[str] = (),
) -> ReachabilityReport:
    """Walk the reachable object graph from the plan's roots.

    Every PPtr in every visited object's type tree is resolved through the
    collection's actual file identity table and classified.  Traversal order
    is deterministic: roots are visited in plan order and expansions sort by
    ``(file_index, path_id)``.
    """

    root_objects, findings = _resolve_root_objects(environment, roots)
    visited: dict[tuple[int, int], UnityRsObject] = {}
    queue: deque[UnityRsObject] = deque(root_objects)
    optional = frozenset(optional_dependencies)
    root_ids = tuple(getattr(root, "root_id", str(index)) for index, root in enumerate(roots))
    while queue:
        current = queue.popleft()
        key = (current.file_index, current.path_id)
        if key in visited:
            continue
        visited[key] = current
        try:
            typetree = current.read_typetree()
        except Exception:  # NOSONAR - unreadable object is a structured finding
            findings.append(
                _finding(
                    DIAG_UNSUPPORTED_REFERENCE,
                    current,
                    PptrLocation(field_path="<typetree>", file_id=0, path_id=0),
                )
            )
            continue
        for location in iter_pptr_locations(typetree):
            finding, target = _classify_and_enqueue(
                environment, table, current, location, expected_classes, optional
            )
            if finding is not None:
                findings.append(finding)
            if target is not None and (target.file_index, target.path_id) not in visited:
                queue.append(target)
    ordered = tuple(sorted(findings, key=lambda finding: finding.sort_key))
    return ReachabilityReport(
        roots=root_ids,
        findings=ordered,
        visited_objects=len(visited),
    )


def load_package_collection(
    plan: ExtractionPlan,
    bundle_payloads: Sequence[tuple[str, bytes]],
    unity_version: str,
) -> tuple[UnityRsEnvironment, CollectionFileTable]:
    """Load the plan's validated bundles as one collection.

    Every payload name must be a plan input so the collection can never
    silently contain work outside the plan.
    """

    plan_names = {ref.bundle_name for ref in plan.bundles}
    payload_names = {name for name, _ in bundle_payloads}
    unexpected = sorted(payload_names - plan_names)
    if unexpected:
        raise ValueError(f"payloads outside the plan: {unexpected}")
    missing = sorted(plan_names - payload_names)
    if missing:
        raise ValueError(f"plan inputs missing payloads: {missing}")
    environment = load_collection(list(bundle_payloads), unity_version)
    return environment, build_collection_file_table(environment)


def input_bundle_checksums(bundle_payloads: Sequence[tuple[str, bytes]]) -> dict[str, str]:
    """SHA-256 per input bundle, keyed by the portable bundle name."""

    return {
        name: hashlib.sha256(payload).hexdigest()
        for name, payload in sorted(bundle_payloads, key=lambda item: item[0])
    }


def _load_diagnostics(environment: UnityRsEnvironment, limit: int = 50) -> dict[str, Any]:
    studio = environment.studio
    count = getattr(studio, "load_diagnostic_count", 0)
    entries: list[dict[str, str]] = []
    page = getattr(studio, "load_diagnostic_page", None)
    if callable(page) and count:
        for diagnostic in page(offset=0, limit=limit):
            entries.append(
                {
                    "path": str(getattr(diagnostic, "path", "")),
                    "message": str(getattr(diagnostic, "message", "")),
                }
            )
    return {"count": int(count), "entries": entries}


def build_package_manifest(
    plan: ExtractionPlan,
    bundle_payloads: Sequence[tuple[str, bytes]],
    unity_version: str,
    report: ReachabilityReport,
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
) -> dict[str, Any]:
    """Assemble the package manifest: inputs, versions, and L0/L2 summaries."""

    return {
        "manifest_version": 1,
        "package_id": plan.package_id,
        "collection_id": plan.collection_id(),
        "unity_version": unity_version,
        "adapter_version": adapter_version() or "unknown",
        "inputs": input_bundle_checksums(bundle_payloads),
        "files": [
            {
                "file_index": identity.file_index,
                "bundle_name": identity.bundle_name,
                "cab_name": identity.cab_name,
                "unity_version": identity.unity_version,
            }
            for identity in table.identities
        ],
        "l0": _load_diagnostics(environment),
        "l2": report.summary(),
        "diagnostics": [finding.as_diagnostic().__dict__ for finding in report.findings],
    }


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("package manifest must be an object")
    required = {
        "manifest_version",
        "package_id",
        "collection_id",
        "unity_version",
        "adapter_version",
        "inputs",
        "files",
        "l0",
        "l2",
        "diagnostics",
    }
    if set(value) != required:
        raise ValueError(
            f"package manifest fields must be exactly {sorted(required)}, got {sorted(value)}"
        )
    if value["manifest_version"] != 1:
        raise ValueError("package manifest_version must be 1")
    for key in ("package_id", "collection_id", "unity_version", "adapter_version"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"package manifest.{key} must be a non-empty string")
    if not isinstance(value["inputs"], dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in value["inputs"].items()
    ):
        raise ValueError("package manifest.inputs must map names to checksums")
    return value


def persist_package_manifest(path, manifest: dict[str, Any]) -> None:
    """Atomically persist one package manifest."""

    atomic_write_json(path, manifest, _validate_manifest)


class PackageNotPublishable(RuntimeError):
    """A package's required L2 validation failed; outputs were not written."""

    def __init__(self, report: ReachabilityReport) -> None:
        errors = [finding for finding in report.findings if finding.severity == "error"]
        detail = "; ".join(
            f"{finding.code} from ({finding.source_file_index},{finding.source_path_id})"
            f"{f' field {finding.field_path}' if finding.field_path else ''}"
            f" -> {finding.dependency_name or (finding.target_file_index, finding.target_path_id)}"
            for finding in errors[:5]
        )
        super().__init__(f"package is not publishable: {len(errors)} error finding(s): {detail}")
        self.report = report


@dataclass(frozen=True, slots=True)
class GlbCollectionExtraction:
    """One validated package collection, ready for Phase 4 export."""

    manifest: dict[str, Any]
    report: ReachabilityReport


def load_package_manifest(path):
    """Load a persisted package manifest, or ``None`` when absent/corrupt."""

    import json
    from pathlib import Path as StdPath

    target = StdPath(os.fspath(path))
    if not target.exists():
        return None
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        return _validate_manifest(document)
    except (ValueError, OSError):
        logger.warning("Ignoring unreadable package manifest: %s", target)
        return None
