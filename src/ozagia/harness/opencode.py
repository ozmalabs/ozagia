"""`ozagia agent`: an OpenCode session on a repository, with the graph in its path.

`run_agent(workdir, prompt=...)` does, in order:

1. Finds the `opencode` executable (a clear error if it is missing) and makes
   a run root `<tmp>/ozagia_agent/<stamp>-<pid>-<hex>/` for the logs.
2. With `clone=True`, makes an isolated clone of `workdir` at `base`
   (default HEAD) under `<run root>/work/clone` (`eval.agent.make_workdir`,
   isolation "clone": shared objects, own refs, no `origin`) and runs there.
   Uncommitted changes in `workdir` are not in the clone. The clone is kept;
   the summary prints how to remove it.
3. Adds `/.opencode/plugins/ozagia.js` and `/.ozagia/` to the repository's
   `info/exclude` (only the lines that are missing), so neither shows in
   `git status` and no tracked file changes. At the end (unless
   `keep_plugin`) the lines this run added are removed again when their
   path is gone (the plugin's); the state dir's line stays while the state
   dir, which keeps the snapshot, exists.
4. Opens a `GraphSession` (snapshot built or refreshed, daemon started). The
   interactive TUI does not wait: `background` makes the session prepare the
   snapshot and start the daemon while OpenCode runs, and each phase goes to
   `<log_dir>/launch_status.json`, which the TUI sidebar reads
   (`OZAGIA_LAUNCH_STATUS`). A non-interactive run waits for both first.
   With `progress`, the synchronous phases are reported on stderr; `ozagia
   agent` passes a `harness.progress.Spinner`. Library callers get no output
   unless they ask for it.
5. Installs the plugin into `<workdir>/.opencode/plugins/ozagia.js`.
   A plugin file that was already there is overwritten unless git tracks it,
   and is not removed afterwards, except one whose content is exactly
   `plugin_source()`: that one was left by a run killed before teardown, and
   is removed like a new one.
6. Writes `OPENCODE_CONFIG` to `<run root>/logs/opencode_config.json`: the
   harness config. OpenCode's own permission service remains in charge of
   shell approvals; the harness does not override the user's policy. Eval runs use
   their separate restrictive config (`eval.agent.OPENCODE_GIT_DENY`).
   a `model` goes here too, because the TUI has no `--model` flag.
   `--opencode-config PATH` layers only the allowlisted provider keys
   (`provider`, `providers`, `model`, `small_model`) from the caller's file;
   the host/global config is never read implicitly, and plugins, MCP servers,
   permissions, agents and tools can never come from that file.
7. Runs OpenCode in the workdir with `OZAGIA_DAEMON_SOCK`, `PWD` (OpenCode
   takes its project directory from it), `OPENCODE_CONFIG`, and with
   `run_python`, a PATH whose first entry is a `python` shim. With
   `repo_map_tokens`, `OZAGIA_REPO_MAP_TOKENS` is set too (and removed from
   the inherited environment otherwise), so the plugin asks the daemon for a
   bounded, ranked repository map on each model request. Without it the
   plugin's ordinary once-per-prompt hint is unchanged.
   - Prompt given: `opencode run --standalone --format json --auto --archived
     [-m model] [extra args]` with the prompt on stdin, never in argv,
     because OpenCode logs its full argv once before the plugin hook runs.
     Its own process group, stdout and stderr to files in the logs, killed
     after `timeout_s`.
   - No prompt: the TUI, `opencode --standalone [extra args] [--prompt P]
     <workdir>`, attached to the terminal.
8. Always (also on Ctrl-C, SIGTERM or an error): removes the plugin and
   the exclude lines unless `keep_plugin`, stops the daemon (stats,
   shutdown, kill group, remove socket). After a hard kill (SIGKILL) none
   of this runs; the daemon then exits by itself (`--parent-pid`), and the
   next run removes the leftover plugin.

Interactive mode and the plugin's environment. The plugin acts only when
`OZAGIA_DAEMON_SOCK` is in the environment of the process that loads plugins.
Without `--standalone` the TUI attaches to OpenCode's background service,
which was started earlier with its own environment, so the plugin there never
sees the socket. `opencode --help` (2.3.0) lists `--standalone` for the TUI:
"Run with a private server instead of the background service"; that server is
started by the TUI process, so it inherits the environment set here. The
harness therefore always passes `--standalone`. That the TUI's private server
loads project plugins is not verified (`opencode run --standalone` is); the
summary's `plugin loaded` (daemon `hellos > 0`) shows it after each session.
While the TUI runs, the harness ignores SIGINT itself, so Ctrl-C goes to the
TUI; teardown runs when the TUI exits.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Sequence

from ozagia.config import config_path
from ozagia.core import mesh as mesh_module
from ozagia.core import temp_space as TS
from ozagia.daemon.protocol import ENV_SOCK
from ozagia.eval import agent as ea
from ozagia.harness import launch_status
from ozagia.harness.progress import NULL_PROGRESS, Progress
from ozagia.harness.session import GraphSession, no_interrupt, run_group
from ozagia.integrations.opencode import install as plugin

RUN_ARGV = ("run", "--standalone", "--format", "json", "--auto", "--archived")
TUI_ARGV = ("--standalone",)
EXCLUDE_PATTERNS = ("/" + plugin.PLUGIN_REL.as_posix(),
                    "/" + plugin.TUI_BRAND_REL.as_posix(),
                    "/" + plugin.TUI_PROFILE_REL.as_posix(), "/.ozagia/")
RUNS_DIRNAME = "ozagia_agent"
REPO_MAP_ENV = "OZAGIA_REPO_MAP_TOKENS"
REPO_MAP_DEFAULT_TOKENS = 1500
REPO_MAP_MIN_TOKENS = 200
REPO_MAP_MAX_TOKENS = 8000
# Default lifetime of a managed run root (7 days).
DEFAULT_TMP_TTL_S = 7 * 24 * 60 * 60
SCRATCH_ENV = "OZAGIA_SCRATCH_ROOT"
# The harness registers as a runner identity, never as an invented `ses_...`.
MESH_SUMMARY_ENV = "OZAGIA_MESH_SUMMARY"
MESH_HARNESS = "opencode-runner"
MESH_EXPOSURE = "read"
MESH_SUMMARY_MAX_CHARS = 700
MESH_HEARTBEAT_S = mesh_module.HEARTBEAT_S

# Seconds the TUI gets to exit after SIGTERM before SIGKILL.
TUI_KILL_GRACE_S = ea.KILL_GRACE_S


class AgentError(RuntimeError):
    """The agent session cannot run (missing opencode, bad arguments)."""


def find_opencode(opencode: str = "opencode") -> str:
    """The opencode executable's path, or AgentError."""
    exe = shutil.which(opencode)
    if not exe:
        raise AgentError(
            f"OpenCode executable {opencode!r} not found on PATH. Install OpenCode "
            "(https://opencode.ai) or put it on PATH, then retry.")
    return exe


