"""Tests for offline application profiling and telemetry joins."""

from __future__ import annotations

import cProfile
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ozagia import cli, profile_cli
from ozagia.core import profile as P
from ozagia.core.profile import GraphIndex, analyze, ingest_file
from ozagia.core.profile import collect
from ozagia.core.deep_review.analyzers import profiles as deep_review_profiles


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_deep_review_profile_report_read_is_bounded(tmp_path, monkeypatch):
    path = _write(tmp_path, "report.json", "12345")
    monkeypatch.setattr(deep_review_profiles, "MAX_REPORT_BYTES", 4)

    with pytest.raises(ValueError, match="exceeds 4 byte limit"):
        deep_review_profiles._load_report(tmp_path, path)


def _tags() -> dict:
    return {
        "root_hash": "profile-test",
        "files": {
            "src/app.py": {
                "tags": [
                    {"kind": "def", "name": "foo", "qualname": "foo",
                     "line": 1, "end_line": 3, "detail": "definition.function"},
                    {"kind": "def", "name": "bar", "qualname": "bar",
                     "line": 5, "end_line": 6, "detail": "definition.function"},
                    {"kind": "ref", "name": "foo", "scope": "bar", "line": 6,
                     "detail": "reference.call"},
                ],
            },
        },
    }


def _speedscope(root: Path, *, weights: list[int], unit: str = "microseconds") -> Path:
    data = {
        "shared": {
            "frames": [
                {"name": "foo", "file": "src/app.py", "line": 2},
                {"name": "bar", "file": "src/app.py", "line": 5},
            ],
        },
        "profiles": [{
            "type": "sampled",
            "name": "test",
            "unit": unit,
            "samples": [[0, 1], [1], [0]],
            "weights": weights,
        }],
    }
    return _write(root, "app.speedscope", json.dumps(data))


def _profile_json(root: Path, name: str, data: dict) -> Path:
    return _write(root, name, json.dumps(data))


def test_speedscope_ingest_joins_frames_to_graph(tmp_path):
    root = tmp_path
    _write(root, "src/app.py", "def foo():\n    return 1\n\ndef bar():\n    return foo()\n")
    path = _speedscope(root, weights=[100, 50, 25])
    profile = ingest_file(path, root=root)
    assert profile["source"]["kind"] == "speedscope"
    assert profile["sample_unit"] == "microseconds"
    assert profile["totals"]["sample_weight"] == 175
    assert len(profile["frames"]) == 2

    graph = GraphIndex(root, tags=_tags())
    result = analyze(profile, graph, top=5)
    rows = {row["name"]: row for row in result["hotspots"]}
    assert rows["foo"]["graph"]["symbol"] == "src/app.py::foo"
    assert rows["foo"]["graph"]["callers"] == 1
    assert rows["foo"]["graph"]["impact_symbols"] == 1
    assert rows["bar"]["self"] == 150
    assert result["measurement"]["total_self"] == 175
    assert result["measurement"]["truth_assessed"] is False
    assert result["coverage"]["mapped_self_bp"] == 10_000
    assert result["paths"][0]["stack"][-1].startswith("bar")


