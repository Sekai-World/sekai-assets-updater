"""Explicit composition of selected Live2D outputs and master data.

This module is intentionally only a composition boundary.  Callers select the
artifacts, Bundle metadata, and durable record identifiers; the output adapter
observes those selections and the association builder constructs candidates.
No model-to-motion ownership is inferred here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from updater.live2d.association import build_live2d_index
from updater.live2d.contracts import Live2DIndex
from updater.live2d.index_adapter import (
    BundleMetadata,
    PathInput,
    build_model_output_record,
    build_shared_motion_set_record,
)
from updater.live2d.master_data import Live2DMasterDataSnapshot, MasterDataProvider

__all__ = [
    "BundleMetadata",
    "Live2DIndexBuilderError",
    "ModelOutputSelection",
    "PathInput",
    "SharedMotionSetSelection",
    "build_live2d_association_index",
]


class Live2DIndexBuilderError(ValueError):
    """Raised when composition inputs do not have the expected domain shape."""


@dataclass(frozen=True, slots=True)
class ModelOutputSelection:
    """One caller-selected model output and its source Bundle metadata."""

    output_root: PathInput
    output_path: str
    model_output_id: str
    bundle: BundleMetadata


@dataclass(frozen=True, slots=True)
class SharedMotionSetSelection:
    """One caller-selected shared motion/facial output pair."""

    output_root: PathInput
    motion_bundle_output_path: PathInput
    motion_output_path: str
    facial_output_path: str
    motion_set_id: str
    bundle: BundleMetadata


def _selection_items(value: object, field_name: str) -> tuple[object, ...]:
    if (
        isinstance(value, (str, bytes, bytearray))
        or isinstance(value, Mapping)
        or not isinstance(value, Sequence)
    ):
        raise Live2DIndexBuilderError(
            f"{field_name} must be a sequence of explicit selection objects"
        )
    return tuple(cast(Sequence[object], value))


def _model_selections(value: object) -> tuple[ModelOutputSelection, ...]:
    selections = _selection_items(value, "model_outputs")
    result: list[ModelOutputSelection] = []
    for index, selection in enumerate(selections):
        if not isinstance(selection, ModelOutputSelection):
            raise Live2DIndexBuilderError(f"model_outputs[{index}] must be a ModelOutputSelection")
        result.append(selection)
    return tuple(result)


def _motion_selections(value: object) -> tuple[SharedMotionSetSelection, ...]:
    selections = _selection_items(value, "motion_sets")
    result: list[SharedMotionSetSelection] = []
    for index, selection in enumerate(selections):
        if not isinstance(selection, SharedMotionSetSelection):
            raise Live2DIndexBuilderError(
                f"motion_sets[{index}] must be a SharedMotionSetSelection"
            )
        result.append(selection)
    return tuple(result)


def _load_snapshot(provider: MasterDataProvider) -> Live2DMasterDataSnapshot:
    load_snapshot = getattr(provider, "load_live2d_snapshot", None)
    if not callable(load_snapshot):
        raise Live2DIndexBuilderError("provider must expose a callable load_live2d_snapshot method")

    # Do not catch exceptions from the provider: file, JSON, and other source
    # errors retain their useful domain and traceback information.
    snapshot = cast(Callable[[], object], load_snapshot)()
    if not isinstance(snapshot, Live2DMasterDataSnapshot):
        raise Live2DIndexBuilderError(
            "provider.load_live2d_snapshot() must return a Live2DMasterDataSnapshot"
        )
    return snapshot


def build_live2d_association_index(
    *,
    provider: MasterDataProvider,
    metadata_version: str,
    model_outputs: Sequence[ModelOutputSelection],
    motion_sets: Sequence[SharedMotionSetSelection],
) -> Live2DIndex:
    """Build an index from explicit model and shared-motion selections.

    The input sequences are copied to tuples before any adapter work.  Record
    IDs, output paths, and Bundle metadata are passed to the existing adapter
    without normalization.  Uniqueness and cross-record integrity remain the
    responsibility of :func:`build_live2d_index` and its contracts.
    """

    selected_models = _model_selections(model_outputs)
    selected_motion_sets = _motion_selections(motion_sets)
    snapshot = _load_snapshot(provider)

    model_records = tuple(
        build_model_output_record(
            output_root=selection.output_root,
            output_path=selection.output_path,
            model_output_id=selection.model_output_id,
            bundle=selection.bundle,
            metadata_version=metadata_version,
        )
        for selection in selected_models
    )
    motion_records = tuple(
        build_shared_motion_set_record(
            output_root=selection.output_root,
            motion_bundle_output_path=selection.motion_bundle_output_path,
            motion_output_path=selection.motion_output_path,
            facial_output_path=selection.facial_output_path,
            motion_set_id=selection.motion_set_id,
            bundle=selection.bundle,
            metadata_version=metadata_version,
        )
        for selection in selected_motion_sets
    )
    return build_live2d_index(
        metadata_version=metadata_version,
        master_db_version=snapshot.master_db_version,
        model_outputs=model_records,
        motion_sets=motion_records,
        tables=snapshot.tables,
    )
