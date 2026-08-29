"""Aggregate Live2D model tree: recovery, remote index, and publishing."""

import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path as StdPath
from typing import Any, Dict

import orjson as json
from anyio import Path

from updater.external_process import (
    get_external_process_timeout as _get_external_process_timeout,
)
from updater.extract.bundle import extract_asset_bundle
from updater.security import prepare_secure_directory
from updater.storage.rclone import upload_directory
from updater.workspace import configured_path as _configured_path
from updater.workspace import get_bundle_cache_path

logger = logging.getLogger("asset_updater")
_MODEL_FILE_SUFFIX = ".model3.json"


def _model_list(model_dir: StdPath) -> list[dict[str, str]]:
    models = []
    for model_file in sorted(model_dir.rglob(f"*{_MODEL_FILE_SUFFIX}")):
        models.append(
            {
                "modelName": model_file.name.removesuffix(_MODEL_FILE_SUFFIX),
                "modelBase": model_file.parent.name,
                "modelPath": str(model_file.parent.relative_to(model_dir)),
                "modelFile": model_file.name,
            }
        )
    return models


def _models_from_remote_entries(entries) -> list[dict[str, str]]:
    """Convert an rclone lsjson corpus (relative to ``live2d/model``)."""
    models = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("IsDir"):
            continue
        relative_path = str(entry.get("Path", "")).replace("\\", "/")
        path = StdPath(relative_path)
        if not relative_path or relative_path.startswith("/") or path.drive or ".." in path.parts:
            raise ValueError(f"unsafe remote Live2D model path: {relative_path!r}")
        if not relative_path.endswith(_MODEL_FILE_SUFFIX):
            continue
        models.append(
            {
                "modelName": path.name.removesuffix(_MODEL_FILE_SUFFIX),
                "modelBase": path.parent.name,
                "modelPath": path.parent.as_posix() if path.parent != StdPath(".") else "",
                "modelFile": path.name,
            }
        )
    return sorted(models, key=lambda model: (model["modelPath"], model["modelFile"]))


def _validate_live2d_storage(storage: dict) -> None:
    """Allow only non-destructive rclone operations for Live2D publishing."""
    args = storage.get("args")
    operation = args[0] if args else None
    if operation not in {"copy", "copyto"}:
        raise ValueError(
            f"Live2D storage requires a copy or copyto operation; got {operation or '<missing>'!r}"
        )


def _listing_args(storage: dict, remote_model_root: str) -> list[str]:
    """Build lsjson arguments while retaining configured opaque rclone flags."""
    _validate_live2d_storage(storage)
    configured = list(storage.get("args", []))
    if len(configured) >= 3 and configured[1:3] == ["src", "dst"]:
        configured = configured[3:]
    return ["lsjson", remote_model_root, "--recursive", *configured]


