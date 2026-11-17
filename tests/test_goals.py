"""Graph-addressed goals (`ozagia.core.goals`): the store, key resolution,
every criterion kind, the evidence rules for test runs (captured runs only,
the working-tree fingerprint), status transitions, claims, the daemon's
hints and `goals` op, the brief, `from-review`, the CLI and the MCP tools."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ozagia.config import state_path
from ozagia.core import fingerprint as FP
from ozagia.core import goals as G
from ozagia.core import working_set as W
from ozagia.core.memory import DevMemory, build_index
from ozagia.intent.shell import command_heads

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
F_OTHER = "file:pkg/other.py"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True)


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
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root.resolve()


@pytest.fixture()
def mem(repo: Path) -> DevMemory:
    return _memory(repo)


# Captured pytest output (`ozagia.intent.run.run_pytest`'s head and lines).
PASSED = "[ozagia] pytest passed: exit 0, 0.1 s, 2 passed, 0 failed, 0 error, 0 skipped\n"
FAILED = ("[ozagia] pytest failed: exit 1, 0.1 s, 1 passed, 1 failed, 0 error, 0 skipped\n"
          "[ozagia] failed test:tests/test_core.py::test_run: assert 3 == 4\n")
ERRORED = ("[ozagia] pytest failed: exit 1, 0.1 s, 1 passed, 0 failed, 1 error, 0 skipped\n"
           "[ozagia] error test:tests/test_core.py::test_run: fixture 'db' not found\n")
COLLECT_ERROR = ("[ozagia] pytest failed: exit 2, 0.1 s, 0 passed, 0 failed, 0 error, "
                 "0 skipped, 1 collection errors\n"
                 "[ozagia] collection error file:tests/test_core.py: ImportError: x\n")


def _pytest(root: Path, text: str, selectors: str = "tests", *,
            command: str | None = None, source: str = "captured",
            fp: dict | None = None, env: dict | None = None):
    """Record a pytest run in the root's test record as the daemon does:
    for a captured run, the start time and the working tree's fingerprint
    (taken now, or `fp`)."""
    command = command or f"python -m pytest -q {selectors}"
    start = time.time()
    tree = fp if fp is not None else FP.tree_fingerprint(root)
    run = W.pytest_run(command, command_heads(command), text, workdir=root, root=root,
                       source=source, env=env or {})
    assert run is not None
    if run.source == "captured":
        run.ts, run.tree = start, tree
    W.TestRecord(root).apply(run)
    return run


def _add(root, mem, *criteria, targets=(F_CORE,), scope=(), title="Goal"):
    return G.add_goal(root, mem, {"title": title, "targets": list(targets),
                                  "scope": list(scope), "criteria": list(criteria)})


def _check(root, mem, gid, **kw):
    (report,) = G.check_goals(root, mem, [gid], **kw)
    return report


def _statuses(report):
    return [r["status"] for r in report["results"]]


def _age(root: Path, rel: str, ts: float) -> None:
    os.utime(root / rel, (ts, ts))


def _reviews(root: Path) -> Path:
    d = G.reviews_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    assert "warnings" not in goal
    data = json.loads(G.goals_path(repo).read_text())
    assert data["version"] == G.SCHEMA_VERSION and data["next_id"] == 2
    assert G.goals_path(repo) == repo / ".ozagia" / "goals" / "goals.json"
    # Prefixes are added to bare paths of the kind's key parameter.
    g2 = _add(repo, mem, "test_passes:tests/test_core.py::test_other")
    assert g2["id"] == "g2" and g2["criteria"][0]["args"]["key"] == T_OTHER


LONG = "x" * (G.MAX_ARG_CHARS + 1)


@pytest.mark.parametrize("spec, message", [
    ({"targets": ["sym:pkg/core.py::nope"]}, "targets: unknown key"),
    ({"targets": ["pkg/core.py"]}, "is not a graph key"),
    ({"scope": ["sym:pkg/core.py::nope"]}, "scope: unknown key"),
    ({"scope": ["pkg/[z-a].py"]}, "invalid glob"),
    ({"scope": ["src/[]x"]}, "invalid glob"),
    ({"scope": ["file:pkg/[z-a].py"]}, "invalid glob"),
    ({"criteria": ["test_passes:test:tests/test_core.py::test_missing"]}, "unknown key"),
    ({"criteria": ["symbol_absent:sym:pkg/core.py::nope"]}, "unknown key"),
    ({"criteria": ["file_absent:file:pkg/nope.py"]}, "unknown key"),
    ({"criteria": [f"calls:{S_RUN} sym:pkg/core.py::nope"]}, "unknown key"),
    ({"criteria": ["shell:make test"]}, "expected kind:args"),
    ({"criteria": [f"test_passes:{S_RUN}"]}, "must be a test: key"),
    ({"criteria": []}, "at least one criterion"),
    ({"title": ""}, "needs a title"),
    # Input caps.
    ({"targets": ["sym:" + "a" * G.MAX_KEY_CHARS]}, "longer than"),
    ({"scope": ["pkg/" + "a" * G.MAX_KEY_CHARS]}, "longer than"),
    ({"targets": [F_CORE] * 2 + [f"file:x{i}.py" for i in range(G.MAX_KEYS)]},
     "at most"),
    ({"criteria": [{"kind": "symbol_exists", "args": {"key": "sym:" + LONG}}]},
     "longer than"),
    ({"criteria": ["symbol_exists:sym:" + "y" * (3 * G.MAX_ARG_CHARS)]}, "longer than"),
    ({"criteria": [{"kind": "review_resolved",
                    "args": {"packet": "r.json", "finding_id": "f" * 201}}]},
     "longer than"),
])
def test_add_refuses_unknown_keys_bad_criteria_and_oversized_input(repo, mem, spec,
                                                                   message):
    base = {"title": "x", "targets": [F_CORE], "criteria": [f"test_passes:{T_RUN}"]}
    with pytest.raises(G.GoalError, match=message):
        G.add_goal(repo, mem, {**base, **spec})
    assert G.read_goals(repo) == []


def test_absent_subject_allowed_only_for_exists_kinds(repo, mem):
    goal = _add(repo, mem, "symbol_exists:sym:pkg/core.py::later",
                "file_exists:file:pkg/later.py")
    assert [c["kind"] for c in goal["criteria"]] == ["symbol_exists", "file_exists"]


def test_goal_count_cap_and_dropped_compaction(repo, mem, monkeypatch):
    monkeypatch.setattr(G, "MAX_GOALS", 2)
    monkeypatch.setattr(G, "MAX_DROPPED", 1)
    g1 = _add(repo, mem, f"symbol_exists:{S_HELPER}")
    g2 = _add(repo, mem, f"symbol_exists:{S_HELPER}")
    with pytest.raises(G.GoalError, match="at most 2"):
        _add(repo, mem, f"symbol_exists:{S_HELPER}")
    G.drop_goal(repo, g1["id"])
    g3 = _add(repo, mem, f"symbol_exists:{S_HELPER}")
    G.drop_goal(repo, g2["id"])
    # Only the most recently dropped goal is kept.
    ids = [g["id"] for g in G.load_store(repo)["goals"]]
    assert ids == [g2["id"], g3["id"]]


def test_repeated_checks_collapse_in_history(repo, mem):
    goal = _add(repo, mem, f"symbol_exists:{S_HELPER}")
    G.block_goal(repo, goal["id"], reason="hold")
    for _ in range(5):
        _check(repo, mem, goal["id"])
    hist = G.get_goal(repo, goal["id"])["history"]
    checked = [h for h in hist if h["event"] == "checked"]
    assert len(checked) == 1 and checked[0]["repeat"] == 5


# --------------------------------------------------------------------------- #
# test criteria: what counts as evidence
# --------------------------------------------------------------------------- #
def test_test_passes_and_fails_from_the_record(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}", f"test_fails:{T_RUN}")
    assert _statuses(_check(repo, mem, goal["id"])) == ["unknown", "unknown"]
    _pytest(repo, PASSED)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass", "fail"]
    ev = report["results"][0]["evidence"]
    assert ev["run_at"] is not None and ev["run_source"] == "captured"
    assert ev["files_compared"] == 2                 # target file and test file
    _pytest(repo, FAILED)
    assert _statuses(_check(repo, mem, goal["id"])) == ["fail", "pass"]


def test_forged_and_reported_summaries_are_not_evidence(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    forged = 'pytest --version; echo "== 3 passed in 0.10s =="'
    # Through `after` (output the harness handed over): not evidence.
    run = _pytest(repo, "pytest 9.0\n== 3 passed in 0.10s ==\n", command=forged,
                  source="reported")
    assert run.complete is False          # --version runs no test
    _pytest(repo, "2 passed in 0.1s\n", source="reported")
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown"]
    assert "captured" in report["results"][0]["reason"]
    # A captured command line with another command is `compound`, not evidence.
    run = _pytest(repo, PASSED, command=forged)
    assert run.source == "compound"
    # A captured `--version`/`--co` run completes nothing.
    for info in ("--version", "--co", "--collect-only", "--fixtures", "--setup-plan",
                 "-h"):
        run = _pytest(repo, PASSED, command=f"python -m pytest {info} tests")
        assert run.source == "captured" and run.complete is False, info
    assert _statuses(_check(repo, mem, goal["id"])) == ["unknown"]
    # Output printed before the daemon's own head line cannot forge one.
    run = _pytest(repo, PASSED + "[ozagia] pytest failed: exit 2, 0 s, 0 passed, 0 failed, "
                  "0 error, 0 skipped, 1 collection errors\n")
    assert run.complete is False
    run = _pytest(repo, "[ozagia] pytest passed: exit 0, 0 s, 9 passed, 0 failed, 0 error, "
                  "0 skipped\n" + FAILED)
    assert run.complete is True and T_RUN in run.failures


def test_dropped_selector_makes_the_run_filtered(repo):
    run = W.TestRun(command="pytest", prefix=["pytest"],
                    selectors=["tests/test_a.py::test_p[a b]"], failures={},
                    complete=True, source="captured")
    entry = W.run_record(run)
    assert entry["selectors"] == [] and entry["filtered"] is True
    rec = {"tests": {}, "runs": [entry]}
    got = W.recorded_outcome(rec, "test:tests/test_other.py::test_never_ran",
                             evidence=True)
    assert got["outcome"] == "unknown"
    # Too many selectors: filtered; too many failures: incomplete, capped.
    many = W.run_record(W.TestRun(
        command="pytest", prefix=["pytest"],
        selectors=[f"tests/t{i}.py" for i in range(W.MAX_RUN_KEYS + 1)],
        failures={f"test:tests/t.py::t{i}": {"outcome": "failed"}
                  for i in range(W.MAX_RUN_KEYS + 5)},
        complete=True, source="captured"))
    assert many["filtered"] is True and many["complete"] is False
    assert len(many["selectors"]) == W.MAX_RUN_KEYS
    assert len(many["failed"]) == W.MAX_RUN_KEYS


def test_ignore_and_addopts_filter_a_run(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    for command in ("pytest --ignore=tests/test_core.py tests",
                    "pytest --ignore tests/test_core.py tests",
                    "pytest --ignore-glob=*core* tests",
                    "pytest -o addopts=-ktest_other tests"):
        assert _pytest(repo, PASSED, command=command).filtered, command
    assert _pytest(repo, PASSED, env={"PYTEST_ADDOPTS": "-k other"}).filtered
    assert _statuses(_check(repo, mem, goal["id"])) == ["unknown"]
    _write(repo / "pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-k other"\n')
    assert _pytest(repo, PASSED).filtered
    _write(repo / "pyproject.toml", '[tool.pytest.ini_options]\naddopts = "-p no:cacheprovider"\n')
    assert not _pytest(repo, PASSED).filtered
    _write(repo / "pytest.ini", "[pytest]\naddopts = --deselect tests/test_core.py::test_run\n")
    assert _pytest(repo, PASSED).filtered


def test_filtered_run_does_not_pass_a_test(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    _pytest(repo, PASSED, command="python -m pytest -q -k other tests")
    assert _statuses(_check(repo, mem, goal["id"])) == ["unknown"]


def test_deleted_test_is_unknown(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}", targets=[S_HELPER])
    _pytest(repo, PASSED)
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass"]
    _write(repo / "tests" / "test_core.py", TESTS.replace(
        "def test_run():\n    assert run(1) == 4\n", ""))
    _pytest(repo, "[ozagia] pytest passed: exit 0, 0.1 s, 1 passed, 0 failed, 0 error, "
            "0 skipped\n")
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown"]
    assert "no longer in the graph" in report["results"][0]["reason"]


def test_errors_are_not_failures(repo, mem):
    goal = _add(repo, mem, f"test_fails:{T_RUN}", f"test_passes:{T_RUN}")
    _pytest(repo, ERRORED)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown", "fail"]
    assert "errored" in report["results"][0]["reason"]
    # A collection or import error: the run did not finish.
    _pytest(repo, COLLECT_ERROR)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown", "unknown"]
    assert "did not finish" in report["results"][0]["reason"]
    _pytest(repo, FAILED)
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass", "fail"]


# --------------------------------------------------------------------------- #
# staleness: the working-tree fingerprint
# --------------------------------------------------------------------------- #
def test_target_edit_after_the_run_is_stale(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    _pytest(repo, PASSED)
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass"]
    text = (repo / "pkg" / "core.py").read_text()
    _write(repo / "pkg" / "core.py", text.replace("return 0", "return 1"))
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["stale"]
    assert report["results"][0]["evidence"]["changed_files"] == ["pkg/core.py"]
    _pytest(repo, PASSED)                        # rerun: fresh again
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass"]
    # The rerun saw the edited content: going back to the old one is a change.
    _write(repo / "pkg" / "core.py", text)
    assert _statuses(_check(repo, mem, goal["id"])) == ["stale"]


def test_backdated_mtime_is_still_stale(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    before = (repo / "pkg" / "core.py").stat().st_mtime
    _pytest(repo, PASSED)
    text = (repo / "pkg" / "core.py").read_text()
    _write(repo / "pkg" / "core.py", text.replace("return 0", "return 9"))
    _age(repo, "pkg/core.py", before - 3600)
    _age(repo, "tests/test_core.py", before - 3600)
    assert _statuses(_check(repo, mem, goal["id"])) == ["stale"]


def test_scope_file_edit_makes_stale(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}", targets=[S_HELPER],
                scope=["pkg/**"])
    _pytest(repo, PASSED)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass"]
    # pkg/__init__.py, core.py, other.py and the test file.
    assert report["results"][0]["evidence"]["files_compared"] == 4
    _write(repo / "docs" / "notes.md", "outside the scope\n")
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass"]
    _write(repo / "pkg" / "other.py", "VALUE = 2\n")
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["stale"]
    assert report["results"][0]["evidence"]["changed_files"] == ["pkg/other.py"]
    # A new file under a scope glob counts too.
    _pytest(repo, PASSED)
    _write(repo / "pkg" / "new.py", "X = 1\n")
    assert _statuses(_check(repo, mem, goal["id"])) == ["stale"]


def test_deleted_target_file_is_stale(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}", targets=[F_OTHER])
    _pytest(repo, PASSED)
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass"]
    (repo / "pkg" / "other.py").unlink()
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["stale"]
    assert report["results"][0]["evidence"]["changed_files"] == ["pkg/other.py"]


def test_edit_during_the_run_is_stale(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    fp = FP.tree_fingerprint(repo)                 # the run starts
    _write(repo / "pkg" / "core.py", CORE.replace("return 0", "return 5"))
    _pytest(repo, PASSED, fp=fp)                   # ... and ends
    assert _statuses(_check(repo, mem, goal["id"])) == ["stale"]


def test_dirty_files_at_run_time_are_hashed(repo, mem):
    _write(repo / "pkg" / "core.py", CORE.replace("return 0", "return 7"))
    _write(repo / "pkg" / "fresh.py", "Y = 1\n")
    fp = FP.tree_fingerprint(repo)
    assert set(fp["dirty"]) == {"pkg/core.py", "pkg/fresh.py"}
    assert not any(p.startswith(".ozagia") for p in fp["dirty"])
    goal = _add(repo, mem, f"test_passes:{T_RUN}", scope=["pkg/*.py"])
    _pytest(repo, PASSED, fp=fp)
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass"]
    (repo / "pkg" / "fresh.py").unlink()
    assert _statuses(_check(repo, mem, goal["id"])) == ["stale"]


def test_no_fingerprint_outside_git_is_unknown(repo):
    import shutil
    shutil.rmtree(repo / ".git")
    mem = _memory(repo)
    assert FP.tree_fingerprint(repo) is None
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    _pytest(repo, PASSED)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown"]
    assert "fingerprint" in report["results"][0]["reason"]


def test_stale_or_unknown_does_not_regress(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}")
    _pytest(repo, PASSED)
    assert _check(repo, mem, goal["id"])["status"] == "met"
    _write(repo / "pkg" / "core.py", CORE + "\n# edit\n")
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["stale"] and report["status"] == "met"


def test_old_test_record_without_runs_loads(repo):
    path = state_path(repo, *W.TEST_RECORD)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tests": {}, "last": None}))
    assert W.load_test_record(repo)["runs"] == []
    # Runs written before `source` existed are `reported`: not evidence.
    path.write_text(json.dumps({"tests": {}, "last": None, "runs": [
        {"ts": 1.0, "selectors": [], "failed": [], "complete": True}]}))
    rec = W.load_test_record(repo)
    assert rec["runs"][0]["source"] == "reported"
    assert W.recorded_outcome(rec, T_RUN, evidence=True)["outcome"] == "unknown"
    assert W.recorded_outcome(rec, T_RUN)["outcome"] == "passed"


# --------------------------------------------------------------------------- #
# graph-backed kinds
# --------------------------------------------------------------------------- #
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


def test_no_graph_gives_unknown(repo, mem):
    goal = _add(repo, mem, f"symbol_absent:{S_LEGACY}", f"symbol_exists:{S_HELPER}",
                f"test_passes:{T_RUN}", f"unchanged:{S_LEGACY}", f"unchanged:{F_CORE}",
                f"file_exists:{F_CORE}", f"no_callers:{S_LEGACY}")
    ev = G.Evaluator(repo, None)
    res = ev.evaluate(goal, {"kind": "symbol_absent", "args": {"key": S_LEGACY}})
    assert res["status"] == "unknown" and "no graph" in res["reason"]
    report = _check(repo, None, goal["id"])
    assert _statuses(report) == ["unknown", "unknown", "unknown", "unknown", "pass",
                                 "pass", "unknown"]
    assert report["graph"] == "no graph"


def test_refresh_failure_gives_unknown(repo, mem, monkeypatch):
    goal = _add(repo, mem, f"symbol_exists:{S_LEGACY}", f"file_exists:{F_CORE}")
    _write(repo / "pkg" / "core.py", CORE.replace("def legacy():\n    return 0\n", ""))

    def boom(*_a, **_k):
        raise RuntimeError("index broken")
    monkeypatch.setattr(mem, "refresh", boom)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown", "pass"]
    assert "refresh failed: RuntimeError: index broken" in report["results"][0]["reason"]
    # --no-refresh: evaluated, and marked so in the evidence.
    report = _check(repo, mem, goal["id"], refresh=False)
    assert _statuses(report) == ["pass", "pass"]
    assert report["results"][0]["evidence"]["graph"] == "not refreshed"


def test_partial_symbol_graph_gives_unknown(repo, mem, monkeypatch):
    goal = _add(repo, mem, f"not_calls:{S_RUN} {S_LEGACY}", f"calls:{S_RUN} {S_LEGACY}")
    # The tag parser skipped `legacy`; the graph still has it.
    partial = {"nodes": {"pkg/core.py::run": {}, "pkg/core.py::helper": {}}, "edges": []}
    monkeypatch.setattr(G.Evaluator, "symbol_graph", lambda self: (partial, None))
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown", "unknown"]
    assert report["results"][0]["evidence"]["in_graph_only"] == ["pkg/core.py::legacy"]
    # Gone from both: no edge.
    _write(repo / "pkg" / "core.py", CORE.replace("def legacy():\n    return 0\n", ""))
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass", "fail"]


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


def test_lint_clean_is_unknown_and_warned(repo, mem):
    goal = _add(repo, mem, f"lint_clean:{F_CORE}")
    assert goal["warnings"] and "cannot currently pass" in goal["warnings"][0]
    assert "warnings" not in G.get_goal(repo, goal["id"])      # not stored
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
    with pytest.raises(G.GoalError, match="a value needs an attr"):
        G.add_goal(repo, mem, {"title": "x", "criteria": [
            {"kind": "ops_has", "args": {"key": key, "value": "nginx:1.25"}}]})


# --------------------------------------------------------------------------- #
# review packets
# --------------------------------------------------------------------------- #
_STAMP = [0]


def _packet(path: Path, findings: list[dict], *, root: Path | None = None,
            generator: str | None = G.REVIEW_GENERATOR) -> Path:
    """A packet shaped like `review_repo`'s: generator, root, a later
    `generated_at` on every call, the working tree's fingerprint now, and
    the checks behind the finding types used here (unreferenced_symbol,
    stale_doc_reference) marked as run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _STAMP[0] += 1
    sections = {}
    for name, check, ftype in (("health", "unreferenced_symbols", "unreferenced_symbol"),
                               ("docs", "links_and_code_spans", "stale_doc_reference")):
        ids = [f["id"] for f in findings if f.get("type") == ftype]
        sections[name] = {"checked": [check], "ids": ids,
                          "ids_by_type": {ftype: ids} if ids else {}}
    data = {"schema": G.PACKET_SCHEMA, "truth_assessed": False,
            "root": str(root or path.parents[2]), "head_commit": None,
            "generated_at": f"2026-11-16T10:00:{_STAMP[0] % 60:02d}.{_STAMP[0]:06d}Z",
            "sections": sections, "findings": findings}
    if generator is not None:
        data["generator"] = generator
    if (path.parents[2] / ".git").exists():
        data["tree"] = FP.tree_fingerprint(path.parents[2])
    path.write_text(json.dumps(data))
    return path


