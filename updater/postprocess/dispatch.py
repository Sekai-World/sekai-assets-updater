"""Mode dispatcher for specialized post-processing runs."""

import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path as StdPath

from anyio import Path

from updater.live2d.automatic_selections import (
    DEFAULT_AUTOMATIC_MASTER_DB_VERSION,
    build_automatic_live2d_associated_selections,
)
from updater.live2d.contracts import Live2DIndex
from updater.live2d.index_builder import build_live2d_association_index
from updater.live2d.master_data import (
    DEFAULT_MASTER_DATA_BRANCH,
    prepare_online_master_data,
)
from updater.live2d.publication import validate_live2d_outputs
from updater.live2d.restore import collect_param_id_map, restore_live2d_motions
from updater.live2d.rollout import (
    Live2DAssociatedRolloutError,
    live2d_associated_namespace_path,
    load_live2d_index,
    publish_latest_associated_index,
    validate_publishable_index,
)
from updater.live2d.viewer_catalog import (
    PUBLIC_MODEL_LIST_FILENAME,
    collect_viewer_asset_files,
    stage_viewer_projection,
    viewer_asset_directories,
)
from updater.postprocess.charts import (
    _render_charts,
    fetch_chart_sources_from_storage,
    has_local_chart_sources,
    needs_temporary_chart_source,
)
from updater.postprocess.config import (
    _region_name,
    get_specialized_storage,
)
from updater.postprocess.incremental_state import (
    _CHART_STATE_SCHEMA_VERSION,
    _LIVE2D_STATE_SCHEMA_VERSION,
    chart_fingerprint,
    chart_state_path,
    compute_live2d_fingerprint,
    compute_motion_bundle_hashes,
    compute_score_hashes,
    live2d_state_path,
    load_chart_state,
    load_live2d_state,
    pending_motion_bundles,
    pending_score_paths,
    validate_chart_state,
    validate_live2d_state,
)
from updater.postprocess.live2d_associated_selections import (
    load_live2d_associated_selections,
)
from updater.postprocess.live2d_models import (
    _publish_live2d_model_list,
    _remote_model_list,
    _upload_live2d_assets,
    _validate_live2d_storage,
    recover_live2d_model_outputs,
)
from updater.state import atomic_write_json, prepare_state_directory
from updater.storage.rclone import upload_directory
from updater.workspace import get_bundle_cache_path

logger = logging.getLogger("asset_updater")

_BUILD_MOTION_DATA_FILENAME = "BuildMotionData.json"
_MOTION_CLIP_SUFFIX = ".motion3.json"
_OPTIONAL_LIVE2D_ASSOCIATED_LOG = "Skipping optional Live2D-associated post-processing: %s"


async def _process_live2d(
    config,
    motion_source: StdPath,
    extracted_dir: StdPath,
    *,
    skip_missing_sources: bool = False,
) -> bool:
    """Incremental Live2D motion restore, upload, and state persist.

    State is only written after a successful restore **and** all uploads/publishes
    so that a crash mid-way causes a full rebuild on the next run.
    """
    live2d_storages = get_specialized_storage(config, "live2d")
    logger.warning(
        "Deprecated legacy Live2D post-processing is enabled; "
        "use live2d-associated/v1 for association-index output"
    )
    for storage in live2d_storages:
        _validate_live2d_storage(storage)

    model_dir = extracted_dir / "live2d" / "model"
    missing_sources = [str(p) for p in (motion_source, model_dir) if not p.is_dir()]
    if missing_sources:
        message = "Live2D post-processing sources are missing: " + ", ".join(missing_sources)
        if skip_missing_sources:
            logger.warning("Skipping optional Live2D post-processing: %s", message)
            return False
        raise RuntimeError(message)

    param_id_map = await collect_param_id_map(Path(str(model_dir)))
    current = compute_motion_bundle_hashes(motion_source)
    fingerprint = compute_live2d_fingerprint(config, model_dir)
    stored_state = load_live2d_state(live2d_state_path(config))

    if (
        stored_state is not None
        and stored_state["fingerprint"] == fingerprint
        and not pending_motion_bundles(current, stored_state["motions"])
    ):
        logger.info("Live2D motions are up to date; skipping restore and upload")
        return True

    # Determine which bundles to restore
    if stored_state is None or stored_state["fingerprint"] != fingerprint:
        to_restore_names = sorted(current)
    else:
        to_restore_names = pending_motion_bundles(current, stored_state["motions"])

    to_restore_paths = [motion_source / name for name in to_restore_names]

    await restore_live2d_motions(
        Path(str(motion_source)),
        Path(str(extracted_dir / "live2d" / "motion")),
        Path(str(model_dir)),
        config.UNITY_VERSION,
        config=config,
        param_id_map=param_id_map,
        bundle_paths=to_restore_paths,
    )

    for storage in live2d_storages:
        await _upload_live2d_assets(extracted_dir / "live2d", storage, config)
        models = await _remote_model_list(storage, config)
        await _publish_live2d_model_list(models, storage, config)

    # Persist state only after all uploads/publishes succeed.
    previous_motions = (
        stored_state["motions"]
        if stored_state is not None and stored_state["fingerprint"] == fingerprint
        else {}
    )
    merged_motions = {**previous_motions, **{name: current[name] for name in to_restore_names}}
    payload = {
        "schema_version": _LIVE2D_STATE_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "motions": merged_motions,
    }
    state_file = live2d_state_path(config)
    prepare_state_directory(state_file.parent)
    atomic_write_json(state_file, payload, validate_live2d_state)
    return True


