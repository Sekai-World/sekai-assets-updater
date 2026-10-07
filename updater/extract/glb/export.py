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

from dataclasses import dataclass
from typing import Any, Sequence

from updater.extract.glb.collection import (
    CollectionFileTable,
    GlbCollectionExtraction,
)
from updater.extract.glb.gltf_writer import GlbScene, content_hash, validate_glb_bytes
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
    "materials": "single placeholder pbr material (converted materials join in phase 5)",
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


def _mirror_position(value: tuple[float, float, float]) -> tuple[float, float, float]:
    # "+ 0.0" normalizes IEEE negative zero so identical inputs always
    # produce byte-identical buffers.
    return (-value[0] + 0.0, value[1] + 0.0, value[2] + 0.0)


def _mirror_rotation(value: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return (value[0], -value[1], -value[2], value[3])


def parse_obj_mesh(obj_bytes: bytes) -> dict[str, Any]:
    """Decode the binding's OBJ emission into mirrored, deduplicated geometry.

    Faces are triangulated in reversed order so front faces survive the X
    mirror.  Unique ``(v, vt, vn)`` corners keep first-seen order, which is
    deterministic given a deterministic OBJ.
    """

    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    texcoords: list[tuple[float, float]] = []
    corner_index: dict[tuple[int, int, int], int] = {}
    out_positions: list[tuple[float, float, float]] = []
    out_normals: list[tuple[float, float, float]] = []
    out_uvs: list[tuple[float, float]] = []
    indices: list[int] = []

    def _vertex(raw: str) -> int:
        v_part, _, rest = raw.partition("/")
        vt_part, _, vn_part = rest.partition("/")
        corner = (int(v_part), int(vt_part or 0), int(vn_part or 0))
        known = corner_index.get(corner)
        if known is not None:
            return known
        vi, vti, vni = corner
        out_positions.append(_mirror_position(positions[vi - 1]))
        out_uvs.append(texcoords[vti - 1] if vti else (0.0, 0.0))
        out_normals.append(_mirror_position(normals[vni - 1]) if vni else (0.0, 1.0, 0.0))
        new_index = len(out_positions) - 1
        corner_index[corner] = new_index
        return new_index

    for line in obj_bytes.decode("utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "v":
            positions.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif parts[0] == "vt":
            texcoords.append((float(parts[1]), float(parts[2])))
        elif parts[0] == "vn":
            normals.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif parts[0] == "f":
            corners = [_vertex(part) for part in parts[1:]]
            # Reversed winding: (a, b, c) becomes (a, c, b) after mirroring.
            for k in range(1, len(corners) - 1):
                indices.append(corners[0])
                indices.append(corners[k + 1])
                indices.append(corners[k])
    return {
        "positions": out_positions,
        "normals": out_normals,
        "texcoords": out_uvs,
        "indices": indices,
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


def export_root_glb(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    extraction: GlbCollectionExtraction,
    root_identity: tuple[int, int],
    *,
    bundle_name_for_file: dict[int, str] | None = None,
    include_fbx_diagnostic: bool = False,
    allow_incomplete: bool = False,
) -> GlbExport:
    """Export one root's subtree as a deterministic GLB artifact.

    ``extraction`` is the validated package from Phase 3; its report must be
    publishable unless ``allow_incomplete`` marks the artifact as a local
    diagnostic (the publish path in sync_worker never passes it — required
    L2 failures make a package non-publishable).  The resulting manifest
    embeds the conversion rules, the GLB content hash, and per-node and
    per-mesh Unity mappings.
    """

    if not extraction.report.publishable and not allow_incomplete:
        raise ValueError("refusing to export a package with required L2 findings")
    scene_nodes = environment.studio.scene()
    subtree = select_root_subtree(scene_nodes, root_identity)
    root_ref = _NodeRef(*root_identity)
    identity_to_bundle = bundle_name_for_file or {
        identity.file_index: identity.bundle_name for identity in table.identities
    }

    scene = GlbScene()
    material_index = scene.add_default_material()
    node_index: dict[_NodeRef, int] = {}
    node_mappings: list[dict[str, Any]] = []
    mesh_mappings: list[dict[str, Any]] = []
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
                geometry = parse_obj_mesh(
                    environment.studio.read_mesh_obj(
                        mesh_identity.file_index, mesh_identity.path_id
                    )
                )
                hierarchy_path = _hierarchy_path(parents, node, root_ref)
                mesh_index = scene.add_mesh(
                    name=mesh_object.name or f"mesh_{mesh_identity.path_id}",
                    positions=geometry["positions"],
                    normals=geometry["normals"],
                    texcoords=geometry["texcoords"],
                    indices=geometry["indices"],
                    material_index=material_index,
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
        "mappings": {"nodes": node_mappings, "meshes": mesh_mappings},
        "report_summary": extraction.report.summary(),
    }
    fbx_bytes: bytes | None = None
    if include_fbx_diagnostic:
        fbx_bytes = environment.studio.read_game_object_fbx(
            root_identity[0], root_identity[1], include_animations=False
        )
    return GlbExport(glb_bytes=glb_bytes, manifest=manifest, fbx_diagnostic=fbx_bytes)


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
    if not isinstance(mappings, dict) or set(mappings) != {"nodes", "meshes"}:
        raise ValueError("export manifest.mappings must contain nodes and meshes")
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
