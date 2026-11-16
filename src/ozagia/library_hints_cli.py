"""``python -m ozagia.library_hints_cli``: detect, explain and rate hints.

Commands::

    detect --root R --diff BASE..HEAD [--max N] [--json] [--out FILE]
    detect --root R --changed PATH [PATH ...] [--max N] [--json]
    explain SYMBOL [--root R] [--json]
    feedback HINT_ID accepted|ignored [--root R] [--json]

Detection reads an explicit diff or changed list; it never scans the whole
repository. Hints are advisory: this command does not rewrite code, install a
package, call a model or use the network. Feedback is stored locally under the
state directory and is never sent anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from ozagia.core import library_hints as L

_REV = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./^~@{}-]{0,199}\Z")
_MAX_FILE_BYTES = L.MAX_FILE_BYTES


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.library_hints_cli",
        description="advisory library-first hints for a diff or changed list")
    sub = parser.add_subparsers(dest="command", required=True)

    detect = sub.add_parser("detect", help="detect hints for a diff or paths")
    detect.add_argument("--root", default=None,
                        help="repository root (default: the current directory)")
    source = detect.add_mutually_exclusive_group(required=True)
    source.add_argument("--diff", metavar="BASE..HEAD",
                        help="committed range to inspect")
    source.add_argument("--changed", nargs="+", metavar="PATH",
                        help="explicit changed paths in the working tree")
    detect.add_argument("--max", type=int, default=None, dest="max_per_edit",
                        help="hints per file (default: hints.max_per_edit)")
    detect.add_argument("--mode", choices=L.MODES, default=None,
                        help="override hints.library for this run")
    detect.add_argument("--ecosystem", action="append", choices=L.ECOSYSTEMS,
                        default=None, help="limit to an ecosystem (repeatable)")
    detect.add_argument("--json", action="store_true", help="emit JSON")
    detect.add_argument("--out", default=None,
                        help="also write the output atomically to this path")
    recording = detect.add_mutually_exclusive_group()
    recording.add_argument("--record", dest="record", action="store_true",
                           default=None, help="record events")
    recording.add_argument("--no-record", dest="record", action="store_false",
                           help="do not record events")
    detect.add_argument("--session", default="cli",
                        help="session label for recorded events")

    explain = sub.add_parser(
        "explain", help="explain a symbol, capability or package")
    explain.add_argument("symbol", metavar="SYMBOL")
    explain.add_argument("--root", default=None)
    explain.add_argument("--json", action="store_true")
    explain.add_argument("--limit", type=int, default=10,
                         help="events shown (default: 10)")

    feedback = sub.add_parser(
        "feedback", help="record local accepted/ignored feedback")
    feedback.add_argument("hint_id", metavar="HINT_ID")
    feedback.add_argument("verdict", choices=("accepted", "ignored"))
    feedback.add_argument("--root", default=None)
    feedback.add_argument("--json", action="store_true")
    feedback.add_argument("--session", default="cli")
    return parser


def _root(args: argparse.Namespace) -> Path:
    return Path(args.root).resolve() if args.root else Path.cwd().resolve()


def _git(root: Path, args: list[str], *, optional: bool = False,
         timeout: int = 30) -> bytes | None:
    env = dict(os.environ)
    env.setdefault("GIT_PAGER", "cat")
    env["LC_ALL"] = "C"
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True,
            timeout=timeout, check=False, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        if optional:
            return None
        raise L.LibraryHintsError(f"git failed: {exc}") from exc
    if proc.returncode != 0:
        if optional:
            return None
        message = proc.stderr.decode("utf-8", errors="replace").strip()
        raise L.LibraryHintsError(
            f"git {' '.join(args)} failed: {message[:400]}")
    return proc.stdout


def _parse_range(spec: str) -> tuple[str, str]:
    text = (spec or "").strip()
    if "..." in text:
        base, _, head = text.partition("...")
    elif ".." in text:
        base, _, head = text.partition("..")
    else:
        raise L.LibraryHintsError("--diff must be BASE..HEAD")
    base, head = base.strip(), head.strip()
    if not base or not head:
        raise L.LibraryHintsError("--diff must name both BASE and HEAD")
    for rev in (base, head):
        if not _REV.fullmatch(rev):
            raise L.LibraryHintsError(f"invalid revision {rev!r}")
    return base, head


def _decode(data: bytes | None) -> str | None:
    if data is None or len(data) > _MAX_FILE_BYTES:
        return None
    if b"\0" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text[1:] if text.startswith("\ufeff") else text


def _blob_size(root: Path, spec: str) -> int | None:
    raw = _git(root, ["cat-file", "-s", spec], optional=True)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _diff_changes(root: Path, spec: str) -> list[L.FileChange]:
    base, head = _parse_range(spec)
    raw = _git(root, ["diff", "--name-status", "-z", base, head, "--"])
    parts = (raw or b"").decode("utf-8", "surrogateescape").split("\0")
    changes: list[L.FileChange] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if not status:
            continue
        if status[0] in ("R", "C"):
            if index + 1 >= len(parts):
                break
            old_path, new_path = parts[index], parts[index + 1]
            index += 2
        else:
            if index >= len(parts):
                break
            old_path = new_path = parts[index]
            index += 1
        if status[0] == "D":
            continue
        before = None
        if status[0] != "A":
            blob = f"{base}:{old_path}"
            size = _blob_size(root, blob)
            if size is None or size > _MAX_FILE_BYTES:
                continue
            before = _decode(_git(root, ["show", blob], optional=True))
            if before is None:
                continue
        blob = f"{head}:{new_path}"
        size = _blob_size(root, blob)
        if size is None or size > _MAX_FILE_BYTES:
            continue
        after = _decode(_git(root, ["show", blob], optional=True))
        if after is None:
            continue
        changes.append(L.FileChange(new_path, before, after))
    return changes


def _format_detect(result: dict) -> str:
    hints = result.get("hints") or []
    if not hints:
        return "no library hints\n"
    return "\n".join(str(hint.get("message") or "") for hint in hints) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _detect(args: argparse.Namespace) -> int:
    root = _root(args)
    settings = L.settings_for(root)
    mode = args.mode or settings.mode
    max_per_edit = (settings.max_per_edit if args.max_per_edit is None
                    else args.max_per_edit)
    if not 0 <= max_per_edit <= 20:
        print("error: --max must be between 0 and 20", file=sys.stderr)
        return 2
    ecosystems = tuple(args.ecosystem) if args.ecosystem else settings.ecosystems
    record = settings.record if args.record is None else bool(args.record)
    if args.diff:
        changes = _diff_changes(root, args.diff)
        result = L.detect_files(
            root, changes, mode=mode, max_per_edit=max_per_edit,
            ecosystems=ecosystems, session=args.session, record=record)
    else:
        result = L.hints_for_paths(
            root, args.changed or (), mode=mode, session=args.session,
            settings=settings, record=record, max_per_edit=max_per_edit)
    text = (json.dumps(result, indent=2, sort_keys=True) + "\n"
            if args.json else _format_detect(result))
    if args.out:
        _write_atomic(Path(args.out), text)
    print(text, end="")
    return 0


def _format_explain(result: dict) -> str:
    lines: list[str] = []
    events = result.get("events") or []
    query = result.get("query")
    if events:
        lines.append(f"library hints for `{query}`:")
        for event in events:
            reason = event.get("reason")
            suffix = f" ({reason})" if reason else ""
            lines.append(
                f"  {event.get('ts')} {event.get('status')} "
                f"{event.get('capability')} -> {event.get('package')} "
                f"({event.get('availability')}, {event.get('confidence')}) "
                f"in {event.get('file')} [{event.get('hint_id')}]{suffix}")
    registry = result.get("registry") or []
    if registry:
        if lines:
            lines.append("")
        for item in registry:
            line = (f"{item.get('capability')}: {item.get('title')}; "
                    f"packages: {', '.join(item.get('packages') or [])}")
            note = item.get("stdlib_note")
            if note:
                line += f"; {note}"
            lines.append(line)
    if not lines:
        lines.append(f"no library hints recorded for `{query}`")
    return "\n".join(lines) + "\n"


def _explain(args: argparse.Namespace) -> int:
    root = _root(args)
    result = L.explain(root, args.symbol, limit=max(0, args.limit))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(_format_explain(result), end="")
    return 0


def _feedback(args: argparse.Namespace) -> int:
    root = _root(args)
    record = L.record_feedback(root, args.hint_id, args.verdict,
                               session=args.session)
    if args.json:
        print(json.dumps({"schema": L.SCHEMA, "feedback": record},
                         indent=2, sort_keys=True))
    else:
        print(f"recorded feedback for {record['hint_id']}: "
              f"{record['verdict']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "detect":
            return _detect(args)
        if args.command == "explain":
            return _explain(args)
        return _feedback(args)
    except (L.LibraryHintsError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
