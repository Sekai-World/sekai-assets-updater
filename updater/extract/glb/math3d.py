"""Small pure-Python 3D math helpers for GLB export (#39).

Unity stores transforms as (position, quaternion(x, y, z, w), scale);
glTF needs column-major mat4s for joint inverse bind matrices.  Only the
operations the exporter needs are implemented, with no third-party math
dependency.
"""

from __future__ import annotations

import math
from typing import Sequence

Vec3 = tuple[float, float, float]
Quat = tuple[float, float, float, float]

IDENTITY4 = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)


def normalize_quaternion(value: Sequence[float]) -> Quat:
    x, y, z, w = (float(component) for component in value)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm == 0.0:  # NOSONAR - only a truly zero quaternion needs the fallback
        return (0.0, 0.0, 0.0, 1.0)
    return (x / norm + 0.0, y / norm + 0.0, z / norm + 0.0, w / norm + 0.0)


def quaternion_to_mat4(q: Sequence[float]) -> tuple[float, ...]:
    """Column-major rotation matrix from a quaternion."""

    x, y, z, w = normalize_quaternion(q)
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    return (
        1.0 - 2.0 * (yy + zz),
        2.0 * (xy + wz),
        2.0 * (xz - wy),
        0.0,
        2.0 * (xy - wz),
        1.0 - 2.0 * (xx + zz),
        2.0 * (yz + wx),
        0.0,
        2.0 * (xz + wy),
        2.0 * (yz - wx),
        1.0 - 2.0 * (xx + yy),
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )


def compose_trs(
    translation: Sequence[float],
    rotation: Sequence[float],
    scale: Sequence[float],
) -> tuple[float, ...]:
    """Column-major TRS matrix (T * R * S) from Unity local transform."""

    r = quaternion_to_mat4(rotation)
    tx, ty, tz = (float(component) for component in translation)
    sx, sy, sz = (float(component) for component in scale)
    return (
        r[0] * sx,
        r[1] * sx,
        r[2] * sx,
        0.0,
        r[4] * sy,
        r[5] * sy,
        r[6] * sy,
        0.0,
        r[8] * sz,
        r[9] * sz,
        r[10] * sz,
        0.0,
        tx,
        ty,
        tz,
        1.0,
    )


def mirror_position(value: Sequence[float]) -> Vec3:
    """Unity -> glTF X-mirror for positions and normals; "+ 0.0" normalizes
    IEEE negative zero so identical inputs always produce byte-identical
    buffers."""

    return (-float(value[0]) + 0.0, float(value[1]) + 0.0, float(value[2]) + 0.0)


def mirror_rotation(value: Sequence[float]) -> Quat:
    """Unity -> glTF X-mirror for rotation quaternions."""

    return (float(value[0]), -float(value[1]), -float(value[2]), float(value[3]))


def multiply_mat4(a: Sequence[float], b: Sequence[float]) -> tuple[float, ...]:
    """Column-major matrix product ``a * b``."""

    return tuple(
        sum(a[k * 4 + row] * b[col * 4 + k] for k in range(4))
        for col in range(4)
        for row in range(4)
    )


def invert_mat4(m: Sequence[float]) -> tuple[float, ...]:
    """Column-major 4x4 inverse via cofactors. Raises ValueError on singular
    matrices."""

    def entry(row: int, col: int) -> float:
        return m[col * 4 + row]

    def minor(row: int, col: int) -> float:
        rows = [r for r in range(4) if r != row]
        cols = [c for c in range(4) if c != col]
        a, b, c, d, e, f, g, h, i = (entry(r, c) for r in rows for c in cols)
        return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)

    determinant = sum((-1) ** (0 + col) * entry(0, col) * minor(0, col) for col in range(4))
    if determinant == 0.0:  # NOSONAR - only an exactly singular matrix must raise
        raise ValueError("matrix is singular and cannot be inverted")
    inverse_determinant = 1.0 / determinant
    return tuple(
        (-1) ** (row + col) * minor(col, row) * inverse_determinant
        for col in range(4)
        for row in range(4)
    )
