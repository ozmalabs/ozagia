"""Read existing profiler and telemetry artifacts into canonical profiles.

Supported inputs are read-only:

* Python ``cProfile``/``pstats`` dumps (``.prof``, ``.pstats``);
* Speedscope sampled or evented JSON (including ``py-spy record -f speedscope``);
* Chrome/Node ``.cpuprofile`` JSON;
* folded/collapsed stack text (for example ``perf script | stackcollapse-perf.pl``);
* OpenTelemetry OTLP JSON traces.

No target project is imported, no profiler is started and no network request is
made.  Missing fields remain unknown; a format is never guessed from a file
extension alone when the content does not match.
"""
from __future__ import annotations

import io
import json
import math
import pstats
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import (
    FormatError, ProfileBuilder, ProfileError, bounded_int, file_digest,
    nonnegative_int, normalize_path, number_text, profile_hash,
    read_bytes_bounded, seconds_to_us, unit_to_us, validate_profile,
)

FORMATS = ("auto", "cprofile", "speedscope", "chrome_cpuprofile",
           "collapsed", "otlp_json", "otlp_metrics", "prometheus_text")
WEIGHT_UNITS = ("auto", "samples", "microseconds", "milliseconds", "seconds")
MAX_TEXT_LINES = 2_000_000
MAX_COLLAPSED_LINE = 16_384
MAX_JSON_BYTES = 64 * 1024 * 1024

_JSON_FORMAT_BY_SUFFIX = {
    ".speedscope": "speedscope",
    ".cpuprofile": "chrome_cpuprofile",
    ".folded": "collapsed",
    ".collapsed": "collapsed",
    ".perf": "collapsed",
}
_PROFILE_SUFFIXES = (".prof", ".pstats")

_CHROME_SYNTHETIC = {"(root)", "(program)"}
_CHROME_IDLE = "(idle)"
_CHROME_SYSTEM = {"(garbage collector)", "(native)", "(compiler)", "(internal)"}

_OTLP_ATTRIBUTES = frozenset({
    "code.filepath", "code.file.path", "code.lineno", "code.line.number",
    "code.function", "code.namespace", "http.method", "http.route",
    "http.status_code", "rpc.system", "rpc.method", "db.system",
    "db.operation", "messaging.system", "messaging.operation",
    "deployment.environment", "service.version", "error.type",
    "exception.type", "k8s.pod.name", "k8s.namespace.name", "cloud.region",
})
_CODE_FILE_KEYS = ("code.filepath", "code.file.path", "code_filepath")
_CODE_LINE_KEYS = ("code.lineno", "code.line.number", "code_lineno")
_CODE_FUNCTION_KEYS = ("code.function", "code_function")
_CODE_NAMESPACE_KEYS = ("code.namespace", "code_namespace")


def _source(path: Path, kind: str, tool: str,
            *, digest: str | None = None, size: int | None = None) -> dict[str, Any]:
    if digest is None or size is None:
        digest, size = file_digest(path)
    return {
        "kind": kind,
        "path": str(path),
        "sha256": digest,
        "bytes": int(size),
        "tool": tool,
    }


def _line(value: Any, *, chrome: bool = False) -> int | None:
    number = bounded_int(value, name="line", low=0, default=None)
    if number is None:
        return None
    if chrome:
        number += 1
    return number if number >= 1 else None


def _frame_for(builder: ProfileBuilder, root: Path, name: Any,
               file: Any, line: Any, *, kind: str = "function",
               raw_file: Any = None, chrome: bool = False) -> str:
    normalized, external = normalize_path(root, file)
    return builder.frame(name, normalized, _line(line, chrome=chrome),
                         kind=kind, raw_file=raw_file or file,
                         external=external)


def _safe_json(path: Path) -> Any:
    raw = read_bytes_bounded(path, limit=MAX_JSON_BYTES)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # The OpenTelemetry file exporter commonly writes one ResourceSpans
        # object per line. Merge those objects without accepting arbitrary
        # truncated JSON.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FormatError(f"{path} is not UTF-8 JSON: {exc}") from exc
        objects = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                objects.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise FormatError(f"{path} is not a JSON profile: {exc}") from exc
        if not objects or not all(isinstance(item, Mapping) for item in objects):
            raise FormatError(f"{path} is not a JSON profile object")
        merged: dict[str, Any] = {}
        for key in ("resourceSpans", "resource_spans",
                    "resourceMetrics", "resource_metrics"):
            values = [item.get(key) for item in objects if isinstance(item.get(key), list)]
            if values:
                merged[key] = [entry for value in values for entry in value]
        if merged:
            return merged
        raise FormatError(f"{path} is not a recognized JSON profile")


def _looks_like_json(head: bytes) -> bool:
    stripped = head.lstrip()
    return stripped.startswith(b"{") or stripped.startswith(b"[")


