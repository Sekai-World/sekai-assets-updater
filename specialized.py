"""Post-processing for the optional live2d and charts pipelines."""

import logging
from pathlib import Path as StdPath

import orjson as json
from anyio import Path

from helpers import upload_directory
from utils.chart import get_json_url, get_list, render_chart
from utils.live2d import restore_live2d_motions

logger = logging.getLogger("asset_updater")

SPECIALIZED_MODES = ("live2d", "charts")


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


def get_specialized_storage(config, mode: str) -> list[dict]:
    """Read the independent upload targets for a specialized mode."""
    if mode not in SPECIALIZED_MODES:
        raise ValueError(f"Unsupported specialized mode: {mode}")
    return list(getattr(config, f"{mode.upper()}_REMOTE_STORAGE", None) or [])


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


def music_id_from_score_path(score_path: StdPath) -> int:
    """Parse the numeric music id from a score directory such as ``001_foo``."""
    return int(score_path.parent.name.split("_", 1)[0])


async def _render_charts(config, extracted_dir: Path) -> None:
    score_files = collect_score_files(extracted_dir)
    if not score_files:
        logger.info("No music score TextAssets found")
        return

    music_info = await get_list(get_json_url(_region_name(config), "musics"))
    music_by_id = {music["id"]: music for music in music_info}
    region = _region_name(config)
    for score_file in score_files:
        music_id = music_id_from_score_path(score_file)
        music = music_by_id.get(music_id)
        if music is None:
            logger.warning("Skipping chart %s: music id %s is not in musics.json", score_file, music_id)
            continue
        padded_id = str(music_id).zfill(3)
        chart_path = extracted_dir / "charts" / region / str(music_id) / f"{score_file.stem}.svg"
        chart_path.parent.mkdir(parents=True, exist_ok=True)
        await render_chart(
            score_file.as_posix(),
            chart_path.as_posix(),
            music,
            f"https://storage.sekai.best/sekai-{region}-assets/music/jacket/jacket_s_{padded_id}/jacket_s_{padded_id}.png",
        )
        logger.info("Rendered chart for %s to %s", score_file, chart_path)


async def run_specialized_postprocess(mode: str, config) -> None:
    """Run mode-specific work only after every bundle has succeeded."""
    if config.ASSET_LOCAL_EXTRACTED_DIR is None:
        raise ValueError("Specialized modes require ASSET_LOCAL_EXTRACTED_DIR")
    if mode == "live2d" and config.ASSET_LOCAL_BUNDLE_CACHE_DIR is None:
        raise ValueError("live2d mode requires ASSET_LOCAL_BUNDLE_CACHE_DIR")
    extracted_dir = StdPath(str(config.ASSET_LOCAL_EXTRACTED_DIR))
    if mode == "live2d":
        await restore_live2d_motions(
            Path(str(config.ASSET_LOCAL_BUNDLE_CACHE_DIR)) / "live2d" / "motion",
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
        await _render_charts(config, extracted_dir)
        await _upload_specialized_directory("charts", extracted_dir / "charts" / _region_name(config), config)
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
        )
