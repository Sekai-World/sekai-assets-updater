# Streaming Live GLB and Live2D Preprocessing Roadmap

Status: proposed implementation plan
Date: 2026-09-01

This roadmap turns the findings in
[`streaming-live-glb-preprocessing-research.md`](streaming-live-glb-preprocessing-research.md)
into an incremental, testable implementation plan. It is deliberately scoped to
offline preprocessing in this repository. The GLB track covers metadata closure,
multi-Bundle Unity collections, root-scoped export, and Timeline manifests. The
Live2D work is a separate lightweight track for shared motion sets, latest
public viewer assets, and local association audit data; it does not turn Live2D
into a GLB-style collection or physical package. A browser runtime consumes the
GLB outputs described here and is a follow-up project; it is not promised or
implemented by this roadmap.

## 1. Goals and invariants

### 1.1 Two different units

The **Bundle** remains the download and cache unit:

- one metadata bundle record maps to one URL, checksum, cache entry, and retry
  item;
- unchanged bundles remain reusable even when several logical assets reference
  them;
- download filtering, priority, concurrency, and cache invalidation continue
  to operate on `bundleName`.

The **logical asset package** is the extraction unit for 3D and GLB work. A
package consists of one or more roots plus the transitive metadata dependency
closure required to load and export those roots. Therefore, a stage, character,
or Timeline must not be extracted as an isolated Bundle merely because its root
object happens to be stored there.

Formally, for a GLB purpose `p` and roots `R`, the extractor receives:

```text
package(p, R) = roots R + closure(metadata_dependencies(R), p)
```

The same Bundle may be:

- extracted alone for a `standard` request;
- part of a shared collection for a `scene3d` package;
- part of a different collection for a `timeline3d` package;
- included in both collections without being downloaded twice; or
- omitted from a package when it is excluded by profile policy, with an
  explicit diagnostic rather than an implicit best-effort guess.

This package and collection definition is for the GLB purposes below. Live2D
model outputs and character-level motion sets deliberately remain separate
Bundle/output records and are joined by the latest local audit index instead of
a multi-Bundle Unity collection.

### 1.2 Live2D boundary: shared motion sets and latest local audit data

Live2D has a separate, explicit boundary from the GLB collection pipeline:

- Live2D-only handling is gated by the explicit `live2d/` Bundle
  scope/namespace (or an equivalent explicit ownership classification) used by
  the extractor. A path containing `motion` is not sufficient to classify a
  Bundle as Live2D.
- The obsolete `sekai-master-db-diff/assetList.json` is excluded from Live2D
  evidence and association inputs. Use current JP Bundle metadata and the
  explicitly named master business tables instead.
- Model Bundles produce model-facing outputs (`Moc`, `Textures`, and `Physics`
  references in `model3.json`); character-level motion Bundles produce shared
  `Motions` and `Facials` outputs. These remain separate physical outputs in
  shared storage.
- `model3.json` is not extended by inference: its observed `FileReferences`
  contains `Moc`, `Textures`, and `Physics`, not `Motions` or `Expressions`.
- `Facials` are facial Motion clips and remain
  `facial/*.motion3.json`. Constant curves do not justify converting them to
  Cubism `.exp3.json`; dynamic Facials must also remain Motion3. The master DB
  `expression` field is a business facial-clip name, not a Cubism Expressions
  file reference.
- The latest local association audit records the
  `model -> Character2D -> motion-set association -> known clips` relation,
  source evidence, and the exact join inputs. It does not claim a direct
  specific-model-variant-to-motion-Bundle field where the master data has none.
- Association index and selection-manifest inputs are optional. The dispatcher
  uses a directly supplied `association_index` first, then an
  `association_index_path`, then the configured selection manifest, and falls
  back to automatic discovery when none is supplied.
- Automatic discovery selects exact `live2d/model/` and `live2d/motion/` Bundle
  names from current metadata, derives output directories from their first
  matching metadata `paths` entries, and requires either a configured local
  directory containing the six named Live2D master-data JSON tables or an
  online master-data archive URL. The local directory takes precedence. A
  GitHub repository URL is resolved to one latest branch archive download per
  automatic run (branch defaults to `main`); it does not use the GitHub API or
  six raw-table requests, and downloaded master data is temporary and never
  uploaded.
- Latest branch mode intentionally follows upstream changes instead of pinning
  a revision, so it trades reproducibility for current data. A local directory,
  explicit index, or selection manifest remains available when repeatability is
  required.
- A shared motion set is never copied into every model output. Live2D does not
  use the GLB multi-Bundle collection dependency, and it does not create a
  physical logical package merely to associate model and motion outputs.
- The temporary standalone `live2d-associated/v1` namespace publishes only the
  latest public viewer assets: `model_list.json`, `model/`, `motion/`, and
  `facial/` (selected paths may be nested). When legacy retires, it is renamed
  to `live2d`; legacy `live2d/` output remains untouched during coexistence.
- `model_list.json` is generated by the standalone associated pipeline, not the
  legacy `live2d/model_list.json`. It is legacy-shaped, adds resolved motion-set
  file paths and clip filenames, and is uploaded after the assets as the public
  ready marker.
- Detailed association index data, evidence, rule codes, diagnostics,
  checksums, source rows, and Bundle metadata are retained only as the latest
  local audit data. They are never uploaded and the viewer does not use them.
- The active associated pipeline has no `candidates`, history, `current.json`,
  `candidate.json`, rollout state, pointers, rollback history, or remote
  revision history, locally or remotely.

The current JP `6.8.0.10` evidence and its naming/join caveats are recorded in
the [research note](streaming-live-glb-preprocessing-research.md).

### 1.3 Extraction purposes

Every extraction request has exactly one declared purpose. Purpose is not
inferred from a path containing a word such as `motion`.

