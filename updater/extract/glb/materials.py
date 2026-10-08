"""Material and texture conversion for streaming Live GLB packages (#39).

Unity materials resolve through the collection identity table, deduplicate
by object identity and converted content hash, and convert to documented
browser-facing glTF representations:

- URP/Lit-style shaders -> metallic-roughness PBR
- shaders whose name contains ``unlit`` -> KHR_materials_unlit
- ``_Surface == 1`` or render queue >= 3000 -> BLEND alpha mode
- ``_AlphaClip == 1`` -> MASK alpha mode with ``_Cutoff``
- ``_EmissionColor`` with positive RGB -> emissive factor

Color spaces are recorded per texture slot: base color and emission are
sRGB, normals are linear.  Texture payloads are embedded as PNG bytes and
deduplicated by content hash.  Unsupported shader features become explicit
diagnostics instead of silent misrepresentation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from updater.extract.glb.collection import resolve_pptr
from updater.extract.glb.contracts import (
    DIAG_MISSING_TARGET,
    DIAG_TYPE_MISMATCH,
    DIAG_UNRESOLVED_EXTERNAL,
    DIAG_UNSUPPORTED_SHADER,
)
from updater.extract.glb.gltf_writer import content_hash
from updater.unity_rs_adapter import (
    CollectionFileTable,
    UnityRsEnvironment,
    UnityRsObject,
    read_image,
)

BASE_COLOR_SLOTS = ("_BaseMap", "_MainTex")
NORMAL_SLOTS = ("_BumpMap",)
EMISSION_SLOTS = ("_EmissionMap",)

MATERIAL_OBJECT_CLASS = 21
TEXTURE_OBJECT_CLASSES = frozenset({28, 187, 213})

UNRESOLVED_TEXTURE_PTR = (0, 0)


@dataclass(frozen=True, slots=True)
class MaterialDiagnostic:
    """One structured material conversion diagnostic."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class TextureEmbed:
    """One deduplicated texture payload bound to a GLB texture slot."""

    slot: str
    bundle_name: str | None
    file_index: int
    path_id: int
    color_space: str
    png_bytes: bytes


@dataclass(frozen=True, slots=True)
class MaterialConversion:
    """Result of converting one Unity material for glTF."""

    identity: tuple[int, int]
    source_name: str
    gltf_material: dict[str, Any]
    textures: tuple[TextureEmbed, ...] = field(default=())
    diagnostics: tuple[MaterialDiagnostic, ...] = field(default=())
    content_hash: str = ""

    @property
    def sort_key(self) -> tuple[int, int]:
        return self.identity


def _shader_name(environment: UnityRsEnvironment, shader_ptr: tuple[int, int]) -> str | None:
    shader = environment.object_by_identity(*shader_ptr)
    if shader is None:
        return None
    return shader.name or ""


def _classify(
    shader_name: str | None, floats: Mapping[str, float], render_queue: int | None
) -> tuple[str, str, float]:
    """Return (representation, alpha_mode, alpha_cutoff)."""

    alpha_cutoff = float(floats.get("_Cutoff", 0.5))
    name = (shader_name or "").lower()
    if "unlit" in name:
        representation = "unlit"
    else:
        representation = "pbr"
    # NOSONAR: Unity serializes shader toggles (_AlphaClip/_Surface) as
    # exact float 0.0/1.0 — no arithmetic rounds into or out of these.
    if floats.get("_AlphaClip", 0.0) == 1.0:  # NOSONAR
        alpha_mode = "MASK"
    elif floats.get("_Surface", 0.0) == 1.0 or (  # NOSONAR
        render_queue is not None and render_queue >= 3000
    ):
        alpha_mode = "BLEND"
    else:
        alpha_mode = "OPAQUE"
    return representation, alpha_mode, alpha_cutoff


def _color_scale_offset_to_glTF(
    scale: Sequence[float], offset: Sequence[float]
) -> dict[str, list[float]]:
    """Unity texture tiling/offset -> glTF KHR_texture_transform (documented)."""

    return {"KHR_texture_transform": {"scale": list(scale), "offset": [-offset[0], -offset[1]]}}


