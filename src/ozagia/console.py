"""A loopback-only, read-only web console over existing ozagia reports.

The console is a thin local shell.  It composes:

* ``core.status.status`` for the repository snapshot;
* ``core.apertures`` and ``core.view`` for graph projections;
* ``core.worktree.check`` for worktree, branch and drift checks;
* ``core.resources.status`` for host observation;
* ``core.testing.plan`` for a deterministic test-selection plan;
* ``core.research_ui`` for recorded research views;
* ``core.files`` for the created-file ledger;
* ``core.actions`` for a list and preview of process actions.

It never applies an action, writes to the repository or its state directory,
builds or refreshes the graph, calls a model or makes a network request.  By
default the only listener is a loopback HTTP server.  A token (or the cookie
set after a token-authenticated request) is required for every ``/api/*``
endpoint.

Usage::

    python -m ozagia.console --root . --port 0 --open
    python -m ozagia.console --root . --host 127.0.0.1 --port 8765

The console deliberately has no static-file route.  Unknown paths are 404s;
there is no directory traversal surface.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.config import repo_root, state_path
from ozagia.core import actions as core_actions
from ozagia.core import apertures as core_apertures
from ozagia.core import files as core_files
from ozagia.core import research_ui as core_research_ui
from ozagia.core import resources as core_resources
from ozagia.core import status as core_status
from ozagia.core import testing as core_testing
from ozagia.core import view as core_view
from ozagia.core import worktree as core_worktree

LOGGER = logging.getLogger("ozagia.console")

SCHEMA = 1
COOKIE_NAME = "ozagia_console"
COOKIE_MAX_AGE = 12 * 60 * 60
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 0
DEFAULT_TOP = 10
DEFAULT_FILES_LIMIT = 200
MAX_FILES_LIMIT = 1000
MAX_ORPHANS = 200
MAX_QUERY_BYTES = 4_096
MAX_QUERY_PARAMS = 32
MAX_PARAM_KEY = 64
MAX_PARAM_VALUE = 1_024
MAX_REQUEST_TARGET = 8_192
MAX_VIEW_DEPTH = 32
MAX_TOP = 50
RATE_LIMIT_REQUESTS = 240
RATE_LIMIT_WINDOW_S = 10.0
SERVER_TIMEOUT_S = 20.0

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_INVESTIGATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^-]{0,127}$")
_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_APERTURE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
_CLASSIFICATION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_STATUS_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_TOKEN_QUERY_RE = re.compile(r"(token=)[^&\s\"']+")
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.", "127.0.0.1", "::1"})


class ConsoleError(ValueError):
    """The console request or configuration is invalid."""


class RequestError(Exception):
    """An HTTP-facing error with a stable JSON code."""

    def __init__(self, status: int, code: str, message: str, *,
                 headers: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.headers = dict(headers or {})


@dataclass
class _RateLimiter:
    """A small per-client sliding-window limiter."""

    limit: int = RATE_LIMIT_REQUESTS
    window_s: float = RATE_LIMIT_WINDOW_S
    _hits: dict[str, deque[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            queue = self._hits.get(key)
            if queue is None:
                queue = deque()
                self._hits[key] = queue
            cutoff = now - self.window_s
            while queue and queue[0] <= cutoff:
                queue.popleft()
            if len(queue) >= self.limit:
                return False
            queue.append(now)
            if len(self._hits) > 1_024:
                for old_key, old_queue in list(self._hits.items()):
                    if not old_queue or old_queue[-1] <= cutoff:
                        self._hits.pop(old_key, None)
            return True


@dataclass(frozen=True)
class ConsoleState:
    root: Path
    token: str
    host: str
    display_host: str
    remote: bool
    limiter: _RateLimiter = field(default_factory=_RateLimiter)


class ConsoleServer(ThreadingHTTPServer):
    """A threaded HTTP server carrying the console's immutable state."""

    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False

    def __init__(self, server_address: tuple[str, int],
                 handler_class: type[BaseHTTPRequestHandler], *,
                 state: ConsoleState) -> None:
        self.state = state
        super().__init__(server_address, handler_class)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        query = urllib.parse.urlencode({"token": self.state.token})
        host = self.state.display_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}/?{query}"


# --------------------------------------------------------------------------- #
# validation and configuration
# --------------------------------------------------------------------------- #
def _loopback_host(host: str) -> bool:
    name = host.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _bind_host(host: str) -> tuple[str, int]:
    name = host.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name.lower() in {"localhost", "localhost."}:
        name = "127.0.0.1"
    family = socket.AF_INET
    try:
        address = ipaddress.ip_address(name.split("%", 1)[0])
        if address.version == 6:
            family = socket.AF_INET6
    except ValueError:
        pass
    return name, family


def _display_host(host: str) -> str:
    name = host.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name.lower() in {"localhost", "localhost."}:
        return "127.0.0.1"
    if name in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return name


def _token(value: str | None, *, remote: bool) -> str:
    if value is None:
        if remote:
            raise ConsoleError(
                "a non-loopback host requires an explicit --token value")
        value = secrets.token_urlsafe(32)
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise ConsoleError(
            "token must be 1-128 characters from A-Z, a-z, 0-9, '-' and '_'")
    return value


def _port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise ConsoleError("port must be an integer between 0 and 65535")
    return value


def create_server(root: str | Path, *, host: str = DEFAULT_HOST,
                  port: int = DEFAULT_PORT, token: str | None = None,
                  allow_remote: bool = False) -> ConsoleServer:
    """Create (but do not serve) a bound console server.

    Binding a non-loopback address is refused unless ``allow_remote=True`` and
    an explicit token is supplied.  No request is accepted until
    ``serve_forever`` is called.
    """
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ConsoleError(f"repository root does not exist: {root_path}")
    if not isinstance(host, str) or not host.strip():
        raise ConsoleError("host must be a non-empty string")
    if not _loopback_host(host) and not allow_remote:
        raise ConsoleError(
            f"refusing non-loopback host {host!r}; the console is loopback-only "
            "(pass --allow-remote with an explicit token to override)")
    if not _loopback_host(host) and token is None:
        raise ConsoleError(
            "a non-loopback host requires an explicit --token value")
    token_value = _token(token, remote=not _loopback_host(host))
    bind_host, family = _bind_host(host)
    limiter = _RateLimiter()
    state = ConsoleState(
        root=root_path, token=token_value, host=bind_host,
        display_host=_display_host(host), remote=not _loopback_host(host),
        limiter=limiter,
    )

    class _Server(ConsoleServer):
        address_family = family

    try:
        return _Server((bind_host, _port(port)), _ConsoleHandler, state=state)
    except OSError as exc:
        raise ConsoleError(f"cannot bind {bind_host}:{port}: {exc}") from None


def serve(root: str | Path, *, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT, token: str | None = None,
          open_browser: bool = False,
          allow_remote: bool = False) -> None:
    """Serve the read-only console until Ctrl-C or ``shutdown()``.

    The URL is printed with its token.  With ``open_browser`` the default
    browser is asked to open that exact loopback URL.
    """
    server = create_server(root, host=host, port=port, token=token,
                           allow_remote=allow_remote)
    url = server.url
    print(f"ozagia console (read-only): {url}", flush=True)
    print("Security: the URL and cookie are bearer credentials. Do not share "
          "them.", flush=True)
    if server.state.remote:
        print("WARNING: a non-loopback host is bound. HTTP is unencrypted; "
              "use only on a trusted network.", file=sys.stderr, flush=True)
    else:
        print("Binding: loopback only.", flush=True)
    print("Press Ctrl-C to stop.", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - browser launch is best effort
            LOGGER.warning("could not open a browser", exc_info=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nozagia console stopped.")
    finally:
        server.server_close()


# --------------------------------------------------------------------------- #
# query parsing
# --------------------------------------------------------------------------- #
def _parse_query(raw: str) -> dict[str, str]:
    if len(raw.encode("utf-8", "replace")) > MAX_QUERY_BYTES:
        raise RequestError(HTTPStatus.REQUEST_URI_TOO_LONG, "query_too_large",
                           f"query string exceeds {MAX_QUERY_BYTES} bytes")
    if not raw:
        return {}
    pieces = raw.split("&")
    if len(pieces) > MAX_QUERY_PARAMS:
        raise RequestError(HTTPStatus.BAD_REQUEST, "too_many_parameters",
                           f"query string has more than {MAX_QUERY_PARAMS} parameters")
    params: dict[str, str] = {}
    for piece in pieces:
        if not piece:
            continue
        key, _, value = piece.partition("=")
        key = urllib.parse.unquote_plus(key)
        value = urllib.parse.unquote_plus(value)
        if not key or len(key) > MAX_PARAM_KEY:
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                               "query parameter name is empty or too long")
        if len(value) > MAX_PARAM_VALUE:
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                               f"query parameter {key!r} is too long")
        if key in params:
            raise RequestError(HTTPStatus.BAD_REQUEST, "duplicate_parameter",
                               f"query parameter {key!r} is repeated")
        params[key] = value
    return params


