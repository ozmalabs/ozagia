"""Deterministic scoring primitives for infrastructure context evaluations.

The evaluator consumes frozen questions and caller-produced context records. It
does not parse IaC, retrieve context, or contact models/services. Locations and
edges are credited only when the returned item carries source attribution.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def _location(value: Any) -> str | None:
    """Canonicalize a repository relative path and optional line/JSON pointer."""
    if isinstance(value, str):
        result = value
    elif isinstance(value, Mapping):
        path = value.get("path")
        if not isinstance(path, str):
            return None
        result = path
        line = value.get("line")
        pointer = value.get("pointer")
        if line is not None:
            result += f":{line}"
        elif isinstance(pointer, str) and pointer:
            result += f"#{pointer}"
    else:
        return None
    result = result.strip().replace("\\", "/")
    while result.startswith("./"):
        result = result[2:]
    if not result or result.startswith("/") or ".." in result.split("/"):
        return None
    return result


def _credited_locations(items: Iterable[Any]) -> set[str]:
    """Only cited locations count; bare claims are un-attributed and ignored."""
    found: set[str] = set()
    for item in items:
        if isinstance(item, Mapping) and item.get("attributed") is False:
            continue
        location = _location(item)
        if location is not None:
            found.add(location)
    return found


def _edge(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, Mapping):
        return None
    source, target = value.get("source"), value.get("target")
    kind = value.get("kind", "depends_on")
    if not all(isinstance(v, str) and v.strip() for v in (source, target, kind)):
        return None
    if value.get("attributed") is False or not _location(value.get("evidence")):
        return None
    return source.strip(), target.strip(), kind.strip()


def _counts(expected: set[Any], actual: set[Any]) -> dict[str, float | int]:
    tp = len(expected & actual)
    precision = tp / len(actual) if actual else (1.0 if not expected else 0.0)
    recall = tp / len(expected) if expected else (1.0 if not actual else 0.0)
    return {"true_positive": tp, "predicted": len(actual),
            "expected": len(expected), "precision": precision,
            "recall": recall}


def score_question(question: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """Score one question against explicit expected locations and directed edges.

    ``context`` has ``sources`` and ``edges`` arrays plus optional ``text``.
    Every edge must include an ``evidence`` location to receive credit. Context
    size is reported as exact UTF-8-independent Python character count and a
    clearly labeled rough 4-characters-per-token estimate.
    """
    expected_sources = {_location(v) for v in question.get("expected_sources", [])}
    expected_sources.discard(None)
    predicted_sources = _credited_locations(context.get("sources", []))
    expected_edges = {edge for value in question.get("expected_edges", [])
                      if (edge := _edge(value))}
    predicted_edges = {edge for value in context.get("edges", [])
                       if (edge := _edge(value))}
    text = context.get("text", "")
    if not isinstance(text, str):
        raise ValueError("context.text must be a string")
    return {
        "question_id": str(question.get("id", "")),
        "sources": _counts(expected_sources, predicted_sources),
        "edges": _counts(expected_edges, predicted_edges),
        "context_chars": len(text),
        "estimated_tokens_4chars": (len(text) + 3) // 4,
    }


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Micro-averaged counts plus context-size distribution for a run."""
    summary: dict[str, Any] = {"questions": len(results)}
    for key in ("sources", "edges"):
        total = {"true_positive": 0, "predicted": 0, "expected": 0}
        for row in results:
            for field in total:
                total[field] += int(row[key][field])
        total["precision"] = (total["true_positive"] / total["predicted"]
                              if total["predicted"] else
                              (1.0 if not total["expected"] else 0.0))
        total["recall"] = (total["true_positive"] / total["expected"]
                           if total["expected"] else
                           (1.0 if not total["predicted"] else 0.0))
        summary[key] = total
    chars = [int(row["context_chars"]) for row in results]
    if chars:
        summary["context_chars"] = {
            "total": sum(chars), "min": min(chars), "max": max(chars),
            "mean": sum(chars) / len(chars),
        }
    else:
        summary["context_chars"] = {"total": 0, "min": 0, "max": 0,
                                    "mean": 0.0}
    return summary


def evaluate(questions: Iterable[Mapping[str, Any]],
             contexts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Score a question set; absent context is an empty result, deterministically."""
    results = []
    for question in questions:
        qid = str(question.get("id", ""))
        results.append(score_question(question, contexts.get(qid, {})))
    return {"version": 1, "summary": _summary(results), "questions": results}
