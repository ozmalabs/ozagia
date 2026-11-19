from ozagia.core.deep_review.reports import render_readme, render_subject


def test_subject_report_shows_coverage_evidence_and_integrity():
    report = render_subject(
        "security",
        [{"id": "dr-a1", "severity": "major", "type": "path-check",
          "confidence": "high", "evidence_kind": "observed",
          "locations": [{"path": "src/a.py", "line": 12}],
          "evidence": [{"source": "scanner", "method": "static scan",
                        "kind": "static", "hash": "sha256:abc",
                        "excerpt_ref": "evidence/a.txt"}],
          "recommendation": "Validate the path."}],
        status="partial", omissions=["external scanner unavailable"],
        checks=["local scan"], skipped=["dependency audit"], unknown=["runtime behavior"])

    assert "1 checks, 1 skipped, 1 unknown, 1 omitted" in report
    assert "Confidence: high; evidence kind: observed." in report
    assert "sha256:abc" in report
    assert "Evidence integrity: 1/1 evidence items carry a content hash" in report
    assert '<a id="finding-dr-a1"></a>' in report


def test_failure_mode_report_renders_bounded_fmea_with_unknowns_and_evidence_link():
    finding = {
        "id": "dr-retry",
        "subject": "failure-modes",
        "type": "unbounded_exception_retry",
        "severity": "minor",
        "confidence": "low",
        "evidence_kind": "inferred",
        "recommendation": "Bound retries and use a deadline.",
        "evidence": [{"source": "static", "method": "AST scan"}],
        "locations": [{"path": "src/worker.py", "symbol": "run"}],
    }
    test_finding = {"id": "dr-test-record", "subject": "tests-and-coverage",
                    "type": "failing_test", "related_ids": []}
    finding["related_ids"] = ["dr-test-record"]
    report = render_subject(
        "failure-modes", [finding], status="partial", omissions=[],
        checks=["Python static patterns"], related_findings=[finding, test_finding])

    assert "## Failure-mode analysis" in report
    assert "critical-path status, runtime likelihood and criticality are unknown" in report
    assert "Unknown (no critical-path classification is available)." in report
    assert "An exception handler may continue an unconditional loop." in report
    assert "static source inspection does not establish runtime trigger conditions" in report
    assert "Static AST rule matched: unconditional loop with an exception-handler continue." in report
    assert "Bound retries and use a deadline." in report
    assert "See evidence details for dr-retry." in report
    assert "[dr-test-record](./07-tests-and-coverage.md#finding-dr-test-record)" in report
    # Rendering is a view; it does not add FMEA fields to canonical source data.
    assert set(finding) == {
        "id", "subject", "type", "severity", "confidence", "evidence_kind",
        "recommendation", "evidence", "locations", "related_ids"}


def test_failure_mode_fmea_marks_unsupported_fields_unknown_and_bounds_text():
    report = render_subject(
        "failure-modes", [{"id": "x" * 500, "type": "unhandled_error",
                           "evidence": [], "recommendation": ""}],
        status="partial", omissions=[])

    assert "no supported symptom mapping" in report
    assert "no supported detection mapping" in report
    assert "no mitigation recorded" in report
    assert "source finding has no evidence items" in report
    assert "no critical-path classification is available" in report
    assert "no explicit test/record finding is linked" in report
    assert len(report) < 10_000


def test_failure_mode_fmea_does_not_guess_test_links_from_matching_locations():
    failure = {"id": "dr-failure", "type": "unbounded_exception_retry",
               "locations": [{"path": "src/worker.py"}], "evidence": []}
    test = {"id": "dr-test", "subject": "tests-and-coverage",
            "type": "failing_test", "locations": [{"path": "src/worker.py"}]}
    report = render_subject("failure-modes", [failure], status="partial",
                            omissions=[], related_findings=[test])

    assert "no explicit test/record finding is linked" in report
    assert "./07-tests-and-coverage.md#finding-dr-test" not in report


def test_subject_metrics_are_allowlisted_bounded_and_path_free():
    report = render_subject(
        "data-structures", [], status="complete", omissions=[],
        checks=["inventory"], metrics={
            "data_inventory": {
                "files_seen": 12, "bytes_read": 4096, "duration_s": 0.5,
                "models": [{"file": "/secret/path.py"}],
                "schema_declarations": [], "documents": [{"file": "secret"}],
                "path": "/secret/path.py", "log": "token=secret",
            },
            "inventory": {"files_seen": 777},
            "password": "secret",
        })

    assert "## Metrics" in report
    assert "| Files seen | 12 |" in report
    assert "| Bytes read | 4096 |" in report
    assert "Analysis seconds" not in report
    assert "| Models | 1 |" in report
    assert "| Structured documents | 1 |" in report
    assert "secret" not in report
    assert "/secret/path.py" not in report
    assert "token" not in report


def test_subject_metrics_reject_unbounded_or_non_numeric_source_values():
    report = render_subject(
        "security", [], status="complete", omissions=[], metrics={
            "files_checked": True,
            "trust_boundary_files_checked": 10**1000,
            "trust_boundary_findings": "password=secret",
            "scanner_summary": {"findings": 99},
            "path": "/private/repo",
        })

    assert "| Status | complete |" in report
    assert "| Findings | 0 |" in report
    assert "Files checked" not in report
    assert "Trust-boundary files checked" not in report
    assert "password" not in report
    assert "99" not in report
    assert "/private/repo" not in report


def test_readme_links_index_and_ranks_top_risks_by_severity_then_priority():
    findings = [
        {"id": "minor-high", "severity": "minor", "subject": "security",
         "priority": {"score": 99}, "title": "Minor high score"},
        {"id": "major-low", "severity": "major", "subject": "security",
         "priority": {"score": 1}, "title": "Major risk"},
        {"id": "major-high", "severity": "major", "subject": "security",
         "priority": {"score": 50}, "title": "Higher priority"},
    ]
    readme = render_readme({"security": {"status": "partial",
        "report": "01-security.md", "findings": [f["id"] for f in findings]}}, findings)

    assert "[Findings index](./findings-index.md)" in readme
    assert readme.index("major-high") < readme.index("major-low") < readme.index("minor-high")
    assert "01-security.md#finding-major-high" in readme