FINDING = {
    "id": "rr-0f3a9c", "section": "health", "type": "unreferenced_symbol",
    "severity": "minor", "keys": [S_LEGACY], "evidence": {"callers": 0},
    "confidence": 0.5,
    "suggested_goal": {"title": "Remove pkg.core.legacy", "targets": [S_LEGACY],
                       "criteria": [f"symbol_absent:{S_LEGACY}",
                                    {"kind": "test_passes", "args": {"key": T_RUN}}]},
}


def test_review_resolved_reads_the_newest_packet(repo, mem):
    packet = _packet(_reviews(repo) / "latest.json", [FINDING])
    goal = _add(repo, mem, f"review_resolved:{packet} rr-0f3a9c", targets=[S_LEGACY])
    args = goal["criteria"][0]["args"]
    assert args["packet"] == ".ozagia/reviews/latest.json"
    assert args["type"] == "unreferenced_symbol" and args["keys"] == [S_LEGACY]
    assert set(args["anchor"]) == {"generated_at", "head_commit", "sha256"}
    assert _statuses(_check(repo, mem, goal["id"])) == ["fail"]
    # A later review without the finding resolves it.
    _packet(packet, [])
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass"], report
    assert report["results"][0]["evidence"]["state"] == "absent"
    # The newest timestamped packet counts when latest.json is gone.
    packet.unlink()
    _packet(_reviews(repo) / "repo_review-20990101T000000Z-x.json", [FINDING])
    assert _statuses(_check(repo, mem, goal["id"])) == ["fail"]
    # No packet at all.
    for p in _reviews(repo).iterdir():
        p.unlink()
    assert _statuses(_check(repo, mem, goal["id"])) == ["unknown"]
    with pytest.raises(G.GoalError, match="cannot open"):
        _add(repo, mem, f"review_resolved:{packet} rr-0f3a9c")


