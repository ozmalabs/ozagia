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
  5. The check's result is classified (`ozagia.core.failures`). A run whose
     check failed as `harness` or `environment` is invalid only when the
     agent cannot have caused it (`check_validity`, recorded as
     `validity_basis`); invalid runs are left out of success counts and
     rates. The agent's own last pytest in the transcript is classified
     too, so "agent saw green, check saw red" shows in the report.

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

Checks run under `--python`, by default the interpreter
`ozagia.core.environments.env_for` picks for the check files in `--repo`
(docs/environments.md). `run` validates the selected tasks first and refuses
(exit 2) when one fails; `--validation <json>` reuses a matching `validate`
report instead, and `--skip-validate` runs without one.

Live drivers refuse to run unless both `--live` and the environment variable
OZAGIA_EVAL_LIVE=1 are set. A live run spends hosted-model tokens and
needs explicit user approval.

Usage:

    .venv/bin/python -m ozagia.eval.agent validate
    .venv/bin/python -m ozagia.eval.agent run --driver fake \\
        --arms baseline,dev_memory --out-json out.json --out-md out.md
    .venv/bin/python -m ozagia.eval.agent report --in out.json --out-md out.md

`report` recomputes each run's per-component timing (ozagia.eval.timing)
from the run JSON and its logs, so earlier runs can be re-reported.
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
from ozagia.core import failures as F
from ozagia.daemon.protocol import ENV_SOCK
from ozagia.eval import timing
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
    plugins = {opencode_plugin.PLUGIN_REL.as_posix(),
               opencode_plugin.TUI_BRAND_REL.as_posix(),
               opencode_plugin.TUI_PROFILE_REL.as_posix()}
    snap: dict[str, str | None] = {}
    for rel in sorted(p for p in proc.stdout.split("\0")
                      if p and p.split("/", 1)[0] != STATE_DIRNAME and p not in plugins):
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


def _first_party(root: Path) -> set[str]:
    """The project's top-level module names (`failures.first_party_names`
    with the import roots of its configuration)."""
    try:
        from ozagia.config import load_project_config
        roots = load_project_config(root).import_roots
    except Exception:  # noqa: BLE001 - the root alone then
        roots = ()
    return F.first_party_names(root, roots)


def failure_fields(result: dict[str, Any], prefix: str = "failure") -> dict[str, Any]:
    """The run-record fields of a classification: `<prefix>_class`,
    `<prefix>_evidence`, `<prefix>_detail` and, when known,
    `<prefix>_module`, `<prefix>_available_in` (interpreters that have it)
    and `<prefix>_declared_in` (manifests that declare it; [] for none)."""
    out = {f"{prefix}_class": result.get("class"),
           f"{prefix}_evidence": list(result.get("evidence") or []),
           f"{prefix}_detail": result.get("detail")}
    if result.get("module"):
        out[f"{prefix}_module"] = result["module"]
    if result.get("available_in"):
        out[f"{prefix}_available_in"] = list(result["available_in"])
    if result.get("declared_in") is not None:
        out[f"{prefix}_declared_in"] = list(result["declared_in"])
    if result.get("items"):
        out[f"{prefix}_items"] = list(result["items"])
    return out


# The daemon's captured head line in a tool output.
_OZAGIA_HEAD_RE = re.compile(r"^\[ozagia\] pytest \w+(?: \([^)]*\))?: exit (-?\d+)")
_PYTEST_SUMMARY_RE = re.compile(
    r"\d+ (?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b.* in [\d.]+s\b")
AGENT_SUMMARY_CHARS = 160


