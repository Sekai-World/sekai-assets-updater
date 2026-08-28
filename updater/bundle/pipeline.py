"""This module contains functions to download, deobfuscate, and extract asset bundles."""

import asyncio
import logging
import os
import re
import shutil
import tempfile
from functools import partial
from pathlib import Path as StdPath
from typing import Dict, List

import cridecoder as cridecoder
from anyio import Path

from updater.bundle.acb_cache import (
    extract_acb_from_cached_bundles as _extract_acb_from_cached_bundles_sync,
)
from updater.bundle.extraction import extract_unity_objects
from updater.bundle.paths import (
    build_unityfs_save_path as _build_unityfs_save_path,  # noqa: F401
)
from updater.bundle.paths import (
    discard_exported_file as _discard_exported_file_sync,
)
from updater.bundle.paths import (
    replace_suffix_secure as _replace_suffix_secure,
)
from updater.bundle.paths import (
    resolve_existing_path as _resolve_existing_path_sync,
)
from updater.bundle.paths import (
    resolve_existing_usm_path as _resolve_existing_usm_path_sync,
)
from updater.bundle.paths import (
    resolve_generated_child_path as _resolve_generated_child_path,
)
from updater.bundle.paths import (
    resolve_local_audio_outputs as _resolve_local_audio_outputs_sync,
)
from updater.bundle.paths import (
    resolve_shared_audio_outputs as _resolve_shared_audio_outputs_sync,
)
from updater.bundle.paths import (
    stream_files as _stream_files,
)
from updater.media.acb import decode_acb_bytes, extract_acb
from updater.media.audio import (
    _process_extracted_audio_file,
)
from updater.media.images import (
    _get_texture_output_formats,
    _get_texture_png_compression,
    _get_texture_webp_method,
)
from updater.media.images import (
    save_image_formats as _save_image_formats,  # noqa: F401
)
from updater.media.video import (
    DEFAULT_USM_IN_MEMORY_MAX_BYTES,
    _demux_usm_sources_in_memory,
    _get_usm_in_memory_limit,
    _process_video_jobs,
)
from updater.modes import (  # noqa: F401  (re-exported until pipeline.py is dissolved)
    is_chart_score_bundle,
    is_live2d_bundle,
)
from updater.runtime import (
    runtime as _bundle_runtime,
)
from updater.security import (
    atomic_write_bytes,
    atomic_write_stream,
    secure_existing_output,
    validate_contained_file,
    validate_output_target,
)
from updater.unity_rs_adapter import load_bundle as _load_unity_bundle

logger = logging.getLogger("live2d")

_get_shared_audio_file_semaphore = _bundle_runtime.audio_file_semaphore
_get_shared_extract_process_pool = _bundle_runtime.extract_process_pool


def _discard_exported_file(exported_files: list[Path], file_path: Path) -> None:
    try:
        exported_files.remove(file_path)
        return
    except ValueError:
        pass

    file_path_lower = file_path.with_name(file_path.name.lower())
    try:
        exported_files.remove(file_path_lower)
    except ValueError:
        logger.debug("%s not tracked in exported_files, skip removal", file_path)


async def _resolve_existing_path(
    expected_path: Path,
    save_dir: Path,
    expected_suffix: str | None = None,
) -> Path:
    if await expected_path.exists():
        return expected_path

    expected_name_lower = expected_path.name.lower()
    expected_path_lower = expected_path.with_name(expected_name_lower)
    if await expected_path_lower.exists():
        logger.debug("Found %s instead of %s", expected_path_lower, expected_path.name)
        return expected_path_lower

    candidate_paths = [
        path
        async for path in save_dir.iterdir()
        if path.name.lower() == expected_name_lower
        and (expected_suffix is None or path.suffix.lower() == expected_suffix.lower())
    ]
    if len(candidate_paths) == 1:
        logger.debug(
            "Found %s instead of %s via case-insensitive lookup",
            candidate_paths[0],
            expected_path.name,
        )
        return candidate_paths[0]

    raise FileNotFoundError(f"{expected_path} not found in {save_dir}")