async def _upload_live2d_associated_projection(
    index: Live2DIndex | Mapping[str, object],
    source_root: StdPath,
    storage: dict,
    config,
) -> None:
    """Upload the latest public viewer projection, never local rollout metadata."""

    _validate_live2d_storage(storage)
    validated_index = validate_publishable_index(index)
    remote_root = Path(f"{str(storage['base']).rstrip('/')}/live2d-associated/v1")

    with tempfile.TemporaryDirectory(prefix="sekai-live2d-associated-root-") as temp_dir:
        root = StdPath(temp_dir) / "v1"
        root.mkdir()
        stage_viewer_projection(validated_index, source_root, root)

        # Upload assets first.  The model list is the only public commit marker
        # and is therefore uploaded after every referenced asset directory.
        for directory in viewer_asset_directories(validated_index):
            await upload_directory(
                Path(str(root / directory)),
                remote_root / directory,
                storage["program"],
                storage["args"],
                config=config,
            )

        model_list_upload = StdPath(temp_dir) / ".model-list-upload"
        model_list_upload.mkdir()
        shutil.copy2(
            root / PUBLIC_MODEL_LIST_FILENAME, model_list_upload / PUBLIC_MODEL_LIST_FILENAME
        )
        await upload_directory(
            Path(str(model_list_upload)),
            remote_root,
            storage["program"],
            storage["args"],
            config=config,
        )


def _validate_associated_storage(storage: dict) -> None:
    _validate_live2d_storage(storage)
    if not isinstance(storage.get("base"), str) or not storage["base"].strip():
        raise ValueError("Live2D-associated storage requires a non-empty base")
    if not isinstance(storage.get("program"), str) or not storage["program"].strip():
        raise ValueError("Live2D-associated storage requires a non-empty program")
    if not isinstance(storage.get("args"), list):
        raise ValueError("Live2D-associated storage requires an args list")


def _associated_model_outputs_exist(index: Live2DIndex, source_root: StdPath) -> bool:
    for record in index.model_outputs:
        references = record.file_references
        for relative in (
            references.moc,
            *references.textures,
            *((references.physics,) if references.physics is not None else ()),
        ):
            path = source_root / record.output_path / relative
            if path.is_symlink() or not path.is_file():
                return False
    return True


def _associated_motion_outputs_missing(index: Live2DIndex, source_root: StdPath) -> bool:
    for record in index.motion_sets:
        for output_directory, clips in (
            (source_root / record.motion_output_path, record.known_clips.motions),
            (source_root / record.facial_output_path, record.known_clips.facials),
        ):
            for clip in clips:
                path = output_directory / f"{clip}.motion3.json"
                if path.is_symlink() or not path.is_file():
                    return True
    return False


def _manifest_motion_outputs_exist(selections, source_root: StdPath) -> bool:
    """Check whether every selected motion set is already materialized."""

    for selection in selections.motion_sets:
        if not _manifest_motion_set_outputs_exist(selection, source_root):
            return False
    return True


