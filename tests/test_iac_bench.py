"""Fast offline checks for the synthetic IaC benchmark harness."""
from __future__ import annotations

import pytest

from ozagia.eval.iac_bench import percentile, run_benchmark


def test_percentile_is_deterministic_nearest_rank():
    samples = [9.0, 1.0, 5.0, 3.0, 7.0]
    assert percentile(samples, 50) == 5.0
    assert percentile(samples, 95) == 9.0
    assert percentile(samples, 95) == percentile(samples, 95)
    assert percentile([], 95) is None


def test_small_offline_benchmark_schema_safety_and_refresh(
    tmp_path, monkeypatch,
):
    pytest.importorskip("yaml")
    ambient_config = tmp_path / "ambient.toml"
    ambient_config.write_text("[adapters]\nmodules = []\n", encoding="utf-8")
    monkeypatch.setenv("OZAGIA_CONFIG", str(ambient_config))
    monkeypatch.setenv("OZAGIA_ADAPTERS", "")

    report = run_benchmark(
        files=12, seed=17, query_count=2, temp_dir=tmp_path,
    )

    required = {
        "schema_version", "generated_at", "python_version", "platform",
        "scope", "file_counts", "sizes_bytes", "cold_build", "warm_queries",
        "incremental_refresh", "counts", "budget_limits", "budget_flags",
        "freshness", "context_quality", "canary_check",
        "adapter_disabled_build_succeeded",
    }
    assert required <= report.keys()
    assert report["schema_version"] == 1
    assert report["file_counts"]["requested"] == 12
    assert report["file_counts"]["terraform_generated"] == 7
    assert report["file_counts"]["kubernetes_generated"] == 5
    assert report["scope"] == (
        "synthetic offline benchmark; not production latency or model token evidence"
    )

    assert report["cold_build"]["adapter_enabled"]["iac_adapter_loaded"] is True
    assert report["cold_build"]["adapter_disabled"]["iac_adapter_loaded"] is False
    assert report["adapter_disabled_build_succeeded"] is True
    assert report["counts"]["adapter_enabled_infra_resources"] > 0
    assert report["counts"]["adapter_enabled_kubernetes_resources"] > 0
    assert report["counts"]["adapter_disabled_infra_resources"] == 0

    assert report["canary_check"]["passed"] is True
    assert report["canary_check"]["absent_from_dump"] is True
    assert report["canary_check"]["absent_from_query_results"] is True
    assert report["context_quality"]["all_within_max_chars"] is True
    assert report["context_quality"]["all_canary_free"] is True
    assert report["context_quality"]["estimated_tokens_method"].startswith(
        "estimate only:"
    )
    assert all(
        row["count"] == 2 and len(row["samples_seconds"]) == 2
        for row in report["warm_queries"].values()
    )
    assert report["incremental_refresh"]["added_terraform"][
        "expected_file_indexed"
    ] is True
    assert report["query_cache"]["hits"] >= 4
    assert report["query_cache"]["entries"] <= report["query_cache"]["max_entries"]
    assert report["repeated_context_seconds"] >= 0
