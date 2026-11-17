"""``python -m ozagia.secrets_cli``: secret references without values.

Commands::

    providers [--root R] [--json]
    list [--root R] [--session S] [--json]
    check REF... [--root R] [--session S] [--json]
    forget REF [--root R] [--session S] [--json]
    capture [--root R] [--session S] [--label L] [--ttl S] [--json]
    capture-text [--root R] [--session S] [--json]
    doctor [--root R] [--json]

No command resolves a value into output. ``capture`` reads one value from
standard input and ``capture-text`` reads text from standard input; neither
takes a value on the command line. Both use the daemon's memory-only
``SessionSecretStore``. If no daemon is available, ``capture-text`` fails
closed: it prints the text with detected secrets redacted and exits ``2``.
``check`` reports status only: ``0`` when every reference is available, ``1``
when one is missing, denied, unavailable or failed, and ``2`` for an invalid
reference or usage error. ``list`` and ``doctor`` are inspection commands and
exit ``0`` after a completed report. The only files read are the two project
configuration files and provider metadata; nothing is written.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from ozagia.core import secrets

_REF_TEXT = re.compile(r"secret://[A-Za-z0-9._~%/:@+-]+")


class _DaemonError(RuntimeError):
    """The daemon refused the request or is unavailable."""


def _safe(text: Any) -> str:
    """Redact an error while keeping ``secret://`` references intact."""
    from ozagia.core import security
    value = str(text)
    refs: list[str] = []

    def keep(match: re.Match[str]) -> str:
        refs.append(match.group(0))
        return f"\x00ref-{len(refs) - 1}\x00"

    masked = _REF_TEXT.sub(keep, value)
    redacted = security.redact_text(masked)
    for index, ref in enumerate(refs):
        redacted = redacted.replace(f"\x00ref-{index}\x00", ref)
    return redacted


def _secret_request(root: str, action: str, fields: dict[str, Any]
                    ) -> dict[str, Any]:
    """One daemon `secrets` request; a refusal is an error with no value."""
    from ozagia.daemon.jobcli import JobUnavailable, request
    try:
        reply = request(Path(root).resolve(), "secrets",
                        {"action": action, **fields})
    except JobUnavailable as exc:
        raise _DaemonError(str(exc)) from None
    except (OSError, ValueError) as exc:
        raise _DaemonError(f"{type(exc).__name__}: {exc}") from None
    if not isinstance(reply, dict) or not reply.get("ok"):
        detail = (reply.get("error") if isinstance(reply, dict) else None) \
            or "the daemon refused the request"
        raise _DaemonError(str(detail))
    return reply


def _read_stdin_text() -> str:
    text = sys.stdin.read()
    return text if isinstance(text, str) else str(text)


def _one_secret(text: str) -> str:
    """One value from stdin: strip only the line ending the terminal added."""
    if text.endswith("\n"):
        text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
    return text


