import pytest

from ozagia.core.deep_review import orchestrator
from ozagia.core.deep_review.orchestrator import DeepReviewError


def _security_packet():
    provenance = {
        "requested": ["bandit", "mystery-scanner"],
        "network_allowed": False,
        "results": [
            {"name": "bandit", "kind": "sast", "status": "ok",
             "version": "1.8.0", "error": None, "findings": 2,
             "report_hash": "sha256:abc"},
            {"name": "mystery-scanner", "kind": None,
             "status": "not_checked", "version": None,
             "error": "unknown tool", "findings": 0},
        ],
    }
    return {
        "findings": [],
        "sections": {"security": {
            "checked": ["offline_security_scan"],
            "not_checked": [{"reason": (
                "security scanner tool 'mystery-scanner' is unknown and "
                "was not run")}],
            "scanner_tools": provenance,
            "scanner_summary": {"findings": 2, "omitted": 0},
            "scanner_graph": {"freshness": "unknown"},
        }},
        "graph": {"freshness": "unknown"},
    }


def _stub(monkeypatch, packet):
    calls = {}

    def collect(root, **kwargs):
        calls.update(kwargs)
        return packet

    monkeypatch.setattr(orchestrator.collect, "collect", collect)
    monkeypatch.setattr(
        orchestrator, "_verified_priority_graph",
        lambda root, **kwargs: (None, {
            "freshness": "unknown", "reason": "test graph omitted"}))
    return calls


def test_manifest_security_provenance_and_unknown_tool_partial(
        tmp_path, monkeypatch):
    packet = _security_packet()
    calls = _stub(monkeypatch, packet)
    out = tmp_path / "report"
    result = orchestrator.build_deep_review(
        tmp_path, out, profile="security",
        security_tools=["bandit", "mystery-scanner"],
        security_network=False)
    manifest = result["manifest"]
    security = manifest["subjects"]["security"]

    assert calls["security_tools"] == ["bandit", "mystery-scanner"]
    assert calls["security_network"] is False
    assert security["scanner_tools"] == packet["sections"]["security"][
        "scanner_tools"]
    assert security["scanner_summary"] == {"findings": 2, "omitted": 0}
    assert security["scanner_graph"] == {"freshness": "unknown"}
    assert security["status"] == "partial"
    assert any("mystery-scanner" in str(item)
               for item in security["omitted"])

    assert manifest["security"] == {
        "requested": ["bandit", "mystery-scanner"],
        "network_allowed": False,
        "results": packet["sections"]["security"]["scanner_tools"]["results"],
        "statuses": {"not_checked": 1, "ok": 1},
        "report_hashes": {"bandit": "sha256:abc"},
        "report_hash": "sha256:abc",
    }
    collectors = manifest["truncation"]["collectors"]
    assert collectors["security_tools_requested"] == [
        "bandit", "mystery-scanner"]
    assert collectors["security_tool_statuses"] == {
        "not_checked": 1, "ok": 1}
    assert "unknown and was not run" in (out / "01-security.md").read_text(
        encoding="utf-8")


@pytest.mark.parametrize("value", ["bandit", ["bandit", 7], [None], {"a": 1}])
def test_security_tools_must_be_a_list_of_strings(
        tmp_path, monkeypatch, value):
    _stub(monkeypatch, _security_packet())
    with pytest.raises(DeepReviewError, match="security_tools"):
        orchestrator.build_deep_review(
            tmp_path, tmp_path / "report", security_tools=value)


def test_only_exact_true_enables_security_network(tmp_path, monkeypatch):
    packet = _security_packet()
    calls = _stub(monkeypatch, packet)
    orchestrator.build_deep_review(
        tmp_path, tmp_path / "truthy", security_tools=["bandit"],
        security_network=1)
    assert calls["security_network"] is False

    orchestrator.build_deep_review(
        tmp_path, tmp_path / "enabled", security_tools=["bandit"],
        security_network=True)
    assert calls["security_network"] is True

    calls.clear()
    orchestrator.build_deep_review(
        tmp_path, tmp_path / "default", security_tools=None)
    assert calls["security_tools"] is None
    assert calls["security_network"] is False
