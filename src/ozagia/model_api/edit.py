"""Bounded structured edit proposals for the model-facing API (M4).

This module is the **only** M4 edit entry point.  A model may return one
structured proposal for one supported Python operation (``replace_body``).
The host parses it deterministically, checks the schema version, graph node
identity, repository scope, host policy, snapshot identity and the file's
base hash, and then translates it into the existing
:mod:`ozagia.edit.code_edit` / :mod:`ozagia.edit.txn` transaction path.

Properties frozen here
----------------------

* **One operation, one file, one symbol.**  The only accepted operation is
  ``replace_body`` on a canonical ``sym:<rel>::<qualname>`` node key.  A
  proposal cannot name a line range, a diff, a patch, a command, another
  language, an edit engine or a second file.
* **Preview by default.**  ``handle_edit`` previews unless the caller asks to
  apply.  Applying also needs a host-constructed :class:`EditAuthorization`
  and the exact ``preview_hash`` returned by the preview.  The transaction
  layer's base-hash check, atomic replace and backup are unchanged.
* **No authority from the model.**  Session/model/root/path/snapshot checks
  are host policy; a proposal can narrow or match them, never widen them.
  The response always says ``authority.granted=false`` even after a host
  applies the edit.
* **No text-patch fallback and no command execution.**  The parsed body is
  only source text passed to the existing Python edit adapter.  It is never
  evaluated, imported or executed, and a model-supplied command/field is
  refused as an unknown field.
* **Bounded input.**  The proposal is at most 64 KiB of canonical JSON, at
  most depth 8 and 512 nodes, with a 16 KiB function body.  Unknown
  non-``x-`` fields, floats, duplicate JSON keys and oversized values fail
  closed.
* **Idempotent apply.**  A successful apply is recorded under
  ``<root>/.ozagia/model_api/actions/`` keyed by the proposal's
  ``action_id``.  Replaying the same action/proposal returns the recorded
  result without writing again; reusing the id for a different proposal is
  a conflict.
* **Evidence is not a verdict.**  :func:`action_evidence` joins the applied
  action and its selected test keys to the read-only
  :mod:`ozagia.core.evidence` collector.  A model claim is recorded as
  ``declared``; it never becomes ``passed``.  A test record only counts when
  it was recorded at or after the action and the action's file is still at
  its recorded new hash; even then the comparison is a recorded result, not
  proof that the edited bytes were executed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.core import contracts as C
from ozagia.core import evidence as E
from ozagia.core import keys as K
from ozagia.edit import code_edit
from ozagia.edit import txn
from ozagia.model_api import policy as policy_mod
from ozagia.model_api import schema as schema_mod
from ozagia.model_api.errors import ModelAPIError, error_payload, internal_error

PROPOSAL_NAMESPACE = "ozagia.model_api.edit_proposal"
PROPOSAL_VERSION = "1.0"
ACTION_NAMESPACE = "ozagia.model_api.edit_action"
ACTION_VERSION = "1.0"
EVIDENCE_NAMESPACE = "ozagia.model_api.action_evidence"
EVIDENCE_VERSION = "1.0"
SUPPORTED_OPERATIONS = ("replace_body",)

MAX_PROPOSAL_BYTES = 64 * 1024
MAX_PROPOSAL_DEPTH = 8
MAX_PROPOSAL_NODES = 512
MAX_BODY_BYTES = 16 * 1024
MAX_DIFF_BYTES = 16 * 1024
MAX_ACTION_ID = 128
MAX_CHECKS = 16
MAX_CLAIMS = 8
MAX_CLAIM_TEXT = 300
MAX_REASON = 300
MAX_SCOPE_TEXT = 4096
MAX_LEDGER_ENTRIES = 128
MAX_LEDGER_BYTES = 64 * 1024
MAX_FILE_BYTES = code_edit.MAX_SOURCE_BYTES

_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TEST_RE = re.compile(r"^test:[^\s]{1,299}$")
_LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LONE_CR_RE = re.compile(r"\r(?!\n)")

_PROPOSAL_FIELDS = (
    "schema", "proposal_version", "action_id", "node", "operation", "body",
    "base_sha256", "snapshot", "scope", "checks", "claims",
)
_NODE_FIELDS = ("kind", "key")
_SCOPE_FIELDS = ("root", "session", "model", "repo", "checkout")


def _fail(message: str, code: str, reason: str,
          details: Mapping[str, Any] | None = None) -> ModelAPIError:
    return ModelAPIError(message, code=code, reason=reason, details=details)


def _line(value: Any, limit: int = MAX_REASON) -> str:
    text = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    return text[:limit]


def _label(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{name} must be a string", "invalid_request",
                    "malformed_proposal")
    text = value.strip()
    if not text or len(text) > 128 or not _LABEL_RE.fullmatch(text):
        raise _fail(f"{name} is not a bounded label", "invalid_request",
                    "malformed_proposal")
    return text


def _unknown_fields(record: Mapping[str, Any], allowed: Iterable[str],
                    *, what: str) -> None:
    permitted = set(allowed)
    for key in record:
        if not isinstance(key, str):
            raise _fail(f"{what} keys must be strings", "invalid_request",
                        "malformed_proposal")
        if key in permitted or key.startswith(C.EXTENSION_PREFIX):
            continue
        raise _fail(f"{what} has unknown field {key!r}", "unknown_field",
                    "malformed_proposal")


def _normalise_rel(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"{name} must be a non-empty relative path",
                    "invalid_request", "malformed_proposal")
    text = value.strip()
    if text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text):
        raise _fail(f"{name} must be repository-relative", "forbidden",
                    "path_escape")
    parts = text.replace("\\", "/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise _fail(f"{name} is not a normalised relative path", "forbidden",
                    "path_escape")
    normalised = "/".join(parts)
    if len(normalised) > 512:
        raise _fail(f"{name} is too long", "invalid_request",
                    "malformed_proposal")
    if _CONTROL_RE.search(normalised):
        raise _fail(f"{name} contains control characters", "invalid_request",
                    "malformed_proposal")
    return normalised


def _identity(value: Any, kind: str, name: str) -> str:
    if not isinstance(value, str):
        raise _fail(f"{name} must be a string", "invalid_request",
                    "malformed_proposal")
    text = value.strip()
    if (not text or len(text) > C.MAX_ID_LENGTH
            or _CONTROL_RE.search(text)):
        raise _fail(f"{name} is not a bounded identity", "invalid_request",
                    "malformed_proposal")
    try:
        parsed = C.parse_id(text)
    except C.ContractError as exc:
        raise _fail(f"{name} is not a {kind} identity", "invalid_request",
                    "malformed_proposal") from exc
    if parsed.kind != kind:
        raise _fail(f"{name} must be a {kind} identity", "invalid_request",
                    "malformed_proposal")
    return parsed.canonical


def _path_allowed(policy: policy_mod.HostPolicy, rel: str) -> bool:
    if not policy.allowed_paths:
        return True
    return any(rel == prefix or rel.startswith(prefix + "/")
               for prefix in policy.allowed_paths)


# ---------------------------------------------------------------------------
# Deterministic parsing and shape limits
# ---------------------------------------------------------------------------


class _DuplicateKey(ValueError):
    pass


class _FloatKey(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _reject_float(_text: str) -> None:
    raise _FloatKey("floats are not accepted in an edit proposal")


def _shape(value: Any, *, depth: int = 0, count: list[int] | None = None) -> None:
    if count is None:
        count = [0]
    count[0] += 1
    if count[0] > MAX_PROPOSAL_NODES:
        raise _fail("proposal exceeds the node bound", "budget_exceeded",
                    "proposal_over_budget")
    if depth > MAX_PROPOSAL_DEPTH:
        raise _fail("proposal exceeds the depth bound", "budget_exceeded",
                    "proposal_over_budget")
    if isinstance(value, Mapping):
        for item in value.values():
            _shape(item, depth=depth + 1, count=count)
    elif isinstance(value, list):
        for item in value:
            _shape(item, depth=depth + 1, count=count)


def _load_payload(payload: Any) -> Mapping[str, Any]:
    """Parse a mapping, JSON string or UTF-8 bytes with strict bounds."""
    try:
        if isinstance(payload, Mapping):
            text = C.canonical_json(payload)
            data = text.encode("utf-8")
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            data = bytes(payload)
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            raise _fail("an edit proposal must be a mapping or JSON text",
                        "invalid_request", "malformed_proposal")
    except ModelAPIError:
        raise
    except C.ContractError as exc:
        raise _fail(f"the proposal is not canonical JSON ({exc.code})",
                    "invalid_value", "proposal_schema_mismatch") from exc
    except UnicodeEncodeError as exc:
        raise _fail("the proposal is not valid UTF-8", "invalid_value",
                    "malformed_proposal") from exc
    if len(data) > MAX_PROPOSAL_BYTES:
        raise _fail(f"the proposal exceeds {MAX_PROPOSAL_BYTES} bytes",
                    "budget_exceeded", "proposal_over_budget")
    if data.startswith(b"\xef\xbb\xbf"):
        raise _fail("the proposal must not carry a UTF-8 BOM", "invalid_value",
                    "malformed_proposal")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail("the proposal is not valid UTF-8", "invalid_value",
                    "malformed_proposal") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except (json.JSONDecodeError, _DuplicateKey, _FloatKey, ValueError,
            RecursionError) as exc:
        raise _fail("the proposal is not deterministic JSON", "invalid_request",
                    "malformed_proposal") from exc
    if not isinstance(raw, Mapping):
        raise _fail("the proposal must be a JSON object", "invalid_request",
                    "malformed_proposal")
    _shape(raw)
    return raw


def _schema_ref(raw: Mapping[str, Any], *,
                namespace: str) -> None:
    ref = raw.get("schema")
    if not isinstance(ref, Mapping):
        raise _fail("schema must be a mapping", "invalid_request",
                    "malformed_proposal")
    _unknown_fields(ref, ("namespace", "version"), what="schema")
    if ref.get("namespace") != namespace:
        raise _fail("the proposal schema namespace is not the M4 namespace",
                    "invalid_request", "proposal_schema_mismatch")
    try:
        version = C.normalize_version(ref.get("version"))
    except C.ContractError as exc:
        raise _fail("the proposal schema version is unsupported",
                    "unsupported_version", "unsupported_version") from exc
    if version != PROPOSAL_VERSION:
        raise _fail("the proposal schema version is not 1.0",
                    "unsupported_version", "unsupported_version")


def _version(raw: Mapping[str, Any], name: str) -> None:
    try:
        version = C.normalize_version(raw.get(name))
    except C.ContractError as exc:
        raise _fail(f"{name} is unsupported", "unsupported_version",
                    "unsupported_version") from exc
    if version != PROPOSAL_VERSION:
        raise _fail(f"{name} is not 1.0", "unsupported_version",
                    "unsupported_version")


def validate_scope(raw: Any) -> dict[str, Any]:
    """Validate a proposal scope (a strict subset of a map request scope)."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _fail("scope must be a mapping", "invalid_request",
                    "malformed_proposal")
    _unknown_fields(raw, _SCOPE_FIELDS, what="scope")
    scope: dict[str, Any] = {}
    if "root" in raw:
        root = raw["root"]
        if not isinstance(root, str) or not root.strip():
            raise _fail("scope.root must be a string", "invalid_request",
                        "malformed_proposal")
        root = root.strip()
        if not root.startswith("/"):
            raise _fail("scope.root must be absolute", "invalid_request",
                        "malformed_proposal")
        if len(root) > MAX_SCOPE_TEXT or _CONTROL_RE.search(root):
            raise _fail("scope.root is malformed", "invalid_request",
                        "malformed_proposal")
        scope["root"] = root
    for name in ("session", "model"):
        if name in raw:
            scope[name] = _label(raw[name], f"scope.{name}")
    for name in ("repo", "checkout"):
        if name in raw:
            scope[name] = _identity(raw[name], name, f"scope.{name}")
    return scope


