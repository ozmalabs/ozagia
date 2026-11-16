"""Graph-addressed goals (`ozagia.core.goals`): the store, key resolution,
every criterion kind, status transitions, claims, the daemon's hints and
`goals` op, the brief, `from-review`, the CLI and the MCP tools."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ozagia.config import state_path
from ozagia.core import goals as G
from ozagia.core import working_set as W
from ozagia.core.memory import DevMemory, build_index

CORE = '''\
def helper(x):
    return x + 1


def run(x):
    return helper(x) * 2


def legacy():
    return 0


class Box:
    def size(self):
        return helper(1)
'''
TESTS = '''\
from pkg.core import run


def test_run():
    assert run(1) == 4


def test_other():
    assert True
'''
T_RUN = "test:tests/test_core.py::test_run"
T_OTHER = "test:tests/test_core.py::test_other"
S_HELPER = "sym:pkg/core.py::helper"
S_RUN = "sym:pkg/core.py::run"
S_LEGACY = "sym:pkg/core.py::legacy"
F_CORE = "file:pkg/core.py"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _memory(root: Path) -> DevMemory:
    dump = state_path(root, "graph.json")
    build_index(root, dump, include_docs="none", include_git=False,
                include_scripts=False)
    return DevMemory(dump, state_path(root, "journal.jsonl"), root=root)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "core.py", CORE)
    _write(root / "pkg" / "other.py", "VALUE = 1\n")
    _write(root / "tests" / "test_core.py", TESTS)
    return root.resolve()


@pytest.fixture()
def mem(repo: Path) -> DevMemory:
    return _memory(repo)


def _pytest(root: Path, text: str, selectors: str = "tests", ts: float | None = None):
    """Record a pytest run in the root's test record (as the daemon does)."""
    command = f"python -m pytest -q {selectors}"
    run = W.pytest_run(command, [command.split()], text, workdir=root, root=root)
    assert run is not None
    if ts is not None:
        run.ts = ts
    W.TestRecord(root).apply(run)
    return run


PASSED = "2 passed in 0.1s\n"
FAILED = "FAILED tests/test_core.py::test_run - assert 3 == 4\n1 failed, 1 passed in 0.1s\n"


def _add(root, mem, *criteria, targets=(F_CORE,), scope=(), title="Goal"):
    return G.add_goal(root, mem, {"title": title, "targets": list(targets),
                                  "scope": list(scope), "criteria": list(criteria)})


def _check(root, mem, gid):
    (report,) = G.check_goals(root, mem, [gid])
    return report


def _statuses(report):
    return [r["status"] for r in report["results"]]


def _age(root: Path, rel: str, ts: float) -> None:
    os.utime(root / rel, (ts, ts))


# --------------------------------------------------------------------------- #
# creation and key resolution
# --------------------------------------------------------------------------- #
def test_add_resolves_keys_and_stores_schema(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}", "symbol_exists:sym:pkg/core.py::new_fn",
                f"calls:{S_RUN} {S_HELPER}", scope=["pkg/**", S_HELPER])
    assert goal["id"] == "g1" and goal["status"] == "open"
    assert [c["id"] for c in goal["criteria"]] == ["c1", "c2", "c3"]
    assert goal["criteria"][2]["args"] == {"caller": S_RUN, "callee": S_HELPER}
    assert goal["history"][0]["event"] == "created"
    data = json.loads(G.goals_path(repo).read_text())
    assert data["version"] == G.SCHEMA_VERSION and data["next_id"] == 2
    assert G.goals_path(repo) == repo / ".ozagia" / "goals" / "goals.json"
    # Prefixes are added to bare paths of the kind's key parameter.
    g2 = _add(repo, mem, "test_passes:tests/test_core.py::test_other")
    assert g2["id"] == "g2" and g2["criteria"][0]["args"]["key"] == T_OTHER


