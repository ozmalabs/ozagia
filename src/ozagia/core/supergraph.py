"""Persisted, versioned cache of the cross-repository relationship graph.

The supergraph stores the report returned by :func:`cross_repo.relations`:
member metadata, declared/package/API/adapter-bridge edges, families, API
references and mappings, derived order and bounded errors. It does **not**
store every member node: graph detail remains in each member's own
``graph.json`` snapshot.

Freshness is source-based. Each member contribution is keyed by member id plus
the SHA-256 of its snapshot bytes and the anchor's federation manifest bytes.
A missing, malformed or out-of-date artifact reports ``absent``, ``invalid`` or
``stale`` with a rebuild command instead of guessing. Writes use a
same-directory temporary file and ``os.replace``; the default location is the
anchor's state directory and no member is written.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.config import state_path

SCHEMA = "ozagia.supergraph.v1"
DEFAULT_NAME = "supergraph.json"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 128
MAX_CHANGED = MAX_MEMBERS
MAX_TEXT = 500
_CHUNK = 1024 * 1024
_REQUIRED = ("schema", "root", "generated_at", "content_hash", "source_hashes",
             "graph", "counts", "artifact")


class SupergraphError(ValueError):
    """The supergraph cannot be built or the requested artifact is unusable."""


def _anchor(root: str | Path) -> Path:
    return Path(root).expanduser().resolve()


def _out_path(root: Path, out: str | Path | None) -> Path:
    if out is None:
        return state_path(root, DEFAULT_NAME)
    path = Path(out).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _snapshot_path(anchor: Path, member_root: Path) -> Path:
    """The selected root uses its configured state path; members stay local."""
    return (state_path(anchor, "graph.json")
            if member_root == anchor
            else member_root / ".ozagia" / "graph.json")


def _digest(path: Path) -> str | None:
    """Hash file bytes in bounded chunks; ``None`` means absent."""
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                hasher.update(chunk)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return "unreadable"
    return hasher.hexdigest()


def _member_root(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _source_hashes(anchor: Path, members: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    manifest = _digest(state_path(anchor, "federation.json"))
    hashes: dict[str, dict[str, Any]] = {}
    for member in members:
        member_id = member.get("id")
        member_root = _member_root(member.get("root"))
        if not isinstance(member_id, str) or not member_id or member_root is None:
            continue
        hashes[member_id] = {
            "snapshot": _digest(_snapshot_path(anchor, member_root)),
            "manifest": manifest,
        }
    return hashes


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def content_hash(graph: Any) -> str:
    """The deterministic SHA-256 of the relationship graph body."""
    return hashlib.sha256(_canonical(graph)).hexdigest()


def _encode(data: Mapping[str, Any]) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n").encode("utf-8")


def _bounded(text: Any) -> str:
    value = str(text)
    return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "..."


def _rebuild(root: Path, out: str | Path | None) -> str:
    command = ["ozagia", "federation", "build", "--root", str(root)]
    if out is not None:
        command += ["--out", str(_out_path(root, out))]
    return " ".join(shlex.quote(part) for part in command)


def _artifact_meta(path: Path, members: int, encoded: bytes,
                   selection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "path": str(path),
        "members": members,
        "bytes": len(encoded),
        "limit_bytes": MAX_ARTIFACT_BYTES,
        "selection": dict(selection),
    }


def _check_size(encoded: bytes) -> None:
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise SupergraphError(
            f"relationship supergraph exceeds the {MAX_ARTIFACT_BYTES} byte "
            "artifact bound; narrow the federation selection")


def _build(anchor: Path, roots: Iterable[str | Path] | None,
           path: Path) -> tuple[dict[str, Any], bytes]:
    from ozagia.core import cross_repo

    selected = [Path(value).expanduser().resolve() for value in roots] if roots is not None else None
    graph = cross_repo.relations(anchor, selected)
    if not isinstance(graph, dict) or not isinstance(graph.get("members"), list):
        raise SupergraphError("cross-repository relations did not return a graph")
    raw_members = graph["members"]
    if len(raw_members) > MAX_MEMBERS:
        raise SupergraphError(
            f"relationship supergraph is limited to {MAX_MEMBERS} members")
    members: list[dict[str, Any]] = []
    seen: set[str] = set()
    for member in raw_members:
        if not isinstance(member, dict):
            raise SupergraphError("relationship graph has a malformed member record")
        member_id = member.get("id")
        if (not isinstance(member_id, str) or not member_id
                or member_id in seen or _member_root(member.get("root")) is None):
            raise SupergraphError(
                "relationship graph has a malformed or duplicate member record")
        seen.add(member_id)
        members.append(member)
    source_hashes = _source_hashes(anchor, members)
    if len(source_hashes) != len(members):
        raise SupergraphError("member ids are not unique in the relationship graph")
    counts = dict(graph.get("counts") or {})
    selection = {
        "mode": "manifest" if roots is None else "explicit",
        "roots": [str(value) for value in selected] if selected is not None else [],
    }
    data: dict[str, Any] = {
        "schema": SCHEMA,
        "root": str(anchor),
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "content_hash": content_hash(graph),
        "source_hashes": source_hashes,
        "graph": graph,
        "counts": counts,
        "artifact": _artifact_meta(path, len(source_hashes), b"", selection),
    }
    encoded = _encode(data)
    _check_size(encoded)
    # The stored byte count changes the serialized length slightly; measure the
    # final form so the metadata is truthful and the cap covers what is written.
    data["artifact"] = _artifact_meta(path, len(source_hashes), encoded, selection)
    encoded = _encode(data)
    _check_size(encoded)
    return data, encoded


def build(root: str | Path, roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Build the relationship supergraph without writing it.

    ``roots`` selects explicit federation members exactly like
    :func:`cross_repo.relations`; ``None`` uses manifest membership and normal
    bounded sibling discovery.
    """
    anchor = _anchor(root)
    data, _ = _build(anchor, roots, _out_path(anchor, None))
    return data