# ---------------------------------------------------------------------------
# The proposal contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditProposal:
    """A parsed, bounded, single-operation edit proposal."""

    action_id: str
    node_key: str
    file: str
    qualname: str
    operation: str
    body: str
    base_sha256: str
    snapshot: str | None
    scope: dict[str, Any]
    checks: tuple[str, ...]
    claims: tuple[dict[str, Any], ...]
    extensions: dict[str, Any]
    normalized: dict[str, Any]
    proposal_hash: str

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.normalized)


def parse_edit_proposal(payload: Any) -> EditProposal:
    """Parse and validate one M4 edit proposal.

    Raises :class:`ModelAPIError` with a stable reason on malformed,
    oversized, unsupported-version, unsupported-operation, path-escape or
    node-identity failures.  It reads no repository file and writes nothing.
    """
    raw = _load_payload(payload)
    _unknown_fields(raw, _PROPOSAL_FIELDS, what="proposal")
    _schema_ref(raw, namespace=PROPOSAL_NAMESPACE)
    if "proposal_version" not in raw:
        raise _fail("proposal_version is required", "invalid_request",
                    "malformed_proposal")
    _version(raw, "proposal_version")

    action_id = raw.get("action_id")
    if not isinstance(action_id, str) or not _ACTION_ID_RE.fullmatch(
            action_id.strip()):
        raise _fail("action_id must be a bounded idempotency key",
                    "invalid_request", "malformed_proposal")
    action_id = action_id.strip()

    node_raw = raw.get("node")
    if not isinstance(node_raw, Mapping):
        raise _fail("node must be a mapping", "invalid_request",
                    "malformed_proposal")
    _unknown_fields(node_raw, _NODE_FIELDS, what="node")
    if node_raw.get("kind") != "symbol":
        raise _fail("node.kind must be symbol", "unsupported",
                    "unsupported_operation")
    key = node_raw.get("key")
    if not isinstance(key, str) or not key.strip():
        raise _fail("node.key must be a non-empty symbol key",
                    "invalid_request", "malformed_proposal")
    key = key.strip()
    try:
        parsed_key = K.parse_key(key)
    except ValueError as exc:
        raise _fail("node.key is not a graph key", "invalid_request",
                    "node_mismatch") from exc
    if len(parsed_key) != 3 or parsed_key[0] != "symbol":
        raise _fail("node.key must be sym:<rel>::<qualname>", "invalid_request",
                    "node_mismatch")
    rel = _normalise_rel(parsed_key[1], "node.key path")
    qualname = parsed_key[2]
    if not rel.endswith(".py"):
        raise _fail("the M4 edit layer supports Python .py files only",
                    "unsupported", "unsupported_operation")
    if (not isinstance(qualname, str) or not qualname.strip()
            or len(qualname) > 256 or _CONTROL_RE.search(qualname)):
        raise _fail("node.key qualname is malformed", "invalid_request",
                    "node_mismatch")
    qualname = qualname.strip()
    if K.symbol_key(rel, qualname) != key:
        raise _fail("node.key is not the canonical symbol key",
                    "invalid_request", "node_mismatch")

    operation = raw.get("operation")
    if not isinstance(operation, str) or operation not in SUPPORTED_OPERATIONS:
        raise _fail(
            f"unsupported edit operation {operation!r}; M4 supports "
            f"{list(SUPPORTED_OPERATIONS)}", "unsupported",
            "unsupported_operation")

    body = raw.get("body")
    if not isinstance(body, str) or not body.strip():
        raise _fail("body must be non-empty source text", "invalid_request",
                    "malformed_proposal")
    if _CONTROL_RE.search(body) or _LONE_CR_RE.search(body):
        raise _fail("body contains a control character or a lone CR",
                    "invalid_value", "malformed_proposal")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise _fail(f"body exceeds {MAX_BODY_BYTES} bytes", "budget_exceeded",
                    "proposal_over_budget")

    base = raw.get("base_sha256")
    if not isinstance(base, str) or not _HASH_RE.fullmatch(base):
        raise _fail("base_sha256 must be 64 lowercase hex characters",
                    "invalid_request", "malformed_proposal")
    base = base.lower()

    snapshot = raw.get("snapshot")
    if snapshot is not None:
        snapshot = _identity(snapshot, "snapshot", "snapshot")

    scope = validate_scope(raw.get("scope"))

    checks_raw = raw.get("checks", [])
    if not isinstance(checks_raw, (list, tuple)) or isinstance(checks_raw, str):
        raise _fail("checks must be a list", "invalid_request",
                    "malformed_proposal")
    if len(checks_raw) > MAX_CHECKS:
        raise _fail(f"checks exceeds {MAX_CHECKS} entries", "budget_exceeded",
                    "proposal_over_budget")
    checks: list[str] = []
    for value in checks_raw:
        if not isinstance(value, str) or not _TEST_RE.fullmatch(value.strip()):
            raise _fail("each check must be a test: key", "invalid_request",
                        "malformed_proposal")
        checks.append(value.strip())

    claims_raw = raw.get("claims", [])
    if not isinstance(claims_raw, (list, tuple)) or isinstance(claims_raw, str):
        raise _fail("claims must be a list", "invalid_request",
                    "malformed_proposal")
    if len(claims_raw) > MAX_CLAIMS:
        raise _fail(f"claims exceeds {MAX_CLAIMS} entries", "budget_exceeded",
                    "proposal_over_budget")
    claims: list[dict[str, Any]] = []
    for value in claims_raw:
        if not isinstance(value, Mapping):
            raise _fail("each claim must be a mapping", "invalid_request",
                        "malformed_proposal")
        _unknown_fields(value, ("text",), what="claim")
        text = value.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _fail("a claim needs bounded text", "invalid_request",
                        "malformed_proposal")
        text = text.strip()
        if len(text) > MAX_CLAIM_TEXT or _CONTROL_RE.search(text):
            raise _fail("a claim text is malformed or too long",
                        "invalid_request", "malformed_proposal")
        text = E.redact_text(text, MAX_CLAIM_TEXT)
        if not text:
            raise _fail("a claim text is empty after redaction",
                        "invalid_request", "malformed_proposal")
        claims.append({"text": text})

    extensions = {name: copy.deepcopy(value) for name, value in raw.items()
                  if name.startswith(C.EXTENSION_PREFIX)}
    normalized: dict[str, Any] = {
        "schema": {"namespace": PROPOSAL_NAMESPACE,
                   "version": PROPOSAL_VERSION},
        "proposal_version": PROPOSAL_VERSION,
        "action_id": action_id,
        "node": {"kind": "symbol", "key": key},
        "operation": operation,
        "body": body,
        "base_sha256": base,
        "snapshot": snapshot,
        "scope": scope,
        "checks": sorted(dict.fromkeys(checks)),
        "claims": claims,
    }
    normalized.update(extensions)
    try:
        C.canonical_json(normalized)
    except C.ContractError as exc:
        raise _fail(f"the proposal is not canonical ({exc.code})",
                    "invalid_value", "proposal_schema_mismatch") from exc
    return EditProposal(
        action_id=action_id,
        node_key=key,
        file=rel,
        qualname=qualname,
        operation=operation,
        body=body,
        base_sha256=base,
        snapshot=snapshot,
        scope=scope,
        checks=tuple(sorted(dict.fromkeys(checks))),
        claims=tuple(claims),
        extensions=extensions,
        normalized=normalized,
        proposal_hash=C.content_hash(normalized),
    )


