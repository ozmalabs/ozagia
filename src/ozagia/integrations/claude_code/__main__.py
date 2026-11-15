"""`python -m ozagia.integrations.claude_code install|uninstall|hook`."""
from __future__ import annotations

import argparse
import sys

from ozagia.integrations.claude_code import hook as hook_mod
from ozagia.integrations.claude_code import install as install_mod


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ozagia.integrations.claude_code",
                                 description="ozagia's Claude Code hooks.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("hook", help="run one hook event (Claude Code calls this; JSON on stdin)")
    for name, text in (("install", "add the hooks to the project's Claude Code settings"),
                       ("uninstall", "remove ozagia's hooks, keeping everything else")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--workdir", default=".", help="project directory (default: .)")
        p.add_argument("--settings", choices=sorted(install_mod.SETTINGS_FILES),
                       default=None,
                       help="local: .claude/settings.local.json (install default); "
                            "project: .claude/settings.json. Uninstall cleans both "
                            "unless given.")
        if name == "install":
            p.add_argument("--python", default=sys.executable,
                           help="Python that runs the hook (default: this one)")
    args = ap.parse_args(argv)
    if args.cmd == "hook":
        return hook_mod.main()
    try:
        if args.cmd == "install":
            path = install_mod.install(args.workdir, python=args.python,
                                       settings=args.settings or "local")
            print(f"installed ozagia hooks in {path}")
        else:
            changed = install_mod.uninstall(args.workdir, settings=args.settings)
            for path in changed:
                print(f"removed ozagia hooks from {path}")
            if not changed:
                print("no ozagia hooks found")
    except (install_mod.SettingsError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