def _only(params: Mapping[str, str], allowed: Iterable[str]) -> None:
    unknown = sorted(set(params) - set(allowed) - {"token"})
    if unknown:
        raise RequestError(
            HTTPStatus.BAD_REQUEST, "unsupported_parameter",
            "unsupported query parameter(s): " + ", ".join(unknown))


def _text_param(params: Mapping[str, str], name: str, *, default: str | None,
                pattern: Any = None, label: str | None = None) -> str | None:
    value = params.get(name)
    if value is None or value == "":
        return default
    if pattern is not None and not pattern.fullmatch(value):
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                           f"{label or name} has an invalid value")
    return value


def _int_param(params: Mapping[str, str], name: str, *, default: int,
               minimum: int, maximum: int) -> int:
    value = params.get(name)
    if value is None or value == "":
        return default
    if not value.isascii() or not value.isdigit():
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                           f"{name} must be an integer")
    number = int(value)
    if not minimum <= number <= maximum:
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                           f"{name} must be between {minimum} and {maximum}")
    return number


def _float_param(params: Mapping[str, str], name: str, *,
                 minimum: float, maximum: float) -> float | None:
    value = params.get(name)
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except ValueError:
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                           f"{name} must be a number") from None
    if not minimum <= number <= maximum:
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                           f"{name} must be between {minimum:g} and {maximum:g}")
    return number


def _bool_param(params: Mapping[str, str], name: str) -> bool:
    value = (params.get(name) or "").lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                       f"{name} must be a boolean")


# --------------------------------------------------------------------------- #
# API payloads
# --------------------------------------------------------------------------- #
def _status_payload(root: Path, params: Mapping[str, str]) -> Any:
    _only(params, {"top"})
    if params.get("top") in (None, ""):
        return core_status.status(root)
    top = _int_param(params, "top", default=DEFAULT_TOP, minimum=0,
                     maximum=MAX_TOP)
    return core_status.status(root, top=top)


def _apertures_payload(root: Path, params: Mapping[str, str]) -> Any:
    _only(params, set())
    return core_apertures.available_apertures(root)


def _view_payload(root: Path, params: Mapping[str, str]) -> Any:
    _only(params, {"aperture", "depth", "node"})
    aperture = _text_param(params, "aperture", default="architecture",
                           pattern=_APERTURE_RE, label="aperture")
    depth_raw = params.get("depth")
    depth: int | None = None
    if depth_raw not in (None, ""):
        if not depth_raw.isascii() or not depth_raw.isdigit():
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                               "depth must be an integer >= 0")
        depth = int(depth_raw)
        if depth > MAX_VIEW_DEPTH:
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                               f"depth must be <= {MAX_VIEW_DEPTH}")
    node = _text_param(params, "node", default=None)
    graph_path = state_path(root, "graph.json")
    if not graph_path.is_file():
        raise RequestError(
            HTTPStatus.NOT_FOUND, "missing_graph",
            f"graph snapshot not found: {graph_path}. Build the index first "
            "(python -m ozagia.core.memory build); the console never builds it.")
    try:
        graph = core_view.read_graph(graph_path)
    except FileNotFoundError:
        raise RequestError(HTTPStatus.NOT_FOUND, "missing_graph",
                           "graph snapshot disappeared before it could be read")
    except core_view.ViewError as exc:
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_graph", str(exc)) from None
    try:
        definition = core_apertures.get_aperture(aperture, root=root)
    except core_apertures.ApertureError as exc:
        raise RequestError(HTTPStatus.BAD_REQUEST, "unknown_aperture",
                           str(exc)) from None
    kwargs: dict[str, Any] = {"root": root, "source": graph_path}
    if depth is not None:
        kwargs["depth"] = depth
    if node:
        kwargs["scope_root"] = node
    try:
        return core_view.build_view(graph, definition, **kwargs)
    except core_view.ViewError as exc:
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_view", str(exc)) from None


def _worktree_payload(root: Path, params: Mapping[str, str]) -> Any:
    _only(params, {"base"})
    if params.get("base") in (None, ""):
        return core_worktree.check(root)
    base = _text_param(params, "base", default="main", pattern=_REF_RE,
                       label="base")
    return core_worktree.check(root, base=base)


def _resources_payload(root: Path, params: Mapping[str, str]) -> Any:
    _only(params, set())
    return core_resources.status(root)


def _testing_payload(root: Path, params: Mapping[str, str]) -> Any:
    _only(params, {"base", "head", "task"})
    base = _text_param(params, "base", default="HEAD", pattern=_REF_RE,
                       label="base")
    head = _text_param(params, "head", default=None, pattern=_REF_RE,
                       label="head")
    task = _text_param(params, "task", default=None, pattern=_TASK_RE,
                       label="task class")
    return core_testing.plan(root, base=base, head=head, task_class=task)


def _research_payload(root: Path, params: Mapping[str, str]) -> Any:
    _only(params, {"investigation", "view"})
    investigation = _text_param(
        params, "investigation", default=None, pattern=_INVESTIGATION_RE,
        label="investigation")
    if investigation is None:
        raise RequestError(HTTPStatus.BAD_REQUEST, "missing_parameter",
                           "investigation is required")
    view = _text_param(params, "view", default="library")
    if view not in core_research_ui.VIEWS:
        raise RequestError(
            HTTPStatus.BAD_REQUEST, "unknown_research_view",
            "view must be one of " + ", ".join(core_research_ui.VIEWS))
    try:
        state, graph_path = core_research_ui.load_state(
            investigation, root=root)
    except core_research_ui.ResearchUIError as exc:
        message = str(exc)
        if "not found" in message:
            raise RequestError(HTTPStatus.NOT_FOUND, "missing_investigation",
                               message) from None
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_investigation",
                           message) from None
    try:
        return core_research_ui.build_research_view(
            state, view, root=root, source=graph_path,
            investigation_id=investigation)
    except core_research_ui.ResearchUIError as exc:
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_research_view",
                           str(exc)) from None


def _files_payload(root: Path, params: Mapping[str, str]) -> dict[str, Any]:
    _only(params, {"classification", "status", "limit"})
    classification = _text_param(
        params, "classification", default=None,
        pattern=_CLASSIFICATION_RE, label="classification")
    status = _text_param(params, "status", default="active",
                         pattern=_STATUS_RE, label="status")
    limit = _int_param(params, "limit", default=DEFAULT_FILES_LIMIT,
                       minimum=1, maximum=MAX_FILES_LIMIT)
    filters: dict[str, Any] = {}
    if "classification" in params:
        filters["classification"] = (
            None if classification in (None, "all") else classification)
    if "status" in params:
        filters["status"] = None if status in (None, "all") else status
    if "limit" in params:
        filters["limit"] = limit
    records = (core_files.list_files(root, **filters) if filters
               else core_files.list_files(root))
    orphan_rows = core_files.orphans(root)
    ledger = core_files.ledger_path(root)
    return {
        "schema": SCHEMA,
        "read_only": True,
        "ledger": {
            "path": str(ledger),
            "present": ledger.is_file(),
        },
        "records": records,
        "list": records,
        "orphans": orphan_rows[:MAX_ORPHANS],
        "counts": {
            "records": len(records),
            "records_limit": limit,
            "orphans": len(orphan_rows),
            "orphans_shown": min(len(orphan_rows), MAX_ORPHANS),
        },
        "omitted": {
            "records": "at most the requested limit are returned",
            "orphans": (f"orphans are capped at {MAX_ORPHANS}"
                        if len(orphan_rows) > MAX_ORPHANS else "none"),
        },
    }


