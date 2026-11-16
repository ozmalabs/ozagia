"""`python -m ozagia.integrations.kiro hook|install|uninstall`."""
from __future__ import annotations

import argparse
import sys

from ozagia.integrations.kiro import hook as hook_mod
from ozagia.integrations.kiro import install as install_mod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ozagia.integrations.kiro",
                                 description="ozagia's Kiro CLI hooks and MCP server.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="run one hook event (Kiro calls this; JSON on stdin)")
    for name, text in (("install", "add the hooks and the MCP server"),
                       ("uninstall", "remove what install added, keeping everything else")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--workdir", default=".", help="project directory (default: .)")
        p.add_argument("--scope", choices=install_mod.SCOPES, default="project",
                       help="project: <workdir>/.kiro (default); user: ~/.kiro")
        if name == "install":
            p.add_argument("--agent", default=install_mod.DEFAULT_AGENT,
                           help="agent profile for --format agent|both: "
                                ".kiro/agents/<AGENT>.json (default: ozagia)")
        else:
            p.add_argument("--agent", default=None,
                           help="only this agent profile, .kiro/agents/<AGENT>.json "
                                "(default: every .kiro/agents/*.json)")
        if name == "install":
            p.add_argument("--format", dest="fmt", choices=install_mod.FORMATS,
                           default=install_mod.DEFAULT_FORMAT,
                           help="v3: .kiro/hooks/ozagia.json, loaded by the V3 engine "
                                "(default); agent: hooks in the agent profile, loaded "
                                "by every engine when that agent is used; both")
            p.add_argument("--python", default=sys.executable,
                           help="Python that runs the hook and the MCP server "
                                "(default: this one)")
    args = ap.parse_args(argv)
    if args.cmd == "hook":
        return hook_mod.main()
    try:
        if args.cmd == "install":
            got = install_mod.install(args.workdir, scope=args.scope, python=args.python,
                                      fmt=args.fmt, agent=args.agent)
            if "hooks" in got:
                print(f"installed ozagia hooks in {got['hooks']}")
            if "agent" in got:
                print(f"installed ozagia hooks in the agent profile {got['agent']}")
            print(f"installed the ozagia MCP server in {got['mcp']}")
            print(install_mod.NEXT_STEPS)
        else:
            changed = install_mod.uninstall(args.workdir, scope=args.scope,
                                            agent=args.agent)
            for path in changed:
                print(f"removed ozagia from {path}")
            if not changed:
                print("ozagia is not installed there")
    except (install_mod.SettingsError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
