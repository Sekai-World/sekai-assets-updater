"""Minimal deterministic glTF 2.0 / GLB writer for streaming Live GLB export.

Controlled-writer strategy (roadmap Phase 4, #38): the exporter builds the
whole glTF document and binary buffer in memory with fixed ordering and
fixed encodings, then serializes once.  No third-party glTF library is
involved, so identical inputs always produce byte-identical output.

Encodings (fixed by contract):
- positions: FLOAT32 VEC3, normals: FLOAT32 VEC3, texcoords: FLOAT32 VEC2
- indices: UNSIGNED_INT SCALAR
- JSON: orjson with sorted keys, padded with 0x20 to 4 bytes
- BIN: zero-padded to 4 bytes
"""

from __future__ import annotations

import hashlib
import struct
from typing import Any

import orjson

GLB_MAGIC = 0x46546C67  # 'glTF'
GLB_VERSION = 2
JSON_CHUNK_MAGIC = 0x4E4F534A  # 'JSON'
BIN_CHUNK_MAGIC = 0x004E4942  # 'BIN'

COMPONENT_FLOAT = 5126
COMPONENT_UNSIGNED_INT = 5125

TARGET_ARRAY_BUFFER = 34962
TARGET_ELEMENT_ARRAY_BUFFER = 34963

TYPE_VEC2 = "VEC2"
TYPE_VEC3 = "VEC3"
TYPE_VEC4 = "VEC4"
TYPE_SCALAR = "SCALAR"

# Byte widths per glTF component type (only the two used here).
_COMPONENT_SIZES = {COMPONENT_FLOAT: 4, COMPONENT_UNSIGNED_INT: 4}
_TYPE_COMPONENT_COUNTS = {TYPE_VEC2: 2, TYPE_VEC3: 3, TYPE_SCALAR: 1}


def _pad4(size: int) -> int:
    return (4 - size % 4) % 4


