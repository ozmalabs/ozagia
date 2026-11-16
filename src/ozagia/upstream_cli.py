"""``python -m ozagia.upstream_cli``: local field-improvement bundles.

``preview`` writes nothing. ``write`` requires explicit ``--consent`` and
writes a directory or zip under ``--out``. ``issue`` writes a Markdown draft
only. No command calls a model, uses the network, uploads a bundle or creates
an issue or pull request.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ozagia.config import repo_root
from ozagia.core import upstream as U


def _root(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _evidence(values: list[list[str]]) -> list[str]:
    return [item for group in values for item in group]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.upstream_cli",
        description="consent-based, redacted local field-improvement bundles; "
                    "nothing is uploaded")
    commands = parser.add_subparsers(dest="command", required=True)

    preview_parser = commands.add_parser(
        "preview", help="show exactly what a bundle would include")
    preview_parser.add_argument("--root", default=None)
    preview_parser.add_argument("--title", required=True)
    preview_parser.add_argument("--summary", default=None)
    preview_parser.add_argument(
        "--evidence", action="append", nargs="+", default=[],
        metavar="PATH",
        help="explicit log report, code-review packet, task run, repo-scan "
             "report or status JSON; repeatable")
    preview_parser.add_argument("--no-environment", action="store_true")
    preview_parser.add_argument("--no-config", action="store_true")
    preview_parser.add_argument("--json", action="store_true")

    write_parser = commands.add_parser(
        "write", help="write a consent-based directory or .zip bundle")
    write_parser.add_argument("--root", default=None)
    write_parser.add_argument("--title", required=True)
    write_parser.add_argument("--summary", default=None)
    write_parser.add_argument(
        "--evidence", action="append", nargs="+", default=[], metavar="PATH")
    write_parser.add_argument(
        "--out", required=True, metavar="DIR_OR_ZIP",
        help="new or empty directory, or a .zip file")
    write_parser.add_argument(
        "--consent", action="store_true",
        help="explicitly consent to writing this local bundle")
    write_parser.add_argument("--no-environment", action="store_true")
    write_parser.add_argument("--no-config", action="store_true")
    write_parser.add_argument("--json", action="store_true")

    issue_parser = commands.add_parser(
        "issue", help="write a Markdown issue draft; nothing is uploaded")
    issue_parser.add_argument("--root", default=None)
    issue_parser.add_argument("--title", default="ozagia field report")
    issue_parser.add_argument("--summary", default=None)
    issue_parser.add_argument(
        "--evidence", action="append", nargs="+", default=[], metavar="PATH")
    issue_parser.add_argument("--out", required=True, metavar="FILE")
    issue_parser.add_argument("--no-environment", action="store_true")
    issue_parser.add_argument("--no-config", action="store_true")
    issue_parser.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = _root(args.root)
    except (OSError, ValueError) as exc:
        print(f"error: cannot resolve root: {exc}", file=sys.stderr)
        return 2

    if args.command == "preview":
        try:
            report = U.preview(
                root, title=args.title, summary=args.summary,
                evidence=_evidence(args.evidence),
                include_environment=not args.no_environment,
                include_config=not args.no_config)
        except (U.UpstreamError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            _print_json(report)
        else:
            print(U.format_preview(report), end="")
        return 0

    if args.command == "write":
        if not args.consent:
            print(
                "refusal: explicit consent is required; run the preview, "
                "review it, then pass --consent",
                file=sys.stderr)
            return 1
        try:
            result = U.write_bundle(
                root, args.out, consent=True, title=args.title,
                summary=args.summary, evidence=_evidence(args.evidence),
                include_environment=not args.no_environment,
                include_config=not args.no_config)
        except U.ConsentRequired as exc:
            print(f"refusal: {exc}", file=sys.stderr)
            return 1
        except U.BundleError as exc:
            print(f"refusal: {exc}", file=sys.stderr)
            return 1
        except (U.UpstreamError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            _print_json(result)
        else:
            print(U.format_bundle(result), end="")
        return 0

    if args.command == "issue":
        try:
            result = U.write_issue(
                root, args.out, title=args.title, summary=args.summary,
                evidence=_evidence(args.evidence),
                include_environment=not args.no_environment,
                include_config=not args.no_config)
        except U.BundleError as exc:
            print(f"refusal: {exc}", file=sys.stderr)
            return 1
        except (U.UpstreamError, OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            _print_json(result)
        else:
            print(f"issue draft written: {result['path']}")
            print("local only; nothing was uploaded and no issue was created")
        return 0

    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
