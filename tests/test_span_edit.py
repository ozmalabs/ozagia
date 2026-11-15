"""Text-span edits mapped to graph nodes (ozagia.edit.span_edit)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ozagia.core import memory as M
from ozagia.core.memory import DevMemory, build_index
from ozagia.edit import span_edit as S
from ozagia.edit.code_edit import CodeEditError
from ozagia.edit.cst import tree_sitter_available
from ozagia.edit.span_edit import apply_span_edit, locate
from ozagia.index.text_nodes import locate_chunk, text_chunks
from ozagia.integrations.mcp.server import DevMemoryServer

MOD = '''"""Module."""


def helper(a, b=1):
    """Help."""
    return a + b


def other(x):
    value = helper(x)
    return value


class Box:
    def __init__(self):
        self.n = 0

    def m(self, a):
        return a

    def use(self):
        return self.m(1)


def dup():
    value = 1
    return value


def shadow(helper):
    return helper()
'''

USE = '''"""Uses mod."""
from pkg.mod import helper, other
from pkg import mod


def run():
    return helper(1) + other(2)


def run_star(args):
    return helper(*args)


def run_mod():
    return mod.helper(3)


def run_kw():
    return helper(1, b=2)
'''

TEST_MOD = '''from pkg.mod import helper


def test_helper():
    assert helper(1) == 2
'''

README = """# Project

Intro.

## Install

Run make.

## Usage

### Advanced

Use flags here.
"""

YAML = """name: app

steps:
  - build
  - test

deploy:
  target: prod
