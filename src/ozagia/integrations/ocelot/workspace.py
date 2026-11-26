"""Ocelot workspace graph, seed impact and scoped test planning.

This is an optional adapter for working *on* an Ocelot checkout. It indexes
top-level ``seeds/*.json`` metadata, seed dependencies, boot/registry
references, test/doc/source mentions, and engine-aware files. It never
imports Ocelot, builds its engine, runs its tests or downloads anything.

The adapter is enabled by a project config in the Ocelot checkout::

    [adapters]
    modules = ["ozagia.integrations.ocelot.workspace:register"]

It registers the indexer, graph queries and MCP tools ``ocelot_status``,
``ocelot_seed``, ``ocelot_seeds`` and ``ocelot_plan``.

Generated caches (``seeds/acquired``, ``seeds/generated``, ``data``,
``results`` and evaluation reports) are deliberately not indexed. The
top-level seed manifests are the development source of truth.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

SEED_DIR = "seeds"
SEED_GLOB = "seeds/*.json"
BOOT_FILES = (
    "substrate/boot_core.py",
    "substrate/boot_all.py",
    "substrate/boot_profiles.py",
    "domains/seed_registry.py",
)
ENGINE_PREFIX = "runners/dsl/"
ENGINE_SOURCE_PREFIX = "runners/dsl/src/"
ENGINE_BUILD_RECORD = "runners/dsl/python/substrate_rs/_native.build.json"
ENGINE_NATIVE = "runners/dsl/python/substrate_rs/_native.abi3.so"
MAX_SEED_BYTES = 32 * 1024 * 1024
MAX_SEED_HEAD_BYTES = 2 * 1024 * 1024
MAX_DESCRIPTION = 600
MAX_ITEM_NAMES = 12
MAX_ITEM_NAME_CHARS = 80
MAX_BOOT_FILES = 20
MAX_INDEX_ERRORS = 20
MAX_PLAN_TESTS = 250
MAX_CENSUS_ENTRIES = 200
SHARED_FANOUT = 20
BUILTIN_SEEDS = frozenset({"core"})
MENTION_TEXT_RE = re.compile(
    r"seeds/[A-Za-z0-9_.-]+\.json|substrate_rs|engine_provenance|"
    r"_native\.abi3\.so|manifest_for\(")
SEED_PATH_RE = re.compile(r"seeds/([A-Za-z0-9_.-]+)\.json")

FULL_GATE_PATHS = frozenset({
    "conftest.py",
    "tests/conftest.py",
    "substrate/seed_loader.py",
    "substrate/boot_core.py",
    "substrate/boot_all.py",
    "substrate/boot_profiles.py",
    "substrate/world_adapter.py",
    "domains/seed_registry.py",
    "domains/engine_provenance.py",
    "scripts/engine_build.sh",
    "scripts/engine_cargo_test.sh",
    "runners/dsl/Cargo.toml",
})
FULL_GATE_PREFIXES = (
    "runners/dsl/.cargo/",
)
KNOWN_SHARED_SEEDS = frozenset({
    "compiler_theory",
    "thinking_budget",
    "shape_decode_loop",
})
ENGINE_TEST = "tests/test_engine_provenance_migrated.py"
FULL_GATE_COMMAND = "pytest tests -k migrated"


def _root(memory: Any) -> Path:
    return Path(memory.raw_repo_root).resolve()


def _seed_key(seed_id: str) -> tuple[str, str]:
    return ("ocelot_seed", str(seed_id))


def _seed_nid(memory: Any, seed_id: str) -> int | None:
    return memory.node_by_key("ocelot_seed:" + str(seed_id))


def _node(memory: Any, nid: int) -> dict[str, Any]:
    return memory.node(int(nid)) or {}


def _attrs(memory: Any, nid: int) -> dict[str, Any]:
    return dict((_node(memory, nid).get("attrs") or {}))


def _type(memory: Any, nid: int) -> str:
    return str(_node(memory, nid).get("type") or "")


def _seed_ids(memory: Any) -> list[str]:
    out: list[str] = []
    for nid in memory.nodes_of_type("OcelotSeed"):
        attrs = _attrs(memory, nid)
        if attrs.get("stub"):
            continue
        seed_id = str(attrs.get("id") or attrs.get("name") or "")
        if seed_id:
            out.append(seed_id)
    return sorted(out)


def _out(memory: Any, nid: int, edge: str) -> list[int]:
    return [int(x) for x in memory._neighbours(nid, edge)]


def _in(memory: Any, nid: int, edge: str) -> list[int]:
    return [int(x) for x in memory._neighbours(nid, edge, incoming=True)]


def _seed_direct_dependents(memory: Any, nid: int) -> list[str]:
    out: list[str] = []
    for x in _in(memory, nid, "ocelot_depends_on"):
        if _type(memory, x) == "OcelotSeed":
            attrs = _attrs(memory, x)
            if not attrs.get("stub") and attrs.get("id"):
                out.append(str(attrs["id"]))
    return sorted(set(out))


def _seed_direct_dependencies(memory: Any, nid: int) -> list[str]:
    out: list[str] = []
    for x in _out(memory, nid, "ocelot_depends_on"):
        if _type(memory, x) == "OcelotSeed":
            attrs = _attrs(memory, x)
            if attrs.get("id"):
                out.append(str(attrs["id"]))
    # The manifest keeps a built-in boundary such as ``core`` even when there
    # is no manifest node to point at; report it in the closure by name.
    out.extend(str(dep) for dep in (_attrs(memory, nid).get("depends_on") or [])
               if str(dep))
    return sorted(set(out))


def _walk_seeds(memory: Any, start: str, *, reverse: bool,
                max_depth: int = 8) -> list[str]:
    seen = {str(start)}
    out: list[str] = []
    frontier = [(str(start), 0)]
    while frontier:
        current, depth = frontier.pop(0)
        if depth >= max_depth:
            continue
        nid = _seed_nid(memory, current)
        if nid is None:
            continue
        children = (_seed_direct_dependents(memory, nid) if reverse
                    else _seed_direct_dependencies(memory, nid))
        for child in children:
            if child in seen:
                continue
            seen.add(child)
            out.append(child)
            frontier.append((child, depth + 1))
    return out


def _seed_test_nodes(memory: Any, nid: int) -> dict[str, list[str]]:
    files: dict[str, list[str]] = {}
    for x in _in(memory, nid, "ocelot_mentions_seed"):
        node = _node(memory, x)
        if node.get("type") != "Test":
            continue
        attrs = node.get("attrs") or {}
        rel = str(attrs.get("file") or "")
        qual = str(attrs.get("qualname") or attrs.get("name") or "")
        if rel:
            files.setdefault(rel, [])
            if qual and qual not in files[rel]:
                files[rel].append(qual)
    return {rel: sorted(names) for rel, names in sorted(files.items())}


def _seed_source_files(memory: Any, nid: int) -> list[str]:
    out: list[str] = []
    for x in _in(memory, nid, "ocelot_mentions_seed"):
        if _type(memory, x) != "File":
            continue
        path = str((_attrs(memory, x)).get("path") or "")
        if (path and not path.startswith("tests/")
                and not path.endswith((".md", ".rst"))):
            out.append(path)
    return sorted(set(out))


def _seed_docs(memory: Any, nid: int) -> list[str]:
    out: list[str] = []
    for x in _in(memory, nid, "ocelot_documents"):
        attrs = _attrs(memory, x)
        path = str(attrs.get("path") or "")
        if path:
            out.append(path)
    return sorted(set(out))


def _seed_boot_files(memory: Any, nid: int) -> list[str]:
    out: list[str] = []
    for x in _in(memory, nid, "ocelot_boots"):
        path = str((_attrs(memory, x)).get("path") or "")
        if path:
            out.append(path)
    return sorted(set(out))


def _blast_radius(memory: Any, nid: int, seed_id: str) -> dict[str, Any]:
    dependents = _seed_direct_dependents(memory, nid)
    boot = _seed_boot_files(memory, nid)
    fanout = len(dependents)
    core = "substrate/boot_core.py" in boot or seed_id in KNOWN_SHARED_SEEDS
    shared = bool(core or fanout >= SHARED_FANOUT)
    return {"class": "core" if core else "shared" if shared else "local",
            "shared": shared, "fanout": fanout,
            "direct_dependents": dependents, "boot_files": boot}


def _seed_payload(memory: Any, nid: int, include: str) -> dict[str, Any]:
    attrs = _attrs(memory, nid)
    seed_id = str(attrs.get("id") or attrs.get("name") or "")
    blast = _blast_radius(memory, nid, seed_id)
    tests = _seed_test_nodes(memory, nid)
    exact = ""
    root = _root(memory)
    candidate = root / "tests" / f"test_{seed_id}_migrated.py"
    if candidate.is_file():
        exact = candidate.relative_to(root).as_posix()
    if exact:
        tests.setdefault(exact, [])
    docs = _seed_docs(memory, nid)
    sources = _seed_source_files(memory, nid)
    payload: dict[str, Any] = {
        "id": seed_id,
        "version": str(attrs.get("version") or ""),
        "path": str(attrs.get("path") or ""),
        "description": str(attrs.get("description") or ""),
        "depends_on": list(attrs.get("depends_on") or []),
        "counts": dict(attrs.get("counts") or {}),
        "term_count": len(attrs.get("terms") or []),
        "self_load_only": bool(attrs.get("self_load_only")),
        "area": attrs.get("area"),
        "partial": bool(attrs.get("partial")),
        "manifest_bytes": attrs.get("manifest_bytes"),
        "blast_radius": blast["class"],
        "shared": blast["shared"],
        "fanout": blast["fanout"],
        "test_count": len(tests),
        "exact_test": exact or None,
        "boot_files": blast["boot_files"][:MAX_BOOT_FILES],
    }
    if include in ("summary", "all", "closure"):
        payload["dependents"] = blast["direct_dependents"]
        payload["dependencies"] = _seed_direct_dependencies(memory, nid)
        payload["closure"] = [seed_id, *_walk_seeds(
            memory, seed_id, reverse=False)]
        payload["reverse_closure"] = [seed_id, *_walk_seeds(
            memory, seed_id, reverse=True)]
    if include in ("summary", "all", "tests"):
        payload["tests"] = [{"file": rel, "tests": names}
                            for rel, names in sorted(tests.items())]
    if include in ("summary", "all", "docs"):
        payload["docs"] = docs
    if include in ("summary", "all", "sources"):
        payload["sources"] = sources
    if include in ("all", "terms"):
        payload["terms"] = list(attrs.get("terms") or [])[:100]
    return payload


def seed_summary(memory: Any, seed_id: str, include: str = "summary") -> dict[str, Any]:
    """One Ocelot seed with dependencies, dependents and related files."""
    seed_id = str(seed_id or "").strip()
    if include not in ("summary", "closure", "tests", "docs", "sources",
                       "terms", "all"):
        return {"ok": False, "error": f"unknown include {include!r}"}
    nid = _seed_nid(memory, seed_id)
    if nid is None:
        return {"ok": False, "error": f"no Ocelot seed matching {seed_id!r}",
                "seeds": _seed_ids(memory)[:20]}
    payload = _seed_payload(memory, nid, include)
    return {"ok": True, **payload}


def _cycles(memory: Any) -> list[list[str]]:
    ids = _seed_ids(memory)
    graph: dict[str, list[str]] = {}
    for seed_id in ids:
        nid = _seed_nid(memory, seed_id)
        graph[seed_id] = (_seed_direct_dependencies(memory, nid)
                          if nid is not None else [])
    found: list[list[str]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for child in graph.get(node, ()):
            if child not in graph:
                continue
            if state.get(child) == 1:
                try:
                    start = stack.index(child)
                except ValueError:
                    start = 0
                cycle = stack[start:] + [child]
                key = " -> ".join(cycle)
                if all(" -> ".join(c) != key for c in found):
                    found.append(cycle)
            elif state.get(child, 0) == 0:
                visit(child)
        stack.pop()
        state[node] = 2

    for seed_id in ids:
        if state.get(seed_id, 0) == 0:
            visit(seed_id)
    return found[:50]


def seed_census(memory: Any) -> dict[str, Any]:
    """Census of indexed Ocelot seeds and their test/boot coverage."""
    root = _root(memory)
    entries: list[dict[str, Any]] = []
    stubs: list[str] = []
    partial: list[str] = []
    errors: list[Any] = []
    for nid in memory.nodes_of_type("OcelotSeed"):
        attrs = _attrs(memory, nid)
        seed_id = str(attrs.get("id") or attrs.get("name") or "")
        if attrs.get("stub"):
            stubs.append(seed_id)
            continue
        if attrs.get("partial"):
            partial.append(seed_id)
        blast = _blast_radius(memory, nid, seed_id)
        tests = _seed_test_nodes(memory, nid)
        exact = (root / "tests" / f"test_{seed_id}_migrated.py").is_file()
        entries.append({
            "id": seed_id,
            "version": str(attrs.get("version") or ""),
            "path": str(attrs.get("path") or ""),
            "fanout": blast["fanout"],
            "shared": blast["shared"],
            "blast_radius": blast["class"],
            "boot_files": blast["boot_files"],
            "test_count": len(tests),
            "exact_test": exact,
            "has_test": bool(tests or exact),
            "depends_on": list(attrs.get("depends_on") or []),
        })
    entries.sort(key=lambda item: item["id"])
    no_test = [item["id"] for item in entries if not item["has_test"]]
    no_boot = [item["id"] for item in entries if not item["boot_files"]]
    shared = [item["id"] for item in entries if item["shared"]]
    total_seeds = len(entries)
    truncated = total_seeds > MAX_CENSUS_ENTRIES
    entries = entries[:MAX_CENSUS_ENTRIES]
    repo_nid = memory.node_by_key("repo:")
    repo_attrs = _attrs(memory, repo_nid) if repo_nid is not None else {}
    errors = list(repo_attrs.get("ocelot_index_errors") or [])
    return {
        "ok": True,
        "seed_count": total_seeds,
        "stub_count": len(stubs),
        "stubs": sorted(stubs)[:50],
        "partial": sorted(partial)[:50],
        "shared": shared,
        "no_test": no_test,
        "no_boot": no_boot,
        "cycles": _cycles(memory),
        "entries": entries,
        "entries_truncated": truncated,
        "errors": errors,
        "indexed_by": "ozagia.integrations.ocelot.workspace",
    }


# --------------------------------------------------------------------------- #
# indexer
# --------------------------------------------------------------------------- #
def _seed_head(path: Path, size: int) -> dict[str, Any]:
    """Metadata from the head of a manifest too large to parse in full."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            head = stream.read(MAX_SEED_HEAD_BYTES)
    except OSError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc

    def string(key: str) -> str:
        match = re.search(rf'"{re.escape(key)}"\s*:\s*("(?:[^"\\]|\\.)*")',
                          head, re.S)
        if not match:
            return ""
        try:
            value = json.loads(match.group(1))
        except json.JSONDecodeError:
            return ""
        return value if isinstance(value, str) else ""

    depends: list[str] = []
    match = re.search(r'"depends_on"\s*:\s*\[([^\]]*)\]', head, re.S)
    if match:
        depends = re.findall(r'"([^"]+)"', match.group(1))
    self_load = re.search(r'"self_load_only"\s*:\s*(true|false)', head)
    return {
        "id": string("id"),
        "version": string("version"),
        "description": string("description"),
        "depends_on": depends,
        "self_load_only": bool(self_load and self_load.group(1) == "true"),
        "_partial": True,
        "_size": size,
    }


