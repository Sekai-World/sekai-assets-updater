import asyncio
import logging
import math
import os
import struct
from io import BytesIO
from pathlib import Path as StdPath
from pathlib import PurePosixPath
from typing import Any, Dict, List, Tuple
from zlib import crc32

import orjson as json
from anyio import Path, open_file

from updater.constants import UNITY_FS_CONTAINER_BASE
from updater.unity_rs_adapter import UnityRsObject, load_bundle

from .binary import BinaryStream

# Unity's stable class ID for Transform (previously UnityPy.enums.ClassIDType.Transform).
TRANSFORM_CLASS_ID = 4

logger = logging.getLogger("live2d")

live2d_target_map = {
    "CubismParameter": ("Parameter", None),
    "CubismPart": ("PartOpacity", None),
    "CubismRenderController": ("Model", "Opacity"),
    "CubismEyeBlinkController": ("Model", "EyeBlink"),
    "CubismMouthController": ("Model", "LipSync"),
}


def format_float(num):
    if isinstance(num, float) and int(num) == num:
        return int(num)
    elif isinstance(num, float):
        return float("{:.3f}".format(num))
    return num


class StreamedCurveKey(object):
    def __init__(self, bs):
        super().__init__()

        self.index: int = bs.readUInt32()
        self.coeff: List[float] = [bs.readFloat() for _ in range(3)]

        self.outSlope: float = self.coeff[2]
        self.value: float = bs.readFloat()
        self.inSlope: float = 0.0

    def __repr__(self) -> str:
        return str(
            {
                "index": self.index,
                "coeff": self.coeff,
                "inSlope": self.inSlope,
                "outSlope": self.outSlope,
                "value": self.value,
            }
        )

    def calc_next_in_slope(self, dx, rhs):
        if self.coeff[0] == 0 and self.coeff[1] == 0 and self.coeff[2] == 0:
            return float("Inf")

        dx = max(dx, 0.0001)
        dy = rhs.value - self.value
        length = 1.0 / (dx * dx)
        d1 = self.outSlope * dx
        d2 = dy + dy + dy - d1 - d1 - self.coeff[1] / length

        return d2 / dx


def find_binding(generic_bindings, index: int):
    curves = 0
    for b in generic_bindings:
        if b.typeID == TRANSFORM_CLASS_ID:
            switch = b.attribute

            if switch in [1, 3, 4]:
                # case 1: #kBindTransformPosition
                # case 3: #kBindTransformScale
                # case 4: #kBindTransformEuler
                curves += 3
            elif switch == 2:  # kBindTransformRotation
                curves += 4
            else:
                curves += 1
        else:
            curves += 1
        if curves > index:
            return b
    return None


def _binding_curve_count(binding) -> int:
    if binding.typeID != TRANSFORM_CLASS_ID:
        return 1
    if binding.attribute in [1, 3, 4]:
        return 3
    if binding.attribute == 2:
        return 4
    return 1


def build_binding_info_lookup(
    generic_bindings,
) -> List[Tuple[str, str]]:
    lookup: List[Tuple[str, str]] = []
    for binding in generic_bindings:
        mono_script = binding.script.deref().read()
        target, bone_name = live2d_target_map[mono_script.m_Name]
        if not bone_name:
            bone_name = str(binding.path)
        lookup.extend([(target, bone_name)] * _binding_curve_count(binding))
    return lookup


def _read_streamed_frames(bs: BinaryStream, payload_len: int) -> List:
    frames = []
    while bs.base_stream.tell() < payload_len:
        time = bs.readFloat()
        num_keys = bs.readUInt32()
        key_list = [StreamedCurveKey(bs) for _ in range(num_keys)]
        assert len(key_list) == num_keys
        if time >= 0:
            frames.append({"time": time, "keyList": key_list})
    return frames


def _apply_frame_in_slopes(frame, previous_curve_by_index) -> None:
    for curve_key in frame["keyList"]:
        previous_curve = previous_curve_by_index.get(curve_key.index)
        if previous_curve:
            previous_time, previous_curve_key = previous_curve
            curve_key.inSlope = previous_curve_key.calc_next_in_slope(
                frame["time"] - previous_time,
                curve_key,
            )


