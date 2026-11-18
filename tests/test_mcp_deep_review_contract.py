"""MCP ``dev_deep_review`` contract: profiles, ordering, and write confinement."""
from __future__ import annotations

import json
from pathlib import Path


def _server(root: Path):
    from ozagia.integrations.mcp.server import DevMemoryServer
    return DevMemoryServer(None, workspace=root)


def _profile_report(path: Path) -> Path:
    report = {
        "schema": {"namespace": "ozagia.profile_report", "version": 1},
        "report_hash": "sha256:mcp-profile-report",
        "clock": "monotonic",
        "measurement": {"unit": "microseconds"},
        "hotspots": [{
            "key": "src/app.py::hot", "name": "hot",
            "file": "src/app.py", "line": 2,
            "self": 5_000, "inclusive": 6_000, "share_bp": 2_500,
        }],
    }
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_mcp_deep_review_profile_description_lists_phase2_profiles(tmp_path):
    server = _server(tmp_path)
    tool = next(item for item in server._tools
                if item["name"] == "dev_deep_review")
    description = tool["inputSchema"]["properties"]["profile"]["description"]

    for profile in ("performance", "failure-modes", "data-structures",
                    "scalability"):
        assert profile in description


def test_mcp_deep_review_preserves_orchestrator_order_and_bound(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review as deep_review

    findings = [
        {"id": "dr-performance", "subject": "performance", "type": "hot"},
        {"id": "dr-security", "subject": "security", "type": "boundary"},
    ]
    seen = {}

    def fake_build(root, out, **kwargs):
        seen.update(kwargs)
        return {
            "out": str(out),
            "manifest": {
                "schema": "ozagia.deep_review.manifest.v1",
                "findings": 7,
                "truncation": {"max_findings": {
                    "limit": 2, "total": 7, "returned": 2, "truncated": True}},
            },
            "findings": findings,
            "failed": False,
        }

    monkeypatch.setattr(deep_review, "build_deep_review", fake_build)
    server = _server(tmp_path)

    result = server._deep_review({"max_findings": 2, "budget_s": 5})

    assert result["ok"] is True
    assert seen["max_findings"] == 2
    # The orchestrator's order is preserved even though the first finding is
    # not from the alphabetically first subject.
    assert [item["id"] for item in result["top_findings"]] == [
        "dr-performance", "dr-security"]
    assert result["findings_total"] == 7
    assert result["findings_truncated"] is True


def test_mcp_deep_review_profile_report_writes_only_reviews(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def hot():\n    return sum(range(10))\n", encoding="utf-8")
    report = _profile_report(tmp_path / "profile.json")
    server = _server(tmp_path)

    result = server._deep_review({
        "profile": "performance",
        "profile_reports": [report.name],
        "budget_s": 10,
        "max_findings": 5,
    })

    assert result["ok"] is True, result.get("error")
    out = Path(result["out"])
    assert out.parent == (tmp_path / ".ozagia" / "reviews").resolve()
    assert (out / "manifest.json").is_file()
    assert not (tmp_path / ".ozagia" / "tags.json").exists()
    assert {item.name for item in (tmp_path / ".ozagia").iterdir()} == {"reviews"}
    assert any(item["type"] == "measured_hotspot"
               for item in result["top_findings"])
    performance = result["manifest"]["subjects"]["performance"]
    assert str(report.resolve()) in performance["profile_reports"]
