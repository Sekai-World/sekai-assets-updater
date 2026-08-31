#!/usr/bin/env python3
# playable.py: Parses timeline AssetBundles and exports .playable entries into time-ordered JSON.

import logging
from typing import Any

from updater.unity_rs_adapter import load_bundle

# Master-of-Ceremony tracks used by virtual_live/mc. These are valid current
# content and are intentionally kept separate from streaming_live tracks.
MC_TRACK_CLASSES = {
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

# Master-of-Ceremony clip classes used by virtual_live/mc.
MC_CLIP_CLASSES = {
    "CharacterTalkClip",
    "CharacterMotionClip",
    "CharacterLookAtClip",
    "CharacterMoveClip",
    "CharacterRotateClip",
    "CharacterSpawnClip",
    "CommentClip",
    "LightClip",
    "CheerClip",
    "AudienceClip",
    "SEClip",
    "GlobalSpotLightClip",
    "StageObjectClip",
}

# Streaming Live uses ordinary Unity Timeline tracks plus custom tracks from
# Sekai.LivePerformance and Sekai.Timeline.Streaming. The fallback extractor
# below also accepts unknown tracks with m_Clips so new game-side track types do
# not silently produce an empty timeline.
STREAMING_LIVE_TRACK_CLASSES = {
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
}

STREAMING_LIVE_CLIP_CLASSES = {
    "AnimationPlayableAsset",
    "CharacterMonitorClip",
    "ColorClip",
    "ColorSequenceClip",
    "EffectClip",
    "IntensityClip",
    "IntensitySequenceClip",
    "MobAvatarColorClip",
    "MobAvatarMotionClip",
    "MobAvatarStampClip",
    "RenderAmbientLightClip",
    "RenderCharacterAmbientLightClip",
    "RenderCharacterRimLightClip",
    "RenderColorLUTClip",
    "RenderFlareLightClip",
    "RenderGlobalScreenFadeClip",
    "RenderGlobalSettingsClip",
    "RenderStagePointLightClip",
    "ScreenChangeEffectClip",
    "SekaiAtomClip",
    "SekaiManaBlackoutClip",
    "SekaiManaClip",
    "ShoutTimeClip",
    "StageSwitchClip",
}

# Kept as a public compatibility alias for callers that imported the old
# constants. MC tracks are not legacy; they belong to virtual_live/mc.
TRACK_CLASSES = (
    MC_TRACK_CLASSES
    | STREAMING_LIVE_TRACK_CLASSES
    | {
        "GroupTrack",
        "TimelineAsset",
    }
)

CLIP_CLASSES = MC_CLIP_CLASSES | STREAMING_LIVE_CLIP_CLASSES


def build_script_map(all_objects: dict) -> dict:
    """Build a mapping from MonoScript PathID to its class name and namespace."""
    script_map = {}
    for pid, obj in all_objects.items():
        if obj["type"] == "MonoScript":
            d = obj["data"]
            script_map[pid] = {
                "className": d.get("m_ClassName", ""),
                "namespace": d.get("m_Namespace", ""),
            }
    return script_map


def get_class_name(data: dict, script_map: dict) -> str:
    """Get the MonoScript class name referenced by the object's m_Script."""
    script_pid = data.get("m_Script", {}).get("m_PathID", 0)
    info = script_map.get(script_pid, {})
    return info.get("className", "unknown")


def build_character_map(all_objects: dict, script_map: dict) -> dict:
    """
    Build a CharacterId -> character name map using spawn tracks and talk tracks.
    Character names are derived from track names (e.g. "こはね_入場" -> "こはね").
    """
    # Collect GroupTrack names (not currently used but available)
    group_names = {}
    for pid, obj in all_objects.items():
        if obj["type"] == "MonoBehaviour":
            d = obj["data"]
            cls = get_class_name(d, script_map)
            if cls == "GroupTrack":
                group_names[pid] = d.get("m_Name", "")

    # Extract from spawn tracks
    char_id_map = {}
    for _pid, obj in all_objects.items():
        if obj["type"] == "MonoBehaviour":
            d = obj["data"]
            cls = get_class_name(d, script_map)
            if cls == "MCTimelineCharacterSpawnTrack":
                cid = d.get("CharacterId", 0)
                name = d.get("m_Name", "")
                char_name = name.replace("_入場", "").strip()
                if cid and char_name:
                    char_id_map[cid] = char_name

    # Supplement using talk tracks
    for _pid, obj in all_objects.items():
        if obj["type"] == "MonoBehaviour":
            d = obj["data"]
            cls = get_class_name(d, script_map)
            if cls == "MCTimelineCharacterTalkTrack":
                cid = d.get("CharacterId", 0)
                name = d.get("m_Name", "")
                char_name = name.replace("_Talk", "").strip()
                if cid and char_name and cid not in char_id_map:
                    char_id_map[cid] = char_name

    return char_id_map


def extract_talk_clip(clip_timing: dict, asset_data: dict, character_name: str) -> dict:
    """Extract a talk clip event."""
    return {
        "type": "talk",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "character": character_name,
        "serif": asset_data.get("Serif", ""),
        "cueName": asset_data.get("CueName", ""),
        "displayName": clip_timing.get("m_DisplayName", ""),
    }


def extract_motion_clip(clip_timing: dict, asset_data: dict, character_name: str) -> dict:
    """Extract a motion/facial clip."""
    return {
        "type": "motion",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "character": character_name,
        "motionKey": asset_data.get("motionKey", ""),
        "facialKey": asset_data.get("facialKey", ""),
    }


def extract_lookat_clip(clip_timing: dict, asset_data: dict, character_name: str) -> dict:
    """Extract a look-at clip."""
    target_type_names = {0: "position", 1: "direction", 2: "character"}
    target_type = asset_data.get("targetType", 0)
    return {
        "type": "lookAt",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "character": character_name,
        "targetType": target_type_names.get(target_type, str(target_type)),
        "targetCharacterId": asset_data.get("targerCharacterId", 0),
        "isContinuousLookAt": bool(asset_data.get("isContinuousLookAt", 0)),
        "position": asset_data.get("position", {}),
    }


def extract_move_clip(clip_timing: dict, asset_data: dict, character_name: str) -> dict:
    """Extract a move clip."""
    return {
        "type": "move",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "character": character_name,
        "motionKey": asset_data.get("motionKey", ""),
        "speed": asset_data.get("speed", 0),
        "position": asset_data.get("position", {}),
        "targetPosition": asset_data.get("targetPosition", ""),
        "direction": asset_data.get("direction", ""),
    }


def extract_rotate_clip(clip_timing: dict, asset_data: dict, character_name: str) -> dict:
    """Extract a rotate clip."""
    return {
        "type": "rotate",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "character": character_name,
        "speed": asset_data.get("speed", 0),
        "direction": asset_data.get("direction", ""),
    }


def extract_spawn_clip(clip_timing: dict, asset_data: dict, character_name: str) -> dict:
    """Extract a spawn (enter) clip."""
    return {
        "type": "spawn",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "character": character_name,
        "character3dId": asset_data.get("Character3dId", 0),
        "motionKey": asset_data.get("motionKey", ""),
        "facialKey": asset_data.get("facialKey", ""),
        "position": asset_data.get("position", {}),
    }


def extract_unspawn_clip(clip_timing: dict, _asset_data: dict, character_name: str) -> dict:
    """Extract an unspawn (exit) clip."""
    return {
        "type": "unspawn",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "character": character_name,
    }


def extract_light_clip(clip_timing: dict, asset_data: dict, character_name: str) -> dict:
    """Extract a light clip."""
    target_type_names = {0: "global", 1: "character"}
    return {
        "type": "light",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "character": character_name,
        "targetType": target_type_names.get(asset_data.get("targetType", 0), "unknown"),
        "intensity": asset_data.get("intensity", 0),
        "characterId": asset_data.get("characterId", 0),
    }


def extract_comment_clip(clip_timing: dict, asset_data: dict, _character_name: str) -> dict:
    """Extract a director/comment clip."""
    return {
        "type": "comment",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "comment": asset_data.get("Comment", clip_timing.get("m_DisplayName", "")),
    }


def extract_se_clip(clip_timing: dict, asset_data: dict, _character_name: str) -> dict:
    """Extract an SE (sound effect) clip."""
    return {
        "type": "se",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "soundKey": asset_data.get("soundKey", ""),
    }


def extract_cheer_clip(clip_timing: dict, asset_data: dict, _character_name: str) -> dict:
    """Extract a cheer (audience) clip."""
    return {
        "type": "cheer",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "aisacKey": asset_data.get("aisacKey", ""),
        "volume": asset_data.get("volume", 1.0),
    }


def extract_audience_clip(clip_timing: dict, asset_data: dict, _character_name: str) -> dict:
    """Extract an audience animation clip."""
    return {
        "type": "audience",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "motionId": asset_data.get("motionId", 0),
    }


def extract_spotlight_clip(clip_timing: dict, asset_data: dict, _character_name: str) -> dict:
    """Extract a spotlight clip."""
    return {
        "type": "spotlight",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "centerPosition": asset_data.get("centerPosition", {}),
        "fadeStartRadius": asset_data.get("fadeStartRadius", 0),
        "fadeEndRadius": asset_data.get("fadeEndRadius", 0),
    }


def extract_stage_object_clip(clip_timing: dict, asset_data: dict, _character_name: str) -> dict:
    """Extract a stage object clip."""
    return {
        "type": "stageObject",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "stageObjectDataList": asset_data.get("StageObjectDataList", []),
    }


# Track class -> (extractor function, needs_character_name)
TRACK_EXTRACTORS = {
    "MCTimelineCharacterTalkTrack": (extract_talk_clip, True),
    "MCTimelineCharacterMotionTrack": (extract_motion_clip, True),
    "MCTimelineCharacterLookAtTrack": (extract_lookat_clip, True),
    "MCTimelineCharacterMoveTrack": (extract_move_clip, True),
    "MCTimelineCharacterRotateTrack": (extract_rotate_clip, True),
    "MCTimelineCharacterSpawnTrack": (extract_spawn_clip, True),
    "MCTimelineCharacterUnSpawnTrack": (extract_unspawn_clip, True),
    "MCTimelineLightTrack": (extract_light_clip, True),
    "MCTimelineCommentTrack": (extract_comment_clip, False),
    "MCTimelineSETrack": (extract_se_clip, False),
    "MCTimelineCheerTrack": (extract_cheer_clip, False),
    "MCTimelineAudienceTrack": (extract_audience_clip, False),
    "MCTimelineGlobalSpotLightTrack": (extract_spotlight_clip, False),
    "MCTimelineStageObjectTrack": (extract_stage_object_clip, False),
}


_UNITY_OBJECT_FIELDS = {"m_GameObject", "m_Enabled", "m_Script", "m_Name"}
_TIMELINE_TRACK_FIELDS = {
    "m_AnimClip",
    "m_Children",
    "m_Clips",
    "m_Curves",
    "m_CustomPlayableFullTypename",
    "m_Enabled",
    "m_GameObject",
    "m_Locked",
    "m_Markers",
    "m_Name",
    "m_Parent",
    "m_Script",
    "m_Version",
}


def _path_id(value: Any) -> int:
    """Return a local Unity PPtr path ID, or zero for a null reference."""
    if isinstance(value, dict):
        path_id = value.get("m_PathID", 0)
        if isinstance(path_id, int):
            return path_id
    return 0


def _without_fields(data: dict, fields: set[str]) -> dict:
    """Remove Unity serialization boilerplate while preserving custom fields."""
    return {key: value for key, value in data.items() if key not in fields}


def extract_streaming_live_clip(
    clip_timing: dict,
    asset_data: dict,
    *,
    track_class: str,
    track_name: str,
    track_path_id: int,
    track_data: dict,
    asset_class: str,
    asset_path_id: int,
) -> dict:
    """Extract a Streaming Live Timeline clip without losing custom data.

    Streaming Live clips are mostly custom PlayableAssets. Their behaviour is
    stored in fields such as ``template``, ``behaviour``, ``data`` or
    ``sequence`` rather than in one common schema, so retaining the asset
    typetree is more useful than guessing a small set of event fields.
    """
    start = clip_timing.get("m_Start", 0.0)
    duration = clip_timing.get("m_Duration", 0.0)
    if not isinstance(start, (int, float)):
        start = 0.0
    if not isinstance(duration, (int, float)):
        duration = 0.0

    event = {
        "type": "streamingLiveClip",
        "trackType": track_class,
        "trackName": track_name,
        "trackPathId": track_path_id,
        "clipType": asset_class,
        "assetPathId": asset_path_id,
        "start": start,
        "clipIn": clip_timing.get("m_ClipIn", 0.0),
        "duration": duration,
        "end": start + duration,
        "timeScale": clip_timing.get("m_TimeScale", 1.0),
        "displayName": clip_timing.get("m_DisplayName", ""),
        "clipData": _without_fields(clip_timing, {"m_Asset"}),
        "trackData": _without_fields(track_data, _TIMELINE_TRACK_FIELDS),
        "assetData": _without_fields(asset_data, _UNITY_OBJECT_FIELDS),
    }

    return event


def gather_referenced_pids(all_objects: dict, start_pid: int) -> set[int]:
    """Collect objects reachable from a TimelineAsset through Unity PPtrs."""
    visited: set[int] = set()
    to_visit = [start_pid]

    while to_visit:
        pid = to_visit.pop()
        if pid in visited:
            continue
        visited.add(pid)
        obj = all_objects.get(pid)
        if not obj:
            continue

        stack = [obj.get("data")]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                path_ref = node.get("m_PathID")
                if isinstance(path_ref, int) and path_ref not in visited:
                    to_visit.append(path_ref)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, (dict, list)):
                        stack.append(item)

    return visited


