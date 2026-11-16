"""W2 shell capture: several grep paths, context, rg globs and types,
`rg --files`, `nl`, `tail -c`, harmless environment prefixes, `timeout`,
subshells, `cd ~`, the wc/sort/uniq/cut filters and the wider find subset.

Each behaviour test runs the captured command and the real one (bash on the
same tree) and compares the text without `[ozagia]` lines and the exit
status. A case whose tool is not installed is skipped."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ozagia.core.memory import DevMemory, build_index
from ozagia.intent.shell import classify

import test_intent as _ti

_call = _ti._call
layout = _ti.layout
repo = _ti.repo

CTX = "".join(f"{'foo ' if i in (1, 5, 7, 14, 18) else ''}line {i}\n"
              for i in range(1, 21))
NUMS = "10\n9\n 3\n-1\nabc\n\n1.5\n1.50\n+2\n1e3\n0x1\n-0\n0\nb\n\t4\nB\na\n"
WORDS = "a\u00a0b c\u2003d\x1ce\n  two  words \n"


@pytest.fixture
def tree(repo: Path) -> Path:
    _ti._write(repo / "ctx.txt", CTX)
    _ti._write(repo / "other.txt", "foo one\nfoo two\nthree\n")
    _ti._write(repo / "nums.txt", NUMS)
    _ti._write(repo / "words.txt", WORDS)
    _ti._write(repo / "pairs.txt", "a:b:c\nnodelim\nx:y\na:b:c\na:b:c\nq\n")
    (repo / "nonl.txt").write_bytes(b"one\ntwo\nthree")
    _ti._write(repo / ".gitignore", "ignored/\n*.log\n")
    _ti._write(repo / "ignored" / "x.py", "foo\n")
    _ti._write(repo / "app.log", "foo\n")
    _ti._write(repo / ".cfg" / "h.py", "foo\n")
    _ti._write(repo / "pkg" / "sub" / "deep.py", "foo = 1\n")
    _ti._write(repo / "nl.txt", "a\n\nb\n  \nc")
    return repo


@pytest.fixture
def mem(tree: Path, tmp_path: Path) -> DevMemory:
    dump = tmp_path / "state" / "graph.json"
    build_index(tree, dump, include_docs="features", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=tree)


def _bash(cwd: Path, command: str, env: dict[str, str] | None = None
          ) -> tuple[int, str]:
    p = subprocess.run(["bash", "-c", command], cwd=cwd, env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout.rstrip("\n")


def _status(out: str) -> int:
    m = re.search(r"^\[ozagia\] exit (\d+)$", out, re.M)
    return int(m.group(1)) if m else 0


def _plain(out: str) -> str:
    return "\n".join(line for line in out.splitlines()
                     if not line.startswith("[ozagia] "))


def _body(out: str) -> str:
    """A read's text: without the `Read file ...` line and graph lines."""
    lines = [line for line in out.splitlines() if not line.startswith("[ozagia] ")]
    assert lines and lines[0].startswith("Read file "), out
    return "\n".join(lines[1:])


def _need(*tools: str) -> None:
    for t in tools:
        if shutil.which(t) is None:
            pytest.skip(f"{t} is not installed")


def _same(mem, tree: Path, command: str, **inp) -> dict:
    res = _call(mem, "shell", command=command, **inp)
    assert res["handled"], res
    env = inp.get("_test_env")
    code, text = _bash(tree, command, env)
    assert _plain(res["output"]) == text, (command, res["output"])
    assert _status(res["output"]) == code, (command, res["output"])
    return res