def _action_preview(root: Path, action: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """Describe an action without issuing a one-time apply token.

    ``core.actions.preview`` is deliberately not called: it writes a pending
    token under the state directory.  The console is read-only, so it uses the
    same canonical argument validation and preview dispatch but skips token
    issuance entirely.  Apply is not exposed anywhere in this server.
    """
    canonical = core_actions._canonical(action, args)  # noqa: SLF001
    result = core_actions._dispatch_preview(  # noqa: SLF001
        root, action, canonical, core_actions.ActionContext())
    payload = result.to_dict()
    payload["mode"] = "preview"
    payload["confirmation"] = None
    payload["apply_available"] = False
    payload["read_only"] = True
    payload["note"] = (
        "Console preview only. No confirmation token was issued and this "
        "server has no apply endpoint.")
    return payload


def _actions_payload(root: Path, params: Mapping[str, str]) -> Any:
    _only(params, {
        "action", "name", "scope", "max_bytes", "wait", "idle_timeout",
        "live", "network",
    })
    action = _text_param(params, "action", default=None)
    if action is None:
        # The list mode mirrors ``core.actions.list_actions`` directly. This
        # server has no apply endpoint; a preview is requested separately.
        return core_actions.list_actions()
    name = _text_param(params, "name", default=None, pattern=_NAME_RE,
                       label="name")
    scope = _text_param(params, "scope", default=None)
    if scope not in (None, "project", "user"):
        raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_parameter",
                           "scope must be project or user")
    args: dict[str, Any] = {}
    if name is not None:
        args["name"] = name
    if scope is not None:
        args["scope"] = scope
    if params.get("max_bytes") not in (None, ""):
        args["max_bytes"] = _int_param(
            params, "max_bytes", default=4_096, minimum=0, maximum=65_536)
    wait = _float_param(params, "wait", minimum=0.0, maximum=600.0)
    if wait is not None:
        args["wait_s"] = wait
    idle = _float_param(params, "idle_timeout", minimum=0.0, maximum=86_400.0)
    if idle is not None:
        args["idle_timeout_s"] = idle
    if "live" in params:
        args["live"] = _bool_param(params, "live")
    if "network" in params:
        args["network"] = _bool_param(params, "network")
    return _action_preview(root, action, args)


_ROUTES = {
    "/api/status": _status_payload,
    "/api/apertures": _apertures_payload,
    "/api/view": _view_payload,
    "/api/worktree": _worktree_payload,
    "/api/resources": _resources_payload,
    "/api/testing/plan": _testing_payload,
    "/api/research": _research_payload,
    "/api/files": _files_payload,
    "/api/actions": _actions_payload,
}


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def _json_bytes(payload: Any) -> bytes:
    try:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          allow_nan=False, separators=(",", ":"),
                          default=_json_default)
    except (TypeError, ValueError) as exc:
        raise RequestError(HTTPStatus.INTERNAL_SERVER_ERROR, "json_error",
                           "the report could not be serialised") from exc
    return text.encode("utf-8")


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": message},
    }


class _ConsoleHandler(BaseHTTPRequestHandler):
    server: ConsoleServer
    server_version = "ozagia-console"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = SERVER_TIMEOUT_S
    max_headers = 64

    # -- routing -------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_TRACE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        self._send_error(
            HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed",
            "this read-only console accepts GET and HEAD only",
            headers={"Allow": "GET, HEAD"})

    def _handle(self) -> None:
        if len(self.requestline) > MAX_REQUEST_TARGET:
            self.close_connection = True
            self._send_error(HTTPStatus.REQUEST_URI_TOO_LONG,
                             "uri_too_long", "request target is too long")
            return
        client = str(self.client_address[0]) if self.client_address else "unknown"
        if not self.server.state.limiter.allow(client):
            self.close_connection = True
            self._send_error(
                HTTPStatus.TOO_MANY_REQUESTS, "rate_limited",
                "too many requests; retry shortly",
                headers={"Retry-After": "1"})
            return
        try:
            self._reject_body()
            split = urllib.parse.urlsplit(self.path)
            params = _parse_query(split.query)
            path = split.path
            supplied = params.get("token")
            cookie = self._cookie()
            token_ok = ((supplied is not None and self._token_ok(supplied))
                        or (cookie is not None and self._token_ok(cookie)))
            if path == "/api" or path.startswith("/api/"):
                if not token_ok:
                    raise RequestError(
                        HTTPStatus.UNAUTHORIZED, "unauthorized",
                        "missing or invalid token")
            elif path == "/" and supplied is not None and not token_ok:
                raise RequestError(HTTPStatus.UNAUTHORIZED, "unauthorized",
                                   "invalid token")
            cookie_header = None
            if supplied is not None and token_ok:
                cookie_header = self._cookie_header()

            if path == "/":
                self._send_html(_HTML_APP, headers=(
                    {"Set-Cookie": cookie_header} if cookie_header else None))
                return
            handler = _ROUTES.get(path)
            if handler is None:
                raise RequestError(HTTPStatus.NOT_FOUND, "not_found",
                                   "unknown endpoint")
            payload = handler(self.server.state.root, params)
            extra = {"Set-Cookie": cookie_header} if cookie_header else None
            self._send_json(HTTPStatus.OK, payload, headers=extra)
        except RequestError as exc:
            # A rejected body, an oversized target or a rate-limit response
            # cannot safely share this connection. Other errors have no
            # unread body and may keep HTTP/1.1 keep-alive.
            if exc.status >= 500 or exc.status in {
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    HTTPStatus.REQUEST_URI_TOO_LONG,
                    HTTPStatus.TOO_MANY_REQUESTS}:
                self.close_connection = True
            self._send_error(exc.status, exc.code, exc.message,
                             headers=exc.headers)
        except core_apertures.ApertureError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "unknown_aperture", str(exc))
        except core_view.ViewError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_view", str(exc))
        except core_testing.TestPolicyError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "invalid_test_plan", str(exc))
        except core_worktree.WorktreeError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "worktree_error", str(exc))
        except core_files.FileLedgerError as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "ledger_error",
                             str(exc))
        except core_research_ui.ResearchUIError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "research_error", str(exc))
        except core_actions.RuntimeStateError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "action_error", str(exc))
        except core_actions.ActionError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, "action_error", str(exc))
        except core_actions.ActionRefused as exc:
            self._send_error(HTTPStatus.CONFLICT, "action_refused", str(exc))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # pragma: no cover - defensive boundary
            LOGGER.exception("unhandled console request error")
            self.close_connection = True
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error",
                             "the report could not be produced")

    # -- request helpers ------------------------------------------------- #
    def _reject_body(self) -> None:
        length = self.headers.get("Content-Length")
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                               "request_body_not_allowed",
                               "request bodies are not accepted")
        if length is None:
            return
        try:
            size = int(length)
        except ValueError:
            self.close_connection = True
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid_content_length",
                               "invalid Content-Length") from None
        if size > 0:
            self.close_connection = True
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                               "request_body_not_allowed",
                               "request bodies are not accepted")

    def _token_ok(self, value: str) -> bool:
        try:
            return secrets.compare_digest(value, self.server.state.token)
        except TypeError:
            # compare_digest rejects non-ASCII text; a malformed credential is
            # simply not the token rather than an internal error.
            return False

    def _cookie(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw or len(raw) > 4_096:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel is not None else None

    def _cookie_header(self) -> str:
        return (f"{COOKIE_NAME}={self.server.state.token}; Path=/; "
                f"Max-Age={COOKIE_MAX_AGE}; HttpOnly; SameSite=Strict")

    # -- responses ------------------------------------------------------- #
    def _send_json(self, status: int, payload: Any, *,
                   headers: Mapping[str, str] | None = None) -> None:
        try:
            body = _json_bytes(payload)
        except RequestError:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            self.close_connection = True
            body = _json_bytes(_error_payload(
                "json_error", "the report could not be serialised"))
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _send_error(self, status: int, code: str, message: str, *,
                    headers: Mapping[str, str] | None = None) -> None:
        self._send_json(status, _error_payload(code, message), headers=headers)

    def _send_html(self, text: str, *,
                   headers: Mapping[str, str] | None = None) -> None:
        self._send(HTTPStatus.OK, text.encode("utf-8"),
                   "text/html; charset=utf-8", headers)

    def _send(self, status: int, body: bytes, content_type: str,
              headers: Mapping[str, str] | None = None) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Ozagia-Read-Only", "true")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.send_header("Permissions-Policy",
                         "camera=(), microphone=(), geolocation=()")
        if self.close_connection:
            self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    # -- logging --------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:
        message = fmt % args
        message = _TOKEN_QUERY_RE.sub(r"\1[redacted]", message)
        LOGGER.info("%s %s", self.address_string(), message)


