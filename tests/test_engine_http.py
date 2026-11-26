"""The bounded loopback HTTP/JSON transport over the graph engine.

Every test uses a real ``ThreadingHTTPServer`` on an ephemeral port.  The
fixture is a tiny git checkout and a loaded ``DevMemory``; no target code is
run and no model or external network is involved.
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import select
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ozagia import cli, engine_http
from ozagia.config import state_path
from ozagia.core.graph_engine import GraphEngine
from ozagia.core.memory import DevMemory, build_index

TOKEN = "test-token-0123456789abcdef"
CORE = '''\
"""Core helpers."""


def add(a, b):
    """Add two numbers."""
    return a + b


def double(x):
    """Double x."""
    return add(x, x)
'''
USER = '''\
"""Uses core."""
from pkg.core import double


def quadruple(x):
    """Four times x."""
    return double(double(x))
'''
TEST_CORE = '''\
from pkg.core import add


def test_add():
    assert add(1, 2) == 3
'''
README = "# Fixture\n\nA small engine HTTP fixture.\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t",
                    "-c", "user.name=t", *args], check=True,
                   capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/core.py", CORE)
    _write(root, "pkg/user.py", USER)
    _write(root, "tests/test_core.py", TEST_CORE)
    _write(root, "README.md", README)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _build(repo: Path, out: Path | None = None) -> DevMemory:
    dump = out or (repo.parent / "state" / "graph.json")
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, dump.parent / "journal.jsonl", root=repo)


@pytest.fixture()
def memory(repo: Path) -> DevMemory:
    return _build(repo)


@pytest.fixture()
def server(repo: Path, memory: DevMemory):
    engine = GraphEngine(memory, spec_enabled=False)
    instance = engine_http.create_server(repo, port=0, token=TOKEN,
                                         engine=engine)
    thread = threading.Thread(
        target=instance.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield instance, engine
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=3)
        engine.close()


def _request(server: engine_http.EngineHTTPServer, method: str, path: str, *,
             body: Any = None, token: str | None = TOKEN,
             raw: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    host = server.server_address[0]
    connection = http.client.HTTPConnection(host, server.port, timeout=10)
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if raw is not None:
        headers.setdefault("Content-Type", "application/json")
    try:
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
    finally:
        connection.close()
    return result


def _json(payload: bytes) -> dict[str, Any]:
    value = json.loads(payload.decode("utf-8"))
    assert isinstance(value, dict)
    return value


def _graph(server: engine_http.EngineHTTPServer, body: dict[str, Any], *,
           token: str | None = TOKEN) -> tuple[int, dict[str, Any]]:
    status, _headers, payload = _request(server, "POST", engine_http.GRAPH_PATH,
                                         body=body, token=token)
    return status, _json(payload)


def _context(server: engine_http.EngineHTTPServer,
             body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    status, _headers, payload = _request(server, "POST",
                                         engine_http.CONTEXT_PATH, body=body)
    return status, _json(payload)


# --------------------------------------------------------------------------- #
# token helper and configuration
# --------------------------------------------------------------------------- #
def test_token_helper_is_0600_and_prints_only_path_and_fingerprint(
        repo: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv(engine_http.TOKEN_ENV, raising=False)
    args = argparse.Namespace(action="token", root=str(repo))
    assert engine_http.run(args) == 0
    output = capsys.readouterr()
    path = engine_http.token_path(repo)
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    token = path.read_text(encoding="utf-8").strip()
    assert token
    assert token not in output.out and token not in output.err
    assert engine_http.token_fingerprint(token) in output.out
    assert "created: yes" in output.out

    assert engine_http.run(args) == 0
    second = capsys.readouterr()
    assert token not in second.out and token not in second.err
    assert "created: no" in second.out
    assert engine_http.resolve_token(repo) == token

    path.chmod(0o644)
    with pytest.raises(engine_http.EngineHTTPError, match="accessible"):
        engine_http.resolve_token(repo)


def test_server_refuses_without_a_configured_token(repo: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(engine_http.TOKEN_ENV, raising=False)
    with pytest.raises(engine_http.EngineHTTPError, match="no engine token"):
        engine_http.create_server(repo, port=0)
    with pytest.raises(engine_http.EngineHTTPError, match="non-loopback"):
        engine_http.create_server(repo, host="0.0.0.0", port=0, token=TOKEN)


def test_environment_token_wins_and_is_used(repo: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(engine_http.TOKEN_ENV, TOKEN)
    assert engine_http.resolve_token(repo) == TOKEN
    monkeypatch.setenv(engine_http.TOKEN_ENV, "bad token with spaces")
    with pytest.raises(engine_http.EngineHTTPError, match="whitespace"):
        engine_http.resolve_token(repo)


# --------------------------------------------------------------------------- #
# routing, auth, limits and stable errors
# --------------------------------------------------------------------------- #
def test_healthz_is_liveness_only_and_needs_no_token(server) -> None:
    instance, _engine = server
    status, headers, payload = _request(instance, "GET", engine_http.HEALTH_PATH,
                                        token=None)
    assert status == 200
    body = _json(payload)
    assert body["ok"] is True and body["read_only"] is True
    assert body["service"] == "ozagia.engine.http"
    assert headers["Content-Type"] == "application/json"
    assert "Access-Control-Allow-Origin" not in headers
    for forbidden in ("nodes", "edges", "rows", "sid", "snapshot", "token"):
        assert forbidden not in body


def test_auth_missing_wrong_and_correct(server) -> None:
    instance, _engine = server
    for token in (None, "wrong-token"):
        status, headers, payload = _request(
            instance, "POST", engine_http.GRAPH_PATH,
            body={"op": "hello"}, token=token)
        assert status == 401
        assert headers["WWW-Authenticate"] == "Bearer"
        body = _json(payload)
        assert body["error"]["code"] == "unauthorized"
        assert "sid" not in body and "hello" not in payload.decode()
    status, body = _graph(instance, {"op": "hello"})
    assert status == 200 and body["p"] == engine_http.GRAPH_PROTOCOL
    assert body["f"] == "fresh" and body["sid"]


def test_malformed_oversized_and_unknown_requests(server) -> None:
    instance, _engine = server
    status, _headers, payload = _request(
        instance, "POST", engine_http.GRAPH_PATH, raw=b"{")
    assert status == 400 and _json(payload)["error"]["code"] == "malformed_json"
    status, _headers, payload = _request(
        instance, "POST", engine_http.GRAPH_PATH, raw=b"[]")
    assert status == 400 and _json(payload)["error"]["code"] == "invalid_request"
    status, _headers, payload = _request(
        instance, "POST", engine_http.GRAPH_PATH,
        raw=b"x" * (engine_http.MAX_BODY_BYTES + 1))
    assert status == 400 and _json(payload)["error"]["code"] == "body_too_large"
    status, _headers, payload = _request(
        instance, "POST", "/not-a-route", body={"op": "hello"})
    assert status == 404 and _json(payload)["error"]["code"] == "not_found"
    status, headers, payload = _request(
        instance, "GET", engine_http.GRAPH_PATH, token=TOKEN)
    assert status == 405 and headers["Allow"] == "POST"
    assert _json(payload)["error"]["code"] == "method_not_allowed"
    status, _headers, payload = _request(
        instance, "POST", engine_http.HEALTH_PATH, body={}, token=None)
    assert status == 405
    status, headers, payload = _request(
        instance, "OPTIONS", engine_http.GRAPH_PATH, token=TOKEN)
    assert status == 405
    assert "Access-Control-Allow-Origin" not in headers


# --------------------------------------------------------------------------- #
# engine protocol over HTTP
# --------------------------------------------------------------------------- #
def test_hello_find_outline_bye_and_expired_session(server) -> None:
    instance, _engine = server
    status, hello = _graph(instance, {"op": "hello"})
    assert status == 200 and "e" not in hello
    sid = hello["sid"]

    status, found = _graph(instance, {"op": "find", "session": sid, "q": "add"})
    assert status == 200 and "e" not in found, found
    assert found.get("r")

    status, outline = _graph(instance, {
        "op": "outline", "session": sid, "key": "sym:pkg/core.py::add"})
    assert status == 200 and "e" not in outline, outline

    status, bye = _graph(instance, {"op": "bye", "session": sid})
    assert status == 200 and bye["bye"] == 1

    status, expired = _graph(instance, {"op": "find", "session": sid, "q": "add"})
    assert status == 200
    assert "e" in expired and "expired session" in expired["e"]


def test_stale_snapshot_withholds_structure_over_http(repo: Path, server) -> None:
    instance, _engine = server
    status, hello = _graph(instance, {"op": "hello"})
    assert status == 200 and hello["f"] == "fresh"
    sid = hello["sid"]

    _write(repo, "pkg/core.py", CORE.replace("return a + b", "return a - b"))
    status, withheld = _graph(instance, {"op": "find", "session": sid, "q": "add"})
    assert status == 200
    assert withheld["f"] == "stale"
    assert withheld["e"] == "snapshot is not verifiably fresh; structural data is withheld"
    assert "r" not in withheld and "rows" not in withheld


def test_ozma_context_endpoint_fresh_stale_and_bounded(repo: Path, server) -> None:
    instance, _engine = server
    status, fresh = _context(instance, {
        "task": "add", "budget_tokens": 80, "max_items": 2,
        "allow_stale": False})
    assert status == 200 and fresh["version"] == 1
    assert fresh["ok"] is True and fresh["freshness"] == "current"
    assert sum(len(item["snippet"]["text"]) for item in fresh["items"]) <= 80 * 4

    _write(repo, "pkg/core.py", CORE.replace("return a + b", "return a - b"))
    status, stale = _context(instance, {"task": "add", "budget_tokens": 80})
    assert status == 200 and stale["ok"] is False
    assert stale["freshness"] == "stale" and stale["items"] == []

    status, allowed = _context(instance, {
        "task": "add", "budget_tokens": 80, "allow_stale": True})
    assert status == 200 and allowed["ok"] is True
    assert allowed["freshness"] == "stale"

    status, _headers, payload = _request(
        instance, "POST", engine_http.CONTEXT_PATH,
        body={"task": "add", "unexpected": 1})
    assert status == 400
    assert _json(payload)["error"]["code"] == "invalid_context_request"
    status, _headers, payload = _request(
        instance, "POST", engine_http.CONTEXT_PATH, body={"task": " "})
    assert status == 400


def test_saturation_returns_503_and_health_still_works(repo: Path) -> None:
    class BlockingEngine:
        mem = None

        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.closed = False

        def handle(self, args: dict[str, Any]) -> dict[str, Any]:
            self.started.set()
            self.release.wait(5)
            return {"ok": True, "op": args.get("op")}

        def close(self) -> None:
            self.closed = True

    engine = BlockingEngine()
    instance = engine_http.create_server(repo, port=0, token=TOKEN,
                                         engine=engine, max_concurrent=1)
    thread = threading.Thread(target=instance.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    first: list[tuple[int, dict[str, str], bytes]] = []
    try:
        worker = threading.Thread(
            target=lambda: first.append(_request(
                instance, "POST", engine_http.GRAPH_PATH,
                body={"op": "hello"})), daemon=True)
        worker.start()
        assert engine.started.wait(3)
        status, headers, payload = _request(
            instance, "POST", engine_http.GRAPH_PATH, body={"op": "hello"})
        assert status == 503 and headers["Retry-After"] == "1"
        assert _json(payload)["error"]["code"] == "saturated"
        status, _headers, _payload = _request(
            instance, "GET", engine_http.HEALTH_PATH, token=None)
        assert status == 200
        engine.release.set()
        worker.join(timeout=3)
        assert first and first[0][0] == 200
    finally:
        engine.release.set()
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=3)


def test_internal_errors_are_contained(repo: Path) -> None:
    class BoomEngine:
        mem = None

        def handle(self, _args: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("sensitive internal detail")

        def close(self) -> None:
            pass

    instance = engine_http.create_server(repo, port=0, token=TOKEN,
                                         engine=BoomEngine())
    thread = threading.Thread(target=instance.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        status, _headers, payload = _request(
            instance, "POST", engine_http.GRAPH_PATH, body={"op": "hello"})
        assert status == 500
        body = _json(payload)
        assert body["error"]["code"] == "internal_error"
        assert b"sensitive" not in payload and b"RuntimeError" not in payload
        assert "nodes" not in body and "rows" not in body
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=3)


# --------------------------------------------------------------------------- #
# read-only boundary, logging and shutdown
# --------------------------------------------------------------------------- #
def test_http_calls_do_not_write_or_execute_and_do_not_log_the_token(
        repo: Path, server, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    instance, _engine = server
    caplog.set_level(logging.INFO, logger="ozagia.engine_http")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the HTTP transport must not run tests or write files")

    monkeypatch.setattr("ozagia.check.run_tests", forbidden)
    monkeypatch.setattr("ozagia.edit.txn.write_source", forbidden)
    before = {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in repo.rglob("*") if path.is_file()
        and ".ozagia" not in path.parts and ".git" not in path.parts
    }
    status, hello = _graph(instance, {"op": "hello"})
    assert status == 200
    sid = hello["sid"]
    for op, extra in (
        ("find", {"q": "add"}),
        ("outline", {"key": "sym:pkg/core.py::add"}),
        ("expand", {"keys": ["sym:pkg/core.py::add"], "edges": ["callers"]}),
        ("impact", {"keys": ["sym:pkg/core.py::add"]}),
        ("tests", {"key": "sym:pkg/core.py::add"}),
        ("context", {"task": "add", "budget": 400}),
        ("shape", {}),
    ):
        status, result = _graph(instance, {"op": op, "session": sid, **extra})
        assert status == 200 and "e" not in result, (op, result)
        assert TOKEN.encode() not in json.dumps(result).encode()
    status, _ = _context(instance, {"task": "add", "budget_tokens": 100})
    assert status == 200
    status, bye = _graph(instance, {"op": "bye", "session": sid})
    assert status == 200 and bye["bye"] == 1
    after = {
        path.relative_to(repo).as_posix(): path.read_bytes()
        for path in repo.rglob("*") if path.is_file()
        and ".ozagia" not in path.parts and ".git" not in path.parts
    }
    assert before == after
    assert TOKEN not in caplog.text
    for op in ("edit", "create", "check", "refresh"):
        status, rejected = _graph(instance, {"op": op, "session": sid})
        assert status == 200 and "e" in rejected
        assert "unknown op" in rejected["e"]


def test_owned_engine_and_server_close_cleanly(repo: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OZAGIA_ENGINE_SPEC", "0")
    build_index(repo, state_path(repo, "graph.json"), include_docs="features",
                include_git=False)
    instance = engine_http.create_server(repo, port=0, token=TOKEN)
    assert instance.owns_engine is True
    assert instance.port > 0
    thread = threading.Thread(target=instance.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    status, _headers, _payload = _request(
        instance, "GET", engine_http.HEALTH_PATH, token=None)
    assert status == 200
    engine = instance.engine
    instance.shutdown()
    instance.server_close()
    instance.close_engine()
    thread.join(timeout=3)
    assert thread.is_alive() is False
    assert engine.spec_worker is None


def test_cli_serve_port_zero_prints_the_chosen_port_and_stops_on_sigterm(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_index(repo, state_path(repo, "graph.json"), include_docs="features",
                include_git=False)
    env = os.environ.copy()
    env[engine_http.TOKEN_ENV] = TOKEN
    env["OZAGIA_ENGINE_SPEC"] = "0"
    env.pop("OZAGIA_STATE_DIR", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "ozagia", "engine", "serve",
         "--root", str(repo), "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        lines: list[str] = []
        deadline = time.monotonic() + 15
        port = None
        while time.monotonic() < deadline:
            ready, _w, _x = select.select([proc.stdout], [], [], 0.2)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                break
            lines.append(line)
            if "Listening on http://127.0.0.1:" in line:
                port = int(line.rsplit(":", 1)[1].split()[0])
                break
        assert port is not None, "".join(lines)
        assert port > 0
        status, _headers, payload = _request(
            type("S", (), {"server_address": ("127.0.0.1", port),
                           "port": port})(),
            "GET", engine_http.HEALTH_PATH, token=None)
        assert status == 200 and _json(payload)["ok"] is True
        proc.terminate()
        proc.wait(timeout=10)
        assert proc.returncode == 0
        remainder = proc.stdout.read() + proc.stderr.read()
        assert TOKEN not in "".join(lines) + remainder
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_cli_registers_engine_serve_and_token() -> None:
    parser = cli.build_parser()
    serve = parser.parse_args(["engine", "serve", "--port", "0"])
    assert serve.command == "engine" and serve.action == "serve"
    assert serve.port == 0 and serve.host == "127.0.0.1"
    assert serve.allow_remote is False
    token = parser.parse_args(["engine", "token", "--root", "/tmp/x"])
    assert token.command == "engine" and token.action == "token"