def detect_format(path: Path, *, head: bytes | None = None) -> str:
    """Return one of :data:`FORMATS` except ``auto``, or raise FormatError."""
    suffix = path.suffix.lower()
    if suffix in _PROFILE_SUFFIXES:
        return "cprofile"
    if suffix in _JSON_FORMAT_BY_SUFFIX:
        guessed = _JSON_FORMAT_BY_SUFFIX[suffix]
        if guessed != "collapsed":
            return guessed
    if head is None:
        try:
            with path.open("rb") as stream:
                head = stream.read(8192)
        except OSError as exc:
            raise ProfileError(f"cannot read {path}: {exc}") from exc
    if _looks_like_json(head):
        try:
            data = _safe_json(path)
        except FormatError:
            raise
        if isinstance(data, Mapping):
            if isinstance(data.get("resourceSpans"), list) or isinstance(
                    data.get("resource_spans"), list):
                return "otlp_json"
            if isinstance(data.get("resourceMetrics"), list) or isinstance(
                    data.get("resource_metrics"), list):
                return "otlp_metrics"
            if isinstance(data.get("profiles"), list):
                return "speedscope"
            if isinstance(data.get("nodes"), list) and (
                    isinstance(data.get("samples"), list)
                    or any(isinstance(node, Mapping) and "hitCount" in node
                           for node in data["nodes"])):
                return "chrome_cpuprofile"
        raise FormatError(f"{path} is JSON but not a recognized profile format")
    if suffix in (".folded", ".collapsed", ".perf"):
        return "collapsed"
    if suffix in (".prom", ".prometheus"):
        return "prometheus_text"
    try:
        text = head.decode("utf-8", "replace")
    except Exception:
        text = ""
    stripped_lines = [line.strip() for line in text.splitlines()[:40]]
    for stripped in stripped_lines:
        if ";" in stripped and re.search(r"\s\d+(?:\.\d+)?\s*$", stripped):
            return "collapsed"
    if any(line.startswith(("# TYPE ", "# HELP ")) for line in stripped_lines):
        return "prometheus_text"
    if any(re.match(r"^[A-Za-z_:][A-Za-z0-9_:]*(?:\{[^}]*\})?\s+"
                    r"[-+]?(?:[0-9.]+(?:[eE][-+]?[0-9]+)?|NaN|[+-]Inf)\b",
                    line) for line in stripped_lines):
        return "prometheus_text"
    raise FormatError(
        f"{path} is not a recognized profile; pass --format explicitly")


def _ingest_cprofile(path: Path, root: Path) -> ProfileBuilder:
    try:
        stats = pstats.Stats(str(path), stream=io.StringIO())
    except (OSError, EOFError, TypeError, ValueError, KeyError) as exc:
        raise FormatError(f"{path} is not a readable cProfile/pstats dump: {exc}") from exc
    builder = ProfileBuilder(source=_source(path, "cprofile", "cProfile/pstats"))
    builder.clock = "wall"
    rows = getattr(stats, "stats", None)
    if not isinstance(rows, dict) or not rows:
        raise FormatError(f"{path} contains no cProfile function records")

    def add_func(key: Any, *, kind: str = "function") -> str | None:
        if not isinstance(key, (tuple, list)) or not key:
            return None
        file = key[0] if len(key) > 0 else None
        line = key[1] if len(key) > 1 else None
        name = key[2] if len(key) > 2 else "(unknown)"
        return _frame_for(builder, root, name, file, line, kind=kind)

    for key, values in rows.items():
        if not isinstance(values, (tuple, list)) or len(values) < 4:
            continue
        cc, nc, tt, ct = values[:4]
        fid = add_func(key)
        if fid is None:
            continue
        builder.function(
            fid,
            self_us=seconds_to_us(tt),
            cumulative_us=seconds_to_us(ct),
            calls=nc,
            primitive_calls=cc,
        )
        callers = values[4] if len(values) > 4 and isinstance(values[4], Mapping) else {}
        for caller, caller_values in callers.items():
            cfid = add_func(caller)
            if cfid is None:
                continue
            calls = None
            if isinstance(caller_values, (tuple, list)) and len(caller_values) > 1:
                calls = caller_values[1]
            builder.edge(cfid, fid, calls=calls)
    return builder


def _speedscope_frames(data: Mapping[str, Any], root: Path,
                       builder: ProfileBuilder) -> tuple[list[str], str | None]:
    shared = data.get("shared")
    raw_frames = shared.get("frames") if isinstance(shared, Mapping) else None
    if not isinstance(raw_frames, list) or not raw_frames:
        raise FormatError("speedscope profile has no shared.frames")
    ids: list[str] = []
    tool = "speedscope"
    for frame in raw_frames:
        if not isinstance(frame, Mapping):
            ids.append(builder.frame("(unknown)", None, None))
            continue
        name = frame.get("name") or "(unknown)"
        file = frame.get("file")
        kind = "function"
        if not file:
            kind = "native" if name.startswith("<") else "unknown"
        ids.append(_frame_for(builder, root, name, file, frame.get("line"),
                              kind=kind, raw_file=file))
    return ids, tool


