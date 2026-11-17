"""ozagia.eval.timing — per-component time of one eval run, from its logs.

Total wall-clock hides what a run spent its time on. A graph arm that saves
30 s of agent time and spends 35 s on a one-time index is not slower for a
second task on the same revision. This module splits a run into components
(docs/eval_agent.md, "Time per component"):

  setup   workdir creation and break patch, plugin install, daemon start.
          Per session: every eval task is its own session.
  index   prep (graph build and tag cache), cold: pay-once per revision.
          `repeat_s`, where measured, is what a later session on the same
          revision pays instead: snapshot load + refresh + tag cache.
  model   waiting for the model: per step, from the end of the previous
          step to the step's first tool call (or its finish when it calls
          no tool), plus the time from the step's last tool end to its
          finish. Prefill and generation together: no driver reports
          per-token timing. For graph_intent it holds the plugin's hint
          wait; `model_excl_hint_s` leaves it out.
  tools   the union of the tool calls' intervals as the driver saw them,
          and per tool name the time in the built-in, in ozagia (the
          daemon's handled `ms`), and the daemon's overhead on calls it
          passed through.
  other   driver wall minus model and tools, never negative: driver start
          and exit, the first request's wait before its first event.
          For a run killed at its timeout, the time after the last event
          (an unanswered request or a running tool) is `timeout_tail_s`.
  check   the task check. Not agent time.

`per_task_s` is model + tools + other + timeout tail (the driver wall, or
more when the driver's event clock runs past the harness timer).
`pay_once_s` is setup + index cold, what a first task pays on top of its
per-task time. Over N tasks on one revision, the amortized time per task is
(index cold + (N - 1) * index repeat) / N + setup + per task. Every function
here is pure over the files it reads and never raises on missing or
malformed logs: a component it cannot compute is None, with the reason in
`reasons`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

TIMING_VERSION = 2
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
# A driver call and its journal entries are a plausible pair only when the
# daemon's ms fit in the call (plus this much for clock differences) ...
PAIR_MS_TOLERANCE_S = 0.25
# ... and every journal `ts` (whole seconds) lies within the call, widened
# by this much on each side.
PAIR_TS_TOLERANCE_S = 1.0
# Logs and record must agree on when the driver started (same system clock).
START_TOLERANCE_S = 2.0


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
    Ocelot runs the median step_start-to-first-tool time was 3-4 ms in two
    log sets and 236 ms in a third, against seconds since the previous
    step_finish. A step's model time is therefore previous step end ->
    first tool start (or step finish), split into `pre_s` (gap before
    step_start) and `in_s` (step_start -> first tool), plus `post_s`, the
    step's last tool end -> its step_finish (the model's turn closing after
    the tool results are in). The first step has no observable start (the
    request goes out before any event), so its `pre_s` is 0 and that wait
    is in `other`. `tool_events` counts every tool_use event (as the
    harness's `tool_calls` does), `calls` only those with valid times.
    Returns {} when there is no step.
    """
    steps: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    calls: list[dict[str, Any]] = []
    stamps: list[float] = []
    tool_events = 0
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
            tool_events += 1
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
        # The step's last tool end -> its finish. Only for a finished step:
        # an unfinished one ends at its last event, which may be the tool.
        post_s = (max(0.0, step["finish"] - tool_end) / 1000
                  if step["finish"] is not None and tool_end is not None else 0.0)
        candidates = [x for x in (end, tool_end) if x is not None]
        if candidates:
            prev_end = max(candidates)
        per_step.append({
            "model_s": _r(pre_s + in_s + post_s), "pre_s": _r(pre_s), "in_s": _r(in_s),
            "post_s": _r(post_s),
            "tools": len(step["tools"]),
            "tools_s": _r(_union_s((c["start"], c["end"])
                                   for c in step["tools"]) / 1000),
            "finished": step["finish"] is not None,
            **(step["tokens"] or {})})
    tokens = {k: sum(int(s.get(k) or 0) for s in per_step)
              for k in ("input_uncached", "input_cached", "cache_write",
                        "output", "reasoning")}
    return {"steps": per_step, "calls": calls, "tokens": tokens,
            "tool_events": tool_events,
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
def journal_ts(value: Any) -> float | None:
    """A journal `ts` ("%Y-%m-%dT%H:%M:%S", the daemon host's local time, whole
    seconds) as epoch seconds in this process's local time, or None. A time
    zone that differs from the eval host's shows up as a whole-quarter-hour
    offset, which `journal_clock_offset` removes."""
    if not isinstance(value, str):
        return None
    try:
        return time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def _call(handled: Any, decision_ms: float, preflight: dict[str, Any] | None,
          intent: str | None, ts: float | None, seq: int) -> dict[str, Any]:
    stamps = [t for t in ((preflight or {}).get("ts"), ts) if t is not None]
    return {"handled": handled, "decision_ms": decision_ms,
            "preflight_ms": (preflight or {}).get("ms", 0.0), "after_ms": 0.0,
            "intent": intent, "refused": False, "ts": stamps,
            "seq": (preflight or {}).get("seq", seq)}


def _orphan(preflight: dict[str, Any]) -> dict[str, Any]:
    """A preflight that got no decision: a call refused at the permission
    step (OpenCode's "Permission denied"), counted as its own call."""
    out = _call(None, 0.0, preflight, "preflight_only", None, preflight["seq"])
    out["refused"] = True
    return out


def journal_calls(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Per tool name, the daemon's view of each call, in the order the calls
    started.

    One call of the plugin can write up to three entries: a `tool` entry
    with an intent ending in `_preflight` (the permission request before a
    mutating call), the deciding `tool` entry (`handled` true or false), and
    for a call left to the built-in an `after` entry (post-processing of
    the built-in's output, awaited inside the tool call). A decision takes
    the oldest waiting preflight of its tool, an `after` entry attaches to
    the latest decided call of its tool. A preflight that never gets a
    decision is a refused call and becomes a call of its own (`refused`):
    one still waiting when another preflight of the tool arrives more than
    PAIR_TS_TOLERANCE_S later (preflights within that window are parallel
    calls), or at the end of the journal. Each call carries its entries'
    `ts` (epoch s) for `tool_breakdown`'s checks. Other ops (hint,
    working_set, session_event, stats, ...) are totalled in `ops`; hints
    are also listed in `hints` ({ts, ms}) for `hint_in_model`."""
    by_tool: dict[str, list[dict[str, Any]]] = {}
    pending: dict[str, list[dict[str, Any]]] = {}
    ops: dict[str, dict[str, Any]] = {}
    hints: list[dict[str, Any]] = []
    for seq, e in enumerate(entries):
        op = str(e.get("op") or "?")
        tool = str(e.get("tool") or "?")
        ms = _num(e.get("ms")) or 0.0
        intent = str(e.get("intent") or "")
        ts = journal_ts(e.get("ts"))
        if op == "tool":
            queue = pending.setdefault(tool, [])
            if intent.endswith("_preflight"):
                last = queue[-1]["ts"] if queue else None
                if queue and not (last is not None and ts is not None
                                  and abs(ts - last) <= PAIR_TS_TOLERANCE_S):
                    by_tool.setdefault(tool, []).extend(_orphan(p) for p in queue)
                    queue.clear()
                queue.append({"ms": ms, "ts": ts, "seq": seq})
                continue
            pre = queue.pop(0) if queue else None
            by_tool.setdefault(tool, []).append(_call(
                e.get("handled") is True and not e.get("expired"), ms, pre,
                intent or None, ts, seq))
        elif op == "after" and any(not c["refused"] for c in by_tool.get(tool, [])):
            # Appended in journal order (orphans too, but those are skipped).
            target = [c for c in by_tool[tool] if not c["refused"]][-1]
            target["after_ms"] += ms
            if ts is not None:
                target["ts"].append(ts)
        else:
            slot = ops.setdefault(op, {"count": 0, "s": 0.0})
            slot["count"] += 1
            slot["s"] += ms / 1000
            if op == "hint":
                # The plugin stops waiting after PLUGIN_HINT_TIMEOUT_S; the
                # daemon may go on (and delay the next calls).
                slot["waited_s"] = (slot.get("waited_s", 0.0)
                                    + min(ms / 1000, PLUGIN_HINT_TIMEOUT_S))
                hints.append({"ts": ts, "ms": ms})
    for tool, queue in pending.items():
        by_tool.setdefault(tool, []).extend(_orphan(p) for p in queue)
    for calls in by_tool.values():
        calls.sort(key=lambda c: c["seq"])
    for slot in ops.values():
        slot["s"] = round(slot["s"], 3)
        if "waited_s" in slot:
            slot["waited_s"] = round(slot["waited_s"], 3)
    return {"by_tool": by_tool, "ops": dict(sorted(ops.items())), "hints": hints}


def journal_clock_offset(calls: list[dict[str, Any]],
                         journal: dict[str, Any] | None) -> float:
    """Seconds to add to journal `ts` to get the driver's clock: the median
    difference between call starts and their journal stamps, rounded to a
    whole quarter hour (0 when the report runs in the eval host's time
    zone; one or more hours when it does not). Pairs by order here, which
    is close enough for a quarter-hour rounding."""
    jt = (journal or {}).get("by_tool") or {}
    names: dict[str, list[dict[str, Any]]] = {}
    for c in sorted(calls, key=lambda c: c["start"]):
        names.setdefault(c["tool"], []).append(c)
    diffs = sorted(c["start"] / 1000 - d["ts"][0]
                   for name, mine in names.items()
                   for c, d in zip(mine, jt.get(name, [])) if d["ts"])
    if not diffs:
        return 0.0
    return round(diffs[len(diffs) // 2] / 900.0) * 900.0


def _pair_problem(call: dict[str, Any], d: dict[str, Any], offset: float) -> str | None:
    """Why a driver call and a journal call cannot be the same call, or None."""
    dur = (call["end"] - call["start"]) / 1000
    daemon_s = (d["decision_ms"] + d["preflight_ms"] + d["after_ms"]) / 1000
    if daemon_s > dur + PAIR_MS_TOLERANCE_S:
        return f"daemon {daemon_s:.2f} s in a {dur:.2f} s call"
    lo = int(call["start"] // 1000) - PAIR_TS_TOLERANCE_S
    hi = int(call["end"] // 1000) + PAIR_TS_TOLERANCE_S
    for ts in d["ts"]:
        t = ts + offset
        if not lo <= t <= hi:
            return (f"journal entry at {time.strftime('%H:%M:%S', time.localtime(t))} "
                    f"outside the call "
                    f"{time.strftime('%H:%M:%S', time.localtime(call['start'] / 1000))}"
                    f"-{time.strftime('%H:%M:%S', time.localtime(call['end'] / 1000))}")
    return None


def tool_breakdown(calls: list[dict[str, Any]], journal: dict[str, Any] | None,
                   *, daemon_expected: bool, offset: float = 0.0) -> dict[str, Any]:
    """Per tool name: calls, driver-seen seconds, and where they went.

    `calls` are the driver's tool calls ({tool, start, end} in ms). With a
    journal, the calls of a tool name are paired in start order with the
    daemon's calls of that name (`journal_calls`, refused preflights
    included). The pairing is trusted (`matched: true`) only when the
    counts agree and every pair is plausible: the daemon's ms fit in the
    call (+ PAIR_MS_TOLERANCE_S) and every journal `ts` (+ `offset`) lies
    within the call's seconds +- PAIR_TS_TOLERANCE_S. Otherwise
    `matched: false`, `mismatch` says why, and the split is by totals:
    ozagia = handled ms, daemon overhead = the rest of the daemon's ms,
    built-in = what is left (including plugin IPC). Without a journal and
    with no daemon expected (baseline, dev_memory), every non-ozagia tool's
    time is built-in time, and MCP tools named `ozagia...` are ozagia time.
    A refused call's time is built-in time (OpenCode's permission handling)
    apart from its preflight ms (daemon overhead).
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
                               "refused_calls": 0,
                               "ozagia_s": 0.0, "builtin_s": 0.0,
                               "daemon_overhead_s": 0.0, "plugin_ipc_s": 0.0,
                               "matched": True}
        decisions = jt.get(name, [])
        problem = None
        if decisions and len(decisions) != len(mine):
            refused = sum(1 for d in decisions if d["refused"])
            problem = (f"{len(mine)} driver calls, {len(decisions)} in the journal"
                       + (f" ({refused} refused)" if refused else ""))
        elif decisions:
            for i, (c, d) in enumerate(zip(mine, decisions), 1):
                why = _pair_problem(c, d, offset)
                if why:
                    problem = f"call {i}: {why}"
                    break
        if name.startswith(OZAGIA_TOOL_PREFIX) and not decisions:
            row["ozagia_s"] = row["s"]
        elif journal is None and daemon_expected:
            row.update(ozagia_s=None, builtin_s=None, daemon_overhead_s=None,
                       plugin_ipc_s=None, handled_calls=None,
                       passthrough_calls=None, refused_calls=None, matched=False,
                       mismatch="no daemon journal")
        elif not decisions:
            row["builtin_s"] = row["s"]
        elif problem is None:
            for dur, d in zip(durs, decisions):
                daemon_s = (d["decision_ms"] + d["preflight_ms"] + d["after_ms"]) / 1000
                if d["refused"]:
                    row["refused_calls"] += 1
                    row["daemon_overhead_s"] += daemon_s
                    row["builtin_s"] += max(0.0, dur - daemon_s)
                elif d["handled"]:
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
            handled = [d for d in decisions if d["handled"] is True]
            passed = [d for d in decisions if d["handled"] is not True]
            oz = sum(d["decision_ms"] + d["preflight_ms"] for d in handled) / 1000
            over = (sum(d["decision_ms"] + d["preflight_ms"] + d["after_ms"]
                        for d in passed)
                    + sum(d["after_ms"] for d in handled)) / 1000
            row.update(handled_calls=len(handled),
                       passthrough_calls=sum(1 for d in passed if not d["refused"]),
                       refused_calls=sum(1 for d in passed if d["refused"]),
                       ozagia_s=oz, daemon_overhead_s=over,
                       builtin_s=max(0.0, row["s"] - oz - over), plugin_ipc_s=None,
                       matched=False, mismatch=problem)
        out[name] = {k: (_r(v) if isinstance(v, float) else v) for k, v in row.items()}
    return out


