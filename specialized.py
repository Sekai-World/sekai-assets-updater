"""Post-processing for the optional live2d and charts pipelines."""

import logging
import asyncio
import tempfile
from pathlib import Path as StdPath

import orjson as json
from anyio import Path

from helpers import upload_directory
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


def collect_score_files(extracted_dir: StdPath) -> list[StdPath]:
    """Collect only chart TextAsset outputs, with deterministic de-duplication."""
    score_root = extracted_dir / "music" / "music_score"
    return sorted({path for path in score_root.rglob("*.txt") if path.is_file()})


def has_local_chart_sources(extracted_dir: StdPath) -> bool:
    """Whether an extraction root already contains usable chart source files."""
    return bool(collect_score_files(extracted_dir))


def needs_temporary_chart_source(
    extracted_dir: StdPath,
    configured_extracted_dir,
) -> bool:
    """Avoid writing remote chart sources into a user's persistent extraction root."""
    return configured_extracted_dir is not None and not has_local_chart_sources(extracted_dir)


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


async def fetch_chart_sources_from_storage(config, extracted_dir: StdPath) -> None:
    """Copy chart sources from the first successful normal asset mirror."""
    target_dir = extracted_dir / "music" / "music_score"
    target_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    for storage in get_normal_storage_candidates(config):
        try:
            await _copy_chart_sources_from_one_storage(storage, target_dir, config)
            if not has_local_chart_sources(extracted_dir):
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


async def _render_charts(config, extracted_dir: StdPath) -> None:
    score_files = collect_score_files(extracted_dir)
    if not score_files:
        logger.info("No music score TextAssets found")
        return

    music_info = await get_list(get_json_url(_region_name(config), "musics"))
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
        chart_path = extracted_dir / "charts" / region / str(music_id) / f"{score_file.stem}.svg"
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
) -> None:
    """Run mode-specific work only after every bundle has succeeded."""
    if config.ASSET_LOCAL_EXTRACTED_DIR is None:
        raise ValueError("Specialized modes require ASSET_LOCAL_EXTRACTED_DIR")
    if mode == "live2d" and getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None) is None:
        raise ValueError("live2d mode requires LIVE2D_BUNDLE_CACHE_DIR")
    extracted_dir = StdPath(str(config.ASSET_LOCAL_EXTRACTED_DIR))
    if mode == "live2d":
        await restore_live2d_motions(
            Path(str(config.LIVE2D_BUNDLE_CACHE_DIR)) / "live2d" / "motion",
            Path(str(extracted_dir / "live2d" / "motion")),
            Path(str(extracted_dir / "live2d" / "model")),
            config.UNITY_VERSION,
            config=config,
        )
        model_dir = extracted_dir / "live2d" / "model"
        model_list_path = extracted_dir / "live2d" / "model_list.json"
        model_list_path.write_bytes(json.dumps(_model_list(model_dir), option=json.OPT_INDENT_2))
        await _upload_specialized_directory("live2d", extracted_dir / "live2d", config)
    elif mode == "charts":
        # A run-scoped extracted directory is already safe to populate. When
        # the user supplied a persistent directory, use a separate workspace
        # for the remote fallback so downloaded scores never pollute it.
        configured_extracted_dir = (
            None if extracted_dir_is_temporary else config.ASSET_LOCAL_EXTRACTED_DIR
        )
        temporary_source = needs_temporary_chart_source(extracted_dir, configured_extracted_dir)
        if temporary_source:
            with tempfile.TemporaryDirectory(prefix="sekai-charts-") as temp_dir:
                chart_source_dir = StdPath(temp_dir)
                await fetch_chart_sources_from_storage(config, chart_source_dir)
                await _render_charts(config, chart_source_dir)
                await _upload_specialized_directory(
                    "charts",
                    chart_source_dir / "charts" / _region_name(config),
                    config,
                )
        else:
            if not has_local_chart_sources(extracted_dir):
                await fetch_chart_sources_from_storage(config, extracted_dir)
            await _render_charts(config, extracted_dir)
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
