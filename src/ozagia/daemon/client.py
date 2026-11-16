"""A client for the graph daemon (see `ozagia.daemon.protocol`).

    with DaemonClient(socket_path(root)) as c:
        c.ping()
        c.tool("read", {"path": "src/x.py"}, session="ses_1")

One connection is reused for every request and reopened when the daemon
closed it before a request was sent. A request that was sent is never
resent: a lost reply raises `DaemonError`, a missing or refusing socket
`DaemonUnavailable`, a reply slower than the timeout `DaemonTimeout` (and the
connection is dropped, since a late reply would be out of order).
"""
from __future__ import annotations

import itertools
import json
import socket
import threading
from pathlib import Path
from typing import Any

from ozagia.daemon.protocol import MAX_MESSAGE_BYTES

DEFAULT_TIMEOUT_S = 30.0


class DaemonError(Exception):
    """The daemon could not answer a request."""


class DaemonUnavailable(DaemonError):
    """No daemon listens on the socket."""


class DaemonTimeout(DaemonError):
    """The daemon did not answer in time."""


class DaemonClient:
    def __init__(self, sock: str | Path, *,
                 timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self.sock = str(sock)
        self.timeout = timeout
        self._conn: socket.socket | None = None
        self._rfile: Any = None
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    # -- connection ---------------------------------------------------------- #
    def _connect(self) -> None:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(self.timeout)
        try:
            conn.connect(self.sock)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            conn.close()
            raise DaemonUnavailable(
                f"no daemon on {self.sock}: {e.strerror or e}") from None
        except socket.timeout:
            conn.close()
            raise DaemonTimeout(f"connect to {self.sock} timed out") from None
        except OSError as e:
            conn.close()
            raise DaemonUnavailable(f"cannot connect to {self.sock}: {e}") from None
        self._conn = conn
        self._rfile = conn.makefile("rb")

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._rfile is not None:
            try:
                self._rfile.close()
            except OSError:
                pass
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
        self._conn = self._rfile = None

    def __enter__(self) -> "DaemonClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- requests ------------------------------------------------------------ #
    def request(self, op: str, *, timeout: float | None = None,
                **fields: Any) -> dict[str, Any]:
        """Send one request and return the daemon's reply object."""
        rid = next(self._ids)
        data = json.dumps({"id": rid, "op": op, **fields},
                          ensure_ascii=False).encode("utf-8") + b"\n"
        if len(data) > MAX_MESSAGE_BYTES:
            raise DaemonError(f"request exceeds {MAX_MESSAGE_BYTES} bytes")
        with self._lock:
            for attempt in (0, 1):
                reused = self._conn is not None
                if self._conn is None:
                    self._connect()
                assert self._conn is not None
                try:
                    self._conn.sendall(data)
                    break
                except (BrokenPipeError, ConnectionResetError) as e:
                    self._drop()
                    if attempt or not reused:
                        raise DaemonError(f"send failed: {e}") from None
                except socket.timeout:
                    self._drop()
                    raise DaemonTimeout("send timed out") from None
            self._conn.settimeout(self.timeout if timeout is None else timeout)
            try:
                line = self._rfile.readline(MAX_MESSAGE_BYTES + 1)
            except socket.timeout:
                self._drop()
                raise DaemonTimeout(
                    f"no reply to {op} within "
                    f"{self.timeout if timeout is None else timeout} s") from None
            except OSError as e:
                self._drop()
                raise DaemonError(f"read failed: {e}") from None
            finally:
                if self._conn is not None:
                    self._conn.settimeout(self.timeout)
            if not line:
                self._drop()
                raise DaemonError(f"the daemon closed the connection ({op})")
            try:
                reply = json.loads(line)
            except ValueError as e:
                self._drop()
                raise DaemonError(f"bad reply: {e}") from None
        if not isinstance(reply, dict):
            raise DaemonError("bad reply: not an object")
        if reply.get("id") not in (rid, None):
            raise DaemonError(f"reply id {reply.get('id')} for request {rid}")
        return reply

    def ping(self, *, timeout: float | None = None) -> dict[str, Any]:
        return self.request("ping", timeout=timeout)

    def hello(self, *, harness: str = "opencode", plugin: str = "ozagia",
              version: str = "", session: str | None = None) -> dict[str, Any]:
        return self.request("hello", harness=harness, plugin=plugin,
                            version=version, session=session)

    def tool(self, tool: str, input: dict[str, Any], *,
             session: str | None = None, harness: str = "opencode",
             timeout: float | None = None) -> dict[str, Any]:
        return self.request("tool", timeout=timeout, tool=tool, input=input,
                            session=session, harness=harness)

    def after(self, tool: str, input: dict[str, Any], output: str, *,
              session: str | None = None, harness: str = "opencode",
              timeout: float | None = None) -> dict[str, Any]:
        return self.request("after", timeout=timeout, tool=tool, input=input,
                            output=output, session=session, harness=harness)

    def read_check(self, tool: str, input: dict[str, Any], *,
                   session: str | None = None, harness: str = "opencode",
                   sub: str | None = None,
                   timeout: float | None = None) -> dict[str, Any]:
        return self.request("read_check", timeout=timeout, tool=tool,
                            input=input, session=session, harness=harness,
                            sub=sub)

    def session_event(self, event: str, *, session: str,
                      harness: str = "opencode",
                      timeout: float | None = None) -> dict[str, Any]:
        return self.request("session_event", timeout=timeout, event=event,
                            session=session, harness=harness)

    def stats(self) -> dict[str, Any]:
        return self.request("stats")

    def shutdown(self) -> dict[str, Any]:
        return self.request("shutdown")


__all__ = ["DaemonClient", "DaemonError", "DaemonTimeout", "DaemonUnavailable"]
