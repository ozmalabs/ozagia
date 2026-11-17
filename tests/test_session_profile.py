from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import pty
import select
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from ozagia.config import state_path
from ozagia.daemon.server import JOURNAL_NAME
from ozagia.session_profile_cli import PROFILE_LOCK_TIMEOUT_S, report, store_daemon_event, store_profile, store_profile_event
from ozagia.daemon.server import DaemonState
from ozagia.integrations.opencode import install as opencode_install


def test_session_profile_aggregates_only_metadata_and_marks_unknowns(tmp_path):
    journal = state_path(tmp_path, JOURNAL_NAME)
    journal.parent.mkdir(parents=True)
    journal.write_text("\n".join([
        json.dumps({"ts": "2026-11-17T10:00:00", "op": "tool", "tool": "read",
                    "session": "s1", "harness": "opencode", "handled": True,
                    "is_error": False, "ms": 12.5, "keys": ["path"], "files": ["secret.py"]}),
        json.dumps({"ts": "2026-11-17T10:00:01", "op": "tool", "tool": "bash",
                    "session": "s1", "harness": "opencode", "handled": False,
                    "is_error": False, "ms": 4, "reason": "sensitive output"}),
        json.dumps({"ts": "2026-11-17T10:00:02", "op": "after", "tool": "bash",
                    "session": "s1", "harness": "opencode", "fallback": "timeout",
                    "ms": 2}),
        json.dumps({"ts": "2026-11-17T10:00:03", "op": "tool", "tool": "write",
                    "session": "s2", "harness": "opencode", "handled": True,
                    "is_error": True, "ms": 8}),
        json.dumps({"op": "tool", "tool": "read", "handled": True, "ms": 3}),
    ]) + "\n")

    result = report(tmp_path, now=datetime(2026, 11, 17, tzinfo=timezone.utc))

    first, second = sorted(result["sessions"], key=lambda item: item["session_id"])
    assert (first["session_id"], first["handled"], first["passthrough"],
            first["plugin_fallbacks"]) == ("s1", 1, 1, 1)
    assert first["duration_ms"] == {"count": 3, "sum": 18.5, "max": 12.5}
    assert second["errors"] == 1
    assert "secret.py" not in json.dumps(result)
    assert "sensitive output" not in json.dumps(result)
    assert "otherwise unknown" in result["coverage"]["model"]
    assert result["coverage"]["critical_path"].startswith("unavailable:")


def test_session_profile_handles_missing_journal(tmp_path):
    result = report(tmp_path)
    assert result["journal"] == "absent"
    assert result["sessions"] == []
    assert result["ok"] is True


def test_host_profile_persists_without_daemon_journal_and_discards_content(tmp_path):
    store_profile(tmp_path, {
        "session_id": "s-host", "harness": "opencode",
        "metadata": {
            "model_calls": 2, "models": ["provider/model"],
            "tokens": {"input": 20, "output": 7, "reasoning": 3},
            "timing_ms": {"first_token": 100, "model_turn": 800},
            "tools": {"read": {"completed": 2}},
            "prompt": "must not be persisted", "path": "/private/source.py",
        },
    })
    result = report(tmp_path)
    assert result["journal"] == "absent"
    assert len(result["sessions"]) == 1
    host = result["sessions"][0]["host_profile"]
    assert host["tokens"]["input"] == 20
    assert host["timing_ms"]["model_turn"] == 800
    assert host["tools"]["read"]["completed"]["count"] == 2
    serialized = json.dumps(result)
    assert "must not be persisted" not in serialized
    assert "/private/source.py" not in serialized


def test_unreadable_journal_still_returns_host_profile(tmp_path):
    store_profile(tmp_path, {"session_id": "s1", "metadata": {"model_calls": 1, "models": [], "tokens": {}, "timing_ms": {}, "tools": {}}})
    journal = state_path(tmp_path, JOURNAL_NAME)
    journal.mkdir(parents=True)
    result = report(tmp_path)
    assert result["journal"] == "unreadable"
    assert result["sessions"][0]["session_id"] == "s1"
    assert result["sessions"][0]["tool_calls_observed"] == 0


