from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from anyio import Path as AnyioPath

from updater import music_selection, pipeline, state
from updater.cli import configuration, lifecycle, runner
from updater.music_selection import RegionalMusicSelection
from updater.net import plan as net_plan
from updater.postprocess import charts, dispatch
from updater.postprocess.incremental_state import load_chart_state


def _selection(
    music_ids: set[int] | frozenset[int] = frozenset(),
    vocals: set[str] | frozenset[str] = frozenset(),
    jackets: set[str] | frozenset[str] = frozenset(),
) -> RegionalMusicSelection:
    return RegionalMusicSelection(
        exclusive_music_ids=frozenset(music_ids),
        vocal_assetbundle_names=frozenset(vocals),
        jacket_assetbundle_names=frozenset(jackets),
    )


def _config(root: Path, *, selection=None, region: str = "TW", enabled: bool = True):
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
        REGION=SimpleNamespace(name=region),
        ENABLE_REGION_EXCLUSIVE_MUSIC=enabled,
        _regional_music_selection=selection,
    )


def _version():
    return {"appVersion": "1.0", "assetVersion": "2"}


def _metadata(bundles: dict[str, dict]):
    return {"version": "v2", "os": "ios", "bundles": bundles}


def _fetch_result(bundles: dict[str, dict]):
    return SimpleNamespace(
        headers={},
        cookie=None,
        game_version_json=_version(),
        asset_ver=None,
        assetbundle_host_hash=None,
        asset_bundle_info=_metadata(bundles),
    )


def _bundle(name: str, checksum: str = "same"):
    return {"bundleName": name, "hash": checksum}


def _mark_processed(bundle: dict) -> dict:
    return {
        **bundle,
        "_sekai_assets_updater": {"processed_checksum": {"field": "hash", "value": bundle["hash"]}},
    }


def _install_runner_fakes(monkeypatch, config, bundles, pipeline=None):
    monkeypatch.setattr(configuration, "config", config)

    async def fake_headers(_config):
        return {}, None

    async def fake_fetch(*_args, **_kwargs):
        return _fetch_result(bundles)

    monkeypatch.setattr(runner, "build_request_headers", fake_headers)
    monkeypatch.setattr(runner, "fetch_asset_bundle_info", fake_fetch)
    if pipeline is not None:
        monkeypatch.setattr(lifecycle, "run_pipeline", pipeline)


@pytest.fixture(autouse=True)
def _stub_regional_music_table_fetch(monkeypatch):
    """Keep integration coverage offline while exercising real preparation."""

    async def load_selection(config):
        selection = getattr(config, "_regional_music_selection", None)
        if selection is None:
            raise AssertionError("test config must inject a prepared regional policy")
        return selection

    monkeypatch.setattr(music_selection, "load_regional_music_selection", load_selection)


@pytest.mark.parametrize("force_full", [False, True], ids=["fresh", "force-full"])
def test_download_plan_hard_gates_required_music_after_regex_selection(
    tmp_path: Path, force_full: bool
) -> None:
    config = _config(
        tmp_path,
        selection=_selection({101}, {"vocal_101"}, {"jacket_s_101"}),
    )
    bundles = {
        "score-exclusive": _bundle("music/music_score/101_song"),
        "score-shared-required": _bundle("music/music_score/202_song"),
        "vocal-allowed-but-user-excluded": _bundle("music/short/vocal_101"),
        "jacket-exclusive": _bundle("music/jacket/jacket_s_101"),
        "non-music": _bundle("character/unit/common"),
    }
    selected = asyncio.run(
        net_plan.get_download_list(
            _metadata(bundles),
            _version(),
            config=config,
            include_list=[
                r"^music/music_score/101_song$",
                r"^music/short/vocal_101$",
                r"^music/jacket/jacket_s_101$",
                r"^character/",
            ],
            exclude_list=[r"^music/short/vocal_101$"],
            automatic_prefixes=("music/music_score/",),
            required_bundle_names=("music/music_score/202_song",),
            force_full_download=force_full,
        )
    )

    assert {bundle["bundleName"] for _, bundle in selected.candidates} == {
        "music/music_score/101_song",
        "music/jacket/jacket_s_101",
        "character/unit/common",
    }
    # The regional restriction is a hard gate even for required/automatic music;
    # user regex filters continue to select non-music and exclude normal items.
    assert set(selected.asset_metadata["bundles"]) == set(bundles)


