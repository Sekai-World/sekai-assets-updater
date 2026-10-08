"""Animation clip decoding into independently addressable artifacts (#39).

Unity AnimationClips serialize two curve families:

- generic curves in the typetree (``m_RotationCurves``, ``m_PositionCurves``,
  ``m_ScaleCurves``, ``m_FloatCurves``, ``m_PPtrCurves``) with full keyframes
  — these convert directly to glTF node bindings;
- the packed ``m_MuscleClip`` streams: streamed frames (a uint32 buffer read
  as ``{time, [(curve index, four cubic coefficients)]}`` tuples whose
  in-slopes reconstruct through Unity's Hermite formula — a ``null``
  in-slope marks a stepped, unbounded tangent), dense clips (one
  sample array addressed per frame and curve), and constant clips (values
  bound at time zero).

Muscle-space bindings address Unity's humanoid muscle space, not scene
nodes, so a clip with only muscle streams decodes its streams into the
artifact for the record and raises DIAG_ANIMATION_UNSUPPORTED_BINDING
instead of inventing bindings.

Every clip becomes one self-describing JSON artifact with its own content
hash, so consumers can address, validate, and cache clips independently.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from updater.extract.glb.collection import resolve_pptr
from updater.extract.glb.contracts import DIAG_ANIMATION_UNSUPPORTED_BINDING
from updater.extract.glb.gltf_writer import content_hash
from updater.unity_rs_adapter import (
    CollectionFileTable,
    UnityRsEnvironment,
    UnityRsObject,
)

ANIMATION_ARTIFACT_VERSION = 1
ANIMATION_CLIP_OBJECT_CLASS = 74

_STREAMED_KEY_STRUCT = struct.Struct("<i4f")


@dataclass(frozen=True, slots=True)
class AnimationDiagnostic:
    """One structured animation decoding diagnostic."""

    code: str
    message: str


def decode_streamed_clip(data: Sequence[int]) -> list[dict[str, Any]]:
    """Decode a muscle clip's streamed frame buffer.

    ``data`` is the serialized ``uint32[]``; reinterpreted as little-endian
    bytes it is a frame list of ``{time: f32, key count: i32, keys}`` where
    one key is ``{curve index: i32, coefficients: 4 x f32}``.  ``coeff[2]``
    is the out-slope and ``coeff[3]`` the key value; in-slopes reconstruct
    from the previous key of the same curve with Unity's Hermite formula.
    """

    buffer = struct.pack(f"<{len(data)}I", *data)
    frames: list[dict[str, Any]] = []
    offset = 0
    while offset < len(buffer):
        (time,) = struct.unpack_from("<f", buffer, offset)
        offset += 4
        (key_count,) = struct.unpack_from("<i", buffer, offset)
        offset += 4
        keys: list[dict[str, Any]] = []
        for _ in range(key_count):
            index, c0, c1, c2, c3 = _STREAMED_KEY_STRUCT.unpack_from(buffer, offset)
            offset += _STREAMED_KEY_STRUCT.size
            keys.append(
                {
                    "index": index,
                    "coeff": [c0, c1, c2, c3],
                    "outSlope": c2,
                    "value": c3,
                    "inSlope": 0.0,
                }
            )
        frames.append({"time": time, "keys": keys})
    _reconstruct_in_slopes(frames)
    return frames


def _reconstruct_in_slopes(frames: list[dict[str, Any]]) -> None:
    """Rebuild streamed in-slopes exactly like Unity's exporter: frames from
    the third onward inherit from the nearest earlier key of the same curve;
    the first two and the last frame keep zero in-slope."""

    for frame_index in range(2, len(frames) - 1):
        frame = frames[frame_index]
        for key in frame["keys"]:
            for earlier in range(frame_index - 1, -1, -1):
                previous = next(
                    (
                        candidate
                        for candidate in frames[earlier]["keys"]
                        if candidate["index"] == key["index"]
                    ),
                    None,
                )
                if previous is not None:
                    key["inSlope"] = _next_in_slope(
                        frame["time"] - frames[earlier]["time"], previous, key
                    )
                    break


def _next_in_slope(
    dx: float, previous: Mapping[str, Any], current: Mapping[str, Any]
) -> float | None:
    coefficient = previous["coeff"]
    # NOSONAR: Unity marks stepped keys with exact 0.0f coefficients —
    # the sentinel is bit-exact by serialization, never a rounded value.
    if coefficient[0] == 0.0 and coefficient[1] == 0.0 and coefficient[2] == 0.0:
        return None  # stepped tangent: unbounded slope, not JSON-representable
    dx = max(dx, 0.0001)
    dy = current["value"] - previous["value"]
    length = 1.0 / (dx * dx)
    d1 = previous["outSlope"] * dx
    d2 = 3.0 * dy - 2.0 * d1 - coefficient[1] / length
    return d2 / dx


def decode_dense_clip(dense: Mapping[str, Any]) -> dict[str, Any]:
    """Decode a dense clip: one sample per (frame, curve) in row-major order."""

    frame_count = int(dense["m_FrameCount"])
    curve_count = int(dense["m_CurveCount"])
    sample_rate = float(dense["m_SampleRate"])
    begin_time = float(dense["m_BeginTime"])
    samples = [float(value) for value in dense["m_SampleArray"]]
    frames = []
    for frame in range(frame_count):
        time = begin_time + frame / sample_rate if sample_rate else begin_time
        frames.append(
            {
                "time": time,
                "values": samples[frame * curve_count : (frame + 1) * curve_count],
            }
        )
    return {
        "curve_count": curve_count,
        "frame_count": frame_count,
        "sample_rate": sample_rate,
        "begin_time": begin_time,
        "frames": frames,
    }


def _generic_curves(typetree: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize the typetree's generic curve arrays into artifact records."""

    curves: list[dict[str, Any]] = []
    for source, kind in (
        ("m_RotationCurves", "rotation"),
        ("m_PositionCurves", "position"),
        ("m_ScaleCurves", "scale"),
    ):
        for entry in typetree.get(source) or ():
            keys = [
                {
                    "time": float(key["time"]),
                    "value": [float(component) for component in key["value"].values()],
                }
                for key in entry.get("curve", {}).get("m_Curve", ())
            ]
            curves.append({"kind": kind, "path": entry.get("path", ""), "keys": keys})
    for entry in typetree.get("m_FloatCurves") or ():
        keys = [
            {
                "time": float(key["time"]),
                "value": float(key["value"]),
                "inSlope": float(key.get("inSlope", 0.0)),
                "outSlope": float(key.get("outSlope", 0.0)),
            }
            for key in entry.get("curve", {}).get("m_Curve", ())
        ]
        curves.append(
            {
                "kind": "float",
                "path": entry.get("path", ""),
                "attribute": entry.get("attribute", ""),
                "class_id": int(entry.get("classID", 0)),
                "keys": keys,
            }
        )
    return curves