def test_host_and_daemon_events_survive_without_journal_and_do_not_overwrite(tmp_path):
    store_daemon_event(tmp_path, {
        "op": "tool", "session": "s-durable", "harness": "opencode",
        "tool": "read", "handled": True, "is_error": False, "ms": 9,
        "ts": "2026-11-17T10:00:00Z",
    })
    event = {
        "session_id": "s-durable", "harness": "opencode", "kind": "message",
        "event_id": "msg-1", "metadata": {
            "model": "provider/model", "tokens": {"input": 12, "output": 4},
            "timing_ms": {"model_turn": 300},
        },
    }
    assert store_profile_event(tmp_path, event)["stored"] is True
    assert store_profile_event(tmp_path, event)["duplicate"] is True
    tool = {
        "session_id": "s-durable", "harness": "opencode", "kind": "tool",
        "event_id": "part-1", "metadata": {
            "tool": "read", "status": "completed", "duration_ms": 55,
        },
    }
    store_profile_event(tmp_path, tool)
    result = report(tmp_path)
    assert result["journal"] == "absent"
    session = result["sessions"][0]
    assert session["daemon_profile"]["handled"] == 1
    assert session["host_profile"]["model_calls"] == 1
    assert session["host_profile"]["tokens"]["input"] == 12
    assert session["host_profile"]["tools"]["read"]["completed"]["duration_ms_sum"] == 55


def test_plugin_event_payload_dispatches_through_daemon_profile_operation(tmp_path):
    state = object.__new__(DaemonState)
    state.root = tmp_path
    state.owners = SimpleNamespace(touch=lambda *_: None)
    message = {
        "op": "profile_event", "harness": "opencode", "session": "ses-from-plugin",
        "kind": "message", "event_id": "msg-from-plugin",
        "metadata": {"model": "provider/model", "tokens": {"input": 4}, "timing_ms": {"model_turn": 123}},
    }
    response = state.dispatch(message)
    assert response == {"ok": True, "stored": True}
    profile = report(tmp_path)["sessions"][0]["host_profile"]
    assert profile["model_calls"] == 1
    assert profile["tokens"]["input"] == 4


def _write_concurrent_profile_event(args):
    root, index = args
    from pathlib import Path
    from ozagia.session_profile_cli import store_profile_event
    return store_profile_event(Path(root), {
        "session_id": "shared-session", "harness": "opencode", "kind": "message",
        "event_id": f"parallel-{index}",
        "metadata": {"tokens": {"input": 1}, "timing_ms": {"model_turn": 1}},
    })


def test_concurrent_profile_updates_from_separate_processes_are_not_lost(tmp_path):
    context = multiprocessing.get_context("fork")
    with context.Pool(2) as pool:
        pool.map(_write_concurrent_profile_event, [(str(tmp_path), i) for i in range(32)])
    host = report(tmp_path)["sessions"][0]["host_profile"]
    assert host["model_calls"] == 32
    assert host["tokens"]["input"] == 32


def test_profile_lock_contention_times_out_and_dispatch_reports_dropped_event(tmp_path):
    import pytest

    directory = state_path(tmp_path, "session_profiles")
    directory.mkdir(parents=True)
    state = object.__new__(DaemonState)
    state.root = tmp_path
    state.owners = SimpleNamespace(touch=lambda *_: None)
    event = {
        "op": "profile_event", "harness": "opencode", "session": "locked",
        "kind": "message", "event_id": "msg-lock", "metadata": {},
    }
    with (directory / ".write.lock").open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="timed out"):
                store_profile_event(tmp_path, event)
            response = state.dispatch(event)
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    elapsed = time.monotonic() - started
    assert elapsed < PROFILE_LOCK_TIMEOUT_S * 4
    assert response["ok"] is False
    assert "event was dropped" in response["error"]


