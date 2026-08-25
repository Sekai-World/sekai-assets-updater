# sekai-assets-updater

Update and extract Project Sekai asset bundles.

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
- `ASSET_REMOTE_STORAGE`: upload targets after processing; set to `[]` to disable uploads. Each target uses `type` to select its pipeline: `normal` for extracted assets, `live2d` for Live2D output, or `charts` for rendered charts.
- `ENABLE_LIVE2D_POSTPROCESS` and `ENABLE_CHARTS_POSTPROCESS` independently enable specialized post-processing in default `assets` mode.
- Multiple targets of each `ASSET_REMOTE_STORAGE` type upload sequentially after successful processing.
- Enabling Live2D automatically adds its required `live2d/` bundles to the download list; these automatic bundles are not removed by `DL_INCLUDE_LIST` or `DL_EXCLUDE_LIST` and are de-duplicated by `bundleName`.
- Live2D always uses `LIVE2D_BUNDLE_CACHE_DIR`, never the normal bundle cache. Its `live2d/` bundles bypass user filters and use metadata plus cache existence checks to download only missing or changed bundles. With no Live2D cache configured, that cache is temporary and removed after the pipeline, post-processing, and upload.
- Charts never download or cache asset bundles. They use existing `music/music_score/*.txt` files first; when absent, they copy `music/music_score/` from the first successful `type == "normal"` target in `ASSET_REMOTE_STORAGE`, using that target's program and args. If `ASSET_LOCAL_EXTRACTED_DIR` is persistent, the fallback uses a separate temporary workspace and cannot pollute it. If it is unset, the existing run-scoped extracted workspace is reused and cleaned after processing. Ordinary assets retain their existing temporary-file semantics.
- Chart incremental state is persisted at `chart_state.json` beside `DL_LIST_CACHE_PATH`. Only new or content-changed scores are re-rendered; runs with no changes skip rendering and upload entirely. State is updated atomically only after a successful render and upload.
- Live2D motion incremental state is persisted at `live2d_motion_state.json` beside `DL_LIST_CACHE_PATH`. On subsequent runs, only new or content-changed motion bundles are restored; unchanged bundles and their uploads are skipped. The state includes a model fingerprint derived from `*.moc3` files and the Unity version, so moc3 changes trigger a full rebuild. State is updated atomically only after all restore, upload, and publish operations succeed.

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

The single entry point supports `--mode assets|live2d|charts` (default `assets`).
The `live2d` mode constrains bundles to `live2d/`, ignores `DL_INCLUDE_LIST` and
`DL_EXCLUDE_LIST` for that namespace, and always runs Live2D processing. The
`charts` mode does not download game bundles: it runs the local-first/normal-storage
chart source fallback and then charts processing regardless of the enable flag.

```bash
uv run python main.py -c config.py --mode live2d
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

Tests are discovered from the project root using the `test_*.py` pattern. The
single-bundle debugging script, `test_download_extract.py`, is excluded from
the test run.

## Resume Behavior

If `DL_LIST_CACHE_PATH` exists, `main.py` will load it and resume from that cached download list instead of rebuilding the list.

With `--force-full-download`, `main.py` skips that resume behavior, ignores cached metadata json, rewrites `DL_LIST_CACHE_PATH` with a fresh full download list, and then processes it.

When all tasks succeed, the cached download list is removed automatically.

If some tasks fail, the remaining failed items are written back to `DL_LIST_CACHE_PATH`.

## Single Bundle Debugging

`test_download_extract.py` downloads and extracts bundles whose `bundleName` starts with a given prefix, using the cached:

- `ASSET_BUNDLE_INFO_CACHE_PATH`
- `GAME_VERSION_JSON_CACHE_PATH`

Example:

```bash
uv run python test_download_extract.py -c config.py sound/menu/menu_bgm/login_bonus
```

Verbose mode:

```bash
uv run python test_download_extract.py -c config.py -v title_screen/bgm_title
```

This is useful when:

- a specific bundle fails
- you want to reproduce extraction issues
- you want to test audio/video conversion on one bundle

## Current Extraction Behavior

Common outputs:

- `Texture2D` / `Sprite` -> `.png`, `.webp`
- `AudioClip` -> extracted raw sample files
- `acb` bundles -> decoded `wav`, then `mp3`, and for music bundles also `flac`
- `usm` bundles -> `.mp4`
- typetree-capable assets -> `.json`

Audio pipeline:

- `acb` is decoded directly to `wav` by [`cridecoder.decode_acb_to_wav`](https://github.com/Team-Haruki/cridecoder) (wrapped in [`utils/acb.py`](./utils/acb.py))
- standalone extracted `hca` files are decoded by `cridecoder` (wrapped in [`utils/hca.py`](./utils/hca.py)), with `vgmstream-cli` as the fallback in `auto` mode
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
