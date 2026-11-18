"""Structured data queries: JSON pointers into JSON/YAML/TOML files, the
read form `json:<rel>#<pointer>`, outlines of large files and dev_query."""
from __future__ import annotations

import builtins
import json
from pathlib import Path

import pytest

from ozagia import intent as I
from ozagia.config import clear_config_cache
from ozagia.core import structured as S
from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.mcp.server import DevMemoryServer

DATA = {
    "name": "run-7",
    "score": 0.875,
    "ratio": 1.50,
    "runs": [{"id": i, "ok": i % 2 == 0} for i in range(30)],
    "a/b": {"t~x": 1},
    "nested": {"deep": {"leaf": "v"}},
    "empty": [],
}
# 1.50 is written as such: the text below keeps it.
DATA_TEXT = json.dumps(DATA, indent=2).replace('"ratio": 1.5', '"ratio": 1.50')

TOML = """[project]
name = "demo"
dependencies = ["a", "b"]

[tool.x]
n = 3
when = 1979-05-27
"""

YAML = """a: 1
items:
  - x
  - y
---
b: 2
"""


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    clear_config_cache()
    root = tmp_path / "repo"
    _write(root / "data" / "run.json", DATA_TEXT)
    _write(root / "pyproject.toml", TOML)
    _write(root / "deploy.yaml", YAML)
    _write(root / "bad.json", '{"a": 1,\n  "b": }\n')
    _write(root / "pkg" / "__init__.py", "")
    big = {"rows": [{"id": i, "v": "x" * 10} for i in range(800)],
           "meta": {"n": 800, "tags": ["a", "b"]}, "title": "big"}
    _write(root / "big.json", json.dumps(big, indent=2))
    _write(root / "big.yaml", "\n".join(f"k{i}: {i}" for i in range(2500)))
    _write(root / "small.json", json.dumps({"a": 1}, indent=2))
    yield root
    clear_config_cache()


@pytest.fixture
def mem(repo: Path, tmp_path: Path) -> DevMemory:
    dump = tmp_path / "state" / "graph.json"
    build_index(repo, dump, include_docs="none", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=repo)


def _doc(text: str, fmt: str = "json") -> S.Doc:
    return S.load_text(fmt, "f." + fmt, text)


# --------------------------------------------------------------------------- #
# pointers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("pointer,tokens", [
    ("", []), ("#", []), (None, []), ("/", [""]), ("/a", ["a"]),
    ("#/a/0", ["a", "0"]), ("/a~1b", ["a/b"]), ("/t~0x", ["t~x"]),
    ("/~01", ["~1"]), ("/a b", ["a b"]),
])
def test_parse_pointer(pointer, tokens):
    assert S.parse_pointer(pointer) == tokens
    if pointer not in (None, "#"):
        assert S.format_pointer(tokens) == pointer.lstrip("#")


@pytest.mark.parametrize("pointer", ["a", "#a", "/a~2", "/a~"])
def test_bad_pointer_syntax(pointer):
    with pytest.raises(S.QueryError):
        S.parse_pointer(pointer)


def test_resolve_escapes_and_indices():
    doc = _doc(DATA_TEXT)
    assert S.resolve(doc, ["a/b", "t~x"]).value == S.Num("1")
    assert S.resolve(doc, ["runs", "3", "id"]).value == S.Num("3")
    assert S.resolve(doc, ["nested", "deep", "leaf"]).value == "v"


def test_resolve_errors_suggest_nearest_keys():
    doc = _doc(DATA_TEXT)
    with pytest.raises(S.QueryError, match=r"no key 'nmae'.*nearest: name"):
        S.resolve(doc, ["nmae"])
    with pytest.raises(S.QueryError, match=r"index 30 is out of range.*0-29"):
        S.resolve(doc, ["runs", "30"])
    with pytest.raises(S.QueryError, match="not an array index"):
        S.resolve(doc, ["runs", "01"])
    with pytest.raises(S.QueryError, match="not an array index"):
        S.resolve(doc, ["runs", "-"])
    with pytest.raises(S.QueryError, match="it is a string"):
        S.resolve(doc, ["name", "x"])
    with pytest.raises(S.QueryError, match="the object is empty"):
        S.resolve(_doc("{}"), ["x"])


