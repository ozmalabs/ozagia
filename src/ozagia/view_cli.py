"""``ozagia-view``: project a graph snapshot through an aperture.

Examples::

    python -m ozagia.view_cli --aperture architecture --format json --root .
    python -m ozagia.view_cli --aperture consensus --format html \
        --graph .ozagia/research/research-20261116.json --out view.html

The CLI reads an existing graph. It does not build or refresh the index, call a
model, or use the network. The default graph is ``<root>/.ozagia/graph.json``
(or the state directory named by ``$OZAGIA_STATE_DIR``).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ozagia.config import repo_root, state_path
from ozagia.core.apertures import (
    ApertureError,
    available_apertures,
    get_aperture,
)
from ozagia.core.view import (
    ViewError,
    build_view,
    read_graph,
    render_json,
)
from ozagia.view_html import render_html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-view",
        description="render a graph snapshot through a data-defined aperture")
    parser.add_argument("--aperture", default="architecture",
                        help="aperture id (default: architecture)")
    parser.add_argument("--format", choices=("json", "html"), default="json",
                        help="output format (default: json)")
    parser.add_argument("--root", default=None,
                        help="repository root (default: the current git root)")
    parser.add_argument("--graph", default=None,
                        help="graph JSON (default: <root>/.ozagia/graph.json)")
    parser.add_argument("--out", default=None,
                        help="write to this file instead of stdout")
    parser.add_argument("--depth", type=int, default=None,
                        help="scope depth from --node or the aperture's root")
    parser.add_argument("--node", default=None,
                        help="scope root node id (overrides the aperture)")
    parser.add_argument("--list-apertures", action="store_true",
                        help="list available apertures and exit")
    return parser


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _aperture_listing(root: Path) -> list[dict[str, object]]:
    return [{
        "id": definition["id"],
        "title": definition["title"],
        "description": definition["description"],
        "grain": definition["grain"],
        "layout": definition["render"]["layout"],
    } for definition in available_apertures(root).values()]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve() if args.root else repo_root()
    try:
        if args.list_apertures:
            listing = _aperture_listing(root)
            if args.format == "json":
                print(json.dumps(listing, indent=2, sort_keys=True))
            else:
                for item in listing:
                    print(f"{item['id']:<14} {item['layout']:<9} "
                          f"{item['title']} — {item['description']}")
            return 0
        if args.depth is not None and args.depth < 0:
            raise ViewError("--depth must be >= 0")
        graph_path = (Path(args.graph).resolve() if args.graph
                      else state_path(root, "graph.json"))
        graph = read_graph(graph_path)
        definition = get_aperture(args.aperture, root=root)
        view = build_view(
            graph, definition, root=root, depth=args.depth,
            scope_root=args.node, source=graph_path,
        )
        if args.format == "html":
            text = render_html(view, title=f"{definition['title']} view")
        else:
            text = render_json(view)
        if args.out:
            _write(Path(args.out), text)
        else:
            sys.stdout.write(text)
        return 0
    except (ApertureError, ViewError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
