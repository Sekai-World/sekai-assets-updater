# Python 3.14t Experiment

Branch: `experiment/python-3.14t`

Current result: dependency installation and representative extraction paths work
after replacing `unitypy` and `orjson`.

Changes tested:

- Set `.python-version` to `3.14t`.
- Set `requires-python` to `==3.14.*`.
- Replaced `unitypy` with an AssetStudioFFI worker adapter.
- Replaced `orjson` with `json_compat`, because `orjson==3.11.6` reports:
  `orjson v3.11.6 does not support free-threaded Python`.
- Added `mise.toml` with Python `3.14.5t` and Rust `stable`.

Runtime requirement:

- `assetstudio_ffi_worker` must be available in `PATH` or configured with
  `ASSET_STUDIO_FFI_WORKER_PATH` / `HARUKI_ASSET_STUDIO_FFI_WORKER_PATH`.
- `HarukiAssetStudioFFI` native library must be configured with
  `ASSET_STUDIO_FFI_LIBRARY_PATH` / `HARUKI_ASSET_STUDIO_FFI_LIBRARY_PATH`.
- Local defaults are auto-discovered from `.tools/assetstudio-ffi/bin` and
  `.tools/assetstudio-ffi/assetstudio-ffi-*`.

Concurrency notes:

- `ASSET_EXTRACT_EXECUTOR = "auto"` uses `ThreadPoolExecutor` on free-threaded
  Python and `ProcessPoolExecutor` otherwise. Set it to `"process"` to force the
  legacy isolation path.
- `MAX_CONCURRENCY_EXTRACTS` controls asset extraction workers for both
  executor modes.
- Use CPU count as the extract/audio baseline, and about half the CPU count for
  `MAX_CONCURRENCY_VIDEO_TRANSCODES`.

Re-test command:

```sh
mise install
mise exec -- uv sync --python 3.14t
```

Validation commands:

```sh
uv run python -m py_compile assetstudio_ffi.py bundle.py utils/playable.py
uv run python test_download_extract.py -c config.local-314t-test.py -v music/music_score/tutorial
uv run python test_download_extract.py -c config.local-314t-test.py -v sound/menu/menu_bgm/login_bonus
uv run python test_download_extract.py -c config.local-314t-test.py -v scenario/background/bg_white
uv run python test_download_extract.py -c config.local-jp-314t-test.py -v scenario/movie/01_leoneed_ed1
uv run python test_download_extract.py -c config.local-jp-314t-test.py -v virtual_live/mc/timeline/mc_ev_shuffle_49_1
```

Validated paths:

- Music score TextAsset extraction.
- Audio ACB/HCA decode and WAV/MP3 transcode.
- Texture PNG/WebP export.
- Movie USM export and MP4 transcode.
- Virtual live `.playable` extraction with `__timelineParse`.

Notes:

- `MonoScript` typetrees are required to resolve playable timeline class names.
- Playable output is written only for the TimelineAsset root object to avoid
  sub-asset JSON overwrites.
