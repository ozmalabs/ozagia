"""Tests for the repository-shape reference (``ozagia overview``)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ozagia import cli
from ozagia.core import capabilities as K
from ozagia.core import repo_shape as RS
from ozagia.core.memory import DevMemory, build_index
from ozagia.core.repo_map import update_tag_cache
from ozagia.integrations.mcp import server

REPO = Path(__file__).resolve().parents[1]
SECRET_RE = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{20,})"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture(root: Path) -> Path:
    _write(root / "README.md", """# Demo Repo

Demo repository for shape tests. It has one small package and one test suite.

Status: test fixture.

## Install

```bash
pip install -e .
```

## Check

```bash
pytest -q
ruff check src tests
demo run
```
""")
    _write(root / "AGENTS.md", """# Working on demo

- Run the focused test before claiming a change works.
""")
    _write(root / "pyproject.toml", """[project]
name = "demo-shape"
version = "0.2.0"
description = "A fixture package for the shape reference."
license = "MIT"
requires-python = ">=3.12"

[project.scripts]
demo = "demo.cli:main"

[project.entry-points."demo.helpers"]
extra = "demo.core:add"

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
testpaths = ["tests"]
""")
    _write(root / "src" / "demo" / "__init__.py",
           '"""Demo package."""\n\n__all__ = []\n')
    _write(root / "src" / "demo" / "core.py", '''"""Core math helpers."""


class Calc:
    """A tiny calculator."""

    def add(self, left: int, right: int) -> int:
        return left + right


def add(left: int, right: int) -> int:
    """Add two integers."""
    return left + right
''')
    _write(root / "src" / "demo" / "cli.py", '''"""Command-line entry points."""
import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demo")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo", help="run the demo")
    sub.add_parser("serve", help="serve the demo")
    return parser


def main() -> int:
    build_parser()
    return 0
''')
    _write(root / "src" / "demo" / "__main__.py",
           '"""Module entry point."""\nfrom demo.cli import main\n')
    _write(root / "docs" / "roadmap.md", "# Roadmap\n\nShip the demo.\n")
    _write(root / "docs" / "plans" / "2026-01-01_plan.md", "# Plan\n\nOne step.\n")
    _write(root / "docs" / "results" / "run.md", "# Result\n\nIt ran.\n")
    _write(root / "docs" / "guide.md", """# Guide

See [core](../src/demo/core.py) and [missing](missing.md).
The file `src/demo/cli.py` is the command surface.
""")
    _write(root / "tests" / "test_core.py", """from demo.core import add


def test_add() -> None:
    assert add(1, 2) == 3
""")
    _write(root / "tests" / "test_cli.py", """from demo.cli import main


def test_main() -> None:
    assert main() == 0
""")
    _write(root / "tests" / "other_test.py", """def test_other() -> None:
    assert True
