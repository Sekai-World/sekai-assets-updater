"""Pure, deterministic incremental keys for the Live2D contracts.

The keys deliberately describe different invalidation boundaries.  A model key
identifies model output inputs, a motion-set key identifies shared motion output
inputs, and an index key identifies the complete association contract.

Every digest is SHA-256 over a canonical JSON envelope::

    {"domain": "...", "key_version": 1, "payload": {...}}

The domain prevents model and motion material from colliding when their payloads
happen to be equal.  ``key_version`` makes a future change to the key material
explicit rather than silently reusing old digests.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from updater.live2d.contracts import (
    Live2DIndex,
    ModelOutputRecord,
    SharedMotionSetRecord,
    _Contract,
    canonical_json_bytes,
    validate_index,
    validate_model_output,
    validate_motion_set,
)

KEY_FORMAT_VERSION = 1
BUILD_MOTION_DATA_SCHEMA_VERSION = 1
MODEL_KEY_DOMAIN = "live2d.model_key"
MOTION_SET_KEY_DOMAIN = "live2d.motion_set_key"
INDEX_KEY_DOMAIN = "live2d.index_key"

__all__ = [
    "BUILD_MOTION_DATA_SCHEMA_VERSION",
    "INDEX_KEY_DOMAIN",
    "KEY_FORMAT_VERSION",
    "MODEL_KEY_DOMAIN",
    "MOTION_SET_KEY_DOMAIN",
    "Live2DKeys",
    "changed_keys",
    "compute_keys",
    "index_key",
    "model_key",
    "motion_set_key",
]


@dataclass(frozen=True, slots=True)
class _KeyEnvelope(_Contract):
    """Internal contract used to serialize the domain-tagged key material."""

    domain: str
    key_version: int
    payload: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "domain": self.domain,
            "key_version": self.key_version,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class Live2DKeys:
    """Immutable keys for one validated :class:`Live2DIndex`.

    ``model_keys`` and ``motion_set_keys`` are keyed by their contract IDs.
    ``changed_keys`` uses those IDs to report affected outputs.
    """

    model_keys: Mapping[str, str]
    motion_set_keys: Mapping[str, str]
    index_key: str

    def __post_init__(self) -> None:
        model_keys = _sorted_key_mapping(self.model_keys, "model_keys")
        motion_set_keys = _sorted_key_mapping(self.motion_set_keys, "motion_set_keys")
        if not isinstance(self.index_key, str):
            raise TypeError("index_key must be a string")
        object.__setattr__(self, "model_keys", MappingProxyType(model_keys))
        object.__setattr__(self, "motion_set_keys", MappingProxyType(motion_set_keys))


def _sorted_key_mapping(value: Mapping[str, str], field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    for key, digest in value.items():
        if not isinstance(key, str) or not isinstance(digest, str):
            raise TypeError(f"{field_name} must map string IDs to string keys")
    return dict(sorted(value.items()))


def _key_digest(domain: str, payload: Mapping[str, object]) -> str:
    envelope = _KeyEnvelope(
        domain=domain,
        key_version=KEY_FORMAT_VERSION,
        payload=payload,
    )
    return hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()


def _model_payload(record: ModelOutputRecord) -> dict[str, object]:
    return {
        "metadata_version": record.metadata_version,
        "model_bundle": record.model_bundle.to_dict(),
        "schema_version": record.schema_version,
    }


def _motion_set_payload(record: SharedMotionSetRecord) -> dict[str, object]:
    # Keep physical BuildMotionData invalidation independent from the
    # SharedMotionSetRecord contract schema so either version can be bumped
    # without implicitly changing the other.
    return {
        "build_motion_data_schema_version": BUILD_MOTION_DATA_SCHEMA_VERSION,
        "metadata_version": record.metadata_version,
        "motion_bundle": record.motion_bundle.to_dict(),
        "schema_version": record.schema_version,
    }


def _canonical_contract_payload(contract: _Contract) -> dict[str, object]:
    payload = json.loads(canonical_json_bytes(contract))
    if not isinstance(payload, dict):  # pragma: no cover - guarded by contracts
        raise TypeError("canonical Live2D contract payload must be an object")
    return payload


def model_key(record: ModelOutputRecord | Mapping[str, object]) -> str:
    """Return the stable key for model bundle/output inputs.

    The model output path, observed file-reference paths, and association rows
    are intentionally excluded.  They belong to publication/index material;
    the model key is invalidated by the metadata version, model bundle identity,
    or model contract schema only.
    """

    validated = validate_model_output(record)
    return _key_digest(MODEL_KEY_DOMAIN, _model_payload(validated))


def motion_set_key(record: SharedMotionSetRecord | Mapping[str, object]) -> str:
    """Return the stable key for shared motion-set output inputs.

    ``BUILD_MOTION_DATA_SCHEMA_VERSION`` independently invalidates physical
    BuildMotionData material, while ``SharedMotionSetRecord.schema_version``
    invalidates the record contract.  Association evidence, known clips, and
    output paths are index/publication material and are excluded.
    """

    validated = validate_motion_set(record)
    return _key_digest(MOTION_SET_KEY_DOMAIN, _motion_set_payload(validated))


def index_key(index: Live2DIndex | Mapping[str, object]) -> str:
    """Return the key for the complete, validated canonical Live2D index."""

    validated = validate_index(index)
    # Live2DIndex canonical serialization already contains the full sorted
    # joins, candidates, known clips, diagnostics, and referenced output fields.
    # Reuse it rather than maintaining a second index serialization here.
    return _key_digest(INDEX_KEY_DOMAIN, _canonical_contract_payload(validated))


def compute_keys(index: Live2DIndex | Mapping[str, object]) -> Live2DKeys:
    """Compute all per-output keys and the key for one validated index."""

    validated = validate_index(index)
    return Live2DKeys(
        model_keys={
            record.model_output_id: model_key(record) for record in validated.model_outputs
        },
        motion_set_keys={
            record.motion_set_id: motion_set_key(record) for record in validated.motion_sets
        },
        index_key=index_key(validated),
    )


def _as_key_snapshot(value: Live2DKeys | Live2DIndex | Mapping[str, object]) -> Live2DKeys:
    if isinstance(value, Live2DKeys):
        return value
    if isinstance(value, Live2DIndex):
        return compute_keys(value)
    if isinstance(value, Mapping):
        snapshot_fields = {"model_keys", "motion_set_keys", "index_key"}
        present_snapshot_fields = snapshot_fields.intersection(value)
        if present_snapshot_fields and present_snapshot_fields != snapshot_fields:
            missing_fields = ", ".join(sorted(snapshot_fields - present_snapshot_fields))
            raise TypeError(f"incomplete key snapshot mapping; missing fields: {missing_fields}")
        if present_snapshot_fields == snapshot_fields:
            return Live2DKeys(
                model_keys=value["model_keys"],  # type: ignore[arg-type]
                motion_set_keys=value["motion_set_keys"],  # type: ignore[arg-type]
                index_key=value["index_key"],  # type: ignore[arg-type]
            )
        return compute_keys(value)
    raise TypeError("expected Live2DKeys, Live2DIndex, or an object")


def changed_keys(
    current: Live2DKeys | Live2DIndex | Mapping[str, object],
    previous: Live2DKeys | Live2DIndex | Mapping[str, object],
) -> tuple[str, ...]:
    """Return sorted affected identifiers between two key snapshots.

    Entries are ``"model:<model_output_id>"``,
    ``"motion_set:<motion_set_id>"``, and ``"index"``.  New and removed IDs
    are both reported, as is an index digest change.  Inputs may be immutable
    ``Live2DKeys`` values, validated index contracts, or their mapping forms.
    """

    current_keys = _as_key_snapshot(current)
    previous_keys = _as_key_snapshot(previous)
    changed: set[str] = set()

    for model_output_id in set(current_keys.model_keys) | set(previous_keys.model_keys):
        if current_keys.model_keys.get(model_output_id) != previous_keys.model_keys.get(
            model_output_id
        ):
            changed.add(f"model:{model_output_id}")

    for motion_set_id in set(current_keys.motion_set_keys) | set(previous_keys.motion_set_keys):
        if current_keys.motion_set_keys.get(motion_set_id) != previous_keys.motion_set_keys.get(
            motion_set_id
        ):
            changed.add(f"motion_set:{motion_set_id}")

    if current_keys.index_key != previous_keys.index_key:
        changed.add("index")

    return tuple(sorted(changed))
