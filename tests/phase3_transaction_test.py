from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio import Path as AnyioPath

import main
from updater import helpers, state


def _metadata(name: str = "current", checksum: str = "new"):
    return {
        "version": "v2",
        "os": "ios",
        "bundles": {name: {"bundleName": name, "hash": checksum}},
    }


def _version():
    return {"appVersion": "1.0", "assetVersion": "2"}


def _config(root: Path) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        DL_LIST_CACHE_PATH=AnyioPath(root / "dl.json"),
        ASSET_BUNDLE_INFO_CACHE_PATH=AnyioPath(root / "metadata.json"),
        GAME_VERSION_JSON_CACHE_PATH=AnyioPath(root / "version.json"),
        DL_INCLUDE_LIST=None,
        DL_EXCLUDE_LIST=None,
        DL_PRIORITY_LIST=None,
        ASSET_BUNDLE_URL="https://example.test/{bundleName}",
        APP_VERSION_OVERRIDE=None,
        REQUEST_TIMEOUT=1,
        MAX_CONCURRENCY_DOWNLOADS=1,
        MAX_CONCURRENCY_EXTRACTS=1,
        MAX_CONCURRENCY_UPLOAD_STAGE=1,
        PIPELINE_STAGE_QUEUE_SIZE=1,
        MAX_CONCURRENCY_UPLOADS=1,
        ASSET_REMOTE_STORAGE=[],
        ASSET_LOCAL_BUNDLE_CACHE_DIR=None,
        ASSET_LOCAL_EXTRACTED_DIR=None,
        UNITY_VERSION=None,
        USER_AGENT=None,
        GAME_COOKIE_URL=None,
    )


def _fetch_result():
    return SimpleNamespace(
        headers={},
        cookie=None,
        game_version_json=_version(),
        asset_ver=None,
        assetbundle_host_hash=None,
        asset_bundle_info=_metadata(),
    )


def test_download_selection_is_persistence_free(tmp_path: Path) -> None:
    config = _config(tmp_path)

    plan = asyncio.run(
        helpers.get_download_list(
            _metadata(),
            _version(),
            config=config,
            force_full_download=True,
        )
    )
    assert isinstance(plan, helpers.DownloadPlan)
    assert plan.candidates[0][1]["bundleName"] == "current"
    assert not (tmp_path / "metadata.json").exists()
    assert not (tmp_path / "version.json").exists()


@pytest.mark.parametrize("cached_assetver", ["same", "old"])
def test_download_plan_retains_fetched_assetver_even_when_cache_differs_or_matches(
    tmp_path: Path, cached_assetver: str
) -> None:
    config = _config(tmp_path)
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        _metadata("current", "old"),
        state.validate_asset_metadata,
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH,
        {"appVersion": "1.0", "assetver": cached_assetver},
        state.validate_game_version,
    )
    plan = asyncio.run(
        helpers.get_download_list(
            _metadata("current", "new"),
            _version(),
            config=config,
            assetver="same",
        )
    )
    assert plan.game_version["assetver"] == "same"


