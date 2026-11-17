"""
handshake.py — Probe injection and ack detection/verification.

The probe is injected into the system message tail on the first turn.
The trained model opens its first response with an ack header that
echoes the capability fingerprints and a keyed HMAC of the nonce.

Ack is once-per-session. Subsequent turns emit no ack header.

Two-section ack (v5.2+, current):

    [OZMA v5.2 ack:<hmac>
      trained: OUTLINE:a4f9c1 EDIT:b2c1d3 ...
      accept: LEARNED-BPE ATTN DOCSTUB ...]

Single-section ack (v4.x legacy, accepted for backward compatibility):

    [OZMA v5.2 ack:<hmac> OUTLINE:a4f9c1 EDIT:b2c1d3 ...]

Edge ack (LE profile, `handshake: "edge"` registry flag):

    [OZMA-EDGE ack: IR EDIT-EDGE]
"""

from __future__ import annotations

import hashlib
import hmac as _hmac_mod
import os
import re
from dataclasses import dataclass, field

from .registry import ModelEntry
from .store import MemorySessionStore, SessionStore
from .types import AckHeader

# Protocol version string embedded in probe/ack.
# v6.0 adds L4 wire-format negotiation (wire:<format> in probe/ack).
PROTOCOL_VERSION = "6.0"

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches the opening of any [OZMA v...] ack line.
# Group 1: 8-hex HMAC
# Group 2: single-section caps string (legacy — only present when ] on same line)
# Group 3: ] present (legacy single-section complete) or None (two-section continues)
_ACK_HEADER_RE = re.compile(
    r'^\[OZMA\s+v[\d.]+\s+ack:([0-9a-f]{8})'
    r'(?:\s+((?:[A-Z][A-Z0-9_-]*:[0-9a-f]{6}\s*)*))?'
    r'(\])?',
    re.IGNORECASE,
)

# Matches "  trained: CAP:fp ..." (with optional closing ])
_ACK_TRAINED_RE = re.compile(
    r'^\s+trained:\s+((?:[A-Z][A-Z0-9_-]*:[0-9a-f]{6}\s*)+)\]?',
    re.IGNORECASE,
)

# Matches "  accept: CAP ..." (with optional closing ])
_ACK_ACCEPT_RE = re.compile(
    r'^\s+accept:\s+((?:[A-Z][A-Z0-9_-]*\s*)+)\]?',
    re.IGNORECASE,
)

# Edge ack: [OZMA-EDGE ack: IR EDIT-EDGE]
# v6.0 may also embed `wire:<format>` inside the bracket; we accept it here
# and strip it out of the caps list in _parse_accept_caps.
_EDGE_ACK_RE = re.compile(
    r'^\[OZMA-EDGE\s+ack:\s*((?:(?:[A-Z][A-Z0-9_-]*|wire:[a-z]+)\s*)*)\]',
    re.IGNORECASE,
)

# Lines to skip when scanning for ack header
_SKIP_RE = re.compile(
    r'^(#|Sure|Of course|I\'ll|Let me|\s*</?thinking>|\s*$)',
    re.IGNORECASE,
)

# v6.0 — optional `wire:<format>` embedded anywhere in the ack line.
# Absent on v5.x acks; present only when the model echoed a non-default
# wire format back from the probe.
_WIRE_RE = re.compile(r'\bwire:([a-z]+)', re.IGNORECASE)


@dataclass
class AckResult:
    """Result of ack detection on a model response."""
    found: bool
    header: AckHeader | None
    hmac_valid: bool | None      # None if no secret configured or not found
    cap_mismatches: list[str]    # trained caps where fingerprint didn't match registry
    accept_caps: list[str]       # caps declared in accept: section
    stripped_response: str       # response with ack line(s) removed


def _negotiate_wire(probed: str, acked: str) -> str:
    """Resolve the session's wire format from probe + ack values.

    Rule: chars wins if either side asks for chars. Otherwise both sides
    must name the same non-default format — mismatched non-default values
    fall back to chars. This keeps mixed traffic correct by construction:
    no session can end up in a transport only one endpoint understands.
    """
    if probed == "chars" or acked == "chars":
        return "chars"
    return probed if probed == acked else "chars"


def _wire_token(entry: ModelEntry) -> str:
    """Emit `wire:<format> ` when the entry asks for a non-default transport.

    The default "chars" is omitted so v6.0 probes stay byte-for-byte
    compatible with v5.x probes on the common path. Non-default values
    carry explicitly and any v5-only counterparty that doesn't recognise
    the token can just ignore it — the caps exchange is still well-formed.
    """
    return "" if entry.wire_format == "chars" else f"wire:{entry.wire_format} "


