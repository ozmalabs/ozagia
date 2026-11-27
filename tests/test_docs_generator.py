"""Tests for the reference-docs generator (``scripts/docs/generate_docs.py``).

The generated pages are committed, so their text must not depend on the
terminal width or on the checkout path of the machine that generated them.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "docs" / "generate_docs.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "ozagia_docs_generator", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def docs():
    return _load_generator()


def _action(default: object) -> argparse.Action:
    return argparse.Action(option_strings=["--root"], dest="root",
                           default=default)


def test_default_text_normalizes_the_checkout_root_only(docs):
    assert docs._default_text(_action(str(docs.REPO))) == "`<repo>`"
    tasks = docs.REPO / "evaluation" / "agent_e2e" / "tasks.json"
    assert docs._default_text(_action(str(tasks))) == (
        "`<repo>/evaluation/agent_e2e/tasks.json`")
    # An unrelated absolute default keeps its real path.
    assert docs._default_text(_action("/tmp/ozagia-out")) == (
        "`/tmp/ozagia-out`")
    # Only a whole path segment is replaced, not a prefix of a sibling name.
    sibling = f"{docs.REPO}-sibling"
    assert docs._default_text(_action(sibling)) == f"`{sibling}`"


def test_walk_parser_wraps_usage_at_a_fixed_width(docs, monkeypatch):
    module = importlib.import_module("ozagia.files_cli")
    parser = module.build_parser()
    original_formatter = parser.formatter_class

    monkeypatch.setenv("COLUMNS", "80")
    narrow = docs.walk_parser(parser)
    assert parser.formatter_class is original_formatter

    monkeypatch.setenv("COLUMNS", "140")
    wide = docs.walk_parser(parser)

    assert narrow == wide
    # The parser has enough options that its usage must wrap at the pinned
    # width; otherwise the comparison above would prove nothing.
    assert "\n" in narrow["usage"]


def test_generated_pages_are_width_independent_and_root_free(docs, monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    narrow = docs.build_all()
    monkeypatch.setenv("COLUMNS", "140")
    wide = docs.build_all()

    assert narrow == wide
    root = str(docs.REPO)
    assert [rel for rel, text in narrow.items() if root in text] == []
    assert any(docs.REPO_PLACEHOLDER in text for text in narrow.values())
