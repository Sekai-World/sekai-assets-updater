import os

from anyio import Path

from updater.model import SekaiServerRegion

# Proxy for fetching restricted content
PROXY_URL = None

# Server region
REGION = SekaiServerRegion.JP

# Single entry point processing mode. CLI --mode overrides this at runtime.
UPDATER_MODE = "assets"
# Optional additional export for bundles containing 3D scene meshes.
ENABLE_MODEL3D_FBX_EXPORT = False

# Fallback unity version, replace with the correct version if needed
UNITY_VERSION = "2022.3.21f1"
# User agent for requests, replace with the correct user agent if needed
USER_AGENT = None
# HTTP request timeout in seconds; set to 0 or None to disable
REQUEST_TIMEOUT = 180
# Maximum lifetime of one ffmpeg/vgmstream subprocess or upload command.
# On timeout, the process receives SIGTERM and has 2 seconds to exit before SIGKILL.
EXTERNAL_PROCESS_TIMEOUT = 300
# Number of download retry attempts on timeout or connection errors
DOWNLOAD_MAX_RETRIES = 3
# Retry delay uses capped exponential full jitter: random delay in [0, cap].
# Numeric Retry-After hints for HTTP 429/503 take precedence, are capped at the
# maximum, and are themselves used as the full-jitter upper bound.
DOWNLOAD_RETRY_BASE_DELAY = 1.0
DOWNLOAD_RETRY_MAX_DELAY = 30.0
# Minimum free bytes to keep on the download filesystem before starting a new download
MIN_FREE_DISK_BYTES = 1024 * 1024 * 1024
# How often blocked downloads recheck free disk space
DOWNLOAD_DISK_SPACE_CHECK_INTERVAL = 5

