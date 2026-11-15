from __future__ import annotations

import pytest

from ozagia.research import appraisal as ap
from ozagia.research.graph import ResearchGraph


def _graph(tmp_path):
    graph = ResearchGraph.create(
        "Appraise evidence",
        root=tmp_path,
        investigation_id="appraisal",
    )
    reference = graph.record(
        "Reference",
        title="A Trial of Graph Methods",
        citation_key="smith2024",
    )
    claim = graph.record(
        "Claim",
        wording="Graph methods reduce error.",
        claim_type="empirical_observation",
        status="unresolved",
        scope="adults",
        author="author",
        limitations="none recorded",
    )
    extraction = graph.record(
        "Extraction",
        target_id=reference["id"],
        study="A trial of graph methods",
        extractor="extractor",
        source_locator={"path": "paper.pdf", "page": 2},
    )
    review_plan = graph.record(
        "ReviewPlan",
        question="Do graph methods reduce error?",
        status="open",
    )
    source = graph.record("Source", locator="paper.pdf")
    graph.link(claim["id"], "cites", reference["id"])
    return graph, claim, reference, extraction, review_plan, source


def _domains(**overrides):
    domain = {
        "name": "Bias arising from randomisation",
        "judgement": "low",
        "support": "The allocation sequence was generated centrally.",
    }
    domain.update(overrides)
    return [domain]


# --------------------------------------------------------------------------- #
# risk of bias
# --------------------------------------------------------------------------- #
def test_record_risk_of_bias_links_assessment_and_check_passes(tmp_path):
    graph, claim, _reference, _extraction, _plan, _source = _graph(tmp_path)
    result = ap.record_risk_of_bias(
        graph,
        target_id=claim["id"],
        tool="RoB 2",
        domains=[
            {
                "name": "Bias arising from randomisation",
                "judgement": "Low",
                "support": "The allocation sequence was generated centrally.",
            },
            {
                "name": "Bias due to deviations",
                "judgement": "some concerns",
                "reason": "Outcome assessors were not blinded.",
            },
            {
                "name": "Bias in missing outcome data",
                "judgement": "not_applicable",
                "source_locator": {"path": "paper.pdf", "page": 4},
            },
        ],
        overall="some concerns",
        reviewer="reviewer-a",
        notes="Provisional judgement.",
    )
    assessment = result["assessment"]
    assert assessment["type"] == "RiskOfBias"
    assert assessment["target_id"] == claim["id"]
    assert assessment["tool"] == "RoB 2"
    assert assessment["overall"] == "some_concerns"
    assert assessment["reviewer"] == "reviewer-a"
    assert assessment["notes"] == "Provisional judgement."
    assert assessment["status"] == "candidate"
    assert assessment["truth_assessed"] is False
    assert assessment["domains"][0]["judgement"] == "low"
    assert assessment["domains"][1]["support"] == "Outcome assessors were not blinded."
    assert assessment["domains"][2]["source_locator"]["page"] == 4
    assert result["target"]["id"] == claim["id"]
    assert result["truth_assessed"] is False
    assert any(
        edge["type"] == "assesses"
        and edge["src"] == assessment["id"]
        and edge["tgt"] == claim["id"]
        for edge in graph.state()["edges"]
    )

    check = ap.check_risk_of_bias(graph, assessment["id"])
    assert check["ok"] is True
    assert check["issues"] == []
    assert check["warnings"] == []
    assert check["counts"]["domains"] == 3
    assert check["counts"]["supported_domains"] == 3
    assert check["counts"]["judgements"]["low"] == 1
    assert check["counts"]["judgements"]["some_concerns"] == 1
    assert check["counts"]["judgements"]["not_applicable"] == 1
    assert check["truth_assessed"] is False