async def _resolve_existing_usm_path(expected_path: Path, save_dir: Path) -> Path:
    try:
        return await _resolve_existing_path(expected_path, save_dir, ".usm")
    except FileNotFoundError:
        pass

    candidate_paths = [path async for path in save_dir.iterdir() if path.suffix.lower() == ".usm"]
    if len(candidate_paths) == 1:
        logger.warning(
            "Expected %s in %s, falling back to discovered usm %s",
            expected_path.name,
            save_dir,
            candidate_paths[0].name,
        )
        return candidate_paths[0]

    raise FileNotFoundError(f"{expected_path} not found in {save_dir}")


def _extract_bundle_files_sync(
    bundle_save_path: str,
    bundle: Dict[str, str],
    extracted_save_path: str,
    unity_version: str | None,
    texture_output_formats: tuple[str, ...],
    bundle_cache_root: str | None = None,
    *,
    live2d_bundle: bool = False,
    webp_method: int | None = None,
    png_compression: str | int | None = None,
    usm_in_memory_limit: int | None = None,
) -> tuple[list[str], list[tuple[str, list[str]]], list[str]]:
    from updater.media.images import DEFAULT_PNG_COMPRESSION, DEFAULT_WEBP_METHOD

    if usm_in_memory_limit is None:
        usm_in_memory_limit = DEFAULT_USM_IN_MEMORY_MAX_BYTES
    bundle_path = StdPath(bundle_save_path)
    output_root = StdPath(extracted_save_path)
    unity_file = _load_unity_bundle(bundle_path, unity_version)

    logger.debug("Loaded bundle %s from %s", bundle.get("bundleName"), bundle_save_path)

    exported_files, post_process_acb_files, post_process_movie_bundles = extract_unity_objects(
        unity_file,
        output_root,
        texture_output_formats,
        live2d_bundle=live2d_bundle,
        webp_method=DEFAULT_WEBP_METHOD if webp_method is None else webp_method,
        png_compression=DEFAULT_PNG_COMPRESSION if png_compression is None else png_compression,
    )
    audio_jobs: list[tuple[str, list[str]]] = []
    video_jobs: list[str] = []

    logger.debug(
        "Extracted %d files from %s, list: %s",
        len(exported_files),
        bundle_save_path,
        exported_files,
    )

    for save_dir, acb_files in post_process_acb_files:
        for acb_file in acb_files:
            acb_cue_sheet_name: str = acb_file["cueSheetName"]
            acb_output_path = _replace_suffix_secure(save_dir, acb_cue_sheet_name, ".acb")

            if acb_file["formatType"] == 0 or acb_file["spilitFileNum"] == 0:
                acb_textasset_filename: str = acb_file["assetBundleFileName"]
                logger.debug("Try to find %s in %s", acb_textasset_filename, save_dir)
                expected_acb_textasset_path = _resolve_generated_child_path(
                    save_dir,
                    acb_textasset_filename.removesuffix(".bytes").removesuffix(".acb"),
                    ".acb",
                )
                try:
                    acb_textasset_path = _resolve_existing_path_sync(
                        expected_acb_textasset_path,
                        save_dir,
                        ".acb",
                    )
                except FileNotFoundError:
                    if _extract_acb_from_cached_bundles_sync(
                        bundle_path,
                        acb_textasset_filename,
                        acb_output_path,
                        unity_version,
                        StdPath(bundle_cache_root) if bundle_cache_root else None,
                    ):
                        pass
                    else:
                        shared_audio_paths = _resolve_shared_audio_outputs_sync(
                            output_root,
                            save_dir,
                            acb_cue_sheet_name,
                        )
                        if not shared_audio_paths:
                            local_audio_paths = _resolve_local_audio_outputs_sync(
                                save_dir,
                                acb_cue_sheet_name,
                            )
                            if local_audio_paths:
                                logger.debug(
                                    "ACB textasset %s not found, but audio for %s already extracted locally, skipping ACB processing",
                                    acb_textasset_filename,
                                    acb_cue_sheet_name,
                                )
                                continue
                            logger.warning(
                                "ACB textasset %s not found in %s and no shared/local audio available for %s; "
                                "the ACB data likely resides in a separate bundle. "
                                "Skipping this acbFile entry — the audio will be extracted when that bundle is processed",
                                acb_textasset_filename,
                                save_dir,
                                acb_cue_sheet_name,
                            )
                            continue

                        for shared_audio_path in shared_audio_paths:
                            copied_audio_path = _resolve_generated_child_path(
                                save_dir, shared_audio_path.name
                            )
                            shutil.copy2(shared_audio_path, copied_audio_path)
                            exported_files.append(copied_audio_path)
                        logger.debug(
                            "Copied shared audio outputs for %s from %s to %s",
                            acb_cue_sheet_name,
                            shared_audio_paths[0].parent,
                            save_dir,
                        )
                        continue
                else:
                    if acb_textasset_path != acb_output_path:
                        acb_textasset_path.rename(acb_output_path)
                        _discard_exported_file_sync(exported_files, acb_textasset_path)
                        exported_files.append(acb_output_path)
                        logger.debug(
                            "Renamed %s to %s to match cue sheet name",
                            acb_textasset_path,
                            acb_output_path,
                        )

                if not acb_output_path.exists():
                    shared_audio_paths = _resolve_shared_audio_outputs_sync(
                        output_root,
                        save_dir,
                        acb_cue_sheet_name,
                    )
                    if not shared_audio_paths:
                        local_audio_paths = _resolve_local_audio_outputs_sync(
                            save_dir,
                            acb_cue_sheet_name,
                        )
                        if local_audio_paths:
                            logger.debug(
                                "ACB textasset %s not found, but audio for %s already extracted locally, skipping ACB processing",
                                acb_textasset_filename,
                                acb_cue_sheet_name,
                            )
                            continue
                        logger.warning(
                            "ACB textasset %s not found in %s and no shared/local audio available for %s; "
                            "the ACB data likely resides in a separate bundle. "
                            "Skipping this acbFile entry — the audio will be extracted when that bundle is processed",
                            acb_textasset_filename,
                            save_dir,
                            acb_cue_sheet_name,
                        )
                        continue

                    for shared_audio_path in shared_audio_paths:
                        copied_audio_path = _resolve_generated_child_path(
                            save_dir, shared_audio_path.name
                        )
                        shutil.copy2(shared_audio_path, copied_audio_path)
                        exported_files.append(copied_audio_path)
                    logger.debug(
                        "Copied shared audio outputs for %s from %s to %s",
                        acb_cue_sheet_name,
                        shared_audio_paths[0].parent,
                        save_dir,
                    )
                    continue
            else:
                pattern = re.compile(r"{(\d)\:D(\d)}")
                acb_textasset_filenames = [
                    pattern.sub(r"{\1:0\2d}", acb_file["assetBundleFileName"]).format(i).lower()
                    for i in range(1, acb_file["spilitFileNum"] + 1)
                ]

                try:
                    acb_textasset_paths = [
                        _resolve_existing_path_sync(
                            _resolve_generated_child_path(
                                save_dir, acb_textasset_filename.removesuffix(".bytes")
                            ),
                            save_dir,
                        )
                        for acb_textasset_filename in acb_textasset_filenames
                    ]
                except FileNotFoundError:
                    logger.error("%s not found in %s", acb_textasset_filenames, save_dir)
                    continue

                atomic_write_stream(
                    acb_output_path,
                    _stream_files(acb_textasset_paths),
                )
                for acb_textasset_path in acb_textasset_paths:
                    _discard_exported_file_sync(exported_files, acb_textasset_path)
                    acb_textasset_path.unlink()

                logger.debug("Merged %s to %s.acb", acb_textasset_filenames, acb_cue_sheet_name)

            if acb_output_path.exists():
                acb_asset_name = (
                    acb_file["assetBundleFileName"].removesuffix(".bytes").removesuffix(".acb")
                )
                cue_name = acb_cue_sheet_name if acb_cue_sheet_name != acb_asset_name else None
                promoted_audio = []

                decoded_tracks: list[tuple[str, bytes]] | None
                try:
                    # Fully in-memory decode: the whole ACB (embedded AWB
                    # included) becomes WAV payloads without staging
                    # directories or intermediate files.
                    decoded_tracks = decode_acb_bytes(
                        acb_output_path.read_bytes(),
                        cue_name,
                    )
                except Exception:
                    logger.warning(
                        "In-memory ACB decode failed for %s, falling back to file decoder",
                        acb_output_path,
                        exc_info=True,
                    )
                    decoded_tracks = None

                if decoded_tracks is not None:
                    for track_filename, track_data in decoded_tracks:
                        final_audio = _resolve_generated_child_path(save_dir, track_filename)
                        validate_output_target(save_dir, final_audio)
                        atomic_write_bytes(final_audio, track_data)
                        promoted_audio.append(final_audio.as_posix())
                else:
                    # Path-based fallback keeps external ``.awb`` resolution:
                    # decode next to the ACB, stage only decoder outputs.
                    acb_stage_dir = StdPath(tempfile.mkdtemp(prefix=".acb-", dir=save_dir))
                    try:
                        with acb_output_path.open("rb") as acb_stream:
                            extracted_audio_files = extract_acb(
                                acb_stream,
                                acb_stage_dir.as_posix(),
                                acb_output_path.as_posix(),
                                cue_name,
                            )

                        for extracted_audio_file in extracted_audio_files:
                            produced = secure_existing_output(acb_stage_dir, extracted_audio_file)
                            final_audio = _resolve_generated_child_path(save_dir, produced.name)
                            validate_output_target(save_dir, final_audio)
                            os.replace(produced, final_audio)
                            promoted_audio.append(final_audio.as_posix())
                    finally:
                        shutil.rmtree(acb_stage_dir, ignore_errors=True)

                acb_output_path.unlink()
                logger.debug("Removed %s", acb_output_path)
                _discard_exported_file_sync(exported_files, acb_output_path)
                audio_jobs.append((save_dir.as_posix(), promoted_audio))
            else:
                logger.warning("%s not found in %s", acb_output_path, save_dir)

    for save_dir, movie_bundles in post_process_movie_bundles:
        if len(movie_bundles) == 1:
            movie_bundle = movie_bundles[0]
            usm_output_name = movie_bundle["usmFileName"].removesuffix(".bytes")
            usm_output_path = _replace_suffix_secure(save_dir, usm_output_name, ".usm")
            usm_output_path = _resolve_existing_usm_path_sync(usm_output_path, save_dir)

            m2v_path = _demux_usm_sources_in_memory(
                [usm_output_path],
                usm_output_path,
                save_dir,
                usm_in_memory_limit,
            )
            if m2v_path is not None:
                _discard_exported_file_sync(exported_files, usm_output_path)
                usm_output_path.unlink(missing_ok=True)
                video_jobs.append(m2v_path.as_posix())
                continue
        elif len(movie_bundles) > 1:
            pattern = re.compile(r"-\d{3}.usm.bytes")
            usm_output_name = pattern.sub(".usm", movie_bundles[0]["usmFileName"])
            usm_output_path = _replace_suffix_secure(save_dir, usm_output_name, ".usm")
            usm_split_filenames: list[str] = [x["usmFileName"] for x in movie_bundles]
            usm_split_paths = [
                _resolve_generated_child_path(save_dir, usm_split_filename.removesuffix(".bytes"))
                for usm_split_filename in usm_split_filenames
            ]

            resolved_usm_split_paths = []
            for usm_split_path in usm_split_paths:
                if not usm_split_path.exists():
                    usm_split_path_lower = usm_split_path.with_name(usm_split_path.name.lower())
                    if usm_split_path_lower.exists():
                        usm_split_path = validate_contained_file(
                            save_dir,
                            usm_split_path_lower.relative_to(save_dir).as_posix(),
                        )
                        logger.debug("Found %s instead of %s", usm_split_path, usm_split_paths)
                    else:
                        raise FileNotFoundError(f"{usm_split_path} not found in {save_dir}")
                else:
                    usm_split_path = validate_contained_file(
                        save_dir,
                        usm_split_path.relative_to(save_dir).as_posix(),
                    )
                resolved_usm_split_paths.append(usm_split_path)

            m2v_path = _demux_usm_sources_in_memory(
                resolved_usm_split_paths,
                usm_output_path,
                save_dir,
                usm_in_memory_limit,
            )
            if m2v_path is not None:
                for usm_split_path in resolved_usm_split_paths:
                    _discard_exported_file_sync(exported_files, usm_split_path)
                    usm_split_path.unlink()
                logger.debug("Demuxed %s in memory to %s", usm_split_filenames, m2v_path.name)
                video_jobs.append(m2v_path.as_posix())
                continue

            atomic_write_stream(
                usm_output_path,
                _stream_files(resolved_usm_split_paths),
            )
            for usm_split_path in resolved_usm_split_paths:
                _discard_exported_file_sync(exported_files, usm_split_path)
                usm_split_path.unlink()

            logger.debug("Merged %s to %s", usm_split_filenames, usm_output_name)
            exported_files.append(usm_output_path)
        else:
            logger.warning("Empty movieBundleDatas in %s", save_dir)
            continue

        if usm_output_path.exists():
            video_jobs.append(usm_output_path.as_posix())

    validated_exported_files = [
        validate_contained_file(
            output_root,
            path.relative_to(output_root).as_posix(),
        )
        for path in exported_files
    ]
    return (
        [path.as_posix() for path in validated_exported_files],
        audio_jobs,
        video_jobs,
    )


