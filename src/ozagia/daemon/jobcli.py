"""Background jobs from outside the daemon: `ozagia bg ...` and the MCP tools.

    ozagia bg run [--root R] [--cwd D] [--env K=V]... [--detach] -- CMD [ARG...]
    ozagia bg wait [JOB|any] [--until exit|file:P|log:RX] [--deadline S] [--cursor N]
    ozagia bg list
    ozagia bg log JOB [--cursor N] [--tail N]
    ozagia bg stop JOB [--signal TERM|KILL|INT|HUP] [--grace S]

Every command takes `--root` (default: the repository of the cwd) and
`--json` (print the daemon's reply). `run` with one word after `--` runs it
as a shell command (`bash -c`), with several as an argv. `wait` and `log`
print the new log lines; their read position belongs to the shell the
command runs from (reader `cli:<parent pid>:<tty>`, `cli_reader`), so two
terminals do not take each other's lines; `--cursor N` reads from byte N
instead. `wait` exits 0 when the condition held, 1 at the deadline, and
every command exits 2 when the daemon is unreachable or refuses the
request (`run` too when jobs are off for the project: see
`ozagia.daemon.jobs`).

The jobs live in the daemon (`ozagia.daemon.jobs`); `request` reaches it
over its socket and starts it when nothing answers
(`hookcore.daemon_request_autostart`: only for a git work tree,
`$OZAGIA_ROOT` or a root with a graph snapshot; off with
`$OZAGIA_DAEMON_AUTOSTART=0`).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# How long a caller waits for a daemon it starts.
AUTOSTART_BUDGET_S = 10.0
# The request timeout beyond a wait's deadline.
WAIT_MARGIN_S = 15.0
REQUEST_TIMEOUT_S = 20.0


class JobUnavailable(RuntimeError):
    """No daemon could be reached, or it refused the request.

    ``refused`` is true when a daemon answered with ``ok: false`` (``code`` is
    its stable reason); false when no daemon answered or the transport failed.
    """

    def __init__(self, message: str, *, code: str | None = None,
                 refused: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.refused = refused


def request(root: str | Path, op: str, fields: dict[str, Any], *,
            budget_s: float = AUTOSTART_BUDGET_S) -> dict[str, Any]:
    """Send one job op to `root`'s daemon (starting it if needed) and return
    its `ok` reply. Raises `JobUnavailable` with a message for the user."""
    from ozagia.integrations import hookcore
    root_p = Path(root).resolve()
    timeout = REQUEST_TIMEOUT_S
    if op in ("job_wait", "job_events"):
        d = fields.get("deadline_s")
        from ozagia.daemon.jobs import WAIT_DEFAULT_S
        timeout = float(d if isinstance(d, (int, float)) else WAIT_DEFAULT_S) + WAIT_MARGIN_S
    if op == "job_stop":
        g = fields.get("grace_s")
        timeout += float(g) if isinstance(g, (int, float)) else 0.0
    msg = {"id": 1, "op": op, **{k: v for k, v in fields.items() if v is not None}}
    try:
        return hookcore.daemon_request_autostart(root_p, msg, timeout=timeout,
                                                 budget_s=budget_s)
    except hookcore.AutostartRefused as e:
        raise JobUnavailable(
            f"no ozagia daemon answers for {root_p}, and none is started there "
            f"({e}); jobs live in the daemon: start one with "
            f"`ozagia daemon ensure --root {root_p}`") from None
    except hookcore.HookError as e:
        text = str(e)
        if text.startswith("daemon error: "):
            raise JobUnavailable(
                text[len("daemon error: "):],
                code=getattr(e, "code", None),
                refused=True) from None
        raise JobUnavailable(
            f"no ozagia daemon could be reached for {root_p}: {text}; jobs live "
            f"in the daemon: start one with `ozagia daemon ensure --root {root_p}`"
        ) from None
    except (OSError, ValueError) as e:
        raise JobUnavailable(
            f"no ozagia daemon could be reached for {root_p}: "
            f"{type(e).__name__}: {e}") from None


def _status_line(r: dict[str, Any]) -> str:
    job = r.get("job") if isinstance(r.get("job"), str) else r.get("id")
    st = r.get("status")
    bits = [f"job {job}" if job else "jobs", str(st)]
    if r.get("exit_code") is not None and st in ("exited", "killed"):
        bits.append(f"exit {r['exit_code']}")
    if r.get("signal"):
        bits.append(f"signal {r['signal']}")
    if r.get("elapsed_s") is not None:
        bits.append(f"{r['elapsed_s']} s")
    if "met" in r:
        bits.append("condition met" if r["met"] else "condition not met")
    if r.get("skipped_lines") or r.get("skipped_bytes"):
        bits.append(f"{r.get('skipped_lines', 0)} older lines and "
                    f"{r.get('skipped_bytes', 0)} bytes skipped")
    if r.get("cursor") is not None:
        bits.append(f"cursor {r['cursor']}")
    return "[ozagia] " + ", ".join(bits)


def cli_reader() -> str:
    """The CLI's reader name: the parent process (the shell) and the
    terminal, so each shell keeps its own place in a job's log."""
    tty = "-"
    for fd in (0, 1, 2):
        try:
            tty = os.ttyname(fd)
            break
        except OSError:
            continue
    return f"cli:{os.getppid()}:{tty}"