def test_cprofile_ingest_keeps_self_cumulative_and_calls(tmp_path):
    root = tmp_path
    source = _write(root, "src/app.py",
                    "def foo():\n"
                    "    total = 0\n"
                    "    for i in range(20_000):\n"
                    "        total += i\n"
                    "    return total\n\n"
                    "def bar():\n"
                    "    return foo() + foo()\n")
    spec = importlib.util.spec_from_file_location("profile_fixture", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    profiler = cProfile.Profile()
    profiler.enable()
    module.bar()
    profiler.disable()
    path = root / "app.prof"
    profiler.dump_stats(str(path))

    profile = ingest_file(path, root=root)
    assert profile["source"]["kind"] == "cprofile"
    assert profile["clock"] == "wall"
    by_name = {frame["name"]: frame for frame in profile["frames"]}
    assert "foo" in by_name and "bar" in by_name
    functions = {row["frame"]: row for row in profile["functions"]}
    foo_id = by_name["foo"]["id"]
    assert functions[foo_id]["calls"] == 2
    assert functions[foo_id]["self_us"] > 0

    graph = GraphIndex(root, tags=_tags())
    result = analyze(profile, graph, top=10)
    rows = {row["name"]: row for row in result["hotspots"]}
    assert rows["foo"]["graph"]["symbol"] == "src/app.py::foo"
    assert rows["foo"]["calls"] == 2
    assert result["measurement"]["kind"] == "functions"


def test_chrome_cpuprofile_excludes_idle_and_normalizes_file_url(tmp_path):
    root = tmp_path
    source = _write(root, "src/app.py", "def foo():\n    return 1\n")
    url = source.as_uri()
    data = {
        "nodes": [
            {"id": 1, "parent": None,
             "callFrame": {"functionName": "(root)", "url": ""}},
            {"id": 2, "parent": 1,
             "callFrame": {"functionName": "foo", "url": url,
                           "lineNumber": 0}},
            {"id": 3, "parent": 1,
             "callFrame": {"functionName": "(idle)", "url": "",
                           "lineNumber": -1}},
        ],
        "samples": [2, 3],
        "timeDeltas": [100, 250],
    }
    path = _profile_json(root, "app.cpuprofile", data)
    profile = ingest_file(path, root=root)
    assert profile["totals"]["idle_us"] == 250
    assert profile["totals"]["sample_weight"] == 100
    frame = next(frame for frame in profile["frames"] if frame["name"] == "foo")
    assert frame["file"] == "src/app.py"
    assert frame["line"] == 1
    result = analyze(profile, GraphIndex(root, tags=_tags()), top=5)
    assert result["measurement"]["total_self"] == 100
    assert result["coverage"]["mapped_self_bp"] == 10_000


def test_collapsed_stacks_support_sample_and_duration_weights(tmp_path):
    root = tmp_path
    _write(root, "src/app.py", "def foo():\n    return 1\n")
    text = ("foo (src/app.py:2);bar (src/app.py:5) 10\n"
            "foo (src/app.py:2) 5\n")
    path = _write(root, "app.folded", text)
    samples = ingest_file(path, root=root, weight_unit="samples")
    assert samples["sample_unit"] == "samples"
    assert samples["totals"]["sample_weight"] == 15

    converted = ingest_file(path, root=root, interval_us=1_000)
    assert converted["sample_unit"] == "microseconds"
    assert converted["totals"]["sample_weight"] == 15_000

    millis = ingest_file(path, root=root, weight_unit="milliseconds")
    assert millis["totals"]["sample_weight"] == 15_000


def test_otlp_metrics_report_gauges_counters_and_histograms(tmp_path):
    root = tmp_path
    _write(root, "src/app.py", "def foo():\n    return 1\n")
    data = {
        "resourceMetrics": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "items-api"}}]},
            "scopeMetrics": [{"metrics": [
                {"name": "queue.depth", "unit": "1", "gauge": {"dataPoints": [
                    {"asInt": "7", "timeUnixNano": "2000000",
                     "attributes": [{"key": "queue",
                                     "value": {"stringValue": "default"}}]},
                ]}},
                {"name": "requests", "unit": "1",
                 "sum": {"isMonotonic": True, "dataPoints": [
                     {"asInt": "10", "timeUnixNano": "1000000"},
                     {"asInt": "15", "timeUnixNano": "2000000"},
                 ]}},
                {"name": "http.duration", "unit": "ms",
                 "histogram": {"dataPoints": [{
                     "count": "10", "sum": "3.5",
                     "explicitBounds": [1, 10],
                     "bucketCounts": ["3", "6", "1"],
                     "timeUnixNano": "3000000",
                     "attributes": [
                         {"key": "code.filepath",
                          "value": {"stringValue": "src/app.py"}},
                         {"key": "code.lineno", "value": {"intValue": "5"}},
                     ],
                 }]}},
            ]}],
        }],
    }
    path = _profile_json(root, "metrics.otlp.json", data)
    profile = ingest_file(path, root=root)
    assert profile["source"]["kind"] == "otlp_metrics"
    assert profile["totals"]["metrics"] == 4
    result = analyze(profile, GraphIndex(root, tags=_tags()), top=10)
    metrics = result["metrics"]
    assert metrics["series_count"] == 3
    by_name = {row["name"]: row for row in metrics["series"]}
    assert by_name["queue.depth"]["value"] == "7"
    assert by_name["requests"]["delta"] == "5"
    assert by_name["http.duration"]["p50"] == "10"
    assert by_name["http.duration"]["p95"] == "+Inf"
    assert by_name["http.duration"]["graph"]["symbol"] == "src/app.py::bar"
    report = P.build_report_from_profile(profile, root,
                                         graph=GraphIndex(root, tags=_tags()),
                                         top=10)
    assert "## Metrics" in P.format_report(report)