async def _append_audio_outputs(
    exported_files: List[Path],
    audio_jobs: list[tuple[str, list[str]]],
    extracted_save_path: Path,
    config,
) -> None:
    audio_file_semaphore = _get_shared_audio_file_semaphore(config)
    extracted_root = StdPath(extracted_save_path.as_posix())
    for save_dir_path, extracted_audio_files in audio_jobs:
        save_dir = Path(save_dir_path)
        audio_tasks = [
            asyncio.create_task(
                _process_extracted_audio_file(
                    extracted_audio_file,
                    save_dir,
                    config,
                    audio_file_semaphore,
                )
            )
            for extracted_audio_file in extracted_audio_files
        ]
        audio_results = await asyncio.gather(*audio_tasks)
        for audio_files in audio_results:
            for audio_file in audio_files:
                exported_files.append(
                    Path(
                        validate_contained_file(
                            extracted_root,
                            StdPath(audio_file.as_posix()).relative_to(extracted_root).as_posix(),
                        ).as_posix()
                    )
                )


async def _append_video_outputs(
    exported_files: List[Path],
    video_jobs: list[str],
    extracted_save_path: Path,
    config,
) -> None:
    extracted_root = StdPath(extracted_save_path.as_posix())
    for video_files, discarded_files in await _process_video_jobs(video_jobs, config):
        for video_file in video_files:
            exported_files.append(
                Path(
                    validate_contained_file(
                        extracted_root,
                        StdPath(video_file.as_posix()).relative_to(extracted_root).as_posix(),
                    ).as_posix()
                )
            )
        for discarded_file in discarded_files:
            _discard_exported_file(exported_files, discarded_file)


