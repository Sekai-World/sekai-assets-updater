"""Motion assembly: AnimationClip -> motion3.json documents."""

import logging
import math
from typing import Any, Dict, List, Tuple

from updater.live2d.curves import (
    build_binding_info_lookup,
    process_streamed_clip,
    read_curve_data,
    read_streamed_data,
)
from updater.unity_rs_adapter import UnityRsObject

logger = logging.getLogger("live2d")


def format_float(num):
    if isinstance(num, float) and int(num) == num:
        return int(num)
    elif isinstance(num, float):
        return float("{:.3f}".format(num))
    return num


def _motion_clip_ref(unity_object):
    """Return the clip reference from a motion entry.

    Supports both the PascalCase field names produced by the unity-rs typetree
    (``Clip``) and the snake_case form (``clip``) used by the manually built
    :class:`Live2DBuildMotion` fallback objects.
    """
    clip = getattr(unity_object, "Clip", None)
    if clip is None:
        clip = getattr(unity_object, "clip", None)
    return clip


def _motion_clip_asset_name(unity_object) -> str:
    """Return the clip asset name, tolerant of Pascal/snake case field names."""
    name = getattr(unity_object, "ClipAssetName", None)
    if name is None:
        name = getattr(unity_object, "clip_asset_name", None)
    return name if isinstance(name, str) else ""


def _load_animation_clip(unity_object):
    asset_name = _motion_clip_asset_name(unity_object)
    clip_ref = _motion_clip_ref(unity_object)
    if clip_ref is None:
        logger.warning("Motion entry %s has no clip reference", asset_name)
        return None

    # The clip reference is either a PPtr produced by the unity-rs adapter
    # (typetree-compatible dict with m_FileID/m_PathID) or, on the container
    # fallback path, the AnimationClip object itself.
    deref = getattr(clip_ref, "deref", None)
    if callable(deref):
        if getattr(clip_ref, "m_PathID", 0) == 0 or getattr(clip_ref, "m_FileID", 0) != 0:
            logger.warning(
                "Clip path id is empty or file id is not 0, reading %s for %s",
                clip_ref,
                asset_name,
            )
            return None
        target = deref()
        if not isinstance(target, UnityRsObject) or target is None:
            logger.warning("Failed to dereference clip for %s", asset_name)
            return None
        animation_clip = target.read()
    else:
        # Fallback: the container item is the AnimationClip object directly.
        if getattr(clip_ref, "type", None) is not None and clip_ref.type.name != "AnimationClip":
            logger.warning(
                "Container item %s is not an AnimationClip for %s",
                clip_ref,
                asset_name,
            )
            return None
        animation_clip = clip_ref.read()

    if animation_clip is None:
        return None
    return animation_clip


def _fill_motion_tracks(
    motion: Dict,
    animation_clip: Any,
    asset_name: str,
) -> None:
    streamed_frames = process_streamed_clip(
        animation_clip.m_MuscleClip.m_Clip.data.m_StreamedClip.data
    )
    clip_binding_constant = animation_clip.m_ClipBindingConstant
    if not clip_binding_constant:
        raise RuntimeError(f"Failed to read clip binding constant {asset_name}")
    binding_info_lookup = build_binding_info_lookup(clip_binding_constant.genericBindings)
    track_by_name: Dict[str, Dict] = {}

    for frame in streamed_frames:
        time = frame["time"]
        for curve_key in frame["keyList"]:
            read_streamed_data(motion, binding_info_lookup, track_by_name, time, curve_key)

    dense_clip = animation_clip.m_MuscleClip.m_Clip.data.m_DenseClip
    stream_count = animation_clip.m_MuscleClip.m_Clip.data.m_StreamedClip.curveCount
    for frame_idx in range(dense_clip.m_FrameCount):
        time = dense_clip.m_BeginTime + frame_idx / dense_clip.m_SampleRate
        for curve_idx in range(dense_clip.m_CurveCount):
            read_curve_data(
                motion,
                binding_info_lookup,
                track_by_name,
                stream_count + curve_idx,
                time,
                dense_clip.m_SampleArray,
                frame_idx * dense_clip.m_CurveCount + curve_idx,
            )

    constant_clip = animation_clip.m_MuscleClip.m_Clip.data.m_ConstantClip
    dense_count = dense_clip.m_CurveCount
    time = 0.0
    for _ in range(2):
        for curve_idx in range(len(constant_clip.data)):
            read_curve_data(
                motion,
                binding_info_lookup,
                track_by_name,
                stream_count + dense_count + curve_idx,
                time,
                constant_clip.data,
                curve_idx,
            )
        time = animation_clip.m_MuscleClip.m_StopTime

    for ev in animation_clip.m_Events:
        motion["Events"].append({"time": ev.time, "value": ev.data})


