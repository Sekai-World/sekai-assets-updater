#!/usr/bin/env python3
# playable.py: Parses timeline AssetBundles and exports .playable entries into time-ordered JSON.

import logging

import UnityPy
import UnityPy.config


# Track class names (tracks that contain m_Clips)
TRACK_CLASSES = {
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
    "GroupTrack",
    "TimelineAsset",
}

# Clip class names (clip assets referenced by m_Asset)
CLIP_CLASSES = {
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
    for pid, obj in all_objects.items():
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
    for pid, obj in all_objects.items():
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


def extract_motion_clip(
    clip_timing: dict, asset_data: dict, character_name: str
) -> dict:
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


def extract_lookat_clip(
    clip_timing: dict, asset_data: dict, character_name: str
) -> dict:
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


def extract_rotate_clip(
    clip_timing: dict, asset_data: dict, character_name: str
) -> dict:
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


def extract_spawn_clip(
    clip_timing: dict, asset_data: dict, character_name: str
) -> dict:
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


def extract_unspawn_clip(
    clip_timing: dict, _asset_data: dict, character_name: str
) -> dict:
    """Extract an unspawn (exit) clip."""
    return {
        "type": "unspawn",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "character": character_name,
    }


def extract_light_clip(
    clip_timing: dict, asset_data: dict, character_name: str
) -> dict:
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


def extract_comment_clip(
    clip_timing: dict, asset_data: dict, _character_name: str
) -> dict:
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


def extract_cheer_clip(
    clip_timing: dict, asset_data: dict, _character_name: str
) -> dict:
    """Extract a cheer (audience) clip."""
    return {
        "type": "cheer",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "aisacKey": asset_data.get("aisacKey", ""),
        "volume": asset_data.get("volume", 1.0),
    }


def extract_audience_clip(
    clip_timing: dict, asset_data: dict, _character_name: str
) -> dict:
    """Extract an audience animation clip."""
    return {
        "type": "audience",
        "start": clip_timing["m_Start"],
        "duration": clip_timing["m_Duration"],
        "end": clip_timing["m_Start"] + clip_timing["m_Duration"],
        "motionId": asset_data.get("motionId", 0),
    }


def extract_spotlight_clip(
    clip_timing: dict, asset_data: dict, _character_name: str
) -> dict:
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


def extract_stage_object_clip(
    clip_timing: dict, asset_data: dict, _character_name: str
) -> dict:
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


logger = logging.getLogger("utils.playable")


def extract_playable(env: UnityPy.load, container_path: str) -> dict:
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

    # Build a set of all path_ids reachable from the playable root. This scopes
    # extraction to only objects actually referenced by the selected .playable,
    # preventing cross-contamination when multiple playables exist in one bundle.
    def gather_referenced_pids(start_pid: int) -> set:
        visited = set()
        to_visit = [start_pid]

        while to_visit:
            pid = to_visit.pop()
            if pid in visited:
                continue
            visited.add(pid)
            obj = all_objects.get(pid)
            if not obj:
                continue
            data = obj.get("data")

            stack = [data]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    # direct path reference
                    path_ref = node.get("m_PathID")
                    if isinstance(path_ref, int) and path_ref not in visited:
                        to_visit.append(path_ref)

                    # clip asset references often live under m_Asset
                    m_asset = node.get("m_Asset")
                    if isinstance(m_asset, dict):
                        aid = m_asset.get("m_PathID")
                        if isinstance(aid, int) and aid not in visited:
                            to_visit.append(aid)

                    # enqueue nested containers
                    for v in node.values():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                elif isinstance(node, list):
                    for item in node:
                        if isinstance(item, (dict, list)):
                            stack.append(item)

        return visited

    referenced_pids = gather_referenced_pids(root_pid)
    # ensure root is included
    referenced_pids.add(root_pid)

    # Collect events for this playable (only from referenced objects)
    events = []
    track_counts = {}
    for pid in referenced_pids:
        obj = all_objects.get(pid)
        if not obj or obj["type"] != "MonoBehaviour":
            continue
        d = obj["data"]
        cls = get_class_name(d, script_map)
        if cls not in TRACK_EXTRACTORS:
            continue
        extractor, needs_character = TRACK_EXTRACTORS[cls]
        character_id = d.get("CharacterId", 0)
        track_name = d.get("m_Name", "")
        character_name = (
            char_map.get(character_id, track_name) if needs_character else ""
        )
        clips = d.get("m_Clips", [])
        if not clips:
            continue
        track_counts[cls] = track_counts.get(cls, 0) + len(clips)
        for clip in clips:
            asset_pid = clip.get("m_Asset", {}).get("m_PathID", 0)
            asset_data = data_by_pid.get(asset_pid, {})
            events.append(extractor(clip, asset_data, character_name))

    events.sort(key=lambda e: (e["start"], e.get("character", ""), e["type"]))

    # Timeline name lookup
    timeline_name = ""
    for pid, obj in all_objects.items():
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
    import os
    import sys
    import json
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
    UnityPy.config.FALLBACK_UNITY_VERSION = "2022.3.21f1"
    env = UnityPy.load(Path(input_file).read_bytes())

    playables = {
        path: obj for path, obj in env.container.items() if path.endswith(".playable")
    }

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

        # Collect events for this playable (scope by references from the playable root)
        def gather_referenced_pids(start_pid: int) -> set:
            visited = set()
            to_visit = [start_pid]

            while to_visit:
                pid = to_visit.pop()
                if pid in visited:
                    continue
                visited.add(pid)
                obj2 = all_objects.get(pid)
                if not obj2:
                    continue
                data2 = obj2.get("data")

                stack = [data2]
                while stack:
                    node = stack.pop()
                    if isinstance(node, dict):
                        path_ref = node.get("m_PathID")
                        if isinstance(path_ref, int) and path_ref not in visited:
                            to_visit.append(path_ref)
                        m_asset = node.get("m_Asset")
                        if isinstance(m_asset, dict):
                            aid = m_asset.get("m_PathID")
                            if isinstance(aid, int) and aid not in visited:
                                to_visit.append(aid)
                        for v in node.values():
                            if isinstance(v, (dict, list)):
                                stack.append(v)
                    elif isinstance(node, list):
                        for item in node:
                            if isinstance(item, (dict, list)):
                                stack.append(item)

            return visited

        referenced_pids = gather_referenced_pids(root_pid)
        referenced_pids.add(root_pid)

        events = []
        track_counts = {}
        for pid in referenced_pids:
            obj = all_objects.get(pid)
            if not obj or obj["type"] != "MonoBehaviour":
                continue
            d = obj["data"]
            cls = get_class_name(d, script_map)
            if cls not in TRACK_EXTRACTORS:
                continue
            extractor, needs_character = TRACK_EXTRACTORS[cls]
            character_id = d.get("CharacterId", 0)
            track_name = d.get("m_Name", "")
            character_name = (
                char_map.get(character_id, track_name) if needs_character else ""
            )
            clips = d.get("m_Clips", [])
            if not clips:
                continue
            track_counts[cls] = track_counts.get(cls, 0) + len(clips)
            for clip in clips:
                asset_pid = clip.get("m_Asset", {}).get("m_PathID", 0)
                asset_data = data_by_pid.get(asset_pid, {})
                events.append(extractor(clip, asset_data, character_name))

        events.sort(key=lambda e: (e["start"], e.get("character", ""), e["type"]))

        # Timeline name lookup
        timeline_name = ""
        for pid, obj in all_objects.items():
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
