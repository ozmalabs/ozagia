"""Go language support package."""
from __future__ import annotations

from ozagia.languages.model import Component, LanguageSupport

SUPPORT = LanguageSupport(
    id="go",
    label="Go",
    suffixes=(".go",),
    components=(
        Component(
            id="tree-sitter-go",
            label="Go grammar",
            kind="python",
            probe="tree_sitter_go",
            manager="pip",
            package="tree-sitter-go>=0.25",
            extra="lang-go",
        ),
    ),
    notes="Go has tags and the file graph; no Go language server is wired.",
)
