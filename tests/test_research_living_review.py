from __future__ import annotations

import pytest

from ozagia.research.graph import ResearchGraph
from ozagia.research.living_review import (
    LivingReviewError,
    acknowledge_alert,
    alert_report,
    record_alert,
    scan_alerts,
)


def _graph(tmp_path, investigation_id: str) -> ResearchGraph:
    return ResearchGraph.create(
        "Living review tests",
        root=tmp_path,
        investigation_id=investigation_id,
    )


def _reference(graph, key: str, title: str, **fields):
    return graph.record(
        "Reference",
        title=title,
        citation_key=key,
        citation_keys=fields.pop("citation_keys", [key]),
        identifiers=fields.pop("identifiers", {"doi": f"10.1234/{key}"}),
        **fields,
    )


def _claim(graph, wording: str, **fields):
    return graph.record(
        "Claim",
        wording=wording,
        claim_type=fields.pop("claim_type", "hypothesis"),
        status=fields.pop("status", "unresolved"),
        author=fields.pop("author", "reader"),
        scope=fields.pop("scope", "the tested corpus"),
        **fields,
    )


def _kinds(result):
    return [alert["kind"] for alert in result["alerts"]]


def test_retraction_and_concern_alerts_are_deterministic(tmp_path):
    graph = _graph(tmp_path, "living-retractions")
    retracted = _reference(
        graph,
        "retracted2020",
        "A retracted trial",
        retraction={
            "status": "retracted",
            "provider": "crossref",
            "checked_at": "2026-01-01T00:00:00Z",
            "notices": ["doi:10.1234/notice"],
        },
    )
    concern = _reference(
        graph,
        "concern2021",
        "A paper with a concern",
        retraction={"status": "concern", "provider": "retraction-watch"},
    )
    _reference(graph, "clean2022", "A clean paper")

    first = scan_alerts(graph.state())
    second = scan_alerts(graph.state())

    assert first == second
    assert first["truth_assessed"] is False
    assert first["count"] == 2
    assert first["kinds"] == ["expression_of_concern", "retracted_reference"]
    assert first["counts_by_kind"] == {
        "expression_of_concern": 1,
        "retracted_reference": 1,
    }
    assert first["plan_id"] is None
    assert first["since"] is None

    retracted_alert = next(
        alert for alert in first["alerts"] if alert["kind"] == "retracted_reference"
    )
    assert retracted_alert["target_id"] == retracted["id"]
    assert retracted_alert["target_type"] == "Reference"
    assert retracted_alert["payload"]["retraction_status"] == "retracted"
    assert retracted_alert["payload"]["provider"] == "crossref"
    assert retracted_alert["truth_assessed"] is False

    concern_alert = next(
        alert for alert in first["alerts"] if alert["kind"] == "expression_of_concern"
    )
    assert concern_alert["target_id"] == concern["id"]

    filtered = scan_alerts(graph.state(), reference_ids=[concern["id"]])
    assert _kinds(filtered) == ["expression_of_concern"]
    assert filtered["count"] == 1


def test_claim_citing_retracted_reference_is_alerted(tmp_path):
    graph = _graph(tmp_path, "living-claim-retracted")
    reference = _reference(
        graph,
        "retracted2020",
        "A retracted trial",
        retraction={"status": "retracted"},
    )
    claim = _claim(graph, "The trial improved survival.", citations=["retracted2020"])
    graph.link(claim["id"], "cites", reference["id"])

    result = scan_alerts(graph.state())
    alerts = [
        alert for alert in result["alerts"] if alert["kind"] == "claim_cites_retracted"
    ]

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["target_id"] == claim["id"]
    assert alert["target_type"] == "Claim"
    assert alert["payload"]["reference_id"] == reference["id"]
    assert alert["payload"]["citation_key"] == "retracted2020"
    assert alert["payload"]["signals"] == ["citation_key", "cites_edge"]
    assert "retracted_reference" in result["kinds"]

    filtered = scan_alerts(graph.state(), reference_ids=["reference-9999"])
    assert not any(
        alert["kind"] == "claim_cites_retracted" for alert in filtered["alerts"]
    )


