"""Checks for the tiny fixture; the scenario manifest owns the failing mutation."""

from tiny.core import add, add_clamped


def test_add() -> None:
    assert add(2, 3) == 5


def test_add_clamped() -> None:
    assert add_clamped(8, 7, 0, 10) == 10
