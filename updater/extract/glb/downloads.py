"""GLB package download/cache integration (GLB roadmap Phase 2, #36).

Bridges deterministic extraction plans to the Bundle-oriented download and
cache pipeline. Bundle identity and standard behavior never change: this
module only (a) computes the deduplicated union of closure members as exact
``required_bundle_names`` for :func:`updater.net.plan.get_download_list`,
(b) evaluates per-package readiness from the shared cache so collection
extraction cannot start on an incomplete package, and (c) persists
plan-to-Bundle associations in their own versioned document, separate from
Bundle cache metadata, so cache invalidation cannot destroy package identity.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

from updater.extract.glb.contracts import (
    DIAG_BUNDLE_DOWNLOAD_FAILED,
    DIAG_CACHE_MISSING,
    PLAN_VERSION,
    BundleRef,
    ExtractionPlan,
    PlanDiagnostic,
    canonical_json_bytes,
)

logger = logging.getLogger("asset_updater")

_ASSOCIATION_VERSION = 1
_DIAG_PHASE_DOWNLOAD = "download"
_DIAG_PHASE_READINESS = "readiness"


@dataclass(frozen=True)
class PackageReadiness:
    """Whether one package's closure is fully cached and checksum-validated."""

    package_id: str
    collection_id: str
    ready: bool
    missing_bundles: tuple[str, ...] = ()
    changed_bundles: tuple[str, ...] = ()
    diagnostics: tuple[PlanDiagnostic, ...] = ()


def plan_download_requirements(
    plans: tuple[ExtractionPlan, ...] | list[ExtractionPlan],
) -> tuple[BundleRef, ...]:
    """Union every plan's closure into one deduplicated, sorted requirement list."""

    merged: dict[str, BundleRef] = {}
    for plan in plans:
        for ref in plan.bundles:
            merged.setdefault(ref.bundle_name, ref)
    return tuple(merged[name] for name in sorted(merged))


def requirement_names(plans: tuple[ExtractionPlan, ...] | list[ExtractionPlan]) -> tuple[str, ...]:
    """Exact bundle names to pass as ``required_bundle_names`` to download planning."""

    return tuple(ref.bundle_name for ref in plan_download_requirements(plans))


def bundle_record_changed(plan_checksum: str | None, current_record: dict) -> bool:
    """Whether the current metadata checksum differs from the plan's record."""

    if plan_checksum is None:
        return False
    current = current_record.get("hash") or current_record.get("crc")
    return current not in (None, "") and str(current) != plan_checksum


def evaluate_package_readiness(
    plan: ExtractionPlan,
    *,
    current_bundles: dict[str, dict],
    cache_path_resolver: Callable[[str], Any] | None,
) -> PackageReadiness:
    """Check every closure member: cache file present and checksum unchanged.

    A bundle whose metadata checksum moved since the plan was built is
    "changed" (the plan is stale and the bundle must be redownloaded before
    the package is ready). A bundle without a resolvable, existing cache file
    is "missing". Either condition keeps the package not-ready — there is no
    partial-success state; the next run redownloads exactly those bundles.
    """

    missing: list[str] = []
    changed: list[str] = []
    diagnostics: list[PlanDiagnostic] = []
    for ref in plan.bundles:
        record = current_bundles.get(ref.bundle_name)
        if record is None:
            missing.append(ref.bundle_name)
            diagnostics.append(
                PlanDiagnostic(
                    code=DIAG_CACHE_MISSING,
                    severity="error",
                    phase=_DIAG_PHASE_READINESS,
                    message=f"closure bundle absent from current metadata: {ref.bundle_name}",
                    bundle_name=ref.bundle_name,
                )
            )
            continue
        if bundle_record_changed(ref.checksum, record):
            changed.append(ref.bundle_name)
            diagnostics.append(
                PlanDiagnostic(
                    code=DIAG_BUNDLE_DOWNLOAD_FAILED,
                    severity="error",
                    phase=_DIAG_PHASE_READINESS,
                    message=f"closure bundle changed since planning; redownload required: {ref.bundle_name}",
                    bundle_name=ref.bundle_name,
                )
            )
            continue
        if cache_path_resolver is not None:
            cache_path = cache_path_resolver(ref.bundle_name)
            if cache_path is None or not os.path.exists(os.fspath(cache_path)):
                missing.append(ref.bundle_name)
                diagnostics.append(
                    PlanDiagnostic(
                        code=DIAG_CACHE_MISSING,
                        severity="error",
                        phase=_DIAG_PHASE_READINESS,
                        message=f"closure bundle missing from cache: {ref.bundle_name}",
                        bundle_name=ref.bundle_name,
                    )
                )
    return PackageReadiness(
        package_id=plan.package_id,
        collection_id=plan.collection_id(),
        ready=not missing and not changed,
        missing_bundles=tuple(sorted(missing)),
        changed_bundles=tuple(sorted(changed)),
        diagnostics=tuple(diagnostics),
    )


def download_failure_diagnostic(plan: ExtractionPlan, bundle_name: str) -> PlanDiagnostic:
    """Retryable diagnostic for a failed closure-bundle download (roadmap §9)."""

    return PlanDiagnostic(
        code=DIAG_BUNDLE_DOWNLOAD_FAILED,
        severity="error",
        phase=_DIAG_PHASE_DOWNLOAD,
        message=f"closure bundle download failed; package {plan.package_id} stays unpublished: {bundle_name}",
        bundle_name=bundle_name,
    )


def _validate_associations(value: Any) -> Any:
    if not isinstance(value, dict) or value.get("association_version") != _ASSOCIATION_VERSION:
        raise ValueError("plan association document must carry association_version 1")
    if not isinstance(value.get("plans"), dict):
        raise ValueError("plan association document requires a plans object")
    return value


def _association_document(plans: tuple[ExtractionPlan, ...] | list[ExtractionPlan]) -> dict:
    document = {
        "association_version": _ASSOCIATION_VERSION,
        "plan_version": PLAN_VERSION,
        "plans": {
            plan.package_id: {
                "collection_id": plan.collection_id(),
                "purpose": plan.purpose,
                "profile": plan.profile,
                "bundle_names": list(plan.bundle_names()),
            }
            for plan in plans
        },
    }
    document["plans"] = dict(sorted(document["plans"].items()))
    return document


def persist_plan_associations(
    plans: tuple[ExtractionPlan, ...] | list[ExtractionPlan], path
) -> None:
    """Atomically persist plan-to-Bundle associations beside (not inside) cache metadata."""

    from updater.state import atomic_write_json

    atomic_write_json(path, _association_document(plans), _validate_associations)


def load_plan_associations(path) -> dict | None:
    """Load persisted plan-to-Bundle associations, or None when absent/corrupt."""

    import json
    from pathlib import Path as StdPath

    target = StdPath(os.fspath(path))
    if not target.exists():
        return None
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        return _validate_associations(document)
    except (ValueError, OSError):
        logger.warning("Ignoring unreadable plan association document: %s", target)
        return None


def association_bytes(plans: tuple[ExtractionPlan, ...] | list[ExtractionPlan]) -> bytes:
    """Canonical association document bytes (persisted form is identical)."""

    return canonical_json_bytes(_association_document(plans))
