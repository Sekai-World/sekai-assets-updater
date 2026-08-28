"""Narrow regression tests for the unity-rs backed Live2D motion restore.

These tests pin the public behaviour of :mod:`updater.live2d` after the
UnityPy -> unity-rs migration: the same ``motion3.json`` / ``BuildMotionData.json``
output contract must hold when objects are read through
:func:`unity_rs_adapter.load_bundle` instead of ``UnityPy.load``.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
from pathlib import Path

import pytest
from anyio import Path as AnyioPath

from updater import unity_rs_adapter
from updater.live2d import curves, motion3, restore
from updater.unity_rs_adapter import load_bundle

# An optional real on-disk bundle that exposes AnimationClip typetrees, used only
# for a smoke test of the data-reading path. It is NOT a Live2D motion bundle,
# so the Cubism-specific script-name mapping is stubbed there. The path is read
# from the environment (no machine-specific default); the test skips when unset.
REAL_BUNDLE = os.environ.get("SEKAI_REAL_BUNDLE")


def _pack_streamed_clip() -> list[int]:
    """Build a minimal valid single-frame streamed curve payload (uint list)."""
    raw = (
        struct.pack("<f", 0.0)  # frame time
        + struct.pack("<I", 1)  # number of keys
        + struct.pack("<I", 0)  # key index
        + struct.pack("<3f", 0.0, 0.0, 0.0)  # coeff
        + struct.pack("<f", 1.0)  # value
    )
    return [struct.unpack_from("<I", raw, i * 4)[0] for i in range(len(raw) // 4)]


def _animation_clip_tree(name: str, binding_path: int, monoscript_path: int) -> dict:
    return {
        "m_Name": name,
        "m_SampleRate": 60.0,
        "m_MuscleClip": {
            "m_StopTime": 1.0,
            "m_Clip": {
                "data": {
                    "m_StreamedClip": {"data": _pack_streamed_clip(), "curveCount": 1},
                    "m_DenseClip": {
                        "m_FrameCount": 0,
                        "m_CurveCount": 0,
                        "m_SampleRate": 60.0,
                        "m_BeginTime": 0.0,
                        "m_SampleArray": [],
                    },
                    "m_ConstantClip": {"data": []},
                }
            },
        },
        "m_ClipBindingConstant": {
            "genericBindings": [
                {
                    "typeID": 115,
                    "attribute": 0,
                    "script": {"m_FileID": 0, "m_PathID": monoscript_path},
                    "path": binding_path,
                    "customType": 0,
                    "isPPtrCurve": 0,
                    "isIntCurve": 0,
                    "isSerializeReferenceCurve": 0,
                }
            ],
            "pptrCurveMapping": [],
        },
        "m_Events": [{"time": 0.5, "data": "evt1"}],
    }


class _Info:
    def __init__(
        self,
        *,
        file_index: int,
        object_index: int,
        path_id: int,
        class_id: int,
        name: str | None = None,
        container: str | None = None,
        source_path: str = "bundle",
    ) -> None:
        self.file_index = file_index
        self.object_index = object_index
        self.path_id = path_id
        self.class_id = class_id
        self.name = name
        self.container = container
        self.source_path = source_path


class _Studio:
    def __init__(self, objects: list[_Info], type_trees: dict[int, dict]) -> None:
        self._objects = objects
        self._type_trees = type_trees

    def objects(self):
        return iter(self._objects)

    def read_type_tree_json(self, _file_index: int, path_id: int) -> str:
        return json.dumps(self._type_trees[path_id])


def _build_environment(
    *,
    empty_groups: bool = False,
    with_container_anims: bool = False,
) -> unity_rs_adapter.UnityRsEnvironment:
    """Construct a synthetic BuildMotionData bundle.

    Layout mirrors ``assets/sekai/assetbundle/resources/ondemand/live2d/motion/...``
    so that :func:`restore._build_motion_save_dir` resolves a stable save path.
    """
    base = "assets/sekai/assetbundle/resources/ondemand/live2d/motion/base"
    buildmotion_path = f"{base}/buildmotiondata.asset"

    motions = (
        []
        if empty_groups
        else [{"clip_asset_name": "motion_a", "clip": {"m_FileID": 0, "m_PathID": 100}}]
    )
    facials = (
        []
        if empty_groups
        else [{"clip_asset_name": "facial_b", "clip": {"m_FileID": 0, "m_PathID": 101}}]
    )

    type_trees = {
        1: {  # BuildMotionData MonoBehaviour
            "m_Name": "BuildMotionData",
            "Motions": motions,
            "Facials": facials,
        },
        100: _animation_clip_tree("motion_a", 12345, 200),
        101: _animation_clip_tree("facial_b", 6789, 201),
        200: {"m_Name": "CubismParameter", "m_ClassName": "CubismParameter"},
        201: {"m_Name": "CubismPart", "m_ClassName": "CubismPart"},
    }

    objects = [
        _Info(
            file_index=0,
            object_index=0,
            path_id=1,
            class_id=114,
            name="BuildMotionData",
            container=buildmotion_path,
        ),
        _Info(file_index=0, object_index=1, path_id=100, class_id=74, name="motion_a"),
        _Info(file_index=0, object_index=2, path_id=101, class_id=74, name="facial_b"),
        _Info(file_index=0, object_index=3, path_id=200, class_id=115, name="CubismParameter"),
        _Info(file_index=0, object_index=4, path_id=201, class_id=115, name="CubismPart"),
    ]

    if with_container_anims:
        # AnimationClips exposed directly as .anim container items (fallback path).
        # They live under ``motion/`` and ``facial/`` parent directories so the
        # container scan matches the primary group names.
        type_trees[300] = _animation_clip_tree("motion_a", 12345, 200)
        type_trees[301] = _animation_clip_tree("facial_b", 6789, 201)
        objects.extend(
            [
                _Info(
                    file_index=0,
                    object_index=5,
                    path_id=300,
                    class_id=74,
                    name="motion_a",
                    container=f"{base}/motion/motion_a.anim",
                ),
                _Info(
                    file_index=0,
                    object_index=6,
                    path_id=301,
                    class_id=74,
                    name="facial_b",
                    container=f"{base}/facial/facial_b.anim",
                ),
            ]
        )

    return unity_rs_adapter.UnityRsEnvironment(_Studio(objects, type_trees))


def test_restore_motion_base_bundle_writes_motion3_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _build_environment()
    monkeypatch.setattr(restore, "load_bundle", lambda _path, _version: env)

    save_dir = restore._restore_motion_base_bundle_sync(
        "fake.bundle",
        tmp_path.as_posix(),
        {"12345": "ParamA", "6789": "ParamB"},
        "2022.3.21f1",
    )

    root = Path(save_dir)
    build = root / "BuildMotionData.json"
    assert build.is_file()
    index = json.loads(build.read_bytes())
    assert index == {"expressions": ["facial_b"], "motions": ["motion_a"]}

    motion_file = root / "motion" / "motion_a.motion3.json"
    facial_file = root / "facial" / "facial_b.motion3.json"
    assert motion_file.is_file()
    assert facial_file.is_file()

    motion3 = json.loads(motion_file.read_bytes())
    assert motion3["Version"] == 3
    meta = motion3["Meta"]
    assert meta["CurveCount"] == 1
    assert meta["Fps"] == 60.0
    assert meta["Duration"] == 1
    assert meta["TotalSegmentCount"] == 1
    assert meta["TotalPointCount"] == 1
    assert meta["UserDataCount"] == 1
    assert motion3["Curves"] == [{"Target": "Parameter", "Id": "ParamA", "Segments": [0, 1]}]
    assert motion3["UserData"] == [{"Time": 0.5, "Value": "evt1"}]

    facial3 = json.loads(facial_file.read_bytes())
    assert facial3["Curves"] == [{"Target": "PartOpacity", "Id": "ParamB", "Segments": [0, 1]}]


def test_restore_motion_base_bundle_falls_back_to_container_anims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _build_environment(empty_groups=True, with_container_anims=True)
    monkeypatch.setattr(restore, "load_bundle", lambda _path, _version: env)

    save_dir = restore._restore_motion_base_bundle_sync(
        "fake.bundle",
        tmp_path.as_posix(),
        {"12345": "ParamA", "6789": "ParamB"},
        "2022.3.21f1",
    )

    root = Path(save_dir)
    motion_file = root / "motion" / "motion_a.motion3.json"
    facial_file = root / "facial" / "facial_b.motion3.json"
    assert motion_file.is_file()
    assert facial_file.is_file()
    motion3 = json.loads(motion_file.read_bytes())
    assert motion3["Curves"][0]["Id"] == "ParamA"


def test_restore_unity_object_to_motion3_rejects_empty_clip() -> None:
    entry = unity_rs_adapter._AttrDict()
    entry["clip_asset_name"] = "x"
    entry["clip"] = unity_rs_adapter._PPtr(0, 0, lambda _f, _p: None)
    assert motion3.restore_unity_object_to_motion3(entry) is None


@pytest.mark.skipif(
    not REAL_BUNDLE or not Path(REAL_BUNDLE).exists(),
    reason="SEKAI_REAL_BUNDLE not set or sample bundle missing",
)
def test_real_bundle_smoke_reads_animationclip_typetree(monkeypatch: pytest.MonkeyPatch) -> None:
    assert REAL_BUNDLE is not None
    env = load_bundle(Path(REAL_BUNDLE), "2022.3.21f1")
    clips = [o for o in env.objects if o.class_id == 74]
    assert clips, "expected AnimationClip objects in sample bundle"
    clip = clips[0]

    def resolver(fid: int, pid: int):
        return env.resolve_reference(fid, pid, clip.file_index)

    entry = unity_rs_adapter._convert_value(
        {
            "clip_asset_name": clip.name,
            "clip": {"m_FileID": 0, "m_PathID": clip.path_id},
        },
        resolver,
    )

    # The sample is not a Live2D motion bundle, so the Cubism-specific
    # MonoScript name mapping cannot be exercised here; stub it with a neutral
    # one-to-one mapping so the real typetree data-reading path is still smoked.
    original = motion3.build_binding_info_lookup

    def fake_lookup(generic_bindings):
        lookup = []
        for i, b in enumerate(generic_bindings):
            count = curves._binding_curve_count(b)
            lookup.extend([("Parameter", str(b.get("path", i)))] * count)
        return lookup

    monkeypatch.setattr(motion3, "build_binding_info_lookup", fake_lookup)
    try:
        result = motion3.restore_unity_object_to_motion3(entry)
    finally:
        monkeypatch.setattr(motion3, "build_binding_info_lookup", original)

    assert result is not None
    name, motion3_doc = result
    assert name == clip.name
    assert motion3_doc["Version"] == 3
    assert isinstance(motion3_doc["Curves"], list)
    # No silent format change: keys are the documented motion3 contract.
    assert set(motion3_doc) == {"Version", "Meta", "Curves", "UserData"}


def test_class_id_89_is_cubemap() -> None:
    # Unity class ID 89 must resolve to "Cubemap" so that bundle_extraction's
    # `case "Mesh" | "Cubemap"`-style skip logic matches the resolved type name.
    info = _Info(file_index=0, object_index=0, path_id=7, class_id=89, name="fixture")
    env = unity_rs_adapter.UnityRsEnvironment(_Studio([info], {}))
    assert env.objects[0].type.name == "Cubemap"
    # The raw id-based fallback must not be used for a known class.
    assert unity_rs_adapter.CLASS_ID_NAMES[89] == "Cubemap"


def test_restore_live2d_motions_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the public ``restore_live2d_motions`` entry point without mocking it.

    ``restore.load_bundle`` is monkeypatched to the synthetic unity-rs
    environment so no real on-disk bundle or network is required. A raw bundle
    file lives in a temp motion cache, and a model directory (only existence is
    required because the param ID map is pre-supplied) satisfies the inputs.
    """
    motion_cache = tmp_path / "motion_cache"
    motion_cache.mkdir()
    # Raw bundle bytes are never read: load_bundle is monkeypatched below.
    (motion_cache / "base.bundle").write_bytes(b"raw-bundle-bytes")

    # Model dir must exist; we pre-supply the param ID map so no moc3 parsing
    # is needed (and load_bundle is monkeypatched anyway).
    model_dir = tmp_path / "model"
    model_dir.mkdir()

    extracted = tmp_path / "extracted"
    extracted.mkdir()

    monkeypatch.setattr(restore, "load_bundle", lambda _path, _version: _build_environment())

    param_id_map = {"12345": "ParamA", "6789": "ParamB"}

    asyncio.run(
        restore.restore_live2d_motions(
            AnyioPath(str(motion_cache)),
            AnyioPath(str(extracted)),
            AnyioPath(str(model_dir)),
            "2022.3.21f1",
            param_id_map=param_id_map,
        )
    )

    # save dir resolves to <extracted>/base (live2d/motion prefix stripped).
    root = extracted / "base"
    build = root / "BuildMotionData.json"
    assert build.is_file()
    index = json.loads(build.read_bytes())
    assert index == {"expressions": ["facial_b"], "motions": ["motion_a"]}

    motion_file = root / "motion" / "motion_a.motion3.json"
    facial_file = root / "facial" / "facial_b.motion3.json"
    assert motion_file.is_file()
    assert facial_file.is_file()

    motion3 = json.loads(motion_file.read_bytes())
    assert motion3["Version"] == 3
    assert motion3["Curves"] == [{"Target": "Parameter", "Id": "ParamA", "Segments": [0, 1]}]

    facial3 = json.loads(facial_file.read_bytes())
    assert facial3["Curves"] == [{"Target": "PartOpacity", "Id": "ParamB", "Segments": [0, 1]}]
