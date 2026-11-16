"""ozagia.eval.agent — end-to-end task-success harness for coding agents.

Why this exists: retrieval recall does not guarantee task success. The studies
cited in docs/features/DEV_TOOLING_V2_PLAN.md (arXiv 2611.07286, 2610.33958)
report that better retrieval often leaves the end result unchanged. This
harness measures the end result: can an agent, given a symptom, make a failing
test pass again?

Each task is a break/fix case (see docs/features/AGENT_E2E_EVAL.md):

  1. A fresh detached git worktree is created at the base commit.
  2. The task's check (a pytest node-id list) must PASS on the base.
  3. The task's break patch is applied. The check must now FAIL.
  4. For a run, the driver (the agent under test) gets the task prompt in the
     broken worktree. The check then decides success.

Drivers:
  noop    does nothing. Expected success 0%. Negative control.
  fake    reverts the break patch with `git apply -R`. Expected success 100%.
          Validates the harness, not any agent.
  claude  Claude Code CLI in print mode. LIVE ONLY, untested live.
  opencode  OpenCode CLI in `run` mode. LIVE ONLY.
  native  ozagia's own agent loop (ozagia.harness.native) against an
          OpenAI-compatible endpoint. LIVE ONLY. baseline and graph_intent.

Arms:
  baseline    no ozagia MCP server. Claude Code runs with --strict-mcp-config.
  dev_memory  ozagia MCP server (ozagia.integrations.mcp.server)
              registered through a config file written into the workdir.
  graph_intent  no MCP server; the ozagia graph daemon runs for the session
              and the OpenCode plugin (ozagia.integrations.opencode) hands
              the agent's read/grep/glob/edit/write/shell calls to it.
              opencode, fake and noop drivers only. The native driver runs
              the intent layer in its own process instead (no daemon, no
              plugin).

Live drivers refuse to run unless both `--live` and the environment variable
OZAGIA_EVAL_LIVE=1 are set. A live run spends hosted-model tokens and
needs explicit user approval.

Usage:

    .venv/bin/python -m ozagia.eval.agent validate
    .venv/bin/python -m ozagia.eval.agent run --driver fake \\
        --arms baseline,dev_memory --out-json out.json --out-md out.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable
from ozagia.config import STATE_DIRNAME, config_path, repo_root
from ozagia.daemon.protocol import ENV_SOCK
from ozagia.integrations.opencode import install as opencode_plugin
from ozagia.intent.shell import GIT_REF_MOVING

ROOT = repo_root()
DEFAULT_TASKS = ROOT / "evaluation" / "agent_e2e" / "tasks.json"
DEFAULT_RUN_ROOT = Path("/tmp/ozagia_eval")
ARMS = ("baseline", "dev_memory", "graph_intent")
GRAPH_ARM = "graph_intent"
# Drivers the graph_intent arm runs with: the plugin is OpenCode's, and the
# local drivers exercise the daemon lifecycle without a model.
GRAPH_ARM_DRIVERS = ("opencode", "native", "fake", "noop")
DAEMON_READY_TIMEOUT_S = 120
# Per request to the daemon from the harness (stats, shutdown); the plugin
# uses the same 30 s.
DAEMON_REQUEST_TIMEOUT_S = 30
LIVE_ENV = "OZAGIA_EVAL_LIVE"
MCP_SERVER_NAME = "ozagia"
MCP_MODULE = "ozagia.integrations.mcp.server"
MCP_CONFIG_CLAUDE = ".ozagia_eval_mcp.json"
MCP_CONFIG_OPENCODE = ".ozagia_eval_opencode.json"
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
LIVE_DRIVERS = ("claude", "opencode", "native")
LOCAL_DRIVERS = ("noop", "fake")
# A driver error that means no session can succeed; the run stops at once.
_AUTH_ERROR_RE = re.compile(r"authenticat|not logged in|invalid api key|oauth",
                            re.IGNORECASE)


class HarnessError(RuntimeError):
    """A harness step failed for a reason the operator must fix."""


class LiveRunNotApproved(HarnessError):
    """A live driver was requested without both --live and the env flag."""


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #
def load_tasks(path: str | Path) -> dict[str, Any]:
    """Load and check a task file. Returns {"base", "tasks", "path"}."""
    path = Path(path).resolve()
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise HarnessError(f"{path}: expected an object with version 1")
    tasks = doc.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise HarnessError(f"{path}: 'tasks' must be a non-empty list")
    seen: set[str] = set()
    for task in tasks:
        _check_task(task, path.parent, seen)
    return {"base": doc.get("base"), "tasks": tasks, "path": str(path),
            "dir": str(path.parent)}


def _check_task(task: Any, tasks_dir: Path, seen: set[str]) -> None:
    if not isinstance(task, dict):
        raise HarnessError("every task must be an object")
    tid = task.get("id")
    if not isinstance(tid, str) or not TASK_ID_RE.match(tid):
        raise HarnessError(f"invalid task id: {tid!r}")
    if tid in seen:
        raise HarnessError(f"duplicate task id: {tid}")
    seen.add(tid)
    for key in ("prompt", "area", "break_patch", "notes"):
        if not isinstance(task.get(key), str) or not task[key].strip():
            raise HarnessError(f"{tid}: '{key}' must be a non-empty string")
    if not (tasks_dir / task["break_patch"]).is_file():
        raise HarnessError(f"{tid}: break_patch not found: {task['break_patch']}")
    check = task.get("check")
    if not isinstance(check, dict):
        raise HarnessError(f"{tid}: 'check' must be an object")
    argv = check.get("argv")
    if (not isinstance(argv, list) or not argv
            or not all(isinstance(a, str) for a in argv)):
        raise HarnessError(f"{tid}: check.argv must be a non-empty list of strings")
    timeout = check.get("timeout_s")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        raise HarnessError(f"{tid}: check.timeout_s must be a positive integer")
    expected = task.get("expected_files")
    if (not isinstance(expected, list) or not expected
            or not all(isinstance(f, str) and f for f in expected)):
        raise HarnessError(f"{tid}: expected_files must be a non-empty list of paths")


def select_tasks(tasks: list[dict[str, Any]],
                 ids: Iterable[str] | None) -> list[dict[str, Any]]:
    """Tasks in id order, optionally restricted to `ids`."""
    ordered = sorted(tasks, key=lambda t: t["id"])
    if ids is None:
        return ordered
    wanted = {str(i).strip() for i in ids if str(i).strip()}
    known = {t["id"] for t in ordered}
    unknown = sorted(wanted - known)
    if unknown:
        raise HarnessError(f"unknown task ids: {', '.join(unknown)}")
    return [t for t in ordered if t["id"] in wanted]


# --------------------------------------------------------------------------- #
# git and worktrees
# --------------------------------------------------------------------------- #
def _git(args: list[str], cwd: str | Path, *, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise HarnessError(
            f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc


def new_run_root(base_dir: str | Path = DEFAULT_RUN_ROOT) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_root = Path(base_dir) / f"{stamp}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    run_root.mkdir(parents=True, exist_ok=True)
    return run_root


def make_workdir(repo: str | Path, base: str, run_root: Path, name: str, *,
                 isolation: str = "worktree") -> Path:
    """A checkout of `base` under run_root/work/<name>.

    `worktree` is a detached linked worktree: fast, and fine for the local
    drivers. `clone` is a shared local clone with its `origin` removed. Live
    agents get a clone: it has its own refs, so a ref the agent moves is not a
    ref of the main repository, and the agent's worktree tools see no sibling
    checkouts of the main repository.
    """
    path = run_root / "work" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isolation == "worktree":
        _git(["worktree", "add", "--detach", str(path), base], repo)
    elif isolation == "clone":
        _git(["clone", "--quiet", "--shared", "--no-checkout",
              str(Path(repo).resolve()), str(path)], repo)
        _git(["checkout", "--quiet", "--detach", base], path)
        _git(["remote", "remove", "origin"], path)
    else:
        raise HarnessError(f"unknown isolation: {isolation}")
    return path


def _worktree_admin_dir(path: Path) -> Path | None:
    """The admin dir a linked worktree points at, read from its `.git` file."""
    try:
        text = (path / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    admin = Path(text.split(":", 1)[1].strip())
    return admin if admin.is_absolute() else (path / admin).resolve()


def remove_workdir(repo: str | Path, path: Path, run_root: Path) -> list[str]:
    """Remove a worktree the harness created under run_root/work.

    Only this worktree is touched. There is no repo-wide `git worktree prune`,
    because that would also drop the admin entries of unrelated worktrees whose
    directories are missing. If `git worktree remove --force` fails, the
    directory is deleted and this worktree's own admin dir (under
    `<common git dir>/worktrees/`) is deleted too. Returns warnings; an empty
    list means the cleanup was complete.
    """
    owned = (run_root.resolve() / "work").resolve()
    if owned not in path.resolve().parents:
        raise HarnessError(f"refusing to remove a path the harness did not create: {path}")
    warnings: list[str] = []
    if (path / ".git").is_dir():
        # A clone made by make_workdir(isolation="clone"): nothing in the
        # source repository refers to it, so deleting the directory is enough.
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            warnings.append(f"directory still present after cleanup: {path}")
        return warnings
    admin = _worktree_admin_dir(path)
    proc = _git(["worktree", "remove", "--force", str(path)], repo, check=False)
    if proc.returncode == 0 and not path.exists():
        return warnings
    warnings.append("git worktree remove --force failed: "
                    + (proc.stderr.strip() or f"exit {proc.returncode}"))
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        warnings.append(f"directory still present after cleanup: {path}")
    common = _git(["rev-parse", "--git-common-dir"], repo, check=False)
    if admin is not None and common.returncode == 0:
        common_dir = (Path(repo) / common.stdout.strip()).resolve()
        worktrees_dir = (common_dir / "worktrees").resolve()
        if worktrees_dir in admin.resolve().parents and admin.exists():
            shutil.rmtree(admin, ignore_errors=True)
            warnings.append(f"removed admin entry {admin.name} by hand")
    return warnings


def apply_patch(workdir: Path, patch: Path, *, reverse: bool = False) -> dict[str, Any]:
    args = ["apply", "--whitespace=nowarn"]
    if reverse:
        args.append("-R")
    args.append(str(patch))
    proc = _git(args, workdir, check=False)
    return {"ok": proc.returncode == 0,
            "output": (proc.stdout + proc.stderr).strip()}


def _snapshot(workdir: Path) -> dict[str, str | None]:
    """Content digest of every modified, deleted or untracked file.

    ozagia's own state directory and the harness-installed OpenCode plugin
    are never part of it, whether or not the target repository ignores them."""
    proc = _git(["ls-files", "-z", "-m", "-d", "-o", "--exclude-standard"], workdir)
    plugin = opencode_plugin.PLUGIN_REL.as_posix()
    snap: dict[str, str | None] = {}
    for rel in sorted(p for p in proc.stdout.split("\0")
                      if p and p.split("/", 1)[0] != STATE_DIRNAME and p != plugin):
        target = workdir / rel
        if target.is_file():
            snap[rel] = hashlib.sha256(target.read_bytes()).hexdigest()
        else:
            snap[rel] = None
    return snap


def changed_since(before: dict[str, str | None],
                  after: dict[str, str | None]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #
def _tail(text: str, lines: int = 30) -> str:
    return "\n".join(text.splitlines()[-lines:])


KILL_GRACE_S = 5.0


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def spawn_group(argv: list[str], *, cwd: str | Path, env: dict[str, str],
                timeout_s: float) -> dict[str, Any]:
    """Run argv in its own session and process group, with a time limit.

    On timeout the whole group gets SIGTERM, the harness waits up to
    KILL_GRACE_S for it to exit, and then SIGKILL is sent to the group. The
    driver's children (an MCP server, a language server, a sleeping shell) are
    killed with it, so they do not outlive the run.
    """
    started = time.monotonic()
    # PWD must match cwd: OpenCode takes its project directory from $PWD, so an
    # inherited PWD would point the agent at the operator's checkout.
    env = dict(env, PWD=str(Path(cwd).resolve()))
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8",
                            errors="replace", start_new_session=True)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        pgid = proc.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + KILL_GRACE_S
        while time.monotonic() < deadline:
            proc.poll()  # reaps the leader so only real survivors keep the group
            if not _group_alive(pgid):
                break
            time.sleep(0.05)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            out, err = proc.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = "", ""
    return {"returncode": None if timed_out else proc.returncode,
            "stdout": out or "", "stderr": err or "", "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 3)}


def run_check(workdir: Path, check: dict[str, Any], python: str,
              log_path: Path) -> dict[str, Any]:
    argv = [a.replace("{workdir}", str(workdir)) for a in check["argv"]]
    cmd = [python, *argv]
    timeout = int(check["timeout_s"])
    # No .pyc files: a break that keeps the byte length (a + b -> a - b) within the
    # same mtime second would otherwise run stale bytecode from the base check.
    env = dict(os.environ, COLUMNS="250", PYTHONDONTWRITEBYTECODE="1")
    res = spawn_group(cmd, cwd=workdir, env=env, timeout_s=timeout)
    out, err = res["stdout"], res["stderr"]
    returncode, timed_out = res["returncode"], res["timed_out"]
    seconds = res["seconds"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"$ {' '.join(cmd)}\n\n{out}\n--- stderr ---\n{err}\n",
                        encoding="utf-8")
    return {"passed": (not timed_out) and returncode == 0,
            "returncode": returncode, "timed_out": timed_out,
            "seconds": seconds, "log": str(log_path),
            "tail": _tail(out + "\n" + err, 12)}


# --------------------------------------------------------------------------- #
# validation: pass on base, fail after the break
# --------------------------------------------------------------------------- #
def validate_task(repo: str | Path, task: dict[str, Any], base: str, *,
                  tasks_dir: str | Path = DEFAULT_TASKS.parent,
                  python: str | None = None, run_root: Path | None = None,
                  keep: bool = False) -> dict[str, Any]:
    python = python or sys.executable
    run_root = run_root or new_run_root()
    log_dir = run_root / "logs"
    tid = task["id"]
    started = time.monotonic()
    workdir = make_workdir(repo, base, run_root, f"validate-{tid}")
    cleanup: list[str] = []
    try:
        base_check = run_check(workdir, task["check"], python,
                               log_dir / f"validate-{tid}-base.log")
        applied = apply_patch(workdir, Path(tasks_dir) / task["break_patch"])
        broken_check: dict[str, Any] | None = None
        if applied["ok"]:
            broken_check = run_check(workdir, task["check"], python,
                                     log_dir / f"validate-{tid}-broken.log")
        pass_on_base = base_check["passed"]
        fail_after_break = broken_check is not None and not broken_check["passed"]
        ok = bool(pass_on_base and applied["ok"] and fail_after_break)
        details: dict[str, Any] = {
            "base_check": {k: base_check[k] for k in ("returncode", "seconds", "log")},
            "break_apply": applied,
        }
        if broken_check is not None:
            details["broken_check"] = {k: broken_check[k]
                                       for k in ("returncode", "seconds", "log")}
        if not ok:
            details["reason"] = _validation_reason(pass_on_base, applied["ok"],
                                                   fail_after_break)
        result = {"id": tid, "ok": ok, "pass_on_base": pass_on_base,
                  "fail_after_break": fail_after_break,
                  "seconds": round(time.monotonic() - started, 3),
                  "details": details, "cleanup_warnings": cleanup}
        if keep:
            result["workdir"] = str(workdir)
        return result
    finally:
        if not keep:
            cleanup.extend(remove_workdir(repo, workdir, run_root))


def _validation_reason(pass_on_base: bool, applied: bool, fail_after_break: bool) -> str:
    if not pass_on_base:
        return "check does not pass on base"
    if not applied:
        return "break patch does not apply to base"
    if not fail_after_break:
        return "check still passes after the break patch"
    return "unknown"


def validate_all(repo: str | Path, tasks: list[dict[str, Any]], base: str, *,
                 tasks_dir: str | Path, python: str | None = None,
                 run_root: Path | None = None, keep: bool = False) -> dict[str, Any]:
    run_root = run_root or new_run_root()
    results = [validate_task(repo, t, base, tasks_dir=tasks_dir, python=python,
                             run_root=run_root, keep=keep) for t in tasks]
    return {"version": 1, "base": base, "run_root": str(run_root),
            "ok": all(r["ok"] for r in results), "results": results}


# --------------------------------------------------------------------------- #
# drivers
# --------------------------------------------------------------------------- #
def _empty_result(**fields: Any) -> dict[str, Any]:
    out = {"exit_code": None, "wall_s": 0.0, "tokens_in": None,
           "tokens_out": None, "cost_usd": None, "tool_calls": None,
           "mcp_calls": None, "tools_by_name": None,
           "transcript": "", "timed_out": False, "driver_error": None}
    out.update(fields)
    return out


class NoopDriver:
    """Does nothing. Negative control; expected success 0%."""
    name = "noop"

    def run(self, workdir: Path, prompt: str, arm: str, timeout_s: int,
            log_dir: Path, *, patch_path: Path | None = None,
            extra_env: dict[str, str] | None = None) -> dict[str, Any]:
        return _empty_result(exit_code=0)


class FakeDriver:
    """Reverts the break patch. A perfect agent, used to validate the harness."""
    name = "fake"

    def run(self, workdir: Path, prompt: str, arm: str, timeout_s: int,
            log_dir: Path, *, patch_path: Path | None = None,
            extra_env: dict[str, str] | None = None) -> dict[str, Any]:
        if patch_path is None:
            raise HarnessError("fake driver needs the task's break patch")
        started = time.monotonic()
        applied = apply_patch(workdir, patch_path, reverse=True)
        return _empty_result(exit_code=0 if applied["ok"] else 1,
                             wall_s=round(time.monotonic() - started, 3),
                             transcript=applied["output"])


def _parse_claude_json(stdout: str) -> dict[str, Any]:
    """Fields from `claude -p --output-format json`: result, total_cost_usd and
    usage.{input_tokens,output_tokens}. Names confirmed in the installed
    binary's strings (version 2.1.322), not by a live run."""
    try:
        doc = json.loads(stdout)
    except ValueError:
        return {}
    if not isinstance(doc, dict):
        return {}
    usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}

    def number(value: Any) -> Any:
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    error = None
    if doc.get("is_error") or doc.get("terminal_reason") == "api_error":
        error = str(doc.get("result") or doc.get("terminal_reason") or "error")
    return {"tokens_in": number(usage.get("input_tokens")),
            "tokens_out": number(usage.get("output_tokens")),
            "cost_usd": number(doc.get("total_cost_usd")),
            "driver_error": error,
            "transcript": doc["result"] if isinstance(doc.get("result"), str) else stdout}


