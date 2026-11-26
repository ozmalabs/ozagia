"""``ozagia overview`` (alias ``shape``): one reference page for a checkout.

    ozagia overview [--root R] [--json | --markdown] [--out PATH]
                    [--budget-s S] [--max-modules N] [--check PATH]

The report is built by :mod:`ozagia.core.repo_shape` from files, the Python
AST, the existing tag cache and graph snapshot. The command executes no target
code, calls no model and opens no socket. It writes nothing unless ``--out`` is
explicit, and then writes atomically. ``--check`` compares a previously
generated file and exits 1 on drift.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core import repo_shape as RS

PROG = "ozagia overview"


def add_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "overview",
        aliases=["shape"],
        help="one deterministic source-shape reference: identity, layout, "
             "modules, docs, entry points, tests and commands, plus a labelled "
             "observed environment",
        description="Describe the checkout without executing it: identity and "
                    "package metadata, top-level layout, module responsibilities, "
                    "documentation map, entry points, tests, commands, "
                    "architecture (when a tag cache exists) and a labelled "
                    "generation environment that is excluded from the stable "
                    "hash and `--check`.")
    parser.add_argument("--root", default=None, help="repository root")
    parser.add_argument("--json", action="store_true",
                        help="print the stable JSON report")
    parser.add_argument("--markdown", action="store_true",
                        help="print Markdown (the default)")
    parser.add_argument("--out", default=None, metavar="PATH",
                        help="write the report atomically to PATH (the only write)")
    parser.add_argument("--budget-s", type=float, default=RS.DEFAULT_BUDGET_S,
                        help=f"wall-clock budget (default {RS.DEFAULT_BUDGET_S:g} s)")
    parser.add_argument("--max-modules", type=int,
                        default=RS.DEFAULT_MAX_MODULES,
                        help=f"maximum modules listed (default "
                             f"{RS.DEFAULT_MAX_MODULES})")
    parser.add_argument("--check", default=None, metavar="PATH",
                        help="compare PATH with freshly generated output; "
                             "exit 1 on drift")


def _render(args: argparse.Namespace, shape: dict[str, Any]) -> str:
    if args.json:
        return RS.render_json(shape)
    return RS.render_markdown(shape)


def run(args: argparse.Namespace) -> int:
    if args.json and args.markdown:
        print(f"{PROG}: --json and --markdown are mutually exclusive",
              file=sys.stderr)
        return 2
    root = (Path(args.root).expanduser().resolve()
            if args.root else repo_root())
    if not root.is_dir():
        print(f"{PROG}: not a directory: {root}", file=sys.stderr)
        return 2
    exclude = [args.out] if args.out else ([args.check] if args.check else [])
    try:
        shape = RS.build_shape(root, budget_s=args.budget_s,
                               max_modules=args.max_modules,
                               exclude=exclude)
    except (OSError, ValueError) as exc:
        print(f"{PROG}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    text = _render(args, shape)
    if args.check:
        result = RS.check_file(args.check, shape, text)
        if result["status"] in {"missing", "unreadable", "no_hash"}:
            print(f"{PROG}: cannot check {args.check}: {result['reason']}",
                  file=sys.stderr)
            return 2
        if result["stable_same"]:
            if result["volatile_only"]:
                print(f"{PROG}: {args.check}: stable shape matches; local "
                      "snapshot/HEAD, generation time or rendered format "
                      "differs, which is not drift", file=sys.stderr)
            else:
                print(f"{PROG}: {args.check} is current "
                      f"({shape['content_hash']})", file=sys.stderr)
            return 0
        print(f"{PROG}: {args.check} has stable shape drift; regenerate with "
              f"`ozagia overview --out {args.check}`", file=sys.stderr)
        return 1
    if args.out:
        RS.write_atomic(args.out, text)
        print(f"{PROG}: wrote {args.out} ({shape['content_hash']})",
              file=sys.stderr)
        return 0
    sys.stdout.write(text)
    return 0


__all__ = ["PROG", "add_parser", "run"]
