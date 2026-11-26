"""``ozagia assets``: catalog durable external assets and plan safe GC.

    ozagia assets list    [--owner O] [--kind K] [--state S] [--pinned] [--json]
    ozagia assets status  [ID | --path P] [--top N] [--json]
    ozagia assets scan    [--path P ...] [--json]
    ozagia assets discover [--limit N] [--no-research] [--json]
    ozagia assets register --path P --kind K --owner O [--scope S]
                           [--source-url URL] [--source-kind K] [--dataset D]
                           [--revision R] [--commit C] [--version V]
                           [--sha256 S] [--license SPDX] [--pinned]
                           [--deletable | --no-delete] [--mark] [--id ID]
                           [--from-hf-metadata] [--reason TEXT] [--json]
    ozagia assets adopt   PATH --kind K [--owner O] [--reason TEXT] [--mark]
                           [--json]
    ozagia assets verify  [ID | --all] [--hash] [--json]
    ozagia assets pin ID
    ozagia assets unpin ID
    ozagia assets forget ID
    ozagia assets gc      [--max-bytes B] [--max-inodes N] [--older-than D]
                           [--json]

Assets are durable datasets, models, corpora, installs, caches and tool trees,
not temporary workspaces.  The registry stores metadata only.  ``discover``
reads only known Ozagia-owned manifests and layouts (never ``$HOME``, never
file contents, never the network); everything else needs an explicit
``register``.  ``gc`` is a dry run in this stage; ``--apply`` is refused.
Plain output is short and stable and never includes file contents.

Exit status is 0 for success (including an empty GC plan), 1 for a refusal,
a missing record or a verification failure, and 2 for bad arguments.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core import assets as A

PROG = "ozagia assets"
FAIL_STATES = frozenset({"mismatch", "missing", "partial"})


def add_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "assets",
        help="catalog durable datasets, models, corpora and caches; plan safe GC",
        description="Record external durable assets with ownership, provenance, "
                    "size, licence and verification state.  Assets are separate "
                    "from temporary space; GC is a dry run and never deletes.")
    children = parser.add_subparsers(dest="action", required=True)
    _add_actions(children)


def _add_actions(children: Any) -> None:
    def common(child: argparse.ArgumentParser) -> argparse.ArgumentParser:
        child.add_argument("--root", default=None, help="repository root")
        child.add_argument("--json", action="store_true", help="print JSON")
        return child

    listed = common(children.add_parser("list", help="list cataloged assets"))
    listed.add_argument("--owner", choices=A.OWNERS, default=None,
                        help="only this owner")
    listed.add_argument("--kind", choices=A.KINDS, default=None,
                        help="only this kind")
    listed.add_argument("--state", choices=A.STATES, default=None,
                        help="only this state")
    listed.add_argument("--pinned", action="store_true",
                        help="only pinned assets")

    status_parser = common(
        children.add_parser("status", help="totals, or one asset"))
    status_parser.add_argument("id", nargs="?", default=None, metavar="ID")
    status_parser.add_argument("--path", default=None, metavar="P",
                               help="show the asset recorded at this path")
    status_parser.add_argument("--top", type=_non_negative, default=None,
                               metavar="N",
                               help="also list the N largest assets")

    scanned = common(children.add_parser(
        "scan", help="register only explicit paths or asset markers"))
    scanned.add_argument("--path", action="extend", nargs="+", default=None,
                         metavar="P",
                         help="explicit path to inspect for a marker "
                              "(repeatable; the home directory is refused)")

    discovered = common(children.add_parser(
        "discover",
        help="register known Ozagia-owned roots and manifests"))
    discovered.add_argument("--limit", type=_non_negative, default=None,
                            metavar="N",
                            help="most candidate entries to examine")
    discovered.add_argument("--no-research", action="store_true",
                            help="skip the research cache/fetch roots")

    registered = common(children.add_parser(
        "register", help="record an existing durable asset"))
    registered.add_argument("--path", required=True, metavar="P")
    registered.add_argument("--kind", required=True, choices=A.KINDS)
    registered.add_argument("--owner", required=True, choices=A.OWNERS)
    registered.add_argument("--scope", choices=A.SCOPES,
                            default=A.DEFAULT_SCOPE)
    registered.add_argument("--source-url", default=None, metavar="URL")
    registered.add_argument("--source-kind", choices=A.SOURCE_KINDS,
                            default=None)
    registered.add_argument("--dataset", default=None, metavar="D",
                            help="dataset identifier (for example a Hugging "
                                 "Face repository)")
    registered.add_argument("--revision", default=None, metavar="R")
    registered.add_argument("--commit", default=None, metavar="C")
    registered.add_argument("--version", default=None, metavar="V")
    registered.add_argument("--sha256", default=None, metavar="S",
                            help="explicit SHA-256 of a single file")
    registered.add_argument("--license", default=None, metavar="SPDX",
                            help="explicit licence identifier; never inferred")
    registered.add_argument("--pinned", action="store_true",
                            help="never a GC candidate")
    deletion = registered.add_mutually_exclusive_group()
    deletion.add_argument("--deletable", action="store_true",
                          help="the owner permits explicit deletion")
    deletion.add_argument("--no-delete", action="store_true",
                          help="explicitly record that deletion is not permitted "
                               "(the default)")
    registered.add_argument("--mark", action="store_true",
                            help="write .ozagia-asset.json (owned assets only)")
    registered.add_argument("--id", default=None, metavar="ID")
    registered.add_argument(
        "--from-hf-metadata", action="store_true",
        help="read <path>/.cache/huggingface/download/**/*.metadata for the "
             "revision and per-file etags; never opens or hashes the data")
    registered.add_argument("--reason", default="", metavar="TEXT",
                            help="short label, never contents or secrets")

    adopted = common(children.add_parser(
        "adopt", help="record an owned asset (register with owner=ozagia)"))
    adopted.add_argument("path", metavar="PATH")
    adopted.add_argument("--kind", required=True, choices=A.KINDS)
    adopted.add_argument("--owner", choices=A.OWNERS, default="ozagia")
    adopted.add_argument("--scope", choices=A.SCOPES,
                         default=A.DEFAULT_SCOPE)
    adopted.add_argument("--reason", default="", metavar="TEXT")
    adopted.add_argument("--mark", action="store_true",
                         help="write .ozagia-asset.json (owner=ozagia only)")

    verified = common(children.add_parser(
        "verify", help="recheck existence, size and optional hashes"))
    verified.add_argument("id", nargs="?", default=None, metavar="ID")
    verified.add_argument("--all", action="store_true", help="verify every asset")
    verified.add_argument("--hash", action="store_true",
                          help="also read and hash the file or declared digests")

    for name, text in (("pin", "never a GC candidate"),
                       ("unpin", "allow GC planning again"),
                       ("forget", "remove only the registry record")):
        item = common(children.add_parser(name, help=text))
        item.add_argument("id", metavar="ID")

    collected = common(children.add_parser(
        "gc", help="plan safe deletion (dry run only)"))
    collected.add_argument("--max-bytes", type=_non_negative, default=None,
                           metavar="B")
    collected.add_argument("--max-inodes", type=_non_negative, default=None,
                           metavar="N")
    collected.add_argument("--older-than", type=_duration, default=None,
                           metavar="D",
                           help="only unused for D (bare = days; s/m/h/d "
                                "suffixes accepted)")
    collected.add_argument("--apply", action="store_true",
                           help="refused in this stage; the plan is dry-run only")


def _non_negative(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"not an integer: {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError("value must not be negative")
    return number


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


def _bad_args(message: str, *, as_json: bool) -> int:
    if as_json:
        _print_json({"ok": False, "code": "bad_args", "error": message})
    else:
        print(f"{PROG}: {message}", file=sys.stderr)
    return 2


def _root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else repo_root()


def _source(args: argparse.Namespace) -> dict[str, Any] | None:
    url = getattr(args, "source_url", None)
    dataset = getattr(args, "dataset", None)
    revision = getattr(args, "revision", None)
    commit = getattr(args, "commit", None)
    kind = getattr(args, "source_kind", None)
    if not any((url, dataset, revision, commit, kind)):
        return None
    if kind is None:
        if dataset:
            kind = "huggingface"
        elif commit:
            kind = "git"
        elif url:
            kind = "url"
        else:
            kind = "unknown"
    return {
        "kind": kind,
        "url": url,
        "dataset": dataset,
        "revision": revision,
        "commit": commit,
    }


def _format_asset(asset: dict[str, Any]) -> str:
    flags = []
    if asset.get("pinned"):
        flags.append("pinned")
    if asset.get("deletable"):
        flags.append("deletable")
    label = ",".join(flags) or "-"
    return (f"{asset['id']}  {asset['kind']:<8} {asset['owner']:<11} "
            f"{asset['state']:<10} bytes={asset['bytes']} "
            f"apparent={asset.get('apparent_bytes', 0)} "
            f"inodes={asset.get('inodes', 0)} flags={label}  {asset['path']}")


def _format_status(result: dict[str, Any]) -> str:
    totals = result["totals"]
    lines = [
        f"assets={totals['entries']}  allocated={totals['bytes']}  "
        f"apparent={totals['apparent_bytes']}  inodes={totals['inodes']}  "
        f"pinned={totals['pinned']}  deletable={totals['deletable']}  "
        f"missing={totals['missing']}  partial={totals['partial']}  "
        f"verified={totals['verified']}  mismatch={totals['mismatch']}",
        f"registry: {result['registry']}",
    ]
    if result["by_kind"]:
        lines.append("by kind:")
        for kind, row in sorted(result["by_kind"].items()):
            lines.append(
                f"  {kind:<8} entries={row['entries']} bytes={row['bytes']} "
                f"apparent={row['apparent_bytes']} inodes={row['inodes']} "
                f"pinned={row['pinned']} deletable={row['deletable']}")
    if result["by_owner"]:
        lines.append("by owner:")
        for owner, row in sorted(result["by_owner"].items()):
            lines.append(
                f"  {owner:<11} entries={row['entries']} bytes={row['bytes']}")
    if result.get("top"):
        lines.append(f"top {len(result['top'])} by allocated:")
        lines.extend(f"  {_format_asset(row)}" for row in result["top"])
    for error in result["registry_errors"]:
        lines.append(f"  registry error: {error}")
    return "\n".join(lines)


def _format_scan(result: dict[str, Any]) -> str:
    lines = [
        f"scanned={result['paths']}  found={result['found']}  "
        f"registered={len(result['registered'])}  "
        f"skipped={len(result['skipped'])}  errors={len(result['errors'])}",
    ]
    lines.extend(f"  registered {row['id']}  {row['path']}"
                 for row in result["registered"])
    lines.extend(f"  skipped {row['path']}: {row['reason']}"
                 for row in result["skipped"])
    lines.extend(f"  error: {error}" for error in result["errors"])
    return "\n".join(lines)


def _format_discover(result: dict[str, Any]) -> str:
    lines = [
        f"discovered={len(result['assets'])}  "
        f"skipped={len(result['skipped'])}  "
        f"errors={len(result['errors'])}  "
        f"partial={'yes' if result['partial'] else 'no'}  "
        f"limit={result['limit']}",
    ]
    for root in result["roots"]:
        location = root.get("path") or "-"
        lines.append(
            f"  root {root['name']:<20} {root['status']:<14} "
            f"registered={root['registered']}  {location}")
    lines.extend(f"  registered {row['id']}  {row['path']}"
                 for row in result["assets"])
    lines.extend(f"  skipped {row['path']}: {row['reason']}"
                 for row in result["skipped"])
    lines.extend(f"  error: {error}" for error in result["errors"])
    return "\n".join(lines)


def _format_verify(results: list[dict[str, Any]]) -> str:
    lines = [_format_asset(row) + f"  reason={row.get('reason') or '-'}"
             for row in results]
    failed = sum(1 for row in results if row["state"] in FAIL_STATES)
    lines.append(f"verified={len(results) - failed}  failed={failed}")
    return "\n".join(lines)


def _format_gc(result: dict[str, Any]) -> str:
    totals = result["totals"]
    lines = [
        f"dry-run  candidates={totals['candidates']}  "
        f"selected={totals['selected']}  "
        f"bytes={totals['selected_bytes']}  inodes={totals['selected_inodes']}",
    ]
    for row in result["candidates"]:
        lines.append(f"  gc    {row['bytes']}B {row['inodes']}i "
                     f"{row['kind']:<8} {row['id']}  {row['path']}")
    for row in result["kept"]:
        lines.append(f"  keep  {row['why']:<32} {row['id']}  {row['path']}")
    for error in result["registry_errors"]:
        lines.append(f"  registry error: {error}")
    return "\n".join(lines)


def _find_path(root: Path, path: str) -> dict[str, Any]:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    resolved = resolved.resolve(strict=False)
    for asset in A.list_assets(root):
        if Path(asset["path"]) == resolved:
            return asset
    raise A.AssetNotFound(f"no asset recorded at {resolved}")


def run(args: argparse.Namespace) -> int:
    """Run one parsed ``ozagia assets`` command."""
    action = args.action
    as_json = bool(getattr(args, "json", False))
    root = _root(getattr(args, "root", None))
    try:
        if action == "list":
            assets = A.list_assets(
                root, owner=getattr(args, "owner", None),
                kind=getattr(args, "kind", None),
                state=getattr(args, "state", None),
                pinned=True if getattr(args, "pinned", False) else None)
            if as_json:
                _print_json({"ok": True, "count": len(assets),
                             "assets": assets})
            elif assets:
                print("\n".join(_format_asset(row) for row in assets))
            else:
                print("no assets")
            return 0
        if action == "status":
            if args.id and args.path:
                return _bad_args("status takes an ID or --path, not both",
                                 as_json=as_json)
            if args.id:
                asset = A.get(root, args.id)
                if as_json:
                    _print_json({"ok": True, "asset": asset})
                else:
                    print(_format_asset(asset))
                return 0
            if args.path:
                asset = _find_path(root, args.path)
                if as_json:
                    _print_json({"ok": True, "asset": asset})
                else:
                    print(_format_asset(asset))
                return 0
            result = A.status(root, top=args.top)
            if as_json:
                _print_json({"ok": True, **result})
            else:
                print(_format_status(result))
            return 0
        if action == "scan":
            result = A.scan(root, paths=args.path)
            if as_json:
                _print_json({"ok": True, **result})
            else:
                print(_format_scan(result))
            return 0
        if action == "discover":
            result = A.discover(
                root, limit=args.limit if args.limit is not None
                else A.MAX_SCAN_LIMIT,
                include_research=not args.no_research)
            if as_json:
                _print_json({"ok": True, **result})
            else:
                print(_format_discover(result))
            return 0
        if action == "register":
            source = _source(args)
            revision = args.revision
            digests = None
            if args.from_hf_metadata:
                if args.sha256:
                    return _bad_args(
                        "--from-hf-metadata cannot be combined with --sha256",
                        as_json=as_json)
                metadata = A.read_hf_metadata(args.path)
                revision = revision or metadata["revision"]
                digests = metadata["digests"]
                if source is None:
                    source = {"kind": "huggingface",
                              "revision": metadata["revision"]}
                elif source.get("revision") is None:
                    source["revision"] = metadata["revision"]
            asset = A.register(
                root, args.path, kind=args.kind, owner=args.owner,
                scope=args.scope, source=source,
                version=args.version, revision=revision,
                sha256=args.sha256, digests=digests, license=args.license,
                pinned=args.pinned,
                deletable=bool(args.deletable and not args.no_delete),
                mark=args.mark, id=args.id, reason=args.reason)
            if as_json:
                _print_json({"ok": True, "asset": asset})
            else:
                print(f"{asset['id']}  {asset['path']}")
            return 0
        if action == "adopt":
            asset = A.adopt(
                root, args.path, kind=args.kind, owner=args.owner,
                scope=args.scope, reason=args.reason, mark=args.mark)
            if as_json:
                _print_json({"ok": True, "asset": asset})
            else:
                print(f"{asset['id']}  {asset['path']}")
            return 0
        if action == "verify":
            if args.all and args.id:
                return _bad_args("verify takes an ID or --all, not both",
                                 as_json=as_json)
            if not args.all and not args.id:
                return _bad_args("verify needs an ID or --all", as_json=as_json)
            if args.all:
                results = A.verify_all(root, hash_files=args.hash)
            else:
                results = [A.verify(root, args.id, hash_files=args.hash)]
            failed = sum(1 for row in results
                         if row["state"] in FAIL_STATES)
            if as_json:
                _print_json({"ok": failed == 0, "count": len(results),
                             "failed": failed, "results": results})
            else:
                print(_format_verify(results))
            return 0 if failed == 0 else 1
        if action == "pin":
            asset = A.pin(root, args.id)
            if as_json:
                _print_json({"ok": True, "asset": asset})
            else:
                print(f"pinned {asset['id']}")
            return 0
        if action == "unpin":
            asset = A.unpin(root, args.id)
            if as_json:
                _print_json({"ok": True, "asset": asset})
            else:
                print(f"unpinned {asset['id']}")
            return 0
        if action == "forget":
            asset = A.forget(root, args.id)
            if as_json:
                _print_json({"ok": True, "forgotten": asset["id"],
                             "asset": asset})
            else:
                print(f"forgot {asset['id']}  {asset['path']}")
            return 0
        if action == "gc":
            result = A.gc(
                root, apply=args.apply, max_bytes=args.max_bytes,
                max_inodes=args.max_inodes,
                older_than_s=args.older_than)
            if as_json:
                _print_json({"ok": True, **result})
            else:
                print(_format_gc(result))
            return 0
    except A.AssetNotFound as exc:
        return _error(str(exc), as_json=as_json, code="not_found")
    except (A.AssetError, OSError, ValueError) as exc:
        return _error(str(exc), as_json=as_json)
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
