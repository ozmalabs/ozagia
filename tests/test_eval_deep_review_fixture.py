import json

from ozagia.eval.deep_review_fixture import DEFAULT_FIXTURE, evaluate, main


def test_seeded_service_fixture_reports_labeled_metrics_and_size():
    result = evaluate()
    assert result["fixture_files"] == 4
    assert result["true_positive"] == 4
    assert result["false_positive"] == 0
    assert result["false_negative"] == 0
    assert result["precision"] == result["recall"] == 1.0
    assert set(result["rules"]) == {
        "repeated_nested_iteration", "mutable_default_argument",
        "silently_suppressed_exception", "materialized_range",
    }
    assert all(row["precision"] == row["recall"] == 1.0
               for row in result["rules"].values())
    assert result["source_bytes"] > 0
    assert result["candidate_artifact_bytes"] > 0
    assert result["static_scan_seconds"] >= 0


def test_fixture_cli_writes_reproducible_metrics(tmp_path):
    out = tmp_path / "fixture.json"
    assert main(["--fixture", str(DEFAULT_FIXTURE), "--out", str(out)]) == 0
    cli_result = json.loads(out.read_text(encoding="utf-8"))
    direct_result = evaluate()
    # Runtime is measured and host-dependent; the classification and size
    # fields must remain identical for the frozen fixture.
    cli_result.pop("static_scan_seconds")
    direct_result.pop("static_scan_seconds")
    assert cli_result == direct_result
