from __future__ import annotations

import pytest

from ozagia.research import theory as th
from ozagia.research.graph import ResearchGraph


def _graph(tmp_path, investigation_id: str = "theory-tests") -> ResearchGraph:
    return ResearchGraph.create(
        "Theory competition tests",
        root=tmp_path,
        investigation_id=investigation_id,
    )


def _theory(graph, name: str, **fields):
    fields.setdefault("statement", f"{name} explains the observed pattern.")
    fields.setdefault("scope", "the recorded corpus")
    return th.record_theory(graph, name=name, **fields)


def _claim(graph, wording: str, **fields):
    fields.setdefault("claim_type", "hypothesis")
    fields.setdefault("status", "unresolved")
    fields.setdefault("author", "reader")
    fields.setdefault("scope", "the recorded corpus")
    return graph.record("Claim", wording=wording, **fields)


def _evidence(graph, wording: str):
    return graph.record(
        "Evidence",
        wording=wording,
        evidence_type="experiment",
        status="recorded",
    )


def _observation(graph, wording: str):
    return graph.record(
        "Observation",
        wording=wording,
        status="recorded",
    )


def _edges(graph, relation: str):
    return [
        edge
        for edge in graph.state()["edges"]
        if edge["type"] == relation
    ]


# --------------------------------------------------------------------------- #
# recording
# --------------------------------------------------------------------------- #
def test_record_theory_creates_and_links_assumptions_and_predictions(tmp_path):
    graph = _graph(tmp_path)
    result = th.record_theory(
        graph,
        name="Shared attention",
        statement="Attention is divided across a shared workspace.",
        scope="adults performing two tasks",
        mechanism="limited shared capacity",
        assumptions=["Capacity is limited.", "Capacity is limited.", "Tasks compete."],
        predictions=["Errors rise with dual-task load."],
        reviewer="reviewer-a",
        status="active",
        notes="Candidate account.",
    )

    theory = result["theory"]
    assert theory["type"] == "Theory"
    assert theory["name"] == "Shared attention"
    assert theory["status"] == "active"
    assert theory["mechanism"] == "limited shared capacity"
    assert theory["reviewer"] == "reviewer-a"
    assert theory["notes"] == "Candidate account."
    assert theory["truth_assessed"] is False
    assert result["truth_assessed"] is False

    assert [item["wording"] for item in result["assumptions"]] == [
        "Capacity is limited.",
        "Tasks compete.",
    ]
    for assumption in result["assumptions"]:
        assert assumption["type"] == "Assumption"
        assert assumption["theory_id"] == theory["id"]
        assert assumption["status"] == "assumed"
        assert assumption["truth_assessed"] is False

    assert len(result["predictions"]) == 1
    prediction = result["predictions"][0]
    assert prediction["type"] == "Prediction"
    assert prediction["wording"] == "Errors rise with dual-task load."
    assert prediction["theory_id"] == theory["id"]
    assert prediction["status"] == "untested"

    assert {
        (edge["src"], edge["tgt"])
        for edge in _edges(graph, "assumes")
    } == {
        (theory["id"], assumption["id"])
        for assumption in result["assumptions"]
    }
    assert {
        (edge["src"], edge["tgt"])
        for edge in _edges(graph, "predicts")
    } == {(theory["id"], prediction["id"])}


def test_record_theory_links_existing_assumptions_and_predictions(tmp_path):
    graph = _graph(tmp_path)
    assumption = graph.record("Assumption", wording="An existing assumption.")
    prediction = graph.record("Prediction", wording="An existing prediction.")
    result = th.record_theory(
        graph,
        name="Extended theory",
        statement="The existing pieces belong to this account.",
        scope="the recorded corpus",
        assumption_ids=[assumption["id"]],
        prediction_ids=[prediction["id"]],
    )

    assert [item["id"] for item in result["assumptions"]] == [assumption["id"]]
    assert [item["id"] for item in result["predictions"]] == [prediction["id"]]
    assert any(
        edge["type"] == "assumes"
        and edge["src"] == result["theory"]["id"]
        and edge["tgt"] == assumption["id"]
        for edge in graph.state()["edges"]
    )
    assert any(
        edge["type"] == "predicts"
        and edge["src"] == result["theory"]["id"]
        and edge["tgt"] == prediction["id"]
        for edge in graph.state()["edges"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("statement", "   "),
        ("scope", ""),
        ("status", "accepted"),
    ],
)
def test_record_theory_rejects_invalid_fields(tmp_path, field, value):
    graph = _graph(tmp_path)
    fields = {
        "name": "A theory",
        "statement": "A statement.",
        "scope": "a scope",
        field: value,
    }
    with pytest.raises(th.TheoryError):
        th.record_theory(graph, **fields)