def _manifest_motion_set_outputs_exist(selection, source_root: StdPath) -> bool:
    motion_bundle_directory = source_root / StdPath(str(selection.motion_bundle_output_path))
    motion_directory = source_root / selection.motion_output_path
    facial_directory = source_root / selection.facial_output_path
    build_motion_data_path = motion_bundle_directory / _BUILD_MOTION_DATA_FILENAME
    if not _manifest_motion_directories_exist(
        motion_bundle_directory,
        build_motion_data_path,
        motion_directory,
        facial_directory,
    ):
        return False

    build_motion_data = _load_manifest_motion_data(build_motion_data_path)
    if not isinstance(build_motion_data, Mapping):
        return False
    return _manifest_motion_clips_exist(
        build_motion_data,
        motion_directory,
        facial_directory,
    )


def _manifest_motion_directories_exist(
    motion_bundle_directory: StdPath,
    build_motion_data_path: StdPath,
    motion_directory: StdPath,
    facial_directory: StdPath,
) -> bool:
    return (
        not motion_bundle_directory.is_symlink()
        and motion_bundle_directory.is_dir()
        and not build_motion_data_path.is_symlink()
        and build_motion_data_path.is_file()
        and not motion_directory.is_symlink()
        and motion_directory.is_dir()
        and not facial_directory.is_symlink()
        and facial_directory.is_dir()
    )


def _load_manifest_motion_data(build_motion_data_path: StdPath):
    try:
        return json.loads(build_motion_data_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _manifest_motion_clips_exist(
    build_motion_data: Mapping,
    motion_directory: StdPath,
    facial_directory: StdPath,
) -> bool:
    for field_name, output_directory in (
        ("motions", motion_directory),
        ("expressions", facial_directory),
    ):
        clip_names = build_motion_data.get(field_name)
        if not isinstance(clip_names, list):
            return False
        if not _manifest_motion_clip_files_exist(clip_names, output_directory):
            return False
    return True


def _manifest_motion_clip_files_exist(clip_names: list, output_directory: StdPath) -> bool:
    for clip_name in clip_names:
        if not _manifest_motion_clip_name_is_safe(clip_name):
            return False
        clip_path = output_directory / f"{clip_name}{_MOTION_CLIP_SUFFIX}"
        if clip_path.is_symlink() or not clip_path.is_file():
            return False
    return True


def _manifest_motion_clip_name_is_safe(clip_name) -> bool:
    return (
        isinstance(clip_name, str)
        and bool(clip_name)
        and clip_name not in {".", ".."}
        and "/" not in clip_name
        and "\\" not in clip_name
        and not any(char.isspace() for char in clip_name)
    )


async def _ensure_manifest_motion_outputs(config, selections, source_root: StdPath) -> None:
    """Restore only the motion bundles explicitly selected by a manifest."""

    if _manifest_motion_outputs_exist(selections, source_root):
        return

    bundle_paths: list[StdPath] = []
    missing: list[str] = []
    for selection in selections.motion_sets:
        bundle = dict(selection.bundle)
        bundle_path = get_bundle_cache_path(config, bundle)
        bundle_name = bundle.get("bundleName", selection.motion_set_id)
        if bundle_path is None:
            missing.append(f"{bundle_name!r} (cache path unavailable)")
            continue
        path = StdPath(str(bundle_path))
        if path.is_symlink() or not path.is_file():
            missing.append(f"{bundle_name!r} ({path})")
            continue
        bundle_paths.append(path)

    if missing:
        raise FileNotFoundError(
            "selected Live2D motion bundle cache is missing: " + ", ".join(missing)
        )

    motion_cache_dir = StdPath(str(config.LIVE2D_BUNDLE_CACHE_DIR)) / "live2d" / "motion"
    model_dir = source_root / "model"
    param_id_map = await collect_param_id_map(Path(str(model_dir)))
    await restore_live2d_motions(
        Path(str(motion_cache_dir)),
        Path(str(source_root / "motion")),
        Path(str(model_dir)),
        config.UNITY_VERSION,
        config=config,
        param_id_map=param_id_map,
        bundle_paths=bundle_paths,
    )


async def _ensure_associated_motion_outputs(
    config,
    index: Live2DIndex,
    source_root: StdPath,
    *,
    motion_outputs_ready: bool = False,
) -> None:
    """Ensure the explicit index's motion files exist without duplicate restore work."""

    validation_error: Exception
    try:
        validate_live2d_outputs(index, source_root)
        return
    except Exception as exc:
        validation_error = exc
        if motion_outputs_ready:
            raise Live2DAssociatedRolloutError(
                "associated Live2D outputs are incomplete after the legacy motion restore"
            ) from validation_error

    if not _associated_model_outputs_exist(index, source_root):
        raise Live2DAssociatedRolloutError(
            "associated Live2D output root is missing one or more model outputs; "
            "recover the model bundles before restoring motions"
        ) from validation_error
    if not _associated_motion_outputs_missing(index, source_root):
        raise Live2DAssociatedRolloutError(
            "associated Live2D outputs failed validation and cannot be safely materialized"
        ) from validation_error

    cache_root = getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None)
    model_dir = source_root / "model"
    motion_source = StdPath(str(cache_root)) / "live2d" / "motion" if cache_root else None
    if not model_dir.is_dir() or motion_source is None or not motion_source.is_dir():
        raise Live2DAssociatedRolloutError(
            "associated Live2D motion outputs are missing and the model/motion bundle sources "
            "are not configured"
        ) from validation_error

    try:
        param_id_map = await collect_param_id_map(Path(str(model_dir)))
        await restore_live2d_motions(
            Path(str(motion_source)),
            Path(str(source_root / "motion")),
            Path(str(model_dir)),
            config.UNITY_VERSION,
            config=config,
            param_id_map=param_id_map,
        )
    except Exception as exc:
        raise Live2DAssociatedRolloutError(
            f"associated Live2D motion restoration failed: {exc}"
        ) from exc

    try:
        validate_live2d_outputs(index, source_root)
    except Exception as exc:
        raise Live2DAssociatedRolloutError(
            f"associated Live2D outputs remain invalid after motion restoration: {exc}"
        ) from exc


