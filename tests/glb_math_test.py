"""GLB Phase 5 tests: pure math helpers (#39)."""

from __future__ import annotations

import random

from updater.extract.glb.math3d import (
    IDENTITY4,
    compose_trs,
    invert_mat4,
    mirror_position,
    mirror_rotation,
    multiply_mat4,
    normalize_quaternion,
    quaternion_to_mat4,
)


def _random_trs(rng: random.Random):
    translation = tuple(rng.uniform(-10, 10) for _ in range(3))
    rotation = normalize_quaternion(tuple(rng.uniform(-1, 1) for _ in range(4)))
    scale = tuple(rng.uniform(0.1, 3.0) for _ in range(3))
    return translation, rotation, scale


def test_invert_mat4_round_trips_random_transforms() -> None:
    rng = random.Random(7)
    for _ in range(50):
        matrix = compose_trs(*_random_trs(rng))
        inverse = invert_mat4(matrix)
        for product in (multiply_mat4(matrix, inverse), multiply_mat4(inverse, matrix)):
            for got, want in zip(product, IDENTITY4, strict=True):
                assert abs(got - want) < 1e-6


def test_multiply_mat4_is_column_major() -> None:
    translation = compose_trs((1.0, 2.0, 3.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    scale = compose_trs((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (2.0, 2.0, 2.0))
    product = multiply_mat4(translation, scale)
    assert product[0] == 2.0  # X axis scaled, ...
    assert product[12:15] == (1.0, 2.0, 3.0)  # ... translation untouched by S


def test_invert_mat4_rejects_singular_matrix() -> None:
    zero_scale = compose_trs((0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0))
    try:
        invert_mat4(zero_scale)
    except ValueError as exc:
        assert "singular" in str(exc)
    else:
        raise AssertionError("singular matrix must not invert")


def test_mirror_rules_match_documented_conversion() -> None:
    assert mirror_position((-0.0, 2.0, -3.0)) == (0.0, 2.0, -3.0)
    assert mirror_rotation((0.1, 0.2, 0.3, 0.9)) == (0.1, -0.2, -0.3, 0.9)


def test_mirrored_locals_compose_to_conjugated_world() -> None:
    """The glTF-space world matrix of mirrored locals equals F * world * F,
    so joint inverse binds stay consistent with the mirrored hierarchy."""

    first = ((1.0, 2.0, 3.0), (0.0, 0.1, 0.2, 0.9), (1.0, 1.0, 1.0))
    second = ((4.0, 5.0, 6.0), (0.2, 0.0, 0.0, 0.9), (2.0, 2.0, 2.0))
    world = multiply_mat4(compose_trs(*first), compose_trs(*second))
    mirrored_chain = multiply_mat4(
        compose_trs(mirror_position(first[0]), mirror_rotation(first[1]), first[2]),
        compose_trs(mirror_position(second[0]), mirror_rotation(second[1]), second[2]),
    )
    # F = diag(-1, 1, 1, 1): negate element (row, col) iff exactly one of
    # row/col is 0 — element (0, 0) flips twice and stays.
    conjugated = tuple(
        value * (-1 if ((index % 4 == 0) != (index // 4 == 0)) else 1)
        for index, value in enumerate(world)
    )
    for got, want in zip(mirrored_chain, conjugated, strict=True):
        assert abs(got - want) < 1e-9


def test_quaternion_to_mat4_identity_and_normalization() -> None:
    identity = quaternion_to_mat4((0.0, 0.0, 0.0, 1.0))
    for got, want in zip(identity, IDENTITY4, strict=True):
        assert got == want
    doubled = quaternion_to_mat4((0.0, 0.0, 0.0, 2.0))
    assert doubled == identity  # normalized before use
    assert normalize_quaternion((0.0, 0.0, 0.0, 0.0)) == (0.0, 0.0, 0.0, 1.0)
