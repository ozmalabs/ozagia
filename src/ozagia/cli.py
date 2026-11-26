"""The ozagia command: the graph, the daemon, the integrations and the agent.

    ozagia doctor [--root R] [--json]
    ozagia index [--root R] [--rebuild]
    ozagia agent [--workdir W] [--prompt TEXT | --prompt-file F] [--model M]
                 [--mcp coding|research|combined | --research]
                 [--opencode-config PATH]
                 [--repo-map [--repo-map-tokens N]]
                 [--clone [--base REV]] [--keep-plugin] [--timeout S]
                 [--python P] [--runs-dir D] [-- <extra opencode args>]
    ozagia daemon serve|ensure|ping|stats|shutdown ... (python -m ozagia.daemon)
    ozagia bg run|wait|list|log|stop ...          (ozagia.daemon.jobcli)
    ozagia mcp ...                                (ozagia.integrations.mcp.server)
    ozagia eval ...                               (python -m ozagia.eval.agent)
    ozagia profile ingest|compare|formats ...     (offline profiler/telemetry
                                                   artifacts joined to the graph)
    ozagia check ...                              (python -m ozagia.check)
    ozagia install opencode|claude|codex|kiro [--workdir W] [...]
    ozagia self info|check|upgrade|uninstall ... (ozagia.selfinstall)
    ozagia [DIR]                                  the TUI on DIR (default: the
                                                  repository of the cwd), as
                                                  `ozagia agent --workdir DIR`
    ozagia job wait ...                           (explicit live job polling)
    ozagia ops record|resume ...                  (append-only ops receipts)
    ozagia live import|diff|save ...              (supplied snapshots only)
    ozagia iac check ...                          (offline Terraform checks)
    ozagia env [--root R] [--for PATH ...] [--json] [--no-cache]
                                                  (Python interpreters and what
                                                  they can import,
                                                  docs/environments.md)
    ozagia env list|show ...                      (declarative environment profiles)
    ozagia relay plan|run|tunnel ...              (nested command/network relays)
    ozagia runtime status|list ...                (configured services and tunnels)
    ozagia service start|run|stop|restart|status|logs NAME
    ozagia process start|run -- ARGV ...          (explicit local processes)
    ozagia tunnel start|stop|status|list ...      (tunnel config vs live state)
    ozagia preflight plan ...                     (dry-run emulator plan)
    ozagia config schema|get|values|set|migrate ... (settings registry and
                                                   .ozagia.toml migration; see
                                                   docs/settings.md)
    ozagia hooks install|uninstall|status ...      (opt-in commit/push hooks;
                                                   docs/worktree.md)
    ozagia who PATH [--root R] [--json]           (sessions that edited PATH)
    ozagia goal add|list|show|check|claim|drop|block|from-review ...
                                                  (graph-addressed goals;
                                                  ozagia.goals_cli)
    ozagia replace PATTERN REPLACEMENT [--glob G ...] [--key K ...] [--regex]
                   [--flags F] [--expect N] [--apply --plan-id ID] ...
                                                  (one pattern across files;
                                                  preview unless --apply)
    ozagia review-repo [--root R] [--json] [--out F] [--section S ...]
                       [--compare PACKET] [--fail-on blocking|major|never]
                       [--index] [--budget-s S] [--save] [--model-pass]
                                                  (deterministic whole-repo
                                                  review, docs/repo_review.md)
    ozagia mesh status|peers|register|send|inbox|lease|worktree|merge ...
                                                  (local session peers,
                                                  messages and advisory leases;
                                                  docs/session_mesh.md)
    ozagia capabilities list|show|audit [--json] [--limit N] [--cursor C]
                                                  (read-only declared
                                                  capabilities and observed
                                                  surfaces, docs/capabilities.md)
    ozagia engine serve|token [--root R] [--host H] [--port P] [--allow-remote]
                                                  (local read-only graph HTTP
                                                  transport; docs/graph_engine.md)
    ozagia task-preflight GOAL [--json] [--limit N] [--cursor C] [--root R]
                               [--checkout DIR ...] [--scope ENTRY ...]
                               [--base REV] [--timeout-s S]
                                                  (read-only goal/task
                                                  preflight; docs/task_preflight.md)
    ozagia overview|shape [--root R] [--json | --markdown] [--out PATH]
                    [--budget-s S] [--max-modules N] [--check PATH]
                                                  (one deterministic reference
                                                  page; docs/repo_shape.md)
    ozagia --version

`daemon`, `bg`, `mcp`, `eval` and `check` pass every argument after their name to
those modules' `main`, unchanged. See docs/harness.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

PROG = "ozagia"
DEEP_REVIEW_PROFILES = (
    "full", "security", "performance", "failure-modes", "data-structures",
    "scalability", "architecture", "tests", "dependencies", "operations",
    "docs", "change", "review-change",
)
DEEP_REVIEW_CHANGE_PROFILES = frozenset({"change", "review-change"})
DEEP_REVIEW_CHANGE_DEFAULT_RANGE = "HEAD~1..HEAD"
DEEP_REVIEW_CHANGE_MAX_FINDINGS = 20
_RETITLE_ENV = "_OZAGIA_RETITLED"
_PYTHON_ENV = "_OZAGIA_PYTHON"


def _retitled_argv(argv: list[str],
                   exe: str | None = None) -> list[str] | None:
    """`argv` with argv[0] renamed to `ozagia`, or None if this is not the CLI.

    The setuptools entry point and the installer's `python -I -m ozagia`
    launcher both run the interpreter first, so `/proc/<pid>/cmdline` starts
    with "python". tmux names a window after argv[0] of the pane's foreground
    process group, so a plain `ozagia` shows up as "python". Re-exec with
    argv[0]="<venv>/bin/ozagia" (setproctitle would add a C dependency to the
    core). That path, not a bare "ozagia", keeps CPython's venv detection:
    argv[0] is what `sys.executable` and `sys.prefix` are derived from.
    """
    if not argv:
        return None
    if exe is None:
        exe = sys.executable
    name = Path(exe).with_name(PROG)
    if Path(argv[0]) in (Path(PROG), name):
        return None
    entry = Path(argv[1]).name if len(argv) > 1 else ""
    module = any(argv[i] in ("-m", "--module")
                 and argv[i + 1] in ("ozagia", "ozagia.cli")
                 for i in range(len(argv) - 1))
    if entry != PROG and not module:
        return None
    return [str(name), *argv[1:]]


def _retitle_process() -> None:
    """Re-exec once so tmux and ps name the process `ozagia`, not `python`."""
    if not sys.platform.startswith("linux"):
        return
    if os.environ.pop(_RETITLE_ENV, None) == "1":
        # the re-exec'd process: restore the interpreter path CPython lost
        # when argv[0] stopped being the python binary
        original = os.environ.pop(_PYTHON_ENV, None)
        if original:
            sys.executable = original
        return
    try:
        raw = Path("/proc/self/cmdline").read_bytes()
    except OSError:
        return
    argv = [part.decode("utf-8", "surrogateescape")
            for part in raw.split(b"\0") if part]
    new = _retitled_argv(argv)
    if new is None:
        return
    os.environ[_RETITLE_ENV] = "1"
    os.environ[_PYTHON_ENV] = sys.executable
    try:
        os.execv(sys.executable, new)
    except OSError:
        os.environ.pop(_RETITLE_ENV, None)
        os.environ.pop(_PYTHON_ENV, None)


def _version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
        return version("ozagia")
    except (ImportError, PackageNotFoundError):  # pragma: no cover - not installed
        return "unknown"


def _daemon_main(argv: list[str]) -> int:
    from ozagia.daemon.__main__ import main
    return main(argv)


def _bg_main(argv: list[str]) -> int:
    from ozagia.daemon.jobcli import main
    return main(argv)


def _mcp_main(argv: list[str]) -> int:
    from ozagia.integrations.mcp.server import main
    return main(argv) or 0


def _eval_main(argv: list[str]) -> int:
    from ozagia.eval.agent import main
    return main(argv)


def _check_main(argv: list[str]) -> int:
    from ozagia.check import main
    return main(argv)


def _self_main(argv: list[str]) -> int:
    from ozagia.selfinstall import main
    return main(argv)


def _settings_main(argv: list[str]) -> int:
    from ozagia.settings_cli import main
    return main(argv, prog=f"{PROG} config")


def _hooks_main(argv: list[str]) -> int:
    from ozagia.hooks_cli import main
    return main(argv, prog=f"{PROG} hooks")


def _federation_main(argv: list[str]) -> int:
    from ozagia.federation_cli import main
    return main(argv, prog=f"{PROG} federation")


def _profile_main(argv: list[str]) -> int:
    from ozagia.profile_cli import main
    return main(argv, prog=f"{PROG} profile")


def _profile_run_main(argv: list[str]) -> int:
    from ozagia.profile_cli import main
    return main(["run", *argv], prog=f"{PROG} profile-run")


# name -> (main(argv) -> int, help). Looked up at call time so tests can patch.
DELEGATES: dict[str, tuple[str, str]] = {
    "daemon": ("_daemon_main", "the graph daemon: serve|ensure|ping|stats|shutdown "
                               "(python -m ozagia.daemon)"),
    "bg": ("_bg_main", "background jobs in the daemon: run|wait|list|log|stop "
                       "(ozagia.daemon.jobcli)"),
    "mcp": ("_mcp_main", "the MCP server (ozagia.integrations.mcp.server)"),
    "eval": ("_eval_main", "the break/fix agent eval (python -m ozagia.eval.agent)"),
    "check": ("_check_main", "run tests, report graph keys (python -m ozagia.check)"),
    "self": ("_self_main", "the installed ozagia: info|check|upgrade|uninstall"),
    "config": ("_settings_main", "inspect settings, preview config writes and "
                                 "migrate .ozagia.toml "
                                 "(schema|get|values|set|migrate)"),
    "hooks": ("_hooks_main", "opt-in Git commit/push hooks "
                             "(install|uninstall|status)"),
    "federation": ("_federation_main", "read-only federated graph queries "
                                      "(status|search|node)"),
    "profile": ("_profile_main", "ingest profiler/telemetry artifacts and join "
                                 "them to the graph (ingest|compare|formats)"),
}


def _delegate(name: str) -> Callable[[list[str]], int]:
    return globals()[DELEGATES[name][0]]


def _root(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    from ozagia.config import repo_root
    return repo_root()


def _ensure_language_support(root: Path) -> None:
    """Report and install support for the languages present under `root`.

    Best effort: a failed probe or install never blocks the command. Disable
    with ``OZAGIA_LANGUAGE_AUTOINSTALL=0``.
    """
    from ozagia.languages import AUTO_INSTALL_ENV, ensure_for_root
    if os.environ.get(AUTO_INSTALL_ENV, "1").strip().lower() in (
            "0", "false", "no"):
        return
    try:
        ensure_for_root(
            root, install=True,
            report=lambda line: print(line, file=sys.stderr, flush=True))
    except Exception as exc:  # an install probe must not break indexing
        print(f"[ozagia] languages: check failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)



def _config_summary(config: dict[str, Any]) -> str:
    """One text line naming both config files, precedence and unmapped count.

    The JSON report carries the detail and the unmapped key reasons; the text
    line names paths and counts only, never values.
    """
    state = "valid" if config.get("valid") else "invalid"
    legacy = config.get("legacy_path") or "none"
    repo = config.get("repo_path") or "none"
    if config.get("repo_path") and not config.get("repo_exists"):
        repo += " (missing)"
    active = config.get("active_source") or "defaults"
    precedence = " > ".join(config.get("precedence") or []) or "defaults"
    count = int(config.get("unmapped_count") or 0)
    shown = len(config.get("unmapped") or [])
    unmapped = (f"{shown} of {count}…" if config.get("unmapped_truncated")
                else str(count))
    return (f"Config: {state} (legacy={legacy}; repo={repo}; "
            f"active={active}; precedence={precedence}; "
            f"unmapped={unmapped})")


def _cmd_doctor(args: argparse.Namespace) -> int:
    from ozagia.doctor import diagnose
    report = diagnose(_root(args.root))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Repository: {report['root']}")
        print(f"Snapshot: {report['snapshot']['state']} ({report['snapshot']['path']})")
        config = report["config"]
        print(_config_summary(config))
        settings = report.get("settings") or {}
        if settings:
            if settings.get("valid"):
                counts = ", ".join(
                    f"{name}={count}" for name, count
                    in (settings.get("source_counts") or {}).items())
                print(f"Settings: valid ({settings.get('keys', 0)} keys"
                      + (f"; sources: {counts}" if counts else "") + ")")
            else:
                print(f"Settings: invalid ({settings.get('error')})")
        for language, status in report["parsers"].items():
            if not status["available"]:
                print(f"Parser {language}: unavailable ({status['error']})")
        print(report["language_server_note"])
        integrations = report.get("integrations") or {}
        for key, item in (integrations.get("hosts") or {}).items():
            version = item.get("version") or (
                "unknown" if item.get("installed") else "")
            suffix = f" {version}" if version else ""
            location = item.get("path") or item.get("command")
            print(f"Integration {key}: {item.get('state')}{suffix} "
                  f"({location}); tested: {item.get('tested_range')}")
        for key, item in (integrations.get("config") or {}).items():
            if not isinstance(item, dict):
                continue
            paths = "; ".join(item.get("found") or []) or "none"
            print(f"Integration config {key}: {item.get('state')} ({paths})")
        capabilities = report.get("capabilities") or {}
        if capabilities:
            catalogue = capabilities.get("catalogue") or {}
            counts = (capabilities.get("audit") or {}).get("counts") or {}
            print(f"Capabilities: {capabilities.get('state')} "
                  f"catalogue={catalogue.get('version')} "
                  f"capabilities={counts.get('capabilities')} "
                  f"surfaces={counts.get('declared_surfaces')} "
                  f"findings={counts.get('findings')} "
                  f"hash={catalogue.get('hash')}")
        for warning in report.get("warnings") or []:
            print(f"Warning: {warning.get('component')}: "
                  f"{warning.get('message')}")
        for name, status in report.get("external_tools", {}).items():
            state = status["path"] if status["available"] else "unavailable"
            print(f"Relay tool {name}: {state}")
        if report.get("external_tool_note"):
            print(report["external_tool_note"])
    return 0 if report["root_exists"] and report["config"]["valid"] else 2

def _cmd_index(args: argparse.Namespace) -> int:
    from ozagia.harness.session import SessionError, prepare_snapshot, tool_env
    from ozagia.config import config_path, state_path
    root = _root(args.root)
    _ensure_language_support(root)
    dump = Path(args.dump).resolve() if args.dump else state_path(root, "graph.json")
    env = tool_env(root, tool_python=sys.executable, config=config_path(root))
    try:
        rep = prepare_snapshot(root, dump, build="always" if args.rebuild else "auto",
                               env=env)
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0
    line = (f"{rep['action']} {rep['dump']}: {rep.get('nodes')} nodes, "
            f"{rep.get('edges')} edges in {rep['seconds']} s")
    if rep["action"] == "refreshed":
        line += (f" ({rep['files']} files reingested; +{rep['added']} "
                 f"-{rep['removed']} ~{rep['changed']} nodes)")
    print(line)
    if rep.get("still_stale"):
        print("note: HEAD moved since the snapshot was built; "
              "`ozagia index --rebuild` updates git history")
    return 0


def _cmd_agent(args: argparse.Namespace) -> int:
    from ozagia.harness.opencode import AgentError, run_agent
    from ozagia.harness.progress import Spinner
    from ozagia.harness.session import SessionError
    prompt = args.prompt
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read --prompt-file: {exc}", file=sys.stderr)
            return 2
    _ensure_language_support(_root(args.workdir))
    # The snapshot build/refresh and the daemon start take seconds on a large
    # repository; report them before the TUI takes the terminal.
    progress = Spinner()
    try:
        res = run_agent(_root(args.workdir), prompt=prompt, model=args.model,
                        clone=args.clone, base=args.base, keep_plugin=args.keep_plugin,
                        timeout_s=args.timeout, extra_args=args.extra,
                        run_python=args.python, opencode=_opencode_exe(args.opencode),
                        runs_dir=args.runs_dir, mcp_mode=args.mcp, progress=progress,
                        repo_map_tokens=args.repo_map_tokens,
                        opencode_config=args.opencode_config,
                        managed_tmp=args.managed_tmp, tmp_ttl_s=args.tmp_ttl,
                        clean_logs=args.clean_logs)
    except (AgentError, SessionError) as exc:
        progress.close()
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        progress.close()
        print("ozagia agent: interrupted; daemon stopped and plugin removed",
              file=sys.stderr)
        return 130
    finally:
        progress.close()
    if res.get("timed_out"):
        return 124
    code = res.get("exit_code")
    return code if isinstance(code, int) and code >= 0 else 1


# install target -> the integration module whose `__main__.main` installs it.
INSTALL_TARGETS: dict[str, str] = {
    "opencode": "ozagia.integrations.opencode",
    "claude": "ozagia.integrations.claude_code",
    "codex": "ozagia.integrations.codex",
    "kiro": "ozagia.integrations.kiro",
}


def _cmd_install(args: argparse.Namespace, extra: list[str]) -> int:
    import importlib
    main = importlib.import_module(INSTALL_TARGETS[args.target] + ".__main__").main
    return main(["install", "--workdir", args.workdir, *extra])


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _payload(value: str | None, *, name: str,
             file: str | None = None) -> str:
    if file:
        try:
            return Path(file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read --{name}-file: {exc}") from exc
    if value is None:
        raise ValueError(f"--{name} or --{name}-file is required")
    if value == "-":
        return sys.stdin.read()
    return value


def _cmd_job(args: argparse.Namespace) -> int:
    from dataclasses import asdict
    import subprocess

    from ozagia.jobs import JobPolicy, JobSpec, wait_for_job

    spec = JobSpec(
        provider=args.provider,
        kind={"ssm": "command_invocation", "codebuild": "build",
              "codepipeline": "execution",
              "kubectl": "rollout_status"}[args.provider],
        job_id=args.job_id,
        resource=args.resource,
        instance_id=args.instance_id,
        region=args.region,
        namespace=args.namespace,
    )
    executable = args.executable or (
        "kubectl" if args.provider == "kubectl" else "aws")
    policy = JobPolicy(
        allowed_providers=frozenset({args.provider}) if args.allow_live
        else frozenset(),
        executables={args.provider: executable} if args.allow_live else {},
        max_timeout_s=args.timeout_s if args.allow_live else 0.0,
    )

    def runner(argv: list[str], timeout_s: float):
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=max(timeout_s, 0.1),
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    try:
        result = wait_for_job(
            spec, runner=runner, policy=policy, timeout_s=args.timeout_s,
            poll_interval_s=args.poll_s, max_output_lines=args.max_lines,
        )
    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload = asdict(result)
    payload["live"] = bool(args.allow_live and result.status != "disabled")
    if args.json:
        _print_json(payload)
    else:
        print(f"{result.status}: {result.terminal_phase or 'no terminal phase'} "
              f"after {result.elapsed_s:.1f}s ({result.attempts} attempts)")
        for line in result.last_log_lines:
            print(f"  {line}")
        if result.status == "disabled":
            print("use --allow-live with a named region/namespace to poll")
    return 0 if result.status in {"succeeded", "disabled"} else 1


def _cmd_ops(args: argparse.Namespace) -> int:
    from ozagia.config import state_path
    from ozagia.core.ops import OpsValidationError, append_record, resume_summary

    directory = state_path(_root(args.root), "ops")
    if args.action == "record":
        try:
            text = _payload(args.record, name="record", file=args.record_file)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        try:
            record = json.loads(text)
        except (TypeError, ValueError):
            print("error: record is not valid JSON", file=sys.stderr)
            return 2
        try:
            saved = append_record(directory, record)
        except (OpsValidationError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            _print_json(saved)
        else:
            print(f"recorded {saved['kind']} for {saved['environment']} "
                  f"at {saved['timestamp']}")
        return 0
    try:
        summary = resume_summary(directory, limit=args.limit,
                                 environment=args.environment)
    except (ValueError, TypeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        _print_json(summary)
    else:
        latest = summary.get("latest_state")
        if latest:
            print(f"latest: {latest['timestamp']} {latest['environment']} "
                  f"{latest['kind']}: {latest['summary']}")
        for item in summary.get("open_items", []):
            print(f"next: {item}")
    return 0


def _cmd_live(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from ozagia.config import state_path
    from ozagia.core.live import (
        SnapshotError, diff_snapshots, import_snapshot, snapshot_to_dict,
        write_snapshot,
    )

    try:
        before = import_snapshot(
            _payload(args.before, name="before", file=args.before_file),
            provenance=args.provenance)
    except (SnapshotError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.action == "import":
        result = {"ok": True, "snapshot": snapshot_to_dict(before)}
    elif args.action == "save":
        try:
            path = write_snapshot(state_path(_root(args.root), "live"), before)
        except (SnapshotError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        result = {"ok": True, "path": str(path), "snapshot": snapshot_to_dict(before)}
    else:
        try:
            after = import_snapshot(
                _payload(args.after, name="after", file=args.after_file),
                provenance=args.provenance)
        except (SnapshotError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        result = {"ok": True,
                  "differences": [asdict(item) for item in
                                  diff_snapshots(before, after)]}
    if args.json:
        _print_json(result)
    else:
        if args.action == "import":
            snapshot = result["snapshot"]
            print(f"{snapshot['environment']}/{snapshot['service']} "
                  f"observed {snapshot['observed_at']}")
        elif args.action == "save":
            print(result["path"])
        else:
            for item in result["differences"]:
                print(f"{item['environment']}/{item['service']}: {item['change']} "
                      f"{','.join(item['changed_fields'])}")
    return 0


def _cmd_iac(args: argparse.Namespace) -> int:
    from ozagia.core.iac_check import check_iac
    result = check_iac(_root(args.root))
    payload = {
        "status": result.status,
        "files_checked": result.files_checked,
        "findings": [item.__dict__ for item in result.findings],
        "diagnostics": list(result.diagnostics),
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"{result.status}: {result.files_checked} files checked")
        for item in result.findings:
            print(f"{item.path}:{item.line}: {item.kind}: {item.message}")
        for message in result.diagnostics:
            print(f"note: {message}")
    return 0 if result.status in {"clean", "findings", "ambiguous"} else 2


def _cmd_python_env(args: argparse.Namespace) -> int:
    """`ozagia env [--for PATH ...]`: interpreters and what they can import."""
    from ozagia.core import environments as EN
    root = _root(args.env_root)
    mode = "off" if args.env_no_cache else "rw"
    try:
        if args.env_for:
            doc = EN.env_for(root, args.env_for, cache_mode=mode)
            text = EN.format_env_for(doc)
        else:
            doc = EN.environments(root, cache_mode=mode)
            text = EN.format_environments(doc)
    except EN.EnvError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.env_json:
        _print_json(doc)
    else:
        print(text, end="")
    if args.env_for:
        return 0 if doc["best"] is not None else 1
    return 0


def _cmd_env(args: argparse.Namespace) -> int:
    if args.action is None:
        return _cmd_python_env(args)
    from ozagia.config import ConfigError, load_project_config
    try:
        cfg = load_project_config(_root(args.root))
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    profiles = [
        {"name": item.name, "paths": list(item.paths),
         "namespaces": list(item.namespaces),
         "account_alias": item.account_alias, "region": item.region,
         "cluster": item.cluster, "risk": item.risk,
         "pipelines": list(item.pipelines)}
        for item in cfg.environments
    ]
    if args.action == "show":
        selected = [item for item in profiles if item["name"] == args.name]
        if not selected:
            print(f"error: no environment {args.name!r}", file=sys.stderr)
            return 2
        profiles = selected
    if args.json:
        _print_json({"environments": profiles, "declared": True, "live": False})
    else:
        for item in profiles:
            print(item["name"])
            for key in ("paths", "namespaces", "account_alias", "region",
                        "cluster", "risk", "pipelines"):
                value = item[key]
                if value:
                    print(f"  {key}: {', '.join(value) if isinstance(value, list) else value}")
    return 0


def _cmd_relay(args: argparse.Namespace, command: list[str]) -> int:
    from ozagia.relay_cli import (
        RelaySpecFileError, build_relay, load_document, load_file,
        parse_target,
    )

    try:
        document = (load_document(sys.stdin.read()) if args.spec == "-"
                    else load_file(args.spec))
        relay = build_relay(
            document, allow_live=args.allow_live,
            allow_network=args.allow_network, ephemeral=args.ephemeral)
    except (RelaySpecFileError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.action == "tunnel":
            try:
                target = parse_target(args.target)
            except RelaySpecFileError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            lease = relay.open_tunnel(
                target, local_port=args.local_port,
                timeout_s=args.timeout_s, dry_run=not args.allow_live)
            payload = {
                "dry_run": lease.dry_run,
                "local_host": lease.local_host,
                "local_port": lease.local_port,
                "target_host": lease.target_host,
                "target_port": lease.target_port,
                "scope": lease.scope,
                "backend": lease.backend,
                "planned_argv": list(lease.planned_argv),
            }
            if args.hold and not args.allow_live:
                print("error: --hold requires --allow-live", file=sys.stderr)
                lease.close()
                return 2
            if args.hold:
                if not args.json:
                    print(f"tunnel {lease.local_host}:{lease.local_port} "
                          f"-> {lease.target_host}:{lease.target_port}")
                try:
                    import time
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    pass
            lease.close()
            if args.json:
                _print_json(payload)
            else:
                print("dry-run tunnel plan" if lease.dry_run else "tunnel closed")
                if lease.dry_run and lease.planned_argv:
                    print(" ".join(lease.planned_argv))
            return 0

        if not command:
            print("error: relay command needs arguments after --", file=sys.stderr)
            return 2
        if args.action == "plan":
            planned = relay.plan_argv(command)
            payload = {"dry_run": True, "argv": list(planned),
                       "backend": relay.kind}
            if args.json:
                _print_json(payload)
            else:
                print(" ".join(planned))
            return 0
        if not args.allow_live:
            print("error: relay run requires --allow-live", file=sys.stderr)
            return 2
        result = relay.run(command, timeout_s=args.timeout_s)
        payload = {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "elapsed_s": result.elapsed_s,
            "dry_run": result.dry_run,
            "backend": result.backend,
            "argv_evidence": result.argv_evidence,
        }
        if args.json:
            _print_json(payload)
        else:
            if result.stdout:
                print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
            if result.stderr:
                print(result.stderr, file=sys.stderr,
                      end="" if result.stderr.endswith("\n") else "\n")
        return 0 if result.returncode == 0 else 1
    except Exception as exc:  # relay errors are deliberately terse
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        relay.close()


def _runtime_config(args: argparse.Namespace):
    from ozagia.config import ConfigError, load_project_config
    try:
        return load_project_config(_root(args.root))
    except (ConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _print_runtime_item(item: dict) -> None:
    from ozagia.core.runtime import runtime_line
    print(runtime_line(item))


def _cmd_runtime(args: argparse.Namespace) -> int:
    from ozagia.core.runtime import (
        RuntimeErrorBase, ServiceManager, TunnelManager, runtime_logs,
        runtime_status,
    )

    config = _runtime_config(args)
    if config is None:
        return 2
    try:
        if args.command == "runtime":
            selected = args.name if getattr(args, "name", None) else None
            payload = runtime_status(config, name=selected)
            if args.json:
                _print_json(payload)
            else:
                if payload["context"]:
                    print(payload["context"])
                else:
                    print("no configured runtime services or tunnels")
            return 0

        if args.command == "service":
            if args.action == "logs":
                payload = runtime_logs(
                    config, args.name, max_bytes=args.max_bytes)
                if args.json:
                    _print_json(payload)
                else:
                    print(payload["logs"], end="")
                return 0
            manager = ServiceManager(config)
            if args.action == "list":
                rows = manager.list()
                if args.json:
                    _print_json({"services": rows, "live": False})
                else:
                    for row in rows:
                        _print_runtime_item(row)
                return 0
            if args.action == "start":
                item = manager.start(args.name)
            elif args.action == "run":
                item = manager.run(args.name)
                if args.json:
                    _print_json(item)
                else:
                    output = item.get("output") or ""
                    if output:
                        print(output, end="" if output.endswith("\n") else "\n")
                    _print_runtime_item(item)
                return 0 if item.get("exit_code") == 0 else 1
            elif args.action == "stop":
                item = manager.stop(args.name)
            elif args.action == "restart":
                item = manager.restart(args.name)
            else:
                item = manager.status(args.name)
            if args.json:
                _print_json(item)
            else:
                _print_runtime_item(item)
            return 0

        if args.command == "process":
            if args.action == "logs":
                payload = runtime_logs(
                    config, args.name, max_bytes=args.max_bytes)
                if args.json:
                    _print_json(payload)
                else:
                    print(payload["logs"], end="")
                return 0
            manager = ServiceManager(config)
            if args.action in {"start", "run"}:
                if not args.argv:
                    print("error: process command needs argv after --",
                          file=sys.stderr)
                    return 2
                workdir = None
                if args.workdir:
                    root = config.root.resolve()
                    candidate = (root / args.workdir).resolve()
                    if not candidate.is_relative_to(root):
                        print("error: workdir must stay inside the repository",
                              file=sys.stderr)
                        return 2
                    workdir = str(candidate)
                if args.action == "start":
                    item = manager.start_raw(args.argv, workdir=workdir)
                else:
                    item = manager.run_raw(args.argv, workdir=workdir)
                    if args.json:
                        _print_json(item)
                    else:
                        output = item.get("output") or ""
                        if output:
                            print(output, end="" if output.endswith("\n") else "\n")
                        _print_runtime_item(item)
                    return 0 if item.get("exit_code") == 0 else 1
            elif args.action == "list":
                rows = manager.list()
                if args.json:
                    _print_json({"processes": rows, "live": False})
                else:
                    for row in rows:
                        _print_runtime_item(row)
                return 0
            elif args.action == "stop":
                item = manager.stop(args.name)
            else:
                item = manager.status(args.name)
            if args.json:
                _print_json(item)
            else:
                _print_runtime_item(item)
            return 0

        if args.command == "tunnel":
            if args.action in {"status", "list"}:
                selected = args.name if getattr(args, "name", None) else None
                payload = runtime_status(config, name=selected)
                if args.json:
                    _print_json(payload)
                else:
                    for item in payload["tunnels"]:
                        _print_runtime_item(item)
                    if not payload["tunnels"]:
                        print("no configured tunnels")
                return 0
            manager = TunnelManager(config)
            if args.action == "stop":
                item = manager.stop(args.name)
            else:
                if not args.allow_live or not args.allow_network:
                    print("error: tunnel start requires --allow-live and "
                          "--allow-network", file=sys.stderr)
                    return 2
                started = manager.start(
                    args.name, allow_live=True, allow_network=True)
                if args.once:
                    stopped = manager.stop(args.name)
                    payload = {"started": started, "stopped": stopped,
                               "live": False}
                    if args.json:
                        _print_json(payload)
                    else:
                        _print_runtime_item(started)
                        print("tunnel opened once and closed")
                    return 0
                if args.json:
                    _print_json(started)
                else:
                    _print_runtime_item(started)
                    print("holding tunnel; press Ctrl-C to stop",
                          file=sys.stderr)
                try:
                    import time
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    pass
                finally:
                    item = manager.stop(args.name)
                if not args.json:
                    _print_runtime_item(item)
                return 0
            if args.json:
                _print_json(item)
            else:
                _print_runtime_item(item)
            return 0
    except (RuntimeErrorBase, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _cmd_preflight(args: argparse.Namespace) -> int:
    from ozagia.core.preflight import build_preflight
    dependencies: dict[str, list[str]] = {}
    for value in args.dependency or []:
        source, separator, target = value.partition(":")
        if not separator or not source or not target:
            print("error: --dependency must be ROOT:ROOT", file=sys.stderr)
            return 2
        dependencies.setdefault(source, []).append(target)
    versions = {}
    for value in args.provider_version or []:
        name, separator, version = value.partition("=")
        if not separator or not name or not version:
            print("error: --provider-version must be NAME=VERSION", file=sys.stderr)
            return 2
        versions[name] = version
    try:
        plan = build_preflight(
            args.root, dependencies, emulator_bin=args.emulator_bin,
            provider_versions=versions)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    payload = {
        "execute": False,
        "roots": list(plan.roots),
        "environment": dict(plan.environment),
        "provider_versions": [list(item) for item in plan.provider_versions],
        "provider_version_note": plan.provider_version_note,
        "commands": [list(item.argv) for item in plan.commands],
    }
    if args.json:
        _print_json(payload)
    else:
        print("dry-run plan; no command was executed")
        for item in plan.commands:
            print(" ".join(item.argv))
    return 0


def _cmd_who(args: argparse.Namespace) -> int:
    """The sessions that edited a file, from the running daemon (which
    knows which sessions are live), else from the journals."""
    from ozagia.config import state_path
    from ozagia.core import owners as O
    root = _root(args.root)
    p = Path(os.path.expanduser(args.path))
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        rel = Path(os.path.realpath(p)).relative_to(root).as_posix()
    except ValueError:
        print(f"{args.path} is outside the repository {root}", file=sys.stderr)
        return 2
    report = None
    try:
        from ozagia.daemon import protocol
        from ozagia.integrations.hookcore import daemon_request
        resp = daemon_request(protocol.socket_path(root),
                              {"id": 1, "op": "who", "path": rel}, timeout=3)
        if isinstance(resp.get("who"), dict):
            report = resp["who"]
    except Exception:  # noqa: BLE001 - no daemon: read the journals
        report = None
    if report is None:
        from ozagia.daemon.server import JOURNAL_NAME
        seen = O.seen_sessions(state_path(root, JOURNAL_NAME))
        tracker = O.Owners(root)
        tracker.seen = seen
        report = O.who(root, rel, state_path(root, "journal.jsonl"),
                       live=tracker.live)
    if args.json:
        _print_json(report)
    else:
        print(O.format_who(report))
    return 0


def _replace_memory(root: Path):
    """The root's DevMemory; the snapshot is built first when missing."""
    from ozagia.config import state_path
    from ozagia.core.memory import DevMemory, build_index
    dump = state_path(root, "graph.json")
    if not dump.exists():
        _ensure_language_support(root)
        print(f"building {dump} ...", file=sys.stderr)
        build_index(root, dump, include_docs="features", include_git=True,
                    include_scripts=True)
    return DevMemory(dump, state_path(root, "journal.jsonl"), root=root)


