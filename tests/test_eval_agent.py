"""tests/test_agent_eval_migrated.py -- hermetic gates for src/ozagia/eval/agent.py.

Every test runs against a tiny git repository built in tmp_path (git init and a commit
with `-c user.name=t -c user.email=t@t`; no global git config is touched). No model is
called. The live drivers are only checked for refusal, never executed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia.config import state_path
from ozagia.eval import agent as ae
from ozagia.eval.agent import (
    ARMS,
    LIVE_ENV,
    CommandDriver,
    FakeDriver,
    HarnessError,
    LiveRunNotApproved,
    NoopDriver,
    aggregate,
    load_tasks,
    main,
    make_driver,
    paired,
    render_markdown,
    run_eval,
    select_tasks,
    validate_task,
    write_arm_config,
)

ROOT = Path(__file__).resolve().parent.parent

MOD_SRC = "def add(a, b):\n    return a + b\n"
TEST_SRC = (
    "from pkg.mod import add\n\n\n"
    "def test_add():\n    assert add(1, 2) == 3\n"
)
BREAK_SIGN = (
    "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n-    return a + b\n+    return a - b\n"
)
BREAK_COMMENT_ONLY = (
    "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,2 +1,3 @@\n"
    " def add(a, b):\n     return a + b\n+# harmless\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "mod.py").write_text(MOD_SRC, encoding="utf-8")
    (repo / "tests" / "test_mod.py").write_text(TEST_SRC, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _task(tid: str, patch: str, node: str = "tests/test_mod.py::test_add") -> dict:
    return {
        "id": tid,
        "prompt": "addition gives the wrong sign",
        "area": "pkg",
        "break_patch": f"patches/{tid}.patch",
        "check": {"argv": ["-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "--confcutdir={workdir}", node],
                  "timeout_s": 60},
        "expected_files": ["pkg/mod.py"],
        "notes": "fixture",
    }


def _tasks_dir(root: Path, tasks: list[tuple[dict, str]]) -> Path:
    tdir = root / "tasks"
    (tdir / "patches").mkdir(parents=True)
    for task, patch in tasks:
        (tdir / task["break_patch"]).write_text(patch, encoding="utf-8")
    return tdir


@pytest.fixture()
def fixture(tmp_path: Path):
    repo, base = _repo(tmp_path)
    sign = _task("add-sign", BREAK_SIGN)
    tdir = _tasks_dir(tmp_path, [(sign, BREAK_SIGN)])
    return {"repo": repo, "base": base, "tdir": tdir, "sign": sign,
            "run_root": tmp_path / "runs"}


def _worktrees(repo: Path) -> list[str]:
    out = _git(repo, "worktree", "list", "--porcelain")
    return [line.split(" ", 1)[1] for line in out.splitlines()
            if line.startswith("worktree ")]


# ===================================================================================== #
# validation
# ===================================================================================== #
def test_validate_detects_pass_on_base_and_fail_after_break(fixture):
    res = validate_task(fixture["repo"], fixture["sign"], fixture["base"],
                        tasks_dir=fixture["tdir"], python=_python(),
                        run_root=fixture["run_root"])
    assert res["ok"] is True
    assert res["pass_on_base"] is True
    assert res["fail_after_break"] is True
    assert res["details"]["break_apply"]["ok"] is True


def test_task_whose_check_survives_the_break_is_rejected(tmp_path):
    repo, base = _repo(tmp_path)
    harmless = _task("comment-only", BREAK_COMMENT_ONLY)
    tdir = _tasks_dir(tmp_path, [(harmless, BREAK_COMMENT_ONLY)])
    res = validate_task(repo, harmless, base, tasks_dir=tdir, python=_python(),
                        run_root=tmp_path / "runs")
    assert res["ok"] is False
    assert res["pass_on_base"] is True
    assert res["fail_after_break"] is False
    assert res["details"]["reason"] == "check still passes after the break patch"


def test_check_failing_on_base_is_rejected(tmp_path):
    repo, base = _repo(tmp_path)
    missing = _task("no-such-test", BREAK_SIGN, node="tests/test_mod.py::test_nope")
    tdir = _tasks_dir(tmp_path, [(missing, BREAK_SIGN)])
    res = validate_task(repo, missing, base, tasks_dir=tdir, python=_python(),
                        run_root=tmp_path / "runs")
    assert res["ok"] is False
    assert res["pass_on_base"] is False
    assert res["details"]["reason"] == "check does not pass on base"


def test_validate_removes_its_workdir(fixture):
    validate_task(fixture["repo"], fixture["sign"], fixture["base"],
                  tasks_dir=fixture["tdir"], python=_python(),
                  run_root=fixture["run_root"])
    assert not any((fixture["run_root"] / "work").glob("*"))
    assert len(_worktrees(fixture["repo"])) == 1


# ===================================================================================== #
# drivers and runs
# ===================================================================================== #
def test_fake_driver_succeeds_and_noop_fails(fixture):
    tasks = [fixture["sign"]]
    fake = run_eval(fixture["repo"], tasks, ARMS, FakeDriver(), fixture["base"],
                    tasks_dir=fixture["tdir"], python=_python(),
                    run_root=fixture["run_root"])
    assert [r["success"] for r in fake["runs"]] == [True] * len(ARMS)
    assert all(r["localized"] for r in fake["runs"])
    assert all(r["files_changed"] == ["pkg/mod.py"] for r in fake["runs"])

    noop = run_eval(fixture["repo"], tasks, ["baseline"], NoopDriver(), fixture["base"],
                    tasks_dir=fixture["tdir"], python=_python(),
                    run_root=fixture["run_root"])
    assert [r["success"] for r in noop["runs"]] == [False]
    assert noop["runs"][0]["files_changed"] == []
    assert noop["runs"][0]["localized"] is False


def test_run_order_is_deterministic_and_repeats_are_recorded(fixture):
    other = _task("add-sign-b", BREAK_SIGN)
    (fixture["tdir"] / other["break_patch"]).write_text(BREAK_SIGN, encoding="utf-8")
    tasks = [other, fixture["sign"]]
    first = run_eval(fixture["repo"], tasks, ARMS, NoopDriver(), fixture["base"],
                     tasks_dir=fixture["tdir"], repeats=2, python=_python(),
                     run_root=fixture["run_root"])
    keys = [(r["task"], r["arm"], r["repeat"]) for r in first["runs"]]
    assert keys == sorted(keys, key=lambda k: (k[0], ARMS.index(k[1]), k[2]))
    assert len(keys) == 2 * len(ARMS) * 2
    assert first["tasks"] == ["add-sign", "add-sign-b"]


def test_workdirs_are_cleaned_up_after_a_run(fixture):
    run_eval(fixture["repo"], [fixture["sign"]], ARMS, FakeDriver(), fixture["base"],
             tasks_dir=fixture["tdir"], python=_python(),
             run_root=fixture["run_root"])
    assert len(_worktrees(fixture["repo"])) == 1
    assert not any((fixture["run_root"] / "work").glob("*"))


def test_harness_refuses_to_remove_a_path_it_did_not_create(fixture):
    with pytest.raises(HarnessError, match="did not create"):
        ae.remove_workdir(fixture["repo"], fixture["repo"], fixture["run_root"])


def test_dev_memory_arm_writes_mcp_config_into_workdir(tmp_path):
    log = tmp_path / "log"
    paths = write_arm_config(tmp_path, "dev_memory", "/usr/bin/python3", log)
    assert {p.name for p in paths} == {ae.MCP_CONFIG_CLAUDE, ae.MCP_CONFIG_OPENCODE}
    claude = json.loads((tmp_path / ae.MCP_CONFIG_CLAUDE).read_text(encoding="utf-8"))
    server = claude["mcpServers"][ae.MCP_SERVER_NAME]
    assert server["args"] == ["-m", "ozagia.integrations.mcp.server"]
    assert server["env"]["OZAGIA_ROOT"] == str(tmp_path)
    # The snapshot is prepared before the driver starts, so the server must not
    # rebuild it inside the timed session.
    assert server["env"]["OZAGIA_AUTOBUILD"] == "0"
    assert server["env"]["OZAGIA_DUMP"] == str(log / "dev_graph.json")
    opencode = json.loads((tmp_path / ae.MCP_CONFIG_OPENCODE).read_text(encoding="utf-8"))
    assert ae.MCP_SERVER_NAME in opencode["mcp"]


def test_baseline_arm_writes_no_mcp_config(tmp_path):
    assert write_arm_config(tmp_path, "baseline", "python", tmp_path / "log") == []
    assert list(tmp_path.iterdir()) == []


def test_claude_command_uses_strict_mcp_flags_per_arm(tmp_path):
    driver = CommandDriver("claude")
    base_argv = driver._argv("p", "baseline", None)
    assert "--strict-mcp-config" in base_argv
    assert "--mcp-config" not in base_argv
    cfg = tmp_path / ae.MCP_CONFIG_CLAUDE
    dm_argv = driver._argv("p", "dev_memory", cfg)
    assert dm_argv[dm_argv.index("--mcp-config") + 1] == str(cfg)
    assert dm_argv[dm_argv.index("-p") + 1] == "p"


def test_claude_json_parser_reads_result_cost_and_usage():
    doc = json.dumps({"result": "done", "total_cost_usd": 0.25,
                      "usage": {"input_tokens": 120, "output_tokens": 30}})
    parsed = ae._parse_claude_json(doc)
    assert parsed == {"tokens_in": 120, "tokens_out": 30, "cost_usd": 0.25,
                      "driver_error": None, "transcript": "done"}
    assert ae._parse_claude_json("not json") == {}


def test_claude_json_parser_reports_api_error():
    # Shape of the real output when the CLI is not logged in (no tokens spent).
    doc = json.dumps({"is_error": True, "terminal_reason": "api_error",
                      "result": "Failed to authenticate: OAuth session expired",
                      "total_cost_usd": 0, "usage": {"input_tokens": 0,
                                                     "output_tokens": 0}})
    parsed = ae._parse_claude_json(doc)
    assert parsed["driver_error"].startswith("Failed to authenticate")
    assert ae._AUTH_ERROR_RE.search(parsed["driver_error"])


def test_preflight_refuses_when_claude_is_logged_out(monkeypatch):
    monkeypatch.setenv(LIVE_ENV, "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    class Done:
        stdout = json.dumps({"loggedIn": False})

    monkeypatch.setattr(ae.subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(HarnessError, match="not logged in"):
        make_driver("claude", live=True).preflight()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    make_driver("claude", live=True).preflight()


def test_run_stops_on_an_authentication_error(fixture):
    class AuthFailDriver(NoopDriver):
        name = "authfail"

        def run(self, *a, **k):
            out = super().run(*a, **k)
            out["driver_error"] = "Failed to authenticate: OAuth session expired"
            return out

    other = _task("add-sign-b", BREAK_SIGN)
    (fixture["tdir"] / other["break_patch"]).write_text(BREAK_SIGN, encoding="utf-8")
    with pytest.raises(HarnessError, match="cannot authenticate"):
        run_eval(fixture["repo"], [fixture["sign"], other], ["baseline"],
                 AuthFailDriver(), fixture["base"], tasks_dir=fixture["tdir"],
                 python=_python(), run_root=fixture["run_root"])
    assert len(_worktrees(fixture["repo"])) == 1


def test_spawned_commands_get_a_closed_stdin(tmp_path):
    res = ae.spawn_group([_python(), "-c", "import sys; print(repr(sys.stdin.read()))"],
                         cwd=tmp_path, env=dict(__import__("os").environ),
                         timeout_s=20)
    assert res["stdout"].strip() == "''"


# ===================================================================================== #
# live gating
# ===================================================================================== #
@pytest.mark.parametrize("name", ["claude", "opencode"])
def test_live_drivers_refuse_without_both_flag_and_env(monkeypatch, name):
    monkeypatch.delenv(LIVE_ENV, raising=False)
    with pytest.raises(LiveRunNotApproved):
        make_driver(name, live=False)
    with pytest.raises(LiveRunNotApproved):
        make_driver(name, live=True)
    monkeypatch.setenv(LIVE_ENV, "1")
    with pytest.raises(LiveRunNotApproved):
        make_driver(name, live=False)
    monkeypatch.setenv(LIVE_ENV, "0")
    with pytest.raises(LiveRunNotApproved):
        make_driver(name, live=True)
    monkeypatch.setenv(LIVE_ENV, "1")
    assert isinstance(make_driver(name, live=True), CommandDriver)


def test_cli_refuses_live_driver_before_creating_worktrees(monkeypatch, fixture, capsys):
    monkeypatch.delenv(LIVE_ENV, raising=False)
    code = main(["run", "--driver", "claude", "--repo", str(fixture["repo"]),
                 "--tasks", str(_write_tasks(fixture)), "--run-root", str(fixture["run_root"]),
                 "--arms", "baseline"])
    assert code == 2
    assert "needs both --live" in capsys.readouterr().err
    assert len(_worktrees(fixture["repo"])) == 1


def _write_tasks(fixture) -> Path:
    path = fixture["tdir"] / "tasks.json"
    path.write_text(json.dumps({"version": 1, "base": fixture["base"],
                                "tasks": [fixture["sign"]]}), encoding="utf-8")
    return path


# ===================================================================================== #
# task file
# ===================================================================================== #
def test_load_tasks_rejects_a_malformed_task(fixture):
    bad = dict(fixture["sign"])
    bad["check"] = {"argv": [], "timeout_s": 60}
    path = fixture["tdir"] / "bad.json"
    path.write_text(json.dumps({"version": 1, "tasks": [bad]}), encoding="utf-8")
    with pytest.raises(HarnessError, match="check.argv"):
        load_tasks(path)


def test_select_tasks_rejects_unknown_ids(fixture):
    with pytest.raises(HarnessError, match="unknown task ids: nope"):
        select_tasks([fixture["sign"]], ["nope"])


# ===================================================================================== #
# report
# ===================================================================================== #
def _run(task, arm, repeat, success, wall=None, tin=None, cost=None, localized=False):
    return {"task": task, "arm": arm, "repeat": repeat, "success": success,
            "wall_s": wall, "tokens_in": tin, "tokens_out": None,
            "cost_usd": cost, "localized": localized}


def test_report_aggregates_arms_and_pairs_outcomes():
    runs = [
        _run("t1", "baseline", 1, True, wall=10.0, tin=100, cost=0.5, localized=True),
        _run("t1", "baseline", 2, False, wall=20.0, tin=None, localized=False),
        _run("t1", "dev_memory", 1, True, wall=5.0, tin=50, cost=0.25, localized=True),
        _run("t1", "dev_memory", 2, True, wall=7.0, tin=None, localized=True),
        _run("t2", "baseline", 1, False, wall=3.0),
        _run("t2", "dev_memory", 1, False, wall=4.0),
    ]
    rows = {r["arm"]: r for r in aggregate(runs, ["baseline", "dev_memory"])}
    assert rows["baseline"]["runs"] == 3
    assert rows["baseline"]["successes"] == 1
    assert rows["baseline"]["success_rate"] == pytest.approx(0.3333)
    assert rows["baseline"]["mean_wall_s"] == pytest.approx(11.0)
    assert rows["baseline"]["mean_tokens_in"] == pytest.approx(100.0, abs=0.1)
    assert rows["baseline"]["tokens_known_runs"] == 1
    assert rows["dev_memory"]["success_rate"] == pytest.approx(0.6667)
    assert rows["dev_memory"]["localization_rate"] == pytest.approx(0.6667)
    assert rows["dev_memory"]["mean_cost_usd"] == pytest.approx(0.25)
    assert rows["dev_memory"]["mean_tokens_out"] is None

    pairs = paired(runs, ["baseline", "dev_memory"])
    assert pairs["t1"]["baseline"]["outcomes"] == [True, False]
    assert pairs["t1"]["dev_memory"]["successes"] == 2
    assert pairs["t2"]["baseline"]["repeats"] == 1


def test_markdown_labels_every_arm_and_task_and_flags_live_drivers():
    report = {
        "driver": "claude", "base": "abc", "arms": ["baseline", "dev_memory"],
        "repeats": 1, "tasks": ["t1"],
        "summary": aggregate([_run("t1", "baseline", 1, True),
                              _run("t1", "dev_memory", 1, False)],
                             ["baseline", "dev_memory"]),
        "per_task": paired([_run("t1", "baseline", 1, True),
                            _run("t1", "dev_memory", 1, False)],
                           ["baseline", "dev_memory"]),
    }
    md = render_markdown(report)
    assert "baseline" in md and "dev_memory" in md and "| t1 |" in md
    assert "untested live" in md


# ===================================================================================== #
# source audit (style of test_contrastive_verdict_no_python_decisions)
# ===================================================================================== #
def test_agent_eval_has_no_substrate_or_rule_engine_calls():
    src = (ROOT / "src" / "ozagia" / "eval" / "agent.py").read_text(encoding="utf-8")
    for needle in ("substrate_rs", "from domains", "import domains", "graph_rewrite",
                   "gr._match", "gr._apply"):
        assert needle not in src, needle


def _python() -> str:
    import sys
    return sys.executable


# ===================================================================================== #
# dt-w4-fixes-b (review 11): scoped cleanup, process groups, shared refs, prep timing
# ===================================================================================== #
def _pid_gone(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _listed(repo: Path) -> set[str]:
    return {str(Path(p).resolve()) for p in _worktrees(repo)}


def test_cleanup_does_not_prune_an_unrelated_missing_worktree(fixture, tmp_path):
    import shutil
    repo, base = fixture["repo"], fixture["base"]
    other = tmp_path / "unrelated-wt"
    _git(repo, "worktree", "add", "--detach", str(other), base)
    shutil.rmtree(other)  # directory vanishes out of band; admin entry stays
    assert str(other.resolve()) in _listed(repo)
    run_eval(repo, [fixture["sign"]], ["baseline"], FakeDriver(), base,
             tasks_dir=fixture["tdir"], python=_python(),
             run_root=fixture["run_root"])
    assert str(other.resolve()) in _listed(repo)
    assert not any((fixture["run_root"] / "work").glob("*"))


def test_partial_cleanup_is_reported_and_removes_only_its_own_entry(fixture):
    repo, base, run_root = fixture["repo"], fixture["base"], fixture["run_root"]
    wd = ae.make_workdir(repo, base, run_root, "locked-1")
    _git(repo, "worktree", "lock", str(wd))
    warnings = ae.remove_workdir(repo, wd, run_root)
    assert warnings and warnings[0].startswith("git worktree remove --force failed")
    assert not wd.exists()
    assert str(wd.resolve()) not in _listed(repo)
    assert str(repo.resolve()) in _listed(repo)


def test_clean_cleanup_reports_no_warnings(fixture):
    repo, base, run_root = fixture["repo"], fixture["base"], fixture["run_root"]
    wd = ae.make_workdir(repo, base, run_root, "clean-1")
    assert ae.remove_workdir(repo, wd, run_root) == []
    assert not wd.exists()


_VICTIM = (
    "import os, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "with open(sys.argv[1], 'w') as fh:\n"
    "    fh.write(f'{os.getpid()} {child.pid}')\n"
    "time.sleep(600)\n"
)


def test_run_check_timeout_kills_driver_and_its_child(tmp_path):
    victim = tmp_path / "victim.py"
    victim.write_text(_VICTIM, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"
    res = ae.run_check(tmp_path, {"argv": [str(victim), str(pidfile)], "timeout_s": 3},
                       _python(), tmp_path / "check.log")
    assert res["timed_out"] is True and res["passed"] is False
    parent, child = (int(x) for x in pidfile.read_text(encoding="utf-8").split())
    assert _pid_gone(parent)
    assert _pid_gone(child)


class _StubCommandDriver(CommandDriver):
    """Test-only. Shares CommandDriver's group runner, with an argv set by the test.

    Not in LIVE_DRIVERS, never returned by make_driver, and it does not call
    _require_live_env because it never reaches a model."""

    def __init__(self, argv: list[str]) -> None:
        self.name = "stub-command"
        self.max_budget_usd = None
        self.spec = {"argv": [], "arm_argv": {}, "mcp_config_file": "none",
                     "parse": lambda stdout: {"transcript": stdout}}
        self._stub_argv = argv

    def run(self, workdir, prompt, arm, timeout_s, log_dir, *, patch_path=None):
        res = ae.spawn_group(self._stub_argv, cwd=workdir, env=dict(os.environ),
                             timeout_s=timeout_s)
        return ae._empty_result(exit_code=res["returncode"], wall_s=res["seconds"],
                                timed_out=res["timed_out"], transcript=res["stdout"])


def test_driver_timeout_kills_the_driver_tree_and_is_recorded(fixture, tmp_path):
    victim = tmp_path / "victim.py"
    victim.write_text(_VICTIM, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"
    driver = _StubCommandDriver([_python(), str(victim), str(pidfile)])
    rec = ae.run_one(fixture["repo"], fixture["sign"], "baseline", 1, driver,
                     fixture["base"], tasks_dir=fixture["tdir"], python=_python(),
                     run_root=fixture["run_root"], timeout_s=3, keep=False)
    assert rec["timed_out"] is True
    assert rec["success"] is False
    parent, child = (int(x) for x in pidfile.read_text(encoding="utf-8").split())
    assert _pid_gone(parent)
    assert _pid_gone(child)
    assert rec["cleanup_warnings"] == []


def test_stub_driver_is_not_a_live_driver_and_make_driver_still_refuses():
    assert "stub-command" not in ae.LIVE_DRIVERS
    with pytest.raises(LiveRunNotApproved):
        make_driver("claude", live=True)


def test_claude_disallows_shared_ref_moving_git_commands():
    argv = CommandDriver("claude")._argv("p", "baseline", None)
    i = argv.index("--disallowedTools")
    tokens = argv[i + 1].split(",")
    expected = ["Bash(git commit:*)", "Bash(git push:*)", "Bash(git stash:*)",
                "Bash(git branch:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
                "Bash(git reset:*)", "Bash(git rebase:*)", "Bash(git merge:*)",
                "Bash(git tag:*)", "Bash(git worktree:*)"]
    assert tokens == expected
    assert argv[i + 2].startswith("--")  # the next token is a flag, so parsing stops


def test_snapshot_ignores_gitignored_var_dir(tmp_path):
    repo, _base = _repo(tmp_path)
    (repo / ".gitignore").write_text("var/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "ignore var")
    base = _git(repo, "rev-parse", "HEAD")
    wd = ae.make_workdir(repo, base, tmp_path / "runs", "snap-1")
    (wd / "var").mkdir()
    (wd / "var" / "cache.json").write_text("{}", encoding="utf-8")
    assert "var/cache.json" not in ae._snapshot(wd)
    ae.remove_workdir(repo, wd, tmp_path / "runs")


def test_snapshot_ignores_the_state_dir_even_when_not_gitignored(tmp_path):
    repo, base = _repo(tmp_path)
    wd = ae.make_workdir(repo, base, tmp_path / "runs", "snap-2")
    state_path(wd, "tags.json").parent.mkdir(parents=True)
    state_path(wd, "tags.json").write_text("{}", encoding="utf-8")
    assert not any(k.startswith(".ozagia/") for k in ae._snapshot(wd))
    ae.remove_workdir(repo, wd, tmp_path / "runs")


def test_prep_failure_is_recorded_and_the_run_still_scored(fixture, tmp_path,
                                                          monkeypatch):
    monkeypatch.setattr(ae, "_PREP_CODE", "raise SystemExit('index broke')")
    prep = ae.prepare_dev_memory(fixture["repo"], _python(), tmp_path / "log")
    assert prep["prep_ok"] is False
    assert prep["prep_error"]
    assert (tmp_path / "log" / "prep.log").is_file()
    runs = run_eval(fixture["repo"], [fixture["sign"]], ARMS, FakeDriver(),
                    fixture["base"], tasks_dir=fixture["tdir"], python=_python(),
                    run_root=fixture["run_root"])["runs"]
    by_arm = {r["arm"]: r for r in runs}
    assert by_arm["baseline"]["prep_s"] == 0.0
    assert by_arm["baseline"]["prep_ok"] is None
    assert by_arm["dev_memory"]["prep_ok"] is False
    assert by_arm["dev_memory"]["prep_error"]
    assert by_arm["dev_memory"]["success"] is True


def test_aggregate_reports_mean_prep_and_prep_failures():
    runs = [
        {"task": "t1", "arm": "baseline", "repeat": 1, "success": True,
         "prep_s": 0.0, "prep_ok": None},
        {"task": "t1", "arm": "dev_memory", "repeat": 1, "success": True,
         "prep_s": 10.0, "prep_ok": True},
        {"task": "t2", "arm": "dev_memory", "repeat": 1, "success": False,
         "prep_s": 2.0, "prep_ok": False},
    ]
    rows = {r["arm"]: r for r in aggregate(runs, ["baseline", "dev_memory"])}
    assert rows["baseline"]["mean_prep_s"] == 0.0
    assert rows["dev_memory"]["mean_prep_s"] == pytest.approx(6.0)
    assert rows["dev_memory"]["prep_failures"] == 1
    assert rows["baseline"]["prep_failures"] == 0


# ===================================================================================== #
# selectable backends (opencode)
# ===================================================================================== #
_OPENCODE_EVENTS = "\n".join(json.dumps(e) for e in [
    {"type": "step_start", "part": {"type": "step-start"}},
    {"type": "tool_use", "part": {"type": "tool", "tool": "bash"}},
    {"type": "step_finish", "part": {"cost": 0.01,
                                     "tokens": {"input": 100, "output": 5}}},
    {"type": "text", "part": {"type": "text", "text": "fixed"}},
    {"type": "step_finish", "part": {"cost": 0.02,
                                     "tokens": {"input": 50, "output": 7}}},
])


def test_opencode_event_parser_sums_steps():
    parsed = ae._parse_opencode_events(_OPENCODE_EVENTS)
    assert parsed["tokens_in"] == 150 and parsed["tokens_out"] == 12
    assert parsed["cost_usd"] == pytest.approx(0.03)
    assert parsed["tool_calls"] == 1
    assert parsed["driver_error"] is None
    assert parsed["transcript"] == "fixed"
    empty = ae._parse_opencode_events("")
    assert empty["driver_error"] == "opencode produced no finished step"
    err = ae._parse_opencode_events(json.dumps(
        {"type": "error", "error": "provider down"}))
    assert err["driver_error"] == "provider down"


def test_opencode_argv_and_per_arm_config(tmp_path, monkeypatch):
    monkeypatch.setenv(LIVE_ENV, "1")
    driver = make_driver("opencode", live=True, model="vllm/qwen3.8-27b")
    argv = driver._argv("fix it", "baseline", None)
    assert argv[:3] == ["opencode", "run", "--standalone"]
    assert argv[argv.index("-m") + 1] == "vllm/qwen3.8-27b"
    assert argv[-1] == "fix it"

    base_cfg = json.loads(driver._opencode_config(
        "baseline", None, tmp_path / "b").read_text(encoding="utf-8"))
    assert "mcp" not in base_cfg
    assert base_cfg["permission"]["bash"]["git commit*"] == "deny"
    assert base_cfg["permission"]["bash"]["*"] == "allow"

    work = tmp_path / "work"
    work.mkdir()
    write_arm_config(work, "dev_memory", "/usr/bin/python3", tmp_path / "log")
    dm_cfg = json.loads(driver._opencode_config(
        "dev_memory", work / ae.MCP_CONFIG_OPENCODE, tmp_path / "d").read_text(
            encoding="utf-8"))
    assert ae.MCP_SERVER_NAME in dm_cfg["mcp"]
    assert dm_cfg["permission"] == base_cfg["permission"]


def test_driver_defaults_to_environment_selection(monkeypatch):
    monkeypatch.setenv(ae.DRIVER_ENV, "fake")
    assert isinstance(make_driver(None), FakeDriver)
    monkeypatch.delenv(ae.DRIVER_ENV)
    with pytest.raises(HarnessError, match="no driver"):
        make_driver(None)


def test_opencode_preflight_checks_the_model(monkeypatch):
    monkeypatch.setenv(LIVE_ENV, "1")

    class Done:
        returncode = 0
        stdout = "vllm/qwen3.8-27b\nopenai/gpt-x\n"
        stderr = ""

    monkeypatch.setattr(ae.subprocess, "run", lambda *a, **k: Done())
    make_driver("opencode", live=True, model="vllm/qwen3.8-27b").preflight()
    with pytest.raises(HarnessError, match="does not list model"):
        make_driver("opencode", live=True, model="nope/model").preflight()


def test_total_cost_cap_stops_the_run(fixture):
    class CostlyDriver(NoopDriver):
        name = "costly"

        def run(self, *a, **k):
            out = super().run(*a, **k)
            out["cost_usd"] = 0.6
            return out

    other = _task("add-sign-b", BREAK_SIGN)
    (fixture["tdir"] / other["break_patch"]).write_text(BREAK_SIGN, encoding="utf-8")
    report = run_eval(fixture["repo"], [fixture["sign"], other], ["baseline"],
                      CostlyDriver(), fixture["base"], tasks_dir=fixture["tdir"],
                      python=_python(), run_root=fixture["run_root"],
                      max_total_usd=0.5)
    assert len(report["runs"]) == 1
    assert report["stopped"].startswith("cost cap reached")
    assert report["total_cost_usd"] == pytest.approx(0.6)


def test_clone_workdir_is_isolated_and_removed(fixture):
    path = ae.make_workdir(fixture["repo"], fixture["base"], fixture["run_root"],
                           "iso", isolation="clone")
    assert (path / ".git").is_dir()
    assert len(_worktrees(fixture["repo"])) == 1
    remotes = subprocess.run(["git", "remote"], cwd=path, capture_output=True,
                             text=True, check=True).stdout.split()
    assert remotes == []
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True,
                          text=True, check=True).stdout.strip()
    assert head == fixture["base"]
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path,
                         capture_output=True, text=True, check=True).stdout.strip()
    assert Path(top).resolve() == path.resolve()
    assert ae.remove_workdir(fixture["repo"], path, fixture["run_root"]) == []
    assert not path.exists()


def test_run_uses_the_driver_isolation(fixture):
    seen = []

    class CloneFake(FakeDriver):
        isolation = "clone"

        def run(self, workdir, *a, **k):
            seen.append((workdir / ".git").is_dir())
            return super().run(workdir, *a, **k)

    report = run_eval(fixture["repo"], [fixture["sign"]], ["baseline"], CloneFake(),
                      fixture["base"], tasks_dir=fixture["tdir"], python=_python(),
                      run_root=fixture["run_root"])
    assert seen == [True]
    assert report["runs"][0]["success"] is True
    assert len(_worktrees(fixture["repo"])) == 1
    assert not any((fixture["run_root"] / "work").glob("*"))


def test_command_driver_puts_the_run_python_first_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv(LIVE_ENV, "1")
    driver = make_driver("opencode", live=True)
    assert driver.isolation == "clone"
    driver.python = _python()
    captured = {}

    def fake_spawn(argv, *, cwd, env, timeout_s):
        captured["env"] = env
        return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False,
                "seconds": 0.0}

    monkeypatch.setattr(ae, "spawn_group", fake_spawn)
    driver.run(tmp_path, "fix it", "baseline", 5, tmp_path / "log")
    first = Path(captured["env"]["PATH"].split(os.pathsep)[0])
    assert first == tmp_path / "log" / "bin"
    assert os.path.realpath(first / "python") == os.path.realpath(_python())
    assert os.path.realpath(first / "python3") == os.path.realpath(_python())


def test_spawned_commands_see_pwd_equal_to_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("PWD", "/somewhere/else")
    res = ae.spawn_group(["sh", "-c", 'printf %s "$PWD"'], cwd=tmp_path,
                         env=dict(os.environ), timeout_s=10)
    assert res["stdout"] == str(tmp_path.resolve())


def test_opencode_parser_counts_code_mode_mcp_calls():
    def tool(name, code=None):
        state = {"input": {"code": code}} if code is not None else {"input": {}}
        return json.dumps({"type": "tool_use", "part": {"tool": name, "state": state}})

    stream = "\n".join([
        tool("read"),
        tool("execute", 'return await tools.opencode.list_mcp_resources();'),
        tool("execute", 'const a = await tools["ozagia"].dev_code_search({q: 1});\n'
                        "return tools['ozagia'].dev_impact({});"),
        tool("ozagia_dev_context"),
        # The form the v2 pilot's model wrote (review: it counted 0 before).
        tool("execute", "return await tools.ozagia.dev_brief({task: 'x'})"),
        json.dumps({"type": "step_finish", "part": {"cost": 0, "tokens": {"input": 1,
                                                                        "output": 1}}}),
    ])
    out = ae._parse_opencode_events(stream)
    assert out["tool_calls"] == 5
    assert out["mcp_calls"] == 4
    assert out["tools_by_name"] == {"execute": 3, "ozagia_dev_context": 1,
                                    "read": 1}


def test_arm_config_and_prep_pass_the_source_repo_config(tmp_path, monkeypatch):
    cfg = tmp_path / "src.toml"
    cfg.write_text("[index]\nsource_dirs = ['pkg']\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    write_arm_config(work, "dev_memory", "/usr/bin/python3", tmp_path / "log", config=cfg)
    doc = json.loads((work / ae.MCP_CONFIG_OPENCODE).read_text(encoding="utf-8"))
    env = doc["mcp"][ae.MCP_SERVER_NAME]["environment"]
    assert env["OZAGIA_CONFIG"] == str(cfg)
    seen = {}

    def fake_spawn(argv, *, cwd, env, timeout_s):
        seen.update(env)
        return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False,
                "seconds": 0.0}

    monkeypatch.setattr(ae, "spawn_group", fake_spawn)
    ae.prepare_dev_memory(work, "/usr/bin/python3", tmp_path / "log2", config=cfg)
    assert seen["OZAGIA_CONFIG"] == str(cfg) and seen["OZAGIA_ROOT"] == str(work)


# ===================================================================================== #
# graph_intent arm (W2-B): daemon lifecycle and plugin install, with a stub daemon
# ===================================================================================== #
# A stand-in for `python -m ozagia.daemon serve`: speaks the JSON-lines protocol
# for hello/tool/after/stats/shutdown, prints `ready <sock>`, and starts a
# sleeping child in its process group so the test can see the group killed.
_STUB_DAEMON = r'''
import json, os, socket, subprocess, sys, time
sock_path, pidfile, mode = sys.argv[1], sys.argv[2], sys.argv[3]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open(pidfile, "w") as fh:
    fh.write(f"{os.getpid()} {child.pid}")
if mode == "fail":
    print("boom: cannot load snapshot", file=sys.stderr)
    sys.exit(3)
if mode == "noready":
    time.sleep(600)
stats = {"hellos": 0, "handled": 0, "fallbacks": 0, "by_tool": {}, "by_intent": {},
         "mean_ms": 1.0, "max_ms": 2.0}
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(sock_path)
srv.listen(4)
print(f"ready {sock_path}", flush=True)
while True:
    conn, _ = srv.accept()
    with conn, conn.makefile("rwb") as fh:
        for line in fh:
            req = json.loads(line)
            op = req.get("op")
            resp = {"id": req.get("id"), "ok": True}
            if op == "hello":
                stats["hellos"] += 1
            elif op == "tool":
                stats["handled"] += 1
                stats["by_tool"][req["tool"]] = stats["by_tool"].get(req["tool"], 0) + 1
                resp.update(handled=True, output="graph says hi", meta={"intent": "x"})
            elif op == "after":
                if (req.get("meta") or {}).get("fallback"):
                    stats["fallbacks"] += 1
                resp["append"] = ""
            elif op == "stats":
                resp["stats"] = stats
            fh.write((json.dumps(resp) + "\n").encode())
            fh.flush()
            if op == "shutdown":
                srv.close()
                os.unlink(sock_path)
                sys.exit(0)
'''


@pytest.fixture(scope="session")
def stub_daemon_script(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("stubd") / "stub_daemon.py"
    path.write_text(_STUB_DAEMON, encoding="utf-8")
    return path


def _use_stub_daemon(monkeypatch, script: Path, pidfile: Path, mode: str = "ok") -> None:
    def command(tool_python, workdir, log_dir, sock):
        return [_python(), str(script), str(sock), str(pidfile), mode]
    monkeypatch.setattr(ae, "daemon_command", command)


@pytest.fixture(autouse=True)
def _hermetic_daemon(monkeypatch, stub_daemon_script, tmp_path_factory):
    """Every test in this file that runs graph_intent gets the stub daemon."""
    pidfile = tmp_path_factory.mktemp("stubpid") / "pids.txt"
    _use_stub_daemon(monkeypatch, stub_daemon_script, pidfile)


def _pids(pidfile: Path) -> list[int]:
    return [int(x) for x in pidfile.read_text(encoding="utf-8").split()]


class _PluginLikeFake(FakeDriver):
    """Fake driver that acts like a loaded plugin: says hello and sends one tool
    call to the daemon, then reverts the break."""

    def __init__(self) -> None:
        self.seen: dict = {}

    def run(self, workdir, prompt, arm, timeout_s, log_dir, *, patch_path=None,
            extra_env=None):
        import socket as socketmod
        plugin = workdir / ".opencode" / "plugins" / "ozagia.js"
        self.seen.update(plugin_present=plugin.is_file(), env=dict(extra_env or {}))
        sock = (extra_env or {}).get("OZAGIA_DAEMON_SOCK")
        if sock and Path(sock).exists():
            with socketmod.socket(socketmod.AF_UNIX) as conn:
                conn.connect(sock)
                fh = conn.makefile("rwb")
                for i, msg in enumerate([{"op": "hello", "harness": "opencode"},
                                         {"op": "tool", "tool": "read",
                                          "input": {"path": "pkg/mod.py"}}]):
                    fh.write((json.dumps({"id": i, **msg}) + "\n").encode())
                    fh.flush()
                    self.seen.setdefault("replies", []).append(json.loads(fh.readline()))
        return super().run(workdir, prompt, arm, timeout_s, log_dir,
                           patch_path=patch_path)


def test_graph_intent_arm_runs_daemon_and_plugin_end_to_end(fixture, monkeypatch,
                                                            stub_daemon_script, tmp_path):
    pidfile = tmp_path / "pids.txt"
    _use_stub_daemon(monkeypatch, stub_daemon_script, pidfile)
    driver = _PluginLikeFake()
    rec = ae.run_one(fixture["repo"], fixture["sign"], "graph_intent", 1, driver,
                     fixture["base"], tasks_dir=fixture["tdir"], python=_python(),
                     run_root=fixture["run_root"], timeout_s=60, keep=True)
    assert rec["success"] is True
    assert rec["daemon_ok"] is True and rec["daemon_error"] is None
    assert isinstance(rec["daemon_ready_s"], float)
    assert rec["daemon_stats"]["hellos"] == 1
    assert rec["daemon_stats"]["by_tool"] == {"read": 1}
    assert rec["plugin_loaded"] is True
    assert rec["captured_calls"] == 1
    assert rec["fallbacks"] == 0
    assert rec["prep_ok"] is not None  # same prep as dev_memory
    # The plugin was there during the run, is gone after it, and is not an edit.
    assert driver.seen["plugin_present"] is True
    assert driver.seen["replies"][1]["output"] == "graph says hi"
    sock = Path(driver.seen["env"]["OZAGIA_DAEMON_SOCK"])
    assert len(str(sock)) < 100
    assert not sock.exists()
    workdir = Path(rec["workdir"])
    assert not (workdir / ".opencode" / "plugins" / "ozagia.js").exists()
    assert rec["files_changed"] == ["pkg/mod.py"]
    # No MCP config for this arm.
    assert not (workdir / ae.MCP_CONFIG_OPENCODE).exists()
    # The daemon and the child in its process group are gone.
    for pid in _pids(pidfile):
        assert _pid_gone(pid), pid
    assert (Path(rec["log_dir"]) / "daemon.stdout.txt").read_text(
        encoding="utf-8").startswith("ready ")
    assert ae.remove_workdir(fixture["repo"], workdir, fixture["run_root"]) == []


def test_snapshot_never_lists_the_installed_plugin(tmp_path):
    repo, base = _repo(tmp_path)
    wd = ae.make_workdir(repo, base, tmp_path / "runs", "snap-plugin")
    from ozagia.integrations.opencode import install as oc
    oc.install(wd)
    assert ".opencode/plugins/ozagia.js" not in ae._snapshot(wd)
    other = wd / ".opencode" / "plugins" / "agent.js"
    other.write_text("x", encoding="utf-8")
    assert ".opencode/plugins/agent.js" in ae._snapshot(wd)
    ae.remove_workdir(repo, wd, tmp_path / "runs")


def test_graph_intent_daemon_start_failure_is_recorded(fixture, monkeypatch,
                                                       stub_daemon_script, tmp_path):
    pidfile = tmp_path / "pids.txt"
    _use_stub_daemon(monkeypatch, stub_daemon_script, pidfile, mode="fail")
    driver = _PluginLikeFake()
    rec = ae.run_one(fixture["repo"], fixture["sign"], "graph_intent", 1, driver,
                     fixture["base"], tasks_dir=fixture["tdir"], python=_python(),
                     run_root=fixture["run_root"], timeout_s=60, keep=False)
    assert rec["daemon_ok"] is False
    assert "exit 3" in rec["daemon_error"] and "boom" in rec["daemon_error"]
    assert rec["daemon_stats"] is None and rec["plugin_loaded"] is None
    assert rec["success"] is True  # the run went on
    assert driver.seen["plugin_present"] is True
    assert "OZAGIA_DAEMON_SOCK" in driver.seen["env"]
    for pid in _pids(pidfile):
        assert _pid_gone(pid), pid


def test_graph_intent_daemon_that_never_gets_ready_is_killed(fixture, monkeypatch,
                                                             stub_daemon_script, tmp_path):
    pidfile = tmp_path / "pids.txt"
    _use_stub_daemon(monkeypatch, stub_daemon_script, pidfile, mode="noready")
    monkeypatch.setattr(ae, "DAEMON_READY_TIMEOUT_S", 1)
    rec = ae.run_one(fixture["repo"], fixture["sign"], "graph_intent", 1, NoopDriver(),
                     fixture["base"], tasks_dir=fixture["tdir"], python=_python(),
                     run_root=fixture["run_root"], timeout_s=60, keep=False)
    assert rec["daemon_ok"] is False
    assert rec["daemon_error"] == "daemon not ready after 1 s"
    assert rec["success"] is False
    for pid in _pids(pidfile):
        assert _pid_gone(pid), pid


def test_graph_intent_with_claude_is_refused_before_any_worktree(fixture):
    with pytest.raises(HarnessError, match="graph_intent needs one of the drivers"):
        run_eval(fixture["repo"], [fixture["sign"]], ["baseline", "graph_intent"],
                 CommandDriver("claude"), fixture["base"], tasks_dir=fixture["tdir"],
                 python=_python(), run_root=fixture["run_root"])
    assert len(_worktrees(fixture["repo"])) == 1
    assert ae.default_arms("claude") == ["baseline", "dev_memory"]
    assert ae.default_arms("opencode") == list(ARMS)


def test_graph_intent_noop_run_aggregates_and_renders(fixture):
    report = run_eval(fixture["repo"], [fixture["sign"]], ["baseline", "graph_intent"],
                      NoopDriver(), fixture["base"], tasks_dir=fixture["tdir"],
                      python=_python(), run_root=fixture["run_root"])
    by_arm = {r["arm"]: r for r in report["runs"]}
    assert by_arm["graph_intent"]["daemon_ok"] is True
    assert by_arm["graph_intent"]["plugin_loaded"] is False  # noop never says hello
    assert "daemon_ok" not in by_arm["baseline"]
    rows = {r["arm"]: r for r in report["summary"]}
    assert rows["graph_intent"]["mean_captured_calls"] == 0.0
    assert rows["graph_intent"]["total_fallbacks"] == 0
    assert rows["graph_intent"]["plugin_load_failures"] == 1
    assert rows["graph_intent"]["daemon_failures"] == 0
    assert "mean_captured_calls" not in rows["baseline"]
    md = render_markdown(report)
    assert "## Graph daemon and plugin" in md
    assert "| graph_intent | 1 | 0.0 |  | 0 | 1 | 0 |" in md
    assert not any((fixture["run_root"] / "work").glob("*"))


def test_graph_stats_fields_read_counts():
    assert ae.graph_stats_fields(None) == {"plugin_loaded": None, "captured_calls": None,
                                           "passthrough_calls": None, "fallbacks": None,
                                           "preflights": None,
                                           "permission_assert": None,
                                           "codemode_tools": None,
                                           "shell_captured": None,
                                           "shell_passthrough": None,
                                           "structured_queries": None,
                                           "structured_outlines": None,
                                           "jq_captures": None,
                                           "repeats_seen": None,
                                           "stubs_served": None,
                                           "notes_served": None,
                                           "outlines_served": None,
                                           "bytes_saved": None,
                                           "patch_checks": None,
                                           "patch_misses_caught": None,
                                           "region_replies": None,
                                           "key_edits": None,
                                           "jobs_started": None,
                                           "job_waits": None,
                                           "job_wait_seconds": None,
                                           "polls_avoided": None,
                                           "path_suggestions": None,
                                           "ownership_warnings": None,
                                           "ops_nodes": None,
                                           "ops_after_annotations": None,
                                           "lint_runs": None,
                                           "lint_findings": None,
                                           "lint_timeouts": None,
                                           "ruff_captures": None,
                                           **{k: None for k in
                                              ae.TEST_SELECTION_FIELDS},
                                           "working_sets_served": None,
                                           "working_set_chars": None,
                                           **{k: None for k in ae.REPLACE_FIELDS}}
    # The daemon's shape: handled {"true": captured, "false": passthrough and
    # expired}; fallbacks = plugin fallbacks + expired + failed (review fix 4).
    assert ae.graph_stats_fields({"hellos": 2, "handled": {"true": 3, "false": 4},
                                  "passthrough": 3, "expired": 1,
                                  "plugin_fallbacks": 2, "fallbacks": 3,
                                  "preflights": 2, "permission_assert": True,
                                  "codemode_tools": ["read"],
                                  "structured_queries": 4,
                                  "structured_outlines": 1,
                                  "jq_captures": 2, "repeats_seen": 5,
                                  "stubs_served": 3, "notes_served": 2,
                                  "outlines_served": 1,
                                  "bytes_saved": 900, "patch_checks": 5,
                                  "patch_misses_caught": 2,
                                  "region_replies": 3, "key_edits": 1}) == {
        "plugin_loaded": True, "captured_calls": 3, "passthrough_calls": 3,
        "fallbacks": 3, "preflights": 2, "permission_assert": True,
        "codemode_tools": ["read"], "shell_captured": None,
        "shell_passthrough": None, "structured_queries": 4,
        "structured_outlines": 1, "jq_captures": 2, "repeats_seen": 5,
        "stubs_served": 3, "notes_served": 2, "outlines_served": 1,
        "bytes_saved": 900, "patch_checks": 5,
        "patch_misses_caught": 2, "region_replies": 3, "key_edits": 1, "jobs_started": None,
        "job_waits": None, "job_wait_seconds": None, "polls_avoided": None,
        "path_suggestions": None, "ownership_warnings": None,
        "ops_nodes": None, "ops_after_annotations": None,
        "lint_runs": None, "lint_findings": None, "lint_timeouts": None,
        "ruff_captures": None,
        **{k: None for k in ae.TEST_SELECTION_FIELDS},
        "working_sets_served": None, "working_set_chars": None,
        **{k: None for k in ae.REPLACE_FIELDS}}
    got = ae.graph_stats_fields({"hellos": 1, "shell": {"captured": 4,
                                                        "passthrough": 1}})
    assert (got["shell_captured"], got["shell_passthrough"]) == (4, 1)
    assert ae.graph_stats_fields({"hellos": 1, "handled": 2})["captured_calls"] == 2
    assert ae.graph_stats_fields({"hellos": 0, "by_handled": {"true": 5}})[
        "captured_calls"] == 5


def test_graph_stats_fields_and_aggregate_read_job_counters():
    got = ae.graph_stats_fields({"hellos": 1, "jobs_started": 2, "waits": 5,
                                 "wait_seconds": 61.234, "polls_avoided": 4})
    assert (got["jobs_started"], got["job_waits"], got["job_wait_seconds"],
            got["polls_avoided"]) == (2, 5, 61.2, 4)
    runs = [{"arm": "graph_intent", "daemon_ok": True, **got},
            {"arm": "graph_intent", "daemon_ok": True, "jobs_started": None, "job_waits": 1,
             "job_wait_seconds": 3.0, "polls_avoided": None}]
    row = ae.aggregate(runs, ["graph_intent"])[0]
    assert (row["total_jobs_started"], row["total_job_waits"],
            row["total_job_wait_seconds"], row["total_polls_avoided"]) == (2, 6, 64.2, 4)


def test_graph_stats_fields_and_aggregate_test_selection_counters():
    got = ae.graph_stats_fields({"hellos": 1, "affected_runs": 2,
                                 "tests_skipped_unaffected": 40,
                                 "cached_passes": 7, "full_runs_forced": 1})
    assert [got[k] for k in ae.TEST_SELECTION_FIELDS] == [2, 40, 7, 1]
    runs = [{"arm": "graph_intent", "daemon_ok": True, **got},
            {"arm": "graph_intent", "daemon_ok": True, "affected_runs": 1,
             "tests_skipped_unaffected": None, "cached_passes": 3,
             "full_runs_forced": None}]
    row = ae.aggregate(runs, ["graph_intent"])[0]
    assert [row[f"total_{k}"] for k in ae.TEST_SELECTION_FIELDS] == [3, 40, 10, 1]


def test_aggregate_totals_structured_reads():
    runs = [{"arm": "graph_intent", "daemon_ok": True, "structured_queries": 2,
             "structured_outlines": 3, "jq_captures": 1},
            {"arm": "graph_intent", "daemon_ok": True, "structured_queries": None,
             "structured_outlines": 4, "jq_captures": None}]
    row = ae.aggregate(runs, ["graph_intent"])[0]
    assert row["total_structured_queries"] == 2
    assert row["total_patch_checks"] == 0 and row["total_key_edits"] == 0
    assert row["total_structured_outlines"] == 7
    assert row["total_jq_captures"] == 1


def test_aggregate_totals_read_ledger():
    runs = [{"arm": "graph_intent", "daemon_ok": True, "repeats_seen": 2,
             "stubs_served": 1, "notes_served": 1, "outlines_served": 3,
             "bytes_saved": 100},
            {"arm": "graph_intent", "daemon_ok": True, "repeats_seen": None,
             "stubs_served": 4, "bytes_saved": 50}]
    row = ae.aggregate(runs, ["graph_intent"])[0]
    assert (row["total_repeats_seen"], row["total_stubs_served"],
            row["total_notes_served"], row["total_outlines_served"],
            row["total_bytes_saved"]) == (2, 5, 1, 3, 150)


def test_opencode_graph_intent_config_and_socket_env(tmp_path, monkeypatch):
    monkeypatch.setenv(LIVE_ENV, "1")
    driver = make_driver("opencode", live=True)
    cfg = json.loads(driver._opencode_config("graph_intent", None, tmp_path / "g")
                     .read_text(encoding="utf-8"))
    base = json.loads(driver._opencode_config("baseline", None, tmp_path / "b")
                      .read_text(encoding="utf-8"))
    assert cfg == base and "mcp" not in cfg
    captured = {}

    def fake_spawn(argv, *, cwd, env, timeout_s):
        captured.update(argv=argv, env=env)
        return {"returncode": 0, "stdout": "", "stderr": "", "timed_out": False,
                "seconds": 0.0}

    monkeypatch.setattr(ae, "spawn_group", fake_spawn)
    driver.run(tmp_path, "fix it", "graph_intent", 5, tmp_path / "log",
               extra_env={"OZAGIA_DAEMON_SOCK": "/tmp/x.sock"})
    assert captured["env"]["OZAGIA_DAEMON_SOCK"] == "/tmp/x.sock"
    assert captured["env"]["OPENCODE_CONFIG"] == str(tmp_path / "log" / "opencode_config.json")
    assert captured["argv"][:3] == ["opencode", "run", "--standalone"]



def test_daemon_gets_the_run_python_first_on_path(tmp_path, monkeypatch):
    seen = {}

    class Proc:
        pid = 0
        stdout = None

    def fake_popen(argv, **kw):
        seen["env"] = kw["env"]
        raise OSError("stop here")

    monkeypatch.setattr(ae.subprocess, "Popen", fake_popen)
    out = ae.start_daemon(tmp_path, "/usr/bin/python3", tmp_path / "log",
                          run_python=_python())
    assert out["daemon_ok"] is False
    first = Path(seen["env"]["PATH"].split(os.pathsep)[0])
    assert first == tmp_path / "log" / "bin"
    assert os.path.realpath(first / "python") == os.path.realpath(_python())


# ===================================================================================== #
# native driver (ozagia.harness.native) against a local stub server
# ===================================================================================== #
from test_harness_native import StubLLM, final, reply, tool_call  # noqa: E402

NATIVE_KEY = "sk-native-stub-0123456789"


def _native_fix_script() -> list[dict]:
    """read, edit, run the tests through the shim's `python`, then stop."""
    return [
        reply(tool_call("read", {"path": "pkg/mod.py"}, "r")),
        reply(tool_call("edit", {"path": "pkg/mod.py", "oldString": "return a - b",
                                 "newString": "return a + b"}, "e"),
              usage={"prompt_tokens": 20, "completion_tokens": 5, "cost": 0.5}),
        reply(tool_call("shell", {"command": "python -m pytest -q -p no:cacheprovider "
                                             "tests/test_mod.py"}, "s")),
        final("fixed"),
    ]