def test_force_full_does_not_merge_old_shared_music_pending_into_pipeline(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, selection=_selection({101}))
    shared_song = "music/music_score/202_shared"
    stale_bundle = "character/old_pending"
    state.atomic_write_json(
        config.DL_LIST_CACHE_PATH,
        [
            ["https://example.test/old-shared", _bundle(shared_song, "old")],
            ["https://example.test/stale", _bundle(stale_bundle, "old")],
        ],
        state.validate_pending_queue,
    )
    bundles = {
        "music/music_score/101_song": _bundle("music/music_score/101_song"),
        shared_song: _bundle(shared_song),
        "character/unit/common": _bundle("character/unit/common"),
    }
    pipeline_runs: list[list[tuple[str, dict]]] = []

    async def capture_pipeline(items, *_args, **_kwargs):
        pipeline_runs.append(list(items))
        return [item for item in items if item[1]["bundleName"] == "character/unit/common"]

    _install_runner_fakes(monkeypatch, config, bundles, capture_pipeline)
    asyncio.run(runner.main(force_full_download=True))

    assert len(pipeline_runs) == 1
    assert {bundle["bundleName"] for _, bundle in pipeline_runs[0]} == {
        "music/music_score/101_song",
        "character/unit/common",
    }
    assert [
        item[1]["bundleName"] for item in state.load_pending_queue(config.DL_LIST_CACHE_PATH)
    ] == ["character/unit/common"]