def test_prometheus_text_reports_counters_and_histograms(tmp_path):
    root = tmp_path
    text = """\
# HELP http_requests_total requests
# TYPE http_requests_total counter
http_requests_total{service="api",code="200"} 12
# TYPE request_duration_seconds histogram
request_duration_seconds_bucket{service="api",le="0.1"} 5
request_duration_seconds_bucket{service="api",le="0.5"} 9
request_duration_seconds_bucket{service="api",le="+Inf"} 10
request_duration_seconds_sum{service="api"} 2.5
request_duration_seconds_count{service="api"} 10
# TYPE cache_ratio gauge
cache_ratio{service="api",code_filepath="src/app.py",code_lineno="2"} 0.75
# TYPE rpc_duration_seconds summary
rpc_duration_seconds{service="api",quantile="0.5"} 0.05
rpc_duration_seconds{service="api",quantile="0.95"} 0.2
rpc_duration_seconds_sum{service="api"} 1.5
rpc_duration_seconds_count{service="api"} 10
"""
    path = _write(root, "app.prom", text)
    assert P.detect_format(path) == "prometheus_text"
    profile = ingest_file(path, root=root)
    assert profile["source"]["kind"] == "prometheus_text"
    assert profile["totals"]["metrics"] == 4
    result = analyze(profile, GraphIndex(root, tags=_tags()), top=10)
    by_name = {row["name"]: row for row in result["metrics"]["series"]}
    assert by_name["http_requests_total"]["value"] == "12"
    assert by_name["request_duration_seconds"]["p50"] == "0.1"
    assert by_name["request_duration_seconds"]["p95"] == "+Inf"
    assert by_name["rpc_duration_seconds"]["p50"] == "0.05"
    assert by_name["rpc_duration_seconds"]["p95"] == "0.2"
    assert by_name["cache_ratio"]["graph"]["status"] == "mapped"
    assert by_name["cache_ratio"]["graph"]["symbol"] == "src/app.py::foo"


def test_speedscope_evented_stacks_accumulate_intervals(tmp_path):
    root = tmp_path
    _write(root, "src/app.py", "def foo():\n    return 1\n")
    data = {
        "shared": {"frames": [
            {"name": "foo", "file": "src/app.py", "line": 1},
            {"name": "bar", "file": "src/app.py", "line": 5},
        ]},
        "profiles": [{
            "type": "evented", "name": "events", "unit": "microseconds",
            "startValue": 0,
            "events": [
                {"type": "O", "at": 0, "frame": 0},
                {"type": "O", "at": 10, "frame": 1},
                {"type": "C", "at": 20, "frame": 1},
                {"type": "C", "at": 30, "frame": 0},
            ],
        }],
    }
    path = _profile_json(root, "events.speedscope", data)
    profile = ingest_file(path, root=root)
    assert profile["totals"]["sample_weight"] == 30
    frame = next(frame for frame in profile["frames"] if frame["name"] == "bar")
    result = analyze(profile, GraphIndex(root, tags=_tags()), top=5)
    row = next(row for row in result["hotspots"] if row["frame"] == frame["id"])
    assert row["self"] == 10


def test_otlp_jsonl_lines_are_merged(tmp_path):
    root = tmp_path
    span = {
        "traceId": "trace-1", "spanId": "span-1", "name": "op",
        "kind": 2, "startTimeUnixNano": "0", "endTimeUnixNano": "1000000",
        "status": {"code": 1},
    }
    line = json.dumps({"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "svc"}}]},
        "scopeSpans": [{"spans": [span]}],
    }]})
    path = _write(root, "trace.jsonl", line + "\n" + line.replace('"span-1"', '"span-2"') + "\n")
    profile = ingest_file(path, root=root, format="otlp_json")
    assert profile["totals"]["spans"] == 2
    result = analyze(profile, None, top=5)
    assert result["telemetry"]["services"] == ["svc"]


