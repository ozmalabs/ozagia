from ozagia.core.repo_review import PreflightError
from ozagia.eval import deep_review_report


def test_report_evaluator_compares_two_runs_in_private_temporary_checkout(monkeypatch):
    roots = []
    outputs = []

    def build(root, out, **kwargs):
        roots.append(root)
        outputs.append(out)
        assert root != deep_review_report.DEFAULT_FIXTURE
        if kwargs.get("baseline"):
            assert kwargs["baseline"] == outputs[0] / "findings.json"
        return {"manifest": {"complete": True, "status": "complete",
                             "subjects": {"performance": {"status": "partial"}},
                             "comparison": {"new": [], "resolved": [],
                                            "unchanged": ["finding-a"]}},
                "findings": [{"id": "finding-a"}], "out": str(out), "failed": False}

    monkeypatch.setattr(deep_review_report, "build_deep_review", build)
    result = deep_review_report.evaluate()
    assert result["status"] == "partial"
    assert result["environment"]["unavailable_label_subjects"]
    assert roots[0] == roots[1]
    assert outputs[0] != outputs[1]
    assert result["comparison"] == {"new": 0, "resolved": 0,
                                    "unchanged": 1, "stable_ids": True}


def test_report_evaluator_records_resource_preflight_refusal(monkeypatch):
    monkeypatch.setattr(
        deep_review_report, "build_deep_review",
        lambda *args, **kwargs: (_ for _ in ()).throw(PreflightError(
            "resource admission refused", {"ok": False, "checks": [
                {"id": "resources.admission", "level": "error",
                 "message": "not enough reserve"}]})))
    result = deep_review_report.evaluate()
    assert result["environment"]["status"] == "preflight_refused"
    assert result["environment"]["check_failures"] == [
        {"id": "resources.admission", "level": "error",
         "message": "not enough reserve"}]
    assert result["comparison"] is None
