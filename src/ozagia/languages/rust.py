"""Rust language support package."""
from __future__ import annotations

from ozagia.languages.model import Component, LanguageSupport

SUPPORT = LanguageSupport(
    id="rust",
    label="Rust",
    suffixes=(".rs",),
    components=(
        Component(
            id="tree-sitter-rust",
            label="Rust grammar",
            kind="python",
            probe="tree_sitter_rust",
            manager="pip",
            package="tree-sitter-rust>=0.24",
            extra="lang-rust",
        ),
        Component(
            id="rust-analyzer",
            label="Rust language server",
            kind="binary",
            probe="rust-analyzer",
            manager="rustup",
            package="rust-analyzer",
            note="rustup component add rust-analyzer",
        ),
        Component(
            id="rustfmt",
            label="Rust formatter",
            kind="binary",
            probe="rustfmt",
            manager="rustup",
            package="rustfmt",
            required=False,
            note="rustup component add rustfmt",
        ),
    ),
    notes="The graph has a regex fallback without tree-sitter-rust; "
          "rust-analyzer is the precise layer and rustfmt the linter.",
)
