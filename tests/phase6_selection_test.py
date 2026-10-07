from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio import Path as AnyioPath

from updater import state
from updater.model import SekaiServerRegion
from updater.net import metadata as asset_bundle_info
from updater.net import plan as net_plan
from updater.net import urls as net_urls


def _config(root: Path, *, url: str = "https://cdn.test/{bundleName}") -> SimpleNamespace:
    return SimpleNamespace(
        ASSET_BUNDLE_INFO_CACHE_PATH=AnyioPath(root / "metadata.json"),
        GAME_VERSION_JSON_CACHE_PATH=AnyioPath(root / "version.json"),
        ASSET_BUNDLE_URL=url,
        APP_VERSION_OVERRIDE=None,
    )


def _metadata(*bundles: dict) -> dict:
    return {
        "version": "v1",
        "os": "ios",
        "bundles": {bundle["bundleName"]: bundle for bundle in bundles},
    }


def _marked(bundle: dict, *, field: str = "hash", value: str | None = None) -> dict:
    if value is None:
        value = str(bundle[field])
    return {
        **bundle,
        "_sekai_assets_updater": {"processed_checksum": {"field": field, "value": value}},
    }


def _version(assetver: str = "asset-1") -> dict:
    return {"appVersion": "1.0", "assetVersion": "2", "assetver": assetver}


def test_priority_patterns_follow_declared_order_and_unmatched_tail() -> None:
    candidates = [
        ("url-zeta", {"bundleName": "zeta"}),
        ("url-character", {"bundleName": "character/member"}),
        ("url-music", {"bundleName": "music/song"}),
        ("url-character-2", {"bundleName": "character/motion"}),
    ]

    result = net_plan.sort_download_list(
        candidates,
        priority_list=[r"^music/", r"^character/"],
    )

    assert [bundle["bundleName"] for _, bundle in result] == [
        "music/song",
        "character/member",
        "character/motion",
        "zeta",
    ]


def test_nuverse_checksum_change_is_downloaded_when_assetver_is_unchanged(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        _metadata({"bundleName": "changed", "hash": "old"}),
        state.validate_asset_metadata,
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH,
        _version("same-assetver"),
        state.validate_game_version,
    )

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata({"bundleName": "changed", "hash": "new"}),
            _version(),
            config=config,
            assetver="same-assetver",
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == ["changed"]


def test_nuverse_assetver_change_alone_does_not_redownload(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        _metadata(_marked({"bundleName": "stable", "hash": "same"})),
        state.validate_asset_metadata,
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH,
        _version("old-assetver"),
        state.validate_game_version,
    )

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata({"bundleName": "stable", "hash": "same"}),
            _version(),
            config=config,
            assetver="new-assetver",
        )
    )

    assert plan.candidates == []


def test_newly_included_unchanged_bundle_is_queued_without_local_cache_resolver(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    target = "music/short/vs_0807_01"
    other = "music/short/other"
    cached_metadata = _metadata(
        {"bundleName": target, "hash": "same"},
        _marked({"bundleName": other, "hash": "same"}),
    )
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH, cached_metadata, state.validate_asset_metadata
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata(
                {"bundleName": target, "hash": "same"},
                {"bundleName": other, "hash": "same"},
            ),
            _version(),
            config=config,
            include_list=[rf"^{target}$"],
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == [target]
    assert plan.successful_asset_metadata["bundles"][target]["_sekai_assets_updater"] == {
        "processed_checksum": {"field": "hash", "value": "same"}
    }
    assert set(plan.asset_metadata) == {"version", "os", "bundles"}


def test_unknown_bundle_queues_even_when_raw_cache_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = "music/short/vs_0807_01"
    other = "music/short/other"
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        _metadata(
            {"bundleName": target, "hash": "same"}, _marked({"bundleName": other, "hash": "same"})
        ),
        state.validate_asset_metadata,
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )
    cache_dir = tmp_path / "bundle-cache"
    cache_dir.mkdir()
    (cache_dir / target.replace("/", "_")).write_bytes(b"cached")

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata(
                {"bundleName": target, "hash": "same"},
                _marked({"bundleName": other, "hash": "same"}),
            ),
            _version(),
            config=config,
            include_list=[rf"^{target}$"],
            bundle_cache_path_resolver=lambda bundle: (
                cache_dir / bundle["bundleName"].replace("/", "_")
            ),
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == [target]