# ---------------------------------------------------------------------------
# Host policy and authorization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditAuthorization:
    """The host's explicit permission to turn a preview into a write.

    This is a host-owned value, not a field of the model proposal.  It maps
    to the existing edit transaction's ``write=True``; the transaction's
    base-hash check and atomic replace still run after it.
    """

    granted: bool = False
    actor: str = "host"
    reason: str = "host edit authorization"

    def __post_init__(self) -> None:
        if self.granted is not True and self.granted is not False:
            raise ValueError("EditAuthorization.granted must be a bool")
        object.__setattr__(self, "actor", _label(self.actor, "actor"))
        line = _line(E.redact_text(self.reason, MAX_REASON))
        if not line:
            raise ValueError("EditAuthorization.reason must not be empty")
        object.__setattr__(self, "reason", line)


@dataclass(frozen=True)
class _ScopeDecision:
    root: str
    session: str
    model: str
    repo: str | None
    checkout: str | None
    snapshot: str | None
    repo_state: str
    checkout_state: str
    snapshot_state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "session": self.session,
            "model": self.model,
            "repo": {"id": self.repo, "state": self.repo_state},
            "checkout": {"id": self.checkout, "state": self.checkout_state},
            "snapshot": {"id": self.snapshot, "state": self.snapshot_state},
        }