def _env_pairs(values: list[str]) -> dict[str, str]:
    out = {}
    for v in values:
        k, sep, val = v.partition("=")
        if not sep or not k:
            raise SystemExit(f"--env takes NAME=VALUE, not {v!r}")
        out[k] = val
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ozagia bg",
                                 description="Background jobs in the ozagia daemon.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None)
    common.add_argument("--json", action="store_true", help="print the daemon's reply")
    r = sub.add_parser("run", parents=[common], help="start a job")
    r.add_argument("--cwd", default=None, help="directory inside the root")
    r.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument("--detach", action="store_true",
                   help="leave it running when the daemon exits")
    r.add_argument("words", nargs=argparse.REMAINDER)
    w = sub.add_parser("wait", parents=[common], help="wait for a job")
    w.add_argument("job", nargs="?", default="any")
    w.add_argument("--until", default="exit")
    w.add_argument("--deadline", type=float, default=None, metavar="S")
    w.add_argument("--cursor", type=int, default=None)
    sub.add_parser("list", parents=[common], help="list jobs")
    lg = sub.add_parser("log", parents=[common], help="new log lines of a job")
    lg.add_argument("job")
    lg.add_argument("--cursor", type=int, default=None)
    lg.add_argument("--tail", type=int, default=None)
    st = sub.add_parser("stop", parents=[common], help="stop a job's process group")
    st.add_argument("job")
    st.add_argument("--signal", default="TERM")
    st.add_argument("--grace", type=float, default=None, metavar="S")
    args = ap.parse_args(argv)
    if args.root:
        root = Path(args.root).resolve()
    else:
        from ozagia.config import repo_root
        root = repo_root()

    if args.cmd == "run":
        words = list(args.words)
        if words[:1] == ["--"]:
            words = words[1:]
        if not words:
            ap.error("run needs a command after --")
        fields: dict[str, Any] = (
            {"command": words[0]} if len(words) == 1 else {"argv": words})
        fields.update(cwd=args.cwd, env=_env_pairs(args.env) or None,
                      detach=True if args.detach else None,
                      harness="cli", session="cli")
        op = "job_start"
    elif args.cmd == "wait":
        op = "job_wait"
        fields = {"job": args.job, "until": args.until, "deadline_s": args.deadline,
                  "cursor": args.cursor, "reader": cli_reader()}
    elif args.cmd == "list":
        op, fields = "job_list", {}
    elif args.cmd == "log":
        op = "job_log"
        fields = {"job": args.job, "cursor": args.cursor, "tail": args.tail,
                  "reader": cli_reader()}
    else:
        op = "job_stop"
        fields = {"job": args.job, "signal": args.signal, "grace_s": args.grace}
    try:
        reply = request(root, op, fields)
    except JobUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(reply, indent=2, sort_keys=True))
    elif op == "job_start":
        job = reply["job"]
        print(f"{job['id']} pid {job['pid']} log {job['log']}")
    elif op == "job_list":
        for j in reply.get("jobs") or []:
            code = "" if j.get("exit_code") is None else f" exit {j['exit_code']}"
            print(f"{j['id']}\t{j['status']}{code}\t{j['elapsed_s']} s\t"
                  f"pid {j['pid']}\t{j['command']}")
    else:
        if reply.get("output"):
            print(reply["output"])
        print(_status_line(reply))
    if op == "job_wait":
        return 0 if reply.get("met") else 1
    return 0


__all__ = ["JobUnavailable", "cli_reader", "main", "request"]
