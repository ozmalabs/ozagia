"""Tests for the read-only task preflight and its assessment rules."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.config import state_path
from ozagia.core import capabilities as K
from ozagia.core import contracts as C
from ozagia.core import goals as G
from ozagia.core import services as S
from ozagia.core import task_preflight as TP
from ozagia.core import working_set as W
from ozagia.core.fingerprint import tree_fingerprint
from ozagia.core.memory import build_index

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t",
       "-c", "commit.gpgsign=false"]
TEST_KEY = "test:tests/test_core.py::test_run"
CORE = "def run(x):\n    return x + 1\n"
TESTS = ("from pkg.core import run\n\n\ndef test_run():\n"
         "    assert run(1) == 3\n")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run([*GIT, "-C", str(root), *args], check=True,
                   capture_output=True)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "core.py", CORE)
    _write(root / "tests" / "test_core.py", TESTS)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root.resolve()


def _write_goal(root: Path, *, status: str = "open",
                last_status: str | None = "fail",
                criteria: list[dict] | None = None) -> Path:
    if criteria is None:
        criteria = [{
            "id": "c1",
            "kind": "test_passes",
            "args": {"key": TEST_KEY},
            "last": ({"status": last_status, "evidence": {}, "reason": "",
                      "at": 1_700_000_000.0} if last_status else {}),
        }]
    goal = {
        "id": "g1", "title": "fix the failing test", "why": "",
        "status": status, "targets": [], "scope": [],
        "criteria": criteria, "claimed_by": [], "history": [], "source": None,
        "created_at": 1, "updated_at": 1, "checked_at": 1,
    }
    path = G.goals_path(root)
    _write(path, json.dumps({"version": 1, "next_id": 2, "goals": [goal]}))
    return path


def _write_test_record(root: Path, *, failed: bool = True) -> Path:
    tree = tree_fingerprint(root)
    path = state_path(root, *W.TEST_RECORD)
    _write(path, json.dumps({
        "tests": {}, "last": None,
        "runs": [{
            "ts": 1_700_000_000.0,
            "selectors": ["tests/test_core.py"],
            "failed": [TEST_KEY] if failed else [],
            "errors": [], "complete": True, "filtered": False, "stops": False,
            "source": "captured", "tree": tree,
        }],
    }))
    return path


# ---------------------------------------------------------------------------
# Fake providers
# ---------------------------------------------------------------------------


def fact(value: object | None = None, *, freshness: str = "current"):
    return TP.fact("preflight.fake", value or {"ok": True}, source="tests",
                   freshness=freshness)


def observation(readiness: bool | str = True, *, freshness: str = "current",
                data: dict | None = None, reason: str | None = None):
    return TP.Observation(readiness, (fact(freshness=freshness),),
                          data or {}, reason=reason)


CRITERION = {
    "id": "c1", "kind": "test_passes", "result": "passed",
    "recorded_status": "pass", "freshness": "current", "evidence": "observed",
    "source": "tests", "observed_at": 1, "reason": None, "test": None,
    "interpreter": None,
}


def goal_observation(*, readiness: bool | str = True,
                     result: str = "passed",
                     freshness: str = "current",
                     criteria: list[dict] | None = None):
    items = criteria if criteria is not None else [dict(CRITERION,
                                                       result=result)]
    data = {
        "id": "goal:g1", "local_id": "g1", "status": "open", "scope": [],
        "criteria": items,
        "counts": {result: len(items)}, "evaluated": True,
    }
    return TP.Observation(readiness, (fact(freshness=freshness),), data,
                          reason="fake goal")


def providers(**overrides) -> TP.Providers:
    defaults = {
        "goal": lambda ctx: goal_observation(),
        "graph": lambda ctx: observation(),
        "capabilities": lambda ctx: observation(),
        "environment": lambda ctx: observation(),
        "resources": lambda ctx: observation(),
        "tests": lambda ctx: observation(
            data={"executed": False, "plan_only": True}),
    }
    defaults.update(overrides)
    return TP.Providers(**defaults)


def context(root: Path, **fields) -> TP.ProviderContext:
    base = {
        "checkout": TP.Checkout.at(root),
        "deadline": S.Deadline.after(10_000, clock=lambda: 0),
        "goal_id": "goal:g1",
        "local_goal_id": "g1",
        "primary": True,
        "scope": (),
        "changed": None,
        "base": "HEAD",
        "capabilities": TP.DEFAULT_CAPABILITIES,
        "capability_trust": {},
        "observed_at": 1_700_000_000_000,
        "timeout_ms": 10_000,
    }
    base.update(fields)
    return TP.ProviderContext(**base)


def build(root: Path, *, fake: TP.Providers | None = None, **fields):
    return TP.build_preflight(
        root, "g1",
        providers=fake or providers(),
        timeout_ms=fields.pop("timeout_ms", 10_000),
        clock=fields.pop("clock", lambda: 0),
        observed_at=fields.pop("observed_at", 1_700_000_000_000),
        **fields,
    )


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


def test_every_current_dimension_is_green(tmp_path):
    report = build(tmp_path)
    assert report["assessment"]["status"] == "green"
    assert report["assessment"]["green"] is True
    assert all(report["dimensions"][name]["readiness"] is True
               for name in TP.DIMENSIONS)
    assert report["schema"] == {"namespace": "ozagia.task_preflight",
                               "version": "1.0"}
    assert report["deferred"]


def test_stale_fact_cannot_be_upgraded_to_green(tmp_path):
    fake = providers(graph=lambda ctx: observation(True, freshness="stale"))
    report = build(tmp_path, fake=fake)
    assert report["dimensions"]["graph"]["readiness"] == "unknown"
    assert report["assessment"]["status"] == "unknown"
    assert any(row["dimension"] == "graph"
               for row in report["assessment"]["reasons"])


def test_absent_fact_is_never_green(tmp_path):
    fake = providers(graph=lambda ctx: observation(True, freshness="absent"))
    report = build(tmp_path, fake=fake)
    assert report["dimensions"]["graph"]["readiness"] == "unknown"
    assert report["assessment"]["status"] != "green"


def test_a_false_dimension_makes_the_assessment_failed(tmp_path):
    fake = providers(environment=lambda ctx: observation(
        False, reason="no interpreter"))
    report = build(tmp_path, fake=fake)
    assert report["dimensions"]["environment"]["readiness"] is False
    assert report["assessment"]["status"] == "failed"
    assert report["assessment"]["green"] is False


def test_unknown_criteria_yield_unknown(tmp_path):
    fake = providers(goal=lambda ctx: goal_observation(
        readiness="unknown", result="unknown", freshness="unknown"))
    report = build(tmp_path, fake=fake)
    assert report["criteria"]["counts"].get("unknown") == 1
    assert report["assessment"]["status"] == "unknown"


def test_a_plan_is_not_a_passed_test(tmp_path):
    fake = providers(
        goal=lambda ctx: goal_observation(
            readiness="unknown", result="not_assessed"),
        tests=lambda ctx: observation(
            True, data={"executed": False, "plan_only": True,
                        "claim": "test selection only; no test was executed"}),
    )
    report = build(tmp_path, fake=fake)
    assert report["dimensions"]["tests"]["readiness"] is True
    assert report["dimensions"]["tests"]["data"]["executed"] is False
    assert report["criteria"]["counts"].get("passed") is None
    assert report["criteria"]["counts"].get("not_assessed") == 1
    assert report["assessment"]["status"] != "green"
    assert any("not an executed test" in item.lower()
               for item in report["assessment"]["notes"])


def test_multiple_checkouts_aggregate_the_worst_readiness(tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    fake = providers(
        environment=lambda ctx: observation(ctx.primary))
    report = build(tmp_path, fake=fake, checkouts=[second])
    assert len(report["checkouts"]) == 2
    assert report["dimensions"]["environment"]["readiness"] is False
    assert report["assessment"]["status"] == "failed"


def test_missing_capability_fails(tmp_path):
    fake = providers(capabilities=lambda ctx: observation(
        False, reason="a declared surface is absent"))
    report = build(tmp_path, fake=fake)
    assert report["dimensions"]["capabilities"]["readiness"] is False
    assert report["assessment"]["status"] == "failed"


# ---------------------------------------------------------------------------
# Default providers
# ---------------------------------------------------------------------------


def test_missing_graph_is_false(tmp_path):
    observed = TP.graph_observation(context(tmp_path))
    assert observed.readiness is False
    assert observed.facts[0].freshness == "absent"
    assert observed.data["refreshed"] is False


def test_stale_graph_is_unknown(project):
    dump = state_path(project, "graph.json")
    build_index(project, dump, include_docs="none", include_git=False,
                include_scripts=False)
    assert TP.read_graph_state(project)["state"] == "fresh"
    _write(project / "pkg" / "core.py", CORE + "\n# changed\n")
    state = TP.read_graph_state(project)
    assert state["state"] == "stale"
    observed = TP.graph_observation(context(project))
    assert observed.readiness == "unknown"
    assert observed.facts[0].freshness == "stale"
    assert "changed" in observed.reason


def test_missing_interpreter_is_false(project, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    observed = TP.environment_observation(
        context(project), which=lambda name: None, include_self=False,
        trust=False)
    assert observed.readiness is False
    assert "no interpreter" in observed.reason
    assert observed.data["probed"] is False


def test_untrusted_project_interpreter_is_unknown(tmp_path):
    observed = TP.environment_observation(
        context(tmp_path),
        discover=lambda root, **kw: [{
            "path": sys.executable, "source": "root_venv",
            "evidence": "root .venv", "project": True, "also": [],
        }],
        trust=False)
    assert observed.readiness == "unknown"
    assert "trust" in observed.reason


def _capability_report(state: str = "observed", *,
                       findings: list | None = None,
                       cap: str = "cap:test.scoped") -> dict:
    return {
        "surface_states": [{"capability": cap, "surface": "mcp:tool:dev_tests",
                            "state": state, "reason": None,
                            "provenance": [], "matches": []}],
        "audit": {"state": "current", "findings": findings or [],
                  "hash": "0" * 64},
    }


def test_capability_trust_and_audit(tmp_path):
    report = _capability_report()
    one = ("cap:test.scoped",)
    observed = TP.capability_observation(
        context(tmp_path, capabilities=one), report=report,
        catalogue=K.catalogue())
    assert observed.readiness is True
    untrusted = TP.capability_observation(
        context(tmp_path, capabilities=one,
                capability_trust={"cap:test.scoped": False}),
        report=report, catalogue=K.catalogue())
    assert untrusted.readiness is False
    unknown = TP.capability_observation(
        context(tmp_path, capabilities=one,
                capability_trust={"cap:test.scoped": "unknown"}),
        report=report, catalogue=K.catalogue())
    assert unknown.readiness == "unknown"
    missing = TP.capability_observation(
        context(tmp_path, capabilities=one),
        report=_capability_report(state="missing"),
        catalogue=K.catalogue())
    assert missing.readiness is False


def test_internal_capability_has_no_observable_surface(tmp_path):
    report = {"surface_states": [],
              "audit": {"state": "current", "findings": [], "hash": None}}
    observed = TP.capability_observation(
        context(tmp_path, capabilities=("cap:tasks.bounded",)),
        report=report, catalogue=K.catalogue())
    assert observed.readiness == "unknown"


def test_failing_test_traces_to_the_goal_assessment(project, monkeypatch):
    dump = state_path(project, "graph.json")
    build_index(project, dump, include_docs="none", include_git=False,
                include_scripts=False)
    _write_goal(project)
    _write_test_record(project, failed=True)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    fake = TP.Providers(
        goal=TP.goal_observation,
        graph=lambda ctx: observation(),
        capabilities=lambda ctx: observation(),
        environment=lambda ctx: TP.environment_observation(
            ctx, which=lambda name: None, include_self=False, trust=False),
        resources=lambda ctx: observation(),
        tests=lambda ctx: observation(
            data={"executed": False, "plan_only": True}),
    )
    report = TP.build_preflight(
        project, "g1", providers=fake, timeout_ms=10_000, clock=lambda: 0,
        observed_at=1_700_000_000_000)

    assert report["dimensions"]["goal"]["readiness"] is False
    assert report["dimensions"]["environment"]["readiness"] is False
    assert report["assessment"]["status"] == "failed"
    item = report["criteria"]["items"][0]
    assert item["result"] == "failed"
    assert item["test"]["outcome"] == "failed"
    assert "test failed" in item["reason"]
    assert item["interpreter"]["state"] == "not_recorded"
    goal_fact = report["checkouts"][0]["dimensions"]["goal"]["facts"][0]
    assert goal_fact["value"]["status"] == "open"


def test_stale_graph_blocks_a_green_goal_assessment(project):
    dump = state_path(project, "graph.json")
    build_index(project, dump, include_docs="none", include_git=False,
                include_scripts=False)
    _write_goal(project, last_status="pass")
    _write_test_record(project, failed=False)
    _write(project / "pkg" / "core.py", CORE + "\n# changed\n")
    observed = TP.goal_observation(context(project))
    assert observed.readiness == "unknown"
    assert any(item["result"] == "unknown"
               for item in observed.data["criteria"])


# ---------------------------------------------------------------------------
# Timeouts, bounds and determinism
# ---------------------------------------------------------------------------


def test_recorded_free_text_reason_is_redacted(project):
    canary = "sk-CANARY-0123456789abcdef"
    _write_goal(project, criteria=[{
        "id": "c1", "kind": "calls", "args": {},
        "last": {"status": "pass", "evidence": {},
                 "reason": f"the recorded reason says {canary}",
                 "at": 1_700_000_000.0},
    }])
    observed = TP.goal_observation(context(project))
    text = json.dumps(observed.data)
    assert canary not in text
    assert "[redacted]" in observed.data["criteria"][0]["reason"]


def test_scope_defaults_to_the_goal(project):
    _write_goal(project, criteria=[{
        "id": "c1", "kind": "file_exists", "args": {"key": "file:pkg/core.py"},
        "last": {"status": "pass", "evidence": {}, "reason": "",
                 "at": 1_700_000_000.0},
    }])
    goal = json.loads(G.goals_path(project).read_text())
    goal["goals"][0]["scope"] = ["docs/**", "pkg"]
    _write(G.goals_path(project), json.dumps(goal))
    fake = providers(goal=TP.goal_observation)
    report = build(project, fake=fake)
    assert report["scope"] == ["docs/**", "pkg"]
    assert report["goal"]["scope"] == ["docs/**", "pkg"]
    assert report["scope_source"] == "goal"


def test_timeout_stops_scheduling_and_marks_unknown(tmp_path):
    calls: list[str] = []
    ticks = iter(range(0, 10_000, 60))

    def clock():
        return next(ticks)

    def provider(name):
        def run(ctx):
            calls.append(name)
            return observation()
        return run

    fake = providers(**{name: provider(name) for name in TP.DIMENSIONS})
    report = TP.build_preflight(
        tmp_path, "g1", providers=fake, timeout_ms=100, clock=clock,
        observed_at=1)
    assert calls == ["goal"]
    assert report["dimensions"]["goal"]["readiness"] is True
    assert report["checkouts"][0]["dimensions"]["graph"]["error"] == "timeout"
    assert report["dimensions"]["tests"]["readiness"] == "unknown"
    assert report["assessment"]["status"] == "unknown"


def test_zero_timeout_calls_no_provider(tmp_path):
    calls: list[str] = []

    def provider(name):
        def run(ctx):
            calls.append(name)
            return observation()
        return run

    fake = providers(**{name: provider(name) for name in TP.DIMENSIONS})
    report = TP.build_preflight(
        tmp_path, "g1", providers=fake, timeout_ms=0, clock=lambda: 0,
        observed_at=1)
    assert calls == []
    assert all(report["dimensions"][name]["readiness"] == "unknown"
               for name in TP.DIMENSIONS)
    assert report["assessment"]["status"] == "unknown"


def test_criteria_are_paginated_and_facts_bounded(tmp_path):
    criteria = [dict(CRITERION, id=f"c{index}") for index in range(60)]
    fake = providers(goal=lambda ctx: goal_observation(criteria=criteria))
    report = build(tmp_path, fake=fake, limit=10, max_facts=2)
    assert report["criteria"]["total"] == 60
    assert len(report["criteria"]["items"]) == 10
    assert report["criteria"]["truncated"]["truncated"] is True
    assert report["criteria"]["truncated"]["omitted"] == 50
    assert report["criteria"]["truncated"]["next_cursor"] == "10"
    assert report["bounds"]["facts_total"] == len(TP.DIMENSIONS)
    assert report["bounds"]["facts_returned"] == 2
    assert report["bounds"]["facts_omitted"] == len(TP.DIMENSIONS) - 2


def test_fact_truncation_sets_the_envelope(tmp_path):
    report = build(tmp_path, max_facts=1)
    assert report["truncated"]["truncated"] is True
    assert report["truncated"]["reason"] == "limit"
    assert report["truncated"]["omitted"] == len(TP.DIMENSIONS) - 1
    assert report["bounds"]["facts_returned"] == 1


def test_report_is_canonical_and_hashed(tmp_path):
    report = build(tmp_path)
    text = TP.report_json(report)
    assert text == C.canonical_json(report)
    assert "\n" not in text
    assert TP.report_hash(report) == C.content_hash(
        {key: value for key, value in report.items() if key != "hash"})
    assert len(report["hash"]) == 64


def test_report_is_stable_across_hash_seeds():
    script = (
        "from ozagia.core import task_preflight as TP\n"
        "def obs(name):\n"
        "    return TP.Observation(True, (TP.fact('preflight.' + name,"
        " {'b': 2, 'a': 1}, source='tests'),))\n"
        "def goal(ctx):\n"
        "    return TP.Observation(True, (TP.fact('preflight.goal',"
        " {'status': 'open'}, source='tests'),),"
        " {'id': 'goal:g1', 'local_id': 'g1', 'status': 'open', 'scope': [],"
        " 'criteria': [], 'counts': {}, 'evaluated': True})\n"
        "providers = TP.Providers(goal=goal, graph=lambda c: obs('graph'),"
        " capabilities=lambda c: obs('capabilities'),"
        " environment=lambda c: obs('environment'),"
        " resources=lambda c: obs('resources'), tests=lambda c: obs('tests'))\n"
        "report = TP.build_preflight('.', 'g1', providers=providers,"
        " timeout_ms=1000, clock=lambda: 0, observed_at=1, scope=['b', 'a'])\n"
        "print(TP.report_json(report))\n"
    )
    outputs = []
    for seed in ("1", "2", "999"):
        env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONHASHSEED": seed}
        result = subprocess.run([sys.executable, "-c", script], cwd=REPO,
                                env=env, capture_output=True, text=True,
                                check=True)
        outputs.append(result.stdout)
    assert len(set(outputs)) == 1


def test_report_wraps_in_the_service_envelope(tmp_path):
    report = build(tmp_path)
    envelope = TP.preflight_envelope(report)
    assert envelope["service"] == "task_preflight"
    assert envelope["version"] == "1.0"
    assert envelope["status"] == "ok"
    assert envelope["side_effect"] == "read"
    assert envelope["data"] == report
    assert envelope["provenance"]["inputs"] == [report["hash"]]
    assert S.validate(envelope) == envelope
    assert len(S.envelope_hash(envelope)) == 64


def test_report_envelope_rejects_stale_embedded_hash(tmp_path):
    report = build(tmp_path)
    report["goal"]["status"] = "complete"
    with pytest.raises(TP.PreflightError, match="hash does not match") as caught:
        TP.preflight_envelope(report)
    assert caught.value.code == "invalid_value"


def test_report_envelope_derives_provenance_hash_when_missing(tmp_path):
    report = build(tmp_path)
    report.pop("hash")
    envelope = TP.preflight_envelope(report)
    assert envelope["provenance"]["inputs"] == [TP.report_hash(report)]


def test_typed_request_errors(tmp_path):
    with pytest.raises(TP.PreflightError) as caught:
        TP.build_preflight(tmp_path, "Not A Goal")
    assert caught.value.code == "invalid_request"
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(),
                           checkouts=[tmp_path])
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(),
                           capability_trust={"cap:graph.context": True})
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(),
                           timeout_ms=TP.MAX_TIMEOUT_MS + 1)
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(),
                           max_facts=TP.MAX_FACTS + 1)
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(), limit=0)
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(), base="")
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(), scope="x")
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(), changed="x")
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(),
                           required_capabilities="cap:test.scoped")


def test_unknown_goal_is_a_refusal(tmp_path):
    with pytest.raises(TP.PreflightError) as caught:
        TP.build_preflight(tmp_path, "g1", timeout_ms=1000, clock=lambda: 0,
                           observed_at=1)
    assert caught.value.code == "not_found"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(TP, "default_providers", lambda **kw: providers())
    assert cli.main(["task-preflight", "g1", "--root", str(tmp_path),
                     "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["assessment"]["status"] == "green"

    monkeypatch.setattr(TP, "default_providers", lambda **kw: providers(
        graph=lambda ctx: observation(True, freshness="stale")))
    assert cli.main(["task-preflight", "g1", "--root", str(tmp_path),
                     "--json"]) == 1

    monkeypatch.setattr(TP, "default_providers", lambda **kw: providers(
        environment=lambda ctx: observation(False)))
    assert cli.main(["task-preflight", "g1", "--root", str(tmp_path)]) == 1

    assert cli.main(["task-preflight", "Not A Goal", "--root", str(tmp_path),
                     "--json"]) == 2
    assert cli.main(["task-preflight", "g1", "--root", str(tmp_path),
                     "--limit", "0"]) == 2


def test_cli_missing_goal_is_two(tmp_path, capsys):
    code = cli.main(["task-preflight", "g1", "--root", str(tmp_path),
                     "--json"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["code"] == "not_found"


def test_cli_help_and_existing_preflight_are_unchanged(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main(["task-preflight", "--help"])
    assert caught.value.code == 0
    parser = cli.build_parser()
    parsed = parser.parse_args([
        "preflight", "plan", "--root", "/tmp", "--emulator-bin", "/bin/true",
    ])
    assert parsed.command == "preflight"
    assert parsed.action == "plan"


def test_cli_does_not_write_goal_or_task_stores(project, tmp_path,
                                                 monkeypatch):
    _write_goal(project)
    before = {
        path.relative_to(project).as_posix(): (path.stat().st_size,
                                               path.stat().st_mtime_ns)
        for path in project.rglob("*") if path.is_file()
    }
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    code = cli.main(["task-preflight", "g1", "--root", str(project),
                     "--timeout-s", "30"])
    after = {
        path.relative_to(project).as_posix(): (path.stat().st_size,
                                               path.stat().st_mtime_ns)
        for path in project.rglob("*") if path.is_file()
    }
    assert code in (1, 2)
    assert after == before
    assert G.goals_path(project).is_file()


# ---------------------------------------------------------------------------
# Linked checkouts (read-only M5/WP7 slice)
# ---------------------------------------------------------------------------


def _linked_member(base: Path, name: str) -> tuple[Path, Path]:
    root = base / name
    _write(root / "pkg" / "core.py", CORE)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    dump = state_path(root, "graph.json")
    build_index(root, dump, include_docs="none", include_git=False,
                include_scripts=False)
    return root.resolve(), dump


def test_linked_dimension_is_absent_without_an_explicit_selection(tmp_path):
    report = build(tmp_path)
    assert list(report["dimensions"]) == list(TP.DIMENSIONS)
    assert "linked" not in report
    assert report["bounds"]["dimensions"] == len(TP.DIMENSIONS)

    empty = build(tmp_path, linked_checkouts=[], research_sessions=[])
    assert empty["dimensions"]["linked"]["readiness"] == "unknown"
    assert empty["dimensions"]["research"]["readiness"] == "unknown"
    assert empty["assessment"]["status"] == "unknown"


def test_two_linked_checkouts_keep_unknown_stale_and_unauthorized_explicit(
        project, tmp_path):
    fresh, _ = _linked_member(tmp_path, "fresh")
    stale, _ = _linked_member(tmp_path, "stale")
    _write(stale / "pkg" / "core.py", CORE + "\n# changed\n")
    unauthorized, _ = _linked_member(tmp_path, "unauthorized")
    missing = (tmp_path / "missing").resolve()
    report = build(project, linked_checkouts=[
        {"root": fresh, "label": "fresh", "authorized": True,
         "read_scope": ["pkg/**"]},
        {"root": stale, "label": "stale", "authorized": "unknown",
         "read_scope": ["pkg/**"]},
        {"root": unauthorized, "label": "unauthorized", "authorized": False,
         "read_scope": ["pkg/**"]},
        {"root": missing, "label": "missing", "authorized": True,
         "read_scope": ["pkg/**"]},
    ])
    linked = {row["label"]: row for row in report["linked"]["members"]}
    assert linked["fresh"]["readiness"] is True
    assert linked["fresh"]["index"]["state"] == "fresh"
    assert linked["fresh"]["reference"]["state"] == "not_declared"

    assert linked["stale"]["readiness"] == "unknown"
    assert linked["stale"]["index"]["state"] == "stale"
    assert linked["stale"]["index"]["freshness"] == "stale"
    assert linked["stale"]["freshness"] == "unknown"
    assert "authorization" in linked["stale"]["reason"]

    assert linked["unauthorized"]["readiness"] is False
    assert linked["unauthorized"]["authorization"]["state"] is False
    assert linked["unauthorized"]["authorization"]["freshness"] == "absent"

    assert linked["missing"]["readiness"] == "unknown"
    assert linked["missing"]["index"]["state"] == "absent"
    assert linked["missing"]["exists"] is False

    assert report["linked"]["counts"] == {
        "members": 4, "ready": 1, "unknown": 2, "failed": 1,
        "stale": 1, "absent": 1, "unauthorized": 1,
    }
    assert report["dimensions"]["linked"]["readiness"] is False
    assert report["assessment"]["status"] == "failed"
    assert report["assessment"]["green"] is False
    assert any("linked" in item["dimension"]
               for item in report["assessment"]["reasons"])


def test_linked_missing_authorization_and_scope_stay_unknown(project, tmp_path):
    member, _ = _linked_member(tmp_path, "member")
    no_auth = TP.linked_observation(context(
        project, linked=(TP.LinkedCheckout.at(member, label="m"),)))
    assert no_auth.readiness == "unknown"
    assert "authorization" in no_auth.reason
    no_scope = TP.linked_observation(context(
        project, linked=(TP.LinkedCheckout.at(
            member, label="m", authorized=True),)))
    assert no_scope.readiness == "unknown"
    assert "scope" in no_scope.reason


def test_linked_identity_comes_from_the_caller_or_the_snapshot(project, tmp_path):
    member, _ = _linked_member(tmp_path, "member")
    repo = C.repo_id("acme", "service")
    checkout = C.checkout_id(repo, "a" * 12)
    snapshot = C.snapshot_id(checkout, "b" * 12)
    observed = TP.linked_observation(context(
        project,
        linked=(TP.LinkedCheckout.at(
            member, label="m", repo=repo, checkout=checkout,
            snapshot=snapshot, authorized=True, read_scope=["src/**"]),)))
    assert observed.readiness is True
    identity = observed.data["members"][0]["identity"]
    assert identity["repo"] == repo
    assert identity["checkout"] == checkout
    assert identity["snapshot"] == snapshot
    assert identity["source"] == "caller"
    assert identity["recorded"]["name"] == "member"


def test_linked_reference_sync_error_is_unknown_and_never_synced(
        project, tmp_path, monkeypatch):
    member, _ = _linked_member(tmp_path, "member")
    federation = state_path(project, "federation.json")
    _write(federation, json.dumps({
        "version": 1, "members": [str(member)],
        "reference_repos": [{"path": str(member)}], "links": [],
    }))

    def forbidden(*args, **kwargs):
        raise AssertionError("reference sync was called")

    monkeypatch.setattr("ozagia.core.federation.sync_references", forbidden)
    monkeypatch.setattr("ozagia.core.federation.request_reference_sync",
                        forbidden)
    monkeypatch.setattr("ozagia.core.reference_sync.sync_reference", forbidden)
    observed = TP.linked_observation(context(
        project, linked=(TP.LinkedCheckout.at(
            member, label="m", authorized=True, read_scope=["src/**"]),)))
    assert observed.readiness == "unknown"
    member_row = observed.data["members"][0]
    assert member_row["reference"]["role"] == "declared_reference"
    assert member_row["reference"]["state"] == "not_checked"
    assert member_row["reference"]["freshness"] == "unknown"
    assert observed.data["reference"]["synced"] is False
    assert "no Git fetch" in " ".join(observed.data["not_assessed"])


def test_linked_selection_never_writes_or_refreshes(project, tmp_path,
                                                    monkeypatch):
    member, dump = _linked_member(tmp_path, "member")
    before = {
        path.relative_to(member).as_posix(): (
            path.stat().st_size, path.stat().st_mtime_ns)
        for path in member.rglob("*") if path.is_file()
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("linked observation attempted a mutating call")

    monkeypatch.setattr("ozagia.core.federation.sync_references", forbidden)
    monkeypatch.setattr("ozagia.core.federation.request_reference_sync",
                        forbidden)
    monkeypatch.setattr("ozagia.core.reference_sync.sync_reference", forbidden)
    report = build(project, linked_checkouts=[{
        "root": member, "label": "m", "authorized": True,
        "read_scope": ["src/**"],
    }])
    assert report["linked"]["readiness"] is True
    after = {
        path.relative_to(member).as_posix(): (
            path.stat().st_size, path.stat().st_mtime_ns)
        for path in member.rglob("*") if path.is_file()
    }
    assert after == before
    assert dump.is_file()
    assert report["linked"]["reference"]["synced"] is False


def test_linked_facts_are_bounded_and_the_dimension_is_dynamic(tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    report = build(tmp_path, linked_checkouts=[
        {"root": first, "label": "a", "authorized": True,
         "read_scope": ["src/**"]},
        {"root": second, "label": "b", "authorized": True,
         "read_scope": ["src/**"]},
    ], max_facts=1)
    assert report["bounds"]["dimensions"] == len(TP.DIMENSIONS) + 1
    assert report["bounds"]["facts_total"] > 1
    assert report["bounds"]["facts_returned"] == 1
    assert report["bounds"]["facts_omitted"] > 0
    assert report["dimensions"]["linked"]["readiness"] == "unknown"


def test_linked_provider_can_be_injected(tmp_path):
    calls: list[tuple[str, ...]] = []

    def linked(ctx):
        calls.append(tuple(member.label for member in ctx.linked))
        return observation(False, reason="linked provider refused")

    fake = providers(linked=linked)
    report = build(tmp_path, fake=fake, linked_checkouts=[
        {"root": tmp_path, "label": "a", "authorized": True,
         "read_scope": ["src/**"]},
    ])
    assert calls == [("a",)]
    assert report["dimensions"]["linked"]["readiness"] is False
    assert report["assessment"]["status"] == "failed"


def test_linked_input_validation(tmp_path):
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(),
                           linked_checkouts=[{"root": tmp_path, "bogus": 1}])
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(),
                           linked_checkouts=[
                               {"root": tmp_path, "label": "a"},
                               {"root": tmp_path, "label": "b"}])
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(),
                           linked_checkouts=[{
                               "root": tmp_path, "authorized": "yes"}])
    with pytest.raises(TP.PreflightError):
        TP.build_preflight(tmp_path, "g1", providers=providers(),
                           linked_checkouts=[
                               {"root": tmp_path / str(index),
                                "label": f"l{index}"}
                               for index in range(TP.MAX_LINKED + 1)])


def test_optional_joins_are_canonical_hashed_and_deterministic(project, tmp_path):
    fixture = _linked_member(tmp_path, "member")
    from ozagia.research.graph import ResearchGraph

    graph = ResearchGraph.create("question", root=project)
    claim = graph.record("Claim", wording="a declared claim",
                         claim_type="inference", status="open")
    report = build(
        project,
        linked_checkouts=[{"root": fixture[0], "label": "m",
                           "authorized": True, "read_scope": ["src/**"]}],
        research_sessions=[graph.investigation_id],
    )
    assert report["hash"] == TP.report_hash(report)
    assert TP.report_json(report) == C.canonical_json(report)
    assert report["linked"]["members"][0]["readiness"] is True
    assert report["research"]["claims"][0]["id"] == claim["id"]
    again = build(
        project,
        linked_checkouts=[{"root": fixture[0], "label": "m",
                           "authorized": True, "read_scope": ["src/**"]}],
        research_sessions=[graph.investigation_id],
    )
    assert again["hash"] == report["hash"]
    envelope = TP.preflight_envelope(report)
    assert S.validate(envelope) == envelope
    assert envelope["provenance"]["inputs"] == [report["hash"]]
