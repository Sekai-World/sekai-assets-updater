"""Tests for the performance-focused encode, upload, and cache-lookup paths."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from updater.extract import acb_cache as bundle_acb_cache
from updater.media import images as media_images
from updater.media.images import save_image_formats
from updater.security import SecurityError
from updater.storage import rclone as storage_rclone
from updater.storage.rclone import upload_to_storage
from updater.unity_rs_adapter import RenderedImage, _rendered_image


class _FakeNativeImage:
    """Native RgbaImage stand-in with a recording Rust-style encoder."""

    def __init__(self, width: int = 2, height: int = 2) -> None:
        self.width = width
        self.height = height
        self.rgba = b"\x10\x20\x30\xff" * (width * height)
        self.encode_calls: list[tuple[str, dict]] = []

    def encode(self, image_format: str, **kwargs) -> bytes:
        self.encode_calls.append((image_format, kwargs))
        return b"native-" + image_format.encode()


class _FakeNativeImageWithoutEncoder:
    def __init__(self, width: int = 2, height: int = 2) -> None:
        self.width = width
        self.height = height
        self.rgba = b"\x10\x20\x30\xff" * (width * height)


def test_save_image_formats_uses_native_png_and_pil_webp(tmp_path: Path) -> None:
    native = _FakeNativeImage()
    image = _rendered_image(native)
    saved = save_image_formats(image, tmp_path / "texture.asset", ("png", "webp"))

    assert saved == [tmp_path / "texture.png", tmp_path / "texture.webp"]
    assert (tmp_path / "texture.png").read_bytes() == b"native-png"
    assert native.encode_calls == [("png", {"compression": "fast"})]
    # WebP stays on the PIL/libwebp side; RIFF container proves a real encode.
    assert (tmp_path / "texture.webp").read_bytes()[:4] == b"RIFF"


def test_save_image_formats_falls_back_to_pil_without_native_encoder(tmp_path: Path) -> None:
    image = _rendered_image(_FakeNativeImageWithoutEncoder())
    saved = save_image_formats(image, tmp_path / "texture.asset", ("png",))

    assert saved == [tmp_path / "texture.png"]
    assert (tmp_path / "texture.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_save_image_formats_honours_png_compression_option(tmp_path: Path) -> None:
    native = _FakeNativeImage()
    save_image_formats(
        _rendered_image(native),
        tmp_path / "texture.asset",
        ("png",),
        png_compression="best",
    )
    assert native.encode_calls == [("png", {"compression": "best"})]


def test_save_image_formats_still_accepts_plain_pil_images(tmp_path: Path) -> None:
    with Image.new("RGBA", (1, 1), (0, 128, 255, 255)) as image:
        saved = save_image_formats(image, tmp_path / "texture.asset", ("png", "webp"))
    assert saved == [tmp_path / "texture.png", tmp_path / "texture.webp"]
    assert (tmp_path / "texture.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert (tmp_path / "texture.webp").read_bytes()[:4] == b"RIFF"


def test_rendered_image_to_pil_validates_pixels() -> None:
    image = RenderedImage(
        native=SimpleNamespace(width=2, height=2, rgba=b"short"), width=2, height=2
    )
    from updater.unity_rs_adapter import UnsupportedUnityObjectError

    with pytest.raises(UnsupportedUnityObjectError):
        image.to_pil()


def test_upload_batches_rclone_copy_into_single_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nested").mkdir()
    first = tmp_path / "first.mp3"
    second = tmp_path / "nested" / "second.mp3"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    recorded: list[tuple[tuple, str]] = []

    async def fake_create_subprocess_exec(*args, **_kwargs):
        list_path = args[args.index("--files-from-raw") + 1]
        recorded.append((args, Path(list_path).read_text(encoding="utf-8")))
        process = SimpleNamespace(returncode=0)
        return process

    async def fake_wait(_process, _timeout):
        return 0

    monkeypatch.setattr(
        storage_rclone.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(storage_rclone, "_wait_for_process", fake_wait)

    asyncio.run(
        upload_to_storage(
            [first, second],
            tmp_path,
            "remote:bucket/prefix",
            "rclone",
            ["copy", "src", "dst"],
            max_concurrent_uploads=7,
        )
    )

    assert len(recorded) == 1
    args, listed = recorded[0]
    assert args[0] == "rclone"
    assert args[1] == "copy"
    assert args[2] == str(tmp_path)
    assert args[3] == "remote:bucket/prefix"
    assert args[args.index("--transfers") + 1] == "7"
    assert sorted(listed.split()) == ["first.mp3", "nested/second.mp3"]


def test_upload_batch_failure_raises_runtime_error(tmp_path: Path, fake_subprocess) -> None:
    exported = tmp_path / "song.mp3"
    exported.write_bytes(b"audio")
    fake_process = fake_subprocess(returncode=1)

    upload = upload_to_storage(
        [exported], tmp_path, "remote:bucket/prefix", "rclone", ["copy", "src", "dst"]
    )
    with pytest.raises(RuntimeError, match=r"upload\(s\) failed"):
        asyncio.run(upload)
    assert len(fake_process.calls) == 1


def test_upload_batch_rejects_outside_source_before_subprocess(
    tmp_path: Path, fake_subprocess
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.mp3"
    outside.write_bytes(b"outside")
    fake_process = fake_subprocess()

    upload = upload_to_storage(
        [outside], tmp_path, "remote:bucket/prefix", "rclone", ["copy", "src", "dst"]
    )
    with pytest.raises(SecurityError):
        asyncio.run(upload)
    assert fake_process.calls == []


def test_cached_acb_lookup_remembers_source_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_root = tmp_path / "cache-store"
    cache_root.mkdir()
    for name in ("aaa.bundle", "bbb.bundle", "voice"):
        (cache_root / name).write_bytes(b"cache")
    bundle_path = tmp_path / "current.bundle"
    bundle_path.write_bytes(b"current")
    output = tmp_path / "artifact" / "voice.acb"
    output.parent.mkdir()

    class _CachedUnityObject:
        type = SimpleNamespace(name="TextAsset")

    loads: list[str] = []

    def fake_load(path, _version):
        loads.append(Path(path).name)
        if Path(path).name == "voice":
            return SimpleNamespace(container={"audio/voice.acb.bytes": _CachedUnityObject()})
        return SimpleNamespace(container={})

    monkeypatch.setattr(bundle_acb_cache, "load_bundle", fake_load)
    monkeypatch.setattr(bundle_acb_cache, "read_text_bytes", lambda _obj: b"cached acb")
    bundle_acb_cache._FOUND_BUNDLE_CACHE.clear()

    assert bundle_acb_cache.extract_acb_from_cached_bundles(
        bundle_path, "voice.acb.bytes", output, None, cache_root
    )
    # The name-matching candidate is tried before unrelated bundles.
    assert loads[0] == "voice"
    assert output.read_bytes() == b"cached acb"

    loads.clear()
    output.unlink()
    assert bundle_acb_cache.extract_acb_from_cached_bundles(
        bundle_path, "voice.acb.bytes", output, None, cache_root
    )
    # The remembered source bundle short-circuits the cache scan entirely.
    assert loads == ["voice"]
    assert output.read_bytes() == b"cached acb"


def test_png_compression_config_accepts_profiles_and_levels() -> None:

    assert media_images.get_texture_png_compression(SimpleNamespace()) == "fast"
    assert (
        media_images.get_texture_png_compression(SimpleNamespace(TEXTURE_PNG_COMPRESSION="best"))
        == "best"
    )
    assert media_images.get_texture_png_compression(SimpleNamespace(TEXTURE_PNG_COMPRESSION=3)) == 3
    assert (
        media_images.get_texture_png_compression(SimpleNamespace(TEXTURE_PNG_COMPRESSION=17))
        == "fast"
    )
    assert (
        media_images.get_texture_png_compression(SimpleNamespace(TEXTURE_PNG_COMPRESSION="zopfli"))
        == "fast"
    )