def write(root: str | Path, roots: Iterable[str | Path] | None = None,
          out: str | Path | None = None) -> dict[str, Any]:
    """Build and atomically persist the relationship supergraph.

    The default output is ``state_path(root, "supergraph.json")``. An explicit
    ``out`` path is honored as requested; parent directories are created only
    for that path. An explicit path inside a selected member must stay under
    that member's ``.ozagia`` directory and may not replace its snapshot or
    federation manifest. A failed replace leaves any existing artifact
    untouched.
    """
    anchor = _anchor(root)
    path = _out_path(anchor, out)
    data, encoded = _build(anchor, roots, path)
    if out is not None:
        _check_explicit_out(anchor, data["graph"], path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    result = dict(data)
    result.update({"path": str(path), "state": "fresh", "bytes": len(encoded)})
    return result


def _artifact_members(graph: Any) -> list[dict[str, str]] | None:
    if not isinstance(graph, dict) or not isinstance(graph.get("members"), list):
        return None
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for member in graph["members"]:
        if not isinstance(member, dict):
            return None
        member_id = member.get("id")
        member_root = _member_root(member.get("root"))
        if (not isinstance(member_id, str) or not member_id
                or member_root is None or member_id in seen):
            return None
        seen.add(member_id)
        records.append({"id": member_id, "root": str(member_root)})
    return records if len(records) <= MAX_MEMBERS else None


def _check_explicit_out(anchor: Path, graph: Any, path: Path) -> None:
    """Reject an explicit output path that could overwrite a member source.

    Default anchor-state writes and files under a selected member's ``.ozagia``
    directory are allowed, except for snapshots and federation manifests.
    """
    members = _artifact_members(graph)
    if members is None:
        raise SupergraphError(
            "cannot validate the explicit output path: graph members are malformed")
    roots = [(member["id"], Path(member["root"])) for member in members]
    target = path.resolve()
    if target == state_path(anchor, DEFAULT_NAME).resolve():
        return
    protected = {(root / ".ozagia" / "graph.json").resolve() for _, root in roots}
    protected.update((root / ".ozagia" / "federation.json").resolve()
                     for _, root in roots)
    protected.add(state_path(anchor, "graph.json").resolve())
    protected.add(state_path(anchor, "federation.json").resolve())
    if target in protected:
        raise SupergraphError(_bounded(
            "refusing to write to a selected member's graph.json or "
            "federation.json; choose another .ozagia artifact path or a path "
            f"outside member checkouts (path: {target})"))
    for member_id, root in roots:
        try:
            target.relative_to(root)
        except ValueError:
            continue
        state = root / ".ozagia"
        if target == state:
            raise SupergraphError(_bounded(
                f"refusing to write to selected member {member_id!r}'s state "
                f"directory; choose a file under {state} or a path outside "
                f"member checkouts (path: {target})"))
        try:
            target.relative_to(state)
        except ValueError:
            raise SupergraphError(_bounded(
                f"refusing to write inside selected member {member_id!r}; use "
                f"a file under {state} or a path outside member checkouts "
                f"(path: {target})"))


def _valid_hashes(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for entry in value.values():
        if not isinstance(entry, dict):
            return False
        if set(entry) != {"snapshot", "manifest"}:
            return False
        if not all(item is None or isinstance(item, str) for item in entry.values()):
            return False
    return True


def _invalid(anchor: Path, path: Path, out: str | Path | None,
             reason: str) -> dict[str, Any]:
    return {
        "ok": True, "schema": SCHEMA, "root": str(anchor), "path": str(path),
        "state": "invalid", "counts": {}, "changed": [],
        "changed_sources": {}, "reason": _bounded(reason),
        "rebuild": _rebuild(anchor, out),
    }


def read(root: str | Path, out: str | Path | None = None) -> dict[str, Any]:
    """Read a persisted supergraph and compare it with the current sources.

    ``state`` is ``fresh``, ``stale``, ``absent`` or ``invalid``. The result
    always carries a shell-quoted ``rebuild`` command; a stale or invalid
    artifact is reported, never repaired.
    """
    anchor = _anchor(root)
    path = _out_path(anchor, out)
    base: dict[str, Any] = {
        "ok": True, "schema": SCHEMA, "root": str(anchor), "path": str(path),
        "counts": {}, "changed": [], "changed_sources": {},
        "rebuild": _rebuild(anchor, out),
    }
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return {**base, "state": "absent", "reason": "supergraph artifact is absent"}
    except OSError as exc:
        return _invalid(anchor, path, out,
                        f"cannot stat supergraph: {type(exc).__name__}: {exc}")
    if size > MAX_ARTIFACT_BYTES:
        return _invalid(anchor, path, out,
                        f"artifact exceeds the {MAX_ARTIFACT_BYTES} byte bound")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {**base, "state": "absent", "reason": "supergraph artifact is absent"}
    except OSError as exc:
        return _invalid(anchor, path, out,
                        f"cannot read supergraph: {type(exc).__name__}: {exc}")
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        return _invalid(anchor, path, out,
                        f"invalid supergraph JSON: {type(exc).__name__}: {exc}")
    if not isinstance(data, dict):
        return _invalid(anchor, path, out, "supergraph must be a JSON object")
    missing = [key for key in _REQUIRED if key not in data]
    if missing:
        return _invalid(anchor, path, out,
                        "missing required keys: " + ", ".join(missing))
    if data["schema"] != SCHEMA:
        return _invalid(anchor, path, out, f"unsupported schema: {data['schema']!r}")
    if not isinstance(data["root"], str) or not isinstance(data["content_hash"], str):
        return _invalid(anchor, path, out, "root and content_hash must be strings")
    if not isinstance(data["generated_at"], str):
        return _invalid(anchor, path, out, "generated_at must be a string")
    if not isinstance(data["counts"], dict) or not isinstance(data["artifact"], dict):
        return _invalid(anchor, path, out, "counts and artifact must be objects")
    members = _artifact_members(data["graph"])
    if members is None:
        return _invalid(anchor, path, out, "graph members are missing or malformed")
    if not _valid_hashes(data["source_hashes"]):
        return _invalid(anchor, path, out, "source_hashes is malformed")
    if content_hash(data["graph"]) != data["content_hash"]:
        return _invalid(anchor, path, out, "content hash does not match the graph body")

    current = _source_hashes(anchor, members)
    stored = data["source_hashes"]
    if set(stored) != {member["id"] for member in members}:
        return _invalid(anchor, path, out,
                        "source_hashes does not match the graph members")
    changed: list[str] = []
    changed_sources: dict[str, list[str]] = {}
    for member_id in sorted(stored):
        fields = [field for field in ("snapshot", "manifest")
                  if stored[member_id].get(field) != current[member_id].get(field)]
        if fields:
            changed.append(member_id)
            changed_sources[member_id] = fields
    unreadable = sorted(member_id for member_id, entry in current.items()
                        if "unreadable" in entry.values())
    if unreadable:
        for member_id in unreadable:
            changed_sources.setdefault(member_id, []).append("unreadable")
        changed = sorted(set(changed) | set(unreadable))
    changed = changed[:MAX_CHANGED]

    result = {
        **base,
        "state": "fresh" if not changed and data["root"] == str(anchor) else "stale",
        "generated_at": data["generated_at"],
        "content_hash": data["content_hash"],
        "source_hashes": stored,
        "graph": data["graph"],
        "counts": data["counts"],
        "artifact": data["artifact"],
        "changed": changed,
        "changed_sources": changed_sources,
    }
    if data["root"] != str(anchor):
        result["reason"] = "artifact was built for another root"
    elif unreadable:
        result["reason"] = "one or more member sources are unreadable"
    elif changed:
        result["reason"] = "member snapshot or federation manifest changed"
    return result


def _same_selection(anchor: Path, artifact: Any, graph: Any,
                    roots: Iterable[str | Path] | None) -> bool:
    wanted = ({Path(value).expanduser().resolve() for value in roots}
              if roots is not None else set())
    mode = "manifest" if roots is None else "explicit"
    selection = artifact.get("selection") if isinstance(artifact, dict) else None
    if (isinstance(selection, dict) and selection.get("mode") in {"manifest", "explicit"}
            and isinstance(selection.get("roots"), list)):
        stored = {Path(value).expanduser().resolve()
                  for value in selection["roots"] if isinstance(value, str)}
        return selection["mode"] == mode and stored == wanted
    # Artifacts from before selection metadata fall back to member roots.
    members = _artifact_members(graph)
    if members is None:
        return False
    expected = {anchor} | wanted
    return {Path(member["root"]) for member in members} == expected


def refresh(root: str | Path, roots: Iterable[str | Path] | None = None,
            out: str | Path | None = None) -> dict[str, Any]:
    """Return a fresh cached supergraph, rebuilding only when needed."""
    anchor = _anchor(root)
    selected = ([Path(value).expanduser().resolve() for value in roots]
                if roots is not None else None)
    cached = read(anchor, out)
    if (cached["state"] == "fresh"
            and _same_selection(anchor, cached.get("artifact"),
                                cached.get("graph"), selected)):
        return {**cached, "cache": "hit", "rebuilt": False}
    result = write(anchor, roots=selected, out=out)
    reason = None
    if cached["state"] == "fresh":
        reason = "selected members differ from the cached artifact"
    result.update({
        "cache": "miss",
        "rebuilt": True,
        "previous_state": cached["state"],
    })
    if reason:
        result["reason"] = reason
    return result


__all__ = ["SCHEMA", "MAX_ARTIFACT_BYTES", "SupergraphError", "build",
           "content_hash", "read", "refresh", "write"]