def validate_repo_map_tokens(value: Any) -> int:
    """An integer repo-map budget in [200, 8000], else AgentError."""
    tokens: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        tokens = value
    if tokens is None or not REPO_MAP_MIN_TOKENS <= tokens <= REPO_MAP_MAX_TOKENS:
        raise AgentError(
            f"repo_map_tokens must be an integer between {REPO_MAP_MIN_TOKENS} "
            f"and {REPO_MAP_MAX_TOKENS}, got {value!r}")
    return tokens


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)


def git_exclude(workdir: str | Path,
                patterns: Sequence[str] = EXCLUDE_PATTERNS) -> dict[str, Any] | None:
    """Add the root-anchored `patterns` to the repository's `info/exclude`,
    only those not already there. Patterns are anchored at `workdir` (its
    prefix inside the repository is added). Returns {path, added, targets
    (added line -> the path it hides)}, or None when `workdir` is not in a
    git repository."""
    workdir = Path(workdir).resolve()
    where = _git(["rev-parse", "--git-path", "info/exclude"], workdir)
    prefix = _git(["rev-parse", "--show-prefix"], workdir)
    if where.returncode != 0 or prefix.returncode != 0:
        return None
    path = Path(where.stdout.strip())
    if not path.is_absolute():
        path = workdir / path
    want = {"/" + prefix.stdout.strip() + p.lstrip("/"): p for p in patterns}
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    have = {line.strip() for line in text.splitlines()}
    added = [w for w in want if w not in have]
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        sep = "" if not text or text.endswith("\n") else "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(sep + "\n".join(added) + "\n")
    return {"path": str(path), "added": added,
            "targets": {w: str(workdir / want[w].strip("/")) for w in added}}


def git_unexclude(info: dict[str, Any] | None) -> list[str]:
    """Remove from `info/exclude` the lines `git_exclude` added (`info` is
    its result) whose path is gone, one occurrence each, leaving every
    other line. A line whose path still exists (the state dir, which keeps
    the snapshot for the next run) stays, so it does not show up in `git
    status`. Returns the lines removed."""
    if not info or not info.get("added"):
        return []
    path = Path(info["path"])
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return []
    targets = info.get("targets") or {}
    todo = [w for w in info["added"]
            if not (w in targets and os.path.lexists(targets[w]))]
    kept: list[str] = []
    removed: list[str] = []
    for line in lines:
        if line.strip() in todo:
            todo.remove(line.strip())
            removed.append(line.strip())
        else:
            kept.append(line)
    if removed:
        path.write_text("".join(kept), encoding="utf-8")
    return removed


MCP_MODES = ("coding", "research", "combined")

# `ozagia agent --opencode-config` layers only provider-related keys from a
# caller-supplied OpenCode config into the harness-owned one. Everything else
# (plugins, MCP servers, permissions, agents, tools) stays the harness's or
# the host's own business; see `load_provider_config`.
OPENCODE_CONFIG_KEYS = ("provider", "providers", "model", "small_model")
# Allowed but not copied: the harness sets its own `$schema`.
_OPENCODE_CONFIG_IGNORED = ("$schema",)


def load_provider_config(path: str | Path) -> dict[str, Any]:
    """The provider-related part of a caller-supplied OpenCode config.

    Only `provider`, `providers`, `model` and `small_model` are taken over
    (`$schema` is allowed and ignored). Any other key is an error rather than
    silently dropped, so a file meant to configure plugins, permissions, MCP
    servers or agents cannot quietly widen the harness's trust boundary. The
    file is read as JSON; `{env:...}` and `{file:...}` values are left exactly
    as written and are never resolved or printed.
    """
    cfg = Path(path)
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise AgentError(f"cannot read --opencode-config {cfg}: {reason}") from None
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentError(
            f"--opencode-config {cfg} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno} column {exc.colno})") from None
    if not isinstance(doc, dict):
        raise AgentError(f"--opencode-config {cfg} must be a JSON object")
    unsupported = sorted(
        key for key in doc
        if key not in OPENCODE_CONFIG_KEYS and key not in _OPENCODE_CONFIG_IGNORED)
    if unsupported:
        raise AgentError(
            f"--opencode-config {cfg} has unsupported key(s): "
            f"{', '.join(unsupported)}; only provider-related keys are layered "
            f"({', '.join(OPENCODE_CONFIG_KEYS)})")
    merged: dict[str, Any] = {}
    for key in OPENCODE_CONFIG_KEYS:
        if key not in doc:
            continue
        value = doc[key]
        if key in ("provider", "providers"):
            if not isinstance(value, dict):
                raise AgentError(f"--opencode-config {cfg}: {key} must be an object")
        elif not isinstance(value, str):
            raise AgentError(f"--opencode-config {cfg}: {key} must be a string")
        merged[key] = value
    return merged


