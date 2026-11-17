"""High-confidence structural question patterns for tool guidance.

This classifier never queries the graph. It only extracts a user-supplied
symbol or path so the caller can direct OpenCode to the permissioned
``ozagia_structure`` tool.
"""
from __future__ import annotations

import re
from typing import Any

_SYMBOL = r"(?P<target>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
_PATH = r"(?P<target>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+(?:\.py|\.ts|\.js|\.go|\.rs))"
_PATTERNS = (
    ("callers", re.compile(rf"\b(?:who calls|what calls|which functions call|callers of|callers for)\s+{_SYMBOL}\b", re.I)),
    ("callees", re.compile(rf"\b(?:what does|which functions does)\s+{_SYMBOL}\s+call\b", re.I)),
    ("callees", re.compile(rf"\bcallees of\s+{_SYMBOL}\b", re.I)),
    ("tests", re.compile(rf"\b(?:which tests (?:cover|exercise|test)|tests for|test coverage for)\s+{_SYMBOL}\b", re.I)),
    ("resolve", re.compile(rf"\bwhere is\s+(?:the\s+)?(?:definition of\s+)?{_SYMBOL}\s+(?:defined|declared)\b", re.I)),
    ("resolve", re.compile(rf"\b(?:locate|find)\s+(?:the\s+)?(?:definition of|symbol)\s+{_SYMBOL}\b", re.I)),
    ("outline", re.compile(rf"\b(?:public api of|public surface of|module outline of|outline (?:the )?(?:module|file))\s+{_PATH}\b", re.I)),
)


def classify_structural_intent(prompt: Any) -> dict[str, str] | None:
    """Classify explicit structural questions, returning their target.

    Generic requests with structural vocabulary but no concrete target are
    deliberately left unmatched.
    """
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        return None
    text = prompt.strip()
    for kind, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            target = match.group("target").strip("'\"`.,;:!?()[]{}")
            if target:
                return {"kind": kind, "target": target}
    return None


def structural_guidance(intent: dict[str, str]) -> str:
    """Permission-aware guidance with no graph-derived paths or source."""
    import json
    request = json.dumps(intent, ensure_ascii=False, separators=(",", ":"))
    return ("[ozagia] Structural question recognized. Use `ozagia_structure` "
            f"with {request}; it checks freshness and coverage, then requests "
            "read permission for cited files before returning graph results. "
            "Call links are syntactic and may miss dynamic dispatch.")


__all__ = ["classify_structural_intent", "structural_guidance"]