def _build_probe(entry: ModelEntry, nonce: str) -> str:
    """Build the probe line for standard (non-edge) sessions.

    Native trained models (parse_mode=strict) use the compact training-data format.
    Prompted models get the verbose caps list so they know what to echo back.
    """
    if entry.parse_mode == "strict":
        return f"[OZMA probe nonce={nonce} {_wire_token(entry)}]".replace(" ]", "]")
    caps_str = " ".join(f"{cap}:{fp}" for cap, fp in entry.caps.items())
    return (
        f"[OZMA v{PROTOCOL_VERSION} probe:{nonce} "
        f"{_wire_token(entry)}caps: {caps_str}]"
    )


def _build_edge_probe(entry: ModelEntry) -> str:
    """Build the flat probe for edge sessions (no nonce, no HMAC)."""
    caps_str = " ".join(f"{cap}:{fp}" for cap, fp in entry.caps.items())
    return (
        f"[OZMA-EDGE v{PROTOCOL_VERSION} "
        f"{_wire_token(entry)}caps: {caps_str}]"
    )


def _verify_hmac(nonce: str, received_hmac: str, secret: str) -> bool:
    expected = hashlib.sha256(f"{nonce}{secret}".encode()).hexdigest()[:8]
    return _hmac_mod.compare_digest(expected, received_hmac.lower())


def _parse_trained_caps(caps_str: str) -> dict[str, str]:
    """Parse 'OUTLINE:a4f9c1 EDIT:b2c1d3 ...' into a dict."""
    caps: dict[str, str] = {}
    for token in caps_str.split():
        if ":" in token:
            name, fp = token.split(":", 1)
            caps[name.upper()] = fp.lower()
    return caps


def _parse_accept_caps(caps_str: str) -> list[str]:
    """Parse 'LEARNED-BPE ATTN DOCSTUB ...' into a list of cap names.

    Skips v6.0 `wire:<format>` tokens — those are transport, not capability.
    """
    return [
        t.upper() for t in caps_str.split()
        if t and not t.lower().startswith("wire:")
    ]


