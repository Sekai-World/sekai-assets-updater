import os

from anyio import Path

from model import SekaiServerRegion

# Proxy for fetching restricted content
PROXY_URL = None

# Server region
REGION = SekaiServerRegion.JP

# Single entry point processing mode. CLI --mode overrides this at runtime.
UPDATER_MODE = "assets"

# Fallback unity version, replace with the correct version if needed
UNITY_VERSION = "2022.3.21f1"
# User agent for requests, replace with the correct user agent if needed
USER_AGENT = None
# HTTP request timeout in seconds; set to 0 or None to disable
REQUEST_TIMEOUT = 180
# Number of download retry attempts on timeout or connection errors
DOWNLOAD_MAX_RETRIES = 3
# Minimum free bytes to keep on the download filesystem before starting a new download
MIN_FREE_DISK_BYTES = 1024 * 1024 * 1024
# How often blocked downloads recheck free disk space
DOWNLOAD_DISK_SPACE_CHECK_INTERVAL = 5

# Concurrency settings, default to the number of CPU cores
MAX_CONCURRENCY = os.cpu_count()
# Pipeline stage concurrency. Defaults preserve the previous MAX_CONCURRENCY behavior
# for download/extract while upload uses one bundle-level worker.
MAX_CONCURRENCY_DOWNLOADS = MAX_CONCURRENCY
MAX_CONCURRENCY_EXTRACTS = MAX_CONCURRENCY
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
# Maximum number of concurrent uploads
MAX_CONCURRENCY_UPLOADS = 10
# Texture export formats. Use ("png",), ("webp",), or ("png", "webp").
TEXTURE_OUTPUT_FORMATS = ("png", "webp")

# Crypto settings
AES_KEY = bytes("AES_KEY")
AES_IV = bytes("AES_IV")

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
# uploads to it: normal assets, Live2D post-processing, or charts.
ASSET_REMOTE_STORAGE = [
    {
        "type": "normal",
        "base": "remote:example-assets/",
        "program": "rclone",
        "args": ["copy", "src", "dst"],
    },
]

# Optional post-processing in the default assets mode. Explicit --mode
# live2d/charts always runs its corresponding post-processor.
ENABLE_LIVE2D_POSTPROCESS = False
ENABLE_CHARTS_POSTPROCESS = False