# --------------------------------------------------------------------------- #
# self-contained HTML app
# --------------------------------------------------------------------------- #
_HTML_APP = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'">
<title>ozagia console - read-only</title>
<style>
:root {
  color-scheme: light;
  --ink: #1d2733;
  --muted: #52616f;
  --line: #d9e1e8;
  --bg: #f5f7f9;
  --card: #ffffff;
  --accent: #2f6f63;
  --warn: #8a5a14;
  --warn-bg: #fff7e6;
  --bad: #a33a32;
  --bad-bg: #fdf0ee;
  --ok: #276749;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 20px;
  color: var(--ink);
  background: var(--bg);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
}
header, nav, main, footer { max-width: 1320px; margin-left: auto; margin-right: auto; }
header { margin-bottom: 14px; }
h1 { margin: 0 0 4px; font-size: 1.65rem; letter-spacing: -.01em; }
h2 { margin: 0 0 10px; font-size: 1.1rem; }
h3 { margin: 16px 0 6px; font-size: .98rem; }
p { margin: 6px 0; }
a { color: var(--accent); }
.lead { color: var(--muted); }
.banner {
  margin: 10px 0 0;
  padding: 9px 12px;
  border-left: 4px solid var(--warn);
  background: var(--warn-bg);
  color: #4d3a16;
  font-size: .92rem;
}
.tabs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 14px; }
.tab {
  border: 1px solid var(--line);
  background: var(--card);
  color: var(--ink);
  padding: 8px 13px;
  border-radius: 7px;
  font: inherit;
  cursor: pointer;
}
.tab[aria-selected="true"] {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.panel { display: none; }
.panel.active { display: block; }
.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 9px;
  padding: 14px 16px;
  margin-bottom: 14px;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: end;
  gap: 10px;
  margin-bottom: 10px;
}
label { display: grid; gap: 4px; color: var(--muted); font-size: .82rem; }
input, select, button { font: inherit; }
input, select {
  min-width: 130px;
  padding: 6px 8px;
  border: 1px solid #becbd5;
  border-radius: 6px;
  background: #fff;
  color: var(--ink);
}
button {
  padding: 7px 12px;
  border: 1px solid var(--accent);
  border-radius: 6px;
  background: var(--accent);
  color: #fff;
  cursor: pointer;
}
button:disabled { opacity: .55; cursor: default; }
button.secondary { background: #fff; color: var(--accent); }
.notice { margin: 8px 0; padding: 8px 10px; border-radius: 6px; font-size: .9rem; }
.notice.info { background: #eef5f3; color: #245247; }
.notice.warn { background: var(--warn-bg); color: var(--warn); }
.notice.error { background: var(--bad-bg); color: var(--bad); }
.grid {
  display: grid;
  grid-template-columns: minmax(150px, 220px) 1fr;
  gap: 3px 12px;
  margin: 8px 0 0;
  font-size: .9rem;
}
.grid dt { color: var(--muted); }
.grid dd { margin: 0; overflow-wrap: anywhere; }
.table-wrap { overflow: auto; max-height: 560px; border: 1px solid var(--line); border-radius: 7px; }
table { border-collapse: collapse; width: 100%; font-size: .84rem; }
th, td {
  text-align: left;
  vertical-align: top;
  padding: 6px 9px;
  border-bottom: 1px solid #e7edf2;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
th { position: sticky; top: 0; background: #f2f5f7; color: var(--muted); font-weight: 600; }
tbody tr:hover { background: #f8fafb; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre {
  margin: 8px 0;
  padding: 10px;
  border-radius: 6px;
  background: #f3f6f8;
  font-size: .8rem;
  max-height: 420px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--accent); }
.legend { display: flex; flex-wrap: wrap; gap: 10px; margin: 8px 0; font-size: .86rem; }
.legend span.swatch {
  display: inline-block;
  width: 11px;
  height: 11px;
  border-radius: 3px;
  margin-right: 4px;
  vertical-align: -1px;
}
.graph {
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 7px;
  background: #fbfcfd;
  margin: 8px 0;
}
.graph svg { display: block; }
.graph .node { cursor: pointer; }
.warnings ul { margin: 4px 0 0; padding-left: 20px; }
footer { color: var(--muted); font-size: .8rem; padding: 8px 2px 24px; }
.muted { color: var(--muted); }
.bad { color: var(--bad); }
.ok { color: var(--ok); }
@media (max-width: 720px) {
  body { padding: 12px; }
  .grid { grid-template-columns: 1fr; }
  .grid dt { margin-top: 5px; }
}
</style>
</head>
<body>
<header>
  <h1>ozagia console</h1>
  <p class="lead">A local, read-only shell over ozagia's graph, status, worktree, testing, research and action previews.</p>
  <p class="banner"><strong>Read-only:</strong> this console does not edit files, write the graph or state directory, run tests, apply actions, call a model or make network requests. Freshness, provenance, omissions and truncation are shown when the source reports them.</p>
</header>

<nav class="tabs" role="tablist" aria-label="Console sections">
  <button class="tab" id="tab-graph" data-tab="graph" role="tab" aria-selected="true" aria-controls="panel-graph">Graph</button>
  <button class="tab" id="tab-status" data-tab="status" role="tab" aria-selected="false" aria-controls="panel-status">Status</button>
  <button class="tab" id="tab-worktree" data-tab="worktree" role="tab" aria-selected="false" aria-controls="panel-worktree">Worktree</button>
  <button class="tab" id="tab-testing" data-tab="testing" role="tab" aria-selected="false" aria-controls="panel-testing">Testing</button>
  <button class="tab" id="tab-research" data-tab="research" role="tab" aria-selected="false" aria-controls="panel-research">Research</button>
  <button class="tab" id="tab-actions" data-tab="actions" role="tab" aria-selected="false" aria-controls="panel-actions">Actions</button>
</nav>

<main>
  <section class="panel active" id="panel-graph" role="tabpanel" aria-labelledby="tab-graph">
    <div class="card">
      <h2>Graph</h2>
      <p class="muted">A deterministic projection of the recorded graph. It is not a truth claim and does not establish that the index is fresh.</p>
      <div class="toolbar">
        <label>Aperture<select id="graph-aperture"><option value="architecture">architecture</option></select></label>
        <label>Depth (optional)<input id="graph-depth" type="number" min="0" max="32" step="1" placeholder="aperture default"></label>
        <label>Node id (optional)<input id="graph-node" type="text" placeholder="e.g. 12 or claim-0001"></label>
        <button id="graph-load">Load view</button>
      </div>
      <div id="graph-notice"></div>
      <div id="graph-meta"></div>
      <div id="graph-legend" class="legend"></div>
      <div id="graph-svg" class="graph"></div>
      <div id="graph-selected"></div>
      <div id="graph-tables"></div>
    </div>
  </section>

  <section class="panel" id="panel-status" role="tabpanel" aria-labelledby="tab-status">
    <div class="card">
      <h2>Status</h2>
      <p class="muted">Composed read-only snapshot. A configured service is not running; an absent daemon is not an error.</p>
      <div class="toolbar"><button id="status-refresh" class="secondary">Refresh status</button></div>
      <div id="status-notice"></div>
      <div id="status-summary"></div>
      <div id="status-issues"></div>
      <div id="status-sections"></div>
    </div>
    <div class="card">
      <h2>Created-file ledger</h2>
      <div id="files-notice"></div>
      <div id="files-summary"></div>
      <div id="files-tables"></div>
    </div>
  </section>

  <section class="panel" id="panel-worktree" role="tabpanel" aria-labelledby="tab-worktree">
    <div class="card">
      <h2>Worktree</h2>
      <p class="muted">Deterministic git checks only. A wrong branch, detached HEAD or unresolved drift is reported, not worked around.</p>
      <div class="toolbar">
        <label>Base branch<input id="worktree-base" type="text" value="main"></label>
        <button id="worktree-load">Check worktrees</button>
      </div>
      <div id="worktree-notice"></div>
      <div id="worktree-summary"></div>
      <div id="worktree-tables"></div>
    </div>
  </section>

  <section class="panel" id="panel-testing" role="tabpanel" aria-labelledby="tab-testing">
    <div class="card">
      <h2>Testing</h2>
      <p class="muted">A deterministic test-selection plan. <strong>No test is executed</strong> and no truth about coverage is claimed.</p>
      <div class="toolbar">
        <label>Base<input id="testing-base" type="text" value="HEAD"></label>
        <label>Head (optional)<input id="testing-head" type="text" placeholder="working tree"></label>
        <label>Task class (optional)<input id="testing-task" type="text" placeholder="e.g. code_review"></label>
        <button id="testing-load">Build plan</button>
      </div>
      <div id="testing-notice"></div>
      <div id="testing-summary"></div>
      <div id="testing-tables"></div>
    </div>
  </section>

  <section class="panel" id="panel-research" role="tabpanel" aria-labelledby="tab-research">
    <div class="card">
      <h2>Research</h2>
      <p class="muted">Projects an existing investigation. Recorded support is shown as recorded; it is not assessed here.</p>
      <div class="toolbar">
        <label>Investigation id<input id="research-id" type="text" placeholder="research-20261116-..."></label>
        <label>View<select id="research-view">
          <option value="library">library</option>
          <option value="fetch">fetch</option>
          <option value="screening">screening</option>
          <option value="evidence">evidence</option>
          <option value="consensus">consensus</option>
          <option value="citation">citation</option>
        </select></label>
        <button id="research-load">Load view</button>
      </div>
      <div id="research-notice"></div>
      <div id="research-summary"></div>
      <div id="research-tables"></div>
    </div>
  </section>

  <section class="panel" id="panel-actions" role="tabpanel" aria-labelledby="tab-actions">
    <div class="card">
      <h2>Actions</h2>
      <p class="muted">List and preview only. <strong>The console cannot apply an action:</strong> no confirmation token is issued and no apply endpoint exists.</p>
      <div id="actions-notice"></div>
      <div id="actions-list"></div>
    </div>
    <div class="card">
      <h3>Preview an action</h3>
      <div class="toolbar">
        <label>Action<select id="actions-action"></select></label>
        <label>Name<input id="actions-name" type="text" placeholder="service, tunnel or integration"></label>
        <label>Scope<select id="actions-scope"><option value="">default</option><option value="project">project</option><option value="user">user</option></select></label>
        <label>Max bytes<input id="actions-max-bytes" type="number" min="0" max="65536" step="1" placeholder="4096"></label>
        <label>Wait (s)<input id="actions-wait" type="number" min="0" max="600" step="0.1" placeholder="default"></label>
        <label>Idle timeout (s)<input id="actions-idle" type="number" min="0" max="86400" step="1" placeholder="default"></label>
        <label>Live<input id="actions-live" type="checkbox"></label>
        <label>Network<input id="actions-network" type="checkbox"></label>
        <button id="actions-preview">Preview</button>
      </div>
      <div id="actions-preview-result"></div>
    </div>
  </section>
</main>

<footer>
  <p>Endpoints: <code>/api/status</code>, <code>/api/apertures</code>, <code>/api/view</code>, <code>/api/worktree</code>, <code>/api/resources</code>, <code>/api/testing/plan</code>, <code>/api/research</code>, <code>/api/files</code>, <code>/api/actions</code>. Every API request needs the token or cookie. GET and HEAD only.</p>
  <p>No external assets, fonts, scripts or network calls. The token is a bearer credential; keep it local.</p>
</footer>

<script>
"use strict";

const $ = (id) => document.getElementById(id);
const TEXT = (value) => {
  if (value === null || value === undefined) return "\u2014";
  if (typeof value === "object") {
    try { return JSON.stringify(value); } catch (err) { return String(value); }
  }
  return String(value);
};

function clear(node) { if (node) node.replaceChildren(); }
function make(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of children) {
    if (child !== null && child !== undefined) {
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
  }
  return node;
}
function notice(target, message, kind) {
  if (!target) return;
  clear(target);
  if (!message) return;
  target.append(make("div", {class: "notice " + (kind || "info"), text: message}));
}
function factGrid(target, pairs) {
  clear(target);
  const dl = make("dl", {class: "grid"});
  for (const [key, value] of pairs) {
    dl.append(make("dt", {text: key}), make("dd", {text: TEXT(value)}));
  }
  target.append(dl);
}
function table(headers, rows) {
  const wrap = make("div", {class: "table-wrap"});
  const node = make("table");
  const thead = make("thead");
  const headRow = make("tr");
  for (const header of headers) headRow.append(make("th", {text: header}));
  thead.append(headRow);
  const body = make("tbody");
  if (!rows.length) {
    const row = make("tr");
    const cell = make("td", {text: "No rows.", colspan: String(Math.max(1, headers.length))});
    row.append(cell);
    body.append(row);
  }
  for (const values of rows) {
    const row = make("tr");
    for (const value of values) row.append(make("td", {text: TEXT(value)}));
    body.append(row);
  }
  node.append(thead, body);
  wrap.append(node);
  return wrap;
}
function detailsJson(summaryText, value) {
  const details = make("details");
  details.append(make("summary", {text: summaryText}));
  details.append(make("pre", {text: JSON.stringify(value, null, 2)}));
  return details;
}
function compareIds(left, right) {
  const a = String(left), b = String(right);
  return a < b ? -1 : (a > b ? 1 : 0);
}
function setBusy(button, busy, label) {
  if (!button) return;
  button.disabled = busy;
  if (busy) {
    button.dataset.label = button.textContent;
    button.textContent = label || "Loading\u2026";
  } else if (button.dataset.label) {
    button.textContent = button.dataset.label;
  }
}

async function api(path, params) {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }
  const response = await fetch(url, {
    method: "GET",
    credentials: "same-origin",
    cache: "no-store",
    headers: {"Accept": "application/json"}
  });
  const raw = await response.text();
  let data = null;
  if (raw) {
    try { data = JSON.parse(raw); }
    catch (err) { throw new Error("The console returned malformed JSON."); }
  }
  if (!response.ok) {
    const detail = data && data.error && data.error.message
      ? data.error.message : ("HTTP " + response.status);
    const error = new Error(detail);
    error.status = response.status;
    error.code = data && data.error ? data.error.code : null;
    throw error;
  }
  return data;
}

function errorText(error) {
  if (error && error.status === 401) {
    return "Unauthorized. Open the exact URL printed by python -m ozagia.console (it contains the token); the console sets an HttpOnly cookie for this page.";
  }
  return error && error.message ? error.message : String(error);
}

/* ---------------- graph ---------------- */
const graphState = {loadedApertures: false};

async function loadApertures() {
  if (graphState.loadedApertures) return;
  try {
    const data = await api("/api/apertures");
    const select = $("graph-aperture");
    const current = select.value;
    clear(select);
    for (const id of Object.keys(data).sort()) {
      const item = data[id] || {};
      const option = make("option", {value: id, text: (item.title ? item.title + " \u2014 " : "") + id});
      select.append(option);
    }
    if (data[current]) select.value = current;
    else if (data["architecture"]) select.value = "architecture";
    graphState.loadedApertures = true;
  } catch (error) {
    notice($("graph-notice"), "Apertures could not be loaded: " + errorText(error), "error");
  }
}

function renderGraphMeta(view) {
  const snapshot = view.snapshot || {};
  const provenance = view.provenance || {};
  const layout = view.layout || {};
  const omitted = view.omitted || {};
  const pairs = [
    ["aperture", view.aperture],
    ["layout", layout.kind + (layout.seed !== undefined ? " (seed " + layout.seed + ")" : "")],
    ["snapshot root", snapshot.root],
    ["snapshot head", snapshot.head],
    ["freshness", snapshot.freshness === null || snapshot.freshness === undefined ? "not recorded" : snapshot.freshness],
    ["generated at", snapshot.generated_at],
    ["provenance", Array.isArray(provenance.sources) && provenance.sources.length ? provenance.sources.join(", ") : "no source paths recorded"],
    ["graph", provenance.graph ? provenance.graph + " (schema " + TEXT(provenance.graph_schema) + ")" : null],
    ["nodes", (view.nodes || []).length],
    ["edges", (view.edges || []).length],
    ["omitted", (omitted.nodes || 0) + " node(s), " + (omitted.edges || 0) + " edge(s)"],
    ["omission reason", omitted.reason],
  ];
  factGrid($("graph-meta"), pairs);
  if (omitted && ((omitted.nodes || 0) > 0 || (omitted.edges || 0) > 0)) {
    const extra = omitted.by && Object.keys(omitted.by).length
      ? " Breakdown: " + JSON.stringify(omitted.by) + "." : "";
    return "Some graph material was omitted by the aperture or its caps." + extra;
  }
  return "";
}

function renderGraphLegend(view) {
  const target = $("graph-legend");
  clear(target);
  const colors = (view.legend && view.legend.color) || {};
  const groups = view.groups || [];
  for (const group of groups) {
    const span = make("span");
    span.append(make("span", {class: "swatch"}));
    span.lastChild.style.background = colors[group.id] || "#9aa7b4";
    span.append(document.createTextNode(group.label || group.id));
    if (group.count !== undefined) span.append(document.createTextNode(" (" + group.count + (group.collapsed ? ", " + group.collapsed + " collapsed" : "") + ")"));
    target.append(span);
  }
  if (!groups.length) target.append(make("span", {class: "muted", text: "No groups."}));
}

function renderGraphSvg(view) {
  const target = $("graph-svg");
  clear(target);
  const nodes = [...(view.nodes || [])].sort((a, b) => compareIds(a.id, b.id));
  const edges = [...(view.edges || [])].sort((a, b) => compareIds(a.id, b.id));
  if (!nodes.length) {
    target.append(make("p", {class: "muted", text: "No nodes in this view."}));
    return;
  }
  if (nodes.length > 250) {
    target.append(make("p", {class: "notice warn", text: "SVG omitted for " + nodes.length + " nodes; the node and edge tables below contain the complete view."}));
    return;
  }
  const width = Math.max(620, Math.min(2600, 90 + Math.ceil(Math.sqrt(nodes.length)) * 145));
  const cols = Math.max(1, Math.ceil(Math.sqrt(nodes.length)));
  const spacingX = 135;
  const spacingY = 82;
  const margin = 42;
  const rows = Math.ceil(nodes.length / cols);
  const height = margin * 2 + Math.max(0, rows - 1) * spacingY;
  const positions = new Map();
  nodes.forEach((node, index) => {
    positions.set(String(node.id), {
      x: margin + (index % cols) * spacingX,
      y: margin + Math.floor(index / cols) * spacingY,
    });
  });
  // A namespace identifier, not a network fetch. Kept concatenated so the
  // self-contained check does not mistake it for an external asset URL.
  const ns = "http:" + "//www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Graph nodes and edges for aperture " + TEXT(view.aperture));
  const edgeLayer = document.createElementNS(ns, "g");
  edgeLayer.setAttribute("stroke", "#aab6c1");
  edgeLayer.setAttribute("stroke-width", "1");
  edgeLayer.setAttribute("fill", "none");
  let dangling = 0;
  for (const edge of edges) {
    const from = positions.get(String(edge.src));
    const to = positions.get(String(edge.tgt));
    if (!from || !to) { dangling += 1; continue; }
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", String(from.x));
    line.setAttribute("y1", String(from.y));
    line.setAttribute("x2", String(to.x));
    line.setAttribute("y2", String(to.y));
    const title = document.createElementNS(ns, "title");
    title.textContent = TEXT(edge.type) + ": " + TEXT(edge.src) + " \u2192 " + TEXT(edge.tgt);
    line.append(title);
    edgeLayer.append(line);
  }
  if (dangling) {
    target.append(make("p", {class: "notice warn", text: dangling + " edge(s) have a missing endpoint and are not drawn; they remain in the edge table."}));
  }
  svg.append(edgeLayer);
  const colors = (view.legend && view.legend.color) || {};
  const nodeLayer = document.createElementNS(ns, "g");
  nodeLayer.setAttribute("class", "nodes");
  for (const node of nodes) {
    const position = positions.get(String(node.id));
    const group = document.createElementNS(ns, "g");
    group.setAttribute("class", "node");
    group.setAttribute("tabindex", "0");
    const circle = document.createElementNS(ns, "circle");
    circle.setAttribute("cx", String(position.x));
    circle.setAttribute("cy", String(position.y));
    const weight = Number(node.weight);
    const radius = Number.isFinite(weight) ? 7 + Math.min(8, weight * 7) : 9;
    circle.setAttribute("r", String(radius));
    circle.setAttribute("fill", colors[node.group] || "#5b6b73");
    circle.setAttribute("stroke", "#ffffff");
    circle.setAttribute("stroke-width", "1.5");
    const title = document.createElementNS(ns, "title");
    title.textContent = TEXT(node.label || node.id) + " \u2014 " + TEXT(node.type) + " [" + TEXT(node.id) + "]";
    circle.append(title);
    group.append(circle);
    if (nodes.length <= 80) {
      const label = document.createElementNS(ns, "text");
      label.setAttribute("x", String(position.x));
      label.setAttribute("y", String(position.y + radius + 13));
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("font-size", "9.5");
      label.setAttribute("fill", "#33424f");
      const raw = TEXT(node.label || node.id);
      label.textContent = raw.length > 22 ? raw.slice(0, 21) + "\u2026" : raw;
      group.append(label);
    }
    const select = () => {
      const panel = $("graph-selected");
      clear(panel);
      panel.append(make("h3", {text: "Selected node"}));
      panel.append(make("pre", {text: JSON.stringify(node, null, 2)}));
    };
    group.addEventListener("click", select);
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); }
    });
    nodeLayer.append(group);
  }
  svg.append(nodeLayer);
  target.append(svg);
}

