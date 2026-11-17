"""Tests for the optional Ocelot research-projection join.

All engines here are injected or fake workers.  No test resolves, installs or
executes a real Ocelot checkout, and no test uses the network or a model.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ozagia.core import contracts as C
from ozagia.core import ocelot_code as OC
from ozagia.core import research_projection as RP
from ozagia.research import ocelot_projection as OP
from ozagia.research.graph import ResearchGraph

CANARY = "sk-CANARY-0123456789abcdef"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _session(root: Path, session_id: str, paper: Path, digest: str,
             *, canary: bool = False) -> None:
    graph = ResearchGraph.create(
        f"Question {session_id}?",
        root=root,
        mode="empirical",
        roots=[{"root_id": "papers", "path": str(paper.parent),
                "files": [paper.name]}],
        investigation_id=session_id,
    )
    source = graph.record(
        "Source", name="paper", source_type="article",
        locator={"root_id": "papers", "path": paper.name, "sha256": digest,
                 "kind": "paper"})
    evidence = graph.record(
        "Evidence", evidence_type="extracted_result",
        wording="Table 1 reports a lower median latency", status="recorded")
    assumption = graph.record(
        "Assumption", wording="the benchmark workload is representative",
        status="open")
    empirical = graph.record(
        "Claim",
        wording=("the cache reduces latency " + CANARY) if canary
        else "the cache reduces median latency",
        claim_type="empirical_observation", status="open")
    theorem = graph.record(
        "Claim", wording="the bound holds for every n",
        claim_type="theorem", status="open")
    hypothesis = graph.record(
        "Claim", wording="the effect generalises to other caches",
        claim_type="hypothesis", status="open")
    graph.record(
        "Claim", wording="a loose conjecture with no source",
        claim_type="hypothesis", status="open")
    for claim in (empirical, theorem, hypothesis):
        graph.link(claim["id"], "cites", source["id"],
                   provenance={"kind": "source_import"})
    graph.link(empirical["id"], "assumes", assumption["id"])
    graph.link(evidence["id"], "supports", empirical["id"],
               provenance={"kind": "extracted_table"})
    graph.link(evidence["id"], "cites", source["id"])


def _fixture(root: Path, *, canary: bool = False) -> dict:
    paper = root / "papers" / "paper.md"
    _write(paper, "# Paper\n\nThe cache reduces latency.\n")
    digest = hashlib.sha256(paper.read_bytes()).hexdigest()
    _session(root, "research-session-a", paper, digest, canary=canary)
    _session(root, "research-session-b", paper, digest)
    return {
        "report": RP.build_projection(root, observed_at=1_700_000_000_000),
        "paper": paper,
        "digest": digest,
    }


def _clusters(payload: dict) -> list[list[str]]:
    by_target: dict[str, list[str]] = {}
    for row in payload["findings"]:
        by_target.setdefault(row["target_id"], []).append(row["finding_id"])
    return [sorted(ids) for ids in by_target.values() if len(ids) > 1]


class FakeEngine:
    """A deterministic worker-shaped engine with no Ocelot dependency."""

    def __init__(self, *, version: str | None = "1.4.0", truth: bool = False,
                 wrong_hash: bool = False, cross_anchor: bool = True,
                 error: str | None = None) -> None:
        self.version = version
        self.truth = truth
        self.wrong_hash = wrong_hash
        self.cross_anchor = cross_anchor
        self.error = error
        self.called = False
        self.payload: dict | None = None
        self.rows: list[dict] = []

    def __call__(self, payload: dict) -> dict:
        self.called = True
        self.payload = payload
        self.rows = list(payload.get("findings") or [])
        if self.error is not None:
            raise RuntimeError(self.error)
        clusters = _clusters(payload)
        analogies = [
            {"source_finding": ids[0], "target_finding": ids[1], "order_n": 4}
            for ids in clusters
        ]
        all_ids = sorted(row["finding_id"] for row in self.rows)
        if self.cross_anchor and len(all_ids) > 2:
            clusters.append([all_ids[0], all_ids[-1]])
            analogies.append({"source_finding": all_ids[0],
                              "target_finding": all_ids[-1]})
        output = {
            "state": "ready",
            "available": True,
            "engine": OP.ENGINE_ID,
            "engine_version": self.version,
            "clusters": clusters,
            "analogies": analogies,
        }
        if not self.wrong_hash:
            output["input_hash"] = payload["input_hash"]
        else:
            output["input_hash"] = "0" * 64
        if self.truth:
            output.update({
                "truth_assessed": True,
                "verified": True,
                "verification": {"state": "verified"},
                "support_edges": [["claim-a", "claim-b"]],
                "support_edges_created": True,
            })
        return output


def _not_installed(root=None, *, dest=None):
    return {"state": "not_installed", "path": None, "version": None,
            "source": None, "error": None, "note": "not installed"}


def _ready(root=None, *, dest=None):
    return {"state": "ready", "path": "/opt/ocelot-checkout",
            "version": "1.4.0", "source": "sibling", "error": None,
            "note": None}


def _section_json(joined: dict) -> str:
    return C.canonical_json(joined["ocelot"])


def _recomputed_output_hash(section: dict) -> str:
    return C.content_hash({
        "schema": {"namespace": f"{OP.SCHEMA_NAMESPACE}.output",
                   "version": OP.SCHEMA_VERSION},
        "input_hash": section["inputs"]["input_hash"],
        "candidate_clusters": section["candidate_clusters"],
        "analogies": section["analogies"],
        "exact_groups": section["exact_groups"],
        "coverage": section["coverage"],
        "score": section["score"],
    })


def test_candidate_clusters_have_provenance_and_input_hashes(tmp_path):
    report = _fixture(tmp_path)["report"]
    engine = FakeEngine()
    joined = RP.attach_ocelot(
        report, engine=engine, expected_engine_id=OP.ENGINE_ID,
        expected_version="1.4.0")
    section = joined["ocelot"]

    assert section["schema"] == {"namespace": OP.SCHEMA_NAMESPACE,
                                 "version": "1.0"}
    assert section["state"] == "ready"
    assert section["available"] is True
    assert section["reason_code"] is None
    assert section["mode"] == "injected"
    assert section["actual_mode"] == "injected"
    assert section["engine"]["id"] == OP.ENGINE_ID
    assert section["engine"]["version"] == "1.4.0"
    assert section["engine"]["version_state"] == "resolved"
    assert section["engine"]["constraint"]["satisfied"] is True
    assert section["verifier"]["state"] == "not_verified"
    assert section["verifier"]["truth_assessed"] is False
    assert section["truth_assessed"] is False
    assert section["support_edges_created"] is False
    assert section["operation"]["cross_class_links"] is False

    inputs = section["inputs"]
    assert inputs["hash_state"] == "match"
    assert inputs["projection_hash"] == report["hash"]
    assert inputs["observed_projection_hash"] == report["hash"]
    for field in ("claim_set_hash", "payload_hash", "input_hash"):
        assert len(inputs[field]) == 64
        assert inputs[field] == inputs[field].lower()
    assert inputs["selected_keys"] == sorted(inputs["selected_keys"])
    assert len(inputs["selected_keys"]) == 8
    assert len(inputs["clustered_keys"]) == 6
    assert inputs["expected_input_hash_state"] == "not_checked"

    coverage = section["coverage"]
    assert coverage["claims_total"] == 8
    assert coverage["claims_selected"] == 8
    assert coverage["claims_anchored"] == 6
    assert coverage["claims_without_sources"] == 2
    assert coverage["anchors"] == 3
    assert coverage["candidate_clusters"] == 3
    assert coverage["exact_groups"] == 3
    assert coverage["analogies"] == 3
    assert coverage["source_identity_kinds"] == {"recorded_sha256": 6}

    assert len(section["candidate_clusters"]) == 3
    assert {cluster["anchor"]["epistemic_class"]
            for cluster in section["candidate_clusters"]} == {
                "empirical", "formal", "conditional"}
    assert {cluster["size"] for cluster in section["candidate_clusters"]} == {2}
    assert {cluster["origin"] for cluster in section["candidate_clusters"]} == {
        "ocelot"}
    assert len(section["exact_groups"]) == 3
    assert {group["origin"] for group in section["exact_groups"]} == {"exact"}
    assert len(section["analogies"]) == 3

    assert len(section["output"]["hash"]) == 64
    assert section["output"]["hash"] == _recomputed_output_hash(section)
    assert section["output"]["state"] == "produced"
    assert section["output"]["candidate_cluster_count"] == 3
    assert section["output"]["analogy_count"] == 3
    assert section["score"]["state"] == "unavailable"
    assert section["unknown"]
    assert section["deferred"]
    assert "claim research-session-a/claim-0004 omitted: no cited source anchor" \
        in section["omissions"]
    assert RP.projection_hash(joined) == joined["hash"]
    assert joined["hash"] != report["hash"]
    assert joined["claims"] == report["claims"]
    assert joined["sources"] == report["sources"]
    assert joined["evidence"] == report["evidence"]
    assert joined["engines"]["ocelot"] == report["engines"]["ocelot"]
    assert section["output"]["rejected_engine_claims"] == []


def test_epistemic_classes_and_evidence_kinds_stay_separate(tmp_path):
    report = _fixture(tmp_path)["report"]
    joined = OP.attach_ocelot(report, engine=FakeEngine())
    section = joined["ocelot"]

    assert section["epistemic_boundary"]["cross_class_links"] is False
    assert section["epistemic_boundary"]["evidence_kinds_merged"] is False
    assert section["epistemic_boundary"]["empirical"]
    assert section["epistemic_boundary"]["conditional"]
    assert section["epistemic_boundary"]["formal"]

    for cluster in section["candidate_clusters"]:
        classes = {claim["epistemic_class"] for claim in cluster["claims"]}
        assert len(classes) == 1
        assert classes == {cluster["anchor"]["epistemic_class"]}
    for analogy in section["analogies"]:
        assert analogy["epistemic_class"] in {
            "empirical", "conditional", "formal"}
        assert analogy["truth_assessed"] is False
        assert analogy["support_edge_created"] is False

    base_support = {
        (claim["session"], claim["id"]): claim["support_state"]
        for claim in report["claims"]}
    joined_support = {
        (claim["session"], claim["id"]): claim["support_state"]
        for claim in joined["claims"]}
    assert joined_support == base_support
    assert base_support[("research-session-a", "claim-0001")] == \
        "declared_links_only"
    assert base_support[("research-session-a", "claim-0002")] == \
        "declared_links_only"
    assert base_support[("research-session-a", "claim-0004")] == "unlinked"
    assert {claim["evidence"] for claim in joined["claims"]} == {"declared"}

    assert any("cross-anchor cluster(s) split" in omission
               for omission in section["omissions"])
    assert any("cross anchor row(s) ignored" in omission
               for omission in section["omissions"])


def test_absent_ocelot_degrades_and_base_projection_is_unchanged(
        tmp_path, monkeypatch):
    report = _fixture(tmp_path)["report"]
    before = RP.projection_json(report)
    assert "ocelot" not in report
    assert report["engines"]["ocelot"]["state"] == "unknown"
    assert "deferred" in report["engines"]["ocelot"]["reason"].lower()

    monkeypatch.setattr(OC.ocelot_installer, "resolve", _not_installed)
    joined = OP.attach_ocelot(report)

    assert RP.projection_json(report) == before
    section = joined["ocelot"]
    assert section["state"] == "not_installed"
    assert section["reason_code"] == "not_installed"
    assert section["available"] is False
    assert section["engine"]["id"] is None
    assert section["engine"]["version_state"] == "not_applicable"
    assert section["candidate_clusters"] == []
    assert section["analogies"] == []
    assert len(section["exact_groups"]) == 3
    assert section["output"]["state"] == "fallback_only"
    assert len(section["output"]["hash"]) == 64
    assert section["output"]["hash"] == _recomputed_output_hash(section)
    assert section["verifier"]["state"] == "not_verified"
    assert section["truth_assessed"] is False
    assert "no engine ran" in section["reason"].lower()
    assert joined["claims"] == report["claims"]
    assert joined["engines"]["ocelot"] == report["engines"]["ocelot"]
    assert RP.projection_hash(joined) == joined["hash"]


def test_explicit_missing_checkout_is_unavailable_without_a_path(
        tmp_path):
    report = _fixture(tmp_path)["report"]
    missing = tmp_path / "missing-ocelot-checkout"
    joined = OP.attach_ocelot(report, ocelot_root=missing)
    section = joined["ocelot"]

    assert section["state"] == "unavailable"
    assert section["reason_code"] == "engine_failed"
    assert section["available"] is False
    assert section["candidate_clusters"] == []
    assert str(missing) not in _section_json(joined)
    assert str(missing) not in section["reason"]
    for omission in section["omissions"]:
        assert str(missing) not in omission


def test_projection_hash_tamper_is_invalid_and_engine_is_not_called(
        tmp_path):
    report = _fixture(tmp_path)["report"]
    tampered = json.loads(RP.projection_json(report))
    tampered["claims"][0]["status"] = "supported_within_scope"
    engine = FakeEngine()
    joined = OP.attach_ocelot(tampered, engine=engine)
    section = joined["ocelot"]

    assert engine.called is False
    assert section["state"] == "invalid"
    assert section["reason_code"] == "projection_hash_mismatch"
    assert section["inputs"]["hash_state"] == "mismatch"
    assert section["output"]["state"] == "not_produced"
    assert section["output"]["hash"] is None
    assert section["candidate_clusters"] == []
    assert joined["hash"] == tampered["hash"]


def test_expected_input_hash_mismatch_is_invalid(tmp_path):
    report = _fixture(tmp_path)["report"]
    engine = FakeEngine()
    joined = OP.attach_ocelot(
        report, engine=engine, expected_input_hash="0" * 64)
    section = joined["ocelot"]

    assert engine.called is False
    assert section["state"] == "invalid"
    assert section["reason_code"] == "input_hash_mismatch"
    assert section["inputs"]["expected_input_hash_state"] == "mismatch"
    assert section["output"]["state"] == "not_produced"
    assert section["candidate_clusters"] == []


def test_engine_input_hash_mismatch_is_invalid(tmp_path):
    report = _fixture(tmp_path)["report"]
    joined = OP.attach_ocelot(report, engine=FakeEngine(wrong_hash=True))
    section = joined["ocelot"]

    assert section["state"] == "invalid"
    assert section["reason_code"] == "engine_input_hash_mismatch"
    assert section["inputs"]["input_hash"] is not None
    assert section["candidate_clusters"] == []
    assert section["output"]["state"] == "not_produced"
    assert joined["hash"] == report["hash"]


@pytest.mark.parametrize("field", ["version", "engine_id"])
def test_engine_version_or_id_mismatch_is_invalid(tmp_path, field):
    report = _fixture(tmp_path)["report"]
    if field == "version":
        engine = FakeEngine(version="9.9.9")
        joined = OP.attach_ocelot(
            report, engine=engine, expected_version="1.4.0")
        assert joined["ocelot"]["reason_code"] == "version_mismatch"
        assert joined["ocelot"]["engine"]["version"] == "9.9.9"
    else:
        engine = FakeEngine()
        joined = OP.attach_ocelot(
            report, engine=engine, expected_engine_id="ocelot:other")
        assert joined["ocelot"]["reason_code"] == "engine_mismatch"
        assert joined["ocelot"]["engine"]["id"] == OP.ENGINE_ID
    section = joined["ocelot"]
    assert section["state"] == "invalid"
    assert section["available"] is False
    assert section["candidate_clusters"] == []
    assert section["exact_groups"] == []
    assert section["output"]["state"] == "not_produced"
    assert section["engine"]["constraint"]["satisfied"] is False


def test_expected_input_hash_match_is_accepted(tmp_path):
    report = _fixture(tmp_path)["report"]
    probe = OP.attach_ocelot(report, engine=FakeEngine())
    expected = probe["ocelot"]["inputs"]["input_hash"]
    joined = OP.attach_ocelot(
        report, engine=FakeEngine(), expected_input_hash=expected)
    section = joined["ocelot"]
    assert section["state"] == "ready"
    assert section["inputs"]["expected_input_hash"] == expected
    assert section["inputs"]["expected_input_hash_state"] == "match"
    assert section["inputs"]["input_hash"] == expected


def test_no_source_claims_report_no_claims_without_calling_engine(tmp_path):
    graph = ResearchGraph.create(
        "No source?", root=tmp_path, mode="exploratory",
        investigation_id="research-no-source")
    graph.record("Claim", wording="a claim without a source",
                 claim_type="hypothesis", status="open")
    report = RP.build_projection(tmp_path, observed_at=1)
    engine = FakeEngine()
    joined = OP.attach_ocelot(report, engine=engine)
    section = joined["ocelot"]

    assert engine.called is False
    assert section["state"] == "no_claims"
    assert section["reason_code"] == "no_claims"
    assert section["available"] is False
    assert section["exact_groups"] == []
    assert section["candidate_clusters"] == []
    assert section["output"]["state"] == "no_output"
    assert section["output"]["hash"] is None
    assert joined["claims"] == report["claims"]


def test_engine_truth_claims_are_rejected_not_promoted(tmp_path):
    report = _fixture(tmp_path)["report"]
    engine = FakeEngine(truth=True)
    joined = OP.attach_ocelot(report, engine=engine)
    section = joined["ocelot"]

    assert section["state"] == "ready"
    assert section["truth_assessed"] is False
    assert section["support_edges_created"] is False
    assert section["verifier"]["state"] == "not_verified"
    assert section["verifier"]["method"] is None
    assert section["output"]["engine_reported_truth_assessed"] is True
    assert set(section["output"]["rejected_engine_claims"]) == {
        "truth_assessed", "verified", "verification", "support_edges_created",
        "support_edges"}
    assert joined["claims"] == report["claims"]
    assert joined["evidence"] == report["evidence"]
    assert joined["engines"]["ocelot"] == report["engines"]["ocelot"]
    assert all(claim["truth_assessed"] is False for claim in joined["claims"])


def test_secret_canary_and_paths_are_not_in_payload_or_output(tmp_path):
    report = _fixture(tmp_path, canary=True)["report"]
    assert CANARY not in RP.projection_json(report)
    engine = FakeEngine(error=f"boom {CANARY} /srv/secret/ocelot-checkout")
    joined = OP.attach_ocelot(report, engine=engine)
    section = joined["ocelot"]

    assert section["state"] == "unavailable"
    assert section["reason_code"] == "engine_failed"
    assert engine.payload is not None
    assert CANARY not in C.canonical_json(engine.payload)
    assert CANARY not in _section_json(joined)
    assert "/srv/secret/ocelot-checkout" not in _section_json(joined)
    assert "/srv/secret" not in _section_json(joined)
    assert "the cache reduces latency" not in C.canonical_json(engine.payload)

    secret_version = OP.attach_ocelot(
        report, engine=FakeEngine(version=CANARY))
    assert CANARY not in _section_json(secret_version)
    assert secret_version["ocelot"]["engine"]["version"] is None
    assert secret_version["ocelot"]["engine"]["version_state"] == "unknown"


def test_default_worker_path_with_fake_worker(tmp_path, monkeypatch):
    report = _fixture(tmp_path)["report"]
    captured: dict = {}

    def worker(payload, *, ocelot_root, mode, timeout):
        captured["payload"] = payload
        captured["root"] = str(ocelot_root)
        clusters = _clusters(payload)
        analogies = [
            {"source_finding": ids[0], "target_finding": ids[1], "order_n": 4}
            for ids in clusters
        ]
        return ({"clusters": clusters, "analogies": analogies,
                 "engine": OP.ENGINE_ID}, "inprocess")

    monkeypatch.setattr(OC.ocelot_installer, "resolve", _ready)
    monkeypatch.setattr(OC, "_worker_output", worker)
    joined = OP.attach_ocelot(report, expected_version="1.4.0")
    section = joined["ocelot"]

    assert section["state"] == "ready"
    assert section["available"] is True
    assert section["actual_mode"] == "inprocess"
    assert section["engine"]["id"] == OP.ENGINE_ID
    assert section["engine"]["version"] == "1.4.0"
    assert section["engine"]["source"] == "sibling"
    assert len(section["candidate_clusters"]) == 3
    assert "/opt/ocelot-checkout" not in _section_json(joined)
    assert captured["root"] == "/opt/ocelot-checkout"
    for row in captured["payload"]["findings"]:
        assert row["target_type"] == "ClaimAnchor"
        assert row["family"] == "other"
        assert row["target_id"]
        assert "cache reduces" not in row["wording"]
        assert "bound holds" not in row["wording"]


def test_default_engine_exception_is_unavailable_without_a_leak(
        tmp_path, monkeypatch):
    report = _fixture(tmp_path)["report"]

    def boom(*args, **kwargs):
        raise RuntimeError(f"{CANARY} /srv/secret/ocelot-checkout")

    monkeypatch.setattr(OC, "cluster_findings", boom)
    joined = OP.attach_ocelot(report)
    section = joined["ocelot"]
    assert section["state"] == "unavailable"
    assert section["reason_code"] == "engine_failed"
    assert CANARY not in _section_json(joined)
    assert "/srv/secret/ocelot-checkout" not in _section_json(joined)
    assert joined["claims"] == report["claims"]


def test_attach_is_deterministic(tmp_path):
    report = _fixture(tmp_path)["report"]
    first = OP.attach_ocelot(report, engine=FakeEngine())
    second = OP.attach_ocelot(report, engine=FakeEngine())
    assert RP.projection_json(first) == RP.projection_json(second)
    assert first["ocelot"]["output"]["hash"] == \
        second["ocelot"]["output"]["hash"]
    assert first["hash"] == second["hash"]


@pytest.mark.parametrize("kwargs, code", [
    ({"mode": "bogus"}, "unsupported_mode"),
    ({"engine": FakeEngine(), "timeout": 0}, "invalid_timeout"),
    ({"engine": FakeEngine(), "max_claims": 0}, "invalid_limit"),
])
def test_invalid_requests_are_states_not_exceptions(tmp_path, kwargs, code):
    report = _fixture(tmp_path)["report"]
    joined = OP.attach_ocelot(report, **kwargs)
    section = joined["ocelot"]
    assert section["state"] == "invalid"
    assert section["reason_code"] == code
    assert section["output"]["state"] == "not_produced"


def test_base_projection_without_attach_is_untouched(tmp_path):
    report = _fixture(tmp_path)["report"]
    before = RP.projection_json(report)
    assert "ocelot" not in report
    assert report["engines"]["ocelot"]["state"] == "unknown"
    assert report["truth_assessed"] is False
    joined = OP.attach_ocelot(report, engine=FakeEngine())
    assert RP.projection_json(report) == before
    assert joined["ocelot"]["inputs"]["projection_hash"] == report["hash"]
    assert joined["schema"] == report["schema"]
    assert joined["projection_version"] == report["projection_version"]
    assert joined["counts"] == report["counts"]