@pytest.fixture()
def native_env(monkeypatch):
    def setup(stub: StubLLM) -> None:
        monkeypatch.setenv(LIVE_ENV, "1")
        monkeypatch.setenv(ae.NATIVE_BASE_URL_ENV, stub.url)
        monkeypatch.setenv(ae.NATIVE_MODEL_ENV, "stub-model")
        monkeypatch.setenv(ae.NATIVE_KEY_ENV_ENV, "STUB_NATIVE_KEY")
        monkeypatch.delenv(ae.NATIVE_KEY_FILE_ENV, raising=False)
        monkeypatch.setenv("STUB_NATIVE_KEY", NATIVE_KEY)
    return setup


def test_native_driver_is_live_gated(monkeypatch):
    monkeypatch.delenv(LIVE_ENV, raising=False)
    with pytest.raises(LiveRunNotApproved):
        make_driver("native", live=True)
    monkeypatch.setenv(LIVE_ENV, "1")
    with pytest.raises(LiveRunNotApproved):
        make_driver("native", live=False)
    driver = make_driver("native", live=True, model="m")
    assert isinstance(driver, ae.NativeDriver) and driver.model == "m"
    assert driver.uses_daemon is False and "native" in ae.LIVE_DRIVERS
    # Built directly (as a test may), it still refuses to run without the env flag.
    monkeypatch.delenv(LIVE_ENV)
    with pytest.raises(LiveRunNotApproved):
        driver.run(Path("/nonexistent"), "p", "baseline", 5, Path("/nonexistent"))


