"""Deterministic root-scoped GLB export for streaming Live packages (#38).

Only the selected root's subtree is exported — never the whole collection.
Conversion rules are fixed and recorded in every export manifest:

- Unity is left-handed Y-up with 1 unit = 1 meter; glTF is right-handed
  Y-up in meters, so the scene is mirrored on the X axis.
- translation ``(x, y, z) -> (-x, y, z)``; rotation quaternion
  ``(x, y, z, w) -> (x, -y, -z, w)``; scale is unchanged.
- normals mirror like positions; triangle winding is reversed to keep
  front faces after the mirror.
- node names are the Unity object names, kept verbatim.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Sequence

from updater.extract.glb.collection import (
    CollectionFileTable,
    GlbCollectionExtraction,
)
from updater.extract.glb.contracts import DIAG_MISSING_TARGET
from updater.extract.glb.gltf_writer import GlbScene, content_hash, validate_glb_bytes
from updater.extract.glb.materials import convert_material
from updater.extract.glb.math3d import mirror_position as _mirror_position
from updater.extract.glb.math3d import mirror_rotation as _mirror_rotation
from updater.extract.glb.skin import BindingSkinWeightsProvider, SkinWeightsProvider
from updater.unity_rs_adapter import UnityRsEnvironment

GENERATOR = "sekai-assets-updater glb-export/1"
MIRROR_X_ROTATION = "quaternion (x, y, z, w) -> (x, -y, -z, w)"
MIRROR_X_TRANSLATION = "vector (x, y, z) -> (-x, y, z)"

CONVERSION_RULES = {
    "coordinate_system": "unity-left-handed-y-up -> gltf-right-handed-y-up",
    "handedness_conversion": "mirror_x",
    "unit_scale": {"unity_to_meters": 1.0},
    "rotation": MIRROR_X_ROTATION,
    "translation": MIRROR_X_TRANSLATION,
    "normal": MIRROR_X_TRANSLATION,
    "winding": "reversed_after_mirror",
    "node_naming": "unity_object_name_verbatim",
    "materials": "converted unity materials (pbr/unlit) with embedded png textures",
    "skins": "mirrored skeleton joints and native per-vertex joint/weight channels",
}


class RootNotFoundError(ValueError):
    """The requested root identity is not part of the collection scene."""


@dataclass(frozen=True, slots=True)
class GlbExport:
    """One completed GLB artifact with its export manifest."""

    glb_bytes: bytes
    manifest: dict[str, Any]
    fbx_diagnostic: bytes | None = None


@dataclass(frozen=True, slots=True)
class _NodeRef:
    """Hashable identity wrapper for one scene node."""

    file_index: int
    path_id: int


def select_root_subtree(scene_nodes: Sequence[Any], root_identity: tuple[int, int]) -> list[Any]:
    """Return the root's subtree in deterministic BFS order.

    Children sort by ``(file_index, path_id)`` at every level so the node
    order — and therefore every GLB index below it — is stable regardless of
    the binding's scene() ordering.
    """

    by_identity = {(node.file_index, node.path_id): node for node in scene_nodes}
    root = by_identity.get(root_identity)
    if root is None:
        raise RootNotFoundError(f"root {root_identity} is not part of the collection scene")
    children_map: dict[_NodeRef, list[Any]] = {}
    for node in scene_nodes:
        parent = node.parent
        if parent is None:
            continue
        children_map.setdefault(_NodeRef(*parent), []).append(node)
    for siblings in children_map.values():
        siblings.sort(key=lambda node: (node.file_index, node.path_id))
    ordered: list[Any] = []
    queue: list[Any] = [root]
    seen: set[_NodeRef] = set()
    while queue:
        current = queue.pop(0)
        identity = _NodeRef(current.file_index, current.path_id)
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(current)
        queue.extend(children_map.get(identity, ()))
    return ordered


def parse_obj_mesh(obj_bytes: bytes) -> dict[str, Any]:
    """Decode the binding's OBJ emission into mirrored, deduplicated geometry.

    Faces are triangulated in reversed order so front faces survive the X
    mirror.  Unique ``(v, vt, vn)`` corners keep first-seen order, and the
    returned ``source_vertex_indices`` preserve the OBJ vertex identity for
    remapping provider skin channels across reordered corners and UV seams.
    """

    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    texcoords: list[tuple[float, float]] = []
    corner_index: dict[tuple[int, int, int], int] = {}
    out_positions: list[tuple[float, float, float]] = []
    out_normals: list[tuple[float, float, float]] = []
    out_uvs: list[tuple[float, float]] = []
    source_vertex_indices: list[int] = []
    indices: list[int] = []

    def _resolve_index(raw: str, count: int, channel: str) -> int:
        value = int(raw)
        if value == 0:
            raise ValueError(f"OBJ {channel} indices are one-based and cannot be zero")
        resolved = value - 1 if value > 0 else count + value
        if resolved < 0 or resolved >= count:
            raise ValueError(f"OBJ {channel} index {value} is out of range")
        return resolved

    def _vertex(raw: str) -> int:
        v_part, _, rest = raw.partition("/")
        vt_part, _, vn_part = rest.partition("/")
        if not v_part:
            raise ValueError("OBJ face corner is missing its position index")
        vi = _resolve_index(v_part, len(positions), "position")
        vti = _resolve_index(vt_part, len(texcoords), "texcoord") if vt_part else -1
        vni = _resolve_index(vn_part, len(normals), "normal") if vn_part else -1
        corner = (vi, vti, vni)
        known = corner_index.get(corner)
        if known is not None:
            return known
        out_positions.append(_mirror_position(positions[vi]))
        out_uvs.append(texcoords[vti] if vti >= 0 else (0.0, 0.0))
        out_normals.append(_mirror_position(normals[vni]) if vni >= 0 else (0.0, 1.0, 0.0))
        source_vertex_indices.append(vi)
        new_index = len(out_positions) - 1
        corner_index[corner] = new_index
        return new_index

    for line_number, line in enumerate(obj_bytes.decode("utf-8").splitlines(), start=1):
        parts = line.split()
        if not parts:
            continue
        try:
            if parts[0] == "v":
                positions.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == "vt":
                texcoords.append((float(parts[1]), float(parts[2])))
            elif parts[0] == "vn":
                normals.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == "f":
                if len(parts) < 4:
                    raise ValueError("OBJ face must contain at least three corners")
                corners = [_vertex(part) for part in parts[1:]]
            else:
                continue
        except (IndexError, ValueError) as exc:
            raise ValueError(f"invalid OBJ mesh data on line {line_number}: {exc}") from exc
        if parts[0] == "f":
            # Reversed winding: (a, b, c) becomes (a, c, b) after mirroring.
            for k in range(1, len(corners) - 1):
                indices.append(corners[0])
                indices.append(corners[k + 1])
                indices.append(corners[k])
    if any(
        not math.isfinite(value)
        for channel in (positions, normals, texcoords)
        for row in channel
        for value in row
    ):
        raise ValueError("OBJ mesh contains a non-finite vertex attribute")
    return {
        "positions": out_positions,
        "normals": out_normals,
        "texcoords": out_uvs,
        "indices": indices,
        "primitives": [{"indices": indices}],
        "source_vertex_indices": source_vertex_indices,
        "source_vertex_count": len(positions),
    }


def _native_attribute(
    attribute: Any,
    *,
    label: str,
    vertex_count: int,
    dimensions: tuple[int, ...],
    default: tuple[float, ...] | None = None,
) -> list[tuple[float, ...]]:
    if attribute is None:
        if default is None:
            raise ValueError(f"native mesh is missing required {label}")
        return [default] * vertex_count

    dimension = attribute.dimension
    if not isinstance(dimension, int) or dimension not in dimensions:
        raise ValueError(f"native mesh {label} dimension {dimension!r} is invalid")
    raw_data = attribute.data
    if not isinstance(raw_data, (bytes, bytearray, memoryview)):
        raise ValueError(f"native mesh {label} data must be bytes")
    data = bytes(raw_data)
    expected_length = vertex_count * dimension * 4
    if len(data) != expected_length:
        raise ValueError(
            f"native mesh {label} byte length {len(data)} does not match "
            f"{vertex_count} vertices x {dimension} float32 components"
        )
    rows = [tuple(values) for values in struct.iter_unpack(f"<{dimension}f", data)]
    if any(not math.isfinite(value) for row in rows for value in row):
        raise ValueError(f"native mesh {label} contains a non-finite value")
    return rows


def _parse_native_mesh(mesh: Any) -> dict[str, Any]:
    """Convert unity-rs native vertex channels and submeshes to GLB space."""

    vertex_count = mesh.vertex_count
    if not isinstance(vertex_count, int) or vertex_count <= 0:
        raise ValueError(f"native mesh vertex_count must be positive, got {vertex_count!r}")

    positions = _native_attribute(
        mesh.positions, label="positions", vertex_count=vertex_count, dimensions=(3,)
    )
    normals = _native_attribute(
        mesh.normals,
        label="normals",
        vertex_count=vertex_count,
        dimensions=(3,),
        default=(0.0, 1.0, 0.0),
    )
    uv_channels = mesh.uvs
    uv0 = uv_channels[0] if uv_channels else None
    texcoords_raw = _native_attribute(
        uv0,
        label="uv0",
        vertex_count=vertex_count,
        dimensions=(2, 3, 4),
        default=(0.0, 0.0),
    )
    texcoords = [(row[0], row[1]) for row in texcoords_raw]

    native_submeshes = list(mesh.sub_meshes)
    if not native_submeshes:
        raise ValueError("native mesh has no submeshes")
    primitives: list[dict[str, Any]] = []
    for submesh_index, submesh in enumerate(native_submeshes):
        index_count = submesh.index_count
        if not isinstance(index_count, int) or index_count < 0 or index_count % 3:
            raise ValueError(
                f"native mesh submesh {submesh_index} index_count must be a non-negative "
                "triangle-list length"
            )
        raw_indices = submesh.indices
        if not isinstance(raw_indices, (bytes, bytearray, memoryview)):
            raise ValueError(f"native mesh submesh {submesh_index} indices must be bytes")
        raw_indices = bytes(raw_indices)
        if len(raw_indices) != index_count * 4:
            raise ValueError(
                f"native mesh submesh {submesh_index} index byte length "
                f"{len(raw_indices)} does not match index_count {index_count}"
            )
        native_indices = [values[0] for values in struct.iter_unpack("<I", raw_indices)]
        if any(index >= vertex_count for index in native_indices):
            raise ValueError(f"native mesh submesh {submesh_index} index exceeds vertex_count")
        indices: list[int] = []
        for offset in range(0, len(native_indices), 3):
            a, b, c = native_indices[offset : offset + 3]
            indices.extend((a, c, b))
        primitives.append({"indices": indices})

    return {
        "positions": [_mirror_position(tuple(position)) for position in positions],
        "normals": [_mirror_position(tuple(normal)) for normal in normals],
        "texcoords": texcoords,
        "primitives": primitives,
        "source_vertex_indices": list(range(vertex_count)),
        "source_vertex_count": vertex_count,
    }


def _hierarchy_path(nodes: dict[_NodeRef, Any], current: Any, root_identity: _NodeRef) -> str:
    names: list[str] = []
    identity = _NodeRef(current.file_index, current.path_id)
    while True:
        node = nodes.get(identity)
        if node is None:
            break
        names.append(node.name or f"node_{identity.path_id}")
        if identity == root_identity:
            break
        parent = node.parent
        if parent is None:
            break
        identity = _NodeRef(*parent)
    return "/".join(reversed(names))


class _MaterialBinder:
    """Converts and embeds materials for one export, deduplicated by identity
    and texture content hash."""

    def __init__(
        self,
        scene: GlbScene,
        environment: UnityRsEnvironment,
        table: CollectionFileTable,
        identity_to_bundle: dict[int, str],
    ) -> None:
        self._scene = scene
        self._environment = environment
        self._table = table
        self._identity_to_bundle = identity_to_bundle
        self._by_identity: dict[tuple[int, int], int] = {}
        self._textures_by_hash: dict[str, int] = {}
        self.diagnostics: list[dict[str, Any]] = []
        self.mappings: list[dict[str, Any]] = []

    def index_for(self, material_ptr: tuple[int, int]) -> int | None:
        identity = (material_ptr[0], material_ptr[1])
        known = self._by_identity.get(identity)
        if known is not None:
            return known
        material_object = self._environment.object_by_identity(*identity)
        if material_object is None:
            self._append_diagnostic(
                DIAG_MISSING_TARGET,
                f"material {identity} is not part of the collection",
                identity,
            )
            return None
        conversion = convert_material(self._environment, self._table, material_object)
        material_index = self._scene.add_material(conversion.gltf_material)
        for embed in conversion.textures:
            key = content_hash(embed.png_bytes)
            texture_index = self._textures_by_hash.get(key)
            if texture_index is None:
                texture_index = self._scene.add_texture(self._scene.add_image(embed.png_bytes))
                self._textures_by_hash[key] = texture_index
            self._scene.bind_material_texture(material_index, embed.slot, texture_index)
        self._by_identity[identity] = material_index
        for diagnostic in conversion.diagnostics:
            self.diagnostics.append(
                {
                    "code": diagnostic.code,
                    "message": diagnostic.message,
                    "source": self._source(identity),
                }
            )
        self.mappings.append(
            {
                "bundle_name": self._identity_to_bundle.get(identity[0]),
                "file_index": identity[0],
                "path_id": identity[1],
                "gltf_material": material_index,
                "texture_count": len(conversion.textures),
                "source_name": conversion.source_name,
            }
        )
        return material_index

    def _append_diagnostic(self, code: str, message: str, identity: tuple[int, int]) -> None:
        self.diagnostics.append(
            {"code": code, "message": message, "source": self._source(identity)}
        )

    def _source(self, identity: tuple[int, int]) -> dict[str, Any]:
        return {
            "bundle_name": self._identity_to_bundle.get(identity[0]),
            "file_index": identity[0],
            "path_id": identity[1],
        }


def export_root_glb(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    extraction: GlbCollectionExtraction,
    root_identity: tuple[int, int],
    *,
    bundle_name_for_file: dict[int, str] | None = None,
    include_fbx_diagnostic: bool = False,
    allow_incomplete: bool = False,
    skin_weights_provider: SkinWeightsProvider | None = None,
) -> GlbExport:
    """Export one root's subtree as a deterministic GLB artifact.

    ``extraction`` is the validated package from Phase 3; its report must be
    publishable unless ``allow_incomplete`` marks the artifact as a local
    diagnostic (the publish path in sync_worker never passes it — required
    L2 failures make a package non-publishable).  The resulting manifest
    embeds the conversion rules, the GLB content hash, per-node and
    per-mesh Unity mappings, converted materials, and any structured
    material/skin diagnostics. ``skin_weights_provider`` defaults to the
    binding-backed provider, which reads native mesh joint/weight channels.
    """

    if not extraction.report.publishable and not allow_incomplete:
        raise ValueError("refusing to export a package with required L2 findings")
    scene_nodes = environment.studio.scene()
    subtree = select_root_subtree(scene_nodes, root_identity)
    root_ref = _NodeRef(*root_identity)
    identity_to_bundle = bundle_name_for_file or {
        identity.file_index: identity.bundle_name for identity in table.identities
    }
    provider = skin_weights_provider or BindingSkinWeightsProvider(environment, table)

    scene = GlbScene()
    binder = _MaterialBinder(scene, environment, table, identity_to_bundle)
    material_index = scene.add_default_material()
    node_index: dict[_NodeRef, int] = {}
    node_mappings: list[dict[str, Any]] = []
    mesh_mappings: list[dict[str, Any]] = []
    skinned_nodes: list[tuple[Any, _NodeRef, int, list[int], int]] = []
    parents: dict[_NodeRef, Any] = {
        _NodeRef(node.file_index, node.path_id): node for node in scene_nodes
    }
    ordered_identities = [_NodeRef(node.file_index, node.path_id) for node in subtree]
    for node, identity in zip(subtree, ordered_identities, strict=True):
        gltf_node: dict[str, Any] = {"name": node.name or ""}
        if node.local_position is not None:
            gltf_node["translation"] = list(_mirror_position(node.local_position))
        if node.local_rotation is not None:
            gltf_node["rotation"] = list(_mirror_rotation(node.local_rotation))
        if node.local_scale is not None:
            gltf_node["scale"] = list(node.local_scale)
        if node.mesh is not None:
            mesh_identity = _NodeRef(*node.mesh)
            mesh_object = environment.object_by_identity(
                mesh_identity.file_index, mesh_identity.path_id
            )
            if mesh_object is not None:
                native_reader = getattr(environment.studio, "read_mesh", None)
                if callable(native_reader):
                    # A malformed native mesh must fail closed; silently
                    # falling back to OBJ would drop its vertex identity.
                    native_mesh = native_reader(mesh_identity.file_index, mesh_identity.path_id)
                    geometry = _parse_native_mesh(native_mesh)
                else:
                    obj_reader = getattr(environment.studio, "read_mesh_obj", None)
                    if not callable(obj_reader):
                        raise ValueError("studio exposes neither read_mesh nor read_mesh_obj")
                    geometry = parse_obj_mesh(
                        obj_reader(mesh_identity.file_index, mesh_identity.path_id)
                    )

                node_materials = tuple(node.materials or ())
                mesh_primitives: list[dict[str, Any]] = []
                for slot, source_primitive in enumerate(geometry["primitives"]):
                    if slot < len(node_materials) and node_materials[slot] is not None:
                        resolved_material = binder.index_for(node_materials[slot])
                    else:
                        resolved_material = None
                        binder._append_diagnostic(
                            DIAG_MISSING_TARGET,
                            f"mesh {mesh_identity.path_id} submesh {slot} has no material slot; "
                            "using the default material",
                            (identity.file_index, identity.path_id),
                        )
                    mesh_primitives.append(
                        {
                            "indices": source_primitive["indices"],
                            "material_index": (
                                material_index if resolved_material is None else resolved_material
                            ),
                        }
                    )
                hierarchy_path = _hierarchy_path(parents, node, root_ref)
                mesh_index = scene.add_mesh(
                    name=mesh_object.name or f"mesh_{mesh_identity.path_id}",
                    positions=geometry["positions"],
                    normals=geometry["normals"],
                    texcoords=geometry["texcoords"],
                    primitives=mesh_primitives,
                )
                gltf_node["mesh"] = mesh_index
                mesh_mappings.append(
                    {
                        "bundle_name": identity_to_bundle.get(mesh_identity.file_index),
                        "file_index": mesh_identity.file_index,
                        "path_id": mesh_identity.path_id,
                        "gltf_mesh": mesh_index,
                        "gltf_node": scene.node_count,
                        "hierarchy_path": hierarchy_path,
                    }
                )
                if node.bones:
                    skinned_nodes.append(
                        (
                            node,
                            identity,
                            mesh_index,
                            geometry["source_vertex_indices"],
                            geometry["source_vertex_count"],
                        )
                    )
        index = scene.add_node(gltf_node)
        node_index[identity] = index
        node_mappings.append(
            {
                "bundle_name": identity_to_bundle.get(identity.file_index),
                "file_index": identity.file_index,
                "path_id": identity.path_id,
                "gltf_node": index,
                "hierarchy_path": _hierarchy_path(parents, node, root_ref),
            }
        )
    # Wire children after all nodes exist so GLB child indices ascend.
    children_map: dict[_NodeRef, list[int]] = {}
    for node, identity in zip(subtree, ordered_identities, strict=True):
        parent = node.parent
        if parent is None:
            continue
        parent_identity = _NodeRef(*parent)
        if parent_identity in node_index:
            children_map.setdefault(parent_identity, []).append(node_index[identity])
    root_nodes: list[int] = []
    for node, identity in zip(subtree, ordered_identities, strict=True):
        parent = node.parent
        is_root = parent is None or _NodeRef(*parent) not in node_index
        children = sorted(children_map.get(identity, ()))
        if children:
            scene.attach_children(node_index[identity], children)
        if is_root:
            root_nodes.append(node_index[identity])
    root_nodes.sort()

    skin_mappings, skin_diagnostics = _attach_skins(
        scene,
        provider,
        scene_nodes,
        skinned_nodes,
        node_index,
        identity_to_bundle,
    )

    glb_bytes = scene.encode(root_nodes=root_nodes, generator=GENERATOR)
    validate_glb_bytes(glb_bytes)

    manifest: dict[str, Any] = {
        "export_version": 1,
        "package_id": extraction.manifest["package_id"],
        "collection_id": extraction.manifest["collection_id"],
        "roots": [
            {
                "file_index": root_identity[0],
                "path_id": root_identity[1],
                "bundle_name": identity_to_bundle.get(root_identity[0]),
            }
        ],
        "conversion_rules": CONVERSION_RULES,
        "content_hash": content_hash(glb_bytes),
        "glb_byte_length": len(glb_bytes),
        "node_count": len(subtree),
        "mesh_count": len(mesh_mappings),
        "mappings": {
            "nodes": node_mappings,
            "meshes": mesh_mappings,
            "materials": binder.mappings,
            "skins": skin_mappings,
        },
        "diagnostics": binder.diagnostics + skin_diagnostics,
        "report_summary": extraction.report.summary(),
    }
    fbx_bytes: bytes | None = None
    if include_fbx_diagnostic:
        fbx_bytes = environment.studio.read_game_object_fbx(
            root_identity[0], root_identity[1], include_animations=False
        )
    return GlbExport(glb_bytes=glb_bytes, manifest=manifest, fbx_diagnostic=fbx_bytes)


def _attach_skins(
    scene: GlbScene,
    provider: SkinWeightsProvider,
    scene_nodes: Sequence[Any],
    skinned_nodes: Sequence[tuple[Any, _NodeRef, int, list[int], int]],
    node_index: dict[_NodeRef, int],
    identity_to_bundle: dict[int, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve and attach skins for the subtree's skinned nodes.

    Every provider diagnostic is recorded.  A skin is attached only when the
    provider supplied per-vertex weights, every joint is inside the exported
    subtree, and the source-order weight count matches the original mesh;
    otherwise the skeleton's nodes remain in the GLB and the reason becomes a
    manifest diagnostic. ``source_vertex_indices`` remaps native weight data
    onto OBJ corner expansion when the compatibility reader is used.
    """

    mappings: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for node, identity, mesh_index, source_vertex_indices, source_vertex_count in skinned_nodes:
        gltf_node = node_index[identity]
        try:
            weights = provider.skin_weights(node, scene_nodes)
        except ValueError as exc:
            diagnostics.append(
                {
                    "code": DIAG_MISSING_TARGET,
                    "message": f"renderer {identity} skeleton failed: {exc}",
                    "source": _diagnostic_source(identity, identity_to_bundle),
                }
            )
            continue
        for diagnostic in weights.diagnostics:
            diagnostics.append(
                {
                    "code": diagnostic.code,
                    "message": diagnostic.message,
                    "source": _diagnostic_source(identity, identity_to_bundle),
                }
            )
        skeleton = weights.skeleton
        mapping: dict[str, Any] = {
            "bundle_name": identity_to_bundle.get(identity.file_index),
            "file_index": identity.file_index,
            "path_id": identity.path_id,
            "gltf_node": gltf_node,
            "gltf_mesh": mesh_index,
            "joints": len(skeleton.joints),
            "weights_available": weights.weights_available,
            "skin_attached": False,
        }
        mappings.append(mapping)
        if not weights.weights_available:
            continue
        missing = [
            joint for joint in skeleton.joints if _NodeRef(*joint.identity) not in node_index
        ]
        if missing:
            diagnostics.append(
                {
                    "code": DIAG_MISSING_TARGET,
                    "message": (
                        f"renderer {identity}: {len(missing)} joints outside the"
                        " exported subtree; no skin attached"
                    ),
                    "source": _diagnostic_source(identity, identity_to_bundle),
                }
            )
            continue
        if (
            weights.vertex_joints is None
            or weights.vertex_weights is None
            or len(weights.vertex_joints) != source_vertex_count
            or len(weights.vertex_joints) != len(weights.vertex_weights)
        ):
            diagnostics.append(
                {
                    "code": DIAG_MISSING_TARGET,
                    "message": (
                        f"renderer {identity}: weight count does not match the native"
                        f" mesh's {source_vertex_count} vertices; no skin attached"
                    ),
                    "source": _diagnostic_source(identity, identity_to_bundle),
                }
            )
            continue
        if not skeleton.joints:
            diagnostics.append(
                {
                    "code": DIAG_MISSING_TARGET,
                    "message": f"renderer {identity}: skeleton has no joints; no skin attached",
                    "source": _diagnostic_source(identity, identity_to_bundle),
                }
            )
            continue
        if any(
            source_index < 0 or source_index >= source_vertex_count
            for source_index in source_vertex_indices
        ):
            diagnostics.append(
                {
                    "code": DIAG_MISSING_TARGET,
                    "message": f"renderer {identity}: mesh vertex remap is out of range; no skin attached",
                    "source": _diagnostic_source(identity, identity_to_bundle),
                }
            )
            continue

        remapped_joints: list[tuple[int, int, int, int]] = []
        remapped_weights: list[tuple[float, float, float, float]] = []
        invalid_skin_data = False
        for source_index in source_vertex_indices:
            joints_row = weights.vertex_joints[source_index]
            weights_row = weights.vertex_weights[source_index]
            try:
                converted_weights = tuple(float(weight) for weight in weights_row)
            except (TypeError, ValueError, OverflowError):
                invalid_skin_data = True
                break
            if (
                len(joints_row) != 4
                or any(
                    not isinstance(joint, int)
                    or joint < 0
                    or joint >= len(skeleton.joints)
                    or joint > 65535
                    for joint in joints_row
                )
                or len(converted_weights) != 4
                or any(not math.isfinite(weight) or weight < 0 for weight in converted_weights)
            ):
                invalid_skin_data = True
                break
            remapped_joints.append(tuple(joints_row))
            remapped_weights.append(converted_weights)
        if invalid_skin_data:
            diagnostics.append(
                {
                    "code": DIAG_MISSING_TARGET,
                    "message": f"renderer {identity}: invalid vertex joint/weight values; no skin attached",
                    "source": _diagnostic_source(identity, identity_to_bundle),
                }
            )
            continue

        inverse_binds = scene.add_inverse_bind_matrices(list(skeleton.inverse_bind_matrices))
        skin_index = scene.add_skin(
            [node_index[_NodeRef(*joint.identity)] for joint in skeleton.joints],
            inverse_binds,
        )
        joints_accessor, weights_accessor = scene.add_vertex_skin_data(
            remapped_joints, remapped_weights
        )
        scene.attach_skin_to_node(
            gltf_node, mesh_index, skin_index, joints_accessor, weights_accessor
        )
        mapping["skin_attached"] = True
    return mappings, diagnostics


