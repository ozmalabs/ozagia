"""Queries across independently indexed repository snapshots.

Federated keys have the form ``<checkout-id>::<snapshot-key>``. Integer node
IDs remain private to each snapshot. Discovery is intentionally bounded: with
no explicit ``roots``, inspect the selected checkout and its immediate sibling
directories only. No adapters are imported and no checkout is indexed. Paths
explicitly designated as references may be fetched and fast-forwarded; callers
can use ``without_reference_sync`` for cache-only inspection.
The selected root uses its configured state path; an absolute
``OZAGIA_STATE_DIR`` applies only to that anchor. Other members are read from
their own ``<root>/.ozagia/graph.json`` so one external state directory cannot
make sibling snapshots appear identical. Explicit roots remain listed when
they do not exist or have no valid snapshot.
Optional declared cross-checkout links live in the consumer's
``.ozagia/federation.json`` as ``{"version": 1, "links": [{"source":
"<checkout-id>::<node-key>", "target": "...", "relation": "...",
"provenance": "..."}]}``; links are exposed only when both endpoints resolve.
Declared members accept either a directory string or
``{"path": "...", "id": "<stable-id>"}``. A declared id is the qualified-key
prefix and survives a path move that keeps the declaration; entries without an
id and explicit ``roots`` keep a deterministic derived alias.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterable

from ozagia.config import repo_root, state_path

_SYNC_ENABLED: ContextVar[bool] = ContextVar("ozagia_reference_sync_enabled", default=True)
_REFERENCE_SYNC_RESULTS: dict[Path, list[dict[str, Any]]] = {}
_REFERENCE_SYNC_PENDING: dict[Path, list[dict[str, Any]]] = {}
_REFERENCE_SYNC_THREADS: dict[Path, threading.Thread] = {}
_REFERENCE_SYNC_LOCK = threading.Lock()
_REFERENCE_SYNC_CURSOR: dict[Path, int] = {}

# A declared member identity is the qualified-key prefix. It must stay short,
# lowercase and easy to reference from stored links.
_MEMBER_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_RESERVED_MEMBER_IDS = frozenset({"anchor", "self"})

# Additive, bounded family preference used only by ``search(family_boost=True)``.
# A hit whose member shares a declared family with the anchor gains this much
# rank score, capped at 1.0. Lexical ``score`` is retained unchanged.
FAMILY_RANK_BOOST = 0.15


@contextmanager
def without_reference_sync():
    """Temporarily disable automatic reference updates in this context."""
    token = _SYNC_ENABLED.set(False)
    try:
        yield
    finally:
        _SYNC_ENABLED.reset(token)


def _root(value: str | Path | None) -> Path:
    return Path(value or repo_root()).expanduser().resolve()


def _state_path(root: Path, anchor: Path) -> Path:
    """Use the configured state directory only for the selected checkout.

    An absolute ``OZAGIA_STATE_DIR`` is a location for the anchor's state; it
    must not make each sibling appear to own the same graph or manifest.
    Other checkouts use their repository-local ``.ozagia`` state directory.
    """
    if root == anchor:
        return state_path(root, "graph.json")
    return root / ".ozagia" / "graph.json"


def _read_snapshot(root: Path, anchor: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = _state_path(root, anchor)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing snapshot"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"invalid snapshot: {type(exc).__name__}: {exc}"
    valid_nodes = isinstance(data, dict) and isinstance(data.get("nodes"), list)
    valid_edges = isinstance(data, dict) and isinstance(data.get("edges"), list)
    if not valid_nodes or not valid_edges:
        return None, "invalid snapshot: expected nodes and edges arrays"
    return data, None


def _repo_attrs(data: dict[str, Any] | None) -> dict[str, Any]:
    if data:
        for node in data.get("nodes", []):
            if isinstance(node, dict) and node.get("type") == "Repo":
                attrs = node.get("attrs")
                return attrs if isinstance(attrs, dict) else {}
    return {}


def _reference_specs(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read explicitly designated references; ordinary members stay read-only."""
    path = state_path(root, "federation.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [], [f"cannot read manifest: {type(exc).__name__}: {exc}"]
    if not isinstance(raw, dict):
        return [], ["manifest must be an object"]
    if raw.get("version") != 1 or not isinstance(raw.get("links"), list):
        return [], ["expected {version: 1, links: [...]} manifest"]
    if "reference_repos" not in raw:
        return [], []
    values = raw["reference_repos"]
    if not isinstance(values, list):
        return [], ["reference_repos must be an array of objects"]
    specs: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[Path] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict) or set(value) - {"path", "pin"}:
            errors.append(f"reference_repos[{index}] must contain only path and optional pin")
            continue
        name, pin = value.get("path"), value.get("pin")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"reference_repos[{index}].path must be a non-empty string")
            continue
        if pin is not None and (not isinstance(pin, str) or not re.fullmatch(
                r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", pin)):
            errors.append(f"reference_repos[{index}].pin must be a full commit SHA")
            continue
        candidate = Path(name).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if resolved == root or resolved in seen:
            errors.append(f"reference_repos[{index}] is duplicate or names the anchor")
            continue
        seen.add(resolved)
        specs.append({"root": resolved, "pin": pin})
    return specs, errors


def _reference_batch(root: Path, specs: list[dict[str, Any]], *,
                     roots: Iterable[str | Path] | None = None,
                     limit: int = 6) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected = ({Path(p).expanduser().resolve() for p in roots}
                if roots is not None else None)
    matching = [spec for spec in specs
                if selected is None or spec["root"] in selected]
    if len(matching) <= limit:
        return matching, []
    with _REFERENCE_SYNC_LOCK:
        start = _REFERENCE_SYNC_CURSOR.get(root, 0) % len(matching)
        ordered = matching[start:] + matching[:start]
        batch = ordered[:limit]
        deferred = ordered[limit:]
        _REFERENCE_SYNC_CURSOR[root] = (start + len(batch)) % len(matching)
    return batch, deferred


def _sync_references(root: Path, roots: Iterable[str | Path] | None) -> list[dict[str, Any]]:
    specs, errors = _reference_specs(root)
    results: list[dict[str, Any]] = []
    if _SYNC_ENABLED.get():
        from ozagia.core.reference_sync import sync_reference
        batch, deferred = _reference_batch(root, specs, roots=roots)
        for spec in batch:
            results.append(sync_reference(spec["root"], pin=spec["pin"]))
        results.extend({"root": str(spec["root"]), "pin": spec["pin"],
                        "state": "pending", "reason": "reference_batch_limit",
                        "updated": False} for spec in deferred)
    else:
        from ozagia.core.reference_sync import cached_status
        selected = ({Path(p).expanduser().resolve() for p in roots}
                    if roots is not None else None)
        matching = [spec for spec in specs
                    if selected is None or spec["root"] in selected]
        for spec in matching:
            cached = cached_status(spec["root"])
            prior = next((item for item in reversed(cached)
                          if item.get("pin") == spec["pin"]), None)
            results.append(prior or {"root": str(spec["root"]),
                                     "state": "not_checked", "updated": False})
    results.extend({"state": "error", "reason": "invalid_manifest",
                    "detail": message, "updated": False} for message in errors)
    _REFERENCE_SYNC_RESULTS[root] = results
    return results


def reference_status(root: str | Path | None = None) -> dict[str, Any]:
    """Return reference declarations and cached status without syncing."""
    selected = _root(root)
    specs, errors = _reference_specs(selected)
    from ozagia.core.reference_sync import cached_status
    entries = []
    for spec in specs:
        cached = cached_status(spec["root"])
        last = next((entry for entry in reversed(_REFERENCE_SYNC_RESULTS.get(selected, []))
                     if entry.get("root") == str(spec["root"])
                     and entry.get("pin") == spec["pin"]), None)
        item = (last if last and last.get("state") == "pending" else
                next((entry for entry in reversed(cached)
                      if entry.get("pin") == spec["pin"]), None))
        is_pending = any(p["path"] == str(spec["root"])
                         for p in _REFERENCE_SYNC_PENDING.get(selected, []))
        entries.append({"path": str(spec["root"]), "pin": spec["pin"],
                        "state": "pending" if is_pending else
                                item["state"] if item else "not_checked",
                        "sync": item})
    return {"root": str(selected), "references": entries, "errors": errors,
            "pending": list(_REFERENCE_SYNC_PENDING.get(selected, []))}


def request_reference_sync(root: str | Path | None = None, *, limit: int = 6
                           ) -> dict[str, Any]:
    """Schedule bounded reference updates and return immediately (no Git I/O here)."""
    selected = _root(root)
    specs, errors = _reference_specs(selected)
    selected_specs, deferred_specs = _reference_batch(
        selected, specs, limit=max(0, int(limit)))
    pending = [{"path": str(item["root"]), "pin": item["pin"], "state": "pending"}
               for item in selected_specs]
    pending.extend({"path": str(item["root"]), "pin": item["pin"],
                    "state": "pending", "reason": "reference_batch_limit"}
                   for item in deferred_specs)
    with _REFERENCE_SYNC_LOCK:
        active = _REFERENCE_SYNC_THREADS.get(selected)
        if active and active.is_alive():
            return {"root": str(selected), "state": "pending",
                    "references": list(_REFERENCE_SYNC_PENDING.get(selected, [])),
                    "errors": errors}
        _REFERENCE_SYNC_PENDING[selected] = pending

        def worker() -> None:
            try:
                roots = [item["root"] for item in selected_specs]
                report = sync_references(selected, roots=roots, force=False)
                completed = list(report["references"])
                completed.extend({"root": str(item["root"]), "pin": item["pin"],
                                  "state": "pending", "reason": "reference_batch_limit",
                                  "updated": False} for item in deferred_specs)
                _REFERENCE_SYNC_RESULTS[selected] = completed
            finally:
                _REFERENCE_SYNC_PENDING[selected] = []

        thread = threading.Thread(target=worker,
                                  name="ozagia-reference-sync", daemon=True)
        _REFERENCE_SYNC_THREADS[selected] = thread
        thread.start()
    return {"root": str(selected), "state": "pending", "references": pending,
            "errors": errors,
            "omitted": len(deferred_specs)}


def sync_references(root: str | Path | None = None,
                    roots: Iterable[str | Path] | None = None,
                    *, force: bool = False) -> dict[str, Any]:
    """Explicitly synchronize selected designated references and report results."""
    selected = _root(root)
    if not force:
        results = _sync_references(selected, roots)
        return {"ok": not any(item.get("state") == "error" for item in results),
                "root": str(selected), "references": results}
    # Explicit sync bypasses the per-process query throttle.
    specs, errors = _reference_specs(selected)
    roots = tuple(roots) if roots is not None else None
    paths = {Path(p).expanduser().resolve() for p in roots} if roots is not None else None
    from ozagia.core.reference_sync import sync_reference
    results = [sync_reference(spec["root"], pin=spec["pin"], force=True)
               for spec in specs if paths is None or spec["root"] in paths]
    results.extend({"state": "error", "reason": "invalid_manifest",
                    "detail": message, "updated": False} for message in errors)
    _REFERENCE_SYNC_RESULTS[selected] = results
    return {"ok": not any(item.get("state") == "error" for item in results),
            "root": str(selected), "references": results}


def _candidate_roots(root: Path, roots: Iterable[str | Path] | None,
                     declared: Iterable[Path] = ()) -> list[Path]:
    candidates = [root]
    if roots is None:
        try:
            siblings = [p for p in root.parent.iterdir()
                        if not p.is_symlink() and p.is_dir() and p.resolve() != root]
        except OSError:
            siblings = []
        candidates.extend(sorted(siblings, key=lambda p: p.name.casefold()))
        references, _reference_errors = _reference_specs(root)
        candidates.extend(declared)
        candidates.extend(spec["root"] for spec in references)
    else:
        candidates.extend(Path(p).expanduser() for p in roots)
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _member_id_error(value: Any) -> str | None:
    """Return why a declared member id is unusable, or None when it is valid."""
    if not isinstance(value, str) or not value:
        return "must be a non-empty string"
    if value in _RESERVED_MEMBER_IDS:
        return f"is reserved: {value}"
    if not _MEMBER_ID_PATTERN.fullmatch(value):
        return ("must be 1-64 characters, start with a lowercase letter or digit, "
                "and use only [a-z0-9._-]")
    return None


def _manifest_members(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read declared member checkouts and their optional stable identities.

    Entries are either a directory string (the original form) or an object
    ``{"path": "...", "id": "..."}``. Relative paths resolve from the anchor.
    An invalid, reserved or duplicated id is reported as a member error; the
    checkout stays visible with a derived identity rather than disappearing.
    """
    path = state_path(root, "federation.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [], [f"cannot read manifest: {type(exc).__name__}: {exc}"]
    if (not isinstance(raw, dict) or raw.get("version") != 1
            or not isinstance(raw.get("links"), list)):
        return [], ["expected {version: 1, links: [...]} manifest"]
    if "members" not in raw:
        return [], []
    values = raw["members"]
    if not isinstance(values, list):
        return [], ["members must be an array of directory strings or member objects"]
    members: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[Path] = set()
    by_index: dict[int, dict[str, Any]] = {}
    ids: dict[str, list[int]] = {}
    for index, value in enumerate(values):
        member_id: Any = None
        if isinstance(value, str):
            name: Any = value
        elif isinstance(value, dict):
            if set(value) - {"path", "id"}:
                errors.append(
                    f"members[{index}] must contain only path and optional id")
                continue
            name = value.get("path")
            member_id = value.get("id")
            if not isinstance(name, str) or not name.strip():
                errors.append(
                    f"members[{index}].path must be a non-empty directory string")
                continue
        else:
            errors.append(f"members[{index}] must be a non-empty directory string")
            continue
        if not isinstance(name, str) or not name.strip():
            errors.append(f"members[{index}] must be a non-empty directory string")
            continue
        try:
            candidate = Path(name).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"members[{index}] cannot resolve path: "
                          f"{type(exc).__name__}: {exc}")
            continue
        if resolved == root or resolved in seen:
            errors.append(f"members[{index}] is duplicate or names the anchor")
            continue
        seen.add(resolved)
        record: dict[str, Any] = {"root": resolved, "id": None, "index": index,
                                  "identity_error": None}
        if member_id is not None:
            message = _member_id_error(member_id)
            if message:
                errors.append(f"members[{index}].id {message}")
                record["identity_error"] = f"members[{index}].id {message}"
            else:
                record["id"] = member_id
                ids.setdefault(member_id, []).append(index)
        members.append(record)
        by_index[index] = record
        if resolved.exists() and not resolved.is_dir():
            errors.append(f"members[{index}] is not a directory: {resolved}")
    for member_id, indexes in ids.items():
        if len(indexes) < 2:
            continue
        for index in indexes:
            message = f"members[{index}].id duplicates another member id: {member_id}"
            errors.append(message)
            record = by_index[index]
            record["id"] = None
            record["identity_error"] = message
    return members, errors


def _members(root: str | Path | None, roots: Iterable[str | Path] | None
             ) -> list[dict[str, Any]]:
    selected = _root(root)
    if roots is not None:
        roots = tuple(roots)
    sync_results = _sync_references(selected, roots)
    specs, _ = _reference_specs(selected)
    references = {spec["root"]: spec for spec in specs}
    sync_by_root = {Path(item["root"]).resolve(): item for item in sync_results
                    if item.get("root")}
    entries: list[dict[str, Any]] = []
    declared_records, _ = _manifest_members(selected)
    declared_roots = [record["root"] for record in declared_records]
    declared_set = set(declared_roots)
    # Explicit roots always replace the manifest, so only the manifest mode
    # carries a declared identity.
    declared = ({record["root"]: record for record in declared_records}
                if roots is None else {})
    reference_roots = set(references)
    for path in _candidate_roots(selected, roots, declared_roots):
        if (roots is None and not path.is_dir() and path not in declared_set
                and path not in reference_roots):
            continue
        snapshot, error = _read_snapshot(path, selected)
        # Automatic discovery includes only immediate siblings with a graph;
        # explicit roots are retained even when their snapshot is unavailable.
        if (roots is None and path != selected and error
                and path not in declared_set and path not in reference_roots):
            continue
        attrs = _repo_attrs(snapshot)
        label = str(attrs.get("name") or path.name or "repo")
        record = declared.get(path)
        entries.append({"root": path, "snapshot_path": _state_path(path, selected),
                        "snapshot": snapshot, "error": error,
                        "label": label, "attrs": attrs,
                        "reference": path in references,
                        "pin": references.get(path, {}).get("pin"),
                        "sync": sync_by_root.get(path),
                        "declared_index": record["index"] if record else None,
                        "declared_id": record["id"] if record else None,
                        "identity_error": record["identity_error"] if record else None})
    # Derived aliases resolve label collisions deterministically with a
    # path-derived suffix. They remain the fallback when no usable id is
    # declared.
    counts: dict[str, int] = {}
    for item in entries:
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", item["label"]).strip("-") or "repo"
        counts[base.casefold()] = counts.get(base.casefold(), 0) + 1
        item["base_alias"] = base
    for item in entries:
        base = item["base_alias"]
        if counts[base.casefold()] > 1:
            suffix = hashlib.sha256(str(item["root"]).encode()).hexdigest()[:8]
            item["derived_alias"] = f"{base}@{suffix}"
        else:
            item["derived_alias"] = base
    # A declared id must not shadow the anchor or another member's identity.
    # Rejected declarations fall back to the derived alias and stay reported.
    derived_owners = {item["derived_alias"].casefold(): item["root"]
                      for item in entries}
    anchor = entries[0]["derived_alias"] if entries else None
    for item in entries:
        wanted = item["declared_id"]
        if not wanted:
            item["alias"] = item["derived_alias"]
            item["identity_source"] = "derived"
            continue
        index = item["declared_index"]
        if anchor is not None and wanted.casefold() == anchor.casefold():
            message = (f"members[{index}].id collides with the anchor identity: "
                       f"{anchor}")
        else:
            owner = derived_owners.get(wanted.casefold())
            if owner is not None and owner != item["root"]:
                message = (f"members[{index}].id collides with another member "
                           f"identity: {wanted}")
            else:
                item["alias"] = wanted
                item["identity_source"] = "declared"
                continue
        item["alias"] = item["derived_alias"]
        item["identity_source"] = "derived"
        item["identity_error"] = message
    return entries


def _freshness(item: dict[str, Any]) -> tuple[str, str | None]:
    if item["error"]:
        return ("missing" if item["error"] == "missing snapshot" else "invalid", item["error"])
    attrs = item["attrs"]
    recorded_root = attrs.get("root")
    if recorded_root and Path(str(recorded_root)).expanduser().resolve() != item["root"]:
        return "stale", "snapshot root differs from checkout path"
    recorded_head = attrs.get("head")
    try:
        result = subprocess.run(["git", "-C", str(item["root"]), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown", "cannot read current git HEAD"
    if result.returncode != 0:
        return "unknown", "cannot read current git HEAD"
    current_head = result.stdout.strip()
    if not recorded_head:
        return "unknown", "snapshot does not record git HEAD"
    if current_head != recorded_head:
        return "stale", "snapshot HEAD differs from checkout HEAD"
    # Working tree freshness needs the configured index scope and is not
    # guessed here. Be explicit that the HEAD comparison is partial.
    return "unknown", "HEAD matches; working-tree freshness was not checked"


def _public_member(item: dict[str, Any]) -> dict[str, Any]:
    state, detail = _freshness(item)
    attrs = item["attrs"]
    out = {"id": item["alias"], "name": item["label"],
            "root": str(item["root"]), "snapshot": str(item["snapshot_path"]),
            "state": state, "detail": detail,
            "identity_source": item.get("identity_source", "derived"),
            "head": attrs.get("head"), "generated_at": attrs.get("generated_at"),
            "nodes": len((item["snapshot"] or {}).get("nodes", [])),
            "edges": len((item["snapshot"] or {}).get("edges", []))}
    if item.get("identity_error"):
        out["identity_error"] = item["identity_error"]
    if item.get("reference"):
        out["reference"] = True
        out["pin"] = item.get("pin")
        out["sync"] = item.get("sync") or {"state": "not_checked", "updated": False}
        if out["sync"].get("updated"):
            out["state"] = "stale"
            out["detail"] = "reference updated; snapshot is stale until reindexed"
    return out


def _declared_links(root: Path, members: list[dict[str, Any]]) -> dict[str, Any]:
    """Read and validate explicit consumer-owned links; never infer links."""
    path = state_path(root, "federation.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"path": str(path), "state": "absent", "links": [], "errors": [],
                "member_errors": []}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        message = f"cannot read manifest: {type(exc).__name__}: {exc}"
        return {"path": str(path), "state": "invalid", "links": [],
                "errors": [message], "member_errors": [message]}
    if (not isinstance(raw, dict) or raw.get("version") != 1
            or not isinstance(raw.get("links"), list)):
        message = "expected {version: 1, links: [...]} manifest"
        return {"path": str(path), "state": "invalid", "links": [],
                "errors": [message], "member_errors": [message]}
    known: set[str] = set()
    for member in members:
        snapshot = member["snapshot"]
        if snapshot:
            known.update(_qualified(member["alias"], _node_key(n))
                         for n in snapshot["nodes"] if isinstance(n, dict))
    valid: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, entry in enumerate(raw["links"]):
        if not isinstance(entry, dict):
            errors.append(f"links[{index}] must be an object")
            continue
        source, target = entry.get("source"), entry.get("target")
        relation, provenance = entry.get("relation"), entry.get("provenance")
        if not all(isinstance(value, str) and value.strip()
                   for value in (source, target, relation, provenance)):
            errors.append(
                f"links[{index}] needs non-empty source, target, relation and provenance strings")
            continue
        missing = [endpoint for endpoint in (source, target) if endpoint not in known]
        if missing:
            errors.append(f"links[{index}] endpoint not found: {', '.join(missing)}")
            continue
        valid.append({"source": source, "target": target, "type": relation,
                      "provenance": provenance, "source_manifest": str(path)})
    _declared, member_errors = _manifest_members(root)
    identity_errors = [item["identity_error"] for item in members
                       if item.get("identity_error")]
    member_errors = list(dict.fromkeys([*member_errors, *identity_errors]))
    return {"path": str(path), "state": "ok" if not errors and not member_errors else "partial",
            "links": valid, "errors": errors, "member_errors": member_errors}


def discover(root: str | Path | None = None,
             roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """List federation members; sync explicitly designated refs by default."""
    selected = _root(root)
    members = _members(selected, roots)
    links = _declared_links(selected, members)
    return {"ok": True, "root": str(selected),
            "discovery": "immediate-siblings" if roots is None else "explicit",
            "members": [_public_member(item) for item in members], "links": links,
            "reference_sync": list(_REFERENCE_SYNC_RESULTS.get(selected, []))}


def status(root: str | Path | None = None,
           roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Return status for selected repos, syncing designated refs by default."""
    report = discover(root, roots)
    report["states"] = {state: sum(m["state"] == state for m in report["members"])
                        for state in ("fresh", "stale", "missing", "invalid", "unknown")}
    return report


def _qualified(alias: str, key: Any) -> str:
    return f"{alias}::{key if isinstance(key, str) else ''}"


def _node_key(node: dict[str, Any]) -> str:
    key = node.get("key")
    if isinstance(key, str) and key:
        return key
    # Legacy snapshots may not carry stable keys. Preserve uniqueness locally.
    return f"{node.get('type', 'node')}:id-{node.get('id')}"


def _searchable(node: dict[str, Any]) -> str:
    attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
    return " ".join([str(node.get("type", "")), _node_key(node),
                      *(str(v) for v in attrs.values()
                        if isinstance(v, (str, int, float)))])


def search(root: str | Path | None, query: str,
           roots: Iterable[str | Path] | None = None, limit: int = 20,
           family_boost: bool = False) -> dict[str, Any]:
    """Simple deterministic lexical search over member snapshot labels/attrs.

    ``family_boost`` is opt-in and defaults to off. When enabled, the anchor is
    the first selected member, and a hit whose member shares a declared family
    with the anchor receives :data:`FAMILY_RANK_BOOST` added to its rank score
    (capped at 1.0). Non-family hits remain in the results and are only
    re-ranked. The lexical ``score`` is unchanged; ``rank_score`` carries the
    preference. Family declarations come from the anchor's manifest, and a
    missing or invalid declaration is reported in ``families`` without failing
    the search. With the default off, ordering is exactly the lexical baseline.
    """
    if roots is not None:
        roots = tuple(roots)
    terms = list(dict.fromkeys(re.findall(r"[a-z0-9_]+", str(query).casefold())))
    if not terms:
        family_state: dict[str, Any] = {
            "state": "disabled", "by_member": {}, "families": [], "errors": [],
            "truncated": False}
        if family_boost:
            from ozagia.core import repo_relations

            empty_members = _members(root, roots)
            family_state = repo_relations.family_index(_root(root), empty_members)
            public_members = [_public_member(item) for item in empty_members]
        else:
            public_members = discover(root, roots)["members"]
        return {"ok": True, "query": query, "results": [],
                "family_boost": bool(family_boost),
                "families": family_state, "members": public_members}
    items = _members(root, roots)
    hits: list[dict[str, Any]] = []
    for item in items:
        if item["error"]:
            continue
        for node in item["snapshot"]["nodes"]:
            if not isinstance(node, dict):
                continue
            key = _node_key(node)
            haystack = _searchable(node).casefold()
            matched = [term for term in terms if term in haystack]
            if not matched:
                continue
            attrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
            score = len(matched) / len(terms)
            hits.append({"qualified_key": _qualified(item["alias"], key),
                         "key": key, "kind": node.get("type"),
                         "name": attrs.get("name") or attrs.get("title") or key,
                         "file": attrs.get("file") or attrs.get("path"),
                         "matched_terms": matched, "score": score,
                         "rank_score": score, "family_match": False,
                         "families": [],
                         "source": {"id": item["alias"], "root": str(item["root"])}})
    family_state: dict[str, Any] = {
        "state": "disabled", "by_member": {}, "families": [], "errors": [],
        "truncated": False}
    if family_boost:
        from ozagia.core import repo_relations

        family_state = repo_relations.family_index(_root(root), items)
        anchor_alias = items[0]["alias"] if items else None
        anchor_families = set(
            family_state["by_member"].get(anchor_alias, [])) if anchor_alias else set()
        for hit in hits:
            member_families = family_state["by_member"].get(
                hit["source"]["id"], [])
            hit["families"] = list(member_families)
            hit["family_match"] = any(
                family_id in anchor_families for family_id in member_families)
            if hit["family_match"]:
                hit["rank_score"] = min(1.0, hit["score"] + FAMILY_RANK_BOOST)
        # Family matches win exact rank-score ties; remaining ties keep the
        # baseline alias/key order so repeated searches are deterministic.
        hits.sort(key=lambda h: (-h["rank_score"], not h["family_match"],
                                 str(h["source"]["id"]).casefold(), h["key"]))
    else:
        hits.sort(key=lambda h: (-h["score"], str(h["source"]["id"]).casefold(),
                                 h["key"]))
    return {"ok": True, "query": query, "results": hits[:max(0, int(limit))],
            "n": len(hits), "family_boost": bool(family_boost),
            "families": family_state,
            "members": [_public_member(item) for item in _members(root, roots)]}


def code_search(root: str | Path | None, query: str,
                roots: Iterable[str | Path] | None = None, limit: int = 20,
                include_snippet: bool = False,
                cache_dir: str | Path | None = None) -> dict[str, Any]:
    """Opt-in, read-only code search over current member source checkouts.

    Members are resolved with :func:`_members`; each existing member root is
    searched with :func:`ozagia.core.search.code_search`. Caches live under the
    anchor state directory (or a temporary fallback) and never under another
    member. No index is refreshed and no member is written. Code mode is
    relevance-ranked only; graph-mode ``search(family_boost=True)`` is
    unchanged. See :mod:`ozagia.core.federation_code` for the merge and
    provenance fields.
    """
    from ozagia.core import federation_code

    return federation_code.code_search(
        root, query, roots=roots, limit=limit,
        include_snippet=include_snippet, cache_dir=cache_dir)


def node(root: str | Path | None, qualified_key: str,
         roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Return one qualified node and its in-snapshot incident edges."""
    alias, sep, key = str(qualified_key).partition("::")
    if not sep or not alias or not key:
        return {"ok": False, "error": "qualified key must be <checkout-id>::<node-key>"}
    members = _members(root, roots)
    item = next((m for m in members if m["alias"] == alias), None)
    if item is None:
        return {"ok": False, "error": f"unknown checkout id: {alias}",
                "members": [_public_member(m) for m in members]}
    if item["error"]:
        return {"ok": False, "error": item["error"], "source": _public_member(item)}
    snapshot = item["snapshot"]
    found = next((n for n in snapshot["nodes"]
                  if isinstance(n, dict) and _node_key(n) == key), None)
    if found is None:
        return {"ok": False, "error": f"node key not found: {qualified_key}",
                "source": _public_member(item)}
    ids = {n.get("id"): _node_key(n) for n in snapshot["nodes"] if isinstance(n, dict)}
    nid = found.get("id")
    edges = []
    for edge in snapshot["edges"]:
        if not isinstance(edge, dict) or (edge.get("src") != nid and edge.get("tgt") != nid):
            continue
        source_key = ids.get(edge.get("src"))
        target_key = ids.get(edge.get("tgt"))
        if source_key is None or target_key is None:
            continue
        edges.append({"src": _qualified(alias, source_key), "type": edge.get("type"),
                      "tgt": _qualified(alias, target_key),
                      "attrs": edge.get("attrs", {}),
                      "source": {"id": alias, "root": str(item["root"])}})
    attrs = found.get("attrs") if isinstance(found.get("attrs"), dict) else {}
    link_data = _declared_links(_root(root), members)
    links = [link for link in link_data["links"]
             if link["source"] == qualified_key or link["target"] == qualified_key]
    return {"ok": True, "qualified_key": qualified_key, "key": key,
            "kind": found.get("type"), "attrs": attrs,
            "source": _public_member(item), "edges": edges,
            "cross_repository_edges": links,
            "cross_repository_link_errors": link_data["errors"]}


__all__ = ["discover", "status", "search", "code_search", "node",
           "sync_references", "reference_status", "request_reference_sync",
           "without_reference_sync"]
