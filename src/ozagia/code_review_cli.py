"""``python -m ozagia.code_review_cli``: deterministic code review packets."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ozagia.core import code_review as R


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.code_review_cli",
        description="deterministic, graph-aware review of a BASE..HEAD diff")
    parser.add_argument("--root", default=None,
                        help="repository root (default: the current directory)")
    parser.add_argument("--range", dest="range_spec", required=True,
                        metavar="BASE..HEAD", help="commit range to review")
    parser.add_argument("--graph", default=None,
                        help="graph snapshot (default: <root>/.ozagia/graph.json)")
    parser.add_argument("--json", action="store_true",
                        help="emit the packet as JSON")
    parser.add_argument("--out", default=None,
                        help="write the output atomically to this path")
    parser.add_argument("--fail-on", default="blocking", choices=R.FAIL_ON,
                        help="exit 1 at or above this severity (default: blocking)")
    parser.add_argument("--max-file-bytes", type=int, default=None,
                        help="large-file threshold (default: 1000000)")
    parser.add_argument("--memory-mb", type=float, default=None,
                        help="expected test/build memory for admission "
                             "(default: 2048)")
    parser.add_argument("--disk-mb", type=float, default=None,
                        help="expected test/build disk for admission "
                             "(default: 4096)")
    parser.add_argument("--worktree-base", default="main",
                        help="branch expected for the main worktree "
                             "(default: main)")
    parser.add_argument("--ocelot", action="store_true",
                        help="add optional Ocelot structural clusters "
                             "(default: off; no install or download)")
    parser.add_argument("--ocelot-root", default=None,
                        help="explicit Ocelot checkout (default: resolve one)")
    parser.add_argument("--ocelot-mode", default="auto",
                        choices=("auto", "inprocess", "subprocess"),
                        help="Ocelot worker transport (default: auto)")
    parser.add_argument("--ocelot-timeout", type=int, default=120,
                        help="Ocelot worker timeout in seconds (default: 120)")
    return parser


def _emit_refusal(report: dict, as_json: bool) -> None:
    if as_json:
        print(R.packet_json(report), end="")
    else:
        print(R.format_refusal(report), end="", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root) if args.root else Path.cwd()
    if args.ocelot_timeout < 1:
        print("error: --ocelot-timeout must be a positive integer",
              file=sys.stderr)
        return 2
    kwargs = {
        "graph_path": args.graph,
        "memory_mb": 2048.0 if args.memory_mb is None else args.memory_mb,
        "disk_mb": 4096.0 if args.disk_mb is None else args.disk_mb,
        "worktree_base": args.worktree_base,
        "ocelot": args.ocelot,
        "ocelot_root": args.ocelot_root,
        "ocelot_mode": args.ocelot_mode,
        "ocelot_timeout": args.ocelot_timeout,
    }
    if args.max_file_bytes is not None:
        kwargs["max_file_bytes"] = args.max_file_bytes
    try:
        packet = R.review_range(root, args.range_spec, **kwargs)
    except R.PreflightError as exc:
        _emit_refusal(exc.report, args.json)
        return 2
    except (R.CodeReviewError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    text = R.packet_json(packet) if args.json else R.format_packet(packet)
    if args.out:
        try:
            R.atomic_write(args.out, text)
        except R.CodeReviewError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    print(text, end="")
    return R.exit_code(packet, args.fail_on)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