def hint_in_model(hints: list[dict[str, Any]], first_ts_ms: float | None,
                  offset: float = 0.0) -> float:
    """Seconds of hint wait (min(ms, PLUGIN_HINT_TIMEOUT_S) per hint) that
    lie in model time: hints that started after the driver's first event.
    The plugin asks for a hint before each model request; the first one is
    before the first event, so its wait is in other. A hint's start is its
    `ts` (end, whole seconds, so + 0.5 s) minus its ms. Without stamps, every
    hint but the first counts."""
    total = 0.0
    for i, h in enumerate(hints):
        waited = min(h["ms"] / 1000, PLUGIN_HINT_TIMEOUT_S)
        if h["ts"] is None or first_ts_ms is None:
            if i > 0:
                total += waited
            continue
        if h["ts"] + offset + 0.5 - h["ms"] / 1000 > first_ts_ms / 1000:
            total += waited
    return total


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


def logs_mismatch(record: dict[str, Any], parsed: dict[str, Any]) -> str | None:
    """Why the parsed OpenCode logs are not this run's, or None. The record's
    `tool_calls` and `tokens_in` come from the same driver output when the
    run is made, and `driver_started_at` (newer runs) must precede the first
    event by less than the wall. A run root reused by a later run leaves
    other logs at the recorded path."""
    problems = []
    tools = record.get("tool_calls")
    if isinstance(tools, int) and not isinstance(tools, bool) \
            and tools != parsed.get("tool_events"):
        problems.append(f"{parsed.get('tool_events')} tool calls in the log, "
                        f"{tools} in the record")
    tokens = record.get("tokens_in")
    seen = (parsed.get("tokens") or {}).get("input_uncached")
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens != seen:
        problems.append(f"{seen} uncached input tokens in the log, {tokens} in the record")
    started = _num(record.get("driver_started_at"))
    first = parsed.get("first_ts")
    wall = _num(record.get("wall_s"))
    if started is not None and first is not None:
        lag = first / 1000 - started
        if lag < -START_TOLERANCE_S or (wall is not None
                                        and lag > wall + START_TOLERANCE_S):
            problems.append(f"first event {lag:+.1f} s from the recorded driver start")
    return "; ".join(problems) or None