def _diagnostic_source(identity: _NodeRef, identity_to_bundle: dict[int, str]) -> dict[str, Any]:
    return {
        "bundle_name": identity_to_bundle.get(identity.file_index),
        "file_index": identity.file_index,
        "path_id": identity.path_id,
    }


EXPORT_MANIFEST_FIELDS = {
    "export_version",
    "package_id",
    "collection_id",
    "roots",
    "conversion_rules",
    "content_hash",
    "glb_byte_length",
    "node_count",
    "mesh_count",
    "mappings",
    "diagnostics",
    "report_summary",
}


def _validate_export_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("export manifest must be an object")
    if set(value) != EXPORT_MANIFEST_FIELDS:
        raise ValueError(
            f"export manifest fields must be exactly {sorted(EXPORT_MANIFEST_FIELDS)},"
            f" got {sorted(value)}"
        )
    if value["export_version"] != 1:
        raise ValueError("export manifest_version must be 1")
    for key in ("package_id", "collection_id", "content_hash"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"export manifest.{key} must be a non-empty string")
    if not isinstance(value["glb_byte_length"], int) or value["glb_byte_length"] <= 0:
        raise ValueError("export manifest.glb_byte_length must be a positive integer")
    if not isinstance(value["conversion_rules"], dict) or not value["conversion_rules"]:
        raise ValueError("export manifest.conversion_rules must be a mapping")
    mappings = value["mappings"]
    if not isinstance(mappings, dict) or set(mappings) != {
        "nodes",
        "meshes",
        "materials",
        "skins",
    }:
        raise ValueError("export manifest.mappings must contain nodes, meshes, materials, skins")
    diagnostics = value["diagnostics"]
    if not isinstance(diagnostics, list):
        raise ValueError("export manifest.diagnostics must be a list")
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict) or not isinstance(diagnostic.get("code"), str):
            raise ValueError("export manifest.diagnostics entries need string codes")
    return value


def persist_export_manifest(path, manifest: dict[str, Any]) -> None:
    """Atomically persist one export manifest."""

    from updater.state import atomic_write_json

    atomic_write_json(path, manifest, _validate_export_manifest)


def load_export_manifest(path) -> dict[str, Any] | None:
    """Load a persisted export manifest, or ``None`` when absent/corrupt."""

    import json
    import os
    from pathlib import Path as StdPath

    target = StdPath(os.fspath(path))
    if not target.exists():
        return None
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
        return _validate_export_manifest(document)
    except (ValueError, OSError):
        return None