@pytest.mark.parametrize(
    ("cache_exists", "expected_candidates"),
    [(True, []), (False, ["music/short/vs_0807_01"])],
)
def test_known_current_bundle_respects_local_cache(
    tmp_path: Path,
    cache_exists: bool,
    expected_candidates: list[str],
) -> None:
    config = _config(tmp_path)
    target = "music/short/vs_0807_01"
    other = "music/short/other"
    cached_metadata = _metadata(
        _marked({"bundleName": target, "hash": "same"}),
        _marked({"bundleName": other, "hash": "same"}),
    )
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH, cached_metadata, state.validate_asset_metadata
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )

    cache_dir = tmp_path / "bundle-cache"
    cache_dir.mkdir()
    cache_path = cache_dir / target.replace("/", "_")
    if cache_exists:
        cache_path.write_bytes(b"cached")

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata(
                {"bundleName": target, "hash": "same"},
                {"bundleName": other, "hash": "same"},
            ),
            _version(),
            config=config,
            include_list=[rf"^{target}$"],
            bundle_cache_path_resolver=lambda bundle: (
                cache_dir / bundle["bundleName"].replace("/", "_")
            ),
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == expected_candidates


def test_relaxed_exclude_queues_unchanged_newly_selected_bundle(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = "music/short/vs_0807_01"
    other = "music/short/other"
    cached_metadata = _metadata(
        {"bundleName": target, "hash": "same"},
        _marked({"bundleName": other, "hash": "same"}),
    )
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH, cached_metadata, state.validate_asset_metadata
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata(
                {"bundleName": target, "hash": "same"},
                {"bundleName": other, "hash": "same"},
            ),
            _version(),
            config=config,
            exclude_list=[],
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == [target]


def test_checksum_change_still_queues_a_processed_bundle(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = "music/short/vs_0807_01"
    cached_metadata = _metadata(_marked({"bundleName": target, "hash": "old"}))
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH, cached_metadata, state.validate_asset_metadata
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )
    cache_dir = tmp_path / "bundle-cache"
    cache_dir.mkdir()
    cache_path = cache_dir / target.replace("/", "_")
    cache_path.write_bytes(b"cached")

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata({"bundleName": target, "hash": "new"}),
            _version(),
            config=config,
            bundle_cache_path_resolver=lambda _bundle: cache_path,
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == [target]


def test_excluded_checksum_change_keeps_old_marker_and_reinclude_queues_with_cache(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    target = "music/short/target"
    other = "music/short/other"
    prior = _metadata(
        _marked({"bundleName": target, "hash": "hash1"}),
        _marked({"bundleName": other, "hash": "stable"}),
    )
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH, prior, state.validate_asset_metadata
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )
    current = _metadata(
        {"bundleName": target, "hash": "hash2"},
        {"bundleName": other, "hash": "stable"},
    )

    excluded = asyncio.run(
        net_plan.get_download_list(
            current,
            _version(),
            config=config,
            include_list=[rf"^{other}$"],
        )
    )

    assert excluded.candidates == []
    assert excluded.asset_metadata["bundles"][target]["hash"] == "hash2"
    assert excluded.asset_metadata["bundles"][target]["_sekai_assets_updater"] == {
        "processed_checksum": {"field": "hash", "value": "hash1"}
    }
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        excluded.asset_metadata,
        state.validate_asset_metadata,
    )
    cache_dir = tmp_path / "bundle-cache"
    cache_dir.mkdir()
    (cache_dir / target.replace("/", "_")).write_bytes(b"raw-cache")

    re_included = asyncio.run(
        net_plan.get_download_list(
            current,
            _version(),
            config=config,
            include_list=[rf"^{target}$"],
            bundle_cache_path_resolver=lambda bundle: (
                cache_dir / bundle["bundleName"].replace("/", "_")
            ),
        )
    )
    assert [bundle["bundleName"] for _, bundle in re_included.candidates] == [target]


def test_removed_bundle_reappears_without_provenance_and_is_queued(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = "music/short/removed"
    other = "music/short/present"
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        _metadata(
            _marked({"bundleName": target, "hash": "same"}),
            _marked({"bundleName": other, "hash": "same"}),
        ),
        state.validate_asset_metadata,
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )

    removed = asyncio.run(
        net_plan.get_download_list(
            _metadata(_marked({"bundleName": other, "hash": "same"})),
            _version(),
            config=config,
        )
    )
    assert target not in removed.asset_metadata["bundles"]
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH, removed.asset_metadata, state.validate_asset_metadata
    )

    reappeared = asyncio.run(
        net_plan.get_download_list(
            _metadata(
                {"bundleName": target, "hash": "same"},
                _marked({"bundleName": other, "hash": "same"}),
            ),
            _version(),
            config=config,
            include_list=[rf"^{target}$"],
        )
    )
    assert [bundle["bundleName"] for _, bundle in reappeared.candidates] == [target]