def test_graph_open_builds_a_missing_tag_cache_once(tmp_path):
    root = tmp_path
    _write(root, "src/app.py", "def foo():\n    return 1\n")
    graph = GraphIndex.open(root, refresh=False)
    assert graph.source == "tag_cache"
    assert graph.defs["src/app.py"]
    cache = root / ".ozagia" / "tags.json"
    assert cache.is_file()
    before = cache.stat().st_mtime_ns
    again = GraphIndex.open(root, refresh=False)
    assert again.defs["src/app.py"]
    assert cache.stat().st_mtime_ns == before


def test_graph_does_not_map_a_name_from_another_file(tmp_path):
    root = tmp_path
    _write(root, "src/other.py", "def foo():\n    return 1\n")
    path = _speedscope(root, weights=[10, 10, 10])
    profile = ingest_file(path, root=root)
    graph = GraphIndex(root, tags={
        "root_hash": "x",
        "files": {"src/other.py": {"tags": [
            {"kind": "def", "name": "foo", "qualname": "foo",
             "line": 1, "end_line": 2, "detail": "definition.function"},
        ]}},
    })
    result = analyze(profile, graph, top=5)
    row = next(row for row in result["hotspots"] if row["name"] == "foo")
    assert row["graph"]["status"] == "unresolved"


def test_otlp_json_keeps_spans_errors_and_allowlisted_attributes(tmp_path):
    root = tmp_path
    _write(root, "src/app.py", "def foo():\n    return 1\n")
    data = {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": "items-api"}},
            ]},
            "scopeSpans": [{"spans": [
                {
                    "traceId": "trace-1", "spanId": "span-1",
                    "name": "GET /items", "kind": 2,
                    "startTimeUnixNano": "1000000",
                    "endTimeUnixNano": "3000000",
                    "status": {"code": 1},
                    "attributes": [
                        {"key": "code.filepath",
                         "value": {"stringValue": "src/app.py"}},
                        {"key": "code.lineno", "value": {"intValue": "5"}},
                        {"key": "http.route",
                         "value": {"stringValue": "/items"}},
                        {"key": "secret", "value": {"stringValue": "x"}},
                    ],
                },
                {
                    "traceId": "trace-1", "spanId": "span-2",
                    "parentSpanId": "span-1", "name": "db.query", "kind": 3,
                    "startTimeUnixNano": "1500000",
                    "endTimeUnixNano": "2500000",
                    "status": {"code": 2},
                    "attributes": [
                        {"key": "db.system",
                         "value": {"stringValue": "postgres"}},
                    ],
                },
            ]}],
        }],
    }
    path = _profile_json(root, "trace.otlp.json", data)
    profile = ingest_file(path, root=root)
    assert profile["source"]["kind"] == "otlp_json"
    assert profile["totals"]["spans"] == 2
    assert any("non-allowlisted OTLP attributes" in item
               for item in profile["warnings"])
    assert "secret" not in json.dumps(profile)
    result = analyze(profile, GraphIndex(root, tags=_tags()), top=5)
    assert result["telemetry"]["error_spans"] == 1
    assert result["telemetry"]["services"] == ["items-api"]
    assert result["telemetry"]["operations"][0]["service"] == "items-api"
    rows = {row["name"]: row for row in result["hotspots"]}
    assert rows["GET /items"]["graph"]["symbol"] == "src/app.py::bar"
    assert result["measurement"]["total_self"] == 2_000


def test_baseline_comparison_matches_by_symbol_not_line(tmp_path):
    root = tmp_path
    _write(root, "src/app.py", "def foo():\n    return 1\n\ndef bar():\n    return foo()\n")
    before = _speedscope(root, weights=[100, 50, 25])
    after_path = root / "app.after.speedscope"
    after_path.write_text(before.read_text(encoding="utf-8").replace(
        '"weights": [100, 50, 25]', '"weights": [300, 50, 25]'),
        encoding="utf-8")
    graph = GraphIndex(root, tags=_tags())
    base_report = P.build_report_from_profile(
        ingest_file(before, root=root), root, graph=graph, top=20)
    current_report = P.build_report_from_profile(
        ingest_file(after_path, root=root), root, graph=graph, top=20)
    comparison = P.compare_reports(base_report, current_report, top=5)
    assert comparison["compatible"] is True
    assert comparison["self_delta"] == 200
    assert any(row["symbol"] == "src/app.py::bar"
               for row in comparison["regressions"])


