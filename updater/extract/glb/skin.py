"""Skeleton and skin-weight extraction for streaming Live GLB export (#39).

A skinned scene node's ``bones`` list names Transforms that deform its mesh.
The binding's scene graph is keyed by GameObject identities, so every bone
Transform is resolved through its typetree and mapped to its
``m_GameObject`` identity — the exported scene node.  Each joint gets an
inverse bind matrix: its world matrix in glTF space — composed from
X-mirrored local transforms up the ``m_Father`` chain — inverted, which by
the mirror-conjugation identity stays consistent with the mirrored exported
hierarchy.  Per-vertex joint/weight data arrives through a provider
pipeline: the bundled binding-backed provider resolves the full skeleton
but records a structured diagnostic instead of weights, because unity_rs
0.5.x exposes no weight streams (its FBX exporter fails on skinned meshes
and raw vertex channels carry stream offsets, not samples).  Converted
GLBs therefore keep the animated skeleton, and the diagnostic names
exactly what is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from updater.extract.glb.contracts import DIAG_MISSING_TARGET, DIAG_SKIN_WEIGHTS_UNAVAILABLE
from updater.extract.glb.math3d import (
    compose_trs,
    invert_mat4,
    mirror_position,
    mirror_rotation,
    multiply_mat4,
)
from updater.unity_rs_adapter import CollectionFileTable, UnityRsEnvironment

JointIdentity = tuple[int, int]

TRANSFORM_OBJECT_CLASS = 4


@dataclass(frozen=True, slots=True)
class SkinDiagnostic:
    """One structured skeleton/skin conversion diagnostic."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class SkinJoint:
    """One resolved joint with its glTF-space world matrix."""

    identity: JointIdentity
    name: str
    world_matrix: tuple[float, ...]
    inverse_bind_matrix: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Skeleton:
    """Resolved joint chain for one skinned node."""

    joints: tuple[SkinJoint, ...]
    diagnostics: tuple[SkinDiagnostic, ...] = ()

    @property
    def inverse_bind_matrices(self) -> tuple[tuple[float, ...], ...]:
        return tuple(joint.inverse_bind_matrix for joint in self.joints)


@dataclass(frozen=True, slots=True)
class SkinWeights:
    """Provider result for one skinned node."""

    skeleton: Skeleton
    vertex_joints: tuple[tuple[int, int, int, int], ...] | None = None
    vertex_weights: tuple[tuple[float, float, float, float], ...] | None = None
    diagnostics: tuple[SkinDiagnostic, ...] = ()

    @property
    def weights_available(self) -> bool:
        return self.vertex_joints is not None and self.vertex_weights is not None


class SkinWeightsProvider(Protocol):
    """Source of per-vertex joint/weight data for one skinned node."""

    def skin_weights(self, node: Any, scene_nodes: Sequence[Any]) -> SkinWeights:
        """Return the weights for ``node``; see SkinWeights."""


class _SkeletonResolver:
    """Composes glTF-space world matrices for Transform identities.

    Transforms resolve through typetrees (the scene graph is GameObject
    keyed); results cache per transform path ID so shared parents compose
    once.
    """

    def __init__(
        self,
        environment: UnityRsEnvironment,
        table: CollectionFileTable,
        source_file_index: int,
    ) -> None:
        self._environment = environment
        self._table = table
        self._source_file_index = source_file_index
        self._world_cache: dict[int, tuple[float, ...]] = {}
        self._in_flight: set[int] = set()

    def world_of(self, transform_identity: JointIdentity) -> tuple[float, ...] | None:
        """Return the mirrored glTF world matrix of one Transform, or
        ``None`` when it cannot be resolved."""

        cached = self._world_cache.get(transform_identity[1])
        if cached is not None:
            return cached
        if transform_identity[1] in self._in_flight:
            return None  # parent cycle: stop composing at the cycle
        transform = self._environment.object_by_identity(*transform_identity)
        if transform is None or transform.class_id != TRANSFORM_OBJECT_CLASS:
            return None
        typetree = transform.read_typetree()
        local = compose_trs(
            mirror_position(_local_position(typetree)),
            mirror_rotation(_local_rotation(typetree)),
            _local_scale(typetree),
        )
        self._in_flight.add(transform_identity[1])
        try:
            father = typetree.get("m_Father") or {}
            father_identity = (
                self._resolve_file(int(father.get("m_FileID", 0))),
                int(father.get("m_PathID", 0)),
            )
            parent_world = self.world_of(father_identity) if father_identity[1] != 0 else None
            world = multiply_mat4(parent_world, local) if parent_world is not None else local
        finally:
            self._in_flight.discard(transform_identity[1])
        self._world_cache[transform_identity[1]] = world
        return world

    def game_object_identity(self, transform_identity: JointIdentity) -> JointIdentity | None:
        """Map one Transform identity to its GameObject identity."""

        transform = self._environment.object_by_identity(*transform_identity)
        if transform is None or transform.class_id != TRANSFORM_OBJECT_CLASS:
            return None
        pointer = (transform.read_typetree().get("m_GameObject") or {}).get("m_PathID", 0)
        return (transform_identity[0], int(pointer))

    def _resolve_file(self, file_id: int) -> int:
        if file_id == 0:
            return self._source_file_index
        external = self._table.resolve_file_id(self._source_file_index, file_id)
        if external.kind == "resolved_external_reference":
            return external.target_file_index
        return -1  # unresolvable: the object lookup will miss