function renderGraphTables(view) {
  const target = $("graph-tables");
  clear(target);
  const nodes = [...(view.nodes || [])].sort((a, b) => compareIds(a.id, b.id));
  const edges = [...(view.edges || [])].sort((a, b) => compareIds(a.id, b.id));
  target.append(make("h3", {text: "Nodes"}));
  target.append(table(
    ["id", "type", "raw type", "label", "group", "weight", "key"],
    nodes.map((node) => [node.id, node.type, node.raw_type, node.label, node.group, node.weight, node.key])
  ));
  target.append(make("h3", {text: "Edges"}));
  target.append(table(
    ["id", "src", "tgt", "type", "raw type"],
    edges.map((edge) => [edge.id, edge.src, edge.tgt, edge.type, edge.raw_type])
  ));
  target.append(detailsJson("Raw view JSON", view));
}

async function loadGraphView() {
  const button = $("graph-load");
  setBusy(button, true, "Loading\u2026");
  notice($("graph-notice"), "Loading graph view\u2026", "info");
  try {
    const data = await api("/api/view", {
      aperture: $("graph-aperture").value,
      depth: $("graph-depth").value,
      node: $("graph-node").value,
    });
    const omitted = renderGraphMeta(data);
    renderGraphLegend(data);
    renderGraphSvg(data);
    clear($("graph-selected"));
    renderGraphTables(data);
    if (omitted) {
      notice($("graph-notice"), "View loaded. " + omitted, "warn");
    } else {
      notice($("graph-notice"), "View loaded. No nodes or edges were omitted by the view. Freshness is reported by the graph; this console does not verify it.", "info");
    }
  } catch (error) {
    notice($("graph-notice"), "Graph view failed: " + errorText(error), "error");
    clear($("graph-meta"));
    clear($("graph-legend"));
    clear($("graph-svg"));
    clear($("graph-selected"));
    clear($("graph-tables"));
  } finally {
    setBusy(button, false);
  }
}

