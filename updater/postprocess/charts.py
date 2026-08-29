import asyncio
import ctypes
import ctypes.util
import logging
import re
import sys
import tempfile
from pathlib import Path
from pathlib import Path as StdPath
from typing import List
from urllib.parse import urlparse

import aiohttp
from anyio import open_file

from updater.net.http import get_request_timeout
from updater.postprocess.config import (
    _region_name,
    get_chart_data_server,
    get_chart_jacket_url,
    get_normal_storage_candidates,
)

# The fetch/render flows came from specialized.py and keep its channel so
# name-based log filtering is unchanged; the renderer preload keeps the
# historic "charts" channel from utils/chart.py.
logger = logging.getLogger("asset_updater")
_renderer_logger = logging.getLogger("charts")
DEFAULT_STYLE_SHEET = (
    Path(__file__).with_name("pjsekai_scores_default.css").read_text(encoding="utf-8")
)
_scores = None


def _load_scores_module():
    global _scores
    if _scores is not None:
        return _scores

    if sys.platform.startswith("linux"):
        freetype = ctypes.util.find_library("freetype")
        if freetype:
            try:
                ctypes.CDLL(freetype, mode=getattr(ctypes, "RTLD_GLOBAL", 0))
            except OSError:
                _renderer_logger.debug("Failed to preload %s", freetype, exc_info=True)

    try:
        import pjsekai_scores_rs as scores
    except ImportError as exc:
        raise ImportError(
            "Failed to import pjsekai_scores_rs. On Linux, a freetype library "
            "exporting FT_Palette_Data_Get must be available."
        ) from exc

    _scores = scores
    return scores


async def _prepare_jacket(jacket: str) -> tuple[str, tempfile.TemporaryDirectory | None]:
    parsed = urlparse(jacket)
    if parsed.scheme == "file":
        return jacket, None
    if parsed.scheme == "" and Path(jacket).exists():
        return Path(jacket).resolve().as_uri(), None

    if parsed.scheme not in ("http", "https"):
        return jacket, None

    tmpdir = tempfile.TemporaryDirectory()
    target_path = Path(tmpdir.name) / "jacket.png"

    async with aiohttp.ClientSession() as session:
        async with session.get(jacket) as response:
            response.raise_for_status()
            async with await open_file(target_path, "wb") as f:
                await f.write(await response.read())

    return target_path.as_uri(), tmpdir


async def render_chart(score_path: str, chart_path: str, music: dict, jacket: str):
    scores = _load_scores_module()
    score = await asyncio.to_thread(scores.Score.open_sus, score_path)
    jacket_uri, jacket_tmpdir = await _prepare_jacket(jacket)
    try:
        score.set_meta(title=music["title"], jacket=jacket_uri)
        drawing = scores.Drawing(
            note_host="https://asset3.pjsekai.moe/live/note/custom01",
            style_sheet=DEFAULT_STYLE_SHEET,
            generator="Sekai Viewer",
        )

        png_path = chart_path.replace(".svg", ".png")

        svg = await asyncio.to_thread(drawing.svg, score)
        async with await open_file(chart_path, "wb") as f:
            await f.write(svg.encode("utf-8"))

        png = await asyncio.to_thread(drawing.png, score)
        async with await open_file(png_path, "wb") as f:
            await f.write(png)
    finally:
        if jacket_tmpdir is not None:
            jacket_tmpdir.cleanup()


async def get_list(url: str) -> List[dict]:
    # use aiohttp to get the list from url
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json()


def get_json_url(server: str, json_name: str) -> str:
    if server == "jp":
        return f"https://sekai-world.github.io/sekai-master-db-diff/{json_name}.json"
    else:
        return f"https://sekai-world.github.io/sekai-master-db-{server}-diff/{json_name}.json"


CHART_SOURCE_CONCURRENCY = 4

CHART_SOURCE_TERMINATE_TIMEOUT = 5


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
