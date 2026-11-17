"""A bounded, read-only task preflight composed from existing owners.

The preflight answers one question: *are the conditions for working on this
goal currently verifiable?*  It composes, without writing anything:

* the goal's criteria state, re-evaluated read-only where that is safe;
* the graph snapshot freshness;
* the declared-capability readiness/audit from the WP1 self-graph;
* interpreter/environment candidates;
* resource admission;
* the affected-test plan.

Every fact is a :class:`ozagia.core.contracts.Fact` with a source, an injected
observation time, a contract freshness value and a contract evidence kind.
Per-dimension readiness is ``True``, ``False`` or ``"unknown"``.

Assessment rules
----------------

* A dimension is ready only when its provider says so **and** every fact it
  returned is ``current``.  A stale, absent or unknown fact can never be
  upgraded to ready by a provider.
* A dimension is ``failed`` when any checkout reports ``False``; it is
  ``unknown`` when no checkout fails but a checkout cannot verify it.
* The overall assessment is ``green`` only when every required dimension is
  ready and current.  Any ``False`` makes it ``failed``; otherwise any
  unknown makes it ``unknown``.
* A test plan is not an executed test.  The tests dimension reports whether a
  plan is available and current; it never turns a criterion into a pass.  The
  goal dimension reads criterion results and the test record.

Nothing in the default providers runs a test, starts a daemon, refreshes the
graph, imports a target project, opens a socket or calls a model.  The
defaults read files, `/proc`, git and the existing owners' read-only APIs.
Tests inject fakes through :class:`Providers`; only the integration tests use
the defaults on a temporary project.

Optional primary-scope joins (only when the caller selects them) add `linked`
checkouts, retained `research` sessions and read-only service/tunnel `actions`
readiness. They never change the six-dimension shape when omitted.

Durable evidence writes are deferred.  This package is read-only and does not
add records to the goal, task, session or graph stores.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ozagia.core import capabilities as K
from ozagia.core import contracts as C
from ozagia.core import services as S

SCHEMA_NAMESPACE = "ozagia.task_preflight"
SCHEMA_VERSION = "1.0"
REPORT_VERSION = "1.0"

DIMENSIONS = (
    "goal", "graph", "capabilities", "environment", "resources", "tests",
)
# Optional primary-scope dimensions are only present when the caller selects
# linked checkouts or retained research sessions.  They are not part of
# DIMENSIONS so existing readers keep the six-dimension shape and assessment.
LINKED_DIMENSION = "linked"
RESEARCH_DIMENSION = "research"
ACTIONS_DIMENSION = "actions"
OPTIONAL_DIMENSIONS = (LINKED_DIMENSION, RESEARCH_DIMENSION, ACTIONS_DIMENSION)
REQUIRED_DIMENSIONS = DIMENSIONS
ASSESSMENTS = ("green", "unknown", "failed")

DEFAULT_CAPABILITIES = (
    "cap:environment.interpreters",
    "cap:goals.inspect",
    "cap:test.scoped",
)
SAFE_GOAL_KINDS = frozenset({
    "file_absent", "file_exists", "ops_has", "review_resolved",
    "symbol_absent", "symbol_exists", "test_fails", "test_passes", "unchanged",
})
UNSAFE_GOAL_KINDS = frozenset({"calls", "lint_clean", "no_callers", "not_calls"})
TEST_GOAL_KINDS = frozenset({"test_fails", "test_passes"})

DEFAULT_LIMIT = 25
MAX_LIMIT = 200
MAX_CRITERIA = 50
MAX_FACTS = 256
MAX_CHECKOUTS = 8
MAX_LINKED = 8
MAX_RESEARCH_SESSIONS = 8
MAX_ACTION_SELECTIONS = 16
MAX_SCOPE_ITEMS = 32
MAX_CHANGED = 512
MAX_TIMEOUT_MS = 600_000
DEFAULT_TIMEOUT_MS = 30_000
MAX_DIMENSION_DATA_BYTES = 64 * 1024
MAX_REPORT_BYTES = 192 * 1024
MAX_UNKNOWNS = 40

STATUS_MAP = {
    "pass": "passed",
    "fail": "failed",
    "stale": "unknown",
    "unknown": "unknown",
    "unchecked": "not_assessed",
}
UNSAFE_RESULT_REASON = (
    "the criterion kind is not re-evaluated read-only; its recorded result is "
    "reported as declared evidence")
NO_TEST_REASON = "no test was run by this preflight"

DEFERRED = (
    "durable evidence writes",
    "goal, task, session and graph store writes",
    "idempotency keys and retention",
    "test execution",
    "graph refresh",
    "daemon start",
    "model and network calls",
)


class PreflightError(ValueError):
    """An invalid request or a refusal with a stable service error code."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        self.code = code if code in C.SERVICE_ERROR_CODES else "invalid_request"
        super().__init__(_line(message))


_SECRET_PATTERNS = (
    re.compile(r"(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"),
)


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


# ---------------------------------------------------------------------------
# Small bounded helpers
# ---------------------------------------------------------------------------


def _line(value: Any, limit: int = 400) -> str:
    text = " ".join(str(value).split())
    return _redact(text)[:limit]


def _text(value: Any, limit: int = 512) -> str:
    text = "" if value is None else str(value)
    text = "".join(" " if ord(char) < 0x20 else char for char in text)
    return _redact(text)[:limit]


def _mtime(path: Path) -> int | str:
    try:
        return int(path.stat().st_mtime_ns) // 1_000_000
    except OSError:
        return "unknown"


def _epoch_ms(value: Any) -> int | None:
    """Epoch seconds (goals) to integer milliseconds, or None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if number < 0 else int(number * 1000)
    return None


def _short(value: Any, limit: int = 12) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _clean(value: Any, *, depth: int = 0) -> Any:
    """Coerce an owner result into a canonical-JSON value.

    Floats become rounded integers; tuples become lists; other objects become
    bounded strings.  This keeps a provider's incidental float (a percentage,
    an age) from breaking canonical JSON while preserving the value's
    integer-scale meaning.
    """
    if depth > 8:
        return "[depth]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max(C.INT64_MIN, min(C.INT64_MAX, value))
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return max(C.INT64_MIN, min(C.INT64_MAX, int(round(value))))
    if isinstance(value, str):
        return _text(value, 512)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 128:
                break
            out[_text(key, 128)] = _clean(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_clean(item, depth=depth + 1) for item in value[:128]]
    return _text(value, 128)


def fact(subject: str, value: Any, *, source: str,
         observed_at: int | str = "unknown", freshness: str = "current",
         evidence: str = "observed") -> C.Fact:
    """Build one bounded fact with the project's provenance vocabulary."""
    return C.Fact(subject=subject, value=_clean(value), source=source,
                  observed_at=observed_at, freshness=freshness,
                  evidence=evidence)


def _check_limit(limit: Any) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise PreflightError("limit must be an integer")
    if limit < 1:
        raise PreflightError("limit must be at least 1")
    if limit > MAX_LIMIT:
        raise PreflightError(f"limit must be at most {MAX_LIMIT}")
    return limit


def _check_cursor(cursor: Any) -> int:
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor or not cursor.isdigit():
        raise PreflightError("cursor must be a decimal offset")
    if len(cursor) > 32:
        raise PreflightError("cursor is too long")
    return int(cursor)


def _paginate(items: Sequence[Any], limit: Any,
              cursor: Any) -> tuple[list[Any], C.Truncation]:
    page_size = _check_limit(limit)
    start = _check_cursor(cursor)
    total = len(items)
    page = list(items[start:start + page_size])
    consumed = start + len(page)
    if consumed >= total:
        return page, C.truncation()
    return page, C.truncation(True, omitted=total - consumed,
                              next_cursor=str(consumed), reason="limit")


def _natural_key(value: str) -> tuple[Any, ...]:
    return tuple(int(part) if part.isdigit() else part
                 for part in re.split(r"(\d+)", value))


def _criteria_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return _natural_key(str(item.get("id") or ""))


# ---------------------------------------------------------------------------
# Inputs, observations and providers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Checkout:
    """One selected checkout with optional contract identities."""

    root: Path
    label: str = "checkout[0]"
    repo: str | None = None
    checkout: str | None = None
    snapshot: str | None = None

    @classmethod
    def at(cls, root: str | Path, *, label: str = "checkout[0]",
           repo: str | None = None, checkout: str | None = None,
           snapshot: str | None = None) -> "Checkout":
        path = Path(root).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError as exc:
            raise PreflightError(f"checkout root does not exist: {root}") from exc
        if not path.is_dir():
            raise PreflightError(f"checkout root is not a directory: {path}")
        if (not isinstance(label, str) or not label or len(label) > 64
                or any(ord(char) < 0x20 for char in label)):
            raise PreflightError("checkout label must be a bounded string")
        repo_id, checkout_id, snapshot_id = _identities(repo, checkout, snapshot)
        return cls(root=path, label=label, repo=repo_id, checkout=checkout_id,
                   snapshot=snapshot_id)

    def identity(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "root": self.root.as_posix(),
            "repo": self.repo or "unknown",
            "checkout": self.checkout or "unknown",
            "snapshot": self.snapshot or "unknown",
        }