def test_tool_and_judgement_aliases_accept_custom_checklists(tmp_path):
    graph, _claim, reference, extraction, _plan, _source = _graph(tmp_path)
    robins = ap.record_risk_of_bias(
        graph,
        target_id=extraction["id"],
        tool="  robins_i  ",
        domains=_domains(
            name="Confounding",
            judgement="Some Concerns",
            support="Confounders were measured but not adjusted.",
        ),
        overall="SOME CONCERNS",
        reviewer="reviewer-a",
    )["assessment"]
    assert robins["tool"] == "ROBINS-I"
    assert robins["overall"] == "some_concerns"
    assert robins["domains"][0]["judgement"] == "some_concerns"
    assert "tool_label" not in robins

    custom = ap.record_risk_of_bias(
        graph,
        target_id=reference["id"],
        tool="My Custom Checklist",
        domains=_domains(judgement="NOT APPLICABLE"),
        overall="low",
        reviewer="reviewer-b",
    )["assessment"]
    assert custom["tool"] == "custom"
    assert custom["tool_label"] == "My Custom Checklist"
    assert custom["domains"][0]["judgement"] == "not_applicable"

    generic = ap.record_risk_of_bias(
        graph,
        target_id=extraction["id"],
        tool="CUSTOM",
        domains=_domains(),
        overall="low",
        reviewer="reviewer-c",
    )["assessment"]
    assert generic["tool"] == "custom"
    assert "tool_label" not in generic


def test_risk_of_bias_rejects_unknown_targets_and_bad_fields(tmp_path):
    graph, claim, _reference, _extraction, _plan, source = _graph(tmp_path)
    with pytest.raises(ap.AppraisalError, match="unknown risk-of-bias target"):
        ap.record_risk_of_bias(
            graph,
            target_id="claim-9999",
            tool="RoB 2",
            domains=_domains(),
            overall="low",
            reviewer="reviewer-a",
        )
    with pytest.raises(ap.AppraisalError, match="must be one of"):
        ap.record_risk_of_bias(
            graph,
            target_id=source["id"],
            tool="RoB 2",
            domains=_domains(),
            overall="low",
            reviewer="reviewer-a",
        )

    for tool in ("", "   ", None, 7):
        with pytest.raises(ap.AppraisalError):
            ap.record_risk_of_bias(
                graph,
                target_id=claim["id"],
                tool=tool,
                domains=_domains(),
                overall="low",
                reviewer="reviewer-a",
            )
    for overall in ("", "moderate", "serious", None):
        with pytest.raises(ap.AppraisalError):
            ap.record_risk_of_bias(
                graph,
                target_id=claim["id"],
                tool="RoB 2",
                domains=_domains(),
                overall=overall,
                reviewer="reviewer-a",
            )
    with pytest.raises(ap.AppraisalError, match="reviewer"):
        ap.record_risk_of_bias(
            graph,
            target_id=claim["id"],
            tool="RoB 2",
            domains=_domains(),
            overall="low",
            reviewer=" ",
        )


@pytest.mark.parametrize(
    "domains",
    [
        [],
        "not a list",
        [None],
        [{"judgement": "low"}],
        [{"name": "   ", "judgement": "low"}],
        [{"name": "Confounding", "judgement": "moderate"}],
        [{"name": "Confounding", "judgement": "low", "support": 3}],
        [{"name": "Confounding", "judgement": "low", "support": "a", "reason": "b"}],
        [{"name": "Confounding", "judgement": "low", "unexpected": "field"}],
        [{"name": "Confounding", "judgement": "low", "source_locator": {}}],
        [{"name": "Confounding", "judgement": "low", "source_locator": 3}],
        [
            {"name": "Confounding", "judgement": "low"},
            {"name": "confounding", "judgement": "high"},
        ],
    ],
)
def test_malformed_domains_are_rejected(tmp_path, domains):
    graph, claim, _reference, _extraction, _plan, _source = _graph(tmp_path)
    with pytest.raises(ap.AppraisalError):
        ap.record_risk_of_bias(
            graph,
            target_id=claim["id"],
            tool="RoB 2",
            domains=domains,
            overall="low",
            reviewer="reviewer-a",
        )