def _remember_frame_curves(frame, previous_curve_by_index) -> None:
    for curve_key in frame["keyList"]:
        previous_curve_by_index[curve_key.index] = (frame["time"], curve_key)


def _apply_streamed_in_slopes(frames: List) -> None:
    previous_curve_by_index = {}
    last_frame_index = len(frames) - 1
    for k, frame in enumerate(frames):
        if k >= 2 and k != last_frame_index:
            _apply_frame_in_slopes(frame, previous_curve_by_index)
        if k > 0:
            _remember_frame_curves(frame, previous_curve_by_index)


def process_streamed_clip(streamed_clip: List[int]) -> List:
    _b = struct.pack("I" * len(streamed_clip), *streamed_clip)
    frames = _read_streamed_frames(BinaryStream(BytesIO(_b)), len(_b))
    _apply_streamed_in_slopes(frames)
    return frames


def read_streamed_data(
    motion: Dict,
    binding_info_lookup: List[Tuple[str, str]],
    track_by_name: Dict[str, Dict],
    time: float,
    curve_key: StreamedCurveKey,
):
    idx = curve_key.index
    try:
        target, bone_name = binding_info_lookup[idx]
    except IndexError as exc:
        raise RuntimeError(f"Failed to find binding constant for {idx}") from exc
    if bone_name:
        track = track_by_name.get(bone_name)
        if not track:
            track = {
                "Name": bone_name,
                "Target": target,
                "Curve": [
                    {
                        "time": time,
                        "value": curve_key.value,
                        "inSlope": curve_key.inSlope,
                        "outSlope": curve_key.outSlope,
                        "coeff": curve_key.coeff,
                    }
                ],
            }
            motion["TrackList"].append(track)
            track_by_name[bone_name] = track
        else:
            track["Curve"].append(
                {
                    "time": time,
                    "value": curve_key.value,
                    "inSlope": curve_key.inSlope,
                    "outSlope": curve_key.outSlope,
                    "coeff": curve_key.coeff,
                }
            )


def read_curve_data(
    motion: Dict,
    binding_info_lookup: List[Tuple[str, str]],
    track_by_name: Dict[str, Dict],
    idx: int,
    time: float,
    sample_list: List[float],
    curve_idx: int,
):
    try:
        target, bone_name = binding_info_lookup[idx]
    except IndexError as exc:
        raise RuntimeError(f"Failed to find binding constant for {idx}") from exc
    if bone_name:
        track = track_by_name.get(bone_name)
        if not track:
            track = {
                "Name": bone_name,
                "Target": target,
                "Curve": [
                    {
                        "time": time,
                        "value": sample_list[curve_idx],
                        "inSlope": 0,
                        "outSlope": 0,
                        "coeff": None,
                    }
                ],
            }
            motion["TrackList"].append(track)
            track_by_name[bone_name] = track
        else:
            track["Curve"].append(
                {
                    "time": time,
                    "value": sample_list[curve_idx],
                    "inSlope": 0,
                    "outSlope": 0,
                    "coeff": None,
                }
            )


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


def extract_params_ids_from_moc3(moc3: bytes) -> Dict[str, str]:
    """Extract parameter IDs from moc3 file"""
    bs = BinaryStream(BytesIO(moc3))
    bs.base_stream.seek(0x4C)
    part_base_addr = bs.readUInt32()
    part_end_addr = bs.readUInt32()

    cursor = part_base_addr
    param_id_map = {}

    while part_end_addr - cursor > 64:
        bs.base_stream.seek(cursor)
        param_id = bs.readStringToNull()
        crc = str(crc32(param_id))
        param_id_map[crc] = param_id.decode()
        crc = str(crc32(b"Parts/" + param_id))
        param_id_map[crc] = param_id.decode()

        cursor += 64

    bs.base_stream.seek(0x108)
    param_base_addr = bs.readUInt32()
    param_end_addr = bs.readUInt32()

    cursor = param_base_addr

    while param_end_addr - cursor > 64:
        bs.base_stream.seek(cursor)
        param_id = bs.readStringToNull()
        crc = str(crc32(param_id))
        param_id_map[crc] = param_id.decode()
        crc = str(crc32(b"Parameters/" + param_id))
        param_id_map[crc] = param_id.decode()

        cursor += 64

    return param_id_map


