"""This module contains functions to download, deobfuscate, and extract asset bundles."""

import asyncio
import orjson as json
import logging
import re
from io import BytesIO
from typing import Dict, List, Tuple

import aiohttp
import UnityPy
import UnityPy.classes
import UnityPy.config
from anyio import Path, open_file

from constants import UNITY_FS_CONTAINER_BASE, UNITY_FS_BUILT_IN_CONTAINER_BASE
from helpers import deobfuscate
from utils.acb import extract_acb
from utils.usm import extract_usm

logger = logging.getLogger("live2d")


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
                        # save as png
                        logger.debug(
                            "Saving texture %s to %s",
                            unityfs_path,
                            save_path.with_suffix(".png"),
                        )
                        data.image.save(save_path.with_suffix(".png"))
                        exported_files.append(save_path.with_suffix(".png"))

                        # save as webp
                        logger.debug(
                            "Saving texture %s to %s",
                            unityfs_path,
                            save_path.with_suffix(".png"),
                        )
                        data.image.save(save_path.with_suffix(".webp"))
                        exported_files.append(save_path.with_suffix(".webp"))
                    else:
                        raise TypeError(
                            f"Expected Texture2D or Sprite, got {type(data)} for {unityfs_path}"
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
                case _:
                    logger.warning(
                        "Unknowen type %s of %s, extracting typetree",
                        unityfs_obj.type.name,
                        unityfs_path,
                    )
                    tree = unityfs_obj.read_typetree()
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
                for extracted_audio_file in extracted_audio_files:
                    extracted_audio_file_path = Path(extracted_audio_file)

                    if not await extracted_audio_file_path.exists():
                        logger.warning(
                            "%s not found in %s", extracted_audio_file_path, save_dir
                        )
                        continue

                    if (await extracted_audio_file_path.stat()).st_size == 0:
                        logger.warning(
                            "%s is empty, skipping", extracted_audio_file_path
                        )
                        continue

                    # hca -> wav
                    hca2wav_process = await asyncio.create_subprocess_exec(
                        config.EXTERNAL_VGMSTREAM_CLI,
                        "-o",
                        extracted_audio_file_path.with_suffix(".wav").as_posix(),
                        extracted_audio_file_path.as_posix(),
                    )
                    await hca2wav_process.wait()
                    if hca2wav_process.returncode != 0:
                        logger.warning(
                            "Failed to convert %s to wav", extracted_audio_file_path
                        )
                    else:
                        # remove the hca file
                        await extracted_audio_file_path.unlink()
                        logger.debug(
                            "Converted %s to wav and removed the original file",
                            extracted_audio_file_path,
                        )
                        exported_files.append(
                            extracted_audio_file_path.with_suffix(".wav")
                        )

                    # wav -> mp3
                    wav2mp3_process = await asyncio.create_subprocess_exec(
                        "ffmpeg",
                        "-loglevel",
                        "panic",
                        "-y",
                        "-i",
                        extracted_audio_file_path.with_suffix(".wav").as_posix(),
                        extracted_audio_file_path.with_suffix(".mp3").as_posix(),
                    )
                    await wav2mp3_process.wait()
                    if wav2mp3_process.returncode != 0:
                        logger.warning(
                            "Failed to convert %s to mp3",
                            extracted_audio_file_path.with_suffix(".wav"),
                        )
                    else:
                        logger.debug(
                            "Converted %s to mp3",
                            extracted_audio_file_path.with_suffix(".wav"),
                        )
                        exported_files.append(
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
                            extracted_audio_file_path.with_suffix(".wav").as_posix(),
                            extracted_audio_file_path.with_suffix(".flac").as_posix(),
                        )
                        await wav2flac_process.wait()
                        if wav2flac_process.returncode != 0:
                            logger.warning(
                                "Failed to convert %s to flac",
                                extracted_audio_file_path.with_suffix(".wav"),
                            )
                        else:
                            logger.debug(
                                "Converted %s to flac",
                                extracted_audio_file_path.with_suffix(".wav"),
                            )
                            exported_files.append(
                                extracted_audio_file_path.with_suffix(".flac")
                            )
            else:
                logger.warning("%s not found in %s", acb_output_path, save_dir)

    # Post-process movie bundles
    for save_dir, movie_bundles in post_process_movie_bundles:
        if len(movie_bundles) == 1:
            # the movie bundle consists of a single file
            movie_bundle = movie_bundles[0]
            usm_output_name = movie_bundle["usmFileName"].removesuffix(".bytes")
            usm_output_path = (save_dir / usm_output_name).with_suffix(".usm")
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
                    async with await open_file(usm_split_path, "rb") as infile:
                        await outfile.write(await infile.read())
                    exported_files.remove(usm_split_path)
                    await usm_split_path.unlink()

                logger.debug("Merged %s to %s", usm_split_filenames, usm_output_name)

        if await usm_output_path.exists():
            async with await open_file(usm_output_path, "rb") as f:
                usm_data = await f.read()
                extracted_movie_files = extract_usm(
                    BytesIO(usm_data), save_dir.as_posix()
                )

            # remove the usm file
            await usm_output_path.unlink()
            try:
                exported_files.remove(usm_output_path)
            except ValueError:
                # remove with lowercase filename
                usm_output_path_lower = usm_output_path.with_name(
                    usm_output_path.name.lower()
                )
                exported_files.remove(usm_output_path_lower)
            logger.debug("Removed %s", usm_output_path)

            if len(extracted_movie_files) == 1:
                # video only
                # call ffmpeg to convert the video to mp4
                video_output_path = Path(extracted_movie_files[0]).with_suffix(".mp4")
                ffmpeg_process = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-loglevel",
                    "panic",
                    "-y",
                    "-i",
                    extracted_movie_files[0],
                    "-tune",
                    "animation",
                    video_output_path.as_posix(),
                )
                await ffmpeg_process.wait()
                if ffmpeg_process.returncode != 0:
                    logger.warning(
                        "Failed to convert %s to mp4", extracted_movie_files[0]
                    )
                else:
                    logger.debug(
                        "Converted %s to mp4 and removed the original file",
                        extracted_movie_files[0],
                    )
                    exported_files.append(video_output_path)
                    await Path(extracted_movie_files[0]).unlink()

            elif len(extracted_movie_files) == 2:
                # video + audio
                # call ffmpeg to merge them to one mp4 file
                video_output_path = Path(extracted_movie_files[0]).with_suffix(".mp4")
                ffmpeg_process = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-loglevel",
                    "panic",
                    "-y",
                    "-i",
                    extracted_movie_files[0],
                    "-i",
                    extracted_movie_files[1],
                    "-tune",
                    "animation",
                    video_output_path.as_posix(),
                )
                await ffmpeg_process.wait()
                if ffmpeg_process.returncode != 0:
                    logger.warning(
                        "Failed to convert %s and %s to mp4",
                        extracted_movie_files[0],
                        extracted_movie_files[1],
                    )
                else:
                    logger.debug(
                        "Converted %s and %s to mp4 and removed the original files",
                        extracted_movie_files[0],
                        extracted_movie_files[1],
                    )
                    exported_files.append(video_output_path)
                    await Path(extracted_movie_files[0]).unlink()
                    await Path(extracted_movie_files[1]).unlink()
            else:
                logger.warning(
                    "Unexpected extracted movie files count: %d, elements: %s",
                    len(extracted_movie_files),
                    extracted_movie_files,
                )

    # Final cleanup of exported files, all files ending with
    # ".bytes", ".acb", ".usm" will be removed
    for file in exported_files[:]:  # Iterate over a copy of the list
        if file.suffix in [".bytes", ".acb", ".usm"]:
            await file.unlink()
            logger.debug("Removed %s in cleanup stage", file)
            exported_files.remove(file)

    return exported_files
