"""Data-defined apertures: the projection rules behind a graph view.

An aperture is a small, versioned definition, not a renderer. It says which
node and edge types a view distinguishes, at what grain, how unresolved or
over-grain material is grouped, which layout is wanted and how much may be
shown. The same definition can later be rendered by any host.

The schema is deliberately plain data so a repository can add or override an
aperture in ``ozagia/apertures/*.toml`` without importing Python::

    schema = 1
    id = "architecture"
    title = "Architecture"
    description = "Modules, files, symbols and imports."
    grain = "file"

    [resolves]
    nodes = ["Repo", "File", "Symbol"]
    edges = ["contains", "defines"]
    attrs = ["name", "path"]

    [scope]
    depth = 2

    [collapse]
    unresolved = "other"

    [collapse.groups]
    Symbol = "symbol"

    [render]
    layout = "layered"
    seed = 7

    [render.encoding]
    color = "group"

    [limits]
    max_nodes = 400
    max_edges = 1200

``validate_aperture`` returns a fully normalised copy: defaults filled in,
type lists sorted and de-duplicated, and every error naming its field. The
built-ins live here; a repository override replaces a built-in with the same
id, it does not merge with it.
"""
from __future__ import annotations

import copy
import re
import tomllib
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
LAYOUTS = ("force", "layered", "timeline", "matrix", "table")
TRUNCATION = ("degree", "id")
UNRESOLVED = "other"
_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")

# A stable palette for legends. Groups are assigned colours by sorted group
# id, so the same view always has the same colours.
PALETTE = (
    "#2f7d6d", "#c0504d", "#3b6ea5", "#b07d2b", "#6b5b95",
    "#2c7a7b", "#a0522d", "#5b6b73", "#8c6d31", "#4a7c59",
    "#9c4f96", "#4f6d8f", "#bc7a3b", "#6f8f3f", "#8f4f4f",
)


class ApertureError(ValueError):
    """A malformed aperture definition."""


def _error(field: str, message: str) -> ApertureError:
    return ApertureError(f"{field}: {message}")


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _error(field, "must be a string")
    text = value.strip()
    if not text and not allow_empty:
        raise _error(field, "must be non-empty")
    return text


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise _error(field, "must be a list of strings")
    out: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise _error(f"{field}[{index}]", "must be a non-empty string")
        out.add(item.strip())
    return sorted(out)


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(field, "must be a table/object")
    return value


def _string_map(value: Any, field: str) -> dict[str, str]:
    data = _mapping(value, field)
    out: dict[str, str] = {}
    for key, item in data.items():
        if not isinstance(key, str) or not key.strip():
            raise _error(field, "keys must be non-empty strings")
        if not isinstance(item, str) or not item.strip():
            raise _error(f"{field}.{key}", "must be a non-empty string")
        out[key.strip()] = item.strip()
    return dict(sorted(out.items()))


def _positive_int(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(field, f"must be an integer >= {minimum}")
    if value < minimum:
        raise _error(field, f"must be an integer >= {minimum}")
    return value


def _maybe_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field, minimum=0)


def _check_keys(data: dict[str, Any], allowed: Iterable[str], field: str) -> None:
    unknown = sorted(str(key) for key in set(data) - set(allowed))
    if unknown:
        raise _error(field, f"unknown key(s): {', '.join(unknown)}")


