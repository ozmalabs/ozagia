"""Tests for the deterministic whole-repository review (core.repo_review)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from ozagia import cli
from ozagia.core import repo_review as RR
from ozagia.core.memory import DevMemory, build_index
from ozagia.core.working_set import save_test_record

HAS_YAML = importlib.util.find_spec("yaml") is not None


# --------------------------------------------------------------------------- #
# fixture repository
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY"):
        env.pop(name, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Review Tester",
        "GIT_AUTHOR_EMAIL": "review@example.com",
        "GIT_COMMITTER_NAME": "Review Tester",
        "GIT_COMMITTER_EMAIL": "review@example.com",
    })
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, env=env, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


FILES = {
    ".gitignore": ".ozagia/\n",
    # Resource admission off, as in the code-review tests.
    "ozagia/resources.toml": "enabled = false\n",
    ".ozagia.toml": '[review]\nlayers = ["src/pkg/b", "src/pkg/a"]\n',
    "src/pkg/__init__.py": "",
    "src/pkg/a/__init__.py": "",
    # pkg.a and pkg.b import each other: a package cycle.
    "src/pkg/a/mod_a.py": "from pkg.b import mod_b\n\n\ndef fa():\n"
                          "    return mod_b.fb()\n",
    "src/pkg/b/__init__.py": "",
    "src/pkg/b/mod_b.py": "from pkg.a import mod_a\n\n\ndef fb():\n"
                          "    return 1\n\n\ndef unused_thing():\n"
                          "    return mod_a\n",
    # No test and no caller.
    "src/pkg/c.py": "def lonely():\n    return 3\n",
    "src/pkg/danger.py": "import ast\nimport subprocess\n\n\n"
                         "def run_it(cmd):\n"
                         "    ast.literal_eval('1')\n"
                         "    return subprocess.run(cmd, shell=True)\n",
    "tests/test_a.py": "from pkg.a.mod_a import fa\nfrom pkg.danger import run_it\n"
                       "from pkg.b.mod_b import fb\n\n\n"
                       "def test_fa():\n    assert fa() == fb() == 1\n"
                       "    assert run_it\n",
    "README.md": "# Fixture\n\nSee [the guide](docs/guide.md).\n",
    "docs/guide.md": (
        "# Guide\n\n"
        "The code is in `src/pkg/c.py` and `src/pkg/missing.py`.\n"
        "Call `pkg.a.mod_a.fa` or `pkg.a.mod_a.nothing_here`.\n"
        "A [broken link](../src/pkg/gone.py) and a [web link](https://example.com).\n"
        "Paths of other trees such as `.codex/config.toml` are not checked.\n"
        "```\nsrc/pkg/in_fence.py\n```\n"),
    "docs/plans/old.md": "# Plan\n\nAdd `src/pkg/planned.py`.\n",
    "deploy/compose.yaml": (
        "services:\n"
        "  web:\n"
        "    image: nginx\n"
        "    ports:\n"
        "      - \"8080:80\"\n"
        "  db:\n"
        "    image: postgres:16\n"
        "    restart: always\n"
        "    healthcheck:\n"
        "      test: [\"CMD\", \"true\"]\n"),
    ".env": "DEBUG=1\n",
    ".env.example": "DEBUG=0\n",
    "keys/id_ed25519": "placeholder, not a key\n",
    "pyproject.toml": "[project]\nname = \"fixture\"\nversion = \"0\"\n"
                      "dependencies = [\"requests>=2\"]\n",
}


def _graph(repo: Path) -> Path:
    path = repo / ".ozagia" / "graph.json"
    build_index(repo, path, include_docs="features", include_git=True)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "commit.gpgsign", "false")
    for rel, text in FILES.items():
        _write(root, rel, text)
    _commit(root, "fixture")
    now = time.time()
    save_test_record(root, {
        "tests": {"test:tests/test_a.py::test_fa":
                  {"outcome": "failed", "ts": now, "since": now - 60}},
        "last": {"ts": now, "failed": 1, "complete": True},
    })
    _graph(root)
    return root


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
        assert finding["id"] == RR.finding_id(finding["type"], finding["keys"])
        assert RR.FINDING_TYPES[finding["type"]] == finding["section"]
        assert finding["check"] == RR.FINDING_CHECKS[finding["type"]]
        assert finding["severity"] in RR.SEVERITIES
        assert finding["evidence"]
        goal = finding["suggested_goal"]
        assert goal["targets"] == finding["keys"]
        assert {c["kind"] for c in goal["criteria"]} <= set(RR.CRITERIA_KINDS)
        assert {"kind": "review_resolved",
                "args": {"finding_id": finding["id"]}} in goal["criteria"]
    assert set(RR.FINDING_TYPES) == set(RR.FINDING_CHECKS)
    # Ids are stable across runs.
    again = RR.review_repo(repo)
    assert [f["id"] for f in again["findings"]] == [
        f["id"] for f in packet["findings"]]


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
    assert all(f["confidence"] == "high" for f in _of(packet, "untested_module"))
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
    assert {"kind": "file_absent", "args": {"key": "file:.env"}} in \
        env["suggested_goal"]["criteria"]
    assert _keys(packet, "sensitive_file") == {"file:keys/id_ed25519"}
    danger = _of(packet, "dangerous_construct")
    assert [f["keys"] for f in danger] == [["file:src/pkg/danger.py"]]
    assert all("eval" not in item for item in danger[0]["evidence"])
    assert "not a security audit" in packet["sections"]["security"]["data"]["note"]


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