def mcp_server_entry(root: Path, mode: str, *, tool_python: str = sys.executable
                     ) -> dict[str, Any]:
    """OpenCode's `mcp` entry for the ozagia MCP server on `root` in `mode`
    (coding, research or combined; see ozagia.integrations.mcp.server)."""
    if mode not in MCP_MODES:
        raise AgentError(f"MCP mode must be one of {', '.join(MCP_MODES)}, got {mode!r}")
    env = {"OZAGIA_ROOT": str(root)}
    if os.path.abspath(tool_python) == os.path.abspath(sys.executable):
        import ozagia
        env["PYTHONPATH"] = str(Path(ozagia.__file__).resolve().parents[1])
    return {"type": "local", "enabled": True,
            "command": [tool_python, "-m", "ozagia.integrations.mcp.server",
                        "--root", str(root), "--mode", mode],
            "environment": env}


def write_opencode_config(path: Path, *, model: str | None = None,
                          mcp: dict[str, Any] | None = None,
                          provider_config: dict[str, Any] | None = None) -> Path:
    """The OPENCODE_CONFIG for one harness session, and optional MCP server.

    `provider_config` is the allowlisted provider part from
    `load_provider_config` (`provider`, `providers`, `model`, `small_model`);
    `model` here stays authoritative over its `model`. `mcp` is the harness's
    own `ozagia` entry, never one from the caller's file.
    """
    doc: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
    }
    if provider_config:
        doc.update(provider_config)
    if model:
        doc["model"] = model
    if mcp is not None:
        doc["mcp"] = {"ozagia": mcp}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _tracked(workdir: Path, rel: Path) -> bool:
    return _git(["ls-files", "--error-unmatch", rel.as_posix()], workdir).returncode == 0


