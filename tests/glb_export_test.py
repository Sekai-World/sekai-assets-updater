"""GLB Phase 4 tests: deterministic root-scoped GLB export (#38)."""

from __future__ import annotations

import hashlib
import struct
from types import SimpleNamespace

import pytest

from updater import unity_rs_adapter
from updater.extract.glb import (
    CONVERSION_RULES,
    SCENE3D_PURPOSE,
    BundleRef,
    ExtractionPlan,
    PackageNotPublishable,
    RootNotFoundError,
    RootRef,
    content_hash,
    export_root_glb,
    load_export_manifest,
    load_package_collection,
    load_package_manifest,
    parse_obj_mesh,
    select_root_subtree,
    validate_glb_bytes,
)
from updater.extract.glb.gltf_writer import (
    BIN_CHUNK_MAGIC,
    GLB_MAGIC,
    GLB_VERSION,
    JSON_CHUNK_MAGIC,
    GlbScene,
)
from updater.extract.sync_worker import export_root_glb_sync

STAGE_BUNDLE = "stage/root_stage"
SHADER_BUNDLE = "shader/common_pack"
STAGE_CAB = "CAB-a"
SHADER_CAB = "CAB-b"
ROOT_CONTAINER = "stage/root_stage#root_a"

TRIANGLE_OBJ = b"""g tri
v 1 0 0
v 0 0 0
v 0 1 0
vt 0 0
vt 1 0
vt 0 1
vn 0 0 -1
f 1/1/1 2/2/1 3/3/1
"""

SHARED_VERT_OBJ = b"""g quad
v 0 0 0
v 1 0 0
v 1 1 0
v 0 1 0
vt 0 0
vt 1 0
vt 1 1
vt 0 1
vn 0 0 -1
f 1/1/1 2/2/1 3/3/1
f 1/1/1 3/3/1 4/4/1
"""


def _plan(*, with_shader_bundle: bool = True) -> ExtractionPlan:
    bundles = [BundleRef(bundle_name=STAGE_BUNDLE)]
    if with_shader_bundle:
        bundles.append(BundleRef(bundle_name=SHADER_BUNDLE))
    return ExtractionPlan(
        package_id="streaming_live/0006_lon_vbs_01/stage",
        purpose=SCENE3D_PURPOSE,
        profile="scene3d",
        roots=(
            RootRef(
                root_id="stage_root",
                kind="stage",
                bundle_name=STAGE_BUNDLE,
                container_path=ROOT_CONTAINER,
            ),
        ),
        bundles=tuple(bundles),
    )


class _Info:
    def __init__(
        self,
        *,
        file_index: int,
        path_id: int,
        class_id: int,
        name: str | None = None,
        container: str | None = None,
        source_path: str = "",
    ) -> None:
        self.file_index = file_index
        self.object_index = 0
        self.path_id = path_id
        self.class_id = class_id
        self.name = name
        self.container = container
        self.source_path = source_path


def _node(
    name, file_index, path_id, *, parent=None, mesh=None, position=None, rotation=None, scale=None
):
    return SimpleNamespace(
        name=name,
        file_index=file_index,
        path_id=path_id,
        parent=parent,
        children=[],
        mesh=mesh,
        materials=[],
        bones=[],
        animator=None,
        local_position=position,
        local_rotation=rotation,
        local_scale=scale,
    )