def _identity(value: Any, kind: str, what: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = C.parse_id(value)
    except C.ContractError as exc:
        raise PreflightError(f"invalid {what} identity: {exc}") from exc
    if parsed.kind != kind:
        raise PreflightError(
            f"{what} identity must be {kind}:, got {parsed.kind}")
    return parsed.canonical


def _identities(repo: Any, checkout: Any, snapshot: Any
                ) -> tuple[str | None, str | None, str | None]:
    repo_id = _identity(repo, "repo", "repo")
    checkout_id = _identity(checkout, "checkout", "checkout")
    snapshot_id = _identity(snapshot, "snapshot", "snapshot")
    if checkout_id is not None and repo_id is None:
        raise PreflightError("a checkout identity needs its repo identity")
    if snapshot_id is not None and checkout_id is None:
        raise PreflightError("a snapshot identity needs its checkout identity")
    if checkout_id is not None:
        parsed = C.parse_id(checkout_id)
        if parsed.components[:2] != C.parse_id(repo_id).components:
            raise PreflightError("checkout does not belong to the repo")
    if snapshot_id is not None:
        parsed = C.parse_id(snapshot_id)
        if parsed.components[:3] != C.parse_id(checkout_id).components:
            raise PreflightError("snapshot does not belong to the checkout")
    return repo_id, checkout_id, snapshot_id


@dataclass(frozen=True)
class LinkedCheckout:
    """One explicitly selected linked checkout and its caller-declared scope.

    A path is not an identity or a permission.  ``authorized`` and
    ``read_scope`` are declarations by the caller; they are never inferred from
    a manifest, a sibling directory or a successful read.
    """

    root: Path
    label: str = "linked[0]"
    repo: str | None = None
    checkout: str | None = None
    snapshot: str | None = None
    authorized: bool | str = "unknown"
    read_scope: tuple[str, ...] = ()
    symlinked: bool = False
    exists: bool = False

    @classmethod
    def at(cls, root: str | Path, *, label: str = "linked[0]",
           repo: str | None = None, checkout: str | None = None,
           snapshot: str | None = None,
           authorized: bool | str | None = None,
           read_scope: Iterable[str] | None = None,
           base: str | Path | None = None) -> "LinkedCheckout":
        if (not isinstance(label, str) or not label or len(label) > 64
                or any(ord(char) < 0x20 for char in label)):
            raise PreflightError("linked checkout label must be a bounded string")
        supplied = Path(root).expanduser()
        if not supplied.is_absolute() and base is not None:
            supplied = Path(base) / supplied
        symlinked = supplied.is_symlink()
        try:
            path = supplied.resolve(strict=False)
        except OSError as exc:
            raise PreflightError(
                f"linked checkout root cannot be resolved: {root}") from exc
        if not path.is_dir():
            # An explicitly selected member that is absent stays visible; it
            # is not silently dropped or replaced by a path match.
            exists = False
        else:
            exists = True
        repo_id, checkout_id, snapshot_id = _identities(repo, checkout, snapshot)
        try:
            authorization = ("unknown" if authorized is None
                             else C.readiness(authorized))
        except C.ContractError as exc:
            raise PreflightError(
                "linked authorization must be true, false or 'unknown'") from exc
        scope = _linked_scope(read_scope)
        return cls(root=path, label=label, repo=repo_id, checkout=checkout_id,
                   snapshot=snapshot_id, authorized=authorization,
                   read_scope=scope, symlinked=symlinked, exists=exists)

    def identity(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "root": self.root.as_posix(),
            "repo": self.repo or "unknown",
            "checkout": self.checkout or "unknown",
            "snapshot": self.snapshot or "unknown",
            "exists": self.exists,
            "symlinked": self.symlinked,
        }


def _linked_scope(value: Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise PreflightError(
            "linked read_scope must be an iterable of entries, not a string")
    items = list(value)
    if len(items) > MAX_SCOPE_ITEMS:
        raise PreflightError(
            f"linked read_scope exceeds {MAX_SCOPE_ITEMS} entries")
    checked: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item or len(item) > 256:
            raise PreflightError(
                "linked read_scope entries must be bounded strings")
        if any(ord(char) < 0x20 for char in item):
            raise PreflightError(
                "linked read_scope entries contain control characters")
        checked.append(item)
    return tuple(sorted(dict.fromkeys(checked)))


def _linked_checkouts(value: Any, *, base: Path
                      ) -> tuple[LinkedCheckout, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes, Mapping)):
        raise PreflightError(
            "linked_checkouts must be an iterable of mappings or LinkedCheckout")
    items = list(value)
    if len(items) > MAX_LINKED:
        raise PreflightError(
            f"at most {MAX_LINKED} linked checkouts are allowed")
    selected: list[LinkedCheckout] = []
    for index, item in enumerate(items):
        if isinstance(item, LinkedCheckout):
            member = item
        elif isinstance(item, Mapping):
            allowed = {"root", "label", "repo", "checkout", "snapshot",
                       "authorized", "read_scope"}
            unknown = sorted(set(item) - allowed)
            if unknown:
                raise PreflightError(
                    f"linked checkout has unknown fields: {unknown}")
            if "root" not in item:
                raise PreflightError("linked checkout needs a root")
            member = LinkedCheckout.at(
                item["root"], label=item.get("label", f"linked[{index}]"),
                repo=item.get("repo"), checkout=item.get("checkout"),
                snapshot=item.get("snapshot"),
                authorized=item.get("authorized"),
                read_scope=item.get("read_scope"), base=base)
        else:
            raise PreflightError(
                "linked checkouts must be mappings or LinkedCheckout")
        if any(existing.root == member.root for existing in selected):
            raise PreflightError(
                f"duplicate linked checkout root: {member.root}")
        if any(existing.label == member.label for existing in selected):
            raise PreflightError(
                f"duplicate linked checkout label: {member.label}")
        selected.append(member)
    return tuple(selected)


def _research_sessions(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        raise PreflightError(
            "research_sessions must be an iterable of ids, not a string")
    items = list(value)
    if len(items) > MAX_RESEARCH_SESSIONS:
        raise PreflightError(
            f"at most {MAX_RESEARCH_SESSIONS} research sessions are allowed")
    checked: list[str] = []
    for item in items:
        if (not isinstance(item, str) or not item or len(item) > 128
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
                                    item)):
            raise PreflightError(
                "research session ids must match "
                "[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
        if item not in checked:
            checked.append(item)
    return tuple(sorted(checked))


def _action_selections(value: Any) -> tuple[Any, ...] | None:
    """Validate an explicit service/tunnel action readiness selection."""
    if value is None:
        return None
    from ozagia.core import action_readiness as AR

    if isinstance(value, (str, bytes, Mapping)):
        raise PreflightError(
            "action_readiness must be an iterable of action selections")
    items = list(value)
    if len(items) > MAX_ACTION_SELECTIONS:
        raise PreflightError(
            f"at most {MAX_ACTION_SELECTIONS} action selections are allowed")
    try:
        parsed = AR.parse_selections(items)
    except AR.ActionReadinessError as exc:
        raise PreflightError(str(exc)) from None
    assert parsed is not None
    return tuple(parsed)


@dataclass(frozen=True)
class ProviderContext:
    """Everything a provider may read, with no ambient authority."""

    checkout: Checkout
    deadline: S.Deadline
    goal_id: str
    local_goal_id: str
    primary: bool
    scope: tuple[str, ...]
    changed: tuple[str, ...] | None
    base: str
    capabilities: tuple[str, ...]
    capability_trust: Mapping[str, bool | str]
    observed_at: int | str
    timeout_ms: int
    linked: tuple[LinkedCheckout, ...] = ()
    research_sessions: tuple[str, ...] | None = None
    action_readiness: tuple[Any, ...] | None = None


@dataclass(frozen=True)
class Observation:
    """One dimension's answer for one checkout."""

    readiness: bool | str
    facts: tuple[C.Fact, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None
    error: str | None = None
    truncated: C.Truncation | None = None

    def __post_init__(self) -> None:
        C.readiness(self.readiness)
        facts = tuple(self.facts)
        for item in facts:
            if not isinstance(item, C.Fact):
                raise TypeError("observation facts must be contracts.Fact")
        object.__setattr__(self, "facts", facts)
        data = dict(self.data or {})
        encoded = C.canonical_json(_clean(data))
        if len(encoded.encode("utf-8")) > MAX_DIMENSION_DATA_BYTES:
            raise C.BoundsError(
                f"dimension data exceeds {MAX_DIMENSION_DATA_BYTES} bytes")
        object.__setattr__(self, "data", _clean(data))
        if self.reason is not None:
            object.__setattr__(self, "reason", _line(self.reason))
        if self.error is not None:
            object.__setattr__(self, "error", _line(self.error))
        if self.truncated is not None:
            C.validate_truncation(self.truncated)


Provider = Callable[[ProviderContext], Observation]


@dataclass(frozen=True)
class Providers:
    graph: Provider
    goal: Provider
    capabilities: Provider
    environment: Provider
    resources: Provider
    tests: Provider
    # Optional primary-scope providers.  They are used only when the caller
    # selects linked checkouts, research sessions or action readiness and an
    # explicit provider was supplied; otherwise the read-only defaults below
    # are used.
    linked: Provider | None = None
    research: Provider | None = None
    actions: Provider | None = None

    def __post_init__(self) -> None:
        for name in DIMENSIONS:
            if not callable(getattr(self, name)):
                raise TypeError(f"provider {name} must be callable")
        for name in OPTIONAL_DIMENSIONS:
            provider = getattr(self, name)
            if provider is not None and not callable(provider):
                raise TypeError(f"provider {name} must be callable")


def _effective_readiness(observation: Observation) -> bool | str:
    """Cap a provider's claim by its facts; never upgrade a stale fact."""
    if observation.error is not None:
        return "unknown"
    if observation.readiness is False:
        return False
    if not observation.facts:
        return "unknown"
    if any(item.freshness != "current" for item in observation.facts):
        return "unknown"
    if observation.readiness is True:
        return True
    return "unknown"


def _aggregate(values: Iterable[bool | str]) -> bool | str:
    items = list(values)
    if any(value is False for value in items):
        return False
    if items and all(value is True for value in items):
        return True
    return "unknown"


def _dimension_reason(observation: Observation, readiness: bool | str) -> str:
    if observation.reason:
        return observation.reason
    if observation.error:
        return f"the {observation.error} error prevented an assessment"
    if readiness is False:
        return "the dimension is not ready"
    if readiness is True:
        return "ready and current"
    if not observation.facts:
        return "the provider returned no facts"
    stale = sorted({item.freshness for item in observation.facts
                    if item.freshness != "current"})
    return f"not current ({', '.join(stale)})"


def _dimension_report(observation: Observation) -> dict[str, Any]:
    readiness = _effective_readiness(observation)
    return {
        "readiness": readiness,
        "required": True,
        "reason": _dimension_reason(observation, readiness),
        "data": dict(observation.data),
        "facts": [item.to_dict() for item in observation.facts],
        "truncated": (observation.truncated.to_dict()
                      if observation.truncated is not None
                      else C.truncation().to_dict()),
        "error": observation.error,
    }


def _run_provider(name: str, provider: Provider, context: ProviderContext,
                  *, timed_out: bool, timeout_ms: int
                  ) -> tuple[Observation, bool]:
    """Run one provider; return the observation and the timeout latch."""
    if timed_out or context.deadline.expired():
        return _timeout_observation(name, timeout_ms=timeout_ms), True
    try:
        observation = provider(context)
    except PreflightError:
        raise
    except Exception as exc:  # noqa: BLE001 - partial states survive
        observation = _failed_observation(name, exc)
    if not isinstance(observation, Observation):
        observation = Observation(
            "unknown",
            reason=f"provider {name} returned "
                   f"{type(observation).__name__}, not Observation",
            error="internal")
    return observation, False


# ---------------------------------------------------------------------------
# Public build
# ---------------------------------------------------------------------------


def goal_identity(text: str) -> tuple[str, str]:
    """Normalize a bare goal id or ``goal:`` id to ``(canonical, local)``."""
    if not isinstance(text, str) or not text.strip():
        raise PreflightError("a goal id is required")
    value = text.strip()
    try:
        if value.startswith("goal:"):
            parsed = C.parse_id(value)
            return parsed.canonical, parsed.components[0]
        canonical = C.format_id("goal", value)
    except C.ContractError as exc:
        raise PreflightError(f"invalid goal id {text!r}: {exc}") from exc
    return canonical, C.parse_id(canonical).components[0]


def _scope(value: Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        raise PreflightError("scope must be an iterable of entries, not a string")
    items = list(value)
    if len(items) > MAX_SCOPE_ITEMS:
        raise PreflightError(f"scope exceeds {MAX_SCOPE_ITEMS} entries")
    checked: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item or len(item) > 256:
            raise PreflightError("scope entries must be bounded strings")
        if any(ord(char) < 0x20 for char in item):
            raise PreflightError("scope entries contain control characters")
        checked.append(item)
    return tuple(sorted(dict.fromkeys(checked)))


def _changed(value: Iterable[str] | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        raise PreflightError(
            "changed must be an iterable of paths, not a string")
    items = list(value)
    if len(items) > MAX_CHANGED:
        raise PreflightError(f"changed exceeds {MAX_CHANGED} paths")
    checked: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item or len(item) > 512:
            raise PreflightError("changed paths must be bounded strings")
        if any(ord(char) < 0x20 for char in item):
            raise PreflightError("changed paths contain control characters")
        checked.append(item)
    return tuple(sorted(dict.fromkeys(checked)))


def _check_capabilities(value: Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        items = list(DEFAULT_CAPABILITIES)
    elif isinstance(value, (str, bytes)):
        raise PreflightError(
            "required_capabilities must be an iterable of ids, not a string")
    else:
        items = list(value)
    checked: list[str] = []
    for item in items:
        try:
            parsed = C.parse_id(item)
        except C.ContractError as exc:
            raise PreflightError(f"invalid capability id {item!r}: {exc}") from exc
        if parsed.kind != "cap":
            raise PreflightError(f"not a capability id: {item!r}")
        checked.append(parsed.canonical)
    return tuple(sorted(dict.fromkeys(checked)))


def _check_trust(value: Mapping[str, Any] | None,
                 required: Sequence[str]) -> dict[str, bool | str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PreflightError("capability_trust must be a mapping")
    out: dict[str, bool | str] = {}
    for key, item in value.items():
        try:
            parsed = C.parse_id(key)
        except C.ContractError as exc:
            raise PreflightError(f"invalid capability id {key!r}: {exc}") from exc
        if parsed.kind != "cap" or parsed.canonical not in required:
            raise PreflightError(
                f"capability_trust names an unrequired capability {key!r}")
        out[parsed.canonical] = C.readiness(item)
    return out


def _check_timeout(value: Any) -> int:
    if value is None:
        return DEFAULT_TIMEOUT_MS
    if isinstance(value, bool) or not isinstance(value, int):
        raise PreflightError("timeout_ms must be an integer")
    if value < 0 or value > MAX_TIMEOUT_MS:
        raise PreflightError(
            f"timeout_ms must be between 0 and {MAX_TIMEOUT_MS}")
    return value


def _check_observed_at(value: Any) -> int | str:
    if value is None:
        return int(time.time() * 1000)
    if isinstance(value, bool):
        raise PreflightError("observed_at must be an injected value, not bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and 0 < len(value) <= 64:
        return value
    raise PreflightError("observed_at must be an integer or a short string")


def _timeout_observation(dimension: str, *, timeout_ms: int) -> Observation:
    return Observation(
        "unknown",
        (fact(f"preflight.{dimension}", {"state": "timeout"},
              source="ozagia.core.task_preflight", freshness="unknown",
              evidence="inferred"),),
        reason=f"the {timeout_ms} ms preflight timeout expired before this "
               "dimension was read",
        error="timeout",
    )


def _failed_observation(dimension: str, exc: BaseException) -> Observation:
    code = getattr(exc, "code", "internal")
    if code not in C.SERVICE_ERROR_CODES:
        code = "internal"
    text = _line(exc)
    return Observation(
        "unknown",
        (fact(f"preflight.{dimension}",
              {"state": "error", "error": code, "reason": text},
              source="ozagia.core.task_preflight", freshness="unknown",
              evidence="inferred"),),
        reason=text,
        error=code,
    )


def build_preflight(root: str | Path, goal_id: str, *,
                    scope: Iterable[str] | None = None,
                    checkouts: Iterable[str | Checkout] | None = None,
                    base: str = "HEAD",
                    changed: Iterable[str] | None = None,
                    required_capabilities: Iterable[str] | None = None,
                    capability_trust: Mapping[str, Any] | None = None,
                    linked_checkouts: (Iterable[Mapping[str, Any] | LinkedCheckout]
                                       | None) = None,
                    research_sessions: Iterable[str] | None = None,
                    action_readiness: (Iterable[Mapping[str, Any] | Any]
                                       | None) = None,
                    providers: Providers | None = None,
                    timeout_ms: int | None = None,
                    clock: Callable[[], int] | None = None,
                    observed_at: int | str | None = None,
                    limit: Any = None,
                    cursor: Any = None,
                    max_facts: int = MAX_FACTS) -> dict[str, Any]:
    """Build the bounded read-only preflight report for one goal."""
    canonical_goal, local_goal = goal_identity(goal_id)
    if not isinstance(base, str) or not base or len(base) > 128:
        raise PreflightError("base must be a bounded revision string")
    if isinstance(max_facts, bool) or not isinstance(max_facts, int):
        raise PreflightError("max_facts must be an integer")
    if max_facts < 0 or max_facts > MAX_FACTS:
        raise PreflightError(f"max_facts must be between 0 and {MAX_FACTS}")
    timeout_value = _check_timeout(timeout_ms)
    observed = _check_observed_at(observed_at)
    scope_items = _scope(scope)
    changed_items = _changed(changed)
    required = _check_capabilities(required_capabilities)
    trust = _check_trust(capability_trust, required)
    limit_value = _check_limit(limit)
    _check_cursor(cursor)

    primary = Checkout.at(root, label="checkout[0]")
    selected: list[Checkout] = [primary]
    if isinstance(checkouts, (str, bytes)):
        raise PreflightError("checkouts must be an iterable of roots or Checkout")
    for index, item in enumerate(checkouts or (), start=1):
        if index >= MAX_CHECKOUTS:
            raise PreflightError(f"at most {MAX_CHECKOUTS} checkouts are allowed")
        checkout = (item if isinstance(item, Checkout)
                    else Checkout.at(item, label=f"checkout[{index}]"))
        if any(existing.root == checkout.root for existing in selected):
            raise PreflightError(f"duplicate checkout root: {checkout.root}")
        if any(existing.label == checkout.label for existing in selected):
            raise PreflightError(
                f"duplicate checkout label: {checkout.label}")
        selected.append(checkout)

    linked_selected = _linked_checkouts(linked_checkouts, base=primary.root)
    research_selected = _research_sessions(research_sessions)
    action_selected = _action_selections(action_readiness)
    provider_set = providers or default_providers()
    if not isinstance(provider_set, Providers):
        raise TypeError("providers must be a task_preflight.Providers")
    deadline = S.Deadline.after(timeout_value, clock=clock or S.monotonic_ms)
    started_ms = deadline.started_ms
    timed_out = False

    checkout_reports: list[dict[str, Any]] = []
    contexts: list[ProviderContext] = []
    for position, checkout in enumerate(selected):
        context = ProviderContext(
            checkout=checkout,
            deadline=deadline,
            goal_id=canonical_goal,
            local_goal_id=local_goal,
            primary=position == 0,
            scope=scope_items,
            changed=changed_items,
            base=base,
            capabilities=required,
            capability_trust=trust,
            observed_at=observed,
            timeout_ms=timeout_value,
            linked=tuple(linked_selected or ()),
            research_sessions=research_selected,
            action_readiness=action_selected,
        )
        contexts.append(context)
        dimensions: dict[str, dict[str, Any]] = {}
        for dimension in DIMENSIONS:
            observation, timed_out = _run_provider(
                dimension, getattr(provider_set, dimension), context,
                timed_out=timed_out, timeout_ms=timeout_value)
            dimensions[dimension] = _dimension_report(observation)
        checkout_reports.append({
            **checkout.identity(),
            "dimensions": dimensions,
        })

    # Linked and research are primary-scope joins: they describe the anchor's
    # explicitly selected federation/research state, not each checkout's own
    # goal.  They are added only when the caller selected them, so the base
    # six-dimension report is byte-for-byte compatible.
    if linked_selected is not None:
        provider = provider_set.linked or linked_observation
        observation, timed_out = _run_provider(
            LINKED_DIMENSION, provider, contexts[0],
            timed_out=timed_out, timeout_ms=timeout_value)
        checkout_reports[0]["dimensions"][LINKED_DIMENSION] = (
            _dimension_report(observation))
    if research_selected is not None:
        provider = provider_set.research or research_observation
        observation, timed_out = _run_provider(
            RESEARCH_DIMENSION, provider, contexts[0],
            timed_out=timed_out, timeout_ms=timeout_value)
        checkout_reports[0]["dimensions"][RESEARCH_DIMENSION] = (
            _dimension_report(observation))
    if action_selected is not None:
        provider = provider_set.actions or actions_observation
        observation, timed_out = _run_provider(
            ACTIONS_DIMENSION, provider, contexts[0],
            timed_out=timed_out, timeout_ms=timeout_value)
        checkout_reports[0]["dimensions"][ACTIONS_DIMENSION] = (
            _dimension_report(observation))

    dimension_names = list(DIMENSIONS)
    for name in OPTIONAL_DIMENSIONS:
        if name in checkout_reports[0]["dimensions"]:
            dimension_names.append(name)
    aggregated: dict[str, dict[str, Any]] = {}
    for dimension in dimension_names:
        present = [row for row in checkout_reports
                   if dimension in row["dimensions"]]
        values = [row["dimensions"][dimension]["readiness"]
                  for row in present]
        readiness = _aggregate(values)
        reason = next((row["dimensions"][dimension]["reason"]
                       for row in present
                       if row["dimensions"][dimension]["readiness"] != readiness),
                      present[0]["dimensions"][dimension]["reason"])
        aggregated[dimension] = {
            "readiness": readiness,
            "required": True,
            "reason": reason,
            "checkouts": [
                {"label": row["label"],
                 "readiness": row["dimensions"][dimension]["readiness"],
                 "reason": row["dimensions"][dimension]["reason"]}
                for row in present
            ],
            "data": present[0]["dimensions"][dimension]["data"],
            "data_checkout": present[0]["label"],
        }

    overall = _overall(aggregated)
    primary_goal_data = dict(
        checkout_reports[0]["dimensions"]["goal"].get("data") or {})
    all_criteria = list(primary_goal_data.pop("criteria", []) or [])
    all_criteria.sort(key=_criteria_key)
    criteria_page, criteria_truncation = _paginate(
        all_criteria, limit_value, cursor)
    effective_scope = (list(scope_items) if scope is not None
                       else list(primary_goal_data.get("scope") or ()))

    facts_total, omitted = _bound_facts(checkout_reports, max_facts)
    if omitted and not criteria_truncation.truncated:
        report_truncation = C.truncation(True, omitted=omitted, reason="limit")
    elif omitted:
        report_truncation = C.truncation(
            True, omitted=criteria_truncation.omitted + omitted,
            reason=criteria_truncation.reason or "limit")
    else:
        report_truncation = criteria_truncation

    linked_data = (aggregated[LINKED_DIMENSION]["data"]
                   if linked_selected is not None else None)
    research_data = (aggregated[RESEARCH_DIMENSION]["data"]
                     if research_selected is not None else None)
    actions_data = (aggregated[ACTIONS_DIMENSION]["data"]
                    if action_selected is not None else None)
    extra_omitted = 0
    if isinstance(linked_data, Mapping):
        extra_omitted += int((linked_data.get("truncated") or {}).get("omitted") or 0)
    if isinstance(research_data, Mapping):
        extra_omitted += int(
            (research_data.get("truncated") or {}).get("omitted") or 0)
    if isinstance(actions_data, Mapping):
        extra_omitted += int(
            (actions_data.get("truncated") or {}).get("omitted") or 0)
    if extra_omitted:
        report_truncation = C.truncation(
            True,
            omitted=report_truncation.omitted + extra_omitted,
            reason=report_truncation.reason or "limit",
        )
    unknowns = _unknowns(aggregated)
    report: dict[str, Any] = {
        "schema": {"namespace": SCHEMA_NAMESPACE, "version": SCHEMA_VERSION},
        "report_version": REPORT_VERSION,
        "goal": {
            "id": canonical_goal,
            "local_id": local_goal,
            "status": primary_goal_data.get("status", "unknown"),
            "scope": list(effective_scope),
            "counts": dict(primary_goal_data.get("counts") or {}),
            "evaluated": bool(primary_goal_data.get("evaluated", False)),
        },
        "scope": list(effective_scope),
        "scope_source": ("request" if scope is not None else
                         "goal" if primary_goal_data.get("scope")
                         else "unspecified"),
        "checkouts": checkout_reports,
        "dimensions": aggregated,
        "criteria": {
            "total": len(all_criteria),
            "counts": _criteria_counts(all_criteria),
            "items": criteria_page,
            "truncated": criteria_truncation.to_dict(),
        },
        "assessment": {
            "status": overall,
            "green": overall == "green",
            "reasons": _assessment_reasons(aggregated),
            "notes": [
                "a test plan is not an executed test; no test was run by this "
                "preflight",
                "the graph was not refreshed; freshness is reported, not fixed",
            ],
        },
        "not_assessed": unknowns,
        "deferred": list(DEFERRED),
        "bounds": {
            "checkouts": len(checkout_reports),
            "dimensions": len(dimension_names),
            "criteria_total": len(all_criteria),
            "criteria_returned": len(criteria_page),
            "facts_total": facts_total,
            "facts_returned": facts_total - omitted,
            "facts_omitted": omitted,
            "max_facts": max_facts,
            "timeout_ms": timeout_value,
            "max_report_bytes": MAX_REPORT_BYTES,
        },
        "started_at": observed,
        "duration_ms": max(0, int(deadline.clock()) - started_ms),
        "truncated": report_truncation.to_dict(),
    }
    if isinstance(linked_data, Mapping):
        linked_section = {
            "schema": {
                "namespace": "ozagia.task_preflight.linked",
                "version": "1.0",
            },
            "selection": "explicit",
            "anchor": {"label": checkout_reports[0]["label"],
                       "root": checkout_reports[0]["root"]},
            "readiness": aggregated[LINKED_DIMENSION]["readiness"],
            "reason": aggregated[LINKED_DIMENSION]["reason"],
            "members": list(linked_data.get("members") or []),
            "counts": dict(linked_data.get("counts") or {}),
            "reference": dict(linked_data.get("reference") or {}),
            "provenance": dict(linked_data.get("provenance") or {}),
            "not_assessed": list(linked_data.get("not_assessed") or []),
            "deferred": list(linked_data.get("deferred") or []),
            "omissions": list(linked_data.get("omissions") or []),
            "truncated": dict(linked_data.get("truncated")
                              or C.truncation().to_dict()),
        }
        report["linked"] = linked_section
        compact_linked = {
            "members": len(linked_section["members"]),
            "counts": linked_section["counts"],
            "reference": linked_section["reference"],
            "provenance": linked_section["provenance"],
            "not_assessed": linked_section["not_assessed"],
            "deferred": linked_section["deferred"],
            "omissions": linked_section["omissions"],
            "truncated": linked_section["truncated"],
        }
        checkout_reports[0]["dimensions"][LINKED_DIMENSION]["data"] = (
            compact_linked)
        aggregated[LINKED_DIMENSION]["data"] = compact_linked
        report["bounds"]["linked_members"] = len(linked_section["members"])
    if isinstance(research_data, Mapping):
        research_section = dict(research_data)
        research_section["readiness"] = aggregated[RESEARCH_DIMENSION]["readiness"]
        research_section["reason"] = aggregated[RESEARCH_DIMENSION]["reason"]
        report["research"] = research_section
        compact_research = {
            "projection": research_section.get("projection"),
            "readiness": research_section["readiness"],
            "reason": research_section["reason"],
            "counts": research_section.get("counts"),
            "engines": research_section.get("engines"),
            "provenance": {"reader": "ozagia.core.task_preflight.research"},
            "not_assessed": research_section.get("not_assessed"),
            "deferred": research_section.get("deferred"),
            "omissions": research_section.get("omissions"),
            "truncated": research_section.get("truncated"),
            "claims": len(research_section.get("claims") or []),
            "sources": len(research_section.get("sources") or []),
            "assumptions": len(research_section.get("assumptions") or []),
            "evidence": len(research_section.get("evidence") or []),
        }
        checkout_reports[0]["dimensions"][RESEARCH_DIMENSION]["data"] = (
            compact_research)
        aggregated[RESEARCH_DIMENSION]["data"] = compact_research
        report["bounds"]["research_sessions"] = len(
            research_section.get("sessions") or [])
        report["bounds"]["research_claims_returned"] = len(
            research_section.get("claims") or [])
        report["bounds"]["research_claims_total"] = int(
            (research_section.get("counts") or {}).get("claims") or 0)
    if isinstance(actions_data, Mapping):
        actions_section = {
            "schema": dict(actions_data.get("schema") or {
                "namespace": "ozagia.action_readiness", "version": "1.0"}),
            "selection": "explicit",
            "state": actions_data.get("state"),
            "readiness": aggregated[ACTIONS_DIMENSION]["readiness"],
            "reason": aggregated[ACTIONS_DIMENSION]["reason"],
            "targets": list(actions_data.get("targets") or []),
            "operations": list(actions_data.get("operations") or []),
            "counts": dict(actions_data.get("counts") or {}),
            "provenance": dict(actions_data.get("provenance") or {}),
            "not_assessed": list(actions_data.get("not_assessed") or []),
            "deferred": list(actions_data.get("deferred") or []),
            "error": actions_data.get("error"),
            "observed_at": actions_data.get("observed_at"),
            "truncated": dict(actions_data.get("truncated")
                              or C.truncation().to_dict()),
        }
        report["actions"] = actions_section
        compact_actions = {
            "selection": actions_section["selection"],
            "state": actions_section["state"],
            "readiness": actions_section["readiness"],
            "reason": actions_section["reason"],
            "operations": [
                {"action": row.get("action"), "target": row.get("target"),
                 "readiness": row.get("readiness"),
                 "reason": row.get("reason")}
                for row in actions_section["operations"]
            ],
            "counts": actions_section["counts"],
            "provenance": {"reader": "ozagia.core.action_readiness"},
            "not_assessed": actions_section["not_assessed"],
            "deferred": actions_section["deferred"],
            "truncated": actions_section["truncated"],
        }
        checkout_reports[0]["dimensions"][ACTIONS_DIMENSION]["data"] = (
            compact_actions)
        aggregated[ACTIONS_DIMENSION]["data"] = compact_actions
        report["bounds"]["actions_selected"] = len(action_selected or ())
        report["bounds"]["actions_operations"] = len(
            actions_section["operations"])
    report = _enforce_bytes(report)
    report["hash"] = C.content_hash(
        {key: value for key, value in report.items() if key != "hash"})
    return report


def _overall(dimensions: Mapping[str, Mapping[str, Any]]) -> str:
    values = [row["readiness"] for row in dimensions.values()]
    if any(value is False for value in values):
        return "failed"
    if any(value is not True for value in values):
        return "unknown"
    return "green"


def _assessment_reasons(
        dimensions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    reasons = []
    for name, row in dimensions.items():
        if row["readiness"] is True:
            continue
        reasons.append({
            "dimension": name,
            "readiness": row["readiness"],
            "reason": row["reason"],
            "checkouts": row["checkouts"],
        })
    return reasons


def _criteria_counts(items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        result = str(item.get("result") or "unknown")
        counts[result] = counts.get(result, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def _unknowns(dimensions: Mapping[str, Mapping[str, Any]]) -> list[str]:
    found: set[str] = {
        "interpreter importability and package availability were not probed",
        "capability trust is only assessed when the caller supplies it",
        "resource admission is a point-in-time host sample",
        "the graph snapshot was not refreshed",
        "no test was executed",
    }
    for name in dimensions:
        data = dimensions[name].get("data") or {}
        values = data.get("not_assessed")
        if isinstance(values, list):
            found.update(str(item) for item in values if item)
    return sorted(found)[:MAX_UNKNOWNS]


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def _bound_facts(checkout_reports: list[dict[str, Any]],
                 max_facts: int) -> tuple[int, int]:
    """Keep a deterministic round-robin prefix of facts across dimensions."""
    total = sum(len(row["facts"])
                for checkout in checkout_reports
                for row in checkout["dimensions"].values())
    if total <= max_facts:
        return total, 0
    kept: dict[tuple[str, str], int] = {
        (checkout["label"], dimension): 0
        for checkout in checkout_reports
        for dimension in checkout["dimensions"]
    }
    remaining = max_facts
    while remaining > 0:
        progressed = False
        for checkout in checkout_reports:
            for dimension, row in checkout["dimensions"].items():
                key = (checkout["label"], dimension)
                if remaining > 0 and kept[key] < len(row["facts"]):
                    kept[key] += 1
                    remaining -= 1
                    progressed = True
        if not progressed:
            break
    omitted = 0
    for checkout in checkout_reports:
        for dimension, row in checkout["dimensions"].items():
            facts = row["facts"]
            allowance = kept[(checkout["label"], dimension)]
            omitted += len(facts) - allowance
            row["facts"] = facts[:allowance]
            row["facts_omitted"] = len(facts) - allowance
    return total, omitted


def _enforce_bytes(report: dict[str, Any]) -> dict[str, Any]:
    """Trim optional lists deterministically when the report exceeds bounds."""
    def too_big() -> bool:
        return len(C.canonical_json(report).encode("utf-8")) > MAX_REPORT_BYTES

    if not too_big():
        report["bounds"]["report_bytes"] = len(
            C.canonical_json(report).encode("utf-8"))
        report["bounds"]["bytes_truncated"] = False
        return report

    facts_dropped = 0
    criteria_dropped = 0
    research_dropped = 0
    actions_dropped = 0
    actions = report.get("actions")
    if isinstance(actions, dict):
        for key in ("operations", "targets"):
            values = actions.get(key)
            while isinstance(values, list) and values and too_big():
                values.pop()
                actions_dropped += 1
        if actions_dropped:
            actions["truncated"] = C.truncation(
                True,
                omitted=int((actions.get("truncated") or {}).get("omitted") or 0)
                + actions_dropped,
                reason="bytes").to_dict()
    research = report.get("research")
    if isinstance(research, dict):
        for key in ("evidence", "assumptions", "sources", "claims", "sessions"):
            values = research.get(key)
            while isinstance(values, list) and values and too_big():
                values.pop()
                research_dropped += 1
        if research_dropped:
            research["truncated"] = C.truncation(
                True,
                omitted=int((research.get("truncated") or {}).get("omitted") or 0)
                + research_dropped,
                reason="bytes").to_dict()
            research.setdefault("omissions", []).append(
                f"{research_dropped} research rows omitted by bytes")
    for checkout in report["checkouts"]:
        for row in checkout["dimensions"].values():
            while row["facts"] and too_big():
                row["facts"].pop()
                row["facts_omitted"] = int(row.get("facts_omitted") or 0) + 1
                facts_dropped += 1
    while report["criteria"]["items"] and too_big():
        report["criteria"]["items"].pop()
        criteria_dropped += 1
    while report["not_assessed"] and too_big():
        report["not_assessed"].pop()
    report["bounds"]["facts_omitted"] = (
        int(report["bounds"].get("facts_omitted") or 0) + facts_dropped)
    report["bounds"]["facts_returned"] = max(
        0, int(report["bounds"].get("facts_returned") or 0) - facts_dropped)
    report["bounds"]["criteria_returned"] = max(
        0, int(report["bounds"].get("criteria_returned") or 0)
        - criteria_dropped)
    report["bounds"]["bytes_truncated"] = True
    report["truncated"] = C.truncation(
        True, omitted=(facts_dropped + criteria_dropped + research_dropped
                       + actions_dropped),
        reason="bytes").to_dict()
    report["criteria"]["truncated"] = report["truncated"]
    report["bounds"]["report_bytes"] = len(
        C.canonical_json(report).encode("utf-8"))
    return report


def report_json(report: Mapping[str, Any]) -> str:
    """Canonical JSON for a preflight report."""
    return C.canonical_json(dict(report))


def report_hash(report: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical report (the ``hash`` field excluded)."""
    return C.content_hash({key: value for key, value in report.items()
                           if key != "hash"})


def preflight_envelope(report: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap a preflight report in the versioned WP2 service envelope."""
    digest = report_hash(report)
    if "hash" in report and report["hash"] != digest:
        raise PreflightError(
            "preflight report hash does not match its contents",
            code="invalid_value")
    sources: dict[str, S.Source] = {}
    for checkout in report.get("checkouts") or []:
        dimensions = checkout.get("dimensions") or {}
        for row in dimensions.values():
            for item in row.get("facts") or []:
                source = item.get("source")
                if (not isinstance(source, str) or source in sources
                        or len(sources) >= S.MAX_PROVENANCE_SOURCES):
                    continue
                sources[source] = S.Source(
                    source,
                    observed_at=item.get("observed_at"),
                    freshness=item.get("freshness", "current"),
                    evidence=item.get("evidence", "observed"))
    return S.ok(
        "task_preflight", dict(report),
        side_effect="read",
        provenance=S.provenance(sources.values(), inputs=[digest]),
        started_at=report.get("started_at"),
        duration_ms=report.get("duration_ms"),
    )


# ---------------------------------------------------------------------------
# Default providers: read-only composition of existing owners
# ---------------------------------------------------------------------------


def read_graph_state(root: str | Path) -> dict[str, Any]:
    """Read an existing snapshot's freshness without refreshing it."""
    from ozagia.config import state_path

    path = state_path(root, "graph.json")
    info: dict[str, Any] = {
        "path": path.as_posix(),
        "available": False,
        "state": "absent",
        "freshness": "absent",
        "reason": f"no graph snapshot at {path}",
        "snapshot_head": None,
        "head": None,
        "root_mismatch": False,
        "tree_changed": False,
        "observed_at": _mtime(path),
    }
    if not path.is_file():
        return info
    try:
        from ozagia.core.memory import DevMemory

        dev = DevMemory(dump=path, journal=state_path(root, "journal.jsonl"),
                        root=root)
        staleness = dev.staleness()
    except Exception as exc:  # noqa: BLE001 - a read failure is unknown
        info.update({
            "available": True,
            "state": "error",
            "freshness": "unknown",
            "reason": _line(f"cannot read graph snapshot: "
                            f"{type(exc).__name__}: {exc}", 300),
        })
        return info
    state = str(staleness.get("freshness") or "unknown")
    info.update({
        "available": True,
        "state": state,
        "freshness": {"fresh": "current", "stale": "stale"}.get(
            state, "unknown"),
        "snapshot_head": _short(staleness.get("snapshot_head")),
        "head": _short(staleness.get("head")),
        "root_mismatch": bool(staleness.get("root_mismatch")),
        "tree_changed": bool(staleness.get("tree_changed")),
    })
    errors = [str(item) for item in staleness.get("verification_errors") or []]
    if state == "fresh":
        info["reason"] = "snapshot root, HEAD and working-tree digest match"
    elif state == "stale":
        details = []
        if info.get("root_mismatch"):
            details.append("snapshot root differs")
        if info.get("tree_changed"):
            details.append("working tree changed since the snapshot")
        if (staleness.get("snapshot_head")
                and staleness.get("snapshot_head") != staleness.get("head")):
            details.append("HEAD moved since the snapshot")
        info["reason"] = _line("; ".join(details) or "snapshot is stale", 300)
    else:
        info["reason"] = _line("; ".join(errors)
                               or "graph freshness could not be verified", 300)
    return info


def graph_observation(context: ProviderContext) -> Observation:
    """Snapshot availability and freshness; never refresh or rebuild."""
    info = read_graph_state(context.checkout.root)
    readiness: bool | str = (
        True if info["state"] == "fresh" else
        False if info["state"] == "absent" else "unknown")
    evidence = "observed" if info["available"] else "inferred"
    value = {
        "state": info["state"],
        "reason": info["reason"],
        "snapshot_head": info["snapshot_head"],
        "head": info["head"],
        "root_mismatch": info["root_mismatch"],
        "tree_changed": info["tree_changed"],
    }
    return Observation(
        readiness,
        (fact("preflight.graph", value, source="ozagia.core.memory.DevMemory",
              observed_at=info["observed_at"], freshness=info["freshness"],
              evidence=evidence),),
        data={**info, "refreshed": False},
        reason=info["reason"],
    )


def _load_goal(root: Path, local_goal: str, *, primary: bool
               ) -> tuple[dict[str, Any] | None, Path, str | None]:
    from ozagia.core import goals as GOALS

    path = GOALS.goals_path(root)
    if not path.is_file():
        if primary:
            raise PreflightError(f"no goal store at {path}", code="not_found")
        return None, path, "no goal store in this checkout"
    try:
        store = GOALS.load_store(root)
    except GOALS.GoalError as exc:
        return None, path, _line(exc, 300)
    for goal in store.get("goals") or []:
        if goal.get("id") == local_goal:
            return goal, path, None
    if primary:
        raise PreflightError(f"no goal {local_goal!r} in {path}", code="not_found")
    return None, path, "the goal is not in this checkout's store"


def _recorded_test(root: Path, key: str) -> dict[str, Any] | None:
    try:
        from ozagia.core import working_set as W

        record = W.load_test_record(root)
        got = W.recorded_outcome(record, key, evidence=True)
    except Exception:  # noqa: BLE001 - evidence stays unknown
        return None
    return {
        "key": key,
        "outcome": got.get("outcome") or "unknown",
        "recorded_at": _epoch_ms(got.get("ts")),
        "reason": _line(got.get("reason") or "", 300) or None,
    }


def goal_observation(context: ProviderContext, *,
                     graph_state: Mapping[str, Any] | None = None
                     ) -> Observation:
    """Read-only criterion state; re-evaluate only the safe kinds."""
    from ozagia.core import goals as GOALS

    observed_at = _mtime(GOALS.goals_path(context.checkout.root))
    goal, path, error = _load_goal(context.checkout.root,
                                   context.local_goal_id,
                                   primary=context.primary)
    if goal is None:
        return Observation(
            "unknown",
            (fact("preflight.goal", {"state": "unreadable", "reason": error},
                  source="ozagia.core.goals", observed_at=observed_at,
                  freshness="unknown", evidence="inferred"),),
            data={"id": context.goal_id, "local_id": context.local_goal_id,
                  "status": "unknown", "criteria": [],
                  "counts": {}, "evaluated": False},
            reason=error,
            error="unavailable",
        )
    state = dict(graph_state or read_graph_state(context.checkout.root))
    memory = None
    graph_label = f"{state.get('state')}: {state.get('reason')}"
    if state.get("state") == "fresh":
        try:
            from ozagia.config import state_path
            from ozagia.core.memory import DevMemory

            memory = DevMemory(
                dump=Path(str(state["path"])),
                journal=state_path(context.checkout.root, "journal.jsonl"),
                root=context.checkout.root)
            graph_label = "not refreshed"
        except Exception as exc:  # noqa: BLE001 - graph kinds stay unknown
            graph_label = f"unreadable: {type(exc).__name__}"
    evaluator = GOALS.Evaluator(context.checkout.root, memory,
                                graph=graph_label)

    items: list[dict[str, Any]] = []
    criterion_facts: list[C.Fact] = []
    criterion_rows = sorted(goal.get("criteria") or [],
                            key=lambda item: _natural_key(
                                str(item.get("id") or "")))
    total_criteria = len(criterion_rows)
    if total_criteria > MAX_CRITERIA:
        criterion_rows = criterion_rows[:MAX_CRITERIA]
    for criterion in criterion_rows:
        kind = str(criterion.get("kind") or "unknown")
        args = criterion.get("args") or {}
        recorded = criterion.get("last") or {}
        evaluated = kind in SAFE_GOAL_KINDS
        if evaluated:
            result = evaluator.evaluate(goal, criterion)
            status = str(result.get("status") or "unknown")
            reason = _line(result.get("reason") or "", 300) or None
            freshness = ("current" if status in ("pass", "fail")
                         else "stale" if status == "stale" else "unknown")
            evidence = "observed"
            item_observed = context.observed_at
        else:
            status = str(recorded.get("status") or "unchecked")
            reason = (_line(recorded.get("reason") or "", 300)
                      or UNSAFE_RESULT_REASON)
            freshness = ("unknown" if recorded else "absent")
            evidence = "declared" if recorded else "inferred"
            item_observed = _epoch_ms(recorded.get("at")) or observed_at
        mapped = STATUS_MAP.get(status, "unknown")
        test_join = None
        key = args.get("key") if isinstance(args, Mapping) else None
        if kind in TEST_GOAL_KINDS and isinstance(key, str):
            test_join = _recorded_test(context.checkout.root, key)
        item = {
            "id": str(criterion.get("id") or "unknown"),
            "kind": kind,
            "result": mapped,
            "recorded_status": status,
            "freshness": freshness,
            "evidence": evidence,
            "source": "ozagia.core.goals",
            "observed_at": item_observed,
            "reason": reason,
            "test": test_join,
            "interpreter": {
                "state": "not_recorded",
                "reason": "the test record does not record which interpreter "
                          "ran",
            } if kind in TEST_GOAL_KINDS else None,
        }
        items.append(item)
        fact_value = {
            "kind": kind,
            "result": mapped,
            "recorded_status": status,
            "reason": reason,
            "test": test_join,
        }
        if isinstance(key, str):
            fact_value["key"] = _text(key, 300)
        criterion_fact = fact(
            f"preflight.goal.criterion.{item['id']}", fact_value,
            source="ozagia.core.goals.Evaluator" if evaluated
                   else "ozagia.core.goals",
            observed_at=item_observed, freshness=freshness,
            evidence=evidence)
        items[-1]["fact"] = criterion_fact.to_dict()
        criterion_facts.append(criterion_fact)

    counts = _criteria_counts(items)
    status = str(goal.get("status") or "unknown")
    truncated = (C.truncation(True, omitted=total_criteria - len(items),
                              reason="limit")
                 if total_criteria > len(items) else C.truncation())
    if status in ("blocked", "dropped"):
        readiness: bool | str = False
        reason = f"goal status is {status}"
    elif not items:
        readiness = "unknown"
        reason = "the goal has no criteria; there is nothing to verify"
    elif total_criteria > len(items):
        readiness = "unknown"
        reason = (f"the goal has {total_criteria} criteria; only the first "
                  f"{len(items)} were assessed")
    elif any(item["result"] == "failed" for item in items):
        readiness = False
        reason = "at least one goal criterion failed"
    elif all(item["result"] == "passed" for item in items):
        readiness = True
        reason = "every goal criterion passed and is current"
    else:
        readiness = "unknown"
        reason = "at least one goal criterion is unknown or not assessed"
    scope = _scope_list(goal.get("scope") or [])
    summary = fact(
        "preflight.goal",
        {"status": status, "criteria": len(items), "counts": counts},
        source="ozagia.core.goals", observed_at=observed_at,
        freshness="current", evidence="observed")
    return Observation(
        readiness,
        (summary, *criterion_facts),
        data={
            "id": context.goal_id,
            "local_id": context.local_goal_id,
            "status": status,
            "scope": scope,
            "criteria": items,
            "counts": counts,
            "criteria_total": total_criteria,
            "graph": {"state": state.get("state"),
                      "freshness": state.get("freshness"),
                      "reason": state.get("reason")},
            "evaluated": True,
            "not_assessed": [
                "the graph was not refreshed before criterion evaluation",
                "criterion kinds that build a tag cache or symbol graph are "
                "reported from their recorded result",
                "a test result is linked to the interpreter only when the "
                "record carries it; the current record does not",
            ],
        },
        reason=reason,
        truncated=truncated,
    )


def _scope_list(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    out = []
    for item in value:
        if isinstance(item, str) and item and len(item) <= 256:
            out.append(item)
        if len(out) >= MAX_SCOPE_ITEMS:
            break
    return sorted(dict.fromkeys(out))


def _finding_capabilities(finding: Mapping[str, Any]) -> set[str]:
    provenance = finding.get("provenance") or {}
    owners = {provenance.get("capability")}
    owners.update(provenance.get("capabilities") or [])
    return {str(item) for item in owners if item}


def capability_observation(
        context: ProviderContext, *,
        report: Mapping[str, Any] | None = None,
        catalogue: Sequence[K.Capability] | None = None) -> Observation:
    """Declared-capability readiness joined with the WP1 self-graph audit."""
    from ozagia.core import self_graph as SG

    caps = tuple(K.catalogue() if catalogue is None else catalogue)
    declared = {item.id: item for item in caps}
    try:
        graph = dict(report) if report is not None else SG.build_self_graph(
            capabilities=caps, root=context.checkout.root)
    except Exception as exc:  # noqa: BLE001 - a partial state survives
        return Observation(
            "unknown",
            (fact("preflight.capabilities",
                  {"state": "unknown", "reason": type(exc).__name__},
                  source="ozagia.core.self_graph", freshness="unknown",
                  evidence="inferred"),),
            data={"capabilities": {}, "audit_state": "unknown"},
            reason=_line(f"self-graph unavailable: {type(exc).__name__}: {exc}",
                         300),
            error="unavailable",
        )
    rows = list(graph.get("surface_states") or [])
    findings = list((graph.get("audit") or {}).get("findings") or [])
    results: dict[str, dict[str, Any]] = {}
    capability_facts: list[C.Fact] = []
    values: list[bool | str] = []
    for capability_id in context.capabilities:
        rows_for = [row for row in rows
                    if row.get("capability") == capability_id]
        findings_for = [row for row in findings
                        if capability_id in _finding_capabilities(row)]
        surface_counts: dict[str, int] = {}
        for row in rows_for:
            state = str(row.get("state") or "unknown")
            surface_counts[state] = surface_counts.get(state, 0) + 1
        finding_counts: dict[str, int] = {}
        for row in findings_for:
            severity = str(row.get("severity") or "unknown")
            finding_counts[severity] = finding_counts.get(severity, 0) + 1
        trust = context.capability_trust.get(capability_id, "unobserved")
        if capability_id not in declared:
            readiness: bool | str = False
            reason = "the capability is not in the declared catalogue"
        elif trust is False:
            readiness = False
            reason = "the caller marked the capability untrusted"
        elif trust == "unknown":
            readiness = "unknown"
            reason = "the caller marked the capability's trust as unknown"
        elif finding_counts.get("error"):
            readiness = False
            reason = "the self-graph audit reports an error for this capability"
        elif surface_counts.get("missing"):
            readiness = False
            reason = "a declared surface is absent"
        elif finding_counts.get("unknown") or surface_counts.get("unknown"):
            readiness = "unknown"
            reason = "a declared surface or audit state is unknown"
        elif surface_counts and not surface_counts.get("observed"):
            readiness = False
            reason = "no declared surface was observed"
        elif surface_counts.get("observed"):
            readiness = True
            reason = "the declared surfaces were observed"
        else:
            readiness = "unknown"
            reason = "the capability declares no observable surface"
        values.append(readiness)
        freshness = ("current" if readiness is True else
                     "absent" if readiness is False else "unknown")
        evidence = "observed" if readiness is True else "declared"
        results[capability_id] = {
            "readiness": readiness,
            "reason": reason,
            "surfaces": dict(sorted(surface_counts.items())),
            "findings": dict(sorted(finding_counts.items())),
            "trust": trust,
        }
        capability_facts.append(fact(
            f"preflight.capabilities.{capability_id}",
            {"readiness": readiness, "reason": reason,
             "surfaces": surface_counts, "findings": finding_counts,
             "trust": trust},
            source="ozagia.core.self_graph", freshness=freshness,
            evidence=evidence))
    readiness = _aggregate(values)
    summary = fact(
        "preflight.capabilities",
        {"required": list(context.capabilities),
         "readiness": readiness,
         "counts": {
             "true": values.count(True),
             "false": values.count(False),
             "unknown": values.count("unknown"),
         }},
        source="ozagia.core.self_graph",
        freshness=("current" if readiness is True else
                   "absent" if readiness is False else "unknown"),
        evidence=("observed" if readiness is True else "declared"))
    reason = next((results[item]["reason"] for item in context.capabilities
                   if results[item]["readiness"] != readiness), None)
    if reason is None:
        reason = ("every required capability is observed"
                  if readiness is True else "capability readiness is unknown")
    return Observation(
        readiness,
        (summary, *capability_facts),
        data={
            "capabilities": results,
            "audit_state": (graph.get("audit") or {}).get("state", "unknown"),
            "report_hash": (graph.get("audit") or {}).get("hash"),
            "not_assessed": [
                "capability trust is not observed unless the caller supplies it",
                "the self-graph is a bounded static/runtime join; coverage is "
                "not exhaustive",
            ],
        },
        reason=reason,
    )


def environment_observation(
        context: ProviderContext, *,
        discover: Callable[..., list[dict[str, Any]]] | None = None,
        which: Callable[[str], str | None] | None = None,
        trust: bool | None = None,
        include_self: bool = False) -> Observation:
    """Interpreter candidates; nothing is executed or probed."""
    from ozagia.core import environments as ENV

    finder = discover or ENV.discover
    kwargs: dict[str, Any] = {
        "include_self": include_self,
        "cache_mode": "read",
        "lock_tools": False,
    }
    if which is not None:
        kwargs["which"] = which
    try:
        candidates = list(finder(context.checkout.root, **kwargs))
    except Exception as exc:  # noqa: BLE001 - unknown, never assumed present
        return Observation(
            "unknown",
            (fact("preflight.environment",
                  {"state": "unknown", "reason": type(exc).__name__},
                  source="ozagia.core.environments.discover",
                  freshness="unknown", evidence="inferred"),),
            data={"candidates": [], "selected": None, "probed": False},
            reason=_line(f"interpreter discovery failed: "
                         f"{type(exc).__name__}: {exc}", 300),
            error="unavailable",
        )
    trusted = ENV.trust_project_python(context.checkout.root) if trust is None \
        else bool(trust)
    rows: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        exists = False
        try:
            exists = (Path(path).is_file()
                      and os.access(path, os.X_OK))
        except OSError:
            exists = False
        project = bool(item.get("project"))
        allowed = exists and (trusted or not project)
        row = {
            "path": _text(path, 512),
            "source": _text(item.get("source") or "unknown", 120),
            "evidence": _text(item.get("evidence") or "", 200),
            "project": project,
            "trusted": trusted if project else True,
            "exists": exists,
        }
        rows.append(row)
        if allowed:
            eligible.append(row)
    selected = eligible[0]["path"] if eligible else None
    if not rows:
        readiness: bool | str = False
        reason = "no interpreter candidate was found"
    elif not eligible:
        readiness = "unknown"
        reason = ("only project-shipped interpreters were found and project "
                  "trust is not granted")
    else:
        readiness = True
        reason = f"selected interpreter {selected}"
    value = {
        "candidates": len(candidates),
        "eligible": len(eligible),
        "selected": selected,
        "probed": False,
    }
    return Observation(
        readiness,
        (fact("preflight.environment", value,
              source="ozagia.core.environments.discover",
              observed_at=context.observed_at, freshness="current",
              evidence="observed"),),
        data={
            "candidates": rows[:8],
            "selected": selected,
            "trusted_project_python": trusted,
            "probed": False,
            "not_assessed": [
                "interpreter importability and package availability were not "
                "probed",
                "a selected interpreter is a candidate, not proof that a test "
                "ran with it",
            ],
        },
        reason=reason,
    )


def resources_observation(
        context: ProviderContext, *,
        check: Callable[..., Mapping[str, Any]] | None = None,
        kind: str = "task") -> Observation:
    """Point-in-time resource admission; nothing is started."""
    from ozagia.core import resources as RES

    checker = check or RES.check_admission
    try:
        report = dict(checker(context.checkout.root, kind=kind))
    except Exception as exc:  # noqa: BLE001 - unknown, never assumed ready
        return Observation(
            "unknown",
            (fact("preflight.resources",
                  {"state": "unknown", "reason": type(exc).__name__},
                  source="ozagia.core.resources.check_admission",
                  freshness="unknown", evidence="inferred"),),
            data={"ok": None, "probed": False},
            reason=_line(f"resource admission failed: "
                         f"{type(exc).__name__}: {exc}", 300),
            error="unavailable",
        )
    ok = report.get("ok")
    disabled = bool(report.get("disabled"))
    readiness: bool | str = True if (ok is True or disabled) else False
    reason = report.get("reason")
    if disabled:
        reason = "resource checks are disabled"
    elif readiness is False:
        reason = f"resource admission refused: {reason or 'unknown reason'}"
    else:
        reason = "resource admission fits the host sample"
    value = {
        "ok": bool(ok) if ok is not None else None,
        "disabled": disabled,
        "reason": report.get("reason"),
        "requested": report.get("requested"),
        "available": report.get("available"),
        "retry_after_s": report.get("retry_after_s"),
        "checks": [check for check in report.get("checks") or []
                   if isinstance(check, Mapping)],
    }
    return Observation(
        readiness,
        (fact("preflight.resources", value,
              source="ozagia.core.resources.check_admission",
              observed_at=context.observed_at, freshness="current",
              evidence="observed"),),
        data={
            "ok": value["ok"],
            "disabled": disabled,
            "requested": _clean(report.get("requested")),
            "available": _clean(report.get("available")),
            "shortfall": _clean(report.get("shortfall")),
            "retry_after_s": report.get("retry_after_s"),
            "suggestions": _clean(report.get("suggestions")),
            "probed": False,
        },
        reason=reason,
    )


def tests_observation(
        context: ProviderContext, *,
        plan: Callable[..., Mapping[str, Any]] | None = None) -> Observation:
    """The affected-test plan; a plan is never a passed test."""
    from ozagia.core import testing as TESTING

    planner = plan or TESTING.plan
    args: dict[str, Any] = {"base": context.base or "HEAD"}
    if context.changed is not None:
        args["changed"] = context.changed
    try:
        result = dict(planner(context.checkout.root, **args))
    except Exception as exc:  # noqa: BLE001 - no plan is unknown, not a pass
        return Observation(
            "unknown",
            (fact("preflight.tests",
                  {"state": "unknown", "executed": False,
                   "reason": type(exc).__name__},
                  source="ozagia.core.testing.plan", freshness="unknown",
                  evidence="inferred"),),
            data={"executed": False, "plan_only": True, "selected": []},
            reason=_line(f"the test plan could not be built: "
                         f"{type(exc).__name__}: {exc}", 300),
            error="unavailable",
        )
    mode = str(result.get("mode") or "unknown")
    graph_freshness = str(result.get("graph_freshness") or "unknown")
    refused = bool(result.get("refused"))
    fallback = bool(result.get("fallback_used"))
    if refused:
        readiness: bool | str = False
        reason = "the test policy refused to select tests"
        freshness = "current"
    elif fallback:
        readiness = "unknown"
        reason = (result.get("fallback_reason")
                  or "the test selection fell back; affected coverage was not "
                     "assessed")
        freshness = "stale" if graph_freshness == "stale" else "unknown"
    elif mode in {"smoke", "affected", "package"} and graph_freshness != "fresh":
        readiness = "unknown"
        reason = f"the graph snapshot is {graph_freshness}"
        freshness = "stale" if graph_freshness == "stale" else "unknown"
    else:
        readiness = True
        reason = "a test plan was built; no test was run"
        freshness = "current"
    selected = [str(item) for item in result.get("selected_files") or []][:10]
    value = {
        "mode": mode,
        "requested_mode": result.get("requested_mode"),
        "selected": len(result.get("selected_files") or []),
        "omitted": int((result.get("counts") or {}).get("omitted") or 0),
        "executed": False,
        "claim": "test selection only; no test was executed",
        "graph_freshness": graph_freshness,
        "fallback_used": fallback,
        "refused": refused,
    }
    return Observation(
        readiness,
        (fact("preflight.tests", value, source="ozagia.core.testing.plan",
              observed_at=context.observed_at, freshness=freshness,
              evidence="observed"),),
        data={
            "mode": mode,
            "requested_mode": result.get("requested_mode"),
            "selected": len(result.get("selected_files") or []),
            "sample": selected,
            "omitted": int((result.get("counts") or {}).get("omitted") or 0),
            "graph_freshness": graph_freshness,
            "fallback_used": fallback,
            "refused": refused,
            "executed": False,
            "plan_only": True,
            "claim": "test selection only; no test was executed",
            "warnings": _clean(result.get("warnings") or [])[:6],
            "not_assessed": _clean(result.get("not_assessed") or [])[:8],
        },
        reason=reason,
    )


def _linked_graph_state(member: LinkedCheckout) -> dict[str, Any]:
    """Read a member's own ``<root>/.ozagia/graph.json`` without refreshing it.

    Unlike :func:`read_graph_state`, this deliberately ignores an absolute
    ``OZAGIA_STATE_DIR``: it belongs to the anchor.  A linked checkout is read
    from its own state directory, matching federation's membership rules.
    """
    path = member.root / ".ozagia" / "graph.json"
    info: dict[str, Any] = {
        "path": path.as_posix(),
        "state": "absent",
        "freshness": "absent",
        "snapshot_head": None,
        "head": None,
        "snapshot_root": None,
        "generated_at": None,
        "root_mismatch": False,
        "tree_changed": False,
        "errors": [],
        "observed_at": _mtime(path),
        "reason": "no graph snapshot in the linked checkout",
        "recorded_name": None,
    }
    if not member.exists:
        info["reason"] = "the linked checkout root does not exist"
        return info
    if member.symlinked:
        info.update({
            "state": "unknown",
            "freshness": "unknown",
            "reason": "a symlinked linked checkout is not read",
        })
        return info
    if not path.is_file():
        return info
    try:
        from ozagia.core.memory import DevMemory

        dev = DevMemory(dump=path, journal=member.root / ".ozagia" / "journal.jsonl",
                        root=member.root)
        staleness = dev.staleness()
        repo: dict[str, Any] = {}
        for node_id in dev.nodes_of_type("Repo"):
            node = dev.node(node_id) or {}
            attrs = node.get("attrs")
            if isinstance(attrs, Mapping):
                repo = dict(attrs)
                break
    except Exception as exc:  # noqa: BLE001 - a read failure is unknown
        info.update({
            "state": "error",
            "freshness": "unknown",
            "reason": _line(f"cannot read the linked graph snapshot: "
                            f"{type(exc).__name__}: {exc}", 300),
        })
        return info
    state = str(staleness.get("freshness") or "unknown")
    errors = [str(item) for item in staleness.get("verification_errors") or []]
    info.update({
        "state": state,
        "freshness": {"fresh": "current", "stale": "stale"}.get(
            state, "unknown"),
        "snapshot_head": _short(staleness.get("snapshot_head")),
        "head": _short(staleness.get("head")),
        "snapshot_root": staleness.get("snapshot_root"),
        "generated_at": _text(repo.get("generated_at"), 64) or None,
        "root_mismatch": bool(staleness.get("root_mismatch")),
        "tree_changed": bool(staleness.get("tree_changed")),
        "errors": errors[:4],
        "recorded_name": _text(repo.get("name"), 128) or None,
        "observed_at": _mtime(path),
    })
    if state == "fresh":
        info["reason"] = "snapshot root, HEAD and working-tree digest match"
    elif state == "stale":
        details = []
        if info["root_mismatch"]:
            details.append("snapshot root differs")
        if info["tree_changed"]:
            details.append("working tree changed since the snapshot")
        if (staleness.get("snapshot_head")
                and staleness.get("snapshot_head") != staleness.get("head")):
            details.append("HEAD moved since the snapshot")
        info["reason"] = _line("; ".join(details) or "snapshot is stale", 300)
    else:
        info["reason"] = _line("; ".join(errors)
                               or "graph freshness could not be verified", 300)
    return info


def _reference_index(anchor: Path) -> dict[str, Any]:
    from ozagia.config import state_path

    manifest = state_path(anchor, "federation.json")
    try:
        from ozagia.core import federation as FED

        report = FED.reference_status(anchor)
    except Exception as exc:  # noqa: BLE001 - unknown, never assumed synced
        return {
            "manifest": manifest.as_posix(),
            "entries": {},
            "declared": 0,
            "errors": [_line(f"cannot read the reference manifest: "
                             f"{type(exc).__name__}: {exc}", 300)],
            "source": "ozagia.core.federation.reference_status",
            "synced": False,
        }
    entries: dict[str, dict[str, Any]] = {}
    for item in report.get("references") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            continue
        try:
            key = Path(item["path"]).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        entries[key.as_posix()] = dict(item)
    errors = [str(item) for item in report.get("errors") or [] if item]
    return {
        "manifest": manifest.as_posix(),
        "entries": entries,
        "declared": len(entries),
        "errors": errors[:6],
        "source": "ozagia.core.federation.reference_status",
        "synced": False,
    }


def _linked_reference_state(member: LinkedCheckout, reference: Mapping[str, Any]
                            ) -> dict[str, Any]:
    if reference.get("errors"):
        return {
            "role": "unknown",
            "state": "unknown",
            "pin": None,
            "updated": False,
            "cached": False,
            "checked_at": None,
            "freshness": "unknown",
            "evidence": "declared",
            "reason": "the federation manifest is invalid",
        }
    entry = (reference.get("entries") or {}).get(member.root.as_posix())
    if entry is None:
        return {
            "role": "member_only",
            "state": "not_declared",
            "pin": None,
            "updated": False,
            "cached": False,
            "checked_at": None,
            "freshness": "current",
            "evidence": "observed",
            "reason": "the member is selected but not a declared reference",
        }
    state = str(entry.get("state") or "not_checked")
    sync = entry.get("sync") if isinstance(entry.get("sync"), Mapping) else {}
    if state == "updated" or (sync or {}).get("index_state") == "stale_until_reindexed":
        freshness = "stale"
        reason = "a reference update left the snapshot stale until reindexed"
    elif state in {"current", "pinned"}:
        freshness = "current"
        reason = "the cached reference state is current"
    elif state == "error":
        freshness = "unknown"
        reason = "the last reference sync reported an error"
    elif state == "pending":
        freshness = "unknown"
        reason = "a reference sync is pending"
    else:
        freshness = "unknown"
        reason = "reference sync state is not verified"
    return {
        "role": "declared_reference",
        "state": state,
        "pin": entry.get("pin"),
        "updated": bool((sync or {}).get("updated")),
        "cached": bool((sync or {}).get("cached")),
        "checked_at": (sync or {}).get("checked_at"),
        "freshness": freshness,
        "evidence": "declared",
        "reason": reason,
    }


def _linked_member_row(member: LinkedCheckout, reference: Mapping[str, Any],
                       observed_at: int | str) -> tuple[dict[str, Any], list[C.Fact]]:
    index = _linked_graph_state(member)
    auth = {
        "state": member.authorized,
        "read_scope": list(member.read_scope),
        "scope_state": "declared" if member.read_scope else "unspecified",
        "source": "caller",
        "evidence": "declared",
    }
    if member.authorized is False:
        auth["readiness"] = False
        auth["freshness"] = "absent"
        auth["reason"] = "the caller marked the linked checkout unauthorized"
    elif member.authorized is not True:
        auth["readiness"] = "unknown"
        auth["freshness"] = "unknown"
        auth["reason"] = "read authorization was not declared"
    elif not member.read_scope:
        auth["readiness"] = "unknown"
        auth["freshness"] = "unknown"
        auth["reason"] = "no read scope was declared"
    else:
        auth["readiness"] = True
        auth["freshness"] = "current"
        auth["reason"] = "the caller declared read authorization and scope"
    ref = _linked_reference_state(member, reference)
    freshness = C.join_freshness(index["freshness"], auth["freshness"])
    freshness = C.join_freshness(freshness, ref["freshness"])
    if auth["readiness"] is False:
        readiness: bool | str = False
        reason = auth["reason"]
    elif auth["readiness"] is not True:
        readiness = "unknown"
        reason = auth["reason"]
    elif index["state"] != "fresh":
        readiness = "unknown"
        reason = index["reason"]
    elif ref["freshness"] != "current":
        readiness = "unknown"
        reason = ref["reason"]
    else:
        readiness = True
        reason = "authorized, current and indexed"
    identity = {
        "repo": member.repo or "unknown",
        "checkout": member.checkout or "unknown",
        "snapshot": member.snapshot or "unknown",
        "recorded": {
            "name": index["recorded_name"],
            "root": index["snapshot_root"],
            "head": index["snapshot_head"],
            "generated_at": index["generated_at"],
        },
        "source": ("caller" if member.repo or member.checkout or member.snapshot
                   else "snapshot" if index["recorded_name"] else "unknown"),
        "freshness": ("current" if index["state"] in {"fresh", "stale"}
                      else "unknown"),
        "evidence": ("observed" if index["recorded_name"] else "declared"),
    }
    row = {
        **member.identity(),
        "identity": identity,
        "authorization": auth,
        "reference": ref,
        "index": index,
        "freshness": freshness,
        "evidence": ("observed" if index["state"] in {"fresh", "stale"}
                     else "inferred"),
        "readiness": readiness,
        "reason": reason,
    }
    facts = [
        fact(f"preflight.linked.identity.{member.label}",
             {"repo": identity["repo"], "checkout": identity["checkout"],
              "snapshot": identity["snapshot"],
              "recorded": identity["recorded"]},
             source=("ozagia.core.memory.DevMemory" if index["recorded_name"]
                     else "ozagia.core.task_preflight"),
             observed_at=index["observed_at"], freshness=identity["freshness"],
             evidence=identity["evidence"]),
        fact(f"preflight.linked.authorization.{member.label}",
             {"authorized": auth["state"], "read_scope": auth["read_scope"],
              "scope_state": auth["scope_state"], "reason": auth["reason"]},
             source="ozagia.core.task_preflight", observed_at=observed_at,
             freshness=auth["freshness"], evidence="declared"),
        fact(f"preflight.linked.reference.{member.label}",
             {"role": ref["role"], "state": ref["state"], "pin": ref["pin"],
              "updated": ref["updated"], "cached": ref["cached"],
              "checked_at": ref["checked_at"], "reason": ref["reason"]},
             source=str(reference.get("source") or
                        "ozagia.core.federation.reference_status"),
             observed_at=observed_at, freshness=ref["freshness"],
             evidence=ref["evidence"]),
        fact(f"preflight.linked.index.{member.label}",
             {"state": index["state"], "snapshot_head": index["snapshot_head"],
              "head": index["head"], "generated_at": index["generated_at"],
              "root_mismatch": index["root_mismatch"],
              "tree_changed": index["tree_changed"],
              "errors": index["errors"], "reason": index["reason"]},
             source="ozagia.core.memory.DevMemory",
             observed_at=index["observed_at"], freshness=index["freshness"],
             evidence=("observed" if index["state"] in {"fresh", "stale", "error"}
                       else "inferred")),
    ]
    return row, facts


def linked_observation(context: ProviderContext) -> Observation:
    """Explicitly selected linked checkouts: identity, access, sync and index.

    This reads the anchor's reference *cache* and each member's existing graph
    snapshot.  It never runs reference sync, Git fetch, reindex, network I/O or
    any linked-project code.
    """
    members = tuple(context.linked)
    reference = _reference_index(context.checkout.root)
    rows: list[dict[str, Any]] = []
    facts: list[C.Fact] = []
    for member in members:
        row, member_facts = _linked_member_row(
            member, reference, context.observed_at)
        rows.append(row)
        facts.extend(member_facts)
    readiness = _aggregate(row["readiness"] for row in rows)
    counts = {
        "members": len(rows),
        "ready": sum(row["readiness"] is True for row in rows),
        "unknown": sum(row["readiness"] == "unknown" for row in rows),
        "failed": sum(row["readiness"] is False for row in rows),
        "stale": sum(row["index"]["state"] == "stale" for row in rows),
        "absent": sum(row["index"]["state"] == "absent" for row in rows),
        "unauthorized": sum(row["authorization"]["state"] is False
                            for row in rows),
    }
    if not rows:
        reason = "no linked checkout was explicitly selected"
        readiness = "unknown"
    else:
        reason = next((row["reason"] for row in rows
                       if row["readiness"] != readiness),
                      rows[0]["reason"])
    summary = fact(
        "preflight.linked",
        {"members": len(rows), "readiness": readiness, "counts": counts,
         "reference_synced": False},
        source="ozagia.core.federation.reference_status",
        observed_at=context.observed_at,
        freshness=("current" if readiness is True else
                   "absent" if readiness is False else "unknown"),
        evidence="observed" if any(row["evidence"] == "observed" for row in rows)
        else "inferred",
    )
    return Observation(
        readiness,
        (summary, *facts),
        data={
            "anchor": context.checkout.root.as_posix(),
            "selection": "explicit",
            "members": rows,
            "counts": counts,
            "reference": {
                "manifest": reference["manifest"],
                "declared": reference["declared"],
                "errors": reference["errors"],
                "source": reference["source"],
                "synced": False,
            },
            "provenance": {
                "reader": "ozagia.core.task_preflight.linked",
                "anchor": context.checkout.root.as_posix(),
                "manifest": reference["manifest"],
                "selection": "explicit",
                "observed_at": context.observed_at,
                "sources": [
                    "ozagia.core.task_preflight",
                    "ozagia.core.memory.DevMemory",
                    "ozagia.core.federation.reference_status",
                ],
                "synced": False,
            },
            "not_assessed": [
                "linked checkout authorization and read scope are caller "
                "declarations, not an observed ACL",
                "reference sync state is the existing cached status; no Git "
                "fetch, network call or reference sync was performed",
                "linked snapshots were read and staleness-checked but not "
                "refreshed or reindexed",
                "cross-repository semantic links and API compatibility were not "
                "evaluated",
                "Ocelot is not connected; its absence is an explicit unknown",
            ],
            "deferred": [
                "reference synchronization",
                "linked checkout mutation",
                "linked snapshot reindex",
                "cross-repository API/code/IaC checks",
            ],
            "omissions": [],
            "truncated": C.truncation().to_dict(),
        },
        reason=reason,
    )


def research_observation(context: ProviderContext) -> Observation:
    """Retained research records as declared facts; never live research I/O."""
    from ozagia.core import research_projection as RP

    try:
        projection = RP.build_projection(
            context.checkout.root,
            sessions=context.research_sessions,
            observed_at=context.observed_at,
            verify_sources=True,
        )
    except Exception as exc:  # noqa: BLE001 - unknown, never assumed green
        return Observation(
            "unknown",
            (fact("preflight.research",
                  {"state": "unknown", "reason": type(exc).__name__},
                  source="ozagia.core.research_projection",
                  freshness="unknown", evidence="inferred"),),
            data={"sessions": [], "claims": [], "sources": [],
                  "assumptions": [], "evidence": [],
                  "engines": {"ocelot": {
                      "state": "unknown",
                      "reason": "the Ocelot adapter is deferred and was not "
                                "imported or executed"}},
                  "not_assessed": [
                      "research projection failed; no claim or source was read",
                  ]},
            reason=_line(f"research projection failed: "
                         f"{type(exc).__name__}: {exc}", 300),
            error="unavailable",
        )
    join = RP.preflight_join(projection)
    facts = RP.projection_facts(projection, limit=RP.MAX_FACTS)
    readiness = join.get("readiness", "unknown")
    reason = str(join.get("reason") or "research records are declarations")
    return Observation(
        readiness,
        facts,
        data=join,
        reason=reason,
    )


def actions_observation(context: ProviderContext) -> Observation:
    """Explicitly selected service/tunnel action readiness; read-only.

    The join reads configuration, the existing runtime status and log
    filesystem metadata.  It never previews, applies, resolves a secret,
    creates a token/audit/runtime record or starts/stops/signals anything.
    """
    from ozagia.core import action_readiness as AR

    selections = context.action_readiness or ()
    try:
        report = AR.readiness(
            context.checkout.root, selections=selections,
            observed_at=context.observed_at)
    except Exception as exc:  # noqa: BLE001 - unknown, never assumed green
        return Observation(
            "unknown",
            (fact(
                "preflight.actions",
                {"state": "unknown", "reason": type(exc).__name__},
                source="ozagia.core.action_readiness",
                freshness="unknown", evidence="inferred"),),
            data={
                "selection": "explicit",
                "state": "unknown",
                "operations": [],
                "counts": {},
                "not_assessed": [
                    "action readiness failed before any operation was observed",
                ],
            },
            reason=_line(f"action readiness failed: "
                         f"{type(exc).__name__}: {exc}", 300),
            error="unavailable",
        )
    state = str(report.get("state") or AR.UNKNOWN)
    readiness: bool | str = (
        True if state == AR.AVAILABLE else
        False if state == AR.ABSENT else "unknown")
    facts = []
    for row in report.get("operations") or []:
        row_state = str(row.get("readiness") or AR.UNKNOWN)
        facts.append(fact(
            f"action.{row.get('action')}.{row.get('target') or '*'}",
            {
                "readiness": row_state,
                "reason": row.get("reason"),
                "mutating": bool(row.get("mutating")),
                "live_gated": bool(row.get("live_gated")),
            },
            source="ozagia.core.action_readiness",
            observed_at=context.observed_at,
            freshness=("current" if row_state == AR.AVAILABLE else
                       "absent" if row_state == AR.ABSENT else "unknown"),
            evidence=("observed" if row_state in {AR.AVAILABLE, AR.ABSENT}
                      else "inferred"),
        ))
    facts.insert(0, fact(
        "preflight.actions",
        {
            "state": state,
            "counts": dict(report.get("counts") or {}),
            "secrets_resolved": False,
        },
        source="ozagia.core.action_readiness",
        observed_at=context.observed_at,
        freshness=("current" if state == AR.AVAILABLE else
                   "absent" if state == AR.ABSENT else "unknown"),
        evidence=("observed" if state in {AR.AVAILABLE, AR.ABSENT}
                  else "inferred"),
    ))
    return Observation(
        readiness,
        tuple(facts),
        data=report,
        reason=_line(report.get("reason") or "action readiness was observed",
                     400),
        error=report.get("error"),
    )


def default_providers(
        *, graph: Provider = graph_observation,
        goal: Provider = goal_observation,
        capabilities: Provider = capability_observation,
        environment: Provider = environment_observation,
        resources: Provider = resources_observation,
        tests: Provider = tests_observation,
        linked: Provider = linked_observation,
        research: Provider = research_observation,
        actions: Provider = actions_observation) -> Providers:
    """The read-only default provider set (individual callables injectable)."""
    return Providers(graph=graph, goal=goal, capabilities=capabilities,
                     environment=environment, resources=resources, tests=tests,
                     linked=linked, research=research, actions=actions)


__all__ = [
    "ACTIONS_DIMENSION", "ASSESSMENTS", "DEFAULT_CAPABILITIES",
    "DEFAULT_LIMIT", "DEFAULT_TIMEOUT_MS", "DEFERRED", "DIMENSIONS",
    "LINKED_DIMENSION", "MAX_ACTION_SELECTIONS", "MAX_CHECKOUTS",
    "MAX_CRITERIA", "MAX_FACTS", "MAX_LIMIT", "MAX_LINKED",
    "MAX_REPORT_BYTES", "MAX_RESEARCH_SESSIONS", "MAX_TIMEOUT_MS",
    "OPTIONAL_DIMENSIONS", "Observation", "PreflightError", "Provider",
    "ProviderContext", "Providers", "REPORT_VERSION", "REQUIRED_DIMENSIONS",
    "RESEARCH_DIMENSION", "SCHEMA_NAMESPACE", "SCHEMA_VERSION",
    "SAFE_GOAL_KINDS", "TEST_GOAL_KINDS", "UNSAFE_GOAL_KINDS", "Checkout",
    "LinkedCheckout", "actions_observation", "build_preflight",
    "capability_observation", "default_providers",
    "environment_observation", "fact", "goal_identity",
    "goal_observation", "graph_observation", "linked_observation",
    "preflight_envelope", "read_graph_state", "report_hash", "report_json",
    "research_observation", "resources_observation", "tests_observation",
]
