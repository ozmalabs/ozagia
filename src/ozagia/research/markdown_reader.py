"""Portable source-reading constructions adapted from Ocelot."""

from __future__ import annotations
import re

_FORMAL_PATTERNS = [
    (r"\*\*Axiom\s+(\d+|\w+)\s*\(([^)]+)\)\.\*\*", "PaperAxiom"),
    (r"\*\*Theorem\s+(\w+)\s*\(([^)]+)\)\.\*\*", "PaperTheorem"),
    (r"\*\*Conjecture\s+(\w+)\s*\(([^)]+)\)\.\*\*", "PaperConjecture"),
    (r"\*\*Hypothesis\s+([\w-]+)\s*\(?([^)]*)\)?\.\*\*", "PaperHypothesis"),
    (r"\*\*Hypothesis\.\*\*", "PaperHypothesis"),
]

_HEADER_FORMAL_PATTERNS = [
    (r"Axiom\s+(\d+|\w+)\s*\(([^)]+)\)", "PaperAxiom"),
    (r"Theorem\s+(\w+)\s*\(([^)]+)\)", "PaperTheorem"),
    (r"Conjecture\s+(\w+)\s*\(([^)]+)\)", "PaperConjecture"),
    (r"Hypothesis\s+([\w-]+)\s*\(?([^)]*)\)?", "PaperHypothesis"),
]


def _extract_formal_objects(content: str) -> list[dict]:
    """Find Axiom / Theorem / Conjecture / Hypothesis in a section."""
    out = []
    paragraphs = re.split(r"\n\s*\n", content)
    for para in paragraphs:
        ps = para.strip()
        if not ps:
            continue
        for pattern, node_type in _FORMAL_PATTERNS:
            m = re.search(pattern, ps)
            if m:
                label = m.group(1) if m.lastindex else ""
                title = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
                out.append(
                    {
                        "node_type": node_type,
                        "label": str(label),
                        "title": str(title),
                        "text": ps[:2500],
                    }
                )
                break
    return out
