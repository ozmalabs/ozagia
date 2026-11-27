"""Explicit adapter-declared dynamic bridges between repositories.

Adapters discover project-specific integration evidence in their own checkout.
This module only validates that evidence and emits a ``RepoBridge`` snapshot
node through the graph builder. It never reads a file, resolves or imports a
module, executes a project or creates a repository edge. The graph shows the
node; :mod:`ozagia.core.repo_relations` turns it into an ``adapter_bridge``
integration edge only when both repository aliases are selected members.

A bridge is always an explicit assertion. Callers must supply non-empty source
and target repository aliases, a bounded relation, and provenance. Evidence is
optional, bounded, and may be a repository-relative string or a
``{"path": ..., "line": ...}`` object.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

BRIDGE_TYPE = "RepoBridge"
KEY_KIND = "repo_bridge"
FIELDS = frozenset({"source_repo", "target_repo", "relation", "provenance",
                    "evidence", "symbol", "explicit"})
MAX_REPO = 200
MAX_RELATION = 80
MAX_TEXT = 500
MAX_SYMBOL = 300
MAX_EVIDENCE = 10
_RELATION = re.compile(r"[a-z][a-z0-9_]*\Z")


class BridgeValidationError(ValueError):
    """An adapter bridge declaration is not explicit or is out of bounds."""


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeValidationError(f"{field} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise BridgeValidationError(f"{field} exceeds {limit} characters")
    return text


def _repo(value: Any, field: str) -> str:
    text = _text(value, field, MAX_REPO)
    if "::" in text:
        raise BridgeValidationError(f"{field} must not contain '::'")
    return text


def _relation(value: Any) -> str:
    text = _text(value, "relation", MAX_RELATION).lower()
    text = re.sub(r"[\s-]+", "_", text)
    if not _RELATION.fullmatch(text):
        raise BridgeValidationError(
            "relation must start with a lowercase letter and use only "
            "[a-z0-9_] (after normalizing spaces and hyphens)")
    return text


def _evidence_key(item: str | Mapping[str, Any]) -> tuple[Any, ...]:
    if isinstance(item, str):
        return ("text", item)
    return ("location", item["path"], item.get("line", 0))


def _evidence(value: Any) -> list[str | dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray, Mapping)):
        raise BridgeValidationError(
            "evidence must be an iterable of strings or {path, line} objects")
    try:
        items = list(value)
    except TypeError as exc:
        raise BridgeValidationError(
            "evidence must be an iterable of strings or {path, line} objects"
        ) from exc
    if len(items) > MAX_EVIDENCE:
        raise BridgeValidationError(
            f"evidence has more than {MAX_EVIDENCE} entries")
    result: list[str | dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for index, item in enumerate(items):
        field = f"evidence[{index}]"
        if isinstance(item, str):
            normalized: str | dict[str, Any] = _text(item, field, MAX_TEXT)
        elif isinstance(item, Mapping):
            extra = set(item) - {"path", "line"}
            if extra:
                raise BridgeValidationError(
                    f"{field} has unknown fields: {', '.join(sorted(extra))}")
            entry: dict[str, Any] = {
                "path": _text(item.get("path"), f"{field}.path", MAX_TEXT)}
            if "line" in item:
                line = item["line"]
                if isinstance(line, bool) or not isinstance(line, int) or line < 1:
                    raise BridgeValidationError(
                        f"{field}.line must be a positive integer")
                entry["line"] = line
            normalized = entry
        else:
            raise BridgeValidationError(
                f"{field} must be a string or a {{path, line}} object")
        key = _evidence_key(normalized)
        if key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def bridge_attrs(*, source_repo: Any, target_repo: Any, relation: Any,
                 provenance: Any, evidence: Any = (),
                 symbol: Any = None) -> dict[str, Any]:
    """Validate fields and return the canonical ``RepoBridge`` attributes.

    ``explicit`` is set to ``True`` by this helper; there is no way to request
    an inferred bridge. Invalid fields raise :class:`BridgeValidationError`.
    """
    source = _repo(source_repo, "source_repo")
    target = _repo(target_repo, "target_repo")
    if source == target:
        raise BridgeValidationError("source_repo and target_repo must differ")
    attrs: dict[str, Any] = {
        "source_repo": source,
        "target_repo": target,
        "relation": _relation(relation),
        "provenance": _text(provenance, "provenance", MAX_TEXT),
        "evidence": _evidence(evidence),
        "explicit": True,
    }
    if symbol is not None:
        attrs["symbol"] = _text(symbol, "symbol", MAX_SYMBOL)
    return attrs


def normalize_bridge_attrs(attrs: Mapping[str, Any]
                           ) -> tuple[dict[str, Any] | None, str | None]:
    """Re-validate node attributes read back from a member snapshot.

    Returns ``(attributes, None)`` or ``(None, error)``. This is used by
    :mod:`ozagia.core.repo_relations` so a hand-written or stale snapshot node
    follows the same explicit/bounded contract as the adapter helper.
    """
    if not isinstance(attrs, Mapping):
        return None, "attributes must be an object"
    extra = set(attrs) - FIELDS
    if extra:
        return None, "unknown fields: " + ", ".join(sorted(extra))
    if attrs.get("explicit") is not True:
        return None, "explicit must be true"
    try:
        normalized = bridge_attrs(
            source_repo=attrs.get("source_repo"),
            target_repo=attrs.get("target_repo"),
            relation=attrs.get("relation"),
            provenance=attrs.get("provenance"),
            evidence=attrs.get("evidence", ()),
            symbol=attrs.get("symbol"),
        )
    except BridgeValidationError as exc:
        return None, str(exc)
    return normalized, None


def bridge_key(attrs: Mapping[str, Any]) -> tuple[str, ...]:
    """The stable builder key for a normalized bridge.

    A ``::`` inside an optional symbol is percent-encoded so the string form of
    the key remains parseable. Source, target and relation cannot contain the
    key separator.
    """
    symbol = attrs.get("symbol")
    symbol_part = ""
    if symbol is not None:
        symbol_part = str(symbol).replace("%", "%25").replace(":", "%3A")
    return (KEY_KIND, str(attrs["source_repo"]), str(attrs["target_repo"]),
            str(attrs["relation"]), symbol_part)


def add_repo_bridge(builder: Any, *, source_repo: Any, target_repo: Any,
                    relation: Any, provenance: Any, evidence: Iterable[Any] = (),
                    symbol: Any = None) -> int:
    """Validate and add one explicit ``RepoBridge`` node to a build.

    The adapter owns discovery and must cite repository-relative evidence.
    Repeated identical declarations reuse one stable node key.
    """
    attrs = bridge_attrs(source_repo=source_repo, target_repo=target_repo,
                         relation=relation, provenance=provenance,
                         evidence=evidence, symbol=symbol)
    return builder.node(BRIDGE_TYPE, attrs, key=bridge_key(attrs))


__all__ = [
    "BRIDGE_TYPE", "BridgeValidationError", "FIELDS", "KEY_KIND",
    "MAX_EVIDENCE", "MAX_RELATION", "MAX_REPO", "MAX_SYMBOL", "MAX_TEXT",
    "add_repo_bridge", "bridge_attrs", "bridge_key", "normalize_bridge_attrs",
]
