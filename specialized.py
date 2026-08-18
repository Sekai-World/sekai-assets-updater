"""Post-processing for the optional live2d and charts pipelines."""

import logging
import asyncio
import re
import shutil
import tempfile
from pathlib import Path as StdPath

import orjson as json
from anyio import Path

from helpers import upload_directory
from helpers import _get_external_process_timeout
from helpers import get_request_timeout
from utils.chart import get_json_url, get_list, render_chart
from utils.live2d import restore_live2d_motions

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
    jacket_base_url = getattr(config, "CHART_JACKET_BASE_URL", None)
    if not jacket_base_url:
        jacket_base_url = DEFAULT_CHART_JACKET_BASE_URL.format(region=region)
    padded_id = str(music_id).zfill(3)
    jacket_name = f"jacket_s_{padded_id}.png"
    return f"{jacket_base_url.rstrip('/')}/jacket_s_{padded_id}/{jacket_name}"


async def _render_charts(
    config, extracted_dir: StdPath, include_list: list[str] | None = None
) -> None:
    score_files = collect_score_files(extracted_dir, include_list)
    if not score_files:
        logger.info("No music score TextAssets found")
        return

    music_info = await get_list(get_json_url(get_chart_data_server(config), "musics"))
    music_by_id = {music["id"]: music for music in music_info}
    region = _region_name(config)
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
        chart_path = (
            extracted_dir / "charts" / region / source_music_id / f"{score_file.stem}.svg"
        )
        chart_path.parent.mkdir(parents=True, exist_ok=True)
        async with semaphore:
            await render_chart(
                score_file.as_posix(),
                chart_path.as_posix(),
                music,
                get_chart_jacket_url(config, region, music_id),
            )
        logger.info("Rendered chart for %s to %s", score_file, chart_path)

    await asyncio.gather(*(render_score(score_file) for score_file in score_files))


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
        live2d_storages = get_specialized_storage(config, "live2d")
        for storage in live2d_storages:
            _validate_live2d_storage(storage)
        motion_source = StdPath(str(config.LIVE2D_BUNDLE_CACHE_DIR)) / "live2d" / "motion"
        model_source = extracted_dir / "live2d" / "model"
        missing_sources = [str(path) for path in (motion_source, model_source) if not path.is_dir()]
        if missing_sources:
            message = "Live2D post-processing sources are missing: " + ", ".join(missing_sources)
            if skip_missing_sources:
                logger.warning("Skipping optional Live2D post-processing: %s", message)
                return
            raise RuntimeError(message)
        await restore_live2d_motions(
            Path(str(motion_source)),
            Path(str(extracted_dir / "live2d" / "motion")),
            Path(str(extracted_dir / "live2d" / "model")),
            config.UNITY_VERSION,
            config=config,
        )
        for storage in live2d_storages:
            await _upload_live2d_assets(extracted_dir / "live2d", storage, config)
            models = await _remote_model_list(storage, config)
            await _publish_live2d_model_list(models, storage, config)
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
                await _render_charts(config, chart_source_dir, score_include_list)
                await _upload_specialized_directory(
                    "charts",
                    chart_source_dir / "charts" / _region_name(config),
                    config,
                )
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
            await _render_charts(config, extracted_dir, score_include_list)
            await _upload_specialized_directory(
                "charts", extracted_dir / "charts" / _region_name(config), config
            )
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
