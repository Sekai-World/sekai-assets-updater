# sekai-assets-updater

Update and extract Project Sekai asset bundles.

## Requirements

- Python `3.14.5t`
- `mise`
- `uv`
- `ffmpeg`
- `vgmstream-cli` for faster HCA decoding (optional)
- `rclone` if you use remote uploads

`ffmpeg` is required for:

- `wav -> mp3/flac`
- `usm -> mp4`

On macOS and supported Linux/Windows environments, video conversion will try to use hardware encoding automatically and fall back to software encoding when unavailable.

## Install

```bash
mise install
uv sync
```

`mise.toml` pins Python `3.14.5t` and Rust `stable`. `uv` also enforces
free-threaded Python `3.14` from `pyproject.toml` and `.python-version`.

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
- `HCA_DECODE_BACKEND`: `auto`, `vgmstream`, or `python`
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
- `ASSET_REMOTE_STORAGE`: upload extracted files after processing; set to `[]` to disable uploads

Cache files:

- `DL_LIST_CACHE_PATH`
- `ASSET_BUNDLE_INFO_CACHE_PATH`
- `GAME_VERSION_JSON_CACHE_PATH`

## Main Usage

Run the full updater:

```bash
uv run python main.py -c config.py
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
- `acb` bundles -> extracted audio, then `wav`, `mp3`, and for music bundles also `flac`
- `usm` bundles -> `.mp4`
- typetree-capable assets -> `.json`

Audio pipeline:

- `acb` is unpacked by the local parser in [`utils/acb.py`](./utils/acb.py)
- extracted `hca` files are decoded by `vgmstream-cli` when available, otherwise by the local Python decoder in [`utils/hca.py`](./utils/hca.py)
- audio file concurrency, HCA decode concurrency, and `ffmpeg` audio encode concurrency are configured separately
- the Python decoder runs in a process pool to use multiple CPU cores better

Video pipeline:

- `usm` is converted directly by `ffmpeg`
- hardware H.264 encoding is used when available

## Notes

- Some configs rely on cached metadata produced by earlier runs.
- If you only want local extraction, set `ASSET_REMOTE_STORAGE = []`.
- The updater runs bounded `download -> extract -> upload` pipeline stages. Temporary bundle files are removed after extraction succeeds when bundle caching is disabled, and temporary extracted directories are removed after upload succeeds when extracted caching is disabled.
