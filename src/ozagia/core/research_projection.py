"""A bounded, read-only projection of retained research graph records.

The projection answers *what has been recorded* about a research question.  It
does not decide whether a claim is true, whether a source supports it, or
whether a model candidate is faithful.  Every claim and assumption is
``declared`` evidence; a recorded ``supports`` edge is still an authored
assertion, not an observation.

Input is the retained graph state written by ``ozagia.research.graph`` under
``<root>/.ozagia/research/<session>.json`` (or an explicitly named legacy
session under ``var/ocelot_research``).  The module reads only JSON/state and
selected local source files:

* no fetch, HTTP, model, OCR, parser or language-server call;
* no graph refresh or write;
* no Ocelot import or execution;
* no free-text wording, rationale, excerpt or URL is returned, only bounded
  hashes and allowlisted labels.

Source freshness is checked only when a Source records a SHA-256 *and* a path
inside an explicitly selected research root.  A remote source, an unselected
path, a missing file or a path outside its root stays ``unknown``/``absent``;
it is never fetched to establish freshness.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ozagia.config import state_path
from ozagia.core import contracts as C
from ozagia.research import holdings
from ozagia.research.graph import (
    ResearchError,
    ResearchGraph,
    SUPPORTED_SCHEMA_VERSIONS,
)

SCHEMA_NAMESPACE = "ozagia.research_projection"
SCHEMA_VERSION = "1.0"
PROJECTION_VERSION = "1.0"

MAX_SESSIONS = 8
MAX_CLAIMS = 50
MAX_SOURCES = 50
MAX_ASSUMPTIONS = 30
MAX_EVIDENCE = 50
MAX_HOLDINGS = 8
MAX_IDS = 16
MAX_FACTS = 48
MAX_SESSION_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_REPORT_BYTES = 96 * 1024
MAX_JOIN_BYTES = 56 * 1024
MAX_TEXT = 512
MAX_REASON = 300
MAX_PATH = 512

SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")

DEFERRED = (
    "live source fetch",
    "network access",
    "model calls",
    "OCR and document parsers",
    "Ocelot adapter",
    "research graph writes",
)

NOT_ASSESSED = (
    "a recorded claim or assumption is a declaration; no truth assessment was "
    "performed",
    "a recorded supports/opposes/derives link is authored provenance, not "
    "observed evidence",
    "source freshness is checked only for a local selected file with a "
    "recorded SHA-256",
    "remote sources were not fetched",
    "model candidates and extracted formulas were not executed",
    "OCR and document parsers were not run",
    "the research graph was not modified or refreshed",
)

OCELOT = {
    "state": "unknown",
    "reason": (
        "the Ocelot adapter is deferred in this read-only slice; no Ocelot "
        "module was imported or executed"
    ),
}

_SECRET_PATTERNS = (
    re.compile(r"(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
)

_CLAIM_STATUSES = frozenset(
    {"open", "supported_within_scope", "contradicted", "superseded", "unresolved"}
)


class ResearchProjectionError(ValueError):
    """A malformed projection request with a stable service error code."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        self.code = code if code in C.SERVICE_ERROR_CODES else "invalid_request"
        super().__init__(_line(message))


# ---------------------------------------------------------------------------
# Bounded helpers
# ---------------------------------------------------------------------------


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _line(value: Any, limit: int = MAX_REASON) -> str:
    return _redact(" ".join(str(value).split()))[:limit]


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    text = "".join(" " if ord(char) < 0x20 else char for char in text)
    return _redact(text)[:limit]


def _path_text(value: Any) -> str:
    return _text(value, MAX_PATH)


def _sha(value: Any) -> str | None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        return None
    return value.lower()


