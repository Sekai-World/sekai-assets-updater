"""GLB purpose profiles and the metadata dependency-closure planner (Phase 1).

The planner is pure: it consumes the asset-metadata index (the same
``bundle["dependencies"]`` records the download planner uses) and produces a
validated, deterministic :class:`ExtractionPlan`. No network, no Unity
runtime, no filesystem. Traversal is iterative BFS with stable ordering;
missing, cycle, and excluded dependencies are classified into explicit plan
records instead of log text. Standard and Live2D profiles never expand a
closure, so enabling GLB flags cannot change per-Bundle behavior.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass

from updater.extract.glb.contracts import (
    COMPOSITE_PURPOSE,
    DIAG_DEPENDENCY_CYCLE,
    DIAG_DEPENDENCY_EXCLUDED,
    DIAG_META_BUNDLE_MISSING,
    EDGE_METADATA,
    EDGE_REASONS,
    LIVE2D_PURPOSE,
    SCENE3D_PURPOSE,
    STANDARD_PURPOSE,
    TIMELINE3D_PURPOSE,
    BundleRef,
    DependencyEdge,
    ExcludedDependency,
    ExtractionPlan,
    MissingDependency,
    PlanDiagnostic,
    PlanValidationError,
    RootRef,
    safe_path_segment,
)
from updater.extract.glb.observability import log_plan_built

_DIAG_PHASE_PLAN = "plan"


@dataclass(frozen=True)
class GLBProfile:
    """Purpose-driven planning policy (roadmap §5 Phase 1)."""

    name: str
    purpose: str
    expand_dependencies: bool = True
    missing_fatal: bool = True
    cycles_fatal: bool = False
    excluded_bundle_patterns: tuple[re.Pattern[str], ...] = ()
    # (pattern, required, reason) triples: dependencies matching `pattern`
    # are excluded; `required` exclusions fail plan validation, optional ones
    # stay as warning diagnostics. `reason` overrides the edge reason recorded
    # for excluded edges.
    exclusions: tuple[tuple[re.Pattern[str], bool, str], ...] = ()
    # (pattern, reason) pairs overriding the recorded edge reason when a
    # dependency is discovered through that edge (e.g. shader bundles).
    reason_overrides: tuple[tuple[re.Pattern[str], str], ...] = ()

    def __post_init__(self) -> None:
        if self.purpose not in {
            STANDARD_PURPOSE,
            LIVE2D_PURPOSE,
            SCENE3D_PURPOSE,
            TIMELINE3D_PURPOSE,
            COMPOSITE_PURPOSE,
        }:
            raise PlanValidationError(
                "unknown_purpose", f"unknown profile purpose {self.purpose!r}"
            )
        for _pattern, _required, reason in self.exclusions:
            if reason not in EDGE_REASONS:
                raise PlanValidationError("unknown_purpose", f"unknown exclusion reason {reason!r}")
        for _pattern, reason in self.reason_overrides:
            if reason not in EDGE_REASONS:
                raise PlanValidationError("unknown_purpose", f"unknown edge reason {reason!r}")

    def edge_reason(self, bundle_name: str) -> str:
        for pattern, reason in self.reason_overrides:
            if pattern.search(bundle_name):
                return reason
        return EDGE_METADATA

    def match_exclusion(self, bundle_name: str) -> tuple[bool, str] | None:
        """Return (required, reason) for the first matching exclusion, if any."""

        for pattern, required, reason in self.exclusions:
            if pattern.search(bundle_name):
                return required, reason
        return None


def standard_profile() -> GLBProfile:
    return GLBProfile(name="standard_v1", purpose=STANDARD_PURPOSE, expand_dependencies=False)


def live2d_profile() -> GLBProfile:
    return GLBProfile(name="live2d_v1", purpose=LIVE2D_PURPOSE, expand_dependencies=False)


def scene3d_profile(
    exclusions: tuple[tuple[re.Pattern[str], bool, str], ...] = (),
    reason_overrides: tuple[tuple[re.Pattern[str], str], ...] = (),
    cycles_fatal: bool = False,
) -> GLBProfile:
    return GLBProfile(
        name="scene3d_v1",
        purpose=SCENE3D_PURPOSE,
        exclusions=exclusions,
        reason_overrides=reason_overrides,
        cycles_fatal=cycles_fatal,
    )


def timeline3d_profile(
    exclusions: tuple[tuple[re.Pattern[str], bool, str], ...] = (),
    reason_overrides: tuple[tuple[re.Pattern[str], str], ...] = (),
    cycles_fatal: bool = False,
) -> GLBProfile:
    return GLBProfile(
        name="timeline3d_v1",
        purpose=TIMELINE3D_PURPOSE,
        exclusions=exclusions,
        reason_overrides=reason_overrides,
        cycles_fatal=cycles_fatal,
    )


def composite_profile(
    exclusions: tuple[tuple[re.Pattern[str], bool, str], ...] = (),
    reason_overrides: tuple[tuple[re.Pattern[str], str], ...] = (),
    cycles_fatal: bool = False,
) -> GLBProfile:
    return GLBProfile(
        name="composite_v1",
        purpose=COMPOSITE_PURPOSE,
        exclusions=exclusions,
        reason_overrides=reason_overrides,
        cycles_fatal=cycles_fatal,
    )


def _root_id_from_bundle(bundle_name: str) -> str:
    return safe_path_segment(bundle_name.rsplit("/", 1)[-1])


def select_stage_roots(
    metadata: dict, bundle_names: tuple[str, ...] | list[str]
) -> tuple[RootRef, ...]:
    """Explicitly select stage roots by exact bundle name (no substrings)."""

    return tuple(
        RootRef(
            root_id=_root_id_from_bundle(name),
            kind="stage",
            bundle_name=name,
            container_path=_first_path(metadata, name),
        )
        for name in bundle_names
    )


def select_timeline_roots(
    metadata: dict, bundle_names: tuple[str, ...] | list[str]
) -> tuple[RootRef, ...]:
    """Explicitly select Timeline roots by exact bundle name (no substrings)."""

    return tuple(
        RootRef(
            root_id=_root_id_from_bundle(name),
            kind="timeline",
            bundle_name=name,
            container_path=_first_path(metadata, name),
        )
        for name in bundle_names
    )


def select_composite_roots(
    metadata: dict, specs: tuple[tuple[str, str, str], ...] | list[tuple[str, str, str]]
) -> tuple[RootRef, ...]:
    """Select explicitly configured composite roots as ``(root_id, kind, bundle_name)``."""

    return tuple(
        RootRef(
            root_id=root_id,
            kind=kind,
            bundle_name=bundle_name,
            container_path=_first_path(metadata, bundle_name),
        )
        for root_id, kind, bundle_name in specs
    )


def _first_path(metadata: dict, bundle_name: str) -> str | None:
    paths = metadata.get(bundle_name, {}).get("paths") or []
    return paths[0] if paths else None


def _require_metadata_bundles(metadata: dict, roots: tuple[RootRef, ...]) -> None:
    for root in roots:
        if root.bundle_name not in metadata:
            raise PlanValidationError(
                DIAG_META_BUNDLE_MISSING,
                f"root {root.root_id!r} bundle {root.bundle_name!r} is absent from the metadata index",
            )


def _checksum_of(record: dict) -> str | None:
    checksum = record.get("hash") or record.get("crc")
    return str(checksum) if checksum not in (None, "") else None


def _ancestors_of(parents: dict[str, str | None], node: str) -> set[str]:
    chain = set()
    current: str | None = node
    while current is not None:
        current = parents.get(current)
        if current is not None:
            chain.add(current)
    return chain


def _closure_bfs(
    metadata: dict,
    profile: GLBProfile,
    root_names: tuple[str, ...],
) -> tuple[
    dict[str, str],
    list[DependencyEdge],
    list[MissingDependency],
    list[ExcludedDependency],
    list[PlanDiagnostic],
]:
    """Iterative, deterministic BFS with parent tracking and classification."""

    parents: dict[str, str | None] = {name: None for name in root_names}
    queue: deque[str] = deque(root_names)
    edges: list[DependencyEdge] = []
    missing: list[MissingDependency] = []
    excluded: list[ExcludedDependency] = []
    diagnostics: list[PlanDiagnostic] = []
    seen_cycles: set[frozenset[str]] = set()

    while queue:
        current = queue.popleft()
        record = metadata.get(current)
        if record is None or not profile.expand_dependencies:
            continue
        for dependency in sorted(record.get("dependencies") or []):
            if dependency == current:
                _record_cycle(seen_cycles, diagnostics, [current])
                continue
            exclusion = profile.match_exclusion(dependency)
            if exclusion is not None:
                required, reason = exclusion
                excluded.append(
                    ExcludedDependency(
                        bundle_name=dependency,
                        referenced_by=current,
                        reason=reason,
                        required=required,
                    )
                )
                if not required:
                    diagnostics.append(
                        PlanDiagnostic(
                            code=DIAG_DEPENDENCY_EXCLUDED,
                            severity="warning",
                            phase=_DIAG_PHASE_PLAN,
                            message=f"optional dependency excluded by profile: {dependency}",
                            bundle_name=dependency,
                        )
                    )
                continue
            if dependency not in metadata:
                missing.append(MissingDependency(bundle_name=dependency, referenced_by=current))
                diagnostics.append(
                    PlanDiagnostic(
                        code=DIAG_META_BUNDLE_MISSING,
                        severity="error",
                        phase=_DIAG_PHASE_PLAN,
                        message=f"dependency absent from metadata index: {dependency}",
                        bundle_name=dependency,
                    )
                )
                continue
            edges.append(
                DependencyEdge(
                    source=current,
                    target=dependency,
                    reason=profile.edge_reason(dependency),
                )
            )
            if dependency in parents:
                if dependency in _ancestors_of(parents, current):
                    _record_cycle(
                        seen_cycles, diagnostics, _cycle_chain(parents, current, dependency)
                    )
                continue
            parents[dependency] = current
            queue.append(dependency)
    return parents, edges, missing, excluded, diagnostics


def _cycle_chain(parents: dict[str, str | None], node: str, ancestor: str) -> list[str]:
    chain = [node]
    current = node
    while current != ancestor:
        current = parents.get(current)  # type: ignore[assignment]
        chain.append(current)  # type: ignore[arg-type]
    return chain


def _record_cycle(
    seen: set[frozenset[str]], diagnostics: list[PlanDiagnostic], members: list[str]
) -> None:
    key = frozenset(members)
    if key in seen:
        return
    seen.add(key)
    diagnostics.append(
        PlanDiagnostic(
            code=DIAG_DEPENDENCY_CYCLE,
            severity="warning",
            phase=_DIAG_PHASE_PLAN,
            message="dependency cycle: " + " -> ".join([*members, members[-1]]),
            bundle_name=sorted(members)[0],
        )
    )


def build_extraction_plan(
    metadata: dict,
    profile: GLBProfile,
    roots: tuple[RootRef, ...],
    *,
    package_id: str,
) -> ExtractionPlan:
    """Build and validate the deterministic extraction plan for one package."""

    _require_metadata_bundles(metadata, roots)
    root_names = tuple(root.bundle_name for root in roots)
    parents, edges, missing, excluded, diagnostics = _closure_bfs(metadata, profile, root_names)

    closure_names = sorted(parents)
    bundles = [
        BundleRef(
            bundle_name=name,
            checksum=_checksum_of(metadata.get(name, {})),
            source="metadata",
            role="root" if name in root_names else "dependency",
        )
        for name in closure_names
    ]

    plan = ExtractionPlan(
        package_id=package_id,
        purpose=profile.purpose,
        profile=profile.name,
        roots=roots,
        bundles=bundles,
        dependency_edges=edges,
        excluded_dependencies=excluded,
        missing_dependencies=missing,
        diagnostics=diagnostics,
    )
    log_plan_built(plan)
    return plan


def validate_plan(plan: ExtractionPlan, profile: GLBProfile) -> tuple[PlanDiagnostic, ...]:
    """Return the fatal problems that must block extraction (empty means valid)."""

    problems: list[PlanDiagnostic] = []
    if profile.missing_fatal and plan.missing_dependencies:
        problems.append(
            PlanDiagnostic(
                code=DIAG_META_BUNDLE_MISSING,
                severity="error",
                phase=_DIAG_PHASE_PLAN,
                message=f"{len(plan.missing_dependencies)} missing dependency(ies) fail purpose {plan.purpose}",
            )
        )
    required_exclusions = [item for item in plan.excluded_dependencies if item.required]
    if required_exclusions:
        problems.append(
            PlanDiagnostic(
                code=DIAG_DEPENDENCY_EXCLUDED,
                severity="error",
                phase=_DIAG_PHASE_PLAN,
                message="required dependencies excluded by profile: "
                + ", ".join(sorted(item.bundle_name for item in required_exclusions)),
            )
        )
    if profile.cycles_fatal:
        cycles = [item for item in plan.diagnostics if item.code == DIAG_DEPENDENCY_CYCLE]
        if cycles:
            problems.append(
                PlanDiagnostic(
                    code=DIAG_DEPENDENCY_CYCLE,
                    severity="error",
                    phase=_DIAG_PHASE_PLAN,
                    message=f"{len(cycles)} dependency cycle(s) are fatal for profile {profile.name}",
                )
            )
    return tuple(problems)


def ensure_plan_valid(plan: ExtractionPlan, profile: GLBProfile) -> None:
    """Raise before extraction when validation finds fatal problems."""

    problems = validate_plan(plan, profile)
    if problems:
        raise PlanValidationError(
            problems[0].code,
            f"plan {plan.package_id!r} failed validation: "
            + "; ".join(p.message for p in problems),
        )


def union_requirements(
    plans: tuple[ExtractionPlan, ...] | list[ExtractionPlan],
) -> tuple[BundleRef, ...]:
    """Merge plan closures into one deduplicated, sorted download requirement list."""

    merged: dict[str, BundleRef] = {}
    for plan in plans:
        for ref in plan.bundles:
            merged.setdefault(ref.bundle_name, ref)
    return tuple(merged[name] for name in sorted(merged))
