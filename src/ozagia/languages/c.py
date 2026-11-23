"""C language support package."""
from __future__ import annotations

from ozagia.languages.model import Component, LanguageSupport

SUPPORT = LanguageSupport(
    id="c",
    label="C",
    suffixes=(".c", ".h"),
    components=(
        Component(
            id="tree-sitter-c",
            label="C grammar",
            kind="python",
            probe="tree_sitter_c",
            manager="pip",
            package="tree-sitter-c>=0.24",
            extra="lang-c",
        ),
        Component(
            id="clangd",
            label="clangd language server",
            kind="binary",
            probe="clangd",
            manager="system",
            package="clangd",
            required=False,
            requires_sudo=True,
            note="install clangd with the system package manager",
        ),
    ),
    notes="C tags are definition-only; clangd provides the precise layer.",
)
