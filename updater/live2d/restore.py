"""Motion-base bundle restore orchestration."""

import asyncio
import logging
import os
from pathlib import Path as StdPath
from pathlib import PurePosixPath
from typing import Dict

import orjson as json
from anyio import Path, open_file

from updater.constants import UNITY_FS_CONTAINER_BASE
from updater.live2d.moc3 import extract_params_ids_from_moc3
from updater.live2d.motion3 import correct_param_ids, restore_unity_object_to_motion3
from updater.unity_rs_adapter import load_bundle

logger = logging.getLogger("live2d")


class Live2DBuildMotion:
    _json_aliases = {
        "ClipAssetName": "clip_asset_name",
        "Clip": "clip",
    }

    clip_asset_name: str
    clip: object

    def __init__(
        self,
        clip_asset_name: str,
        clip: object,
    ):
        self.clip_asset_name = clip_asset_name
        self.clip = clip

    def __getattr__(self, name: str):
        try:
            python_name = self._json_aliases[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return object.__getattribute__(self, python_name)

    def to_dict(self) -> Dict[str, object]:
        return {
            json_name: getattr(self, python_name)
            for json_name, python_name in self._json_aliases.items()
        }

    def __repr__(self) -> str:
        return str(self.to_dict())


def get_max_concurrent_motion_base_files(config=None) -> int:
    value = getattr(
        config,
        "MAX_CONCURRENCY_MOTION_BASE_FILES",
        max(1, (os.cpu_count() or 1) // 2),
    )
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        logger.warning(
            "Invalid MAX_CONCURRENCY_MOTION_BASE_FILES=%r, falling back to 1",
            value,
        )
        return 1


def _build_motion_save_dir(
    buildmotiondata_path: str,
    local_live2d_motion_extracted_dir: StdPath,
) -> StdPath:
    reldir = (
        PurePosixPath(buildmotiondata_path)
        .relative_to(PurePosixPath(UNITY_FS_CONTAINER_BASE.as_posix()))
        .parent
    )
    rel_parts = reldir.parts[1:]
    if rel_parts[:2] == ("live2d", "motion"):
        rel_parts = rel_parts[2:]
    return local_live2d_motion_extracted_dir.joinpath(*rel_parts)


def _find_buildmotiondata(container_items):
    return next(
        (
            (asset_path, obj.read())
            for asset_path, obj in container_items
            if obj.type.name == "MonoBehaviour" and "buildmotiondata" in asset_path.lower()
        ),
        (None, None),
    )


def _find_container_animation_items(container_items, parent_name: str):
    return [
        Live2DBuildMotion(StdPath(asset_path).stem, pptr)
        for asset_path, pptr in container_items
        if PurePosixPath(asset_path).parent.name == parent_name
        and PurePosixPath(asset_path).suffix == ".anim"
    ]


def _restore_motion_entries(entries, param_id_map: Dict[str, str]):
    restored = [restore_unity_object_to_motion3(entry) for entry in entries]
    restored = [entry for entry in restored if entry is not None]
    correct_param_ids(restored, param_id_map)
    return restored


def _restore_motion_group(
    buildmotiondata,
    container_items,
    group_name: str,
    param_id_map: Dict[str, str],
    motion_bundle_path: StdPath,
):
    entries = getattr(buildmotiondata, group_name)
    restored = _restore_motion_entries(entries, param_id_map)
    # Match original gating: only fall back to container .anim items when the
    # primary group is empty and BuildMotionData.Motions is also empty.
    if restored or buildmotiondata.Motions:
        return restored

    label = "facials" if group_name == "Facials" else "motions"
    parent_name = "facial" if group_name == "Facials" else "motion"
    logger.warning(
        "No %s found in %s, try searching container items",
        label,
        motion_bundle_path,
    )
    container_entries = _find_container_animation_items(container_items, parent_name)
    if not container_entries:
        raise RuntimeError(f"Failed to find {label} in {motion_bundle_path}")
    return _restore_motion_entries(container_entries, param_id_map)


def _write_restored_motions(save_dir, facials, motions):
    all_motion_names = {
        "expressions": [name for name, _ in facials],
        "motions": [name for name, _ in motions],
    }
    save_dir.mkdir(parents=True, exist_ok=True)
    (save_dir / "BuildMotionData.json").write_bytes(
        json.dumps(all_motion_names, option=json.OPT_INDENT_2)
    )

    facial_save_dir = save_dir / "facial"
    facial_save_dir.mkdir(parents=True, exist_ok=True)
    for name, motion in facials:
        (facial_save_dir / f"{name}.motion3.json").write_bytes(
            json.dumps(motion, option=json.OPT_INDENT_2)
        )

    motion_save_dir = save_dir / "motion"
    motion_save_dir.mkdir(parents=True, exist_ok=True)
    for name, motion in motions:
        (motion_save_dir / f"{name}.motion3.json").write_bytes(
            json.dumps(motion, option=json.OPT_INDENT_2)
        )


def _restore_motion_base_bundle_sync(
    motion_base_bundle_path: str,
    local_live2d_motion_extracted_dir: str,
    param_id_map: Dict[str, str],
    unity_version: str,
) -> str:
    motion_bundle_path = StdPath(motion_base_bundle_path)
    try:
        motion_base = load_bundle(motion_base_bundle_path, unity_version)
    except Exception as exc:
        raise RuntimeError(f"Failed to load motion bundle {motion_bundle_path}") from exc

    container_items = list(motion_base.container.items())
    buildmotiondata_path, buildmotiondata = _find_buildmotiondata(container_items)
    if not buildmotiondata_path or not buildmotiondata:
        raise RuntimeError(f"Failed to find buildmotiondata in {motion_bundle_path}")

    facials = _restore_motion_group(
        buildmotiondata,
        container_items,
        "Facials",
        param_id_map,
        motion_bundle_path,
    )
    motions = _restore_motion_group(
        buildmotiondata,
        container_items,
        "Motions",
        param_id_map,
        motion_bundle_path,
    )

    save_dir = _build_motion_save_dir(
        buildmotiondata_path,
        StdPath(local_live2d_motion_extracted_dir),
    )
    _write_restored_motions(save_dir, facials, motions)
    return save_dir.as_posix()


async def collect_param_id_map(
    local_live2d_model_extracted_dir: Path,
) -> Dict[str, str]:
    """Gather the parameter ID map from every ``*.moc3`` under *model_extracted_dir*."""
    param_id_map: Dict[str, str] = {}
    async for moc3_path in local_live2d_model_extracted_dir.glob("**/*.moc3"):
        async with await open_file(moc3_path, "rb") as f:
            moc3 = await f.read()
            param_id_map.update(extract_params_ids_from_moc3(moc3))
    return param_id_map


async def restore_live2d_motions(
    local_live2d_motion_bundle_cache_dir: Path,
    local_live2d_motion_extracted_dir: Path,
    local_live2d_model_extracted_dir: Path,
    unity_version: str,
    config=None,
    *,
    param_id_map: Dict[str, str] | None = None,
    bundle_paths: list[StdPath] | None = None,
):
    if not await local_live2d_motion_bundle_cache_dir.exists():
        raise FileNotFoundError(
            f"Motion bundle dir {local_live2d_motion_bundle_cache_dir} does not exist"
        )
    if not await local_live2d_model_extracted_dir.exists():
        raise FileNotFoundError(
            f"Model extracted dir {local_live2d_model_extracted_dir} does not exist"
        )

    # Gather param ID map (skip when caller pre-supplied one)
    if param_id_map is None:
        param_id_map = await collect_param_id_map(local_live2d_model_extracted_dir)
    logger.debug("Param ID map: %s", param_id_map)

    # Resolve the set of motion bundles to restore
    if bundle_paths is not None:
        motion_base_bundle_paths = list(bundle_paths)
    else:
        motion_base_bundle_paths = []
        async for motion_base_bundle_path in local_live2d_motion_bundle_cache_dir.glob("*"):
            if await motion_base_bundle_path.is_file():
                motion_base_bundle_paths.append(StdPath(motion_base_bundle_path))

    max_concurrency = get_max_concurrent_motion_base_files(config)
    logger.info(
        "Restoring %d live2d motion base bundle(s) with concurrency=%d",
        len(motion_base_bundle_paths),
        max_concurrency,
    )
    semaphore = asyncio.Semaphore(max_concurrency)

    async def restore_one(motion_base_bundle_path: StdPath) -> None:
        async with semaphore:
            save_dir = await asyncio.to_thread(
                _restore_motion_base_bundle_sync,
                motion_base_bundle_path.as_posix(),
                local_live2d_motion_extracted_dir.as_posix(),
                param_id_map,
                unity_version,
            )
            logger.info(
                "Restored %s motion data to %s",
                motion_base_bundle_path,
                save_dir,
            )

    await asyncio.gather(*(restore_one(path) for path in motion_base_bundle_paths))