""")
    return root


def _tree_fingerprint(root: Path) -> dict[str, str]:
    fingerprint: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            fingerprint[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return fingerprint


def test_shape_identity_layout_modules_docs_entries_and_commands(tmp_path: Path) -> None:
    root = _fixture(tmp_path / "demo")
    shape = RS.build_shape(root, budget_s=5, max_modules=40)
    identity = shape["identity"]
    assert identity["name"] == "demo"
    assert identity["package"] == "demo-shape"
    assert identity["version"] == "0.2.0"
    assert identity["description"] == "A fixture package for the shape reference."
    assert identity["license"] == "MIT"
    assert identity["counts"]["source_files"] >= 3
    assert identity["counts"]["test_files"] == 3
    assert identity["counts"]["symbols"] >= 4
    assert shape["environment"]["snapshot"]["state"] == "missing"
    assert "snapshot" not in identity
    assert "head" not in identity

    layout = shape["layout"]
    top = {row["path"]: row for row in layout["top_level"]}
    assert {"src", "tests", "docs"} <= set(top)
    assert top["tests"]["tests"] == 3
    assert "README.md" in layout["key_files"]
    assert "pyproject.toml" in layout["key_files"]

    entry = shape["entry_points"]
    assert entry["scripts"] == [{
        "name": "demo", "target": "demo.cli:main", "module": "demo.cli",
        "attr": "main", "kind": "console_script",
    }]
    assert {"path": "src/demo/__main__.py", "name": "__main__"} in entry["main_modules"]
    assert {"demo", "serve"} <= set(entry["command_names"])

    groups = {group["name"]: group["modules"] for group in shape["modules"]["groups"]}
    core = {module["path"]: module for module in groups["core"]}
    assert core["src/demo/core.py"]["responsibility"] == "Core math helpers."
    assert core["src/demo/core.py"]["doc_source"] == "module_docstring"
    assert any(module["path"] == "src/demo/cli.py"
               for module in groups["tools"])

    docs = shape["documentation"]
    assert docs["readme"]["title"] == "Demo Repo"
    assert "Demo repository" in docs["readme"]["prose"]
    titles = {row["path"]: row["title"] for row in docs["index"]}
    assert titles["docs/roadmap.md"] == "Roadmap"
    assert titles["docs/plans/2026-01-01_plan.md"] == "Plan"
    assert docs["canonical"]["roadmap"] == "docs/roadmap.md"
    assert docs["canonical"]["plans"] == 1
    assert docs["canonical"]["results"] == 1
    assert docs["coverage"]["links_to_existing_files"] >= 1
    assert docs["coverage"]["missing_examples"]

    commands = shape["commands"]
    assert any("pip install" in row["command"] for row in commands["build"])
    assert any("pytest" in row["command"] for row in commands["test"])
    assert any("ruff" in row["command"] for row in commands["lint"])
    assert any("demo run" in row["command"] for row in commands["run"])
    conventions = " ".join(row["text"] for row in shape["conventions"])
    assert "ruff" in conventions and "pytest" in conventions

    tests = shape["tests"]
    assert tests["count"] == 3
    assert tests["mapped_count"] >= 2
    mapped = {row["test"]: row for row in tests["mapped"]}
    assert "src/demo/core.py" in mapped["tests/test_core.py"]["sources"]
    assert set(mapped["tests/test_core.py"]["basis"]) & {"graph", "filename", "import"}


def test_shape_is_deterministic_and_json_is_key_sorted(tmp_path: Path) -> None:
    root = _fixture(tmp_path / "demo")
    first = RS.build_shape(root, budget_s=5)
    second = RS.build_shape(root, budget_s=5)
    assert first == second
    assert first["content_hash"] == second["content_hash"]
    stable = RS.stable_projection(first)
    assert set(stable) == set(RS.STABLE_KEYS)
    assert "environment" not in stable
    assert "architecture" not in stable
    markdown = RS.render_markdown(first)
    assert markdown == RS.render_markdown(second)
    assert "# demo — repository shape" in markdown
    assert "## What this is" in markdown
    assert "## Generation environment (observed; not checked)" in markdown
    assert "[src/demo/core.py](src/demo/core.py)" in markdown
    payload = RS.render_json(first)
    assert payload == RS.render_json(second)
    assert payload.startswith('{\n  "architecture"')
    assert payload.endswith("\n")
    parsed = json.loads(payload)
    assert parsed["content_hash"] == first["content_hash"]
    assert payload == json.dumps(parsed, indent=2, sort_keys=True,
                                 ensure_ascii=False) + "\n"

    trimmed, cut = RS.trim_markdown("x" * 5_000, 10)
    assert cut is True and "truncated at the requested token budget" in trimmed
    bounded, bounded_cut = RS.limit_json(first, 1_000)
    assert bounded_cut is True
    assert json.loads(bounded)["view"]["truncated"] is True
    assert json.loads(bounded)["content_hash"] == first["content_hash"]


def test_shape_marks_unknowns_and_truncates(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    shape = RS.build_shape(empty, budget_s=5)
    assert shape["source"]["readme"]["state"] == "missing"
    assert shape["source"]["package"]["state"] == "missing"
    assert shape["architecture"]["available"] is False
    assert any("README" in item for item in shape["coverage"]["unknowns"])
    assert any("architecture" in item
               for item in shape["environment"]["unknowns"])
    markdown = RS.render_markdown(shape)
    assert "**Unavailable.**" in markdown
    assert "_No README prose was read._" in markdown

    root = _fixture(tmp_path / "demo")
    truncated = RS.build_shape(root, budget_s=5, max_modules=1)
    assert truncated["modules"]["shown"] == 1
    assert truncated["modules"]["truncated"] is True
    assert any(entry["section"] == "modules"
               for entry in truncated["coverage"]["truncated"])
    assert "Truncated at 1 modules." in RS.render_markdown(truncated)


def test_architecture_reuses_an_existing_tag_cache_without_rewriting_it(
        tmp_path: Path) -> None:
    root = _fixture(tmp_path / "demo")
    update_tag_cache(root)
    cache = root / ".ozagia" / "tags.json"
    assert cache.is_file()
    before = cache.read_bytes()
    shape = RS.build_shape(root, budget_s=5)
    assert shape["architecture"]["available"] is True
    assert shape["architecture"]["source"] == "tag_cache"
    assert shape["architecture"]["files"] >= 3
    assert shape["architecture"]["test_source_ratio"]
    assert "hubs" in shape["architecture"]
    assert cache.read_bytes() == before


def test_check_ignores_environment_only_changes(tmp_path: Path, capsys) -> None:
    root = _fixture(tmp_path / "demo")
    out = root / "REPO_SHAPE.md"
    assert cli.main(["overview", "--root", str(root), "--out", str(out),
                     "--budget-s", "5"]) == 0
    capsys.readouterr()
    stable_before = RS.build_shape(root, budget_s=5,
                                   exclude=[out])["content_hash"]

    # Only local VCS/snapshot/cache/time state changes.
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("a" * 40 + "\n", encoding="utf-8")
    _write(root / ".ozagia" / "graph.json", json.dumps({
        "version": 1,
        "nodes": [{"id": 0, "type": "Repo", "attrs": {
            "head": "b" * 40,
            "generated_at": "2026-01-01T00:00:00",
            "files": 1, "symbols": 2, "tests": 3, "docs": 4,
            "commits": 5, "failures": 0,
        }}],
        "edges": [],
    }))
    update_tag_cache(root)

    stable_after = RS.build_shape(root, budget_s=5,
                                  exclude=[out])["content_hash"]
    assert stable_before == stable_after
    assert cli.main(["overview", "--root", str(root), "--check", str(out),
                     "--budget-s", "5"]) == 0
    assert "not drift" in capsys.readouterr().err


def test_check_fails_on_real_module_doc_and_command_drift(
        tmp_path: Path, capsys) -> None:
    root = _fixture(tmp_path / "demo")
    out = root / "REPO_SHAPE.md"

    def generate() -> None:
        assert cli.main(["overview", "--root", str(root), "--out", str(out),
                         "--budget-s", "5"]) == 0
        capsys.readouterr()

    generate()
    _write(root / "src" / "demo" / "extra.py",
           '"""Extra module."""\n\ndef extra() -> int:\n    return 1\n')
    assert cli.main(["overview", "--root", str(root), "--check", str(out),
                     "--budget-s", "5"]) == 1
    assert "stable shape drift" in capsys.readouterr().err

    generate()
    guide = root / "docs" / "guide.md"
    _write(guide, guide.read_text(encoding="utf-8").replace(
        "# Guide", "# Guide changed"))
    assert cli.main(["overview", "--root", str(root), "--check", str(out),
                     "--budget-s", "5"]) == 1
    capsys.readouterr()

    generate()
    readme = root / "README.md"
    _write(readme, readme.read_text(encoding="utf-8").replace(
        "pytest -q", "pytest -q -x"))
    assert cli.main(["overview", "--root", str(root), "--check", str(out),
                     "--budget-s", "5"]) == 1


def test_cli_writes_nothing_by_default_and_writes_atomically_on_out(
        tmp_path: Path, capsys) -> None:
    root = _fixture(tmp_path / "demo")
    before = _tree_fingerprint(root)
    assert cli.main(["overview", "--root", str(root),
                     "--budget-s", "5"]) == 0
    assert capsys.readouterr().out.startswith("# demo — repository shape")
    assert _tree_fingerprint(root) == before

    out = root / "REPO_SHAPE.md"
    assert cli.main(["overview", "--root", str(root), "--out", str(out),
                     "--budget-s", "5"]) == 0
    assert out.is_file()
    assert out.read_text(encoding="utf-8").startswith("# demo — repository shape")
    assert not list(root.glob("REPO_SHAPE.md.*.tmp"))
    after = _tree_fingerprint(root)
    assert set(after) == set(before) | {"REPO_SHAPE.md"}

    assert cli.main(["overview", "--root", str(root), "--check", str(out),
                     "--budget-s", "5"]) == 0
    _write(root / "README.md", (root / "README.md").read_text(encoding="utf-8")
           + "\nAn extra paragraph changes the reference.\n")
    assert cli.main(["overview", "--root", str(root), "--check", str(out),
                     "--budget-s", "5"]) == 1
    assert cli.main(["overview", "--root", str(root), "--check",
                     str(root / "absent.md"), "--budget-s", "5"]) == 2


def test_cli_json_and_alias(tmp_path: Path, capsys) -> None:
    root = _fixture(tmp_path / "demo")
    assert cli.main(["shape", "--root", str(root), "--json",
                     "--max-modules", "2", "--budget-s", "5"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == RS.SCHEMA
    assert payload["identity"]["name"] == "demo"
    assert payload["modules"]["max_modules"] == 2


def test_mcp_overview_tool_and_resources(tmp_path: Path) -> None:
    root = _fixture(tmp_path / "demo")
    graph = root / ".ozagia" / "graph.json"
    build_index(root, graph, include_git=False, include_scripts=False)
    memory = DevMemory(graph, root / ".ozagia" / "journal.jsonl")
    instance = server.DevMemoryServer(memory, workspace=str(root))
    names = {tool["name"] for tool in instance._tools}
    assert "dev_overview" in names

    call = instance.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "dev_overview",
                   "arguments": {"max_modules": 3, "budget_tokens": 600}},
    })
    assert call["result"]["isError"] is False
    payload = json.loads(call["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["schema"] == RS.SCHEMA
    assert payload["format"] == "markdown"
    assert "# demo — repository shape" in payload["content"]

    as_json = instance.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "dev_overview", "arguments": {"format": "json"}},
    })
    decoded = json.loads(as_json["result"]["content"][0]["text"])
    report = json.loads(decoded["content"])
    assert decoded["format"] == "json"
    assert report["identity"]["name"] == "demo"

    resources = instance.handle({
        "jsonrpc": "2.0", "id": 3, "method": "resources/list",
    })["result"]["resources"]
    assert [row["uri"] for row in resources[:1]] == ["ozagia://brief"]
    assert "ozagia://overview" in {row["uri"] for row in resources}
    read = instance.handle({
        "jsonrpc": "2.0", "id": 4, "method": "resources/read",
        "params": {"uri": "ozagia://overview"},
    })
    assert read["result"]["contents"][0]["mimeType"] == "text/markdown"
    assert read["result"]["contents"][0]["text"].startswith(
        "# demo — repository shape")


def test_catalogue_declares_the_overview_feature() -> None:
    capability = K.get("cap:graph.overview")
    assert capability.side_effect == "read"
    assert set(capability.exposure) == {"cli", "mcp"}
    assert {"cli:ozagia.overview", "cli:ozagia.shape",
            "mcp:tool:dev_overview"} <= set(capability.surfaces)
    assert "test:tests/test_repo_shape.py" in capability.tests
    assert "doc:docs/repo_shape.md" in capability.docs
    assert "impl:ozagia.core.repo_shape.build_shape" in capability.impl


def test_this_repository_check_ignores_a_new_head(monkeypatch) -> None:
    exclude = ["REPO_SHAPE.md"]
    baseline = RS.build_shape(REPO, budget_s=20, max_modules=40,
                              exclude=exclude)
    monkeypatch.setattr(RS, "_git_head", lambda root: {
        "commit": "0" * 40, "ref": "refs/heads/main",
        "state": "read", "source": ".git/refs/heads/main",
    })
    changed = RS.build_shape(REPO, budget_s=20, max_modules=40,
                             exclude=exclude)
    assert changed["content_hash"] == baseline["content_hash"]
    assert changed["environment"]["git"]["commit"] == "0" * 40
    result = RS.check_file(REPO / "REPO_SHAPE.md", changed,
                           RS.render_markdown(changed))
    assert result["stable_same"] is True
    assert result["status"] == "environment_diff"
    assert result["volatile_only"] is True
    assert "not drift" in result["reason"]


def test_this_repository_reference_is_stable_and_secret_free() -> None:
    exclude = ["REPO_SHAPE.md"]
    first = RS.build_shape(REPO, budget_s=20, max_modules=40, exclude=exclude)
    second = RS.build_shape(REPO, budget_s=20, max_modules=40, exclude=exclude)
    assert first == second
    markdown = RS.render_markdown(first)
    assert markdown == RS.render_markdown(second)
    assert "# ozagia — repository shape" in markdown
    assert "src/ozagia/core" in markdown
    assert "docs/roadmap.md" in markdown
    assert SECRET_RE.search(markdown) is None
    assert SECRET_RE.search(RS.render_json(first)) is None
    reference = REPO / "REPO_SHAPE.md"
    assert reference.is_file(), "REPO_SHAPE.md must be generated and committed"
    result = RS.check_file(reference, first, markdown)
    assert result["stable_same"] is True
    assert result["status"] in {"current", "environment_diff"}