class Live2DBuildMotion:
    _json_aliases = {
        "ClipAssetName": "clip_asset_name",
        "Clip": "clip",
    }

    clip_asset_name: str
    clip: object

    def __init__(
        self,
        clip_asset_name: str,
        clip: object,
    ):
        self.clip_asset_name = clip_asset_name
        self.clip = clip

    def __getattr__(self, name: str):
        try:
            python_name = self._json_aliases[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return object.__getattribute__(self, python_name)

    def to_dict(self) -> Dict[str, object]:
        return {
            json_name: getattr(self, python_name)
            for json_name, python_name in self._json_aliases.items()
        }

    def __repr__(self) -> str:
        return str(self.to_dict())


def get_max_concurrent_motion_base_files(config=None) -> int:
    value = getattr(
        config,
        "MAX_CONCURRENCY_MOTION_BASE_FILES",
        max(1, (os.cpu_count() or 1) // 2),
    )
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid MAX_CONCURRENCY_MOTION_BASE_FILES=%r, falling back to 1",
            value,
        )
        return 1


def _build_motion_save_dir(
    buildmotiondata_path: str,
    local_live2d_motion_extracted_dir: StdPath,
) -> StdPath:
    reldir = (
        PurePosixPath(buildmotiondata_path)
        .relative_to(PurePosixPath(UNITY_FS_CONTAINER_BASE.as_posix()))
        .parent
    )
    rel_parts = reldir.parts[1:]
    if rel_parts[:2] == ("live2d", "motion"):
        rel_parts = rel_parts[2:]
    return local_live2d_motion_extracted_dir.joinpath(*rel_parts)


def _find_buildmotiondata(container_items):
    return next(
        (
            (asset_path, obj.read())
            for asset_path, obj in container_items
            if obj.type.name == "MonoBehaviour" and "buildmotiondata" in asset_path.lower()
        ),
        (None, None),
    )


def _find_container_animation_items(container_items, parent_name: str):
    return [
        Live2DBuildMotion(StdPath(asset_path).stem, pptr)
        for asset_path, pptr in container_items
        if PurePosixPath(asset_path).parent.name == parent_name
        and PurePosixPath(asset_path).suffix == ".anim"
    ]


def _restore_motion_entries(entries, param_id_map: Dict[str, str]):
    restored = [restore_unity_object_to_motion3(entry) for entry in entries]
    restored = [entry for entry in restored if entry is not None]
    correct_param_ids(restored, param_id_map)
    return restored


def _restore_motion_group(
    buildmotiondata,
    container_items,
    group_name: str,
    param_id_map: Dict[str, str],
    motion_bundle_path: StdPath,
):
    entries = getattr(buildmotiondata, group_name)
    restored = _restore_motion_entries(entries, param_id_map)
    # Match original gating: only fall back to container .anim items when the
    # primary group is empty and BuildMotionData.Motions is also empty.
    if restored or buildmotiondata.Motions:
        return restored

    label = "facials" if group_name == "Facials" else "motions"
    parent_name = "facial" if group_name == "Facials" else "motion"
    logger.warning(
        "No %s found in %s, try searching container items",
        label,
        motion_bundle_path,
    )
    container_entries = _find_container_animation_items(container_items, parent_name)
    if not container_entries:
        raise RuntimeError(f"Failed to find {label} in {motion_bundle_path}")
    return _restore_motion_entries(container_entries, param_id_map)


def _write_restored_motions(save_dir, facials, motions):
    all_motion_names = {
        "expressions": [name for name, _ in facials],
        "motions": [name for name, _ in motions],
    }
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / "BuildMotionData.json").write_bytes(
        json.dumps(all_motion_names, option=json.OPT_INDENT_2)
    )

    facial_save_dir = save_dir / "facial"
    facial_save_dir.mkdir(parents=True, exist_ok=True)
    for name, motion in facials:
        (facial_save_dir / f"{name}.motion3.json").write_bytes(
            json.dumps(motion, option=json.OPT_INDENT_2)
        )

    motion_save_dir = save_dir / "motion"
    motion_save_dir.mkdir(parents=True, exist_ok=True)
    for name, motion in motions:
        (motion_save_dir / f"{name}.motion3.json").write_bytes(
            json.dumps(motion, option=json.OPT_INDENT_2)
        )