def test_server_supplied_marker_is_stripped_and_cannot_suppress_candidate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = "music/short/server-marker"
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        _metadata({"bundleName": target, "hash": "same"}),
        state.validate_asset_metadata,
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )
    server_bundle = _marked({"bundleName": target, "hash": "same"})

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata(server_bundle),
            _version(),
            config=config,
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == [target]
    assert "_sekai_assets_updater" not in plan.asset_metadata["bundles"][target]


def test_intermediate_processed_names_are_untrusted_and_never_reemitted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    target = "music/short/intermediate"
    raw_cache = {
        **_metadata({"bundleName": target, "hash": "same"}),
        "processed_bundle_names": [target],
    }
    (tmp_path / "metadata.json").write_text(json.dumps(raw_cache))
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )
    cache_dir = tmp_path / "bundle-cache"
    cache_dir.mkdir()
    (cache_dir / target.replace("/", "_")).write_bytes(b"cached")

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata({"bundleName": target, "hash": "same"}),
            _version(),
            config=config,
            bundle_cache_path_resolver=lambda bundle: (
                cache_dir / bundle["bundleName"].replace("/", "_")
            ),
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == [target]
    assert "processed_bundle_names" not in plan.asset_metadata
    assert "processed_bundle_names" not in plan.successful_asset_metadata


def test_force_full_download_uses_only_effective_selection(tmp_path: Path) -> None:
    config = _config(tmp_path)
    included = "music/short/included"
    excluded = "music/short/excluded"
    other = "live2d/model/required"

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata(
                {"bundleName": included, "hash": "same"},
                {"bundleName": excluded, "hash": "same"},
                {"bundleName": other, "hash": "same"},
            ),
            _version(),
            config=config,
            include_list=[rf"^{included}$"],
            automatic_prefixes=("live2d/",),
            force_full_download=True,
        )
    )

    assert {bundle["bundleName"] for _, bundle in plan.candidates} == {included, other}


def test_automatic_prefixes_are_snapshotted_with_user_selection_expansion(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    automatic = "live2d/model/required"
    newly_selected = "music/short/vs_0807_01"
    cached_metadata = _metadata(
        _marked({"bundleName": automatic, "hash": "same"}),
        {"bundleName": newly_selected, "hash": "same"},
    )
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH, cached_metadata, state.validate_asset_metadata
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata(
                {"bundleName": automatic, "hash": "same"},
                {"bundleName": newly_selected, "hash": "same"},
            ),
            _version(),
            config=config,
            include_list=[rf"^{newly_selected}$"],
            automatic_prefixes=("live2d/",),
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == [newly_selected]
    assert plan.successful_asset_metadata["bundles"][automatic]["_sekai_assets_updater"] == {
        "processed_checksum": {"field": "hash", "value": "same"}
    }
    assert plan.successful_asset_metadata["bundles"][newly_selected]["_sekai_assets_updater"] == {
        "processed_checksum": {"field": "hash", "value": "same"}
    }


def test_legacy_metadata_queues_selected_bundle_once_then_nested_marker_suppresses_repeat(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    target = "music/short/vs_0807_01"
    other = "music/short/not-selected"
    legacy_metadata = _metadata(
        {"bundleName": target, "hash": "same"},
        {"bundleName": other, "hash": "same"},
    )
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH, legacy_metadata, state.validate_asset_metadata
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH, _version(), state.validate_game_version
    )

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata(
                {"bundleName": target, "hash": "same"},
                {"bundleName": other, "hash": "same"},
            ),
            _version(),
            config=config,
            include_list=[rf"^{target}$"],
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == [target]
    assert plan.asset_metadata["bundles"][target].get("_sekai_assets_updater") is None
    assert plan.successful_asset_metadata["bundles"][target]["_sekai_assets_updater"] == {
        "processed_checksum": {"field": "hash", "value": "same"}
    }
    assert "_sekai_assets_updater" not in plan.successful_asset_metadata["bundles"][other]
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        plan.successful_asset_metadata,
        state.validate_asset_metadata,
    )

    repeated = asyncio.run(
        net_plan.get_download_list(
            _metadata(
                {"bundleName": target, "hash": "same"},
                {"bundleName": other, "hash": "same"},
            ),
            _version(),
            config=config,
            include_list=[rf"^{target}$"],
        )
    )
    assert repeated.candidates == []


