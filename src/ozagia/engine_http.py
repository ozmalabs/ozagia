"""A bounded, read-only local HTTP/JSON transport over one graph engine.

The transport exposes the existing stateful
:class:`ozagia.core.graph_engine.GraphEngine` and the existing
:func:`ozagia.integrations.ozma.context.task_context` provider.  It adds no
graph logic: every request is handed to those objects, whose freshness,
budget, session and read-only guarantees still apply.

Endpoints:

``POST /v1/graph``
    The request body is an engine request object; the response is the engine
    JSON (including ``hello`` and ``bye``).
``POST /v1/ozma/context``
    ``{task, budget_tokens, max_items, allow_stale}`` mapped to
    ``task_context``.  The response is the provider's versioned result.
``GET /healthz``
    Liveness only; it carries no graph content and needs no token.

The server binds ``127.0.0.1`` by default.  A non-loopback bind needs an
explicit ``allow_remote=True`` and prints a warning; HTTP is unencrypted and a
TLS/auth perimeter remains the caller's responsibility.  A bearer token is
required for every ``/v1`` request.  The token comes from
``OZAGIA_ENGINE_TOKEN`` or ``<state>/engine/token``; there is no implicit token
and no serving without one.  The token is never logged or echoed.

There is no CORS, no directory or static-file route, no request-body echo and
no target import, execution, network or model call.  Error responses are
stable JSON with a code and a short message, never rows, secrets or the
offending body.  See ``docs/graph_engine.md`` and ``docs/ozma_integration.md``.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import signal
import socket
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

from ozagia.config import repo_root, state_path
from ozagia.core.graph_engine import GraphEngine
from ozagia.core.memory import DevMemory
from ozagia.integrations.ozma.context import task_context

LOGGER = logging.getLogger("ozagia.engine_http")

PROTOCOL = "ozagia.graph_engine.http/1"
GRAPH_PROTOCOL = "ozagia.graph_engine/2"
TOKEN_ENV = "OZAGIA_ENGINE_TOKEN"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_BODY_BYTES = 64 * 1024
MAX_CONCURRENT_REQUESTS = 4
REQUEST_TIMEOUT_S = 15.0
MAX_TOKEN_BYTES = 4_096
GRAPH_PATH = "/v1/graph"
CONTEXT_PATH = "/v1/ozma/context"
HEALTH_PATH = "/healthz"
_ENDPOINTS = {GRAPH_PATH: "POST", CONTEXT_PATH: "POST", HEALTH_PATH: "GET"}
_CONTEXT_FIELDS = frozenset({"task", "budget_tokens", "max_items", "allow_stale"})
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.", "127.0.0.1", "::1"})


class EngineHTTPError(ValueError):
    """The transport configuration is invalid."""


class _RequestError(Exception):
    """An HTTP-facing error with a stable JSON code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TokenFile:
    """Where a generated token lives and its non-secret fingerprint."""

    path: Path
    fingerprint: str
    created: bool


# --------------------------------------------------------------------------- #
# token handling
# --------------------------------------------------------------------------- #
def token_path(root: str | Path) -> Path:
    """The default token file for a checkout."""
    return state_path(root, "engine", "token")


