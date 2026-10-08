"""GLB Phase 5 tests: material/texture conversion and export binding (#39)."""

from __future__ import annotations

from types import SimpleNamespace

from tests.glb_export_test import (
    ROOT_IDENTITY,
    SHADER_BUNDLE,
    STAGE_BUNDLE,
    _ExportStudio,
    _extraction,
    _Info,
)
from updater import unity_rs_adapter
from updater.extract.glb import (
    DIAG_MISSING_TARGET,
    DIAG_TYPE_MISMATCH,
    DIAG_UNRESOLVED_EXTERNAL,
    export_root_glb,
    validate_glb_bytes,
)
from updater.extract.glb.materials import (
    MaterialConversion,
    convert_material,
    deduplicate_materials,
    deduplicate_textures,
)

STAGE_SOURCE = f"{STAGE_BUNDLE}::CAB-a"

FAKE_PNG = b"fake-png-payload"


class _FakeNativeImage:
    width = 2
    height = 2

    def encode(self, _format: str, compression: str | int = "fast") -> bytes:
        return FAKE_PNG


def _material_native(
    *,
    name: str = "m_floor",
    shader: tuple[int, int] = (0, 200),
    floats: list[tuple[str, float]] | None = None,
    colors: list[tuple[str, tuple[float, ...]]] | None = None,
    textures: list[tuple[str, tuple[int, int], tuple[float, float], tuple[float, float]]]
    | None = None,
    render_queue: int | None = 2000,
):
    return SimpleNamespace(
        name=name,
        shader=shader,
        floats=floats or [],
        colors=colors or [],
        texture_environments=textures or [],
        custom_render_queue=render_queue,
    )


class _MaterialStudio(_ExportStudio):
    """Export studio plus one material, one shader, and one texture."""

    def __init__(
        self,
        *,
        texture_object_class: int = 28,
        texture_decoder: str = "ok",
        material_shader: tuple[int, int] = (0, 200),
    ) -> None:
        super().__init__()
        self._objects.append(
            _Info(file_index=0, path_id=200, class_id=48, name="URP/Lit", source_path=STAGE_SOURCE)
        )
        self._objects.append(
            _Info(file_index=0, path_id=300, class_id=21, name="m_floor", source_path=STAGE_SOURCE)
        )
        self._objects.append(
            _Info(
                file_index=0,
                path_id=400,
                class_id=texture_object_class,
                name="tex_floor",
                source_path=STAGE_SOURCE,
            )
        )
        self._typetrees[(0, 200)] = {"m_ParsedForm": {}}
        self._typetrees[(0, 300)] = {"m_Name": "m_floor"}
        self._typetrees[(0, 400)] = {"m_Name": "tex_floor"}
        self._material_shader = material_shader
        self.texture_decoder = texture_decoder
        # child_a carries the material so the export binds it.
        self._scene[1].materials = [(0, 300)]

    def read_material(self, _file_index: int, path_id: int):
        assert path_id == 300
        return _material_native(
            shader=self._material_shader,
            textures=[("_BaseMap", (0, 400), (1.0, 1.0), (0.0, 0.0))],
        )

    def read_texture(self, _file_index: int, _path_id: int):
        if self.texture_decoder == "raise":
            raise RuntimeError("decode exploded")
        if self.texture_decoder == "none":
            return SimpleNamespace(width=2, height=2)  # no encode method
        return _FakeNativeImage()


def _material_environment(**kwargs):
    return unity_rs_adapter.UnityRsEnvironment(_MaterialStudio(**kwargs))


def _convert(studio):
    environment = unity_rs_adapter.UnityRsEnvironment(studio)
    table = unity_rs_adapter.build_collection_file_table(environment)
    material = environment.object_by_identity(0, 300)
    return convert_material(environment, table, material)


def test_pbr_opaque_material_converts_with_factors() -> None:
    conversion = _convert(_MaterialStudio())

    assert isinstance(conversion, MaterialConversion)
    assert conversion.source_name == "m_floor"
    assert conversion.diagnostics == ()
    assert conversion.content_hash
    gltf_material = conversion.gltf_material
    assert gltf_material["name"] == "m_floor"
    assert gltf_material["pbrMetallicRoughness"] == {
        "metallicFactor": 0.0,
        "roughnessFactor": 1.0,
        "baseColorTexture": {"index": None},  # default fixture carries a _BaseMap
    }
    assert "alphaMode" not in gltf_material
    assert "extensions" not in gltf_material