@contextlib.contextmanager
def _sigterm_as_interrupt() -> Iterator[None]:
    """SIGTERM raises KeyboardInterrupt (main thread only), so teardown runs."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def on_term(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, on_term)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextlib.contextmanager
def _prompt_stdin(prompt: str | None) -> Iterator[Any]:
    """The prompt on fd 0 for `opencode run`, never in argv.

    OpenCode writes its full argv to its own log once, before the plugin hook
    runs, so a prompt in argv leaks there. The prompt goes to an anonymous
    `tempfile.TemporaryFile` (no pathname, mode 0600) written and rewound
    before the child starts, or to /dev/null when there is none."""
    if not prompt:
        yield subprocess.DEVNULL
        return
    stream = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
    try:
        stream.write(prompt)
        stream.seek(0)
        yield stream
    finally:
        stream.close()


def _run_tui(argv: list[str], *, cwd: Path, env: dict[str, str],
             timeout_s: float | None) -> dict[str, Any]:
    """The TUI on this terminal. SIGINT is ignored here (a Python handler, so
    the child gets the default disposition back at exec) while it runs."""
    started = time.monotonic()
    main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGINT, lambda *_: None) if main else None
    timed_out = False
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env)
        try:
            code: int | None = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out, code = True, None
            _stop(proc)
        except BaseException:
            _stop(proc)
            raise
    finally:
        if main:
            signal.signal(signal.SIGINT, previous)
    return {"returncode": code, "timed_out": timed_out,
            "seconds": round(time.monotonic() - started, 3)}


def _stop(proc: subprocess.Popen) -> None:
    """SIGTERM, wait TUI_KILL_GRACE_S, then SIGKILL."""
    with no_interrupt():
        proc.terminate()
        try:
            proc.wait(timeout=TUI_KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _register_run_root(source: Path, run_root: Path, *, model: str | None,
                       ttl_s: int | None) -> tuple[dict[str, Any] | None, str | None]:
    """Adopt the newly created run root; return (entry, warning)."""
    try:
        entry = TS.adopt(source, run_root, kind="agent",
                         reason="opencode agent run", run_id=run_root.name,
                         model=model, ttl_s=ttl_s, owned=True, mark=True)
        return entry, None
    except Exception as exc:  # noqa: BLE001 - registration fails open
        return None, f"managed tmp registration failed: {exc}"


def _register_scratch(source: Path, run_root: Path, *,
                      ttl_s: int | None) -> tuple[Path | None, str | None, str | None]:
    """Create the disk-backed scratch workspace; return (path, id, warning)."""
    target = TS.workspaces_root(source) / "agent" / run_root.name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        entry = TS.create(source, kind="agent",
                          reason="agent scratch workspace",
                          run_id=run_root.name, ttl_s=ttl_s, path=target)
        return Path(entry["path"]), entry["id"], None
    except Exception as exc:  # noqa: BLE001 - scratch is optional
        return None, None, f"agent scratch workspace unavailable: {exc}"


def _tmp_env(run_root: Path) -> dict[str, str]:
    """A 0700 TMPDIR under the run root, exposed under the usual names."""
    try:
        path = run_root / "tmp"
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError:
        return {}
    return {"TMPDIR": str(path), "TMP": str(path), "TEMP": str(path)}


@contextlib.contextmanager
def _lease_guard(lease: Any) -> Iterator[None]:
    """Close a managed run-root lease when the session context exits."""
    try:
        yield
    finally:
        if lease is not None:
            lease.close()


def _refresh_tmp_usage(source: Path, res: dict[str, Any]) -> None:
    if not res.get("tmp_id"):
        return
    try:
        entry = TS.get(source, res["tmp_id"])
    except Exception:  # noqa: BLE001 - usage is informational
        return
    res.update(tmp_path=entry.get("path"), tmp_expires_at=entry.get("expires_at"),
               tmp_bytes=entry.get("bytes"), tmp_files=entry.get("files"),
               tmpfs=entry.get("tmpfs"))


def _clean_run_tmp(source: Path, res: dict[str, Any], *,
                   managed: bool, scratch_id: str | None) -> None:
    """Delete an owned run root and scratch after a successful, uncloned run."""
    if res.get("timed_out") or res.get("exit_code") != 0:
        res["tmp_cleaned"] = False
        res["tmp_clean_reason"] = "run did not succeed"
        return
    if res.get("clone"):
        res["tmp_cleaned"] = False
        res["tmp_clean_reason"] = "clone kept"
        return
    if not managed or not res.get("tmp_id"):
        res["tmp_cleaned"] = False
        res["tmp_clean_reason"] = ("explicit --runs-dir is unmanaged" if not managed
                                   else "run root is not registered")
        return
    run_root = Path(res["run_root"])
    try:
        TS.release(source, res["tmp_id"], delete=True)
    except Exception as exc:  # noqa: BLE001 - cleanup must not fail the run
        res["tmp_cleaned"] = False
        res["tmp_clean_error"] = f"managed tmp release failed: {exc}"
        return
    res["tmp_cleaned"] = not os.path.lexists(run_root)
    if not res["tmp_cleaned"]:
        res["tmp_clean_reason"] = "run root still exists after release"
    if scratch_id:
        try:
            TS.release(source, scratch_id, delete=True)
        except Exception as exc:  # noqa: BLE001 - scratch is best effort
            res.setdefault("tmp_clean_warnings", []).append(str(exc))


def _tmp_line(res: dict[str, Any]) -> str:
    if not res.get("tmp_registered"):
        if res.get("runs_dir_explicit"):
            why = "explicit --runs-dir is unmanaged"
        else:
            why = res.get("tmp_warning") or "not registered"
        return f"  managed tmp: not registered ({why}); {res.get('run_root')}"
    filesystem = ("tmpfs" if res.get("tmpfs") else
                  "disk" if res.get("tmpfs") is False else "filesystem unknown")
    ttl = res.get("tmp_ttl_s")
    expires = res.get("tmp_expires_at")
    expiry = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires))
              if expires is not None else "none")
    return (f"  managed tmp: {res.get('tmp_id')}  {res.get('tmp_path')}  ({filesystem})"
            f"  TTL {ttl}s (expires {expiry}); "
            f"ozagia tmp release {res.get('tmp_id')} --delete")


def _mesh_warn(res: dict[str, Any], message: Any) -> None:
    """Record a non-fatal mesh problem without making the run fail."""
    text = " ".join(str(message).split())[:300]
    if not text:
        return
    old = res.get("mesh_warning")
    res["mesh_warning"] = f"{old}; {text}" if old else text


def _mesh_branch(work: Path) -> str | None:
    """The checked-out branch, when git can name one cheaply."""
    found = _git(["rev-parse", "--abbrev-ref", "HEAD"], work)
    name = found.stdout.strip()
    return name if found.returncode == 0 and name and name != "HEAD" else None


def _mesh_text(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value if value is not None else "?").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _mesh_summary_text(payload: dict[str, Any],
                       max_chars: int = MESH_SUMMARY_MAX_CHARS) -> str:
    """Bounded metadata for the OpenCode process; never a body or subject."""
    self_row = payload.get("self") or {}
    key = self_row.get("key")
    parts = [
        f"mesh={_mesh_text(payload.get('mesh_id'), 64)}",
        f"self={_mesh_text(key, 32)}",
        f"{_mesh_text(self_row.get('harness'), 32)}:{_mesh_text(self_row.get('session'), 80)}",
        f"role={_mesh_text(self_row.get('role'), 16)}",
        f"exposure={_mesh_text(self_row.get('exposure'), 16)}",
    ]
    sessions = [item for item in (payload.get("sessions") or [])
                if item.get("key") != key]
    live = sum(1 for item in sessions
               if item.get("effective_state") == "live")
    stale = sum(1 for item in sessions
                if item.get("effective_state") == "stale")
    parts.append(f"peers live={live} stale={stale}")
    links = []
    link_items = self_row.get("links")
    if link_items is None:
        link_items = payload.get("links") or []
    for item in link_items[:4]:
        peer = item.get("peer") or {}
        if not isinstance(peer, dict):
            peer = {}
        links.append(
            f"{_mesh_text(peer.get('harness'), 24)}:"
            f"{_mesh_text(peer.get('session'), 60)}"
            f" rel={_mesh_text(item.get('relation'), 8)}"
            f" state={_mesh_text(item.get('state'), 12)}")
    if links:
        parts.append("links=[" + " | ".join(links) + "]")
    leases = []
    for item in (payload.get("leases") or []):
        if item.get("effective_state") not in {"active", "stale"}:
            continue
        if len(leases) >= 4:
            break
        resource = item.get("resource") or {}
        holder = item.get("holder") or {}
        lease = (f"{_mesh_text(resource.get('kind'), 16)}:"
                 f"{_mesh_text(resource.get('id'), 80)}"
                 f" mode={_mesh_text(resource.get('mode'), 8)}"
                 f" exp={_mesh_text(item.get('expires_at'), 32)}"
                 f" queue={len(item.get('queue') or ())}")
        if holder.get("key") == key:
            lease += " holder=self"
        leases.append(lease)
    if leases:
        parts.append("leases=[" + " | ".join(leases) + "]")
    tail = f"unread={int(self_row.get('unread') or 0)}"
    text = "; ".join(parts)
    suffix = "; " + tail
    room = max_chars - len(suffix) - 1
    if len(text) > room:
        text = text[:max(0, room)] + ("…" if room >= 0 else "")
    text += suffix
    return text[:max_chars]


class _MeshHeartbeat:
    """Refresh one mesh registration until stopped; failures are advisory."""

    def __init__(self, root: Path, harness: str, session: str,
                 on_error: Any, interval: float | None = None) -> None:
        self.root = root
        self.harness = harness
        self.session = session
        self.on_error = on_error
        self.interval = max(0.01, float(
            MESH_HEARTBEAT_S if interval is None else interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ozagia-mesh-heartbeat")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                mesh_module.heartbeat(self.root, self.harness, self.session)
            except Exception as exc:  # noqa: BLE001 - heartbeat fails open
                try:
                    self.on_error(f"mesh heartbeat failed: {exc}")
                except Exception:  # noqa: BLE001 - never kill the thread
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


def _mesh_start(source: Path, work: Path, run_root: Path,
                res: dict[str, Any]) -> tuple[_MeshHeartbeat | None, str | None]:
    """Register this runner under the source state dir and start heartbeat."""
    harness = MESH_HARNESS
    session = f"run:{run_root.name}"
    res.update(mesh_session=session, mesh_exposure=MESH_EXPOSURE)
    try:
        view = mesh_module.register(
            source, harness, session, pid=os.getpid(),
            worktree=str(work), branch=_mesh_branch(work),
            exposure=MESH_EXPOSURE, source="host")
    except Exception as exc:  # noqa: BLE001 - registration fails open
        _mesh_warn(res, f"mesh registration failed: {exc}")
        return None, None
    res.update(mesh_registered=True, mesh_id=view.get("mesh_id"),
               mesh_key=view.get("key"), mesh_exposure=view.get("exposure"))
    summary: str | None = None
    try:
        payload = mesh_module.status(source, harness=harness, session=session)
        res["mesh_peer_count"] = sum(
            1 for item in (payload.get("sessions") or ())
            if item.get("key") != view.get("key")
            and item.get("effective_state") in {"live", "stale"})
        summary = _mesh_summary_text(payload)
    except Exception as exc:  # noqa: BLE001 - summary is optional
        _mesh_warn(res, f"mesh summary unavailable: {exc}")
    beat: _MeshHeartbeat | None = None
    try:
        beat = _MeshHeartbeat(source, harness, session,
                              lambda message: _mesh_warn(res, message))
        beat.start()
    except Exception as exc:  # noqa: BLE001 - heartbeat is best effort
        _mesh_warn(res, f"mesh heartbeat unavailable: {exc}")
        beat = None
    return beat, summary


def _mesh_finish(source: Path, res: dict[str, Any],
                 heartbeat: _MeshHeartbeat | None) -> None:
    """Stop heartbeat, release this runner's leases, then unregister."""
    errors = res.setdefault("mesh_cleanup_errors", [])
    try:
        if heartbeat is not None:
            heartbeat.stop()
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort
        errors.append(f"mesh heartbeat stop failed: {exc}")
    if not res.get("mesh_registered"):
        return
    harness = MESH_HARNESS
    session = str(res.get("mesh_session") or "")
    held: list[str] = []
    try:
        records = mesh_module.lease_list(source, include_all=True).get(
            "leases") or []
        for item in records:
            holder = item.get("holder") or {}
            if (holder.get("harness"), holder.get("session")) != (harness,
                                                                   session):
                continue
            if item.get("effective_state") not in {"active", "stale"}:
                continue
            held.append(str(item.get("lease_id")))
        res["mesh_lease_ids"] = held
        for lease_id in held:
            try:
                mesh_module.lease_release(source, harness, session, lease_id)
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort
                errors.append(f"mesh lease {lease_id} release failed: {exc}")
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort
        errors.append(f"mesh lease cleanup failed: {exc}")
    try:
        mesh_module.unregister(source, harness, session, release_leases=True)
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort
        errors.append(f"mesh unregister failed: {exc}")


