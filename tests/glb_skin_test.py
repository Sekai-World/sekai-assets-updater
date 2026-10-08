"""GLB Phase 5 tests: skeleton extraction and skin-weight providers (#39)."""

from __future__ import annotations

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.glb_export_test import (
    ROOT_IDENTITY,
    STAGE_BUNDLE,
    _environment,
    _extraction,
)
from updater import unity_rs_adapter
from updater.extract.glb import (
    DIAG_MISSING_TARGET,
    DIAG_SKIN_WEIGHTS_UNAVAILABLE,
    export_root_glb,
    validate_glb_bytes,
)
from updater.extract.glb.math3d import IDENTITY4, compose_trs, invert_mat4, multiply_mat4
from updater.extract.glb.skin import (
    BindingSkinWeightsProvider,
    SkinWeights,
    _SingularJointMatrixError,
    build_skeleton,
)

STAGE_SOURCE = f"{STAGE_BUNDLE}::CAB-a"


def _transform_typetree(
    *, position, rotation=(0.0, 0.0, 0.0, 1.0), scale=(1.0, 1.0, 1.0), father=0, game_object
):
    return {
        "m_LocalPosition": {"x": position[0], "y": position[1], "z": position[2]},
        "m_LocalRotation": {"x": rotation[0], "y": rotation[1], "z": rotation[2], "w": rotation[3]},
        "m_LocalScale": {"x": scale[0], "y": scale[1], "z": scale[2]},
        "m_Father": {"m_FileID": 0, "m_PathID": father},
        "m_GameObject": {"m_FileID": 0, "m_PathID": game_object},
    }


class _SkinStudio:
    """Two transforms (hip -> knee) with GameObjects, plus a renderer node."""

    def __init__(self) -> None:
        self._files = [SimpleNamespace(index=0, path=STAGE_SOURCE, unity_version="2022.3.21f1")]
        self._objects = [
            SimpleNamespace(
                file_index=0,
                object_index=0,
                path_id=1,
                class_id=142,
                name=STAGE_BUNDLE,
                container=None,
                source_path=STAGE_SOURCE,
            ),
            SimpleNamespace(
                file_index=0,
                object_index=1,
                path_id=90,
                class_id=4,
                name=None,
                container=None,
                source_path=STAGE_SOURCE,
            ),
            SimpleNamespace(
                file_index=0,
                object_index=2,
                path_id=91,
                class_id=4,
                name=None,
                container=None,
                source_path=STAGE_SOURCE,
            ),
            SimpleNamespace(
                file_index=0,
                object_index=3,
                path_id=10,
                class_id=1,
                name="Hip",
                container=None,
                source_path=STAGE_SOURCE,
            ),
            SimpleNamespace(
                file_index=0,
                object_index=4,
                path_id=11,
                class_id=1,
                name="Knee",
                container=None,
                source_path=STAGE_SOURCE,
            ),
        ]
        self._typetrees = {
            (0, 1): {"m_Name": STAGE_BUNDLE},
            (0, 90): _transform_typetree(position=(1.0, 0.0, 0.0), game_object=10),
            (0, 10): {"m_Name": "Hip"},
            (0, 91): _transform_typetree(position=(0.0, 2.0, 0.0), father=90, game_object=11),
            (0, 11): {"m_Name": "Knee"},
        }

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


def _renderer(bones: list[tuple[int, int]]):
    return SimpleNamespace(name="Body", file_index=0, path_id=50, bones=bones)


def _skin_environment():
    return unity_rs_adapter.UnityRsEnvironment(_SkinStudio())


