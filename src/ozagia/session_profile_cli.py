"""Read-only, metadata-only aggregation of daemon session activity."""
from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import json
import os
import re
import tempfile
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ozagia.config import repo_root, state_path
from ozagia.daemon.server import JOURNAL_NAME
from ozagia.daemon import protocol
from ozagia.integrations.hookcore import daemon_request

MAX_JOURNAL_BYTES = 2 * 1024 * 1024
MAX_ROWS = 200
MAX_PROFILE_BYTES = 32_768
PROFILE_LOCK_TIMEOUT_S = 0.05
_PROFILE_LOCK = threading.RLock()


@contextmanager
def _profile_file_lock(directory: Path):
    """Serialize profile updates, bounding both thread and process waits."""
    deadline = time.monotonic() + PROFILE_LOCK_TIMEOUT_S
    if not _PROFILE_LOCK.acquire(timeout=PROFILE_LOCK_TIMEOUT_S):
        raise TimeoutError(f"session profile lock timed out after {PROFILE_LOCK_TIMEOUT_S:.3f}s")
    stream = None
    locked = False
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stream = (directory / ".write.lock").open("a+b")
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"session profile lock timed out after {PROFILE_LOCK_TIMEOUT_S:.3f}s"
                    ) from exc
                time.sleep(min(0.005, remaining))
        yield
    finally:
        try:
            if locked and stream is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            if stream is not None:
                stream.close()
            _PROFILE_LOCK.release()


def _bounded_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return min(2**53 - 1, max(0, int(value))) if value >= 0 else None
    except (ValueError, OverflowError):
        return None


def active_journal(root: Path) -> tuple[Path, str]:
    """Prefer the journal path announced by the live daemon's stats."""
    try:
        response = daemon_request(protocol.socket_path(root), {"id": 1, "op": "stats"}, timeout=0.4)
        stats = response.get("stats")
        journal = stats.get("journal") if isinstance(stats, dict) else None
        if isinstance(journal, str) and journal:
            return Path(journal), "active_daemon"
    except Exception:  # a profile remains useful when no daemon is reachable
        pass
    return state_path(root, JOURNAL_NAME), "fallback_unverified"


