"""GLB Phase 5 tests: animation clip decoding and addressable artifacts (#39)."""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from updater import unity_rs_adapter
from updater.extract.glb import DIAG_ANIMATION_UNSUPPORTED_BINDING
from updater.extract.glb.animation import (
    ANIMATION_ARTIFACT_FIELDS,
    animation_artifact_path,
    collect_animation_artifacts,
    decode_animation_clip,
    decode_dense_clip,
    decode_streamed_clip,
    load_animation_artifact,
    persist_animation_artifact,
    validate_animation_artifact,
)


def _streamed_buffer(
    frames: list[tuple[float, list[tuple[int, tuple[float, float, float, float]]]]],
) -> list[int]:
    buf = b""
    for time, keys in frames:
        buf += struct.pack("<fi", time, len(keys))
        for index, coeff in keys:
            buf += struct.pack("<i4f", index, *coeff)
    return list(struct.unpack(f"<{len(buf) // 4}I", buf))


class _TestImage:
    width = 2
    height = 2

    def encode(self, _format: str, compression: str | int = "fast") -> bytes:
        return b"png"


class _AnimationStudio:
    """Minimal binding studio: one file, one target object, animation clips."""

    def __init__(self, clips: dict[int, dict]) -> None:
        source = "stage/with_clips::CAB-a"
        objects = [
            SimpleNamespace(index=0, path="stage/with_clips::CAB-a", unity_version="2022.3.21f1")
        ]
        native = [
            SimpleNamespace(
                file_index=0,
                object_index=0,
                path_id=1,
                class_id=142,
                name="stage/with_clips",
                container="stage/with_clips#bundle",
                source_path=source,
            ),
            SimpleNamespace(
                file_index=0,
                object_index=1,
                path_id=100,
                class_id=1,
                name="target",
                container=None,
                source_path=source,
            ),
        ]
        for path_id, _typetree in clips.items():
            native.append(
                SimpleNamespace(
                    file_index=0,
                    object_index=len(native),
                    path_id=path_id,
                    class_id=74,
                    name=f"clip_{path_id}",
                    container=None,
                    source_path=source,
                )
            )
        self._objects = native
        self._typetrees = {
            (0, 1): {"m_Name": "stage/with_clips"},
            (0, 100): {"m_Name": "target"},
        }
        self._typetrees.update({(0, pid): tree for pid, tree in clips.items()})
        self._files = objects

    def files(self):
        return list(self._files)

    def objects(self):
        return iter(self._objects)

    def scene(self, *, limits=None):
        return []

    def read_asset_bundle(self, _file_index: int, _path_id: int):
        return SimpleNamespace(container=[], dependencies=[])

    def read_type_tree_json(self, file_index: int, path_id: int):
        import orjson

        return orjson.dumps(self._typetrees[(file_index, path_id)])


def _clip_environment(clips: dict[int, dict]):
    return unity_rs_adapter.UnityRsEnvironment(_AnimationStudio(clips))


def _clip_object(environment: object, path_id: int):
    return environment.object_by_identity(0, path_id)


GENERIC_CLIP = {
    "m_Name": "generic_wave",
    "m_SampleRate": 60.0,
    "m_RotationCurves": [
        {
            "path": "Arm",
            "curve": {
                "m_Curve": [
                    {"time": 0.0, "value": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}},
                    {"time": 1.5, "value": {"x": 0.0, "y": 0.7071, "z": 0.0, "w": 0.7071}},
                ]
            },
        }
    ],
    "m_PositionCurves": [
        {
            "path": "Arm",
            "curve": {"m_Curve": [{"time": 0.0, "value": {"x": 1.0, "y": 2.0, "z": 3.0}}]},
        }
    ],
    "m_ScaleCurves": [
        {
            "path": "Arm",
            "curve": {"m_Curve": [{"time": 0.0, "value": {"x": 1.0, "y": 1.0, "z": 1.0}}]},
        }
    ],
    "m_FloatCurves": [
        {
            "path": "",
            "attribute": "material._Shininess",
            "classID": 23,
            "script": {"m_FileID": 0, "m_PathID": 0},
            "curve": {
                "m_Curve": [
                    {"time": 0.0, "value": 0.2, "inSlope": 0.0, "outSlope": 1.0},
                    {"time": 2.0, "value": 1.0, "inSlope": 1.0, "outSlope": 0.0},
                ]
            },
        }
    ],
    "m_PPtrCurves": [
        {
            "path": "Icon",
            "attribute": "m_Sprite",
            "curve": {"m_Curve": [{"time": 0.0, "value": {"m_FileID": 0, "m_PathID": 100}}]},
        }
    ],
}