def _validated_associated_storages(config) -> list[dict]:
    storages = get_specialized_storage(config, "live2d-associated")
    if not storages:
        message = (
            "ENABLE_LIVE2D_ASSOCIATED_PIPELINE is enabled but no matching "
            "live2d-associated storage is configured; refusing local-only advancement"
        )
        logger.error(message)
        raise ValueError(message)
    for storage in storages:
        _validate_associated_storage(storage)
    return storages


def _resolve_associated_index_path(
    association_index,
    association_index_path,
    configured_path,
    configured_selections_path,
):
    if association_index is not None and association_index_path is not None:
        raise ValueError(
            "live2d-associated accepts either association_index or association_index_path, not both"
        )
    if association_index is not None:
        return None
    if association_index_path is not None:
        return association_index_path
    if configured_path is not None:
        return configured_path
    return None


def _associated_index_source_is_missing(
    association_index,
    association_index_path,
    configured_selections_path,
    configured_path=None,
) -> bool:
    return (
        association_index is None
        and association_index_path is None
        and configured_selections_path is None
        and configured_path is None
    )


async def _build_associated_index_from_manifest(
    config,
    source_root: StdPath,
    configured_selections_path,
    live2d_bundles,
    asset_metadata_version,
):
    if live2d_bundles is None:
        raise ValueError("association-selection manifest requires current live2d_bundles metadata")
    if not isinstance(asset_metadata_version, str) or not asset_metadata_version.strip():
        raise ValueError(
            "association-selection manifest requires a non-empty asset metadata version"
        )
    selections = load_live2d_associated_selections(
        configured_selections_path,
        output_root=source_root,
        live2d_bundles=live2d_bundles,
    )
    await _ensure_manifest_motion_outputs(config, selections, source_root)
    return build_live2d_association_index(
        provider=selections.provider,
        metadata_version=asset_metadata_version,
        model_outputs=selections.model_outputs,
        motion_sets=selections.motion_sets,
    )


def _automatic_model_outputs_exist(selections, source_root: StdPath) -> bool:
    for selection in selections.model_outputs:
        output_directory = source_root / selection.output_path
        if output_directory.is_symlink() or not output_directory.is_dir():
            return False
        try:
            model3_files = [
                path
                for path in output_directory.rglob("*.model3.json")
                if path.is_file() and not path.is_symlink()
            ]
            if len(model3_files) != 1:
                return False
        except (OSError, RuntimeError):
            return False
    return True