"""


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(root), "PATH": "/usr/bin:/bin"})


@pytest.fixture
def layout(monkeypatch):
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "IMPORT_ROOTS", ())
    monkeypatch.setattr(M, "_PATH_RE", M.path_mention_re(["pkg", "tests"]))
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)


@pytest.fixture
def repo(tmp_path: Path, layout) -> Path:
    root = tmp_path / "repo"
    _write(root / "pkg" / "__init__.py", '"""The package."""\n')
    _write(root / "pkg" / "mod.py", MOD)
    _write(root / "pkg" / "use.py", USE)
    _write(root / "tests" / "test_mod.py", TEST_MOD)
    _write(root / "README.md", README)
    _write(root / "config.json",
           json.dumps({"a": {"b": [1, {"c": "deep"}]}, "name": "x"}, indent=2)
           + "\n")
    _write(root / "deploy.yaml", YAML)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def mem(repo: Path, tmp_path: Path) -> DevMemory:
    dump = tmp_path / "state" / "graph.json"
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=repo)


def _backups(root: Path) -> list[Path]:
    d = root / ".ozagia" / "edits" / "backups"
    return sorted(d.iterdir()) if d.is_dir() else []


def _refs(result: dict, kind: str) -> list[dict]:
    return [r for r in result["broken_references"] if r["kind"] == kind]


# --------------------------------------------------------------------------- #
# locate
# --------------------------------------------------------------------------- #
def test_unique_match_names_enclosing_function(repo: Path) -> None:
    res = locate(repo, "pkg/mod.py", "return a + b")
    assert res["ok"] is True
    (m,) = res["matches"]
    assert m["key"] == "sym:pkg/mod.py::helper"
    assert m["start_line"] == m["end_line"] == 6
    assert MOD[m["start"]:m["end"]] == "return a + b"
    assert res["keys"] == ["sym:pkg/mod.py::helper"]


def test_multiline_match_names_innermost_common_def(repo: Path) -> None:
    res = locate(repo, "pkg/mod.py", "    def m(self, a):\n        return a\n")
    assert res["matches"][0]["key"] == "sym:pkg/mod.py::Box.m"
    span = "        self.n = 0\n\n    def m(self, a):"
    assert locate(repo, "pkg/mod.py", span)["matches"][0]["key"] == \
        "sym:pkg/mod.py::Box"


def test_multiple_matches_error_lists_every_key(repo: Path) -> None:
    res = locate(repo, "pkg/mod.py", "    return value\n")
    assert res["ok"] is False
    assert [m["key"] for m in res["matches"]] == [
        "sym:pkg/mod.py::other", "sym:pkg/mod.py::dup"]
    assert "sym:pkg/mod.py::other" in res["error"]
    assert "sym:pkg/mod.py::dup" in res["error"]
    assert "L11" in res["error"]
    out = apply_span_edit(repo, "pkg/mod.py", "    return value\n",
                          "    return value + 0\n", write=True)
    assert out["ok"] is False and out["applied"] is False
    assert (repo / "pkg" / "mod.py").read_text() == MOD


def test_replace_all(repo: Path) -> None:
    out = apply_span_edit(repo, "pkg/mod.py", "    return value\n",
                          "    return value + 0\n", replace_all=True,
                          write=True)
    assert out["ok"] and out["applied"]
    assert len(out["matches"]) == 2
    text = (repo / "pkg" / "mod.py").read_text()
    assert text.count("return value + 0\n") == 2
    assert out["keys"] == ["sym:pkg/mod.py::other", "sym:pkg/mod.py::dup"]


def test_zero_matches_lists_nearest_lines(repo: Path) -> None:
    res = locate(repo, "pkg/mod.py", "return a - b")
    assert res["ok"] is False
    assert "not found" in res["error"]
    assert 1 <= len(res["nearest"]) <= 3
    best = res["nearest"][0]
    assert best["line"] == 6
    assert best["key"] == "sym:pkg/mod.py::helper"


def test_crlf_file_matches_lf_old_string(repo: Path) -> None:
    p = repo / "pkg" / "crlf.py"
    p.write_bytes(b"def f():\r\n    x = 1\r\n    return x\r\n")
    res = locate(repo, "pkg/crlf.py", "x = 1\n    return x")
    assert res["ok"] and res["newline_converted"] is True
    assert res["matches"][0]["key"] == "sym:pkg/crlf.py::f"
    out = apply_span_edit(repo, "pkg/crlf.py", "x = 1\n    return x",
                          "x = 2\n    y = x\n    return y", write=True)
    assert out["ok"] and out["applied"]
    data = p.read_bytes()
    assert data == b"def f():\r\n    x = 2\r\n    y = x\r\n    return y\r\n"


# --------------------------------------------------------------------------- #
# keys per file type
# --------------------------------------------------------------------------- #
def test_markdown_section_reported(repo: Path) -> None:
    res = locate(repo, "README.md", "Use flags here.")
    (m,) = res["matches"]
    assert m["key"] == "doc:README.md"
    assert m["section"] == ["Project", "Usage", "Advanced"]
    assert locate(repo, "README.md", "Run make.")["matches"][0]["section"] == [
        "Project", "Install"]


def test_json_pointer(repo: Path) -> None:
    res = locate(repo, "config.json", '"deep"')
    (m,) = res["matches"]
    assert m["key"] == "file:config.json"
    assert m["pointer"] == "/a/b/1/c"


def test_yaml_gets_text_chunk_key(repo: Path) -> None:
    res = locate(repo, "deploy.yaml", "target: prod")
    (m,) = res["matches"]
    want = locate_chunk(text_chunks("deploy.yaml", YAML), m["start_line"])
    assert m["key"] == want["key"]
    assert m["key"].startswith("text:deploy.yaml#L")
    out = apply_span_edit(repo, "deploy.yaml", "prod", "staging")
    assert out["ok"] and out["parse_check"] == "none"
    assert out["broken_references"] == []


# --------------------------------------------------------------------------- #
# parse guard
# --------------------------------------------------------------------------- #
def test_parse_guard_refuses_python_syntax_break(repo: Path) -> None:
    out = apply_span_edit(repo, "pkg/mod.py", "return a + b", "return a +",
                          write=True)
    assert out["ok"] is False and out["applied"] is False
    assert out["parse_check"] == "refused"
    assert out["parse_error"]["line"] == 6
    assert out["parse_error"]["message"]
    assert out["key"] == "sym:pkg/mod.py::helper"
    assert (repo / "pkg" / "mod.py").read_text() == MOD
    assert _backups(repo) == []


def test_already_broken_file_is_not_guarded(repo: Path) -> None:
    _write(repo / "pkg" / "broken.py", "def f(:\n    pass\n")
    out = apply_span_edit(repo, "pkg/broken.py", "pass", "return (", write=True)
    assert out["ok"] and out["applied"]
    assert out["parse_check"] == "was_broken"
    assert out["broken_references_info"]["checked"] is False


def test_json_parse_guard(repo: Path) -> None:
    before = (repo / "config.json").read_text()
    out = apply_span_edit(repo, "config.json", '"deep"', '"deep",', write=True)
    assert out["ok"] is False and out["parse_check"] == "refused"
    assert out["parse_error"]["line"] >= 1
    assert (repo / "config.json").read_text() == before
    ok = apply_span_edit(repo, "config.json", '"deep"', '"deeper"', write=True)
    assert ok["ok"] and ok["parse_check"] == "ok"


@pytest.mark.skipif(not tree_sitter_available("rust"),
                    reason="tree-sitter rust grammar not installed")
def test_rust_parse_guard(repo: Path) -> None:
    _write(repo / "src" / "lib.rs", "fn main() {\n    let x = 1;\n}\n")
    out = apply_span_edit(repo, "src/lib.rs", "let x = 1;", "let x = (1;")
    assert out["ok"] is False and out["parse_check"] == "refused"
    assert out["key"] == "file:src/lib.rs"
    ok = apply_span_edit(repo, "src/lib.rs", "let x = 1;", "let x = 2;")
    assert ok["ok"] and ok["parse_check"] == "ok"


def test_rust_parse_check_unavailable(repo: Path, monkeypatch) -> None:
    import ozagia.edit.cst as cst
    monkeypatch.setattr(cst, "_make_parser", lambda lang: (None, "absent"))
    _write(repo / "src" / "lib.rs", "fn main() {}\n")
    out = apply_span_edit(repo, "src/lib.rs", "{}", "{")
    assert out["ok"] and out["parse_check"] == "unavailable"


# --------------------------------------------------------------------------- #
# transaction
# --------------------------------------------------------------------------- #
def test_preview_then_apply(repo: Path) -> None:
    p = repo / "pkg" / "mod.py"
    prev = apply_span_edit(repo, "pkg/mod.py", "return a + b", "return b + a")
    assert prev["ok"] and prev["applied"] is False and prev["backup"] is None
    assert p.read_text() == MOD
    assert "+    return b + a" in prev["diff_view"]
    assert prev["parse_check"] == "ok"
    out = apply_span_edit(repo, "pkg/mod.py", "return a + b", "return b + a",
                          write=True)
    assert out["applied"] and Path(out["backup"]).is_file()
    assert Path(out["backup"]).read_text() == MOD
    assert "return b + a" in p.read_text()
    assert out["new_sha256"] == prev["new_sha256"] == out["after"]["sha256"]
    assert out["before"]["lines"] == out["after"]["lines"]
    assert out["graph_stale"] is True


def test_stale_base_refused(repo: Path) -> None:
    with pytest.raises(CodeEditError, match="stale base"):
        apply_span_edit(repo, "pkg/mod.py", "return a + b", "return b",
                        base_sha256="0" * 64, write=True)
    assert (repo / "pkg" / "mod.py").read_text() == MOD


def test_binary_and_identical_refused(repo: Path) -> None:
    (repo / "blob.dat").write_bytes(b"ab\0cd")
    with pytest.raises(CodeEditError, match="binary"):
        locate(repo, "blob.dat", "ab")
    with pytest.raises(CodeEditError, match="identical"):
        apply_span_edit(repo, "pkg/mod.py", "return a + b", "return a + b")


# --------------------------------------------------------------------------- #
# DevMemory: graph delta, broken references, journal
# --------------------------------------------------------------------------- #
def test_rename_function_delta_and_missing_names(mem: DevMemory,
                                                 repo: Path) -> None:
    out = mem.span_edit("pkg/mod.py", "def helper(a, b=1):",
                        "def helper2(a, b=1):", apply=True)
    assert out["ok"] and out["applied"]
    delta = out["graph_delta"]
    assert "sym:pkg/mod.py::helper" in delta["removed"]
    assert "sym:pkg/mod.py::helper2" in delta["added"]
    assert mem.node_by_key("sym:pkg/mod.py::helper2") is not None
    missing = _refs(out, "missing_name")
    at = {(r["file"], r["line"], r["key"]) for r in missing}
    assert ("pkg/use.py", 2, "file:pkg/use.py") in at
    assert ("tests/test_mod.py", 1, "file:tests/test_mod.py") in at
    # mod.helper(...) through a module import, and the call left in mod.py.
    assert ("pkg/use.py", 15, "sym:pkg/use.py::run_mod") in at
    assert ("pkg/mod.py", 10, "sym:pkg/mod.py::other") in at
    assert all(r["name"] == "helper" for r in missing)
    # The parameter named `helper` in shadow() shadows the function.
    assert not any(r["key"] == "sym:pkg/mod.py::shadow" for r in missing)
    assert out["broken_references_info"]["removed_names"] == ["helper"]
    assert out["broken_references_info"]["importers"] == 2


def test_required_parameter_reports_arity(mem: DevMemory) -> None:
    out = mem.span_edit("pkg/mod.py", "def helper(a, b=1):",
                        "def helper(a, c, b=1):")
    assert out["ok"] and out["applied"] is False
    arity = _refs(out, "arity")
    keys = {r["key"] for r in arity}
    assert {"sym:pkg/mod.py::other", "sym:pkg/use.py::run",
            "sym:pkg/use.py::run_mod",
            "test:tests/test_mod.py::test_helper"} <= keys
    # helper(*args) cannot be judged; helper(1, b=2) still lacks c.
    assert "sym:pkg/use.py::run_star" not in keys
    assert "sym:pkg/use.py::run_kw" in keys
    assert out["broken_references_info"]["calls_skipped_star_args"] == 1
    run = next(r for r in arity if r["key"] == "sym:pkg/use.py::run")
    assert run["call"] == "1 positional"
    assert run["signature"] == "helper(a, c, b=1)"
    assert "missing 'c'" in run["problem"]
    assert not any(r["key"] == "sym:pkg/mod.py::shadow" for r in arity)


def test_renamed_keyword_reports_arity(mem: DevMemory) -> None:
    out = mem.span_edit("pkg/mod.py", "def helper(a, b=1):",
                        "def helper(a, bb=1):")
    arity = _refs(out, "arity")
    assert [r["key"] for r in arity] == ["sym:pkg/use.py::run_kw"]
    assert arity[0]["call"] == "1 positional, kw b"
    assert "unexpected keyword 'b'" in arity[0]["problem"]


def test_method_arity_via_self(mem: DevMemory) -> None:
    out = mem.span_edit("pkg/mod.py", "    def m(self, a):",
                        "    def m(self, a, b):")
    arity = _refs(out, "arity")
    assert len(arity) == 1
    assert arity[0]["key"] == "sym:pkg/mod.py::Box.use"
    assert arity[0]["file"] == "pkg/mod.py" and arity[0]["name"] == "Box.m"
    assert arity[0]["signature"] == "m(self, a, b)"


def test_without_memory_only_same_file(repo: Path) -> None:
    out = apply_span_edit(repo, "pkg/mod.py", "def helper(a, b=1):",
                          "def helper(a, c, b=1):")
    assert {r["file"] for r in out["broken_references"]} == {"pkg/mod.py"}
    assert out["broken_references_info"]["importers"] == 0


def test_broken_references_truncated(mem: DevMemory, monkeypatch) -> None:
    monkeypatch.setattr(S, "MAX_BROKEN_REFERENCES", 1)
    out = mem.span_edit("pkg/mod.py", "def helper(a, b=1):",
                        "def helper(a, c, b=1):")
    assert len(out["broken_references"]) == 1
    assert out["broken_references_truncated"] is True


def test_journal_entry_written(mem: DevMemory) -> None:
    mem.span_edit("pkg/mod.py", "return a + b", "return b + a", task="swap")
    assert mem.journal_recent() == []
    out = mem.span_edit("pkg/mod.py", "return a + b", "return b + a",
                        apply=True, task="swap")
    (entry,) = mem.journal_recent()
    assert entry["kind"] == "span_edit" and entry["task"] == "swap"
    assert entry["files"] == ["pkg/mod.py"]
    assert entry["keys"] == ["sym:pkg/mod.py::helper"]
    assert entry["new_sha256"] == out["new_sha256"]
    assert entry["old_string"] == "return a + b"
    assert out["journaled"] == entry
    assert "file:pkg/mod.py" in out["graph_delta"]["changed"]


def test_apply_must_be_boolean(mem: DevMemory) -> None:
    with pytest.raises(ValueError, match="apply"):
        mem.span_edit("pkg/mod.py", "return a + b", "return b", apply="yes")


def test_mcp_dev_span_edit(mem: DevMemory, repo: Path) -> None:
    server = DevMemoryServer(mem)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                            "params": {}})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    assert "dev_span_edit" in tools
    assert set(tools["dev_span_edit"]["inputSchema"]["properties"]) == {
        "file", "old_string", "new_string", "replace_all", "apply",
        "base_sha256", "task"}

    def call(args: dict) -> dict:
        resp = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "dev_span_edit",
                                         "arguments": args}})
        assert resp["result"]["isError"] is False, resp
        return json.loads(resp["result"]["content"][0]["text"])

    args = {"file": "pkg/mod.py", "old_string": "return a + b",
            "new_string": "return b + a"}
    prev = call(args)
    assert prev["ok"] and prev["applied"] is False
    assert prev["keys"] == ["sym:pkg/mod.py::helper"]
    assert (repo / "pkg" / "mod.py").read_text() == MOD
    done = call({**args, "apply": "true", "base_sha256": prev["base_sha256"]})
    assert done["applied"] and "graph_delta" in done
    assert "return b + a" in (repo / "pkg" / "mod.py").read_text()
    assert mem.journal_recent()[-1]["kind"] == "span_edit"
