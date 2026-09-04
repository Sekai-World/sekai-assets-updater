"""Top-level run orchestration and locking."""

import logging
import tempfile
import time
from typing import cast

from updater.cli.configuration import _StatePathConfig, require_config
from updater.cli.lifecycle import (
    _complete_with_download_list,
    _complete_with_empty_download_list,
    _write_metadata_only_cache,
)
from updater.cli.pending import (
    _build_new_download_list,
    _ensure_run_cache_dirs,
    _load_pending_download_lists,
    _merge_pending_and_new_download_lists,
    _pending_items_outside_mode,
)
from updater.model import ConfigLike
from updater.modes import (
    get_required_bundle_prefixes,
    mode_uses_bundle_pipeline,
    needs_live2d_bundle_cache,
    needs_shared_workspace,
)
from updater.net.metadata import build_request_headers, fetch_asset_bundle_info
from updater.net.plan import (
    dedupe_download_items,
)
from updater.postprocess.dispatch import run_specialized_postprocess
from updater.state import (
    StateLock,
    StatePaths,
    commit_empty_transaction,
    create_journal,
    derive_active_state_paths,
    derive_state_paths,
    prepare_state_directory,
    replay_journal,
)
from updater.workspace import ensure_dir_exists

logger = logging.getLogger("asset_updater")


def _asset_metadata_version(asset_bundle_info) -> str | None:
    version = asset_bundle_info.get("version")
    return version.strip() if isinstance(version, str) and version.strip() else None


async def _run_full_download_pipeline(
    cfg: ConfigLike,
    mode: str,
    force_full_download: bool,
    extracted_dir_is_temporary: bool,
    automatic_prefixes,
    fetch_result,
    start_time: float,
    paths: StatePaths | None = None,
) -> None:
    # Charts consume extracted scores from local/normal remote storage and do
    # not participate in the asset-bundle download pipeline at all.
    new_download_list, plan = await _build_new_download_list(
        cfg,
        mode,
        automatic_prefixes,
        fetch_result.asset_bundle_info,
        fetch_result.game_version_json,
        fetch_result.asset_ver,
        fetch_result.assetbundle_host_hash,
        force_full_download,
    )
    cached_pending_list, pending_list = await _load_pending_download_lists(
        cfg, mode, force_full_download
    )
    pending_items_outside_mode = _pending_items_outside_mode(cached_pending_list, mode)
    merge_started = time.perf_counter()
    download_list = _merge_pending_and_new_download_lists(pending_list, new_download_list)
    logger.info(
        "RUN | step=2/4 | timing=pending_merge | duration_sec=%.6f | pending=%d | new=%d | total=%d",
        time.perf_counter() - merge_started,
        len(pending_list),
        len(new_download_list),
        len(download_list),
    )

    if paths is not None and not paths.journal.exists():
        queue_items = dedupe_download_items(pending_items_outside_mode + download_list)
        if not download_list and not pending_items_outside_mode:
            commit_started = time.perf_counter()
            commit_empty_transaction(paths, plan.asset_metadata, plan.game_version)
            logger.info(
                "RUN | step=2/4 | timing=empty_transaction_commit | duration_sec=%.6f",
                time.perf_counter() - commit_started,
            )
        else:
            journal_started = time.perf_counter()
            verified_journal = create_journal(
                paths, [list(item) for item in queue_items], plan.asset_metadata, plan.game_version
            )
            logger.info(
                "RUN | step=2/4 | timing=journal_create | duration_sec=%.6f | items=%d",
                time.perf_counter() - journal_started,
                len(queue_items),
            )
            replay_started = time.perf_counter()
            replay_journal(paths, _verified_envelope=verified_journal)
            logger.info(
                "RUN | step=2/4 | timing=journal_replay | duration_sec=%.6f | items=%d",
                time.perf_counter() - replay_started,
                len(queue_items),
            )

    if not download_list:
        await _complete_with_empty_download_list(
            cfg,
            mode,
            pending_items_outside_mode,
            extracted_dir_is_temporary,
            start_time,
            paths,
            fetch_result.asset_bundle_info.get("bundles", {}),
            asset_metadata_version=_asset_metadata_version(fetch_result.asset_bundle_info),
        )
        return

    await _complete_with_download_list(
        cfg,
        mode,
        fetch_result.headers,
        fetch_result.cookie,
        download_list,
        pending_items_outside_mode,
        extracted_dir_is_temporary,
        start_time,
        paths,
        fetch_result.asset_bundle_info.get("bundles", {}),
        asset_metadata_version=_asset_metadata_version(fetch_result.asset_bundle_info),
    )


