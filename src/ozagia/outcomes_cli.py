"""Read-only ``ozagia outcomes`` CLI (Stage 1, S1-C).

    ozagia outcomes show INTENT [--events-limit N]
    ozagia outcomes list [--limit N]
    ozagia outcomes events INTENT [--after SEQ] [--limit N]

Every command takes ``--root R``, ``--state-dir DIR`` and ``--json``.  The
commands only read the durable outcome journal through
:mod:`ozagia.core.outcomes.view` and ``OutcomeStore.follow``.  They never
append, recover, truncate or create the store.

Standalone use::

    python -m ozagia.outcomes_cli show INTENT --root R --state-dir DIR --json

Top-level integration step (deliberately not applied in this slice because it
also updates the command/capability inventory): register ``add_parser`` in
``ozagia.cli.build_parser``, add ``outcomes`` to ``cli._COMMANDS``, and dispatch
the parsed ``outcomes`` namespace to ``run`` in ``ozagia.cli.main``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core.outcomes.errors import OutcomeError
from ozagia.core.outcomes.store import (
    DEFAULT_ACTIVE,
    DEFAULT_EVENTS,
    DEFAULT_FOLLOW,
    OutcomeStore,
)
from ozagia.core.outcomes.view import (
    KIND_LIST,
    VIEW_SCHEMA,
    list_view,
    view,
)

PROG = "ozagia outcomes"


def _common(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--root", default=None, help="repository root")
    parser.add_argument("--state-dir", default=None,
                        help="explicit outcome state directory")
    parser.add_argument("--json", action="store_true",
                        help="print versioned JSON")
    return parser


def _add_commands(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    show = _common(sub.add_parser(
        "show", help="show one intent as ozagia.outcomes.view.v1"))
    show.add_argument("intent", help="intent id")
    show.add_argument("--events-limit", type=int, default=DEFAULT_EVENTS,
                      help=f"recent event rows (default {DEFAULT_EVENTS})")
    listing = _common(sub.add_parser(
        "list", help="list bounded intent rows"))
    listing.add_argument("--limit", type=int, default=DEFAULT_ACTIVE,
                         help=f"maximum rows (default {DEFAULT_ACTIVE})")
    events = _common(sub.add_parser(
        "events", help="read the journal event cursor for one intent"))
    events.add_argument("intent", help="intent id")
    events.add_argument("--after", type=int, default=0,
                        help="exclusive sequence cursor (default 0)")
    events.add_argument("--limit", type=int, default=DEFAULT_FOLLOW,
                        help=f"maximum records (default {DEFAULT_FOLLOW})")


def add_parser(sub: Any) -> None:
    """Register ``ozagia outcomes`` on a top-level subparser collection."""
    parser = sub.add_parser(
        "outcomes",
        help="read-only durable outcome views: show|list|events",
        description="Read the durable outcome journal without writing it.")
    _add_commands(parser)


def _root(value: str | None) -> Path:
    return (Path(value).expanduser().resolve()
            if value else repo_root())


def _events(store: OutcomeStore, intent_id: str, after: int,
            limit: int) -> dict[str, Any]:
    page = store.follow(after, intent_id=intent_id, limit=limit)
    result = {
        "schema": VIEW_SCHEMA,
        "kind": "events",
        "intent_id": intent_id,
        "after_seq": after,
    }
    result.update(page)
    return result


def _text_show(payload: dict[str, Any]) -> list[str]:
    intent = payload.get("intent") or {}
    lines = [
        f"{intent.get('intent_id')} [{payload.get('status')}] "
        f"revision {intent.get('revision')}: "
        f"{intent.get('requested_outcome')}",
    ]
    principal = intent.get("principal")
    if principal:
        lines.append(f"  principal: {principal}")
    criteria = payload.get("criteria") or []
    if criteria:
        lines.append("  criteria:")
        for row in criteria:
            lines.append(f"    [{row.get('status')}] {row.get('criterion')}")
    outcome = payload.get("outcome")
    if outcome:
        detail = outcome.get("summary") or outcome.get("detail") or ""
        lines.append(
            f"  outcome: {outcome.get('status')} ({outcome.get('record_id')})"
            + (f" - {detail}" if detail else ""))
    else:
        lines.append("  outcome: none")
    evidence = payload.get("evidence") or {}
    verified = evidence.get("verified") or []
    pending = evidence.get("pending") or []
    lines.append(
        f"  evidence: {len(verified)} verified, {len(pending)} pending")
    work = payload.get("current_work")
    if work:
        lines.append(
            f"  work: {work.get('work_id')} [{work.get('state')}] "
            f"{work.get('title')}"
            + ("" if work.get("revision_current") else " (stale revision)"))
    attempt = payload.get("current_attempt")
    if attempt:
        lines.append(
            f"  attempt: {attempt.get('attempt_id')} "
            f"[{attempt.get('state')}/{attempt.get('outcome') or '-'}]")
    pending_decision = payload.get("pending_decision")
    if pending_decision:
        lines.append(
            f"  pending decision: {pending_decision.get('gate_id')} "
            f"(required={pending_decision.get('required')})")
    blocker = payload.get("blocker")
    if blocker:
        lines.append(f"  blocker: {blocker.get('kind')}: "
                     f"{blocker.get('summary')}")
    operations = [
        item.get("id")
        for item in payload.get("operations") or []
        if item.get("enabled")
    ]
    lines.append("  operations: " + (", ".join(operations) or "-"))
    other = payload.get("other") or []
    if other:
        lines.append(f"  unrecognised records: {len(other)} (preserved)")
    lines.append(
        f"  last sequence: {payload.get('last_updated_seq')} "
        f"(cursor {payload.get('cursor')})")
    return lines


def _text_list(payload: dict[str, Any]) -> list[str]:
    rows = payload.get("intents") or []
    lines = [
        f"{payload.get('count')} intent(s), total {payload.get('total')}, "
        f"cursor {payload.get('cursor')}",
    ]
    for row in rows:
        work = row.get("current_work")
        work_text = (
            f" work={work.get('work_id')}[{work.get('state')}]"
            if work else " work=-")
        pending = row.get("pending_decision")
        decision_text = (
            f" decision={pending.get('gate_id')}" if pending else "")
        blocker = row.get("blocker")
        blocker_text = (
            f" blocker={blocker.get('kind')}" if blocker else "")
        outcome = row.get("outcome")
        outcome_text = (
            f" outcome={outcome.get('status')}" if outcome else "")
        lines.append(
            f"{row.get('intent_id')} [{row.get('state')}] "
            f"rev {row.get('revision')} seq {row.get('last_seq')}"
            f"{work_text}{decision_text}{blocker_text}{outcome_text}")
    if payload.get("truncated"):
        lines.append("  more rows omitted by --limit")
    return lines


def _text_events(payload: dict[str, Any]) -> list[str]:
    records = payload.get("records") or []
    lines = [
        f"{payload.get('intent_id')} events after "
        f"{payload.get('after_seq')}: {len(records)} record(s), "
        f"cursor {payload.get('cursor')}",
    ]
    for record in records:
        body = record.get("payload") or {}
        summary = (body.get("kind") or body.get("state")
                   or body.get("status") or body.get("outcome") or "")
        lines.append(
            f"  {record.get('seq'):>6} {record.get('event')} "
            f"{record.get('record_id')}"
            + (f" {summary}" if summary else ""))
    if payload.get("more"):
        lines.append(f"  more after {payload.get('cursor')}")
    if payload.get("tail_truncated"):
        lines.append("  journal has a torn tail; recover before writing")
    return lines


def render_text(payload: dict[str, Any]) -> str:
    """Render a view, list or event page as compact text."""
    kind = payload.get("kind")
    if kind == KIND_LIST:
        return "\n".join(_text_list(payload))
    if kind == "events":
        return "\n".join(_text_events(payload))
    return "\n".join(_text_show(payload))


def run(args: argparse.Namespace) -> int:
    """Execute one parsed command; returns a process exit code."""
    root = _root(args.root)
    try:
        store = OutcomeStore(root, state_dir=args.state_dir)
        if args.action == "show":
            payload = view(args.intent, store,
                           events_limit=args.events_limit)
        elif args.action == "list":
            payload = list_view(store, limit=args.limit)
        elif args.action == "events":
            payload = _events(store, args.intent, args.after, args.limit)
        else:  # pragma: no cover - argparse enforces the choices
            print(f"{PROG}: unknown action {args.action!r}", file=sys.stderr)
            return 2
    except OutcomeError as exc:
        print(f"{PROG}: {exc.code}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(payload))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Read the durable outcome journal without writing it.")
    _add_commands(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
