"""Post-processing for the optional live2d and charts pipelines."""

import asyncio
import hashlib
import logging
import re
import shutil
import tempfile
from pathlib import Path as StdPath

import orjson as json
from anyio import Path

from helpers import _get_external_process_timeout, get_request_timeout, upload_directory
from state import atomic_write_json, prepare_state_directory
from utils.chart import get_json_url, get_list, render_chart
from utils.live2d import collect_param_id_map, restore_live2d_motions

logger = logging.getLogger("asset_updater")

SPECIALIZED_MODES = ("live2d", "charts")
SPECIALIZED_BUNDLE_PREFIXES = {
    "live2d": ("live2d/",),
    "charts": (),
}
CHART_SOURCE_CONCURRENCY = 4
CHART_SOURCE_TERMINATE_TIMEOUT = 5
DEFAULT_CHART_JACKET_BASE_URL = "https://storage.sekai.best/sekai-{region}-assets/music/jacket"


def mode_uses_bundle_pipeline(mode: str) -> bool:
    """Whether a mode fetches and processes game asset bundles."""
    if mode not in ("assets", "live2d", "charts"):
        raise ValueError(f"Unsupported updater mode: {mode}")
    return mode != "charts"


def get_enabled_specialized_modes(mode: str, config) -> tuple[str, ...]:
    """Select specialized processors from mode and independent config flags."""
    if mode in SPECIALIZED_MODES:
        return (mode,)
    if mode != "assets":
        raise ValueError(f"Unsupported updater mode: {mode}")
    return tuple(
        specialized_mode
        for specialized_mode in SPECIALIZED_MODES
        if getattr(config, f"ENABLE_{specialized_mode.upper()}_POSTPROCESS", False)
    )


def get_required_bundle_prefixes(mode: str, config) -> tuple[str, ...]:
    """Return prefixes that enabled post-processors must always download."""
    return tuple(
        prefix
        for specialized_mode in get_enabled_specialized_modes(mode, config)
        for prefix in SPECIALIZED_BUNDLE_PREFIXES[specialized_mode]
    )


def needs_shared_workspace(mode: str, config) -> bool:
    """Whether specialized processing needs a run-scoped extracted workspace."""
    return (
        bool(get_enabled_specialized_modes(mode, config))
        and getattr(config, "ASSET_LOCAL_EXTRACTED_DIR", None) is None
    )


def needs_live2d_bundle_cache(mode: str, config) -> bool:
    """Whether Live2D needs a run-scoped bundle cache root."""
    return (
        "live2d" in get_enabled_specialized_modes(mode, config)
        and getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None) is None
    )


def retains_live2d_extracted_outputs(config) -> bool:
    """Whether Live2D extraction must remain available for post-processing."""
    return "live2d" in get_enabled_specialized_modes(
        getattr(config, "UPDATER_MODE", "assets"), config
    )


def get_specialized_storage(config, mode: str) -> list[dict]:
    """Return asset storage targets configured for a specialized mode."""
    if mode not in SPECIALIZED_MODES:
        raise ValueError(f"Unsupported specialized mode: {mode}")
    return [
        storage
        for storage in (getattr(config, "ASSET_REMOTE_STORAGE", None) or [])
        if storage.get("type") == mode
    ]


def get_normal_storage_candidates(config) -> list[dict]:
    """Return extracted-asset mirrors usable as chart source fallbacks."""
    return [
        storage
        for storage in (getattr(config, "ASSET_REMOTE_STORAGE", None) or [])
        if storage.get("type") == "normal"
    ]


def _region_name(config) -> str:
    region = getattr(config, "REGION", None)
    return getattr(region, "name", str(getattr(region, "value", region))).lower()


def get_chart_data_server(config) -> str:
    """Return the master-data server used while rendering charts.

    This is normally the asset region, but can differ when a region's master
    data is published under a distinct server name (for example, TC vs. TW).
    """
    return getattr(config, "CHART_DATA_SERVER", None) or _region_name(config)


def _model_list(model_dir: StdPath) -> list[dict[str, str]]:
    models = []
    for model_file in sorted(model_dir.rglob("*.model3.json")):
        models.append(
            {
                "modelName": model_file.name.removesuffix(".model3.json"),
                "modelBase": model_file.parent.name,
                "modelPath": str(model_file.parent.relative_to(model_dir)),
                "modelFile": model_file.name,
            }
        )
    return models


