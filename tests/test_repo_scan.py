"""Tests for the advisory, read-only repository scan."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from ozagia import repo_scan_cli as CLI
from ozagia.core import repo_scan as RS


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY"):
        env.pop(name, None)
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Scan Tester",
        "GIT_AUTHOR_EMAIL": "scan@example.com",
        "GIT_COMMITTER_NAME": "Scan Tester",
        "GIT_COMMITTER_EMAIL": "scan@example.com",
    })
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        env=env, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Scan Tester")
    _git(path, "config", "user.email", "scan@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    (path / ".gitignore").write_text(".ozagia/\n", encoding="utf-8")
    (path / ".ozagia.toml").write_text(
        "[index]\n"
        'source_dirs = ["src", "tests"]\n'
        'test_dirs = ["tests"]\n'
        "\n"
        "[tests]\n"
        'select = "all"\n',
        encoding="utf-8",
    )
    return path


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_node(nid: int, path: Path, rel: str, role: str
               ) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "id": nid,
            "type": "File",
            "key": f"file:{rel}",
            "attrs": {
                "name": rel,
                "path": rel,
                "language": path.suffix.lstrip(".").lower(),
                "indexed": 1,
                "size": path.stat().st_size,
                "sha256": _sha(path),
            },
        },
        {"role": role, "size": path.stat().st_size, "mtime_ns": 0},
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = _init_repo(tmp_path / "repo")
    for directory in ("src", "tests", "docs", "history/2020-01-01",
                      "__pycache__", "src/generated"):
        (repo / directory).mkdir(parents=True, exist_ok=True)

    hot_lines = "def huge():\n" + "".join(
        f"    x{i} = {i}\n" for i in range(138)) + "    return 1\n"
    (repo / "src" / "hot.py").write_text(hot_lines, encoding="utf-8")
    (repo / "src" / "app.py").write_text(
        "def app():\n    return 1\n", encoding="utf-8")
    (repo / "src" / "covered.py").write_text(
        "def covered():\n    return 2\n", encoding="utf-8")
    (repo / "src" / "uncovered.py").write_text(
        "def uncovered():\n    return 3\n", encoding="utf-8")
    (repo / "src" / "orphan.py").write_text(
        "def orphan():\n    return 4\n", encoding="utf-8")
    (repo / "src" / "hub.py").write_text(
        "def hub():\n    return 5\n", encoding="utf-8")
    shared = ("def shared_one():\n    value = 'a' * 40\n    return value\n"
              "\n"
              "def shared_two():\n    return 'b' * 40\n"
              "\n"
              "def shared_three():\n    return 'c' * 40\n")
    (repo / "src" / "dup_a.py").write_text(shared, encoding="utf-8")
    (repo / "src" / "dup_b.py").write_text(shared, encoding="utf-8")
    for index in range(1, 7):
        (repo / "src" / f"importer{index}.py").write_text(
            "from hub import hub\n\n"
            f"def use_{index}():\n    return hub()\n",
            encoding="utf-8",
        )
    (repo / "src" / "web.ts").write_text(
        "export function web(): number { return 1; }\n", encoding="utf-8")
    (repo / "tests" / "test_covered.py").write_text(
        "from covered import covered\n\n"
        "def test_covered():\n    assert covered() == 2\n",
        encoding="utf-8",
    )
    guide = "\n".join(
        f"Line {index} explains the same durable policy in consistent words."
        for index in range(40)) + "\n"
    (repo / "docs" / "guide.md").write_text(guide, encoding="utf-8")
    (repo / "docs" / "copy.md").write_text(guide, encoding="utf-8")
    (repo / "docs" / "near.md").write_text(
        guide.replace("durable", "lasting"), encoding="utf-8")
    (repo / "history" / "2020-01-01" / "old.md").write_text(
        "# Old note\n\nThis note is older than the scan window.\n",
        encoding="utf-8")
    (repo / "__pycache__" / "thing.pyc").write_bytes(b"x" * 4096)

    head = _commit(repo, "fixture")
    old_time = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
    old_epoch = 1_600_000_000.0
    os.utime(repo / "history" / "2020-01-01" / "old.md",
             (old_epoch, old_epoch))
    assert old_time

    files = [
        ("src/hot.py", "source"),
        ("src/app.py", "source"),
        ("src/covered.py", "source"),
        ("src/uncovered.py", "source"),
        ("src/orphan.py", "source"),
        ("src/hub.py", "source"),
        ("src/dup_a.py", "source"),
        ("src/dup_b.py", "source"),
        ("tests/test_covered.py", "test"),
        ("docs/guide.md", "doc"),
        ("docs/copy.md", "doc"),
        ("docs/near.md", "doc"),
    ]
    for index in range(1, 7):
        files.append((f"src/importer{index}.py", "source"))
    nodes: list[dict[str, Any]] = [{
        "id": 0,
        "type": "Repo",
        "key": "repo:",
        "attrs": {"name": "repo", "root": str(repo), "head": head,
                  "generated_at": "2026-11-16T00:00:00"},
    }]
    table: dict[str, Any] = {}
    ids: dict[str, int] = {}
    for nid, (rel, role) in enumerate(files, start=1):
        node, entry = _file_node(nid, repo / rel, rel, role)
        nodes.append(node)
        table[rel] = entry
        ids[rel] = nid

    def symbol(nid: int, rel: str, qualname: str, start: int,
               end: int) -> dict[str, Any]:
        return {
            "id": nid, "type": "Symbol",
            "key": f"sym:{rel}::{qualname}",
            "attrs": {"name": qualname.split(".")[-1], "qualname": qualname,
                      "file": rel, "start_line": start, "end_line": end,
                      "kind": "function"},
        }

    nodes.extend([
        symbol(100, "src/hot.py", "huge", 1, 140),
        symbol(101, "src/app.py", "app", 1, 2),
        symbol(102, "src/uncovered.py", "uncovered", 1, 2),
        symbol(103, "src/orphan.py", "orphan", 1, 2),
        symbol(104, "src/hub.py", "hub", 1, 2),
        {
            "id": 110, "type": "Test",
            "key": "test:tests/test_covered.py::test_covered",
            "attrs": {"name": "test_covered",
                      "qualname": "test_covered",
                      "file": "tests/test_covered.py",
                      "start_line": 3, "end_line": 4},
        },
    ])
    for index, rel in enumerate(("docs/guide.md", "docs/copy.md",
                                 "docs/near.md"), start=120):
        nodes.append({
            "id": index, "type": "Doc", "key": f"doc:{rel}",
            "attrs": {"name": rel, "path": rel, "title": rel},
        })
    commits = []
    for index, offset in enumerate((1, 3, 5, 7), start=200):
        stamp = (date.today() - timedelta(days=offset)).isoformat()
        commits.append({
            "id": index, "type": "Commit", "key": f"commit:{index:040d}",
            "attrs": {"name": f"{index:040d}"[:12],
                      "hash": f"{index:040d}", "date": stamp,
                      "subject": f"change {index}"},
        })
    nodes.extend(commits)

    edges: list[dict[str, Any]] = []
    for rel, nid in ids.items():
        edges.append({"src": 0, "type": "contains", "tgt": nid})
    edges.extend([
        {"src": ids["tests/test_covered.py"], "type": "has_test", "tgt": 110},
        {"src": 110, "type": "tested_by_file", "tgt": ids["tests/test_covered.py"]},
        {"src": 110, "type": "tests_module", "tgt": ids["src/covered.py"]},
        {"src": ids["src/hot.py"], "type": "defines", "tgt": 100},
        {"src": ids["src/app.py"], "type": "defines", "tgt": 101},
        {"src": ids["src/uncovered.py"], "type": "defines", "tgt": 102},
        {"src": ids["src/orphan.py"], "type": "defines", "tgt": 103},
        {"src": ids["src/hub.py"], "type": "defines", "tgt": 104},
    ])
    for index in range(1, 7):
        edges.append({"src": ids[f"src/importer{index}.py"], "type": "imports",
                      "tgt": ids["src/hub.py"]})
    for commit in commits:
        edges.append({"src": commit["id"], "type": "changes",
                      "tgt": ids["src/hot.py"]})
    graph = {
        "version": 2,
        "nodes": nodes,
        "edges": edges,
        "next_id": 300,
        "build": {"include_docs": "features"},
        "files": table,
        "adapter": {"files": [], "edges": [], "unkeyed": 0},
    }
    _write_graph(repo, graph)
    _write_state(repo)
    return repo


def _write_graph(repo: Path, graph: dict[str, Any]) -> Path:
    path = repo / ".ozagia" / "graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph, indent=2), encoding="utf-8")
    return path


def _write_state(repo: Path) -> None:
    reviews = repo / ".ozagia" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / "packet.json").write_text(json.dumps({
        "schema": "ozagia.code_review.packet.v1",
        "base": "HEAD~1",
        "head": "HEAD",
        "findings": [{
            "id": "CR-001",
            "type": "missing_test",
            "severity": "blocking",
            "file": "src/app.py",
            "line": 1,
            "message": "changed source has no test",
            "evidence": ["no covering test recorded"],
            "graph_keys": ["file:src/app.py"],
            "confidence": "high",
        }],
    }), encoding="utf-8")
    results = repo / ".ozagia" / "tests"
    results.mkdir(parents=True, exist_ok=True)
    (results / "results.json").write_text(json.dumps({
        "version": 2,
        "failing": {"default": {"files": {"tests/test_covered.py": 1}}},
        "flaky": {"tests/test_covered.py::test_flaky": {}},
    }), encoding="utf-8")


def _snapshot_tree(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if rel.startswith(".git/"):
                continue
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# --------------------------------------------------------------------------- #
# categories and report shape
# --------------------------------------------------------------------------- #
def test_scan_reports_every_category(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    report = RS.scan(repo, top=100)
    assert report["status"] == "ok"
    assert report["graph"]["freshness"] == "fresh"
    assert report["advisory"] is True
    assert report["truth_assessed"] is False
    categories = {item["category"] for item in report["opportunities"]}
    assert categories == set(RS.CATEGORY_IDS), (
        categories ^ set(RS.CATEGORY_IDS))
    for item in report["opportunities"]:
        assert item["id"]
        assert item["title"]
        assert item["evidence"]
        assert item["expected_effect"]
        assert item["effort"] in RS.EFFORTS
        assert item["risk"] in RS.RISKS
        assert item["confidence"] in RS.CONFIDENCES
        assert item["proposed_module"]
        assert item["action"]
        assert item["not_assessed"]
        kinds = {entry["kind"] for entry in item["evidence"]}
        assert kinds & {"file", "graph_key", "command", "metric"}
    text = RS.format_report(report)
    assert "Repository scan (advisory)" in text
    assert "nothing was built, edited, executed, deleted or sent" in text
    assert "missing_tests" not in text or "Missing tests" in text


def test_missing_tests_prioritise_changed_files(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src" / "uncovered.py").write_text(
        "def uncovered():\n    return 99\n", encoding="utf-8")
    report = RS.scan(repo, top=100)
    assert report["graph"]["freshness"] == "unknown"
    missing = [item for item in report["opportunities"]
               if item["category"] == "missing_tests"
               and item["id"] == "missing-tests:src/uncovered.py"]
    assert missing
    evidence = missing[0]["evidence"]
    assert any(entry.get("kind") == "command"
               and entry.get("command") == "git status --porcelain"
               for entry in evidence)


# --------------------------------------------------------------------------- #
# graph states
# --------------------------------------------------------------------------- #
def test_missing_graph_is_unavailable_but_exit_zero(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "missing")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def app():\n    return 1\n",
                                        encoding="utf-8")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_bytes(b"x" * 32)
    _commit(repo, "no graph")
    graph_path = repo / ".ozagia" / "graph.json"
    before = graph_path.exists()
    report = RS.scan(repo, top=100)
    assert report["status"] == "unavailable"
    assert report["graph"]["available"] is False
    assert report["graph"]["freshness"] == "missing"
    assert "no graph snapshot" in report["graph"]["reason"]
    assert not graph_path.exists()
    assert graph_path.exists() == before
    categories = {item["category"] for item in report["opportunities"]}
    assert "missing_tests" not in categories
    assert "hotspots" not in categories
    assert "generated_clutter" in categories
    assert any("graph-derived" in item for item in report["not_assessed"])
    assert CLI.main(["scan", "--root", str(repo), "--json"]) == 0


def test_stale_and_unknown_graphs_are_degraded(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = repo / ".ozagia" / "graph.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["nodes"][0]["attrs"]["head"] = "0" * 40
    path.write_text(json.dumps(raw), encoding="utf-8")
    report = RS.scan(repo, top=20)
    assert report["status"] == "degraded"
    assert report["graph"]["freshness"] == "stale"
    assert report["graph"]["usable"] is False

    raw["nodes"][0]["attrs"].pop("head")
    path.write_text(json.dumps(raw), encoding="utf-8")
    report = RS.scan(repo, top=20)
    assert report["status"] == "degraded"
    assert report["graph"]["freshness"] == "unknown"


def test_empty_graph_is_degraded_and_does_not_crash(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "empty")
    head = _commit(repo, "empty")
    _write_graph(repo, {
        "version": 2,
        "nodes": [{"id": 0, "type": "Repo", "key": "repo:",
                   "attrs": {"root": str(repo), "head": head}}],
        "edges": [],
        "next_id": 1,
        "files": {},
        "adapter": {},
    })
    report = RS.scan(repo, top=10)
    assert report["status"] == "degraded"
    assert report["graph"]["empty"] is True
    assert report["opportunities"] == []
    assert "empty" in RS.format_report(report)
    assert CLI.main(["scan", "--root", str(repo)]) == 0


def test_unreadable_graph_is_degraded(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "broken")
    _commit(repo, "broken")
    path = repo / ".ozagia" / "graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    report = RS.scan(repo, top=5)
    assert report["status"] == "degraded"
    assert report["graph"]["freshness"] == "error"
    assert "cannot read graph snapshot" in report["graph"]["reason"]
    assert report["truth_assessed"] is False
    assert CLI.main(["scan", "--root", str(repo), "--json"]) == 0


# --------------------------------------------------------------------------- #
# caps and determinism
# --------------------------------------------------------------------------- #
def test_caps_report_omitted_counts(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    full = RS.scan(repo, top=100, per_category=10)
    assert len(full["opportunities"]) > 3

    capped = RS.scan(repo, top=3, per_category=10)
    assert len(capped["opportunities"]) == 3
    assert capped["omitted"]["total"] == full["summary"]["candidates"] - 3
    assert sum(capped["omitted"]["by_category"].values()) == (
        capped["omitted"]["total"])
    assert [item["rank"] for item in capped["opportunities"]] == [1, 2, 3]
    scores = [item["score"] for item in capped["opportunities"]]
    assert scores == sorted(scores, reverse=True)

    per_category = RS.scan(repo, top=100, per_category=2)
    counts: dict[str, int] = {}
    for item in per_category["opportunities"]:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    assert counts
    assert max(counts.values()) <= 2
    assert per_category["omitted"]["per_category_cap"] == 2


def test_json_is_deterministic(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    first = RS.report_json(RS.scan(repo, top=50))
    second = RS.report_json(RS.scan(repo, top=50))
    assert first == second
    payload = json.loads(first)
    assert payload["schema"] == RS.SCHEMA
    assert payload["advisory"] is True
    assert payload["truth_assessed"] is False


# --------------------------------------------------------------------------- #
# read-only guarantees and CLI
# --------------------------------------------------------------------------- #
def test_scan_does_not_write_build_or_use_network(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    before = _snapshot_tree(repo)
    graph = repo / ".ozagia" / "graph.json"
    graph_before = graph.read_bytes()
    graph_mtime = graph.stat().st_mtime_ns

    def _no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the scan must not use the network")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)

    real_run = subprocess.run
    commands: list[list[str]] = []

    def _git_only(args: Any, *call_args: Any, **call_kwargs: Any) -> Any:
        command = list(args) if isinstance(args, (list, tuple)) else [str(args)]
        commands.append(command)
        assert command and command[0] == "git", command
        return real_run(args, *call_args, **call_kwargs)

    monkeypatch.setattr(subprocess, "run", _git_only)
    try:
        report = RS.scan(repo, top=40)
    finally:
        monkeypatch.setattr(subprocess, "run", real_run)
    assert commands
    assert all(command[:2] == ["git", "-C"] for command in commands)
    assert report["status"] == "ok"
    assert _snapshot_tree(repo) == before
    assert graph.read_bytes() == graph_before
    assert graph.stat().st_mtime_ns == graph_mtime


def test_cli_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _make_repo(tmp_path)
    code = CLI.main(["scan", "--root", str(repo), "--json", "--top", "15"])
    assert code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema"] == RS.SCHEMA
    assert len(payload["opportunities"]) <= 15

    code = CLI.main(["scan", "--root", str(repo), "--top", "4"])
    assert code == 0
    text = capsys.readouterr().out
    assert "Repository scan (advisory)" in text
    assert "nothing was built" in text

    out = tmp_path / "report.json"
    code = CLI.main(["scan", "--root", str(repo), "--json", "--out",
                     str(out)])
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["schema"] == RS.SCHEMA
    capsys.readouterr()

    code = CLI.main(["list-categories"])
    assert code == 0
    listing = capsys.readouterr().out
    for category in RS.CATEGORY_IDS:
        assert category in listing

    assert CLI.main(["scan", "--root", str(tmp_path / "nope")]) == 2
    assert CLI.main(["scan", "--root", str(repo), "--top", "0"]) == 2
    assert CLI.main(["scan", "--root", str(repo), "--since", "0"]) == 2
    captured = capsys.readouterr()
    assert "error:" in captured.err


# --------------------------------------------------------------------------- #
# optional Ocelot clustering
# --------------------------------------------------------------------------- #
def test_ocelot_section_is_optional_and_reports_unavailable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    report = RS.scan(repo, top=100)
    assert "ocelot" not in report

    def _not_installed(_root: Any = None) -> dict[str, Any]:
        return {
            "state": "not_installed",
            "path": None,
            "version": None,
            "source": None,
            "manifest": None,
            "manifest_path": None,
            "install_root": "/nonexistent/ozagia/ocelot",
            "invalid_installs": [],
            "note": "Ocelot is not installed.",
        }

    monkeypatch.setattr(
        RS.ocelot_code.ocelot_installer, "resolve", _not_installed)
    report = RS.scan(repo, top=100, ocelot=True)
    section = report["ocelot"]
    assert section["requested"] is True
    assert section["available"] is False
    assert section["state"] == "not_installed"
    assert section["engine"] == "exact"
    assert section["truth_assessed"] is False
    assert section["clusters"]
    assert report["summary"]["ocelot_clusters"] == len(section["clusters"])
    selected = {item["id"] for item in report["opportunities"]
                if item["category"] in RS.ocelot_code.SCAN_CATEGORIES}
    clustered = {finding for cluster in section["clusters"]
                 for finding in cluster["findings"]}
    assert clustered == selected
    assert "ocelot: not_installed" in RS.format_report(report)


def test_ocelot_patterns_section_is_optional_and_candidate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    report = RS.scan(repo, top=100)
    assert "ocelot" not in report

    def _not_installed(_root: Any = None) -> dict[str, Any]:
        return {
            "state": "not_installed",
            "path": None,
            "version": None,
            "source": None,
            "manifest": None,
            "manifest_path": None,
            "install_root": "/nonexistent/ozagia/ocelot",
            "invalid_installs": [],
            "note": "Ocelot is not installed.",
        }

    monkeypatch.setattr(
        RS.ocelot_code.ocelot_installer, "resolve", _not_installed)
    report = RS.scan(repo, top=100, ocelot_patterns=True)
    section = report["ocelot"]
    assert section["state"] == "not_requested"
    assert section["clusters"] == []
    patterns = section["patterns"]
    assert patterns["state"] == "not_installed"
    assert patterns["candidate"] is True
    assert patterns["truth_assessed"] is False
    assert patterns["patterns"]
    assert all(cluster["truth_assessed"] is False
               for cluster in patterns["patterns"])
    assert report["summary"]["ocelot_patterns"] == len(patterns["patterns"])
    assert any("candidate clusters" in item
               for item in report["not_assessed"])
    assert "ocelot patterns:" in RS.format_report(report)


def test_cli_ocelot_patterns_flag(tmp_path: Path,
                                  monkeypatch: pytest.MonkeyPatch,
                                  capsys) -> None:
    repo = _make_repo(tmp_path)

    def _not_installed(_root: Any = None) -> dict[str, Any]:
        return {
            "state": "not_installed",
            "path": None,
            "version": None,
            "source": None,
            "manifest": None,
            "manifest_path": None,
            "install_root": "/nonexistent/ozagia/ocelot",
            "invalid_installs": [],
            "note": "Ocelot is not installed.",
        }

    monkeypatch.setattr(
        RS.ocelot_code.ocelot_installer, "resolve", _not_installed)
    code = CLI.main([
        "scan", "--root", str(repo), "--json", "--top", "100",
        "--ocelot-patterns"])
    captured = capsys.readouterr()
    assert code == 0
    report = json.loads(captured.out)
    assert report["ocelot"]["patterns"]["candidate"] is True
    assert report["ocelot"]["truth_assessed"] is False
