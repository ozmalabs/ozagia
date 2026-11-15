"""Compound shell commands (`shell.plan_compound`, `intent.compound`), git
refusal through wrapper commands, and graph context after passthrough
compound commands."""
from __future__ import annotations

import subprocess
import sys
import threading

import pytest

from ozagia.intent import after, handle
from ozagia.intent.shell import bre_to_ere, classify, plan_compound

import test_intent as _ti

_call = _ti._call
# The fixtures of test_intent.py: a small indexed git repository.
layout = _ti.layout
repo = _ti.repo
mem = _ti.mem


# --------------------------------------------------------------------------- #
# git refusal through wrappers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command,sub", [
    ("env git commit -m x", "commit"),
    ("env -i git push", "push"),
    ("env -u HOME git push", "push"),
    ("env FOO=1 git commit", "commit"),
    ("env -S 'git push'", "push"),
    ("xargs git checkout", "checkout"),
    ("echo a | xargs -I{} git checkout {}", "checkout"),
    ("command git reset", "reset"),
    ("exec git reset --hard", "reset"),
    ("nice -n 5 git commit", "commit"),
    ("timeout -s KILL 5 git push", "push"),
    ("nohup git push", "push"),
    ("sudo -u bob git push", "push"),
    ("time -p git commit", "commit"),
    ("builtin git push", "push"),
    ("eval 'git checkout x'", "checkout"),
    ("find . -name x -exec git checkout {} \\;", "checkout"),
    ("{ git commit; }", "commit"),
    ("! git push", "push"),
    ("if true; then git push; fi", "push"),
    ("for f in a; do git checkout $f; done", "checkout"),
    ("FOO=1 nice timeout 5 env git commit", "commit"),
    ("cd x && git diff && env git stash", "stash"),
])
def test_wrapped_ref_moving_git_is_refused(command, sub):
    got = classify(command)
    assert got["class"] == "git_refs" and got["subcommand"] == sub, got


@pytest.mark.parametrize("command", [
    "command -v git", "env git status", "xargs grep foo", "nice ls",
    "timeout 10 git log", "echo git push", "find . -name '*.py'",
])
def test_wrappers_without_ref_moving_git_are_not_refused(command):
    assert classify(command)["class"] != "git_refs"


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command,kinds,ops", [
    ("cd src && python -m pytest t.py -q 2>&1 | tail -5",
     ["cd", "pytest"], ["&&"]),
    ("git diff --stat; echo ---; sed -n 1,5p a.py", ["git_read", "echo", "read"],
     [";", ";"]),
    ("grep -n x a.py | head -3 && echo found", ["grep", "echo"], ["&&"]),
    ("grep -n x a.py || echo none", ["grep", "echo"], ["||"]),
    ("pwd && git log --oneline -3", ["pwd", "git_read"], ["&&"]),
    ("git show HEAD:a.py | sed -n '3,9p'", ["git_read"], []),
    ("PYTHONPATH=lib python -m pytest t.py -q", ["pytest"], []),
    ("cd a && cd b && cat x.py\n", ["cd", "cd", "read"], ["&&", "&&"]),
])
def test_plan_compound(command, kinds, ops):
    plan, why = plan_compound(command)
    assert plan is not None, why
    assert [s["kind"] for s in plan["steps"]] == kinds
    assert plan["ops"] == ops
    assert classify(command)["class"] == "compound"


@pytest.mark.parametrize("command,why", [
    ("cat <<EOF\nx\nEOF", "heredoc"),
    ("(cd a && pytest)", "operator ( is not mapped"),
    ("pytest &", "operator & is not mapped"),
    ("pytest > out.txt", "redirect >out.txt is not mapped"),
    ("python -c 'print(1)' && cat a.py", "python command"),
    ("pwd && ls", "no pytest, read, grep or git step"),
    ("cat a.py | wc -l", "pipeline into wc is not mapped"),
    ("git diff --output=x.patch && cat a.py", "git option --output is not mapped"),
    ("git -c core.pager=x diff && cat a.py", "git option -c is not mapped"),
    ("cat a.py && cd b", "cd after another step"),
    ("FOO=1 cat a.py && echo", "environment assignment prefix"),
    ("cat $HOME/a.py && echo", "cat: substitution"),
])
def test_plan_compound_refuses(command, why):
    plan, got = plan_compound(command)
    assert plan is None
    assert got == why


