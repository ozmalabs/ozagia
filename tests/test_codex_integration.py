"""The Codex hook end to end: the hook runs as a subprocess, as Codex runs
it, against a real daemon (`python -m ozagia.daemon serve`) on a temp git
repository that enables jobs. Covers the local git refusal, patch-miss
recovery, the job `ps` answer, the read ledger's repeat note, the
SessionStart brief and the PreCompact ledger reset (see docs/codex.md)."""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ozagia.daemon.client import DaemonClient
from ozagia.integrations.codex import hook

SRC = Path(__file__).resolve().parents[1] / "src"
READY_TIMEOUT_S = 60
A_PY = "def f(x):\n    y = x + 1\n    return y\n\n\ndef g(z):\n    return z * 2\n"


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OZAGIA_") and k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(SRC)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(extra)
    return env


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=root, check=True, capture_output=True)


def _gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        try:                            # reap it if it is our child
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """A git repository with `[jobs] enabled = true` and a daemon serving
    it; the daemon is shut down (its jobs with it) afterwards."""
    root = (tmp_path_factory.mktemp("cxi") / "repo")
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text(A_PY, encoding="utf-8")
    (root / ".ozagia.toml").write_text("[jobs]\nenabled = true\n", encoding="utf-8")
    # Keep job admission independent of this host's free disk and memory. The
    # budgets are far below what `sleep` needs; production keeps its defaults.
    (root / "ozagia").mkdir()
    (root / "ozagia" / "resources.toml").write_text(
        "reserve_memory_pct = 0\n"
        "reserve_disk_gb = 0\n"
        "\n"
        "[limits]\n"
        "job_memory_mb = 256\n"
        "job_disk_mb = 64\n"
        "\n"
        "[admission]\n"
        "min_free_memory_pct = 0\n"
        "min_free_disk_gb = 0\n",
        encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial commit")
    root = root.resolve()
    proc = subprocess.Popen(
        [sys.executable, "-m", "ozagia.daemon", "serve", "--root", str(root)],
        env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=str(root))
    ready, _, _ = select.select([proc.stdout], [], [], READY_TIMEOUT_S)
    line = proc.stdout.readline() if ready else ""
    if not line.startswith("ready "):
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError(f"no ready line: {line!r}")
    # Resolve from this process's ready line. `socket_path()` honors the
    # caller's OZAGIA_DAEMON_SOCK, which may point at a developer's live
    # session because module-scoped fixtures run before function autouse
    # fixtures sanitize the test environment.
    sock = Path(line.removeprefix("ready ").strip())
    yield {"root": root, "proc": proc, "sock": sock}
    if proc.poll() is None:
        try:
            with DaemonClient(sock, timeout=10) as c:
                c.shutdown()
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
            proc.wait(timeout=5)
    proc.stdout.close()
    proc.stderr.close()


def run_hook(live, event: str, session: str, **fields):
    """One hook event through `python -m ozagia.integrations.codex hook`.
    Autostart is off: the hook must use the running daemon."""
    payload = {"session_id": session, "transcript_path": f"/tmp/{session}.jsonl",
               "cwd": str(live["root"]), "hook_event_name": event,
               "model": "gpt-5.5", "permission_mode": "default", **fields}
    proc = subprocess.run(
        [sys.executable, "-m", "ozagia.integrations.codex", "hook"],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_env(OZAGIA_DAEMON_AUTOSTART="0"), cwd=str(live["root"]),
        timeout=hook.HOOK_TIMEOUT_S + 10)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def tool(live, event: str, session: str, name: str, tool_input: dict, **extra):
    return run_hook(live, event, session, turn_id="turn-1", tool_name=name,
                    tool_use_id="call_1", tool_input=tool_input, **extra)


def _denied(out) -> str:
    assert out is not None
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    assert spec["permissionDecision"] == "deny"
    return spec["permissionDecisionReason"]


def _context(out, event="PostToolUse") -> str:
    assert out is not None
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == event
    return spec["additionalContext"]


def _sed(live, session: str, output: str):
    return tool(live, "PostToolUse", session, "Bash",
                {"command": "sed -n '1,3p' src/a.py"}, tool_response=output)


SED_OUT = "".join(A_PY.splitlines(keepends=True)[:3])


def test_pre_bash_git_refused(live):
    reason = _denied(tool(live, "PreToolUse", "s-git", "Bash",
                          {"command": "git commit -am 'wip'"}))
    assert "git" in reason and "commit" in reason


def test_pre_apply_patch_miss_denied_with_region(live):
    patch = ("*** Begin Patch\n*** Update File: src/a.py\n@@\n def f(x):\n"
             "-    y = x + 2\n+    y = x + 3\n     return y\n*** End Patch\n")
    reason = _denied(tool(live, "PreToolUse", "s-patch", "apply_patch",
                          {"command": patch}))
    assert "nearest region: L1-L3 sym:src/a.py::f" in reason
    assert "2:     y = x + 1" in reason
    assert "node sha256" in reason
    # A patch that applies passes.
    good = patch.replace("x + 2", "x + 1")
    assert tool(live, "PreToolUse", "s-patch", "apply_patch", {"command": good}) is None
    assert (live["root"] / "src" / "a.py").read_text() == A_PY


def test_pre_bash_ps_of_a_running_job_denied_with_ps_output(live):
    with DaemonClient(live["sock"], timeout=10) as c:
        job = c.request("job_start", argv=["sleep", "30"])["job"]
    try:
        time.sleep(0.2)
        reason = _denied(tool(live, "PreToolUse", "s-ps", "Bash",
                              {"command": f"ps -p {job['pid']}"}))
        assert reason.startswith("[ozagia] answered from the ozagia job table")
        assert "PID TTY" in reason and str(job["pid"]) in reason
        assert f"job {job['id']}" in reason
        # A ps about no job runs as usual.
        assert tool(live, "PreToolUse", "s-ps", "Bash", {"command": "ps -p 1"}) is None
    finally:
        with DaemonClient(live["sock"], timeout=10) as c:
            c.request("job_stop", job=job["id"], grace_s=1)
    assert _gone(job["pid"])


def test_post_bash_repeat_read_gets_the_unchanged_note(live):
    first = _sed(live, "s-read", SED_OUT)
    assert first is None or "unchanged since read" not in _context(first)
    again = _context(_sed(live, "s-read", SED_OUT))
    assert again.startswith("[ozagia] unchanged since read 1 (lines 1-3, sha ")
    # Another session has its own ledger.
    other = _sed(live, "s-read-2", SED_OUT)
    assert other is None or "unchanged since read" not in _context(other)


def test_session_start_brief(live):
    text = _context(run_hook(live, "SessionStart", "s-start", source="startup"),
                    "SessionStart")
    assert text.startswith("[ozagia] session brief")
    assert "initial commit" in text
    assert hook.MCP_TOOLS_LINE in text and hook.JOBS_LINE in text
    assert len(text) <= hook.BRIEF_MAX_CHARS


def test_pre_compact_clears_the_ledger(live):
    _sed(live, "s-compact", SED_OUT)
    assert "unchanged since read" in _context(_sed(live, "s-compact", SED_OUT))
    assert run_hook(live, "PreCompact", "s-compact", turn_id="t", trigger="auto") is None
    after = _sed(live, "s-compact", SED_OUT)
    assert after is None or "unchanged since read" not in _context(after)
    # The ledger fills again from there.
    assert "unchanged since read" in _context(_sed(live, "s-compact", SED_OUT))


def test_hooks_used_the_running_daemon(live):
    """The hooks reached the fixture's daemon (autostart was off)."""
    assert live["proc"].poll() is None
    _sed(live, "s-stats", SED_OUT)
    with DaemonClient(live["sock"], timeout=10) as c:
        stats = c.stats()["stats"]
    assert stats["by_harness"].get("codex", 0) >= 1