def test_generic_curves_decode_into_addressable_artifact() -> None:
    environment = _clip_environment({200: GENERIC_CLIP})
    table = unity_rs_adapter.build_collection_file_table(environment)

    artifact = decode_animation_clip(environment, table, _clip_object(environment, 200))

    assert artifact["clip_name"] == "generic_wave"
    assert artifact["sample_rate"] == 60.0
    assert artifact["duration"] == 2.0
    assert artifact["bundle_name"] == "stage/with_clips"
    assert artifact["diagnostics"] == []
    kinds = {curve["kind"] for curve in artifact["curves"]}
    assert kinds == {"rotation", "position", "scale", "float", "pptr"}
    rotation = next(c for c in artifact["curves"] if c["kind"] == "rotation")
    assert rotation["path"] == "Arm"
    assert rotation["keys"][1]["value"] == [0.0, 0.7071, 0.0, 0.7071]
    float_curve = next(c for c in artifact["curves"] if c["kind"] == "float")
    assert float_curve["attribute"] == "material._Shininess"
    assert float_curve["keys"][1] == {"time": 2.0, "value": 1.0, "inSlope": 1.0, "outSlope": 0.0}
    pptr = next(c for c in artifact["curves"] if c["kind"] == "pptr")
    assert pptr["keys"][0]["target"] == {
        "file_id": 0,
        "path_id": 100,
        "bundle_name": "stage/with_clips",
    }
    assert artifact["content_hash"]


def test_streamed_frames_decode_with_hermite_in_slopes() -> None:
    data = _streamed_buffer(
        [
            (0.0, [(5, (0.1, 0.2, 1.0, 7.0))]),
            (1.0, [(5, (0.0, 0.0, 0.0, 9.0))]),  # stepped
            (2.0, [(5, (1.0, 2.0, 3.0, 4.0))]),
            (3.0, [(5, (0.0, 0.0, 1.0, 5.0))]),
        ]
    )

    frames = decode_streamed_clip(data)

    assert [frame["time"] for frame in frames] == [0.0, 1.0, 2.0, 3.0]
    first = frames[0]["keys"][0]
    assert (first["index"], first["value"], first["outSlope"]) == (5, 7.0, 1.0)
    # Frames 0 and 1 keep zero in-slope; frame 2 reconstructs from the
    # stepped frame 1 (null marks the unbounded tangent); the last frame is
    # excluded.
    assert frames[1]["keys"][0]["inSlope"] == 0.0
    assert frames[2]["keys"][0]["inSlope"] is None
    assert frames[3]["keys"][0]["inSlope"] == 0.0