def test_review_resolved_needs_a_later_review_of_this_root(repo, mem):
    reviews = _reviews(repo)
    packet = _packet(reviews / "latest.json", [FINDING])
    goal = _add(repo, mem, f"review_resolved:{packet} rr-0f3a9c", targets=[S_LEGACY])
    anchor = goal["criteria"][0]["args"]["anchor"]
    # The finding id must be in the named packet.
    with pytest.raises(G.GoalError, match="not in"):
        _add(repo, mem, f"review_resolved:{packet} rr-made-up")
    # Not written by ozagia's review: refused at creation, unknown at check.
    unsigned = _packet(reviews / "unsigned.json", [FINDING], generator=None)
    with pytest.raises(G.GoalError, match="not written by ozagia's review"):
        _add(repo, mem, f"review_resolved:{unsigned} rr-0f3a9c")
    _packet(packet, [], generator=None)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown"]
    assert "not written by ozagia's review" in report["results"][0]["reason"]
    # Another root.
    _packet(packet, [], root=Path("/somewhere/else"))
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown"]
    assert "reviews /somewhere/else" in report["results"][0]["reason"]
    # Not later than the anchor (an older generated_at, or the same bytes).
    _packet(packet, [])
    raw = json.loads(packet.read_text())
    raw["generated_at"] = anchor["generated_at"]
    packet.write_text(json.dumps(raw))
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown"] and "not a later review" in \
        report["results"][0]["reason"]
    # A check that did not run: not_checked -> unknown.
    raw["generated_at"] = "2099-01-01T00:00:00.000000Z"
    raw["sections"] = {}
    packet.write_text(json.dumps(raw))
    assert _statuses(_check(repo, mem, goal["id"])) == ["unknown"]
    # The finding id among a section's ids (capped out of findings) is present.
    raw["sections"] = {"health": {"checked": ["unreferenced_symbols"],
                                  "ids": ["rr-0f3a9c"]}}
    packet.write_text(json.dumps(raw))
    assert _statuses(_check(repo, mem, goal["id"])) == ["fail"]