def _get_object_class(obj: dict | None, script_map: dict) -> str:
    if not obj:
        return "unknown"
    if obj["type"] == "MonoBehaviour":
        return get_class_name(obj["data"], script_map)
    return obj["type"]


def extract_timeline_events(
    all_objects: dict,
    script_map: dict,
    data_by_pid: dict,
    referenced_pids: set[int],
) -> tuple[list[dict], dict[str, int]]:
    """Extract both virtual_live/mc and Streaming Live Timeline clips."""
    events = []
    track_counts: dict[str, int] = {}
    char_map = build_character_map(all_objects, script_map)

    for pid in referenced_pids:
        obj = all_objects.get(pid)
        if not obj or obj["type"] != "MonoBehaviour":
            continue

        track_data = obj["data"]
        track_class = get_class_name(track_data, script_map)
        clips = track_data.get("m_Clips")
        if not isinstance(clips, list) or not clips:
            continue

        extractor_info = TRACK_EXTRACTORS.get(track_class)

        track_name = track_data.get("m_Name", "")
        character_id = track_data.get("CharacterId", 0)
        character_name = (
            char_map.get(character_id, track_name) if extractor_info and extractor_info[1] else ""
        )
        track_counts[track_class] = track_counts.get(track_class, 0) + len(clips)

        for clip in clips:
            asset_path_id = _path_id(clip.get("m_Asset"))
            asset_data = data_by_pid.get(asset_path_id, {})
            if not isinstance(asset_data, dict):
                asset_data = {}

            if extractor_info:
                extractor, _needs_character = extractor_info
                events.append(extractor(clip, asset_data, character_name))
            else:
                events.append(
                    extract_streaming_live_clip(
                        clip,
                        asset_data,
                        track_class=track_class,
                        track_name=track_name,
                        track_path_id=pid,
                        track_data=track_data,
                        asset_class=_get_object_class(all_objects.get(asset_path_id), script_map),
                        asset_path_id=asset_path_id,
                    )
                )

    events.sort(key=lambda event: (event["start"], event.get("trackName", ""), event["type"]))
    return events, track_counts


