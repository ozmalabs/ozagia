"""``python -m ozagia.models_cli``: inspect the model registry and resolve one.

Reads :mod:`ozagia.core.models`. Discovery is read-only and never calls a
model. An unavailable ``opencode`` is reported in the output and does not fail
the command; validation and IO errors exit with status 2.

Examples::

    python -m ozagia.models_cli list --root .
    python -m ozagia.models_cli list --root . --json
    python -m ozagia.models_cli show local/qwen
    python -m ozagia.models_cli resolve --task diff_review --root . --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ozagia.config import repo_root
from ozagia.core import model_graph as G
from ozagia.core import models as M


def _root(value: str | None) -> Path:
    return Path(value).resolve() if value else repo_root()


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _local(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def _number(value: int | float | None) -> str:
    return "unknown" if value is None else str(value)


def _warnings(warnings: tuple[str, ...]) -> None:
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _print_list(item: M.Catalog) -> None:
    discovery = item.discovery
    detail = f" ({discovery.message})" if discovery.message else ""
    print(f"discovery: {discovery.status}{detail}")
    print(f"models: {len(item.records)}")
    for record in item:
        capabilities = ",".join(record.capabilities) or "-"
        print(
            f"{record.id}  status={record.status}  source={record.source}  "
            f"local={_local(record.local)}  context={_number(record.context)}  "
            f"input={_number(record.input_cost)}  "
            f"output={_number(record.output_cost)}  "
            f"capabilities={capabilities}")
    _warnings(item.warnings)


def _print_show(record: M.ModelRecord, node: G.ModelNode | None = None) -> None:
    print(f"id: {record.id}")
    print(f"provider: {record.provider}")
    print(f"model: {record.model}")
    print(f"status: {record.status}")
    print(f"source: {record.source}")
    print(f"local: {_local(record.local)}")
    print(f"context: {_number(record.context)}")
    print(f"input_cost: {_number(record.input_cost)}")
    print(f"output_cost: {_number(record.output_cost)}")
    print("capabilities: "
          + (", ".join(record.capabilities) if record.capabilities else "-"))
    print("references: "
          + (", ".join(record.references) if record.references else "-"))
    if node is not None:
        _print_profile(node)


def _print_profile(node: G.ModelNode) -> None:
    print(f"family: {node.family}/{node.variant}")
    print(f"type: {node.type}")
    print(f"size_class: {node.size_class}")
    print(f"params_total_b: {_number(node.params_total_b)}")
    print(f"params_active_b: {_number(node.params_active_b)}")
    print(f"graph_context: {_number(node.context)}")
    print(f"graph_input_cost: {_number(node.input_cost)}")
    print(f"graph_output_cost: {_number(node.output_cost)}")
    print("modalities_in: "
          + (", ".join(node.modalities_in) if node.modalities_in else "-"))
    print("modalities_out: "
          + (", ".join(node.modalities_out) if node.modalities_out else "-"))
    print("graph_capabilities: "
          + (", ".join(node.capabilities) if node.capabilities else "-"))
    print("tool_formats: "
          + (", ".join(node.tool_formats) if node.tool_formats else "-"))
    print(f"remote: {_local(node.remote)}")
    print(f"profile_source: {node.source}  confidence={node.confidence}")
    print(f"profile_detail: {node.profile_source}")
    if node.notes:
        print(f"notes: {node.notes}")


def _print_graph(item: G.ModelGraph, *, text_limit: int = 40) -> None:
    print(f"models: {len(item.nodes)}  edges: {len(item.edges)}  "
          f"truncated: {'yes' if item.truncated else 'no'}")
    detail = f" ({item.discovery.message})" if item.discovery.message else ""
    print(f"discovery: {item.discovery.status}{detail}")
    families = ", ".join(f"{name}={count}"
                         for name, count in item.families) or "-"
    print(f"families: {families}")
    print("nodes:")
    for node in item.nodes[:text_limit]:
        print(f"  {node.id}  family={node.family}  type={node.type}  "
              f"size={node.size_class}  status={node.status}  "
              f"local={_local(node.local)}  confidence={node.confidence}")
    if len(item.nodes) > text_limit:
        print(f"  ... {len(item.nodes) - text_limit} more nodes")
    print("edges:")
    for edge in item.edges[:text_limit]:
        print(f"  {edge.kind}  {edge.source} <-> {edge.target}  "
              f"similarity={edge.similarity}  coverage={edge.coverage}")
    if len(item.edges) > text_limit:
        print(f"  ... {len(item.edges) - text_limit} more edges")
    _warnings(item.warnings)


def _print_similar(model_id: str, results: tuple[dict, ...]) -> None:
    print(f"model: {model_id}")
    for entry in results:
        print(f"  {entry['id']}  similarity={entry['similarity']}  "
              f"distance={entry['distance']}  coverage={entry['coverage']}  "
              f"family={entry['family']}  type={entry['type']}  "
              f"size={entry['size_class']}  status={entry['status']}")


def _print_match(item: G.MatchResult) -> None:
    print(f"task: {item.requirements.task or '-'}")
    print(f"source: {item.source}")
    req = item.requirements
    print("requirements: "
          f"capabilities={','.join(req.capabilities) or '-'}  "
          f"tool_formats={','.join(req.tool_formats) or '-'}  "
          f"local_only={'yes' if req.local_only else 'no'}  "
          f"min_context={_number(req.min_context)}  "
          f"max_cost={_number(req.max_cost)}  "
          f"size={req.size or '-'}")
    if item.selected is not None:
        selected = item.selected
        print(f"selected: {selected.id}  score={selected.score}  "
              f"family={selected.family}  type={selected.type}  "
              f"size={selected.size_class}  status={selected.status}")
        print("  capabilities: "
              + (", ".join(selected.capabilities) or "-"))
        print("  tool_formats: "
              + (", ".join(selected.tool_formats) or "-"))
        for reason in selected.reasons:
            print(f"  reason: {reason}")
    else:
        print("selected: none")
    print("candidates:")
    for candidate in item.candidates:
        print(f"  {candidate.id}  score={candidate.score}  "
              f"family={candidate.family}  type={candidate.type}  "
              f"size={candidate.size_class}  status={candidate.status}")
        print("    capabilities: "
              + (", ".join(candidate.capabilities) or "-"))
        print("    tool_formats: "
              + (", ".join(candidate.tool_formats) or "-"))
        for reason in candidate.reasons:
            print(f"    {reason}")
    print(f"excluded: {len(item.excluded)}")
    for entry in item.excluded:
        print(f"  {entry['id']}  status={entry['status']}  "
              + "; ".join(entry["reasons"]))
    print(f"not_assessed: {len(item.not_assessed)}")
    for entry in item.not_assessed:
        print(f"  {entry['id']}  status={entry['status']}  "
              + "; ".join(entry["reasons"]))
    _warnings(item.warnings)


def _print_resolve(item: M.Resolution) -> None:
    print(f"task: {item.task_class or '-'}")
    if item.requested is not None:
        print(f"requested: {item.requested}  role={item.requested_role}  "
              f"config_source={item.requested_layer}")
    if item.selected is not None:
        print(f"selected: {item.selected.id}  status={item.selected.status}  "
              f"source={item.selected.source}")
    else:
        print("selected: none")
    print(f"source: {item.source}")
    print(f"status: {item.status}")
    print(f"fallback_used: {'yes' if item.fallback_used else 'no'}")
    print(f"reason: {item.reason}")
    if item.candidates:
        print("candidates:")
        for candidate in item.candidates:
            mark = "selected" if candidate.selected else "-"
            print(f"  {candidate.id}  role={candidate.role}  "
                  f"source={candidate.source}  layer={candidate.layer}  "
                  f"status={candidate.status}  {mark}  {candidate.reason}")
    print(f"discovery: {item.discovery.status}"
          + (f" ({item.discovery.message})" if item.discovery.message else ""))
    _warnings(item.warnings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ozagia-models",
        description="inspect available models and resolve a task's model")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser(
        "list", help="list discovered and configured models")
    list_parser.add_argument("--root", default=None)
    list_parser.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show one model by id")
    show.add_argument("model_id")
    show.add_argument("--root", default=None)
    show.add_argument("--json", action="store_true")

    resolve = sub.add_parser(
        "resolve", help="choose the model for a task class or the default")
    resolve.add_argument("--task", default=None, metavar="CLASS")
    resolve.add_argument("--root", default=None)
    resolve.add_argument("--json", action="store_true")

    graph = sub.add_parser(
        "graph", help="show the bounded model graph and its profiles")
    graph.add_argument("--root", default=None)
    graph.add_argument("--json", action="store_true")
    graph.add_argument("--max-nodes", type=int, default=G.MAX_NODES)
    graph.add_argument("--max-edges", type=int, default=G.MAX_EDGES)
    graph.add_argument("--top-k", type=int, default=G.DEFAULT_TOP_K)
    graph.add_argument("--threshold", type=float,
                       default=G.DEFAULT_SIMILARITY_THRESHOLD)

    similar = sub.add_parser(
        "similar", help="nearest models in model space")
    similar.add_argument("model_id")
    similar.add_argument("--limit", type=int, default=5)
    similar.add_argument("--root", default=None)
    similar.add_argument("--json", action="store_true")

    match = sub.add_parser(
        "match", help="rank models for advisory task requirements")
    match.add_argument("--task", default=None, metavar="CLASS")
    match.add_argument("--capability", action="append", default=[],
                       metavar="X")
    match.add_argument("--tool-format", action="append", default=[],
                       metavar="F")
    match.add_argument("--local", action="store_true")
    match.add_argument("--min-context", type=int, default=None,
                       metavar="N")
    match.add_argument("--max-cost", type=float, default=None, metavar="N")
    match.add_argument("--size", default=None, metavar="CLASS")
    match.add_argument("--type", default=None, metavar="TYPE")
    match.add_argument("--modality", action="append", default=[],
                       metavar="M")
    match.add_argument("--limit", type=int, default=5)
    match.add_argument("--root", default=None)
    match.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _root(getattr(args, "root", None))
    try:
        if args.command == "list":
            item = M.catalog(root)
            if args.json:
                _print_json(item.to_dict())
            else:
                _print_list(item)
            return 0

        if args.command == "show":
            item = M.catalog(root)
            record = item.get(args.model_id)
            if record is None:
                print(f"error: model {args.model_id!r} is not listed or "
                      "configured", file=sys.stderr)
                return 2
            graph_item = G.graph(root, catalog=item)
            node = graph_item.get(record.id)
            if args.json:
                _print_json({
                    "schema": M.SCHEMA,
                    "root": str(root),
                    "model": record.to_dict(),
                    "profile": (node.to_dict()
                                if node is not None else None),
                    "discovery": item.discovery.to_dict(),
                    "warnings": list(graph_item.warnings),
                })
            else:
                _print_show(record, node)
                _warnings(graph_item.warnings)
            return 0

        if args.command == "graph":
            item = G.graph(
                root, max_nodes=args.max_nodes, max_edges=args.max_edges,
                top_k=args.top_k, similarity_threshold=args.threshold)
            if args.json:
                _print_json(item.to_dict())
            else:
                _print_graph(item)
            return 0

        if args.command == "similar":
            results = G.neighbours(root, args.model_id, limit=args.limit)
            if args.json:
                _print_json({
                    "schema": G.SCHEMA,
                    "root": str(root),
                    "model": args.model_id,
                    "similar": list(results),
                })
            else:
                _print_similar(args.model_id, results)
            return 0

        if args.command == "match":
            req = G.requirements(
                root, task=args.task, capabilities=args.capability,
                tool_formats=args.tool_format, local_only=args.local,
                min_context=args.min_context, max_cost=args.max_cost,
                size=args.size, type=args.type,
                modalities_in=args.modality)
            item = G.match(root, req, limit=args.limit)
            if args.json:
                _print_json(item.to_dict())
            else:
                _print_match(item)
            return 0

        resolution = M.resolve(root, args.task)
        if args.json:
            _print_json(resolution.to_dict())
        else:
            _print_resolve(resolution)
        return 0
    except (M.ModelError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