def test_cn_partial_pipeline_failures_commit_only_completed_candidates_and_retry(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(
        tmp_path,
        selection=_selection({101}, {"vocal_101"}, {"jacket_101"}),
        region="CN",
    )
    download_failed = "music/music_score/101_cn_song"
    extract_failed = "music/short/vocal_101"
    upload_failed = "character/unit/common"
    succeeded = "music/jacket/jacket_101"
    shared = "music/music_score/202_shared"
    bundles = {
        name: _bundle(name, f"hash-{name}")
        for name in (download_failed, extract_failed, upload_failed, succeeded, shared)
    }
    config.ASSET_REMOTE_STORAGE = [
        {"type": "normal", "base": "remote:", "program": "fake", "args": []}
    ]

    fail_once = {
        ("download", download_failed),
        ("extract", extract_failed),
        ("upload", upload_failed),
    }
    injected_failures: list[tuple[str, str]] = []
    download_calls: list[str] = []
    extract_calls: list[str] = []
    upload_calls: list[str] = []

    def fail_stage_once(stage: str, bundle_name: str) -> None:
        failure = (stage, bundle_name)
        if failure in fail_once:
            fail_once.remove(failure)
            injected_failures.append(failure)
            raise RuntimeError(f"synthetic {stage} failure")

    async def fake_download(url, root, relative, **_kwargs):
        bundle_name = url.removeprefix("https://example.test/")
        download_calls.append(bundle_name)
        fail_stage_once("download", bundle_name)
        await root.joinpath(relative).write_bytes(bundle_name.encode())

    async def fake_extract(_bundle_path, bundle, output_root, **_kwargs):
        bundle_name = bundle["bundleName"]
        extract_calls.append(bundle_name)
        fail_stage_once("extract", bundle_name)
        output = output_root / "source.bin"
        await output.write_bytes(bundle_name.encode())
        return [output]

    async def fake_upload(files, _root, *_args, **_kwargs):
        bundle_name = (await files[0].read_bytes()).decode()
        upload_calls.append(bundle_name)
        fail_stage_once("upload", bundle_name)

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(pipeline, "upload_to_storage", fake_upload)
    _install_runner_fakes(monkeypatch, config, bundles)

    actual_run_pipeline = lifecycle.run_pipeline
    pipeline_failures: list[list[tuple[str, dict]]] = []

    async def capture_pipeline_failures(items, *args, **kwargs):
        failures = await actual_run_pipeline(items, *args, **kwargs)
        pipeline_failures.append(failures)
        return failures

    monkeypatch.setattr(lifecycle, "run_pipeline", capture_pipeline_failures)

    asyncio.run(runner.main(force_full_download=True))

    expected_failed_names = {download_failed, extract_failed, upload_failed}
    assert len(pipeline_failures) == 1
    assert {
        (url, bundle["bundleName"], bundle["hash"]) for url, bundle in pipeline_failures[0]
    } == {(f"https://example.test/{name}", name, f"hash-{name}") for name in expected_failed_names}
    assert set(injected_failures) == {
        ("download", download_failed),
        ("extract", extract_failed),
        ("upload", upload_failed),
    }
    paths = state.derive_state_paths(
        config.DL_LIST_CACHE_PATH,
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        config.GAME_VERSION_JSON_CACHE_PATH,
    )
    assert {
        bundle["bundleName"] for _, bundle in state.load_pending_queue(paths.queue)
    } == expected_failed_names
    partial_metadata = state.load_asset_metadata(paths.asset_metadata)
    assert partial_metadata["bundles"][succeeded]["_sekai_assets_updater"] == {
        "processed_checksum": {"field": "hash", "value": f"hash-{succeeded}"}
    }
    for name in expected_failed_names | {shared}:
        assert "_sekai_assets_updater" not in partial_metadata["bundles"][name]

    first_run_downloads = set(download_calls)
    assert first_run_downloads == {download_failed, extract_failed, upload_failed, succeeded}
    assert len(download_calls) == len(first_run_downloads)
    assert shared not in first_run_downloads
    first_run_download_count = len(download_calls)

    asyncio.run(runner.main())

    assert len(pipeline_failures) == 2
    assert pipeline_failures[1] == []
    retried_downloads = download_calls[first_run_download_count:]
    assert set(retried_downloads) == expected_failed_names
    assert len(retried_downloads) == len(expected_failed_names)
    assert shared not in download_calls
    assert not paths.queue.exists()
    assert not paths.journal.exists()
    committed_metadata = state.load_asset_metadata(paths.asset_metadata)
    for name in {download_failed, extract_failed, upload_failed, succeeded}:
        assert committed_metadata["bundles"][name]["_sekai_assets_updater"] == {
            "processed_checksum": {"field": "hash", "value": f"hash-{name}"}
        }
    assert "_sekai_assets_updater" not in committed_metadata["bundles"][shared]
    assert state.load_game_version(paths.game_version) == _version()


@pytest.mark.parametrize(
    ("prior_markers", "force_full_download", "seed_pending"),
    [(False, False, True), (True, True, False)],
    ids=["fresh-candidates-with-pending", "same-checksum-old-markers"],
)
def test_partial_chart_failure_reselects_temporary_score_sources_on_retry(
    tmp_path: Path,
    monkeypatch,
    prior_markers: bool,
    force_full_download: bool,
    seed_pending: bool,
) -> None:
    score_a = "music/music_score/101_a"
    score_b = "music/music_score/102_b"
    bundles = {score_a: _bundle(score_a, "checksum-a"), score_b: _bundle(score_b, "checksum-b")}
    config = _config(tmp_path, selection=_selection({101, 102}), region="CN")
    config.ENABLE_CHARTS_POSTPROCESS = True
    mirror_root = tmp_path / "mirror"
    mirror_score_root = mirror_root / "music" / "music_score"
    for directory, source in (("101_a", "A"), ("102_b", "B")):
        score_file = mirror_score_root / directory / "expert.txt"
        score_file.parent.mkdir(parents=True, exist_ok=True)
        score_file.write_text(source)
    config.ASSET_REMOTE_STORAGE = [
        {"type": "normal", "base": str(mirror_root), "program": "fake", "args": []},
        {"type": "charts", "base": "charts:", "program": "fake", "args": []},
    ]

    cached_bundles = {
        name: _mark_processed(bundle) if prior_markers else dict(bundle)
        for name, bundle in bundles.items()
    }
    cached_metadata = _metadata(cached_bundles)
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH, cached_metadata, state.validate_asset_metadata
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )
    if seed_pending:
        state.atomic_write_json(
            config.DL_LIST_CACHE_PATH,
            [[f"https://example.test/{name}", bundle] for name, bundle in bundles.items()],
            state.validate_pending_queue,
        )

    fail_bundle_b_once = True
    download_calls: list[str] = []
    extracted_names: list[str] = []
    extracted_roots: list[Path] = []
    rendered_sources: list[str] = []
    chart_uploads: list[str] = []
    mirror_fetches: list[Path] = []

    async def fake_download(url, root, relative, **_kwargs):
        nonlocal fail_bundle_b_once
        name = url.removeprefix("https://example.test/")
        download_calls.append(name)
        if name == score_b and fail_bundle_b_once:
            fail_bundle_b_once = False
            raise RuntimeError("synthetic bundle B download failure")
        await root.joinpath(relative).write_bytes(name.encode())

    async def fake_extract(_bundle_path, bundle, output_root, **_kwargs):
        name = bundle["bundleName"]
        extracted_names.append(name)
        extracted_roots.append(Path(str(output_root)))
        directory = name.rsplit("/", 1)[1]
        source = "A" if name == score_a else "B"
        score_file = output_root / "music" / "music_score" / directory / "expert.txt"
        await score_file.parent.mkdir(parents=True, exist_ok=True)
        await score_file.write_text(source)
        return [score_file]

    async def fake_upload(*_args, **_kwargs):
        return None

    async def fake_chart_source_fetch(_config, extracted_root, *_args):
        target_root = Path(extracted_root) / "music" / "music_score"
        mirror_fetches.append(target_root)
        for source in mirror_score_root.rglob("*.txt"):
            target = target_root / source.relative_to(mirror_score_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())

    async def fake_get_list(_url):
        return [{"id": 101, "title": "A"}, {"id": 102, "title": "B"}]

    async def fake_render_chart(score_path, chart_path, *_args, **_kwargs):
        source = Path(score_path).read_text()
        rendered_sources.append(source)
        output = Path(chart_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"rendered:{source}")
        output.with_suffix(".png").write_bytes(b"png")

    async def capture_chart_upload(mode, source_dir, _config):
        assert mode == "charts"
        chart_uploads.extend(
            path.read_text() for path in Path(source_dir).rglob("*.svg") if path.is_file()
        )

    monkeypatch.setattr(pipeline, "download_deobfuscate_bundle", fake_download)
    monkeypatch.setattr(pipeline, "extract_asset_bundle", fake_extract)
    monkeypatch.setattr(pipeline, "upload_to_storage", fake_upload)
    monkeypatch.setattr(dispatch, "fetch_chart_sources_from_storage", fake_chart_source_fetch)
    monkeypatch.setattr(dispatch, "_upload_specialized_directory", capture_chart_upload)
    monkeypatch.setattr(charts, "get_list", fake_get_list)
    monkeypatch.setattr(charts, "render_chart", fake_render_chart)
    original_temporary_directory = tempfile.TemporaryDirectory

    def temporary_directory_in_test_root(*args, **kwargs):
        kwargs.setdefault("dir", tmp_path)
        return original_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", temporary_directory_in_test_root)
    _install_runner_fakes(monkeypatch, config, bundles)

    asyncio.run(runner.main(force_full_download=force_full_download))

    paths = state.derive_state_paths(
        config.DL_LIST_CACHE_PATH,
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        config.GAME_VERSION_JSON_CACHE_PATH,
    )
    assert [bundle["bundleName"] for _, bundle in state.load_pending_queue(paths.queue)] == [
        score_b
    ]
    partial_metadata = state.load_asset_metadata(paths.asset_metadata)
    assert all(
        "_sekai_assets_updater" not in partial_metadata["bundles"][name]
        for name in (score_a, score_b)
    )
    assert set(download_calls) == {score_a, score_b}
    assert extracted_names == [score_a]
    assert rendered_sources == []
    assert mirror_fetches == []
    assert extracted_roots and all(not root.exists() for root in extracted_roots)

    first_run_download_count = len(download_calls)
    asyncio.run(runner.main())

    retry_downloads = download_calls[first_run_download_count:]
    assert set(retry_downloads) == {score_a, score_b}
    assert len(retry_downloads) == 2
    assert set(extracted_names[1:]) == {score_a, score_b}
    assert set(rendered_sources) == {"A", "B"}
    assert set(chart_uploads) == {"rendered:A", "rendered:B"}
    assert mirror_fetches == []
    assert not paths.queue.exists()
    assert not paths.journal.exists()
    committed_metadata = state.load_asset_metadata(paths.asset_metadata)
    for name in (score_a, score_b):
        assert committed_metadata["bundles"][name]["_sekai_assets_updater"] == {
            "processed_checksum": {
                "field": "hash",
                "value": bundles[name]["hash"],
            }
        }
    chart_state = load_chart_state(tmp_path / "chart_state.json")
    assert chart_state is not None
    assert set(chart_state["scores"]) == {"101_a/expert.txt", "102_b/expert.txt"}