def _summary(res: dict[str, Any]) -> str:
    def yn(v: Any) -> str:
        return "unknown" if v is None else ("yes" if v else "no")
    prep = res.get("prep") or {}
    if prep.get("action"):
        graph = (f"{prep.get('action')} ({prep.get('nodes', '?')} nodes) in "
                 f"{res.get('build_s')} s")
    elif res.get("launch_error"):
        graph = f"not ready: {res['launch_error']}"
    else:
        graph = "not ready (the session ended first)"
    ready = res.get("ready_s")
    daemon = f"daemon ready in {ready} s" if ready is not None else "daemon not ready"
    if res.get("launch_error") and ready is None:
        daemon = f"daemon not ready ({res['launch_error']})"
    lines = [
        f"ozagia agent: {'timed out' if res.get('timed_out') else 'exit ' + str(res['exit_code'])}"
        f" after {res['seconds']} s in {res['workdir']}",
        f"  graph: {graph}; {daemon}",
        f"  plugin loaded: {yn(res.get('plugin_loaded'))}; captured calls: "
        f"{res.get('captured_calls')}; passthrough: {res.get('passthrough_calls')}; "
        f"fallbacks: {res.get('fallbacks')}",
    ]
    if res.get("repo_map_tokens") is not None:
        stats = res.get("daemon_stats") or {}

        def count(name: str) -> str:
            value = stats.get(name)
            return "unknown" if value is None else str(value)

        lines.append(
            f"  repo map: {res['repo_map_tokens']} tokens; delivered "
            f"{count('repo_map_delivered')}, skipped {count('repo_map_skipped')}")
    if prep.get("still_stale"):
        lines.append("  note: the snapshot was refreshed but HEAD moved since it was built; "
                     "`ozagia index --rebuild` updates git history")
    if res.get("mesh_registered"):
        lines.append(
            f"  mesh: {res.get('mesh_id')} {res.get('mesh_session')} "
            f"exposure={res.get('mesh_exposure')}; "
            f"peers {res.get('mesh_peer_count', '?')}; "
            f"leases {len(res.get('mesh_lease_ids') or [])}")
    elif res.get("mesh_enabled"):
        why = res.get("mesh_warning") or "not registered"
        lines.append(f"  mesh: off/unavailable ({why})")
    else:
        lines.append("  mesh: off")
    if res.get("plugin_loaded") is False:
        lines.append("  warning: the plugin never connected to the daemon; OpenCode ran "
                     "with its built-in tools only")
    if res.get("transcript_path"):
        lines.append(f"  transcript: {res['transcript_path']}")
    lines.append(f"  logs: {res['log_dir']}")
    lines.append(_tmp_line(res))
    if res.get("clean_logs"):
        if res.get("tmp_cleaned") is True:
            lines.append("  managed tmp cleaned: yes")
        elif res.get("tmp_cleaned") is False:
            why = res.get("tmp_clean_reason") or res.get("tmp_clean_error") or "kept"
            lines.append(f"  managed tmp cleaned: no ({why})")
    if res.get("clone"):
        lines.append(f"  clone kept at {res['workdir']}; remove it with: "
                     f"rm -rf {res['run_root']}")
    return "\n".join(lines)