def _local_position(typetree: dict[str, Any]) -> tuple[float, float, float]:
    value = typetree.get("m_LocalPosition") or {}
    return (float(value.get("x", 0.0)), float(value.get("y", 0.0)), float(value.get("z", 0.0)))


def _local_rotation(typetree: dict[str, Any]) -> tuple[float, float, float, float]:
    value = typetree.get("m_LocalRotation") or {}
    return (
        float(value.get("x", 0.0)),
        float(value.get("y", 0.0)),
        float(value.get("z", 0.0)),
        float(value.get("w", 1.0)),
    )


def _local_scale(typetree: dict[str, Any]) -> tuple[float, float, float]:
    value = typetree.get("m_LocalScale") or {}
    return (float(value.get("x", 1.0)), float(value.get("y", 1.0)), float(value.get("z", 1.0)))


class _SingularJointMatrixError(ValueError):
    """A joint's world matrix could not be inverted."""


def build_skeleton(
    node: Any,
    scene_nodes: Sequence[Any],
    environment: UnityRsEnvironment,
    table: CollectionFileTable,
) -> Skeleton:
    """Resolve one skinned node's bones into joints with inverse binds.

    Bones are Transform references; each maps to its GameObject identity —
    the exported scene node — and its glTF-space world matrix composes from
    mirrored local transforms up the ``m_Father`` chain.  ``scene_nodes``
    exists for provider-protocol symmetry; bones resolve through the
    collection objects.
    """

    resolver = _SkeletonResolver(environment, table, node.file_index)
    joints: list[SkinJoint] = []
    diagnostics: list[SkinDiagnostic] = []
    renderer_identity = (node.file_index, node.path_id)
    for bone in node.bones:
        bone_identity = (int(bone[0]), int(bone[1]))
        game_object = resolver.game_object_identity(bone_identity)
        if game_object is None:
            diagnostics.append(
                SkinDiagnostic(
                    code=DIAG_MISSING_TARGET,
                    message=f"bone {bone_identity} of renderer {renderer_identity}"
                    " is not a resolvable Transform",
                )
            )
            continue
        world = resolver.world_of(bone_identity)
        if world is None:
            diagnostics.append(
                SkinDiagnostic(
                    code=DIAG_MISSING_TARGET,
                    message=f"bone {bone_identity} of renderer {renderer_identity}"
                    " has an unresolvable parent chain",
                )
            )
            continue
        name = _joint_name(environment, game_object)
        try:
            inverse_bind = invert_mat4(world)
        except ValueError as exc:
            raise _SingularJointMatrixError(
                f"joint {game_object} ({name!r}) has a singular world matrix: {exc}"
            ) from exc
        joints.append(
            SkinJoint(
                identity=game_object,
                name=name,
                world_matrix=world,
                inverse_bind_matrix=inverse_bind,
            )
        )
    return Skeleton(joints=tuple(joints), diagnostics=tuple(diagnostics))


def _joint_name(environment: UnityRsEnvironment, game_object: JointIdentity) -> str:
    target = environment.object_by_identity(*game_object)
    if target is None:
        return ""
    return str(target.read_typetree().get("m_Name", ""))


@dataclass(frozen=True, slots=True)
class BindingSkinWeightsProvider:
    """unity_rs-backed provider: full skeleton, no per-vertex weights.

    The binding cannot supply skin weights (see module docstring), so every
    result carries DIAG_SKIN_WEIGHTS_UNAVAILABLE and no GLB skin is attached
    from this provider.
    """

    environment: UnityRsEnvironment
    table: CollectionFileTable

    def skin_weights(self, node: Any, scene_nodes: Sequence[Any]) -> SkinWeights:
        skeleton = build_skeleton(node, scene_nodes, self.environment, self.table)
        unavailable = SkinDiagnostic(
            code=DIAG_SKIN_WEIGHTS_UNAVAILABLE,
            message=(
                f"renderer {(node.file_index, node.path_id)} ({node.name!r}):"
                f" {len(skeleton.joints)} joints resolved; the unity_rs binding"
                " exposes no per-vertex skin-weight streams, so no glTF skin"
                " is attached"
            ),
        )
        return SkinWeights(
            skeleton=skeleton,
            diagnostics=(unavailable, *skeleton.diagnostics),
        )


__all__ = [
    "BindingSkinWeightsProvider",
    "JointIdentity",
    "SkinDiagnostic",
    "SkinJoint",
    "SkinWeights",
    "SkinWeightsProvider",
    "Skeleton",
    "build_skeleton",
]
