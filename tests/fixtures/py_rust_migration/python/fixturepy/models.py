"""Models for the migration fixture."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


__all__ = [
    "User", "Widget", "ProtocolThing", "Colour", "flush_cache",
    "parse_pending", "legacy_only",
]


@dataclass
class User:
    """A user record."""

    id: int
    name: str


@dataclass
class Widget:
    """A widget with a size."""

    size: int


class ProtocolThing(Protocol):
    """A runtime protocol."""

    def run(self, value: int) -> str:
        ...


class Colour(Enum):
    """A colour."""

    RED = "red"
    BLUE = "blue"


def flush_cache(force: bool = False) -> None:
    """Flush a cache."""
    return None


def parse_pending(value: int | str) -> str:
    """Parse a pending value."""
    return str(value)


def legacy_only() -> None:
    """Intentionally not ported."""
    return None


def _private_helper() -> None:
    """Private by convention and absent from __all__."""
    return None