def test_native_driver_refuses_dev_memory_before_any_worktree(fixture, native_env):
    with StubLLM() as stub:
        native_env(stub)
        driver = make_driver("native", live=True)
        with pytest.raises(HarnessError, match="no MCP client"):
            run_eval(fixture["repo"], [fixture["sign"]], ["baseline", "dev_memory"],
                     driver, fixture["base"], tasks_dir=fixture["tdir"],
                     python=_python(), run_root=fixture["run_root"])
        assert stub.requests == []
    assert len(_worktrees(fixture["repo"])) == 1
    assert ae.default_arms("native") == ["baseline", "graph_intent"]
    with pytest.raises(HarnessError, match="cannot run arm"):
        driver.argv(Path("/w"), Path("/p"), "dev_memory", 5, Path("/l"))


def test_native_preflight_lists_models(native_env, monkeypatch, tmp_path):
    with StubLLM(models=("other", "stub-model")) as stub:
        native_env(stub)
        driver = make_driver("native", live=True)
        driver.preflight()
        assert stub.requests[0]["path"] == "/v1/models"
        assert stub.requests[0]["headers"]["Authorization"] == f"Bearer {NATIVE_KEY}"
        driver.model = "missing-model"
        with pytest.raises(HarnessError, match="does not list model 'missing-model'"):
            driver.preflight()
        assert [r["method"] for r in stub.requests] == ["GET", "GET"]
        keyfile = tmp_path / "key"
        keyfile.write_text("from-file\n", encoding="utf-8")
        monkeypatch.delenv(ae.NATIVE_KEY_ENV_ENV)
        monkeypatch.setenv(ae.NATIVE_KEY_FILE_ENV, str(keyfile))
        make_driver("native", live=True).preflight()
        assert stub.requests[-1]["headers"]["Authorization"] == "Bearer from-file"
    monkeypatch.delenv(ae.NATIVE_BASE_URL_ENV)
    with pytest.raises(HarnessError, match="OZAGIA_NATIVE_BASE_URL"):
        make_driver("native", live=True).preflight()


