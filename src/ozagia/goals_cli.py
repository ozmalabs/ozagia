"""`ozagia goal`: graph-addressed goals (`ozagia.core.goals`).

    ozagia goal add --title T [--why W] [--target KEY ...] [--scope KEY|GLOB ...]
                    [--criterion kind:args ...] [--file goal.toml]
    ozagia goal list [--all | --status S]
    ozagia goal show ID
    ozagia goal check [ID ...] [--no-refresh]
    ozagia goal claim ID --harness H --session S [--release]
    ozagia goal drop ID [--reason R]
    ozagia goal block ID --reason R | --clear
    ozagia goal from-review PACKET FINDING_ID

Every command takes `--root R` and `--json`. `add`, `check` and
`from-review` load the graph (`<state dir>/graph.json`, built first when
missing); the others read and write only the goal store.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


def add_parser(sub: Any) -> None:
    goal = sub.add_parser("goal", help="graph-addressed goals: add|list|show|check|"
                                       "claim|drop|block|from-review")
    gsub = goal.add_subparsers(dest="action", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--root", default=None, help="repository root")
        p.add_argument("--json", action="store_true", help="print JSON")
        return p

    add = common(gsub.add_parser("add", help="create a goal (keys resolved against the graph)"))
    add.add_argument("--title", default=None)
    add.add_argument("--why", default=None, help="free text")
    add.add_argument("--target", action="append", default=[],
                     help="graph key the work is about (repeatable)")
    add.add_argument("--scope", action="append", default=[],
                     help="graph key or path glob the work may change (repeatable)")
    add.add_argument("--criterion", action="append", default=[],
                     help="kind:args, e.g. test_passes:tests/t.py::test_x (repeatable)")
    add.add_argument("--file", default=None, help="goal.toml with the same fields")
    lst = common(gsub.add_parser("list", help="list goals"))
    lst.add_argument("--all", action="store_true", help="include dropped goals")
    lst.add_argument("--status", default=None, help="only this status")
    show = common(gsub.add_parser("show", help="one goal with its criteria and history"))
    show.add_argument("id")
    chk = common(gsub.add_parser("check", help="evaluate criteria, record results"))
    chk.add_argument("ids", nargs="*", help="goal ids (default: every goal not dropped)")
    chk.add_argument("--no-refresh", action="store_true",
                     help="do not take pending file changes into the in-memory graph")
    claim = common(gsub.add_parser("claim", help="claim a goal for a harness session"))
    claim.add_argument("id")
    claim.add_argument("--harness", required=True)
    claim.add_argument("--session", required=True)
    claim.add_argument("--release", action="store_true", help="release the claim")
    drop = common(gsub.add_parser("drop", help="drop a goal"))
    drop.add_argument("id")
    drop.add_argument("--reason", default="")
    block = common(gsub.add_parser("block", help="block a goal (with a reason)"))
    block.add_argument("id")
    block.add_argument("--reason", default="")
    block.add_argument("--clear", action="store_true", help="unblock (back to open)")
    fr = common(gsub.add_parser("from-review", help="a goal from a repo-review finding"))
    fr.add_argument("packet", help="ozagia.repo_review.packet.v1 JSON file under "
                    "<state dir>/reviews/")
    fr.add_argument("finding_id")


def _root(value: str | None) -> Path:
    if value:
        return Path(value).resolve()
    from ozagia.config import repo_root
    return repo_root()


def _memory(root: Path) -> Any:
    from ozagia.config import state_path
    from ozagia.core.memory import DevMemory, build_index
    dump = state_path(root, "graph.json")
    if not dump.exists():
        print(f"building {dump} ...", file=sys.stderr)
        build_index(root, dump, include_docs="features", include_git=True,
                    include_scripts=True)
    return DevMemory(dump, state_path(root, "journal.jsonl"), root=root)


def _clock(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return "-"


def format_goal(goal: dict[str, Any], *, history: int = 10) -> str:
    from ozagia.core import goals as G
    lines = [f"{goal['id']} [{goal.get('status')}] {goal.get('title')}"]
    if goal.get("why"):
        lines.append(f"  why: {goal['why']}")
    if goal.get("blocked_reason") and goal.get("status") == "blocked":
        lines.append(f"  blocked: {goal['blocked_reason']}")
    lines.append("  targets: " + (", ".join(goal.get("targets") or []) or "-"))
    lines.append("  scope: " + (", ".join(goal.get("scope") or []) or "-"))
    claims = [f"{c.get('harness')}/{c.get('session')}" for c in goal.get("claimed_by") or []]
    lines.append("  claimed by: " + (", ".join(claims) or "-"))
    src = goal.get("source") or {}
    if src:
        lines.append(f"  source: {src.get('review_packet')} finding {src.get('finding_id')}")
    lines.append(f"  last check: {_clock(goal.get('checked_at'))}")
    lines.append("  criteria:")
    for c in goal.get("criteria") or []:
        last = c.get("last") or {}
        status = last.get("status") or "unchecked"
        reason = f": {last['reason']}" if last.get("reason") else ""
        lines.append(f"    {status:9} {G.criterion_label(c)}{reason}")
    hist = goal.get("history") or []
    if history and hist:
        lines.append("  history:")
        for h in hist[-history:]:
            detail = h.get("detail")
            d = f" {json.dumps(detail, sort_keys=True)}" if detail else ""
            lines.append(f"    {_clock(h.get('at'))} {h.get('event')} by {h.get('by')}{d}")
    return "\n".join(lines)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def run(args: argparse.Namespace) -> int:
    from ozagia.core import goals as G
    root = _root(args.root)
    try:
        return _run(args, root)
    except G.GoalError as e:
        if args.json:
            _print({"ok": False, "error": str(e)})
        else:
            print(f"ozagia goal: {e}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace, root: Path) -> int:
    from ozagia.core import goals as G
    act = args.action
    if act == "add":
        spec: dict[str, Any] = {}
        if args.file:
            spec = dict(G.load_goal_file(args.file))
        if args.title:
            spec["title"] = args.title
        if args.why:
            spec["why"] = args.why
        for name, extra in (("targets", args.target), ("scope", args.scope),
                            ("criteria", args.criterion)):
            if extra:
                base = spec.get(name) or []
                spec[name] = (list(base) if isinstance(base, list) else [base]) + list(extra)
        goal = G.add_goal(root, _memory(root), spec, by="cli")
        if args.json:
            _print({"ok": True, "goal": goal})
        else:
            print(format_goal(goal, history=0))
            for w in goal.get("warnings") or []:
                print(f"ozagia goal: warning: {w}", file=sys.stderr)
        return 0
    if act == "list":
        goals = G.list_goals(root, status=args.status, include_dropped=args.all)
        if args.json:
            _print({"ok": True, "goals": [G.summary(g) for g in goals]})
            return 0
        if not goals:
            print("no goals")
        for g in goals:
            s = G.summary(g)
            claims = f" claimed by {', '.join(s['claimed_by'])}" if s["claimed_by"] else ""
            print(f"{s['id']} [{s['status']}] {s['title']} "
                  f"({len(s['unmet'])}/{s['criteria']} unmet){claims}")
        return 0
    if act == "show":
        goal = G.get_goal(root, args.id)
        if args.json:
            _print({"ok": True, "goal": goal})
        else:
            print(format_goal(goal))
        return 0
    if act == "check":
        reports = G.check_goals(root, _memory(root), args.ids or None, by="cli",
                                refresh=not args.no_refresh)
        if args.json:
            _print({"ok": True, "goals": reports})
            return 0
        if not reports:
            print("no goals to check")
        for r in reports:
            moved = f" (was {r['previous']})" if r["previous"] != r["status"] else ""
            print(f"{r['id']} [{r['status']}]{moved} {r['title']}")
            for res in r["results"]:
                print(f"    {res['status']:7} {res['id']} {res['kind']}: {res['reason']}")
        return 0
    if act == "claim":
        goal = G.claim_goal(root, args.id, args.harness, args.session,
                            release=args.release, by="cli")
        if args.json:
            _print({"ok": True, "goal": G.summary(goal)})
        else:
            verb = "released" if args.release else "claimed"
            print(f"{goal['id']} {verb} for {args.harness}/{args.session}")
        return 0
    if act == "drop":
        goal = G.drop_goal(root, args.id, reason=args.reason, by="cli")
        if args.json:
            _print({"ok": True, "goal": G.summary(goal)})
        else:
            print(f"{goal['id']} dropped")
        return 0
    if act == "block":
        goal = G.block_goal(root, args.id, reason=args.reason, clear=args.clear, by="cli")
        if args.json:
            _print({"ok": True, "goal": G.summary(goal)})
        else:
            print(f"{goal['id']} {goal['status']}")
        return 0
    if act == "from-review":
        packet = str(Path(args.packet).expanduser().resolve())
        goal = G.from_review(root, _memory(root), packet, args.finding_id, by="cli")
        if args.json:
            _print({"ok": True, "goal": goal})
        else:
            print(format_goal(goal, history=0))
        return 0
    raise G.GoalError(f"unknown goal action {act}")  # pragma: no cover
