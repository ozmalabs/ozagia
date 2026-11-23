"""TypeScript / JavaScript language support package."""
from __future__ import annotations

from ozagia.languages.model import Component, LanguageSupport

SUPPORT = LanguageSupport(
    id="typescript",
    label="TypeScript / JavaScript",
    suffixes=(".ts", ".tsx", ".js", ".jsx"),
    components=(
        Component(
            id="tree-sitter-typescript",
            label="TypeScript grammar",
            kind="python",
            probe="tree_sitter_typescript",
            manager="pip",
            package="tree-sitter-typescript>=0.23",
            extra="lang-typescript",
        ),
        Component(
            id="typescript-language-server",
            label="TypeScript language server",
            kind="binary",
            probe="typescript-language-server",
            manager="npm",
            package="typescript typescript-language-server",
            required=False,
            requires_sudo=True,
            note="npm install -g typescript typescript-language-server",
        ),
    ),
    notes="The TSX grammar also parses plain JavaScript and JSX.",
)
