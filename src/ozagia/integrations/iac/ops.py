"""Operations sources in the opt-in IaC adapter: compose files,
Containerfiles, quadlets and systemd units (`ozagia.core.ops_graph`).

The IaC indexer's bounded discovery hands the sources over (kind `ops`);
this module resolves them, emits `InfraResource` nodes with `family: ops`
and `ops:` keys, `infra_<relation>` edges between them (so `infra.locate`,
`infra.dependencies` and `infra.impact` cover them), and `ops_<relation>`
edges to the File nodes of repository files they run, mount or read
(`runs`, `env_file`, `mounts`, `kube_yaml`). Env files are linked by path
and never read.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from ozagia.core.infra_discovery import (
    MAX_FILE_BYTES,
    env_file_name,
    is_skipped_path,
    read_source,
)
from ozagia.core.ops_graph import (
    EXTRACTOR_VERSION,
    OpsGraph,
    ops_query,
    parse_ops_source,
    resolve,
)

_LIST_ATTRS = ("ports", "expose", "listen", "volumes", "networks", "env",
               "env_files", "args", "profiles", "depends_on", "exec",
               "exec_other", "on_calendar", "timer_triggers", "watches",
               "requires", "wants", "after", "before", "part_of", "binds_to",
               "requisite", "upholds", "conflicts", "wanted_by", "required_by",
               "upheld_by", "secrets")
_SCALAR_ATTRS = ("image", "container_name", "build_context", "entrypoint",
                 "cmd", "user", "working_directory", "service_type", "unit",
                 "service_unit", "pod", "pod_name", "network_name",
                 "volume_name", "yaml", "project", "final_stage",
                 "from_stage", "base_image")


def _regular_file(root: Any, rel: str, skipped: Any = None) -> bool:
    if is_skipped_path(root, rel) or (skipped is not None and skipped(rel)):
        return False
    current = root
    for part in PurePosixPath(rel).parts:
        current = current / part
        if current.is_symlink():
            return False
    return current.is_file()


def _file_id(ctx: Any, rel: str) -> int:
    """The File node of `rel`: the indexed one, else an unindexed node
    (its text is never read here)."""
    fid = ctx.builder.find(("file", rel))
    return fid if fid is not None else ctx.file_node(rel, indexed=False)


def build_ops_graph(root: Any, sources: dict[str, str], *,
                    skipped: Any = None, parse: Any = None) -> OpsGraph:
    """Parse and resolve `sources` (root-relative path -> text). One source
    that fails to parse becomes a diagnostic; the others still index.
    `skipped(rel)` is the caller's skip policy (memory skip config, state
    directory) for build files and linked files; `parse(rel, text)` parses
    one source (default `parse_ops_source`; the adapter passes a cached
    one, whose results must not be mutated)."""
    graph = OpsGraph()
    for rel, text in sorted(sources.items()):
        try:
            parsed = parse(rel, text) if parse else parse_ops_source(text, rel)
        except Exception:  # noqa: BLE001 - one bad file, a diagnostic
            graph.diagnostics.append((rel, "operations source could not be parsed"))
            continue
        graph.extend(parsed)

    def read_build_file(rel: str) -> str | None:
        # Env files are linked by path, never read.
        if env_file_name(rel) or not _regular_file(root, rel, skipped):
            return None
        return read_source(root, rel, max_bytes=MAX_FILE_BYTES)

    return resolve(graph, file_exists=lambda rel: _regular_file(root, rel, skipped),
                   root=str(root.resolve()), read_containerfile=read_build_file)


def emit_ops(ctx: Any, sources: dict[str, str], *,
             parse: Any = None) -> list[tuple[str, str]]:
    """Add the ops graph of `sources` to the build; return diagnostics."""
    graph = build_ops_graph(ctx.root, sources, skipped=ctx.skipped, parse=parse)
    ids: dict[str, int] = {}
    for node in graph.nodes:
        attrs: dict[str, Any] = {
            "name": node.name, "address": node.name, "path": node.path,
            "kind": node.kind, "family": "ops", "provenance": "declared",
            "confidence": node.confidence,
            "source_digest": node.attrs.get("source_digest") or "",
            "extractor_version": EXTRACTOR_VERSION,
        }
        if isinstance(node.line, int):
            attrs["line"] = node.line
        service_unit = node.attrs.get("service_unit")
        if isinstance(service_unit, str):
            attrs["aliases"] = service_unit
        elif node.kind.startswith("systemd_"):
            attrs["aliases"] = PurePosixPath(node.name).stem
        for field in _SCALAR_ATTRS:
            value = node.attrs.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                attrs[field] = value
        for field in _LIST_ATTRS:
            value = node.attrs.get(field)
            if isinstance(value, list) and value:
                attrs[field] = list(value)
        if "declared" in node.attrs:
            attrs["declared"] = bool(node.attrs["declared"])
        nid = ctx.builder.node("InfraResource", attrs,
                               key=("ops", node.key[len("ops:"):]))
        ids[node.key] = nid
        if node.kind != "image" and node.path:
            ctx.builder.edge(nid, "declared_in", _file_id(ctx, node.path))
    for edge in graph.edges:
        if edge.source in ids and edge.target in ids:
            ctx.builder.edge(ids[edge.source], "infra_" + edge.relation,
                             ids[edge.target],
                             {"provenance": "declared", "evidence": edge.evidence,
                              "confidence": "high"})
    for source, relation, rel in graph.file_links:
        if source not in ids or ctx.skipped(rel):
            continue
        ctx.builder.edge(ids[source], "ops_" + relation, _file_id(ctx, rel),
                         {"provenance": "declared"})
    return graph.diagnostics


def register_ops(registry: Any) -> None:
    """The `infra.ops` graph query and `infra_ops` MCP tool."""

    def query(memory: Any, **args: Any) -> dict[str, Any]:
        freshness = memory.staleness().get("freshness", "unknown")
        if freshness != "fresh":
            return {"freshness": freshness, "complete": False, "results": []}
        try:
            result = ops_query(memory, str(args.get("query", "")),
                               str(args.get("target", "")),
                               limit=int(args.get("limit", 20)))
        except ValueError as exc:
            return {"freshness": freshness, "complete": False, "results": [],
                    "error": str(exc)}
        result["freshness"] = freshness
        return result

    registry.add_graph_query("infra.ops", query)
    registry.add_mcp_tool({
        "name": "infra_ops",
        "description": (
            "Query the static operations graph (compose services, "
            "Containerfiles, podman quadlets, systemd units): `dependents` "
            "of a node, what a service `exposes` (ports, volumes, networks, "
            "env names), or which units/services `runs` a repository file. "
            "Declared sources only; not live container or unit state."),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "enum": ["dependents", "exposes", "runs"]},
            "target": {"type": "string"},
            "limit": {"type": "integer", "default": 20}},
            "required": ["query", "target"], "additionalProperties": False},
    }, lambda memory, args: query(memory, **args))
