"""``python -m ozagia.secrets_cli``: secret references without values.

Commands::

    providers [--root R] [--json]
    list [--root R] [--json]
    check REF... [--root R] [--json]
    doctor [--root R] [--json]

No command resolves a value into output. ``check`` reports status only:
``0`` when every reference is available, ``1`` when one is missing, denied,
unavailable or failed, and ``2`` for an invalid reference or usage error.
``list`` and ``doctor`` are inspection commands and exit ``0`` after a
completed report. The only files read are the two project configuration files
and provider metadata; nothing is written.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ozagia.core import secrets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.secrets_cli",
        description="secret references, provider status and redacted checks")
    sub = parser.add_subparsers(dest="command", required=True)

    providers = sub.add_parser(
        "providers", help="show provider names, availability and requirements")
    providers.add_argument("--root", default=".",
                           help="repository root (default: current directory)")
    providers.add_argument("--json", action="store_true",
                           help="emit deterministic JSON")

    listed = sub.add_parser(
        "list", help="find secret:// references in both configuration files")
    listed.add_argument("--root", default=".",
                        help="repository root (default: current directory)")
    listed.add_argument("--json", action="store_true",
                        help="emit deterministic JSON")

    check = sub.add_parser(
        "check", help="check references without resolving their values")
    check.add_argument("refs", nargs="+", metavar="REF",
                       help="one or more secret:// references")
    check.add_argument("--root", default=".",
                       help="repository root (default: current directory)")
    check.add_argument("--json", action="store_true",
                       help="emit deterministic JSON")

    doctor = sub.add_parser(
        "doctor", help="show configuration, provider status and diagnostics")
    doctor.add_argument("--root", default=".",
                        help="repository root (default: current directory)")
    doctor.add_argument("--json", action="store_true",
                        help="emit deterministic JSON")
    return parser


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _warn_config(broker: secrets.SecretBroker) -> None:
    for message in broker.config.errors:
        print(f"warning: {message}", file=sys.stderr)


def _provider_lines(broker: secrets.SecretBroker) -> str:
    lines = ["providers:"]
    for item in broker.providers():
        flags = " ".join(
            f"{name}={'yes' if item[name] else 'no'}"
            for name in ("enabled", "available", "ready"))
        requires = ", ".join(item["requires"]) or "none"
        line = f"  {item['name']:<8} {flags}  requires: {requires}"
        if item["notes"]:
            line += f"  ({item['notes']})"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _doctor_lines(broker: secrets.SecretBroker) -> str:
    info = broker.describe()
    lines = [
        "secrets:",
        f"  root: {info['root']}",
        f"  legacy config: {info['files'].get('legacy') or '(none)'}",
        f"  repository config: {info['files'].get('repo') or '(none)'}",
        f"  file base: {info['secrets_dir']}",
        f"  allow_network: {'yes' if info['allow_network'] else 'no'}",
        f"  allow_absolute_files: "
        f"{'yes' if info['allow_absolute_files'] else 'no'}",
        f"  max_bytes: {info['max_bytes']}",
        f"  redact_output: {'yes' if info['redact_output'] else 'no'}",
    ]
    lines.extend(_provider_lines(broker).rstrip("\n").splitlines())
    if info["errors"]:
        lines.append("  errors:")
        lines.extend(f"    {item}" for item in info["errors"])
    if info["warnings"]:
        lines.append("  warnings:")
        lines.extend(f"    {item}" for item in info["warnings"])
    if info["unknown"]:
        lines.append("  unknown keys: " + ", ".join(info["unknown"]))
    return "\n".join(lines) + "\n"


def _list_payload(broker: secrets.SecretBroker) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for hit in broker.discover_refs():
        check = broker.check(hit.ref)
        payload.append({
            "ref": hit.ref,
            "key": hit.key,
            "source": str(hit.source),
            "valid": hit.valid,
            "error": hit.error,
            "status": check.status,
            "provider": check.provider,
            "code": check.code,
            "detail": check.detail,
        })
    return payload


def _list_lines(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no secret:// references found in the configuration files\n"
    lines = ["references:"]
    for row in rows:
        suffix = f"  ({row['key']} in {row['source']})"
        lines.append(f"  {row['status']:<20} {row['ref']}{suffix}")
        if row["detail"]:
            lines.append(f"    {row['detail']}")
    return "\n".join(lines) + "\n"


def _check_lines(checks: list[secrets.SecretCheck]) -> str:
    lines = ["checks:"]
    for check in checks:
        lines.append(f"  {check.status:<20} {check.reference}")
        if check.code or check.detail:
            detail = " ".join(item for item in (check.code, check.detail)
                              if item)
            lines.append(f"    {detail}")
    return "\n".join(lines) + "\n"


def _check_exit(checks: list[secrets.SecretCheck]) -> int:
    if any(check.status == secrets.STATUS_INVALID for check in checks):
        return 2
    if any(check.status != secrets.STATUS_AVAILABLE for check in checks):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        broker = secrets.SecretBroker(args.root)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _warn_config(broker)

    if args.command == "providers":
        if args.json:
            print(_json({
                "providers": broker.providers(),
                "allow_cli": list(broker.config.allow_cli),
                "allow_network": broker.config.allow_network,
                "max_bytes": broker.config.max_bytes,
            }), end="")
        else:
            print(_provider_lines(broker), end="")
        return 0

    if args.command == "doctor":
        info = broker.describe()
        print(_json(info) if args.json else _doctor_lines(broker), end="")
        return 0

    if args.command == "list":
        rows = _list_payload(broker)
        print(_json({"references": rows}) if args.json
              else _list_lines(rows), end="")
        return 0

    checks = broker.check_many(args.refs)
    print(_json({"checks": [check.as_dict() for check in checks]})
          if args.json else _check_lines(checks), end="")
    return _check_exit(checks)


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