def test_review_resolved_is_stale_after_a_target_edit(repo, mem):
    packet = _packet(_reviews(repo) / "latest.json", [FINDING])
    goal = _add(repo, mem, f"review_resolved:{packet} rr-0f3a9c", targets=[S_LEGACY])
    # A packet with the working tree's fingerprint (as review_repo writes).
    raw = json.loads(_packet(packet, []).read_text())
    raw["tree"] = FP.tree_fingerprint(repo)
    packet.write_text(json.dumps(raw))
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass"]
    assert report["results"][0]["evidence"]["basis"] == "content"
    # An edit after the review, even with the mtime set back.
    before = (repo / "pkg" / "core.py").stat().st_mtime
    _write(repo / "pkg" / "core.py", CORE + "\n# edited\n")
    _age(repo, "pkg/core.py", before - 100)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["stale"], report
    assert report["results"][0]["evidence"]["changed_files"] == ["pkg/core.py"]
    # Without a fingerprint: by mtime against generated_at (weaker).
    del raw["tree"]
    raw["generated_at"] = "2000-01-01T00:00:00.000000Z"
    packet.write_text(json.dumps(raw))
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["stale"]
    assert report["results"][0]["evidence"]["basis"] == "mtime"


def test_packets_outside_the_reviews_dir_are_refused(repo, mem, tmp_path):
    outside = _packet(tmp_path / "review.json", [FINDING])
    in_root = _packet(repo / "review.json", [FINDING])
    for path in (outside, in_root, "../review.json", "~/review.json"):
        with pytest.raises(G.GoalError, match="not under the reviews directory"):
            _add(repo, mem, f"review_resolved:{path} rr-0f3a9c")
        with pytest.raises(G.GoalError, match="not under the reviews directory"):
            G.from_review(repo, mem, str(path), "rr-0f3a9c")
    # A symlink inside the reviews dir to a file outside it.
    link = _reviews(repo) / "link.json"
    link.symlink_to(outside)
    with pytest.raises(G.GoalError, match="not under the reviews directory"):
        _add(repo, mem, f"review_resolved:{link} rr-0f3a9c")
    # A stored criterion whose newest packet became a link out is `unknown`.
    good = _packet(_reviews(repo) / "latest.json", [FINDING])
    goal = _add(repo, mem, f"review_resolved:{good} rr-0f3a9c")
    good.unlink()
    good.symlink_to(outside)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown"]
    assert "not under the reviews directory" in report["results"][0]["reason"]
    assert G.read_goals(repo)[0]["id"] == goal["id"] and len(G.read_goals(repo)) == 1


