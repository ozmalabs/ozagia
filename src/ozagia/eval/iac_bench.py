"""Deterministic offline benchmark for the experimental IaC graph."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import tempfile
import time
import tracemalloc
from typing import Any, Iterator

SCHEMA_VERSION = 1
DEFAULT_WORKSPACE = Path("/tmp/ozagia_iac_bench/repo")
DEFAULT_OUT = Path("/tmp/ozagia_iac_bench/report.json")
ADAPTER_REF = "ozagia.integrations.iac.adapter:register"
TF_CANARY = "IAC_TF_SECRET_CANARY_72A6"
K8S_CANARY = "IAC_K8S_SECRET_CANARY_91D3"
SCOPE = "synthetic offline benchmark; not production latency or model token evidence"


class BenchmarkError(RuntimeError):
    def __init__(self, stage: str, error_type: str):
        super().__init__(f"benchmark failed during {stage} ({error_type})")
        self.stage = stage
        self.error_type = error_type


def percentile(samples: list[float], percent: float) -> float | None:
    if not samples:
        return None
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    ordered = sorted(float(sample) for sample in samples)
    rank = max(1, math.ceil(percent * len(ordered) / 100))
    return ordered[rank - 1]


@contextmanager
def _benchmark_environment(config: Path) -> Iterator[None]:
    names = ("OZAGIA_CONFIG", "OZAGIA_ADAPTERS")
    previous = {name: os.environ.get(name) for name in names}
    os.environ["OZAGIA_CONFIG"] = str(config.resolve())
    os.environ["OZAGIA_ADAPTERS"] = ""
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _terraform_sources(root: Path, count: int, seed: int) -> list[str]:
    files: list[tuple[str, str]] = [("infra/network/base.tf", f'''variable "region" {{ default = "us-test-1" }}
locals {{
  prefix = "bench"
}}
data "aws_ami" "base" {{ most_recent = true }}
resource "aws_vpc" "core" {{
 cidr_block = "10.42.0.0/16"
tags = {{ Name = local.prefix }}
}}
resource "aws_db_instance" "canary_fixture" {{ password = "{TF_CANARY}" }}
output "vpc_id" {{ value = aws_vpc.core.id }}
module "app" {{
 source = "./modules/app"
 vpc_id = aws_vpc.core.id
}}
''')]
    for index in range(1, count):
        if index == 1:
            rel = "infra/network/modules/app/main.tf"
            text = (
                'variable "vpc_id" { type = string }\n'
                'resource "aws_security_group" "app" {\n'
                '  vpc_id = var.vpc_id\n'
                f'  name = "app-{seed}"\n'
                '}\n'
            )
        else:
            rel = f"infra/network/subnet-{index:04d}.tf"
            text = (
                f'resource "aws_subnet" "subnet_{index}" {{\n'
                '  vpc_id = aws_vpc.core.id\n'
                f'  cidr_block = "10.42.{index % 255}.0/24"\n'
                '}\n'
                f'output "subnet_{index}_id" {{ value = aws_subnet.subnet_{index}.id }}\n'
            )
        files.append((rel, text))
    for rel, text in files:
        _write(root / rel, text)
    return [rel for rel, _text in files]


def _kubernetes_sources(root: Path, count: int, seed: int) -> list[str]:
    paths = []
    for index in range(count):
        ns, app = f"bench-{seed}-{index:03d}", f"payments-{seed}-{index:03d}"
        text = f'''apiVersion: v1
kind: Namespace
metadata: {{name: {ns}}}
---
apiVersion: v1
kind: ServiceAccount
metadata: {{name: {app}-identity, namespace: {ns}}}
---
apiVersion: v1
kind: ConfigMap
metadata: {{name: {app}-settings, namespace: {ns}}}
data: {{LOG_LEVEL: info}}
---
apiVersion: v1
kind: Secret
metadata: {{name: {app}-credentials, namespace: {ns}}}
stringData: {{password: {K8S_CANARY}}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: {app}, namespace: {ns}}}
spec: {{selector: {{matchLabels: {{app: {app}}}}}, template: {{metadata: {{labels: {{app: {app}}}}}, spec: {{serviceAccountName: {app}-identity, containers: [{{name: {app}, image: example.invalid/payments:v1, envFrom: [{{configMapRef: {{name: {app}-settings}}}}, {{secretRef: {{name: {app}-credentials}}}}]}}]}}}}}}
---
apiVersion: v1
kind: Service
metadata: {{name: {app}-service, namespace: {ns}}}
spec: {{selector: {{app: {app}}}, ports: [{{port: 80, targetPort: 8080}}]}}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {{name: {app}-ingress, namespace: {ns}}}
spec: {{rules: [{{host: payments.example.invalid, http: {{paths: [{{path: /, pathType: Prefix, backend: {{service: {{name: {app}-service, port: {{number: 80}}}}}}}}]}}}}]}}
'''
        rel = f"k8s/stack-{index:04d}.yaml"
        _write(root / rel, text)
        paths.append(rel)
    return paths


def _git_fixture(root: Path) -> None:
    commands = (["git", "init", "-q", str(root)], ["git", "-C", str(root), "add", "."],
                ["git", "-C", str(root), "-c", "user.name=IaC benchmark",
                 "-c", "user.email=iac-bench@example.invalid", "commit", "-qm",
                 "synthetic fixture"])
    for command in commands:
        subprocess.run(command, check=True, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)


def _cold_build(root: Path, dump: Path, config: Path, *, enabled: bool,
                clear_cache: bool = True) -> dict[str, Any]:
    from ozagia.adapters import registry
    from ozagia.core.memory import build_index

    with _benchmark_environment(config):
        active = registry(root, reload=True)
        iac_loaded = any(
            getattr(indexer, "__module__", "") == "ozagia.integrations.iac.adapter"
            for indexer in active.indexers
        )
        if iac_loaded is not enabled:
            raise RuntimeError("requested adapter state did not load")
        # Wall time is measured without tracemalloc, which adds substantial
        # per-allocation overhead. Peak memory comes from a second, cold-enough
        # build after clearing the adapter's per-file parse cache.
        started = time.perf_counter()
        stats = build_index(root, dump, include_docs="none", include_git=False)
        wall_seconds = time.perf_counter() - started
        from ozagia.integrations.iac import adapter as iac_adapter
        if clear_cache:
            iac_adapter.clear_parse_cache()
        was_tracing = tracemalloc.is_tracing()
        if not was_tracing:
            tracemalloc.start()
        tracemalloc.reset_peak()
        traced_started = time.perf_counter()
        try:
            build_index(root, dump, include_docs="none", include_git=False)
        finally:
            traced_wall_seconds = time.perf_counter() - traced_started
            _current, peak = tracemalloc.get_traced_memory()
            if not was_tracing:
                tracemalloc.stop()
        graph = json.loads(dump.read_text(encoding="utf-8"))
        infra_resources = sum(n.get("type") == "InfraResource" for n in graph.get("nodes", []))
        kubernetes_resources = sum(n.get("type") == "InfraResource" and n.get("attrs", {}).get("family") == "kubernetes" for n in graph.get("nodes", []))
        infra_edges = sum(str(e.get("type", "")).startswith("infra_") for e in graph.get("edges", []))
        return {"wall_seconds": wall_seconds,
                "tracemalloc_wall_seconds": traced_wall_seconds,
                "peak_tracemalloc_bytes": peak,
                "nodes": stats["nodes"], "edges": stats["edges"],
                "infra_resources": infra_resources, "kubernetes_resources": kubernetes_resources,
                "infra_edges": infra_edges,
                "snapshot_bytes": dump.stat().st_size, "adapter_enabled": enabled,
                "iac_adapter_loaded": iac_loaded}


def _query_latency(memory: Any, query_names: list[str], query_count: int, max_chars: int, canaries: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, Any]]:
    rows: dict[str, Any] = {}
    context_sizes: list[int] = []
    estimated_tokens: list[float] = []
    response_text: list[str] = []

    def invoke(kind: str, name: str) -> tuple[float, dict[str, Any]]:
        if kind == "locate":
            args = {"query": name, "limit": 20}
        elif kind == "context":
            args = {"query": f"{name} service deployment", "max_chars": max_chars}
        else:
            located = memory.adapter_query("infra.locate", query=name, limit=1)
            results = located.get("results") or []
            key = results[0].get("key") if results else None
            if not key:
                raise RuntimeError("generated benchmark query did not resolve")
            args = {"key": key, "depth": 2, "limit": 20}
        started = time.perf_counter()
        result = memory.adapter_query(f"infra.{kind}", **args)
        return time.perf_counter() - started, result

    first_call: dict[str, float] = {}
    for kind in ("locate", "context", "dependencies"):
        elapsed, result = invoke(kind, query_names[0])
        first_call[kind] = elapsed
        response_text.append(json.dumps(result, sort_keys=True))
        if kind == "context":
            context = str(result.get("context", ""))
            context_sizes.append(len(context))
            estimated_tokens.append(len(context) / 4)

    for kind in ("locate", "context", "dependencies"):
        samples: list[float] = []
        result_counts: list[int] = []
        for index in range(query_count):
            elapsed, result = invoke(kind, query_names[index % len(query_names)])
            samples.append(elapsed)
            response_text.append(json.dumps(result, sort_keys=True))
            if kind == "locate":
                result_counts.append(len(result.get("results") or []))
            elif kind == "context":
                context = str(result.get("context", ""))
                context_sizes.append(len(context))
                estimated_tokens.append(len(context) / 4)
                result_counts.append(len(result.get("results") or []))
            else:
                result_counts.append(len(result.get("edges") or []))
        rows[f"infra.{kind}"] = {
            "count": len(samples), "query_kinds": [kind],
            "first_call_seconds": first_call[kind],
            "samples_seconds": samples,
            "result_counts": result_counts, "p50_seconds": percentile(samples, 50),
            "p95_seconds": percentile(samples, 95),
        }

    if any(canary in response for canary in canaries for response in response_text):
        raise RuntimeError("a secret canary appeared in a graph query result")
    quality = {"requested_max_chars": max_chars, "sample_count": len(context_sizes) - 1,
               "all_within_max_chars": all(size <= max_chars for size in context_sizes),
               "context_chars_samples": context_sizes[1:],
               "estimated_tokens_samples": estimated_tokens[1:],
               "estimated_tokens_method": "estimate only: characters / 4; no tokenizer used",
               "all_canary_free": True}
    return rows, quality


def _refresh_sample(memory: Any, expected_path: str | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    delta = memory.refresh()
    elapsed = time.perf_counter() - started
    result = {"wall_seconds": elapsed, "changed_files": list(delta.files),
              "delta_empty": bool(delta.empty)}
    if expected_path:
        result["expected_file_indexed"] = (
            memory.node_by_key(f"file:{expected_path}") is not None and any(
            node.get("type") == "InfraResource"
            and node.get("attrs", {}).get("path") == expected_path
            for node in memory.nodes.values())
        )
    return result


def _run_in_workspace(files: int, seed: int, query_count: int, workspace: Path) -> dict[str, Any]:
    previous_env = {
        name: os.environ.get(name)
        for name in ("OZAGIA_CONFIG", "OZAGIA_ADAPTERS")
    }
    if files < 3:
        raise ValueError("--files must be at least 3")
    if query_count < 1:
        raise ValueError("--query-count must be at least 1")
    tf_count = round(files * 0.6)
    tf_count = min(files - 1, max(1, tf_count))
    kube_count = files - tf_count
    root = workspace / "repo"
    root.mkdir(parents=True)
    try:
        tf_paths = _terraform_sources(root, tf_count, seed)
        kube_paths = _kubernetes_sources(root, kube_count, seed)
        config = root / ".ozagia.toml"
        _write(config, f'[adapters]\nmodules = ["{ADAPTER_REF}"]\n')
        _git_fixture(root)
        disabled_config = workspace / "disabled.toml"
        _write(disabled_config, "[adapters]\nmodules = []\n")

        from ozagia.adapters import registry
        from ozagia.core.memory import DevMemory

        enabled_dump = workspace / "enabled-graph.json"
        disabled_dump = workspace / "disabled-graph.json"
        enabled_build = _cold_build(root, enabled_dump, config, enabled=True)
        disabled_build = _cold_build(
            root, disabled_dump, disabled_config, enabled=False,
            clear_cache=False)
        if not enabled_build["kubernetes_resources"]:
            raise RuntimeError("Kubernetes fixture was not parsed; install the optional YAML extra")
        os.environ["OZAGIA_CONFIG"] = str(config.resolve())
        os.environ["OZAGIA_ADAPTERS"] = ""
        registry(root, reload=True)
        canaries = (TF_CANARY, K8S_CANARY)
        dump_text = enabled_dump.read_text(encoding="utf-8")
        if any(canary in dump_text for canary in canaries):
            raise RuntimeError("a secret canary appeared in the graph dump")

        memory = DevMemory(enabled_dump, workspace / "journal.jsonl", root=root)
        initial_freshness = memory.staleness().get("freshness")
        infra_names = [str(n.get("attrs", {}).get("address") or n.get("attrs", {}).get("name") or "")
                       for n in memory.nodes.values() if n.get("type") == "InfraResource"]
        if not infra_names:
            raise RuntimeError("enabled graph contains no infrastructure resources")
        rng = random.Random(seed)
        query_names = [rng.choice(infra_names) for _ in range(query_count)]
        query_rows, context_quality = _query_latency(memory, query_names, query_count, 480, canaries)
        if not context_quality["all_within_max_chars"]:
            raise RuntimeError("context exceeded its requested character budget")
        from ozagia.core.infra_query import cache_stats
        repeated_started = time.perf_counter()
        for _ in range(5):
            memory.adapter_query("infra.context", query=query_names[0],
                                 max_chars=480)
        repeated_context_seconds = (time.perf_counter() - repeated_started) / 5
        query_cache = cache_stats(memory)

        changed_path = tf_paths[0]
        _write(root / changed_path, (root / changed_path).read_text(encoding="utf-8")
               + '\nresource "aws_security_group" "refresh_marker" {\n  vpc_id = aws_vpc.core.id\n}\n')
        modified_refresh = _refresh_sample(memory, changed_path)
        modified_refresh["marker_indexed"] = any(
            n.get("type") == "InfraResource"
            and n.get("attrs", {}).get("address") == "aws_security_group.refresh_marker"
            for n in memory.nodes.values())
        if not modified_refresh["marker_indexed"]:
            raise RuntimeError("modified Terraform declaration was not indexed")
        modified_freshness = memory.staleness().get("freshness")

        new_path = "infra/network/added-by-refresh.tf"
        _write(root / new_path, 'resource "aws_subnet" "added_by_refresh" {\n'
               '  vpc_id = aws_vpc.core.id\n  cidr_block = "10.43.0.0/24"\n}\n')
        added_refresh = _refresh_sample(memory, new_path)
        if not added_refresh["expected_file_indexed"]:
            raise RuntimeError("new Terraform file was not indexed by refresh")
        added_freshness = freshness_before_noop = memory.staleness().get("freshness")
        noop_refresh = _refresh_sample(memory)
        final_freshness = memory.staleness().get("freshness")

        if any(canary in dump_text for canary in canaries):
            raise RuntimeError("secret canary appeared in a graph dump")
        live_dump_text = enabled_dump.read_text(encoding="utf-8")
        if any(canary in live_dump_text for canary in canaries):
            raise RuntimeError("secret canary appeared in a refreshed graph dump")
        source_sizes = [(root / rel).stat().st_size for rel in tf_paths + kube_paths]
        source_bytes, largest_source = sum(source_sizes), max(source_sizes, default=0)
        from ozagia.core.infra_discovery import MAX_FILES, MAX_FILE_BYTES
        from ozagia.integrations.iac.adapter import MAX_TOTAL_SOURCE_BYTES

        return {
            "schema_version": SCHEMA_VERSION, "generated_at": datetime.now(UTC).isoformat(),
            "python_version": sys.version.split()[0], "platform": platform.platform(), "scope": SCOPE,
            "file_counts": {"requested": files, "terraform_generated": tf_count,
                            "kubernetes_generated": kube_count, "generated_total": files},
            "sizes_bytes": {"source_files_total": source_bytes, "largest_source_file": largest_source,
                            "adapter_enabled_index_file": enabled_build["snapshot_bytes"],
                            "adapter_disabled_index_file": disabled_build["snapshot_bytes"]},
            "cold_build": {"adapter_enabled": enabled_build, "adapter_disabled": disabled_build,
                           "adapter_overhead_seconds": enabled_build["wall_seconds"] - disabled_build["wall_seconds"]},
            "warm_queries": query_rows,
            "repeated_context_seconds": repeated_context_seconds,
            "query_cache": query_cache,
            "incremental_refresh": {"modified_terraform": modified_refresh,
                                    "added_terraform": added_refresh, "noop_after_freshness_check": noop_refresh},
            "counts": {"adapter_enabled_nodes": enabled_build["nodes"], "adapter_enabled_edges": enabled_build["edges"],
                       "adapter_enabled_infra_resources": enabled_build["infra_resources"],
                       "adapter_enabled_kubernetes_resources": enabled_build["kubernetes_resources"],
                       "adapter_enabled_infra_edges": enabled_build["infra_edges"],
                       "adapter_disabled_infra_resources": disabled_build["infra_resources"],
                       "adapter_disabled_infra_edges": disabled_build["infra_edges"]},
            "budget_limits": {"candidate_files": MAX_FILES, "per_file_bytes": MAX_FILE_BYTES,
                              "total_source_bytes": MAX_TOTAL_SOURCE_BYTES},
            "budget_flags": {"candidate_file_limit_hit": files > MAX_FILES,
                             "per_file_byte_limit_hit": largest_source > MAX_FILE_BYTES,
                             "total_source_byte_limit_hit": source_bytes > MAX_TOTAL_SOURCE_BYTES,
                             "any_limit_hit": files > MAX_FILES or largest_source > MAX_FILE_BYTES or source_bytes > MAX_TOTAL_SOURCE_BYTES},
            "freshness": {"after_enabled_build": initial_freshness,
                          "after_modified_refresh": modified_freshness, "after_added_refresh": added_freshness,
                          "before_noop_refresh": freshness_before_noop, "after_noop_refresh": final_freshness},
            "context_quality": context_quality,
            "canary_check": {"passed": True, "canary_count": len(canaries), "absent_from_dump": True,
                             "absent_from_query_results": True},
            "adapter_disabled_build_succeeded": disabled_build["nodes"] >= 1 and not disabled_build["iac_adapter_loaded"],
        }
    finally:
        for name, value in previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def run_benchmark(files: int = 300, seed: int = 7, query_count: int = 20, *, temp_dir: str | Path = DEFAULT_WORKSPACE) -> dict[str, Any]:
    try:
        workspace_parent = Path(temp_dir)
        workspace_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=workspace_parent) as temp:
            return _run_in_workspace(files, seed, query_count, Path(temp))
    except BenchmarkError:
        raise
    except Exception as exc:
        raise BenchmarkError("synthetic build or query", type(exc).__name__) from None


def _error_report(error: BenchmarkError) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "scope": SCOPE,
            "error": {"message": str(error), "stage": error.stage, "type": error.error_type}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", type=int, default=300, help="synthetic files, split about 60/40")
    parser.add_argument("--seed", type=int, default=7, help="deterministic fixture seed")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"JSON report path (default {DEFAULT_OUT})")
    parser.add_argument("--query-count", type=int, default=20, help="warm samples per query kind")
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(args.files, args.seed, args.query_count)
        exit_code = 0
    except BenchmarkError as error:
        report = _error_report(error)
        exit_code = 1
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        report = {"schema_version": SCHEMA_VERSION,
                  "error": {"message": "could not write report output", "type": "OSError"}}
        exit_code = 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