def token_fingerprint(token: str) -> str:
    """A short, non-secret identifier for a token (never the token itself)."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _valid_token(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_TOKEN_BYTES
            and not any(ord(char) < 0x21 or ord(char) == 0x7F for char in value))


def _token_from_file(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        raise EngineHTTPError(f"cannot read engine token file {path}: {exc}") from None
    if path.is_symlink() or not path.is_file():
        raise EngineHTTPError(f"engine token path is not a regular file: {path}")
    if info.st_mode & 0o077:
        raise EngineHTTPError(
            f"engine token file {path} is accessible to group/other; "
            "run `chmod 600` on it or regenerate with `ozagia engine token`")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_TOKEN_BYTES + 1)
    except OSError as exc:
        raise EngineHTTPError(f"cannot read engine token file {path}: {exc}") from None
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise EngineHTTPError(f"engine token file {path} is not UTF-8") from None
    if not _valid_token(value):
        raise EngineHTTPError(f"engine token file {path} is empty or malformed")
    return value


def resolve_token(root: str | Path) -> str | None:
    """Return the configured token, or ``None`` when none is configured.

    ``OZAGIA_ENGINE_TOKEN`` wins over the token file.  A malformed file or one
    readable by group/other is refused rather than silently trusted.
    """
    env = os.environ.get(TOKEN_ENV)
    if env is not None:
        if not _valid_token(env):
            raise EngineHTTPError(
                f"{TOKEN_ENV} is empty or contains whitespace/control characters")
        return env
    path = token_path(root)
    if not path.exists():
        return None
    return _token_from_file(path)


def ensure_token_file(root: str | Path) -> TokenFile:
    """Create or repair ``<state>/engine/token`` with mode ``0600``.

    The token value is never returned or printed; callers get the path and a
    fingerprint.  An existing file is reused, its permissions are tightened and
    its contents are never overwritten.
    """
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise EngineHTTPError(f"repository root does not exist: {root_path}")
    path = token_path(root_path)
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise EngineHTTPError(f"cannot create {directory}: {exc}") from None
    created = False
    if path.exists():
        token = _token_from_file(path)
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise EngineHTTPError(f"cannot tighten {path}: {exc}") from None
    else:
        token = secrets.token_urlsafe(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            token = _token_from_file(path)
        except OSError as exc:
            raise EngineHTTPError(f"cannot create {path}: {exc}") from None
        else:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(token + "\n")
                os.chmod(path, 0o600)
                created = True
            except OSError as exc:
                raise EngineHTTPError(f"cannot write {path}: {exc}") from None
    return TokenFile(path=path, fingerprint=token_fingerprint(token), created=created)


# --------------------------------------------------------------------------- #
# binding and engine loading
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


def load_engine(root: str | Path, *,
                auto_known: bool = True) -> GraphEngine:
    """Load the existing snapshot for ``root`` without building or refreshing it.

    The HTTP surface is a model-facing serving surface, so auto-known defaults
    on: an omitted ``known`` on a repeated find/outline/context is answered
    from the session's remembered hashes.  A caller that sends an explicit
    ``known`` (including ``[]``) keeps full control, and ``auto_known=False``
    restores the core default.
    """
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise EngineHTTPError(f"repository root does not exist: {root_path}")
    dump = state_path(root_path, "graph.json")
    journal = state_path(root_path, "journal.jsonl")
    if not dump.is_file():
        raise EngineHTTPError(
            f"no graph snapshot at {dump}; run `ozagia index --root {root_path}` first")
    try:
        memory = DevMemory(dump, journal, root=root_path)
    except Exception as exc:  # noqa: BLE001 - report a stable configuration error
        raise EngineHTTPError(
            f"cannot load the graph snapshot at {dump}: {type(exc).__name__}") from None
    return GraphEngine(memory, workspace=root_path, auto_known=auto_known)


# --------------------------------------------------------------------------- #
# server
# --------------------------------------------------------------------------- #
class EngineHTTPServer(ThreadingHTTPServer):
    """A threaded HTTP server carrying one engine and one token."""

    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False
    request_queue_size = 16

    def __init__(self, server_address: tuple[str, int],
                 handler_class: type[BaseHTTPRequestHandler], *,
                 engine: Any, token: str, root: Path, display_host: str,
                 remote: bool, owns_engine: bool,
                 max_concurrent: int = MAX_CONCURRENT_REQUESTS) -> None:
        self.engine = engine
        self.token = token
        self.root = root
        self.display_host = display_host
        self.remote = remote
        self.owns_engine = owns_engine
        self._slots = threading.BoundedSemaphore(max(1, int(max_concurrent)))
        super().__init__(server_address, handler_class)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def base_url(self) -> str:
        host = self.display_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def try_acquire_slot(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release_slot(self) -> None:
        try:
            self._slots.release()
        except ValueError:  # pragma: no cover - defensive double release
            pass

    def close_engine(self) -> None:
        if not self.owns_engine:
            return
        self.owns_engine = False
        try:
            self.engine.close()
        except Exception:  # noqa: BLE001 - best effort during shutdown
            LOGGER.warning("engine close failed", exc_info=True)


def create_server(root: str | Path, *, host: str = DEFAULT_HOST,
                  port: int = DEFAULT_PORT, token: str | None = None,
                  engine: Any | None = None, allow_remote: bool = False,
                  auto_known: bool = True,
                  max_concurrent: int = MAX_CONCURRENT_REQUESTS) -> EngineHTTPServer:
    """Create (but do not serve) a bound engine HTTP server.

    ``token`` is an explicit injection point for tests and embedding.  When it
    is ``None`` the token is read from ``OZAGIA_ENGINE_TOKEN`` or the token file;
    with neither, creation fails.  ``engine`` may be injected too; otherwise the
    existing snapshot is loaded read-only.
    """
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise EngineHTTPError(f"repository root does not exist: {root_path}")
    if not isinstance(host, str) or not host.strip():
        raise EngineHTTPError("host must be a non-empty string")
    remote = not _loopback_host(host)
    if remote and not allow_remote:
        raise EngineHTTPError(
            f"refusing non-loopback host {host!r}; the engine transport is "
            "loopback-only (pass --allow-remote to override; HTTP has no TLS)")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise EngineHTTPError("port must be an integer between 0 and 65535")
    token_value = token if token is not None else resolve_token(root_path)
    if token_value is None:
        raise EngineHTTPError(
            "no engine token configured; run `ozagia engine token --root "
            f"{root_path}` or set {TOKEN_ENV}")
    if not _valid_token(token_value):
        raise EngineHTTPError("engine token is empty or contains whitespace/control characters")

    owns_engine = engine is None
    if engine is None:
        engine = load_engine(root_path, auto_known=auto_known)
    bind_host, family = _bind_host(host)

    class _Server(EngineHTTPServer):
        address_family = family

    try:
        return _Server(
            (bind_host, port), _EngineHandler, engine=engine,
            token=token_value, root=root_path, display_host=_display_host(host),
            remote=remote, owns_engine=owns_engine,
            max_concurrent=max_concurrent)
    except OSError as exc:
        if owns_engine:
            try:
                engine.close()
            except Exception:  # noqa: BLE001 - best effort while reporting bind
                pass
        raise EngineHTTPError(f"cannot bind {bind_host}:{port}: {exc}") from None


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "protocol": PROTOCOL,
            "error": {"code": code, "message": message}}


def _constant_time_token(supplied: str, expected: str) -> bool:
    try:
        return hmac.compare_digest(supplied, expected)
    except TypeError:
        return False


class _EngineHandler(BaseHTTPRequestHandler):
    server: EngineHTTPServer
    server_version = "ozagia-engine-http"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_S
    max_headers = 64

    # -- routing -------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_HEAD(self) -> None:  # noqa: N802
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
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed",
                         "method is not allowed on this endpoint",
                         headers={"Allow": "GET, POST"})

    def _handle(self, method: str) -> None:
        self.close_connection = True
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == HEALTH_PATH:
                if method != "GET":
                    self._send_error(HTTPStatus.METHOD_NOT_ALLOWED,
                                     "method_not_allowed",
                                     "healthz accepts GET only",
                                     headers={"Allow": "GET"})
                    return
                self._send_json(HTTPStatus.OK, {
                    "ok": True, "service": "ozagia.engine.http",
                    "version": PROTOCOL, "graph": GRAPH_PROTOCOL,
                    "read_only": True,
                })
                return
            expected = _ENDPOINTS.get(path)
            if expected is None:
                self._send_error(HTTPStatus.NOT_FOUND, "not_found",
                                 "unknown endpoint")
                return
            if method != expected:
                self._send_error(HTTPStatus.METHOD_NOT_ALLOWED,
                                 "method_not_allowed",
                                 f"{path} accepts {expected} only",
                                 headers={"Allow": expected})
                return
            if not self._authorized():
                self._send_error(HTTPStatus.UNAUTHORIZED, "unauthorized",
                                 "missing or invalid bearer token",
                                 headers={"WWW-Authenticate": "Bearer"})
                return
            body = self._read_json_body()
            if path == CONTEXT_PATH:
                self._context(body)
            else:
                self._graph(body)
        except _RequestError as exc:
            self.close_connection = True
            self._send_error(exc.status, exc.code, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001 - contained boundary, no detail leaked
            LOGGER.error("engine http request failed", exc_info=True)
            self.close_connection = True
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error",
                             "the request could not be completed")

    # -- request helpers ------------------------------------------------- #
    def _authorized(self) -> bool:
        header = self.headers.get("Authorization")
        if not header or not header.startswith("Bearer "):
            return False
        supplied = header[len("Bearer "):].strip()
        if not supplied:
            return False
        return _constant_time_token(supplied, self.server.token)

    def _read_json_body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "body_not_supported",
                                "chunked request bodies are not supported")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "length_required",
                                "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid_content_length",
                                "invalid Content-Length") from None
        if length < 0:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid_content_length",
                                "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "body_too_large",
                                f"request body exceeds {MAX_BODY_BYTES} bytes")
        if length == 0:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "empty_body",
                                "a JSON object is required")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "incomplete_body",
                                "request body ended early")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "malformed_json",
                                "request body is not valid JSON") from None
        if not isinstance(body, dict):
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid_request",
                                "request body must be a JSON object")
        return body

    def _graph(self, body: dict[str, Any]) -> None:
        if not self.server.try_acquire_slot():
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "saturated",
                             "too many concurrent requests; retry shortly",
                             headers={"Retry-After": "1"})
            return
        try:
            result = self.server.engine.handle(body)
        finally:
            self.server.release_slot()
        self._send_json(HTTPStatus.OK, result)

    def _context(self, body: dict[str, Any]) -> None:
        unknown = sorted(set(body) - _CONTEXT_FIELDS)
        if unknown:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid_context_request",
                                "the context request has unsupported fields")
        memory = getattr(self.server.engine, "mem", None)
        if memory is None:
            raise _RequestError(HTTPStatus.INTERNAL_SERVER_ERROR,
                                "context_unavailable",
                                "the context provider is not available")
        if not self.server.try_acquire_slot():
            self._send_error(HTTPStatus.SERVICE_UNAVAILABLE, "saturated",
                             "too many concurrent requests; retry shortly",
                             headers={"Retry-After": "1"})
            return
        try:
            result = task_context(
                memory, body.get("task"),
                budget_tokens=body.get("budget_tokens", 1500),
                max_items=body.get("max_items", 8),
                allow_stale=body.get("allow_stale", False))
        except ValueError as exc:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid_context_request",
                                str(exc)) from None
        finally:
            self.server.release_slot()
        self._send_json(HTTPStatus.OK, result)

    # -- responses ------------------------------------------------------- #
    def _send_json(self, status: int, payload: Any, *,
                   headers: Mapping[str, str] | None = None) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            self.close_connection = True
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            body = json.dumps(_error_payload(
                "serialization_error", "the response could not be serialised"),
                separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Ozagia-Read-Only", "true")
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_error(self, status: int, code: str, message: str, *,
                    headers: Mapping[str, str] | None = None) -> None:
        self._send_json(status, _error_payload(code, message), headers=headers)

    # -- logging --------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:
        path = self.path.split("?", 1)[0]
        LOGGER.info("%s %s %s", self.address_string(), self.command, path)


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #
def _install_signal_handlers(server: EngineHTTPServer) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def stop(_signum: int, _frame: Any) -> None:
        # ``shutdown`` waits for ``serve_forever`` to return, so it cannot run
        # on the serving thread itself.
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, stop)
        except (OSError, ValueError):
            continue
    return previous


def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            continue


def serve(root: str | Path, *, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT, token: str | None = None,
          allow_remote: bool = False,
          auto_known: bool = True,
          max_concurrent: int = MAX_CONCURRENT_REQUESTS) -> None:
    """Serve until SIGINT/SIGTERM or ``shutdown``; close server and engine."""
    server = create_server(root, host=host, port=port, token=token,
                           allow_remote=allow_remote,
                           auto_known=auto_known,
                           max_concurrent=max_concurrent)
    previous = _install_signal_handlers(server)
    try:
        print(f"ozagia engine http (read-only): {server.base_url}{GRAPH_PATH}",
              flush=True)
        print(f"Listening on {server.base_url} (port {server.port}).", flush=True)
        print("Bearer token required. The token is read from "
              f"{TOKEN_ENV} or {token_path(server.root)}; its value is never "
              "printed.", flush=True)
        print(f"Limits: {MAX_BODY_BYTES} byte body, {max_concurrent} concurrent "
              "requests.", flush=True)
        if server.remote:
            print("WARNING: a non-loopback host is bound. HTTP is unencrypted; "
                  "TLS and the network/auth perimeter are the caller's "
                  "responsibility.", file=sys.stderr, flush=True)
        else:
            print("Binding: loopback only.", flush=True)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.close_engine()
        _restore_signal_handlers(previous)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def add_parser(sub: Any) -> None:
    """Register ``ozagia engine`` (serve/token) on the main CLI."""
    parser = sub.add_parser(
        "engine", help="local read-only graph HTTP transport (serve/token)")
    children = parser.add_subparsers(dest="action", required=True)

    server = children.add_parser(
        "serve", help="serve the engine over loopback HTTP/JSON")
    server.add_argument("--root", default=None,
                        help="repository root (default: the current checkout)")
    server.add_argument("--host", default=DEFAULT_HOST,
                        help=f"bind host (default: {DEFAULT_HOST}; loopback only)")
    server.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"bind port (default: {DEFAULT_PORT}; 0 chooses one)")
    server.add_argument("--allow-remote", action="store_true",
                        help="allow a non-loopback bind; HTTP has no TLS and the "
                             "caller owns the network/auth perimeter")
    server.add_argument("--no-auto-known", action="store_true",
                        help="return full find/outline/context rows when a "
                             "request omits known (default: remember hashes in "
                             "the session and return u handles)")

    token = children.add_parser(
        "token", help="create or repair <state>/engine/token (mode 0600)")
    token.add_argument("--root", default=None,
                       help="repository root (default: the current checkout)")


def run(args: argparse.Namespace) -> int:
    """Run one parsed ``ozagia engine`` command."""
    root = Path(args.root).expanduser() if args.root else repo_root()
    try:
        if args.action == "token":
            info = ensure_token_file(root)
            print(f"token file: {info.path}")
            print(f"fingerprint: {info.fingerprint}")
            print("mode: 0600")
            if info.created:
                print("created: yes")
            else:
                print("created: no (existing token reused)")
            return 0
        if args.action == "serve":
            if not root.is_dir():
                print(f"error: repository root does not exist: {root}",
                      file=sys.stderr)
                return 2
            serve(root, host=args.host, port=args.port,
                  allow_remote=args.allow_remote,
                  auto_known=not getattr(args, "no_auto_known", False))
            return 0
    except EngineHTTPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    print("error: unknown engine action", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.engine_http",
        description="serve a loopback, read-only graph engine over HTTP/JSON")
    children = parser.add_subparsers(dest="action", required=True)
    server = children.add_parser("serve", help="serve the engine over loopback HTTP/JSON")
    server.add_argument("--root", default=None)
    server.add_argument("--host", default=DEFAULT_HOST)
    server.add_argument("--port", type=int, default=DEFAULT_PORT)
    server.add_argument("--allow-remote", action="store_true")
    server.add_argument("--no-auto-known", action="store_true")
    token = children.add_parser("token", help="create or repair <state>/engine/token")
    token.add_argument("--root", default=None)
    return run(parser.parse_args(argv))


__all__ = [
    "CONTEXT_PATH", "DEFAULT_HOST", "DEFAULT_PORT", "EngineHTTPError",
    "EngineHTTPServer", "GRAPH_PATH", "HEALTH_PATH", "MAX_BODY_BYTES",
    "MAX_CONCURRENT_REQUESTS", "PROTOCOL", "TOKEN_ENV", "TokenFile",
    "add_parser", "create_server", "ensure_token_file", "load_engine", "main",
    "resolve_token", "run", "serve", "token_fingerprint", "token_path",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
