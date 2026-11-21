"""Mesh topology, vertex identity, material slots, and skin semantics."""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

import updater.extract.glb.export as glb_export
from tests.glb_export_test import ROOT_IDENTITY, _environment, _extraction
from updater.extract.glb import export_root_glb, parse_obj_mesh, validate_glb_bytes
from updater.extract.glb.skin import Skeleton, SkinJoint, SkinWeights

SEAM_OBJ = b"""\
v 0 0 0
v 1 0 0
v 0 1 0
v 1 1 0
vt 0 0
vt 1 0
vt 0 1
vt 0.5 0.5
vn 0 0 1
f 3/3/1 2/2/1 1/1/1
f 3/4/1 4/2/1 2/2/1
"""


def _attribute(rows: list[tuple[float, ...]], dimension: int) -> SimpleNamespace:
    values = [value for row in rows for value in row]
    payload = struct.pack(f"<{len(values)}f", *values) if values else b""
    return SimpleNamespace(dimension=dimension, data=payload)


def _native_mesh() -> SimpleNamespace:
    return SimpleNamespace(
        vertex_count=4,
        positions=_attribute(
            [(1.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 3.0), (4.0, 4.0, 4.0)],
            3,
        ),
        normals=_attribute(
            [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 1.0, 0.0)],
            3,
        ),
        uvs=[_attribute([(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)], 2)],
        sub_meshes=[
            SimpleNamespace(index_count=3, indices=struct.pack("<3I", 0, 1, 2)),
            SimpleNamespace(index_count=3, indices=struct.pack("<3I", 1, 3, 2)),
        ],
    )