def test_keys_parse():
    assert S.parse_key("json:a/b.json#/x") == ("json", "a/b.json", "/x")
    assert S.parse_key("yaml:a.yaml") == ("yaml", "a.yaml", "")
    assert S.parse_key("toml:p.toml#") == ("toml", "p.toml", "")
    assert S.parse_key("file:a.json") is None
    assert S.parse_key("json:") is None


# --------------------------------------------------------------------------- #
# query: objects, arrays, scalars, slices, truncation
# --------------------------------------------------------------------------- #
def test_query_object_lists_keys_with_type_size_and_line():
    res = S.query(_doc(DATA_TEXT), "")
    assert res["type"] == "object" and res["total"] == 7
    assert res["lines"][0] == '"name": string, 5 chars  "run-7"  (line 2)'
    assert res["lines"][2] == '"ratio": 1.50  (line 4)'
    assert res["lines"][3].startswith('"runs": array, 30 items  [first: ')
    assert res["key"] == "json:f.json#"


def test_query_array_default_slice_and_offset_limit():
    doc = _doc(DATA_TEXT)
    res = S.query(doc, "/runs")
    assert res["shown"] == (0, 19) and res["next"] == 20
    assert res["lines"][0].startswith('[0] {"id":0,"ok":true}  (line ')
    res = S.query(doc, "/runs", offset=25, limit=3)
    assert res["shown"] == (25, 27) and res["next"] == 28
    assert [ln.split(" ")[0] for ln in res["lines"]] == ["[25]", "[26]", "[27]"]
    res = S.query(doc, "/runs", offset=28, limit=10)
    assert res["shown"] == (28, 29) and res["next"] is None
    assert S.query(doc, "/runs", offset=40)["shown"] is None
    with pytest.raises(S.QueryError):
        S.query(doc, "/runs", limit=0)


def test_query_scalar_and_line():
    doc = _doc(DATA_TEXT)
    res = S.query(doc, "/score")
    assert res["lines"] == ["0.875"] and res["type"] == "number"
    assert res["line"] == 3
    assert S.query(doc, "/runs/1/ok")["lines"] == ["false"]


def test_query_truncates_at_cap():
    doc = _doc(json.dumps({"k%d" % i: "v" * 300 for i in range(200)}))
    res = S.query(doc, "", limit=200)
    assert res["truncated"] is True
    assert sum(len(x.encode()) + 1 for x in res["lines"]) <= S.OUTPUT_CAP
    assert res["next"] == len(res["lines"])
    long = _doc(json.dumps({"s": "x" * 50_000}))
    res = S.query(long, "/s")
    assert res["truncated"] is True and len(res["lines"][0]) < S.OUTPUT_CAP


def test_parse_error_has_line_and_column():
    with pytest.raises(S.QueryError, match="line 2 column 8"):
        _doc('{"a": 1,\n  "b": }\n')


def test_toml_query():
    doc = _doc(TOML, "toml")
    res = S.query(doc, "/project/dependencies")
    assert res["lines"] == ['[0] "a"', '[1] "b"']
    assert S.query(doc, "/tool/x/when")["lines"] == ['"1979-05-27"']
    assert S.query(doc, "/tool/x/when")["type"] == "date"
    with pytest.raises(S.QueryError, match="invalid TOML"):
        _doc("a = = 1", "toml")


def test_yaml_query_and_documents():
    pytest.importorskip("yaml")
    doc = _doc(YAML, "yaml")
    assert "2 documents" in doc.note
    assert S.query(doc, "/0/items/1")["lines"] == ['"y"']
    single = _doc("a:\n  b: [1, 2]\n", "yaml")
    assert S.query(single, "/a/b")["lines"] == ["[0] 1", "[1] 2"]
    with pytest.raises(S.QueryError, match="invalid YAML at line"):
        _doc("a: [1,\n", "yaml")


