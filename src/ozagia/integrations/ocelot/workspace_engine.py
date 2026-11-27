"""Syntactic Rust/Python/seed bridge for an Ocelot checkout.

The bridge is read-only and never imports or executes Ocelot. It reads a
bounded set of Rust sources under ``runners/dsl/src``, the Python files that
import ``substrate_rs``, and the top-level seed manifests that the workspace
adapter already indexed. From those it builds:

- ``OcelotPrimitive`` nodes for ``#[pyfunction]``, ``#[pyclass]`` and simple
  ``#[pymethods]`` methods, plus Python uses that resolve to them;
- ``OcelotTerm`` nodes for ``enum TermNode`` variants and the wire names in
  ``jsonio.rs``;
- ``File -ocelot_defines_primitive/term->``, ``File/Test
  -ocelot_uses_primitive->`` and ``OcelotSeed -ocelot_uses_term->`` edges.

It registers the indexer, the ``ocelot_engine`` graph query and MCP tool::

    [adapters]
    modules = [
      "ozagia.integrations.ocelot.workspace:register",
      "ozagia.integrations.ocelot.workspace_engine:register_engine",
    ]

Register the workspace adapter first so ``OcelotSeed`` nodes exist; the bridge
does not create or edit seed manifests.
"""
from __future__ import annotations

import ast
import json
import re
from bisect import bisect_right
from pathlib import Path
from typing import Any, Iterable, Mapping

RUST_SOURCE_DIR = "runners/dsl/src"
RUST_GLOB = "runners/dsl/src/*.rs"
RUST_RECURSIVE_GLOB = "runners/dsl/src/**/*.rs"
PYTHON_ENGINE_GLOB = "runners/dsl/python/substrate_rs/*.py"
SEED_GLOB = "seeds/*.json"
TERM_FILE = "runners/dsl/src/term.rs"
JSONIO_FILE = "runners/dsl/src/jsonio.rs"
TEXT_RE = re.compile(r"substrate_rs|TermNode")

MAX_RUST_FILES = 400
MAX_RUST_BYTES = 4 * 1024 * 1024
MAX_SEED_BYTES = 32 * 1024 * 1024
MAX_BRIDGE_ERRORS = 20
MAX_ITEMS = 200
MAX_TOP_TERMS = 20
MAX_ERROR_CHARS = 300

BUILTIN_PRIMITIVE_PREFIX = "substrate_rs."
PYTHON_ENGINE_MODULES = frozenset({
    "audio", "bitstream", "communication", "ext", "speech", "video",
    "vision", "_native",
})

_PYFUNCTION_ATTR_RE = re.compile(r"#\[\s*pyfunction\b([^\]]*)\]", re.S)
_PYCLASS_ATTR_RE = re.compile(r"#\[\s*pyclass\b([^\]]*)\]", re.S)
_PYMETHODS_ATTR_RE = re.compile(r"#\[\s*pymethods\b[^\]]*\]", re.S)
_NAME_ATTR_RE = re.compile(r"\bname\s*=\s*\"([^\"]+)\"")
_FN_RE = re.compile(r"\bfn\s+([A-Za-z_]\w*)")
_STRUCT_RE = re.compile(r"\b(?:struct|enum|union)\s+([A-Za-z_]\w*)")
_ENUM_TERM_RE = re.compile(r"\benum\s+TermNode\b[^{;]*\{", re.S)
_WIRE_RE = re.compile(
    r"\bTermNode::([A-Za-z_]\w*)\b[^\n=]*?=>\s*(?:Some\()?\s*\"([^\"]+)\"")
_WIRE_BLOCK_RE = re.compile(
    r"\bTermNode::([A-Za-z_]\w*)\b[^\n=]*?=>\s*\{\s*\"([^\"]+)\"", re.S)
_METHOD_LINE_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?"
    r"fn\s+([A-Za-z_]\w*)")
_IMPORT_ATTR_RE = re.compile(
    r"\bsubstrate_rs(?:\s*\.\s*_native)?\s*\.\s*([A-Za-z_]\w*)")
_FROM_IMPORT_RE = re.compile(
    r"\bfrom\s+substrate_rs(?:\s*\.\s*_native)?\s+import\s+([^\n]+)")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _line_starts(text: str) -> list[int]:
    starts = [0]
    starts.extend(match.end() for match in re.finditer("\n", text))
    return starts


def _line_at(starts: list[int], pos: int) -> int:
    return bisect_right(starts, max(0, int(pos)))


def _short(value: Any) -> str:
    return str(value)[:MAX_ERROR_CHARS]


def _node(memory: Any, nid: int) -> dict[str, Any]:
    return memory.node(int(nid)) or {}


def _attrs(memory: Any, nid: int) -> dict[str, Any]:
    return dict((_node(memory, nid).get("attrs") or {}))


def _type(memory: Any, nid: int) -> str:
    return str(_node(memory, nid).get("type") or "")


def _out(memory: Any, nid: int, edge: str) -> list[int]:
    return [int(x) for x in memory._neighbours(nid, edge)]


def _in(memory: Any, nid: int, edge: str) -> list[int]:
    return [int(x) for x in memory._neighbours(nid, edge, incoming=True)]


def _repo_attrs(memory: Any) -> dict[str, Any]:
    for nid in memory.nodes_of_type("Repo"):
        return _attrs(memory, nid)
    return {}


