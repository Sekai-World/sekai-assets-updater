"""Unity AnimationClip curve decoding for Cubism targets."""

import logging
import struct
from io import BytesIO
from typing import Dict, List, Tuple

from updater.media.binary import BinaryStream

logger = logging.getLogger("live2d")


TRANSFORM_CLASS_ID = 4

live2d_target_map = {
    "CubismParameter": ("Parameter", None),
    "CubismPart": ("PartOpacity", None),
    "CubismRenderController": ("Model", "Opacity"),
    "CubismEyeBlinkController": ("Model", "EyeBlink"),
    "CubismMouthController": ("Model", "LipSync"),
}


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
