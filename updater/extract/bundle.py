"""Async per-bundle extraction orchestration: pool submit and media fan-out."""

import asyncio
import logging
from functools import partial
from pathlib import Path as StdPath
from typing import Dict, List

from anyio import Path

from updater.extract.sync_worker import _extract_bundle_files_sync
from updater.media.audio import process_extracted_audio_file
from updater.media.images import (
    get_texture_output_formats,
    get_texture_png_compression,
    get_texture_webp_method,
)
from updater.media.video import get_usm_in_memory_limit, process_video_jobs
from updater.modes import is_live2d_bundle
from updater.runtime import runtime as _bundle_runtime
from updater.security import validate_contained_file

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
                process_extracted_audio_file(
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
    for video_files, discarded_files in await process_video_jobs(video_jobs, config):
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
    if getattr(config, "UPDATER_MODE", "assets") in {"live2d", "live2d-associated"} and bundle.get(
        "bundleName", ""
    ).startswith("live2d/motion/"):
        return []
    loop = asyncio.get_running_loop()
    worker_bundle = dict(bundle)
    worker_bundle["_enable_model3d_fbx_export"] = bool(  # type: ignore[index]
        getattr(config, "ENABLE_MODEL3D_FBX_EXPORT", False)
    )
    exported_paths, audio_jobs, video_jobs = await loop.run_in_executor(
        _get_shared_extract_process_pool(config),
        partial(
            _extract_bundle_files_sync,
            bundle_save_path.as_posix(),
            worker_bundle,
            extracted_save_path.as_posix(),
            unity_version,
            get_texture_output_formats(config),
            bundle_cache_root.as_posix() if bundle_cache_root is not None else None,
            live2d_bundle=live2d_bundle,
            webp_method=get_texture_webp_method(config),
            png_compression=get_texture_png_compression(config),
            usm_in_memory_limit=get_usm_in_memory_limit(config),
        ),
    )

    exported_files: List[Path] = [Path(path) for path in exported_paths]

    await _append_audio_outputs(exported_files, audio_jobs, extracted_save_path, config)
    await _append_video_outputs(exported_files, video_jobs, extracted_save_path, config)
    await _cleanup_extracted_files(exported_files, extracted_save_path)

    return exported_files
