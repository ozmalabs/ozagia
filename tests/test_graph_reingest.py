"""Stable node keys, incremental reingest and refresh (ozagia.core.reingest).

The key gate is the equivalence property: after any sequence of edits, each
followed by `DevMemory.reingest`, the graph equals a fresh `build_index` of
the final tree, compared by node (type, key, attrs) and edge (source key,
type, target key) multisets.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from ozagia.core import memory as M
from ozagia.core.delta import GraphDelta
from ozagia.core.keys import key_file
from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.mcp.server import DevMemoryServer

# Repo attrs that record when or against which tree the snapshot was made.
VOLATILE = {"generated_at", "tree_digest", "tree_digest_error"}


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
    """A fixed indexing layout, independent of the host configuration."""
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ("README.md", "docs/**/*.md"))
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ("README.md", "docs/**/*.md"))
    monkeypatch.setattr(M, "IMPORT_ROOTS", ("src",))
    monkeypatch.setattr(M, "_PATH_RE", M.path_mention_re(
        ["pkg", "src", "tests", "docs", "rs", "seeds"]))
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)


def _fixture(root: Path, *, git: bool = True) -> None:
    _write(root / "pkg" / "__init__.py", '"""The package."""\n')
    _write(root / "pkg" / "a.py", '''"""Module a."""
from pkg.b import beta
from pkg.future import later
from lib.x import x


class Widget:
    """A widget."""

    def size(self) -> int:
        return 1


def helper(n):
    """Double n."""
    return n * 2
''')
    _write(root / "pkg" / "b.py", '''"""Module b."""
from . import a


def beta():
    return 2
''')
    _write(root / "src" / "lib" / "x.py", '"""Lib x."""\n\ndef x():\n    return 0\n')
    _write(root / "rs" / "lib.rs", "mod util;\npub fn run() {}\nfn run() {}\n"
           "impl Thing {}\nstruct Thing;\n")
    _write(root / "rs" / "util.rs", "pub fn util() {}\n")
    _write(root / "tests" / "test_a.py", '''"""Tests for a; see pkg/b.py."""
from pkg.a import helper


def test_one():
    """One."""
    assert helper(1) == 2


class TestGroup:
    def test_two(self):
        assert True
''')
    _write(root / "docs" / "guide.md", "# Guide\n\nSee `pkg/a.py` and "
           "`tests/test_a.py`.\n")
    _write(root / "README.md", "# Fixture\n\n`pkg/b.py`\n")
    _write(root / "results" / "failure_baseline.json", json.dumps({
        "entries": [{"node": "tests/test_a.py::test_one",
                     "file": "tests/test_a.py", "kind": "FAILED",
                     "message": "boom", "class": "X"},
                    {"node": "tests/test_a.py::test_two",
                     "file": "tests/test_a.py", "kind": "FAILED",
                     "message": "boom", "class": "X"}]}))
    _write(root / "notes.txt", "not indexed\n")
    _write(root / "pkg" / "data.json", "{}\n")
    if git:
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "initial")
    # Not indexed and not in the history: only mentions give it a File node.
    _write(root / "pkg" / "late.json", "[]\n")


def _canon(graph: dict) -> tuple[Counter, Counter]:
    by_id = {}
    nodes = Counter()
    for n in graph["nodes"]:
        attrs = {k: v for k, v in n["attrs"].items()
                 if not (n["type"] == "Repo" and k in VOLATILE)}
        by_id[n["id"]] = n["key"]
        nodes[json.dumps([n["type"], n["key"], attrs], sort_keys=True)] += 1
    edges = Counter(json.dumps([by_id[e["src"]], e["type"], by_id[e["tgt"]],
                                e.get("attrs") or {}])
                    for e in graph["edges"])
    return nodes, edges


def _diff(a: tuple[Counter, Counter], b: tuple[Counter, Counter]) -> str:
    out = []
    for label, x, y in (("node", a[0], b[0]), ("edge", a[1], b[1])):
        for k in (x - y):
            out.append(f"only reingest {label}: {k}")
        for k in (y - x):
            out.append(f"only fresh {label}: {k}")
    return "\n".join(out[:40])


def _build(root: Path, out: Path) -> dict:
    build_index(root, out, include_docs="features", include_git=True)
    return json.loads(out.read_text())


def _assert_equivalent(mem: DevMemory, root: Path, fresh_out: Path) -> None:
    got = _canon(mem.snapshot_dict())
    want = _canon(_build(root, fresh_out))
    assert got == want, _diff(got, want)


@pytest.fixture
def repo(tmp_path: Path, layout) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    _fixture(root)
    dump = tmp_path / "state" / "graph.json"
    _build(root, dump)
    return root, dump


# --------------------------------------------------------------------------- #
# keys
# --------------------------------------------------------------------------- #
def test_keys_on_every_node_and_version(repo) -> None:
    root, dump = repo
    g = json.loads(dump.read_text())
    assert g["version"] == 2
    assert all(n.get("key") for n in g["nodes"])
    keys = [n["key"] for n in g["nodes"]]
    assert len(keys) == len(set(keys))
    by_type = {}
    for n in g["nodes"]:
        by_type.setdefault(n["type"], []).append(n["key"])
    for typ in ("Repo", "File", "Symbol", "Test", "Doc", "Commit", "Failure"):
        assert by_type.get(typ), typ
    assert by_type["Repo"] == ["repo:"]
    assert "sym:pkg/a.py::Widget.size" in keys
    assert "test:tests/test_a.py::TestGroup.test_two" in keys
    assert "doc:docs/guide.md" in keys
    assert "failure:tests/test_a.py::test_one" in keys
    assert any(k.startswith("commit:") for k in by_type["Commit"])
    mem = DevMemory(dump, root / "j.jsonl")
    nid = mem.node_by_key("sym:pkg/a.py::helper")
    assert nid is not None and mem.key_of(nid) == "sym:pkg/a.py::helper"
    assert mem.view(nid)["key"] == "sym:pkg/a.py::helper"
    # Files carry their content hash.
    fa = mem.nodes[mem.node_by_key("file:pkg/a.py")]["attrs"]
    assert len(fa["sha256"]) == 64


def test_rust_duplicate_qualnames_get_suffix(repo) -> None:
    root, dump = repo
    mem = DevMemory(dump, root / "j.jsonl")
    a = mem.node_by_key("sym:rs/lib.rs::run")
    b = mem.node_by_key("sym:rs/lib.rs::run#2")
    assert a is not None and b is not None
    assert mem.nodes[a]["attrs"]["start_line"] == 2
    assert mem.nodes[b]["attrs"]["start_line"] == 3
    assert mem.nodes[b]["attrs"]["qualname"] == "run"
    assert mem.node_by_key("sym:rs/lib.rs::run#3") is None


def test_version1_snapshot_loads(repo, tmp_path: Path) -> None:
    root, dump = repo
    g = json.loads(dump.read_text())
    for k in ("version", "files", "build", "adapter"):
        g.pop(k)
    for n in g["nodes"]:
        n.pop("key")
    v1 = tmp_path / "v1.json"
    v1.write_text(json.dumps(g))
    mem = DevMemory(v1, tmp_path / "j.jsonl")
    assert mem.version == 1
    for key, typ in (("file:pkg/a.py", "File"),
                     ("sym:pkg/a.py::Widget.size", "Symbol"),
                     ("test:tests/test_a.py::test_one", "Test"),
                     ("doc:README.md", "Doc"), ("repo:", "Repo")):
        nid = mem.node_by_key(key)
        assert nid is not None and mem.nodes[nid]["type"] == typ, key
    fail = next(n for n in mem.nodes.values() if n["type"] == "Failure")
    assert mem.key_of(fail["id"]) is None
    assert mem.view(fail["id"])["key"] is None
    with pytest.raises(ValueError, match="version 1"):
        mem.reingest(["pkg/a.py"])


# --------------------------------------------------------------------------- #
# reingest scenarios
# --------------------------------------------------------------------------- #
def _mem(dump: Path, root: Path) -> DevMemory:
    return DevMemory(dump, root.parent / "j.jsonl", root=root)


def test_modify_function_body(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    sid = mem.node_by_key("sym:pkg/a.py::helper")
    p = root / "pkg" / "a.py"
    p.write_text(p.read_text().replace("return n * 2", "x = n\n    return x * 2"))
    delta = mem.reingest(["pkg/a.py"])
    assert isinstance(delta, GraphDelta)
    assert delta.files == ["pkg/a.py"]
    assert not delta.added and not delta.removed
    assert "sym:pkg/a.py::helper" in delta.changed     # end_line moved
    assert "file:pkg/a.py" in delta.changed            # size and sha256
    assert "sym:pkg/a.py::Widget" not in delta.changed
    assert not delta.edges_added and not delta.edges_removed
    assert mem.node_by_key("sym:pkg/a.py::helper") == sid   # id kept
    assert mem.nodes[sid]["attrs"]["end_line"] == 17
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_rename_function(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    p = root / "pkg" / "a.py"
    p.write_text(p.read_text().replace("def helper(n)", "def assist(n)"))
    before_next = mem._next_id
    delta = mem.reingest(["pkg/a.py"])
    assert delta.removed == ["sym:pkg/a.py::helper"]
    assert delta.added == ["sym:pkg/a.py::assist"]
    assert ("file:pkg/a.py", "defines", "sym:pkg/a.py::assist") in delta.edges_added
    assert ("file:pkg/a.py", "defines", "sym:pkg/a.py::helper") in delta.edges_removed
    new = mem.node_by_key("sym:pkg/a.py::assist")
    assert new >= before_next
    assert mem.search("assist")[0]["key"] == "sym:pkg/a.py::assist"
    assert not [h for h in mem.search("helper", types=["Symbol"])
                if h["key"] == "sym:pkg/a.py::helper"]
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_new_file_resolves_existing_import(repo) -> None:
    """pkg/a.py imports pkg.future, which does not exist yet. Creating it
    adds the importer's edge without parsing the importer."""
    root, dump = repo
    mem = _mem(dump, root)
    assert mem.node_by_key("file:pkg/future.py") is None
    _write(root / "pkg" / "future.py", "def later():\n    return 1\n")
    parsed: list[str] = []
    orig = M._python_parse

    def spy(path, src=None):
        parsed.append(Path(path).name)
        return orig(path, src)

    M._python_parse = spy
    try:
        delta = mem.reingest(["pkg/future.py"])
    finally:
        M._python_parse = orig
    assert parsed == ["future.py"]
    assert ("file:pkg/a.py", "imports", "file:pkg/future.py") in delta.edges_added
    assert "file:pkg/future.py" in delta.added
    assert "sym:pkg/future.py::later" in delta.added
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_new_file_shadows_import_root(repo) -> None:
    """`from lib.x import x` resolves to src/lib/x.py until lib/x.py exists at the
    root, which comes first in the search order."""
    root, dump = repo
    mem = _mem(dump, root)
    _write(root / "lib" / "x.py", "def x():\n    return 1\n")
    delta = mem.reingest(["lib/x.py"])
    assert ("file:pkg/a.py", "imports", "file:lib/x.py") in delta.edges_added
    assert ("file:pkg/a.py", "imports", "file:src/lib/x.py") in delta.edges_removed
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_delete_imported_tested_file(repo) -> None:
    """pkg/a.py is imported, tested and documented, and in git history.
    Deleting it removes its symbols and every in-edge; its File node stays,
    unindexed, because the history still names it."""
    root, dump = repo
    mem = _mem(dump, root)
    (root / "pkg" / "a.py").unlink()
    delta = mem.reingest(["pkg/a.py"])
    assert "sym:pkg/a.py::helper" in delta.removed
    assert "file:pkg/a.py" in delta.changed
    fid = mem.node_by_key("file:pkg/a.py")
    attrs = mem.nodes[fid]["attrs"]
    assert attrs["indexed"] == 0 and "size" not in attrs
    in_types = {e["type"] for e in mem._in[fid]}
    assert in_types == {"contains", "changes"}
    assert ("test:tests/test_a.py::test_one", "tests_module",
            "file:pkg/a.py") in delta.edges_removed
    assert ("doc:docs/guide.md", "documents", "file:pkg/a.py") in delta.edges_removed
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_delete_untracked_file_removes_file_node(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    _write(root / "pkg" / "c.py", "def c():\n    return 3\n")
    mem.reingest(["pkg/c.py"])
    assert mem.node_by_key("file:pkg/c.py") is not None
    (root / "pkg" / "c.py").unlink()
    delta = mem.reingest(["pkg/c.py"])
    assert mem.node_by_key("file:pkg/c.py") is None
    assert {"file:pkg/c.py", "sym:pkg/c.py::c"} <= set(delta.removed)
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_edit_test_file_relinks_failure(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    p = root / "tests" / "test_a.py"
    p.write_text(p.read_text().replace("def test_one():", "def test_zero():\n"
                                       "    pass\n\n\ndef test_one():")
                 + "\n\ndef test_three():\n    \"\"\"see docs/guide.md\"\"\"\n")
    delta = mem.reingest(["tests/test_a.py"])
    assert "test:tests/test_a.py::test_zero" in delta.added
    assert "test:tests/test_a.py::test_three" in delta.added
    assert ("test:tests/test_a.py::test_three", "mentions_file",
            "file:docs/guide.md") in delta.edges_added
    assert ("test:tests/test_a.py::test_three", "tests_module",
            "file:pkg/a.py") in delta.edges_added
    fail = mem.node_by_key("failure:tests/test_a.py::test_one")
    assert [mem.key_of(e["tgt"]) for e in mem._out[fail]
            if e["type"] == "refers_to"] == ["test:tests/test_a.py::test_one"]
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_edit_doc(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    _write(root / "docs" / "guide.md", "# Guide v2\n\nNow `pkg/b.py` only.\n")
    delta = mem.reingest(["docs/guide.md"])
    assert delta.changed == ["doc:docs/guide.md", "file:docs/guide.md"]
    assert delta.edges_added == [("doc:docs/guide.md", "documents", "file:pkg/b.py")]
    assert ("doc:docs/guide.md", "documents", "file:pkg/a.py") in delta.edges_removed
    assert mem.nodes[mem.node_by_key("doc:docs/guide.md")]["attrs"]["title"] == "Guide v2"
    hits = mem._anchor_index.get("guide", [])
    assert hits
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_unchanged_reingest_is_empty(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    delta = mem.reingest(["pkg/a.py", "tests/test_a.py", "README.md",
                          "notes.txt", "rs/lib.rs"])
    assert delta.empty, delta.to_dict()


def test_reingest_persists_and_is_not_stale(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    assert mem.staleness()["stale"] is False
    p = root / "pkg" / "b.py"
    p.write_text(p.read_text() + "\n\ndef gamma():\n    return 3\n")
    mem._staleness_memo = None
    assert mem.staleness()["stale"] is True
    assert mem.pending_changes() == ["pkg/b.py"]
    mem.reingest(["pkg/b.py"])
    assert mem.staleness()["stale"] is False
    assert mem.pending_changes() == []
    again = _mem(dump, root)
    assert again.node_by_key("sym:pkg/b.py::gamma") is not None
    assert again.staleness()["stale"] is False
    assert again.nodes[again.node_by_key("repo:")]["attrs"]["symbols"] == \
        mem.nodes[mem.node_by_key("repo:")]["attrs"]["symbols"]
    assert _canon(again.snapshot_dict()) == _canon(mem.snapshot_dict())



def test_reingest_does_not_certify_failed_second_enumeration(repo, monkeypatch) -> None:
    import ozagia.core.reingest as reingest_module

    root, dump = repo
    mem = _mem(dump, root)
    monkeypatch.setattr(reingest_module, "pending_changes", lambda *args, **kwargs: [])
    monkeypatch.setattr(reingest_module, "unhandled_changes", lambda *args, **kwargs: [])
    monkeypatch.setattr(M, "_tree_digest", lambda *args, **kwargs: ("digest", None))
    monkeypatch.setattr(M, "_build_untracked_paths", lambda *args, **kwargs: None)
    mem.reingest(["pkg/a.py"])
    attrs = mem.nodes[mem.node_by_key("repo:")]["attrs"]
    assert "tree_digest" not in attrs
    assert "cannot enumerate" in attrs["tree_digest_error"]

def test_partial_reingest_stays_stale(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    for rel in ("pkg/a.py", "pkg/b.py"):
        p = root / rel
        p.write_text(p.read_text() + "\n# touched\n")
    mem.reingest(["pkg/a.py"])
    assert mem.staleness()["stale"] is True
    assert mem.pending_changes() == ["pkg/b.py"]


def test_refresh_finds_changed_new_and_deleted(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    p = root / "pkg" / "b.py"
    p.write_text(p.read_text().replace("return 2", "return 22"))
    _write(root / "pkg" / "new.py", "def fresh():\n    pass\n")
    (root / "docs" / "guide.md").unlink()
    _write(root / "other.txt", "ignored\n")
    assert sorted(mem.pending_changes()) == ["docs/guide.md", "pkg/b.py",
                                             "pkg/new.py"]
    changed = mem.changed()
    assert changed["refresh"]["needed"] is True
    delta = mem.refresh()
    assert sorted(delta.files) == ["docs/guide.md", "pkg/b.py", "pkg/new.py"]
    assert mem.pending_changes() == []
    assert mem.changed()["refresh"]["needed"] is False
    # The digest includes untracked files in the source roots, even when the
    # built-in graph has no role for their extension.
    assert mem.staleness()["stale"] is True
    (root / "other.txt").unlink()
    mem.refresh()
    assert mem.staleness()["stale"] is False
    _assert_equivalent(mem, root, root.parent / "fresh.json")
    assert mem.refresh().empty


def test_edit_returns_graph_delta(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    res = mem.edit({"file": "pkg/b.py", "apply": True, "ops": [
        {"op": "replace_body", "symbol": "beta", "text": "return 3"}]})
    assert res.get("applied"), res
    gd = res["graph_delta"]
    assert gd["files"] == ["pkg/b.py"]
    assert "file:pkg/b.py" in gd["changed"]
    assert "error" not in gd
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_mcp_dev_refresh(repo) -> None:
    root, dump = repo
    server = DevMemoryServer(_mem(dump, root))
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "dev_refresh" in names
    p = root / "pkg" / "b.py"
    p.write_text(p.read_text() + "\n\ndef gamma():\n    return 3\n")

    def call(name, args):
        msg = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": name, "arguments": args}})
        return json.loads(msg["result"]["content"][0]["text"])

    assert call("dev_changed", {})["refresh"]["needed"] is True
    out = call("dev_refresh", {})
    assert out["ok"] is True
    assert "sym:pkg/b.py::gamma" in out["delta"]["added"]
    assert call("dev_changed", {})["refresh"]["needed"] is False
    got = call("dev_file", {"path": "pkg/b.py"})
    assert "gamma" in json.dumps(got)


# --------------------------------------------------------------------------- #
# equivalence property
# --------------------------------------------------------------------------- #
_POOL_PY = ["pkg/a.py", "pkg/b.py", "pkg/future.py", "pkg/c.py", "lib/x.py",
            "src/lib/x.py", "pkg/sub/__init__.py", "pkg/sub/d.py"]
_POOL_TEST = ["tests/test_a.py", "tests/test_b.py", "tests/test_new.py"]
_POOL_DOC = ["README.md", "docs/guide.md", "docs/more/notes.md"]
_POOL_RS = ["rs/lib.rs", "rs/util.rs", "rs/extra.rs"]
_MODULES = ["pkg.a", "pkg.b", "pkg.future", "pkg.c", "lib.x", "pkg.sub",
            "pkg.sub.d", "pkg.missing"]
_MENTIONS = ["pkg/a.py", "pkg/b.py", "pkg/c.py", "tests/test_a.py",
             "docs/guide.md", "rs/util.rs", "pkg/sub/d.py", "src/lib/x.py",
             "pkg/data.json", "pkg/late.json"]


def _py_text(rng: random.Random, n_funcs: int) -> str:
    lines = [f'"""Module {rng.randint(0, 9)}."""']
    for m in rng.sample(_MODULES, rng.randint(0, 3)):
        lines.append(f"from {m} import thing")
    if rng.random() < 0.3:
        lines.append("from . import a")
    names = rng.sample(["alpha", "beta", "gamma", "delta", "eps", "zeta"],
                       n_funcs)
    for name in names:
        lines += ["", "", f"def {name}(x):", f'    """{name} doc."""',
                  *["    x += 1"] * rng.randint(0, 3), "    return x"]
    if rng.random() < 0.5:
        lines += ["", "", "class Box:", "    def open(self):", "        pass"]
    return "\n".join(lines) + "\n"


def _test_text(rng: random.Random) -> str:
    lines = []
    if rng.random() < 0.5:
        lines.append(f'"""Mentions {rng.choice(_MENTIONS)}."""')
    for m in rng.sample(_MODULES, rng.randint(0, 2)):
        lines.append(f"from {m} import thing")
    for name in rng.sample(["test_one", "test_two", "test_x", "test_y"],
                           rng.randint(0, 3)):
        lines += ["", "", f"def {name}():"]
        if rng.random() < 0.4:
            lines.append(f"    # {rng.choice(_MENTIONS)}")
        lines.append("    assert True")
    if rng.random() < 0.4:
        lines += ["", "", "class TestGroup:", "    def test_two(self):",
                  "        assert True"]
    return "\n".join(lines) + "\n"


def _doc_text(rng: random.Random) -> str:
    refs = " and ".join(f"`{m}`" for m in rng.sample(_MENTIONS, rng.randint(0, 3)))
    return f"# Title {rng.randint(0, 99)}\n\nSee {refs}.\n"


def _rs_text(rng: random.Random) -> str:
    lines = []
    for mod in rng.sample(["util", "extra", "gone"], rng.randint(0, 2)):
        lines.append(f"mod {mod};")
    for _ in range(rng.randint(0, 4)):
        lines.append(f"pub fn {rng.choice(['run', 'stop', 'go'])}() {{}}")
    return "\n".join(lines) + "\n"


def _random_edit(rng: random.Random, root: Path) -> list[str]:
    kind = rng.choice(["py", "py", "test", "doc", "rs", "delete", "rename"])
    if kind == "delete":
        existing = [r for r in _POOL_PY + _POOL_TEST + _POOL_DOC + _POOL_RS
                    if (root / r).exists()]
        rel = rng.choice(existing)
        (root / rel).unlink()
        return [rel]
    if kind == "rename":
        existing = [r for r in _POOL_PY if (root / r).exists()]
        free = [r for r in _POOL_PY if not (root / r).exists()]
        if not existing or not free:
            return []
        src, dst = rng.choice(existing), rng.choice(free)
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        (root / src).rename(root / dst)
        return [src, dst]
    rel, text = {
        "py": lambda: (rng.choice(_POOL_PY), _py_text(rng, rng.randint(0, 3))),
        "test": lambda: (rng.choice(_POOL_TEST), _test_text(rng)),
        "doc": lambda: (rng.choice(_POOL_DOC), _doc_text(rng)),
        "rs": lambda: (rng.choice(_POOL_RS), _rs_text(rng)),
    }[kind]()
    _write(root / rel, text)
    return [rel]


@pytest.mark.parametrize("seed", list(range(5)))
def test_reingest_equals_fresh_build(repo, seed: int) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    rng = random.Random(seed)
    fresh = root.parent / "fresh.json"
    steps = 0
    kinds: Counter = Counter()
    while steps < 35:
        rels = _random_edit(rng, root)
        if not rels:
            continue
        steps += 1
        delta = mem.reingest(rels, write=False)
        kinds.update(t for _s, t, _g in delta.edges_added + delta.edges_removed)
        _assert_equivalent(mem, root, fresh)
    # The edits moved every kind of per-file edge.
    assert {"imports", "tests_module", "mentions_file", "documents",
            "defines", "has_test", "contains_symbol", "refers_to"} <= set(kinds)
    # The snapshot written at the end loads to the same graph.
    mem.reingest([])
    mem.write_snapshot()
    assert _canon(_mem(dump, root).snapshot_dict()) == _canon(mem.snapshot_dict())
    # Every key still names one node and owned keys name existing files.
    keys = [n["key"] for n in mem.nodes.values()]
    assert len(keys) == len(set(keys))
    for k in keys:
        if k.startswith(("sym:", "test:", "doc:")):
            assert (root / key_file(k)).is_file(), k


# --------------------------------------------------------------------------- #
# adapters
# --------------------------------------------------------------------------- #
@pytest.fixture
def seed_repo(tmp_path: Path, layout, seed_adapter) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    _fixture(root, git=False)
    _write(root / "seeds" / "base.json", json.dumps({
        "id": "base", "version": "1", "nodes": [], "depends_on": []}))
    _write(root / "seeds" / "demo.json", json.dumps({
        "id": "demo", "version": "1", "depends_on": ["base"],
        "nodes": [{"local_id": 1, "type": "Concept",
                   "attrs": {"name": "widget"}}],
        "rules": [{"name": "demo_rule"}]}))
    p = root / "tests" / "test_a.py"
    p.write_text(p.read_text() + "\n\ndef test_seed():\n"
                 "    # loads seeds/demo.json\n    assert True\n")
    dump = tmp_path / "state" / "graph.json"
    _build(root, dump)
    return root, dump


def test_adapter_edges_kept_for_surviving_nodes(seed_repo) -> None:
    root, dump = seed_repo
    mem = _mem(dump, root)
    assert ("test:tests/test_a.py::test_seed", "mentions_seed", "seed:demo") in {
        (mem.key_of(e["src"]), e["type"], mem.key_of(e["tgt"]))
        for e in mem.edges}
    p = root / "tests" / "test_a.py"
    p.write_text(p.read_text().replace("assert helper(1) == 2",
                                       "assert helper(2) == 4"))
    delta = mem.reingest(["tests/test_a.py"])
    assert mem.last_reingest_timings["adapter_pass"] == 0.0
    assert not delta.edges_removed
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_seed_edit_runs_adapter_pass(seed_repo) -> None:
    root, dump = seed_repo
    mem = _mem(dump, root)
    seed_id = mem.node_by_key("seed:demo")
    concept = mem.node_by_key("seed_item:demo::Concept::widget")
    assert concept is not None
    _write(root / "seeds" / "demo.json", json.dumps({
        "id": "demo", "version": "2", "depends_on": ["base"],
        "nodes": [{"local_id": 1, "type": "Concept",
                   "attrs": {"name": "widget"}},
                  {"local_id": 2, "type": "Concept",
                   "attrs": {"name": "gadget"}}],
        "rules": []}))
    delta = mem.reingest(["seeds/demo.json"])
    assert mem.last_reingest_timings["adapter_pass"] == 1.0
    assert "seed_item:demo::Concept::gadget" in delta.added
    assert "seed_item:demo::Rule::demo_rule" in delta.removed
    assert "seed:demo" in delta.changed
    # Nodes whose keys survive the adapter pass keep their ids.
    assert mem.node_by_key("seed:demo") == seed_id
    assert mem.node_by_key("seed_item:demo::Concept::widget") == concept
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_deleted_file_loses_adapter_edges(seed_repo) -> None:
    root, dump = seed_repo
    mem = _mem(dump, root)
    (root / "tests" / "test_a.py").unlink()
    delta = mem.reingest(["tests/test_a.py"])
    assert ("file:tests/test_a.py", "mentions_seed", "seed:demo") in delta.edges_removed
    # The failure baseline still names the file.
    assert mem.nodes[mem.node_by_key("file:tests/test_a.py")]["attrs"]["indexed"] == 0
    _assert_equivalent(mem, root, root.parent / "fresh.json")


# --------------------------------------------------------------------------- #
# review fixes 2 and 3: adapter-owned files and the tree digest
# --------------------------------------------------------------------------- #
def test_changed_tracked_unindexed_file_keeps_the_snapshot_stale(repo) -> None:
    root, dump = repo
    mem = _mem(dump, root)
    assert mem.staleness()["stale"] is False and mem.unhandled_changes() == []
    (root / "notes.txt").write_text("changed, still not indexed\n")
    p = root / "pkg" / "b.py"
    p.write_text(p.read_text() + "\n# touched\n")
    assert mem.pending_changes() == ["pkg/b.py"]
    mem.reingest(["pkg/b.py"])
    assert mem.pending_changes() == []
    assert mem.unhandled_changes() == ["notes.txt"]
    assert mem.staleness()["stale"] is True
    # The snapshot on disk keeps reporting it, too.
    assert _mem(dump, root).staleness()["stale"] is True
    # Reverting the file clears it.
    _git(root, "checkout", "--", "notes.txt")
    mem.reingest(["pkg/b.py"])
    assert mem.unhandled_changes() == []
    assert mem.staleness()["stale"] is False


def test_unindexed_file_dirty_at_build_does_not_block_the_digest(repo, tmp_path) -> None:
    root, _ = repo
    (root / "notes.txt").write_text("dirty before the build\n")
    dump = tmp_path / "state2" / "graph.json"
    _build(root, dump)
    mem = _mem(dump, root)
    assert mem.unhandled_changes() == []
    p = root / "pkg" / "b.py"
    p.write_text(p.read_text() + "\n# touched\n")
    mem.reingest(["pkg/b.py"])
    assert mem.staleness()["stale"] is False


@pytest.fixture
def git_seed_repo(tmp_path: Path, layout, seed_adapter) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    _fixture(root, git=False)
    _write(root / "seeds" / "base.json", json.dumps({
        "id": "base", "version": "1", "nodes": [], "depends_on": []}))
    _write(root / "seeds" / "demo.json", json.dumps({
        "id": "demo", "version": "1", "depends_on": ["base"],
        "nodes": [{"local_id": 1, "type": "Concept", "attrs": {"name": "widget"}}],
        "rules": []}))
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    dump = tmp_path / "state" / "graph.json"
    _build(root, dump)
    return root, dump


def test_out_of_band_seed_edit_is_pending_and_refresh_runs_the_adapter_pass(
        git_seed_repo) -> None:
    root, dump = git_seed_repo
    mem = _mem(dump, root)
    assert "seeds/demo.json" in mem._adapter_info["files"]
    assert "seeds/demo.json" in mem._adapter_info["stamps"]
    assert mem.pending_changes() == []
    _write(root / "seeds" / "demo.json", json.dumps({
        "id": "demo", "version": "2", "depends_on": ["base"],
        "nodes": [{"local_id": 1, "type": "Concept", "attrs": {"name": "widget"}},
                  {"local_id": 2, "type": "Concept", "attrs": {"name": "gadget"}}],
        "rules": []}))
    assert mem.pending_changes() == ["seeds/demo.json"]
    delta = mem.refresh()
    assert mem.last_reingest_timings["adapter_pass"] == 1.0
    assert "seed_item:demo::Concept::gadget" in delta.added
    assert mem.pending_changes() == []
    assert mem.staleness()["stale"] is False
    # Same content, new mtime: not pending (the stamp is refreshed).
    p = root / "seeds" / "demo.json"
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 10**9))
    assert mem.pending_changes() == []
    _assert_equivalent(mem, root, root.parent / "fresh.json")


def test_snapshot_without_adapter_stamps_reports_git_listed_adapter_files(
        git_seed_repo) -> None:
    root, dump = git_seed_repo
    mem = _mem(dump, root)
    mem._adapter_info.pop("stamps")  # a snapshot built before stamps
    (root / "seeds" / "base.json").write_text(json.dumps({
        "id": "base", "version": "2", "nodes": [], "depends_on": []}))
    assert mem.pending_changes() == ["seeds/base.json"]


def test_graph_delta_merge_cancels_opposite_entries() -> None:
    a = GraphDelta(files=["x"], added=["k1", "k3"], removed=["k2"], changed=["k4"],
                   edges_added=[("a", "t", "b")])
    b = GraphDelta(files=["y"], added=["k2"], removed=["k1", "k4"], changed=["k3"],
                   edges_removed=[("a", "t", "b")])
    m = a.merge(b)
    assert m.added == ["k3"]                  # k1 added then removed: gone
    assert m.removed == ["k4"]                # k2 removed then added: gone
    assert m.changed == []                    # k4 changed+removed -> removed;
    assert m.edges_added == [] and m.edges_removed == []   # k3 stays added
    assert m.files == ["x", "y"]