def _run_timing(record: dict[str, Any], log_dir: Path | None) -> dict[str, Any]:
    reasons: dict[str, str] = {}
    arm = record.get("arm")
    driver = str(record.get("driver") or "")
    wall = _num(record.get("wall_s"))
    timed_out = record.get("timed_out") is True

    # setup: per session
    parts = {"workdir_s": _num(record.get("setup_workdir_s")),
             "plugin_install_s": _num(record.get("plugin_install_s")),
             "daemon_start_s": _num(record.get("daemon_ready_s"))}
    uses_daemon = record.get("daemon_ok") is not None
    expected = ["workdir_s"] + (["plugin_install_s", "daemon_start_s"]
                                if uses_daemon else [])
    missing = [k for k in expected if parts[k] is None]
    known = [parts[k] for k in expected if parts[k] is not None]
    setup = {"s": _r(sum(known)) if known else None,
             **{k: _r(v) for k, v in parts.items()}, "missing": missing,
             "partial": bool(known) and bool(missing)}
    if missing:
        reasons["setup"] = ("not recorded by this harness version: "
                            + ", ".join(missing)
                            + ("" if known else " (setup unknown)"))

    # index: cold per revision, repeat where measured
    prepped = arm in INDEX_ARMS and record.get("prep_ok") is not None
    if arm in INDEX_ARMS and not prepped:
        index: dict[str, Any] = {"cold_s": None, "repeat_s": None, "ok": None}
        reasons["index"] = "no prep recorded"
    else:
        index = {"cold_s": _r(_num(record.get("prep_s")) or 0.0) if prepped else 0.0,
                 "repeat_s": (_r(_num(record.get("prep_repeat_s"))) if prepped
                              else 0.0),
                 "ok": record.get("prep_ok") if prepped else None}
        if prepped and record.get("prep_repeat_parts"):
            index["repeat_parts"] = record["prep_repeat_parts"]
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
    hint_model_s: float | None = None
    mismatch: str | None = None
    stdout = _read_text(log_dir / "driver.stdout.txt") if log_dir else None
    if log_dir is None:
        reasons["model"] = reasons["tools"] = "no log dir for this run"
    elif driver == "opencode":
        parsed = opencode_events(stdout) if stdout is not None else {}
        mismatch = logs_mismatch(record, parsed) if parsed else None
        if not parsed:
            reasons["model"] = reasons["tools"] = (
                "driver.stdout.txt missing" if stdout is None
                else "no step events in driver.stdout.txt")
        elif mismatch:
            reasons["model"] = reasons["tools"] = (
                f"the logs in {log_dir} are not this run's: {mismatch}")
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
            offset = journal_clock_offset(parsed["calls"], journal)
            model = {"s": _r(sum(s["model_s"] for s in steps)),
                     "pre_step_s": _r(sum(s["pre_s"] for s in steps)),
                     "in_step_s": _r(sum(s["in_s"] for s in steps)),
                     "post_tool_s": _r(sum(s["post_s"] for s in steps)),
                     "steps": len(steps),
                     "split": "combined",
                     "split_reason": "opencode reports no per-token timing",
                     "tokens": parsed["tokens"], "per_step": steps}
            if journal is not None and journal["hints"]:
                hint_model_s = min(model["s"], hint_in_model(
                    journal["hints"], parsed["first_ts"], offset))
                model["hint_waited_s"] = _r(hint_model_s)
            tools_union = parsed["tools_union_s"]
            span = parsed["span_s"]
            first_ts = parsed["first_ts"]
            by_name = tool_breakdown(parsed["calls"], journal,
                                     daemon_expected=uses_daemon, offset=offset)
            tools = _tools_block(by_name, tools_union)
            if offset:
                tools["journal_offset_s"] = offset
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

    # other: the rest of the driver wall; for a timed-out run the time after
    # the last event is the timeout tail, not other.
    other: dict[str, Any] | None = None
    accounted = None
    tail_s: float | None = None
    if model is not None and wall is not None:
        accounted = model["s"] + (tools_union or 0.0)
        rest = wall - accounted
        other = {"s": None,
                 "startup_shutdown_s": (_r(max(0.0, wall - span))
                                        if span is not None else None)}
        started_at = _num(record.get("driver_started_at"))
        if span is not None and started_at is not None and first_ts is not None:
            # Both are the system wall clock (time.time, Date.now); the
            # harness wall is monotonic. Clipped so head + tail <= the rest.
            head = min(max(0.0, first_ts / 1000 - started_at), max(0.0, wall - span))
            other["head_s"] = _r(head)
            other["tail_s"] = _r(max(0.0, wall - span - head))
        tail_s = 0.0
        if timed_out:
            if other.get("tail_s") is not None:
                tail_s = other["tail_s"]
            elif span is not None:
                # No driver start recorded: the time outside the event span,
                # start-up included (a few seconds in the live runs).
                tail_s = max(0.0, wall - span)
                other["timeout_tail_includes_head"] = True
            tail_s = min(tail_s, max(0.0, rest))
        other["s"] = _r(max(0.0, rest) - tail_s)
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
        per_task = _r(accounted + other["s"] + (tail_s or 0.0))
    else:
        per_task = _r(wall)
    if setup["s"] is None or index["cold_s"] is None:
        pay_once = None
    else:
        pay_once = _r(setup["s"] + index["cold_s"])
    model_excl = (None if model is None
                  else _r(max(0.0, model["s"] - (hint_model_s or 0.0))))
    out = {"version": TIMING_VERSION, "driver": driver, "wall_s": _r(wall),
           "timed_out": timed_out,
           "setup": setup, "index": index, "model": model, "tools": tools,
           "other": other, "check": check, "daemon_ops": daemon_ops,
           "model_excl_hint_s": model_excl,
           "timeout_tail_s": _r(tail_s) if other is not None else None,
           "per_task_s": per_task, "pay_once_s": pay_once,
           "pay_once_partial": setup["partial"], "reasons": reasons}
    if mismatch:
        out["log_mismatch"] = mismatch
    return out


