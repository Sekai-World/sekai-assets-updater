# Phase 5 real-model acceptance — 2026-10-27

## Scope and result

**Overall: blocked, not accepted for publication.** Real JP/iOS model and
animation inputs were downloaded and exercised with Python 3.12 and the mainline
lockfile's `unity-rs==0.6.1`. These are model bundles, not MC or Streaming Live
Timeline substitutes. Phase 6 Timeline/package work is not part of this branch.

The strict real-model tests require `report.publishable` and never pass
`allow_incomplete=True`. Both initially failed: the stage had seven missing
texture targets and the character had two unresolved Shader dependencies.
Investigation found that `AssetBundle.dependencies` had incorrectly been used
as serialized-file `m_Externals` slots. These are different tables.

The 0.6.1 public Python binding exposes no serialized externals accessor.
Consequently, bundle dependency order is no longer used to infer PPtr targets.
Without an explicitly supplied, authoritative serialized external table,
nonzero file IDs remain unresolved and publication stays blocked. No global
path-ID search, basename matching, builtin-ID heuristic, or permissive export
can substitute for that table. Restoring this capability requires an upstream
binding API or another separately reviewed authoritative metadata provider;
this change does not introduce a second UnityFS/serialized-file parser.

## Inputs and integrity

`tests/fixtures/glb_phase5_real_samples.json` records the JP/iOS metadata revision
`6.8.0.50`, its SHA-256, the three roots, dependency closures, and all 18 decoded
Bundle SHA-256 hashes. Combined metadata size: 22,525,288 bytes. Bundle payloads,
credentials, downloaded configuration, absolute local paths, and generated GLBs
are not committed. Metadata `hash` and CRC are provenance, not independently
verified downloaded-byte digests.

| Sample | Bundle root | Selected scene root | Closure |
| --- | --- | --- | --- |
| Stage | `streaming_live/model/stage/base_007_sp_live` | `svl_mdl_stage_007_sp_live_kyoutu_stage` | 16 Bundles / 22,109,664 bytes |
| Character | `live_pv/model/characterv2/body/99/0042/boys` | `mdl_bdy_COM_41` | 2 Bundles / 760,973 bytes |
| Animation | `model3d/motion/result/36` | Four AnimationClip objects | 1 Bundle / 36,489 bytes |

Closures overlap at `shader/live`. The character and result-motion inputs are
general character/animation evidence, not evidence for Streaming Live Timeline.

## Correctness changes and checks

- Prefer native Mesh positions, normals, UVs, and triangle indices. Mirror X and
  reverse winding exactly once; the native OBJ writer already does both.
- Preserve native vertex identity and every submesh/material slot. The OBJ
  compatibility path explicitly remaps weights across reordered/seam vertices.
- Decode four native float32 weights and four uint32 bone indices per vertex;
  accept normalization only within `1e-3`, reject invalid positive-weight joints,
  and retain the original bone-slot order.
- Export native bind poses as `C * B * C`, column-major,
  `C = diag(-1, 1, 1, 1)`. Attach `skin` to the mesh node and joint/weight
  attributes to every primitive. Unresolved or cyclic bone parent chains fail
  closed; caches use full file/path identity.
- Resolve same-file AssetBundle container PPtrs relative to their owner file.
  Nonzero container file IDs are not guessed.
- The real character contains a Body mesh with 4,630 vertices, 80 bones and 80
  bind poses. Each native weight/index stream is 74,080 bytes; the largest
  source weight-sum deviation is approximately `1.10e-7`.
- Strict tests independently decode final GLB accessors, compare native
  geometry/submesh/material mappings and normalized skin data, and compare
  Unity-side versus GLB-side CPU skinning at first/middle/last vertices. They do
  not incorrectly require the current scene pose to be the bind pose.

### Local diagnostic results, not publication acceptance

After removing dependency-order inference, default export rejects both real
roots. Explicit local diagnostic exports produced the following repeatable
artifacts. Their successful structural checks do **not** override unresolved
required references or establish complete texture fidelity.