class GlbScene:
    """In-memory glTF scene assembled with deterministic identities."""

    def __init__(self) -> None:
        self._bin = bytearray()
        self._accessors: list[dict[str, Any]] = []
        self._buffer_views: list[dict[str, Any]] = []
        self._meshes: list[dict[str, Any]] = []
        self._nodes: list[dict[str, Any]] = []
        self._materials: list[dict[str, Any]] = []
        self._images: list[dict[str, Any]] = []
        self._textures: list[dict[str, Any]] = []
        self._samplers: list[dict[str, Any]] = []
        self._default_material_index: int | None = None

    # -- buffer plumbing ---------------------------------------------------

    def _add_accessor(
        self,
        elements: list[tuple],
        component_type: int,
        accessor_type: str,
        *,
        bounds: bool = False,
    ) -> int:
        stride = _COMPONENT_SIZES[component_type] * _TYPE_COMPONENT_COUNTS[accessor_type]
        padding = _pad4(len(self._bin))
        self._bin.extend(b"\x00" * padding)
        offset = len(self._bin)
        if component_type == COMPONENT_FLOAT:
            fmt = "<" + "f" * (stride // 4)
            payload = b"".join(struct.pack(fmt, *element) for element in elements)
        else:
            payload = struct.pack(f"<{len(elements)}I", *(element[0] for element in elements))
        self._bin.extend(payload)
        view = {
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": stride * len(elements),
            "target": (
                TARGET_ELEMENT_ARRAY_BUFFER if accessor_type == TYPE_SCALAR else TARGET_ARRAY_BUFFER
            ),
        }
        self._buffer_views.append(view)
        accessor: dict[str, Any] = {
            "bufferView": len(self._buffer_views) - 1,
            "componentType": component_type,
            "count": len(elements),
            "type": accessor_type,
        }
        if bounds and elements:
            accessor["min"] = [
                min(element[index] for element in elements) for index in range(len(elements[0]))
            ]
            accessor["max"] = [
                max(element[index] for element in elements) for index in range(len(elements[0]))
            ]
        self._accessors.append(accessor)
        return len(self._accessors) - 1

    def add_positions(self, positions: list[tuple[float, float, float]]) -> int:
        return self._add_accessor(positions, COMPONENT_FLOAT, TYPE_VEC3, bounds=True)

    def add_normals(self, normals: list[tuple[float, float, float]]) -> int:
        return self._add_accessor(normals, COMPONENT_FLOAT, TYPE_VEC3)

    def add_texcoords(self, uvs: list[tuple[float, float]]) -> int:
        return self._add_accessor(uvs, COMPONENT_FLOAT, TYPE_VEC2)

    def add_indices(self, indices: list[int]) -> int:
        return self._add_accessor(
            [(index,) for index in indices], COMPONENT_UNSIGNED_INT, TYPE_SCALAR
        )

    def add_default_material(self, name: str = "stage_default") -> int:
        """Phase 4 placeholder PBR material (converted materials join in #39)."""
        if self._default_material_index is None:
            self._materials.append(
                {
                    "name": name,
                    "pbrMetallicRoughness": {
                        "baseColorFactor": [0.8, 0.8, 0.8, 1.0],
                        "metallicFactor": 0.0,
                        "roughnessFactor": 1.0,
                    },
                }
            )
            self._default_material_index = len(self._materials) - 1
        return self._default_material_index

    # -- scene assembly ----------------------------------------------------

    def add_mesh(
        self,
        *,
        name: str,
        positions: list[tuple[float, float, float]],
        normals: list[tuple[float, float, float]],
        texcoords: list[tuple[float, float]],
        indices: list[int],
        material_index: int,
    ) -> int:
        primitive = {
            "attributes": {
                "POSITION": self.add_positions(positions),
                "NORMAL": self.add_normals(normals),
                "TEXCOORD_0": self.add_texcoords(texcoords),
            },
            "indices": self.add_indices(indices),
            "material": material_index,
            "mode": 4,
        }
        self._meshes.append({"name": name, "primitives": [primitive]})
        return len(self._meshes) - 1

    def add_node(self, node: dict[str, Any]) -> int:
        self._nodes.append(node)
        return len(self._nodes) - 1

    def attach_children(self, node_index: int, children: list[int]) -> None:
        """Set a node's child indices (ascending order enforced)."""

        self._nodes[node_index]["children"] = sorted(children)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    # -- serialization -----------------------------------------------------

    def document(self, *, root_nodes: list[int], generator: str) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "asset": {"version": "2.0", "generator": generator},
            "scene": 0,
            "scenes": [{"nodes": list(root_nodes)}],
            "nodes": self._nodes,
            "accessors": self._accessors,
            "bufferViews": self._buffer_views,
            "buffers": [{"byteLength": len(self._bin)}],
        }
        if self._meshes:
            doc["meshes"] = self._meshes
        if self._materials:
            doc["materials"] = self._materials
        if self._textures:
            doc["textures"] = self._textures
        if self._images:
            doc["images"] = self._images
        if self._samplers:
            doc["samplers"] = self._samplers
        return doc

    def encode(self, *, root_nodes: list[int], generator: str) -> bytes:
        """Serialize the document and binary chunk into GLB bytes."""

        self._bin.extend(b"\x00" * _pad4(len(self._bin)))
        json_bytes = orjson.dumps(
            self.document(root_nodes=root_nodes, generator=generator),
            option=orjson.OPT_SORT_KEYS,
        )
        json_padding = _pad4(len(json_bytes))
        json_bytes += b" " * json_padding

        total = 12 + 8 + len(json_bytes) + 8 + len(self._bin)
        header = struct.pack("<III", GLB_MAGIC, GLB_VERSION, total)
        json_header = struct.pack("<II", len(json_bytes), JSON_CHUNK_MAGIC)
        bin_header = struct.pack("<II", len(self._bin), BIN_CHUNK_MAGIC)
        return header + json_header + json_bytes + bin_header + bytes(self._bin)