def _tools_block(by_name: dict[str, Any], union_s: float | None) -> dict[str, Any]:
    out = {"s": _r(union_s),
           "sum_s": _sum_field(by_name, "s"),
           "ozagia_s": _sum_field(by_name, "ozagia_s"),
           "builtin_s": _sum_field(by_name, "builtin_s"),
           "daemon_overhead_s": _sum_field(by_name, "daemon_overhead_s"),
           "plugin_ipc_s": _sum_field(by_name, "plugin_ipc_s"),
           "matched": all(r.get("matched") for r in by_name.values()),
           "by_name": by_name}
    mismatches = {k: r["mismatch"] for k, r in by_name.items() if r.get("mismatch")}
    if mismatches:
        out["mismatches"] = mismatches
    return out


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


def amortized(index_cold_s: float | None, index_repeat_s: float | None,
              setup_s: float | None, per_task_s: float | None,
              n: int) -> float | None:
    """Time per task over `n` tasks (sessions) on one revision:
    (index cold + (n - 1) * index repeat) / n + setup + per task. Setup is
    per session, so it is not divided. An unknown repeat counts as 0 (the
    caller says so); an unknown cold index, setup or per-task time makes
    the result unknown."""
    if None in (index_cold_s, setup_s, per_task_s) or n < 1:
        return None
    index = index_cold_s + (n - 1) * (index_repeat_s or 0.0)
    return round(index / n + setup_s + per_task_s, 3)