def test_main_commits_journal_targets_before_pipeline(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(main, "config", config)
    events: list[str] = []
    original_create = main.create_journal
    original_replay = main.replay_journal

    async def fake_fetch(*_args, **_kwargs):
        return _fetch_result()

    async def fake_pipeline(*_args, **_kwargs):
        events.append("pipeline")
        paths = state.derive_state_paths(
            config.DL_LIST_CACHE_PATH,
            config.ASSET_BUNDLE_INFO_CACHE_PATH,
            config.GAME_VERSION_JSON_CACHE_PATH,
        )
        assert not paths.journal.exists()
        return []

    def create(*args, **kwargs):
        events.append("journal")
        return original_create(*args, **kwargs)

    def replay(*args, **kwargs):
        result = original_replay(*args, **kwargs)
        events.append("replay")
        return result

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    monkeypatch.setattr(main, "create_journal", create)
    monkeypatch.setattr(main, "replay_journal", replay)
    monkeypatch.setattr(main, "run_pipeline", fake_pipeline)

    asyncio.run(main.main(force_full_download=True))

    assert events == ["replay", "journal", "replay", "pipeline"]
    assert not (tmp_path / "dl.json").exists()
    assert state.load_asset_metadata(tmp_path / "metadata.json") == _metadata()
    assert state.load_game_version(tmp_path / "version.json") == _version()


def test_empty_download_plan_commits_metadata_without_journal_replay(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(main, "config", config)
    events: list[str] = []

    async def fake_fetch(*_args, **_kwargs):
        return _fetch_result()

    async def fake_plan(*_args, **_kwargs):
        return helpers.DownloadPlan([], _metadata("fresh", "new"), _version())

    original_replay = main.replay_journal

    def replay(*args, **kwargs):
        events.append("replay")
        return original_replay(*args, **kwargs)

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    monkeypatch.setattr(main, "get_download_list", fake_plan)
    monkeypatch.setattr(main, "replay_journal", replay)
    asyncio.run(main.main())

    assert events == ["replay"]  # startup recovery only
    paths = state.derive_state_paths(
        config.DL_LIST_CACHE_PATH,
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        config.GAME_VERSION_JSON_CACHE_PATH,
    )
    assert state.load_asset_metadata(paths.asset_metadata) == _metadata("fresh", "new")
    assert state.load_game_version(paths.game_version) == _version()
    assert not paths.queue.exists()
    assert not paths.journal.exists()


def test_empty_transaction_failure_retains_journal_for_recovery(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    paths = state.derive_state_paths(
        config.DL_LIST_CACHE_PATH,
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        config.GAME_VERSION_JSON_CACHE_PATH,
    )
    original_write = state.atomic_write_json
    writes = 0

    def fail_after_queue(path, data, validator):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise state.StatePersistenceError("simulated metadata failure")
        return original_write(path, data, validator)

    monkeypatch.setattr(state, "atomic_write_json", fail_after_queue)
    with pytest.raises(state.StatePersistenceError):
        state.commit_empty_transaction(paths, _metadata("recovered", "fresh"), _version())
    assert paths.journal.exists()

    monkeypatch.setattr(state, "atomic_write_json", original_write)
    assert state.replay_journal(paths)
    assert state.load_asset_metadata(paths.asset_metadata) == _metadata("recovered", "fresh")
    assert state.load_game_version(paths.game_version) == _version()
    assert not paths.journal.exists()


def test_startup_replay_is_authoritative_before_fetch(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(main, "config", config)
    paths = state.derive_state_paths(
        config.DL_LIST_CACHE_PATH,
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        config.GAME_VERSION_JSON_CACHE_PATH,
    )
    queue = [["old-url", {"bundleName": "old", "hash": "old"}]]
    state.create_journal(paths, queue, _metadata("old", "old"), _version(), "recovery")
    fetched = False

    async def fake_fetch(*_args, **_kwargs):
        nonlocal fetched
        fetched = True
        return _fetch_result()

    async def fake_plan(*_args, **_kwargs):
        return helpers.DownloadPlan([], _metadata(), _version())

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    monkeypatch.setattr(main, "get_download_list", fake_plan)
    monkeypatch.setattr(
        main, "do_download", lambda *_args, **_kwargs: asyncio.sleep(0, result=True)
    )
    asyncio.run(main.main())
    assert fetched
    assert not paths.queue.exists()


@pytest.mark.parametrize("failure", ["exception", "cancel"])
def test_pipeline_exception_or_cancellation_preserves_complete_queue(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    config = _config(tmp_path)
    paths = state.derive_state_paths(
        config.DL_LIST_CACHE_PATH,
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        config.GAME_VERSION_JSON_CACHE_PATH,
    )
    queue = [["url", {"bundleName": "current", "hash": "new"}]]
    state.atomic_write_json(paths.queue, queue, state.validate_pending_queue)
    monkeypatch.setattr(main, "config", config)

    async def fake_fetch(*_args, **_kwargs):
        return _fetch_result()

    async def fake_pipeline(*_args, **_kwargs):
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("pipeline crash")

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    monkeypatch.setattr(main, "run_pipeline", fake_pipeline)
    run = main.main()
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        asyncio.run(run)
    assert state.load_pending_queue(paths.queue) == [
        ["https://example.test/current", {"bundleName": "current", "hash": "new"}]
    ]


def test_partial_failure_replaces_queue_and_success_deletes_queue(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(main, "config", config)
    paths = state.derive_state_paths(
        config.DL_LIST_CACHE_PATH,
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        config.GAME_VERSION_JSON_CACHE_PATH,
    )

    async def fake_fetch(*_args, **_kwargs):
        return _fetch_result()

    async def failed_pipeline(*_args, **_kwargs):
        return [("url", {"bundleName": "current", "hash": "new"})]

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    monkeypatch.setattr(main, "run_pipeline", failed_pipeline)
    asyncio.run(main.main(force_full_download=True))
    assert state.load_pending_queue(paths.queue) == [
        ["url", {"bundleName": "current", "hash": "new"}]
    ]

    async def successful_pipeline(*_args, **_kwargs):
        return []

    monkeypatch.setattr(main, "run_pipeline", successful_pipeline)
    asyncio.run(main.main())
    assert not paths.queue.exists()


def test_metadata_only_uses_observed_siblings_without_mutating_normal_targets(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(main, "config", config)
    paths = state.derive_state_paths(
        config.DL_LIST_CACHE_PATH,
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        config.GAME_VERSION_JSON_CACHE_PATH,
    )
    normal_metadata = _metadata("normal", "stable")
    normal_version = _version()
    state.atomic_write_json(paths.asset_metadata, normal_metadata, state.validate_asset_metadata)
    state.atomic_write_json(paths.game_version, normal_version, state.validate_game_version)
    before_metadata = paths.asset_metadata.read_bytes()
    before_version = paths.game_version.read_bytes()

    async def fake_fetch(*_args, **_kwargs):
        result = _fetch_result()
        result.asset_bundle_info = _metadata("observed", "fresh")
        return result

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    asyncio.run(main.main(update_asset_bundle_info_only=True))
    assert paths.asset_metadata.read_bytes() == before_metadata
    assert paths.game_version.read_bytes() == before_version
    assert (tmp_path / "metadata.observed.json").exists()
    assert (tmp_path / "version.observed.json").exists()


def test_metadata_only_rejects_observed_path_aliasing_normal_target(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    config.DL_LIST_CACHE_PATH = AnyioPath(tmp_path / "metadata.observed.json")
    config.ASSET_BUNDLE_INFO_CACHE_PATH = AnyioPath(tmp_path / "metadata.json")
    monkeypatch.setattr(main, "config", config)

    async def fake_fetch(*_args, **_kwargs):
        return _fetch_result()

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    run = main.main(update_asset_bundle_info_only=True)
    with pytest.raises(RuntimeError, match="aliases"):
        asyncio.run(run)


def test_lock_contention_blocks_second_main_run(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    paths = state.derive_state_paths(
        config.DL_LIST_CACHE_PATH,
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        config.GAME_VERSION_JSON_CACHE_PATH,
    )
    holder = state.StateLock(paths.lock).acquire()
    try:
        monkeypatch.setattr(main, "config", config)
        run = main.main(force_full_download=True)
        with pytest.raises(state.StateLockError, match="already held"):
            asyncio.run(run)
    finally:
        holder.release()


def test_charts_uses_legacy_lock_without_replaying_journal_and_releases(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    config.ASSET_LOCAL_EXTRACTED_DIR = AnyioPath(tmp_path / "extracted")
    paths = state.derive_state_paths(config.DL_LIST_CACHE_PATH)
    state.create_journal(paths, [], _metadata(), _version(), "must-not-replay")
    monkeypatch.setattr(main, "config", config)

    async def fake_postprocess(*_args, **_kwargs):
        with pytest.raises(state.StateLockError, match="already held"):
            state.StateLock(paths.lock).acquire()

    monkeypatch.setattr(main, "run_specialized_postprocess", fake_postprocess)
    asyncio.run(main.main(mode="charts"))

    assert paths.journal.exists()
    released = state.StateLock(paths.lock).acquire()
    released.release()
