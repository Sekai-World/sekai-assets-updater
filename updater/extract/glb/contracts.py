"""Streaming Live GLB extraction-plan contracts (GLB roadmap Phase 0, #34).

Frozen, validated, deterministically serializable descriptions of collection
extraction work. The planner (Phase 1) constructs plans before extraction;
this module only defines and enforces the contract. Serialization is canonical
so the same logical plan produces byte-equivalent output on repeated runs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

PLAN_VERSION = 1

STANDARD_PURPOSE = "standard"
LIVE2D_PURPOSE = "live2d"
SCENE3D_PURPOSE = "scene3d"
TIMELINE3D_PURPOSE = "timeline3d"
COMPOSITE_PURPOSE = "composite"

EXTRACTION_PURPOSES = frozenset(
    {STANDARD_PURPOSE, LIVE2D_PURPOSE, SCENE3D_PURPOSE, TIMELINE3D_PURPOSE, COMPOSITE_PURPOSE}
)
# Only these purposes aggregate multi-Bundle dependency closures (roadmap §3).
GLB_PURPOSES = frozenset({SCENE3D_PURPOSE, TIMELINE3D_PURPOSE, COMPOSITE_PURPOSE})

EDGE_METADATA = "metadata"
EDGE_ROOT_REFERENCE = "root_reference"
EDGE_SHADER = "shader"
EDGE_TEXTURE = "texture"
EDGE_ANIMATION = "animation"
EDGE_MEDIA = "media"
EDGE_REASONS = frozenset(
    {EDGE_METADATA, EDGE_ROOT_REFERENCE, EDGE_SHADER, EDGE_TEXTURE, EDGE_ANIMATION, EDGE_MEDIA}
)

SEVERITIES = ("info", "warning", "error")

# Stable diagnostic codes (roadmap §9). Phase 0 defines the plan-side codes;
# extraction/export codes join them in later phases.
DIAG_UNKNOWN_PURPOSE = "unknown_purpose"
DIAG_UNSAFE_PATH = "unsafe_path"
DIAG_DUPLICATE_BUNDLE = "duplicate_bundle_identity"
DIAG_ROOT_BUNDLE_MISSING = "root_bundle_missing"
DIAG_EDGE_UNKNOWN_BUNDLE = "edge_unknown_bundle"
DIAG_META_BUNDLE_MISSING = "metadata_bundle_missing"
DIAG_DEPENDENCY_CYCLE = "dependency_cycle"
DIAG_DEPENDENCY_EXCLUDED = "dependency_excluded"
DIAG_CACHE_MISSING = "cache_missing"
DIAG_BUNDLE_DOWNLOAD_FAILED = "bundle_download_failed"

# Collection validation codes (Phase 3).  These classify every way a
# cross-file reference from a reachable root can fail or stay unsupported.
DIAG_ROOT_OBJECT_MISSING = "root_object_missing"
DIAG_NULL_REFERENCE = "null_reference"
DIAG_MISSING_TARGET = "missing_target"
DIAG_UNRESOLVED_EXTERNAL = "unresolved_external"
DIAG_TYPE_MISMATCH = "type_mismatch"
DIAG_UNSUPPORTED_REFERENCE = "unsupported_reference"


class PlanValidationError(ValueError):
    """Raised when an extraction plan violates the Phase 0 contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def safe_path_segment(value: Any) -> str:
    """Validate one filesystem path segment and return it unchanged."""

    if not isinstance(value, str) or not value:
        raise PlanValidationError(
            DIAG_UNSAFE_PATH, f"path segment must be a non-empty string, got {value!r}"
        )
    if "\\" in value or "/" in value:
        raise PlanValidationError(
            DIAG_UNSAFE_PATH, f"path segment must not contain separators: {value!r}"
        )
    if value in {".", ".."} or any(ord(ch) < 0x20 for ch in value):
        raise PlanValidationError(
            DIAG_UNSAFE_PATH, f"path segment contains unsafe characters: {value!r}"
        )
    return value


def safe_relative_path(value: Any) -> str:
    """Validate a relative, forward-slash package path and return it unchanged."""

    if not isinstance(value, str) or not value:
        raise PlanValidationError(
            DIAG_UNSAFE_PATH, f"path must be a non-empty string, got {value!r}"
        )
    if value.startswith("/") or value.endswith("/"):
        raise PlanValidationError(
            DIAG_UNSAFE_PATH, f"path must not start or end with '/': {value!r}"
        )
    for segment in value.split("/"):
        safe_path_segment(segment)
    return value