def test_record_theory_rejects_bad_existing_references(tmp_path):
    graph = _graph(tmp_path)
    claim = _claim(graph, "A claim is not an assumption.")

    with pytest.raises(th.TheoryError, match="assumption_ids"):
        th.record_theory(
            graph,
            name="Bad assumptions",
            statement="Statement.",
            scope="scope",
            assumption_ids="assumption-0001",
        )
    with pytest.raises(th.TheoryError, match="unknown prediction"):
        th.record_theory(
            graph,
            name="Bad prediction",
            statement="Statement.",
            scope="scope",
            prediction_ids=["prediction-9999"],
        )
    with pytest.raises(th.TheoryError, match="must be one of"):
        th.record_theory(
            graph,
            name="Wrong assumption type",
            statement="Statement.",
            scope="scope",
            assumption_ids=[claim["id"]],
        )


# --------------------------------------------------------------------------- #
# rivalry and compatibility
# --------------------------------------------------------------------------- #
def test_record_rivalry_is_symmetric_and_deduplicates(tmp_path):
    graph = _graph(tmp_path)
    first = _theory(graph, "First theory")
    second = _theory(graph, "Second theory")

    created = th.record_rivalry(
        graph,
        first["theory"]["id"],
        second["theory"]["id"],
        scope="the same task",
        rationale="They make incompatible load predictions.",
        reviewer="reviewer-a",
    )
    assert created["created"] is True
    edge = created["edge"]
    assert edge["type"] == "rival_of"
    assert edge["provenance"] == {
        "kind": "theory_rivalry",
        "scope": "the same task",
        "rationale": "They make incompatible load predictions.",
        "reviewer": "reviewer-a",
    }
    assert created["truth_assessed"] is False

    duplicate = th.record_rivalry(
        graph,
        second["theory"]["id"],
        first["theory"]["id"],
        scope="the same task",
        rationale="They make incompatible load predictions.",
        reviewer="reviewer-a",
    )
    assert duplicate["created"] is False
    assert duplicate["edge"]["id"] == edge["id"]
    assert len(_edges(graph, "rival_of")) == 1

    different = th.record_rivalry(
        graph,
        second["theory"]["id"],
        first["theory"]["id"],
        scope="a narrower task",
        rationale="They disagree only in the narrow scope.",
        reviewer="reviewer-b",
    )
    assert different["created"] is True
    assert len(_edges(graph, "rival_of")) == 2

    forward = th.theory_compare(graph, first["theory"]["id"], second["theory"]["id"])
    reverse = th.theory_compare(graph, second["theory"]["id"], first["theory"]["id"])
    assert {
        record["edge_id"] for record in forward["rivalry"]
    } == {
        record["edge_id"] for record in reverse["rivalry"]
    }
    assert all(record["kind"] == "theory_rivalry" for record in forward["rivalry"])


def test_record_rivalry_rejects_self_and_non_theory(tmp_path):
    graph = _graph(tmp_path)
    theory = _theory(graph, "Only theory")
    claim = _claim(graph, "Not a theory.")

    with pytest.raises(th.TheoryError, match="own rival"):
        th.record_rivalry(graph, theory["theory"]["id"], theory["theory"]["id"])
    with pytest.raises(th.TheoryError, match="must be a Theory"):
        th.record_rivalry(graph, theory["theory"]["id"], claim["id"])


