"""Tests for the deterministic whole-repository review (core.repo_review)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ozagia import cli
from ozagia.core import repo_review as RR
from ozagia.core.memory import DevMemory, build_index

from review_fixture import (  # noqa: F401 - shared fixture helpers
    FILES, _commit, _git, _graph, _rich_repo, _write, make_repo)

HAS_YAML = importlib.util.find_spec("yaml") is not None


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFOs")
def test_graph_fifo_is_rejected_without_blocking(tmp_path):
    path = tmp_path / "graph.json"
    os.mkfifo(path)
    code = ("from ozagia.core.repo_review import _read_graph_nofollow\n"
            "import sys\n"
            "try:\n _read_graph_nofollow(sys.argv[1])\n"
            "except OSError: sys.exit(0)\n"
            "sys.exit(1)\n")
    result = subprocess.run([sys.executable, "-c", code, str(path)],
                            timeout=3, capture_output=True, check=False)
    assert result.returncode == 0


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path)


def _of(packet: dict[str, Any], ftype: str) -> list[dict[str, Any]]:
    return [f for f in packet["findings"] if f["type"] == ftype]


def _keys(packet: dict[str, Any], ftype: str) -> set[str]:
    return {k for f in _of(packet, ftype) for k in f["keys"]}


# --------------------------------------------------------------------------- #
# packet shape
# --------------------------------------------------------------------------- #


def test_packet_shape_and_ids(repo: Path) -> None:
    packet = RR.review_repo(repo)
    assert packet["schema"] == RR.PACKET_SCHEMA
    assert packet["truth_assessed"] is False
    assert packet["model_review"]["status"] == "not_run"
    assert packet["graph"]["freshness"] == "fresh"
    assert packet["graph"]["usable"] is True
    assert list(packet["sections"]) == list(RR.SECTIONS)
    json.dumps(packet)  # JSON-safe
    for finding in packet["findings"]:
        if finding["type"] != "package_cycle":
            assert finding["id"] == RR.finding_id(finding["type"],
                                                  finding["keys"])
        assert RR.FINDING_TYPES[finding["type"]] == finding["section"]
        assert finding["check"] == RR.FINDING_CHECKS[finding["type"]]
        assert finding["severity"] in RR.SEVERITIES
        assert finding["evidence"]
        goal = finding["suggested_goal"]
        assert goal["targets"] == finding["keys"]
        assert {c["kind"] for c in goal["criteria"]} <= set(RR.CRITERIA_KINDS)
        # The goal system adds review_resolved itself.
        assert "review_resolved" not in {c["kind"] for c in goal["criteria"]}
    assert set(RR.FINDING_TYPES) == set(RR.FINDING_CHECKS)
    # Ids are stable across runs.
    again = RR.review_repo(repo)
    assert [f["id"] for f in again["findings"]] == [
        f["id"] for f in packet["findings"]]


def test_review_cache_mode_off_does_not_create_environment_caches(repo: Path,
                                                                  monkeypatch) -> None:
    from ozagia.core import environments as EN

    monkeypatch.setattr(EN, "discover", lambda *args, **kwargs: [])
    state = repo / ".ozagia" / "environments"
    packet = RR.review_repo(repo, sections=["dependencies"], budget_s=8,
                            cache_mode="off")
    assert packet["preflight"]["ok"] is True
    assert not (state / "probes.json").exists()
    assert not (state / "imports.json").exists()


def test_review_cache_mode_rejects_unknown_value(repo: Path) -> None:
    with pytest.raises(RR.RepoReviewError, match="cache_mode"):
        RR.review_repo(repo, sections=["architecture"], cache_mode="write")


def test_review_can_override_project_interpreter_trust(repo: Path, monkeypatch) -> None:
    from ozagia.core import environments as EN

    observed = []

    def discover(root, *, trust, lock_tools, cache_mode, **kwargs):
        observed.append((trust, lock_tools, cache_mode))
        return [{"path": str(repo / ".venv" / "bin" / "python"),
                 "source": "root_venv", "project": True}]

    def probe_one(candidate, *, trusted, **kwargs):
        observed.append(("probe", trusted))
        return {**candidate, "probe": {"ok": False, "error": "not probed"}}

    monkeypatch.setattr(EN, "trust_project_python", lambda _root: True)
    monkeypatch.setattr(EN, "discover", discover)
    monkeypatch.setattr(EN, "probe_one", probe_one)
    packet = RR.review_repo(repo, sections=["dependencies"], budget_s=8,
                            cache_mode="off", trust_project_python=False)
    assert packet["preflight"]["ok"] is True
    assert (False, False, "off") in observed
    assert ("probe", False) in observed


def test_review_trust_override_must_be_boolean_or_none(repo: Path) -> None:
    with pytest.raises(RR.RepoReviewError, match="trust_project_python"):
        RR.review_repo(repo, sections=["architecture"], trust_project_python="yes")


def test_architecture_cycles_and_layers(repo: Path) -> None:
    packet = RR.review_repo(repo, sections=["architecture"])
    assert list(packet["sections"]) == ["architecture"]
    cycle = _of(packet, "package_cycle")
    assert len(cycle) == 1
    assert "file:src/pkg/a/mod_a.py" in cycle[0]["keys"]
    assert "file:src/pkg/b/mod_b.py" in cycle[0]["keys"]
    assert cycle[0]["severity"] == "major"
    violations = _of(packet, "layer_violation")
    assert [f["keys"] for f in violations] == [
        ["file:src/pkg/a/__init__.py", "file:src/pkg/b/mod_b.py"]]
    data = packet["sections"]["architecture"]["data"]
    assert data["hubs"] and data["import_edges"] >= 2


def test_tests_section(repo: Path) -> None:
    packet = RR.review_repo(repo, sections=["tests"])
    untested = _keys(packet, "untested_module")
    assert "file:src/pkg/c.py" in untested
    assert "file:src/pkg/a/mod_a.py" not in untested
    assert all(f["confidence"] == "medium" for f in _of(packet, "untested_module"))
    failing = _of(packet, "failing_test")
    assert [f["keys"] for f in failing] == [["test:tests/test_a.py::test_fa"]]
    assert {"kind": "test_passes",
            "args": {"key": "test:tests/test_a.py::test_fa"}} in \
        failing[0]["suggested_goal"]["criteria"]
    assert packet["sections"]["tests"]["data"]["mode"] == "graph"


def test_health_unreferenced_symbols(repo: Path) -> None:
    packet = RR.review_repo(repo, sections=["health"])
    dead = _keys(packet, "unreferenced_symbol")
    assert "sym:src/pkg/c.py::lonely" in dead
    assert "sym:src/pkg/b/mod_b.py::unused_thing" in dead
    assert "sym:src/pkg/a/mod_a.py::fa" not in dead
    assert all(f["confidence"] == "low" for f in _of(packet, "unreferenced_symbol"))
    assert "health.unreferenced_symbols" in packet["checked"]


def test_docs_stale_references(repo: Path) -> None:
    packet = RR.review_repo(repo, sections=["docs"])
    stale = _of(packet, "stale_doc_reference")
    assert [f["keys"] for f in stale] == [["file:docs/guide.md"]]
    evidence = " ".join(stale[0]["evidence"])
    assert "src/pkg/missing.py" in evidence
    assert "gone.py" in evidence
    assert "nothing_here" in evidence
    assert "src/pkg/c.py" not in evidence
    assert "in_fence" not in evidence
    assert ".codex" not in evidence
    assert "example.com" not in evidence
    assert any(item["check"] == "history_docs"
               for item in packet["not_checked"])


def test_security_section(repo: Path) -> None:
    packet = RR.review_repo(repo, sections=["security"])
    assert _keys(packet, "committed_env_file") == {"file:.env"}
    env = _of(packet, "committed_env_file")[0]
    # Untracking resolves it; the file may stay on disk.
    assert env["suggested_goal"]["criteria"] == []
    assert _keys(packet, "sensitive_file") == {"file:keys/id_ed25519"}
    danger = _of(packet, "dangerous_construct")
    assert [f["keys"] for f in danger] == [["file:src/pkg/danger.py"]]
    assert all("eval" not in item for item in danger[0]["evidence"])
    assert "not a security audit" in packet["sections"]["security"]["data"]["note"]


def test_sensitive_file_names_allow_only_observed_safe_npmrc_setting(repo: Path) -> None:
    _write(repo, ".npmrc", "package-lock=false\n")
    _write(repo, "support/maintainers.gpg.key",
           "-----BEGIN PGP PUBLIC KEY BLOCK-----\n"
           "Version: GnuPG v2\n\n"
           "mQEFZXhhbXBsZXBheWxvYWQxMjM0NTY3ODkwYWJjZA==\n"
           "-----END PGP PUBLIC KEY BLOCK-----\n")
    _write(repo, "support/private.key",
           "-----BEGIN PGP PRIVATE KEY BLOCK-----\nprivate material\n")
    _write(repo, "deploy/.npmrc", "//registry.example/:_authToken=${NPM_TOKEN}\n")
    _commit(repo, "credential-shaped files")

    packet = RR.review_repo(repo, sections=["security"])

    assert _keys(packet, "sensitive_file") == {
        "file:keys/id_ed25519", "file:support/private.key",
        "file:support/maintainers.gpg.key",
        "file:deploy/.npmrc",
    }


def test_ops_and_dependencies(repo: Path) -> None:
    packet = RR.review_repo(repo, sections=["ops", "dependencies"])
    web = "ops:compose/deploy/compose.yaml#service/web"
    db = "ops:compose/deploy/compose.yaml#service/db"
    assert _keys(packet, "unpinned_image") == {web}
    assert {"kind": "ops_has", "args": {"key": web, "attr": "image",
                                        "pinned": True}} in \
        _of(packet, "unpinned_image")[0]["suggested_goal"]["criteria"]
    ports = _of(packet, "published_port")
    assert [f["keys"] for f in ports] == [[web]]
    assert ports[0]["severity"] == "minor"
    if HAS_YAML:
        assert _keys(packet, "no_restart_policy") == {web}
        assert db not in _keys(packet, "no_healthcheck")
    assert _keys(packet, "missing_lock_file") == {"file:pyproject.toml"}
    manifest = packet["sections"]["dependencies"]["data"]["manifests"][0]
    assert manifest["names"] == ["requests"] and manifest["lock"] is None


def test_churn_hotspot_without_tests(repo: Path) -> None:
    for number in range(3):
        _write(repo, "src/pkg/c.py",
               f"def lonely():\n    return {number + 4}\n")
        _commit(repo, f"churn {number}")
    _graph(repo)
    packet = RR.review_repo(repo, sections=["churn"])
    assert _keys(packet, "churn_untested") == {"file:src/pkg/c.py"}
    hotspots = packet["sections"]["churn"]["data"]["hotspots"]
    assert hotspots[0] == {"file": "src/pkg/c.py", "commits": 4}


# --------------------------------------------------------------------------- #
# graph freshness
# --------------------------------------------------------------------------- #


def test_stale_graph_degrades(repo: Path) -> None:
    _write(repo, "src/pkg/new.py", "def fresh_code():\n    return 1\n")
    _commit(repo, "new module")
    packet = RR.review_repo(repo)
    graph = packet["graph"]
    assert graph["freshness"] == "stale" and graph["usable"] is False
    assert "--index" in graph["reason"]
    skipped = {(i["section"], i["check"]) for i in packet["not_checked"]}
    assert ("architecture", "import_cycles") in skipped
    assert ("health", "unreferenced_symbols") in skipped
    assert not _of(packet, "package_cycle")
    assert not _of(packet, "unreferenced_symbol")
    # The rest still runs; untested modules fall back to file names.
    assert _keys(packet, "committed_env_file") == {"file:.env"}
    fallback = _of(packet, "untested_module")
    assert "file:src/pkg/new.py" in _keys(packet, "untested_module")
    assert all(f["confidence"] == "low" for f in fallback)

    calls: list[Path] = []

    def indexer(root: Path, graph_file: Path) -> dict[str, Any]:
        calls.append(graph_file)
        build_index(root, graph_file)
        return {"action": "built"}

    rebuilt = RR.review_repo(repo, index=True, indexer=indexer)
    assert calls == [repo.resolve() / ".ozagia" / "graph.json"]
    assert rebuilt["graph"]["freshness"] == "fresh"
    assert rebuilt["graph"]["indexed"] == {"ok": True, "action": "built"}
    assert "sym:src/pkg/new.py::fresh_code" in _keys(rebuilt, "unreferenced_symbol")


def test_graph_loader_rejects_symlink_snapshot(repo: Path, tmp_path: Path) -> None:
    graph_file = repo / ".ozagia" / "graph.json"
    outside = tmp_path / "outside-graph.json"
    outside.write_bytes(graph_file.read_bytes())
    graph_file.unlink()
    graph_file.symlink_to(outside)

    info, dev, graph_index = RR._load_graph(repo, graph_file, "a" * 40)

    assert info["freshness"] == "error"
    assert "cannot read graph snapshot" in info["reason"]
    assert dev is None
    assert graph_index is None


def test_graph_loader_rejects_symlink_state_directory(repo: Path,
                                                       tmp_path: Path) -> None:
    state = repo / ".ozagia"
    moved = tmp_path / "state"
    state.rename(moved)
    state.symlink_to(moved, target_is_directory=True)

    info, dev, graph_index = RR._load_graph(
        repo, state / "graph.json", "a" * 40)

    assert info["freshness"] == "error"
    assert dev is None
    assert graph_index is None


def test_missing_graph_and_failed_index(repo: Path) -> None:
    (repo / ".ozagia" / "graph.json").unlink()

    def broken(root: Path, graph_file: Path) -> dict[str, Any]:
        raise RuntimeError("no indexer here")

    packet = RR.review_repo(repo, sections=["architecture"], index=True,
                            indexer=broken)
    assert packet["graph"]["freshness"] == "missing"
    assert "ozagia index" in packet["graph"]["reason"]
    assert packet["graph"]["indexed"]["ok"] is False
    assert packet["sections"]["architecture"]["checked"] == []


# --------------------------------------------------------------------------- #
# compare, budget, caps
# --------------------------------------------------------------------------- #


def test_compare_round_trip(repo: Path, tmp_path: Path) -> None:
    first = RR.review_repo(repo)
    previous = tmp_path / "previous.json"
    RR.atomic_write(previous, RR.packet_json(first))
    env_id = _of(first, "committed_env_file")[0]["id"]
    cycle_id = _of(first, "package_cycle")[0]["id"]

    _git(repo, "rm", "-q", ".env")
    _write(repo, "src/pkg/d.py", "def another():\n    return 4\n")
    _commit(repo, "remove env, add module")
    _graph(repo)
    second = RR.review_repo(repo, compare=previous)
    comparison = second["comparison"]
    assert comparison["previous"] == str(previous)
    assert env_id in comparison["resolved"]
    assert cycle_id in comparison["unchanged"]
    new_d = RR.finding_id("untested_module", ["file:src/pkg/d.py"])
    assert new_d in comparison["new"]
    assert comparison["not_compared"] == []
    assert any(f["id"] == env_id for f in comparison["resolved_findings"])

    # A stale graph: graph checks are not compared instead of "resolved".
    _write(repo, "src/pkg/e.py", "def more():\n    return 5\n")
    _commit(repo, "stale again")
    third = RR.review_repo(repo, compare=second)
    assert cycle_id not in third["comparison"]["resolved"]
    assert {"package_cycle", "unreferenced_symbol"} <= {
        item["type"] for item in third["comparison"]["not_compared"]}


def test_compare_rejects_other_packets(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text('{"schema": "something.else"}', encoding="utf-8")
    with pytest.raises(RR.RepoReviewError):
        RR.review_repo(repo, compare=other)


def test_zero_budget_truncates_every_section(repo: Path) -> None:
    packet = RR.review_repo(repo, budget_s=0)
    assert packet["findings"] == []
    assert packet["truncated"]["any"] is True
    assert set(packet["truncated"]["sections"]) == set(RR.SECTIONS)
    for section in packet["sections"].values():
        assert section["status"] == "skipped"
        assert section["truncated_reason"] == "budget"


def test_budget_runs_out_mid_review(repo: Path) -> None:
    ticks = iter(range(10_000))
    # Each clock read is one second; a 40 s budget stops partway through.
    packet = RR.review_repo(repo, budget_s=40, clock=lambda: float(next(ticks)))
    statuses = [s["status"] for s in packet["sections"].values()]
    assert "skipped" in statuses
    assert statuses[0] != "skipped"
    assert packet["budget"]["exhausted"] is True


def test_cap_truncates_and_keeps_all_ids(repo: Path) -> None:
    packet = RR.review_repo(repo, sections=["security"],
                            max_findings_per_section=1)
    section = packet["sections"]["security"]
    assert section["truncated_reason"] == "cap"
    assert section["findings"] == 1 and section["found"] == 3
    assert len(section["ids"]) == 3
    assert sum(section["dropped"].values()) == 2


def test_bad_inputs(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(RR.RepoReviewError):
        RR.review_repo(repo, sections=["nope"])
    with pytest.raises(RR.RepoReviewError):
        RR.review_repo(tmp_path / "not-a-repo")
    with pytest.raises(RR.RepoReviewError):
        RR.review_repo(repo, budget_s=-1)


# --------------------------------------------------------------------------- #
# model pass seam
# --------------------------------------------------------------------------- #


def test_model_pass_with_injected_runner(repo: Path) -> None:
    packet = RR.review_repo(repo, sections=["tests"])
    seen: list[Any] = []

    def runner(model: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        seen.append((model, messages))
        return {"summary": "s", "findings": [{
            "wording": "c.py looks unused", "severity": "minor",
            "issue_type": "design",
            "citation_keys": ["file:src/pkg/c.py", "file:not/there.py"]}]}

    out = RR.run_model_pass(packet, model_runner=runner, models=["fake"])
    assert len(seen) == 1 and RR.COMPACT_SCHEMA in seen[0][1][1]["content"]
    review = out["model_review"]
    assert review["status"] == "injected" and not review["errors"]
    model = review["findings"][0]
    assert model["source"] == "model" and model["reviewer"] == "critic:fake"
    assert model["keys"] == ["file:src/pkg/c.py"]
    assert model["unknown_keys"] == ["file:not/there.py"]
    assert out["findings"] == packet["findings"]
    assert out["truth_assessed"] is False


# --------------------------------------------------------------------------- #
# CLI and MCP
# --------------------------------------------------------------------------- #


def test_cli_exit_codes_and_output(repo: Path, tmp_path: Path,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "packet.json"
    assert cli.main(["review-repo", "--root", str(repo), "--json",
                     "--out", str(out), "--fail-on", "major"]) == 1
    packet = json.loads(capsys.readouterr().out)
    assert packet["schema"] == RR.PACKET_SCHEMA
    assert json.loads(out.read_text(encoding="utf-8"))["schema"] == RR.PACKET_SCHEMA

    assert cli.main(["review-repo", "--root", str(repo),
                     "--section", "dependencies"]) == 0
    text = capsys.readouterr().out
    assert "[dependencies]" in text and "[security]" not in text
    assert "truth_assessed: false" in text

    assert cli.main(["review-repo", "--root", str(repo), "--section",
                     "security", "--fail-on", "never", "--compare", str(out),
                     "--json"]) == 0
    compared = json.loads(capsys.readouterr().out)
    assert compared["comparison"]["new"] == []
    assert compared["comparison"]["resolved"] == []

    assert cli.main(["review-repo", "--root", str(repo), "--section", "docs",
                     "--model-pass", "--json"]) == 0
    prepared = json.loads(capsys.readouterr().out)["model_review"]
    assert prepared["status"] == "prepared"
    compact = json.loads(Path(prepared["compact_path"]).read_text(encoding="utf-8"))
    assert compact["schema"] == RR.COMPACT_SCHEMA

    assert cli.main(["review-repo", "--root", str(tmp_path / "missing")]) == 2
    assert "error:" in capsys.readouterr().err


def test_mcp_dev_review_repo(repo: Path) -> None:
    from ozagia.integrations.mcp.server import DevMemoryServer
    mem = DevMemory(repo / ".ozagia" / "graph.json",
                    repo / ".ozagia" / "journal.jsonl", root=repo)
    server = DevMemoryServer(mem)
    names = {t["name"] for t in server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert "dev_review_repo" in names

    def call(arguments: dict[str, Any]) -> dict[str, Any]:
        resp = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "dev_review_repo",
                                         "arguments": arguments}})
        assert resp["result"]["isError"] is False
        return json.loads(resp["result"]["content"][0]["text"])

    first = call({"max_findings": 3})
    assert first["ok"] is True
    assert len(first["top_findings"]) == 3
    assert first["findings_omitted"] == first["summary"]["findings"] - 3
    assert first["graph"]["freshness"] == "fresh"
    saved = Path(first["path"])
    assert saved.parent == repo.resolve() / ".ozagia" / "reviews"
    assert json.loads(saved.read_text(encoding="utf-8"))["schema"] == RR.PACKET_SCHEMA
    assert Path(first["latest"]).name == "latest.json"

    second = call({"compare_latest": True, "sections": ["security"]})
    assert second["compared_with"] == first["latest"]
    assert second["comparison"]["new"] == 0
    assert second["comparison"]["resolved"] == 0


# --------------------------------------------------------------------------- #
# the suggested-goal contract with the goal system
# --------------------------------------------------------------------------- #

# A local copy of the goal system's argument table for the criteria kinds a
# suggested goal may use: kind -> (allowed args, required args, key prefix).
# `review_resolved` is not allowed: the goal system adds it itself when it
# converts a finding.
GOAL_CRITERIA = {
    "test_passes": ({"key"}, {"key"}, "test:"),
    "test_fails": ({"key"}, {"key"}, "test:"),
    "symbol_exists": ({"key"}, {"key"}, "sym:"),
    "symbol_absent": ({"key"}, {"key"}, "sym:"),
    "file_exists": ({"key"}, {"key"}, "file:"),
    "file_absent": ({"key"}, {"key"}, "file:"),
    "ops_has": ({"key", "attr", "value", "pinned"}, {"key"}, "ops:"),
}
TARGET_PREFIXES = ("file:", "sym:", "test:", "ops:")


def _contract_errors(finding: dict[str, Any]) -> list[str]:
    goal = finding["suggested_goal"]
    errors = []
    if not goal["title"]:
        errors.append("no title")
    for key in goal["targets"]:
        if not key.startswith(TARGET_PREFIXES):
            errors.append(f"target {key} is not a graph key")
    for criterion in goal["criteria"]:
        kind, args = criterion.get("kind"), criterion.get("args")
        if kind not in GOAL_CRITERIA:
            errors.append(f"kind {kind} is not in the contract")
            continue
        allowed, required, prefix = GOAL_CRITERIA[kind]
        if not isinstance(args, dict):
            errors.append(f"{kind}: args are not an object")
            continue
        if set(args) - allowed:
            errors.append(f"{kind}: unknown args {sorted(set(args) - allowed)}")
        if required - set(args):
            errors.append(f"{kind}: missing {sorted(required - set(args))}")
        if not str(args.get("key", "")).startswith(prefix):
            errors.append(f"{kind}: key {args.get('key')} is not {prefix}")
        if "pinned" in args and not isinstance(args["pinned"], bool):
            errors.append(f"{kind}: pinned is not a boolean")
    return errors



def test_every_suggested_goal_meets_the_contract(repo: Path) -> None:
    _rich_repo(repo)
    packet = RR.review_repo(repo, max_findings_per_section=500)
    seen = {f["type"] for f in packet["findings"]}
    assert seen == set(RR.FINDING_TYPES), set(RR.FINDING_TYPES) - seen
    for finding in packet["findings"]:
        assert _contract_errors(finding) == [], (finding["type"], finding)
        kinds = {c["kind"] for c in finding["suggested_goal"]["criteria"]}
        assert "review_resolved" not in kinds
    dead = _of(packet, "unreferenced_symbol")[0]
    assert dead["suggested_goal"]["title"].startswith("Remove")
    assert dead["suggested_goal"]["criteria"] == [
        {"kind": "symbol_absent", "args": {"key": dead["keys"][0]}}]
    image = _of(packet, "unpinned_image")[0]
    assert image["suggested_goal"]["targets"][0].startswith("ops:")
    assert image["suggested_goal"]["criteria"] == [
        {"kind": "ops_has", "args": {"key": image["keys"][0], "attr": "image",
                                     "pinned": True}}]
    # The packet keeps what the goal system reads.
    assert packet["root"] and packet["head_commit"] and packet["generated_at"]
    assert packet["generator"] == RR.GENERATOR and packet["tree"]["head"]
    assert RR.parse_generated_at(packet["generated_at"]) is not None


def test_section_refuses_review_resolved(repo: Path) -> None:
    files, _skipped = RR._tracked_files(repo)
    ctx = RR._Context(repo, "HEAD", files, {}, None, None, layers=(),
                      deadline=1e18, clock=time.monotonic, churn_days=90,
                      cap=50)
    sec = RR._Section("tests", ctx)
    with pytest.raises(ValueError):
        sec.add("untested_module", "minor", ["file:x.py"], "m",
                criteria=[{"kind": "review_resolved",
                           "args": {"finding_id": "RR-1"}}])


# --------------------------------------------------------------------------- #
# reading files
# --------------------------------------------------------------------------- #


def test_tracked_symlinks_are_not_followed(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text(
        "[x](sk-live-SECRETVALUE.md) and `src/pkg/leaked_TOKEN_abc123.py`\n",
        encoding="utf-8")
    (repo / "docs" / "notes.md").symlink_to(outside / "secret.md")
    (repo / "docs" / "inner.md").symlink_to(repo / "docs" / "guide.md")
    _commit(repo, "links")
    packet = RR.review_repo(repo)
    text = json.dumps(packet)
    assert "SECRETVALUE" not in text and "leaked_TOKEN" not in text
    assert packet["skipped_files"] == {"docs/inner.md": "symlink",
                                       "docs/notes.md": "symlink"}
    assert any(item["check"] == "skipped_files"
               for item in packet["not_checked"])
    assert "file:docs/notes.md" not in _keys(packet, "stale_doc_reference")


def test_doc_span_relative_to_doc_reads_in_root_file(repo: Path,
                                                     tmp_path: Path) -> None:
    _write(repo, "docs/span.md",
           "See `../src/pkg/c.py::lonely` and `../src/pkg/c.py::nothing`.\n")
    _commit(repo, "span doc")
    # Files above the root with the opposite contents: if the check read
    # root/../src/pkg/c.py, both answers would flip.
    _write(tmp_path, "src/pkg/c.py", "def nothing():\n    return 0\n")
    packet = RR.review_repo(repo, sections=["docs"])
    span = [f for f in _of(packet, "stale_doc_reference")
            if f["keys"] == ["file:docs/span.md"]]
    assert len(span) == 1
    evidence = " ".join(span[0]["evidence"])
    assert "symbol nothing" in evidence
    assert "lonely" not in evidence


# --------------------------------------------------------------------------- #
# test detection
# --------------------------------------------------------------------------- #


def test_untested_module_unions_graph_names_and_imports(repo: Path) -> None:
    _write(repo, "src/pkg/e.py", "def e_fn():\n    return 1\n")
    _write(repo, "src/pkg/f.py", "def f_fn():\n    return 1\n")
    # e.py is only imported as a module from its package; f.py only has a
    # test named after it.
    _write(repo, "tests/test_misc.py", "from pkg import e\n\n\n"
                                       "def test_e():\n    assert e.e_fn()\n")
    _write(repo, "tests/test_f.py", "def test_nothing():\n    assert True\n")
    _commit(repo, "more modules")
    _graph(repo)
    packet = RR.review_repo(repo, sections=["tests"])
    assert packet["sections"]["tests"]["modes"] == {"untested_modules": "graph"}
    untested = _keys(packet, "untested_module")
    assert "file:src/pkg/e.py" not in untested
    assert "file:src/pkg/f.py" not in untested
    assert "file:src/pkg/c.py" in untested
    assert all(f["confidence"] != "high" for f in _of(packet, "untested_module"))


def test_imported_modules() -> None:
    names = RR._imported_modules(
        "import a.b as c, d\nfrom x.y import (z,  # note\n    w as v)\n"
        "from . import rel\n")
    assert {"a.b", "d", "x.y", "x.y.z", "x.y.w"} <= names
    assert not any(n.startswith(".") for n in names)


# --------------------------------------------------------------------------- #
# imports against manifests and interpreters
# --------------------------------------------------------------------------- #


def _stub_interpreters(monkeypatch, *module_sets: set[str]) -> None:
    from ozagia.core import environments as EN
    paths = [f"/stub/env{n}/bin/python" for n in range(len(module_sets))]
    table = dict(zip(paths, module_sets))
    monkeypatch.setattr(EN, "discover", lambda root, **_k: [
        {"path": p, "source": "path", "evidence": p, "also": []} for p in paths])
    monkeypatch.setattr(EN, "probe", lambda python, **_k: {
        "ok": True, "path": python, "version": "3.13.0", "stdlib": sorted(EN.STDLIB),
        "modules": {m: [] for m in table[python]}, "scanned": []})


def _import_repo(repo: Path) -> None:
    _write(repo, "pyproject.toml",
           FILES["pyproject.toml"]
           + "\n[project.optional-dependencies]\nplot = [\"zz-plot\"]\n"
           "\n[dependency-groups]\ndev = [\"zz-devtool\"]\n")
    _write(repo, "src/pkg/uses_plot.py", "import zz_plot\n")
    _write(repo, "src/pkg/later_plot.py", "def f():\n    import zz_plot\n")
    _write(repo, "src/pkg/guarded.py",
           "try:\n    import zz_guarded\nexcept ImportError:\n    zz_guarded = None\n")
    _write(repo, "scripts/tool.py", "import zz_plot\n")
    _write(repo, "tests/test_dev.py",
           "import zz_devtool\nfrom pkg.uses_plot import zz_plot\n\n\n"
           "def test_d():\n    import zz_undeclared\n    assert zz_undeclared\n")
    _commit(repo, "imports")
    _graph(repo)


def test_interpreter_probing_can_be_disabled_without_skipping_static_imports(
        repo: Path, monkeypatch) -> None:
    from ozagia.core import environments as EN

    _import_repo(repo)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("interpreter discovery or probing must not run")

    monkeypatch.setattr(EN, "discover", forbidden)
    monkeypatch.setattr(EN, "probe_one", forbidden)
    packet = RR.review_repo(repo, sections=["dependencies"], cache_mode="off",
                            probe_interpreters=False)

    assert "zz_undeclared" in {
        finding["message"].split()[0]
        for finding in _of(packet, "undeclared_import")
    }
    section = packet["sections"]["dependencies"]
    assert section["data"]["interpreter_probing"] == "disabled"
    reasons = {item["check"]: item["reason"] for item in packet["not_checked"]}
    assert reasons["test_environments"] == (
        "interpreter probing was disabled for this review")


def test_probe_interpreters_option_must_be_boolean(repo: Path) -> None:
    with pytest.raises(RR.RepoReviewError, match="probe_interpreters"):
        RR.review_repo(repo, sections=["architecture"], probe_interpreters="no")


def test_dependency_import_findings(repo: Path, monkeypatch) -> None:
    _import_repo(repo)
    _stub_interpreters(monkeypatch, {"zz_plot"}, set())
    packet = RR.review_repo(repo, sections=["dependencies"])
    # Imported, declared nowhere: lazy imports count, guarded ones do not.
    assert {f["message"].split()[0] for f in _of(packet, "undeclared_import")} \
        == {"zz_undeclared"}
    undeclared = _of(packet, "undeclared_import")[0]
    assert undeclared["keys"] == ["file:tests/test_dev.py"]
    assert undeclared["id"] == RR.finding_id("undeclared_import",
                                             ["import:zz_undeclared"])
    assert undeclared["suggested_goal"]["criteria"] == [
        {"kind": "test_passes", "args": {"key": "test:tests/test_dev.py::test_d"}}]
    # Declared only in an extra and imported at import time by a source
    # module (not a script, not lazily); the dev group covers the test.
    optional = _of(packet, "optional_only_import")
    assert [f["keys"] for f in optional] == [["file:src/pkg/uses_plot.py"]]
    assert "plot" in optional[0]["evidence"][0]
    # Through pkg.uses_plot, the test needs zz_plot too; no interpreter
    # has both zz_devtool and zz_plot.
    gaps = _of(packet, "test_env_gap")
    assert [f["keys"] for f in gaps] == [["file:tests/test_dev.py"]]
    assert "missing zz_devtool" in gaps[0]["evidence"][0]
    assert gaps[0]["suggested_goal"]["criteria"][0]["kind"] == "test_passes"
    data = packet["sections"]["dependencies"]
    assert {"undeclared_imports", "optional_only_imports",
            "test_environments"} <= set(data["checked"])
    assert [i["path"] for i in data["data"]["interpreters"]] == [
        "/stub/env0/bin/python", "/stub/env1/bin/python"]


def test_dependency_import_checks_degrade(repo: Path, monkeypatch) -> None:
    _import_repo(repo)
    _stub_interpreters(monkeypatch, {"zz_plot", "zz_devtool"})
    packet = RR.review_repo(repo, sections=["dependencies"])
    assert not _of(packet, "test_env_gap")
    _stub_interpreters(monkeypatch)
    packet = RR.review_repo(repo, sections=["dependencies"])
    reasons = {i["check"]: i["reason"] for i in packet["not_checked"]}
    assert reasons["test_environments"] == "no Python interpreter could be probed"
    # Without a Python manifest, imports cannot be compared with one.
    (repo / "pyproject.toml").unlink()
    _commit(repo, "no manifest")
    packet = RR.review_repo(repo, sections=["dependencies"])
    reasons = {i["check"]: i["reason"] for i in packet["not_checked"]}
    assert reasons["undeclared_imports"] == "no Python manifest is tracked"
    assert not _of(packet, "undeclared_import")


def test_import_checks_record_the_probed_set_and_compare_by_it(
        repo: Path, monkeypatch) -> None:
    _import_repo(repo)
    _stub_interpreters(monkeypatch, {"zz_plot"}, set())
    first = RR.review_repo(repo, sections=["dependencies"])
    section = first["sections"]["dependencies"]
    expected = [["/stub/env0/bin/python", "exec", "3.13.0"],
                ["/stub/env1/bin/python", "exec", "3.13.0"]]
    assert section["populations"] == {"optional_only_imports": expected,
                                      "test_environments": expected,
                                      "undeclared_imports": expected}
    assert RR.check_population(first, "test_env_gap") == expected
    gap = _of(first, "test_env_gap")[0]
    undeclared = _of(first, "undeclared_import")[0]
    # Another interpreter set (an active venv, another ozagia install), and
    # the lazy import is gone.
    _write(repo, "tests/test_dev.py",
           "import zz_devtool\nfrom pkg.uses_plot import zz_plot\n\n\n"
           "def test_d():\n    assert zz_plot\n")
    _commit(repo, "no lazy import")
    _stub_interpreters(monkeypatch, {"zz_plot", "zz_devtool"})
    second = RR.review_repo(repo, sections=["dependencies"])
    assert not _of(second, "test_env_gap") and not _of(second, "undeclared_import")
    diff = RR.compare_packets(second, first)
    assert {e["type"]: e["reason"] for e in diff["not_compared"]}["test_env_gap"] \
        .startswith("the check ran over another set of probed interpreters")
    assert gap["id"] not in diff["resolved"]
    held = {h["id"]: h["reason"] for h in diff["held"]}
    assert "probed interpreters" in held[undeclared["id"]]
    assert undeclared["id"] not in diff["resolved"]
    # A goal anchored on the first packet cannot be closed by the second.
    assert RR.finding_state(second, gap["id"], type="test_env_gap",
                            keys=gap["keys"]) == "absent"
    assert RR.finding_state(second, gap["id"], type="test_env_gap", keys=gap["keys"],
                            population=RR.check_population(first, "test_env_gap")) \
        == "not_checked"
    # The same set again: compared as usual.
    third = RR.review_repo(repo, sections=["dependencies"])
    diff = RR.compare_packets(third, second)
    assert "test_env_gap" not in {e["type"] for e in diff["not_compared"]}
    assert RR.finding_state(third, gap["id"], type="test_env_gap", keys=gap["keys"],
                            population=RR.check_population(second, "test_env_gap")) \
        == "absent"


def test_import_checks_respect_the_budget_around_probes(repo: Path,
                                                         monkeypatch) -> None:
    from ozagia.core import environments as EN
    _import_repo(repo)
    now = [0.0]
    timeouts: list[float] = []
    paths = [f"/stub/slow{n}/bin/python" for n in range(3)]
    monkeypatch.setattr(EN, "discover", lambda root, **_k: [
        {"path": p, "source": "path", "evidence": p, "also": []} for p in paths])

    def slow_probe(python, *, timeout, **_k):
        timeouts.append(timeout)
        now[0] += 25.0
        return {"ok": False, "path": python, "error": "probe timed out"}

    monkeypatch.setattr(EN, "probe", slow_probe)
    packet = RR.review_repo(repo, sections=["dependencies"], budget_s=40,
                            clock=lambda: now[0])
    section = packet["sections"]["dependencies"]
    assert len(timeouts) == 2 and all(t <= EN.PROBE_TIMEOUT_S for t in timeouts)
    assert section["truncated_reason"] == "budget"
    assert "test_environments" not in section["checked"]


def test_import_checks_cache_parsed_imports(repo: Path, monkeypatch) -> None:
    _import_repo(repo)
    _stub_interpreters(monkeypatch, {"zz_plot"})
    first = RR.review_repo(repo, sections=["dependencies"])
    cold = first["sections"]["dependencies"]["data"]["import_cache"]
    assert cold["misses"] > 0
    second = RR.review_repo(repo, sections=["dependencies"])
    warm = second["sections"]["dependencies"]["data"]["import_cache"]
    assert warm["misses"] == 0 and warm["hits"] >= cold["misses"]
    assert [f["id"] for f in second["findings"]] == [f["id"] for f in first["findings"]]


# --------------------------------------------------------------------------- #
# compare across changed populations
# --------------------------------------------------------------------------- #


def test_compare_refuses_across_mode_change(repo: Path, tmp_path: Path) -> None:
    first = RR.review_repo(repo, sections=["tests"])
    assert _of(first, "untested_module")
    second = RR.review_repo(repo, sections=["tests"],
                            graph_path=tmp_path / "missing.json")
    assert second["sections"]["tests"]["modes"] == {
        "untested_modules": "filename"}
    comparison = RR.compare_packets(second, first)
    ids = {f["id"] for f in _of(first, "untested_module")}
    assert not ids & set(comparison["resolved"])
    reasons = {i["type"]: i["reason"] for i in comparison["not_compared"]}
    assert "mode" in reasons["untested_module"]


def test_compare_holds_findings_of_skipped_files(repo: Path) -> None:
    _write(repo, "src/pkg/big.py", "".join(f"X{n} = {n}\n" for n in range(1001)))
    _commit(repo, "big")
    first = RR.review_repo(repo, sections=["health", "docs"])
    big = _of(first, "large_file")[0]["id"]
    guide = [f["id"] for f in _of(first, "stale_doc_reference")
             if f["keys"] == ["file:docs/guide.md"]][0]
    # The file grows past the read limit and the doc becomes binary.
    _write(repo, "src/pkg/big.py", "X = 1\n" + "#" * (RR.MAX_READ_BYTES + 10))
    (repo / "docs" / "guide.md").write_bytes(b"# Guide\0\n")
    _commit(repo, "unreadable")
    second = RR.review_repo(repo, sections=["health", "docs"], compare=first)
    comparison = second["comparison"]
    assert big not in comparison["resolved"]
    assert guide not in comparison["resolved"]
    held = {i["id"] for i in comparison["held"]}
    assert {big, guide} <= held
    skipped = second["sections"]["health"]["skipped_files"]
    assert skipped["large_files"]["src/pkg/big.py"] == "over the read limit"
    assert second["sections"]["docs"]["skipped_files"][
        "links_and_code_spans"]["docs/guide.md"] == "binary"
    assert RR.finding_state(second, big, type="large_file") == "not_checked"
    assert RR.finding_state(second, big, type="large_file",
                            keys=["file:src/pkg/big.py"]) == "not_checked"
    assert RR.finding_state(second, big, type="large_file",
                            keys=["file:src/pkg/other.py"]) == "absent"


def test_compare_without_test_record(repo: Path) -> None:
    first = RR.review_repo(repo, sections=["tests"])
    failing = _of(first, "failing_test")[0]["id"]
    (repo / ".ozagia" / "working_set" / "tests.json").unlink()
    second = RR.review_repo(repo, sections=["tests"], compare=first)
    assert "tests.failing_tests" not in second["checked"]
    assert failing not in second["comparison"]["resolved"]
    assert "failing_test" in {i["type"]
                              for i in second["comparison"]["not_compared"]}
    assert RR.finding_state(second, failing, type="failing_test") == "not_checked"


def test_compare_refuses_other_root(repo: Path) -> None:
    first = RR.review_repo(repo)
    other = {**first, "root": "/somewhere/else"}
    comparison = RR.compare_packets({**first, "sections": {}}, other)
    assert comparison["root_mismatch"] is True
    assert comparison["resolved"] == [] and comparison["new"] == []
    assert {i["type"] for i in comparison["not_compared"]} == set(RR.FINDING_TYPES)


# --------------------------------------------------------------------------- #
# architecture
# --------------------------------------------------------------------------- #


def test_package_cycle_id_follows_packages(repo: Path) -> None:
    first = _of(RR.review_repo(repo, sections=["architecture"]), "package_cycle")
    expected = RR.finding_id("package_cycle", ["package:src/pkg/a",
                                               "package:src/pkg/b"])
    assert [f["id"] for f in first] == [expected]
    _write(repo, "src/pkg/a/extra.py", "from pkg.b.mod_b import fb\n\n\n"
                                       "def ex():\n    return fb()\n")
    # Two modules directly in src/pkg import each other: an import cycle,
    # not a package cycle.
    _write(repo, "src/pkg/p.py", "from pkg.q import qf\n\n\ndef pf():\n"
                                 "    return qf\n")
    _write(repo, "src/pkg/q.py", "from pkg.p import pf\n\n\ndef qf():\n"
                                 "    return pf\n")
    _commit(repo, "more links")
    _graph(repo)
    packet = RR.review_repo(repo, sections=["architecture"])
    second = _of(packet, "package_cycle")
    assert [f["id"] for f in second] == [expected]
    assert "file:src/pkg/a/extra.py" in second[0]["keys"]
    cycles = [set(f["keys"]) for f in _of(packet, "import_cycle")]
    assert {"file:src/pkg/p.py", "file:src/pkg/q.py"} in cycles


# --------------------------------------------------------------------------- #
# security and ops
# --------------------------------------------------------------------------- #


def test_fixture_trees_are_excluded_from_security_and_ops(repo: Path) -> None:
    _write(repo, "tests/fixtures/x/service-account.json", "{}\n")
    _write(repo, "tests/fixtures/x/.env", "A=1\n")
    _write(repo, "examples/evil.py", "def f(s):\n    return eval(s)\n")
    _write(repo, "tests/fixtures/ops/compose.yaml",
           "services:\n  app:\n    image: redis\n    ports:\n      - \"80:80\"\n")
    _commit(repo, "fixtures")
    packet = RR.review_repo(repo, sections=["security", "ops"])
    keys = {k for f in packet["findings"] for k in f["keys"]}
    assert not any("fixtures" in k or "examples" in k for k in keys)
    notes = {(i["check"], i["reason"]) for i in packet["not_checked"]
             if i["section"] in ("security", "ops")}
    assert any(check == "sensitive_names" and "service-account.json" in reason
               for check, reason in notes)
    assert any(check == "dangerous_constructs" and "evil.py" in reason
               for check, reason in notes)
    assert any(check == "ops_source" and "compose.yaml" in reason
               for check, reason in notes)


def test_yaml_load_with_explicit_loader(repo: Path) -> None:
    _write(repo, "src/pkg/loaders.py",
           "import yaml\n\n\n"
           "def safe(t):\n    return yaml.load(t, Loader=yaml.SafeLoader)\n\n\n"
           "def custom(t):\n    return yaml.load(\n        t,\n"
           "        Loader=MyLoader)\n")
    _write(repo, "src/pkg/unsafe.py",
           "import yaml\n\n\n"
           "def bare(t):\n    return yaml.load(t)\n\n\n"
           "def full(t):\n    return yaml.load(t,\n"
           "                     Loader=yaml.Loader)\n")
    _commit(repo, "yaml")
    packet = RR.review_repo(repo, sections=["security"])
    danger = {f["keys"][0]: f for f in _of(packet, "dangerous_construct")}
    assert "file:src/pkg/loaders.py" not in danger
    evidence = danger["file:src/pkg/unsafe.py"]["evidence"]
    assert [e.split(":")[0] for e in evidence] == ["line 5", "line 9"]


def test_env_file_names() -> None:
    for name in (".env", ".env.local", "prod.env", "app.env"):
        assert RR._env_file(name), name
    for name in (".env.example", "example.env", "prod.env.sample",
                 "template.env", "env.py", ".envrc"):
        assert not RR._env_file(name), name


# --------------------------------------------------------------------------- #
# budget, finding_state, saving
# --------------------------------------------------------------------------- #


def test_non_finite_budget_is_rejected(repo: Path,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    for value in (float("nan"), float("inf")):
        with pytest.raises(RR.RepoReviewError):
            RR.review_repo(repo, budget_s=value)
    assert cli.main(["review-repo", "--root", str(repo),
                     "--budget-s", "nan"]) == 2
    assert "finite" in capsys.readouterr().err
    from ozagia.integrations.mcp.server import DevMemoryServer
    mem = DevMemory(repo / ".ozagia" / "graph.json",
                    repo / ".ozagia" / "journal.jsonl", root=repo)
    resp = DevMemoryServer(mem).handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "dev_review_repo",
                   "arguments": {"budget_s": float("nan")}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["ok"] is False and "finite" in body["error"]
    assert not (repo / ".ozagia" / "reviews").exists()


def test_finding_state(repo: Path, tmp_path: Path) -> None:
    packet = RR.review_repo(repo, sections=["security", "health"],
                            max_findings_per_section=1)
    section = packet["sections"]["security"]
    capped = [i for i in section["ids"]
              if i not in {f["id"] for f in packet["findings"]}]
    assert capped
    assert RR.finding_state(packet, capped[0]) == "present"
    env = RR.finding_id("committed_env_file", ["file:.env"])
    assert RR.finding_state(packet, env, type="committed_env_file") == "present"

    _git(repo, "rm", "-q", ".env")
    _commit(repo, "drop env")
    later = RR.review_repo(repo, sections=["security"])
    assert RR.finding_state(later, env, type="committed_env_file") == "absent"
    # Without a type, an absent id cannot be tied to a check.
    assert RR.finding_state(later, env) == "not_checked"
    assert RR.finding_state(later, env, type="no_such_type") == "not_checked"
    # A section that did not run.
    dead = RR.finding_id("unreferenced_symbol", ["sym:src/pkg/c.py::lonely"])
    assert RR.finding_state(later, dead, type="unreferenced_symbol") == \
        "not_checked"
    # A stale graph leaves graph checks unchecked; filename mode is not the
    # full population of untested_modules.
    stale = RR.review_repo(repo, sections=["health", "tests"])
    assert stale["graph"]["usable"] is False
    assert RR.finding_state(stale, "RR-0", type="unreferenced_symbol") == \
        "not_checked"
    assert RR.finding_state(stale, "RR-0", type="untested_module") == \
        "not_checked"
    _graph(repo)
    fresh = RR.review_repo(repo, sections=["health", "tests"])
    assert RR.finding_state(fresh, "RR-0", type="untested_module") == "absent"


def test_save_prunes_old_packets(repo: Path) -> None:
    directory = RR.reviews_dir(repo)
    directory.mkdir(parents=True)
    for number in range(25):
        (directory / f"repo_review-2020010{number:02d}T000000Z-abc.json"
         ).write_text("{}", encoding="utf-8")
    (directory / "compact.json").write_text("{}", encoding="utf-8")
    packet = RR.review_repo(repo, sections=["dependencies"])
    paths = RR.save_packet(repo, packet)
    names = sorted(p.name for p in directory.glob("repo_review-*.json"))
    assert len(names) == RR.MAX_SAVED_PACKETS
    assert Path(paths["path"]).name in names
    assert "repo_review-202001000T000000Z-abc.json" not in names
    assert (directory / "latest.json").is_file()
    assert (directory / "compact.json").is_file()


def test_cli_save(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["review-repo", "--root", str(repo), "--section",
                     "dependencies", "--json", "--save"]) == 0
    out = capsys.readouterr()
    packet = json.loads(out.out)
    assert out.err.startswith("saved: ")
    saved = Path(out.err.split()[1])
    assert saved.parent == repo.resolve() / ".ozagia" / "reviews"
    assert json.loads(saved.read_text(encoding="utf-8")) == packet
    latest = saved.parent / "latest.json"
    assert json.loads(latest.read_text(encoding="utf-8")) == packet
    assert cli.main(["review-repo", "--root", str(repo), "--section",
                     "dependencies", "--save"]) == 0
    assert "saved: " in capsys.readouterr().out


def test_source_read_rejects_ancestor_swap(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    directory = root / "source"
    directory.mkdir(parents=True)
    (directory / "file.py").write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.py").write_text("secret", encoding="utf-8")
    original = RR._read_graph_nofollow

    def swapped(path, *, max_bytes=None):
        directory.rename(root / "moved")
        directory.symlink_to(outside, target_is_directory=True)
        return original(path, max_bytes=max_bytes)

    monkeypatch.setattr(RR, "_read_graph_nofollow", swapped)
    content, reason = RR.read_text(root, "source/file.py")
    assert content is None
    assert reason is not None


def test_test_record_rejects_fifo_without_blocking(tmp_path):
    import os
    import subprocess
    import sys

    record = tmp_path / ".ozagia" / "working_set" / "tests.json"
    record.parent.mkdir(parents=True)
    os.mkfifo(record)
    process = subprocess.run(
        [sys.executable, "-c",
         "from pathlib import Path; from ozagia.core.repo_review import _test_record; "
         "import sys; assert _test_record(Path(sys.argv[1])) is None", str(tmp_path)],
        capture_output=True, timeout=5, check=False)
    assert process.returncode == 0, process.stderr
