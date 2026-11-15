"""The native agent loop (ozagia.harness.native) against a local stub server.

No model and no network endpoint is called: `StubLLM` is an OpenAI-compatible
server on 127.0.0.1 that answers from a scripted queue and records requests.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from ozagia.core import memory as M
from ozagia.core.memory import DevMemory, build_index
from ozagia.harness import native
from ozagia.harness import tools as T

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


# --------------------------------------------------------------------------- #
# the stub server
# --------------------------------------------------------------------------- #
def tool_call(name: str, args: Any, call_id: str | None = None) -> dict:
    """One tool call; `args` a dict (sent as JSON) or a raw string."""
    return {"id": call_id or f"call_{name}", "type": "function",
            "function": {"name": name,
                         "arguments": args if isinstance(args, str)
                         else json.dumps(args)}}


def reply(*calls: dict, content: str | None = None, usage: dict | None = None,
          **extra: Any) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = list(calls)
    doc = {"id": "cmpl", "object": "chat.completion",
           "choices": [{"index": 0, "message": msg,
                        "finish_reason": "tool_calls" if calls else "stop"}],
           "usage": usage or {"prompt_tokens": 10, "completion_tokens": 2}}
    doc.update(extra)
    return doc


def final(text: str = "Fixed.", **kw: Any) -> dict:
    return reply(content=text, **kw)


def status(code: int, body: str = "busy") -> dict:
    return {"_status": code, "_body": body}


def sleep(seconds: float, then: dict | None = None) -> dict:
    return {"_sleep": seconds, "_then": then or final()}


class StubLLM:
    """A scripted OpenAI-compatible server. `responses` is consumed in order
    by POST /v1/chat/completions; when it is empty the reply is `final()`.
    GET /v1/models lists `models`."""

    def __init__(self, responses: list[dict] | None = None,
                 models: tuple[str, ...] = ("stub-model",)) -> None:
        self.queue = list(responses or [])
        self.models = models
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, code: int, doc: Any) -> None:
                data = (doc if isinstance(doc, str) else json.dumps(doc)).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def _record(self, body: Any) -> None:
                with stub.lock:
                    stub.requests.append({"method": self.command,
                                          "path": self.path,
                                          "headers": dict(self.headers),
                                          "body": body})

            def do_GET(self) -> None:
                self._record(None)
                if self.path.rstrip("/").endswith("/models"):
                    self._send(200, {"object": "list", "data": [
                        {"id": m, "object": "model"} for m in stub.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null")
                self._record(body)
                with stub.lock:
                    item = stub.queue.pop(0) if stub.queue else final()
                if "_sleep" in item:
                    time.sleep(item["_sleep"])
                    item = item["_then"]
                if "_status" in item:
                    self._send(item["_status"], {"error": item["_body"]})
                else:
                    self._send(200, item)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def posts(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]

    def __enter__(self) -> "StubLLM":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------------- #
# fixture repository
# --------------------------------------------------------------------------- #
BROKEN = "def add(a, b):\n    return a - b\n"
TEST_SRC = ("from pkg.mod import add\n\n\n"
            "def test_add():\n    assert add(1, 2) == 3\n")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def layout(monkeypatch):
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ())
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ())
    monkeypatch.setattr(M, "IMPORT_ROOTS", ())
    monkeypatch.setattr(M, "_PATH_RE", M.path_mention_re(["pkg", "tests"]))
    for name in ("OZAGIA_CONFIG", "OZAGIA_ADAPTERS", "OZAGIA_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo(tmp_path: Path, layout) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "mod.py").write_text(BROKEN, encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text(TEST_SRC, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


@pytest.fixture
def mem(repo: Path, tmp_path: Path) -> DevMemory:
    dump = tmp_path / "state" / "graph.json"
    build_index(repo, dump, include_docs="none", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=repo)


PYTEST = f"{sys.executable} -m pytest -q -p no:cacheprovider tests/test_mod.py"


def _fix_script(test_cmd: str = PYTEST) -> list[dict]:
    return [
        reply(tool_call("read", {"path": "pkg/mod.py"}, "c1")),
        reply(tool_call("edit", {"path": "pkg/mod.py",
                                 "oldString": "return a - b",
                                 "newString": "return a + b"}, "c2")),
        reply(tool_call("shell", {"command": test_cmd,
                                  "description": "run tests"}, "c3")),
        final("The sign was wrong; fixed and tests pass."),
    ]


def _run(stub: StubLLM, workdir: Path, log: Path, **kw: Any) -> dict:
    kw.setdefault("mode", "plain")
    return native.run(workdir, "addition gives the wrong sign",
                      base_url=stub.url, model="stub-model", log_dir=log, **kw)


def _steps(log: Path) -> list[dict]:
    return [json.loads(line) for line in
            (log / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]


def _tool_msgs(body: dict) -> list[dict]:
    return [m for m in body["messages"] if m["role"] == "tool"]


# --------------------------------------------------------------------------- #
# plain mode
# --------------------------------------------------------------------------- #
def test_plain_mode_fixes_the_bug_and_ends_on_content(repo, tmp_path):
    log = tmp_path / "log"
    with StubLLM(_fix_script()) as stub:
        s = _run(stub, repo, log)
    assert s["stopped"] == "done" and s["exit_code"] == 0 and s["error"] is None
    assert s["steps"] == 4 and s["tool_calls"] == 3
    assert s["tools_by_name"] == {"edit": 1, "read": 1, "shell": 1}
    assert s["final_text"].startswith("The sign was wrong")
    assert s["captured_calls"] is None and s["fallbacks"] is None
    assert (repo / "pkg" / "mod.py").read_text() == "def add(a, b):\n    return a + b\n"
    posts = stub.posts()
    read_out = _tool_msgs(posts[1]["body"])[0]
    assert read_out["tool_call_id"] == "c1"
    assert read_out["content"] == (f"Read file {repo / 'pkg' / 'mod.py'}, lines 1-2\n"
                                   "1: def add(a, b):\n2:     return a - b")
    assert _tool_msgs(posts[2]["body"])[1]["content"] == "Edit applied successfully."
    shell_out = _tool_msgs(posts[3]["body"])[2]["content"]
    assert "1 passed" in shell_out and "[ozagia]" not in shell_out
    # The assistant turn with its tool calls is sent back before the results.
    assistant = [m for m in posts[1]["body"]["messages"] if m["role"] == "assistant"]
    assert assistant[0]["tool_calls"][0]["id"] == "c1"
    steps = _steps(log)
    assert len(steps) == 4
    assert [t["name"] for t in steps[0]["tools"]] == ["read"]
    rec = steps[2]["tools"][0]
    assert rec["input"]["command"] == PYTEST
    assert rec["output_chars"] == len(shell_out) and rec["ms"] >= 0
    assert json.loads((log / "summary.json").read_text()) == s


def test_tools_schema_and_request_shape(repo, tmp_path):
    with StubLLM([final()]) as stub:
        _run(stub, repo, tmp_path / "log", temperature=0.2)
        _run(stub, repo, tmp_path / "log2", mode="graph", memory=object())
    plain, graph = stub.posts()[0]["body"], stub.posts()[1]["body"]
    assert stub.posts()[0]["path"] == "/v1/chat/completions"
    assert plain["model"] == "stub-model" and plain["tool_choice"] == "auto"
    assert plain["temperature"] == 0.2 and "temperature" not in graph
    names = [t["function"]["name"] for t in plain["tools"]]
    assert names == ["read", "grep", "glob", "edit", "write", "shell"]
    props = {t["function"]["name"]: t["function"]["parameters"]["properties"]
             for t in plain["tools"]}
    assert set(props["read"]) == {"path", "offset", "limit"}
    assert set(props["grep"]) == {"pattern", "path", "include", "caseSensitive"}
    assert set(props["glob"]) == {"pattern", "path"}
    assert set(props["edit"]) == {"path", "oldString", "newString", "replaceAll"}
    assert set(props["write"]) == {"path", "content"}
    assert set(props["shell"]) == {"command", "workdir", "timeout", "description"}
    assert not any("[ozagia]" in t["function"]["description"] for t in plain["tools"])
    assert all("[ozagia]" in t["function"]["description"] for t in graph["tools"])
    assert "node key" in graph["tools"][0]["function"]["description"]
    system = plain["messages"][0]
    assert system["role"] == "system" and str(repo) in system["content"]
    assert "commit" in system["content"] and "test" in system["content"]
    assert plain["messages"][1] == {"role": "user",
                                    "content": "addition gives the wrong sign"}


def test_usage_and_cost_are_summed(repo, tmp_path):
    script = [reply(tool_call("glob", {"pattern": "*.py"}),
                    usage={"prompt_tokens": 100, "completion_tokens": 7,
                           "cost": 0.01}),
              final(usage={"prompt_tokens": 150, "completion_tokens": 3,
                           "cost": 0.02})]
    with StubLLM(script) as stub:
        s = _run(stub, repo, tmp_path / "log")
    assert (s["tokens_in"], s["tokens_out"]) == (250, 10)
    assert s["cost_usd"] == pytest.approx(0.03)
    with StubLLM([final()]) as stub:
        assert _run(stub, repo, tmp_path / "log2")["cost_usd"] is None


def test_invalid_json_arguments_and_unknown_tool_are_tool_errors(repo, tmp_path):
    script = [reply(tool_call("read", "{not json", "bad"),
                    tool_call("browse", {"url": "x"}, "unk"),
                    tool_call("read", {"path": "pkg/mod.py"}, "ok")),
              final()]
    with StubLLM(script) as stub:
        s = _run(stub, repo, tmp_path / "log")
    assert s["stopped"] == "done" and s["tool_calls"] == 3
    msgs = _tool_msgs(stub.posts()[1]["body"])
    assert [m["tool_call_id"] for m in msgs] == ["bad", "unk", "ok"]
    assert msgs[0]["content"].startswith("Error: tool arguments are not valid JSON")
    assert msgs[1]["content"].startswith("Error: unknown tool: browse")
    assert msgs[2]["content"].startswith("Read file ")
    tools = _steps(tmp_path / "log")[0]["tools"]
    assert tools[0]["is_error"] and tools[0]["input"] == "{not json"
    assert tools[1]["is_error"] and not tools[2]["is_error"]


def test_server_errors_are_retried(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(native, "RETRY_BACKOFF_S", 0.01)
    with StubLLM([status(500), status(429), final("ok")]) as stub:
        s = _run(stub, repo, tmp_path / "log")
    assert s["stopped"] == "done" and s["final_text"] == "ok"
    assert len(stub.posts()) == 3


def test_retries_give_up_after_three_attempts(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(native, "RETRY_BACKOFF_S", 0.01)
    with StubLLM([status(503)] * 3 + [final()]) as stub:
        s = _run(stub, repo, tmp_path / "log")
    assert s["stopped"] == "error" and s["exit_code"] == 1
    assert "3 attempts" in s["error"] and "HTTP 503" in s["error"]
    assert len(stub.posts()) == 3


def test_client_error_is_not_retried(repo, tmp_path):
    with StubLLM([status(400, "bad request"), final()]) as stub:
        s = _run(stub, repo, tmp_path / "log")
    assert s["stopped"] == "error" and "HTTP 400" in s["error"]
    assert len(stub.posts()) == 1
    with StubLLM([status(401, "nope")]) as stub:
        s = _run(stub, repo, tmp_path / "log2")
    assert "authentication failed" in s["error"]


def test_max_steps(repo, tmp_path):
    script = [reply(tool_call("glob", {"pattern": "*.py"}, f"c{i}"))
              for i in range(5)]
    with StubLLM(script) as stub:
        s = _run(stub, repo, tmp_path / "log", max_steps=3)
    assert s["stopped"] == "max_steps" and s["exit_code"] == 0
    assert s["steps"] == 3 and s["tool_calls"] == 3
    assert len(stub.posts()) == 3


def test_timeout_while_the_model_is_slow(repo, tmp_path):
    with StubLLM([reply(tool_call("glob", {"pattern": "*.py"})),
                  sleep(5)]) as stub:
        t0 = time.monotonic()
        s = _run(stub, repo, tmp_path / "log", timeout_s=1.0)
        took = time.monotonic() - t0
    assert s["stopped"] == "timeout" and s["exit_code"] == 0
    assert s["steps"] == 1
    assert took < 4


def test_shell_timeout_kills_the_process_group(tmp_path):
    pidfile = tmp_path / "pid"
    t0 = time.monotonic()
    out = T.tool_shell(tmp_path, {
        "command": f"sleep 60 & echo $! > {pidfile}; echo started; wait",
        "timeout": 500})
    assert time.monotonic() - t0 < 10
    assert "started" in out and "timed out after 500 ms" in out
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"background child {pid} survived the timeout")


def test_shell_output_is_truncated_to_the_tail(tmp_path):
    out = T.tool_shell(tmp_path, {"command": "python3 -c \"print('x' * 40000 + 'END')\""})
    assert out.startswith("(output truncated: showing the last 30000 of 40004")
    assert out.rstrip().endswith("END")
    assert len(out) < T.MAX_SHELL_OUTPUT_CHARS + 200
    assert T.tool_shell(tmp_path, {"command": "echo err >&2; exit 3"}) == (
        "err\n\n\n(exit code 3)")


def test_api_key_is_sent_as_bearer_and_never_logged(repo, tmp_path):
    key = "sk-test-SECRET-1234567890"
    keyfile = tmp_path / "key.txt"
    keyfile.write_text(key + "\n", encoding="utf-8")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("addition gives the wrong sign", encoding="utf-8")
    log = tmp_path / "log"
    script = [reply(tool_call("shell", {"command": "env"})), final("done")]
    with StubLLM(script) as stub:
        argv = [sys.executable, "-m", "ozagia.harness.native", "run",
                "--workdir", str(repo), "--prompt-file", str(prompt),
                "--mode", "plain", "--base-url", stub.url, "--model", "stub-model",
                "--api-key-env", "STUB_NATIVE_KEY", "--log-dir", str(log)]
        env = dict(os.environ, PYTHONPATH=str(SRC), STUB_NATIVE_KEY=key)
        proc = subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=120)
        argv2 = argv[:-4] + ["--api-key-file", str(keyfile), "--log-dir",
                             str(tmp_path / "log2")]
        stub.queue.append(final("again"))
        proc2 = subprocess.run(argv2, env=dict(os.environ, PYTHONPATH=str(SRC)),
                               capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc2.returncode == 0, proc2.stderr
    assert key not in " ".join(argv + argv2)
    summary = json.loads(proc.stdout)
    assert summary["stopped"] == "done" and summary["final_text"] == "done"
    assert json.loads(proc2.stdout)["final_text"] == "again"
    for req in stub.posts():
        assert req["headers"]["Authorization"] == f"Bearer {key}"
    # The agent's `env` ran without the key in its environment.
    env_out = _tool_msgs(stub.posts()[1]["body"])[0]["content"]
    assert "PATH=" in env_out and key not in env_out
    assert "STUB_NATIVE_KEY" not in env_out and "PYTHONPATH" not in env_out
    for d in (log, tmp_path / "log2"):
        for name in ("transcript.jsonl", "summary.json"):
            assert key not in (d / name).read_text(encoding="utf-8")
    assert key not in proc.stdout + proc.stderr + proc2.stdout + proc2.stderr


def test_no_key_sends_no_authorization(repo, tmp_path):
    with StubLLM([final()]) as stub:
        _run(stub, repo, tmp_path / "log")
    assert "Authorization" not in stub.posts()[0]["headers"]


def test_list_models_reads_the_stub():
    with StubLLM(models=("a", "b")) as stub:
        assert native.list_models(stub.url, "k") == ["a", "b"]
        assert stub.requests[0]["path"] == "/v1/models"
        assert stub.requests[0]["headers"]["Authorization"] == "Bearer k"


# --------------------------------------------------------------------------- #
# graph mode
# --------------------------------------------------------------------------- #
def test_graph_mode_read_is_handled_with_graph_headers(repo, mem, tmp_path):
    log = tmp_path / "log"
    with StubLLM(_fix_script()) as stub:
        s = _run(stub, repo, log, mode="graph", memory=mem)
    assert s["stopped"] == "done"
    assert (repo / "pkg" / "mod.py").read_text().endswith("return a + b\n")
    read_out = _tool_msgs(stub.posts()[1]["body"])[0]["content"]
    assert read_out.startswith(f"Read file {repo / 'pkg' / 'mod.py'}, lines 1-2")
    assert "[ozagia] file:pkg/mod.py" in read_out
    assert "[ozagia] sym:pkg/mod.py::add" in read_out
    edit_out = _tool_msgs(stub.posts()[2]["body"])[1]["content"]
    assert edit_out.startswith("Edited ") and "[ozagia] delta:" in edit_out
    shell_out = _tool_msgs(stub.posts()[3]["body"])[2]["content"]
    assert "[ozagia] pytest passed" in shell_out
    assert s["captured_calls"] == 3 and s["fallbacks"] == 0
    tools = [t for st in _steps(log) for t in st["tools"]]
    assert all(t["handled"] is True and t["fallback"] is False for t in tools)
    assert tools[0]["intent"] == "read_file"


def test_graph_mode_unhandled_shell_runs_plain_then_after(repo, mem, tmp_path):
    log = tmp_path / "log"
    cmd = "printf 'def extra():\\n    return 1\\n' > pkg/extra.py && echo wrote"
    script = [reply(tool_call("shell", {"command": cmd})),
              reply(tool_call("read", {"path": "sym:pkg/mod.py::add"})),
              final()]
    with StubLLM(script) as stub:
        s = _run(stub, repo, log, mode="graph", memory=mem)
    out = _tool_msgs(stub.posts()[1]["body"])[0]["content"]
    assert out.startswith("wrote\n")
    assert "[ozagia] delta:" in out and "sym:pkg/extra.py::extra" in out
    node = _tool_msgs(stub.posts()[2]["body"])[1]["content"]
    assert "[ozagia] sym:pkg/mod.py::add" in node and "2:     return a - b" in node
    # A command the intent layer leaves to the built-in by design is a
    # passthrough, not a fallback (review fix 4).
    assert s["captured_calls"] == 1 and s["passthrough_calls"] == 1
    assert s["fallbacks"] == 0
    first = _steps(log)[0]["tools"][0]
    assert first["handled"] is False and first["fallback"] is False
    assert first["after_intent"] == "after_refresh"


def test_graph_mode_handled_error_is_a_tool_error(repo, mem, tmp_path):
    script = [reply(tool_call("read", {"path": "pkg/mod.py", "offset": 50})),
              reply(tool_call("shell", {"command": "git commit -m x"})),
              final()]
    with StubLLM(script) as stub:
        s = _run(stub, repo, tmp_path / "log", mode="graph", memory=mem)
    first = _tool_msgs(stub.posts()[1]["body"])[0]["content"]
    assert first.startswith("Error: Offset 50 is out of range")
    refused = _tool_msgs(stub.posts()[2]["body"])[1]["content"]
    assert refused.startswith("Error: Refused by ozagia")
    assert s["captured_calls"] == 2


def test_graph_mode_loads_or_builds_the_dump(repo, tmp_path, monkeypatch):
    calls = []

    def fake_load(workdir, dump):
        calls.append((workdir, dump))
        return "MEM"

    monkeypatch.setattr(native, "load_graph", fake_load)
    with StubLLM([final()]) as stub:
        s = _run(stub, repo, tmp_path / "log", mode="graph",
                 dump=tmp_path / "g.json")
    assert calls == [(repo.resolve(), tmp_path / "g.json")]
    assert s["captured_calls"] == 0 and s["fallbacks"] == 0

    def broken(workdir, dump):
        raise FileNotFoundError("no snapshot")

    monkeypatch.setattr(native, "load_graph", broken)
    with StubLLM([final()]) as stub:
        s = _run(stub, repo, tmp_path / "log2", mode="graph")
        assert stub.posts() == []
    assert s["stopped"] == "error" and "graph load failed" in s["error"]


# --------------------------------------------------------------------------- #
# plain tools
# --------------------------------------------------------------------------- #
def test_plain_read_formats(repo):
    out = T.tool_read(repo, {"path": "pkg/mod.py", "offset": 2, "limit": 5})
    assert out == f"Read file {repo / 'pkg' / 'mod.py'}, lines 2-2 of 2\n2:     return a - b"
    d = T.tool_read(repo, {"path": "pkg"})
    assert d == f"Read directory {repo / 'pkg'}, entries 1-2\n__init__.py\nmod.py"
    with pytest.raises(T.ToolError, match="File not found"):
        T.tool_read(repo, {"path": "nope.py"})
    with pytest.raises(T.ToolError, match="outside the working directory"):
        T.tool_read(repo, {"path": "/etc/passwd"})
    with pytest.raises(T.ToolError, match="out of range"):
        T.tool_read(repo, {"path": "pkg/mod.py", "offset": 9})


def test_plain_grep_and_glob(repo):
    out = T.tool_grep(repo, {"pattern": "def add"})
    assert out.splitlines()[0] == "Found 1 matches"
    assert f"{repo / 'pkg' / 'mod.py'}:" in out and "  Line 1: def add(a, b):" in out
    assert T.tool_grep(repo, {"pattern": "DEF ADD"}) == "No files found"
    assert "Found 1 matches" in T.tool_grep(repo, {"pattern": "DEF ADD",
                                                   "caseSensitive": False})
    assert "test_mod.py" not in T.tool_grep(repo, {"pattern": "add",
                                                   "include": "mod.py"})
    with pytest.raises(T.ToolError):
        T.tool_grep(repo, {"pattern": "("})
    old = time.time() - 100
    os.utime(repo / "pkg" / "__init__.py", (old, old))
    files = T.tool_glob(repo, {"pattern": "*.py", "path": "pkg"}).splitlines()
    assert files == [str(repo / "pkg" / "mod.py"), str(repo / "pkg" / "__init__.py")]
    assert T.tool_glob(repo, {"pattern": "*.rs"}) == "No files found"


def test_plain_edit_and_write(repo):
    p = repo / "pkg" / "dup.py"
    T.tool_write(repo, {"path": "pkg/dup.py", "content": "x = 1\nx = 1\n"})
    assert p.read_text() == "x = 1\nx = 1\n"
    with pytest.raises(T.ToolError, match="multiple matches"):
        T.tool_edit(repo, {"path": "pkg/dup.py", "oldString": "x = 1",
                           "newString": "x = 2"})
    with pytest.raises(T.ToolError, match="not found"):
        T.tool_edit(repo, {"path": "pkg/dup.py", "oldString": "y",
                           "newString": "z"})
    T.tool_edit(repo, {"path": "pkg/dup.py", "oldString": "x = 1",
                       "newString": "x = 2", "replaceAll": True})
    assert p.read_text() == "x = 2\nx = 2\n"
    assert T.tool_write(repo, {"path": "new/deep/f.txt", "content": "hi"}) == (
        "Wrote file successfully.")
    assert (repo / "new" / "deep" / "f.txt").read_text() == "hi"


# --------------------------------------------------------------------------- #
# review fixes: arm parity, counters, environment, snapshot writes
# --------------------------------------------------------------------------- #
def _head(repo: Path) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize("mode", ["plain", "graph"])
def test_both_modes_refuse_ref_moving_git(repo, mem, tmp_path, mode):
    head = _head(repo)
    cmd = "git -c user.name=t -c user.email=t@t commit --allow-empty -m x"
    with StubLLM([reply(tool_call("shell", {"command": cmd})), final()]) as stub:
        _run(stub, repo, tmp_path / "log", mode=mode, memory=mem)
    out = _tool_msgs(stub.posts()[1]["body"])[0]["content"]
    assert out == f"Error: Refused by ozagia: {cmd} changes git refs"
    assert _head(repo) == head


class _Captured(Exception):
    pass


def _capture_run_tests(monkeypatch) -> list[dict]:
    from ozagia import check
    seen: list[dict] = []

    def fake(root, selectors, **kw):
        seen.append(kw)
        raise _Captured
    monkeypatch.setattr(check, "run_tests", fake)
    return seen


def test_graph_pytest_gets_the_plain_shell_timeout_env_and_deadline(
        repo, mem, monkeypatch):
    seen = _capture_run_tests(monkeypatch)
    shell_env = {"PATH": os.environ["PATH"], "ONLY": "this"}
    cmd = {"command": PYTEST}
    ex = native._Executor(repo, "graph", mem, shell_env, time.monotonic() + 3600)
    ex.call("shell", dict(cmd))
    assert seen[-1]["timeout_s"] == T.DEFAULT_SHELL_TIMEOUT_MS / 1000
    assert seen[-1]["env"] == shell_env and seen[-1]["inherit"] is False
    ex = native._Executor(repo, "graph", mem, shell_env, time.monotonic() + 5)
    ex.call("shell", dict(cmd, timeout=600_000))
    assert 4 < seen[-1]["timeout_s"] <= 5
    ex = native._Executor(repo, "graph", mem, shell_env, time.monotonic() - 1)
    ex.call("shell", dict(cmd))
    assert seen[-1]["timeout_s"] == 0.1
    # run_tests raised inside the intent layer: a fallback, not a passthrough.
    assert ex.fallbacks == 1 and ex.passthrough == 0


def test_shell_env_drops_native_settings_and_the_key(monkeypatch):
    monkeypatch.setenv("OZAGIA_NATIVE_API_KEY_FILE", "/secret/key")
    monkeypatch.setenv("OZAGIA_NATIVE_MODEL", "m")
    monkeypatch.setenv("MY_KEY", "sk-123")
    monkeypatch.setenv("PYTHONPATH", "/ozagia/src")
    env = native._shell_env("sk-123")
    assert not any(k.startswith("OZAGIA_NATIVE_") for k in env)
    assert "MY_KEY" not in env and "PYTHONPATH" not in env
    assert env["PATH"] == os.environ["PATH"]


@pytest.mark.parametrize("url,warns", [
    ("http://10.0.0.5:8000/v1", True), ("http://example.com/v1", True),
    ("http://127.0.0.1:8000/v1", False), ("http://localhost:1/v1", False),
    ("http://[::1]:8000/v1", False), ("https://example.com/v1", False),
])
def test_plain_http_to_another_host_warns(url, warns):
    assert (native._plain_http_warning(url) is not None) is warns


def test_graph_mode_writes_the_snapshot_once_at_the_end(repo, mem, tmp_path,
                                                        monkeypatch):
    writes: list[int] = []
    real = mem.write_snapshot
    monkeypatch.setattr(mem, "write_snapshot", lambda: writes.append(1) or real())
    with StubLLM(_fix_script()) as stub:
        s = _run(stub, repo, tmp_path / "log", mode="graph", memory=mem)
    assert s["stopped"] == "done" and mem.defer_writes is True
    assert writes == [1] and mem.snapshot_dirty is False
    assert "snapshot_write_s" in s


def test_plain_edit_and_write_refuse_git(repo):
    head = (repo / ".git" / "HEAD").read_text()
    for call in (lambda: T.tool_write(repo, {"path": ".git/HEAD", "content": "x"}),
                 lambda: T.tool_write(repo, {"path": "a/.git/refs/x", "content": "x"}),
                 lambda: T.tool_edit(repo, {"path": str(repo / ".git" / "HEAD"),
                                            "oldString": "ref", "newString": "fer"})):
        with pytest.raises(T.ToolError, match="inside .git"):
            call()
    assert (repo / ".git" / "HEAD").read_text() == head
    assert not (repo / "a").exists()