def _read_seed(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size > MAX_SEED_BYTES:
        return _seed_head(path, size)
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: manifest is not a JSON object")
    return data


def _count(value: Any) -> int:
    return len(value) if isinstance(value, (list, tuple, dict)) else 0


def _seed_counts(data: Mapping[str, Any]) -> dict[str, int]:
    keys = ("nodes", "edges", "productions", "rules", "concepts", "reflexes",
            "frames", "goals", "capability_keys")
    return {key: _count(data.get(key)) for key in keys}


def _seed_terms(data: Mapping[str, Any]) -> list[str]:
    """Term ``type`` names used by rules, reflexes and productions."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            kind = value.get("type")
            if isinstance(kind, str) and kind:
                found.add(kind)
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    for key in ("rules", "reflexes"):
        walk(data.get(key))
    for production in data.get("productions") or []:
        if isinstance(production, Mapping):
            walk(production.get("where"))
            walk(production.get("weight"))
    return sorted(found)[:500]


def _item_names(data: Mapping[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for key in ("rules", "concepts", "nodes", "reflexes", "frames", "goals"):
        values = data.get(key)
        if not isinstance(values, (list, tuple)) or not values:
            continue
        names: list[str] = []
        for index, value in enumerate(values):
            if isinstance(value, Mapping):
                name = (value.get("name") or value.get("id")
                        or value.get("provenance") or f"{key}#{index}")
            else:
                name = str(value)
            name = str(name)[:MAX_ITEM_NAME_CHARS]
            if name and name not in names:
                names.append(name)
            if len(names) >= MAX_ITEM_NAMES:
                break
        if names:
            out[key] = names
    return out


def _seed_attrs(seed_id: str, rel: str, data: Mapping[str, Any]) -> dict[str, Any]:
    deps = [str(x) for x in (data.get("depends_on") or []) if str(x)][:100]
    capability = [str(x) for x in (data.get("capability_keys") or [])][:50]
    return {
        "name": seed_id,
        "id": seed_id,
        "version": str(data.get("version") or ""),
        "path": rel,
        "description": str(data.get("description") or "")[:MAX_DESCRIPTION],
        "depends_on": deps,
        "self_load_only": bool(data.get("self_load_only")),
        "area": str(data.get("area") or ""),
        "counts": _seed_counts(data),
        "item_names": _item_names(data),
        "terms": _seed_terms(data),
        "capability_keys": capability,
        "partial": bool(data.get("_partial")),
        "manifest_bytes": int(data.get("_size") or 0) or None,
    }


def _patterns(ids: Iterable[str]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    values = sorted({str(x) for x in ids if x}, key=len, reverse=True)
    if not values:
        never = re.compile(r"(?!)")
        return never, never
    escaped = "|".join(re.escape(value) for value in values)
    quoted = re.compile(r"""["'](""" + escaped + r""")["']""")
    words = [value for value in values if len(value) >= 7]
    if words:
        word = re.compile(r"(?<![A-Za-z0-9_.-])(" + "|".join(
            re.escape(value) for value in words) + r")(?![A-Za-z0-9_.-])")
    else:
        word = re.compile(r"(?!)")
    return quoted, word


def _mentions(text: str, known: set[str], quoted: re.Pattern[str],
              word: re.Pattern[str]) -> set[str]:
    found = {path for path in SEED_PATH_RE.findall(text) if path in known}
    found.update(quoted.findall(text))
    found.update(word.findall(text))
    return found


def _add_seed_mentions(builder: Any, nid: int, rel: str,
                       found: set[str], seeds: Mapping[str, int]) -> None:
    if not found:
        return
    for seed_id in sorted(found):
        target = seeds.get(seed_id)
        if target is not None:
            builder.edge(nid, "ocelot_mentions_seed", target)


def index_ocelot(ctx: Any) -> None:
    """Index Ocelot seed metadata, seed references and engine-aware files."""
    b = ctx.builder
    root = Path(ctx.root).resolve()
    seed_dir = root / SEED_DIR
    seed_paths = sorted(seed_dir.glob("*.json")) if seed_dir.is_dir() else []
    seeds: dict[str, int] = {}
    errors: list[str] = []
    all_ids: list[str] = []
    for path in seed_paths:
        rel = path.relative_to(root).as_posix()
        try:
            data = _read_seed(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{rel}: {exc}")
            continue
        seed_id = str(data.get("id") or path.stem)
        if not seed_id:
            errors.append(f"{rel}: empty seed id")
            continue
        if seed_id != path.stem:
            errors.append(f"{rel}: id {seed_id!r} differs from filename")
        if seed_id in seeds:
            errors.append(f"{rel}: duplicate seed id {seed_id!r}")
        attrs = _seed_attrs(seed_id, rel, data)
        fid = ctx.file_node(rel)
        existing = b.find(_seed_key(seed_id))
        if existing is None:
            sid = b.node("OcelotSeed", attrs, key=_seed_key(seed_id))
        else:
            sid = existing
            b.attrs(sid).update(attrs)
            b.attrs(sid).pop("stub", None)
        seeds[seed_id] = sid
        all_ids.append(seed_id)
        b.edge(fid, "defines", sid)
        b.edge(sid, "defined_in", fid)
        b.edge(ctx.repo, "has_ocelot_seed", sid)
        for dep in attrs["depends_on"]:
            if dep in BUILTIN_SEEDS:
                continue
            dep_nid = b.find(_seed_key(dep))
            if dep_nid is None:
                dep_nid = b.node("OcelotSeed", {
                    "name": dep, "id": dep, "version": "", "path": "",
                    "description": "", "depends_on": [], "stub": 1,
                    "counts": {},
                }, key=_seed_key(dep))
            b.edge(sid, "ocelot_depends_on", dep_nid)

    known = set(seeds)
    quoted, word = _patterns(all_ids)

    # Boot and registry files name seeds in string literals.
    for rel in BOOT_FILES:
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        found = _mentions(text, known, quoted, word)
        if not found:
            continue
        fid = ctx.file_node(rel)
        for seed_id in sorted(found):
            sid = seeds.get(seed_id)
            if sid is None:
                continue
            b.edge(fid, "ocelot_boots", sid)
            attrs = b.attrs(sid)
            files = attrs.setdefault("boot_files", [])
            if rel not in files:
                files.append(rel)

    # Tests, docs and ordinary sources that mention a seed.
    for src in ctx.sources:
        found = _mentions(src.text or "", known, quoted, word)
        _add_seed_mentions(b, src.node, src.rel, found, seeds)
    for tf in ctx.tests:
        found = _mentions(tf.text or "", known, quoted, word)
        _add_seed_mentions(b, tf.node, tf.rel, found, seeds)
        for tid in tf.tests:
            _add_seed_mentions(b, tid, tf.rel, found, seeds)
    for doc in ctx.docs:
        found = _mentions(doc.text or "", known, quoted, word)
        for seed_id in sorted(found):
            sid = seeds.get(seed_id)
            if sid is not None:
                b.edge(doc.node, "ocelot_documents", sid)

    # Engine-aware files and the Rust source set.
    engine = b.node("OcelotEngine", {
        "name": "substrate_rs",
        "native": ENGINE_NATIVE,
        "build_record": ENGINE_BUILD_RECORD,
    }, key=("ocelot_engine",))
    b.edge(ctx.repo, "has_ocelot_engine", engine)
    for src in ctx.sources:
        text = src.text or ""
        if "substrate_rs" in text or "engine_provenance" in text:
            b.edge(src.node, "ocelot_uses_engine", engine)
        if src.rel.startswith(ENGINE_SOURCE_PREFIX):
            b.edge(src.node, "ocelot_engine_source", engine)
    for tf in ctx.tests:
        text = tf.text or ""
        if ("substrate_rs" in text or "engine_provenance" in text
                or "_native.abi3.so" in text):
            b.edge(tf.node, "ocelot_uses_engine", engine)
            for tid in tf.tests:
                b.edge(tid, "ocelot_uses_engine", engine)

    repo_attrs = b.attrs(ctx.repo)
    repo_attrs["ocelot_seeds"] = len(seeds)
    repo_attrs["ocelot_seed_manifests"] = len(seed_paths)
    repo_attrs["ocelot_index_errors"] = errors[:MAX_INDEX_ERRORS]
    repo_attrs["ocelot_index_error_count"] = len(errors)


# --------------------------------------------------------------------------- #
# test planning
# --------------------------------------------------------------------------- #
def _git(root: Path, args: list[str], *, timeout: int = 20) -> str | None:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            errors="replace", timeout=timeout, env=env, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _changed_paths(root: Path, *, base: str | None,
                   head: str | None) -> list[str]:
    if base or head:
        left = base or "HEAD"
        right = head or "HEAD"
        out = _git(root, ["diff", "--name-only", "--diff-filter=ACMR",
                          f"{left}...{right}"])
        if out is None:
            out = _git(root, ["diff", "--name-only", "--diff-filter=ACMR",
                              left, right])
        return [line for line in (out or "").splitlines() if line]
    tracked = _git(root, ["diff", "HEAD", "--name-only",
                          "--diff-filter=ACMR"])
    paths = [line for line in (tracked or "").splitlines() if line]
    untracked = _git(root, ["ls-files", "--others", "--exclude-standard"])
    for line in (untracked or "").splitlines():
        if line and line not in paths:
            paths.append(line)
    return paths


def _normalize_changed(root: Path, changed: Iterable[str]) -> list[str]:
    out: list[str] = []
    for raw in changed:
        value = str(raw).strip()
        if not value:
            continue
        path = Path(value)
        if path.is_absolute():
            try:
                value = path.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
        value = PurePosixPath(value).as_posix()
        if value.startswith("./"):
            value = value[2:]
        if not value or value == ".." or value.startswith("../"):
            continue
        if value not in out:
            out.append(value)
    return out


def _python(root: Path) -> str:
    candidate = root / ".venv" / "bin" / "python"
    return str(candidate) if candidate.is_file() else "python3"


def _tests_for_file(memory: Any, rel: str) -> list[str]:
    try:
        result = memory.tests_for(rel)
    except Exception:  # noqa: BLE001 - an absent/unmapped file is not a failure
        return []
    out: list[str] = []
    for item in result.get("tests") or []:
        if not isinstance(item, dict):
            continue
        path = item.get("file")
        if isinstance(path, str) and path and path not in out:
            out.append(path)
    return out


def _engine_tests(memory: Any) -> list[str]:
    nid = memory.node_by_key("ocelot_engine:")
    if nid is None:
        return []
    out: set[str] = set()
    for x in _in(memory, nid, "ocelot_uses_engine"):
        kind = _type(memory, x)
        attrs = _attrs(memory, x)
        if kind == "Test":
            path = str(attrs.get("file") or "")
            if path:
                out.add(path)
        elif kind == "File":
            path = str(attrs.get("path") or "")
            if path and not path.startswith("tests/"):
                out.update(_tests_for_file(memory, path))
    return sorted(out)


def _seed_tests(memory: Any, seed_id: str) -> list[str]:
    nid = _seed_nid(memory, seed_id)
    if nid is None:
        return []
    return sorted(_seed_test_nodes(memory, nid))


def _shared(seed_id: str, payload: Mapping[str, Any]) -> bool:
    return bool(payload.get("shared") or seed_id in KNOWN_SHARED_SEEDS)


def test_plan(memory: Any, changed: Iterable[str] | None = None, *,
              base: str | None = None, head: str | None = None,
              seeds: Iterable[str] | None = None) -> dict[str, Any]:
    """Plan a conservative Ocelot test slice for changed files or seeds."""
    root = _root(memory)
    changed_values = ([changed] if isinstance(changed, str)
                      else list(changed or []))
    seed_values = ([seeds] if isinstance(seeds, str)
                   else list(seeds or []))
    raw = list(changed_values)
    raw.extend(f"seeds/{str(seed).strip()}.json"
               for seed in seed_values if str(seed).strip())
    paths = _normalize_changed(root, raw)
    if not paths and base is None and head is None:
        paths = _changed_paths(root, base=None, head=None)
    elif base or head:
        paths = _normalize_changed(root, _changed_paths(root, base=base, head=head))
    tests: set[str] = set()
    reasons: list[str] = []
    warnings: list[str] = []
    full_reasons: list[str] = []
    seed_changes: list[dict[str, Any]] = []
    engine_changed = False
    graph_missing = False
    docs_only = True
    python_sources: list[str] = []
    test_changes: list[str] = []

    for rel in paths:
        pure = PurePosixPath(rel)
        parts = pure.parts
        if rel.startswith("seeds/") and len(parts) == 2 and rel.endswith(".json"):
            seed_id = pure.stem
            payload = seed_summary(memory, seed_id, include="all")
            if not payload.get("ok"):
                warnings.append(f"{rel}: no indexed seed; run `ozagia index --root .`")
                full_reasons.append(f"{rel}: seed is not in the graph")
                graph_missing = True
                continue
            seed_changes.append({"id": seed_id, "path": rel,
                                 "blast_radius": payload["blast_radius"],
                                 "shared": payload["shared"]})
            exact = payload.get("exact_test")
            seed_tests = {exact} if isinstance(exact, str) and exact else set()
            seed_tests.update(item["file"] for item in payload.get("tests") or []
                              if isinstance(item, dict) and item.get("file"))
            for dependent in payload.get("dependents") or []:
                dep = seed_summary(memory, dependent, include="tests")
                if dep.get("ok"):
                    seed_tests.update(
                        item["file"] for item in dep.get("tests") or []
                        if isinstance(item, dict) and item.get("file"))
            tests.update(seed_tests)
            docs_only = False
            reasons.append(f"{rel}: seed {seed_id} affects {len(seed_tests)} test file(s)")
            if _shared(seed_id, payload):
                full_reasons.append(f"seed {seed_id} is {payload['blast_radius']} "
                                    f"(fan-out {payload['fanout']})")
            if not seed_tests:
                warnings.append(f"{rel}: no test mentions seed {seed_id}")
                if payload.get("fanout", 0) or payload.get("boot_files"):
                    full_reasons.append(
                        f"seed {seed_id} is booted or shared but has no tests")
            continue
        if rel.startswith("tests/") and rel.endswith(".py"):
            if (root / rel).is_file():
                test_changes.append(rel)
            else:
                warnings.append(f"{rel}: deleted or renamed test file")
                full_reasons.append(f"{rel}: test file was removed")
            docs_only = False
            continue
        if rel.startswith("docs/") or pure.suffix in (".md", ".rst"):
            continue
        if rel == "runners/dsl/Cargo.toml":
            engine_changed = True
            docs_only = False
            full_reasons.append(f"{rel} changes the Rust engine manifest")
            reasons.append(f"{rel}: Rust engine manifest")
            continue
        if rel.startswith(ENGINE_SOURCE_PREFIX):
            engine_changed = True
            docs_only = False
            reasons.append(f"{rel}: Rust engine source")
            continue
        if rel in FULL_GATE_PATHS or rel.startswith(FULL_GATE_PREFIXES):
            full_reasons.append(f"{rel} is a shared/core Ocelot file")
            docs_only = False
            continue
        if rel.startswith("runners/dsl/") and rel.endswith(".rs"):
            engine_changed = True
            docs_only = False
            reasons.append(f"{rel}: Rust engine source")
            continue
        if rel.startswith(("substrate/", "domains/", "worlds/", "scripts/",
                           "tools/", "tests/fixtures/")) and rel.endswith(".py"):
            python_sources.append(rel)
            docs_only = False
            reasons.append(f"{rel}: Python source")
            continue
        if rel in ("pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini"):
            full_reasons.append(f"{rel} changes test discovery or configuration")
            docs_only = False
            continue
        full_reasons.append(f"{rel}: unclassified path")
        docs_only = False

    tests.update(test_changes)
    for rel in python_sources:
        found = _tests_for_file(memory, rel)
        tests.update(found)
        if found:
            reasons.append(f"{rel}: {len(found)} test file(s) from the graph")
        else:
            warnings.append(f"{rel}: no test link in the graph")
    if engine_changed:
        engine_tests = _engine_tests(memory)
        tests.update(engine_tests)
        if (root / ENGINE_TEST).is_file():
            tests.add(ENGINE_TEST)
        reasons.append("Rust engine change: rebuild before measuring")
        if not engine_tests and not (root / ENGINE_TEST).is_file():
            warnings.append("Rust engine change has no linked Python test")
    if len(tests) > MAX_PLAN_TESTS:
        full_reasons.append(f"selected {len(tests)} test files (cap {MAX_PLAN_TESTS})")

    mode = "full" if full_reasons else "affected" if tests else "none"
    py = _python(root)
    commands: list[str] = []
    if engine_changed:
        commands.append("bash scripts/engine_cargo_test.sh --lib -- "
                        "--test-threads=1")
    if mode == "full":
        commands.append(f"{shlex.quote(py)} -m pytest tests -k migrated -q")
    elif tests:
        commands.append(f"{shlex.quote(py)} -m pytest "
                        + " ".join(shlex.quote(path) for path in sorted(tests))
                        + " -q")
    if docs_only and not tests and not full_reasons:
        warnings.append("documentation-only change: no pytest selected")
    if not paths:
        warnings.append("no changed paths and no seed ids supplied")
    result: dict[str, Any] = {
        "ok": not graph_missing,
        "mode": mode,
        "root": str(root),
        "changed": sorted(set(paths)),
        "seeds": seed_changes,
        "tests": sorted(tests),
        "test_count": len(tests),
        "commands": commands,
        "cwd": str(root),
        "python": py,
        "engine_rebuild": bool(engine_changed),
        "full_gate": bool(full_reasons),
        "full_command": (f"cd {shlex.quote(str(root))} && "
                         f"{shlex.quote(py)} -m pytest tests -k migrated -q"),
        "reasons": reasons,
        "full_reasons": full_reasons,
        "warnings": warnings,
    }
    if not paths and not seed_changes:
        result["ok"] = True
    return result


# --------------------------------------------------------------------------- #
# adapter surface
# --------------------------------------------------------------------------- #
def _tool_seed(memory: Any, args: Mapping[str, Any]) -> Any:
    return seed_summary(memory, str(args.get("id") or ""),
                        include=str(args.get("include") or "summary"))


def _tool_seeds(memory: Any, args: Mapping[str, Any]) -> Any:
    return seed_census(memory)


def _tool_plan(memory: Any, args: Mapping[str, Any]) -> Any:
    return test_plan(memory, changed=args.get("changed"),
                     base=args.get("base"), head=args.get("head"),
                     seeds=args.get("seeds"))


def _tool_status(memory: Any, args: Mapping[str, Any]) -> Any:
    from .workspace_doctor import status
    return status(memory.raw_repo_root)


SEED_TOOL = {
    "name": "ocelot_seed",
    "description": ("Ocelot seed metadata, dependency closure, reverse "
                    "dependents, tests, docs and boot references."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "include": {"type": "string",
                        "enum": ["summary", "closure", "tests", "docs",
                                 "sources", "terms", "all"]},
        },
        "required": ["id"],
        "additionalProperties": False,
    },
}
SEEDS_TOOL = {
    "name": "ocelot_seeds",
    "description": ("Ocelot seed census: shared seeds, missing tests, "
                    "dependency cycles, stubs and boot coverage."),
    "inputSchema": {"type": "object", "properties": {},
                    "additionalProperties": False},
}
PLAN_TOOL = {
    "name": "ocelot_plan",
    "description": ("Conservative Ocelot test plan for changed files or seed "
                    "ids, with full-gate fallbacks for shared/core changes."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "changed": {"type": "array", "items": {"type": "string"}},
            "base": {"type": "string"},
            "head": {"type": "string"},
            "seeds": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    },
}
STATUS_TOOL = {
    "name": "ocelot_status",
    "description": ("Read-only Ocelot worktree/engine doctor: native artifact, "
                    "build record, Rust source match, datasets and baseline."),
    "inputSchema": {"type": "object", "properties": {},
                    "additionalProperties": False},
}


def register(registry: Any) -> None:
    """Register the Ocelot workspace indexer, queries and MCP tools."""
    registry.add_indexer(
        index_ocelot,
        watch=(SEED_GLOB, *BOOT_FILES, ENGINE_BUILD_RECORD),
        texts=MENTION_TEXT_RE,
    )
    registry.add_graph_query(
        "ocelot_seed", lambda memory, **kw: seed_summary(
            memory, str(kw.get("id") or ""),
            include=str(kw.get("include") or "summary")))
    registry.add_graph_query("ocelot_seeds",
                             lambda memory, **kw: seed_census(memory))
    registry.add_graph_query(
        "ocelot_plan", lambda memory, **kw: test_plan(
            memory, changed=kw.get("changed"), base=kw.get("base"),
            head=kw.get("head"), seeds=kw.get("seeds")))
    registry.add_graph_query(
        "ocelot_status", lambda memory, **kw: _tool_status(memory, {}))
    registry.add_mcp_tool(SEED_TOOL, _tool_seed)
    registry.add_mcp_tool(SEEDS_TOOL, _tool_seeds)
    registry.add_mcp_tool(PLAN_TOOL, _tool_plan)
    registry.add_mcp_tool(STATUS_TOOL, _tool_status)
    # The bridge and research map are part of the same profile. Import lazily
    # so the base seed/test/doctor tools still load if an optional layer is
    # unavailable in a partial checkout.
    try:
        from .workspace_engine import register_engine
        register_engine(registry)
    except ImportError:  # pragma: no cover - partial source tree
        pass
    try:
        from .workspace_research import register_research
        register_research(registry)
    except ImportError:  # pragma: no cover - partial source tree
        pass
    try:
        from .workspace_validate import register_validate
        register_validate(registry)
    except ImportError:  # pragma: no cover - partial source tree
        pass


__all__ = [
    "BOOT_FILES",
    "FULL_GATE_COMMAND",
    "index_ocelot",
    "register",
    "seed_census",
    "seed_summary",
    "test_plan",
]
