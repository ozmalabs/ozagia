"""Ocelot research/evidence map: a read-only overlay over protocol documents.

Ocelot keeps its research protocol under ``docs/research``: preregistrations,
amendments, results and per-topic indexes.  Only those four targeted patterns
are indexed, never the thousands of other files in that tree.  Documents are
read directly by this indexer (``texts=False``) and are never imported or
executed.

Node and edge shapes:

- ``OcelotResearch`` with key ``("ocelot_research", rel)`` and attrs ``kind``,
  ``path``, ``title``, ``topic``, ``blocks``, ``engine_hashes``,
  ``commit_pins`` and missing-link flags;
- the owning ``File`` node links to the document with
  ``file -ocelot_research_doc-> node`` (the direction chosen here);
- a document links to referenced workspace files with
  ``node -ocelot_research_uses-> file``;
- a seed manifest mentioned by a document links with
  ``node -ocelot_research_seed-> OcelotSeed`` when the seed node exists.

The overlay only links and reports.  Ocelot's protocol text remains the
authority on whether a claim is true.

Enable it beside the workspace adapter::

    [adapters]
    modules = [
        "ozagia.integrations.ocelot.workspace:register",
        "ozagia.integrations.ocelot.workspace_research:register_research",
    ]

It registers graph queries ``ocelot_research`` and
``ocelot_research_census`` and one MCP tool, ``ocelot_research``.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

RESEARCH_GLOBS = (
    "docs/research/**/*_PREREG.md",
    "docs/research/**/*_AMENDMENT.md",
    "docs/research/**/*_RESULTS.md",
    "docs/research/**/INDEX.md",
)
MAX_RESEARCH_FILES = 2_000
MAX_RESEARCH_BYTES = 2 * 1024 * 1024
MAX_LINKS_PER_DOC = 100
MAX_TOTAL_LINKS = 5_000
MAX_INDEX_ERRORS = 20
MAX_SUMMARY_NODES = 200

KIND_BY_SUFFIX = (
    ("_PREREG.MD", "prereg"),
    ("_AMENDMENT.MD", "amendment"),
    ("_RESULTS.MD", "results"),
)
INDEX_NAME = "INDEX.MD"
CHAIN_KINDS = frozenset({"prereg", "amendment", "results"})

HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
BLOCK_RE = re.compile(r"\bblock\s+(\d+)\b", re.I)
ENGINE_RE = re.compile(r"\bengine\s+([0-9a-f]{7,40})\b", re.I)
ENGINE_SHA_RE = re.compile(r"engine_sha256[\"':= ]+([0-9a-f]{64})", re.I)
COMMIT_RE = re.compile(
    r"\b(?:at|commit|base)\s+[`\"']?([0-9a-f]{7,40})\b", re.I)
PINNED_RE = re.compile(r"\bpinned\b.*?([0-9a-f]{7,40})\b", re.I)
REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])("
    r"(?:scripts|tools)/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.py"
    r"|tests/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.py"
    r"|seeds/[A-Za-z0-9_.-]+\.json"
    r"|docs/research/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.md"
    r"|results/[A-Za-z0-9_.-]+\.json"
    r"|runners/dsl/src/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.rs"
    r")(?![\w./-])"
)


# --------------------------------------------------------------------------- #
# small graph helpers
# --------------------------------------------------------------------------- #
def _root(memory: Any) -> Path:
    return Path(memory.raw_repo_root).resolve()


def _node(memory: Any, nid: int) -> dict[str, Any]:
    return memory.node(int(nid)) or {}


def _attrs(memory: Any, nid: int) -> dict[str, Any]:
    return dict((_node(memory, nid).get("attrs") or {}))


def _out(memory: Any, nid: int, edge: str) -> list[int]:
    return [int(x) for x in memory._neighbours(nid, edge)]


def _research_ids(memory: Any) -> list[int]:
    return sorted(memory.nodes_of_type("OcelotResearch"))


def _path_attr(memory: Any, nid: int) -> str:
    return str(_attrs(memory, nid).get("path") or "")


def _norm_rel(value: Any) -> str | None:
    """A safe root-relative POSIX path, or None for absolute/traversal input."""
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    if not text:
        return None
    path = PurePosixPath(text)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        return None
    norm = path.as_posix()
    return norm if norm not in ("", ".") else None


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _existing_rel(root: Path, value: Any) -> str | None:
    rel = _norm_rel(value)
    if rel is None:
        return None
    path = root / rel
    if not path.is_file() or not _inside(root, path):
        return None
    return rel


def _kind(rel: str) -> str:
    name = PurePosixPath(rel).name.upper()
    if name == INDEX_NAME:
        return "index"
    for suffix, kind in KIND_BY_SUFFIX:
        if name.endswith(suffix):
            return kind
    return "other"


def _chain_id(rel: str) -> str:
    name = PurePosixPath(rel).name
    upper = name.upper()
    for suffix, _kind_name in KIND_BY_SUFFIX:
        if upper.endswith(suffix):
            return name[: -len(suffix)]
    return PurePosixPath(name).stem


def _title(text: str, rel: str) -> str:
    match = HEADING_RE.search(text)
    if match:
        title = match.group(1).strip().strip("#").strip()
        if title:
            return title[:200]
    return PurePosixPath(rel).stem


def _hashes(pattern: re.Pattern[str], text: str) -> list[str]:
    return [match.group(1).lower() for match in pattern.finditer(text)]


def _unique(values: Iterable[str]) -> list[str]:
    return sorted({str(value) for value in values if str(value)})


# --------------------------------------------------------------------------- #
# indexer
# --------------------------------------------------------------------------- #
def _research_paths(ctx: Any, errors: list[str]) -> list[Path]:
    root = Path(ctx.root).resolve()
    found: set[Path] = set()
    for pattern in RESEARCH_GLOBS:
        try:
            matches = root.glob(pattern)
            for path in matches:
                if not path.is_file():
                    continue
                rel = path.relative_to(root).as_posix()
                if ctx.skipped(rel):
                    continue
                if not _inside(root, path):
                    errors.append(f"{rel}: resolves outside the checkout")
                    continue
                found.add(path)
                if len(found) >= MAX_RESEARCH_FILES:
                    errors.append(
                        f"research index capped at {MAX_RESEARCH_FILES} files")
                    return sorted(found)
        except OSError as exc:
            errors.append(f"{pattern}: {exc}")
    return sorted(found)


def _references(text: str) -> list[str]:
    return sorted(set(REFERENCE_RE.findall(text)))[:MAX_LINKS_PER_DOC]


def _link_references(ctx: Any, builder: Any, rid: int,
                     text: str, linked: list[str]) -> dict[str, list[str]]:
    root = Path(ctx.root).resolve()
    out: dict[str, list[str]] = {"tests": [], "scripts": [], "seeds": []}
    for reference in _references(text):
        if len(linked) >= MAX_TOTAL_LINKS:
            break
        target = _existing_rel(root, reference)
        if target is None:
            continue
        fid = ctx.file_node(target)
        builder.edge(rid, "ocelot_research_uses", fid)
        if target not in linked:
            linked.append(target)
        if target.startswith("tests/") and target.endswith(".py"):
            out["tests"].append(target)
        elif target.startswith(("scripts/", "tools/")) and target.endswith(".py"):
            out["scripts"].append(target)
        if target.startswith("seeds/") and target.endswith(".json"):
            seed_id = PurePosixPath(target).stem
            out["seeds"].append(seed_id)
            seed = builder.find(("ocelot_seed", seed_id))
            if seed is not None:
                builder.edge(rid, "ocelot_research_seed", seed)
    return out


def index_research(ctx: Any) -> None:
    """Index Ocelot prereg/amendment/results/index documents."""
    b = ctx.builder
    root = Path(ctx.root).resolve()
    errors: list[str] = []
    paths = _research_paths(ctx, errors)
    linked: list[str] = []
    records: dict[int, dict[str, Any]] = {}
    by_chain: dict[tuple[str, str], dict[str, list[int]]] = {}

    for path in paths:
        rel = path.relative_to(root).as_posix()
        kind = _kind(rel)
        try:
            size = path.stat().st_size
            if size > MAX_RESEARCH_BYTES:
                errors.append(
                    f"{rel}: larger than {MAX_RESEARCH_BYTES} bytes")
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
            continue

        topic = PurePosixPath(rel).parent.as_posix()
        chain_id = _chain_id(rel)
        attrs = {
            "kind": kind,
            "path": rel,
            "title": _title(text, rel),
            "topic": topic,
            "chain": chain_id,
            "blocks": sorted({int(value) for value in BLOCK_RE.findall(text)}),
            "block_ids": sorted(
                {int(value) for value in BLOCK_RE.findall(text)}),
            "engine_hashes": _unique(
                (*_hashes(ENGINE_RE, text), *_hashes(ENGINE_SHA_RE, text))),
            "commit_pins": _unique(
                (*_hashes(COMMIT_RE, text), *_hashes(PINNED_RE, text))),
            "missing_gate": False,
            "missing_results": False,
            "missing_prereg": False,
            "missing_script": False,
        }
        fid = ctx.file_node(rel)
        existing = b.find(("ocelot_research", rel))
        if existing is None:
            rid = b.node("OcelotResearch", attrs,
                         key=("ocelot_research", rel))
        else:
            rid = existing
            b.attrs(rid).update(attrs)
        b.edge(fid, "ocelot_research_doc", rid)
        b.edge(ctx.repo, "has_ocelot_research", rid)
        refs = _link_references(ctx, b, rid, text, linked)
        records[rid] = {
            "rel": rel, "kind": kind, "topic": topic, "chain": chain_id,
            "tests": sorted(set(refs["tests"])),
            "scripts": sorted(set(refs["scripts"])),
            "seeds": sorted(set(refs["seeds"])),
        }
        if kind in CHAIN_KINDS:
            by_chain.setdefault((topic, chain_id), {}).setdefault(
                kind, []).append(rid)

    missing_gate: list[str] = []
    for rid, record in records.items():
        kind = record["kind"]
        chain = by_chain.get((record["topic"], record["chain"]), {})
        if kind == "prereg":
            record["missing_results"] = not chain.get("results")
            record["missing_prereg"] = False
        elif kind == "results":
            record["missing_results"] = False
            record["missing_prereg"] = not chain.get("prereg")
        else:
            record["missing_results"] = False
            record["missing_prereg"] = False
        if kind in ("prereg", "results"):
            record["missing_gate"] = not record["tests"]
            record["missing_script"] = not record["scripts"]
        else:
            record["missing_gate"] = False
            record["missing_script"] = False
        b.attrs(rid).update({
            "missing_gate": bool(record["missing_gate"]),
            "missing_results": bool(record["missing_results"]),
            "missing_prereg": bool(record["missing_prereg"]),
            "missing_script": bool(record["missing_script"]),
        })
        if record["missing_gate"]:
            missing_gate.append(record["rel"])

    by_kind: dict[str, int] = {}
    for record in records.values():
        by_kind[record["kind"]] = by_kind.get(record["kind"], 0) + 1
    repo_attrs = b.attrs(ctx.repo)
    repo_attrs["ocelot_research_docs"] = len(records)
    repo_attrs["ocelot_research_missing_gate"] = len(missing_gate)
    repo_attrs["ocelot_research_by_kind"] = by_kind
    repo_attrs["ocelot_research_errors"] = errors[:MAX_INDEX_ERRORS]
    repo_attrs["ocelot_research_error_count"] = len(errors)


# --------------------------------------------------------------------------- #
# summaries
# --------------------------------------------------------------------------- #
def _summary_shell(target: str) -> dict[str, Any]:
    return {
        "ok": False,
        "target": target,
        "nodes": [],
        "chain": {"prereg": [], "amendments": [], "results": [], "indexes": []},
        "scripts": [],
        "tests": [],
        "seeds": [],
        "engine_hashes": [],
        "commit_pins": [],
        "missing_gate": False,
        "stale": False,
        "reasons": [],
        "resources": [],
    }


def _node_payload(memory: Any, nid: int) -> dict[str, Any]:
    attrs = _attrs(memory, nid)
    return {
        "id": int(nid),
        "type": "OcelotResearch",
        "kind": str(attrs.get("kind") or ""),
        "path": str(attrs.get("path") or ""),
        "title": str(attrs.get("title") or ""),
        "topic": str(attrs.get("topic") or ""),
        "blocks": list(attrs.get("blocks") or []),
        "block_ids": list(attrs.get("block_ids") or attrs.get("blocks") or []),
        "engine_hashes": list(attrs.get("engine_hashes") or []),
        "commit_pins": list(attrs.get("commit_pins") or []),
        "missing_gate": bool(attrs.get("missing_gate")),
        "missing_results": bool(attrs.get("missing_results")),
        "missing_prereg": bool(attrs.get("missing_prereg")),
        "missing_script": bool(attrs.get("missing_script")),
    }


def _resolve_target(memory: Any, target: str) -> tuple[list[int], str | None]:
    ids = _research_ids(memory)
    value = str(target or "").strip()
    if not value:
        return [], "target is required (path, block number or topic)"

    block_match = re.fullmatch(r"(?:block\s+)?(\d+)", value, re.I)
    if block_match:
        block = int(block_match.group(1))
        matches = [
            nid for nid in ids
            if str(_attrs(memory, nid).get("kind") or "") != "index"
            and block in [int(x) for x in (_attrs(memory, nid).get("blocks")
                                           or [])]
        ]
        return matches, None if matches else f"no research document for block {block}"

    rel = _norm_rel(value)
    matches = [nid for nid in ids if _path_attr(memory, nid) == rel]
    if not matches and rel is not None and "/" not in rel:
        matches = [
            nid for nid in ids
            if PurePosixPath(_path_attr(memory, nid)).name == rel
        ]
    if matches:
        return matches, None

    needle = value.casefold()
    matches = []
    for nid in ids:
        attrs = _attrs(memory, nid)
        haystack = " ".join(str(attrs.get(key) or "") for key in
                            ("path", "topic", "title", "chain")).casefold()
        if needle in haystack:
            matches.append(nid)
    if matches:
        return matches, None
    return [], f"no Ocelot research document matches {value!r}"


def _expand_chain(memory: Any, seeds: list[int]) -> list[int]:
    """Add sibling prereg/amendment/results and the topic index."""
    ids = _research_ids(memory)
    selected = set(int(nid) for nid in seeds)
    chains: set[tuple[str, str]] = set()
    topics: set[str] = set()
    index_topics: set[str] = set()
    for nid in selected:
        attrs = _attrs(memory, nid)
        topic = str(attrs.get("topic") or "")
        kind = str(attrs.get("kind") or "")
        if topic:
            topics.add(topic)
        if kind in CHAIN_KINDS:
            chain = str(attrs.get("chain") or "")
            if chain:
                chains.add((topic, chain))
        elif kind == "index":
            index_topics.add(topic)
    for nid in ids:
        attrs = _attrs(memory, nid)
        topic = str(attrs.get("topic") or "")
        kind = str(attrs.get("kind") or "")
        if (topic, str(attrs.get("chain") or "")) in chains:
            selected.add(int(nid))
        elif kind == "index" and topic in topics:
            selected.add(int(nid))
        elif topic in index_topics:
            selected.add(int(nid))
    return sorted(selected)[:MAX_SUMMARY_NODES]


def _current_engine_sha(root: Path) -> tuple[str | None, str | None]:
    try:
        from .workspace_doctor import status
        result = status(root)
    except Exception as exc:  # doctor is best-effort and must never fail a query
        return None, f"{type(exc).__name__}: {exc}"
    native = ((result.get("engine") or {}).get("native") or {})
    sha = native.get("sha256")
    return (str(sha) if isinstance(sha, str) and sha else None), None


def _hash_matches(recorded: str, current: str) -> bool:
    return current.lower().startswith(recorded.lower())


def research_summary(memory: Any, target: str) -> dict[str, Any]:
    """The evidence chain for a research path, block number or topic."""
    target = str(target or "").strip()
    result = _summary_shell(target)
    matches, error = _resolve_target(memory, target)
    if error:
        result["error"] = error
        return result
    selected = _expand_chain(memory, matches)
    if not selected:
        result["error"] = f"no Ocelot research document matches {target!r}"
        return result

    root = _root(memory)
    chain: dict[str, list[str]] = {
        "prereg": [], "amendments": [], "results": [], "indexes": []}
    scripts: set[str] = set()
    tests: set[str] = set()
    seeds: set[str] = set()
    engine_hashes: set[str] = set()
    commit_pins: set[str] = set()
    resources: set[str] = set()
    missing_gate: list[str] = []
    reasons: list[str] = []
    nodes: list[dict[str, Any]] = []

    for nid in selected:
        attrs = _attrs(memory, nid)
        rel = str(attrs.get("path") or "")
        kind = str(attrs.get("kind") or "")
        nodes.append(_node_payload(memory, nid))
        if rel:
            resources.add(rel)
        key = {"prereg": "prereg", "amendment": "amendments",
               "results": "results", "index": "indexes"}.get(kind)
        if key and rel:
            chain[key].append(rel)
        engine_hashes.update(str(x) for x in attrs.get("engine_hashes") or [])
        commit_pins.update(str(x) for x in attrs.get("commit_pins") or [])
        for fid in _out(memory, nid, "ocelot_research_uses"):
            path = _existing_rel(root, _attrs(memory, fid).get("path"))
            if path is None:
                continue
            resources.add(path)
            if path.startswith("tests/") and path.endswith(".py"):
                tests.add(path)
            elif path.startswith(("scripts/", "tools/")) and path.endswith(".py"):
                scripts.add(path)
        for sid in _out(memory, nid, "ocelot_research_seed"):
            seed_attrs = _attrs(memory, sid)
            seed_id = str(seed_attrs.get("id") or seed_attrs.get("name") or "")
            if seed_id:
                seeds.add(seed_id)
        if kind in ("prereg", "results") and attrs.get("missing_gate"):
            missing_gate.append(rel)

    for values in chain.values():
        values.sort()
    nodes.sort(key=lambda item: item["path"])
    result["nodes"] = nodes
    result["chain"] = chain
    result["scripts"] = sorted(scripts)
    result["tests"] = sorted(tests)
    result["seeds"] = sorted(seeds)
    result["engine_hashes"] = sorted(engine_hashes)
    result["commit_pins"] = sorted(commit_pins)
    result["resources"] = sorted(resources)
    result["missing_gate"] = bool(missing_gate)
    if missing_gate:
        reasons.append(
            "prereg/results without a linked tests/ gate: "
            + ", ".join(sorted(missing_gate)))
    if commit_pins:
        reasons.append("commit pins are recorded but not checked by this slice")

    current, doctor_error = _current_engine_sha(root)
    recorded = result["engine_hashes"]
    if recorded and current:
        if not any(_hash_matches(value, current) for value in recorded):
            result["stale"] = True
            reasons.append(
                "recorded engine hashes do not match the current native "
                f"engine ({current[:12]})")
    elif recorded and doctor_error:
        reasons.append(f"current engine hash unavailable: {doctor_error}")
    elif recorded:
        reasons.append("current native engine hash unavailable; "
                       "engine staleness not checked")
    if not nodes:
        reasons.append("no research documents matched")
    result["ok"] = True
    result["reasons"] = reasons
    return result


def research_census(memory: Any) -> dict[str, Any]:
    """Counts and missing links across every indexed research document."""
    root = _root(memory)
    ids = _research_ids(memory)
    by_kind: dict[str, int] = {}
    prereg_without_results: list[str] = []
    results_without_prereg: list[str] = []
    missing_gates: list[str] = []
    resources: list[str] = []
    for nid in ids:
        attrs = _attrs(memory, nid)
        rel = str(attrs.get("path") or "")
        kind = str(attrs.get("kind") or "other")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if not _existing_rel(root, rel):
            continue
        resources.append(rel)
        if kind == "prereg" and attrs.get("missing_results"):
            prereg_without_results.append(rel)
        if kind == "results" and attrs.get("missing_prereg"):
            results_without_prereg.append(rel)
        if kind in ("prereg", "results") and attrs.get("missing_gate"):
            missing_gates.append(rel)
    errors: list[str] = []
    for nid in memory.nodes_of_type("Repo"):
        errors = list(_attrs(memory, nid).get("ocelot_research_errors") or [])
        break
    return {
        "ok": True,
        "docs": len(resources),
        "by_kind": dict(sorted(by_kind.items())),
        "prereg_without_results": sorted(prereg_without_results),
        "results_without_prereg": sorted(results_without_prereg),
        "missing_gates": sorted(missing_gates),
        "missing_gate": len(missing_gates),
        "resources": sorted(resources),
        "errors": errors,
    }


# --------------------------------------------------------------------------- #
# adapter surface
# --------------------------------------------------------------------------- #
def _query_research(memory: Any, **kwargs: Any) -> Any:
    target = str(kwargs.get("target") or "").strip()
    return research_summary(memory, target) if target else research_census(memory)


def _tool_research(memory: Any, args: Mapping[str, Any]) -> Any:
    return _query_research(memory, target=args.get("target"))


RESEARCH_TOOL = {
    "name": "ocelot_research",
    "description": (
        "Read-only Ocelot research/evidence map. With a target (path, block "
        "number or topic) return the prereg/amendment/results chain, linked "
        "scripts, tests and seeds, engine hashes and staleness; without one "
        "return a census of missing links."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": ("research document path, block number such "
                                "as 371000, or a topic substring"),
            },
        },
        "additionalProperties": False,
    },
}


def register_research(registry: Any) -> None:
    """Register the research indexer, graph queries and MCP tool."""
    if getattr(registry, "_ocelot_research_registered", False):
        return
    setattr(registry, "_ocelot_research_registered", True)
    registry.add_indexer(index_research, watch=RESEARCH_GLOBS, texts=False)
    registry.add_graph_query("ocelot_research", _query_research)
    registry.add_graph_query(
        "ocelot_research_census", lambda memory, **kw: research_census(memory))
    registry.add_mcp_tool(RESEARCH_TOOL, _tool_research)


__all__ = [
    "MAX_RESEARCH_BYTES",
    "MAX_RESEARCH_FILES",
    "RESEARCH_GLOBS",
    "RESEARCH_TOOL",
    "index_research",
    "register_research",
    "research_census",
    "research_summary",
]
