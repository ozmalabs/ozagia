"""The `graph` MCP surface: find/node/expand/impact/edit/create/check/note.

Every verb is called through `DevMemoryServer.handle`, so the argument
validation and the surface switch are the server's own.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.mcp import graph_surface
from ozagia.integrations.mcp.graph_surface import (
    GRAPH_TOOL_NAMES,
    GRAPH_TOOLS,
    description_tokens,
)
from ozagia.integrations.mcp.server import DevMemoryServer, main

# `tools/list` of the full surface before the graph surface was added, with
# the seed adapter loaded (dev_seed, dev_rule and the adapter's tool).
FULL_SURFACE_TOOLS = [
    "dev_context", "dev_brief", "dev_repo_map", "dev_impact",
    "dev_architecture", "dev_code_search", "dev_scip", "dev_search",
    "dev_file", "dev_symbol", "dev_outline", "dev_query", "dev_ast",
    "dev_edit",
    "dev_span_edit", "dev_key_edit", "dev_replace", "dev_tests", "dev_seed",
    "dev_rule", "dev_node",
    "dev_neighbours", "dev_stats", "dev_changed", "dev_refresh", "dev_record",
    "dev_session_start", "dev_session_record", "dev_session_state",
    "dev_session_next", "dev_session_finish", "dev_session_list",
    "dev_who", "dev_recent", "federation_status", "federation_search", "federation_node", "federation_check",
    "dev_lsp_status", "dev_definition", "dev_references",
    "dev_callers", "dev_callees", "dev_symbol_impact", "dev_rename_preview",
    "dev_diagnostics", "dev_hover", "dev_document_symbols",
    "dev_symbol_callers", "dev_symbol_callees", "dev_symbol_graph_impact",
    "dev_symbol_resolve", "job_start", "job_wait", "job_list", "job_log",
    "job_stop", "dev_secrets", "dev_working_set", "dev_files_list",
    "dev_files_explain",
    "dev_review_repo", "dev_environment", "dev_tools", "dev_capabilities",
    "goal_list", "goal_show", "goal_check", "goal_add", "goal_from_review", "goal_claim",
    "dev_graph_query", "dev_environments",
    "dev_check_iac", "dev_ops_record", "dev_ops_resume", "dev_live_import",
    "dev_live_diff", "dev_live_save", "dev_aws_cli_check",
    "dev_relay_plan", "dev_preflight_plan", "dev_runtime_status",
    "dev_runtime_logs", "research_fetch", "dev_maps",
]
VERBS = ["find", "node", "expand", "impact", "edit", "create", "check", "note"]
FEDERATION_TOOLS = ["federation_status", "federation_search", "federation_node", "federation_check"]

CORE = '''\
"""Core helpers."""


def add(a, b):
    """Add two numbers."""
    return a + b


def double(x):
    """Double x."""
    return add(x, x)


class Counter:
    """A counter."""

    def __init__(self):
        self.n = 0

    def bump(self, k=1):
        """Bump by k."""
        if k < 0:
            raise ValueError("negative bump")
        self.n = add(self.n, k)
        return self.n
'''
USER = '''\
"""Uses core."""
from pkg.core import double


def quadruple(x):
    """Four times x."""
    return double(double(x))
'''
TEST_CORE = '''\
from pkg.core import Counter, add


def test_add():
    assert add(1, 2) == 3


def test_bump_negative():
    assert Counter().bump(-1) == -1
'''
TEST_USER = '''\
from pkg.user import quadruple


def test_quadruple():
    assert quadruple(1) == 4
'''
README = """\
# Fixture

Intro text.

## Usage

Call `add`.

## Deploy