def _scope_decision(policy: policy_mod.HostPolicy, proposal: EditProposal, *,
                    expected_snapshot: str | None,
                    expected_checkout: str | None) -> _ScopeDecision:
    if expected_checkout is not None:
        expected_checkout = _identity(expected_checkout, "checkout",
                                      "expected_checkout")
    if expected_snapshot is not None:
        expected_snapshot = _identity(expected_snapshot, "snapshot",
                                      "expected_snapshot")
    scope = proposal.scope
    session = str(scope.get("session") or policy.session)
    model = str(scope.get("model") or policy.model)
    if not (session == policy.session or session in policy.allowed_sessions):
        raise _fail("the proposal session is not allowed by host policy",
                    "forbidden", "session_not_allowed")
    if not (model == policy.model or model in policy.allowed_models):
        raise _fail("the proposal model is not allowed by host policy",
                    "forbidden", "model_not_allowed")

    requested_root = scope.get("root")
    if requested_root is not None:
        try:
            candidate = Path(requested_root).expanduser().resolve(strict=False)
        except OSError as exc:
            raise _fail("the proposal root cannot be resolved", "invalid_request",
                        "malformed_proposal") from exc
        if candidate != policy.root:
            raise _fail("the proposal root is outside the host policy scope",
                        "forbidden", "cross_scope")

    requested_repo = scope.get("repo")
    if requested_repo is not None:
        if policy.repo is None:
            raise _fail("the proposal names a repository the host cannot "
                        "verify", "unknown", "scope_unverifiable")
        if requested_repo != policy.repo:
            raise _fail("the proposal repo does not match the host policy",
                        "forbidden", "cross_scope")

    requested_checkout = scope.get("checkout")
    if requested_checkout is not None:
        if expected_checkout is None:
            raise _fail("the proposal names a checkout the host cannot verify",
                        "unknown", "scope_unverifiable")
        if requested_checkout != expected_checkout:
            raise _fail("the proposal checkout does not match the observed "
                        "checkout", "stale", "checkout_mismatch")

    requested_snapshot = proposal.snapshot
    if requested_snapshot is not None:
        if expected_snapshot is None:
            raise _fail("the proposal names a snapshot the host cannot verify",
                        "unknown", "scope_unverifiable")
        if requested_snapshot != expected_snapshot:
            raise _fail("the proposal snapshot does not match the observed "
                        "snapshot", "stale", "snapshot_mismatch")
        if requested_checkout is not None:
            try:
                snapshot_parts = C.parse_id(requested_snapshot).components
                checkout_parts = C.parse_id(requested_checkout).components
            except C.ContractError as exc:
                raise _fail("the proposal snapshot identity is malformed",
                            "invalid_request", "malformed_proposal") from exc
            if snapshot_parts[:3] != checkout_parts:
                raise _fail("the proposal snapshot does not belong to the "
                            "proposal checkout", "stale", "snapshot_mismatch")

    if expected_checkout is not None and expected_snapshot is not None:
        try:
            if (C.parse_id(expected_snapshot).components[:3]
                    != C.parse_id(expected_checkout).components):
                raise _fail("the host's expected snapshot does not belong to "
                            "its expected checkout", "stale",
                            "snapshot_mismatch")
        except C.ContractError as exc:
            raise _fail("the host supplied an invalid expected identity",
                        "invalid_request", "malformed_proposal") from exc
    if policy.repo is not None and expected_snapshot is not None:
        try:
            if C.parse_id(expected_snapshot).components[:2] != (
                    C.parse_id(policy.repo).components):
                raise _fail("the host's expected snapshot belongs to another "
                            "repository", "stale", "snapshot_mismatch")
        except C.ContractError as exc:
            raise _fail("the host supplied an invalid expected snapshot",
                        "invalid_request", "malformed_proposal") from exc

    if not _path_allowed(policy, proposal.file):
        raise _fail("the proposal file is outside the allowed path scope",
                    "forbidden", "cross_scope")

    return _ScopeDecision(
        root=str(policy.root),
        session=session,
        model=model,
        repo=policy.repo or requested_repo,
        checkout=requested_checkout or expected_checkout,
        snapshot=requested_snapshot or expected_snapshot,
        repo_state="observed" if policy.repo is not None else "unknown",
        checkout_state=("observed" if (requested_checkout is not None
                                     or expected_checkout is not None)
                        else "unknown"),
        snapshot_state=("observed" if (requested_snapshot is not None
                                     or expected_snapshot is not None)
                        else "unknown"),
    )


# ---------------------------------------------------------------------------
# State directory, ledger and file checks
# ---------------------------------------------------------------------------


def _check_state_dir(root: Path) -> Path:
    root = Path(root).resolve()
    state = root / ".ozagia"
    if state.is_symlink():
        raise _fail("the repository state directory is a symlink",
                    "forbidden", "path_escape")
    if state.exists() and not state.is_dir():
        raise _fail("the repository state directory is not a directory",
                    "invalid_state", "invalid_internal_state")
    if state.exists():
        try:
            state.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise _fail("the repository state directory escapes the root",
                        "forbidden", "path_escape") from exc
    for rel in ("edits", "edits/backups"):
        path = state / rel
        if path.is_symlink():
            raise _fail("the edit backup directory is a symlink", "forbidden",
                        "path_escape")
        if path.exists():
            try:
                path.resolve(strict=True).relative_to(root)
            except (OSError, ValueError) as exc:
                raise _fail("the edit backup directory escapes the root",
                            "forbidden", "path_escape") from exc
    return state


def _ensure_state_subdir(root: Path, *parts: str) -> Path:
    state = _check_state_dir(root)
    try:
        state.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fail("the repository state directory could not be created",
                    "unavailable", "ledger_unavailable") from exc
    current = state
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise _fail("the action ledger path is a symlink", "forbidden",
                        "path_escape")
        try:
            current.mkdir(exist_ok=True)
        except OSError as exc:
            raise _fail("the action ledger could not be created",
                        "unavailable", "ledger_unavailable") from exc
        try:
            current.resolve(strict=True).relative_to(Path(root).resolve())
        except (OSError, ValueError) as exc:
            raise _fail("the action ledger escapes the repository state",
                        "forbidden", "path_escape") from exc
    return current


def _ledger_dir(root: Path) -> Path:
    return _ensure_state_subdir(root, "model_api", "actions")


def _ledger_path(root: Path, action_id: str) -> Path:
    digest = hashlib.sha256(action_id.encode("utf-8")).hexdigest()
    return _ledger_dir(root) / f"{digest}.json"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _prune_ledger(directory: Path) -> None:
    try:
        entries = sorted(
            (entry for entry in directory.glob("*.json") if entry.is_file()),
            key=lambda entry: entry.stat().st_mtime_ns)
    except OSError:
        return
    for entry in entries[:-MAX_LEDGER_ENTRIES]:
        try:
            entry.unlink()
        except OSError:
            continue


def _ledger_read(root: Path, action_id: str) -> dict[str, Any] | None:
    path = _ledger_path(root, action_id)
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_LEDGER_BYTES:
            raise ValueError("entry too large")
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _fail("the action ledger entry is unreadable", "invalid_state",
                    "ledger_unavailable") from exc
    if not isinstance(record, Mapping):
        raise _fail("the action ledger entry is malformed", "invalid_state",
                    "ledger_unavailable")
    schema = record.get("schema")
    if not isinstance(schema, Mapping):
        raise _fail("the action ledger entry has a foreign schema",
                    "invalid_state", "ledger_unavailable")
    try:
        entry_version = C.normalize_version(schema.get("version") or "")
    except C.ContractError as exc:
        raise _fail("the action ledger entry has a foreign version",
                    "invalid_state", "ledger_unavailable") from exc
    if (schema.get("namespace") != ACTION_NAMESPACE
            or entry_version != ACTION_VERSION):
        raise _fail("the action ledger entry has a foreign schema",
                    "invalid_state", "ledger_unavailable")
    action = record.get("action")
    if not isinstance(action, Mapping):
        raise _fail("the action ledger entry is malformed", "invalid_state",
                    "ledger_unavailable")
    required = ("action_id", "proposal_hash", "preview_hash", "status",
                "file", "node", "operation", "base_sha256", "new_sha256")
    if any(not isinstance(action.get(name), str) or not action.get(name)
           for name in required):
        raise _fail("the action ledger entry is incomplete", "invalid_state",
                    "ledger_unavailable")
    if not isinstance(action.get("scope"), Mapping):
        raise _fail("the action ledger entry has no scope", "invalid_state",
                    "ledger_unavailable")
    if action.get("action_id") != action_id or action.get("status") != "applied":
        raise _fail("the action ledger entry does not match the action",
                    "invalid_state", "ledger_unavailable")
    return dict(action)