# argv templates. "{prompt}" and "{mcp_config}" are replaced as whole tokens.
# Git subcommands that move or rewrite shared refs. Bash stays allowed for the
# rest. Residual risk: a pattern matches the command's text, so `git -C dir
# commit` or `git --git-dir=... push` gets past it. See AGENT_E2E_EVAL.md.
DISALLOWED_GIT_TOOLS = (
    "Bash(git commit:*)", "Bash(git push:*)", "Bash(git stash:*)",
    "Bash(git branch:*)", "Bash(git checkout:*)", "Bash(git switch:*)",
    "Bash(git reset:*)", "Bash(git rebase:*)", "Bash(git merge:*)",
    "Bash(git tag:*)", "Bash(git worktree:*)",
)
DRIVERS: dict[str, dict[str, Any]] = {
    "claude": {
        "argv": ["claude", "-p", "{prompt}", "--output-format", "json",
                 "--permission-mode", "acceptEdits", "--no-session-persistence",
                 "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash",
                 # Refs are shared with the canonical checkout (detached worktrees
                 # share refs and stash), so the agent may not move them.
                 # Comma-joined: one token, and the next token is a flag.
                 "--disallowedTools", ",".join(DISALLOWED_GIT_TOOLS)],
        "arm_argv": {
            "baseline": ["--strict-mcp-config"],
            "dev_memory": ["--strict-mcp-config", "--mcp-config", "{mcp_config}"],
        },
        "mcp_config_file": MCP_CONFIG_CLAUDE,
        "parse": _parse_claude_json,
    },
    "opencode": {
        # --standalone: a private server, so the user's background service and
        # its cached project config do not leak into the run. --archived keeps
        # eval sessions out of the user's session list.
        "argv": ["opencode", "run", "--standalone", "--format", "json", "--auto",
                 "--archived", "{prompt}"],
        "arm_argv": {"baseline": [], "dev_memory": [], "graph_intent": []},
        # Every arm gets a generated config (git denylist; MCP only for
        # dev_memory). See CommandDriver._opencode_config.
        "arm_env": {"baseline": {"OPENCODE_CONFIG": "{driver_config}"},
                    "dev_memory": {"OPENCODE_CONFIG": "{driver_config}"},
                    "graph_intent": {"OPENCODE_CONFIG": "{driver_config}"}},
        "mcp_config_file": MCP_CONFIG_OPENCODE,
        "model_flag": "-m",
        "parse": lambda stdout: _parse_opencode_events(stdout),
    },
}