# --------------------------------------------------------------------------- #
# grep and rg: several paths, context, globs and types
# --------------------------------------------------------------------------- #
# `| cut -f1-` passes every line unchanged and makes the step print the
# command's own text (a transform filter), so separators and prefixes are
# compared exactly. Several files are sorted: rg's order is that of its
# parallel walk and grep's that of readdir, and the capture's is by path.
@pytest.mark.parametrize("command", [
    "rg -n foo ctx.txt | cut -f1-",
    "rg foo ctx.txt | cut -f1-",
    "rg -n -C1 foo ctx.txt | cut -f1-",
    "rg -n -A2 foo ctx.txt | cut -f1-",
    "rg -B 3 foo ctx.txt | cut -f1-",
    "rg -A0 foo ctx.txt | cut -f1-",
    "rg -C1 -A0 foo ctx.txt | cut -f1-",
    "rg -A0 -C1 foo ctx.txt | cut -f1-",
    "rg -n --context=2 foo ctx.txt | cut -f1-",
    "grep -n -C1 foo ctx.txt | cut -f1-",
    "grep -A0 foo ctx.txt | cut -f1-",
    "grep -2 foo ctx.txt | cut -f1-",
    "grep -nB2 foo ctx.txt | cut -f1-",
    "grep -C1 -A0 foo ctx.txt | cut -f1-",
    "rg -n -C1 foo ctx.txt other.txt | wc -l",
    "grep -n -C1 foo ctx.txt other.txt | wc -l",
    "rg -n foo ctx.txt other.txt | LC_ALL=C sort",
    "rg foo ctx.txt other.txt pkg | LC_ALL=C sort",
    "rg -H foo ctx.txt | cut -f1-",
    "rg -n helper pkg tests | LC_ALL=C sort",
    "rg -n helper pkg/ | LC_ALL=C sort",
    "rg -n helper ./pkg | LC_ALL=C sort",
    "rg foo | LC_ALL=C sort",
    "rg foo . | LC_ALL=C sort",
    "grep -rn helper pkg tests | LC_ALL=C sort",
    "grep -n helper pkg/use.py pkg/mod.py | LC_ALL=C sort",
    "grep -rn helper pkg/mod.py | cut -f1-",
    "rg -n -g '*.py' -g '!use.py' helper | LC_ALL=C sort",
    "rg -n --glob='!*.py' foo | LC_ALL=C sort",
    "rg -t py helper | LC_ALL=C sort",
    "rg -T py foo | LC_ALL=C sort",
    "rg -n foo ctx.txt | cut -d: -f1 | uniq -c",
    "rg -c foo ctx.txt || echo unmapped-but-same",
])
def test_grep_text_matches_the_command(mem, tree, command):
    _need("rg")
    if "-c foo" in command:
        assert classify(command)["class"] != "compound"
        return
    _same(mem, tree, command)


@pytest.mark.parametrize("command", [
    "rg -n foo ctx.txt other.txt",
    "rg -n -C1 foo ctx.txt other.txt",
    "grep -n -A1 foo ctx.txt other.txt",
    "rg -n -g '*.txt' foo",
])
def test_grep_with_context_and_several_paths_keeps_graph_lines(mem, tree, command):
    _need("rg")
    res = _call(mem, "shell", command=command)
    assert res["handled"] and res["meta"]["intent"] == "shell_grep", res
    # The same match and context lines as the command, in the grep format.
    real = _bash(tree, command.replace("rg ", "rg -H --no-heading ", 1)
                 if command.startswith("rg") else command)[1].splitlines()
    want = set()
    for line in real:
        if line == "--":
            continue
        m = re.match(r"^(.+?)([:-])(\d+)[:-](.*)$", line)
        want.add((str(tree / m.group(1)), int(m.group(3)), m.group(2), m.group(4)))
    got = set()
    current = None
    for line in res["output"].splitlines():
        if line.endswith(":") and line.startswith("/"):
            current = line[:-1]
        m = re.match(r"^  Line (\d+)([:-]) (.*)$", line)
        if m:
            got.add((current, int(m.group(1)), m.group(2), m.group(3)))
    assert got == want
    if "-C1" in command:
        assert "  --" in res["output"].splitlines()