async def _cleanup_extracted_files(
    exported_files: List[Path],
    extracted_save_path: Path,
) -> None:
    extracted_root = StdPath(extracted_save_path.as_posix())
    for file in exported_files[:]:
        validate_contained_file(
            extracted_root,
            StdPath(file.as_posix()).relative_to(extracted_root).as_posix(),
        )
        if file.suffix in [".bytes", ".acb", ".usm"]:
            await file.unlink()
            logger.debug("Removed %s in cleanup stage", file)
            exported_files.remove(file)


async def extract_asset_bundle(
    bundle_save_path: Path,
    bundle: Dict[str, str],
    extracted_save_path: Path,
    unity_version: str = None,
    config=None,
    bundle_cache_root: Path | None = None,
) -> List[Path]:
    """Extract the asset bundle to the specified directory."""
    live2d_bundle = is_live2d_bundle(bundle)
    if getattr(config, "UPDATER_MODE", "assets") == "live2d" and bundle.get(
        "bundleName", ""
    ).startswith("live2d/motion/"):
        return []
    loop = asyncio.get_running_loop()
    exported_paths, audio_jobs, video_jobs = await loop.run_in_executor(
        _get_shared_extract_process_pool(config),
        partial(
            _extract_bundle_files_sync,
            bundle_save_path.as_posix(),
            bundle,
            extracted_save_path.as_posix(),
            unity_version,
            _get_texture_output_formats(config),
            bundle_cache_root.as_posix() if bundle_cache_root is not None else None,
            live2d_bundle=live2d_bundle,
            webp_method=_get_texture_webp_method(config),
            png_compression=_get_texture_png_compression(config),
            usm_in_memory_limit=_get_usm_in_memory_limit(config),
        ),
    )

    exported_files: List[Path] = [Path(path) for path in exported_paths]

    await _append_audio_outputs(exported_files, audio_jobs, extracted_save_path, config)
    await _append_video_outputs(exported_files, video_jobs, extracted_save_path, config)
    await _cleanup_extracted_files(exported_files, extracted_save_path)

    return exported_files