def _text_sha(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _truth(value: Any) -> bool:
    return value is True


def _ids(values: Iterable[Any], limit: int = MAX_IDS) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def _mtime(path: Path) -> int | str:
    try:
        return int(path.stat().st_mtime_ns) // 1_000_000
    except OSError:
        return "unknown"


def _observed_at(value: Any) -> int | str:
    if value is None:
        return int(time.time() * 1000)
    if isinstance(value, bool):
        raise ResearchProjectionError("observed_at must not be bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and 0 < len(value) <= 64:
        return value
    raise ResearchProjectionError(
        "observed_at must be an integer or a short string")


def _truncation(omitted: int, *, reason: str = "limit") -> C.Truncation:
    if omitted <= 0:
        return C.truncation()
    return C.truncation(True, omitted=omitted, reason=reason)


# ---------------------------------------------------------------------------
# Session loading
# ---------------------------------------------------------------------------


def _session_candidates(root: Path, session_id: str) -> list[tuple[Path, str]]:
    return [
        (state_path(root, "research", f"{session_id}.json"), "state"),
        (root / "var" / "ocelot_research" / f"{session_id}.json", "legacy"),
    ]


def _discover_sessions(root: Path) -> tuple[list[str], list[str]]:
    directory = state_path(root, "research")
    try:
        entries = sorted(directory.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"cannot list research state: {type(exc).__name__}"]
    found: list[str] = []
    omissions: list[str] = []
    for entry in entries:
        if entry.suffix != ".json" or entry.name.startswith("."):
            continue
        if entry.is_symlink():
            omissions.append(f"symlinked research session skipped: {entry.name}")
            continue
        if not entry.is_file():
            continue
        session_id = entry.stem
        if not SESSION_ID_RE.fullmatch(session_id):
            omissions.append(f"invalid research session filename skipped: {entry.name}")
            continue
        found.append(session_id)
        if len(found) >= MAX_SESSIONS:
            if len(entries) > len(found):
                omissions.append("research session discovery reached its bound")
            break
    return found, omissions


def _check_sessions(sessions: Iterable[str] | None,
                    root: Path) -> tuple[list[str], list[str], str]:
    if sessions is None:
        found, omissions = _discover_sessions(root)
        return found, omissions, "discovered"
    if isinstance(sessions, (str, bytes)):
        raise ResearchProjectionError(
            "sessions must be an iterable of ids, not a string")
    items = list(sessions)
    if len(items) > MAX_SESSIONS:
        raise ResearchProjectionError(
            f"at most {MAX_SESSIONS} research sessions are allowed")
    checked: list[str] = []
    for item in items:
        if not isinstance(item, str) or not SESSION_ID_RE.fullmatch(item):
            raise ResearchProjectionError(
                "research session ids must match [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
        if item not in checked:
            checked.append(item)
    return sorted(checked), [], "explicit"


def _read_session_file(path: Path
                       ) -> tuple[str | None, int | None, int | str, str | None]:
    try:
        info = path.stat()
    except PermissionError:
        return None, None, "unknown", "unauthorized"
    except OSError:
        return None, None, "unknown", "unreadable"
    if info.st_size > MAX_SESSION_BYTES:
        return None, int(info.st_size), _mtime(path), "too_large"
    try:
        raw = path.read_bytes()
    except PermissionError:
        return None, int(info.st_size), _mtime(path), "unauthorized"
    except OSError:
        return None, int(info.st_size), _mtime(path), "unreadable"
    return hashlib.sha256(raw).hexdigest(), len(raw), _mtime(path), None


def _load_session(root: Path, session_id: str) -> dict[str, Any]:
    candidates = _session_candidates(root, session_id)
    selected: tuple[Path, str] | None = None
    for path, storage in candidates:
        if path.is_file():
            selected = (path, storage)
            break
    if selected is None:
        return {
            "id": session_id,
            "state": "absent",
            "storage": "none",
            "path": candidates[0][0].as_posix(),
            "freshness": "absent",
            "evidence": "inferred",
            "reason": "the research session is not on disk",
            "observed_at": "unknown",
        }
    path, storage = selected
    if path.is_symlink():
        return {
            "id": session_id,
            "state": "invalid",
            "storage": storage,
            "path": path.as_posix(),
            "freshness": "unknown",
            "evidence": "inferred",
            "reason": "a symlinked research session is not opened",
            "observed_at": _mtime(path),
        }
    file_sha, size, observed_at, read_error = _read_session_file(path)
    if file_sha is None:
        if read_error == "unauthorized":
            return {
                "id": session_id,
                "state": "unauthorized",
                "storage": storage,
                "path": path.as_posix(),
                "file_sha256": None,
                "bytes": size,
                "freshness": "absent",
                "evidence": "inferred",
                "reason": "the research session is not readable",
                "observed_at": observed_at,
            }
        reason = ("the research session exceeds the size bound"
                  if read_error == "too_large"
                  else "the research session file could not be read")
        return {
            "id": session_id,
            "state": "invalid",
            "storage": storage,
            "path": path.as_posix(),
            "file_sha256": None,
            "bytes": size,
            "freshness": "unknown",
            "evidence": "inferred",
            "reason": reason,
            "observed_at": observed_at,
        }
    try:
        graph = ResearchGraph.load(session_id, root=root)
        state = graph.state()
    except PermissionError:
        return {
            "id": session_id, "state": "unauthorized", "storage": storage,
            "path": path.as_posix(), "file_sha256": file_sha, "bytes": size,
            "freshness": "absent", "evidence": "inferred",
            "reason": "the research session is not readable",
            "observed_at": observed_at,
        }
    except (ResearchError, OSError, ValueError, TypeError,
            AttributeError, KeyError) as exc:
        return {
            "id": session_id, "state": "invalid", "storage": storage,
            "path": path.as_posix(), "file_sha256": file_sha, "bytes": size,
            "freshness": "unknown", "evidence": "inferred",
            "reason": _line(f"cannot load the research session: "
                            f"{type(exc).__name__}: {exc}"),
            "observed_at": observed_at,
        }
    version = state.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        return {
            "id": session_id, "state": "invalid", "storage": storage,
            "path": path.as_posix(), "file_sha256": file_sha, "bytes": size,
            "freshness": "unknown", "evidence": "inferred",
            "reason": f"unsupported research graph schema {version!r}",
            "observed_at": observed_at,
        }
    return {
        "id": session_id,
        "state": "present",
        "storage": storage,
        "path": path.as_posix(),
        "file_sha256": file_sha,
        "bytes": size,
        "schema_version": version,
        "created_at": _text(state.get("created_at"), 64) or None,
        "mode": _text(state.get("mode"), 64) or None,
        "freshness": "current",
        "evidence": "observed",
        "reason": None,
        "observed_at": observed_at,
        "_state": state,
    }


# ---------------------------------------------------------------------------
# Graph projection
# ---------------------------------------------------------------------------


def _nodes(state: Mapping[str, Any], node_type: str) -> list[dict[str, Any]]:
    values = [
        dict(node) for node in state.get("nodes", [])
        if isinstance(node, Mapping) and node.get("type") == node_type
        and isinstance(node.get("id"), str)
    ]
    values.sort(key=lambda node: str(node["id"]))
    return values


def _edges(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [edge for edge in state.get("edges", []) if isinstance(edge, Mapping)]


def _cites(edges: Sequence[Mapping[str, Any]], source_id: str) -> list[str]:
    out: list[str] = []
    for edge in edges:
        if edge.get("type") != "cites" or edge.get("src") != source_id:
            continue
        target = edge.get("tgt")
        if isinstance(target, str) and target not in out:
            out.append(target)
    return out


def _edge_targets(edges: Sequence[Mapping[str, Any]], source_id: str,
                  relation: str) -> list[str]:
    out: list[str] = []
    for edge in edges:
        if edge.get("type") != relation or edge.get("src") != source_id:
            continue
        target = edge.get("tgt")
        if isinstance(target, str) and target not in out:
            out.append(target)
    return out


def _edge_sources(edges: Sequence[Mapping[str, Any]], target_id: str,
                  relation: str) -> list[str]:
    out: list[str] = []
    for edge in edges:
        if edge.get("type") != relation or edge.get("tgt") != target_id:
            continue
        source = edge.get("src")
        if isinstance(source, str) and source not in out:
            out.append(source)
    return out


def _locator_fields(node: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key in ("locator", "source_locator"):
        raw = node.get(key)
        if isinstance(raw, Mapping):
            values.update(raw)
        elif isinstance(raw, str) and raw.strip():
            values.setdefault("path", raw.strip())
    for key in ("root_id", "path", "sha256", "revision", "dirty", "kind",
                "url", "checksum", "repository"):
        if key in node and key not in values:
            values[key] = node[key]
    return values


def _selected_files(root: Mapping[str, Any]) -> set[str] | None:
    files = root.get("files")
    if isinstance(files, Mapping):
        return {str(key) for key in files}
    if isinstance(files, Sequence) and not isinstance(files, (str, bytes)):
        return {str(item) for item in files if isinstance(item, str)}
    return None


def _verify_source(state: Mapping[str, Any], locator: Mapping[str, Any],
                   recorded: str | None, *, projection_root: Path) -> dict[str, Any]:
    if recorded is None:
        return {
            "hash_state": "not_recorded",
            "freshness": "unknown",
            "evidence": "declared",
            "reason": "the Source records no valid SHA-256",
        }
    raw_path = locator.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return {
            "hash_state": "unverified",
            "freshness": "unknown",
            "evidence": "declared",
            "reason": ("the Source has no local path; remote content was not "
                       "fetched"),
        }
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        return {
            "hash_state": "unverified",
            "freshness": "unknown",
            "evidence": "declared",
            "reason": "the recorded source path is not a safe relative path",
        }
    root_id = locator.get("root_id")
    root = next(
        (item for item in state.get("roots", [])
         if isinstance(item, Mapping) and item.get("root_id") == root_id),
        None,
    )
    if root is None or not isinstance(root.get("path"), str):
        return {
            "hash_state": "unverified",
            "freshness": "unknown",
            "evidence": "declared",
            "reason": "the Source has no configured research root to check",
        }
    selected = _selected_files(root)
    if selected is None:
        return {
            "hash_state": "unverified",
            "freshness": "unknown",
            "evidence": "declared",
            "reason": "the research root records no selected files",
        }
    if raw_path not in selected:
        return {
            "hash_state": "unverified",
            "freshness": "unknown",
            "evidence": "declared",
            "reason": "the source path is not in the selected root manifest",
        }
    base = Path(root["path"]).expanduser()
    if not base.is_absolute():
        base = projection_root / base
    try:
        resolved_root = base.resolve(strict=True)
        candidate = (resolved_root / relative).resolve(strict=True)
        if not candidate.is_relative_to(resolved_root):
            return {
                "hash_state": "unverified",
                "freshness": "unknown",
                "evidence": "declared",
                "reason": "the source path escapes its configured root",
            }
        if not candidate.is_file():
            return {
                "hash_state": "unverified",
                "freshness": "unknown",
                "evidence": "declared",
                "reason": "the recorded source is not a regular file",
            }
        size = candidate.stat().st_size
        if size > MAX_SOURCE_BYTES:
            return {
                "hash_state": "unverified",
                "freshness": "unknown",
                "evidence": "declared",
                "reason": "the source exceeds the verification size bound",
            }
        with candidate.open("rb") as stream:
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        if len(raw) > MAX_SOURCE_BYTES:
            return {
                "hash_state": "unverified",
                "freshness": "unknown",
                "evidence": "declared",
                "reason": "the source exceeds the verification size bound",
            }
        digest = hashlib.sha256(raw).hexdigest()
    except FileNotFoundError:
        return {
            "hash_state": "absent",
            "freshness": "absent",
            "evidence": "observed",
            "reason": "the recorded source is not on disk",
            "observed_at": "unknown",
        }
    except (OSError, ValueError):
        return {
            "hash_state": "unverified",
            "freshness": "unknown",
            "evidence": "declared",
            "reason": "the recorded source could not be checked",
        }
    if digest == recorded:
        return {
            "hash_state": "match",
            "freshness": "current",
            "evidence": "observed",
            "reason": "the recorded source hash matches the selected file",
            "observed_sha256": digest,
        }
    return {
        "hash_state": "mismatch",
        "freshness": "stale",
        "evidence": "observed",
        "reason": "the recorded source hash differs from the selected file",
        "observed_sha256": digest,
    }


def _holding_view(holding: Mapping[str, Any]) -> dict[str, Any]:
    constraints = _ids(holding.get("use_constraints") or [], 8)
    confidence = holding.get("confidence")
    confidence_pct = None
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        confidence_pct = max(0, min(100, int(round(float(confidence) * 100))))
    return {
        "id": str(holding.get("id")),
        "source_type": _text(holding.get("source_type"), 64) or "unknown",
        "epistemic_role": _text(holding.get("epistemic_role"), 64) or "unknown",
        "authority": _text(holding.get("authority"), 64) or "unknown",
        "review_status": _text(holding.get("review_status"), 64) or "unknown",
        "status": _text(holding.get("status"), 32) or "unknown",
        "use_constraints": [_text(item, 120) for item in constraints],
        "has_scope": bool(holding.get("scope")),
        "has_rationale": bool(holding.get("rationale")),
        "reviewer": _text(holding.get("reviewer"), 120) or None,
        "confidence_pct": confidence_pct,
        "recorded_truth_assessed": _truth(holding.get("truth_assessed")),
        "truth_assessed": False,
    }


def _source_holdings(state: Mapping[str, Any], source_id: str) -> list[dict[str, Any]]:
    by_id = {
        str(node.get("id")): node for node in state.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    }
    ids: list[str] = []
    for node in _nodes(state, "Holding"):
        if node.get("source_id") == source_id:
            ids.append(str(node["id"]))
    for edge in _edges(state):
        if edge.get("type") == "held_as" and edge.get("src") == source_id:
            target = edge.get("tgt")
            if isinstance(target, str) and target not in ids:
                ids.append(target)
    rows = []
    for holding_id in sorted(ids)[:MAX_HOLDINGS]:
        holding = by_id.get(holding_id)
        if holding is not None and holding.get("type") == "Holding":
            view = _holding_view(holding)
            view["source_id"] = source_id
            rows.append(view)
    return rows


def _defaults_view(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "source_id": value.get("source_id"),
        "source_type": _text(value.get("source_type"), 64) or None,
        "epistemic_role": _text(value.get("epistemic_role"), 64) or None,
        "authority": _text(value.get("authority"), 64) or None,
        "review_status": _text(value.get("review_status"), 64) or None,
        "use_constraints": [_text(item, 120)
                            for item in _ids(value.get("use_constraints") or [], 8)],
        "state": "defaults_only",
        "truth_assessed": False,
    }


def _claim_trust(state: Mapping[str, Any], claim_id: str,
                 source_ids: Sequence[str]) -> dict[str, Any]:
    try:
        context = holdings.claim_holding(state, claim_id)
    except Exception as exc:  # noqa: BLE001 - provenance stays explicit
        return {
            "state": "unknown",
            "reason": _line(f"claim holdings are unavailable: "
                            f"{type(exc).__name__}"),
            "truth_assessed": False,
        }
    contexts = []
    for item in context.get("contexts") or []:
        if not isinstance(item, Mapping):
            continue
        holding = item.get("holding")
        if not isinstance(holding, Mapping):
            continue
        view = _holding_view(holding)
        source = item.get("source")
        view["source_id"] = (source.get("id") if isinstance(source, Mapping)
                             else item.get("source_id"))
        checks = item.get("checks") or {}
        view["issue_count"] = len(checks.get("issues") or [])
        view["warning_count"] = len(checks.get("warnings") or [])
        contexts.append(view)
        if len(contexts) >= MAX_HOLDINGS:
            break
    unheld = []
    for item in context.get("unheld_sources") or []:
        if not isinstance(item, Mapping):
            continue
        unheld.append({
            "source_id": item.get("source_id"),
            "source_type": _text(item.get("source_type"), 64) or None,
            "state": "unheld",
            "defaults": _defaults_view(item.get("defaults")),
        })
        if len(unheld) >= MAX_HOLDINGS:
            break
    warning_codes: dict[str, int] = {}
    for item in context.get("warnings") or []:
        if isinstance(item, Mapping):
            code = _text(item.get("code"), 80) or "unknown"
            warning_codes[code] = warning_codes.get(code, 0) + 1
    issue_codes: dict[str, int] = {}
    for item in context.get("issues") or []:
        if isinstance(item, Mapping):
            code = _text(item.get("code"), 80) or "unknown"
            issue_codes[code] = issue_codes.get(code, 0) + 1
    default = _defaults_view(context.get("default"))
    return {
        "state": ("recorded" if contexts else
                  "unheld" if source_ids else "no_source"),
        "holdings": contexts,
        "unheld_sources": unheld,
        "default": default,
        "warnings": dict(sorted(warning_codes.items())),
        "issues": dict(sorted(issue_codes.items())),
        "truth_assessed": False,
    }


def _url_view(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return {"present": True, "valid": False}
    return {
        "present": True,
        "valid": bool(parts.scheme and parts.hostname),
        "scheme": _text(parts.scheme, 16) or None,
        "host": _text(parts.hostname, 128) or None,
        "url_sha256": hashlib.sha256(value.encode("utf-8", "replace")).hexdigest(),
    }


def _source_view(state: Mapping[str, Any], node: Mapping[str, Any], *,
                 edges: Sequence[Mapping[str, Any]], observed_at: int | str,
                 projection_root: Path, verify_sources: bool) -> dict[str, Any]:
    source_id = str(node["id"])
    locator = _locator_fields(node)
    recorded = _sha(locator.get("sha256")) or _sha(locator.get("checksum"))
    verification = (
        _verify_source(state, locator, recorded, projection_root=projection_root)
        if verify_sources else {
            "hash_state": "unverified",
            "freshness": "unknown",
            "evidence": "declared",
            "reason": "source verification was disabled by the caller",
        }
    )
    reference_ids = _edge_targets(edges, source_id, "manifestation_of")
    return {
        "session": None,
        "id": source_id,
        "source_type": _text(node.get("source_type"), 64) or "unknown",
        "kind": _text(locator.get("kind"), 64) or None,
        "root_id": _text(locator.get("root_id"), 128) or None,
        "repository": _text(locator.get("repository"), 128) or None,
        "path": _path_text(locator.get("path")) or None,
        "url": _url_view(locator.get("url")),
        "recorded_sha256": recorded,
        "observed_sha256": verification.get("observed_sha256"),
        "hash_state": verification["hash_state"],
        "freshness": verification["freshness"],
        "evidence": verification["evidence"],
        "reason": verification["reason"],
        "observed_at": verification.get("observed_at", observed_at),
        "revision": _text(locator.get("revision"), 80) or None,
        "dirty": bool(locator.get("dirty")) if "dirty" in locator else None,
        "reference_ids": _ids(reference_ids),
        "holdings": _source_holdings(state, source_id),
        "truth_assessed": False,
    }


def _claim_view(state: Mapping[str, Any], node: Mapping[str, Any], *,
                edges: Sequence[Mapping[str, Any]], session_freshness: str,
                source_freshness: Mapping[str, str],
                observed_at: int | str) -> dict[str, Any]:
    claim_id = str(node["id"])
    by_type = {
        str(item.get("id")): str(item.get("type"))
        for item in state.get("nodes", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    cited = _cites(edges, claim_id)
    source_ids = sorted(item for item in cited if by_type.get(item) == "Source")
    reference_ids = sorted(item for item in cited if by_type.get(item) == "Reference")
    evidence_ids = sorted(item for item in cited if by_type.get(item) == "Evidence")
    assumption_ids = _edge_targets(edges, claim_id, "assumes")
    derived_ids = _edge_targets(edges, claim_id, "derived_from")
    support_sources = _edge_sources(edges, claim_id, "supports")
    oppose_sources = _edge_sources(edges, claim_id, "opposes")
    relation_counts: dict[str, int] = {}
    for edge in edges:
        if edge.get("src") != claim_id and edge.get("tgt") != claim_id:
            continue
        relation = _text(edge.get("type"), 64) or "unknown"
        relation_counts[relation] = relation_counts.get(relation, 0) + 1
    freshness = session_freshness
    for source_id in source_ids:
        freshness = C.join_freshness(
            freshness, source_freshness.get(source_id, "unknown"))
    if not source_ids:
        freshness = C.join_freshness(freshness, "unknown")
    wording = node.get("wording")
    citations = node.get("citations")
    return {
        "session": None,
        "id": claim_id,
        "claim_type": _text(node.get("claim_type"), 64) or "unknown",
        "status": (_text(node.get("status"), 64)
                   if node.get("status") in _CLAIM_STATUSES else "unknown"),
        "model_candidate": _truth(node.get("model_candidate")),
        "parsed_assertion": _truth(node.get("parsed_assertion")),
        "semantics_verified": _truth(node.get("semantics_verified")),
        "has_wording": isinstance(wording, str) and bool(wording),
        "wording_sha256": _text_sha(wording),
        "wording_chars": len(wording) if isinstance(wording, str) else 0,
        "citation_count": len(citations) if isinstance(citations, list) else 0,
        "source_ids": _ids(source_ids),
        "reference_ids": _ids(reference_ids),
        "evidence_ids": _ids(evidence_ids),
        "assumption_ids": _ids(assumption_ids),
        "derived_from_ids": _ids(derived_ids),
        "support_edge_count": len(support_sources),
        "oppose_edge_count": len(oppose_sources),
        "relation_counts": dict(sorted(relation_counts.items())),
        "source_freshness": {
            source_id: source_freshness.get(source_id, "unknown")
            for source_id in _ids(source_ids, 8)
        },
        "support_state": ("declared_links_only"
                          if relation_counts else "unlinked"),
        "evidence": "declared",
        "freshness": freshness,
        "observed_at": observed_at,
        "trust": _claim_trust(state, claim_id, source_ids),
        "truth_assessed": False,
    }


def _assumption_view(node: Mapping[str, Any], *,
                     edges: Sequence[Mapping[str, Any]],
                     session_freshness: str,
                     observed_at: int | str) -> dict[str, Any]:
    assumption_id = str(node["id"])
    wording = node.get("wording")
    return {
        "session": None,
        "id": assumption_id,
        "status": _text(node.get("status"), 64) or "unknown",
        "scope": _text(node.get("scope"), 120) or None,
        "has_wording": isinstance(wording, str) and bool(wording),
        "wording_sha256": _text_sha(wording),
        "claim_ids": _ids(_edge_sources(edges, assumption_id, "assumes")),
        "evidence": "declared",
        "freshness": session_freshness,
        "observed_at": observed_at,
        "truth_assessed": False,
    }


def _evidence_view(state: Mapping[str, Any], node: Mapping[str, Any], *,
                   edges: Sequence[Mapping[str, Any]],
                   session_freshness: str,
                   source_freshness: Mapping[str, str],
                   observed_at: int | str) -> dict[str, Any]:
    evidence_id = str(node["id"])
    by_type = {
        str(item.get("id")): str(item.get("type"))
        for item in state.get("nodes", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    cited = _cites(edges, evidence_id)
    source_ids = sorted(item for item in cited if by_type.get(item) == "Source")
    freshness = session_freshness
    for source_id in source_ids:
        freshness = C.join_freshness(
            freshness, source_freshness.get(source_id, "unknown"))
    if not source_ids:
        freshness = C.join_freshness(freshness, "unknown")
    wording = node.get("wording")
    return {
        "session": None,
        "id": evidence_id,
        "evidence_type": _text(node.get("evidence_type"), 64) or "unknown",
        "status": _text(node.get("status"), 64) or "unknown",
        "has_wording": isinstance(wording, str) and bool(wording),
        "wording_sha256": _text_sha(wording),
        "source_ids": _ids(source_ids),
        "supports_ids": _ids(_edge_targets(edges, evidence_id, "supports")),
        "opposes_ids": _ids(_edge_targets(edges, evidence_id, "opposes")),
        "evidence": "declared",
        "freshness": freshness,
        "observed_at": observed_at,
        "truth_assessed": False,
    }


def _project_session(row: Mapping[str, Any], *, projection_root: Path,
                     verify_sources: bool, omitted: list[str]
                     ) -> dict[str, Any] | None:
    state = row.get("_state")
    if not isinstance(state, Mapping):
        return None
    session_id = str(row["id"])
    observed_at = row.get("observed_at", "unknown")
    session_freshness = str(row.get("freshness") or "unknown")
    edges = _edges(state)
    source_nodes = _nodes(state, "Source")
    omitted.extend(f"{session_id}: source {node['id']} omitted"
                   for node in source_nodes[MAX_SOURCES:])
    source_rows = [
        _source_view(state, node, edges=edges, observed_at=observed_at,
                     projection_root=projection_root,
                     verify_sources=verify_sources)
        for node in source_nodes[:MAX_SOURCES]
    ]
    source_freshness = {
        str(item["id"]): str(item["freshness"]) for item in source_rows
    }
    for item in source_rows:
        item["session"] = session_id
    claim_nodes = _nodes(state, "Claim")
    omitted.extend(f"{session_id}: claim {node['id']} omitted"
                   for node in claim_nodes[MAX_CLAIMS:])
    claim_rows = [
        _claim_view(state, node, edges=edges,
                    session_freshness=session_freshness,
                    source_freshness=source_freshness,
                    observed_at=observed_at)
        for node in claim_nodes[:MAX_CLAIMS]
    ]
    for item in claim_rows:
        item["session"] = session_id
    assumption_nodes = _nodes(state, "Assumption")
    omitted.extend(f"{session_id}: assumption {node['id']} omitted"
                   for node in assumption_nodes[MAX_ASSUMPTIONS:])
    assumption_rows = [
        _assumption_view(node, edges=edges,
                         session_freshness=session_freshness,
                         observed_at=observed_at)
        for node in assumption_nodes[:MAX_ASSUMPTIONS]
    ]
    for item in assumption_rows:
        item["session"] = session_id
    evidence_nodes = _nodes(state, "Evidence")
    omitted.extend(f"{session_id}: evidence {node['id']} omitted"
                   for node in evidence_nodes[MAX_EVIDENCE:])
    evidence_rows = [
        _evidence_view(state, node, edges=edges,
                       session_freshness=session_freshness,
                       source_freshness=source_freshness,
                       observed_at=observed_at)
        for node in evidence_nodes[:MAX_EVIDENCE]
    ]
    for item in evidence_rows:
        item["session"] = session_id
    return {
        "session": row,
        "sources": source_rows,
        "claims": claim_rows,
        "assumptions": assumption_rows,
        "evidence": evidence_rows,
        "counts": {
            "nodes": len(state.get("nodes") or []),
            "edges": len(state.get("edges") or []),
            "events": len(state.get("events") or []),
            "claims": len(claim_nodes),
            "sources": len(source_nodes),
            "assumptions": len(assumption_nodes),
            "evidence": len(evidence_nodes),
            "holdings": len(_nodes(state, "Holding")),
            "omitted_claims": max(0, len(claim_nodes) - MAX_CLAIMS),
            "omitted_sources": max(0, len(source_nodes) - MAX_SOURCES),
        },
    }


# ---------------------------------------------------------------------------
# Public projection
# ---------------------------------------------------------------------------


def build_projection(root: str | Path, *, sessions: Iterable[str] | None = None,
                     observed_at: int | str | None = None,
                     verify_sources: bool = True) -> dict[str, Any]:
    """Project retained research records without modifying them.

    ``sessions=None`` discovers bounded sessions under the state directory;
    an explicit sequence (including an empty one) selects only those ids.
    ``verify_sources`` only hashes local files inside an explicitly selected
    research root.  Nothing is fetched.
    """
    base = Path(root).expanduser()
    try:
        resolved = base.resolve(strict=True)
    except OSError as exc:
        raise ResearchProjectionError(
            f"research projection root does not exist: {base}",
            code="not_found") from exc
    if not resolved.is_dir():
        raise ResearchProjectionError(
            f"research projection root is not a directory: {resolved}")
    observed = _observed_at(observed_at)
    selected, discovery_omissions, selection = _check_sessions(sessions, resolved)
    omissions = list(discovery_omissions)
    session_rows: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    assumptions: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    totals = {
        "sessions": len(selected), "present": 0, "absent": 0, "invalid": 0,
        "unauthorized": 0, "claims": 0, "sources": 0, "assumptions": 0,
        "evidence": 0, "holdings": 0,
    }
    for session_id in selected:
        loaded = _load_session(resolved, session_id)
        files.append({
            "id": session_id,
            "state": loaded["state"],
            "path": loaded["path"],
            "sha256": loaded.get("file_sha256"),
            "bytes": loaded.get("bytes"),
            "observed_at": loaded.get("observed_at"),
        })
        totals[loaded["state"]] = totals.get(loaded["state"], 0) + 1
        projected = _project_session(
            loaded, projection_root=resolved, verify_sources=verify_sources,
            omitted=omissions)
        if projected is None:
            session_rows.append(loaded)
            continue
        session = projected["session"]
        session["counts"] = projected["counts"]
        session.pop("_state", None)
        session_rows.append(session)
        claims.extend(projected["claims"])
        sources.extend(projected["sources"])
        assumptions.extend(projected["assumptions"])
        evidence.extend(projected["evidence"])
        totals["claims"] += projected["counts"]["claims"]
        totals["sources"] += projected["counts"]["sources"]
        totals["assumptions"] += projected["counts"]["assumptions"]
        totals["evidence"] += projected["counts"]["evidence"]
        totals["holdings"] += projected["counts"]["holdings"]
    counts = {
        "sessions": len(session_rows),
        "claims": len(claims),
        "sources": len(sources),
        "assumptions": len(assumptions),
        "evidence": len(evidence),
        "totals": dict(sorted(totals.items())),
    }
    report: dict[str, Any] = {
        "schema": {"namespace": SCHEMA_NAMESPACE, "version": SCHEMA_VERSION},
        "projection_version": PROJECTION_VERSION,
        "root": resolved.as_posix(),
        "mode": "read_only",
        "selection": selection,
        "state_dir": state_path(resolved, "research").as_posix(),
        "sessions": session_rows,
        "claims": claims,
        "sources": sources,
        "assumptions": assumptions,
        "evidence": evidence,
        "counts": counts,
        "provenance": {
            "reader": "ozagia.core.research_projection",
            "observed_at": observed,
            "state_dir": state_path(resolved, "research").as_posix(),
            "files": files,
            "selection": selection,
        },
        "engines": {"ocelot": dict(OCELOT)},
        "not_assessed": list(NOT_ASSESSED),
        "deferred": list(DEFERRED),
        "omissions": sorted(omissions),
        "truncated": _truncation(len(omissions)).to_dict(),
        "truth_assessed": False,
    }
    report = _enforce_bytes(report)
    report["hash"] = projection_hash(report)
    return report


def _enforce_bytes(report: dict[str, Any]) -> dict[str, Any]:
    """Trim detail lists deterministically; readiness is not recomputed."""
    def too_big() -> bool:
        return len(C.canonical_json(report).encode("utf-8")) > MAX_REPORT_BYTES

    if not too_big():
        report["counts"]["report_bytes"] = len(
            C.canonical_json(report).encode("utf-8"))
        return report
    dropped = 0
    for key in ("evidence", "assumptions", "sources", "claims"):
        values = report.get(key)
        while isinstance(values, list) and values and too_big():
            values.pop()
            dropped += 1
    if dropped:
        report["omissions"] = sorted(set(
            list(report.get("omissions") or [])
            + [f"{dropped} projected research records omitted by bytes"]))
        report["truncated"] = _truncation(
            int(report["truncated"].get("omitted") or 0) + dropped,
            reason="bytes").to_dict()
    report["counts"]["report_bytes"] = len(
        C.canonical_json(report).encode("utf-8"))
    report["counts"]["bytes_truncated"] = dropped > 0
    return report


def projection_hash(report: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical projection without its ``hash`` field."""
    return C.content_hash({key: value for key, value in report.items()
                           if key != "hash"})


def projection_json(report: Mapping[str, Any]) -> str:
    return C.canonical_json(dict(report))


def projection_readiness(report: Mapping[str, Any]) -> tuple[bool | str, str]:
    """Readiness of the *projection*, never of the claims it describes.

    A readable session with no claim is current.  Recorded claims, assumptions
    or evidence are declarations, so the projection is ``unknown`` for
    assessment purposes; only an unreadable/unauthorized session is a definite
    negative.
    """
    sessions = report.get("sessions") or []
    if not sessions:
        return "unknown", "no research session was selected or discovered"
    if any(row.get("state") == "unauthorized" for row in sessions):
        return False, "a selected research session is not readable"
    unreadable = [row for row in sessions if row.get("state") != "present"]
    if unreadable:
        return "unknown", (
            f"{len(unreadable)} selected research session(s) are absent or "
            "invalid")
    if any(row.get("freshness") != "current" for row in sessions):
        return "unknown", "a research session record is not current"
    declared = sum(int((report.get("counts") or {}).get(key) or 0)
                   for key in ("claims", "assumptions", "evidence"))
    if declared:
        return "unknown", (
            f"{declared} recorded claim/assumption/evidence record(s) are "
            "declarations; none was verified")
    stale_sources = [
        row for row in report.get("sources") or []
        if row.get("freshness") != "current"
    ]
    if stale_sources:
        return "unknown", (
            f"{len(stale_sources)} recorded source(s) are stale, absent or "
            "unverified")
    return True, "the selected research sessions are readable and assert no claim"


def _holding_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "source_id": row.get("source_id"),
        "source_type": row.get("source_type"),
        "epistemic_role": row.get("epistemic_role"),
        "authority": row.get("authority"),
        "review_status": row.get("review_status"),
        "status": row.get("status"),
        "truth_assessed": False,
    }


def _trust_summary(trust: Mapping[str, Any] | None) -> dict[str, Any]:
    value = trust if isinstance(trust, Mapping) else {}
    return {
        "state": value.get("state", "unknown"),
        "holdings": [
            _holding_summary(row) for row in value.get("holdings") or []
            if isinstance(row, Mapping)
        ],
        "unheld_source_ids": [
            row.get("source_id") for row in value.get("unheld_sources") or []
            if isinstance(row, Mapping)
        ],
        "default": (dict(value["default"])
                    if isinstance(value.get("default"), Mapping) else None),
        "warnings": dict(value.get("warnings") or {}),
        "issues": dict(value.get("issues") or {}),
        "truth_assessed": False,
    }


def preflight_join(report: Mapping[str, Any], *, max_claims: int = 12,
                   max_sources: int = 12, max_assumptions: int = 8,
                   max_evidence: int = 8) -> dict[str, Any]:
    """A compact, deterministic view for the task preflight."""
    claims = list(report.get("claims") or [])
    sources = list(report.get("sources") or [])
    assumptions = list(report.get("assumptions") or [])
    evidence = list(report.get("evidence") or [])
    omitted = (
        max(0, len(claims) - max_claims)
        + max(0, len(sources) - max_sources)
        + max(0, len(assumptions) - max_assumptions)
        + max(0, len(evidence) - max_evidence)
    )
    readiness, reason = projection_readiness(report)
    report_truncated = dict(report.get("truncated") or C.truncation().to_dict())
    if omitted:
        report_truncated = C.truncation(
            True,
            omitted=int(report_truncated.get("omitted") or 0) + omitted,
            reason=report_truncated.get("reason") or "limit",
        ).to_dict()
    result = {
        "schema": {
            "namespace": "ozagia.task_preflight.research",
            "version": "1.0",
        },
        "projection": {
            "namespace": report.get("schema", {}).get("namespace"),
            "version": report.get("schema", {}).get("version"),
            "hash": report.get("hash"),
            "selection": report.get("selection"),
        },
        "readiness": readiness,
        "reason": reason,
        "sessions": [
            {key: row.get(key) for key in (
                "id", "state", "storage", "freshness", "evidence", "reason",
                "file_sha256", "schema_version", "counts")}
            for row in report.get("sessions") or []
        ],
        "claims": [
            {
                **{key: row.get(key) for key in (
                    "session", "id", "claim_type", "status", "model_candidate",
                    "has_wording", "wording_sha256", "source_ids",
                    "assumption_ids", "evidence_ids", "support_state",
                    "evidence", "freshness", "observed_at",
                    "truth_assessed")},
                "trust": _trust_summary(row.get("trust")),
            }
            for row in claims[:max_claims]
        ],
        "sources": [
            {
                **{key: row.get(key) for key in (
                    "session", "id", "source_type", "kind", "root_id", "path",
                    "url", "recorded_sha256", "observed_sha256", "hash_state",
                    "freshness", "evidence", "reason", "observed_at",
                    "reference_ids", "truth_assessed")},
                "holdings": [
                    _holding_summary(item)
                    for item in row.get("holdings") or []
                    if isinstance(item, Mapping)
                ],
            }
            for row in sources[:max_sources]
        ],
        "assumptions": [
            {key: row.get(key) for key in (
                "session", "id", "status", "scope", "has_wording",
                "wording_sha256", "claim_ids", "evidence", "freshness",
                "observed_at", "truth_assessed")}
            for row in assumptions[:max_assumptions]
        ],
        "evidence": [
            {key: row.get(key) for key in (
                "session", "id", "evidence_type", "status", "has_wording",
                "wording_sha256", "source_ids", "supports_ids", "opposes_ids",
                "evidence", "freshness", "observed_at", "truth_assessed")}
            for row in evidence[:max_evidence]
        ],
        "counts": dict(report.get("counts") or {}),
        "provenance": dict(report.get("provenance") or {}),
        "engines": dict(report.get("engines") or {}),
        "not_assessed": list(report.get("not_assessed") or []),
        "deferred": list(report.get("deferred") or []),
        "omissions": list(report.get("omissions") or []) + (
            [f"{omitted} research rows omitted by the preflight view"]
            if omitted else []),
        "truncated": report_truncated,
        "truth_assessed": False,
    }
    bytes_dropped = 0
    for key in ("evidence", "assumptions", "sources", "claims"):
        while result[key] and len(
                C.canonical_json(result).encode("utf-8")) > MAX_JOIN_BYTES:
            result[key].pop()
            bytes_dropped += 1
    if bytes_dropped:
        result["truncated"] = C.truncation(
            True,
            omitted=int((result.get("truncated") or {}).get("omitted") or 0)
            + bytes_dropped,
            reason="bytes",
        ).to_dict()
        result["omissions"] = list(result.get("omissions") or []) + [
            f"{bytes_dropped} research rows omitted by the preflight byte bound"]
    return result


def projection_facts(report: Mapping[str, Any], *,
                     limit: int = MAX_FACTS) -> tuple[C.Fact, ...]:
    """Bounded :class:`contracts.Fact` records for the preflight join."""
    facts: list[C.Fact] = []

    def add(subject: str, value: Mapping[str, Any], *, source: str,
            observed_at: int | str, freshness: str, evidence: str) -> None:
        if len(facts) >= limit:
            return
        facts.append(C.Fact(
            subject=subject, value=dict(value), source=source,
            observed_at=observed_at, freshness=freshness, evidence=evidence,
        ))

    for row in report.get("sessions") or []:
        counts = row.get("counts") if isinstance(row.get("counts"), Mapping) else {}
        add(
            f"preflight.research.session.{row.get('id')}",
            {"state": row.get("state"), "storage": row.get("storage"),
             "file_sha256": row.get("file_sha256"),
             "claims": counts.get("claims"), "sources": counts.get("sources"),
             "truth_assessed": False},
            source="ozagia.core.research_projection",
            observed_at=row.get("observed_at", "unknown"),
            freshness=str(row.get("freshness") or "unknown"),
            evidence=str(row.get("evidence") or "inferred"),
        )
    for row in report.get("claims") or []:
        add(
            f"preflight.research.claim.{row.get('session')}.{row.get('id')}",
            {"claim_type": row.get("claim_type"), "status": row.get("status"),
             "source_ids": row.get("source_ids"),
             "assumption_ids": row.get("assumption_ids"),
             "support_state": row.get("support_state"),
             "trust_state": (row.get("trust") or {}).get("state"),
             "wording_sha256": row.get("wording_sha256"),
             "truth_assessed": False},
            source="ozagia.research.graph",
            observed_at=row.get("observed_at", "unknown"),
            freshness=str(row.get("freshness") or "unknown"),
            evidence="declared",
        )
    for row in report.get("sources") or []:
        add(
            f"preflight.research.source.{row.get('session')}.{row.get('id')}",
            {"source_type": row.get("source_type"),
             "hash_state": row.get("hash_state"),
             "recorded_sha256": row.get("recorded_sha256"),
             "observed_sha256": row.get("observed_sha256"),
             "holdings": row.get("holdings"),
             "truth_assessed": False},
            source="ozagia.research.graph",
            observed_at=row.get("observed_at", "unknown"),
            freshness=str(row.get("freshness") or "unknown"),
            evidence=str(row.get("evidence") or "declared"),
        )
    for row in report.get("assumptions") or []:
        add(
            f"preflight.research.assumption.{row.get('session')}.{row.get('id')}",
            {"status": row.get("status"), "claim_ids": row.get("claim_ids"),
             "wording_sha256": row.get("wording_sha256"),
             "truth_assessed": False},
            source="ozagia.research.graph",
            observed_at=row.get("observed_at", "unknown"),
            freshness=str(row.get("freshness") or "unknown"),
            evidence="declared",
        )
    for row in report.get("evidence") or []:
        add(
            f"preflight.research.evidence.{row.get('session')}.{row.get('id')}",
            {"evidence_type": row.get("evidence_type"),
             "status": row.get("status"),
             "source_ids": row.get("source_ids"),
             "truth_assessed": False},
            source="ozagia.research.graph",
            observed_at=row.get("observed_at", "unknown"),
            freshness=str(row.get("freshness") or "unknown"),
            evidence="declared",
        )
    add(
        "preflight.research.ocelot",
        dict(OCELOT),
        source="ozagia.core.research_projection",
        observed_at=report.get("provenance", {}).get("observed_at", "unknown"),
        freshness="unknown", evidence="inferred",
    )
    return tuple(facts)


def projection_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """Counts and omissions without the records themselves."""
    readiness, reason = projection_readiness(report)
    return {
        "readiness": readiness,
        "reason": reason,
        "counts": dict(report.get("counts") or {}),
        "sessions": len(report.get("sessions") or []),
        "claims": len(report.get("claims") or []),
        "sources": len(report.get("sources") or []),
        "assumptions": len(report.get("assumptions") or []),
        "evidence": len(report.get("evidence") or []),
        "engines": dict(report.get("engines") or {}),
        "not_assessed": list(report.get("not_assessed") or []),
        "deferred": list(report.get("deferred") or []),
        "omissions": list(report.get("omissions") or []),
        "truncated": dict(report.get("truncated")
                          or C.truncation().to_dict()),
    }


__all__ = [
    "DEFERRED", "MAX_CLAIMS", "MAX_EVIDENCE", "MAX_FACTS", "MAX_JOIN_BYTES",
    "MAX_REPORT_BYTES", "MAX_SESSIONS", "MAX_SOURCES", "NOT_ASSESSED", "OCELOT",
    "PROJECTION_VERSION", "ResearchProjectionError", "SCHEMA_NAMESPACE",
    "SCHEMA_VERSION", "build_projection", "preflight_join", "projection_facts",
    "projection_hash", "projection_json", "projection_readiness",
    "projection_summary",
]
