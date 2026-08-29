"""The synchronous per-bundle extraction worker run inside the process pool."""

import logging
import os
import re
import shutil
import tempfile
from pathlib import Path as StdPath
from typing import Dict

from updater.extract.acb_cache import (
    extract_acb_from_cached_bundles as _extract_acb_from_cached_bundles_sync,
)
from updater.extract.paths import (
    discard_exported_file as _discard_exported_file_sync,
)
from updater.extract.paths import (
    replace_suffix_secure as _replace_suffix_secure,
)
from updater.extract.paths import (
    resolve_existing_path as _resolve_existing_path_sync,
)
from updater.extract.paths import (
    resolve_existing_usm_path as _resolve_existing_usm_path_sync,
)
from updater.extract.paths import (
    resolve_generated_child_path as _resolve_generated_child_path,
)
from updater.extract.paths import (
    resolve_local_audio_outputs as _resolve_local_audio_outputs_sync,
)
from updater.extract.paths import (
    resolve_shared_audio_outputs as _resolve_shared_audio_outputs_sync,
)
from updater.extract.paths import (
    stream_files as _stream_files,
)
from updater.extract.unity_objects import extract_unity_objects
from updater.media.acb import decode_acb_bytes, extract_acb
from updater.media.video import (
    DEFAULT_USM_IN_MEMORY_MAX_BYTES,
    demux_usm_sources_in_memory,
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
_BYTES_SUFFIX = ".bytes"


def _copy_shared_audio_outputs(
    output_root: StdPath,
    save_dir: StdPath,
    cue_name: str,
    exported_files: list[StdPath],
) -> bool:
    shared_audio_paths = _resolve_shared_audio_outputs_sync(output_root, save_dir, cue_name)
    if not shared_audio_paths:
        return False
    for shared_audio_path in shared_audio_paths:
        copied_audio_path = _resolve_generated_child_path(save_dir, shared_audio_path.name)
        shutil.copy2(shared_audio_path, copied_audio_path)
        exported_files.append(copied_audio_path)
    logger.debug(
        "Copied shared audio outputs for %s from %s to %s",
        cue_name,
        shared_audio_paths[0].parent,
        save_dir,
    )
    return True


def _log_missing_acb_source(
    output_root: StdPath,
    save_dir: StdPath,
    acb_textasset_filename: str,
    cue_name: str,
    exported_files: list[StdPath],
) -> None:
    if _copy_shared_audio_outputs(output_root, save_dir, cue_name, exported_files):
        return
    local_audio_paths = _resolve_local_audio_outputs_sync(save_dir, cue_name)
    if local_audio_paths:
        logger.debug(
            "ACB textasset %s not found, but audio for %s already extracted locally, skipping ACB processing",
            acb_textasset_filename,
            cue_name,
        )
        return
    logger.warning(
        "ACB textasset %s not found in %s and no shared/local audio available for %s; "
        "the ACB data likely resides in a separate bundle. "
        "Skipping this acbFile entry - the audio will be extracted when that bundle is processed",
        acb_textasset_filename,
        save_dir,
        cue_name,
    )


def _resolve_unsplit_acb(
    bundle_path: StdPath,
    save_dir: StdPath,
    acb_textasset_filename: str,
    cue_name: str,
    acb_output_path: StdPath,
    output_root: StdPath,
    unity_version: str | None,
    bundle_cache_root: str | None,
    exported_files: list[StdPath],
) -> StdPath | None:
    expected_path = _resolve_generated_child_path(
        save_dir,
        acb_textasset_filename.removesuffix(_BYTES_SUFFIX).removesuffix(".acb"),
        ".acb",
    )
    try:
        acb_textasset_path = _resolve_existing_path_sync(expected_path, save_dir, ".acb")
    except FileNotFoundError:
        if _extract_acb_from_cached_bundles_sync(
            bundle_path,
            acb_textasset_filename,
            acb_output_path,
            unity_version,
            StdPath(bundle_cache_root) if bundle_cache_root else None,
        ):
            return acb_output_path
        _log_missing_acb_source(
            output_root,
            save_dir,
            acb_textasset_filename,
            cue_name,
            exported_files,
        )
        return None

    if acb_textasset_path != acb_output_path:
        acb_textasset_path.rename(acb_output_path)
        _discard_exported_file_sync(exported_files, acb_textasset_path)
        exported_files.append(acb_output_path)
        logger.debug(
            "Renamed %s to %s to match cue sheet name",
            acb_textasset_path,
            acb_output_path,
        )
    return acb_output_path


def _merge_split_acb(
    save_dir: StdPath,
    acb_file: dict,
    acb_output_path: StdPath,
    exported_files: list[StdPath],
) -> bool:
    pattern = re.compile(r"{(\d)\:D(\d)}")
    acb_textasset_filenames = [
        pattern.sub(r"{\1:0\2d}", acb_file["assetBundleFileName"]).format(i).lower()
        for i in range(1, acb_file["spilitFileNum"] + 1)
    ]
    try:
        acb_textasset_paths = [
            _resolve_existing_path_sync(
                _resolve_generated_child_path(
                    save_dir, acb_textasset_filename.removesuffix(_BYTES_SUFFIX)
                ),
                save_dir,
            )
            for acb_textasset_filename in acb_textasset_filenames
        ]
    except FileNotFoundError:
        logger.error("%s not found in %s", acb_textasset_filenames, save_dir)
        return False

    atomic_write_stream(acb_output_path, _stream_files(acb_textasset_paths))
    for acb_textasset_path in acb_textasset_paths:
        _discard_exported_file_sync(exported_files, acb_textasset_path)
        acb_textasset_path.unlink()
    logger.debug("Merged %s to %s.acb", acb_textasset_filenames, acb_file["cueSheetName"])
    return True


def _decode_acb_output(
    acb_output_path: StdPath,
    save_dir: StdPath,
    cue_name: str | None,
    exported_files: list[StdPath],
    audio_jobs: list[tuple[str, list[str]]],
) -> None:
    promoted_audio: list[str] = []
    try:
        decoded_tracks = decode_acb_bytes(acb_output_path.read_bytes(), cue_name)
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


def _process_acb_file(
    bundle_path: StdPath,
    save_dir: StdPath,
    acb_file: dict,
    output_root: StdPath,
    unity_version: str | None,
    bundle_cache_root: str | None,
    exported_files: list[StdPath],
    audio_jobs: list[tuple[str, list[str]]],
) -> None:
    cue_name: str = acb_file["cueSheetName"]
    acb_output_path = _replace_suffix_secure(save_dir, cue_name, ".acb")
    if acb_file["formatType"] == 0 or acb_file["spilitFileNum"] == 0:
        acb_textasset_filename: str = acb_file["assetBundleFileName"]
        logger.debug("Try to find %s in %s", acb_textasset_filename, save_dir)
        if (
            _resolve_unsplit_acb(
                bundle_path,
                save_dir,
                acb_textasset_filename,
                cue_name,
                acb_output_path,
                output_root,
                unity_version,
                bundle_cache_root,
                exported_files,
            )
            is None
        ):
            return
        if not acb_output_path.exists():
            _log_missing_acb_source(
                output_root,
                save_dir,
                acb_textasset_filename,
                cue_name,
                exported_files,
            )
            return
    elif not _merge_split_acb(save_dir, acb_file, acb_output_path, exported_files):
        return

    if acb_output_path.exists():
        acb_asset_name = (
            acb_file["assetBundleFileName"].removesuffix(_BYTES_SUFFIX).removesuffix(".acb")
        )
        decode_name = cue_name if cue_name != acb_asset_name else None
        _decode_acb_output(acb_output_path, save_dir, decode_name, exported_files, audio_jobs)
    else:
        logger.warning("%s not found in %s", acb_output_path, save_dir)


def _process_acb_groups(
    bundle_path: StdPath,
    acb_groups: list[tuple[StdPath, list[dict]]],
    output_root: StdPath,
    unity_version: str | None,
    bundle_cache_root: str | None,
    exported_files: list[StdPath],
    audio_jobs: list[tuple[str, list[str]]],
) -> None:
    for save_dir, acb_files in acb_groups:
        for acb_file in acb_files:
            _process_acb_file(
                bundle_path,
                save_dir,
                acb_file,
                output_root,
                unity_version,
                bundle_cache_root,
                exported_files,
                audio_jobs,
            )


def _resolve_usm_split_paths(save_dir: StdPath, usm_split_filenames: list[str]) -> list[StdPath]:
    resolved_paths = []
    for usm_split_filename in usm_split_filenames:
        usm_split_path = _resolve_generated_child_path(
            save_dir, usm_split_filename.removesuffix(_BYTES_SUFFIX)
        )
        if not usm_split_path.exists():
            lower_path = usm_split_path.with_name(usm_split_path.name.lower())
            if not lower_path.exists():
                raise FileNotFoundError(f"{usm_split_path} not found in {save_dir}")
            usm_split_path = validate_contained_file(
                save_dir,
                lower_path.relative_to(save_dir).as_posix(),
            )
            logger.debug("Found %s instead of %s", usm_split_path, usm_split_filename)
        else:
            usm_split_path = validate_contained_file(
                save_dir,
                usm_split_path.relative_to(save_dir).as_posix(),
            )
        resolved_paths.append(usm_split_path)
    return resolved_paths


def _process_movie_group(
    save_dir: StdPath,
    movie_bundles: list[dict],
    usm_in_memory_limit: int,
    exported_files: list[StdPath],
    video_jobs: list[str],
) -> None:
    if len(movie_bundles) == 1:
        movie_bundle = movie_bundles[0]
        usm_output_name = movie_bundle["usmFileName"].removesuffix(_BYTES_SUFFIX)
        usm_output_path = _replace_suffix_secure(save_dir, usm_output_name, ".usm")
        usm_output_path = _resolve_existing_usm_path_sync(usm_output_path, save_dir)
        m2v_path = demux_usm_sources_in_memory(
            [usm_output_path], usm_output_path, save_dir, usm_in_memory_limit
        )
        if m2v_path is not None:
            _discard_exported_file_sync(exported_files, usm_output_path)
            usm_output_path.unlink(missing_ok=True)
            video_jobs.append(m2v_path.as_posix())
            return
    elif len(movie_bundles) > 1:
        pattern = re.compile(r"-\d{3}.usm.bytes")
        usm_output_name = pattern.sub(".usm", movie_bundles[0]["usmFileName"])
        usm_output_path = _replace_suffix_secure(save_dir, usm_output_name, ".usm")
        usm_split_filenames = [item["usmFileName"] for item in movie_bundles]
        resolved_usm_split_paths = _resolve_usm_split_paths(save_dir, usm_split_filenames)
        m2v_path = demux_usm_sources_in_memory(
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
            return
        atomic_write_stream(usm_output_path, _stream_files(resolved_usm_split_paths))
        for usm_split_path in resolved_usm_split_paths:
            _discard_exported_file_sync(exported_files, usm_split_path)
            usm_split_path.unlink()
        logger.debug("Merged %s to %s", usm_split_filenames, usm_output_name)
        exported_files.append(usm_output_path)
    else:
        logger.warning("Empty movieBundleDatas in %s", save_dir)
        return

    if usm_output_path.exists():
        video_jobs.append(usm_output_path.as_posix())


def _process_movie_groups(
    movie_groups: list[tuple[StdPath, list[dict]]],
    usm_in_memory_limit: int,
    exported_files: list[StdPath],
    video_jobs: list[str],
) -> None:
    for save_dir, movie_bundles in movie_groups:
        _process_movie_group(
            save_dir,
            movie_bundles,
            usm_in_memory_limit,
            exported_files,
            video_jobs,
        )


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

    in_memory_limit: int = (
        DEFAULT_USM_IN_MEMORY_MAX_BYTES if usm_in_memory_limit is None else usm_in_memory_limit
    )
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

    _process_acb_groups(
        bundle_path,
        post_process_acb_files,
        output_root,
        unity_version,
        bundle_cache_root,
        exported_files,
        audio_jobs,
    )
    _process_movie_groups(
        post_process_movie_bundles,
        in_memory_limit,
        exported_files,
        video_jobs,
    )

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
