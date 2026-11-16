"""The jq subset (`ozagia.core.jq`) and its shell capture: output must be
jq's own, byte for byte, for every accepted case."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ozagia import intent as I
from ozagia.core import jq as J
from ozagia.core.memory import DevMemory, build_index
from ozagia.intent.shell import classify

JQ = shutil.which("jq")
needs_jq = pytest.mark.skipif(JQ is None, reason="jq is not installed")

FIXTURES = {
    "obj.json": (
        '{"a": {"b": [1, 2.50, {"c": "x\\ty"}], "key with space": null,'
        ' "n": -0, "z": 0.000030},\n'
        ' "list": [{"x": 1, "y": "\\u00e9"}, {"x": 2}, {"y": true}],\n'
        ' "big": 123456789012345678901234567890, "f": 0.30000000000000004,\n'
        ' "s": "hello\\u007f\\u001b\\u2028/\\"q\\" \\\\ \\ud834\\udd1e",\n'
        ' "e": {}, "arr": [], "B": 1, "\\u00e9": 2, "dup": 1, "dup": 3,\n'
        ' "nested": {"deep": [[1, [2, []]], {"k": {"m": [{}]}}]}}\n'),
    "arr.json": json.dumps([{"name": "n%d" % i, "v": i * 1.5, "t": [i]}
                            for i in range(12)], indent=4),
    "scalar.json": '"just a string\\nwith a newline"\n',
    "num.json": "  42  \n",
    "null.json": "null",
}

# Filters accepted by the subset; each is run with every flag set below.
ACCEPTED = [
    ".", ".a", ".a.b", '.a."key with space"', '.a["key with space"]',
    ".a.b[0]", ".a.b[-1]", ".a.b[2].c", ".a.b[:2]", ".a.b[1:]", ".a.b[-2:]",
    ".list[]", ".list[] | .x", ".list[].y", ".list | length", "keys",
    ".a | keys", ".list | keys", ".s | length", ".[] | type", ".big",
    ".f", ".a.z", ".s", ".list[5]", ".nope", ".nope.deeper", ".e", ".arr",
    ".[]", ".nested", ".nested.deep[1].k", ".dup", "type", ".list[0] | keys",
    ".s[1:4]", ".list[1:2] | length", ". | keys | length", "length",
    '."\u00e9"', ".B", ".a.n | length", ".a.b[0] | length",
]
ARR_FILTERS = [".", ".[0]", ".[3:5]", ".[-1].name", ".[] | .name", "length",
               ".[2:4] | .[] | .v", "keys", ".[100]", ".[1:3][]", ".[].t"]
SCALAR_FILTERS = [".", "length", "type"]
NULL_FILTERS = [".", ".a", ".[0]", ".[1:2]", "length", "type"]
FLAGS = [[], ["-c"], ["-r"], ["-S"], ["-rc"], ["-c", "-S"], ["-M"]]


@pytest.fixture
def files(tmp_path: Path) -> Path:
    for name, text in FIXTURES.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    return tmp_path


def _jq(flags: list[str], filt: str, path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([JQ, *flags, filt, str(path)], capture_output=True,
                          text=True, encoding="utf-8")


def _ours(flags: list[str], filt: str, path: Path) -> str:
    opts = "".join(f.lstrip("-") for f in flags)
    return J.run_text(path.read_text(encoding="utf-8"), J.parse(filt),
                      raw="r" in opts, compact="c" in opts,
                      sort_keys="S" in opts, numbers=J.number_policy())


def _cases():
    for f in ACCEPTED:
        yield "obj.json", f
    for f in ARR_FILTERS:
        yield "arr.json", f
    for f in SCALAR_FILTERS:
        yield "scalar.json", f
        yield "num.json", f
    for f in NULL_FILTERS:
        yield "null.json", f


@needs_jq
@pytest.mark.parametrize("name,filt", list(_cases()))
def test_matches_real_jq(files, name, filt):
    for flags in FLAGS:
        want = _jq(flags, filt, files / name)
        assert want.returncode == 0, (flags, filt, want.stderr)
        assert _ours(flags, filt, files / name) == want.stdout, (flags, filt)


# Filters jq accepts but whose result is a jq error: the subset refuses
# them at run time, so the real jq reports the error.
RUNTIME_ERRORS = [".a.b.c", ".list.x", ".s[0]", ".list[2].y | length", ".big | keys",
                  ".s | keys", ".list[0].x[]", ".e.x[]", ".a[0]",
                  ".list[] | .y | length"]


@needs_jq
@pytest.mark.parametrize("filt", RUNTIME_ERRORS)
def test_jq_errors_are_refused(files, filt):
    want = _jq([], filt, files / "obj.json")
    assert want.returncode != 0
    with pytest.raises(J.Refused):
        _ours([], filt, files / "obj.json")


@needs_jq
@pytest.mark.parametrize("filt", [".a.b[1] | length", ".big | length"])
def test_computed_doubles_are_refused(files, filt):
    assert _jq([], filt, files / "obj.json").returncode == 0
    with pytest.raises(J.Refused):
        _ours([], filt, files / "obj.json")


@pytest.mark.parametrize("text", [
    "\ufeff{}", "", "  \n", '{"a": NaN}', "[Infinity]", '{"a":1}\n{"a":2}\n',
    '"\\ud800"', "{'a': 1}", "[1,]",
])
def test_inputs_jq_reads_differently_are_refused(text):
    with pytest.raises(J.Refused):
        J.run_text(text, J.parse("."))


@pytest.mark.parametrize("text,out", [
    ("1e3", None), ("[1E-2]", None), ("1.0", "1.0\n"), ("-0.10", "-0.10\n"),
    ("100000000000000000001", "100000000000000000001\n"),
])
def test_number_literals_follow_jq_17(text, out):
    if out is None:
        with pytest.raises(J.Refused, match="notation"):
            J.run_text(text, J.parse("."))
    else:
        assert J.run_text(text, J.parse("."), numbers="literal") == out


@pytest.mark.parametrize("text,out", [
    ("1.0", None), ("-0", None), ("100000000000000000", None),
    ("99999999999999999", "99999999999999999\n"), ("[3, -4]", "[3,-4]\n"),
])
def test_older_jq_only_gets_integers(text, out):
    if out is None:
        with pytest.raises(J.Refused):
            J.run_text(text, J.parse("."), numbers="integers")
    else:
        assert J.run_text(text, J.parse("."), compact=True,
                          numbers="integers") == out


def test_number_policy_follows_jq_version(monkeypatch):
    monkeypatch.setattr(J, "_VERSION", {"v": (1, 6)})
    assert J.number_policy() == "integers"
    monkeypatch.setattr(J, "_VERSION", {"v": (1, 7)})
    assert J.number_policy() == "literal"
    monkeypatch.setattr(J, "_VERSION", {"v": None})
    assert J.number_policy() == "literal"


@pytest.mark.parametrize("filt,program", [
    (".", [("path", [])]),
    (".a.b", [("path", [("key", "a"), ("key", "b")])]),
    ('."a b"[0]', [("path", [("key", "a b"), ("index", 0)])]),
    (".[1:]", [("path", [("slice", 1, None)])]),
    (".a[] | keys", [("path", [("key", "a"), ("iter",)]), ("builtin", "keys")]),
    ('.["x|y"]', [("path", [("key", "x|y")])]),
])
def test_parse(filt, program):
    assert J.parse(filt) == program


@pytest.mark.parametrize("filt", [
    "", " ", "..", ".a?", ".a,.b", ".[]?", "map(.x)", "select(.a)", ".a + 1",
    ".a | .b | .c | .d | .e | .f | .g", '."a\\"b"', ".a.[0]", ".[1.5]",
    ".[01]", ".[:]", "$x", ".a as $x | $x", "keys[]", ".a | not",
    ". a", ".if", ".[\"a\"", "to_entries", ".a\t", "length | length x",
    ".[a]", ".a[", '"a"', "[.a]", "{a}", ".a // 1", "@csv",
])
def test_parse_refuses(filt):
    with pytest.raises(J.Unsupported, match="is not mapped"):
        J.parse(filt)


def test_pointer_of():
    assert J.pointer_of(J.parse(".a.b[2]")) == ["a", "b", 2]
    assert J.pointer_of(J.parse(".")) == []
    assert J.pointer_of(J.parse(".a[-1]")) is None
    assert J.pointer_of(J.parse(".a | keys")) is None
    assert J.pointer_of(J.parse(".a[]")) is None


# --------------------------------------------------------------------------- #
# shell classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command,file,filt,opts", [
    ("jq . f.json", "f.json", ".", {}),
    ("jq '.a.b' f.json", "f.json", ".a.b", {}),
    ("jq -r '.a[0]' f.json", "f.json", ".a[0]", {"raw": True}),
    ("jq -c '.[2:4]' f.json", "f.json", ".[2:4]", {"compact": True}),
    ("jq -rc '.a | keys' f.json", "f.json", ".a | keys",
     {"raw": True, "compact": True}),
    ("jq -S -M . f.json", "f.json", ".", {"sort_keys": True}),
    ("jq --raw-output --compact-output .x f.json", "f.json", ".x",
     {"raw": True, "compact": True}),
    ("jq '.[] | .x' f.json", "f.json", ".[] | .x", {}),
    ("jq '.\"key with space\"' f.json", "f.json", '."key with space"', {}),
    ("jq length f.json", "f.json", "length", {}),
    ("cat f.json | jq '.a'", "f.json", ".a", {"stdin": True}),
    ("cat f.json | jq -r keys", "f.json", "keys", {"stdin": True, "raw": True}),
    ("jq '.a' < f.json", "f.json", ".a", {"stdin": True}),
    ("< f.json jq type", "f.json", "type", {"stdin": True}),
    ("jq -c . <f.json", "f.json", ".", {"stdin": True, "compact": True}),
])
def test_classify_accepts_jq(command, file, filt, opts):
    got = classify(command)
    assert got["class"] == "jq", got
    assert got["file"] == file and got["filter"] == filt
    for k in ("raw", "compact", "sort_keys", "stdin"):
        assert got[k] is opts.get(k, False), k


def test_classify_cd_jq():
    got = classify("cd data && jq .a f.json")
    assert got["class"] == "jq" and got["cd"] == "data"


@pytest.mark.parametrize("command,reason", [
    ("jq 'map(.x)' f.json", "jq filter 'map(.x)' is not mapped"),
    ("jq '.a?' f.json", "jq filter '.a?' is not mapped"),
    ("jq '.a, .b' f.json", "jq filter '.a, .b' is not mapped"),
    ("jq 'select(.ok)' f.json", "jq filter 'select(.ok)' is not mapped"),
    ("jq '..' f.json", "jq filter '..' is not mapped"),
    ("jq '.[] | select(.a == 1)' f.json", "is not mapped"),
    ("jq -e .a f.json", "jq option -e is not mapped"),
    ("jq --arg x 1 .a f.json", "jq option --arg is not mapped"),
    ("jq -s . f.json", "jq option -s is not mapped"),
    ("jq -n 1", "jq option -n is not mapped"),
    ("jq -C . f.json", "jq option -C is not mapped"),
    ("jq . a.json b.json", "jq of 2 files is not mapped"),
    ("jq .a", "jq of 0 files is not mapped"),
    ("jq . -", "jq of 1 files is not mapped"),
    ("jq", "jq without a filter"),
    ("jq .a f.json -r", "after the filter"),
])
def test_classify_refuses_jq(command, reason):
    got = classify(command)
    assert got["class"] == "other", got
    assert reason in got["reason"]


@pytest.mark.parametrize("command", [
    "cat a.json b.json | jq .",
    "cat f.json | jq . | head -3",
    "jq . f.json > out.json",
    "jq . f.json 2>&1",
    "cat f.json | jq . f.json",
    "curl x | jq .",
    "jq .a[0] f.json",                 # unquoted glob characters
    "jq \"$F\" f.json",
    "jq . < f.json < g.json",
    "cat -n f.json | jq .",
])
def test_classify_jq_pipelines_not_mapped(command):
    assert classify(command)["class"] in ("complex", "other", "compound")
    assert classify(command)["class"] != "jq"


def test_jq_is_read_only_for_after(tmp_path):
    from ozagia.intent import _may_change_files
    assert _may_change_files("shell", {"command": "jq .a f.json"})[0] is False
    assert _may_change_files("shell", {"command": "cat f | jq .a"})[0] is False


def test_plugin_treats_shell_jq_as_read_only():
    js = (Path(__file__).resolve().parents[1] / "src" / "ozagia"
          / "integrations" / "opencode" / "plugin.js").read_text()
    assert '"shell_grep", "shell_jq"]' in js


# --------------------------------------------------------------------------- #
# the shell capture
# --------------------------------------------------------------------------- #
@pytest.fixture
def mem(tmp_path: Path, monkeypatch) -> DevMemory:
    for var in ("OZAGIA_CONFIG", "OZAGIA_STATE_DIR", "OZAGIA_ADAPTERS"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    for name, text in FIXTURES.items():
        (root / "data" / name).write_text(text, encoding="utf-8")
    (root / "data" / "exp.json").write_text('{"x": 1e3}')
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    dump = tmp_path / "state" / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=root)


@needs_jq
@pytest.mark.parametrize("command", [
    "jq .a data/obj.json",
    "jq -r '.list[] | .x' data/obj.json",
    "cat data/obj.json | jq -c '.a.b'",
    "jq -S . < data/obj.json",
    "cd data && jq '.[3:5]' arr.json",
])
def test_shell_jq_output_is_jqs(mem, command):
    res = I.handle(mem, "shell", {"command": command})
    assert res["handled"] and not res["is_error"], res
    assert res["meta"]["intent"] == "shell_jq"
    root = Path(mem.raw_repo_root)
    want = subprocess.run(["bash", "-c", command], cwd=root,
                          capture_output=True, text=True, encoding="utf-8")
    assert want.returncode == 0
    assert res["output"] == want.stdout


def test_shell_jq_keys_and_passthrough(mem):
    res = I.handle(mem, "shell", {"command": "jq '.a.b[2]' data/obj.json"})
    assert res["meta"]["keys"] == ["json:data/obj.json#/a/b/2"]
    # A jq error, an exponent and a missing file are left to the real jq.
    for command in ("jq .a.b.c data/obj.json", "jq . data/exp.json",
                    "jq . data/missing.json", "jq 'map(.x)' data/obj.json"):
        res = I.handle(mem, "shell", {"command": command})
        assert res["handled"] is False, (command, res)
    res = I.handle(mem, "shell", {"command": "jq . /etc/hostname"})
    assert res["handled"] is False

