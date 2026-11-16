"""The ozagia command: the graph, the daemon, the integrations and the agent.

    ozagia doctor [--root R] [--json]
    ozagia index [--root R] [--rebuild]
    ozagia agent [--workdir W] [--prompt TEXT | --prompt-file F] [--model M]
                 [--mcp coding|research|combined | --research]
                 [--clone [--base REV]] [--keep-plugin] [--timeout S]
                 [--python P] [--runs-dir D] [-- <extra opencode args>]
    ozagia daemon serve|ensure|ping|stats|shutdown ... (python -m ozagia.daemon)
    ozagia bg run|wait|list|log|stop ...          (ozagia.daemon.jobcli)
    ozagia mcp ...                                (ozagia.integrations.mcp.server)
    ozagia eval ...                               (python -m ozagia.eval.agent)
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
    ozagia env list|show ...                      (declarative environment profiles)
    ozagia relay plan|run|tunnel ...              (nested command/network relays)
    ozagia runtime status|list ...                (configured services and tunnels)
    ozagia service start|run|stop|restart|status|logs NAME
    ozagia process start|run -- ARGV ...          (explicit local processes)
    ozagia tunnel start|stop|status|list ...      (tunnel config vs live state)
    ozagia preflight plan ...                     (dry-run emulator plan)
    ozagia --version

`daemon`, `bg`, `mcp`, `eval` and `check` pass every argument after their name to
those modules' `main`, unchanged. See docs/harness.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

PROG = "ozagia"


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
}


def _delegate(name: str) -> Callable[[list[str]], int]:
    return globals()[DELEGATES[name][0]]


def _root(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    from ozagia.config import repo_root
    return repo_root()



def _cmd_doctor(args: argparse.Namespace) -> int:
    from ozagia.doctor import diagnose
    report = diagnose(_root(args.root))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Repository: {report['root']}")
        print(f"Snapshot: {report['snapshot']['state']} ({report['snapshot']['path']})")
        config = report["config"]
        print(f"Config: {'valid' if config['valid'] else 'invalid'} ({config['path']})")
        for language, status in report["parsers"].items():
            if not status["available"]:
                print(f"Parser {language}: unavailable ({status['error']})")
        print(report["language_server_note"])
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
    from ozagia.harness.session import SessionError
    prompt = args.prompt
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot read --prompt-file: {exc}", file=sys.stderr)
            return 2
    try:
        res = run_agent(_root(args.workdir), prompt=prompt, model=args.model,
                        clone=args.clone, base=args.base, keep_plugin=args.keep_plugin,
                        timeout_s=args.timeout, extra_args=args.extra,
                        run_python=args.python, opencode=_opencode_exe(args.opencode),
                        runs_dir=args.runs_dir, mcp_mode=args.mcp)
    except (AgentError, SessionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ozagia agent: interrupted; daemon stopped and plugin removed",
              file=sys.stderr)
        return 130
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


def _cmd_env(args: argparse.Namespace) -> int:
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
    pr.add_argument("--prompt", default=None, help="run non-interactively with this prompt")
    pr.add_argument("--prompt-file", default=None, help="read the prompt from a file")
    ag.add_argument("--model", "-m", default=None, help="provider/model, e.g. vllm/qwen3.8-27b")
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

    for name, (_fn, text) in DELEGATES.items():
        sub.add_parser(name, help=text, add_help=False)

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

    env = sub.add_parser("env", help="declarative environment profiles")
    esub = env.add_subparsers(dest="action", required=True)
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
    return p


def _opencode_exe(value: str | None) -> str:
    import os
    if value == "system":
        return "opencode"
    return value or os.environ.get("OZAGIA_OPENCODE") or "opencode"


_COMMANDS = frozenset({"index", "agent", "install", *DELEGATES})
# Agent options that may lead a bare `ozagia ...` (the TUI).
_AGENT_FLAGS = frozenset({"--research", "--mcp", "--model", "-m", "--workdir",
                          "--root", "--clone", "--prompt", "--prompt-file"})


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
    argv = _default_agent(list(sys.argv[1:] if argv is None else argv))
    if argv[:2] == ["check", "iac"]:
        argv = ["iac", "check", *argv[2:]]
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
    parser.error(f"unknown command {args.command}")  # pragma: no cover
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