| Purpose | Roots and result | Collection behavior |
| --- | --- | --- |
| `standard` | Existing per-Bundle asset extraction and media outputs | One Bundle at a time; preserves current behavior and output conventions |
| `live2d` | Live2D model outputs, shared character motion sets, and latest local association audit data | Processes explicit model/motion inputs separately; no multi-Bundle collection and no physical model package |
| `scene3d` | A static scene/stage root exported to a root-scoped GLB | Aggregates stage, shared stage, shader, material, and texture dependencies |
| `timeline3d` | A Timeline plus all referenced scene, character, animation, monitor, and media assets, with a timeline manifest | Aggregates the complete logical package; roots remain separately addressable |
| `composite` | A declared set of roots and purpose-specific outputs, for example scene + characters + timeline | Aggregates the union of closures, while retaining per-purpose root and diagnostic records |

For GLB purposes, one physical Bundle can consequently have multiple extraction
records. Those records must be keyed by `(package_id, purpose, root_id)` rather
than by `bundleName`, while the download/cache record remains keyed by
`bundleName`. Live2D does not create a collection record: model outputs,
shared motion-set outputs, and latest local audit records have independent
stable keys and are linked by references.

## 2. Proposed data model: `ExtractionPlan` (GLB)

The GLB plan is a serializable, deterministic description of collection work. It
should be constructed before extraction and passed through the pipeline rather
than reconstructing intent from filesystem names. A `purpose="live2d"` value
must not be interpreted as permission to build a Unity collection; Live2D uses
the separate contract below.

```python
ExtractionPlan(
    plan_version=1,
    package_id="streaming_live/0006_lon_vbs_01",
    purpose="timeline3d",
    profile="streaming_live_v1",
    roots=[RootRef(kind="timeline", bundle_name=..., path_id=..., name=...)],
    bundles=[BundleRef(bundle_name=..., checksum=..., source=..., role=...)],
    dependency_edges=[DependencyEdge(source=..., target=..., reason=...)],
    excluded_dependencies=[...],
    missing_dependencies=[...],
    diagnostics=[...],
)
```

The concrete Python representation may use frozen dataclasses or validated
dictionaries, but these fields are contractual:

- `plan_version`, `package_id`, `purpose`, and `profile` identify intent;
- `roots` identifies the requested Unity asset by stable metadata/container
  identity and, when known, `(file_index, path_id)`;
- `bundles` is a sorted, duplicate-free list of physical inputs, including
  source metadata identity and checksum;
- `dependency_edges` records why each dependency was included (`metadata`,
  `root_reference`, `shader`, `texture`, `animation`, or `media`);
- `missing_dependencies`, `excluded_dependencies`, and `diagnostics` are
  machine-readable and never hidden in log text only;
- `collection_id` or an equivalent stable hash may be derived from the ordered
  bundle identities and plan version, but must not include temporary paths.

Planning must be deterministic: sort roots and bundle names, preserve stable
edge reasons, and use a stable traversal order. A plan can be persisted beside
the package to make retries and output comparison reproducible.

### 2.1 Live2D association index contract (latest local audit data)

The Live2D track uses a small association document rather than an
`ExtractionPlan` collection. A representative logical shape is:

```python
Live2DIndex(
    index_version=1,
    metadata_version="6.8.0.10",
    master_db_version=...,
    models=[
        {
            "model_output_id": ...,
            "model_bundle": {"name": ..., "checksum": ...},
            "character2d_id": ...,
            "character_id": ...,
            "motion_sets": [
                {
                    "motion_bundle": {"name": ..., "checksum": ...},
                    "evidence": [...],
                    "known_clips": {
                        "motions": [...],
                        "facials": [...],
                    },
                },
            ],
        },
    ],
    diagnostics=[...],
)
```

The exact serialization may differ, but the latest local audit contract must
preserve the `costume2ds.character2dId -> character2ds` join, the source
business rows, Bundle names/checksums, resolved motion-set links, known
Motion/Facial clip names, and the evidence used for each association. It must
not invent a `model3.json` `Motions`/`Expressions` section, treat the business
`expression` field as `.exp3.json`, or copy shared motion files into model
outputs. This detailed index is local audit data only: it is never uploaded,
and the viewer does not use it. Only the latest audit data is retained; the
active pipeline has no candidates, history, current-pointer files, rollout
state, rollback history, or remote revision history.

The dispatcher accepts either explicit latest-audit input or automatic input.
The precedence is `association_index`, `association_index_path`, configured
selection manifest, then automatic Bundle discovery. Automatic discovery uses
the first matching `paths` entry for each selected model or motion Bundle and
loads the six Live2D master-data tables from the configured local directory or
from one run-scoped latest branch archive; no pre-built index or manifest is
required. Explicit index and selection inputs never download the online master
archive.

## 3. GLB dependency closure and reference validation

This section is exclusively for `scene3d`, `timeline3d`, and `composite` GLB
work. Live2D model/motion association is intentionally not a metadata closure
or a multi-Bundle collection dependency; it follows the independent track in
Section 6.

### 3.1 L0: metadata dependency graph

L0 uses the asset metadata as the authoritative graph for bundle-to-bundle
dependencies. For every selected root:

1. Resolve its owning Bundle from the metadata index.
2. Read the dependency list using the server's exact bundle identity, without
   normalizing away meaningful case or path components.
3. Traverse with BFS (preferred for useful level diagnostics) or iterative DFS
   (acceptable if it records the same ordered result).
4. Maintain `discovered`, `expanded`, and `parent/reason` sets keyed by canonical
   metadata bundle identity.
5. Expand each Bundle once, add each edge once, and produce a sorted closure.
6. Apply the purpose/profile exclusion policy while retaining the excluded edge
   and its parent in the plan.

The traversal must be iterative, not recursive, to avoid metadata graphs causing
Python recursion failures. A dependency listed more than once is one Bundle in
the closure. A self-edge or back-edge is a cycle diagnostic, not a reason to
loop forever. Cycles may be accepted when all nodes are present; the plan must
record them and the acceptance policy must be explicit.

### 3.2 Missing, cyclic, and excluded dependencies

Each dependency outcome is classified:

- **missing**: referenced by metadata but absent from the current metadata
  index; fail `scene3d`, `timeline3d`, and `composite` by default, and allow
  `standard` only under its existing missing-input behavior;
- **cycle**: encountered again on the current traversal path; record the cycle
  members and continue using the already discovered node, unless the profile
  marks cycles fatal;
- **excluded**: deliberately removed by include/exclude/profile policy; never
  silently substitute a similarly named Bundle. The plan fails if the excluded
  node is required for a selected root, otherwise it remains a warning;
- **filtered**: not selected as a root but still pulled into closure; this is
  normal and must be distinguishable from excluded.

Failures should happen at plan validation before expensive downloads when L0 is
available. A `--allow-incomplete-plan`/equivalent feature flag may be provided
for diagnostics, but incomplete output must be marked non-publishable.

### 3.3 L2: runtime PPtr validation

L0 proves that the likely Bundle inputs are present; it does not prove that the
serialized Unity objects can resolve their references. After all closure files
are loaded into one collection, L2 validates:

- every non-null PPtr used by a selected root and its export-reachable objects;
- file identity using portable source-file identity, never a guessed file index;
- target existence, target class/type, and expected use (Mesh, Material,
  Texture2D, AnimationClip, GameObject, and so on);
- external references crossing Bundle boundaries;
- Timeline PPtrs from `AnimationPlayableAsset` and other supported tracks.

The adapter must expose a collection-level resolver that can distinguish a
valid null pointer, a missing target, an invalid file identity, and an
unsupported target. L2 results are stored in the plan/package diagnostics with
source object identity and target identity. A required unresolved PPtr fails
the package; an optional unsupported track or decoration is retained as a
structured warning and makes the relevant feature incomplete.

## 4. Existing code landing points

The implementation should extend existing seams rather than introduce a second
download pipeline:

- **`updater/net/plan.py`**: retain Bundle-level `DownloadPlan`; add the
  metadata index/closure inputs or a planner-facing association from selected
  roots to required Bundles. Download planning must union all package closures,
  deduplicate by `bundleName`, and preserve include/exclude semantics.
- **`updater/pipeline.py`**: carry an `ExtractionPlan` or package work item
  through download, extraction, and upload queues. Keep download/cache work
  Bundle-oriented, but schedule collection extraction only after all required
  cached files are available. Define package-level failure and retry behavior.
- **`updater/extract/bundle.py`**: preserve the current `standard` per-Bundle
  path and add dispatch for plan-driven collection extraction. It is the
  asynchronous boundary for process-pool submission and output validation.
- **`updater/extract/sync_worker.py`**: load a collection, invoke root-scoped
  extraction, perform L2 checks, and write atomic package outputs. Existing
  media fan-out and cleanup remain available to `standard` extraction.
- **`updater/unity_rs_adapter.py`**: provide the narrow collection abstraction,
  portable file/object identity, collection-level PPtr resolution, object type
  reads, and root-scoped model/mesh access. Do not leak native `unity_rs`
  assumptions into planners or manifests.
- **Suggested new planner module** (for example
  **`updater/extract/planner.py`** or `updater/net/extraction_planner.py`):** own
  purpose profiles, root selection, L0 BFS/DFS, cycle/missing/exclusion
  classification, deterministic `ExtractionPlan` construction, and plan
  validation. Keep it mostly pure so graph behavior can be unit-tested without
  network or Unity native libraries.
- **Existing Live2D seams** (`updater/extract/bundle.py`,
  `updater/live2d/restore.py`, and `updater/postprocess/live2d_models.py`):
  preserve explicit Live2D scope gating, separate model/motion processing,
  `BuildMotionData` handling, aggregate-workspace recovery, atomic JSON output,
  and the standalone associated `model_list.json` publication. Keep legacy
  `live2d/model_list.json` untouched during coexistence. Add the latest local
  association audit index without routing Live2D through the GLB collection
  loader.
- **Suggested Live2D join/index module:** own the exact master-table joins,
  Bundle-name evidence, known-clip references, incremental keys, and atomic
  latest-audit publication. It may reuse Bundle cache files, but it must not
  create a logical Unity collection or copy a shared motion set into a model
  output.

## 5. Phased execution plan

Each phase is independently reviewable and should land behind a feature flag
until its acceptance criteria pass on fixtures and a representative current JP
metadata sample.

### Phase 0 — Contracts, fixtures, and observability

**Dependencies:** none.

**Work:**

1. Define `purpose`, profile names, plan version, root identity, Bundle identity,
   dependency edge, diagnostic, and output manifest schemas.
2. Capture sanitized metadata fixtures for one `scene3d` stage, one
   `timeline3d` Timeline, one character dependency, a shared shader dependency,
   a missing dependency, and a cycle. No credentials or signed URLs enter the
   repository.
3. Add structured plan/extraction logging with package ID, purpose, root ID,
   Bundle name, phase, and diagnostic code.
4. Add `ENABLE_STREAMING_LIVE_GLB_PREPROCESSING` (default `False`) and narrower
   flags for collection extraction and publication. Disabled behavior must be
   byte-for-byte compatible with the existing standard path where practical.

**Acceptance criteria:**

- Schemas reject unknown purposes, unsafe paths, duplicate Bundle identities,
  and roots without an owning Bundle.
- Fixtures can construct and serialize a deterministic plan on two runs.
- Logs and fixtures contain no credentials or raw transport payloads.
- The default configuration does not schedule any new GLB work.

### Phase 1 — Profiles and metadata closure planner

**Dependencies:** Phase 0 contracts and metadata fixtures.

**Work:**

1. Implement GLB profiles for `standard`, `scene3d`, `timeline3d`, and
   `composite`, including root selectors, required/optional dependency classes,
   exclusions, and failure policy. Keep the specialized `live2d` mode outside
   this closure planner; it uses the independent Live2D track below.
2. Implement iterative BFS/DFS over the metadata dependency index with stable
   ordering and parent edge reasons.