def test_native_driver_runs_both_arms_end_to_end(fixture, native_env, monkeypatch):
    def no_daemon(*a, **kw):
        raise AssertionError("the native driver must not start the daemon")

    def no_plugin(*a, **kw):
        raise AssertionError("the native driver must not install the plugin")

    monkeypatch.setattr(ae, "start_daemon", no_daemon)
    monkeypatch.setattr(ae.opencode_plugin, "install", no_plugin)
    with StubLLM(_native_fix_script() + _native_fix_script()) as stub:
        native_env(stub)
        driver = make_driver("native", live=True)
        report = run_eval(fixture["repo"], [fixture["sign"]],
                          ["baseline", "graph_intent"], driver, fixture["base"],
                          tasks_dir=fixture["tdir"], python=_python(),
                          run_root=fixture["run_root"], timeout_s=120)
        requests = list(stub.requests)
    assert requests[0]["method"] == "GET"  # the preflight
    posts = [r for r in requests if r["method"] == "POST"]
    assert len(posts) == 8
    assert all(r["headers"]["Authorization"] == f"Bearer {NATIVE_KEY}" for r in posts)
    by_arm = {r["arm"]: r for r in report["runs"]}
    for arm, rec in by_arm.items():
        assert rec["success"] is True, (arm, rec)
        assert rec["driver_error"] is None and rec["exit_code"] == 0
        assert rec["tool_calls"] == 3 and rec["mcp_calls"] == 0
        assert rec["tools_by_name"] == {"edit": 1, "read": 1, "shell": 1}
        assert rec["tokens_in"] == 50 and rec["tokens_out"] == 11
        assert rec["cost_usd"] == 0.5
        assert rec["stopped"] == "done" and rec["steps"] == 4
        assert rec["files_changed"] == ["pkg/mod.py"] and rec["localized"] is True
        log = Path(rec["log_dir"])
        for name in ("transcript.jsonl", "summary.json"):
            assert NATIVE_KEY not in (log / "native" / name).read_text(encoding="utf-8")
        assert (log / "transcript.txt").read_text(encoding="utf-8") == "fixed"
    base, graph = by_arm["baseline"], by_arm["graph_intent"]
    assert "captured_calls" not in base and "daemon_ok" not in base
    assert graph["prep_ok"] is True
    assert graph["daemon_ok"] is None and graph["plugin_loaded"] is None
    assert graph["captured_calls"] == 3 and graph["fallbacks"] == 0
    # The model saw graph context in graph_intent only.
    read_plain = [m for m in posts[1]["body"]["messages"] if m["role"] == "tool"][0]
    read_graph = [m for m in posts[5]["body"]["messages"] if m["role"] == "tool"][0]
    assert "[ozagia]" not in read_plain["content"]
    assert "[ozagia] sym:pkg/mod.py::add" in read_graph["content"]
    rows = {r["arm"]: r for r in report["summary"]}
    assert rows["graph_intent"]["mean_captured_calls"] == 3.0
    assert rows["graph_intent"]["plugin_load_failures"] == 0
    assert report["backend"] == {"backend": "native", "model": "stub-model",
                                 "base_url": stub.url}
    assert len(_worktrees(fixture["repo"])) == 1


