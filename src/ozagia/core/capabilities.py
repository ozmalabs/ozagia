"""The explicit, built-in catalogue of Ozagia's declared capabilities.

The catalogue is authoritative for *declarations*.  It is deliberately a
hand-written, versioned data table rather than a scan of whatever code happens
to be importable: a source scan cannot tell a supported capability from an
internal helper, and it silently turns an omission into an absence.  CLI, MCP,
daemon, toolchain and status surfaces are separate **observations** joined
read-only by :mod:`ozagia.core.self_graph`.

Nothing in this module imports a target project, reads the environment, runs a
process, opens a socket, calls a model or writes a file.  Building the
catalogue is pure and deterministic.

Record fields
-------------

Every capability carries at least:

``id``
    A ``cap:`` identity validated by :mod:`ozagia.core.contracts`.
``domain``
    One of :data:`DOMAINS`.
``purpose``
    One bounded sentence saying what the capability is for.
``owner``
    The module that owns the behaviour (``ozagia.core.memory``).
``side_effect``
    One of :data:`SIDE_EFFECTS`.
``exposure``
    A non-empty subset of :data:`EXPOSURES`.  ``internal`` is exclusive: an
    internal capability has no CLI/MCP/daemon surface, though it may have an
    internal observation such as a status section or the toolchain registry.
``impl``
    One or more ``impl:`` identities.  The implementation part of the id is a
    dotted module path with an optional symbol, e.g.
    ``impl:ozagia.core.memory.context``.  Resolution is static and does not
    import the module.
``surfaces``
    Observed surface ids this capability declares, exactly as
    :mod:`ozagia.core.inventory` names them (``cli:ozagia.index``,
    ``mcp:tool:dev_context``, ``mcp:graph:find``, ``daemon:op:stats``,
    ``toolchain:registry``, ``status:workspace``).  A CLI declaration covers
    its dotted descendants (``cli:ozagia.goal`` covers ``cli:ozagia.goal.add``).
``prerequisites``
    Bounded declarative prerequisites, not probes.
``limits``
    Documented limits.
``authorization``
    Who or what may invoke it, and which existing gate applies.
``tests`` / ``docs``
    ``test:`` and ``doc:`` local graph-key references.  They are *not*
    contract identities: the v1 contract layer reserves ``test:`` and ``doc:``
    as unqualified local graph keys, so they are only checked as relative
    repository paths here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

from ozagia.core import contracts as C

CATALOGUE_NAMESPACE = "ozagia.self_graph.capabilities"
CATALOGUE_VERSION = "1.0"
CAPABILITY_VERSION = "1.0"

DOMAINS = (
    "graph", "edit", "test", "goals", "tasks", "environment", "resources",
    "toolchain", "status", "federation", "research", "runtime", "actions",
    "eval",
)
EXPOSURES = ("cli", "daemon", "internal", "mcp")
SIDE_EFFECTS = ("none", "read", "write", "execute", "network", "model")
ORIGINS = ("builtin",)

MAX_PURPOSE = 400
MAX_TEXT = 400
MAX_AUTHORIZATION = 300
MAX_ITEMS = 16
MAX_SURFACES = 64
MAX_REF = C.MAX_LOCAL_KEY_LENGTH

_OWNER_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SURFACE_RE = re.compile(
    r"^(?:cli|mcp|daemon|toolchain|status|settings|entrypoint|adapter):"
    r"[a-z0-9][a-z0-9_.:-]*$")
_REF_RE = re.compile(r"^(?:test|doc):[A-Za-z0-9_./#:@%+\-]+$")

_REQUIRED_FIELDS = (
    "id", "domain", "purpose", "owner", "side_effect", "exposure", "impl",
    "surfaces", "prerequisites", "limits", "authorization", "tests", "docs",
)
_OPTIONAL_FIELDS = ("origin",)


class CapabilityError(ValueError):
    """A malformed or inconsistent capability declaration."""


# ---------------------------------------------------------------------------
# Bounded-field helpers
# ---------------------------------------------------------------------------


def _text(value: Any, name: str, *, limit: int = MAX_TEXT,
          allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise CapabilityError(f"{name} must be a string")
    if not allow_empty and not value:
        raise CapabilityError(f"{name} must not be empty")
    if len(value) > limit:
        raise CapabilityError(f"{name} exceeds {limit} characters")
    if any(ord(char) < 0x20 for char in value):
        raise CapabilityError(f"{name} contains control characters")
    return value


def _items(value: Any, name: str, *, limit: int = MAX_ITEMS,
           min_items: int = 0) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CapabilityError(f"{name} must be a list of strings")
    items = []
    for item in value:
        items.append(_text(item, f"{name} entry", limit=MAX_TEXT))
    if len(items) > limit:
        raise CapabilityError(f"{name} exceeds {limit} entries")
    if len(items) < min_items:
        raise CapabilityError(f"{name} needs at least {min_items} entries")
    unique = tuple(dict.fromkeys(items))
    return tuple(sorted(unique))


def _exposure(value: Any) -> tuple[str, ...]:
    items = _items(value, "exposure", limit=len(EXPOSURES), min_items=1)
    for item in items:
        if item not in EXPOSURES:
            raise CapabilityError(
                f"unknown exposure {item!r}; expected one of {EXPOSURES}")
    if "internal" in items and len(items) != 1:
        raise CapabilityError(
            "an internal capability cannot also expose cli, mcp or daemon")
    return items


def _surface(value: Any) -> str:
    text = _text(value, "surface", limit=MAX_REF)
    if not _SURFACE_RE.fullmatch(text):
        raise CapabilityError(f"invalid surface id {text!r}")
    return text


def _surfaces(value: Any, exposure: Sequence[str]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CapabilityError("surfaces must be a list of surface ids")
    surfaces = tuple(sorted({_surface(item) for item in value}))
    if len(surfaces) > MAX_SURFACES:
        raise CapabilityError(f"surfaces exceeds {MAX_SURFACES} entries")
    prefixes = tuple(surface.split(":", 1)[0] for surface in surfaces)
    if exposure == ("internal",):
        allowed = {"toolchain", "status"}
        bad = sorted(set(prefixes) - allowed)
        if bad:
            raise CapabilityError(
                "an internal capability may only declare toolchain/status "
                f"observations, not {bad}")
        return surfaces
    for name in exposure:
        if name not in prefixes:
            raise CapabilityError(
                f"exposure {name!r} needs at least one {name}: surface")
    return surfaces


def _ref(value: Any, prefix: str) -> str:
    text = _text(value, f"{prefix} reference", limit=MAX_REF)
    if not _REF_RE.fullmatch(text) or not text.startswith(prefix + ":"):
        raise CapabilityError(
            f"{prefix} reference {text!r} must be {prefix}:<relative path>")
    path = text.split(":", 1)[1]
    if path.startswith("/") or ".." in path.split("/"):
        raise CapabilityError(
            f"{prefix} reference {text!r} must stay inside the repository")
    if prefix == "test" and not path.startswith("tests/"):
        raise CapabilityError(
            f"test reference {text!r} must start with tests/")
    if prefix == "doc" and not path.startswith("docs/"):
        raise CapabilityError(f"doc reference {text!r} must start with docs/")
    return text


def _refs(value: Any, prefix: str, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise CapabilityError(f"{name} must be a list of {prefix}: references")
    refs = tuple(sorted({_ref(item, prefix) for item in value}))
    if not refs:
        raise CapabilityError(f"{name} needs at least one {prefix}: reference")
    if len(refs) > MAX_ITEMS:
        raise CapabilityError(f"{name} exceeds {MAX_ITEMS} entries")
    return refs


def _owner(value: Any) -> str:
    text = _text(value, "owner", limit=128)
    if not _OWNER_RE.fullmatch(text):
        raise CapabilityError(
            f"owner {text!r} must be a dotted lowercase module path")
    return text


def _impl(value: Any) -> str:
    parsed = C.parse_id(value)
    if parsed.kind != "impl":
        raise CapabilityError(f"implementation id must be impl:, got {value!r}")
    return parsed.canonical


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Capability:
    """One declared capability, validated and canonically ordered."""

    id: str
    domain: str
    purpose: str
    owner: str
    side_effect: str
    exposure: tuple[str, ...]
    impl: tuple[str, ...]
    surfaces: tuple[str, ...]
    prerequisites: tuple[str, ...]
    limits: tuple[str, ...]
    authorization: str
    tests: tuple[str, ...]
    docs: tuple[str, ...]
    origin: str = "builtin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CAPABILITY_VERSION,
            "id": self.id,
            "domain": self.domain,
            "purpose": self.purpose,
            "owner": self.owner,
            "side_effect": self.side_effect,
            "exposure": list(self.exposure),
            "impl": list(self.impl),
            "surfaces": list(self.surfaces),
            "prerequisites": list(self.prerequisites),
            "limits": list(self.limits),
            "authorization": self.authorization,
            "tests": list(self.tests),
            "docs": list(self.docs),
            "origin": self.origin,
        }


def build_capability(record: Mapping[str, Any]) -> Capability:
    """Validate one declaration and return a :class:`Capability`."""
    if not isinstance(record, Mapping):
        raise CapabilityError("a capability declaration must be a mapping")
    unknown = sorted(set(record) - set(_REQUIRED_FIELDS) - set(_OPTIONAL_FIELDS))
    if unknown:
        raise CapabilityError(f"unknown capability fields: {unknown}")
    missing = [name for name in _REQUIRED_FIELDS if name not in record]
    if missing:
        raise CapabilityError(f"missing capability fields: {missing}")

    identity = C.parse_id(record["id"])
    if identity.kind != "cap":
        raise CapabilityError(
            f"capability id must be cap:, got {record['id']!r}")
    if identity.canonical != record["id"]:
        raise CapabilityError(
            f"capability id {record['id']!r} is not canonical "
            f"({identity.canonical!r})")

    domain = _text(record["domain"], "domain", limit=32)
    if domain not in DOMAINS:
        raise CapabilityError(
            f"unknown domain {domain!r}; expected one of {DOMAINS}")
    side_effect = _text(record["side_effect"], "side_effect", limit=32)
    if side_effect not in SIDE_EFFECTS:
        raise CapabilityError(
            f"unknown side-effect class {side_effect!r}; expected one of "
            f"{SIDE_EFFECTS}")
    exposure = _exposure(record["exposure"])

    impls = _items(record["impl"], "impl", limit=MAX_ITEMS, min_items=1)
    canonical_impl = tuple(sorted({_impl(item) for item in impls}))
    if len(canonical_impl) != len(impls):
        raise CapabilityError("impl ids collapse to duplicates")
    origins = _text(record.get("origin", "builtin"), "origin", limit=32)
    if origins not in ORIGINS:
        raise CapabilityError(f"unknown origin {origins!r}")

    return Capability(
        id=identity.canonical,
        domain=domain,
        purpose=_text(record["purpose"], "purpose", limit=MAX_PURPOSE),
        owner=_owner(record["owner"]),
        side_effect=side_effect,
        exposure=exposure,
        impl=canonical_impl,
        surfaces=_surfaces(record["surfaces"], exposure),
        prerequisites=_items(record["prerequisites"], "prerequisites"),
        limits=_items(record["limits"], "limits"),
        authorization=_text(record["authorization"], "authorization",
                            limit=MAX_AUTHORIZATION),
        tests=_refs(record["tests"], "test", "tests"),
        docs=_refs(record["docs"], "doc", "docs"),
        origin=origins,
    )


def _def(**fields: Any) -> dict[str, Any]:
    return fields


_READ = "none: read-only; no approval gate for reads"
_EDIT = ("edit/txn.py: preview by default; apply needs the caller's existing "
         "authorization, base-hash check and backup")
_STATE = "none: writes only under the repository state directory"
_EXEC = "the existing harness/daemon permission and action gate"


# ---------------------------------------------------------------------------
# The built-in catalogue
# ---------------------------------------------------------------------------

_DOCS = {
    "capabilities": "doc:docs/capabilities.md",
    "graph": "doc:docs/memory_mcp.md",
    "graph_surface": "doc:docs/graph_surface.md",
    "graph_engine": "doc:docs/graph_engine.md",
    "intent": "doc:docs/intent.md",
    "model_api": "doc:docs/model_api.md",
    "code_search": "doc:docs/code_search.md",
    "symbol_graph": "doc:docs/symbol_graph.md",
    "lsp": "doc:docs/lsp.md",
    "scip": "doc:docs/scip.md",
    "architecture": "doc:docs/architecture_summaries.md",
    "repo_shape": "doc:docs/repo_shape.md",
    "repo_review": "doc:docs/repo_review.md",
    "deep_review": "doc:docs/deep_review.md",
    "profile": "doc:docs/profiling.md",
    "edit": "doc:docs/cst_edit.md",
    "files": "doc:docs/files.md",
    "test": "doc:docs/testing.md",
    "goals": "doc:docs/goals.md",
    "tasks": "doc:docs/tasks.md",
    "session": "doc:docs/session.md",
    "jobs": "doc:docs/jobs.md",
    "environment": "doc:docs/environments.md",
    "resources": "doc:docs/resources.md",
    "toolchain": "doc:docs/toolchain.md",
    "languages": "doc:docs/languages.md",
    "rust": "doc:docs/rust.md",
    "migration": "doc:docs/migration.md",
    "supply_chain": "doc:docs/supply_chain.md",
    "status": "doc:docs/status.md",
    "federation": "doc:docs/federation.md",
    "research": "doc:docs/research.md",
    "runtime": "doc:docs/runtime.md",
    "secrets": "doc:docs/secrets.md",
    "actions": "doc:docs/actions.md",
    "relay": "doc:docs/relay.md",
    "iac": "doc:docs/iac_check.md",
    "live": "doc:docs/live_state.md",
    "ops": "doc:docs/ops.md",
    "preflight": "doc:docs/preflight.md",
    "task_preflight": "doc:docs/task_preflight.md",
    "eval_agent": "doc:docs/eval_agent.md",
    "eval_retrieval": "doc:docs/eval_retrieval.md",
    "install": "doc:docs/install.md",
}

_TESTS = {
    "cli": "test:tests/test_cli.py",
    "memory": "test:tests/test_memory.py",
    "repo_map": "test:tests/test_repo_map.py",
    "architecture": "test:tests/test_architecture.py",
    "repo_shape": "test:tests/test_repo_shape.py",
    "search": "test:tests/test_search.py",
    "scip": "test:tests/test_lsp.py",
    "lsp": "test:tests/test_lsp.py",
    "graph_surface": "test:tests/test_graph_surface.py",
    "graph_engine": "test:tests/test_graph_engine.py",
    "engine_http": "test:tests/test_engine_http.py",
    "engine_client": "test:tests/test_engine_client.py",
    "symbol_graph": "test:tests/test_call_paths.py",
    "read_ledger": "test:tests/test_read_ledger.py",
    "intent": "test:tests/test_intent.py",
    "proactive": "test:tests/test_proactive_context.py",
    "reingest": "test:tests/test_graph_reingest.py",
    "owners": "test:tests/test_paths_owners.py",
    "daemon": "test:tests/test_daemon.py",
    "mcp": "test:tests/test_mcp_server.py",
    "edit": "test:tests/test_edit_txn.py",
    "code_edit": "test:tests/test_code_edit.py",
    "cst": "test:tests/test_cst_edit.py",
    "replace": "test:tests/test_replace.py",
    "test_policy": "test:tests/test_affected_tests.py",
    "check": "test:tests/test_check.py",
    "review": "test:tests/test_repo_review.py",
    "deep_review": "test:tests/test_deep_review.py",
    "app_profile": "test:tests/test_profile.py",
    "goals": "test:tests/test_goals.py",
    "jobs": "test:tests/test_jobs.py",
    "tasks": "test:tests/test_tasks.py",
    "environment": "test:tests/test_environments.py",
    "files": "test:tests/test_files.py",
    "resources": "test:tests/test_cgroup.py",
    "toolchain": "test:tests/test_toolchain.py",
    "languages": "test:tests/test_languages.py",
    "migration": "test:tests/test_migration_cli.py",
    "mcp_migration": "test:tests/test_mcp_migration.py",
    "supply_chain": "test:tests/test_supply_chain.py",
    "status": "test:tests/test_status.py",
    "settings": "test:tests/test_settings.py",
    "federation": "test:tests/test_federation.py",
    "federation_surface": "test:tests/test_federation_surface.py",
    "research": "test:tests/test_research_acquisition.py",
    "runtime": "test:tests/test_runtime.py",
    "secrets": "test:tests/test_secrets.py",
    "hooks": "test:tests/test_hooks.py",
    "session": "test:tests/test_session.py",
    "profile": "test:tests/test_session_profile.py",
    "actions": "test:tests/test_actions.py",
    "relay": "test:tests/test_relay.py",
    "iac": "test:tests/test_iac_check.py",
    "live": "test:tests/test_live_state.py",
    "ops": "test:tests/test_ops.py",
    "preflight": "test:tests/test_preflight.py",
    "task_preflight": "test:tests/test_task_preflight.py",
    "eval_agent": "test:tests/test_eval_agent.py",
    "eval_retrieval": "test:tests/test_eval_retrieval.py",
    "self": "test:tests/test_self_graph.py",
    "structural": "test:tests/test_structural_intent_routing.py",
    "model_api": "test:tests/test_model_api.py",
}


def _tests(*keys: str) -> tuple[str, ...]:
    return tuple(_TESTS[key] for key in keys)


def _docs(*keys: str) -> tuple[str, ...]:
    return tuple(_DOCS[key] for key in keys)


_BUILTIN_DEFINITIONS: tuple[dict[str, Any], ...] = (
    # graph ----------------------------------------------------------------
    _def(
        id="cap:graph.index",
        domain="graph",
        purpose=("Build or refresh the repository graph snapshot used by every "
                 "graph capability."),
        owner="ozagia.harness.session",
        side_effect="write",
        exposure=["cli"],
        impl=["impl:ozagia.harness.session.prepare_snapshot"],
        surfaces=["cli:ozagia.index"],
        prerequisites=["a Git checkout", "the target's configured adapters"],
        limits=["writes only the snapshot under <state dir>",
                "full rebuilds are opt-in with --rebuild"],
        authorization=_STATE,
        tests=_tests("cli", "memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.context",
        domain="graph",
        purpose="Build a cited context pack for a development task.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.context"],
        surfaces=["mcp:tool:dev_context"],
        prerequisites=["a graph snapshot"],
        limits=["max_items and budget_tokens bound the pack"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.brief",
        domain="graph",
        purpose="Return a compact repository brief and optional task context.",
        owner="ozagia.core.memory",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.brief"],
        surfaces=["mcp:tool:dev_brief"],
        prerequisites=["a graph snapshot"],
        limits=["write=false by default; write=true saves .ozagia/brief.md"],
        authorization=_STATE,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.overview",
        domain="graph",
        purpose=("Return one deterministic source-shape reference for a "
                 "checkout: identity, layout, modules, docs, entry points, "
                 "tests and commands, with a separately labelled observed "
                 "environment."),
        owner="ozagia.core.repo_shape",
        side_effect="read",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.core.repo_shape.build_shape",
              "impl:ozagia.core.repo_shape.render_markdown",
              "impl:ozagia.core.repo_shape.render_json"],
        surfaces=["cli:ozagia.overview", "cli:ozagia.shape",
                  "mcp:tool:dev_overview"],
        prerequisites=["a checkout; optional tag cache and graph snapshot"],
        limits=["bounded file reads and module/doc caps",
                "no target import or execution; no network or model call",
                "no writes except an explicit `--out`"],
        authorization=_READ,
        tests=_tests("repo_shape", "cli", "mcp"),
        docs=_docs("repo_shape", "graph"),
    ),
    _def(
        id="cap:graph.repo_map",
        domain="graph",
        purpose="Return a ranked, token-budgeted repository map.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.repo_map",
              "impl:ozagia.core.repo_map.update_tag_cache"],
        surfaces=["mcp:tool:dev_repo_map"],
        prerequisites=["a graph snapshot", "optional tree-sitter extras"],
        limits=["budget_tokens bounds the result", "tag extraction is best-effort"],
        authorization=_READ,
        tests=_tests("repo_map"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.impact",
        domain="graph",
        purpose="List transitive dependents of a file over the reference graph.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.impact"],
        surfaces=["mcp:tool:dev_impact", "mcp:graph:impact"],
        prerequisites=["a graph snapshot with reference edges"],
        limits=["depth is bounded by the caller"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph_surface"),
    ),
    _def(
        id="cap:graph.architecture",
        domain="graph",
        purpose="Summarize hubs, cycles, coupling and module shape.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.architecture",
              "impl:ozagia.core.architecture.architecture_report"],
        surfaces=["mcp:tool:dev_architecture"],
        prerequisites=["a graph snapshot"],
        limits=["top bounds the number of rows"],
        authorization=_READ,
        tests=_tests("architecture"),
        docs=_docs("architecture"),
    ),
    _def(
        id="cap:graph.code_search",
        domain="graph",
        purpose="Run lexical search over the graph and source text.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.code_search"],
        surfaces=["mcp:tool:dev_code_search"],
        prerequisites=["a graph snapshot"],
        limits=["limit bounds the number of hits"],
        authorization=_READ,
        tests=_tests("search"),
        docs=_docs("code_search"),
    ),
    _def(
        id="cap:graph.scip",
        domain="graph",
        purpose="Import and summarize a supplied SCIP export.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.scip"],
        surfaces=["mcp:tool:dev_scip"],
        prerequisites=["a SCIP JSON export"],
        limits=["the export is read as data; no compiler or network call"],
        authorization=_READ,
        tests=_tests("scip"),
        docs=_docs("scip"),
    ),
    _def(
        id="cap:graph.search",
        domain="graph",
        purpose="Search graph nodes by text and type.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.search"],
        surfaces=["mcp:tool:dev_search", "mcp:graph:find"],
        prerequisites=["a graph snapshot"],
        limits=["limit and types bound the result"],
        authorization=_READ,
        tests=_tests("search"),
        docs=_docs("graph_surface"),
    ),
    _def(
        id="cap:graph.file",
        domain="graph",
        purpose="Show one file's symbols, tests, imports, docs and failures.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.file"],
        surfaces=["mcp:tool:dev_file"],
        prerequisites=["a graph snapshot"],
        limits=["source snippets are bounded by the caller"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.symbol",
        domain="graph",
        purpose="Show one symbol's definition, file and tests.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.symbol"],
        surfaces=["mcp:tool:dev_symbol"],
        prerequisites=["a graph snapshot"],
        limits=["exact-name lookup; ambiguous names are reported"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.outline",
        domain="graph",
        purpose="Return signatures and docstrings for a file or symbol.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.outline_target"],
        surfaces=["mcp:tool:dev_outline"],
        prerequisites=["a graph snapshot"],
        limits=["bodies are omitted"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.node",
        domain="graph",
        purpose="Expand one graph node and its immediate edges.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.node",
              "impl:ozagia.core.memory.related"],
        surfaces=["mcp:tool:dev_node", "mcp:graph:node"],
        prerequisites=["a graph snapshot"],
        limits=["include_snippet bounds source text"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph_surface"),
    ),
    _def(
        id="cap:graph.expand",
        domain="graph",
        purpose="Expand several graph keys along selected edge types.",
        owner="ozagia.integrations.mcp.graph_surface",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.integrations.mcp.graph_surface.expand"],
        surfaces=["mcp:graph:expand"],
        prerequisites=["a graph snapshot"],
        limits=["keys and edges are bounded by the graph surface"],
        authorization=_READ,
        tests=_tests("graph_surface"),
        docs=_docs("graph_surface"),
    ),
    _def(
        id="cap:graph.neighbours",
        domain="graph",
        purpose="Walk a node's incoming and outgoing dependency edges.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.related"],
        surfaces=["mcp:tool:dev_neighbours"],
        prerequisites=["a graph snapshot"],
        limits=["one node and a bounded neighbour list"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.structured_query",
        domain="graph",
        purpose="Read a JSON, YAML or TOML pointer as a graph-addressed value.",
        owner="ozagia.core.structured",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.structured.parse_key"],
        surfaces=["mcp:tool:dev_query"],
        prerequisites=["a graph snapshot", "the target file"],
        limits=["offset and limit bound the returned value"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.ast",
        domain="graph",
        purpose="Show Python AST paths and spans for a file.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.ast"],
        surfaces=["mcp:tool:dev_ast"],
        prerequisites=["a Python file"],
        limits=["max_nodes bounds the tree"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.seed",
        domain="graph",
        purpose="Show a Seed node's version, dependencies, rules and tests.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.node"],
        surfaces=["mcp:tool:dev_seed"],
        prerequisites=["an adapter that indexes Seed nodes"],
        limits=["listed only when the snapshot has Seed nodes"],
        authorization=_READ,
        tests=_tests("graph_surface"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.rule",
        domain="graph",
        purpose="Show a Rule node's seed, file and current body.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.node"],
        surfaces=["mcp:tool:dev_rule"],
        prerequisites=["an adapter that indexes Rule nodes"],
        limits=["listed only when the snapshot has Rule nodes"],
        authorization=_READ,
        tests=_tests("graph_surface"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.changed",
        domain="graph",
        purpose="Show working-tree changes linked to graph files.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.changed"],
        surfaces=["mcp:tool:dev_changed"],
        prerequisites=["a Git checkout", "a graph snapshot"],
        limits=["the caller's limits bound the diff lines"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.refresh",
        domain="graph",
        purpose="Re-ingest changed files into the current graph snapshot.",
        owner="ozagia.core.memory",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.refresh",
              "impl:ozagia.core.reingest.reingest"],
        surfaces=["mcp:tool:dev_refresh"],
        prerequisites=["a graph snapshot"],
        limits=["writes only the snapshot and its journal under <state dir>"],
        authorization=_STATE,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.lsp_status",
        domain="graph",
        purpose="Report which language servers are installed; start none.",
        owner="ozagia.lsp",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.lsp.available_servers"],
        surfaces=["mcp:tool:dev_lsp_status"],
        prerequisites=["optional language-server binaries"],
        limits=["availability check only; no server is started"],
        authorization=_READ,
        tests=_tests("lsp"),
        docs=_docs("lsp"),
    ),
    _def(
        id="cap:graph.lsp_navigation",
        domain="graph",
        purpose="Answer compiler-accurate navigation questions through an LSP.",
        owner="ozagia.lsp",
        side_effect="execute",
        exposure=["mcp"],
        impl=["impl:ozagia.lsp.get_client"],
        surfaces=[
            "mcp:tool:dev_definition", "mcp:tool:dev_references",
            "mcp:tool:dev_callers", "mcp:tool:dev_callees",
            "mcp:tool:dev_symbol_impact", "mcp:tool:dev_hover",
            "mcp:tool:dev_document_symbols",
        ],
        prerequisites=["a configured or discoverable language server"],
        limits=["the server is started lazily and closed with the process",
                "results are bounded per request"],
        authorization=_READ,
        tests=_tests("lsp"),
        docs=_docs("lsp"),
    ),
    _def(
        id="cap:graph.lsp_diagnostics",
        domain="graph",
        purpose="Return current diagnostics for a file from a language server.",
        owner="ozagia.lsp",
        side_effect="execute",
        exposure=["mcp"],
        impl=["impl:ozagia.lsp.get_client"],
        surfaces=["mcp:tool:dev_diagnostics"],
        prerequisites=["a configured or discoverable language server"],
        limits=["one file per call; a server may be started"],
        authorization=_READ,
        tests=_tests("lsp"),
        docs=_docs("lsp"),
    ),
    _def(
        id="cap:graph.symbol_graph",
        domain="graph",
        purpose="Answer syntactic caller, callee, impact and resolve queries.",
        owner="ozagia.core.symbol_graph",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.symbol_graph.symbol_query"],
        surfaces=[
            "mcp:tool:dev_symbol_callers", "mcp:tool:dev_symbol_callees",
            "mcp:tool:dev_symbol_graph_impact",
            "mcp:tool:dev_symbol_resolve",
        ],
        prerequisites=["a graph snapshot with symbol edges"],
        limits=["depth is bounded; confidence is reported"],
        authorization=_READ,
        tests=_tests("symbol_graph"),
        docs=_docs("symbol_graph"),
    ),
    _def(
        id="cap:graph.read_check",
        domain="graph",
        purpose="Tell a hook whether a read repeats an unchanged range.",
        owner="ozagia.intent",
        side_effect="read",
        exposure=["daemon"],
        impl=["impl:ozagia.intent.check_read"],
        surfaces=["daemon:op:read_check"],
        prerequisites=["a daemon with a session ledger"],
        limits=["does not record the read; the following after call does"],
        authorization=_READ,
        tests=_tests("read_ledger"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.path_check",
        domain="graph",
        purpose="Suggest the nearest existing paths for a missing read target.",
        owner="ozagia.intent",
        side_effect="read",
        exposure=["daemon"],
        impl=["impl:ozagia.intent.paths.missing_paths"],
        surfaces=["daemon:op:path_check"],
        prerequisites=["a daemon"],
        limits=["never waits on the graph lock; candidates may be absent"],
        authorization=_READ,
        tests=_tests("intent"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.proactive_context",
        domain="graph",
        purpose="Offer bounded, read-only graph context for a prompt.",
        owner="ozagia.core.proactive_context",
        side_effect="read",
        exposure=["daemon"],
        impl=["impl:ozagia.core.proactive_context.build_context"],
        surfaces=["daemon:op:hint"],
        prerequisites=["a daemon and a graph snapshot"],
        limits=["budget_tokens bounds the context"],
        authorization=_READ,
        tests=_tests("proactive"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:graph.structural_query",
        domain="graph",
        purpose=("Answer bounded structural questions about a fresh snapshot: "
                 "resolve, callers, callees, related tests and outline."),
        owner="ozagia.intent.structural",
        side_effect="read",
        exposure=["daemon"],
        impl=["impl:ozagia.intent.structural.query"],
        surfaces=["daemon:op:structural"],
        prerequisites=["a daemon and a current graph snapshot"],
        limits=["depth and budget_tokens bound the result",
                "a stale or unverifiable graph yields no results"],
        authorization=_READ,
        tests=_tests("structural"),
        docs=_docs("intent"),
    ),
    _def(
        id="cap:graph.model_maps",
        domain="graph",
        purpose=("Project bounded, versioned read-only maps for a model-facing "
                 "view of the graph, outline, capabilities and work evidence."),
        owner="ozagia.model_api",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.model_api.project.request_map"],
        surfaces=["mcp:tool:dev_maps"],
        prerequisites=["a host policy scoped to one checkout",
                       "an existing graph snapshot for graph maps"],
        limits=["default 64 KiB, 25 items and depth 2; hard 256 KiB, 200 and 4",
                "writes only cache metadata under <.ozagia>/model_api/cache",
                "never refreshes the graph or grants authority"],
        authorization=_STATE,
        tests=_tests("model_api"),
        docs=_docs("model_api"),
    ),
    _def(
        id="cap:graph.engine",
        domain="graph",
        purpose=("Answer bounded, token-estimated graph questions through one "
                 "stateful engine tool or a local read-only HTTP transport, with "
                 "compact rows, known deltas and bounded in-process background "
                 "speculative handles."),
        owner="ozagia.core.graph_engine",
        side_effect="read",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.core.graph_engine", "impl:ozagia.engine_client"],
        surfaces=["cli:ozagia.engine", "mcp:tool:graph"],
        prerequisites=["a graph snapshot whose freshness can be verified"],
        limits=["default 1200 and maximum 8000 estimated tokens per response",
                "16 bounded sessions; a fork child (or thread fallback) "
                "prepares at most two spec handles per response (300 ms "
                "default, one long call not preempted)",
                "fetch waits at most 250 ms; worker can be disabled with "
                "OZAGIA_ENGINE_SPEC=0 or forced to threads with "
                "OZAGIA_ENGINE_SPEC_PROCESS=0",
                "HTTP is loopback-only by default, always needs a bearer token, "
                "caps bodies at 64 KiB and concurrent calls at 4; no CORS, "
                "directory serving or remote mode without --allow-remote",
                "no edit, create, check, refresh, shell or target execution",
                "a stale or unknown snapshot yields no structural rows"],
        authorization=_READ,
        tests=_tests("graph_engine", "engine_http", "engine_client"),
        docs=_docs("graph_engine"),
    ),
    _def(
        id="cap:graph.after_refresh",
        domain="graph",
        purpose="Re-ingest what a harness tool changed and return context lines.",
        owner="ozagia.core.reingest",
        side_effect="write",
        exposure=["daemon"],
        impl=["impl:ozagia.core.reingest.reingest"],
        surfaces=["daemon:op:after"],
        prerequisites=["a daemon and a graph snapshot"],
        limits=["writes only the snapshot/journal under <state dir>"],
        authorization=_STATE,
        tests=_tests("reingest"),
        docs=_docs("graph"),
    ),
    # edit -----------------------------------------------------------------
    _def(
        id="cap:edit.script",
        domain="edit",
        purpose="Apply a semantic edit script to one file through the transaction.",
        owner="ozagia.edit.dispatch",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.edit.dispatch.apply_edit_script"],
        surfaces=["mcp:tool:dev_edit", "mcp:graph:edit"],
        prerequisites=["the existing edit transaction and parse guards"],
        limits=["one file per call; preview is the default"],
        authorization=_EDIT,
        tests=_tests("edit", "code_edit"),
        docs=_docs("edit"),
    ),
    _def(
        id="cap:edit.span",
        domain="edit",
        purpose="Replace an exact text span and return the enclosing graph delta.",
        owner="ozagia.edit.span_edit",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.edit.span_edit.apply_span_edit"],
        surfaces=["mcp:tool:dev_span_edit"],
        prerequisites=["the existing edit transaction", "the target file"],
        limits=["span must be unique unless replace_all is set",
                "preview is the default"],
        authorization=_EDIT,
        tests=_tests("edit", "cst"),
        docs=_docs("edit"),
    ),
    _def(
        id="cap:edit.key",
        domain="edit",
        purpose="Replace the whole text of one graph node by key.",
        owner="ozagia.edit.key_edit",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.edit.key_edit.apply_key_edit"],
        surfaces=["mcp:tool:dev_key_edit"],
        prerequisites=["the node key", "its current base hash"],
        limits=["a stale or file-only base hash is refused",
                "preview is the default"],
        authorization=_EDIT,
        tests=_tests("edit", "cst"),
        docs=_docs("edit"),
    ),
    _def(
        id="cap:edit.replace",
        domain="edit",
        purpose="Replace a literal or regular-expression pattern across files.",
        owner="ozagia.edit.replace",
        side_effect="write",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.edit.replace.replace",
              "impl:ozagia.edit.replace.scan"],
        surfaces=["cli:ozagia.replace", "mcp:tool:dev_replace"],
        prerequisites=["the existing edit transaction", "a reviewed plan id"],
        limits=["--apply needs the matching --plan-id",
                "preview is the default"],
        authorization=_EDIT,
        tests=_tests("replace"),
        docs=_docs("edit"),
    ),
    _def(
        id="cap:edit.rename_preview",
        domain="edit",
        purpose="Preview a rename workspace edit; never write it.",
        owner="ozagia.lsp",
        side_effect="execute",
        exposure=["mcp"],
        impl=["impl:ozagia.lsp.get_client"],
        surfaces=["mcp:tool:dev_rename_preview"],
        prerequisites=["a configured or discoverable language server"],
        limits=["preview only; no file is written"],
        authorization=_READ,
        tests=_tests("lsp"),
        docs=_docs("lsp"),
    ),
    _def(
        id="cap:edit.create",
        domain="edit",
        purpose="Create a new file or node from supplied content.",
        owner="ozagia.integrations.mcp.graph_surface",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.integrations.mcp.graph_surface.create"],
        surfaces=["mcp:graph:create"],
        prerequisites=["the graph surface and a checked parent path"],
        limits=["refuses an existing path", "parse guard before writing"],
        authorization=_EDIT,
        tests=_tests("graph_surface"),
        docs=_docs("graph_surface"),
    ),
    # test -----------------------------------------------------------------
    _def(
        id="cap:test.scoped",
        domain="test",
        purpose="Plan the scoped tests affected by a change; never run them.",
        owner="ozagia.core.testing",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.testing.plan"],
        surfaces=["mcp:tool:dev_tests"],
        prerequisites=["a graph snapshot and test policy"],
        limits=["a plan is not a passed test"],
        authorization=_READ,
        tests=_tests("test_policy"),
        docs=_docs("test"),
    ),
    _def(
        id="cap:test.check",
        domain="test",
        purpose="Run selected tests and report graph keys for failures.",
        owner="ozagia.check",
        side_effect="execute",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.check.run_tests",
              "impl:ozagia.check.resolve_selectors"],
        surfaces=["cli:ozagia.check", "mcp:graph:check"],
        prerequisites=["a runnable test interpreter"],
        limits=["runs only the selected test files",
                "the result is evidence, not a model verdict"],
        authorization="the caller explicitly runs a check command",
        tests=_tests("check"),
        docs=_docs("test"),
    ),
    _def(
        id="cap:test.review_repo",
        domain="test",
        purpose="Run the deterministic whole-repository review packet.",
        owner="ozagia.core.repo_review",
        side_effect="write",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.core.repo_review.review_repo"],
        surfaces=["cli:ozagia.review-repo", "mcp:tool:dev_review_repo"],
        prerequisites=["a Git checkout"],
        limits=["no model call, network call or repository write",
                "saved packets stay under <state dir>/reviews"],
        authorization=_STATE,
        tests=_tests("review"),
        docs=_docs("repo_review"),
    ),
    # goals ----------------------------------------------------------------
    _def(
        id="cap:goals.inspect",
        domain="goals",
        purpose="List and show graph-addressed goals without evaluating them.",
        owner="ozagia.core.goals",
        side_effect="read",
        exposure=["cli", "daemon", "mcp"],
        impl=["impl:ozagia.core.goals.list_goals",
              "impl:ozagia.core.goals.get_goal"],
        surfaces=["cli:ozagia.goal", "mcp:tool:goal_list",
                  "mcp:tool:goal_show", "daemon:op:goals"],
        prerequisites=["the goal store under <state dir>/goals"],
        limits=["read-only; dropped goals are excluded unless asked for"],
        authorization=_READ,
        tests=_tests("goals"),
        docs=_docs("goals"),
    ),
    _def(
        id="cap:goals.check",
        domain="goals",
        purpose="Evaluate goal criteria from the graph, worktree and test record.",
        owner="ozagia.core.goals",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.core.goals.check_goals"],
        surfaces=["mcp:tool:goal_check"],
        prerequisites=["a graph snapshot", "the goal store"],
        limits=["records criterion results and status history only"],
        authorization=_STATE,
        tests=_tests("goals"),
        docs=_docs("goals"),
    ),
    _def(
        id="cap:goals.manage",
        domain="goals",
        purpose="Create, claim or derive goals from review findings.",
        owner="ozagia.core.goals",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.core.goals.add_goal",
              "impl:ozagia.core.goals.claim_goal",
              "impl:ozagia.core.goals.from_review"],
        surfaces=["mcp:tool:goal_add", "mcp:tool:goal_from_review",
                  "mcp:tool:goal_claim"],
        prerequisites=["the goal store", "a graph snapshot for key resolution"],
        limits=["writes only the goal store under <state dir>/goals"],
        authorization=_STATE,
        tests=_tests("goals"),
        docs=_docs("goals"),
    ),
    # tasks ----------------------------------------------------------------
    _def(
        id="cap:tasks.jobs",
        domain="tasks",
        purpose="Wait for, list and read the output of daemon background jobs.",
        owner="ozagia.daemon.jobs",
        side_effect="read",
        exposure=["cli", "daemon", "mcp"],
        impl=["impl:ozagia.daemon.jobs.wait",
              "impl:ozagia.daemon.jobs.log"],
        surfaces=["cli:ozagia.bg", "cli:ozagia.job",
                  "mcp:tool:job_wait", "mcp:tool:job_list",
                  "mcp:tool:job_log", "daemon:op:job_wait",
                  "daemon:op:job_list", "daemon:op:job_log",
                  "daemon:op:job_events", "daemon:op:job_ps"],
        prerequisites=["a running daemon with jobs enabled"],
        limits=["logs are read as bounded chunks", "wait deadlines are caller-owned"],
        authorization=_READ,
        tests=_tests("jobs"),
        docs=_docs("jobs"),
    ),
    _def(
        id="cap:tasks.job_start",
        domain="tasks",
        purpose="Start one explicit background job in the daemon.",
        owner="ozagia.daemon.jobs",
        side_effect="execute",
        exposure=["daemon", "mcp"],
        impl=["impl:ozagia.daemon.jobs.start"],
        surfaces=["daemon:op:job_start", "mcp:tool:job_start"],
        prerequisites=["a running daemon", "jobs enabled in project config"],
        limits=["argv/env/cwd are validated by the job manager"],
        authorization=_EXEC,
        tests=_tests("jobs"),
        docs=_docs("jobs"),
    ),
    _def(
        id="cap:tasks.job_stop",
        domain="tasks",
        purpose="Signal one verified background job.",
        owner="ozagia.daemon.jobs",
        side_effect="execute",
        exposure=["daemon", "mcp"],
        impl=["impl:ozagia.daemon.jobs.stop"],
        surfaces=["daemon:op:job_stop", "mcp:tool:job_stop"],
        prerequisites=["a running daemon and a known job id"],
        limits=["one job per call; a grace period is bounded"],
        authorization=_EXEC,
        tests=_tests("jobs"),
        docs=_docs("jobs"),
    ),
    _def(
        id="cap:tasks.journal_write",
        domain="tasks",
        purpose="Append a decision or verification receipt to the journal.",
        owner="ozagia.core.memory",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.journal_append"],
        surfaces=["mcp:tool:dev_record", "mcp:graph:note"],
        prerequisites=["a graph snapshot and its journal path"],
        limits=["one append-only JSONL line", "no prompts or secret values"],
        authorization=_STATE,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:tasks.journal_read",
        domain="tasks",
        purpose="Read the most recent decision and verification entries.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.journal_decisions"],
        surfaces=["mcp:tool:dev_recent"],
        prerequisites=["the journal under <state dir>"],
        limits=["n bounds the number of entries"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:tasks.working_set",
        domain="tasks",
        purpose="Return a session's bounded working set or start brief.",
        owner="ozagia.core.working_set",
        side_effect="read",
        exposure=["daemon", "mcp"],
        impl=["impl:ozagia.core.working_set.merge_views"],
        surfaces=["daemon:op:working_set", "mcp:tool:dev_working_set"],
        prerequisites=["a daemon with session state"],
        limits=["at most 7,000 characters; 1,500 for a start brief"],
        authorization=_READ,
        tests=_tests("jobs"),
        docs=_docs("session"),
    ),
    _def(
        id="cap:tasks.session_control",
        domain="tasks",
        purpose="Start, record into and finish a replayable DevSession.",
        owner="ozagia.core.session",
        side_effect="write",
        exposure=["mcp"],
        impl=["impl:ozagia.core.session"],
        surfaces=["mcp:tool:dev_session_start",
                  "mcp:tool:dev_session_record",
                  "mcp:tool:dev_session_finish"],
        prerequisites=["the session store under <state dir>"],
        limits=["writes one session record per state change"],
        authorization=_STATE,
        tests=_tests("session"),
        docs=_docs("session"),
    ),
    _def(
        id="cap:tasks.session_inspect",
        domain="tasks",
        purpose="Show a DevSession's state, next action or the session list.",
        owner="ozagia.core.session",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.session"],
        surfaces=["mcp:tool:dev_session_state",
                  "mcp:tool:dev_session_next",
                  "mcp:tool:dev_session_list"],
        prerequisites=["the session store under <state dir>"],
        limits=["read-only over persisted sessions"],
        authorization=_READ,
        tests=_tests("session"),
        docs=_docs("session"),
    ),
    _def(
        id="cap:tasks.session_events",
        domain="tasks",
        purpose="Apply a harness session start, compact or end event.",
        owner="ozagia.daemon",
        side_effect="write",
        exposure=["daemon"],
        impl=["impl:ozagia.daemon.server"],
        surfaces=["daemon:op:session_event"],
        prerequisites=["a running daemon"],
        limits=["clears or drops read ledgers; writes no target file"],
        authorization=_STATE,
        tests=_tests("daemon"),
        docs=_docs("session"),
    ),
    _def(
        id="cap:tasks.profile_event",
        domain="tasks",
        purpose="Persist an allowlisted aggregate session-profile event.",
        owner="ozagia.session_profile_cli",
        side_effect="write",
        exposure=["daemon"],
        impl=["impl:ozagia.session_profile_cli.store_profile_event"],
        surfaces=["daemon:op:profile_event"],
        prerequisites=["a running daemon"],
        limits=["allowlisted metadata only; no prompts or raw logs"],
        authorization=_STATE,
        tests=_tests("profile"),
        docs=_docs("session"),
    ),
    _def(
        id="cap:tasks.bounded",
        domain="tasks",
        purpose="Validate a bounded task specification without executing it.",
        owner="ozagia.core.tasks",
        side_effect="read",
        exposure=["internal"],
        impl=["impl:ozagia.core.tasks.validate_spec"],
        surfaces=[],
        prerequisites=["a TOML or JSON task specification"],
        limits=["validation only; an executor must be injected to run"],
        authorization=_READ,
        tests=_tests("tasks"),
        docs=_docs("tasks"),
    ),
    # environment ----------------------------------------------------------
    _def(
        id="cap:environment.interpreters",
        domain="environment",
        purpose="Discover Python interpreters and what each can import.",
        owner="ozagia.core.environments",
        side_effect="execute",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.core.environments.discover"],
        surfaces=["cli:ozagia.env", "mcp:tool:dev_environment"],
        prerequisites=["Python interpreters on the host or in the checkout"],
        limits=["project-shipped interpreters are read as text unless trusted",
                "probes run `python -I -c` only where the policy allows"],
        authorization="explicit --no-cache/probe or the configured trust policy",
        tests=_tests("environment"),
        docs=_docs("environment"),
    ),
    # resources ------------------------------------------------------------
    _def(
        id="cap:resources.status",
        domain="resources",
        purpose="Report host memory, CPU, disk and process-group pressure.",
        owner="ozagia.core.resources",
        side_effect="read",
        exposure=["internal"],
        impl=["impl:ozagia.core.resources.host_snapshot"],
        surfaces=[],
        prerequisites=["readable /proc and disk mounts"],
        limits=["a missing counter is reported, never guessed"],
        authorization=_READ,
        tests=_tests("resources"),
        docs=_docs("resources"),
    ),
    _def(
        id="cap:resources.files",
        domain="resources",
        purpose="List and explain files created or changed in the checkout.",
        owner="ozagia.core.files",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.files.list_files",
              "impl:ozagia.core.files.explain"],
        surfaces=["mcp:tool:dev_files_list", "mcp:tool:dev_files_explain"],
        prerequisites=["the created-file ledger under <state dir>"],
        limits=["reads the ledger and accepted files; no cleanup"],
        authorization=_READ,
        tests=_tests("files"),
        docs=_docs("files"),
    ),
    _def(
        id="cap:resources.admission",
        domain="resources",
        purpose="Apply configured CPU, memory and file limits to a child process.",
        owner="ozagia.core.resources",
        side_effect="execute",
        exposure=["internal"],
        impl=["impl:ozagia.core.resources.apply_child_limits"],
        surfaces=[],
        prerequisites=["a caller-supplied resource policy"],
        limits=["child-only limits; no host-wide change"],
        authorization=_EXEC,
        tests=_tests("resources"),
        docs=_docs("resources"),
    ),
    # toolchain ------------------------------------------------------------
    _def(
        id="cap:toolchain.plan",
        domain="toolchain",
        purpose="Plan which external tools a task needs and what is missing.",
        owner="ozagia.core.toolchain",
        side_effect="execute",
        exposure=["daemon", "mcp"],
        impl=["impl:ozagia.core.toolchain.tool_plan"],
        surfaces=["mcp:tool:dev_tools", "daemon:op:toolchain"],
        prerequisites=["the versioned toolchain registry"],
        limits=["never installs or uses the network",
                "version and sudo probes are opt-in"],
        authorization="explicit probe_versions/probe_sudo; install is never offered",
        tests=_tests("toolchain"),
        docs=_docs("toolchain"),
    ),
    _def(
        id="cap:toolchain.registry",
        domain="toolchain",
        purpose="Expose the versioned capability/tool registry as data.",
        owner="ozagia.core.toolchain",
        side_effect="read",
        exposure=["internal"],
        impl=["impl:ozagia.core.toolchain.registry"],
        surfaces=["toolchain:registry"],
        prerequisites=[],
        limits=["static registry data; readiness is a separate observation"],
        authorization=_READ,
        tests=_tests("toolchain"),
        docs=_docs("toolchain"),
    ),
    _def(
        id="cap:security.supply_chain",
        domain="toolchain",
        purpose=("Inventory dependency inputs and report offline supply-chain "
                 "attack candidates: untrusted registries, credentials, "
                 "mutable sources, install-time code, missing integrity and "
                 "typosquat/confusion signals."),
        owner="ozagia.core.supply_chain",
        side_effect="read",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.core.supply_chain.analyze",
              "impl:ozagia.core.supply_chain.check",
              "impl:ozagia.supply_chain_cli.run"],
        surfaces=["cli:ozagia.supply-chain",
                  "mcp:tool:dev_supply_chain"],
        prerequisites=["dependency manifests, lockfiles or registry "
                       "configuration under the checkout",
                       "an optional explicit supply-chain policy JSON"],
        limits=["deterministic and offline; no fetch, install, target import "
                "or code execution",
                "findings are candidates; absence of a finding is not a clean "
                "verdict",
                "registry hosts and direct URLs are hashed in findings",
                "online metadata, signatures, attestations and tarball "
                "contents remain unknown"],
        authorization=_READ,
        tests=_tests("supply_chain"),
        docs=_docs("supply_chain"),
    ),
    _def(
        id="cap:languages.support",
        domain="toolchain",
        purpose=("Detect the languages in a checkout, report their component "
                 "readiness, and install declared pip or rustup components."),
        owner="ozagia.languages.model",
        side_effect="network",
        exposure=["cli"],
        impl=["impl:ozagia.languages.model.ensure_for_root",
              "impl:ozagia.languages.model.ensure",
              "impl:ozagia.languages_cli.run"],
        surfaces=["cli:ozagia.languages"],
        prerequisites=["the language support registry", "a checkout to scan"],
        limits=["pip specs and rustup components only; no sudo, npm or OS "
                "package manager",
                "bounded 600 s per install; failures are reported, not raised",
                "optional/manual components are reported but not installed"],
        authorization="explicit CLI; automatic check is opt-out; no sudo",
        tests=_tests("languages"),
        docs=_docs("languages"),
    ),
    _def(
        id="cap:languages.rust_tier",
        domain="toolchain",
        purpose=("Declare the syntactic Rust tier and its explicit limits for "
                 "tags, symbols, imports, edits, LSP, tests and optional SCIP."),
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["internal"],
        impl=["impl:ozagia.core.memory.build_index",
              "impl:ozagia.core.rust_modules.discover",
              "impl:ozagia.core.rust_tests.discover",
              "impl:ozagia.index.scip.load_scip",
              "impl:ozagia.edit.rust_edit",
              "impl:ozagia.lsp",
              "impl:ozagia.intent.run.run_cargo_test"],
        surfaces=[],
        prerequisites=["Rust source trees in the checkout",
                       "optional tree-sitter-rust grammar",
                       "optional rust-analyzer for the precise layer"],
        limits=["tags are structural; macros, generated files and cfg-hidden "
                "items are not expanded",
                "symbols and imports are syntactic; no type inference, trait "
                "resolution or MIR",
                "external crates and ambiguous bindings stay unresolved",
                "edits are preview-first and same-file rename only; "
                "cross-file rename is an LSP preview",
                "rust-analyzer is optional; first queries wait for indexing",
                "cargo tests use target-level affected selection and cache, "
                "stable human output only; failures are never cached"],
        authorization=("read-only by default; edits are explicit transactions; "
                       "LSP and tests run only when invoked"),
        tests=_tests("memory", "repo_map", "scip", "lsp", "edit",
                     "test_policy"),
        docs=_docs("rust", "scip", "lsp", "edit", "test"),
    ),
    _def(
        id="cap:migration.overlay",
        domain="toolchain",
        purpose=("Inventory configured Python and Rust trees, pair symbols, "
                 "derive migration status and parity flags, report inference "
                 "candidates without accepting them, and emit a task checklist "
                 "or a bounded scaffold preview."),
        owner="ozagia.migration.report",
        side_effect="write",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.migration.inventory.inventory_python",
              "impl:ozagia.migration.inventory.inventory_rust",
              "impl:ozagia.migration.match.match",
              "impl:ozagia.migration.report.build_report",
              "impl:ozagia.migration.tasks.build_tasks",
              "impl:ozagia.migration.scaffold.scaffold",
              "impl:ozagia.migration_cli.run"],
        surfaces=["cli:ozagia.migration", "mcp:tool:dev_migration"],
        prerequisites=["a validated [migration] section naming two existing "
                       "trees inside the repository",
                       "explicit pairs, or inference for candidates only"],
        limits=["status and tasks never modify a source tree or execute the "
                "project",
                "inference is a name/module heuristic and never accepted",
                "reports are bounded and written only under the state directory",
                "scaffold supports simple scalar dataclasses/functions only",
                "the MCP tool is preview-only; only the CLI --write path edits"],
        authorization=("read-only by default; scaffold --write is an explicit "
                       "CLI edit through the transaction; MCP never writes"),
        tests=_tests("migration", "mcp_migration"),
        docs=_docs("migration"),
    ),
    # status ---------------------------------------------------------------
    _def(
        id="cap:status.inspect",
        domain="status",
        purpose="Compose one read-only status snapshot from existing owners.",
        owner="ozagia.core.status",
        side_effect="read",
        exposure=["internal"],
        impl=["impl:ozagia.core.status.status"],
        surfaces=["status:workspace", "status:resources", "status:files",
                  "status:runtime", "status:daemon", "status:doctor",
                  "status:config", "status:reviews", "status:integrations",
                  "status:capabilities", "status:jobs", "status:admissions",
                  "status:providers", "status:snapshot", "status:sessions",
                  "status:ownership"],
        prerequisites=["the repository root"],
        limits=["absence and unknown are distinct; no service is started"],
        authorization=_READ,
        tests=_tests("status"),
        docs=_docs("status"),
    ),
    _def(
        id="cap:status.doctor",
        domain="status",
        purpose="Diagnose the snapshot, optional tools and local prerequisites.",
        owner="ozagia.doctor",
        side_effect="read",
        exposure=["cli"],
        impl=["impl:ozagia.doctor.diagnose"],
        surfaces=["cli:ozagia.doctor"],
        prerequisites=["the repository root"],
        limits=["reports missing optional tools; installs nothing"],
        authorization=_READ,
        tests=_tests("status"),
        docs=_docs("status"),
    ),
    _def(
        id="cap:status.owners",
        domain="status",
        purpose="Show which harness sessions edited a file and their state.",
        owner="ozagia.core.owners",
        side_effect="read",
        exposure=["cli", "daemon", "mcp"],
        impl=["impl:ozagia.core.owners.who"],
        surfaces=["cli:ozagia.who", "mcp:tool:dev_who",
                  "daemon:op:who", "daemon:op:owner_check"],
        prerequisites=["the daemon journal or edit journals"],
        limits=["advisory only; it never blocks or claims ownership"],
        authorization=_READ,
        tests=_tests("owners"),
        docs=_docs("status"),
    ),
    _def(
        id="cap:status.graph_stats",
        domain="status",
        purpose="Report graph snapshot histograms and freshness.",
        owner="ozagia.core.memory",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.memory.stats"],
        surfaces=["mcp:tool:dev_stats"],
        prerequisites=["a graph snapshot"],
        limits=["counts and ages only; no rebuild"],
        authorization=_READ,
        tests=_tests("memory"),
        docs=_docs("status"),
    ),
    _def(
        id="cap:status.daemon_stats",
        domain="status",
        purpose="Report daemon counters without changing daemon state.",
        owner="ozagia.daemon.server",
        side_effect="read",
        exposure=["daemon"],
        impl=["impl:ozagia.daemon.server.stats"],
        surfaces=["daemon:op:stats"],
        prerequisites=["a running daemon"],
        limits=["counters are in-process and reset with the daemon"],
        authorization=_READ,
        tests=_tests("daemon"),
        docs=_docs("status"),
    ),
    _def(
        id="cap:status.config",
        domain="status",
        purpose="Inspect settings, preview config writes and migrate .ozagia.toml.",
        owner="ozagia.core.settings",
        side_effect="write",
        exposure=["cli"],
        impl=["impl:ozagia.core.settings.schema",
              "impl:ozagia.core.settings.values"],
        surfaces=["cli:ozagia.config"],
        prerequisites=["the settings registry and project config"],
        limits=["writes are previewed; --dry-run changes nothing"],
        authorization=_STATE,
        tests=_tests("settings"),
        docs=_docs("status"),
    ),
    # federation -----------------------------------------------------------
    _def(
        id="cap:federation.status",
        domain="federation",
        purpose="Report linked-repository members, aliases and freshness.",
        owner="ozagia.core.federation",
        side_effect="write",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.core.federation.status"],
        surfaces=["cli:ozagia.federation", "mcp:tool:federation_status"],
        prerequisites=["explicitly configured linked repositories"],
        limits=["reference sync may write member snapshots under the state dir",
                "no member is modified"],
        authorization=_STATE,
        tests=_tests("federation"),
        docs=_docs("federation"),
    ),
    _def(
        id="cap:federation.search",
        domain="federation",
        purpose="Search selected members for graph nodes.",
        owner="ozagia.core.federation",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.federation.search"],
        surfaces=["mcp:tool:federation_search"],
        prerequisites=["indexed linked repositories"],
        limits=["limit bounds the hits; members are read-only"],
        authorization=_READ,
        tests=_tests("federation_surface"),
        docs=_docs("federation"),
    ),
    _def(
        id="cap:federation.node",
        domain="federation",
        purpose="Read one qualified node from a selected member graph.",
        owner="ozagia.core.federation",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.federation.node"],
        surfaces=["mcp:tool:federation_node"],
        prerequisites=["indexed linked repositories"],
        limits=["one qualified key per call"],
        authorization=_READ,
        tests=_tests("federation_surface"),
        docs=_docs("federation"),
    ),
    _def(
        id="cap:federation.check",
        domain="federation",
        purpose="Compare declared cross-repository references, dependencies and drift.",
        owner="ozagia.core.cross_repo",
        side_effect="read",
        exposure=["mcp"],
        impl=["impl:ozagia.core.cross_repo.check",
              "impl:ozagia.core.dependency_links.check"],
        surfaces=["mcp:tool:federation_check"],
        prerequisites=["indexed linked repositories"],
        limits=["reports comparisons; it never rewrites a member"],
        authorization=_READ,
        tests=_tests("federation_surface"),
        docs=_docs("federation"),
    ),
    # research -------------------------------------------------------------
    _def(
        id="cap:research.session",
        domain="research",
        purpose="Keep a bounded, evidence-linked research session.",
        owner="ozagia.research.service",
        side_effect="write",
        exposure=["internal"],
        impl=["impl:ozagia.research.service"],
        surfaces=[],
        prerequisites=["the research package and a workspace"],
        limits=["research tools load dynamically and are not in the static "
                "full-surface inventory"],
        authorization=_STATE,
        tests=_tests("research"),
        docs=_docs("research"),
    ),
    _def(
        id="cap:research.fetch",
        domain="research",
        purpose="Fetch one explicitly requested source for research.",
        owner="ozagia.research.fallback",
        side_effect="network",
        exposure=["internal"],
        impl=["impl:ozagia.research.fallback.research_fetch_tool"],
        surfaces=[],
        prerequisites=["network access", "an explicit caller request"],
        limits=["one bounded fetch per call; not a crawler",
                "the dynamic MCP tool is not in the static inventory"],
        authorization="explicit caller request; no automatic network fetch",
        tests=_tests("research"),
        docs=_docs("research"),
    ),
    # runtime --------------------------------------------------------------
    _def(
        id="cap:runtime.daemon",
        domain="runtime",
        purpose="Serve, ping or stop the graph daemon for one repository.",
        owner="ozagia.daemon.server",
        side_effect="execute",
        exposure=["cli", "daemon"],
        impl=["impl:ozagia.daemon.server"],
        surfaces=["cli:ozagia.daemon", "daemon:op:ping",
                  "daemon:op:shutdown"],
        prerequisites=["a repository root and a Unix socket path"],
        limits=["one repository per daemon", "the socket is removed on exit"],
        authorization=_EXEC,
        tests=_tests("runtime"),
        docs=_docs("runtime"),
    ),
    _def(
        id="cap:runtime.mcp_server",
        domain="runtime",
        purpose="Serve the read-mostly MCP tools over stdio.",
        owner="ozagia.integrations.mcp.server",
        side_effect="execute",
        exposure=["cli"],
        impl=["impl:ozagia.integrations.mcp.server.serve"],
        surfaces=["cli:ozagia.mcp"],
        prerequisites=["a graph snapshot for coding tools"],
        limits=["one stdio server process; no shell or network tool"],
        authorization=_EXEC,
        tests=_tests("mcp"),
        docs=_docs("graph"),
    ),
    _def(
        id="cap:runtime.agent",
        domain="runtime",
        purpose="Run one OpenCode agent session with the graph daemon.",
        owner="ozagia.harness.opencode",
        side_effect="model",
        exposure=["cli"],
        impl=["impl:ozagia.harness.opencode.run_agent"],
        surfaces=["cli:ozagia.agent"],
        prerequisites=["an installed OpenCode", "a repository"],
        limits=["one session; timeout and clone options are caller-owned"],
        authorization="the operator starts an agent session explicitly",
        tests=_tests("cli"),
        docs=_docs("runtime"),
    ),
    _def(
        id="cap:runtime.install",
        domain="runtime",
        purpose="Install a harness integration into a workdir.",
        owner="ozagia.selfinstall",
        side_effect="write",
        exposure=["cli"],
        impl=["impl:ozagia.selfinstall.main"],
        surfaces=["cli:ozagia.install"],
        prerequisites=["a target harness and workdir"],
        limits=["writes only the integration files it names"],
        authorization="the operator runs install explicitly",
        tests=_tests("cli"),
        docs=_docs("install"),
    ),
    _def(
        id="cap:runtime.self",
        domain="runtime",
        purpose="Inspect, upgrade or uninstall the installed Ozagia bundle.",
        owner="ozagia.selfinstall",
        side_effect="write",
        exposure=["cli"],
        impl=["impl:ozagia.selfinstall.info"],
        surfaces=["cli:ozagia.self"],
        prerequisites=["an installed Ozagia bundle"],
        limits=["upgrade/uninstall act on the bundle, not a target repository"],
        authorization="the operator runs the command explicitly",
        tests=_tests("cli"),
        docs=_docs("install"),
    ),
    _def(
        id="cap:runtime.hooks",
        domain="runtime",
        purpose="Install, remove or report opt-in Git commit/push hooks.",
        owner="ozagia.hooks_cli",
        side_effect="write",
        exposure=["cli"],
        impl=["impl:ozagia.hooks_cli.main"],
        surfaces=["cli:ozagia.hooks"],
        prerequisites=["a Git checkout"],
        limits=["writes only hook files it installs"],
        authorization="the operator runs hooks install/uninstall explicitly",
        tests=_tests("hooks"),
        docs=_docs("install"),
    ),
    _def(
        id="cap:runtime.status",
        domain="runtime",
        purpose="Show configured services, processes and tunnels with live state.",
        owner="ozagia.core.runtime",
        side_effect="read",
        exposure=["cli"],
        impl=["impl:ozagia.core.runtime.runtime_status"],
        surfaces=["cli:ozagia.runtime"],
        prerequisites=["project runtime configuration"],
        limits=["observes; it does not start or stop anything"],
        authorization=_READ,
        tests=_tests("runtime"),
        docs=_docs("runtime"),
    ),
    _def(
        id="cap:runtime.service",
        domain="runtime",
        purpose="Start, run, stop, restart, inspect or log one local service.",
        owner="ozagia.core.runtime",
        side_effect="execute",
        exposure=["cli"],
        impl=["impl:ozagia.core.runtime"],
        surfaces=["cli:ozagia.service"],
        prerequisites=["a declared service and its argv"],
        limits=["one service per command", "verified process identity"],
        authorization=_EXEC,
        tests=_tests("runtime"),
        docs=_docs("runtime"),
    ),
    _def(
        id="cap:runtime.process",
        domain="runtime",
        purpose="Start, run, inspect or stop one explicit local process.",
        owner="ozagia.core.runtime",
        side_effect="execute",
        exposure=["cli"],
        impl=["impl:ozagia.core.runtime"],
        surfaces=["cli:ozagia.process"],
        prerequisites=["an explicit argv"],
        limits=["one process per command", "verified process identity"],
        authorization=_EXEC,
        tests=_tests("runtime"),
        docs=_docs("runtime"),
    ),
    _def(
        id="cap:runtime.tunnel",
        domain="runtime",
        purpose="Open or plan a configured local tunnel.",
        owner="ozagia.core.runtime",
        side_effect="network",
        exposure=["cli"],
        impl=["impl:ozagia.core.runtime"],
        surfaces=["cli:ozagia.tunnel"],
        prerequisites=["a configured tunnel and an explicit --allow-network"],
        limits=["policy-gated; one tunnel per command"],
        authorization=_EXEC,
        tests=_tests("runtime"),
        docs=_docs("runtime"),
    ),
    _def(
        id="cap:runtime.secrets",
        domain="runtime",
        purpose="Report secret providers and references without resolving values.",
        owner="ozagia.core.secrets",
        side_effect="read",
        exposure=["daemon", "mcp"],
        impl=["impl:ozagia.core.secrets.discover_refs"],
        surfaces=["daemon:op:secrets", "mcp:tool:dev_secrets"],
        prerequisites=["the secret reference broker"],
        limits=["never captures, resolves or reveals a value"],
        authorization=_READ,
        tests=_tests("secrets"),
        docs=_docs("secrets"),
    ),
    _def(
        id="cap:runtime.tool_dispatch",
        domain="runtime",
        purpose="Dispatch one harness tool call through the existing handlers.",
        owner="ozagia.intent",
        side_effect="execute",
        exposure=["daemon"],
        impl=["impl:ozagia.intent.handle"],
        surfaces=["daemon:op:tool"],
        prerequisites=["a running daemon and a harness session"],
        limits=["edits go through edit/txn.py; shell is the harness's own tool"],
        authorization=_EXEC,
        tests=_tests("intent"),
        docs=_docs("runtime"),
    ),
    _def(
        id="cap:runtime.harness_handshake",
        domain="runtime",
        purpose="Record a harness plugin hello and its declared tool options.",
        owner="ozagia.daemon.server",
        side_effect="write",
        exposure=["daemon"],
        impl=["impl:ozagia.daemon.server"],
        surfaces=["daemon:op:hello"],
        prerequisites=["a running daemon"],
        limits=["records counters only; grants nothing"],
        authorization=_STATE,
        tests=_tests("daemon"),
        docs=_docs("runtime"),
    ),
    # actions --------------------------------------------------------------
    _def(
        id="cap:actions.relay",
        domain="actions",
        purpose="Plan or run one policy-gated command or TCP relay.",
        owner="ozagia.core.relay",
        side_effect="network",
        exposure=["cli"],
        impl=["impl:ozagia.core.relay.register_relay"],
        surfaces=["cli:ozagia.relay"],
        prerequisites=["a relay spec", "explicit --allow-live/--allow-network"],
        limits=["one chain per invocation", "allowlisted commands only"],
        authorization=_EXEC,
        tests=_tests("relay"),
        docs=_docs("relay"),
    ),
    _def(
        id="cap:actions.iac",
        domain="actions",
        purpose="Run source-only Terraform/OpenTofu pre-plan checks.",
        owner="ozagia.core.iac_check",
        side_effect="read",
        exposure=["cli"],
        impl=["impl:ozagia.core.iac_check.check_iac"],
        surfaces=["cli:ozagia.iac"],
        prerequisites=["Terraform/OpenTofu source files"],
        limits=["no plan, provider or project code is executed"],
        authorization=_READ,
        tests=_tests("iac"),
        docs=_docs("iac"),
    ),
    _def(
        id="cap:actions.live",
        domain="actions",
        purpose="Validate, diff or save a supplied live-state artifact.",
        owner="ozagia.core.live",
        side_effect="write",
        exposure=["cli"],
        impl=["impl:ozagia.core.live.import_snapshot",
              "impl:ozagia.core.live.write_snapshot"],
        surfaces=["cli:ozagia.live"],
        prerequisites=["a caller-supplied artifact"],
        limits=["no live provider call", "saved artifacts stay under <state dir>"],
        authorization="the operator supplies the artifact explicitly",
        tests=_tests("live"),
        docs=_docs("live"),
    ),
    _def(
        id="cap:actions.ops",
        domain="actions",
        purpose="Append one redacted ops receipt or read a bounded handoff.",
        owner="ozagia.core.ops",
        side_effect="write",
        exposure=["cli"],
        impl=["impl:ozagia.core.ops.append_record",
              "impl:ozagia.core.ops.resume_summary"],
        surfaces=["cli:ozagia.ops"],
        prerequisites=["the ops ledger under <state dir>"],
        limits=["redacted metadata only", "receipts are not replayed"],
        authorization=_STATE,
        tests=_tests("ops"),
        docs=_docs("ops"),
    ),
    _def(
        id="cap:actions.preflight",
        domain="actions",
        purpose="Build a dry-run, scrubbed IaC emulator plan.",
        owner="ozagia.core.preflight",
        side_effect="read",
        exposure=["cli"],
        impl=["impl:ozagia.core.preflight.build_preflight"],
        surfaces=["cli:ozagia.preflight"],
        prerequisites=["a configured emulator binary"],
        limits=["dry-run only; the plan is never executed"],
        authorization=_READ,
        tests=_tests("preflight"),
        docs=_docs("preflight"),
    ),
    # eval -----------------------------------------------------------------
    _def(
        id="cap:eval.agent",
        domain="eval",
        purpose="Run the break/fix agent evaluation harness.",
        owner="ozagia.eval.agent",
        side_effect="model",
        exposure=["cli"],
        impl=["impl:ozagia.eval.agent.main"],
        surfaces=["cli:ozagia.eval"],
        prerequisites=["--live and OZAGIA_EVAL_LIVE=1 for live drivers"],
        limits=["offline fixtures by default; live runs need approval"],
        authorization="explicit --live plus OZAGIA_EVAL_LIVE=1 and human approval",
        tests=_tests("eval_agent"),
        docs=_docs("eval_agent"),
    ),
    _def(
        id="cap:eval.retrieval",
        domain="eval",
        purpose="Run the offline retrieval evaluation fixtures.",
        owner="ozagia.eval.retrieval",
        side_effect="read",
        exposure=["internal"],
        impl=["impl:ozagia.eval.retrieval.load_tasks"],
        surfaces=[],
        prerequisites=["the fixture task sets"],
        limits=["no model or network call"],
        authorization=_READ,
        tests=_tests("eval_retrieval"),
        docs=_docs("eval_retrieval"),
    ),
    _def(
        id="cap:eval.bench",
        domain="eval",
        purpose="Benchmark cold and warm index performance on a checkout.",
        owner="ozagia.eval.bench",
        side_effect="write",
        exposure=["internal"],
        impl=["impl:ozagia.eval.bench.main"],
        surfaces=[],
        prerequisites=["a checkout and a writable state dir"],
        limits=["writes a temporary index under the state dir",
                "no network or model call"],
        authorization=_STATE,
        tests=_tests("eval_retrieval"),
        docs=_docs("eval_retrieval"),
    ),
    _def(
        id="cap:profile.ingest",
        domain="eval",
        purpose=("Read existing profiler, OTLP and Prometheus telemetry artifacts "
                 "and join their active frames to the repository graph."),
        owner="ozagia.core.profile.report",
        side_effect="read",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.core.profile.report.build_report"],
        surfaces=["cli:ozagia.profile", "mcp:tool:dev_profile"],
        prerequisites=["a readable cProfile, speedscope, Chrome, collapsed-stack "
                       "or OTLP JSON artifact",
                       "a tag cache for graph joins; missing frames stay unknown"],
        limits=["read-only over the target; no import, profiler start, network or model call",
                "graph joins are syntactic and limited to mapped definitions",
                "writes only the caller-selected --out report or the state tag cache"],
        authorization=_READ,
        tests=_tests("app_profile"),
        docs=_docs("profile"),
    ),
    _def(
        id="cap:profile.collect",
        domain="eval",
        purpose=("Run one explicitly approved profiler command under the "
                 "resource policy, then ingest its artifact."),
        owner="ozagia.core.profile.collect",
        side_effect="execute",
        exposure=["cli"],
        impl=["impl:ozagia.core.profile.collect.run_collection"],
        surfaces=["cli:ozagia.profile-run"],
        prerequisites=["explicit --allow-exec",
                       "an installed cProfile-capable interpreter, py-spy or node"],
        limits=["argv only; no shell; cwd must stay inside the repository",
                "bounded timeout and existing memory/file rlimits",
                "admission uses the requested/expected disk and absolute floors",
                "the artifact and report are written under the selected output directory"],
        authorization="explicit --allow-exec plus resource admission",
        tests=_tests("app_profile"),
        docs=_docs("profile"),
    ),
    # preflight ------------------------------------------------------------
    _def(
        id="cap:preflight.task",
        domain="tasks",
        purpose=("Compose a read-only preflight from a goal's criteria, graph "
                 "freshness, capability readiness, interpreters, resources and "
                 "the affected-test plan."),
        owner="ozagia.core.task_preflight",
        side_effect="read",
        exposure=["cli"],
        impl=["impl:ozagia.core.task_preflight.build_preflight",
              "impl:ozagia.task_preflight_cli.run"],
        surfaces=["cli:ozagia.task-preflight"],
        prerequisites=["the goal store", "an optional graph snapshot",
                       "the WP1 self-graph"],
        limits=["read-only; no test execution, graph refresh or daemon start",
                "bounded by --limit/--cursor and --timeout-s",
                "a test plan is never a passed test"],
        authorization=_READ,
        tests=_tests("task_preflight"),
        docs=_docs("task_preflight"),
    ),
    # self-graph -----------------------------------------------------------
    _def(
        id="cap:self_graph.inspect",
        domain="status",
        purpose="List, show and audit declared capabilities against observed surfaces.",
        owner="ozagia.core.self_graph",
        side_effect="read",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.core.self_graph.build_self_graph",
              "impl:ozagia.core.capabilities.catalogue"],
        surfaces=["cli:ozagia.capabilities", "mcp:tool:dev_capabilities"],
        prerequisites=["the built-in catalogue and the generated inventory"],
        limits=["read-only; no probe, daemon start or target import",
                "pages bounded by --limit/--cursor"],
        authorization=_READ,
        tests=_tests("self"),
        docs=_docs("capabilities"),
    ),
    _def(
        id="cap:review.deep",
        domain="test",
        purpose=("Compose a subject-based review report with explicit coverage, "
                 "evidence, budgets and optional scanner, profile or model inputs."),
        owner="ozagia.core.deep_review.orchestrator",
        side_effect="write",
        exposure=["cli", "mcp"],
        impl=["impl:ozagia.core.deep_review.orchestrator.build_deep_review"],
        surfaces=["cli:ozagia.deep-review", "mcp:tool:dev_deep_review"],
        prerequisites=["a readable checkout; a fresh graph for graph-dependent checks",
                       "explicit allowlisted scanner names for external security tools",
                       "live gates and a provider endpoint for the optional model pass"],
        limits=["deterministic and offline by default; no test execution, target import or network",
                "security tools run only when named with --security-tool; --security-network is a separate opt-in, and unknown/unavailable/refused tools are coverage, not crashes",
                "optional --model-pass requires --live and OZAGIA_EVAL_LIVE=1; candidates are advisory, unverified, role-mapped and never blocking",
                "model time, request, token and cost budgets apply; token/cost caps depend on provider-reported usage and cannot preempt an in-flight request",
                "optional --ocelot clusters model candidates only, with exact fallback; no install, network or model call",
                "profile reports and --run-bench commands are explicit inputs; measured evidence is not a verdict",
                "optional --local-advisories matches supported exact pins and selected SemVer, Go or PEP 440 ranges from a local file; no fetch or clean verdict",
                "evidence excerpts are bounded; sbom.json is inventory-shaped with CVE state unknown",
                "writes reports to the report directory; --index may refresh the graph under .ozagia"],
        authorization=("none: caller runs the command; scanners and model calls need explicit flags/gates; writes stay under report/state"),
        tests=_tests("deep_review"),
        docs=_docs("deep_review"),
    ),
)


@lru_cache(maxsize=1)
def catalogue() -> tuple[Capability, ...]:
    """Return the validated built-in catalogue, sorted by canonical id."""
    capabilities = tuple(
        sorted((build_capability(record) for record in _BUILTIN_DEFINITIONS),
               key=lambda item: item.id))
    C.assert_unique_ids(item.id for item in capabilities)
    return capabilities


def build_catalogue(
        definitions: Iterable[Mapping[str, Any]] | None = None
) -> tuple[Capability, ...]:
    """Validate an explicit definition iterable (the built-in one by default).

    Tests and diagnostics use this to build a small synthetic catalogue without
    mutating the built-in authority or writing a file.
    """
    records = _BUILTIN_DEFINITIONS if definitions is None else tuple(definitions)
    capabilities = tuple(
        sorted((build_capability(record) for record in records),
               key=lambda item: item.id))
    C.assert_unique_ids(item.id for item in capabilities)
    return capabilities


def get(capability_id: str) -> Capability:
    """Look up one capability by id, accepting only canonical ``cap:`` ids."""
    try:
        parsed = C.parse_id(capability_id)
    except C.ContractError as exc:
        raise CapabilityError(f"unknown capability {capability_id!r}") from exc
    if parsed.kind != "cap":
        raise CapabilityError(f"not a capability id: {capability_id!r}")
    for capability in catalogue():
        if capability.id == parsed.canonical:
            return capability
    raise CapabilityError(f"unknown capability {parsed.canonical!r}")


def catalogue_hash(capabilities: Sequence[Capability] | None = None) -> str:
    """SHA-256 of the canonical capability records."""
    items = catalogue() if capabilities is None else tuple(capabilities)
    return C.content_hash([item.to_dict() for item in items])


def domain_counts(capabilities: Sequence[Capability] | None = None
                  ) -> dict[str, int]:
    items = catalogue() if capabilities is None else tuple(capabilities)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.domain] = counts.get(item.domain, 0) + 1
    return {domain: counts[domain] for domain in sorted(counts)}


def surface_index(capabilities: Sequence[Capability] | None = None
                  ) -> dict[str, tuple[str, ...]]:
    """Map each declared surface to the capabilities that declare it."""
    items = catalogue() if capabilities is None else tuple(capabilities)
    index: dict[str, list[str]] = {}
    for item in items:
        for surface in item.surfaces:
            index.setdefault(surface, []).append(item.id)
    return {surface: tuple(sorted(set(ids)))
            for surface, ids in sorted(index.items())}


def duplicate_surfaces(capabilities: Sequence[Capability] | None = None
                       ) -> dict[str, tuple[str, ...]]:
    """Declared surfaces owned by more than one capability."""
    return {surface: ids for surface, ids in
            surface_index(capabilities).items() if len(ids) > 1}


__all__ = [
    "CAPABILITY_VERSION", "CATALOGUE_NAMESPACE", "CATALOGUE_VERSION",
    "Capability", "CapabilityError", "DOMAINS", "EXPOSURES", "ORIGINS",
    "SIDE_EFFECTS", "build_capability", "build_catalogue", "catalogue",
    "catalogue_hash", "domain_counts", "duplicate_surfaces", "get",
    "surface_index",
]