def test_fifo_directory_and_oversized_packets_are_refused(repo, mem, monkeypatch):
    reviews = _reviews(repo)
    fifo = reviews / "pipe.json"
    os.mkfifo(fifo)
    t0 = time.monotonic()
    with pytest.raises(G.GoalError, match="not a regular file"):
        _add(repo, mem, f"review_resolved:{fifo} rr-0f3a9c")
    assert time.monotonic() - t0 < 5             # never blocks on the FIFO
    (reviews / "dir.json").mkdir()
    with pytest.raises(G.GoalError, match="not a regular file"):
        _add(repo, mem, f"review_resolved:{reviews / 'dir.json'} rr-0f3a9c")
    big = _packet(reviews / "big.json", [FINDING])
    monkeypatch.setattr(G, "MAX_PACKET_BYTES", 64)
    with pytest.raises(G.GoalError, match="larger than 64 bytes"):
        _add(repo, mem, f"review_resolved:{big} rr-0f3a9c")
    with pytest.raises(G.GoalError, match="larger than 64 bytes"):
        G.from_review(repo, mem, str(big), "rr-0f3a9c")
    assert G.read_goals(repo) == []


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
    # A missing harness never matches a claim.
    assert not G.claimed_by(got, None, "s1") and not G.claimed_by(got, "", "s1")
    assert G.edit_hints(repo, None, "s1", ["pkg/core.py"], [S_HELPER]) == []
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
    # An invalid glob (a store written before globs were checked) matches
    # nothing instead of raising.
    assert not G.in_scope(["[z-a].py"], "b.py")
    assert G.in_scope(["[z-a].py", "pkg/**"], "pkg/b.py")
    with pytest.raises(G.GoalError, match="invalid glob"):
        G.check_glob("src/[]x")


# --------------------------------------------------------------------------- #
# the daemon: edit hints, scope note, `goals` op, working set
# --------------------------------------------------------------------------- #
def _edit(state, rel, old, new, *, harness="claude", session="s1"):
    resp = state.dispatch({"op": "tool", "tool": "edit", "harness": harness,
                           "session": session,
                           "input": {"path": rel, "oldString": old, "newString": new}})
    assert resp["ok"] and resp["handled"] and not resp["is_error"], resp
    return resp