def test_native_driver_records_a_loop_error(fixture, native_env, monkeypatch):
    monkeypatch.setattr(ae, "start_daemon", None)
    with StubLLM([{"_status": 400, "_body": "bad model"}]) as stub:
        native_env(stub)
        driver = make_driver("native", live=True)
        rec = ae.run_one(fixture["repo"], fixture["sign"], "baseline", 1, driver,
                         fixture["base"], tasks_dir=fixture["tdir"], python=_python(),
                         run_root=fixture["run_root"], timeout_s=60, keep=False)
    assert rec["success"] is False and rec["exit_code"] == 1
    assert "HTTP 400" in rec["driver_error"] and rec["stopped"] == "error"


def test_tool_env_runs_the_same_ozagia_code(tmp_path, monkeypatch):
    """The daemon and prep subprocesses import the ozagia the eval runs
    (the v4 pilot ran an old daemon next to a new plugin without this)."""
    import sys
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    env = ae._tool_env(tmp_path, None, tool_python=sys.executable)
    assert env["PYTHONPATH"] == str(ae._ozagia_src())
    env = ae._tool_env(tmp_path, None, tool_python="/other/python")
    assert "PYTHONPATH" not in env


def test_graph_stats_fields_and_aggregate_read_working_set_counters():
    got = ae.graph_stats_fields({"hellos": 1, "working_sets_served": 2,
                                 "working_set_chars": 4100})
    assert (got["working_sets_served"], got["working_set_chars"]) == (2, 4100)
    runs = [{"arm": "graph_intent", "daemon_ok": True, **got},
            {"arm": "graph_intent", "daemon_ok": True, "working_sets_served": 1,
             "working_set_chars": None}]
    row = ae.aggregate(runs, ["graph_intent"])[0]
    assert (row["total_working_sets_served"], row["total_working_set_chars"]) == (3, 4100)


