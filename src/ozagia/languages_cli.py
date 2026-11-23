"""``ozagia languages``: per-language support packages and their components.

Commands::

    list [--json]
    detect [--root R] [--json]
    ensure [--root R] [--json] [--no-install] [--no-network] [--require-ready]

``detect`` is read-only. ``ensure`` reports missing components and, unless
``--no-install`` is given, installs the ones ozagia knows how to install
(Python packages in the active environment and rustup components). It never
uses sudo, never runs an OS package manager and never executes project code.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ozagia.languages import (
    PREFIX,
    detect_languages,
    ensure_for_root,
    registry_payload,
    support_status,
)

PROG = "ozagia languages"


def _json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _root(value: str | None) -> Path:
    from ozagia.config import repo_root
    return Path(value).resolve() if value else repo_root()


def _format_registry(rows: list[dict[str, Any]]) -> str:
    lines = [f"{PREFIX} {len(rows)} support packages"]
    for row in rows:
        lines.append(f"  {row['language']}: {row['label']} "
                     f"({', '.join(row['suffixes'])}); extra "
                     f"ozagia[{row['extra']}]")
        for item in row["components"]:
            mark = "required" if item["required"] else "optional"
            install = ("install=" + " ".join(item["install"])
                       if item.get("install") else "no automatic install")
            manager = item.get("effective_manager") or item["manager"]
            lines.append(f"    {item['id']} [{mark}, {manager}]: "
                         f"{install}")
    return "\n".join(lines)


def _format_detect(payload: dict[str, Any]) -> str:
    languages = payload["languages"]
    if not languages:
        return f"{PREFIX} no supported languages detected in {payload['root']}"
    lines = [f"{PREFIX} detected in {payload['root']}: "
             f"{', '.join(item['label'] for item in languages)}"]
    for item in languages:
        lines.append(f"  {item['language']}: "
                     f"{'ready' if item['ready'] else 'missing components'}")
        for component in item["components"]:
            mark = "ok" if component["installed"] else "missing"
            extra = ""
            if component["extra"]:
                extra = f"; ozagia[{component['extra']}]"
            manager = (component.get("effective_manager")
                       or component["manager"])
            lines.append(f"    {mark} {component['id']} "
                         f"({manager}{extra})")
    if payload.get("truncated"):
        lines.append("  (file scan truncated; later directories were not read)")
    return "\n".join(lines)


def _format_ensure(payload: dict[str, Any]) -> str:
    lines = [f"{PREFIX} checked {len(payload['languages'])} language(s) "
             f"in {payload['root']}"]
    for item in payload["languages"]:
        state = "ready" if item["ready"] else "missing components"
        lines.append(f"  {item['label']}: {state}")
        if item["installed"]:
            lines.append(f"    installed: {', '.join(item['installed'])}")
        if item["failed"]:
            lines.append(f"    failed: {', '.join(item['failed'])}")
        if item["blocked"]:
            lines.append(f"    blocked: {', '.join(item['blocked'])}")
        for component in item["missing"]:
            install = (" ".join(component["install"])
                       if component.get("install") else "manual install")
            lines.append(f"    missing {component['id']}: {install}")
    return "\n".join(lines)


def _add_actions(children: Any) -> None:
    listed = children.add_parser(
        "list", help="list the per-language support packages")
    listed.add_argument("--json", action="store_true", help="print JSON")

    detect = children.add_parser(
        "detect", help="detect languages and report component readiness")
    detect.add_argument("--root", default=None,
                        help="checkout to inspect (default: the repo root)")
    detect.add_argument("--json", action="store_true", help="print JSON")

    ensure = children.add_parser(
        "ensure", help="report and install missing language components")
    ensure.add_argument("--root", default=None,
                        help="checkout to inspect (default: the repo root)")
    ensure.add_argument("--json", action="store_true", help="print JSON")
    ensure.add_argument(
        "--no-install", action="store_true",
        help="report missing components without installing anything")
    ensure.add_argument(
        "--no-network", action="store_true",
        help="refuse installs that need the network")
    ensure.add_argument(
        "--require-ready", action="store_true",
        help="exit 1 when a required component is still missing")


def add_parser(sub: Any) -> None:
    parser = sub.add_parser(
        "languages",
        help="detect, report and install per-language support packages")
    _add_actions(parser.add_subparsers(dest="action", required=True))


def run(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "") or "")
    as_json = bool(getattr(args, "json", False))
    if action == "list":
        rows = registry_payload()
        _json(rows) if as_json else print(_format_registry(rows))
        return 0

    root = _root(getattr(args, "root", None))
    if action == "detect":
        found = detect_languages(root)
        payload = {
            "root": found["root"],
            "files_scanned": found["files_scanned"],
            "truncated": found["truncated"],
            "languages": [support_status(item, root=root)
                          for item in found["languages"]],
        }
        _json(payload) if as_json else print(_format_detect(payload))
        return 0

    if action == "ensure":
        network = not bool(getattr(args, "no_network", False))
        payload = ensure_for_root(
            root,
            install=not bool(getattr(args, "no_install", False)),
            network=network,
            report=(lambda line: print(line, file=sys.stderr))
            if as_json else print,
        )
        if as_json:
            _json(payload)
        else:
            print(_format_ensure(payload))
        return 0 if payload["ok"] or not getattr(args, "require_ready", False) else 1

    print(f"{PROG}: unknown action {action!r}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="per-language support packages, detection and installs")
    _add_actions(parser.add_subparsers(dest="action", required=True))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