def _provider(vertex_joints, vertex_weights):
    identity = (
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    skeleton = Skeleton(
        joints=(
            SkinJoint((0, 100), "root_a", identity, identity),
            SkinJoint((0, 101), "child_a", identity, identity),
        )
    )

    class Provider:
        def skin_weights(self, _node, _scene_nodes):
            return SkinWeights(
                skeleton=skeleton,
                vertex_joints=tuple(vertex_joints),
                vertex_weights=tuple(vertex_weights),
            )

    return Provider()


def _read_accessor(data: bytes, document: dict, accessor_index: int, fmt: str):
    accessor = document["accessors"][accessor_index]
    view = document["bufferViews"][accessor["bufferView"]]
    json_length = struct.unpack_from("<I", data, 12)[0]
    bin_start = 20 + json_length + 8
    start = bin_start + view["byteOffset"]
    payload = data[start : start + view["byteLength"]]
    return [tuple(row) for row in struct.iter_unpack(fmt, payload)]


def _add_two_test_materials(monkeypatch: pytest.MonkeyPatch) -> None:
    def add_material(binder, pointer):
        return binder._scene.add_material(
            {
                "name": f"material_{pointer[1]}",
                "pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 1.0]},
            }
        )

    monkeypatch.setattr(glb_export._MaterialBinder, "index_for", add_material)


def test_native_mesh_preserves_vertices_submeshes_materials_and_node_skin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment()
    native_mesh = _native_mesh()
    environment.studio.read_mesh = lambda _file_index, _path_id: native_mesh
    child = environment.studio._scene[1]
    child.materials = [(0, 901), (0, 902)]
    child.bones = [(0, 120)]
    _add_two_test_materials(monkeypatch)
    extraction, table = _extraction(environment)
    skin_provider = _provider(
        [(0, 0, 0, 0), (1, 0, 0, 0), (0, 1, 0, 0), (1, 1, 0, 0)],
        [
            (0.1, 0.9, 0.0, 0.0),
            (0.2, 0.8, 0.0, 0.0),
            (0.3, 0.7, 0.0, 0.0),
            (0.4, 0.6, 0.0, 0.0),
        ],
    )

    export = export_root_glb(
        environment,
        table,
        extraction,
        ROOT_IDENTITY,
        skin_weights_provider=skin_provider,
    )

    document = validate_glb_bytes(export.glb_bytes)
    mesh = document["meshes"][0]
    assert len(mesh["primitives"]) == 2
    assert [primitive["material"] for primitive in mesh["primitives"]] == [1, 2]
    assert [
        _read_accessor(export.glb_bytes, document, primitive["indices"], "<I")
        for primitive in mesh["primitives"]
    ] == [[(0,), (2,), (1,)], [(1,), (2,), (3,)]]

    attributes = mesh["primitives"][0]["attributes"]
    assert _read_accessor(export.glb_bytes, document, attributes["POSITION"], "<3f") == [
        (-1.0, 0.0, 0.0),
        (0.0, 2.0, 0.0),
        (0.0, 0.0, 3.0),
        (-4.0, 4.0, 4.0),
    ]
    assert _read_accessor(export.glb_bytes, document, attributes["NORMAL"], "<3f")[0] == (
        -1.0,
        0.0,
        0.0,
    )
    assert _read_accessor(export.glb_bytes, document, attributes["TEXCOORD_0"], "<2f") == [
        (0.0, 0.0),
        (1.0, 0.0),
        (0.0, 1.0),
        (1.0, 1.0),
    ]
    assert document["nodes"][1]["skin"] == 0
    for primitive in mesh["primitives"]:
        assert "skin" not in primitive
        assert "JOINTS_0" in primitive["attributes"]
        assert "WEIGHTS_0" in primitive["attributes"]
        assert document["accessors"][primitive["attributes"]["JOINTS_0"]]["count"] == 4
        assert document["accessors"][primitive["attributes"]["WEIGHTS_0"]]["count"] == 4
    joint_rows = _read_accessor(export.glb_bytes, document, attributes["JOINTS_0"], "<4H")
    weight_rows = _read_accessor(export.glb_bytes, document, attributes["WEIGHTS_0"], "<4f")
    assert [row[0] for row in joint_rows] == [0, 1, 0, 1]
    assert [row[0] for row in weight_rows] == pytest.approx([0.1, 0.2, 0.3, 0.4])
    skin_mapping = export.manifest["mappings"]["skins"][0]
    assert skin_mapping["weights_available"] is True
    assert skin_mapping["skin_attached"] is True


def test_obj_vertex_identity_remaps_weights_across_reorder_and_uv_seam() -> None:
    geometry = parse_obj_mesh(SEAM_OBJ)
    assert geometry["source_vertex_indices"] == [2, 1, 0, 2, 3]

    environment = _environment()
    environment.studio._mesh_obj[102] = SEAM_OBJ
    child = environment.studio._scene[1]
    child.bones = [(0, 120)]
    extraction, table = _extraction(environment)
    source_joints = [
        (0, 0, 0, 0),
        (1, 0, 0, 0),
        (0, 1, 0, 0),
        (1, 1, 0, 0),
    ]
    source_weights = [
        (0.1, 0.9, 0.0, 0.0),
        (0.2, 0.8, 0.0, 0.0),
        (0.3, 0.7, 0.0, 0.0),
        (0.4, 0.6, 0.0, 0.0),
    ]

    export = export_root_glb(
        environment,
        table,
        extraction,
        ROOT_IDENTITY,
        skin_weights_provider=_provider(source_joints, source_weights),
    )

    document = validate_glb_bytes(export.glb_bytes)
    attributes = document["meshes"][0]["primitives"][0]["attributes"]
    joint_rows = _read_accessor(export.glb_bytes, document, attributes["JOINTS_0"], "<4H")
    weight_rows = _read_accessor(export.glb_bytes, document, attributes["WEIGHTS_0"], "<4f")
    assert [row[0] for row in joint_rows] == [0, 1, 0, 0, 1]
    assert [row[0] for row in weight_rows] == pytest.approx([0.3, 0.2, 0.1, 0.3, 0.4])
    assert len(joint_rows) == document["accessors"][attributes["POSITION"]]["count"]


def test_native_missing_material_slot_uses_default_and_records_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _environment()
    environment.studio.read_mesh = lambda _file_index, _path_id: _native_mesh()
    environment.studio._scene[1].materials = [(0, 901)]
    material_calls = []

    def resolve_one_material(_binder, pointer):
        material_calls.append(pointer)
        return 0

    monkeypatch.setattr(glb_export._MaterialBinder, "index_for", resolve_one_material)
    extraction, table = _extraction(environment)

    export = export_root_glb(environment, table, extraction, ROOT_IDENTITY)

    document = validate_glb_bytes(export.glb_bytes)
    assert [primitive["material"] for primitive in document["meshes"][0]["primitives"]] == [0, 0]
    assert material_calls == [(0, 901)]
    assert any(
        "submesh 1 has no material slot" in diagnostic["message"]
        for diagnostic in export.manifest["diagnostics"]
    )


@pytest.mark.parametrize("invalid_part", ["positions", "non_finite", "indices"])
def test_invalid_native_mesh_data_is_rejected_without_obj_fallback(invalid_part: str) -> None:
    environment = _environment()
    native_mesh = _native_mesh()
    if invalid_part == "positions":
        native_mesh.positions = SimpleNamespace(dimension=3, data=b"invalid")
        message = "positions byte length"
    elif invalid_part == "non_finite":
        values = [float("nan"), *([0.0] * 11)]
        native_mesh.positions = SimpleNamespace(dimension=3, data=struct.pack("<12f", *values))
        message = "positions contains a non-finite value"
    else:
        native_mesh.sub_meshes[0].indices = struct.pack("<3I", 0, 1, 4)
        message = "index exceeds vertex_count"
    environment.studio.read_mesh = lambda _file_index, _path_id: native_mesh
    extraction, table = _extraction(environment)

    with pytest.raises(ValueError, match=message):
        export_root_glb(environment, table, extraction, ROOT_IDENTITY)
