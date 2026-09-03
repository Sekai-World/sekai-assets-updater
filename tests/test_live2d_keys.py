"""Focused tests for pure Live2D incremental keys."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import updater.live2d.keys as keys_module
from updater.live2d.contracts import Live2DIndex, canonical_json_bytes
from updater.live2d.keys import (
    BUILD_MOTION_DATA_SCHEMA_VERSION,
    INDEX_KEY_DOMAIN,
    KEY_FORMAT_VERSION,
    MODEL_KEY_DOMAIN,
    MOTION_SET_KEY_DOMAIN,
    Live2DKeys,
    changed_keys,
    compute_keys,
    index_key,
    model_key,
    motion_set_key,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "live2d" / "contracts_6.8.0.10.json"


def fixture_data() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def fixture_index() -> Live2DIndex:
    return Live2DIndex.from_dict(fixture_data())


def envelope_digest(domain: str, payload: object) -> str:
    envelope = {
        "domain": domain,
        "key_version": KEY_FORMAT_VERSION,
        "payload": payload,
    }
    serialized = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def reverse_dict_order(value: object) -> object:
    if isinstance(value, dict):
        return {key: reverse_dict_order(value[key]) for key in reversed(tuple(value))}
    if isinstance(value, list):
        return [reverse_dict_order(item) for item in value]
    return value


def test_reordered_input_dicts_produce_the_same_keys() -> None:
    original = fixture_data()
    reordered = reverse_dict_order(original)

    first = compute_keys(Live2DIndex.from_dict(original))
    second = compute_keys(Live2DIndex.from_dict(reordered))

    assert second == first
    assert model_key(reordered["model_outputs"][0]) == model_key(original["model_outputs"][0])
    assert motion_set_key(reordered["motion_sets"][0]) == motion_set_key(original["motion_sets"][0])


def test_model_motion_and_index_keys_have_expected_sha256_digests() -> None:
    index = fixture_index()
    model = index.model_outputs[0]
    motion_set = index.motion_sets[0]

    expected_model_payload = {
        "metadata_version": model.metadata_version,
        "model_bundle": model.model_bundle.to_dict(),
        "schema_version": model.schema_version,
    }
    expected_motion_payload = {
        "build_motion_data_schema_version": BUILD_MOTION_DATA_SCHEMA_VERSION,
        "metadata_version": motion_set.metadata_version,
        "motion_bundle": motion_set.motion_bundle.to_dict(),
        "schema_version": motion_set.schema_version,
    }
    expected_index_payload = json.loads(canonical_json_bytes(index))

    assert model_key(model) == ("ce18c9f9e4a208d31693a7bdc4dbab0645b2947873aec964bf97091bb1500021")
    assert model_key(model) == envelope_digest(MODEL_KEY_DOMAIN, expected_model_payload)
    assert motion_set_key(motion_set) == (
        "dc69126ffee4be7b87ec1dfc0ba9425ab105688561b1bcf0c8f1afb893c95a27"
    )
    assert motion_set_key(motion_set) == envelope_digest(
        MOTION_SET_KEY_DOMAIN, expected_motion_payload
    )
    assert index_key(index) == ("eb55220a0d6b1344feb89aa9e0989260f4bbce44645808264d0b7f9e9d7f1d5b")
    assert index_key(index) == envelope_digest(INDEX_KEY_DOMAIN, expected_index_payload)
    assert compute_keys(index).index_key == index_key(index)


def test_build_motion_data_schema_version_independently_changes_motion_set_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    motion_set = fixture_index().motion_sets[0]
    original_key = motion_set_key(motion_set)

    monkeypatch.setattr(
        keys_module,
        "BUILD_MOTION_DATA_SCHEMA_VERSION",
        BUILD_MOTION_DATA_SCHEMA_VERSION + 1,
    )

    assert motion_set_key(motion_set) != original_key


def test_model_bundle_change_only_changes_model_and_index_keys() -> None:
    original_index = fixture_index()
    changed_data = fixture_data()
    changed_checksum = "sha256:" + ("9" * 64)
    changed_data["model_outputs"][0]["model_bundle"]["checksum"] = changed_checksum
    changed_data["models"][0]["model_bundle"]["checksum"] = changed_checksum
    changed_index = Live2DIndex.from_dict(changed_data)

    original = compute_keys(original_index)
    changed = compute_keys(changed_index)

    assert changed.model_keys["ichika-unit"] != original.model_keys["ichika-unit"]
    assert changed.model_keys["mizuki-unit"] == original.model_keys["mizuki-unit"]
    assert changed.motion_set_keys == original.motion_set_keys
    assert changed.index_key != original.index_key


def test_association_evidence_change_only_changes_the_index_key() -> None:
    original_index = fixture_index()
    changed_data = fixture_data()
    changed_data["models"][0]["join_evidence"][0]["rule"] = "updated join evidence"
    changed_index = Live2DIndex.from_dict(changed_data)

    original = compute_keys(original_index)
    changed = compute_keys(changed_index)

    assert changed.model_keys == original.model_keys
    assert changed.motion_set_keys == original.motion_set_keys
    assert changed.index_key != original.index_key


def test_changed_keys_is_sorted_and_reports_new_and_removed_ids() -> None:
    previous = Live2DKeys(
        model_keys={"same-model": "model-a", "removed-model": "model-b"},
        motion_set_keys={"same-motion": "motion-a", "removed-motion": "motion-b"},
        index_key="index-a",
    )
    current = Live2DKeys(
        model_keys={"same-model": "model-a", "added-model": "model-c"},
        motion_set_keys={"same-motion": "motion-a", "added-motion": "motion-c"},
        index_key="index-b",
    )

    expected = (
        "index",
        "model:added-model",
        "model:removed-model",
        "motion_set:added-motion",
        "motion_set:removed-motion",
    )
    assert changed_keys(current, previous) == expected
    assert changed_keys(previous, current) == expected
    assert changed_keys(current, current) == ()

    reversed_current = Live2DKeys(
        model_keys=dict(reversed(tuple(current.model_keys.items()))),
        motion_set_keys=dict(reversed(tuple(current.motion_set_keys.items()))),
        index_key=current.index_key,
    )
    assert changed_keys(reversed_current, previous) == expected


def test_partial_key_snapshot_mapping_reports_missing_fields() -> None:
    with pytest.raises(TypeError, match="incomplete key snapshot mapping") as error:
        changed_keys(
            {"model_keys": {}},
            Live2DKeys(model_keys={}, motion_set_keys={}, index_key="index"),
        )

    assert "index_key, motion_set_keys" in str(error.value)