def _ledger_write(root: Path, action_id: str, action: Mapping[str, Any]) -> None:
    record = {
        "schema": {"namespace": ACTION_NAMESPACE, "version": ACTION_VERSION},
        "action": dict(action),
    }
    try:
        data = C.canonical_json(record).encode("utf-8")
    except C.ContractError as exc:
        raise _fail("the action record is not canonical", "invalid_state",
                    "ledger_unavailable") from exc
    if len(data) > MAX_LEDGER_BYTES:
        raise _fail("the action record exceeds the ledger bound",
                    "budget_exceeded", "proposal_over_budget")
    try:
        _atomic_write(_ledger_path(root, action_id), data)
    except OSError as exc:
        raise _fail("the action ledger could not be written", "unavailable",
                    "ledger_unavailable") from exc
    _prune_ledger(_ledger_dir(root))


def _read_source(root: Path, proposal: EditProposal) -> txn.SourceFile:
    try:
        return txn.read_source(
            root, proposal.file, suffixes=(".py",),
            max_bytes=MAX_FILE_BYTES, error_cls=code_edit.CodeEditError)
    except code_edit.CodeEditError as exc:
        raise _map_edit_error(exc) from exc
    except OSError as exc:
        raise _fail("the target file could not be read", "unavailable",
                    "edit_failed") from exc


def _check_base(root: Path, proposal: EditProposal) -> txn.SourceFile:
    source = _read_source(root, proposal)
    if source.sha256 != proposal.base_sha256:
        raise _fail("the proposal base hash no longer matches the file",
                    "stale", "changed_base",
                    details={"expected": proposal.base_sha256,
                             "observed": source.sha256})
    return source


def _preview_hash(policy: policy_mod.HostPolicy, proposal: EditProposal,
                  decision: _ScopeDecision, base_sha256: str) -> str:
    return C.content_hash({
        "namespace": PROPOSAL_NAMESPACE,
        "proposal_hash": proposal.proposal_hash,
        "root": str(policy.root),
        "session": decision.session,
        "model": decision.model,
        "repo": decision.repo,
        "checkout": decision.checkout,
        "snapshot": decision.snapshot,
        "file": proposal.file,
        "node": proposal.node_key,
        "operation": proposal.operation,
        "base_sha256": base_sha256,
        "body_sha256": proposal.body_sha256,
    })


def _current_file(root: Path, rel: str, expected: str | None) -> dict[str, Any]:
    path = Path(root) / rel
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return {"state": "stale", "sha256": None}
        raw = path.read_bytes()
    except OSError:
        return {"state": "absent", "sha256": None}
    digest = hashlib.sha256(raw).hexdigest()
    if expected is None:
        state = "observed"
    elif digest == expected:
        state = "current"
    else:
        state = "stale"
    return {"state": state, "sha256": digest}


def _map_edit_error(exc: code_edit.CodeEditError) -> ModelAPIError:
    """Map an adapter refusal to typed vocabulary without copying its text."""
    message = str(exc)
    if "stale base" in message:
        return _fail("the file changed since the proposal was made", "stale",
                     "changed_base")
    if "escapes the repo root" in message:
        return _fail("the proposal target escapes the repository root",
                     "forbidden", "path_escape")
    if "no such file" in message:
        return _fail("the proposal target does not exist", "not_found",
                     "target_not_found")
    if "no definition" in message or "unknown symbol" in message:
        return _fail("the proposal symbol is not in the target file",
                     "invalid_request", "target_not_found")
    if "not supported" in message or "only" in message:
        return _fail("the edit adapter does not support this operation",
                     "unsupported", "unsupported_operation")
    if "would not parse" in message or "does not parse" in message:
        return _fail("the edited source would not parse", "invalid_value",
                     "parse_failed")
    if "overlapping edits" in message:
        return _fail("the proposal's edits overlap", "invalid_request",
                     "edit_failed")
    return _fail("the edit adapter refused the transaction", "invalid_request",
                 "edit_failed")


def _run_edit(root: Path, proposal: EditProposal, *, write: bool) -> dict[str, Any]:
    script = {
        "file": proposal.file,
        "base_sha256": proposal.base_sha256,
        "ops": [{
            "op": proposal.operation,
            "symbol": proposal.qualname,
            "text": proposal.body,
        }],
    }
    try:
        return code_edit.apply_edit_script(root, script, write=write)
    except code_edit.CodeEditError as exc:
        raise _map_edit_error(exc) from exc
    except OSError as exc:
        raise _fail("the edit transaction could not complete", "unavailable",
                    "edit_failed") from exc


def _bounded_diff(text: Any) -> tuple[str, bool]:
    value = str(text or "")
    data = value.encode("utf-8")
    if len(data) <= MAX_DIFF_BYTES:
        return value, False
    cut = data[:MAX_DIFF_BYTES].decode("utf-8", "ignore")
    return cut + "\n[diff truncated at the M4 bound]\n", True


def _trace(mode: str, proposal: EditProposal, *, extra: Mapping[str, Any] | None = None
           ) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event": f"edit_{mode}",
        "action_id": proposal.action_id,
        "proposal_hash": proposal.proposal_hash,
        "file": proposal.file,
        "node": proposal.node_key,
        "operation": proposal.operation,
    }
    if extra:
        event.update(extra)
    return {
        "trace_id": None,
        "redacted": True,
        "events": [event],
    }


def _authority() -> dict[str, Any]:
    return {
        "granted": False,
        "note": ("An edit response, a preview hash and an action id never "
                 "grant permission. The host's existing edit authorization "
                 "and the transaction's base-hash check remain authoritative."),
    }


def _edit_error(exc: ModelAPIError, trace: Mapping[str, Any]) -> dict[str, Any]:
    return error_payload(exc, trace=trace, fallback={
        "used": True,
        "representation": "plain",
        "reason": exc.reason,
        "instruction": ("Use the existing edit preview/CLI/MCP tools; no M4 "
                        "edit was applied for this request."),
    })


def _internal_error(exc: BaseException, trace: Mapping[str, Any]) -> dict[str, Any]:
    return _edit_error(internal_error(exc), trace)