def run_agent(workdir: str | Path, *, prompt: str | None = None, model: str | None = None,
              interactive: bool | None = None, clone: bool = False, base: str | None = None,
              keep_plugin: bool = False, opencode: str = "opencode",
              timeout_s: float | None = None, extra_args: Sequence[str] = (),
              run_python: str | None = None, tool_python: str = sys.executable,
              build: str = "auto", runs_dir: str | Path | None = None,
              quiet: bool = False, mcp_mode: str | None = None,
              progress: Progress | None = None,
              background: bool | None = None,
              repo_map_tokens: int | None = None,
              opencode_config: str | Path | None = None,
              mesh: bool | None = None,
              managed_tmp: bool = True,
              tmp_ttl_s: int | None = None,
              clean_logs: bool = False) -> dict[str, Any]:
    """Run one OpenCode session on `workdir` with the graph daemon and plugin
    (see the module docstring). Returns {exit_code, seconds, timed_out,
    transcript_path, stdout_path, stderr_path, daemon_stats, plugin_loaded,
    captured_calls, fallbacks, workdir, clone, run_root, log_dir, sock, prep,
    build_s, ready_s, interactive, ...}. `interactive` None means "no prompt".
    `mcp_mode` (coding, research, combined) also gives the session the ozagia
    MCP server in that mode, e.g. the research tools. `progress` (a
    `harness.progress.Progress`) sees the launch phases; it defaults to
    silence. `background` (default: the interactive TUI; always off for a
    non-interactive run) makes the snapshot current and starts the daemon
    while the session runs, publishing the phases for the TUI sidebar; `False`
    waits before OpenCode starts. `repo_map_tokens` (200..8000) opts into the
    bounded repo-map delivery: the OpenCode process gets
    `OZAGIA_REPO_MAP_TOKENS`, and the result and summary report the daemon's
    delivered/skipped counts. Without it no repo-map variable is passed and
    the plugin's ordinary hint is unchanged. `opencode_config` is a
    caller-supplied OpenCode config whose provider-related keys (`provider`,
    `providers`, `model`, `small_model`) are layered into the generated
    harness config; `model` (from `--model`) stays authoritative, and any
    other key, invalid JSON, or an unreadable file is an AgentError before
    OpenCode starts. The user's global config is never read implicitly.
    `managed_tmp` (default true) adopts the run root in the managed temp
    registry (`ozagia.core.temp_space`) and isolates the session's TMPDIR;
    `tmp_ttl_s` defaults to 7 days. Each of the OpenCode process, the graph
    daemon and its shell tools gets `<run root>/tmp`. A disk-backed scratch
    workspace under `<state dir>/tmp/workspaces/agent/<run>` becomes
    `OZAGIA_SCRATCH_ROOT`. Registration is best effort: a failure records a
    warning and the session runs normally. With an explicit `runs_dir` the
    root is not registered, the source state dir is not created for it, and
    scratch stays under `<run root>/scratch`. `clean_logs` (default off)
    removes an owned run root and scratch only after a successful run; a kept
    clone, a failure or a timeout always keeps them.

    `mesh` is the layer 0-1 session-mesh opt-in: `None` follows `[mesh]
    enabled` or `OZAGIA_MESH`, `True`/`False` (from `--mesh`/`--no-mesh`) win.
    When on, the harness registers `opencode-runner` / `run:<run root name>`
    under the source repository's state directory with exposure `read`,
    heartbeats while the session runs, releases its leases and unregisters on
    every exit path. Registration and heartbeat fail open: a mesh problem is
    reported in `mesh_warning` and never aborts the run. A registered run
    passes a bounded metadata summary (`OZAGIA_MESH_SUMMARY`) to the OpenCode
    process only. The result carries `mesh_enabled`, `mesh_id`,
    `mesh_session`, `mesh_key`, `mesh_exposure`, `mesh_registered`,
    `mesh_peer_count`, `mesh_lease_ids`, `mesh_warning` and
    `mesh_cleanup_errors`."""
    if repo_map_tokens is not None:
        repo_map_tokens = validate_repo_map_tokens(repo_map_tokens)
    provider_config = (load_provider_config(opencode_config)
                       if opencode_config is not None else None)
    if provider_config is not None and model is not None:
        # --model wins: do not leave a conflicting model in the TUI config,
        # and `opencode run` gets -m instead.
        provider_config.pop("model", None)
    exe = find_opencode(opencode)
    progress = progress if progress is not None else NULL_PROGRESS
    if mcp_mode is not None and mcp_mode not in MCP_MODES:
        raise AgentError(f"MCP mode must be one of {', '.join(MCP_MODES)}, got {mcp_mode!r}")
    if interactive is None:
        interactive = prompt is None
    if background is None:
        background = interactive
    if not interactive:
        background = False      # `opencode run` needs the daemon up front
    if not interactive and not prompt:
        raise AgentError("a non-interactive run needs a prompt")
    if base is not None and not clone:
        raise AgentError("base needs clone=True (--base needs --clone)")
    source = Path(workdir).resolve()
    if not source.is_dir():
        raise AgentError(f"not a directory: {source}")
    explicit_runs = runs_dir is not None
    managed = bool(managed_tmp and not explicit_runs)
    if managed and tmp_ttl_s is None:
        tmp_ttl_s = DEFAULT_TMP_TTL_S
    runs = Path(runs_dir) if runs_dir else Path(tempfile.gettempdir()) / RUNS_DIRNAME
    run_root = ea.new_run_root(runs)
    log_dir = run_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    status_path = log_dir / "launch_status.json" if background else None
    work = source
    res: dict[str, Any] = {"exit_code": None, "seconds": 0.0, "timed_out": False,
                           "transcript_path": None, "stdout_path": None,
                           "stderr_path": None, "daemon_stats": None,
                           "plugin_loaded": None, "captured_calls": None,
                           "passthrough_calls": None, "fallbacks": None, "workdir": str(work), "source": str(source),
                           "clone": clone, "base": None, "run_root": str(run_root),
                           "log_dir": str(log_dir), "interactive": interactive,
                           "background": background, "launch_status": (
                               str(status_path) if status_path is not None else None),
                           "sock": None, "prep": None, "build_s": None, "ready_s": None,
                           "launch_error": None, "repo_map_tokens": repo_map_tokens,
                           "exclude": None, "plugin_path": None, "daemon_stop_error": None,
                           "managed_tmp": managed, "runs_dir_explicit": explicit_runs,
                           "clean_logs": clean_logs, "tmp_id": None, "tmp_path": None,
                           "tmp_registered": False, "tmp_ttl_s": tmp_ttl_s if managed else None,
                           "tmp_expires_at": None, "tmp_bytes": None, "tmp_files": None,
                           "tmpfs": None, "scratch_root": None, "tmp_cleaned": None,
                           "tmp_leased": False, "tmp_warning": None,
                           "mesh_enabled": False, "mesh_id": None,
                           "mesh_session": None, "mesh_key": None,
                           "mesh_exposure": None, "mesh_registered": False,
                           "mesh_peer_count": None, "mesh_lease_ids": [],
                           "mesh_warning": None, "mesh_cleanup_errors": []}
    mesh_on = False
    if mesh is None:
        try:
            mesh_on = mesh_module.mesh_enabled(source)
        except Exception as exc:  # noqa: BLE001 - opt-in fails open
            _mesh_warn(res, f"mesh opt-in failed: {exc}")
    else:
        mesh_on = bool(mesh)
    res["mesh_enabled"] = mesh_on
    scratch_id: str | None = None
    tmp_env: dict[str, str] = {}
    lease: Any = None
    if managed:
        entry, warning = _register_run_root(source, run_root, model=model,
                                            ttl_s=tmp_ttl_s)
        if warning:
            res["tmp_warning"] = warning
        if entry is not None:
            res.update(tmp_id=entry["id"], tmp_path=entry["path"],
                       tmp_registered=True, tmp_expires_at=entry.get("expires_at"),
                       tmpfs=entry.get("tmpfs"))
            try:
                lease = TS.acquire_lease(source, entry["id"])
                res["tmp_leased"] = True
            except Exception as exc:  # noqa: BLE001 - leasing is best effort
                res["tmp_lease_warning"] = f"managed tmp lease failed: {exc}"
        tmp_env = _tmp_env(run_root)
        scratch, scratch_id, warning = _register_scratch(source, run_root,
                                                         ttl_s=tmp_ttl_s)
        if warning:
            res.setdefault("tmp_warnings", []).append(warning)
        if scratch is not None:
            res["scratch_root"] = str(scratch)
    else:
        scratch = run_root / "scratch"
        try:
            scratch.mkdir(parents=True, exist_ok=True)
            res["scratch_root"] = str(scratch)
        except OSError:
            pass
    with _sigterm_as_interrupt(), _lease_guard(lease):
        if clone:
            rev = f"{base or 'HEAD'}^{{commit}}"
            found = _git(["rev-parse", "--verify", "--quiet", rev], source)
            if found.returncode != 0:
                raise AgentError(f"{base or 'HEAD'!r} is not a commit in {source}")
            res["base"] = found.stdout.strip()
            try:
                work = ea.make_workdir(source, res["base"], run_root, "clone",
                                       isolation="clone")
            except ea.HarnessError as exc:
                raise AgentError(str(exc)) from exc
            res["workdir"] = str(work)
        mesh_beat: _MeshHeartbeat | None = None
        mesh_summary: str | None = None
        try:
            if mesh_on:
                mesh_beat, mesh_summary = _mesh_start(source, work, run_root, res)
            res["exclude"] = git_exclude(work)
            config = write_opencode_config(
                log_dir / "opencode_config.json",
                model=model if interactive else None,
                provider_config=provider_config,
                mcp=(mcp_server_entry(work, mcp_mode, tool_python=tool_python)
                     if mcp_mode else None))
            session = GraphSession(work, tool_python=tool_python,
                                   run_python=run_python, build=build,
                                   log_dir=log_dir, config=config_path(source),
                                   progress=progress, background=background,
                                   status_path=status_path)
            if tmp_env:
                session.env.update(tmp_env)
            try:
                with session:
                    res.update(sock=str(session.sock) if session.sock is not None else None,
                               daemon_pid=session.pid)
                    if background:
                        progress.note("opencode: starting the TUI "
                                      "(the graph builds in the background)")
                    else:
                        progress.note("opencode: starting the TUI" if interactive
                                      else "opencode: starting")
                    res.update(_with_plugin(
                        exe, work, session, prompt=prompt, model=model,
                        interactive=interactive, keep_plugin=keep_plugin,
                        timeout_s=timeout_s, extra_args=extra_args,
                        config=config, log_dir=log_dir,
                        repo_map_tokens=repo_map_tokens,
                        tmp_env=tmp_env,
                        scratch_root=res.get("scratch_root"),
                        mesh_summary=mesh_summary,
                        mesh_session=(res.get("mesh_session")
                                      if mesh_summary else None)))
            finally:
                if not keep_plugin:
                    with no_interrupt():
                        res["exclude_removed"] = git_unexclude(res["exclude"])
                res["daemon_stats"] = session.final_stats
                res["daemon_stop_error"] = session.stop_error
                res.update(ea.graph_stats_fields(session.final_stats))
                res.update(prep=session.prep, build_s=session.build_s,
                           ready_s=session.ready_s, launch_error=session.error)
                if clone and not quiet and sys.exc_info()[0] is not None:
                    print(f"ozagia agent: clone kept at {work}; remove it with: "
                          f"rm -rf {run_root}", file=sys.stderr)
        finally:
            _mesh_finish(source, res, mesh_beat)
    _refresh_tmp_usage(source, res)
    if clean_logs:
        _clean_run_tmp(source, res, managed=managed, scratch_id=scratch_id)
    if not quiet:
        print(_summary(res))
    return res