def summarize_arm(runs: list[dict[str, Any]], n: int,
                  index_repeat_s: float | None = None) -> dict[str, Any]:
    """Means over one arm's runs (unknown values left out of each mean), the
    arm's per-task and amortized time, and per tool name means per run.
    `index_repeat_s` is the run root's repeat index cost (`repeat_index`)."""
    timings = [r.get("timing") for r in runs if isinstance(r.get("timing"), dict)]
    setups = [_get(t, "setup", "s") for t in timings]
    known_setup = [v for v in setups if v is not None]
    row: dict[str, Any] = {
        "timing_runs": len(timings),
        "breakdown_runs": sum(1 for t in timings if t.get("model") is not None),
        "timed_out_runs": sum(1 for t in timings if t.get("timed_out")),
        "mean_setup_s": _mean(known_setup),
        "mean_index_s": _mean(_get(t, "index", "cold_s") for t in timings),
        "mean_model_s": _mean(_get(t, "model", "s") for t in timings),
        "mean_model_excl_hint_s": _mean(t.get("model_excl_hint_s") for t in timings),
        "mean_hint_in_model_s": _mean(_get(t, "model", "hint_waited_s")
                                      for t in timings),
        "mean_tools_s": _mean(_get(t, "tools", "s") for t in timings),
        "mean_tools_ozagia_s": _mean(_get(t, "tools", "ozagia_s") for t in timings),
        "mean_tools_builtin_s": _mean(_get(t, "tools", "builtin_s") for t in timings),
        "mean_daemon_overhead_s": _mean(_get(t, "tools", "daemon_overhead_s")
                                        for t in timings),
        "mean_other_s": _mean(_get(t, "other", "s") for t in timings),
        "mean_timeout_tail_s": _mean(t.get("timeout_tail_s") for t in timings),
        "timeout_tail_with_head_runs": sum(
            1 for t in timings if _get(t, "other", "timeout_tail_includes_head")),
        "mean_hint_s": _mean(_get(t, "other", "hint_waited_s") for t in timings),
        "mean_check_s": _mean(_get(t, "check", "s") for t in timings),
        "mean_input_uncached": _mean((_get(t, "model", "tokens", "input_uncached")
                                      for t in timings), 1),
        "mean_input_cached": _mean((_get(t, "model", "tokens", "input_cached")
                                    for t in timings), 1),
        "per_task_s": _mean(t.get("per_task_s") for t in timings),
        "unmatched_tool_runs": sum(1 for t in timings
                                   if _get(t, "tools", "matched") is False),
        "amortize_n": n,
    }
    # Setup is partial when a run lacks some of its parts, or some runs
    # lack all of them; unknown when no run has any.
    row["setup_partial"] = bool(known_setup) and (
        len(known_setup) < len(timings)
        or any(_get(t, "setup", "partial") for t in timings))
    row["setup_missing"] = sorted({m for t in timings
                                   for m in (_get(t, "setup", "missing") or [])})
    index_s = row["mean_index_s"]
    indexes = bool(index_s)
    row["index_repeat_s"] = _r(index_repeat_s) if indexes else None
    row["repeat_assumed_zero"] = indexes and index_repeat_s is None and n > 1
    row["pay_once_s"] = (None if row["mean_setup_s"] is None or index_s is None
                         else _r(row["mean_setup_s"] + index_s))
    row["amortized_s"] = amortized(index_s, row["index_repeat_s"],
                                   row["mean_setup_s"], row["per_task_s"], n)
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


