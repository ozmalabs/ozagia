"""Swift language support package."""
from __future__ import annotations

from ozagia.languages.model import Component, LanguageSupport

SUPPORT = LanguageSupport(
    id="swift",
    label="Swift",
    suffixes=(".swift",),
    components=(
        Component(
            id="tree-sitter-swift",
            label="Swift grammar",
            kind="python",
            probe="tree_sitter_swift",
            manager="pip",
            package="tree-sitter-swift>=0.7",
            extra="lang-swift",
        ),
        Component(
            id="sourcekit-lsp",
            label="SourceKit-LSP",
            kind="binary",
            probe="sourcekit-lsp",
            manager="none",
            package="sourcekit-lsp",
            required=False,
            note="install SourceKit-LSP with the Swift toolchain",
        ),
    ),
    notes="Swift tags are definition-only.",
)