def validate_aperture(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise one aperture definition.

    Raises ``ApertureError`` naming the offending field. The result is JSON
    serialisable and safe to copy; the input is not modified.
    """
    if not isinstance(data, dict):
        raise _error("aperture", "must be a table/object")
    data = copy.deepcopy(data)
    _check_keys(
        data,
        ("schema", "schema_version", "id", "title", "description", "resolves",
         "grain", "scope", "collapse", "render", "limits"),
        "aperture",
    )
    schema = data.get("schema", data.get("schema_version", SCHEMA_VERSION))
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise _error("schema", "must be an integer")
    if schema != SCHEMA_VERSION:
        raise _error("schema", f"unsupported version {schema}; expected {SCHEMA_VERSION}")

    aperture_id = _string(data.get("id"), "id")
    if not _ID_RE.fullmatch(aperture_id):
        raise _error("id", "must be a slug (letters, digits, '_' and '-')")
    title = _string(data.get("title"), "title")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise _error("description", "must be a string")

    resolves = _mapping(data.get("resolves", {}), "resolves")
    _check_keys(resolves, ("nodes", "edges", "attrs"), "resolves")
    resolved = {
        "nodes": _string_list(resolves.get("nodes", []), "resolves.nodes"),
        "edges": _string_list(resolves.get("edges", []), "resolves.edges"),
        "attrs": _string_list(resolves.get("attrs", []), "resolves.attrs"),
    }

    grain = _string(data.get("grain", "node"), "grain")

    scope = _mapping(data.get("scope", {}), "scope")
    _check_keys(scope, ("root", "depth", "node_types", "edge_types"), "scope")
    root = scope.get("root")
    if root is not None:
        if isinstance(root, bool) or not isinstance(root, (str, int)):
            raise _error("scope.root", "must be a node id or null")
        root = str(root)
        if not root:
            raise _error("scope.root", "must be a node id or null")
    normalised_scope = {
        "root": root,
        "depth": _maybe_int(scope.get("depth"), "scope.depth"),
        "node_types": _string_list(scope.get("node_types", []),
                                   "scope.node_types"),
        "edge_types": _string_list(scope.get("edge_types", []),
                                   "scope.edge_types"),
    }

    collapse = _mapping(data.get("collapse", {}), "collapse")
    _check_keys(collapse, ("unresolved", "groups", "by_attr", "labels"),
                "collapse")
    unresolved = _string(collapse.get("unresolved", UNRESOLVED),
                         "collapse.unresolved")
    if unresolved != UNRESOLVED:
        raise _error("collapse.unresolved",
                     f"must be {UNRESOLVED!r}; unresolved types are never renamed")
    normalised_collapse = {
        "unresolved": unresolved,
        "groups": _string_map(collapse.get("groups", {}), "collapse.groups"),
        "by_attr": _string_map(collapse.get("by_attr", {}), "collapse.by_attr"),
        "labels": _string_map(collapse.get("labels", {}), "collapse.labels"),
    }

    render = _mapping(data.get("render", {}), "render")
    _check_keys(render, ("layout", "seed", "encoding"), "render")
    layout = _string(render.get("layout", "force"), "render.layout")
    if layout not in LAYOUTS:
        raise _error("render.layout",
                     f"{layout!r} is not one of {', '.join(LAYOUTS)}")
    seed = render.get("seed", 7)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise _error("render.seed", "must be an integer >= 0")
    encoding = _mapping(render.get("encoding", {}), "render.encoding")
    _check_keys(encoding, ("color", "size", "label"), "render.encoding")
    normalised_encoding = {
        "color": _string(encoding.get("color", "group"),
                         "render.encoding.color", allow_empty=False),
        "size": _string(encoding.get("size", "weight"),
                        "render.encoding.size", allow_empty=False),
        "label": _string(encoding.get("label", "label"),
                         "render.encoding.label", allow_empty=False),
    }

    limits = _mapping(data.get("limits", {}), "limits")
    _check_keys(limits, ("max_nodes", "max_edges", "truncate"), "limits")
    truncate = _string(limits.get("truncate", "degree"), "limits.truncate")
    if truncate not in TRUNCATION:
        raise _error("limits.truncate",
                     f"{truncate!r} is not one of {', '.join(TRUNCATION)}")
    normalised_limits = {
        "max_nodes": _positive_int(limits.get("max_nodes", 500),
                                   "limits.max_nodes"),
        "max_edges": _positive_int(limits.get("max_edges", 1500),
                                   "limits.max_edges"),
        "truncate": truncate,
    }

    return {
        "schema": SCHEMA_VERSION,
        "id": aperture_id,
        "title": title,
        "description": description.strip(),
        "resolves": resolved,
        "grain": grain,
        "scope": normalised_scope,
        "collapse": normalised_collapse,
        "render": {
            "layout": layout,
            "seed": seed,
            "encoding": normalised_encoding,
        },
        "limits": normalised_limits,
    }


# --------------------------------------------------------------------------- #
# built-in apertures
# --------------------------------------------------------------------------- #
# Imported lazily inside the builder so importing the aperture module does not
# pull in the research package unless the built-ins are actually used.
def _research_types() -> tuple[list[str], list[str]]:
    from ozagia.research.graph import EDGE_TYPES, NODE_TYPES
    return sorted(NODE_TYPES), sorted(EDGE_TYPES)


_DEV_NODES = ["Commit", "Doc", "Failure", "File", "Repo", "Symbol", "Test"]
_DEV_EDGES = [
    "changes", "contains", "contains_symbol", "defined_in", "defines",
    "documents", "has_doc", "has_failure", "has_test", "head", "imports",
    "in_file", "mentions_file", "refers_to", "tested_by_file", "tests_module",
]
_DEV_LABELS = {
    "commit": "Commits", "doc": "Docs", "failure": "Failures",
    "file": "Files", "history": "History", "repo": "Repository",
    "symbol": "Symbols", "test": "Tests",
}
_RESEARCH_LABELS = {
    "argument": "Arguments", "assumption": "Assumptions",
    "claim": "Claims", "consensus": "Consensus", "contradiction": "Contradictions",
    "evidence": "Evidence", "finding": "Findings", "frame": "Frames",
    "holding": "Holdings", "observation": "Observations",
    "question": "Questions", "reference": "References",
    "review": "Reviews", "run": "Runs", "source": "Sources",
    "theory": "Theories", "plan": "Plans", "prediction": "Predictions",
    "protocol": "Protocols", "report": "Reports", "decision": "Decisions",
    "appraisal": "Appraisals",
}


def _research_groups() -> dict[str, str]:
    """Group research types into the categories their apertures talk about."""
    return {
        "Argument": "argument",
        "Assumption": "assumption",
        "Certainty": "appraisal",
        "Claim": "claim",
        "Contradiction": "contradiction",
        "Decision": "decision",
        "Evidence": "evidence",
        "Extraction": "evidence",
        "Frame": "frame",
        "Holding": "holding",
        "Observation": "observation",
        "Prediction": "prediction",
        "Protocol": "protocol",
        "Question": "question",
        "Reconciliation": "consensus",
        "Reference": "reference",
        "Report": "report",
        "ReviewFinding": "finding",
        "ReviewPlan": "plan",
        "ReviewRun": "review",
        "RiskOfBias": "appraisal",
        "Run": "run",
        "Source": "source",
        "Theory": "theory",
    }


def _builtin_definitions() -> list[dict[str, Any]]:
    research_nodes, research_edges = _research_types()
    research_groups = _research_groups()
    return [
        {
            "schema": SCHEMA_VERSION,
            "id": "architecture",
            "title": "Architecture",
            "description": "Modules, files, symbols, tests, docs and imports.",
            "resolves": {
                "nodes": _DEV_NODES,
                "edges": _DEV_EDGES,
                "attrs": ["name", "path", "language", "kind", "start_line",
                          "end_line", "size", "root", "head", "generated_at"],
            },
            "grain": "file",
            "collapse": {
                "unresolved": UNRESOLVED,
                "groups": {"Commit": "history", "Failure": "failure"},
                "labels": _DEV_LABELS,
            },
            "render": {"layout": "layered", "seed": 7,
                       "encoding": {"color": "group", "size": "weight",
                                    "label": "label"}},
            "limits": {"max_nodes": 400, "max_edges": 1200,
                       "truncate": "degree"},
        },
        {
            "schema": SCHEMA_VERSION,
            "id": "impact",
            "title": "Impact",
            "description": "Changed files and symbols, their callers, tests "
                           "and known failures.",
            "resolves": {
                "nodes": ["Commit", "Failure", "File", "Repo", "Symbol", "Test"],
                "edges": ["changes", "contains", "contains_symbol",
                          "defined_in", "defines", "has_failure", "has_test",
                          "imports", "in_file", "mentions_file", "refers_to",
                          "tested_by_file", "tests_module"],
                "attrs": ["name", "path", "file", "kind", "start_line",
                          "end_line", "class", "message", "last_green_commit"],
            },
            "grain": "symbol",
            "collapse": {
                "unresolved": UNRESOLVED,
                "groups": {"Commit": "history", "Failure": "failure"},
                "labels": _DEV_LABELS,
            },
            "render": {"layout": "layered", "seed": 11,
                       "encoding": {"color": "group", "size": "weight",
                                    "label": "label"}},
            "limits": {"max_nodes": 300, "max_edges": 900,
                       "truncate": "degree"},
        },
        {
            "schema": SCHEMA_VERSION,
            "id": "evidence",
            "title": "Evidence",
            "description": "Claims, evidence, sources, holdings and the "
                           "relationships that connect them.",
            "resolves": {
                "nodes": research_nodes,
                "edges": research_edges,
                "attrs": ["wording", "title", "name", "status", "claim_type",
                          "scope", "locator", "created_at", "updated_at"],
            },
            "grain": "claim",
            "collapse": {
                "unresolved": UNRESOLVED,
                "groups": research_groups,
                "labels": _RESEARCH_LABELS,
            },
            "render": {"layout": "matrix", "seed": 17,
                       "encoding": {"color": "group", "size": "weight",
                                    "label": "label"}},
            "limits": {"max_nodes": 300, "max_edges": 1200,
                       "truncate": "degree"},
        },
        {
            "schema": SCHEMA_VERSION,
            "id": "consensus",
            "title": "Consensus",
            "description": "Review plans, runs and findings; agreement and "
                           "dissent stay separate.",
            "resolves": {
                "nodes": ["Argument", "Claim", "Evidence", "Observation",
                          "Reconciliation", "Reference", "ReviewFinding",
                          "ReviewPlan", "ReviewRun", "Run", "Source"],
                "edges": ["about", "addresses", "agrees_with", "cites",
                          "derived_from", "disagrees_with", "finds",
                          "implements", "opposes", "reconciles", "reviews",
                          "searches", "supports"],
                "attrs": ["wording", "severity", "issue_type", "model", "role",
                          "method", "created_at", "confidence"],
            },
            "grain": "finding",
            "collapse": {
                "unresolved": UNRESOLVED,
                "groups": research_groups,
                "labels": _RESEARCH_LABELS,
            },
            "render": {"layout": "force", "seed": 23,
                       "encoding": {"color": "group", "size": "weight",
                                    "label": "label"}},
            "limits": {"max_nodes": 250, "max_edges": 1000,
                       "truncate": "degree"},
        },
        {
            "schema": SCHEMA_VERSION,
            "id": "timeline",
            "title": "Timeline",
            "description": "Sessions, commits, edits, tests, reviews and "
                           "fetches in date order.",
            "resolves": {
                "nodes": sorted(set(_DEV_NODES) | set(research_nodes)),
                "edges": sorted(set(_DEV_EDGES) | set(research_edges)),
                "attrs": ["name", "title", "wording", "path", "date", "day",
                          "created_at", "updated_at", "subject", "head_date"],
            },
            "grain": "day",
            "collapse": {
                "unresolved": UNRESOLVED,
                "groups": {**_research_groups(), "Commit": "history",
                           "Failure": "failure"},
                "labels": {**_RESEARCH_LABELS, **_DEV_LABELS},
            },
            "render": {"layout": "timeline", "seed": 29,
                       "encoding": {"color": "group", "size": "weight",
                                    "label": "label"}},
            "limits": {"max_nodes": 500, "max_edges": 1500,
                       "truncate": "degree"},
        },
    ]


def builtin_apertures() -> dict[str, dict[str, Any]]:
    """Every built-in aperture, keyed by id and already validated."""
    return {definition["id"]: validate_aperture(definition)
            for definition in _builtin_definitions()}


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]
                 ) -> dict[str, Any]:
    """A recursive override: tables merge, lists and scalars replace."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def repo_apertures(root: str | Path) -> dict[str, dict[str, Any]]:
    """Apertures under ``<root>/ozagia/apertures/*.toml``.

    A file's ``id`` wins; without one the filename stem is used. A partial
    override of a built-in inherits the fields it does not name; lists replace
    rather than append. A malformed file reports its path and the offending
    field.
    """
    directory = Path(root).resolve() / "ozagia" / "apertures"
    if not directory.is_dir():
        return {}
    builtins = builtin_apertures()
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise ApertureError(f"{path}: {exc}") from exc
        if "id" not in data:
            data["id"] = path.stem
        raw_id = data.get("id")
        base = builtins.get(raw_id, {}) if isinstance(raw_id, str) else {}
        try:
            definition = validate_aperture(_merge_dicts(base, data))
        except ApertureError as exc:
            raise ApertureError(f"{path}: {exc}") from exc
        out[definition["id"]] = definition
    return out


def available_apertures(root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Built-ins plus repository overrides, keyed by id."""
    out = builtin_apertures()
    if root is not None:
        out.update(repo_apertures(root))
    return dict(sorted(out.items()))


def get_aperture(aperture_id: str, *, root: str | Path | None = None
                 ) -> dict[str, Any]:
    """One aperture by id, with repository overrides applied."""
    if not isinstance(aperture_id, str) or not aperture_id.strip():
        raise ApertureError("aperture id must be a non-empty string")
    definitions = available_apertures(root)
    try:
        return copy.deepcopy(definitions[aperture_id])
    except KeyError:
        names = ", ".join(sorted(definitions))
        raise ApertureError(
            f"unknown aperture: {aperture_id!r} (available: {names})") from None


def as_aperture(value: str | dict[str, Any], *,
                root: str | Path | None = None) -> dict[str, Any]:
    """Accept an aperture id or definition and return a validated copy."""
    if isinstance(value, str):
        return get_aperture(value, root=root)
    if isinstance(value, dict):
        return validate_aperture(value)
    raise ApertureError("aperture must be an id or a definition table")


def group_colors(groups: Iterable[str]) -> dict[str, str]:
    """Assign a stable colour to each group, ordered by group id."""
    return {group: PALETTE[index % len(PALETTE)]
            for index, group in enumerate(sorted(set(groups)))}


__all__ = [
    "ApertureError", "LAYOUTS", "PALETTE", "SCHEMA_VERSION",
    "as_aperture", "available_apertures", "builtin_apertures",
    "get_aperture", "group_colors", "repo_apertures", "validate_aperture",
]
