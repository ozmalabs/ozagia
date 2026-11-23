"""Pair Python and Rust inventory symbols and derive parity facts.

Explicit ``[[migration.pairs]]`` entries win. ``infer = true`` adds
name/path/snake-case candidates, which are reported with a confidence and are
never accepted: they do not change a status and cannot make a pair ``ported``.

A status is manual when the pair config sets one; otherwise it is derived:

* both sides resolve and there are no parity flags → ``ported``;
* both sides resolve but a flag remains → ``diverged``;
* only the Python side resolves → ``planned``;
* only the Rust side resolves → ``extra``.

Parity flags are facts about the two inventories, not a promise of equivalent
behaviour. Cross-language type equality is never attempted: ``signature``
compares parameter/field names and whether a return annotation exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from ozagia.config import MIGRATION_STATUSES, MigrationConfig
from ozagia.migration.inventory import (
    PythonSymbol,
    RustSymbol,
    TreeInventory,
)

MAX_CANDIDATES = 200
MAX_INFER_PAIRS = 250_000
FLAG_ORDER = (
    "missing_counterpart", "kind", "arity", "async", "visibility", "doc",
    "signature",
)
_CALLABLE_KINDS = frozenset({
    "function", "async_function", "method", "async_method",
})
_RUST_CALLABLE_KINDS = frozenset({"function", "method"})
_RUST_TYPE_KINDS = frozenset({"struct", "enum", "union", "trait", "type_alias"})


@dataclass(frozen=True)
class MigrationRow:
    """One status row: a pair, a Python-only gap or a Rust-only extra."""

    python: str | None
    rust: str | None
    status: str
    source: str
    manual: bool
    flags: tuple[str, ...]
    python_kind: str = ""
    rust_kind: str = ""
    python_path: str = ""
    rust_path: str = ""
    python_signature: str = ""
    rust_signature: str = ""
    python_doc: bool = False
    rust_doc: bool = False
    python_resolved: bool = True
    rust_resolved: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "python": self.python,
            "rust": self.rust,
            "status": self.status,
            "source": self.source,
            "manual": self.manual,
            "flags": list(self.flags),
            "python_kind": self.python_kind,
            "rust_kind": self.rust_kind,
            "python_path": self.python_path,
            "rust_path": self.rust_path,
            "python_signature": self.python_signature,
            "rust_signature": self.rust_signature,
            "python_doc": self.python_doc,
            "rust_doc": self.rust_doc,
            "python_resolved": self.python_resolved,
            "rust_resolved": self.rust_resolved,
        }


@dataclass(frozen=True)
class Candidate:
    """An inferred pair. It is a report row only; it never changes status."""

    python: str
    rust: str
    confidence: float
    reason: str
    accepted: bool = False
    status_effect: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "python": self.python,
            "rust": self.rust,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "accepted": self.accepted,
            "status_effect": self.status_effect,
        }


@dataclass(frozen=True)
class MatchResult:
    rows: tuple[MigrationRow, ...]
    candidates: tuple[Candidate, ...]
    unresolved_pairs: tuple[dict[str, Any], ...]
    inferred: bool
    truncated: bool = False

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in MIGRATION_STATUSES}
        for row in self.rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        return counts

    def counts(self) -> dict[str, int]:
        matched = sum(1 for row in self.rows
                      if row.python_resolved and row.rust_resolved)
        return {
            "rows": len(self.rows),
            "matched": matched,
            "python_only": sum(1 for row in self.rows
                               if row.python_resolved and not row.rust_resolved),
            "rust_only": sum(1 for row in self.rows
                             if not row.python_resolved and row.rust_resolved),
            "candidates": len(self.candidates),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [row.to_dict() for row in self.rows],
            "candidates": [item.to_dict() for item in self.candidates],
            "unresolved_pairs": list(self.unresolved_pairs),
            "inferred": self.inferred,
            "truncated": self.truncated,
            "counts": self.counts(),
            "status_counts": self.status_counts(),
        }


def _ordered(flags: Iterable[str]) -> tuple[str, ...]:
    seen = set(flags)
    return tuple(flag for flag in FLAG_ORDER if flag in seen)


def _parity_flags(python: PythonSymbol, rust: RustSymbol) -> tuple[str, ...]:
    flags: list[str] = []
    py_type = python.kind not in _CALLABLE_KINDS
    rs_type = rust.kind in _RUST_TYPE_KINDS
    if py_type != rs_type:
        flags.append("kind")
    elif py_type:
        py_names = tuple(field.name for field in python.fields) or python.args
        rs_names = tuple(field.name for field in rust.fields) or rust.args
        if len(py_names) != len(rs_names):
            flags.append("arity")
        if py_names != rs_names:
            flags.append("signature")
        if python.async_ != rust.async_:
            flags.append("async")
        if python.api and rust.visibility not in ("pub", ""):
            flags.append("visibility")
        if bool(python.doc) != bool(rust.doc):
            flags.append("doc")
    else:
        if len(python.args) != len(rust.args):
            flags.append("arity")
        if python.args != rust.args:
            flags.append("signature")
        if python.async_ != rust.async_:
            flags.append("async")
        if python.api and rust.visibility not in ("pub", ""):
            flags.append("visibility")
        if bool(python.doc) != bool(rust.doc):
            flags.append("doc")
        if bool(python.returns) != bool(rust.returns):
            flags.append("signature")
    return _ordered(flags)


def _resolve_python(key: str,
                    by_key: dict[str, PythonSymbol],
                    ) -> tuple[PythonSymbol | None, str]:
    exact = by_key.get(key)
    if exact is not None:
        return exact, ""
    candidates = [item for item_key, item in by_key.items()
                  if item_key.endswith("." + key)]
    if len(candidates) == 1:
        return candidates[0], ""
    if len(candidates) > 1:
        return None, ("ambiguous Python symbol: "
                      + ", ".join(sorted(item.key for item in candidates[:5])))
    return None, "no Python symbol with that key"


def _resolve_rust(key: str, by_key: dict[str, RustSymbol],
                  ) -> tuple[RustSymbol | None, str]:
    exact = by_key.get(key)
    if exact is not None:
        return exact, ""
    candidates: list[RustSymbol] = []
    for item_key, item in by_key.items():
        match = item_key == key or item_key.endswith("::" + key)
        if not match and key.startswith("crate::"):
            match = item_key == f"{item.crate}::{key[len('crate::'):]}"
        if match:
            candidates.append(item)
    if len(candidates) == 1:
        return candidates[0], ""
    if len(candidates) > 1:
        return None, ("ambiguous Rust symbol: "
                      + ", ".join(sorted(item.key for item in candidates[:5])))
    return None, "no Rust symbol with that key"


def _normalized(name: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return re.sub(r"[^a-z0-9]", "", snake)


def _candidate_score(python: PythonSymbol, rust: RustSymbol) -> tuple[float, str]:
    py_name, rs_name = _normalized(python.name), _normalized(rust.name)
    py_module = python.module.rsplit(".", 1)[-1]
    rs_module = rust.module.rsplit("::", 1)[-1]
    if python.name == rust.name:
        return 0.95, "exact name"
    if py_name == rs_name and py_name:
        if py_module == rs_module:
            return 0.92, "same module and normalized name"
        return 0.90, "snake_case-insensitive name"
    if py_name and rs_name and (py_name.endswith(rs_name)
                                or rs_name.endswith(py_name)) and min(
            len(py_name), len(rs_name)) >= 4:
        return 0.65, "name suffix"
    if py_module == rs_module and py_module:
        return 0.55, "same module name"
    return 0.0, ""


def _build_candidates(python_inventory: TreeInventory,
                      rust_inventory: TreeInventory,
                      consumed_python: set[str],
                      consumed_rust: set[str],
                      ) -> tuple[tuple[Candidate, ...], bool]:
    rust_api = [item for item in rust_inventory.api
                if item.key not in consumed_rust and isinstance(item, RustSymbol)]
    python_api = [item for item in python_inventory.api
                  if item.key not in consumed_python
                  and isinstance(item, PythonSymbol)]
    if len(python_api) * len(rust_api) > MAX_INFER_PAIRS:
        return (), True
    scored: list[tuple[float, str, str, str]] = []
    for py in python_api:
        best: dict[str, tuple[float, str]] = {}
        for rs in rust_api:
            score, reason = _candidate_score(py, rs)
            if score < 0.6:
                continue
            previous = best.get(rs.key)
            if previous is None or score > previous[0]:
                best[rs.key] = (score, reason)
        for rust_key, (score, reason) in best.items():
            scored.append((score, py.key, rust_key, reason))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    out = [Candidate(python=py_key, rust=rust_key, confidence=score,
                     reason=reason)
           for score, py_key, rust_key, reason in scored[:MAX_CANDIDATES]]
    return tuple(out), len(scored) > MAX_CANDIDATES


def match(python_inventory: TreeInventory, rust_inventory: TreeInventory,
          config: MigrationConfig) -> MatchResult:
    """Pair both inventories according to `config` (see module docstring)."""
    python_by_key = python_inventory.by_key()
    rust_by_key = rust_inventory.by_key()
    consumed_python: set[str] = set()
    consumed_rust: set[str] = set()
    rows: list[MigrationRow] = []
    unresolved: list[dict[str, Any]] = []

    for pair in config.pairs:
        python, py_reason = _resolve_python(pair.python, python_by_key)
        rust, rs_reason = _resolve_rust(pair.rust, rust_by_key)
        if python is None and rust is None and pair.status is None:
            unresolved.append({
                "python": pair.python,
                "rust": pair.rust,
                "python_reason": py_reason,
                "rust_reason": rs_reason,
            })
            continue
        if python is not None:
            consumed_python.add(python.key)
        if rust is not None:
            consumed_rust.add(rust.key)
        flags = _parity_flags(python, rust) if python and rust else []
        if python is None or rust is None:
            flags = _ordered((*flags, "missing_counterpart"))
        if pair.status is not None:
            status = pair.status
            manual = True
        elif python is not None and rust is not None:
            status = "diverged" if flags else "ported"
            manual = False
        elif python is not None:
            status = "planned"
            manual = False
        else:
            status = "extra"
            manual = False
        rows.append(MigrationRow(
            python=python.key if python is not None else pair.python,
            rust=rust.key if rust is not None else pair.rust,
            status=status,
            source="explicit",
            manual=manual,
            flags=flags,
            python_kind=python.kind if python is not None else "",
            rust_kind=rust.kind if rust is not None else "",
            python_path=python.path if python is not None else "",
            rust_path=rust.path if rust is not None else "",
            python_signature=python.signature if python is not None else "",
            rust_signature=rust.signature if rust is not None else "",
            python_doc=bool(python and python.doc),
            rust_doc=bool(rust and rust.doc),
            python_resolved=python is not None,
            rust_resolved=rust is not None,
        ))

    for item in python_inventory.api:
        if item.key in consumed_python or not isinstance(item, PythonSymbol):
            continue
        rows.append(MigrationRow(
            python=item.key, rust=None, status="planned", source="derived",
            manual=False, flags=("missing_counterpart",),
            python_kind=item.kind, python_path=item.path,
            python_signature=item.signature, python_doc=bool(item.doc),
            rust_resolved=False,
        ))
    for item in rust_inventory.api:
        if item.key in consumed_rust or not isinstance(item, RustSymbol):
            continue
        rows.append(MigrationRow(
            python=None, rust=item.key, status="extra", source="derived",
            manual=False, flags=("missing_counterpart",),
            rust_kind=item.kind, rust_path=item.path,
            rust_signature=item.signature, rust_doc=bool(item.doc),
            python_resolved=False,
        ))

    rows.sort(key=lambda row: (row.python or "", row.rust or "", row.status))
    if config.infer:
        candidates, truncated = _build_candidates(
            python_inventory, rust_inventory, consumed_python, consumed_rust)
    else:
        candidates, truncated = (), False
    return MatchResult(
        rows=tuple(rows),
        candidates=candidates,
        unresolved_pairs=tuple(unresolved),
        inferred=config.infer,
        truncated=truncated,
    )


__all__ = [
    "Candidate",
    "FLAG_ORDER",
    "MatchResult",
    "MigrationRow",
    "match",
]