def test_report_is_deterministic_and_writes_both_files(tmp_path):
    root = tmp_path
    _write(root, "src/app.py", "def foo():\n    return 1\n")
    path = _speedscope(root, weights=[5, 5, 5])
    profile = ingest_file(path, root=root)
    report = P.build_report_from_profile(
        profile, root, graph=GraphIndex(root, tags=_tags()), top=5)
    again = P.build_report_from_profile(
        profile, root, graph=GraphIndex(root, tags=_tags()), top=5)
    assert report["report_hash"] == again["report_hash"]
    assert report["schema"]["namespace"] == "ozagia.profile_report"
    assert "src/app.py::foo" in P.format_report(report)
    paths = P.write_report(report, root / "out")
    assert Path(paths["json"]).is_file()
    assert Path(paths["markdown"]).is_file()
    assert json.loads(Path(paths["json"]).read_text())["report_hash"] == report["report_hash"]


def test_guided_collection_requires_explicit_execution(tmp_path):
    root = tmp_path
    _write(root, "work.py", "print('hello')\n")
    with pytest.raises(P.CollectionRefused):
        P.run_collection(root, tool="cprofile",
                         command=[sys.executable, str(root / "work.py")],
                         out_dir=root / "out", allow_exec=False)


def test_guided_collection_builds_argv_without_a_shell(tmp_path):
    root = tmp_path
    out = root / "out"
    argv, artifact = collect.build_command(
        "cprofile", [sys.executable, "-m", "json.tool"], out / "artifact")
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "cProfile", "-o"]
    assert argv[4] == str(artifact)
    assert argv[5:] == ["-m", "json.tool"]
    assert artifact.name == "profile.prof"

    node, node_artifact = collect.build_command(
        "node", ["node", "app.js"], out / "node")
    assert node[node.index("--cpu-prof")] == "--cpu-prof"
    assert node_artifact.name == "profile.cpuprofile"


def test_guided_cprofile_run_writes_artifact_and_report(tmp_path):
    root = tmp_path
    script = _write(root, "work.py",
                    "def spin():\n"
                    "    total = 0\n"
                    "    for i in range(20_000):\n"
                    "        total += i\n"
                    "    return total\n\n"
                    "spin()\n")
    out = root / "collection"
    # This exercises collection, not the host's free-space or memory
    # admission policy. Supply explicit test floors so the result does not
    # depend on the size, occupancy or memory pressure of the host running
    # the suite.
    from copy import deepcopy
    from ozagia.core.resources import DEFAULT_POLICY
    policy = deepcopy(DEFAULT_POLICY)
    policy["reserve_memory_pct"] = 0
    policy["reserve_disk_pct"] = 0
    policy["reserve_disk_gb"] = 0
    policy["admission"]["min_free_memory_pct"] = 0
    policy["admission"]["min_free_disk_gb"] = 0
    profile, run = P.run_collection(
        root, tool="cprofile", command=[sys.executable, str(script)],
        out_dir=out, allow_exec=True, timeout_s=30, policy=policy,
        memory_mb=256, disk_mb=64)
    assert run["returncode"] == 0
    assert run["timed_out"] is False
    assert Path(run["artifact"]["path"]).is_file()
    assert profile["source"]["kind"] == "cprofile"
    report = P.build_report_from_profile(
        profile, root, graph=GraphIndex(root, tags=_tags()),
        collection=run, top=5)
    assert report["collection"]["tool"] == "cprofile"
    assert report["report_hash"]
    assert "Collection:" in P.format_report(report)
    assert Path(P.write_report(report, out)["json"]).is_file()