def test_git_refs_wins_over_compound():
    assert classify("cat a.py && git commit -m x")["class"] == "git_refs"


@pytest.mark.parametrize("bre,ere", [
    (r"foo\|bar", "foo|bar"),
    (r"a\{2,3\}", "a{2,3}"),
    ("x+y?", r"x\+y\?"),
    ("def f(", r"def f\("),
    ("^*a", r"^\*a"),
    ("a$b", r"a\$b"),
    ("[]a]", r"[\]a]"),
    (r"a\.b", r"a\.b"),
    (r"\<w\>", None),
    ("[[:space:]]", None),
    (r"\(a\)\1", None),
])
def test_bre_to_ere(bre, ere):
    assert bre_to_ere(bre) == ere


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #
def _bash(repo, command):
    p = subprocess.run(["bash", "-c", command], cwd=repo, capture_output=True,
                       text=True)
    return p.returncode, (p.stdout + p.stderr).rstrip("\n")


def _status(out):
    import re
    m = re.search(r"^\[ozagia\] exit (\d+)$", out, re.M)
    return int(m.group(1)) if m else 0


def _plain(out):
    return "\n".join(line for line in out.splitlines()
                     if not line.startswith("[ozagia] "))


@pytest.mark.parametrize("command", [
    "git status --short; echo ---; git log --oneline -1 | head -1",
    "git diff --stat && echo same",
    "git show HEAD --stat | tail -n 2",
    "git log --oneline | grep initial || echo no",
    "git log --oneline | grep nothing || echo no",
    "echo a; git status --short || echo b",
    "git log --oneline -1 && git show HEAD:pkg/mod.py | sed -n '1,2p'",
])
def test_git_echo_steps_match_bash(mem, repo, command):
    res = _call(mem, "shell", command=command)
    assert res["handled"], res
    code, text = _bash(repo, command)
    assert _status(res["output"]) == code
    assert _plain(res["output"]) == text


@pytest.mark.parametrize("command", [
    "grep -n helper pkg/mod.py && echo yes",
    "grep -n nothing_here pkg/mod.py && echo yes",
    "grep -n nothing_here pkg/mod.py || echo none",
    "grep -n nothing_here pkg/mod.py; echo after",
    "cat pkg/mod.py | grep zzz",
    "cat pkg/mod.py | grep -A1 helper | tail -1",
    "sed -n 1,3p pkg/mod.py | grep def",
    "sed -n 1,3p pkg/mod.py | grep -v def | head -1",
])
def test_exit_statuses_match_bash(mem, repo, command):
    res = _call(mem, "shell", command=command)
    assert res["handled"], res
    assert _status(res["output"]) == _bash(repo, command)[0]


def test_filtered_read_shows_bash_lines_and_keys(mem, repo):
    command = "cat pkg/mod.py | grep def"
    res = _call(mem, "shell", command=command)
    assert res["handled"], res
    assert _plain(res["output"]) == _bash(repo, command)[1]
    assert "[ozagia] lines in: sym:pkg/mod.py::" in res["output"]


def test_grep_filter_keeps_graph_lines(mem):
    res = _call(mem, "shell", command="grep -rn helper pkg | head -1")
    assert res["handled"], res
    assert res["output"].count("  Line ") == 1
    assert "[ozagia] sym:" in res["output"]


def test_cd_sets_the_directory_of_later_steps(mem, repo):
    res = _call(mem, "shell", command=f"cd {repo}/pkg && pwd && cat mod.py")
    assert res["handled"], res
    assert res["output"].splitlines()[0] == str(repo / "pkg")
    assert "pkg/mod.py" in res["output"]


