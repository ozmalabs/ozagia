"""``python -m ozagia.integrations.ocelot``: resolve, install and verify Ocelot.

Commands:
    resolve   where Ocelot comes from, or "not installed"
    status    path, version, source, manifest and licence note
    install   dry run by default; --apply downloads and installs
    verify    check an installed manifest and its recorded files

Exit status is 0 on success (including a dry run and an informational
resolve/status), 1 when verification fails, and 2 for validation or I/O errors.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import (
    OcelotError,
    install,
    resolve,
    status,
    verify,
)


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _root(value: str | None) -> Path | None:
    return Path(value).expanduser().resolve() if value else None


def _version(value: str | None) -> str:
    return value if value else "unknown"


def _format_resolution(result: dict[str, Any]) -> str:
    if result["state"] == "not_installed":
        lines = ["Ocelot: not installed", f"install root: {result['install_root']}"]
    else:
        lines = [
            f"Ocelot {_version(result['version'])}",
            f"path: {result['path']}",
            f"source: {result['source']}",
        ]
        if result.get("manifest_path"):
            lines.append(f"manifest: {result['manifest_path']}")
    if result.get("note"):
        lines.append(f"note: {result['note']}")
    for item in result.get("invalid_installs") or []:
        lines.append(f"ignored: {item['path']} ({item['error']})")
    return "\n".join(lines)


def _format_status(result: dict[str, Any]) -> str:
    installed = "yes" if result["installed"] else "no"
    lines = [
        f"state: {result['state']}",
        f"installed: {installed}",
        f"path: {result['path'] or '-'}",
        f"version: {_version(result['version'])}",
        f"source: {result['source'] or '-'}",
        f"install root: {result['install_root']}",
    ]
    if result.get("manifest_path"):
        lines.append(f"manifest: {result['manifest_path']}")
    manifest = result.get("manifest")
    if manifest:
        lines.append(f"archive sha256: {manifest.get('sha256')}")
        lines.append(f"installed at: {manifest.get('installed_at')}")
        lines.append(f"files: {manifest.get('file_count')}")
    lines.append(
        "licence: MIT (Ocelot is MIT-licensed; ozagia does not bundle it)")
    if result.get("reconciliation_optional"):
        lines.append("reconciliation: optional")
    if result.get("note"):
        lines.append(f"note: {result['note']}")
    for item in result.get("invalid_installs") or []:
        lines.append(f"ignored: {item['path']} ({item['error']})")
    return "\n".join(lines)


def _format_install(result: dict[str, Any]) -> str:
    action = result["action"]
    version = _version(result.get("version"))
    if action == "dry_run":
        return "\n".join([
            f"dry run: would install Ocelot {version}",
            f"source: {result['source']}",
            f"sha256: {result['sha256']}",
            f"path: {result['path']}",
            "no download or write was performed; pass --apply to install",
        ])
    if action == "already_installed":
        return f"Ocelot {version} is already installed at {result['path']}"
    if action == "installed":
        return "\n".join([
            f"installed Ocelot {version}",
            f"path: {result['path']}",
            f"manifest: {result['manifest_path']}",
            f"sha256: {result['sha256']}",
        ])
    return f"Ocelot {version}: {action}"


def _format_verify(result: dict[str, Any]) -> str:
    if result["ok"]:
        return "\n".join([
            f"Ocelot {_version(result['version'])} verified",
            f"path: {result['path']}",
            f"files: {result['files_checked']}",
            f"archive sha256: {result['sha256']}",
        ])
    lines = [f"Ocelot {_version(result['version'])} verification failed",
             f"path: {result['path']}"]
    lines.extend(f"error: {error}" for error in result["errors"])
    for key in ("files_missing", "files_mismatched", "files_unsafe"):
        for path in result.get(key) or []:
            lines.append(f"{key}: {path}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.integrations.ocelot",
        description="resolve, install and verify a separate Ocelot checkout; "
                    "Ocelot is not bundled and is never downloaded implicitly")
    sub = parser.add_subparsers(dest="command", required=True)

    resolved = sub.add_parser("resolve", help="show the resolved Ocelot path")
    resolved.add_argument("--root", default=None,
                          help="repository whose sibling checkout is considered")
    resolved.add_argument("--dest", default=None,
                          help="install root to search (default: user data dir)")
    resolved.add_argument("--json", action="store_true")

    state = sub.add_parser("status", help="path, version, source, manifest and licence")
    state.add_argument("--root", default=None)
    state.add_argument("--dest", default=None)
    state.add_argument("--json", action="store_true")

    installing = sub.add_parser(
        "install", help="install a pinned archive (dry run unless --apply)")
    installing.add_argument("--version", required=True,
                            help="Ocelot version; also the install directory name")
    installing.add_argument("--url", required=True,
                            help="HTTPS .tar.gz/.tgz/.zip archive URL")
    installing.add_argument("--sha256", required=True,
                            help="pinned SHA-256 of the archive (64 hexadecimal characters)")
    installing.add_argument("--dest", default=None,
                            help="install root (default: ~/.local/share/ozagia/ocelot)")
    installing.add_argument("--apply", action="store_true",
                            help="download and install; without it this is a dry run")
    installing.add_argument("--json", action="store_true")

    checking = sub.add_parser("verify", help="verify an installed manifest and files")
    checking.add_argument("--version", default=None,
                          help="installed version (default: newest installed)")
    checking.add_argument("--dest", default=None)
    checking.add_argument("--sha256", default=None,
                          help="also require this archive checksum")
    checking.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "resolve":
            result = resolve(_root(args.root), dest=args.dest)
            if args.json:
                _print_json(result)
            else:
                print(_format_resolution(result))
            return 0
        if args.command == "status":
            result = status(_root(args.root), dest=args.dest)
            if args.json:
                _print_json(result)
            else:
                print(_format_status(result))
            return 0
        if args.command == "install":
            result = install(
                args.version, args.url, args.sha256, dest=args.dest,
                dry_run=not args.apply, allow_download=args.apply)
            if args.json:
                _print_json(result)
            else:
                print(_format_install(result))
            return 0
        if args.command == "verify":
            result = verify(args.version, dest=args.dest, sha256=args.sha256)
            if args.json:
                _print_json(result)
            else:
                print(_format_verify(result))
            return 0 if result["ok"] else 1
    except (OcelotError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"error: unknown command {args.command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
