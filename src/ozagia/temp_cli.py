"""``ozagia tmp``: manage provenanced temporary space.

    ozagia tmp list    [--json] [--all] [--kind K] [--session S] [--owned|--unowned]
                       [--sort allocated|apparent|files|inodes|age]
    ozagia tmp status  [--json] [--top N]
    ozagia tmp audit   [--json]
    ozagia tmp create  --reason TEXT [--kind K] [--session S] [--harness H]
                       [--ttl-s N] [--path P] [--max-total-bytes B]
                       [--max-inodes N] [--max-session-bytes B]
                       [--max-session-inodes N] [--min-free B]
                       [--model NAME] [--run-id ID] [--json]
    ozagia tmp adopt   PATH --reason TEXT [--kind K] [--session S] [--harness H]
                       [--ttl-s N] [--owned] [--mark] [--model NAME]
                       [--run-id ID] [--json]
    ozagia tmp release ID [--delete] [--json]
    ozagia tmp pin     ID [--until EPOCH|ISO] [--json]
    ozagia tmp unpin   ID [--json]
    ozagia tmp clean   [--apply] [--expired] [--older-than D]
                       [--max-total-bytes B] [--id ID] [--json]
    ozagia tmp gc      [--apply] [--max-total-bytes B] [--max-inodes N]
                       [--max-session-bytes B] [--max-session-inodes N]
                       [--min-free B] [--older-than D] [--json]
    ozagia tmp clean-legacy [--apply] [--older-than D] [--include-caches]
                            [--json]
    ozagia tmp path    [--kind K]

Plain output is short and stable.  ``create`` prints only the created path so a
caller can use it directly.  ``clean``, ``gc`` and ``clean-legacy`` are plans
unless ``--apply`` is given.  ``audit`` is read-only and only reports
conservative legacy-name matches.  JSON output is deterministic except for
timestamps and paths.  No command prints file contents.

Exit status is 0 for success (including a cleanup plan with no candidates) and
1 for a refusal or error.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core import temp_space as TS

PROG = "ozagia tmp"


def add_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "tmp",
        help="manage provenanced temporary space and clean it safely",
        description="Create, adopt, inspect, release and clean managed "
                    "temporary directories.  Paths are tracked in "
                    "<state dir>/tmp/registry; cleanup is a dry run unless "
                    "--apply is given.")
    children = parser.add_subparsers(dest="action", required=True)
    _add_actions(children)


def _add_actions(children: Any) -> None:
    def common(child: argparse.ArgumentParser) -> argparse.ArgumentParser:
        child.add_argument("--root", default=None, help="repository root")
        child.add_argument("--json", action="store_true", help="print JSON")
        return child

    listed = common(children.add_parser("list", help="list managed entries"))
    listed.add_argument("--all", action="store_true",
                        help="include released and expired records")
    listed.add_argument("--kind", choices=TS.KINDS, default=None,
                        help="only this kind")
    listed.add_argument("--session", default=None,
                        help="only this session id, or HARNESS:ID")
    ownership = listed.add_mutually_exclusive_group()
    ownership.add_argument("--owned", dest="owned", action="store_true",
                           default=None, help="only owned entries")
    ownership.add_argument("--unowned", dest="owned", action="store_false",
                           help="only unowned entries")
    listed.add_argument("--sort", default=None,
                        choices=tuple(key for key in TS.LIST_SORT_KEYS
                                       if key != TS.DEFAULT_LIST_SORT),
                        help="order: largest allocated/apparent/files/inodes, "
                             "or least recently used (age); default created-at")

    status_parser = common(
        children.add_parser("status", help="usage and filesystem capacity"))
    status_parser.add_argument("--top", type=int, default=None, metavar="N",
                               help="also list the N largest entries by "
                                    "allocated bytes")
    common(children.add_parser(
        "audit",
        help="read-only scan for untracked legacy temp artifacts"))

    created = common(children.add_parser("create", help="create one entry"))
    created.add_argument("--reason", required=True,
                         help="short label (at most 200 characters)")
    created.add_argument("--kind", choices=TS.KINDS,
                         default=TS.DEFAULT_SCRATCH_KIND)
    _session_arguments(created)
    created.add_argument("--ttl-s", type=int, default=None,
                         help="expiry in seconds (60 to 90 days)")
    created.add_argument("--path", default=None, metavar="PATH",
                         help="create at PATH instead of the default location")
    _budget_arguments(created)
    _metadata_arguments(created)

    adopted = common(children.add_parser("adopt", help="track an existing path"))
    adopted.add_argument("path", metavar="PATH")
    adopted.add_argument("--reason", required=True,
                         help="short label (at most 200 characters)")
    adopted.add_argument("--kind", choices=TS.KINDS, default=TS.DEFAULT_KIND)
    _session_arguments(adopted)
    adopted.add_argument("--ttl-s", type=int, default=None,
                         help="expiry in seconds (60 to 90 days)")
    adopted.add_argument("--owned", action="store_true",
                         help="allow cleanup of this entry")
    adopted.add_argument("--mark", action="store_true",
                         help="write the ownership marker (needs --owned)")
    _metadata_arguments(adopted)

    released = common(children.add_parser("release", help="release one entry"))
    released.add_argument("id", metavar="ID")
    released.add_argument("--delete", action="store_true",
                          help="delete an owned, marker-matched tree")

    pinned = common(children.add_parser("pin", help="protect one entry"))
    pinned.add_argument("id", metavar="ID")
    pinned.add_argument("--until", type=_timestamp, default=None, metavar="TIME",
                        help="optional epoch second or ISO-8601 pin metadata")

    unpinned = common(children.add_parser("unpin", help="remove a pin"))
    unpinned.add_argument("id", metavar="ID")

    cleaned = common(children.add_parser("clean", help="plan or apply cleanup"))
    cleaned.add_argument("--apply", action="store_true",
                         help="perform the plan (the default is a dry run)")
    cleaned.add_argument("--expired", action="store_true",
                         help="select entries whose TTL has elapsed")
    cleaned.add_argument("--older-than", type=_duration, default=None,
                         metavar="D",
                         help="select entries unused for D (bare = days; "
                              "s/m/h/d suffixes accepted)")
    cleaned.add_argument("--max-total-bytes", type=int, default=None,
                         metavar="B",
                         help="delete oldest eligible entries until the total "
                              "is at most B bytes")
    cleaned.add_argument("--id", action="append", default=None, metavar="ID",
                         help="restrict to ID (repeatable)")

    collected = common(children.add_parser(
        "gc", help="plan or apply budget-driven garbage collection"))
    collected.add_argument("--apply", action="store_true",
                           help="perform the plan (the default is a dry run)")
    _budget_arguments(collected)
    collected.add_argument("--older-than", type=_duration, default=None,
                           metavar="D",
                           help="only entries unused for D (bare = days; "
                                "s/m/h/d suffixes accepted)")

    legacy = common(children.add_parser(
        "clean-legacy",
        help="plan or apply cleanup of untracked legacy temp artifacts"))
    legacy.add_argument("--apply", action="store_true",
                        help="perform the plan (the default is a dry run)")
    legacy.add_argument("--older-than", type=_duration, default=None,
                        metavar="D",
                        help="only artifacts older than D (bare = days; "
                             "s/m/h/d suffixes accepted; default 1 day)")
    legacy.add_argument("--include-caches", action="store_true",
                        help="also consider regenerable benchmark cache roots")

    path = common(children.add_parser(
        "path", help="print (and create) the default managed scratch root"))
    path.add_argument("--kind", choices=TS.KINDS,
                      default=TS.DEFAULT_SCRATCH_KIND)


def _session_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", default=None, metavar="S",
                        help="session id (never invented when omitted)")
    parser.add_argument("--harness", default=None, metavar="H",
                        help="harness name for --session")


def _metadata_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default=None, metavar="NAME",
                        help="optional model label for provenance")
    parser.add_argument("--run-id", default=None, metavar="ID",
                        help="optional run identifier for provenance")


def _budget_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-total-bytes", type=int, default=None, metavar="B",
                        help="refuse/gc when total allocated bytes exceed B")
    parser.add_argument("--max-inodes", type=int, default=None, metavar="N",
                        help="refuse/gc when total inodes exceed N")
    parser.add_argument("--max-session-bytes", type=int, default=None,
                        metavar="B",
                        help="refuse/gc when one session exceeds B bytes")
    parser.add_argument("--max-session-inodes", type=int, default=None,
                        metavar="N",
                        help="refuse/gc when one session exceeds N inodes")
    parser.add_argument("--min-free", type=int, default=None, metavar="B",
                        help="refuse/gc when the filesystem has fewer than B "
                             "free bytes")


def _budget(args: argparse.Namespace) -> TS.TempBudget | None:
    values = {
        "max_total_bytes": getattr(args, "max_total_bytes", None),
        "max_inodes": getattr(args, "max_inodes", None),
        "max_session_bytes": getattr(args, "max_session_bytes", None),
        "max_session_inodes": getattr(args, "max_session_inodes", None),
        "min_free_bytes": getattr(args, "min_free", None),
    }
    if all(value is None for value in values.values()):
        return None
    return TS.TempBudget(**values)


def _timestamp(value: str) -> int:
    """Parse an epoch second or an ISO-8601 timestamp into seconds."""
    text = str(value).strip()
    if not text:
        raise argparse.ArgumentTypeError("timestamp must not be empty")
    try:
        stamp = int(text)
    except ValueError:
        pass
    else:
        if stamp < 0:
            raise argparse.ArgumentTypeError("timestamp must not be negative")
        return stamp
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not an epoch second or ISO-8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    stamp = int(parsed.timestamp())
    if stamp < 0:
        raise argparse.ArgumentTypeError("timestamp must not be negative")
    return stamp


def _duration(value: str) -> int:
    """Parse a bare day count or a value with an s/m/h/d suffix."""
    text = str(value).strip().lower()
    multiplier = 24 * 60 * 60
    if text and text[-1] in "smhd":
        suffix = text[-1]
        text = text[:-1]
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 24 * 60 * 60}[suffix]
    try:
        amount = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a duration: {value!r}") from None
    seconds = int(amount * multiplier)
    if seconds < 0:
        raise argparse.ArgumentTypeError("duration must not be negative")
    return seconds


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _error(message: str, *, as_json: bool, code: str = "refused") -> int:
    if as_json:
        _print_json({"ok": False, "code": code, "error": message})
    else:
        print(f"{PROG}: {message}", file=sys.stderr)
    return 1


def _root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else repo_root()


def _session(args: argparse.Namespace) -> dict[str, str] | None:
    return TS.make_session(getattr(args, "harness", None),
                           getattr(args, "session", None))


def _session_label(entry: dict[str, Any]) -> str:
    session = entry.get("session")
    if not isinstance(session, dict):
        return "unknown"
    return f"{session.get('harness')}:{session.get('id')}"


def _format_entry(entry: dict[str, Any]) -> str:
    return (f"{entry['id']}  {entry['kind']:<8} {entry['state']:<8} "
            f"owned={'yes' if entry['owned'] else 'no'}  "
            f"pinned={'yes' if entry.get('pinned') else 'no'}  "
            f"leased={'yes' if entry.get('leased') else 'no'}  "
            f"allocated={entry['bytes']}  "
            f"apparent={entry.get('apparent_bytes', 0)}  "
            f"files={entry['files']}  inodes={entry.get('inodes', 0)}  "
            f"session={_session_label(entry)}  {entry['path']}")


def _format_status(result: dict[str, Any]) -> str:
    totals = result["totals"]
    lines = [
        f"entries={totals['entries']}  allocated={totals['bytes']}  "
        f"apparent={totals['apparent_bytes']}  files={totals['files']}  "
        f"inodes={totals['inodes']}  pinned={totals.get('pinned', 0)}  "
        f"leased={totals.get('leased', 0)}  partial={totals['partial']}  "
        f"unknown={totals['unknown']}",
        f"registry: {result['registry']}",
    ]
    if result["by_kind"]:
        lines.append("by kind:")
        for kind, row in result["by_kind"].items():
            lines.append(
                f"  {kind:<8} entries={row['entries']} "
                f"allocated={row['bytes']} apparent={row['apparent_bytes']} "
                f"files={row['files']} inodes={row['inodes']} "
                f"owned={row['owned']} unowned={row['unowned']}")
    if result["by_session"]:
        lines.append("by session:")
        for label, row in result["by_session"].items():
            lines.append(
                f"  {label}  entries={row['entries']} "
                f"allocated={row['bytes']} apparent={row['apparent_bytes']} "
                f"files={row['files']} inodes={row['inodes']}")
    for filesystem in result["filesystems"]:
        percent = ("unknown" if filesystem["percent"] is None
                   else f"{filesystem['percent']:.1f}%")
        kind = ("tmpfs" if filesystem["tmpfs"] else
                "unknown" if filesystem["tmpfs"] is None else "disk")
        inodes = ""
        if filesystem["total_inodes"]:
            inode_percent = (filesystem["inode_percent"]
                             if filesystem["inode_percent"] is not None
                             else "unknown")
            inodes = (f" inodes={filesystem['free_inodes']}/"
                      f"{filesystem['total_inodes']} "
                      f"({inode_percent}% used)")
        lines.append(
            f"filesystem {filesystem['path']} ({kind}): "
            f"total={filesystem['total_bytes']} free={filesystem['free_bytes']} "
            f"used={filesystem['used_bytes']} ({percent}){inodes}")
    if "top" in result:
        rows = result["top"]
        if not rows:
            lines.append("top 0 by allocated: (none)")
        else:
            lines.append(f"top {len(rows)} by allocated:")
            for row in rows:
                lines.append(
                    f"  allocated={row['bytes']} apparent={row['apparent_bytes']} "
                    f"files={row['files']} inodes={row['inodes']}  "
                    f"{row['kind']}  {row['session_label']}  "
                    f"{row['reason']}  {row['path']}")
    for warning in result["warnings"]:
        lines.append(f"warning: {warning}")
    for error in result["registry_errors"]:
        lines.append(f"registry warning: {error}")
    return "\n".join(lines)


def _format_audit(result: dict[str, Any]) -> str:
    totals = result["totals"]
    age = result["age"]
    lines = [
        f"artifacts={totals['artifacts']}  registered={totals['registered']}  "
        f"covered={totals['covered']}  untracked={totals['untracked']}  "
        f"allocated={totals['bytes']}  apparent={totals['apparent_bytes']}  "
        f"files={totals['files']}  inodes={totals['inodes']}  "
        f"partial={totals['partial']}  "
        f"truncated={'yes' if result['truncated'] else 'no'}",
    ]
    if age["oldest_s"] is not None:
        lines.append(f"oldest: {age['oldest_s']}s  {age['oldest_path']}")
        lines.append(f"newest: {age['newest_s']}s  {age['newest_path']}")
    for item in result["artifacts"]:
        if item["registered"]:
            status = "registered"
        elif item["registered_ancestor"]:
            status = "under-entry"
        else:
            status = "untracked"
        lines.append(
            f"  {status:<11} {item['bytes']}B {item['files']}f "
            f"age={item['age_s']}s {item['path']}  [{item['pattern']}]")
    for warning in result["warnings"]:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)


def _format_clean(result: dict[str, Any]) -> str:
    totals = result["totals"]
    mode = "plan" if result["dry_run"] else "apply"
    lines = [
        f"{mode}: {totals['candidates']} candidate(s), "
        f"{totals['bytes']} allocated, {totals['apparent_bytes']} apparent, "
        f"{totals['files']} files, {totals['inodes']} inodes, "
        f"{totals['deleted']} deleted, {totals['kept']} kept",
    ]
    for row in result["candidates"]:
        verb = "delete" if result["dry_run"] else "deleted"
        lines.append(
            f"  {verb} {row['id']}  {row['kind']}  {row['bytes']} allocated  "
            f"{row['apparent_bytes']} apparent  "
            f"{row['reason']}  {row['path']}")
    for row in result["kept"]:
        if row["why"] in ("not-selected", "missing"):
            continue
        lines.append(
            f"  keep   {row['id']}  {row['kind']}  {row['bytes']} allocated  "
            f"{row['why']}  {row['path']}")
    return "\n".join(lines)


def _format_gc(result: dict[str, Any]) -> str:
    totals = result["totals"]
    mode = "plan" if result["dry_run"] else "apply"
    lines = [
        f"{mode}: {totals['candidates']} candidate(s), "
        f"{totals['bytes']} allocated, {totals['apparent_bytes']} apparent, "
        f"{totals['files']} files, {totals['inodes']} inodes, "
        f"{totals['deleted']} deleted, {totals['kept']} kept, "
        f"{totals['errors']} error(s)",
    ]
    budget = result.get("budget")
    if budget:
        limits = ", ".join(f"{key}={value}" for key, value in budget.items()
                           if value is not None)
        if limits:
            lines.append(f"budget: {limits}")
    min_free = result.get("min_free") or {}
    if min_free.get("minimum") is not None:
        lines.append(
            f"min free: {min_free.get('current')} of "
            f"{min_free['minimum']} required"
            + (" (violated)" if min_free.get("violated") else ""))
    for row in result["candidates"]:
        verb = "delete" if result["dry_run"] else "deleted"
        lines.append(
            f"  {verb} {row['id']}  {row['kind']}  {row['state']}  "
            f"{row['bytes']} allocated  {row['apparent_bytes']} apparent  "
            f"{row['reason']}  {row['path']}")
    for row in result["kept"]:
        if row["why"] == "not-selected":
            continue
        lines.append(
            f"  keep   {row['id']}  {row['kind']}  {row['bytes']} allocated  "
            f"{row['why']}  {row['path']}")
    for error in result.get("errors") or []:
        lines.append(f"  error: {error}")
    return "\n".join(lines)


def _format_clean_legacy(result: dict[str, Any]) -> str:
    totals = result["totals"]
    mode = "plan" if result["dry_run"] else "apply"
    lines = [
        f"{mode}: {totals['candidates']} artifact(s), "
        f"{totals['bytes']} allocated, {totals['apparent_bytes']} apparent, "
        f"{totals['files']} files, {totals['inodes']} inodes, "
        f"{totals['deleted']} deleted, {totals['kept']} kept, "
        f"{totals['errors']} error(s), "
        f"truncated={'yes' if result['truncated'] else 'no'}",
    ]
    for row in result["candidates"]:
        verb = "delete" if result["dry_run"] else "deleted"
        lines.append(
            f"  {verb} {row['bytes']}B {row['files']}f {row['path']}  "
            f"[{row['pattern']}]  {row['why']}")
    for row in result["kept"]:
        lines.append(
            f"  keep   {row['bytes']}B {row['files']}f {row['path']}  "
            f"[{row['pattern']}]  {row['why']}")
    for error in result["errors"]:
        lines.append(f"  error: {error}")
    for warning in result["warnings"]:
        lines.append(f"  warning: {warning}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    """Run one parsed ``ozagia tmp`` command."""
    action = args.action
    as_json = bool(getattr(args, "json", False))
    root = _root(getattr(args, "root", None))
    try:
        if action == "list":
            entries = TS.list_entries(
                root, kind=getattr(args, "kind", None),
                session=getattr(args, "session", None),
                owned=getattr(args, "owned", None),
                include_all=bool(getattr(args, "all", False)),
                sort=getattr(args, "sort", None) or TS.DEFAULT_LIST_SORT)
            if as_json:
                _print_json({"ok": True, "count": len(entries),
                             "entries": entries})
            elif entries:
                print("\n".join(_format_entry(entry) for entry in entries))
            else:
                print("no managed temporary entries")
            return 0
        if action == "status":
            result = TS.status(
                root, top=getattr(args, "top", None))
            if as_json:
                _print_json({"ok": True, **result})
            else:
                print(_format_status(result))
            return 0
        if action == "audit":
            result = TS.audit(root)
            if as_json:
                _print_json({"ok": True, **result})
            else:
                print(_format_audit(result))
            return 0
        if action == "clean-legacy":
            older = (TS.LEGACY_DEFAULT_OLDER_THAN_S
                     if args.older_than is None else args.older_than)
            result = TS.clean_legacy(
                root, apply=args.apply, older_than_s=older,
                include_caches=args.include_caches)
            if as_json:
                _print_json({"ok": not result["errors"], **result})
            else:
                print(_format_clean_legacy(result))
            return 0 if not result["errors"] else 1
        if action == "create":
            entry = TS.create(
                root, kind=args.kind, reason=args.reason,
                session=_session(args), ttl_s=args.ttl_s, path=args.path,
                model=args.model, run_id=args.run_id, budget=_budget(args))
            if as_json:
                _print_json({"ok": True, "entry": entry})
            else:
                print(entry["path"])
            return 0
        if action == "adopt":
            entry = TS.adopt(
                root, args.path, reason=args.reason, kind=args.kind,
                session=_session(args), ttl_s=args.ttl_s, owned=args.owned,
                mark=args.mark, model=args.model, run_id=args.run_id)
            if as_json:
                _print_json({"ok": True, "entry": entry})
            else:
                print(f"{entry['id']}  {entry['path']}")
            return 0
        if action == "release":
            entry = TS.release(root, args.id, delete=args.delete)
            if as_json:
                _print_json({"ok": True, "entry": entry})
            elif args.delete:
                print(f"deleted {entry['id']}" if entry["state"] == "released"
                      else f"released {entry['id']} ({entry['state']})")
            else:
                print(f"released {entry['id']}")
            return 0
        if action == "pin":
            entry = TS.pin(root, args.id, until=args.until)
            if as_json:
                _print_json({"ok": True, "entry": entry})
            else:
                print(f"pinned {entry['id']}")
            return 0
        if action == "unpin":
            entry = TS.unpin(root, args.id)
            if as_json:
                _print_json({"ok": True, "entry": entry})
            else:
                print(f"unpinned {entry['id']}")
            return 0
        if action == "clean":
            result = TS.clean(
                root, dry_run=True, expired=args.expired,
                older_than_s=args.older_than,
                max_total_bytes=args.max_total_bytes, ids=args.id,
                apply=args.apply)
            if as_json:
                _print_json({"ok": True, **result})
            else:
                print(_format_clean(result))
            return 0
        if action == "gc":
            result = TS.gc(
                root, budget=_budget(args), apply=args.apply,
                older_than_s=args.older_than)
            ok = not result.get("errors")
            if as_json:
                _print_json({"ok": ok, **result})
            else:
                print(_format_gc(result))
            return 0 if ok else 1
        if action == "path":
            target = TS.ensure_default_root(root, args.kind)
            if as_json:
                _print_json({"ok": True, "path": str(target),
                             "kind": args.kind})
            else:
                print(target)
            return 0
    except (TS.TempSpaceError, OSError, ValueError) as exc:
        if isinstance(exc, TS.TempNotFound):
            code = "not_found"
        elif isinstance(exc, TS.TempBudgetError):
            code = "budget"
        elif isinstance(exc, TS.TempLeaseHeld):
            code = "lease_held"
        else:
            code = "refused"
        return _error(str(exc), as_json=as_json, code=code)
    print(f"{PROG}: unknown action {action!r}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG)
    children = parser.add_subparsers(dest="action", required=True)
    _add_actions(children)
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

__all__ = ["PROG", "add_parser", "run"]