@pytest.mark.parametrize(
    ("cache_name", "legacy_payload"),
    [
        ("metadata.json", {"assetBundles": []}),
        ("version.json", {"version": "legacy-game-version"}),
    ],
)
def test_incompatible_metadata_caches_are_treated_as_missing(
    tmp_path: Path,
    cache_name: str,
    legacy_payload: dict,
) -> None:
    """Legacy cache schemas must trigger a refresh, not block selection."""
    config = _config(tmp_path)
    (tmp_path / cache_name).write_text(json.dumps(legacy_payload))

    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata({"bundleName": "fresh", "hash": "new"}),
            _version(),
            config=config,
            assetver="current-assetver",
        )
    )

    assert [bundle["bundleName"] for _, bundle in plan.candidates] == ["fresh"]


def test_missing_nuverse_template_value_is_descriptive(monkeypatch) -> None:
    class Response:
        status = 200

        def __init__(self, *, json_value=None, body=b""):
            self.json_value = json_value
            self.body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def json(self, **_kwargs):
            return self.json_value

        async def read(self):
            return self.body

    responses = [
        Response(json_value={"appVersion": "1.0", "dataVersion": "2", "assetVersion": "3"}),
        Response(body=b"asset-1"),
    ]

    class Session:
        def __init__(self, **_options):
            pass

        def get(self, *_args, **_kwargs):
            return responses.pop(0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(asset_bundle_info.aiohttp, "ClientSession", Session)
    config = SimpleNamespace(
        GAME_VERSION_JSON_URL="https://meta.test/version",
        GAME_VERSION_URL=None,
        ASSET_VER_URL="https://meta.test/{appVersion}/assetver",
        ASSET_BUNDLE_INFO_URL="https://cdn.test/{assetVer}/{required}",
        REGION=SekaiServerRegion.TW,
        APP_VERSION_OVERRIDE=None,
        PROXY_URL=None,
        REQUEST_TIMEOUT=1,
        AES_KEY=b"key",
        AES_IV=b"iv",
    )

    request = asset_bundle_info.fetch_asset_bundle_info(config, headers={}, cookie=None)
    with pytest.raises(ValueError, match=r"Missing format values for required") as caught:
        asyncio.run(request)

    assert "https://cdn.test/{assetVer}/{required}" in str(caught.value)


def test_colorful_same_checksum_live2d_selected_only_when_cache_path_absent(
    tmp_path: Path,
) -> None:
    """For colorful (non-assetver) servers, an unchanged Live2D bundle whose
    configured cache file is missing must still be selected, mirroring the
    assetver path's ``select_changed_bundles`` behaviour.

    When the cache file exists the bundle must NOT be selected.
    """
    config = _config(tmp_path)
    state.atomic_write_json(
        config.ASSET_BUNDLE_INFO_CACHE_PATH,
        _metadata(_marked({"bundleName": "live2d/motion/foo", "hash": "same"})),
        state.validate_asset_metadata,
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH,
        _version("asset-1"),
        state.validate_game_version,
    )

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    def resolver(bundle: dict) -> Path:
        # Flat, filesystem-safe mapping of a bundle name to its cache path.
        return cache_dir / bundle["bundleName"].replace("/", "_")

    # Cache file absent -> unchanged bundle with identical checksum is selected.
    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata({"bundleName": "live2d/motion/foo", "hash": "same"}),
            _version(),
            config=config,
            assetbundle_host_hash="host-1",
            bundle_cache_path_resolver=resolver,
        )
    )
    assert [b["bundleName"] for _, b in plan.candidates] == ["live2d/motion/foo"]

    # Cache file present -> unchanged bundle is no longer selected.
    (cache_dir / "live2d_motion_foo").write_bytes(b"cached")
    plan = asyncio.run(
        net_plan.get_download_list(
            _metadata({"bundleName": "live2d/motion/foo", "hash": "same"}),
            _version(),
            config=config,
            assetbundle_host_hash="host-1",
            bundle_cache_path_resolver=resolver,
        )
    )
    assert plan.candidates == []


def test_url_template_rejects_missing_and_none_values() -> None:
    with pytest.raises(ValueError, match=r"Missing format values for assetVer"):
        net_urls.format_url_template(
            "https://cdn.test/{appVersion}/{assetVer}",
            appVersion="1.0",
            assetVer=None,
        )

    with pytest.raises(ValueError, match=r"Missing format values for assetVer"):
        net_urls.format_url_template(
            "https://cdn.test/{appVersion}/{assetVer}",
            appVersion="1.0",
        )