def test_unresolved_citations_and_missing_identifiers(tmp_path):
    graph = _graph(tmp_path, "living-citations")
    _reference(graph, "known2020", "A known paper")
    claim = _claim(
        graph,
        "A claim with mixed citations.",
        citations=["known2020", "ghost2021"],
    )
    evidence = graph.record(
        "Evidence",
        wording="Evidence citing a missing key.",
        citation_keys=["absent2022"],
    )
    no_identifier = graph.record(
        "Reference",
        title="A reference without identifiers",
        citation_key="noid2023",
        citation_keys=["noid2023"],
    )

    result = scan_alerts(graph.state())

    unresolved = {
        (alert["target_id"], alert["payload"]["citation_key"])
        for alert in result["alerts"]
        if alert["kind"] == "unresolved_citation"
    }
    assert unresolved == {(claim["id"], "ghost2021"), (evidence["id"], "absent2022")}
    missing = [
        alert for alert in result["alerts"] if alert["kind"] == "missing_identifier"
    ]
    assert [alert["target_id"] for alert in missing] == [no_identifier["id"]]
    assert missing[0]["payload"]["citation_key"] == "noid2023"


def test_new_cites_work_since_plan_and_since_cutoff(tmp_path):
    graph = _graph(tmp_path, "living-new-citations")
    source = _reference(graph, "source2020", "A citing work")
    target = _reference(graph, "new2025", "A newly cited work")
    plan = graph.record(
        "ReviewPlan",
        question="Does the new work change the review?",
        kind="systematic",
        status="open",
        truth_assessed=False,
    )
    graph.link(
        source["id"],
        "cites_work",
        target["id"],
        provenance={
            "provider": "openalex",
            "retrieved_at": "2999-01-01T00:00:00Z",
            "direction": "references",
        },
    )
    graph.link(
        source["id"],
        "cites_work",
        _reference(graph, "old2024", "An old cited work")["id"],
        provenance={
            "provider": "openalex",
            "retrieved_at": "2024-01-01T00:00:00Z",
            "direction": "references",
        },
    )

    unscoped = scan_alerts(graph.state())
    assert not any(alert["kind"] == "new_citation_since_plan" for alert in unscoped["alerts"])

    by_plan = scan_alerts(graph.state(), review_plan_id=plan["id"])
    assert by_plan["plan_id"] == plan["id"]
    new_alerts = [
        alert for alert in by_plan["alerts"]
        if alert["kind"] == "new_citation_since_plan"
    ]
    assert len(new_alerts) == 1
    assert new_alerts[0]["target_id"] == target["id"]
    assert new_alerts[0]["payload"]["source_reference_id"] == source["id"]
    assert new_alerts[0]["payload"]["plan_id"] == plan["id"]
    assert new_alerts[0]["payload"]["retrieved_at"] == "2999-01-01T00:00:00Z"

    by_since = scan_alerts(graph.state(), since="2025-01-01T00:00:00Z")
    assert by_since["since"] == "2025-01-01T00:00:00Z"
    assert len(
        [
            alert for alert in by_since["alerts"]
            if alert["kind"] == "new_citation_since_plan"
        ]
    ) == 1

    later = scan_alerts(graph.state(), since="3000-01-01T00:00:00Z")
    assert not any(
        alert["kind"] == "new_citation_since_plan" for alert in later["alerts"]
    )

    with pytest.raises(LivingReviewError, match="unknown ReviewPlan"):
        scan_alerts(graph.state(), review_plan_id="reviewplan-9999")
    with pytest.raises(LivingReviewError, match="ISO timestamp"):
        scan_alerts(graph.state(), since="not-a-date")


def test_source_locator_status_does_not_read_files(tmp_path):
    graph = _graph(tmp_path, "living-sources")
    missing = graph.record(
        "Source",
        locator={"kind": "local_file", "path": str(tmp_path / "absent.pdf"), "status": "missing"},
    )
    stale = graph.record(
        "Source",
        source_locators=[
            {"kind": "cached_fetch", "path": "cache/paper.pdf", "status": "stale"}
        ],
    )
    graph.record(
        "Source",
        locator={"kind": "local_file", "path": str(tmp_path / "present.md"), "status": "current"},
    )

    result = scan_alerts(graph.state())

    unavailable = [
        alert for alert in result["alerts"] if alert["kind"] == "source_unavailable"
    ]
    assert [alert["target_id"] for alert in unavailable] == sorted(
        [missing["id"], stale["id"]]
    )
    assert {alert["payload"]["status"] for alert in unavailable} == {"missing", "stale"}


def test_scan_is_read_only_and_scans_record_no_alerts(tmp_path):
    graph = _graph(tmp_path, "living-read-only")
    reference = _reference(
        graph,
        "retracted2020",
        "A retracted trial",
        retraction={"status": "retracted"},
    )
    _claim(graph, "A claim citing work.", citations=["retracted2020"])
    before_bytes = graph.path.read_bytes()
    before_state = graph.state()

    result = scan_alerts(graph.state())

    assert result["count"] >= 1
    assert graph.path.read_bytes() == before_bytes
    assert graph.state() == before_state
    assert not any(node["type"] == "Alert" for node in graph.state()["nodes"])
    assert reference["id"] in {
        alert["target_id"] for alert in result["alerts"]
        if alert["kind"] == "retracted_reference"
    }


