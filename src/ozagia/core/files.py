"""Created-file ledger, session manifests, Markdown routing and cleanup.

The ledger records metadata (path, hash, size, creator, reason, classification,
references) and never stores prompts or file contents. It is a bounded JSONL
file under the state directory.

Session start writes a bounded worktree manifest under
`.ozagia/files/sessions/`; session end diffs against it so pre-existing
untracked files are not attributed to the session. Cleanup is preview-first:
`cleanup_plan` issues a one-time token, `cleanup_apply` moves planned files
under `.ozagia/trash/<timestamp>/` (never deletes), and `restore` moves them
back with conflict checks. Trash is retained until a person explicitly purges
it: `trash_purge` is a dry-run that issues a one-time token, and applying the
token deletes only old, intact trash entries (never repository files) and
appends an audit row under `.ozagia/trash/audit.jsonl`.

Trash retention comes from the typed setting `files.retention_days`
(default 30). Nothing purges trash automatically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ozagia.config import state_path

SCHEMA = 1
LEDGER_DIR = "files"
LEDGER_FILE = "ledger.jsonl"
MAX_LEDGER_RECORDS = 10000
MAX_LEDGER_KEEP = 5000
MAX_HASH_BYTES = 8 * 1024 * 1024
MAX_SCAN_FILES = 2000
MAX_SCAN_BYTES = 8 * 1024 * 1024
# A scan records at most this many new files (one `git status` run).
MAX_SCAN_RECORDS = 500
# A scan looks at at most this many status entries, so already-recorded
# files do not stop a later scan from reaching new ones.
MAX_SCAN_CANDIDATES = 5000
# `record_paths` examines at most this many paths per call.
MAX_RECORD_PATHS = 200
# A session start manifest is bounded by files, by total bytes hashed, and by
# the number of manifests kept. Large files are noted, not hashed.
MAX_MANIFEST_FILES = 5000
MAX_MANIFEST_HASH_BYTES = 64 * 1024 * 1024
MAX_SESSION_MANIFESTS = 64
MAX_SESSION_RECORDS = 500
# Cleanup previews are one-time, short-lived tokens. Trash runs are kept until
# a person purges them; this only bounds how many runs `list_trash` reads.
CLEANUP_PLAN_TTL_S = 15 * 60
MAX_CLEANUP_PLANS = 64
MAX_TRASH_RUNS = 200
# Trash purge plans are one-time and short-lived too. Retention is a policy
# label (`files.retention_days`, default 30): nothing is purged automatically.
DEFAULT_TRASH_RETENTION_DAYS = 30
TRASH_PURGE_PLAN_TTL_S = 15 * 60
MAX_TRASH_PURGE_PLANS = 64
MAX_TRASH_AUDIT_RECORDS = 5000
TRASH_AUDIT_FILE = "audit.jsonl"
# The reason is a short label, never a prompt or file content.
MAX_REASON_CHARS = 200
# Created_by strings (session, harness, task, tool) are labels too.
MAX_LABEL_CHARS = 200
HISTORY_DIR = "history"
SCRATCH_DIR = ".ozagia/scratch"
ROOT_ALLOW = frozenset({
    "README.md", "CHANGELOG.md", "LICENSE", "LICENSE.md", "CONTRIBUTING.md",
    "AGENTS.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "NOTICE.md",
})
SKIP_PARTS = frozenset({
    ".git", ".ozagia", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".next", "target",
    "build", "dist", "coverage", "htmlcov", ".cache",
})
SOURCE_SUFFIXES = frozenset({
    ".py", ".pyi", ".rs", ".js", ".jsx", ".ts", ".tsx", ".go", ".java",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".lua", ".sh", ".bash", ".zsh",
    ".toml", ".yaml", ".yml", ".json", ".sql", ".css", ".html",
})


class FileLedgerError(RuntimeError):
    """The repository or ledger could not be read."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _git(root: Path, args: Sequence[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("LC_ALL", "C")
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            errors="replace", timeout=timeout, env=env,
            stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FileLedgerError(f"git {' '.join(args)} could not run: {exc}") from exc


def _relative(root: Path, path: str | Path) -> str | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        rel = candidate.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    return None if rel in ("", ".") else rel


def _skip(rel: str) -> bool:
    parts = set(Path(rel).parts)
    return bool(parts & SKIP_PARTS)


def _hash_file(path: Path) -> tuple[str | None, int]:
    try:
        size = path.stat().st_size
    except OSError:
        return None, 0
    if size > MAX_HASH_BYTES:
        return None, size
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None, size
    return digest.hexdigest(), size


def _classify(rel: str) -> str:
    parts = Path(rel).parts
    suffix = Path(rel).suffix.lower()
    name = Path(rel).name.lower()
    if parts and parts[0] == HISTORY_DIR:
        return "history"
    if parts[:2] == (".ozagia", "scratch"):
        return "scratch"
    if parts and parts[0] == "docs":
        if len(parts) > 1 and parts[1] == "plans":
            return "plan"
        if len(parts) > 1 and parts[1] == "results":
            return "result"
        if len(parts) > 1 and parts[1] == "research":
            return "research"
        return "docs"
    if suffix == ".md":
        return "note"
    if parts and parts[0] == "tests" or name.startswith("test_") or name.endswith("_test.py"):
        return "test"
    if suffix in SOURCE_SUFFIXES:
        return "source"
    return "other"


def _front_matter(content: str) -> dict[str, Any]:
    if not content.startswith("---"):
        return {}
    fields: dict[str, Any] = {}
    for line in content.splitlines()[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip().lower()
        raw = value.strip()
        if not key:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            fields[key] = [item.strip().strip("'\"")
                           for item in raw[1:-1].split(",") if item.strip()]
        else:
            fields[key] = raw.strip("'\"")
    return fields


def _title(content: str) -> str:
    lines = content.splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                start = index + 1
                break
    for line in lines[start:]:
        text = line.strip()
        if text.startswith("#"):
            return text.lstrip("#").strip()[:120]
        if text:
            return text[:120]
    return ""


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:60] or "note"


def _shingles(text: str, size: int = 3) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    if len(words) < size:
        return set(words)
    return {" ".join(words[index:index + size])
            for index in range(len(words) - size + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _clean_reason(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:MAX_REASON_CHARS] or "unknown"


def _label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:MAX_LABEL_CHARS] or None


def _existing_markdown(root: Path) -> Iterable[Path]:
    count = 0
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name not in SKIP_PARTS]
        base = Path(directory)
        for name in files:
            if not name.lower().endswith(".md"):
                continue
            path = base / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if _skip(rel):
                continue
            yield path
            count += 1
            if count >= MAX_SCAN_FILES:
                return


def markdown_route(path: str | Path, content: str = "", *,
                   root: str | Path | None = None,
                   context: Mapping[str, Any] | None = None,
                   check_duplicates: bool = True) -> dict[str, Any]:
    """Classify a Markdown path and suggest a durable destination.

    Deterministic: path rules, front matter, title and headings, hashes and
    word-shingle similarity. No model call and no write.
    """
    root_path = Path(root).resolve() if root is not None else Path.cwd().resolve()
    raw = Path(path)
    rel = _relative(root_path, raw)
    if rel is None:
        return {
            "schema": SCHEMA, "path": str(raw), "kind": "outside",
            "allowed": False, "suggested_path": None,
            "reason": "path is outside the repository", "duplicate_of": None,
            "append_to": None, "confidence": "high",
        }
    context = dict(context or {})
    fields = _front_matter(content)
    title = _title(content)
    slug = _slug(str(fields.get("title") or title or Path(rel).stem))
    date_value = str(context.get("date") or fields.get("created") or _today())
    session = str(context.get("session") or fields.get("session") or "manual")
    parts = Path(rel).parts
    front_kind = str(fields.get("kind") or "").lower()

    if Path(rel).name in ROOT_ALLOW and len(parts) == 1:
        kind = "root"
    elif parts and parts[0] == HISTORY_DIR:
        kind = "history"
    elif parts[:2] == (".ozagia", "scratch"):
        kind = "scratch"
    elif parts and parts[0] == "docs":
        if len(parts) > 1 and parts[1] == "plans":
            kind = "plan"
        elif len(parts) > 1 and parts[1] == "results":
            kind = "result"
        elif len(parts) > 1 and parts[1] == "research":
            kind = "research"
        elif (front_kind in {"note", "scratch", "history"}
              or str(fields.get("status") or "").lower() == "historical"):
            kind = "note"
        else:
            kind = "docs"
    elif front_kind in {"plan", "result", "report", "research", "history",
                        "scratch", "note"}:
        kind = "result" if front_kind == "report" else front_kind
    elif "plan" in Path(rel).stem.lower():
        kind = "plan"
    elif any(word in Path(rel).stem.lower() for word in ("report", "result")):
        kind = "result"
    else:
        kind = "note"

    if kind == "root":
        suggested = rel
        reason = "root documentation allowlist"
    elif kind == "plan":
        suggested = rel if parts[:2] == ("docs", "plans") else (
            f"docs/plans/{date_value}_{slug}.md")
        reason = "dated plans live in docs/plans"
    elif kind == "result":
        suggested = rel if parts[:2] == ("docs", "results") else (
            f"docs/results/{slug}_{date_value}/README.md")
        reason = "dated results live in docs/results"
    elif kind == "research":
        suggested = rel if parts[:2] == ("docs", "research") else (
            f"docs/research/{slug}.md")
        reason = "research notes live in docs/research"
    elif kind == "docs":
        suggested = rel
        reason = ("docs/ is for living docs; mark one-offs kind: note or "
                  "status: historical to route them to history")
    elif kind == "history":
        suggested = rel if len(parts) > 2 and parts[1][:4].isdigit() else (
            f"{HISTORY_DIR}/{date_value}/{slug}.md")
        reason = "one-off notes are date-partitioned in history"
    elif kind == "scratch":
        suggested = rel if len(parts) > 3 and parts[2] == session else (
            f"{SCRATCH_DIR}/{session}/{slug}.md")
        reason = "scratch notes live under the state directory"
    else:
        kind = "note"
        suggested = f"{HISTORY_DIR}/{date_value}/{slug}.md"
        reason = "one-off notes go to history, not the root or docs"

    allowed = (suggested == rel and kind in {
        "root", "plan", "result", "research", "docs", "history", "scratch",
    })
    duplicate: str | None = None
    append_to: str | None = None
    confidence = "low"
    if content and check_duplicates:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        total_bytes = 0
        for candidate in _existing_markdown(root_path):
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            total_bytes += size
            if total_bytes > MAX_SCAN_BYTES:
                break
            if candidate.resolve() == (root_path / rel).resolve():
                continue
            try:
                existing = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if hashlib.sha256(existing.encode("utf-8")).hexdigest() == digest:
                duplicate = candidate.relative_to(root_path).as_posix()
                append_to = duplicate
                confidence = "high"
                break
            existing_title = _title(existing)
            if existing_title and title and existing_title == title:
                duplicate = candidate.relative_to(root_path).as_posix()
                append_to = duplicate
                confidence = "medium"
                break
            if title and _jaccard(_shingles(content), _shingles(existing)) >= 0.85:
                duplicate = candidate.relative_to(root_path).as_posix()
                append_to = duplicate
                confidence = "low"
                break
    return {
        "schema": SCHEMA,
        "path": rel,
        "kind": kind,
        "allowed": allowed,
        "suggested_path": suggested,
        "reason": reason,
        "duplicate_of": duplicate,
        "append_to": append_to,
        "confidence": confidence,
        "title": title or None,
    }