def test_domain_json_safety_and_notes_validation(tmp_path):
    graph, claim, _reference, _extraction, _plan, _source = _graph(tmp_path)
    with pytest.raises(ap.AppraisalError, match="JSON-serializable"):
        ap.record_risk_of_bias(
            graph,
            target_id=claim["id"],
            tool="RoB 2",
            domains=_domains(source_locator={"path": "paper.pdf", "page": float("nan")}),
            overall="low",
            reviewer="reviewer-a",
        )
    with pytest.raises(ap.AppraisalError, match="notes"):
        ap.record_risk_of_bias(
            graph,
            target_id=claim["id"],
            tool="RoB 2",
            domains=_domains(),
            overall="low",
            reviewer="reviewer-a",
            notes=12,
        )


def test_check_flags_missing_domain_support(tmp_path):
    graph, claim, _reference, _extraction, _plan, _source = _graph(tmp_path)
    assessment = ap.record_risk_of_bias(
        graph,
        target_id=claim["id"],
        tool="RoB 2",
        domains=[{"name": "Unsupported domain", "judgement": "unclear"}],
        overall="unclear",
        reviewer="reviewer-a",
    )["assessment"]
    check = ap.check_risk_of_bias(graph.state(), assessment["id"])
    assert check["ok"] is True
    assert check["issues"] == []
    assert [warning["code"] for warning in check["warnings"]] == [
        "domain_support_missing"
    ]
    assert check["warnings"][0]["domain"] == "Unsupported domain"
    assert check["counts"]["supported_domains"] == 0
    assert check["truth_assessed"] is False


def test_check_flags_overall_more_severe_than_domains(tmp_path):
    graph, claim, _reference, _extraction, _plan, _source = _graph(tmp_path)
    assessment = ap.record_risk_of_bias(
        graph,
        target_id=claim["id"],
        tool="RoB 2",
        domains=[
            {"name": "Randomisation", "judgement": "low", "support": "Central."},
            {"name": "Missing data", "judgement": "low", "support": "Complete."},
        ],
        overall="high",
        reviewer="reviewer-a",
    )["assessment"]
    check = ap.check_risk_of_bias(graph, assessment["id"])
    assert check["ok"] is True
    assert [warning["code"] for warning in check["warnings"]] == [
        "overall_without_domain_signal"
    ]
    assert check["warnings"][0]["overall"] == "high"

    with_signal = ap.record_risk_of_bias(
        graph,
        target_id=claim["id"],
        tool="RoB 2",
        domains=[
            {"name": "Randomisation", "judgement": "high", "support": "Unblinded."},
        ],
        overall="high",
        reviewer="reviewer-a",
    )["assessment"]
    assert ap.check_risk_of_bias(graph, with_signal["id"])["warnings"] == []