def _goal_lines(resp):
    return [x for x in resp["output"].splitlines() if x.startswith("[ozagia] goal")]


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
    lines = _goal_lines(resp)
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
    assert _goal_lines(resp) == [
        "[ozagia] goal scope note: pkg/other.py is outside the scope of the goals "
        f"this session claimed ({goal['id']}) (advisory)"]
    assert (repo / "pkg" / "other.py").read_text() == "VALUE = 2\n"
    assert st.stats()["goal_hints"] == 2
    # `after` of a built-in edit gets the lines in `append`.
    body = st._goal_hints("after", {"append": "", "meta": {}}, "claude", "s1",
                          ["pkg/core.py"], [S_HELPER])
    assert body["append"].startswith(f"[ozagia] goal {goal['id']}")


def test_scope_note_uses_the_union_of_claimed_scopes(repo, mem, tmp_path):
    from ozagia.daemon.server import DaemonState
    g1 = _add(repo, mem, f"symbol_exists:{S_HELPER}", targets=[S_HELPER],
              scope=["pkg/core.py"])
    g2 = _add(repo, mem, "file_exists:file:pkg/other.py", targets=["file:pkg/other.py"],
              scope=["pkg/other.py"])
    for g in (g1, g2):
        G.claim_goal(repo, g["id"], "claude", "s1")
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    # Inside g2's scope: no note, though outside g1's.
    resp = _edit(st, "pkg/other.py", "VALUE = 1", "VALUE = 2")
    assert not any("scope note" in x for x in _goal_lines(resp)), resp["output"]
    resp = _edit(st, "tests/test_core.py", "assert True", "assert 1")
    notes = [x for x in _goal_lines(resp) if "scope note" in x]
    assert notes == ["[ozagia] goal scope note: tests/test_core.py is outside the scope "
                     f"of the goals this session claimed ({g1['id']}, {g2['id']}) "
                     "(advisory)"]
    # A claimed goal without a scope leaves the work unbounded: no note.
    g3 = _add(repo, mem, f"symbol_exists:{S_RUN}", targets=[S_RUN])
    G.claim_goal(repo, g3["id"], "claude", "s1")
    resp = _edit(st, "tests/test_core.py", "assert 1", "assert 2")
    assert not any("scope note" in x for x in _goal_lines(resp))


def test_claims_beyond_the_hint_limit_are_named(repo, mem):
    goals = [_add(repo, mem, f"symbol_exists:{S_HELPER}", targets=[F_CORE],
                  title=f"G{i}") for i in range(G.HINT_GOALS + 2)]
    for g in goals:
        G.claim_goal(repo, g["id"], "claude", "s1")
    lines = G.edit_hints(repo, "claude", "s1", ["pkg/core.py"], [S_HELPER])
    detailed = [x for x in lines if "this edit touched target" in x]
    assert len(detailed) == G.HINT_GOALS
    extra = [g["id"] for g in goals[G.HINT_GOALS:]]
    assert lines[-1] == (f"[ozagia] goals: this edit also touched targets of claimed "
                         f"goal(s) {', '.join(extra)}, not detailed (more than "
                         f"{G.HINT_GOALS} claimed); ozagia goal list")


def test_daemon_goals_op_and_working_set(repo, mem, tmp_path):
    from ozagia.daemon.server import DaemonState
    goal = _add(repo, mem, f"test_passes:{T_RUN}", targets=[S_HELPER],
                title="Secret title text")
    G.claim_goal(repo, goal["id"], "claude", "s1")
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    before = G.goals_path(repo).read_bytes()
    resp = st.dispatch({"op": "goals", "harness": "claude", "session": "s1"})
    assert resp["ok"] and resp["goals"][0]["id"] == goal["id"]
    assert resp["goals"][0]["mine"] is True
    assert resp["goals"][0]["unmet"][0]["status"] == "unchecked"
    assert f"{goal['id']} [open, claimed by this session]" in resp["text"]
    assert G.goals_path(repo).read_bytes() == before        # read-only
    # Without a harness, nothing is "this session's".
    resp = st.dispatch({"op": "goals", "session": "s1"})
    assert resp["goals"][0]["mine"] is False
    assert "claimed by this session" not in resp["text"]
    _edit(st, "pkg/core.py", "return x + 1", "return x + 2")
    ws = st.dispatch({"op": "working_set", "harness": "claude", "session": "s1"})
    assert "goals (unmet criteria" in ws["text"] and f"{goal['id']} [open" in ws["text"]
    assert "Secret title text" in ws["text"]
    # `free_text: false`: ids, statuses and criterion labels, no titles.
    ws = st.dispatch({"op": "working_set", "harness": "claude", "session": "s1",
                      "free_text": False})
    assert f"{goal['id']} [open, claimed by this session]: unmet c1 test_passes" in ws["text"]
    assert "Secret title text" not in ws["text"]
    brief = st.dispatch({"op": "working_set", "since": True, "harness": "claude",
                         "session": "s1"})
    assert f"{goal['id']} [open, claimed by this session]" in brief["text"]
    assert len(brief["text"]) <= W.BRIEF_MAX_CHARS


def test_working_set_op_reads_goals_outside_the_graph_lock(repo, mem, tmp_path,
                                                           monkeypatch):
    from ozagia.daemon.server import DaemonState
    _add(repo, mem, f"test_passes:{T_RUN}", targets=[S_HELPER])
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    _edit(st, "pkg/core.py", "return x + 1", "return x + 2")
    held: list[bool] = []
    real = G.brief_lines

    def spy(*a, **k):
        held.append(st.lock.locked())
        return real(*a, **k)
    monkeypatch.setattr(G, "brief_lines", spy)
    ws = st.dispatch({"op": "working_set", "harness": "claude", "session": "s1"})
    assert "goals (unmet criteria" in ws["text"]
    assert held == [False]


def test_store_read_is_cached(repo, mem, monkeypatch):
    goal = _add(repo, mem, f"symbol_exists:{S_HELPER}")
    G.read_goals(repo)
    parses: list[int] = []
    real = G._parse_store

    def spy(text, path):
        parses.append(1)
        return real(text, path)
    monkeypatch.setattr(G, "_parse_store", spy)
    for _ in range(5):
        assert G.read_goals(repo)[0]["id"] == goal["id"]
        G.edit_hints(repo, "claude", "s1", ["pkg/core.py"], [S_HELPER])
    assert parses == []
    G.claim_goal(repo, goal["id"], "claude", "s1")      # a write: parsed again
    assert G.read_goals(repo)[0]["claimed_by"][0]["session"] == "s1"
    assert len(parses) >= 1


