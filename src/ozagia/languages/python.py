"""Python language support package."""
from __future__ import annotations

from ozagia.languages.model import Component, LanguageSupport

SUPPORT = LanguageSupport(
    id="python",
    label="Python",
    suffixes=(".py", ".pyi"),
    components=(
        Component(
            id="tree-sitter-python",
            label="Python grammar",
            kind="python",
            probe="tree_sitter_python",
            manager="pip",
            package="tree-sitter-python>=0.25",
            extra="lang-python",
        ),
        Component(
            id="pyright",
            label="Pyright language server",
            kind="python",
            probe="pyright",
            manager="pip",
            package="pyright>=1.1.400",
            extra="lang-python",
        ),
        Component(
            id="ruff",
            label="Ruff linter",
            kind="python",
            probe="ruff",
            manager="pip",
            package="ruff>=0.6",
            extra="lang-python",
            required=False,
        ),
    ),
    notes="Python tags have a stdlib-AST fallback; pyright is the precise "
          "layer and ruff the edit-time linter.",
)