def ledger_path(root: str | Path) -> Path:
    return state_path(root, LEDGER_DIR, LEDGER_FILE)


def _read_ledger(root: str | Path) -> list[dict[str, Any]]:
    path = ledger_path(root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-MAX_LEDGER_RECORDS:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _trim_ledger(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= MAX_LEDGER_RECORDS:
        return
    keep = lines[-MAX_LEDGER_KEEP:]
    try:
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except OSError:
        pass


def append_record(root: str | Path, record: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(record)
    item.setdefault("schema", SCHEMA)
    item.setdefault("recorded_at", _now())
    rel = item.get("path")
    if not isinstance(rel, str) or not rel or "\n" in rel:
        raise ValueError("record path must be a non-empty single-line string")
    if len(json.dumps(item, sort_keys=True)) > 64 * 1024:
        raise ValueError("ledger record exceeds 64 KiB")
    path = ledger_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")
    except OSError as exc:
        raise FileLedgerError(f"cannot write ledger: {exc}") from exc
    _trim_ledger(path)
    return item


def latest_records(root: str | Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for item in _read_ledger(root):
        path = item.get("path")
        if isinstance(path, str):
            latest[path] = item
    return latest


def list_files(root: str | Path, *, classification: str | None = None,
               status: str | None = "active", limit: int = 200) -> list[dict[str, Any]]:
    records = list(latest_records(root).values())
    if classification:
        records = [item for item in records
                   if item.get("classification") == classification]
    if status:
        records = [item for item in records if item.get("status") == status]
    records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return records[:max(0, min(int(limit), 1000))]


def explain(root: str | Path, path: str | Path) -> dict[str, Any]:
    root_path = Path(root).resolve()
    rel = _relative(root_path, path)
    if rel is None:
        raise ValueError("path is outside the repository")
    record = latest_records(root_path).get(rel)
    full = root_path / rel
    digest, size = _hash_file(full) if full.is_file() else (None, 0)
    current = {
        "path": rel,
        "exists": full.is_file(),
        "size": size,
        "hash": digest,
    }
    if record is not None:
        current["changed"] = bool(record.get("hash") and digest
                                  and record.get("hash") != digest)
        current["record"] = record
    else:
        current["changed"] = None
        current["record"] = None
    if rel.lower().endswith(".md"):
        content = ""
        if full.is_file() and size <= MAX_HASH_BYTES:
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
        current["route"] = markdown_route(rel, content, root=root_path)
    return current


def _created_paths(root: Path, *, paths: Sequence[str] | None = None,
                   limit: int | None = None) -> list[tuple[str, str | None]]:
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    wanted = [p for p in paths or [] if p]
    if wanted:
        args.extend(["--", *wanted])
    proc = _git(root, args)
    if proc.returncode != 0:
        raise FileLedgerError((proc.stderr or proc.stdout or "git status failed").strip()[:500])
    parts = proc.stdout.split("\0")
    created: list[tuple[str, str | None]] = []
    index = 0
    while index < len(parts):
        if limit is not None and len(created) >= max(0, int(limit)):
            break
        record = parts[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            continue
        status = record[:2]
        rel = record[3:]
        moved_from = None
        if status[0] in {"R", "C"} and index < len(parts):
            moved_from = parts[index]
            index += 1
        if status == "??" or status[0] == "A" or status[0] in {"R", "C"}:
            created.append((rel, moved_from))
    return created


def _route_content(full: Path, size: int) -> str:
    """A Markdown file's text for routing, bounded; "" when unreadable."""
    if not full.is_file():
        return ""
    try:
        if size <= 1024 * 1024:
            return full.read_text(encoding="utf-8", errors="replace")
        with full.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(64 * 1024)
    except OSError:
        return ""


def _cleanup_policy(classification: str) -> str:
    if classification in {"root", "plan", "result", "research", "docs",
                          "history"}:
        return "keep"
    if classification == "scratch":
        return "delete_after"
    return "ask"


def _record_item(root_path: Path, rel: str, *, digest: str | None, size: int,
                 session: str | None, harness: str | None, task: str | None,
                 tool: str | None, reason: str,
                 moved_from: str | None = None,
                 extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One ledger record for `rel`: metadata only, never file content."""
    route: dict[str, Any] = {}
    if rel.lower().endswith(".md"):
        route = markdown_route(rel, _route_content(root_path / rel, size),
                               root=root_path,
                               context={"session": session},
                               check_duplicates=False)
        classification = str(route.get("kind") or "note")
    else:
        classification = _classify(rel)
    cleanup_policy = _cleanup_policy(classification)
    item = {
        "schema": SCHEMA,
        "path": rel,
        "worktree": str(root_path),
        "hash": digest,
        "size": size,
        "created_at": _now(),
        "created_by": {
            "session": _label(session), "harness": _label(harness),
            "task": _label(task), "tool": _label(tool), "command": None,
        },
        "reason": _clean_reason(reason),
        "classification": classification,
        "status": "active",
        "references": [],
        "duplicate_of": route.get("duplicate_of"),
        "moved_from": moved_from,
        "cleanup": {"policy": cleanup_policy, "after": None},
    }
    if extra:
        item.update({k: v for k, v in extra.items() if k not in item})
    return item



def _record_paths(root: str | Path, paths: Iterable[str | Path], *,
                  session: str | None = None, harness: str | None = None,
                  task: str | None = None, tool: str | None = None,
                  reason: str = "unknown",
                  limit: int = MAX_RECORD_PATHS) -> list[dict[str, Any]]:
    """Record existing files among `paths`; skip duplicates (same path and
    hash), ignored trees and paths outside the root."""
    root_path = Path(root).resolve()
    latest = latest_records(root_path)
    recorded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(paths):
        if index >= max(0, int(limit)):
            break
        rel = _relative(root_path, raw)
        if rel is None or _skip(rel) or rel in seen:
            continue
        seen.add(rel)
        full = root_path / rel
        if not full.is_file():
            continue
        digest, size = _hash_file(full)
        previous = latest.get(rel)
        if (previous and previous.get("hash") == digest
                and previous.get("status") == "active"):
            continue
        item = _record_item(root_path, rel, digest=digest, size=size,
                            session=session, harness=harness, task=task,
                            tool=tool, reason=reason)
        recorded.append(append_record(root_path, item))
        latest[rel] = item
    return recorded


def record_file(root: str | Path, rel: str | Path, *, session: str | None = None,
                harness: str | None = None, task: str | None = None,
                tool: str | None = None, reason: str = "unknown",
                ) -> dict[str, Any] | None:
    """Record one file in the ledger (metadata only, never its content).

    `rel` is relative to `root` (an absolute path inside `root` works too).
    A file already recorded with the same path and hash (and still active)
    is not recorded again: the return value is then None. Ignored trees
    (`.git`, `.ozagia`, caches) and paths outside the root are skipped.
    """
    got = _record_paths(root, [rel], session=session, harness=harness,
                        task=task, tool=tool, reason=reason, limit=1)
    return got[0] if got else None


def record_paths(root: str | Path, paths: Iterable[str | Path], *,
                 session: str | None = None, harness: str | None = None,
                 task: str | None = None, tool: str | None = None,
                 reason: str = "unknown",
                 limit: int = MAX_RECORD_PATHS) -> list[dict[str, Any]]:
    """`record_file` for several paths, reading the ledger once.

    Returns only the records appended (deduplicated and skipped paths are
    absent)."""
    return _record_paths(root, paths, session=session, harness=harness,
                         task=task, tool=tool, reason=reason, limit=limit)


def scan_new_files(root: str | Path, *, session: str | None = None,
                   task: str | None = None, reason: str = "unknown",
                   tool: str = "scan", harness: str | None = None,
                   paths: Sequence[str] | None = None,
                   limit: int = MAX_SCAN_RECORDS) -> list[dict[str, Any]]:
    """Record currently untracked/added files. Does not modify the repository.

    With `paths`, only those paths are checked (one bounded `git status`
    pathspec run): this is how a built-in edit's `meta.changed_paths` are
    recorded without scanning the whole repository. At most `limit` files
    are recorded per call."""
    root_path = Path(root).resolve()
    rel_paths: list[str] = []
    if paths is not None:
        for p in paths:
            rel = _relative(root_path, p)
            if rel is not None and not _skip(rel):
                rel_paths.append(rel)
        if not rel_paths:
            return []
    latest = latest_records(root_path)
    recorded: list[dict[str, Any]] = []
    for rel, moved_from in _created_paths(root_path, paths=rel_paths or None,
                                          limit=MAX_SCAN_CANDIDATES):
        if len(recorded) >= max(0, int(limit)):
            break
        if _skip(rel):
            continue
        full = root_path / rel
        digest, size = _hash_file(full) if full.is_file() else (None, 0)
        previous = latest.get(rel)
        if previous and previous.get("hash") == digest and previous.get("status") == "active":
            continue
        item = _record_item(root_path, rel, digest=digest, size=size,
                            session=session, harness=harness, task=task,
                            tool=tool, reason=reason, moved_from=moved_from)
        recorded.append(append_record(root_path, item))
        latest[rel] = item
    return recorded


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write one JSON document atomically (state files only)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise FileLedgerError(f"cannot write {path.name}: {exc}") from exc


# -- session manifests (`start` snapshots, `end` deltas) -------------------- #
def _session_key(session: str, harness: str | None) -> str:
    seed = f"{harness or ''}\0{session}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


def session_manifest_path(root: str | Path, session: str,
                          *, harness: str | None = None) -> Path:
    """The baseline path for one (harness, session): state-dir JSON."""
    if not isinstance(session, str) or not session:
        raise ValueError("session must be a non-empty string")
    return state_path(root, LEDGER_DIR, "sessions",
                      f"{_session_key(session, harness)}.json")


def _manifest_hash(full: Path, remaining: int) -> dict[str, Any]:
    """Hash one manifest file within the total byte budget.

    Returns size/hash/hash_state. Large files and files past the budget are
    noted (`large`, `bounded`), never read. Symlinks and non-files get no hash.
    """
    try:
        info = full.lstat()
    except OSError:
        return {"size": 0, "hash": None, "hash_state": "missing"}
    if full.is_symlink():
        return {"size": info.st_size, "hash": None, "hash_state": "symlink"}
    if not full.is_file():
        return {"size": info.st_size, "hash": None, "hash_state": "not-file"}
    size = info.st_size
    if size > MAX_HASH_BYTES:
        return {"size": size, "hash": None, "hash_state": "large"}
    if size > remaining:
        return {"size": size, "hash": None, "hash_state": "bounded"}
    digest, size = _hash_file(full)
    if digest is None:
        return {"size": size, "hash": None, "hash_state": "unreadable"}
    return {"size": size, "hash": digest, "hash_state": "hashed"}


def _manifest_files(root: Path) -> tuple[dict[str, dict[str, Any]], bool]:
    """A bounded worktree manifest: tracked and untracked paths, no contents.

    Caches, `.git` and the state directory are skipped. Untracked and dirty
    paths are hashed first so the byte budget favours what a session is most
    likely to change. The result is sorted by path.
    """
    listing = _git(root, ["ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"])
    if listing.returncode != 0:
        raise FileLedgerError(
            (listing.stderr or listing.stdout or "git ls-files failed").strip()[:500])
    status_proc = _git(root, ["status", "--porcelain=v1", "-z",
                              "--untracked-files=all"])
    if status_proc.returncode != 0:
        raise FileLedgerError(
            (status_proc.stderr or status_proc.stdout or "git status failed").strip()[:500])
    statuses: dict[str, str] = {}
    parts = status_proc.stdout.split("\0")
    index = 0
    while index < len(parts):
        record = parts[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            continue
        xy, rel = record[:2], record[3:]
        if xy[0] in {"R", "C"} and index < len(parts):
            index += 1        # the original path
        statuses[rel] = xy

    paths = list(dict.fromkeys(
        p for p in listing.stdout.split("\0") if p and not _skip(p)))

    def priority(rel: str) -> int:
        xy = statuses.get(rel, "")
        if xy == "??":
            return 0
        if xy.strip():
            return 1
        return 2

    paths.sort(key=lambda rel: (priority(rel), rel))
    truncated = len(paths) > MAX_MANIFEST_FILES
    paths = paths[:MAX_MANIFEST_FILES]
    remaining = MAX_MANIFEST_HASH_BYTES
    files: dict[str, dict[str, Any]] = {}
    for rel in paths:
        full = root / rel
        xy = statuses.get(rel, "")
        if xy == "??":
            status = "untracked"
        elif "D" in xy:
            status = "deleted"
        elif xy.startswith(("R", "C")):
            status = "renamed"
        elif xy.startswith("A"):
            status = "added"
        elif xy.strip():
            status = "modified"
        else:
            status = "tracked"
        entry = _manifest_hash(full, remaining)
        if status == "deleted" and entry["hash_state"] == "missing":
            entry["status"] = "deleted"
        elif entry["hash_state"] == "missing":
            entry["status"] = "deleted" if status == "tracked" else status
        else:
            entry["status"] = status
        if entry["hash_state"] == "hashed":
            remaining = max(0, remaining - int(entry.get("size") or 0))
        files[rel] = entry
    return {rel: files[rel] for rel in sorted(files)}, truncated


def _prune_session_manifests(root: Path, *, keep: Path | None = None) -> None:
    directory = state_path(root, LEDGER_DIR, "sessions")
    try:
        paths = [p for p in directory.glob("*.json") if p.is_file()]
    except OSError:
        return
    if len(paths) <= MAX_SESSION_MANIFESTS:
        return
    try:
        paths.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
    except OSError:
        return
    keep = keep.resolve() if keep is not None else None
    for path in paths[MAX_SESSION_MANIFESTS:]:
        if keep is not None and path.resolve() == keep:
            continue
        try:
            path.unlink()
        except OSError:
            pass


def session_snapshot(root: str | Path, session: str, *,
                     harness: str | None = None) -> dict[str, Any]:
    """Write the session-start baseline manifest and return it.

    The manifest stores relative path, size, hash (or the reason it has none:
    `large`, `bounded`, `symlink`, `missing`), and tracked/untracked status.
    It never stores file contents.
    """
    root_path = Path(root).resolve()
    files, truncated = _manifest_files(root_path)
    manifest = {
        "schema": SCHEMA,
        "kind": "session_baseline",
        "session": session,
        "harness": _label(harness),
        "root": str(root_path),
        "created_at": _now(),
        "baseline": "manifest",
        "truncated": truncated,
        "count": len(files),
        "files": files,
    }
    _write_json(session_manifest_path(root_path, session, harness=harness),
                manifest)
    _prune_session_manifests(
        root_path, keep=session_manifest_path(root_path, session,
                                              harness=harness))
    return manifest


def _load_session_manifest(root: str | Path, session: str,
                           harness: str | None) -> dict[str, Any] | None:
    path = session_manifest_path(root, session, harness=harness)
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(item, dict) or item.get("schema") != SCHEMA:
        return None
    if not isinstance(item.get("files"), dict):
        return None
    return item


def _append_session_record(root_path: Path, latest: dict[str, dict[str, Any]],
                           rel: str, entry: Mapping[str, Any], *,
                           session: str | None, harness: str | None,
                           tool: str, reason: str, change: str,
                           moved_from: str | None = None,
                           extra: Mapping[str, Any] | None = None,
                           ) -> dict[str, Any] | None:
    """Append one active session-delta ledger row unless it is unchanged."""
    digest = entry.get("hash")
    previous = latest.get(rel)
    if (previous and previous.get("hash") == digest
            and previous.get("status") == "active"):
        return None
    fields: dict[str, Any] = {"change": change, "attribution": True}
    if extra:
        fields.update(extra)
    item = _record_item(
        root_path, rel, digest=digest, size=int(entry.get("size") or 0),
        session=session, harness=harness, task=None, tool=tool,
        reason=reason, moved_from=moved_from, extra=fields)
    latest[rel] = append_record(root_path, item)
    return latest[rel]


def _tombstone(root_path: Path, latest: dict[str, dict[str, Any]], rel: str,
               entry: Mapping[str, Any], *, status: str,
               session: str | None, harness: str | None, tool: str,
               previous: Mapping[str, Any] | None = None,
               extra: Mapping[str, Any] | None = None,
               ) -> dict[str, Any] | None:
    """A metadata-only row for a moved or deleted file (no contents)."""
    digest = entry.get("hash")
    previous = previous or latest.get(rel)
    if (previous and previous.get("status") == status
            and previous.get("hash") == digest):
        return None
    classification = str((previous or {}).get("classification")
                         or _classify(rel))
    item: dict[str, Any] = {
        "schema": SCHEMA,
        "path": rel,
        "worktree": str(root_path),
        "hash": digest,
        "size": int(entry.get("size") or 0),
        "created_at": _now(),
        "created_by": dict((previous or {}).get("created_by") or {
            "session": None, "harness": None, "task": None, "tool": None,
            "command": None}),
        "removed_by": {"session": _label(session), "harness": _label(harness),
                       "task": None, "tool": _label(tool), "command": None},
        "reason": str((previous or {}).get("reason") or "unknown"),
        "classification": classification,
        "status": status,
        "references": list((previous or {}).get("references") or []),
        "duplicate_of": (previous or {}).get("duplicate_of"),
        "moved_from": (previous or {}).get("moved_from"),
        "cleanup": dict((previous or {}).get("cleanup") or {
            "policy": _cleanup_policy(classification), "after": None}),
        "change": status,
        "attribution": bool(previous),
    }
    if extra:
        item.update(extra)
    latest[rel] = append_record(root_path, item)
    return latest[rel]


def _entry_view(rel: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    return {"path": rel, **{k: entry.get(k) for k in
                            ("size", "hash", "hash_state", "status")}}


def session_delta(root: str | Path, session: str, *, harness: str | None = None,
                  record: bool = True, reason: str = "unknown",
                  tool: str = "session_event",
                  limit: int = MAX_SESSION_RECORDS) -> dict[str, Any]:
    """Compare the current worktree with the session-start manifest.

    Only files that are new since the baseline get the session as creator; a
    pre-existing untracked file that did not change is not recorded at all.
    Changed pre-existing untracked files and moves are recorded as unattributed
    metadata. Moves are paired by content hash; deletes keep a metadata-only
    tombstone (for files the ledger could attribute or that were untracked at
    baseline).

    Without a baseline (old daemon, crash, pruned state) the current
    untracked/added files are recorded as a fallback with
    `baseline: "missing"`, `attribution: false` and no session creator.
    Nothing is recorded twice when the same end event is replayed.
    """
    root_path = Path(root).resolve()
    current, truncated = _manifest_files(root_path)
    baseline = _load_session_manifest(root_path, session, harness)
    latest = latest_records(root_path)
    remaining = max(0, int(limit))

    if baseline is None:
        recorded: list[dict[str, Any]] = []
        new: list[dict[str, Any]] = []
        for rel in sorted(current):
            entry = current[rel]
            if entry.get("status") not in {"untracked", "added", "renamed"}:
                continue
            new.append(_entry_view(rel, entry))
            if not record or remaining <= 0:
                continue
            row = _append_session_record(
                root_path, latest, rel, entry, session=None, harness=None,
                tool=tool, reason=_clean_reason(reason),
                change="new",
                extra={"baseline": "missing", "attribution": False})
            if row is not None:
                recorded.append(row)
                remaining -= 1
        return {
            "schema": SCHEMA, "session": session, "harness": _label(harness),
            "baseline": "missing", "attribution": False, "replayed": False,
            "truncated": truncated, "new": new, "changed": [], "moved": [],
            "deleted": [], "recorded": recorded,
            "counts": {"new": len(new), "changed": 0, "moved": 0,
                       "deleted": 0, "recorded": len(recorded)},
        }

    old_files = {str(k): v for k, v in baseline["files"].items()
                 if isinstance(v, dict)}
    current_paths = set(current)
    old_paths = set(old_files)
    new_paths = sorted(current_paths - old_paths)
    gone_paths = sorted(old_paths - current_paths)
    changed_paths = [
        rel for rel in sorted(current_paths & old_paths)
        if (current[rel].get("hash") != old_files[rel].get("hash")
            or int(current[rel].get("size") or 0)
            != int(old_files[rel].get("size") or 0)
            or current[rel].get("status") != old_files[rel].get("status"))
    ]

    # Pair a new path with a gone path of the same content and size: a move.
    moved: list[dict[str, Any]] = []
    gone_by_hash: dict[tuple[Any, Any], list[str]] = {}
    for rel in gone_paths:
        entry = old_files[rel]
        known = (entry.get("status") == "untracked"
                 or (latest.get(rel) or {}).get("status") == "active")
        if known and entry.get("hash"):
            gone_by_hash.setdefault(
                (entry.get("hash"), int(entry.get("size") or 0)),
                []).append(rel)
    moved_from: dict[str, str] = {}
    for rel in list(new_paths):
        key = (current[rel].get("hash"), int(current[rel].get("size") or 0))
        bucket = gone_by_hash.get(key)
        if not bucket:
            continue
        source = bucket.pop(0)
        moved.append({"from": source, "to": rel,
                      "size": int(current[rel].get("size") or 0),
                      "hash": current[rel].get("hash")})
        moved_from[rel] = source
        new_paths.remove(rel)
        gone_paths.remove(source)

    replayed = bool(baseline.get("closed_at"))
    may_record = bool(record) and not replayed
    recorded: list[dict[str, Any]] = []
    attributed_new: list[dict[str, Any]] = []
    for rel in new_paths:
        entry = current[rel]
        attributed_new.append(_entry_view(rel, entry))
        if not may_record or remaining <= 0:
            continue
        row = _append_session_record(
            root_path, latest, rel, entry, session=session, harness=harness,
            tool=tool, reason=reason, change="new",
            extra={"attribution": True})
        if row is not None:
            recorded.append(row)
            remaining -= 1
    for destination, source in moved_from.items():
        if not may_record or remaining <= 0:
            continue
        row = _append_session_record(
            root_path, latest, destination, current[destination],
            session=None, harness=None, tool=tool, reason=reason,
            change="moved", moved_from=source,
            extra={"attribution": False})
        if row is not None:
            recorded.append(row)
            remaining -= 1
    changed: list[dict[str, Any]] = []
    for rel in changed_paths:
        view = _entry_view(rel, current[rel])
        view["previous"] = dict(old_files[rel])
        changed.append(view)
        # The ledger tracks created files: a changed untracked file is a
        # session change, but a modified tracked source file is not a creation.
        # A file that was already untracked at the baseline is not attributed
        # to the session, even when it changed.
        if (not may_record or remaining <= 0
                or current[rel].get("status") != "untracked"
                or old_files[rel].get("status") != "untracked"):
            continue
        row = _append_session_record(
            root_path, latest, rel, current[rel], session=None, harness=None,
            tool=tool, reason=reason, change="modified",
            extra={"attribution": False})
        if row is not None:
            recorded.append(row)
            remaining -= 1

    deleted: list[dict[str, Any]] = []
    for rel in gone_paths:
        entry = old_files[rel]
        known = (entry.get("status") == "untracked"
                 or (latest.get(rel) or {}).get("status") == "active")
        if not known:
            continue
        deleted.append(_entry_view(rel, entry))
        if not may_record or remaining <= 0:
            continue
        row = _tombstone(root_path, latest, rel, entry, status="deleted",
                         session=session, harness=harness, tool=tool,
                         previous=latest.get(rel),
                         extra={"deleted_at": _now()})
        if row is not None:
            recorded.append(row)
            remaining -= 1
    for move in moved:
        source = str(move["from"])
        if not may_record or remaining <= 0:
            continue
        row = _tombstone(root_path, latest, source, old_files[source],
                         status="moved", session=session, harness=harness,
                         tool=tool, previous=latest.get(source),
                         extra={"moved_to": move["to"]})
        if row is not None:
            recorded.append(row)
            remaining -= 1

    if may_record:
        baseline["closed_at"] = _now()
        baseline["last_delta"] = {
            "new": len(attributed_new), "changed": len(changed),
            "moved": len(moved), "deleted": len(deleted),
            "recorded": len(recorded)}
        try:
            _write_json(session_manifest_path(root_path, session,
                                              harness=harness), baseline)
        except FileLedgerError:
            pass
    return {
        "schema": SCHEMA, "session": session, "harness": _label(harness),
        "baseline": "manifest", "attribution": True, "replayed": replayed,
        "truncated": truncated, "new": attributed_new, "changed": changed,
        "moved": moved, "deleted": deleted, "recorded": recorded,
        "counts": {"new": len(attributed_new), "changed": len(changed),
                   "moved": len(moved), "deleted": len(deleted),
                   "recorded": len(recorded)},
    }



def orphans(root: str | Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in latest_records(root).values():
        if item.get("status") != "active":
            continue
        if item.get("references"):
            continue
        if item.get("classification") in {"note", "scratch", "other"}:
            out.append(item)
        elif item.get("reason") == "unknown":
            out.append(item)
    return sorted(out, key=lambda item: str(item.get("path")))


def cleanup_candidates(root: str | Path, *, older_than_days: int = 30,
                       classifications: Sequence[str] | None = None,
                       include_referenced: bool = False
                       ) -> list[dict[str, Any]]:
    selected = set(classifications or ("note", "scratch", "other"))
    out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for item in latest_records(root).values():
        if item.get("status") != "active":
            continue
        if item.get("references") and not include_referenced:
            continue
        if item.get("classification") not in selected:
            continue
        created = item.get("created_at")
        age = 0
        if isinstance(created, str):
            try:
                age = max(0, int((now - datetime.fromisoformat(created)).days))
            except ValueError:
                age = 0
        if older_than_days and age < older_than_days:
            continue
        policy = (item.get("cleanup") or {}).get("policy")
        if item.get("references"):
            recommended = "refuse"
        else:
            recommended = ("trash" if policy == "delete_after" or
                           item.get("classification") == "scratch" else "ask")
        out.append({**item, "age_days": age, "recommended": recommended})
    return sorted(out, key=lambda item: (-int(item.get("size") or 0),
                                         str(item.get("path"))))


# -- cleanup plans, trash and restore --------------------------------------- #
def cleanup_plan(root: str | Path, *, older_than_days: int = 30,
                 classifications: Sequence[str] | None = None,
                 ttl_s: int = CLEANUP_PLAN_TTL_S,
                 include_referenced: bool = False) -> dict[str, Any]:
    """A dry-run cleanup preview and its one-time confirmation token.

    The plan stores metadata only: path, size, hash, classification, reason
    and recommendation. `cleanup_apply` accepts the token once before it
    expires; by default it applies the planned files. Explicitly named
    candidates outside the plan are checked too and need `force=True`.
    Referenced files are excluded unless `include_referenced` is set; they
    still need `force=True` to move.
    """
    root_path = Path(root).resolve()
    candidates = cleanup_candidates(
        root_path, older_than_days=older_than_days,
        classifications=classifications,
        include_referenced=include_referenced)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=max(0, int(ttl_s)))
    token = "c" + secrets.token_urlsafe(18)
    planned = [{
        "path": item.get("path"),
        "size": int(item.get("size") or 0),
        "hash": item.get("hash"),
        "classification": item.get("classification"),
        "reason": item.get("reason"),
        "references": list(item.get("references") or []),
        "recommended": item.get("recommended"),
        "age_days": item.get("age_days"),
    } for item in candidates if isinstance(item.get("path"), str)]
    plan = {
        "schema": SCHEMA, "kind": "cleanup_plan", "token": token,
        "root": str(root_path), "created_at": now.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "ttl_s": max(0, int(ttl_s)), "used_at": None,
        "candidates": planned,
    }
    _write_json(_cleanup_dir(root_path) / f"{token}.json", plan)
    _prune_cleanup_plans(root_path)
    return {**plan, "dry_run": True, "count": len(planned),
            "confirmation": token}


def _cleanup_dir(root: Path) -> Path:
    return state_path(root, LEDGER_DIR, "cleanup")


def _trash_root(root: Path) -> Path:
    return state_path(root, "trash")


def _trash_audit_path(root: str | Path) -> Path:
    return _trash_root(Path(root).resolve()) / TRASH_AUDIT_FILE


def trash_retention_days(root: str | Path) -> int:
    """The configured trash retention in days (`files.retention_days`).

    The typed settings registry is the source of truth (default 30). A
    malformed configuration raises instead of silently using the default: a
    purge decision should never run on a value nobody can read.
    """
    try:
        from ozagia.core.settings import get as settings_get
    except ImportError:  # pragma: no cover - settings is part of core
        return DEFAULT_TRASH_RETENTION_DAYS
    value = settings_get(root, "files.retention_days").get("value")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FileLedgerError(
            "files.retention_days must be a non-negative integer, "
            f"got {value!r}")
    return value


def _purge_older_than(root: str | Path, older_than_days: int | None) -> int:
    value = (trash_retention_days(root) if older_than_days is None
             else older_than_days)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("older_than_days must be a non-negative integer")
    return value


def _age_days(value: Any, *, now: datetime | None = None) -> int | None:
    """Whole days since an ISO timestamp, or None when it cannot be read."""
    if not isinstance(value, str) or not value:
        return None
    try:
        created = datetime.fromisoformat(value)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0, int((current - created).total_seconds() // 86400))


def _trash_entry_state(entry: Mapping[str, Any]) -> str:
    if entry.get("purged_at"):
        return "purged"
    if entry.get("restored_at") or entry.get("decision") == "restore":
        return "restored"
    return "in-trash"


def _valid_trash_id(value: Any) -> bool:
    return (isinstance(value, str) and value not in {"", ".", "..", ".git"}
            and len(value) <= 128 and "\x00" not in value and "/" not in value
            and "\\" not in value)


def _load_trash_manifest(root: Path, run_dir: Path
                         ) -> tuple[dict[str, Any] | None, str | None]:
    """Read and validate one trash run's metadata-only manifest."""
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_symlink():
        return None, "trash manifest is a symlink"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError:
        return None, "trash manifest is missing"
    except json.JSONDecodeError:
        return None, "trash manifest is corrupt"
    if not isinstance(manifest, dict):
        return None, "trash manifest is not an object"
    if manifest.get("kind") != "trash_manifest":
        return None, "trash manifest has an unknown kind"
    if manifest.get("trash_id") != run_dir.name:
        return None, "trash manifest id does not match its directory"
    if manifest.get("root") != str(root):
        return None, "trash manifest belongs to another repository"
    if not isinstance(manifest.get("entries"), list):
        return None, "trash manifest has no entries"
    return manifest, None


def _trash_entry_problem(root: Path, run_dir: Path, entry: Any) -> str | None:
    """Why this manifest entry must not be purged, or None when it is safe.

    Only a regular file below this run's directory can be deleted. The
    repository path in the manifest is metadata for the audit, never a
    deletion target.
    """
    if not isinstance(entry, Mapping):
        return "entry is not an object"
    rel = entry.get("path")
    if not isinstance(rel, str) or not rel:
        return "entry has no repository path"
    if _path_parts_problem(rel):
        return "entry repository path is outside the repository"
    trash_path = entry.get("trash_path")
    if not isinstance(trash_path, str) or not trash_path:
        return "entry has no trash path"
    target = _resolve_display(root, trash_path)
    if _unsafe_under(run_dir, target):
        return "trash path is unsafe"
    if target == run_dir:
        return "trash path is the run directory"
    if target.name == "manifest.json" and target.parent == run_dir:
        return "trash path is the trash manifest"
    try:
        resolved = target.resolve(strict=False)
        base = run_dir.resolve()
    except OSError:
        return "trash path is unsafe"
    if base not in resolved.parents:
        return "trash path is unsafe"
    if target.is_symlink():
        return "trash path is a symlink"
    if not target.is_file():
        return "file is missing from trash"
    size = entry.get("size")
    try:
        actual = target.stat().st_size
    except OSError:
        return "file is missing from trash"
    if (isinstance(size, int) and not isinstance(size, bool)
            and size != actual):
        return "file size does not match the manifest"
    return None


def _trash_runs(root: Path) -> tuple[Path, list[Path]]:
    """The trash root and its direct, non-symlink run directories."""
    trash_root = _trash_root(root)
    if trash_root.is_symlink():
        raise FileLedgerError("trash directory is a symlink; refusing to purge")
    try:
        relative = trash_root.relative_to(root)
    except ValueError:
        relative = None
    if relative is not None:
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise FileLedgerError(
                    "trash directory contains a symlink; refusing to purge")
    try:
        directories = [p for p in trash_root.iterdir()
                       if p.is_dir() and not p.is_symlink()]
    except OSError:
        return trash_root, []
    try:
        base = trash_root.resolve()
    except OSError:
        return trash_root, []
    directories = [p for p in directories
                   if _valid_trash_id(p.name) and p.resolve().parent == base]
    directories.sort(key=lambda p: p.name, reverse=True)
    return trash_root, directories


def _prune_cleanup_plans(root: Path) -> None:
    directory = _cleanup_dir(root)
    try:
        paths = [p for p in directory.glob("*.json") if p.is_file()]
        markers = [p for p in directory.glob("*.used") if p.is_file()]
    except OSError:
        return
    for group in (paths, markers):
        if len(group) <= MAX_CLEANUP_PLANS:
            continue
        try:
            group.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        except OSError:
            continue
        for path in group[MAX_CLEANUP_PLANS:]:
            try:
                path.unlink()
            except OSError:
                pass


def _valid_token(value: Any) -> bool:
    return (isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value) is not None)


def _load_cleanup_plan(root: str | Path, confirmation: str) -> dict[str, Any]:
    if not _valid_token(confirmation):
        raise FileLedgerError("invalid cleanup confirmation token")
    root_path = Path(root).resolve()
    path = _cleanup_dir(root_path) / f"{confirmation}.json"
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileLedgerError("unknown cleanup confirmation token") from exc
    except json.JSONDecodeError as exc:
        raise FileLedgerError("cleanup confirmation token is corrupt") from exc
    if (not isinstance(plan, dict) or plan.get("kind") != "cleanup_plan"
            or plan.get("token") != confirmation):
        raise FileLedgerError("cleanup confirmation token is not a plan")
    if plan.get("used_at"):
        raise FileLedgerError("cleanup confirmation token was already used")
    if plan.get("root") != str(root_path):
        raise FileLedgerError("cleanup plan belongs to another repository")
    expires = plan.get("expires_at")
    try:
        expired = (not isinstance(expires, str)
                   or datetime.now(timezone.utc) >= datetime.fromisoformat(expires))
    except ValueError:
        expired = True
    if expired:
        raise FileLedgerError("cleanup confirmation token has expired")
    if not isinstance(plan.get("candidates"), list):
        raise FileLedgerError("cleanup confirmation token has no candidates")
    return plan


def _consume_cleanup_plan(root: Path, plan: Mapping[str, Any], *,
                          session: str | None, harness: str | None) -> None:
    marker = _cleanup_dir(root) / f"{plan['token']}.used"
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                             0o600)
    except FileExistsError as exc:
        raise FileLedgerError(
            "cleanup confirmation token was already used") from exc
    except OSError as exc:
        raise FileLedgerError(f"cannot consume cleanup token: {exc}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(_now() + "\n")
    item = dict(plan)
    item["used_at"] = _now()
    item["used_by"] = {"session": _label(session), "harness": _label(harness)}
    _write_json(_cleanup_dir(root) / f"{plan['token']}.json", item)


def _path_parts_problem(rel: str, *, allow_state: bool = False) -> str | None:
    if not rel or rel.startswith("/") or "\x00" in rel:
        return "path is outside the repository"
    parts = Path(rel).parts
    if not parts or ".." in parts or "." in parts:
        return "path is outside the repository"
    if parts[0] == ".git":
        return "refusing to move .git"
    if parts[0] == ".ozagia" and not allow_state:
        return "refusing to move the state directory"
    return None


def _source_problem(root: Path, rel: str) -> str | None:
    problem = _path_parts_problem(rel)
    if problem:
        return problem
    full = root / rel
    current = root
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            return "symlink path refused"
    try:
        resolved = full.resolve(strict=True)
    except OSError:
        return "file is missing"
    if _relative(root, resolved) != rel:
        return "path resolves outside the repository"
    if not full.is_file():
        return "not a regular file"
    return None


def _trash_display(root: Path, full: Path) -> str:
    try:
        return full.relative_to(root).as_posix()
    except ValueError:
        return str(full)


def _resolve_display(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _trash_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _unsafe_under(base: Path, target: Path) -> bool:
    """True when `target` is outside `base` or an existing component is a
    symlink (a trash destination must not escape through one)."""
    try:
        relative = target.relative_to(base)
    except ValueError:
        return True
    if any(part in {"", ".", ".."} for part in relative.parts):
        return True
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def cleanup_apply(root: str | Path, candidates: Sequence[str | Mapping[str, Any]]
                  | None = None, confirmation: str = "", *,
                  force: bool = False, session: str | None = None,
                  harness: str | None = None, task: str | None = None,
                  reason: str = "cleanup") -> dict[str, Any]:
    """Apply a cleanup plan by moving planned files to the trash.

    Nothing is deleted. Referenced files, files with `reason: unknown` and
    files with no ledger record are refused unless `force=True`; a forced move
    is recorded as such. A candidate not previewed in the plan is refused
    unless `force=True` and is then recorded as forced. A token is one-time:
    it is consumed even when some candidates are refused. Paths outside the
    root, symlinks, `.git`/`.ozagia` sources, changed files and existing trash
    destinations are always refused.
    """
    root_path = Path(root).resolve()
    if isinstance(candidates, Mapping):
        candidates = candidates.get("candidates")
    if isinstance(confirmation, Mapping):
        confirmation = str(confirmation.get("confirmation")
                           or confirmation.get("token") or "")
    plan = _load_cleanup_plan(root_path, str(confirmation))
    planned = {str(item.get("path")): item for item in plan["candidates"]
               if isinstance(item, dict) and isinstance(item.get("path"), str)}
    if candidates is None:
        wanted = list(planned)
        unplanned: set[str] = set()
    else:
        if isinstance(candidates, (str, Path)):
            candidates = [candidates]
        wanted = []
        unplanned = set()
        for value in candidates:
            raw = value.get("path") if isinstance(value, Mapping) else value
            rel = _relative(root_path, raw) if isinstance(raw, (str, Path)) else None
            if rel is None:
                rel = str(raw) if isinstance(raw, str) else ""
            if rel not in planned:
                unplanned.add(rel)
            if rel not in wanted:
                wanted.append(rel)
    _consume_cleanup_plan(root_path, plan, session=session, harness=harness)
    latest = latest_records(root_path)
    run_id = _trash_run_id()
    trash_root = _trash_root(root_path)
    run_dir = trash_root / run_id
    applied: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []

    def refuse(path: str, why: str) -> None:
        refused.append({"path": path, "reason": why})

    for rel in wanted:
        planned_item = planned.get(rel, {})
        problem = _source_problem(root_path, rel)
        if problem:
            refuse(rel, problem)
            continue
        record = latest.get(rel)
        forced_reasons: list[str] = []
        if record is None:
            if not force:
                refuse(rel, "no ledger record; use --force to record a forced move")
                continue
            forced_reasons.append("no ledger record")
            record = {
                "schema": SCHEMA, "path": rel, "worktree": str(root_path),
                "hash": planned_item.get("hash"),
                "size": int(planned_item.get("size") or 0),
                "created_at": _now(),
                "created_by": {"session": None, "harness": None, "task": None,
                               "tool": None, "command": None},
                "reason": _clean_reason(planned_item.get("reason")),
                "classification": planned_item.get("classification") or "other",
                "status": "active", "references": [],
                "duplicate_of": None, "moved_from": None,
                "cleanup": {"policy": "ask", "after": None},
            }
        if record.get("status") != "active":
            if not force:
                refuse(rel, "ledger record is not active")
                continue
            forced_reasons.append("inactive ledger record")
        references = [str(r) for r in (record.get("references") or [])]
        coverage = record.get("reason") in (None, "", "unknown") or (
            record.get("classification") == "other")
        if references and not force:
            refuse(rel, "referenced file; use --force to record a forced move")
            continue
        if references:
            forced_reasons.append("referenced")
        if coverage and not force:
            refuse(rel, "unknown origin; use --force to record a forced move")
            continue
        if coverage:
            forced_reasons.append("unknown origin")
        if rel in unplanned:
            if not force:
                refuse(rel, "not in the cleanup plan")
                continue
            forced_reasons.append("not in the cleanup plan")
        digest, size = _hash_file(root_path / rel)
        expected = planned_item.get("hash") or record.get("hash")
        if expected and digest != expected:
            if not force:
                refuse(rel, "file changed since the plan")
                continue
            forced_reasons.append("changed since plan")
        destination = run_dir / rel
        if destination.is_symlink() or destination.exists():
            refuse(rel, "trash destination already exists")
            continue
        if _unsafe_under(trash_root, destination):
            refuse(rel, "trash destination is unsafe")
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if _unsafe_under(trash_root, destination.parent):
                refuse(rel, "trash destination is unsafe")
                continue
            shutil.move(str(root_path / rel), str(destination))
        except OSError as exc:
            refuse(rel, f"move failed: {exc}")
            continue
        trash_path = _trash_display(root_path, destination)
        decision = "forced" if force and forced_reasons else "trashed"
        item = {k: v for k, v in record.items()
                if k not in ("recorded_at", "restored_from", "restored_at",
                             "restored_by")}
        item.update({
            "schema": SCHEMA, "path": rel, "status": "trashed",
            "source_path": rel, "trash_path": trash_path,
            "trashed_at": _now(),
            "decision": decision, "change": "trashed",
            "trashed_by": {
                "session": _label(session), "harness": _label(harness),
                "task": _label(task), "tool": "cleanup",
                "reason": _clean_reason(reason),
            },
        })
        item["cleanup"] = {
            **(record.get("cleanup") or {}),
            "decision": decision, "trashed_at": item["trashed_at"],
        }
        try:
            append_record(root_path, item)
        except FileLedgerError:
            try:
                shutil.move(str(destination), str(root_path / rel))
            except OSError:
                pass
            raise
        latest[rel] = item
        applied.append({
            "path": rel, "trash_path": trash_path, "size": size,
            "hash": digest, "decision": decision,
            "classification": record.get("classification"),
            "reason": record.get("reason"),
            "references": references,
            "forced": decision == "forced",
        })

    if applied:
        manifest = {
            "schema": SCHEMA, "kind": "trash_manifest", "trash_id": run_id,
            "root": str(root_path), "created_at": _now(),
            "entries": [{
                "path": item["path"], "source_path": item["path"],
                "trash_path": item["trash_path"],
                "size": item["size"], "hash": item["hash"],
                "classification": item["classification"],
                "reason": item["reason"], "references": item["references"],
                "forced": item["forced"], "decision": item["decision"],
            } for item in applied],
        }
        _write_json(run_dir / "manifest.json", manifest)
    return {
        "schema": SCHEMA, "dry_run": False, "token": confirmation,
        "trash_id": run_id if applied else None,
        "trash_dir": (_trash_display(root_path, run_dir) if applied else None),
        "applied": applied, "moved": applied,
        "trashed": len(applied), "refused": refused,
        "counts": {"applied": len(applied), "refused": len(refused)},
        "ok": not refused,
    }


def list_trash(root: str | Path, *, limit: int = MAX_TRASH_RUNS,
               retention_days: int | None = None) -> dict[str, Any]:
    """List trash runs and their entries (metadata only, newest first).

    Each run reports its age, size, retention state and whether a purge has
    anything to do. Runs whose manifest is missing or unreadable are reported
    as `unknown` and are never purge candidates.
    """
    root_path = Path(root).resolve()
    retention = (_purge_older_than(root_path, retention_days)
                 if retention_days is not None
                 else trash_retention_days(root_path))
    now = datetime.now(timezone.utc)
    trash_root, directories = _trash_runs(root_path)
    runs: list[dict[str, Any]] = []
    for directory in directories[:max(0, int(limit))]:
        manifest, problem = _load_trash_manifest(root_path, directory)
        if manifest is None:
            runs.append({
                "trash_id": directory.name,
                "id": directory.name,
                "trash_dir": _trash_display(root_path, directory),
                "created_at": None,
                "count": 0,
                "restored": 0,
                "purged": 0,
                "bytes": 0,
                "remaining_bytes": 0,
                "age_days": None,
                "retention_days": retention,
                "retention_state": "unknown",
                "retention": "unknown",
                "expired": False,
                "purgeable": False,
                "manifest": False,
                "problem": problem,
                "entries": [],
            })
            continue
        entries = [e for e in manifest["entries"] if isinstance(e, dict)]
        age = _age_days(manifest.get("created_at"), now=now)
        in_trash = [entry for entry in entries
                    if _trash_entry_state(entry) == "in-trash"]
        if age is None:
            state = "unknown"
        elif age >= retention:
            state = "expired"
        else:
            state = "retained"
        purgeable = bool(in_trash) and state == "expired" and any(
            _trash_entry_problem(root_path, directory, entry) is None
            for entry in in_trash)
        runs.append({
            "trash_id": directory.name,
            "id": directory.name,
            "trash_dir": _trash_display(root_path, directory),
            "created_at": manifest.get("created_at"),
            "count": len(entries),
            "restored": sum(1 for e in entries if e.get("restored_at")),
            "purged": sum(1 for e in entries if e.get("purged_at")),
            "bytes": sum(int(e.get("size") or 0) for e in entries),
            "remaining_bytes": sum(int(e.get("size") or 0)
                                   for e in in_trash),
            "age_days": age,
            "retention_days": retention,
            "retention_state": state,
            "retention": state,
            "expired": state == "expired",
            "purgeable": purgeable,
            "manifest": True,
            "problem": None,
            "entries": entries,
        })
    return {"schema": SCHEMA, "trash": runs, "runs": runs,
            "count": len(runs), "retention_days": retention,
            "trash_dir": _trash_display(root_path, trash_root)}


def _purge_dir(root: Path) -> Path:
    return state_path(root, LEDGER_DIR, "purge")


def _prune_purge_plans(root: Path) -> None:
    directory = _purge_dir(root)
    try:
        paths = [p for p in directory.glob("*.json") if p.is_file()]
        markers = [p for p in directory.glob("*.used") if p.is_file()]
    except OSError:
        return
    for group in (paths, markers):
        if len(group) <= MAX_TRASH_PURGE_PLANS:
            continue
        try:
            group.sort(key=lambda p: p.stat().st_mtime_ns, reverse=True)
        except OSError:
            continue
        for path in group[MAX_TRASH_PURGE_PLANS:]:
            try:
                path.unlink()
            except OSError:
                pass


def _trash_purge_candidates(root: Path, older_than_days: int,
                            now: datetime) -> tuple[list[dict[str, Any]],
                                                   list[dict[str, Any]]]:
    """Old, intact trash runs and why other runs are not candidates."""
    _, directories = _trash_runs(root)
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for directory in directories:
        manifest, problem = _load_trash_manifest(root, directory)
        if manifest is None:
            skipped.append({"trash_id": directory.name,
                            "reason": problem or "unknown trash manifest"})
            continue
        created_at = manifest.get("created_at")
        age = _age_days(created_at, now=now)
        if age is None:
            skipped.append({"trash_id": directory.name,
                            "reason": "trash manifest has no valid created_at"})
            continue
        if age < older_than_days:
            skipped.append({
                "trash_id": directory.name,
                "reason": f"run is {age} day(s) old; older-than-days is "
                          f"{older_than_days}"})
            continue
        entries = [e for e in manifest["entries"] if isinstance(e, dict)]
        eligible: list[dict[str, Any]] = []
        for entry in entries:
            if _trash_entry_state(entry) != "in-trash":
                continue
            if _trash_entry_problem(root, directory, entry) is not None:
                continue
            target = _resolve_display(root, str(entry.get("trash_path")))
            try:
                size = target.stat().st_size
            except OSError:
                continue
            digest = entry.get("hash")
            if not isinstance(digest, str) or not digest:
                digest, _ = _hash_file(target)
            eligible.append({
                "path": entry.get("path"),
                "source_path": entry.get("path"),
                "trash_path": entry.get("trash_path"),
                "size": size,
                "hash": digest,
                "classification": entry.get("classification"),
                "reason": entry.get("reason"),
                "run_created_at": created_at,
                "age_days": age,
            })
        if not eligible:
            skipped.append({
                "trash_id": directory.name,
                "reason": "no purgeable entries (restored, purged or unknown)"})
            continue
        candidates.append({
            "trash_id": directory.name,
            "id": directory.name,
            "trash_dir": _trash_display(root, directory),
            "created_at": created_at,
            "age_days": age,
            "count": len(eligible),
            "entries_total": len(entries),
            "restored": sum(1 for e in entries if e.get("restored_at")),
            "purged": sum(1 for e in entries if e.get("purged_at")),
            "remove_run": bool(entries) and len(eligible) == len(entries),
            "bytes": sum(int(item["size"] or 0) for item in eligible),
            "entries": eligible,
        })
    return candidates, skipped


def trash_purge_plan(root: str | Path, *, older_than_days: int | None = None,
                     ttl_s: int = TRASH_PURGE_PLAN_TTL_S,
                     retention_days: int | None = None) -> dict[str, Any]:
    """A dry-run trash purge and its one-time confirmation token.

    Candidates are whole runs with a valid manifest older than
    `older_than_days` (default `files.retention_days`), restricted to entries
    that are still in the trash and safe to delete. Restored, purged, missing,
    symlinked and unsafe entries are never candidates. Nothing is deleted and
    no repository path is touched.
    """
    root_path = Path(root).resolve()
    if retention_days is None:
        retention = trash_retention_days(root_path)
    elif (isinstance(retention_days, bool)
          or not isinstance(retention_days, int) or retention_days < 0):
        raise ValueError("retention_days must be a non-negative integer")
    else:
        retention = retention_days
    older = retention if older_than_days is None else older_than_days
    if isinstance(older, bool) or not isinstance(older, int) or older < 0:
        raise ValueError("older_than_days must be a non-negative integer")
    now = datetime.now(timezone.utc)
    candidates, skipped = _trash_purge_candidates(root_path, older, now)
    expires = now + timedelta(seconds=max(0, int(ttl_s)))
    token = "p" + secrets.token_urlsafe(18)
    plan = {
        "schema": SCHEMA, "kind": "trash_purge_plan", "token": token,
        "root": str(root_path), "created_at": now.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "ttl_s": max(0, int(ttl_s)), "used_at": None,
        "older_than_days": older, "retention_days": retention,
        "candidates": candidates, "skipped": skipped,
    }
    _write_json(_purge_dir(root_path) / f"{token}.json", plan)
    _prune_purge_plans(root_path)
    return {**plan, "dry_run": True, "confirmation": token,
            "counts": {
                "runs": len(candidates),
                "entries": sum(int(run.get("count") or 0)
                               for run in candidates),
                "bytes": sum(int(run.get("bytes") or 0)
                             for run in candidates),
                "skipped": len(skipped),
            },
            "ok": True}


def _load_purge_plan(root: str | Path, confirmation: str) -> dict[str, Any]:
    if not _valid_token(confirmation):
        raise FileLedgerError("invalid purge confirmation token")
    root_path = Path(root).resolve()
    path = _purge_dir(root_path) / f"{confirmation}.json"
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileLedgerError("unknown purge confirmation token") from exc
    except json.JSONDecodeError as exc:
        raise FileLedgerError("purge confirmation token is corrupt") from exc
    if (not isinstance(plan, dict) or plan.get("kind") != "trash_purge_plan"
            or plan.get("token") != confirmation):
        raise FileLedgerError("purge confirmation token is not a plan")
    if plan.get("used_at"):
        raise FileLedgerError("purge confirmation token was already used")
    if plan.get("root") != str(root_path):
        raise FileLedgerError("trash purge plan belongs to another repository")
    expires = plan.get("expires_at")
    try:
        expired = (not isinstance(expires, str)
                   or datetime.now(timezone.utc) >= datetime.fromisoformat(expires))
    except ValueError:
        expired = True
    if expired:
        raise FileLedgerError("purge confirmation token has expired")
    if not isinstance(plan.get("candidates"), list):
        raise FileLedgerError("purge confirmation token has no candidates")
    return plan


def _consume_purge_plan(root: Path, plan: Mapping[str, Any], *,
                        session: str | None, harness: str | None) -> None:
    marker = _purge_dir(root) / f"{plan['token']}.used"
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                             0o600)
    except FileExistsError as exc:
        raise FileLedgerError(
            "purge confirmation token was already used") from exc
    except OSError as exc:
        raise FileLedgerError(f"cannot consume purge token: {exc}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(_now() + "\n")
    item = dict(plan)
    item["used_at"] = _now()
    item["used_by"] = {"session": _label(session), "harness": _label(harness)}
    _write_json(_purge_dir(root) / f"{plan['token']}.json", item)


def _trim_jsonl(path: Path, limit: int) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= limit:
        return
    try:
        path.write_text("\n".join(lines[-limit:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def _append_trash_audit(root: Path, record: Mapping[str, Any]) -> None:
    """Append one metadata-only audit row under `.ozagia/trash/audit.jsonl`."""
    path = _trash_audit_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), sort_keys=True) + "\n")
    except OSError as exc:
        raise FileLedgerError(f"cannot write trash audit: {exc}") from exc
    _trim_jsonl(path, MAX_TRASH_AUDIT_RECORDS)


def _prune_empty_dirs(run_dir: Path, start: Path) -> None:
    """Remove empty directories between a purged file and its run directory."""
    current = start
    while current != run_dir and run_dir in current.parents:
        if current.is_symlink():
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _remove_purged_run(run_dir: Path, manifest: Mapping[str, Any]) -> bool:
    """Remove a run directory when only its manifest is left.

    Fully purged runs disappear from `trash list`; the audit is the durable
    record. A run with restored entries, unknown entries or extra files keeps
    its manifest.
    """
    manifest_path = run_dir / "manifest.json"
    try:
        leftovers = [p for p in run_dir.iterdir() if p != manifest_path]
    except OSError:
        return False
    if leftovers:
        return False
    try:
        manifest_path.unlink()
    except OSError:
        return False
    try:
        run_dir.rmdir()
    except OSError:
        _write_json(manifest_path, manifest)
        return False
    return True


def _trash_purge_apply(root: Path, confirmation: str, *,
                       session: str | None, harness: str | None,
                       reason: str) -> dict[str, Any]:
    plan = _load_purge_plan(root, confirmation)
    _consume_purge_plan(root, plan, session=session, harness=harness)
    older = int(plan.get("older_than_days") or 0)
    stored_retention = plan.get("retention_days")
    retention = (int(stored_retention)
                 if isinstance(stored_retention, int)
                 and not isinstance(stored_retention, bool) else older)
    trash_root, _ = _trash_runs(root)
    applied: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    removed_runs: list[str] = []
    audit_path = _trash_audit_path(root)

    def refuse(path: Any, why: str) -> None:
        refused.append({"path": path, "reason": why})

    for run in plan["candidates"]:
        if not isinstance(run, Mapping):
            continue
        trash_id = run.get("trash_id")
        planned_entries = run.get("entries")
        planned_entries = (planned_entries
                           if isinstance(planned_entries, list) else [])
        if not _valid_trash_id(trash_id):
            for item in planned_entries:
                if isinstance(item, Mapping):
                    refuse(item.get("path"), "unsafe trash run id")
            continue
        run_dir = trash_root / str(trash_id)
        if run_dir.is_symlink() or not run_dir.is_dir():
            for item in planned_entries:
                if isinstance(item, Mapping):
                    refuse(item.get("path"), "trash run is missing or unsafe")
            continue
        try:
            if run_dir.resolve().parent != trash_root.resolve():
                raise ValueError
        except (OSError, ValueError):
            for item in planned_entries:
                if isinstance(item, Mapping):
                    refuse(item.get("path"), "trash run is outside the trash")
            continue
        manifest, problem = _load_trash_manifest(root, run_dir)
        if manifest is None:
            for item in planned_entries:
                if isinstance(item, Mapping):
                    refuse(item.get("path"), problem or "unknown trash manifest")
            continue
        if manifest.get("created_at") != run.get("created_at"):
            for item in planned_entries:
                if isinstance(item, Mapping):
                    refuse(item.get("path"),
                           "trash manifest changed since the plan")
            continue
        current: dict[tuple[Any, Any], dict[str, Any]] = {}
        for entry in manifest["entries"]:
            if isinstance(entry, dict):
                current.setdefault(
                    (entry.get("path"), entry.get("trash_path")), entry)
        changed = False
        removed_run = False
        audits: list[dict[str, Any]] = []
        for planned in planned_entries:
            if not isinstance(planned, Mapping):
                continue
            rel = planned.get("path")
            trash_path = planned.get("trash_path")
            entry = current.get((rel, trash_path))
            if entry is None:
                refuse(rel, "entry is no longer in the trash manifest")
                continue
            state = _trash_entry_state(entry)
            if state != "in-trash":
                refuse(rel, f"entry is already {state}")
                continue
            problem = _trash_entry_problem(root, run_dir, entry)
            if problem:
                refuse(rel, problem)
                continue
            target = _resolve_display(root, str(entry.get("trash_path")))
            try:
                size = target.stat().st_size
            except OSError:
                refuse(rel, "file is missing from trash")
                continue
            expected = planned.get("hash") or entry.get("hash")
            if isinstance(expected, str) and expected:
                digest, _ = _hash_file(target)
                if digest is not None and digest != expected:
                    refuse(rel, "file changed since the plan")
                    continue
            try:
                target.unlink()
            except OSError as exc:
                refuse(rel, f"delete failed: {exc}")
                continue
            purged_at = _now()
            entry["purged_at"] = purged_at
            entry["decision"] = "purged"
            changed = True
            applied.append({
                "path": rel, "trash_id": trash_id,
                "trash_path": trash_path, "size": size,
                "hash": expected or entry.get("hash"), "decision": "purged",
                "age_days": planned.get("age_days"),
            })
            audits.append({
                "schema": SCHEMA, "kind": "trash_purge",
                "event": "purged", "decision": "purged",
                "purged_at": purged_at, "trash_id": trash_id,
                "trash_dir": _trash_display(root, run_dir),
                "path": rel, "trash_path": trash_path,
                "size": size, "hash": expected or entry.get("hash"),
                "classification": entry.get("classification"),
                "reason": entry.get("reason"),
                "age_days": planned.get("age_days"),
                "manifest_created_at": manifest.get("created_at"),
                "older_than_days": older, "retention_days": retention,
                "purged_by": {
                    "session": _label(session), "harness": _label(harness),
                    "tool": "trash_purge", "reason": _clean_reason(reason),
                },
            })
            _prune_empty_dirs(run_dir, target.parent)
        if changed:
            if (manifest["entries"]
                    and all(isinstance(entry, Mapping)
                            and _trash_entry_state(entry) == "purged"
                            for entry in manifest["entries"])):
                removed_run = _remove_purged_run(run_dir, manifest)
            if not removed_run:
                _write_json(run_dir / "manifest.json", manifest)
            for audit in audits:
                _append_trash_audit(root, audit)
        if removed_run:
            removed_runs.append(str(trash_id))
    return {
        "schema": SCHEMA, "dry_run": False, "token": confirmation,
        "confirmation": confirmation,
        "older_than_days": older, "retention_days": retention,
        "applied": applied, "purged": applied, "refused": refused,
        "removed_runs": removed_runs,
        "counts": {"purged": len(applied), "applied": len(applied),
                   "refused": len(refused), "runs": len(removed_runs),
                   "bytes": sum(int(item.get("size") or 0)
                                for item in applied)},
        "audit_path": _trash_display(root, audit_path),
        "ok": not refused,
    }


def trash_purge(root: str | Path, *, older_than_days: int | None = None,
                confirmation: str | Mapping[str, Any] = "",
                ttl_s: int = TRASH_PURGE_PLAN_TTL_S,
                dry_run: bool | None = None,
                session: str | None = None, harness: str | None = None,
                reason: str = "trash purge") -> dict[str, Any]:
    """Dry-run trash purging, or apply a one-time confirmation token.

    With no confirmation token the call is a dry run (the default) and returns
    the token for a later apply. Applying deletes only trash entries that are
    still present, older than `older_than_days`, and safe; repository files are
    never touched. Each purge appends metadata to
    `.ozagia/trash/audit.jsonl`. Restored and unknown entries are refused.
    """
    root_path = Path(root).resolve()
    if isinstance(confirmation, Mapping):
        confirmation = str(confirmation.get("confirmation")
                           or confirmation.get("token") or "")
    token = str(confirmation or "")
    apply = bool(token) if dry_run is None else not dry_run
    if not apply:
        return trash_purge_plan(root_path, older_than_days=older_than_days,
                                ttl_s=ttl_s)
    return _trash_purge_apply(root_path, token, session=session,
                              harness=harness, reason=reason)


def _trash_directory(root: Path, value: str) -> Path:
    trash_root = _trash_root(root).resolve()
    raw = str(value or "")
    if not raw or "\x00" in raw:
        raise FileLedgerError("trash id is required")
    candidates: list[Path] = []
    path = Path(raw)
    if path.is_absolute():
        candidates.append(path)
    elif "/" in raw or raw == "manifest.json":
        candidates.append(root / raw)
        candidates.append(trash_root / raw)
    else:
        candidates.append(trash_root / raw)
        candidates.append(root / raw)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.name == "manifest.json":
            resolved = resolved.parent
        if resolved != trash_root and trash_root not in resolved.parents:
            continue
        if resolved.is_dir() and not resolved.is_symlink():
            return resolved
    raise FileLedgerError("trash run not found")


def restore(root: str | Path, trash_id: str, *,
            paths: Sequence[str] | None = None, session: str | None = None,
            harness: str | None = None, task: str | None = None,
            tool: str = "restore") -> dict[str, Any]:
    """Move files from one trash run back, refusing any conflict.

    The destination must not exist and must not be a symlink; `.git` and the
    state directory are refused. Restoring updates the trash manifest and
    appends an active ledger record with `restored_from`.
    """
    root_path = Path(root).resolve()
    if isinstance(trash_id, Mapping):
        trash_id = str(trash_id.get("trash_id") or trash_id.get("trash_dir")
                       or "")
    directory = _trash_directory(root_path, str(trash_id))
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileLedgerError("trash manifest is missing") from exc
    except json.JSONDecodeError as exc:
        raise FileLedgerError("trash manifest is corrupt") from exc
    if not isinstance(manifest, dict) or not isinstance(
            manifest.get("entries"), list):
        raise FileLedgerError("trash manifest is corrupt")
    entries = [e for e in manifest["entries"] if isinstance(e, dict)]
    wanted: list[str] | None = None
    if paths is not None:
        if isinstance(paths, (str, Path)):
            paths = [paths]
        wanted = []
        for value in paths:
            rel = _relative(root_path, value)
            if rel is None:
                rel = str(value)
            wanted.append(rel)
    latest = latest_records(root_path)
    restored: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    matched: set[str] = set()
    changed_manifest = False
    for entry in entries:
        rel = entry.get("path")
        trash_path = entry.get("trash_path")
        if not isinstance(rel, str) or not isinstance(trash_path, str):
            refused.append({"path": str(rel), "reason": "corrupt manifest entry"})
            continue
        if wanted is not None and rel not in wanted:
            continue
        matched.add(rel)
        if entry.get("restored_at"):
            refused.append({"path": rel, "reason": "already restored"})
            continue
        problem = _path_parts_problem(rel)
        if problem:
            refused.append({"path": rel, "reason": problem})
            continue
        source = _resolve_display(root_path, trash_path)
        expected_parent = directory.resolve()
        unsafe_source = False
        try:
            relative_source = source.relative_to(directory)
        except ValueError:
            unsafe_source = True
            relative_source = Path()
        current_source = directory
        for part in relative_source.parts:
            current_source = current_source / part
            if current_source.is_symlink():
                unsafe_source = True
                break
        try:
            source_resolved = source.resolve(strict=True)
        except OSError:
            refused.append({"path": rel, "reason": "file is missing from trash"})
            continue
        if (unsafe_source or expected_parent not in source_resolved.parents
                or source.is_symlink() or not source.is_file()):
            refused.append({"path": rel, "reason": "trash path is unsafe"})
            continue
        destination = root_path / rel
        current = root_path
        unsafe = False
        for part in Path(rel).parts[:-1]:
            current = current / part
            if current.is_symlink():
                unsafe = True
                break
        if unsafe:
            refused.append({"path": rel, "reason": "symlink destination refused"})
            continue
        if destination.is_symlink() or destination.exists():
            refused.append({"path": rel, "reason": "destination already exists"})
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        except OSError as exc:
            refused.append({"path": rel, "reason": f"restore failed: {exc}"})
            continue
        previous = None
        for item in latest.values():
            if item.get("trash_path") == trash_path and item.get("path") == rel:
                previous = item
                break
        record = {k: v for k, v in (previous or {}).items()
                  if k != "recorded_at"}
        if not record:
            record = {
                "schema": SCHEMA, "path": rel, "worktree": str(root_path),
                "hash": entry.get("hash"), "size": int(entry.get("size") or 0),
                "created_at": _now(),
                "created_by": {"session": None, "harness": None, "task": None,
                               "tool": None, "command": None},
                "reason": _clean_reason(entry.get("reason")),
                "classification": entry.get("classification") or "other",
                "references": [], "duplicate_of": None, "moved_from": None,
                "cleanup": {"policy": "ask", "after": None},
            }
        record.update({
            "schema": SCHEMA, "path": rel, "status": "active",
            "restored_from": trash_path, "restored_to": rel,
            "restored_at": _now(),
            "decision": "restore", "change": "restored",
            "restored_by": {"session": _label(session),
                            "harness": _label(harness),
                            "task": _label(task), "tool": _label(tool)},
        })
        append_record(root_path, record)
        latest[rel] = record
        entry["restored_at"] = record["restored_at"]
        entry["decision"] = "restore"
        changed_manifest = True
        restored.append({"path": rel, "trash_path": trash_path,
                         "size": int(entry.get("size") or 0),
                         "hash": entry.get("hash")})
    if wanted is not None:
        refused_paths = {str(item.get("path")) for item in refused}
        for rel in wanted:
            if rel not in matched and rel not in refused_paths:
                refused.append({"path": rel,
                                "reason": "not in this trash run"})
    if changed_manifest:
        _write_json(manifest_path, manifest)
    return {
        "schema": SCHEMA, "dry_run": False, "trash_id": directory.name,
        "restored": restored, "refused": refused,
        "counts": {"restored": len(restored), "refused": len(refused)},
        "ok": not refused,
    }



def new_file_count(root: str | Path, *, session: str | None = None,
                   since_hours: int = 24) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    count = 0
    for item in list_files(root, limit=1000):
        created = item.get("created_at")
        if not isinstance(created, str):
            continue
        try:
            age = now - datetime.fromisoformat(created)
        except ValueError:
            continue
        if age.total_seconds() > since_hours * 3600:
            continue
        creator = item.get("created_by") or {}
        if session and creator.get("session") != session:
            continue
        count += 1
    return {"count": count, "since_hours": since_hours, "session": session}


def format_list(records: Sequence[Mapping[str, Any]]) -> str:
    if not records:
        return "no created files recorded"
    lines = []
    for item in records:
        lines.append(f"{item.get('classification', '?'):9} "
                     f"{item.get('size', 0):>8}  {item.get('path')}  "
                     f"({item.get('reason') or 'unknown'})")
    return "\n".join(lines)


def format_explain(report: Mapping[str, Any]) -> str:
    record = report.get("record") or {}
    lines = [f"{report.get('path')}: exists={report.get('exists')} "
             f"size={report.get('size')} changed={report.get('changed')}"]
    if record:
        creator = record.get("created_by") or {}
        lines.append(f"  classification: {record.get('classification')}")
        lines.append(f"  reason: {record.get('reason')}")
        lines.append(f"  created_by: session={creator.get('session')} "
                     f"task={creator.get('task')} tool={creator.get('tool')}")
        lines.append(f"  references: {', '.join(record.get('references') or []) or 'none'}")
        lines.append(f"  cleanup: {(record.get('cleanup') or {}).get('policy')}")
    else:
        lines.append("  no ledger record; unknown origin")
    route = report.get("route")
    if route:
        lines.append(f"  markdown: kind={route.get('kind')} "
                     f"allowed={route.get('allowed')} "
                     f"suggested={route.get('suggested_path')}")
        if route.get("duplicate_of"):
            lines.append(f"  duplicate_of: {route['duplicate_of']}")
    return "\n".join(lines)


def format_cleanup(records: Sequence[Mapping[str, Any]]) -> str:
    if not records:
        return "no cleanup candidates"
    lines = ["cleanup is dry-run only; nothing is deleted"]
    for item in records:
        lines.append(f"  {item.get('recommended', 'ask'):5} "
                     f"{item.get('size', 0):>8}  {item.get('path')}  "
                     f"age={item.get('age_days')}d reason={item.get('reason')}")
    return "\n".join(lines)


def format_cleanup_plan(plan: Mapping[str, Any]) -> str:
    candidates = plan.get("candidates") or []
    lines = [format_cleanup(candidates)]
    if candidates:
        lines.append(f"apply with: ozagia-files cleanup --apply --confirm "
                     f"{plan.get('confirmation') or plan.get('token')}")
        lines.append(f"the token is one-time and expires at "
                     f"{plan.get('expires_at')}")
    return "\n".join(lines)


def format_cleanup_apply(result: Mapping[str, Any]) -> str:
    lines = [f"cleanup: applied={result.get('counts', {}).get('applied', 0)} "
             f"refused={result.get('counts', {}).get('refused', 0)} "
             f"(nothing is deleted)"]
    if result.get("trash_id"):
        lines.append(f"  trash: {result.get('trash_dir') or result['trash_id']}")
    for item in result.get("applied") or []:
        lines.append(f"  {item.get('decision', 'trashed'):7} "
                     f"{item.get('size', 0):>8}  {item.get('path')} -> "
                     f"{item.get('trash_path')}")
    for item in result.get("refused") or []:
        lines.append(f"  refused {item.get('path')}: {item.get('reason')}")
    return "\n".join(lines)


def format_trash(listing: Mapping[str, Any]) -> str:
    runs = listing.get("trash") or []
    if not runs:
        return "trash is empty"
    lines = []
    for run in runs:
        state = run.get("retention_state") or "unknown"
        age = run.get("age_days")
        age_text = "unknown" if age is None else f"{age}d"
        lines.append(
            f"{run.get('trash_id')}: {run.get('count')} file(s), "
            f"{run.get('remaining_bytes', run.get('bytes', 0))} bytes, "
            f"restored={run.get('restored')} purged={run.get('purged')} "
            f"age={age_text} retention={state} "
            f"({run.get('trash_dir')})")
        for item in run.get("entries") or []:
            state = "restored" if item.get("restored_at") else (
                "purged" if item.get("purged_at") else "in trash")
            lines.append(f"  {state:9} {item.get('size', 0):>8}  "
                         f"{item.get('path')}")
    return "\n".join(lines)


def format_trash_purge(result: Mapping[str, Any]) -> str:
    counts = result.get("counts") or {}
    if result.get("dry_run"):
        lines = [
            "trash purge is dry-run only; nothing is deleted",
            f"  older-than-days={result.get('older_than_days')} "
            f"retention-days={result.get('retention_days')} "
            f"runs={counts.get('runs', 0)} entries={counts.get('entries', 0)} "
            f"bytes={counts.get('bytes', 0)}",
        ]
        for run in result.get("candidates") or []:
            lines.append(f"  {run.get('trash_id')} age={run.get('age_days')}d "
                         f"{run.get('count')} file(s) {run.get('bytes')} bytes")
            for item in run.get("entries") or []:
                lines.append(f"    {item.get('size', 0):>8}  "
                             f"{item.get('path')}")
        for skipped in result.get("skipped") or []:
            lines.append(f"  skipped {skipped.get('trash_id')}: "
                         f"{skipped.get('reason')}")
        if result.get("candidates"):
            lines.append(f"purge with: ozagia-files trash purge --confirm "
                         f"{result.get('confirmation') or result.get('token')}")
            lines.append(f"the token is one-time and expires at "
                         f"{result.get('expires_at')}")
        return "\n".join(lines)
    lines = [f"trash purge: purged={counts.get('purged', 0)} "
             f"refused={counts.get('refused', 0)} "
             f"bytes={counts.get('bytes', 0)}"]
    for item in result.get("applied") or []:
        lines.append(f"  purged {item.get('trash_id')} "
                     f"{item.get('size', 0):>8}  {item.get('path')}")
    for item in result.get("refused") or []:
        lines.append(f"  refused {item.get('path')}: {item.get('reason')}")
    if result.get("audit_path"):
        lines.append(f"  audit: {result.get('audit_path')}")
    return "\n".join(lines)


def format_restore(result: Mapping[str, Any]) -> str:
    lines = [f"restore {result.get('trash_id')}: "
             f"restored={result.get('counts', {}).get('restored', 0)} "
             f"refused={result.get('counts', {}).get('refused', 0)}"]
    for item in result.get("restored") or []:
        lines.append(f"  restored {item.get('path')}")
    for item in result.get("refused") or []:
        lines.append(f"  refused {item.get('path')}: {item.get('reason')}")
    return "\n".join(lines)


__all__ = [
    "FileLedgerError", "append_record", "cleanup_apply", "cleanup_candidates",
    "cleanup_plan", "explain", "format_cleanup", "format_cleanup_apply",
    "format_cleanup_plan", "format_explain", "format_list", "format_restore",
    "format_trash", "format_trash_purge", "ledger_path", "list_files",
    "list_trash", "markdown_route", "new_file_count", "orphans",
    "record_file", "record_paths", "restore", "scan_new_files",
    "session_delta", "session_manifest_path", "session_snapshot",
    "trash_purge", "trash_purge_plan", "trash_retention_days",
]