def test_daemon_records_captured_runs_with_a_fingerprint(repo, mem, tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    command = f"{sys.executable} -m pytest -q tests"
    assert st._pytest_start("tool", "shell", {"command": "ls"}) is None
    assert st._pytest_start("after", "shell", {"command": command}) is None
    started = st._pytest_start("tool", "shell", {"command": command})
    assert started is not None and started[1]["head"]
    state = st.session_state({"harness": "claude", "session": "s1"})
    body = {"handled": True, "output": PASSED, "meta": {"intent": "shell_pytest"}}
    st._observe("tool", "shell", {"command": command}, {}, body, state, started)
    body = {"handled": True, "output": PASSED, "meta": {"intent": "shell_compound"}}
    st._observe("tool", "shell", {"command": command + " && echo x"}, {}, body, state)
    st._observe("after", "shell", {"command": command}, {"output": PASSED},
                {"append": ""}, state)
    runs = W.load_test_record(repo)["runs"]
    assert [r["source"] for r in runs] == ["captured", "compound", "reported"]
    assert runs[0]["ts"] == started[0] and runs[0]["tree"]["head"] == started[1]["head"]
    assert "tree" not in runs[1] and "tree" not in runs[2]


# --------------------------------------------------------------------------- #
# the brief
# --------------------------------------------------------------------------- #
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


def test_brief_puts_goals_after_failing_tests_with_a_reserved_share(repo, mem):
    goal = _add(repo, mem, f"symbol_exists:{S_HELPER}", title="Short")
    _pytest(repo, FAILED, source="reported")             # a failing test
    text = W.session_brief(repo, jobs_enabled=False)
    assert text.index("failing tests") < text.index("goals (") < text.index(
        W.MCP_TOOLS_LINE)
    # Over the cap, the text before the goals is cut, not the goals.
    for i in range(30):
        _write(repo / "pkg" / f"dirty_{i:02d}_{'x' * 40}.py", "")
    cap = 800
    full = W.session_brief(repo, 5000, jobs_enabled=False)
    assert len(full) > cap
    text = W.session_brief(repo, cap, jobs_enabled=False)
    assert len(text) <= cap and text.endswith(W.MCP_TOOLS_LINE)
    assert f"  {goal['id']} [open, unclaimed] Short" in text
    assert "failing tests" not in text and "\n...\n" in text
    lines = W.session_brief(repo, cap, jobs_enabled=False,
                            goal_lines=["goals:", "  " + "y" * 400]).splitlines()
    assert "goals:" not in lines                          # over the share: left out


# --------------------------------------------------------------------------- #
# from-review
# --------------------------------------------------------------------------- #
def test_from_review_with_a_fixture_packet(repo, mem):
    packet = _packet(_reviews(repo) / "latest.json", [FINDING, {
        "id": "rr-other", "section": "docs", "type": "stale_doc_reference",
        "severity": "note", "keys": [S_HELPER], "evidence": {}, "confidence": 0.9}])
    # The packet defaults to the reviews dir's latest.json.
    goal = G.from_review(repo, mem, None, "rr-0f3a9c")
    assert goal["title"] == "Remove pkg.core.legacy"
    assert goal["targets"] == [S_LEGACY]
    assert goal["source"] == {"review_packet": ".ozagia/reviews/latest.json",
                              "finding_id": "rr-0f3a9c"}
    assert [c["kind"] for c in goal["criteria"]] == [
        "symbol_absent", "test_passes", "review_resolved"]
    assert G.tracking_goals(repo) == {"rr-0f3a9c": goal["id"]}
    with pytest.raises(G.GoalError, match="already tracks finding"):
        G.from_review(repo, mem, str(packet), "rr-0f3a9c")
    with pytest.raises(G.GoalError, match="not in"):
        G.from_review(repo, mem, str(packet), "rr-missing")
    # No suggested_goal: the finding's keys are the targets.
    g2 = G.from_review(repo, mem, str(packet), "rr-other")
    assert g2["targets"] == [S_HELPER] and g2["criteria"][0]["kind"] == "review_resolved"
    # Resolve it: remove the symbol, rerun the tests and the review.
    _write(repo / "pkg" / "core.py", CORE.replace("def legacy():\n    return 0\n", ""))
    _pytest(repo, PASSED)
    _packet(packet, [])
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass", "pass", "pass"] and report["status"] == "met"
    bad = _reviews(repo) / "bad.json"
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


def test_lock_failure_raises(repo, mem, monkeypatch):
    import errno
    import fcntl
    goal = _add(repo, mem, f"symbol_exists:{S_HELPER}")
    before = G.goals_path(repo).read_bytes()

    def nolock(*_a, **_k):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(fcntl, "flock", nolock)
    with pytest.raises(G.GoalError, match="cannot lock the goal store"):
        G.claim_goal(repo, goal["id"], "claude", "s1")
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "fcntl", None)       # no fcntl at all
    with pytest.raises(G.GoalError, match="needs a file lock"):
        G.claim_goal(repo, goal["id"], "claude", "s1")
    monkeypatch.undo()
    # Another writer holding the lock: a timeout, not an unlocked write.
    monkeypatch.setattr(G, "LOCK_TIMEOUT_S", 0.2)
    with open(G.goals_path(repo).parent / G.LOCK_FILE, "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        code = ("import sys\nfrom ozagia.core import goals as G\nG.LOCK_TIMEOUT_S = 0.2\n"
                "try:\n    G.claim_goal(sys.argv[1], 'g1', 'h', 's')\n"
                "except G.GoalError as e:\n    print(e)\n")
        out = subprocess.run([sys.executable, "-c", code, str(repo)], capture_output=True,
                             text=True, timeout=60).stdout
    assert "is locked by another writer" in out
    assert G.goals_path(repo).read_bytes() == before