def _with_plugin(exe: str, work: Path, session: GraphSession, *, prompt: str | None,
                 model: str | None, interactive: bool, keep_plugin: bool,
                 timeout_s: float | None, extra_args: Sequence[str], config: Path,
                 log_dir: Path, repo_map_tokens: int | None = None,
                 tmp_env: dict[str, str] | None = None,
                 scratch_root: str | None = None,
                 mesh_summary: str | None = None,
                 mesh_session: str | None = None) -> dict[str, Any]:
    """Install the plugin, run OpenCode, remove the plugin (in `finally`)."""
    path = plugin.plugin_path(work)
    existed = path.exists()
    # A file with exactly our source is ours, left by a run that was killed
    # before its teardown: it is removed after this run like a new one.
    if existed and not _tracked(work, plugin.PLUGIN_REL):
        try:
            if path.read_text(encoding="utf-8") == plugin.plugin_source():
                existed = False
        except OSError:
            pass
    out: dict[str, Any] = {"plugin_path": str(path)}
    try:
        if not (existed and _tracked(work, plugin.PLUGIN_REL)):
            plugin.install(work)
        # OpenCode gets this process's environment; PATH as the session's
        # (the python shim first, when run_python is set).
        env = dict(os.environ, PATH=session.env.get("PATH", os.environ.get("PATH", "")))
        env.update({ENV_SOCK: str(session.sock), "PWD": str(work),
                    "OPENCODE_CONFIG": str(config),
                    "OZAGIA_GIT_REFS_PASSTHROUGH": "1"})
        if tmp_env:
            env.update(tmp_env)
        if scratch_root:
            env[SCRATCH_ENV] = scratch_root
        if repo_map_tokens is None:
            # Off by default even when the caller's environment sets it.
            env.pop(REPO_MAP_ENV, None)
        else:
            env[REPO_MAP_ENV] = str(repo_map_tokens)
        if mesh_summary:
            # Registered only: a bounded metadata summary and the identity the
            # read-only MCP mesh tools may act for. A missing registration
            # leaves the inherited variables out entirely.
            env[MESH_SUMMARY_ENV] = mesh_summary
            env["OZAGIA_MESH_HARNESS"] = MESH_HARNESS
            if mesh_session:
                env["OZAGIA_MESH_SESSION"] = mesh_session
        else:
            for name in (MESH_SUMMARY_ENV, "OZAGIA_MESH_HARNESS",
                         "OZAGIA_MESH_SESSION"):
                env.pop(name, None)
        if session.status_path is not None:
            env[launch_status.ENV_LAUNCH_STATUS] = str(session.status_path)
        if interactive:
            argv = [exe, *TUI_ARGV, *extra_args, *(["--prompt", prompt] if prompt else []),
                    str(work)]
            ran = _run_tui(argv, cwd=work, env=env, timeout_s=timeout_s)
        else:
            # No prompt argument: `opencode run` reads the message from stdin.
            argv = [exe, *RUN_ARGV, *(["-m", model] if model else []), *extra_args]
            stdout_p = log_dir / "opencode.stdout.jsonl"
            stderr_p = log_dir / "opencode.stderr.txt"
            with _prompt_stdin(prompt) as prompt_in, \
                    stdout_p.open("w", encoding="utf-8") as so, \
                    stderr_p.open("w", encoding="utf-8") as se:
                ran = run_group(argv, cwd=work, env=env, timeout_s=timeout_s,
                                stdout=so, stderr=se, stdin=prompt_in)
            parsed = ea._parse_opencode_events(
                stdout_p.read_text(encoding="utf-8", errors="replace"))
            transcript = log_dir / "transcript.txt"
            transcript.write_text(parsed.get("transcript") or "", encoding="utf-8")
            out.update(stdout_path=str(stdout_p), stderr_path=str(stderr_p),
                       transcript_path=str(transcript),
                       tokens_in=parsed.get("tokens_in"), tokens_out=parsed.get("tokens_out"),
                       cost_usd=parsed.get("cost_usd"), tool_calls=parsed.get("tool_calls"),
                       tools_by_name=parsed.get("tools_by_name"),
                       driver_error=parsed.get("driver_error"))
        out.update(exit_code=ran["returncode"], seconds=ran["seconds"],
                   timed_out=ran["timed_out"], argv=argv)
        return out
    finally:
        if not keep_plugin and not existed:
            with no_interrupt():
                plugin.uninstall(work)


__all__ = ["AgentError", "EXCLUDE_PATTERNS", "MESH_HARNESS", "MESH_SUMMARY_ENV",
           "OPENCODE_CONFIG_KEYS", "REPO_MAP_DEFAULT_TOKENS", "REPO_MAP_ENV",
           "REPO_MAP_MAX_TOKENS", "REPO_MAP_MIN_TOKENS", "RUN_ARGV", "TUI_ARGV",
           "find_opencode", "git_exclude", "git_unexclude",
           "load_provider_config", "run_agent", "validate_repo_map_tokens",
           "write_opencode_config"]