def test_grep_separator_only_inside_a_file_in_the_grep_format(mem, tree):
    _need("rg")
    out = _call(mem, "shell", command="rg -n -C1 foo ctx.txt other.txt")["output"]
    files = out.split("\n\n")
    assert len(files) == 2
    for block in files:
        lines = block.splitlines()
        assert lines[-1] != "  --" and not lines[0].startswith("  --")


def test_grep_status_and_refusals(mem, tree):
    _need("rg")
    assert _status(_call(mem, "shell", command="rg zzz ctx.txt other.txt; echo")["output"]) == 0
    _same(mem, tree, "rg zzz ctx.txt other.txt | wc -l")
    res = _call(mem, "shell", command="rg zzz ctx.txt other.txt && echo yes")
    assert _status(res["output"]) == 1 and "yes" not in res["output"]
    for command, why in [
        ("rg foo ctx.txt ctx.txt", "overlapping paths are not mapped"),
        ("rg foo pkg pkg/mod.py", "overlapping paths are not mapped"),
        ("grep -rn --include=*.py foo ctx.txt", "grep --include with a file operand"),
        ("grep -n foo pkg", "grep of a directory without -r"),
        ("rg foo nosuch.txt", "no such path"),
        ("rg foo /etc", "outside the project root"),
    ]:
        res = _call(mem, "shell", command=command)
        assert res["handled"] is False and why in res["reason"], (command, res)


def test_rg_search_skips_hidden_and_ignored_files_like_rg(mem, tree):
    _need("rg")
    out = _call(mem, "shell", command="rg -n foo")["output"]
    assert str(tree / ".cfg") not in out and "app.log" not in out
    assert "ignored/x.py" not in out
    assert str(tree / "ctx.txt") in out


# --------------------------------------------------------------------------- #
# rg --files
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", [
    "rg --files | LC_ALL=C sort",
    "rg --files pkg tests | LC_ALL=C sort",
    "rg --files -g '*.py' | LC_ALL=C sort",
    "rg --files -g '!*.py' -g '!*.txt' | LC_ALL=C sort",
    "rg --files -t py pkg | LC_ALL=C sort",
    "rg --files --hidden | LC_ALL=C sort",
    "rg --files --no-ignore | LC_ALL=C sort",
    "rg --files -uu . | LC_ALL=C sort",
    "rg --files | wc -l",
    "rg --files pkg/ | grep sub",
    "rg --files -t nosuchtype; echo after",
])
def test_rg_files_matches_rg(mem, tree, command):
    _need("rg")
    _same(mem, tree, command)


def test_rg_files_alone_is_rg_sorted_by_path(mem, tree):
    _need("rg")
    res = _call(mem, "shell", command="rg --files pkg")
    assert res["handled"] and res["meta"]["intent"] == "shell_files", res
    real = _bash(tree, "rg --files pkg")[1].splitlines()
    got = _plain(res["output"]).splitlines()
    assert got == sorted(real)
    res = _call(mem, "shell", command="cd pkg && rg --files -g '*.py'")
    assert res["handled"] and "sub/deep.py" in res["output"]
    assert _call(mem, "shell", command="rg --files nosuch")["handled"] is False


# --------------------------------------------------------------------------- #
# nl and tail -c
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", [
    "nl -ba pkg/mod.py", "nl -b a nl.txt", "nl nl.txt", "nl -ba nonl.txt",
    "nl --body-numbering=t pkg/mod.py",
])
def test_nl_reads_number_lines_as_nl(mem, tree, command):
    _need("nl")
    res = _call(mem, "shell", command=command)
    assert res["handled"] and res["meta"]["intent"] == "shell_read", res
    assert _body(res["output"]) == _bash(tree, command)[1]


@pytest.mark.parametrize("command", [
    "nl -ba pkg/mod.py | sed -n '3,5p'",
    "nl -ba pkg/mod.py | head -4",
    "nl -ba pkg/mod.py | tail -n 3",
])
def test_nl_ranges_are_ranged_reads(mem, tree, command):
    _need("nl")
    res = _call(mem, "shell", command=command)
    assert res["handled"], res
    assert _body(res["output"]) == _bash(tree, command)[1]
    assert "[ozagia] " in res["output"]


