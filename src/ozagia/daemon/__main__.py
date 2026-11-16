"""Command line for the graph daemon.

    python -m ozagia.daemon serve --root <repo> [--dump <graph>]
        [--journal <jsonl>] [--sock <path>] [--no-build] [--parent-pid PID]
        [--idle-timeout S]
    python -m ozagia.daemon ensure [--root <repo>] [--sock <path>]
        [--wait S] [--idle-timeout S]
    python -m ozagia.daemon ping|stats|shutdown [--root <repo> | --sock <path>]

`serve` prints `ready <socket path>` once it accepts connections; with
`--parent-pid` it exits when that process is gone, with `--idle-timeout`
after that many seconds without a request. `ensure` starts `serve` detached
when no daemon answers (`ozagia.daemon.autostart`), prints the result as
JSON and exits 1 when no daemon answered within `--wait`. The
client commands print the daemon's reply as JSON and exit 1 when no daemon
answers.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _root(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    from ozagia.config import repo_root
    return repo_root()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ozagia.daemon",
                                 description="The ozagia graph daemon.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="load the graph and listen")
    s.add_argument("--root", default=None)
    s.add_argument("--dump", default=None)
    s.add_argument("--journal", default=None)
    s.add_argument("--sock", default=None)
    s.add_argument("--no-build", action="store_true",
                   help="fail instead of building a missing snapshot")
    s.add_argument("--parent-pid", type=int, default=None,
                   help="shut down when this process (the harness) is gone")
    s.add_argument("--idle-timeout", type=float, default=None, metavar="S",
                   help="shut down after S seconds without a request (default: never)")
    s.add_argument("--lock-fd", type=int, default=None, help=argparse.SUPPRESS)
    e = sub.add_parser("ensure", help="start the daemon detached unless one answers")
    e.add_argument("--root", default=None)
    e.add_argument("--sock", default=None)
    e.add_argument("--wait", type=float, default=None, metavar="S",
                   help="seconds to wait for the daemon to answer (default: 10)")
    e.add_argument("--idle-timeout", type=float, default=None, metavar="S",
                   help="the started daemon's idle timeout (default: 1800; 0: never)")
    for name in ("ping", "stats", "shutdown"):
        c = sub.add_parser(name)
        c.add_argument("--root", default=None)
        c.add_argument("--sock", default=None)
        c.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args(argv)
    root = _root(args.root)

    if args.cmd == "serve":
        # ozagia.core.memory takes its layout from $OZAGIA_ROOT at import.
        os.environ["OZAGIA_ROOT"] = str(root)
        from ozagia.daemon.server import DaemonStartError, serve
        try:
            return serve(root, dump=args.dump, journal=args.journal,
                         sock=args.sock, build_if_missing=not args.no_build,
                         parent_pid=args.parent_pid,
                         idle_timeout=args.idle_timeout, lock_fd=args.lock_fd)
        except (DaemonStartError, FileNotFoundError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    if args.cmd == "ensure":
        from ozagia.daemon import autostart
        try:
            res = autostart.ensure(
                root, sock=args.sock,
                wait_s=autostart.DEFAULT_WAIT_S if args.wait is None else args.wait,
                idle_timeout_s=(autostart.DEFAULT_IDLE_TIMEOUT_S
                                if args.idle_timeout is None else args.idle_timeout))
        except OSError as e:
            res = {"ok": False, "started": False, "reason": str(e)}
        print(json.dumps(res, sort_keys=True))
        return 0 if res.get("ok") else 1

    from ozagia.daemon.client import DaemonClient, DaemonError
    from ozagia.daemon.protocol import socket_path
    sock = Path(args.sock) if args.sock else socket_path(root)
    try:
        with DaemonClient(sock, timeout=args.timeout) as c:
            reply = c.request(args.cmd)
    except DaemonError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(reply, indent=2, sort_keys=True))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