def test_check_reports_missing_or_wrong_target(tmp_path):
    graph, claim, _reference, _extraction, review_plan, _source = _graph(tmp_path)
    missing = graph.record(
        "RiskOfBias",
        target_id="claim-9999",
        tool="RoB 2",
        domains=[{"name": "Domain", "judgement": "low", "support": "Recorded."}],
        overall="low",
        reviewer="reviewer-a",
        status="candidate",
        truth_assessed=False,
    )
    check = ap.check_risk_of_bias(graph, missing["id"])
    assert check["ok"] is False
    assert check["issues"][0]["code"] == "target_missing"

    wrong_type = graph.record(
        "RiskOfBias",
        target_id=review_plan["id"],
        tool="RoB 2",
        domains=[{"name": "Domain", "judgement": "low", "support": "Recorded."}],
        overall="low",
        reviewer="reviewer-a",
        status="candidate",
        truth_assessed=False,
    )
    check = ap.check_risk_of_bias(graph, wrong_type["id"])
    assert check["ok"] is False
    assert check["issues"][0]["code"] == "target_type_invalid"

    malformed = graph.record(
        "RiskOfBias",
        target_id="claim-0001",
        tool="RoB 2",
        domains="not a list",
        overall="low",
        reviewer="reviewer-a",
        status="candidate",
        truth_assessed=False,
    )
    check = ap.check_risk_of_bias(graph, malformed["id"])
    assert check["ok"] is False
    assert check["issues"][0]["code"] == "domains_missing"

    no_overall = graph.record(
        "RiskOfBias",
        target_id=claim["id"],
        tool="RoB 2",
        domains=[{"name": "Domain", "judgement": "low", "support": "Recorded."}],
        reviewer="reviewer-a",
        status="candidate",
        truth_assessed=False,
    )
    check = ap.check_risk_of_bias(graph, no_overall["id"])
    assert check["ok"] is False
    assert [issue["code"] for issue in check["issues"]] == ["overall_missing"]

    with pytest.raises(ap.AppraisalError, match="unknown risk-of-bias assessment"):
        ap.check_risk_of_bias(graph, "riskofbias-9999")
    with pytest.raises(ap.AppraisalError, match="not a RiskOfBias"):
        ap.check_risk_of_bias(graph, review_plan["id"])


# --------------------------------------------------------------------------- #
# certainty
# --------------------------------------------------------------------------- #
def test_record_certainty_links_grade_and_normalises_fields(tmp_path):
    graph, claim, _reference, _extraction, _plan, _source = _graph(tmp_path)
    result = ap.record_certainty(
        graph,
        target_id=claim["id"],
        rating="High",
        reasons=["Large consistent effect.", "Direct evidence.", "Direct evidence."],
        reviewer="reviewer-a",
        framework="grade",
        downgrades=[
            {
                "domain": "risk of bias",
                "level": 1,
                "reason": "Outcome assessment was unblinded.",
            }
        ],
        upgrades=[
            {
                "domain": "large effect",
                "level": 1,
                "reason": "The effect is unusually large.",
            }
        ],
        notes="Provisional certainty.",
    )
    certainty = result["certainty"]
    assert certainty["type"] == "Certainty"
    assert certainty["target_id"] == claim["id"]
    assert certainty["framework"] == "GRADE"
    assert certainty["rating"] == "high"
    assert certainty["reasons"] == [
        "Large consistent effect.",
        "Direct evidence.",
    ]
    assert certainty["downgrades"][0]["level"] == 1
    assert certainty["upgrades"][0]["domain"] == "large effect"
    assert certainty["reviewer"] == "reviewer-a"
    assert certainty["notes"] == "Provisional certainty."
    assert certainty["status"] == "candidate"
    assert certainty["truth_assessed"] is False
    assert result["target"]["id"] == claim["id"]
    assert result["truth_assessed"] is False
    assert any(
        edge["type"] == "grades"
        and edge["src"] == certainty["id"]
        and edge["tgt"] == claim["id"]
        for edge in graph.state()["edges"]
    )


def test_certainty_accepts_reference_and_review_plan_targets(tmp_path):
    graph, _claim, reference, _extraction, plan, _source = _graph(tmp_path)
    for target_id in (reference["id"], plan["id"]):
        record = ap.record_certainty(
            graph,
            target_id=target_id,
            rating="very low",
            reasons=["Indirect evidence."],
            reviewer="reviewer-a",
        )["certainty"]
        assert record["target_id"] == target_id
        assert record["rating"] == "very_low"
        assert record["framework"] == "GRADE"
        assert record["downgrades"] == []
        assert record["upgrades"] == []