def test_profile_thread_lock_contention_is_bounded(tmp_path):
    import threading
    import pytest
    from ozagia import session_profile_cli as profile

    acquired = threading.Event()
    release = threading.Event()

    def hold_lock():
        with profile._PROFILE_LOCK:
            acquired.set()
            release.wait(2)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    assert acquired.wait(1)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            store_profile_event(tmp_path, {
                "session": "s1", "harness": "opencode", "kind": "message",
                "event_id": "m1", "metadata": {},
            })
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        thread.join(timeout=1)


def test_profile_cli_filters_by_session(tmp_path, capsys):
    from ozagia.session_profile_cli import main

    for session in ("one", "two"):
        store_profile_event(tmp_path, {
            "session": session, "harness": "opencode", "kind": "message",
            "event_id": "m1", "metadata": {},
        })
    assert main(["--root", str(tmp_path), "--session", "two"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert [item["session_id"] for item in output["sessions"]] == ["two"]


def test_daemon_only_profiles_obey_retention_limit(tmp_path, monkeypatch):
    from ozagia import session_profile_cli as profile

    monkeypatch.setattr(profile, "MAX_ROWS", 2)
    for session in ("one", "two", "three"):
        store_daemon_event(tmp_path, {
            "op": "tool", "session": session, "harness": "opencode",
            "tool": "read", "ok": True, "handled": True,
        })
    assert len(list(state_path(tmp_path, "session_profiles").glob("*.json"))) == 2


def test_persisted_daemon_counters_cover_truncated_journal_window(tmp_path):
    journal = state_path(tmp_path, JOURNAL_NAME)
    journal.parent.mkdir(parents=True)
    lines = []
    for index in range(850):
        row = {
            "ts": f"2026-11-17T10:{index // 60:02d}:{index % 60:02d}Z",
            "op": "tool", "tool": "read", "session": "long-run",
            "harness": "opencode", "handled": True, "is_error": False, "ms": 1,
        }
        lines.append(json.dumps(row))
        store_daemon_event(tmp_path, row)
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = report(tmp_path, journal=journal, journal_source="explicit")
    session = result["sessions"][0]
    assert session["activity_source"] == "persisted_daemon_events"
    assert session["handled"] == 850
    assert session["tool_calls_observed"] == 850
    assert result["truncated"] is True


def test_failed_unknown_and_legacy_tool_outcomes_are_not_misreported(tmp_path):
    records = [
        {"op": "tool", "session": "outcomes", "harness": "opencode", "tool": "read",
         "ok": True, "handled": False},
        {"op": "tool", "session": "outcomes", "harness": "opencode", "tool": "edit",
         "ok": False, "handled": False},
        {"op": "tool", "session": "outcomes", "harness": "opencode", "tool": "shell",
         "ok": True, "handled": None},
        # Older journal records did not contain `ok`; an explicit handled=False
        # remains a passthrough for backwards compatibility.
        {"op": "tool", "session": "outcomes", "harness": "opencode", "tool": "grep",
         "handled": False},
    ]
    for record in records:
        store_daemon_event(tmp_path, record)
    session = report(tmp_path)["sessions"][0]
    assert session["handled"] == 0
    assert session["passthrough"] == 2
    assert session["failures"] == 1
    assert session["unknown"] == 1
    assert session["errors"] == 1
    assert session["tool_calls_observed"] == 4


def test_journal_outcome_counts_keep_failures_and_unknown_separate(tmp_path):
    journal = state_path(tmp_path, JOURNAL_NAME)
    journal.parent.mkdir(parents=True)
    journal.write_text("\n".join(json.dumps(record) for record in [
        {"op": "tool", "session": "journal-outcomes", "harness": "opencode",
         "tool": "read", "ok": False, "handled": False},
        {"op": "tool", "session": "journal-outcomes", "harness": "opencode",
         "tool": "edit", "ok": True, "handled": None},
        {"op": "tool", "session": "journal-outcomes", "harness": "opencode",
         "tool": "grep", "handled": False},
    ]) + "\n", encoding="utf-8")
    session = report(tmp_path, journal=journal)["sessions"][0]
    assert session["passthrough"] == 1
    assert session["failures"] == 1
    assert session["unknown"] == 1
    assert session["errors"] == 1


def test_legacy_persisted_passthrough_counts_migrate_to_unknown(tmp_path):
    store_profile(tmp_path, {
        "session_id": "legacy-counts", "metadata": {
            "model_calls": 0, "models": [], "tokens": {}, "timing_ms": {}, "tools": {},
        },
    })
    profile_path = next(state_path(tmp_path, "session_profiles").glob("*.json"))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["daemon_profile"] = {
        "source": "daemon_events", "operations": 3, "tool_calls_observed": 3,
        "handled": 0, "passthrough": 3, "expired": 0, "errors": 0,
        "plugin_fallbacks": 0, "tools_by_name": {},
        "duration_ms": {"count": 0, "sum": 0, "max": None},
    }
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    before_write = report(tmp_path)["sessions"][0]["daemon_profile"]
    assert before_write["passthrough"] == 0
    assert before_write["unknown"] == 3
    assert before_write["outcome_counts_incomplete"] is True
    store_daemon_event(tmp_path, {
        "op": "tool", "session": "legacy-counts", "harness": "opencode",
        "tool": "read", "ok": True, "handled": False,
    })
    daemon = report(tmp_path)["sessions"][0]["daemon_profile"]
    assert daemon["passthrough"] == 1
    assert daemon["unknown"] == 3
    assert daemon["outcome_counts_incomplete"] is True


def test_opencode_sdk_event_payloads_extract_session_and_metadata(tmp_path):
    node = shutil.which("node")
    if not node:
        return
    source = Path(__file__).parents[1] / "src/ozagia/integrations/opencode/plugin.js"
    plugin = tmp_path / "plugin.mjs"
    plugin.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    script = textwrap.dedent(f"""\
        import {{ profileMetadataEvent }} from {json.dumps(plugin.as_uri())};
        const message = profileMetadataEvent({{
          type: "message.updated",
          properties: {{info: {{id: "msg-sdk", sessionID: "ses-sdk", role: "assistant",
            modelID: "model-v4", providerID: "provider", time: {{created: 100, completed: 900}},
            tokens: {{input: 12, output: 5, reasoning: 2, cache: {{read: 3, write: 1}}}},
            system: ["must not be copied"], path: {{cwd: "/private"}}}}}},
        }});
        const tool = profileMetadataEvent({{
          type: "message.part.updated",
          properties: {{part: {{id: "part-sdk", sessionID: "ses-sdk", type: "tool",
            tool: "bash", state: {{status: "completed", input: {{secret: "no"}}, output: "no",
              time: {{start: 200, end: 350}}}}}}}},
        }});
        console.log(JSON.stringify([message, tool]));
    """)
    completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
    message, tool = json.loads(completed.stdout)
    assert message["session"] == "ses-sdk"
    assert message["metadata"]["model"] == "provider/model-v4"
    assert message["metadata"]["timing_ms"]["model_turn"] == 800
    assert tool["session"] == "ses-sdk"
    assert tool["metadata"]["duration_ms"] == 150
    serialized = json.dumps([message, tool])
    assert "must not be copied" not in serialized
    assert "/private" not in serialized
    assert '"secret"' not in serialized
    state = object.__new__(DaemonState)
    state.root = tmp_path
    state.owners = SimpleNamespace(touch=lambda *_: None)
    assert state.dispatch(message) == {"ok": True, "stored": True}
    assert state.dispatch(tool) == {"ok": True, "stored": True}
    persisted = report(tmp_path)["sessions"][0]["host_profile"]
    assert persisted["model_calls"] == 1
    assert persisted["tools"]["bash"]["completed"]["duration_ms_sum"] == 150


def test_opencode_23_durable_event_payloads_extract_metadata_only(tmp_path):
    node = shutil.which("node")
    if not node:
        return
    source = Path(__file__).parents[1] / "src/ozagia/integrations/opencode/plugin.js"
    plugin = tmp_path / "plugin.mjs"
    plugin.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    script = textwrap.dedent(f"""\
        import {{ profileMetadataEvent }} from {json.dumps(plugin.as_uri())};
        const message = profileMetadataEvent({{
          type: "session.step.ended", created: 900,
          data: {{sessionID: "ses-23", assistantMessageID: "msg-23",
            tokens: {{input: 12, output: 5, reasoning: 2, cache: {{read: 3, write: 1}}}}}},
        }});
        const tool = profileMetadataEvent({{
          type: "session.message.content.updated",
          data: {{sessionID: "ses-23", messageID: "msg-23", content: [{{
            type: "tool", id: "call-23", name: "bash", time: {{created: 200, ran: 220, completed: 350}},
            state: {{status: "completed", input: {{secret: "no"}}, content: [{{text: "no"}}]}}
          }}]}}
        }});
        console.log(JSON.stringify([message, tool]));
    """)
    completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                               text=True, check=True)
    message, tool = json.loads(completed.stdout)
    assert message["metadata"]["tokens"]["input"] == 12
    # Step duration is correlated from the separate started/ended events.
    assert message["metadata"]["timing_ms"]["model_turn"] is None
    assert tool["metadata"] == {"tool": "bash", "status": "completed", "duration_ms": 130}
    serialized = json.dumps([message, tool])
    assert "secret" not in serialized and '"no"' not in serialized


def test_opencode_23_subscribes_to_supported_durable_event_types(tmp_path):
    node = shutil.which("node")
    if not node:
        return
    source = Path(__file__).parents[1] / "src/ozagia/integrations/opencode/plugin.js"
    plugin = tmp_path / "plugin.mjs"
    plugin.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    script = textwrap.dedent(f"""\
        import {{ createPlugin }} from {json.dumps(plugin.as_uri())};
        process.env.OZAGIA_DAEMON_SOCK = "/no/daemon.sock";
        let types;
        const events = {{ subscribe(options) {{ types = options.types; return (async function*() {{}})(); }} }};
        await createPlugin().setup({{ event: events }});
        await new Promise((resolve) => setImmediate(resolve));
        console.log(JSON.stringify(types));
    """)
    completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                               text=True, check=True, timeout=10)
    types = json.loads(completed.stdout)
    assert "session.step.ended" in types
    assert "session.message.content.updated" in types
    assert "message.updated" not in types
    assert "message.part.updated" not in types