def _speedscope_unit(profile: Mapping[str, Any], interval_us: int | None,
                     builder: ProfileBuilder) -> str:
    unit = str(profile.get("unit") or "none").lower()
    if unit == "bytes":
        raise FormatError("speedscope byte profiles are not time profiles")
    if unit == "none":
        if interval_us:
            builder.warn("speedscope weights were sample counts; "
                         f"converted with the supplied {interval_us} us interval")
            return "microseconds"
        return "samples"
    if unit in ("microseconds", "milliseconds", "seconds", "nanoseconds"):
        return unit
    builder.warn(f"unknown speedscope unit {unit!r}; samples are kept as counts")
    return "samples"


def _speedscope_weight(value: Any, source_unit: str,
                       interval_us: int | None) -> tuple[int | None, str]:
    if source_unit in ("microseconds", "milliseconds", "seconds",
                       "nanoseconds"):
        return unit_to_us(value, source_unit), "microseconds"
    if interval_us:
        count = nonnegative_int(value)
        if count is None:
            return None, "samples"
        return count * int(interval_us), "microseconds"
    return nonnegative_int(value), "samples"


def _ingest_speedscope(path: Path, root: Path,
                       interval_us: int | None) -> ProfileBuilder:
    data = _safe_json(path)
    if not isinstance(data, Mapping):
        raise FormatError("speedscope profile must be a JSON object")
    builder = ProfileBuilder(source=_source(path, "speedscope", "speedscope"))
    builder.clock = "cpu"
    ids, tool = _speedscope_frames(data, root, builder)
    builder.source["tool"] = tool
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise FormatError("speedscope profile has no profiles")
    for profile in profiles:
        if not isinstance(profile, Mapping):
            continue
        if "py-spy" in str(profile.get("name") or "").lower():
            builder.source["tool"] = "py-spy"
        kind = profile.get("type")
        if kind == "sampled":
            source_unit = str(profile.get("unit") or "none").lower()
            _speedscope_unit(profile, interval_us, builder)
            samples = profile.get("samples")
            weights = profile.get("weights")
            if not isinstance(samples, list):
                continue
            if not isinstance(weights, list):
                weights = []
            if len(weights) != len(samples):
                builder.warn("speedscope weights do not match samples; "
                             "missing weights use 1")
            for index, sample in enumerate(samples):
                weight = weights[index] if index < len(weights) else 1
                if not isinstance(sample, list) or not sample:
                    continue
                stack = []
                for frame_index in sample:
                    number = bounded_int(frame_index, name="frame index", low=0,
                                         default=None)
                    if number is None or number >= len(ids):
                        builder.omit("invalid speedscope frame index", 1)
                        stack = []
                        break
                    stack.append(ids[number])
                if not stack:
                    continue
                value, chosen = _speedscope_weight(weight, source_unit,
                                                   interval_us)
                if value:
                    builder.sample(stack, value, chosen)
        elif kind == "evented":
            source_unit = str(profile.get("unit") or "none").lower()
            _speedscope_unit(profile, interval_us, builder)
            events = profile.get("events")
            if not isinstance(events, list):
                continue
            last = profile.get("startValue", 0)
            last = float(last) if isinstance(last, (int, float)) else 0.0
            stack: list[str] = []
            warned = False
            for event in events:
                if not isinstance(event, Mapping):
                    continue
                at = event.get("at")
                if isinstance(at, (int, float)) and not isinstance(at, bool):
                    delta = float(at) - last
                    if delta > 0 and stack:
                        value, chosen = _speedscope_weight(delta, source_unit,
                                                           interval_us)
                        if value:
                            builder.sample(stack, value, chosen)
                    last = float(at)
                index = bounded_int(event.get("frame"), name="frame index",
                                    low=0, default=None)
                if index is None or index >= len(ids):
                    continue
                if event.get("type") == "O":
                    stack.append(ids[index])
                elif event.get("type") == "C":
                    if stack and stack[-1] == ids[index]:
                        stack.pop()
                    elif ids[index] in stack:
                        if not warned:
                            builder.warn("speedscope evented stack close did not "
                                         "match the current frame")
                            warned = True
                        stack.remove(ids[index])
    return builder


def _chrome_stack(nodes: Mapping[int, Mapping[str, Any]], node_id: Any,
                  builder: ProfileBuilder) -> list[Mapping[str, Any]]:
    chain: list[Mapping[str, Any]] = []
    seen: set[int] = set()
    current = bounded_int(node_id, name="node id", low=0, default=None)
    while current is not None and current not in seen:
        seen.add(current)
        node = nodes.get(current)
        if node is None:
            break
        chain.append(node)
        parent = node.get("parent", node.get("parentId"))
        current = bounded_int(parent, name="parent node", low=0, default=None)
    chain.reverse()
    return chain