def validate_glb_bytes(data: bytes) -> dict[str, Any]:
    """Structurally validate GLB bytes and return the parsed document.

    Enforces the reference-integrity rules an offline glTF validator applies
    to the subset this writer emits: chunk layout, accessor/bufferView bounds,
    mesh/material/node references, and POSITION min/max requirements.
    """

    if len(data) < 20:
        raise ValueError("GLB too short for header")
    magic, version, total = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC:
        raise ValueError("not a GLB container")
    if version != GLB_VERSION:
        raise ValueError(f"unsupported GLB version {version}")
    if total != len(data):
        raise ValueError(f"GLB length {len(data)} does not match header {total}")
    json_length, json_magic = struct.unpack_from("<II", data, 12)
    if json_magic != JSON_CHUNK_MAGIC:
        raise ValueError("first chunk is not JSON")
    if 20 + json_length > len(data):
        raise ValueError("JSON chunk exceeds container")
    document = orjson.loads(data[20 : 20 + json_length])
    if not isinstance(document, dict) or document.get("asset", {}).get("version") != "2.0":
        raise ValueError("glTF document must declare asset version 2.0")
    bin_offset = 20 + json_length
    bin_length, bin_magic = struct.unpack_from("<II", data, bin_offset)
    if bin_magic != BIN_CHUNK_MAGIC:
        raise ValueError("second chunk is not BIN")
    if bin_offset + 8 + bin_length != len(data):
        raise ValueError("BIN chunk length does not match container")
    buffers = document.get("buffers", [])
    if not buffers or buffers[0]["byteLength"] > bin_length:
        raise ValueError("buffer byteLength exceeds BIN chunk")

    views = document.get("bufferViews", [])
    accessors = document.get("accessors", [])
    position_accessors = {
        primitive["attributes"]["POSITION"]
        for mesh in document.get("meshes", [])
        for primitive in mesh["primitives"]
        if "POSITION" in primitive["attributes"]
    }
    for view in views:
        start = view["byteOffset"]
        end = start + view["byteLength"]
        if end > bin_length or start % 4:
            raise ValueError(f"bufferView out of bounds or unaligned: {view}")
    for accessor_position, accessor in enumerate(accessors):
        view = views[accessor["bufferView"]]
        stride = (
            _COMPONENT_SIZES[accessor["componentType"]] * _TYPE_COMPONENT_COUNTS[accessor["type"]]
        )
        if view["byteLength"] < stride * accessor["count"]:
            raise ValueError(f"accessor exceeds bufferView: {accessor}")
        if accessor_position in position_accessors and (
            "min" not in accessor or "max" not in accessor
        ):
            raise ValueError("POSITION accessor missing min/max")
    node_ids = set(range(len(document.get("nodes", []))))
    for node in document.get("nodes", []):
        for child in node.get("children", []):
            if child not in node_ids:
                raise ValueError(f"node references unknown child {child}")
    mesh_ids = set(range(len(document.get("meshes", []))))
    material_ids = set(range(len(document.get("materials", []))))
    for node in document.get("nodes", []):
        if "mesh" in node and node["mesh"] not in mesh_ids:
            raise ValueError(f"node references unknown mesh {node['mesh']}")
    for mesh in document.get("meshes", []):
        for primitive in mesh["primitives"]:
            if primitive["indices"] >= len(accessors):
                raise ValueError("primitive references unknown index accessor")
            for attribute, accessor_index in primitive["attributes"].items():
                if accessor_index >= len(accessors):
                    raise ValueError(f"attribute {attribute} references unknown accessor")
            if "material" in primitive and primitive["material"] not in material_ids:
                raise ValueError("primitive references unknown material")
    return document


def content_hash(data: bytes) -> str:
    """SHA-256 content hash used in export manifests."""

    return hashlib.sha256(data).hexdigest()
