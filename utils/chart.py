import ctypes
import ctypes.util
import asyncio
import logging
import tempfile
import sys
from pathlib import Path
from urllib.parse import urlparse
from typing import List

import aiohttp
from anyio import open_file

logger = logging.getLogger("charts")
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
                logger.debug("Failed to preload %s", freetype, exc_info=True)

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