def test_deep_review_consumes_a_measured_profile_report(tmp_path):
    from ozagia.core.deep_review import build_deep_review

    root = tmp_path
    _write(root, "src/app.py", "def foo():\n    return 1\n")
    path = _speedscope(root, weights=[100, 10, 10])
    profile = ingest_file(path, root=root)
    report = P.build_report_from_profile(
        profile, root, graph=GraphIndex(root, tags=_tags()), top=5)
    report_path = root / "profile.json"
    report_path.write_text(P.report_json(report), encoding="utf-8")
    out = root / "review"
    result = build_deep_review(root, out, profile="performance", budget_s=10,
                               profile_reports=[str(report_path)])
    findings = json.loads((out / "findings.json").read_text(encoding="utf-8"))
    measured = [item for item in findings
                if item["type"] in {"measured_hotspot", "measured_regression"}]
    assert measured
    assert any(item["evidence"][0]["source"] == "ozagia.core.profile"
               for item in measured)
    assert result["manifest"]["subjects"]["performance"]["profile_reports"]

    # Passing the artifact itself exercises the read-only report fallback.
    # Building a graph there would create `.ozagia/tags.json`; a review that
    # only reads supplied evidence must write nothing outside its own output.
    before = {item.relative_to(root).as_posix()
              for item in root.rglob("*") if item.is_file()}
    artifact_out = root / "review-artifact"
    result = build_deep_review(root, artifact_out, profile="performance",
                               budget_s=10, profile_reports=[str(path)])
    after = {item.relative_to(root).as_posix()
             for item in root.rglob("*") if item.is_file()}
    assert all(item.startswith("review-artifact/") for item in after - before)
    assert not (root / ".ozagia" / "tags.json").exists()
    findings = json.loads((artifact_out / "findings.json").read_text(
        encoding="utf-8"))
    measured = [item for item in findings
                if item["type"] in {"measured_hotspot", "measured_regression"}]
    assert measured
    assert any(item["evidence"][0]["source"] == "ozagia.core.profile"
               for item in measured)
    assert result["manifest"]["subjects"]["performance"]["profile_reports"]

    # A binary cProfile artifact takes the same read-only fallback; decoding
    # failure must not abort the review or trigger a graph build.
    profiler = cProfile.Profile()
    profiler.enable()
    sum(range(10_000))
    profiler.disable()
    prof_path = root / "app.prof"
    profiler.dump_stats(str(prof_path))
    before = {item.relative_to(root).as_posix()
              for item in root.rglob("*") if item.is_file()}
    binary_out = root / "review-binary"
    build_deep_review(root, binary_out, profile="performance", budget_s=10,
                      profile_reports=[str(prof_path)])
    after = {item.relative_to(root).as_posix()
             for item in root.rglob("*") if item.is_file()}
    assert all(item.startswith("review-binary/") for item in after - before)
    assert not (root / ".ozagia" / "tags.json").exists()
    findings = json.loads((binary_out / "findings.json").read_text(
        encoding="utf-8"))
    assert any(item["type"] == "measured_hotspot" for item in findings)
    assert all(item["evidence"][0]["source"] == "ozagia.core.profile"
               for item in findings if item["type"] == "measured_hotspot")


def test_run_parser_keeps_the_subcommand_and_remainder_separate():
    args = profile_cli.build_parser().parse_args([
        "run", "--tool", "cprofile", "--allow-exec", "--",
        "python", "app.py"])
    assert args.command == "run"
    assert args.profiler_command == ["--", "python", "app.py"]
    assert args.allow_exec is True


def test_cli_ingest_compare_and_errors(tmp_path, capsys):
    root = tmp_path
    _write(root, "src/app.py", "def foo():\n    return 1\n")
    path = _speedscope(root, weights=[10, 20, 30])
    assert profile_cli.main(["formats", "--json"]) == 0
    assert "cprofile" in capsys.readouterr().out
    assert profile_cli.main(["ingest", str(path), "--root", str(root),
                             "--no-graph", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"]["namespace"] == "ozagia.profile_report"
    assert profile_cli.main(["ingest", str(root / "missing.prof"),
                             "--root", str(root)]) == 2
    assert profile_cli.main(["compare", str(path), str(path), "--root", str(root),
                             "--no-graph", "--json"]) == 0
    comparison = json.loads(capsys.readouterr().out)
    assert comparison["compatible"] is True


def test_profile_command_is_registered():
    parser = cli.build_parser()
    sub = next(action for action in parser._actions
               if hasattr(action, "choices") and action.choices)
    assert "profile" in sub.choices
