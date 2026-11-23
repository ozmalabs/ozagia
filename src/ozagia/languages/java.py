"""Java language support package."""
from __future__ import annotations

from ozagia.languages.model import Component, LanguageSupport

SUPPORT = LanguageSupport(
    id="java",
    label="Java",
    suffixes=(".java",),
    components=(
        Component(
            id="tree-sitter-java",
            label="Java grammar",
            kind="python",
            probe="tree_sitter_java",
            manager="pip",
            package="tree-sitter-java>=0.23",
            extra="lang-java",
        ),
    ),
    notes="Java has tags and the file graph; no Java language server is wired.",
)