class HandshakeManager:
    """
    Manages per-session nonces and ack state.

    One instance per OzmaShim. Delegates persistence to a SessionStore so
    state survives restarts when an SQLiteSessionStore is used.
    """

    def __init__(self, store: SessionStore | None = None) -> None:
        self._store: SessionStore = store if store is not None else MemorySessionStore()

    def generate_probe(self, session_id: str, entry: ModelEntry) -> str:
        """
        Generate and store a probe line for injection into the system message.

        Returns the complete probe line. Call this once per session on the
        first request only — subsequent turns must NOT re-inject the probe.
        """
        if self._store.is_acked(session_id):
            return ""
        if entry.handshake == "edge":
            # Edge probe has no nonce — no need to store one.
            return _build_edge_probe(entry)
        nonce = os.urandom(4).hex()  # 8 hex chars
        self._store.set_nonce(session_id, nonce)
        return _build_probe(entry, nonce)

    def is_first_turn(self, session_id: str) -> bool:
        """True if we haven't seen an ack for this session yet."""
        return not self._store.is_acked(session_id)

    def detect_ack(
        self,
        session_id: str,
        response_text: str,
        entry: ModelEntry,
    ) -> AckResult:
        """
        Scan the first few non-blank lines of response_text for an ack header.

        For edge sessions (`entry.handshake == "edge"`) scans for
        `[OZMA-EDGE ack: ...]`; for standard sessions scans for `[OZMA v...]`.

        Returns AckResult with found=True if detected, and the response with
        the ack line(s) stripped.
        """
        if entry.handshake == "edge":
            return self._detect_edge_ack(session_id, response_text, entry)
        return self._detect_standard_ack(session_id, response_text, entry)

    def _detect_edge_ack(
        self,
        session_id: str,
        response_text: str,
        entry: ModelEntry,
    ) -> AckResult:
        lines = response_text.splitlines(keepends=True)
        ack_line_idx: int | None = None

        for i, line in enumerate(lines[:5]):
            stripped = line.strip()
            if not stripped:
                continue
            m = _EDGE_ACK_RE.match(stripped)
            if m:
                ack_line_idx = i
                declared = _parse_accept_caps(m.group(1))
                break
            # Stop scanning after first non-blank, non-edge-ack line
            break

        if ack_line_idx is None:
            self._store.mark_acked(session_id, {}, entry.model_id, entry.level)
            return AckResult(
                found=False,
                header=None,
                hmac_valid=None,
                cap_mismatches=[],
                accept_caps=[],
                stripped_response=response_text,
            )

        wire_match = _WIRE_RE.search(lines[ack_line_idx])
        wire_format = (
            wire_match.group(1).lower() if wire_match else "chars"
        )
        header = AckHeader(
            version=PROTOCOL_VERSION,
            hmac="",
            caps={},            # edge ack carries no fingerprints
            accept_caps=[],
            is_edge=True,
            wire_format=_negotiate_wire(entry.wire_format, wire_format),
        )
        self._store.mark_acked(session_id, {}, entry.model_id, entry.level)
        stripped = "".join(line for i, line in enumerate(lines) if i != ack_line_idx)
        return AckResult(
            found=True,
            header=header,
            hmac_valid=None,
            cap_mismatches=[],
            accept_caps=declared,
            stripped_response=stripped,
        )

    def _detect_standard_ack(
        self,
        session_id: str,
        response_text: str,
        entry: ModelEntry,
    ) -> AckResult:
        lines = response_text.splitlines(keepends=True)
        scanned = 0
        header_line_idx: int | None = None
        header_match = None

        for i, line in enumerate(lines):
            stripped = line.strip()
            if _SKIP_RE.match(stripped):
                continue
            m = _ACK_HEADER_RE.match(stripped)
            if m:
                header_line_idx = i
                header_match = m
                break
            scanned += 1
            if scanned >= 3:
                break

        if header_line_idx is None:
            self._store.mark_acked(session_id, {}, entry.model_id, entry.level)
            return AckResult(
                found=False,
                header=None,
                hmac_valid=None,
                cap_mismatches=[],
                accept_caps=[],
                stripped_response=response_text,
            )

        received_hmac = header_match.group(1)
        legacy_caps_str = header_match.group(2) or ""
        has_close_bracket = header_match.group(3) is not None

        trained_caps: dict[str, str] = {}
        accept_caps: list[str] = []
        ack_end_idx = header_line_idx  # last line consumed by the ack

        if has_close_bracket:
            # Legacy single-section: all caps on the header line, treated as trained
            trained_caps = _parse_trained_caps(legacy_caps_str)
        else:
            # Two-section form: scan next 1-2 lines for trained: and accept:
            for j in range(header_line_idx + 1, min(header_line_idx + 3, len(lines))):
                raw = lines[j]
                tm = _ACK_TRAINED_RE.match(raw)
                if tm:
                    trained_caps = _parse_trained_caps(tm.group(1))
                    ack_end_idx = j
                    continue
                am = _ACK_ACCEPT_RE.match(raw)
                if am:
                    accept_caps = _parse_accept_caps(am.group(1))
                    ack_end_idx = j
                    continue
                break  # non-ack line — done scanning

        wire_match = _WIRE_RE.search(lines[header_line_idx])
        wire_format = (
            wire_match.group(1).lower() if wire_match else "chars"
        )
        header = AckHeader(
            version=PROTOCOL_VERSION,
            hmac=received_hmac,
            caps=trained_caps,
            accept_caps=accept_caps,
            is_edge=False,
            wire_format=_negotiate_wire(entry.wire_format, wire_format),
        )

        nonce = self._store.get_nonce(session_id)
        hmac_valid: bool | None = None
        if nonce is not None and entry.secret is not None:
            hmac_valid = _verify_hmac(nonce, received_hmac, entry.secret)

        mismatches = []
        for cap, fp in entry.caps.items():
            if cap in trained_caps and trained_caps[cap] != fp:
                mismatches.append(cap)

        self._store.mark_acked(session_id, trained_caps, entry.model_id, entry.level)

        ack_line_indices = set(range(header_line_idx, ack_end_idx + 1))
        stripped = "".join(
            line for i, line in enumerate(lines) if i not in ack_line_indices
        )

        return AckResult(
            found=True,
            header=header,
            hmac_valid=hmac_valid,
            cap_mismatches=mismatches,
            accept_caps=accept_caps,
            stripped_response=stripped,
        )

    def mark_acked(self, session_id: str) -> None:
        self._store.mark_acked(session_id, {}, "", 1)
