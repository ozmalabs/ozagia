"""``python -m ozagia.models_cli``: inspect the model registry and resolve one.

Reads :mod:`ozagia.core.models`. Discovery is read-only and never calls a
model. An unavailable ``opencode`` is reported in the output and does not fail
the command; validation and IO errors exit with status 2.

Examples::

    python -m ozagia.models_cli list --root .
    python -m ozagia.models_cli list --root . --json
    python -m ozagia.models_cli show local/qwen
    python -m ozagia.models_cli resolve --task diff_review --root . --json
    python -m ozagia.models_cli sources --root .
    python -m ozagia.models_cli import models-dev ./snapshot.json --root .
    python -m ozagia.models_cli graph --source models-dev --root . --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ozagia.config import repo_root
from ozagia.core import model_graph as G
from ozagia.core import model_sources as S
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


def _source_selection(args: argparse.Namespace) -> tuple[str, ...]:
    """Sources requested on the command line, in registry precedence order."""
    if getattr(args, "no_sources", False):
        return ()
    selected = tuple(getattr(args, "sources", None) or S.DEFAULT_ORDER)
    for source_id in selected:
        S.get_source(source_id)
    return selected


def _merge_sources(root: Path, args: argparse.Namespace,
                   item: M.Catalog) -> S.MergedExternal:
    selected = _source_selection(args)
    return S.merge_cached(
        root, selected, model_ids=[record.id for record in item.records])


def _print_source_summary(report: S.MergedExternal) -> None:
    if not report.order:
        print("sources: disabled")
        return
    parts: list[str] = []
    for entry in report.sources:
        status = entry.get("status", "unknown")
        age = entry.get("age_s")
        label = f"{entry.get('source')}={status}"
        if age is not None and status in {"ok", "stale"}:
            label += f"(age={int(age)}s)"
        parts.append(label)
    print("sources: " + ", ".join(parts))
    if report.not_assessed:
        print("sources_not_assessed: " + ", ".join(report.not_assessed))


def _print_sources(report: dict) -> None:
    print(f"sources: {len(report['sources'])}")
    for entry in report["sources"]:
        age = entry.get("age_s")
        age_text = f"{int(age)}s" if age is not None else "unknown"
        print(f"{entry['id']}  status={entry['status']}  "
              f"stale={_yes(entry.get('stale'))}  age={age_text}  "
              f"license={entry.get('license')}  role={entry.get('role')}")
        print(f"  url: {entry.get('url')}")
        print("  fields: " + (", ".join(entry.get("fields", [])) or "-"))
        if entry.get("description"):
            print(f"  description: {entry['description']}")
        if entry.get("not_assessed"):
            print("  not_assessed: yes")
    if report["not_assessed"]:
        print("not_assessed: " + ", ".join(report["not_assessed"]))
    _warnings(tuple(report.get("warnings", ())))


def _yes(value: object) -> str:
    return "yes" if value is True else ("no" if value is False else "unknown")


def _print_sync(report: dict) -> None:
    print(f"source: {report['source']}")
    print(f"status: {report['status']}  origin={report.get('origin')}")
    print(f"bytes: {report.get('bytes')}  sha256: {report.get('sha256')}")
    print(f"fetched_at: {report.get('fetched_at')}")
    print(f"cache: {report.get('cache_path')}")
    if report.get("fallback_used"):
        print("fallback_used: yes")
    _warnings(tuple(report.get("warnings", ())))


def _print_metadata(metadata: dict | None,
                    report: S.MergedExternal | None = None) -> None:
    if not metadata:
        print("metadata: none")
        return
    print("metadata:")
    for key in sorted(metadata):
        value = metadata[key]
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value) or "-"
        print(f"  {key}: {value}")
    if report is not None:
        _print_source_summary(report)


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


def _print_show(record: M.ModelRecord, node: G.ModelNode | None = None,
                *, metadata: dict | None = None,
                report: S.MergedExternal | None = None) -> None:
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
    if report is not None:
        _print_metadata(metadata, report)


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


def _add_source_args(parser: argparse.ArgumentParser, *,
                     metadata: bool = False) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--source", action="append", default=[], metavar="SOURCE",
        dest="sources",
        help="cached metadata source (repeatable; default: all active)")
    group.add_argument(
        "--no-sources", action="store_true",
        help="disable cached metadata and use curated profiles only")
    if metadata:
        parser.add_argument(
            "--with-metadata", action="store_true",
            help="print provenance and the cached metadata for this model")


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
    _add_source_args(show, metadata=True)

    resolve = sub.add_parser(
        "resolve", help="choose the model for a task class or the default")
    resolve.add_argument("--task", default=None, metavar="CLASS")
    resolve.add_argument("--root", default=None)
    resolve.add_argument("--json", action="store_true")

    sources = sub.add_parser(
        "sources", help="list metadata sources and their cache state")
    sources.add_argument("--root", default=None)
    sources.add_argument("--json", action="store_true")

    sync = sub.add_parser(
        "sync", help="fetch and cache one metadata source (needs "
                     "--allow-network)")
    sync.add_argument("source")
    sync.add_argument("--root", default=None)
    sync.add_argument("--allow-network", action="store_true")
    sync.add_argument("--timeout", type=float, default=S.DEFAULT_TIMEOUT_S)
    sync.add_argument("--max-bytes", type=int, default=None)
    sync.add_argument("--json", action="store_true")

    import_parser = sub.add_parser(
        "import", help="validate and cache a local metadata snapshot")
    import_parser.add_argument("source")
    import_parser.add_argument("path")
    import_parser.add_argument("--root", default=None)
    import_parser.add_argument("--json", action="store_true")

    graph = sub.add_parser(
        "graph", help="show the bounded model graph and its profiles")
    graph.add_argument("--root", default=None)
    graph.add_argument("--json", action="store_true")
    graph.add_argument("--max-nodes", type=int, default=G.MAX_NODES)
    graph.add_argument("--max-edges", type=int, default=G.MAX_EDGES)
    graph.add_argument("--top-k", type=int, default=G.DEFAULT_TOP_K)
    graph.add_argument("--threshold", type=float,
                       default=G.DEFAULT_SIMILARITY_THRESHOLD)
    _add_source_args(graph)

    similar = sub.add_parser(
        "similar", help="nearest models in model space")
    similar.add_argument("model_id")
    similar.add_argument("--limit", type=int, default=5)
    similar.add_argument("--root", default=None)
    similar.add_argument("--json", action="store_true")
    _add_source_args(similar)

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
    _add_source_args(match)
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

        if args.command == "sources":
            report = S.source_status(root)
            if args.json:
                _print_json(report)
            else:
                _print_sources(report)
            return 0

        if args.command == "sync":
            try:
                report = S.sync(
                    root, args.source, allow_network=args.allow_network,
                    timeout_s=args.timeout, max_bytes=args.max_bytes)
            except S.SourceError as exc:
                if args.json:
                    _print_json(exc.to_dict())
                else:
                    print(f"error: {exc}", file=sys.stderr)
                return 2
            if args.json:
                _print_json(report)
            else:
                _print_sync(report)
            return 0

        if args.command == "import":
            try:
                report = S.import_file(root, args.source, args.path)
            except S.SourceError as exc:
                if args.json:
                    _print_json(exc.to_dict())
                else:
                    print(f"error: {exc}", file=sys.stderr)
                return 2
            if args.json:
                _print_json(report)
            else:
                _print_sync(report)
            return 0

        if args.command == "show":
            item = M.catalog(root)
            record = item.get(args.model_id)
            if record is None:
                print(f"error: model {args.model_id!r} is not listed or "
                      "configured", file=sys.stderr)
                return 2
            with_metadata = (args.with_metadata or bool(args.sources)
                             or args.no_sources)
            merged = _merge_sources(root, args, item)
            graph_item = G.graph(
                root, catalog=item, external=merged.external)
            node = graph_item.get(record.id)
            metadata = merged.external.get(record.id)
            if args.json:
                payload = {
                    "schema": M.SCHEMA,
                    "root": str(root),
                    "model": record.to_dict(),
                    "profile": (node.to_dict()
                                if node is not None else None),
                    "discovery": item.discovery.to_dict(),
                    "warnings": list(graph_item.warnings),
                }
                if with_metadata:
                    payload["metadata"] = metadata
                    payload["sources"] = merged.to_dict()
                _print_json(payload)
            else:
                _print_show(
                    record, node,
                    metadata=metadata if with_metadata else None,
                    report=merged if with_metadata else None)
                _warnings(graph_item.warnings)
            return 0

        if args.command == "graph":
            item = M.catalog(root)
            merged = _merge_sources(root, args, item)
            graph_item = G.graph(
                root, catalog=item, external=merged.external,
                max_nodes=args.max_nodes, max_edges=args.max_edges,
                top_k=args.top_k, similarity_threshold=args.threshold)
            if args.json:
                payload = graph_item.to_dict()
                payload["sources"] = merged.to_dict()
                _print_json(payload)
            else:
                _print_graph(graph_item)
                _print_source_summary(merged)
            return 0

        if args.command == "similar":
            item = M.catalog(root)
            merged = _merge_sources(root, args, item)
            graph_item = G.graph(
                root, catalog=item, external=merged.external)
            results = graph_item.neighbours(args.model_id, limit=args.limit)
            if args.json:
                _print_json({
                    "schema": G.SCHEMA,
                    "root": str(root),
                    "model": args.model_id,
                    "similar": list(results),
                    "sources": merged.to_dict(),
                })
            else:
                _print_similar(args.model_id, results)
                _print_source_summary(merged)
            return 0

        if args.command == "match":
            item = M.catalog(root)
            merged = _merge_sources(root, args, item)
            req = G.requirements(
                root, task=args.task, capabilities=args.capability,
                tool_formats=args.tool_format, local_only=args.local,
                min_context=args.min_context, max_cost=args.max_cost,
                size=args.size, type=args.type,
                modalities_in=args.modality)
            result = G.match(
                root, req, limit=args.limit, catalog=item,
                external=merged.external)
            if args.json:
                payload = result.to_dict()
                payload["sources"] = merged.to_dict()
                _print_json(payload)
            else:
                _print_match(result)
                _print_source_summary(merged)
            return 0

        resolution = M.resolve(root, args.task)
        if args.json:
            _print_json(resolution.to_dict())
        else:
            _print_resolve(resolution)
        return 0
    except S.SourceError as exc:
        if getattr(args, "json", False):
            _print_json(exc.to_dict())
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except (M.ModelError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