def _primitive_key(name: str) -> tuple[str, str]:
    return ("ocelot_primitive", str(name))


def _term_key(name: str) -> tuple[str, str]:
    return ("ocelot_term", str(name))


def _seed_key(seed_id: str) -> tuple[str, str]:
    return ("ocelot_seed", str(seed_id))


def _seed_id(memory: Any, nid: int) -> str:
    attrs = _attrs(memory, nid)
    for key in ("id", "seed_id", "name"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    key = memory.key_of(nid) or ""
    return key.split(":", 1)[1] if key.startswith("ocelot_seed:") else ""


def _seed_path(memory: Any, seed_id: str) -> str:
    nid = memory.node_by_key("ocelot_seed:" + str(seed_id))
    if nid is not None:
        attrs = _attrs(memory, nid)
        path = str(attrs.get("path") or "")
        if path:
            return path
    return f"seeds/{seed_id}.json"


# --------------------------------------------------------------------------- #
# Rust parsing
# --------------------------------------------------------------------------- #
def _skip_rust_string(text: str, start: int) -> int:
    """Index after the quoted string that starts at *start*."""
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return len(text)


def _skip_rust_attribute(text: str, start: int) -> int:
    """Index after ``#[...]``/``#![...]`` at *start* (nested brackets ok)."""
    i = start
    while i < len(text) and text[i] != "[":
        i += 1
    if i >= len(text):
        return len(text)
    depth = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            i = _skip_rust_string(text, i)
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _brace_body(text: str, open_pos: int) -> tuple[str, int]:
    """The text inside the brace at *open_pos*, and the closing brace index."""
    depth = 0
    i = open_pos
    while i < len(text):
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end + 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        ch = text[i]
        if ch == '"':
            i = _skip_rust_string(text, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_pos + 1:i], i
        i += 1
    return text[open_pos + 1:], len(text)


def _brace_delta(line: str) -> int:
    depth = 0
    i = 0
    while i < len(line):
        if line.startswith("//", i):
            break
        if line.startswith("/*", i):
            end = line.find("*/", i + 2)
            i = len(line) if end < 0 else end + 2
            continue
        ch = line[i]
        if ch == '"':
            i = _skip_rust_string(line, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return depth


def _next_declaration(text: str, start: int, pattern: re.Pattern[str],
                      limit: int = 1200) -> tuple[str, int] | None:
    window = text[start:start + limit]
    match = pattern.search(window)
    if match is None:
        return None
    return match.group(1), start + match.start(1)


def _impl_type_name(header: str) -> str:
    value = re.sub(r"<[^<>]*>", "", header).strip()
    if " for " in value:
        value = value.split(" for ", 1)[1].strip()
    value = value.split(" where ", 1)[0].strip()
    value = re.sub(r"<[^<>]*>", "", value).strip()
    if "::" in value:
        value = value.rsplit("::", 1)[-1].strip()
    match = re.match(r"([A-Za-z_]\w*)", value)
    return match.group(1) if match else ""


def _parse_pymethods(text: str, starts: list[int],
                     class_names: Mapping[str, str] | None = None
                     ) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for attr in _PYMETHODS_ATTR_RE.finditer(text):
        impl = re.search(r"\bimpl\b([^{;]*)\{", text[attr.end():], re.S)
        if impl is None:
            continue
        class_name = _impl_type_name(impl.group(1))
        if not class_name:
            continue
        if class_names:
            class_name = class_names.get(class_name, class_name)
        open_pos = attr.end() + impl.end() - 1
        body, _close = _brace_body(text, open_pos)
        body_start = open_pos + 1
        depth = 0
        offset = 0
        for line in body.splitlines(keepends=True):
            if depth == 0:
                match = _METHOD_LINE_RE.match(line)
                if match is not None:
                    out.append({
                        "name": f"{class_name}.{match.group(1)}",
                        "kind": "pymethod",
                        "line": _line_at(starts, body_start + offset),
                    })
            depth += _brace_delta(line)
            offset += len(line)
    return out


def _parse_rust_primitives(text: str, starts: list[int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    class_names: dict[str, str] = {}
    for attr in _PYFUNCTION_ATTR_RE.finditer(text):
        name_match = _NAME_ATTR_RE.search(attr.group(1))
        if name_match is not None:
            name = name_match.group(1)
            pos = attr.start(1) + name_match.start(1)
        else:
            found = _next_declaration(text, attr.end(), _FN_RE)
            if found is None:
                continue
            name, pos = found
        out.append({"name": name, "kind": "pyfunction",
                    "line": _line_at(starts, pos)})
    for attr in _PYCLASS_ATTR_RE.finditer(text):
        struct = _next_declaration(text, attr.end(), _STRUCT_RE)
        name_match = _NAME_ATTR_RE.search(attr.group(1))
        if name_match is not None:
            name = name_match.group(1)
            pos = attr.start(1) + name_match.start(1)
        elif struct is not None:
            name, pos = struct
        else:
            continue
        if struct is not None:
            class_names[struct[0]] = name
        out.append({"name": name, "kind": "pyclass",
                    "line": _line_at(starts, pos)})
    out.extend(_parse_pymethods(text, starts, class_names))
    return out


def _first_variant_name(segment: str) -> tuple[str, int] | None:
    """The first code identifier in a top-level enum segment."""
    i = 0
    while i < len(segment):
        if segment.startswith("//", i):
            end = segment.find("\n", i)
            i = len(segment) if end < 0 else end + 1
            continue
        if segment.startswith("/*", i):
            end = segment.find("*/", i + 2)
            i = len(segment) if end < 0 else end + 2
            continue
        if segment.startswith("#[", i) or segment.startswith("#![", i):
            i = _skip_rust_attribute(segment, i)
            continue
        if segment[i].isspace() or segment[i] == ",":
            i += 1
            continue
        match = re.match(r"(?:pub\s+)?([A-Za-z_]\w*)", segment[i:])
        if match is None:
            return None
        return match.group(1), i + match.start(1)
    return None


def _top_level_segments(body: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    start = 0
    depth = 0
    i = 0
    while i < len(body):
        if body.startswith("//", i):
            end = body.find("\n", i)
            i = len(body) if end < 0 else end + 1
            continue
        if body.startswith("/*", i):
            end = body.find("*/", i + 2)
            i = len(body) if end < 0 else end + 2
            continue
        ch = body[i]
        if ch == '"':
            i = _skip_rust_string(body, i)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            out.append((body[start:i], start))
            start = i + 1
        i += 1
    out.append((body[start:], start))
    return out


def _parse_term_variants(text: str, starts: list[int]) -> list[dict[str, Any]]:
    match = _ENUM_TERM_RE.search(text)
    if match is None:
        return []
    body, _close = _brace_body(text, match.end() - 1)
    body_start = match.end()
    out: list[dict[str, Any]] = []
    for segment, offset in _top_level_segments(body):
        found = _first_variant_name(segment)
        if found is None:
            continue
        name, pos = found
        out.append({"variant": name,
                    "line": _line_at(starts, body_start + offset + pos)})
    return out


def _parse_wire_names(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _WIRE_RE.finditer(text):
        out.setdefault(match.group(1), match.group(2))
    for match in _WIRE_BLOCK_RE.finditer(text):
        out.setdefault(match.group(1), match.group(2))
    return out


def _rust_sources(root: Path, errors: list[str]) -> list[tuple[str, str, list[int]]]:
    source_dir = root / RUST_SOURCE_DIR
    if not source_dir.is_dir():
        return []
    paths = sorted(path for path in source_dir.rglob("*.rs") if path.is_file())
    if len(paths) > MAX_RUST_FILES:
        errors.append(
            f"{RUST_SOURCE_DIR}: {len(paths)} Rust files exceeds limit "
            f"{MAX_RUST_FILES}; only the first {MAX_RUST_FILES} were read")
        paths = paths[:MAX_RUST_FILES]
    out: list[tuple[str, str, list[int]]] = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError as exc:
            errors.append(f"{rel}: {_short(exc)}")
            continue
        if size > MAX_RUST_BYTES:
            errors.append(
                f"{rel}: {size} bytes exceeds Rust read limit "
                f"{MAX_RUST_BYTES}; skipped")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"{rel}: {_short(exc)}")
            continue
        out.append((rel, text, _line_starts(text)))
    return out


# --------------------------------------------------------------------------- #
# builder helpers
# --------------------------------------------------------------------------- #
def _ensure_primitive(builder: Any, name: str, *, kind: str = "",
                      rust_path: str = "", line: int = 0,
                      defined: bool = False) -> int:
    key = _primitive_key(name)
    nid = builder.find(key)
    if nid is None:
        return builder.node("OcelotPrimitive", {
            "name": name,
            "kind": kind or ("used" if not defined else "unknown"),
            "rust_path": rust_path,
            "line": int(line),
            "defined": bool(defined),
        }, key=key)
    attrs = builder.attrs(nid)
    attrs.setdefault("name", name)
    if defined:
        if not attrs.get("defined"):
            attrs.update({
                "kind": kind or attrs.get("kind") or "unknown",
                "rust_path": rust_path,
                "line": int(line),
                "defined": True,
            })
        else:
            primary = {"rust_path": attrs.get("rust_path"),
                       "line": int(attrs.get("line") or 0),
                       "kind": attrs.get("kind")}
            entry = {"rust_path": rust_path, "line": int(line), "kind": kind}
            if rust_path and entry != primary:
                definitions = attrs.setdefault("definitions", [])
                if entry not in definitions:
                    definitions.append(entry)
    elif not attrs.get("kind"):
        attrs["kind"] = "used"
    return nid


def _ensure_term(builder: Any, names: Mapping[str, str], name: str, *,
                 rust_path: str = "", line: int = 0,
                 defined: bool = False) -> int:
    canonical = names.get(name, name)
    key = _term_key(canonical)
    nid = builder.find(key)
    aliases = [] if name == canonical else [name]
    if nid is None:
        return builder.node("OcelotTerm", {
            "name": canonical,
            "wire": canonical,
            "wire_name": canonical,
            "aliases": aliases,
            "rust_path": rust_path,
            "line": int(line),
            "defined": bool(defined),
        }, key=key)
    attrs = builder.attrs(nid)
    attrs.setdefault("name", canonical)
    attrs.setdefault("wire", canonical)
    attrs.setdefault("wire_name", canonical)
    known = attrs.setdefault("aliases", [])
    for alias in aliases:
        if alias not in known:
            known.append(alias)
    if defined:
        attrs["defined"] = True
        if rust_path:
            attrs.setdefault("rust_path", rust_path)
            attrs.setdefault("line", int(line))
    return nid


def _term_alias_map(builder: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    for nid in builder.find_nodes("OcelotTerm"):
        attrs = builder.attrs(nid)
        name = str(attrs.get("name") or attrs.get("wire") or "")
        if not name:
            continue
        names[name] = name
        aliases = attrs.get("aliases") or []
        if isinstance(aliases, (list, tuple, set)):
            for alias in aliases:
                names.setdefault(str(alias), name)
        variant = attrs.get("variant")
        if isinstance(variant, str) and variant:
            names.setdefault(variant, name)
    return names


def _add_definition_edges(builder: Any, file_nid: int, edge: str,
                          nids: Iterable[int]) -> None:
    for nid in nids:
        builder.edge(file_nid, edge, nid)


# --------------------------------------------------------------------------- #
# Python scanning
# --------------------------------------------------------------------------- #
def _attribute_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    current: ast.AST | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        parts.reverse()
        return parts
    return []


def _python_uses_ast(text: str) -> set[str] | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    names: set[str] = set()
    package_aliases = {"substrate_rs"}
    native_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                alias_name = alias.asname or alias.name
                if alias.name == "substrate_rs":
                    package_aliases.add(alias_name)
                elif alias.name == "substrate_rs._native":
                    native_aliases.add(alias_name)
        elif isinstance(node, ast.ImportFrom):
            if node.module not in ("substrate_rs", "substrate_rs._native"):
                continue
            for alias in node.names:
                if alias.name in ("*", "_native"):
                    if alias.name == "_native":
                        native_aliases.add(alias.asname or "_native")
                    continue
                names.add(alias.name)
        elif isinstance(node, ast.Attribute):
            chain = _attribute_chain(node)
            if len(chain) < 2:
                continue
            head, parts = chain[0], chain[1:]
            if head in package_aliases:
                if parts and parts[0] == "_native":
                    parts = parts[1:]
            elif head in native_aliases:
                pass
            else:
                continue
            if parts and parts[0] not in ("_native", "substrate_rs"):
                names.add(parts[0])
    return names


def _python_uses_regex(text: str) -> set[str]:
    names: set[str] = set()
    for match in _IMPORT_ATTR_RE.finditer(text):
        if match.group(1) != "_native":
            names.add(match.group(1))
    for match in _FROM_IMPORT_RE.finditer(text):
        body = match.group(1)
        body = body.split("#", 1)[0]
        for part in body.split(","):
            part = part.strip()
            if not part or part.startswith("*"):
                continue
            name = part.split(" as ", 1)[0].strip().strip("()")
            if re.fullmatch(r"[A-Za-z_]\w*", name) and name != "_native":
                names.add(name)
    return names


def _python_primitive_uses(text: str) -> set[str]:
    if not text:
        return set()
    names = _python_uses_ast(text)
    if names is None:
        names = _python_uses_regex(text)
    return {name for name in names if name and name != "_native"}


# --------------------------------------------------------------------------- #
# seed scanning
# --------------------------------------------------------------------------- #
def _seed_term_names(data: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            item = value.get("type")
            if isinstance(item, str) and item.strip():
                names.add(item.strip())
            for child in value.values():
                walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    for key in ("rules", "reflexes"):
        if key in data:
            walk(data[key])
    # A production's top-level `type` is an endpoint node type, not a Term.
    for production in data.get("productions") or []:
        if isinstance(production, Mapping):
            walk(production.get("where"))
            walk(production.get("weight"))
    return names


def _seed_manifests(ctx: Any) -> list[tuple[int, str, str]]:
    """Existing ``OcelotSeed`` nodes as ``(node id, seed id, path)``."""
    out: list[tuple[int, str, str]] = []
    for nid in ctx.builder.find_nodes("OcelotSeed"):
        attrs = ctx.builder.attrs(nid)
        seed_id = str(attrs.get("id") or attrs.get("name") or "")
        if not seed_id:
            key = ctx.builder.nodes[nid].get("key") or ""
            if key.startswith("ocelot_seed:"):
                seed_id = key.split(":", 1)[1]
        if not seed_id:
            continue
        rel = str(attrs.get("path") or "")
        if not rel:
            if attrs.get("stub"):
                continue
            rel = f"seeds/{seed_id}.json"
        out.append((nid, seed_id, rel))
    return sorted(set(out), key=lambda item: (item[1], item[2]))


# --------------------------------------------------------------------------- #
# indexer
# --------------------------------------------------------------------------- #
def index_engine_bridge(ctx: Any) -> None:
    """Index Ocelot's Rust primitives, Python uses and seed Term uses."""
    builder = ctx.builder
    root = Path(ctx.root).resolve()
    errors: list[str] = []

    rust_files = _rust_sources(root, errors)
    variants: list[dict[str, Any]] = []
    wires: dict[str, str] = {}
    rust_edges: dict[str, list[int]] = {}
    for rel, text, starts in rust_files:
        primitives = _parse_rust_primitives(text, starts)
        file_nid = ctx.file_node(rel) if primitives or "TermNode" in text else None
        for item in primitives:
            nid = _ensure_primitive(
                builder, item["name"], kind=item["kind"],
                rust_path=rel, line=item["line"], defined=True)
            rust_edges.setdefault(rel, []).append(nid)
        if file_nid is not None and primitives:
            _add_definition_edges(builder, file_nid,
                                  "ocelot_defines_primitive",
                                  rust_edges.get(rel, []))
        if rel == TERM_FILE or _ENUM_TERM_RE.search(text):
            for item in _parse_term_variants(text, starts):
                variants.append({**item, "rust_path": rel})
        if rel == JSONIO_FILE or "TermNode::" in text:
            for variant, wire in _parse_wire_names(text).items():
                wires.setdefault(variant, wire)

    term_names: dict[str, str] = {}
    for item in variants:
        variant = item["variant"]
        wire = wires.get(variant, variant)
        term_names.setdefault(variant, wire)
        term_names.setdefault(wire, wire)
    term_labels = set(term_names)
    for item in variants:
        variant = item["variant"]
        wire = wires.get(variant, variant)
        nid = _ensure_term(builder, term_names, wire,
                           rust_path=item["rust_path"], line=item["line"],
                           defined=True)
        attrs = builder.attrs(nid)
        attrs["variant"] = variant
        aliases = attrs.setdefault("aliases", [])
        if variant != wire and variant not in aliases:
            aliases.append(variant)
        file_nid = ctx.file_node(item["rust_path"])
        builder.edge(file_nid, "ocelot_defines_term", nid)

    # Python imports/calls of the extension module.
    def primitive_names(text: str) -> set[str]:
        return {name for name in _python_primitive_uses(text)
                if name not in term_labels and name not in PYTHON_ENGINE_MODULES}

    for src in ctx.sources:
        if not src.rel.endswith(".py"):
            continue
        for name in sorted(primitive_names(src.text or "")):
            pid = _ensure_primitive(builder, name, defined=False)
            builder.edge(src.node, "ocelot_uses_primitive", pid)
    for test in ctx.tests:
        if not test.rel.endswith(".py"):
            continue
        uses = primitive_names(test.text or "")
        for name in sorted(uses):
            pid = _ensure_primitive(builder, name, defined=False)
            builder.edge(test.node, "ocelot_uses_primitive", pid)
            for tid in test.tests:
                builder.edge(tid, "ocelot_uses_primitive", pid)

    # Seed rule/reflex/production Term names, using the already indexed nodes.
    aliases = _term_alias_map(builder)
    for seed_nid, seed_id, rel in _seed_manifests(ctx):
        path = root / rel
        try:
            size = path.stat().st_size
        except OSError as exc:
            errors.append(f"{rel}: {_short(exc)}")
            continue
        if size > MAX_SEED_BYTES:
            errors.append(
                f"{rel}: {size} bytes exceeds seed read limit "
                f"{MAX_SEED_BYTES}; term uses skipped")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8",
                                             errors="replace"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{rel}: {_short(exc)}")
            continue
        if not isinstance(data, Mapping):
            errors.append(f"{rel}: manifest is not a JSON object")
            continue
        for name in sorted(_seed_term_names(data)):
            tid = _ensure_term(builder, aliases, name, defined=False)
            builder.edge(seed_nid, "ocelot_uses_term", tid)

    repo_attrs = builder.attrs(ctx.repo)
    repo_attrs["ocelot_primitive_count"] = builder.count("OcelotPrimitive")
    repo_attrs["ocelot_term_count"] = builder.count("OcelotTerm")
    repo_attrs["ocelot_bridge_errors"] = errors[:MAX_BRIDGE_ERRORS]
    repo_attrs["ocelot_bridge_error_count"] = len(errors)
    # Short names are what the bridge contract documents; keep the namespaced
    # forms too so they sit alongside the workspace adapter's attrs.
    repo_attrs["primitive_count"] = repo_attrs["ocelot_primitive_count"]
    repo_attrs["term_count"] = repo_attrs["ocelot_term_count"]
    repo_attrs["bridge_errors"] = repo_attrs["ocelot_bridge_errors"]
    repo_attrs["bridge_error_count"] = repo_attrs["ocelot_bridge_error_count"]


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #
def _normalize_primitive(target: str) -> str:
    value = str(target or "").strip()
    for prefix in ("substrate_rs._native.", "substrate_rs::_native::",
                   "substrate_rs.", "substrate_rs::", "_native."):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value.endswith("()"):
        value = value[:-2]
    return value.strip()


def _find_primitive(memory: Any, target: str) -> int | None:
    name = _normalize_primitive(target)
    if not name:
        return None
    nid = memory.node_by_key("ocelot_primitive:" + name)
    if nid is not None:
        return nid
    lowered = name.casefold()
    for candidate in memory.nodes_of_type("OcelotPrimitive"):
        attrs = _attrs(memory, candidate)
        if str(attrs.get("name") or "").casefold() == lowered:
            return candidate
    return None


def _term_label(memory: Any, nid: int) -> str:
    attrs = _attrs(memory, nid)
    return str(attrs.get("name") or attrs.get("wire") or memory.label(nid))


def _find_term(memory: Any, target: str) -> int | None:
    name = str(target or "").strip()
    if not name:
        return None
    nid = memory.node_by_key("ocelot_term:" + name)
    if nid is not None:
        return nid
    lowered = name.casefold()
    for candidate in memory.nodes_of_type("OcelotTerm"):
        attrs = _attrs(memory, candidate)
        values = [attrs.get("name"), attrs.get("wire"), attrs.get("variant")]
        aliases = attrs.get("aliases") or []
        if isinstance(aliases, (list, tuple, set)):
            values.extend(aliases)
        if any(str(value or "").casefold() == lowered for value in values):
            return candidate
    return None


def _primitive_usage(memory: Any, pid: int) -> tuple[list[str], list[str],
                                                     list[str]]:
    """``(python files, test files, test ids)`` that use *pid*."""
    python_files: set[str] = set()
    test_files: set[str] = set()
    test_ids: set[str] = set()
    for nid in _in(memory, pid, "ocelot_uses_primitive"):
        node_type = _type(memory, nid)
        attrs = _attrs(memory, nid)
        if node_type == "Test":
            rel = str(attrs.get("file") or "")
            qual = str(attrs.get("qualname") or attrs.get("name") or "")
            if rel:
                test_files.add(rel)
                test_ids.add(f"{rel}::{qual}" if qual else rel)
        elif node_type == "File":
            rel = str(attrs.get("path") or "")
            if rel:
                if rel.startswith("tests/"):
                    test_files.add(rel)
                else:
                    python_files.add(rel)
    return sorted(python_files), sorted(test_files), sorted(test_ids)


def _term_seeds(memory: Any, tid: int) -> set[str]:
    out: set[str] = set()
    for nid in _in(memory, tid, "ocelot_uses_term"):
        if _type(memory, nid) == "OcelotSeed":
            seed_id = _seed_id(memory, nid)
            if seed_id:
                out.add(seed_id)
    return out


def _norm_words(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _camel_words(value: str) -> str:
    return "".join(part[:1].upper() + part[1:]
                   for part in re.split(r"[^A-Za-z0-9]+", value) if part)


def _term_matches_primitive(memory: Any, tid: int, primitive: str) -> bool:
    attrs = _attrs(memory, tid)
    values = [attrs.get("name"), attrs.get("wire")]
    aliases = attrs.get("aliases") or []
    if isinstance(aliases, (list, tuple, set)):
        values.extend(aliases)
    wanted = {_norm_words(primitive), _norm_words(_camel_words(primitive))}
    for value in values:
        text = str(value or "")
        if not text:
            continue
        if text.casefold() == primitive.casefold():
            return True
        if _norm_words(text) in wanted:
            return True
    return False


def _primitive_terms(memory: Any, pid: int) -> list[int]:
    attrs = _attrs(memory, pid)
    name = str(attrs.get("name") or "")
    if not name:
        return []
    leaf = name.rsplit(".", 1)[-1]
    return [
        nid for nid in memory.nodes_of_type("OcelotTerm")
        if _term_matches_primitive(memory, nid, leaf)
    ]


def _term_resources(memory: Any, seed_ids: Iterable[str]) -> list[str]:
    return sorted({_seed_path(memory, seed_id) for seed_id in seed_ids})


def _primitive_payload(memory: Any, target: str) -> dict[str, Any]:
    pid = _find_primitive(memory, target)
    if pid is None:
        return {
            "ok": False,
            "primitive": str(target),
            "error": f"unknown Ocelot primitive {target!r}",
            "definitions": [],
            "python_files": [],
            "tests": [],
            "seeds": [],
            "terms": [],
            "resources": [],
        }
    attrs = _attrs(memory, pid)
    name = str(attrs.get("name") or target)
    definitions: list[dict[str, Any]] = []
    if attrs.get("defined"):
        definitions.append({
            "rust_path": str(attrs.get("rust_path") or ""),
            "line": int(attrs.get("line") or 0),
            "kind": str(attrs.get("kind") or ""),
        })
    extras = attrs.get("definitions") or []
    if isinstance(extras, (list, tuple)):
        for item in extras:
            if isinstance(item, Mapping) and item not in definitions:
                definitions.append(dict(item))
    python_files, test_files, test_ids = _primitive_usage(memory, pid)
    term_ids = _primitive_terms(memory, pid)
    terms = sorted({_term_label(memory, tid) for tid in term_ids})
    seeds: set[str] = set()
    for tid in term_ids:
        seeds.update(_term_seeds(memory, tid))
    resources = set(python_files) | set(test_files)
    resources.update(str(item.get("rust_path") or "") for item in definitions)
    resources.update(_term_resources(memory, seeds))
    return {
        "ok": True,
        "primitive": name,
        "kind": str(attrs.get("kind") or ""),
        "defined": bool(attrs.get("defined")),
        "rust_path": str(attrs.get("rust_path") or ""),
        "line": int(attrs.get("line") or 0),
        "definitions": definitions,
        "python_files": python_files,
        "test_files": test_files,
        "tests": sorted(set(test_files) | set(test_ids)),
        "seeds": sorted(seeds),
        "terms": terms,
        "resources": sorted(path for path in resources if path),
    }


def _primitive_list(memory: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for nid in sorted(memory.nodes_of_type("OcelotPrimitive"),
                      key=lambda item: str(_attrs(memory, item).get("name")
                                           or "")):
        attrs = _attrs(memory, nid)
        name = str(attrs.get("name") or "")
        if not name:
            continue
        python_files, test_files, test_ids = _primitive_usage(memory, nid)
        out.append({
            "name": name,
            "kind": str(attrs.get("kind") or ""),
            "defined": bool(attrs.get("defined")),
            "rust_path": str(attrs.get("rust_path") or ""),
            "line": int(attrs.get("line") or 0),
            "python_files": len(python_files),
            "tests": len(test_ids or test_files),
        })
        if len(out) >= MAX_ITEMS:
            break
    return out


def _term_census(memory: Any) -> dict[str, Any]:
    defined: set[str] = set()
    term_ids: dict[str, int] = {}
    for nid in memory.nodes_of_type("OcelotTerm"):
        attrs = _attrs(memory, nid)
        name = str(attrs.get("name") or attrs.get("wire") or "")
        if not name:
            continue
        term_ids[name] = nid
        if attrs.get("defined"):
            defined.add(name)
    used: set[str] = set()
    seed_terms: dict[str, set[str]] = {}
    term_seed_counts: dict[str, int] = {}
    for nid in memory.nodes_of_type("OcelotSeed"):
        seed_id = _seed_id(memory, nid)
        if not seed_id:
            continue
        for tid in _out(memory, nid, "ocelot_uses_term"):
            name = _term_label(memory, tid)
            if not name:
                continue
            used.add(name)
            seed_terms.setdefault(seed_id, set()).add(name)
            term_seed_counts[name] = term_seed_counts.get(name, 0) + 1
    unknown = used - defined
    unused = defined - used
    top_terms = [
        {"name": name, "seeds": count}
        for name, count in sorted(term_seed_counts.items(),
                                  key=lambda item: (-item[1], item[0]))
    ][:MAX_TOP_TERMS]
    seeds = [
        {"id": seed_id, "terms": len(names)}
        for seed_id, names in sorted(seed_terms.items(),
                                     key=lambda item: (-len(item[1]),
                                                       item[0]))
    ][:MAX_ITEMS]
    errors = _repo_attrs(memory).get("ocelot_bridge_errors") or []
    if not isinstance(errors, list):
        errors = [str(errors)]
    seed_count = sum(
        1 for nid in memory.nodes_of_type("OcelotSeed")
        if _seed_id(memory, nid) and not _attrs(memory, nid).get("stub"))
    return {
        "ok": True,
        "defined_terms": sorted(defined),
        "used_terms": sorted(used),
        "unknown_terms": sorted(unknown),
        "unused_terms": sorted(unused),
        "defined_count": len(defined),
        "used_count": len(used),
        "unknown_count": len(unknown),
        "unused_count": len(unused),
        "term_count": len(term_ids),
        "seed_count": seed_count,
        "seeds_with_terms": len(seed_terms),
        "seeds": seeds,
        "top_terms": top_terms,
        "errors": errors[:MAX_BRIDGE_ERRORS],
    }


def _impact(memory: Any, target: str) -> dict[str, Any]:
    value = str(target or "").strip()
    base: dict[str, Any] = {
        "ok": False,
        "target": value,
        "kind": "",
        "engine_rebuild": True,
        "primitives": [],
        "terms": [],
        "python_files": [],
        "test_files": [],
        "tests": [],
        "seeds": [],
        "resources": [],
    }
    if not value:
        base["error"] = "target is required"
        return base

    rel = value.replace("\\", "/")
    root = Path(memory.raw_repo_root).resolve()
    try:
        candidate = (root / rel).resolve()
        if candidate.is_file():
            rel = candidate.relative_to(root).as_posix()
    except (OSError, ValueError):
        pass

    file_nid = memory.node_by_key("file:" + rel)
    primitive_ids: set[int] = set()
    term_ids: set[int] = set()
    if file_nid is not None:
        base["kind"] = "path"
        primitive_ids.update(_out(memory, file_nid,
                                  "ocelot_defines_primitive"))
        primitive_ids.update(_out(memory, file_nid,
                                  "ocelot_uses_primitive"))
        term_ids.update(_out(memory, file_nid, "ocelot_defines_term"))
    else:
        for nid in memory.nodes_of_type("OcelotPrimitive"):
            attrs = _attrs(memory, nid)
            if str(attrs.get("rust_path") or "") == rel:
                primitive_ids.add(nid)
        for nid in memory.nodes_of_type("OcelotTerm"):
            attrs = _attrs(memory, nid)
            if str(attrs.get("rust_path") or "") == rel:
                term_ids.add(nid)
        if primitive_ids or term_ids:
            base["kind"] = "path"

    if not primitive_ids and not term_ids:
        pid = _find_primitive(memory, value)
        if pid is not None:
            primitive_ids.add(pid)
            base["kind"] = "primitive"
        else:
            tid = _find_term(memory, value)
            if tid is not None:
                term_ids.add(tid)
                base["kind"] = "term"

    if not primitive_ids and not term_ids:
        base["error"] = f"no Rust path, primitive or term matches {value!r}"
        return base

    related_terms: set[int] = set(term_ids)
    for pid in primitive_ids:
        related_terms.update(_primitive_terms(memory, pid))
    for tid in list(related_terms):
        label = _term_label(memory, tid)
        pid = _find_primitive(memory, label)
        if pid is not None:
            primitive_ids.add(pid)

    python_files: set[str] = set()
    test_files: set[str] = set()
    test_ids: set[str] = set()
    for pid in primitive_ids:
        files, tests, ids = _primitive_usage(memory, pid)
        python_files.update(files)
        test_files.update(tests)
        test_ids.update(ids)

    seeds: set[str] = set()
    for tid in related_terms:
        seeds.update(_term_seeds(memory, tid))

    primitive_names = sorted(
        str(_attrs(memory, pid).get("name") or "") for pid in primitive_ids)
    term_labels = sorted({_term_label(memory, tid) for tid in related_terms})
    definitions = {
        str(_attrs(memory, pid).get("rust_path") or "")
        for pid in primitive_ids
    } | {
        str(_attrs(memory, tid).get("rust_path") or "")
        for tid in related_terms
    }
    resources = set(python_files) | set(test_files)
    resources.update(path for path in definitions if path)
    resources.update(_term_resources(memory, seeds))
    if file_nid is not None:
        resources.add(rel)
    base.update({
        "ok": True,
        "primitives": [name for name in primitive_names if name],
        "terms": term_labels,
        "python_files": sorted(python_files),
        "test_files": sorted(test_files),
        "tests": sorted(set(test_ids) | set(test_files)),
        "seeds": sorted(seeds),
        "resources": sorted(path for path in resources if path),
    })
    return base


def _rs_parity(memory: Any) -> dict[str, Any]:
    """Pair ``domains_rs`` mirrors with their ``domains`` counterparts."""
    root = Path(memory.raw_repo_root).resolve()
    rs_dir = root / "domains_rs"
    pairs: list[dict[str, Any]] = []
    if rs_dir.is_dir():
        for path in sorted(rs_dir.glob("*.py")):
            if path.name == "__init__.py":
                continue
            stem = path.stem
            rs_rel = path.relative_to(root).as_posix()
            py_rel = f"domains/{stem}.py"
            py = root / py_rel
            tests: set[str] = set()
            for rel in (rs_rel, py_rel if py.is_file() else ""):
                if not rel:
                    continue
                try:
                    result = memory.tests_for(rel)
                except Exception:  # noqa: BLE001 - absent graph context
                    continue
                for item in result.get("tests") or []:
                    if isinstance(item, dict) and item.get("file"):
                        tests.add(str(item["file"]))
            for candidate in (f"tests/test_{stem}_parity.py",
                              f"tests/test_{stem}.py"):
                if (root / candidate).is_file():
                    tests.add(candidate)
            status = ("paired" if py.is_file() else "missing_python")
            pairs.append({
                "module": stem,
                "rust_python_path": rs_rel,
                "domains_path": py_rel if py.is_file() else None,
                "status": status,
                "tests": sorted(tests),
            })
    missing = [item["module"] for item in pairs
               if item["status"] != "paired"]
    return {"ok": True, "op": "rs_parity", "count": len(pairs),
            "pairs": pairs, "missing_python": missing,
            "paired_count": len(pairs) - len(missing),
            "note": "syntactic path pairing only; parity assertions and test "
                    "verdicts remain the repository's own gates"}


def engine_summary(memory: Any, target: str | None = None,
                   op: str = "primitive") -> dict[str, Any]:
    """Ocelot engine bridge query: primitive, rust impact or term census."""
    mode = str(op or "primitive").strip().lower()
    if mode == "term_census":
        return _term_census(memory)
    if mode == "rust_impact":
        return _impact(memory, str(target or ""))
    if mode == "rs_parity":
        return _rs_parity(memory)
    if mode != "primitive":
        return {"ok": False,
                "error": f"unknown op {op!r}; expected primitive, "
                         "rust_impact, term_census or rs_parity"}
    if target:
        return _primitive_payload(memory, str(target))
    primitives = _primitive_list(memory)
    return {
        "ok": True,
        "primitive": None,
        "count": len(memory.nodes_of_type("OcelotPrimitive")),
        "primitives": primitives,
        "definitions": [],
        "python_files": [],
        "tests": [],
        "seeds": [],
        "terms": [],
        "resources": [],
    }


# --------------------------------------------------------------------------- #
# adapter surface
# --------------------------------------------------------------------------- #
def _tool_engine(memory: Any, args: Mapping[str, Any]) -> Any:
    return engine_summary(memory, target=args.get("target"),
                          op=str(args.get("op") or "primitive"))


ENGINE_TOOL = {
    "name": "ocelot_engine",
    "description": ("Syntactic Ocelot Rust/Python/seed bridge: primitive "
                    "definition and uses, Rust change impact, or Term census."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "op": {"type": "string",
                   "enum": ["primitive", "rust_impact", "term_census",
                            "rs_parity"],
                   "default": "primitive"},
            "target": {"type": "string"},
        },
        "additionalProperties": False,
    },
}


def register_engine(registry: Any) -> None:
    """Register the Ocelot engine bridge indexer, query and MCP tool."""
    if getattr(registry, "_ocelot_engine_registered", False):
        return
    setattr(registry, "_ocelot_engine_registered", True)
    registry.add_indexer(
        index_engine_bridge,
        watch=(RUST_GLOB, RUST_RECURSIVE_GLOB, PYTHON_ENGINE_GLOB, SEED_GLOB),
        texts=TEXT_RE,
    )
    registry.add_graph_query(
        "ocelot_engine", lambda memory, **kw: engine_summary(
            memory, target=kw.get("target"),
            op=str(kw.get("op") or "primitive")))
    registry.add_mcp_tool(ENGINE_TOOL, _tool_engine)


__all__ = [
    "ENGINE_TOOL",
    "JSONIO_FILE",
    "PYTHON_ENGINE_GLOB",
    "RUST_GLOB",
    "RUST_SOURCE_DIR",
    "SEED_GLOB",
    "TERM_FILE",
    "TEXT_RE",
    "engine_summary",
    "index_engine_bridge",
    "register_engine",
]