def _session_args(args: argparse.Namespace) -> dict[str, str]:
    return {"harness": getattr(args, "harness", None) or "cli",
            "session": getattr(args, "session", None) or "cli"}


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
        "list", help="find secret:// references in both configuration files, "
                     "or list one session's captures with --session")
    listed.add_argument("--root", default=".",
                        help="repository root (default: current directory)")
    listed.add_argument("--session", default=None,
                        help="list this session's memory-only captures (daemon)")
    listed.add_argument("--harness", default=None,
                        help="harness for --session (default: cli)")
    listed.add_argument("--json", action="store_true",
                        help="emit deterministic JSON")

    check = sub.add_parser(
        "check", help="check references without resolving their values")
    check.add_argument("refs", nargs="+", metavar="REF",
                       help="one or more secret:// references")
    check.add_argument("--root", default=".",
                       help="repository root (default: current directory)")
    check.add_argument("--session", default=None,
                       help="check for this daemon session (needed for "
                            "secret://session refs)")
    check.add_argument("--harness", default=None,
                       help="harness for --session (default: cli)")
    check.add_argument("--json", action="store_true",
                       help="emit deterministic JSON")

    forget = sub.add_parser(
        "forget", help="forget one of this session's captures")
    forget.add_argument("ref", metavar="REF", help="a secret://session/... reference")
    forget.add_argument("--root", default=".",
                        help="repository root (default: current directory)")
    forget.add_argument("--session", default=None,
                        help="the capture's session (default: cli)")
    forget.add_argument("--harness", default=None,
                        help="harness for --session (default: cli)")
    forget.add_argument("--json", action="store_true",
                        help="emit deterministic JSON")

    capture = sub.add_parser(
        "capture", help="capture one value read from stdin; prints its reference")
    capture.add_argument("--root", default=".",
                         help="repository root (default: current directory)")
    capture.add_argument("--session", default=None,
                         help="the capture's session (default: cli)")
    capture.add_argument("--harness", default=None,
                         help="harness for --session (default: cli)")
    capture.add_argument("--label", default=None,
                         help="optional bounded label")
    capture.add_argument("--ttl", type=float, default=None, metavar="S",
                         help="lifetime in seconds (capped at 24 hours)")
    capture.add_argument("--json", action="store_true",
                         help="emit deterministic JSON")

    capture_text = sub.add_parser(
        "capture-text",
        help="rewrite secrets in text from stdin as references; fails closed "
             "when no daemon answers")
    capture_text.add_argument("--root", default=".",
                              help="repository root (default: current directory)")
    capture_text.add_argument("--session", default=None,
                              help="the capture's session (default: cli)")
    capture_text.add_argument("--harness", default=None,
                              help="harness for --session (default: cli)")
    capture_text.add_argument("--json", action="store_true",
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


def _check_from_rows(rows: list[dict[str, Any]]) -> list[secrets.SecretCheck]:
    return [
        secrets.SecretCheck(
            str(row.get("status") or secrets.STATUS_ERROR),
            reference=str(row.get("reference") or ""),
            provider=row.get("provider"), code=row.get("code"),
            detail=str(row.get("detail") or ""))
        for row in rows
    ]


def _session_rows_lines(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "no session captures\n"
    lines = ["session captures:"]
    for row in rows:
        label = f"  ({row['label']})" if row.get("label") else ""
        lines.append(f"  {row.get('ref')}{label}")
        lines.append(f"    fingerprint {row.get('fingerprint')}  "
                     f"expires {row.get('expires')}")
    return "\n".join(lines) + "\n"


def _capture_lines(row: dict[str, Any]) -> str:
    lines = [f"ref: {row.get('ref')}"]
    if row.get("fingerprint"):
        lines.append(f"fingerprint: {row['fingerprint']}")
    if row.get("expires") is not None:
        lines.append(f"expires: {row['expires']}")
    if row.get("label"):
        lines.append(f"label: {row['label']}")
    return "\n".join(lines) + "\n"


def _daemon_unavailable(command: str, exc: Exception) -> int:
    print(f"error: {command} needs the ozagia daemon: {_safe(exc)}",
          file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "capture-text":
        text = _read_stdin_text()
        fields = {"text": text, **_session_args(args)}
        try:
            reply = _secret_request(args.root, "capture_text", fields)
        except _DaemonError as exc:
            # Fail closed: no raw value, only locally redacted text. An
            # unscanned tail is withheld rather than echoed.
            capture = secrets.detect_text(text)
            redacted = capture.text
            if capture.truncated:
                redacted = (capture.text[:secrets.MAX_DETECT_CHARS]
                            + "\n[ozagia] text after the scan limit withheld\n")
            sys.stdout.write(redacted)
            print(f"error: capture-text could not reach the daemon; the text "
                  f"was redacted and no value was captured: {_safe(exc)}",
                  file=sys.stderr)
            return 2
        if args.json:
            print(_json(reply), end="")
        else:
            sys.stdout.write(str(reply.get("text") or ""))
        return 0

    if args.command == "capture":
        value = _one_secret(_read_stdin_text())
        if not value:
            print("error: capture reads one non-empty value from stdin",
                  file=sys.stderr)
            return 2
        fields = {"value": value, **_session_args(args)}
        if args.label is not None:
            fields["label"] = args.label
        if args.ttl is not None:
            fields["ttl_s"] = args.ttl
        try:
            reply = _secret_request(args.root, "capture", fields)
        except _DaemonError as exc:
            return _daemon_unavailable("capture", exc)
        print(_json(reply) if args.json else _capture_lines(reply), end="")
        return 0

    if args.command == "forget":
        fields = {"ref": args.ref, **_session_args(args)}
        try:
            reply = _secret_request(args.root, "forget", fields)
        except _DaemonError as exc:
            return _daemon_unavailable("forget", exc)
        if args.json:
            print(_json(reply), end="")
        else:
            state = "forgotten" if reply.get("forgotten") else "not found"
            print(f"{state}: {reply.get('ref')}")
        return 0 if reply.get("forgotten") else 1

    if args.command == "list" and args.session is not None:
        fields = _session_args(args)
        try:
            reply = _secret_request(args.root, "list", fields)
        except _DaemonError as exc:
            return _daemon_unavailable("list --session", exc)
        rows = list(reply.get("secrets") or [])
        print(_json(reply) if args.json else _session_rows_lines(rows), end="")
        return 0

    if args.command == "check" and args.session is not None:
        fields = {"refs": list(args.refs), **_session_args(args)}
        try:
            reply = _secret_request(args.root, "check", fields)
        except _DaemonError as exc:
            return _daemon_unavailable("check --session", exc)
        rows = list(reply.get("checks") or [])
        checks = _check_from_rows(rows)
        print(_json({"checks": rows}) if args.json else _check_lines(checks),
              end="")
        return _check_exit(checks)

    try:
        broker = secrets.SecretBroker(args.root)
    except (OSError, ValueError) as exc:
        print(f"error: {_safe(exc)}", file=sys.stderr)
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
