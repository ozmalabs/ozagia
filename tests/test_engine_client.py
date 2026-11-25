"""The reusable stateful client over the loopback graph-engine transport.

Every test uses the real ``ThreadingHTTPServer`` from ``engine_http`` and a
tiny git fixture.  No model, external network or target execution is involved.
"""
from __future__ import annotations

import json
import logging
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ozagia import engine_http
from ozagia.core.graph_engine import GraphEngine, InlineSpecWorker
from ozagia.core.memory import DevMemory, build_index
from ozagia.engine_client import (
    TOKEN_ENV,
    EngineAuthError,
    EngineBudgetError,
    EngineConfigError,
    EnginePagingError,
    EngineResultError,
    EngineSessionError,
    EngineTransportError,
    EngineWithheldError,
    GraphEngineClient,
)

TOKEN = "client-test-token-4e3f2a1b"
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
README = "# Fixture\n\nA small engine client fixture.\n"


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


@pytest.fixture()
def memory(repo: Path) -> DevMemory:
    dump = repo.parent / "state" / "graph.json"
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, dump.parent / "journal.jsonl", root=repo)


@pytest.fixture()
def server(repo: Path, memory: DevMemory):
    engine = GraphEngine(memory, spec_worker=InlineSpecWorker())
    instance = engine_http.create_server(
        repo, port=0, token=TOKEN, engine=engine)
    import threading

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


@pytest.fixture()
def client(server):
    instance, _engine = server
    with GraphEngineClient(instance.base_url, token=TOKEN) as value:
        yield value


# --------------------------------------------------------------------------- #
# compact payload helpers
# --------------------------------------------------------------------------- #
def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    cols = payload.get("c")
    rows = payload.get("r")
    if not isinstance(cols, list) or not isinstance(rows, list):
        return []
    return [dict(zip(cols, row)) for row in rows if isinstance(row, list)]


def _keys(payload: dict[str, Any]) -> list[str]:
    return [str(row["k"]) for row in _rows(payload) if row.get("k")]


