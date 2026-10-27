"""Timeline resolution into addressable playable documents (#40, Phase 6).

A Streaming Live timeline is a Unity TimelineAsset MonoBehaviour stored in
container paths ending in ``.playable``.  The asset holds track PPtrs
(``m_Tracks``), group tracks nest further tracks under ``m_Children``, and
every track carries ``m_Clips`` whose ``m_Asset`` PPtrs point at the clip
PlayableAssets — custom MonoBehaviours whose behaviour lives in their own
typetree fields.

The document keeps stable object identities (bundle name, file index, path
ID) for every track, clip, and resolved reference instead of guessing from
names: track classes come from each MonoBehaviour's ``m_Script`` MonoScript
class name, and every PPtr in clip asset data resolves through the
collection identity table.  Known track classes (the catalog used by the
legacy playable extractor) produce resolved clip records; unknown track
classes are recorded verbatim with DIAG_TRACK_UNSUPPORTED so new game-side
tracks never silently disappear.  Resolved references that point at
AnimationClip objects are collected into ``animation_references`` so the
package can bind character/animation GLBs without duplication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from updater.extract.glb.collection import iter_pptr_locations, resolve_pptr
from updater.extract.glb.contracts import (
    DIAG_PLAYABLE_MISSING,
    DIAG_TRACK_UNSUPPORTED,
)
from updater.extract.glb.gltf_writer import content_hash
from updater.unity_rs_adapter import (
    CollectionFileTable,
    UnityRsEnvironment,
    UnityRsObject,
)

TIMELINE_ARTIFACT_VERSION = 1
TIMELINE_ARTIFACT_KIND = "timeline_playable"
TIMELINE_ARTIFACT_FIELDS = {
    "artifact_version",
    "kind",
    "package_id",
    "bundle_name",
    "file_index",
    "path_id",
    "container_path",
    "timeline_name",
    "duration_mode",
    "fixed_duration",
    "duration",
    "characters",
    "tracks",
    "animation_references",
    "diagnostics",
    "content_hash",
}

ANIMATION_CLIP_OBJECT_CLASS = 74

# Structural Timeline/track fields that carry no custom behaviour.
_TIMELINE_BOILERPLATE = {
    "m_GameObject",
    "m_Enabled",
    "m_Script",
    "m_Name",
    "m_Version",
    "m_FixedDuration",
    "m_EditorSettings",
    "m_DurationMode",
    "m_MarkerTrack",
}
_TRACK_BOILERPLATE = {
    "m_GameObject",
    "m_Enabled",
    "m_Script",
    "m_Name",
    "m_Version",
    "m_AnimClip",
    "m_Locked",
    "m_Muted",
    "m_CustomPlayableFullTypename",
    "m_Curves",
    "m_Parent",
    "m_Children",
    "m_Clips",
    "m_Markers",
}
_CLIP_ASSET_BOILERPLATE = {"m_GameObject", "m_Enabled", "m_Script", "m_Name"}
_CLIP_TIMING_FIELDS = {
    "m_Version",
    "m_ParentTrack",
    "m_EaseInDuration",
    "m_EaseOutDuration",
    "m_BlendInDuration",
    "m_BlendOutDuration",
    "m_MixInCurve",
    "m_MixOutCurve",
    "m_BlendInCurveMode",
    "m_BlendOutCurveMode",
    "m_ExposedParameterNames",
    "m_AnimationCurves",
    "m_Recordable",
    "m_PostExtrapolationMode",
    "m_PreExtrapolationMode",
    "m_PostExtrapolationTime",
    "m_PreExtrapolationTime",
}

# Track catalog: the classes the legacy playable extractor accepts, kept
# as the Phase 6 supported set.  GroupTrack is structural; TimelineAsset
# never appears as a track.  Unknown classes still round-trip, flagged.
STRUCTURAL_TRACK_CLASSES = frozenset({"GroupTrack"})
SUPPORTED_TRACK_CLASSES = frozenset(
    {
        "AnimationTrack",
        "CharacterMonitorTrack",
        "EffectTrack",
        "IntensityTrack",
        "MetaColorTrack",
        "MetaIntensityTrack",
        "MobAvatarColorTrack",
        "MobAvatarMotionTrack",
        "MobAvatarStampTrack",
        "RenderAmbientLightTrack",
        "RenderCharacterAmbientLightTrack",
        "RenderCharacterRimLightTrack",
        "RenderColorLUTTrack",
        "RenderFlareLightTrack",
        "RenderGlobalScreenFadeTrack",
        "RenderGlobalSettingsTrack",
        "RenderStagePointLightTrack",
        "ScreenChangeEffectTrack",
        "SekaiAtomTrack",
        "SekaiManaBlackoutTrack",
        "SekaiManaTrack",
        "ShoutTimeTrack",
        "StageSwitchTrack",
        "MCTimelineCharacterTalkTrack",
        "MCTimelineCharacterMotionTrack",
        "MCTimelineCharacterLookAtTrack",
        "MCTimelineCharacterMoveTrack",
        "MCTimelineCharacterRotateTrack",
        "MCTimelineCharacterSpawnTrack",
        "MCTimelineCharacterUnSpawnTrack",
        "MCTimelineLightTrack",
        "MCTimelineCheerTrack",
        "MCTimelineAudienceTrack",
        "MCTimelineSETrack",
        "MCTimelineCommentTrack",
        "MCTimelineStageObjectTrack",
        "MCTimelineGlobalSpotLightTrack",
    }
)


@dataclass(frozen=True, slots=True)
class TimelineDiagnostic:
    """One structured timeline resolution diagnostic."""

    code: str
    message: str
    track_path_id: int | None = None


def find_playable_roots(environment: UnityRsEnvironment) -> list[tuple[str, UnityRsObject]]:
    """Return every ``.playable`` container entry, ordered by path."""

    roots = [
        (container_path, target)
        for container_path, target in sorted(environment.container.items())
        if container_path.endswith(".playable")
    ]
    return roots


def _script_classes(environment: UnityRsEnvironment) -> dict[int, str]:
    """Map MonoScript path ID (per file, same-file scripts only) to class name."""

    classes: dict[int, str] = {}
    for obj in environment.objects:
        if obj.type.name != "MonoScript":
            continue
        try:
            data = obj.read_typetree()
        except Exception:  # NOSONAR - unreadable scripts just classify as unknown
            continue
        classes[obj.path_id] = str(data.get("m_ClassName", ""))
    return classes


def _object_class(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    source_file_index: int,
    typetree: Mapping[str, Any],
    script_classes: Mapping[int, str],
) -> str:
    script = typetree.get("m_Script") or {}
    file_id = int(script.get("m_FileID", 0))
    path_id = int(script.get("m_PathID", 0))
    if path_id:
        if file_id == 0:
            return script_classes.get(path_id, "unknown")
        resolution = resolve_pptr(environment, table, source_file_index, file_id, path_id)
        if resolution.object is not None:
            try:
                return str(resolution.object.read_typetree().get("m_ClassName", ""))
            except Exception:  # NOSONAR - same fallback
                return "unknown"
    return "unknown"


def _resolved_reference(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    source_file_index: int,
    location: Any,
) -> dict[str, Any]:
    """Resolve one PPtr location into a stable-identity record."""

    resolution = resolve_pptr(
        environment, table, source_file_index, location.file_id, location.path_id
    )
    record: dict[str, Any] = {
        "field_path": location.field_path,
        "file_id": location.file_id,
        "path_id": location.path_id,
    }
    if resolution.object is not None:
        record["resolved"] = True
        record["bundle_name"] = _bundle_for_file(table, resolution.object.file_index)
        record["target_file_index"] = resolution.object.file_index
        record["target_path_id"] = resolution.object.path_id
        record["target_class"] = resolution.object.type.name
    else:
        record["resolved"] = False
        if resolution.dependency_name:
            record["dependency_name"] = resolution.dependency_name
    return record


def _bundle_for_file(table: CollectionFileTable, file_index: int) -> str | None:
    for identity in table.identities:
        if identity.file_index == file_index:
            return identity.bundle_name
    return None


def _resolved_pptrs(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    source_file_index: int,
    data: Mapping[str, Any],
    excluded_fields: set[str],
) -> list[dict[str, Any]]:
    """Resolve every non-null PPtr in ``data`` outside the excluded fields."""

    scoped = {key: value for key, value in data.items() if key not in excluded_fields}
    records = []
    for location in iter_pptr_locations(scoped):
        if location.file_id == 0 and location.path_id == 0:
            continue
        records.append(_resolved_reference(environment, table, source_file_index, location))
    return records


def _clip_record(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    clip_timing: Mapping[str, Any],
    track: UnityRsObject,
    track_class: str,
    script_classes: Mapping[int, str],
) -> dict[str, Any]:
    source_file_index = track.file_index
    asset_ptr = clip_timing.get("m_Asset") or {}
    asset_file_id = int(asset_ptr.get("m_FileID", 0))
    asset_path_id = int(asset_ptr.get("m_PathID", 0))
    asset_record: dict[str, Any] = {
        "file_id": asset_file_id,
        "path_id": asset_path_id,
    }

    start = clip_timing.get("m_Start", 0.0)
    duration = clip_timing.get("m_Duration", 0.0)
    start = float(start) if isinstance(start, (int, float)) else 0.0
    duration = float(duration) if isinstance(duration, (int, float)) else 0.0

    record: dict[str, Any] = {
        "start": start,
        "duration": duration,
        "end": start + duration,
        "clip_in": float(clip_timing.get("m_ClipIn", 0.0) or 0.0),
        "time_scale": float(clip_timing.get("m_TimeScale", 1.0) or 1.0),
        "display_name": str(clip_timing.get("m_DisplayName", "")),
        "asset": asset_record,
        "asset_data": {},
        "resolved_references": [],
    }

    asset_object: UnityRsObject | None = None
    resolution = resolve_pptr(environment, table, source_file_index, asset_file_id, asset_path_id)
    if resolution.object is None:
        record["asset"]["resolved"] = False
        if resolution.dependency_name:
            record["asset"]["dependency_name"] = resolution.dependency_name
        return record

    asset_object = resolution.object
    record["asset"]["resolved"] = True
    record["asset"]["bundle_name"] = _bundle_for_file(table, asset_object.file_index)
    record["asset"]["target_file_index"] = asset_object.file_index
    record["asset"]["target_path_id"] = asset_object.path_id
    try:
        asset_data = asset_object.read_typetree()
    except Exception as exc:  # NOSONAR - unreadable clip assets stay recorded
        record["asset"]["target_class"] = "unknown"
        record["asset_data"] = {"_error": str(exc)}
        return record
    record["asset"]["target_class"] = _object_class(
        environment, table, asset_object.file_index, asset_data, script_classes
    )
    record["asset_data"] = {
        key: value for key, value in asset_data.items() if key not in _CLIP_ASSET_BOILERPLATE
    }
    record["resolved_references"] = _resolved_pptrs(
        environment,
        table,
        asset_object.file_index,
        asset_data,
        _CLIP_ASSET_BOILERPLATE,
    )
    return record


def _track_record(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    track_object: UnityRsObject,
    track_data: Mapping[str, Any],
    track_class: str,
    script_classes: Mapping[int, str],
) -> dict[str, Any]:
    supported = track_class in SUPPORTED_TRACK_CLASSES
    clips = [
        _clip_record(environment, table, clip, track_object, track_class, script_classes)
        for clip in track_data.get("m_Clips") or ()
        if isinstance(clip, Mapping)
    ]
    return {
        "track_class": track_class,
        "track_name": str(track_data.get("m_Name", "")),
        "file_index": track_object.file_index,
        "path_id": track_object.path_id,
        "supported": supported,
        "clips": clips,
    }


def build_timeline_document(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    package_id: str,
    container_path: str,
) -> dict[str, Any]:
    """Resolve one ``.playable`` root into a playable document."""

    root = environment.container.get(container_path)
    if root is None:
        raise ValueError(f"{DIAG_PLAYABLE_MISSING}: no container entry {container_path!r}")
    timeline_data = root.read_typetree()
    script_classes = _script_classes(environment)

    diagnostics: list[TimelineDiagnostic] = []
    tracks: list[dict[str, Any]] = []
    visited_tracks: set[tuple[int, int]] = set()
    active_tracks: set[tuple[int, int]] = set()
    track_refs = timeline_data.get("m_Tracks") or ()
    for ref in track_refs:
        _walk_track(
            environment,
            table,
            ref,
            root.file_index,
            script_classes,
            tracks,
            diagnostics,
            visited_tracks,
            active_tracks,
        )
    tracks.sort(key=lambda record: (record["file_index"], record["path_id"]))

    animation_references: dict[tuple[str, int, int], dict[str, Any]] = {}
    for track in tracks:
        for clip in track["clips"]:
            for reference in clip["resolved_references"]:
                if reference.get("target_class") != "AnimationClip":
                    continue
                key = (
                    reference.get("bundle_name") or "",
                    int(reference.get("target_file_index", 0)),
                    int(reference.get("target_path_id", 0)),
                )
                if key not in animation_references:
                    target = environment.object_by_identity(key[1], key[2])
                    animation_references[key] = {
                        "bundle_name": reference.get("bundle_name"),
                        "file_index": key[1],
                        "path_id": key[2],
                        "clip_name": str(getattr(target, "name", None) or ""),
                        "source_track_path_ids": [],
                    }
                source_list = animation_references[key]["source_track_path_ids"]
                if track["path_id"] not in source_list:
                    source_list.append(track["path_id"])

    characters: dict[int, dict[str, Any]] = {}
    for track in tracks:
        for clip in track["clips"]:
            asset_data = clip.get("asset_data") or {}
            character_id = asset_data.get("CharacterId")
            if isinstance(character_id, int) and character_id:
                characters.setdefault(
                    character_id,
                    {
                        "character_id": character_id,
                        "character3d_id": asset_data.get("Character3dId", character_id),
                        "source_track_path_ids": [],
                    },
                )
                source_list = characters[character_id]["source_track_path_ids"]
                if track["path_id"] not in source_list:
                    source_list.append(track["path_id"])

    duration = 0.0
    for track in tracks:
        for clip in track["clips"]:
            if clip["end"] > duration:
                duration = clip["end"]
    fixed_duration = timeline_data.get("m_FixedDuration")
    if not isinstance(fixed_duration, (int, float)):
        fixed_duration = None

    document: dict[str, Any] = {
        "artifact_version": TIMELINE_ARTIFACT_VERSION,
        "kind": TIMELINE_ARTIFACT_KIND,
        "package_id": package_id,
        "bundle_name": _bundle_for_file(table, root.file_index),
        "file_index": root.file_index,
        "path_id": root.path_id,
        "container_path": container_path,
        "timeline_name": str(timeline_data.get("m_Name", "")),
        "duration_mode": timeline_data.get("m_DurationMode"),
        "fixed_duration": float(fixed_duration) if fixed_duration is not None else None,
        "duration": duration,
        "characters": [characters[key] for key in sorted(characters)],
        "tracks": tracks,
        "animation_references": [animation_references[key] for key in sorted(animation_references)],
        "diagnostics": [],
    }
    document["diagnostics"] = [
        {
            "code": diagnostic.code,
            "message": diagnostic.message,
            "track_path_id": diagnostic.track_path_id,
        }
        for diagnostic in diagnostics
    ]
    document["content_hash"] = _document_hash(document)
    return document


def _walk_track(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    track_ref: Mapping[str, Any],
    source_file_index: int,
    script_classes: Mapping[int, str],
    tracks: list[dict[str, Any]],
    diagnostics: list[TimelineDiagnostic],
    visited_tracks: set[tuple[int, int]],
    active_tracks: set[tuple[int, int]],
) -> None:
    if not isinstance(track_ref, Mapping):
        diagnostics.append(
            TimelineDiagnostic(
                code=DIAG_TRACK_UNSUPPORTED,
                message=f"invalid track reference {track_ref!r}; skipped",
            )
        )
        return
    try:
        file_id = int(track_ref.get("m_FileID", 0))
        path_id = int(track_ref.get("m_PathID", 0))
    except (TypeError, ValueError):
        diagnostics.append(
            TimelineDiagnostic(
                code=DIAG_TRACK_UNSUPPORTED,
                message=f"invalid track reference {dict(track_ref)}; skipped",
            )
        )
        return
    resolution = resolve_pptr(
        environment,
        table,
        source_file_index,
        file_id,
        path_id,
    )
    if resolution.object is None:
        diagnostics.append(
            TimelineDiagnostic(
                code=DIAG_TRACK_UNSUPPORTED,
                message=f"track reference {dict(track_ref)} did not resolve",
                track_path_id=path_id,
            )
        )
        return
    track_object = resolution.object
    identity = (track_object.file_index, track_object.path_id)
    if identity in active_tracks:
        diagnostics.append(
            TimelineDiagnostic(
                code=DIAG_TRACK_UNSUPPORTED,
                message=f"track {track_object.path_id} forms a child cycle; skipped",
                track_path_id=track_object.path_id,
            )
        )
        return
    if identity in visited_tracks:
        return

    visited_tracks.add(identity)
    active_tracks.add(identity)
    try:
        track_data = track_object.read_typetree()
        track_class = _object_class(
            environment, table, track_object.file_index, track_data, script_classes
        )
        if track_class not in STRUCTURAL_TRACK_CLASSES:
            if track_class not in SUPPORTED_TRACK_CLASSES:
                diagnostics.append(
                    TimelineDiagnostic(
                        code=DIAG_TRACK_UNSUPPORTED,
                        message=(
                            f"track {track_object.path_id} ({track_data.get('m_Name', '')!r})"
                            f" uses unsupported track class {track_class!r};"
                            f" {len(track_data.get('m_Clips') or ())} clips recorded verbatim"
                        ),
                        track_path_id=track_object.path_id,
                    )
                )
            tracks.append(
                _track_record(
                    environment, table, track_object, track_data, track_class, script_classes
                )
            )

        for child_ref in track_data.get("m_Children") or ():
            _walk_track(
                environment,
                table,
                child_ref,
                track_object.file_index,
                script_classes,
                tracks,
                diagnostics,
                visited_tracks,
                active_tracks,
            )
    finally:
        active_tracks.remove(identity)


def _document_hash(document: Mapping[str, Any]) -> str:
    import orjson

    payload = {key: value for key, value in document.items() if key != "content_hash"}
    return content_hash(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS))


def validate_timeline_document(value: Any) -> dict[str, Any]:
    """Validate one persisted playable document."""

    if not isinstance(value, dict):
        raise ValueError("timeline document must be an object")
    if set(value) != TIMELINE_ARTIFACT_FIELDS:
        raise ValueError(
            f"timeline document fields must be exactly"
            f" {sorted(TIMELINE_ARTIFACT_FIELDS)}, got {sorted(value)}"
        )
    if value["artifact_version"] != TIMELINE_ARTIFACT_VERSION:
        raise ValueError("timeline artifact_version must be 1")
    if value["kind"] != TIMELINE_ARTIFACT_KIND:
        raise ValueError(f"timeline kind must be {TIMELINE_ARTIFACT_KIND!r}")
    if not isinstance(value["package_id"], str) or not value["package_id"]:
        raise ValueError("timeline package_id must be a non-empty string")
    if not isinstance(value["tracks"], list):
        raise ValueError("timeline tracks must be a list")
    if not isinstance(value["animation_references"], list):
        raise ValueError("timeline animation_references must be a list")
    if not isinstance(value["content_hash"], str) or not value["content_hash"]:
        raise ValueError("timeline content_hash must be a non-empty string")
    return value


def persist_timeline_document(path, document: dict[str, Any]) -> None:
    """Atomically persist one playable document."""

    from updater.state import atomic_write_json

    atomic_write_json(path, document, validate_timeline_document)


def load_timeline_document(path) -> dict[str, Any] | None:
    """Load one persisted playable document, or ``None`` when absent/corrupt."""

    import json
    import os
    from pathlib import Path as StdPath

    target = StdPath(os.fspath(path))
    if not target.exists():
        return None
    try:
        return validate_timeline_document(json.loads(target.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return None


def timeline_reference_problems(document: Mapping[str, Any]) -> list[str]:
    """Referential problems among the document's own records.

    Every animation reference must be a complete stable identity
    (bundle name, file index, path ID) with at least one source track, and
    track identities must be unique — structural checks the offline package
    validator can call without knowing the filesystem.
    """

    problems: list[str] = []
    track_ids = [
        (track.get("file_index"), track.get("path_id")) for track in document.get("tracks", ())
    ]
    duplicated = {identity for identity in track_ids if track_ids.count(identity) > 1}
    if duplicated:
        problems.append(f"duplicate track identities: {sorted(duplicated)}")
    for reference in document.get("animation_references", ()):
        if not reference.get("bundle_name"):
            problems.append(f"animation reference {reference.get('path_id')} has no bundle name")
        if reference.get("file_index") is None or reference.get("path_id") is None:
            problems.append(f"incomplete animation reference: {reference}")
        if not reference.get("source_track_path_ids"):
            problems.append(f"animation reference {reference.get('path_id')} has no source track")
    return problems