def _ingest_chrome(path: Path, root: Path) -> ProfileBuilder:
    data = _safe_json(path)
    if not isinstance(data, Mapping):
        raise FormatError("Chrome CPU profile must be a JSON object")
    nodes_raw = data.get("nodes")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise FormatError("Chrome CPU profile has no nodes")
    builder = ProfileBuilder(source=_source(path, "chrome_cpuprofile",
                                             "Chrome/Node CPU profile"))
    builder.clock = "cpu"
    nodes: dict[int, Mapping[str, Any]] = {}
    for node in nodes_raw:
        if not isinstance(node, Mapping):
            continue
        number = bounded_int(node.get("id"), name="node id", low=0, default=None)
        if number is not None:
            nodes[number] = node
    if not nodes:
        raise FormatError("Chrome CPU profile has no valid nodes")

    def node_frame(node: Mapping[str, Any]) -> tuple[str | None, str] | None:
        call = node.get("callFrame")
        if not isinstance(call, Mapping):
            call = {}
        name = call.get("functionName") or "(anonymous)"
        if name in _CHROME_SYNTHETIC or name == _CHROME_IDLE:
            return None
        kind = "function"
        if name in _CHROME_SYSTEM:
            kind = "system"
        return _frame_for(builder, root, name, call.get("url"),
                          call.get("lineNumber"), kind=kind,
                          raw_file=call.get("url"), chrome=True), name

    def add_stack(node_id: Any) -> tuple[list[str] | None, bool]:
        chain = _chrome_stack(nodes, node_id, builder)
        stack: list[str] = []
        leaf_idle = False
        for node in chain:
            call = node.get("callFrame")
            raw_name = call.get("functionName") if isinstance(call, Mapping) else None
            if raw_name == _CHROME_IDLE:
                leaf_idle = True
                continue
            if raw_name not in _CHROME_SYNTHETIC:
                leaf_idle = False
            result = node_frame(node)
            if result is None:
                continue
            frame, _name = result
            if frame:
                stack.append(frame)
        return (stack or None), leaf_idle

    samples = data.get("samples")
    deltas = data.get("timeDeltas", data.get("time_deltas"))
    if isinstance(samples, list) and samples:
        use_deltas = isinstance(deltas, list)
        if use_deltas and len(deltas) < len(samples):
            builder.warn("Chrome timeDeltas are shorter than samples; "
                         "missing samples use 1 us")
        for index, node_id in enumerate(samples):
            stack, leaf_idle = add_stack(node_id)
            weight: Any = 1
            unit = "samples"
            if use_deltas:
                weight = deltas[index] if index < len(deltas) else 1
                unit = "microseconds"
            if leaf_idle:
                value = bounded_int(weight, name="idle weight", low=0, default=0) or 0
                if unit == "microseconds":
                    builder.idle_us += value
                continue
            if not stack:
                continue
            builder.sample(stack, weight, unit)
    else:
        for node_id, node in sorted(nodes.items()):
            count = bounded_int(node.get("hitCount"), name="hit count", low=0,
                                default=0) or 0
            if not count:
                continue
            stack, leaf_idle = add_stack(node_id)
            if leaf_idle or not stack:
                continue
            builder.sample(stack, count, "samples")
    if builder.idle_us:
        builder.warn(f"excluded {builder.idle_us} us of Chrome idle samples")
    return builder


_COLLAPSED_PAREN = re.compile(
    r"^(?P<name>.*?)\s+\((?P<file>[^()]+?)(?::(?P<line>\d+))?(?::\d+)?\)$")
_COLLAPSED_AT = re.compile(
    r"^(?P<name>.*?)\s+(?:at|@)\s+(?P<file>.+?):(?P<line>\d+)(?::\d+)?$")


def _collapsed_frame(token: str) -> tuple[str, str | None, int | None]:
    text = token.strip()
    if not text:
        return "(unknown)", None, None
    match = _COLLAPSED_PAREN.match(text)
    if match:
        return (match.group("name") or "(unknown)", match.group("file"),
                bounded_int(match.group("line"), name="line", low=1, default=None))
    match = _COLLAPSED_AT.match(text)
    if match:
        return (match.group("name") or "(unknown)", match.group("file"),
                bounded_int(match.group("line"), name="line", low=1, default=None))
    # path:line or path:line:column, without a separate function name.
    if ":" in text and not text.startswith(("http://", "https://")):
        parts = text.rsplit(":", 2)
        line_text = None
        file = None
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            line_text, file = parts[1], parts[0]
        elif len(parts) >= 2 and parts[-1].isdigit():
            line_text, file = parts[-1], ":".join(parts[:-1])
        if line_text and file:
            line = bounded_int(line_text, name="line", low=1, default=None)
            stem = file.replace("\\", "/").rsplit("/", 1)[-1]
            return (stem or "(unknown)", file, line)
    return text, None, None


