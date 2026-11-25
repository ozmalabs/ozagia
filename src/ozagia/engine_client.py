"""A small, stdlib-only client for the loopback graph-engine HTTP transport.

The transport in :mod:`ozagia.engine_http` is deliberately thin: every request
is one engine object and every response is the engine JSON.  A caller that
wants the stateful, token-saving behaviour (one session, op-scoped ``known``
hashes, paging and bounded ``fetch``) otherwise has to remember which rows it
already showed, inject the right deltas and keep the session handle.  This
module does that once for every local client.

What it does *not* do:

* no model, outbound network or target import/execution beyond the configured
  base URL;
* no retries (a failed call is reported, never replayed);
* no hidden ``spec``/``fetch`` work: speculation is opt-in per call/session;
* no token in ``repr``, ``stats``, exceptions or logging.

Usage::

    from ozagia.engine_client import GraphEngineClient

    with GraphEngineClient("http://127.0.0.1:8765") as engine:
        hello = engine.hello()
        hits = engine.find("retry backoff")
        # The client remembers find/outline/context hashes and sends them
        # automatically, so this response is a delta (`u`), not a repeat.
        again = engine.find("retry backoff")
        while engine.next_request(again) is not None:
            again = engine.next_page(again)
        print(engine.stats())

The token comes from ``OZAGIA_ENGINE_TOKEN``, an explicit ``token`` argument,
an explicit ``token_file``, or ``<state>/engine/token`` (state is normally
``<root>/.ozagia``).  The token file is refused when it is a symlink or is
readable by group/other, matching the server.

``task_context`` is the one exception to the session flow: it posts to
``/v1/ozma/context`` without calling ``hello`` or touching ``session``/``known``,
so one client can serve both graph operations and Ozma provider context.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Sequence

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_BUDGET = 1200
MAX_TOKEN_BYTES = 4_096
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
TOKEN_ENV = "OZAGIA_ENGINE_TOKEN"
BASE_URL_ENV = "OZAGIA_ENGINE_URL"
GRAPH_PATH = "/v1/graph"
CONTEXT_PATH = "/v1/ozma/context"
HEALTH_PATH = "/healthz"
KNOWN_OPS = ("find", "outline", "context")
DEFAULT_USER_AGENT = "ozagia-engine-client/1"
# Provider bounds (ozagia.integrations.ozma.context.task_context), plus a local
# task-size bound that keeps the JSON body well under the server's 64 KiB cap.
CONTEXT_DEFAULT_BUDGET_TOKENS = 1500
CONTEXT_MIN_BUDGET_TOKENS = 1
CONTEXT_MAX_BUDGET_TOKENS = 32_000
CONTEXT_DEFAULT_MAX_ITEMS = 8
CONTEXT_MIN_ITEMS = 1
CONTEXT_MAX_ITEMS = 100
MAX_TASK_CHARS = 4_096
CONTEXT_OP = "ozma_context"


class EngineClientError(RuntimeError):
    """A client-side failure that is not a graph result."""


class EngineConfigError(EngineClientError):
    """The client configuration (URL, token, bounds) is invalid."""


class EngineTransportError(EngineClientError):
    """The request never produced an engine JSON object."""


class EngineHTTPStatusError(EngineClientError):
    """The transport returned a stable JSON HTTP error."""

    def __init__(self, status: int, code: str, message: str, *,
                 retry_after: str | None = None) -> None:
        self.status = int(status)
        self.code = str(code or "http_error")
        self.message = str(message or "")
        self.retry_after = retry_after
        text = f"HTTP {self.status} {self.code}"
        if self.message:
            text += f": {self.message}"
        super().__init__(text)


class EngineAuthError(EngineHTTPStatusError):
    """The bearer token is missing or wrong (HTTP 401)."""


class EngineUnavailableError(EngineHTTPStatusError):
    """The transport is saturated (HTTP 503); ``retry_after`` may be set."""


class EngineResultError(EngineClientError):
    """The engine answered with a compact ``e`` error object."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)
        self.code = str(payload.get("e") or "engine_error")
        why = payload.get("why")
        super().__init__(f"{self.code}: {why}" if why else self.code)

    @property
    def next(self) -> dict[str, Any] | None:
        """The engine's self-contained retry/continuation, when it gave one."""
        value = self.payload.get("n")
        if not isinstance(value, dict):
            value = self.payload.get("next")
        return dict(value) if isinstance(value, dict) else None


