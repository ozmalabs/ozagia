"""test_dev_memory_mcp_migrated — stdio transport and argument checks of the
dev-memory MCP server.

Dev tooling, not agent runtime. These tests drive `serve()` in-process over
`io.BytesIO` streams, so they exercise the real line framing, batch handling
and error replies without starting a subprocess.
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.mcp.server import DevMemoryServer, serve

PING = b'{"jsonrpc":"2.0","id":5,"method":"ping"}\n'
ADD_EDIT = {"file": "domains/foo.py",
            "ops": [{"op": "replace_body", "symbol": "add",
                     "text": "return a + b + 1"}]}


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _fixture(root: Path) -> None:
    _write(root / "domains" / "foo.py", '''"""Foo domain."""


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b
''')
    _write(root / "seeds" / "demo.json", json.dumps({
        "id": "demo",
        "version": "1.2.0",
        "depends_on": [],
        "description": "demo seed",
        "nodes": [],
        "rules": [],
    }))
    _write(root / "README.md", "# Fixture repo\n\n`seeds/demo.json`.\n")


@pytest.fixture()
def server(tmp_path: Path, seed_adapter) -> DevMemoryServer:
    # The seed adapter makes dev_seed available for the argument checks.
    _fixture(tmp_path)
    out = tmp_path / "var" / "dev_graph.json"
    build_index(tmp_path, out, include_docs="none",
                include_git=False, include_scripts=False)
    mem = DevMemory(out, tmp_path / "var" / "journal.jsonl", root=tmp_path)
    return DevMemoryServer(mem)


def _serve(monkeypatch: pytest.MonkeyPatch, server: DevMemoryServer,
           payload: bytes) -> list[Any]:
    """Run serve() over `payload`; return every JSON line written to stdout."""
    stdout = io.BytesIO()
    monkeypatch.setattr(sys, "stdin",
                        SimpleNamespace(buffer=io.BytesIO(payload)))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=stdout))
    assert serve(server) == 0
    return [json.loads(line) for line in
            stdout.getvalue().splitlines() if line.strip()]


def _call(name: str, arguments: Any, req_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_dev_tools_is_listed_and_plans_paths(monkeypatch, server) -> None:
    out = _serve(monkeypatch, server,
                 b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
    tools = {tool["name"]: tool for tool in out[0]["result"]["tools"]}
    assert "dev_tools" in tools
    schema = tools["dev_tools"]["inputSchema"]
    assert set(schema["properties"]) == {
        "capabilities", "paths", "model_family", "probe_versions", "probe_sudo"}
    assert schema["properties"]["probe_versions"]["default"] is False
    assert schema["properties"]["probe_sudo"]["default"] is False
    # The tool is additive: the existing coding surface is unchanged.
    assert {"dev_context", "dev_working_set", "dev_environment"} <= set(tools)

    from ozagia.core import toolchain as T
    calls = []
    real = T.tool_plan

    def fake_plan(root, **kwargs):
        calls.append(dict(kwargs))
        kwargs.setdefault("which",
                          lambda name: "/usr/bin/uv" if name == "uv" else None)
        kwargs.setdefault("module_finder", lambda name: None)
        kwargs.setdefault("environ", {})
        return real(root, **kwargs)

    monkeypatch.setattr(T, "tool_plan", fake_plan)

    def no_command(*_args, **_kwargs):
        raise AssertionError("version/sudo probes must be off by default")

    monkeypatch.setattr(T, "_run_command", no_command)

    payload = json.dumps(_call("dev_tools", {
        "capabilities": ["pdf"], "paths": ["docs/report.pdf"],
    })).encode() + b"\n"
    out = _serve(monkeypatch, server, payload)
    result = json.loads(out[0]["result"]["content"][0]["text"])

    assert result["ok"] is True
    assert result["plan"]["request"]["capabilities"] == ["pdf"]
    assert result["plan"]["request"]["inferred"] == ["pdf"]
    assert result["text"].startswith("[ozagia] tools:")
    assert "pdf" in result["text"]
    assert calls[0]["probe_versions"] is False
    assert calls[0]["probe_sudo"] is False
    assert all(not option["would_run"]
               for capability in result["plan"]["capabilities"]
               for option in capability["install"])


def test_dev_secrets_is_read_only_and_calls_the_daemon(monkeypatch, server) -> None:
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
    assert "dev_secrets" in tools
    schema = tools["dev_secrets"]["inputSchema"]
    assert set(schema["properties"]) == {"action", "refs", "session", "harness"}
    assert schema["properties"]["action"]["enum"] == [
        "providers", "list", "check"]
    description = tools["dev_secrets"]["description"].lower()
    assert "never" in description and "value" in description
    assert "capture" not in schema["properties"]["action"]["enum"]

    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_daemon(self, op, fields):
        calls.append((op, dict(fields)))
        return {"ok": True, "checks": [{
            "reference": "secret://env/TOKEN", "status": "available",
            "provider": "env", "code": None, "detail": "set"}]}

    monkeypatch.setattr(DevMemoryServer, "_daemon", fake_daemon)
    resp = server.handle(_call("dev_secrets", {
        "action": "check", "refs": ["secret://env/TOKEN"],
        "session": "s1", "harness": "opencode"}))
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["checks"][0]["status"] == "available"
    assert calls == [("secrets", {
        "action": "check", "refs": ["secret://env/TOKEN"],
        "session": "s1", "harness": "opencode"})]

    # A capture action is not part of the tool's schema.
    bad = server.handle(_call("dev_secrets", {"action": "capture"}))
    assert bad["error"]["code"] == -32602



    out = _serve(monkeypatch, server, b"\n" + PING)
    assert out == [{"jsonrpc": "2.0", "id": 5, "result": {}}]


def test_batch_of_two_pings_gets_one_array(monkeypatch, server) -> None:
    batch = json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]).encode() + b"\n"
    out = _serve(monkeypatch, server, batch)
    assert len(out) == 1 and isinstance(out[0], list)
    assert [r["id"] for r in out[0]] == [1, 2]
    assert all(r["result"] == {} for r in out[0])


def test_batch_of_only_notifications_writes_nothing(monkeypatch, server) -> None:
    batch = b'[{"jsonrpc":"2.0","method":"notifications/initialized"}]\n'
    assert _serve(monkeypatch, server, batch) == []


def test_empty_batch_is_invalid_request(monkeypatch, server) -> None:
    out = _serve(monkeypatch, server, b"[]\n" + PING)
    assert out[0]["id"] is None
    assert out[0]["error"]["code"] == -32600
    assert out[1]["id"] == 5 and out[1]["result"] == {}


def test_bare_string_is_invalid_then_server_continues(monkeypatch, server) -> None:
    out = _serve(monkeypatch, server, b'"hello"\n' + PING)
    assert out[0]["id"] is None and out[0]["error"]["code"] == -32600
    assert out[1]["id"] == 5 and out[1]["result"] == {}


def test_malformed_json_is_parse_error_then_server_continues(
        monkeypatch, server) -> None:
    out = _serve(monkeypatch, server, b"{not json\n" + PING)
    assert out[0]["id"] is None
    assert out[0]["error"]["code"] == -32700
    assert out[0]["error"]["message"].startswith("parse error: ")
    assert out[1]["id"] == 5 and out[1]["result"] == {}


def test_notification_error_is_never_answered(monkeypatch, server) -> None:
    bad = b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"nope"}}\n'
    out = _serve(monkeypatch, server, bad + PING)
    assert [r["id"] for r in out] == [5]


def test_params_not_an_object_is_invalid_params(server) -> None:
    resp = server.handle({"jsonrpc": "2.0", "id": 1,
                          "method": "tools/call", "params": "x"})
    assert resp["error"]["code"] == -32602


def test_resources_read_exception_is_internal_error(
        monkeypatch, server) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(server.mem, "brief", boom)
    read = (b'{"jsonrpc":"2.0","id":7,"method":"resources/read",'
            b'"params":{"uri":"ozagia://brief"}}\n')
    out = _serve(monkeypatch, server, read + PING)
    assert out[0]["id"] == 7
    assert out[0]["error"]["code"] == -32603
    assert out[0]["error"]["message"] == "internal error: RuntimeError: boom"
    assert out[1]["id"] == 5 and out[1]["result"] == {}


def test_unknown_tool_is_invalid_params(server) -> None:
    resp = server.handle(_call("dev_no_such_tool", {}))
    assert resp["error"]["code"] == -32602
    assert "dev_no_such_tool" in resp["error"]["message"]


def test_unknown_argument_key_is_invalid_params(server) -> None:
    resp = server.handle(_call("dev_seed", {"seed_id": "demo", "bogus": 1}))
    assert resp["error"]["code"] == -32602
    assert "bogus" in resp["error"]["message"]


def test_research_mode_exposes_only_lazy_research_tools(monkeypatch) -> None:
    calls = []
    module = types.ModuleType("ozagia.research.mcp")
    module.research_tools = lambda: [{
        "name": "research_ping", "description": "research ping",
        "inputSchema": {"type": "object", "properties": {
            "value": {"type": "string"}}, "required": ["value"],
            "additionalProperties": False}}]
    module.call_research_tool = lambda name, args, workspace: calls.append(
        (name, args, workspace)) or {"ok": True}
    monkeypatch.setitem(sys.modules, "ozagia.research.mcp", module)

    server = DevMemoryServer(None, mode="research", workspace="/work/repo")
    names = {t["name"] for t in server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )["result"]["tools"]}
    # research_fetch (`ozagia.research.fallback`) is in every mode.
    assert names == {"research_ping", "research_fetch"}
    response = server.handle(_call("research_ping", {"value": "x"}))
    assert response["result"]["isError"] is False
    assert calls == [("research_ping", {"value": "x"}, "/work/repo")]


def test_mode_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="coding, research, combined"):
        DevMemoryServer(None, mode="unknown")


def test_cli_research_mode_skips_dev_memory_build(monkeypatch, tmp_path) -> None:
    from ozagia.integrations.mcp import server as server_module

    module = types.ModuleType("ozagia.research.mcp")
    module.research_tools = lambda: []
    module.call_research_tool = lambda *_args: None
    monkeypatch.setitem(sys.modules, "ozagia.research.mcp", module)
    monkeypatch.setattr(server_module, "serve", lambda instance: (
        0 if instance.mode == "research" and instance.mem is None else 1))
    monkeypatch.setattr(server_module, "_load_memory",
                        lambda _args: pytest.fail("research mode loaded memory"))

    assert server_module.main(["--root", str(tmp_path), "--mode", "research"]) == 0


def test_cli_state_defaults_follow_root_and_state_dir(monkeypatch, tmp_path) -> None:
    from ozagia.integrations.mcp import server as server_module

    captured = []
    monkeypatch.setattr(server_module, "_load_memory", lambda args: (
        captured.append((args.dump, args.journal)) or object()))
    monkeypatch.setattr(server_module, "DevMemoryServer",
                        lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server_module, "serve", lambda _server: 0)
    monkeypatch.setattr(server_module, "_close_lsp", lambda: None)
    monkeypatch.setattr(server_module.signal, "signal", lambda *_args: None)

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("OZAGIA_STATE_DIR", "custom-state")
    monkeypatch.delenv("OZAGIA_DUMP", raising=False)
    monkeypatch.delenv("OZAGIA_JOURNAL", raising=False)
    assert server_module.main(["--root", str(root)]) == 0
    assert captured[-1] == (
        str(root / "custom-state" / "graph.json"),
        str(root / "custom-state" / "journal.jsonl"),
    )

    env_dump, env_journal = tmp_path / "env-graph.json", tmp_path / "env-journal.jsonl"
    monkeypatch.setenv("OZAGIA_DUMP", str(env_dump))
    monkeypatch.setenv("OZAGIA_JOURNAL", str(env_journal))
    assert server_module.main(["--root", str(root)]) == 0
    assert captured[-1] == (str(env_dump), str(env_journal))

    cli_dump, cli_journal = tmp_path / "cli-graph.json", tmp_path / "cli-journal.jsonl"
    assert server_module.main([
        "--root", str(root), "--dump", str(cli_dump),
        "--journal", str(cli_journal),
    ]) == 0
    assert captured[-1] == (str(cli_dump), str(cli_journal))


def test_research_dev_context_is_combined_only_and_keeps_results_independent(
        monkeypatch, server) -> None:
    from ozagia.integrations.mcp import server as server_module

    calls = []
    research_context_spec = {
        "name": "research_context", "description": "research context",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    }
    module = types.ModuleType("ozagia.research.mcp")
    module.research_tools = lambda: [research_context_spec]

    def call_research_tool(name, args, workspace):
        calls.append((name, args, workspace))
        if args["session_id"] == "invalid":
            raise ValueError("investigation not found")
        return {"session_id": args["session_id"], "citations": ["claim-1"]}

    module.call_research_tool = call_research_tool
    monkeypatch.setitem(sys.modules, "ozagia.research.mcp", module)
    coding_calls = []
    monkeypatch.setattr(server.mem, "context", lambda task: (
        coding_calls.append(task) or {"task": task, "citations": ["file.py"]}))

    research_only = server_module.DevMemoryServer(None, mode="research",
                                                   workspace=server.mem.raw_repo_root)
    research_names = {tool["name"] for tool in research_only._tools}
    assert "research_dev_context" not in research_names
    assert not any(name.startswith("dev_") for name in research_names)

    combined = server_module.DevMemoryServer(server.mem, mode="combined",
                                              workspace=server.mem.raw_repo_root)
    combined_names = {tool["name"] for tool in combined._tools}
    assert "research_dev_context" in combined_names
    response = combined.handle(_call("research_dev_context", {
        "task": "inspect persistence", "session_id": "session-7"}))
    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["coding_context"] == {
        "task": "inspect persistence", "citations": ["file.py"]}
    assert payload["research_context"] == {
        "session_id": "session-7", "citations": ["claim-1"]}
    assert payload["relationship"]["independent_retrievals"] is True
    assert payload["relationship"]["linked"] is False
    assert calls == [("research_context", {
        "session_id": "session-7", "query": "inspect persistence"},
        server.mem.raw_repo_root)]
    assert coding_calls == ["inspect persistence"]

    invalid = combined.handle(_call("research_dev_context", {
        "task": "inspect persistence", "session_id": "invalid"}))
    assert invalid["result"]["isError"] is True
    assert "investigation not found" in invalid["result"]["content"][0]["text"]
    # Session validation happens before querying coding context.
    assert coding_calls == ["inspect persistence"]


def test_missing_required_argument_is_invalid_params(server) -> None:
    resp = server.handle(_call("dev_seed", {}))
    assert resp["error"]["code"] == -32602
    assert "seed_id" in resp["error"]["message"]


def test_string_booleans_and_integers_are_coerced(server) -> None:
    resp = server.handle(_call("dev_search", {"text": "add", "limit": "1"}))
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert len(payload["results"]) == 1
    changed = server.handle(_call("dev_changed", {"all": "TRUE"}))
    assert changed["result"]["isError"] is False


def test_dev_edit_apply_false_string_is_preview(server) -> None:
    target = server.mem.raw_repo_root + "/domains/foo.py"
    before = _sha(Path(target))
    resp = server.handle(_call("dev_edit", {**ADD_EDIT, "apply": "false"}))
    assert resp["result"]["isError"] is False
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["applied"] is False
    assert _sha(Path(target)) == before


def test_dev_edit_apply_maybe_is_invalid_params(server) -> None:
    target = Path(server.mem.raw_repo_root) / "domains" / "foo.py"
    before = _sha(target)
    resp = server.handle(_call("dev_edit", {**ADD_EDIT, "apply": "maybe"}))
    assert resp["error"]["code"] == -32602
    assert "apply" in resp["error"]["message"]
    assert _sha(target) == before


def test_arguments_must_be_an_object(server) -> None:
    resp = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "dev_stats", "arguments": [1]}})
    assert resp["error"]["code"] == -32602


def test_initialize_negotiates_protocol_version(server) -> None:
    echoed = server.handle({"jsonrpc": "2.0", "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26"}})
    assert echoed["result"]["protocolVersion"] == "2025-03-26"
    newest = server.handle({"jsonrpc": "2.0", "id": 2,
                            "method": "initialize",
                            "params": {"protocolVersion": "1999-01-01"}})
    assert newest["result"]["protocolVersion"] == "2025-06-18"


def _framed_replies(raw: bytes) -> list[Any]:
    """Decode a stream that may mix newline and Content-Length replies."""
    out: list[Any] = []
    stream = io.BytesIO(raw)
    while True:
        line = stream.readline()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
            assert stream.readline() in (b"\r\n", b"\n")
            out.append(json.loads(stream.read(length).decode("utf-8")))
        elif line.strip():
            out.append(json.loads(line))
    return out


def _serve_framed(monkeypatch, server, payload: bytes) -> list[Any]:
    stdout = io.BytesIO()
    monkeypatch.setattr(sys, "stdin",
                        SimpleNamespace(buffer=io.BytesIO(payload)))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=stdout))
    assert serve(server) == 0
    return _framed_replies(stdout.getvalue())


@pytest.mark.parametrize("length", [-1, 64 * 1024 * 1024 + 1])
def test_out_of_range_content_length_is_parse_error_then_continues(
        monkeypatch, server, length: int) -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}).encode()
    payload = (f"Content-Length: {length}\r\n\r\n".encode()
               + body + b"\n" + PING)
    replies = _serve_framed(monkeypatch, server, payload)
    assert replies[0]["error"]["code"] == -32700
    assert replies[0]["id"] is None
    assert replies[-1]["id"] == 5 and "result" in replies[-1]


def test_non_integer_content_length_is_parse_error(monkeypatch, server) -> None:
    replies = _serve_framed(monkeypatch, server,
                            b"Content-Length: nope\r\n\r\n")
    assert replies[0]["error"]["code"] == -32700


def test_dev_edit_diagnostics_defaults_to_off(server) -> None:
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    prop = tools["dev_edit"]["inputSchema"]["properties"]["diagnostics"]
    assert prop["default"] is False
    assert "diagnostics=true" in prop["description"]


def test_dev_files_tools_read_the_ledger(server, tmp_path: Path) -> None:
    from ozagia.core import files as F
    (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
    F.record_file(tmp_path, "notes.md", session="ses-mcp", harness="mcp",
                  tool="write", reason="one-off note")
    reply = server.handle(_call("dev_files_list", {}))
    payload = json.loads(reply["result"]["content"][0]["text"])
    assert payload["ok"] and payload["count"] == 1
    assert payload["files"][0]["path"] == "notes.md"
    reply = server.handle(_call("dev_files_explain", {"path": "notes.md"}))
    payload = json.loads(reply["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["record"]["reason"] == "one-off note"
    assert payload["route"]["kind"] == "note"
    reply = server.handle(_call("dev_files_explain", {"path": "../outside.md"}))
    payload = json.loads(reply["result"]["content"][0]["text"])
    assert payload["ok"] is False