def last_agent_pytest(stdout: str) -> dict[str, Any] | None:
    """The agent's last completed shell call that ran pytest, from
    OpenCode's JSON events (`tool_use` with tool `shell` or `bash` and
    `pytest` in `input.command`): `{"command", "output", "returncode"}`.
    The exit code is the daemon's (`[ozagia] pytest ...: exit N`) when the
    call was captured (the head is in the block of `[ozagia]` lines that
    ends the output, `failures.daemon_head_index`; a head a test printed
    is followed by pytest's own output), else the tool's own
    `metadata.exit` for a command with no pipe (with one, the exit is the
    last command's), else None. None when no such call is found (other
    drivers' output included)."""
    last: dict[str, Any] | None = None
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{") or '"tool_use"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if event.get("type") != "tool_use" or part.get("tool") not in ("shell", "bash"):
            continue
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
        command = inp.get("command")
        if not isinstance(command, str) or "pytest" not in command:
            continue
        if state.get("status") not in (None, "completed", "error"):
            continue
        output = state.get("output") if isinstance(state.get("output"), str) else ""
        rc: int | None = None
        rows = output.splitlines()
        at = F.daemon_head_index(rows)
        head = _OZAGIA_HEAD_RE.match(rows[at].strip()) if at is not None else None
        if head:
            rc = int(head.group(1))
        elif "|" not in command:
            meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
            inner = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else meta
            exit_code = inner.get("exit")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                rc = exit_code
        last = {"command": command, "output": output, "returncode": rc}
    return last


def agent_test_fields(log_dir: Path, workdir: Path | None = None,
                      first_party: set[str] | None = None) -> dict[str, Any]:
    """`agent_test_class` and `agent_test_summary` for the agent's last
    pytest in `log_dir/driver.stdout.txt` (`last_agent_pytest`), both None
    when there is none. The summary is the daemon's head line or pytest's
    summary line (no paths), at most `AGENT_SUMMARY_CHARS`."""
    try:
        text = (log_dir / "driver.stdout.txt").read_text(encoding="utf-8",
                                                          errors="replace")
    except OSError:
        text = ""
    got = last_agent_pytest(text)
    if got is None:
        return {"agent_test_class": None, "agent_test_summary": None}
    res = F.classify(got["returncode"], False, got["output"], "",
                     command=got["command"], first_party=first_party)
    rows = [x.strip() for x in got["output"].splitlines()]
    at = F.daemon_head_index(rows)
    summary = (rows[at][len("[ozagia] "):] if at is not None
               and _OZAGIA_HEAD_RE.match(rows[at]) else None)
    if summary is None:
        summary = next((x.strip("= ") for x in reversed(rows)
                        if _PYTEST_SUMMARY_RE.search(x)), None)
    if summary is None:
        summary = res.get("detail")
    summary = str(summary or "")
    if len(summary) > AGENT_SUMMARY_CHARS:
        summary = summary[:AGENT_SUMMARY_CHARS - 3] + "..."
    return {"agent_test_class": res["class"], "agent_test_summary": summary,
            "agent_test_evidence": list(res.get("evidence") or [])}


IMPORT_PROBE_TIMEOUT_S = 60


def import_probe(python: str, module: str) -> bool | None:
    """Whether `python` imports the top-level package of `module` when run
    isolated (`-I`: no current directory, user site or `PYTHON*`
    variables on the path) from an empty temporary directory outside any
    worktree; None when the probe cannot run (a missing interpreter, a
    timeout) or the name is not an identifier."""
    top = str(module).split(".")[0]
    if not top.isidentifier():
        return None
    tmp = tempfile.mkdtemp(prefix="ozagia_probe_")
    try:
        proc = subprocess.run([python, "-I", "-c", f"import {top}"], cwd=tmp,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=IMPORT_PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return proc.returncode == 0


def _inside(path: str, root: Path | None) -> bool:
    if root is None:
        return False
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
    except ValueError:
        return False
    return True


BASE_GREP_TIMEOUT_S = 30
# (repo realpath, base, top-level name) -> whether a tracked file imports it.
_BASE_IMPORTS: dict[tuple[str, str, str], bool | None] = {}
BASE_IMPORTS_MAX = 512


def _import_patterns(top: str) -> list[str]:
    """POSIX extended regexes for a line that imports ``top``."""
    end = "([^[:alnum:]_]|$)"
    return [
        rf"^[[:space:]]*from[[:space:]]+{top}([[:space:].]|$)",
        rf"^[[:space:]]*import[[:space:]]+(.*[^[:alnum:]_.])?{top}{end}",
        rf"(import_module|__import__|importorskip)\([[:space:]]*[\"']{top}[.\"']",
    ]


def _read_patch(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def patch_adds_import(patch: str | None, module: str) -> bool:
    """Whether a unified diff adds a line that imports the top-level name
    of ``module`` (the `base_imports` patterns, on `+` lines)."""
    top = str(module).split(".")[0]
    if not patch or not top.isidentifier():
        return False
    pats = [re.compile(rf"^\s*from\s+{top}([\s.]|$)"),
            re.compile(rf"^\s*import\s+(.*[^0-9A-Za-z_.])?{top}([^0-9A-Za-z_]|$)"),
            re.compile(rf"(import_module|__import__|importorskip)\(\s*[\"']{top}[.\"']")]
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            text = line[1:]
            if any(p.search(text) for p in pats):
                return True
    return False


def base_imports(repo: str | Path, base: str, module: str) -> bool | None:
    """Whether any tracked Python file at commit ``base`` of ``repo``
    imports the top-level name of ``module``: an `import` or `from` line,
    or `import_module`, `__import__` or `pytest.importorskip` with the
    name as a string (`git grep` at the commit, bounded, cached per
    repo, base and name). None when git cannot answer or the name is not
    an identifier. Matches inside strings and comments count, so it errs
    toward True (the import probe then decides)."""
    top = str(module).split(".")[0]
    if not top.isidentifier():
        return None
    key = (os.path.realpath(repo), str(base), top)
    if key in _BASE_IMPORTS:
        return _BASE_IMPORTS[key]
    argv = ["git", "-C", str(repo), "grep", "-q", "-I", "-E"]
    for pat in _import_patterns(top):
        argv += ["-e", pat]
    argv += [str(base), "--", "*.py", "*.pyi"]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=BASE_GREP_TIMEOUT_S, check=False)
        got: bool | None = {0: True, 1: False}.get(proc.returncode)
    except (OSError, subprocess.SubprocessError):
        got = None
    if len(_BASE_IMPORTS) >= BASE_IMPORTS_MAX:
        _BASE_IMPORTS.pop(next(iter(_BASE_IMPORTS)))
    _BASE_IMPORTS[key] = got
    return got


def check_validity(failure: dict[str, Any], *, base_passed: bool = False,
                   python: str | None = None, workdir: Path | None = None,
                   repo: str | Path | None = None,
                   base: str | None = None,
                   break_patch: str | None = None) -> dict[str, Any]:
    """Whether a run is valid, as `{"valid", "invalid_reason",
    "validity_basis"}`. Only a `harness` or `environment` check can make a
    run invalid, and only when the agent cannot have caused it:

    - `base_passed` (validation ran with this interpreter and the check
      passed on base): an environment failure after the agent is the
      agent's (a new import of an uninstalled package, `cannot import name
      X from numpy`), and so is a harness failure, unless the interpreter
      itself is gone and lies outside the worktree. The run counts as a
      failure, with its class shown.
    - No validation (`--skip-validate`, or a report recorded before
      validation): for `environment` with a `module`, when ``repo`` and
      ``base`` are given, no tracked file at ``base`` imports the module's
      top-level name (`base_imports`) and the task's ``break_patch`` (its
      text, when known) adds no such import (`patch_adds_import`), the
      agent introduced the import: a failure. Otherwise `import_probe(python, module)` decides:
      importable means the agent broke it (a failure), not importable
      means the environment lacks it (invalid). Without a module to probe,
      or for `harness`, invalid.

    A passing check is always valid."""
    cls = failure.get("class")
    detail = failure.get("detail")
    if cls not in F.NOT_EVIDENCE:
        return {"valid": True, "invalid_reason": None,
                "validity_basis": f"check class {cls}"}
    invalid = f"check {cls}: {detail}"
    if cls == "harness" and python and not os.path.exists(python) \
            and not _inside(python, workdir):
        return {"valid": False, "invalid_reason": invalid,
                "validity_basis": f"the check interpreter {python} is missing "
                                  "and lies outside the worktree"}
    if base_passed:
        return {"valid": True, "invalid_reason": None,
                "validity_basis": f"check {cls} after the base check passed with "
                                  "this interpreter: counted against the agent"}
    module = failure.get("module")
    if cls == "environment" and module and python:
        top = str(module).split(".")[0]
        if repo is not None and base and base_imports(repo, base, top) is False \
                and not patch_adds_import(break_patch, top):
            return {"valid": True, "invalid_reason": None,
                    "validity_basis": f"no validation; no tracked file at base "
                                      f"{str(base)[:12]} imports {top}"
                                      + (" and the break patch adds no such import"
                                         if break_patch is not None else "")
                                      + ": the agent introduced it, counted "
                                        "against the agent"}
        got = import_probe(python, module)
        if got is True:
            return {"valid": True, "invalid_reason": None,
                    "validity_basis": f"no validation, import probe: {top} imports "
                                      f"under {python} outside the worktree: "
                                      "counted against the agent"}
        why = ("does not import" if got is False else "could not be probed")
        return {"valid": False, "invalid_reason": invalid,
                "validity_basis": f"no validation, import probe: {top} {why} under {python} "
                                  "outside the worktree"}
    return {"valid": False, "invalid_reason": invalid,
            "validity_basis": f"no validation; check {cls} with nothing to probe"}


def run_check(workdir: Path, check: dict[str, Any], python: str,
              log_path: Path) -> dict[str, Any]:
    """Run the task check with `python` and classify its result
    (`ozagia.core.failures`): `failure` holds the class, evidence and
    detail. A runner that cannot start (a missing interpreter or workdir)
    is a `harness` failure, not an exception."""
    argv = [a.replace("{workdir}", str(workdir)) for a in check["argv"]]
    cmd = [python, *argv]
    timeout = int(check["timeout_s"])
    # No .pyc files: a break that keeps the byte length (a + b -> a - b) within the
    # same mtime second would otherwise run stale bytecode from the base check.
    env = dict(os.environ, COLUMNS="250", PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    try:
        res = spawn_group(cmd, cwd=workdir, env=env, timeout_s=timeout)
    except OSError as e:
        res = {"returncode": None, "timed_out": False, "stdout": "",
               "stderr": f"{type(e).__name__}: [Errno {e.errno}] {e.strerror}: "
                         f"'{e.filename}'",
               "seconds": round(time.monotonic() - started, 3)}
    out, err = res["stdout"], res["stderr"]
    returncode, timed_out = res["returncode"], res["timed_out"]
    seconds = res["seconds"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(f"$ {' '.join(cmd)}\n\n{out}\n--- stderr ---\n{err}\n",
                        encoding="utf-8")
    failure = F.classify(returncode, timed_out, out, err, command=cmd,
                         first_party=_first_party(workdir) if workdir.is_dir() else None,
                         interpreter=python, root=workdir)
    return {"passed": (not timed_out) and returncode == 0,
            "returncode": returncode, "timed_out": timed_out,
            "seconds": seconds, "log": str(log_path),
            "tail": _tail(out + "\n" + err, 12), "failure": failure}


# --------------------------------------------------------------------------- #
# the check interpreter
# --------------------------------------------------------------------------- #
WORKDIR_TOKEN = "{workdir}"


def check_files(task: dict[str, Any]) -> list[str]:
    """Repo-relative Python files a task's check names (`tests/x.py`,
    `tests/x.py::test_y`, `{workdir}/tests/x.py`)."""
    out: list[str] = []
    for arg in task["check"]["argv"]:
        if arg.startswith("-"):
            continue
        path = arg.split("::", 1)[0]
        if path.startswith(WORKDIR_TOKEN):
            path = path[len(WORKDIR_TOKEN):].lstrip("/")
        if path.endswith(".py") and not os.path.isabs(path) and path not in out:
            out.append(path)
    return out


def check_modules(task: dict[str, Any]) -> list[str]:
    """Modules a check runs with `-m` (`pytest`)."""
    argv = task["check"]["argv"]
    return [argv[n + 1] for n, arg in enumerate(argv[:-1]) if arg == "-m"]


def default_check_python(repo: str | Path, tasks: list[dict[str, Any]],
                         **env_kwargs: Any) -> dict[str, Any]:
    """The interpreter for the checks of ``tasks``: the best of
    ``environments.env_for`` over their test files in the source repo (and
    the modules they run with `-m`), else this interpreter. Returns
    {"python", "reason", "missing"}. Project-shipped interpreters are run
    to probe them (``trust=True``): the harness runs the repository's
    tests under the chosen one anyway."""
    from ozagia.core import environments as EN
    files = list(dict.fromkeys(f for t in tasks for f in check_files(t)))
    modules = list(dict.fromkeys(m for t in tasks for m in check_modules(t)))
    env_kwargs.setdefault("trust", True)
    # The source repo is read, not written: no probe or import cache under
    # its state dir.
    env_kwargs.setdefault("cache_mode", "read")
    try:
        doc = EN.env_for(repo, files, extra_modules=modules, **env_kwargs)
    except (EN.EnvError, OSError) as exc:
        return {"python": sys.executable, "missing": None,
                "reason": f"environments failed ({exc}); this interpreter"}
    needs = ", ".join(doc["required"]) or "no third-party module"
    best = doc["best"]
    if best is not None:
        return {"python": best["path"], "missing": [],
                "reason": f"env_for: {best['source']} ({best['evidence']}) "
                          f"imports every module the checks need ({needs})"}
    usable = [e for e in doc["interpreters"] if e.get("ok")]
    if usable:
        top = usable[0]
        return {"python": top["path"], "missing": top["missing"],
                "reason": f"env_for: no found interpreter imports every module "
                          f"the checks need ({needs}); {top['source']} "
                          f"({top['evidence']}) misses the fewest: "
                          + ", ".join(top["missing"])}
    return {"python": sys.executable, "missing": None,
            "reason": "env_for: no interpreter could be probed; this interpreter"}


NOT_VALIDATED_REASON = ("not validated (--skip-validate, or run_eval called "
                        "without a validation report): invalid runs are decided "
                        "by the import probe")


def validation_summary(report: dict[str, Any] | None, *,
                       source: str | None = None) -> dict[str, Any]:
    """What a run report says about its validation: {"ran": false} without
    one, else whether every task passed, the counts, the failed ids with
    their reasons, the interpreter the checks ran under (and why), and
    `source` ("preflight", or the path of a reused `validate` report)."""
    if report is None:
        return {"ran": False, "reason": NOT_VALIDATED_REASON}
    results = report.get("results") or []
    failed = [{"id": r.get("id"), "reason": (r.get("details") or {}).get("reason")}
              for r in results if not r.get("ok")]
    return {"ran": True, "ok": bool(report.get("ok")), "tasks": len(results),
            "passed": len(results) - len(failed), "failed": failed,
            "base": report.get("base"), "base_commit": report.get("base_commit"),
            "source": source or "preflight",
            "check_python": validation_python(report),
            "check_python_reason": report.get("check_python_reason")}


def validation_python(report: dict[str, Any]) -> str | None:
    """The interpreter a `validate_all` report ran its checks under
    (`check_python`; `python` in reports written before W23)."""
    got = report.get("check_python") or report.get("python")
    return got if isinstance(got, str) else None


def task_fingerprint(task: dict[str, Any], tasks_dir: str | Path) -> str:
    """A digest of what validation tested for a task: its check and the
    text of its break patch."""
    try:
        patch = (Path(tasks_dir) / task["break_patch"]).read_bytes()
    except (OSError, KeyError, TypeError):
        patch = b""
    h = hashlib.sha256(json.dumps(task.get("check"), sort_keys=True).encode())
    h.update(b"\0")
    h.update(patch)
    return h.hexdigest()[:16]


def _same_interpreter(a: str, b: str) -> bool:
    # abspath, not realpath: a venv's bin/python is a symlink to its base
    # interpreter, and the two import different packages.
    return os.path.abspath(a) == os.path.abspath(b)


def resolve_base(repo: str | Path, base: str) -> str:
    """The commit id `base` (a ref, `HEAD`, a short or full id) names in
    `repo` now; HarnessError when it names none."""
    proc = _git(["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], repo,
                check=False)
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise HarnessError(f"base {base!r} is not a commit in {repo}")
    return sha


# Whether each module is found, without running it: one bounded
# subprocess, isolated, from an empty directory outside any worktree.
_FIND_SPECS = (
    "import importlib.util as u, json, sys\n"
    "out = {}\n"
    "for m in sys.argv[1:]:\n"
    "    try:\n"
    "        out[m] = u.find_spec(m) is not None\n"
    "    except Exception:\n"
    "        out[m] = False\n"
    "print(json.dumps(out))\n")


def modules_found(python: str, modules: Iterable[str]) -> dict[str, bool] | None:
    """{module: whether `python -I` finds it (`importlib.util.find_spec`, so
    no module code runs)} for top-level identifiers, from an empty
    temporary directory; None when the interpreter cannot be run."""
    names = sorted({str(m).split(".")[0] for m in modules
                    if str(m).split(".")[0].isidentifier()})
    if not names:
        return {}
    tmp = tempfile.mkdtemp(prefix="ozagia_probe_")
    try:
        proc = subprocess.run([python, "-I", "-c", _FIND_SPECS, *names], cwd=tmp,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=IMPORT_PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        got = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return {k: bool(v) for k, v in got.items()} if isinstance(got, dict) else None


def check_requirements(repo: str | Path, tasks: list[dict[str, Any]],
                       python: str) -> list[str]:
    """The modules the checks of ``tasks`` need (`env_for` over their test
    files, with the `-m` modules), judged for ``python`` alone; only the
    `-m` modules when the import model cannot be read."""
    from ozagia.core import environments as EN
    files = list(dict.fromkeys(f for t in tasks for f in check_files(t)))
    modules = list(dict.fromkeys(m for t in tasks for m in check_modules(t)))
    cand = [{"path": python, "source": "check", "evidence": "the check interpreter",
             "also": []}]
    try:
        doc = EN.env_for(repo, files, extra_modules=modules, cache_mode="read",
                         candidates=cand, trust=True)
    except (EN.EnvError, OSError):
        return sorted({m.split(".")[0] for m in modules})
    return list(doc["required"])


def interpreter_state(repo: str | Path, tasks: list[dict[str, Any]],
                      python: str) -> dict[str, Any]:
    """What validation's verdict depends on in the check interpreter:
    `signature` (`environments.interpreter_signature`: realpath, the
    binary's and `pyvenv.cfg`'s mtimes, the scanned site-packages
    directories' mtimes) and `modules` (`modules_found` for
    `check_requirements`)."""
    from ozagia.core import environments as EN
    return {"signature": EN.interpreter_signature(python),
            "modules": modules_found(python, check_requirements(repo, tasks, python))}


def state_mismatch(state: Any, python: str) -> str | None:
    """Why the check interpreter is no longer what a validation recorded
    (`interpreter_state`), or None. Only stats and one `find_spec`
    subprocess: the recorded directories are re-statted, not re-probed."""
    from ozagia.core import environments as EN
    if not isinstance(state, dict) or not isinstance(state.get("signature"), dict):
        return ("the report records no check interpreter state (made before "
                "interpreter states were recorded)")
    sig = state["signature"]
    dirs = sig.get("dirs") if isinstance(sig.get("dirs"), dict) else {}
    now = EN.interpreter_signature(python, list(dirs))
    if now != sig:
        names = [label for k, label in (("real", "its resolved binary"),
                                        ("mtime_ns", "its binary's mtime"),
                                        ("cfg", "its pyvenv.cfg"))
                 if now.get(k) != sig.get(k)]
        names += [f"{d} (packages installed or removed)" for d in sorted(dirs)
                  if now["dirs"].get(d) != dirs.get(d)]
        return ("the check interpreter changed since validation: "
                + "; ".join(names))
    recorded = state.get("modules")
    if isinstance(recorded, dict) and recorded:
        want = [m for m, ok in recorded.items() if ok is True]
        got = modules_found(python, want) if want else {}
        if got is None:
            return "the check interpreter cannot be run to check its modules"
        gone = sorted(m for m in want if got.get(m) is not True)
        if gone:
            return ("module(s) the checks need no longer found under the check "
                    "interpreter: " + ", ".join(gone))
    return None


def validation_mismatch(report: Any, *, base: str, python: str,
                        tasks: list[dict[str, Any]],
                        tasks_dir: str | Path,
                        base_commit: str | None = None) -> str | None:
    """Why a `validate_all` report cannot stand for a run with `base`,
    `python` and `tasks`, or None when it can: the same base commit (the
    report's `base_commit` against ``base_commit``, the run's base resolved
    now; without it ``base`` is taken as a commit id), the same check
    interpreter in the same state (`state_mismatch`), and every task
    present with the same check and break patch (`task_fingerprint`, when
    the report has one)."""
    if not isinstance(report, dict) or not isinstance(report.get("results"), list):
        return "not a `validate --out-json` report"
    want = base_commit or base
    got = report.get("base_commit") or report.get("base")
    if got != want:
        shown = (f"base {report.get('base')!r} at {got}" if report.get("base_commit")
                 else f"base {report.get('base')!r} (no commit id recorded)")
        return f"{shown}, this run {base!r} at {want}"
    vp = validation_python(report)
    if vp is None or not _same_interpreter(vp, python):
        return f"check interpreter {vp!r}, this run {python!r}"
    by_id = {r.get("id"): r for r in report["results"] if isinstance(r, dict)}
    missing = [t["id"] for t in tasks if t["id"] not in by_id]
    if missing:
        return "task(s) not in the report: " + ", ".join(sorted(missing))
    changed = [t["id"] for t in tasks
               if by_id[t["id"]].get("fingerprint") is not None
               and by_id[t["id"]]["fingerprint"] != task_fingerprint(t, tasks_dir)]
    if changed:
        return "check or break patch changed since validation: " + ", ".join(sorted(changed))
    return state_mismatch(report.get("check_python_state"), python)


def restrict_validation(report: dict[str, Any], task_ids: Iterable[str]) -> dict[str, Any]:
    """`report` with only the results of `task_ids`, `ok` recomputed."""
    keep = set(task_ids)
    results = [r for r in report.get("results") or []
               if isinstance(r, dict) and r.get("id") in keep]
    return {**report, "results": results, "ok": all(r.get("ok") for r in results)}


def validated_tasks(validation: dict[str, Any] | None) -> set[str]:
    """The task ids whose check passed on base in a `validate_all` report
    (already matched to the run, `validation_mismatch`); empty without
    one."""
    if not isinstance(validation, dict):
        return set()
    return {r["id"] for r in validation.get("results") or []
            if isinstance(r, dict) and r.get("pass_on_base") is True
            and isinstance(r.get("id"), str)}


def resolve_check_python(repo: str | Path, tasks: list[dict[str, Any]],
                         python: str | None) -> tuple[str, str]:
    """(interpreter, why): an explicit `python` wins."""
    if python:
        return python, "explicit --python"
    chosen = default_check_python(repo, tasks)
    return chosen["python"], chosen["reason"]


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
            "base_check": {**{k: base_check[k] for k in ("returncode", "seconds", "log")},
                           "failure_class": base_check["failure"]["class"],
                           "failure_detail": base_check["failure"]["detail"]},
            "break_apply": applied,
        }
        if broken_check is not None:
            details["broken_check"] = {
                **{k: broken_check[k] for k in ("returncode", "seconds", "log")},
                "failure_class": broken_check["failure"]["class"],
                "failure_detail": broken_check["failure"]["detail"]}
        if not ok:
            details["reason"] = _validation_reason(pass_on_base, applied["ok"],
                                                   fail_after_break)
        result = {"id": tid, "ok": ok, "pass_on_base": pass_on_base,
                  "fail_after_break": fail_after_break,
                  "fingerprint": task_fingerprint(task, tasks_dir),
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
                 python_reason: str | None = None,
                 run_root: Path | None = None, keep: bool = False) -> dict[str, Any]:
    """Validate every task. Without ``python`` the check interpreter is
    ``default_check_python``'s choice. ``base`` is resolved to a commit id
    first (`base_commit`), and every task is validated at that commit. The
    report records the check interpreter's state (`check_python_state`,
    ``interpreter_state``), so a reused report is refused once the
    interpreter changed (``validation_mismatch``)."""
    if python is None:
        python, python_reason = resolve_check_python(repo, tasks, None)
    base_commit = resolve_base(repo, base)
    run_root = run_root or new_run_root()
    results = [validate_task(repo, t, base_commit, tasks_dir=tasks_dir, python=python,
                             run_root=run_root, keep=keep) for t in tasks]
    state = interpreter_state(repo, tasks, python)
    return {"version": 1, "base": base, "base_commit": base_commit,
            "run_root": str(run_root),
            "check_python": python,
            "check_python_reason": python_reason or "explicit --python",
            "check_python_state": state,
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


# The repeat cost of the index: what a later session on an indexed checkout
# does instead of a build (as the daemon's `after_refresh` does): load the
# snapshot, reingest the files changed since it (none here: the tree is the
# one prep just indexed) and bring the tag cache up to date. Each step is
# timed in the subprocess, so interpreter start-up is not counted.
_REPEAT_CODE = (
    "import json, sys, time\n"
    "from pathlib import Path\n"
    "from ozagia.core.memory import DevMemory\n"
    "from ozagia.core.repo_map import update_tag_cache\n"
    "work, dump = Path(sys.argv[1]), Path(sys.argv[2])\n"
    "t0 = time.monotonic()\n"
    "mem = DevMemory(dump, dump.parent / 'journal.jsonl', root=work)\n"
    "t1 = time.monotonic()\n"
    "pending = mem.pending_changes()\n"
    "mem.reingest(pending)\n"
    "t2 = time.monotonic()\n"
    "update_tag_cache(work, cache_path=work / '.ozagia' / 'tags.json')\n"
    "t3 = time.monotonic()\n"
    "print(json.dumps({'load_s': round(t1 - t0, 3), 'refresh_s': round(t2 - t1, 3),\n"
    "                  'tags_s': round(t3 - t2, 3), 'pending': len(pending)}))\n"
)


def measure_repeat_index(workdir: Path, tool_python: str, log_dir: Path, *,
                         config: Path | None = None) -> dict[str, Any]:
    """The repeat index cost on the tree prep just indexed, outside every timer.

    Runs `_REPEAT_CODE` on a copy of the run's snapshot (`dev_graph.json`),
    in `<log_dir>/repeat_index/`, which is removed afterwards, so the
    snapshot the daemon loads is not touched. The tag cache is the run's own
    (unchanged tree: nothing to rewrite). Returns `prep_repeat_s` (load +
    refresh + tag cache), `prep_repeat_parts`, `prep_repeat_ok` and
    `prep_repeat_error`; the output goes to `prep_repeat.log`."""
    out: dict[str, Any] = {"prep_repeat_s": None, "prep_repeat_parts": None,
                           "prep_repeat_ok": False, "prep_repeat_error": None}
    scratch = log_dir / "repeat_index"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        dump = scratch / "dev_graph.json"
        shutil.copyfile(log_dir / "dev_graph.json", dump)
    except OSError as exc:
        shutil.rmtree(scratch, ignore_errors=True)
        out["prep_repeat_error"] = f"cannot copy the snapshot: {exc}"
        return out
    try:
        env = _tool_env(workdir, config, tool_python=tool_python)
        res = spawn_group([tool_python, "-c", _REPEAT_CODE, str(workdir), str(dump)],
                          cwd=workdir, env=env, timeout_s=PREP_TIMEOUT_S)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    (log_dir / "prep_repeat.log").write_text(
        f"{res['stdout']}\n--- stderr ---\n{res['stderr']}\n", encoding="utf-8")
    parts = None
    if res["returncode"] == 0 and not res["timed_out"]:
        for line in reversed(res["stdout"].splitlines()):
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict) and "load_s" in doc:
                parts = doc
                break
    if parts is None:
        out["prep_repeat_error"] = (
            f"timed out after {PREP_TIMEOUT_S} s" if res["timed_out"]
            else _tail(res["stderr"], 12) or f"exit {res['returncode']}, no timings")
        return out
    out.update(prep_repeat_ok=True, prep_repeat_parts=parts,
               prep_repeat_s=round(sum(float(parts[k]) for k in
                                       ("load_s", "refresh_s", "tags_s")), 3))
    return out


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
# Working sets handed back after compactions (`ozagia.core.working_set`).
WORKING_SET_FIELDS = ("working_sets_served", "working_set_chars")
# Multi-file replace (`ozagia.intent.replace`): previews, applies, files written.
REPLACE_FIELDS = ("replace_previews", "replace_applies", "replace_files")


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
    inside the edit), affected_runs, tests_skipped_unaffected,
    cached_passes and full_runs_forced (`TEST_SELECTION_FIELDS`, test
    selection, `ozagia.intent.affected`), path_suggestions and
    ownership_warnings (missing paths answered with candidates,
    `ozagia.intent.paths`; warnings about another live session's
    uncommitted edits, `ozagia.core.owners`), ops_nodes and
    ops_after_annotations (the operations graph, `ozagia.core.ops_graph`,
    and `[ozagia] ops:` lines after log/status commands), and working_sets_served and
    working_set_chars (`WORKING_SET_FIELDS`, working sets handed back after
    compactions, `ozagia.core.working_set`), and replace_previews,
    replace_applies and replace_files (`REPLACE_FIELDS`, multi-file
    replace, `ozagia.intent.replace`)."""
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
                "path_suggestions": None, "ownership_warnings": None,
                "ops_nodes": None, "ops_after_annotations": None,
                **{k: None for k in LINT_FIELDS},
                **{k: None for k in TEST_SELECTION_FIELDS},
                **{k: None for k in WORKING_SET_FIELDS},
                **{k: None for k in REPLACE_FIELDS}}
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
            "path_suggestions": _count(stats.get("path_suggestions")),
            "ownership_warnings": _count(stats.get("ownership_warnings")),
            "ops_nodes": _count(stats.get("ops_nodes")),
            "ops_after_annotations": _count(stats.get("ops_after_annotations")),
            **{k: _count(stats.get(k)) for k in LINT_FIELDS},
            **{k: _count(stats.get(k)) for k in TEST_SELECTION_FIELDS},
            **{k: _count(stats.get(k)) for k in WORKING_SET_FIELDS},
            **{k: _count(stats.get(k)) for k in REPLACE_FIELDS}}



def run_one(repo: str | Path, task: dict[str, Any], arm: str, repeat: int,
            driver: Any, base: str, *, tasks_dir: str | Path, python: str,
            run_root: Path, timeout_s: int, keep: bool,
            isolation: str = "worktree",
            tool_python: str | None = None,
            base_passed: bool = False,
            repeat_index: bool = False) -> dict[str, Any]:
    """One task × arm × repeat. `base_passed`: validation ran with `python`
    and the task's check passed on base (`check_validity`). `repeat_index`
    measures the repeat index cost (`measure_repeat_index`,
    `prep_repeat_s`) right after prep; run_eval asks for it once per run
    root."""
    tool_python = tool_python or sys.executable
    config = config_path(repo)
    tid = task["id"]
    name = f"{tid}-{arm}-r{repeat}"
    log_dir = run_root / "logs" / name
    setup_started = time.monotonic()
    workdir = make_workdir(repo, base, run_root, name, isolation=isolation)
    cleanup: list[str] = []
    record: dict[str, Any] = {"task": tid, "arm": arm, "repeat": repeat,
                              "driver": driver.name, "prep_s": 0.0,
                              "prep_ok": None, "prep_error": None,
                              "setup_workdir_s": None, "plugin_install_s": None,
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
        # Workdir, break patch and arm config: setup, outside every timer.
        record["setup_workdir_s"] = round(time.monotonic() - setup_started, 3)
        if arm in ("dev_memory", GRAPH_ARM):
            prep = prepare_dev_memory(workdir, tool_python, log_dir, config=config)
            record.update(prep_s=prep["prep_s"], prep_ok=prep["prep_ok"],
                          prep_error=prep["prep_error"])
            if repeat_index and prep["prep_ok"]:
                # What a later session on this revision pays instead of a
                # build: snapshot load + refresh + tag cache (not a rebuild).
                record.update(measure_repeat_index(workdir, tool_python, log_dir,
                                                   config=config))
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
            install_started = time.monotonic()
            opencode_plugin.install(workdir)
            record["plugin_install_s"] = round(time.monotonic() - install_started, 3)
            plugin_installed = True
            run_kwargs["extra_env"] = {ENV_SOCK: str(started["sock"])}
        elif graph and config is not None:
            # The loop's graph must use the layout prep built it with.
            run_kwargs["extra_env"] = {"OZAGIA_CONFIG": str(config)}
        before = _snapshot(workdir)
        # System wall clock, as the driver's event timestamps are: splits
        # the time before the first event from the time after the last.
        record["driver_started_at"] = round(time.time(), 3)
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
            **check_validity(check["failure"], base_passed=base_passed,
                             python=python, workdir=workdir, repo=repo, base=base,
                             break_patch=_read_patch(patch)),
            **failure_fields(check["failure"]),
            **agent_test_fields(log_dir, workdir, _first_party(workdir)),
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
        record["timing"] = timing.run_timing(record, log_dir)


def check_arms(arms: list[str], driver: Any) -> None:
    """Refuse arms that are unknown or that the driver cannot run."""
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise HarnessError(f"unknown arms: {', '.join(unknown)}")
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


def run_eval(repo: str | Path, tasks: list[dict[str, Any]], arms: Iterable[str],
             driver: Any, base: str, *, tasks_dir: str | Path = DEFAULT_TASKS.parent,
             repeats: int = 1, python: str | None = None,
             python_reason: str | None = None, run_root: Path | None = None,
             keep: bool = False, timeout_s: int = 900,
             max_total_usd: float | None = None,
             validation: dict[str, Any] | None = None,
             validation_source: str | None = None,
             repeat_index: bool = True,
             amortize: int | None = None) -> dict[str, Any]:
    """Run every task × arm × repeat. `max_total_usd` stops the run once the
    reported cost of finished sessions exceeds it (a soft cap: the session
    that crosses it still completes). Without `python` the checks run under
    `default_check_python`'s choice; each run records the interpreter and
    why (`check_python`, `check_python_reason`).

    `validation` is the `validate_all` report for these tasks (the `run`
    preflight's, or a reused one; `validation_source` says which), None when
    validation did not run. It must match `base`, `python` and the tasks
    (`validation_mismatch`, else HarnessError): `base` is resolved to a
    commit id (`base_commit`) once, and every run starts from that commit,
    the one the validation must have used. Its tasks whose check passed
    on base are `validated_tasks`: for them an environment or harness check
    after the agent counts against the agent (`check_validity`). Without
    one, the import probe decides. The report records `validated`,
    `validation` (``validation_summary``) and `validated_tasks`.

    `repeat_index` measures the repeat index cost once, in the first run
    that preps (see run_one). `amortize` is the N of the report's amortized
    view (default: the number of tasks run)."""
    arms = list(arms)
    check_arms(arms, driver)
    if repeats < 1:
        raise HarnessError("repeats must be at least 1")
    if amortize is not None and amortize < 1:
        raise HarnessError("--amortize must be at least 1")
    if python is None:
        python, python_reason = resolve_check_python(repo, tasks, None)
    python_reason = python_reason or "explicit --python"
    base_commit = resolve_base(repo, base)
    run_root = run_root or new_run_root()
    if validation is not None:
        why = validation_mismatch(validation, base=base, python=python,
                                  tasks=tasks, tasks_dir=tasks_dir,
                                  base_commit=base_commit)
        if why:
            raise HarnessError(f"the validation report does not match this run: {why}")
        validation = restrict_validation(validation, (t["id"] for t in tasks))
    validated = validated_tasks(validation)
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
                want_repeat = (repeat_index and arm in timing.INDEX_ARMS
                               and not any(r.get("prep_repeat_ok") is not None
                                           for r in runs))
                record = run_one(repo, task, arm, repeat, driver, base_commit,
                                 tasks_dir=tasks_dir, python=python,
                                 run_root=run_root, timeout_s=timeout_s,
                                 keep=keep, isolation=isolation,
                                 base_passed=task["id"] in validated,
                                 repeat_index=want_repeat)
                record.update(check_python=python,
                              check_python_reason=python_reason)
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
    n = amortize_n(amortize, runs)
    return {"version": 1, "base": base, "base_commit": base_commit,
            "driver": driver.name,
            "backend": backend,
            "total_cost_usd": round(sum(float(r.get("cost_usd") or 0)
                                        for r in runs), 6),
            "stopped": stopped,
            "check_python": python, "check_python_reason": python_reason,
            "validated": validation is not None,
            "validation": validation_summary(validation, source=validation_source),
            "arms": ordered_arms, "repeats": repeats,
            "tasks": [t["id"] for t in sorted(tasks, key=lambda t: t["id"])],
            "run_root": str(run_root),
            "validated_tasks": sorted(validated),
            "amortize_n": n,
            "index_repeat_s": timing.repeat_index(runs),
            "runs": runs,
            "summary": aggregate(runs, ordered_arms, amortize=n),
            "per_task": paired(runs, ordered_arms)}


def amortize_n(amortize: int | None, runs: list[dict[str, Any]]) -> int:
    """N of the amortized view: `amortize`, else the number of tasks run."""
    if amortize is not None:
        if amortize < 1:
            raise HarnessError("--amortize must be at least 1")
        return amortize
    return max(1, len({r.get("task") for r in runs}))


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def _mean(values: Iterable[Any], digits: int = 3) -> float | None:
    known = [float(v) for v in values if v is not None]
    return round(sum(known) / len(known), digits) if known else None


def _valid(run: dict[str, Any]) -> bool:
    """A run counts for success unless `check_validity` made it invalid;
    runs recorded before classes count."""
    return run.get("valid") is not False


def paired_cells(runs: list[dict[str, Any]], arms: Iterable[str]) -> set[tuple[Any, Any]]:
    """The (task, repeat) cells that have a valid run in every arm."""
    arms = list(arms)
    have: dict[tuple[Any, Any], set[str]] = {}
    bad: set[tuple[Any, Any]] = set()
    for r in runs:
        if r.get("arm") not in arms:
            continue
        cell = (r.get("task"), r.get("repeat"))
        if _valid(r):
            have.setdefault(cell, set()).add(r["arm"])
        else:
            bad.add(cell)
    return {c for c, got in have.items() if c not in bad and got >= set(arms)}


def aggregate(runs: list[dict[str, Any]], arms: Iterable[str], *,
              amortize: int | None = None) -> list[dict[str, Any]]:
    """Per arm: `runs` counts every run, `valid_runs` and `invalid_runs`
    split them by `check_validity`, and `successes` and `success_rate`
    count the arm's valid runs. `paired_runs`, `paired_successes` and
    `paired_rate` cover only the (task, repeat) cells valid in every arm
    (`paired_cells`), so arms compare over the same runs. The time, token
    and tool means (`mean_wall_s`, `mean_tokens_in`, ...) cover every run.
    Each row also carries the per-component time means of
    `timing.summarize_arm` over the arm's valid runs, the same runs as
    `success_rate` (per_task_s, pay_once_s, amortized_s over `amortize`
    tasks, default the number of tasks in `runs`; the components table),
    `timing_invalid_excluded` (the invalid runs left out),
    `per_task_all_runs_s` (per-task time over every run) and
    `paired_timing` (`timing.headline_view` over the paired cells, the
    runs of `paired_rate`: what the timing headline and its deltas use)."""
    arms = list(arms)
    cells = paired_cells(runs, arms)
    tasks_n = amortize_n(amortize, runs)
    repeat = timing.repeat_index(runs)
    rows = []
    for arm in arms:
        mine = [r for r in runs if r["arm"] == arm]
        n = len(mine)
        valid = [r for r in mine if _valid(r)]
        successes = sum(1 for r in valid if r.get("success") is True)
        both = [r for r in valid if (r.get("task"), r.get("repeat")) in cells]
        paired_ok = sum(1 for r in both if r.get("success") is True)
        rows.append({
            "arm": arm,
            "runs": n,
            "valid_runs": len(valid),
            "invalid_runs": n - len(valid),
            "successes": successes,
            "success_rate": round(successes / len(valid), 4) if valid else None,
            "paired_runs": len(both),
            "paired_successes": paired_ok,
            "paired_rate": round(paired_ok / len(both), 4) if both else None,
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
        # The components table covers each arm's valid runs, the same runs
        # as success_rate. The headline compares arms over the cells valid
        # in every arm (`paired_timing`, the runs of paired_rate), and
        # gives per-task time over every run beside it.
        rows[-1].update(timing.summarize_arm(valid, tasks_n, repeat))
        rows[-1]["timing_invalid_excluded"] = n - len(valid)
        rows[-1]["per_task_all_runs_s"] = (
            timing.summarize_arm(mine, tasks_n, repeat)["per_task_s"]
            if len(valid) < n else rows[-1]["per_task_s"])
        rows[-1]["paired_timing"] = timing.headline_view(
            timing.summarize_arm(both, tasks_n, repeat))
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
                "total_path_suggestions": sum(
                    int(r.get("path_suggestions") or 0) for r in mine),
                "total_ownership_warnings": sum(
                    int(r.get("ownership_warnings") or 0) for r in mine),
                "total_ops_after_annotations": sum(
                    int(r.get("ops_after_annotations") or 0) for r in mine),
                **{f"total_{k}": sum(int(r.get(k) or 0) for r in mine)
                   for k in (*LINT_FIELDS, *TEST_SELECTION_FIELDS,
                             *WORKING_SET_FIELDS, *REPLACE_FIELDS)},
                "codemode_tools": sorted({t for r in mine
                                          for t in (r.get("codemode_tools") or [])}),
            })
    return rows


def paired(runs: list[dict[str, Any]], arms: Iterable[str]) -> dict[str, Any]:
    """Per task, for each arm, the success outcome of each repeat in repeat
    order (`outcomes[i]` is repeat i + 1): True or False for a valid run,
    None for an invalid or missing one, so arms stay aligned by repeat.
    `successes` and `repeats` count the valid runs, `invalid` the invalid
    ones."""
    arms = list(arms)
    out: dict[str, Any] = {}
    for tid in sorted({r["task"] for r in runs}):
        mine_all = [r for r in runs if r["task"] == tid]
        last = max((int(r["repeat"]) for r in mine_all
                    if isinstance(r.get("repeat"), int)), default=0)
        row: dict[str, Any] = {}
        for arm in arms:
            by_rep = {r["repeat"]: r for r in mine_all if r["arm"] == arm}
            outcomes: list[bool | None] = []
            for rep in range(1, last + 1):
                r = by_rep.get(rep)
                outcomes.append(None if r is None or not _valid(r)
                                else r.get("success") is True)
            known = [o for o in outcomes if o is not None]
            row[arm] = {"outcomes": outcomes,
                        "successes": sum(known),
                        "repeats": len(known),
                        "invalid": sum(1 for r in by_rep.values() if not _valid(r))}
        out[tid] = row
    return out


def _validation_line(summary: dict[str, Any] | None) -> str:
    if summary is None:
        return "not recorded (a report written before validation was recorded)"
    if not summary.get("ran"):
        return "no" + (f" ({summary['reason']})" if summary.get("reason") else "")
    source = summary.get("source") or "preflight"
    how = "preflight" if source == "preflight" else f"reused `{source}`"
    return (f"yes ({how}), {summary.get('passed')} of {summary.get('tasks')} task(s) "
            f"passed with `{summary.get('check_python')}`")


def _interpreter_line(report: dict[str, Any]) -> str:
    """The check interpreter and why: the report's, else the runs' (an old
    report re-read by `report`: taken from the check logs)."""
    if report.get("check_python"):
        return (f"`{report['check_python']}` "
                f"({report.get('check_python_reason') or 'no reason recorded'})")
    seen = sorted({(r.get("check_python"), r.get("check_python_reason"))
                   for r in report.get("runs") or [] if r.get("check_python")})
    if not seen:
        return "not recorded"
    return "; ".join(f"`{p}` ({why or 'no reason recorded'})" for p, why in seen)


def _success_headline(rows: list[dict[str, Any]]) -> list[str]:
    """Success per arm over its valid runs, the invalid count and the paired
    rate (the cells valid in every arm)."""
    if not rows:
        return []
    parts = []
    for r in rows:
        valid = r.get("valid_runs", r.get("runs"))
        text = f"{r['arm']} {r.get('successes')}/{valid}"
        if r.get("invalid_runs"):
            text += f" ({r['invalid_runs']} invalid)"
        parts.append(text)
    line = f"- Success over valid runs: {', '.join(parts)}."
    if any(r.get("invalid_runs") for r in rows) and len(rows) > 1:
        line += " Paired rate over the cells valid in every arm: " + ", ".join(
            f"{r['arm']} {_paired_cell(r)}" for r in rows) + "."
    return [line]


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
        "- Check interpreter: " + _interpreter_line(report),
        "- Validated before the run: "
        + _validation_line(report.get("validation")),
        *_success_headline(report["summary"]),
        *timing.headline(report["summary"]),
        *([f"- Warning: the logs of {len(report['log_mismatches'])} run(s) do not "
           "match their record, so their components are unknown: "
           + ", ".join(b["run"] for b in report["log_mismatches"]) + "."]
          if report.get("log_mismatches") else []),
        "",
        "## Per arm",
        "",
        "| arm | runs | valid runs | invalid runs | success | success rate | "
        "paired rate | mean wall s | mean prep s | "
        "prep failures | driver errors | mean tokens in | "
        "mean tokens out | mean cost USD | mean tool calls | mean MCP calls | "
        "localization rate |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in report["summary"]:
        lines.append("| " + " | ".join(_cell(v) for v in (
            row["arm"], row["runs"], row.get("valid_runs", row["runs"]),
            row.get("invalid_runs", 0), row["successes"], row["success_rate"],
            _paired_cell(row), row["mean_wall_s"], row["mean_prep_s"], row["prep_failures"],
            row.get("driver_errors", 0),
            row["mean_tokens_in"], row["mean_tokens_out"],
            row["mean_cost_usd"], row.get("mean_tool_calls"),
            row.get("mean_mcp_calls"), row["localization_rate"])) + " |")
    lines += ["", "Success and success rate count the arm's valid runs: a run "
              "whose check failed as `harness` or `environment` for a reason the "
              "agent cannot have caused (see Invalid runs) is invalid. Paired "
              "rate counts only the (task, repeat) cells valid in every arm "
              "(successes/cells), so arms compare over the same runs. "
              "Token, cost and tool columns cover every run and are "
              "blank where the driver reports no "
              "value. MCP calls count calls to the ozagia MCP server. "
              "Prep (dev_memory snapshot and tag cache) runs before the driver timer; "
              "mean prep s is 0 for baseline."]
    lines += timing.render_markdown_sections(report["summary"])
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
    lines += _invalid_section(report.get("runs") or [])
    lines += _agent_test_section(report.get("runs") or [])
    lines += ["", "## Per task (paired)", ""]
    header = "| task | " + " | ".join(report["arms"]) + " |"
    lines += [header, "|---" * (len(report["arms"]) + 1) + "|"]
    for tid, row in report["per_task"].items():
        cells = [f"{row[a]['successes']}/{row[a]['repeats']}"
                 + (f" ({row[a]['invalid']} invalid)" if row[a].get("invalid") else "")
                 for a in report["arms"]]
        lines.append(f"| {_cell(tid)} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _paired_cell(row: dict[str, Any]) -> str | None:
    if row.get("paired_runs") is None:
        return None
    return f"{row.get('paired_rate')} ({row.get('paired_successes')}/{row['paired_runs']})"


def _invalid_section(runs: list[dict[str, Any]]) -> list[str]:
    """The report's list of invalid runs and why, or nothing."""
    bad = [r for r in runs if not _valid(r)]
    if not bad:
        return []
    lines = ["", "## Invalid runs", "",
             "Runs whose check failed for a reason outside the agent's work "
             "(`ozagia.core.failures`): left out of success counts and rates, "
             "kept in the JSON.", "",
             "| task | arm | repeat | check class | reason | basis |",
             "|---|---|---|---|---|---|"]
    for r in bad:
        lines.append("| " + " | ".join(_cell(v) for v in (
            r.get("task"), r.get("arm"), r.get("repeat"), r.get("failure_class"),
            r.get("invalid_reason"), r.get("validity_basis"))) + " |")
    return lines


def agent_check_note(run: dict[str, Any]) -> str:
    """`agent saw green, check saw red` (or the reverse) when the agent's
    last pytest and the check disagree, else "" (also when either class is
    unknown or missing)."""
    agent, check = run.get("agent_test_class"), run.get("failure_class")
    if agent in (None, "unknown") or check in (None, "unknown"):
        return ""
    if agent == "passed" and check != "passed":
        return "agent saw green, check saw red"
    if agent != "passed" and check == "passed":
        return "agent saw red, check saw green"
    return ""


def _agent_test_section(runs: list[dict[str, Any]]) -> list[str]:
    """The agent's last pytest next to the check's result, per run that
    has either class, or nothing."""
    rows = [r for r in runs if r.get("agent_test_class") or r.get("failure_class")]
    if not rows:
        return []
    lines = ["", "## Agent tests and check", "",
             "The agent's last pytest in its transcript (class and summary) "
             "next to the check's class. The two can differ: another "
             "interpreter, other tests, edits after the agent's last run.", "",
             "| task | arm | repeat | agent last pytest | agent summary | check | note |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| " + " | ".join(_cell(v) for v in (
            r.get("task"), r.get("arm"), r.get("repeat"),
            r.get("agent_test_class") or "none", r.get("agent_test_summary"),
            r.get("failure_class"), agent_check_note(r))) + " |")
    return lines


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
    python, reason = resolve_check_python(args.repo, tasks, args.python)
    print(f"check python: {python} ({reason})")
    report = validate_all(args.repo, tasks, base, tasks_dir=spec["dir"],
                          python=python, python_reason=reason,
                          run_root=_run_root(args), keep=args.keep)
    _print_validation(report)
    _write(args.out_json, json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if report["ok"] else 1


def _print_validation(report: dict[str, Any], *, failed_only: bool = False,
                      file: Any = None) -> None:
    for r in report["results"]:
        if failed_only and r["ok"]:
            continue
        base_fc = (r["details"].get("base_check") or {}).get("failure_class")
        why = r["details"].get("reason")
        if not r["ok"] and not r["pass_on_base"] and base_fc:
            why = f"{why}; base check {base_fc}: " \
                  f"{r['details']['base_check'].get('failure_detail')}"
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['id']}: pass_on_base={r['pass_on_base']} "
              f"fail_after_break={r['fail_after_break']} "
              + ("" if r["ok"] else f"({why})"),
              file=file or sys.stdout)


def _run_root(args: argparse.Namespace) -> Path:
    return Path(args.run_root) if args.run_root else new_run_root()


def default_arms(driver_name: str) -> list[str]:
    """Every arm the driver can run: graph_intent only for its drivers."""
    allowed = DRIVER_ARMS.get(driver_name, ARMS)
    return [a for a in ARMS if a in allowed
            and (a != GRAPH_ARM or driver_name in GRAPH_ARM_DRIVERS)]


def _read_validation(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise HarnessError(f"cannot read the validation report {path}: {e}") from e


def summary_line(row: dict[str, Any]) -> str:
    """The console line for one arm: successes over valid runs, the invalid
    count, the paired rate and localization."""
    valid = row.get("valid_runs", row["runs"])
    return (f"{row['arm']}: {row['successes']}/{valid} valid runs succeeded "
            f"(rate {row['success_rate']}; {row.get('invalid_runs', 0)} invalid "
            f"of {row['runs']} runs; paired rate {row.get('paired_rate')} over "
            f"{row.get('paired_runs')} cells), localized {row['localization_rate']}")


def _cmd_run(args: argparse.Namespace) -> int:
    spec = load_tasks(args.tasks)
    base = args.base or spec["base"]
    tasks = select_tasks(spec["tasks"], _split(args.task_ids))
    driver = make_driver(args.driver, live=args.live,
                         max_budget_usd=args.max_budget_usd, model=args.model)
    arms = _split(args.arms) or default_arms(driver.name)
    check_arms(arms, driver)
    run_root = _run_root(args)
    python, reason = resolve_check_python(args.repo, tasks, args.python)
    print(f"check python: {python} ({reason})")
    # One validation, three ways: the preflight (default), a reused
    # `validate --out-json` report (--validation; it must match the base,
    # the tasks and the check interpreter), or none (--skip-validate; the
    # import probe then decides which environment failures are invalid).
    checked: dict[str, Any] | None = None
    source: str | None = None
    if args.validation:
        checked = _read_validation(args.validation)
        why = validation_mismatch(checked, base=base, python=python, tasks=tasks,
                                  tasks_dir=spec["dir"],
                                  base_commit=resolve_base(args.repo, base))
        if why:
            print(f"refused: the validation report {args.validation} does not "
                  f"match this run ({why}); validate again, or pass the "
                  "--python it was made with", file=sys.stderr)
            return 2
        checked = restrict_validation(checked, (t["id"] for t in tasks))
        source = args.validation
        print(f"validation: reused {args.validation}")
    elif not args.skip_validate:
        # A check that does not pass on base or does not fail after the
        # break measures the harness, not the agent.
        checked = validate_all(args.repo, tasks, base, tasks_dir=spec["dir"],
                               python=python, python_reason=reason,
                               run_root=run_root, keep=False)
        source = "preflight"
    if checked is not None and not checked["ok"]:
        bad = [r for r in checked["results"] if not r["ok"]]
        print(f"refused: {len(bad)} of {len(tasks)} task(s) fail validation "
              f"with {python} (--skip-validate runs anyway):", file=sys.stderr)
        _print_validation(checked, failed_only=True, file=sys.stderr)
        return 2
    report = run_eval(args.repo, tasks, arms, driver, base,
                      tasks_dir=spec["dir"], repeats=args.repeats,
                      python=python, python_reason=reason, run_root=run_root,
                      keep=args.keep, timeout_s=args.timeout_s,
                      max_total_usd=args.max_total_usd, validation=checked,
                      validation_source=source,
                      repeat_index=not args.no_repeat_index,
                      amortize=args.amortize)
    _write(args.out_json, json.dumps(report, indent=2, sort_keys=True) + "\n")
    _write(args.out_md, render_markdown(report))
    for row in report["summary"]:
        print(summary_line(row))
    return 0


def resolve_log_dir(record: dict[str, Any], logs_root: str | Path | None) -> Path | None:
    """A run's log dir: its recorded `log_dir`, or with `logs_root`, the
    directory of the same name under `<logs_root>/logs/` (a run root) or
    `<logs_root>/` (a logs dir). The run name is `<task>-<arm>-r<repeat>`."""
    recorded = record.get("log_dir")
    name = (Path(recorded).name if recorded
            else f"{record.get('task')}-{record.get('arm')}-r{record.get('repeat')}")
    if logs_root is not None:
        root = Path(logs_root)
        for cand in (root / "logs" / name, root / name):
            if cand.is_dir():
                return cand
        return None
    return Path(recorded) if recorded else None


def read_check_log(path: Path) -> tuple[str, str, str] | None:
    """(command, stdout, stderr) of a `run_check` log, None when unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    head, _, rest = text.partition("\n")
    if not head.startswith("$ "):
        return None
    out, _, err = rest.partition("\n--- stderr ---\n")
    return head[2:], out, err


def reclassify_run(record: dict[str, Any], log_dir: Path | None, *,
                   validated: Iterable[str] = (),
                   repo: str | Path | None = None,
                   base: str | None = None) -> dict[str, Any]:
    """The W21 fields for a run recorded before failure classes, from its
    `check.log`: `failure_*`, `valid`, `invalid_reason`, `validity_basis`,
    the agent's last pytest, and `check_python` (the log's interpreter)
    when the record has none; `classified_at_report` marks them. {} when
    the record already has a class, or there is no readable check log.
    `validated`: the report's validated task ids (none in old reports, so
    the import probe decides, run now with the logged interpreter). `repo`
    gives first-party names and the environment detail; with `base` (a
    commit id) too, an environment failure on a name no file at base
    imports counts against the agent (`check_validity`; the break patch is
    not known here).

    A record without `check_returncode` whose `check.log` exists is a
    check that timed out: the harness wrote the log after the process ran,
    so the interpreter existed then. It is classified as a timeout, not by
    today's filesystem (a since-deleted interpreter is not `harness`)."""
    if record.get("failure_class") is not None or log_dir is None:
        return {}
    got = read_check_log(Path(log_dir) / "check.log")
    if got is None:
        return {}
    command, out, err = got
    argv = command.split()
    interpreter = record.get("check_python") or (argv[0] if argv else None)
    rc = record.get("check_returncode")
    first_party = _first_party(Path(repo)) if repo else None
    # The record keeps no check timeout flag. A check that could not start
    # wrote no log in the old harness, so no exit code with a log means
    # the check ran and was killed at its timeout.
    timed_out = rc is None
    failure = F.classify(rc, timed_out, out, err, command=argv,
                         first_party=first_party, interpreter=interpreter,
                         root=repo, env_cache="read")
    fields: dict[str, Any] = {
        **check_validity(failure, base_passed=record.get("task") in set(validated),
                         python=interpreter, workdir=None,
                         repo=repo if base else None, base=base),
        **failure_fields(failure),
        "classified_at_report": True}
    if record.get("agent_test_class") is None:
        fields.update(agent_test_fields(Path(log_dir), None, first_party))
    if not record.get("check_python") and interpreter:
        fields.update(check_python=interpreter,
                      check_python_reason="not recorded; read from the check log")
    return fields


_COMMIT_ID = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


def report_base_commit(report: dict[str, Any]) -> str | None:
    """The commit a report's runs started from: `base_commit`, else `base`
    when it is a full commit id (an old report's `HEAD` or branch name may
    have moved since); None otherwise."""
    got = report.get("base_commit")
    if isinstance(got, str) and got:
        return got
    base = report.get("base")
    return base if isinstance(base, str) and _COMMIT_ID.fullmatch(base) else None


def rereport(report: dict[str, Any], *, logs_root: str | Path | None = None,
             amortize: int | None = None,
             repo: str | Path | None = None) -> dict[str, Any]:
    """`report` with every run's `timing` recomputed from its logs, and the
    summary and per-task tables rebuilt. Other run fields are kept. The
    repeat index cost is not re-measured: it is taken from `prep_repeat_s`
    where a run recorded one. Runs whose logs do not match their record
    (`timing.logs_mismatch`: a reused run root) get no model/tool
    breakdown and are listed in `log_mismatches`. Runs recorded before
    failure classes are classified from their check logs
    (`reclassify_run`), except those whose logs do not match."""
    runs = report.get("runs")
    if not isinstance(runs, list):
        raise HarnessError("report JSON has no 'runs' list")
    arms = report.get("arms") or sorted({r.get("arm") for r in runs})
    out = dict(report)
    validated = report.get("validated_tasks") or []
    base = report_base_commit(report)
    new_runs = []
    for r in runs:
        r = dict(r)
        log_dir = resolve_log_dir(r, logs_root)
        r["timing"] = timing.run_timing(r, log_dir)
        if not r["timing"].get("log_mismatch"):
            r.update(reclassify_run(r, log_dir, validated=validated, repo=repo,
                                    base=base))
        new_runs.append(r)
    n = amortize_n(amortize, new_runs)
    out.pop("warm_index_s", None)
    out.update(runs=new_runs, amortize_n=n,
               index_repeat_s=timing.repeat_index(new_runs),
               log_mismatches=log_mismatches(new_runs),
               summary=aggregate(new_runs, arms, amortize=n),
               per_task=paired(new_runs, arms))
    out.setdefault("tasks", sorted({r.get("task") for r in new_runs}))
    return out


def log_mismatches(runs: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Runs whose logs are not theirs: [{run, log_dir, reason}]."""
    out = []
    for r in runs:
        reason = (r.get("timing") or {}).get("log_mismatch")
        if reason:
            out.append({"run": f"{r.get('task')}-{r.get('arm')}-r{r.get('repeat')}",
                        "log_dir": str(r.get("log_dir")), "reason": reason})
    return out


def _cmd_report(args: argparse.Namespace) -> int:
    try:
        report = json.loads(Path(args.in_json).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError(f"cannot read {args.in_json}: {exc}") from exc
    if not isinstance(report, dict):
        raise HarnessError(f"{args.in_json}: expected a report object")
    out = rereport(report, logs_root=args.logs_root, amortize=args.amortize,
                   repo=args.repo)
    _write(args.out_json, json.dumps(out, indent=2, sort_keys=True) + "\n")
    _write(args.out_md, render_markdown(out))
    print("- Check interpreter: " + _interpreter_line(out))
    print("- Validated before the run: " + _validation_line(out.get("validation")))
    for line in _success_headline(out["summary"]) + timing.headline(out["summary"]):
        print(line)
    bad = out["log_mismatches"]
    if bad:
        print(f"warning: the logs of {len(bad)} run(s) do not match their record "
              "(a reused run root? pass --logs-root); their components are unknown:",
              file=sys.stderr)
        for b in bad:
            print(f"  {b['run']}: {b['reason']}", file=sys.stderr)
    missing = sum(1 for r in out["runs"] if (r.get("timing") or {}).get("model") is None)
    if missing:
        print(f"{missing} of {len(out['runs'])} run(s) have no model/tool breakdown "
              "(see timing.reasons in the JSON)")
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
                        help="python for the checks and the agent's `python` "
                             "(default: the interpreter `ozagia env --for` picks "
                             "for the checks' test files in --repo)")
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
    once = r.add_mutually_exclusive_group()
    once.add_argument("--skip-validate", action="store_true",
                      help="start without validating the selected tasks first "
                           "(by default `run` refuses with exit 2 when a check "
                           "does not pass on base or does not fail after the "
                           "break); invalid runs are then decided by an import "
                           "probe")
    once.add_argument("--validation", default=None,
                      help="reuse a `validate --out-json` report instead of "
                           "validating again; it must have the same base, every "
                           "selected task unchanged and the same check "
                           "interpreter, else `run` refuses with exit 2")
    r.add_argument("--amortize", type=int, default=None, metavar="N",
                   help="N of the amortized view (pay-once / N + per task; "
                        "default: the number of tasks run)")
    r.add_argument("--no-repeat-index", action="store_true",
                   help="skip the repeat index cost (snapshot load + refresh + "
                        "tag cache) measured once per run root")
    r.set_defaults(func=_cmd_run)

    rep = sub.add_parser("report", help="recompute timing and the report from a "
                                        "run JSON and its logs")
    rep.add_argument("--in", dest="in_json", required=True, help="run JSON")
    rep.add_argument("--logs-root", default=None,
                     help="where the run logs are now: a run root (with logs/) "
                          "or a logs dir (default: each run's recorded log_dir)")
    rep.add_argument("--amortize", type=int, default=None, metavar="N",
                     help="N of the amortized view (default: the number of tasks)")
    rep.add_argument("--repo", default=None,
                     help="the source repo, for first-party names and the "
                          "environment detail when classifying runs recorded "
                          "before failure classes (default: none)")
    rep.add_argument("--out-md", default=None)
    rep.add_argument("--out-json", default=None)
    rep.set_defaults(func=_cmd_report)
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