class EngineBudgetError(EngineResultError):
    """No row fits the requested budget; ``next`` may carry a larger budget."""

    @property
    def suggested_budget(self) -> int | None:
        value = (self.next or {}).get("budget")
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


class EngineContextError(EngineResultError):
    """``/v1/ozma/context`` rejected the request (HTTP 400).

    The payload keeps the transport's stable code (``invalid_context_request``)
    and message, so callers can treat it like other engine ``e`` results.
    """

    def __init__(self, payload: dict[str, Any], *, status: int = 400) -> None:
        self.status = int(status)
        super().__init__(payload)


class EngineWithheldError(EngineResultError):
    """The snapshot is stale/unknown, so structural data was withheld."""

    @property
    def freshness(self) -> str | None:
        value = self.payload.get("f")
        return str(value) if value is not None else None

    @property
    def snapshot(self) -> str | None:
        value = self.payload.get("s")
        return str(value) if value is not None else None


class EngineSessionError(EngineResultError):
    """The session handle is unknown or expired; call ``hello`` again."""


class EnginePagingError(EngineClientError):
    """A page was requested but the result has no ``next`` request."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so requests stay at the configured base URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _valid_token(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_TOKEN_BYTES
            and not any(ord(char) < 0x21 or ord(char) == 0x7F for char in value))


def _read_token_file(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise EngineConfigError(
            f"cannot read engine token file {path}: {exc}") from None
    if path.is_symlink() or not path.is_file():
        raise EngineConfigError(
            f"engine token path is not a regular file: {path}")
    if info.st_mode & 0o077:
        raise EngineConfigError(
            f"engine token file {path} is accessible to group/other; "
            "run `chmod 600` on it or regenerate with `ozagia engine token`")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_TOKEN_BYTES + 1)
    except OSError as exc:
        raise EngineConfigError(
            f"cannot read engine token file {path}: {exc}") from None
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise EngineConfigError(f"engine token file {path} is not UTF-8") from None
    if not _valid_token(value):
        raise EngineConfigError(f"engine token file {path} is empty or malformed")
    return value


def _default_token_path(root: str | Path) -> Path:
    # Imported lazily so constructing a client with an explicit token does not
    # pull in the rest of the application.
    from ozagia.config import state_path

    return state_path(root, "engine", "token")


def _resolve_token(token: str | None, token_file: str | Path | None,
                   root: str | Path | None) -> str:
    if token is not None:
        if not _valid_token(token):
            raise EngineConfigError(
                "the engine token is empty or contains whitespace/control characters")
        return token
    env = os.environ.get(TOKEN_ENV)
    if env is not None:
        if not _valid_token(env):
            raise EngineConfigError(
                f"{TOKEN_ENV} is empty or contains whitespace/control characters")
        return env
    if token_file is not None:
        path = Path(token_file).expanduser()
        explicit = True
    else:
        path = _default_token_path(Path(root).expanduser() if root else Path.cwd())
        explicit = False
    if not path.exists():
        if explicit:
            raise EngineConfigError(f"engine token file does not exist: {path}")
        raise EngineConfigError(
            "no engine token configured; pass token/token_file or set "
            f"{TOKEN_ENV}, or run `ozagia engine token --root <repo>`")
    return _read_token_file(path)


def _base_url(value: str | None) -> str:
    text = (value or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).strip()
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EngineConfigError(
            f"engine base URL must be an http(s) origin, got {text!r}")
    if parsed.username or parsed.password:
        raise EngineConfigError("the engine base URL must not contain userinfo")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise EngineConfigError(
            "the engine base URL must be an origin without a path, query or fragment")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


# --------------------------------------------------------------------------- #
# context-request validation
# --------------------------------------------------------------------------- #
def _context_task(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EngineConfigError("task must be a non-empty string")
    if len(value) > MAX_TASK_CHARS:
        raise EngineConfigError(
            f"task must be at most {MAX_TASK_CHARS} characters")
    return value


def _context_int(name: str, value: Any, minimum: int, maximum: int) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or not minimum <= value <= maximum):
        raise EngineConfigError(
            f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _context_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise EngineConfigError(f"{name} must be a boolean")
    return value


class GraphEngineClient:
    """One stateful graph-engine session over a loopback HTTP endpoint.

    ``session`` resumes an existing server session.  Without it the client
    calls ``hello`` lazily before the first data operation.  The client never
    calls ``hello`` twice behind the caller's back and never retries a request.
    """

    def __init__(self, base_url: str | None = None, *,
                 token: str | None = None,
                 token_file: str | Path | None = None,
                 root: str | Path | None = None,
                 timeout: float = 15.0,
                 budget: int = DEFAULT_BUDGET,
                 session: str | None = None,
                 spec: bool = False,
                 known: bool = True,
                 known_limit: int = 1000,
                 known_per_request: int = 64,
                 raise_on_error: bool = True,
                 user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.base_url = _base_url(base_url)
        self.timeout = float(timeout)
        if not self.timeout > 0:
            raise EngineConfigError("timeout must be positive")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
            raise EngineConfigError("budget must be a positive integer")
        if isinstance(known_limit, bool) or not isinstance(known_limit, int) or known_limit < 0:
            raise EngineConfigError("known_limit must be a non-negative integer")
        if (isinstance(known_per_request, bool)
                or not isinstance(known_per_request, int)
                or known_per_request < 0):
            raise EngineConfigError(
                "known_per_request must be a non-negative integer")
        if session is not None and (not isinstance(session, str) or not session):
            raise EngineConfigError("session must be a non-empty string")
        self.budget = budget
        self.spec = bool(spec)
        self.use_known = bool(known)
        self.known_limit = known_limit
        self.known_per_request = known_per_request
        self.raise_on_error = bool(raise_on_error)
        self.user_agent = str(user_agent or DEFAULT_USER_AGENT)
        self.session: str | None = str(session) if session else None
        self.protocol: str | None = None
        self.version: str | None = None
        self.snapshot: str | None = None
        self.freshness: str | None = None

        self._token = _resolve_token(token, token_file, root)
        self._known: dict[tuple[str, str], str] = {}
        self._cols: dict[str, list[str]] = {}
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())

        self.requests = 0
        self.responses = 0
        self.errors = 0
        self.result_errors = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.engine_tokens = 0
        self.last_t: int | None = None
        self.last_op: str | None = None
        self.ops: dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------- #
    def __enter__(self) -> "GraphEngineClient":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:
        return (f"GraphEngineClient(base_url={self.base_url!r}, "
                f"session={self.session!r}, known={len(self._known)})")

    def close(self) -> None:
        """Close an open session without raising; safe to call repeatedly."""
        if self.session is None:
            self._forget_all()
            return
        try:
            self.bye(raise_on_error=False)
        except EngineClientError:
            self.session = None
            self._forget_all()

    # -- handshake ---------------------------------------------------------- #
    def hello(self, *, budget: int | None = None, caps: bool = False,
              detail: bool = False, stats: bool = False, new: bool = False,
              raise_on_error: bool | None = None) -> dict[str, Any]:
        """Start or refresh one session.  ``new`` ignores the current handle."""
        payload: dict[str, Any] = {"op": "hello"}
        if self.session and not new:
            payload["session"] = self.session
        payload["budget"] = int(self.budget if budget is None else budget)
        if caps or detail:
            payload["caps"] = True
        if stats:
            payload["stats"] = True
        result = self._send(payload, "hello")
        self._raise_result(result, raise_on_error)
        sid = result.get("sid")
        if isinstance(sid, str) and sid:
            if self.session and sid != self.session:
                self._forget_all()
            self.session = sid
        protocol = result.get("p")
        if isinstance(protocol, str):
            self.protocol = protocol
        version = result.get("v")
        if isinstance(version, str):
            self.version = version
        snapshot = result.get("s")
        if isinstance(snapshot, str):
            self.snapshot = snapshot
        freshness = result.get("f")
        if isinstance(freshness, str):
            self.freshness = freshness
        return result

    def bye(self, *, raise_on_error: bool | None = None) -> dict[str, Any]:
        """Close the server session and clear client-side remembered state."""
        if self.session is None:
            raise EngineClientError("no open engine session; call hello first")
        payload = {"op": "bye", "session": self.session}
        try:
            result = self._send(payload, "bye")
            self._raise_result(result, raise_on_error)
            return result
        finally:
            self.session = None
            self.snapshot = None
            self.freshness = None
            self._forget_all()

    # -- operations --------------------------------------------------------- #
    def find(self, query: str, *, limit: int = 20, offset: int = 0,
             budget: int | None = None, spec: bool | None = None,
             inline: bool | None = None, known: Any = None,
             raise_on_error: bool | None = None,
             **options: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": "find", "q": query}
        if limit != 20:
            payload["limit"] = int(limit)
        if offset:
            payload["offset"] = int(offset)
        wanted_spec = self.spec if spec is None else bool(spec)
        if wanted_spec:
            payload["spec"] = True
        if inline is not None:
            payload["inline"] = bool(inline)
        payload.update(options)
        payload["op"], payload["q"] = "find", query
        return self.request(payload, budget=budget, known=known,
                            raise_on_error=raise_on_error)

    def outline(self, key: str, *, limit: int = 40, offset: int = 0,
                budget: int | None = None, known: Any = None,
                raise_on_error: bool | None = None,
                **options: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": "outline", "key": key}
        if limit != 40:
            payload["limit"] = int(limit)
        if offset:
            payload["offset"] = int(offset)
        payload.update(options)
        payload["op"], payload["key"] = "outline", key
        return self.request(payload, budget=budget, known=known,
                            raise_on_error=raise_on_error)

    def expand(self, keys: Sequence[str], *, edges: Sequence[str] | None = None,
               depth: int = 1, limit: int = 40, offset: int = 0,
               budget: int | None = None,
               raise_on_error: bool | None = None,
               **options: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": "expand", "keys": list(keys)}
        if edges is not None:
            payload["edges"] = list(edges)
        if depth != 1:
            payload["depth"] = int(depth)
        if limit != 40:
            payload["limit"] = int(limit)
        if offset:
            payload["offset"] = int(offset)
        payload.update(options)
        payload["op"], payload["keys"] = "expand", list(keys)
        return self.request(payload, budget=budget, known=False,
                            raise_on_error=raise_on_error)

    def impact(self, keys: Sequence[str], *, depth: int = 2, limit: int = 40,
               budget: int | None = None,
               raise_on_error: bool | None = None,
               **options: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": "impact", "keys": list(keys)}
        if depth != 2:
            payload["depth"] = int(depth)
        if limit != 40:
            payload["limit"] = int(limit)
        payload.update(options)
        payload["op"], payload["keys"] = "impact", list(keys)
        return self.request(payload, budget=budget, known=False,
                            raise_on_error=raise_on_error)

    def tests(self, keys: Sequence[str] | None = None, *, key: str | None = None,
              limit: int = 40, budget: int | None = None,
              raise_on_error: bool | None = None,
              **options: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": "tests"}
        if key is not None:
            payload["key"] = key
        if keys is not None:
            payload["keys"] = list(keys)
        if limit != 40:
            payload["limit"] = int(limit)
        payload.update(options)
        payload["op"] = "tests"
        if key is not None:
            payload["key"] = key
        if keys is not None:
            payload["keys"] = list(keys)
        return self.request(payload, budget=budget, known=False,
                            raise_on_error=raise_on_error)

    def context(self, task: str, *, limit: int = 8, offset: int = 0,
                mode: str | None = None, budget: int | None = None,
                known: Any = None, raise_on_error: bool | None = None,
                **options: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": "context", "task": task}
        if limit != 8:
            payload["limit"] = int(limit)
        if offset:
            payload["offset"] = int(offset)
        if mode is not None:
            payload["mode"] = mode
        payload.update(options)
        payload["op"], payload["task"] = "context", task
        return self.request(payload, budget=budget, known=known,
                            raise_on_error=raise_on_error)

    def shape(self, *, limit: int = 20, budget: int | None = None,
              raise_on_error: bool | None = None,
              **options: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"op": "shape"}
        if limit != 20:
            payload["limit"] = int(limit)
        payload.update(options)
        payload["op"] = "shape"
        return self.request(payload, budget=budget, known=False,
                            raise_on_error=raise_on_error)

    def fetch(self, handles: Sequence[str] | str | None = None, *,
              handle: str | None = None, wait_ms: int = 0,
              budget: int | None = None,
              raise_on_error: bool | None = None,
              **options: Any) -> dict[str, Any]:
        if handles is None and handle is not None:
            handles = handle
        if handles is None:
            wanted: list[str] = []
        elif isinstance(handles, str):
            wanted = [handles]
        else:
            wanted = [str(item) for item in handles]
        payload: dict[str, Any] = {"op": "fetch", "fetch": wanted}
        if wait_ms:
            payload["wait_ms"] = int(wait_ms)
        payload.update(options)
        payload["op"], payload["fetch"] = "fetch", wanted
        return self.request(payload, budget=budget, known=False,
                            raise_on_error=raise_on_error)

    def healthz(self) -> dict[str, Any]:
        """Liveness only; the transport does not require a token for this."""
        result = self._http("GET", HEALTH_PATH, None, auth=False)
        self._account("healthz", result)
        return result

    # -- Ozma task context (session-independent) --------------------------- #
    def task_context(self, task: str, *, budget_tokens: int = CONTEXT_DEFAULT_BUDGET_TOKENS,
                     max_items: int = CONTEXT_DEFAULT_MAX_ITEMS,
                     allow_stale: bool = False,
                     raise_on_error: bool | None = None) -> dict[str, Any]:
        """Return the Ozma provider result from ``POST /v1/ozma/context``.

        This is deliberately independent of the engine session: it never calls
        ``hello``, never sends or creates a ``session``, and never reads or
        writes remembered ``known`` hashes.  A fresh client can assemble task
        context before (or without) starting a graph session.

        ``task`` must be a non-empty string of at most :data:`MAX_TASK_CHARS`
        characters.  ``budget_tokens`` and ``max_items`` must be integers in
        the provider's inclusive bounds (1..32000 and 1..100), and
        ``allow_stale`` must be a bool.  Invalid arguments raise
        :class:`EngineConfigError` before any request; unsupported keyword
        arguments are rejected by the signature rather than forwarded.

        A ``400 invalid_context_request`` becomes :class:`EngineContextError`
        (an :class:`EngineResultError`); ``401`` becomes
        :class:`EngineAuthError`, ``503`` becomes
        :class:`EngineUnavailableError`, and transport failures become
        :class:`EngineTransportError`.  ``raise_on_error=False`` returns the
        stable ``{"e": "invalid_context_request", "why": ...}`` payload for a
        400 instead of raising; auth and availability failures still raise.
        No request is retried.
        """
        payload = {
            "task": _context_task(task),
            "budget_tokens": _context_int(
                "budget_tokens", budget_tokens,
                CONTEXT_MIN_BUDGET_TOKENS, CONTEXT_MAX_BUDGET_TOKENS),
            "max_items": _context_int(
                "max_items", max_items, CONTEXT_MIN_ITEMS, CONTEXT_MAX_ITEMS),
            "allow_stale": _context_bool("allow_stale", allow_stale),
        }
        try:
            result = self._http("POST", CONTEXT_PATH, payload)
        except EngineHTTPStatusError as exc:
            if exc.status != 400 or exc.code != "invalid_context_request":
                raise
            failure = {"e": exc.code, "why": exc.message, "status": exc.status}
            self.result_errors += 1
            wanted = self.raise_on_error if raise_on_error is None else bool(raise_on_error)
            if wanted:
                raise EngineContextError(failure, status=exc.status) from None
            return failure
        self._account(CONTEXT_OP, result)
        self._raise_result(result, raise_on_error)
        return result

    # -- low-level managed request ----------------------------------------- #
    def request(self, payload: dict[str, Any], *, budget: int | None = None,
                known: Any = None, raise_on_error: bool | None = None) -> dict[str, Any]:
        """Send one engine request with session and ``known`` management.

        ``known`` is normally automatic.  ``known=False`` sends an explicit
        empty list for ``find``/``outline``/``context`` (the documented
        full-refresh opt-out, which also defeats a server-side auto-known), and
        an explicit iterable sends exactly those entries.  The
        ``raise_errors=False`` escape hatch is available on every convenience
        method for budget/withheld payloads.
        """
        if not isinstance(payload, dict):
            raise EngineConfigError("an engine request must be a JSON object")
        body = dict(payload)
        op = str(body.get("op") or "").strip()
        if not op:
            raise EngineConfigError("an engine request needs op")
        if op == "hello":
            wanted_budget = budget if budget is not None else body.get("budget")
            return self.hello(
                budget=wanted_budget,
                caps=bool(body.get("caps") or body.get("detail")),
                stats=bool(body.get("stats")),
                new=not bool(body.get("session")),
                raise_on_error=raise_on_error)
        if op == "bye":
            return self.bye(raise_on_error=raise_on_error)
        self._ensure_session()
        supplied = body.get("session")
        if supplied is not None and supplied != self.session:
            raise EngineConfigError(
                "the request session does not match this client's session")
        body["session"] = self.session
        if budget is not None:
            body["budget"] = int(budget)
        elif "budget" not in body:
            body["budget"] = int(self.budget)
        self._apply_known(op, body, known)
        result = self._send(body, op)
        self._remember_rows(op, result)
        self._raise_result(result, raise_on_error)
        return result

    # Aliases that read naturally for callers using the transport vocabulary.
    graph = request
    call = request

    # -- paging ------------------------------------------------------------- #
    @staticmethod
    def next_request(result: Any) -> dict[str, Any] | None:
        """The engine's self-contained ``n`` request, or ``None``."""
        if isinstance(result, EngineResultError):
            result = result.payload
        if not isinstance(result, dict):
            return None
        value = result.get("n")
        if not isinstance(value, dict):
            value = result.get("next")
        return dict(value) if isinstance(value, dict) else None

    def next_page(self, result: Any, *, budget: int | None = None,
                  known: Any = None,
                  raise_on_error: bool | None = None) -> dict[str, Any]:
        """Follow one ``n`` continuation.  Bounded: the caller controls loops."""
        value = self.next_request(result)
        if value is None:
            raise EnginePagingError("the result has no next page")
        return self.request(value, budget=budget, known=known,
                            raise_on_error=raise_on_error)

    # -- remembered hashes -------------------------------------------------- #
    def known_entries(self, op: str | None = None) -> list[dict[str, str]]:
        """A copy of the ``{op,key,hash}`` entries the client remembers."""
        if op is not None and op not in KNOWN_OPS:
            raise EngineConfigError(
                f"known entries are only for {', '.join(KNOWN_OPS)}")
        return [{"op": entry_op, "key": key, "hash": digest}
                for (entry_op, key), digest in self._known.items()
                if op is None or entry_op == op]

    def remember(self, op: str, key: str, digest: str) -> None:
        """Seed one remembered hash (for a persisted cache or a test)."""
        if op not in KNOWN_OPS:
            raise EngineConfigError(
                f"known entries are only for {', '.join(KNOWN_OPS)}")
        if not isinstance(key, str) or not key:
            raise EngineConfigError("key must be a non-empty string")
        if not isinstance(digest, str) or not digest:
            raise EngineConfigError("hash must be a non-empty string")
        self._remember(op, key, digest)

    def forget(self, key: str, *, op: str | None = None) -> int:
        """Forget one key (for one op, or every op); return the count removed."""
        if op is not None:
            if op not in KNOWN_OPS:
                raise EngineConfigError(
                    f"known entries are only for {', '.join(KNOWN_OPS)}")
            return 1 if self._known.pop((op, key), None) is not None else 0
        removed = 0
        for entry_op in KNOWN_OPS:
            if self._known.pop((entry_op, key), None) is not None:
                removed += 1
        return removed

    def clear_known(self, *, op: str | None = None) -> None:
        """Forget all remembered hashes, optionally only one operation's."""
        if op is None:
            self._known.clear()
            return
        if op not in KNOWN_OPS:
            raise EngineConfigError(
                f"known entries are only for {', '.join(KNOWN_OPS)}")
        for key in [key for (value_op, key) in self._known if value_op == op]:
            self._known.pop((op, key), None)

    # -- counters ----------------------------------------------------------- #
    def stats(self) -> dict[str, Any]:
        """Compact, non-secret counters for logs and assertions."""
        return {
            "session": self.session,
            "protocol": self.protocol,
            "freshness": self.freshness,
            "requests": self.requests,
            "responses": self.responses,
            "errors": self.errors,
            "result_errors": self.result_errors,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
            "engine_tokens": self.engine_tokens,
            "last_t": self.last_t,
            "last_op": self.last_op,
            "known": len(self._known),
            "budget": self.budget,
            "ops": dict(self.ops),
        }

    # -- internals ---------------------------------------------------------- #
    def _ensure_session(self) -> None:
        if self.session is None:
            self.hello()

    def _send(self, payload: dict[str, Any], op: str) -> dict[str, Any]:
        result = self._http("POST", GRAPH_PATH, payload)
        self._account(op, result)
        return result

    def _account(self, op: str, result: dict[str, Any]) -> None:
        self.responses += 1
        self.last_op = op
        self.ops[op] = self.ops.get(op, 0) + 1
        tokens = result.get("t")
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            self.engine_tokens += tokens
            self.last_t = tokens

    def _apply_known(self, op: str, body: dict[str, Any], known: Any) -> None:
        if known is False:
            # An explicit empty list, not an omitted key: a serving engine may
            # auto-apply remembered hashes when `known` is absent, and this is
            # the documented opt-out for a full refresh.
            if op in KNOWN_OPS:
                body["known"] = []
            else:
                body.pop("known", None)
            return
        if known is not None and known is not True:
            body["known"] = [dict(entry) for entry in known]
            return
        if "known" in body or not self.use_known or op not in KNOWN_OPS:
            return
        entries = [{"op": entry_op, "key": key, "hash": digest}
                   for (entry_op, key), digest in self._known.items()
                   if entry_op == op]
        if self.known_per_request <= 0:
            entries = []
        elif len(entries) > self.known_per_request:
            entries = entries[-self.known_per_request:]
        if entries:
            body["known"] = entries

    def _remember(self, op: str, key: str, digest: str) -> None:
        entry = (op, key)
        self._known.pop(entry, None)
        self._known[entry] = digest
        while len(self._known) > self.known_limit:
            self._known.popitem(last=False)

    def _remember_rows(self, op: str, payload: dict[str, Any]) -> None:
        if op not in KNOWN_OPS:
            return
        cols = payload.get("c")
        if not isinstance(cols, list):
            cols = payload.get("cols")
        if isinstance(cols, list) and cols:
            self._cols[op] = [str(value) for value in cols]
        rows = payload.get("r")
        if not isinstance(rows, list):
            rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            return
        columns = self._cols.get(op)
        if not columns or "k" not in columns or "h" not in columns:
            return
        key_at = columns.index("k")
        hash_at = columns.index("h")
        for row in rows:
            if isinstance(row, dict):
                key = row.get("k", row.get("key"))
                digest = row.get("h", row.get("hash"))
            elif isinstance(row, list) and len(row) > max(key_at, hash_at):
                key, digest = row[key_at], row[hash_at]
            else:
                continue
            if isinstance(key, str) and key and isinstance(digest, str) and digest:
                self._remember(op, key, digest)

    def _forget_all(self) -> None:
        self._known.clear()
        self._cols.clear()

    def _raise_result(self, result: dict[str, Any],
                      raise_on_error: bool | None) -> None:
        if "e" not in result:
            return
        self.result_errors += 1
        wanted = self.raise_on_error if raise_on_error is None else bool(raise_on_error)
        if wanted:
            raise self._classify_result(result)

    @staticmethod
    def _classify_result(result: dict[str, Any]) -> EngineResultError:
        code = str(result.get("e") or "engine_error")
        freshness = str(result.get("f") or "")
        lowered = code.lower()
        if code == "budget_too_small":
            return EngineBudgetError(result)
        if freshness in {"stale", "unknown"} or "withheld" in lowered:
            return EngineWithheldError(result)
        if "session" in lowered and ("expired" in lowered or "unknown" in lowered):
            return EngineSessionError(result)
        return EngineResultError(result)

    def _http(self, method: str, path: str, payload: Any, *,
              auth: bool = True) -> dict[str, Any]:
        data: bytes | None = None
        if payload is not None:
            try:
                data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError):
                raise EngineConfigError("the request is not JSON-serialisable") from None
        request = urllib.request.Request(self.base_url + path, data=data,
                                         method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", self.user_agent)
        if auth:
            request.add_header("Authorization", "Bearer " + self._token)
        self.requests += 1
        if data is not None:
            self.bytes_out += len(data)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            self.errors += 1
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            self.bytes_in += len(raw)
            status = int(exc.code)
            code, message = _error_fields(raw)
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if status == 401:
                raise EngineAuthError(status, code, message,
                                      retry_after=retry_after) from None
            if status == 503:
                raise EngineUnavailableError(status, code, message,
                                             retry_after=retry_after) from None
            raise EngineHTTPStatusError(status, code, message,
                                        retry_after=retry_after) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self.errors += 1
            reason = getattr(exc, "reason", exc)
            raise EngineTransportError(
                f"engine request failed at {self.base_url}: {reason}") from None
        self.bytes_in += len(raw)
        if len(raw) > MAX_RESPONSE_BYTES:
            self.errors += 1
            raise EngineTransportError("engine response exceeds the client limit")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.errors += 1
            raise EngineTransportError(
                "engine returned a non-JSON response") from None
        if not isinstance(result, dict):
            self.errors += 1
            raise EngineTransportError("engine returned a non-object JSON value")
        return result


# The live driver's name for the same role; kept as a small compatibility alias.
EngineClient = GraphEngineClient


def _error_fields(raw: bytes) -> tuple[str, str]:
    """Stable code/message from an HTTP error body; never return the body."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "http_error", ""
    error = value.get("error") if isinstance(value, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "http_error")[:160]
        message = str(error.get("message") or "")[:240]
        return code, message
    if isinstance(error, str):
        return "http_error", error[:240]
    return "http_error", ""


__all__ = [
    "BASE_URL_ENV", "CONTEXT_DEFAULT_BUDGET_TOKENS", "CONTEXT_DEFAULT_MAX_ITEMS",
    "CONTEXT_MAX_BUDGET_TOKENS", "CONTEXT_MAX_ITEMS", "CONTEXT_MIN_BUDGET_TOKENS",
    "CONTEXT_MIN_ITEMS", "CONTEXT_OP", "CONTEXT_PATH", "DEFAULT_BASE_URL",
    "DEFAULT_BUDGET", "EngineAuthError", "EngineBudgetError", "EngineClient",
    "EngineClientError", "EngineConfigError", "EngineContextError",
    "EngineHTTPStatusError", "EnginePagingError", "EngineResultError",
    "EngineSessionError", "EngineTransportError", "EngineUnavailableError",
    "EngineWithheldError", "GRAPH_PATH", "GraphEngineClient", "HEALTH_PATH",
    "KNOWN_OPS", "MAX_TASK_CHARS", "TOKEN_ENV",
]