def _response(*, proposal: EditProposal, decision: _ScopeDecision,
              preview_hash: str, result: Mapping[str, Any], mode: str,
              status: str, replayed: bool = False,
              ledger: Mapping[str, Any] | None = None,
              authorization: EditAuthorization | None = None,
              applied_at: int | str | None = None,
              warnings: Iterable[str] = ()) -> dict[str, Any]:
    root = Path(decision.root)
    diff_view, diff_truncated = _bounded_diff(result.get("diff_view"))
    current = _current_file(
        root, proposal.file,
        result.get("new_sha256") if result.get("applied") else result.get(
            "base_sha256"))
    response: dict[str, Any] = {
        "ok": True,
        "schema": {"namespace": schema_mod.RESPONSE_NAMESPACE,
                   "version": "1.0"},
        "response_version": "1.0",
        "kind": "edit",
        "mode": mode,
        "status": status,
        "action_id": proposal.action_id,
        "proposal_hash": proposal.proposal_hash,
        "preview_hash": preview_hash,
        "node": {
            "kind": "symbol",
            "key": proposal.node_key,
            "file": proposal.file,
            "qualname": proposal.qualname,
        },
        "operation": proposal.operation,
        "file": proposal.file,
        "base_sha256": result.get("base_sha256") or proposal.base_sha256,
        "new_sha256": result.get("new_sha256"),
        "body_sha256": proposal.body_sha256,
        "before": result.get("before"),
        "after": result.get("after"),
        "applied": bool(result.get("applied")) and not replayed,
        "replayed": replayed,
        "backup": result.get("backup"),
        "newline": result.get("newline"),
        "bom": result.get("bom"),
        "changes": result.get("changes") or [],
        "diff_view": diff_view,
        "diff_truncated": diff_truncated,
        "diff_note": ("a unified diff is a host-rendered view; the canonical "
                      "record is the operation, node and hashes"),
        "checks": list(proposal.checks),
        "claims": [dict(claim) for claim in proposal.claims],
        "scope": decision.to_dict(),
        "snapshot": {
            "id": decision.snapshot,
            "state": ("verified" if proposal.snapshot is not None
                      else "not_provided"),
        },
        "transaction": {
            "path": "ozagia.edit.txn",
            "authorized": mode == "apply" and not replayed,
            "write": bool(result.get("applied")) and not replayed,
            "authorization": ({"actor": authorization.actor,
                               "reason": authorization.reason}
                              if authorization is not None else None),
            "applied_at": applied_at,
        },
        "ledger": dict(ledger) if ledger is not None else {"state": "not_written"},
        "current_file": current,
        "warnings": [str(item) for item in warnings],
        "authority": _authority(),
        "trace": _trace(mode, proposal),
    }
    return response


def preview_edit(payload: Any, policy: policy_mod.HostPolicy, *,
                 expected_snapshot: str | None = None,
                 expected_checkout: str | None = None) -> dict[str, Any]:
    """Validate and preview one proposal; never writes."""
    trace: dict[str, Any] = {"trace_id": None, "redacted": True, "events": []}
    try:
        proposal = parse_edit_proposal(payload)
        trace = _trace("preview", proposal)
        decision = _scope_decision(
            policy, proposal, expected_snapshot=expected_snapshot,
            expected_checkout=expected_checkout)
        source = _check_base(Path(decision.root), proposal)
        preview_hash = _preview_hash(policy, proposal, decision, source.sha256)
        result = _run_edit(Path(decision.root), proposal, write=False)
        return _response(
            proposal=proposal, decision=decision, preview_hash=preview_hash,
            result=result, mode="preview", status="preview")
    except ModelAPIError as exc:
        return _edit_error(exc, trace)
    except Exception as exc:  # noqa: BLE001 - typed internal failure
        return _internal_error(exc, trace)


def _validate_time(value: Any, name: str) -> int | str:
    if isinstance(value, bool):
        raise _fail(f"{name} must be an injected timestamp, not bool",
                    "invalid_request", "malformed_proposal")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and 0 < len(value) <= 64:
        return value
    raise _fail(f"{name} must be an integer or a short string",
                "invalid_request", "malformed_proposal")


def _action_record(proposal: EditProposal, decision: _ScopeDecision,
                   preview_hash: str, result: Mapping[str, Any],
                   authorization: EditAuthorization, applied_at: int | str
                   ) -> dict[str, Any]:
    return {
        "schema": {"namespace": ACTION_NAMESPACE, "version": ACTION_VERSION},
        "action_id": proposal.action_id,
        "proposal_hash": proposal.proposal_hash,
        "preview_hash": preview_hash,
        "status": "applied",
        "file": proposal.file,
        "node": proposal.node_key,
        "operation": proposal.operation,
        "base_sha256": result.get("base_sha256") or proposal.base_sha256,
        "new_sha256": result.get("new_sha256"),
        "backup": result.get("backup"),
        "body_sha256": proposal.body_sha256,
        "applied_at": applied_at,
        "authorization": {"actor": authorization.actor,
                          "reason": authorization.reason},
        "checks": list(proposal.checks),
        "claims": [dict(claim) for claim in proposal.claims],
        "scope": decision.to_dict(),
        "source": "ozagia.model_api.edit",
    }


def _replay(*, proposal: EditProposal, decision: _ScopeDecision,
            entry: Mapping[str, Any], preview_hash: str,
            authorization: EditAuthorization) -> dict[str, Any]:
    if entry.get("scope") != decision.to_dict():
        raise _fail("this action id belongs to another effective scope",
                    "conflict", "action_conflict")
    record = {
        "base_sha256": entry.get("base_sha256"),
        "new_sha256": entry.get("new_sha256"),
        "backup": entry.get("backup"),
        # The historical action was applied; ``replayed=True`` keeps the
        # response's ``applied`` flag false while the current-file check uses
        # the recorded new hash.
        "applied": True,
        "before": None,
        "after": None,
        "changes": [],
    }
    current = _current_file(Path(decision.root), proposal.file,
                            entry.get("new_sha256"))
    warnings = ["the action was already applied; this call did not write again"]
    if current["state"] != "current":
        warnings.append("the file no longer matches the recorded result")
    return _response(
        proposal=proposal, decision=decision, preview_hash=preview_hash,
        result=record, mode="apply", status="replayed", replayed=True,
        ledger={"state": "replayed",
                "applied_at": entry.get("applied_at")},
        authorization=authorization,
        applied_at=entry.get("applied_at"), warnings=warnings)


def _attach_evidence(response: dict[str, Any], decision: _ScopeDecision,
                     goal: str | None, sources: E.Sources | None,
                     observed_at: int | str) -> dict[str, Any]:
    if goal is None:
        return response
    try:
        response["evidence"] = action_evidence(
            response, root=decision.root, goal=goal, sources=sources,
            observed_at=observed_at)
    except ModelAPIError as exc:
        response["evidence"] = {
            "ok": False,
            "reason": exc.reason,
            "error": exc.service_error.to_dict(),
            "authority": _authority(),
        }
    except Exception as exc:  # noqa: BLE001 - the edit stands
        response["evidence"] = {
            "ok": False,
            "reason": "evidence_unavailable",
            "error": internal_error(exc).service_error.to_dict(),
            "authority": _authority(),
        }
    return response


