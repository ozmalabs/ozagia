#!/usr/bin/env python3
"""A probe hook for Kiro CLI: records what Kiro sends to a command hook.

Things only a live Kiro session can show (see docs/kiro.md, "Open
questions"): the exact stdin of each event per engine, the tool names and
input fields, the shape of `tool_response`, and whether a hook's stdout
reaches the model on PostToolUse. This script answers them without ozagia.

    python3 scripts/kiro_probe_hook.py install --workdir /tmp/kiro-probe [--echo]
    cd /tmp/kiro-probe && git init -q && echo 'x = 1' > a.py
    kiro-cli chat --v3                       # the V3 engine: hook file
    kiro-cli chat --v2 --agent ozagia-probe  # the V2 engine: agent profile
    python3 scripts/kiro_probe_hook.py show --workdir /tmp/kiro-probe
    python3 scripts/kiro_probe_hook.py uninstall --workdir /tmp/kiro-probe

`install` copies this script to `<workspace>/.ozagia/kiro_probe_hook.py`
and writes `.kiro/hooks/ozagia-probe.json` (V3) and
`.kiro/agents/ozagia-probe.json` (every engine, with that agent) into the
given workspace, both running `<this python> <the copy> hook || true`. The
copy and the `|| true` keep the hook at exit 0 even if this checkout moves
or the interpreter fails (Kiro blocks a tool on exit 2, which is what
Python exits with when it cannot open its script). Use a scratch directory,
not a real project, and trust it when Kiro asks.

`hook` (what Kiro runs) reads stdin and writes one JSON file per event to
`$OZAGIA_KIRO_PROBE_DIR`, default `<event cwd>/.ozagia/kiro_probe/`: the raw
stdin, the parsed event, argv, the working directory and the *names* of the
environment variables (never their values). It prints nothing and exits 0,
so it changes nothing in the session. With `--echo` (install it with
`--echo`) it prints one marker line, `ozagia-probe-marker <event>
<tool>`, on stdout; ask the agent afterwards whether it saw a marker after
a tool call to learn whether PostToolUse stdout reaches the model.

`show` prints a summary of the recorded events (event, tool, input keys,
response type), not their content.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path

NAME = "ozagia-probe"
MARKER = "ozagia-probe-marker"


def _probe_dir(cwd: str | None) -> Path:
    env = os.environ.get("OZAGIA_KIRO_PROBE_DIR")
    if env:
        return Path(env)
    base = Path(cwd) if cwd and os.path.isdir(cwd) else Path.cwd()
    return base / ".ozagia" / "kiro_probe"


def cmd_hook(echo: bool) -> int:
    raw = sys.stdin.read()
    try:
        parsed = json.loads(raw)
        error = None
    except ValueError as exc:
        parsed, error = None, str(exc)
    p = parsed if isinstance(parsed, dict) else {}
    event = str(p.get("hook_event_name") or "unknown")
    record = {"ts": time.time(), "argv": sys.argv, "cwd": os.getcwd(),
              "stdin": raw, "event": parsed, "parse_error": error,
              "env_names": sorted(os.environ)}
    out = _probe_dir(p.get("cwd"))
    try:
        out.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}-{os.getpid()}-{event}.json"
        (out / name).write_text(json.dumps(record, indent=2, ensure_ascii=False),
                                encoding="utf-8")
    except OSError:
        pass
    if echo:
        print(f"{MARKER} {event} {p.get('tool_name') or '-'}")
    return 0


def script_copy(workdir: str) -> Path:
    """Where install copies this script in the workspace."""
    return Path(workdir).resolve() / ".ozagia" / "kiro_probe_hook.py"


def _command(echo: bool, script: Path) -> str:
    parts = [sys.executable, str(script), "hook"]
    if echo:
        parts.append("--echo")
    # Never exit non-zero: exit 2 on PreToolUse would block every tool.
    return " ".join(shlex.quote(x) for x in parts) + " || true"


def configs(echo: bool, script: Path) -> tuple[dict, dict]:
    command = _command(echo, script)

    def doc(trigger: str, matcher: str | None = None) -> dict:
        d: dict = {"name": f"{NAME}-{trigger}", "trigger": trigger}
        if matcher:
            d["matcher"] = matcher
        d["action"] = {"type": "command", "command": command}
        d["timeout"] = 10
        return d

    # PreCompact/PostCompact need CLI 3.1.0+; older V3 skips them with a warning.
    hook_file = {"version": "v1", "hooks": [
        doc("SessionStart"), doc("UserPromptSubmit"), doc("PreToolUse", "*"),
        doc("PostToolUse", "*"), doc("Stop"), doc("PreCompact"), doc("PostCompact")]}
    profile = {"name": NAME, "description": "ozagia probe: records hook stdin",
               "tools": ["*"], "includeMcpJson": True, "hooks": [
                   doc("agentSpawn"), doc("userPromptSubmit"), doc("preToolUse", "*"),
                   doc("postToolUse", "*"), doc("stop")]}
    return hook_file, profile


def _paths(workdir: str) -> tuple[Path, Path]:
    base = Path(workdir).resolve() / ".kiro"
    return base / "hooks" / f"{NAME}.json", base / "agents" / f"{NAME}.json"


def cmd_install(workdir: str, echo: bool) -> int:
    script = script_copy(workdir)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(Path(__file__).read_bytes())
    print(f"wrote {script}")
    hook_file, profile = configs(echo, script)
    for path, data in zip(_paths(workdir), (hook_file, profile)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    return 0


def cmd_uninstall(workdir: str) -> int:
    for path in (*_paths(workdir), script_copy(workdir)):
        if path.exists():
            path.unlink()
            print(f"removed {path}")
    return 0


def cmd_show(workdir: str) -> int:
    d = Path(os.environ.get("OZAGIA_KIRO_PROBE_DIR")
             or Path(workdir).resolve() / ".ozagia" / "kiro_probe")
    for f in sorted(d.glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        ev = rec.get("event") if isinstance(rec.get("event"), dict) else {}
        ti = ev.get("tool_input")
        tr = ev.get("tool_response")
        print(json.dumps({
            "file": f.name, "keys": sorted(ev),
            "hook_event_name": ev.get("hook_event_name"),
            "tool_name": ev.get("tool_name"), "tool_tags": ev.get("tool_tags"),
            "tool_input_keys": sorted(ti) if isinstance(ti, dict) else type(ti).__name__,
            "tool_response_type": type(tr).__name__,
            "tool_response_keys": sorted(tr) if isinstance(tr, dict) else None}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook", help="record one event (Kiro runs this)")
    h.add_argument("--echo", action="store_true", help="print a marker line on stdout")
    for name in ("install", "uninstall", "show"):
        p = sub.add_parser(name)
        p.add_argument("--workdir", required=True, help="a scratch Kiro workspace")
        if name == "install":
            p.add_argument("--echo", action="store_true",
                           help="the hooks print a marker line on stdout")
    args = ap.parse_args(argv)
    if args.cmd == "hook":
        try:
            return cmd_hook(args.echo)
        except Exception:  # a probe must never disturb the session
            return 0
    if args.cmd == "install":
        return cmd_install(args.workdir, args.echo)
    if args.cmd == "uninstall":
        return cmd_uninstall(args.workdir)
    return cmd_show(args.workdir)


if __name__ == "__main__":
    sys.exit(main())