class _ExportStudio:
    """Fake binding studio with a scene graph and OBJ mesh payloads."""

    def __init__(self, *, include_shader_pack: bool = True) -> None:
        stage_source = f"{STAGE_BUNDLE}::{STAGE_CAB}"
        shader_source = f"{SHADER_BUNDLE}::{SHADER_CAB}"
        self._files = [
            SimpleNamespace(index=0, path=stage_source, unity_version="2022.3.21f1"),
        ]
        self._objects = [
            _Info(
                file_index=0,
                path_id=1,
                class_id=142,
                name=STAGE_BUNDLE,
                container=ROOT_CONTAINER,
                source_path=stage_source,
            ),
            _Info(
                file_index=0,
                path_id=100,
                class_id=1,
                name="root_a",
                container=ROOT_CONTAINER,
                source_path=stage_source,
            ),
            _Info(file_index=0, path_id=101, class_id=1, name="child_a", source_path=stage_source),
            _Info(file_index=0, path_id=102, class_id=43, name="mesh_a", source_path=stage_source),
            _Info(file_index=0, path_id=110, class_id=1, name="root_b", source_path=stage_source),
            _Info(file_index=0, path_id=111, class_id=43, name="mesh_b", source_path=stage_source),
            _Info(file_index=0, path_id=120, class_id=4, source_path=stage_source),
        ]
        self._typetrees = {
            (0, 1): {"m_Name": STAGE_BUNDLE},
            (0, 100): {"m_Name": "root_a", "m_Shader": {"m_FileID": 1, "m_PathID": 200}},
            (0, 101): {"m_Name": "child_a"},
            (0, 102): {"m_Name": "mesh_a"},
            (0, 110): {"m_Name": "root_b"},
            (0, 111): {"m_Name": "mesh_b"},
            (0, 120): {
                "m_GameObject": {"m_FileID": 0, "m_PathID": 100},
                "m_Father": {"m_FileID": 0, "m_PathID": 0},
            },
        }
        self._dependencies = {0: [SHADER_BUNDLE]}
        self._containers = {0: [(ROOT_CONTAINER, 0, 1, (0, 100))]}
        if include_shader_pack:
            self._files.append(
                SimpleNamespace(index=1, path=shader_source, unity_version="2022.3.21f1")
            )
            self._objects.append(
                _Info(
                    file_index=1,
                    path_id=1,
                    class_id=142,
                    name=SHADER_BUNDLE,
                    source_path=shader_source,
                )
            )
            self._objects.append(
                _Info(
                    file_index=1,
                    path_id=200,
                    class_id=48,
                    name="shader_lit",
                    source_path=shader_source,
                )
            )
            self._typetrees[(1, 1)] = {"m_Name": SHADER_BUNDLE}
            self._typetrees[(1, 200)] = {"m_ParsedForm": {}}
            self._dependencies[1] = []
            self._containers[1] = []
        self._scene = [
            _node("root_a", 0, 100, rotation=(0.1, 0.2, 0.3, 0.9), scale=(1.0, 1.0, 1.0)),
            _node("child_a", 0, 101, parent=(0, 100), mesh=(0, 102), position=(1.0, 2.0, 3.0)),
            _node("root_b", 0, 110, mesh=(0, 111)),
        ]
        self._mesh_obj = {102: SHARED_VERT_OBJ, 111: TRIANGLE_OBJ}

    def files(self):
        return list(self._files)

    def objects(self):
        return iter(self._objects)

    def scene(self, *, limits=None):
        return list(self._scene)

    def read_asset_bundle(self, file_index: int, _path_id: int):
        return SimpleNamespace(
            container=self._containers.get(file_index, []),
            dependencies=self._dependencies.get(file_index, []),
        )

    def read_type_tree_json(self, file_index: int, path_id: int):
        import orjson

        return orjson.dumps(self._typetrees[(file_index, path_id)])

    def read_mesh_obj(self, _file_index: int, path_id: int, **_kwargs):
        return self._mesh_obj[path_id]

    def read_game_object_fbx(self, _file_index: int, _path_id: int, **_kwargs):
        return b"FBX diagnostic bytes"


def _environment(include_shader_pack: bool = True):
    return unity_rs_adapter.UnityRsEnvironment(
        _ExportStudio(include_shader_pack=include_shader_pack)
    )


def _extraction(environment):
    from updater.extract.glb.collection import (
        GlbCollectionExtraction,
        build_package_manifest,
        validate_collection_reachability,
    )

    table = unity_rs_adapter.build_collection_file_table(environment)
    plan = _plan()
    payloads = [(STAGE_BUNDLE, b"stage"), (SHADER_BUNDLE, b"shader")]
    report = validate_collection_reachability(environment, table, plan.roots)
    manifest = build_package_manifest(plan, payloads, "2022.3.21f1", report, environment, table)
    return GlbCollectionExtraction(manifest=manifest, report=report), table


ROOT_IDENTITY = (0, 100)