@pytest.mark.parametrize("spec, message", [
    ({"targets": ["sym:pkg/core.py::nope"]}, "targets: unknown key"),
    ({"targets": ["pkg/core.py"]}, "is not a graph key"),
    ({"scope": ["sym:pkg/core.py::nope"]}, "scope: unknown key"),
    ({"criteria": ["test_passes:test:tests/test_core.py::test_missing"]}, "unknown key"),
    ({"criteria": ["symbol_absent:sym:pkg/core.py::nope"]}, "unknown key"),
    ({"criteria": ["file_absent:file:pkg/nope.py"]}, "unknown key"),
    ({"criteria": [f"calls:{S_RUN} sym:pkg/core.py::nope"]}, "unknown key"),
    ({"criteria": ["shell:make test"]}, "expected kind:args"),
    ({"criteria": [f"test_passes:{S_RUN}"]}, "must be a test: key"),
    ({"criteria": []}, "at least one criterion"),
    ({"title": ""}, "needs a title"),
])
def test_add_refuses_unknown_keys_and_bad_criteria(repo, mem, spec, message):
    base = {"title": "x", "targets": [F_CORE], "criteria": [f"test_passes:{T_RUN}"]}
    with pytest.raises(G.GoalError, match=message):
        G.add_goal(repo, mem, {**base, **spec})
    assert G.read_goals(repo) == []


def test_absent_subject_allowed_only_for_exists_kinds(repo, mem):
    goal = _add(repo, mem, "symbol_exists:sym:pkg/core.py::later",
                "file_exists:file:pkg/later.py")
    assert [c["kind"] for c in goal["criteria"]] == ["symbol_exists", "file_exists"]


# --------------------------------------------------------------------------- #
# criteria
# --------------------------------------------------------------------------- #
def test_test_passes_and_fails_from_the_record(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}", f"test_fails:{T_RUN}")
    assert _statuses(_check(repo, mem, goal["id"])) == ["unknown", "unknown"]
    _pytest(repo, PASSED)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass", "fail"]
    assert report["results"][0]["evidence"]["run_at"] is not None
    _pytest(repo, FAILED)
    assert _statuses(_check(repo, mem, goal["id"])) == ["fail", "pass"]


def test_test_result_older_than_target_change_is_stale(repo, mem):
    now = time.time()
    _age(repo, "pkg/core.py", now - 100)
    _age(repo, "tests/test_core.py", now - 100)
    _pytest(repo, PASSED, ts=now - 50)
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass"]
    _age(repo, "pkg/core.py", now - 10)          # an edit after the run
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["stale"]
    assert report["results"][0]["evidence"]["changed_file"] == "pkg/core.py"
    _pytest(repo, PASSED, ts=now)                # rerun: fresh again
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass"]