/* ---------------- status ---------------- */
function renderStatus(data) {
  const snapshot = data.doctor && data.doctor.report
    ? data.doctor.report.snapshot : null;
  factGrid($("status-summary"), [
    ["checked at", data.checked_at],
    ["root", data.root],
    ["ok", data.ok],
    ["errors", (data.errors || []).length],
    ["degraded", (data.degraded || []).length],
    ["graph snapshot", snapshot && snapshot.state],
    ["daemon", data.daemon && data.daemon.state],
  ]);
  const issues = $("status-issues");
  clear(issues);
  for (const [label, rows] of [["Errors", data.errors || []], ["Degraded", data.degraded || []]]) {
    if (!rows.length) continue;
    issues.append(make("h3", {text: label}));
    issues.append(table(["component", "code", "message"], rows.map((row) => [row.component, row.code, row.message])));
  }
  const sections = ["workspace", "resources", "files", "runtime", "daemon", "doctor", "config"];
  const rows = sections.map((name) => {
    const section = data[name] || {};
    return [name, section.state, section.error, section.report ? "present" : "null"];
  });
  const target = $("status-sections");
  clear(target);
  target.append(make("h3", {text: "Sections"}));
  target.append(table(["section", "state", "error", "report"], rows));
  for (const name of sections) {
    if (data[name] !== undefined) target.append(detailsJson(name + " report", data[name]));
  }
  target.append(detailsJson("Raw status JSON", data));
}

