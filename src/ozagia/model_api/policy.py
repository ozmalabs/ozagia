"""Host-owned policy checks for model map requests.

The model never supplies authority.  A :class:`HostPolicy` is constructed by
the host adapter (the MCP server, a CLI caller or a test) and is the only
source of truth for the allowed root, session/model labels, map types, path
prefixes, representation and budgets.  A request may *request* a smaller
budget or a narrower path scope, never a larger or wider one.

This module is pure except for resolving the host root with
``Path.resolve(strict=True)``; it does not read project files, start processes,
call models or write state.  The host's existing authorization remains
authoritative after a map is returned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ozagia.core import contracts as C
from ozagia.model_api import schema
from ozagia.model_api.errors import ModelAPIError

MAX_LABEL = 128
_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


def _fail(message: str, code: str, reason: str) -> ModelAPIError:
    return ModelAPIError(message, code=code, reason=reason)


def _label(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{name} must be a string", "invalid_request",
                    "malformed_scope")
    text = value.strip()
    if not text or len(text) > MAX_LABEL or not _LABEL_RE.fullmatch(text):
        raise _fail(f"{name} is not a bounded label", "invalid_request",
                    "malformed_scope")
    return text


def _repo_identity(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = C.parse_id(value)
    except C.ContractError as exc:
        raise _fail("policy repo must be a repo: identity", "invalid_request",
                    "malformed_scope") from exc
    if parsed.kind != "repo":
        raise _fail("policy repo must be a repo: identity", "invalid_request",
                    "malformed_scope")
    return parsed.canonical


def _normalise_path(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"{name} must be a non-empty path", "invalid_request",
                    "malformed_scope")
    text = value.strip()
    if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        raise _fail(f"{name} must be repository-relative", "forbidden",
                    "cross_scope")
    parts = text.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _fail(f"{name} is not a normalised relative path", "forbidden",
                    "cross_scope")
    return "/".join(parts)


@dataclass(frozen=True)
class HostPolicy:
    """The host's authoritative read scope for one model/session.

    ``allowed_models`` and ``allowed_sessions`` are additional labels the same
    host permits.  They exist so a host can serve a model switch without a new
    process; they are not inferred from request text.  The cache is still
    partitioned by the effective label.
    """

    root: Path
    session: str
    model: str
    repo: str | None = None
    allowed_models: tuple[str, ...] = ()
    allowed_sessions: tuple[str, ...] = ()
    allowed_maps: tuple[str, ...] = schema.MAP_TYPES
    allowed_paths: tuple[str, ...] = ()
    allow_structured: bool = True
    allow_cache: bool = True
    max_bytes: int = schema.HARD_MAX_BYTES
    max_items: int = schema.HARD_MAX_ITEMS
    max_depth: int = schema.HARD_MAX_DEPTH
    snapshot_path: Path | None = None

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser()
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise _fail("host policy root does not exist", "invalid_request",
                        "policy_root_missing") from exc
        if not resolved.is_dir():
            raise _fail("host policy root is not a directory", "invalid_request",
                        "policy_root_missing")
        object.__setattr__(self, "root", resolved)
        object.__setattr__(self, "session", _label(self.session, "session"))
        object.__setattr__(self, "model", _label(self.model, "model"))
        object.__setattr__(self, "repo", _repo_identity(self.repo))
        for name in ("allowed_models", "allowed_sessions"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)):
                raise _fail(f"{name} must be a tuple of labels",
                            "invalid_request", "malformed_scope")
            cleaned = tuple(sorted({_label(value, name) for value in values}))
            object.__setattr__(self, name, cleaned)
        maps = tuple(self.allowed_maps)
        if not maps or any(item not in schema.MAP_TYPES for item in maps):
            raise _fail("allowed_maps contains an unknown map type",
                        "invalid_request", "unsupported_map")
        object.__setattr__(self, "allowed_maps", tuple(sorted(set(maps))))
        paths = tuple(sorted({_normalise_path(path, "allowed_paths")
                              for path in self.allowed_paths}))
        object.__setattr__(self, "allowed_paths", paths)
        if self.allow_structured is not True and self.allow_structured is not False:
            raise _fail("allow_structured must be a bool", "invalid_request",
                        "malformed_request")
        if self.allow_cache is not True and self.allow_cache is not False:
            raise _fail("allow_cache must be a bool", "invalid_request",
                        "malformed_request")
        for name, hard in (("max_bytes", schema.HARD_MAX_BYTES),
                           ("max_items", schema.HARD_MAX_ITEMS),
                           ("max_depth", schema.HARD_MAX_DEPTH)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise _fail(f"{name} must be an integer", "invalid_request",
                            "malformed_budget")
            if value < schema.DEFAULT_BUDGETS[name] or value > hard:
                raise _fail(
                    f"{name} must be between the v1 default "
                    f"{schema.DEFAULT_BUDGETS[name]} and the hard bound {hard}",
                    "budget_exceeded", "budget_over_hard_bound")
        if self.snapshot_path is not None:
            path = Path(self.snapshot_path).expanduser()
            if not path.is_absolute():
                path = resolved / path
            object.__setattr__(self, "snapshot_path", path)

    @property
    def policy_hash(self) -> str:
        """A redacted fingerprint of the host decision (no file content)."""
        return C.content_hash({
            "root": str(self.root),
            "session": self.session,
            "model": self.model,
            "repo": self.repo,
            "allowed_models": list(self.allowed_models),
            "allowed_sessions": list(self.allowed_sessions),
            "allowed_maps": list(self.allowed_maps),
            "allowed_paths": list(self.allowed_paths),
            "allow_structured": self.allow_structured,
            "allow_cache": self.allow_cache,
            "max_bytes": self.max_bytes,
            "max_items": self.max_items,
            "max_depth": self.max_depth,
            "snapshot_path": (str(self.snapshot_path)
                              if self.snapshot_path is not None else None),
        })

    def _model_allowed(self, label: str) -> bool:
        return label == self.model or label in self.allowed_models

    def _session_allowed(self, label: str) -> bool:
        return label == self.session or label in self.allowed_sessions


@dataclass(frozen=True)
class PolicyDecision:
    """The validated effective scope for one request."""

    map_type: str
    root: str
    session: str
    model: str
    repo: str | None
    repo_state: str
    requested_repo: str | None
    requested_checkout: str | None
    requested_snapshot: str | None
    paths: tuple[str, ...]
    budgets: dict[str, int] = field(default_factory=dict)
    format: str = "structured"
    cache_enabled: bool = True
    allow_structured: bool = True
    fallback_reason: str | None = None
    policy_hash: str = ""

    def scope(self) -> dict[str, Any]:
        scope: dict[str, Any] = {
            "root": self.root,
            "session": self.session,
            "model": self.model,
        }
        if self.repo is not None:
            scope["repo"] = self.repo
        if self.requested_checkout is not None:
            scope["checkout"] = self.requested_checkout
        if self.requested_snapshot is not None:
            scope["snapshot"] = self.requested_snapshot
        if self.paths:
            scope["paths"] = list(self.paths)
        return scope


def decide(policy: HostPolicy, request: Mapping[str, Any]) -> PolicyDecision:
    """Apply host policy to a validated request.

    Checks are fail-closed: a different root, model or session, a map the host
    did not enable, an out-of-policy budget or a path outside the allowed
    prefixes is refused rather than clamped or silently narrowed.
    """
    map_type = str(request["map"])
    if map_type not in policy.allowed_maps:
        raise _fail(f"host policy does not allow the {map_type} map",
                    "forbidden", "map_not_allowed")
    scope = request.get("scope") or {}
    session = str(scope.get("session") or policy.session)
    model = str(scope.get("model") or policy.model)
    if not policy._session_allowed(session):
        raise _fail("request session is not allowed by host policy",
                    "forbidden", "session_not_allowed")
    if not policy._model_allowed(model):
        raise _fail("request model is not allowed by host policy",
                    "forbidden", "model_not_allowed")

    root = policy.root
    requested_root = scope.get("root")
    if requested_root is not None:
        try:
            candidate = Path(str(requested_root)).expanduser().resolve(
                strict=False)
        except OSError as exc:
            raise _fail("request root cannot be resolved", "invalid_request",
                        "malformed_scope") from exc
        if candidate != root:
            raise _fail("request root is outside the host policy scope",
                        "forbidden", "cross_scope")

    requested_repo = scope.get("repo")
    if requested_repo is not None and policy.repo is not None \
            and requested_repo != policy.repo:
        raise _fail("request repo does not match the host policy repo",
                    "forbidden", "cross_scope")
    repo = policy.repo
    repo_state = "observed" if policy.repo is not None else "unknown"

    requested_checkout = scope.get("checkout")
    requested_snapshot = scope.get("snapshot")
    if requested_snapshot is not None and requested_checkout is not None:
        parsed_snapshot = C.parse_id(requested_snapshot)
        parsed_checkout = C.parse_id(requested_checkout)
        if parsed_snapshot.components[:3] != parsed_checkout.components:
            raise _fail("requested snapshot does not belong to the checkout",
                        "invalid_request", "snapshot_mismatch")

    paths = tuple(scope.get("paths") or ())
    if policy.allowed_paths:
        for path in paths:
            if not any(path == prefix or path.startswith(prefix + "/")
                       for prefix in policy.allowed_paths):
                raise _fail(f"path {path!r} is outside the allowed path scope",
                            "forbidden", "cross_scope")
        if not paths:
            paths = tuple(policy.allowed_paths)

    budgets: dict[str, int] = {}
    for name in schema.DEFAULT_BUDGETS:
        requested_value = int(request["budgets"][name])
        limit = getattr(policy, name)
        if requested_value > limit:
            raise _fail(f"budget {name}={requested_value} exceeds host policy "
                        f"limit {limit}", "budget_exceeded",
                        "budget_over_policy")
        budgets[name] = requested_value

    requested_format = str(request.get("format") or "structured")
    effective_format = requested_format
    fallback_reason = None
    if requested_format == "structured" and not policy.allow_structured:
        effective_format = "plain"
        fallback_reason = "policy_disallows_structured"

    return PolicyDecision(
        map_type=map_type,
        root=str(root),
        session=session,
        model=model,
        repo=repo,
        repo_state=repo_state,
        requested_repo=requested_repo,
        requested_checkout=requested_checkout,
        requested_snapshot=requested_snapshot,
        paths=paths,
        budgets=budgets,
        format=effective_format,
        cache_enabled=policy.allow_cache,
        allow_structured=policy.allow_structured,
        fallback_reason=fallback_reason,
        policy_hash=policy.policy_hash,
    )


__all__ = ["HostPolicy", "PolicyDecision", "decide"]
