"""Lua language support package."""
from __future__ import annotations

from ozagia.languages.model import Component, LanguageSupport

SUPPORT = LanguageSupport(
    id="lua",
    label="Lua",
    suffixes=(".lua",),
    components=(
        Component(
            id="tree-sitter-lua",
            label="Lua grammar",
            kind="python",
            probe="tree_sitter_lua",
            manager="pip",
            package="tree-sitter-lua>=0.5",
            extra="lang-lua",
        ),
    ),
    notes="Lua has tags and CST edits; no language server is wired yet.",
)