def apply_edit(payload: Any, policy: policy_mod.HostPolicy, *,
               authorization: EditAuthorization | None,
               preview_hash: str | None,
               expected_snapshot: str | None = None,
               expected_checkout: str | None = None,
               recorded_at: int | str = "unknown",
               evidence_goal: str | None = None,
               evidence_sources: E.Sources | None = None,
               observed_at: int | str = "unknown") -> dict[str, Any]:
    """Apply one previewed proposal through the existing edit transaction."""
    trace: dict[str, Any] = {"trace_id": None, "redacted": True, "events": []}
    try:
        proposal = parse_edit_proposal(payload)
        trace = _trace("apply", proposal)
        if (not isinstance(authorization, EditAuthorization)
                or authorization.granted is not True):
            raise _fail("the host did not authorize a write", "forbidden",
                        "write_declined")
        if not isinstance(preview_hash, str) or not preview_hash.strip():
            raise _fail("apply needs the preview_hash from a preview",
                        "invalid_request", "preview_required")
        preview_hash = preview_hash.strip()
        if not _HASH_RE.fullmatch(preview_hash):
            raise _fail("preview_hash is malformed", "invalid_request",
                        "malformed_proposal")
        decision = _scope_decision(
            policy, proposal, expected_snapshot=expected_snapshot,
            expected_checkout=expected_checkout)
        applied_at = _validate_time(recorded_at, "recorded_at")
        _check_state_dir(Path(decision.root))

        existing = _ledger_read(Path(decision.root), proposal.action_id)
        if existing is not None:
            if existing.get("proposal_hash") != proposal.proposal_hash:
                raise _fail("this action id was used for another proposal",
                            "conflict", "action_conflict")
            if existing.get("preview_hash") != preview_hash:
                raise _fail("the action id does not match this preview",
                            "conflict", "action_conflict")
            if existing.get("status") != "applied":
                raise _fail("the action id is not in an applied state",
                            "conflict", "action_conflict")
            return _attach_evidence(
                _replay(proposal=proposal, decision=decision,
                        entry=existing, preview_hash=preview_hash,
                        authorization=authorization),
                decision, evidence_goal, evidence_sources, observed_at)

        source = _check_base(Path(decision.root), proposal)
        expected_preview = _preview_hash(policy, proposal, decision,
                                         source.sha256)
        if preview_hash != expected_preview:
            raise _fail("the preview hash does not match this proposal and "
                        "base", "stale", "preview_mismatch")
        result = _run_edit(Path(decision.root), proposal, write=True)
        warnings: list[str] = []
        if _current_file(Path(decision.root), proposal.file,
                         result.get("new_sha256"))["state"] != "current":
            warnings.append("the file does not match the adapter's new hash "
                            "after the write")

        record = _action_record(proposal, decision, preview_hash, result,
                                authorization, applied_at)
        ledger_state = "applied"
        try:
            _ledger_write(Path(decision.root), proposal.action_id, record)
        except ModelAPIError as exc:
            ledger_state = "failed"
            warnings.append(f"the action ledger was not written ({exc.reason})")
        except Exception:  # noqa: BLE001 - the edit has already been applied
            ledger_state = "failed"
            warnings.append("the action ledger was not written")

        response = _response(
            proposal=proposal, decision=decision, preview_hash=preview_hash,
            result=result, mode="apply", status="applied",
            ledger={"state": ledger_state, "applied_at": applied_at},
            authorization=authorization, applied_at=applied_at,
            warnings=warnings)
        return _attach_evidence(response, decision, evidence_goal,
                                evidence_sources, observed_at)
    except ModelAPIError as exc:
        return _edit_error(exc, trace)
    except Exception as exc:  # noqa: BLE001 - typed internal failure
        return _internal_error(exc, trace)


def handle_edit(payload: Any, policy: policy_mod.HostPolicy, *,
                apply: bool = False,
                authorization: EditAuthorization | None = None,
                preview_hash: str | None = None,
                expected_snapshot: str | None = None,
                expected_checkout: str | None = None,
                recorded_at: int | str = "unknown",
                evidence_goal: str | None = None,
                evidence_sources: E.Sources | None = None,
                observed_at: int | str = "unknown") -> dict[str, Any]:
    """Preview by default; apply only with host authorization and a preview."""
    if apply is not True and apply is not False:
        raise TypeError("apply must be a bool")
    if not apply:
        return preview_edit(
            payload, policy, expected_snapshot=expected_snapshot,
            expected_checkout=expected_checkout)
    return apply_edit(
        payload, policy, authorization=authorization,
        preview_hash=preview_hash, expected_snapshot=expected_snapshot,
        expected_checkout=expected_checkout, recorded_at=recorded_at,
        evidence_goal=evidence_goal, evidence_sources=evidence_sources,
        observed_at=observed_at)


# ---------------------------------------------------------------------------
# Evidence wiring
# ---------------------------------------------------------------------------