def _row(payload: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    for row in _rows(payload):
        if key is None or row.get("k") == key:
            return row
    raise AssertionError(f"no row for {key!r} in {payload!r}")


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #
def test_hello_find_outline_impact_shape_fetch_and_bye(server, client) -> None:
    instance, _engine = server
    hello = client.hello()
    assert hello["p"] == "ozagia.graph_engine/2"
    assert hello["sid"] == client.session
    assert hello["f"] == "fresh"
    assert "ops" not in hello  # detail stays opt-in

    detail = client.hello(caps=True)
    assert detail["sid"] == hello["sid"]
    assert "find" in detail["ops"] and "known" in detail["ft"]

    found = client.find("add")
    assert _keys(found)
    key = _keys(found)[0]
    assert key in {row.get("k") for row in _rows(found)}

    outline = client.outline(key)
    assert _rows(outline)
    assert _row(outline, key)["r"] == "root"

    impact = client.impact([key])
    assert impact.get("ct") or impact.get("counts")

    shape = client.shape()
    assert any(row.get("section") == "repo" or row.get("sec") == "repo"
               for row in _rows(shape))

    speculative = client.find("add", spec=True, inline=False, budget=2000)
    handles = (speculative.get("q") or {}).get("h") or []
    if handles:
        fetched = client.fetch(handles[:1], wait_ms=50)
        assert "e" not in fetched
        assert fetched.get("h") in handles or fetched.get("it")
    else:  # the fixture is allowed to suggest no candidate
        with pytest.raises(EngineResultError) as error:
            client.fetch()
        assert error.value.code == "no_handles"

    bye = client.bye()
    assert bye["bye"] == 1
    assert client.session is None
    assert instance.engine.sessions == {}


def test_repeated_find_outline_and_context_use_op_scoped_known(client) -> None:
    first = client.find("add")
    key = _keys(first)[0]
    find_hash = _row(first, key)["h"]
    assert {"op": "find", "key": key, "hash": find_hash} in client.known_entries()

    repeated = client.find("add")
    assert key in repeated.get("u", [])
    assert key not in _keys(repeated)

    outline = client.outline(key)
    outline_hash = _row(outline, key)["h"]
    assert outline_hash != find_hash
    outline_again = client.outline(key)
    assert key in outline_again.get("u", [])

    # The outline refresh must not overwrite the find hash, so find still
    # returns only the handle after an outline of the same key.
    find_again = client.find("add")
    assert key in find_again.get("u", [])
    assert all(entry["op"] == "find" for entry in client.known_entries("find"))
    assert all(entry["op"] == "outline"
               for entry in client.known_entries("outline"))
    assert any(entry["key"] == key and entry["hash"] == find_hash
               for entry in client.known_entries("find"))

    context = client.context("add two numbers", budget=2000)
    assert _rows(context)
    context_row = _rows(context)[0]
    context_key = context_row["k"]
    context_again = client.context("add two numbers", budget=2000)
    assert context_key in context_again.get("u", [])
    assert all(entry["op"] == "context"
               for entry in client.known_entries("context"))


def test_client_known_false_opts_out_of_server_auto_known(server, client) -> None:
    engine = server[1]
    engine.auto_known = True
    # A caller that does not manage hashes omits `known` entirely.
    client.use_known = False
    first = client.find("add")
    assert _keys(first) and "u" not in first
    again = client.find("add")
    assert again.get("u") and not _rows(again)
    # known=False is sent as an explicit empty list and forces full rows even
    # though the server remembers the hashes.
    full = client.find("add", known=False)
    assert _keys(full) and "u" not in full
    assert len(_keys(full)) == len(_keys(first))


def test_stale_hash_reserializes_and_forget_clears(client) -> None:
    first = client.find("add")
    key = _keys(first)[0]
    good = _row(first, key)["h"]

    # Simulate a persisted/stale hash: the engine refuses it and sends the row.
    client.remember("find", key, "0" * 16)
    stale = client.find("add")
    assert key in _keys(stale)
    assert key not in stale.get("u", [])
    assert _row(stale, key)["h"] != "0" * 16
    assert any(entry["key"] == key and entry["hash"] == good
               for entry in client.known_entries("find"))

    assert client.forget(key, op="find") == 1
    forgotten = client.find("add")
    assert key in _keys(forgotten)
    assert key not in forgotten.get("u", [])

    client.remember("find", key, good)
    client.clear_known(op="find")
    assert client.known_entries("find") == []


def test_paging_follows_next_and_covers_every_hit(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    hits = _find_hits(20)
    monkeypatch.setattr(GraphEngine, "_surface",
                        lambda self: _FakeSurface(hits))

    page = client.find("helper", limit=20, budget=600)
    assert page.get("n"), page
    seen = set(_keys(page))
    offsets = [int(page["n"]["offset"])]
    for _ in range(20):
        if client.next_request(page) is None:
            break
        page = client.next_page(page, budget=600)
        assert page.get("t", 0) <= 600
        seen.update(_keys(page))
        if page.get("n"):
            offsets.append(int(page["n"]["offset"]))
    assert seen == {hit["key"] for hit in hits}
    assert offsets == sorted(offsets)
    assert all(left < right for left, right in zip(offsets, offsets[1:]))

    with pytest.raises(EnginePagingError):
        client.next_page({"r": [], "t": 1})


def test_budget_too_small_is_typed_and_next_retries(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GraphEngine, "_surface",
                        lambda self: _FakeSurface(_find_hits(20)))
    with pytest.raises(EngineBudgetError) as error:
        client.find("helper", limit=20, budget=64)
    failure = error.value
    assert failure.payload["e"] == "budget_too_small"
    assert failure.next is not None
    assert failure.suggested_budget is not None
    assert failure.suggested_budget >= 1200

    raw = client.find("helper", limit=20, budget=64, raise_on_error=False)
    assert raw["e"] == "budget_too_small"
    retried = client.request(failure.next)
    assert "e" not in retried
    assert retried.get("r") or retried.get("u")


def test_stale_snapshot_is_withheld_with_typed_error(
        repo: Path, client) -> None:
    client.hello()
    _write(repo, "pkg/core.py", CORE.replace("return a + b", "return a - b"))
    with pytest.raises(EngineWithheldError) as error:
        client.find("add")
    withheld = error.value
    assert withheld.freshness == "stale"
    assert "withheld" in withheld.code
    assert "r" not in withheld.payload and "rows" not in withheld.payload

    raw = client.find("add", raise_on_error=False)
    assert raw["e"] == withheld.code
    assert raw["f"] == "stale"
    assert client.bye()["bye"] == 1


def test_auth_failure_is_typed_and_the_token_is_never_logged(
        server, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    instance, _engine = server
    bad = GraphEngineClient(instance.base_url, token="wrong-" + TOKEN)
    with pytest.raises(EngineAuthError) as error:
        bad.hello()
    assert error.value.status == 401
    assert error.value.code == "unauthorized"
    assert "wrong-" + TOKEN not in str(error.value)
    assert TOKEN not in repr(bad)
    assert TOKEN not in json.dumps(bad.stats())

    caplog.set_level(logging.INFO, logger="ozagia")
    with GraphEngineClient(instance.base_url, token=TOKEN) as client:
        client.hello()
        client.find("add")
        assert TOKEN not in repr(client)
        assert TOKEN not in json.dumps(client.stats())
    assert TOKEN not in caplog.text


def test_expired_session_is_typed_and_hello_recovers(server, client) -> None:
    instance, engine = server
    client.find("add")
    old = client.session
    assert old and client.known_entries()

    engine.sessions.pop(old)  # simulate an evicted/expired server session
    with pytest.raises(EngineSessionError):
        client.find("add")
    assert client.session == old  # no silent re-hello or retry

    hello = client.hello()
    assert hello["sid"] != old
    assert client.session == hello["sid"]
    assert client.known_entries() == []  # a new session starts with no deltas
    assert client.find("add").get("r")


def test_counters_and_close_cleanup(server) -> None:
    instance, engine = server
    client = GraphEngineClient(instance.base_url, token=TOKEN)
    client.hello()
    client.find("add")
    client.find("add")
    stats = client.stats()
    assert stats["requests"] >= 3
    assert stats["responses"] >= 3
    assert stats["engine_tokens"] > 0
    assert stats["bytes_in"] > 0 and stats["bytes_out"] > 0
    assert stats["ops"]["find"] == 2
    assert stats["known"] >= 1
    assert stats["session"] == client.session

    client.close()
    closed = client.stats()
    assert closed["session"] is None
    assert closed["known"] == 0
    assert engine.sessions == {}
    client.close()  # idempotent

    assert client.healthz()["ok"] is True
    assert client.stats()["ops"]["healthz"] == 1


def test_token_from_environment_and_file(
        repo: Path, server, monkeypatch: pytest.MonkeyPatch) -> None:
    instance, _engine = server
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    from_env = GraphEngineClient(instance.base_url, root=repo)
    assert from_env.hello()["sid"]
    from_env.close()

    path = engine_http.token_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TOKEN + "\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    from_file = GraphEngineClient(instance.base_url, root=repo)
    assert from_file.hello()["sid"]
    from_file.close()

    path.chmod(0o644)
    with pytest.raises(EngineConfigError, match="accessible"):
        GraphEngineClient(instance.base_url, root=repo)

    monkeypatch.setenv(TOKEN_ENV, "bad token with spaces")
    with pytest.raises(EngineConfigError, match="whitespace"):
        GraphEngineClient(instance.base_url, root=repo)


def test_transport_failure_is_typed() -> None:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    try:
        client = GraphEngineClient(
            f"http://127.0.0.1:{port}", token=TOKEN, timeout=1.0)
        with pytest.raises(EngineTransportError):
            client.hello()
        assert client.requests == 1
        assert client.errors == 1
        assert TOKEN not in repr(client)
    finally:
        holder.close()


def test_repeated_find_savings_are_measurable(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GraphEngine, "_surface",
                        lambda self: _FakeSurface(_find_hits(20)))
    before = client.stats()["bytes_in"]
    first = client.find("helper", limit=20, budget=4000)
    first_bytes = client.stats()["bytes_in"] - before
    assert len(first["r"]) == 20

    before = client.stats()["bytes_in"]
    repeated = client.find("helper", limit=20, budget=4000)
    repeated_bytes = client.stats()["bytes_in"] - before
    assert len(repeated.get("u", [])) == 20
    assert not repeated.get("r")
    assert repeated["t"] < first["t"]
    assert repeated_bytes < first_bytes
    print("engine-client known probe:",
          {"first_t": first["t"], "repeated_t": repeated["t"],
           "first_bytes": first_bytes, "repeated_bytes": repeated_bytes})


# --------------------------------------------------------------------------- #
# deterministic synthetic find surface for paging/savings
# --------------------------------------------------------------------------- #
class _FakeSurface:
    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self.hits = hits

    def find(self, query: str, kinds: list[str] | None = None,
             limit: int = 20) -> dict[str, Any]:
        return {"hits": self.hits[:limit]}


def _find_hits(count: int = 20) -> list[dict[str, Any]]:
    signature = "def helper(number: int, *, mode: str | None = None) -> str"
    summary = ("Return the resolved helper description for a numbered "
               "symbol in the synthetic find fixture used by the client test.")
    return [{
        "key": f"sym:src/mod.py::helper_{index}",
        "name": f"helper_{index}",
        "file": "src/mod.py",
        "line": 100 + index,
        "signature": signature,
        "summary": summary,
        "score": 0.5,
    } for index in range(count)]
