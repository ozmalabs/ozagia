"""Read-only queries across independently indexed repository snapshots.

Federated keys have the form ``<checkout-id>::<snapshot-key>``. Integer node
IDs remain private to each snapshot. Discovery is intentionally bounded: with
no explicit ``roots``, inspect the selected checkout and its immediate sibling
directories only. No adapters are imported and no checkout is indexed.
The selected root uses its configured state path; an absolute
``OZAGIA_STATE_DIR`` applies only to that anchor. Other members are read from
their own ``<root>/.ozagia/graph.json`` so one external state directory cannot
make sibling snapshots appear identical. Explicit roots remain listed when
they do not exist or have no valid snapshot.
Optional declared cross-checkout links live in the consumer's
``.ozagia/federation.json`` as ``{"version": 1, "links": [{"source":
"<checkout-id>::<node-key>", "target": "...", "relation": "...",
"provenance": "..."}]}``; links are exposed only when both endpoints resolve.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable

from ozagia.config import repo_root, state_path


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


def _candidate_roots(root: Path, roots: Iterable[str | Path] | None) -> list[Path]:
    candidates = [root]
    if roots is None:
        try:
            siblings = [p for p in root.parent.iterdir()
                        if not p.is_symlink() and p.is_dir() and p.resolve() != root]
        except OSError:
            siblings = []
        candidates.extend(sorted(siblings, key=lambda p: p.name.casefold()))
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


def _members(root: str | Path | None, roots: Iterable[str | Path] | None
             ) -> list[dict[str, Any]]:
    selected = _root(root)
    entries: list[dict[str, Any]] = []
    for path in _candidate_roots(selected, roots):
        if roots is None and not path.is_dir():
            continue
        snapshot, error = _read_snapshot(path, selected)
        # Automatic discovery includes only immediate siblings with a graph;
        # explicit roots are retained even when their snapshot is unavailable.
        if roots is None and path != selected and error:
            continue
        attrs = _repo_attrs(snapshot)
        label = str(attrs.get("name") or path.name or "repo")
        entries.append({"root": path, "snapshot_path": _state_path(path, selected),
                        "snapshot": snapshot, "error": error,
                        "label": label, "attrs": attrs})
    # Alias collisions are resolved deterministically with an identity suffix.
    counts: dict[str, int] = {}
    for item in entries:
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", item["label"]).strip("-") or "repo"
        counts[base.casefold()] = counts.get(base.casefold(), 0) + 1
        item["base_alias"] = base
    for item in entries:
        base = item["base_alias"]
        if counts[base.casefold()] > 1:
            suffix = hashlib.sha256(str(item["root"]).encode()).hexdigest()[:8]
            item["alias"] = f"{base}@{suffix}"
        else:
            item["alias"] = base
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
    return {"id": item["alias"], "name": item["label"],
            "root": str(item["root"]), "snapshot": str(item["snapshot_path"]),
            "state": state, "detail": detail,
            "head": attrs.get("head"), "generated_at": attrs.get("generated_at"),
            "nodes": len((item["snapshot"] or {}).get("nodes", [])),
            "edges": len((item["snapshot"] or {}).get("edges", []))}


def _declared_links(root: Path, members: list[dict[str, Any]]) -> dict[str, Any]:
    """Read and validate explicit consumer-owned links; never infer links."""
    path = state_path(root, "federation.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"path": str(path), "state": "absent", "links": [], "errors": []}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"path": str(path), "state": "invalid", "links": [],
                "errors": [f"cannot read manifest: {type(exc).__name__}: {exc}"]}
    if (not isinstance(raw, dict) or raw.get("version") != 1
            or not isinstance(raw.get("links"), list)):
        return {"path": str(path), "state": "invalid", "links": [],
                "errors": ["expected {version: 1, links: [...]} manifest"]}
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
    return {"path": str(path), "state": "ok" if not errors else "partial",
            "links": valid, "errors": errors}


def discover(root: str | Path | None = None,
             roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """List federation members and snapshot availability without writing."""
    selected = _root(root)
    members = _members(selected, roots)
    links = _declared_links(selected, members)
    return {"ok": True, "root": str(selected),
            "discovery": "immediate-siblings" if roots is None else "explicit",
            "members": [_public_member(item) for item in members], "links": links}


def status(root: str | Path | None = None,
           roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Return read-only status for each discovered or explicitly named repo."""
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
           roots: Iterable[str | Path] | None = None, limit: int = 20) -> dict[str, Any]:
    """Simple deterministic lexical search over member snapshot labels/attrs."""
    if roots is not None:
        roots = tuple(roots)
    terms = list(dict.fromkeys(re.findall(r"[a-z0-9_]+", str(query).casefold())))
    if not terms:
        return {"ok": True, "query": query, "results": [],
                "members": discover(root, roots)["members"]}
    hits: list[dict[str, Any]] = []
    for item in _members(root, roots):
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
            hits.append({"qualified_key": _qualified(item["alias"], key),
                         "key": key, "kind": node.get("type"),
                         "name": attrs.get("name") or attrs.get("title") or key,
                         "file": attrs.get("file") or attrs.get("path"),
                         "matched_terms": matched, "score": len(matched) / len(terms),
                         "source": {"id": item["alias"], "root": str(item["root"])}})
    hits.sort(key=lambda h: (-h["score"], str(h["source"]["id"]).casefold(), h["key"]))
    return {"ok": True, "query": query, "results": hits[:max(0, int(limit))],
            "n": len(hits), "members": [_public_member(item) for item in _members(root, roots)]}


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


__all__ = ["discover", "status", "search", "node"]
