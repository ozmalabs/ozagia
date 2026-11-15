"""Small, inspectable append-only research graph.

This is the phase-one domain layer. It stores researcher-authored structure; it
does not assess truth, call models, inspect sources, or execute experiments.
Sessions are single-writer JSON files under ``.ozagia/research``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from ozagia.config import state_path

SCHEMA_VERSION = 6
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5, 6})
LEGACY_GRAPH_DIR = Path("var") / "ocelot_research"
NODE_TYPES = frozenset(
    {
        "Question",
        "Frame",
        "Claim",
        "Assumption",
        "Source",
        "Holding",
        "Reference",
        "Theory",
        "ReviewPlan",
        "ReviewRun",
        "ReviewFinding",
        "Reconciliation",
        "Argument",
        "SearchRun",
        "Extraction",
        "RiskOfBias",
        "Certainty",
        "Contradiction",
        "Alert",
        "Evidence",
        "Alternative",
        "Prediction",
        "Protocol",
        "Run",
        "Observation",
        "Decision",
        "Report",
        "MathematicalSymbol",
        "MathematicalDefinition",
    }
)
EDGE_TYPES = frozenset(
    {
        "scoped_by",
        "assumes",
        "supports",
        "opposes",
        "derived_from",
        "tests",
        "produced",
        "supersedes",
        "cites",
        "cites_work",
        "manifestation_of",
        "same_as",
        "screens",
        "addresses",
        "reviews",
        "finds",
        "about",
        "reconciles",
        "agrees_with",
        "disagrees_with",
        "implements",
        "searches",
        "argues_for",
        "uses",
        "extracts",
        "assesses",
        "grades",
        "contradicts",
        "alerts",
        "predicts",
        "rival_of",
        "compatible_with",
        "supported_by",
        "challenged_by",
        "discriminated_by",
        "explains",
        "held_as",
        "defines",
        "uses_symbol",
        "constrains",
    }
)
CLAIM_TYPES = frozenset(
    {"definition", "theorem", "empirical_observation", "hypothesis", "inference"}
)
CLAIM_STATUSES = frozenset(
    {"open", "supported_within_scope", "contradicted", "superseded", "unresolved"}
)
# Endpoint rules catch malformed relationships while leaving the graph deliberately
# permissive about researcher-authored evidence and interpretation.
_EVIDENCE_NODES = frozenset({"Claim", "Evidence", "Observation", "Run", "Source", "Alternative"})
_EDGE_ENDPOINTS = {
    "scoped_by": (NODE_TYPES, frozenset({"Frame"})),
    "assumes": (NODE_TYPES, frozenset({"Assumption"})),
    "supports": (_EVIDENCE_NODES, _EVIDENCE_NODES),
    "opposes": (_EVIDENCE_NODES, _EVIDENCE_NODES),
    "derived_from": (NODE_TYPES, NODE_TYPES),
    "tests": (
        frozenset({"Protocol", "Prediction", "Claim", "Alternative"}),
        frozenset({"Run", "Observation", "Evidence"}),
    ),
    "produced": (frozenset({"Protocol", "Run"}), frozenset({"Observation", "Evidence", "Report"})),
    "supersedes": (NODE_TYPES, NODE_TYPES),
    "cites": (
        frozenset(
            {
                "Claim",
                "Evidence",
                "Observation",
                "Report",
                "Source",
                "MathematicalSymbol",
                "MathematicalDefinition",
            }
        ),
        frozenset({"Source", "Evidence", "Run", "Observation", "Reference"}),
    ),
    # A Reference is the bibliographic work; a Source is one artifact of it.
    "manifestation_of": (frozenset({"Source"}), frozenset({"Reference"})),
    # Provider-asserted citation links between works are not support edges.
    "cites_work": (frozenset({"Reference"}), frozenset({"Reference"})),
    # Duplicate, preprint/published, or version-of relationships.
    "same_as": (frozenset({"Reference"}), frozenset({"Reference"})),
    # A screening decision applies to a bibliographic work.
    "screens": (frozenset({"Decision"}), frozenset({"Reference"})),
    # Structured review and multi-model reconciliation.
    "addresses": (
        frozenset({"ReviewPlan"}),
        frozenset({"Claim", "Reference", "Report", "Argument", "Source"}),
    ),
    "reviews": (
        frozenset({"ReviewRun"}),
        frozenset({"Claim", "Reference", "Report", "Argument", "Source"}),
    ),
    "finds": (frozenset({"ReviewRun"}), frozenset({"ReviewFinding"})),
    "about": (
        frozenset({"ReviewFinding"}),
        frozenset({"Claim", "Evidence", "Reference", "Argument", "Source"}),
    ),
    "reconciles": (frozenset({"Reconciliation"}), frozenset({"ReviewRun"})),
    "agrees_with": (frozenset({"ReviewFinding"}), frozenset({"ReviewFinding"})),
    "disagrees_with": (frozenset({"ReviewFinding"}), frozenset({"ReviewFinding"})),
    "implements": (frozenset({"ReviewRun"}), frozenset({"ReviewPlan"})),
    "searches": (frozenset({"SearchRun"}), frozenset({"ReviewPlan"})),
    "argues_for": (frozenset({"Argument"}), frozenset({"Claim"})),
    "uses": (
        frozenset({"Argument"}),
        frozenset({"Claim", "Evidence", "Reference", "ReviewFinding"}),
    ),
    # Structured extraction, appraisal, contradiction and living review.
    "extracts": (
        frozenset({"Extraction"}),
        frozenset({"Reference", "Claim", "Evidence"}),
    ),
    "assesses": (
        frozenset({"RiskOfBias"}),
        frozenset({"Reference", "Claim", "Extraction"}),
    ),
    "grades": (
        frozenset({"Certainty"}),
        frozenset({"Claim", "Reference", "ReviewPlan"}),
    ),
    "contradicts": (
        frozenset({"Contradiction"}),
        frozenset({"Claim", "Evidence", "Observation"}),
    ),
    "alerts": (
        frozenset({"Alert"}),
        frozenset({"Reference", "Claim", "Source", "ReviewPlan"}),
    ),
    # Theory competition: predictions, rivals, compatibility and an evidence
    # basis that can be shared, unique, or insufficient to discriminate.
    "predicts": (frozenset({"Theory"}), frozenset({"Prediction"})),
    "rival_of": (frozenset({"Theory"}), frozenset({"Theory"})),
    "compatible_with": (frozenset({"Theory"}), frozenset({"Theory"})),
    "supported_by": (
        frozenset({"Theory"}),
        frozenset({"Evidence", "Claim", "Observation", "Extraction"}),
    ),
    "challenged_by": (
        frozenset({"Theory"}),
        frozenset({"Evidence", "Claim", "Observation", "Extraction"}),
    ),
    "discriminated_by": (
        frozenset({"Theory"}),
        frozenset({"Prediction", "Evidence", "Observation"}),
    ),
    "explains": (
        frozenset({"Theory"}),
        frozenset({"Claim", "Evidence", "Observation"}),
    ),
    # A Source can have several epistemic holdings (for example, a book held
    # as an authorial interpretation rather than as behavioural evidence).
    "held_as": (frozenset({"Source"}), frozenset({"Holding"})),
    # A small scoped-symbol layer, following the OpenMath/OMDoc distinction
    # between a symbol's identity, its defining assertion, and its context.
    # These remain authored graph assertions; they are not inferred from TeX.
    "defines": (frozenset({"MathematicalDefinition"}), frozenset({"MathematicalSymbol"})),
    "uses_symbol": (NODE_TYPES, frozenset({"MathematicalSymbol"})),
    "constrains": (frozenset({"Assumption"}), frozenset({"MathematicalSymbol"})),
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ResearchError(RuntimeError):
    """Invalid graph operation or malformed persisted graph."""


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError) as exc:
        raise ResearchError(f"value must be JSON-serializable: {exc}") from exc


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


class ResearchGraph:
    """A versioned research investigation with append-only graph events."""

    def __init__(self, data: dict[str, Any], path: Path):
        self._data, self.path = data, path
        self._validate()

    @classmethod
    def create(
        cls,
        question: str,
        *,
        root: str | Path = ".",
        mode: str = "exploratory",
        roots: list[str] | None = None,
        investigation_id: str | None = None,
    ) -> "ResearchGraph":
        question = str(question).strip()
        if not question:
            raise ResearchError("question must be non-empty")
        if mode not in {"deductive", "empirical", "exploratory", "qualitative", "confirmatory"}:
            raise ResearchError(
                "mode must be deductive, empirical, exploratory, qualitative, or confirmatory"
            )
        base = Path(root).resolve()
        graph_id = (
            investigation_id
            or f"research-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{secrets.token_hex(4)}"
        )
        if not _ID_RE.fullmatch(graph_id):
            raise ResearchError("invalid investigation id")
        path = state_path(base, "research", f"{graph_id}.json")
        legacy_path = base / LEGACY_GRAPH_DIR / f"{graph_id}.json"
        if path.exists() or legacy_path.is_file():
            raise ResearchError(f"investigation already exists: {graph_id}")
        data = {
            "schema_version": SCHEMA_VERSION,
            "investigation_id": graph_id,
            "created_at": _utc(),
            "mode": mode,
            "roots": _safe(roots or []),
            "events": [],
            "nodes": [],
            "edges": [],
        }
        graph = cls(data, path)
        graph.record("Question", wording=question)
        graph._save()
        return graph

    @classmethod
    def load(cls, investigation_id: str, *, root: str | Path = ".") -> "ResearchGraph":
        if not _ID_RE.fullmatch(investigation_id):
            raise ResearchError("invalid investigation id")
        base = Path(root).resolve()
        path = state_path(base, "research", f"{investigation_id}.json")
        source_path = path
        if not source_path.exists():
            legacy = base / LEGACY_GRAPH_DIR / f"{investigation_id}.json"
            if legacy.is_file():
                source_path = legacy
        try:
            data = json.loads(source_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ResearchError(f"investigation not found: {investigation_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchError(f"cannot load investigation: {exc}") from exc
        if data.get("investigation_id") != investigation_id:
            raise ResearchError("investigation id does not match its filename")
        return cls(data, path)

    @property
    def investigation_id(self) -> str:
        return self._data["investigation_id"]

    def _validate(self) -> None:
        if (
            not isinstance(self._data, dict)
            or self._data.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS
        ):
            raise ResearchError(
                "unsupported or malformed schema "
                f"(expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
            )
        if (
            not isinstance(self._data.get("events"), list)
            or not isinstance(self._data.get("nodes"), list)
            or not isinstance(self._data.get("edges"), list)
        ):
            raise ResearchError("graph needs events, nodes, and edges arrays")
        ids = [n.get("id") for n in self._data["nodes"]]
        if len(ids) != len(set(ids)):
            raise ResearchError("duplicate node id in persisted graph")
        known = set(ids)
        node_types = {
            n.get("id"): n.get("type") for n in self._data["nodes"] if isinstance(n, dict)
        }
        self._validate_event_log()
        for edge in self._data["edges"]:
            if (
                edge.get("type") not in EDGE_TYPES
                or edge.get("src") not in known
                or edge.get("tgt") not in known
            ):
                raise ResearchError("invalid persisted edge")
            allowed_src, allowed_tgt = _EDGE_ENDPOINTS[edge["type"]]
            if (
                node_types[edge["src"]] not in allowed_src
                or node_types[edge["tgt"]] not in allowed_tgt
            ):
                raise ResearchError("persisted edge has incompatible endpoint types")

    def _validate_event_log(self) -> None:
        """Replay append events and require exact agreement with materialized state."""
        events = self._data["events"]
        replay_nodes: list[dict[str, Any]] = []
        replay_edges: list[dict[str, Any]] = []
        node_type_counts: dict[str, int] = {}
        for ordinal, event in enumerate(events, start=1):
            if not isinstance(event, dict) or event.get("id") != f"event-{ordinal:06d}":
                raise ResearchError("event log has invalid ordering or event id")
            kind = event.get("type")
            if kind == "node_recorded":
                entity, collection, entity_type = event.get("node"), replay_nodes, "node"
                if set(event) != {"id", "type", "at", "node"}:
                    raise ResearchError("event log node event content is inconsistent")
                if not isinstance(entity, dict) or event.get("at") != entity.get("created_at"):
                    raise ResearchError("event log node event content is inconsistent")
                if entity.get("type") not in NODE_TYPES:
                    raise ResearchError("event log contains an unknown node type")
            elif kind == "edge_linked":
                entity, collection, entity_type = event.get("edge"), replay_edges, "edge"
                if set(event) != {"id", "type", "at", "edge"}:
                    raise ResearchError("event log edge event content is inconsistent")
                if not isinstance(entity, dict) or event.get("at") != entity.get("created_at"):
                    raise ResearchError("event log edge event content is inconsistent")
                if entity.get("type") not in EDGE_TYPES:
                    raise ResearchError("event log contains an unknown edge type")
            elif kind == "node_amended":
                if set(event) != {"id", "type", "at", "node_id", "fields"}:
                    raise ResearchError("event log amendment content is inconsistent")
                target = next(
                    (node for node in replay_nodes if node.get("id") == event.get("node_id")),
                    None,
                )
                fields = event.get("fields")
                if (
                    target is None
                    or not isinstance(fields, dict)
                    or {"id", "type", "created_at"}.intersection(fields)
                    or fields.get("updated_at", event.get("at")) != event.get("at")
                ):
                    raise ResearchError("event log amendment content is inconsistent")
                target.update(fields)
                continue
            else:
                raise ResearchError("event log contains an unknown event type")
            if entity_type == "node":
                node_type_counts[entity["type"]] = node_type_counts.get(entity["type"], 0) + 1
                expected_id = f"{entity['type'].lower()}-{node_type_counts[entity['type']]:04d}"
            else:
                expected_id = f"edge-{len(collection) + 1:06d}"
            if entity.get("id") != expected_id:
                raise ResearchError(f"event log has invalid {entity_type} ordering or id")
            collection.append(entity)
        if replay_nodes != self._data["nodes"] or replay_edges != self._data["edges"]:
            raise ResearchError("event log replay does not match persisted nodes and edges")

    def _save(self) -> None:
        self._data["schema_version"] = SCHEMA_VERSION
        _atomic_write(self.path, self._data)

    def record(self, node_type: str, **fields: Any) -> dict[str, Any]:
        if node_type not in NODE_TYPES:
            raise ResearchError(f"unknown node type: {node_type}")
        reserved = {"id", "type", "created_at"}.intersection(fields)
        if reserved:
            raise ResearchError(f"reserved node fields cannot be supplied: {sorted(reserved)}")
        fields = _safe(fields)
        if node_type == "Claim":
            if fields.get("claim_type") is not None and fields["claim_type"] not in CLAIM_TYPES:
                raise ResearchError(f"claim_type must be one of {sorted(CLAIM_TYPES)}")
            if fields.get("status") is not None and fields["status"] not in CLAIM_STATUSES:
                raise ResearchError(f"status must be one of {sorted(CLAIM_STATUSES)}")
        ordinal = 1 + sum(n["type"] == node_type for n in self._data["nodes"])
        node = {
            "id": f"{node_type.lower()}-{ordinal:04d}",
            "type": node_type,
            "created_at": _utc(),
            **fields,
        }
        event = {
            "id": f"event-{len(self._data['events']) + 1:06d}",
            "type": "node_recorded",
            "at": node["created_at"],
            "node": node,
        }
        self._data["nodes"].append(node)
        self._data["events"].append(event)
        try:
            self._save()
        except BaseException:
            self._data["nodes"].pop()
            self._data["events"].pop()
            raise
        return _safe(node)

    def amend(self, node_id: str, **fields: Any) -> dict[str, Any]:
        """Append an amendment event and materialize the merged node.

        Amendments preserve the original node event. They are for enrichment
        (identifiers, retraction status, metadata provenance), not for changing
        a node's type or inventing history.
        """
        index = next(
            (i for i, node in enumerate(self._data["nodes"]) if node.get("id") == node_id),
            None,
        )
        if index is None:
            raise ResearchError(f"unknown node id: {node_id}")
        reserved = {"id", "type", "created_at"}.intersection(fields)
        if reserved:
            raise ResearchError(f"reserved node fields cannot be amended: {sorted(reserved)}")
        fields = _safe(fields)
        at = _utc()
        fields["updated_at"] = at
        previous = self._data["nodes"][index]
        updated = {**previous, **fields}
        event = {
            "id": f"event-{len(self._data['events']) + 1:06d}",
            "type": "node_amended",
            "at": at,
            "node_id": node_id,
            "fields": fields,
        }
        self._data["nodes"][index] = updated
        self._data["events"].append(event)
        try:
            self._save()
        except BaseException:
            self._data["nodes"][index] = previous
            self._data["events"].pop()
            raise
        return _safe(updated)

    def link(
        self,
        src: str,
        relation: str,
        tgt: str,
        *,
        provenance: Any = None,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        if relation not in EDGE_TYPES:
            raise ResearchError(f"unknown edge type: {relation}")
        by_id = {n["id"]: n for n in self._data["nodes"]}
        if src not in by_id or tgt not in by_id:
            raise ResearchError("edge endpoints must refer to existing nodes")
        allowed_src, allowed_tgt = _EDGE_ENDPOINTS[relation]
        if by_id[src]["type"] not in allowed_src or by_id[tgt]["type"] not in allowed_tgt:
            raise ResearchError(
                f"{relation} edge is incompatible with {by_id[src]['type']} -> {by_id[tgt]['type']}"
            )
        edge = {
            "id": f"edge-{len(self._data['edges']) + 1:06d}",
            "type": relation,
            "src": src,
            "tgt": tgt,
            "created_at": _utc(),
        }
        if provenance is not None:
            edge["provenance"] = _safe(provenance)
        if rationale is not None:
            edge["rationale"] = str(rationale)
        self._data["edges"].append(edge)
        self._data["events"].append(
            {
                "id": f"event-{len(self._data['events']) + 1:06d}",
                "type": "edge_linked",
                "at": edge["created_at"],
                "edge": edge,
            }
        )
        try:
            self._save()
        except BaseException:
            self._data["edges"].pop()
            self._data["events"].pop()
            raise
        return _safe(edge)

    def state(self) -> dict[str, Any]:
        return _safe(self._data)

    def inspect(self, node_id: str, *, transitive: bool = False) -> dict[str, Any]:
        nodes = {n["id"]: n for n in self._data["nodes"]}
        if node_id not in nodes:
            raise ResearchError(f"unknown node id: {node_id}")
        reached = {node_id}
        edges: list[dict[str, Any]] = []
        pending = [node_id]
        while pending:
            current = pending.pop()
            incident = [
                e for e in self._data["edges"] if e["src"] == current or e["tgt"] == current
            ]
            for edge in incident:
                if edge not in edges:
                    edges.append(edge)
                other = edge["src"] if edge["tgt"] == current else edge["tgt"]
                if other not in reached:
                    reached.add(other)
                    if transitive:
                        pending.append(other)
            if not transitive:
                break
        related = sorted(reached - {node_id})
        result = {
            "node": _safe(nodes[node_id]),
            "edges": _safe(edges),
            "related_nodes": [_safe(nodes[i]) for i in related],
        }
        if transitive:
            result["transitive"] = True
        return result

    def check(self) -> dict[str, Any]:
        issues: list[dict[str, str]] = []
        nodes = self._data["nodes"]
        if not any(n["type"] == "Question" for n in nodes):
            issues.append({"code": "missing_question", "reason": "Investigation has no question."})
        if not any(n["type"] == "Frame" for n in nodes):
            issues.append(
                {
                    "code": "missing_frame",
                    "reason": "Frame is not recorded; do not assume a GE application.",
                }
            )
        for node in nodes:
            if node["type"] == "Source" and not node.get("locator"):
                issues.append(
                    {
                        "code": "source_locator_missing",
                        "node_id": node["id"],
                        "reason": "Source has no locator.",
                    }
                )
            if node["type"] == "Reference":
                if not node.get("title") and not node.get("raw"):
                    issues.append(
                        {
                            "code": "reference_title_missing",
                            "node_id": node["id"],
                            "reason": "Reference has no title or raw citation text.",
                        }
                    )
                if not node.get("identifiers") and not node.get("citation_key") and not node.get("raw"):
                    issues.append(
                        {
                            "code": "reference_identity_missing",
                            "node_id": node["id"],
                            "reason": "Reference has no identifier, citation key or raw text.",
                        }
                    )
                retraction = node.get("retraction") or {}
                if retraction.get("status") in {"retracted", "concern"}:
                    issues.append(
                        {
                            "code": "reference_retracted",
                            "node_id": node["id"],
                            "reason": f"Reference status is {retraction.get('status')}; review before relying on it.",
                        }
                    )
            if node["type"] == "Holding":
                for field in ("source_id", "source_type", "epistemic_role", "authority"):
                    if not node.get(field):
                        issues.append({
                            "code": "holding_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"Holding has no {field}.",
                        })
            if node["type"] == "Theory":
                for field in ("name", "statement", "scope", "status"):
                    if not node.get(field):
                        issues.append({
                            "code": "theory_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"Theory has no {field}.",
                        })
            if node["type"] == "ReviewPlan" and not node.get("question"):
                issues.append({
                    "code": "review_plan_question_missing",
                    "node_id": node["id"],
                    "reason": "ReviewPlan has no question.",
                })
            if node["type"] == "ReviewRun":
                for field in ("plan_id", "model", "role", "packet_sha256"):
                    if not node.get(field):
                        issues.append({
                            "code": "review_run_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"ReviewRun has no {field}.",
                        })
            if node["type"] == "ReviewFinding":
                for field in ("severity", "issue_type", "wording"):
                    if not node.get(field):
                        issues.append({
                            "code": "review_finding_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"ReviewFinding has no {field}.",
                        })
            if node["type"] == "Reconciliation" and not node.get("method"):
                issues.append({
                    "code": "reconciliation_method_missing",
                    "node_id": node["id"],
                    "reason": "Reconciliation has no method.",
                })
            if node["type"] == "Argument":
                for field in ("mode", "thesis", "reviewer"):
                    if not node.get(field):
                        issues.append({
                            "code": "argument_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"Argument has no {field}.",
                        })
            if node["type"] == "SearchRun":
                for field in ("query", "provider"):
                    if not node.get(field):
                        issues.append({
                            "code": "search_run_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"SearchRun has no {field}.",
                        })
            if node["type"] == "Extraction":
                for field in ("target_id", "extractor"):
                    if not node.get(field):
                        issues.append({
                            "code": "extraction_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"Extraction has no {field}.",
                        })
            if node["type"] == "RiskOfBias":
                for field in ("target_id", "tool", "overall", "reviewer"):
                    if not node.get(field):
                        issues.append({
                            "code": "risk_of_bias_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"RiskOfBias has no {field}.",
                        })
            if node["type"] == "Certainty":
                for field in ("target_id", "rating", "reviewer"):
                    if not node.get(field):
                        issues.append({
                            "code": "certainty_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"Certainty has no {field}.",
                        })
            if node["type"] == "Contradiction":
                for field in ("claim_a_id", "claim_b_id", "kind", "status"):
                    if not node.get(field):
                        issues.append({
                            "code": "contradiction_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"Contradiction has no {field}.",
                        })
            if node["type"] == "Alert":
                for field in ("kind", "target_id"):
                    if not node.get(field):
                        issues.append({
                            "code": "alert_field_missing",
                            "node_id": node["id"],
                            "field": field,
                            "reason": f"Alert has no {field}.",
                        })
            if node["type"] == "Claim":
                if not any(
                    e["type"] in {"supports", "opposes", "cites", "derived_from"}
                    and node["id"] in (e["src"], e["tgt"])
                    for e in self._data["edges"]
                ):
                    issues.append(
                        {
                            "code": "claim_evidence_missing",
                            "node_id": node["id"],
                            "reason": "Claim has no recorded evidence or citation relationship.",
                        }
                    )
                for key in ("wording", "scope", "claim_type", "author", "status"):
                    if not node.get(key):
                        issues.append(
                            {
                                "code": "claim_field_missing",
                                "node_id": node["id"],
                                "field": key,
                                "reason": f"Claim {node['id']} has no {key}.",
                            }
                        )
                if node.get("status") not in CLAIM_STATUSES:
                    issues.append(
                        {
                            "code": "claim_status_unassessed",
                            "node_id": node["id"],
                            "reason": "Claim has no recognized signed-off status.",
                        }
                    )
        return {
            "ok": not issues,
            "issues": issues,
            "truth_assessed": False,
            "node_count": len(nodes),
            "edge_count": len(self._data["edges"]),
        }

    def export(self) -> dict[str, Any]:
        """Return a Markdown ledger and full machine-readable graph bundle."""
        lines = [
            f"# Research investigation {self.investigation_id}",
            "",
            f"Mode: **{self._data['mode']}**",
            "",
            "## Nodes",
            "",
        ]
        for n in self._data["nodes"]:
            title = (n.get("wording") or n.get("title") or n.get("name")
                     or n.get("question") or n.get("summary") or n["type"])
            lines.append(f"- **{n['id']} ({n['type']})** — {title}")
            if n["type"] == "Claim":
                lines.append(
                    f"  - Scope: {n.get('scope', 'unknown')}; status: {n.get('status', 'unresolved')}; limitations: {n.get('limitations', 'unknown')}."
                )
        lines.extend(["", "## Relationships", ""])
        for e in self._data["edges"]:
            lines.append(f"- `{e['src']}` —**{e['type']}**→ `{e['tgt']}`")
        lines.extend(["", "## Mechanical check", "", "Truth is not assessed by this graph.", ""])
        for issue in self.check()["issues"]:
            lines.append(f"- {issue['reason']}")
        return {"markdown": "\n".join(lines), "bundle": self.state()}


def load_graph(investigation_id: str, *, root: str | Path = ".") -> ResearchGraph:
    return ResearchGraph.load(investigation_id, root=root)
