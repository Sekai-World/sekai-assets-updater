# sekai-assets-updater

Update and extract Project Sekai asset bundles.

## Optional 3D FBX export

Set `ENABLE_MODEL3D_FBX_EXPORT = True` to enable optional additional FBX output
(disabled by default). Normal download filtering is unchanged. Bundles that
have entered ordinary extraction are judged by content; bundles with a scene
mesh produce `fbx/model.fbx` and textures in the same per-bundle directory.
This is per-bundle model export only: it does not assemble complete characters
and does not produce GLB.

## Requirements

- Python `3.12`
- `uv`
- `ffmpeg`
- `vgmstream-cli` as an optional HCA decoding fallback
- `rclone` if you use remote uploads

`ffmpeg` is required for:

- `wav -> mp3/flac`
- `usm -> mp4`

On macOS and supported Linux/Windows environments, video conversion will try to use hardware encoding automatically and fall back to software encoding when unavailable.

## Install

```bash
uv sync
```

## Project Layout

A run flows fetch → plan → pipeline (download / extract / upload) → post-process.
The package layout follows that story:

- `main.py` — two-line entry shim (`uv run python main.py -c config.py`)
- `config.example.py` — configuration template (copy to `config.py`)
- `updater/` — application package
  - `cli/` — the run itself: `entry.py` (argparse + dynamic config loading),
    `configuration.py` (the loaded-config cell and validation), `pending.py`
    (download-queue caches), `lifecycle.py` (download/completion flows),
    `runner.py` (journaled orchestration), `logging_setup.py`
  - `net/` — fetch and plan: `metadata.py` (asset-bundle-info/game-version),
    `plan.py` (download-list building), `download.py` (streaming download +
    deobfuscation), `integrity.py`, `disk_space.py`, `http.py`, `cookies.py`,
    `urls.py`
  - `pipeline.py` — the async download → extract → upload stage pipeline
  - `extract/` — Unity bundle extraction: `bundle.py` (per-bundle
    orchestration), `sync_worker.py` (the ProcessPool CPU worker),
    `unity_objects.py` (object export walk), `paths.py`, `acb_cache.py`,
    `playable.py`
  - `media/` — codec layer, each module a Rust-offload seam: `audio.py`
    (HCA/ACB → wav → mp3/flac), `video.py` (USM demux + ffmpeg mp4),
    `images.py` (texture export), `acb.py`, `hca.py`, `usm.py`, `binary.py`,
    `process.py`
  - `storage/` — upload backends: `rclone.py` (subprocess + batched),
    `opendal.py` (in-process), `remote.py` (key derivation/validation)
  - `live2d/` — Cubism motion restoration and association contracts: `curves.py`,
    `motion3.py`, `moc3.py`, `restore.py`, `contracts.py`, `association.py`,
    `keys.py`, `publication.py`, `rollout.py`
  - `postprocess/` — Live2D and Charts post-processing: `dispatch.py`,
    `charts.py`, `live2d_models.py`, `incremental_state.py`, `config.py`
  - `modes.py`, `workspace.py`, `runtime.py`, `sanitize.py`, `state.py`,
    `security.py`, `crypto.py`, `constants.py`, `model.py`,
    `external_process.py`, `unity_rs_adapter.py` — cross-cutting support
- `tests/` — automated regression suite
- `docs/` — design documents

## Config

Copy `config.example.py` to your own config file and fill in the values:

```bash
cp config.example.py config.py
```

Important fields:

- `REGION`: `JP`, `EN`, `TW`, `KR`, `CN`
- `AES_KEY`, `AES_IV`
- `GAME_VERSION_JSON_URL`
- `GAME_COOKIE_URL` if the server requires cookies
- `GAME_VERSION_URL`
- `ASSET_VER_URL` for Nuverse regions
- `ASSET_BUNDLE_INFO_URL`
- `ASSET_BUNDLE_URL`
- `MIN_FREE_DISK_BYTES`: minimum free disk space to keep before a new download starts
- `DOWNLOAD_DISK_SPACE_CHECK_INTERVAL`: how often blocked downloads recheck free space
- `EXTERNAL_PROCESS_TIMEOUT`: maximum seconds for each selected external codec or upload
  command. A timed-out process is terminated and given a 2-second grace period before it
  is killed; configuration must be a positive number.