async function loadStatus() {
  const button = $("status-refresh");
  setBusy(button, true, "Loading\u2026");
  notice($("status-notice"), "Reading status\u2026", "info");
  try {
    const data = await api("/api/status");
    renderStatus(data);
    notice($("status-notice"), "Status loaded. A configured service or tunnel is not running and a missing daemon is reported, not guessed.", "info");
    await loadFiles();
  } catch (error) {
    notice($("status-notice"), "Status failed: " + errorText(error), "error");
    clear($("status-summary"));
    clear($("status-issues"));
    clear($("status-sections"));
  } finally {
    setBusy(button, false);
  }
}

async function loadFiles() {
  notice($("files-notice"), "Reading the created-file ledger\u2026", "info");
  try {
    const data = await api("/api/files");
    factGrid($("files-summary"), [
      ["ledger", data.ledger && data.ledger.path],
      ["present", data.ledger && data.ledger.present],
      ["records returned", data.counts && data.counts.records],
      ["orphans", data.counts && data.counts.orphans],
      ["orphans shown", data.counts && data.counts.orphans_shown],
    ]);
    const target = $("files-tables");
    clear(target);
    target.append(make("h3", {text: "Ledger records"}));
    target.append(table(
      ["path", "classification", "status", "created at", "reason", "session", "hash"],
      (data.records || []).map((row) => [row.path, row.classification, row.status, row.created_at, row.reason, row.session, row.hash])
    ));
    target.append(make("h3", {text: "Orphans"}));
    target.append(table(
      ["path", "classification", "status", "created at", "reason"],
      (data.orphans || []).map((row) => [row.path, row.classification, row.status, row.created_at, row.reason])
    ));
    notice($("files-notice"), "The ledger is metadata only. Orphans are listed, never deleted.", "info");
  } catch (error) {
    notice($("files-notice"), "Ledger read failed: " + errorText(error), "error");
    clear($("files-summary"));
    clear($("files-tables"));
  }
}

/* ---------------- worktree ---------------- */
async function loadWorktree() {
  const button = $("worktree-load");
  setBusy(button, true, "Checking\u2026");
  notice($("worktree-notice"), "Running read-only git checks\u2026", "info");
  try {
    const data = await api("/api/worktree", {base: $("worktree-base").value});
    factGrid($("worktree-summary"), [
      ["ok", data.ok],
      ["root", data.root],
      ["base", data.base],
      ["checked at", data.checked_at],
      ["worktrees", (data.worktrees || []).length],
      ["findings", (data.findings || []).length],
    ]);
    const target = $("worktree-tables");
    clear(target);
    target.append(make("h3", {text: "Worktrees"}));
    target.append(table(
      ["path", "branch", "expected", "head", "ahead", "behind", "dirty", "operation",
       "mount", "fstype", "free GB", "tmpfs", "error"],
      (data.worktrees || []).map((row) => {
        const expected = row.expected || {};
        const dirty = row.dirty || {};
        const filesystem = row.filesystem || {};
        return [
          row.path, row.branch, expected.branch, row.head, row.base && row.base.ahead,
          row.base && row.base.behind,
          "staged=" + TEXT(dirty.staged) + ", unstaged=" + TEXT(dirty.unstaged) +
            ", untracked=" + TEXT(dirty.untracked) + ", conflicted=" + TEXT(dirty.conflicted),
          row.operation, filesystem.mount, filesystem.fstype, filesystem.free_gb,
          filesystem.is_tmpfs, row.error
        ];
      })
    ));
    target.append(make("h3", {text: "Findings"}));
    target.append(table(["level", "code", "message", "path"],
      (data.findings || []).map((row) => [row.level, row.code, row.message, row.path || row.worktree])));
    target.append(detailsJson("Raw worktree report", data));
    notice($("worktree-notice"), "Worktree results are observations. This console never commits, merges, pushes, switches or prunes.", "info");
  } catch (error) {
    notice($("worktree-notice"), "Worktree check failed: " + errorText(error), "error");
    clear($("worktree-summary"));
    clear($("worktree-tables"));
  } finally {
    setBusy(button, false);
  }
}

/* ---------------- testing ---------------- */
async function loadTesting() {
  const button = $("testing-load");
  setBusy(button, true, "Planning\u2026");
  notice($("testing-notice"), "Building a test-selection plan\u2026", "info");
  try {
    const data = await api("/api/testing/plan", {
      base: $("testing-base").value,
      head: $("testing-head").value,
      task: $("testing-task").value,
    });
    const counts = data.counts || {};
    factGrid($("testing-summary"), [
      ["mode", data.mode],
      ["requested mode", data.requested_mode],
      ["fallback", data.fallback],
      ["fallback used", data.fallback_used],
      ["refused", data.refused],
      ["executed", data.executed],
      ["truth assessed", data.truth_assessed],
      ["claim", data.claim],
      ["changed files", counts.changed_files],
      ["selected files", counts.selected_files],
      ["selected node ids", counts.selected_node_ids],
      ["omitted files", counts.omitted_files],
      ["max tests", counts.max_tests],
      ["cap exceeded", counts.cap_exceeded],
      ["graph freshness", data.graph && data.graph.freshness],
      ["graph usable", data.graph && data.graph.usable],
      ["graph reason", data.graph && data.graph.reason],
    ]);
    const target = $("testing-tables");
    clear(target);
    target.append(make("h3", {text: "Changed files"}));
    target.append(table(["file"],
      (data.changed_files || []).map((path) => [path])));
    target.append(make("h3", {text: "Selected tests"}));
    target.append(table(["file", "node ids", "sources"],
      (data.selected_tests || []).map((row) => [row.file, (row.node_ids || []).join(", "), (row.sources || []).join(", ")])));
    target.append(make("h3", {text: "Omitted"}));
    target.append(table(["file", "reason"],
      (data.omitted || []).map((row) => [row.file || row.path || row.id, row.reason])));
    target.append(make("h3", {text: "Not assessed"}));
    target.append(table(["item"], (data.not_assessed || []).map((item) => [item])));
    target.append(detailsJson("Raw test plan", data));
    const warning = (data.not_assessed || []).length
      ? "Plan only. No test was executed. " + data.not_assessed[0]
      : "Plan only. No test was executed.";
    notice($("testing-notice"), warning, "warn");
  } catch (error) {
    notice($("testing-notice"), "Test plan failed: " + errorText(error), "error");
    clear($("testing-summary"));
    clear($("testing-tables"));
  } finally {
    setBusy(button, false);
  }
}