def convert_material(
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
    material_object: UnityRsObject,
) -> MaterialConversion:
    """Convert one Unity Material object into a glTF material description."""

    studio = environment.studio
    native = studio.read_material(material_object.file_index, material_object.path_id)
    floats = {name: value for name, value in native.floats}
    colors = {name: value for name, value in native.colors}
    texenvs = {
        name: (ptr, scale, offset) for name, ptr, scale, offset in native.texture_environments
    }

    shader_name = _shader_name(environment, native.shader)
    representation, alpha_mode, alpha_cutoff = _classify(
        shader_name, floats, native.custom_render_queue
    )

    diagnostics: list[MaterialDiagnostic] = []
    if shader_name is None:
        diagnostics.append(
            MaterialDiagnostic(
                code=DIAG_MISSING_TARGET,
                message=(f"shader pointer {native.shader} did not resolve in the collection"),
            )
        )

    gltf_material: dict[str, Any] = {"name": native.name}
    base_color = colors.get("_BaseColor") or colors.get("_Color")
    if base_color is not None:
        gltf_material.setdefault(
            "pbrMetallicRoughness",
            {},
        )["baseColorFactor"] = [base_color[0], base_color[1], base_color[2], base_color[3]]
    if representation == "pbr":
        gltf_material.setdefault("pbrMetallicRoughness", {}).update(
            {"metallicFactor": 0.0, "roughnessFactor": 1.0}
        )

    if alpha_mode == "MASK":
        gltf_material["alphaMode"] = "MASK"
        gltf_material["alphaCutoff"] = alpha_cutoff
    elif alpha_mode == "BLEND":
        gltf_material["alphaMode"] = "BLEND"

    emission = colors.get("_EmissionColor")
    if emission is not None and any(component > 0.0 for component in emission[:3]):
        gltf_material["emissiveFactor"] = [emission[0], emission[1], emission[2]]

    if representation == "unlit":
        extensions = gltf_material.setdefault("extensions", {})
        extensions["KHR_materials_unlit"] = {}

    textures: list[TextureEmbed] = []
    texture_slot_map: list[tuple[str, tuple[str, ...], str]] = [
        ("baseColorTexture", BASE_COLOR_SLOTS, "sRGB"),
        ("normalTexture", NORMAL_SLOTS, "linear"),
        ("emissiveTexture", EMISSION_SLOTS, "sRGB"),
    ]
    for gltf_slot, unity_slots, color_space in texture_slot_map:
        for unity_slot in unity_slots:
            entry = texenvs.get(unity_slot)
            if entry is None:
                continue
            ptr, scale, offset = entry
            if ptr == UNRESOLVED_TEXTURE_PTR:
                continue
            resolution = resolve_pptr(
                environment,
                table,
                material_object.file_index,
                ptr[0],
                ptr[1],
            )
            if resolution.status == "unknown_external":
                diagnostics.append(
                    MaterialDiagnostic(
                        code=DIAG_UNRESOLVED_EXTERNAL,
                        message=(
                            f"{unity_slot} texture lives in unloaded dependency"
                            f" {resolution.dependency_name}"
                        ),
                    )
                )
                continue
            texture_object = resolution.object
            if texture_object is None:
                diagnostics.append(
                    MaterialDiagnostic(
                        code=DIAG_MISSING_TARGET,
                        message=f"{unity_slot} texture {ptr} not found in its file",
                    )
                )
                continue
            if texture_object.class_id not in TEXTURE_OBJECT_CLASSES:
                diagnostics.append(
                    MaterialDiagnostic(
                        code=DIAG_TYPE_MISMATCH,
                        message=(
                            f"{unity_slot} resolved to {texture_object.type.name},"
                            " expected a texture object"
                        ),
                    )
                )
                continue
            try:
                rendered = read_image(texture_object)
            except Exception as exc:  # NOSONAR - decode failures become diagnostics
                diagnostics.append(
                    MaterialDiagnostic(
                        code=DIAG_UNSUPPORTED_SHADER,
                        message=f"{unity_slot} texture could not be decoded: {exc}",
                    )
                )
                continue
            png_bytes = rendered.encode_png()
            if png_bytes is None:
                diagnostics.append(
                    MaterialDiagnostic(
                        code=DIAG_UNSUPPORTED_SHADER,
                        message=f"{unity_slot} texture encoded no PNG payload",
                    )
                )
                continue
            textures.append(
                TextureEmbed(
                    slot=gltf_slot,
                    bundle_name=_bundle_for_file(table, texture_object.file_index),
                    file_index=texture_object.file_index,
                    path_id=texture_object.path_id,
                    color_space=color_space,
                    png_bytes=png_bytes,
                )
            )
            # glTF 2.0 puts baseColorTexture inside pbrMetallicRoughness;
            # normal/emissive textures sit on the material root.
            container = (
                gltf_material.setdefault("pbrMetallicRoughness", {})
                if gltf_slot == "baseColorTexture"
                else gltf_material
            )
            container[gltf_slot] = {"index": None}  # bound by the scene builder
            if tuple(scale) != (1.0, 1.0) or tuple(offset) != (0.0, 0.0):
                container[gltf_slot]["extensions"] = _color_scale_offset_to_glTF(scale, offset)
            break

    if diagnostics:
        unsupported = {diagnostic.code for diagnostic in diagnostics}
        if DIAG_UNSUPPORTED_SHADER in unsupported:
            gltf_material.setdefault("extras", {})["diagnostics"] = [
                {"code": diagnostic.code, "message": diagnostic.message}
                for diagnostic in diagnostics
            ]

    return MaterialConversion(
        identity=(material_object.file_index, material_object.path_id),
        source_name=native.name,
        gltf_material=gltf_material,
        textures=tuple(textures),
        diagnostics=tuple(diagnostics),
        content_hash=content_hash(
            repr(sorted(gltf_material.items(), key=lambda item: item[0])).encode("utf-8")
        ),
    )


def _bundle_for_file(table: CollectionFileTable, file_index: int) -> str | None:
    for identity in table.identities:
        if identity.file_index == file_index:
            return identity.bundle_name
    return None


def deduplicate_materials(
    conversions: Sequence[MaterialConversion],
) -> tuple[MaterialConversion, ...]:
    """Deduplicate conversions by object identity, keeping first-seen order."""

    seen: dict[tuple[int, int], MaterialConversion] = {}
    for conversion in conversions:
        seen.setdefault(conversion.identity, conversion)
    return tuple(seen[name] for name in sorted(seen))


def deduplicate_textures(textures: Sequence[TextureEmbed]) -> tuple[tuple[str, bytes], ...]:
    """Deduplicate texture payloads by content hash."""

    unique: dict[str, tuple[str, bytes]] = {}
    for texture in textures:
        key = content_hash(texture.png_bytes)
        unique.setdefault(key, (texture.color_space, texture.png_bytes))
    return tuple(unique[key] for key in sorted(unique))