def repeat_index(runs: list[dict[str, Any]]) -> float | None:
    """The repeat index cost measured in this run root (`prep_repeat_s`,
    mean if several), or None."""
    return _mean(r.get("prep_repeat_s") for r in runs)


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _has_hints(row: dict[str, Any]) -> bool:
    return row.get("mean_hint_in_model_s") is not None


HEADLINE_KEYS = (
    "per_task_s", "amortized_s", "amortize_n", "timing_runs", "timed_out_runs",
    "timeout_tail_with_head_runs", "mean_setup_s", "setup_partial",
    "setup_missing", "repeat_assumed_zero", "mean_model_s",
    "mean_model_excl_hint_s", "mean_hint_in_model_s")


def headline_view(row: dict[str, Any]) -> dict[str, Any]:
    """The fields of a `summarize_arm` row that `headline` reads."""
    return {k: row.get(k) for k in HEADLINE_KEYS}


def _paired_view(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str, int, bool]:
    """(the rows the headline shows, the label of the runs they cover, the
    number of cells valid in every arm when the headline uses them,
    whether arms are compared). Arms are compared over the paired cells
    (`paired_timing`) when some arm has valid runs outside them; when no
    such cell has a per-task time in every arm they are shown over their
    own valid runs, without deltas."""
    if not all(isinstance(r.get("paired_timing"), dict) for r in rows) or \
            all(r.get("paired_runs") == r.get("valid_runs") for r in rows):
        return rows, "", 0, True
    cells = int(rows[0].get("paired_runs") or 0)
    if not cells or any(r["paired_timing"].get("per_task_s") is None for r in rows):
        return rows, (", over each arm's own valid runs (no (task, repeat) cell "
                      "valid in every arm has a per-task time in every arm, so "
                      "arms are not compared)"), 0, False
    label = (f", over the {cells} (task, repeat) cell{'s' if cells != 1 else ''} "
             "valid in every arm")
    return [{**r["paired_timing"], "arm": r["arm"]} for r in rows], label, cells, True


