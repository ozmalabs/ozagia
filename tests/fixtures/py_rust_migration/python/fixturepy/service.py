"""Service functions for the migration fixture."""
from __future__ import annotations

import functools

from .models import User


__all__ = [
    "fetch_user", "checksum", "normalize_name", "display", "cached_normalize",
]


async def fetch_user(user_id: int) -> str | None:
    """Fetch a user's display name."""
    return None


def checksum(data: bytes) -> int:
    """Compute a checksum."""
    return sum(data)


def normalize_name(name: str) -> str:
    """Normalize a display name."""
    return name.strip()


def display(user: User) -> str:
    """Render a user."""
    return user.name


@functools.lru_cache(maxsize=8)
def cached_normalize(name: str) -> str:
    """Normalize a name through a cache."""
    return name.strip()