def test_record_compatibility_is_graded_undirected_and_deduplicated(tmp_path):
    graph = _graph(tmp_path)
    first = _theory(graph, "First theory")
    second = _theory(graph, "Second theory")
    claim = _claim(graph, "Compatibility evidence.")

    created = th.record_compatibility(
        graph,
        first["theory"]["id"],
        second["theory"]["id"],
        degree="partially compatible",
        scope="adults",
        rationale="They agree on the direction but not the size.",
        reviewer="reviewer-a",
        evidence_ids=[claim["id"]],
    )
    assert created["created"] is True
    edge = created["edge"]
    assert edge["type"] == "compatible_with"
    assert edge["provenance"] == {
        "kind": "theory_compatibility",
        "degree": "partially_compatible",
        "scope": "adults",
        "rationale": "They agree on the direction but not the size.",
        "reviewer": "reviewer-a",
        "evidence_ids": [claim["id"]],
    }
    assert created["truth_assessed"] is False

    duplicate = th.record_compatibility(
        graph,
        second["theory"]["id"],
        first["theory"]["id"],
        degree="partially_compatible",
        scope="adults",
    )
    assert duplicate["created"] is False
    assert duplicate["edge"]["id"] == edge["id"]
    assert len(_edges(graph, "compatible_with")) == 1

    other_degree = th.record_compatibility(
        graph,
        second["theory"]["id"],
        first["theory"]["id"],
        degree="orthogonal",
        scope="adults",
    )
    assert other_degree["created"] is True
    assert len(_edges(graph, "compatible_with")) == 2

    with pytest.raises(th.TheoryError, match="degree must be one of"):
        th.record_compatibility(
            graph,
            first["theory"]["id"],
            second["theory"]["id"],
            degree="mostly_fine",
        )
    with pytest.raises(th.TheoryError, match="unknown compatibility evidence"):
        th.record_compatibility(
            graph,
            first["theory"]["id"],
            second["theory"]["id"],
            degree="compatible",
            evidence_ids=["evidence-9999"],
        )


def test_link_theory_evidence_checks_endpoints_and_deduplicates(tmp_path):
    graph = _graph(tmp_path)
    theory = _theory(graph, "Linked theory")["theory"]
    claim = _claim(graph, "Supported claim.")
    evidence = _evidence(graph, "Challenging evidence.")
    observation = _observation(graph, "Discriminating observation.")
    prediction = _theory(graph, "Prediction owner", predictions=["A prediction."])[
        "predictions"
    ][0]

    supported = th.link_theory_evidence(
        graph,
        theory["id"],
        claim["id"],
        relation="supported_by",
        rationale="The claim supports the theory.",
        reviewer="reviewer-a",
    )
    assert supported["created"] is True
    assert supported["edge"]["type"] == "supported_by"
    assert supported["truth_assessed"] is False
    duplicate = th.link_theory_evidence(graph, theory["id"], claim["id"])
    assert duplicate["created"] is False
    assert duplicate["edge"]["id"] == supported["edge"]["id"]

    challenged = th.link_theory_evidence(
        graph, theory["id"], evidence["id"], relation="challenged_by"
    )
    discriminated = th.link_theory_evidence(
        graph, theory["id"], observation["id"], relation="discriminated_by"
    )
    explained = th.link_theory_evidence(
        graph, theory["id"], claim["id"], relation="explains"
    )
    assert challenged["created"] is True
    assert discriminated["created"] is True
    assert explained["created"] is True

    with pytest.raises(th.TheoryError, match="must be one of"):
        th.link_theory_evidence(
            graph, theory["id"], prediction["id"], relation="supported_by"
        )
    with pytest.raises(th.TheoryError, match="must be one of"):
        th.link_theory_evidence(
            graph, theory["id"], claim["id"], relation="discriminated_by"
        )
    with pytest.raises(th.TheoryError, match="relation must be one of"):
        th.link_theory_evidence(graph, theory["id"], claim["id"], relation="proves")


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
def test_theory_compare_reports_shared_and_unique_evidence(tmp_path):
    graph = _graph(tmp_path)
    first = _theory(graph, "First theory")["theory"]
    second = _theory(graph, "Second theory")["theory"]
    shared = _claim(graph, "Shared evidence.")
    unique_first = _evidence(graph, "Unique first evidence.")
    unique_second = _observation(graph, "Unique second observation.")

    th.link_theory_evidence(graph, first["id"], shared["id"])
    th.link_theory_evidence(graph, second["id"], shared["id"])
    th.link_theory_evidence(graph, first["id"], unique_first["id"])
    th.link_theory_evidence(
        graph, second["id"], unique_second["id"], relation="challenged_by"
    )

    result = th.theory_compare(graph, first["id"], second["id"])

    assert result["theory_a"]["id"] == first["id"]
    assert result["theory_b"]["id"] == second["id"]
    assert result["supported"] == {
        "a": sorted([shared["id"], unique_first["id"]]),
        "b": [shared["id"]],
    }
    assert result["challenged"] == {"a": [], "b": [unique_second["id"]]}
    assert result["shared_evidence"] == [shared["id"]]
    assert result["unique_a"] == [unique_first["id"]]
    assert result["unique_b"] == [unique_second["id"]]
    assert result["overlap_ratio"] == pytest.approx(1 / 3, abs=1e-6)
    assert result["discernment"]["status"] == "unique_but_not_discriminating"
    assert result["truth_assessed"] is False


