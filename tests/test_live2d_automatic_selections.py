"""Unit tests for metadata-driven Live2D association selections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from updater.live2d.association import LIVE2D_TABLE_NAMES
from updater.live2d.automatic_selections import (
    Live2DAutomaticSelectionsError,
    build_automatic_live2d_associated_selections,
    expand_automatic_live2d_model_selections,
)
from updater.live2d.contracts import INDEX_SCHEMA_VERSION, MODEL_OUTPUT_SCHEMA_VERSION
from updater.live2d.index_builder import build_live2d_association_index


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


def test_automatic_discovery_selects_unique_versioned_motion_path_over_root_alias(
    tmp_path: Path,
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    bundle_name = "live2d/motion/clb01_21miku_motion_base"

    selections = build_automatic_live2d_associated_selections(
        {
            "motion": {
                "bundleName": bundle_name,
                "paths": [
                    "StartApp/live2d/motion/v1/collabo/21_miku/clb01_21miku_motion_base",
                    "StartApp/live2d/motion/clb01_21miku_motion_base",
                ],
                "hash": "motion",
            }
        },
        output_root=tmp_path / "output",
        master_data_root=master_root,
    )

    assert selections.motion_sets[0].motion_bundle_output_path == (
        "motion/v1/collabo/21_miku/clb01_21miku_motion_base"
    )


def test_automatic_discovery_ignores_motion_child_paths_when_selecting_root(
    tmp_path: Path,
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    bundle_name = "live2d/motion/clb01_21miku_motion_base"
    root_alias = bundle_name.removeprefix("live2d/motion/")
    versioned_root = "v1/collabo/21_miku/clb01_21miku_motion_base"

    selections = build_automatic_live2d_associated_selections(
        {
            "motion": {
                "bundleName": bundle_name,
                "paths": [
                    f"StartApp/live2d/motion/{root_alias}",
                    f"StartApp/live2d/motion/{root_alias}/facial",
                    f"StartApp/live2d/motion/{root_alias}/motion",
                    f"StartApp/live2d/motion/{versioned_root}",
                    f"StartApp/live2d/motion/{versioned_root}/facial",
                    f"StartApp/live2d/motion/{versioned_root}/motion",
                ],
                "hash": "motion",
            }
        },
        output_root=tmp_path / "output",
        master_data_root=master_root,
    )

    assert selections.motion_sets[0].motion_bundle_output_path == f"motion/{versioned_root}"


def test_automatic_discovery_accepts_motion_bundle_with_only_root_path(tmp_path: Path) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    bundle_name = "live2d/motion/clb01_21miku_motion_base"

    selections = build_automatic_live2d_associated_selections(
        {
            "motion": {
                "bundleName": bundle_name,
                "paths": ["StartApp/live2d/motion/clb01_21miku_motion_base"],
                "hash": "motion",
            }
        },
        output_root=tmp_path / "output",
        master_data_root=master_root,
    )

    assert selections.motion_sets[0].motion_bundle_output_path == (
        "motion/clb01_21miku_motion_base"
    )


def test_automatic_discovery_rejects_ambiguous_versioned_motion_fallback(
    tmp_path: Path,
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)

    with pytest.raises(Live2DAutomaticSelectionsError, match="ambiguous versioned metadata paths"):
        build_automatic_live2d_associated_selections(
            {
                "motion": {
                    "bundleName": "live2d/motion/clb01_21miku_motion_base",
                    "paths": [
                        "StartApp/live2d/motion/clb01_21miku_motion_base",
                        "StartApp/live2d/motion/v1/collabo/21_miku/clb01_21miku_motion_base",
                        "StartApp/live2d/motion/v2/collabo/21_miku/clb01_21miku_motion_base",
                    ],
                    "hash": "motion",
                }
            },
            output_root=tmp_path / "output",
            master_data_root=master_root,
        )


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
            "StartApp/live2d/model/v1/main/first/textures",
            "StartApp/live2d/model/v1/main/first/motion",
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
    model_root = tmp_path / "output" / "model" / "v1" / "main" / "first"
    model_root.mkdir(parents=True)
    for filename in ("first.model3.json", "second.model3.json"):
        (model_root / filename).write_text(
            json.dumps(
                {
                    "Version": 3,
                    "FileReferences": {
                        "Moc": f"{filename}.moc3",
                        "Textures": [],
                    },
                }
            ),
            encoding="utf-8",
        )

    assert [selection.output_path for selection in selections.model_outputs] == [
        "model/v1/main/first",
    ]
    expanded = expand_automatic_live2d_model_selections(selections)
    assert [selection.output_path for selection in expanded.model_outputs] == [
        "model/v1/main/first",
        "model/v1/main/first",
    ]
    assert [selection.model3_path for selection in expanded.model_outputs] == [
        "first.model3.json",
        "second.model3.json",
    ]
    assert [selection.model_output_id for selection in expanded.model_outputs] == [
        "model-v1-main-first-first.model3.json",
        "model-v1-main-first-second.model3.json",
    ]
    assert [selection.motion_bundle_output_path for selection in selections.motion_sets] == [
        "motion/v1/main/first"
    ]

    repeated = expand_automatic_live2d_model_selections(
        build_automatic_live2d_associated_selections(
            {"first": first, "motion": motion},
            output_root=tmp_path / "output",
            master_data_root=master_root,
        )
    )
    assert repeated.model_outputs == expanded.model_outputs

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


def test_automatic_discovery_rejects_overlapping_roots_from_different_bundles(
    tmp_path: Path,
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    bundles = {
        "root": {
            "bundleName": "live2d/model/root",
            "paths": ["StartApp/live2d/model/v1/root"],
            "hash": "root",
        },
        "descendant": {
            "bundleName": "live2d/model/descendant",
            "paths": ["StartApp/live2d/model/v1/root/descendant"],
            "hash": "descendant",
        },
    }

    with pytest.raises(Live2DAutomaticSelectionsError, match="colliding model output paths"):
        build_automatic_live2d_associated_selections(
            bundles,
            output_root=tmp_path / "output",
            master_data_root=master_root,
        )


def test_real_model_layouts_collapse_roots_expand_siblings_and_build_index(
    tmp_path: Path,
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    output_root = tmp_path / "output"
    layouts = {
        "21miku_wonder": "v1/main/21_miku/21miku_wonder",
        "25meiko_band": "v1/main/25_meiko/25meiko_band",
        "sub_asahi": "v1/main/sub/sub_asahi",
        "sub_otoribrother2": "v1/main/sub/sub_otoribrother2",
        "sub_yuina": "v1/main/sub/sub_yuina",
        "v2_02saki_casual": "v1/main/02_saki/v2_02saki_casual",
    }
    bundles = {
        bundle_name: {
            "bundleName": f"live2d/model/{bundle_name}",
            "paths": [
                f"StartApp/live2d/model/{relative_path}",
                f"StartApp/live2d/model/{relative_path}/textures",
            ],
            "hash": bundle_name,
        }
        for bundle_name, relative_path in layouts.items()
    }

    for bundle_name, relative_path in layouts.items():
        model_root = output_root / "model" / relative_path
        model_root.mkdir(parents=True)
        for suffix in ("t04", "t05"):
            filename = f"{bundle_name}_3.0_f_{suffix}.model3.json"
            (model_root / filename).write_text(
                json.dumps(
                    {
                        "Version": 3,
                        "FileReferences": {"Moc": f"{filename}.moc3", "Textures": []},
                    }
                ),
                encoding="utf-8",
            )

    selections = build_automatic_live2d_associated_selections(
        bundles,
        output_root=output_root,
        master_data_root=master_root,
    )
    expected_roots = tuple(f"model/{layouts[bundle_name]}" for bundle_name in sorted(layouts))
    assert tuple(selection.output_path for selection in selections.model_outputs) == expected_roots
    assert all(selection.model3_path is None for selection in selections.model_outputs)

    expanded = expand_automatic_live2d_model_selections(selections)
    repeated = expand_automatic_live2d_model_selections(selections)
    assert expanded.model_outputs == repeated.model_outputs
    assert len(expanded.model_outputs) == 12
    assert len({selection.model_output_id for selection in expanded.model_outputs}) == 12
    first_pair = [
        selection
        for selection in expanded.model_outputs
        if selection.output_path == "model/v1/main/21_miku/21miku_wonder"
    ]
    assert [selection.model3_path for selection in first_pair] == [
        "21miku_wonder_3.0_f_t04.model3.json",
        "21miku_wonder_3.0_f_t05.model3.json",
    ]
    assert [selection.model_output_id for selection in first_pair] == [
        "model-v1-main-21_miku-21miku_wonder-21miku_wonder_3.0_f_t04.model3.json",
        "model-v1-main-21_miku-21miku_wonder-21miku_wonder_3.0_f_t05.model3.json",
    ]

    index = build_live2d_association_index(
        provider=expanded.provider,
        metadata_version="6.8.0.10",
        model_outputs=expanded.model_outputs,
        motion_sets=(),
    )
    assert index.index_version == INDEX_SCHEMA_VERSION
    assert len(index.model_outputs) == 12
    assert all(record.model3_path is not None for record in index.model_outputs)
    assert all(
        record.schema_version == MODEL_OUTPUT_SCHEMA_VERSION for record in index.model_outputs
    )


def test_automatic_model_discovery_uses_actual_case_mismatched_output_path(
    tmp_path: Path,
) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    output_root = tmp_path / "output"
    actual_relative_path = "model/v2/main/01_ichika/v2_01ichika_unit"
    model_root = output_root / actual_relative_path
    model_root.mkdir(parents=True)
    (model_root / "v2_01ichika_unit_3.0_f_t04.model3.json").write_text(
        json.dumps(
            {
                "Version": 3,
                "FileReferences": {
                    "Moc": "v2_01ichika_unit.moc3",
                    "Textures": [],
                },
            }
        ),
        encoding="utf-8",
    )

    selections = build_automatic_live2d_associated_selections(
        {
            "ichika": {
                "bundleName": "live2d/model/v2_01ichika_unit",
                "paths": [
                    "StartApp/live2d/model/v2/main/01_ichika/V2_01ichika_unit",
                ],
                "hash": "ichika",
            }
        },
        output_root=output_root,
        master_data_root=master_root,
    )

    expanded = expand_automatic_live2d_model_selections(selections)
    assert [selection.output_path for selection in expanded.model_outputs] == [actual_relative_path]
    assert [selection.model3_path for selection in expanded.model_outputs] == [
        "v2_01ichika_unit_3.0_f_t04.model3.json"
    ]

    index = build_live2d_association_index(
        provider=expanded.provider,
        metadata_version="6.8.0.10",
        model_outputs=expanded.model_outputs,
        motion_sets=(),
    )
    assert index.model_outputs[0].output_path == actual_relative_path
    assert index.model_outputs[0].model3_path == ("v2_01ichika_unit_3.0_f_t04.model3.json")


def test_automatic_expansion_rejects_model3_symlinks(tmp_path: Path) -> None:
    master_root = tmp_path / "master"
    _write_master_tables(master_root)
    output_root = tmp_path / "output"
    model_root = output_root / "model" / "v1" / "root"
    model_root.mkdir(parents=True)
    outside = tmp_path / "outside.model3.json"
    outside.write_text("{}", encoding="utf-8")
    try:
        (model_root / "linked.model3.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    selections = build_automatic_live2d_associated_selections(
        {
            "root": {
                "bundleName": "live2d/model/root",
                "paths": ["StartApp/live2d/model/v1/root"],
                "hash": "root",
            }
        },
        output_root=output_root,
        master_data_root=master_root,
    )

    with pytest.raises(Live2DAutomaticSelectionsError, match="symlink"):
        expand_automatic_live2d_model_selections(selections)
