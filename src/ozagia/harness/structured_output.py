"""Normalize model output when the caller explicitly expects JSON.

Do not use this on ordinary chat or Markdown: a code fence can be intentional
content there. A single unlabelled or JSON-labelled fence around a valid JSON
value can be recovered, with any surrounding prose retained separately.
Multiple blocks, other languages and malformed JSON remain unchanged.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

_FENCED_JSON = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})[ \t]*(?:json)?[ \t]*\r?\n"
    r"(?P<body>.*?)\r?\n(?P=fence)[ \t]*(?=\r?$)",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)


@dataclass(frozen=True)
class RecoveredJSON:
    """A recovered JSON body and the exact surrounding model text."""

    body: str
    prefix: str
    suffix: str


def _reject_constant(value: str) -> None:
    """Reject Python's non-standard NaN and Infinity JSON extensions."""
    raise ValueError(f"non-standard JSON constant {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate object names instead of silently taking the last."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    """Reject syntactically valid numbers that overflow Python to infinity."""
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number is outside the supported finite range")
    return parsed


_MAX_JSON_DEPTH = 512


def _check_nesting(text: str) -> None:
    """Refuse pathological nesting before the decoder can hit its own limit.

    Python 3.14's C scanner accepts some deeply nested inputs that older
    interpreters reject with ``RecursionError``; the contract here is a normal
    ``ValueError`` on every supported interpreter.
    """
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise ValueError(
                    f"JSON nesting exceeds {_MAX_JSON_DEPTH} levels")
        elif char in "]}":
            depth = max(0, depth - 1)


def _loads_strict_json(text: str) -> Any:
    """Decode RFC JSON without non-finite constants or duplicate names."""
    _check_nesting(text)
    try:
        return json.loads(text, parse_constant=_reject_constant,
                          parse_float=_finite_float,
                          object_pairs_hook=_unique_object)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the decoder limit") from exc


def recover_fenced_json(text: str) -> RecoveredJSON | None:
    """Recover one valid JSON block; reject extra or ambiguous fenced blocks."""
    matches = list(_FENCED_JSON.finditer(text))
    if len(matches) != 1:
        return None
    match = matches[0]
    prefix, suffix = text[:match.start()], text[match.end():]
    # Other blocks, including non-JSON blocks, make the intended payload
    # ambiguous. Fences inside a JSON string remain part of the JSON body.
    if any(marker in part for part in (prefix, suffix) for marker in ("```", "~~~")):
        return None
    body = match.group("body")
    try:
        _loads_strict_json(body)
    except (ValueError, RecursionError):
        return None
    return RecoveredJSON(body=body, prefix=prefix, suffix=suffix)


def normalize_json_response(text: str) -> str:
    """Recover valid fenced JSON; use ``recover_fenced_json`` to retain prose."""
    recovered = recover_fenced_json(text)
    return recovered.body if recovered else text


def loads_model_json(text: str) -> Any:
    """Parse an explicitly JSON-valued model response or tool argument."""
    return _loads_strict_json(normalize_json_response(text))
