"""Command line for preview-first process and connection actions.

    python -m ozagia.actions_cli list [--json]
    python -m ozagia.actions_cli preview ACTION [options] [--json]
    python -m ozagia.actions_cli apply ACTION --confirm TOKEN [options] [--json]

Exit codes: 0 for a preview or successful apply, 2 for a validation/IO error,
1 for a refused or failed action.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

from ozagia.config import repo_root
from ozagia.core.actions import (
    ActionError,
    ActionRefused,
    ActionResult,
    apply as apply_action,
    list_actions,
    preview as preview_action,
)


def _add_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=None,
                        help="repository root (default: the current checkout)")
    parser.add_argument("--name", default=None,
                        help="service, tunnel or integration name")
    parser.add_argument("--allow-live", action="store_true",
                        help="explicit live gate for tunnel.start")
    parser.add_argument("--allow-network", action="store_true",
                        help="explicit network gate for tunnel.start")
    parser.add_argument("--max-bytes", type=int, default=None,
                        help="bounded service log bytes (service.logs)")
    parser.add_argument("--scope", choices=("project", "user"), default=None,
                        help="integration install scope (default: project)")
    parser.add_argument("--wait", type=float, default=None,
                        help="daemon ensure wait seconds")
    parser.add_argument("--idle-timeout", type=float, default=None,
                        help="daemon ensure idle timeout seconds (0: never)")
    parser.add_argument("--json", action="store_true",
                        help="print the structured result as JSON")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.actions_cli",
        description="Preview, confirm and audit process/connection actions.")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="show supported actions")
    listing.add_argument("--json", action="store_true")
    for name, help_text in (
        ("preview", "describe an action without changing anything"),
        ("apply", "perform a previewed action with its confirmation token"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("action")
        if name == "apply":
            item.add_argument("--confirm", required=True, metavar="TOKEN")
        _add_options(item)
    return parser


def _action_kwargs(namespace: argparse.Namespace) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if namespace.name is not None:
        values["name"] = namespace.name
    if namespace.allow_live:
        values["live"] = True
    if namespace.allow_network:
        values["network"] = True
    if namespace.max_bytes is not None:
        values["max_bytes"] = namespace.max_bytes
    if namespace.scope is not None:
        values["scope"] = namespace.scope
    if namespace.wait is not None:
        values["wait_s"] = namespace.wait
    if namespace.idle_timeout is not None:
        values["idle_timeout_s"] = namespace.idle_timeout
    return values


def _print_list(as_json: bool) -> None:
    rows = list_actions()
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    width = max(len(row["name"]) for row in rows) + 2
    for row in rows:
        flags = ["read-only"]
        if row["mutating"]:
            flags = ["mutating"]
        if row["destructive"]:
            flags.append("destructive")
        if row["live_gated"]:
            flags.append("live-gated")
        print(f"{row['name']:<{width}} {', '.join(flags):<32} {row['description']}")


def _print_preview(result: ActionResult) -> None:
    print(f"preview: {result.action}")
    print(f"operation: {result.operation}")
    if result.manager_call:
        print(f"manager call: {result.manager_call}")
    if result.argv:
        print(f"argv: {shlex.join(result.argv)}")
    print(f"expected: {result.expected_effect}")
    if result.current_state:
        print(f"current: {result.current_state}")
    if not result.applicable:
        print("applicable: no (apply would be refused or a no-op)")
    for label, values in (("precondition", result.preconditions),
                          ("risk", result.risks), ("note", result.notes)):
        for value in values:
            print(f"{label}: {value}")
    print(f"confirmation: {result.confirmation}")
    print("apply with: python -m ozagia.actions_cli apply "
          f"{result.action} --confirm {result.confirmation}"
          + (" --allow-live --allow-network" if result.gated else ""))


def _print_apply(result: ActionResult) -> None:
    stream = sys.stdout if result.ok else sys.stderr
    print(f"apply: {result.action}: {result.state}", file=stream)
    if result.current_state:
        print(f"current: {result.current_state}", file=stream)
    if result.error:
        print(f"error: {result.error}", file=stream)
    payload = result.result
    if isinstance(payload, dict) and isinstance(payload.get("logs"), str):
        logs = payload["logs"]
        if logs:
            print(logs, end="" if logs.endswith("\n") else "\n")
    elif isinstance(payload, dict) and isinstance(payload.get("integrations"), list):
        for item in payload["integrations"]:
            print(f"  {item.get('name')}: {item.get('state')}")
    elif isinstance(payload, dict) and isinstance(payload.get("services"), list):
        for item in payload["services"]:
            print(f"  {item.get('name')}: {item.get('state')}")
    elif isinstance(payload, dict) and isinstance(payload.get("tunnels"), list):
        for item in payload["tunnels"]:
            print(f"  {item.get('name')}: {item.get('state')}")
    elif isinstance(payload, dict) and "state" not in payload:
        print(json.dumps(payload, indent=2, sort_keys=True))
    if result.audit_error:
        print(f"warning: audit record failed: {result.audit_error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    namespace = parser.parse_args(argv)
    if namespace.command == "list":
        _print_list(namespace.json)
        return 0
    root = (Path(namespace.root).expanduser() if namespace.root is not None
            else repo_root())
    kwargs = _action_kwargs(namespace)
    try:
        if namespace.command == "preview":
            result = preview_action(root, namespace.action, **kwargs)
            if namespace.json:
                print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
            else:
                _print_preview(result)
            return 0
        result = apply_action(root, namespace.action, namespace.confirm, **kwargs)
        if namespace.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            _print_apply(result)
        return 0 if result.ok else 1
    except ActionRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    except ActionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