def _restore_motion_base_bundle_sync(
    motion_base_bundle_path: str,
    local_live2d_motion_extracted_dir: str,
    param_id_map: Dict[str, str],
    unity_version: str,
) -> str:
    motion_bundle_path = StdPath(motion_base_bundle_path)
    try:
        motion_base = load_bundle(motion_base_bundle_path, unity_version)
    except Exception as exc:
        raise RuntimeError(f"Failed to load motion bundle {motion_bundle_path}") from exc

    container_items = list(motion_base.container.items())
    buildmotiondata_path, buildmotiondata = _find_buildmotiondata(container_items)
    if not buildmotiondata_path or not buildmotiondata:
        raise RuntimeError(f"Failed to find buildmotiondata in {motion_bundle_path}")

    facials = _restore_motion_group(
        buildmotiondata,
        container_items,
        "Facials",
        param_id_map,
        motion_bundle_path,
    )
    motions = _restore_motion_group(
        buildmotiondata,
        container_items,
        "Motions",
        param_id_map,
        motion_bundle_path,
    )

    save_dir = _build_motion_save_dir(
        buildmotiondata_path,
        StdPath(local_live2d_motion_extracted_dir),
    )
    _write_restored_motions(save_dir, facials, motions)
    return save_dir.as_posix()


async def collect_param_id_map(
    local_live2d_model_extracted_dir: Path,
) -> Dict[str, str]:
    """Gather the parameter ID map from every ``*.moc3`` under *model_extracted_dir*."""
    param_id_map: Dict[str, str] = {}
    async for moc3_path in local_live2d_model_extracted_dir.glob("**/*.moc3"):
        async with await open_file(moc3_path, "rb") as f:
            moc3 = await f.read()
            param_id_map.update(extract_params_ids_from_moc3(moc3))
    return param_id_map


async def restore_live2d_motions(
    local_live2d_motion_bundle_cache_dir: Path,
    local_live2d_motion_extracted_dir: Path,
    local_live2d_model_extracted_dir: Path,
    unity_version: str,
    config=None,
    *,
    param_id_map: Dict[str, str] | None = None,
    bundle_paths: list[StdPath] | None = None,
):
    if not await local_live2d_motion_bundle_cache_dir.exists():
        raise FileNotFoundError(
            f"Motion bundle dir {local_live2d_motion_bundle_cache_dir} does not exist"
        )
    if not await local_live2d_model_extracted_dir.exists():
        raise FileNotFoundError(
            f"Model extracted dir {local_live2d_model_extracted_dir} does not exist"
        )

    # Gather param ID map (skip when caller pre-supplied one)
    if param_id_map is None:
        param_id_map = await collect_param_id_map(local_live2d_model_extracted_dir)
    logger.debug("Param ID map: %s", param_id_map)

    # Resolve the set of motion bundles to restore
    if bundle_paths is not None:
        motion_base_bundle_paths = list(bundle_paths)
    else:
        motion_base_bundle_paths = []
        async for motion_base_bundle_path in local_live2d_motion_bundle_cache_dir.glob("*"):
            if await motion_base_bundle_path.is_file():
                motion_base_bundle_paths.append(StdPath(motion_base_bundle_path))

    max_concurrency = get_max_concurrent_motion_base_files(config)
    logger.info(
        "Restoring %d live2d motion base bundle(s) with concurrency=%d",
        len(motion_base_bundle_paths),
        max_concurrency,
    )
    semaphore = asyncio.Semaphore(max_concurrency)

    async def restore_one(motion_base_bundle_path: StdPath) -> None:
        async with semaphore:
            save_dir = await asyncio.to_thread(
                _restore_motion_base_bundle_sync,
                motion_base_bundle_path.as_posix(),
                local_live2d_motion_extracted_dir.as_posix(),
                param_id_map,
                unity_version,
            )
            logger.info(
                "Restored %s motion data to %s",
                motion_base_bundle_path,
                save_dir,
            )

    await asyncio.gather(*(restore_one(path) for path in motion_base_bundle_paths))