def test_old_pending_queue_is_rescreened_without_dropping_non_music_or_regional_vocals(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(
        tmp_path,
        selection=_selection({101}, {"vocal_101_long"}, {"jacket_s_101"}),
    )
    bundles = {
        "music/music_score/202_shared": _bundle("music/music_score/202_shared"),
        "music/long/vocal_101_long": _bundle("music/long/vocal_101_long"),
        "character/common": _bundle("character/common"),
    }
    queued = [[f"https://example.test/{name}", bundle] for name, bundle in bundles.items()]
    state.atomic_write_json(config.DL_LIST_CACHE_PATH, queued, state.validate_pending_queue)
    captured: list[list[tuple[str, dict]]] = []

    async def partial_pipeline(items, *_args, **_kwargs):
        captured.append(items)
        return items

    _install_runner_fakes(monkeypatch, config, bundles, partial_pipeline)
    asyncio.run(runner.main())

    assert len(captured) == 1
    names = [bundle["bundleName"] for _, bundle in captured[0]]
    assert "music/music_score/202_shared" not in names
    assert "music/long/vocal_101_long" in names
    assert "character/common" in names
    persisted = state.load_pending_queue(config.DL_LIST_CACHE_PATH)
    assert [bundle["bundleName"] for _, bundle in persisted] == names


def test_all_music_filtered_by_region_commits_a_successful_empty_run(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, selection=_selection())
    config.DL_INCLUDE_LIST = [r"^music/"]
    bundles = {
        "music/music_score/202_shared": _bundle("music/music_score/202_shared"),
        "music/short/vocal_shared": _bundle("music/short/vocal_shared"),
    }
    _install_runner_fakes(monkeypatch, config, bundles)
    asyncio.run(runner.main())

    committed = state.load_asset_metadata(config.ASSET_BUNDLE_INFO_CACHE_PATH)
    assert set(committed["bundles"]) == set(bundles)
    assert all("_sekai_assets_updater" not in bundle for bundle in committed["bundles"].values())
    assert not Path(str(config.DL_LIST_CACHE_PATH)).exists()


@pytest.mark.parametrize(
    ("region", "enabled"),
    [("TW", False), ("JP", True)],
    ids=["disabled-legacy", "japan-legacy"],
)
def test_disabled_or_jp_plan_keeps_legacy_music_selection(
    tmp_path: Path, region: str, enabled: bool
) -> None:
    config = _config(tmp_path, selection=None, region=region, enabled=enabled)
    if not enabled:
        # A missing legacy setting is defined to mean disabled.
        del config.ENABLE_REGION_EXCLUSIVE_MUSIC
    bundles = {"music/music_score/202_shared": _bundle("music/music_score/202_shared")}

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata(bundles),
            _version(),
            config=config,
            force_full_download=True,
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == [
        "music/music_score/202_shared"
    ]


def test_disabled_feature_keeps_empty_user_filter_error(tmp_path: Path) -> None:
    config = _config(tmp_path, selection=None, enabled=False)
    config.DL_INCLUDE_LIST = [r"^not-a-bundle$"]
    bundles = {"music/music_score/202_shared": _bundle("music/music_score/202_shared")}

    download_plan = net_plan.get_download_list(
        _metadata(bundles),
        _version(),
        config=config,
        include_list=config.DL_INCLUDE_LIST,
        force_full_download=True,
    )
    with pytest.raises(ValueError, match="No bundles found after filtering"):
        asyncio.run(download_plan)


def test_metadata_only_observation_does_not_change_success_provenance_or_queue(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(
        tmp_path,
        selection=_selection({101}, {"vocal_101"}, {"jacket_s_101"}),
    )
    old = _metadata(
        {
            "music/music_score/101_song": _mark_processed(
                _bundle("music/music_score/101_song", "old")
            ),
            "music/music_score/202_shared": _bundle("music/music_score/202_shared", "old"),
        }
    )
    state.atomic_write_json(config.ASSET_BUNDLE_INFO_CACHE_PATH, old, state.validate_asset_metadata)
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )
    queued = [["https://example.test/pending", _bundle("character/common")]]
    state.atomic_write_json(config.DL_LIST_CACHE_PATH, queued, state.validate_pending_queue)
    metadata_path = Path(str(config.ASSET_BUNDLE_INFO_CACHE_PATH))
    version_path = Path(str(config.GAME_VERSION_JSON_CACHE_PATH))
    queue_path = Path(str(config.DL_LIST_CACHE_PATH))
    before_metadata = metadata_path.read_bytes()
    before_version = version_path.read_bytes()
    before_queue = queue_path.read_bytes()
    observed = {
        "music/music_score/101_song": _bundle("music/music_score/101_song", "new"),
        "music/music_score/202_shared": _bundle("music/music_score/202_shared", "new"),
        "music/music_score/303_unprocessed": _bundle("music/music_score/303_unprocessed", "new"),
    }
    _install_runner_fakes(monkeypatch, config, observed)

    asyncio.run(runner.main(update_asset_bundle_info_only=True))

    assert metadata_path.read_bytes() == before_metadata
    assert version_path.read_bytes() == before_version
    assert queue_path.read_bytes() == before_queue
    observed_path = tmp_path / "metadata.observed.json"
    assert observed_path.exists()
    observation = state.load_asset_metadata(observed_path)
    assert all("_sekai_assets_updater" not in bundle for bundle in observation["bundles"].values())


def test_expanding_region_selection_downloads_unprocessed_same_hash_bundle_only(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, selection=_selection({101}))
    bundle_names = (
        "music/music_score/101_song",
        "music/music_score/202_song",
        "music/music_score/303_shared",
    )
    prior_bundles = {
        bundle_names[0]: _mark_processed(_bundle(bundle_names[0])),
        bundle_names[1]: _bundle(bundle_names[1]),
        bundle_names[2]: _bundle(bundle_names[2]),
    }
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        _metadata(prior_bundles),
        state.validate_asset_metadata,
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )
    fetched = {name: _bundle(name) for name in bundle_names}
    pipeline_runs: list[list[tuple[str, dict]]] = []

    async def successful_pipeline(items, *_args, **_kwargs):
        pipeline_runs.append(items)
        return []

    _install_runner_fakes(monkeypatch, config, fetched, successful_pipeline)

    # The initial narrow selection keeps the complete manifest but does not
    # invent a success marker for the excluded, unchanged 202 bundle.
    asyncio.run(runner.main())
    after_narrow = state.load_asset_metadata(config.ASSET_BUNDLE_INFO_CACHE_PATH)
    assert set(after_narrow["bundles"]) == set(bundle_names)
    assert "_sekai_assets_updater" not in after_narrow["bundles"][bundle_names[1]]

    config._regional_music_selection = _selection({101, 202})
    asyncio.run(runner.main())

    assert len(pipeline_runs) == 1
    assert [bundle["bundleName"] for _, bundle in pipeline_runs[0]] == [bundle_names[1]]
    after_expansion = state.load_asset_metadata(config.ASSET_BUNDLE_INFO_CACHE_PATH)
    assert set(after_expansion["bundles"]) == set(bundle_names)
    assert after_expansion["bundles"][bundle_names[1]]["_sekai_assets_updater"] == {
        "processed_checksum": {"field": "hash", "value": "same"}
    }
    assert "_sekai_assets_updater" not in after_expansion["bundles"][bundle_names[2]]