3. Implement missing, cycle, excluded, and filtered classifications and a
   `validate_plan()` step.
4. Add root selectors for a stage Bundle/container, Timeline identifier, and
   explicitly configured composite roots. Do not use substring path heuristics.
5. Expose the closure as a unionable list of Bundle download requirements.

**Acceptance criteria:**

- A selected stage produces the expected closure from the research fixture,
  including shader, shared stage, light, and camera-decoration dependencies.
- Missing and excluded required nodes fail before extraction; accepted cycles
  terminate and are recorded exactly once.
- Two roots sharing a Bundle produce one physical Bundle requirement and two
  logical root records.
- Include/exclude filters cannot remove an automatically required dependency
  without an explicit excluded-dependency diagnostic.
- Live2D model and motion outputs do not become closure members or a Phase 3
  collection prerequisite merely because a business record references them.

### Phase 2 — Bundle download and cache integration

**Dependencies:** Phase 1 deterministic closure and existing Bundle download
  and cache behavior.

**Work:**

1. Union plan closures into the existing `DownloadPlan` while keeping each
   `DownloadItem` keyed by Bundle name and checksum.
2. Ensure a Bundle required by multiple packages downloads once, is written to
   the existing cache location, and is reused by all package jobs.
3. Make cache validation cover every closure member, not only the selected root;
   unchanged metadata/checksum and missing cache files must use existing
   incremental-download rules.
4. Persist the plan-to-Bundle association separately from Bundle cache metadata,
   so cache invalidation does not destroy logical package identity.
5. Define package readiness: no collection extraction may start until every
   required cache file is present and validated.

**Acceptance criteria:**

- A multi-root run downloads each Bundle at most once and schedules every
  package after all of its closure members are ready.
- A missing or changed dependency is downloaded on an incremental run without
  forcing unrelated Bundles to redownload.
- Existing `standard` downloads, retries, temporary-file cleanup, and cache
  paths remain unchanged when the feature flag is disabled.
- A failed Bundle download prevents dependent package publication and leaves a
  retryable diagnostic rather than a partial success marker.

### Phase 3 — Multi-Bundle collection loading and PPtr validation (GLB only)

**Dependencies:** Phase 2 cache-ready package work items; adapter fixtures or a
supported `unity_rs` collection API.

**Work:**

1. Add collection loading in `unity_rs_adapter.py` with stable portable source
   identities and deterministic file ordering.
2. Resolve external references through the collection's actual file identity
   table. Never map a non-zero PPtr file ID to a file by position or filename
   guess.
3. Run L2 validation from selected roots through the reachable object graph;
   report missing targets, type mismatches, nulls, and unsupported references
   separately.
4. Add a collection-level extraction worker path in `sync_worker.py`, while
   retaining `load_bundle()` for existing one-Bundle consumers.
5. Record input Bundle checksums, Unity version, adapter version, and L0/L2
   summaries in the package manifest.

**Acceptance criteria:**

- The research collection (8 Unity input files) loads as one collection and
  resolves required cross-file references without file-index guessing.
- The stage-alone fixture produces an unresolved-reference diagnostic, while
  the combined fixture resolves the expected material/texture references.
- Required L2 failures make the package non-publishable and identify source and
  target identities; optional failures do not disappear into a generic warning.
- Existing Live2D and standard extraction tests still use the one-Bundle API;
  Live2D is not a prerequisite for this collection phase.

### Phase 4 — Static root-scoped GLB export

**Dependencies:** Phase 3 collection loading, L2 identity, and a chosen
controlled GLB writer/converter implementation.

**Work:**

1. Select the configured root Prefab/GameObject and export only its hierarchy,
   local transforms, meshes, bones, and skin weights. Do not export the entire
   loaded collection.
2. Define coordinate-system, handedness, unit scale, winding, and node naming
   conversion rules; make them explicit in the manifest.
3. Write a deterministic GLB artifact with stable node ordering and content
   hashes. Keep FBX as an optional diagnostic/intermediate artifact only.
4. Emit root and Unity object mappings (Bundle, file identity, path ID,
   hierarchy path to GLB node) for every exported object.
5. Publish only atomically completed GLB and manifest files; incomplete exports
   remain local diagnostics.

**Acceptance criteria:**

- `base_007_sp_live` exports a root-scoped scene rather than the complete
  collection, with size and node counts comparable to the research result.
- A package containing two roots emits separate stable root records and does
  not accidentally include unrelated scene roots.
- Repeating the export with the same inputs produces equivalent bytes or a
  documented canonicalization-equivalent result and identical mappings.
- A GLB can be parsed by an offline validator and all required mesh/material
  references are internally valid.

### Phase 5 — Materials, textures, skeletons, and animation

**Dependencies:** Phase 4 root-scoped GLB and object mappings.

**Work:**

1. Resolve and deduplicate Material, Texture2D/Sprite/TextureArray, and Shader
   objects across the collection by stable object identity and content hash.
2. Convert supported Unity shaders and properties to documented browser-facing
   PBR, unlit, transparent, and emissive material representations. Unsupported
   properties receive diagnostics and deterministic fallbacks.
3. Preserve texture color space, alpha mode, dimensions, and encoded output
   metadata; avoid treating Unity `.mat` or typetree JSON as browser-ready
   material files.
4. Export skeletons, bind poses, skin weights, and character roots from model
   dependencies.
5. Decode AnimationClip streamed, dense, constant, and PPtr curves; map Unity
   binding paths to GLB node paths; emit unsupported binding diagnostics.
6. Keep animation extraction independently addressable so a composite package
   can reuse a character or animation artifact without duplicating textures.

**Acceptance criteria:**

- The combined stage fixture has no unresolved required texture references and
  produces deduplicated material/texture entries.
- Supported opaque, transparent, unlit, and emissive fixtures pass material
  conversion checks; unsupported shader features are explicit, not silently
  misrepresented.
- A character fixture has valid joints, inverse bind matrices, and weights.
- At least one fixture each for streamed, dense, constant, and PPtr animation
  curves round-trips through the emitted mapping and has no orphaned required
  binding.