logger = logging.getLogger("live2d")


def extract_playable(env: Any, container_path: str) -> dict:
    """
    Parse an AssetBundle and extract the full timeline data.
    container_path: optional path of the playable container for metadata.
    """
    script_obj = None
    for path, obj in env.container.items():
        if path != container_path:
            continue
        script_obj = obj
        break

    if not script_obj:
        raise ValueError(f"No .playable entry found for {container_path}")

    # load all objects once
    all_objects = {}
    for obj in env.objects:
        data = obj.read_typetree()
        all_objects[obj.path_id] = {"type": obj.type.name, "data": data}
    logger.debug(f"Loaded {len(all_objects)} objects")

    script_map = build_script_map(all_objects)
    char_map = build_character_map(all_objects, script_map)
    logger.debug(f"Character map: {char_map}")
    data_by_pid = {pid: obj["data"] for pid, obj in all_objects.items()}

    # playable main file
    root_pid = script_obj.path_id
    logger.debug(f"Processing: {container_path}, root object path_id: {root_pid}")

    # Scope extraction to objects referenced by this .playable, preventing
    # cross-contamination when multiple playables exist in one bundle.
    referenced_pids = gather_referenced_pids(all_objects, root_pid)
    # ensure root is included
    referenced_pids.add(root_pid)

    events, track_counts = extract_timeline_events(
        all_objects, script_map, data_by_pid, referenced_pids
    )

    # Timeline name lookup
    timeline_name = ""
    for _pid, obj in all_objects.items():
        if obj["type"] == "MonoBehaviour":
            d = obj["data"]
            if get_class_name(d, script_map) == "TimelineAsset":
                timeline_name = d.get("m_Name", "")
                break

    # Character list
    characters = []
    for cid, cname in sorted(char_map.items()):
        characters.append({"characterId": cid, "character3dId": cid, "name": cname})

    result = script_obj.read_typetree()
    result.update(
        {
            "__timelineParse": {
                "version": 1,
                "meta": {
                    "timelineName": timeline_name,
                    "containerPath": container_path,
                    "totalEvents": len(events),
                    "characters": characters,
                    "trackEventCounts": track_counts,
                },
                "events": events,
            }
        }
    )

    logger.debug(f"Extracted {len(events)} events")
    return result


