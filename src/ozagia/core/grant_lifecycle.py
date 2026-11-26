"""Host-side lifecycle for task, eval and harness resource grants.

A task, eval run or agent session declares grant ids.  The host that starts it
supplies the approval flags; the task file and the model never do.  This module
ties the read-only check to the claim ledger:

* :func:`normalize_ids` bounds and validates a declared id list;
* :func:`validate_ids` refuses ids that are not declared in the repository;
* :class:`GrantLease` preflights every grant read-only, claims each one before
  the work starts, and releases every claim in reverse order afterwards.

A lease never reads a credential or calls a provider.  Its report contains only
grant ids, claim ids, non-secret names, budgets, approval metadata, observed
values supplied by the caller, and cleanup state.  When the caller supplies no
observed cost/time or cleanup, those values stay ``unknown``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.core import resource_grants as RG
from ozagia.core import security as SEC

MAX_GRANTS = 16
_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")


class GrantLifecycleError(ValueError):
    """A declared grant list is invalid or cannot be resolved."""


def normalize_ids(values: object) -> tuple[str, ...]:
    """A bounded, duplicate-free tuple of validated grant ids."""
    if values is None:
        return ()
    if isinstance(values, (str, bytes, Mapping)) or not isinstance(
            values, (list, tuple)):
        raise GrantLifecycleError("must be a list of resource grant ids")
    if len(values) > MAX_GRANTS:
        raise GrantLifecycleError(
            f"must contain at most {MAX_GRANTS} grant ids")
    result: list[str] = []
    for index, item in enumerate(values):
        if not isinstance(item, str):
            raise GrantLifecycleError(
                f"entry {index} must be a resource grant id")
        text = item.strip()
        if not _ID_RE.fullmatch(text):
            raise GrantLifecycleError(
                f"entry {index} must match [A-Za-z][A-Za-z0-9_.-]{{0,63}}")
        if text in result:
            raise GrantLifecycleError(f"duplicate resource grant id {text!r}")
        result.append(text)
    return tuple(result)


def validate_ids(root: str | Path, ids: Iterable[str]) -> list[str]:
    """Unknown declared grant ids, named, as task-validation errors."""
    listing = RG.list_grants(root)
    known = {str(item.get("id")) for item in listing.get("grants") or []}
    errors: list[str] = []
    for grant_id in ids:
        if grant_id not in known:
            errors.append(f"unknown resource grant {grant_id!r}")
    return errors


def _budget_text(budget: Mapping[str, Any] | None) -> str:
    if not budget:
        return "the grant's declared budget"
    parts: list[str] = []
    max_usd = budget.get("max_usd")
    max_minutes = budget.get("max_minutes")
    if max_usd is not None:
        parts.append(f"{float(max_usd):g} USD")
    if max_minutes is not None:
        parts.append(f"{float(max_minutes):g} minutes")
    return "budget " + " / ".join(parts) if parts else "the grant's declared budget"


def _claim_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    approval = record.get("approval") or {}
    return {
        "grant_id": record.get("grant_id"),
        "claim_id": record.get("claim_id"),
        "task": record.get("task"),
        "session": record.get("session"),
        "reason": record.get("reason"),
        "status": record.get("status"),
        "approval": {
            "required": approval.get("required") is True,
            "granted": approval.get("granted") is True,
            "kind": approval.get("kind"),
            "reason": approval.get("reason"),
            "approved_by": approval.get("approved_by"),
            "source": approval.get("source"),
            "at": approval.get("at"),
            "permissions": list(approval.get("permissions") or []),
            "flags": dict(approval.get("flags") or {}),
        },
        "budget": dict(record.get("budget") or {}),
        "observed": dict(record.get("observed") or {}),
        "cleanup": dict(record.get("cleanup") or {}),
        "release": None,
    }


class GrantLease:
    """Preflight, claim and release a bounded set of resource grants.

    ``approve``, ``allow_unbounded`` and ``allow_keep`` are host decisions and
    are never read from a task file.  ``acquire`` releases a partial claim set
    before returning ``False``; ``release`` is idempotent and releases in
    reverse claim order.
    """

    def __init__(
            self, root: str | Path, grant_ids: object, *,
            task: str | None = None,
            session: str | None = None,
            reason: str | None = None,
            approve: bool = False,
            allow_unbounded: bool = False,
            allow_keep: bool = False,
            budget: Mapping[str, Any] | None = None) -> None:
        self.root = Path(root).resolve()
        self.ids = normalize_ids(grant_ids)
        self.task = task
        self.session = session
        self.reason = reason
        self.approve = bool(approve)
        self.allow_unbounded = bool(allow_unbounded)
        self.allow_keep = bool(allow_keep)
        self.budget = dict(budget) if budget is not None else None
        self.claims: list[dict[str, Any]] = []
        self.refused: dict[str, Any] | None = None
        self.warnings: list[str] = []
        self.preflight_report: dict[str, Any] | None = None
        self._released: dict[str, dict[str, Any]] = {}
        self._release_errors: dict[str, dict[str, Any]] = {}

    # -- read-only preflight ------------------------------------------------ #
    def preflight(self) -> bool:
        """Check every grant read-only; never claim or write."""
        if not self.ids:
            self.preflight_report = {
                "declared": [], "ok": True, "checks": [], "failures": [],
                "warnings": [],
            }
            return True
        checks: list[dict[str, Any]] = []
        failures: list[str] = []
        for grant_id in self.ids:
            row: dict[str, Any] = {"grant": grant_id}
            try:
                report = RG.check_grant(
                    self.root, grant_id,
                    approval_granted=self.approve,
                    unbounded_allowed=self.allow_unbounded)
            except RG.GrantError as exc:
                row.update(state="unknown", ready=False,
                           error=str(exc))
                failures.append(
                    f"resource grant {grant_id!r}: {str(exc)}")
                checks.append(row)
                continue
            state = str(report.get("state") or "unknown")
            ready = bool(report.get("ready"))
            row.update(
                state=state,
                ready=ready,
                budget=report.get("budget"),
                cleanup=report.get("cleanup"),
                approval=report.get("approval"),
                failed_checks=[
                    check.get("id") for check in report.get("checks") or []
                    if check.get("level") == "error"
                ][:10],
            )
            permissions = report.get("permissions") or []
            if ready and "admin" in permissions and not (
                    self.approve and self.allow_unbounded):
                ready = False
                state = "requires_approval"
                row["reason"] = ("admin permission requires --approve and "
                                 "--allow-unbounded")
            if ready and report.get("cleanup") == "keep" and not (
                    self.approve and self.allow_keep):
                ready = False
                state = "requires_approval"
                row["reason"] = ("cleanup keep requires --approve and "
                                 "--allow-keep")
            row["state"] = state
            row["ready"] = ready
            if not ready:
                failures.append(
                    f"resource grant {grant_id!r} is not ready ({state})")
            checks.append(row)
        self.preflight_report = {
            "declared": list(self.ids),
            "ok": not failures,
            "checks": checks,
            "failures": failures,
            "warnings": list(self.warnings),
            "approval": {
                "approve": self.approve,
                "allow_unbounded": self.allow_unbounded,
                "allow_keep": self.allow_keep,
            },
        }
        return not failures

    # -- claim/release ------------------------------------------------------ #
    def _claim_reason(self, task: str | None, session: str | None) -> str:
        parts: list[str] = []
        if self.reason:
            parts.append(self.reason.strip())
        parts.append(
            f"task {task or self.task or 'unknown'} run "
            f"{session or self.session or 'unknown'}")
        parts.append(_budget_text(self.budget))
        return "; ".join(parts)

    def acquire(self, *, task: str | None = None,
                session: str | None = None) -> bool:
        """Claim every grant, or release the partial set and return ``False``."""
        if not self.ids:
            return True
        task = task or self.task
        session = session or self.session
        self.task = task
        self.session = session
        reason = self._claim_reason(task, session)
        self.claims = []
        for grant_id in self.ids:
            try:
                record = RG.claim(
                    self.root, grant_id, reason=reason, task=task,
                    session=session, approve=self.approve,
                    allow_unbounded=self.allow_unbounded,
                    allow_keep=self.allow_keep, budget=self.budget)
            except RG.GrantError as exc:
                self.refused = {
                    "grant": grant_id,
                    "code": getattr(exc, "code", "invalid_grant"),
                    "message": str(exc),
                    "detail": dict(getattr(exc, "detail", {}) or {}),
                }
                self.release()
                return False
            except Exception as exc:  # noqa: BLE001 - a claim failure refuses
                self.refused = {
                    "grant": grant_id,
                    "code": "claim_failed",
                    "message": type(exc).__name__,
                    "detail": {},
                }
                self.release()
                return False
            self.claims.append(record)
        return True

    def release(self, *, observed: Mapping[str, Any] | None = None
                ) -> dict[str, Any]:
        """Release every claim in reverse order; unknown values stay unknown."""
        for record in reversed(self.claims):
            claim_id = str(record["claim_id"])
            if claim_id in self._released or claim_id in self._release_errors:
                continue
            try:
                result = RG.release(
                    self.root, claim_id, apply=True,
                    observed=dict(observed) if observed is not None else None)
            except RG.GrantError as exc:
                error = {
                    "code": getattr(exc, "code", "release_failed"),
                    "message": str(exc),
                }
                self._release_errors[claim_id] = error
                self.warnings.append(
                    f"resource grant claim {claim_id} was not released: "
                    f"{exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - report, never hide
                self._release_errors[claim_id] = {
                    "code": "release_failed",
                    "message": type(exc).__name__,
                }
                self.warnings.append(
                    f"resource grant claim {claim_id} was not released: "
                    f"{type(exc).__name__}")
                continue
            self._released[claim_id] = result
        return self.to_dict()

    @property
    def released(self) -> bool:
        return bool(self.claims) and all(
            str(record["claim_id"]) in self._released
            for record in self.claims)

    # -- reporting ---------------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        claims: list[dict[str, Any]] = []
        unreleased: list[str] = []
        for record in self.claims:
            claim_id = str(record["claim_id"])
            item = _claim_summary(record)
            result = self._released.get(claim_id)
            error = self._release_errors.get(claim_id)
            if result is not None:
                item["status"] = result.get("status")
                item["observed"] = dict(result.get("observed") or {})
                item["cleanup"] = dict(result.get("cleanup") or {})
                item["release"] = {
                    "applied": bool(result.get("applied")),
                    "status": result.get("status"),
                    "at": (result.get("released") or
                           (result.get("release") or {}).get("at")),
                    "observed": dict(result.get("observed") or {}),
                    "cleanup": dict(result.get("cleanup") or {}),
                }
            elif error is not None:
                item["release"] = dict(error)
                unreleased.append(claim_id)
            else:
                unreleased.append(claim_id)
            claims.append(item)
        warnings = list(self.warnings)
        if unreleased:
            warnings.append(
                f"{len(unreleased)} resource grant claim(s) were not "
                "released: " + ", ".join(unreleased[:5]))
        return {
            "declared": list(self.ids),
            "approval": {
                "approve": self.approve,
                "allow_unbounded": self.allow_unbounded,
                "allow_keep": self.allow_keep,
                "reason": (SEC.redact_text(self.reason)
                           if self.reason is not None else None),
            },
            "budget": dict(self.budget) if self.budget is not None else None,
            "preflight": self.preflight_report,
            "claims": claims,
            "refused": self.refused,
            "released": not unreleased,
            "unreleased": unreleased,
            "warnings": list(dict.fromkeys(warnings)),
        }


def format_summary(report: Mapping[str, Any] | None,
                   *, indent: str = "  ") -> list[str]:
    """Bounded console lines for a grant report; [] when no grants."""
    if not report:
        return []
    claims = list(report.get("claims") or [])
    released = sum(1 for claim in claims
                   if (claim.get("release") or {}).get("applied") is True)
    cleanup: dict[str, int] = {}
    for claim in claims:
        state = str((claim.get("cleanup") or {}).get("status") or "unknown")
        cleanup[state] = cleanup.get(state, 0) + 1
    cleanup_text = ", ".join(
        f"{state} {cleanup[state]}" for state in ("done", "failed", "unknown")
        if cleanup.get(state))
    line = (f"{indent}resource grants: {len(report.get('declared') or [])} "
            f"declared; {len(claims)} claimed; {released} released")
    if cleanup_text:
        line += f"; cleanup {cleanup_text}"
    lines = [line]
    for claim in claims:
        release = claim.get("release") or {}
        state = release.get("status") or claim.get("status") or "unknown"
        lines.append(
            f"{indent}  {claim.get('grant_id')}: {claim.get('claim_id')} "
            f"({state}, cleanup "
            f"{(claim.get('cleanup') or {}).get('status') or 'unknown'})")
    refused = report.get("refused") or {}
    if refused:
        lines.append(
            f"{indent}  refused {refused.get('grant')}: {refused.get('code')} "
            f"{refused.get('message')}")
    for warning in report.get("warnings") or []:
        lines.append(f"{indent}warning: {warning}")
    return lines


__all__ = [
    "GrantLease", "GrantLifecycleError", "MAX_GRANTS", "format_summary",
    "normalize_ids", "validate_ids",
]
