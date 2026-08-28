from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from updater import unity_rs_adapter


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
    def __init__(self, objects: list[_Info]) -> None:
        self._objects = objects

    def objects(self):
        return iter(self._objects)

    def read_type_tree_json(self, _file_index, _path_id):
        return json.dumps({"m_Name": "fixture", "m_Asset": {"m_FileID": 0, "m_PathID": 7}})

    def read_text(self, _file_index, _path_id):
        return b"fixture\xff"


def test_environment_sorts_objects_and_exposes_unique_container_entries() -> None:
    infos = [
        _Info(file_index=1, object_index=2, path_id=20, class_id=49, container="second"),
        _Info(file_index=0, object_index=4, path_id=10, class_id=49, container="first"),
        _Info(file_index=1, object_index=1, path_id=19, class_id=49, container="second"),
    ]

    environment = unity_rs_adapter.UnityRsEnvironment(_Studio(infos))

    assert [(obj.file_index, obj.object_index) for obj in environment.objects] == [
        (0, 4),
        (1, 1),
        (1, 2),
    ]
    assert list(environment.container) == ["first", "second"]
    assert environment.container["second"].object_index == 1


def test_environment_uses_asset_bundle_container_targets() -> None:
    root = _Info(file_index=0, object_index=0, path_id=42, class_id=114, container="wrong")
    asset_bundle = _Info(file_index=0, object_index=1, path_id=1, class_id=142)

    class _AssetBundleStudio(_Studio):
        def read_asset_bundle(self, _file_index, _path_id):
            return SimpleNamespace(container=[("actual.playable", 0, 1, (0, 42))])

    environment = unity_rs_adapter.UnityRsEnvironment(_AssetBundleStudio([root, asset_bundle]))

    assert environment.container["actual.playable"].path_id == 42
    assert environment.container["actual.playable"].container == "actual.playable"


def test_type_tree_and_text_asset_keep_json_and_raw_bytes() -> None:
    info = _Info(file_index=0, object_index=0, path_id=7, class_id=49, name="fixture")
    obj = unity_rs_adapter.UnityRsEnvironment(_Studio([info])).objects[0]

    assert obj.read_typetree()["m_Name"] == "fixture"
    assert unity_rs_adapter.read_text_bytes(obj) == b"fixture\xff"
    assert obj.read().m_Script.encode("utf-8", "surrogateescape") == b"fixture\xff"


def test_pptr_values_remain_mapping_compatible_and_resolvable() -> None:
    info = _Info(file_index=0, object_index=0, path_id=1, class_id=114)
    environment = unity_rs_adapter.UnityRsEnvironment(_Studio([info]))
    value = environment.objects[0].read()

    pointer = value["m_Asset"]
    assert pointer["m_FileID"] == 0
    assert pointer.m_PathID == 7
    assert pointer.deref() is None


def test_nonzero_pptr_does_not_silently_resolve_to_a_wrong_file() -> None:
    info = _Info(file_index=0, object_index=0, path_id=1, class_id=114)

    class _CrossFileStudio(_Studio):
        def read_type_tree_json(self, _file_index, _path_id):
            return json.dumps({"m_Asset": {"m_FileID": 2, "m_PathID": 7}})

    obj = unity_rs_adapter.UnityRsEnvironment(_CrossFileStudio([info])).objects[0]
    with pytest.raises(unity_rs_adapter.UnsupportedReferenceError):
        obj.read()["m_Asset"].deref()


def test_image_contract_rejects_malformed_native_image() -> None:
    with pytest.raises(unity_rs_adapter.UnsupportedUnityObjectError):
        unity_rs_adapter._rgba_image(SimpleNamespace(width=2, height=2, rgba=b"short"))


def test_load_bundle_requires_explicit_unity_version(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(unity_rs_adapter.UnityRsLoadError, match="requires UNITY_VERSION"):
        unity_rs_adapter.load_bundle(b"fixture", None)

    class _FakeUnityRs:
        def __new__(cls, _path, *, unity_version):
            assert unity_version == "2022.3.52f1"
            return _Studio([])

        @staticmethod
        def from_bytes(_data, *, unity_version):
            assert unity_version == "2022.3.52f1"
            return _Studio([])

    monkeypatch.setattr(unity_rs_adapter.unity_rs, "UnityRs", _FakeUnityRs)
    environment = unity_rs_adapter.load_bundle(b"fixture", "2022.3.52f1")
    assert environment.objects == []