def test_opencode_23_event_subscription_persists_correlated_metadata_and_deduplicates(tmp_path):
    node = shutil.which("node")
    if not node:
        return
    source = Path(__file__).parents[1] / "src/ozagia/integrations/opencode/plugin.js"
    plugin = tmp_path / "plugin.mjs"
    plugin.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    sockdir = Path(tempfile.mkdtemp(prefix="ozsess-"))
    sock = sockdir / "daemon.sock"
    responses = []
    errors = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                request = json.loads(self.rfile.readline())
                if request.get("op") == "profile_event":
                    response = store_profile_event(tmp_path, request)
                    responses.append(response)
                else:
                    response = {"ok": True}
                self.wfile.write((json.dumps({"id": request["id"], **response}) + "\n").encode())
            except Exception as exc:  # surface fixture protocol failures in the test
                errors.append(repr(exc))

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        allow_reuse_address = True

    server = Server(str(sock), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        script = textwrap.dedent(f"""\
            import {{ createPlugin }} from {json.dumps(plugin.as_uri())};
            process.env.OZAGIA_DAEMON_SOCK = {json.dumps(str(sock))};
            const sessionID = "ses-opencode-23";
            const events = [
              {{type: "session.step.started", created: 1000, data: {{sessionID,
                assistantMessageID: "msg-opencode-23", model: {{providerID: "vendor", id: "model-x"}}, started: 1000}}}},
              {{type: "session.step.first-token", created: 1100, data: {{sessionID,
                assistantMessageID: "msg-opencode-23"}}}},
              {{type: "session.step.streamed", created: 1200, data: {{sessionID,
                assistantMessageID: "msg-opencode-23"}}}},
              // Actual 2.3 SessionStepEnded has no `started` field.
              {{type: "session.step.ended", created: 1500, data: {{sessionID,
                assistantMessageID: "msg-opencode-23", finish: "stop", cost: 0.01,
                tokens: {{input: 21, output: 8, reasoning: 3, cache: {{read: 4, write: 2}}}}}}}},
              {{type: "session.message.content.updated", created: 1600, data: {{sessionID,
                messageID: "msg-opencode-23", content: [
                  {{type: "tool", id: "call-bash", name: "bash", time: {{created: 1200, ran: 1220, completed: 1320}},
                    state: {{status: "completed", input: {{secret: "must-not-persist"}}, content: [{{type: "text", text: "private-output"}}]}}}},
                  {{type: "tool", id: "call-edit", name: "edit", time: {{created: 1390, ran: 1400, completed: 1450}},
                    state: {{status: "error", input: {{secret: "must-not-persist"}}, content: [{{type: "text", text: "private-output"}}]}}}}
                ]}}}}
            ];
            let drained;
            const finished = new Promise((resolve) => {{ drained = resolve; }});
            const keepAlive = setInterval(() => {{}}, 1000);
            const event = {{subscribe(options) {{
              if (!options.types.includes("session.step.ended")
                  || !options.types.includes("session.message.content.updated")) throw new Error("wrong event filter");
              return (async function*() {{ for (const item of [...events, ...events]) yield item; drained(); }})();
            }}}};
            await createPlugin().setup({{event}});
            await finished;
            clearInterval(keepAlive);
            console.log("event stream drained");
        """)
        completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True,
                                   text=True, check=True, timeout=15)
        assert "event stream drained" in completed.stdout
        assert not errors, errors
        assert len(responses) == 6
        assert sum(bool(response.get("duplicate")) for response in responses) == 3

        persisted = report(tmp_path)["sessions"][0]["host_profile"]
        assert persisted["model_calls"] == 1
        assert persisted["models"] == ["vendor/model-x"]
        assert persisted["tokens"] == {
            "input": 21, "output": 8, "reasoning": 3,
            "cache_read": 4, "cache_write": 2,
        }
        assert persisted["timing_ms"] == {
            "first_token": 100, "streamed": 200, "completed": 500, "model_turn": 500,
        }
        assert persisted["coverage"]["host_tool_calls"] == 2
        assert persisted["tools"]["bash"]["completed"]["duration_ms_sum"] == 100
        assert persisted["tools"]["edit"]["error"]["duration_ms_sum"] == 50
        raw = json.dumps(persisted)
        assert "must-not-persist" not in raw and "private-output" not in raw
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        sock.unlink(missing_ok=True)
        sockdir.rmdir()


