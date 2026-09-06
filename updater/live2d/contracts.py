"""Versioned, sanitized contracts for the independent Live2D track.

The contracts in this module describe metadata and output identities only.  They
intentionally do not model a Unity collection and they do not add inferred
motion or expression references to a Cubism ``model3.json`` document.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, NoReturn, TypeAlias

from updater.modes import is_live2d_bundle as _is_live2d_bundle

CONTRACT_VERSION = 1
LEGACY_MODEL_OUTPUT_SCHEMA_VERSION = 1
# The index container remains v1; model-output records are independently
# versioned so legacy indexes can still be parsed when their records omit
# ``model3_path``.
MODEL_OUTPUT_SCHEMA_VERSION = 2
MOTION_SET_SCHEMA_VERSION = 1
CANDIDATE_SCHEMA_VERSION = 1
MODEL_ASSOCIATION_SCHEMA_VERSION = 1
KNOWN_CLIPS_SCHEMA_VERSION = 1
DIAGNOSTIC_SCHEMA_VERSION = 1
INDEX_SCHEMA_VERSION = 1


JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


class CandidateStatus(StrEnum):
    """Status of evidence connecting a shared motion set to a model record."""

    VERIFIED = "verified"
    DERIVED = "derived"
    AMBIGUOUS = "ambiguous"


class CandidateEvidenceRuleCode(StrEnum):
    """Stable machine-readable origins for :class:`CandidateEvidence`.

    ``UNSPECIFIED`` is the compatibility fallback used when loading legacy
    serialized evidence that predates the ``rule_code`` field.
    """

    EXACT_COSTUME_JOIN_ANCHOR = "exact_costume_join_anchor"
    EXACT_COSTUME_JOIN_TARGET = "exact_costume_join_target"
    BUSINESS_ROLE_USE = "business_role_use"
    NAMING_CANDIDATE = "naming_candidate"
    BUNDLE_IDENTITY = "bundle_identity"
    UNSPECIFIED = "unspecified"


CANDIDATE_EVIDENCE_RULE_CODES = frozenset(code.value for code in CandidateEvidenceRuleCode)


class DiagnosticSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class DiagnosticCode(StrEnum):
    LIVE2D_SCOPE_MISMATCH = "live2d_scope_mismatch"
    LIVE2D_JOIN_MISSING = "live2d_join_missing"
    LIVE2D_MAPPING_AMBIGUOUS = "live2d_mapping_ambiguous"
    LIVE2D_BUILD_MOTION_INVALID = "live2d_build_motion_invalid"
    LIVE2D_FACIAL_FORMAT_INVALID = "live2d_facial_format_invalid"
    LIVE2D_INDEX_INTEGRITY = "live2d_index_integrity"


# Keep this list explicit so a typo cannot silently become a durable diagnostic
# code.  The general codes are included for callers that attach shared updater
# diagnostics to a Live2D index; the six Live2D codes above are the required
# association-track vocabulary.
DIAGNOSTIC_CODES = frozenset(
    {
        *(code.value for code in DiagnosticCode),
        "metadata_missing",
        "dependency_cycle",
        "dependency_excluded",
        "cache_missing",
        "bundle_download_failed",
        "collection_load_failed",
        "unresolved_pptr",
        "pptr_type_mismatch",
        "unsupported_unity_object",
        "unsupported_shader",
        "unsupported_track",
        "invalid_glb",
        "manifest_integrity_failure",
    }
)

_TOKEN_RE = re.compile(r"^(?!_)\w[\w.+:-]*$", re.ASCII)
_RULE_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$", re.ASCII)
_KEY_RE = re.compile(r"^(?![\d_])\w+$", re.ASCII)
_SENSITIVE_TEXT_RE = re.compile(
    r"(?ix)"
    r"(?:\b[a-z][a-z0-9+.-]*://)"
    r"|(?:^|[?&\s])(?:access[_-]?token|api[_-]?key|credential|password|secret|"
    r"signature|sig|token|x-amz-[a-z-]+)\s*="
    r"|\b(?:authorization|cookie|set-cookie)\s*:"
    r"|-----BEGIN\s+(?:[A-Z ]+)?PRIVATE KEY-----"
    r"|^\s*(?:UnityFS|UnityWebData1)\b"
)
_SENSITIVE_KEYS = frozenset(
    {
        "url",
        "uri",
        "token",
        "accesstoken",
        "apikey",
        "accesskey",
        "secret",
        "secretkey",
        "password",
        "credential",
        "credentials",
        "clientsecret",
        "privatekey",
        "cookie",
        "setcookie",
        "cookieheader",
        "authorization",
        "signature",
        "sig",
        "payload",
        "rawpayload",
        "bundlepayload",
        "rawbundle",
        "headers",
        "requestheaders",
        "responseheaders",
        "header",
        "body",
        "blob",
        "bytes",
        "content",
        "data",
        "raw",
        "serialized",
        "serializeddata",
        "binary",
        "transport",
        "signedurl",
        "signeduri",
        "presignedurl",
        "downloadurl",
        "sourceurl",
        "assetlist",
        "sekaimasterdbdiff",
    }
)

_FORBIDDEN_MODEL3_REFERENCE_KEYS = frozenset({"motions", "expressions"})
_REQUIRED_MESSAGE = "is required"
_FILE_REFERENCES_FIELD = "file_references"
_MODEL3_MOC_KEY = "Moc"
_MODEL3_TEXTURES_KEY = "Textures"
_MODEL3_PHYSICS_KEY = "Physics"
_MODEL3_REFERENCE_KEYS = frozenset({_MODEL3_MOC_KEY, _MODEL3_TEXTURES_KEY, _MODEL3_PHYSICS_KEY})
_FILE_REFERENCES_MOC_FIELD = f"{_FILE_REFERENCES_FIELD}.{_MODEL3_MOC_KEY}"
_FILE_REFERENCES_TEXTURES_FIELD = f"{_FILE_REFERENCES_FIELD}.{_MODEL3_TEXTURES_KEY}"
_FILE_REFERENCES_PHYSICS_FIELD = f"{_FILE_REFERENCES_FIELD}.{_MODEL3_PHYSICS_KEY}"
_MODEL3_REFERENCE_ALIASES = MappingProxyType(
    {
        _MODEL3_MOC_KEY: "moc",
        _MODEL3_TEXTURES_KEY: "textures",
        _MODEL3_PHYSICS_KEY: "physics",
    }
)


class Live2DContractError(ValueError):
    """Raised when a Live2D contract cannot be represented safely."""


class _Contract:
    __slots__ = ()

    def validate(self) -> _Contract:
        return self

    def to_json_dict(self) -> dict[str, JSONValue]:
        return to_json_dict(self)

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self)


def _fail(field_name: str, message: str) -> NoReturn:
    raise Live2DContractError(f"{field_name}: {message}")


def _integrity_fail(message: str) -> NoReturn:
    _fail(DiagnosticCode.LIVE2D_INDEX_INTEGRITY.value, message)


def _require_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail(field_name, "expected an object")
    return value


def _reject_unknown_keys(value: Mapping[str, object], allowed: set[str], field_name: str) -> None:
    unknown = sorted(key for key in value if key not in allowed)
    if unknown:
        _fail(field_name, f"unsupported fields: {', '.join(unknown)}")


def _require_fields(
    value: Mapping[str, object], field_name: str, required_fields: tuple[str, ...]
) -> None:
    for required_field in required_fields:
        if required_field not in value:
            _fail(f"{field_name}.{required_field}", _REQUIRED_MESSAGE)


def _validate_schema_version(value: object, field_name: str, expected: int) -> int:
    if type(value) is not int or value != expected:
        _fail(field_name, f"unsupported schema version {value!r}; expected {expected}")
    return value


def _validate_model_output_schema_version(
    value: object,
    model3_path: object,
    field_name: str = "model_output.schema_version",
) -> int:
    if type(value) is not int or value not in {
        LEGACY_MODEL_OUTPUT_SCHEMA_VERSION,
        MODEL_OUTPUT_SCHEMA_VERSION,
    }:
        _fail(
            field_name,
            f"unsupported schema version {value!r}; expected one of "
            f"{LEGACY_MODEL_OUTPUT_SCHEMA_VERSION} or {MODEL_OUTPUT_SCHEMA_VERSION}",
        )
    if value == LEGACY_MODEL_OUTPUT_SCHEMA_VERSION and model3_path is not None:
        _fail("model_output.model3_path", "is not supported by schema version 1")
    if value == MODEL_OUTPUT_SCHEMA_VERSION and model3_path is None:
        _fail("model_output.model3_path", "is required by schema version 2")
    return value


def _looks_sensitive(value: str) -> bool:
    return bool(_SENSITIVE_TEXT_RE.search(value))


def _validate_safe_text(
    value: object,
    field_name: str,
    *,
    allow_empty: bool = False,
    max_length: int = 4096,
) -> str:
    if not isinstance(value, str):
        _fail(field_name, "expected a string")
    if not allow_empty and not value:
        _fail(field_name, "must not be empty")
    if value != value.strip():
        _fail(field_name, "must not have leading or trailing whitespace")
    if len(value) > max_length:
        _fail(field_name, f"must be at most {max_length} characters")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        _fail(field_name, "contains a control character")
    if _looks_sensitive(value):
        _fail(field_name, "contains sensitive transport data")
    return value


def _validate_token(value: object, field_name: str) -> str:
    text = _validate_safe_text(value, field_name, max_length=256)
    if not _TOKEN_RE.fullmatch(text):
        _fail(field_name, "must be a stable identifier token")
    return text


def _validate_rule_code(value: object, field_name: str) -> str:
    if isinstance(value, CandidateEvidenceRuleCode):
        value = value.value
    text = _validate_safe_text(value, field_name, max_length=128)
    if not _RULE_CODE_RE.fullmatch(text):
        _fail(field_name, "must be a lowercase snake_case identifier token")
    return text


def _validate_identifier(value: object, field_name: str) -> str:
    return _validate_token(value, field_name)


def _validate_version_label(value: object, field_name: str) -> str:
    return _validate_token(value, field_name)


def _validate_relative_path(value: object, field_name: str) -> str:
    path = _validate_safe_text(value, field_name, max_length=1024)
    if path.startswith(("/", "\\", "~")) or "\\" in path or "://" in path or ":" in path:
        _fail(field_name, "must be a relative POSIX path")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _fail(field_name, "contains an unsafe path segment")
    return path


def _validate_bundle_name(value: object, field_name: str = "bundle.name") -> str:
    return _validate_relative_path(value, field_name)


def _validate_checksum(value: object, field_name: str = "checksum") -> str:
    checksum = _validate_safe_text(value, field_name, max_length=256)
    if not _TOKEN_RE.fullmatch(checksum):
        _fail(field_name, "must be a stable checksum token")
    lowered = checksum.casefold()
    if any(part in lowered for part in ("token", "secret", "password", "credential")):
        _fail(field_name, "must not contain credential-like data")
    return checksum


def _sequence(value: object, field_name: str) -> tuple[object, ...]:
    if not isinstance(value, (list, tuple)):
        _fail(field_name, "expected a JSON array")
    return tuple(value)


def _validate_json_key(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
        _fail(field_name, "must be a simple field name")
    normalized = value.casefold().replace("_", "")
    if normalized in _SENSITIVE_KEYS:
        _fail(field_name, "sensitive transport and obsolete payload fields are forbidden")
    return value


def _is_path_key(key: str) -> bool:
    normalized = key.casefold().replace("_", "")
    return normalized == "path" or normalized.endswith("path")


def _freeze_json_object(
    value: Mapping[str, object], field_name: str, depth: int
) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key in sorted(value, key=str):
        safe_key = _validate_json_key(key, f"{field_name}.key")
        child = value[key]
        if _is_path_key(safe_key) and isinstance(child, str):
            _validate_relative_path(child, f"{field_name}.{safe_key}")
        frozen[safe_key] = _freeze_json_value(child, f"{field_name}.{safe_key}", depth=depth + 1)
    return MappingProxyType(frozen)


def _freeze_json_sequence(
    value: list[object] | tuple[object, ...], field_name: str, depth: int
) -> tuple[object, ...]:
    return tuple(
        _freeze_json_value(item, f"{field_name}[{index}]", depth=depth + 1)
        for index, item in enumerate(value)
    )


def _freeze_json_value(value: object, field_name: str, *, depth: int = 0) -> object:
    """Validate a small JSON value and make containers immutable."""

    if depth > 8:
        _fail(field_name, "nested values are too deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(field_name, "non-finite numbers are not allowed")
        return 0.0 if value == 0 else value
    if isinstance(value, str):
        return _validate_safe_text(value, field_name, allow_empty=True)
    if isinstance(value, Mapping):
        return _freeze_json_object(value, field_name, depth)
    if isinstance(value, (list, tuple)):
        return _freeze_json_sequence(value, field_name, depth)
    _fail(field_name, f"unsupported JSON value type {type(value).__name__}")


def _freeze_json_mapping(value: object, field_name: str) -> Mapping[str, object]:
    mapping = _require_mapping(value, field_name)
    frozen = _freeze_json_value(mapping, field_name)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        _fail(field_name, "expected an object")
    return frozen


def _validate_model3_textures(moc: object, textures: object) -> tuple[str, ...]:
    _validate_relative_path(moc, _FILE_REFERENCES_MOC_FIELD)
    texture_values = _sequence(textures, _FILE_REFERENCES_TEXTURES_FIELD)
    validated = tuple(
        _validate_relative_path(texture, f"{_FILE_REFERENCES_TEXTURES_FIELD}[{index}]")
        for index, texture in enumerate(texture_values)
    )
    _validate_unique(validated, _FILE_REFERENCES_TEXTURES_FIELD)
    return validated


def _validate_model3_additional(value: object) -> Mapping[str, object]:
    additional = _freeze_json_mapping(value, f"{_FILE_REFERENCES_FIELD}.additional")
    for key in additional:
        if key in _MODEL3_REFERENCE_KEYS:
            _fail(f"{_FILE_REFERENCES_FIELD}.{key}", "duplicates a represented field")
        if key.casefold() in _FORBIDDEN_MODEL3_REFERENCE_KEYS:
            _fail(
                f"{_FILE_REFERENCES_FIELD}.{key}",
                "Motions and Expressions are not model output references",
            )
    return additional


def _plain(value: object) -> Any:
    if isinstance(value, _Contract):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {key: _plain(value[key]) for key in sorted(value)}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def _stable_object_bytes(value: object) -> bytes:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _coerce(value: object, contract_type: type[_Contract], field_name: str) -> _Contract:
    if isinstance(value, contract_type):
        return value
    if isinstance(value, Mapping):
        return contract_type.from_dict(value)
    _fail(field_name, f"expected {contract_type.__name__} or an object")


def _enum_value(value: object, enum_type: type[StrEnum], field_name: str) -> str:
    if not isinstance(value, str):
        _fail(field_name, f"unsupported value {value!r}")
    try:
        return enum_type(value).value
    except ValueError:
        _fail(field_name, f"unsupported value {value!r}")


def _validate_optional_entity_id(value: object, field_name: str) -> int | str | None:
    if value is None:
        return None
    if type(value) is int:
        if value < 0:
            _fail(field_name, "must not be negative")
        return value
    return _validate_identifier(value, field_name)


def _validate_clip_name(value: object, field_name: str) -> str:
    name = _validate_safe_text(value, field_name, max_length=256)
    # Internal whitespace is valid in source clip names; the generated output
    # filename is the name plus the motion3 suffix.
    if (
        name in (".", "..")
        or "/" in name
        or "\\" in name
        or name.casefold().endswith((".motion3.json", ".exp3.json"))
    ):
        _fail(field_name, "must be a clip name, not a path or output filename")
    return name


def _validate_unique(values: tuple[object, ...], field_name: str, key=None) -> None:
    seen: set[object] = set()
    for value in values:
        identity = key(value) if key is not None else value
        if identity in seen:
            _fail(field_name, f"duplicate identity {identity!r}")
        seen.add(identity)


@dataclass(frozen=True, slots=True)
class BundleIdentity(_Contract):
    """A sanitized metadata Bundle name and its content checksum."""

    name: str
    checksum: str

    def __post_init__(self) -> None:
        _validate_bundle_name(self.name)
        _validate_checksum(self.checksum)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BundleIdentity:
        mapping = _require_mapping(value, "bundle")
        _reject_unknown_keys(mapping, {"name", "bundleName", "checksum"}, "bundle")
        if (
            "name" in mapping
            and "bundleName" in mapping
            and mapping["name"] != mapping["bundleName"]
        ):
            _fail("bundle", "name and bundleName disagree")
        name = mapping.get("name", mapping.get("bundleName"))
        if name is None:
            _fail("bundle.name", _REQUIRED_MESSAGE)
        _require_fields(mapping, "bundle", ("checksum",))
        return cls(name=name, checksum=mapping["checksum"])

    def to_dict(self) -> dict[str, JSONValue]:
        return {"name": self.name, "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class Model3FileReferences(_Contract):
    """Observed Cubism model references with safe preservation of future fields."""

    moc: str
    textures: tuple[str, ...]
    physics: str | None = None
    additional: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        textures = _validate_model3_textures(self.moc, self.textures)
        object.__setattr__(self, "textures", textures)
        if self.physics is not None:
            _validate_relative_path(self.physics, _FILE_REFERENCES_PHYSICS_FIELD)
        additional = _validate_model3_additional(self.additional)
        object.__setattr__(self, "additional", additional)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Model3FileReferences:
        mapping = _require_mapping(value, "file_references")
        forbidden = sorted(
            key
            for key in mapping
            if isinstance(key, str) and key.casefold() in _FORBIDDEN_MODEL3_REFERENCE_KEYS
        )
        if forbidden:
            _fail(
                _FILE_REFERENCES_FIELD,
                f"forbidden fields: {', '.join(forbidden)}",
            )
        _require_fields(
            mapping,
            _FILE_REFERENCES_FIELD,
            (_MODEL3_MOC_KEY, _MODEL3_TEXTURES_KEY),
        )
        return cls(
            moc=mapping[_MODEL3_MOC_KEY],
            textures=mapping[_MODEL3_TEXTURES_KEY],
            physics=mapping.get(_MODEL3_PHYSICS_KEY),
            additional={
                key: item for key, item in mapping.items() if key not in _MODEL3_REFERENCE_KEYS
            },
        )

    @classmethod
    def from_model3_json(cls, value: Mapping[str, object]) -> Model3FileReferences:
        mapping = _require_mapping(value, "model3")
        file_references = mapping.get("FileReferences", mapping)
        return cls.from_dict(_require_mapping(file_references, "model3.FileReferences"))

    def __getattr__(self, name: str) -> object:
        """Keep legacy model3-style attribute access without field-name clashes."""

        field_name = _MODEL3_REFERENCE_ALIASES.get(name)
        if field_name is None:
            raise AttributeError(f"{type(self).__name__} object has no attribute {name!r}")
        return object.__getattribute__(self, field_name)

    @property
    def additional_references(self) -> Mapping[str, object]:
        return self.additional

    @property
    def pose(self) -> object | None:
        return self.additional.get("Pose")

    @property
    def display_info(self) -> object | None:
        return self.additional.get("DisplayInfo")

    @property
    def hit_areas(self) -> object | None:
        return self.additional.get("HitAreas")

    def to_dict(self) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            _MODEL3_MOC_KEY: self.moc,
            _MODEL3_TEXTURES_KEY: list(self.textures),
        }
        if self.physics is not None:
            result[_MODEL3_PHYSICS_KEY] = self.physics
        result.update({key: _plain(self.additional[key]) for key in sorted(self.additional)})
        return result


@dataclass(frozen=True, slots=True)
class KnownClips(_Contract):
    """Names emitted by a shared motion set, separated by output purpose."""

    motions: tuple[str, ...] = field(default_factory=tuple)
    facials: tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = KNOWN_CLIPS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_schema_version(
            self.schema_version, "known_clips.schema_version", KNOWN_CLIPS_SCHEMA_VERSION
        )
        motions = tuple(
            _validate_clip_name(name, f"known_clips.motions[{index}]")
            for index, name in enumerate(_sequence(self.motions, "known_clips.motions"))
        )
        facials = tuple(
            _validate_clip_name(name, f"known_clips.facials[{index}]")
            for index, name in enumerate(_sequence(self.facials, "known_clips.facials"))
        )
        _validate_unique(motions, "known_clips.motions")
        _validate_unique(facials, "known_clips.facials")
        object.__setattr__(self, "motions", tuple(sorted(motions)))
        object.__setattr__(self, "facials", tuple(sorted(facials)))

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> KnownClips:
        mapping = _require_mapping(value, "known_clips")
        _reject_unknown_keys(mapping, {"schema_version", "motions", "facials"}, "known_clips")
        return cls(
            motions=mapping.get("motions", ()),
            facials=mapping.get("facials", ()),
            schema_version=mapping.get("schema_version", KNOWN_CLIPS_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.schema_version,
            "motions": list(self.motions),
            "facials": list(self.facials),
        }


@dataclass(frozen=True, slots=True)
class CandidateEvidence(_Contract):
    """Auditable, sanitized evidence for a motion-set candidate."""

    source: str
    rule: str
    source_table: str | None = None
    source_row: Mapping[str, object] = field(default_factory=dict)
    observed: str | None = None
    expected: str | None = None
    evidence_id: str | None = None
    schema_version: int = CANDIDATE_SCHEMA_VERSION
    # Legacy serialized evidence may omit this field; ``unspecified`` keeps
    # those records loadable while making the compatibility fallback explicit.
    rule_code: CandidateEvidenceRuleCode | str = CandidateEvidenceRuleCode.UNSPECIFIED

    def __post_init__(self) -> None:
        _validate_schema_version(
            self.schema_version, "evidence.schema_version", CANDIDATE_SCHEMA_VERSION
        )
        _validate_token(self.source, "evidence.source")
        _validate_safe_text(self.rule, "evidence.rule", max_length=1024)
        rule_code = _validate_rule_code(self.rule_code, "evidence.rule_code")
        object.__setattr__(self, "rule_code", rule_code)
        if self.source_table is not None:
            table = _validate_token(self.source_table, "evidence.source_table")
            lowered = table.casefold()
            if "assetlist" in lowered or "sekai_master_db_diff" in lowered:
                _fail("evidence.source_table", "obsolete assetList evidence is forbidden")
            object.__setattr__(self, "source_table", table)
        frozen_row = _freeze_json_mapping(self.source_row, "evidence.source_row")
        object.__setattr__(self, "source_row", frozen_row)
        if self.observed is not None:
            _validate_safe_text(self.observed, "evidence.observed", max_length=1024)
        if self.expected is not None:
            _validate_safe_text(self.expected, "evidence.expected", max_length=1024)
        if self.evidence_id is None:
            seed = {
                "source": self.source,
                "rule": self.rule,
                "rule_code": rule_code,
                "source_table": self.source_table,
                "source_row": _plain(frozen_row),
                "observed": self.observed,
                "expected": self.expected,
            }
            generated = "evidence-" + hashlib.sha256(_stable_object_bytes(seed)).hexdigest()[:24]
            object.__setattr__(self, "evidence_id", generated)
        else:
            _validate_identifier(self.evidence_id, "evidence.evidence_id")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CandidateEvidence:
        mapping = _require_mapping(value, "evidence")
        _reject_unknown_keys(
            mapping,
            {
                "schema_version",
                "evidence_id",
                "source",
                "source_table",
                "source_row",
                "rule",
                "rule_code",
                "observed",
                "expected",
            },
            "evidence",
        )
        _require_fields(mapping, "evidence", ("source", "rule"))
        return cls(
            source=mapping["source"],
            rule=mapping["rule"],
            rule_code=mapping.get("rule_code", CandidateEvidenceRuleCode.UNSPECIFIED),
            source_table=mapping.get("source_table"),
            source_row=mapping.get("source_row", {}),
            observed=mapping.get("observed"),
            expected=mapping.get("expected"),
            evidence_id=mapping.get("evidence_id"),
            schema_version=mapping.get("schema_version", CANDIDATE_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "source": self.source,
            "rule": self.rule,
            "rule_code": self.rule_code,
            "source_row": _plain(self.source_row),
        }
        if self.source_table is not None:
            result["source_table"] = self.source_table
        if self.observed is not None:
            result["observed"] = self.observed
        if self.expected is not None:
            result["expected"] = self.expected
        return result


@dataclass(frozen=True, slots=True)
class Diagnostic(_Contract):
    """A stable machine-readable diagnostic with no transport payload."""

    code: DiagnosticCode | str
    severity: DiagnosticSeverity | str
    message: str
    path: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = DIAGNOSTIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_schema_version(
            self.schema_version, "diagnostic.schema_version", DIAGNOSTIC_SCHEMA_VERSION
        )
        code = self.code.value if isinstance(self.code, DiagnosticCode) else self.code
        if not isinstance(code, str) or code not in DIAGNOSTIC_CODES:
            _fail("diagnostic.code", f"unsupported diagnostic code {code!r}")
        object.__setattr__(self, "code", code)
        severity = _enum_value(self.severity, DiagnosticSeverity, "diagnostic.severity")
        object.__setattr__(self, "severity", severity)
        _validate_safe_text(self.message, "diagnostic.message", max_length=2048)
        if self.path is not None:
            _validate_relative_path(self.path, "diagnostic.path")
        object.__setattr__(
            self, "details", _freeze_json_mapping(self.details, "diagnostic.details")
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Diagnostic:
        mapping = _require_mapping(value, "diagnostic")
        _reject_unknown_keys(
            mapping,
            {"schema_version", "code", "severity", "message", "path", "details"},
            "diagnostic",
        )
        _require_fields(mapping, "diagnostic", ("code", "severity", "message"))
        return cls(
            code=mapping["code"],
            severity=mapping["severity"],
            message=mapping["message"],
            path=mapping.get("path"),
            details=mapping.get("details", {}),
            schema_version=mapping.get("schema_version", DIAGNOSTIC_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            "schema_version": self.schema_version,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "details": _plain(self.details),
        }
        if self.path is not None:
            result["path"] = self.path
        return result


@dataclass(frozen=True, slots=True)
class ModelOutputRecord(_Contract):
    """A standalone model output identity and observed model3 references."""

    model_output_id: str
    model_bundle: BundleIdentity | Mapping[str, object]
    output_path: str
    file_references: Model3FileReferences | Mapping[str, object]
    metadata_version: str
    schema_version: int = MODEL_OUTPUT_SCHEMA_VERSION
    # Legacy serialized records may omit this additive field.  New records
    # produced by the index adapter always populate it with the path selected
    # relative to ``output_path``.
    model3_path: str | None = None

    def __post_init__(self) -> None:
        _validate_model_output_schema_version(
            self.schema_version,
            self.model3_path,
        )
        _validate_identifier(self.model_output_id, "model_output.model_output_id")
        bundle = _coerce(self.model_bundle, BundleIdentity, "model_output.model_bundle")
        references = _coerce(
            self.file_references, Model3FileReferences, "model_output.file_references"
        )
        object.__setattr__(self, "model_bundle", bundle)
        object.__setattr__(self, "file_references", references)
        _validate_relative_path(self.output_path, "model_output.output_path")
        if self.model3_path is not None:
            model3_path = _validate_relative_path(self.model3_path, "model_output.model3_path")
            if not model3_path.endswith(".model3.json"):
                _fail("model_output.model3_path", "must name a .model3.json file")
            object.__setattr__(self, "model3_path", model3_path)
        _validate_version_label(self.metadata_version, "model_output.metadata_version")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ModelOutputRecord:
        mapping = _require_mapping(value, "model_output")
        _reject_unknown_keys(
            mapping,
            {
                "schema_version",
                "model_output_id",
                "model_bundle",
                "output_path",
                "model3_path",
                "file_references",
                "metadata_version",
            },
            "model_output",
        )
        _require_fields(
            mapping,
            "model_output",
            (
                "model_output_id",
                "model_bundle",
                "output_path",
                "file_references",
                "metadata_version",
            ),
        )
        model3_path = mapping.get("model3_path")
        if "schema_version" not in mapping:
            schema_version = (
                MODEL_OUTPUT_SCHEMA_VERSION
                if model3_path is not None
                else LEGACY_MODEL_OUTPUT_SCHEMA_VERSION
            )
        else:
            schema_version = mapping["schema_version"]
        return cls(
            model_output_id=mapping["model_output_id"],
            model_bundle=mapping["model_bundle"],
            output_path=mapping["output_path"],
            model3_path=model3_path,
            file_references=mapping["file_references"],
            metadata_version=mapping["metadata_version"],
            schema_version=schema_version,
        )

    def to_dict(self) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            "schema_version": self.schema_version,
            "model_output_id": self.model_output_id,
            "model_bundle": self.model_bundle.to_dict(),
            "output_path": self.output_path,
            "file_references": self.file_references.to_dict(),
            "metadata_version": self.metadata_version,
        }
        if self.model3_path is not None:
            result["model3_path"] = self.model3_path
        return result


@dataclass(frozen=True, slots=True)
class SharedMotionSetRecord(_Contract):
    """A shared motion/facial output pair kept outside every model output."""

    motion_set_id: str
    motion_bundle: BundleIdentity | Mapping[str, object]
    motion_output_path: str
    facial_output_path: str
    known_clips: KnownClips | Mapping[str, object]
    metadata_version: str
    schema_version: int = MOTION_SET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_schema_version(
            self.schema_version, "motion_set.schema_version", MOTION_SET_SCHEMA_VERSION
        )
        _validate_identifier(self.motion_set_id, "motion_set.motion_set_id")
        bundle = _coerce(self.motion_bundle, BundleIdentity, "motion_set.motion_bundle")
        clips = _coerce(self.known_clips, KnownClips, "motion_set.known_clips")
        object.__setattr__(self, "motion_bundle", bundle)
        object.__setattr__(self, "known_clips", clips)
        _validate_relative_path(self.motion_output_path, "motion_set.motion_output_path")
        _validate_relative_path(self.facial_output_path, "motion_set.facial_output_path")
        if self.motion_output_path == self.facial_output_path:
            _fail("motion_set", "motion and facial outputs must remain physically separate")
        _validate_version_label(self.metadata_version, "motion_set.metadata_version")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SharedMotionSetRecord:
        mapping = _require_mapping(value, "motion_set")
        _reject_unknown_keys(
            mapping,
            {
                "schema_version",
                "motion_set_id",
                "motion_bundle",
                "motion_output_path",
                "facial_output_path",
                "known_clips",
                "metadata_version",
            },
            "motion_set",
        )
        _require_fields(
            mapping,
            "motion_set",
            (
                "motion_set_id",
                "motion_bundle",
                "motion_output_path",
                "facial_output_path",
                "known_clips",
                "metadata_version",
            ),
        )
        return cls(
            motion_set_id=mapping["motion_set_id"],
            motion_bundle=mapping["motion_bundle"],
            motion_output_path=mapping["motion_output_path"],
            facial_output_path=mapping["facial_output_path"],
            known_clips=mapping["known_clips"],
            metadata_version=mapping["metadata_version"],
            schema_version=mapping.get("schema_version", MOTION_SET_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.schema_version,
            "motion_set_id": self.motion_set_id,
            "motion_bundle": self.motion_bundle.to_dict(),
            "motion_output_path": self.motion_output_path,
            "facial_output_path": self.facial_output_path,
            "known_clips": self.known_clips.to_dict(),
            "metadata_version": self.metadata_version,
        }


@dataclass(frozen=True, slots=True)
class MotionSetCandidate(_Contract):
    """A candidate link whose status prevents naming evidence becoming fact."""

    motion_set_id: str
    motion_bundle: BundleIdentity | Mapping[str, object]
    status: CandidateStatus | str
    evidence: tuple[CandidateEvidence | Mapping[str, object], ...] = field(default_factory=tuple)
    schema_version: int = CANDIDATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_schema_version(
            self.schema_version, "candidate.schema_version", CANDIDATE_SCHEMA_VERSION
        )
        _validate_identifier(self.motion_set_id, "candidate.motion_set_id")
        bundle = _coerce(self.motion_bundle, BundleIdentity, "candidate.motion_bundle")
        status = _enum_value(self.status, CandidateStatus, "candidate.status")
        evidence = tuple(
            _coerce(item, CandidateEvidence, f"candidate.evidence[{index}]")
            for index, item in enumerate(_sequence(self.evidence, "candidate.evidence"))
        )
        _validate_unique(evidence, "candidate.evidence", key=lambda item: item.evidence_id)
        object.__setattr__(self, "motion_bundle", bundle)
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self, "evidence", tuple(sorted(evidence, key=lambda item: item.evidence_id))
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MotionSetCandidate:
        mapping = _require_mapping(value, "candidate")
        _reject_unknown_keys(
            mapping,
            {
                "schema_version",
                "motion_set_id",
                "motion_bundle",
                "status",
                "evidence",
            },
            "candidate",
        )
        _require_fields(mapping, "candidate", ("motion_set_id", "motion_bundle", "status"))
        return cls(
            motion_set_id=mapping["motion_set_id"],
            motion_bundle=mapping["motion_bundle"],
            status=mapping["status"],
            evidence=mapping.get("evidence", ()),
            schema_version=mapping.get("schema_version", CANDIDATE_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.schema_version,
            "motion_set_id": self.motion_set_id,
            "motion_bundle": self.motion_bundle.to_dict(),
            "status": self.status,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class Live2DModelAssociation(_Contract):
    """The model -> Character2D -> candidate portion of the index."""

    model_output_id: str
    model_bundle: BundleIdentity | Mapping[str, object]
    character2d_id: int | str | None
    character_id: int | str | None
    motion_sets: tuple[MotionSetCandidate | Mapping[str, object], ...] = field(
        default_factory=tuple
    )
    join_evidence: tuple[CandidateEvidence | Mapping[str, object], ...] = field(
        default_factory=tuple
    )
    schema_version: int = INDEX_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_schema_version(
            self.schema_version,
            "model_association.schema_version",
            MODEL_ASSOCIATION_SCHEMA_VERSION,
        )
        _validate_identifier(self.model_output_id, "model_association.model_output_id")
        bundle = _coerce(self.model_bundle, BundleIdentity, "model_association.model_bundle")
        candidates = tuple(
            _coerce(item, MotionSetCandidate, f"model_association.motion_sets[{index}]")
            for index, item in enumerate(
                _sequence(self.motion_sets, "model_association.motion_sets")
            )
        )
        _validate_unique(
            candidates, "model_association.motion_sets", key=lambda item: item.motion_set_id
        )
        join_evidence = tuple(
            _coerce(item, CandidateEvidence, f"model_association.join_evidence[{index}]")
            for index, item in enumerate(
                _sequence(self.join_evidence, "model_association.join_evidence")
            )
        )
        _validate_unique(
            join_evidence, "model_association.join_evidence", key=lambda item: item.evidence_id
        )
        object.__setattr__(self, "model_bundle", bundle)
        object.__setattr__(
            self,
            "character2d_id",
            _validate_optional_entity_id(self.character2d_id, "model_association.character2d_id"),
        )
        object.__setattr__(
            self,
            "character_id",
            _validate_optional_entity_id(self.character_id, "model_association.character_id"),
        )
        object.__setattr__(
            self,
            "motion_sets",
            tuple(sorted(candidates, key=lambda item: item.motion_set_id)),
        )
        object.__setattr__(
            self,
            "join_evidence",
            tuple(sorted(join_evidence, key=lambda item: item.evidence_id)),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Live2DModelAssociation:
        mapping = _require_mapping(value, "model_association")
        _reject_unknown_keys(
            mapping,
            {
                "schema_version",
                "model_output_id",
                "model_bundle",
                "character2d_id",
                "character_id",
                "motion_sets",
                "join_evidence",
            },
            "model_association",
        )
        _require_fields(mapping, "model_association", ("model_output_id", "model_bundle"))
        return cls(
            model_output_id=mapping["model_output_id"],
            model_bundle=mapping["model_bundle"],
            character2d_id=mapping.get("character2d_id"),
            character_id=mapping.get("character_id"),
            motion_sets=mapping.get("motion_sets", ()),
            join_evidence=mapping.get("join_evidence", ()),
            schema_version=mapping.get("schema_version", MODEL_ASSOCIATION_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.schema_version,
            "model_output_id": self.model_output_id,
            "model_bundle": self.model_bundle.to_dict(),
            "character2d_id": self.character2d_id,
            "character_id": self.character_id,
            "motion_sets": [item.to_dict() for item in self.motion_sets],
            "join_evidence": [item.to_dict() for item in self.join_evidence],
        }


def _coerce_contract_sequence(
    value: object, contract_type: type[_Contract], field_name: str
) -> tuple[_Contract, ...]:
    return tuple(
        _coerce(item, contract_type, f"{field_name}[{index}]")
        for index, item in enumerate(_sequence(value, field_name))
    )


def _validate_index_uniqueness(
    model_outputs: tuple[_Contract, ...],
    motion_sets: tuple[_Contract, ...],
    models: tuple[_Contract, ...],
    diagnostics: tuple[_Contract, ...],
) -> None:
    _validate_unique(model_outputs, "index.model_outputs", key=lambda item: item.model_output_id)
    _validate_unique(
        model_outputs,
        "index.model_outputs selections",
        key=lambda item: (item.output_path, item.model3_path),
    )
    seen_model_bundle_records: dict[str, tuple[str, set[str] | None]] = {}
    for record in model_outputs:
        previous = seen_model_bundle_records.get(record.model_bundle.name)
        if previous is None:
            if record.model3_path is None:
                seen_model_bundle_records[record.model_bundle.name] = (record.output_path, None)
            else:
                seen_model_bundle_records[record.model_bundle.name] = (
                    record.output_path,
                    {record.model3_path},
                )
            continue
        if (
            previous[0] != record.output_path
            or record.model3_path is None
            or previous[1] is None
            or record.model3_path in previous[1]
        ):
            _fail(
                "index.model_outputs bundles",
                f"duplicate identity {record.model_bundle.name!r}",
            )
        assert previous[1] is not None
        previous[1].add(record.model3_path)
    _validate_unique(motion_sets, "index.motion_sets", key=lambda item: item.motion_set_id)
    _validate_unique(
        motion_sets, "index.motion_sets bundles", key=lambda item: item.motion_bundle.name
    )
    _validate_unique(models, "index.models", key=lambda item: item.model_output_id)
    diagnostic_ids = tuple(
        (item.code, item.severity, item.message, item.path, _stable_object_bytes(item.details))
        for item in diagnostics
    )
    _validate_unique(diagnostic_ids, "index.diagnostics")


def _validate_motion_reference(
    candidate: MotionSetCandidate, motion_by_id: Mapping[str, SharedMotionSetRecord]
) -> None:
    referenced_motion = motion_by_id.get(candidate.motion_set_id)
    if referenced_motion is None:
        _integrity_fail(f"dangling motion-set reference {candidate.motion_set_id!r}")
    if candidate.motion_bundle != referenced_motion.motion_bundle:
        _integrity_fail(f"motion Bundle mismatch for {candidate.motion_set_id!r}")


def _validate_model_association_reference(
    association: Live2DModelAssociation,
    model_by_id: Mapping[str, ModelOutputRecord],
    motion_by_id: Mapping[str, SharedMotionSetRecord],
    metadata_version: str,
) -> None:
    referenced_model = model_by_id.get(association.model_output_id)
    if referenced_model is None:
        _integrity_fail(f"dangling model output reference {association.model_output_id!r}")
    if association.model_bundle != referenced_model.model_bundle:
        _integrity_fail(f"model bundle mismatch for {association.model_output_id!r}")
    if referenced_model.metadata_version != metadata_version:
        _integrity_fail(f"model metadata version mismatch for {association.model_output_id!r}")
    for candidate in association.motion_sets:
        _validate_motion_reference(candidate, motion_by_id)


def _validate_index_references(
    model_outputs: tuple[ModelOutputRecord, ...],
    motion_sets: tuple[SharedMotionSetRecord, ...],
    models: tuple[Live2DModelAssociation, ...],
    metadata_version: str,
) -> None:
    model_by_id = {item.model_output_id: item for item in model_outputs}
    motion_by_id = {item.motion_set_id: item for item in motion_sets}
    for association in models:
        _validate_model_association_reference(
            association, model_by_id, motion_by_id, metadata_version
        )


def _validate_index_record_versions(
    model_outputs: tuple[ModelOutputRecord, ...],
    motion_sets: tuple[SharedMotionSetRecord, ...],
    metadata_version: str,
) -> None:
    for record in model_outputs:
        if record.metadata_version != metadata_version:
            _integrity_fail(f"model metadata version mismatch for {record.model_output_id!r}")
        if record.schema_version == LEGACY_MODEL_OUTPUT_SCHEMA_VERSION:
            if record.model3_path is not None:
                _integrity_fail(
                    f"model output {record.model_output_id!r} has model3_path in schema version 1"
                )
        elif record.schema_version == MODEL_OUTPUT_SCHEMA_VERSION:
            if record.model3_path is None:
                _integrity_fail(
                    f"model output {record.model_output_id!r} is missing model3_path in schema version 2"
                )
        else:  # pragma: no cover - ModelOutputRecord validates this first.
            _integrity_fail(
                f"model output {record.model_output_id!r} has unsupported schema version "
                f"{record.schema_version!r}"
            )
    for record in motion_sets:
        if record.metadata_version != metadata_version:
            _integrity_fail(f"motion metadata version mismatch for {record.motion_set_id!r}")


def _validate_index_bundle_names(
    model_outputs: tuple[ModelOutputRecord, ...],
    motion_sets: tuple[SharedMotionSetRecord, ...],
) -> None:
    model_bundle_names = {record.model_bundle.name for record in model_outputs}
    motion_bundle_names = tuple(record.motion_bundle.name for record in motion_sets)
    _validate_unique(motion_bundle_names, "index Bundle identities")
    overlapping_names = model_bundle_names.intersection(motion_bundle_names)
    if overlapping_names:
        _fail(
            "index Bundle identities",
            f"duplicate identity {sorted(overlapping_names)[0]!r}",
        )


@dataclass(frozen=True, slots=True)
class Live2DIndex(_Contract):
    """The additive, separately versioned Live2D association index."""

    index_version: int
    metadata_version: str
    master_db_version: str
    model_outputs: tuple[ModelOutputRecord | Mapping[str, object], ...] = field(
        default_factory=tuple
    )
    motion_sets: tuple[SharedMotionSetRecord | Mapping[str, object], ...] = field(
        default_factory=tuple
    )
    models: tuple[Live2DModelAssociation | Mapping[str, object], ...] = field(default_factory=tuple)
    diagnostics: tuple[Diagnostic | Mapping[str, object], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_schema_version(self.index_version, "index_version", INDEX_SCHEMA_VERSION)
        _validate_version_label(self.metadata_version, "metadata_version")
        _validate_version_label(self.master_db_version, "master_db_version")

        model_outputs = _coerce_contract_sequence(
            self.model_outputs, ModelOutputRecord, "index.model_outputs"
        )
        motion_sets = _coerce_contract_sequence(
            self.motion_sets, SharedMotionSetRecord, "index.motion_sets"
        )
        models = _coerce_contract_sequence(self.models, Live2DModelAssociation, "index.models")
        diagnostics = _coerce_contract_sequence(self.diagnostics, Diagnostic, "index.diagnostics")

        _validate_index_uniqueness(model_outputs, motion_sets, models, diagnostics)
        _validate_index_references(model_outputs, motion_sets, models, self.metadata_version)
        _validate_index_record_versions(model_outputs, motion_sets, self.metadata_version)
        _validate_index_bundle_names(model_outputs, motion_sets)

        object.__setattr__(
            self,
            "model_outputs",
            tuple(sorted(model_outputs, key=lambda item: item.model_output_id)),
        )
        object.__setattr__(
            self, "motion_sets", tuple(sorted(motion_sets, key=lambda item: item.motion_set_id))
        )
        object.__setattr__(
            self, "models", tuple(sorted(models, key=lambda item: item.model_output_id))
        )
        object.__setattr__(
            self,
            "diagnostics",
            tuple(
                sorted(
                    diagnostics,
                    key=lambda item: (
                        item.code,
                        item.severity,
                        item.path or "",
                        item.message,
                        _stable_object_bytes(item.details),
                    ),
                )
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Live2DIndex:
        mapping = _require_mapping(value, "index")
        _reject_unknown_keys(
            mapping,
            {
                "index_version",
                "metadata_version",
                "master_db_version",
                "model_outputs",
                "motion_sets",
                "models",
                "diagnostics",
            },
            "index",
        )
        _require_fields(mapping, "index", ("metadata_version", "master_db_version"))
        return cls(
            index_version=mapping.get("index_version", INDEX_SCHEMA_VERSION),
            metadata_version=mapping["metadata_version"],
            master_db_version=mapping["master_db_version"],
            model_outputs=mapping.get("model_outputs", ()),
            motion_sets=mapping.get("motion_sets", ()),
            models=mapping.get("models", ()),
            diagnostics=mapping.get("diagnostics", ()),
        )

    @classmethod
    def from_json_bytes(cls, value: bytes) -> Live2DIndex:
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise Live2DContractError("index JSON is invalid") from exc
        return cls.from_dict(_require_mapping(decoded, "index"))

    def validate(self) -> Live2DIndex:
        """Return this already-validated immutable index for fluent callers."""

        return self

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "index_version": self.index_version,
            "metadata_version": self.metadata_version,
            "master_db_version": self.master_db_version,
            "model_outputs": [item.to_dict() for item in self.model_outputs],
            "motion_sets": [item.to_dict() for item in self.motion_sets],
            "models": [item.to_dict() for item in self.models],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def is_live2d_bundle(bundle: Mapping[str, object]) -> bool:
    """Reuse the existing explicit ``live2d/`` Bundle namespace check."""

    return _is_live2d_bundle(bundle)


def is_live2d_scope(bundle: Mapping[str, object]) -> bool:
    """Readable alias for the explicit Live2D Bundle scope predicate."""

    return is_live2d_bundle(bundle)


def to_json_dict(contract: _Contract) -> dict[str, JSONValue]:
    """Return a JSON-safe dictionary containing only contract fields."""

    if not isinstance(contract, _Contract):
        raise TypeError("expected a Live2D contract")
    return _plain(contract.to_dict())


def canonical_json_bytes(contract: _Contract) -> bytes:
    """Serialize a contract deterministically for atomic file publication."""

    return json.dumps(
        to_json_dict(contract),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json(contract: _Contract) -> str:
    """Return the deterministic UTF-8 JSON representation as text."""

    return canonical_json_bytes(contract).decode("utf-8")


def validate_model_output(value: ModelOutputRecord | Mapping[str, object]) -> ModelOutputRecord:
    if isinstance(value, Mapping):
        return ModelOutputRecord.from_dict(value)
    if isinstance(value, ModelOutputRecord):
        return value.validate()
    raise TypeError("expected a ModelOutputRecord or an object")


def validate_motion_set(
    value: SharedMotionSetRecord | Mapping[str, object],
) -> SharedMotionSetRecord:
    if isinstance(value, Mapping):
        return SharedMotionSetRecord.from_dict(value)
    if isinstance(value, SharedMotionSetRecord):
        return value.validate()
    raise TypeError("expected a SharedMotionSetRecord or an object")


def validate_candidate(
    value: MotionSetCandidate | Mapping[str, object],
) -> MotionSetCandidate:
    if isinstance(value, Mapping):
        return MotionSetCandidate.from_dict(value)
    if isinstance(value, MotionSetCandidate):
        return value.validate()
    raise TypeError("expected a MotionSetCandidate or an object")


def validate_index(value: Live2DIndex | Mapping[str, object]) -> Live2DIndex:
    if isinstance(value, Mapping):
        return Live2DIndex.from_dict(value)
    if isinstance(value, Live2DIndex):
        return value.validate()
    raise TypeError("expected a Live2DIndex or an object")


# Small aliases keep the public vocabulary usable without introducing duplicate
# implementations or additional schema versions.
ModelOutput = ModelOutputRecord
SharedMotionSet = SharedMotionSetRecord
Candidate = MotionSetCandidate
ModelAssociation = Live2DModelAssociation
AssociationIndex = Live2DIndex
Live2DAssociationIndex = Live2DIndex