# CLI script
if __name__ == "__main__":
    import json
    import os
    import sys
    from pathlib import Path

    if len(sys.argv) < 2:
        print("Usage: python playable.py <input_file> [output_dir]")
        print("  input_file: Unity AssetBundle file path (contains .playable)")
        print("  output_dir: output directory (default: current directory)")
        sys.exit(1)

    input_file = sys.argv[1]
    if not os.path.exists(input_file):
        print(f"[!] File not found: {input_file}")
        sys.exit(1)

    output_dir = sys.argv[2] if len(sys.argv) >= 3 else "."
    os.makedirs(output_dir, exist_ok=True)

    # Step 1: scan container for .playable entries
    print(f"[*] Scanning container: {input_file}")
    env = load_bundle(Path(input_file), "2022.3.21f1")

    playables = {path: obj for path, obj in env.container.items() if path.endswith(".playable")}

    if not playables:
        print("[!] No .playable entries found in container")
        sys.exit(1)

    print(f"[*] Found {len(playables)} .playable entries:")
    for p in playables:
        print(f"    {p}")

    # Step 2: load all objects once (same SerializedFile)
    all_objects = {}
    for obj in env.objects:
        data = obj.read_typetree()
        all_objects[obj.path_id] = {"type": obj.type.name, "data": data}
    print(f"[*] Loaded {len(all_objects)} objects")

    script_map = build_script_map(all_objects)
    char_map = build_character_map(all_objects, script_map)
    print(f"[*] Character map: {char_map}")
    data_by_pid = {pid: obj["data"] for pid, obj in all_objects.items()}

    # Step 3: export each playable separately
    for container_path, script_obj in playables.items():
        root_pid = script_obj.path_id
        playable_filename = os.path.basename(container_path)
        output_file = os.path.join(output_dir, playable_filename)

        print(f"\n[>] Processing: {container_path}")
        print(f"    root object path_id: {root_pid}")

        referenced_pids = gather_referenced_pids(all_objects, root_pid)
        referenced_pids.add(root_pid)

        events, track_counts = extract_timeline_events(
            all_objects, script_map, data_by_pid, referenced_pids
        )

        # Timeline name lookup
        timeline_name = ""
        for _pid, obj in all_objects.items():
            if obj["type"] == "MonoBehaviour":
                d = obj["data"]
                if get_class_name(d, script_map) == "TimelineAsset":
                    timeline_name = d.get("m_Name", "")
                    break

        # Character list
        characters = []
        for cid, cname in sorted(char_map.items()):
            characters.append({"characterId": cid, "character3dId": cid, "name": cname})

        result = script_obj.read_typetree()
        result.update(
            {
                "__timelineParse": {
                    "version": 1,
                    "meta": {
                        "timelineName": timeline_name,
                        "containerPath": container_path,
                        "totalEvents": len(events),
                        "characters": characters,
                        "trackEventCounts": track_counts,
                    },
                    "events": events,
                }
            }
        )

        print(f"[*] Extracted {len(events)} events")
        for cls, count in sorted(track_counts.items()):
            print(f"    {cls}: {count} clips")

        # Save full timeline as JSON (use container file name)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False, default=str)
        print(f"[+] Saved full timeline: {output_file}")
