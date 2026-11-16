"""``python -m ozagia.research_ui_cli``: read-only research views.

Examples::

    python -m ozagia.research_ui_cli --root . \
        --investigation research-20261116 --view library --format json
    python -m ozagia.research_ui_cli --root . \
        --investigation research-20261116 --view consensus \
        --format html --out consensus.html

The CLI loads an existing investigation, projects recorded nodes and edges and
writes nothing except an explicit ``--out`` file. It does not build or refresh
the graph, download an artifact, call a model or use the network.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ozagia.config import repo_root
from ozagia.core.research_ui import (
    VIEWS,
    ResearchUIError,
    build_research_view,
    load_state,
    render_html,
    render_json,
)
from ozagia.research.graph import ResearchError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.research_ui_cli",
        description="render a recorded research investigation as a read-only "
                    "JSON or HTML view")
    parser.add_argument("--root", default=None,
                        help="repository root (default: the current git root)")
    parser.add_argument("--investigation", required=True,
                        metavar="ID", help="research investigation id")
    parser.add_argument("--view", required=True, choices=VIEWS,
                        help="view to project")
    parser.add_argument("--format", choices=("json", "html"), default="json",
                        help="output format (default: json)")
    parser.add_argument("--out", default=None,
                        help="write to this file instead of stdout")
    return parser


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve() if args.root else repo_root()
    try:
        state, graph_path = load_state(args.investigation, root=root)
        model = build_research_view(
            state, args.view, root=root, source=graph_path,
            investigation_id=args.investigation)
        text = (render_html(model) if args.format == "html"
                else render_json(model))
        if args.out:
            _write(Path(args.out), text)
        else:
            sys.stdout.write(text)
        return 0
    except (ResearchUIError, ResearchError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