def test_dense_clip_decodes_row_major_samples() -> None:
    dense = decode_dense_clip(
        {
            "m_FrameCount": 2,
            "m_CurveCount": 3,
            "m_SampleRate": 30.0,
            "m_BeginTime": 0.5,
            "m_SampleArray": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )

    assert dense["frames"][0] == {"time": 0.5, "values": [0.0, 1.0, 2.0]}
    assert dense["frames"][1]["time"] == pytest.approx(0.5 + 1 / 30.0)
    assert dense["frames"][1]["values"] == [3.0, 4.0, 5.0]


MUSCLE_CLIP = {
    "m_Name": "muscle_idle",
    "m_SampleRate": 30.0,
    "m_RotationCurves": [],
    "m_PositionCurves": [],
    "m_ScaleCurves": [],
    "m_FloatCurves": [],
    "m_PPtrCurves": [],
    "m_MuscleClip": {
        # Unity 2022 layout: the sections nest under m_Clip.data.
        "m_Clip": {
            "data": {
                "m_StreamedClip": {
                    "data": _streamed_buffer([(0.0, [(0, (0.0, 0.0, 1.0, 0.5))])]),
                },
                "m_DenseClip": {
                    "m_FrameCount": 2,
                    "m_CurveCount": 2,
                    "m_SampleRate": 30.0,
                    "m_BeginTime": 0.0,
                    "m_SampleArray": [0.0, 0.1, 0.2, 0.3],
                },
                "m_ConstantClip": {"data": [1.0, 2.0, 3.0]},
            }
        },
    },
}


def test_muscle_clip_decodes_streams_and_raises_structured_diagnostic() -> None:
    environment = _clip_environment({201: MUSCLE_CLIP})
    table = unity_rs_adapter.build_collection_file_table(environment)

    artifact = decode_animation_clip(environment, table, _clip_object(environment, 201))

    assert artifact["curves"] == []
    codes = [diagnostic["code"] for diagnostic in artifact["diagnostics"]]
    assert codes == [DIAG_ANIMATION_UNSUPPORTED_BINDING]
    message = artifact["diagnostics"][0]["message"]
    assert "1 streamed" in message and "2 dense" in message and "3 constant" in message
    # The decoded streams stay in the artifact for the record.
    assert artifact["streams"]["streamed"][0]["keys"][0]["value"] == 0.5
    assert len(artifact["streams"]["dense"]["frames"]) == 2
    assert artifact["streams"]["constant"] == [1.0, 2.0, 3.0]


def test_artifact_round_trips_through_persist_and_load(tmp_path) -> None:
    environment = _clip_environment({200: GENERIC_CLIP, 201: MUSCLE_CLIP})
    table = unity_rs_adapter.build_collection_file_table(environment)
    artifact = decode_animation_clip(environment, table, _clip_object(environment, 200))

    path = animation_artifact_path(tmp_path, artifact)
    assert path.name == "animation.stage_with_clips.0.200.generic_wave.json"
    persist_animation_artifact(path, artifact)
    assert load_animation_artifact(path) == artifact

    # Corruption and absence load as None instead of raising.
    path.write_text("{not json", encoding="utf-8")
    assert load_animation_artifact(path) is None
    assert load_animation_artifact(tmp_path / "absent.json") is None

    document = dict(artifact)
    document.pop("content_hash")
    with pytest.raises(ValueError, match="fields must be exactly"):
        validate_animation_artifact(document)
    assert set(artifact) == ANIMATION_ARTIFACT_FIELDS


def test_collect_animation_artifacts_walks_animation_objects_sorted(tmp_path) -> None:
    environment = _clip_environment({201: MUSCLE_CLIP, 200: GENERIC_CLIP})
    table = unity_rs_adapter.build_collection_file_table(environment)

    artifacts = collect_animation_artifacts(environment, table)

    assert [artifact["path_id"] for artifact in artifacts] == [200, 201]
    for artifact in artifacts:
        persist_animation_artifact(animation_artifact_path(tmp_path, artifact), artifact)
    assert len(list(tmp_path.glob("animation.*.json"))) == 2


def test_real_animation_clips_round_trip_when_env_set(tmp_path) -> None:
    """Integration: a real motion bundle (SEKAI_GLB_ANIMATION=bundle=path).

    Real Project Sekai clips are muscle clips: every artifact must decode
    its streams, carry the unsupported-binding diagnostic, and persist
    JSON-safely (Unity's infinite terminators become null).
    """

    import os

    spec = os.environ.get("SEKAI_GLB_ANIMATION", "")
    if not spec:
        pytest.skip("SEKAI_GLB_ANIMATION not set or sample bundle missing")
    name, _, raw_path = spec.partition("=")
    bundle = Path(raw_path)
    if not bundle.is_file():
        pytest.skip("SEKAI_GLB_ANIMATION sample bundle missing")

    environment = unity_rs_adapter.load_collection([(name, bundle.read_bytes())], "2022.3.21f1")
    table = unity_rs_adapter.build_collection_file_table(environment)

    artifacts = collect_animation_artifacts(environment, table)
    assert artifacts, "expected at least one AnimationClip"
    for artifact in artifacts:
        persist_animation_artifact(animation_artifact_path(tmp_path, artifact), artifact)
        reloaded = load_animation_artifact(animation_artifact_path(tmp_path, artifact))
        assert reloaded == artifact
