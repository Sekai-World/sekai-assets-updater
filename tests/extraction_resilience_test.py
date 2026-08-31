from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from updater.extract import unity_objects
from updater.unity_rs_adapter import InvalidImageDimensions


class _TextObject:
    type = SimpleNamespace(name="TextAsset")
    class_id = 49


class _FontObject:
    type = SimpleNamespace(name="Font")
    class_id = 128


@pytest.mark.parametrize(
    ("name", "payload", "suffix"),
    [
        ("font.ttf.bytes", b"\x00\x01\x00\x00" + b"font", ".ttf"),
        ("font.otf.bytes", b"OTTO" + b"font", ".otf"),
    ],
)
def test_font_textasset_keeps_binary_bytes_and_detects_extension(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    payload: bytes,
    suffix: str,
) -> None:
    output: list[Path] = []
    monkeypatch.setattr(unity_objects, "read_text_bytes", lambda _obj: payload)

    unity_objects._extract_text_asset(
        _TextObject(), tmp_path / name, False, {}, output
    )

    expected = tmp_path / f"font{suffix}"
    assert expected.read_bytes() == payload
    assert output == [expected]


@pytest.mark.parametrize(
    ("name", "payload", "suffix"),
    [("font.otf", b"OTTO" + b"font", ".otf"), ("font.ttf", b"\x00\x01\x00\x00font", ".ttf")],
)
def test_font_object_uses_native_bytes_and_skips_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, payload: bytes, suffix: str
) -> None:
    output: list[Path] = []
    monkeypatch.setattr(unity_objects, "read_font_bytes", lambda _obj: payload)

    unity_objects._extract_font_object(_FontObject(), tmp_path / name, output)

    expected = tmp_path / f"font{suffix}"
    assert expected.read_bytes() == payload
    assert output == [expected]

    output.clear()
    monkeypatch.setattr(unity_objects, "read_font_bytes", lambda _obj: b"")
    unity_objects._extract_font_object(_FontObject(), tmp_path / "empty.otf", output)
    assert output == []
    assert not (tmp_path / "empty.otf").exists()


def test_empty_texture_is_warned_and_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog) -> None:
    obj = SimpleNamespace(type=SimpleNamespace(name="Texture2D"))
    monkeypatch.setattr(
        unity_objects,
        "render_image_asset",
        lambda _obj: (_ for _ in ()).throw(InvalidImageDimensions("invalid dimensions 0x0")),
    )
    exported: list[Path] = []

    with caplog.at_level("WARNING"):
        unity_objects._extract_one_object(
            SimpleNamespace(), "font/empty.texture", obj, tmp_path / "empty.png", ("png",),
            False, 2, "fast", [], {}, [], [], exported
        )

    assert exported == []
    assert "skipping" in caplog.text
    assert not list(tmp_path.iterdir())


def test_plain_textasset_is_not_mislabeled_as_font(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output: list[Path] = []
    monkeypatch.setattr(unity_objects, "read_text_bytes", lambda _obj: b"ordinary text")

    unity_objects._extract_text_asset(_TextObject(), tmp_path / "notice.bytes", False, {}, output)

    assert output == [tmp_path / "notice"]
    assert (tmp_path / "notice").read_bytes() == b"ordinary text"


def test_malformed_playable_is_skipped_at_object_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
) -> None:
    monkeypatch.setattr(
        unity_objects,
        "extract_playable",
        lambda *_args: (_ for _ in ()).throw(OSError("failed to fill whole buffer")),
    )
    obj = SimpleNamespace(type=SimpleNamespace(name="MonoBehaviour"))
    exported: list[Path] = []

    with caplog.at_level("WARNING"):
        unity_objects._extract_one_object(
            SimpleNamespace(), "effect_asset/gacha/anim_02.playable", obj,
            tmp_path / "anim_02.playable.json", ("png",), False, 2, "fast", [], {}, [], [], exported
        )

    assert exported == []
    assert "failed to fill whole buffer" in caplog.text