Disk space gate:

- Download admission is checked against the filesystem backing `ASSET_LOCAL_BUNDLE_CACHE_DIR`
- If `ASSET_LOCAL_BUNDLE_CACHE_DIR` is `None`, the gate uses the system temp directory filesystem
- Each in-flight download reserves the bundle `fileSize` from metadata before the request starts
- A new download waits until `free_bytes - reserved_bytes >= MIN_FREE_DISK_BYTES + bundle.fileSize`
- Set `MIN_FREE_DISK_BYTES = 0` to disable the gate
- The gate only controls when a download may start; bounded stage queues prevent downloads from outrunning extract/upload

Concurrency:

- `MAX_CONCURRENCY`: legacy default for download/extract stage concurrency
- `MAX_CONCURRENCY_DOWNLOADS`: concurrent bundle downloads
- `MAX_CONCURRENCY_EXTRACTS`: concurrent bundle extractions
- `MAX_CONCURRENCY_UPLOAD_STAGE`: concurrent bundle upload stages
- `PIPELINE_STAGE_QUEUE_SIZE`: maximum queued artifacts between pipeline stages
- `MAX_CONCURRENT_AUDIO_FILES`: concurrent audio file pipelines
- `MAX_CONCURRENCY_HCA_DECODES`: concurrent HCA decodes
- `MAX_CONCURRENCY_AUDIO_ENCODERS`: concurrent `ffmpeg` audio encodes
- `MAX_CONCURRENCY_AUDIO_TRANSCODES`: legacy fallback for the three audio settings above
- `HCA_DECODE_BACKEND`: `auto`, `vgmstream`, or `python` (`python` is a legacy alias for `cridecoder`)
- `MAX_CONCURRENCY_USM_DEMUXES`: concurrent `cridecoder` USM demux tasks
- `MAX_CONCURRENCY_VIDEO_TRANSCODES`: concurrent video transcodes
- `MAX_CONCURRENCY_UPLOADS`: concurrent remote uploads
- `TEXTURE_OUTPUT_FORMATS`: texture formats to export, for example `("webp",)` or `("png", "webp")`

Filters:

- `DL_INCLUDE_LIST`
- `DL_EXCLUDE_LIST`
- `DL_PRIORITY_LIST`

Storage:

- `ASSET_LOCAL_EXTRACTED_DIR`: keep extracted files locally; if `None`, use a temp dir
- `ASSET_LOCAL_BUNDLE_CACHE_DIR`: keep downloaded bundles locally; if `None`, use a temp file
- `LIVE2D_BUNDLE_CACHE_DIR`: separate persistent cache for `live2d/` bundles; if `None`, use a run-scoped temporary cache only while Live2D post-processing runs
- `ASSET_REMOTE_STORAGE`: upload targets after processing; set to `[]` to disable uploads. Each target uses `type` to select its pipeline: `normal` for extracted assets, `live2d` for deprecated legacy Live2D output, `live2d-associated` for the temporary standalone associated viewer namespace, or `charts` for rendered charts.
- `ENABLE_LIVE2D_POSTPROCESS` and `ENABLE_CHARTS_POSTPROCESS` independently enable specialized post-processing in default `assets` mode.
- `ENABLE_LIVE2D_POSTPROCESS` is deprecated but retained: it continues to own `live2d/model_list.json` and the legacy `live2d/` output. `ENABLE_LIVE2D_ASSOCIATED_PIPELINE` is independent and may be enabled at the same time.
- `LIVE2D_ASSOCIATION_INDEX_PATH` optionally supplies a pre-built, validated `Live2DIndex` JSON document for the latest local association audit data.
- `LIVE2D_ASSOCIATION_SELECTIONS_PATH` optionally supplies an explicit association-selection manifest. The manifest identifies master-data input, stable model/motion IDs, exact run bundle keys, and output paths.
- When neither an explicit index nor an explicit selection manifest is supplied, the associated pipeline automatically discovers the exact `live2d/model/` and `live2d/motion/` Bundles from current metadata and builds the latest local audit data. Automatic discovery requires either `LIVE2D_ASSOCIATION_MASTER_DATA_DIR` containing the six Live2D master-data JSON tables or `LIVE2D_ASSOCIATION_MASTER_DATA_URL`; the local directory takes precedence. A GitHub repository URL is converted to one latest-branch archive download (no GitHub API or six raw-table requests), using `LIVE2D_ASSOCIATION_MASTER_DATA_BRANCH` (`main` by default), and the downloaded master data is temporary and never uploaded. `LIVE2D_ASSOCIATION_MASTER_DB_VERSION` remains the local version label and online mode defaults to `latest:<branch>` when it is left at `local`.
- Online latest mode follows upstream branch changes and is therefore not reproducible by revision. Use a local directory, explicit validated index, or selection manifest when repeatability is required.
- Association input precedence is: directly supplied `association_index`, supplied `association_index_path` (or configured `LIVE2D_ASSOCIATION_INDEX_PATH`), configured `LIVE2D_ASSOCIATION_SELECTIONS_PATH`, then automatic discovery.
- Multiple targets of each `ASSET_REMOTE_STORAGE` type upload sequentially after successful processing.
- Enabling Live2D automatically adds its required `live2d/` bundles to the download list; these automatic bundles are not removed by `DL_INCLUDE_LIST` or `DL_EXCLUDE_LIST` and are de-duplicated by `bundleName`.
- Live2D always uses `LIVE2D_BUNDLE_CACHE_DIR`, never the normal bundle cache. Its `live2d/` bundles bypass user filters and use metadata plus cache existence checks to download only missing or changed bundles. With no Live2D cache configured, that cache is temporary and removed after the pipeline, post-processing, and upload.
- Charts never download or cache asset bundles. They use existing `music/music_score/*.txt` files first; when absent, they copy `music/music_score/` from the first successful `type == "normal"` target in `ASSET_REMOTE_STORAGE`, using that target's program and args. If `ASSET_LOCAL_EXTRACTED_DIR` is persistent, the fallback uses a separate temporary workspace and cannot pollute it. If it is unset, the existing run-scoped extracted workspace is reused and cleaned after processing. Ordinary assets retain their existing temporary-file semantics.
- Chart incremental state is persisted at `chart_state.json` beside `DL_LIST_CACHE_PATH`. Only new or content-changed scores are re-rendered; runs with no changes skip rendering and upload entirely. State is updated atomically only after a successful render and upload.
- Live2D motion incremental state is persisted at `live2d_motion_state.json` beside `DL_LIST_CACHE_PATH`. On subsequent runs, only new or content-changed motion bundles are restored; unchanged bundles and their uploads are skipped. The state includes a model fingerprint derived from `*.moc3` files and the Unity version, so moc3 changes trigger a full rebuild. State is updated atomically only after all restore, upload, and publish operations succeed.
- The associated pipeline publishes only the latest public viewer assets under the temporary standalone `live2d-associated/v1/` namespace: `model_list.json`, `model/`, `motion/`, and `facial/` (selected paths may be nested).
- Its standalone, new-pipeline `model_list.json` is legacy-shaped, adds resolved motion-set file paths and clip filenames, and is uploaded after those assets as the public ready marker.
- Detailed association index data, evidence, rule codes, diagnostics, checksums, source rows, and bundle metadata are retained only as the latest local audit data; they are never uploaded and the viewer does not use them. The active associated pipeline has no `candidates`, history, `current.json`, `candidate.json`, rollout state, pointers, rollback history, or remote revision history, locally or remotely.
- The namespace is temporary; when legacy retires, it is renamed to `live2d`. During coexistence, legacy `live2d/` output remains untouched.

Startup validates all configured concurrency values, AES key/IV lengths, the external process
timeout, and executables required by the selected decoder and upload backends.

Cache files:

- `DL_LIST_CACHE_PATH`
- `ASSET_BUNDLE_INFO_CACHE_PATH`
- `GAME_VERSION_JSON_CACHE_PATH`
- `live2d_motion_state.json` (Live2D incremental state, sibling of `DL_LIST_CACHE_PATH`)

## Main Usage

Run the full updater:

```bash
uv run python main.py -c config.py
```