def test_theory_compare_distinguishes_discriminating_from_merely_unique(tmp_path):
    graph = _graph(tmp_path)
    first = _theory(graph, "First theory")["theory"]
    second = _theory(graph, "Second theory")["theory"]
    unique_first = _evidence(graph, "Merely unique evidence.")
    unique_second = _observation(graph, "Other unique observation.")
    th.link_theory_evidence(graph, first["id"], unique_first["id"])
    th.link_theory_evidence(graph, second["id"], unique_second["id"])

    before = th.theory_compare(graph, first["id"], second["id"])
    assert before["discernment"]["status"] == "unique_but_not_discriminating"
    assert before["discriminating_evidence"] == []
    assert unique_first["id"] in before["unique_a"]
    assert unique_first["id"] not in before["discriminating_evidence"]

    discriminating = _observation(graph, "A result only one theory predicts.")
    th.link_theory_evidence(
        graph,
        first["id"],
        discriminating["id"],
        relation="discriminated_by",
        rationale="Only the first theory predicts this observation.",
        reviewer="reviewer-a",
    )
    after = th.theory_compare(graph, first["id"], second["id"])
    assert after["discernment"]["status"] == "discriminating_evidence_present"
    assert after["discriminating_evidence"] == [discriminating["id"]]
    assert unique_first["id"] not in after["discriminating_evidence"]
    record = next(
        item
        for item in after["discriminating_targets"]
        if item["target_id"] == discriminating["id"]
    )
    assert record["is_evidence"] is True
    assert record["uniqueness"] == "not_in_evidence_sets"


def test_theory_compare_insufficient_to_discern_statuses(tmp_path):
    graph = _graph(tmp_path)
    first = _theory(graph, "First theory")["theory"]
    second = _theory(graph, "Second theory")["theory"]

    no_evidence = th.theory_compare(graph, first["id"], second["id"])
    assert no_evidence["discernment"]["status"] == "no_evidence"
    assert no_evidence["shared_evidence"] == []
    assert no_evidence["unique_a"] == []
    assert no_evidence["unique_b"] == []
    assert no_evidence["overlap_ratio"] == 0.0

    shared = _claim(graph, "Shared evidence.")
    th.link_theory_evidence(graph, first["id"], shared["id"])
    th.link_theory_evidence(graph, second["id"], shared["id"])
    shared_only = th.theory_compare(graph, first["id"], second["id"])
    assert shared_only["discernment"]["status"] == "shared_only"
    assert shared_only["shared_evidence"] == [shared["id"]]

    unique = _evidence(graph, "Unique evidence.")
    th.link_theory_evidence(graph, first["id"], unique["id"])
    unique_only = th.theory_compare(graph, first["id"], second["id"])
    assert unique_only["discernment"]["status"] == "unique_but_not_discriminating"

    discriminating = _observation(graph, "Discriminating observation.")
    th.link_theory_evidence(
        graph, second["id"], discriminating["id"], relation="discriminated_by"
    )
    discriminating_present = th.theory_compare(graph, first["id"], second["id"])
    assert (
        discriminating_present["discernment"]["status"]
        == "discriminating_evidence_present"
    )
    assert all(
        "No theory is selected" in reason
        for reason in [
            no_evidence["discernment"]["reasons"][-1],
            shared_only["discernment"]["reasons"][-1],
            unique_only["discernment"]["reasons"][-1],
            discriminating_present["discernment"]["reasons"][-1],
        ]
    )


def test_discriminating_predictions_do_not_count_as_discriminating_evidence(tmp_path):
    graph = _graph(tmp_path)
    first = _theory(graph, "First theory", predictions=["A unique prediction."])
    second = _theory(graph, "Second theory")["theory"]
    prediction = first["predictions"][0]
    th.link_theory_evidence(
        graph,
        first["theory"]["id"],
        prediction["id"],
        relation="discriminated_by",
        rationale="The test would separate the theories, but has not run.",
    )

    result = th.theory_compare(graph, first["theory"]["id"], second["id"])
    assert result["discernment"]["status"] == "no_evidence"
    assert result["discriminating_evidence"] == []
    assert result["discriminating_predictions"] == [prediction["id"]]
    assert "Discriminating predictions are recorded" in " ".join(
        result["discernment"]["reasons"]
    )


