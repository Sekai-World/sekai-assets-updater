"""Structured GLB plan/extraction logging (GLB roadmap Phase 0).

Every line carries package ID, purpose, phase, and diagnostic code so plan
decisions are machine-filterable; free-form messages pass through the same
sanitizer as the rest of the pipeline so URLs and paths never leak into logs.
"""

from __future__ import annotations

import logging

from updater.extract.glb.contracts import ExtractionPlan, PlanDiagnostic
from updater.sanitize import sanitize_log_label

logger = logging.getLogger("asset_updater")


def log_plan_built(plan: ExtractionPlan) -> None:
    logger.info(
        "GLB | phase=plan | action=plan_built | package=%s | purpose=%s | profile=%s | roots=%d | bundles=%d | edges=%d | missing=%d | excluded=%d | diagnostics=%d | collection_id=%s",
        sanitize_log_label(plan.package_id),
        plan.purpose,
        sanitize_log_label(plan.profile),
        len(plan.roots),
        len(plan.bundles),
        len(plan.dependency_edges),
        len(plan.missing_dependencies),
        len(plan.excluded_dependencies),
        len(plan.diagnostics),
        plan.collection_id(),
    )


def log_plan_diagnostic(diagnostic: PlanDiagnostic, package_id: str, purpose: str) -> None:
    level = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}[
        diagnostic.severity
    ]
    logger.log(
        level,
        "GLB | phase=%s | code=%s | severity=%s | package=%s | purpose=%s | root=%s | bundle=%s | message=%s",
        sanitize_log_label(diagnostic.phase),
        diagnostic.code,
        diagnostic.severity,
        sanitize_log_label(package_id),
        purpose,
        sanitize_log_label(diagnostic.root_id) if diagnostic.root_id else "-",
        sanitize_log_label(diagnostic.bundle_name) if diagnostic.bundle_name else "-",
        sanitize_log_label(diagnostic.message),
    )


def log_plan_diagnostics(plan: ExtractionPlan) -> None:
    for diagnostic in plan.diagnostics:
        log_plan_diagnostic(diagnostic, plan.package_id, plan.purpose)
