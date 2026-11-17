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
7. Runs OpenCode in the workdir with `OZAGIA_DAEMON_SOCK`, `PWD` (OpenCode
   takes its project directory from it), `OPENCODE_CONFIG`, and with
   `run_python`, a PATH whose first entry is a `python` shim.
   - Prompt given: `opencode run --standalone --format json --auto --archived
     [-m model] [extra args] <prompt>`, its own process group, stdout and
     stderr to files in the logs, killed after `timeout_s`.
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
                          mcp: dict[str, Any] | None = None) -> Path:
    """The OPENCODE_CONFIG for one harness session, and optional MCP server."""
    doc: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
    }
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
    if prep.get("still_stale"):
        lines.append("  note: the snapshot was refreshed but HEAD moved since it was built; "
                     "`ozagia index --rebuild` updates git history")
    if res.get("plugin_loaded") is False:
        lines.append("  warning: the plugin never connected to the daemon; OpenCode ran "
                     "with its built-in tools only")
    if res.get("transcript_path"):
        lines.append(f"  transcript: {res['transcript_path']}")
    lines.append(f"  logs: {res['log_dir']}")
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
              background: bool | None = None) -> dict[str, Any]:
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
    waits before OpenCode starts."""
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
                           "launch_error": None,
                           "exclude": None, "plugin_path": None, "daemon_stop_error": None}
    with _sigterm_as_interrupt():
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
        res["exclude"] = git_exclude(work)
        config = write_opencode_config(
            log_dir / "opencode_config.json", model=model if interactive else None,
            mcp=(mcp_server_entry(work, mcp_mode, tool_python=tool_python)
                 if mcp_mode else None))
        session = GraphSession(work, tool_python=tool_python, run_python=run_python,
                               build=build, log_dir=log_dir, config=config_path(source),
                               progress=progress, background=background,
                               status_path=status_path)
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
                res.update(_with_plugin(exe, work, session, prompt=prompt, model=model,
                                        interactive=interactive, keep_plugin=keep_plugin,
                                        timeout_s=timeout_s, extra_args=extra_args,
                                        config=config, log_dir=log_dir))
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
                print(f"ozagia agent: clone kept at {work}; remove it with: rm -rf {run_root}",
                      file=sys.stderr)
    if not quiet:
        print(_summary(res))
    return res


def _with_plugin(exe: str, work: Path, session: GraphSession, *, prompt: str | None,
                 model: str | None, interactive: bool, keep_plugin: bool,
                 timeout_s: float | None, extra_args: Sequence[str], config: Path,
                 log_dir: Path) -> dict[str, Any]:
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
        if session.status_path is not None:
            env[launch_status.ENV_LAUNCH_STATUS] = str(session.status_path)
        if interactive:
            argv = [exe, *TUI_ARGV, *extra_args, *(["--prompt", prompt] if prompt else []),
                    str(work)]
            ran = _run_tui(argv, cwd=work, env=env, timeout_s=timeout_s)
        else:
            argv = [exe, *RUN_ARGV, *(["-m", model] if model else []), *extra_args, prompt]
            stdout_p = log_dir / "opencode.stdout.jsonl"
            stderr_p = log_dir / "opencode.stderr.txt"
            with stdout_p.open("w", encoding="utf-8") as so, \
                    stderr_p.open("w", encoding="utf-8") as se:
                ran = run_group(argv, cwd=work, env=env, timeout_s=timeout_s,
                                stdout=so, stderr=se)
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


__all__ = ["AgentError", "EXCLUDE_PATTERNS", "RUN_ARGV", "TUI_ARGV", "find_opencode",
           "git_exclude", "git_unexclude", "run_agent", "write_opencode_config"]