@pytest.mark.parametrize("command", [
    "nl -ba pkg/mod.py | grep def",
    "nl nl.txt | cut -f1-",
    "nl -ba nl.txt | wc -l",
    "nl -ba nonl.txt | wc -c",
    "nl -ba pkg/mod.py | grep class | cut -f1",
])
def test_nl_pipelines_match_nl(mem, tree, command):
    _need("nl")
    _same(mem, tree, command)


def test_nl_refuses_what_nl_prints_differently(mem, tree):
    (tree / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    (tree / "bom.txt").write_bytes(b"\xef\xbb\xbfa\n")
    _ti._write(tree / "sect.txt", "a\n\\:\\:\nb\n")
    for name, why in [("crlf.txt", "carriage returns"), ("bom.txt", "byte order"),
                      ("sect.txt", "section delimiter")]:
        res = _call(mem, "shell", command=f"nl -ba {name}")
        assert res["handled"] is False and why in res["reason"], res


@pytest.mark.parametrize("command", [
    "tail -c 20 pkg/mod.py", "tail -c 5 nonl.txt", "tail -c +3 nonl.txt",
    "tail --bytes=1 ctx.txt", "tail -c 100000 pkg/use.py",
])
def test_tail_bytes(mem, tree, command):
    res = _call(mem, "shell", command=command)
    assert res["handled"] and res["meta"]["intent"] == "shell_read", res
    assert _body(res["output"]) == _bash(tree, command)[1]


@pytest.mark.parametrize("command", [
    "tail -c 7 ctx.txt | wc -c", "tail -c +4 nonl.txt | wc -l",
    "tail -c 30 ctx.txt | head -1", "tail -c 30 ctx.txt | grep foo",
])
def test_tail_bytes_in_pipelines(mem, tree, command):
    _same(mem, tree, command)


def test_tail_bytes_inside_a_character_falls_back(mem, tree):
    _ti._write(tree / "u.txt", "h\u00e9\n")
    res = _call(mem, "shell", command="tail -c 2 u.txt")
    assert res["handled"] is False and "UTF-8" in res["reason"]


# --------------------------------------------------------------------------- #
# environment prefixes and timeout
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", [
    "LC_ALL=C rg -n helper pkg/mod.py | cut -f1-",
    "NO_COLOR=1 TERM=dumb cat pkg/use.py | wc -l",
    "LANG=C nl -ba pkg/use.py | head -2 | cut -f1-",
    "COLUMNS=40 git diff --stat HEAD; echo x",
    "GIT_PAGER=cat PAGER=less git log --oneline -1 | wc -l",
    "LC_ALL=C ls pkg && cat pkg/use.py | wc -l",
    "LC_ALL=C find pkg -name '*.py' | LC_ALL=C sort",
    "timeout 5 git log --oneline -1 | wc -l",
    "timeout -s KILL 10 rg -n helper pkg/mod.py | cut -f1-",
    "timeout 1m cat pkg/use.py | tail -n 2 | cut -f1-",
    "timeout 5 find pkg -type d && echo done",
    "timeout 5 rg --files pkg | LC_ALL=C sort",
])
def test_harmless_env_and_timeout(mem, tree, command):
    _need("rg")
    _same(mem, tree, command)


def test_env_and_timeout_on_simple_commands(mem, tree):
    _need("rg")
    for command in ("LC_ALL=C rg helper pkg", "timeout 5 rg helper pkg",
                    "NO_COLOR=1 cat pkg/use.py", "timeout 5 nl -ba pkg/use.py",
                    "LANG=C find pkg -name '*.py'"):
        res = _call(mem, "shell", command=command)
        assert res["handled"], (command, res)
    res = _call(mem, "shell", command="timeout 5 nl -ba pkg/use.py")
    assert _body(res["output"]) == _bash(tree, "nl -ba pkg/use.py")[1]


