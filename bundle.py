"""This module contains functions to download, deobfuscate, and extract asset bundles."""

import asyncio
import orjson as json
import logging
import re
import sys
from io import BytesIO
from typing import Dict, List, Tuple

import aiohttp
import UnityPy
import UnityPy.classes
import UnityPy.config
from PIL import Image
from UnityPy.enums import ClassIDType, SpritePackingRotation
from UnityPy.export.SpriteHelper import SpriteSettings, get_image
from anyio import Path, open_file

from constants import UNITY_FS_CONTAINER_BASE, UNITY_FS_BUILT_IN_CONTAINER_BASE
from helpers import deobfuscate
from utils.acb import extract_acb
from utils.playable import extract_playable

logger = logging.getLogger("live2d")

_ffmpeg_video_encoder_cache: tuple[str | None, list[str]] | None = None


def _get_audio_transcode_concurrency(config) -> int:
    concurrency = getattr(
        config,
        "MAX_CONCURRENCY_AUDIO_TRANSCODES",
        getattr(config, "MAX_CONCURRENCY", 1),
    )
    try:
        return max(1, int(concurrency))
    except (TypeError, ValueError):
        return 1


async def _get_ffmpeg_video_encoder() -> tuple[str | None, list[str]]:
    """Detect a usable hardware H.264 encoder for the current platform."""
    global _ffmpeg_video_encoder_cache

    if _ffmpeg_video_encoder_cache is not None:
        return _ffmpeg_video_encoder_cache

    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-encoders",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        _ffmpeg_video_encoder_cache = (None, [])
        return _ffmpeg_video_encoder_cache

    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.warning(
            "Failed to inspect ffmpeg encoders, falling back to software encoding: %s",
            stderr.decode(errors="ignore").strip(),
        )
        _ffmpeg_video_encoder_cache = (None, [])
        return _ffmpeg_video_encoder_cache

    available_encoders = stdout.decode(errors="ignore")
    candidates: list[tuple[str, list[str]]] = []

    if sys.platform == "darwin":
        candidates.append(("h264_videotoolbox", ["-c:v", "h264_videotoolbox"]))
    elif sys.platform.startswith("linux"):
        if await Path("/dev/nvidia0").exists() or await Path("/dev/nvidiactl").exists():
            candidates.append(("h264_nvenc", ["-c:v", "h264_nvenc"]))
        if await Path("/dev/dri/renderD128").exists():
            candidates.append(
                (
                    "h264_vaapi",
                    [
                        "-vaapi_device",
                        "/dev/dri/renderD128",
                        "-vf",
                        "format=nv12,hwupload",
                        "-c:v",
                        "h264_vaapi",
                    ],
                )
            )
    elif sys.platform == "win32":
        candidates.extend(
            [
                ("h264_nvenc", ["-c:v", "h264_nvenc"]),
                ("h264_amf", ["-c:v", "h264_amf"]),
            ]
        )

    for encoder_name, encoder_args in candidates:
        if encoder_name in available_encoders:
            logger.info("Using ffmpeg hardware video encoder: %s", encoder_name)
            _ffmpeg_video_encoder_cache = (encoder_name, encoder_args)
            return _ffmpeg_video_encoder_cache

    logger.info("No usable ffmpeg hardware video encoder detected, using software encoding")
    _ffmpeg_video_encoder_cache = (None, [])
    return _ffmpeg_video_encoder_cache


def _disable_ffmpeg_video_encoder() -> None:
    global _ffmpeg_video_encoder_cache
    _ffmpeg_video_encoder_cache = (None, [])


async def _run_ffmpeg_usm_to_mp4(
    input_path: Path,
    output_path: Path,
) -> tuple[asyncio.subprocess.Process, str | None]:
    encoder_name, encoder_args = await _get_ffmpeg_video_encoder()
    command = [
        "ffmpeg",
        "-loglevel",
        "panic",
        "-y",
        "-i",
        input_path.as_posix(),
    ]

    if encoder_args:
        command.extend(encoder_args)
    else:
        command.extend(["-tune", "animation"])

    command.append(output_path.as_posix())
    process = await asyncio.create_subprocess_exec(*command)
    return process, encoder_name