def test_unlit_and_blend_classification() -> None:
    conversion = _convert(_UnlitStudio(surface=1.0, unlit=True))
    gltf_material = conversion.gltf_material
    assert gltf_material["extensions"] == {"KHR_materials_unlit": {}}
    assert gltf_material["alphaMode"] == "BLEND"


def test_alpha_clip_becomes_mask_with_cutoff() -> None:
    conversion = _convert(_AlphaClipStudio())
    gltf_material = conversion.gltf_material
    assert gltf_material["alphaMode"] == "MASK"
    assert gltf_material["alphaCutoff"] == 0.25


class _BaseTweakStudio(_MaterialStudio):
    """Material studio whose shader floats/colors/textures are customized."""

    def __init__(self, *, floats=None, colors=None, textures=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._floats = floats or []
        self._colors = colors or []
        self._textures = textures or []

    def read_material(self, _file_index: int, path_id: int):
        assert path_id == 300
        return _material_native(
            shader=self._material_shader,
            floats=self._floats,
            colors=self._colors,
            textures=self._textures,
        )


class _UnlitStudio(_BaseTweakStudio):
    def __init__(self, *, surface: float, unlit: bool) -> None:
        super().__init__(
            floats=[("_Surface", surface)],
            material_shader=(0, 301 if unlit else 200),
        )
        if unlit:
            self._objects.append(
                _Info(
                    file_index=0,
                    path_id=301,
                    class_id=48,
                    name="Unlit/Color",
                    source_path=STAGE_SOURCE,
                )
            )
            self._typetrees[(0, 301)] = {"m_ParsedForm": {}}


class _AlphaClipStudio(_BaseTweakStudio):
    def __init__(self) -> None:
        super().__init__(floats=[("_AlphaClip", 1.0), ("_Cutoff", 0.25)])


def test_color_factors_flow_from_unity_properties() -> None:
    environment = _BaseTweakStudio(
        colors=[("_BaseColor", (0.2, 0.4, 0.6, 0.8)), ("_EmissionColor", (0.1, 0.0, 0.0, 1.0))]
    )
    conversion = _convert(environment)
    gltf_material = conversion.gltf_material
    assert gltf_material["pbrMetallicRoughness"]["baseColorFactor"] == [0.2, 0.4, 0.6, 0.8]
    assert gltf_material["emissiveFactor"] == [0.1, 0.0, 0.0]
    # Zero emission stays absent.
    dark = _BaseTweakStudio(colors=[("_EmissionColor", (0.0, 0.0, 0.0, 1.0))])
    assert "emissiveFactor" not in _convert(dark).gltf_material


def test_texture_slots_embed_and_carry_color_space_and_transform() -> None:
    environment = _BaseTweakStudio(
        textures=[
            ("_BaseMap", (0, 400), (2.0, 2.0), (0.5, 0.0)),
            ("_BumpMap", (0, 400), (1.0, 1.0), (0.0, 0.0)),
        ]
    )
    conversion = _convert(environment)

    assert [texture.slot for texture in conversion.textures] == [
        "baseColorTexture",
        "normalTexture",
    ]
    assert [texture.color_space for texture in conversion.textures] == ["sRGB", "linear"]
    assert conversion.textures[0].png_bytes == FAKE_PNG
    gltf_material = conversion.gltf_material
    pbr = gltf_material["pbrMetallicRoughness"]
    assert pbr["baseColorTexture"]["index"] is None  # scene binds it
    assert pbr["baseColorTexture"]["extensions"] == {
        "KHR_texture_transform": {"scale": [2.0, 2.0], "offset": [-0.5, -0.0]}
    }
    assert gltf_material["normalTexture"]["index"] is None
    assert "extensions" not in gltf_material["normalTexture"]


def test_texture_diagnostics_cover_unresolved_missing_and_mismatch() -> None:
    resolution_studio = _UnresolvedExternalStudio()
    conversion = _convert(resolution_studio)
    codes = {diagnostic.code for diagnostic in conversion.diagnostics}
    assert DIAG_UNRESOLVED_EXTERNAL in codes

    missing = _MaterialStudio(material_shader=(0, 200))
    missing._typetrees.pop((0, 400))
    missing._objects = [o for o in missing._objects if o.path_id != 400]
    conversion = _convert(missing)
    assert DIAG_MISSING_TARGET in {d.code for d in conversion.diagnostics}

    mismatch = _MaterialStudio(texture_object_class=1)  # a GameObject, not a texture
    conversion = _convert(mismatch)
    assert DIAG_TYPE_MISMATCH in {d.code for d in conversion.diagnostics}

    broken = _MaterialStudio(texture_decoder="raise")
    conversion = _convert(broken)
    assert conversion.diagnostics, "decode failure must surface"
    silent = _MaterialStudio(texture_decoder="none")
    conversion = _convert(silent)
    assert any("no PNG payload" in d.message for d in conversion.diagnostics)


class _UnresolvedExternalStudio(_MaterialStudio):
    """Material whose _BaseMap lives behind an unloaded dependency."""

    def __init__(self) -> None:
        super().__init__()
        self._dependencies[0] = [SHADER_BUNDLE, "extra/not_loaded"]

    def read_material(self, _file_index: int, path_id: int):
        assert path_id == 300
        return _material_native(textures=[("_BaseMap", (2, 400), (1.0, 1.0), (0.0, 0.0))])


def test_dedup_helpers() -> None:
    environment = _BaseTweakStudio(textures=[("_BaseMap", (0, 400), (1.0, 1.0), (0.0, 0.0))])
    conversion = _convert(environment)
    again = _convert(environment)
    merged = deduplicate_materials([conversion, again])
    assert len(merged) == 1
    unique = deduplicate_textures([*conversion.textures, *again.textures])
    assert len(unique) == 1


def test_export_binds_converted_material_with_embedded_texture() -> None:
    environment = _material_environment()
    extraction, table = _extraction(environment)

    export = export_root_glb(environment, table, extraction, ROOT_IDENTITY)

    document = validate_glb_bytes(export.glb_bytes)
    converted = document["materials"][1]
    assert converted["name"] == "m_floor"
    assert converted["pbrMetallicRoughness"]["baseColorTexture"]["index"] == 0
    assert document["textures"] == [{"sampler": 0, "source": 0}]
    image = document["images"][0]
    assert image["mimeType"] == "image/png"
    assert document["bufferViews"][image["bufferView"]]["byteLength"] == len(FAKE_PNG)
    primitive = document["meshes"][0]["primitives"][0]
    assert primitive["material"] == 1  # child_a's mesh uses the converted material
    materials_mapping = export.manifest["mappings"]["materials"]
    assert materials_mapping == [
        {
            "bundle_name": STAGE_BUNDLE,
            "file_index": 0,
            "path_id": 300,
            "gltf_material": 1,
            "texture_count": 1,
            "source_name": "m_floor",
        }
    ]
    assert export.manifest["diagnostics"] == []


def test_export_deduplicates_shared_textures_and_materials() -> None:
    environment = _material_environment()
    # root_b's mesh shares the same material pointer.
    environment.studio._scene[2].materials = [(0, 300)]
    extraction, table = _extraction(environment)

    export = export_root_glb(environment, table, extraction, ROOT_IDENTITY)

    document = validate_glb_bytes(export.glb_bytes)
    assert len(document["materials"]) == 2  # default + one converted
    assert len(document["images"]) == 1
    assert len(document["textures"]) == 1
    assert len(export.manifest["mappings"]["materials"]) == 1


def test_export_reports_missing_material_as_diagnostic() -> None:
    environment = _material_environment()
    environment.studio._scene[1].materials = [(0, 999)]
    extraction, table = _extraction(environment)

    export = export_root_glb(environment, table, extraction, ROOT_IDENTITY)

    document = validate_glb_bytes(export.glb_bytes)
    # Falls back to the default material and records the miss.
    assert document["meshes"][0]["primitives"][0]["material"] == 0
    codes = [d["code"] for d in export.manifest["diagnostics"]]
    assert DIAG_MISSING_TARGET in codes


def test_export_is_deterministic_with_materials() -> None:
    environment = _material_environment()
    extraction, table = _extraction(environment)
    first = export_root_glb(environment, table, extraction, ROOT_IDENTITY)
    second = export_root_glb(environment, table, extraction, ROOT_IDENTITY)
    assert first.glb_bytes == second.glb_bytes