# ===================================================================================== #
# W20: the check interpreter and the validate preflight
# ===================================================================================== #
def test_check_files_and_modules():
    task = {"check": {"argv": ["-m", "pytest", "-q", "--confcutdir={workdir}",
                               "tests/test_a.py::test_x", "{workdir}/tests/test_b.py",
                               "tests/test_a.py::test_y", "/abs/test_c.py", "-k", "x"],
                      "timeout_s": 1}}
    assert ae.check_files(task) == ["tests/test_a.py", "tests/test_b.py"]
    assert ae.check_modules(task) == ["pytest"]


def test_default_check_python_is_the_env_that_has_the_imports(tmp_path, monkeypatch):
    from ozagia.core import environments as EN
    from venv_fixture import make_venv
    repo, _base = _repo(tmp_path)
    (repo / "tests" / "test_np.py").write_text(
        "import zzfake_numpy\n\n\ndef test_np():\n    assert zzfake_numpy\n",
        encoding="utf-8")
    venv_python = make_venv(repo / ".venv", {"fake-numpy": ["zzfake_numpy", "pytest"]})
    monkeypatch.setattr(EN, "_lock_candidates", lambda *a, **k: [])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    task = _task("np", BREAK_SIGN, node="tests/test_np.py::test_np")
    EN.clear_memo()
    chosen = ae.default_check_python(repo, [task], cache_mode="off")
    assert chosen["python"] == str(venv_python)
    assert chosen["missing"] == []
    # The repo's .venv is project-shipped: the harness runs it (trust=True),
    # the plain model reads it statically.
    EN.clear_memo()
    doc = EN.env_for(repo, ["tests/test_np.py"], cache_mode="off")
    assert doc["best"]["probe"] == "static" and doc["trusted"] is False
    EN.clear_memo()
    doc = EN.env_for(repo, ["tests/test_np.py"], cache_mode="off", trust=True)
    assert doc["best"]["probe"] == "exec"
    assert "root_venv" in chosen["reason"] and "zzfake_numpy" in chosen["reason"]
    assert ae.resolve_check_python(repo, [task], "/x/python") == (
        "/x/python", "explicit --python")
    # Nothing has the import: the interpreter missing the fewest, and why.
    other = _task("none", BREAK_SIGN, node="tests/test_none.py::test_n")
    (repo / "tests" / "test_none.py").write_text("import zz_nowhere_w20\n",
                                                 encoding="utf-8")
    chosen = ae.default_check_python(repo, [other], cache_mode="off")
    assert chosen["missing"] == ["zz_nowhere_w20"]
    assert "misses the fewest: zz_nowhere_w20" in chosen["reason"]


def test_runs_record_the_check_python(fixture, monkeypatch):
    explicit = run_eval(fixture["repo"], [fixture["sign"]], ["baseline"], NoopDriver(),
                        fixture["base"], tasks_dir=fixture["tdir"], python=_python(),
                        run_root=fixture["run_root"])
    assert explicit["check_python"] == _python()
    assert explicit["check_python_reason"] == "explicit --python"
    assert explicit["runs"][0]["check_python_reason"] == "explicit --python"
    asked = []
    monkeypatch.setattr(ae, "default_check_python", lambda repo, tasks: asked.append(
        [t["id"] for t in tasks]) or {"python": _python(), "reason": "stub reason",
                                      "missing": []})
    chosen = run_eval(fixture["repo"], [fixture["sign"]], ["baseline"], NoopDriver(),
                      fixture["base"], tasks_dir=fixture["tdir"],
                      run_root=fixture["run_root"])
    assert asked == [["add-sign"]]
    assert chosen["runs"][0]["check_python"] == _python()
    assert chosen["runs"][0]["check_python_reason"] == "stub reason"
    report = ae.validate_all(fixture["repo"], [fixture["sign"]], fixture["base"],
                             tasks_dir=fixture["tdir"], run_root=fixture["run_root"])
    assert report["check_python_reason"] == "stub reason" and report["ok"]


def test_run_refuses_tasks_that_fail_validation(tmp_path, capsys):
    repo, base = _repo(tmp_path)
    good = _task("add-sign", BREAK_SIGN)
    harmless = _task("comment-only", BREAK_COMMENT_ONLY)
    tdir = _tasks_dir(tmp_path, [(good, BREAK_SIGN), (harmless, BREAK_COMMENT_ONLY)])
    tasks = tdir / "tasks.json"
    tasks.write_text(json.dumps({"version": 1, "base": base, "tasks": [good, harmless]}),
                     encoding="utf-8")
    argv = ["run", "--driver", "fake", "--repo", str(repo), "--tasks", str(tasks),
            "--python", _python(), "--arms", "baseline"]
    out_json = tmp_path / "out.json"
    code = main([*argv, "--run-root", str(tmp_path / "r1"), "--out-json", str(out_json)])
    err = capsys.readouterr().err
    assert code == 2
    assert "refused: 1 of 2 task(s) fail validation" in err
    assert "comment-only" in err and "check still passes after the break patch" in err
    assert "add-sign:" not in err
    assert not out_json.exists()
    assert len(_worktrees(repo)) == 1
    # Only the good task: validated, then run.
    assert main([*argv, "--task-ids", "add-sign", "--run-root", str(tmp_path / "r2"),
                 "--out-json", str(out_json)]) == 0
    report = json.loads(out_json.read_text())
    assert report["runs"][0]["success"] is True
    assert report["validated"] is True
    assert report["validation"] == {"ran": True, "ok": True, "tasks": 1, "passed": 1,
                                    "failed": [], "base": base, "source": "preflight",
                                    "check_python": _python(),
                                    "check_python_reason": "explicit --python"}
    assert report["validated_tasks"] == ["add-sign"]
    assert "Validated before the run: yes (preflight), 1 of 1" in ae.render_markdown(report)
    # --validation reuses a report instead of validating again; the subset
    # of a larger report is enough, and only the selected tasks count.
    val_json = tmp_path / "val.json"
    assert main(["validate", "--repo", str(repo), "--tasks", str(tasks), "--python",
                 _python(), "--task-ids", "add-sign", "--run-root",
                 str(tmp_path / "v"), "--out-json", str(val_json)]) == 0
    capsys.readouterr()
    assert main([*argv, "--task-ids", "add-sign", "--validation", str(val_json),
                 "--run-root", str(tmp_path / "r2b"), "--out-json", str(out_json)]) == 0
    assert "validation: reused" in capsys.readouterr().out
    report = json.loads(out_json.read_text())
    assert report["validation"]["source"] == str(val_json)
    assert report["validated_tasks"] == ["add-sign"]
    assert not (tmp_path / "r2b" / "logs" / "validate-add-sign-base.log").exists()
    # ... and refuses one that does not cover the tasks, or another interpreter.
    assert main([*argv, "--validation", str(val_json),
                 "--run-root", str(tmp_path / "r2c")]) == 2
    assert "not in the report: comment-only" in capsys.readouterr().err
    other = [a if a != _python() else str(tmp_path / "other-python") for a in argv]
    assert main([*other, "--task-ids", "add-sign", "--validation", str(val_json),
                 "--run-root", str(tmp_path / "r2d")]) == 2
    assert "check interpreter" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main([*argv, "--skip-validate", "--validation", str(val_json)])
    capsys.readouterr()
    # --skip-validate starts anyway, and the report says so.
    assert main([*argv, "--skip-validate", "--run-root", str(tmp_path / "r3"),
                 "--out-json", str(out_json)]) == 0
    report = json.loads(out_json.read_text())
    assert [r["task"] for r in report["runs"]] == ["add-sign", "comment-only"]
    assert report["check_python_reason"] == "explicit --python"
    assert report["validated"] is False and report["validation"]["ran"] is False
    assert "Validated before the run: no" in ae.render_markdown(report)
    assert ae.validation_summary({"ok": False, "base": "b", "check_python": "p",
                                  "results": [{"id": "t", "ok": False,
                                               "details": {"reason": "r"}}]}) == {
        "ran": True, "ok": False, "tasks": 1, "passed": 0,
        "failed": [{"id": "t", "reason": "r"}], "base": "b", "source": "preflight",
        "check_python": "p", "check_python_reason": None}
    # Bad arms are refused before any validation.
    capsys.readouterr()
    assert main([*argv, "--arms", "nope", "--run-root", str(tmp_path / "r4")]) == 2
    assert "unknown arms" in capsys.readouterr().err
    assert not (tmp_path / "r4").exists()