def test_certainty_rejects_invalid_targets_ratings_and_reasons(tmp_path):
    graph, claim, _reference, extraction, _plan, source = _graph(tmp_path)
    with pytest.raises(ap.AppraisalError, match="unknown certainty target"):
        ap.record_certainty(
            graph,
            target_id="claim-9999",
            rating="high",
            reasons=["Reason."],
            reviewer="reviewer-a",
        )
    for target_id in (extraction["id"], source["id"]):
        with pytest.raises(ap.AppraisalError, match="must be one of"):
            ap.record_certainty(
                graph,
                target_id=target_id,
                rating="high",
                reasons=["Reason."],
                reviewer="reviewer-a",
            )
    for rating in ("", "moderate certainty", "very high", None, 3):
        with pytest.raises(ap.AppraisalError):
            ap.record_certainty(
                graph,
                target_id=claim["id"],
                rating=rating,
                reasons=["Reason."],
                reviewer="reviewer-a",
            )
    for reasons in ([], "Reason.", [""], [3], [None]):
        with pytest.raises(ap.AppraisalError):
            ap.record_certainty(
                graph,
                target_id=claim["id"],
                rating="high",
                reasons=reasons,
                reviewer="reviewer-a",
            )
    with pytest.raises(ap.AppraisalError, match="framework"):
        ap.record_certainty(
            graph,
            target_id=claim["id"],
            rating="high",
            reasons=["Reason."],
            reviewer="reviewer-a",
            framework=" ",
        )
    with pytest.raises(ap.AppraisalError, match="reviewer"):
        ap.record_certainty(
            graph,
            target_id=claim["id"],
            rating="high",
            reasons=["Reason."],
            reviewer=" ",
        )


@pytest.mark.parametrize(
    "keyword,value",
    [
        ("downgrades", "not a list"),
        ("downgrades", [None]),
        ("downgrades", [{"domain": "d", "level": -1, "reason": "r"}]),
        ("downgrades", [{"domain": "d", "level": True, "reason": "r"}]),
        ("downgrades", [{"domain": "d", "level": 1.5, "reason": "r"}]),
        ("downgrades", [{"domain": "", "level": 1, "reason": "r"}]),
        ("downgrades", [{"domain": "d", "level": 1, "reason": ""}]),
        ("downgrades", [{"domain": "d", "level": 1, "reason": "r", "extra": "x"}]),
        ("upgrades", [{"domain": "d", "level": -2, "reason": "r"}]),
        ("upgrades", [{"domain": "d", "level": 1}]),
    ],
)
def test_malformed_downgrades_and_upgrades_are_rejected(tmp_path, keyword, value):
    graph, claim, _reference, _extraction, _plan, _source = _graph(tmp_path)
    arguments = {
        "target_id": claim["id"],
        "rating": "moderate",
        "reasons": ["Reason."],
        "reviewer": "reviewer-a",
        keyword: value,
    }
    with pytest.raises(ap.AppraisalError):
        ap.record_certainty(graph, **arguments)