The single entry point supports `--mode assets|live2d|live2d-associated|charts` (default `assets`).
The `live2d` mode constrains bundles to `live2d/`, ignores `DL_INCLUDE_LIST` and
`DL_EXCLUDE_LIST` for that namespace, and always runs Live2D processing. The
`charts` mode does not download game bundles: it runs the local-first/normal-storage
chart source fallback and then charts processing regardless of the enable flag.
The `live2d-associated` mode uses the same explicit `live2d/` bundle scope and
cache policy but publishes only the latest public viewer assets in the temporary
standalone `live2d-associated/v1` namespace. It accepts an explicit validated
index or selection manifest, and otherwise automatically builds the latest
local audit data from current Live2D Bundle metadata and master data. A
directly supplied `association_index` takes precedence over
`association_index_path`, which takes precedence over the selections manifest;
automatic discovery is the final fallback. Automatic mode requires either
`LIVE2D_ASSOCIATION_MASTER_DATA_DIR` containing the six Live2D master-data JSON
tables or `LIVE2D_ASSOCIATION_MASTER_DATA_URL` pointing to a GitHub repository
or direct archive. The local directory takes precedence. Online mode downloads
one latest `LIVE2D_ASSOCIATION_MASTER_DATA_BRANCH` archive per automatic run
(default `main`), without GitHub API/raw-per-table requests, and does not upload
the master data. Latest branch mode follows upstream changes; use local data or
an explicit index/selection manifest when repeatability is required.

```bash
uv run python main.py -c config.py --mode live2d
uv run python main.py -c config.py --mode live2d-associated
uv run python main.py -c config.py --mode charts
```

Verbose mode:

```bash
uv run python main.py -c config.py -v
```

Quiet mode:

```bash
uv run python main.py -c config.py -q
```

Only refresh filtered `asset_bundle_info.json` and stop before generating downloads:

```bash
uv run python main.py -c config.py --update-asset-bundle-info-only
```

Force a full rebuild of `dl_list.json` and redownload everything matched by the filters, ignoring cached json metadata and any existing cached `dl_list.json`:

```bash
uv run python main.py -c config.py --force-full-download
```

If downloads are blocked by low free disk space, the updater logs a warning and retries after `DOWNLOAD_DISK_SPACE_CHECK_INTERVAL` seconds.

## Tests

Run the test suite with uv, including the development dependencies:

```bash
uv run --group dev pytest
```

Tests live in the `tests/` directory and are discovered with the `test_*.py`
and `*_test.py` patterns.

## Resume Behavior

If `DL_LIST_CACHE_PATH` exists, `main.py` will load it and resume from that cached download list instead of rebuilding the list.

With `--force-full-download`, `main.py` skips that resume behavior, ignores cached metadata json, rewrites `DL_LIST_CACHE_PATH` with a fresh full download list, and then processes it.

When all tasks succeed, the cached download list is removed automatically.

If some tasks fail, the remaining failed items are written back to `DL_LIST_CACHE_PATH`.

## Current Extraction Behavior

Common outputs:

- `Texture2D` / `Sprite` -> `.png`, `.webp`
- `AudioClip` -> extracted raw sample files
- `acb` bundles -> decoded `wav`, then `mp3`, and for music bundles also `flac`
- `usm` bundles -> `.mp4`
- typetree-capable assets -> `.json`

Audio pipeline:

- `acb` is decoded directly to `wav` by [`cridecoder.decode_acb_to_wav`](https://github.com/Team-Haruki/cridecoder) (wrapped in [`updater/media/acb.py`](./updater/media/acb.py))
- standalone extracted `hca` files are decoded by `cridecoder` (wrapped in [`updater/media/hca.py`](./updater/media/hca.py)), with `vgmstream-cli` as the fallback in `auto` mode
- audio file concurrency, HCA decode concurrency, and `ffmpeg` audio encode concurrency are configured separately
- the `cridecoder` HCA decoder runs in a process pool to use multiple CPU cores better

Video pipeline:

- `usm` is demuxed to a raw `.m2v` video stream by `cridecoder`, which `ffmpeg` then transcodes to `.mp4`
- USM demux tasks run in a bounded process pool controlled by `MAX_CONCURRENCY_USM_DEMUXES`
- hardware H.264 encoding is used when available

## Notes

- Some configs rely on cached metadata produced by earlier runs.
- If you only want local extraction, set `ASSET_REMOTE_STORAGE = []`.
- The updater runs bounded `download -> extract -> upload` pipeline stages. Temporary bundle files are removed after extraction succeeds when bundle caching is disabled, and temporary extracted directories are removed after upload succeeds when extracted caching is disabled.