def test_store_profile_bounds_retention_and_rejects_oversized_input(tmp_path):
    for index in range(205):
        store_profile(tmp_path, {
            "session_id": f"session-{index}",
            "metadata": {"model_calls": index, "models": [], "tokens": {}, "timing_ms": {}, "tools": {}},
        })
    files = list(state_path(tmp_path, "session_profiles").glob("*.json"))
    assert len(files) == 200
    try:
        store_profile(tmp_path, {"session_id": "bad", "metadata": {"models": ["x" * 200] * 200}})
    except ValueError:
        pass
    else:
        raise AssertionError("oversized model metadata should be rejected")


def test_active_daemon_custom_journal_is_used(tmp_path, monkeypatch):
    custom = tmp_path / "agent-logs" / "daemon.jsonl"
    custom.parent.mkdir()
    custom.write_text(json.dumps({
        "ts": "2026-11-17T10:00:00", "op": "tool", "tool": "read",
        "session": "active", "harness": "opencode", "handled": True, "ms": 2,
    }) + "\n")
    monkeypatch.setattr(
        "ozagia.session_profile_cli.active_journal",
        lambda _root: (custom, "active_daemon"),
    )
    default = state_path(tmp_path, JOURNAL_NAME)
    default.parent.mkdir(parents=True, exist_ok=True)
    default.write_text(json.dumps({
        "ts": "2026-11-17T10:00:00", "op": "tool", "tool": "write",
        "session": "wrong-daemon", "harness": "opencode", "handled": True, "ms": 8,
    }) + "\n")

    result = report(tmp_path)
    assert result["journal_source"] == "active_daemon"
    assert [item["session_id"] for item in result["sessions"]] == ["active"]