def _ingest_collapsed(path: Path, root: Path, *, weight_unit: str,
                      interval_us: int | None) -> ProfileBuilder:
    raw = read_bytes_bounded(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormatError(f"{path} is not UTF-8 collapsed stack text: {exc}") from exc
    builder = ProfileBuilder(source=_source(path, "collapsed",
                                             "collapsed stacks"))
    builder.clock = "cpu"
    if weight_unit == "auto":
        chosen = "microseconds" if interval_us else "samples"
    elif weight_unit in ("microseconds", "milliseconds", "seconds"):
        chosen = "microseconds"
    else:
        chosen = "samples"
        if interval_us:
            builder.warn("--interval-us is ignored with an explicit sample weight unit")
    lines = text.splitlines()
    if len(lines) > MAX_TEXT_LINES:
        builder.omit("collapsed line limit reached",
                     len(lines) - MAX_TEXT_LINES)
        lines = lines[:MAX_TEXT_LINES]
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > MAX_COLLAPSED_LINE:
            builder.omit("oversized collapsed line", 1)
            continue
        match = re.match(r"^(?P<stack>.*?)\s+(?P<weight>\d+(?:\.\d+)?)\s*$",
                         line)
        if match:
            stack_text, raw_weight = match.group("stack"), match.group("weight")
        else:
            stack_text, raw_weight = line, "1"
        try:
            weight = float(raw_weight)
        except ValueError:
            stack_text, raw_weight = line, "1"
            weight = 1.0
        if not math.isfinite(weight) or weight <= 0:
            builder.omit("invalid collapsed weight", 1)
            continue
        if chosen == "microseconds":
            if interval_us and weight_unit == "auto":
                value = int(round(weight * interval_us))
            elif weight_unit in ("milliseconds", "seconds"):
                value = unit_to_us(weight, weight_unit)
            else:
                value = int(round(weight))
        else:
            value = int(round(weight))
        if not value:
            continue
        stack = []
        for token in stack_text.split(";"):
            name, file, line_no = _collapsed_frame(token)
            stack.append(_frame_for(builder, root, name, file, line_no,
                                    kind="function" if file else "unknown",
                                    raw_file=file))
        if stack:
            builder.sample(stack, value, chosen)
    builder.warnings.append(
        "collapsed stacks do not state whether weights are samples or "
        "durations; use --weight-unit/--interval-us when known")
    return builder


def _attr_value(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return None
    for key in ("stringValue", "string_value"):
        if key in value:
            return value[key]
    for key in ("intValue", "int_value"):
        if key in value:
            try:
                return int(value[key])
            except (TypeError, ValueError):
                return None
    for key in ("doubleValue", "double_value"):
        if key in value:
            try:
                number = float(value[key])
            except (TypeError, ValueError):
                return None
            return number if math.isfinite(number) else None
    for key in ("boolValue", "bool_value"):
        if key in value:
            return bool(value[key])
    return None


def _attributes(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): _attr_value(item) for key, item in value.items()}
    if not isinstance(value, list):
        return {}
    out: dict[str, Any] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        key = item.get("key")
        if isinstance(key, str):
            out[key] = _attr_value(item.get("value"))
    return out


def _first(attributes: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in attributes and attributes[key] not in (None, ""):
            return attributes[key]
    return None


def _otlp_status(value: Any) -> str:
    if isinstance(value, Mapping):
        code = value.get("code")
    else:
        code = value
    if code in (2, "2", "STATUS_CODE_ERROR", "ERROR", "error"):
        return "error"
    if code in (1, "1", "STATUS_CODE_OK", "OK", "ok"):
        return "ok"
    return "unset"


def _otlp_kind(value: Any) -> str:
    names = {0: "unknown", 1: "internal", 2: "server", 3: "client",
             4: "producer", 5: "consumer"}
    if isinstance(value, str) and value.lower().startswith("span_kind_"):
        value = value.lower().removeprefix("span_kind_")
        return value if value in names.values() else "unknown"
    number = bounded_int(value, name="span kind", low=0, high=5, default=0)
    return names.get(number or 0, "unknown")


def _ingest_otlp(path: Path, root: Path) -> ProfileBuilder:
    data = _safe_json(path)
    if not isinstance(data, Mapping):
        raise FormatError("OTLP JSON must be an object")
    resources = data.get("resourceSpans", data.get("resource_spans"))
    if not isinstance(resources, list):
        raise FormatError("OTLP JSON has no resourceSpans")
    builder = ProfileBuilder(source=_source(path, "otlp_json",
                                             "OpenTelemetry OTLP JSON"))
    builder.clock = "wall"
    dropped_attributes = 0
    for resource in resources:
        if not isinstance(resource, Mapping):
            continue
        resource_attrs = _attributes(resource.get("resource", {}).get("attributes")
                                     if isinstance(resource.get("resource"), Mapping)
                                     else resource.get("attributes"))
        service = str(resource_attrs.get("service.name") or "unknown")
        scopes = resource.get("scopeSpans", resource.get("scope_spans"))
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if not isinstance(scope, Mapping):
                continue
            spans = scope.get("spans")
            if not isinstance(spans, list):
                continue
            for span in spans:
                if not isinstance(span, Mapping):
                    continue
                attrs = _attributes(span.get("attributes"))
                allowed: dict[str, Any] = {}
                for key, value in attrs.items():
                    if key in _OTLP_ATTRIBUTES:
                        allowed[key] = value
                    else:
                        dropped_attributes += 1
                file = _first(allowed, _CODE_FILE_KEYS)
                line = _first(allowed, _CODE_LINE_KEYS)
                function = _first(allowed, _CODE_FUNCTION_KEYS)
                namespace = _first(allowed, _CODE_NAMESPACE_KEYS)
                name = span.get("name") or "(unnamed span)"
                frame_name = function or name
                if function and namespace:
                    frame_name = f"{namespace}.{function}"
                frame_kind = "function" if (file or function or namespace) else "span"
                frame = _frame_for(builder, root, frame_name, file, line,
                                   kind=frame_kind, raw_file=file)
                trace = span.get("traceId", span.get("trace_id"))
                span_id = span.get("spanId", span.get("span_id"))
                parent = span.get("parentSpanId", span.get("parent_span_id"))
                if isinstance(parent, str) and set(parent) <= {"0"}:
                    parent = None
                start = span.get("startTimeUnixNano",
                                span.get("start_time_unix_nano"))
                end = span.get("endTimeUnixNano", span.get("end_time_unix_nano"))
                if start is None or end is None:
                    builder.omit("OTLP span has no start/end timestamp", 1)
                    continue
                try:
                    start_us = int(start) // 1000
                    end_us = int(end) // 1000
                except (TypeError, ValueError):
                    builder.omit("OTLP span has an invalid timestamp", 1)
                    continue
                duration = max(0, end_us - start_us)
                builder.span(
                    name=name, frame=frame, service=service,
                    span_id=span_id, parent_id=parent, trace_id=trace,
                    kind=_otlp_kind(span.get("kind")),
                    start_us=start_us, duration_us=duration,
                    status=_otlp_status(span.get("status")),
                    attributes=allowed)
    if dropped_attributes:
        builder.warn(f"dropped {dropped_attributes} non-allowlisted OTLP attributes")
    if not builder.spans:
        raise FormatError("OTLP JSON contains no spans")
    return builder


def _metric_frame(builder: ProfileBuilder, root: Path,
                  attributes: Mapping[str, Any], fallback: str) -> str | None:
    file = _first(attributes, _CODE_FILE_KEYS)
    line = _first(attributes, _CODE_LINE_KEYS)
    function = _first(attributes, _CODE_FUNCTION_KEYS)
    namespace = _first(attributes, _CODE_NAMESPACE_KEYS)
    if not any(item not in (None, "") for item in (file, line, function, namespace)):
        return None
    name = function or fallback
    if function and namespace:
        name = f"{namespace}.{function}"
    return _frame_for(builder, root, name, file, line, kind="function",
                      raw_file=file)


def _ns_us(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number // 1000)


def _metric_value(point: Mapping[str, Any]) -> Any:
    for key in ("asDouble", "as_double", "asInt", "as_int", "value"):
        if key in point and point[key] not in (None, ""):
            return point[key]
    return None


def _metric_buckets(point: Mapping[str, Any]) -> list[dict[str, Any]]:
    bounds = point.get("explicitBounds", point.get("explicit_bounds"))
    counts = point.get("bucketCounts", point.get("bucket_counts"))
    if not isinstance(bounds, list):
        return []
    if not isinstance(counts, list):
        counts = []
    out: list[dict[str, Any]] = []
    cumulative = 0
    for index, bound in enumerate(bounds):
        if index < len(counts):
            cumulative += bounded_int(counts[index], name="bucket count",
                                      low=0, default=0) or 0
        text = number_text(bound)
        if text is not None:
            out.append({"le": text, "count": cumulative})
    if len(counts) > len(bounds):
        cumulative += bounded_int(counts[len(bounds)], name="bucket count",
                                  low=0, default=0) or 0
    out.append({"le": "+Inf", "count": cumulative})
    return out


def _ingest_otlp_metrics(path: Path, root: Path) -> ProfileBuilder:
    data = _safe_json(path)
    if not isinstance(data, Mapping):
        raise FormatError("OTLP metrics JSON must be an object")
    resources = data.get("resourceMetrics", data.get("resource_metrics"))
    if not isinstance(resources, list):
        raise FormatError("OTLP JSON has no resourceMetrics")
    builder = ProfileBuilder(source=_source(path, "otlp_metrics",
                                             "OpenTelemetry OTLP metrics"))
    builder.clock = "wall"
    for resource in resources:
        if not isinstance(resource, Mapping):
            continue
        resource_data = resource.get("resource")
        resource_attrs = _attributes(
            resource_data.get("attributes") if isinstance(resource_data, Mapping)
            else resource.get("attributes"))
        service = str(resource_attrs.get("service.name") or "unknown")
        scopes = resource.get("scopeMetrics", resource.get("scope_metrics"))
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if not isinstance(scope, Mapping):
                continue
            metrics = scope.get("metrics")
            if not isinstance(metrics, list):
                continue
            for metric in metrics:
                if not isinstance(metric, Mapping):
                    continue
                name = metric.get("name") or "(unnamed metric)"
                unit = metric.get("unit") or ""
                for field, kind in (("gauge", "gauge"), ("sum", "sum"),
                                    ("histogram", "histogram"),
                                    ("summary", "summary")):
                    payload = metric.get(field)
                    if not isinstance(payload, Mapping):
                        continue
                    points = payload.get("dataPoints", payload.get("data_points"))
                    if not isinstance(points, list):
                        continue
                    for point in points:
                        if not isinstance(point, Mapping):
                            continue
                        attributes = _attributes(point.get("attributes"))
                        labels = {key: value for key, value in attributes.items()
                                  if value not in (None, "")}
                        frame = _metric_frame(builder, root, labels, str(name))
                        start = _ns_us(point.get("startTimeUnixNano",
                                                 point.get("start_time_unix_nano")))
                        end = _ns_us(point.get("timeUnixNano",
                                               point.get("time_unix_nano")))
                        actual_kind = kind
                        if kind == "sum" and payload.get("isMonotonic") is True:
                            actual_kind = "counter"
                        if kind == "histogram":
                            builder.metric(
                                name=name, kind=actual_kind, unit=unit,
                                service=service, labels=labels,
                                count=point.get("count"),
                                sum_value=point.get("sum"),
                                buckets=_metric_buckets(point),
                                start_us=start, end_us=end, frame=frame,
                                attributes=attributes)
                        elif kind == "summary":
                            quantiles = []
                            for qv in point.get("quantileValues",
                                                point.get("quantile_values")) or []:
                                if not isinstance(qv, Mapping):
                                    continue
                                quantiles.append({
                                    "q": qv.get("quantile"),
                                    "value": qv.get("value"),
                                })
                            builder.metric(
                                name=name, kind=actual_kind, unit=unit,
                                service=service, labels=labels,
                                count=point.get("count"),
                                sum_value=point.get("sum"),
                                quantiles=quantiles,
                                start_us=start, end_us=end, frame=frame,
                                attributes=attributes)
                        else:
                            builder.metric(
                                name=name, kind=actual_kind, unit=unit,
                                service=service, labels=labels,
                                value=_metric_value(point),
                                start_us=start, end_us=end, frame=frame,
                                attributes=attributes)
                if isinstance(metric.get("exponentialHistogram"), Mapping):
                    builder.omit("exponential histogram is not converted", 1)
    if not builder.metrics:
        raise FormatError("OTLP JSON contains no metrics")
    return builder


_PROM_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:\\.|[^"\\])*)"')
_PROM_SAMPLE = re.compile(
    r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+"
    r"([-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|NaN|"
    r"[+-]Inf)(?:\s+(\d+))?\s*$")


def _prom_labels(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    out: dict[str, str] = {}
    for match in _PROM_LABEL.finditer(text):
        key, value = match.group(1), match.group(2)
        value = (value.replace(r"\\", "\\").replace(r"\"", '"')
                 .replace(r"\n", "\n"))
        out[key] = value
    return out


def _prom_service(labels: Mapping[str, str]) -> str:
    for key in ("service", "service_name", "service.name", "job", "app"):
        if labels.get(key):
            return labels[key]
    return "unknown"


def _ingest_prometheus(path: Path, root: Path) -> ProfileBuilder:
    raw = read_bytes_bounded(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FormatError(f"{path} is not UTF-8 Prometheus text: {exc}") from exc
    builder = ProfileBuilder(source=_source(path, "prometheus_text",
                                             "Prometheus text exposition"))
    builder.clock = "wall"
    types: dict[str, str] = {}
    samples: list[tuple[str, dict[str, str], str, int | None]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("# TYPE "):
            parts = line.split()
            if len(parts) >= 4:
                types[parts[2]] = parts[3].lower()
            continue
        if line.startswith("#"):
            continue
        match = _PROM_SAMPLE.match(line)
        if not match:
            builder.omit("unparsed Prometheus line", 1)
            continue
        timestamp = None
        if match.group(4):
            try:
                timestamp = int(match.group(4)) * 1000
            except (TypeError, ValueError):
                timestamp = None
        samples.append((match.group(1), _prom_labels(match.group(2)),
                        match.group(3), timestamp))

    histograms: dict[tuple[str, str], dict[str, Any]] = {}
    summaries: dict[tuple[str, str], dict[str, Any]] = {}
    for name, labels, value, timestamp in samples:
        base = name
        kind = types.get(name, "")
        if name.endswith("_bucket"):
            base = name[:-7]
            bucket_labels = {key: item for key, item in labels.items()
                             if key != "le"}
            key = (base, json.dumps(bucket_labels, sort_keys=True))
            histograms.setdefault(key, {
                "name": base, "labels": bucket_labels, "buckets": [],
                "count": None, "sum": None, "timestamp": timestamp,
            })
            histograms[key]["buckets"].append({
                "le": labels.get("le", "+Inf"), "count": value})
            continue
        if name.endswith("_sum") and base[:-4] in types and types[base[:-4]] == "histogram":
            base = name[:-4]
            histograms.setdefault((base, json.dumps(labels, sort_keys=True)), {
                "name": base, "labels": dict(labels), "buckets": [],
                "count": None, "sum": None, "timestamp": timestamp,
            })["sum"] = value
            continue
        if name.endswith("_count") and base[:-6] in types and types[base[:-6]] == "histogram":
            base = name[:-6]
            histograms.setdefault((base, json.dumps(labels, sort_keys=True)), {
                "name": base, "labels": dict(labels), "buckets": [],
                "count": None, "sum": None, "timestamp": timestamp,
            })["count"] = value
            continue
        if kind == "histogram":
            # A type line without _bucket samples is still a histogram.
            histograms.setdefault((name, json.dumps(labels, sort_keys=True)), {
                "name": name, "labels": dict(labels), "buckets": [],
                "count": None, "sum": None, "timestamp": timestamp,
            })
            continue
        suffix = ""
        base = name
        for candidate in ("_sum", "_count"):
            if name.endswith(candidate):
                suffix, base = candidate, name[:-len(candidate)]
                break
        base_type = types.get(base, "")
        is_summary = (kind == "summary" or base_type == "summary"
                      or "quantile" in labels)
        if is_summary:
            summary_labels = {key: item for key, item in labels.items()
                              if key != "quantile"}
            key = (base, json.dumps(summary_labels, sort_keys=True))
            summaries.setdefault(key, {
                "name": base, "labels": summary_labels, "quantiles": [],
                "count": None, "sum": None, "timestamp": timestamp,
            })
            entry = summaries[key]
            if "quantile" in labels:
                entry["quantiles"].append({"q": labels["quantile"],
                                           "value": value})
            elif suffix == "_sum":
                entry["sum"] = value
            elif suffix == "_count":
                entry["count"] = value
            continue
        actual = kind if kind in ("counter", "gauge") else (
            "counter" if name.endswith("_total") else "unknown")
        frame = _metric_frame(builder, root, labels, name)
        builder.metric(name=name, kind=actual, unit="", service=_prom_service(labels),
                       labels=labels, value=value, end_us=timestamp,
                       frame=frame, attributes=labels)

    for entry in histograms.values():
        buckets = entry["buckets"]
        if not buckets:
            continue
        # Prometheus buckets are already cumulative; keep the +Inf bucket.
        frame = _metric_frame(builder, root, entry["labels"], entry["name"])
        builder.metric(name=entry["name"], kind="histogram", unit="",
                       service=_prom_service(entry["labels"]),
                       labels=entry["labels"], count=entry["count"],
                       sum_value=entry["sum"], buckets=buckets,
                       end_us=entry["timestamp"], frame=frame,
                       attributes=entry["labels"])
    for entry in summaries.values():
        if not entry["quantiles"] and entry["count"] is None and entry["sum"] is None:
            continue
        frame = _metric_frame(builder, root, entry["labels"], entry["name"])
        builder.metric(name=entry["name"], kind="summary", unit="",
                       service=_prom_service(entry["labels"]),
                       labels=entry["labels"], count=entry["count"],
                       sum_value=entry["sum"], quantiles=entry["quantiles"],
                       end_us=entry["timestamp"], frame=frame,
                       attributes=entry["labels"])
    if not builder.metrics:
        raise FormatError("Prometheus text contains no metric samples")
    return builder


def ingest_file(path: str | Path, *, root: str | Path,
                format: str = "auto", interval_us: int | None = None,
                weight_unit: str = "auto") -> dict[str, Any]:
    """Read one artifact and return a validated canonical profile."""
    path = Path(path)
    root = Path(root).resolve()
    if not path.exists():
        raise ProfileError(f"profile input does not exist: {path}")
    if not path.is_file():
        raise ProfileError(f"profile input is not a regular file: {path}")
    if format not in FORMATS:
        raise ProfileError(f"unknown profile format {format!r}")
    if weight_unit not in WEIGHT_UNITS:
        raise ProfileError(f"unknown weight unit {weight_unit!r}")
    if interval_us is not None:
        interval_us = bounded_int(interval_us, name="interval_us", low=1,
                                  default=None)
        if interval_us is None:
            raise ProfileError("interval_us must be a positive integer")
    if format == "auto":
        format = detect_format(path)
    if format == "cprofile":
        builder = _ingest_cprofile(path, root)
    elif format == "speedscope":
        builder = _ingest_speedscope(path, root, interval_us)
    elif format == "chrome_cpuprofile":
        builder = _ingest_chrome(path, root)
    elif format == "collapsed":
        builder = _ingest_collapsed(path, root, weight_unit=weight_unit,
                                    interval_us=interval_us)
    elif format == "otlp_metrics":
        builder = _ingest_otlp_metrics(path, root)
    elif format == "prometheus_text":
        builder = _ingest_prometheus(path, root)
    else:
        builder = _ingest_otlp(path, root)
    profile = builder.build()
    profile["scope"] = {
        "root": str(root),
        "revision": "unknown",
        "snapshot": "unknown",
    }
    profile["profile_hash"] = None
    validate_profile(profile)
    profile["profile_hash"] = profile_hash(profile)
    return profile


__all__ = ["FORMATS", "WEIGHT_UNITS", "detect_format", "ingest_file"]
