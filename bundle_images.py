"""Texture and sprite rendering for extracted Unity assets."""

import logging
import os
import tempfile
from pathlib import Path

from PIL import Image

from security import SecurityError, resolve_secure_path
from unity_rs_adapter import UnityRsObject, read_image

logger = logging.getLogger("live2d")


def should_fallback_sprite_render(exc: Exception) -> bool:
    if isinstance(exc, ValueError):
        return "Coordinate 'lower' is less than 'upper'" in str(exc)
    if isinstance(exc, StopIteration):
        return True
    return isinstance(exc, RuntimeError) and isinstance(exc.__cause__, StopIteration)


def render_sprite_with_fallback(data: UnityRsObject) -> Image.Image:
    """Decode a Sprite, including atlas crop/packing in unity-rs."""

    return read_image(data)


def render_image_asset(
    data: UnityRsObject | Image.Image,
) -> Image.Image:
    if isinstance(data, Image.Image):
        return data
    return read_image(data)


def save_image_formats(
    image: Image.Image,
    save_path: Path,
    texture_output_formats: tuple[str, ...],
) -> list[Path]:
    saved_paths: list[Path] = []
    for image_format in texture_output_formats:
        output_path = Path(
            resolve_secure_path(save_path.parent, f"{save_path.stem}.{image_format}").as_posix()
        )
        logger.debug("Saving texture to %s", output_path)
        if output_path.exists() and output_path.is_symlink():
            raise SecurityError(f"image output must not be a symlink: {output_path}")
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
                image.save(temporary_file, format=image_format)
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
