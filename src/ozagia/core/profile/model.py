"""Canonical application-profile records.

The profiler adapters in :mod:`ozagia.core.profile.ingest` produce one
versioned record per artifact.  This module owns the record shape, bounded
values and deterministic ordering; it is standard-library only and performs no
I/O beyond the explicit helpers at the bottom.

All durations are integer microseconds.  A sampled profile whose source does
not state a time unit uses integer sample counts instead; it never invents a
sampling interval.  Durations are measurements from the artifact, not
estimates of task or wall-clock time.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlparse

SCHEMA_NAMESPACE = "ozagia.app_profile"
REPORT_NAMESPACE = "ozagia.profile_report"
VERSION = "1.0"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_FRAMES = 200_000
MAX_SAMPLES = 500_000
MAX_FUNCTIONS = 200_000
MAX_SPANS = 200_000
MAX_METRICS = 200_000
MAX_EDGES = 500_000
MAX_TEXT = 400
MAX_WARNINGS = 20
MAX_ATTRIBUTES = 16
MAX_ATTRIBUTE_TEXT = 200
MAX_METRIC_LABELS = 32
MAX_PATH_TEXT = 1_000

FRAME_KINDS = ("function", "method", "module", "native", "system", "span",
               "idle", "unknown")
SAMPLE_UNITS = ("microseconds", "samples")
SPAN_STATUSES = ("ok", "error", "unset", "unknown")
SPAN_KINDS = ("internal", "server", "client", "producer", "consumer", "unknown")
METRIC_KINDS = ("counter", "gauge", "histogram", "summary", "sum", "unknown")

_UNIT_TO_US = {
    "microseconds": 1,
    "milliseconds": 1_000,
    "seconds": 1_000_000,
    "nanoseconds": 0.001,
}
_EXTENSION_PREFIXES = ("src/", "lib/", "pkg/", "app/")


class ProfileError(ValueError):
    """A malformed, unsupported or unsafe profile input."""


class FormatError(ProfileError):
    """The input is not a recognized profile or telemetry format."""


def bounded_text(value: Any, *, name: str = "text", limit: int = MAX_TEXT,
                 allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProfileError(f"{name} must be a string")
    text = value.replace("\x00", "")
    if not allow_empty and not text:
        raise ProfileError(f"{name} must not be empty")
    if len(text) > limit:
        text = text[: limit - 1] + "\u2026"
    return text


def bounded_int(value: Any, *, name: str = "value", low: int = 0,
                high: int = 2**63 - 1, default: int | None = None) -> int | None:
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if number < low or number > high:
        return default
    return number


def seconds_to_us(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number < 0 or number == float("inf"):
        return None
    return int(round(number * 1_000_000))


def ns_to_us(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number < 0:
        return None
    return number // 1_000


def unit_to_us(value: Any, unit: str) -> int | None:
    factor = _UNIT_TO_US.get(unit)
    if factor is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number < 0 or number == float("inf"):
        return None
    return int(round(number * factor))


def nonnegative_int(value: Any) -> int | None:
    """A bounded non-negative integer, rounding a finite float."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number < 0 or number == float("inf"):
        return None
    return min(2**63 - 1, int(round(number)))


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def file_digest(path: Path, *, limit: int = MAX_INPUT_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ProfileError(
                    f"input exceeds the {limit} byte profiling limit")
            digest.update(chunk)
    return digest.hexdigest(), size


def read_bytes_bounded(path: Path, *, limit: int = MAX_INPUT_BYTES) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ProfileError(f"cannot read {path}: {exc}") from exc
    if size > limit:
        raise ProfileError(f"input is {size} bytes; the limit is {limit}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProfileError(f"cannot read {path}: {exc}") from exc


def read_json_bounded(path: Path, *, limit: int = MAX_INPUT_BYTES) -> Any:
    raw = read_bytes_bounded(path, limit=limit)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FormatError(f"{path} is not a JSON profile: {exc}") from exc


def normalize_path(root: Path | None, value: Any) -> tuple[str | None, bool]:
    """Return ``(normalized path or None, external)``.

    Paths under ``root`` become repository-relative POSIX paths.  Paths outside
    it stay absolute and are marked external.  Synthetic names such as
    ``<string>`` and ``node:internal`` are returned as-is and marked external;
    ``None`` means the artifact did not supply a path.
    """
    if value is None:
        return None, False
    if not isinstance(value, str):
        return None, False
    text = value.strip()
    if not text:
        return None, False
    if text.startswith("<") and text.endswith(">"):
        return text, True
    if text.startswith(("node:", "webpack:", "vite:", "internal:")):
        return text[:MAX_PATH_TEXT], True
    if text.startswith("file://"):
        parsed = urlparse(text)
        text = unquote(parsed.path)
        if parsed.netloc and parsed.netloc not in ("", "localhost"):
            text = f"//{parsed.netloc}{text}"
    elif text.startswith("file:"):
        text = unquote(text[5:])
    if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", text):
        return text[:MAX_PATH_TEXT], True
    text = os.path.expanduser(text)
    candidate = Path(text)
    if root is not None:
        root = root.resolve()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            return candidate.resolve(strict=False).relative_to(root).as_posix(), False
        except (OSError, ValueError):
            pass
    try:
        return candidate.resolve(strict=False).as_posix()[:MAX_PATH_TEXT], True
    except OSError:
        return text[:MAX_PATH_TEXT], True


def module_for(file: str | None) -> str | None:
    """A stable module-ish label for a file; never a package import."""
    if not file or file.startswith("<") or "://" in file or file.startswith("node:"):
        return None
    path = file.replace("\\", "/")
    for prefix in _EXTENSION_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    path = re.sub(r"\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|kts|rb|php|cs|c|cc|cpp|h|hpp|swift|scala|lua|ex|exs)$",
                  "", path)
    path = path.strip("/")
    if not path:
        return None
    parts = [part for part in path.split("/") if part and part != "."]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def frame_id(file: str | None, line: int | None, name: str) -> str:
    return "f" + sha256_text(f"{file or ''}\x00{line or 0}\x00{name}")[:16]


def _bounded_attributes(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            continue
        if len(out) >= MAX_ATTRIBUTES:
            break
        if isinstance(item, (str, bool, int)):
            out[key[:MAX_TEXT]] = (
                item[:MAX_ATTRIBUTE_TEXT] if isinstance(item, str) else item)
        elif item is None:
            out[key[:MAX_TEXT]] = None
    return out


def number_text(value: Any) -> str | None:
    """A decimal string without turning a measurement into a float in JSON."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return format(value, ".17g")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return text[:MAX_TEXT]
    return None


def _bounded_labels(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            continue
        if len(out) >= MAX_METRIC_LABELS:
            break
        text = number_text(item)
        if text is None and not isinstance(item, str):
            continue
        out[key[:MAX_TEXT]] = (text if text is not None else
                               bounded_text(str(item), name="label",
                                            limit=MAX_ATTRIBUTE_TEXT))
    return out


class ProfileBuilder:
    """Build one canonical profile record with bounded, deterministic state."""

    def __init__(self, *, source: Mapping[str, Any] | None = None) -> None:
        self.frames: dict[str, dict[str, Any]] = {}
        self.samples: dict[tuple[str, ...], int] = {}
        self.functions: dict[str, dict[str, int]] = {}
        self.spans: list[dict[str, Any]] = []
        self.metrics: list[dict[str, Any]] = []
        self.edges: dict[tuple[str, str], dict[str, Any]] = {}
        self.warnings: list[str] = []
        self.omissions: dict[str, Any] = {
            "truncated": False, "omitted": 0, "reasons": []}
        self.source = dict(source or {})
        self.clock = "unknown"
        self.sample_unit: str | None = None
        self.idle_us = 0
        self._span_ids: set[str] = set()

    def warn(self, message: str) -> None:
        text = bounded_text(message, name="warning", allow_empty=False)
        if text not in self.warnings and len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(text)

    def omit(self, reason: str, count: int = 1) -> None:
        if count <= 0:
            return
        reason = bounded_text(reason, name="omission", allow_empty=False)
        self.omissions["truncated"] = True
        self.omissions["omitted"] = int(self.omissions["omitted"]) + count
        reasons = self.omissions["reasons"]
        if reason not in reasons and len(reasons) < 8:
            reasons.append(reason)

    def frame(self, name: Any, file: str | None, line: int | None,
              *, kind: str = "function", raw_file: Any = None,
              external: bool | None = None) -> str:
        label = bounded_text(str(name) if name not in (None, "") else "(unknown)",
                             name="frame name", limit=MAX_TEXT)
        if kind not in FRAME_KINDS:
            kind = "unknown"
        if line is not None:
            line = bounded_int(line, name="frame line", low=1)
        if external is None:
            external = bool(file and (file.startswith("/") or "://" in file
                                      or file.startswith("<")))
        if len(self.frames) >= MAX_FRAMES:
            fid = frame_id(file, line, label)
            existing = self.frames.get(fid)
            if existing is not None:
                return fid
            self.omit("frame limit reached", 1)
            return frame_id(None, None, "(omitted)")
        fid = frame_id(file, line, label)
        existing = self.frames.get(fid)
        if existing is None:
            self.frames[fid] = {
                "id": fid,
                "name": label,
                "file": file,
                "line": line,
                "module": None if external else module_for(file),
                "kind": kind,
                "external": bool(external),
            }
            if raw_file not in (None, "", file):
                raw = bounded_text(str(raw_file), name="raw file",
                                   limit=MAX_PATH_TEXT, allow_empty=True)
                if raw:
                    self.frames[fid]["raw_file"] = raw
        return fid

    def sample(self, stack: Sequence[str], weight: Any, unit: str) -> None:
        if not stack or unit not in SAMPLE_UNITS:
            return
        weight_int = nonnegative_int(weight)
        if not weight_int:
            return
        if self.sample_unit is None:
            self.sample_unit = unit
        elif self.sample_unit != unit:
            raise ProfileError("one profile cannot mix sample and duration units")
        if len(self.samples) >= MAX_SAMPLES:
            key = tuple(stack)
            if key in self.samples:
                self.samples[key] += weight_int
            else:
                self.omit("sample limit reached", weight_int)
            return
        key = tuple(stack)
        self.samples[key] = self.samples.get(key, 0) + weight_int

    def function(self, frame: str, *, self_us: Any, cumulative_us: Any = None,
                 calls: Any = None, primitive_calls: Any = None) -> None:
        if frame not in self.frames:
            return
        self_us_int = nonnegative_int(self_us)
        if self_us_int is None:
            return
        cumulative = nonnegative_int(cumulative_us)
        if cumulative is None:
            cumulative = self_us_int
        call_count = bounded_int(calls, name="calls", default=0)
        primitive = bounded_int(primitive_calls, name="primitive calls",
                                default=call_count)
        if len(self.functions) >= MAX_FUNCTIONS and frame not in self.functions:
            self.omit("function limit reached", 1)
            return
        current = self.functions.get(frame)
        if current is None:
            self.functions[frame] = {
                "frame": frame,
                "self_us": self_us_int,
                "cumulative_us": cumulative,
                "calls": call_count,
                "primitive_calls": primitive,
            }
            return
        current["self_us"] += self_us_int
        current["cumulative_us"] += cumulative
        current["calls"] += call_count
        current["primitive_calls"] += primitive

    def span(self, *, name: Any, frame: str | None, service: Any,
             span_id: Any, parent_id: Any, trace_id: Any, kind: str,
             start_us: Any, duration_us: Any, status: str,
             attributes: Mapping[str, Any] | None = None) -> str | None:
        if len(self.spans) >= MAX_SPANS:
            self.omit("span limit reached", 1)
            return None
        trace = bounded_text(str(trace_id), name="trace id",
                             limit=128, allow_empty=True) if trace_id else ""
        sid = bounded_text(str(span_id), name="span id", limit=128,
                           allow_empty=True) if span_id else ""
        sid = f"{trace}:{sid}" if trace and sid else (sid or f"span{len(self.spans)}")
        if sid in self._span_ids:
            self.omit("duplicate span id", 1)
            return None
        parent = (bounded_text(str(parent_id), name="parent span id",
                               limit=128, allow_empty=True) if parent_id else "")
        parent = f"{trace}:{parent}" if trace and parent else (parent or None)
        if kind not in SPAN_KINDS:
            kind = "unknown"
        if status not in SPAN_STATUSES:
            status = "unknown"
        duration = bounded_int(duration_us, name="span duration", default=0)
        start = bounded_int(start_us, name="span start", default=0)
        self._span_ids.add(sid)
        self.spans.append({
            "id": sid,
            "parent": parent,
            "name": bounded_text(str(name) if name not in (None, "") else "(unnamed span)",
                                 name="span name", limit=MAX_TEXT),
            "frame": frame,
            "service": bounded_text(str(service), name="service",
                                    limit=MAX_TEXT, allow_empty=True) if service else "unknown",
            "kind": kind,
            "start_us": start,
            "duration_us": duration,
            "status": status,
            "attributes": _bounded_attributes(attributes),
        })
        return sid

    def metric(self, *, name: Any, kind: str, unit: Any = "",
               service: Any = "unknown", labels: Mapping[str, Any] | None = None,
               value: Any = None, count: Any = None, sum_value: Any = None,
               buckets: Sequence[Mapping[str, Any]] | None = None,
               quantiles: Sequence[Mapping[str, Any]] | None = None,
               start_us: Any = None, end_us: Any = None,
               frame: str | None = None,
               attributes: Mapping[str, Any] | None = None) -> None:
        """Append one metric point. Values are decimal strings, never floats."""
        if len(self.metrics) >= MAX_METRICS:
            self.omit("metric limit reached", 1)
            return
        if kind not in METRIC_KINDS:
            kind = "unknown"
        label_map = _bounded_labels(labels)
        value_text = number_text(value)
        sum_text = number_text(sum_value)
        count_int = nonnegative_int(count)
        bucket_rows: list[dict[str, Any]] = []
        for bucket in buckets or []:
            if not isinstance(bucket, Mapping):
                continue
            raw_le = bucket.get("le")
            if isinstance(raw_le, str) and raw_le.strip().lower() in (
                    "+inf", "inf", "infinity"):
                le = "+Inf"
            else:
                le = number_text(raw_le)
            number = nonnegative_int(bucket.get("count"))
            if le is None or number is None:
                continue
            bucket_rows.append({"le": le, "count": number})
            if len(bucket_rows) >= 1_000:
                break
        quantile_rows: list[dict[str, Any]] = []
        for quantile in quantiles or []:
            if not isinstance(quantile, Mapping):
                continue
            q = number_text(quantile.get("q"))
            q_value = number_text(quantile.get("value"))
            if q is None or q_value is None:
                continue
            quantile_rows.append({"q": q, "value": q_value})
            if len(quantile_rows) >= 20:
                break
        if all(item is None for item in
               (value_text, count_int, sum_text)) and not bucket_rows and not quantile_rows:
            return
        self.metrics.append({
            "name": bounded_text(str(name or "(unnamed metric)"),
                                 name="metric name", limit=MAX_TEXT),
            "kind": kind,
            "unit": bounded_text(str(unit), name="metric unit", limit=80,
                                 allow_empty=True),
            "service": bounded_text(str(service), name="metric service",
                                    limit=MAX_TEXT, allow_empty=True) if service else "unknown",
            "labels": label_map,
            "value": value_text,
            "count": count_int,
            "sum": sum_text,
            "buckets": bucket_rows,
            "quantiles": quantile_rows,
            "start_us": bounded_int(start_us, name="metric start", default=None),
            "end_us": bounded_int(end_us, name="metric end", default=None),
            "frame": frame if frame in self.frames else None,
            "attributes": _bounded_attributes(attributes),
        })

    def edge(self, caller: str, callee: str, *, calls: Any = None,
             resolution: str = "profile") -> None:
        if caller not in self.frames or callee not in self.frames or caller == callee:
            return
        if len(self.edges) >= MAX_EDGES and (caller, callee) not in self.edges:
            self.omit("edge limit reached", 1)
            return
        key = (caller, callee)
        current = self.edges.get(key)
        count = bounded_int(calls, name="edge calls", default=0)
        if current is None:
            self.edges[key] = {"caller": caller, "callee": callee,
                               "calls": count, "resolution": resolution}
        else:
            current["calls"] += count

    def build(self) -> dict[str, Any]:
        frames = sorted(self.frames.values(),
                        key=lambda item: (item.get("file") or "",
                                          item.get("line") or 0,
                                          item.get("name") or "",
                                          item["id"]))
        samples = [{"stack": list(stack), "weight": int(weight),
                    "unit": self.sample_unit}
                   for stack, weight in sorted(self.samples.items())]
        functions = [self.functions[key] for key in sorted(self.functions)]
        spans = sorted(self.spans, key=lambda item: (item["start_us"],
                                                     item["id"]))
        metrics = sorted(
            self.metrics,
            key=lambda item: (item["name"], item["service"],
                              json.dumps(item["labels"], sort_keys=True),
                              item.get("end_us") or 0,
                              item.get("start_us") or 0,
                              item["kind"]))
        edges = [self.edges[key] for key in sorted(self.edges)]
        totals = {
            "frames": len(frames),
            "samples": len(samples),
            "functions": len(functions),
            "spans": len(spans),
            "metrics": len(metrics),
            "edges": len(edges),
            "sample_weight": int(sum(item["weight"] for item in samples)),
            "self_us": int(sum(item["self_us"] for item in functions)),
            "idle_us": int(self.idle_us),
        }
        return {
            "schema": {"namespace": SCHEMA_NAMESPACE, "version": VERSION},
            "source": dict(self.source),
            "scope": {},
            "clock": self.clock,
            "sample_unit": self.sample_unit,
            "frames": frames,
            "samples": samples,
            "functions": functions,
            "spans": spans,
            "metrics": metrics,
            "edges": edges,
            "totals": totals,
            "warnings": list(self.warnings),
            "omissions": dict(self.omissions),
        }


def profile_hash(profile: Mapping[str, Any]) -> str:
    """Hash the profile content, excluding transient fields."""
    value = {key: item for key, item in profile.items()
             if key not in ("created_at", "profile_hash")}
    return sha256_text(canonical_json(value))


def validate_profile(profile: Mapping[str, Any]) -> None:
    if not isinstance(profile, Mapping):
        raise ProfileError("profile must be an object")
    schema = profile.get("schema")
    if not isinstance(schema, Mapping):
        raise ProfileError("profile.schema must be an object")
    if schema.get("namespace") != SCHEMA_NAMESPACE:
        raise ProfileError("unknown profile namespace")
    if schema.get("version") != VERSION:
        raise ProfileError(f"unsupported profile version {schema.get('version')!r}")
    for key in ("frames", "samples", "functions", "spans", "metrics", "edges"):
        if not isinstance(profile.get(key), list):
            raise ProfileError(f"profile.{key} must be a list")
    if profile.get("sample_unit") not in (None, *SAMPLE_UNITS):
        raise ProfileError("profile.sample_unit is invalid")


__all__ = [
    "FormatError", "METRIC_KINDS", "MAX_METRICS", "ProfileBuilder",
    "ProfileError", "REPORT_NAMESPACE", "SCHEMA_NAMESPACE", "VERSION",
    "bounded_int", "bounded_text", "canonical_json", "file_digest", "frame_id",
    "module_for", "nonnegative_int", "normalize_path", "ns_to_us", "number_text",
    "profile_hash", "read_bytes_bounded", "read_json_bounded", "seconds_to_us",
    "sha256_text", "unit_to_us", "validate_profile",
]
