"""Per-component timing of eval runs (src/ozagia/eval/timing.py).

The fixtures under tests/fixtures/eval_timing are scrubbed excerpts of a live
OpenCode run on Ocelot (one baseline and one graph_intent session of the task
admissible-moves-weight): event types, timestamps, tool names and times,
step tokens, and the daemon journal's op/tool/handled/intent/ms. No inputs,
outputs, paths or session ids. No model is called.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from ozagia.eval import agent as ae
from ozagia.eval import timing

FIX = Path(__file__).resolve().parent / "fixtures" / "eval_timing"
GRAPH = "admissible-moves-weight-graph_intent-r1"
BASE = "admissible-moves-weight-baseline-r1"


def _report() -> dict:
    return json.loads((FIX / "run.json").read_text(encoding="utf-8"))


def _record(arm: str) -> dict:
    return next(r for r in _report()["runs"] if r["arm"] == arm)


def _ev(kind: str, ts: int, **part) -> str:
    return json.dumps({"type": kind, "timestamp": ts, "part": part})


def _tool(name: str, start: int, end: int, ts: int | None = None) -> str:
    return _ev("tool_use", ts or end, type="tool", tool=name,
               state={"status": "completed", "time": {"start": start, "end": end}})


def _finish(ts: int, inp: int = 10, cached: int = 0, out: int = 1) -> str:
    return _ev("step_finish", ts, tokens={"input": inp, "output": out, "reasoning": 0,
                                          "cache": {"read": cached, "write": 0}})


# --------------------------------------------------------------------------- #
# opencode extraction from the live fixtures
# --------------------------------------------------------------------------- #
def test_graph_run_components_from_opencode_and_journal():
    t = timing.run_timing(_record("graph_intent"), FIX / "logs" / GRAPH)
    assert t["version"] == timing.TIMING_VERSION
    m = t["model"]
    assert m["steps"] == 15
    assert m["s"] == pytest.approx(36.588, abs=0.002)
    assert m["s"] == pytest.approx(m["pre_step_s"] + m["in_step_s"] + m["post_tool_s"],
                                   abs=0.002)
    assert m["post_tool_s"] == pytest.approx(0.382, abs=0.002)
    # The first hint is before the first event (in other); the rest in model.
    assert 0 < m["hint_waited_s"] < t["other"]["hint_waited_s"]
    assert t["model_excl_hint_s"] == pytest.approx(m["s"] - m["hint_waited_s"], abs=0.002)
    assert m["split"] == "combined"
    # Matches the record's token count; cached input comes from cache.read.
    assert m["tokens"]["input_uncached"] == _record("graph_intent")["tokens_in"]
    assert m["tokens"]["input_cached"] == 238080
    tools = t["tools"]
    assert tools["matched"] is True
    assert tools["s"] == pytest.approx(15.393, abs=0.002)
    assert tools["sum_s"] >= tools["s"]          # two greps ran in parallel
    shell = tools["by_name"]["shell"]
    assert (shell["calls"], shell["handled_calls"], shell["passthrough_calls"]) == (7, 5, 2)
    assert shell["ozagia_s"] > 0 and shell["daemon_overhead_s"] > 0
    for row in tools["by_name"].values():
        parts = row["ozagia_s"] + row["builtin_s"] + row["daemon_overhead_s"] + row["plugin_ipc_s"]
        assert parts == pytest.approx(row["s"], abs=0.01)
    assert t["other"]["s"] >= 0
    assert t["other"]["hint_waited_s"] <= t["other"]["hint_daemon_s"]
    assert t["per_task_s"] == pytest.approx(t["wall_s"], abs=0.002)
    assert t["model"]["s"] + t["tools"]["s"] + t["other"]["s"] == pytest.approx(
        t["wall_s"], abs=0.005)
    assert t["setup"]["daemon_start_s"] == 2.224
    assert "plugin_install_s" in t["setup"]["missing"]
    assert t["setup"]["partial"] is True and t["pay_once_partial"] is True
    assert t["index"]["cold_s"] == 63.307
    assert t["index"]["repeat_s"] is None          # not measured in this run
    assert t["pay_once_s"] == pytest.approx(2.224 + 63.307, abs=0.002)
    assert t["timed_out"] is False and t["timeout_tail_s"] == 0.0
    assert "log_mismatch" not in t
    assert t["check"]["s"] == 0.542


def test_baseline_tools_are_all_builtin():
    t = timing.run_timing(_record("baseline"), FIX / "logs" / BASE)
    assert t["index"]["cold_s"] == 0.0
    assert t["tools"]["ozagia_s"] == 0.0
    assert t["tools"]["builtin_s"] == t["tools"]["sum_s"]
    assert t["daemon_ops"] is None
    assert t["other"]["s"] >= 0
    # An older record: no setup part known, so setup and pay-once are unknown.
    assert t["setup"]["s"] is None and t["setup"]["partial"] is False
    assert "setup unknown" in t["reasons"]["setup"]
    assert t["pay_once_s"] is None


# --------------------------------------------------------------------------- #
# missing or partial logs
# --------------------------------------------------------------------------- #
def test_no_log_dir_gives_null_components_with_reasons():
    rec = _record("graph_intent")
    t = timing.run_timing(rec, None)
    assert t["model"] is None and t["tools"] is None and t["other"] is None
    assert t["reasons"]["model"] == "no log dir for this run"
    assert t["per_task_s"] == rec["wall_s"]       # still known from the record
    assert t["index"]["cold_s"] == rec["prep_s"]


def test_missing_driver_log_is_a_reason_not_an_error(tmp_path):
    t = timing.run_timing(_record("baseline"), tmp_path / "nope")
    assert t["model"] is None
    assert "driver.stdout.txt missing" in t["reasons"]["model"]


def test_missing_daemon_journal_leaves_the_split_unknown(tmp_path):
    shutil.copy(FIX / "logs" / GRAPH / "driver.stdout.txt", tmp_path)
    t = timing.run_timing(_record("graph_intent"), tmp_path)
    assert t["model"] is not None
    assert t["tools"]["s"] == pytest.approx(15.393, abs=0.002)
    assert t["tools"]["ozagia_s"] is None
    assert "daemon_journal.jsonl missing" in t["reasons"]["tools"]


def test_garbage_driver_log_gives_a_reason(tmp_path):
    (tmp_path / "driver.stdout.txt").write_text("not json\n{broken\n", encoding="utf-8")
    t = timing.run_timing(_record("baseline"), tmp_path)
    assert t["model"] is None
    assert "no step events" in t["reasons"]["model"]


def test_fake_driver_has_no_breakdown():
    t = timing.run_timing({"arm": "baseline", "driver": "fake", "wall_s": 0.1,
                           "check_s": 0.2, "setup_workdir_s": 0.3, "prep_s": 0.0}, None)
    assert t["model"] is None and t["per_task_s"] == 0.1
    assert t["setup"]["s"] == 0.3 and t["setup"]["missing"] == []
    assert t["check"]["s"] == 0.2


def test_index_arm_without_prep_is_unknown():
    t = timing.run_timing({"arm": "dev_memory", "driver": "fake", "wall_s": 1.0,
                           "prep_ok": None}, None)
    assert t["index"]["cold_s"] is None
    assert t["reasons"]["index"] == "no prep recorded"


# --------------------------------------------------------------------------- #
# model, tools and other on synthetic events
# --------------------------------------------------------------------------- #
def test_model_is_the_wait_between_steps_and_other_is_never_negative(tmp_path):
    events = [
        _ev("step_start", 1000),
        _tool("read", 3000, 4000),
        _finish(4100, inp=100, cached=50),
        _ev("step_start", 6100),               # 2.0 s after the last finish
        _tool("grep", 6500, 7500), _tool("grep", 7000, 8000),   # parallel
        _finish(8100, inp=20, cached=130),
        _ev("step_start", 9100),
        _finish(10100),                        # no tool: model to the finish
    ]
    (tmp_path / "driver.stdout.txt").write_text("\n".join(events) + "\n", encoding="utf-8")
    rec = {"arm": "baseline", "driver": "opencode", "wall_s": 12.0}
    t = timing.run_timing(rec, tmp_path)
    m = t["model"]
    # step 1: in 2.0 + post 0.1; step 2: pre 2.0 + in 0.4 + post 0.1;
    # step 3: pre 1.0 + in 1.0 (no tool, so no post)
    assert m["in_step_s"] == pytest.approx(3.4)
    assert m["pre_step_s"] == pytest.approx(3.0)
    assert m["post_tool_s"] == pytest.approx(0.2)
    assert m["s"] == pytest.approx(6.6)
    assert m["tokens"]["input_uncached"] == 130 and m["tokens"]["input_cached"] == 180
    assert t["tools"]["s"] == pytest.approx(2.5)       # union of 1.0 and 6.5-8.0
    assert t["tools"]["sum_s"] == pytest.approx(3.0)
    assert t["other"]["s"] == pytest.approx(12.0 - 6.6 - 2.5)
    assert t["other"]["startup_shutdown_s"] == pytest.approx(12.0 - 9.1)

    # The event clock runs past the harness wall: other is 0, with a note.
    short = timing.run_timing(dict(rec, wall_s=5.0), tmp_path)
    assert short["other"]["s"] == 0.0
    assert "exceed the harness wall" in short["other"]["clock_note"]
    assert short["per_task_s"] == pytest.approx(6.6 + 2.5)


def test_driver_start_time_splits_head_and_tail(tmp_path):
    events = [_ev("step_start", 1_000_000), _finish(1_002_000)]
    (tmp_path / "driver.stdout.txt").write_text("\n".join(events) + "\n", encoding="utf-8")
    rec = {"arm": "baseline", "driver": "opencode", "wall_s": 10.0,
           "driver_started_at": 997.0}
    other = timing.run_timing(rec, tmp_path)["other"]
    assert other["head_s"] == pytest.approx(3.0)
    assert other["tail_s"] == pytest.approx(5.0)


def test_unfinished_last_step_is_counted_to_its_last_event(tmp_path):
    events = [_ev("step_start", 1000), _finish(2000),
              _ev("step_start", 3000), _tool("shell", 3500, 9000)]
    (tmp_path / "driver.stdout.txt").write_text("\n".join(events) + "\n", encoding="utf-8")
    t = timing.run_timing({"arm": "baseline", "driver": "opencode", "wall_s": 9.0},
                          tmp_path)
    assert t["model"]["steps"] == 2
    assert t["model"]["per_step"][1]["finished"] is False
    assert t["model"]["s"] == pytest.approx(1.0 + 1.0 + 0.5)


def test_journal_pairs_preflight_and_after_with_their_call():
    entries = [
        {"op": "hint", "ms": 9000.0},
        {"op": "tool", "tool": "shell", "intent": "shell_preflight", "handled": False, "ms": 10.0},
        {"op": "tool", "tool": "shell", "intent": "shell_pytest", "handled": True, "ms": 500.0},
        {"op": "tool", "tool": "shell", "intent": "shell_passthrough", "handled": False,
         "ms": 100.0},
        {"op": "after", "tool": "shell", "intent": "after_refresh", "ms": 200.0},
    ]
    j = timing.journal_calls(entries)
    shell = j["by_tool"]["shell"]
    assert len(shell) == 2
    assert {k: shell[0][k] for k in ("handled", "decision_ms", "preflight_ms",
                                     "after_ms", "intent", "refused")} == {
        "handled": True, "decision_ms": 500.0, "preflight_ms": 10.0,
        "after_ms": 0.0, "intent": "shell_pytest", "refused": False}
    assert shell[1]["after_ms"] == 200.0 and shell[1]["handled"] is False
    assert j["ops"]["hint"] == {"count": 1, "s": 9.0,
                                "waited_s": timing.PLUGIN_HINT_TIMEOUT_S}
    calls = [{"tool": "shell", "start": 0, "end": 600},
             {"tool": "shell", "start": 1000, "end": 2000}]
    row = timing.tool_breakdown(calls, j, daemon_expected=True)["shell"]
    assert row["matched"] is True
    assert row["ozagia_s"] == pytest.approx(0.51)
    assert row["plugin_ipc_s"] == pytest.approx(0.09)
    assert row["daemon_overhead_s"] == pytest.approx(0.3)
    assert row["builtin_s"] == pytest.approx(0.7)
    # One driver call more than the journal knows: split by totals.
    calls.append({"tool": "shell", "start": 3000, "end": 3100})
    row = timing.tool_breakdown(calls, j, daemon_expected=True)["shell"]
    assert row["matched"] is False and row["plugin_ipc_s"] is None
    assert row["mismatch"] == "3 driver calls, 2 in the journal"
    assert row["builtin_s"] == pytest.approx(1.7 - 0.51 - 0.3)


# The shape of the before-fix life-step-cache graph_intent run (from the live
# logs): a shell call refused at the permission step leaves only a
# preflight in the journal, and a call still running at the timeout leaves a
# decision with no driver event. Times in s from T0; journal ts are local
# time with whole seconds, as the daemon writes them.
T0 = 1_800_000_000


def _jts(sec: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(T0 + sec))


def _jtool(sec: float, intent: str, ms: float, handled: bool | None = None) -> dict:
    return {"op": "tool", "tool": "shell", "intent": intent, "ms": ms,
            "handled": handled if handled is not None else False, "ts": _jts(sec)}


def _dcall(start: float, end: float) -> dict:
    return {"tool": "shell", "start": (T0 + start) * 1000, "end": (T0 + end) * 1000}


LIFE_CALLS = [_dcall(12.608, 13.414),    # "Permission denied: shell"
              _dcall(17.720, 18.858), _dcall(21.675, 22.251)]
LIFE_JOURNAL = [_jtool(13, "shell_preflight", 1.5),                  # refused
                _jtool(18, "shell_preflight", 2.4),
                _jtool(18, "shell_compound", 709.0, True),
                _jtool(22, "shell_preflight", 2.1),
                _jtool(22, "shell_compound", 14.4, True)]


def test_refused_preflight_is_its_own_call():
    j = timing.journal_calls(LIFE_JOURNAL)
    shell = j["by_tool"]["shell"]
    assert [c["refused"] for c in shell] == [True, False, False]
    assert shell[0]["preflight_ms"] == 1.5 and shell[1]["preflight_ms"] == 2.4
    row = timing.tool_breakdown(LIFE_CALLS, j, daemon_expected=True)["shell"]
    assert row["matched"] is True and "mismatch" not in row
    assert (row["refused_calls"], row["handled_calls"], row["passthrough_calls"]) == (1, 2, 0)
    assert row["ozagia_s"] == pytest.approx((2.4 + 709.0 + 2.1 + 14.4) / 1000, abs=0.001)
    parts = row["ozagia_s"] + row["builtin_s"] + row["daemon_overhead_s"] + row["plugin_ipc_s"]
    assert parts == pytest.approx(row["s"], abs=0.002)
    # A preflight still waiting at the end of the journal is refused too.
    tail = timing.journal_calls(LIFE_JOURNAL + [_jtool(30, "shell_preflight", 1.0)])
    assert tail["by_tool"]["shell"][-1]["refused"] is True


def test_parallel_preflights_are_not_refused():
    j = timing.journal_calls([_jtool(3, "shell_preflight", 1.7),
                              _jtool(3, "shell_preflight", 2.7),
                              _jtool(3, "shell_compound", 29.8, True),
                              _jtool(3, "shell_compound", 36.7, True)])
    shell = j["by_tool"]["shell"]
    assert [c["refused"] for c in shell] == [False, False]
    calls = [_dcall(2.640, 3.326), _dcall(3.006, 3.319)]
    assert timing.tool_breakdown(calls, j, daemon_expected=True)["shell"]["matched"]


def test_in_flight_journal_decision_falls_back_to_totals():
    # The call running at the timeout: decided at 26 s, no driver event.
    j = timing.journal_calls(LIFE_JOURNAL + [_jtool(26, "shell_passthrough", 2.2)])
    row = timing.tool_breakdown(LIFE_CALLS, j, daemon_expected=True)["shell"]
    assert row["matched"] is False and row["plugin_ipc_s"] is None
    assert row["mismatch"] == "3 driver calls, 4 in the journal (1 refused)"
    assert row["refused_calls"] == 1 and row["passthrough_calls"] == 1
    assert row["ozagia_s"] == pytest.approx((2.4 + 709.0 + 2.1 + 14.4) / 1000, abs=0.001)


def test_pairs_that_drift_by_one_call_are_rejected():
    # The refused preflight is missing and the in-flight decision is there:
    # the counts agree (3 = 3) but every pair is one call off.
    journal = LIFE_JOURNAL[1:] + [_jtool(26, "shell_passthrough", 2.2)]
    row = timing.tool_breakdown(LIFE_CALLS, timing.journal_calls(journal),
                                daemon_expected=True)["shell"]
    assert row["matched"] is False
    assert row["mismatch"].startswith("call 1: journal entry at ")
    # Daemon time longer than the call (a pytest paired with a grep).
    calls = [_dcall(48.913, 50.259)]
    j = timing.journal_calls([_jtool(50, "shell_compound", 26380.1, True)])
    row = timing.tool_breakdown(calls, j, daemon_expected=True)["shell"]
    assert row["matched"] is False
    assert row["mismatch"] == "call 1: daemon 26.38 s in a 1.35 s call"
    # Within PAIR_MS_TOLERANCE_S it is still a pair.
    j = timing.journal_calls([_jtool(50, "shell_compound", 1500.0, True)])
    assert timing.tool_breakdown(calls, j, daemon_expected=True)["shell"]["matched"]


def test_journal_in_another_time_zone_is_offset_by_whole_quarter_hours():
    shifted = [dict(e, ts=time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.localtime(timing.journal_ts(e["ts"]) - 3600)))
               for e in LIFE_JOURNAL]
    j = timing.journal_calls(shifted)
    offset = timing.journal_clock_offset(LIFE_CALLS, j)
    assert offset == 3600.0
    row = timing.tool_breakdown(LIFE_CALLS, j, daemon_expected=True, offset=offset)
    assert row["shell"]["matched"] is True
    assert timing.journal_clock_offset(LIFE_CALLS, timing.journal_calls(LIFE_JOURNAL)) == 0.0


def test_hint_wait_before_the_first_event_is_not_model_time():
    hints = [{"ts": T0 + 10, "ms": 15754.4},     # started before the first event
             {"ts": T0 + 20, "ms": 422.1}, {"ts": None, "ms": 9000.0}]
    first = (T0 + 5) * 1000
    assert timing.hint_in_model(hints[:2], first) == pytest.approx(0.4221)
    # Without stamps: every hint but the first.
    assert timing.hint_in_model([{"ts": None, "ms": 1.0}, hints[2]], None) == 6.0


def test_mcp_tools_count_as_ozagia_time():
    calls = [{"tool": "ozagia_dev_code_search", "start": 0, "end": 1500}]
    row = timing.tool_breakdown(calls, None, daemon_expected=False)
    assert row["ozagia_dev_code_search"]["ozagia_s"] == 1.5
    assert row["ozagia_dev_code_search"]["builtin_s"] == 0.0


def test_native_transcript_and_claude_result(tmp_path):
    native = tmp_path / "native"
    native.mkdir()
    lines = [{"step": 1, "ms": 2000.0, "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                                                 "prompt_tokens_details": {"cached_tokens": 60}},
              "tools": [{"name": "read", "ms": 300.0, "handled": True},
                        {"name": "shell", "ms": 700.0, "handled": False}]},
             {"step": 2, "ms": 1000.0, "usage": {"prompt_tokens": 120}, "tools": []}]
    (native / "transcript.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    t = timing.run_timing({"arm": "graph_intent", "driver": "native", "wall_s": 5.0,
                           "prep_s": 4.0, "prep_ok": True}, tmp_path)
    assert t["model"]["s"] == 3.0
    assert t["model"]["tokens"] == {"input_uncached": 160, "input_cached": 60, "output": 5}
    assert t["tools"]["s"] == 1.0
    assert t["tools"]["by_name"]["read"]["ozagia_s"] == 0.3
    assert t["tools"]["by_name"]["shell"]["builtin_s"] == 0.7
    assert t["other"]["s"] == 1.0

    (tmp_path / "driver.stdout.txt").write_text(json.dumps(
        {"duration_api_ms": 4000, "num_turns": 3,
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7}}),
        encoding="utf-8")
    c = timing.run_timing({"arm": "baseline", "driver": "claude", "wall_s": 6.0}, tmp_path)
    assert c["model"]["s"] == 4.0 and c["model"]["tokens"]["input_cached"] == 90
    assert c["tools"] is None and "no per-tool timing" in c["reasons"]["tools"]
    assert c["other"]["s"] == 2.0 and c["other"]["includes_tools"] is True


def _two_steps(tmp_path: Path) -> Path:
    events = [_ev("step_start", 1_000_000), _tool("shell", 1_001_000, 1_002_000),
              _finish(1_002_000, inp=10), _ev("step_start", 1_003_000),
              _tool("read", 1_004_000, 1_005_000)]
    (tmp_path / "driver.stdout.txt").write_text("\n".join(events) + "\n", encoding="utf-8")
    return tmp_path


def test_logs_that_do_not_match_the_record_give_no_breakdown(tmp_path):
    logs = _two_steps(tmp_path)
    rec = {"arm": "baseline", "driver": "opencode", "wall_s": 10.0,
           "tool_calls": 2, "tokens_in": 10, "driver_started_at": 998.0}
    assert timing.run_timing(rec, logs)["model"] is not None
    for change, words in (({"tool_calls": 5}, "2 tool calls in the log, 5 in the record"),
                          ({"tokens_in": 99}, "10 uncached input tokens in the log, 99"),
                          ({"driver_started_at": 1500.0}, "from the recorded driver start")):
        t = timing.run_timing(dict(rec, **change), logs)
        assert t["model"] is None and t["tools"] is None and t["other"] is None
        assert words in t["log_mismatch"]
        assert "are not this run's" in t["reasons"]["model"]
        assert t["per_task_s"] == 10.0


def test_report_warns_about_runs_whose_logs_are_not_theirs(tmp_path, capsys):
    rep = _report()
    for r in rep["runs"]:
        if r["arm"] == "graph_intent":
            r["tool_calls"] += 3                     # as if a later run's logs
    src, out_md = tmp_path / "run.json", tmp_path / "out.md"
    src.write_text(json.dumps(rep), encoding="utf-8")
    assert ae.main(["report", "--in", str(src), "--logs-root", str(FIX),
                    "--out-md", str(out_md)]) == 0
    err = capsys.readouterr().err
    assert "warning: the logs of 1 run(s) do not match their record" in err
    assert f"{GRAPH}: 7 tool calls" not in err and GRAPH in err
    md = out_md.read_text(encoding="utf-8")
    assert "- Warning: the logs of 1 run(s) do not match their record" in md
    assert GRAPH in md


def test_timed_out_run_puts_the_time_after_its_last_event_in_the_timeout_tail(tmp_path):
    logs = _two_steps(tmp_path)
    rec = {"arm": "baseline", "driver": "opencode", "wall_s": 20.0, "timed_out": True,
           "driver_started_at": 998.0}
    t = timing.run_timing(rec, logs)
    # Events span 1000-1005 s; start-up 2 s; killed 13 s after the last event.
    assert t["timed_out"] is True
    assert t["timeout_tail_s"] == pytest.approx(13.0)
    assert t["other"]["head_s"] == pytest.approx(2.0)
    assert t["other"]["s"] == pytest.approx(20.0 - 13.0 - t["model"]["s"] - t["tools"]["s"])
    assert t["per_task_s"] == pytest.approx(20.0)
    # Without the driver start the tail holds start-up too, and says so.
    t = timing.run_timing(dict(rec, driver_started_at=None), logs)
    assert t["timeout_tail_s"] == pytest.approx(15.0)
    assert t["other"]["timeout_tail_includes_head"] is True
    # A run that finished has no tail.
    t = timing.run_timing(dict(rec, timed_out=False), logs)
    assert t["timeout_tail_s"] == 0.0
    row = timing.summarize_arm([{"timing": timing.run_timing(rec, logs)},
                                {"timing": t}], 2)
    assert row["timed_out_runs"] == 1
    assert row["mean_timeout_tail_s"] == pytest.approx(6.5)
    row["arm"] = "baseline"
    assert any("Runs killed at the timeout: 1 of 2 baseline" in x
               for x in timing.headline([row]))


# --------------------------------------------------------------------------- #
# amortization and the per-arm summary
# --------------------------------------------------------------------------- #
def test_amortization_math():
    # (cold + (N - 1) * repeat) / N + setup + per task; setup is not divided.
    assert timing.amortized(70.0, 0.0, 0.0, 60.0, 7) == 70.0
    assert timing.amortized(35.0, None, 2.0, 100.0, 1) == 137.0
    assert timing.amortized(40.0, 5.0, 2.0, 60.0, 4) == pytest.approx(
        (40.0 + 3 * 5.0) / 4 + 2.0 + 60.0)
    assert timing.amortized(40.0, None, 2.0, 60.0, 4) == pytest.approx(10.0 + 62.0)
    assert timing.amortized(0.0, None, None, 10.0, 3) is None     # setup unknown
    assert timing.amortized(None, 1.0, 0.0, 10.0, 3) is None      # index unknown
    assert timing.amortized(5.0, 1.0, 0.0, None, 3) is None
    # The user's case: 30 s less agent time, 35 s more index once.
    base = timing.amortized(0.0, None, 1.0, 100.0, 8)
    graph = timing.amortized(35.0, 3.0, 3.0, 70.0, 8)
    assert graph < base
    assert timing.amortized(35.0, 3.0, 3.0, 70.0, 1) > base


def _timed(per_task: float, setup: float | None, index: float,
           model: float | None = None, partial: bool = False) -> dict:
    return {"arm": "graph_intent", "task": "t", "timing": {
        "setup": {"s": setup, "partial": partial,
                  "missing": ["workdir_s"] if partial or setup is None else []},
        "index": {"cold_s": index},
        "model": None if model is None else {"s": model, "tokens": {}},
        "per_task_s": per_task, "other": None, "tools": None, "check": {"s": 1.0}}}


def test_summarize_arm_means_and_amortizes():
    runs = [_timed(60.0, 2.0, 30.0, model=40.0), _timed(80.0, 4.0, 50.0)]
    row = timing.summarize_arm(runs, 4, index_repeat_s=12.0)
    assert row["per_task_s"] == 70.0
    assert row["mean_setup_s"] == 3.0 and row["mean_index_s"] == 40.0
    assert row["mean_model_s"] == 40.0            # the unknown run is left out
    assert row["breakdown_runs"] == 1
    assert row["pay_once_s"] == 43.0
    assert row["amortized_s"] == pytest.approx((40.0 + 3 * 12.0) / 4 + 3.0 + 70.0)
    assert row["index_repeat_s"] == 12.0
    assert row["setup_partial"] is False and row["repeat_assumed_zero"] is False
    # An arm without an index shows no repeat cost.
    assert timing.summarize_arm([_timed(10.0, 0.0, 0.0)], 4, 12.0)["index_repeat_s"] is None
    # No repeat cost measured: counted as 0, and flagged.
    row = timing.summarize_arm(runs, 4)
    assert row["repeat_assumed_zero"] is True
    assert row["amortized_s"] == pytest.approx(40.0 / 4 + 3.0 + 70.0)


def test_unknown_setup_leaves_pay_once_blank_and_partial_setup_is_marked():
    unknown = timing.summarize_arm([_timed(10.0, None, 0.0)], 4)
    assert unknown["mean_setup_s"] is None
    assert unknown["pay_once_s"] is None and unknown["amortized_s"] is None
    partial = timing.summarize_arm([_timed(20.0, 2.0, 30.0, partial=True)], 4, 3.0)
    assert partial["setup_partial"] is True and partial["setup_missing"] == ["workdir_s"]
    assert partial["pay_once_s"] == 32.0
    for row, arm in ((unknown, "baseline"), (partial, "graph_intent")):
        row["arm"] = arm
    lines = timing.headline([unknown, partial])
    amort = next(x for x in lines if x.startswith("- Amortized"))
    assert "baseline unknown" in amort
    assert f"graph_intent {(30.0 + 3 * 3.0) / 4 + 2.0 + 20.0:.1f} s*" in amort
    assert any("baseline: setup not recorded" in x for x in lines)
    assert any("* graph_intent: setup only partly recorded (missing workdir_s)" in x
               for x in lines)
    md = "\n".join(timing.render_markdown_sections([unknown, partial]))
    assert "| 2.0* |" in md and "| 32.0* |" in md
    assert "| baseline | 1 | 0 | 0 |  |" in md      # setup blank, not 0.0


def test_aggregate_rows_carry_timing_and_default_n_is_tasks_run():
    report = ae.rereport(_report(), logs_root=FIX)
    rows = {r["arm"]: r for r in report["summary"]}
    assert report["amortize_n"] == 1
    assert report["log_mismatches"] == []
    g, b = rows["graph_intent"], rows["baseline"]
    assert g["per_task_s"] == pytest.approx(66.155, abs=0.002)
    assert g["amortized_s"] == pytest.approx(g["pay_once_s"] + g["per_task_s"], abs=0.002)
    assert g["setup_partial"] is True
    assert b["mean_setup_s"] is None and b["amortized_s"] is None
    assert g["mean_tools_by_name"]["shell"]["calls"] == 7.0
    # Existing columns are still there.
    for key in ("mean_wall_s", "mean_prep_s", "success_rate", "mean_tokens_in"):
        assert key in g
    n4 = ae.rereport(_report(), logs_root=FIX, amortize=4)
    g4 = {r["arm"]: r for r in n4["summary"]}["graph_intent"]
    assert g4["repeat_assumed_zero"] is True
    assert g4["amortized_s"] == pytest.approx(
        g4["mean_index_s"] / 4 + g4["mean_setup_s"] + g4["per_task_s"], abs=0.002)
    # A measured repeat cost enters as (cold + (N - 1) * repeat) / N.
    rep = _report()
    for r in rep["runs"]:
        if r["arm"] == "graph_intent":
            r["prep_repeat_s"] = 6.0
    g4r = {r["arm"]: r for r in ae.rereport(rep, logs_root=FIX, amortize=4)["summary"]}
    g4r = g4r["graph_intent"]
    assert g4r["index_repeat_s"] == 6.0 and g4r["repeat_assumed_zero"] is False
    assert g4r["amortized_s"] == pytest.approx(g4["amortized_s"] + 3 * 6.0 / 4, abs=0.002)


def test_markdown_has_headline_once_components_and_old_columns():
    report = ae.rereport(_report(), logs_root=FIX, amortize=8)
    md = ae.render_markdown(report)
    assert "## Time per component" in md
    line = ("- Per task (agent time, without setup and index): baseline 137.7 s, "
            "graph_intent 66.2 s (-71.5 s vs baseline).")
    assert md.count(line) == 1
    assert md.count("- Amortized over 8 tasks on one revision") == 1
    assert "graph_intent 35.9 s without its" in md        # model without hint wait
    assert "index repeat cost not measured in this run, counted as 0" in md
    assert "| arm | runs | runs with breakdown | timed out | setup | index cold |" in md
    assert "### Tools (mean per run)" in md
    assert "| graph_intent | shell | 7.0 |" in md
    assert "mean wall s | mean prep s" in md     # the old per-arm table


def test_markdown_without_timing_is_unchanged_in_shape():
    runs = [{"task": "t", "arm": "baseline", "repeat": 1, "success": True}]
    rows = ae.aggregate(runs, ["baseline"])
    rows[0]["timing_runs"] = 0
    assert timing.render_markdown_sections(rows) == []
    assert timing.headline([{"arm": "baseline", "per_task_s": None}]) == []


# --------------------------------------------------------------------------- #
# the report subcommand
# --------------------------------------------------------------------------- #
def test_report_subcommand_recomputes_from_json_and_logs(tmp_path, capsys):
    src = tmp_path / "run.json"
    shutil.copy(FIX / "run.json", src)
    out_md, out_json = tmp_path / "out.md", tmp_path / "out.json"
    code = ae.main(["report", "--in", str(src), "--logs-root", str(FIX),
                    "--out-md", str(out_md), "--out-json", str(out_json)])
    assert code == 0
    doc = json.loads(out_json.read_text(encoding="utf-8"))
    assert all(r["timing"]["model"] is not None for r in doc["runs"])
    assert doc["amortize_n"] == 1
    assert "## Time per component" in out_md.read_text(encoding="utf-8")
    assert "Per task" in capsys.readouterr().out
    # The input is not modified.
    assert "timing" not in json.loads(src.read_text(encoding="utf-8"))["runs"][0]


def test_report_subcommand_without_logs_still_reports(tmp_path, capsys):
    out_md = tmp_path / "out.md"
    code = ae.main(["report", "--in", str(FIX / "run.json"),
                    "--logs-root", str(tmp_path / "missing"), "--out-md", str(out_md)])
    assert code == 0
    assert "have no model/tool breakdown" in capsys.readouterr().out
    assert "## Time per component" in out_md.read_text(encoding="utf-8")


def test_report_subcommand_rejects_bad_input(tmp_path, capsys):
    assert ae.main(["report", "--in", str(tmp_path / "none.json")]) == 2
    assert ae.main(["report", "--in", str(FIX / "run.json"), "--amortize", "0"]) == 2
    assert "error:" in capsys.readouterr().err


def test_resolve_log_dir_accepts_run_root_or_logs_dir(tmp_path):
    rec = {"log_dir": "/elsewhere/logs/x-baseline-r1"}
    (tmp_path / "logs" / "x-baseline-r1").mkdir(parents=True)
    assert ae.resolve_log_dir(rec, tmp_path) == tmp_path / "logs" / "x-baseline-r1"
    assert ae.resolve_log_dir(rec, tmp_path / "logs") == tmp_path / "logs" / "x-baseline-r1"
    assert ae.resolve_log_dir(rec, tmp_path / "other") is None
    assert ae.resolve_log_dir(rec, None) == Path("/elsewhere/logs/x-baseline-r1")
