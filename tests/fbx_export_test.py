from types import SimpleNamespace
from typing import cast

import pytest

from updater.cli import configuration
from updater.extract import sync_worker
from updater.model import ConfigLike
from updater.unity_rs_adapter import FbxPayload, ModelFilePayload, UnsupportedUnityObjectError


def _payload(skipped=()):
    return FbxPayload(b"fbx", (ModelFilePayload("albedo.png", b"png"),), tuple(skipped))


def test_export_fbx_supports_nested_bundle_name(monkeypatch, tmp_path):
    exported = []
    monkeypatch.setattr(
        sync_worker, "_read_fbx_with_textures", lambda *_args, **_kwargs: _payload()
    )
    sync_worker._export_model_fbx(SimpleNamespace(), tmp_path, "live_pv/models/a", exported)


def test_export_fbx_writes_model_and_textures(monkeypatch, tmp_path):
    payload = _payload(("missing.png",))
    monkeypatch.setattr(sync_worker, "_read_fbx_with_textures", lambda *_args, **_kwargs: payload)
    exported = []
    sync_worker._export_model_fbx(SimpleNamespace(), tmp_path, "models/a", exported)
    assert (tmp_path / "models/a/fbx/model.fbx").read_bytes() == b"fbx"
    assert (tmp_path / "models/a/fbx/albedo.png").read_bytes() == b"png"
    assert len(exported) == 2


@pytest.mark.parametrize("name", ["/absolute", "a/../b", "a//b", "a\\b", "a\x00b"])
def test_export_fbx_rejects_unsafe_bundle_name(monkeypatch, tmp_path, name):
    monkeypatch.setattr(
        sync_worker, "_read_fbx_with_textures", lambda *_args, **_kwargs: _payload()
    )
    unity_file = SimpleNamespace()
    exported = []
    with pytest.raises((ValueError, OSError)):
        sync_worker._export_model_fbx(unity_file, tmp_path, name, exported)


@pytest.mark.parametrize(
    "payload",
    [
        (b"", (), ()),
        (b"fbx", (ModelFilePayload("../x.png", b"x"),), ()),
        (b"fbx", (ModelFilePayload("A.png", b"x"), ModelFilePayload("a.PNG", b"x")), ()),
        (b"fbx", (ModelFilePayload("x.png", "not bytes"),), ()),  # type: ignore[arg-type]
        (b"fbx", (), (1,)),
    ],
)
def test_fbx_payload_validation(payload):
    with pytest.raises(UnsupportedUnityObjectError):
        FbxPayload(*payload)


def test_skipped_texture_warns(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        sync_worker, "_read_fbx_with_textures", lambda *_args, **_kwargs: _payload(("x",))
    )
    with caplog.at_level("WARNING"):
        sync_worker._export_model_fbx(SimpleNamespace(), tmp_path, "bundle", [])
    assert "Skipped FBX texture x" in caplog.text


@pytest.mark.parametrize(
    "enabled,mesh,called", [(False, True, False), (True, False, False), (True, True, True)]
)
def test_extract_fbx_is_content_and_flag_gated(monkeypatch, tmp_path, enabled, mesh, called):
    monkeypatch.setattr(sync_worker, "_load_unity_bundle", lambda *_args: SimpleNamespace())
    monkeypatch.setattr(
        sync_worker, "extract_unity_objects", lambda *_args, **_kwargs: ([], [], [])
    )
    monkeypatch.setattr(sync_worker, "_has_mesh_scene", lambda *_args: mesh)
    export_calls = []
    monkeypatch.setattr(
        sync_worker,
        "_read_fbx_with_textures",
        lambda *_args, **_kwargs: export_calls.append(1) or _payload(),
    )
    bundle = {"bundleName": "bundle", "_enable_model3d_fbx_export": enabled}
    sync_worker._extract_bundle_files_sync("input", bundle, str(tmp_path), "2022", ("png",))
    assert bool(export_calls) is called


@pytest.mark.parametrize(
    "error", [NotImplementedError("skinned weights"), ValueError("sample rate")]
)
def test_unsupported_fbx_export_does_not_fail_normal_extraction(monkeypatch, tmp_path, error):
    normal_file = tmp_path / "normal.txt"
    normal_file.write_bytes(b"normal")

    monkeypatch.setattr(sync_worker, "_load_unity_bundle", lambda *_args: SimpleNamespace())

    def extract_normal(*_args, **_kwargs):
        normal_file.write_bytes(b"normal")
        return ([normal_file], [], [])

    monkeypatch.setattr(sync_worker, "extract_unity_objects", extract_normal)
    monkeypatch.setattr(sync_worker, "_has_mesh_scene", lambda *_args: True)

    def fail_fbx(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(sync_worker, "_read_fbx_with_textures", fail_fbx)
    result, _audio, _video = sync_worker._extract_bundle_files_sync(
        "input",
        {"bundleName": "bundle", "_enable_model3d_fbx_export": True},
        str(tmp_path),
        "2022",
        ("png",),
    )

    assert result == [normal_file.as_posix()]
    assert not (tmp_path / "bundle" / "fbx" / "model.fbx").exists()


def test_unrelated_fbx_reader_oserror_is_not_swallowed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sync_worker,
        "_read_fbx_with_textures",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("permission denied")),
    )

    with pytest.raises(OSError, match="permission denied"):
        sync_worker._export_model_fbx(SimpleNamespace(), tmp_path, "bundle", [])


def test_fbx_write_failure_rolls_back_this_export(monkeypatch, tmp_path):
    payload = FbxPayload(
        b"fbx",
        (ModelFilePayload("first.png", b"first"), ModelFilePayload("second.png", b"second")),
        (),
    )
    monkeypatch.setattr(sync_worker, "_read_fbx_with_textures", lambda *_args, **_kwargs: payload)
    original_write = sync_worker.atomic_write_bytes

    def fail_on_second_texture(path, data):
        original_write(path, data)
        if path.name == "second.png":
            raise OSError("disk full")

    monkeypatch.setattr(sync_worker, "atomic_write_bytes", fail_on_second_texture)
    exported = []

    with pytest.raises(OSError, match="disk full"):
        sync_worker._export_model_fbx(SimpleNamespace(), tmp_path, "bundle", exported)

    assert exported == []
    assert not (tmp_path / "bundle" / "fbx").exists()


def test_image_pixel_mismatch_is_not_downgraded(monkeypatch, tmp_path):
    from updater.extract import unity_objects

    obj = SimpleNamespace(type=SimpleNamespace(name="Texture2D"))
    error = UnsupportedUnityObjectError("unity-rs image returned invalid pixel length")
    monkeypatch.setattr(
        unity_objects, "render_image_asset", lambda _obj: (_ for _ in ()).throw(error)
    )

    with pytest.raises(UnsupportedUnityObjectError, match="pixel length"):
        unity_objects._extract_one_object(
            SimpleNamespace(),
            "texture.asset",
            obj,
            tmp_path / "texture",
            ("png",),
            False,
            2,
            "fast",
            [],
            {},
            [],
            [],
            [],
        )


def test_validate_config_rejects_non_boolean_fbx_flag(monkeypatch):
    from tests.phase5_specialized_validation_test import _valid_config

    monkeypatch.setattr(configuration.shutil, "which", lambda _program: "/bin/true")
    config = _valid_config(ENABLE_MODEL3D_FBX_EXPORT="yes")  # type: ignore[arg-type]
    typed_config = cast(ConfigLike, config)
    with pytest.raises(ValueError, match="ENABLE_MODEL3D_FBX_EXPORT"):
        configuration.validate_config(typed_config)
