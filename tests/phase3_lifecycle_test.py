from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio import Path as AnyioPath

import main
from updater import state
from updater.net import plan as net_plan


def _metadata(name: str = "bundle", checksum: str = "hash") -> dict:
    return {
        "version": "asset-v1",
        "os": "ios",
        "bundles": {name: {"bundleName": name, "hash": checksum}},
    }


def _version(assetver: str = "asset-1") -> dict:
    return {"appVersion": "1.0", "assetVersion": "2", "assetver": assetver}


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


def _paths(root: Path) -> state.StatePaths:
    return state.derive_state_paths(root / "dl.json", root / "metadata.json", root / "version.json")


def _write_generation(paths: state.StatePaths, queue, metadata, version) -> None:
    state.atomic_write_json(paths.queue, queue, state.validate_pending_queue)
    state.atomic_write_json(paths.asset_metadata, metadata, state.validate_asset_metadata)
    state.atomic_write_json(paths.game_version, version, state.validate_game_version)


@pytest.mark.parametrize("boundary", ["published", "queue", "metadata", "version"])
def test_replay_restores_generation_after_each_commit_boundary(
    tmp_path: Path, boundary: str
) -> None:
    root = tmp_path / boundary
    root.mkdir()
    paths = _paths(root)
    queue = [["https://example.test/bundle", {"bundleName": "bundle", "hash": "new"}]]
    metadata = _metadata()
    version = _version()
    state.create_journal(paths, queue, metadata, version, transaction_id=boundary)

    if boundary in {"queue", "metadata", "version"}:
        state.atomic_write_json(paths.queue, queue, state.validate_pending_queue)
    if boundary in {"metadata", "version"}:
        state.atomic_write_json(paths.asset_metadata, metadata, state.validate_asset_metadata)
    if boundary == "version":
        state.atomic_write_json(paths.game_version, version, state.validate_game_version)

    assert paths.journal.exists()
    assert state.replay_journal(paths)
    assert state.load_pending_queue(paths.queue) == queue
    assert state.load_asset_metadata(paths.asset_metadata) == metadata
    assert state.load_game_version(paths.game_version) == version
    assert not paths.journal.exists()


def test_current_invalid_network_version_remains_a_commit_error(
    tmp_path: Path, monkeypatch
) -> None:
    """Compatibility handling applies only to prior cache reads, never fetched data."""
    config = _config(tmp_path)
    monkeypatch.setattr(main, "config", config)
    invalid_version = {"appVersion": "1.0", "assetVersion": 2}
    fetch_result = SimpleNamespace(
        headers={},
        cookie=None,
        game_version_json=invalid_version,
        asset_ver="fetched",
        assetbundle_host_hash=None,
        asset_bundle_info=_metadata(),
    )

    async def fake_fetch(*_args, **_kwargs):
        return fetch_result

    async def fake_plan(*_args, **_kwargs):
        return net_plan.DownloadPlan([], _metadata(), invalid_version)

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    monkeypatch.setattr(main, "get_download_list", fake_plan)

    with pytest.raises(state.StateValidationError, match="assetVersion"):
        asyncio.run(main.main(force_full_download=True))


