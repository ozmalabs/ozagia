"""User positions: a user's stated stance, held as a conversation-sourced Holding.

In research and writing sessions the user often corrects the agent's reading
("that is not the lesson of chapter 3", "the paper argues X, not Y"). A
position records that correction so it is not lost when the session ends:

- a ``Source`` of type ``conversation`` per conversation session id;
- a ``Holding`` with ``epistemic_role`` ``user_position``, the stance, the
  topic, the date it was stated and, optionally, a quoted span the user
  approved;
- ``concerns`` edges to the claims, sources or other records it is about.

Only what the tool call passes is stored. Nothing reads or scrapes a
transcript. A position is the user's stance, not evidence: research context
reports it in its own ``user_positions`` section with its date.

A position is ``active`` until it is superseded by a later position (linked
with a ``supersedes`` edge) or retracted. Both are amendment events, so the
history stays in the graph's event log.
"""
from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Callable

from ozagia.research.graph import ResearchError

__all__ = [
    "POSITION_STATUSES",
    "PositionError",
    "list_positions",
    "record_position",
    "relevant_positions",
    "retract_position",
]

POSITION_STATUSES = ("active", "superseded", "retracted")
MAX_QUOTE_CHARS = 4000
MAX_STANCE_CHARS = 8000
# Records a position may concern. Matches the graph's ``concerns`` endpoints.
CONCERN_TYPES = frozenset({
    "Question", "Frame", "Claim", "Assumption", "Source", "Reference",
    "Theory", "Argument", "Evidence", "Prediction", "ReviewFinding",
    "LedgerFinding", "Holding",
})
DEFAULT_THRESHOLD = 0.2
USER_POSITION_NOTE = (
    "User positions are stances the user stated in conversation, with the "
    "date they were stated. They are not sourced evidence and do not support "
    "or contradict any claim; check a later conversation has not changed them."
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
# Function words carry no topic; dropping them keeps a short query from
# matching every position through "the" and "of".
_STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has "
    "have he her his how i if in into is it its me my no not of on or our she "
    "so than that the their them then there these they this those to us was "
    "we were what when where which who why will with would you your".split()
)


class PositionError(ResearchError):
    """A user position record, supersession or retraction is invalid."""


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _nodes(state: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        node for node in state.get("nodes", [])
        if isinstance(node, Mapping) and isinstance(node.get("id"), str)
    ]


def _node(state: Mapping[str, Any], node_id: str) -> Mapping[str, Any] | None:
    return next((node for node in _nodes(state) if node["id"] == node_id), None)