def _pptr_curves(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    clip_file_index: int,
    typetree: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[AnimationDiagnostic]]:
    curves: list[dict[str, Any]] = []
    diagnostics: list[AnimationDiagnostic] = []
    for entry in typetree.get("m_PPtrCurves") or ():
        keys = []
        for key in entry.get("curve", {}).get("m_Curve", ()):
            pointer = key.get("value", {})
            file_id = int(pointer.get("m_FileID", 0))
            path_id = int(pointer.get("m_PathID", 0))
            target: dict[str, Any] = {"file_id": file_id, "path_id": path_id}
            resolution = resolve_pptr(environment, table, clip_file_index, file_id, path_id)
            if resolution.object is not None:
                target["bundle_name"] = _bundle_for_file(table, resolution.object.file_index)
            elif resolution.dependency_name is not None:
                target["dependency_name"] = resolution.dependency_name
                diagnostics.append(
                    AnimationDiagnostic(
                        code=DIAG_ANIMATION_UNSUPPORTED_BINDING,
                        message=(
                            "pptr curve target lives in unloaded dependency"
                            f" {resolution.dependency_name}"
                        ),
                    )
                )
            keys.append({"time": float(key["time"]), "target": target})
        curves.append(
            {
                "kind": "pptr",
                "path": entry.get("path", ""),
                "attribute": entry.get("attribute", ""),
                "keys": keys,
            }
        )
    return curves, diagnostics


def _bundle_for_file(table: CollectionFileTable, file_index: int) -> str | None:
    for identity in table.identities:
        if identity.file_index == file_index:
            return identity.bundle_name
    return None


