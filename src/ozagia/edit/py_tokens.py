"""Lossless Python token leaves: tokenize tokens plus explicit trivia.

Rendering the `text` of the leaves in order gives the original source, byte for
byte. `lossless.LosslessText.from_python` builds on this. Standard library only.

Ported from Ocelot's `domains/py_cst_ingest.python_cst_leaves`; the leaf
records are the same, so edits made with either produce the same text.
"""
from __future__ import annotations

import io
import tokenize
from typing import Any

_UNNAMED = (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
            tokenize.ENDMARKER)


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    for i, ch in enumerate(source):
        if ch == "\n":
            offsets.append(i + 1)
    return offsets


def _offset(offsets: list[int], lineno: int, col: int, n: int) -> int:
    if lineno - 1 >= len(offsets):
        return n
    return offsets[max(0, lineno - 1)] + max(0, col)


def python_token_leaves(source: str) -> list[dict[str, Any]]:
    """Token and trivia leaves of `source`, in document order.

    Raises ValueError when the source cannot be tokenized.
    """
    offsets = _line_offsets(source)
    leaves: list[dict[str, Any]] = []
    prev_end = 0
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError) as e:
        raise ValueError(f"cannot tokenize Python source: {e}") from e
    for tok in tokens:
        start = _offset(offsets, tok.start[0], tok.start[1], len(source))
        end = _offset(offsets, tok.end[0], tok.end[1], len(source))
        if start > prev_end:
            leaves.append({
                "kind": "trivia", "text": source[prev_end:start],
                "start": prev_end, "end": start,
                "line": tok.start[0], "col": tok.start[1],
                "named": False, "field": "", "child_index": len(leaves),
            })
        text = source[start:end]
        if text:
            leaves.append({
                "kind": tokenize.tok_name[tok.type],
                "text": text, "start": start, "end": end,
                "line": tok.start[0], "col": tok.start[1],
                "named": tok.type not in _UNNAMED,
                "field": "", "child_index": len(leaves),
            })
        prev_end = max(prev_end, end)
    if prev_end < len(source):
        leaves.append({
            "kind": "trivia", "text": source[prev_end:],
            "start": prev_end, "end": len(source),
            "line": source.count("\n", 0, prev_end) + 1,
            "col": 0, "named": False, "field": "",
            "child_index": len(leaves),
        })
    return leaves