See deploy/app.yaml.
"""
YAML = "name: app\nreplicas: 2\n\nports:\n  - 80\n"
SEED = {"id": "demo", "version": "1.0.0", "depends_on": [],
        "description": "demo seed", "nodes": [],
        "rules": [{"name": "greet_rule", "match": [], "effects": []}]}


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", *args], check=True,
                   capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "conftest.py",
           "import os, sys\nsys.path.insert(0, os.path.dirname(__file__))\n")
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/core.py", CORE)
    _write(root, "pkg/user.py", USER)
    _write(root, "tests/test_core.py", TEST_CORE)
    _write(root, "tests/test_user.py", TEST_USER)
    _write(root, "README.md", README)
    _write(root, "deploy/app.yaml", YAML)
    _write(root, "seeds/demo.json", json.dumps(SEED))
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _memory(repo: Path) -> DevMemory:
    dump = repo.parent / "state" / "graph.json"
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, repo.parent / "state" / "journal.jsonl", root=repo)


@pytest.fixture()
def server(repo: Path) -> DevMemoryServer:
    return DevMemoryServer(_memory(repo), surface="graph")


def _rpc(server: DevMemoryServer, method: str, params: dict | None = None
         ) -> dict[str, Any]:
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}})


def call(server: DevMemoryServer, name: str, **args: Any) -> Any:
    reply = _rpc(server, "tools/call", {"name": name, "arguments": args})
    assert "error" not in reply, reply
    assert reply["result"]["isError"] is False, reply
    return json.loads(reply["result"]["content"][0]["text"])


def _keys(obj: Any) -> set[str]:
    """Every string in `obj` that looks like a node key."""
    out: set[str] = set()
    if isinstance(obj, dict):
        for v in obj.values():
            out |= _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _keys(v)
    elif isinstance(obj, str) and obj.split(":", 1)[0] in (
            "file", "sym", "test", "doc", "text", "seed", "seed_item", "repo") \
            and ":" in obj and "\n" not in obj and " " not in obj:
        out.add(obj)
    return out


# --------------------------------------------------------------------------- #
# surface switch
# --------------------------------------------------------------------------- #
def test_graph_surface_lists_verbs_and_federation_tools(server) -> None:
    tools = _rpc(server, "tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == VERBS + FEDERATION_TOOLS
    reply = _rpc(server, "tools/call", {"name": "dev_context",
                                        "arguments": {"task": "x"}})
    assert reply["error"]["code"] == -32602


def test_graph_surface_lists_adapter_tools(repo, seed_adapter) -> None:
    srv = DevMemoryServer(_memory(repo), surface="graph")
    names = [t["name"] for t in _rpc(srv, "tools/list")["result"]["tools"]]
    assert names == VERBS + FEDERATION_TOOLS + ["dev_graph_query"]
    hits = call(srv, "find", query="greet_rule")["hits"]
    assert hits[0]["key"] == "seed_item:demo::Rule::greet_rule"
    assert call(srv, "node", key=hits[0]["key"])["ok"] is True


def test_full_surface_unchanged(repo, seed_adapter) -> None:
    srv = DevMemoryServer(_memory(repo))
    names = [t["name"] for t in _rpc(srv, "tools/list")["result"]["tools"]]
    assert names == FULL_SURFACE_TOOLS
    assert DevMemoryServer(_memory(repo), surface="full")._schemas.keys() \
        == srv._schemas.keys()


def test_unknown_surface_is_refused(repo, monkeypatch) -> None:
    with pytest.raises(ValueError):
        DevMemoryServer(_memory(repo), surface="tiny")
    monkeypatch.setenv("OZAGIA_SURFACE", "tiny")
    with pytest.raises(SystemExit):
        main(["--dump", str(repo / "missing.json")])


def test_descriptions_fit_the_token_budget() -> None:
    assert [t["name"] for t in GRAPH_TOOLS] == list(GRAPH_TOOL_NAMES) == VERBS
    assert description_tokens() <= 1500
    for tool in GRAPH_TOOLS:
        assert tool["inputSchema"]["additionalProperties"] is False


def test_schemas_are_validated(server) -> None:
    reply = _rpc(server, "tools/call", {"name": "find",
                                        "arguments": {"query": "x", "bogus": 1}})
    assert reply["error"]["code"] == -32602
    reply = _rpc(server, "tools/call", {"name": "node", "arguments": {}})
    assert reply["error"]["code"] == -32602
    reply = _rpc(server, "tools/call", {"name": "node", "arguments": {
        "key": "file:pkg/core.py", "view": "everything"}})
    assert reply["error"]["code"] == -32602


# --------------------------------------------------------------------------- #
# find
# --------------------------------------------------------------------------- #
def test_find_ranks_exact_name_first(server) -> None:
    res = call(server, "find", query="add")
    top = res["hits"][0]
    assert top == {"key": "sym:pkg/core.py::add", "kind": "sym", "name": "add",
                   "file": "pkg/core.py", "line": 4,
                   "signature": "def add(a, b):",
                   "summary": "Add two numbers.", "score": top["score"]}
    assert res == call(server, "find", query="add")  # deterministic
    keys = [h["key"] for h in res["hits"]]
    assert "file:pkg/core.py" in keys
    assert "test:tests/test_core.py::test_add" in keys


def test_find_kinds_limit_and_prefix(server) -> None:
    res = call(server, "find", query="Counter", kinds=["sym"], limit=2)
    assert [h["key"] for h in res["hits"]] == [
        "sym:pkg/core.py::Counter", "sym:pkg/core.py::Counter.bump"]
    assert len(res["hits"]) == 2
    tests = call(server, "find", query="test_b", kinds=["test"])["hits"]
    assert tests[0]["key"] == "test:tests/test_core.py::test_bump_negative"
    doc = call(server, "find", query="Fixture", kinds=["doc"])["hits"]
    assert doc[0]["key"] == "doc:README.md"


def test_find_errors(server) -> None:
    assert call(server, "find", query="  ") == {"ok": False,
                                                "error": "empty query"}


# --------------------------------------------------------------------------- #
# node
# --------------------------------------------------------------------------- #
def test_node_outline_and_source(server) -> None:
    out = call(server, "node", key="sym:pkg/core.py::Counter.bump")
    assert out["lines"] == [20, 25]
    assert out["signature"] == "def bump(self, k=1):"
    assert out["summary"] == "Bump by k."
    assert out["edges"] == {"<contains_symbol": 1, "<defines": 1,
                            "defined_in": 1}
    assert "source" not in out
    src = call(server, "node", key="sym:pkg/core.py::Counter.bump",
               view="source")
    assert src["source"].startswith("    def bump(self, k=1):\n")
    assert src["source"].endswith("        return self.n\n")
    assert src["truncated"] is False


def test_node_file_text_only_with_full(server, repo) -> None:
    src = call(server, "node", key="file:pkg/core.py", view="source")
    assert "source" not in src and "source_note" in src
    full = call(server, "node", key="file:pkg/core.py", view="full")
    assert full["source"] == (repo / "pkg/core.py").read_text()
    assert "sym:pkg/core.py::add" in full["neighbours"]["defines"]


def test_node_file_text_is_capped(server, repo, monkeypatch) -> None:
    monkeypatch.setattr(graph_surface, "MAX_SOURCE_LINES", 3)
    full = call(server, "node", key="file:pkg/core.py", view="full")
    assert full["truncated"] is True
    assert full["source"] == "".join(CORE.splitlines(keepends=True)[:3])


def test_node_doc_and_text_chunks(server) -> None:
    doc = call(server, "node", key="doc:README.md", view="source")
    assert doc["source"] == README and doc["name"] == "Fixture"
    yaml = call(server, "node", key="file:deploy/app.yaml")
    assert yaml["chunks"] == ["text:deploy/app.yaml#L1-L3",
                              "text:deploy/app.yaml#L4-L5"]
    chunk = call(server, "node", key="text:deploy/app.yaml#L4-L5",
                 view="source")
    assert chunk["source"] == "ports:\n  - 80\n" and chunk["lines"] == [4, 5]


def test_node_errors(server) -> None:
    assert call(server, "node", key="sym:pkg/core.py::nope")["ok"] is False
    assert call(server, "node", key="text:deploy/app.yaml#L1-L2")["ok"] is False
    out = call(server, "node", key="file:../outside.txt")
    assert out["ok"] is False and "escapes" in out["error"]


# --------------------------------------------------------------------------- #
# expand, impact
# --------------------------------------------------------------------------- #
def test_expand_callers_and_graph_edges(server) -> None:
    res = call(server, "expand", keys=["sym:pkg/core.py::double"],
               edges=["callers"], depth=2)
    keys = [n["key"] for n in res["nodes"]]
    assert keys == ["sym:pkg/core.py::double", "sym:pkg/user.py::quadruple",
                    "test:tests/test_user.py::test_quadruple"]
    assert res["edges"] == [
        ["sym:pkg/user.py::quadruple", "calls", "sym:pkg/core.py::double"],
        ["test:tests/test_user.py::test_quadruple", "calls",
         "sym:pkg/user.py::quadruple"]]
    assert "symbol_graph_seconds" in res and res["truncated"] is False
    again = call(server, "expand", keys=["sym:pkg/core.py::double"],
                 edges=["callers"], depth=2)
    assert "symbol_graph_seconds" not in again  # built once per process

    res = call(server, "expand", keys=["file:pkg/user.py"], edges=["imports"])
    assert res["edges"] == [["file:pkg/user.py", "imports", "file:pkg/core.py"],
                            ["file:tests/test_user.py", "imports",
                             "file:pkg/user.py"]]


def test_expand_default_edges_and_limit(server) -> None:
    res = call(server, "expand", keys=["file:pkg/core.py"], limit=3)
    assert len(res["nodes"]) == 3 and res["truncated"] is True
    nodes = {n["key"] for n in res["nodes"]}
    for src, _typ, tgt in res["edges"]:
        assert src in nodes and tgt in nodes
    assert call(server, "expand", keys=["sym:nope.py::x"])["ok"] is False


def test_impact_includes_importer_and_its_test(server) -> None:
    res = call(server, "impact", keys=["sym:pkg/core.py::double"])
    syms = {s["key"]: s for s in res["symbols"]}
    files = {f["key"]: f for f in res["files"]}
    tests = {t["key"]: t for t in res["tests"]}
    assert syms["sym:pkg/user.py::quadruple"]["why"] == [
        "sym:pkg/user.py::quadruple", "calls", "sym:pkg/core.py::double"]
    assert "file:pkg/user.py" in files
    assert "test:tests/test_user.py::test_quadruple" in tests
    for item in res["symbols"] + res["files"] + res["tests"]:
        assert item["why"][0] == item["key"]
        assert item["why"][-1] == "sym:pkg/core.py::double"
    by_file = call(server, "impact", keys=["file:pkg/core.py"], depth=1)
    assert "file:pkg/user.py" in {f["key"] for f in by_file["files"]}
    assert "test:tests/test_core.py::test_add" in {
        t["key"] for t in by_file["tests"]}
    assert by_file["truncated"] is False
    cut = call(server, "impact", keys=["sym:pkg/core.py::double"], limit=1)
    assert cut["truncated"] is True and len(cut["tests"]) == 1
    assert cut["counts"] == res["counts"] and cut["counts"]["tests"] > 1


def test_keys_round_trip(server) -> None:
    results = [
        call(server, "find", query="add"),
        call(server, "find", query="Counter"),
        call(server, "expand", keys=["file:pkg/core.py"], depth=2, limit=100),
        call(server, "expand", keys=["sym:pkg/core.py::add"],
             edges=["callers", "callees"], depth=2),
        call(server, "impact", keys=["sym:pkg/core.py::add"], depth=3),
        call(server, "node", key="file:deploy/app.yaml"),
        call(server, "node", key="doc:README.md", view="full"),
    ]
    keys = set().union(*(_keys(r) for r in results))
    assert len(keys) > 10
    for key in sorted(keys):
        assert call(server, "node", key=key)["ok"] is True, key
    for r in results:
        assert '"id"' not in json.dumps(r)


# --------------------------------------------------------------------------- #
# edit
# --------------------------------------------------------------------------- #
def test_edit_inside_key_range(server, repo) -> None:
    before = (repo / "pkg/core.py").read_text()
    preview = call(server, "edit", key_or_file="sym:pkg/core.py::add",
                   old_string="return a + b", new_string="return b + a")
    assert preview["ok"] and preview["applied"] is False
    assert preview["keys"] == ["sym:pkg/core.py::add"]
    assert (repo / "pkg/core.py").read_text() == before
    stale = call(server, "edit", key_or_file="sym:pkg/core.py::add",
                 old_string="return a + b", new_string="return b + a",
                 apply=True, base_sha256="0" * 64)
    assert stale["ok"] is False and "stale base" in stale["error"]
    base = hashlib.sha256((repo / "pkg/core.py").read_bytes()).hexdigest()
    done = call(server, "edit", key_or_file="sym:pkg/core.py::add",
                old_string="return a + b", new_string="return b + a",
                apply=True, base_sha256=base)
    assert done["applied"] is True and done["parse_check"] == "ok"
    assert done["graph_delta"]["changed"]
    assert "return b + a" in (repo / "pkg/core.py").read_text()
    assert set(done) <= {"ok", "applied", "file", "keys", "parse_check",
                         "broken_references", "broken_references_truncated",
                         "diff_view", "diff_truncated", "new_sha256",
                         "backup", "graph_delta"}


def test_edit_outside_key_range_is_refused(server, repo) -> None:
    before = (repo / "pkg/core.py").read_text()
    out = call(server, "edit", key_or_file="sym:pkg/core.py::double",
               old_string="return a + b", new_string="return 0", apply=True)
    assert out["ok"] is False and out["range"] == [9, 11]
    assert out["matches"] == [{"key": "sym:pkg/core.py::add", "lines": [6, 6]}]
    assert (repo / "pkg/core.py").read_text() == before


def test_edit_by_path_and_failures(server, repo) -> None:
    out = call(server, "edit", key_or_file="pkg/core.py",
               old_string="def double(x):", new_string="def double(x, y):",
               apply=True)
    assert out["applied"] is True
    assert any(r["kind"] == "arity" for r in out["broken_references"])
    miss = call(server, "edit", key_or_file="pkg/core.py",
                old_string="return a - b", new_string="x")
    assert miss["ok"] is False and miss["nearest"]
    many = call(server, "edit", key_or_file="pkg/core.py",
                old_string="self.n", new_string="self.m")
    assert many["ok"] is False and len(many["matches"]) > 1
    broken = call(server, "edit", key_or_file="pkg/core.py",
                  old_string="def add(a, b):", new_string="def add(a, b)")
    assert broken["ok"] is False and broken["parse_check"] == "refused"
    esc = call(server, "edit", key_or_file="../x.py", old_string="a",
               new_string="b")
    assert esc["ok"] is False and "escapes" in esc["error"]


def test_edit_text_chunk_key(server, repo) -> None:
    out = call(server, "edit", key_or_file="text:deploy/app.yaml#L1-L3",
               old_string="replicas: 2", new_string="replicas: 3", apply=True)
    assert out["applied"] is True
    out = call(server, "edit", key_or_file="text:deploy/app.yaml#L4-L5",
               old_string="name: app", new_string="name: b")
    assert out["ok"] is False and out["range"] == [4, 5]


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
def test_create_new_file(server, repo) -> None:
    preview = call(server, "create", path="pkg/extra.py",
                   content="def extra():\n    return 1\n")
    assert preview["applied"] is False and not (repo / "pkg/extra.py").exists()
    out = call(server, "create", path="pkg/extra.py",
               content="def extra():\n    return 1\n", apply=True)
    assert out["applied"] is True
    assert set(out["keys"]) >= {"file:pkg/extra.py", "sym:pkg/extra.py::extra"}
    assert call(server, "node", key="sym:pkg/extra.py::extra")["ok"] is True
    again = call(server, "create", path="pkg/extra.py", content="x = 1\n")
    assert again["ok"] is False and "use edit" in again["error"]
    bad = call(server, "create", path="pkg/bad.py", content="def (:\n",
               apply=True)
    assert bad["ok"] is False and not (repo / "pkg/bad.py").exists()
    esc = call(server, "create", path="../evil.py", content="x = 1\n")
    assert esc["ok"] is False
    both = call(server, "create", path="a.py", parent_key="file:pkg/core.py",
                content="x = 1\n")
    assert both["ok"] is False


def test_create_function_in_file(server, repo) -> None:
    out = call(server, "create", parent_key="file:pkg/user.py",
               content="def triple(x):\n    return 3 * x\n", apply=True)
    assert out["keys"] == ["sym:pkg/user.py::triple"]
    assert (repo / "pkg/user.py").read_text() == USER + (
        "\n\ndef triple(x):\n    return 3 * x\n")
    found = call(server, "find", query="triple")["hits"]
    assert found[0]["key"] == "sym:pkg/user.py::triple"


def test_create_after_top_level_function(server, repo) -> None:
    out = call(server, "create", parent_key="sym:pkg/core.py::add",
               content="def sub(a, b):\n    return a - b\n", apply=True)
    assert out["keys"] == ["sym:pkg/core.py::sub"]
    text = (repo / "pkg/core.py").read_text()
    assert "    return a + b\n\n\ndef sub(a, b):\n    return a - b\n\n\n" \
           "def double(x):" in text


def test_create_method_in_class(server, repo) -> None:
    content = textwrap.dedent("""\
        def reset(self):
            \"\"\"Back to zero.\"\"\"
            self.n = 0
        """)
    out = call(server, "create", parent_key="sym:pkg/core.py::Counter",
               content=content, apply=True)
    assert out["keys"] == ["sym:pkg/core.py::Counter.reset"]
    assert (repo / "pkg/core.py").read_text().endswith(
        "        return self.n\n\n    def reset(self):\n"
        "        \"\"\"Back to zero.\"\"\"\n        self.n = 0\n")
    node = call(server, "node", key="sym:pkg/core.py::Counter.reset")
    assert node["summary"] == "Back to zero."
    nested = call(server, "create", parent_key="sym:pkg/core.py::Counter.bump",
                  content="def x(self):\n    pass\n")
    assert nested["ok"] is False and "nested" in nested["error"]
    notdef = call(server, "create", parent_key="file:pkg/core.py",
                  content="x = 1\n")
    assert notdef["ok"] is False


# --------------------------------------------------------------------------- #
# check, note
# --------------------------------------------------------------------------- #
def test_check_by_symbol_key_runs_covering_tests(server) -> None:
    res = call(server, "check", keys=["sym:pkg/core.py::Counter.bump"])
    assert res["selectors"] == ["tests/test_core.py"]
    assert res["status"] == "failed" and res["ok"] is False
    assert res["counts"]["passed"] == 1 and res["counts"]["failed"] == 1
    failed = [t for t in res["tests"] if t["outcome"] == "failed"]
    assert failed[0]["key"] == "test:tests/test_core.py::test_bump_negative"
    assert failed[0]["message"] == "ValueError: negative bump"
    assert failed[0]["frames"][-1] == "sym:pkg/core.py::Counter.bump"
    for key in _keys(res):
        assert call(server, "node", key=key)["ok"] is True, key


def test_check_tests_and_ignored(server) -> None:
    res = call(server, "check",
               tests=["test:tests/test_user.py::test_quadruple",
                      "tests/test_core.py::test_add", "nowhere.py::t"])
    assert res["ok"] is True and res["counts"]["passed"] == 2
    assert res["ignored"] == [{"key": "nowhere.py::t",
                               "reason": "no such file: nowhere.py"}]
    none = call(server, "check", keys=["doc:README.md"])
    assert none["ok"] is False and none["error"] == "no tests to run"


def test_check_caps_test_files(server, monkeypatch) -> None:
    monkeypatch.setattr(graph_surface, "MAX_CHECK_TEST_FILES", 1)
    res = call(server, "check", keys=["sym:pkg/core.py::double",
                                      "sym:pkg/user.py::quadruple"])
    assert res["test_files_truncated"] is True
    assert res["selectors"] == ["tests/test_core.py"]


def test_note(server) -> None:
    out = call(server, "note", text="double is hot",
               keys=["sym:pkg/core.py::double"])
    assert out["entry"]["kind"] == "note"
    assert out["entry"]["keys"] == ["sym:pkg/core.py::double"]
    assert server.mem.journal_recent(1)[0]["summary"] == "double is hot"
    assert call(server, "note", text="x", keys=["sym:nope.py::y"])["ok"] is False


def test_graph_surface_with_research_mode(repo) -> None:
    pytest.importorskip("ozagia.research.mcp")
    srv = DevMemoryServer(_memory(repo), mode="combined", surface="graph")
    names = [t["name"] for t in _rpc(srv, "tools/list")["result"]["tools"]]
    assert names[:len(VERBS)] == VERBS
    assert any(n.startswith("research_") for n in names[len(VERBS):])
    assert not any(n.startswith("dev_") for n in names)


# --------------------------------------------------------------------------- #
# review fixes 11 and 17
# --------------------------------------------------------------------------- #
def _error(server: DevMemoryServer, name: str, **args: Any) -> str:
    reply = _rpc(server, "tools/call", {"name": name, "arguments": args})
    if "error" in reply:
        return str(reply["error"])
    text = reply["result"]["content"][0]["text"]
    if reply["result"]["isError"] is False:
        doc = json.loads(text)
        assert doc["ok"] is False, doc
        return str(doc["error"])
    return text


def test_edit_and_create_refuse_git_and_the_state_dir(server, repo) -> None:
    head = (repo / ".git" / "HEAD").read_text()
    msg = _error(server, "edit", key_or_file=".git/HEAD", old_string="ref:",
                 new_string="ref: ", apply=True)
    assert "inside .git" in msg
    assert (repo / ".git" / "HEAD").read_text() == head
    _write(repo, ".ozagia/notes.md", "# notes\n")
    msg = _error(server, "edit", key_or_file=".ozagia/notes.md", old_string="notes",
                 new_string="secrets", apply=True)
    assert "state directory" in msg
    assert (repo / ".ozagia" / "notes.md").read_text() == "# notes\n"
    msg = _error(server, "create", path="pkg/.git/x.py", content="X = 1\n", apply=True)
    assert "inside .git" in msg


def test_check_uses_the_run_python(server, repo, monkeypatch) -> None:
    from ozagia import check
    seen: list[dict] = []
    real = check.run_tests

    def spy(root, selectors, **kw):
        seen.append(kw)
        return real(root, selectors, **kw)

    monkeypatch.setattr(check, "run_tests", spy)
    monkeypatch.setenv("OZAGIA_RUN_PYTHON", sys.executable)
    call(server, "check", tests=["tests/test_core.py::test_add"])
    assert seen[-1]["python"] == sys.executable
    monkeypatch.delenv("OZAGIA_RUN_PYTHON")
    venv = repo / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    call(server, "check", tests=["tests/test_core.py::test_add"])
    assert seen[-1]["python"] == str(venv / "python")
