"""Mode dispatcher for specialized post-processing runs."""

import logging
import tempfile
from pathlib import Path as StdPath

from anyio import Path

from updater.live2d.restore import collect_param_id_map, restore_live2d_motions
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
from updater.postprocess.live2d_models import (
    _publish_live2d_model_list,
    _remote_model_list,
    _upload_live2d_assets,
    _validate_live2d_storage,
)
from updater.state import atomic_write_json, prepare_state_directory
from updater.storage.rclone import upload_directory

logger = logging.getLogger("asset_updater")


async def _process_live2d(
    config,
    motion_source: StdPath,
    extracted_dir: StdPath,
    *,
    skip_missing_sources: bool = False,
) -> None:
    """Incremental Live2D motion restore, upload, and state persist.

    State is only written after a successful restore **and** all uploads/publishes
    so that a crash mid-way causes a full rebuild on the next run.
    """
    live2d_storages = get_specialized_storage(config, "live2d")
    for storage in live2d_storages:
        _validate_live2d_storage(storage)

    model_dir = extracted_dir / "live2d" / "model"
    missing_sources = [str(p) for p in (motion_source, model_dir) if not p.is_dir()]
    if missing_sources:
        message = "Live2D post-processing sources are missing: " + ", ".join(missing_sources)
        if skip_missing_sources:
            logger.warning("Skipping optional Live2D post-processing: %s", message)
            return
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
        return

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
    if mode == "live2d" and getattr(config, "LIVE2D_BUNDLE_CACHE_DIR", None) is None:
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

    if not has_local_chart_sources(extracted_dir, score_include_list):
        if not await _fetch_chart_sources_or_skip(
            config, extracted_dir, score_include_list, skip_missing_sources
        ):
            return
    await _process_charts(config, extracted_dir, score_include_list)


async def _run_live2d_postprocess(
    config,
    extracted_dir: StdPath,
    motion_source: StdPath,
    skip_missing_sources: bool,
) -> None:
    await _process_live2d(
        config,
        motion_source,
        extracted_dir,
        skip_missing_sources=skip_missing_sources,
    )


async def run_specialized_postprocess(
    mode: str,
    config,
    *,
    extracted_dir_is_temporary: bool = False,
    skip_missing_sources: bool = False,
    score_include_list: list[str] | None = None,
) -> None:
    """Run mode-specific work only after every bundle has succeeded."""
    _validate_postprocess_config(mode, config)
    extracted_dir = StdPath(str(config.ASSET_LOCAL_EXTRACTED_DIR))
    if mode == "live2d":
        motion_source = StdPath(str(config.LIVE2D_BUNDLE_CACHE_DIR)) / "live2d" / "motion"
        await _run_live2d_postprocess(
            config,
            extracted_dir,
            motion_source,
            skip_missing_sources=skip_missing_sources,
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
