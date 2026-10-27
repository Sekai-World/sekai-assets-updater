"""Skeleton and skin-weight extraction for streaming Live GLB export (#39).

A skinned scene node's ``bones`` list names Transforms that deform its mesh.
The binding's scene graph is keyed by GameObject identities, so every bone
Transform is resolved through its typetree and mapped to its
``m_GameObject`` identity — the exported scene node.  Each joint gets an
inverse bind matrix: its world matrix in glTF space — composed from
X-mirrored local transforms up the ``m_Father`` chain — inverted, which by
the mirror-conjugation identity stays consistent with the mirrored exported
hierarchy. Per-vertex joint/weight data arrives through a provider pipeline.
The unity_rs-backed provider reads native skin streams and bind poses when
available, and records a structured diagnostic when those streams are missing
or malformed.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, replace
from typing import Any, Protocol, Sequence

from updater.extract.glb.contracts import DIAG_MISSING_TARGET, DIAG_SKIN_WEIGHTS_UNAVAILABLE
from updater.extract.glb.math3d import (
    compose_trs,
    invert_mat4,
    mirror_position,
    mirror_rotation,
    multiply_mat4,
)
from updater.unity_rs_adapter import (
    RESOLVED_EXTERNAL_REFERENCE,
    CollectionFileTable,
    UnityRsEnvironment,
)

JointIdentity = tuple[int, int]

TRANSFORM_OBJECT_CLASS = 4
_WEIGHT_SUM_TOLERANCE = 1e-3
_UINT16_MAX = 0xFFFF


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
    keyed); results cache per full transform identity so shared parents
    compose once without colliding across serialized files.
    """

    def __init__(
        self,
        environment: UnityRsEnvironment,
        table: CollectionFileTable,
    ) -> None:
        self._environment = environment
        self._table = table
        self._world_cache: dict[JointIdentity, tuple[float, ...]] = {}
        self._in_flight: set[JointIdentity] = set()

    def world_of(self, transform_identity: JointIdentity) -> tuple[float, ...] | None:
        """Return the mirrored glTF world matrix of one Transform, or
        ``None`` when it cannot be resolved."""

        cached = self._world_cache.get(transform_identity)
        if cached is not None:
            return cached
        if transform_identity in self._in_flight:
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
        self._in_flight.add(transform_identity)
        try:
            father = typetree.get("m_Father") or {}
            father_identity = (
                self._resolve_file(int(father.get("m_FileID", 0)), transform_identity[0]),
                int(father.get("m_PathID", 0)),
            )
            if father_identity[1] == 0:
                world = local
            else:
                parent_world = self.world_of(father_identity)
                world = multiply_mat4(parent_world, local) if parent_world is not None else None
        finally:
            self._in_flight.discard(transform_identity)
        if world is None:
            return None
        self._world_cache[transform_identity] = world
        return world

    def game_object_identity(self, transform_identity: JointIdentity) -> JointIdentity | None:
        """Map one Transform identity to its GameObject identity."""

        transform = self._environment.object_by_identity(*transform_identity)
        if transform is None or transform.class_id != TRANSFORM_OBJECT_CLASS:
            return None
        pointer = (transform.read_typetree().get("m_GameObject") or {}).get("m_PathID", 0)
        return (transform_identity[0], int(pointer))

    def _resolve_file(self, file_id: int, source_file_index: int) -> int:
        if file_id == 0:
            return source_file_index
        external = self._table.resolve_file_id(source_file_index, file_id)
        if external.kind == RESOLVED_EXTERNAL_REFERENCE:
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

    resolver = _SkeletonResolver(environment, table)
    joints: list[SkinJoint] = []
    diagnostics: list[SkinDiagnostic] = []
    renderer_identity = (node.file_index, node.path_id)
    for bone in getattr(node, "bones", ()) or ():
        try:
            bone_identity = (int(bone[0]), int(bone[1]))
        except (IndexError, TypeError, ValueError):
            diagnostics.append(
                SkinDiagnostic(
                    code=DIAG_MISSING_TARGET,
                    message=f"renderer {renderer_identity} has an invalid bone reference {bone!r}",
                )
            )
            continue
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
    """unity_rs-backed provider for native mesh skin streams and bind poses."""

    environment: UnityRsEnvironment
    table: CollectionFileTable

    def skin_weights(self, node: Any, scene_nodes: Sequence[Any]) -> SkinWeights:
        skeleton = build_skeleton(node, scene_nodes, self.environment, self.table)
        renderer_identity = (node.file_index, node.path_id)

        def unavailable(reason: str) -> SkinWeights:
            diagnostic = SkinDiagnostic(
                code=DIAG_SKIN_WEIGHTS_UNAVAILABLE,
                message=(
                    f"renderer {renderer_identity} ({getattr(node, 'name', '')!r}): "
                    f"{reason}; no skin attached"
                ),
            )
            return SkinWeights(
                skeleton=skeleton,
                diagnostics=(diagnostic, *skeleton.diagnostics),
            )

        bones = tuple(getattr(node, "bones", ()) or ())
        if not bones:
            return unavailable("renderer has no bone references")
        if skeleton.diagnostics or len(skeleton.joints) != len(bones):
            return unavailable("one or more bone references could not be resolved")

        mesh_identity = getattr(node, "mesh", None)
        if not isinstance(mesh_identity, (tuple, list)) or len(mesh_identity) != 2:
            return unavailable("renderer has no resolvable mesh identity")
        read_mesh = getattr(self.environment.studio, "read_mesh", None)
        if not callable(read_mesh):
            return unavailable("unity_rs does not expose read_mesh")
        try:
            mesh = read_mesh(int(mesh_identity[0]), int(mesh_identity[1]))
        except Exception as exc:  # NOSONAR - native mesh data is optional per renderer
            return unavailable(f"native mesh read failed: {exc}")
        if mesh is None:
            return unavailable("native mesh read returned no mesh")

        try:
            vertex_count = int(mesh.vertex_count)
            raw_weights = mesh.skin_weights
            raw_indices = mesh.skin_bone_indices
            raw_bind_poses = mesh.bind_poses
            vertex_joints, vertex_weights = _decode_skin_streams(
                vertex_count, len(bones), raw_indices, raw_weights
            )
            bind_poses = _decode_bind_poses(raw_bind_poses, len(bones))
        except Exception as exc:  # NOSONAR - malformed native fields must not abort export
            return unavailable(f"native mesh skin data is unavailable or invalid: {exc}")

        joints = tuple(
            replace(joint, inverse_bind_matrix=bind_pose)
            for joint, bind_pose in zip(skeleton.joints, bind_poses, strict=True)
        )
        skeleton = replace(skeleton, joints=joints)
        return SkinWeights(
            skeleton=skeleton,
            vertex_joints=vertex_joints,
            vertex_weights=vertex_weights,
            diagnostics=skeleton.diagnostics,
        )


