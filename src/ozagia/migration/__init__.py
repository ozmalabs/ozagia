"""Static Python-to-Rust migration overlay.

This package is read-only by default. It inventories the configured Python and
Rust trees, pairs symbols from explicit ``[[migration.pairs]]`` configuration,
reports parity facts and renders a bounded scaffold preview. The overlay never
imports or executes the target project, never builds Rust, and never calls the
network. The only write path is ``ozagia migration scaffold --write`` through
``ozagia.edit.txn``.

See ``docs/migration.md`` for the configuration schema, status vocabulary,
construct table and trust limits.
"""
from __future__ import annotations

from ozagia.migration.hints import HINTS, Hint, hints_for
from ozagia.migration.inventory import (
    PythonSymbol,
    RustSymbol,
    inventory_python,
    inventory_rust,
)
from ozagia.migration.match import MigrationRow, match
from ozagia.migration.report import build_report, markdown_report

__all__ = [
    "HINTS",
    "Hint",
    "MigrationRow",
    "PythonSymbol",
    "RustSymbol",
    "build_report",
    "hints_for",
    "inventory_python",
    "inventory_rust",
    "markdown_report",
    "match",
]