def test_record_acknowledge_and_alert_report(tmp_path):
    graph = _graph(tmp_path, "living-record")
    reference = _reference(
        graph,
        "retracted2020",
        "A retracted trial",
        retraction={"status": "retracted"},
    )
    claim = _claim(graph, "A claim needing review.")
    source = graph.record("Source", locator={"kind": "local_file", "path": "paper.md"})
    plan = graph.record(
        "ReviewPlan",
        question="A living review plan",
        kind="systematic",
        status="open",
    )

    recorded = record_alert(
        graph,
        kind="retracted_reference",
        target_id=reference["id"],
        payload={"retraction_status": "retracted", "source": "manual"},
        reviewer="reviewer-a",
    )
    alert = recorded["alert"]
    assert alert["type"] == "Alert"
    assert alert["kind"] == "retracted_reference"
    assert alert["target_id"] == reference["id"]
    assert alert["status"] == "open"
    assert alert["reviewer"] == "reviewer-a"
    assert alert["payload"]["source"] == "manual"
    assert alert["truth_assessed"] is False
    assert recorded["truth_assessed"] is False
    links = [
        edge for edge in graph.state()["edges"]
        if edge["type"] == "alerts" and edge["src"] == alert["id"]
    ]
    assert [edge["tgt"] for edge in links] == [reference["id"]]

    second = record_alert(
        graph,
        kind="unresolved_citation",
        target_id=claim["id"],
        payload={"citation_key": "ghost2021"},
    )["alert"]
    dismissed = record_alert(
        graph,
        kind="source_unavailable",
        target_id=source["id"],
        payload={"status": "missing"},
        status="dismissed",
    )["alert"]
    plan_alert = record_alert(
        graph,
        kind="new_citation_since_plan",
        target_id=plan["id"],
        payload={"edge_id": "edge-000001"},
    )["alert"]

    report = alert_report(graph.state())
    assert report["truth_assessed"] is False
    assert report["count"] == 4
    assert report["open_count"] == 3
    assert report["counts"]["alerts"] == 4
    assert report["by_status"]["open"] == 3
    assert report["by_status"]["dismissed"] == 1
    assert report["by_kind"]["retracted_reference"] == 1
    assert report["plan"]["id"] == plan["id"]
    assert plan_alert["id"] in {item["id"] for item in report["open_alerts"]}
    assert second["id"] in {item["id"] for item in report["open_alerts"]}
    assert dismissed["id"] not in {item["id"] for item in report["open_alerts"]}

    acknowledged = acknowledge_alert(
        graph,
        alert["id"],
        reviewer="reviewer-b",
        note="Checked the notice; claim needs revision.",
    )
    assert acknowledged["truth_assessed"] is False
    assert acknowledged["previous_status"] == "open"
    assert acknowledged["alert"]["status"] == "acknowledged"
    assert acknowledged["alert"]["reviewer"] == "reviewer-b"
    assert acknowledged["alert"]["note"] == "Checked the notice; claim needs revision."
    assert acknowledged["alert"]["acknowledged_at"]
    assert acknowledged["alert"]["updated_at"]

    report = alert_report(graph.state(), status="acknowledged")
    assert report["count"] == 1
    assert report["alerts"][0]["id"] == alert["id"]
    assert report["open_count"] == 2
    assert report["counts"]["filtered"] == 1
    assert report["status"] == "acknowledged"

    with pytest.raises(LivingReviewError, match="unknown Alert"):
        acknowledge_alert(graph, "alert-9999", reviewer="reviewer")
    with pytest.raises(LivingReviewError, match="reviewer"):
        acknowledge_alert(graph, alert["id"], reviewer="  ")
    with pytest.raises(LivingReviewError, match="unknown alert target"):
        record_alert(graph, kind="missing_identifier", target_id="reference-9999")
    with pytest.raises(LivingReviewError, match="alert target must be"):
        record_alert(graph, kind="other", target_id=graph.record(
            "Evidence", wording="Not an alert target"
        )["id"])
    with pytest.raises(LivingReviewError, match="status must be"):
        record_alert(
            graph,
            kind="other",
            target_id=reference["id"],
            status="closed",
        )
    with pytest.raises(LivingReviewError, match="kind"):
        record_alert(graph, kind="  ", target_id=reference["id"])
    with pytest.raises(LivingReviewError, match="status must be"):
        alert_report(graph.state(), status="closed")
