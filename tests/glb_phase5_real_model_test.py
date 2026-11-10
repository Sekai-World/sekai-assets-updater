"""Strict, opt-in real-bundle acceptance for stage and character GLB exports."""

from __future__ import annotations

import hashlib
import math
import os
import struct
from pathlib import Path

import pytest

from updater import unity_rs_adapter
from updater.extract.glb import (
    SCENE3D_PURPOSE,
    BundleRef,
    ExtractionPlan,
    RootRef,
    export_root_glb,
    validate_glb_bytes,
)
from updater.extract.glb.collection import (
    GlbCollectionExtraction,
    build_package_manifest,
    validate_collection_reachability,
)
from updater.extract.glb.export import select_root_subtree
from updater.extract.glb.math3d import compose_trs, mirror_position, multiply_mat4

_COMPONENTS = {5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
_UNITY_VERSION = "2022.3.21f1"


def _inputs(variable: str) -> list[tuple[str, bytes]]:
    spec = os.environ.get(variable, "")
    if not spec:
        pytest.skip(f"{variable} not set")
    inputs = []
    for entry in spec.split(";"):
        name, separator, raw_path = entry.partition("=")
        if not separator or not name or not raw_path:
            raise ValueError(f"invalid {variable} entry; expected bundle=absolute_path")
        path = Path(raw_path)
        assert path.is_file(), f"{variable} bundle path does not exist: {path}"
        inputs.append((name, path.read_bytes()))
    assert inputs
    return inputs


def _package(inputs, environment, table, root, root_id, kind):
    identity = table.identity_for(root.file_index)
    assert identity is not None
    assert any(name == identity.bundle_name for name, _ in inputs)
    plan = ExtractionPlan(
        package_id=f"real_model/{kind}",
        purpose=SCENE3D_PURPOSE,
        profile="scene3d",
        roots=(
            RootRef(
                root_id=root_id,
                kind=kind,
                bundle_name=identity.bundle_name,
                file_index=root.file_index,
                path_id=root.path_id,
            ),
        ),
        bundles=tuple(BundleRef(bundle_name=name) for name, _ in inputs),
    )
    report = validate_collection_reachability(environment, table, plan.roots)
    errors = [finding for finding in report.findings if finding.severity == "error"]
    assert report.publishable, errors
    manifest = build_package_manifest(plan, inputs, _UNITY_VERSION, report, environment, table)
    return GlbCollectionExtraction(manifest=manifest, report=report)


def _binary(data: bytes) -> bytes:
    json_length = struct.unpack_from("<I", data, 12)[0]
    bin_header = 20 + json_length
    bin_length = struct.unpack_from("<I", data, bin_header)[0]
    return data[bin_header + 8 : bin_header + 8 + bin_length]


def _accessor(document, binary: bytes, accessor_index: int) -> list[tuple]:
    accessor = document["accessors"][accessor_index]
    code, component_size = _COMPONENTS[accessor["componentType"]]
    width = _WIDTHS[accessor["type"]]
    packed_size = component_size * width
    view = document["bufferViews"][accessor["bufferView"]]
    stride = view.get("byteStride", packed_size)
    assert stride >= packed_size
    start = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    count = accessor["count"]
    assert start + (count - 1) * stride + packed_size <= len(binary)
    return [
        struct.unpack_from(f"<{code * width}", binary, start + index * stride)
        for index in range(count)
    ]


def _assert_embedded_textures(document, binary: bytes) -> None:
    images = document.get("images", [])
    textures = document.get("textures", [])
    assert images and textures
    image_hashes = []
    for image in images:
        view = document["bufferViews"][image["bufferView"]]
        start = view.get("byteOffset", 0)
        payload = binary[start : start + view["byteLength"]]
        assert payload and image["mimeType"] == "image/png"
        image_hashes.append(hashlib.sha256(payload).digest())
    assert len(image_hashes) == len(set(image_hashes)), "duplicate embedded image payloads"
    assert all(texture["source"] < len(images) for texture in textures)
    used = {
        holder["index"]
        for material in document.get("materials", [])
        for container in (material, material.get("pbrMetallicRoughness", {}))
        for slot in ("baseColorTexture", "normalTexture", "emissiveTexture")
        if isinstance((holder := container.get(slot)), dict)
    }
    assert used, "expected a converted material to reference an embedded texture"
    assert all(0 <= index < len(textures) for index in used)


def _assert_geometry(environment, subtree, exported, document, binary) -> None:
    mappings = exported.manifest["mappings"]
    material_ids = [(item["file_index"], item["path_id"]) for item in mappings["materials"]]
    assert len(material_ids) == len(set(material_ids)), "material identities were duplicated"
    material_map = {
        (item["file_index"], item["path_id"]): item["gltf_material"]
        for item in mappings["materials"]
    }
    node_map = {
        (item["file_index"], item["path_id"]): item["gltf_node"] for item in mappings["nodes"]
    }
    mesh_map = {
        (item["file_index"], item["path_id"], item["gltf_node"]): item["gltf_mesh"]
        for item in mappings["meshes"]
    }
    mesh_nodes = [node for node in subtree if node.mesh is not None]
    assert mesh_nodes
    assert len(mesh_nodes) == len(mappings["meshes"])
    triangle_count = 0
    for node in mesh_nodes:
        node_index = node_map[(node.file_index, node.path_id)]
        mesh_identity = tuple(node.mesh)
        mesh_index = mesh_map[(*mesh_identity, node_index)]
        assert document["nodes"][node_index]["mesh"] == mesh_index
        native = environment.studio.read_mesh(*mesh_identity)
        vertex_count = native.vertex_count
        assert vertex_count > 0
        source_positions = list(struct.iter_unpack("<3f", bytes(native.positions.data)))
        assert native.positions.dimension == 3 and len(source_positions) == vertex_count
        primitives = document["meshes"][mesh_index]["primitives"]
        submeshes = list(native.sub_meshes)
        source_materials = tuple(node.materials or ())
        assert len(primitives) == len(submeshes) == len(source_materials)
        position_rows = _accessor(document, binary, primitives[0]["attributes"]["POSITION"])
        assert len(position_rows) == vertex_count
        for sample in {0, vertex_count // 2, vertex_count - 1}:
            assert position_rows[sample] == pytest.approx(mirror_position(source_positions[sample]))
        for slot, (submesh, primitive, source_material) in enumerate(
            zip(submeshes, primitives, source_materials, strict=True)
        ):
            source_indices = [row[0] for row in struct.iter_unpack("<I", bytes(submesh.indices))]
            assert len(source_indices) == submesh.index_count and len(source_indices) % 3 == 0
            assert all(index < vertex_count for index in source_indices)
            expected = [
                index
                for triangle in zip(
                    source_indices[::3], source_indices[1::3], source_indices[2::3], strict=True
                )
                for index in (triangle[0], triangle[2], triangle[1])
            ]
            gltf_indices = [row[0] for row in _accessor(document, binary, primitive["indices"])]
            assert len(gltf_indices) == len(source_indices)
            assert all(index < vertex_count for index in gltf_indices)
            assert gltf_indices == expected, f"submesh {slot} winding/order changed"
            material_identity = tuple(source_material)
            assert material_identity in material_map
            assert primitive["material"] == material_map[material_identity]
            triangle_count += len(gltf_indices) // 3
    assert triangle_count > 0
    assert mappings["materials"] and document.get("materials")
    _assert_embedded_textures(document, binary)


def _source_scene_world(identity, scene_by_id, cache):
    if identity not in cache:
        node = scene_by_id[identity]
        local = compose_trs(
            node.local_position or (0, 0, 0),
            node.local_rotation or (0, 0, 0, 1),
            node.local_scale or (1, 1, 1),
        )
        cache[identity] = (
            local
            if node.parent is None
            else multiply_mat4(_source_scene_world(node.parent, scene_by_id, cache), local)
        )
    return cache[identity]


def _gltf_scene_world(index, document, parent_by_node, cache):
    if index not in cache:
        node = document["nodes"][index]
        local = (
            tuple(node["matrix"])
            if "matrix" in node
            else compose_trs(
                node.get("translation", (0, 0, 0)),
                node.get("rotation", (0, 0, 0, 1)),
                node.get("scale", (1, 1, 1)),
            )
        )
        assert "matrix" not in node or len(node["matrix"]) == 16
        cache[index] = (
            multiply_mat4(
                _gltf_scene_world(parent_by_node[index], document, parent_by_node, cache), local
            )
            if index in parent_by_node
            else local
        )
    return cache[index]


def _transform_point(matrix, point):
    return tuple(
        sum(matrix[column * 4 + row] * point[column] for column in range(3)) + matrix[12 + row]
        for row in range(3)
    )


def _assert_cpu_skin_samples(
    environment, table, subtree, body, skin, native, document, binary, node_map
):
    scene_by_id = {(node.file_index, node.path_id): node for node in subtree}
    parent_by_node = {
        child: parent
        for parent, node in enumerate(document["nodes"])
        for child in node.get("children", [])
    }
    source_cache, gltf_cache, source_worlds, gltf_worlds = {}, {}, [], []
    for index, bone in enumerate(body.bones):
        identity = (int(bone[0]), int(bone[1]))
        transform = environment.object_by_identity(*identity)
        assert transform is not None and transform.class_id == 4
        pointer = transform.read_typetree()["m_GameObject"]
        file_id = int(pointer.get("m_FileID", 0))
        file_index = (
            table.resolve_file_id(identity[0], file_id).target_file_index
            if file_id
            else identity[0]
        )
        assert file_index is not None
        game_object = file_index, int(pointer["m_PathID"])
        assert game_object in scene_by_id and skin["joints"][index] == node_map[game_object]
        source_worlds.append(_source_scene_world(game_object, scene_by_id, source_cache))
        gltf_worlds.append(
            _gltf_scene_world(skin["joints"][index], document, parent_by_node, gltf_cache)
        )
    body_node = node_map[(body.file_index, body.path_id)]
    primitive = document["meshes"][document["nodes"][body_node]["mesh"]]["primitives"][0]
    attributes = primitive["attributes"]
    gltf = {
        key: _accessor(document, binary, attributes[key])
        for key in ("POSITION", "JOINTS_0", "WEIGHTS_0")
    }
    gltf_ibms = _accessor(document, binary, skin["inverseBindMatrices"])
    raw_positions = bytes(native.positions.data)
    native_bindposes = bytes(native.bind_poses)
    raw_bindposes = list(struct.iter_unpack("<16f", native_bindposes))
    raw_weights = list(struct.iter_unpack("<4f", bytes(native.skin_weights)))
    raw_joints = list(struct.iter_unpack("<4I", bytes(native.skin_bone_indices)))
    samples = {0, native.vertex_count // 2, native.vertex_count - 1}
    for vertex in samples:
        source_position = struct.unpack_from("<3f", raw_positions, vertex * 12)
        source_total = sum(raw_weights[vertex])
        source_deformed = gltf_deformed = (0.0, 0.0, 0.0)
        for source_joint, source_weight, gltf_joint, gltf_weight in zip(
            raw_joints[vertex],
            raw_weights[vertex],
            gltf["JOINTS_0"][vertex],
            gltf["WEIGHTS_0"][vertex],
            strict=True,
        ):
            if source_weight:
                point = _transform_point(
                    multiply_mat4(source_worlds[source_joint], raw_bindposes[source_joint]),
                    source_position,
                )
                source_deformed = tuple(
                    a + b * source_weight / source_total
                    for a, b in zip(source_deformed, point, strict=True)
                )
            if gltf_weight:
                point = _transform_point(
                    multiply_mat4(gltf_worlds[gltf_joint], gltf_ibms[gltf_joint]),
                    gltf["POSITION"][vertex],
                )
                gltf_deformed = tuple(
                    a + b * gltf_weight for a, b in zip(gltf_deformed, point, strict=True)
                )
        assert gltf_deformed == pytest.approx(mirror_position(source_deformed), abs=1e-4, rel=1e-4)


def _assert_character_skin(environment, table, subtree, exported, document, binary) -> None:
    bodies = [node for node in subtree if node.name == "Body" and node.mesh and node.bones]
    assert len(bodies) == 1, "expected the real Body renderer with native skin data"
    body = bodies[0]
    node_map = {
        (item["file_index"], item["path_id"]): item["gltf_node"]
        for item in exported.manifest["mappings"]["nodes"]
    }
    node_index = node_map[(body.file_index, body.path_id)]
    gltf_node = document["nodes"][node_index]
    assert "skin" in gltf_node
    skin = document["skins"][gltf_node["skin"]]
    native = environment.studio.read_mesh(*body.mesh)
    bones = tuple(body.bones)
    assert len(skin["joints"]) == len(bones) == len(native.bind_poses) // 64
    assert any(
        (item["file_index"], item["path_id"]) == (body.file_index, body.path_id)
        and item["weights_available"]
        and item["skin_attached"]
        for item in exported.manifest["mappings"]["skins"]
    )
    positions = document["meshes"][gltf_node["mesh"]]["primitives"]
    raw_weights = list(struct.iter_unpack("<4f", bytes(native.skin_weights)))
    raw_joints = list(struct.iter_unpack("<4I", bytes(native.skin_bone_indices)))
    assert len(raw_weights) == len(raw_joints) == native.vertex_count
    signs = (-1.0, 1.0, 1.0, 1.0)
    bindposes = list(struct.iter_unpack("<16f", bytes(native.bind_poses)))
    for primitive in positions:
        attrs = primitive["attributes"]
        assert "skin" not in primitive
        primitive_positions = _accessor(document, binary, attrs["POSITION"])
        joints = _accessor(document, binary, attrs["JOINTS_0"])
        weights = _accessor(document, binary, attrs["WEIGHTS_0"])
        assert len(joints) == len(weights) == len(primitive_positions) == native.vertex_count
        for index, (joint_row, weight_row) in enumerate(zip(joints, weights, strict=True)):
            assert all(math.isfinite(weight) and weight >= 0 for weight in weight_row)
            assert sum(weight_row) == pytest.approx(1.0, abs=1e-5)
            assert all(joint < len(skin["joints"]) for joint in joint_row)
            raw_total = sum(raw_weights[index])
            expected_joints = tuple(
                joint if weight > 0 else 0
                for joint, weight in zip(raw_joints[index], raw_weights[index], strict=True)
            )
            assert joint_row == expected_joints and weight_row == pytest.approx(
                tuple(w / raw_total for w in raw_weights[index])
            )
    ibms = _accessor(document, binary, skin["inverseBindMatrices"])
    assert len(ibms) == len(bones)
    for actual, source in zip(ibms, bindposes, strict=True):
        expected = tuple(value * signs[i % 4] * signs[i // 4] for i, value in enumerate(source))
        assert actual == pytest.approx(expected, abs=1e-5)
    _assert_cpu_skin_samples(
        environment, table, subtree, body, skin, native, document, binary, node_map
    )


def _acceptance(variable, root_variable, default_root, kind):
    inputs = _inputs(variable)
    root_name = os.environ.get(root_variable, default_root)
    environment = unity_rs_adapter.load_collection(inputs, _UNITY_VERSION)
    table = unity_rs_adapter.build_collection_file_table(environment)
    scene_nodes = environment.studio.scene()
    roots = [node for node in scene_nodes if node.name == root_name and node.parent is None]
    assert len(roots) == 1, f"expected one root named {root_name!r}"
    root = roots[0]
    extraction = _package(inputs, environment, table, root, kind, kind)
    subtree = select_root_subtree(scene_nodes, (root.file_index, root.path_id))
    first = export_root_glb(environment, table, extraction, (root.file_index, root.path_id))
    second = export_root_glb(environment, table, extraction, (root.file_index, root.path_id))
    assert first.glb_bytes == second.glb_bytes
    document = validate_glb_bytes(first.glb_bytes)
    assert first.manifest["node_count"] == len(document["nodes"])
    binary = _binary(first.glb_bytes)
    _assert_geometry(environment, subtree, first, document, binary)
    return environment, table, subtree, first, document, binary


def test_real_stage_collection_exports_mesh_materials_and_textures() -> None:
    _acceptance(
        "SEKAI_GLB_COLLECTION",
        "SEKAI_GLB_ROOT_NAME",
        "svl_mdl_stage_007_sp_live_kyoutu_stage",
        "stage",
    )


def test_real_character_collection_exports_native_skin_and_bindposes() -> None:
    environment, table, subtree, exported, document, binary = _acceptance(
        "SEKAI_GLB_CHARACTER_COLLECTION",
        "SEKAI_GLB_CHARACTER_ROOT_NAME",
        "mdl_bdy_COM_41",
        "character",
    )
    _assert_character_skin(environment, table, subtree, exported, document, binary)
