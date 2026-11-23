"""Kotlin language support package."""
from __future__ import annotations

from ozagia.languages.model import Component, LanguageSupport

SUPPORT = LanguageSupport(
    id="kotlin",
    label="Kotlin",
    suffixes=(".kt", ".kts"),
    components=(
        Component(
            id="tree-sitter-kotlin",
            label="Kotlin grammar",
            kind="python",
            probe="tree_sitter_kotlin",
            manager="pip",
            package="tree-sitter-kotlin>=1.1",
            extra="lang-kotlin",
        ),
        Component(
            id="kotlin-language-server",
            label="Kotlin language server",
            kind="binary",
            probe="kotlin-language-server",
            manager="none",
            package="kotlin-language-server",
            required=False,
            note="install kotlin-language-server from its release archive",
        ),
    ),
    notes="The grammar wheel ships no tags query; ozagia bundles a small one.",
)
