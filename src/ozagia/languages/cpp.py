"""C++ language support package."""
from __future__ import annotations

from ozagia.languages.model import Component, LanguageSupport

SUPPORT = LanguageSupport(
    id="cpp",
    label="C++",
    suffixes=(".cpp", ".cc", ".cxx", ".hpp", ".hh"),
    components=(
        Component(
            id="tree-sitter-cpp",
            label="C++ grammar",
            kind="python",
            probe="tree_sitter_cpp",
            manager="pip",
            package="tree-sitter-cpp>=0.23",
            extra="lang-cpp",
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
    notes="C++ tags are definition-only; clangd provides the precise layer.",
)
