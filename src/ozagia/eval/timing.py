"""ozagia.eval.timing — per-component time of one eval run, from its logs.

Total wall-clock hides what a run spent its time on. A graph arm that saves
30 s of agent time and spends 35 s on a one-time index is not slower for a
second task on the same revision. This module splits a run into components
(docs/eval_agent.md, "Time per component"):

  setup   workdir creation and break patch, plugin install, daemon start.
          Pay-once per session.
  index   prep (graph build and tag cache), cold. Pay-once per revision.
          `warm_s`, where measured, is the same prep run a second time on
          the same tree.
  model   waiting for the model: per step, from the end of the previous
          step to the step's first tool call (or its finish when it calls
          no tool). Prefill and generation together: no driver reports
          per-token timing.
  tools   the union of the tool calls' intervals as the driver saw them,
          and per tool name the time in the built-in, in ozagia (the
          daemon's handled `ms`), and the daemon's overhead on calls it
          passed through.
  other   driver wall minus model and tools, never negative: driver start
          and exit, the first request's wait before its first event, and
          per-step bookkeeping.
  check   the task check. Not agent time.

`per_task_s` is model + tools + other (the driver wall, or more when the
driver's event clock runs past the harness timer). `pay_once_s` is setup +
index. Every function here is pure over the files it reads and never raises
on missing or malformed logs: a component it cannot compute is None, with
the reason in `reasons`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

TIMING_VERSION = 1
# Read at most this many bytes of a driver log or journal.
MAX_LOG_BYTES = 64 * 1024 * 1024
# Arms whose runs build the dev-memory index before the driver starts.
INDEX_ARMS = ("dev_memory", "graph_intent")
# MCP tool names of the ozagia server start with this (`ozagia_dev_...`).
OZAGIA_TOOL_PREFIX = "ozagia"
# The plugin waits this long for a task hint before the first model request
# goes out without it (`HINT_TIMEOUT_MS` in integrations/opencode/plugin.js).
PLUGIN_HINT_TIMEOUT_S = 6.0
COMPONENTS = ("setup", "index", "model", "tools", "other", "check")


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _read_text(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(MAX_LOG_BYTES)
    except OSError:
        return None


def _json_lines(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def _union_s(intervals: Iterable[tuple[float, float]]) -> float:
    """Total length (in the intervals' unit) covered by at least one interval."""
    total = 0.0
    cur_start = cur_end = None
    for start, end in sorted((s, e) for s, e in intervals if e >= s):
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        total += cur_end - cur_start
    return total


# --------------------------------------------------------------------------- #
# driver logs
# --------------------------------------------------------------------------- #
def opencode_events(text: str) -> dict[str, Any]:
    """Steps, tool calls and tokens from `opencode run --format json`.

    Timestamps are OpenCode's wall clock in ms: the event `timestamp`, and
    each tool part's `state.time.{start,end}`. The `step_start` event is
    printed after OpenCode has the response stream and has taken its
    per-step snapshot, so most of a step's wait comes before it: in the live
    Ocelot runs the step_start-to-first-tool time was a few ms in most steps
    and the gap since the previous step_finish was seconds. A step's model
    time is therefore previous step end -> first tool start (or step
    finish), split into `pre_s` (gap before step_start) and `in_s`
    (step_start -> first tool). The first step has no observable start (the
    request goes out before any event), so its `pre_s` is 0 and that wait
    is in `other`. Returns {} when there is no step.
    """
    steps: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    calls: list[dict[str, Any]] = []
    stamps: list[float] = []
    for event in _json_lines(text):
        kind = event.get("type")
        ts = _num(event.get("timestamp"))
        if ts is not None:
            stamps.append(ts)
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if kind == "step_start":
            cur = {"start": ts, "finish": None, "last": ts, "tools": [],
                   "tokens": None}
            steps.append(cur)
            continue
        if cur is None:
            # An event before any step_start: a step with an unknown start.
            cur = {"start": None, "finish": None, "last": ts, "tools": [],
                   "tokens": None}
            steps.append(cur)
        if ts is not None:
            cur["last"] = ts
        if kind == "tool_use":
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            times = state.get("time") if isinstance(state.get("time"), dict) else {}
            start, end = _num(times.get("start")), _num(times.get("end"))
            if start is None or end is None or end < start:
                continue
            stamps += [start, end]
            call = {"tool": str(part.get("tool") or "?"), "start": start,
                    "end": end, "status": state.get("status")}
            cur["tools"].append(call)
            calls.append(call)
        elif kind == "step_finish":
            cur["finish"] = ts
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            cur["tokens"] = {"input_uncached": int(_num(tok.get("input")) or 0),
                             "input_cached": int(_num(cache.get("read")) or 0),
                             "cache_write": int(_num(cache.get("write")) or 0),
                             "output": int(_num(tok.get("output")) or 0),
                             "reasoning": int(_num(tok.get("reasoning")) or 0)}
            cur = None
    if not steps:
        return {}
    per_step = []
    prev_end: float | None = None
    for step in steps:
        tool_starts = [c["start"] for c in step["tools"]]
        end = step["finish"] if step["finish"] is not None else step["last"]
        first = min(tool_starts) if tool_starts else end
        start = step["start"]
        in_s = (max(0.0, first - start) / 1000
                if start is not None and first is not None else 0.0)
        pre_s = (max(0.0, start - prev_end) / 1000
                 if start is not None and prev_end is not None else 0.0)
        tool_end = max((c["end"] for c in step["tools"]), default=None)
        candidates = [x for x in (end, tool_end) if x is not None]
        if candidates:
            prev_end = max(candidates)
        per_step.append({
            "model_s": _r(pre_s + in_s), "pre_s": _r(pre_s), "in_s": _r(in_s),
            "tools": len(step["tools"]),
            "tools_s": _r(_union_s((c["start"], c["end"])
                                   for c in step["tools"]) / 1000),
            "finished": step["finish"] is not None,
            **(step["tokens"] or {})})
    tokens = {k: sum(int(s.get(k) or 0) for s in per_step)
              for k in ("input_uncached", "input_cached", "cache_write",
                        "output", "reasoning")}
    return {"steps": per_step, "calls": calls, "tokens": tokens,
            "span_s": (max(stamps) - min(stamps)) / 1000 if stamps else 0.0,
            "first_ts": min(stamps) if stamps else None,
            "tools_union_s": _union_s((c["start"], c["end"]) for c in calls) / 1000}


def native_transcript(text: str) -> dict[str, Any]:
    """Steps from the native loop's `transcript.jsonl`: per step the model
    request's `ms` (a non-streaming call, so prefill and generation are one
    number) and per tool its `ms` and `handled`. Tool calls run one after
    another, so their union is their sum."""
    per_step = []
    calls = []
    for entry in _json_lines(text):
        ms = _num(entry.get("ms"))
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
        details = (usage.get("prompt_tokens_details")
                   if isinstance(usage.get("prompt_tokens_details"), dict) else {})
        prompt = int(_num(usage.get("prompt_tokens")) or 0)
        cached = int(_num(details.get("cached_tokens")) or 0)
        tools = entry.get("tools") if isinstance(entry.get("tools"), list) else []
        step_tools = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tms = _num(tool.get("ms"))
            if tms is None:
                continue
            call = {"tool": str(tool.get("name") or "?"), "s": tms / 1000,
                    "handled": tool.get("handled")}
            step_tools.append(call)
            calls.append(call)
        per_step.append({"model_s": _r((ms or 0.0) / 1000), "tools": len(step_tools),
                         "tools_s": _r(sum(c["s"] for c in step_tools)),
                         "input_uncached": max(0, prompt - cached),
                         "input_cached": cached,
                         "output": int(_num(usage.get("completion_tokens")) or 0)})
    if not per_step:
        return {}
    tokens = {k: sum(int(s.get(k) or 0) for s in per_step)
              for k in ("input_uncached", "input_cached", "output")}
    return {"steps": per_step, "calls": calls, "tokens": tokens}


def claude_result(text: str) -> dict[str, Any]:
    """`claude -p --output-format json`: `duration_api_ms` (time in model
    requests) and usage. Field names from the CLI's documented JSON output,
    not confirmed by a live run of this harness."""
    try:
        doc = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(doc, dict):
        return {}
    usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
    api_ms = _num(doc.get("duration_api_ms"))
    return {"model_s": None if api_ms is None else api_ms / 1000,
            "steps": int(_num(doc.get("num_turns")) or 0) or None,
            "tokens": {"input_uncached": int(_num(usage.get("input_tokens")) or 0),
                       "input_cached": int(_num(usage.get("cache_read_input_tokens")) or 0),
                       "cache_write": int(_num(usage.get("cache_creation_input_tokens"))
                                          or 0),
                       "output": int(_num(usage.get("output_tokens")) or 0)}}


# --------------------------------------------------------------------------- #
# daemon journal
# --------------------------------------------------------------------------- #
def journal_calls(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Per tool name, the daemon's view of each call, in journal order.

    One call of the plugin can write up to three entries: a `tool` entry
    with an intent ending in `_preflight` (the permission request before a
    mutating call), the deciding `tool` entry (`handled` true or false), and
    for a call left to the built-in an `after` entry (post-processing of
    the built-in's output, awaited inside the tool call). Preflights attach
    to the next deciding entry of the same tool, `after` entries to the
    previous one. Other ops (hint, working_set, session_event, stats, ...)
    are totalled in `ops`."""
    by_tool: dict[str, list[dict[str, Any]]] = {}
    pending_preflight: dict[str, float] = {}
    ops: dict[str, dict[str, Any]] = {}
    for e in entries:
        op = str(e.get("op") or "?")
        tool = str(e.get("tool") or "?")
        ms = _num(e.get("ms")) or 0.0
        intent = str(e.get("intent") or "")
        if op == "tool":
            if intent.endswith("_preflight"):
                pending_preflight[tool] = pending_preflight.get(tool, 0.0) + ms
                continue
            by_tool.setdefault(tool, []).append({
                "handled": e.get("handled") is True and not e.get("expired"),
                "decision_ms": ms, "preflight_ms": pending_preflight.pop(tool, 0.0),
                "after_ms": 0.0, "intent": intent or None})
        elif op == "after" and by_tool.get(tool):
            by_tool[tool][-1]["after_ms"] += ms
        else:
            slot = ops.setdefault(op, {"count": 0, "s": 0.0})
            slot["count"] += 1
            slot["s"] += ms / 1000
            if op == "hint":
                # The plugin stops waiting after PLUGIN_HINT_TIMEOUT_S; the
                # daemon may go on (and delay the next calls).
                slot["waited_s"] = (slot.get("waited_s", 0.0)
                                    + min(ms / 1000, PLUGIN_HINT_TIMEOUT_S))
    for slot in ops.values():
        slot["s"] = round(slot["s"], 3)
        if "waited_s" in slot:
            slot["waited_s"] = round(slot["waited_s"], 3)
    return {"by_tool": by_tool, "ops": dict(sorted(ops.items()))}


def tool_breakdown(calls: list[dict[str, Any]], journal: dict[str, Any] | None,
                   *, daemon_expected: bool) -> dict[str, Any]:
    """Per tool name: calls, driver-seen seconds, and where they went.

    `calls` are the driver's tool calls ({tool, start, end} in ms). With a
    journal, the calls of a tool name are paired in start order with the
    daemon's deciding entries of that name; when the counts differ the pair
    is not trusted (`matched: false`) and the split is by totals:
    ozagia = handled ms, daemon overhead = the rest of the daemon's ms,
    built-in = what is left (including plugin IPC). Without a journal and
    with no daemon expected (baseline, dev_memory), every non-ozagia tool's
    time is built-in time, and MCP tools named `ozagia...` are ozagia time.
    """
    names: dict[str, list[dict[str, Any]]] = {}
    for c in sorted(calls, key=lambda c: c["start"]):
        names.setdefault(c["tool"], []).append(c)
    jt = (journal or {}).get("by_tool") or {}
    out: dict[str, Any] = {}
    for name, mine in sorted(names.items()):
        durs = [(c["end"] - c["start"]) / 1000 for c in mine]
        row: dict[str, Any] = {"calls": len(mine), "s": sum(durs),
                               "handled_calls": 0, "passthrough_calls": 0,
                               "ozagia_s": 0.0, "builtin_s": 0.0,
                               "daemon_overhead_s": 0.0, "plugin_ipc_s": 0.0,
                               "matched": True}
        decisions = jt.get(name, [])
        if name.startswith(OZAGIA_TOOL_PREFIX) and not decisions:
            row["ozagia_s"] = row["s"]
        elif journal is None and daemon_expected:
            row.update(ozagia_s=None, builtin_s=None, daemon_overhead_s=None,
                       plugin_ipc_s=None, handled_calls=None,
                       passthrough_calls=None, matched=False)
        elif not decisions:
            row["builtin_s"] = row["s"]
        elif len(decisions) == len(mine):
            for dur, d in zip(durs, decisions):
                daemon_s = (d["decision_ms"] + d["preflight_ms"] + d["after_ms"]) / 1000
                if d["handled"]:
                    row["handled_calls"] += 1
                    work = (d["decision_ms"] + d["preflight_ms"]) / 1000
                    row["ozagia_s"] += work
                    row["plugin_ipc_s"] += max(0.0, dur - daemon_s)
                    row["daemon_overhead_s"] += d["after_ms"] / 1000
                else:
                    row["passthrough_calls"] += 1
                    row["daemon_overhead_s"] += daemon_s
                    row["builtin_s"] += max(0.0, dur - daemon_s)
        else:
            handled = [d for d in decisions if d["handled"]]
            passed = [d for d in decisions if not d["handled"]]
            oz = sum(d["decision_ms"] + d["preflight_ms"] for d in handled) / 1000
            over = (sum(d["decision_ms"] + d["preflight_ms"] + d["after_ms"]
                        for d in passed)
                    + sum(d["after_ms"] for d in handled)) / 1000
            row.update(handled_calls=len(handled), passthrough_calls=len(passed),
                       ozagia_s=oz, daemon_overhead_s=over,
                       builtin_s=max(0.0, row["s"] - oz - over), plugin_ipc_s=None,
                       matched=False)
        out[name] = {k: (_r(v) if isinstance(v, float) else v) for k, v in row.items()}
    return out


def _sum_field(by_name: dict[str, Any], key: str) -> float | None:
    values = [row.get(key) for row in by_name.values()]
    if any(v is None for v in values):
        return None
    return _r(sum(float(v) for v in values))


# --------------------------------------------------------------------------- #
# one run
# --------------------------------------------------------------------------- #
def run_timing(record: dict[str, Any], log_dir: str | Path | None) -> dict[str, Any]:
    """The `timing` block of one run record, from the record and its logs."""
    try:
        return _run_timing(record, Path(log_dir) if log_dir else None)
    except Exception as exc:  # noqa: BLE001 - timing must never break a run
        return {"version": TIMING_VERSION, "error": f"{type(exc).__name__}: {exc}",
                **{c: None for c in COMPONENTS}, "per_task_s": _num(record.get("wall_s")),
                "pay_once_s": None, "reasons": {"all": "timing failed"}}


def _run_timing(record: dict[str, Any], log_dir: Path | None) -> dict[str, Any]:
    reasons: dict[str, str] = {}
    arm = record.get("arm")
    driver = str(record.get("driver") or "")
    wall = _num(record.get("wall_s"))

    # setup
    parts = {"workdir_s": _num(record.get("setup_workdir_s")),
             "plugin_install_s": _num(record.get("plugin_install_s")),
             "daemon_start_s": _num(record.get("daemon_ready_s"))}
    uses_daemon = record.get("daemon_ok") is not None
    expected = ["workdir_s"] + (["plugin_install_s", "daemon_start_s"]
                                if uses_daemon else [])
    missing = [k for k in expected if parts[k] is None]
    known = [parts[k] for k in expected if parts[k] is not None]
    setup = {"s": _r(sum(known)) if known else None,
             **{k: _r(v) for k, v in parts.items()}, "missing": missing}
    if missing:
        reasons["setup"] = ("not recorded by this harness version: "
                            + ", ".join(missing))

    # index
    prepped = arm in INDEX_ARMS and record.get("prep_ok") is not None
    if arm in INDEX_ARMS and not prepped:
        index: dict[str, Any] = {"cold_s": None, "warm_s": None, "ok": None}
        reasons["index"] = "no prep recorded"
    else:
        index = {"cold_s": _r(_num(record.get("prep_s")) or 0.0) if prepped else 0.0,
                 "warm_s": _r(_num(record.get("prep_warm_s"))),
                 "ok": record.get("prep_ok") if prepped else None}
    index["s"] = index["cold_s"]

    check = {"s": _r(_num(record.get("check_s")))}
    if check["s"] is None:
        reasons["check"] = "no check time in the record"

    model: dict[str, Any] | None = None
    tools: dict[str, Any] | None = None
    tools_union: float | None = None
    span: float | None = None
    first_ts: float | None = None
    daemon_ops: dict[str, Any] | None = None
    stdout = _read_text(log_dir / "driver.stdout.txt") if log_dir else None
    if log_dir is None:
        reasons["model"] = reasons["tools"] = "no log dir for this run"
    elif driver == "opencode":
        parsed = opencode_events(stdout) if stdout is not None else {}
        if not parsed:
            reasons["model"] = reasons["tools"] = (
                "driver.stdout.txt missing" if stdout is None
                else "no step events in driver.stdout.txt")
        else:
            journal = None
            jtext = _read_text(log_dir / "daemon_journal.jsonl")
            if jtext is not None:
                journal = journal_calls(_json_lines(jtext))
                daemon_ops = journal["ops"]
            elif uses_daemon:
                reasons["tools"] = ("daemon_journal.jsonl missing: no ozagia/built-in "
                                    "split")
            steps = parsed["steps"]
            model = {"s": _r(sum(s["model_s"] for s in steps)),
                     "pre_step_s": _r(sum(s["pre_s"] for s in steps)),
                     "in_step_s": _r(sum(s["in_s"] for s in steps)),
                     "steps": len(steps),
                     "split": "combined",
                     "split_reason": "opencode reports no per-token timing",
                     "tokens": parsed["tokens"], "per_step": steps}
            tools_union = parsed["tools_union_s"]
            span = parsed["span_s"]
            first_ts = parsed["first_ts"]
            by_name = tool_breakdown(parsed["calls"], journal,
                                     daemon_expected=uses_daemon)
            tools = _tools_block(by_name, tools_union)
    elif driver == "native":
        ttext = _read_text(log_dir / "native" / "transcript.jsonl")
        parsed = native_transcript(ttext) if ttext is not None else {}
        if not parsed:
            reasons["model"] = reasons["tools"] = "native/transcript.jsonl missing or empty"
        else:
            steps = parsed["steps"]
            model = {"s": _r(sum(s["model_s"] for s in steps)), "steps": len(steps),
                     "split": "combined",
                     "split_reason": "the native loop makes non-streaming requests",
                     "tokens": parsed["tokens"], "per_step": steps}
            by_name: dict[str, Any] = {}
            for c in parsed["calls"]:
                row = by_name.setdefault(c["tool"], {
                    "calls": 0, "s": 0.0, "handled_calls": 0, "passthrough_calls": 0,
                    "ozagia_s": 0.0, "builtin_s": 0.0, "daemon_overhead_s": None,
                    "plugin_ipc_s": None, "matched": True})
                row["calls"] += 1
                row["s"] += c["s"]
                if c["handled"] is True:
                    row["handled_calls"] += 1
                    row["ozagia_s"] += c["s"]
                else:
                    if c["handled"] is False:
                        row["passthrough_calls"] += 1
                    # Built-in plus ozagia's after-processing: one timer.
                    row["builtin_s"] += c["s"]
            by_name = {k: {f: (_r(v) if isinstance(v, float) else v)
                           for f, v in row.items()} for k, row in sorted(by_name.items())}
            tools_union = sum(c["s"] for c in parsed["calls"])
            tools = _tools_block(by_name, tools_union)
    elif driver == "claude":
        parsed = claude_result(stdout) if stdout is not None else {}
        if not parsed or parsed.get("model_s") is None:
            reasons["model"] = "no duration_api_ms in driver.stdout.txt"
        else:
            model = {"s": _r(parsed["model_s"]), "steps": parsed.get("steps"),
                     "split": "combined",
                     "split_reason": "claude -p reports only total API time",
                     "tokens": parsed["tokens"]}
        reasons["tools"] = "claude -p json reports no per-tool timing"
    else:
        reasons["model"] = reasons["tools"] = (
            f"driver {driver or '?'} writes no model or tool events")

    # other: the rest of the driver wall
    other: dict[str, Any] | None = None
    accounted = None
    if model is not None and wall is not None:
        accounted = model["s"] + (tools_union or 0.0)
        rest = wall - accounted
        other = {"s": _r(max(0.0, rest)),
                 "startup_shutdown_s": (_r(max(0.0, wall - span))
                                        if span is not None else None)}
        started_at = _num(record.get("driver_started_at"))
        if span is not None and started_at is not None and first_ts is not None:
            # Both are the system wall clock (time.time, Date.now); the
            # harness wall is monotonic. Clipped so head + tail <= the rest.
            head = min(max(0.0, first_ts / 1000 - started_at), max(0.0, wall - span))
            other["head_s"] = _r(head)
            other["tail_s"] = _r(max(0.0, wall - span - head))
        if daemon_ops and "hint" in daemon_ops:
            other["hint_waited_s"] = daemon_ops["hint"].get("waited_s")
            other["hint_daemon_s"] = daemon_ops["hint"]["s"]
        if rest < 0:
            other["clock_note"] = (
                f"model + tools ({accounted:.3f} s, driver clock) exceed the "
                f"harness wall ({wall:.3f} s, monotonic) by {-rest:.3f} s; "
                "other set to 0")
        if tools is None and driver == "claude":
            other["includes_tools"] = True
    elif wall is None:
        reasons["other"] = "no driver wall in the record"
    else:
        reasons["other"] = "model time unknown"

    if other is not None:
        per_task = _r(accounted + other["s"])
    else:
        per_task = _r(wall)
    pay_once = _r((setup["s"] or 0.0) + (index["cold_s"] or 0.0))
    return {"version": TIMING_VERSION, "driver": driver, "wall_s": _r(wall),
            "setup": setup, "index": index, "model": model, "tools": tools,
            "other": other, "check": check, "daemon_ops": daemon_ops,
            "per_task_s": per_task, "pay_once_s": pay_once, "reasons": reasons}


def _tools_block(by_name: dict[str, Any], union_s: float | None) -> dict[str, Any]:
    return {"s": _r(union_s),
            "sum_s": _sum_field(by_name, "s"),
            "ozagia_s": _sum_field(by_name, "ozagia_s"),
            "builtin_s": _sum_field(by_name, "builtin_s"),
            "daemon_overhead_s": _sum_field(by_name, "daemon_overhead_s"),
            "plugin_ipc_s": _sum_field(by_name, "plugin_ipc_s"),
            "matched": all(r.get("matched") for r in by_name.values()),
            "by_name": by_name}


# --------------------------------------------------------------------------- #
# per arm
# --------------------------------------------------------------------------- #
def _mean(values: Iterable[Any], digits: int = 3) -> float | None:
    known = [float(v) for v in values if v is not None]
    return round(sum(known) / len(known), digits) if known else None


def _get(timing: dict[str, Any] | None, *path: str) -> Any:
    cur: Any = timing
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def amortized(pay_once_s: float | None, per_task_s: float | None,
              n: int) -> float | None:
    """Time per task over `n` tasks on one revision: pay_once / n + per_task."""
    if per_task_s is None or n < 1:
        return None
    return round((pay_once_s or 0.0) / n + per_task_s, 3)


def summarize_arm(runs: list[dict[str, Any]], n: int,
                  warm_index_s: float | None = None) -> dict[str, Any]:
    """Means over one arm's runs (unknown values left out of each mean), the
    arm's per-task and amortized time, and per tool name means per run."""
    timings = [r.get("timing") for r in runs if isinstance(r.get("timing"), dict)]
    row: dict[str, Any] = {
        "timing_runs": len(timings),
        "breakdown_runs": sum(1 for t in timings if t.get("model") is not None),
        "mean_setup_s": _mean(_get(t, "setup", "s") for t in timings),
        "mean_index_s": _mean(_get(t, "index", "cold_s") for t in timings),
        "mean_model_s": _mean(_get(t, "model", "s") for t in timings),
        "mean_tools_s": _mean(_get(t, "tools", "s") for t in timings),
        "mean_tools_ozagia_s": _mean(_get(t, "tools", "ozagia_s") for t in timings),
        "mean_tools_builtin_s": _mean(_get(t, "tools", "builtin_s") for t in timings),
        "mean_daemon_overhead_s": _mean(_get(t, "tools", "daemon_overhead_s")
                                        for t in timings),
        "mean_other_s": _mean(_get(t, "other", "s") for t in timings),
        "mean_hint_s": _mean(_get(t, "other", "hint_waited_s") for t in timings),
        "mean_check_s": _mean(_get(t, "check", "s") for t in timings),
        "mean_input_uncached": _mean((_get(t, "model", "tokens", "input_uncached")
                                      for t in timings), 1),
        "mean_input_cached": _mean((_get(t, "model", "tokens", "input_cached")
                                    for t in timings), 1),
        "per_task_s": _mean(t.get("per_task_s") for t in timings),
        "amortize_n": n,
    }
    index_s = row["mean_index_s"] or 0.0
    row["warm_index_s"] = _r(warm_index_s) if index_s > 0 else None
    row["pay_once_s"] = (_r((row["mean_setup_s"] or 0.0) + index_s)
                         if timings else None)
    row["amortized_s"] = amortized(row["pay_once_s"], row["per_task_s"], n)
    tools: dict[str, dict[str, Any]] = {}
    with_tools = [t for t in timings if isinstance(_get(t, "tools", "by_name"), dict)]
    for t in with_tools:
        for name, rec in t["tools"]["by_name"].items():
            slot = tools.setdefault(name, {})
            for key in ("calls", "s", "ozagia_s", "builtin_s", "daemon_overhead_s",
                        "plugin_ipc_s"):
                slot.setdefault(key, []).append(rec.get(key))
    # A run that did not call a tool counts as 0 for it; a run whose split
    # of that tool is unknown (None) is left out of that field's mean.
    row["mean_tools_by_name"] = {
        name: {key: (round(sum(float(v) for v in vals if v is not None)
                           / (len(with_tools) - sum(v is None for v in vals)), 3)
                     if len(with_tools) > sum(v is None for v in vals) else None)
               for key, vals in slot.items()}
        for name, slot in sorted(tools.items())}
    return row


def warm_index(runs: list[dict[str, Any]]) -> float | None:
    """The warm re-index time measured in this run root (mean if several)."""
    return _mean(r.get("prep_warm_s") for r in runs)


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def headline(rows: list[dict[str, Any]]) -> list[str]:
    """Per-task and amortized time per arm, each against the first arm."""
    rows = [r for r in rows if r.get("per_task_s") is not None]
    if not rows:
        return []
    n = rows[0].get("amortize_n")
    ref = rows[0]

    def against(row: dict[str, Any], key: str) -> str:
        if row is ref or ref.get(key) is None or row.get(key) is None:
            return ""
        delta = row[key] - ref[key]
        return f" ({'+' if delta >= 0 else '-'}{abs(delta):.1f} s vs {ref['arm']})"

    per_task = ", ".join(f"{r['arm']} {_fmt(r['per_task_s'])} s{against(r, 'per_task_s')}"
                         for r in rows)
    amort = ", ".join(f"{r['arm']} {_fmt(r['amortized_s'])} s{against(r, 'amortized_s')}"
                      for r in rows)
    return [f"- Per task (agent time, no pay-once costs): {per_task}.",
            f"- Amortized over {n} task{'s' if n != 1 else ''} on one revision "
            f"(pay-once / {n} + per task): {amort}."]


def render_markdown_sections(rows: list[dict[str, Any]]) -> list[str]:
    """The components and per-tool tables for render_markdown."""
    rows = [r for r in rows if r.get("timing_runs")]
    if not rows:
        return []
    n = rows[0].get("amortize_n")
    lines = ["", "## Time per component", "",
             *headline(rows), "",
             "Mean seconds per run. Pay-once = setup + index (cold). "
             f"Amortized = pay-once / {n} + per task.", "",
             "| arm | runs | runs with breakdown | setup | index cold | index warm | "
             "model | tools | in ozagia | in built-in | daemon overhead | other | "
             "hint waited | per task | pay-once | amortized | check | "
             "uncached input tokens | cached input tokens |",
             "|---" * 19 + "|"]
    for r in rows:
        lines.append("| " + " | ".join(_fmt(v) for v in (
            r["arm"], r["timing_runs"], r["breakdown_runs"], r["mean_setup_s"],
            r["mean_index_s"], r["warm_index_s"], r["mean_model_s"], r["mean_tools_s"],
            r["mean_tools_ozagia_s"], r["mean_tools_builtin_s"],
            r["mean_daemon_overhead_s"], r["mean_other_s"], r["mean_hint_s"],
            r["per_task_s"], r["pay_once_s"], r["amortized_s"], r["mean_check_s"],
            r["mean_input_uncached"], r["mean_input_cached"])) + " |")
    lines += ["", "Model is the wait from one step's end to the next step's first "
              "tool call (prefill and generation together). Tools is the union of "
              "tool-call intervals; in ozagia, in built-in and daemon overhead are "
              "sums over calls, so with parallel calls they can exceed it. Other is "
              "the rest of the driver wall: start-up, the first request's wait, "
              "exit (and, for a run killed at its timeout, the last unanswered "
              "request). Hint waited is the time the plugin waited for the "
              f"daemon's task hint (at most {PLUGIN_HINT_TIMEOUT_S:g} s per hint; "
              "it lies in model or other). Check is not agent time. "
              "Blank cells are unknown. Definitions: docs/eval_agent.md."]
    tool_rows = [(r["arm"], name, t) for r in rows
                 for name, t in (r.get("mean_tools_by_name") or {}).items()]
    if tool_rows:
        lines += ["", "### Tools (mean per run)", "",
                  "| arm | tool | calls | driver s | in ozagia s | in built-in s | "
                  "daemon overhead s | plugin and IPC s |",
                  "|---|---|---|---|---|---|---|---|"]
        for arm, name, t in tool_rows:
            lines.append("| " + " | ".join(_fmt(v) for v in (
                arm, name, t.get("calls"), t.get("s"), t.get("ozagia_s"),
                t.get("builtin_s"), t.get("daemon_overhead_s"),
                t.get("plugin_ipc_s"))) + " |")
    return lines