def _cmd_replace(args: argparse.Namespace) -> int:
    """`ozagia replace`: preview (default) or apply one pattern across the
    files of a scope (`ozagia.edit.replace`)."""
    from ozagia.edit.code_edit import CodeEditError
    from ozagia.intent.replace import file_lines, lint_replace
    root = _root(args.root)
    if args.apply and not args.plan_id:
        print("error: --apply needs --plan-id (the preview's plan_id); run "
              "without --apply first", file=sys.stderr)
        return 2
    try:
        mem = _replace_memory(root)
        res = mem.replace(
            args.pattern, args.replacement, regex=args.regex,
            flags=args.flags, globs=args.glob, keys=args.key,
            max_files=args.max_files, max_matches=args.max_matches,
            expect=args.expect, gitignore=not args.no_gitignore,
            any_suffix=args.any_suffix, apply=args.apply, plan_id=args.plan_id,
            allow_parse_errors=args.allow_parse_errors, task=args.task)
    except (CodeEditError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if res.get("applied"):
        lines, meta = lint_replace(mem, res)
        res["lint"] = lines
        res.update(meta)
    if args.json:
        _print_json(res)
        return 0 if res.get("ok") else 1
    n = res.get("total", 0)
    if not res.get("ok") and not res.get("applied"):
        print(f"refused: {res.get('error')}", file=sys.stderr)
        for line in file_lines(res):
            print(line)
        return 1
    if res.get("applied"):
        k = len(res.get("written") or [])
        w = int(res.get("written_matches") or 0)
        if res.get("partial") or res.get("error"):
            print(f"partially applied: {res.get('error')}", file=sys.stderr)
        print(f"replaced {w} match{'es' if w != 1 else ''} in {k} "
              f"file{'s' if k != 1 else ''}")
        for line in file_lines(res):
            print(line)
        for f in res.get("files") or []:
            if f.get("backup"):
                print(f"backup {f['file']}: {f['backup']}")
        for line in res.get("lint") or []:
            print(line)
        return 1 if res.get("partial") or res.get("error") else 0
    k = res.get("files_changed", 0)
    print(f"preview: {n} match{'es' if n != 1 else ''} in {k} "
          f"file{'s' if k != 1 else ''}; nothing written")
    for line in file_lines(res):
        print(line)
    for f in res.get("files") or []:
        if f.get("diff"):
            sys.stdout.write(f["diff"])
            if f.get("diff_truncated"):
                print(f"... diff of {f['file']} truncated")
    if res.get("parse_refused"):
        print("an apply would be refused: it breaks the parse of "
              + ", ".join(res["parse_refused"]) + " (--allow-parse-errors)")
    print(f"plan_id {res['plan_id']}: apply with --apply --plan-id "
          f"{res['plan_id']}")
    return 0


def _cmd_review_repo(args: argparse.Namespace) -> int:
    """`ozagia review-repo`: a deterministic whole-repository review packet
    (`ozagia.core.repo_review`)."""
    from ozagia.core import repo_review as RR
    root = _root(args.root)
    try:
        packet = RR.review_repo(root, sections=args.section, compare=args.compare,
                                budget_s=args.budget_s, index=args.index,
                                graph_path=args.graph)
        if args.model_pass:
            path = RR.save_compact(root, packet)
            packet["model_review"] = {
                "status": "prepared",
                "compact_path": path,
                "reason": ("the CLI never calls a model; run the reviewers on "
                           "the compact packet with `python -m "
                           "ozagia.harness.review --live --packet PATH --plan "
                           "PLAN --base-url URL` and OZAGIA_EVAL_LIVE=1"),
                "truth_assessed": False,
            }
    except RR.PreflightError as exc:
        if args.json:
            print(RR.packet_json(exc.report), end="")
        else:
            print(RR.format_refusal(exc.report), end="", file=sys.stderr)
        return 2
    except (RR.RepoReviewError, RR.CR.CodeReviewError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    saved = None
    if args.save:
        try:
            saved = RR.save_packet(root, packet)
        except (RR.RepoReviewError, RR.CR.CodeReviewError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    text = RR.packet_json(packet) if args.json else RR.format_packet(packet)
    if args.out:
        try:
            RR.atomic_write(args.out, RR.packet_json(packet) if args.json
                            or str(args.out).endswith(".json") else text)
        except RR.CR.CodeReviewError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(text, end="")
    if saved:
        # Keep --json stdout a single JSON document.
        print(f"saved: {saved['path']} (latest: {saved['latest']})",
              file=sys.stderr if args.json else sys.stdout)
    return RR.exit_code(packet, args.fail_on)


def _git_probe(root: Path, *args: str):
    """Run a read-only git probe, or return None when git cannot be started."""
    import subprocess

    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            timeout=5, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def _deep_review_change_range(root: Path, requested: str | None):
    """Resolve the ``change`` preset's range, or return a clear refusal."""
    inside = _git_probe(root, "rev-parse", "--is-inside-work-tree")
    if (inside is None or inside.returncode != 0
            or inside.stdout.strip() != "true"):
        return None, ("--profile change requires a Git repository; "
                      "pass --root or use --profile full")
    if requested:
        return requested, None
    head = _git_probe(root, "rev-parse", "--verify", "--quiet",
                      "HEAD^{commit}")
    if head is None or head.returncode != 0 or not head.stdout.strip():
        return None, (f"cannot resolve the default range "
                      f"{DEEP_REVIEW_CHANGE_DEFAULT_RANGE}: the repository has "
                      "no committed HEAD; pass --range BASE..HEAD")
    previous = _git_probe(root, "rev-parse", "--verify", "--quiet",
                          "HEAD~1^{commit}")
    if previous is None or previous.returncode != 0 or not previous.stdout.strip():
        return None, (f"cannot resolve the default range "
                      f"{DEEP_REVIEW_CHANGE_DEFAULT_RANGE}: the repository has "
                      "no previous commit; pass --range BASE..HEAD")
    return DEEP_REVIEW_CHANGE_DEFAULT_RANGE, None


def _cmd_deep_review(args: argparse.Namespace) -> int:
    from ozagia.core.deep_review import build_deep_review, DeepReviewError
    from ozagia.core.repo_review import RepoReviewError
    from datetime import datetime, timezone
    root = _root(args.root)
    if not root.is_dir():
        print(f"error: review root is not a directory: {root}", file=sys.stderr)
        return 2
    profile = args.profile
    range_spec = args.range_spec
    max_findings = args.max_findings
    if profile in DEEP_REVIEW_CHANGE_PROFILES:
        range_spec, change_error = _deep_review_change_range(root, range_spec)
        if change_error:
            print(f"error: {change_error}", file=sys.stderr)
            return 2
        # The preset is the established full review plus a change-set range;
        # core/deep_review keeps no extra profile or preset semantics.
        profile = "full"
        if max_findings is None:
            max_findings = DEEP_REVIEW_CHANGE_MAX_FINDINGS
    out = args.out or (root / ".ozagia" / "reviews" /
                       ("deep-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")))
    # Reports are writes into the reviewed repository. Keep those under its
    # state directory; an explicitly supplied destination outside the target
    # remains available for callers who want to store reports elsewhere.
    resolved_out = Path(out).resolve()
    state_dir = (root / ".ozagia").resolve()
    try:
        resolved_out.relative_to(root)
        inside_root = True
    except ValueError:
        inside_root = False
    if inside_root:
        try:
            resolved_out.relative_to(state_dir)
        except ValueError:
            print("error: --out inside the repository must be under .ozagia/", file=sys.stderr)
            return 2
    bench_commands = None
    if args.run_bench is not None:
        try:
            bench_commands = [json.loads(value) for value in args.run_bench]
            if any(not isinstance(command, list) for command in bench_commands):
                raise ValueError("each --run-bench value must be a JSON array of strings")
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"error: invalid --run-bench command: {exc}", file=sys.stderr)
            return 2
    try:
        security_options = {}
        if args.security_tool:
            security_options["security_tools"] = args.security_tool
        if args.security_network:
            security_options["security_network"] = True
        if args.local_advisories is not None:
            security_options["local_advisories"] = args.local_advisories
        if args.license_policy is not None:
            security_options["license_policy"] = args.license_policy
        if args.supply_chain_policy is not None:
            security_options["supply_chain_policy"] = args.supply_chain_policy
        result = build_deep_review(root, resolved_out, profile=profile,
                                   budget_s=args.budget_s, index=args.index,
                                   range_spec=range_spec,
                                   baseline=args.baseline, fail_on=args.fail_on,
                                   max_findings=max_findings,
                                   focus_subjects=args.focus,
                                   profile_reports=args.profile_report,
                                   bench_commands=bench_commands,
                                   bench_timeout_s=args.bench_timeout_s,
                                   **security_options)
    except (DeepReviewError, RepoReviewError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.model_pass:
        import hashlib
        import os
        from ozagia.core.deep_review.model_pass import run_model_pass
        from ozagia.core.deep_review.orchestrator import _write
        from ozagia.core.deep_review.verification import (
            verify_model_candidates, verify_python_mutable_default,
        )
        from ozagia.core.security import redact_text
        model_result = None
        if not args.live or os.environ.get("OZAGIA_EVAL_LIVE") != "1":
            model_result = {"status": "unavailable", "runs": [], "errors": [{
                "stage": "gate",
                "error": "model review requires --live and OZAGIA_EVAL_LIVE=1",
            }], "findings": [], "truth_assessed": False}
        elif not args.model or not args.base_url:
            model_result = {"status": "unavailable", "runs": [], "errors": [{
                "stage": "configuration",
                "error": "--model and --base-url are required for model review",
            }], "findings": [], "truth_assessed": False}
        else:
            try:
                from ozagia.harness.review import openai_runner
                runner = openai_runner(base_url=args.base_url, model=args.model,
                                       max_tokens=args.max_tokens)
                pass_options = {"roles": args.model_role} if args.model_role else {}
                if args.model_budget_s is not None:
                    pass_options["deadline_s"] = args.model_budget_s
                if args.model_max_requests is not None:
                    pass_options["max_requests"] = args.model_max_requests
                if args.model_token_budget is not None:
                    pass_options["max_tokens"] = args.model_token_budget
                if args.model_cost_budget is not None:
                    pass_options["max_cost_usd"] = args.model_cost_budget
                pass_options["ocelot"] = args.ocelot
                model_result = run_model_pass(
                    result["findings"], model_runner=runner, models=args.model,
                    root=root, **pass_options)
            except Exception as exc:
                # The deterministic report is already complete and remains the
                # authoritative output if provider setup or execution fails.
                model_result = {"status": "unavailable", "runs": [],
                                "errors": [{"stage": "model",
                                            "error": f"{type(exc).__name__}: model review failed"}],
                                "findings": [], "truth_assessed": False}
        # Harness exceptions can contain endpoint details, response excerpts,
        # or credentials supplied by a provider. Keep only their exception type;
        # locally constructed gate/configuration and budget messages are fixed
        # text and remain useful.
        for error in model_result.get("errors", []):
            if isinstance(error, dict):
                if error.get("error_type"):
                    error["error"] = (f"{error.get('error_type') or 'ProviderError'}: "
                                      "model review failed")
                else:
                    error["error"] = redact_text(str(error.get("error") or
                                                     "model review failed"))[:500]
        # Verify candidates against the complete canonical deterministic
        # artifact. ``result['findings']`` may be a top-N view; findings.json
        # retains the full set and is the only promotion evidence source.
        try:
            deterministic_rows = json.loads(
                (resolved_out / "findings.json").read_text(encoding="utf-8"))
            if not isinstance(deterministic_rows, list):
                raise ValueError("findings.json must contain a list")
            verification = verify_model_candidates(
                model_result.get("findings", []), deterministic_rows)
        except (OSError, ValueError, json.JSONDecodeError):
            verification = {
                "schema": "ozagia.deep_review.model_verification.v1",
                "decisions": [], "corroborated": 0,
                "unverified": len(model_result.get("findings", [])),
                "truth_assessed": False,
                "error": "deterministic findings could not be read; no candidates corroborated",
            }
        model_result["verification"] = verification
        # Machine-check only bounded assertion payloads. These results attach
        # to model candidates and never promote their wording or severity.
        checked_assertions = 0
        for candidate in model_result.get("findings", []):
            if not isinstance(candidate, dict):
                continue
            candidate["assertion_verifications"] = []
            assertions = candidate.get("assertions", [])
            if not isinstance(assertions, list):
                continue
            for assertion in assertions:
                if checked_assertions >= 32:
                    candidate["assertion_verification_omitted"] = True
                    break
                candidate["assertion_verifications"].append(
                    verify_python_mutable_default(root, assertion)
                    if isinstance(assertion, dict) else {
                        "state": "unverified",
                        "assertion": "python.mutable_default",
                        "reason": "malformed assertion payload",
                        "truth_assessed": False,
                    })
                checked_assertions += 1
        model_result["assertion_verification_note"] = (
            "A verified_assertion result confirms only the exact hash-pinned "
            "Python mutable-default fact. It does not verify model wording, "
            "impact, severity, or overall truth; it never changes deterministic "
            "findings or blocking status. At most 32 assertions are checked.")
        model_result["verification_note"] = (
            "Corroboration requires a unique cited deterministic finding with "
            "exact subject and type. It confirms rule identity only; it does "
            "not verify candidate wording, impact, severity, or truth. "
            "Deterministic findings and their severity/status are unchanged.")
        try:
            model_text = json.dumps(model_result, sort_keys=True, indent=2) + "\n"
            _write(resolved_out / "model-review.json", model_text)
            manifest = result["manifest"]
            manifest["model_review"] = {
                "status": model_result["status"],
                "report": "model-review.json",
                "report_hash": "sha256:" + hashlib.sha256(model_text.encode()).hexdigest(),
                "candidate_findings": len(model_result.get("findings", [])),
                "verification": {
                    "corroborated": verification["corroborated"],
                    "unverified": verification["unverified"],
                    "truth_assessed": False,
                },
                "validation": model_result.get("validation"),
                "budgets": model_result.get("budgets"),
                "ocelot": ({
                    "requested": bool((model_result.get("ocelot") or {}).get("requested")),
                    "available": bool((model_result.get("ocelot") or {}).get("available")),
                    "state": (model_result.get("ocelot") or {}).get("state"),
                    "cluster_count": (model_result.get("ocelot") or {}).get("cluster_count"),
                    "error": (model_result.get("ocelot") or {}).get("error"),
                } if isinstance(model_result.get("ocelot"), dict) else None),
                "truth_assessed": False,
            }
            _write(resolved_out / "manifest.json",
                   json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        except OSError as exc:
            print(f"warning: model review artifact could not be written: {exc}",
                  file=sys.stderr)
    if args.json:
        print(json.dumps(result["manifest"], sort_keys=True, indent=2))
    else:
        print(f"deep review written to {result['out']}")
    return 1 if result["failed"] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version=f"{PROG} {_version()}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    doc = sub.add_parser("doctor", help="inspect snapshot and optional tool availability")
    doc.add_argument("--root", default=None, help="repository root")
    doc.add_argument("--json", action="store_true", help="print structured diagnostics")

    ix = sub.add_parser("index", help="build or refresh the graph snapshot",
                        description="Build the snapshot if missing; refresh it if stale.")
    ix.add_argument("--root", default=None, help="repository (default: OZAGIA_ROOT or "
                                                 "the git top level of the cwd)")
    ix.add_argument("--rebuild", action="store_true", help="build from scratch")
    ix.add_argument("--dump", default=None, help="snapshot path (default: "
                                                 "<state dir>/graph.json)")
    ix.add_argument("--json", action="store_true", help="print the report as JSON")

    ag = sub.add_parser("agent", help="run an OpenCode session with the graph daemon",
                        description="Run OpenCode on a repository with the ozagia plugin "
                                    "and graph daemon. With a prompt: `opencode run`; "
                                    "without: the interactive TUI. Arguments after `--` "
                                    "go to opencode.")
    ag.add_argument("--workdir", "--root", dest="workdir", default=None,
                    help="repository (default: OZAGIA_ROOT or the git top level of the cwd)")
    pr = ag.add_mutually_exclusive_group()
    pr.add_argument("--prompt", default=None,
                    help="run non-interactively with this prompt (sent on stdin, "
                         "never in opencode's argv)")
    pr.add_argument("--prompt-file", default=None,
                    help="read the prompt from a file (its contents go to stdin; "
                         "the path is not passed to opencode)")
    ag.add_argument("--model", "-m", default=None, help="provider/model, e.g. vllm/qwen3.8-27b")
    ag.add_argument("--opencode-config", default=None, metavar="PATH",
                    help="layer provider settings from this OpenCode JSON config "
                         "(provider, providers, model, small_model only) into the "
                         "generated harness config; --model wins. Plugins, MCP "
                         "servers, permissions, agents and tools are never copied, "
                         "and the global config is not read implicitly")
    ag.add_argument("--clone", action="store_true",
                    help="run in an isolated clone under the temp dir (kept)")
    ag.add_argument("--base", default=None, help="with --clone: commit to check out "
                                                 "(default HEAD)")
    ag.add_argument("--keep-plugin", action="store_true",
                    help="leave .opencode/plugins/ozagia.js in the workdir")
    ag.add_argument("--timeout", type=float, default=None,
                    help="seconds before the session is killed (default: none)")
    ag.add_argument("--python", default=None,
                    help="interpreter that `python` names in the agent's shell commands")
    ag.add_argument("--opencode", default=None,
                    help="opencode executable (default: $OZAGIA_OPENCODE, which the "
                         "installer sets to its pinned OpenCode; `system` or unset: "
                         "`opencode` on PATH)")
    ag.add_argument("--mcp", default=None, choices=("coding", "research", "combined"),
                    help="also give the session the ozagia MCP server in this mode "
                         "(research: the research tools; combined: coding and research)")
    ag.add_argument("--research", dest="mcp", action="store_const", const="combined",
                    help="same as --mcp combined")
    ag.add_argument("--runs-dir", default=None,
                    help="where run roots (logs, clones) go (default: <tmp>/ozagia_agent)")
    ag.add_argument("--no-managed-tmp", dest="managed_tmp", action="store_false",
                    help="do not register the run root in the managed temp registry")
    ag.add_argument("--tmp-ttl", type=_tmp_ttl, default=None, metavar="D",
                    help="managed run-root lifetime (default: 7 days; a bare number "
                         "is days, or use an s/m/h/d suffix)")
    ag.add_argument("--clean-logs", action="store_true",
                    help="after a successful run with no kept clone, delete the run "
                         "root and scratch workspace (default: keep the logs)")
    ag.add_argument("--repo-map", action="store_true",
                    help="send a bounded, task-ranked repository map in the first "
                         "model request (off by default)")
    ag.add_argument("--repo-map-tokens", type=_repo_map_budget, default=None,
                    metavar="N",
                    help="repo-map token budget with --repo-map, 200..8000 "
                         "(default: 1500)")

    for name, (_fn, text) in DELEGATES.items():
        sub.add_parser(name, help=text, add_help=False)
    # The explicit collection surface is also reachable as `ozagia profile run`;
    # this hidden entry point keeps the capability audit one surface per
    # top-level command.
    sub.add_parser("profile-run", help=argparse.SUPPRESS, add_help=False)

    ins = sub.add_parser("install", help="install an integration into a workdir",
                         description="Install the OpenCode plugin, the Claude Code hooks, "
                                     "or the Codex or Kiro hooks and MCP server. Other "
                                     "options go to the integration's installer (for "
                                     "codex: --scope project|user, --python P, --jobs; for kiro "
                                     "also --format v3|agent|both, --agent NAME).")
    ins.add_argument("target", choices=tuple(INSTALL_TARGETS))
    ins.add_argument("--workdir", default=".", help="project directory (default: .)")

    job = sub.add_parser("job", help="wait for an explicitly named infrastructure job")
    jsub = job.add_subparsers(dest="action", required=True)
    wait = jsub.add_parser("wait", help="poll one reviewed read-only job")
    wait.add_argument("--provider", required=True,
                      choices=("ssm", "codebuild", "codepipeline", "kubectl"))
    wait.add_argument("--job-id", required=True)
    wait.add_argument("--resource", default=None,
                      help="CodePipeline pipeline name")
    wait.add_argument("--instance-id", default=None,
                      help="SSM instance id (SSM provider)")
    wait.add_argument("--region", default=None, help="explicit AWS region")
    wait.add_argument("--namespace", default=None,
                      help="explicit Kubernetes namespace")
    wait.add_argument("--executable", default=None,
                      help="explicit aws/kubectl path or name")
    wait.add_argument("--allow-live", action="store_true",
                      help="explicitly enable the live read-only provider")
    wait.add_argument("--timeout-s", type=float, default=300.0)
    wait.add_argument("--poll-s", type=float, default=2.0)
    wait.add_argument("--max-lines", type=int, default=10)
    wait.add_argument("--json", action="store_true")

    ops = sub.add_parser("ops", help="append-only ops receipts and handoffs")
    osub = ops.add_subparsers(dest="action", required=True)
    rec = osub.add_parser("record", help="append one redacted ops receipt")
    rec.add_argument("--record", default=None, help="JSON object, or - for stdin")
    rec.add_argument("--record-file", default=None)
    rec.add_argument("--root", default=None)
    rec.add_argument("--json", action="store_true")
    resume = osub.add_parser("resume", help="show a bounded ops handoff")
    resume.add_argument("--root", default=None)
    resume.add_argument("--limit", type=int, default=20)
    resume.add_argument("--environment", default=None)
    resume.add_argument("--json", action="store_true")

    live = sub.add_parser("live", help="import/diff supplied live-state snapshots")
    lsub = live.add_subparsers(dest="action", required=True)
    for name, help_text in (("import", "validate one supplied artifact"),
                            ("save", "save one supplied artifact under .ozagia/live"),
                            ("diff", "diff two supplied artifacts")):
        item = lsub.add_parser(name, help=help_text)
        item.add_argument("--before", default=None)
        item.add_argument("--before-file", default=None)
        item.add_argument("--after", default=None)
        item.add_argument("--after-file", default=None)
        item.add_argument("--provenance", default="supplied-artifact")
        item.add_argument("--root", default=None)
        item.add_argument("--json", action="store_true")

    iac = sub.add_parser("iac", help="offline Terraform/OpenTofu checks")
    isub = iac.add_subparsers(dest="action", required=True)
    check = isub.add_parser("check", help="source-only pre-plan checks")
    check.add_argument("--root", default=None)
    check.add_argument("--json", action="store_true")

    env = sub.add_parser(
        "env", help="Python environments: which interpreter can run what "
                    "(docs/environments.md); `env list|show`: declarative "
                    "environment profiles",
        description="Without a subcommand: the project's Python interpreters, "
                    "what each provides, and with --for the interpreters that "
                    "can import those files, best first. Interpreters are probed "
                    "by running `python -I -c`, which runs their site setup "
                    "(.pth files, sitecustomize); interpreters the project ships "
                    "(venvs in the root or the source repository) are only read "
                    "as text unless OZAGIA_TRUST_PROJECT_PYTHON=1 or the user "
                    "config sets [environments] trust_project_python = true.")
    env.add_argument("--root", dest="env_root", default=None,
                     help="repository root")
    env.add_argument("--for", dest="env_for", nargs="+", action="extend",
                     default=None, metavar="PATH",
                     help="files or directories (root-relative) to find an "
                          "interpreter for")
    env.add_argument("--json", dest="env_json", action="store_true",
                     help="print JSON")
    env.add_argument("--no-cache", dest="env_no_cache", action="store_true",
                     help="probe every interpreter, read and write no cache")
    esub = env.add_subparsers(dest="action", required=False)
    env_list = esub.add_parser("list", help="list configured environments")
    env_list.add_argument("--root", default=None)
    env_list.add_argument("--json", action="store_true")
    env_show = esub.add_parser("show", help="show one configured environment")
    env_show.add_argument("name")
    env_show.add_argument("--root", default=None)
    env_show.add_argument("--json", action="store_true")

    relay = sub.add_parser("relay", help="policy-gated command and TCP relays")
    rsub = relay.add_subparsers(dest="action", required=True)
    for name, help_text in (("plan", "print the exact argv plan"),
                            ("run", "run one allowlisted command")):
        item = rsub.add_parser(name, help=help_text)
        item.add_argument("--spec", required=True, help="JSON chain spec, or - for stdin")
        item.add_argument("--allow-live", action="store_true")
        item.add_argument("--allow-network", action="store_true")
        item.add_argument("--ephemeral", action="store_true")
        item.add_argument("--timeout-s", type=float, default=300.0)
        item.add_argument("--json", action="store_true")
    tunnel = rsub.add_parser("tunnel", help="open or plan a local tunnel")
    tunnel.add_argument("--spec", required=True, help="JSON chain spec, or - for stdin")
    tunnel.add_argument("--target", required=True, help="HOST:PORT")
    tunnel.add_argument("--local-port", type=int, default=0)
    tunnel.add_argument("--allow-live", action="store_true")
    tunnel.add_argument("--allow-network", action="store_true")
    tunnel.add_argument("--ephemeral", action="store_true")
    tunnel.add_argument("--hold", action="store_true")
    tunnel.add_argument("--timeout-s", type=float, default=30.0)
    tunnel.add_argument("--json", action="store_true")

    runtime = sub.add_parser(
        "runtime", help="configured services, processes and tunnels")
    rtsub = runtime.add_subparsers(dest="action", required=True)
    for name, help_text in (("status", "show configured and observed runtime state"),
                            ("list", "list configured runtime items")):
        item = rtsub.add_parser(name, help=help_text)
        item.add_argument("name", nargs="?", default=None)
        item.add_argument("--root", default=None)
        item.add_argument("--json", action="store_true")

    service = sub.add_parser("service", help="manage configured local services")
    svcsub = service.add_subparsers(dest="action", required=True)
    svc_list = svcsub.add_parser("list", help="list configured services")
    svc_list.add_argument("--root", default=None)
    svc_list.add_argument("--json", action="store_true")
    for name, help_text in (
        ("start", "start one configured service in the background"),
        ("run", "run one configured service in the foreground and wait"),
        ("stop", "stop one verified background service"),
        ("restart", "restart one configured background service"),
        ("status", "show one configured service"),
        ("logs", "read a bounded local service log"),
    ):
        item = svcsub.add_parser(name, help=help_text)
        item.add_argument("name")
        item.add_argument("--root", default=None)
        item.add_argument("--json", action="store_true")
        if name == "logs":
            item.add_argument("--max-bytes", type=int, default=4_096)

    process = sub.add_parser("process", help="manage explicit local processes")
    psub = process.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("start", "start an explicit argv in the background; use -- before it"),
        ("run", "run an explicit argv in the foreground; use -- before it"),
    ):
        item = psub.add_parser(name, help=help_text)
        item.add_argument("--workdir", default=None,
                          help="repository-relative working directory")
        item.add_argument("--root", default=None)
        item.add_argument("--json", action="store_true")
    proc_list = psub.add_parser("list", help="list configured and raw processes")
    proc_list.add_argument("--root", default=None)
    proc_list.add_argument("--json", action="store_true")
    for name, help_text in (
        ("status", "show one process"),
        ("stop", "stop one verified background process"),
        ("logs", "read a bounded local process log"),
    ):
        item = psub.add_parser(name, help=help_text)
        item.add_argument("name")
        item.add_argument("--root", default=None)
        item.add_argument("--json", action="store_true")
        if name == "logs":
            item.add_argument("--max-bytes", type=int, default=4_096)

    tunnel = sub.add_parser(
        "tunnel", help="configured relay tunnels and their live state")
    tsub = tunnel.add_subparsers(dest="action", required=True)
    for name, help_text in (("status", "show configured and observed tunnels"),
                            ("list", "list configured tunnels")):
        item = tsub.add_parser(name, help=help_text)
        item.add_argument("name", nargs="?", default=None)
        item.add_argument("--root", default=None)
        item.add_argument("--json", action="store_true")
    tunnel_start = tsub.add_parser(
        "start", help="open one configured tunnel and hold it in this process")
    tunnel_start.add_argument("name")
    tunnel_start.add_argument("--root", default=None)
    tunnel_start.add_argument("--allow-live", action="store_true")
    tunnel_start.add_argument("--allow-network", action="store_true")
    tunnel_start.add_argument("--once", action="store_true",
                              help="open, report and close it immediately")
    tunnel_start.add_argument("--json", action="store_true")
    tunnel_stop = tsub.add_parser("stop", help="stop a tunnel held by this process")
    tunnel_stop.add_argument("name")
    tunnel_stop.add_argument("--root", default=None)
    tunnel_stop.add_argument("--json", action="store_true")

    from ozagia.goals_cli import add_parser as _goal_parser
    _goal_parser(sub)

    who = sub.add_parser("who", help="sessions that edited a file, and whether "
                                     "their edits are committed")
    who.add_argument("path", help="file (relative to the cwd, or absolute)")
    who.add_argument("--root", default=None, help="repository root")
    who.add_argument("--json", action="store_true", help="print the report as JSON")

    rp = sub.add_parser("replace", help="replace a pattern across files "
                                        "(preview unless --apply)",
                        description="Replace a literal (or with --regex, a Python "
                                    "re) pattern across the files of a scope, "
                                    "through the edit transaction. Preview by "
                                    "default; --apply writes each file with a "
                                    "backup and journals one entry.")
    rp.add_argument("pattern")
    rp.add_argument("replacement")
    rp.add_argument("--glob", action="append", default=None,
                    help="root-relative glob (repeatable; ** crosses dirs)")
    rp.add_argument("--key", action="append", default=None,
                    help="node key whose span restricts matches (repeatable)")
    rp.add_argument("--regex", action="store_true",
                    help="PATTERN is a Python re; REPLACEMENT may use \\1, \\g<name>")
    rp.add_argument("--flags", default=None, help="regex flags: letters i,m,s,x,a")
    rp.add_argument("--expect", type=int, default=None,
                    help="refuse unless there are exactly N matches")
    rp.add_argument("--max-files", type=int, default=None)
    rp.add_argument("--max-matches", type=int, default=None)
    rp.add_argument("--apply", action="store_true",
                    help="write the change (needs --plan-id)")
    rp.add_argument("--plan-id", default=None,
                    help="the preview's plan_id; --apply refuses unless the "
                         "plan is still the preview's")
    rp.add_argument("--any-suffix", action="store_true",
                    help="every file, not only text suffixes (dotfiles too)")
    rp.add_argument("--allow-parse-errors", action="store_true")
    rp.add_argument("--no-gitignore", action="store_true",
                    help="also files .gitignore excludes")
    rp.add_argument("--task", default=None, help="journal task label")
    rp.add_argument("--root", default=None, help="repository root")
    rp.add_argument("--json", action="store_true", help="print the result as JSON")

    rr = sub.add_parser("review-repo", help="deterministic review of the whole "
                                            "repository (docs/repo_review.md)",
                        description="Review the checkout: architecture, tests, "
                                    "health, churn, docs, security hygiene, ops "
                                    "sources and dependencies. No model calls, no "
                                    "network, no writes to the repository.")
    rr.add_argument("--root", default=None, help="repository root")
    rr.add_argument("--json", action="store_true", help="print the packet as JSON")
    rr.add_argument("--out", default=None,
                    help="also write the output atomically to this path (a .json "
                         "path gets the JSON packet)")
    rr.add_argument("--section", action="append", default=None,
                    choices=("architecture", "tests", "health", "churn", "docs",
                             "security", "ops", "dependencies"),
                    help="review only this section (repeatable; default: all)")
    rr.add_argument("--compare", default=None, metavar="PACKET",
                    help="previous packet JSON: report new, resolved and "
                         "unchanged finding ids")
    rr.add_argument("--fail-on", default="blocking",
                    choices=("blocking", "major", "never"),
                    help="exit 1 at or above this severity (default: blocking)")
    rr.add_argument("--index", action="store_true",
                    help="build or refresh the graph snapshot first")
    rr.add_argument("--graph", default=None,
                    help="graph snapshot (default: <state dir>/graph.json)")
    rr.add_argument("--budget-s", type=float, default=60.0,
                    help="wall-clock budget for the sections (default: 60)")
    rr.add_argument("--save", action="store_true",
                    help="also save the packet under <state dir>/reviews/ and "
                         "as latest.json (atomic), as the MCP tool does")
    rr.add_argument("--model-pass", action="store_true",
                    help="also write a compact packet for model reviewers under "
                         "<state dir>/reviews/ (no model is called)")

    dr = sub.add_parser("deep-review", help="compose an offline subject-based review report")
    dr.add_argument("--root", default=None, help="repository root")
    dr.add_argument("--out", default=None, help="report directory (default: <root>/.ozagia/reviews/deep-<UTC stamp>)")
    dr.add_argument("--profile", default="full", choices=DEEP_REVIEW_PROFILES,
                    help=("full, one subject profile, or 'change' "
                          "(alias 'review-change') for a full-profile review of "
                          "--range, defaulting to HEAD~1..HEAD"))
    dr.add_argument("--index", action="store_true", help="refresh graph before review")
    dr.add_argument("--security-tool", action="append", default=None,
                    metavar="NAME",
                    help="explicitly run one allowlisted security scanner; "
                         "repeatable (default: no external scanners)")
    dr.add_argument("--security-network", action="store_true",
                    help="explicitly allow a requested scanner that requires "
                         "network access (default: refuse)")
    dr.add_argument("--local-advisories", default=None, metavar="PATH",
                    help="match exact locked versions against this local JSON "
                         "advisory dataset; no download, ranges or clean verdict")
    dr.add_argument("--license-policy", default=None, metavar="PATH",
                    help="evaluate declared lockfile licenses against a local "
                         "allow/deny policy; ambiguous values remain unknown")
    dr.add_argument("--supply-chain-policy", default=None, metavar="PATH",
                    help="apply an explicit local supply-chain policy JSON to "
                         "the offline dependency checks; no network")
    dr.add_argument("--range", dest="range_spec", default=None,
                    metavar="BASE..HEAD", help="include deterministic review of a commit range")
    dr.add_argument("--profile-report", action="append", default=None,
                    metavar="PATH",
                    help="register a measured profile report (or artifact) as "
                         "performance/scalability evidence; repeatable")
    dr.add_argument("--run-bench", action="append", default=None,
                    metavar="JSON_ARGV",
                    help="explicitly run a caller-supplied argv vector, encoded "
                         "as a JSON string array; repeatable, no shell")
    dr.add_argument("--bench-timeout-s", type=float, default=30.0,
                    help="maximum seconds per benchmark command (default: 30; "
                         "also bounded by --budget-s)")
    dr.add_argument("--baseline", default=None, help="previous report directory")
    dr.add_argument("--fail-on", default="never", choices=("blocking", "major", "never"))
    dr.add_argument("--budget-s", type=float, default=60.0)
    dr.add_argument("--max-findings", type=int, default=None)
    dr.add_argument("--focus", action="append", default=None,
                    choices=("security", "performance", "failure-modes",
                             "data-structures", "scalability", "architecture",
                             "tests-and-coverage", "dependencies", "operations",
                             "documents-and-api", "correctness"),
                    help="prioritize this subject within its severity; repeatable")
    dr.add_argument("--model-pass", action="store_true",
                    help="run optional independent model reviewers; requires --live and OZAGIA_EVAL_LIVE=1")
    dr.add_argument("--live", action="store_true",
                    help="explicitly enable model calls (also requires OZAGIA_EVAL_LIVE=1)")
    dr.add_argument("--model", default=None, help="provider model name for --model-pass")
    dr.add_argument("--max-tokens", type=int, default=None,
                    help="maximum completion tokens per model reviewer")
    dr.add_argument("--base-url", default=None, help="OpenAI-compatible API base URL")
    dr.add_argument("--model-role", action="append", default=None,
                    choices=("security", "performance", "failures", "data",
                             "scalability", "architecture", "tests"))
    dr.add_argument("--model-budget-s", type=float, default=None,
                    help="overall wall-clock budget for the model pass "
                         "(default: 120 seconds)")
    dr.add_argument("--model-max-requests", type=int, default=None,
                    help="maximum number of model reviewer requests "
                         "(default: 32)")
    dr.add_argument("--model-token-budget", type=int, default=None,
                    help="optional total token cap across model reviewer runs "
                         "when the provider reports usage")
    dr.add_argument("--model-cost-budget", type=float, default=None,
                    help="optional total cost cap in USD across model reviewer "
                         "runs when the provider reports usage")
    dr.add_argument("--ocelot", action="store_true",
                    help="opt in to Ocelot clustering of model candidates "
                         "(no install, network or model call; exact fallback)")
    dr.add_argument("--json", action="store_true")

    preflight = sub.add_parser("preflight", help="dry-run IaC emulator plan")
    psub = preflight.add_subparsers(dest="action", required=True)
    plan = psub.add_parser("plan", help="print an ordered, scrubbed plan")
    plan.add_argument("--root", action="append", required=True)
    plan.add_argument("--dependency", action="append", default=None,
                      help="ROOT:ROOT (repeatable)")
    plan.add_argument("--emulator-bin", required=True)
    plan.add_argument("--provider-version", action="append", default=None,
                      help="NAME=VERSION (repeatable)")
    plan.add_argument("--json", action="store_true")

    from ozagia.capabilities_cli import add_parser as _capabilities_parser
    _capabilities_parser(sub)
    from ozagia.engine_http import add_parser as _engine_parser
    _engine_parser(sub)
    from ozagia.task_preflight_cli import add_parser as _task_preflight_parser
    _task_preflight_parser(sub)
    from ozagia.overview_cli import add_parser as _overview_parser
    _overview_parser(sub)
    from ozagia.languages_cli import add_parser as _languages_parser
    _languages_parser(sub)
    from ozagia.supply_chain_cli import add_parser as _supply_chain_parser
    _supply_chain_parser(sub)
    from ozagia.migration_cli import add_parser as _migration_parser
    _migration_parser(sub)
    from ozagia.temp_cli import add_parser as _temp_parser
    _temp_parser(sub)
    from ozagia.assets_cli import add_parser as _assets_parser
    _assets_parser(sub)
    from ozagia.mesh_cli import add_parser as _mesh_parser
    _mesh_parser(sub)
    return p


def _opencode_exe(value: str | None) -> str:
    if value == "system":
        return "opencode"
    return value or os.environ.get("OZAGIA_OPENCODE") or "opencode"


def _repo_map_budget(value: str) -> int:
    """Argparse type for --repo-map-tokens: an integer in the runner's range."""
    from ozagia.harness.opencode import AgentError, validate_repo_map_tokens
    try:
        tokens = int(value)
    except ValueError:
        tokens = None
    try:
        return validate_repo_map_tokens(tokens)
    except AgentError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _tmp_ttl(value: str) -> int:
    """Argparse type for --tmp-ttl: days by default, or an s/m/h/d suffix."""
    from ozagia.core.temp_space import MAX_TTL_S, MIN_TTL_S
    text = str(value).strip().lower()
    multiplier = 24 * 60 * 60
    if text and text[-1] in "smhd":
        suffix = text[-1]
        text = text[:-1]
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 24 * 60 * 60}[suffix]
    try:
        seconds = int(float(text) * multiplier)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a duration: {value!r}") from None
    if not MIN_TTL_S <= seconds <= MAX_TTL_S:
        raise argparse.ArgumentTypeError(
            f"tmp TTL must be between {MIN_TTL_S} and {MAX_TTL_S} seconds")
    return seconds


_COMMANDS = frozenset({"index", "agent", "install", "who", "replace",
                       "review-repo", "goal", "capabilities", "engine",
                       "profile-run", "task-preflight", "overview", "shape",
                       "languages", "supply-chain", "migration", "tmp",
                       "assets", "mesh", *DELEGATES})
# Agent options that may lead a bare `ozagia ...` (the TUI).
_AGENT_FLAGS = frozenset({"--research", "--mcp", "--model", "-m", "--workdir",
                          "--root", "--clone", "--prompt", "--prompt-file",
                          "--repo-map", "--repo-map-tokens", "--opencode-config"})


def _default_agent(argv: list[str]) -> list[str]:
    """`ozagia` alone, or `ozagia DIR [agent options]`, is the TUI (as
    `opencode` and `opencode DIR` are)."""
    if not argv:
        return ["agent"]
    head = argv[0]
    if head in _AGENT_FLAGS:
        return ["agent", *argv]           # `ozagia --research`, `ozagia -m M`
    if head in _COMMANDS or head.startswith("-"):
        return argv
    if Path(head).is_dir():
        return ["agent", "--workdir", head, *argv[1:]]
    return argv


def main(argv: list[str] | None = None) -> int:
    _retitle_process()
    argv = _default_agent(list(sys.argv[1:] if argv is None else argv))
    if argv[:2] == ["check", "iac"]:
        argv = ["iac", "check", *argv[2:]]
    if argv[:1] == ["profile-run"]:
        return _profile_run_main(argv[1:])
    if argv and argv[0] in DELEGATES:
        return _delegate(argv[0])(argv[1:])
    extra: list[str] = []
    if argv[:1] in (["agent"], ["relay"], ["process"]) and "--" in argv:
        at = argv.index("--")
        argv, extra = argv[:at], argv[at + 1:]
    parser = build_parser()
    if argv[:1] == ["install"]:
        args, rest = parser.parse_known_args(argv)
        return _cmd_install(args, rest)
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "index":
        return _cmd_index(args)
    if args.command == "agent":
        args.extra = extra
        if args.base and not args.clone:
            parser.error("--base needs --clone")
        if args.repo_map_tokens is not None and not args.repo_map:
            parser.error("--repo-map-tokens needs --repo-map")
        if args.repo_map and args.repo_map_tokens is None:
            from ozagia.harness.opencode import REPO_MAP_DEFAULT_TOKENS
            args.repo_map_tokens = REPO_MAP_DEFAULT_TOKENS
        return _cmd_agent(args)
    if args.command == "job":
        return _cmd_job(args)
    if args.command == "ops":
        return _cmd_ops(args)
    if args.command == "live":
        return _cmd_live(args)
    if args.command == "iac":
        return _cmd_iac(args)
    if args.command == "env":
        return _cmd_env(args)
    if args.command == "relay":
        return _cmd_relay(args, extra)
    if args.command == "runtime":
        return _cmd_runtime(args)
    if args.command == "service":
        return _cmd_runtime(args)
    if args.command == "process":
        args.argv = extra
        return _cmd_runtime(args)
    if args.command == "tunnel":
        return _cmd_runtime(args)
    if args.command == "preflight":
        return _cmd_preflight(args)
    if args.command == "who":
        return _cmd_who(args)
    if args.command == "goal":
        from ozagia.goals_cli import run as _goal_run
        return _goal_run(args)
    if args.command == "replace":
        return _cmd_replace(args)
    if args.command == "review-repo":
        return _cmd_review_repo(args)
    if args.command == "deep-review":
        return _cmd_deep_review(args)
    if args.command == "capabilities":
        from ozagia.capabilities_cli import run as _capabilities_run
        return _capabilities_run(args)
    if args.command == "engine":
        from ozagia.engine_http import run as _engine_run
        return _engine_run(args)
    if args.command == "task-preflight":
        from ozagia.task_preflight_cli import run as _task_preflight_run
        return _task_preflight_run(args)
    if args.command in {"overview", "shape"}:
        from ozagia.overview_cli import run as _overview_run
        return _overview_run(args)
    if args.command == "languages":
        from ozagia.languages_cli import run as _languages_run
        return _languages_run(args)
    if args.command == "supply-chain":
        from ozagia.supply_chain_cli import run as _supply_chain_run
        return _supply_chain_run(args)
    if args.command == "migration":
        from ozagia.migration_cli import run as _migration_run
        return _migration_run(args)
    if args.command == "tmp":
        from ozagia.temp_cli import run as _temp_run
        return _temp_run(args)
    if args.command == "assets":
        from ozagia.assets_cli import run as _assets_run
        return _assets_run(args)
    if args.command == "mesh":
        from ozagia.mesh_cli import run as _mesh_run
        return _mesh_run(args)
    parser.error(f"unknown command {args.command}")  # pragma: no cover
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
