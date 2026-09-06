"""Unit tests for metadata-driven Live2D association selections."""

from __future__ import annotations

from pathlib import Path

import pytest

from updater.live2d.association import LIVE2D_TABLE_NAMES
from updater.live2d.automatic_selections import (
    Live2DAutomaticSelectionsError,
    build_automatic_live2d_associated_selections,
)


def _write_master_tables(root: Path) -> None:
    root.mkdir(parents=True)
    for table_name in LIVE2D_TABLE_NAMES:
        (root / f"{table_name}.json").write_text("[]", encoding="utf-8")


def test_automatic_discovery_filters_and_sorts_exact_bundle_names(tmp_path: Path) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    bundles = {
        "ignored": {"bundleName": "live2d/modeling/not-a-model", "hash": "ignored"},
        "motion-z": {
            "bundleName": "live2d/motion/z",
            "paths": ["StartApp/music/ignored", "StartApp/live2d/motion/v2/main/z"],
            "hash": "motion-z",
        },
        "model-z": {
            "bundleName": "live2d/model/z",
            "paths": ["StartApp/live2d/model/v1/main/z"],
            "hash": "model-z",
        },
        "motion-a": {
            "bundleName": "live2d/motion/a/nested",
            "paths": ["StartApp/live2d/motion/v2/main/a/nested"],
            "hash": "motion-a",
        },
        "model-a": {
            "bundleName": "live2d/model/a/nested",
            "paths": ["StartApp/live2d/model/v1/main/a/nested"],
            "hash": "model-a",
        },
    }

    selections = build_automatic_live2d_associated_selections(
        bundles,
        output_root=tmp_path / "output",
        master_data_root=master_root,
    )

    assert [selection.bundle["bundleName"] for selection in selections.model_outputs] == [
        "live2d/model/a/nested",
        "live2d/model/z",
    ]
    assert [selection.output_path for selection in selections.model_outputs] == [
        "model/v1/main/a/nested",
        "model/v1/main/z",
    ]
    assert [selection.model_output_id for selection in selections.model_outputs] == [
        "model-v1-main-a-nested",
        "model-v1-main-z",
    ]
    assert [selection.bundle["bundleName"] for selection in selections.motion_sets] == [
        "live2d/motion/a/nested",
        "live2d/motion/z",
    ]
    assert selections.motion_sets[0].motion_bundle_output_path == "motion/v2/main/a/nested"
    assert selections.motion_sets[0].motion_output_path == "motion/v2/main/a/nested/motion"
    assert selections.motion_sets[0].facial_output_path == "motion/v2/main/a/nested/facial"


@pytest.mark.parametrize(
    "bundles, message",
    [
        (
            {
                "first": {
                    "bundleName": "live2d/model/one",
                    "paths": ["StartApp/live2d/model/v1/one"],
                    "hash": "one",
                },
                "second": {
                    "bundleName": "live2d/model/one",
                    "paths": ["StartApp/live2d/model/v1/one"],
                    "hash": "two",
                },
            },
            "duplicate model bundleName",
        ),
        (
            {
                "nested": {
                    "bundleName": "live2d/model/a/b",
                    "paths": ["StartApp/live2d/model/a/b"],
                    "hash": "nested",
                },
                "flat": {
                    "bundleName": "live2d/model/a-b",
                    "paths": ["StartApp/live2d/model/a-b"],
                    "hash": "flat",
                },
            },
            "colliding model selection IDs",
        ),
        (
            {
                "first": {
                    "bundleName": "live2d/model/a/model",
                    "paths": ["StartApp/live2d/model/v1/shared"],
                    "hash": "first",
                },
                "second": {
                    "bundleName": "live2d/model/b/model",
                    "paths": ["StartApp/live2d/model/v1/shared"],
                    "hash": "second",
                },
            },
            "colliding model output paths",
        ),
        (
            {
                "shared": {
                    "bundleName": "live2d/model/shared",
                    "paths": [
                        "StartApp/live2d/model/v1/shared",
                        "StartApp/live2d/model/v1/shared/nested",
                    ],
                    "hash": "shared",
                }
            },
            "colliding model output paths",
        ),
        (
            {
                "base": {
                    "bundleName": "live2d/motion/a",
                    "paths": ["StartApp/live2d/motion/v2/a"],
                    "hash": "base",
                },
                "nested": {
                    "bundleName": "live2d/motion/a/facial",
                    "paths": ["StartApp/live2d/motion/v2/a/facial"],
                    "hash": "nested",
                },
            },
            "colliding motion output paths",
        ),
        (
            {
                "unsafe": {
                    "bundleName": "live2d/motion/../escape",
                    "paths": ["StartApp/live2d/motion/v2/escape"],
                    "hash": "unsafe",
                }
            },
            "unsafe bundle path",
        ),
    ],
)
def test_automatic_discovery_rejects_duplicates_and_unsafe_paths(
    tmp_path: Path, bundles: dict[str, dict[str, str]], message: str
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)

    with pytest.raises(Live2DAutomaticSelectionsError, match=message):
        build_automatic_live2d_associated_selections(
            bundles,
            output_root=tmp_path / "output",
            master_data_root=master_root,
        )