def test_opencode_install_adds_profile_sidebar_as_sibling(tmp_path):
    opencode_install.install(tmp_path)
    profile = tmp_path / opencode_install.TUI_PROFILE_REL
    brand = tmp_path / opencode_install.TUI_BRAND_REL
    assert profile.read_text(encoding="utf-8") == opencode_install.tui_profile_source()
    assert brand.read_text(encoding="utf-8") == opencode_install.tui_brand_source()
    assert "sidebar.content" in profile.read_text(encoding="utf-8")
    assert sys.executable in profile.read_text(encoding="utf-8")
    assert "__OZAGIA_PYTHON__" not in profile.read_text(encoding="utf-8")
    assert opencode_install.uninstall(tmp_path)
    assert not profile.exists()


def _sidebar_pty_output(tmp_path, extra_env: dict[str, str] | None = None) -> bytes:
    """The installed sidebar in a real TUI PTY, with no model call.

    Creates a session on a private server, installs the plugins, opens the TUI,
    toggles the sidebar and returns the captured terminal stream."""
    executable = shutil.which("opencode")
    if not executable:
        pytest.skip("opencode not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".ozagia.toml").write_text("invalid = [", encoding="utf-8")
    opencode_install.install(repo)
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "OPENCODE_PASSWORD": "profile-pty-test-only", "TERM": "xterm-256color",
        "COLUMNS": "120", "LINES": "32", "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    }
    env.update(extra_env or {})
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    base_url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [executable, "serve", "--hostname", "127.0.0.1", "--port", str(port)],
        cwd=repo, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    tui = None
    master = slave = None
    try:
        payload = None
        for _ in range(40):
            created = subprocess.run(
                [executable, "api", "--server", base_url, "session.create",
                 "--data", '{"title":"OzAGIa profile PTY fixture"}'],
                cwd=repo, env=env, capture_output=True, text=True, timeout=5,
            )
            if created.returncode == 0:
                payload = json.loads(created.stdout).get("data")
                break
            time.sleep(0.2)
        assert isinstance(payload, dict) and payload.get("id", "").startswith("ses_")
        store_daemon_event(repo, {
            "op": "tool", "session": payload["id"], "harness": "opencode",
            "tool": "read", "handled": True, "ms": 3,
            "ts": "2026-11-17T10:00:00Z",
        })

        master, slave = pty.openpty()
        tui = subprocess.Popen(
            [executable, "--server", base_url, "--session", payload["id"]],
            cwd=repo, env=env, stdin=slave, stdout=slave, stderr=slave,
        )
        os.close(slave)
        slave = None
        output = bytearray()

        def collect(seconds: float) -> None:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        return

        collect(4)
        os.write(master, b"\x18")  # TUI leader key (Ctrl+X)
        time.sleep(0.2)
        os.write(master, b"b")      # Toggle session sidebar
        collect(6)
        return bytes(output)
    finally:
        if tui is not None:
            tui.terminate()
            try:
                tui.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tui.kill()
        if slave is not None:
            os.close(slave)
        if master is not None:
            os.close(master)
        server.terminate()
        try:
            server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server.kill()


def test_opencode_profile_sidebar_renders_in_isolated_pty_without_model(tmp_path):
    output = _sidebar_pty_output(tmp_path)
    assert b"OzAGIa G:" in output
    assert b"H1 P0 F0 U0" in output
    assert b"E0" in output
    assert b"down H1" in output  # daemon loss is explicit in the sidebar


def test_opencode_profile_sidebar_shows_the_launch_phase(tmp_path):
    status = tmp_path / "launch_status.json"
    status.write_text(json.dumps({
        "version": 1, "phase": "building", "started_at": time.time() - 4,
        "updated_at": time.time(), "root": str(tmp_path), "sock": None,
    }), encoding="utf-8")
    output = _sidebar_pty_output(tmp_path, {"OZAGIA_LAUNCH_STATUS": str(status)})
    assert b"OzAGIa G:building D:waiting" in output
    # The launch line replaces the profile counters until the snapshot is ready.
    assert b"H1 P0 F0 U0" not in output