# Concurrency settings, default to the number of CPU cores
MAX_CONCURRENCY = os.cpu_count() or 1
# Pipeline stage concurrency. Defaults preserve the previous MAX_CONCURRENCY behavior
# for download/extract while upload uses one bundle-level worker.
MAX_CONCURRENCY_DOWNLOADS = MAX_CONCURRENCY
MAX_CONCURRENCY_EXTRACTS = MAX_CONCURRENCY
# Executor for bundle extraction: "process" (default) or "thread".
# unity-rs 0.5+, cridecoder 0.3.5+ and PIL release the GIL during their heavy
# work, so "thread" matches process throughput while sharing one interpreter
# (saving one Python process per extract worker). "process" additionally
# isolates a native decoder crash to a single worker.
EXTRACT_EXECUTOR = "process"
MAX_CONCURRENCY_UPLOAD_STAGE = 1
# Maximum queued artifacts between stages.
PIPELINE_STAGE_QUEUE_SIZE = MAX_CONCURRENCY
# Maximum number of audio files processed concurrently
MAX_CONCURRENT_AUDIO_FILES = MAX_CONCURRENCY
# Maximum number of concurrent HCA decode tasks
MAX_CONCURRENCY_HCA_DECODES = MAX_CONCURRENCY
# Maximum number of concurrent audio encoder tasks (mp3/flac)
MAX_CONCURRENCY_AUDIO_ENCODERS = MAX_CONCURRENCY
# Legacy combined audio concurrency fallback used when the new knobs are unset
MAX_CONCURRENCY_AUDIO_TRANSCODES = MAX_CONCURRENCY
# HCA decoder backend: "auto" prefers cridecoder, falls back to vgmstream-cli;
# "python" is kept as a legacy alias for cridecoder.
HCA_DECODE_BACKEND = "auto"
# Maximum number of concurrent video transcodes, defaults to half the CPU cores
MAX_CONCURRENCY_VIDEO_TRANSCODES = max(1, (os.cpu_count() or 1) // 2)
# Maximum number of concurrent cridecoder USM demux tasks
MAX_CONCURRENCY_USM_DEMUXES = MAX_CONCURRENCY_VIDEO_TRANSCODES
# USMs up to this size are demuxed fully in memory during extraction (skipping
# the merged .usm intermediate on disk); larger movies stream through disk.
USM_IN_MEMORY_MAX_BYTES = 64 * 1024 * 1024
# Maximum number of concurrent uploads
MAX_CONCURRENCY_UPLOADS = 10
# Texture export formats. Use ("png",), ("webp",), or ("png", "webp").
TEXTURE_OUTPUT_FORMATS = ("png", "webp")
# libwebp effort (0-6) for lossy WebP texture output. 2 encodes ~2x faster than
# the old default (4) at nearly identical size; 0 is ~3x faster but ~35% larger.
TEXTURE_WEBP_METHOD = 2
# PNG encoder profile used by the native unity-rs encoder: "fast" (default,
# ~9x faster than PIL at ~9% larger output), "default", "best", or an explicit
# zlib level 0-9 (unity-rs 0.5+).
TEXTURE_PNG_COMPRESSION = "fast"

# Crypto settings
# Replace these with the game's AES material. AES keys must be 16, 24, or 32
# bytes and the CBC IV must be exactly 16 bytes.
AES_KEY = b"0123456789012345"
AES_IV = b"0123456789012345"

# JSON URL for fetching game version information, can be set to a specific version json manually
GAME_VERSION_JSON_URL = None
# URL for fetching game cookies
GAME_COOKIE_URL = None
# URL for fetching in-game version information
GAME_VERSION_URL = None
# URL for fetching assetver (nuverse servers)
ASSET_VER_URL = None
# URL for fetching asset bundle info
ASSET_BUNDLE_INFO_URL = None
# URL for downloading asset bundle
ASSET_BUNDLE_URL = None

# Cache information for downloading
DL_LIST_CACHE_PATH = Path("cache", "jp", "json", "dl_list.json")
ASSET_BUNDLE_INFO_CACHE_PATH = Path("cache", "jp", "json", "asset_bundle_info.json")
GAME_VERSION_JSON_CACHE_PATH = Path("cache", "jp", "json", "version.json")

# Download filters, these are regex patterns matched against the bundle name
DL_INCLUDE_LIST = None  # Example: [r"^music/.*"]
DL_EXCLUDE_LIST = None  # Example: [r"^live_pv/.*"]
# Sorting download list by priority
DL_PRIORITY_LIST = None  # Example: [r"^music/.*", r"^character/member.*"]

# Local asset directories
ASSET_LOCAL_EXTRACTED_DIR = None  # Example: Path("cache", "jp", "extracted")
ASSET_LOCAL_BUNDLE_CACHE_DIR = None  # Example: Path("cache", "jp", "bundle")
# Live2D bundles use this separate cache root. If None, they use a run-scoped
# temporary cache that lasts through Live2D post-processing and upload only.
LIVE2D_BUNDLE_CACHE_DIR = None  # Example: Path("cache", "jp", "live2d-bundle")

# Asset remote storage settings. Each target's type controls which pipeline
# uploads to it: normal assets, legacy Live2D output, associated Live2D viewer
# assets, or charts.
#
# A "live2d-associated" target publishes only the latest public viewer assets
# below the temporary standalone live2d-associated/v1/ namespace: model_list.json,
# model/, motion/, and facial/ (selected paths may be nested). Its standalone,
# new-pipeline model_list.json is legacy-shaped, adds resolved motion-set file
# paths and clip filenames, and is uploaded after the assets as the ready marker.
# Detailed association index data, evidence, rule codes, diagnostics, checksums,
# source rows, and bundle metadata are only the latest local audit data; they are
# never uploaded and are not used by the viewer. The active pipeline has no
# candidates, history, current.json, candidate.json, rollout state, pointers,
# rollback history, or remote revision history, locally or remotely.
# This namespace is renamed to live2d when legacy retires; legacy output remains
# untouched while both namespaces coexist.
#
# Two backends are supported for "normal" targets:
# - subprocess (default): spawns `program` with `args`, replacing "src"/"dst".
#   The rclone `["copy", "src", "dst"]` template is automatically batched into
#   one process per artifact via --files-from-raw.
# - opendal: uploads in-process through Apache OpenDAL (no subprocess, no
#   external binary). `scheme` names the service ("s3", "fs", "azblob", ...),
#   `options` carries its string-valued configuration, and the optional
#   `prefix` is prepended to every object key. Example:
#   {
#       "type": "normal",
#       "backend": "opendal",
#       "scheme": "s3",
#       "prefix": "",
#       "options": {
#           "bucket": "example-assets",
#           "endpoint": "https://s3.example.com",
#           "region": "auto",
#           "access_key_id": os.environ.get("STORAGE_ACCESS_KEY_ID", ""),
#           "secret_access_key": os.environ.get("STORAGE_SECRET_ACCESS_KEY", ""),
#       },
#   },
ASSET_REMOTE_STORAGE = [
    {
        "type": "normal",
        "base": "remote:example-assets/",
        "program": "rclone",
        "args": ["copy", "src", "dst"],
    },
]

# Optional post-processing in the default assets mode. Explicit --mode
# live2d/live2d-associated/charts always runs its corresponding post-processor.
# Deprecated: the legacy flag and live2d/model_list.json output remain available
# for compatibility. The associated pipeline generates its own model_list.json
# under live2d-associated/v1/; the legacy live2d/ output remains untouched while
# both namespaces coexist.
ENABLE_LIVE2D_POSTPROCESS = False
ENABLE_LIVE2D_ASSOCIATED_PIPELINE = False
ENABLE_CHARTS_POSTPROCESS = False
# Optional path to a pre-built, validated Live2DIndex JSON document for the
# latest local audit data. The associated mode refuses to invent an index when
# this is unset.
LIVE2D_ASSOCIATION_INDEX_PATH = None
# Optional path to an explicit Live2D association-selection manifest. When set,
# it is used to build the latest local audit data from the run's Live2D bundle
# metadata.
LIVE2D_ASSOCIATION_SELECTIONS_PATH = None
# Master-data server used to load chart metadata. Defaults to REGION; set this
# when its repository name differs from the asset/cache region (for example,
# TC charts use "tc" while TC assets use the TW region).
CHART_DATA_SERVER = None
# Optional base URL for chart jacket images. When unset, charts use
# https://storage.sekai.best/sekai-{region}-assets/music/jacket.
CHART_JACKET_BASE_URL = None
