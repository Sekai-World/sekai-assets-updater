from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from anyio import Path as AnyioPath

import asset_bundle_info
import helpers
import state
from model import SekaiServerRegion


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


def _version(assetver: str = "asset-1") -> dict:
    return {"appVersion": "1.0", "assetVersion": "2", "assetver": assetver}


def test_priority_patterns_follow_declared_order_and_unmatched_tail() -> None:
    candidates = [
        ("url-zeta", {"bundleName": "zeta"}),
        ("url-character", {"bundleName": "character/member"}),
        ("url-music", {"bundleName": "music/song"}),
        ("url-character-2", {"bundleName": "character/motion"}),
    ]

    result = asyncio.run(
        helpers.sort_download_list(
            candidates,
            priority_list=[r"^music/", r"^character/"],
        )
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
        helpers.get_download_list(
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
        _metadata({"bundleName": "stable", "hash": "same"}),
        state.validate_asset_metadata,
    )
    state.atomic_write_json(
        config.GAME_VERSION_JSON_CACHE_PATH,
        _version("old-assetver"),
        state.validate_game_version,
    )

    plan = asyncio.run(
        helpers.get_download_list(
            _metadata({"bundleName": "stable", "hash": "same"}),
            _version(),
            config=config,
            assetver="new-assetver",
        )
    )

    assert plan.candidates == []


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


def test_url_template_rejects_missing_and_none_values() -> None:
    with pytest.raises(ValueError, match=r"Missing format values for assetVer"):
        helpers.format_url_template(
            "https://cdn.test/{appVersion}/{assetVer}",
            appVersion="1.0",
            assetVer=None,
        )

    with pytest.raises(ValueError, match=r"Missing format values for assetVer"):
        helpers.format_url_template(
            "https://cdn.test/{appVersion}/{assetVer}",
            appVersion="1.0",
        )