def test_filtered_run_does_not_pass_a_test(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    command = "python -m pytest -q -k other tests"
    run = W.pytest_run(command, [command.split()], "1 passed, 1 deselected in 0.1s\n",
                       workdir=repo, root=repo)
    W.TestRecord(repo).apply(run)
    assert _statuses(_check(repo, mem, goal["id"])) == ["unknown"]


def test_symbol_and_file_kinds(repo, mem):
    goal = _add(repo, mem, f"symbol_exists:{S_HELPER}", f"symbol_absent:{S_LEGACY}",
                "symbol_exists:sym:pkg/core.py::later", f"file_exists:{F_CORE}",
                "file_absent:file:pkg/other.py", "file_exists:file:pkg/later.py")
    assert _statuses(_check(repo, mem, goal["id"])) == [
        "pass", "fail", "fail", "pass", "fail", "fail"]
    text = (repo / "pkg" / "core.py").read_text().replace(
        "def legacy():\n    return 0\n", "def later():\n    return 0\n")
    _write(repo / "pkg" / "core.py", text)
    (repo / "pkg" / "other.py").unlink()
    _write(repo / "pkg" / "later.py", "X = 1\n")
    # The check takes the pending changes into the in-memory graph.
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass"] * 6


def test_calls_not_calls_and_no_callers(repo, mem):
    pytest.importorskip("tree_sitter_python")
    goal = _add(repo, mem, f"calls:{S_RUN} {S_HELPER}", f"not_calls:{S_RUN} {S_LEGACY}",
                f"no_callers:{S_LEGACY}", f"no_callers:{S_HELPER}")
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass", "pass", "pass", "fail"]
    assert report["results"][0]["evidence"]["edge"]["resolution"] == "local"
    assert report["results"][3]["evidence"]["caller_count"] == 2
    text = (repo / "pkg" / "core.py").read_text().replace(
        "return helper(x) * 2", "return legacy() + x")
    _write(repo / "pkg" / "core.py", text)
    assert _statuses(_check(repo, mem, goal["id"])) == ["fail", "fail", "fail", "fail"]


def test_unchanged_stores_hash_at_creation(repo, mem):
    goal = _add(repo, mem, f"unchanged:{S_LEGACY}", f"unchanged:{F_CORE}",
                "unchanged:file:pkg/other.py")
    assert all(len(c["args"]["hash"]) == 64 for c in goal["criteria"])
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass", "pass", "pass"]
    text = (repo / "pkg" / "core.py").read_text().replace("return x + 1", "return x + 2")
    _write(repo / "pkg" / "core.py", text)
    # Editing `helper` leaves `legacy`'s lines alone but changes the file.
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass", "fail", "pass"]
    assert report["results"][1]["evidence"]["created_hash"] != report[
        "results"][1]["evidence"]["hash"]


def test_lint_clean_is_unknown_with_a_reason(repo, mem):
    goal = _add(repo, mem, f"lint_clean:{F_CORE}")
    (res,) = _check(repo, mem, goal["id"])["results"]
    assert res["status"] == "unknown" and "keeps no lint results" in res["reason"]
    with pytest.raises(G.GoalError, match="unknown key"):
        _add(repo, mem, "lint_clean:file:pkg/nope.py")


def _inject_ops_node(root: Path) -> DevMemory:
    dump = state_path(root, "graph.json")
    raw = json.loads(dump.read_text())
    nid = raw["next_id"]
    raw["nodes"].append({"id": nid, "type": "InfraResource",
                         "key": "ops:compose/compose.yaml#service/web",
                         "attrs": {"family": "ops", "kind": "compose_service",
                                   "name": "web", "image": "nginx:1.25",
                                   "ports": ["8080:80"]}})
    raw["next_id"] = nid + 1
    dump.write_text(json.dumps(raw))
    return DevMemory(dump, state_path(root, "journal.jsonl"), root=root)


def test_ops_has(repo, mem):
    mem = _inject_ops_node(repo)
    key = "ops:compose/compose.yaml#service/web"
    goal = G.add_goal(repo, mem, {"title": "pin", "targets": [key], "criteria": [
        f"ops_has:{key}", f"ops_has:{key} image nginx:1.25", f"ops_has:{key} image nginx:1.26",
        f"ops_has:{key} restart", f"ops_has:{key} ports 8080:80"]})
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass", "pass", "fail", "fail", "pass"]
    with pytest.raises(G.GoalError, match="unknown key"):
        G.add_goal(repo, mem, {"title": "x", "criteria": ["ops_has:ops:image/nope"]})


def _packet(path: Path, findings: list[dict]) -> Path:
    path.write_text(json.dumps({"schema": G.PACKET_SCHEMA, "truth_assessed": False,
                                "sections": {}, "findings": findings}))
    return path


FINDING = {
    "id": "rr-0f3a9c", "section": "health", "type": "dead_symbol",
    "severity": "minor", "keys": [S_LEGACY], "evidence": {"callers": 0},
    "confidence": 0.5,
    "suggested_goal": {"title": "Remove pkg.core.legacy", "targets": [S_LEGACY],
                       "criteria": [f"symbol_absent:{S_LEGACY}",
                                    {"kind": "test_passes", "args": {"key": T_RUN}}]},
}


def test_review_resolved_reads_the_packet(repo, mem, tmp_path):
    packet = _packet(tmp_path / "review.json", [FINDING])
    goal = _add(repo, mem, f"review_resolved:{packet} rr-0f3a9c", targets=[S_LEGACY])
    assert goal["criteria"][0]["args"]["packet"] == str(packet)
    assert _statuses(_check(repo, mem, goal["id"])) == ["fail"]
    _packet(packet, [])
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass"] and report["results"][0]["evidence"]["packet_sha256"]
    _age(repo, "pkg/core.py", time.time() + 5)   # a change after the packet
    assert _statuses(_check(repo, mem, goal["id"])) == ["stale"]
    packet.unlink()
    assert _statuses(_check(repo, mem, goal["id"])) == ["unknown"]
    with pytest.raises(G.GoalError, match="cannot read"):
        _add(repo, mem, f"review_resolved:{packet} rr-0f3a9c")


# --------------------------------------------------------------------------- #
# status transitions, claims, drop and block
# --------------------------------------------------------------------------- #
def test_met_then_regressed_then_met(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}", f"symbol_exists:{S_HELPER}")
    _pytest(repo, FAILED)
    assert _check(repo, mem, goal["id"])["status"] == "open"
    _pytest(repo, PASSED)
    report = _check(repo, mem, goal["id"])
    assert (report["previous"], report["status"]) == ("open", "met")
    _pytest(repo, FAILED)
    report = _check(repo, mem, goal["id"])
    assert (report["previous"], report["status"]) == ("met", "regressed")
    _pytest(repo, PASSED)
    assert _check(repo, mem, goal["id"])["status"] == "met"
    events = [h["event"] for h in G.get_goal(repo, goal["id"])["history"]]
    assert events == ["created", "checked", "checked", "met", "checked", "regressed",
                      "checked", "met"]
    last = G.get_goal(repo, goal["id"])["criteria"][0]["last"]
    assert last["status"] == "pass" and last["at"]


def test_stale_or_unknown_does_not_regress(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    _pytest(repo, PASSED, ts=time.time() + 10)
    assert _check(repo, mem, goal["id"])["status"] == "met"
    _age(repo, "pkg/core.py", time.time() + 60)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["stale"] and report["status"] == "met"


def test_drop_and_block_only_by_command(repo, mem):
    goal = _add(repo, mem, f"symbol_exists:{S_HELPER}")
    with pytest.raises(G.GoalError, match="needs a reason"):
        G.block_goal(repo, goal["id"])
    assert G.block_goal(repo, goal["id"], reason="waiting on API")["status"] == "blocked"
    # Evaluation records results but never moves a blocked goal.
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass"] and report["status"] == "blocked"
    assert G.block_goal(repo, goal["id"], clear=True)["status"] == "open"
    assert _check(repo, mem, goal["id"])["status"] == "met"
    assert G.drop_goal(repo, goal["id"], reason="obsolete")["status"] == "dropped"
    assert G.check_goals(repo, mem) == []          # dropped goals are skipped
    assert _check(repo, mem, goal["id"])["status"] == "dropped"
    assert G.list_goals(repo) == [] and len(G.list_goals(repo, include_dropped=True)) == 1
    with pytest.raises(G.GoalError, match="dropped"):
        G.claim_goal(repo, goal["id"], "claude", "s1")
    events = [h["event"] for h in G.get_goal(repo, goal["id"])["history"]]
    assert {"blocked", "unblocked", "met", "dropped"} <= set(events)


def test_claim_is_by_harness_and_session(repo, mem):
    goal = _add(repo, mem, f"symbol_exists:{S_HELPER}")
    G.claim_goal(repo, goal["id"], "claude", "s1")
    G.claim_goal(repo, goal["id"], "claude", "s1")      # idempotent
    G.claim_goal(repo, goal["id"], "codex", "s1")
    got = G.get_goal(repo, goal["id"])
    assert [(c["harness"], c["session"]) for c in got["claimed_by"]] == [
        ("claude", "s1"), ("codex", "s1")]
    assert G.claimed_by(got, "claude", "s1") and not G.claimed_by(got, "kiro", "s1")
    G.claim_goal(repo, goal["id"], "claude", "s1", release=True)
    got = G.get_goal(repo, goal["id"])
    assert [(c["harness"], c["session"]) for c in got["claimed_by"]] == [("codex", "s1")]
    assert [h["event"] for h in got["history"]].count("claimed") == 2
    with pytest.raises(G.GoalError, match="no goal"):
        G.claim_goal(repo, "g99", "claude", "s1")


# --------------------------------------------------------------------------- #
# scope globs
# --------------------------------------------------------------------------- #
def test_in_scope_globs_and_keys():
    assert G.in_scope(["pkg/**"], "pkg/a/b.py")
    assert G.in_scope(["pkg/"], "pkg/b.py")
    assert G.in_scope(["**/*.md"], "docs/x.md") and G.in_scope(["**/*.md"], "x.md")
    assert not G.in_scope(["pkg/*.py"], "pkg/a/b.py")
    assert G.in_scope(["file:pkg/core.py"], "pkg/core.py")
    assert G.in_scope(["file:pkg/*.py"], "pkg/core.py")
    assert G.in_scope([S_HELPER], "pkg/core.py", [S_HELPER])
    assert G.in_scope([S_HELPER], "pkg/core.py", [])
    assert not G.in_scope([S_HELPER], "pkg/core.py", [S_RUN])
    assert G.in_scope(["sym:pkg/core.py::Box"], "pkg/core.py", ["sym:pkg/core.py::Box.size"])
    assert not G.in_scope([S_HELPER], "pkg/other.py")


# --------------------------------------------------------------------------- #
# the daemon: edit hints, scope note, `goals` op, working set
# --------------------------------------------------------------------------- #
def _edit(state, rel, old, new, *, harness="claude", session="s1"):
    resp = state.dispatch({"op": "tool", "tool": "edit", "harness": harness,
                           "session": session,
                           "input": {"path": rel, "oldString": old, "newString": new}})
    assert resp["ok"] and resp["handled"] and not resp["is_error"], resp
    return resp


def test_daemon_hint_and_scope_note(repo, mem, tmp_path):
    from ozagia.daemon.server import DaemonState
    goal = _add(repo, mem, f"test_passes:{T_RUN}", f"symbol_exists:{S_HELPER}",
                f"unchanged:{S_LEGACY}", targets=[S_HELPER], scope=["pkg/core.py"])
    other = _add(repo, mem, f"symbol_exists:{S_RUN}", targets=[S_RUN], title="Other")
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    # Not claimed: no goal lines.
    resp = _edit(st, "pkg/core.py", "return x + 1", "return x + 2")
    assert "[ozagia] goal" not in resp["output"]
    G.claim_goal(repo, goal["id"], "claude", "s1")
    G.claim_goal(repo, other["id"], "codex", "s9")
    resp = _edit(st, "pkg/core.py", "return x + 2", "return x + 3")
    lines = [x for x in resp["output"].splitlines() if x.startswith("[ozagia] goal")]
    assert len(lines) == 1, resp["output"]
    assert lines[0].startswith(f"[ozagia] goal {goal['id']} \"Goal\": this edit touched "
                               f"target {S_HELPER}")
    assert f"c1 test_passes {T_RUN}" in lines[0] and "c3 unchanged" in lines[0]
    assert resp["meta"]["goal_hints"] == 1
    # Another session's edit gets nothing; an edit outside the scope gets
    # a scope note (and the edit still happens).
    resp = _edit(st, "pkg/core.py", "return x + 3", "return x + 4", session="s2")
    assert "[ozagia] goal" not in resp["output"]
    resp = _edit(st, "pkg/other.py", "VALUE = 1", "VALUE = 2")
    lines = [x for x in resp["output"].splitlines() if x.startswith("[ozagia] goal")]
    assert lines == [f"[ozagia] goal {goal['id']} scope note: pkg/other.py is outside "
                     "the claimed goal's scope (advisory)"]
    assert (repo / "pkg" / "other.py").read_text() == "VALUE = 2\n"
    assert st.stats()["goal_hints"] == 2
    # `after` of a built-in edit gets the lines in `append`.
    body = st._goal_hints("after", {"append": "", "meta": {}}, "claude", "s1",
                          ["pkg/core.py"], [S_HELPER])
    assert body["append"].startswith(f"[ozagia] goal {goal['id']}")


def test_daemon_goals_op_and_working_set(repo, mem, tmp_path):
    from ozagia.daemon.server import DaemonState
    goal = _add(repo, mem, f"test_passes:{T_RUN}", targets=[S_HELPER])
    G.claim_goal(repo, goal["id"], "claude", "s1")
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    before = G.goals_path(repo).read_bytes()
    resp = st.dispatch({"op": "goals", "harness": "claude", "session": "s1"})
    assert resp["ok"] and resp["goals"][0]["id"] == goal["id"]
    assert resp["goals"][0]["mine"] is True
    assert resp["goals"][0]["unmet"][0]["status"] == "unchecked"
    assert f"{goal['id']} [open, claimed by this session]" in resp["text"]
    assert G.goals_path(repo).read_bytes() == before        # read-only
    _edit(st, "pkg/core.py", "return x + 1", "return x + 2")
    ws = st.dispatch({"op": "working_set", "harness": "claude", "session": "s1"})
    assert "goals (unmet criteria" in ws["text"] and f"{goal['id']} [open" in ws["text"]
    brief = st.dispatch({"op": "working_set", "since": True, "harness": "claude",
                         "session": "s1"})
    assert f"{goal['id']} [open, claimed by this session]" in brief["text"]
    assert len(brief["text"]) <= W.BRIEF_MAX_CHARS


def test_brief_lists_goals_within_cap(repo, mem):
    for i in range(8):
        _add(repo, mem, f"test_passes:{T_RUN}", f"symbol_exists:{S_HELPER}",
             title=f"Goal number {i} " + "with a long title " * 6)
    met = _add(repo, mem, f"symbol_exists:{S_HELPER}", title="Met one")
    _check(repo, mem, met["id"])
    text = W.session_brief(repo, jobs_enabled=False)
    assert len(text) <= W.BRIEF_MAX_CHARS
    assert "goals (unmet criteria" in text and "more goal(s)" in text
    assert "Met one" not in text
    goal_part = [x for x in text.splitlines() if x.startswith("  g")]
    assert 1 <= len(goal_part) <= G.BRIEF_GOALS
    assert sum(len(x) + 1 for x in G.brief_lines(repo, max_chars=500)) <= 500
    short = W.session_brief(repo, 300, jobs_enabled=False)
    assert len(short) <= 300 and short.endswith(W.MCP_TOOLS_LINE)
    # A missing or broken store gives no lines and no error.
    G.goals_path(repo).write_text("{not json")
    assert G.brief_lines(repo) == [] and "goals (" not in W.session_brief(
        repo, jobs_enabled=False)


# --------------------------------------------------------------------------- #
# from-review
# --------------------------------------------------------------------------- #
def test_from_review_with_a_fixture_packet(repo, mem, tmp_path):
    packet = _packet(tmp_path / "review.json", [FINDING, {
        "id": "rr-other", "section": "docs", "type": "dangling_doc_ref",
        "severity": "note", "keys": [S_HELPER], "evidence": {}, "confidence": 0.9}])
    goal = G.from_review(repo, mem, str(packet), "rr-0f3a9c")
    assert goal["title"] == "Remove pkg.core.legacy"
    assert goal["targets"] == [S_LEGACY]
    assert goal["source"] == {"review_packet": str(packet), "finding_id": "rr-0f3a9c"}
    assert [c["kind"] for c in goal["criteria"]] == [
        "symbol_absent", "test_passes", "review_resolved"]
    with pytest.raises(G.GoalError, match="already tracks finding"):
        G.from_review(repo, mem, str(packet), "rr-0f3a9c")
    with pytest.raises(G.GoalError, match="not in"):
        G.from_review(repo, mem, str(packet), "rr-missing")
    # No suggested_goal: the finding's keys are the targets.
    g2 = G.from_review(repo, mem, str(packet), "rr-other")
    assert g2["targets"] == [S_HELPER] and g2["criteria"][0]["kind"] == "review_resolved"
    # Resolve it: remove the symbol, rerun the tests and the review.
    _write(repo / "pkg" / "core.py", CORE.replace("def legacy():\n    return 0\n", ""))
    _pytest(repo, PASSED, ts=time.time() + 5)
    _packet(packet, [])
    _age(repo, "pkg/core.py", time.time() - 5)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass", "pass", "pass"] and report["status"] == "met"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "something.else", "findings": []}))
    with pytest.raises(G.GoalError, match="no readable"):
        G.from_review(repo, mem, str(bad), "rr-0f3a9c")


# --------------------------------------------------------------------------- #
# the store: locking and atomic writes
# --------------------------------------------------------------------------- #
def test_store_writes_are_atomic(repo, mem, monkeypatch):
    goal = _add(repo, mem, f"symbol_exists:{S_HELPER}")
    before = G.goals_path(repo).read_bytes()
    # An error inside the block writes nothing.
    with pytest.raises(RuntimeError):
        with G.locked_store(repo) as data:
            data["goals"].clear()
            raise RuntimeError("boom")
    assert G.goals_path(repo).read_bytes() == before
    # A failed replace leaves the old file and no temporary file.
    def fail(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(G.os, "replace", fail)
    with pytest.raises(OSError):
        G.claim_goal(repo, goal["id"], "claude", "s1")
    monkeypatch.undo()
    assert G.goals_path(repo).read_bytes() == before
    assert sorted(p.name for p in G.goals_path(repo).parent.iterdir()) == [
        ".lock", "goals.json"]
    # Another schema version is refused, not overwritten.
    G.goals_path(repo).write_text(json.dumps({"version": 2, "goals": []}))
    with pytest.raises(G.GoalError, match="schema version 2"):
        G.claim_goal(repo, goal["id"], "claude", "s1")


def test_store_lock_serialises_processes(repo, mem):
    goal = _add(repo, mem, f"symbol_exists:{S_HELPER}")
    code = ("import sys\nfrom ozagia.core import goals as G\n"
            "for i in range(5):\n"
            "    G.claim_goal(sys.argv[1], 'g1', 'h' + sys.argv[2], f's{i}')\n")
    env = {**os.environ, "OZAGIA_ROOT": str(repo)}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(repo), str(n)], env=env)
             for n in range(4)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    got = G.get_goal(repo, goal["id"])
    assert len(got["claimed_by"]) == 20
    assert [h["event"] for h in got["history"]].count("claimed") == 20


# --------------------------------------------------------------------------- #
# CLI and MCP
# --------------------------------------------------------------------------- #
def test_cli_goal_commands(repo, mem, tmp_path, capsys):
    from ozagia.cli import main
    root = str(repo)
    toml = tmp_path / "goal.toml"
    toml.write_text(f'''title = "From a file"
why = "because"
targets = ["{F_CORE}"]

[[criteria]]
kind = "symbol_exists"
args = "{S_HELPER}"

[[criteria]]
kind = "test_passes"
key = "{T_RUN}"
''')
    assert main(["goal", "add", "--root", root, "--file", str(toml),
                 "--criterion", f"file_exists:{F_CORE}", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["goal"]["id"] == "g1" and len(out["goal"]["criteria"]) == 3
    assert main(["goal", "add", "--root", root, "--title", "Flags",
                 "--target", S_HELPER, "--scope", "pkg/**",
                 "--criterion", f"calls:{S_RUN} {S_HELPER}"]) == 0
    assert "g2 [open] Flags" in capsys.readouterr().out
    assert main(["goal", "add", "--root", root, "--title", "Bad",
                 "--target", "sym:pkg/core.py::nope", "--criterion",
                 f"symbol_exists:{S_HELPER}"]) == 2
    assert "unknown key sym:pkg/core.py::nope" in capsys.readouterr().err
    assert main(["goal", "claim", "--root", root, "g1", "--harness", "codex",
                 "--session", "abc"]) == 0
    assert "g1 claimed for codex/abc" in capsys.readouterr().out
    _pytest(repo, PASSED, ts=time.time() + 5)
    assert main(["goal", "check", "--root", root, "g1"]) == 0
    out = capsys.readouterr().out
    assert "g1 [met] (was open) From a file" in out and "pass    c2 test_passes" in out
    assert main(["goal", "list", "--root", root]) == 0
    out = capsys.readouterr().out
    assert "g1 [met] From a file (0/3 unmet) claimed by codex/abc" in out
    assert main(["goal", "show", "--root", root, "g1"]) == 0
    out = capsys.readouterr().out
    assert "why: because" in out and "history:" in out and "claimed by cli" in out
    assert main(["goal", "block", "--root", root, "g2", "--reason", "later"]) == 0
    assert main(["goal", "drop", "--root", root, "g2", "--json"]) == 0
    capsys.readouterr()
    assert main(["goal", "list", "--root", root, "--all", "--json"]) == 0
    statuses = {g["id"]: g["status"] for g in json.loads(capsys.readouterr().out)["goals"]}
    assert statuses == {"g1": "met", "g2": "dropped"}
    packet = _packet(tmp_path / "review.json", [FINDING])
    assert main(["goal", "from-review", "--root", root, str(packet), "rr-0f3a9c",
                 "--json"]) == 0
    got = json.loads(capsys.readouterr().out)["goal"]
    assert got["source"]["finding_id"] == "rr-0f3a9c"
    assert main(["goal", "show", "--root", root, "g42"]) == 2


def test_mcp_goal_tools(repo, mem):
    from ozagia.integrations.mcp.server import DevMemoryServer
    server = DevMemoryServer(mem)
    names = {t["name"] for t in server._tools}
    assert {"goal_list", "goal_show", "goal_check", "goal_add", "goal_claim"} <= names

    def call(name, args):
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": name, "arguments": args}})
        return json.loads(reply["result"]["content"][0]["text"])

    got = call("goal_add", {"title": "MCP goal", "targets": [S_HELPER],
                            "criteria": [f"symbol_exists:{S_HELPER}",
                                         {"kind": "test_passes", "args": {"key": T_RUN}}]})
    assert got["ok"] and got["goal"]["id"] == "g1"
    assert call("goal_add", {"title": "x", "criteria": ["symbol_absent:sym:a.py::b"]}
                ) == {"ok": False, "error": "criterion symbol_absent: unknown key sym:a.py::b"}
    assert call("goal_claim", {"id": "g1", "harness": "kiro", "session": "k1"}
                )["goal"]["claimed_by"] == ["kiro/k1"]
    assert call("goal_list", {})["goals"][0]["unmet"][0]["status"] == "unchecked"
    checked = call("goal_check", {"ids": ["g1"]})["goals"][0]
    assert [r["status"] for r in checked["results"]] == ["pass", "unknown"]
    shown = call("goal_show", {"id": "g1"})["goal"]
    assert shown["history"][-1]["by"] == "mcp"
    assert call("goal_show", {"id": "g9"}) == {"ok": False, "error": "no goal 'g9'"}
    reply = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "goal_claim", "arguments": {"id": "g1"}}})
    assert "error" in reply or reply["result"].get("isError")
