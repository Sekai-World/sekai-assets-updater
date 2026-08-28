"""Unity object traversal and extraction."""

import logging
from pathlib import Path
from typing import Any, cast

import orjson as json

from updater.extract.paths import build_unityfs_save_path, resolve_generated_child_path
from updater.extract.playable import extract_playable
from updater.live2d.moc3 import extract_params_ids_from_moc3
from updater.live2d.motion3 import correct_param_ids, restore_unity_object_to_motion3
from updater.media.images import (
    DEFAULT_PNG_COMPRESSION,
    DEFAULT_WEBP_METHOD,
    render_image_asset,
    save_image_formats,
)
from updater.security import atomic_write_bytes
from updater.unity_rs_adapter import read_text_bytes

logger = logging.getLogger("live2d")


def extract_unity_objects(
    unity_file,
    output_root: Path,
    texture_output_formats: tuple[str, ...],
    *,
    live2d_bundle: bool,
    webp_method: int = DEFAULT_WEBP_METHOD,
    png_compression: str | int = DEFAULT_PNG_COMPRESSION,
) -> tuple[list[Path], list[tuple[Path, list[dict]]], list[tuple[Path, list[dict]]]]:
    exported_files: list[Path] = []
    post_process_acb_files: list[tuple[Path, list[dict]]] = []
    post_process_movie_bundles: list[tuple[Path, list[dict]]] = []
    additional_motion_jobs: list[tuple[Any, Path]] = []
    param_id_map: dict[str, str] = {}

    for unityfs_path, unityfs_obj in unity_file.container.items():
        try:
            save_path = build_unityfs_save_path(unityfs_path, output_root)
        except Exception:
            logger.exception("Failed to get relative path for %s", unityfs_path)
            raise

        save_path = save_path.with_name(save_path.name.strip())
        if (
            live2d_bundle
            and len(save_path.parts) >= 2
            and save_path.parts[0] == "live2d"
            and save_path.parts[1] == "motion"
        ):
            logger.debug("Skipping live2d motion asset %s for post-processing", unityfs_path)
            continue
        save_dir = save_path.parent
        save_dir.mkdir(parents=True, exist_ok=True)

        try:
            match unityfs_obj.type.name:
                case "MonoBehaviour":
                    tree = None
                    try:
                        if unityfs_obj.serialized_type.node:
                            tree = unityfs_obj.read_typetree()
                    except AttributeError:
                        tree = unityfs_obj.read_typetree()
                    logger.debug("Saving MonoBehaviour %s to %s", unityfs_path, save_path)

                    if unityfs_path.endswith(".playable"):
                        tree = extract_playable(unity_file, unityfs_path)

                    atomic_write_bytes(save_path, json.dumps(tree, option=json.OPT_INDENT_2))
                    exported_files.append(save_path)

                    if (
                        live2d_bundle
                        and isinstance(tree, dict)
                        and tree.get("AdditionalMotionData")
                    ):
                        additional_motion_jobs.append((unityfs_obj.read(), save_dir))

                    tree_mapping = cast(dict[str, Any], tree)
                    if "acbFiles" in tree_mapping:
                        post_process_acb_files.append((save_dir, tree_mapping["acbFiles"]))
                        logger.debug(
                            "Found acbFiles in %s: %s", unityfs_path, tree_mapping["acbFiles"]
                        )
                    elif "movieBundleDatas" in tree_mapping:
                        post_process_movie_bundles.append(
                            (save_dir, tree_mapping["movieBundleDatas"])
                        )
                        logger.debug(
                            "Found movieBundleDatas in %s: %s",
                            unityfs_path,
                            tree_mapping["movieBundleDatas"],
                        )
                case "TextAsset":
                    if save_path.suffix == ".bytes":
                        save_path = save_path.with_suffix("")
                    data_bytes = read_text_bytes(unityfs_obj)
                    atomic_write_bytes(save_path, data_bytes)
                    if live2d_bundle and save_path.suffix == ".moc3":
                        param_id_map.update(extract_params_ids_from_moc3(data_bytes))
                    exported_files.append(save_path)
                case "Texture2D" | "Sprite":
                    exported_files.extend(
                        save_image_formats(
                            render_image_asset(unityfs_obj),
                            save_path,
                            texture_output_formats,
                            webp_method=webp_method,
                            png_compression=png_compression,
                        )
                    )
                case "Texture2DArray":
                    data = unityfs_obj.read()
                    for index, image in enumerate(data.images):
                        texture_path = save_path.with_name(f"{save_path.stem}_{index}")
                        exported_files.extend(
                            save_image_formats(
                                image,
                                texture_path,
                                texture_output_formats,
                                webp_method=webp_method,
                                png_compression=png_compression,
                            )
                        )
                case "AudioClip":
                    data = unityfs_obj.read()
                    for filename, sample_data in data.samples.items():
                        sample_path = resolve_generated_child_path(save_dir, filename)
                        logger.debug("Saving audio clip %s to %s", filename, sample_path)
                        atomic_write_bytes(sample_path, sample_data)
                        exported_files.append(sample_path)
                case "Mesh" | "Cubemap":
                    logger.warning(
                        "%s data is not supported yet, skipping %s",
                        unityfs_obj.type.name,
                        unityfs_path,
                    )
                case _:
                    logger.warning(
                        "Unknown type %s of %s, extracting typetree",
                        unityfs_obj.type.name,
                        unityfs_path,
                    )
                    tree = unityfs_obj.read_typetree()
                    try:
                        json.dumps(tree)
                    except (ValueError, TypeError):
                        logger.warning("Failed to serialize %s, skipping", tree)
                    atomic_write_bytes(save_path, json.dumps(tree, option=json.OPT_INDENT_2))
                    exported_files.append(save_path)
        except (ValueError, TypeError, AttributeError, OSError) as exc:
            logger.exception("Failed to extract %s: %s", unityfs_path, exc)
            raise

    if live2d_bundle:
        for mono_behaviour, save_dir in additional_motion_jobs:
            motions = [
                restore_unity_object_to_motion3(motion)
                for motion in mono_behaviour.AdditionalMotionData
            ]
            motions = [motion for motion in motions if motion is not None]
            correct_param_ids(motions, param_id_map)
            motion_dir = save_dir / "motions"
            motion_dir.mkdir(parents=True, exist_ok=True)
            build_motion_path = motion_dir / "BuildMotionData.json"
            atomic_write_bytes(
                build_motion_path,
                json.dumps({"motions": [name for name, _ in motions]}, option=json.OPT_INDENT_2),
            )
            exported_files.append(build_motion_path)
            for name, motion in motions:
                motion_path = resolve_generated_child_path(motion_dir, name, ".motion3.json")
                atomic_write_bytes(motion_path, json.dumps(motion, option=json.OPT_INDENT_2))
                exported_files.append(motion_path)

    return exported_files, post_process_acb_files, post_process_movie_bundles
