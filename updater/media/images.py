"""Texture and sprite rendering for extracted Unity assets."""

import logging
import os
import tempfile
from pathlib import Path

from PIL import Image

from updater.security import SecurityError, resolve_secure_path
from updater.unity_rs_adapter import RenderedImage, UnityRsObject, read_image

logger = logging.getLogger("live2d")

# libwebp effort level for lossy WebP output.  Method 2 produces output within
# a few percent of the default (method 4) size at roughly half the CPU cost;
# all Python WebP bindings wrap the same libwebp, so this knob — not the
# library choice — is what controls encode speed.
DEFAULT_WEBP_METHOD = 2
# unity-rs native PNG profile.  "fast" (fdeflate) measures ~7x faster than
# PIL's default zlib level 6 at ~10% larger output.
DEFAULT_PNG_COMPRESSION = "fast"


def should_fallback_sprite_render(exc: Exception) -> bool:
    if isinstance(exc, ValueError):
        return "Coordinate 'lower' is less than 'upper'" in str(exc)
    if isinstance(exc, StopIteration):
        return True
    return isinstance(exc, RuntimeError) and isinstance(exc.__cause__, StopIteration)


def render_sprite_with_fallback(data: UnityRsObject) -> RenderedImage:
    """Decode a Sprite, including atlas crop/packing in unity-rs."""

    return read_image(data)


def render_image_asset(
    data: UnityRsObject | RenderedImage | Image.Image,
) -> RenderedImage | Image.Image:
    if isinstance(data, (RenderedImage, Image.Image)):
        return data
    return read_image(data)


def _encode_image(
    image: RenderedImage | Image.Image,
    image_format: str,
    png_compression: str | int,
) -> tuple[bytes | None, Image.Image | None]:
    """Return either a pre-encoded payload or a PIL image to save directly."""
    format_key = image_format.lower()
    if isinstance(image, RenderedImage):
        if format_key == "png":
            payload = image.encode_png(compression=png_compression)
            if payload is not None:
                return payload, None
        return None, image.to_pil()
    return None, image


def save_image_formats(
    image: RenderedImage | Image.Image,
    save_path: Path,
    texture_output_formats: tuple[str, ...],
    *,
    webp_method: int = DEFAULT_WEBP_METHOD,
    png_compression: str | int = DEFAULT_PNG_COMPRESSION,
) -> list[Path]:
    saved_paths: list[Path] = []
    for image_format in texture_output_formats:
        output_path = Path(
            resolve_secure_path(save_path.parent, f"{save_path.stem}.{image_format}").as_posix()
        )
        logger.debug("Saving texture to %s", output_path)
        if output_path.exists() and output_path.is_symlink():
            raise SecurityError(f"image output must not be a symlink: {output_path}")
        payload, pil_image = _encode_image(image, image_format, png_compression)
        temporary_path: Path | None = None
        descriptor: int | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                dir=output_path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w+b") as temporary_file:
                descriptor = None
                if payload is not None:
                    temporary_file.write(payload)
                elif image_format.lower() == "webp":
                    pil_image.save(temporary_file, format=image_format, method=webp_method)
                else:
                    pil_image.save(temporary_file, format=image_format)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, output_path)
            temporary_path = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        saved_paths.append(output_path)
    return saved_paths


def get_texture_output_formats(config) -> tuple[str, ...]:
    value = getattr(config, "TEXTURE_OUTPUT_FORMATS", ("png", "webp"))
    if isinstance(value, str):
        formats = [part.strip().lower().removeprefix(".") for part in value.split(",")]
    else:
        formats = [str(part).strip().lower().removeprefix(".") for part in value]

    valid_formats = []
    for image_format in formats:
        if image_format in {"png", "webp"} and image_format not in valid_formats:
            valid_formats.append(image_format)

    if not valid_formats and formats:
        logger.warning("No valid TEXTURE_OUTPUT_FORMATS found, skipping texture export")

    return tuple(valid_formats)


def get_texture_webp_method(config) -> int:
    value = getattr(config, "TEXTURE_WEBP_METHOD", DEFAULT_WEBP_METHOD)
    try:
        method = int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid TEXTURE_WEBP_METHOD=%r, using default", value)
        return DEFAULT_WEBP_METHOD
    if not 0 <= method <= 6:
        logger.warning("TEXTURE_WEBP_METHOD must be within 0-6, got %r; using default", value)
        return DEFAULT_WEBP_METHOD
    return method


def get_texture_png_compression(config) -> str | int:
    value = getattr(config, "TEXTURE_PNG_COMPRESSION", DEFAULT_PNG_COMPRESSION)
    # unity-rs 0.5+ also accepts an explicit zlib level (0-9).
    if type(value) is int and 0 <= value <= 9:
        return value
    text = str(value).lower()
    if text in {"fast", "default", "best"}:
        return text
    logger.warning("Invalid TEXTURE_PNG_COMPRESSION=%r, using default", value)
    return DEFAULT_PNG_COMPRESSION