def test_cd_outside_the_root_is_passthrough(mem):
    res = _call(mem, "shell", command="cd /etc && cat hostname")
    assert res["handled"] is False
    assert "outside the project root" in res["reason"]


def test_missing_file_is_passthrough_before_anything_runs(mem):
    res = _call(mem, "shell", command="echo hi && cat pkg/missing.py")
    assert res["handled"] is False


def test_git_diff_step_adds_touched_keys(mem, repo):
    p = repo / "pkg" / "mod.py"
    p.write_text(p.read_text() + "\n# changed\n")
    res = _call(mem, "shell", command="git diff; echo done")
    assert res["handled"], res
    assert "[ozagia] touched: " in res["output"]
    assert res["output"].rstrip().splitlines()[-1] != "[ozagia] exit 1"


def test_pytest_step_reports_failures_and_status(mem, repo):
    py = sys.executable
    command = f"cd {repo} && {py} -m pytest tests/test_mod.py -q 2>&1 | tail -3 && echo after"
    res = _call(mem, "shell", command=command)
    assert res["handled"], res
    assert res["meta"]["intent"] == "shell_compound"
    assert "[ozagia] pytest " in res["output"]
    assert res["output"].rstrip().endswith("after")   # tail made the status 0
    res = _call(mem, "shell",
                command=f"{py} -m pytest tests/test_mod.py -q && echo after")
    assert "after" not in _plain(res["output"]).splitlines()[-1:]
    assert _status(res["output"]) == 1


def test_one_timeout_covers_the_whole_command(mem, repo):
    py = sys.executable
    res = _call(mem, "shell", timeout=1,
                command=f"{py} -m pytest tests/test_mod.py -q; echo after")
    assert res["handled"], res
    assert "timed out" in res["output"]


def test_compound_with_pytest_takes_the_lock_only_for_graph_steps(mem, repo):
    lock = threading.Lock()
    seen = []

    class Spy:
        def __enter__(self):
            seen.append("in")
            lock.acquire()

        def __exit__(self, *a):
            lock.release()

    py = sys.executable
    res = handle(mem, "shell", {
        "command": f"{py} -m pytest tests/test_mod.py -q; cat pkg/mod.py",
        "_lock": Spy()})
    assert res["handled"], res
    assert seen == ["in"]          # the read step only


def test_daemon_runs_a_compound_with_pytest_without_the_lock():
    from ozagia.daemon.server import _lock_free
    assert _lock_free("tool", "shell", {"command": "cd x && pytest -q | tail -3"})
    assert not _lock_free("tool", "shell", {"command": "git diff; cat a.py"})


# --------------------------------------------------------------------------- #
# after a passthrough compound command
# --------------------------------------------------------------------------- #
def test_after_maps_pytest_summary_lines_to_test_keys(mem, repo):
    out = ("...F\nFAILED tests/test_mod.py::test_broken - assert 3 == 5\n"
           "FAILED tests/test_mod.py::test_unknown - x\n1 failed\n")
    res = after(mem, "shell", {"command": "python -c 'x' ; pytest -q"}, out)
    assert "[ozagia] failed test:tests/test_mod.py::test_broken: assert 3 == 5" \
        in res["append"]
    assert "test_unknown" not in res["append"]


def test_after_maps_git_diff_in_a_passthrough_compound(mem, repo):
    p = repo / "pkg" / "mod.py"
    p.write_text(p.read_text() + "\n# changed\n")
    diff = subprocess.run(["git", "diff"], cwd=repo, capture_output=True,
                          text=True).stdout
    res = after(mem, "shell", {"command": "python -c 'x'; git diff"}, diff)
    assert "[ozagia] touched: " in res["append"]


def test_after_compound_without_pytest_skips_the_refresh(mem):
    res = after(mem, "shell", {"command": "git log -1; cat pkg/mod.py"}, "")
    assert res["meta"]["intent"] == "after_none"