def test_git_step_gets_the_prefix_environment(mem, tree):
    res = _call(mem, "shell", command="GIT_PAGER=cat COLUMNS=30 git diff --stat "
                                      "HEAD~0; echo x")
    assert res["handled"], res


# --------------------------------------------------------------------------- #
# subshells and cd ~
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", [
    "(cd pkg && rg -n helper mod.py | cut -f1-)",
    "(cd pkg; cat use.py | wc -l) && pwd",
    "(cd pkg && grep -n nothing mod.py | wc -l) || echo none",
    "(cd pkg && rg -n helper mod.py | grep nothing) || echo none",
    "(cd pkg && rg -n helper mod.py | grep nothing); echo after",
    "cd pkg && (cd ../tests && pwd) && pwd && cat use.py | wc -l",
    "( cd pkg && git status --short ) && ( cd tests && git log --oneline -1 | wc -l )",
    "echo a && (cd pkg && cat use.py | head -1 | cut -f1-) && echo b",
    "rg nothing ctx.txt | cut -f1- && (cd pkg && pwd)",
    "cat ctx.txt | grep nothing || (cd pkg && pwd) && echo c",
    "cat ctx.txt | grep nothing && (cd pkg && pwd) || echo d",
])
def test_subshells_match_bash(mem, tree, command):
    _need("rg")
    _same(mem, tree, command)


def test_cd_home_inside_the_root(mem, tree):
    env = {**os.environ, "HOME": str(tree)}
    for command in ("cd ~ && cat pkg/use.py | wc -l",
                    "cd ~/pkg && pwd && cat use.py | head -1 | cut -f1-",
                    "(cd ~/tests && pwd); cat pkg/use.py | wc -l"):
        _same(mem, tree, command, _test_env=env)
    res = _call(mem, "shell", command="cd ~ && cat x",
                _test_env={**os.environ, "HOME": "/"})
    assert res["handled"] is False and "outside the project root" in res["reason"]


# --------------------------------------------------------------------------- #
# wc, sort, uniq, cut
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", [
    "cat nums.txt | LC_ALL=C sort",
    "cat nums.txt | LC_ALL=C sort -n",
    "cat nums.txt | LC_ALL=C sort -rn",
    "cat nums.txt | LC_ALL=C sort -nu",
    "cat nums.txt | LC_ALL=C sort -u -r -n",
    "cat nums.txt | LC_ALL=C sort -u",
    "cat nums.txt | LC_ALL=C sort -r",
    "cat nums.txt | LC_ALL=C sort --numeric-sort --reverse",
    "cat pairs.txt | LC_ALL=C sort | uniq -c",
    "cat pairs.txt | uniq",
    "cat pairs.txt | uniq --count | LC_ALL=C sort -rn",
    "cat pairs.txt | cut -d: -f2-",
    "cat pairs.txt | cut -d : -f 3,1",
    "cat pairs.txt | cut -d: -f5",
    "cat pairs.txt | cut -s -d: -f1",
    "cat pairs.txt | cut --delimiter=: --fields=-2",
    "cat nonl.txt | wc -l",
    "cat nonl.txt | wc -c",
    "cat nonl.txt | wc -w",
    "head -n 2 nonl.txt | wc -c",
    "sed -n '3p' nonl.txt | wc -c",
    "tail -n 1 nonl.txt | wc -l",
    "cat nonl.txt | grep t | wc -c",
    "cat words.txt | wc -w",
    "cat words.txt | LC_ALL=C wc -w",
    "cat words.txt | LC_ALL=C.UTF-8 wc -w",
    "cat ctx.txt | wc -l && cat ctx.txt | wc --bytes",
    "git log --oneline | wc -l",
    "cat README.md | LC_ALL=C sort | head -2",
])
def test_transform_filters_match_coreutils(mem, tree, command):
    _same(mem, tree, command)


