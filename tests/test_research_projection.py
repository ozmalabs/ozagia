"""Tests for the read-only research projection and its preflight join."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from ozagia.config import state_path
from ozagia.core import contracts as C
from ozagia.core import research_projection as RP
from ozagia.core import task_preflight as TP
from ozagia.research import holdings
from ozagia.research.graph import ResearchGraph

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
CANARY = "sk-CANARY-0123456789abcdef"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fixture(root: Path) -> dict:
    """A source-backed claim, an assumption, evidence and a holding."""
    sources = root / "papers"
    paper = sources / "paper.md"
    _write(paper, "# Paper\n\nThe cache reduces latency.\n")
    digest = hashlib.sha256(paper.read_bytes()).hexdigest()
    graph = ResearchGraph.create(
        "Does the cache reduce latency?",
        root=root,
        mode="empirical",
        roots=[{"root_id": "papers", "path": str(sources),
                "files": ["paper.md"]}],
    )
    source = graph.record(
        "Source", name="paper", source_type="article",
        locator={"root_id": "papers", "path": "paper.md", "sha256": digest,
                 "kind": "paper"})
    holding = holdings.record_holding(
        graph, source_id=source["id"], source_type="article",
        epistemic_role="evidence", authority="peer_reviewed",
        review_status="peer_reviewed", status="accepted", reviewer="reviewer",
        scope="latency claims", rationale="methods section reviewed")
    assumption = graph.record(
        "Assumption", wording="the benchmark workload is representative",
        status="open")
    evidence = graph.record(
        "Evidence", evidence_type="extracted_result",
        wording="Table 1 reports a lower median latency", status="recorded")
    claim = graph.record(
        "Claim", wording="the cache reduces median latency",
        claim_type="empirical_observation", status="open")
    graph.link(claim["id"], "cites", source["id"],
               provenance={"kind": "source_import"})
    graph.link(claim["id"], "assumes", assumption["id"])
    graph.link(evidence["id"], "supports", claim["id"],
               provenance={"kind": "extracted_table"})
    graph.link(evidence["id"], "cites", source["id"])
    return {
        "session": graph.investigation_id,
        "graph": graph,
        "claim": claim,
        "source": source,
        "assumption": assumption,
        "evidence": evidence,
        "holding": holding["holding"],
        "paper": paper,
        "digest": digest,
    }


def _fake_providers() -> TP.Providers:
    def provider(name: str):
        def run(ctx):
            return TP.Observation(
                True,
                (TP.fact(f"preflight.{name}", {"ok": True}, source="tests"),),
                {})
        return run
    return TP.Providers(
        goal=provider("goal"), graph=provider("graph"),
        capabilities=provider("capabilities"),
        environment=provider("environment"),
        resources=provider("resources"), tests=provider("tests"))


def _build(root: Path, **fields):
    return TP.build_preflight(
        root, "g1", providers=_fake_providers(), timeout_ms=10_000,
        clock=lambda: 0, observed_at=1_700_000_000_000, **fields)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def test_projection_reports_claim_source_assumption_and_evidence(tmp_path):
    fixture = _fixture(tmp_path)
    report = RP.build_projection(tmp_path, observed_at=1_700_000_000_000)

    assert report["schema"] == {"namespace": "ozagia.research_projection",
                               "version": "1.0"}
    assert report["mode"] == "read_only"
    assert report["truth_assessed"] is False
    assert report["counts"]["claims"] == 1
    assert report["counts"]["sources"] == 1
    assert report["counts"]["assumptions"] == 1
    assert report["counts"]["evidence"] == 1

    claim = report["claims"][0]
    assert claim["id"] == fixture["claim"]["id"]
    assert claim["evidence"] == "declared"
    assert claim["truth_assessed"] is False
    assert claim["source_ids"] == [fixture["source"]["id"]]
    assert claim["assumption_ids"] == [fixture["assumption"]["id"]]
    assert claim["support_state"] == "declared_links_only"
    assert claim["wording_sha256"] == hashlib.sha256(
        b"the cache reduces median latency").hexdigest()
    assert "wording" not in claim

    source = report["sources"][0]
    assert source["hash_state"] == "match"
    assert source["freshness"] == "current"
    assert source["evidence"] == "observed"
    assert source["recorded_sha256"] == fixture["digest"]
    assert source["observed_sha256"] == fixture["digest"]
    assert source["holdings"][0]["epistemic_role"] == "evidence"
    assert source["holdings"][0]["authority"] == "peer_reviewed"
    assert source["holdings"][0]["status"] == "accepted"
    assert source["holdings"][0]["truth_assessed"] is False

    assert report["assumptions"][0]["evidence"] == "declared"
    assert report["assumptions"][0]["claim_ids"] == [fixture["claim"]["id"]]
    assert report["evidence"][0]["evidence"] == "declared"
    assert report["evidence"][0]["source_ids"] == [fixture["source"]["id"]]
    assert report["engines"]["ocelot"]["state"] == "unknown"
    assert "deferred" in report["engines"]["ocelot"]["reason"] or \
        "deferred" in report["engines"]["ocelot"]["reason"].lower()

    readiness, reason = RP.projection_readiness(report)
    assert readiness == "unknown"
    assert "declaration" in reason
    assert report["hash"] == C.content_hash(
        {key: value for key, value in report.items() if key != "hash"})


def test_source_mismatch_is_stale_and_remote_is_unknown(tmp_path):
    fixture = _fixture(tmp_path)
    _write(fixture["paper"], "# Paper\n\nChanged.\n")
    graph = fixture["graph"]
    remote = graph.record(
        "Source", name="remote", source_type="website",
        locator={"url": "https://example.invalid/paper?token=" + CANARY})
    report = RP.build_projection(tmp_path, observed_at=1)
    by_id = {row["id"]: row for row in report["sources"]}
    assert by_id[fixture["source"]["id"]]["hash_state"] == "mismatch"
    assert by_id[fixture["source"]["id"]]["freshness"] == "stale"
    assert by_id[remote["id"]]["hash_state"] == "not_recorded"
    assert by_id[remote["id"]]["freshness"] == "unknown"
    claim = report["claims"][0]
    assert claim["evidence"] == "declared"
    assert claim["freshness"] == "stale"


def test_absent_and_invalid_sessions_stay_explicit(tmp_path):
    absent = RP.build_projection(tmp_path, sessions=["missing-session"],
                                 observed_at=1)
    assert absent["sessions"][0]["state"] == "absent"
    assert absent["sessions"][0]["freshness"] == "absent"
    assert RP.projection_readiness(absent)[0] == "unknown"

    bad = state_path(tmp_path, "research", "bad-session.json")
    _write(bad, "{not json")
    invalid = RP.build_projection(tmp_path, sessions=["bad-session"],
                                  observed_at=1)
    assert invalid["sessions"][0]["state"] == "invalid"
    assert invalid["sessions"][0]["freshness"] == "unknown"
    assert RP.projection_readiness(invalid)[0] == "unknown"


def test_secret_canary_is_never_projected(tmp_path):
    fixture = _fixture(tmp_path)
    graph = fixture["graph"]
    graph.amend(fixture["claim"]["id"], wording=f"claim {CANARY}")
    graph.amend(fixture["assumption"]["id"], wording=f"assume {CANARY}")
    graph.amend(fixture["evidence"]["id"], wording=f"evidence {CANARY}")
    holdings.record_holding(
        graph, source_id=fixture["source"]["id"], source_type="article",
        epistemic_role="background", status="proposed",
        rationale=f"private note {CANARY}")
    report = RP.build_projection(tmp_path, observed_at=1)
    text = RP.projection_json(report)
    assert CANARY not in text
    assert report["claims"][0]["wording_sha256"]
    assert report["assumptions"][0]["wording_sha256"]
    assert report["evidence"][0]["wording_sha256"]


def test_projection_is_deterministic_and_writes_nothing(tmp_path, monkeypatch):
    _fixture(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size,
                                                path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*") if path.is_file()
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("research projection attempted live I/O")

    monkeypatch.setattr("ozagia.research.fetch.http_get", forbidden)
    monkeypatch.setattr("ozagia.core.reference_sync.sync_reference", forbidden)

    first = RP.build_projection(tmp_path, observed_at=1)
    second = RP.build_projection(tmp_path, observed_at=1)
    assert first == second
    assert RP.projection_json(first) == RP.projection_json(second)
    after = {
        path.relative_to(tmp_path).as_posix(): (path.stat().st_size,
                                                path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*") if path.is_file()
    }
    assert after == before


def test_projection_does_not_import_ocelot(tmp_path):
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from ozagia.core import research_projection as RP\n"
        f"report = RP.build_projection(Path({str(tmp_path)!r}), sessions=[],"
        " observed_at=1)\n"
        "assert 'ozagia.research.ocelot_reconcile' not in sys.modules\n"
        "print(report['engines']['ocelot']['state'])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "unknown"


def test_projection_facts_are_bounded_and_declared(tmp_path):
    fixture = _fixture(tmp_path)
    graph = fixture["graph"]
    for index in range(60):
        graph.record("Claim", wording=f"extra claim {index}",
                     claim_type="inference", status="open")
    report = RP.build_projection(tmp_path, observed_at=1)
    assert report["counts"]["totals"]["claims"] == 61
    assert any("claim" in item for item in report["omissions"])
    facts = RP.projection_facts(report)
    assert 0 < len(facts) <= RP.MAX_FACTS
    claim_facts = [item for item in facts if ".claim." in item.subject]
    assert claim_facts
    assert all(item.evidence == "declared" for item in claim_facts)


# ---------------------------------------------------------------------------
# Preflight join
# ---------------------------------------------------------------------------


def test_preflight_research_join_is_declared_not_observed(tmp_path):
    fixture = _fixture(tmp_path)
    report = _build(tmp_path, research_sessions=[fixture["session"]])
    assert report["research"]["readiness"] == "unknown"
    assert report["dimensions"]["research"]["readiness"] == "unknown"
    assert report["assessment"]["status"] == "unknown"
    assert report["assessment"]["green"] is False
    assert report["research"]["claims"][0]["evidence"] == "declared"
    assert report["research"]["engines"]["ocelot"]["state"] == "unknown"
    facts = report["checkouts"][0]["dimensions"]["research"]["facts"]
    claim_facts = [item for item in facts
                   if ".claim." in item["subject"]]
    assert claim_facts
    assert all(item["evidence"] == "declared" for item in claim_facts)
    assert all(item["value"]["truth_assessed"] is False
               for item in claim_facts)


def test_research_dimension_is_absent_without_selection(tmp_path):
    report = _build(tmp_path)
    assert "research" not in report
    assert "linked" not in report
    assert list(report["dimensions"]) == list(TP.DIMENSIONS)
    assert report["assessment"]["status"] == "green"


def test_existing_providers_constructor_remains_compatible(tmp_path):
    providers = TP.Providers(
        graph=lambda ctx: TP.Observation(True, (TP.fact(
            "preflight.graph", {}, source="tests"),)),
        goal=lambda ctx: TP.Observation(True, (TP.fact(
            "preflight.goal", {}, source="tests"),), {
                "id": "goal:g1", "local_id": "g1", "status": "open",
                "scope": [], "criteria": [], "counts": {}, "evaluated": True}),
        capabilities=lambda ctx: TP.Observation(True, (TP.fact(
            "preflight.capabilities", {}, source="tests"),)),
        environment=lambda ctx: TP.Observation(True, (TP.fact(
            "preflight.environment", {}, source="tests"),)),
        resources=lambda ctx: TP.Observation(True, (TP.fact(
            "preflight.resources", {}, source="tests"),)),
        tests=lambda ctx: TP.Observation(True, (TP.fact(
            "preflight.tests", {}, source="tests"),)),
    )
    report = TP.build_preflight(tmp_path, "g1", providers=providers,
                                timeout_ms=1000, clock=lambda: 0, observed_at=1)
    assert report["assessment"]["status"] == "green"
    assert report["schema"]["version"] == "1.0"


def test_missing_selected_source_is_absent_not_current(tmp_path):
    fixture = _fixture(tmp_path)
    fixture["paper"].unlink()
    report = RP.build_projection(tmp_path, observed_at=1)
    source = report["sources"][0]
    assert source["hash_state"] == "absent"
    assert source["freshness"] == "absent"
    assert source["evidence"] == "observed"
    assert report["claims"][0]["evidence"] == "declared"
    assert report["claims"][0]["freshness"] == "absent"