@dataclass(frozen=True)
class RootRef:
    """One requested Unity asset root, identified by stable metadata identity."""

    root_id: str
    kind: str
    bundle_name: str
    container_path: str | None = None
    file_index: int | None = None
    path_id: int | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        safe_path_segment(self.root_id)
        if not isinstance(self.kind, str) or not self.kind:
            raise PlanValidationError(
                DIAG_UNSAFE_PATH, f"root kind must be a non-empty string, got {self.kind!r}"
            )
        if not isinstance(self.bundle_name, str) or not self.bundle_name:
            raise PlanValidationError(
                DIAG_UNSAFE_PATH,
                f"root bundle_name must be a non-empty string, got {self.bundle_name!r}",
            )

    def sort_key(self) -> tuple[str, str, str]:
        return (self.root_id, self.kind, self.bundle_name)


@dataclass(frozen=True)
class BundleRef:
    """One physical Bundle input with its source metadata identity."""

    bundle_name: str
    checksum: str | None = None
    source: str | None = None
    role: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.bundle_name, str) or not self.bundle_name:
            raise PlanValidationError(
                DIAG_UNSAFE_PATH,
                f"bundle_name must be a non-empty string, got {self.bundle_name!r}",
            )

    def sort_key(self) -> tuple[str, str | None, str | None]:
        return (self.bundle_name, self.checksum, self.role)


@dataclass(frozen=True)
class DependencyEdge:
    """Why one Bundle depends on another inside this closure."""

    source: str
    target: str
    reason: str

    def __post_init__(self) -> None:
        if self.reason not in EDGE_REASONS:
            raise PlanValidationError(
                DIAG_UNSAFE_PATH,
                f"dependency reason must be one of {sorted(EDGE_REASONS)}, got {self.reason!r}",
            )

    def sort_key(self) -> tuple[str, str, str]:
        return (self.source, self.target, self.reason)


@dataclass(frozen=True)
class MissingDependency:
    """A dependency referenced by the metadata but absent from the index."""

    bundle_name: str
    referenced_by: str
    reason: str = EDGE_METADATA


@dataclass(frozen=True)
class ExcludedDependency:
    """A dependency deliberately removed by purpose/profile policy."""

    bundle_name: str
    referenced_by: str
    reason: str = EDGE_METADATA
    required: bool = False


@dataclass(frozen=True)
class PlanDiagnostic:
    """One structured plan/extraction diagnostic (never log-text-only)."""

    code: str
    severity: str
    phase: str
    message: str = ""
    root_id: str | None = None
    bundle_name: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise PlanValidationError(
                DIAG_UNSAFE_PATH,
                f"severity must be one of {list(SEVERITIES)}, got {self.severity!r}",
            )

    def sort_key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.phase,
            self.code,
            self.severity,
            self.root_id or "",
            self.bundle_name or "",
            self.message,
        )


def _unique_sorted(items: Iterable[Any], key) -> tuple:
    seen: dict[Any, Any] = {}
    for item in items:
        seen.setdefault(key(item), item)
    return tuple(seen[name] for name in sorted(seen))


