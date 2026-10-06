"""Deterministic Live2D association selections derived from bundle metadata.

Automatic discovery is deliberately narrower than the general ``live2d/``
namespace.  It selects only model and motion bundles from the current asset
metadata, then reuses the existing explicit selection and index-builder
contracts.  Model output directories come from every matching ``paths`` entry;
motion output directories use the unique versioned entry when metadata also
contains the bundle-root alias, otherwise preserving the first-entry behavior.
Restored motion bundles use the path layout already produced by
``restore_live2d_motions``::

    <output_root>/motion/<metadata-path-suffix>/BuildMotionData.json
    <output_root>/motion/<metadata-path-suffix>/motion/*.motion3.json
    <output_root>/motion/<metadata-path-suffix>/facial/*.motion3.json

Bundle names and metadata path suffixes are normalized and checked before they
are used.  Model metadata paths are reduced to shallow roots before extracted
outputs exist; each materialized root is then expanded to its model3 files.
Selection IDs are derived from the normalized metadata path, rather than using
an arbitrary metadata key or an absolute filesystem path.
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from updater.live2d.index_adapter import _discover_model3_paths
from updater.live2d.index_builder import (
    ModelOutputSelection,
    SharedMotionSetSelection,
)
from updater.live2d.master_data import (
    DEFAULT_MASTER_DATA_BRANCH,
    LocalMasterDataProvider,
    MasterDataProvider,
    OnlineMasterDataProvider,
    default_online_master_db_version,
)

MODEL_BUNDLE_PREFIX = "live2d/model/"
MOTION_BUNDLE_PREFIX = "live2d/motion/"
DEFAULT_AUTOMATIC_MASTER_DB_VERSION = "local"
PathInput: TypeAlias = str | os.PathLike[str]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")
_IDENTIFIER_CHAR_RE = re.compile(r"[A-Za-z0-9_.+-]")
_LIVE2D_METADATA_ROOTS = ("StartApp/live2d/", "assets/live2d/")

__all__ = [
    "DEFAULT_AUTOMATIC_MASTER_DB_VERSION",
    "Live2DAutomaticSelections",
    "Live2DAutomaticSelectionsError",
    "MODEL_BUNDLE_PREFIX",
    "MOTION_BUNDLE_PREFIX",
    "build_automatic_live2d_associated_selections",
    "build_live2d_automatic_associated_selections",
    "build_live2d_automatic_selections",
    "expand_automatic_live2d_model_selections",
]


class Live2DAutomaticSelectionsError(ValueError):
    """Raised when current metadata cannot produce safe automatic selections."""


@dataclass(frozen=True, slots=True)
class Live2DAutomaticSelections:
    """Inputs for the existing Live2D index builder."""

    provider: MasterDataProvider
    model_outputs: tuple[ModelOutputSelection, ...]
    motion_sets: tuple[SharedMotionSetSelection, ...]


@dataclass(frozen=True, slots=True)
class _BundleSelection:
    bundle: Mapping[str, object]
    bundle_name: str
    relative_name: str
    metadata_key: object


def _master_data_path(value: PathInput | None) -> Path:
    if value is None:
        raise Live2DAutomaticSelectionsError(
            "automatic Live2D association generation needs "
            "LIVE2D_ASSOCIATION_MASTER_DATA_DIR or "
            "LIVE2D_ASSOCIATION_MASTER_DATA_URL containing the six Live2D master-data "
            "JSON tables; configure one or provide an explicit validated association "
            "index or association-selection manifest"
        )
    try:
        raw = os.fspath(value)
    except (TypeError, ValueError) as exc:
        raise Live2DAutomaticSelectionsError(
            "LIVE2D_ASSOCIATION_MASTER_DATA_DIR must be a filesystem path"
        ) from exc
    if not isinstance(raw, str) or not raw.strip():
        raise Live2DAutomaticSelectionsError(
            "automatic Live2D association generation needs "
            "LIVE2D_ASSOCIATION_MASTER_DATA_DIR or "
            "LIVE2D_ASSOCIATION_MASTER_DATA_URL containing the six Live2D master-data "
            "JSON tables; configure one or provide an explicit validated association "
            "index or association-selection manifest"
        )
    if "\x00" in raw:
        raise Live2DAutomaticSelectionsError(
            "LIVE2D_ASSOCIATION_MASTER_DATA_DIR contains a NUL byte"
        )
    return Path(raw)


def _master_db_version(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or not _IDENTIFIER_RE.fullmatch(value):
        raise Live2DAutomaticSelectionsError(
            "automatic Live2D association master_db_version must be a stable identifier token"
        )
    return value


def _has_unsafe_path_characters(relative_name: str) -> bool:
    return bool(
        "\x00" in relative_name
        or "\\" in relative_name
        or ":" in relative_name
        or relative_name.startswith("/")
        or ntpath.isabs(relative_name)
        or ntpath.splitdrive(relative_name)[0]
    )


def _has_unsafe_path_components(relative_name: str) -> bool:
    components = relative_name.split("/")
    if any(not component or component in {".", ".."} for component in components):
        return True
    return any(
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in component)
        for component in components
    )


def _safe_bundle_suffix(bundle_name: str, prefix: str) -> str:
    relative_name = bundle_name[len(prefix) :]
    if not relative_name:
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found an empty bundle path: {bundle_name!r}"
        )
    if _has_unsafe_path_characters(relative_name) or _has_unsafe_path_components(relative_name):
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found an unsafe bundle path: {bundle_name!r}"
        )
    return relative_name


def _matching_metadata_relative_name(
    path: object,
    prefixes: tuple[str, ...],
    *,
    bundle_name: str,
    kind: str,
) -> str | None:
    """Return the safe suffix of the first metadata path prefix that matches."""
    if not isinstance(path, str):
        return None
    for prefix in prefixes:
        if not path.startswith(prefix):
            continue
        relative_name = path[len(prefix) :]
        if not relative_name:
            raise Live2DAutomaticSelectionsError(
                f"automatic Live2D discovery found an empty metadata path for "
                f"{kind} bundle {bundle_name!r}"
            )
        if _has_unsafe_path_characters(relative_name) or _has_unsafe_path_components(relative_name):
            raise Live2DAutomaticSelectionsError(
                f"automatic Live2D discovery found an unsafe metadata path for "
                f"{kind} bundle {bundle_name!r}: {path!r}"
            )
        return relative_name
    return None


def _metadata_relative_names(
    bundle: Mapping[str, object],
    *,
    bundle_name: str,
    kind: str,
    all_matching: bool,
) -> tuple[str, ...]:
    paths = bundle.get("paths")
    if not isinstance(paths, (list, tuple)):
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found missing metadata paths for {kind} bundle "
            f"{bundle_name!r}"
        )
    prefixes = tuple(f"{root}{kind}/" for root in _LIVE2D_METADATA_ROOTS)
    relative_names: list[str] = []
    for path in paths:
        relative_name = _matching_metadata_relative_name(
            path,
            prefixes,
            bundle_name=bundle_name,
            kind=kind,
        )
        if relative_name is None:
            continue
        relative_names.append(relative_name)
        if not all_matching:
            return (relative_names[-1],)

    if relative_names:
        return tuple(relative_names)

    raise Live2DAutomaticSelectionsError(
        f"automatic Live2D discovery found no matching metadata path for {kind} bundle "
        f"{bundle_name!r}"
    )


def _metadata_relative_name(
    bundle: Mapping[str, object],
    *,
    bundle_name: str,
    kind: str,
) -> str:
    """Return the safe metadata path selected for one automatic bundle."""

    relative_names = _metadata_relative_names(
        bundle,
        bundle_name=bundle_name,
        kind=kind,
        all_matching=kind == "motion",
    )
    if kind != "motion":
        return relative_names[0]

    # A motion Bundle can be listed once at its root and once at the versioned
    # path used by BuildMotionData.  The root alias is not where restoration
    # materializes that real-data bundle.  Prefer the one non-root candidate,
    # but fail closed if metadata offers more than one such candidate instead
    # of silently choosing an arbitrary path.
    root_alias = bundle_name.removeprefix(MOTION_BUNDLE_PREFIX)
    root_candidates = tuple(
        name for name in relative_names if name.rpartition("/")[2] == root_alias
    )
    if not root_candidates:
        return relative_names[0]
    versioned_names = tuple(name for name in root_candidates if name != root_alias)
    if len(versioned_names) > 1:
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found ambiguous versioned metadata paths for "
            f"motion bundle {bundle_name!r}: {', '.join(versioned_names)}"
        )
    return versioned_names[0] if versioned_names else root_alias


def _reduce_shallow_model_paths(relative_names: tuple[str, ...]) -> tuple[str, ...]:
    """Keep deterministic shallow roots while suppressing descendants."""

    ordered = sorted(set(relative_names), key=lambda value: (value.casefold(), value))
    roots: list[tuple[str, tuple[str, ...]]] = []
    seen_casefold: set[str] = set()
    for relative_name in ordered:
        casefolded_name = relative_name.casefold()
        if casefolded_name in seen_casefold:
            continue
        seen_casefold.add(casefolded_name)
        parts = tuple(component.casefold() for component in relative_name.split("/"))
        if any(parts[: len(root_parts)] == root_parts for _root, root_parts in roots):
            continue
        roots.append((relative_name, parts))
    return tuple(root for root, _parts in roots)


def _selection_id(kind: str, relative_name: str) -> str:
    encoded: list[str] = []
    for character in relative_name:
        if character == "/":
            encoded.append("-")
        elif _IDENTIFIER_CHAR_RE.fullmatch(character):
            encoded.append(character)
        else:
            encoded.append(f"-u{ord(character):x}-")

    candidate = f"{kind}-{''.join(encoded)}"
    if len(candidate) > 256:
        digest = hashlib.sha256(relative_name.encode("utf-8", "surrogatepass")).hexdigest()
        candidate = f"{kind}-{digest[:48]}"
    if not _IDENTIFIER_RE.fullmatch(candidate):  # pragma: no cover - guarded by construction
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery could not derive a safe {kind} selection ID"
        )
    return candidate


def _candidate_bundles(
    live2d_bundles: Mapping[str, object],
    prefix: str,
) -> list[tuple[object, Mapping[str, object], str]]:
    """Collect the named, safe bundles under one automatic discovery prefix."""
    candidates: list[tuple[object, Mapping[str, object], str]] = []
    for metadata_key, bundle in live2d_bundles.items():
        if not isinstance(bundle, Mapping):
            continue
        bundle_name = bundle.get("bundleName")
        if not isinstance(bundle_name, str) or not bundle_name.startswith(prefix):
            continue
        _safe_bundle_suffix(bundle_name, prefix)
        candidates.append((metadata_key, bundle, bundle_name))
    return candidates


def _bundle_relative_names(
    bundle: Mapping[str, object],
    *,
    bundle_name: str,
    kind: str,
    all_matching: bool,
) -> tuple[str, ...]:
    if not all_matching:
        return (
            _metadata_relative_name(
                bundle,
                bundle_name=bundle_name,
                kind=kind,
            ),
        )
    relative_names = _metadata_relative_names(
        bundle,
        bundle_name=bundle_name,
        kind=kind,
        all_matching=True,
    )
    if kind == "model":
        return _reduce_shallow_model_paths(relative_names)
    return relative_names


def _reject_duplicate_bundle_name(
    seen_names: dict[str, _BundleSelection],
    kind: str,
    bundle_name: str,
    metadata_key: object,
) -> None:
    previous = seen_names.get(bundle_name)
    if previous is not None:
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found duplicate {kind} bundleName "
            f"{bundle_name!r} (metadata keys {previous.metadata_key!r} and "
            f"{metadata_key!r})"
        )


def _register_selection_keys(
    kind: str,
    selection: _BundleSelection,
    seen_paths: dict[str, _BundleSelection],
    seen_ids: dict[str, _BundleSelection],
) -> None:
    path_key = selection.relative_name.casefold()
    previous = seen_paths.get(path_key)
    if previous is not None:
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found colliding {kind} output paths "
            f"{previous.relative_name!r} and {selection.relative_name!r}"
        )
    seen_paths[path_key] = selection

    selection_id = _selection_id(kind, selection.relative_name)
    id_key = selection_id.casefold()
    previous = seen_ids.get(id_key)
    if previous is not None:
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found colliding {kind} selection IDs "
            f"{_selection_id(kind, previous.relative_name)!r} and {selection_id!r}"
        )
    seen_ids[id_key] = selection


def _discover_bundles(
    live2d_bundles: Mapping[str, object],
    *,
    prefix: str,
    kind: str,
    all_matching_metadata_paths: bool = False,
) -> tuple[_BundleSelection, ...]:
    if not isinstance(live2d_bundles, Mapping):
        raise Live2DAutomaticSelectionsError(
            "automatic Live2D association generation requires current live2d_bundles metadata"
        )

    discovered_metadata = _candidate_bundles(live2d_bundles, prefix)
    discovered_metadata.sort(key=lambda item: item[2])
    seen_names: dict[str, _BundleSelection] = {}
    seen_paths: dict[str, _BundleSelection] = {}
    seen_ids: dict[str, _BundleSelection] = {}
    discovered: list[_BundleSelection] = []
    for metadata_key, bundle, bundle_name in discovered_metadata:
        _reject_duplicate_bundle_name(seen_names, kind, bundle_name, metadata_key)
        relative_names = _bundle_relative_names(
            bundle,
            bundle_name=bundle_name,
            kind=kind,
            all_matching=all_matching_metadata_paths,
        )
        selections = tuple(
            _BundleSelection(
                bundle=bundle,
                bundle_name=bundle_name,
                relative_name=relative_name,
                metadata_key=metadata_key,
            )
            for relative_name in relative_names
        )
        seen_names[bundle_name] = selections[0]

        for selection in selections:
            _register_selection_keys(kind, selection, seen_paths, seen_ids)
        discovered.extend(selections)

    return tuple(discovered)


def _path_overlaps(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return left[: len(right)] == right or right[: len(left)] == left


def _reject_generated_output_collisions(
    model_bundles: tuple[_BundleSelection, ...],
    motion_bundles: tuple[_BundleSelection, ...],
) -> None:
    generated: list[tuple[str, str, tuple[str, ...]]] = []
    for selection in model_bundles:
        generated.append(
            (
                "model",
                selection.bundle_name,
                tuple(f"model/{selection.relative_name}".casefold().split("/")),
            )
        )
    for selection in motion_bundles:
        base = tuple(f"motion/{selection.relative_name}".casefold().split("/"))
        generated.extend(
            (
                "motion",
                selection.bundle_name,
                path,
            )
            for path in (base, (*base, "motion"), (*base, "facial"))
        )

    for index, (kind, bundle_name, path) in enumerate(generated):
        for _other_kind, other_bundle_name, other_path in generated[index + 1 :]:
            if not _path_overlaps(path, other_path):
                continue
            if kind == _other_kind == "motion" and bundle_name == other_bundle_name:
                continue
            raise Live2DAutomaticSelectionsError(
                f"automatic Live2D discovery found colliding {kind} output paths for "
                f"{bundle_name!r} and {other_bundle_name!r}"
            )


def build_automatic_live2d_associated_selections(
    live2d_bundles: Mapping[str, object],
    *,
    output_root: PathInput,
    master_data_root: PathInput | None = None,
    master_db_version: str = DEFAULT_AUTOMATIC_MASTER_DB_VERSION,
    master_data_url: str | None = None,
    master_data_branch: str = DEFAULT_MASTER_DATA_BRANCH,
) -> Live2DAutomaticSelections:
    """Build deterministic selection objects from current bundle metadata.

    Only exact ``live2d/model/`` and ``live2d/motion/`` prefixes are selected.
    Other namespaces are ignored.  A configured local master-data directory
    takes precedence over the optional online branch archive.  Online data is
    downloaded only when the existing index builder loads its snapshot.
    """

    if master_data_root is not None:
        provider: MasterDataProvider = LocalMasterDataProvider(
            root=_master_data_path(master_data_root),
            master_db_version=_master_db_version(master_db_version),
        )
    elif master_data_url is not None:
        try:
            online_version = (
                default_online_master_db_version(master_data_branch)
                if master_db_version == DEFAULT_AUTOMATIC_MASTER_DB_VERSION
                else _master_db_version(master_db_version)
            )
            provider = OnlineMasterDataProvider(
                url=master_data_url,
                branch=master_data_branch,
                master_db_version=online_version,
            )
        except ValueError as exc:
            raise Live2DAutomaticSelectionsError(str(exc)) from exc
    else:
        _master_data_path(None)
        raise AssertionError("_master_data_path(None) must raise")  # pragma: no cover
    model_bundles = _discover_bundles(
        live2d_bundles,
        prefix=MODEL_BUNDLE_PREFIX,
        kind="model",
        all_matching_metadata_paths=True,
    )
    motion_bundles = _discover_bundles(
        live2d_bundles,
        prefix=MOTION_BUNDLE_PREFIX,
        kind="motion",
    )
    _reject_generated_output_collisions(model_bundles, motion_bundles)

    model_outputs = tuple(
        ModelOutputSelection(
            output_root=output_root,
            output_path=f"model/{selection.relative_name}",
            model_output_id=_selection_id("model", selection.relative_name),
            bundle=selection.bundle,
        )
        for selection in model_bundles
    )
    motion_sets = tuple(
        SharedMotionSetSelection(
            output_root=output_root,
            motion_bundle_output_path=f"motion/{selection.relative_name}",
            motion_output_path=f"motion/{selection.relative_name}/motion",
            facial_output_path=f"motion/{selection.relative_name}/facial",
            motion_set_id=_selection_id("motion", selection.relative_name),
            bundle=selection.bundle,
        )
        for selection in motion_bundles
    )
    return Live2DAutomaticSelections(
        provider=provider,
        model_outputs=model_outputs,
        motion_sets=motion_sets,
    )


def _resolved_model3_paths(
    selection: ModelOutputSelection,
) -> tuple[str, tuple[str, ...]]:
    """Return the on-disk output path and its model3 paths for one selection."""
    if selection.model3_path is not None:
        return selection.output_path, (selection.model3_path,)
    try:
        return _discover_model3_paths(selection.output_root, selection.output_path)
    except Exception as exc:
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery could not inspect model output "
            f"{selection.output_path!r}: {exc}"
        ) from exc


def _expanded_model_selections(
    selection: ModelOutputSelection,
    actual_output_path: str,
    model3_paths: tuple[str, ...],
    seen_ids: set[str],
) -> list[ModelOutputSelection]:
    root_name = actual_output_path.removeprefix("model/")
    expanded: list[ModelOutputSelection] = []
    for model3_path in model3_paths:
        model_output_id = _selection_id("model", f"{root_name}/{model3_path}")
        if model_output_id in seen_ids:
            raise Live2DAutomaticSelectionsError(
                f"automatic Live2D discovery found duplicate model selection ID {model_output_id!r}"
            )
        seen_ids.add(model_output_id)
        expanded.append(
            ModelOutputSelection(
                output_root=selection.output_root,
                output_path=actual_output_path,
                model_output_id=model_output_id,
                bundle=selection.bundle,
                model3_path=model3_path,
            )
        )
    return expanded


def expand_automatic_live2d_model_selections(
    selections: Live2DAutomaticSelections,
) -> Live2DAutomaticSelections:
    """Expand materialized automatic model roots into one selection per model3 file.

    Automatic metadata discovery intentionally runs before extracted outputs are
    guaranteed to exist.  Callers invoke this function after materialization;
    the shared adapter scanner then supplies safe, deterministic model3 paths.
    """

    if not isinstance(selections, Live2DAutomaticSelections):
        raise Live2DAutomaticSelectionsError("automatic selections must be validated selections")

    expanded: list[ModelOutputSelection] = []
    seen_ids: set[str] = set()
    for selection in selections.model_outputs:
        actual_output_path, model3_paths = _resolved_model3_paths(selection)
        if not model3_paths:
            raise Live2DAutomaticSelectionsError(
                f"automatic Live2D discovery found no model3 files under {selection.output_path!r}"
            )
        expanded.extend(
            _expanded_model_selections(selection, actual_output_path, model3_paths, seen_ids)
        )

    return Live2DAutomaticSelections(
        provider=selections.provider,
        model_outputs=tuple(expanded),
        motion_sets=selections.motion_sets,
    )


# Keep both likely caller vocabularies discoverable without duplicate logic.
build_live2d_automatic_selections = build_automatic_live2d_associated_selections
build_live2d_automatic_associated_selections = build_automatic_live2d_associated_selections
