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


def _print_show(record: M.ModelRecord) -> None:
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
            if args.json:
                _print_json({
                    "schema": M.SCHEMA,
                    "root": str(root),
                    "model": record.to_dict(),
                    "discovery": item.discovery.to_dict(),
                    "warnings": list(item.warnings),
                })
            else:
                _print_show(record)
                _warnings(item.warnings)
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