async def _build_associated_index_automatically(
    config,
    source_root: StdPath,
    live2d_bundles,
    asset_metadata_version,
):
    master_data_root = getattr(config, "LIVE2D_ASSOCIATION_MASTER_DATA_DIR", None)
    master_data_url = getattr(config, "LIVE2D_ASSOCIATION_MASTER_DATA_URL", None)
    master_data_branch = getattr(
        config,
        "LIVE2D_ASSOCIATION_MASTER_DATA_BRANCH",
        DEFAULT_MASTER_DATA_BRANCH,
    )
    master_db_version = getattr(
        config,
        "LIVE2D_ASSOCIATION_MASTER_DB_VERSION",
        DEFAULT_AUTOMATIC_MASTER_DB_VERSION,
    )
    prepared_master_data = None
    if master_data_root is None and master_data_url is not None:
        online_version = (
            None if master_db_version == DEFAULT_AUTOMATIC_MASTER_DB_VERSION else master_db_version
        )
        # urllib/tarfile are synchronous stdlib APIs.  Prepare one run-scoped
        # snapshot off the event loop, then use its local provider for all six
        # tables in this index build.
        prepared_master_data = await asyncio.to_thread(
            prepare_online_master_data,
            master_data_url,
            branch=master_data_branch,
            master_db_version=online_version,
            timeout=getattr(config, "REQUEST_TIMEOUT", 180),
        )
        master_data_root = prepared_master_data.root
        master_db_version = prepared_master_data.master_db_version

    try:
        selections = build_automatic_live2d_associated_selections(
            live2d_bundles,
            output_root=source_root,
            master_data_root=master_data_root,
            master_data_url=master_data_url,
            master_data_branch=master_data_branch,
            master_db_version=master_db_version,
        )
        if not isinstance(asset_metadata_version, str) or not asset_metadata_version.strip():
            raise ValueError(
                "automatic Live2D association generation requires a non-empty asset metadata version"
            )

        if not _automatic_model_outputs_exist(selections, source_root):
            configured_extracted_dir = getattr(config, "ASSET_LOCAL_EXTRACTED_DIR", None)
            recovery_root = (
                None
                if configured_extracted_dir is None
                else StdPath(str(configured_extracted_dir)) / "live2d"
            )
            if recovery_root is None or recovery_root.resolve(strict=False) != source_root.resolve(
                strict=False
            ):
                raise ValueError(
                    "automatic Live2D association generation cannot recover model outputs into a "
                    "custom association_output_root; use the configured extracted Live2D root"
                )
            recovery_bundles = (
                live2d_bundles
                if isinstance(live2d_bundles, dict)
                else {
                    metadata_key: dict(bundle)
                    for metadata_key, bundle in live2d_bundles.items()
                    if isinstance(bundle, Mapping)
                }
            )
            await recover_live2d_model_outputs(config, recovery_bundles)

        # This intentionally reuses the manifest restore path without writing a
        # manifest.  Its output layout is the one documented by automatic_selections.py.
        await _ensure_manifest_motion_outputs(config, selections, source_root)
        return build_live2d_association_index(
            provider=selections.provider,
            metadata_version=asset_metadata_version,
            model_outputs=selections.model_outputs,
            motion_sets=selections.motion_sets,
        )
    finally:
        if prepared_master_data is not None:
            prepared_master_data.cleanup()


async def _load_associated_index(
    config,
    source_root: StdPath,
    association_index,
    association_index_path,
    configured_selections_path,
    live2d_bundles,
    asset_metadata_version,
):
    if association_index is not None:
        index = association_index
    elif association_index_path is not None:
        index = load_live2d_index(association_index_path)
    elif configured_selections_path is not None:
        index = await _build_associated_index_from_manifest(
            config,
            source_root,
            configured_selections_path,
            live2d_bundles,
            asset_metadata_version,
        )
    else:
        index = await _build_associated_index_automatically(
            config,
            source_root,
            live2d_bundles,
            asset_metadata_version,
        )
    return validate_publishable_index(index)


