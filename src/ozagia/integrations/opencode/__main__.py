"""`python -m ozagia.integrations.opencode install|uninstall|check --workdir W`."""
from __future__ import annotations

import argparse
import json
import sys

from ozagia.integrations.opencode.install import (
    check_loads,
    install,
    plugin_path,
    tui_brand_path,
    uninstall,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m ozagia.integrations.opencode",
                                description="Manage the ozagia OpenCode plugin in a workdir.")
    p.add_argument("command", choices=("install", "uninstall", "check"))
    p.add_argument("--workdir", required=True, help="project directory")
    p.add_argument("--timeout", type=float, default=60, help="check: seconds to wait")
    args = p.parse_args(argv)
    if args.command == "install":
        install(args.workdir)
        # The server plugin and the TUI brand plugin are siblings in the same
        # directory; report both.
        print(plugin_path(args.workdir))
        print(tui_brand_path(args.workdir))
        return 0
    if args.command == "uninstall":
        print("removed" if uninstall(args.workdir) else "not installed")
        return 0
    res = check_loads(args.workdir, timeout=args.timeout)
    print(json.dumps(res, indent=2, sort_keys=True))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