def _text(value: Any, field: str, *, limit: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PositionError(f"{field} must be a non-empty string")
    text = value.strip()
    if limit is not None and len(text) > limit:
        raise PositionError(f"{field} must be at most {limit} characters")
    return text


def _optional_text(value: Any, field: str, *, limit: int | None = None) -> str | None:
    if value is None:
        return None
    return _text(value, field, limit=limit)


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PositionError(f"{field} must be a list of strings")
    out: list[str] = []
    for index, item in enumerate(value):
        text = _text(item, f"{field}[{index}]")
        if text not in out:
            out.append(text)
    return out


def _timestamp(value: Any) -> str:
    if value is None:
        return _utc()
    text = _text(value, "stated_at")
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PositionError("stated_at must be an ISO 8601 timestamp") from exc
    return text


def is_position(node: Mapping[str, Any]) -> bool:
    return node.get("type") == "Holding" and node.get("position") is True


def _require_position(state: Mapping[str, Any], position_id: Any) -> Mapping[str, Any]:
    node_id = _text(position_id, "position_id")
    node = _node(state, node_id)
    if node is None or not is_position(node):
        raise PositionError(f"unknown user position: {node_id}")
    return node


def _conversation_source(graph: Any, session: str, stated_at: str) -> Mapping[str, Any]:
    """The Source for one conversation session, recorded on first use."""
    for node in _nodes(graph.state()):
        locator = node.get("locator")
        if (node.get("type") == "Source" and node.get("source_type") == "conversation"
                and isinstance(locator, Mapping)
                and locator.get("conversation_session_id") == session):
            return node
    return graph.record(
        "Source",
        source_type="conversation",
        title=f"Conversation {session}",
        locator={"kind": "conversation", "conversation_session_id": session},
        metadata={"title": f"Conversation {session}", "session_id": session,
                  "date": stated_at[:10]},
        truth_assessed=False,
    )


def record_position(
    graph: Any,
    *,
    stance: str,
    conversation_session_id: str,
    topic: str | None = None,
    quote: str | None = None,
    stated_at: str | None = None,
    concerns: Sequence[str] | None = None,
    documents: Sequence[str] | None = None,
    correction_of: str | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Record a user's stated stance as a conversation-sourced Holding.

    ``concerns`` names existing records (claims, sources, references ...);
    ``documents`` names document paths the position is about. With
    ``supersedes``, the earlier position is marked superseded and the new one
    inherits its topic, concerns and documents when they are omitted.
    """
    state = graph.state()
    stance = _text(stance, "stance", limit=MAX_STANCE_CHARS)
    session = _text(conversation_session_id, "conversation_session_id", limit=256)
    topic = _optional_text(topic, "topic", limit=500)
    quote = _optional_text(quote, "quote", limit=MAX_QUOTE_CHARS)
    correction_of = _optional_text(correction_of, "correction_of", limit=MAX_STANCE_CHARS)
    when = _timestamp(stated_at)
    concern_ids = _string_list(concerns, "concerns")
    document_paths = _string_list(documents, "documents")

    previous = None
    if supersedes is not None:
        previous = _require_position(state, supersedes)
        if previous.get("position_status") != "active":
            raise PositionError(
                f"{previous['id']} is {previous.get('position_status')}; only an "
                "active position can be superseded")
        if topic is None:
            topic = previous.get("topic")
        if not concern_ids:
            concern_ids = list(previous.get("concerns") or [])
        if not document_paths:
            document_paths = list(previous.get("documents") or [])
    if topic is None and not concern_ids and not document_paths:
        raise PositionError(
            "a position needs a topic, concerns or documents so it can be found again")
    targets = []
    for node_id in concern_ids:
        node = _node(state, node_id)
        if node is None:
            raise PositionError(f"concerns names an unknown record: {node_id}")
        if node.get("type") not in CONCERN_TYPES:
            raise PositionError(f"a position cannot concern a {node.get('type')}")
        targets.append(node)

    source = _conversation_source(graph, session, when)
    rationale = "Stated by the user in conversation " + session + "."
    fields: dict[str, Any] = {
        "source_id": source["id"],
        "source_type": "conversation",
        "epistemic_role": "user_position",
        "authority": "personal",
        "review_status": "none",
        "use_constraints": ["user_stance", "not_external_evidence"],
        "status": "accepted",
        "reviewer": "user",
        "rationale": rationale,
        "truth_assessed": False,
        "position": True,
        "position_status": "active",
        "stance": stance,
        "conversation_session_id": session,
        "stated_at": when,
        "concerns": concern_ids,
        "documents": document_paths,
    }
    if topic is not None:
        fields["topic"] = topic
        fields["scope"] = topic
    if quote is not None:
        fields["quote"] = quote
    if correction_of is not None:
        fields["correction_of"] = correction_of
    if previous is not None:
        fields["supersedes"] = previous["id"]
    holding = graph.record("Holding", **fields)
    graph.link(
        source["id"], "held_as", holding["id"],
        provenance={"kind": "user_position", "conversation_session_id": session},
        rationale=rationale,
    )
    for target in targets:
        graph.link(
            holding["id"], "concerns", target["id"],
            provenance={"kind": "user_position", "conversation_session_id": session},
            rationale="The user's position is about this record; it is not support.",
        )
    superseded = None
    if previous is not None:
        graph.link(
            holding["id"], "supersedes", previous["id"],
            provenance={"kind": "user_position_revision",
                        "conversation_session_id": session},
            rationale="The user stated a later position on the same topic.",
        )
        superseded = graph.amend(
            previous["id"], position_status="superseded",
            superseded_by=holding["id"], superseded_at=when,
            superseded_in=session)
    return {
        "position": _view(holding),
        "source": dict(source),
        "superseded": _view(superseded) if superseded else None,
        "truth_assessed": False,
    }


def retract_position(
    graph: Any,
    *,
    position_id: str,
    conversation_session_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Mark an active position retracted; the record and its history remain."""
    state = graph.state()
    position = _require_position(state, position_id)
    session = _text(conversation_session_id, "conversation_session_id", limit=256)
    note = _optional_text(note, "note", limit=MAX_STANCE_CHARS)
    if position.get("position_status") != "active":
        raise PositionError(
            f"{position['id']} is already {position.get('position_status')}")
    fields: dict[str, Any] = {
        "position_status": "retracted",
        "retracted_at": _utc(),
        "retracted_in": session,
        # The Holding itself is withdrawn; the rationale keeps the reason.
        "status": "rejected",
        "rationale": f"{position.get('rationale', '')} Retracted by the user in "
                     f"conversation {session}." + (f" {note}" if note else ""),
    }
    if note is not None:
        fields["retraction_note"] = note
    updated = graph.amend(position["id"], **fields)
    return {"position": _view(updated), "truth_assessed": False}


def _view(node: Mapping[str, Any] | None) -> dict[str, Any]:
    if node is None:
        return {}
    keys = ("id", "stance", "topic", "quote", "correction_of", "stated_at",
            "conversation_session_id", "concerns", "documents",
            "position_status", "supersedes", "superseded_by", "superseded_at",
            "superseded_in", "retracted_at", "retracted_in", "retraction_note",
            "source_id")
    out = {key: node[key] for key in keys if node.get(key) not in (None, [], "")}
    out["label"] = "user position"
    out["date"] = str(node.get("stated_at", ""))[:10]
    return out


def list_positions(
    state: Mapping[str, Any],
    *,
    status: str | None = "active",
    node_id: str | None = None,
    document: str | None = None,
) -> dict[str, Any]:
    """Positions in recording order, filtered by status, record or document."""
    if status not in (None, "all", *POSITION_STATUSES):
        raise PositionError(f"status must be one of {[*POSITION_STATUSES, 'all']}")
    positions = [node for node in _nodes(state) if is_position(node)]
    counts = {name: 0 for name in POSITION_STATUSES}
    for node in positions:
        counts[str(node.get("position_status"))] = counts.get(
            str(node.get("position_status")), 0) + 1
    selected = [
        node for node in positions
        if (status in (None, "all") or node.get("position_status") == status)
        and (node_id is None or node_id in (node.get("concerns") or []))
        and (document is None or document in (node.get("documents") or []))
    ]
    return {
        "positions": [_view(node) for node in selected],
        "count": len(selected),
        "status_counts": counts,
        "note": USER_POSITION_NOTE,
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# surfacing in research context
# --------------------------------------------------------------------------- #
def _content_tokens(text: str) -> str:
    return " ".join(
        token for token in _TOKEN_RE.findall(text.casefold())
        if len(token) > 2 and token not in _STOPWORDS)


def _record_text(node: Mapping[str, Any]) -> str:
    parts = [node.get(key) for key in ("wording", "title", "name", "statement",
                                       "text", "question")]
    return " ".join(str(part) for part in parts if isinstance(part, str))


def _record_paths(node: Mapping[str, Any]) -> set[str]:
    paths = set()
    for key in ("locator", "source_locator"):
        locator = node.get(key)
        if isinstance(locator, Mapping) and isinstance(locator.get("path"), str):
            paths.add(locator["path"])
    return paths


def relevant_positions(
    state: Mapping[str, Any],
    query: str,
    *,
    retrieved_paths: Sequence[str] = (),
    node_ids: Sequence[str] = (),
    threshold: float = DEFAULT_THRESHOLD,
    limit: int = 8,
    embedder: Callable[[list[str]], Any] | None = None,
) -> list[dict[str, Any]]:
    """Active positions that touch a research query or its retrieved context.

    A position matches when it concerns a record whose source path was
    retrieved (or a listed node), names a retrieved document, or when its
    topic, stance and concerned records are similar to the query under the
    research package's ``hash-bow`` embedder (or an injected one).
    """
    from ozagia.research.semantic import _as_vectors, _cosine, hash_bow_embedder

    positions = [
        node for node in _nodes(state)
        if is_position(node) and node.get("position_status") == "active"
    ]
    if not positions:
        return []
    paths = set(retrieved_paths)
    wanted = set(node_ids)
    texts = []
    for node in positions:
        concerned = [_node(state, cid) for cid in node.get("concerns") or []]
        texts.append(_content_tokens(" ".join(filter(None, [
            node.get("topic"), node.get("stance"), node.get("correction_of"),
            *(_record_text(item) for item in concerned if item is not None),
        ]))))
    embed = embedder or hash_bow_embedder(1024)
    query_text = _content_tokens(query)
    vectors, _dim = _as_vectors(embed([query_text, *texts]), count=len(texts) + 1)
    out = []
    for node, vector in zip(positions, vectors[1:]):
        reasons = []
        for cid in node.get("concerns") or []:
            concerned = _node(state, cid)
            if cid in wanted:
                reasons.append({"kind": "concerns_listed_record", "node_id": cid})
            elif concerned is not None and _record_paths(concerned) & paths:
                reasons.append({"kind": "concerns_retrieved_source", "node_id": cid})
        for path in node.get("documents") or []:
            if path in paths:
                reasons.append({"kind": "names_retrieved_document", "path": path})
        score = _cosine(vectors[0], vector) if query_text else 0.0
        if score >= threshold:
            reasons.append({"kind": "topic_similarity", "score": round(score, 4)})
        if reasons:
            out.append({**_view(node), "match": {"reasons": reasons,
                                                 "score": round(score, 4)}})
    out.sort(key=lambda item: (-len(item["match"]["reasons"]),
                               -item["match"]["score"], item["id"]))
    return out[:limit]


def context_section(state: Mapping[str, Any], query: str, *,
                    retrieved_paths: Sequence[str] = (),
                    node_ids: Sequence[str] = ()) -> dict[str, Any]:
    """The ``user_positions`` block of research context, apart from evidence."""
    items = relevant_positions(state, query, retrieved_paths=retrieved_paths,
                               node_ids=node_ids)
    return {
        "label": "user position",
        "separate_from_evidence": True,
        "items": items,
        "count": len(items),
        "note": USER_POSITION_NOTE,
    }
