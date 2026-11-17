"""The one behaviour the tiny fixture checks."""

from tiny.util import clamp


def add(left: int, right: int) -> int:
    """Return the sum of two integers."""
    return left + right


def add_clamped(left: int, right: int, low: int, high: int) -> int:
    """Return the sum clamped to the inclusive range."""
    return clamp(add(left, right), low, high)
