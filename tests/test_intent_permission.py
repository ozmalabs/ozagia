"""Preflight (run the checks, change nothing) and the permission request a
harness's own tool would make for a call."""
from __future__ import annotations

import sys

import test_intent as _ti
from ozagia.intent import handle, permission_request

layout = _ti.layout
repo = _ti.repo
mem = _ti.mem


def test_preflight_edit_changes_nothing_and_names_the_permission(mem, repo):
    before = (repo / "pkg/mod.py").read_text()
    inp = {"path": "pkg/mod.py", "oldString": "return a + b",
           "newString": "return a - b"}
    res = handle(mem, "edit", inp, preflight=True)
    assert res["preflight"] is True and res["handled"] is False
    assert res["permission"] == {"action": "edit", "resources": ["pkg/mod.py"],
                                 "save": ["*"]}
    assert (repo / "pkg/mod.py").read_text() == before
    res = handle(mem, "edit", inp)
    assert res["handled"] and not res["is_error"]
    assert "return a - b" in (repo / "pkg/mod.py").read_text()


def test_preflight_edit_that_would_fail_or_pass_through_answers_as_usual(mem):
    miss = handle(mem, "edit", {"path": "pkg/mod.py", "oldString": "nowhere",
                                "newString": "x"}, preflight=True)
    assert miss["handled"] is False and "preflight" not in miss
    many = handle(mem, "edit", {"path": "pkg/mod.py", "oldString": "return",
                                "newString": "yield"}, preflight=True)
    assert many["handled"] is True and many["is_error"] is True


def test_preflight_write_does_not_create_the_file(mem, repo):
    res = handle(mem, "write", {"path": "pkg/new.py", "content": "X = 1\n"},
                 preflight=True)
    assert res["preflight"] is True
    assert not (repo / "pkg/new.py").exists()
    res = handle(mem, "edit", {"path": "pkg/new2.py", "oldString": "",
                               "newString": "X = 1\n"}, preflight=True)
    assert res["preflight"] is True
    assert not (repo / "pkg/new2.py").exists()


def test_preflight_shell_runs_nothing(mem, repo):
    marker = repo / "ran.txt"
    script = repo / "tests" / "test_marker.py"
    script.write_text("from pathlib import Path\n\n\ndef test_m():\n"
                      f"    Path({str(marker)!r}).write_text('x')\n")
    for command in (f"{sys.executable} -m pytest tests/test_marker.py -q",
                    f"echo a && {sys.executable} -m pytest tests/test_marker.py -q"):
        res = handle(mem, "shell", {"command": command}, preflight=True)
        assert res["preflight"] is True, res
        assert res["permission"]["action"] == "shell"
        assert not marker.exists()
    res = handle(mem, "shell", {"command": "cat pkg/mod.py"}, preflight=True)
    assert res["preflight"] is True


def test_preflight_refusal_and_passthrough_are_answered_directly(mem):
    res = handle(mem, "shell", {"command": "env git commit -m x"}, preflight=True)
    assert res["handled"] is True and res["is_error"] is True
    res = handle(mem, "shell", {"command": "ls"}, preflight=True)
    assert res["handled"] is False and "preflight" not in res


def test_read_only_tools_ignore_preflight_and_carry_the_permission(mem):
    res = handle(mem, "read", {"path": "pkg/mod.py"}, preflight=True)
    assert res["handled"] is True
    assert res["permission"] == {"action": "read", "resources": ["pkg/mod.py"],
                                 "save": ["*"]}
    res = handle(mem, "grep", {"pattern": "helper"})
    assert res["permission"] == {"action": "grep", "resources": ["helper"],
                                 "save": ["*"]}


def test_permission_request_shapes(mem, repo):
    got = permission_request(mem, "shell", {
        "command": "cd pkg && python -m pytest -q 2>&1 | tail -3"})
    assert got == {"action": "shell",
                   "resources": ["cd pkg", "python -m pytest -q", "tail -3"],
                   "save": ["cd *", "python *", "tail *"]}
    got = permission_request(mem, "edit", {"filePath": str(repo / "pkg/mod.py")})
    assert got["resources"] == ["pkg/mod.py"]
    got = permission_request(mem, "read", {"path": "/etc/hostname"})
    assert got["resources"] == ["/etc/hostname"]
    assert permission_request(mem, "patch", {}) is None


def test_daemon_counts_preflights(mem, repo, tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    msg = {"op": "tool", "tool": "write", "preflight": True,
           "input": {"path": "pkg/x.py", "content": "X = 1\n"}}
    resp = st.dispatch(msg)
    st.record(msg, resp, 1.0)
    assert resp["preflight"] is True and not (repo / "pkg/x.py").exists()
    stats = st.stats()
    assert stats["preflights"] == 1 and stats["passthrough"] == 0
    # A client cannot set the reserved field itself.
    resp = st.dispatch({"op": "tool", "tool": "write",
                        "input": {"path": "pkg/y.py", "content": "Y = 1\n",
                                  "_preflight": True}})
    assert resp["handled"] is True and (repo / "pkg/y.py").exists()