async def _prepare_optional_associated_index(
    config,
    source_root: StdPath,
    association_index,
    association_index_path,
    configured_selections_path,
    live2d_bundles,
    asset_metadata_version,
):
    try:
        return await _load_associated_index(
            config,
            source_root,
            association_index,
            association_index_path,
            configured_selections_path,
            live2d_bundles,
            asset_metadata_version,
        )
    except Exception as exc:
        mapped_error = (
            exc
            if isinstance(exc, Live2DAssociatedRolloutError)
            else Live2DAssociatedRolloutError(f"associated Live2D index preparation failed: {exc}")
        )
        logger.warning(_OPTIONAL_LIVE2D_ASSOCIATED_LOG, mapped_error)
        return None


class _AssociatedIndexErrorMapper:
    def __enter__(self):
        return self

    def __exit__(self, _exception_type, exception, _traceback) -> bool:
        if (
            exception is None
            or not isinstance(exception, Exception)
            or isinstance(exception, Live2DAssociatedRolloutError)
        ):
            return False
        raise Live2DAssociatedRolloutError(
            f"associated Live2D index preparation failed: {exception}"
        ) from exception


async def _prepare_forced_associated_index(
    config,
    source_root: StdPath,
    association_index,
    association_index_path,
    configured_selections_path,
    live2d_bundles,
    asset_metadata_version,
):
    with _AssociatedIndexErrorMapper():
        return await _load_associated_index(
            config,
            source_root,
            association_index,
            association_index_path,
            configured_selections_path,
            live2d_bundles,
            asset_metadata_version,
        )


async def _prepare_associated_index(
    config,
    source_root: StdPath,
    association_index,
    association_index_path,
    configured_selections_path,
    live2d_bundles,
    asset_metadata_version,
    skip_missing_sources: bool,
):
    prepare = (
        _prepare_optional_associated_index
        if skip_missing_sources
        else _prepare_forced_associated_index
    )
    return await prepare(
        config,
        source_root,
        association_index,
        association_index_path,
        configured_selections_path,
        live2d_bundles,
        asset_metadata_version,
    )


async def _ensure_associated_outputs_for_dispatch(
    config,
    validated_index: Live2DIndex,
    source_root: StdPath,
    motion_outputs_ready: bool,
    skip_missing_sources: bool,
) -> bool:
    try:
        await _ensure_associated_motion_outputs(
            config,
            validated_index,
            source_root,
            motion_outputs_ready=motion_outputs_ready,
        )
    except Live2DAssociatedRolloutError as exc:
        if skip_missing_sources:
            logger.warning(_OPTIONAL_LIVE2D_ASSOCIATED_LOG, exc)
            return False
        raise
    return True


async def _upload_associated_projections(
    config,
    validated_index: Live2DIndex,
    source_root: StdPath,
    storages: list[dict],
) -> None:
    """Upload the latest projection to every configured storage, without receipts."""

    first_error: Exception | None = None
    for storage in storages:
        try:
            await _upload_live2d_associated_projection(
                validated_index,
                source_root,
                storage,
                config,
            )
        except Exception as exc:
            if first_error is None:
                first_error = exc
            logger.error("Live2D-associated projection upload failed: %s", exc)
    if first_error is not None:
        raise first_error


