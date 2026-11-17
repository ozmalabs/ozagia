"""
stream.py — Streaming variant of the Ozma protocol shim.

Consistent with IRShim.expand_stream_delta() in ozmalib/context/ir_shim.py.
The caller drives the loop; this module accumulates chunks and emits
partial Turn updates after each complete line.

Usage::

    ss = shim.stream("sess_abc")
    for chunk in raw_api_stream:
        delta = chunk.choices[0].delta.content or ""
        partial_turn = ss.feed_delta(delta)
        # partial_turn has whatever has been parsed so far
    final = ss.end_stream()
    # final is a ShimTurn identical to intercept_response()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .parser import TurnParser
from .types import Turn


@dataclass
class StreamState:
    """Mutable accumulator for a streaming response session."""
    session_id: str
    accumulated: str = ""
    pending_line: str = ""   # partial last line (no trailing newline yet)
    ack_found: bool = False
    partial_turn: Turn = field(default_factory=lambda: Turn(raw="", text=""))


class StreamingShim:
    """
    Drives incremental parsing of a streaming model response.

    Obtained via OzmaShim.stream(session_id). The caller feeds raw content
    deltas and receives a progressively-richer Turn after each call.

    Parsing strategy:
    - Complete lines (terminated by \\n) are parsed immediately.
    - The partial last line is held in StreamState.pending_line and
      appended to accumulated once it terminates.
    - Full re-parse on every complete line is cheap because the parser is
      line-oriented and allocation is minimal.
    """

    def __init__(self, parser: TurnParser, session_id: str) -> None:
        self._parser = parser

    def begin(self, session_id: str) -> StreamState:
        """Create a fresh StreamState for a new streaming response."""
        return StreamState(session_id=session_id)

    def feed_delta(self, state: StreamState, delta: str, *, strict: bool = True) -> Turn:
        """
        Consume a raw content delta and return the current partial Turn.

        Only re-parses when at least one complete line has been received.
        """
        if not delta:
            return state.partial_turn

        state.pending_line += delta

        if "\n" not in state.pending_line:
            return state.partial_turn

        *complete, state.pending_line = state.pending_line.split("\n")
        new_lines = "\n".join(complete) + "\n"
        state.accumulated += new_lines

        state.partial_turn = self._parser.parse(state.accumulated, strict=strict)
        state.partial_turn.raw = state.accumulated
        return state.partial_turn

    def end_stream(self, state: StreamState, *, strict: bool = True) -> Turn:
        """
        Flush the pending partial line and return the final complete Turn.

        Call this once the upstream stream is exhausted.
        """
        if state.pending_line:
            state.accumulated += state.pending_line
            state.pending_line = ""

        turn = self._parser.parse(state.accumulated, strict=strict)
        turn.raw = state.accumulated
        state.partial_turn = turn
        return turn
