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
    assert m["s"] == pytest.approx(36.206, abs=0.002)
    assert m["s"] == pytest.approx(m["pre_step_s"] + m["in_step_s"], abs=0.002)
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
    assert t["index"]["cold_s"] == 63.307
    assert t["pay_once_s"] == pytest.approx(2.224 + 63.307, abs=0.002)
    assert t["check"]["s"] == 0.542


def test_baseline_tools_are_all_builtin():
    t = timing.run_timing(_record("baseline"), FIX / "logs" / BASE)
    assert t["index"]["cold_s"] == 0.0
    assert t["tools"]["ozagia_s"] == 0.0
    assert t["tools"]["builtin_s"] == t["tools"]["sum_s"]
    assert t["daemon_ops"] is None
    assert t["other"]["s"] >= 0
    assert t["pay_once_s"] == 0.0


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
    # step 1: in 2.0; step 2: pre 2.0 + in 0.4; step 3: pre 1.0 + in 1.0
    assert m["in_step_s"] == pytest.approx(3.4)
    assert m["pre_step_s"] == pytest.approx(3.0)
    assert m["s"] == pytest.approx(6.4)
    assert m["tokens"]["input_uncached"] == 130 and m["tokens"]["input_cached"] == 180
    assert t["tools"]["s"] == pytest.approx(2.5)       # union of 1.0 and 6.5-8.0
    assert t["tools"]["sum_s"] == pytest.approx(3.0)
    assert t["other"]["s"] == pytest.approx(12.0 - 6.4 - 2.5)
    assert t["other"]["startup_shutdown_s"] == pytest.approx(12.0 - 9.1)

    # The event clock runs past the harness wall: other is 0, with a note.
    short = timing.run_timing(dict(rec, wall_s=5.0), tmp_path)
    assert short["other"]["s"] == 0.0
    assert "exceed the harness wall" in short["other"]["clock_note"]
    assert short["per_task_s"] == pytest.approx(6.4 + 2.5)


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
    assert shell[0] == {"handled": True, "decision_ms": 500.0, "preflight_ms": 10.0,
                        "after_ms": 0.0, "intent": "shell_pytest"}
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
    assert row["builtin_s"] == pytest.approx(1.7 - 0.51 - 0.3)


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


# --------------------------------------------------------------------------- #
# amortization and the per-arm summary
# --------------------------------------------------------------------------- #
def test_amortization_math():
    assert timing.amortized(70.0, 60.0, 7) == 70.0
    assert timing.amortized(35.0, 100.0, 1) == 135.0
    assert timing.amortized(None, 10.0, 3) == 10.0
    assert timing.amortized(5.0, None, 3) is None
    # The user's case: 30 s less agent time, 35 s more pay-once.
    base, graph = timing.amortized(0.0, 100.0, 8), timing.amortized(35.0, 70.0, 8)
    assert graph < base
    assert timing.amortized(35.0, 70.0, 1) > base


def _timed(per_task: float, setup: float, index: float, model: float | None = None) -> dict:
    return {"arm": "graph_intent", "task": "t", "timing": {
        "setup": {"s": setup}, "index": {"cold_s": index},
        "model": None if model is None else {"s": model, "tokens": {}},
        "per_task_s": per_task, "other": None, "tools": None, "check": {"s": 1.0}}}


def test_summarize_arm_means_and_amortizes():
    runs = [_timed(60.0, 2.0, 30.0, model=40.0), _timed(80.0, 4.0, 50.0)]
    row = timing.summarize_arm(runs, 4, warm_index_s=12.0)
    assert row["per_task_s"] == 70.0
    assert row["mean_setup_s"] == 3.0 and row["mean_index_s"] == 40.0
    assert row["mean_model_s"] == 40.0            # the unknown run is left out
    assert row["breakdown_runs"] == 1
    assert row["pay_once_s"] == 43.0
    assert row["amortized_s"] == pytest.approx(43.0 / 4 + 70.0)
    assert row["warm_index_s"] == 12.0
    # An arm without an index shows no warm index.
    assert timing.summarize_arm([_timed(10.0, 0.0, 0.0)], 4, 12.0)["warm_index_s"] is None


def test_aggregate_rows_carry_timing_and_default_n_is_tasks_run():
    report = ae.rereport(_report(), logs_root=FIX)
    rows = {r["arm"]: r for r in report["summary"]}
    assert report["amortize_n"] == 1
    g, b = rows["graph_intent"], rows["baseline"]
    assert g["per_task_s"] == pytest.approx(66.155, abs=0.002)
    assert g["amortized_s"] == pytest.approx(g["pay_once_s"] + g["per_task_s"], abs=0.002)
    assert b["amortized_s"] == b["per_task_s"]
    assert g["mean_tools_by_name"]["shell"]["calls"] == 7.0
    # Existing columns are still there.
    for key in ("mean_wall_s", "mean_prep_s", "success_rate", "mean_tokens_in"):
        assert key in g
    n4 = ae.rereport(_report(), logs_root=FIX, amortize=4)
    g4 = {r["arm"]: r for r in n4["summary"]}["graph_intent"]
    assert g4["amortized_s"] == pytest.approx(g4["pay_once_s"] / 4 + g4["per_task_s"],
                                              abs=0.002)


def test_markdown_has_headline_components_and_old_columns():
    report = ae.rereport(_report(), logs_root=FIX, amortize=8)
    md = ae.render_markdown(report)
    assert "## Time per component" in md
    assert "- Per task (agent time, no pay-once costs): baseline 137.7 s, " \
           "graph_intent 66.2 s (-71.5 s vs baseline)." in md
    assert "- Amortized over 8 tasks on one revision" in md
    assert "| arm | runs | runs with breakdown | setup | index cold |" in md
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