# failure classes: invalid runs, the agent's last pytest
# ===================================================================================== #
FAILURE_FIX = ROOT / "tests" / "fixtures" / "failures"


def test_check_classes_and_invalid_runs_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "ozagia.core.environments", None)
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "pkg" / "mod.py").write_text(MOD_SRC, encoding="utf-8")
    (repo / "tests" / "test_mod.py").write_text(TEST_SRC, encoding="utf-8")
    (repo / "tests" / "test_env.py").write_text(
        "import w21_absent_mod\nfrom pkg.mod import add\n\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    sign = _task("add-sign", BREAK_SIGN)
    env = _task("add-env", BREAK_SIGN, node="tests/test_env.py::test_add")
    tdir = _tasks_dir(tmp_path, [(sign, BREAK_SIGN), (env, BREAK_SIGN)])
    report = run_eval(repo, [sign, env], ["baseline"], NoopDriver(), base,
                      tasks_dir=tdir, python=_python(), run_root=tmp_path / "runs")
    by_task = {r["task"]: r for r in report["runs"]}
    assert by_task["add-sign"]["failure_class"] == "assertion"
    assert by_task["add-sign"]["valid"] is True
    bad = by_task["add-env"]
    assert bad["failure_class"] == "environment" and bad["valid"] is False
    assert bad["failure_module"] == "w21_absent_mod"
    assert "missing module w21_absent_mod" in bad["invalid_reason"]
    assert any("No module named 'w21_absent_mod'" in e for e in bad["failure_evidence"])
    assert bad["agent_test_class"] is None          # noop runs no pytest
    (row,) = report["summary"]
    assert (row["runs"], row["valid_runs"], row["invalid_runs"]) == (2, 1, 1)
    assert (row["successes"], row["success_rate"]) == (0, 0.0)
    assert report["per_task"]["add-env"]["baseline"] == {
        "outcomes": [None], "successes": 0, "repeats": 0, "invalid": 1}
    # Not validated: the probe found the module missing outside the worktree.
    assert bad["validity_basis"].startswith("validation skipped; w21_absent_mod "
                                            "does not import under ")
    md = render_markdown(report)
    assert "## Invalid runs" in md
    assert "| add-env | baseline | 1 | environment | check environment: missing " \
           "module w21_absent_mod (third-party) under " in md
    assert "| baseline | 2 | 1 | 1 | 0 | 0.0 |" in md
    assert "| add-env | 0/0 (1 invalid) |" in md


def test_run_check_reports_a_missing_interpreter_as_harness(tmp_path):
    res = ae.run_check(tmp_path, {"argv": ["-c", "pass"], "timeout_s": 5},
                       str(tmp_path / "no-python"), tmp_path / "check.log")
    assert res["passed"] is False and res["returncode"] is None
    assert res["failure"]["class"] == "harness"
    got = ae.check_validity(res["failure"], python=str(tmp_path / "no-python"),
                            base_passed=True)
    assert got["valid"] is False
    assert "is missing and lies outside the worktree" in got["validity_basis"]


def test_success_rates_count_valid_runs_only():
    runs = [
        {**_run("t1", "baseline", 1, True), "valid": True, "failure_class": "passed"},
        {**_run("t2", "baseline", 1, False), "valid": False,
         "failure_class": "environment", "invalid_reason": "check environment: x"},
        {**_run("t3", "baseline", 1, False), "valid": False,
         "failure_class": "harness", "invalid_reason": "check harness: y"},
        _run("t4", "baseline", 1, False),        # recorded before failure classes
    ]
    (row,) = aggregate(runs, ["baseline"])
    assert (row["runs"], row["valid_runs"], row["invalid_runs"]) == (4, 2, 2)
    assert row["successes"] == 1 and row["success_rate"] == pytest.approx(0.5)
    (row,) = aggregate(runs[1:3], ["baseline"])
    assert row["successes"] == 0 and row["success_rate"] is None


def test_agent_last_pytest_from_the_transcript(tmp_path):
    log = tmp_path / "log"
    log.mkdir()
    (log / "driver.stdout.txt").write_text(
        (FAILURE_FIX / "driver_graph_intent.jsonl").read_text(encoding="utf-8"))
    got = ae.agent_test_fields(log)
    assert got["agent_test_class"] == "passed"
    assert got["agent_test_summary"] == ("pytest passed: exit 0, 0.959 s, 4 passed, "
                                         "0 failed, 0 error, 0 skipped")
    # Its first pytest (before the fix) failed an assertion.
    events = (FAILURE_FIX / "driver_graph_intent.jsonl").read_text().splitlines()
    (log / "driver.stdout.txt").write_text(events[1] + "\n")
    got = ae.agent_test_fields(log)
    assert got["agent_test_class"] == "assertion"
    assert got["agent_test_summary"].startswith("pytest failed: exit 1")
    # Baseline: no daemon head, a piped command (the exit is tail's): the
    # summary line decides.
    (log / "driver.stdout.txt").write_text(
        (FAILURE_FIX / "driver_baseline.jsonl").read_text(encoding="utf-8"))
    got = ae.agent_test_fields(log)
    assert got == {"agent_test_class": "passed", "agent_test_summary": "20 passed in 0.89s",
                   "agent_test_evidence": ["20 passed in 0.89s"]}
    (log / "driver.stdout.txt").write_text('{"type": "text", "part": {"text": "hi"}}\n')
    assert ae.agent_test_fields(log) == {"agent_test_class": None,
                                         "agent_test_summary": None}


def test_report_shows_agent_green_check_red():
    runs = [{**_run("learner", "graph_intent", 1, False), "valid": False,
             "failure_class": "environment", "invalid_reason": "check environment: numpy",
             "agent_test_class": "passed", "agent_test_summary": "4 passed in 0.72s"},
            {**_run("sign", "graph_intent", 1, True), "valid": True,
             "failure_class": "passed", "agent_test_class": "assertion",
             "agent_test_summary": "1 failed, 1 passed in 0.42s"}]
    report = {"driver": "opencode", "base": "abc", "arms": ["graph_intent"],
              "repeats": 1, "tasks": ["learner", "sign"], "runs": runs,
              "summary": aggregate(runs, ["graph_intent"]),
              "per_task": paired(runs, ["graph_intent"])}
    md = render_markdown(report)
    assert "## Agent tests and check" in md
    assert ("| learner | graph_intent | 1 | passed | 4 passed in 0.72s | environment | "
            "agent saw green, check saw red |") in md
    assert "| sign | graph_intent | 1 | assertion | 1 failed, 1 passed in 0.42s | " \
           "passed | agent saw red, check saw green |" in md
    assert ae.agent_check_note({"agent_test_class": "passed",
                                "failure_class": "passed"}) == ""


# ===================================================================================== #
# W21 review: validity basis, paired rates, the agent's last pytest
# ===================================================================================== #
BREAK_IMPORT = (
    "--- a/pkg/mod.py\n+++ b/pkg/mod.py\n@@ -1,2 +1,3 @@\n"
    "+import w21_absent_mod  # noqa: F401\n"
    " def add(a, b):\n     return a + b\n"
)


def test_check_validity_needs_proof_the_agent_did_not_cause_it(tmp_path):
    from ozagia.core import failures as F
    absent = F.classify(2, False, "E   ModuleNotFoundError: No module named "
                        "'w21_absent_mod'\n1 error in 0.1s\n", "")
    assert absent["class"] == "environment"
    # Validated base with this interpreter: the agent's.
    got = ae.check_validity(absent, base_passed=True, python=_python())
    assert got["valid"] is True and got["invalid_reason"] is None
    assert "after the base check passed" in got["validity_basis"]
    # Not validated: the probe decides.
    got = ae.check_validity(absent, python=_python(), workdir=tmp_path)
    assert got["valid"] is False
    assert "w21_absent_mod does not import under" in got["validity_basis"]
    third = F.classify(2, False, "E   ImportError: cannot import name 'arrray' from "
                       "'json' (/usr/lib/python3/json/__init__.py)\n1 error in 0.1s\n", "")
    assert (third["class"], third["module"]) == ("environment", "json")
    got = ae.check_validity(third, python=_python(), workdir=tmp_path)
    assert got["valid"] is True and "json imports under" in got["validity_basis"]
    # Nothing to probe, or a harness failure, with no validation: invalid.
    plugin = F.classify(4, False, "", "ERROR: usage: __main__.py\n__main__.py: error: "
                        "unrecognized arguments: --cov\n")
    assert ae.check_validity(plugin, python=_python())["valid"] is False
    harness = {"class": "harness", "detail": "the runner x was not found"}
    assert ae.check_validity(harness, python=_python())["valid"] is False
    assert ae.check_validity(harness, python=_python(), base_passed=True)["valid"] is True
    # A missing interpreter inside the worktree is the agent's too.
    inside = str(tmp_path / ".venv" / "bin" / "python")
    assert ae.check_validity(harness, python=inside, workdir=tmp_path,
                             base_passed=True)["valid"] is True
    assert ae.check_validity({"class": "assertion", "detail": "x"}) == {
        "valid": True, "invalid_reason": None, "validity_basis": "check class assertion"}


def test_import_probe_runs_outside_the_worktree(tmp_path, monkeypatch):
    (tmp_path / "w21_local_only.py").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    assert ae.import_probe(_python(), "w21_local_only") is False
    assert ae.import_probe(_python(), "json.decoder") is True
    assert ae.import_probe(str(tmp_path / "no-python"), "json") is None
    assert ae.import_probe(_python(), "x; import os") is None


def test_agent_caused_environment_failure_counts_after_validation(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "ozagia.core.environments", None)
    repo, base = _repo(tmp_path)
    task = _task("add-import", BREAK_IMPORT)
    tdir = _tasks_dir(tmp_path, [(task, BREAK_IMPORT)])
    validation = ae.validate_all(repo, [task], base, tasks_dir=tdir, python=_python(),
                                 run_root=tmp_path / "val")
    assert validation["ok"] and validation["check_python"] == _python()
    (res,) = validation["results"]
    assert res["details"]["broken_check"]["failure_class"] == "environment"
    report = run_eval(repo, [task], ["baseline"], NoopDriver(), base, tasks_dir=tdir,
                      python=_python(), run_root=tmp_path / "runs",
                      validation=validation)
    (run,) = report["runs"]
    assert report["validated_tasks"] == ["add-import"]
    assert run["failure_class"] == "environment" and run["valid"] is True
    assert run["success"] is False
    assert "counted against the agent" in run["validity_basis"]
    (row,) = report["summary"]
    assert (row["valid_runs"], row["invalid_runs"], row["success_rate"]) == (1, 0, 0.0)
    # A report written before W23 names the interpreter `python`: still read.
    legacy = {k: v for k, v in validation.items() if k != "check_python"}
    report = run_eval(repo, [task], ["baseline"], NoopDriver(), base, tasks_dir=tdir,
                      python=_python(), run_root=tmp_path / "runs-legacy",
                      validation={**legacy, "python": _python()})
    assert report["validated_tasks"] == ["add-import"]
    # Without the validation report the probe decides: the module is not
    # installed, so invalid.
    report = run_eval(repo, [task], ["baseline"], NoopDriver(), base,
                      tasks_dir=tdir, python=_python(),
                      run_root=tmp_path / "runs-none", validation=None)
    assert report["runs"][0]["valid"] is False
    assert report["validated_tasks"] == [] and report["validated"] is False
    assert "validation skipped" in report["runs"][0]["validity_basis"]
    # One made with another interpreter, base or task is refused, not
    # silently ignored.
    other_task = {**task, "check": {**task["check"], "timeout_s": 7}}
    for val, tasks, want in (
            ({**validation, "check_python": "/other/python"}, [task], "check interpreter"),
            ({**validation, "base": "0" * 40}, [task], "base"),
            (validation, [other_task], "check or break patch changed"),
            ({**validation, "results": []}, [task], "not in the report")):
        with pytest.raises(ae.HarnessError, match=want):
            run_eval(repo, tasks, ["baseline"], NoopDriver(), base, tasks_dir=tdir,
                     python=_python(), run_root=tmp_path / "runs-bad", validation=val)
    # A venv interpreter is not its base interpreter, though one symlinks
    # to the other.
    link = tmp_path / "venvish" / "python"
    link.parent.mkdir()
    link.symlink_to(_python())
    assert ae.validation_mismatch(validation, base=base, python=str(link),
                                  tasks=[task], tasks_dir=tdir)


def test_invalid_runs_that_differ_across_arms_compare_on_shared_cells():
    def r(task, arm, rep, success, valid=True):
        return {**_run(task, arm, rep, success), "valid": valid}
    runs = [r("t1", "baseline", 1, False, valid=False), r("t1", "baseline", 2, True),
            r("t2", "baseline", 1, True), r("t2", "baseline", 2, True),
            r("t1", "dev_memory", 1, True), r("t1", "dev_memory", 2, True),
            r("t2", "dev_memory", 1, False), r("t2", "dev_memory", 2, False, valid=False)]
    rows = {row["arm"]: row for row in aggregate(runs, ["baseline", "dev_memory"])}
    assert ae.paired_cells(runs, ["baseline", "dev_memory"]) == {("t1", 2), ("t2", 1)}
    b, d = rows["baseline"], rows["dev_memory"]
    assert (b["success_rate"], d["success_rate"]) == (1.0, pytest.approx(0.6667))
    assert (b["paired_runs"], b["paired_successes"], b["paired_rate"]) == (2, 2, 1.0)
    assert (d["paired_runs"], d["paired_successes"], d["paired_rate"]) == (2, 1, 0.5)
    per = paired(runs, ["baseline", "dev_memory"])
    assert per["t1"]["baseline"] == {"outcomes": [None, True], "successes": 1,
                                     "repeats": 1, "invalid": 1}
    assert per["t2"]["dev_memory"]["outcomes"] == [False, None]
    # A missing repeat (a run stopped by the cost cap) is None too.
    per = paired(runs[:-1], ["baseline", "dev_memory"])
    assert per["t2"]["dev_memory"] == {"outcomes": [False, None], "successes": 0,
                                       "repeats": 1, "invalid": 0}
    md = render_markdown({"driver": "noop", "base": "abc",
                          "arms": ["baseline", "dev_memory"], "repeats": 2,
                          "tasks": ["t1", "t2"], "runs": runs,
                          "summary": aggregate(runs, ["baseline", "dev_memory"]),
                          "per_task": paired(runs, ["baseline", "dev_memory"])})
    assert "| baseline | 4 | 3 | 1 | 3 | 1.0 | 1.0 (2/2) |" in md
    assert "| dev_memory | 4 | 3 | 1 | 2 | 0.6667 | 0.5 (1/2) |" in md


def test_cli_summary_line_uses_valid_runs():
    runs = [{**_run("t1", "baseline", 1, True), "valid": True},
            {**_run("t1", "baseline", 2, True), "valid": True},
            {**_run("t1", "baseline", 3, False), "valid": False}]
    (row,) = aggregate(runs, ["baseline"])
    assert ae.summary_line(row) == (
        "baseline: 2/2 valid runs succeeded (rate 1.0; 1 invalid of 3 runs; "
        "paired rate 1.0 over 2 cells), localized 0.0")


def test_agent_note_is_empty_for_unknown():
    for agent in ("unknown", None):
        assert ae.agent_check_note({"agent_test_class": agent,
                                    "failure_class": "passed"}) == ""
    assert ae.agent_check_note({"agent_test_class": "passed",
                                "failure_class": "unknown"}) == ""
    assert ae.agent_check_note({"agent_test_class": "assertion",
                                "failure_class": "passed"}) == \
        "agent saw red, check saw green"


def _tool_event(command: str, output: str, exit_code: int | None) -> str:
    meta = {"metadata": {"truncated": False}}
    if exit_code is not None:
        meta["metadata"]["exit"] = exit_code
    return json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": "bash", "state": {
        "status": "completed", "input": {"command": command}, "output": output,
        "metadata": meta}}})