def test_theory_compare_reports_predictions_tests_and_evidence_profiles(tmp_path):
    graph = _graph(tmp_path)
    first = _theory(
        graph,
        "First theory",
        predictions=["The error rate rises with load."],
    )
    second = _theory(graph, "Second theory")["theory"]
    prediction = first["predictions"][0]
    observation = _observation(graph, "The error rate rose with load.")
    graph.link(prediction["id"], "tests", observation["id"])

    claim = _claim(graph, "A linked claim.")
    th.link_theory_evidence(graph, first["theory"]["id"], claim["id"])
    certainty = graph.record(
        "Certainty",
        target_id=claim["id"],
        framework="GRADE",
        rating="Low",
        reasons=["Small sample."],
        reviewer="reviewer-a",
        status="candidate",
        truth_assessed=False,
    )
    graph.link(certainty["id"], "grades", claim["id"])
    risk = graph.record(
        "RiskOfBias",
        target_id=claim["id"],
        tool="RoB 2",
        domains=[],
        overall="Some Concerns",
        reviewer="reviewer-a",
        status="candidate",
        truth_assessed=False,
    )
    graph.link(risk["id"], "assesses", claim["id"])
    extraction = graph.record(
        "Extraction",
        target_id=claim["id"],
        fields={"study_design": "randomised_trial"},
        study_design="randomised_trial",
        extractor="reader",
        status="recorded",
        truth_assessed=False,
    )
    th.link_theory_evidence(graph, first["theory"]["id"], extraction["id"])

    result = th.theory_compare(
        graph, first["theory"]["id"], second["id"]
    )
    prediction_record = result["predictions"]["a"][0]
    assert prediction_record["tested"] is True
    assert [item["id"] for item in prediction_record["tests"]] == [observation["id"]]

    profile = result["evidence_profiles"]["a"]
    assert profile["supported_by_type"] == {"Claim": 1, "Extraction": 1}
    assert profile["challenged_by_type"] == {}
    assert profile["certainty"] == {"total": 1, "by_rating": {"low": 1}}
    assert profile["risk_of_bias"] == {
        "total": 1,
        "by_judgement": {"some_concerns": 1},
    }
    assert profile["extraction_study_designs"] == {"randomised_trial": 1}
    assert result["truth_assessed"] is False


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def test_theory_scan_reports_gaps_conflicts_and_counts(tmp_path):
    graph = _graph(tmp_path)
    no_data = _theory(graph, "No data theory")["theory"]
    untested = _theory(graph, "Untested theory", predictions=["A prediction."])
    tested = _theory(graph, "Tested theory", predictions=["Another prediction."])
    tested_prediction = tested["predictions"][0]
    tested_observation = _observation(graph, "A completed observation.")
    graph.link(tested_prediction["id"], "tests", tested_observation["id"])
    shared = _evidence(graph, "Shared evidence.")
    th.link_theory_evidence(graph, untested["theory"]["id"], shared["id"])
    th.link_theory_evidence(graph, tested["theory"]["id"], shared["id"])

    th.record_rivalry(
        graph,
        no_data["id"],
        untested["theory"]["id"],
        rationale="They disagree about the mechanism.",
        reviewer="reviewer-a",
    )
    th.record_rivalry(
        graph,
        untested["theory"]["id"],
        tested["theory"]["id"],
        rationale="They disagree about the load prediction.",
        reviewer="reviewer-a",
    )
    th.record_compatibility(
        graph,
        untested["theory"]["id"],
        tested["theory"]["id"],
        degree="partially_compatible",
        rationale="They agree on direction.",
        reviewer="reviewer-b",
    )

    unexplained = _claim(graph, "A claim no theory explains.")
    explained = _claim(graph, "A claim one theory explains.")
    graph.link(tested["theory"]["id"], "explains", explained["id"])

    result = th.theory_scan(graph)

    assert [item["theory_id"] for item in result["theories_without_predictions"]] == [
        no_data["id"]
    ]
    assert [item["prediction_id"] for item in result["untested_predictions"]] == [
        untested["predictions"][0]["id"]
    ]
    assert [item["theory_id"] for item in result["theories_without_evidence"]] == [
        no_data["id"]
    ]
    assert {
        (item["theory_a"], item["theory_b"])
        for item in result["rival_pairs_with_shared_only_evidence"]
    } == {(untested["theory"]["id"], tested["theory"]["id"])}
    assert {
        (item["theory_a"], item["theory_b"])
        for item in result["rival_pairs_without_discriminating_evidence"]
    } == {
        tuple(sorted((no_data["id"], untested["theory"]["id"]))),
        tuple(sorted((untested["theory"]["id"], tested["theory"]["id"]))),
    }
    assert [
        item["claim_id"] for item in result["claims_not_explained_by_any_theory"]
    ] == [unexplained["id"]]
    assert len(result["contradictory_relations"]) == 1
    contradiction = result["contradictory_relations"][0]
    assert contradiction["compatibility_degrees"] == ["partially_compatible"]
    assert contradiction["theory_a"] < contradiction["theory_b"]

    counts = result["counts"]
    assert counts["theories"] == 3
    assert counts["predictions"] == 2
    assert counts["untested_predictions"] == 1
    assert counts["theories_without_predictions"] == 1
    assert counts["theories_without_evidence"] == 1
    assert counts["rival_pairs"] == 2
    assert counts["rival_pairs_with_shared_only_evidence"] == 1
    assert counts["rival_pairs_without_discriminating_evidence"] == 2
    assert counts["contradictory_relations"] == 1
    assert result["truth_assessed"] is False

    assert result == th.theory_scan(graph.state())
    assert result == th.theory_scan(graph)