def test_yaml_missing_is_reported(monkeypatch, repo, mem):
    real = builtins.__import__

    def no_yaml(name, *a, **k):
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("no yaml")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_yaml)
    assert S.yaml_available() is False
    with pytest.raises(S.QueryError, match="iac-yaml"):
        S.load_text("yaml", "x.yaml", "a: 1")
    res = I.handle(mem, "read", {"path": "yaml:deploy.yaml#/a"})
    assert res["handled"] and res["is_error"]
    assert "iac-yaml" in res["output"]
    # A plain read of a large YAML file without PyYAML is the text.
    res = I.handle(mem, "read", {"path": "big.yaml"})
    assert res["meta"]["intent"] == "read_file"


def test_outline_depth_two():
    doc = _doc(DATA_TEXT)
    lines = S.outline(doc)
    assert lines[0] == "root: object, 7 keys"
    assert any(ln.startswith('- "nested": object, 1 key') for ln in lines)
    deep = next(ln for ln in lines if ln.startswith('    "deep"'))
    assert deep.startswith('    "deep": object, 1 key  {"leaf":"v"}  (line ')
    assert any(ln.startswith('    [0] {"id":0,"ok":true}') for ln in lines)


def test_num_keeps_literal_text():
    assert S.dump(S.load_json('{"a":[1.50,-0,1e3]}')) == '{"a":[1.50,-0,1e3]}'


# --------------------------------------------------------------------------- #
# the read form and outlines through the intent layer
# --------------------------------------------------------------------------- #
def test_read_form_json(mem, repo):
    res = I.handle(mem, "read", {"path": "json:data/run.json#/runs",
                                 "offset": 3, "limit": 2})
    assert res["handled"] and not res["is_error"], res
    out = res["output"].splitlines()
    assert out[0] == (f"Read json {repo / 'data/run.json'}#/runs: "
                      f"array, 30 items, line 5")
    assert out[1] == "[ozagia] json:data/run.json#/runs"
    assert out[2].startswith('[2] {"id":2,"ok":true}')
    assert out[3].startswith('[3] {"id":3,"ok":false}')
    assert out[4] == ("[ozagia] items 2-3 of 30 (0-based index); offset 5 "
                      "shows the next ones")
    assert res["meta"]["intent"] == "read_structured"
    assert res["meta"]["keys"][0] == "json:data/run.json#/runs"


def test_read_form_root_toml_and_errors(mem):
    out = I.handle(mem, "read", {"path": "toml:pyproject.toml#"})["output"]
    assert out.splitlines()[2].startswith('"project": object, 2 keys')
    out = I.handle(mem, "read", {"path": "json:data/run.json"})["output"]
    assert '"runs": array, 30 items' in out
    res = I.handle(mem, "read", {"path": "json:data/run.json#/nmae"})
    assert res["is_error"] and "nearest: name" in res["output"]
    res = I.handle(mem, "read", {"path": "json:missing.json#/a"})
    assert res["is_error"] and "No such file: missing.json" in res["output"]
    res = I.handle(mem, "read", {"path": "json:bad.json#"})
    assert res["is_error"] and "line 2 column 8" in res["output"]
    res = I.handle(mem, "read", {"path": "json:../x.json#"})
    assert res["is_error"] and "outside the project root" in res["output"]


