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


class _FontStudio(_Studio):
    def __init__(self, objects: list[_Info], font):
        super().__init__(objects)
        self.font = font

    def read_font(self, _file_index, _path_id):
        return self.font


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


@pytest.mark.parametrize(
    "font", [b"OTTOfont-data", SimpleNamespace(data=b"\x00\x01\x00\x00font-data")]
)
def test_font_bytes_use_native_reader_without_typetree(font) -> None:
    info = _Info(file_index=0, object_index=0, path_id=8, class_id=128, name="font.otf")
    studio = _FontStudio([info], font)
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    obj = environment.objects[0]

    assert unity_rs_adapter.read_font_bytes(obj) == (font if isinstance(font, bytes) else font.data)


def test_pptr_values_remain_mapping_compatible_and_resolvable() -> None:
    info = _Info(file_index=0, object_index=0, path_id=1, class_id=114)
    environment = unity_rs_adapter.UnityRsEnvironment(_Studio([info]))
    value = environment.objects[0].read()

    pointer = value["m_Asset"]
    assert pointer["m_FileID"] == 0
    assert pointer.m_PathID == 7
    dereferenced = pointer.deref()
    assert dereferenced is None


def test_nonzero_pptr_does_not_silently_resolve_to_a_wrong_file() -> None:
    info = _Info(file_index=0, object_index=0, path_id=1, class_id=114)

    class _CrossFileStudio(_Studio):
        def read_type_tree_json(self, _file_index, _path_id):
            return json.dumps({"m_Asset": {"m_FileID": 2, "m_PathID": 7}})

    obj = unity_rs_adapter.UnityRsEnvironment(_CrossFileStudio([info])).objects[0]
    pointer = obj.read()["m_Asset"]
    with pytest.raises(unity_rs_adapter.UnsupportedReferenceError):
        pointer.deref()


def test_image_contract_rejects_malformed_native_image() -> None:
    malformed = SimpleNamespace(width=2, height=2, rgba=b"short")
    with pytest.raises(unity_rs_adapter.UnsupportedUnityObjectError):
        unity_rs_adapter._rgba_image(malformed)


@pytest.mark.parametrize("width,height", [(0, 1), (1, 0), (-1, 2), (2, -1)])
def test_image_contract_rejects_zero_or_negative_dimensions(width: int, height: int) -> None:
    with pytest.raises(unity_rs_adapter.InvalidImageDimensions, match="invalid dimensions"):
        unity_rs_adapter._rendered_image(SimpleNamespace(width=width, height=height, rgba=b""))


@pytest.mark.parametrize(
    "message",
    ["Texture2D 0x0 carries no image data", "Sprite 0x0 carries no image data"],
)
def test_native_empty_image_error_becomes_invalid_dimensions(message: str) -> None:
    with pytest.raises(unity_rs_adapter.InvalidImageDimensions, match="no image data"):
        unity_rs_adapter._read_native_image(
            lambda: (_ for _ in ()).throw(NotImplementedError(message))
        )


def test_other_native_not_implemented_image_error_is_preserved() -> None:
    with pytest.raises(NotImplementedError, match="decoder unavailable"):
        unity_rs_adapter._read_native_image(
            lambda: (_ for _ in ()).throw(NotImplementedError("decoder unavailable"))
        )


def test_invalid_image_dimensions_remains_unsupported_error() -> None:
    assert issubclass(
        unity_rs_adapter.InvalidImageDimensions,
        unity_rs_adapter.UnsupportedUnityObjectError,
    )


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
