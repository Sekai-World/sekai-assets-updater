"""Legacy JSON cache I/O and pending download-queue building/merging."""

import logging
import time
from typing import Any, Dict, List, Tuple, cast

import orjson as json
from anyio import open_file

from updater.cli.configuration import require_config  # noqa: F401
from updater.model import ConfigLike
from updater.modes import (
    filter_bundles_for_mode,
    filter_download_items_for_mode,
    get_mode_bundle_prefixes,
)
from updater.net.plan import (
    DownloadPlan,
    dedupe_download_items,
    get_download_list,
)
from updater.state import (
    StateNotFoundError,
    load_pending_queue,
)
from updater.workspace import ensure_dir_exists, get_bundle_cache_path

logger = logging.getLogger("asset_updater")

DownloadItem = Tuple[str, Dict[str, Any]]


def _pending_items_outside_mode(items: List[DownloadItem], mode: str) -> List[DownloadItem]:
    """Keep pending entries for other bundle namespaces when rewriting the cache."""
    prefixes = get_mode_bundle_prefixes(mode)
    if not prefixes:
        return []
    return [item for item in items if not (item[1].get("bundleName") or "").startswith(prefixes)]


async def _write_json_cache(path, data) -> None:
    async with await open_file(path, "wb") as f:
        await f.write(json.dumps(data, option=json.OPT_INDENT_2))


async def _read_json_cache(path):
    async with await open_file(path, "r") as f:
        return json.loads(await f.read())


async def _ensure_run_cache_dirs(cfg: ConfigLike) -> None:
    await ensure_dir_exists(cfg.DL_LIST_CACHE_PATH.parent)
    await ensure_dir_exists(cfg.ASSET_BUNDLE_INFO_CACHE_PATH.parent)
    await ensure_dir_exists(cfg.GAME_VERSION_JSON_CACHE_PATH.parent)


async def _build_new_download_list(
    cfg: ConfigLike,
    mode: str,
    automatic_prefixes,
    asset_bundle_info: Dict[str, Any],
    game_version_json,
    asset_ver,
    assetbundle_host_hash,
    force_full_download: bool,
) -> tuple[List[DownloadItem], DownloadPlan]:
    logger.info("RUN | step=2/4 | action=build_download_list")
    build_started = time.perf_counter()
    # get_download_list applies the user filters and writes the metadata cache;
    # the mandatory mode scope is applied both before and after it so it cannot
    # be bypassed by a cached queue or a broad include expression.
    scoped_info = dict(asset_bundle_info)
    scoped_info["bundles"] = filter_bundles_for_mode(scoped_info.get("bundles", {}), mode)
    plan: DownloadPlan = await get_download_list(
        scoped_info,
        game_version_json,
        config=cfg,
        assetver=asset_ver,
        assetbundle_host_hash=assetbundle_host_hash,
        include_list=cfg.DL_INCLUDE_LIST,
        exclude_list=cfg.DL_EXCLUDE_LIST,
        priority_list=cfg.DL_PRIORITY_LIST,
        force_full_download=force_full_download,
        automatic_prefixes=automatic_prefixes,
        bundle_cache_path_resolver=lambda bundle: get_bundle_cache_path(cfg, bundle),
        asset_bundle_info_for_cache=asset_bundle_info,
    )
    new_download_list = filter_download_items_for_mode(plan.candidates, mode)
    logger.debug("New download candidates: %d item(s)", len(new_download_list))
    logger.info(
        "RUN | step=2/4 | timing=build_list | duration_sec=%.6f | candidates=%d",
        time.perf_counter() - build_started,
        len(new_download_list),
    )
    return new_download_list, plan


async def _load_pending_download_lists(
    cfg: ConfigLike,
    mode: str,
    force_full_download: bool,
) -> Tuple[List[DownloadItem], List[DownloadItem]]:
    """Load cached pending items and the subset belonging to the current mode."""
    pending_started = time.perf_counter()
    if not await cfg.DL_LIST_CACHE_PATH.exists():
        logger.info(
            "RUN | step=2/4 | timing=pending_load_merge | duration_sec=%.6f | loaded=0 | merged=0",
            time.perf_counter() - pending_started,
        )
        return [], []

    try:
        cached_pending_list = cast(List[DownloadItem], load_pending_queue(cfg.DL_LIST_CACHE_PATH))
    except StateNotFoundError:
        logger.info(
            "RUN | step=2/4 | timing=pending_load_merge | duration_sec=%.6f | loaded=0 | merged=0",
            time.perf_counter() - pending_started,
        )
        return [], []
    if force_full_download:
        logger.info(
            "RUN | step=2/4 | timing=pending_load_merge | duration_sec=%.6f | loaded=%d | merged=0 | force_full=true",
            time.perf_counter() - pending_started,
            len(cached_pending_list),
        )
        return cached_pending_list, []

    pending_list = filter_download_items_for_mode(cached_pending_list, mode)
    logger.info(
        "RUN | action=load_pending | count=%d | path=%s",
        len(pending_list),
        cfg.DL_LIST_CACHE_PATH,
    )
    logger.info(
        "RUN | step=2/4 | timing=pending_load_merge | duration_sec=%.6f | loaded=%d | merged=%d",
        time.perf_counter() - pending_started,
        len(cached_pending_list),
        len(pending_list),
    )
    return cached_pending_list, pending_list


def _merge_pending_and_new_download_lists(
    pending_list: List[DownloadItem],
    new_download_list: List[DownloadItem],
) -> List[DownloadItem]:
    """Merge pending retries ahead of new candidates without duplicates."""
    if pending_list and new_download_list:
        current_by_name = {
            bundle.get("bundleName"): (url, bundle) for url, bundle in new_download_list
        }
        ordered_pending = [
            current_by_name.get(bundle.get("bundleName"), (url, bundle))
            for url, bundle in pending_list
        ]
        pending_bundle_names = {bundle.get("bundleName") for _, bundle in ordered_pending}
        deduped_new = [
            item
            for item in new_download_list
            if item[1].get("bundleName") not in pending_bundle_names
        ]
        download_list = dedupe_download_items(ordered_pending + deduped_new)
        logger.info(
            "RUN | action=merge_download_list | pending=%d | new=%d | total=%d",
            len(pending_list),
            len(deduped_new),
            len(download_list),
        )
        return download_list
    if pending_list:
        logger.info("RUN | action=retry_pending_only | count=%d", len(pending_list))
        return pending_list
    return new_download_list
