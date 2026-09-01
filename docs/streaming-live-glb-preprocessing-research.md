# Streaming Live GLB Preprocessing Research

Date: 2026-09-01

## Scope

This note records the investigation of browser playback for Project Sekai
Streaming Live resources. It covers the local extractor, the current JP
AssetBundle metadata, decoded files published in the S3-compatible storage,
`sekai-master-db-diff`, and the available APK reverse-engineering reports.

No credentials, signed cookies, or raw downloaded bundles are stored here.

## Findings

### AssetBundle loading

The current JP metadata is version `6.8.0.10`. A Streaming Live Timeline and a
stage are not self-contained resources. The selected `0006_lon_vbs_01` Timeline
and `base_007_sp_live` stage require a dependency closure containing shader
bundles, shared stage bundles, light prefabs, and camera decoration resources.

Direct local downloads were performed without a proxy. The following current
bundles were downloaded and validated through the existing transport decoder:

- `streaming_live/model/stage/base_007_sp_live`
- `streaming_live/model/stage/base`
- `streaming_live/model/stage/base_002_colabo_es_live`
- `streaming_live/model/stage/base_006_joint_mmj_wnd`
- `shader/live`
- `shader/rp_common`
- `live_pv/model/camera_decoration/0068`

Adding the locally cached `base_000_joint_live_lon_vbs` to that collection gave
the following result:

- 8 Unity input files
- 20,607 serialized objects
- 5,413 scene nodes
- 1,764 mesh-bearing scene nodes
- 318 materials
- 195 animators
- no unresolved texture references in the combined FBX export

The same stage loaded alone reports unresolved external `_BaseMap` references.
Therefore, single-Bundle extraction is not sufficient for GLB generation.

The APK reverse-engineering reports corroborate that AssetBundle loading uses
metadata-driven dependencies, cache lookup, URL fallback, and a custom XOR
transport layer. The extractor already handles the transport header, but the
GLB path still needs a collection-level dependency resolver.

### Existing S3 output

The S3 package for
`live_pv/streaming_live/model/stage/base_007_sp_live` contains the expected
prefab, material, texture, and extracted-object files. However:

- The files under `fbx/` with original Unity names can be JSON typetree output,
  despite their `.fbx` suffix. They are not necessarily FBX files.
- The actual generated `fbx/model.fbx` is not present in the inspected S3
  package.
- `.mat` files are Unity serialized material data, not browser-ready materials.
- Image and material byte differences are caused by exporter representation and
  encoding differences; byte equality is not a reliable semantic comparison.

The current implementation exports a whole loaded scene through
`read_fbx_with_textures()`. A root-scoped export through
`read_game_object_fbx()` produced approximately 1.9 MB for
`base_007_sp_live` and 4.5 MB for the circus stage object. Root-scoped export
is the appropriate unit for a future GLB asset.

### Timeline and animation data

The inspected modern Timeline has 5,941 events and 18 track types, including:

- 457 `AnimationPlayableAsset` clips
- 73 `EffectClip` clips
- 124 `CharacterMonitorClip` clips
- 24 `SekaiManaClip` clips
- 23 `ShoutTimeClip` clips
- lighting, color, intensity, global-settings, mob, and stage-switch clips

`AnimationPlayableAsset` entries contain PPtr references to AnimationClip
objects. The current playable JSON preserves the references and event data but
does not resolve them into browser animation assets.

Cached character motion bundles contain Unity AnimationClip data with streamed
and constant curves. The existing Live2D code demonstrates that these curves
can be decoded from the Unity typetree, but that logic is not yet generalized
to character GLB animation export.

The selected stage Bundles contain no useful AnimationClip collection of their
own. Character motion, facial animation, and related model resources must be
resolved from their separate model/motion dependencies.

## Required preprocessing

The GLB pipeline needs the following stages:

1. Build the metadata dependency closure for every Timeline and model root.
2. Download/decode the closure and load all files into one Unity collection.
3. Resolve cross-file PPtrs by portable file identity, never by guessing a
   file index.
4. Select the referenced root Prefab or GameObject instead of exporting the
   entire collection.
5. Export hierarchy, local transforms, meshes, materials, textures, bones, and
   skin weights into GLB.
6. Convert Unity shaders and material properties to WebGL-compatible PBR,
   unlit, transparent, and emissive materials.
7. Decode AnimationClip streamed, dense, constant, and PPtr curves, then map
   Unity binding paths to GLB node paths.
8. Resolve monitor meshes, video names, audio cues, effects, and character IDs
   referenced by Timeline tracks.
9. Emit a browser manifest that maps Timeline references to GLB nodes and
   media URLs.

## Proposed browser package

```text
streaming_live/<id>/
  manifest.json
  timeline.playable.json
  scene/<stage>.glb
  character/<character>.glb
  animation/<clip>.glb
  texture/*
  video/*
  audio/*
```

GLB should be the browser-facing format. FBX may remain as an optional local
diagnostic/intermediate artifact, but it should not be required by the browser.

The manifest should contain at least:

- asset and Timeline identifiers
- dependency and source Bundle names
- GLB URLs
- Unity PPtr to exported asset mappings
- Unity hierarchy path to GLB node mappings
- coordinate-system and unit conversion information
- animation clip and track mappings
- monitor mesh and video-texture mappings
- audio cue and media mappings
- unsupported-track diagnostics

## Implementation order

1. Add dependency-aware, multi-Bundle loading to the extractor.
2. Add root-scoped static scene export and a deterministic GLB writer/converter.
3. Add material/shader conversion and texture deduplication.
4. Add character skeleton and AnimationClip export.
5. Add Timeline reference resolution and the browser manifest.
6. Implement browser handlers for lighting, effects, monitors, media, mobs,
   and stage switching.

The repository currently has no native GLB exporter or installed FBX-to-GLB
converter, so the GLB implementation must either add a controlled build-time
converter or expose enough Unity mesh/animation data to write GLB directly.
