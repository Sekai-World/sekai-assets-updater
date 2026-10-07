from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from updater.extract import bundle as bundle_module


def test_extract_asset_bundle_routes_pool_by_cost_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routed: list[str | None] = []

    def fake_pool(_config, cost_class):
        routed.append(cost_class)
        return None

    def fake_sync(*_args, **_kwargs):
        return [], [], []

    monkeypatch.setattr(bundle_module, "_resolve_extract_pool", fake_pool)
    monkeypatch.setattr(bundle_module, "_extract_bundle_files_sync", fake_sync)
    config = SimpleNamespace(UPDATER_MODE="assets", ENABLE_MODEL3D_FBX_EXPORT=False)

    for cost_class in ("media", "light", None):
        outputs = asyncio.run(
            bundle_module.extract_asset_bundle(
                Path("bundle"),
                {"bundleName": "b"},
                Path("out"),
                unity_version="2022.3.21f1",
                config=config,
                cost_class=cost_class,
            )
        )
        assert outputs == []

    assert routed == ["media", "light", None]


@pytest.mark.parametrize("mode", ["live2d", "live2d-associated"])
def test_extract_asset_bundle_skips_live2d_motion_bundles_regardless_of_cost_class(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    routed: list[str | None] = []

    def fake_pool(_config, cost_class):
        routed.append(cost_class)
        return None

    monkeypatch.setattr(bundle_module, "_resolve_extract_pool", fake_pool)
    config = SimpleNamespace(UPDATER_MODE=mode, ENABLE_MODEL3D_FBX_EXPORT=False)

    for cost_class in ("media", "light", None):
        outputs = asyncio.run(
            bundle_module.extract_asset_bundle(
                Path("bundle"),
                {"bundleName": "live2d/motion/base_1"},
                Path("out"),
                unity_version="2022.3.21f1",
                config=config,
                cost_class=cost_class,
            )
        )
        assert outputs == []

    # Invariant I5: the motion-bundle skip happens before pool routing, so
    # scheduler classification cannot change which bundles skip extraction.
    assert routed == []