### Phase 6 — Timeline package, manifest, and publication validation

**Dependencies:** Phases 1–5, plus Timeline object fixtures and media metadata.

**Work:**

1. Resolve Timeline roots and supported tracks, including
   `AnimationPlayableAsset` AnimationClip PPtrs, character monitors, effects,
   lighting, color/intensity, global settings, mob, and stage-switch records.
2. Associate track references with scene nodes, character/animation GLBs,
   monitor meshes, video names, audio cues, and media URLs without guessing
   from filenames.
3. Emit the browser-facing `manifest.json`, `timeline.playable.json`, GLB
   references, media references, source Bundle list, PPtr mappings, hierarchy
   mappings, conversion metadata, and unsupported-track diagnostics.
4. Validate the manifest as a complete output contract, including relative URL
   safety, unique IDs, referential integrity, artifact hashes, and package
   completeness status.
5. Add `composite` publication that unions artifacts but preserves purpose and
   root boundaries. Browser handlers for effects, lighting, monitors, media,
   mobs, and stage switching are explicitly out of scope here.

**Acceptance criteria:**

- The modern Timeline fixture (including its AnimationClip PPtrs) yields a
  manifest with resolved supported track references and explicit diagnostics
  for unsupported tracks.
- Every manifest artifact URL exists, is contained within the package output,
  and matches its recorded hash; no package marked `publishable=false` reaches
  remote storage.
- A composite package can refer to the same character, texture, and animation
  artifact from multiple tracks without duplicate physical files.
- An independent offline schema/reference validator can reject a deliberately
  corrupted manifest and accept a complete fixture package.

## 6. Independent Live2D implementation track

This is an executable, lightweight track for the existing Live2D extraction and
post-processing path. It can reuse the ordinary Bundle cache and sanitized
metadata fixtures, but it has no dependency on GLB Phase 3 (or on any other GLB
collection phase). Its result is shared model/motion-set storage, latest public
viewer assets under `live2d-associated/v1`, and latest local association audit
data; it is not a Live2D multi-Bundle collection or a physical model package.

The track is ordered as `L2D-0 → L2D-1 → L2D-2 → L2D-3 → L2D-4`. Each step is
independently reviewable and must preserve legacy `live2d/model_list.json`
untouched during coexistence while generating the standalone associated
`model_list.json` as the public ready marker after viewer assets validate.

### L2D-0 — Scope, evidence fixtures, and contracts

**Dependencies:** none.

**Work:**

1. Add a sanitized current-JP metadata fixture for the verified sample at
   version `6.8.0.10`:

   - `model/v2_01ichika_unit`
   - `model/v2_01ichika_april2025`
   - `motion/v2_01ichika_motion_base`
   - `model/v2_20mizuki_unit`
   - `motion/v2_20mizuki_motion_base`

2. Define the explicit Live2D Bundle scope check. A generic `motion` path,
   unrelated Bundle, or filename substring must not trigger Live2D-only
   extraction.
3. Define versioned schemas for model-output records, shared motion-set
   records, association evidence, known clips, diagnostics, and the latest local
   association audit index. Keep model and motion outputs physically separate.
4. Record only sanitized metadata/master-table rows and output identities; do
   not put credentials, signed URLs, or raw Bundle payloads in fixtures.
   Do not use the obsolete `sekai-master-db-diff/assetList.json` as fixture or
   association evidence.

**Acceptance criteria:**

- The fixture contains the five verified Bundle names above without asserting a
  direct model-variant-to-motion-Bundle field that is not present in metadata.
- Scope tests prove that only the explicit Live2D scope receives Live2D-only
  handling.
- The contract represents observed `model3.json` `FileReferences` (`Moc`,
  `Textures`, and `Physics`) and does not add `Motions` or `Expressions`.
- Two serializations of the same evidence produce the same canonical latest
  local audit input, with no sensitive transport data.

### L2D-1 — Master-table joins and Bundle naming verification

**Dependencies:** L2D-0.

**Work:**

1. Load the current business tables `character2ds`, `costume2ds`,
   `systemLive2ds`, `bondsLive2ds`, `bondsRankUpLive2ds`, and
   `loginBonusLive2ds`.
2. Build the model/character relation with the exact
   `costume2ds.character2dId -> character2ds` join. Preserve the resulting
   Character2D and character identifiers rather than replacing the join with
   name matching.
3. Treat `characterId + motion + expression` in applicable business rows as
   actual role-use records. Store `expression` as the business facial-clip
   name; it is not evidence of a Cubism `.exp3.json` file.
4. Compare master character `assetName` values with the verified `v2_*`
   Bundle names to produce auditable motion-set associations. Store the source
   fields and reason for every association.
5. Leave `*_back_motion_base` and `*_still_*_motion_base` associations unresolved
   or explicitly ambiguous unless separate evidence verifies their ownership;
   never assign them from `characterId` alone.

**Acceptance criteria:**

- The exact `costume2ds -> character2ds` join yields the expected direct model
  and character relation, and missing keys are diagnostic rather than silently
  matched.
- Every association has provenance; no specific model variant is treated as
  resolved solely because its `characterId` matches a motion record.
- Special `back`/`still` motion-base names remain protected from automatic
  attribution.
- Business facial names and Cubism Expressions are distinct fields in the
  serialized evidence.

### L2D-2 — `BuildMotionData` validation and facial Motion3 policy

**Dependencies:** L2D-0 and the sanitized motion-output fixture from L2D-1.

**Work:**

1. Validate the extracted `BuildMotionData` structure, including its `Facials`
   and `Motions` groups, clip names, output paths, and referential diagnostics.
2. Assert the current character-level motion-base counts: Ichika has 73
   Facials and 271 Motions; Mizuki has 76 Facials and 278 Motions.