def test_empty_calculated_queue_commits_checkpoints_then_leaves_no_pending_queue(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(main, "config", config)
    fetch_result = SimpleNamespace(
        headers={},
        cookie=None,
        game_version_json=_version("fetched"),
        asset_ver="fetched",
        assetbundle_host_hash=None,
        asset_bundle_info=_metadata(),
    )

    async def fake_fetch(*_args, **_kwargs):
        return fetch_result

    async def fake_plan(*_args, **_kwargs):
        return net_plan.DownloadPlan([], _metadata(), _version("fetched"))

    async def unexpected_pipeline(*_args, **_kwargs):
        raise AssertionError("empty calculated queue must not start pipeline")

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    monkeypatch.setattr(main, "get_download_list", fake_plan)
    monkeypatch.setattr(main, "run_pipeline", unexpected_pipeline)
    asyncio.run(main.main(force_full_download=True))

    paths = _paths(tmp_path)
    assert state.load_asset_metadata(paths.asset_metadata) == _metadata()
    assert state.load_game_version(paths.game_version) == _version("fetched")
    assert not paths.queue.exists()
    assert not paths.journal.exists()


def test_success_clears_queue_only_after_pipeline_success(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(main, "config", config)
    seen_queue_exists: list[bool] = []

    async def fake_fetch(*_args, **_kwargs):
        return SimpleNamespace(
            headers={},
            cookie=None,
            game_version_json=_version(),
            asset_ver=None,
            assetbundle_host_hash=None,
            asset_bundle_info=_metadata(),
        )

    async def fake_pipeline(*_args, **_kwargs):
        seen_queue_exists.append(_paths(tmp_path).queue.exists())
        return []

    async def verify_postprocess_after_queue_cleanup(*_args, **_kwargs):
        assert not _paths(tmp_path).queue.exists()

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    monkeypatch.setattr(main, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(
        main, "_run_enabled_specialized_postprocess", verify_postprocess_after_queue_cleanup
    )
    asyncio.run(main.main(force_full_download=True))
    assert seen_queue_exists == [True]
    assert not _paths(tmp_path).queue.exists()


def test_specialized_failure_does_not_restore_successful_download_queue(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    paths = _paths(tmp_path)
    download_list = [("url", {"bundleName": "bundle", "hash": "stable"})]

    async def successful_download(*_args, **_kwargs):
        return True

    async def failing_postprocess(*_args, **_kwargs):
        assert not paths.queue.exists()
        raise FileNotFoundError("live2d/model is unavailable")

    monkeypatch.setattr(main, "do_download", successful_download)
    monkeypatch.setattr(main, "_run_enabled_specialized_postprocess", failing_postprocess)

    completion = main._complete_with_download_list(
        config,
        "assets",
        {},
        None,
        download_list,
        [],
        True,
        0,
        paths,
    )
    with pytest.raises(FileNotFoundError, match="live2d/model"):
        asyncio.run(completion)

    assert not paths.queue.exists()


def test_partial_failure_persists_exact_failed_subset(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    paths = _paths(tmp_path)
    original = [
        ["one-url", {"bundleName": "one", "hash": "one"}],
        ["two-url", {"bundleName": "two", "hash": "two"}],
    ]
    state.atomic_write_json(paths.queue, original, state.validate_pending_queue)
    failed = [("two-url", {"bundleName": "two", "hash": "two"})]

    async def fake_pipeline(*_args, **_kwargs):
        return failed

    monkeypatch.setattr(main, "run_pipeline", fake_pipeline)
    asyncio.run(main.do_download(original, config, {}, None, paths))  # type: ignore[arg-type]
    assert state.load_pending_queue(paths.queue) == [list(failed[0])]


@pytest.mark.parametrize("failure", [RuntimeError("crash"), asyncio.CancelledError()])
def test_unexpected_pipeline_failure_retains_full_pre_run_queue(
    tmp_path: Path, monkeypatch, failure: BaseException
) -> None:
    config = _config(tmp_path)
    paths = _paths(tmp_path)
    original = [["url", {"bundleName": "bundle", "hash": "stable"}]]
    state.atomic_write_json(paths.queue, original, state.validate_pending_queue)
    before = paths.queue.read_bytes()

    async def fake_pipeline(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(main, "run_pipeline", fake_pipeline)
    download = main.do_download(original, config, {}, None, paths)  # type: ignore[arg-type]
    with pytest.raises(type(failure)):
        asyncio.run(download)
    assert paths.queue.read_bytes() == before


def test_malformed_queue_without_journal_fails_closed_and_preserves_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(main, "config", config)
    paths = _paths(tmp_path)
    paths.queue.write_bytes(b"malformed pending queue")
    before = paths.queue.read_bytes()

    async def fake_fetch(*_args, **_kwargs):
        return SimpleNamespace(
            headers={},
            cookie=None,
            game_version_json=_version(),
            asset_ver=None,
            assetbundle_host_hash=None,
            asset_bundle_info=_metadata(),
        )

    async def fake_plan(*_args, **_kwargs):
        return net_plan.DownloadPlan([], _metadata(), _version())

    monkeypatch.setattr(main, "fetch_asset_bundle_info", fake_fetch)
    monkeypatch.setattr(main, "get_download_list", fake_plan)
    run = main.main()
    with pytest.raises(state.StateValidationError):
        asyncio.run(run)
    assert paths.queue.read_bytes() == before
    assert not paths.journal.exists()


def test_valid_journal_supersedes_corrupt_queue_metadata_and_version(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    queue = [["journal-url", {"bundleName": "bundle", "hash": "journal"}]]
    metadata = _metadata(checksum="journal")
    version = _version("journal")
    state.create_journal(paths, queue, metadata, version, transaction_id="authoritative")
    paths.queue.write_bytes(b"corrupt queue")
    paths.asset_metadata.write_bytes(b"corrupt metadata")
    paths.game_version.write_bytes(b"corrupt version")

    assert state.replay_journal(paths)
    assert state.load_pending_queue(paths.queue) == queue
    assert state.load_asset_metadata(paths.asset_metadata) == metadata
    assert state.load_game_version(paths.game_version) == version
    assert not paths.journal.exists()