@dataclass(frozen=True)
class ExtractionPlan:
    """Deterministic description of one logical package's extraction work."""

    package_id: str
    purpose: str
    profile: str
    roots: tuple[RootRef, ...] = field(default=())
    bundles: tuple[BundleRef, ...] = field(default=())
    dependency_edges: tuple[DependencyEdge, ...] = field(default=())
    excluded_dependencies: tuple[ExcludedDependency, ...] = field(default=())
    missing_dependencies: tuple[MissingDependency, ...] = field(default=())
    diagnostics: tuple[PlanDiagnostic, ...] = field(default=())
    plan_version: int = PLAN_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.plan_version, int) or self.plan_version < 1:
            raise PlanValidationError(
                DIAG_UNSAFE_PATH,
                f"plan_version must be a positive integer, got {self.plan_version!r}",
            )
        if self.purpose not in EXTRACTION_PURPOSES:
            raise PlanValidationError(
                DIAG_UNKNOWN_PURPOSE,
                f"purpose must be one of {sorted(EXTRACTION_PURPOSES)}, got {self.purpose!r}",
            )
        if not isinstance(self.profile, str) or not self.profile:
            raise PlanValidationError(
                DIAG_UNSAFE_PATH, f"profile must be a non-empty string, got {self.profile!r}"
            )
        safe_relative_path(self.package_id)

        roots = _unique_sorted(self.roots, RootRef.sort_key)
        raw_root_ids = [root.root_id for root in self.roots]
        if len(set(raw_root_ids)) != len(raw_root_ids):
            raise PlanValidationError(
                DIAG_DUPLICATE_BUNDLE,
                f"duplicate root identities in plan: {sorted({rid for rid in raw_root_ids if raw_root_ids.count(rid) > 1})}",
            )
        raw_bundle_names = [ref.bundle_name for ref in self.bundles]
        if len(set(raw_bundle_names)) != len(raw_bundle_names):
            duplicates = sorted(
                {name for name in raw_bundle_names if raw_bundle_names.count(name) > 1}
            )
            raise PlanValidationError(
                DIAG_DUPLICATE_BUNDLE, f"duplicate bundle identities in plan: {duplicates}"
            )
        bundles = _unique_sorted(self.bundles, BundleRef.sort_key)
        edges = _unique_sorted(self.dependency_edges, DependencyEdge.sort_key)
        excluded = _unique_sorted(
            self.excluded_dependencies, lambda d: (d.bundle_name, d.referenced_by, d.reason)
        )
        missing = _unique_sorted(
            self.missing_dependencies, lambda d: (d.bundle_name, d.referenced_by, d.reason)
        )
        diagnostics = _unique_sorted(self.diagnostics, PlanDiagnostic.sort_key)

        bundle_names = {ref.bundle_name for ref in bundles}
        if not roots and self.purpose != LIVE2D_PURPOSE:
            raise PlanValidationError(
                DIAG_ROOT_BUNDLE_MISSING, f"purpose {self.purpose!r} requires at least one root"
            )
        for root in roots:
            if root.bundle_name not in bundle_names:
                raise PlanValidationError(
                    DIAG_ROOT_BUNDLE_MISSING,
                    f"root {root.root_id!r} references bundle {root.bundle_name!r} that is not a plan input",
                )
        for edge in edges:
            for endpoint in (edge.source, edge.target):
                if endpoint not in bundle_names:
                    raise PlanValidationError(
                        DIAG_EDGE_UNKNOWN_BUNDLE,
                        f"dependency edge references unknown bundle {endpoint!r}",
                    )

        object.__setattr__(self, "roots", roots)
        object.__setattr__(self, "bundles", bundles)
        object.__setattr__(self, "dependency_edges", edges)
        object.__setattr__(self, "excluded_dependencies", excluded)
        object.__setattr__(self, "missing_dependencies", missing)
        object.__setattr__(self, "diagnostics", diagnostics)

    def bundle_names(self) -> tuple[str, ...]:
        return tuple(ref.bundle_name for ref in self.bundles)

    def to_dict(self) -> dict[str, Any]:
        """Canonical, order-stable dictionary form of this plan."""

        return {
            "plan_version": self.plan_version,
            "package_id": self.package_id,
            "purpose": self.purpose,
            "profile": self.profile,
            "roots": [dataclasses.asdict(root) for root in self.roots],
            "bundles": [dataclasses.asdict(ref) for ref in self.bundles],
            "dependency_edges": [dataclasses.asdict(edge) for edge in self.dependency_edges],
            "excluded_dependencies": [
                dataclasses.asdict(item) for item in self.excluded_dependencies
            ],
            "missing_dependencies": [
                dataclasses.asdict(item) for item in self.missing_dependencies
            ],
            "diagnostics": [dataclasses.asdict(item) for item in self.diagnostics],
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def collection_id(self) -> str:
        """Stable package identity from the ordered bundle inputs and plan version.

        Deliberately excludes diagnostics, paths, and anything run-specific
        (roadmap §2): a plan whose recorded warnings change keeps its identity.
        """

        identity = {
            "plan_version": self.plan_version,
            "bundle_identities": [
                {"bundleName": ref.bundle_name, "checksum": ref.checksum} for ref in self.bundles
            ],
        }
        return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:16]


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Serialize to deterministic compact UTF-8 JSON bytes."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


# Phase 5 material/animation codes.
DIAG_UNSUPPORTED_SHADER = "unsupported_shader"
DIAG_SKIN_WEIGHTS_UNAVAILABLE = "skin_weights_unavailable"
DIAG_ANIMATION_UNSUPPORTED_BINDING = "animation_unsupported_binding"
