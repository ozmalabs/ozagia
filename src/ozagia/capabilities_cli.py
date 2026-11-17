"""``ozagia capabilities``: inspect and audit declared capabilities.

    ozagia capabilities list  [--json] [--limit N] [--cursor C]
    ozagia capabilities show  ID [--json] [--limit N] [--cursor C]
    ozagia capabilities audit [--json] [--limit N] [--cursor C]

The command is read-only.  It does not refresh the graph, start or probe a
daemon, import a target project, install a tool or call a model.  ``list`` and
``audit`` are paginated; ``show`` paginates the edges that mention one
capability.  Exit status is ``0`` when the report was produced (findings are
data, not a command failure) and ``2`` for an invalid request or unknown id.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ozagia.core import capabilities as K
from ozagia.core import contracts as C
from ozagia.core import self_graph as SG

PROG = "ozagia capabilities"


def add_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "capabilities",
        help="list, show and audit declared capabilities (read-only)")
    children = parser.add_subparsers(dest="action", required=True)

    def common(child: argparse.ArgumentParser) -> argparse.ArgumentParser:
        child.add_argument("--json", action="store_true", help="print JSON")
        child.add_argument("--limit", type=int, default=SG.DEFAULT_LIMIT,
                           help=f"page size (default {SG.DEFAULT_LIMIT}, "
                                f"max {SG.MAX_LIMIT})")
        child.add_argument("--cursor", default=None,
                           help="opaque page cursor from a previous call")
        return child

    common(children.add_parser("list", help="list declared capabilities"))
    show = common(children.add_parser("show", help="show one capability and its edges"))
    show.add_argument("id", help="capability id, e.g. cap:graph.context")
    common(children.add_parser("audit", help="audit declarations against observed surfaces"))


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _error(message: str, *, as_json: bool, code: str = "invalid_request") -> int:
    if as_json:
        _print({"ok": False, "code": code, "error": message})
    else:
        print(f"{PROG}: {message}", file=sys.stderr)
    return 2


def payload(action: str, *, capability_id: str | None = None,
            limit: Any = None, cursor: Any = None) -> dict[str, Any]:
    """Build one bounded view.  Raises a project error for a bad request."""
    if action == "list":
        return SG.capability_list(limit=limit, cursor=cursor)
    if action == "show":
        if not capability_id:
            raise SG.SelfGraphError("show needs a capability id")
        return SG.capability_show(capability_id, limit=limit, cursor=cursor)
    if action == "audit":
        return SG.audit_self_graph(limit=limit, cursor=cursor)
    raise SG.SelfGraphError(f"unknown action {action!r}")


def _format_list(result: dict[str, Any]) -> str:
    lines = []
    for item in result["items"]:
        exposure = ",".join(item["exposure"])
        states = ",".join(
            f"{name}={count}"
            for name, count in sorted(item["surface_states"].items())) or "none"
        lines.append(
            f"{item['id']}  [{item['domain']}]  {item['side_effect']}  "
            f"{exposure}  surfaces={item['surfaces']} ({states})  "
            f"findings={item['findings']}")
    if not lines:
        lines.append("no capabilities on this page")
    truncation = result["truncation"]
    if truncation.get("truncated"):
        lines.append(
            f"truncated: {truncation['omitted']} more; next --cursor "
            f"{truncation['next_cursor']}")
    return "\n".join(lines)


def _format_show(result: dict[str, Any]) -> str:
    cap = result["capability"]
    lines = [
        f"{cap['id']}  [{cap['domain']}]  {cap['side_effect']}",
        f"  purpose: {cap['purpose']}",
        f"  owner: {cap['owner']}",
        f"  exposure: {', '.join(cap['exposure'])}",
        f"  authorization: {cap['authorization']}",
        f"  implementations: {', '.join(cap['impl'])}",
        f"  surfaces: {', '.join(cap['surfaces']) or '-'}",
        f"  prerequisites: {', '.join(cap['prerequisites']) or '-'}",
        f"  limits: {'; '.join(cap['limits']) or '-'}",
        f"  tests: {', '.join(cap['tests'])}",
        f"  docs: {', '.join(cap['docs'])}",
    ]
    if result["edges"]:
        lines.append("  edges:")
        for edge in result["edges"]:
            lines.append(
                f"    {edge['type']:13} {edge['from']} -> {edge['to']}  "
                f"[{edge['state']}]")
    if result["findings"]:
        lines.append("  findings:")
        for finding in result["findings"]:
            lines.append(
                f"    [{finding['severity']}] {finding['kind']} "
                f"{finding['subject']}: {finding['reason']}")
    truncation = result["truncation"]
    if truncation.get("truncated"):
        lines.append(
            f"  truncated: {truncation['omitted']} more edges; next --cursor "
            f"{truncation['next_cursor']}")
    return "\n".join(lines)


def _format_audit(result: dict[str, Any]) -> str:
    counts = result["counts"]
    lines = [
        f"state: {result['state']}",
        f"capabilities: {counts['capabilities']}  "
        f"declared surfaces: {counts['declared_surfaces']}  "
        f"observed surfaces: {counts['observed_surfaces']}  "
        f"undeclared: {counts['undeclared_surfaces']}  "
        f"findings: {counts['findings']}",
    ]
    if result["findings"]:
        lines.append("findings:")
        for finding in result["findings"]:
            lines.append(
                f"  [{finding['severity']}] {finding['kind']} "
                f"{finding['subject']}: {finding['reason']}")
    else:
        lines.append("findings: none")
    truncation = result["truncation"]
    if truncation.get("truncated"):
        lines.append(
            f"truncated: {truncation['omitted']} more findings; next --cursor "
            f"{truncation['next_cursor']}")
    unknowns = (result.get("coverage") or {}).get("unknowns") or []
    if unknowns:
        lines.append("unknowns:")
        lines.extend(f"  {item}" for item in unknowns)
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    """Run one parsed ``ozagia capabilities`` command."""
    try:
        result = payload(
            args.action,
            capability_id=getattr(args, "id", None),
            limit=args.limit,
            cursor=args.cursor,
        )
    except (SG.SelfGraphError, K.CapabilityError, C.ContractError) as exc:
        code = ("not_found" if "unknown capability" in str(exc)
                else "invalid_request")
        return _error(str(exc), as_json=bool(args.json), code=code)
    if args.json:
        _print(result)
        return 0
    if args.action == "list":
        print(_format_list(result))
    elif args.action == "show":
        print(_format_show(result))
    else:
        print(_format_audit(result))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG)
    children = parser.add_subparsers(dest="action", required=True)

    def common(child: argparse.ArgumentParser) -> argparse.ArgumentParser:
        child.add_argument("--json", action="store_true")
        child.add_argument("--limit", type=int, default=SG.DEFAULT_LIMIT)
        child.add_argument("--cursor", default=None)
        return child

    common(children.add_parser("list"))
    show = common(children.add_parser("show"))
    show.add_argument("id")
    common(children.add_parser("audit"))
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
