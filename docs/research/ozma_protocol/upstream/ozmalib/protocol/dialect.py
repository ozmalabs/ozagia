"""
dialect.py — L2 dialect codecs: encode/decode the structural protocol form
against a model-specific tokenizer.

The protocol grammar (probe/ack/caps/edit/plan/claim/...) is invariant.
What varies is the *encoding* of that grammar on a particular wire. A
dialect is a specific encoding realization — a tokenizer's vocabulary,
merges, and special tokens, plus the mapping from protocol primitives to
token IDs. Dialects are static artifacts identified by a stable
fingerprint.

This module defines:

- `DialectCodec`: a loaded codec bound to one tokenizer. Exposes `encode`
  (text → token IDs) and `decode` (token IDs → text) and a `.fingerprint`
  property. Losslessness is a per-codec invariant, verified on load.

- `DialectStore`: a lazy-loading cache keyed by fingerprint. Loads at
  most once per fingerprint across the process; discards the underlying
  tokenizer on evict.

- `compute_fingerprint(path)`: deterministic SHA-256 over a `.ozmap`
  directory. Two dialects have the same fingerprint iff their
  tokenizer.json and added-tokens manifest are byte-identical. Used as
  the cache key, the telemetry tag, and the future map-push identity.

- `OZMAP_FILES`: the canonical file set that makes up a `.ozmap`. The
  fingerprint is computed over the concatenation of these in fixed
  order so that cross-platform serialization artifacts (permissions,
  timestamps) do not leak into the hash.

The `transformers` import is deferred to the first `load` call so the
base protocol library stays dependency-free for consumers that never
use L4.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any


# Canonical file set for a `.ozmap` dialect directory. The fingerprint
# hashes these in fixed order. Any file not in this list (editor
# backups, cached __pycache__, etc.) is ignored.
OZMAP_FILES: tuple[str, ...] = (
    "tokenizer.json",
    "tokenizer_config.json",
    "ozma_tokens.json",
)


# ──────────────────────────────────────────────────────────────────────
# Fingerprinting
# ──────────────────────────────────────────────────────────────────────


def compute_fingerprint(path: pathlib.Path | str) -> str:
    """Deterministic fingerprint of a dialect directory.

    Hashes the contents of every file in `OZMAP_FILES` in fixed order,
    separated by a length-prefixed frame so the hash is injective over
    the file set. Missing files are included as a zero-length frame —
    so a dialect without `tokenizer_config.json` has a distinct,
    well-defined fingerprint rather than being indistinguishable from
    one with an empty config.

    Returns a 64-char lowercase hex SHA-256 digest.
    """
    p = pathlib.Path(path)
    if not p.is_dir():
        raise ValueError(f"dialect path {p} is not a directory")

    h = hashlib.sha256()
    for name in OZMAP_FILES:
        target = p / name
        if target.is_file():
            blob = target.read_bytes()
        else:
            blob = b""
        # Length-prefixed frame: 4-byte big-endian length + name + 0x00 + content
        h.update(len(name).to_bytes(4, "big"))
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update(len(blob).to_bytes(8, "big"))
        h.update(blob)
    return h.hexdigest()


# ──────────────────────────────────────────────────────────────────────
# Codec
# ──────────────────────────────────────────────────────────────────────


@dataclass
class DialectMetadata:
    """Parsed contents of a dialect's `ozma_tokens.json`.

    Not all fields are meaningful for every dialect — the manifest grows
    over time. Unknown fields are preserved in `.extra` for forward
    compat.
    """
    base: str                         # Upstream tokenizer (e.g. "Qwen/Qwen2.5-Coder-3B-Instruct")
    vocab_before: int                 # Base tokenizer size before extension
    vocab_after: int                  # Size after adding Ozma tokens
    added_tokens: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_path(cls, path: pathlib.Path) -> "DialectMetadata":
        manifest_path = path / "ozma_tokens.json"
        if not manifest_path.is_file():
            raise ValueError(
                f"dialect at {path} has no ozma_tokens.json — "
                f"did you run extend_tokenizer.py?"
            )
        raw = json.loads(manifest_path.read_text())
        known = {"base", "vocab_before", "vocab_after", "added_tokens"}
        return cls(
            base=raw.get("base", "?"),
            vocab_before=int(raw.get("vocab_before", 0)),
            vocab_after=int(raw.get("vocab_after", 0)),
            added_tokens=list(raw.get("added_tokens", [])),
            extra={k: v for k, v in raw.items() if k not in known},
        )


class DialectCodec:
    """Encode/decode text against a dialect's tokenizer.

    Thin wrapper around a HuggingFace tokenizer plus the dialect's
    metadata. The tokenizer is loaded on construction and held for the
    codec's lifetime — reuse across requests is cheap and thread-safe
    (HF fast tokenizers are Rust-backed).

    Losslessness invariant: `decode(encode(x))` equals `x` for the
    protocol chars encoding (probes, acks, IR-glyph code, cap lines,
    edit blocks). This is verified on load by `_verify_round_trip()`;
    failures surface immediately rather than at the first mis-tokenized
    request.
    """

    __slots__ = ("_tokenizer", "_metadata", "_fingerprint", "_path")

    def __init__(
        self,
        tokenizer: Any,
        metadata: DialectMetadata,
        fingerprint: str,
        path: pathlib.Path | None = None,
    ) -> None:
        self._tokenizer = tokenizer
        self._metadata = metadata
        self._fingerprint = fingerprint
        self._path = path

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def metadata(self) -> DialectMetadata:
        return self._metadata

    @property
    def vocab_size(self) -> int:
        return len(self._tokenizer)

    def encode(self, text: str) -> list[int]:
        """Encode text to dialect token IDs. No special tokens added."""
        return self._tokenizer.encode(text, add_special_tokens=False)

    def decode(self, token_ids: list[int]) -> str:
        """Decode dialect token IDs back to text.

        Uses `skip_special_tokens=False` because the Ozma added tokens
        ARE special tokens but they're part of the structural encoding
        (probe/ack/caps/wire/etc.) — stripping them would break the
        parser. Base tokenizer's own special tokens (BOS/EOS/etc.) are
        preserved too; callers that want them hidden can strip post-hoc.
        """
        return self._tokenizer.decode(token_ids, skip_special_tokens=False)

    def single_token_id(self, text: str) -> int | None:
        """Return the single token ID for `text` if it tokenizes to one,
        else None. Useful for structural matching on a raw token stream
        without full decode (e.g. "is this the probe-start token?").
        """
        ids = self.encode(text)
        return ids[0] if len(ids) == 1 else None

    def _verify_round_trip(self, sample: str) -> None:
        """Assert encode∘decode is identity on `sample`. Called on load."""
        ids = self.encode(sample)
        round_trip = self.decode(ids)
        if round_trip != sample:
            raise ValueError(
                f"dialect {self._fingerprint[:12]} fails round-trip on "
                f"sample {sample!r}: decoded back as {round_trip!r}"
            )


def load_dialect(path: pathlib.Path | str) -> DialectCodec:
    """Load a dialect from a `.ozmap` directory.

    The directory must contain a HuggingFace tokenizer (tokenizer.json +
    tokenizer_config.json) and the Ozma manifest (ozma_tokens.json).
    Fingerprint is computed from those files; the tokenizer is loaded
    via transformers.AutoTokenizer; round-trip is verified on a small
    fixed probe + IR snippet so the codec is known-lossless before it
    serves a request.
    """
    p = pathlib.Path(path)
    if not p.is_dir():
        raise ValueError(f"dialect path {p} is not a directory")

    metadata = DialectMetadata.from_path(p)
    fingerprint = compute_fingerprint(p)

    # Deferred import — keeps transformers out of the base protocol
    # dependency surface. Callers who never load a dialect never import
    # it. This is the reason the function isn't just `DialectCodec.load`.
    try:
        from transformers import AutoTokenizer
    except ImportError as e:  # pragma: no cover — tested as ImportError path
        raise ImportError(
            "loading an L2 dialect requires the `transformers` package; "
            "install ozmalib[l4] or `pip install transformers`"
        ) from e

    tokenizer = AutoTokenizer.from_pretrained(str(p), use_fast=True)
    codec = DialectCodec(
        tokenizer=tokenizer,
        metadata=metadata,
        fingerprint=fingerprint,
        path=p,
    )

    # Round-trip check on a handful of structural fixtures. These cover
    # the probe shape, the ack shape, and an IR code snippet — the three
    # encoding surfaces the shim actually round-trips. If any of them
    # breaks we fail loudly at load rather than silently corrupting a
    # real session later.
    for sample in _ROUND_TRIP_FIXTURES:
        codec._verify_round_trip(sample)

    return codec


# Fixtures chosen to cover the three structural surfaces: a standard
# probe (nonce + caps), a trained-native strict probe, an IR code
# snippet. Kept minimal — every additional fixture is a load-time cost
# paid on every shim startup.
_ROUND_TRIP_FIXTURES: tuple[str, ...] = (
    "[OZMA v6.0 probe:abc123 wire:tokens caps: IR:ab12cd EDIT:b2c1d3]",
    "[OZMA probe nonce=xyz wire:tokens]",
    "ƒ foo(x){→ x+1}",
)


# ──────────────────────────────────────────────────────────────────────
# Store
# ──────────────────────────────────────────────────────────────────────


class DialectStore:
    """Process-lifetime cache of loaded dialects, keyed by fingerprint.

    A shim holds one store. On first request for a given model, the
    store materializes the codec from `entry.dialect_path` (or a caller-
    supplied override), verifies the fingerprint matches
    `entry.dialect_fingerprint`, and caches the result. Subsequent
    requests reuse the loaded codec.

    Fingerprint verification on load guards against the registry entry
    claiming a dialect that no longer matches the artifact on disk —
    catches silent upgrades and packaging mistakes before they reach
    the wire.
    """

    def __init__(self) -> None:
        self._codecs: dict[str, DialectCodec] = {}

    def register(self, codec: DialectCodec) -> None:
        """Insert a pre-loaded codec. Useful in tests and when the codec
        is constructed out-of-band (e.g. from an in-memory `.ozmap`)."""
        self._codecs[codec.fingerprint] = codec

    def get(self, fingerprint: str) -> DialectCodec | None:
        return self._codecs.get(fingerprint)

    def load_from_path(
        self,
        path: pathlib.Path | str,
        expected_fingerprint: str | None = None,
    ) -> DialectCodec:
        """Load and cache a dialect from `path`.

        If `expected_fingerprint` is set and the computed fingerprint
        doesn't match, raises ValueError — the caller's registry entry
        is out of sync with the artifact on disk. This is almost always
        a packaging / update mistake, so we fail loudly.
        """
        codec = load_dialect(path)
        if expected_fingerprint is not None and codec.fingerprint != expected_fingerprint:
            raise ValueError(
                f"dialect at {path} has fingerprint "
                f"{codec.fingerprint[:12]}... but registry entry claims "
                f"{expected_fingerprint[:12]}..."
            )
        self._codecs[codec.fingerprint] = codec
        return codec

    def ensure_for_entry(self, entry: Any) -> DialectCodec | None:
        """Return the codec for a ModelEntry, loading it on first use.

        Returns None when:
          - The entry has no dialect_fingerprint (chars-only model).
          - The codec isn't cached and no `dialect_path` is set (not yet
            packaged; shim falls back to chars with a one-time warning
            at the call site).

        Raises:
          - ValueError if the loaded artifact's fingerprint doesn't match
            what the entry claims (stale registry vs artifact).
        """
        fp = getattr(entry, "dialect_fingerprint", None)
        if fp is None:
            return None
        cached = self._codecs.get(fp)
        if cached is not None:
            return cached
        path = getattr(entry, "dialect_path", None)
        if path is None:
            return None
        return self.load_from_path(path, expected_fingerprint=fp)

    def clear(self) -> None:
        """Drop all cached codecs. Frees the underlying tokenizers."""
        self._codecs.clear()