# --------------------------------------------------------------------------- #
# report and matrix
# --------------------------------------------------------------------------- #
def test_theory_report_single_and_all_are_deterministic(tmp_path):
    graph = _graph(tmp_path)
    first = _theory(
        graph,
        "First theory",
        assumptions=["An assumption."],
        predictions=["A prediction."],
    )
    second = _theory(graph, "Second theory")
    shared = _claim(graph, "Shared evidence.")
    th.link_theory_evidence(graph, first["theory"]["id"], shared["id"])
    th.link_theory_evidence(graph, second["theory"]["id"], shared["id"])
    th.link_theory_evidence(
        graph, first["theory"]["id"], shared["id"], relation="explains"
    )
    th.record_rivalry(
        graph,
        first["theory"]["id"],
        second["theory"]["id"],
        scope="the same scope",
        rationale="Competing mechanisms.",
        reviewer="reviewer-a",
    )
    th.record_compatibility(
        graph,
        first["theory"]["id"],
        second["theory"]["id"],
        degree="partially_compatible",
        scope="the same scope",
        rationale="They agree on the direction.",
        reviewer="reviewer-b",
    )

    report = th.theory_report(graph, first["theory"]["id"])
    assert report["theory"]["id"] == first["theory"]["id"]
    assert [item["id"] for item in report["assumptions"]] == [
        first["assumptions"][0]["id"]
    ]
    assert [item["prediction"]["id"] for item in report["predictions"]] == [
        first["predictions"][0]["id"]
    ]
    assert [item["id"] for item in report["evidence"]["supported"]] == [shared["id"]]
    assert [item["id"] for item in report["evidence"]["explains"]] == [shared["id"]]
    assert [item["id"] for item in report["claims"]] == [shared["id"]]
    assert [item["theory_id"] for item in report["rivals"]] == [second["theory"]["id"]]
    assert report["compatibility"][0]["degree"] == "partially_compatible"
    assert report["compatibility"][0]["scope"] == "the same scope"
    assert report["compatibility"][0]["reviewer"] == "reviewer-b"
    assert report["checks"]["ok"] is True
    assert report["truth_assessed"] is False

    all_report = th.theory_report(graph)
    assert [item["theory"]["id"] for item in all_report["theories"]] == [
        first["theory"]["id"],
        second["theory"]["id"],
    ]
    assert all_report["counts"]["theories"] == 2
    assert all_report["counts"]["rivals"] == 2
    assert all_report["truth_assessed"] is False
    assert all_report == th.theory_report(graph.state())
    assert all_report == th.theory_report(graph)

    with pytest.raises(th.TheoryError, match="unknown theory"):
        th.theory_report(graph, "theory-9999")


