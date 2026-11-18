"""Turn a canonical profile into ranked, graph-linked measurements.

The analysis is descriptive.  It reports the artifact's measured self and
inclusive values, sample weights or span durations, joins active frames to the
repository graph, and ranks them.  It does not infer a cause, claim a
regression is bad in isolation, or turn a missing frame into zero.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

from .graph import GraphIndex
from .model import bounded_int, bounded_text, number_text

MAX_JOIN_FRAMES = 20_000
MAX_METRIC_JOIN = 5_000
MAX_TOP = 200
MAX_PATHS = 20
MAX_PATH_STEPS = 16
SORTS = ("self", "inclusive", "calls", "impact")


def _pct(values: Iterable[int], percentile: int) -> int:
    ordered = sorted(int(value) for value in values)
    if not ordered:
        return 0
    index = max(0, min(len(ordered) - 1,
                       math.ceil((percentile / 100.0) * len(ordered)) - 1))
    return ordered[index]


def _label(frame: Mapping[str, Any] | None, *, limit: int = 240) -> str:
    if not frame:
        return "(unknown)"
    name = str(frame.get("name") or "(unknown)")
    file = frame.get("file")
    line = frame.get("line")
    if isinstance(file, str) and file:
        location = f"{file}:{line}" if line else file
        return bounded_text(f"{name} ({location})", name="label", limit=limit)
    return bounded_text(name, name="label", limit=limit)


def _key(row: Mapping[str, Any]) -> str:
    symbol = row.get("symbol") or (row.get("graph") or {}).get("symbol")
    if isinstance(symbol, str) and symbol:
        return symbol
    file = row.get("file") or ""
    name = row.get("name") or ""
    return f"{file}::{name}"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _histogram_quantile(buckets: list[Mapping[str, Any]],
                        quantile: float) -> str | None:
    parsed = []
    total = 0
    for bucket in buckets:
        le = str(bucket.get("le") or "")
        try:
            count = int(bucket.get("count") or 0)
        except (TypeError, ValueError):
            continue
        total = max(total, count)
        if le == "+Inf":
            continue
        bound = _number(le)
        if bound is not None:
            parsed.append((bound, count, le))
    if total <= 0:
        return None
    parsed.sort(key=lambda item: item[0])
    target = quantile * total
    for _bound, count, text in parsed:
        if count >= target:
            return text
    return "+Inf"


def _summary_quantile(quantiles: list[Mapping[str, Any]],
                      quantile: float) -> str | None:
    best: tuple[float, str] | None = None
    for item in quantiles:
        q = _number(item.get("q"))
        value = item.get("value")
        if q is None or value in (None, ""):
            continue
        distance = abs(q - quantile)
        if best is None or distance < best[0]:
            best = (distance, str(value))
    return best[1] if best is not None else None


def _sort_key(sort: str):
    if sort == "inclusive":
        return lambda row: (-int(row["inclusive"]), -int(row["self"]), row["key"])
    if sort == "calls":
        return lambda row: (-int(row.get("calls") if row.get("calls") is not None
                                  else row.get("hits") or 0),
                            -int(row["self"]), row["key"])
    if sort == "impact":
        return lambda row: (-int((row.get("graph") or {}).get("impact_symbols") or 0),
                            -int(row["self"]), row["key"])
    return lambda row: (-int(row["self"]), -int(row["inclusive"]), row["key"])


def _span_self(spans: list[Mapping[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    by_id = {str(span["id"]): span for span in spans}
    child_sum: dict[str, int] = defaultdict(int)
    for span in spans:
        parent = span.get("parent")
        if isinstance(parent, str) and parent in by_id:
            child_sum[parent] += int(span.get("duration_us") or 0)
    self_us: dict[str, int] = defaultdict(int)
    for span in spans:
        sid = str(span["id"])
        duration = int(span.get("duration_us") or 0)
        self_us[sid] = max(0, duration - child_sum.get(sid, 0))
    return self_us, child_sum


def analyze(profile: Mapping[str, Any], graph: GraphIndex | None = None, *,
            top: int = 25, sort: str = "self",
            baseline: Mapping[str, Any] | None = None) -> dict[str, Any]:
    top = bounded_int(top, name="top", low=1, high=MAX_TOP, default=25) or 25
    if sort not in SORTS:
        sort = "self"
    frames = [frame for frame in profile.get("frames") or []
              if isinstance(frame, Mapping) and isinstance(frame.get("id"), str)]
    frame_map = {str(frame["id"]): frame for frame in frames}
    functions = [item for item in profile.get("functions") or []
                 if isinstance(item, Mapping) and item.get("frame") in frame_map]
    samples = [item for item in profile.get("samples") or []
               if isinstance(item, Mapping)]
    spans = [item for item in profile.get("spans") or []
             if isinstance(item, Mapping) and item.get("id") is not None]
    metrics = [item for item in profile.get("metrics") or []
               if isinstance(item, Mapping)]
    edges = [item for item in profile.get("edges") or [] if isinstance(item, Mapping)]

    if functions:
        unit = "microseconds"
        kind = "functions"
    elif spans and not samples:
        unit = "microseconds"
        kind = "spans"
    elif samples:
        unit = str(profile.get("sample_unit") or "samples")
        kind = "samples"
    elif metrics:
        unit = "metrics"
        kind = "metrics"
    else:
        unit = "unknown"
        kind = "empty"

    self_value: dict[str, int] = defaultdict(int)
    inclusive: dict[str, int] = defaultdict(int)
    calls: dict[str, int] = defaultdict(int)
    primitive_calls: dict[str, int] = defaultdict(int)
    hits: dict[str, int] = defaultdict(int)
    total_self = 0
    idle_us = int((profile.get("totals") or {}).get("idle_us") or 0)

    if functions:
        for item in functions:
            fid = str(item["frame"])
            self_value[fid] += int(item.get("self_us") or 0)
            inclusive[fid] += int(item.get("cumulative_us") or 0)
            calls[fid] += int(item.get("calls") or 0)
            primitive_calls[fid] += int(item.get("primitive_calls") or 0)
        total_self = sum(self_value.values())
    elif spans and not samples:
        span_self, _children = _span_self(spans)
        id_to_frame = {str(span["id"]): span.get("frame") for span in spans}
        for sid, value in span_self.items():
            fid = id_to_frame.get(sid)
            if not isinstance(fid, str):
                continue
            self_value[fid] += value
            calls[fid] += 1
        for span in spans:
            fid = span.get("frame")
            if isinstance(fid, str):
                inclusive[fid] += int(span.get("duration_us") or 0)
        total_self = sum(self_value.values())
    elif samples:
        for item in samples:
            stack = [fid for fid in item.get("stack") or []
                     if isinstance(fid, str) and fid in frame_map]
            if not stack:
                continue
            weight = int(item.get("weight") or 0)
            if weight <= 0:
                continue
            total_self += weight
            leaf = stack[-1]
            self_value[leaf] += weight
            hits[leaf] += 1
            for fid in set(stack):
                inclusive[fid] += weight
                hits[fid] += 1

    active = [fid for fid in frame_map
              if self_value.get(fid) or inclusive.get(fid) or calls.get(fid)]
    active.sort(key=lambda fid: (-self_value.get(fid, 0),
                                 -inclusive.get(fid, 0), fid))
    join_ids = set(active[:MAX_JOIN_FRAMES])
    resolutions: dict[str, dict[str, Any]] = {}
    for fid in join_ids:
        if graph is not None:
            resolutions[fid] = graph.resolve(frame_map[fid])
        else:
            resolutions[fid] = {"status": "not_requested",
                                "reason": "graph join disabled"}

    # Caller/impact traversal is useful for the rows a person will read; doing
    # it for every frame in a very large profile adds latency without changing
    # the top list. The cap is reported through graph_stats=None.
    stats_ids = set(active[: min(len(active), max(top * 8, 500))])
    rows: list[dict[str, Any]] = []
    for fid in active:
        frame = frame_map[fid]
        resolution = resolutions.get(fid)
        if resolution is None:
            resolution = {"status": "not_assessed",
                          "reason": f"join limited to the first {MAX_JOIN_FRAMES} frames"}
        symbol = resolution.get("symbol")
        graph_stats = None
        if graph is not None and isinstance(symbol, str) and fid in stats_ids:
            graph_stats = graph.symbol_stats(symbol)
        row = {
            "frame": fid,
            "name": frame.get("name"),
            "file": frame.get("file"),
            "line": frame.get("line"),
            "module": frame.get("module"),
            "kind": frame.get("kind"),
            "external": bool(frame.get("external")),
            "self": int(self_value.get(fid, 0)),
            "inclusive": int(inclusive.get(fid, 0)),
            "calls": int(calls.get(fid, 0)) if functions or spans else None,
            "primitive_calls": int(primitive_calls.get(fid, 0)) if functions else None,
            "hits": int(hits.get(fid, 0)) if samples else None,
            "share_bp": (int(self_value.get(fid, 0)) * 10_000 // total_self
                         if total_self else 0),
            "graph": {
                "status": resolution.get("status"),
                "symbol": symbol,
                "reason": resolution.get("reason"),
                "stats_limited": bool(graph is not None and isinstance(symbol, str)
                                      and fid not in stats_ids),
                **(graph_stats or {}),
            },
        }
        row["key"] = _key(row)
        rows.append(row)

    rows.sort(key=_sort_key(sort))
    hotspots = rows[:top]

    files: dict[str, dict[str, Any]] = {}
    modules: dict[str, dict[str, Any]] = {}
    for row in rows:
        file = row.get("file") or "<unknown>"
        entry = files.setdefault(file, {"file": file, "self": 0, "inclusive": 0,
                                        "frames": 0, "mapped": 0,
                                        "external": bool(row.get("external"))})
        entry["self"] += row["self"]
        entry["inclusive"] += row["inclusive"]
        entry["frames"] += 1
        entry["mapped"] += 1 if (row.get("graph") or {}).get("status") == "mapped" else 0
        module = row.get("module") or "<unknown>"
        module_entry = modules.setdefault(module, {"module": module, "self": 0,
                                                   "inclusive": 0, "frames": 0,
                                                   "mapped": 0})
        module_entry["self"] += row["self"]
        module_entry["inclusive"] += row["inclusive"]
        module_entry["frames"] += 1
        module_entry["mapped"] += 1 if (row.get("graph") or {}).get("status") == "mapped" else 0
    for table in (files, modules):
        for entry in table.values():
            entry["self_share_bp"] = (entry["self"] * 10_000 // total_self
                                      if total_self else 0)
    file_rows = sorted(files.values(), key=lambda item: (-item["self"], item["file"]))[:top]
    module_rows = sorted(modules.values(),
                         key=lambda item: (-item["self"], item["module"]))[:top]

    paths: list[dict[str, Any]] = []
    if samples:
        counts: Counter[tuple[str, ...]] = Counter()
        weights: dict[tuple[str, ...], int] = {}
        for item in samples:
            stack = tuple(str(fid) for fid in item.get("stack") or []
                          if isinstance(fid, str) and fid in frame_map)
            if not stack:
                continue
            counts[stack] += 1
            weights[stack] = weights.get(stack, 0) + int(item.get("weight") or 0)
        ranked = sorted(weights, key=lambda stack: (-weights[stack], stack))
        for stack in ranked[:MAX_PATHS]:
            shown = list(stack[-MAX_PATH_STEPS:])
            omitted = max(0, len(stack) - len(shown))
            paths.append({
                "stack": [_label(frame_map[fid]) for fid in shown],
                "omitted_frames": omitted,
                "weight": weights[stack],
                "occurrences": counts[stack],
                "unit": unit,
            })
    elif functions:
        pairs: dict[tuple[str, str], int] = defaultdict(int)
        for edge in edges:
            caller, callee = edge.get("caller"), edge.get("callee")
            if caller in frame_map and callee in frame_map:
                pairs[(str(caller), str(callee))] += int(edge.get("calls") or 0)
        for (caller, callee), count in sorted(
                pairs.items(), key=lambda item: (-item[1], item[0]))[:MAX_PATHS]:
            paths.append({
                "stack": [_label(frame_map[caller]), _label(frame_map[callee])],
                "omitted_frames": 0,
                "weight": count,
                "occurrences": count,
                "unit": "calls",
            })
    elif spans:
        pairs: dict[tuple[str, str], int] = defaultdict(int)
        counts: dict[tuple[str, str], int] = defaultdict(int)
        by_id = {str(span["id"]): span for span in spans}
        for span in spans:
            parent = span.get("parent")
            if isinstance(parent, str) and parent in by_id:
                parent_frame = by_id[parent].get("frame")
                child_frame = span.get("frame")
                if isinstance(parent_frame, str) and isinstance(child_frame, str):
                    key = (parent_frame, child_frame)
                    pairs[key] += int(span.get("duration_us") or 0)
                    counts[key] += 1
        for (parent, child), duration in sorted(
                pairs.items(), key=lambda item: (-item[1], item[0]))[:MAX_PATHS]:
            paths.append({
                "stack": [_label(frame_map.get(parent)), _label(frame_map.get(child))],
                "omitted_frames": 0,
                "weight": duration,
                "occurrences": counts[(parent, child)],
                "unit": "microseconds",
            })

    telemetry = None
    if spans:
        operations: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        statuses: Counter[str] = Counter()
        services: set[str] = set()
        symbol_by_frame = {
            row["frame"]: (row.get("graph") or {}).get("symbol")
            for row in rows
            if (row.get("graph") or {}).get("symbol")
        }
        for span in spans:
            service = str(span.get("service") or "unknown")
            services.add(service)
            fid = span.get("frame")
            frame = frame_map.get(fid) if isinstance(fid, str) else None
            key = (service, str(span.get("name") or "(unnamed span)"),
                   fid if isinstance(fid, str) else None)
            entry = operations.setdefault(key, {
                "service": service,
                "operation": key[1],
                "frame": fid if isinstance(fid, str) else None,
                "symbol": symbol_by_frame.get(fid) if isinstance(fid, str) else None,
                "kind": span.get("kind"),
                "count": 0,
                "total_us": 0,
                "errors": 0,
                "durations": [],
            })
            entry["count"] += 1
            entry["total_us"] += int(span.get("duration_us") or 0)
            entry["durations"].append(int(span.get("duration_us") or 0))
            status = str(span.get("status") or "unknown")
            statuses[status] += 1
            if status == "error":
                entry["errors"] += 1
        operation_rows = []
        for entry in operations.values():
            durations = entry.pop("durations")
            entry["p50_us"] = _pct(durations, 50)
            entry["p95_us"] = _pct(durations, 95)
            operation_rows.append(entry)
        operation_rows.sort(key=lambda item: (-item["total_us"], item["service"],
                                              item["operation"]))
        telemetry = {
            "spans": len(spans),
            "services": sorted(services),
            "status": dict(sorted(statuses.items())),
            "operations": operation_rows[:top],
            "operations_truncated": len(operation_rows) > top,
            "total_us": sum(int(span.get("duration_us") or 0) for span in spans),
            "error_spans": statuses.get("error", 0),
        }

    metric_report = None
    if metrics:
        grouped: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
        for metric in metrics:
            labels = metric.get("labels") if isinstance(metric.get("labels"), Mapping) else {}
            key = (str(metric.get("name") or "(unnamed metric)"),
                   str(metric.get("kind") or "unknown"),
                   str(metric.get("unit") or ""),
                   str(metric.get("service") or "unknown"),
                   json.dumps(labels, sort_keys=True))
            grouped.setdefault(key, []).append(metric)
        metric_rows: list[dict[str, Any]] = []
        mapped_metrics = 0
        for group_index, (key, points) in enumerate(grouped.items()):
            points.sort(key=lambda item: (item.get("end_us") or 0,
                                          item.get("start_us") or 0))
            latest = points[-1]
            name, metric_kind, unit_text, service, labels_text = key
            labels = dict(latest.get("labels") or {})
            fid = latest.get("frame")
            resolution = {"status": "unresolved",
                          "reason": "metric has no code location"}
            graph_stats = None
            if isinstance(fid, str) and graph is not None:
                if group_index < MAX_METRIC_JOIN:
                    resolution = graph.resolve(frame_map.get(fid, {"id": fid}))
                    if resolution.get("status") == "mapped" and resolution.get("symbol"):
                        graph_stats = graph.symbol_stats(resolution["symbol"])
                else:
                    resolution = {
                        "status": "not_assessed",
                        "reason": f"metric join limited to {MAX_METRIC_JOIN} series"}
            elif isinstance(fid, str):
                resolution = {"status": "not_requested",
                              "reason": "graph join disabled"}
            if resolution.get("status") == "mapped":
                mapped_metrics += 1
            buckets = [item for item in latest.get("buckets") or []
                       if isinstance(item, Mapping)]
            quantiles = [item for item in latest.get("quantiles") or []
                         if isinstance(item, Mapping)]
            row = {
                "name": name,
                "kind": metric_kind,
                "unit": unit_text,
                "service": service,
                "labels": labels,
                "points": len(points),
                "value": latest.get("value"),
                "count": latest.get("count"),
                "sum": latest.get("sum"),
                "frame": fid if isinstance(fid, str) else None,
                "graph": {
                    "status": resolution.get("status"),
                    "symbol": resolution.get("symbol"),
                    "reason": resolution.get("reason"),
                    **({} if graph_stats is None else graph_stats),
                },
            }
            if metric_kind == "histogram":
                row["p50"] = _histogram_quantile(buckets, 0.50)
                row["p95"] = _histogram_quantile(buckets, 0.95)
                row["p99"] = _histogram_quantile(buckets, 0.99)
                row["buckets"] = buckets
            elif metric_kind == "summary":
                row["p50"] = _summary_quantile(quantiles, 0.50)
                row["p95"] = _summary_quantile(quantiles, 0.95)
                row["p99"] = _summary_quantile(quantiles, 0.99)
                row["quantiles"] = quantiles
            elif metric_kind == "counter" and len(points) > 1:
                first = _number(points[0].get("value"))
                last = _number(points[-1].get("value"))
                if first is not None and last is not None:
                    row["delta"] = number_text(last - first)
            metric_rows.append(row)
        metric_rows.sort(key=lambda item: (item["name"], item["service"],
                                           json.dumps(item["labels"], sort_keys=True)))
        metric_report = {
            "series": metric_rows[:top],
            "series_count": len(metric_rows),
            "points": len(metrics),
            "services": sorted({row["service"] for row in metric_rows}),
            "by_kind": dict(sorted(Counter(row["kind"]
                                          for row in metric_rows).items())),
            "mapped": mapped_metrics,
            "histogram_quantiles": "upper bucket bounds, not interpolated",
        }

    counts_by_status = Counter((row.get("graph") or {}).get("status")
                               for row in rows)
    mapped_self = sum(row["self"] for row in rows
                      if (row.get("graph") or {}).get("status") == "mapped")
    mapped_inclusive = sum(row["inclusive"] for row in rows
                           if (row.get("graph") or {}).get("status") == "mapped")
    frame_count = len(rows)
    coverage = {
        "frames": frame_count,
        "mapped": counts_by_status.get("mapped", 0),
        "ambiguous": counts_by_status.get("ambiguous", 0),
        "unresolved": counts_by_status.get("unresolved", 0),
        "external": counts_by_status.get("external", 0),
        "unavailable": counts_by_status.get("unavailable", 0),
        "not_requested": counts_by_status.get("not_requested", 0),
        "not_assessed": counts_by_status.get("not_assessed", 0),
        "mapped_frames_bp": (counts_by_status.get("mapped", 0) * 10_000 // frame_count
                             if frame_count else 0),
        "mapped_self_bp": mapped_self * 10_000 // total_self if total_self else 0,
        "mapped_inclusive_bp": (mapped_inclusive * 10_000 // sum(inclusive.values())
                                if inclusive else 0),
        "unmapped_self_bp": ((total_self - mapped_self) * 10_000 // total_self
                             if total_self else 0),
        "metrics_series": metric_report["series_count"] if metric_report else 0,
        "metrics_mapped": metric_report["mapped"] if metric_report else 0,
    }

    callouts: list[str] = []
    if hotspots and total_self:
        top3 = sum(row["self"] for row in hotspots[:3])
        callouts.append(
            f"top 3 frames account for {top3 * 10_000 // total_self / 100:.1f}% "
            f"of measured self {'time' if unit == 'microseconds' else 'samples'}")
    callouts.append(f"{coverage['mapped_self_bp'] / 100:.1f}% of self weight "
                    "maps to repository definitions")
    if coverage["external"]:
        callouts.append(f"{coverage['external']} active frames are outside the "
                        "repository or use synthetic paths")
    if coverage["unresolved"] or coverage["ambiguous"]:
        callouts.append(f"{coverage['unresolved']} unresolved and "
                        f"{coverage['ambiguous']} ambiguous active frames remain")
    if idle_us:
        callouts.append(f"{idle_us} us of Chrome idle samples were excluded")
    if telemetry and telemetry["error_spans"]:
        callouts.append(f"{telemetry['error_spans']} telemetry span(s) have error status")
    if telemetry:
        callouts.append("telemetry totals sum span durations; nested spans can overlap")
    if metric_report:
        callouts.append(f"{metric_report['series_count']} metric series; "
                        f"{metric_report['mapped']} map to a code location")
        if "histogram" in metric_report["by_kind"] or "summary" in metric_report["by_kind"]:
            callouts.append("histogram p50/p95/p99 are bucket upper bounds; "
                            "summary quantiles are the exporter's values")
    if graph is not None and graph.error:
        callouts.append(f"graph join is partial: {graph.error}")

    result = {
        "measurement": {
            "kind": kind,
            "unit": unit,
            "clock": profile.get("clock") or "unknown",
            "total_self": int(total_self),
            "total_inclusive": int(sum(inclusive.values())),
            "frames": frame_count,
            "samples": len(samples),
            "sample_weight": int((profile.get("totals") or {}).get("sample_weight") or 0),
            "functions": len(functions),
            "spans": len(spans),
            "metrics": len(metrics),
            "idle_us": idle_us,
            "truth_assessed": False,
        },
        "coverage": coverage,
        "hotspots": hotspots,
        "files": file_rows,
        "modules": module_rows,
        "paths": paths,
        "telemetry": telemetry,
        "metrics": metric_report,
        "callouts": callouts,
        "graph": graph.summary() if graph is not None else {
            "source": "not_requested", "error": None},
        "baseline": None,
        "omissions": dict(profile.get("omissions") or {}),
        "warnings": list(profile.get("warnings") or []),
        "sort": sort,
    }
    if baseline is not None:
        result["baseline"] = compare_analyses(baseline, result, top=min(top, 20))
    return result


def compare_analyses(base: Mapping[str, Any], current: Mapping[str, Any], *,
                     top: int = 10) -> dict[str, Any]:
    """Compare two analyses by symbol or file/name key, never by line."""
    try:
        top = min(max(int(top), 1), MAX_TOP)
    except (TypeError, ValueError):
        top = 10
    base_measure = base.get("measurement") or {}
    current_measure = current.get("measurement") or {}
    if base_measure.get("unit") != current_measure.get("unit"):
        return {"compatible": False,
                "reason": "units differ: "
                          f"{base_measure.get('unit')!r} vs "
                          f"{current_measure.get('unit')!r}"}
    base_rows = {row.get("key"): row for row in base.get("hotspots") or []
                 if isinstance(row, Mapping)}
    current_rows = {row.get("key"): row for row in current.get("hotspots") or []
                    if isinstance(row, Mapping)}
    matched = 0
    changes: list[dict[str, Any]] = []
    for key in sorted(set(base_rows) | set(current_rows)):
        old = base_rows.get(key)
        new = current_rows.get(key)
        if old is None or new is None:
            continue
        matched += 1
        delta_self = int(new.get("self") or 0) - int(old.get("self") or 0)
        delta_inclusive = int(new.get("inclusive") or 0) - int(old.get("inclusive") or 0)
        delta_calls = (int(new.get("calls") or 0) - int(old.get("calls") or 0)
                       if new.get("calls") is not None and old.get("calls") is not None
                       else None)
        changes.append({
            "key": key,
            "name": new.get("name") or old.get("name"),
            "file": new.get("file") or old.get("file"),
            "symbol": (new.get("graph") or {}).get("symbol")
                      or (old.get("graph") or {}).get("symbol"),
            "base_self": int(old.get("self") or 0),
            "current_self": int(new.get("self") or 0),
            "delta_self": delta_self,
            "delta_inclusive": delta_inclusive,
            "delta_calls": delta_calls,
        })
    regressions = sorted((item for item in changes if item["delta_self"] > 0),
                         key=lambda item: (-item["delta_self"], item["key"]))[:top]
    improvements = sorted((item for item in changes if item["delta_self"] < 0),
                          key=lambda item: (item["delta_self"], item["key"]))[:top]
    return {
        "compatible": True,
        "unit": current_measure.get("unit"),
        "self_delta": int(current_measure.get("total_self") or 0)
                       - int(base_measure.get("total_self") or 0),
        "matched": matched,
        "base_only": len(set(base_rows) - set(current_rows)),
        "current_only": len(set(current_rows) - set(base_rows)),
        "regressions": regressions,
        "improvements": improvements,
    }


__all__ = ["SORTS", "analyze", "compare_analyses"]
