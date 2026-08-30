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


def test_validate_config_rejects_non_boolean_fbx_flag(monkeypatch):
    from tests.phase5_specialized_validation_test import _valid_config

    monkeypatch.setattr(configuration.shutil, "which", lambda _program: "/bin/true")
    config = _valid_config(ENABLE_MODEL3D_FBX_EXPORT="yes")  # type: ignore[arg-type]
    typed_config = cast(ConfigLike, config)
    with pytest.raises(ValueError, match="ENABLE_MODEL3D_FBX_EXPORT"):
        configuration.validate_config(typed_config)