def _decode_skin_streams(
    vertex_count: int,
    bone_count: int,
    raw_indices: bytes | bytearray | memoryview | None,
    raw_weights: bytes | bytearray | memoryview | None,
) -> tuple[
    tuple[tuple[int, int, int, int], ...],
    tuple[tuple[float, float, float, float], ...],
]:
    if vertex_count < 0:
        raise ValueError(f"negative mesh vertex_count {vertex_count}")
    if raw_indices is None or raw_weights is None:
        raise ValueError("skin_bone_indices or skin_weights is None")
    try:
        indices_data = bytes(raw_indices)
        weights_data = bytes(raw_weights)
    except (TypeError, ValueError) as exc:
        raise ValueError("skin streams are not byte data") from exc
    expected_bytes = vertex_count * 16
    if len(indices_data) != expected_bytes:
        raise ValueError(
            f"skin_bone_indices has {len(indices_data)} bytes; expected {expected_bytes}"
        )
    if len(weights_data) != expected_bytes:
        raise ValueError(f"skin_weights has {len(weights_data)} bytes; expected {expected_bytes}")

    vertex_joints: list[tuple[int, int, int, int]] = []
    vertex_weights: list[tuple[float, float, float, float]] = []
    for offset in range(0, expected_bytes, 16):
        weights = struct.unpack_from("<4f", weights_data, offset)
        indices = struct.unpack_from("<4I", indices_data, offset)
        if any(not math.isfinite(weight) or weight < 0.0 for weight in weights):
            raise ValueError(f"vertex {offset // 16} has non-finite or negative weights")
        total = sum(weights)
        if total <= 0.0:
            raise ValueError(f"vertex {offset // 16} has zero total weight")
        if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
            raise ValueError(
                f"vertex {offset // 16} weights sum to {total}, outside normalization tolerance"
            )

        normalized_indices: list[int] = []
        for index, weight in zip(indices, weights, strict=True):
            if weight == 0.0:
                normalized_indices.append(0)
                continue
            if index >= bone_count:
                raise ValueError(
                    f"vertex {offset // 16} positive-weight bone index {index} "
                    f"is outside {bone_count} renderer bones"
                )
            if index > _UINT16_MAX:
                raise ValueError(
                    f"vertex {offset // 16} positive-weight bone index {index} exceeds uint16"
                )
            normalized_indices.append(index)
        vertex_joints.append(tuple(normalized_indices))
        vertex_weights.append(tuple(weight / total for weight in weights))

    return tuple(vertex_joints), tuple(vertex_weights)


def _decode_bind_poses(
    raw_bind_poses: bytes | bytearray | memoryview | None,
    bone_count: int,
) -> tuple[tuple[float, ...], ...]:
    if raw_bind_poses is None:
        raise ValueError("bind_poses is None")
    try:
        data = bytes(raw_bind_poses)
    except (TypeError, ValueError) as exc:
        raise ValueError("bind_poses are not byte data") from exc
    expected_bytes = bone_count * 64
    if len(data) != expected_bytes:
        raise ValueError(f"bind_poses has {len(data)} bytes; expected {expected_bytes}")

    signs = (-1.0, 1.0, 1.0, 1.0)
    matrices: list[tuple[float, ...]] = []
    for bone_index, offset in enumerate(range(0, expected_bytes, 64)):
        matrix = struct.unpack_from("<16f", data, offset)
        if any(not math.isfinite(value) for value in matrix):
            raise ValueError(f"bind pose {bone_index} contains non-finite values")
        # Unity and glTF both encode matrices column-major. C*B*C changes the
        # signs by row and column and does not transpose the matrix.
        mirrored = tuple(
            value * signs[index % 4] * signs[index // 4] for index, value in enumerate(matrix)
        )
        try:
            invert_mat4(mirrored)
        except ValueError as exc:
            raise ValueError(f"bind pose {bone_index} is singular") from exc
        matrices.append(mirrored)
    return tuple(matrices)


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
