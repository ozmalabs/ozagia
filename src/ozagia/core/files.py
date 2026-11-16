"""Created-file ledger, Markdown routing and read-only cleanup candidates.

The ledger records metadata (path, hash, size, creator, reason, classification,
references) and never stores prompts or file contents. It is a bounded JSONL
file under the state directory. Cleanup here is a dry-run listing; moving files
to trash is a later, explicitly confirmed action.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import date, datetime, timezone
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


def _record_item(root_path: Path, rel: str, *, digest: str | None, size: int,
                 session: str | None, harness: str | None, task: str | None,
                 tool: str | None, reason: str,
                 moved_from: str | None = None) -> dict[str, Any]:
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
    if classification in {"root", "plan", "result", "research", "docs",
                          "history"}:
        cleanup_policy = "keep"
    elif classification == "scratch":
        cleanup_policy = "delete_after"
    else:
        cleanup_policy = "ask"
    return {
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
                       classifications: Sequence[str] | None = None) -> list[dict[str, Any]]:
    selected = set(classifications or ("note", "scratch", "other"))
    out: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for item in latest_records(root).values():
        if item.get("status") != "active" or item.get("references"):
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
        recommended = ("trash" if policy == "delete_after" or
                       item.get("classification") == "scratch" else "ask")
        out.append({**item, "age_days": age, "recommended": recommended})
    return sorted(out, key=lambda item: (-int(item.get("size") or 0),
                                         str(item.get("path"))))


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


__all__ = [
    "FileLedgerError", "append_record", "cleanup_candidates", "explain",
    "format_cleanup", "format_explain", "format_list", "ledger_path",
    "list_files", "markdown_route", "new_file_count", "orphans",
    "record_file", "record_paths", "scan_new_files",
]