async def _process_live2d_associated(
    config,
    extracted_dir: StdPath,
    *,
    association_index: Live2DIndex | Mapping[str, object] | None = None,
    association_index_path: StdPath | str | None = None,
    association_output_root: StdPath | None = None,
    association_namespace_root: StdPath | None = None,
    association_state_path: StdPath | None = None,
    skip_missing_sources: bool = False,
    motion_outputs_ready: bool = False,
    live2d_bundles: Mapping[str, Mapping[str, object]] | None = None,
    asset_metadata_version: str | None = None,
) -> None:
    """Build, upload, and locally audit the latest associated projection."""

    storages = _validated_associated_storages(config)

    configured_path = getattr(config, "LIVE2D_ASSOCIATION_INDEX_PATH", None)
    configured_selections_path = getattr(config, "LIVE2D_ASSOCIATION_SELECTIONS_PATH", None)
    association_index_path = _resolve_associated_index_path(
        association_index,
        association_index_path,
        configured_path,
        configured_selections_path,
    )
    if _associated_index_source_is_missing(
        association_index,
        association_index_path,
        configured_selections_path,
        configured_path,
    ):
        logger.info(
            "No explicit Live2D association index or selection manifest configured; "
            "using automatic bundle discovery"
        )

    source_root = association_output_root or extracted_dir / "live2d"
    validated_index = await _prepare_associated_index(
        config,
        source_root,
        association_index,
        association_index_path,
        configured_selections_path,
        live2d_bundles,
        asset_metadata_version,
        skip_missing_sources,
    )
    if validated_index is None:
        return

    if not await _ensure_associated_outputs_for_dispatch(
        config,
        validated_index,
        StdPath(str(source_root)),
        motion_outputs_ready,
        skip_missing_sources,
    ):
        return
    try:
        collect_viewer_asset_files(validated_index, StdPath(str(source_root)))
    except Exception as exc:
        mapped_error = Live2DAssociatedRolloutError(f"associated viewer outputs are invalid: {exc}")
        if skip_missing_sources:
            logger.warning(_OPTIONAL_LIVE2D_ASSOCIATED_LOG, mapped_error)
            return
        raise mapped_error from exc

    await _upload_associated_projections(
        config,
        validated_index,
        StdPath(str(source_root)),
        storages,
    )

    namespace_root = association_namespace_root or live2d_associated_namespace_path(extracted_dir)
    publish_latest_associated_index(validated_index, source_root, namespace_root)

    logger.info(
        "Live2D-associated public viewer projection uploaded and latest local audit index stored"
    )


async def _process_charts(
    config, workspace_dir: StdPath, include_list: list[str] | None = None
) -> None:
    """Compute incremental state, render pending scores, upload, and persist state.

    State is only written after a successful render **and** upload so that a
    crash mid-way will cause a full rebuild on the next run.
    """
    current = compute_score_hashes(workspace_dir, include_list)
    stored_state = load_chart_state(chart_state_path(config))
    fingerprint = chart_fingerprint(config)

    if (
        stored_state is not None
        and stored_state["fingerprint"] == fingerprint
        and not pending_score_paths(current, stored_state["scores"])
    ):
        logger.info("Charts are up to date; skipping render and upload")
        return

    # Full rebuild when there is no stored state or the fingerprint changed.
    if stored_state is None or stored_state["fingerprint"] != fingerprint:
        to_render = sorted(current)
    else:
        to_render = pending_score_paths(current, stored_state["scores"])

    rendered = await _render_charts(config, workspace_dir, include_list, score_files=to_render)
    await _upload_specialized_directory(
        "charts", workspace_dir / "charts" / _region_name(config), config
    )

    # Persist state only after upload succeeds.
    previous_scores = (
        stored_state["scores"]
        if stored_state is not None and stored_state["fingerprint"] == fingerprint
        else {}
    )
    merged_scores = {**previous_scores, **{rel: current[rel] for rel in rendered}}
    payload = {
        "schema_version": _CHART_STATE_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "scores": merged_scores,
    }
    state_file = chart_state_path(config)
    prepare_state_directory(state_file.parent)
    atomic_write_json(state_file, payload, validate_chart_state)


def _validate_postprocess_config(mode: str, config) -> None:
    if config.ASSET_LOCAL_EXTRACTED_DIR is None:
        raise ValueError("Specialized modes require ASSET_LOCAL_EXTRACTED_DIR")
    if (
        mode in {"live2d", "live2d-associated"}
        and getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None) is None
    ):
        raise ValueError("live2d mode requires LIVE2D_BUNDLE_CACHE_DIR")


async def _fetch_chart_sources_or_skip(
    config,
    target_dir: StdPath,
    score_include_list: list[str] | None,
    skip_missing_sources: bool,
) -> bool:
    try:
        await fetch_chart_sources_from_storage(config, target_dir, score_include_list)
    except Exception as exc:
        if skip_missing_sources:
            logger.warning("Skipping optional Charts post-processing: %s", exc)
            return False
        raise
    return True