def test_sort_outside_byte_order_falls_back(mem, tree):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LC_")}
    env["LANG"] = "en_US.UTF-8"
    res = _call(mem, "shell", command="cat nums.txt | sort", _test_env=env)
    assert res["handled"] is False and "sort under LC_COLLATE=en_US.UTF-8" in res["reason"]
    env["LANG"] = "C.UTF-8"
    assert _call(mem, "shell", command="cat nums.txt | sort", _test_env=env)["handled"]


# --------------------------------------------------------------------------- #
# find
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", [
    "find pkg",
    "find pkg/",
    "find pkg//",
    "find .",
    "find",
    "find . -maxdepth 1",
    "find . -maxdepth 0",
    "find pkg tests -type f",
    "find . -maxdepth 2 -type d",
    "find . -mindepth 2 -maxdepth 2 -name '*.py'",
    "find . -name '*.py' -not -path './tests/*'",
    "find . ! -name '*.py' -type f -path './pkg/*'",
    "find . -path ./.git -prune -o -type f -name '*.py' -print",
    "find . -path ./.git -prune -o -name '*.txt'",
    "find . \\( -name '*.md' -o -name '*.yaml' \\) -print",
    "find . -name '*.md' -o -name '*.yaml'",
    "find . -iname 'readme*'",
    "find . -name '[a-m]*.py'",
    "find . -name '[!a-m]*.py' -type f",
    "find . -name 'mod.p?'",
    "find . -name '.*'",
    "find pkg -name pkg",
    "find pkg/ -name pkg",
    "find . -type f -name '*.py' -a -path '*sub*'",
    "find . -type l",
    "find . -false -o -name 'use.py'",
    "find . -name '*.py' | wc -l",
    "find . -name '*.txt' | LC_ALL=C sort | head -3",
])
def test_find_matches_gnu_find(mem, tree, command):
    _need("find")
    _same(mem, tree, command)


def test_find_newer_and_symlinks(mem, tree):
    _need("find")
    time.sleep(0.02)
    _ti._write(tree / "pkg" / "fresh.py", "x = 1\n")
    (tree / "pkg" / "alias.py").symlink_to(tree / "pkg" / "mod.py")
    for command in ("find . -newer pkg/mod.py -type f",
                    "find pkg -type l", "find pkg -name '*.py'",
                    "find pkg -type f -name '*.py'"):
        _same(mem, tree, command)


def test_find_refusals(mem, tree):
    (tree / "pkg-link").symlink_to(tree / "pkg", target_is_directory=True)
    for command, why in [
        ("find pkg-link -name '*.py'", "symlink start path"),
        ("find nosuch -name x", "does not exist"),
        ("find /etc -name x", "outside the project root"),
        ("find . -newer /etc/hostname", "outside the project root"),
    ]:
        res = _call(mem, "shell", command=command)
        assert res["handled"] is False and why in res["reason"], (command, res)


# --------------------------------------------------------------------------- #
# the read-only lists
# --------------------------------------------------------------------------- #
def test_new_read_only_classes_do_not_refresh(mem, tree):
    from ozagia.intent import _may_change_files
    for command in ("rg --files", "nl -ba a.py", "tail -c 5 a.py",
                    "LC_ALL=C rg foo", "timeout 5 find .",
                    "(cd pkg && rg foo | wc -l)"):
        changes, _cls = _may_change_files("shell", {"command": command})
        assert changes is False, command
    assert _may_change_files("shell", {"command": "(cd x && pytest -q)"})[0]


def test_plugin_lists_the_files_intent_as_read_only():
    plugin = (Path(__file__).resolve().parents[1] / "src" / "ozagia" /
              "integrations" / "opencode" / "plugin.js").read_text()
    m = re.search(r"\[([^\]]*\"shell_read\"[^\]]*)\]", plugin)
    assert m and '"shell_files"' in m.group(1)
