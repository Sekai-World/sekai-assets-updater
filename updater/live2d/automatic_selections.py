"""Deterministic Live2D association selections derived from bundle metadata.

Automatic discovery is deliberately narrower than the general ``live2d/``
namespace.  It selects only model and motion bundles from the current asset
metadata, then reuses the existing explicit selection and index-builder
contracts.  Model output directories come from every matching ``paths`` entry;
motion output directories use the first matching entry.  Restored motion bundles
use the path layout already produced by ``restore_live2d_motions``::

    <output_root>/motion/<metadata-path-suffix>/BuildMotionData.json
    <output_root>/motion/<metadata-path-suffix>/motion/*.motion3.json
    <output_root>/motion/<metadata-path-suffix>/facial/*.motion3.json

Bundle names and metadata path suffixes are normalized and checked before they
are used.  Selection IDs are derived from the normalized metadata path, rather
than using an arbitrary metadata key or an absolute filesystem path.
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


def _safe_bundle_suffix(bundle_name: str, prefix: str) -> str:
    relative_name = bundle_name[len(prefix) :]
    if not relative_name:
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found an empty bundle path: {bundle_name!r}"
        )
    if (
        "\x00" in relative_name
        or "\\" in relative_name
        or ":" in relative_name
        or relative_name.startswith("/")
        or ntpath.isabs(relative_name)
        or ntpath.splitdrive(relative_name)[0]
    ):
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found an unsafe bundle path: {bundle_name!r}"
        )

    components = relative_name.split("/")
    if any(not component or component in {".", ".."} for component in components):
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found an unsafe bundle path: {bundle_name!r}"
        )
    if any(
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in component)
        for component in components
    ):
        raise Live2DAutomaticSelectionsError(
            f"automatic Live2D discovery found an unsafe bundle path: {bundle_name!r}"
        )
    return "/".join(components)


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
        if not isinstance(path, str):
            continue
        for prefix in prefixes:
            if not path.startswith(prefix):
                continue
            relative_name = path[len(prefix) :]
            if not relative_name:
                raise Live2DAutomaticSelectionsError(
                    f"automatic Live2D discovery found an empty metadata path for "
                    f"{kind} bundle {bundle_name!r}"
                )
            if (
                "\x00" in relative_name
                or "\\" in relative_name
                or ":" in relative_name
                or relative_name.startswith("/")
                or ntpath.isabs(relative_name)
                or ntpath.splitdrive(relative_name)[0]
            ):
                raise Live2DAutomaticSelectionsError(
                    f"automatic Live2D discovery found an unsafe metadata path for "
                    f"{kind} bundle {bundle_name!r}: {path!r}"
                )
            components = relative_name.split("/")
            if any(not component or component in {".", ".."} for component in components):
                raise Live2DAutomaticSelectionsError(
                    f"automatic Live2D discovery found an unsafe metadata path for "
                    f"{kind} bundle {bundle_name!r}: {path!r}"
                )
            if any(
                any(ord(character) < 0x20 or ord(character) == 0x7F for character in component)
                for component in components
            ):
                raise Live2DAutomaticSelectionsError(
                    f"automatic Live2D discovery found an unsafe metadata path for "
                    f"{kind} bundle {bundle_name!r}: {path!r}"
                )
            relative_names.append("/".join(components))
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
    """Return the first matching metadata path, preserving motion behavior."""

    return _metadata_relative_names(
        bundle,
        bundle_name=bundle_name,
        kind=kind,
        all_matching=False,
    )[0]


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

    discovered_metadata: list[tuple[object, Mapping[str, object], str]] = []
    for metadata_key, bundle in live2d_bundles.items():
        if not isinstance(bundle, Mapping):
            continue
        bundle_name = bundle.get("bundleName")
        if not isinstance(bundle_name, str) or not bundle_name.startswith(prefix):
            continue
        _safe_bundle_suffix(bundle_name, prefix)
        discovered_metadata.append((metadata_key, bundle, bundle_name))

    discovered_metadata.sort(key=lambda item: item[2])
    seen_names: dict[str, _BundleSelection] = {}
    seen_paths: dict[str, _BundleSelection] = {}
    seen_ids: dict[str, _BundleSelection] = {}
    discovered: list[_BundleSelection] = []
    for metadata_key, bundle, bundle_name in discovered_metadata:
        previous = seen_names.get(bundle_name)
        if previous is not None:
            raise Live2DAutomaticSelectionsError(
                f"automatic Live2D discovery found duplicate {kind} bundleName "
                f"{bundle_name!r} (metadata keys {previous.metadata_key!r} and "
                f"{metadata_key!r})"
            )
        if all_matching_metadata_paths:
            relative_names = _metadata_relative_names(
                bundle,
                bundle_name=bundle_name,
                kind=kind,
                all_matching=True,
            )
        else:
            relative_names = (
                _metadata_relative_name(
                    bundle,
                    bundle_name=bundle_name,
                    kind=kind,
                ),
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


# Keep both likely caller vocabularies discoverable without duplicate logic.
build_live2d_automatic_selections = build_automatic_live2d_associated_selections
build_live2d_automatic_associated_selections = build_automatic_live2d_associated_selections