async def _process_extracted_audio_file(
    extracted_audio_file: str,
    save_dir: Path,
    config,
    semaphore: asyncio.Semaphore,
) -> list[Path]:
    async with semaphore:
        extracted_audio_file_path = Path(extracted_audio_file)
        exported_audio_files: list[Path] = []

        try:
            if not await extracted_audio_file_path.exists():
                logger.warning(
                    "%s not found in %s", extracted_audio_file_path, save_dir
                )
                return exported_audio_files

            if (await extracted_audio_file_path.stat()).st_size == 0:
                logger.warning("%s is empty, skipping", extracted_audio_file_path)
                return exported_audio_files

            wav_path = extracted_audio_file_path.with_suffix(".wav")

            # hca -> wav
            hca2wav_process = await asyncio.create_subprocess_exec(
                config.EXTERNAL_VGMSTREAM_CLI,
                "-o",
                wav_path.as_posix(),
                extracted_audio_file_path.as_posix(),
            )
            await hca2wav_process.wait()
            if hca2wav_process.returncode != 0:
                logger.warning("Failed to convert %s to wav", extracted_audio_file_path)
                return exported_audio_files

            await extracted_audio_file_path.unlink()
            logger.debug(
                "Converted %s to wav and removed the original file",
                extracted_audio_file_path,
            )
            exported_audio_files.append(wav_path)

            # wav -> mp3
            wav2mp3_process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-loglevel",
                "panic",
                "-y",
                "-i",
                wav_path.as_posix(),
                extracted_audio_file_path.with_suffix(".mp3").as_posix(),
            )
            await wav2mp3_process.wait()
            if wav2mp3_process.returncode != 0:
                logger.warning("Failed to convert %s to mp3", wav_path)
            else:
                logger.debug("Converted %s to mp3", wav_path)
                exported_audio_files.append(
                    extracted_audio_file_path.with_suffix(".mp3")
                )

            # wav -> flac
            # only for music files
            if "music" in save_dir.parts:
                wav2flac_process = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-loglevel",
                    "panic",
                    "-y",
                    "-i",
                    wav_path.as_posix(),
                    extracted_audio_file_path.with_suffix(".flac").as_posix(),
                )
                await wav2flac_process.wait()
                if wav2flac_process.returncode != 0:
                    logger.warning("Failed to convert %s to flac", wav_path)
                else:
                    logger.debug("Converted %s to flac", wav_path)
                    exported_audio_files.append(
                        extracted_audio_file_path.with_suffix(".flac")
                    )
        except Exception as exc:
            logger.exception(
                "Failed processing extracted audio %s: %s",
                extracted_audio_file_path,
                exc,
            )

        return exported_audio_files


def _should_fallback_sprite_render(exc: Exception) -> bool:
    if isinstance(exc, ValueError):
        return "Coordinate 'lower' is less than 'upper'" in str(exc)
    if isinstance(exc, StopIteration):
        return True
    return isinstance(exc, RuntimeError) and isinstance(exc.__cause__, StopIteration)


def _get_sprite_atlas_data(data: UnityPy.classes.Sprite):
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


def _render_sprite_with_fallback(data: UnityPy.classes.Sprite) -> Image.Image:
    """Render a sprite, falling back to its texture rect when tight mesh export fails."""
    try:
        return data.image
    except (ValueError, RuntimeError, StopIteration) as exc:
        if not _should_fallback_sprite_render(exc):
            raise

    sprite_atlas_data = _get_sprite_atlas_data(data)

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


def _render_image_asset(
    data: UnityPy.classes.Texture2D | UnityPy.classes.Sprite,
) -> Image.Image:
    if isinstance(data, UnityPy.classes.Sprite):
        return _render_sprite_with_fallback(data)
    return data.image


