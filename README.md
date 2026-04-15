# sekai-assets-updater

Update and extract Project Sekai asset bundles.

## Requirements

- Python `3.12`
- `uv`
- `ffmpeg`
- `rclone` if you use remote uploads

`ffmpeg` is required for:

- `hca -> wav/mp3/flac`
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

Concurrency:

- `MAX_CONCURRENCY`: download/extract worker count
- `MAX_CONCURRENCY_AUDIO_TRANSCODES`: concurrent audio transcodes
- `MAX_CONCURRENCY_UPLOADS`: concurrent remote uploads

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

Only refresh filtered `asset_bundle_info.json` and stop before generating downloads:

```bash
uv run python main.py -c config.py --update-asset-bundle-info-only
```

## Resume Behavior

If `DL_LIST_CACHE_PATH` exists, `main.py` will load it and resume from that cached download list instead of rebuilding the list.

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
- extracted `hca` files are transcoded by `ffmpeg`

Video pipeline:

- `usm` is converted directly by `ffmpeg`
- hardware H.264 encoding is used when available

## Notes

- Some configs rely on cached metadata produced by earlier runs.
- `ffmpeg` must support `hca` decoding for audio conversion to work.
- If you only want local extraction, set `ASSET_REMOTE_STORAGE = []`.
