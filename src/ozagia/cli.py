"""The ozagia command: the graph, the daemon, the integrations and the agent.

    ozagia doctor [--root R] [--json]
    ozagia index [--root R] [--rebuild]
    ozagia agent [--workdir W] [--prompt TEXT | --prompt-file F] [--model M]
                 [--mcp coding|research|combined | --research]
                 [--clone [--base REV]] [--keep-plugin] [--timeout S]
                 [--python P] [--runs-dir D] [-- <extra opencode args>]
    ozagia daemon serve|ensure|ping|stats|shutdown ... (python -m ozagia.daemon)
    ozagia job run|wait|list|log|stop ...         (ozagia.daemon.jobcli)
    ozagia mcp ...                                (ozagia.integrations.mcp.server)
    ozagia eval ...                               (python -m ozagia.eval.agent)
    ozagia check ...                              (python -m ozagia.check)
    ozagia install opencode|claude|codex [--workdir W] [...]
    ozagia self info|check|upgrade|uninstall ... (ozagia.selfinstall)
    ozagia [DIR]                                  the TUI on DIR (default: the
                                                  repository of the cwd), as
                                                  `ozagia agent --workdir DIR`
    ozagia --version

`daemon`, `job`, `mcp`, `eval` and `check` pass every argument after their name to
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


def _job_main(argv: list[str]) -> int:
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
    "job": ("_job_main", "background jobs in the daemon: run|wait|list|log|stop "
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
}


def _cmd_install(args: argparse.Namespace, extra: list[str]) -> int:
    import importlib
    main = importlib.import_module(INSTALL_TARGETS[args.target] + ".__main__").main
    return main(["install", "--workdir", args.workdir, *extra])


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
                         description="Install the OpenCode plugin, the Claude Code hooks "
                                     "or the Codex hooks and MCP server. Other options "
                                     "go to the integration's installer (for codex: "
                                     "--scope project|user, --python P).")
    ins.add_argument("target", choices=tuple(INSTALL_TARGETS))
    ins.add_argument("--workdir", default=".", help="project directory (default: .)")
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
    if argv and argv[0] in DELEGATES:
        return _delegate(argv[0])(argv[1:])
    extra: list[str] = []
    if argv[:1] == ["agent"] and "--" in argv:
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
    parser.error(f"unknown command {args.command}")  # pragma: no cover
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