def test_build_skeleton_maps_bones_to_game_objects_and_composes_worlds() -> None:
    environment = _skin_environment()
    table = unity_rs_adapter.build_collection_file_table(environment)

    skeleton = build_skeleton(_renderer([(0, 90), (0, 91)]), [], environment, table)

    assert [joint.name for joint in skeleton.joints] == ["Hip", "Knee"]
    # Joint identities are GameObject identities (the exported scene nodes).
    assert [joint.identity for joint in skeleton.joints] == [(0, 10), (0, 11)]
    hip, knee = skeleton.joints
    # X-mirrored translations.
    assert hip.world_matrix[12:15] == (-1.0, 0.0, 0.0)
    assert knee.world_matrix[12:15] == (-1.0, 2.0, 0.0)
    # Child world = parent world * local.
    local = compose_trs((0.0, 2.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    expected = multiply_mat4(hip.world_matrix, local)
    for got, want in zip(knee.world_matrix, expected, strict=True):
        assert abs(got - want) < 1e-9
    # Inverse binds invert the world matrices.
    for joint in skeleton.joints:
        product = multiply_mat4(joint.world_matrix, joint.inverse_bind_matrix)
        for got, want in zip(product, IDENTITY4, strict=True):
            assert abs(got - want) < 1e-9
    assert skeleton.diagnostics == ()


def test_build_skeleton_reports_unresolvable_bones() -> None:
    environment = _skin_environment()
    table = unity_rs_adapter.build_collection_file_table(environment)

    skeleton = build_skeleton(_renderer([(0, 90), (0, 999)]), [], environment, table)

    assert [joint.name for joint in skeleton.joints] == ["Hip"]
    assert [diagnostic.code for diagnostic in skeleton.diagnostics] == [DIAG_MISSING_TARGET]


def test_build_skeleton_raises_on_singular_joint() -> None:
    studio = _SkinStudio()
    studio._objects.append(
        SimpleNamespace(
            file_index=0,
            object_index=9,
            path_id=92,
            class_id=4,
            name=None,
            container=None,
            source_path=STAGE_SOURCE,
        )
    )
    studio._objects.append(
        SimpleNamespace(
            file_index=0,
            object_index=10,
            path_id=12,
            class_id=1,
            name="Collapsed",
            container=None,
            source_path=STAGE_SOURCE,
        )
    )
    studio._typetrees[(0, 92)] = _transform_typetree(
        position=(0.0, 0.0, 0.0), scale=(0.0, 0.0, 0.0), game_object=12
    )
    studio._typetrees[(0, 12)] = {"m_Name": "Collapsed"}
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    table = unity_rs_adapter.build_collection_file_table(environment)

    with pytest.raises(_SingularJointMatrixError, match="singular"):
        build_skeleton(_renderer([(0, 92)]), [], environment, table)


def test_binding_provider_resolves_skeleton_but_marks_weights_unavailable() -> None:
    environment = _skin_environment()
    table = unity_rs_adapter.build_collection_file_table(environment)

    weights = BindingSkinWeightsProvider(environment, table).skin_weights(
        _renderer([(0, 90), (0, 91)]), []
    )

    assert isinstance(weights, SkinWeights)
    assert not weights.weights_available
    assert weights.vertex_joints is None and weights.vertex_weights is None
    assert [d.code for d in weights.diagnostics] == [DIAG_SKIN_WEIGHTS_UNAVAILABLE]
    assert "2 joints resolved" in weights.diagnostics[0].message
    assert len(weights.skeleton.joints) == 2


class _WeightedProvider:
    """Test provider supplying valid per-vertex weights."""

    def __init__(self, environment, table, *, vertex_count: int = 4) -> None:
        self.environment = environment
        self.table = table
        self.vertex_count = vertex_count

    def skin_weights(self, node, scene_nodes):
        skeleton = build_skeleton(node, scene_nodes, self.environment, self.table)
        return SkinWeights(
            skeleton=skeleton,
            vertex_joints=tuple((0, 0, 0, 0) for _ in range(self.vertex_count)),
            vertex_weights=tuple((1.0, 0.0, 0.0, 0.0) for _ in range(self.vertex_count)),
        )


def test_export_attaches_skin_from_provider() -> None:
    environment = _environment()
    studio = environment.studio
    # child_a (mesh node) becomes skinned through the stage's Transform,
    # whose GameObject is the exported root_a.
    studio._scene[1].bones = [(0, 120)]
    extraction, table = _extraction(environment)
    provider = _WeightedProvider(environment, table, vertex_count=4)

    export = export_root_glb(
        environment,
        table,
        extraction,
        ROOT_IDENTITY,
        skin_weights_provider=provider,
    )

    document = validate_glb_bytes(export.glb_bytes)
    skin = document["skins"][0]
    primitive = document["meshes"][0]["primitives"][0]
    assert primitive["skin"] == 0
    assert skin["joints"] == [0]  # root_a is glTF node 0
    ibm_view = document["accessors"][skin["inverseBindMatrices"]]
    assert ibm_view["type"] == "MAT4" and ibm_view["count"] == 1
    assert primitive["attributes"]["JOINTS_0"] >= 0
    assert primitive["attributes"]["WEIGHTS_0"] >= 0
    mapping = export.manifest["mappings"]["skins"]
    assert mapping == [
        {
            "bundle_name": STAGE_BUNDLE,
            "file_index": 0,
            "path_id": 101,
            "gltf_node": 1,
            "gltf_mesh": 0,
            "joints": 1,
            "weights_available": True,
        }
    ]
    assert export.manifest["diagnostics"] == []


def test_export_default_provider_keeps_skeleton_and_records_diagnostic() -> None:
    environment = _environment()
    environment.studio._scene[1].bones = [(0, 120)]
    extraction, table = _extraction(environment)

    export = export_root_glb(environment, table, extraction, ROOT_IDENTITY)

    document = validate_glb_bytes(export.glb_bytes)
    assert "skins" not in document  # no weights -> no skin attached
    assert "JOINTS_0" not in document["meshes"][0]["primitives"][0]["attributes"]
    assert export.manifest["mappings"]["skins"][0]["weights_available"] is False
    assert export.manifest["mappings"]["skins"][0]["joints"] == 1
    codes = [d["code"] for d in export.manifest["diagnostics"]]
    assert DIAG_SKIN_WEIGHTS_UNAVAILABLE in codes


def test_export_rejects_provider_weights_that_do_not_fit_the_mesh() -> None:
    environment = _environment()
    environment.studio._scene[1].bones = [(0, 120)]
    extraction, table = _extraction(environment)
    provider = _WeightedProvider(environment, table, vertex_count=3)

    export = export_root_glb(
        environment,
        table,
        extraction,
        ROOT_IDENTITY,
        skin_weights_provider=provider,
    )

    document = validate_glb_bytes(export.glb_bytes)
    assert "skins" not in document
    assert any("weight count" in d["message"] for d in export.manifest["diagnostics"])


def test_provider_inverse_binds_match_skeleton_world() -> None:
    """The IBM bytes written into a GLB invert the joint's mirrored world."""

    environment = _environment()
    studio = environment.studio
    studio._scene[1].bones = [(0, 120)]
    extraction, table = _extraction(environment)
    export = export_root_glb(
        environment,
        table,
        extraction,
        ROOT_IDENTITY,
        skin_weights_provider=_WeightedProvider(environment, table),
    )
    document = validate_glb_bytes(export.glb_bytes)

    skeleton = build_skeleton(studio._scene[1], [], environment, table)
    expected = invert_mat4(skeleton.joints[0].world_matrix)
    skin = document["skins"][0]
    accessor = document["accessors"][skin["inverseBindMatrices"]]
    assert accessor["componentType"] == 5126 and accessor["type"] == "MAT4"
    view = document["bufferViews"][accessor["bufferView"]]
    json_length, _magic = struct.unpack_from("<II", export.glb_bytes, 12)
    bin_start = 12 + 8 + json_length + 8
    values = struct.unpack_from("<16f", export.glb_bytes, bin_start + view["byteOffset"])
    for got, want in zip(values, expected, strict=True):
        assert abs(got - want) < 1e-6


def test_real_character_skeleton_resolves_when_env_set() -> None:
    """Integration: a real skinned character (SEKAI_GLB_SKIN=bundle=path).

    The binding's bones reference Transforms while the scene graph is
    GameObject keyed; every bone must resolve to a joint whose inverse bind
    matrix inverts its glTF-space world matrix.
    """

    import os

    spec = os.environ.get("SEKAI_GLB_SKIN", "")
    if not spec:
        pytest.skip("SEKAI_GLB_SKIN not set or sample bundle missing")
    name, _, raw_path = spec.partition("=")
    bundle = Path(raw_path)
    if not bundle.is_file():
        pytest.skip("SEKAI_GLB_SKIN sample bundle missing")

    environment = unity_rs_adapter.load_collection([(name, bundle.read_bytes())], "2022.3.21f1")
    table = unity_rs_adapter.build_collection_file_table(environment)
    scene = environment.studio.scene()
    skinned = [node for node in scene if getattr(node, "bones", None)]
    assert skinned, "expected at least one skinned node"

    for node in skinned:
        skeleton = build_skeleton(node, scene, environment, table)
        assert len(skeleton.joints) == len(node.bones)
        for joint in skeleton.joints:
            product = multiply_mat4(joint.world_matrix, joint.inverse_bind_matrix)
            for got, want in zip(product, IDENTITY4, strict=True):
                assert abs(got - want) < 1e-6
