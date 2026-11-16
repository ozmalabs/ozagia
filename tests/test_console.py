"""Tests for the loopback-only, read-only standalone console."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlencode

import pytest

from ozagia import console


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture()
def server(repo: Path):
    instance = console.create_server(repo, port=0)
    thread = threading.Thread(
        target=instance.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=3)


def _request(server: console.ConsoleServer, path: str, *,
             method: str = "GET", headers: dict[str, str] | None = None,
             body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    host = server.server_address[0]
    if host == "::1":
        host = "::1"
    connection = http.client.HTTPConnection(host, server.port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
    finally:
        connection.close()
    return result


def _json(body: bytes) -> object:
    return json.loads(body.decode("utf-8"))


def _token_path(server: console.ConsoleServer, path: str) -> str:
    separator = "&" if "?" in path else "?"
    return path + separator + urlencode({"token": server.state.token})


def _auth(server: console.ConsoleServer) -> dict[str, str]:
    return {"Cookie": f"{console.COOKIE_NAME}={server.state.token}"}


def _cookie_from(headers: dict[str, str]) -> str:
    raw = headers.get("Set-Cookie", "")
    assert raw
    return raw.split(";", 1)[0]


def _graph() -> dict:
    return {
        "version": 2,
        "nodes": [
            {"id": 0, "type": "Repo", "key": "repo:",
             "attrs": {"name": "demo", "root": "/tmp/demo", "head": "abc123"}},
            {"id": 1, "type": "File", "key": "file:src/app.py",
             "attrs": {"name": "src/app.py", "path": "src/app.py"}},
            {"id": 2, "type": "File", "key": "file:tests/test_app.py",
             "attrs": {"name": "tests/test_app.py",
                       "path": "tests/test_app.py"}},
        ],
        "edges": [
            {"src": 0, "type": "contains", "tgt": 1, "attrs": {}},
            {"src": 0, "type": "contains", "tgt": 2, "attrs": {}},
            {"src": 1, "type": "imports", "tgt": 2, "attrs": {}},
        ],
    }


# --------------------------------------------------------------------------- #
# configuration, binding and URL
# --------------------------------------------------------------------------- #
def test_create_server_is_loopback_and_uses_a_random_url_safe_token(repo: Path):
    server = console.create_server(repo, port=0)
    try:
        assert server.state.token
        assert len(server.state.token) >= 32
        assert set(server.state.token) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        assert server.url.startswith("http://127.0.0.1:")
        assert urlencode({"token": server.state.token}) in server.url
        assert server.state.root == repo.resolve()
        assert server.state.remote is False
    finally:
        server.server_close()


def test_non_loopback_host_is_refused_unless_token_is_explicit(repo: Path):
    with pytest.raises(console.ConsoleError, match="non-loopback"):
        console.create_server(repo, host="0.0.0.0", port=0)
    with pytest.raises(console.ConsoleError, match="explicit --token"):
        console.create_server(repo, host="0.0.0.0", port=0, allow_remote=True)


def test_invalid_root_and_token_are_rejected(tmp_path: Path):
    with pytest.raises(console.ConsoleError, match="root"):
        console.create_server(tmp_path / "missing", port=0)
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(console.ConsoleError, match="token"):
        console.create_server(root, port=0, token="not a token")
    with pytest.raises(console.ConsoleError, match="port"):
        console.create_server(root, port=70_000)


def test_explicit_token_and_port_validation(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    server = console.create_server(root, port=0, token="explicit-token")
    try:
        assert server.state.token == "explicit-token"
        assert "token=explicit-token" in server.url
    finally:
        server.server_close()


# --------------------------------------------------------------------------- #
# HTML and authentication
# --------------------------------------------------------------------------- #
def test_html_is_self_contained_and_has_every_tab(server: console.ConsoleServer):
    status, headers, body = _request(server, "/")
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert headers["X-Ozagia-Read-Only"] == "true"
    text = body.decode("utf-8")
    assert text.lower().startswith("<!doctype html>")
    for tab in ("Graph", "Status", "Worktree", "Testing", "Research", "Actions"):
        assert f">{tab}<" in text or tab in text
    assert "<script src" not in text.lower()
    assert "<link " not in text.lower()
    assert "https://" not in text
    assert "http://" not in text
    assert "@import" not in text
    assert "url(" not in text
    assert "cdn." not in text.lower()
    assert "Read-only" in text
    assert "No external assets" in text


def test_auth_token_cookie_and_cors(server: console.ConsoleServer, monkeypatch):
    monkeypatch.setattr(console.core_status, "status",
                        lambda root, **kwargs: {"marker": "status", "root": str(root), **kwargs})

    status, headers, body = _request(server, "/api/status")
    assert status == 401
    assert _json(body)["error"]["code"] == "unauthorized"
    assert "Access-Control-Allow-Origin" not in headers

    status, headers, body = _request(server, "/?token=" + server.state.token)
    assert status == 200
    set_cookie = headers["Set-Cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Path=/" in set_cookie
    assert "Access-Control-Allow-Origin" not in headers
    cookie = _cookie_from(headers)

    status, headers, body = _request(
        server, "/api/status", headers={"Cookie": cookie})
    assert status == 200
    assert _json(body)["marker"] == "status"
    assert "Access-Control-Allow-Origin" not in headers

    status, headers, _ = _request(
        server, "/api/status?token=" + server.state.token)
    assert status == 200
    assert "HttpOnly" in headers["Set-Cookie"]

    status, headers, body = _request(
        server, "/api/status?token=wrong-token")
    assert status == 401
    assert _json(body)["error"]["code"] == "unauthorized"
    assert "Set-Cookie" not in headers


def test_unauthorized_api_is_rejected_before_dispatch(server: console.ConsoleServer,
                                                      monkeypatch):
    called = False

    def fail(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not be called without a token")

    monkeypatch.setattr(console.core_status, "status", fail)
    status, headers, body = _request(server, "/api/status")
    assert status == 401
    assert called is False
    assert "Access-Control-Allow-Origin" not in headers


def test_head_and_methods(server: console.ConsoleServer, monkeypatch):
    monkeypatch.setattr(console.core_apertures, "available_apertures",
                        lambda root: {"demo": {"id": "demo"}})
    status, headers, body = _request(
        server, "/api/apertures", method="HEAD", headers=_auth(server))
    assert status == 200
    assert body == b""
    assert int(headers["Content-Length"]) > 0

    status, headers, _ = _request(
        server, "/api/apertures", method="POST", headers=_auth(server))
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
    assert "Access-Control-Allow-Origin" not in headers

    status, headers, _ = _request(
        server, "/api/apertures", method="OPTIONS", headers=_auth(server))
    assert status == 405
    assert "Access-Control-Allow-Origin" not in headers


def test_traversal_and_unknown_paths_are_plain_404(server: console.ConsoleServer):
    for path in (
        "/api/not-a-route",
        "/../../etc/passwd",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/api/../files",
        "/api/status/../../etc/passwd",
    ):
        status, headers, body = _request(server, path, headers=_auth(server))
        assert status == 404, path
        assert _json(body)["error"]["code"] == "not_found"
    status, _, body = _request(server, "/api/not-a-route")
    assert status == 401
    assert _json(body)["error"]["code"] == "unauthorized"


def test_query_limits_and_unknown_parameters(server: console.ConsoleServer):
    status, headers, body = _request(
        server, "/api/status?" + "x" * 5_000, headers=_auth(server))
    assert status == 414
    assert _json(body)["error"]["code"] == "query_too_large"

    many = "&".join(f"p{i}=1" for i in range(33))
    status, _, body = _request(
        server, "/api/status?" + many, headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "too_many_parameters"

    status, _, body = _request(
        server, "/api/status?path=/etc/passwd", headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "unsupported_parameter"

    status, _, body = _request(
        server, "/api/status?top=abc", headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "invalid_parameter"


def test_request_body_is_rejected(server: console.ConsoleServer):
    status, _, body = _request(
        server, "/api/status", method="POST", headers=_auth(server),
        body=b"{}")
    assert status == 405
    assert _json(body)["error"]["code"] == "method_not_allowed"

    # A GET with a body is rejected before the route runs.
    connection = http.client.HTTPConnection(
        server.server_address[0], server.port, timeout=5)
    try:
        connection.putrequest("GET", "/api/status")
        connection.putheader("Cookie", f"{console.COOKIE_NAME}={server.state.token}")
        connection.putheader("Content-Length", "2")
        connection.endheaders(b"{}")
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 413
        assert json.loads(payload)["error"]["code"] == "request_body_not_allowed"
    finally:
        connection.close()


# --------------------------------------------------------------------------- #
# API payloads
# --------------------------------------------------------------------------- #
def test_status_endpoint(server: console.ConsoleServer, monkeypatch):
    seen: dict[str, object] = {}

    def fake_status(root, **kwargs):
        seen["root"] = root
        seen.update(kwargs)
        return {"schema": 1, "checked_at": "now", "root": str(root),
                "ok": True, "errors": [], "degraded": []}

    monkeypatch.setattr(console.core_status, "status", fake_status)
    status, headers, body = _request(
        server, "/api/status?top=7", headers=_auth(server))
    assert status == 200
    payload = _json(body)
    assert payload["ok"] is True
    assert seen["root"] == server.state.root
    assert seen["top"] == 7
    assert headers["Content-Type"].startswith("application/json")


def test_apertures_endpoint(server: console.ConsoleServer, monkeypatch):
    monkeypatch.setattr(
        console.core_apertures, "available_apertures",
        lambda root: {"architecture": {"id": "architecture",
                                       "title": "Architecture"}})
    status, _, body = _request(server, "/api/apertures", headers=_auth(server))
    assert status == 200
    assert _json(body)["architecture"]["title"] == "Architecture"


def test_view_endpoint_and_missing_graph(server: console.ConsoleServer):
    state = server.state.root / ".ozagia"
    state.mkdir()
    graph_path = state / "graph.json"
    graph_path.write_text(json.dumps(_graph()), encoding="utf-8")

    status, _, body = _request(
        server, "/api/view?aperture=architecture&depth=1&node=0",
        headers=_auth(server))
    assert status == 200
    payload = _json(body)
    assert payload["aperture"] == "architecture"
    assert payload["schema"] == 1
    assert {node["id"] for node in payload["nodes"]} == {"0", "1", "2"}
    assert payload["snapshot"]["root"] == "/tmp/demo"
    assert payload["snapshot"]["head"] == "abc123"
    assert payload["omitted"]["nodes"] == 0
    assert payload["provenance"]["graph"] == "dev-memory"
    assert str(graph_path) in payload["provenance"]["sources"]

    status, _, body = _request(
        server, "/api/view?aperture=unknown", headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "unknown_aperture"

    status, _, body = _request(
        server, "/api/view?aperture=architecture&depth=-1",
        headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "invalid_parameter"

    status, _, body = _request(
        server, "/api/view?aperture=architecture&graph=/etc/passwd",
        headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "unsupported_parameter"

    graph_path.unlink()
    status, _, body = _request(
        server, "/api/view?aperture=architecture", headers=_auth(server))
    assert status == 404
    assert _json(body)["error"]["code"] == "missing_graph"
    assert "never builds it" in _json(body)["error"]["message"]


def test_worktree_resources_and_testing_endpoints(server: console.ConsoleServer,
                                                  monkeypatch):
    worktree_seen: dict[str, object] = {}
    testing_seen: dict[str, object] = {}
    resources_seen: dict[str, object] = {}

    def fake_worktree(root, **kwargs):
        worktree_seen["root"] = root
        worktree_seen.update(kwargs)
        return {"schema": 1, "ok": True, "base": kwargs.get("base"),
                "checked_at": "now", "worktrees": [], "findings": []}

    def fake_resources(root, **kwargs):
        resources_seen["root"] = root
        return {"schema": 1, "checked_at": "now", "host": {}, "disks": [],
                "top_groups": [], "policy": {}, "history": {},
                "enforcement": {}}

    def fake_plan(root, **kwargs):
        testing_seen["root"] = root
        testing_seen.update(kwargs)
        return {"schema": "ozagia.testing.plan.v1", "mode": "none",
                "requested_mode": "none", "changed_files": [],
                "selected_files": [], "selected_tests": [], "counts": {},
                "omitted": [], "not_assessed": ["no test executed"],
                "executed": False, "truth_assessed": False,
                "claim": "test selection only"}

    monkeypatch.setattr(console.core_worktree, "check", fake_worktree)
    monkeypatch.setattr(console.core_resources, "status", fake_resources)
    monkeypatch.setattr(console.core_testing, "plan", fake_plan)

    status, _, body = _request(
        server, "/api/worktree?base=main", headers=_auth(server))
    assert status == 200
    assert _json(body)["ok"] is True
    assert worktree_seen["base"] == "main"
    assert worktree_seen["root"] == server.state.root

    status, _, body = _request(
        server, "/api/resources", headers=_auth(server))
    assert status == 200
    assert resources_seen["root"] == server.state.root

    status, _, body = _request(
        server, "/api/testing/plan?base=HEAD~1&head=HEAD&task=code_review",
        headers=_auth(server))
    assert status == 200
    assert _json(body)["executed"] is False
    assert testing_seen["base"] == "HEAD~1"
    assert testing_seen["head"] == "HEAD"
    assert testing_seen["task_class"] == "code_review"
    assert testing_seen["root"] == server.state.root

    status, _, body = _request(
        server, "/api/testing/plan?base=--bad", headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "invalid_parameter"


def test_research_endpoint_and_missing_investigation(server: console.ConsoleServer,
                                                     monkeypatch):
    state = {"nodes": [], "edges": [], "investigation_id": "research-demo"}
    seen: dict[str, object] = {}

    def fake_load(investigation, *, root):
        seen["load"] = (investigation, root)
        return state, server.state.root / ".ozagia" / "research" / "demo.json"

    def fake_view(value, view, *, root, source, investigation_id):
        seen["view"] = (value, view, root, source, investigation_id)
        return {"schema": 1, "schema_name": "ozagia.research_ui.view",
                "view": view, "title": "Library", "investigation_id": investigation_id,
                "columns": [{"key": "reference_id", "label": "Reference"}],
                "rows": [{"reference_id": "reference-1"}],
                "summary": {"references": 1},
                "provenance": {"generator": "test"},
                "omitted": {"rows": 0}, "not_assessed": ["truth"],
                "truth_assessed": False, "note": "projection only"}

    monkeypatch.setattr(console.core_research_ui, "load_state", fake_load)
    monkeypatch.setattr(console.core_research_ui, "build_research_view", fake_view)

    status, _, body = _request(
        server, "/api/research?investigation=research-demo&view=library",
        headers=_auth(server))
    assert status == 200
    payload = _json(body)
    assert payload["investigation_id"] == "research-demo"
    assert payload["truth_assessed"] is False
    assert seen["load"] == ("research-demo", server.state.root)
    assert seen["view"][1] == "library"

    status, _, body = _request(
        server, "/api/research?investigation=research-demo&view=unknown",
        headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "unknown_research_view"

    status, _, body = _request(server, "/api/research", headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "missing_parameter"

    # Missing investigations are tested separately, without the fake loader.


def test_files_endpoint_and_missing_ledger(server: console.ConsoleServer,
                                           monkeypatch):
    status, _, body = _request(server, "/api/files", headers=_auth(server))
    assert status == 200
    payload = _json(body)
    assert payload["read_only"] is True
    assert payload["ledger"]["present"] is False
    assert payload["records"] == []
    assert payload["list"] == []
    assert payload["orphans"] == []

    monkeypatch.setattr(console.core_files, "list_files",
                        lambda root, **kwargs: [{
                            "path": "notes/new.md", "classification": "note",
                            "status": "active", "created_at": "2026-11-16",
                            "reason": "notes", "session": "s", "hash": "a" * 64,
                        }])
    monkeypatch.setattr(console.core_files, "orphans",
                        lambda root: [{"path": "notes/old.md"}])
    monkeypatch.setattr(console.core_files, "ledger_path",
                        lambda root: server.state.root / ".ozagia" / "files" / "ledger.jsonl")
    status, _, body = _request(
        server, "/api/files?classification=note&status=active&limit=5",
        headers=_auth(server))
    assert status == 200
    payload = _json(body)
    assert payload["records"][0]["path"] == "notes/new.md"
    assert payload["orphans"][0]["path"] == "notes/old.md"
    assert payload["counts"]["records_limit"] == 5
    assert payload["ledger"]["present"] is False


def test_actions_list_preview_and_no_apply(server: console.ConsoleServer,
                                           monkeypatch):
    before = sorted(str(path.relative_to(server.state.root))
                    for path in server.state.root.rglob("*"))

    status, _, body = _request(server, "/api/actions", headers=_auth(server))
    assert status == 200
    payload = _json(body)
    assert isinstance(payload, list)
    assert any(row["name"] == "service.list" for row in payload)
    assert all("confirmation" not in row for row in payload)

    seen: dict[str, object] = {}

    def fake_preview(root, action, args):
        seen.update(root=root, action=action, args=dict(args))
        return {"schema": 1, "action": action, "mode": "preview",
                "state": "previewed", "confirmation": None,
                "apply_available": False, "read_only": True,
                "operation": "nothing", "expected_effect": "none",
                "preconditions": [], "risks": [], "notes": [],
                "current": None, "current_state": "configured",
                "applicable": True, "argv": [], "live": False, "gated": False,
                "manager_call": None, "error": None, "ok": True,
                "audit": None, "audit_error": None, "result": None}

    monkeypatch.setattr(console, "_action_preview", fake_preview)
    status, _, body = _request(
        server, "/api/actions?action=service.start&name=api",
        headers=_auth(server))
    assert status == 200
    payload = _json(body)
    assert payload["confirmation"] is None
    assert payload["apply_available"] is False
    assert seen["action"] == "service.start"
    assert seen["args"] == {"name": "api"}

    after = sorted(str(path.relative_to(server.state.root))
                   for path in server.state.root.rglob("*"))
    assert before == after
    assert not (server.state.root / ".ozagia").exists()


def test_actual_preview_does_not_issue_a_token_or_write(server: console.ConsoleServer):
    status, _, body = _request(
        server, "/api/actions?action=service.list", headers=_auth(server))
    assert status == 200
    payload = _json(body)
    assert payload["mode"] == "preview"
    assert payload["confirmation"] is None
    assert payload["apply_available"] is False
    assert "no apply endpoint" in payload["note"]
    assert not (server.state.root / ".ozagia").exists()


def test_status_and_files_do_not_write_a_state_directory(server: console.ConsoleServer):
    status, _, body = _request(server, "/api/status", headers=_auth(server))
    assert status == 200
    assert _json(body)["root"] == str(server.state.root)
    status, _, body = _request(server, "/api/files", headers=_auth(server))
    assert status == 200
    assert _json(body)["ledger"]["present"] is False
    assert not (server.state.root / ".ozagia").exists()


# --------------------------------------------------------------------------- #
# shutdown and CLI
# --------------------------------------------------------------------------- #
def test_graceful_shutdown_stops_accepting_connections(repo: Path):
    server = console.create_server(repo, port=0)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        status, _, _ = _request(server, "/")
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert not thread.is_alive()
    with pytest.raises(OSError):
        _request(server, "/")


def test_cli_forwards_options_and_exits_zero(repo: Path, monkeypatch,
                                             capsys):
    seen: dict[str, object] = {}

    def fake_serve(root, **kwargs):
        seen["root"] = root
        seen.update(kwargs)

    monkeypatch.setattr(console, "serve", fake_serve)
    code = console.main(["--root", str(repo), "--port", "0",
                         "--token", "test-token"])
    assert code == 0
    assert Path(seen["root"]) == repo
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 0
    assert seen["token"] == "test-token"
    assert seen["open_browser"] is False
    assert seen["allow_remote"] is False
    output = capsys.readouterr().out
    assert "read-only" in output.lower()
    assert "no writes" in output.lower()


def test_cli_rejects_non_loopback_without_override(repo: Path):
    assert console.main(["--root", str(repo), "--host", "0.0.0.0"]) == 2


def test_rate_limiter_is_bounded():
    limiter = console._RateLimiter(limit=2, window_s=60.0)
    assert limiter.allow("a") is True
    assert limiter.allow("a") is True
    assert limiter.allow("a") is False
    assert limiter.allow("b") is True


# --------------------------------------------------------------------------- #
# one real core integration: test planning on a git checkout
# --------------------------------------------------------------------------- #
def _git(cwd: Path, *args: str) -> None:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    })
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True, env=env)


def test_real_testing_plan_endpoint_on_a_git_repo(tmp_path: Path):
    root = tmp_path / "git-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "app.py").write_text("def f():\n    return 1\n",
                                         encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(
        "from src.app import f\n\ndef test_f():\n    assert f() == 1\n",
        encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "initial")
    server = console.create_server(root, port=0)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        status, _, body = _request(
            server, "/api/testing/plan?base=HEAD", headers=_auth(server))
        assert status == 200
        payload = _json(body)
        assert payload["schema"] == "ozagia.testing.plan.v1"
        assert payload["executed"] is False
        assert payload["truth_assessed"] is False
        assert payload["mode"] == "none"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_missing_research_investigation_is_404(server: console.ConsoleServer):
    status, _, body = _request(
        server, "/api/research?investigation=missing.investigation",
        headers=_auth(server))
    assert status == 404
    assert _json(body)["error"]["code"] == "missing_investigation"


def test_action_preview_validation_errors_are_400(server: console.ConsoleServer):
    status, _, body = _request(
        server, "/api/actions?action=service.start", headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "action_error"

    status, _, body = _request(
        server, "/api/actions?action=service.start&name=api&live=1",
        headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "action_error"

    status, _, body = _request(
        server, "/api/actions?action=not.an.action", headers=_auth(server))
    assert status == 400
    assert _json(body)["error"]["code"] == "action_error"