3. Include the observed constant-curve samples (Ichika 60/73 Facials and
   Mizuki 62/76 Facials) in validation, but keep every Facial at
   `facial/*.motion3.json`. Do not force a conversion to Cubism `.exp3.json`,
   including for constant curves; dynamic Facial clips must remain Motion3 as
   well.
4. Validate Motion3 curve/output integrity separately from any future Cubism
   Expressions support. A master `expression` value may identify a facial clip
   but cannot change its output type.

**Acceptance criteria:**

- The Ichika and Mizuki fixtures pass the stated Facials/Motions counts and
  retain all known clip names.
- The Ichika 60/73 and Mizuki 62/76 constant-curve samples, together with
  dynamic Facial samples, remain under `facial/` as `.motion3.json`; no default
  `.exp3.json` is emitted.
- Missing, malformed, or duplicate `BuildMotionData` entries produce stable
  diagnostics and cannot make an invalid motion set look complete.

### L2D-3 — Separate outputs, local audit index, and latest-only publication

**Dependencies:** L2D-1 and L2D-2.

**Work:**

1. Publish model outputs and shared character-level motion-set outputs as
   separate storage records. A model record references a shared motion set; it
   does not contain a copied Motion/Facial directory.
2. Emit the latest local audit index described in Section 2.1 with the chain
   `model -> Character2D -> motion-set association -> known clips`, Bundle
   identities/checksums, evidence, and diagnostics.
3. Generate `live2d-associated/v1/model_list.json` in the standalone associated
   pipeline, not with the legacy model-list generator. Preserve the legacy model
   shape and add resolved motion-set file paths and clip filenames.
4. Publish only the latest public viewer assets (`model/`, `motion/`, and
   `facial/`, with selected paths possibly nested), then upload the associated
   `model_list.json` as the public ready marker. Keep the detailed index,
   evidence, rule codes, diagnostics, checksums, source rows, and Bundle
   metadata only as the latest local audit data; the viewer does not use them.
5. The active associated pipeline has no `candidates`, history, `current.json`,
   `candidate.json`, rollout state, pointers, rollback history, or remote
   revision history, locally or remotely.

**Acceptance criteria:**

- Multiple model records can reference one physical shared motion set without
  duplicate Motion/Facial files.
- The latest local audit index has no dangling model, motion-set, or known-clip
  reference and records the evidence for each association.
- A failed index validation leaves legacy output and valid associated
  model/motion outputs intact; no incomplete associated `model_list.json` is
  published.
- No Live2D output requires a GLB collection ID or a multi-Bundle Unity load.

### L2D-4 — Incremental keys and publication acceptance

**Dependencies:** L2D-3.

**Work:**

1. Define stable incremental keys, for example:

   - `model_key = hash(metadata_version, model_bundle_name, model_checksum,
     model_schema_version)`;
   - `motion_set_key = hash(metadata_version, motion_bundle_name,
     motion_checksum, BuildMotionData_schema_version)`;
   - `index_key = hash(metadata_version, master_db_version, index_version,
     sorted joins, known clips, and referenced output keys)`.

2. Rebuild only the affected physical output: a changed model invalidates its
   model key, a changed shared motion Bundle invalidates that motion set and
   dependent audit records, and a master-table/naming change can invalidate the
   latest local audit data without copying or rebuilding unchanged motion files.
3. Make ambiguity and validation failure non-publishable for the affected
   association while allowing independent valid outputs to remain reusable.
4. Publish the latest viewer assets under the temporary standalone
   `live2d-associated/v1` namespace and compare canonical JSON and keys before
   publishing its `model_list.json` ready marker.

**Acceptance criteria:**

- Repeating an unchanged run reuses the same model and motion-set keys and
  performs no physical duplication.
- Changing one model does not rebuild an unrelated shared motion set; changing
  one motion set updates only its dependents and the latest local audit data.
- A changed master DB join updates the latest local audit data deterministically
  without changing valid model/motion output bytes.
- Publication is atomic, and disabling the associated track leaves legacy
  `live2d/` output available; the GLB collection phases remain unaffected.

## 7. Compatibility and feature flags

The following flags are proposed names; final names should follow the existing
configuration conventions:

```text
ENABLE_STREAMING_LIVE_GLB_PREPROCESSING = false
ENABLE_EXTRACTION_PLANS = false
ENABLE_MULTIBUNDLE_COLLECTIONS = false
ENABLE_STATIC_GLB_EXPORT = false
ENABLE_GLB_MATERIALS = false
ENABLE_GLB_ANIMATIONS = false
ENABLE_TIMELINE_MANIFEST = false
ENABLE_LIVE2D_ASSOCIATED_PIPELINE = false
ALLOW_INCOMPLETE_EXTRACTION = false
```

Rules:

- all flags default off until the corresponding phase is accepted;
- `standard` remains the default purpose and continues to use per-Bundle
  extraction, including the existing explicit Live2D scope gating;
- enabling a later GLB flag automatically requires earlier GLB phases or fails
  fast with a configuration error; `ENABLE_LIVE2D_ASSOCIATED_PIPELINE` is
  independent of GLB collection loading and publishes only the associated
  latest viewer assets while keeping audit data local;
- `--mode live2d|charts` and existing specialized behavior are not redefined by
  this roadmap;
- feature flags must be visible in the relevant plan, index, or output
  manifest so an artifact cannot be mistaken for a fully processed package;
- no silent fallback from collection extraction to single-Bundle GLB export is
  allowed for `scene3d` or `timeline3d`. A diagnostic fallback may be enabled
  only for local debugging and must be non-publishable.

## 8. Data and output contracts

### 8.1 Inputs

The GLB planner consumes current asset metadata, game/version metadata,
configured Bundle URL/cache rules, purpose/profile configuration, and explicit
roots. It must not require browser state. Raw downloaded Bundles remain
private/cache inputs and are not copied into published GLB packages.

The Live2D index consumes the verified current-JP Bundle metadata, the current
master DB business tables, validated model/motion outputs, and explicit join
and naming evidence. Its inputs are model Bundles and shared character-level
motion Bundles, not a GLB dependency closure. The latest local audit index
preserves the resolved associations, evidence, source rows, diagnostics,
checksums, and ambiguity needed for audit without becoming viewer input.