async def _run_charts_postprocess(
    config,
    extracted_dir: StdPath,
    extracted_dir_is_temporary: bool,
    skip_missing_sources: bool,
    score_include_list: list[str] | None,
) -> None:
    configured_extracted_dir = (
        None if extracted_dir_is_temporary else config.ASSET_LOCAL_EXTRACTED_DIR
    )
    temporary_source = needs_temporary_chart_source(
        extracted_dir, configured_extracted_dir, score_include_list
    )
    if temporary_source:
        with tempfile.TemporaryDirectory(prefix="sekai-charts-") as temp_dir:
            chart_source_dir = StdPath(temp_dir)
            if await _fetch_chart_sources_or_skip(
                config, chart_source_dir, score_include_list, skip_missing_sources
            ):
                await _process_charts(config, chart_source_dir, score_include_list)
        return

    if not has_local_chart_sources(
        extracted_dir, score_include_list
    ) and not await _fetch_chart_sources_or_skip(
        config, extracted_dir, score_include_list, skip_missing_sources
    ):
        return
    await _process_charts(config, extracted_dir, score_include_list)


async def _run_live2d_postprocess(
    config,
    extracted_dir: StdPath,
    motion_source: StdPath,
    skip_missing_sources: bool,
) -> bool:
    return await _process_live2d(
        config,
        motion_source,
        extracted_dir,
        skip_missing_sources=skip_missing_sources,
    )


async def _run_live2d_associated_postprocess(
    config,
    extracted_dir: StdPath,
    skip_missing_sources: bool,
    *,
    association_index: Live2DIndex | Mapping[str, object] | None = None,
    association_index_path: StdPath | str | None = None,
    association_output_root: StdPath | None = None,
    association_namespace_root: StdPath | None = None,
    association_state_path: StdPath | None = None,
    motion_outputs_ready: bool = False,
    live2d_bundles: Mapping[str, Mapping[str, object]] | None = None,
    asset_metadata_version: str | None = None,
) -> None:
    await _process_live2d_associated(
        config,
        extracted_dir,
        association_index=association_index,
        association_index_path=association_index_path,
        association_output_root=association_output_root,
        association_namespace_root=association_namespace_root,
        association_state_path=association_state_path,
        skip_missing_sources=skip_missing_sources,
        motion_outputs_ready=motion_outputs_ready,
        live2d_bundles=live2d_bundles,
        asset_metadata_version=asset_metadata_version,
    )


async def run_specialized_postprocess(
    mode: str,
    config,
    *,
    extracted_dir_is_temporary: bool = False,
    skip_missing_sources: bool = False,
    score_include_list: list[str] | None = None,
    association_index: Live2DIndex | Mapping[str, object] | None = None,
    association_index_path: StdPath | str | None = None,
    association_output_root: StdPath | None = None,
    association_namespace_root: StdPath | None = None,
    association_state_path: StdPath | None = None,
    motion_outputs_ready: bool = False,
    live2d_bundles: Mapping[str, Mapping[str, object]] | None = None,
    asset_metadata_version: str | None = None,
) -> bool | None:
    """Run mode-specific work only after every bundle has succeeded."""
    _validate_postprocess_config(mode, config)
    extracted_dir = StdPath(str(config.ASSET_LOCAL_EXTRACTED_DIR))
    if mode == "live2d":
        motion_source = StdPath(str(config.LIVE2D_BUNDLE_CACHE_DIR)) / "live2d" / "motion"
        return await _run_live2d_postprocess(
            config,
            extracted_dir,
            motion_source,
            skip_missing_sources=skip_missing_sources,
        )
    if mode == "live2d-associated":
        await _run_live2d_associated_postprocess(
            config,
            extracted_dir,
            skip_missing_sources,
            association_index=association_index,
            association_index_path=association_index_path,
            association_output_root=association_output_root,
            association_namespace_root=association_namespace_root,
            association_state_path=association_state_path,
            motion_outputs_ready=motion_outputs_ready,
            live2d_bundles=live2d_bundles,
            asset_metadata_version=asset_metadata_version,
        )
        return
    if mode == "charts":
        await _run_charts_postprocess(
            config,
            extracted_dir,
            extracted_dir_is_temporary,
            skip_missing_sources,
            score_include_list,
        )
        return
    raise ValueError(f"Unsupported specialized mode: {mode}")


async def _upload_specialized_directory(mode: str, source_dir: Path, config) -> None:
    if not get_specialized_storage(config, mode) or not source_dir.exists():
        return
    region = _region_name(config)
    for storage in get_specialized_storage(config, mode):
        remote_path = Path(storage["base"]) / ("live2d" if mode == "live2d" else region)
        await upload_directory(
            source_dir,
            remote_path,
            storage["program"],
            storage["args"],
            config=config,
        )
