"""Tested host versions for the live integrations.

This module is the single source of truth used by :func:`ozagia.doctor.diagnose`
and by the integration summary in ``ozagia-status``.  The human-readable
matrix, with the evidence for each level, is ``docs/integrations.md``; change
both files together.

A version is *tested* only in the sense named by ``verification``.  The
default level is a protocol fixture plus an installer write/remove round trip,
which proves our side but not a host session.  ``verified`` in this module
therefore means the integration's stated verification level was reached on
that version, not that every surface works.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: The matrix document this table mirrors.
MATRIX_DOC = "docs/integrations.md"
#: A host binary gets this long to answer ``--version``.  The probe is never a
#: model, tool or daemon call.
VERSION_TIMEOUT_S = 2.0

STATES = (
    "verified",
    "installed_untested_version",
    "not_installed",
    "unknown",
)


@dataclass(frozen=True)
class HostVersion:
    """One integration surface and the host versions it was checked against."""

    key: str
    label: str
    command: str
    tested_versions: tuple[str, ...]
    tested_range: str
    verification: str
    verified: bool
    matrix_anchor: str
    #: Also look in ``<root>/.venv/bin`` and the ozagia checkout's ``.venv``
    #: before PATH.  This mirrors ``ozagia.lsp._resolve_binary``.
    search_venv: bool = False


HOSTS: Final[dict[str, HostVersion]] = {
    "opencode": HostVersion(
        key="opencode",
        label="OpenCode TUI",
        command="opencode",
        tested_versions=("2.3.0", "2.4.1"),
        tested_range="2.3.0, 2.4.1",
        verification="host-launch without model",
        verified=True,
        matrix_anchor="opencode-tui",
    ),
    "codex": HostVersion(
        key="codex",
        label="Codex hooks",
        command="codex",
        tested_versions=("0.169.0",),
        tested_range="0.169.0 (MCP table read only)",
        verification="protocol fixture + installer write/remove",
        verified=False,
        matrix_anchor="codex-hooks",
    ),
    "kiro": HostVersion(
        key="kiro",
        label="Kiro hooks",
        command="kiro-cli",
        tested_versions=("2.37.0",),
        tested_range="2.37.0 (install/profile validation only)",
        verification="protocol fixture + installer write/remove",
        verified=False,
        matrix_anchor="kiro-hooks",
    ),
    "claude": HostVersion(
        key="claude",
        label="Claude Code hooks",
        command="claude",
        tested_versions=(),
        tested_range="none (protocol fixtures only)",
        verification="protocol fixture + installer write/remove",
        verified=False,
        matrix_anchor="claude-code-hooks",
    ),
    "pyright": HostVersion(
        key="pyright",
        label="pyright",
        command="pyright",
        tested_versions=("1.1.414",),
        tested_range="1.1.414",
        verification="live language server",
        verified=True,
        matrix_anchor="pyright",
        search_venv=True,
    ),
    "rust_analyzer": HostVersion(
        key="rust_analyzer",
        label="rust-analyzer",
        command="rust-analyzer",
        tested_versions=("1.97.1",),
        tested_range="1.97.1 (previous host; not installed here)",
        verification="live language server",
        verified=True,
        matrix_anchor="rust-analyzer",
    ),
}

#: The order doctor and status print the host rows in.
HOST_ORDER: Final[tuple[str, ...]] = (
    "opencode",
    "codex",
    "kiro",
    "claude",
    "pyright",
    "rust_analyzer",
)

#: The integrations whose configuration files doctor inspects, read-only.
CONFIG_KEYS: Final[tuple[str, ...]] = ("opencode", "codex", "kiro", "claude")

_VERSION_RE = re.compile(
    r"(?<![\w.])[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?")


def parse_version(text: object) -> str | None:
    """The first dotted version in ``text`` as ``major.minor.patch``."""
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return ".".join(part.lstrip("0") or "0" for part in match.groups())


def _normalized(value: str) -> str | None:
    return parse_version(value)


def version_matches(spec: HostVersion, version: str | None) -> bool:
    """Whether ``version`` is one of the versions the matrix names."""
    normalized = _normalized(version) if version is not None else None
    if normalized is None:
        return False
    return normalized in {
        candidate for candidate in
        (_normalized(item) for item in spec.tested_versions)
        if candidate is not None
    }


def classify(spec: HostVersion, version: str | None) -> str:
    """Classify an installed version: ``verified`` or ``installed_untested_version``.

    A missing/unparsable version is ``unknown``; that is decided by the caller
    which knows whether the binary was found.  A tested version is verified
    only when the matrix records a verification level above the protocol
    fixture for that surface.
    """
    if version is None or _normalized(version) is None:
        return "unknown"
    if spec.verified and version_matches(spec, version):
        return "verified"
    return "installed_untested_version"


__all__ = [
    "CONFIG_KEYS", "HOSTS", "HOST_ORDER", "MATRIX_DOC", "STATES",
    "VERSION_TIMEOUT_S", "HostVersion", "classify", "parse_version",
    "version_matches",
]