### 8.2 GLB package layout

The exact path may be configured, but the logical contract is:

```text
streaming_live/<package_id>/
  extraction-plan.json
  manifest.json
  timeline.playable.json                 # timeline3d/composite when present
  scene/<root-id>.glb
  character/<root-id>.glb
  animation/<clip-id>.glb                 # or the chosen animation contract
  texture/<content-hash>.<format>
  material/<material-id>.json              # optional diagnostic/contract data
  media/video/*                            # references or derived outputs
  media/audio/*                            # references or derived outputs
  diagnostics.json
```

`manifest.json` must include package/purpose/profile/version, source Bundle
names and checksums, Unity and adapter versions, root records, artifact URLs and
hashes, PPtr and hierarchy mappings, coordinate/unit conversion, animation and
track mappings, monitor/video/audio mappings, closure status, and unsupported
feature diagnostics. JSON ordering and numeric formatting should be canonical
where deterministic byte comparison is expected.

GLB is the browser-facing geometry format. FBX and Unity typetree files may be
retained as opt-in local diagnostics, but consumers must not need them. The
manifest is an output contract for a future browser runtime, not an assertion
that this repository implements that runtime.

### 8.3 Live2D associated public viewer contract

The associated output is a temporary standalone public namespace, not a
physical Unity package or collection:

```text
live2d-associated/v1/
  model_list.json                    # standalone associated ready marker
  model/                             # selected paths may be nested
  motion/                            # selected paths may be nested
  facial/                            # selected paths may be nested
```

The public remote contains only the latest public viewer assets in this layout.
`model_list.json` is generated by the standalone new pipeline, not the legacy
`live2d/model_list.json`. It is legacy-shaped, adds resolved motion-set file
paths and clip filenames, and is uploaded after the model, motion, and facial
assets as the public ready marker.

Detailed association index data, evidence, rule codes, diagnostics, checksums,
source rows, and Bundle metadata are retained only as the latest local audit
data. They are never uploaded, and the viewer does not use them. The active
associated pipeline has no `candidates`, history, `current.json`,
`candidate.json`, rollout state, pointers, rollback history, or remote revision
history, locally or remotely.

When legacy retires, the temporary namespace is renamed to `live2d`. During
coexistence, legacy `live2d/` output remains untouched. No Live2D output
requires a GLB `collection_id` or causes a shared motion set to be copied into
a model directory.

## 9. Error handling and publication rules

Use stable diagnostic codes and severity (`error`, `warning`, `info`) rather than
parsing log messages. At minimum define codes for metadata missing, dependency
cycle, dependency excluded, cache missing, Bundle download failure, collection
load failure, unresolved PPtr, PPtr type mismatch, unsupported Unity object,
unsupported shader/track, invalid GLB, manifest integrity failure,
`live2d_scope_mismatch`, `live2d_join_missing`, `live2d_mapping_ambiguous`,
`live2d_build_motion_invalid`, `live2d_facial_format_invalid`, and
`live2d_index_integrity`.

- Plan errors stop before download where possible.
- For GLB, Bundle errors fail only the packages that require that Bundle, while
  allowing independent standard work to finish according to current pipeline
  behavior.
- For GLB, required extraction/reference/export errors make the package
  non-publishable.
- Optional unsupported features produce warnings and an incomplete feature list.
- Every GLB package output file is written to a temporary sibling and renamed
  atomically; manifests are written last and are the commit marker for a package.
- GLB upload must be package-aware: upload `manifest.json` only after all
  referenced artifacts validate, and never upload a partial package as current.
- Retries are idempotent by package ID and artifact hash. A retry may reuse
  valid cache files and already completed local artifacts.
- Live2D mapping ambiguity or invalid `BuildMotionData` blocks the affected
  associated publication, but does not invalidate unrelated valid model or
  shared motion-set outputs. The associated `model_list.json` is uploaded only
  after the latest viewer assets validate; legacy `live2d/model_list.json`
  remains independent and untouched.

## 10. Testing strategy

Testing should be layered and runnable without network access whenever possible:

1. **Pure planner unit tests:** profile selection, deterministic BFS/DFS,
   deduplication, cycles, missing nodes, excluded required/optional nodes,
   include/exclude interaction, and union of packages.
2. **Contract tests:** schema validation, canonical serialization, safe package
   paths, diagnostic codes, manifest referential integrity, and feature-flag
   compatibility.
3. **Cache/download integration tests:** shared dependency downloaded once,
   changed/missing closure member redownloaded, package readiness, retry, and
   cancellation cleanup using mocked transport.
4. **Adapter tests:** synthetic or checked-in sanitized Unity fixtures for
   portable identities, same-file and cross-file PPtrs, nulls, wrong types, and
   missing targets. Native-dependent tests should skip clearly when
   `unity_rs` fixtures are unavailable.
5. **Exporter golden tests:** root scoping, node ordering, transforms, mesh and
   skin invariants, texture/material conversion, GLB parsing, and stable hashes.
6. **Animation/Timeline tests:** curve variants, binding-path mapping, Timeline
   PPtrs, track-to-node/media mappings, unsupported diagnostics, and corrupted
   manifest rejection.
7. **Live2D track tests:** explicit Bundle-scope gating; the five verified
   `6.8.0.10` Bundle names; exact `costume2ds.character2dId -> character2ds`
   joins; business `characterId`/`motion`/`expression` records; protected
   `back`/`still` cases;
   `BuildMotionData` counts and integrity; constant and dynamic Facial Motion3
   outputs; separate shared storage; latest local audit-index integrity;
   incremental keys; standalone associated `model_list.json` generation as the
   ready marker; latest-only public output; and atomic publication without
   changing legacy `model_list.json`.
8. **Regression tests:** existing standard, Live2D, charts, media, security,
   and pipeline tests with all new flags disabled and enabled at each accepted
   phase.

