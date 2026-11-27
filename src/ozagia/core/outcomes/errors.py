"""Typed errors for the durable outcome journal.

The hierarchy separates a caller mistake (validation, conflict, stale
precondition) from damage to the stored journal (integrity, unsupported
version) and from an unavailable lock.  Every error carries a stable ``code``
so a client, CLI or MCP adapter can map it without matching message text.
"""

from __future__ import annotations

from typing import Any

MAX_ERROR_MESSAGE = 1000


class OutcomeError(ValueError):
    """Base class for outcome-store failures."""

    code = "outcomes.error"

    def __init__(self, message: Any, *, code: str | None = None,
                 detail: Any = None) -> None:
        if code is not None:
            self.code = code
        self.detail = detail
        super().__init__(" ".join(str(message).split(" "))[:MAX_ERROR_MESSAGE])

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.detail is not None:
            result["detail"] = self.detail
        return result


class OutcomeValidationError(OutcomeError):
    """A caller supplied a missing, malformed or oversized value."""

    code = "outcomes.invalid_request"


class OutcomeBoundsError(OutcomeValidationError):
    """A field or payload exceeded a documented bound."""

    code = "outcomes.bounds"


class OutcomeConflictError(OutcomeError):
    """An idempotency key or natural key was reused for a different request."""

    code = "outcomes.conflict"


class OutcomeTransitionError(OutcomeError):
    """A requested lifecycle transition is not allowed in the current state."""

    code = "outcomes.invalid_state"


class OutcomeStaleError(OutcomeTransitionError):
    """A revision, lease generation or expected state is no longer current."""

    code = "outcomes.stale"


class OutcomeNotFoundError(OutcomeError):
    """The requested intent, work item, attempt or record does not exist."""

    code = "outcomes.not_found"


class OutcomeIntegrityError(OutcomeError):
    """The journal is damaged, non-canonical or has a broken hash chain.

    The store fails closed: a damaged journal is never partially applied and
    an interior malformed line is never skipped.
    """

    code = "outcomes.corrupt"


class OutcomeUnsupportedError(OutcomeIntegrityError):
    """The journal uses a schema or event this build cannot interpret."""

    code = "outcomes.unsupported"


class OutcomeLockError(OutcomeError):
    """The outcome journal could not be locked in the allowed time."""

    code = "outcomes.unavailable"


class OutcomeStoreError(OutcomeError):
    """A filesystem or store-level failure that is not journal corruption."""

    code = "outcomes.unavailable"
