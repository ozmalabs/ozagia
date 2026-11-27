"""Queries over discovered or explicitly selected repository graphs.

Usage::

    ozagia federation status [--root R] [--repo R ...]
    ozagia federation search QUERY [--root R] [--repo R ...] [--limit N] [--family-boost]
    ozagia federation node QUALIFIED_KEY [--root R] [--repo R ...]
    ozagia federation check [--root R] [--repo R ...]
    ozagia federation relations [--root R] [--repo R ...] [--write|--cached] [--out PATH]
    ozagia federation build [--root R] [--repo R ...] [--out PATH]
    ozagia federation graph [--root R] [--repo R ...] [--out PATH] [--refresh]
    ozagia federation sync [--root R] [--repo R ...]

Queries automatically fast-forward only checkouts explicitly declared in
``reference_repos``; ``--no-sync`` opts out. Indexes are not refreshed after
a checkout update.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import sys
from pathlib import Path
from typing import Any


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "show discovered repositories and snapshot freshness"),
        ("search", "search indexed snapshots without refreshing them"),
        ("node", "inspect a qualified node and its edges"),
        ("check", "check declared cross-repository relationships"),
        ("relations", "show declared, package-derived and API repository "
                      "relationships"),
        ("build", "write the persisted cross-repository relationship supergraph"),
        ("graph", "read the persisted relationship supergraph without rebuilding "
                  "unless --refresh is given"),
        ("sync", "fast-forward explicitly designated reference repositories"),
    ):
        item = sub.add_parser(name, help=help_text)
        if name == "search":
            item.add_argument("query")
            item.add_argument("--limit", type=int, default=20)
            item.add_argument(
                "--family-boost", action="store_true",
                help="prefer hits from members sharing a declared family with "
                     "the anchor (opt-in; default off)")
        elif name == "node":
            item.add_argument("qualified_key")
        if name in {"build", "graph", "relations"}:
            item.add_argument("--out", default=None,
                              help="artifact path (default: state dir supergraph.json)")
        if name == "graph":
            item.add_argument("--refresh", action="store_true",
                              help="rebuild the artifact when it is absent, stale or invalid")
        if name == "relations":
            artifact = item.add_mutually_exclusive_group()
            artifact.add_argument(
                "--write", action="store_true",
                help="write the persisted relationship supergraph and return it")
            artifact.add_argument(
                "--cached", action="store_true",
                help="read the persisted relationship supergraph instead of "
                     "querying live members")
        item.add_argument("--root", default=None,
                          help="discovery anchor (default: current repository)")
        item.add_argument("--repo", action="append", default=None, dest="roots",
                          help="explicit federation member; repeat to select several")
        item.add_argument("--json", action="store_true",
                          help="print the complete JSON result")
        if name != "sync":
            item.add_argument("--no-sync", action="store_true",
                              help="do not update designated reference checkouts")
    return parser


def _render(result: Any, *, as_json: bool) -> str:
    if as_json:
        return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    # Core results are JSON-serializable and carry per-repository provenance.
    # Keep output lossless until the shape is finalized by the federation API.
    return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)


def run(argv: list[str] | None = None, *, prog: str = "ozagia federation") -> int:
    args = _parser(prog).parse_args(argv)
    from ozagia.config import repo_root
    from ozagia.core import federation as F

    root = Path(args.root).expanduser().resolve() if args.root else repo_root()
    roots = [Path(value).expanduser().resolve() for value in args.roots] if args.roots else None
    if args.command == "sync":
        result = F.sync_references(root, roots=roots, force=True)
    else:
        context = F.without_reference_sync() if args.no_sync else nullcontext()
        with context:
            if args.command == "status":
                result = F.status(root, roots=roots)
            elif args.command == "search":
                result = F.search(root, args.query, roots=roots,
                                  limit=args.limit,
                                  family_boost=args.family_boost)
            elif args.command == "node":
                result = F.node(root, args.qualified_key, roots=roots)
            elif args.command == "build":
                from ozagia.core import supergraph
                result = supergraph.write(root, roots=roots, out=args.out)
            elif args.command == "graph":
                from ozagia.core import supergraph
                result = (supergraph.refresh(root, roots=roots, out=args.out)
                          if args.refresh else supergraph.read(root, out=args.out))
            elif args.command == "relations":
                from ozagia.core import cross_repo, supergraph
                meta_keys = ("schema", "path", "state", "generated_at",
                             "content_hash", "source_hashes", "counts",
                             "artifact", "changed", "changed_sources", "reason",
                             "rebuild", "bytes")
                if args.cached:
                    cached = supergraph.read(root, out=args.out)
                    if cached.get("state") != "fresh":
                        result = {key: value for key, value in cached.items()
                                  if key != "graph"}
                        result["ok"] = False
                    else:
                        result = dict(cached.get("graph") or {})
                        result["persisted"] = {
                            key: cached.get(key) for key in meta_keys}
                        result["ok"] = True
                elif args.write:
                    written = supergraph.write(root, roots=roots, out=args.out)
                    result = dict(written.get("graph") or {})
                    result["persisted"] = {
                        key: written.get(key) for key in meta_keys}
                    result["ok"] = True
                else:
                    result = cross_repo.relations(root, roots=roots)
            else:
                from ozagia.core import cross_repo
                result = cross_repo.check(root, roots=roots)
    print(_render(result, as_json=args.json))
    return 0 if not isinstance(result, dict) or result.get("ok", True) else 2


def main(argv: list[str] | None = None, *, prog: str = "ozagia federation") -> int:
    try:
        return run(argv, prog=prog)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