def test_compatibility_matrix_marks_missing_pairs(tmp_path):
    graph = _graph(tmp_path)
    first = _theory(graph, "First theory")["theory"]
    second = _theory(graph, "Second theory")["theory"]
    third = _theory(graph, "Third theory")["theory"]
    shared = _claim(graph, "Shared evidence.")
    unique_first = _evidence(graph, "First unique evidence.")
    unique_second = _observation(graph, "Second unique observation.")
    th.link_theory_evidence(graph, first["id"], shared["id"])
    th.link_theory_evidence(graph, second["id"], shared["id"])
    th.link_theory_evidence(graph, first["id"], unique_first["id"])
    th.link_theory_evidence(graph, second["id"], unique_second["id"])

    th.record_compatibility(
        graph,
        first["id"],
        second["id"],
        degree="partially_compatible",
        scope="adults",
        rationale="Partial agreement.",
        reviewer="reviewer-a",
        evidence_ids=[shared["id"]],
    )
    th.record_compatibility(
        graph,
        third["id"],
        first["id"],
        degree="incompatible",
        scope="children",
        rationale="Different predictions.",
        reviewer="reviewer-b",
    )

    matrix = th.compatibility_matrix(graph)
    assert [item["id"] for item in matrix["theories"]] == [
        first["id"],
        second["id"],
        third["id"],
    ]
    rows = {
        (row["theory_a"], row["theory_b"], row["relation"]): row
        for row in matrix["pairs"]
    }
    assert set(rows) == {
        tuple(sorted((first["id"], second["id"]))) + ("compatible_with",),
        tuple(sorted((first["id"], third["id"]))) + ("compatible_with",),
        tuple(sorted((second["id"], third["id"]))) + ("missing",),
    }

    partial = rows[
        tuple(sorted((first["id"], second["id"]))) + ("compatible_with",)
    ]
    assert partial["degree"] == "partially_compatible"
    assert partial["scope"] == "adults"
    assert partial["rationale"] == "Partial agreement."
    assert partial["reviewer"] == "reviewer-a"
    assert partial["evidence_ids"] == [shared["id"]]
    assert partial["shared_evidence_count"] == 1
    assert partial["unique_a_count"] == 1
    assert partial["unique_b_count"] == 1

    incompatible = rows[
        tuple(sorted((first["id"], third["id"]))) + ("compatible_with",)
    ]
    assert incompatible["degree"] == "incompatible"

    missing = rows[
        tuple(sorted((second["id"], third["id"]))) + ("missing",)
    ]
    assert missing["missing"] is True
    assert missing["degree"] is None
    assert missing["scope"] is None
    assert missing["rationale"] is None
    assert missing["reviewer"] is None
    assert missing["edge_id"] is None
    assert missing["evidence_ids"] == []
    assert matrix["counts"] == {
        "theories": 3,
        "pairs": 3,
        "recorded": 2,
        "missing": 1,
    }
    assert matrix["pairs"] == sorted(
        matrix["pairs"],
        key=lambda row: (
            row["theory_a"],
            row["theory_b"],
            str(row["edge_id"] or ""),
        ),
    )
    assert matrix == th.compatibility_matrix(graph.state())
    assert matrix == th.compatibility_matrix(graph)
    assert matrix["truth_assessed"] is False


# --------------------------------------------------------------------------- #
# no truth verdicts
# --------------------------------------------------------------------------- #
def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_all_theory_outputs_retain_no_truth_verdict(tmp_path):
    graph = _graph(tmp_path)
    first = _theory(
        graph,
        "First theory",
        assumptions=["An assumption."],
        predictions=["A prediction."],
    )
    second = _theory(graph, "Second theory")
    evidence = _evidence(graph, "An evidence item.")
    th.link_theory_evidence(graph, first["theory"]["id"], evidence["id"])
    th.record_rivalry(graph, first["theory"]["id"], second["theory"]["id"])
    th.record_compatibility(
        graph,
        first["theory"]["id"],
        second["theory"]["id"],
        degree="compatible",
    )
    outputs = [
        first,
        th.theory_compare(graph, first["theory"]["id"], second["theory"]["id"]),
        th.theory_scan(graph),
        th.theory_report(graph),
        th.theory_report(graph, first["theory"]["id"]),
        th.compatibility_matrix(graph),
    ]
    for output in outputs:
        assert output["truth_assessed"] is False
        keys = {key.casefold() for key in _keys(output)}
        assert not {
            "truth_score",
            "truth_verdict",
            "verdict",
            "winner",
            "correct_theory",
            "probability",
        } & keys