/* ---------------- research ---------------- */
async function loadResearch() {
  const button = $("research-load");
  const investigation = $("research-id").value.trim();
  if (!investigation) {
    notice($("research-notice"), "An investigation id is required.", "error");
    return;
  }
  setBusy(button, true, "Loading\u2026");
  notice($("research-notice"), "Loading a recorded research view\u2026", "info");
  try {
    const data = await api("/api/research", {
      investigation: investigation,
      view: $("research-view").value,
    });
    factGrid($("research-summary"), [
      ["view", data.view],
      ["title", data.title],
      ["investigation", data.investigation_id],
      ["schema", data.schema],
      ["truth assessed", data.truth_assessed],
      ["note", data.note],
      ["summary", data.summary],
      ["provenance", data.provenance],
      ["omitted", data.omitted],
      ["not assessed", data.not_assessed],
    ]);
    const target = $("research-tables");
    clear(target);
    const columns = (data.columns || []).map((column) => (
      typeof column === "string" ? {key: column, label: column} : column
    ));
    target.append(make("h3", {text: "Rows"}));
    target.append(table(
      columns.map((column) => column.label || column.key),
      (data.rows || []).map((row) => columns.map((column) => row[column.key]))
    ));
    const extraKeys = Object.keys(data).filter((key) => ![
      "schema", "schema_name", "view", "title", "investigation_id", "columns",
      "nodes", "rows", "summary", "provenance", "omitted", "not_assessed",
      "truth_assessed", "note", "graph"
    ].includes(key));
    for (const key of extraKeys) target.append(detailsJson(key, data[key]));
    target.append(detailsJson("Raw research JSON", data));
    notice($("research-notice"), "This is a projection of recorded data. A support edge is not a truth verdict.", "info");
  } catch (error) {
    notice($("research-notice"), "Research view failed: " + errorText(error), "error");
    clear($("research-summary"));
    clear($("research-tables"));
  } finally {
    setBusy(button, false);
  }
}

/* ---------------- actions ---------------- */
async function loadActions() {
  notice($("actions-notice"), "Listing supported actions\u2026", "info");
  try {
    const data = await api("/api/actions");
    const actions = Array.isArray(data) ? data : (data.actions || []);
    const target = $("actions-list");
    clear(target);
    target.append(table(
      ["action", "description", "mutating", "destructive", "live gated"],
      actions.map((row) => [row.name, row.description, row.mutating, row.destructive, row.live_gated])
    ));
    const select = $("actions-action");
    clear(select);
    for (const row of actions) {
      select.append(make("option", {value: row.name, text: row.name}));
    }
    notice($("actions-notice"), "Preview only. No apply endpoint exists and no confirmation token is issued by this console.", "warn");
  } catch (error) {
    notice($("actions-notice"), "Actions could not be listed: " + errorText(error), "error");
  }
}

async function previewAction() {
  const action = $("actions-action").value;
  const params = {action: action};
  const name = $("actions-name").value.trim();
  const usesName = ["service.status", "service.start", "service.run", "service.stop",
    "service.restart", "service.logs", "tunnel.status", "tunnel.start",
    "integration.status"].includes(action);
  if (usesName && name) params.name = name;
  if (action === "service.logs") {
    if ($("actions-max-bytes").value) params.max_bytes = $("actions-max-bytes").value;
  }
  if (action === "integration.status") {
    if ($("actions-scope").value) params.scope = $("actions-scope").value;
  }
  if (action === "daemon.ensure" || action === "daemon.restart") {
    if ($("actions-wait").value) params.wait = $("actions-wait").value;
    if ($("actions-idle").value) params.idle_timeout = $("actions-idle").value;
  }
  if (action === "tunnel.start") {
    params.live = $("actions-live").checked ? "1" : "0";
    params.network = $("actions-network").checked ? "1" : "0";
  }
  const button = $("actions-preview");
  setBusy(button, true, "Previewing\u2026");
  notice($("actions-notice"), "Rendering a preview; nothing is started, stopped or signalled\u2026", "info");
  try {
    const data = await api("/api/actions", params);
    const target = $("actions-preview-result");
    clear(target);
    const pairs = [
      ["action", data.action],
      ["mode", data.mode],
      ["state", data.state],
      ["operation", data.operation],
      ["manager call", data.manager_call],
      ["argv", (data.argv || []).join(" ")],
      ["expected effect", data.expected_effect],
      ["current state", data.current_state],
      ["applicable", data.applicable],
      ["live", data.live],
      ["gated", data.gated],
      ["confirmation", "not issued (read-only console)"],
      ["apply available", data.apply_available],
      ["note", data.note],
    ];
    factGrid(target, pairs);
    for (const [label, key] of [["Preconditions", "preconditions"], ["Risks", "risks"], ["Notes", "notes"]]) {
      target.append(make("h3", {text: label}));
      target.append(table(["item"], (data[key] || []).map((item) => [item])));
    }
    target.append(detailsJson("Current observation", data.current));
    target.append(detailsJson("Raw preview JSON", data));
    notice($("actions-notice"), "Preview only. Applying an action is intentionally unavailable in this console.", "warn");
  } catch (error) {
    notice($("actions-notice"), "Preview failed: " + errorText(error), "error");
    clear($("actions-preview-result"));
  } finally {
    setBusy(button, false);
  }
}

/* ---------------- tabs ---------------- */
const loaders = {
  graph: async () => { await loadApertures(); await loadGraphView(); },
  status: loadStatus,
  worktree: loadWorktree,
  testing: loadTesting,
  research: loadResearch,
  actions: loadActions,
};
const loaded = {};

function activate(tab) {
  for (const button of document.querySelectorAll(".tab")) {
    const selected = button.dataset.tab === tab;
    button.setAttribute("aria-selected", selected ? "true" : "false");
  }
  for (const panel of document.querySelectorAll(".panel")) {
    panel.classList.toggle("active", panel.id === "panel-" + tab);
  }
  if (!loaded[tab] && loaders[tab]) {
    loaded[tab] = true;
    loaders[tab]();
  }
}

for (const button of document.querySelectorAll(".tab")) {
  button.addEventListener("click", () => activate(button.dataset.tab));
}
$("graph-load").addEventListener("click", loadGraphView);
$("status-refresh").addEventListener("click", loadStatus);
$("worktree-load").addEventListener("click", loadWorktree);
$("testing-load").addEventListener("click", loadTesting);
$("research-load").addEventListener("click", loadResearch);
$("actions-preview").addEventListener("click", previewAction);
$("graph-aperture").addEventListener("change", loadGraphView);
activate("graph");
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.console",
        description="serve a loopback-only, read-only ozagia console")
    parser.add_argument("--root", default=None,
                        help="repository root (default: the current checkout)")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"bind host (default: {DEFAULT_HOST}; loopback only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="bind port (default: 0, an ephemeral port)")
    parser.add_argument("--open", action="store_true", dest="open_browser",
                        help="open the token URL in the default browser")
    parser.add_argument("--token", default=None,
                        help="explicit URL-safe token (default: a random token)")
    parser.add_argument("--allow-remote", action="store_true",
                        help="allow a non-loopback bind; requires --token "
                             "(not recommended)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser() if args.root else repo_root()
    try:
        if not root.is_dir():
            print(f"error: repository root does not exist: {root}", file=sys.stderr)
            return 2
        print("ozagia console: read-only views. No writes, no network, no "
              "model calls.", flush=True)
        serve(root, host=args.host, port=args.port, token=args.token,
              open_browser=args.open_browser, allow_remote=args.allow_remote)
        return 0
    except ConsoleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nozagia console stopped.")
        return 0


__all__ = [
    "COOKIE_NAME", "ConsoleError", "ConsoleServer", "ConsoleState",
    "build_parser", "create_server", "main", "serve",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