async def _remote_model_list(storage: dict, config) -> list[dict[str, str]]:
    """Read and validate the authoritative remote Live2D model corpus."""
    remote_root = f"{str(storage['base']).rstrip('/')}/live2d/model"
    process = await asyncio.create_subprocess_exec(
        storage["program"],
        *_listing_args(storage, remote_root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timeout = _get_external_process_timeout(config)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError("remote Live2D model listing timed out") from exc
    if process.returncode != 0:
        raise RuntimeError(
            f"remote Live2D model listing failed with status {process.returncode}: "
            f"{stderr.decode(errors='replace').strip()}"
        )
    try:
        entries = json.loads(stdout)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("remote Live2D model listing was not valid JSON") from exc
    models = _models_from_remote_entries(entries)
    if not models:
        raise RuntimeError("remote Live2D model listing was empty or contained no model files")
    return models


async def _publish_live2d_model_list(models: list[dict[str, str]], storage: dict, config) -> None:
    """Publish only the generated index after the remote corpus is validated."""
    with tempfile.TemporaryDirectory(prefix="sekai-live2d-index-") as temp_dir:
        index_dir = StdPath(temp_dir) / "live2d"
        index_dir.mkdir()
        (index_dir / "model_list.json").write_bytes(json.dumps(models, option=json.OPT_INDENT_2))
        await upload_directory(
            Path(str(index_dir)),
            Path(f"{str(storage['base']).rstrip('/')}/live2d"),
            storage["program"],
            storage["args"],
            config=config,
        )


async def _upload_live2d_assets(source_dir: StdPath, storage: dict, config) -> None:
    """Upload current assets without allowing a stale local index to leak through."""
    with tempfile.TemporaryDirectory(prefix="sekai-live2d-assets-") as temp_dir:
        staged = StdPath(temp_dir) / "live2d"
        staged.mkdir()
        for child in source_dir.iterdir():
            if child.name == "model_list.json":
                continue
            destination = staged / child.name
            if child.is_dir():
                shutil.copytree(child, destination)
            else:
                shutil.copy2(child, destination)
        await upload_directory(
            Path(str(staged)),
            Path(f"{str(storage['base']).rstrip('/')}/live2d"),
            storage["program"],
            storage["args"],
            config=config,
        )


def _select_model_bundles(bundles: Dict[str, Dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        bundle
        for bundle in bundles.values()
        if isinstance(bundle, dict) and (bundle.get("bundleName") or "").startswith("live2d/model/")
    ]


async def _validate_cached_model_bundles(
    config,
    model_bundles: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], Path]]:
    cached_model_bundles = []
    for bundle in model_bundles:
        bundle_path = get_bundle_cache_path(config, bundle)
        if bundle_path is None or not await Path(str(bundle_path)).is_file():
            raise RuntimeError(
                "Live2D recovery unavailable: cached model bundle file is missing: "
                f"{bundle.get('bundleName', '<unknown>')}"
            )
        cached_model_bundles.append((bundle, Path(str(bundle_path))))
    return cached_model_bundles


async def _extract_cached_model_bundles(
    config,
    cached_model_bundles: list[tuple[dict[str, Any], Path]],
    staging_root: Path,
    cache_root: Path,
) -> None:
    for bundle, bundle_path in cached_model_bundles:
        try:
            exported = await extract_asset_bundle(
                bundle_path,
                bundle,
                staging_root,
                unity_version=config.UNITY_VERSION,
                config=config,
                bundle_cache_root=cache_root,
            )
        except Exception as exc:
            raise RuntimeError(
                "Live2D recovery failed extracting cached model bundle "
                f"{bundle.get('bundleName', '<unknown>')}: {exc}"
            ) from exc
        if not exported:
            raise RuntimeError(
                "Live2D recovery failed: cached model bundle produced no outputs: "
                f"{bundle.get('bundleName', '<unknown>')}"
            )


def _promote_recovered_models(extracted_root: StdPath, staging_root: StdPath) -> None:
    staged_model = staging_root / "live2d" / "model"
    if not staged_model.is_dir() or not any(staged_model.rglob(f"*{_MODEL_FILE_SUFFIX}")):
        raise RuntimeError("Live2D recovery failed: cached model bundles produced no model files")

    target_model = extracted_root / "live2d" / "model"
    target_model.parent.mkdir(parents=True, exist_ok=True)
    backup_model = target_model.with_name(f".model-backup-{uuid.uuid4().hex}")
    had_existing_target = target_model.exists()
    try:
        if had_existing_target:
            os.replace(target_model, backup_model)
        os.replace(staged_model, target_model)
    except Exception as exc:
        if had_existing_target and backup_model.exists() and not target_model.exists():
            os.replace(backup_model, target_model)
        raise RuntimeError(f"Live2D recovery failed promoting model outputs: {exc}") from exc
    finally:
        if backup_model.exists():
            shutil.rmtree(backup_model)


async def recover_live2d_model_outputs(config, bundles: Dict[str, Dict[str, Any]]) -> None:
    """Transactionally rebuild the aggregate Live2D model tree from raw cache.

    Motion bundles are intentionally not extracted by forced Live2D runs.  Their
    raw-cache directory is therefore the source consumed by motion restoration;
    model bundles, on the other hand, must be extracted into the aggregate
    workspace using their current manifest metadata.
    """
    cache_root = _configured_path(getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None))
    extracted_root = _configured_path(getattr(config, "ASSET_LOCAL_EXTRACTED_DIR", None))
    if cache_root is None or extracted_root is None:
        raise RuntimeError(
            "Live2D recovery unavailable: bundle cache or extracted workspace is not configured"
        )

    cache_root = Path(prepare_secure_directory(cache_root).as_posix())
    motion_source = cache_root / "live2d" / "motion"
    if not await motion_source.is_dir():
        raise RuntimeError(
            f"Live2D recovery unavailable: cached motion source is missing: {motion_source}"
        )

    model_bundles = _select_model_bundles(bundles)
    if not model_bundles:
        raise RuntimeError(
            "Live2D recovery unavailable: current metadata contains no Live2D model bundles"
        )

    # Validate the whole current manifest cache before touching output. A
    # missing later bundle must not leave a partial aggregate model tree.
    cached_model_bundles = await _validate_cached_model_bundles(config, model_bundles)

    extracted_root_std = __import__("pathlib").Path(extracted_root.as_posix())
    staging_root_std = __import__("pathlib").Path(
        tempfile.mkdtemp(prefix=".sekai-live2d-recovery-", dir=extracted_root_std.parent)
    )
    staging_root = Path(staging_root_std.as_posix())
    try:
        await _extract_cached_model_bundles(config, cached_model_bundles, staging_root, cache_root)
        _promote_recovered_models(extracted_root_std, staging_root_std)
    finally:
        shutil.rmtree(staging_root_std, ignore_errors=True)