# Same refs-protection intent as DISALLOWED_GIT_TOOLS, in OpenCode's
# permission syntax (glob over the bash command text, last match wins): the
# original entries, then one per remaining `intent.shell.GIT_REF_MOVING`
# subcommand.
_OPENCODE_GIT_DENY_BASE = (
    "git commit*", "git push*", "git stash*", "git branch*", "git checkout*",
    "git switch*", "git reset*", "git rebase*", "git merge*", "git tag*",
    "git worktree*",
)
OPENCODE_GIT_DENY = _OPENCODE_GIT_DENY_BASE + tuple(
    f"git {sub}*" for sub in sorted(GIT_REF_MOVING)
    if f"git {sub}*" not in _OPENCODE_GIT_DENY_BASE)
DRIVERS["claude"]["model_flag"] = "--model"
DRIVER_ENV = "OZAGIA_EVAL_DRIVER"


# OpenCode 2.3 Code Mode runs MCP tools inside its `execute` tool, as
# `tools.ozagia.dev_code_search(...)` or `tools["ozagia"].dev_code_search(...)`.
# Each such call is one MCP call.
_MCP_CODE_CALL_RE = re.compile(
    r"""tools\s*(?:\[\s*["']""" + re.escape(MCP_SERVER_NAME) + r"""["']\s*\]|\.\s*"""
    + re.escape(MCP_SERVER_NAME) + r""")\s*\.\s*\w+\s*\(""")


def _parse_opencode_events(stdout: str) -> dict[str, Any]:
    """Totals from `opencode run --format json` (one JSON event per line).

    Confirmed against opencode 2.3.0 output: `step_finish` parts carry `cost`
    and `tokens.{input,output}`, `tool_use` events mark tool calls, `text`
    parts carry the reply. An `error` event, or no finished step at all, is a
    driver error.
    """
    cost = 0.0
    tokens_in = tokens_out = 0
    tools = steps = 0
    by_name: dict[str, int] = {}
    mcp = 0
    texts: list[str] = []
    error: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if kind == "step_finish":
            steps += 1
            if isinstance(part.get("cost"), (int, float)):
                cost += float(part["cost"])
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            tokens_in += int(tok.get("input") or 0)
            tokens_out += int(tok.get("output") or 0)
        elif kind == "tool_use":
            tools += 1
            tool = str(part.get("tool") or "?")
            by_name[tool] = by_name.get(tool, 0) + 1
            if tool.startswith(MCP_SERVER_NAME):
                mcp += 1
            else:
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                inp = state.get("input") if isinstance(state.get("input"), dict) else {}
                mcp += len(_MCP_CODE_CALL_RE.findall(str(inp.get("code") or "")))
        elif kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "error":
            detail = event.get("error") or part.get("error") or event
            error = json.dumps(detail)[:500] if not isinstance(detail, str) else detail[:500]
    if error is None and steps == 0:
        error = "opencode produced no finished step"
    return {"tokens_in": tokens_in if steps else None,
            "tokens_out": tokens_out if steps else None,
            "cost_usd": round(cost, 6) if steps else None,
            "tool_calls": tools, "driver_error": error,
            "mcp_calls": mcp,
            "tools_by_name": dict(sorted(by_name.items())),
            "transcript": "\n".join(texts) or stdout}