def test_uncaptured_transcript_with_a_printed_head_uses_the_tool_exit(tmp_path):
    output = ("F.\n=================================== FAILURES ===================\n"
              "___________________ test_a ___________________\n"
              "----------------------------- Captured stdout call ------------------\n"
              "[ozagia] pytest passed: exit 0, 0.1 s, 2 passed, 0 failed, 0 error, "
              "0 skipped\n"
              "    def test_a():\n>       assert 1 == 2\nE       assert 1 == 2\n\n"
              "tests/test_a.py:3: AssertionError\n"
              "=========================== short test summary info ==========\n"
              "FAILED tests/test_a.py::test_a - assert 1 == 2\n1 failed, 1 passed in 0.1s\n")
    got = ae.last_agent_pytest(_tool_event("python -m pytest -q tests", output, 1))
    assert got["returncode"] == 1
    log = tmp_path / "log"
    log.mkdir()
    (log / "driver.stdout.txt").write_text(
        _tool_event("python -m pytest -q tests", output, 1) + "\n")
    fields = ae.agent_test_fields(log)
    assert fields["agent_test_class"] == "assertion"
    assert fields["agent_test_summary"] == "1 failed, 1 passed in 0.1s"
    # The printed head as the very last pytest line, then pytest's summary:
    # still not the daemon's.
    forged = output.replace("1 failed, 1 passed in 0.1s\n", "1 failed, 1 passed in "
                            "0.1s\n[ozagia] pytest passed: exit 0, 0.1 s, 2 passed, "
                            "0 failed, 0 error, 0 skipped\nextra\n")
    assert ae.last_agent_pytest(
        _tool_event("python -m pytest -q tests", forged, 1))["returncode"] == 1
    # A captured call (the head ends the output) keeps the daemon's exit.
    captured = output + ("[ozagia] pytest failed: exit 1, 0.1 s, 1 passed, 1 failed, "
                         "0 error, 0 skipped\n")
    assert ae.last_agent_pytest(
        _tool_event("python -m pytest -q tests", captured, None))["returncode"] == 1
# W22: per-component timing stored per run
# ===================================================================================== #
def test_run_records_timing_setup_and_one_repeat_index(fixture):
    other = dict(fixture["sign"], id="add-sign-2")
    report = run_eval(fixture["repo"], [fixture["sign"], other],
                      ["baseline", "graph_intent"], FakeDriver(), fixture["base"],
                      tasks_dir=fixture["tdir"], python=_python(),
                      run_root=fixture["run_root"])
    runs = report["runs"]
    assert all(isinstance(r.get("timing"), dict) for r in runs)
    for r in runs:
        assert r["setup_workdir_s"] >= 0
        assert r["timing"]["setup"]["workdir_s"] == r["setup_workdir_s"]
        assert r["timing"]["check"]["s"] == r["check_s"]
        assert r["timing"]["model"] is None          # the fake driver has no events
        assert isinstance(r["driver_started_at"], float)
    graph = [r for r in runs if r["arm"] == "graph_intent"]
    assert all(r["plugin_install_s"] is not None for r in graph)
    assert all(r["timing"]["setup"]["missing"] == [] for r in graph)
    # Measured once per run root, in the first indexing run: snapshot load +
    # refresh + tag cache on the unchanged tree, not a second build.
    measured = [r for r in runs if r.get("prep_repeat_ok") is not None]
    assert len(measured) == 1 and measured[0]["arm"] == "graph_intent"
    rec = measured[0]
    assert rec["prep_repeat_ok"] is True, rec["prep_repeat_error"]
    parts = rec["prep_repeat_parts"]
    assert parts["pending"] == 0
    assert rec["prep_repeat_s"] == pytest.approx(
        parts["load_s"] + parts["refresh_s"] + parts["tags_s"], abs=0.002)
    log_dir = Path(rec["log_dir"])
    assert (log_dir / "prep_repeat.log").is_file()
    assert not (log_dir / "repeat_index").exists()   # the scratch copy is gone
    assert (log_dir / "dev_graph.json").is_file()
    assert rec["timing"]["index"]["repeat_s"] == rec["prep_repeat_s"]
    assert report["index_repeat_s"] == rec["prep_repeat_s"]
    assert report["amortize_n"] == 2
    rows = {r["arm"]: r for r in report["summary"]}
    g = rows["graph_intent"]
    assert g["index_repeat_s"] == rec["prep_repeat_s"]
    assert rows["baseline"]["index_repeat_s"] is None
    assert g["amortized_s"] == pytest.approx(
        (g["mean_index_s"] + g["index_repeat_s"]) / 2 + g["mean_setup_s"]
        + g["per_task_s"], abs=0.002)
    md = render_markdown(report)
    assert "## Time per component" in md
    assert md.count("- Per task (agent time") == 1


def test_run_can_skip_the_repeat_index_and_set_n(fixture):
    report = run_eval(fixture["repo"], [fixture["sign"]], ["dev_memory"], NoopDriver(),
                      fixture["base"], tasks_dir=fixture["tdir"], python=_python(),
                      run_root=fixture["run_root"], repeat_index=False,
                      amortize=5)
    assert report["runs"][0].get("prep_repeat_ok") is None
    assert report["index_repeat_s"] is None
    assert report["summary"][0]["amortize_n"] == 5
    assert report["summary"][0]["repeat_assumed_zero"] is True
    with pytest.raises(HarnessError):
        run_eval(fixture["repo"], [fixture["sign"]], ["baseline"], NoopDriver(),
                 fixture["base"], tasks_dir=fixture["tdir"], amortize=0)


def test_failed_repeat_index_is_recorded_not_raised(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    out = ae.measure_repeat_index(tmp_path, _python(), log_dir)   # no snapshot
    assert out["prep_repeat_ok"] is False and out["prep_repeat_s"] is None
    assert "cannot copy the snapshot" in out["prep_repeat_error"]
    assert not (log_dir / "repeat_index").exists()