def test_glb_writer_is_deterministic_and_structurally_valid() -> None:
    def build():
        scene = GlbScene()
        material = scene.add_default_material()
        mesh = scene.add_mesh(
            name="tri",
            positions=[(-1.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            normals=[(0.0, 0.0, -1.0)] * 3,
            texcoords=[(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)],
            indices=[0, 2, 1],
            material_index=material,
        )
        root = scene.add_node({"name": "root", "mesh": mesh})
        return scene.encode(root_nodes=[root], generator="test")

    first = build()
    second = build()
    assert first == second
    magic, version, total = struct.unpack_from("<III", first, 0)
    assert magic == GLB_MAGIC and version == GLB_VERSION and total == len(first)
    document = validate_glb_bytes(first)
    assert document["asset"]["version"] == "2.0"
    assert document["meshes"][0]["primitives"][0]["material"] == 0
    targets = {view["target"] for view in document["bufferViews"]}
    assert targets == {34962, 34963}  # ARRAY_BUFFER + ELEMENT_ARRAY_BUFFER


@pytest.mark.parametrize(
    "corruption",
    ["truncate", "bad_magic", "bad_length", "unknown_child"],
)
def test_validate_glb_bytes_rejects_corruption(corruption: str) -> None:
    scene = GlbScene()
    root = scene.add_node({"name": "root"})
    scene.attach_children(root, [3])  # dangling child
    data = scene.encode(root_nodes=[root], generator="test")

    if corruption == "truncate":
        with pytest.raises(ValueError, match="too short"):
            validate_glb_bytes(data[:8])
    elif corruption == "bad_magic":
        with pytest.raises(ValueError, match="not a GLB"):
            validate_glb_bytes(b"NOPE" + data[4:])
    elif corruption == "bad_length":
        with pytest.raises(ValueError, match="does not match"):
            validate_glb_bytes(data[:-1])
    else:
        with pytest.raises(ValueError, match="unknown child"):
            validate_glb_bytes(data)


def test_json_and_bin_chunk_magicians_are_declared() -> None:
    assert JSON_CHUNK_MAGIC == 0x4E4F534A
    assert BIN_CHUNK_MAGIC == 0x004E4942


def test_parse_obj_mesh_mirrors_dedups_and_reverses_winding() -> None:
    geometry = parse_obj_mesh(TRIANGLE_OBJ)

    # X-mirrored positions, first-seen corner order preserved.
    assert geometry["positions"] == [(-1.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)]
    assert geometry["normals"] == [(0.0, 0.0, -1.0)] * 3  # one entry per corner
    assert geometry["texcoords"] == [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    # (a, b, c) -> (a, c, b)
    assert geometry["indices"] == [0, 2, 1]


def test_parse_obj_mesh_deduplicates_shared_corners() -> None:
    geometry = parse_obj_mesh(SHARED_VERT_OBJ)

    assert len(geometry["positions"]) == 4
    # Two triangles, each reversed; corner 0 is shared and emitted once.
    assert geometry["indices"][:3] == [0, 2, 1]
    assert geometry["indices"][3:] == [0, 3, 2]


def test_select_root_subtree_excludes_unrelated_roots_deterministically() -> None:
    scene_nodes = _ExportStudio().scene()
    subtree = select_root_subtree(scene_nodes, ROOT_IDENTITY)

    assert [(n.file_index, n.path_id) for n in subtree] == [(0, 100), (0, 101)]
    assert all(n.name != "root_b" for n in subtree)

    again = select_root_subtree(list(reversed(scene_nodes)), ROOT_IDENTITY)
    assert [n.path_id for n in again] == [100, 101]

    with pytest.raises(RootNotFoundError):
        select_root_subtree(scene_nodes, (9, 9))


def test_export_root_glb_embeds_rules_mappings_and_hash() -> None:
    environment = _environment()
    extraction, table = _extraction(environment)

    export = export_root_glb(environment, table, extraction, ROOT_IDENTITY)

    document = validate_glb_bytes(export.glb_bytes)
    assert export.manifest["conversion_rules"] == CONVERSION_RULES
    assert export.manifest["content_hash"] == content_hash(export.glb_bytes)
    assert export.manifest["glb_byte_length"] == len(export.glb_bytes)
    assert export.manifest["node_count"] == 2
    assert export.manifest["mesh_count"] == 1
    assert export.manifest["roots"] == [
        {"file_index": 0, "path_id": 100, "bundle_name": STAGE_BUNDLE}
    ]
    nodes = export.manifest["mappings"]["nodes"]
    assert [m["hierarchy_path"] for m in nodes] == ["root_a", "root_a/child_a"]
    assert export.manifest["mappings"]["meshes"] == [
        {
            "bundle_name": STAGE_BUNDLE,
            "file_index": 0,
            "path_id": 102,
            "gltf_mesh": 0,
            "gltf_node": 1,
            "hierarchy_path": "root_a/child_a",
        }
    ]
    # Unrelated root stays out of the artifact.
    assert all(m["path_id"] not in (110, 111) for m in nodes)
    gltf_nodes = document["nodes"]
    assert gltf_nodes[0]["rotation"] == [0.1, -0.2, -0.3, 0.9]
    assert gltf_nodes[1]["translation"] == [-1.0, 2.0, 3.0]
    assert document["materials"][0]["name"] == "stage_default"


def test_export_root_glb_is_byte_deterministic() -> None:
    first_env = _environment()
    second_env = _environment()
    first_extraction, first_table = _extraction(first_env)
    second_extraction, second_table = _extraction(second_env)

    first = export_root_glb(first_env, first_table, first_extraction, ROOT_IDENTITY)
    second = export_root_glb(second_env, second_table, second_extraction, ROOT_IDENTITY)

    assert first.glb_bytes == second.glb_bytes
    assert first.manifest == second.manifest


def test_export_refuses_non_publishable_package() -> None:
    environment = _environment(include_shader_pack=False)
    extraction, table = _extraction(environment)
    assert not extraction.report.publishable

    with pytest.raises(ValueError, match="required L2"):
        export_root_glb(environment, table, extraction, ROOT_IDENTITY)


def test_export_root_glb_sync_publishes_all_artifacts(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from updater.extract.glb import collection as glb_collection

    plan = _plan()
    payloads = [(STAGE_BUNDLE, b"stage"), (SHADER_BUNDLE, b"shader")]
    monkeypatch.setattr(
        glb_collection,
        "load_collection",
        lambda _payloads, _version: _environment(include_shader_pack=True),
    )
    glb_path = tmp_path / "out" / "stage.glb"
    export_manifest_path = tmp_path / "out" / "stage.export.json"
    package_manifest_path = tmp_path / "out" / "stage.package.json"

    export = export_root_glb_sync(
        plan,
        payloads,
        "2022.3.21f1",
        str(package_manifest_path),
        ROOT_IDENTITY,
        str(glb_path),
        str(export_manifest_path),
    )

    assert glb_path.read_bytes() == export.glb_bytes
    assert validate_glb_bytes(glb_path.read_bytes())
    stored = load_export_manifest(export_manifest_path)
    assert stored == export.manifest
    assert stored["content_hash"] == hashlib.sha256(glb_path.read_bytes()).hexdigest()
    assert load_package_manifest(package_manifest_path)["package_id"] == plan.package_id


def test_export_root_glb_sync_refuses_before_writing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from updater.extract.glb import collection as glb_collection

    plan = _plan(with_shader_bundle=False)
    payloads = [(STAGE_BUNDLE, b"stage")]
    monkeypatch.setattr(
        glb_collection,
        "load_collection",
        lambda _payloads, _version: _environment(include_shader_pack=False),
    )
    glb_path = tmp_path / "out" / "stage.glb"

    with pytest.raises(PackageNotPublishable):
        export_root_glb_sync(
            plan,
            payloads,
            "2022.3.21f1",
            str(tmp_path / "out" / "package.json"),
            ROOT_IDENTITY,
            str(glb_path),
            str(tmp_path / "out" / "stage.export.json"),
        )

    assert not glb_path.exists()


def test_export_root_glb_sync_writes_optional_fbx_diagnostic(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from updater.extract.glb import collection as glb_collection

    plan = _plan()
    payloads = [(STAGE_BUNDLE, b"stage"), (SHADER_BUNDLE, b"shader")]
    monkeypatch.setattr(
        glb_collection,
        "load_collection",
        lambda _payloads, _version: _environment(include_shader_pack=True),
    )
    fbx_path = tmp_path / "out" / "stage.fbx"

    export_root_glb_sync(
        plan,
        payloads,
        "2022.3.21f1",
        str(tmp_path / "out" / "package.json"),
        ROOT_IDENTITY,
        str(tmp_path / "out" / "stage.glb"),
        str(tmp_path / "out" / "stage.export.json"),
        include_fbx_diagnostic=True,
        fbx_path=str(fbx_path),
    )

    assert fbx_path.read_bytes() == b"FBX diagnostic bytes"


def test_load_package_collection_still_rejects_mismatched_payloads() -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="missing payloads"):
        load_package_collection(plan, [(STAGE_BUNDLE, b"stage")], "2022.3.21f1")


def _real_collection_inputs() -> list[tuple[str, bytes]]:
    import os
    from pathlib import Path as StdPath

    spec = os.environ.get("SEKAI_GLB_COLLECTION", "")
    if not spec:
        return []
    inputs: list[tuple[str, bytes]] = []
    for entry in spec.split(";"):
        name, _, path = entry.partition("=")
        payload = StdPath(path)
        if not payload.is_file():
            return []
        inputs.append((name, payload.read_bytes()))
    return inputs


def test_real_stage_export_is_root_scoped_and_deterministic() -> None:
    """Integration: export a real stage root (SEKAI_GLB_COLLECTION/SEKAI_GLB_ROOT_NAME)."""

    import os

    inputs = _real_collection_inputs()
    if not inputs:
        pytest.skip("SEKAI_GLB_COLLECTION not set or sample bundles missing")
    root_name = os.environ.get("SEKAI_GLB_ROOT_NAME", "svl_mdl_stage_007_sp_live_kyoutu_stage")

    environment = unity_rs_adapter.load_collection(inputs, "2022.3.21f1")
    table = unity_rs_adapter.build_collection_file_table(environment)
    scene_nodes = environment.studio.scene()
    root = next(node for node in scene_nodes if node.name == root_name and node.parent is None)

    plan = ExtractionPlan(
        package_id="streaming_live/0006_lon_vbs_01/stage",
        purpose=SCENE3D_PURPOSE,
        profile="scene3d",
        roots=(
            RootRef(
                root_id="stage",
                kind="stage",
                bundle_name=inputs[0][0],
                file_index=root.file_index,
                path_id=root.path_id,
            ),
        ),
        bundles=tuple(BundleRef(bundle_name=name) for name, _ in inputs),
    )
    from updater.extract.glb.collection import (
        GlbCollectionExtraction,
        build_package_manifest,
        validate_collection_reachability,
    )

    report = validate_collection_reachability(environment, table, plan.roots)
    manifest = build_package_manifest(plan, inputs, "2022.3.21f1", report, environment, table)
    extraction = GlbCollectionExtraction(manifest=manifest, report=report)

    # With only part of the closure cached, the required shader/light
    # externals stay unresolved: the publish gate must refuse.
    if not extraction.report.publishable:
        with pytest.raises(ValueError, match="required L2"):
            export_root_glb(environment, table, extraction, (root.file_index, root.path_id))
        export_kwargs = {"allow_incomplete": True}
    else:
        export_kwargs = {}

    first = export_root_glb(
        environment, table, extraction, (root.file_index, root.path_id), **export_kwargs
    )
    second = export_root_glb(
        environment, table, extraction, (root.file_index, root.path_id), **export_kwargs
    )

    assert first.glb_bytes == second.glb_bytes
    document = validate_glb_bytes(first.glb_bytes)
    assert first.manifest["node_count"] == len(document["nodes"])
    assert first.manifest["content_hash"] == content_hash(first.glb_bytes)
    assert first.manifest["mesh_count"] == len(document.get("meshes", []))
    assert first.manifest["node_count"] > 1
    # Root scoping: the artifact holds only this root's subtree.
    assert all(
        m["hierarchy_path"].split("/")[0] == root_name for m in first.manifest["mappings"]["nodes"]
    )