def _append_curve_segment(
    segments: List,
    curve: Dict,
    pre_curve: Dict,
    track_curves: List,
    index: int,
) -> Tuple[int, int]:
    if (
        index + 1 < len(track_curves)
        and abs(curve["time"] - pre_curve["time"] - 0.01) < 0.0001
        and math.isclose(track_curves[index + 1]["value"], curve["value"], abs_tol=0.0001)
    ):
        next_curve = track_curves[index + 1]
        segments.extend([3, format_float(next_curve["time"]), format_float(next_curve["value"])])
        return 1, 1

    if curve["inSlope"] == float("inf"):
        segments.extend([2, format_float(curve["time"]), format_float(curve["value"])])
        return 1, 1

    if math.isclose(pre_curve["outSlope"], 0.0, abs_tol=0.0001) and abs(curve["inSlope"]) < 0.0001:
        segments.extend([0, format_float(curve["time"]), format_float(curve["value"])])
        return 1, 1

    tangent_len = (curve["time"] - pre_curve["time"]) / 3.0
    segments.extend(
        [
            1,
            format_float(pre_curve["time"] + tangent_len),
            format_float(pre_curve["outSlope"] * tangent_len + pre_curve["value"]),
            format_float(curve["time"] - tangent_len),
            format_float(curve["value"] - curve["inSlope"] * tangent_len),
            format_float(curve["time"]),
            format_float(curve["value"]),
        ]
    )
    return 3, 1


def _build_motion3_curves(motion: Dict) -> Tuple[List, int, int]:
    curves = []
    total_segment_count = 0
    total_point_count = 0
    for _idx, track in enumerate(motion["TrackList"]):
        track_curves = track["Curve"]
        segments = [0, format_float(track_curves[0]["value"])]
        total_segment_count += 1
        total_point_count += 1
        for j in range(1, len(track_curves)):
            point_delta, segment_delta = _append_curve_segment(
                segments, track_curves[j], track_curves[j - 1], track_curves, j
            )
            total_point_count += point_delta
            total_segment_count += segment_delta
        curves.append(
            {
                "Target": track["Target"],
                "Id": track["Name"],
                "Segments": segments,
            }
        )
    return curves, total_segment_count, total_point_count


def _build_motion3_user_data(motion: Dict) -> Tuple[List, int]:
    user_data = []
    total_user_data_size = sum(len(ev["value"]) for ev in motion["Events"])
    for ev in motion["Events"]:
        user_data.append(
            {
                "Time": format_float(ev["time"]),
                "Value": ev["value"],
            }
        )
    return user_data, total_user_data_size


def _build_restored_motion3(motion: Dict, duration, sample_rate) -> Dict:
    curves, total_segment_count, total_point_count = _build_motion3_curves(motion)
    user_data, total_user_data_size = _build_motion3_user_data(motion)
    return {
        "Version": 3,
        "Meta": {
            "Duration": duration,
            "Fps": sample_rate,
            "Loop": True,
            "AreBeziersRestricted": True,
            "CurveCount": len(motion["TrackList"]),
            "UserDataCount": len(motion["Events"]),
            "TotalSegmentCount": total_segment_count,
            "TotalPointCount": total_point_count,
            "TotalUserDataSize": total_user_data_size,
        },
        "Curves": curves,
        "UserData": user_data,
    }


def restore_unity_object_to_motion3(unity_object) -> Tuple | None:
    """Restore unity game object to motion3 json format"""
    animation_clip = _load_animation_clip(unity_object)
    if animation_clip is None:
        return

    name = animation_clip.m_Name
    sample_rate = animation_clip.m_SampleRate
    duration = format_float(animation_clip.m_MuscleClip.m_StopTime)
    motion = {
        "Name": name,
        "SampleRate": sample_rate,
        "Duration": duration,
        "TrackList": [],
        "Events": [],
    }
    assert name == animation_clip.m_Name, f"Name mismatch {name} != {animation_clip.m_Name}"
    logger.debug("Restoring %s with sample rate %s and duration %s", name, sample_rate, duration)
    _fill_motion_tracks(motion, animation_clip, _motion_clip_asset_name(unity_object))
    return name, _build_restored_motion3(motion, duration, sample_rate)


def correct_param_ids(motions: List[Tuple[str, Dict]], param_id_map: Dict[str, str]):
    """Correct the parameter IDs in the motions"""
    for name, motion in motions:
        for curve in motion["Curves"]:
            try:
                num_id = curve["Id"]
                curve["Id"] = param_id_map[num_id]
            except KeyError:
                logger.warning("unable to find key %s in file %s", curve["Id"], name)
