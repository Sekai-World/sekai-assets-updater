"""Feature flags for Streaming Live GLB preprocessing (GLB roadmap Phase 0).

All flags default to off. Enabling a later GLB phase flag requires the
earlier ones (fail fast with a configuration error), and every GLB flag
requires the master ``ENABLE_STREAMING_LIVE_GLB_PREPROCESSING`` switch, so a
default configuration schedules no GLB work at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from updater.extract.glb.contracts import PlanValidationError

MASTER_FLAG = "ENABLE_STREAMING_LIVE_GLB_PREPROCESSING"

# Ordered prerequisite chain: each flag requires every flag before it.
_PHASED_FLAGS = (
    "ENABLE_EXTRACTION_PLANS",
    "ENABLE_MULTIBUNDLE_COLLECTIONS",
    "ENABLE_STATIC_GLB_EXPORT",
    "ENABLE_GLB_MATERIALS",
    "ENABLE_GLB_ANIMATIONS",
    "ENABLE_TIMELINE_MANIFEST",
)
# Diagnostics-only escape hatch; gated by the master flag but not phased.
INCOMPLETE_FLAG = "ALLOW_INCOMPLETE_EXTRACTION"

FLAG_NAMES = (MASTER_FLAG, *_PHASED_FLAGS, INCOMPLETE_FLAG)


@dataclass(frozen=True)
class GLBFlags:
    """Resolved GLB preprocessing switches; all-off means standard-only."""

    preprocessing: bool = False
    extraction_plans: bool = False
    multibundle_collections: bool = False
    static_glb_export: bool = False
    glb_materials: bool = False
    glb_animations: bool = False
    timeline_manifest: bool = False
    allow_incomplete: bool = False

    @property
    def any_enabled(self) -> bool:
        return any(
            (
                self.preprocessing,
                self.extraction_plans,
                self.multibundle_collections,
                self.static_glb_export,
                self.glb_materials,
                self.glb_animations,
                self.timeline_manifest,
                self.allow_incomplete,
            )
        )


def _enabled(config, name: str) -> bool:
    return bool(getattr(config, name, False))


def resolve_glb_flags(config) -> GLBFlags:
    """Read and cross-validate the GLB feature flags from a config object."""

    flags = GLBFlags(
        preprocessing=_enabled(config, MASTER_FLAG),
        extraction_plans=_enabled(config, "ENABLE_EXTRACTION_PLANS"),
        multibundle_collections=_enabled(config, "ENABLE_MULTIBUNDLE_COLLECTIONS"),
        static_glb_export=_enabled(config, "ENABLE_STATIC_GLB_EXPORT"),
        glb_materials=_enabled(config, "ENABLE_GLB_MATERIALS"),
        glb_animations=_enabled(config, "ENABLE_GLB_ANIMATIONS"),
        timeline_manifest=_enabled(config, "ENABLE_TIMELINE_MANIFEST"),
        allow_incomplete=_enabled(config, INCOMPLETE_FLAG),
    )

    phased = {
        "extraction_plans": flags.extraction_plans,
        "multibundle_collections": flags.multibundle_collections,
        "static_glb_export": flags.static_glb_export,
        "glb_materials": flags.glb_materials,
        "glb_animations": flags.glb_animations,
        "timeline_manifest": flags.timeline_manifest,
    }
    if not flags.preprocessing:
        enabled_later = [name for name, enabled in phased.items() if enabled]
        if enabled_later or flags.allow_incomplete:
            raise PlanValidationError(
                "flag_requires_master",
                f"{MASTER_FLAG} must be enabled before: {sorted(enabled_later + ([INCOMPLETE_FLAG] if flags.allow_incomplete else []))}",
            )
        return flags

    seen: list[str] = []
    for index, (name, enabled) in enumerate(phased.items()):
        if not enabled:
            continue
        flag_name = _PHASED_FLAGS[index]
        missing = [candidate for candidate in _PHASED_FLAGS[:index] if candidate not in seen]
        if missing:
            raise PlanValidationError(
                "flag_prerequisite_missing",
                f"enabling {name} requires its earlier GLB phases: {missing}",
            )
        seen.append(flag_name)
    return flags