# --------------------------------------------------------------------------- #
# reports and tables
# --------------------------------------------------------------------------- #
def test_certainty_report_counts_and_high_certainty_warning(tmp_path):
    graph, claim, reference, _extraction, _plan, _source = _graph(tmp_path)
    high_rob = ap.record_risk_of_bias(
        graph,
        target_id=claim["id"],
        tool="rob2",
        domains=[
            {"name": "Randomisation", "judgement": "high", "support": "Unblinded."},
        ],
        overall="high",
        reviewer="reviewer-a",
    )["assessment"]
    ap.record_risk_of_bias(
        graph,
        target_id=reference["id"],
        tool="My Custom Checklist",
        domains=[{"name": "Reporting", "judgement": "low", "support": "Complete."}],
        overall="low",
        reviewer="reviewer-b",
    )
    high_certainty = ap.record_certainty(
        graph,
        target_id=claim["id"],
        rating="high",
        reasons=["Direct evidence."],
        reviewer="reviewer-a",
    )["certainty"]
    ap.record_certainty(
        graph,
        target_id=claim["id"],
        rating="low",
        reasons=["Indirect evidence."],
        reviewer="reviewer-a",
    )

    report = ap.certainty_report(graph)
    assert report["counts"]["certainty"] == 2
    assert report["counts"]["risk_of_bias"] == 2
    assert report["counts"]["by_rating"]["high"] == 1
    assert report["counts"]["by_rating"]["low"] == 1
    assert report["counts"]["by_tool"]["RoB 2"] == 1
    assert report["counts"]["by_tool"]["custom"] == 1
    assert report["counts"]["by_framework"] == {"GRADE": 2}
    assert report["counts"]["warnings"] == 1
    assert report["truth_assessed"] is False
    assert len(report["certainty"]) == 2
    assert len(report["risk_of_bias"]) == 2
    summary = next(
        item for item in report["risk_of_bias"] if item["id"] == high_rob["id"]
    )
    assert summary["overall"] == "high"
    assert summary["target_label"] == "Graph methods reduce error."
    assert summary["judgements"]["high"] == 1
    assert summary["check"]["ok"] is True
    warning = report["warnings"][0]
    assert warning["code"] == "high_certainty_with_risk_of_bias"
    assert warning["certainty_id"] == high_certainty["id"]
    assert warning["risk_of_bias_ids"] == [high_rob["id"]]

    filtered = ap.certainty_report(graph.state(), target_id=reference["id"])
    assert filtered["counts"]["certainty"] == 0
    assert filtered["counts"]["risk_of_bias"] == 1
    assert filtered["risk_of_bias"][0]["tool"] == "custom"
    assert filtered["warnings"] == []

    with pytest.raises(ap.AppraisalError, match="unknown target"):
        ap.certainty_report(graph, target_id="claim-9999")


def test_appraisal_table_markdown_and_csv_are_deterministic(tmp_path):
    graph, claim, _reference, _extraction, _plan, _source = _graph(tmp_path)
    assessment = ap.record_risk_of_bias(
        graph,
        target_id=claim["id"],
        tool="rob2",
        domains=[
            {
                "name": "Randomisation",
                "judgement": "low",
                "support": "Central allocation sequence.",
            }
        ],
        overall="low",
        reviewer="reviewer-a",
    )["assessment"]
    certainty = ap.record_certainty(
        graph,
        target_id=claim["id"],
        rating="moderate",
        reasons=["Direct evidence."],
        reviewer="reviewer-b",
        downgrades=[{"domain": "risk of bias", "level": 1, "reason": "Unblinded."}],
    )["certainty"]

    table = ap.appraisal_table(graph)
    assert table["format"] == "markdown"
    assert table["count"] == 2
    assert [row["record_id"] for row in table["rows"]] == [
        assessment["id"],
        certainty["id"],
    ]
    assert [row["kind"] for row in table["rows"]] == ["risk_of_bias", "certainty"]
    assert table["content"] == ap.appraisal_table(graph.state())["content"]
    assert "Graph methods reduce error." in table["content"]
    assert "RoB 2" in table["content"]
    assert "Randomisation: low (Central allocation sequence.)" in table["content"]
    assert "Direct evidence." in table["content"]
    assert table["truth_assessed"] is False

    csv_table = ap.appraisal_table(graph, format="CSV")
    assert csv_table["format"] == "csv"
    assert csv_table["content"] == ap.appraisal_table(graph, format="csv")["content"]
    lines = csv_table["content"].splitlines()
    assert lines[0] == (
        "record_id,kind,target_id,target_type,target,tool,judgement,"
        "reviewer,support_summary,status"
    )
    assert len(lines) == 3
    assert "Randomisation: low (Central allocation sequence.)" in csv_table["content"]
    assert ap.appraisal_table(graph, format="md")["content"] == table["content"]

    with pytest.raises(ap.AppraisalError, match="markdown or csv"):
        ap.appraisal_table(graph, format="html")