def test_outline_for_large_structured_files(mem, repo):
    res = I.handle(mem, "read", {"path": "big.json"})
    assert res["meta"]["intent"] == "read_structured_outline"
    out = res["output"].splitlines()
    assert out[0].startswith(f"Read file {repo / 'big.json'}, outline of ")
    assert "outline only" in out[1] and "offset=<line>" in out[1]
    assert "[ozagia] json:big.json#" in out
    assert any(ln.startswith('- "rows": array, 800 items') for ln in out)
    assert any(ln.startswith('    "tags": array, 2 items') for ln in out)
    note = [ln for ln in out if ln.startswith("[ozagia] an outline instead")]
    assert len(note) == 1 and "json:big.json#/<key>" in note[0]
    assert "Lines 1-200 follow" in note[0] and "comments" not in note[0]
    # The head of the text follows the outline, so a read before an edit
    # still sees it.
    head = out[out.index(note[0]) + 1:]
    text = (repo / "big.json").read_text().splitlines()
    assert head == [f"{i}: {ln}" for i, ln in enumerate(text[:200], 1)]
    # A range reads the text, as before.
    res = I.handle(mem, "read", {"path": "big.json", "offset": 1, "limit": 3})
    assert res["meta"]["intent"] == "read_file"
    assert res["output"].splitlines()[1] == "1: {"
    # A small file is unchanged.
    res = I.handle(mem, "read", {"path": "small.json"})
    assert res["meta"]["intent"] == "read_file"
    # cat of a large file is the text.
    res = I.handle(mem, "shell", {"command": "cat big.json"})
    assert res["meta"]["intent"] == "shell_read"


def test_outline_threshold_is_configurable(mem, repo):
    _write(repo / ".ozagia.toml", "[reads]\nstructured_outline_lines = 2\n"
                                  "structured_outline_bytes = 0\n")
    clear_config_cache()
    res = I.handle(mem, "read", {"path": "small.json"})
    assert res["meta"]["intent"] == "read_structured_outline"
    assert "over 2 lines" in res["output"]
    _write(repo / ".ozagia.toml", "[reads]\nstructured_outline_lines = 0\n"
                                  "structured_outline_bytes = 0\n")
    clear_config_cache()
    res = I.handle(mem, "read", {"path": "big.json"})
    assert res["meta"]["intent"] == "read_file"


def test_outline_head_is_capped(mem, repo):
    # One minified line: the head is that line, cut.
    _write(repo / "min.json", json.dumps({"rows": ["x" * 50] * 3000}))
    res = I.handle(mem, "read", {"path": "min.json"})
    assert res["meta"]["intent"] == "read_structured_outline"
    out = res["output"].splitlines()
    assert "Lines 1-1 follow" in res["output"]
    assert out[-1].startswith('1: {"rows": ["xxx')
    assert "[ozagia] line cut at 2000 of" in out[-1]
    assert len(res["output"]) < 16 * 1024


@pytest.mark.parametrize("name,text,note", [
    ("values.yaml", "# replicas: how many pods\n" + "\n".join(
        f"k{i}: &a{i} {i}" for i in range(2500)) + "\n",
     "omits comments and shows anchors, aliases and merge keys resolved"),
    ("big.toml", "# the project\n" + "\n".join(
        f"k{i} = {i}  # n" for i in range(2500)) + "\n", "omits comments"),
])
def test_outline_says_what_yaml_and_toml_lose(mem, repo, name, text, note):
    if name.endswith(".yaml"):
        pytest.importorskip("yaml")
    _write(repo / name, text)
    res = I.handle(mem, "read", {"path": name})
    assert res["meta"]["intent"] == "read_structured_outline", res
    assert note in res["output"]
    # The comment at the head is in the text that follows the outline.
    assert "\n1: # " in res["output"]


def test_outline_size_ceiling_reads_the_text(mem, repo, monkeypatch):
    from ozagia.intent import read as R
    monkeypatch.setattr(R, "OUTLINE_MAX_BYTES", 10_000)
    res = I.handle(mem, "read", {"path": "big.json"})
    assert res["meta"]["intent"] == "read_file"
    assert res["output"].splitlines()[1] == "1: {"


def test_outline_ceiling_is_lower_for_pure_python_yaml(monkeypatch):
    yaml = pytest.importorskip("yaml")
    from ozagia.intent import read as R
    monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    assert R._outline_ceiling("yaml") == R.OUTLINE_MAX_BYTES_PURE_YAML
    assert R._outline_ceiling("json") == R.OUTLINE_MAX_BYTES == 8 * 1024 * 1024
    assert R.OUTLINE_MAX_BYTES_PURE_YAML == 2 * 1024 * 1024


