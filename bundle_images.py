"""Texture and sprite rendering for extracted Unity assets."""

import logging
import os
import tempfile
from pathlib import Path

import UnityPy
import UnityPy.classes
from PIL import Image
from UnityPy.enums.ClassIDType import ClassIDType
from UnityPy.enums.SpritePackingRotation import SpritePackingRotation
from UnityPy.export.SpriteHelper import SpriteSettings, get_image

from security import SecurityError, resolve_secure_path

logger = logging.getLogger("live2d")


def should_fallback_sprite_render(exc: Exception) -> bool:
    if isinstance(exc, ValueError):
        return "Coordinate 'lower' is less than 'upper'" in str(exc)
    if isinstance(exc, StopIteration):
        return True
    return isinstance(exc, RuntimeError) and isinstance(exc.__cause__, StopIteration)


def get_sprite_atlas_data(data: UnityPy.classes.Sprite):
    atlas = None
    if data.m_SpriteAtlas:
        atlas = data.m_SpriteAtlas.read()
    elif data.m_AtlasTags:
        for obj in data.assets_file.objects.values():
            if obj.type != ClassIDType.SpriteAtlas:
                continue
            atlas = obj.read()
            if atlas.m_Name == data.m_AtlasTags[0]:
                break
            atlas = None

    if not atlas:
        return data.m_RD

    sprite_atlas_data = next(
        (value for key, value in atlas.m_RenderDataMap if key == data.m_RenderDataKey),
        None,
    )
    if sprite_atlas_data is None:
        logger.warning(
            "Sprite atlas render data missing for %s, falling back to embedded render data",
            data.m_Name or data.path_id,
        )
        return data.m_RD
    return sprite_atlas_data


def render_sprite_with_fallback(data: UnityPy.classes.Sprite) -> Image.Image:
    """Render a sprite, falling back to its texture rect when tight mesh export fails."""
    try:
        return data.image
    except (ValueError, RuntimeError, StopIteration) as exc:
        if not should_fallback_sprite_render(exc):
            raise

    sprite_atlas_data = get_sprite_atlas_data(data)
    texture_rect = sprite_atlas_data.textureRect
    if texture_rect.width <= 0 or texture_rect.height <= 0:
        raise ValueError(
            f"Invalid sprite texture rect {texture_rect} for {data.m_Name or data.path_id}"
        )

    image = get_image(
        data,
        sprite_atlas_data.texture,
        sprite_atlas_data.alphaTexture,
    ).crop(
        (
            texture_rect.x,
            texture_rect.y,
            texture_rect.x + texture_rect.width,
            texture_rect.y + texture_rect.height,
        )
    )

    settings = SpriteSettings(sprite_atlas_data.settingsRaw)
    if settings.packed == 1:
        rotation = settings.packingRotation
        if rotation == SpritePackingRotation.kSPRFlipHorizontal:
            image = image.transpose(Image.FLIP_LEFT_RIGHT)
        elif rotation == SpritePackingRotation.kSPRFlipVertical:
            image = image.transpose(Image.FLIP_TOP_BOTTOM)
        elif rotation == SpritePackingRotation.kSPRRotate180:
            image = image.transpose(Image.ROTATE_180)
        elif rotation == SpritePackingRotation.kSPRRotate90:
            image = image.transpose(Image.ROTATE_270)

    logger.warning(
        "Falling back to texture rect export for sprite %s",
        data.m_Name or data.path_id,
    )
    return image.transpose(Image.FLIP_TOP_BOTTOM)


def render_image_asset(
    data: UnityPy.classes.Texture2D | UnityPy.classes.Sprite,
) -> Image.Image:
    if isinstance(data, UnityPy.classes.Sprite):
        return render_sprite_with_fallback(data)
    return data.image


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