def _models_from_remote_entries(entries) -> list[dict[str, str]]:
    """Convert an rclone lsjson corpus (relative to ``live2d/model``)."""
    models = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("IsDir"):
            continue
        relative_path = str(entry.get("Path", "")).replace("\\", "/")
        path = StdPath(relative_path)
        if not relative_path or relative_path.startswith("/") or path.drive or ".." in path.parts:
            raise ValueError(f"unsafe remote Live2D model path: {relative_path!r}")
        if not relative_path.endswith(".model3.json"):
            continue
        models.append(
            {
                "modelName": path.name.removesuffix(".model3.json"),
                "modelBase": path.parent.name,
                "modelPath": path.parent.as_posix() if path.parent != StdPath(".") else "",
                "modelFile": path.name,
            }
        )
    return sorted(models, key=lambda model: (model["modelPath"], model["modelFile"]))


def _validate_live2d_storage(storage: dict) -> None:
    """Allow only non-destructive rclone operations for Live2D publishing."""
    args = storage.get("args")
    operation = args[0] if args else None
    if operation not in {"copy", "copyto"}:
        raise ValueError(
            f"Live2D storage requires a copy or copyto operation; got {operation or '<missing>'!r}"
        )


def _listing_args(storage: dict, remote_model_root: str) -> list[str]:
    """Build lsjson arguments while retaining configured opaque rclone flags."""
    _validate_live2d_storage(storage)
    configured = list(storage.get("args", []))
    if len(configured) >= 3 and configured[1:3] == ["src", "dst"]:
        configured = configured[3:]
    return ["lsjson", remote_model_root, "--recursive", *configured]


