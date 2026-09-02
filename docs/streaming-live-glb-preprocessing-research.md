# Streaming Live GLB and Live2D Preprocessing Research

Date: 2026-09-01

Roadmap: [`STREAMING_LIVE_GLB_ROADMAP.md`](STREAMING_LIVE_GLB_ROADMAP.md)

## Scope

This note records the investigation of browser playback for Project Sekai
Streaming Live resources. It covers the local extractor, the current JP
AssetBundle metadata, decoded files published in the S3-compatible storage,
current master DB business tables used for Live2D associations, and the
available APK reverse-engineering reports.

No credentials, signed cookies, or raw downloaded bundles are stored here.
The obsolete `sekai-master-db-diff/assetList.json` is deliberately excluded
from this investigation and is not evidence for Live2D associations.

## Findings

### GLB AssetBundle loading

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

### Live2D Bundle and model evidence

The same current JP metadata sample is version `6.8.0.10`. The verified Live2D
Bundle records in the sample are:

- `model/v2_01ichika_unit`
- `model/v2_01ichika_april2025`
- `motion/v2_01ichika_motion_base`
- `model/v2_20mizuki_unit`
- `motion/v2_20mizuki_motion_base`

These names are evidence for the Bundle fixture, not a license to infer a
model-variant-to-motion relation from a substring. Live2D-only extraction must
use the explicit `live2d/` Bundle scope/ownership check (or an equivalent
explicit classification); an unrelated path that contains `motion` must remain
outside that behavior.

The observed `model3.json` `FileReferences` contains `Moc`, `Textures`, and
`Physics`, but no `Motions` or `Expressions`. Motion and facial associations
must therefore not be fabricated inside the model descriptor.

The character motion base is a shared character-level library, not a private
set to copy into each model. The observed counts are:

| Character motion base | Facials | Motions |
| --- | ---: | ---: |
| Ichika | 73 | 271 |
| Mizuki | 76 | 278 |

`Facials` means facial Motion clips and currently remains
`facial/*.motion3.json`. The sample includes constant curves for Ichika 60/73
and Mizuki 62/76, but that does not change the default Motion3 policy: constant
and dynamic Facials are both retained as Motion3. Cubism `Expressions` refers
to `.exp3.json` support and is a different concept; no Facial is force-converted
to `.exp3.json` by this preprocessing track.

### Master DB evidence and join caveats

The relevant current business tables are `character2ds`, `costume2ds`,
`systemLive2ds`, `bondsLive2ds`, `bondsRankUpLive2ds`, and
`loginBonusLive2ds`.

- `costume2ds.character2dId -> character2ds` is the direct model/character
  association. This exact join is the model-side anchor for the index.
- Applicable business records use `characterId + motion + expression` to record
  actual character-level use. Here `expression` is the name of a facial clip
  used by the business record; it is not a Cubism Expressions asset and does
  not imply an `.exp3.json` file.
- These tables do not contain a direct specific-model-variant-to-motion-Bundle
  field. A model's motion-set link therefore remains a candidate until naming
  evidence and other auditable evidence verify it.
- The master character `assetName` and the observed `v2_*` model/motion naming
  can produce an auditable `derived` or `verified` candidate. The source values,
  rule, and status must be retained; a candidate must not be presented as a
  direct database relation.
- `*_back_motion_base` and `*_still_*_motion_base` cannot be assigned to a
  character automatically from `characterId` alone. They require separate
  verification or remain ambiguous.

The resulting Live2D representation should be
`model -> Character2D -> motion-set candidate -> known clips`. Model outputs
and shared motion-set outputs remain separate in shared storage. The index is
an additive, lightweight association document: it must not duplicate Motion or
Facial files, introduce a Live2D multi-Bundle Unity collection, or replace the
existing atomic JSON output and remote `model_list` authority.

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

### Timeline and animation data (GLB)

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

This GLB observation concerns Unity AnimationClip dependencies. The selected
stage Bundles contain no useful AnimationClip collection of their own, so GLB
character motion and related model resources must be resolved from their
separate model/motion dependencies. That dependency resolution must not be
confused with the separate Live2D shared-motion index: Live2D does not join
model and motion files through the GLB multi-Bundle collection.

## Required GLB preprocessing

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

## Proposed GLB browser package

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
This package is GLB-only; Live2D model and shared motion-set outputs remain
separate shared-storage records and are not placed in this package.

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

## Recommended Live2D preprocessing track

The Live2D work can proceed independently of GLB collection loading:

1. Select only the explicit Live2D Bundle scope and fixture the verified
   `6.8.0.10` names listed above; do not classify an unrelated `motion` path by
   substring.
2. Join `costume2ds.character2dId -> character2ds` for the direct model and
   Character2D relation, then retain `characterId + motion + expression` from
   the applicable business rows in `systemLive2ds`, `bondsLive2ds`,
   `bondsRankUpLive2ds`, and `loginBonusLive2ds` as business-use evidence.
3. Compare master character `assetName` with the observed `v2_*` names to
   produce auditable `derived`/`verified` candidates. Do not auto-assign
   `*_back_motion_base` or `*_still_*_motion_base` from `characterId` alone.
4. Validate `BuildMotionData` and the observed counts (Ichika: 73 Facials/271
   Motions; Mizuki: 76 Facials/278 Motions). Keep Facials as
   `facial/*.motion3.json`, including both constant and dynamic curves; the
   business `expression` field is not a Cubism `.exp3.json` conversion request.
5. Emit separate model and shared motion-set outputs plus a lightweight
   `model -> Character2D -> motion-set candidate -> known clips` index. Use
   independent incremental keys for model output, motion-set output, and the
   index, and atomically publish the index only after its references validate.

The index is additive: existing atomic JSON output and remote `model_list`
remain authoritative. No shared Motion is copied into every model, and no
Live2D output requires a GLB collection ID.

## GLB implementation order

1. Add dependency-aware, multi-Bundle loading to the extractor.
2. Add root-scoped static scene export and a deterministic GLB writer/converter.
3. Add material/shader conversion and texture deduplication.
4. Add character skeleton and AnimationClip export.
5. Add Timeline reference resolution, the browser manifest, and publication
   validation.

The repository currently has no native GLB exporter or installed FBX-to-GLB
converter, so the GLB implementation must either add a controlled build-time
converter or expose enough Unity mesh/animation data to write GLB directly.
Browser handlers for lighting, effects, monitors, media, mobs, and stage
switching remain a future runtime concern rather than a preprocessing step.