def test_regional_preparation_failure_stops_before_fetch_or_state_writes(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, selection=None)
    fetch_calls = 0

    async def fail_load(_config):
        raise RuntimeError("regional music source unavailable")

    async def should_not_fetch(*_args, **_kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        return _fetch_result({"music/music_score/101_song": _bundle("music/music_score/101_song")})

    monkeypatch.setattr(configuration, "config", config)
    monkeypatch.setattr(music_selection, "load_regional_music_selection", fail_load)
    monkeypatch.setattr(runner, "fetch_asset_bundle_info", should_not_fetch)
    run = runner.main()

    with pytest.raises(RuntimeError, match="regional music source unavailable"):
        asyncio.run(run)

    assert fetch_calls == 0
    assert not Path(str(config.DL_LIST_CACHE_PATH)).exists()
    assert not Path(str(config.ASSET_BUNDLE_INFO_CACHE_PATH)).exists()
    assert not Path(str(config.GAME_VERSION_JSON_CACHE_PATH)).exists()


def test_live2d_only_run_does_not_prepare_regional_music_selection(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, selection=None)
    config.ASSET_LOCAL_EXTRACTED_DIR = AnyioPath(tmp_path / "extracted")
    config.LIVE2D_BUNDLE_CACHE_DIR = AnyioPath(tmp_path / "live2d-cache")
    bundles = {"live2d/model/unit": _bundle("live2d/model/unit")}

    async def complete_without_postprocess(*_args, **_kwargs):
        return None

    _install_runner_fakes(monkeypatch, config, bundles)
    monkeypatch.setattr(runner, "_complete_with_download_list", complete_without_postprocess)

    # The autouse table-fetch stub raises if regional music preparation is
    # attempted without an injected selection.
    asyncio.run(runner.main(mode="live2d"))


def test_standalone_charts_preparation_failure_skips_remote_and_render(
    tmp_path: Path, monkeypatch
) -> None:
    config = _config(tmp_path, selection=None)
    monkeypatch.setattr(configuration, "config", config)

    async def fail_load(_config):
        raise RuntimeError("regional chart selection unavailable")

    monkeypatch.setattr(music_selection, "load_regional_music_selection", fail_load)
    fetch_sources = AsyncMock()
    render_charts = AsyncMock()
    monkeypatch.setattr(dispatch, "fetch_chart_sources_from_storage", fetch_sources)
    monkeypatch.setattr(dispatch, "_render_charts", render_charts)
    run = runner.main(mode="charts")

    with pytest.raises(RuntimeError, match="regional chart selection unavailable"):
        asyncio.run(run)

    fetch_sources.assert_not_awaited()
    render_charts.assert_not_awaited()


def _write_score(root: Path, directory: str, text: str = "# SUS") -> None:
    score = root / "music" / "music_score" / directory / "master.txt"
    score.parent.mkdir(parents=True, exist_ok=True)
    score.write_text(text, encoding="utf-8")


def test_charts_filter_local_shared_scores_and_rebuild_after_selection_expansion(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _write_score(workspace, "101_song", "exclusive")
    _write_score(workspace, "202_song", "shared")
    config = _config(
        tmp_path / "state",
        selection=_selection({101}),
    )
    config.ASSET_LOCAL_EXTRACTED_DIR = workspace
    config.CHART_DATA_SERVER = None
    config.CHART_JACKET_BASE_URL = None
    config.ASSET_REMOTE_STORAGE = []
    render_calls: list[list[str]] = []

    async def fake_render(
        _config, _workspace, _include_list=None, score_files=None, music_ids=None
    ):
        paths = list(score_files or [])
        render_calls.append(paths)
        return set(paths)

    async def fake_upload(*_args, **_kwargs):
        return None

    async def run_charts():
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(dispatch, "_render_charts", fake_render)
            monkeypatch.setattr(dispatch, "_upload_specialized_directory", fake_upload)
            await dispatch.run_specialized_postprocess("charts", config)

    asyncio.run(run_charts())
    assert render_calls == [["101_song/master.txt"]]

    # Expanding the policy invalidates prior incremental state; previously
    # rendered scores are rebuilt while the now-allowed local score is added.
    config._regional_music_selection = _selection({101, 202})
    asyncio.run(run_charts())
    assert render_calls[-1] == ["101_song/master.txt", "202_song/master.txt"]
    chart_state = load_chart_state(Path(str(config.DL_LIST_CACHE_PATH)).parent / "chart_state.json")
    assert set(chart_state["scores"]) == {
        "101_song/master.txt",
        "202_song/master.txt",
    }


def test_empty_chart_policy_does_not_fetch_fallback_for_shared_local_scores(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_score(workspace, "202_shared")
    config = _config(tmp_path / "state", selection=_selection())
    config.ASSET_LOCAL_EXTRACTED_DIR = workspace
    config.CHART_DATA_SERVER = None
    config.CHART_JACKET_BASE_URL = None
    config.ASSET_REMOTE_STORAGE = []

    async def no_fallback(*_args, **_kwargs):
        raise AssertionError("regional empty selection must not fetch remote chart sources")

    async def no_render(*_args, **_kwargs):
        raise AssertionError("regional empty selection must not render shared scores")

    async def run_charts():
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(dispatch, "fetch_chart_sources_from_storage", no_fallback)
            monkeypatch.setattr(dispatch, "_render_charts", no_render)
            await dispatch.run_specialized_postprocess("charts", config)

    asyncio.run(run_charts())


def test_scoped_chart_upload_stages_only_current_in_scope_score_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "extracted"
    allowed_svg = workspace / "charts" / "tw" / "0101" / "master.svg"
    shared_svg = workspace / "charts" / "tw" / "0202" / "master.svg"
    stale_svg = workspace / "charts" / "tw" / "0101" / "stale.svg"
    for path in (allowed_svg, shared_svg, stale_svg):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    config = _config(tmp_path / "state")
    config.ASSET_REMOTE_STORAGE = [
        {"type": "charts", "base": "dummy:charts", "program": "rclone", "args": []}
    ]
    staged: list[list[str]] = []

    async def inspect_staged_directory(mode, source_dir, actual_config):
        assert mode == "charts"
        assert actual_config is config
        staged.append(
            sorted(
                path.relative_to(source_dir).as_posix()
                for path in source_dir.rglob("*")
                if path.is_file()
            )
        )

    monkeypatch.setattr(dispatch, "_upload_specialized_directory", inspect_staged_directory)
    asyncio.run(
        dispatch._upload_scoped_chart_outputs(
            config,
            workspace,
            {"0101_song/master.txt": "a" * 64},
        )
    )

    assert staged == [["0101/master.svg"]]


def test_remote_chart_fallback_filters_shared_scores_in_temporary_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    persistent_root = tmp_path / "persistent-extracted"
    config = _config(tmp_path / "state", selection=_selection({101}))
    config.ASSET_LOCAL_EXTRACTED_DIR = persistent_root
    config.ASSET_REMOTE_STORAGE = []
    fetch_calls: list[tuple[Path, frozenset[int] | None]] = []
    render_calls: list[list[str]] = []

    async def fetch_sources(_config, source_root, _include_list=None, music_ids=None):
        source_root = Path(source_root)
        fetch_calls.append((source_root, music_ids))
        score_root = source_root / "music" / "music_score"
        for directory in ("101_song", "202_shared"):
            score = score_root / directory / "master.txt"
            score.parent.mkdir(parents=True, exist_ok=True)
            score.write_text("# SUS", encoding="utf-8")

    async def render_scores(
        _config, _source_root, _include_list=None, score_files=None, music_ids=None
    ):
        render_calls.append(list(score_files or []))
        return set(score_files or [])

    monkeypatch.setattr(dispatch, "fetch_chart_sources_from_storage", fetch_sources)
    monkeypatch.setattr(dispatch, "_render_charts", render_scores)
    asyncio.run(dispatch.run_specialized_postprocess("charts", config))

    assert len(fetch_calls) == 1
    source_root, fetched_music_ids = fetch_calls[0]
    assert source_root != persistent_root
    assert fetched_music_ids == frozenset({101})
    assert render_calls == [["101_song/master.txt"]]
    assert not persistent_root.exists()
