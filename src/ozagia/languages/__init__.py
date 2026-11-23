"""Per-language support packages.

Each language is one module exporting a :class:`LanguageSupport` (see
``model.py``). The registry maps source suffixes to those packages, detects
the languages in a checkout, and can report or install their components.

This package imports only the standard library and its own modules. It never
probes, installs or runs a process at import time.
"""
from __future__ import annotations

from pathlib import Path

from ozagia.languages import c as _c
from ozagia.languages import cpp as _cpp
from ozagia.languages import go as _go
from ozagia.languages import java as _java
from ozagia.languages import kotlin as _kotlin
from ozagia.languages import lua as _lua
from ozagia.languages import python as _python
from ozagia.languages import rust as _rust
from ozagia.languages import swift as _swift
from ozagia.languages import typescript as _typescript
from ozagia.languages.model import (
    AUTO_INSTALL_ENV,
    PREFIX,
    Component,
    LanguageSupport,
    component_status,
    detect_languages,
    ensure,
    ensure_for_root,
    registry_payload,
    support_status,
)

SUPPORTS: dict[str, LanguageSupport] = {
    support.id: support
    for support in (
        _python.SUPPORT,
        _rust.SUPPORT,
        _lua.SUPPORT,
        _typescript.SUPPORT,
        _go.SUPPORT,
        _java.SUPPORT,
        _c.SUPPORT,
        _cpp.SUPPORT,
        _kotlin.SUPPORT,
        _swift.SUPPORT,
    )
}

BY_SUFFIX: dict[str, LanguageSupport] = {
    suffix: support
    for support in SUPPORTS.values()
    for suffix in support.suffixes
}


def support_for(path: str | Path) -> LanguageSupport | None:
    """The support package for a path's suffix, or None."""
    return BY_SUFFIX.get(Path(path).suffix.lower())


def support(language_id: str) -> LanguageSupport | None:
    """The support package for a language id (``rust``), or None."""
    return SUPPORTS.get(str(language_id or "").lower())


__all__ = [
    "AUTO_INSTALL_ENV",
    "BY_SUFFIX",
    "Component",
    "LanguageSupport",
    "PREFIX",
    "SUPPORTS",
    "component_status",
    "detect_languages",
    "ensure",
    "ensure_for_root",
    "registry_payload",
    "support",
    "support_for",
    "support_status",
]