def test_outline_threshold_config_is_validated(repo):
    from ozagia.config import ConfigError, load_project_config
    _write(repo / ".ozagia.toml", "[reads]\nstructured_outline_lines = -1\n")
    clear_config_cache()
    with pytest.raises(ConfigError, match="non-negative"):
        load_project_config(repo)


def test_outline_skipped_for_invalid_large_file(mem, repo):
    _write(repo / "broken.json", "{\n" + "  1,\n" * 3000)
    res = I.handle(mem, "read", {"path": "broken.json"})
    assert res["meta"]["intent"] == "read_file"


def test_outline_does_not_affect_edits(mem, repo):
    res = I.handle(mem, "edit", {"filePath": "big.json",
                                 "oldString": '"title": "big"',
                                 "newString": '"title": "bigger"'})
    assert res["handled"] and not res["is_error"], res
    assert '"title": "bigger"' in (repo / "big.json").read_text()


# --------------------------------------------------------------------------- #
# MCP dev_query
# --------------------------------------------------------------------------- #
def _mcp(server, args):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "dev_query", "arguments": args}})
    assert "result" in resp, resp
    return json.loads(resp["result"]["content"][0]["text"])


def test_mcp_dev_query(mem):
    server = DevMemoryServer(mem)
    names = [t["name"] for t in server._tools]
    assert "dev_query" in names
    got = _mcp(server, {"path": "data/run.json", "pointer": "/runs",
                        "offset": 5, "limit": 2})
    assert got["ok"] is True
    assert got["key"] == "json:data/run.json#/runs"
    assert got["shown"] == [5, 6] and got["next_offset"] == 7
    assert got["total"] == 30 and got["type"] == "array"
    assert "offset 7 shows the next ones" in got["text"]
    got = _mcp(server, {"path": "json:data/run.json#/score"})
    assert got["ok"] and got["text"].splitlines()[-1] == "0.875"
    got = _mcp(server, {"path": "pyproject.toml", "pointer": "/project/name"})
    assert got["ok"] and got["text"].splitlines()[-1] == '"demo"'
    got = _mcp(server, {"path": "data/run.json", "pointer": "/nmae"})
    assert got["ok"] is False and "nearest: name" in got["error"]
    got = _mcp(server, {"path": "pkg/__init__.py"})
    assert got["ok"] is False and "give format" in got["error"]
    got = _mcp(server, {"path": "json:data/run.json#/a", "pointer": "/b"})
    assert got["ok"] is False
    bad = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "dev_query",
                                    "arguments": {"path": "x.json",
                                                  "format": "xml"}}})
    assert bad["error"]["code"] == -32602


def test_read_form_permission_names_the_file(mem):
    res = I.handle(mem, "read", {"path": "json:data/run.json#/runs"})
    assert res["permission"]["resources"] == ["data/run.json"]


def test_read_errors_carry_the_permission(mem):
    # Without it the plugin would run the built-in read on the literal path.
    for path, resource in (("json:data/run.json#/nmae", "data/run.json"),
                           ("json:bad.json#", "bad.json"),
                           ("json:missing.json#/a", "missing.json"),
                           ("sym:pkg/__init__.py::nope", "sym:pkg/__init__.py::nope")):
        res = I.handle(mem, "read", {"path": path})
        assert res["handled"] and res["is_error"], (path, res)
        assert res["permission"] == {"action": "read", "resources": [resource],
                                     "save": ["*"]}, path


def test_mcp_dev_query_path_with_hash(mem, repo):
    _write(repo / "dir#1" / "a.json", json.dumps({"x": {"y": 2}}))
    server = DevMemoryServer(mem)
    got = _mcp(server, {"path": "dir#1/a.json", "pointer": "/x/y"})
    assert got["ok"] is True, got
    assert got["path"] == "dir#1/a.json" and got["text"].splitlines()[-1] == "2"
    got = _mcp(server, {"path": "dir#1/a.json"})
    assert got["ok"] is True and got["type"] == "object"