async def _run_main(
    update_asset_bundle_info_only: bool = False,
    force_full_download: bool = False,
    mode: str = "assets",
    extracted_dir_is_temporary: bool = False,
):
    cfg = require_config()
    cfg.UPDATER_MODE = mode

    paths = derive_active_state_paths(
        mode,
        cfg.DL_LIST_CACHE_PATH,
        cfg.ASSET_BUNDLE_INFO_CACHE_PATH,
        cfg.GAME_VERSION_JSON_CACHE_PATH,
    )
    active_cfg = cast(
        ConfigLike,
        _StatePathConfig(cfg, paths) if mode in {"live2d", "live2d-associated"} else cfg,
    )

    run_mode = "metadata-only" if update_asset_bundle_info_only else "full-pipeline"
    logger.info(
        "RUN | status=start | mode=%s | force_full_download=%s",
        run_mode,
        force_full_download,
    )

    await _ensure_run_cache_dirs(active_cfg)
    shared_dir = paths.queue.parent
    prepare_state_directory(shared_dir)
    lock = StateLock(paths.lock)
    lock.acquire()
    try:
        replay_started = time.perf_counter()
        recovered = replay_journal(paths)
        logger.info(
            "RUN | step=2/4 | timing=journal_replay_startup | duration_sec=%.6f | recovered=%s",
            time.perf_counter() - replay_started,
            recovered,
        )
        await _run_main_locked(
            active_cfg,
            update_asset_bundle_info_only,
            force_full_download,
            mode,
            extracted_dir_is_temporary,
            paths,
        )
    finally:
        lock.release()


async def _run_main_locked(
    cfg: ConfigLike,
    update_asset_bundle_info_only: bool,
    force_full_download: bool,
    mode: str,
    extracted_dir_is_temporary: bool,
    paths: StatePaths,
) -> None:
    start_time = time.monotonic()
    automatic_prefixes = get_required_bundle_prefixes(mode, cfg)
    headers, cookie = await build_request_headers(cfg)

    if force_full_download:
        logger.info("RUN | option=force_full_download | cache_metadata=false | cache_pending=false")

    logger.info("RUN | step=1/4 | action=fetch_metadata")
    fetch_result = await fetch_asset_bundle_info(cfg, headers=headers, cookie=cookie)
    logger.info(
        "RUN | action=metadata_fetched | asset_ver=%s | bundle_count=%d",
        fetch_result.asset_ver,
        len(fetch_result.asset_bundle_info.get("bundles", {})),
    )

    if update_asset_bundle_info_only:
        await _write_metadata_only_cache(
            cfg,
            mode,
            automatic_prefixes,
            fetch_result.asset_bundle_info,
            fetch_result.game_version_json,
            start_time,
            paths,
        )
        return

    await _run_full_download_pipeline(
        cfg,
        mode,
        force_full_download,
        extracted_dir_is_temporary,
        automatic_prefixes,
        fetch_result,
        start_time,
        paths=paths,
    )


async def _run_charts_with_shared_lock(
    cfg: ConfigLike, *, extracted_dir_is_temporary: bool = False
):
    """Run queue-free Charts work while holding the legacy state-region lock."""
    paths = derive_state_paths(cfg.DL_LIST_CACHE_PATH)
    await ensure_dir_exists(cfg.DL_LIST_CACHE_PATH.parent)
    prepare_state_directory(paths.queue.parent)
    lock = StateLock(paths.lock)
    lock.acquire()
    try:
        return await run_specialized_postprocess(
            "charts",
            cfg,
            extracted_dir_is_temporary=extracted_dir_is_temporary,
        )
    finally:
        lock.release()


async def main(
    update_asset_bundle_info_only: bool = False,
    force_full_download: bool = False,
    mode: str = "assets",
):
    """Run the updater with only the specialized run-scoped storage it needs."""
    cfg = require_config()
    if not mode_uses_bundle_pipeline(mode):
        if update_asset_bundle_info_only:
            logger.info("RUN | result=noop | reason=metadata_only_not_applicable_to_charts")
            return

        needs_extracted_workspace = needs_shared_workspace(mode, cfg)
        if not needs_extracted_workspace:
            return await _run_charts_with_shared_lock(cfg)

    needs_extracted_workspace = needs_shared_workspace(mode, cfg)
    needs_live2d_cache = needs_live2d_bundle_cache(mode, cfg)
    if not needs_extracted_workspace and not needs_live2d_cache:
        return await _run_main(update_asset_bundle_info_only, force_full_download, mode)

    original_extracted_dir = cfg.ASSET_LOCAL_EXTRACTED_DIR
    original_live2d_dir = getattr(cfg, "LIVE2D_BUNDLE_CACHE_DIR", None)
    needs_run_temp_root = needs_extracted_workspace or needs_live2d_cache
    if not needs_run_temp_root:
        return await _run_charts_with_shared_lock(cfg)

    with tempfile.TemporaryDirectory(prefix="sekai-assets-") as temp_dir:
        from anyio import Path

        root = Path(temp_dir)
        if needs_live2d_cache:
            cfg.LIVE2D_BUNDLE_CACHE_DIR = root / "live2d-bundle"
        if needs_extracted_workspace:
            cfg.ASSET_LOCAL_EXTRACTED_DIR = root / "extracted"
        try:
            if mode == "charts":
                return await _run_charts_with_shared_lock(
                    cfg, extracted_dir_is_temporary=needs_extracted_workspace
                )
            return await _run_main(
                update_asset_bundle_info_only,
                force_full_download,
                mode,
                extracted_dir_is_temporary=needs_extracted_workspace,
            )
        finally:
            cfg.ASSET_LOCAL_EXTRACTED_DIR = original_extracted_dir
            cfg.LIVE2D_BUNDLE_CACHE_DIR = original_live2d_dir
