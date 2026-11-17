"""The intent layer: captured tool calls carried out on the graph."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ozagia import intent as I
from ozagia.core import memory as M
from ozagia.core.memory import DevMemory, build_index
from ozagia.intent.shell import classify

MOD = '''"""Module."""
import os


def helper(a, b=1):
    """Help."""
    return a + b


@staticmethod
def decorated():
    return os.sep


class Box:
    def __init__(self):
        self.n = 0

    def m(self, a):
        return a
'''

USE = '''"""Uses mod."""
from pkg.mod import helper


def run():
    return helper(1, 2)
'''

TEST_MOD = '''from pkg.mod import helper


def test_helper():
    assert helper(1) == 2


def test_broken():
    assert helper(2) == 5
'''

README = "# Project\n\nUse helper.\n"

YAML = """name: app

steps:
  - build
  - test

deploy:
  target: prod
"""

_LINE_RE = re.compile(r"^(\d+): (.*)$", re.S)


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "HOME": str(root), "PATH": "/usr/bin:/bin"}).stdout


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
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)


@pytest.fixture
def repo(tmp_path: Path, layout) -> Path:
    root = tmp_path / "repo"
    _write(root / "pkg" / "__init__.py", '"""The package."""\n')
    _write(root / "pkg" / "mod.py", MOD)
    _write(root / "pkg" / "use.py", USE)
    _write(root / "pkg" / "big.py",
           "".join(f"X{i} = {i}\n" for i in range(1, 2101)))
    _write(root / "pkg" / "many.txt", "needle\n" * 150)
    _write(root / "tests" / "test_mod.py", TEST_MOD)
    _write(root / "README.md", README)
    _write(root / "deploy.yaml", YAML)
    (root / "blob.bin").write_bytes(b"\0\1\2needle\0" * 10)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    # The state dir is never searched.
    _write(root / ".ozagia" / "notes.txt", "needle helper\n")
    return root


@pytest.fixture
def mem(repo: Path, tmp_path: Path) -> DevMemory:
    dump = tmp_path / "state" / "graph.json"
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=repo)


def _call(mem, tool, **inp):
    res = I.handle(mem, tool, inp)
    assert set(res["meta"]) >= {"intent", "keys", "ms"}
    return res


def _ok(mem, tool, **inp) -> str:
    res = _call(mem, tool, **inp)
    assert res["handled"] is True, res
    assert res["is_error"] is False, res["output"]
    return res["output"]


def _source(output: str) -> list[tuple[int, str]]:
    """(n, text) of every `<n>: ` line; every other line after the first
    must be a graph addition."""
    out = []
    for line in output.split("\n")[1:]:
        if line.startswith("[ozagia] "):
            continue
        m = _LINE_RE.match(line)
        assert m, f"neither source nor [ozagia] line: {line!r}"
        out.append((int(m.group(1)), m.group(2)))
    return out


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").split("\n")[:-1]


def _preceding(output: str, source_line: str) -> str:
    lines = output.split("\n")
    return lines[lines.index(source_line) - 1]


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def test_read_file_is_verbatim_with_headers(mem, repo):
    out = _ok(mem, "read", path="pkg/mod.py")
    lines = out.split("\n")
    total = len(_lines(repo / "pkg/mod.py"))
    assert lines[0] == f"Read file {repo / 'pkg/mod.py'}, lines 1-{total}"
    assert lines[1] == ("[ozagia] file:pkg/mod.py  imports: 0  imported by: 2  "
                        "tests: 2")
    assert [t for _, t in _source(out)] == _lines(repo / "pkg/mod.py")
    assert [n for n, _ in _source(out)] == list(range(1, total + 1))
    assert _preceding(out, "5: def helper(a, b=1):") == (
        "[ozagia] sym:pkg/mod.py::helper  tests: 2 "
        "(test:tests/test_mod.py::test_helper, "
        "test:tests/test_mod.py::test_broken)")
    # A decorated def: the header precedes the decorator.
    assert _preceding(out, "10: @staticmethod").startswith(
        "[ozagia] sym:pkg/mod.py::decorated")
    assert _preceding(out, "15: class Box:").startswith("[ozagia] sym:pkg/mod.py::Box")
    # Only module-level symbols get headers.
    assert "sym:pkg/mod.py::Box.m" not in out
    res = _call(mem, "read", path=str(repo / "pkg/mod.py"))
    assert res["output"] == out
    assert "sym:pkg/mod.py::helper" in res["meta"]["keys"]


def test_read_range_and_continuation_header(mem, repo):
    out = _ok(mem, "read", path="pkg/mod.py", offset=7, limit=3)
    lines = out.split("\n")
    assert lines[0].endswith("lines 7-9 of 20")
    assert lines[1].startswith("[ozagia] file:pkg/mod.py")
    assert lines[2].startswith("[ozagia] sym:pkg/mod.py::helper")
    assert lines[2].endswith("(from line 5)")
    assert _source(out) == [(7, "    return a + b"), (8, ""), (9, "")]


@pytest.fixture
def no_outline(tmp_path, monkeypatch):
    """Outline-first reads off (`[reads] outline_lines = outline_bytes = 0`)."""
    cfg = tmp_path / "no_outline.toml"
    cfg.write_text("[reads]\noutline_lines = 0\noutline_bytes = 0\n")
    monkeypatch.setenv("OZAGIA_CONFIG", str(cfg))


def test_read_default_limit_is_2000_lines(mem, repo, no_outline):
    out = _ok(mem, "read", path="pkg/big.py")
    assert out.split("\n")[0].endswith("lines 1-2000 of 2100")
    src = _source(out)
    assert len(src) == 2000 and src[-1] == (2000, "X2000 = 2000")


def test_read_offset_out_of_range_is_an_error(mem):
    res = _call(mem, "read", path="pkg/use.py", offset=99)
    assert res["handled"] and res["is_error"]
    assert "out of range" in res["output"]


def test_read_node_keys(mem, repo):
    out = _ok(mem, "read", path="sym:pkg/mod.py::Box.m")
    assert out.split("\n")[0] == f"Read file {repo / 'pkg/mod.py'}, lines 19-20 of 20"
    assert out.split("\n")[1].startswith("[ozagia] sym:pkg/mod.py::Box.m")
    assert _source(out) == [(19, "    def m(self, a):"), (20, "        return a")]

    out = _ok(mem, "read", path="test:tests/test_mod.py::test_broken")
    assert out.split("\n")[1] == "[ozagia] test:tests/test_mod.py::test_broken"
    assert [t for _, t in _source(out)] == _lines(repo / "tests/test_mod.py")[7:9]

    out = _ok(mem, "read", path="text:deploy.yaml#L3-L6")
    assert out.split("\n")[1] == "[ozagia] text:deploy.yaml#L3-L6"
    assert [n for n, _ in _source(out)] == [3, 4, 5, 6]

    out = _ok(mem, "read", path="doc:README.md")
    assert [t for _, t in _source(out)] == _lines(repo / "README.md")
    assert "[ozagia] doc:README.md" in out.split("\n")

    res = _call(mem, "read", path="sym:pkg/mod.py::helpr")
    assert res["is_error"] and res["output"].startswith("No node sym:pkg/mod.py::helpr")
    assert "sym:pkg/mod.py::helper" in res["output"]


def test_read_unparsed_text_has_chunk_headers(mem, repo):
    out = _ok(mem, "read", path="deploy.yaml")
    assert [t for _, t in _source(out)] == _lines(repo / "deploy.yaml")
    assert _preceding(out, "1: name: app") == "[ozagia] text:deploy.yaml#L1-L2"
    assert _preceding(out, "3: steps:") == "[ozagia] text:deploy.yaml#L3-L6"
    assert _preceding(out, "7: deploy:") == "[ozagia] text:deploy.yaml#L7-L8"


def test_read_directory(mem, repo):
    out = _ok(mem, "read", path="pkg")
    lines = out.split("\n")
    assert lines[0] == f"Read directory {repo / 'pkg'}, entries 1-5"
    assert lines[1:] == [
        "__init__.py  [ozagia] 0 symbols, 0 tests",
        "big.py  [ozagia] 0 symbols, 0 tests",
        "many.txt",
        "mod.py  [ozagia] 5 symbols, 2 tests",
        "use.py  [ozagia] 1 symbols, 0 tests",
    ]
    root_out = _ok(mem, "read", path=".")
    assert "pkg/" in root_out.split("\n")
    assert "tests/" in root_out.split("\n")


def test_read_unhandled_and_refused_paths(mem, repo):
    assert _call(mem, "read", path="blob.bin")["handled"] is False
    assert _call(mem, "read", path="missing.py")["handled"] is False
    for bad in ("/etc/passwd", "../outside.py", "pkg/../../x"):
        res = _call(mem, "read", path=bad)
        assert res["handled"] and res["is_error"], bad
        assert "outside the project root" in res["output"]


# --------------------------------------------------------------------------- #
# grep / glob
# --------------------------------------------------------------------------- #
def _grep_matches(output: str) -> list[str]:
    return [ln for ln in output.split("\n") if ln.startswith("  Line ")]


@pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep for the expected count")
def test_grep_groups_matches_under_keys(mem, repo):
    out = _ok(mem, "grep", pattern="helper")
    lines = out.split("\n")
    assert lines[0] == "[ozagia] defined: sym:pkg/mod.py::helper"
    expected = subprocess.run(["rg", "-c", "--no-filename", "helper", str(repo)],
                              capture_output=True, text=True).stdout.split()
    total = sum(int(x) for x in expected)
    assert lines[1] == f"Found {total} matches"
    assert len(_grep_matches(out)) == total
    block = out.split(f"{repo / 'pkg/use.py'}:\n", 1)[1].split("\n\n", 1)[0]
    assert block.split("\n") == [
        "[ozagia] file:pkg/use.py",
        "  Line 2: from pkg.mod import helper",
        "[ozagia] sym:pkg/use.py::run",
        "  Line 6:     return helper(1, 2)",
    ]
    tests_block = out.split(f"{repo / 'tests/test_mod.py'}:\n", 1)[1].split("\n\n")[0]
    assert "[ozagia] test:tests/test_mod.py::test_broken" in tests_block
    assert ".ozagia" not in out
    assert f"{repo / 'README.md'}:\n[ozagia] doc:README.md" in out


def test_grep_defined_only_for_identifiers(mem):
    out = _ok(mem, "grep", pattern="def helper")
    assert not out.startswith("[ozagia] defined")
    assert out.startswith("Found 1 matches")


def test_grep_literal_include_and_path(mem, repo):
    assert _ok(mem, "grep", pattern="a + b").startswith("No files found")
    out = _ok(mem, "grep", pattern="a + b", literal=True)
    assert _grep_matches(out) == ["  Line 7:     return a + b"]
    out = _ok(mem, "grep", pattern="t", include="*.yaml")
    assert {ln[:-1] for ln in out.split("\n") if ln.startswith("/")} == {
        str(repo / "deploy.yaml")}
    out = _ok(mem, "grep", pattern="helper", path="tests")
    assert str(repo / "pkg") not in out and str(repo / "tests/test_mod.py") in out
    res = _call(mem, "grep", pattern="helper", path="/tmp")
    assert res["is_error"]


def test_grep_truncates_at_100_and_reports_the_total(mem):
    out = _ok(mem, "grep", pattern="needle")
    lines = out.split("\n")
    assert lines[0] == "Found 150 matches (showing first 100)"
    assert len(_grep_matches(out)) == 100
    assert lines[-1].startswith("(Results are truncated: showing 100 of 150")
    assert "text:pkg/many.txt#L1-L60" in out


def test_grep_bad_regex_is_an_error(mem):
    res = _call(mem, "grep", pattern="(")
    assert res["handled"] and res["is_error"]


def test_glob_lists_newest_first_with_roles(mem, repo):
    future = time.time() + 100
    os.utime(repo / "pkg/use.py", (future, future))
    out = _ok(mem, "glob", pattern="*.py")
    lines = out.split("\n")
    assert lines[0] == f"{repo / 'pkg/use.py'}  [ozagia] 1 symbols, 0 tests"
    assert f"{repo / 'pkg/mod.py'}  [ozagia] 5 symbols, 2 tests" in lines
    assert f"{repo / 'tests/test_mod.py'}  [ozagia] 0 symbols, 2 tests" in lines
    assert _ok(mem, "glob", pattern="*.nothing") == "No files found"
    out = _ok(mem, "glob", pattern="*.yaml")
    assert out == str(repo / "deploy.yaml")


# --------------------------------------------------------------------------- #
# edit / write
# --------------------------------------------------------------------------- #
def test_edit_reports_delta_and_broken_references(mem, repo):
    out = _ok(mem, "edit", path="pkg/mod.py", oldString="def helper(a, b=1):",
              newString="def helper(a):")
    lines = out.split("\n")
    assert lines[0] == f"Edited {repo / 'pkg/mod.py'} (1 replacement)"
    assert lines[1] == "[ozagia] in: sym:pkg/mod.py::helper"
    assert lines[2].startswith("[ozagia] delta: 0 added, 0 removed, 2 changed")
    assert "~sym:pkg/mod.py::helper" in lines[2]
    assert ("[ozagia] broken: arity sym:pkg/use.py::run pkg/use.py:6 helper — "
            "2 positional given, at most 1; signature helper(a)") in lines
    assert "def helper(a):" in (repo / "pkg/mod.py").read_text()


def test_edit_round_trips_text_copied_from_read(mem, repo):
    out = _ok(mem, "read", path="pkg/mod.py", offset=15, limit=6)
    old = "\n".join(t for _, t in _source(out))
    new = old.replace("return a", "return a * 2")
    _ok(mem, "edit", path="pkg/mod.py", oldString=old, newString=new)
    assert "return a * 2" in (repo / "pkg/mod.py").read_text()


def test_edit_replace_all_and_count(mem, repo):
    out = _ok(mem, "edit", path="deploy.yaml", oldString="  - ", newString="  -  ",
              replaceAll=True)
    assert out.split("\n")[0].endswith("(2 replacements)")


def test_edit_errors(mem, repo):
    before = (repo / "pkg/mod.py").read_text()
    res = _call(mem, "edit", path="pkg/mod.py", oldString="def helpr(a):",
                newString="x")
    # No exact match: the built-in edit (with its tolerant matchers) gets it.
    assert res["handled"] is False
    assert "no exact match" in res["reason"]
    assert (repo / "pkg/mod.py").read_text() == before

    res = _call(mem, "edit", path="pkg/mod.py", oldString="return", newString="x")
    assert res["is_error"]
    lines = res["output"].split("\n")
    assert lines[0].startswith("Found multiple matches for oldString.")
    assert "[ozagia] match: L7 sym:pkg/mod.py::helper: return a + b" in lines
    assert "[ozagia] match: L20 sym:pkg/mod.py::Box.m: return a" in lines

    res = _call(mem, "edit", path="pkg/mod.py", oldString="def m(self, a):",
                newString="def m(self, a:")
    assert res["is_error"]
    assert res["output"].startswith("Edit refused: the result does not parse (")
    assert "at line 19)" in res["output"].split("\n")[0]
    assert "[ozagia] in: sym:pkg/mod.py::Box.m" in res["output"]

    res = _call(mem, "edit", path="/etc/hosts", oldString="a", newString="b")
    assert res["is_error"] and "outside the project root" in res["output"]
    res = _call(mem, "edit", path="pkg/mod.py", oldString="os", newString="os")
    assert res["is_error"] and "identical" in res["output"]
    res = _call(mem, "edit", path="pkg/nope.py", oldString="a", newString="b")
    assert res["is_error"] and "not found" in res["output"]
    assert (repo / "pkg/mod.py").read_text() == before


def test_edit_with_empty_old_string_creates_the_file(mem, repo):
    res = _call(mem, "edit", path="pkg/fresh.py", oldString="",
                newString="def fresh():\n    return 1\n")
    assert res["meta"]["intent"] == "edit_write"
    assert res["output"].split("\n")[0] == "Wrote file successfully."
    assert "+sym:pkg/fresh.py::fresh" in res["output"]
    assert mem.node_by_key("sym:pkg/fresh.py::fresh") is not None


def test_write_create_and_overwrite(mem, repo):
    out = _ok(mem, "write", path="pkg/sub/new.py", content="def a():\n    pass\n")
    assert out.split("\n")[:2] == ["Wrote file successfully.",
                                   "[ozagia] created file:pkg/sub/new.py"]
    assert (repo / "pkg/sub/new.py").read_text() == "def a():\n    pass\n"
    assert mem.node_by_key("sym:pkg/sub/new.py::a") is not None

    out = _ok(mem, "write", path="pkg/use.py", content="def other():\n    pass\n")
    head = out.split("\n")[1]
    assert head.startswith("[ozagia] replaced file:pkg/use.py  backup: ")
    backup = Path(head.split("backup: ", 1)[1])
    assert backup.read_text() == USE
    assert "-sym:pkg/use.py::run" in out and "+sym:pkg/use.py::other" in out
    assert mem.node_by_key("sym:pkg/use.py::run") is None
    res = _call(mem, "write", path="../escape.py", content="x")
    assert res["is_error"]
    res = _call(mem, "write", path=".ozagia/x.txt", content="x")
    assert res["is_error"] and "state directory" in res["output"]


def test_patch_is_left_to_the_builtin(mem):
    res = _call(mem, "patch", patchText="*** Begin Patch")
    assert res["handled"] is False


def test_after_patch_refreshes_the_graph(mem, repo):
    # Codex's apply_patch ran the built-in; `after` re-ingests what changed.
    path = repo / "pkg" / "use.py"
    path.write_text(path.read_text() + "\n\ndef added():\n    return run()\n")
    patch = ("*** Begin Patch\n*** Update File: pkg/use.py\n@@\n+def added():\n"
             "+    return run()\n*** End Patch\n")
    res = I.after(mem, "patch", {"patch": patch}, "Success. Updated the following files:\n"
                  "M pkg/use.py\n", harness="codex")
    assert res["meta"]["intent"] == "after_refresh"
    assert res["meta"]["changed_files"] == 1
    assert "+sym:pkg/use.py::added" in res["append"]
    assert res["append"].startswith("[ozagia] delta: 1 added")
    assert mem.node_by_key("sym:pkg/use.py::added") is not None


# --------------------------------------------------------------------------- #
# shell
# --------------------------------------------------------------------------- #
CLASSIFY = [
    ("pytest", "pytest"),
    ("pytest -q tests/test_x.py::test_a", "pytest"),
    ("py.test tests", "pytest"),
    ('python -m pytest -x -k "foo and bar" tests', "pytest"),
    ("python3 -m pytest tests/test_x.py", "pytest"),
    (".venv/bin/python -m pytest tests -vv", "pytest"),
    ("/usr/bin/python3.13 -m pytest --tb=short", "pytest"),
    ("pytest --tb short tests", "pytest"),
    ("pytest -rA -p no:randomly tests", "pytest"),
    ("python3 -m pytest --lf", "other"),
    ("pytest --pdb tests", "other"),
    ("pytest --weird thing", "other"),
    ("python -X dev -m pytest", "other"),
    ("cat pkg/mod.py", "read"),
    ("cat a b", "other"),
    ("cat -n a", "other"),
    ("head pkg/mod.py", "read"),
    ("head -n 20 a.py", "read"),
    ("head -20 a.py", "read"),
    ("tail -n 5 a.py", "read"),
    ("tail -n +5 a.py", "read"),
    ("tail -f log.txt", "other"),
    ("sed -n '10,20p' a.py", "read"),
    ("sed -n 5p a.py", "read"),
    ("sed -n '3,$p' a.py", "read"),
    ("sed -i 's/a/b/' a.py", "other"),
    ("wc -l a.py", "other"),
    ("nl a.py", "read"),
    ("grep -rn foo .", "grep"),
    ("grep -rni foo src", "grep"),
    ("grep -n foo a.py", "grep"),
    ("grep -rnF 'a.b(' .", "grep"),
    ("grep -rnE 'foo|bar' src", "grep"),
    ("grep -rn 'foo\\|bar' .", "grep"),        # BRE alternation, converted
    ("grep -rn 'a\\1' .", "other"),             # a back-reference has no equivalent
    ("grep foo", "other"),
    ("grep -rnw foo .", "other"),
    ("grep -rn foo --include=*.py .", "grep"),
    ("grep -rn foo a b", "grep"),
    ("rg foo", "grep"),
    ("rg -i Foo src", "grep"),
    ("rg -t py foo", "grep"),
    ("git grep -n foo", "grep"),
    ("git diff", "git_read"),
    ("git diff HEAD~1 -- pkg", "git_read"),
    ("git show HEAD", "git_read"),
    ("git status --short", "git_read"),
    ("git log --oneline -5", "git_read"),
    ("git commit -m x", "git_refs"),
    ("git -C sub push origin main", "git_refs"),
    ("git checkout -b feature", "git_refs"),
    ("git reset --hard", "git_refs"),
    ("git rebase main", "git_refs"),
    ("git branch", "git_read"),
    ("git branch new", "git_refs"),
    ("git stash", "git_refs"),
    ("git stash list", "git_read"),
    ("git tag -l", "git_read"),
    ("git tag v1", "git_refs"),
    ("git worktree add ../x", "git_refs"),
    ("git add . && git commit -m x", "git_refs"),
    ("git add pkg", "other"),
    ("ls", "ls"),
    ("ls pkg", "ls"),
    ("ls -la", "other"),
    ("find . -type f -name '*.py'", "find"),
    ("find src -type f -name '*.py'", "find"),
    ("find . -type f -name '[ab].py'", "find"),
    ("find . -type f -name 'a\\*.py'", "find"),
    ("find . -exec rm {} \\;", "complex"),
    ("cd src && pytest", "compound"),
    ("cd pkg && cat mod.py", "read"),
    ("cd 'pkg' && grep -rn helper .", "grep"),
    ("cd $DIR && cat a.py", "complex"),
    ("cd pkg && python gen.py", "complex"),
    ("cd missing && ls", "ls"),
    ("cd pkg && cat mod.py | head -3", "compound"),
    ("FOO=1 pytest", "compound"),         # an env prefix on pytest is captured
    ("FOO=1 cat a", "complex"),
    ("pytest | tail", "compound"),
    ("pytest > out.txt", "complex"),
    ("pytest; echo done", "compound"),
    ("echo $(git rev-parse HEAD)", "complex"),
    ("cat `ls`", "complex"),
    ("cat $FILE", "complex"),
    ("cat ~/x", "complex"),
    ("cat *.py", "complex"),
    ("grep -rn 'foo$' .", "grep"),
    ('grep -rn "foo$" .', "grep"),
    ('grep -rn "$HOME" .', "complex"),
    ("python -c 'print(1)'", "other"),
    ("python script.py", "other"),
    ("./run.sh", "other"),
    ("echo 'unbalanced", "complex"),
    ("", "other"),
    # review fix 10: ref-moving git through paths, env prefixes,
    # substitutions, shells, aliases and bisect
    ("/usr/bin/git commit -am x", "git_refs"),
    ("GIT_DIR=.git git commit -m x", "git_refs"),
    ("echo `git stash`", "git_refs"),
    ('echo "$(git reset --hard)"', "git_refs"),
    ("bash -c 'git reset --hard'", "git_refs"),
    ("sh -lc 'cd x && git checkout y'", "git_refs"),
    ("zsh -c \"bash -c 'git commit -m x'\"", "git_refs"),
    ("git -c alias.c=commit c -m x", "git_refs"),
    ("git -c 'alias.x=!git push' x", "git_refs"),
    ("git bisect start HEAD~3", "git_refs"),
    ("echo hi\ngit commit -m x", "git_refs"),
    ("git bisect log", "git_read"),
    ("/usr/bin/git status", "git_read"),
    ("FOO=1 git status", "complex"),
    ("bash -c 'git status'", "other"),
    ("echo '$(git stash)'", "other"),
    ("git -c user.name=x log", "git_read"),
    # W2: more shell capture
    ("rg foo pkg tests", "grep"),
    ("rg -n foo pkg/mod.py README.md", "grep"),
    ("grep -rn foo pkg tests README.md", "grep"),
    ("git grep foo a b", "other"),                 # git grep: one path
    ("rg -C 2 foo pkg", "grep"),
    ("rg -A1 -B2 foo", "grep"),
    ("rg --context=3 foo", "grep"),
    ("grep -rnC2 foo .", "grep"),
    ("grep -n -2 foo a.py", "grep"),
    ("grep -A x foo a.py", "other"),
    ("git grep -C2 foo", "other"),
    ("rg -g '*.py' -g '!test_*' foo", "grep"),
    ("rg --glob=*.py foo", "grep"),
    ("rg -t py -T md foo src", "grep"),
    ("rg --hidden foo", "other"),
    ("rg -e a -e b", "other"),
    ("rg foo -", "other"),
    ("grep -rn --include=src/*.py foo .", "other"),
    ("rg --files", "files"),
    ("rg --files pkg tests -g '*.py'", "files"),
    ("rg --files -t py --hidden", "files"),
    ("rg --files -uu --sort path", "files"),
    ("rg --files -L", "other"),
    ("rg --files --null", "other"),
    ("rg --files --sort modified", "other"),
    ("cd pkg && rg --files", "files"),
    ("nl -ba a.py", "read"),
    ("nl -b a a.py", "read"),
    ("nl --body-numbering=t a.py", "read"),
    ("nl -bn a.py", "other"),
    ("nl -w3 a.py", "other"),
    ("nl a b", "other"),
    ("tail -c 100 a.py", "read"),
    ("tail -c +5 a.py", "read"),
    ("tail --bytes=20 a.py", "read"),
    ("tail -c 1K a.py", "other"),
    ("tail -n 2 -c 5 a.py", "other"),
    ("head -c 5 a.py", "other"),
    ("LC_ALL=C rg foo", "grep"),
    ("NO_COLOR=1 TERM=dumb cat a.py", "read"),
    ("LANG=C nl -ba a.py", "read"),
    ("COLUMNS=80 rg --files", "files"),
    ("LC_ALL=C grep -rn foo .", "other"),          # locale changes grep
    ("LC_ALL=C find . -iname '*.PY'", "other"),    # and find -iname
    ("LC_ALL=C find . -name '*.py'", "find"),
    ("FOO=1 rg foo", "complex"),
    ("LC_ALL=C pytest", "compound"),
    ("timeout 10 rg foo", "grep"),
    ("timeout -s KILL 5 cat a.py", "read"),
    ("timeout --signal=TERM -k 2 1.5m nl -ba a.py", "read"),
    ("timeout 5 find . -name '*.py'", "find"),
    ("timeout 5 pytest", "other"),
    ("timeout 5 python x.py", "other"),
    ("timeout 5", "other"),
    ("timeout 5 git push", "git_refs"),
    ("timeout 5 git log -1", "git_read"),
    ("find . -maxdepth 2 -type f -name '*.py'", "find"),
    ("find src tests -type d", "find"),
    ("find . -mindepth 1 -maxdepth 1", "find"),
    ("find . -name '*.py' -not -path './.git/*'", "find"),
    ("find . ! -name '*.pyc' -type f", "find"),
    ("find . -path ./node_modules -prune -o -name '*.js' -print", "find"),
    ("find . \\( -name '*.md' -o -name '*.rst' \\) -print", "find"),
    ("find . -iname 'readme*'", "find"),
    ("find . -newer setup.py -type f", "find"),
    ("find . -type l", "find"),
    ("find .", "find"),
    ("find . -name '*.py' -maxdepth 1", "other"),  # global option after a test
    ("find . -type f -size +1k", "other"),
    ("find . -name '*.py' -print0", "other"),
    ("find . -name '*.py' -delete", "other"),
    ("find -L . -name x", "other"),
    ("find . -name 'a/b'", "other"),
    ("find . -name '[[:alpha:]]*'", "other"),
    ("find . -type f,d", "other"),
    ("(cd pkg && rg foo)", "compound"),
    ("(cd pkg; cat mod.py)", "compound"),
    ("( cd pkg && git status ) && echo x", "compound"),
    ("cd ~ && rg foo", "compound"),
    ("cd ~/proj && cat a.py", "compound"),
    ("cd ~bob && cat a.py", "complex"),
    ("rg foo | wc -l", "compound"),
    ("rg -n foo | cut -d: -f1 | sort -u", "compound"),
    ("cat a.py | sort -rn | uniq -c", "compound"),
    ("find . -name '*.py' | wc -l", "compound"),
    ("rg --files | grep test", "compound"),
    ("nl -ba a.py | sed -n '3,9p'", "compound"),
    ("cat a.py | sort -k2", "complex"),
    ("cat a.py | cut -c1-5", "complex"),
    ("cat a.py | uniq -d", "complex"),
    ("cat a.py | wc", "complex"),
    ("pytest | wc -l", "complex"),
]


@pytest.mark.parametrize("command,expected", CLASSIFY)
def test_classify(command, expected):
    assert classify(command)["class"] == expected


def test_classify_fields():
    c = classify('python -m pytest -x -k "a or b" tests/t.py::T::test_x -q')
    assert c["python"] == "python"
    assert c["selectors"] == ["tests/t.py::T::test_x"]
    assert c["extra_args"] == ["-x", "-k", "a or b", "-q"]
    assert classify("tail -n +5 a.py")["range"] == ("from", 5)
    assert classify("sed -n '3,$p' a")["range"] == ("lines", 3, None)
    g = classify("grep -rniF 'x(' --include=*.py src")
    assert (g["pattern"], g["path"], g["include"], g["literal"],
            g["ignore_case"]) == ("x(", "src", "*.py", True, True)


def test_shell_pytest_maps_failures_to_keys(mem, repo):
    res = _call(mem, "shell", command=f"{sys.executable} -m pytest tests/test_mod.py")
    assert res["handled"] and not res["is_error"], res
    out = res["output"]
    assert "FAILED tests/test_mod.py::test_broken" in out
    assert ("[ozagia] pytest failed: exit 1" in out
            and "1 passed, 1 failed, 0 error, 0 skipped" in out)
    assert ("[ozagia] failed test:tests/test_mod.py::test_broken: "
            "assert 3 == 5") in out
    assert "[ozagia]   frame test:tests/test_mod.py::test_broken tests/test_mod.py:9" in out
    assert res["meta"]["intent"] == "shell_pytest"
    assert "test:tests/test_mod.py::test_broken" in res["meta"]["keys"]


def test_shell_pytest_selector_relative_to_workdir(mem, repo):
    res = _call(mem, "shell", command=f"{sys.executable} -m pytest test_mod.py::test_helper",
                workdir=str(repo / "tests"))
    assert res["handled"], res
    assert "pytest passed: exit 0" in res["output"]


def test_shell_reads_map_to_read_ranges(mem, repo, no_outline):
    def first(cmd: str) -> str:
        res = _call(mem, "shell", command=cmd)
        assert res["handled"] and res["meta"]["intent"] == "shell_read", res
        return res["output"].split("\n")[0]
    p = repo / "pkg/mod.py"
    assert first("cat pkg/mod.py") == f"Read file {p}, lines 1-20"
    assert first("head -n 3 pkg/mod.py") == f"Read file {p}, lines 1-3 of 20"
    assert first("head pkg/mod.py") == f"Read file {p}, lines 1-10 of 20"
    assert first("tail -n 4 pkg/mod.py") == f"Read file {p}, lines 17-20 of 20"
    assert first("tail -n +18 pkg/mod.py") == f"Read file {p}, lines 18-20 of 20"
    assert first("sed -n '5,7p' pkg/mod.py") == f"Read file {p}, lines 5-7 of 20"
    assert first("sed -n '19,$p' pkg/mod.py") == f"Read file {p}, lines 19-20 of 20"
    big = _call(mem, "shell", command="cat pkg/big.py")["output"]
    assert big.split("\n")[0].endswith("lines 1-2100")
    assert _call(mem, "shell", command="cat ../x")["handled"] is False
    assert _call(mem, "shell", command="wc -l pkg/mod.py")["handled"] is False
    wrapped = _call(mem, "shell", command="cd pkg && head -n 2 mod.py")
    assert wrapped["handled"] and wrapped["meta"]["intent"] == "shell_read"
    assert str(repo / "pkg" / "mod.py") in wrapped["output"].splitlines()[0]


def test_shell_grep_and_passthrough(mem, repo):
    res = _call(mem, "shell", command="grep -rn helper pkg")
    assert res["meta"]["intent"] == "shell_grep"
    assert "[ozagia] sym:pkg/use.py::run" in res["output"]
    assert str(repo / "tests") not in res["output"]
    res = _call(mem, "shell", command="grep -rni HELPER tests")
    assert "Line 4: def test_helper():" in res["output"]
    ls = _call(mem, "shell", command="ls pkg")
    assert ls["handled"] and ls["meta"]["intent"] == "shell_ls"
    (repo / "pkg" / ".hidden.py").write_text("hidden\n")
    ls = _call(mem, "shell", command="ls pkg")
    assert ".hidden.py" not in ls["output"]
    found = _call(mem, "shell", command="find pkg -type f -name '*.py'")
    assert found["handled"] and found["meta"]["intent"] == "shell_find"
    assert "pkg/mod.py" in found["output"]
    assert "\\n" not in found["output"]
    subdir = _call(mem, "shell", command="find . -type f -name '*.py'",
                   workdir="pkg")
    assert subdir["handled"] and "./mod.py" in subdir["output"]
    listed = _call(mem, "shell", command="ls .", workdir="pkg")
    assert listed["handled"] and "mod.py" in listed["output"]
    assert _call(mem, "shell", command="ls pkg/mod.py")["handled"] is False
    assert _call(mem, "shell", command="cd missing && ls")["handled"] is False
    link = repo / "pkg" / "alias.py"
    link.symlink_to(repo / "pkg" / "mod.py")
    symlink_search = _call(mem, "shell", command="find pkg -type f -name '*.py'")
    assert symlink_search["handled"] and "alias.py" not in symlink_search["output"]
    root_link = repo / "pkg-link"
    root_link.symlink_to(repo / "pkg", target_is_directory=True)
    assert _call(mem, "shell", command="find pkg-link -type f -name '*.py'")["handled"] is False


def test_explicit_definition_grep_appends_fresh_structural_lookup(mem, repo):
    result = _call(mem, "shell", command="grep -rn 'def helper' pkg")
    assert result["handled"] and result["meta"]["intent"] == "shell_grep"
    output = result["output"]
    assert f"{repo}/pkg/mod.py:" in output
    assert "  Line 5: def helper(a, b=1):" in output
    assert "[ozagia] structural definitions (fresh snapshot;" in output
    assert "sym:pkg/mod.py::helper" in output
    assert "tag cache refresh:" in output
    assert "read a definition by passing its sym: key to read" in output
    assert "sym:pkg/mod.py::helper" in result["meta"]["keys"]
    assert result["permission"]["resources"] == ["grep -rn 'def helper' pkg"]


def test_bre_definition_search_uses_only_paths_with_grep_matches(mem, repo, tmp_path):
    _write(repo / "other.py", "def helper():\n    return 99\n")
    _git(repo, "add", "other.py")
    _git(repo, "commit", "-q", "-m", "add duplicate symbol")
    build_index(repo, mem.dump, include_docs="features", include_git=False)
    scoped = DevMemory(mem.dump, tmp_path / "state" / "journal-2.jsonl", root=repo)

    result = _call(scoped, "shell", command="grep -rn '^def \\|^class ' pkg")
    assert "[ozagia] structural definitions (fresh snapshot;" in result["output"]
    assert "sym:pkg/mod.py::helper" in result["output"]
    assert "other.py::helper" not in result["output"]
    assert str(repo / "other.py") not in result["output"]


def test_definition_grep_keeps_shell_result_and_skips_stale_graph(mem, repo):
    (repo / "pkg" / "mod.py").write_text(MOD + "# changed after index\n")
    result = _call(mem, "shell", command="grep -rn 'def helper' pkg")
    assert "  Line 5: def helper(a, b=1):" in result["output"]
    assert "structural lookup unavailable: graph freshness is stale" in result["output"]


def test_structural_query_routes_to_existing_fresh_graph_services(mem):
    from ozagia.intent.structural import query

    definitions = query(mem, "resolve", "helper")
    assert definitions["ok"] and definitions["freshness"] == "fresh"
    assert {row["id"] for row in definitions["results"]} == {
        "pkg/mod.py::helper"}
    assert definitions["coverage"].startswith(
        "graph snapshot: fresh; tag cache refresh:")
    assert definitions["resources"] == ["pkg/mod.py"]

    callers = query(mem, "callers", "helper")
    assert callers["ok"]
    assert any(row["id"] == "pkg/use.py::run" for row in callers["results"])

    tests = query(mem, "tests", "helper")
    assert tests["ok"]
    assert any(row["file"] == "tests/test_mod.py" for row in tests["results"])
    assert tests["resources"] == ["tests/test_mod.py"]

    outline = query(mem, "outline", "pkg/mod.py", budget_tokens=100)
    assert outline["ok"] and outline["text"]
    assert "def helper" in outline["text"]
    assert outline["resources"] == ["pkg/mod.py"]
    assert "snapshot nodes" in outline["coverage"]


def test_structural_query_reports_warm_cache_reuse_not_missing_coverage(
        mem, monkeypatch):
    from ozagia.intent.structural import query

    original = mem.symbol_graph
    observed_stats = []

    def record_stats(*args, **kwargs):
        result = original(*args, **kwargs)
        observed_stats.append(result["tag_cache"])
        return result

    monkeypatch.setattr(mem, "symbol_graph", record_stats)
    cold = query(mem, "callers", "helper")
    warm = query(mem, "callers", "helper")

    assert cold["ok"] and warm["ok"]
    assert any(row["id"] == "pkg/use.py::run" for row in warm["results"])
    cold_stats, warm_stats = observed_stats
    assert cold_stats["parsed"] == cold_stats["files"]
    assert warm_stats["parsed"] == 0
    assert warm_stats["reused"] == warm_stats["files"]
    assert warm_stats["failed"] == 0
    assert warm_stats["cached_files"] >= warm_stats["files"]
    assert "0 parsed" in warm["coverage"]
    assert f"{warm_stats['reused']} reused" in warm["coverage"]
    assert f"{warm_stats['failed']} failed" in warm["coverage"]
    assert "cached entries" in warm["coverage"]
    assert "0/" not in warm["coverage"]


def test_structural_outline_withholds_text_without_safe_citation(mem, monkeypatch):
    from ozagia.intent import structural

    monkeypatch.setattr(mem, "outline_target", lambda _target: {
        "ok": True, "path": "../outside.py", "text": "private source"})
    result = structural.query(mem, "outline", "pkg/mod.py")
    assert result["ok"] is False
    assert result["resources"] == []
    assert result["text"] == ""
    assert "no safe in-checkout citation" in result["error"]


def test_shell_ls_refuses_truncated_or_unreadable_directory(mem, repo, monkeypatch):
    from ozagia.intent import run
    import os

    for n in range(2001):
        (repo / "pkg" / f"f{n:04}.txt").touch()
    assert _call(mem, "shell", command="ls pkg")["handled"] is False

    original = os.scandir
    def denied(path):
        if Path(path) == repo / "pkg":
            raise PermissionError("denied")
        return original(path)
    monkeypatch.setattr(run.os, "scandir", denied)
    assert _call(mem, "shell", command="ls pkg")["handled"] is False


def test_shell_find_permission_errors_fall_back(mem, repo, monkeypatch):
    from ozagia.intent import shellfs

    def denied_scandir(path):
        raise PermissionError(str(path))
    monkeypatch.setattr(shellfs.os, "scandir", denied_scandir)
    assert _call(mem, "shell", command="find pkg -type f -name '*.py'")["handled"] is False
    for cmd in ("git status", "python -c 'print(1)'", "pwd && ls"):
        assert _call(mem, "shell", command=cmd)["handled"] is False, cmd


def test_shell_refuses_ref_moving_git(mem, repo):
    head = _git(repo, "rev-parse", "HEAD")
    for cmd in ("git commit -am x", "git reset --hard HEAD~1",
                "git add . && git commit -m x"):
        res = _call(mem, "shell", command=cmd)
        assert res["handled"] and res["is_error"]
        assert res["output"] == f"Refused by ozagia: {cmd} changes git refs"
        assert res["meta"]["intent"] == "shell_refused"
    assert _git(repo, "rev-parse", "HEAD") == head


# --------------------------------------------------------------------------- #
# after
# --------------------------------------------------------------------------- #
def test_after_refreshes_out_of_band_changes(mem, repo):
    (repo / "pkg/mod.py").write_text(MOD + "\n\ndef added():\n    return 2\n")
    res = I.after(mem, "shell", {"command": "python gen.py"}, "", harness="opencode",
                  session="s")
    assert "+sym:pkg/mod.py::added" in res["append"]
    assert res["append"].startswith("[ozagia] delta: 1 added")
    assert res["meta"]["intent"] == "after_refresh"
    assert mem.node_by_key("sym:pkg/mod.py::added") is not None
    # Nothing left: a second refresh appends nothing.
    assert I.after(mem, "shell", {"command": "true"}, "", harness="opencode",
                   session="s")["append"] == ""


def test_after_read_like_calls_do_not_refresh(mem, repo):
    (repo / "pkg/mod.py").write_text(MOD + "\n\ndef added():\n    return 2\n")
    for tool, inp in (("read", {"path": "pkg/mod.py"}),
                      ("shell", {"command": "cat pkg/mod.py"})):
        res = I.after(mem, tool, inp, "", harness="opencode", session=None)
        assert res["append"] == "" and res["meta"]["intent"] == "after_none"
    assert mem.node_by_key("sym:pkg/mod.py::added") is None


def test_after_defers_large_refreshes(mem, repo, monkeypatch):
    monkeypatch.setattr(I, "MAX_REFRESH_FILES", 0)
    (repo / "pkg/use.py").write_text(USE + "\nX = 1\n")
    res = I.after(mem, "edit", {"path": "pkg/use.py"}, "", harness="opencode",
                  session=None)
    assert res["append"] == "[ozagia] many files changed; graph refresh deferred"


def test_after_git_diff_names_touched_keys(mem, repo):
    text = (repo / "pkg/mod.py").read_text()
    (repo / "pkg/mod.py").write_text(text.replace('    """Help."""\n', "")
                                     .replace("        return a\n",
                                              "        return a * 3\n"))
    (repo / "deploy.yaml").write_text(YAML.replace("prod", "staging"))
    diff = _git(repo, "diff")
    res = I.after(mem, "shell", {"command": "git diff"}, diff, harness="opencode",
                  session=None)
    first = res["append"].split("\n")[0]
    assert first.startswith("[ozagia] touched: ")
    touched = first[len("[ozagia] touched: "):].split(", ")
    assert touched == ["text:deploy.yaml#L7-L8", "sym:pkg/mod.py::helper",
                       "sym:pkg/mod.py::Box.m"]
    assert res["meta"]["intent"] == "after_git_touched"


def test_handler_exception_falls_back(mem, monkeypatch):
    def boom(memory, inp):
        raise RuntimeError("kaput")
    monkeypatch.setitem(I.HANDLERS, "read", boom)
    res = _call(mem, "read", path="pkg/mod.py")
    assert res["handled"] is False
    assert res["reason"] == "RuntimeError: kaput"
    assert res["meta"]["intent"] == "read_exception"


def test_after_exception_is_reported_not_raised(mem, monkeypatch):
    def boom():
        raise RuntimeError("kaput")
    monkeypatch.setattr(mem, "pending_changes", boom)
    res = I.after(mem, "write", {}, "", harness="opencode", session=None)
    assert res["append"] == "" and "kaput" in res["meta"]["error"]



@pytest.mark.parametrize("command,filt", [
    ("python -m pytest tests/test_mod.py::test_helper -q 2>&1 | tail -20", ("tail", 20)),
    ("pytest -q | head -5", ("head", 5)),
    ("pytest tests 2>&1", ("all", 0)),
])
def test_pytest_with_an_output_filter_is_still_pytest(command, filt):
    from ozagia.intent.shell import classify
    got = classify(command)
    assert got["class"] == "pytest" and got["output_filter"] == filt


@pytest.mark.parametrize("command", ["pytest > out.txt", "pytest | wc -l",
                                     "cat a | tee x", "pytest &"])
def test_other_pipelines_stay_complex(command):
    from ozagia.intent.shell import classify
    assert classify(command)["class"] == "complex"


def test_pytest_tail_filter_applies_to_output(mem):
    import sys
    res = _call(mem, "shell",
                command=f"{sys.executable} -m pytest tests/test_mod.py -q 2>&1 | tail -3")
    assert res["handled"], res
    head = res["output"].split("[ozagia] pytest")[0].rstrip("\n").splitlines()
    assert len(head) <= 3
    assert "[ozagia] pytest " in res["output"]


def test_grep_case_sensitive_false_ignores_case(mem):
    res = _call(mem, "grep", pattern="HELPER", caseSensitive=False)
    assert res["handled"] and "Found" in res["output"]
    assert "No files found" in _call(mem, "grep", pattern="HELPER")["output"]


# --------------------------------------------------------------------------- #
# review fixes 1d and 11
# --------------------------------------------------------------------------- #
def _broken_journal(*a, **k):
    raise OSError("disk full")


def test_journal_failure_after_an_edit_is_a_warning_not_a_fallback(mem, repo, monkeypatch):
    monkeypatch.setattr(mem, "journal_append", _broken_journal)
    res = _call(mem, "edit", path="pkg/mod.py", oldString="return a + b",
                newString="return a + b + 0")
    assert res["handled"] is True and res["is_error"] is False, res
    assert "[ozagia] warning: journal not written: OSError: disk full" in res["output"]
    assert (repo / "pkg/mod.py").read_text().count("return a + b + 0") == 1


def test_journal_failure_after_a_write_is_a_warning_not_a_fallback(mem, repo, monkeypatch):
    monkeypatch.setattr(mem, "journal_append", _broken_journal)
    res = _call(mem, "write", path="pkg/new.py", content="X = 1\n")
    assert res["handled"] is True and res["is_error"] is False, res
    assert res["output"].startswith("Wrote file successfully.")
    assert "[ozagia] warning: journal not written" in res["output"]

    def boom(paths):
        raise RuntimeError("reingest broke")
    monkeypatch.setattr(mem, "reingest", boom)
    res = _call(mem, "write", path="pkg/new2.py", content="Y = 1\n")
    assert res["handled"] is True and "graph update failed" in res["output"]


@pytest.mark.parametrize("path", [".git/HEAD", ".git/refs/heads/main", "sub/.git/config"])
def test_edits_and_writes_into_git_are_refused(mem, repo, path):
    head = (repo / ".git" / "HEAD").read_text()
    (repo / "sub" / ".git").mkdir(parents=True, exist_ok=True)
    (repo / "sub" / ".git" / "config").write_text("x\n")
    res = _call(mem, "write", path=path, content="ref: refs/heads/evil\n")
    assert res["handled"] and res["is_error"] and "inside .git" in res["output"]
    res = _call(mem, "edit", path=".git/HEAD", oldString="ref:", newString="ref: ")
    assert res["handled"] and res["is_error"] and "inside .git" in res["output"]
    assert (repo / ".git" / "HEAD").read_text() == head


def test_span_edit_refuses_git_paths(mem, repo):
    from ozagia.edit.code_edit import CodeEditError
    with pytest.raises(CodeEditError, match="inside .git"):
        mem.span_edit(".git/HEAD", "ref:", "ref: ", apply=True)


def test_ref_moving_list_is_shared_with_the_opencode_deny_list():
    from ozagia.eval.agent import OPENCODE_GIT_DENY
    from ozagia.integrations.claude_code.hook import REF_MOVING_GIT
    from ozagia.intent.shell import GIT_REF_MOVING
    assert REF_MOVING_GIT is GIT_REF_MOVING
    assert OPENCODE_GIT_DENY[:11] == (
        "git commit*", "git push*", "git stash*", "git branch*", "git checkout*",
        "git switch*", "git reset*", "git rebase*", "git merge*", "git tag*",
        "git worktree*")
    assert {f"git {s}*" for s in GIT_REF_MOVING} == set(OPENCODE_GIT_DENY)
