"""`python -m ozagia.integrations.codex hook|install|uninstall`."""
from __future__ import annotations

import argparse
import sys

from ozagia.integrations.codex import hook as hook_mod
from ozagia.integrations.codex import install as install_mod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ozagia.integrations.codex",
                                 description="ozagia's Codex hooks and MCP server.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="run one hook event (Codex calls this; JSON on stdin)")
    for name, text in (("install", "add the hooks, the MCP server and the AGENTS.md "
                                   "section"),
                       ("uninstall", "remove what install added, keeping everything else")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--workdir", default=".", help="project directory (default: .)")
        p.add_argument("--scope", choices=install_mod.SCOPES, default="project",
                       help="project: <workdir>/.codex and <workdir>/AGENTS.md "
                            "(default); user: $CODEX_HOME (~/.codex)")
        if name == "install":
            p.add_argument("--python", default=sys.executable,
                           help="Python that runs the hook and the MCP server "
                                "(default: this one)")
    args = ap.parse_args(argv)
    if args.cmd == "hook":
        return hook_mod.main()
    try:
        if args.cmd == "install":
            got = install_mod.install(args.workdir, scope=args.scope, python=args.python)
            print(f"installed ozagia hooks in {got['hooks']}")
            print(f"installed the ozagia MCP server in {got['config']}")
            print(f"added the ozagia section to {got['agents']}")
            print(install_mod.TRUST_STEPS)
        else:
            changed = install_mod.uninstall(args.workdir, scope=args.scope)
            for path in changed:
                print(f"removed ozagia from {path}")
            if not changed:
                print("ozagia is not installed there")
    except (install_mod.SettingsError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
