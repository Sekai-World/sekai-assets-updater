import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from updater.postprocess import charts
from updater.postprocess import config as chart_config
from updater.postprocess.config import get_chart_font_kwargs


def test_chart_fonts_use_configured_paths_and_dirs() -> None:
    config = SimpleNamespace(
        CHART_FONT_PATHS=[Path("/fonts/NotoSansCJKjp-Regular.otf")],
        CHART_FONT_DIRS=["/fonts/extra"],
    )

    assert get_chart_font_kwargs(config) == {
        "font_paths": ["/fonts/NotoSansCJKjp-Regular.otf"],
        "font_dirs": ["/fonts/extra"],
    }


def test_chart_fonts_fall_back_to_existing_system_font_dirs(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with patch.object(chart_config, "DEFAULT_CHART_FONT_DIRS", (str(tmp_path), str(missing))):
        assert get_chart_font_kwargs(SimpleNamespace()) == {"font_dirs": [str(tmp_path)]}


def test_chart_fonts_are_empty_without_any_font_dir() -> None:
    with patch.object(chart_config, "DEFAULT_CHART_FONT_DIRS", ()):
        assert get_chart_font_kwargs(SimpleNamespace()) == {}


class _FakeScore:
    def set_meta(self, **meta) -> None:
        self.meta = meta


class _FakeDrawing:
    created: list[dict] = []

    def __init__(self, **kwargs) -> None:
        _FakeDrawing.created.append(kwargs)

    def svg(self, score) -> str:
        return "<svg/>"

    def png(self, score) -> bytes:
        return b"\x89PNG"


def test_render_chart_passes_fonts_to_the_drawing(tmp_path: Path) -> None:
    scores = SimpleNamespace(
        Score=SimpleNamespace(open_sus=lambda path: _FakeScore()),
        Drawing=_FakeDrawing,
    )
    jacket = tmp_path / "jacket.png"
    jacket.write_bytes(b"")
    chart_path = tmp_path / "master.svg"
    with patch.object(charts, "_load_scores_module", return_value=scores):
        asyncio.run(
            charts.render_chart(
                str(tmp_path / "master.txt"),
                str(chart_path),
                {"title": "Song"},
                str(jacket),
                font_paths=["/fonts/a.otf"],
            )
        )

    assert _FakeDrawing.created[-1]["font_paths"] == ["/fonts/a.otf"]
    assert _FakeDrawing.created[-1]["font_dirs"] is None
    assert chart_path.read_text() == "<svg/>"
    assert (tmp_path / "master.png").read_bytes() == b"\x89PNG"