def test_automatic_discovery_requires_configured_master_data_root(tmp_path: Path) -> None:
    with pytest.raises(Live2DAutomaticSelectionsError, match="LIVE2D_ASSOCIATION_MASTER_DATA_DIR"):
        build_automatic_live2d_associated_selections(
            {"model": {"bundleName": "live2d/model/model", "hash": "model"}},
            output_root=tmp_path / "output",
        )


@pytest.mark.parametrize(
    "bundle",
    [
        {"bundleName": "live2d/model/model", "hash": "model"},
        {"bundleName": "live2d/motion/motion", "paths": [], "hash": "motion"},
    ],
)
def test_automatic_discovery_rejects_missing_or_empty_metadata_paths(
    tmp_path: Path, bundle: dict[str, object]
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)

    with pytest.raises(Live2DAutomaticSelectionsError, match="metadata path"):
        build_automatic_live2d_associated_selections(
            {"selected": bundle},
            output_root=tmp_path / "output",
            master_data_root=master_root,
        )


def test_automatic_discovery_emits_each_matching_model_path_and_rejects_ambiguous_outputs(
    tmp_path: Path,
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    first = {
        "bundleName": "live2d/model/first",
        "paths": [
            "StartApp/music/ignored",
            "StartApp/live2d/model/v1/main/first",
            "StartApp/live2d/model/v1/main/second",
        ],
        "hash": "first",
    }
    motion = {
        "bundleName": "live2d/motion/shared",
        "paths": [
            "StartApp/live2d/motion/v1/main/first",
            "StartApp/live2d/motion/v1/main/second",
        ],
        "hash": "motion",
    }
    selections = build_automatic_live2d_associated_selections(
        {"first": first, "motion": motion},
        output_root=tmp_path / "output",
        master_data_root=master_root,
    )
    assert [selection.output_path for selection in selections.model_outputs] == [
        "model/v1/main/first",
        "model/v1/main/second",
    ]
    assert [selection.model_output_id for selection in selections.model_outputs] == [
        "model-v1-main-first",
        "model-v1-main-second",
    ]
    assert [selection.motion_bundle_output_path for selection in selections.motion_sets] == [
        "motion/v1/main/first"
    ]

    duplicate_output = {
        "bundleName": "live2d/model/second",
        "paths": ["StartApp/live2d/model/v1/main/first"],
        "hash": "second",
    }
    with pytest.raises(Live2DAutomaticSelectionsError, match="colliding model output paths"):
        build_automatic_live2d_associated_selections(
            {"first": first, "second": duplicate_output},
            output_root=tmp_path / "output",
            master_data_root=master_root,
        )
