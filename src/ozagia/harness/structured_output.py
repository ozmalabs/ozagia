"""Normalize model output when the caller explicitly expects JSON.

Do not use this on ordinary chat or Markdown: a code fence can be intentional
content there. A single unlabelled or JSON-labelled fence around a valid JSON
value can be recovered, with any surrounding prose retained separately.
Multiple blocks, other languages and malformed JSON remain unchanged.
"""
from __future__ import annotations

import json
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
        json.loads(body)
    except (ValueError, RecursionError):
        return None
    return RecoveredJSON(body=body, prefix=prefix, suffix=suffix)


def normalize_json_response(text: str) -> str:
    """Recover valid fenced JSON; use ``recover_fenced_json`` to retain prose."""
    recovered = recover_fenced_json(text)
    return recovered.body if recovered else text


def loads_model_json(text: str) -> Any:
    """Parse an explicitly JSON-valued model response or tool argument."""
    return json.loads(normalize_json_response(text))