class CommandDriver:
    """A live CLI agent. Built only through make_driver, which enforces approval.

    Live agents get a clone workdir (see make_workdir). `python` is the run's
    interpreter; run_eval sets it, and its bin dir goes first on the agent's
    PATH so `python` resolves to the venv the checks use.
    """

    isolation = "clone"

    def __init__(self, name: str, *, max_budget_usd: float | None = None,
                 model: str | None = None) -> None:
        self.name = name
        self.spec = DRIVERS[name]
        self.max_budget_usd = max_budget_usd
        self.model = model
        self.python: str | None = None

    def _argv(self, prompt: str, arm: str, mcp_config: Path | None) -> list[str]:
        argv = list(self.spec["argv"])
        argv.extend(self.spec["arm_argv"].get(arm, []))
        if self.max_budget_usd is not None and self.name == "claude":
            argv.extend(["--max-budget-usd", str(self.max_budget_usd)])
        if self.model:
            # Before the prompt token, so a prompt never swallows the flag.
            at = argv.index("{prompt}")
            argv[at:at] = [self.spec["model_flag"], self.model]
        out = []
        for token in argv:
            if token == "{prompt}":
                out.append(prompt)
            elif token == "{mcp_config}":
                if mcp_config is None:
                    raise HarnessError(f"{self.name} arm {arm} needs an MCP config")
                out.append(str(mcp_config))
            else:
                out.append(token)
        return out

    def run(self, workdir: Path, prompt: str, arm: str, timeout_s: int,
            log_dir: Path, *, patch_path: Path | None = None,
            extra_env: dict[str, str] | None = None) -> dict[str, Any]:
        _require_live_env(self.name)
        mcp = workdir / self.spec["mcp_config_file"] if arm == "dev_memory" else None
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **(extra_env or {}))
        log_dir.mkdir(parents=True, exist_ok=True)
        if self.python:
            env["PATH"] = os.pathsep.join(
                [str(self._python_shim(log_dir)), env.get("PATH", "")])
        driver_config = (self._opencode_config(arm, mcp, log_dir)
                         if self.name == "opencode" else None)
        for key, value in self.spec.get("arm_env", {}).get(arm, {}).items():
            env[key] = (value.replace("{mcp_config}", str(mcp))
                        .replace("{driver_config}", str(driver_config)))
        argv = self._argv(prompt, arm, mcp)
        res = spawn_group(argv, cwd=workdir, env=env, timeout_s=timeout_s)
        code, out, err = res["returncode"], res["stdout"], res["stderr"]
        timed_out, wall = res["timed_out"], res["seconds"]
        (log_dir / "driver.stdout.txt").write_text(out, encoding="utf-8")
        (log_dir / "driver.stderr.txt").write_text(err, encoding="utf-8")
        parsed = self.spec["parse"](out)
        return _empty_result(exit_code=code, wall_s=wall, timed_out=timed_out,
                             tokens_in=parsed.get("tokens_in"),
                             tokens_out=parsed.get("tokens_out"),
                             cost_usd=parsed.get("cost_usd"),
                             tool_calls=parsed.get("tool_calls"),
                             mcp_calls=parsed.get("mcp_calls"),
                             tools_by_name=parsed.get("tools_by_name"),
                             driver_error=parsed.get("driver_error"),
                             transcript=parsed.get("transcript", out))

    def _python_shim(self, log_dir: Path) -> Path:
        return python_shim(log_dir, self.python)

    def _opencode_config(self, arm: str, mcp: Path | None, log_dir: Path) -> Path:
        """The OPENCODE_CONFIG for one session, written under log_dir.

        Every arm denies the ref-moving git commands. Only dev_memory adds the
        ozagia MCP server (copied from the arm config in the workdir).
        graph_intent gets the baseline config: its tools come from the plugin
        in the workdir, and the agent's tool list matches baseline.
        """
        doc: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "permission": {"bash": {"*": "allow",
                                    **{p: "deny" for p in OPENCODE_GIT_DENY}}},
        }
        if arm == "dev_memory":
            if mcp is None or not mcp.is_file():
                raise HarnessError("dev_memory arm needs the generated MCP config")
            doc["mcp"] = json.loads(mcp.read_text(encoding="utf-8"))["mcp"]
        path = log_dir / "opencode_config.json"
        log_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        return path

    def preflight(self) -> None:
        """Refuse before any worktree is made when the CLI cannot authenticate.

        `claude auth status` makes no model call. An API key in the
        environment also counts as credentials.
        """
        if self.name == "opencode":
            self._opencode_preflight()
            return
        if self.name != "claude":
            return
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
                "CLAUDE_CODE_OAUTH_TOKEN"):
            return
        try:
            proc = subprocess.run(["claude", "auth", "status"], capture_output=True,
                                  text=True, timeout=30, stdin=subprocess.DEVNULL)
            status = json.loads(proc.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise HarnessError(f"claude auth status failed: {exc}") from exc
        if not status.get("loggedIn"):
            raise HarnessError(
                "claude is not logged in (claude auth status: loggedIn=false). "
                "Run `claude` and /login, or set ANTHROPIC_API_KEY, then retry.")

    def _opencode_preflight(self) -> None:
        """The CLI runs, and the requested model (if any) is one it lists.

        `opencode models` makes no model call.
        """
        try:
            proc = subprocess.run(["opencode", "models"], capture_output=True,
                                  text=True, timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessError(f"opencode is not usable: {exc}") from exc
        if proc.returncode != 0:
            raise HarnessError(
                f"opencode models failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        if self.model:
            listed = {line.strip() for line in proc.stdout.splitlines()}
            if self.model.split("#", 1)[0] not in listed:
                raise HarnessError(
                    f"opencode does not list model {self.model!r}; "
                    "see `opencode models`")

    def describe(self) -> dict[str, Any]:
        """Backend identity for the report."""
        model = self.model
        if model is None and self.name == "opencode":
            model = _opencode_default_model()
        return {"backend": self.name, "model": model or "cli default"}


# --------------------------------------------------------------------------- #
# native: ozagia's own agent loop (ozagia.harness.native)
# --------------------------------------------------------------------------- #
NATIVE_BASE_URL_ENV = "OZAGIA_NATIVE_BASE_URL"
NATIVE_MODEL_ENV = "OZAGIA_NATIVE_MODEL"
NATIVE_KEY_FILE_ENV = "OZAGIA_NATIVE_API_KEY_FILE"
NATIVE_KEY_ENV_ENV = "OZAGIA_NATIVE_API_KEY_ENV"
NATIVE_MODULE = "ozagia.harness.native"
# The loop stops itself at the session timeout; its process group is killed
# this much later if it has not exited.
NATIVE_GRACE_S = 30
# Arms per driver, where a driver cannot run every arm. The native loop has
# no MCP client, so no dev_memory.
DRIVER_ARMS: dict[str, tuple[str, ...]] = {"native": ("baseline", GRAPH_ARM)}
NATIVE_MODES = {"baseline": "plain", GRAPH_ARM: "graph"}


def _ozagia_src() -> Path:
    """The directory this ozagia package is imported from, so the loop's
    subprocess runs the same code as the harness."""
    import ozagia
    return Path(ozagia.__file__).resolve().parent.parent


class NativeDriver:
    """`python -m ozagia.harness.native run` against an OpenAI-compatible
    endpoint. Built only through make_driver, which enforces approval.

    baseline runs the loop's plain tools; graph_intent runs its graph mode
    with the dump prep built. The loop calls the intent layer in process, so
    run_one starts no daemon and installs no plugin (`uses_daemon = False`).
    """

    name = "native"
    isolation = "clone"
    uses_daemon = False
    arms = DRIVER_ARMS["native"]

    def __init__(self, *, model: str | None = None, base_url: str | None = None,
                 api_key_file: str | None = None,
                 api_key_env: str | None = None) -> None:
        self.base_url = (base_url or os.environ.get(NATIVE_BASE_URL_ENV) or "").rstrip("/")
        self.model = model or os.environ.get(NATIVE_MODEL_ENV) or None
        self.api_key_file = api_key_file or os.environ.get(NATIVE_KEY_FILE_ENV) or None
        self.api_key_env = api_key_env or os.environ.get(NATIVE_KEY_ENV_ENV) or None
        self.python: str | None = None

    def _check_config(self) -> None:
        if not self.base_url:
            raise HarnessError(f"native driver needs {NATIVE_BASE_URL_ENV}")
        if not self.model:
            raise HarnessError(f"native driver needs --model or {NATIVE_MODEL_ENV}")
        if self.api_key_file and self.api_key_env:
            raise HarnessError(f"set only one of {NATIVE_KEY_FILE_ENV} and "
                               f"{NATIVE_KEY_ENV_ENV}")

    def _key_args(self) -> list[str]:
        if self.api_key_file:
            return ["--api-key-file", self.api_key_file]
        if self.api_key_env:
            return ["--api-key-env", self.api_key_env]
        return []

    def argv(self, workdir: Path, prompt_file: Path, arm: str, timeout_s: int,
             log_dir: Path) -> list[str]:
        if arm not in NATIVE_MODES:
            raise HarnessError(f"driver native cannot run arm {arm!r} "
                               f"(arms: {', '.join(self.arms)})")
        argv = [sys.executable, "-m", NATIVE_MODULE, "run",
                "--workdir", str(workdir), "--prompt-file", str(prompt_file),
                "--mode", NATIVE_MODES[arm], "--base-url", self.base_url,
                "--model", str(self.model), "--log-dir", str(log_dir / "native"),
                "--timeout", str(timeout_s), *self._key_args()]
        if arm == GRAPH_ARM:
            argv += ["--dump", str(log_dir / "dev_graph.json")]
        return argv

    def run(self, workdir: Path, prompt: str, arm: str, timeout_s: int,
            log_dir: Path, *, patch_path: Path | None = None,
            extra_env: dict[str, str] | None = None) -> dict[str, Any]:
        _require_live_env(self.name)
        self._check_config()
        log_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = log_dir / "prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        argv = self.argv(workdir, prompt_file, arm, timeout_s, log_dir)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                   OZAGIA_ROOT=str(workdir), PYTHONPATH=str(_ozagia_src()),
                   **(extra_env or {}))
        if self.python:
            env["PATH"] = os.pathsep.join(
                [str(python_shim(log_dir, self.python)), env.get("PATH", "")])
        res = spawn_group(argv, cwd=workdir, env=env,
                          timeout_s=timeout_s + NATIVE_GRACE_S)
        out, err = res["stdout"], res["stderr"]
        (log_dir / "driver.stdout.txt").write_text(out, encoding="utf-8")
        (log_dir / "driver.stderr.txt").write_text(err, encoding="utf-8")
        summary = _parse_native_summary(out, log_dir / "native" / "summary.json")
        error = None
        if summary is None:
            error = ("native loop wrote no summary"
                     + (f" (killed after {res['seconds']} s)" if res["timed_out"]
                        else f" (exit {res['returncode']})")
                     + (": " + _tail(err, 5) if err.strip() else ""))
            summary = {}
        elif summary.get("stopped") == "error":
            error = str(summary.get("error") or "native loop error")
        return _empty_result(
            exit_code=res["returncode"], wall_s=res["seconds"],
            timed_out=res["timed_out"] or summary.get("stopped") == "timeout",
            tokens_in=summary.get("tokens_in"),
            tokens_out=summary.get("tokens_out"),
            cost_usd=summary.get("cost_usd"),
            tool_calls=summary.get("tool_calls"), mcp_calls=0,
            tools_by_name=summary.get("tools_by_name"),
            driver_error=error,
            transcript=str(summary.get("final_text") or ""),
            captured_calls=summary.get("captured_calls"),
            passthrough_calls=summary.get("passthrough_calls"),
            fallbacks=summary.get("fallbacks"),
            stopped=summary.get("stopped"), steps=summary.get("steps"))

    def preflight(self) -> None:
        """The endpoint answers `GET {base_url}/models` and lists the model.
        No completion is requested."""
        self._check_config()
        from ozagia.harness import native
        try:
            key = native.read_api_key(env_name=self.api_key_env,
                                      file=self.api_key_file)
            listed = native.list_models(self.base_url, key)
        except (OSError, ValueError, native.ModelError) as exc:
            raise HarnessError(f"native preflight failed: {exc}") from exc
        if self.model not in listed:
            raise HarnessError(
                f"{self.base_url}/models does not list model {self.model!r}")

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "model": self.model,
                "base_url": self.base_url}


def _parse_native_summary(stdout: str, path: Path) -> dict[str, Any] | None:
    """The loop's summary: its stdout, else the summary.json it wrote."""
    for text in (stdout, None):
        try:
            if text is None:
                text = path.read_text(encoding="utf-8")
            doc = json.loads(text)
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and "stopped" in doc:
            return doc
    return None


def _opencode_default_model() -> str | None:
    """The `model` key of the user's opencode config, for the report only."""
    for name in ("opencode.json", "opencode.jsonc", "config.json"):
        path = Path.home() / ".config" / "opencode" / name
        try:
            text = re.sub(r"^\s*//.*$", "", path.read_text(encoding="utf-8"),
                          flags=re.M)
            model = json.loads(text).get("model")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(model, str) and model:
            return f"{model} (opencode config default)"
    return None


def _require_live_env(name: str) -> None:
    if os.environ.get(LIVE_ENV) != "1":
        raise LiveRunNotApproved(
            f"driver {name!r} makes hosted-model calls. Set {LIVE_ENV}=1 and pass --live.")


def make_driver(name: str | None, *, live: bool = False,
                max_budget_usd: float | None = None,
                model: str | None = None) -> Any:
    """Build a driver. `name` None means $OZAGIA_EVAL_DRIVER."""
    name = name or os.environ.get(DRIVER_ENV) or ""
    if not name:
        raise HarnessError(
            f"no driver: pass --driver or set {DRIVER_ENV} "
            f"(one of {', '.join(sorted(LOCAL_DRIVERS + LIVE_DRIVERS))})")
    if name in LOCAL_DRIVERS:
        return {"noop": NoopDriver, "fake": FakeDriver}[name]()
    if name in LIVE_DRIVERS:
        if not live or os.environ.get(LIVE_ENV) != "1":
            raise LiveRunNotApproved(
                f"driver {name!r} makes hosted-model calls and needs both --live "
                f"and {LIVE_ENV}=1. Get user approval first.")
        if name == "native":
            return NativeDriver(model=model)
        return CommandDriver(name, max_budget_usd=max_budget_usd, model=model)
    raise HarnessError(f"unknown driver: {name}")


def write_arm_config(workdir: Path, arm: str, tool_python: str,
                     log_dir: Path, *, config: Path | None = None) -> list[Path]:
    """Write the MCP registration for an arm into the workdir.

    baseline writes nothing. dev_memory writes one Claude Code file and one
    OpenCode file. Both start the same stdio server with a snapshot and a journal
    kept under log_dir, so the workdir stays clean. The files are untracked and
    are written before the change snapshot, so they never count as agent edits.

    The server runs with `tool_python`, the interpreter ozagia is installed in,
    and acts on the workdir through OZAGIA_ROOT. The workdir is not put on
    PYTHONPATH, so a task that breaks the target's code cannot break the tool.
    `config` is the source repository's project config (OZAGIA_CONFIG): the
    workdir is at the task's base commit, which may predate the config."""
    if arm in ("baseline", GRAPH_ARM):
        return []
    if arm != "dev_memory":
        raise HarnessError(f"unknown arm: {arm}")
    env = {
        "OZAGIA_ROOT": str(workdir),
        "OZAGIA_DUMP": str(log_dir / "dev_graph.json"),
        "OZAGIA_JOURNAL": str(log_dir / "journal.jsonl"),
        # The snapshot is built by prepare_dev_memory before the driver starts,
        # so the server never builds it inside the timed agent session.
        "OZAGIA_AUTOBUILD": "0",
    }
    if config is not None:
        env["OZAGIA_CONFIG"] = str(config)
    log_dir.mkdir(parents=True, exist_ok=True)
    claude_doc = {"mcpServers": {MCP_SERVER_NAME: {
        "command": tool_python, "args": ["-m", MCP_MODULE], "env": env}}}
    opencode_doc = {"$schema": "https://opencode.ai/config.json", "mcp": {
        MCP_SERVER_NAME: {"type": "local", "command": [tool_python, "-m", MCP_MODULE],
                          "environment": env, "enabled": True}}}
    paths = [workdir / MCP_CONFIG_CLAUDE, workdir / MCP_CONFIG_OPENCODE]
    for path, doc in zip(paths, (claude_doc, opencode_doc)):
        path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return paths


# --------------------------------------------------------------------------- #
# run loop
# --------------------------------------------------------------------------- #
PREP_TIMEOUT_S = 900
_PREP_CODE = (
    "import sys\n"
    "from pathlib import Path\n"
    "from ozagia.core.memory import build_index\n"
    "from ozagia.core.repo_map import update_tag_cache\n"
    "work = Path(sys.argv[1])\n"
    "build_index(work, Path(sys.argv[2]))\n"
    "update_tag_cache(work, cache_path=work / '.ozagia' / 'tags.json')\n"
)


def _tool_env(workdir: Path, config: Path | None, *,
              tool_python: str | None = None) -> dict[str, str]:
    """Environment for ozagia subprocesses acting on `workdir` (prep, daemon).

    The caller's PYTHONPATH is dropped (a target's path must not break the
    tool). When `tool_python` is this interpreter, PYTHONPATH is the
    directory this ozagia package was imported from, so the subprocess runs
    the same ozagia code as the eval (as `harness.session.tool_env` does)."""
    env = dict(os.environ, OZAGIA_ROOT=str(workdir), PYTHONDONTWRITEBYTECODE="1",
               COLUMNS="250")
    env.pop("PYTHONPATH", None)
    if tool_python and os.path.abspath(tool_python) == os.path.abspath(sys.executable):
        env["PYTHONPATH"] = str(_ozagia_src())
    if config is not None:
        env["OZAGIA_CONFIG"] = str(config)
    else:
        env.pop("OZAGIA_CONFIG", None)
    return env


def prepare_dev_memory(workdir: Path, tool_python: str,
                       log_dir: Path, *, config: Path | None = None) -> dict[str, Any]:
    """Build the dev-memory snapshot and warm the tag cache, outside the timer.

    Runs in a subprocess with `tool_python` (where ozagia is installed) and
    OZAGIA_ROOT=workdir, so the snapshot describes the broken workdir. The
    graph dump goes to log_dir and the tag cache to `<workdir>/.ozagia/`, which
    `_snapshot` ignores, so it is not an agent edit.
    A failed prep is recorded in the run; the driver still runs and the check
    still decides, so a broken index shows up as a result, not a silent skip.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    env = _tool_env(workdir, config, tool_python=tool_python)
    res = spawn_group([tool_python, "-c", _PREP_CODE, str(workdir),
                       str(log_dir / "dev_graph.json")],
                      cwd=workdir, env=env, timeout_s=PREP_TIMEOUT_S)
    ok = res["returncode"] == 0 and not res["timed_out"]
    (log_dir / "prep.log").write_text(
        f"{res['stdout']}\n--- stderr ---\n{res['stderr']}\n", encoding="utf-8")
    if ok:
        error = None
    elif res["timed_out"]:
        error = f"prep timed out after {PREP_TIMEOUT_S} s"
    else:
        error = _tail(res["stderr"], 12) or f"exit {res['returncode']}"
    return {"prep_s": res["seconds"], "prep_ok": ok, "prep_error": error}


# --------------------------------------------------------------------------- #
# graph_intent: the graph daemon for one run
# --------------------------------------------------------------------------- #
def daemon_command(tool_python: str, workdir: Path, log_dir: Path,
                   sock: Path) -> list[str]:
    """The argv that starts the graph daemon for one run.

    Module-level so tests can substitute a stub server; the stub must accept
    connections on `sock`, print `ready <sock>` and answer stats and shutdown.
    `--parent-pid` makes the daemon exit if this process dies without
    stopping it."""
    return [tool_python, "-m", "ozagia.daemon", "serve", "--root", str(workdir),
            "--dump", str(log_dir / "dev_graph.json"),
            "--journal", str(log_dir / "daemon_journal.jsonl"), "--sock", str(sock),
            "--parent-pid", str(os.getpid())]


def _daemon_sock_path() -> Path:
    """A short socket path: Unix socket paths are limited to about 100 bytes."""
    return Path(tempfile.gettempdir()) / f"ozagia-eval-{uuid.uuid4().hex[:12]}.sock"


def _daemon_request(sock: Path, msg: dict[str, Any],
                    timeout_s: float = DAEMON_REQUEST_TIMEOUT_S) -> dict[str, Any]:
    """One request on a fresh connection; the response line, parsed."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout_s)
        conn.connect(str(sock))
        conn.sendall((json.dumps({"id": 1, **msg}) + "\n").encode("utf-8"))
        with conn.makefile("rb") as fh:
            line = fh.readline()
    if not line:
        raise HarnessError(f"daemon closed the connection on {msg.get('op')}")
    doc = json.loads(line)
    if not isinstance(doc, dict) or not doc.get("ok"):
        raise HarnessError(f"daemon {msg.get('op')} failed: {doc}")
    return doc


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the process group, wait KILL_GRACE_S, then SIGKILL survivors."""
    pgid = proc.pid
    proc.poll()
    if not _group_alive(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + KILL_GRACE_S
    while time.monotonic() < deadline:
        proc.poll()
        if not _group_alive(pgid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        pass


class GraphDaemon:
    """A started daemon process (its own process group) and its socket."""

    def __init__(self, proc: subprocess.Popen, sock: Path,
                 pump: threading.Thread) -> None:
        self.proc = proc
        self.sock = sock
        self.pump = pump

    def stop(self) -> dict[str, Any]:
        """Ask for stats, shut it down, kill the group if it is still alive,
        and remove the socket. Returns {"daemon_stats", "daemon_stop_error"}."""
        stats: Any = None
        error = None
        try:
            stats = _daemon_request(self.sock, {"op": "stats"}).get("stats")
        except (OSError, ValueError, HarnessError) as exc:
            error = f"stats failed: {exc}"
        try:
            _daemon_request(self.sock, {"op": "shutdown"})
            self.proc.wait(timeout=KILL_GRACE_S)
        except (OSError, ValueError, HarnessError, subprocess.TimeoutExpired):
            pass
        _kill_group(self.proc)
        self.pump.join(timeout=KILL_GRACE_S)
        if self.sock.is_socket():
            self.sock.unlink()
        return {"daemon_stats": stats, "daemon_stop_error": error}


def python_shim(log_dir: Path, python: str) -> Path:
    """A bin dir under log_dir whose `python` and `python3` are `python`.

    The interpreter's own dir is not enough: a resolved venv python lives in
    a base install that may have no `python` name. The agent's shell and the
    graph daemon (which runs the agent's `python -m pytest`) both get it
    first on PATH."""
    shim = log_dir / "bin"
    shim.mkdir(parents=True, exist_ok=True)
    for name in ("python", "python3"):
        link = shim / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(python)
    return shim


def start_daemon(workdir: Path, tool_python: str, log_dir: Path, *,
                 config: Path | None = None,
                 run_python: str | None = None) -> dict[str, Any]:
    """Start the graph daemon and wait for its `ready` line.

    Returns {"daemon": GraphDaemon | None, "sock", "daemon_ok",
    "daemon_error", "daemon_ready_s"}. A daemon that fails to start is
    killed and reported, not raised: the run goes on without it (the plugin
    then falls back to the built-ins on every call)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    sock = _daemon_sock_path()
    argv = daemon_command(tool_python, workdir, log_dir, sock)
    env = dict(_tool_env(workdir, config, tool_python=tool_python),
               PWD=str(Path(workdir).resolve()))
    if run_python:
        # `python` in the agent's commands is the run's interpreter.
        env["PATH"] = os.pathsep.join(
            [str(python_shim(log_dir, run_python)), env.get("PATH", "")])
    started = time.monotonic()

    def failed(error: str) -> dict[str, Any]:
        return {"daemon": None, "sock": sock, "daemon_ok": False,
                "daemon_error": error,
                "daemon_ready_s": round(time.monotonic() - started, 3)}

    stderr_path = log_dir / "daemon.stderr.txt"
    try:
        with stderr_path.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(argv, cwd=str(workdir), env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=stderr, text=True, encoding="utf-8",
                                    errors="replace", start_new_session=True)
    except OSError as exc:
        return failed(f"daemon did not start: {exc}")
    ready = threading.Event()
    seen: dict[str, Any] = {}

    def pump() -> None:
        # Copies the daemon's stdout to the log for its whole life, so the
        # pipe never fills; the first `ready <path>` line marks it ready.
        with (log_dir / "daemon.stdout.txt").open("w", encoding="utf-8") as out:
            for line in proc.stdout:
                out.write(line)
                out.flush()
                if "ready" not in seen and line.startswith("ready "):
                    seen["ready"] = line[len("ready "):].strip()
                    ready.set()
        seen["eof"] = True
        ready.set()

    thread = threading.Thread(target=pump, name="ozagia-daemon-stdout", daemon=True)
    thread.start()
    # Also watch the process: a daemon that exits leaving a child holding its
    # stdout would otherwise never show EOF.
    deadline = started + DAEMON_READY_TIMEOUT_S
    while not ready.wait(0.05):
        if proc.poll() is not None:
            ready.wait(0.5)  # let the pump read a last `ready` line, if any
            seen.setdefault("eof", True)
            break
        if time.monotonic() >= deadline:
            break
    daemon = GraphDaemon(proc, Path(seen["ready"]) if seen.get("ready") else sock, thread)
    if seen.get("ready"):
        return {"daemon": daemon, "sock": daemon.sock, "daemon_ok": True,
                "daemon_error": None,
                "daemon_ready_s": round(time.monotonic() - started, 3)}
    if seen.get("eof"):
        try:
            code: Any = proc.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            code = "still running"
        error = (f"daemon exited before ready (exit {code}): "
                 + _tail(stderr_path.read_text(encoding="utf-8", errors="replace"), 12))
    else:
        error = f"daemon not ready after {DAEMON_READY_TIMEOUT_S} s"
    _kill_group(proc)
    thread.join(timeout=KILL_GRACE_S)
    if sock.is_socket():
        sock.unlink()
    return failed(error.strip())


def _count(value: Any) -> int | None:
    """A count from a stats field: an int, a list's length, or a dict's sum."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        nums = [v for v in value.values() if isinstance(v, int) and not isinstance(v, bool)]
        return sum(nums) if nums else 0
    return None


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


# Read-ledger counters of the daemon's `stats` (`ozagia.intent.ledger`),
# copied into each run record and totalled per arm.
READ_LEDGER_FIELDS = ("repeats_seen", "stubs_served", "notes_served",
                      "outlines_served", "bytes_saved")
# Affected-test selection and the test-result cache (`ozagia.intent.affected`).
TEST_SELECTION_FIELDS = ("affected_runs", "tests_skipped_unaffected",
                         "cached_passes", "full_runs_forced")
# Lint inside the edit (`ozagia.intent.lint`) and captured `ruff check`
# calls, copied into each run record and totalled per arm.
LINT_FIELDS = ("lint_runs", "lint_findings", "lint_timeouts", "ruff_captures")


def graph_stats_fields(stats: Any) -> dict[str, Any]:
    """Run-record fields from the daemon's `stats`: plugin_loaded
    (hellos > 0), captured_calls (tool calls the daemon handled; an expired
    request is never handled), passthrough_calls (left to the built-in by
    design), fallbacks (`stats.fallbacks`: one per call the plugin could not
    get answered; see `ozagia.daemon.server`), preflights (permission
    checks before a handled edit, write or shell), permission_assert and
    codemode_tools (from the plugin's `hello`), and shell_captured /
    shell_passthrough (`stats.shell`: shell calls the daemon carried out or
    left to the built-in; absent from older daemons), structured_queries,
    structured_outlines and jq_captures (structured data reads), and the
    read ledger's repeats_seen, stubs_served, notes_served, outlines_served
    and bytes_saved (`ozagia.intent.ledger`), and patch_checks,
    patch_misses_caught, region_replies and key_edits (patch and edit miss
    recovery), and jobs_started, job_waits, job_wait_seconds and
    polls_avoided (background jobs, `ozagia.daemon.jobs`), lint_runs,
    lint_findings, lint_timeouts and ruff_captures (`LINT_FIELDS`, lint
    inside the edit), and affected_runs, tests_skipped_unaffected,
    cached_passes and full_runs_forced (`TEST_SELECTION_FIELDS`, test
    selection, `ozagia.intent.affected`)."""
    if not isinstance(stats, dict):
        return {"plugin_loaded": None, "captured_calls": None,
                "passthrough_calls": None, "fallbacks": None,
                "preflights": None, "permission_assert": None,
                "codemode_tools": None, "shell_captured": None,
                "shell_passthrough": None,
                "structured_queries": None, "structured_outlines": None,
                "jq_captures": None, **{k: None for k in READ_LEDGER_FIELDS},
                "patch_checks": None, "patch_misses_caught": None,
                "region_replies": None, "key_edits": None, "jobs_started": None, "job_waits": None,
                "job_wait_seconds": None, "polls_avoided": None,
                **{k: None for k in LINT_FIELDS},
                **{k: None for k in TEST_SELECTION_FIELDS}}
    hellos = _count(stats.get("hellos"))
    raw = stats.get("handled")
    # The daemon reports {"true": n, "false": m}; only handled calls count.
    handled = (_count(raw.get("true")) if isinstance(raw, dict) else _count(raw))
    if handled is None and isinstance(stats.get("by_handled"), dict):
        handled = _count(stats["by_handled"].get("true"))
    codemode = stats.get("codemode_tools")
    shell = stats.get("shell") if isinstance(stats.get("shell"), dict) else {}
    return {"plugin_loaded": None if hellos is None else hellos > 0,
            "captured_calls": handled,
            "passthrough_calls": _count(stats.get("passthrough")),
            "fallbacks": _count(stats.get("fallbacks")),
            # Permission checks before handled mutating calls, and whether
            # the harness offered the permission API.
            "preflights": _count(stats.get("preflights")),
            "permission_assert": stats.get("permission_assert"),
            # Replaced tools OpenCode could also reach through Code Mode.
            "codemode_tools": list(codemode) if isinstance(codemode, list) else None,
            "shell_captured": _count(shell.get("captured")),
            "shell_passthrough": _count(shell.get("passthrough")),
            "structured_queries": _count(stats.get("structured_queries")),
            "structured_outlines": _count(stats.get("structured_outlines")),
            "jq_captures": _count(stats.get("jq_captures")),
            **{k: _count(stats.get(k)) for k in READ_LEDGER_FIELDS},
            "patch_checks": _count(stats.get("patch_checks")),
            "patch_misses_caught": _count(stats.get("patch_misses_caught")),
            "region_replies": _count(stats.get("region_replies")),
            "key_edits": _count(stats.get("key_edits")),
            "jobs_started": _count(stats.get("jobs_started")),
            "job_waits": _count(stats.get("waits")),
            "job_wait_seconds": (round(float(stats["wait_seconds"]), 1)
                                 if isinstance(stats.get("wait_seconds"), (int, float))
                                 and not isinstance(stats.get("wait_seconds"), bool)
                                 else None),
            "polls_avoided": _count(stats.get("polls_avoided")),
            **{k: _count(stats.get(k)) for k in LINT_FIELDS},
            **{k: _count(stats.get(k)) for k in TEST_SELECTION_FIELDS}}



def run_one(repo: str | Path, task: dict[str, Any], arm: str, repeat: int,
            driver: Any, base: str, *, tasks_dir: str | Path, python: str,
            run_root: Path, timeout_s: int, keep: bool,
            isolation: str = "worktree",
            tool_python: str | None = None) -> dict[str, Any]:
    tool_python = tool_python or sys.executable
    config = config_path(repo)
    tid = task["id"]
    name = f"{tid}-{arm}-r{repeat}"
    log_dir = run_root / "logs" / name
    workdir = make_workdir(repo, base, run_root, name, isolation=isolation)
    cleanup: list[str] = []
    record: dict[str, Any] = {"task": tid, "arm": arm, "repeat": repeat,
                              "driver": driver.name, "prep_s": 0.0,
                              "prep_ok": None, "prep_error": None,
                              "cleanup_warnings": cleanup}
    graph = arm == GRAPH_ARM
    # A driver that runs the intent layer itself (native) needs no daemon
    # and no plugin.
    uses_daemon = graph and getattr(driver, "uses_daemon", True)
    if graph:
        record.update(daemon_ok=None, daemon_error=None, daemon_ready_s=None,
                      daemon_stats=None, daemon_stop_error=None,
                      plugin_loaded=None, captured_calls=None,
                      passthrough_calls=None, fallbacks=None)
    daemon: GraphDaemon | None = None
    plugin_installed = False
    try:
        patch = Path(tasks_dir) / task["break_patch"]
        applied = apply_patch(workdir, patch)
        if not applied["ok"]:
            record.update(success=False, error="break patch does not apply",
                          files_changed=[], localized=False, wall_s=0.0)
            return record
        write_arm_config(workdir, arm, tool_python, log_dir, config=config)
        if arm in ("dev_memory", GRAPH_ARM):
            prep = prepare_dev_memory(workdir, tool_python, log_dir, config=config)
            record.update(prep_s=prep["prep_s"], prep_ok=prep["prep_ok"],
                          prep_error=prep["prep_error"])
        run_kwargs: dict[str, Any] = {}
        if uses_daemon:
            started = start_daemon(workdir, tool_python, log_dir, config=config,
                                   run_python=python)
            daemon = started["daemon"]
            record.update(daemon_ok=started["daemon_ok"],
                          daemon_error=started["daemon_error"],
                          daemon_ready_s=started["daemon_ready_s"])
            # Installed before the "before" snapshot and removed after the
            # "after" one; _snapshot also never lists it.
            opencode_plugin.install(workdir)
            plugin_installed = True
            run_kwargs["extra_env"] = {ENV_SOCK: str(started["sock"])}
        elif graph and config is not None:
            # The loop's graph must use the layout prep built it with.
            run_kwargs["extra_env"] = {"OZAGIA_CONFIG": str(config)}
        before = _snapshot(workdir)
        result = driver.run(workdir, task["prompt"], arm, timeout_s, log_dir,
                            patch_path=patch, **run_kwargs)
        after = _snapshot(workdir)
        if uses_daemon:
            if daemon is not None:
                stopped = daemon.stop()
                daemon = None
                record.update(stopped)
                record.update(graph_stats_fields(stopped["daemon_stats"]))
            opencode_plugin.uninstall(workdir)
            plugin_installed = False
        elif graph:
            record.update(captured_calls=result.get("captured_calls"),
                          passthrough_calls=result.get("passthrough_calls"),
                          fallbacks=result.get("fallbacks"))
        if "stopped" in result:
            record.update(stopped=result["stopped"], steps=result.get("steps"))
        files_changed = changed_since(before, after)
        check = run_check(workdir, task["check"], python, log_dir / "check.log")
        expected = [f.replace("\\", "/") for f in task["expected_files"]]
        record.update(
            success=check["passed"],
            check_returncode=check["returncode"],
            check_s=check["seconds"],
            exit_code=result["exit_code"],
            wall_s=result["wall_s"],
            timed_out=result["timed_out"],
            tokens_in=result["tokens_in"],
            tokens_out=result["tokens_out"],
            cost_usd=result["cost_usd"],
            tool_calls=result["tool_calls"],
            mcp_calls=result.get("mcp_calls"),
            tools_by_name=result.get("tools_by_name"),
            driver_error=result.get("driver_error"),
            files_changed=files_changed,
            localized=any(f in files_changed for f in expected),
            diff_vs_base=_git(["diff", "--name-only", base], workdir).stdout.split(),
            log_dir=str(log_dir),
        )
        (log_dir / "transcript.txt").write_text(result["transcript"] or "", encoding="utf-8")
        return record
    finally:
        if daemon is not None:
            daemon.stop()
        if plugin_installed:
            opencode_plugin.uninstall(workdir)
        if not keep:
            cleanup.extend(remove_workdir(repo, workdir, run_root))
        else:
            record["workdir"] = str(workdir)


def run_eval(repo: str | Path, tasks: list[dict[str, Any]], arms: Iterable[str],
             driver: Any, base: str, *, tasks_dir: str | Path = DEFAULT_TASKS.parent,
             repeats: int = 1, python: str | None = None, run_root: Path | None = None,
             keep: bool = False, timeout_s: int = 900,
             max_total_usd: float | None = None) -> dict[str, Any]:
    """Run every task × arm × repeat. `max_total_usd` stops the run once the
    reported cost of finished sessions exceeds it (a soft cap: the session
    that crosses it still completes)."""
    arms = list(arms)
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise HarnessError(f"unknown arms: {', '.join(unknown)}")
    if repeats < 1:
        raise HarnessError("repeats must be at least 1")
    if GRAPH_ARM in arms and driver.name not in GRAPH_ARM_DRIVERS:
        raise HarnessError(
            f"arm {GRAPH_ARM} needs one of the drivers {', '.join(GRAPH_ARM_DRIVERS)}: "
            f"its plugin is OpenCode's, and driver {driver.name!r} has none")
    allowed = getattr(driver, "arms", None)
    if allowed is not None and any(a not in allowed for a in arms):
        raise HarnessError(
            f"driver {driver.name!r} runs only the arms {', '.join(allowed)}"
            + (" (the native loop has no MCP client)" if driver.name == "native"
               else ""))
    python = python or sys.executable
    run_root = run_root or new_run_root()
    ordered_arms = [a for a in ARMS if a in arms]
    runs: list[dict[str, Any]] = []
    isolation = getattr(driver, "isolation", "worktree")
    if hasattr(driver, "python"):
        driver.python = python
    preflight = getattr(driver, "preflight", None)
    if callable(preflight):
        preflight()
    stopped: str | None = None
    for task in sorted(tasks, key=lambda t: t["id"]):
        for arm in ordered_arms:
            for repeat in range(1, repeats + 1):
                if stopped:
                    break
                record = run_one(repo, task, arm, repeat, driver, base,
                                 tasks_dir=tasks_dir, python=python,
                                 run_root=run_root, timeout_s=timeout_s,
                                 keep=keep, isolation=isolation)
                runs.append(record)
                if _AUTH_ERROR_RE.search(str(record.get("driver_error") or "")):
                    raise HarnessError(
                        f"driver cannot authenticate ({record['driver_error']}); "
                        f"stopped after {len(runs)} run(s). Logs: {run_root}")
                spent = sum(float(r.get("cost_usd") or 0) for r in runs)
                if max_total_usd is not None and spent > max_total_usd:
                    stopped = (f"cost cap reached: ${spent:.4f} > "
                               f"${max_total_usd:.2f} after {len(runs)} run(s)")
    describe = getattr(driver, "describe", None)
    backend = describe() if callable(describe) else {"backend": driver.name,
                                                     "model": None}
    return {"version": 1, "base": base, "driver": driver.name,
            "backend": backend,
            "total_cost_usd": round(sum(float(r.get("cost_usd") or 0)
                                        for r in runs), 6),
            "stopped": stopped,
            "arms": ordered_arms, "repeats": repeats,
            "tasks": [t["id"] for t in sorted(tasks, key=lambda t: t["id"])],
            "run_root": str(run_root),
            "runs": runs,
            "summary": aggregate(runs, ordered_arms),
            "per_task": paired(runs, ordered_arms)}


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def _mean(values: Iterable[Any], digits: int = 3) -> float | None:
    known = [float(v) for v in values if v is not None]
    return round(sum(known) / len(known), digits) if known else None


def aggregate(runs: list[dict[str, Any]], arms: Iterable[str]) -> list[dict[str, Any]]:
    rows = []
    for arm in arms:
        mine = [r for r in runs if r["arm"] == arm]
        n = len(mine)
        successes = sum(1 for r in mine if r.get("success") is True)
        rows.append({
            "arm": arm,
            "runs": n,
            "successes": successes,
            "success_rate": round(successes / n, 4) if n else None,
            "mean_wall_s": _mean(r.get("wall_s") for r in mine),
            "mean_prep_s": _mean((r.get("prep_s") for r in mine), 1),
            "prep_failures": sum(1 for r in mine if r.get("prep_ok") is False),
            "driver_errors": sum(1 for r in mine if r.get("driver_error")),
            "tokens_known_runs": sum(1 for r in mine if r.get("tokens_in") is not None),
            "mean_tokens_in": _mean((r.get("tokens_in") for r in mine), 1),
            "mean_tokens_out": _mean((r.get("tokens_out") for r in mine), 1),
            "mean_cost_usd": _mean((r.get("cost_usd") for r in mine), 4),
            "mean_tool_calls": _mean((r.get("tool_calls") for r in mine), 1),
            "mean_mcp_calls": _mean((r.get("mcp_calls") for r in mine), 1),
            "localization_rate": round(sum(1 for r in mine if r.get("localized")) / n, 4)
            if n else None,
        })
        if any("daemon_ok" in r for r in mine):
            rows[-1].update({
                "mean_captured_calls": _mean((r.get("captured_calls") for r in mine), 1),
                "mean_passthrough_calls": _mean((r.get("passthrough_calls")
                                                 for r in mine), 1),
                "total_fallbacks": sum(int(r.get("fallbacks") or 0) for r in mine),
                # Shell calls captured over shell calls answered, all runs.
                "shell_capture_rate": _rate(
                    sum(int(r.get("shell_captured") or 0) for r in mine),
                    sum(int(r.get("shell_captured") or 0)
                        + int(r.get("shell_passthrough") or 0) for r in mine)),
                # Only runs that started a daemon have a plugin to load.
                "plugin_load_failures": sum(1 for r in mine
                                            if r.get("daemon_ok") is not None
                                            and r.get("plugin_loaded") is not True),
                "daemon_failures": sum(1 for r in mine if r.get("daemon_ok") is False),
                "total_structured_queries": sum(int(r.get("structured_queries") or 0)
                                                for r in mine),
                "total_structured_outlines": sum(
                    int(r.get("structured_outlines") or 0) for r in mine),
                "total_jq_captures": sum(int(r.get("jq_captures") or 0)
                                         for r in mine),
                **{f"total_{k}": sum(int(r.get(k) or 0) for r in mine)
                   for k in READ_LEDGER_FIELDS},
                "total_patch_checks": sum(int(r.get("patch_checks") or 0)
                                          for r in mine),
                "total_patch_misses_caught": sum(
                    int(r.get("patch_misses_caught") or 0) for r in mine),
                "total_region_replies": sum(int(r.get("region_replies") or 0)
                                            for r in mine),
                "total_key_edits": sum(int(r.get("key_edits") or 0)
                                       for r in mine),
                "total_jobs_started": sum(int(r.get("jobs_started") or 0)
                                          for r in mine),
                "total_job_waits": sum(int(r.get("job_waits") or 0) for r in mine),
                "total_job_wait_seconds": round(sum(
                    float(r.get("job_wait_seconds") or 0) for r in mine), 1),
                "total_polls_avoided": sum(int(r.get("polls_avoided") or 0)
                                           for r in mine),
                **{f"total_{k}": sum(int(r.get(k) or 0) for r in mine)
                   for k in (*LINT_FIELDS, *TEST_SELECTION_FIELDS)},
                "codemode_tools": sorted({t for r in mine
                                          for t in (r.get("codemode_tools") or [])}),
            })
    return rows


def paired(runs: list[dict[str, Any]], arms: Iterable[str]) -> dict[str, Any]:
    """Per task, the success outcome of each repeat, for each arm."""
    arms = list(arms)
    out: dict[str, Any] = {}
    for tid in sorted({r["task"] for r in runs}):
        row: dict[str, Any] = {}
        for arm in arms:
            outcomes = [r.get("success") is True for r in sorted(
                (r for r in runs if r["task"] == tid and r["arm"] == arm),
                key=lambda r: r["repeat"])]
            row[arm] = {"outcomes": outcomes,
                        "successes": sum(outcomes),
                        "repeats": len(outcomes)}
        out[tid] = row
    return out


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Agent end-to-end evaluation",
        "",
        f"- Driver: `{report['driver']}`"
        + ((" (live backend; see docs/features/AGENT_E2E_EVAL.md"
            + (", untested live)" if report["driver"] == "claude" else ")"))
           if report["driver"] in LIVE_DRIVERS else ""),
        f"- Backend: `{(report.get('backend') or {}).get('backend', report['driver'])}`, "
        f"model `{(report.get('backend') or {}).get('model')}`",
        f"- Total reported cost: ${report.get('total_cost_usd', 0)}"
        + (f" ({report['stopped']})" if report.get("stopped") else ""),
        f"- Base: `{report['base']}`",
        f"- Arms: {', '.join(report['arms'])}",
        f"- Repeats per task and arm: {report['repeats']}",
        f"- Tasks: {len(report['tasks'])}",
        "",
        "## Per arm",
        "",
        "| arm | runs | success | success rate | mean wall s | mean prep s | "
        "prep failures | driver errors | mean tokens in | "
        "mean tokens out | mean cost USD | mean tool calls | mean MCP calls | "
        "localization rate |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["summary"]:
        lines.append("| " + " | ".join(_cell(v) for v in (
            row["arm"], row["runs"], row["successes"], row["success_rate"],
            row["mean_wall_s"], row["mean_prep_s"], row["prep_failures"],
            row.get("driver_errors", 0),
            row["mean_tokens_in"], row["mean_tokens_out"],
            row["mean_cost_usd"], row.get("mean_tool_calls"),
            row.get("mean_mcp_calls"), row["localization_rate"])) + " |")
    lines += ["", "Token, cost and tool columns are blank where the driver reports no "
              "value. MCP calls count calls to the ozagia MCP server. "
              "Prep (dev_memory snapshot and tag cache) runs before the driver timer; "
              "mean prep s is 0 for baseline."]
    graph_rows = [row for row in report["summary"] if "mean_captured_calls" in row]
    if graph_rows:
        lines += ["", "## Graph daemon and plugin", "",
                  "| arm | runs | mean captured calls | mean passthrough calls | "
                  "total fallbacks | plugin-load failures | daemon failures |",
                  "|---|---|---|---|---|---|---|"]
        for row in graph_rows:
            lines.append("| " + " | ".join(_cell(v) for v in (
                row["arm"], row["runs"], row["mean_captured_calls"],
                row.get("mean_passthrough_calls"),
                row["total_fallbacks"], row["plugin_load_failures"],
                row["daemon_failures"])) + " |")
        lines += ["", "Captured calls are tool calls the graph layer handled. "
                  "Passthrough calls are calls it left to the built-in by design "
                  "(not mapped). Fallbacks are calls where the graph layer failed: "
                  "for the daemon, the plugin's fallback reports (daemon error, "
                  "timeout, outcome unknown), one per call; for the native loop, "
                  "intent-layer "
                  "exceptions. A plugin-load failure is a run whose daemon "
                  "recorded no `hello` (or gave no stats)."]
        reachable = sorted({t for row in graph_rows
                            for t in (row.get("codemode_tools") or [])})
        if reachable:
            lines += ["", "Warning: replaced tools not registered `codemode: false` "
                      f"(Code Mode could call their built-ins): {', '.join(reachable)}."]
    lines += ["", "## Per task (paired)", ""]
    header = "| task | " + " | ".join(report["arms"]) + " |"
    lines += [header, "|---" * (len(report["arms"]) + 1) + "|"]
    for tid, row in report["per_task"].items():
        cells = [f"{row[a]['successes']}/{row[a]['repeats']}" for a in report["arms"]]
        lines.append(f"| {_cell(tid)} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _split(text: str | None) -> list[str] | None:
    if text is None:
        return None
    return [p.strip() for p in text.split(",") if p.strip()]


def _write(path: str | None, text: str) -> None:
    if path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")


def _cmd_validate(args: argparse.Namespace) -> int:
    spec = load_tasks(args.tasks)
    base = args.base or spec["base"]
    tasks = select_tasks(spec["tasks"], _split(args.task_ids))
    report = validate_all(args.repo, tasks, base, tasks_dir=spec["dir"],
                          python=args.python, run_root=_run_root(args),
                          keep=args.keep)
    for r in report["results"]:
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['id']}: pass_on_base={r['pass_on_base']} "
              f"fail_after_break={r['fail_after_break']} "
              + ("" if r["ok"] else f"({r['details'].get('reason')})"))
    _write(args.out_json, json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if report["ok"] else 1


def _run_root(args: argparse.Namespace) -> Path:
    return Path(args.run_root) if args.run_root else new_run_root()


def default_arms(driver_name: str) -> list[str]:
    """Every arm the driver can run: graph_intent only for its drivers."""
    allowed = DRIVER_ARMS.get(driver_name, ARMS)
    return [a for a in ARMS if a in allowed
            and (a != GRAPH_ARM or driver_name in GRAPH_ARM_DRIVERS)]


def _cmd_run(args: argparse.Namespace) -> int:
    spec = load_tasks(args.tasks)
    base = args.base or spec["base"]
    tasks = select_tasks(spec["tasks"], _split(args.task_ids))
    driver = make_driver(args.driver, live=args.live,
                         max_budget_usd=args.max_budget_usd, model=args.model)
    arms = _split(args.arms) or default_arms(driver.name)
    report = run_eval(args.repo, tasks, arms, driver, base,
                      tasks_dir=spec["dir"], repeats=args.repeats,
                      python=args.python, run_root=_run_root(args),
                      keep=args.keep, timeout_s=args.timeout_s,
                      max_total_usd=args.max_total_usd)
    _write(args.out_json, json.dumps(report, indent=2, sort_keys=True) + "\n")
    _write(args.out_md, render_markdown(report))
    for row in report["summary"]:
        print(f"{row['arm']}: {row['successes']}/{row['runs']} succeeded "
              f"(rate {row['success_rate']}), localized {row['localization_rate']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--tasks", default=str(DEFAULT_TASKS))
        sp.add_argument("--repo", default=str(ROOT), help="git repo to create worktrees from")
        sp.add_argument("--base", default=None, help="base commit (default: tasks file)")
        sp.add_argument("--task-ids", default=None, help="comma-separated subset")
        sp.add_argument("--python", default=None,
                        help="python for checks and the MCP server (default: this one)")
        sp.add_argument("--run-root", default=None,
                        help="scratch root (default: a new dir under /tmp/ozagia_eval)")
        sp.add_argument("--keep", action="store_true", help="keep worktrees")
        sp.add_argument("--out-json", default=None)

    v = sub.add_parser("validate", help="check pass-on-base and fail-after-break")
    common(v)
    v.set_defaults(func=_cmd_validate)

    r = sub.add_parser("run", help="run drivers over tasks and arms")
    common(r)
    r.add_argument("--driver", default=None,
                   choices=sorted(LOCAL_DRIVERS + LIVE_DRIVERS),
                   help=f"agent backend (default: ${DRIVER_ENV})")
    r.add_argument("--model", default=None,
                   help="backend model, e.g. vllm/qwen3.8-27b for opencode "
                        "(default: the backend's own configured default; for "
                        f"native: ${NATIVE_MODEL_ENV})")
    r.add_argument("--arms", default=None,
                   help=f"comma-separated subset of {','.join(ARMS)} (default: every "
                        f"arm the driver supports; {GRAPH_ARM} needs "
                        f"{'/'.join(GRAPH_ARM_DRIVERS)})")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--timeout-s", type=int, default=900,
                   help="wall-clock limit per driver session")
    r.add_argument("--live", action="store_true",
                   help="required, with OZAGIA_EVAL_LIVE=1, for claude, opencode "
                        "and native")
    r.add_argument("--max-budget-usd", type=float, default=None,
                   help="claude only: passed as --max-budget-usd (per session)")
    r.add_argument("--max-total-usd", type=float, default=None,
                   help="any backend: stop once reported session cost exceeds this")
    r.add_argument("--out-md", default=None)
    r.set_defaults(func=_cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except LiveRunNotApproved as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