def _check_rows(action: Mapping[str, Any], checks: Iterable[str],
                report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = {row.get("key"): row for row in report.get("test_keys") or []
            if isinstance(row, Mapping)}
    applied_at = action.get("transaction", {}).get("applied_at")
    out: list[dict[str, Any]] = []
    for key in sorted(dict.fromkeys(checks)):
        row = rows.get(key)
        if row is None:
            out.append({
                "key": key,
                "result": "not_assessed",
                "freshness": "absent",
                "evidence": "inferred",
                "recorded_at": None,
                "after_action": None,
                "reason": "the evidence collector returned no row for this key",
                "source": "ozagia.core.evidence",
                "run": None,
            })
            continue
        outcome = str(row.get("outcome") or "unknown")
        recorded_at = row.get("recorded_at")
        after_action: bool | None = None
        if isinstance(applied_at, int) and isinstance(recorded_at, int):
            after_action = recorded_at >= applied_at
        if after_action is True and outcome in ("passed", "failed", "error"):
            if outcome == "passed":
                result = "passed"
                reason = row.get("reason")
            elif outcome == "failed":
                result = "failed"
                reason = row.get("reason")
            else:
                result = "unknown"
                reason = ("the test errored (setup, fixture or collection); "
                          "an error is not an assertion failure")
            freshness = "current"
            evidence = "observed"
        else:
            result = "unknown"
            freshness = ("stale" if after_action is False else "unknown")
            evidence = "observed"
            if after_action is False:
                reason = "the check was recorded before the action was applied"
            elif after_action is True:
                reason = (row.get("reason")
                          or "the recorded check has no result")
            else:
                reason = ("the check has no comparable action/record "
                          "timestamps; it cannot be tied to this action")
        out.append({
            "key": key,
            "result": result,
            "freshness": freshness,
            "evidence": evidence,
            "recorded_at": recorded_at,
            "after_action": after_action,
            "reason": _line(reason) if reason else None,
            "source": "ozagia.core.working_set.recorded_outcome",
            "run": row.get("run"),
        })
    return out


def _action_assessment(action: Mapping[str, Any], rows: list[dict[str, Any]],
                       claims: list[dict[str, Any]],
                       action_freshness: str) -> dict[str, Any]:
    applied = action.get("applied") is True or action.get("replayed") is True
    if not applied:
        return {
            "status": "not_assessed",
            "green": False,
            "basis": ["no edit was applied"],
            "reasons": ["a preview is not an applied action"],
        }
    if not rows:
        if claims:
            return {
                "status": "declared",
                "green": False,
                "basis": ["model claims only; no selected check was read"],
                "reasons": ["a model claim is not a result"],
            }
        return {
            "status": "not_assessed",
            "green": False,
            "basis": ["no selected check and no claim"],
            "reasons": ["the edit is applied but nothing was checked"],
        }
    if any(row["result"] == "failed" for row in rows):
        return {
            "status": "failed",
            "green": False,
            "basis": ["an observed selected check failed"],
            "reasons": [row["key"] for row in rows
                        if row["result"] == "failed"],
        }
    if any(row["result"] != "passed" or row["freshness"] != "current"
           or row["evidence"] != "observed" for row in rows):
        return {
            "status": "unknown",
            "green": False,
            "basis": ["a selected check is unknown, stale or not tied to the "
                      "action"],
            "reasons": [row["key"] for row in rows
                        if row["result"] != "passed"
                        or row["freshness"] != "current"
                        or row["evidence"] != "observed"],
        }
    if action_freshness != "current":
        return {
            "status": "unknown",
            "green": False,
            "basis": ["the action's file is no longer at its recorded hash"],
            "reasons": [f"file freshness is {action_freshness}"],
        }
    return {
        "status": "passed",
        "green": True,
        "basis": ["an applied action", "an observed selected check recorded "
                  "at or after the action", "the file is at the recorded hash"],
        "reasons": [],
    }


def action_evidence(action: Mapping[str, Any], *, root: str | Path, goal: str,
                    checks: Iterable[str] = (),
                    sources: E.Sources | None = None,
                    observed_at: int | str = "unknown") -> dict[str, Any]:
    """Join one applied action and its selected checks to evidence.

    The result is read-only and honest about its limits: a test record is a
    recorded result, not proof that the edited bytes were executed, and a
    model claim stays ``declared``.  No check is run here; the collector only
    reads the goal store, run manifests and the working-set test record.
    """
    if not isinstance(action, Mapping) or action.get("ok") is not True:
        raise _fail("action evidence needs a successful edit response",
                    "invalid_request", "malformed_proposal")
    file = action.get("file")
    if not isinstance(file, str):
        raise _fail("the action has no file", "invalid_request",
                    "malformed_proposal")
    rel = _normalise_rel(file, "action.file")
    action_id = action.get("action_id")
    if not isinstance(action_id, str) or not _ACTION_ID_RE.fullmatch(action_id):
        raise _fail("the action id is malformed", "invalid_request",
                    "malformed_proposal")

    selected: list[str] = []
    if isinstance(checks, str):
        raise _fail("checks must be an iterable of test: keys",
                    "invalid_request", "malformed_proposal")
    action_checks = action.get("checks") or []
    if isinstance(action_checks, str):
        raise _fail("the action checks field is malformed", "invalid_request",
                    "malformed_proposal")
    for value in list(action_checks) + list(checks or ()):
        if not isinstance(value, str) or not _TEST_RE.fullmatch(value.strip()):
            raise _fail("a selected check must be a test: key",
                        "invalid_request", "malformed_proposal")
        selected.append(value.strip())
    if len(selected) > MAX_CHECKS:
        raise _fail(f"checks exceeds {MAX_CHECKS} entries", "budget_exceeded",
                    "proposal_over_budget")

    try:
        report = E.collect_evidence(
            root, goal, test_keys=sorted(dict.fromkeys(selected)),
            sources=sources, observed_at=observed_at)
    except E.EvidenceError as exc:
        raise _fail("the evidence collector refused the join", exc.code,
                    "evidence_unavailable") from exc
    except Exception as exc:  # noqa: BLE001 - an unavailable join stays typed
        raise _fail("the evidence collector was unavailable", "unavailable",
                    "evidence_unavailable") from exc

    claims_raw = action.get("claims") or []
    if not isinstance(claims_raw, (list, tuple)) or isinstance(claims_raw, str):
        raise _fail("the action claims field is malformed", "invalid_request",
                    "malformed_proposal")
    if len(claims_raw) > MAX_CLAIMS:
        raise _fail(f"claims exceeds {MAX_CLAIMS} entries", "budget_exceeded",
                    "proposal_over_budget")
    claims = [{"text": E.redact_text(_line(claim.get("text"), MAX_CLAIM_TEXT),
                                    MAX_CLAIM_TEXT),
               "result": "declared", "freshness": "unknown",
               "evidence": "declared", "authority": "none"}
              for claim in claims_raw
              if isinstance(claim, Mapping)]
    claims = [claim for claim in claims if claim["text"]]
    rows = _check_rows(action, selected, report)
    applied_or_replayed = (action.get("applied") is True
                           or action.get("replayed") is True)
    expected = action.get("new_sha256") if applied_or_replayed else None
    current = _current_file(Path(str(Path(root).resolve())), rel,
                            expected if isinstance(expected, str) else None)
    if applied_or_replayed:
        action_freshness = current["state"]
    else:
        action_freshness = "unknown"
    assessment = _action_assessment(action, rows, claims, action_freshness)
    transaction = action.get("transaction")
    applied_at = (transaction.get("applied_at")
                  if isinstance(transaction, Mapping) else None)
    return {
        "ok": True,
        "schema": {"namespace": EVIDENCE_NAMESPACE,
                   "version": EVIDENCE_VERSION},
        "evidence_version": EVIDENCE_VERSION,
        "action": {
            "action_id": action_id,
            "proposal_hash": action.get("proposal_hash"),
            "status": action.get("status"),
            "applied": action.get("applied") is True,
            "replayed": action.get("replayed") is True,
            "file": rel,
            "base_sha256": action.get("base_sha256"),
            "new_sha256": action.get("new_sha256"),
            "applied_at": applied_at,
            "freshness": action_freshness,
            "evidence": ("observed" if action.get("applied") is True
                         or action.get("replayed") is True else "declared"),
            "source": "ozagia.model_api.edit",
        },
        "goal": report.get("goal"),
        "checks": rows,
        "claims": claims,
        "assessment": assessment,
        "evidence": report,
        "evidence_hash": E.evidence_hash(report),
        "not_assessed": [
            "the evidence collector does not execute a test or a command",
            "a test record does not record which bytes or interpreter it ran",
            "a model claim is declared text, never a result",
            "durable evidence writes are deferred",
        ],
        "authority": _authority(),
    }


__all__ = [
    "ACTION_NAMESPACE", "ACTION_VERSION", "EVIDENCE_NAMESPACE",
    "EVIDENCE_VERSION", "EditAuthorization", "EditProposal",
    "MAX_ACTION_ID", "MAX_BODY_BYTES", "MAX_CLAIMS", "MAX_CHECKS",
    "MAX_DIFF_BYTES", "MAX_FILE_BYTES", "MAX_LEDGER_BYTES",
    "MAX_LEDGER_ENTRIES", "MAX_PROPOSAL_BYTES", "MAX_PROPOSAL_DEPTH",
    "MAX_PROPOSAL_NODES", "PROPOSAL_NAMESPACE", "PROPOSAL_VERSION",
    "SUPPORTED_OPERATIONS", "action_evidence", "apply_edit", "handle_edit",
    "parse_edit_proposal", "preview_edit", "validate_scope",
]