def headline(rows: list[dict[str, Any]]) -> list[str]:
    """Per-task, model and amortized time per arm, each against the first
    arm, with footnotes on what is unknown, partial or assumed. When arms
    lose different runs to invalid checks, the figures and deltas cover
    the (task, repeat) cells valid in every arm (`paired_timing`), and
    each arm's own valid runs and every run are given beside them."""
    rows = [r for r in rows if r.get("per_task_s") is not None]
    if not rows:
        return []
    n = rows[0].get("amortize_n")
    view, over, cells, compare = _paired_view(rows)
    ref = view[0]

    def against(row: dict[str, Any], key: str) -> str:
        if not compare or row is ref or ref.get(key) is None or row.get(key) is None:
            return ""
        delta = row[key] - ref[key]
        return f" ({'+' if delta >= 0 else '-'}{abs(delta):.1f} s vs {ref['arm']})"

    per_task = ", ".join(f"{r['arm']} {_fmt(r['per_task_s'])} s{against(r, 'per_task_s')}"
                         for r in view)
    lines = [f"- Per task (agent time, without setup and index){over}: {per_task}."]
    again = (", over the same cells" if cells
             else ", over each arm's own valid runs" if not compare else "")
    excluded = [f"{r['timing_invalid_excluded']} {r['arm']}" for r in rows
                if r.get("timing_invalid_excluded")]
    every = ", ".join(f"{r['arm']} {_fmt(r.get('per_task_all_runs_s'))} s"
                      for r in rows)
    if cells:
        own = ", ".join(f"{r['arm']} {_fmt(r['per_task_s'])} s" for r in rows)
        lines.append(f"  - Over each arm's own valid runs (as success rate): {own}"
                     + (f"; left out {', '.join(excluded)} invalid run(s)"
                        if excluded else "")
                     + f". Per task over every run: {every}.")
    elif excluded:
        lines.append(f"  - Timing, like success, covers valid runs only: left out "
                     f"{', '.join(excluded)} invalid run(s). Per task over every "
                     f"run: {every}.")
    timed = [f"{r['timed_out_runs']} of {r['timing_runs']} {r['arm']}"
             for r in view if r.get("timed_out_runs")]
    if timed:
        with_head = sum(r.get("timeout_tail_with_head_runs") or 0 for r in view)
        lines.append(f"  - Runs killed at the timeout: {', '.join(timed)}; their "
                     "per-task time is the timeout, and the time after their last "
                     "event is the timeout tail"
                     + (f" (for {with_head} without a recorded driver start, the "
                        "tail also holds start-up and the first request)"
                        if with_head else "") + ".")
    if any(_has_hints(r) for r in view):
        parts = []
        for r in view:
            if _has_hints(r):
                parts.append(f"{r['arm']} {_fmt(r['mean_model_excl_hint_s'])} s "
                             f"without its {_fmt(r['mean_hint_in_model_s'])} s of "
                             "hint wait")
            elif r.get("mean_model_s") is not None:
                parts.append(f"{r['arm']} {_fmt(r['mean_model_s'])} s")
        lines.append(f"- Model time per task{again}: {', '.join(parts)}. The hint wait is "
                     "the plugin waiting for the daemon's task hint before a model "
                     "request; it is in the per-task time above.")

    def amort(r: dict[str, Any]) -> str:
        if r.get("amortized_s") is None:
            return f"{r['arm']} unknown"
        mark = "*" if r.get("setup_partial") else ""
        return f"{r['arm']} {_fmt(r['amortized_s'])} s{mark}{against(r, 'amortized_s')}"

    lines.append(f"- Amortized over {n} task{'s' if n != 1 else ''} on one revision "
                 f"((index cold + {n - 1 if n else 'N-1'} × index repeat) / {n} + "
                 f"setup + per task){again}: {', '.join(amort(r) for r in view)}.")
    for r in view:
        if r.get("mean_setup_s") is None and r.get("per_task_s") is not None:
            lines.append(f"  - {r['arm']}: setup not recorded by this harness "
                         "version, so pay-once and amortized are unknown.")
        elif r.get("setup_partial"):
            lines.append(f"  - * {r['arm']}: setup only partly recorded (missing "
                         f"{', '.join(r.get('setup_missing') or []) or 'some runs'}); "
                         "a lower bound.")
        if r.get("repeat_assumed_zero"):
            lines.append(f"  - {r['arm']}: index repeat cost not measured in this "
                         "run, counted as 0, so its amortized time is a lower bound.")
    return lines