def test_check_evaluates_outside_the_store_lock(repo, mem, monkeypatch):
    goal = _add(repo, mem, f"symbol_exists:{S_HELPER}", f"file_exists:{F_CORE}")
    real = G.Evaluator.evaluate
    done: list[bool] = []

    def slow(self, g, crit):
        if not done:
            # A claim from another thread while the check evaluates.
            t = threading.Thread(target=G.claim_goal,
                                 args=(repo, goal["id"], "claude", "s1"))
            t.start()
            t.join(10)
            done.append(not t.is_alive())
        return real(self, g, crit)
    monkeypatch.setattr(G.Evaluator, "evaluate", slow)
    report = _check(repo, mem, goal["id"])
    assert done == [True]
    assert _statuses(report) == ["pass", "pass"] and report["status"] == "met"
    got = G.get_goal(repo, goal["id"])
    assert [c["session"] for c in got["claimed_by"]] == ["s1"]   # not lost
    assert [c["last"]["status"] for c in got["criteria"]] == ["pass", "pass"]


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
    assert main(["goal", "add", "--root", root, "--title", "Lint",
                 "--criterion", f"lint_clean:{F_CORE}"]) == 0
    assert "warning: c1 lint_clean cannot currently pass" in capsys.readouterr().err
    assert main(["goal", "drop", "--root", root, "g3"]) == 0
    assert main(["goal", "claim", "--root", root, "g1", "--harness", "codex",
                 "--session", "abc"]) == 0
    assert "g1 claimed for codex/abc" in capsys.readouterr().out
    _pytest(repo, PASSED)
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
    assert statuses == {"g1": "met", "g2": "dropped", "g3": "dropped"}
    packet = _packet(_reviews(repo) / "review.json", [FINDING])
    assert main(["goal", "from-review", "--root", root, str(packet), "rr-0f3a9c",
                 "--json"]) == 0
    got = json.loads(capsys.readouterr().out)["goal"]
    assert got["source"]["finding_id"] == "rr-0f3a9c"
    outside = _packet(tmp_path / "review.json", [FINDING])
    assert main(["goal", "from-review", "--root", root, str(outside), "rr-0f3a9c"]) == 2
    assert "not under the reviews directory" in capsys.readouterr().err
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


# --------------------------------------------------------------------------- #
# failure classes (`ozagia.core.failures`): runs that say nothing about the code
# --------------------------------------------------------------------------- #
ENV_ERROR = ("[ozagia] pytest interrupted: exit 2, 0.1 s, 0 passed, 0 failed, 0 error, "
             "0 skipped, 1 collection errors\n"
             "[ozagia] failure class environment, module numpy: missing module numpy "
             "(third-party) under /usr/bin/python3\n"
             "[ozagia] collection error file:tests/test_core.py: ModuleNotFoundError\n")
ENV_IN_BODY = ("[ozagia] pytest failed: exit 1, 0.1 s, 1 passed, 1 failed, 0 error, "
               "0 skipped\n"
               "[ozagia] failure class environment, module yaml: missing module yaml "
               "(third-party)\n"
               "[ozagia] failed test:tests/test_core.py::test_run: ModuleNotFoundError\n")


def test_environment_runs_give_unknown_with_class_and_module(repo, mem):
    goal = _add(repo, mem, f"test_passes:{T_RUN}", f"test_fails:{T_RUN}")
    _pytest(repo, ENV_ERROR)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown", "unknown"]
    for res in report["results"]:
        assert "failure class is environment (module numpy)" in res["reason"]
        assert res["evidence"]["failure_class"] == "environment"
        assert res["evidence"]["failure_module"] == "numpy"
    # A test that failed because its body could not import a module is not
    # a failure of the code either.
    _pytest(repo, ENV_IN_BODY)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown", "unknown"]
    assert "(module yaml)" in report["results"][1]["reason"]
    # A passing run decides again.
    _pytest(repo, PASSED)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass", "fail"]
    assert report["results"][0]["evidence"]["failure_class"] == "passed"


def test_harness_and_third_party_collection_runs_give_unknown(repo, mem):
    goal = _add(repo, mem, f"test_fails:{T_RUN}")
    harness = FAILED.replace("exit 1, 0.1 s, 1 passed, 1 failed, 0 error, 0 skipped\n",
                             "exit 1, 0.1 s, 1 passed, 1 failed, 0 error, 0 skipped\n"
                             "[ozagia] failure class harness: the runner was not found\n")
    _pytest(repo, harness)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["unknown"]
    assert "failure class is harness" in report["results"][0]["reason"]
    third = FAILED.replace("0 skipped\n", "0 skipped\n[ozagia] failure class collection, "
                           "module requests: import error\n", 1)
    _pytest(repo, third)
    assert _statuses(_check(repo, mem, goal["id"])) == ["unknown"]
    # A collection failure of a first-party module still decides as before.
    first = FAILED.replace("0 skipped\n", "0 skipped\n[ozagia] failure class collection, "
                           "module pkg.core: import error\n", 1)
    _pytest(repo, first)
    assert _statuses(_check(repo, mem, goal["id"])) == ["pass"]


MIXED = ("[ozagia] pytest failed: exit 1, 0.1 s, 0 passed, 2 failed, 0 error, 0 skipped\n"
         "[ozagia] failure class assertion: 1 failed with assertion output; 1 more "
         "with an environment cause (yaml)\n"
         "[ozagia] failure item environment test:tests/test_core.py::test_other, "
         "module yaml\n"
         "[ozagia] failed test:tests/test_core.py::test_run: assert 3 == 4\n"
         "[ozagia] failed test:tests/test_core.py::test_other: ModuleNotFoundError: "
         "No module named 'yaml'\n")


def test_not_evidence_is_decided_per_test(repo, mem):
    """One test with a missing module does not make another test's
    assertion failure `unknown`."""
    goal = _add(repo, mem, f"test_fails:{T_RUN}", f"test_fails:{T_OTHER}",
                f"test_passes:{T_OTHER}")
    _pytest(repo, MIXED)
    report = _check(repo, mem, goal["id"])
    assert _statuses(report) == ["pass", "unknown", "unknown"]
    run_res, other, other_pass = report["results"]
    assert run_res["evidence"]["failure_class"] == "assertion"
    assert "test_failure_class" not in run_res["evidence"]
    assert "the test's own failure class is environment (module yaml)" in other["reason"]
    assert other["evidence"]["test_failure_class"] == "environment"
    assert other["evidence"]["test_failure_module"] == "yaml"
    assert "own failure class is environment" in other_pass["reason"]