def _muscle_stream_container(muscle: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the mapping holding the streamed/dense/constant sections.

    Unity 2022 serializes ``m_MuscleClip.m_Clip.data`` as a nested mapping;
    older layouts keep the sections directly on the muscle clip.
    """

    inner = muscle.get("m_Clip")
    if isinstance(inner, Mapping):
        data = inner.get("data")
        if isinstance(data, Mapping):
            return data
        return inner
    return muscle


def decode_animation_clip(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    clip_object: UnityRsObject,
) -> dict[str, Any]:
    """Decode one AnimationClip object into an artifact document."""

    typetree = clip_object.read_typetree()
    curves = _generic_curves(typetree)
    pptr_curves, diagnostics = _pptr_curves(environment, table, clip_object.file_index, typetree)
    curves.extend(pptr_curves)

    muscle = typetree.get("m_MuscleClip") or {}
    clip_data = _muscle_stream_container(muscle)
    streamed = decode_streamed_clip((clip_data.get("m_StreamedClip") or {}).get("data") or ())
    dense = (
        decode_dense_clip(clip_data["m_DenseClip"])
        if clip_data.get("m_DenseClip")
        else {
            "curve_count": 0,
            "frame_count": 0,
            "sample_rate": 0.0,
            "begin_time": 0.0,
            "frames": [],
        }
    )
    constant = [float(value) for value in (clip_data.get("m_ConstantClip") or {}).get("data") or ()]
    streams = {"streamed": streamed, "dense": dense, "constant": constant}

    if not curves and (streamed or dense["frames"] or constant):
        diagnostics.append(
            AnimationDiagnostic(
                code=DIAG_ANIMATION_UNSUPPORTED_BINDING,
                message=(
                    f"clip {typetree.get('m_Name', '')!r} binds"
                    f" {sum(len(frame['keys']) for frame in streamed)} streamed,"
                    f" {dense['curve_count']} dense, and {len(constant)} constant"
                    " muscle-space values; muscle clips do not address glTF nodes"
                ),
            )
        )

    sample_times = [key["time"] for curve in curves for key in curve["keys"]]
    sample_times.extend(frame["time"] for frame in streamed)
    if dense["frame_count"] and dense["sample_rate"]:
        sample_times.append(dense["begin_time"] + dense["frame_count"] / dense["sample_rate"])
    finite_times = [time for time in sample_times if math.isfinite(time)]
    document: dict[str, Any] = {
        "artifact_version": ANIMATION_ARTIFACT_VERSION,
        "bundle_name": _bundle_for_file(table, clip_object.file_index),
        "file_index": clip_object.file_index,
        "path_id": clip_object.path_id,
        "clip_name": typetree.get("m_Name", ""),
        "sample_rate": float(typetree.get("m_SampleRate", 0.0)),
        "duration": max(finite_times) if finite_times else 0.0,
        "curves": curves,
        "streams": streams,
        "diagnostics": [
            {"code": diagnostic.code, "message": diagnostic.message} for diagnostic in diagnostics
        ],
    }
    document["streams"] = _json_safe(document["streams"])
    document["content_hash"] = _artifact_hash(document)
    return document


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats with ``null``.

    Unity uses infinite frame times as stream terminators and unbounded
    (stepped) tangents; neither is JSON-representable, so ``null`` marks
    them in artifacts.
    """

    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _artifact_hash(document: Mapping[str, Any]) -> str:
    import orjson

    payload = {key: value for key, value in document.items() if key != "content_hash"}
    return content_hash(
        orjson.dumps(payload, option=orjson.OPT_SORT_KEYS),
    )


ANIMATION_ARTIFACT_FIELDS = {
    "artifact_version",
    "bundle_name",
    "file_index",
    "path_id",
    "clip_name",
    "sample_rate",
    "duration",
    "curves",
    "streams",
    "diagnostics",
    "content_hash",
}


def validate_animation_artifact(value: Any) -> dict[str, Any]:
    """Validate one persisted animation artifact document."""

    if not isinstance(value, dict):
        raise ValueError("animation artifact must be an object")
    if set(value) != ANIMATION_ARTIFACT_FIELDS:
        raise ValueError(
            f"animation artifact fields must be exactly"
            f" {sorted(ANIMATION_ARTIFACT_FIELDS)}, got {sorted(value)}"
        )
    if value["artifact_version"] != ANIMATION_ARTIFACT_VERSION:
        raise ValueError("animation artifact_version must be 1")
    if not isinstance(value["clip_name"], str):
        raise ValueError("animation artifact.clip_name must be a string")
    if not isinstance(value["curves"], list):
        raise ValueError("animation artifact.curves must be a list")
    streams = value["streams"]
    if not isinstance(streams, dict) or set(streams) != {"streamed", "dense", "constant"}:
        raise ValueError("animation artifact.streams must contain streamed/dense/constant")
    if not isinstance(value["content_hash"], str) or not value["content_hash"]:
        raise ValueError("animation artifact.content_hash must be a non-empty string")
    return value


def persist_animation_artifact(path, document: dict[str, Any]) -> None:
    """Atomically persist one animation artifact."""

    from updater.state import atomic_write_json

    atomic_write_json(path, document, validate_animation_artifact)


def load_animation_artifact(path) -> dict[str, Any] | None:
    """Load one persisted animation artifact, or ``None`` when absent/corrupt."""

    import json
    import os
    from pathlib import Path as StdPath

    target = StdPath(os.fspath(path))
    if not target.exists():
        return None
    try:
        return validate_animation_artifact(json.loads(target.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return None


def animation_artifact_path(directory, document: Mapping[str, Any]):
    """Deterministic, identity-addressed artifact file name."""

    from pathlib import Path as StdPath

    def slug(value: Any) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)) or "clip"

    return StdPath(directory) / (
        f"animation.{slug(document['bundle_name'])}.{document['file_index']}"
        f".{document['path_id']}.{slug(document['clip_name'])}.json"
    )


def collect_animation_artifacts(
    environment: UnityRsEnvironment, table: CollectionFileTable
) -> tuple[dict[str, Any], ...]:
    """Decode every AnimationClip in the collection, in object-table order."""

    artifacts = [
        decode_animation_clip(environment, table, clip)
        for clip in environment.objects
        if clip.class_id == ANIMATION_CLIP_OBJECT_CLASS
    ]
    return tuple(sorted(artifacts, key=lambda document: _artifact_sort_key(document)))


def _artifact_sort_key(document: Mapping[str, Any]) -> tuple[str, int, int]:
    return (
        str(document["bundle_name"]),
        int(document["file_index"]),
        int(document["path_id"]),
    )