async def _remote_model_list(storage: dict, config) -> list[dict[str, str]]:
    """Read and validate the authoritative remote Live2D model corpus."""
    remote_root = f"{str(storage['base']).rstrip('/')}/live2d/model"
    process = await asyncio.create_subprocess_exec(
        storage["program"],
        *_listing_args(storage, remote_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timeout = _get_external_process_timeout(config)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError("remote Live2D model listing timed out") from exc
    if process.returncode != 0:
        raise RuntimeError(
            f"remote Live2D model listing failed with status {process.returncode}: "
            f"{stderr.decode(errors='replace').strip()}"
        )
    try:
        entries = json.loads(stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("remote Live2D model listing was not valid JSON") from exc
    models = _models_from_remote_entries(entries)
    if not models:
        raise RuntimeError("remote Live2D model listing was empty or contained no model files")
    return models


async def _publish_live2d_model_list(models: list[dict[str, str]], storage: dict, config) -> None:
    """Publish only the generated index after the remote corpus is validated."""
    with tempfile.TemporaryDirectory(prefix="sekai-live2d-index-") as temp_dir:
        index_dir = StdPath(temp_dir) / "live2d"
        index_dir.mkdir()
        (index_dir / "model_list.json").write_bytes(json.dumps(models, option=json.OPT_INDENT_2))
        await upload_directory(
            Path(str(index_dir)),
            Path(f"{str(storage['base']).rstrip('/')}/live2d"),
            storage["program"],
            storage["args"],
            config=config,
        )


async def _upload_live2d_assets(source_dir: StdPath, storage: dict, config) -> None:
    """Upload current assets without allowing a stale local index to leak through."""
    with tempfile.TemporaryDirectory(prefix="sekai-live2d-assets-") as temp_dir:
        staged = StdPath(temp_dir) / "live2d"
        staged.mkdir()
        for child in source_dir.iterdir():
            if child.name == "model_list.json":
                continue
            destination = staged / child.name
            if child.is_dir():
                shutil.copytree(child, destination)
            else:
                shutil.copy2(child, destination)
        await upload_directory(
            Path(str(staged)),
            Path(f"{str(storage['base']).rstrip('/')}/live2d"),
            storage["program"],
            storage["args"],
            config=config,
        )


def _score_matches_include_list(score_path: StdPath, include_list: list[str] | None) -> bool:
    """Match a score's reconstructed bundle directory using download semantics."""
    if not include_list:
        return True
    score_directory = f"music/music_score/{score_path.parent.name}"
    return any(re.match(pattern, score_directory) for pattern in include_list)


def collect_score_files(
    extracted_dir: StdPath, include_list: list[str] | None = None
) -> list[StdPath]:
    """Collect eligible chart TextAsset outputs with deterministic de-duplication."""
    score_root = extracted_dir / "music" / "music_score"
    return sorted(
        {
            path
            for path in score_root.rglob("*.txt")
            if path.is_file() and _score_matches_include_list(path, include_list)
        }
    )


def has_local_chart_sources(extracted_dir: StdPath, include_list: list[str] | None = None) -> bool:
    """Whether an extraction root already contains usable chart source files."""
    return bool(collect_score_files(extracted_dir, include_list))


def needs_temporary_chart_source(
    extracted_dir: StdPath,
    configured_extracted_dir,
    include_list: list[str] | None = None,
) -> bool:
    """Avoid writing remote chart sources into a user's persistent extraction root."""
    return configured_extracted_dir is not None and not has_local_chart_sources(
        extracted_dir, include_list
    )


def _storage_source_path(storage: dict) -> StdPath:
    return StdPath(storage["base"]) / "music" / "music_score"


async def _stop_chart_source_process(process) -> None:
    """Terminate a timed-out chart source process, escalating to kill if needed."""
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=CHART_SOURCE_TERMINATE_TIMEOUT)
    except (asyncio.TimeoutError, ProcessLookupError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _copy_chart_sources_from_one_storage(
    storage: dict,
    target_dir: StdPath,
    config,
) -> None:
    """Run one normal-storage chart source copy and enforce success."""
    source_dir = _storage_source_path(storage)
    args = storage["args"][:]
    args[args.index("src")] = str(source_dir)
    args[args.index("dst")] = str(target_dir)
    process = await asyncio.create_subprocess_exec(storage["program"], *args)
    timeout = get_request_timeout(config).total
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        logger.warning(
            "Timed out loading chart sources from normal storage %s",
            storage.get("base", "<unknown>"),
        )
        await _stop_chart_source_process(process)
        raise RuntimeError("command timed out") from exc
    if process.returncode != 0:
        raise RuntimeError(f"command exited with status {process.returncode}")


async def fetch_chart_sources_from_storage(
    config, extracted_dir: StdPath, include_list: list[str] | None = None
) -> None:
    """Copy chart sources from the first successful normal asset mirror."""
    target_dir = extracted_dir / "music" / "music_score"
    target_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    for storage in get_normal_storage_candidates(config):
        try:
            await _copy_chart_sources_from_one_storage(storage, target_dir, config)
            if not has_local_chart_sources(extracted_dir, include_list):
                raise RuntimeError("storage did not provide any chart .txt files")
            logger.info("Loaded chart sources from normal storage %s", storage["base"])
            return
        except Exception as exc:
            errors.append(f"{storage.get('base', '<unknown>')}: {exc}")

    if errors:
        detail = "; ".join(errors)
        raise RuntimeError(f"Failed to load chart sources from normal storage: {detail}")
    raise RuntimeError("No normal ASSET_REMOTE_STORAGE target is configured for chart sources")


def music_id_from_score_path(score_path: StdPath) -> int:
    """Parse the numeric music id from a score directory such as ``001_foo``."""
    return int(score_path.parent.name.split("_", 1)[0])


def get_chart_jacket_url(config, region: str, music_id: int) -> str:
    """Build a chart jacket URL from the configured or legacy base URL."""
    jacket_base_url = _resolve_chart_jacket_base_url(config, region)
    padded_id = str(music_id).zfill(3)
    jacket_name = f"jacket_s_{padded_id}.png"
    return f"{jacket_base_url.rstrip('/')}/jacket_s_{padded_id}/{jacket_name}"


def _resolve_chart_jacket_base_url(config, region: str) -> str:
    """Return the effective jacket base URL for *region*."""
    jacket_base_url = getattr(config, "CHART_JACKET_BASE_URL", None)
    if not jacket_base_url:
        jacket_base_url = DEFAULT_CHART_JACKET_BASE_URL.format(region=region)
    return jacket_base_url


# ---------------------------------------------------------------------------
# Incremental chart state
# ---------------------------------------------------------------------------

_CHART_STATE_SCHEMA_VERSION = 1


def chart_state_path(config) -> StdPath:
    """Return the filesystem path for persisted chart incremental state."""
    return StdPath(config.DL_LIST_CACHE_PATH).parent / "chart_state.json"


def chart_fingerprint(config) -> dict[str, str]:
    """Build a fingerprint dict from the current chart configuration."""
    region = _region_name(config)
    return {
        "region": region,
        "data_server": get_chart_data_server(config),
        "jacket_base_url": _resolve_chart_jacket_base_url(config, region),
    }


def hash_score_file(path: StdPath) -> str:
    """Return the SHA-256 hex digest of the file at *path*."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_score_hashes(
    extracted_dir: StdPath, include_list: list[str] | None = None
) -> dict[str, str]:
    """Return ``{relative_posix_path: sha256_hex}`` for every score file."""
    score_root = extracted_dir / "music" / "music_score"
    result: dict[str, str] = {}
    for path in collect_score_files(extracted_dir, include_list):
        rel = path.relative_to(score_root).as_posix()
        result[rel] = hash_score_file(path)
    return result


def validate_chart_state(value: object) -> dict:
    """Strict validator for ``chart_state.json`` compatible with ``atomic_write_json``."""
    if not isinstance(value, dict):
        raise ValueError("chart state must be an object")
    allowed_top = {"schema_version", "fingerprint", "scores"}
    unknown = set(value) - allowed_top
    if unknown:
        raise ValueError(f"chart state contains unknown fields: {sorted(unknown)}")
    if value.get("schema_version") != _CHART_STATE_SCHEMA_VERSION:
        raise ValueError(
            f"chart state schema_version must be {_CHART_STATE_SCHEMA_VERSION}, "
            f"got {value.get('schema_version')!r}"
        )
    fp = value.get("fingerprint")
    if not isinstance(fp, dict):
        raise ValueError("chart state fingerprint must be an object")
    for field in ("region", "data_server", "jacket_base_url"):
        if not isinstance(fp.get(field), str):
            raise ValueError(f"chart state fingerprint.{field} must be a string")
    scores = value.get("scores")
    if not isinstance(scores, dict):
        raise ValueError("chart state scores must be an object")
    for key, val in scores.items():
        if not isinstance(key, str):
            raise ValueError("chart state score keys must be strings")
        if not isinstance(val, str) or len(val) != 64 or val != val.lower():
            raise ValueError(
                f"chart state score hash for {key!r} must be a 64-char lowercase hex string"
            )
    return {
        "schema_version": _CHART_STATE_SCHEMA_VERSION,
        "fingerprint": {k: fp[k] for k in ("region", "data_server", "jacket_base_url")},
        "scores": dict(scores),
    }


def load_chart_state(path: StdPath) -> dict | None:
    """Load chart state from *path*, returning ``None`` when absent or corrupt."""
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        return validate_chart_state(json.loads(raw))
    except Exception:
        logger.warning("Corrupt chart state at %s; falling back to full rebuild", path)
        return None


def pending_score_paths(
    current: dict[str, str], stored: dict[str, str]
) -> list[str]:
    """Sorted relative paths that are new or whose content hash changed."""
    return sorted(
        path for path, digest in current.items() if digest != stored.get(path)
    )


# ---------------------------------------------------------------------------
# Incremental Live2D motion state
# ---------------------------------------------------------------------------

_LIVE2D_STATE_SCHEMA_VERSION = 1


def live2d_state_path(config) -> StdPath:
    """Return the filesystem path for persisted Live2D motion incremental state."""
    return StdPath(config.DL_LIST_CACHE_PATH).parent / "live2d_motion_state.json"


def compute_motion_bundle_hashes(motion_source: StdPath) -> dict[str, str]:
    """Return ``{file_name: sha256_hex}`` over every file in *motion_source*."""
    result: dict[str, str] = {}
    for path in sorted(p for p in motion_source.glob("*") if p.is_file()):
        result[path.name] = hash_score_file(path)
    return result


def compute_live2d_fingerprint(config, model_dir: StdPath) -> dict[str, str]:
    """Build a fingerprint dict from the current Live2D configuration.

    The fingerprint includes the Unity version and a composite hash over all
    ``*.moc3`` files under *model_dir* so that any moc3 content change
    invalidates the cached state.
    """
    # Build a deterministic composite hash of all moc3 files
    moc3_parts: list[str] = []
    for moc3_path in sorted(model_dir.rglob("*.moc3")):
        rel = moc3_path.relative_to(model_dir).as_posix()
        h = hash_score_file(moc3_path)
        moc3_parts.append(f"{rel}:{h}")
    model_hash = hashlib.sha256("\n".join(moc3_parts).encode()).hexdigest()
    return {
        "unity_version": getattr(config, "UNITY_VERSION", "") or "",
        "model_hash": model_hash,
    }


def validate_live2d_state(value: object) -> dict:
    """Strict validator for ``live2d_motion_state.json`` compatible with ``atomic_write_json``."""
    if not isinstance(value, dict):
        raise ValueError("live2d motion state must be an object")
    allowed_top = {"schema_version", "fingerprint", "motions"}
    unknown = set(value) - allowed_top
    if unknown:
        raise ValueError(f"live2d motion state contains unknown fields: {sorted(unknown)}")
    if value.get("schema_version") != _LIVE2D_STATE_SCHEMA_VERSION:
        raise ValueError(
            f"live2d motion state schema_version must be {_LIVE2D_STATE_SCHEMA_VERSION}, "
            f"got {value.get('schema_version')!r}"
        )
    fp = value.get("fingerprint")
    if not isinstance(fp, dict):
        raise ValueError("live2d motion state fingerprint must be an object")
    for field in ("unity_version", "model_hash"):
        if not isinstance(fp.get(field), str):
            raise ValueError(f"live2d motion state fingerprint.{field} must be a string")
    motions = value.get("motions")
    if not isinstance(motions, dict):
        raise ValueError("live2d motion state motions must be an object")
    for key, val in motions.items():
        if not isinstance(key, str):
            raise ValueError("live2d motion state motion keys must be strings")
        if not isinstance(val, str) or len(val) != 64 or val != val.lower():
            raise ValueError(
                f"live2d motion state hash for {key!r} must be a 64-char lowercase hex string"
            )
    return {
        "schema_version": _LIVE2D_STATE_SCHEMA_VERSION,
        "fingerprint": {k: fp[k] for k in ("unity_version", "model_hash")},
        "motions": dict(motions),
    }


def load_live2d_state(path: StdPath) -> dict | None:
    """Load Live2D motion state from *path*, returning ``None`` when absent or corrupt."""
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
        return validate_live2d_state(json.loads(raw))
    except Exception:
        logger.warning("Corrupt Live2D motion state at %s; falling back to full rebuild", path)
        return None


def pending_motion_bundles(
    current: dict[str, str], stored: dict[str, str]
) -> list[str]:
    """Sorted file names that are new or whose content hash changed."""
    return sorted(
        name for name, digest in current.items() if digest != stored.get(name)
    )


async def _process_live2d(
    config,
    motion_source: StdPath,
    extracted_dir: StdPath,
    *,
    skip_missing_sources: bool = False,
) -> None:
    """Incremental Live2D motion restore, upload, and state persist.

    State is only written after a successful restore **and** all uploads/publishes
    so that a crash mid-way causes a full rebuild on the next run.
    """
    live2d_storages = get_specialized_storage(config, "live2d")
    for storage in live2d_storages:
        _validate_live2d_storage(storage)

    model_dir = extracted_dir / "live2d" / "model"
    missing_sources = [str(p) for p in (motion_source, model_dir) if not p.is_dir()]
    if missing_sources:
        message = "Live2D post-processing sources are missing: " + ", ".join(missing_sources)
        if skip_missing_sources:
            logger.warning("Skipping optional Live2D post-processing: %s", message)
            return
        raise RuntimeError(message)

    param_id_map = await collect_param_id_map(Path(str(model_dir)))
    current = compute_motion_bundle_hashes(motion_source)
    fingerprint = compute_live2d_fingerprint(config, model_dir)
    stored_state = load_live2d_state(live2d_state_path(config))

    if (
        stored_state is not None
        and stored_state["fingerprint"] == fingerprint
        and not pending_motion_bundles(current, stored_state["motions"])
    ):
        logger.info("Live2D motions are up to date; skipping restore and upload")
        return

    # Determine which bundles to restore
    if stored_state is None or stored_state["fingerprint"] != fingerprint:
        to_restore_names = sorted(current)
    else:
        to_restore_names = pending_motion_bundles(current, stored_state["motions"])

    to_restore_paths = [motion_source / name for name in to_restore_names]

    await restore_live2d_motions(
        Path(str(motion_source)),
        Path(str(extracted_dir / "live2d" / "motion")),
        Path(str(model_dir)),
        config.UNITY_VERSION,
        config=config,
        param_id_map=param_id_map,
        bundle_paths=to_restore_paths,
    )

    for storage in live2d_storages:
        await _upload_live2d_assets(extracted_dir / "live2d", storage, config)
        models = await _remote_model_list(storage, config)
        await _publish_live2d_model_list(models, storage, config)

    # Persist state only after all uploads/publishes succeed.
    previous_motions = (
        stored_state["motions"]
        if stored_state is not None and stored_state["fingerprint"] == fingerprint
        else {}
    )
    merged_motions = {**previous_motions, **{name: current[name] for name in to_restore_names}}
    payload = {
        "schema_version": _LIVE2D_STATE_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "motions": merged_motions,
    }
    state_file = live2d_state_path(config)
    prepare_state_directory(state_file.parent)
    atomic_write_json(state_file, payload, validate_live2d_state)


async def _render_charts(
    config,
    extracted_dir: StdPath,
    include_list: list[str] | None = None,
    score_files: list[str] | None = None,
) -> set[str]:
    """Render chart SVGs and return the set of relative posix paths rendered.

    When *score_files* is provided (relative to ``music/music_score``), only
    those scores are rendered.  Otherwise every eligible score under
    *extracted_dir* is rendered.  Scores skipped due to an invalid music id
    or a missing ``musics.json`` entry are **not** in the returned set so
    they will be retried on the next run.
    """
    if score_files is not None:
        score_root = extracted_dir / "music" / "music_score"
        resolved = sorted(score_root / rel for rel in score_files)
    else:
        resolved = collect_score_files(extracted_dir, include_list)
    if not resolved:
        logger.info("No music score TextAssets found")
        return set()

    music_info = await get_list(get_json_url(get_chart_data_server(config), "musics"))
    music_by_id = {music["id"]: music for music in music_info}
    region = _region_name(config)
    rendered: set[str] = set()
    semaphore = asyncio.Semaphore(CHART_SOURCE_CONCURRENCY)

    async def render_score(score_file: StdPath) -> None:
        try:
            music_id = music_id_from_score_path(score_file)
        except ValueError:
            logger.warning("Skipping chart %s: invalid music id", score_file)
            return
        music = music_by_id.get(music_id)
        if music is None:
            logger.warning(
                "Skipping chart %s: music id %s is not in musics.json", score_file, music_id
            )
            return
        # The directory name is part of the published chart layout. Keep its
        # source spelling (for example, ``0001_song`` -> ``0001``), while the
        # numeric value above remains the musics.json lookup key.
        source_music_id = score_file.parent.name.split("_", 1)[0]
        chart_path = extracted_dir / "charts" / region / source_music_id / f"{score_file.stem}.svg"
        chart_path.parent.mkdir(parents=True, exist_ok=True)
        async with semaphore:
            await render_chart(
                score_file.as_posix(),
                chart_path.as_posix(),
                music,
                get_chart_jacket_url(config, region, music_id),
            )
        rel = score_file.relative_to(extracted_dir / "music" / "music_score").as_posix()
        rendered.add(rel)
        logger.info("Rendered chart for %s to %s", score_file, chart_path)

    await asyncio.gather(*(render_score(score_file) for score_file in resolved))
    return rendered


async def _process_charts(
    config, workspace_dir: StdPath, include_list: list[str] | None = None
) -> None:
    """Compute incremental state, render pending scores, upload, and persist state.

    State is only written after a successful render **and** upload so that a
    crash mid-way will cause a full rebuild on the next run.
    """
    current = compute_score_hashes(workspace_dir, include_list)
    stored_state = load_chart_state(chart_state_path(config))
    fingerprint = chart_fingerprint(config)

    if (
        stored_state is not None
        and stored_state["fingerprint"] == fingerprint
        and not pending_score_paths(current, stored_state["scores"])
    ):
        logger.info("Charts are up to date; skipping render and upload")
        return

    # Full rebuild when there is no stored state or the fingerprint changed.
    if stored_state is None or stored_state["fingerprint"] != fingerprint:
        to_render = sorted(current)
    else:
        to_render = pending_score_paths(current, stored_state["scores"])

    rendered = await _render_charts(config, workspace_dir, include_list, score_files=to_render)
    await _upload_specialized_directory(
        "charts", workspace_dir / "charts" / _region_name(config), config
    )

    # Persist state only after upload succeeds.
    previous_scores = (
        stored_state["scores"]
        if stored_state is not None and stored_state["fingerprint"] == fingerprint
        else {}
    )
    merged_scores = {**previous_scores, **{rel: current[rel] for rel in rendered}}
    payload = {
        "schema_version": _CHART_STATE_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "scores": merged_scores,
    }
    state_file = chart_state_path(config)
    prepare_state_directory(state_file.parent)
    atomic_write_json(state_file, payload, validate_chart_state)


async def run_specialized_postprocess(
    mode: str,
    config,
    *,
    extracted_dir_is_temporary: bool = False,
    skip_missing_sources: bool = False,
    score_include_list: list[str] | None = None,
) -> None:
    """Run mode-specific work only after every bundle has succeeded."""
    if config.ASSET_LOCAL_EXTRACTED_DIR is None:
        raise ValueError("Specialized modes require ASSET_LOCAL_EXTRACTED_DIR")
    if mode == "live2d" and getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None) is None:
        raise ValueError("live2d mode requires LIVE2D_BUNDLE_CACHE_DIR")
    extracted_dir = StdPath(str(config.ASSET_LOCAL_EXTRACTED_DIR))
    if mode == "live2d":
        motion_source = StdPath(str(config.LIVE2D_BUNDLE_CACHE_DIR)) / "live2d" / "motion"
        await _process_live2d(
            config,
            motion_source,
            extracted_dir,
            skip_missing_sources=skip_missing_sources,
        )
    elif mode == "charts":
        # A run-scoped extracted directory is already safe to populate. When
        # the user supplied a persistent directory, use a separate workspace
        # for the remote fallback so downloaded scores never pollute it.
        configured_extracted_dir = (
            None if extracted_dir_is_temporary else config.ASSET_LOCAL_EXTRACTED_DIR
        )
        temporary_source = needs_temporary_chart_source(
            extracted_dir, configured_extracted_dir, score_include_list
        )
        if temporary_source:
            with tempfile.TemporaryDirectory(prefix="sekai-charts-") as temp_dir:
                chart_source_dir = StdPath(temp_dir)
                try:
                    await fetch_chart_sources_from_storage(
                        config, chart_source_dir, score_include_list
                    )
                except Exception as exc:
                    if skip_missing_sources:
                        logger.warning("Skipping optional Charts post-processing: %s", exc)
                        return
                    raise
                await _process_charts(config, chart_source_dir, score_include_list)
        else:
            if not has_local_chart_sources(extracted_dir, score_include_list):
                try:
                    await fetch_chart_sources_from_storage(
                        config, extracted_dir, score_include_list
                    )
                except Exception as exc:
                    if skip_missing_sources:
                        logger.warning("Skipping optional Charts post-processing: %s", exc)
                        return
                    raise
            await _process_charts(config, extracted_dir, score_include_list)
    else:
        raise ValueError(f"Unsupported specialized mode: {mode}")


async def _upload_specialized_directory(mode: str, source_dir: Path, config) -> None:
    if not get_specialized_storage(config, mode) or not source_dir.exists():
        return
    region = _region_name(config)
    for storage in get_specialized_storage(config, mode):
        remote_path = Path(storage["base"]) / ("live2d" if mode == "live2d" else region)
        await upload_directory(
            source_dir,
            remote_path,
            storage["program"],
            storage["args"],
            config=config,
        )