def render_markdown_sections(rows: list[dict[str, Any]]) -> list[str]:
    """The components and per-tool tables for render_markdown (the headline
    is in the report's header list)."""
    rows = [r for r in rows if r.get("timing_runs")]
    if not rows:
        return []
    n = rows[0].get("amortize_n")

    def marked(r: dict[str, Any], key: str) -> str:
        text = _fmt(r.get(key))
        return f"{text}*" if text and r.get("setup_partial") else text

    lines = ["", "## Time per component", "",
             "Mean seconds per run. Setup is per session (every task is a session), "
             "index cold per revision, index repeat what a later session on the same "
             "revision pays instead (snapshot load + refresh + tag cache). "
             "Pay-once = setup + index cold. Amortized = (index cold + "
             f"{n - 1} × index repeat) / {n} + setup + per task.", "",
             "Each arm's own valid runs, the runs of its success rate (see Invalid "
             "runs); the headline compares arms over the (task, repeat) cells valid "
             "in every arm.", "",
             "| arm | runs | runs with breakdown | timed out | setup | index cold | "
             "index repeat | model | model without hint | tools | in ozagia | "
             "in built-in | daemon overhead | other | timeout tail | hint waited | "
             "per task | pay-once | amortized | check | uncached input tokens | "
             "cached input tokens |",
             "|---" * 22 + "|"]
    for r in rows:
        excl = r["mean_model_excl_hint_s"] if _has_hints(r) else None
        lines.append("| " + " | ".join((
            str(r["arm"]), _fmt(r["timing_runs"]), _fmt(r["breakdown_runs"]),
            _fmt(r["timed_out_runs"]), marked(r, "mean_setup_s"),
            _fmt(r["mean_index_s"]), _fmt(r["index_repeat_s"]),
            _fmt(r["mean_model_s"]), _fmt(excl), _fmt(r["mean_tools_s"]),
            _fmt(r["mean_tools_ozagia_s"]), _fmt(r["mean_tools_builtin_s"]),
            _fmt(r["mean_daemon_overhead_s"]), _fmt(r["mean_other_s"]),
            _fmt(r["mean_timeout_tail_s"]), _fmt(r["mean_hint_s"]),
            _fmt(r["per_task_s"]), marked(r, "pay_once_s"), marked(r, "amortized_s"),
            _fmt(r["mean_check_s"]), _fmt(r["mean_input_uncached"]),
            _fmt(r["mean_input_cached"]))) + " |")
    unmatched = [f"{r['arm']} {r['unmatched_tool_runs']}" for r in rows
                 if r.get("unmatched_tool_runs")]
    lines += ["", "Model is the wait from one step's end to the next step's first "
              "tool call, plus a step's last tool end to its finish (prefill and "
              "generation together). Model without hint leaves out the plugin's "
              "hint wait that falls in model time (arms with the plugin only). "
              "Tools is the union of tool-call intervals; in ozagia, in built-in "
              "and daemon overhead are sums over calls, so with parallel calls they "
              "can exceed it. Other is the rest of the driver wall: start-up, the "
              "first request's wait and exit. Timeout tail is, for a run killed at "
              "its timeout, the time after its last event (an unanswered request "
              "or a running tool). Hint waited is the time the plugin waited for "
              "the daemon's task hint (at most "
              f"{PLUGIN_HINT_TIMEOUT_S:g} s per hint; it lies in model or other). "
              "Check is not agent time. * marks a partial setup sum (a lower "
              "bound). Blank cells are unknown. Definitions: docs/eval_agent.md."]
    if unmatched:
        lines += ["", "Runs whose driver calls and journal entries could not be "
                  f"paired one to one (tool split by totals): {', '.join(unmatched)}."]
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