| Metric | Stage | Character |
| --- | --- | --- |
| Nodes | 172 | 89 |
| Meshes / primitives | 47 / 50 | 1 / 1 |
| Triangle-list indices | 125,469 | 19,503 |
| Materials (including the default) | 37 | 2 |
| Embedded PNGs | 1 | 1 |
| Skins / joints | 0 / 0 | 1 / 80 |
| Unresolved external errors | 7 | 1 |
| Default export / publication | Refused / blocked | Refused / blocked |

Both diagnostic GLBs are byte-identical across repeated exports; embedded PNGs
decode and are payload-deduplicated. Source vertex samples, complete triangle
lists, submesh counts, and material-slot mappings match the output. The
character's final JOINTS/WEIGHTS and all 80 native bind poses also match;
independent CPU skinning agrees for vertices `0`, `2315`, and `4629` within
`1e-4`. These are executed checks on diagnostic output, not skipped assertions.

Diagnostic GLB SHA-256:

- Stage (2,170,060 bytes):
  `947502ea0438b8239b6aa12c5fb7e383f885328019e59ec02f3b65d1ba09c099`
- Character (500,112 bytes):
  `ec5daf2b86b7adad842b84849e49c498956bf90f2bdbaab9c36fa3ae221c0117`

## Animation boundary

All four real clips (`motion_loop`, `face`, `motion`, `face_loop`) decode
deterministically and their independently addressable JSON artifacts survive
persist/load round trips. They contain streamed and constant **muscle-space**
values and explicit `animation_unsupported_binding` diagnostics. This sample
does not establish dense-curve or PPtr-curve coverage, required node-binding
mapping, or playable GLB animation channels. The roadmap's complete animation
acceptance therefore remains unmet; synthetic curve tests are not a replacement
for the missing real coverage.

## Reproduction

Install the frozen environment with `uv sync --all-groups --frozen`. Obtain the
authorized local Bundle inputs listed in the provenance fixture, verify their
decoded SHA-256 hashes, then set these variables. Collection values are
semicolon-separated `bundle-name=/absolute/path` entries, including each listed
dependency; the character collection is independent of the stage collection.

```sh
export SEKAI_GLB_COLLECTION='stage-bundle=/absolute/stage;dependency=/absolute/dependency;...'
export SEKAI_GLB_ROOT_NAME='svl_mdl_stage_007_sp_live_kyoutu_stage'
export SEKAI_GLB_CHARACTER_COLLECTION='live_pv/model/characterv2/body/99/0042/boys=/absolute/body;shader/live=/absolute/shader'
export SEKAI_GLB_CHARACTER_ROOT_NAME='mdl_bdy_COM_41'
.venv/bin/python -m pytest tests/glb_phase5_real_model_test.py -vv --tb=short
```

Use the actual fixture Bundle names, not the illustrative stage placeholders.
With 0.6.1 and no authoritative external table, this command is expected to fail
at publishability. Explicitly configured missing paths also fail; absent input
variables alone cause opt-in tests to skip. A default test run that skips these
cases must not be reported as successful real-model acceptance.

For the separate animation JSON round-trip check:

```sh
export SEKAI_GLB_ANIMATION='model3d/motion/result/36=/absolute/motion'
.venv/bin/python -m pytest tests/glb_animation_test.py -k real_animation -v
```

Local evidence is retained under
`cache/glb-phase5-real-model-acceptance-20261027/`; it is intentionally ignored.
Diagnostic GLBs, if generated with incomplete references, are local investigation
artifacts only, never accepted publication products.

## Final verification

- Full pytest run with real character-skeleton and animation inputs:
  **922 passed, 6 skipped, 1 subtest passed**. Strict stage/character input
  variables were unset in this run; its skips are not acceptance evidence.
- Separate strict stage/character run with all cached inputs: **2 failed,
  0 skipped**, both at the unchanged publication gate. This is the recorded
  blocked result, not a passing acceptance run.
- Repository-wide Ruff lint and formatting checks passed (146 formatted files).
- Configured Pyright check: **0 errors, 0 warnings**. Its configured include
  list is not a claim of exhaustive static checking of every GLB module.
- Frozen offline lockfile check and `git diff --check` passed. No dependency
  versions were changed.

Full Phase 5 publication acceptance and the corresponding Phase 6 integrated
publication claim remain blocked until authoritative native externals metadata
and the missing real animation binding/curve coverage are available.