def report(root: Path, *, now: datetime | None = None,
           journal: Path | None = None, journal_source: str | None = None) -> dict:
    if journal is None:
        journal, detected_source = active_journal(root)
        journal_source = journal_source or detected_source
    journal_source = journal_source or "explicit"
    observed = datetime.now(timezone.utc) if now is None else now
    result = {
        "ok": True,
        "source": "daemon_journal",
        "journal_source": journal_source,
        "journal": "available" if journal.exists() else "absent",
        "observed_at": observed.isoformat(),
        "sessions": [],
        "coverage": {
            "model": "observed from OpenCode server message events when the Ozagia plugin is active; otherwise unknown",
            "tokens": "observed from OpenCode assistant-message metadata when supplied; otherwise unknown",
            "daemon": "persisted counters are preferred; bounded journal rows are a fallback. Successful explicit passthrough, handled, expired, failed (ok=false), and unknown outcomes are separate; errors include daemon failures. Legacy rows without ok accept explicit handled=false as passthrough. Missing or timed-out profile writes are unknown; daemon logs/RPC errors mark dropped writes.",
            "critical_path": "unavailable: model and tool durations do not establish task dependencies",
        },
    }
    size = 0
    if journal.exists():
        try:
            with journal.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - MAX_JOURNAL_BYTES))
                raw = stream.read(MAX_JOURNAL_BYTES).decode("utf-8", "replace")
        except OSError:
            result["journal"] = "unreadable"
            raw = ""
    else:
        raw = ""
    raw_lines = raw.splitlines()
    journal_rows_truncated = len(raw_lines) > MAX_ROWS * 4
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for line in raw_lines[-MAX_ROWS * 4:]:
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict) or row.get("op") not in ("tool", "after"):
            continue
        harness, session = row.get("harness"), row.get("session")
        if not isinstance(harness, str) or not isinstance(session, str) or not session:
            continue
        groups[(harness[:80], session[:256])].append(row)
    host_profiles = {}
    saved_profiles = {}
    profile_dir = state_path(root, "session_profiles")
    profiles_truncated = False
    try:
        paths = sorted(profile_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        profiles_truncated = len(paths) > MAX_ROWS
        for profile_path in paths[:MAX_ROWS]:
            if profile_path.stat().st_size > MAX_PROFILE_BYTES:
                continue
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(profile, dict) and isinstance(profile.get("session_id"), str):
                key = (str(profile.get("harness") or "opencode")[:80], profile["session_id"][:256])
                saved_profiles[key] = profile
                if isinstance(profile.get("host_profile"), dict):
                    host_profiles[key] = profile["host_profile"]
                elif profile.get("source") == "opencode_server_events" or "model_calls" in profile:
                    host_profiles[key] = profile
    except OSError:
        profiles_truncated = False
    for key in saved_profiles:
        groups.setdefault(key, [])
    sessions = []
    for (harness, session), rows in groups.items():
        saved = saved_profiles.get((harness, session)) or {}
        persisted_raw = saved.get("daemon_profile")
        persisted = dict(persisted_raw) if isinstance(persisted_raw, dict) else {}
        if persisted and "outcome_schema" not in persisted:
            # Reports can encounter old summaries before another daemon write
            # migrates them. Do not repeat their formerly inflated passthrough
            # count as a current known outcome.
            persisted["unknown"] = persisted.get("unknown", 0) + persisted.get("passthrough", 0)
            persisted["passthrough"] = 0
            persisted["failures"] = persisted.get("failures", 0)
            persisted["outcome_counts_incomplete"] = True
        actual_tools = [r for r in rows if r.get("op") == "tool" and not r.get("preflight")]
        tools = Counter(str(r.get("tool") or "unknown")[:80] for r in actual_tools)
        handled = sum(r.get("handled") is True and r.get("ok") is not False for r in actual_tools)
        passthrough = sum(
            r.get("handled") is False and not r.get("expired")
            and (r.get("ok") is True or "ok" not in r)
            for r in actual_tools
        )
        failures = sum(r.get("ok") is False for r in actual_tools)
        unknown = sum(
            r.get("ok") is not False and not r.get("expired")
            and r.get("handled") is not True and r.get("handled") is not False
            for r in actual_tools
        )
        errors = sum(r.get("is_error") is True or r.get("ok") is False for r in actual_tools)
        expired = sum(bool(r.get("expired")) and r.get("ok") is not False for r in actual_tools)
        fallbacks = sum(r.get("op") == "after" and bool(r.get("fallback")) for r in rows)
        durations = [r["ms"] for r in rows if isinstance(r.get("ms"), (int, float)) and r["ms"] >= 0]
        stamps = sorted(str(r["ts"]) for r in rows if isinstance(r.get("ts"), str))
        journal_counts_available = bool(rows)
        activity_source = "persisted_daemon_events" if persisted else (
            journal_source if journal_counts_available else "unknown")
        if persisted:
            tools = Counter(persisted.get("tools_by_name", {}))
            handled = persisted.get("handled", 0)
            passthrough = persisted.get("passthrough", 0)
            failures = persisted.get("failures", 0)
            unknown = persisted.get("unknown", 0)
            errors = persisted.get("errors", 0)
            expired = persisted.get("expired", 0)
            fallbacks = persisted.get("plugin_fallbacks", 0)
            duration = persisted.get("duration_ms", {})
            durations = []
            stamps = [str(persisted.get(k)) for k in ("first_seen", "last_seen") if persisted.get(k)]
            persisted_duration = duration if isinstance(duration, dict) else {}
        else:
            persisted_duration = None
        sessions.append({
            "harness": harness, "session_id": session,
            "activity_source": activity_source,
            "first_seen": stamps[0] if stamps else None,
            "last_seen": stamps[-1] if stamps else None,
            "operations": (persisted.get("operations", 0) if persisted_duration is not None else len(rows)), "tools": sum(tools.values()),
            "tool_calls_observed": (persisted.get("tool_calls_observed", 0) if persisted_duration is not None else len(actual_tools)),
            "handled": handled, "passthrough": passthrough, "expired": expired, "errors": errors,
            "failures": failures, "unknown": unknown,
            "plugin_fallbacks": fallbacks, "tools_by_name": dict(sorted(tools.items())),
            "duration_ms": persisted_duration or {"count": len(durations), "sum": round(sum(durations), 1),
                            "max": round(max(durations), 1) if durations else None},
            "host_profile": host_profiles.get((harness, session)),
            "daemon_profile": persisted or None,
        })
    sessions.sort(key=lambda item: item["last_seen"] or "", reverse=True)
    result["sessions"] = sessions[:MAX_ROWS]
    result["truncated"] = (size > MAX_JOURNAL_BYTES or journal_rows_truncated
                           or len(groups) > MAX_ROWS or profiles_truncated)
    result["journal_bytes_read"] = min(size, MAX_JOURNAL_BYTES)
    return result


def store_profile(root: Path, value: dict) -> dict:
    """Store only bounded model/tool metadata, keyed by a hashed session id."""
    directory = state_path(root, "session_profiles")
    with _profile_file_lock(directory):
        return _store_profile(root, value)


def _store_profile(root: Path, value: dict) -> dict:
    session = value.get("session_id")
    harness = value.get("harness", "opencode")
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", session):
        raise ValueError("session_id is required and must be at most 256 characters")
    if not isinstance(harness, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", harness):
        raise ValueError("harness must be a string of at most 80 characters")
    metadata = value.get("metadata")
    if not isinstance(metadata, dict) or len(json.dumps(metadata)) > MAX_PROFILE_BYTES:
        raise ValueError("metadata must be an object no larger than 32 KiB")
    # Whitelist structure/primitive fields so caller data cannot smuggle prompt,
    # tool input/output, paths, or arbitrary host objects into state.
    model_names = metadata.get("models")
    if not isinstance(model_names, list) or len(model_names) > 20 or any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_.:/+-]{1,160}", x) for x in model_names):
        raise ValueError("models must be a list of at most 20 short names")
    def count(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            return min(2**53 - 1, max(0, int(value))) if value >= 0 else None
        except (ValueError, OverflowError):
            return None
    def counters(name: str, allowed: tuple[str, ...]) -> dict[str, int | None]:
        raw = metadata.get(name, {})
        if not isinstance(raw, dict):
            return {key: None for key in allowed}
        out = {}
        for key in allowed:
            item = raw.get(key)
            out[key] = count(item)
        return out
    host_tools = metadata.get("tools", {})
    clean_tools = {}
    if isinstance(host_tools, dict):
        for name, statuses in list(host_tools.items())[:64]:
            if not isinstance(name, str) or len(name) > 80 or not isinstance(statuses, dict):
                continue
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
                continue
            clean_tools[name] = {}
            for state, raw_stats in list(statuses.items())[:8]:
                state_name = str(state) if state in {
                    "completed", "error", "running", "pending", "unknown"
                } else "other"
                if isinstance(raw_stats, dict):
                    clean_tools[name][state_name] = {
                        key: count(raw_stats.get(key))
                        for key in ("count", "timed_count", "untimed_count",
                                    "duration_ms_sum", "duration_ms_max")
                    }
                else:
                    amount = count(raw_stats)
                    if amount is not None:
                        clean_tools[name][state_name] = {
                            "count": amount, "timed_count": 0,
                            "untimed_count": amount, "duration_ms_sum": 0,
                            "duration_ms_max": None,
                        }
    coverage = metadata.get("coverage", {})
    clean_coverage = {
        key: count(coverage.get(key)) if isinstance(coverage, dict) else None
        for key in (
            "assistant_turns", "turns_with_tokens", "turns_without_tokens",
            "turns_with_complete_timing", "turns_without_complete_timing",
            "host_tool_calls", "host_tool_calls_with_timing",
            "host_tool_calls_without_timing",
        )
    }
    clean = {
        "session_id": session, "harness": harness,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "model_calls": min(1000000, count(metadata.get("model_calls", 0)) or 0),
        "models": list(dict.fromkeys(model_names))[:20],
        "tokens": counters("tokens", ("input", "output", "reasoning", "cache_read", "cache_write")),
        "timing_ms": counters("timing_ms", ("first_token", "streamed", "completed", "model_turn")),
        "tools": clean_tools, "coverage": clean_coverage,
    }
    import hashlib
    key = hashlib.sha256((harness + "\0" + session).encode()).hexdigest()
    directory = state_path(root, "session_profiles")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{key}.json"
    envelope = {}
    if path.exists() and path.stat().st_size <= MAX_PROFILE_BYTES:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                envelope = existing
        except (OSError, ValueError):
            pass
    envelope.update({"session_id": session, "harness": harness,
                     "observed_at": clean["observed_at"], "host_profile": clean})
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > MAX_PROFILE_BYTES:
        raise ValueError("sanitized profile exceeds the storage limit")
    fd, temp_name = tempfile.mkstemp(prefix=".profile-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            # Atomic replacement is sufficient for this optional CLI ingest.
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    profiles = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in profiles[MAX_ROWS:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return {"ok": True, "stored": True, "session_id": session}


def store_profile_event(root: Path, value: dict) -> dict:
    directory = state_path(root, "session_profiles")
    with _profile_file_lock(directory):
        return _store_profile_event(root, value)


_CAPTURE_DIAGNOSTIC_COUNTERS = (
    "events_received", "step_events", "content_events", "tool_parts_seen",
    "tool_parts_accepted", "tool_lifecycle_started", "tool_lifecycle_called",
    "tool_lifecycle_terminal", "tool_lifecycle_events_accepted",
    "profile_events_accepted", "store_attempts",
    "store_successes_observed_lower_bound", "store_failures_observed_lower_bound",
)


def _merge_capture_diagnostics(profile: dict, raw: object) -> bool:
    """Merge a bounded snapshot of metadata-only plugin counters."""
    if not isinstance(raw, dict):
        return False
    capture_id = raw.get("capture_id")
    if not isinstance(capture_id, str) or not re.fullmatch(r"[a-f0-9-]{36}", capture_id):
        return False
    counters = raw.get("counters") if isinstance(raw.get("counters"), dict) else raw
    clean = {key: min(2**53 - 1, int(counters[key])) for key in _CAPTURE_DIAGNOSTIC_COUNTERS
             if isinstance(counters.get(key), int) and not isinstance(counters.get(key), bool)
             and 0 <= counters[key] <= 2**53 - 1}
    if not clean:
        return False
    records = profile.setdefault("capture_diagnostics", [])
    if not isinstance(records, list):
        records = profile["capture_diagnostics"] = []
    record = next((item for item in records if isinstance(item, dict)
                   and item.get("capture_id") == capture_id), None)
    if record is None:
        record = {"capture_id": capture_id, "counters": {}}
        records.append(record)
        del records[:-4]
    old = record.get("counters") if isinstance(record.get("counters"), dict) else {}
    merged = {key: max(old.get(key, 0), value) for key, value in clean.items()}
    if merged == old:
        return False
    record["counters"] = merged
    return True


def _write_profile_event(path: Path, directory: Path, envelope: dict, new_profile: bool) -> None:
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode()) > MAX_PROFILE_BYTES:
        profile = envelope["host_profile"]
        profile["models"] = profile.get("models", [])[:10]
        profile["tools"] = dict(list(profile.get("tools", {}).items())[-64:])
        profile["truncated"] = True
        envelope["host_profile"] = profile
        encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    fd, temp_name = tempfile.mkstemp(prefix=".profile-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    if new_profile:
        profiles = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in profiles[MAX_ROWS:]:
            try:
                stale.unlink()
            except OSError:
                pass


def _store_profile_event(root: Path, value: dict) -> dict:
    """Merge one metadata-only OpenCode message/tool completion event.

    The server plugin sends final assistant messages and terminal tool-part
    states only. A short hashed-ID window deduplicates reconnect/replay events;
    aggregate counters are retained for the whole session in state.
    """
    session = value.get("session_id", value.get("session"))
    harness = value.get("harness", "opencode")
    kind = value.get("kind")
    event_id = value.get("event_id")
    metadata = value.get("metadata")
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", session):
        raise ValueError("invalid session id")
    if not isinstance(harness, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", harness):
        raise ValueError("invalid harness")
    if kind not in ("message", "tool") or not isinstance(event_id, str) or not event_id or len(event_id) > 256:
        raise ValueError("invalid event kind or id")
    if not isinstance(metadata, dict) or len(json.dumps(metadata)) > 2048:
        raise ValueError("event metadata must be an object under 2 KiB")
    import hashlib
    event_key = hashlib.sha256(event_id.encode()).hexdigest()[:20]
    directory = state_path(root, "session_profiles")
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((harness + "\0" + session).encode()).hexdigest()
    path = directory / f"{key}.json"
    new_profile = not path.exists()
    if not new_profile and path.stat().st_size <= MAX_PROFILE_BYTES:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            envelope = {}
    else:
        envelope = {}
    if not isinstance(envelope, dict):
        envelope = {}
    profile = envelope.get("host_profile")
    if not isinstance(profile, dict) or profile.get("source") != "opencode_server_events":
        profile = {
            "session_id": session, "harness": harness,
            "source": "opencode_server_events", "model_calls": 0, "models": [],
            "tokens": {k: None for k in ("input", "output", "reasoning", "cache_read", "cache_write")},
            "timing_ms": {k: None for k in ("first_token", "streamed", "completed", "model_turn")},
            "coverage": {
                "assistant_turns": 0, "turns_with_tokens": 0,
                "turns_without_tokens": 0, "turns_with_complete_timing": 0,
                "turns_without_complete_timing": 0, "host_tool_calls": 0,
                "host_tool_calls_with_timing": 0,
                "host_tool_calls_without_timing": 0,
            },
            "tools": {}, "seen_message_ids": [], "seen_tool_ids": [],
            "truncated": False,
        }
    seen_key = "seen_message_ids" if kind == "message" else "seen_tool_ids"
    seen = profile.get(seen_key)
    if not isinstance(seen, list):
        seen = []
    diagnostics_changed = _merge_capture_diagnostics(profile, metadata.get("capture_diagnostics"))
    if event_key in seen:
        if diagnostics_changed:
            profile["observed_at"] = datetime.now(timezone.utc).isoformat()
            envelope.update({"session_id": session, "harness": harness,
                             "observed_at": profile["observed_at"], "host_profile": profile})
            _write_profile_event(path, directory, envelope, new_profile)
        return {"ok": True, "stored": False, "duplicate": True}
    seen.append(event_key)
    if len(seen) > 256:
        del seen[:-256]
        profile["truncated"] = True
    profile[seen_key] = seen
    coverage = profile["coverage"]

    def add_known(target: dict, key: str, raw: object) -> bool:
        amount = _bounded_count(raw)
        if amount is None:
            return False
        target[key] = (target[key] or 0) + amount if target.get(key) is not None else amount
        return True

    if kind == "message":
        model = metadata.get("model")
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:/+-]{1,160}", model):
            profile["models"] = list(dict.fromkeys([*(profile.get("models") or []), model]))[:20]
        profile["model_calls"] = int(profile.get("model_calls") or 0) + 1
        tokens = metadata.get("tokens") if isinstance(metadata.get("tokens"), dict) else {}
        has_tokens = False
        for key_name in ("input", "output", "reasoning", "cache_read", "cache_write"):
            has_tokens |= add_known(profile["tokens"], key_name, tokens.get(key_name))
        coverage["assistant_turns"] += 1
        coverage["turns_with_tokens" if has_tokens else "turns_without_tokens"] += 1
        timing = metadata.get("timing_ms") if isinstance(metadata.get("timing_ms"), dict) else {}
        for key_name in ("first_token", "streamed", "completed", "model_turn"):
            add_known(profile["timing_ms"], key_name, timing.get(key_name))
        complete_timing = _bounded_count(timing.get("model_turn")) is not None
        coverage["turns_with_complete_timing" if complete_timing else "turns_without_complete_timing"] += 1
    else:
        tool = metadata.get("tool")
        status = metadata.get("status")
        if not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", tool):
            return {"ok": False, "error": "invalid tool name"}
        if status not in ("completed", "error"):
            return {"ok": False, "error": "tool event must be terminal"}
        duration = _bounded_count(metadata.get("duration_ms"))
        coverage["host_tool_calls"] += 1
        coverage["host_tool_calls_with_timing" if duration is not None else "host_tool_calls_without_timing"] += 1
        statuses = profile["tools"].setdefault(tool, {})
        stats = statuses.setdefault(status, {
            "count": 0, "timed_count": 0, "untimed_count": 0,
            "duration_ms_sum": 0, "duration_ms_max": None,
        })
        stats["count"] += 1
        if duration is None:
            stats["untimed_count"] += 1
        else:
            stats["timed_count"] += 1
            stats["duration_ms_sum"] += duration
            stats["duration_ms_max"] = max(stats["duration_ms_max"] or 0, duration)
    profile["observed_at"] = datetime.now(timezone.utc).isoformat()
    envelope.update({"session_id": session, "harness": harness,
                     "observed_at": profile["observed_at"], "host_profile": profile})
    _write_profile_event(path, directory, envelope, new_profile)
    return {"ok": True, "stored": True}


def store_daemon_event(root: Path, record: dict) -> None:
    """Persist a sanitized daemon request summary before the daemon exits."""
    op = record.get("op")
    session = record.get("session")
    harness = record.get("harness")
    if (op not in ("tool", "after") or not isinstance(session, str) or not session
            or not isinstance(harness, str) or not harness):
        return
    directory = state_path(root, "session_profiles")
    with _profile_file_lock(directory):
        import hashlib
        key = hashlib.sha256((harness + "\0" + session).encode()).hexdigest()
        path = directory / f"{key}.json"
        new_profile = not path.exists()
        if not new_profile and path.stat().st_size <= MAX_PROFILE_BYTES:
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                profile = {}
        else:
            profile = {}
        if not isinstance(profile, dict):
            profile = {}
        profile.update({"session_id": session, "harness": harness})
        stats = profile.setdefault("daemon_profile", {
            "source": "daemon_events", "operations": 0, "tool_calls_observed": 0,
            "outcome_schema": 2, "outcome_counts_incomplete": False,
            "handled": 0, "passthrough": 0, "expired": 0, "failures": 0, "unknown": 0,
            "preflights": 0,
            "errors": 0, "plugin_fallbacks": 0, "tools_by_name": {},
            "duration_ms": {"count": 0, "sum": 0, "max": None},
            "first_seen": record.get("ts"), "last_seen": record.get("ts"),
        })
        if "outcome_schema" not in stats:
            # Old versions counted handled=None and failed requests as
            # passthrough. Preserve that quantity only as unknown.
            stats["unknown"] = stats.get("unknown", 0) + stats.get("passthrough", 0)
            stats["passthrough"] = 0
            stats["failures"] = stats.get("failures", 0)
            stats["outcome_schema"] = 2
            stats["outcome_counts_incomplete"] = True
        stats["operations"] += 1
        if record.get("op") == "tool":
            if record.get("preflight"):
                stats["preflights"] += 1
            else:
                stats["tool_calls_observed"] += 1
                if record.get("ok") is False:
                    stats["failures"] += 1
                elif record.get("expired"):
                    stats["expired"] += 1
                elif record.get("handled") is True:
                    stats["handled"] += 1
                elif record.get("handled") is False and (
                    record.get("ok") is True or "ok" not in record
                ):
                    stats["passthrough"] += 1
                else:
                    stats["unknown"] += 1
                if record.get("is_error") is True or record.get("ok") is False:
                    stats["errors"] += 1
                tool = record.get("tool")
                if isinstance(tool, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", tool):
                    tools = stats["tools_by_name"]
                    tools[tool] = tools.get(tool, 0) + 1
        elif record.get("fallback"):
            stats["plugin_fallbacks"] += 1
        duration = _bounded_count(record.get("ms"))
        if duration is not None:
            elapsed = stats["duration_ms"]
            elapsed["count"] += 1
            elapsed["sum"] += duration
            elapsed["max"] = max(elapsed["max"] or 0, duration)
        if isinstance(record.get("ts"), str):
            stats["last_seen"] = record["ts"]
        stats["tools_by_name"] = dict(list(stats["tools_by_name"].items())[-64:])
        profile["observed_at"] = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(profile, sort_keys=True, separators=(",", ":"))
        fd, temp_name = tempfile.mkstemp(prefix=".profile-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                # Match the daemon journal's process-crash durability without
                # forcing storage-device sync latency on every tool response.
                # Power-loss durability is not claimed.
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        if new_profile:
            profiles = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in profiles[MAX_ROWS:]:
                try:
                    stale.unlink()
                except OSError:
                    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--session", help="show only this session ID")
    parser.add_argument("--record", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else repo_root()
    if args.record:
        try:
            value = json.loads(base64.urlsafe_b64decode(args.record.encode("ascii")))
            if not isinstance(value, dict):
                raise ValueError("record must be an object")
            result = store_profile(root, value)
        except (ValueError, UnicodeError) as exc:
            parser.error(str(exc))
    else:
        result = report(root)
        if args.session is not None:
            result["sessions"] = [item for item in result["sessions"]
                                  if item["session_id"] == args.session]
            result["session_filter"] = args.session
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