async def download_deobfuscate_bundle(
    url: str, bundle_save_path: Path, headers: Dict[str, str]
) -> Tuple[str, Dict]:
    """Download and deobfuscate the bundle."""
    # Download the bundle
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                # Read the response data
                data = await response.read()
                # Deobfuscate the data
                deobfuscated_data = await deobfuscate(data)
                # Save the deobfuscated data to the file
                async with await open_file(bundle_save_path, "wb") as f:
                    await f.write(deobfuscated_data)
            else:
                logger.debug(
                    "Failed to download %s: %s, response: %s",
                    url,
                    response.status,
                    await response.text(),
                )
                raise aiohttp.ClientError(f"Failed to download {url}")


async def extract_asset_bundle(
    bundle_save_path: Path,
    bundle: Dict[str, str],
    extracted_save_path: Path,
    unity_version: str = None,
    config=None,
) -> List[Path]:
    """Extract the asset bundle to the specified directory.

    Args:
        bundle_save_path (Path): _description_
        bundle (Dict[str, str]): _description_
        extracted_save_path (Path): _description_
        unity_version (str, optional): _description_. Defaults to None.
        config (_type_, optional): _description_. Defaults to None.

    Raises:
        ValueError: _description_
        TypeError: _description_
        TypeError: _description_
        TypeError: _description_
        RuntimeError: _description_

    Returns:
        List[Path]: _description_
    """
    UnityPy.config.FALLBACK_UNITY_VERSION = unity_version

    # Load the bundle
    _unity_file = UnityPy.load(bundle_save_path.as_posix())
    # Check if the bundle is valid
    if not _unity_file:
        raise ValueError(f"Failed to load {bundle_save_path}")

    logger.debug("Loaded bundle %s from %s", bundle.get("bundleName"), bundle_save_path)

    exported_files: List[Path] = []
    post_process_acb_files: List[Tuple[Path, List[Dict]]] = []
    post_process_movie_bundles: List[Tuple[Path, List[Dict]]] = []
    for unityfs_path, unityfs_obj in _unity_file.container.items():
        try:
            relpath = Path(unityfs_path).relative_to(UNITY_FS_CONTAINER_BASE)
            save_path = extracted_save_path / relpath.relative_to(*relpath.parts[:1])
        except ValueError:
            relpath = Path(unityfs_path).relative_to(UNITY_FS_BUILT_IN_CONTAINER_BASE)
            save_path = extracted_save_path / relpath
        except Exception as e:
            logger.exception("Failed to get relative path for %s", unityfs_path)
            raise e
        # trim whitespace from the path
        save_path = save_path.with_name(save_path.name.strip())
        save_dir = save_path.parent
        # Create the directory if it doesn't exist
        await save_dir.mkdir(parents=True, exist_ok=True)

        try:
            match unityfs_obj.type.name:
                case "MonoBehaviour":
                    tree = None
                    try:
                        if unityfs_obj.serialized_type.node:
                            tree = unityfs_obj.read_typetree()
                    except AttributeError:
                        tree = unityfs_obj.read_typetree()
                    logger.debug(
                        "Saving MonoBehaviour %s to %s", unityfs_path, save_path
                    )
    
                    if unityfs_path.endswith(".playable"):
                        tree = extract_playable(_unity_file, unityfs_path)

                    # Save the typetree to a json file
                    async with await open_file(save_path, "wb") as f:
                        await f.write(json.dumps(tree, option=json.OPT_INDENT_2))
                    exported_files.append(save_path)

                    if "acbFiles" in tree:
                        post_process_acb_files.append((save_dir, tree["acbFiles"]))
                        logger.debug(
                            "Found acbFiles in %s: %s", unityfs_path, tree["acbFiles"]
                        )
                    elif "movieBundleDatas" in tree:
                        post_process_movie_bundles.append(
                            (save_dir, tree["movieBundleDatas"])
                        )
                        logger.debug(
                            "Found movieBundleDatas in %s: %s",
                            unityfs_path,
                            tree["movieBundleDatas"],
                        )
                case "TextAsset":
                    data = unityfs_obj.read()
                    if isinstance(data, UnityPy.classes.TextAsset):
                        if save_path.suffix == ".bytes":
                            save_path = save_path.with_suffix("")
                        async with await open_file(save_path, "wb") as f:
                            await f.write(
                                data.m_Script.encode("utf-8", "surrogateescape")
                            )
                        exported_files.append(save_path)
                    else:
                        raise TypeError(
                            f"Expected TextAsset, got {type(data)} for {unityfs_path}"
                        )
                case "Texture2D" | "Sprite":
                    data = unityfs_obj.read()
                    if isinstance(data, UnityPy.classes.Texture2D) or isinstance(
                        data, UnityPy.classes.Sprite
                    ):
                        image = _render_image_asset(data)
                        # save as png
                        logger.debug(
                            "Saving texture %s to %s",
                            unityfs_path,
                            save_path.with_suffix(".png"),
                        )
                        image.save(save_path.with_suffix(".png"))
                        exported_files.append(save_path.with_suffix(".png"))

                        # save as webp
                        logger.debug(
                            "Saving texture %s to %s",
                            unityfs_path,
                            save_path.with_suffix(".png"),
                        )
                        image.save(save_path.with_suffix(".webp"))
                        exported_files.append(save_path.with_suffix(".webp"))
                    else:
                        raise TypeError(
                            f"Expected Texture2D or Sprite, got {type(data)} for {unityfs_path}"
                        )
                case "Texture2DArray":
                    data = unityfs_obj.read()
                    if isinstance(data, UnityPy.classes.Texture2DArray):
                        for i, image in enumerate(data.images):
                            _save_path = save_path.with_name(
                                save_path.stem + f"_{i}"
                            ).with_suffix(".png")
                            logger.debug(
                                "Saving texture %s to %s",
                                unityfs_path,
                                _save_path,
                            )
                            image.save(_save_path)
                            exported_files.append(_save_path)
                    else:
                        raise TypeError(
                            f"Expected Texture2DArray, got {type(data)} for {unityfs_path}"
                        )
                case "AudioClip":
                    data = unityfs_obj.read()
                    if isinstance(data, UnityPy.classes.AudioClip):
                        for filename, sample_data in data.samples.items():
                            logger.debug(
                                "Saving audio clip %s to %s",
                                filename,
                                save_path.with_name(filename),
                            )
                            async with await open_file(
                                save_path.with_name(filename), "wb"
                            ) as f:
                                await f.write(sample_data)
                            exported_files.append(save_path.with_name(filename))
                    else:
                        raise TypeError(
                            f"Expected AudioClip, got {type(data)} for {unityfs_path}"
                        )
                case "Mesh":
                    # Mesh data is not supported yet
                    logger.warning(
                        "Mesh data is not supported yet, skipping %s", unityfs_path
                    )
                    continue
                case "Cubemap":
                    # Cubemap data is not supported yet
                    logger.warning(
                        "Cubemap data is not supported yet, skipping %s", unityfs_path
                    )
                    continue
                case _:
                    logger.warning(
                        "Unknowen type %s of %s, extracting typetree",
                        unityfs_obj.type.name,
                        unityfs_path,
                    )
                    tree = unityfs_obj.read_typetree()
                    try:
                        json.dumps(tree)
                    except (ValueError, TypeError):
                        logger.warning(
                            "Failed to serialize %s, skipping", tree
                        )
                    async with await open_file(save_path, "wb") as f:
                        await f.write(json.dumps(tree, option=json.OPT_INDENT_2))
                    exported_files.append(save_path)
        except (ValueError, TypeError, AttributeError, OSError) as e:
            logger.exception("Failed to extract %s: %s", unityfs_path, e)
            raise e

    logger.debug(
        "Extracted %d files from %s, list: %s",
        len(exported_files),
        bundle_save_path,
        exported_files,
    )

    # Post-process acb files
    audio_transcode_semaphore = asyncio.Semaphore(
        _get_audio_transcode_concurrency(config)
    )
    for save_dir, acb_files in post_process_acb_files:
        for acb_file in acb_files:
            acb_cue_sheet_name: str = acb_file["cueSheetName"]
            acb_output_path = (save_dir / acb_cue_sheet_name).with_suffix(".acb")

            if acb_file["formatType"] == 0 or acb_file["spilitFileNum"] == 0:
                # single file
                acb_textasset_filename: str = acb_file["assetBundleFileName"]

                logger.debug("Try to find %s in %s", acb_textasset_filename, save_dir)
                acb_textasset_path = (save_dir / acb_textasset_filename.removesuffix(
                    ".bytes"
                )).with_suffix(".acb")
                assert (
                    acb_textasset_path == acb_output_path
                ), f"Path mismatch: {acb_textasset_path} != {acb_output_path}"
                if not await acb_textasset_path.exists():
                    logger.error("%s not found in %s", acb_textasset_filename, save_dir)
            else:
                # split files
                patter = re.compile(r"{(\d)\:D(\d)}")
                acb_textasset_filenames = [
                    patter.sub(r"{\1:0\2d}", acb_file["assetBundleFileName"])
                    .format(i)
                    .lower()
                    for i in range(1, acb_file["spilitFileNum"] + 1)
                ]

                # find and merge the files
                acb_textasset_paths = [
                    save_dir / acb_textasset_filename.removesuffix(".bytes")
                    for acb_textasset_filename in acb_textasset_filenames
                ]
                if all([await path.exists() for path in acb_textasset_paths]):
                    # merge the files
                    async with await open_file(acb_output_path, "wb") as outfile:
                        for acb_textasset_path in acb_textasset_paths:
                            async with await open_file(
                                acb_textasset_path, "rb"
                            ) as infile:
                                await outfile.write(await infile.read())
                            exported_files.remove(acb_textasset_path)
                            await acb_textasset_path.unlink()

                    logger.debug(
                        "Merged %s to %s.acb",
                        acb_textasset_filenames,
                        acb_cue_sheet_name,
                    )
                else:
                    logger.error(
                        "%s not found in %s", acb_textasset_filenames, save_dir
                    )

            # extract audio files from the acb file
            if await acb_output_path.exists():
                # acb -> hca
                async with await open_file(acb_output_path, "rb") as f:
                    acb_data = await f.read()
                    extracted_audio_files = extract_acb(
                        BytesIO(acb_data),
                        save_dir.as_posix(),
                        acb_output_path.as_posix(),
                    )

                # remove the acb file
                await acb_output_path.unlink()
                logger.debug("Removed %s", acb_output_path)
                exported_files.remove(acb_output_path)

                if not config.EXTERNAL_VGMSTREAM_CLI:
                    raise RuntimeError(
                        "External vgmstream cli not found, please set the path in config"
                    )
                audio_tasks = [
                    asyncio.create_task(
                        _process_extracted_audio_file(
                            extracted_audio_file,
                            save_dir,
                            config,
                            audio_transcode_semaphore,
                        )
                    )
                    for extracted_audio_file in extracted_audio_files
                ]
                audio_results = await asyncio.gather(*audio_tasks)
                for audio_files in audio_results:
                    exported_files.extend(audio_files)
            else:
                logger.warning("%s not found in %s", acb_output_path, save_dir)

    # Post-process movie bundles
    for save_dir, movie_bundles in post_process_movie_bundles:
        if len(movie_bundles) == 1:
            # the movie bundle consists of a single file
            movie_bundle = movie_bundles[0]
            usm_output_name = movie_bundle["usmFileName"].removesuffix(".bytes")
            usm_output_path = (save_dir / usm_output_name).with_suffix(".usm")
            
            if not await usm_output_path.exists():
                # maybe case-sensitive filesystem issue
                usm_output_path_lower = usm_output_path.with_name(
                    usm_output_path.name.lower()
                )
                if await usm_output_path_lower.exists():
                    usm_output_path = usm_output_path_lower
                    logger.debug(
                        "Found %s instead of %s", usm_output_path, usm_output_name
                    )
                else:
                    raise FileNotFoundError(
                        f"{usm_output_path} not found in {save_dir}"
                    )
        elif len(movie_bundles) > 1:
            # the movie bundle consists of multiple files
            pattern = re.compile(r"-\d{3}.usm.bytes")
            usm_output_name = pattern.sub(".usm", movie_bundles[0]["usmFileName"])
            usm_output_path = save_dir / usm_output_name
            usm_split_filenames: List[str] = [x["usmFileName"] for x in movie_bundles]
            usm_split_paths = [
                save_dir / usm_split_filename.removesuffix(".bytes")
                for usm_split_filename in usm_split_filenames
            ]

            # merge split usm files to one
            async with await open_file(usm_output_path, "wb") as outfile:
                for usm_split_path in usm_split_paths:
                    if not await usm_split_path.exists():
                        # maybe case-sensitive filesystem issue
                        usm_split_path_lower = usm_split_path.with_name(
                            usm_split_path.name.lower()
                        )
                        if await usm_split_path_lower.exists():
                            usm_split_path = usm_split_path_lower
                            logger.debug(
                                "Found %s instead of %s", usm_split_path, usm_split_paths
                            )
                        else:
                            raise FileNotFoundError(
                                f"{usm_split_path} not found in {save_dir}"
                            )
                    async with await open_file(usm_split_path, "rb") as infile:
                        await outfile.write(await infile.read())
                    try:
                        exported_files.remove(usm_split_path)
                        await usm_split_path.unlink()
                    except ValueError:
                        # maybe case-sensitive issue
                        usm_split_path_lower = usm_split_path.with_name(
                            usm_split_path.name.lower()
                        )
                        exported_files.remove(usm_split_path_lower)
                        await usm_split_path_lower.unlink()

                logger.debug("Merged %s to %s", usm_split_filenames, usm_output_name)
                exported_files.append(usm_output_path)
        else:
            logger.warning("Empty movieBundleDatas in %s", save_dir)
            continue

        if await usm_output_path.exists():
            # ffmpeg already supports usm; convert directly to mp4.
            video_output_path = usm_output_path.with_suffix(".mp4")
            ffmpeg_process, encoder_name = await _run_ffmpeg_usm_to_mp4(
                usm_output_path, video_output_path
            )
            await ffmpeg_process.wait()

            if ffmpeg_process.returncode != 0 and encoder_name:
                logger.warning(
                    "Failed to convert %s to mp4 with %s, falling back to software encoding",
                    usm_output_path,
                    encoder_name,
                )
                _disable_ffmpeg_video_encoder()
                ffmpeg_process, _ = await _run_ffmpeg_usm_to_mp4(
                    usm_output_path, video_output_path
                )
                await ffmpeg_process.wait()

            if ffmpeg_process.returncode != 0:
                logger.warning("Failed to convert %s to mp4", usm_output_path)
            else:
                logger.debug("Converted %s to mp4", usm_output_path)
                exported_files.append(video_output_path)

            await usm_output_path.unlink()
            logger.debug("Removed %s", usm_output_path)
            try:
                exported_files.remove(usm_output_path)
            except ValueError:
                # maybe case-sensitive issue
                usm_output_path_lower = usm_output_path.with_name(
                    usm_output_path.name.lower()
                )
                if usm_output_path_lower in exported_files:
                    exported_files.remove(usm_output_path_lower)

    # Final cleanup of exported files, all files ending with
    # ".bytes", ".acb", ".usm" will be removed
    for file in exported_files[:]:  # Iterate over a copy of the list
        if file.suffix in [".bytes", ".acb", ".usm"]:
            await file.unlink()
            logger.debug("Removed %s in cleanup stage", file)
            exported_files.remove(file)

    return exported_files