Fixtures must be small, sanitized, versioned, and documented. Tests should
assert semantic equivalence where exporter encoding makes byte equality
unreliable; byte-level golden files are appropriate only for canonical outputs.

## 11. Rollback and migration strategy

Rollback is a configuration operation first: disable the relevant flag and
return to the existing Bundle-level pipeline. Do not change or invalidate the
existing Bundle cache layout while introducing package outputs.

- Use a versioned output namespace, for example `streaming_live-glb/v1`, and
  never overwrite a legacy asset path in place.
- Keep `plan_version` and `manifest_version`; reject incompatible versions
  rather than guessing migrations.
- Treat the manifest as the package commit marker. Remove or quarantine failed
  temporary package directories; leave valid Bundle caches intact.
- On a failed GLB rollout, stop package publication, disable the feature flag,
  and optionally delete only the new GLB output namespace. Existing
  standard/Live2D/charts outputs and caches remain available.
- Reprocessing with a new adapter/exporter writes a new package version and
  compares diagnostics and hashes before any alias/current pointer is changed.
- A migration utility, if later needed, must be explicit, dry-run capable, and
  never reinterpret an old manifest as a new one automatically.
- Disabling the associated pipeline leaves legacy model outputs, shared
  motion-set outputs, atomic JSON files, and legacy `model_list.json` available.
  The latest local audit data is not viewer input, and the associated pipeline
  has no candidates, history, current-pointer files, rollout state, rollback
  history, or remote revision history. It must not alter the GLB collection or
  Timeline package namespaces.

## 12. Non-goals

- Implementing a browser renderer, Timeline player, WebGL handlers, or runtime
  streaming/cache policy in this repository.
- Replacing the existing Bundle download/cache unit with logical packages.
- Inferring dependency closure from filenames, directory substrings, or guessed
  Unity file indices when metadata or portable identity is unavailable.
- Making every Unity shader, effect, Timeline track, video, audio format, or
  custom MonoBehaviour browser-compatible in the first implementation.
- Treating FBX, `.mat`, or typetree JSON as the final browser format.
- Turning Live2D into a GLB-style multi-Bundle collection or physical package,
  copying a shared motion set into each model output, or making Live2D a
  prerequisite of GLB Phase 3.
- Converting Facials to Cubism `.exp3.json` by default, treating the master DB
  `expression` field as an Expressions file, or adding inferred `Motions`/
  `Expressions` sections to `model3.json`.
- Inferring a specific model-variant-to-motion-Bundle association from
  `characterId` alone, especially for `*_back_motion_base` or
  `*_still_*_motion_base` names.
- Rewriting existing standard extraction or unrelated Charts behavior merely to
  support the GLB roadmap; Live2D changes are confined to its independent
  track.
- Promising visual parity for unsupported Unity rendering features before a
  documented converter and fixture exist.

## 13. Issue breakdown

The GitHub issue mapping below keeps the GLB parent and its six GLB phases
separate from the independent Live2D parent and its five phases. The Live2D
sequence does not add a dependency to GLB Phase 3 or change the GLB issue
hierarchy.

| Issue | Phase | Scope | Depends on |
| --- | --- | --- | --- |
| [#33](https://github.com/Sekai-World/sekai-assets-updater/issues/33) | GLB all | Dependency-aware Streaming Live GLB preprocessing (parent) | — |
| [#34](https://github.com/Sekai-World/sekai-assets-updater/issues/34) | GLB 0 | Define plan contracts, fixtures, diagnostics, and feature flags | — |
| [#35](https://github.com/Sekai-World/sekai-assets-updater/issues/35) | GLB 1 | Implement GLB extraction profiles and metadata dependency closure | #34 |
| [#36](https://github.com/Sekai-World/sekai-assets-updater/issues/36) | GLB 2 | Integrate closures with Bundle download and cache planning | #35 |
| [#37](https://github.com/Sekai-World/sekai-assets-updater/issues/37) | GLB 3 | Load multi-Bundle Unity collections and resolve cross-file PPtrs | #36 |
| [#38](https://github.com/Sekai-World/sekai-assets-updater/issues/38) | GLB 4 | Export deterministic root-scoped static GLB scenes | #37 |
| [#39](https://github.com/Sekai-World/sekai-assets-updater/issues/39) | GLB 5 | Convert GLB materials and export character animation | #38 |
| [#40](https://github.com/Sekai-World/sekai-assets-updater/issues/40) | GLB 6 | Emit and validate Timeline packages and atomic publication | #37, #39 |
| [#41](https://github.com/Sekai-World/sekai-assets-updater/issues/41) | Live2D all | Shared Live2D motion-set audit and latest viewer assets (parent) | — |
| [#45](https://github.com/Sekai-World/sekai-assets-updater/issues/45) | L2D-0 | Define association-index contracts and fixtures | — |
| [#42](https://github.com/Sekai-World/sekai-assets-updater/issues/42) | L2D-1 | Join model records with master-data evidence | #45 |
| [#46](https://github.com/Sekai-World/sekai-assets-updater/issues/46) | L2D-2 | Validate shared motion sets and retain Facial Motion3 | #45, #42 |
| [#43](https://github.com/Sekai-World/sekai-assets-updater/issues/43) | L2D-3 | Publish latest associated viewer assets and local audit data atomically | #42, #46 |
| [#44](https://github.com/Sekai-World/sekai-assets-updater/issues/44) | L2D-4 | Add incremental keys and publication controls | #43 |

Recommended GLB execution order:

```text
#34 → #35 → #36 → #37 → #38 → #39 → #40
```

Recommended independent Live2D execution order:

```text
#45 → #42 → #46 → #43 → #44
```

Completion of #40 provides a stable preprocessing/output contract. A future
browser-runtime project may consume that contract and implement playback; that
work is intentionally outside this repository and outside the acceptance scope
of this roadmap. Completion of L2D-4 provides a separate latest-only associated
viewer contract and local audit data; it does not change the GLB contract.
