"""Pure L2D-1 master-table joins and naming-based candidate construction.

This module consumes already-sanitized records and business-table rows.  It does
not fetch metadata, inspect Bundles, or publish outputs.  The only relation that
is treated as authoritative is ``costume2ds.character2dId -> character2ds.id``;
motion Bundle links remain auditable candidates.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from updater.live2d.contracts import (
    CandidateEvidence,
    CandidateStatus,
    Diagnostic,
    DiagnosticCode,
    DiagnosticSeverity,
    Live2DIndex,
    Live2DModelAssociation,
    ModelOutputRecord,
    MotionSetCandidate,
    SharedMotionSetRecord,
)

LIVE2D_TABLE_NAMES = (
    "character2ds",
    "costume2ds",
    "systemLive2ds",
    "bondsLive2ds",
    "bondsRankUpLive2ds",
    "loginBonusLive2ds",
)

_ROW_FIELDS = (
    "id",
    "characterId",
    "character2dId",
    "assetName",
    "bundleName",
    "motion",
    "expression",
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]*$")
_V2_PREFIX_RE = re.compile(r"^(\d+)(.+)$")
_MOTION_SUFFIX = "_motion_base"
_BUSINESS_ROLE_RULE = (
    "characterId + motion + expression records character-level role use; "
    "expression is a business facial clip name, not a Cubism Expressions/.exp3 reference"
)


@dataclass(frozen=True)
class _CharacterContext:
    row: Mapping[str, object]
    character2d_id: int | str | None
    character_id: int | str | None
    exact_join: bool
    join_ambiguous: bool


@dataclass(frozen=True)
class _PreparedTables:
    rows: Mapping[str, tuple[Mapping[str, object], ...]]
    role_rows_by_character_id: Mapping[
        tuple[str, int | str], tuple[tuple[str, Mapping[str, object]], ...]
    ]
    diagnostics: tuple[Diagnostic, ...]


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_digest(row: Mapping[str, object]) -> str:
    """Return a non-reversible identity for diagnostics, never the row itself."""

    safe_values: dict[str, object] = {}
    for key in _ROW_FIELDS:
        if key not in row:
            continue
        value = row[key]
        if value is None or isinstance(value, (bool, int, str)):
            safe_values[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            safe_values[key] = value
        else:
            safe_values[key] = type(value).__name__
    return hashlib.sha256(_stable_json(safe_values).encode("utf-8")).hexdigest()[:16]


def _append_diagnostic(
    diagnostics: list[Diagnostic],
    seen: set[str],
    *,
    code: DiagnosticCode,
    severity: DiagnosticSeverity,
    message: str,
    path: str,
    details: Mapping[str, object] | None = None,
) -> None:
    diagnostic = Diagnostic(
        code=code,
        severity=severity,
        message=message,
        path=path,
        details=dict(details or {}),
    )
    identity = _stable_json(diagnostic.to_dict())
    if identity not in seen:
        diagnostics.append(diagnostic)
        seen.add(identity)


def _selected_row(row: Mapping[str, object]) -> dict[str, object]:
    return {key: row[key] for key in _ROW_FIELDS if key in row}


def _sanitize_row(table_name: str, row: object) -> Mapping[str, object] | None:
    if not isinstance(row, Mapping):
        return None
    selected = _selected_row(row)
    try:
        # CandidateEvidence owns the L2D-0 scalar, control-character, and
        # transport-data checks.  Only the allowlisted business fields reach it.
        evidence = CandidateEvidence(
            source="master_table",
            source_table=table_name,
            rule="sanitized business-table row",
            source_row=selected,
        )
    except ValueError:
        return None
    return dict(evidence.source_row)


def _prepare_tables(tables: Mapping[str, object]) -> _PreparedTables:
    if not isinstance(tables, Mapping):
        raise ValueError("tables must be a mapping of the six Live2D business tables")
    unknown = sorted(set(tables) - set(LIVE2D_TABLE_NAMES))
    if unknown:
        raise ValueError(f"unsupported Live2D business tables: {', '.join(unknown)}")

    diagnostics: list[Diagnostic] = []
    seen_diagnostics: set[str] = set()
    prepared: dict[str, tuple[Mapping[str, object], ...]] = {}
    for table_name in LIVE2D_TABLE_NAMES:
        if table_name not in tables:
            _append_diagnostic(
                diagnostics,
                seen_diagnostics,
                code=DiagnosticCode.LIVE2D_JOIN_MISSING,
                severity=DiagnosticSeverity.ERROR,
                message="Required Live2D business table is missing",
                path=f"tables/{table_name}",
                details={"reason": "missing_table"},
            )
            prepared[table_name] = ()
            continue

        table_rows = tables[table_name]
        if not isinstance(table_rows, (list, tuple)):
            raise ValueError(f"tables.{table_name} must be a sequence of row mappings")

        valid_rows: list[Mapping[str, object]] = []
        for raw_row in table_rows:
            sanitized = _sanitize_row(table_name, raw_row)
            if sanitized is None:
                raw_mapping = raw_row if isinstance(raw_row, Mapping) else {}
                _append_diagnostic(
                    diagnostics,
                    seen_diagnostics,
                    code=DiagnosticCode.LIVE2D_JOIN_MISSING,
                    severity=DiagnosticSeverity.ERROR,
                    message="Malformed sanitized business-table row was ignored",
                    path=f"tables/{table_name}",
                    details={
                        "reason": "malformed_row",
                        "row_id": _row_digest(raw_mapping),
                    },
                )
                continue
            valid_rows.append(sanitized)

        valid_rows.sort(key=_stable_json)
        prepared[table_name] = tuple(valid_rows)

    role_rows: dict[
        tuple[str, int | str], list[tuple[str, Mapping[str, object]]]
    ] = {}
    for table_name in LIVE2D_TABLE_NAMES:
        for row in prepared[table_name]:
            if "motion" not in row and "expression" not in row:
                continue
            character_id = _entity_id(row.get("characterId"))
            motion_name = _string_value(row, "motion")
            expression_name = _string_value(row, "expression")
            if character_id is None or motion_name is None or expression_name is None:
                _append_diagnostic(
                    diagnostics,
                    seen_diagnostics,
                    code=DiagnosticCode.LIVE2D_JOIN_MISSING,
                    severity=DiagnosticSeverity.ERROR,
                    message="Malformed character role-use row was ignored",
                    path=f"tables/{table_name}",
                    details={
                        "reason": "malformed_role_row",
                        "row_id": _row_digest(row),
                    },
                )
                continue
            key = _entity_key(character_id)
            if key is not None:
                role_rows.setdefault(key, []).append((table_name, row))

    indexed_roles = {
        key: tuple(sorted(rows, key=lambda item: (item[0], _stable_json(item[1]))))
        for key, rows in role_rows.items()
    }
    return _PreparedTables(prepared, indexed_roles, tuple(diagnostics))


def _coerce_records(
    values: object,
    record_type: type[ModelOutputRecord] | type[SharedMotionSetRecord],
    field_name: str,
) -> tuple[ModelOutputRecord, ...] | tuple[SharedMotionSetRecord, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field_name} must be a sequence of contract records")
    records: list[Any] = []
    for index, value in enumerate(values):
        if isinstance(value, record_type):
            records.append(value)
        elif isinstance(value, Mapping):
            try:
                records.append(record_type.from_dict(value))
            except ValueError as exc:
                raise ValueError(f"{field_name}[{index}] is not a valid contract record") from exc
        else:
            raise ValueError(f"{field_name}[{index}] must be a contract record or mapping")
    return tuple(sorted(records, key=lambda record: record.model_output_id if isinstance(record, ModelOutputRecord) else record.motion_set_id))


def _entity_id(value: object) -> int | str | None:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and _ID_RE.fullmatch(value):
        return value
    return None


def _entity_key(value: object) -> tuple[str, int | str] | None:
    entity = _entity_id(value)
    if entity is None:
        return None
    return ("int", entity) if type(entity) is int else ("str", entity)


def _same_entity(left: object, right: object) -> bool:
    left_key = _entity_key(left)
    right_key = _entity_key(right)
    return left_key is not None and left_key == right_key


def _string_value(row: Mapping[str, object], key: str) -> str | None:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        return None
    return value


def _model_leaf(model: ModelOutputRecord) -> str:
    return model.model_bundle.name.rsplit("/", 1)[-1]


def _model_variant_matches(model_leaf: str, asset_name: str) -> bool:
    if not model_leaf.startswith("v2_"):
        return False
    match = _V2_PREFIX_RE.fullmatch(model_leaf[3:])
    return bool(match and match.group(2).startswith(f"{asset_name}_"))


def _motion_numeric_prefix(motion: SharedMotionSetRecord) -> str | None:
    leaf = motion.motion_bundle.name.rsplit("/", 1)[-1]
    if not leaf.startswith("v2_"):
        return None
    match = _V2_PREFIX_RE.fullmatch(leaf[3:])
    return match.group(1) if match else None


def _motion_match_kind(
    motion: SharedMotionSetRecord,
    context: _CharacterContext,
) -> str | None:
    asset_name = _string_value(context.row, "assetName")
    if asset_name is None:
        return None
    leaf = motion.motion_bundle.name.rsplit("/", 1)[-1]
    if not leaf.startswith("v2_"):
        return None
    prefix_match = _V2_PREFIX_RE.fullmatch(leaf[3:])
    if not prefix_match:
        return None
    numeric_prefix, tail = prefix_match.groups()
    if not tail.startswith(f"{asset_name}_"):
        return None
    expected_character_id: int | None = None
    if type(context.character_id) is int:
        expected_character_id = context.character_id
    elif isinstance(context.character_id, str) and context.character_id.isdigit():
        expected_character_id = int(context.character_id)
    if expected_character_id is not None and int(numeric_prefix) != expected_character_id:
        return "character_id_mismatch" if context.exact_join else None

    rest = tail[len(asset_name) :]
    if rest == _MOTION_SUFFIX:
        return "normal"
    if not rest.endswith(_MOTION_SUFFIX) or not rest.startswith("_"):
        return None
    variant_tokens = rest[1 : -len(_MOTION_SUFFIX)].split("_")
    if "back" in variant_tokens or "still" in variant_tokens:
        return "protected"
    return "variant"


def _context_key(context: _CharacterContext) -> str:
    return _stable_json(
        {
            "row": dict(context.row),
            "character2d_id": context.character2d_id,
            "character_id": context.character_id,
            "exact_join": context.exact_join,
            "join_ambiguous": context.join_ambiguous,
        }
    )


def _evidence(
    *,
    source: str,
    rule: str,
    source_table: str | None,
    source_row: Mapping[str, object],
    observed: str | None = None,
    expected: str | None = None,
) -> CandidateEvidence:
    return CandidateEvidence(
        source=source,
        rule=rule,
        source_table=source_table,
        source_row=source_row,
        observed=observed,
        expected=expected,
    )


def _unique_evidence(values: list[CandidateEvidence]) -> list[CandidateEvidence]:
    unique: dict[str, CandidateEvidence] = {}
    for value in values:
        if value.evidence_id is not None:
            unique[value.evidence_id] = value
    return [unique[key] for key in sorted(unique)]


def _join_evidence(
    matching_costumes: list[Mapping[str, object]],
    character_rows: list[Mapping[str, object]],
) -> list[CandidateEvidence]:
    evidence: list[CandidateEvidence] = []
    for row in matching_costumes:
        evidence.append(
            _evidence(
                source="master_table",
                source_table="costume2ds",
                source_row=row,
                rule="Exact costume2ds.character2dId -> character2ds.id join anchor",
                observed=str(row.get("character2dId"))
                if row.get("character2dId") is not None
                else None,
            )
        )
    for row in character_rows:
        evidence.append(
            _evidence(
                source="master_table",
                source_table="character2ds",
                source_row=row,
                rule="Exact costume2ds.character2dId -> character2ds.id join target",
                observed=str(row.get("id")) if row.get("id") is not None else None,
            )
        )
    return _unique_evidence(evidence)


def _role_evidence(
    context: _CharacterContext,
    prepared: _PreparedTables,
) -> list[CandidateEvidence]:
    if context.character_id is None:
        return []
    key = _entity_key(context.character_id)
    if key is None:
        return []
    return [
        _evidence(
            source="master_table",
            source_table=table_name,
            source_row=row,
            rule=_BUSINESS_ROLE_RULE,
            observed=row["motion"],
            expected=row["expression"],
        )
        for table_name, row in prepared.role_rows_by_character_id.get(key, ())
    ]


def _model_contexts(
    model: ModelOutputRecord,
    prepared: _PreparedTables,
    diagnostics: list[Diagnostic],
    seen_diagnostics: set[str],
) -> tuple[
    int | str | None,
    int | str | None,
    tuple[_CharacterContext, ...],
    tuple[CandidateEvidence, ...],
]:
    model_path = f"models/{model.model_output_id}"
    model_leaf = _model_leaf(model)
    costume_rows = prepared.rows["costume2ds"]
    character_rows = prepared.rows["character2ds"]
    matching_costumes = [
        row
        for row in costume_rows
        if row.get("assetName") == model_leaf
        or row.get("bundleName") in {model.model_bundle.name, model_leaf}
    ]
    matching_costumes.sort(key=_stable_json)

    join_ambiguous = len(matching_costumes) > 1
    if join_ambiguous:
        row_character2d_ids = [
            _entity_id(row.get("character2dId")) for row in matching_costumes
        ]
        valid_character2d_ids = [
            character2d_id
            for character2d_id in row_character2d_ids
            if character2d_id is not None
        ]
        duplicate_reason = (
            "duplicate_character2d_id"
            if len(valid_character2d_ids) == len(row_character2d_ids)
            and len({_entity_key(value) for value in valid_character2d_ids})
            < len(valid_character2d_ids)
            else "duplicate_costume_match"
        )
        _append_diagnostic(
            diagnostics,
            seen_diagnostics,
            code=DiagnosticCode.LIVE2D_MAPPING_AMBIGUOUS,
            severity=DiagnosticSeverity.ERROR,
            message="Multiple costume2ds rows match the model Bundle",
            path=model_path,
            details={"match_count": len(matching_costumes), "reason": duplicate_reason},
        )
    elif not matching_costumes:
        _append_diagnostic(
            diagnostics,
            seen_diagnostics,
            code=DiagnosticCode.LIVE2D_JOIN_MISSING,
            severity=DiagnosticSeverity.ERROR,
            message="No exact costume2ds row matches the model Bundle leaf",
            path=model_path,
            details={"bundle_leaf": model_leaf},
        )

    row_character2d_ids = [
        _entity_id(row.get("character2dId")) for row in matching_costumes
    ]
    invalid_character2d_rows = sum(
        character2d_id is None for character2d_id in row_character2d_ids
    )
    costume_ids = [
        character2d_id
        for character2d_id in row_character2d_ids
        if character2d_id is not None
    ]
    costume_ids = list(dict.fromkeys(costume_ids))
    if invalid_character2d_rows:
        _append_diagnostic(
            diagnostics,
            seen_diagnostics,
            code=DiagnosticCode.LIVE2D_JOIN_MISSING,
            severity=DiagnosticSeverity.ERROR,
            message="A matching costume2ds row has no valid character2dId",
            path=model_path,
            details={
                "reason": "missing_character2d_id",
                "invalid_row_count": invalid_character2d_rows,
            },
        )

    contexts: list[_CharacterContext] = []
    joined_character_rows: list[Mapping[str, object]] = []
    association_character2d_id: int | str | None = None
    association_character_id: int | str | None = None

    if len(matching_costumes) == 1 and len(costume_ids) == 1:
        association_character2d_id = costume_ids[0]
    for character2d_id in costume_ids:
        matches = [
            row
            for row in character_rows
            if _same_entity(row.get("id"), character2d_id)
        ]
        matches.sort(key=_stable_json)
        joined_character_rows.extend(matches)
        if not matches:
            _append_diagnostic(
                diagnostics,
                seen_diagnostics,
                code=DiagnosticCode.LIVE2D_JOIN_MISSING,
                severity=DiagnosticSeverity.ERROR,
                message="No character2ds row satisfies the exact character2dId join",
                path=model_path,
                details={"character2d_id": character2d_id},
            )
        elif len(matches) > 1:
            join_ambiguous = True
            _append_diagnostic(
                diagnostics,
                seen_diagnostics,
                code=DiagnosticCode.LIVE2D_MAPPING_AMBIGUOUS,
                severity=DiagnosticSeverity.ERROR,
                message="Multiple character2ds rows satisfy the exact character2dId join",
                path=model_path,
                details={"character2d_id": character2d_id, "match_count": len(matches)},
            )
        if len(matches) == 1:
            character_id = _entity_id(matches[0].get("characterId"))
            if character_id is None:
                _append_diagnostic(
                    diagnostics,
                    seen_diagnostics,
                    code=DiagnosticCode.LIVE2D_JOIN_MISSING,
                    severity=DiagnosticSeverity.ERROR,
                    message="Joined character2ds row has no valid characterId",
                    path=model_path,
                    details={"character2d_id": character2d_id},
                )
            elif len(matching_costumes) == 1 and len(costume_ids) == 1:
                association_character_id = character_id
        for row in matches:
            character_id = _entity_id(row.get("characterId"))
            if _string_value(row, "assetName") is not None:
                contexts.append(
                    _CharacterContext(
                        row=row,
                        character2d_id=character2d_id,
                        character_id=character_id,
                        exact_join=(
                            len(matching_costumes) == 1
                            and len(costume_ids) == 1
                            and len(matches) == 1
                            and character_id is not None
                        ),
                        join_ambiguous=join_ambiguous,
                    )
                )

    # A missing/ambiguous costume join may still have useful character-level
    # naming evidence.  This fallback is deliberately candidate-only and never
    # fills the association's Character2D or character identifiers.
    if not contexts or join_ambiguous or not matching_costumes:
        for row in character_rows:
            asset_name = _string_value(row, "assetName")
            if asset_name is None or not _model_variant_matches(model_leaf, asset_name):
                continue
            contexts.append(
                _CharacterContext(
                    row=row,
                    character2d_id=_entity_id(row.get("id")),
                    character_id=_entity_id(row.get("characterId")),
                    exact_join=False,
                    join_ambiguous=join_ambiguous,
                )
            )

    unique_contexts: dict[str, _CharacterContext] = {}
    for context in contexts:
        unique_contexts[_context_key(context)] = context
    contexts = sorted(unique_contexts.values(), key=_context_key)

    join_evidence = _join_evidence(matching_costumes, joined_character_rows)
    return (
        association_character2d_id,
        association_character_id,
        tuple(contexts),
        tuple(join_evidence),
    )


def _build_model_candidates(
    model: ModelOutputRecord,
    contexts: tuple[_CharacterContext, ...],
    join_evidence: tuple[CandidateEvidence, ...],
    motion_sets: tuple[SharedMotionSetRecord, ...],
    prepared: _PreparedTables,
    diagnostics: list[Diagnostic],
    seen_diagnostics: set[str],
) -> tuple[MotionSetCandidate, ...]:
    grouped: dict[str, dict[str, object]] = {}
    for context in contexts:
        for motion in motion_sets:
            match_kind = _motion_match_kind(motion, context)
            if match_kind == "character_id_mismatch":
                _append_diagnostic(
                    diagnostics,
                    seen_diagnostics,
                    code=DiagnosticCode.LIVE2D_MAPPING_AMBIGUOUS,
                    severity=DiagnosticSeverity.WARNING,
                    message="Joined characterId disagrees with the motion Bundle numeric prefix",
                    path=f"models/{model.model_output_id}",
                    details={
                        "motion_set_id": motion.motion_set_id,
                        "character_id": context.character_id,
                        "bundle_prefix": _motion_numeric_prefix(motion),
                        "reason": "character_id_prefix_mismatch",
                    },
                )
                continue
            if match_kind is None:
                continue
            group = grouped.setdefault(
                motion.motion_set_id,
                {"motion": motion, "kind": match_kind, "contexts": []},
            )
            if match_kind != "normal":
                group["kind"] = match_kind
            group["contexts"].append(context)

    candidates: list[MotionSetCandidate] = []
    model_path = f"models/{model.model_output_id}"
    for motion_set_id in sorted(grouped):
        group = grouped[motion_set_id]
        motion = group["motion"]
        match_kind = group["kind"]
        group_contexts: list[_CharacterContext] = []
        seen_contexts: set[str] = set()
        for context in group["contexts"]:
            context_key = _context_key(context)
            if context_key not in seen_contexts:
                group_contexts.append(context)
                seen_contexts.add(context_key)
        group_contexts.sort(key=_context_key)

        evidence: list[CandidateEvidence] = list(join_evidence)
        for context in group_contexts:
            asset_name = _string_value(context.row, "assetName") or ""
            motion_leaf = motion.motion_bundle.name.rsplit("/", 1)[-1]
            if match_kind == "protected":
                naming_rule = (
                    "back/still motion Bundle variants remain ambiguous and are protected "
                    "from automatic character attribution"
                )
            elif match_kind == "variant":
                naming_rule = (
                    "non-base motion Bundle naming supplies candidate evidence only and "
                    "does not verify a model-variant relation"
                )
            elif context.exact_join and not context.join_ambiguous:
                naming_rule = (
                    "character2ds.assetName matches the exact character segment of the "
                    "v2_* motion Bundle name; this is a derived candidate, not a direct "
                    "model-variant relation"
                )
            else:
                naming_rule = (
                    "model/character naming supplies candidate evidence only; no exact "
                    "costume2ds.character2dId -> character2ds.id join verifies this model variant"
                )
            evidence.append(
                _evidence(
                    source="naming",
                    source_table="character2ds",
                    source_row=context.row,
                    rule=naming_rule,
                    observed=asset_name,
                    expected=motion_leaf,
                )
            )
            evidence.extend(_role_evidence(context, prepared))

        evidence.append(
            _evidence(
                source="bundle_metadata",
                rule="Candidate references the supplied SharedMotionSetRecord Bundle identity",
                source_table=None,
                source_row={"bundleName": motion.motion_bundle.name},
                observed=motion.motion_set_id,
            )
        )
        evidence = _unique_evidence(evidence)

        exact_derived = (
            match_kind == "normal"
            and len(group_contexts) == 1
            and group_contexts[0].exact_join
            and not group_contexts[0].join_ambiguous
        )
        status = CandidateStatus.DERIVED.value if exact_derived else CandidateStatus.AMBIGUOUS.value
        if match_kind != "normal" or len(group_contexts) > 1 or any(
            context.join_ambiguous for context in group_contexts
        ):
            reason = "protected_motion_variant" if match_kind == "protected" else "ambiguous_mapping"
            _append_diagnostic(
                diagnostics,
                seen_diagnostics,
                code=DiagnosticCode.LIVE2D_MAPPING_AMBIGUOUS,
                severity=DiagnosticSeverity.WARNING,
                message="Motion-set ownership remains an auditable candidate",
                path=model_path,
                details={"motion_set_id": motion_set_id, "reason": reason},
            )

        candidates.append(
            MotionSetCandidate(
                motion_set_id=motion.motion_set_id,
                motion_bundle=motion.motion_bundle,
                status=status,
                evidence=evidence,
            )
        )
    return tuple(candidates)


def build_live2d_index(
    *,
    metadata_version: str,
    master_db_version: str,
    model_outputs: object,
    motion_sets: object,
    tables: Mapping[str, object],
) -> Live2DIndex:
    """Build a deterministic L2D-1 association index from sanitized inputs."""

    coerced_models = _coerce_records(model_outputs, ModelOutputRecord, "model_outputs")
    coerced_motion_sets = _coerce_records(motion_sets, SharedMotionSetRecord, "motion_sets")
    prepared = _prepare_tables(tables)
    diagnostics = list(prepared.diagnostics)
    seen_diagnostics = {_stable_json(item.to_dict()) for item in diagnostics}

    associations: list[Live2DModelAssociation] = []
    for model in coerced_models:
        character2d_id, character_id, contexts, join_evidence = _model_contexts(
            model,
            prepared,
            diagnostics,
            seen_diagnostics,
        )
        candidates = _build_model_candidates(
            model,
            contexts,
            join_evidence,
            coerced_motion_sets,
            prepared,
            diagnostics,
            seen_diagnostics,
        )
        associations.append(
            Live2DModelAssociation(
                model_output_id=model.model_output_id,
                model_bundle=model.model_bundle,
                character2d_id=character2d_id,
                character_id=character_id,
                motion_sets=candidates,
                join_evidence=join_evidence,
            )
        )

    return Live2DIndex(
        index_version=1,
        metadata_version=metadata_version,
        master_db_version=master_db_version,
        model_outputs=coerced_models,
        motion_sets=coerced_motion_sets,
        models=tuple(associations),
        diagnostics=tuple(diagnostics),
    )
