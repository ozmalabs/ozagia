"""Tests for the deterministic, graph-aware code-review harness."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ozagia import code_review_cli as CLI
from ozagia.core import code_review as R
from ozagia.core.memory import _tree_digest


HINT_RETRY = '''import time


def _retry(fn):
    delay = 1
    for attempt in range(3):
        try:
            return fn()
        except Exception:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("failed")
'''

HINT_IMPORTED = '''import time
import tenacity


def _retry(fn):
    delay = 1
    for attempt in range(3):
        try:
            return fn()
        except Exception:
            time.sleep(delay)
            delay *= 2
'''

HINT_MIXED = '''import sys
import time


def _retry(fn):
    delay = 1
    for attempt in range(3):
        try:
            return fn()
        except Exception:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("failed")


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: app [--verbose]")
        return 2
    if args[0].startswith("--"):
        return 1
    return 0
'''


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
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
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=env, check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {proc.stderr or proc.stdout}")
    return proc


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Review Tester")
    _git(path, "config", "user.email", "review@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    (path / ".gitignore").write_text(".ozagia/\n", encoding="utf-8")
    (path / "ozagia").mkdir()
    (path / "ozagia" / "resources.toml").write_text(
        "enabled = false\n", encoding="utf-8")
    return path


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def review_repo(tmp_path: Path) -> dict[str, Any]:
    repo = _init_repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "assets").mkdir()
    (repo / "data").mkdir()
    (repo / "src" / "app.py").write_text(
        "def f(x):\n"
        "    return x + 1\n",
        encoding="utf-8",
    )
    (repo / "src" / "user.py").write_text(
        "from app import f\n"
        "\n"
        "def g():\n"
        "    return f(1)\n",
        encoding="utf-8",
    )
    (repo / "src" / "legacy.py").write_text("legacy = True\n", encoding="utf-8")
    (repo / "tests" / "test_app.py").write_text(
        "from app import f\n"
        "\n"
        "def test_f():\n"
        "    assert f(1) == 2\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# fixture\n", encoding="utf-8")
    base = _commit(repo, "base")

    (repo / "src" / "app.py").write_text(
        "def f(x):\n"
        "    return x + 2\n"
        "\n"
        "def h():\n"
        "    return f(2)\n",
        encoding="utf-8",
    )
    (repo / "src" / "newmod.py").write_text(
        "def brand_new(x):\n"
        "    return x * 3\n",
        encoding="utf-8",
    )
    (repo / "src" / "conflict.py").write_text(
        "<<<<<<< HEAD\n"
        "x = 1\n"
        "=======\n"
        "y = 2\n"
        ">>>>>>> other\n",
        encoding="utf-8",
    )
    (repo / "src" / "space.py").write_text(
        "def broken():\n"
        "    return 1   \n",
        encoding="utf-8",
    )
    (repo / "src" / "auth.py").write_text(
        "def login(token):\n"
        "    return token\n",
        encoding="utf-8",
    )
    (repo / "package-lock.json").write_text(
        '{"lockfileVersion": 3}\n', encoding="utf-8")
    (repo / "src" / "auto.py").write_text(
        "# Code generated. DO NOT EDIT.\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (repo / "assets" / "logo.bin").write_bytes(b"\x00\x01\x02binary\n")
    (repo / "data" / "blob.dat").write_bytes(b"x" * 2048)
    (repo / "src" / "legacy.py").unlink()
    head = _commit(repo, "head")
    return {"repo": repo, "base": base, "head": head,
            "range": f"{base}..{head}"}


def _graph_payload(repo: Path, head: str, *,
                   snapshot_head: str | None = None) -> dict[str, Any]:
    graph_path = repo / ".ozagia" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    digest, error = _tree_digest(repo, dump=graph_path)
    assert error is None
    assert digest
    nodes = [
        {"id": 0, "type": "Repo", "key": "repo:",
         "attrs": {"root": str(repo.resolve()),
                   "head": snapshot_head or head,
                   "tree_digest": digest}},
        {"id": 1, "type": "File", "key": "file:src/app.py",
         "attrs": {"path": "src/app.py"}},
        {"id": 2, "type": "Symbol", "key": "sym:src/app.py::f",
         "attrs": {"file": "src/app.py", "qualname": "f", "name": "f",
                   "start_line": 1, "end_line": 2}},
        {"id": 3, "type": "Symbol", "key": "sym:src/app.py::h",
         "attrs": {"file": "src/app.py", "qualname": "h", "name": "h",
                   "start_line": 4, "end_line": 5}},
        {"id": 4, "type": "File", "key": "file:src/user.py",
         "attrs": {"path": "src/user.py"}},
        {"id": 5, "type": "Symbol", "key": "sym:src/user.py::g",
         "attrs": {"file": "src/user.py", "qualname": "g", "name": "g",
                   "start_line": 3, "end_line": 4}},
        {"id": 6, "type": "File", "key": "file:tests/test_app.py",
         "attrs": {"path": "tests/test_app.py"}},
        {"id": 7, "type": "Test", "key": "test:tests/test_app.py::test_f",
         "attrs": {"file": "tests/test_app.py", "qualname": "test_f",
                   "start_line": 3, "end_line": 4}},
        {"id": 8, "type": "File", "key": "file:src/newmod.py",
         "attrs": {"path": "src/newmod.py"}},
        {"id": 9, "type": "Symbol", "key": "sym:src/newmod.py::brand_new",
         "attrs": {"file": "src/newmod.py", "qualname": "brand_new",
                   "name": "brand_new", "start_line": 1, "end_line": 2}},
    ]
    edges = [
        {"src": 1, "type": "defines", "tgt": 2},
        {"src": 1, "type": "defines", "tgt": 3},
        {"src": 4, "type": "defines", "tgt": 5},
        {"src": 5, "type": "calls", "tgt": 2},
        {"src": 6, "type": "defines", "tgt": 7},
        {"src": 6, "type": "has_test", "tgt": 7},
        {"src": 7, "type": "tests_module", "tgt": 1},
        {"src": 8, "type": "defines", "tgt": 9},
    ]
    return {
        "version": 2,
        "nodes": nodes,
        "edges": edges,
        "next_id": 10,
        "build": {},
        "files": {},
    }


def _write_graph(repo: Path, head: str, *,
                 snapshot_head: str | None = None) -> Path:
    path = repo / ".ozagia" / "graph.json"
    path.write_text(
        json.dumps(_graph_payload(repo, head, snapshot_head=snapshot_head)),
        encoding="utf-8")
    return path


def _file(packet: dict[str, Any], path: str) -> dict[str, Any]:
    return next(item for item in packet["files"] if item["path"] == path)


def _findings(packet: dict[str, Any], *, ftype: str | None = None,
              file: str | None = None) -> list[dict[str, Any]]:
    out = []
    for item in packet["findings"]:
        if ftype is not None and item["type"] != ftype:
            continue
        if file is not None and item["file"] != file:
            continue
        out.append(item)
    return out


def _hint_repo(tmp_path: Path, text: str = HINT_RETRY) -> dict[str, Any]:
    repo = _init_repo(tmp_path / "hint-repo")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text(
        "def placeholder():\n    return 0\n", encoding="utf-8")
    base = _commit(repo, "base")
    (repo / "src" / "app.py").write_text(text, encoding="utf-8")
    head = _commit(repo, "hint change")
    return {"repo": repo, "base": base, "head": head,
            "range": f"{base}..{head}"}


# --------------------------------------------------------------------------- #
# no graph: file-level diff checks
# --------------------------------------------------------------------------- #


def test_packet_without_graph_uses_file_level_checks(review_repo):
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)

    assert packet["schema"] == R.PACKET_SCHEMA
    assert packet["base_commit"] == review_repo["base"]
    assert packet["head_commit"] == review_repo["head"]
    assert packet["truth_assessed"] is False
    assert packet["model_review"]["status"] == "not_run"
    assert packet["preflight"]["ok"] is True
    assert packet["graph"]["freshness"] == "missing"
    assert packet["graph"]["usable"] is False
    assert packet["worktree"]["path"] == str(review_repo["repo"].resolve())

    assert _findings(packet, ftype="breaking_change", file="src/legacy.py")
    assert _findings(packet, ftype="unhandled_error", file="src/conflict.py")
    assert _findings(packet, ftype="unknown", file="assets/logo.bin")
    assert _findings(packet, ftype="scope_creep", file="package-lock.json")
    assert _findings(packet, ftype="scope_creep", file="src/auto.py")
    assert _findings(packet, ftype="security_sensitive", file="src/auth.py")
    assert _findings(packet, ftype="performance_risk", file="data/blob.dat")
    assert _findings(packet, ftype="missing_test", file="src/newmod.py")
    # The unchanged test file tests/test_app.py is a file-level match.
    assert not _findings(packet, ftype="missing_test", file="src/app.py")
    whitespace = _findings(packet, ftype="unknown", file="src/space.py")
    assert any("whitespace" in item["message"] for item in whitespace)

    conflict = _findings(packet, ftype="unhandled_error", file="src/conflict.py")
    assert conflict[0]["severity"] == "blocking"
    assert conflict[0]["line"] == 1
    assert all(item["graph_keys"] == [] for item in packet["findings"])
    assert packet["omissions"]

    legacy = _file(packet, "src/legacy.py")
    assert legacy["deleted"] is True
    assert _file(packet, "assets/logo.bin")["binary"] is True
    assert _file(packet, "package-lock.json")["generated"] is True
    assert _file(packet, "src/auto.py")["generated"] is True
    assert _file(packet, "data/blob.dat")["large"] is True

    for item in packet["findings"]:
        required = {"id", "type", "severity", "file", "line", "message",
                    "evidence", "graph_keys", "confidence",
                    "confidence_note"}
        assert required <= set(item)
        assert item["type"] in R.FINDING_TYPES
        assert item["severity"] in R.SEVERITIES

    assert R.exit_code(packet) == 1
    assert R.exit_code(packet, "never") == 0
    assert R.exit_code(packet, "major") == 1


# --------------------------------------------------------------------------- #
# graph present
# --------------------------------------------------------------------------- #


def test_fresh_graph_maps_symbols_callers_and_tests(review_repo):
    _write_graph(review_repo["repo"], review_repo["head"])
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)

    graph = packet["graph"]
    assert graph["freshness"] == "fresh"
    assert graph["usable"] is True
    assert graph["call_edges"] == 1
    assert graph["snapshot_head"] == review_repo["head"]

    app = _file(packet, "src/app.py")
    assert "file:src/app.py" in app["graph_keys"]
    assert any(symbol["key"] == "sym:src/app.py::f"
               for symbol in app["symbols"])
    assert any(test["key"] == "test:tests/test_app.py::test_f"
               for test in app["covering_tests"])
    assert any(test["source"] == "graph" for test in app["covering_tests"])

    stale = _findings(packet, ftype="stale_reference", file="src/app.py")
    assert len(stale) == 1
    assert "sym:src/app.py::f" in stale[0]["graph_keys"]
    assert "sym:src/user.py::g" in stale[0]["graph_keys"]
    assert stale[0]["confidence"] == "medium"
    assert "src/user.py" in " ".join(stale[0]["evidence"])

    missing = _findings(packet, ftype="missing_test", file="src/newmod.py")
    assert len(missing) == 1
    assert "file:src/newmod.py" in missing[0]["graph_keys"]
    assert "sym:src/newmod.py::brand_new" in missing[0]["graph_keys"]

    assert not _findings(packet, ftype="missing_test", file="src/app.py")
    assert packet["truth_assessed"] is False


def test_packet_includes_a_test_plan_without_running_tests(review_repo):
    _write_graph(review_repo["repo"], review_repo["head"])
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)

    plan = packet["test_plan"]
    assert plan["executed"] is False
    assert plan["truth_assessed"] is False
    assert "does not execute tests" in plan["not_run_reason"]
    assert packet["tests_executed"] is False
    assert "tests/test_app.py" in plan["selected_files"]
    assert any(entry.get("area") == "tests"
               for entry in packet["not_assessed"])
    assert "test plan:" in R.format_packet(packet)


def test_testing_task_override_can_select_none_for_review(review_repo):
    _write_graph(review_repo["repo"], review_repo["head"])
    (review_repo["repo"] / "ozagia" / "config.toml").write_text(
        "[testing.tasks]\n"
        'code_review = "none"\n',
        encoding="utf-8",
    )
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)
    assert packet["test_plan"]["mode"] == "none"
    assert packet["test_plan"]["selected_files"] == []
    assert packet["tests_executed"] is False


def test_packet_includes_bounded_library_hints(tmp_path):
    repo = _hint_repo(tmp_path)
    packet = R.review_range(repo["repo"], repo["range"])

    section = packet["library_hints"]
    assert section["requested"] is True
    assert section["state"] == "ready"
    assert section["recorded"] is False
    assert section["truth_assessed"] is False
    assert section["counts"]["hints"] >= 1
    hint = next(item for item in section["hints"]
                if item["capability"] == "retry")
    assert hint["file"] == "src/app.py"
    assert hint["symbol"] == "_retry"
    assert hint["package"]
    assert hint["availability"] in ("stdlib", "declared", "installed", "known")
    assert hint["confidence"] in ("low", "medium", "high")
    assert "evidence" in hint and "time.sleep" not in hint["evidence"]
    # Evidence and the packet never carry file contents.
    raw = json.dumps(packet, sort_keys=True)
    assert "time.sleep" not in raw
    assert "for attempt in range" not in raw
    assert packet["summary"]["library_hints"] >= 1
    assert packet["summary"]["library_hints_state"] == "ready"
    assert any(entry.get("area") == "library-hints"
               for entry in packet["not_assessed"])
    text = R.format_packet(packet)
    assert "library hints: ready" in text
    assert "src/app.py::_retry" in text


def test_library_hint_cap_and_off_setting(tmp_path):
    repo = _hint_repo(tmp_path, HINT_MIXED)
    (repo["repo"] / "ozagia" / "config.toml").write_text(
        "[hints]\n"
        'library = "gentle"\n'
        "max_per_edit = 1\n",
        encoding="utf-8",
    )
    packet = R.review_range(repo["repo"], repo["range"])
    section = packet["library_hints"]
    assert len(section["hints"]) == 1
    assert section["hints"][0]["capability"] == "retry"
    assert section["counts"]["suppressed"] >= 1

    (repo["repo"] / "ozagia" / "config.toml").write_text(
        "[hints]\n"
        'library = "off"\n',
        encoding="utf-8",
    )
    packet = R.review_range(repo["repo"], repo["range"])
    section = packet["library_hints"]
    assert section["state"] == "off"
    assert section["hints"] == []
    assert section["counts"]["hints"] == 0
    assert "not checked" in R.format_packet(packet)


def test_library_hints_are_suppressed_when_the_package_is_imported(tmp_path):
    repo = _hint_repo(tmp_path, HINT_IMPORTED)
    packet = R.review_range(repo["repo"], repo["range"])
    section = packet["library_hints"]
    assert section["hints"] == []
    assert section["state"] == "no_hints"
    assert section["counts"]["suppressed"] >= 1


def test_cli_can_disable_library_hints(tmp_path, capsys):
    repo = _hint_repo(tmp_path)
    code = CLI.main([
        "--root", str(repo["repo"]), "--range", repo["range"],
        "--json", "--fail-on", "never", "--no-library-hints",
    ])
    assert code == 0
    packet = json.loads(capsys.readouterr().out)
    assert packet["library_hints"]["state"] == "disabled"
    assert packet["library_hints"]["hints"] == []


def test_stale_graph_is_reported_and_not_used(review_repo):
    _write_graph(review_repo["repo"], review_repo["head"],
                 snapshot_head=review_repo["base"])
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)

    graph = packet["graph"]
    assert graph["freshness"] == "stale"
    assert graph["usable"] is False
    assert "does not match" in graph["reason"]

    stale = _findings(packet, ftype="stale_reference", file=None)
    assert len(stale) == 1
    assert "stale" in stale[0]["message"]
    assert not _findings(packet, ftype="stale_reference", file="src/app.py")
    assert all(item["graph_keys"] == []
               for item in _findings(packet, ftype="missing_test"))
    assert any("Graph evidence was not used" in line
               for line in packet["omissions"])


def test_missing_graph_path_is_an_error(review_repo):
    with pytest.raises(R.CodeReviewError):
        R.review_range(
            review_repo["repo"], review_repo["range"],
            graph_path=review_repo["repo"] / "missing.json")


def test_patch_parser_keeps_added_plus_lines_in_the_hunk():
    text = (
        "diff --git a/src/plus.py b/src/plus.py\n"
        "--- /dev/null\n"
        "+++ b/src/plus.py\n"
        "@@ -0,0 +1,3 @@\n"
        "++ plus content\n"
        "+eval(value)\n"
        "+line\n"
    )
    parsed = R._parse_patch(text)
    assert parsed["first_added"] == {"src/plus.py": 1}
    assert parsed["dangerous"] == [
        {"file": "src/plus.py", "line": 2, "label": "dynamic eval"}]


# --------------------------------------------------------------------------- #
# preflight refusals and input errors
# --------------------------------------------------------------------------- #


def test_range_and_root_errors(review_repo):
    for spec in ("HEAD", "a...b", "", "..HEAD", "HEAD.."):
        with pytest.raises(R.CodeReviewError):
            R.parse_range(spec)
    base, head = R.parse_range(f"{review_repo['base']}..HEAD")
    assert base == review_repo["base"]
    assert head == "HEAD"

    with pytest.raises(R.CodeReviewError):
        R.review_range(review_repo["repo"], "not-a-revision..HEAD")
    with pytest.raises(R.CodeReviewError):
        R.review_range(review_repo["repo"] / "missing", "HEAD~1..HEAD")
    with pytest.raises(R.CodeReviewError):
        R.review_range(review_repo["repo"] / "src", "HEAD~1..HEAD")
    with pytest.raises(R.PreflightError):
        R.review_diff(
            review_repo["repo"], base=review_repo["head"],
            head=review_repo["base"])


def test_wrong_branch_refuses(review_repo):
    (review_repo["repo"] / "ozagia" / "worktrees.toml").write_text(
        "[worktrees.review]\n"
        'path = "."\n'
        'branch = "feat/other"\n',
        encoding="utf-8",
    )
    with pytest.raises(R.PreflightError) as caught:
        R.review_range(review_repo["repo"], review_repo["range"])
    report = caught.value.report
    assert report["refused"] is True
    assert report["reason"] == "worktree.expected_branch"
    assert report["truth_assessed"] is False
    assert any(
        item["id"] == "worktree.expected_branch" and item["level"] == "error"
        for item in report["preflight"]["checks"])


def test_unresolved_conflicts_refuse(tmp_path):
    repo = _init_repo(tmp_path / "merge")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("line = 0\n", encoding="utf-8")
    base = _commit(repo, "base")
    _git(repo, "checkout", "-q", "-b", "other")
    (repo / "src" / "app.py").write_text("line = 2\n", encoding="utf-8")
    _commit(repo, "other")
    _git(repo, "checkout", "-q", "main")
    (repo / "src" / "app.py").write_text("line = 1\n", encoding="utf-8")
    _commit(repo, "main")
    merged = _git(repo, "merge", "other", check=False)
    assert merged.returncode != 0

    with pytest.raises(R.PreflightError) as caught:
        R.review_diff(repo, base=base, head="HEAD")
    checks = caught.value.report["preflight"]["checks"]
    assert any(item["id"] == "worktree.conflicts" and item["level"] == "error"
               for item in checks)
    assert any(item["id"] == "worktree.operation" and item["level"] == "error"
               for item in checks)


def test_resource_shortfall_refuses(review_repo, monkeypatch):
    def refused(*_args, **_kwargs):
        return {
            "ok": False,
            "reason": "insufficient_memory",
            "requested": {"memory_mb": 4096, "disk_mb": 0},
            "shortfall": {"memory_mb": 3000, "disk_mb": 0},
            "suggestions": ["stop job j-1", "retry in about 90 s"],
            "checks": [],
        }

    monkeypatch.setattr(R.resources, "check_admission", refused)
    with pytest.raises(R.PreflightError) as caught:
        R.review_range(review_repo["repo"], review_repo["range"])
    report = caught.value.report
    assert report["reason"] == "resources.admission"
    assert report["resources"]["ok"] is False
    assert report["resources"]["shortfall"]["memory_mb"] == 3000
    assert any(
        item["id"] == "resources.admission" and item["level"] == "error"
        for item in report["preflight"]["checks"])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_json_packet_and_exit_codes(review_repo, capsys):
    repo = review_repo["repo"]
    args = ["--root", str(repo), "--range", review_repo["range"], "--json"]

    code = CLI.main([*args, "--fail-on", "never"])
    captured = capsys.readouterr()
    packet = json.loads(captured.out)
    assert code == 0
    assert packet["schema"] == R.PACKET_SCHEMA
    assert packet["truth_assessed"] is False
    assert packet["findings"]

    code = CLI.main(args)
    capsys.readouterr()
    assert code == 1

    code = CLI.main(["--root", str(repo), "--range", review_repo["range"],
                     "--fail-on", "never"])
    captured = capsys.readouterr()
    assert code == 0
    assert "truth_assessed: false" in captured.out
    assert "findings:" in captured.out


def test_cli_out_is_atomic(review_repo, tmp_path, capsys):
    out = tmp_path / "packet.json"
    code = CLI.main([
        "--root", str(review_repo["repo"]), "--range", review_repo["range"],
        "--json", "--fail-on", "never", "--out", str(out),
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["schema"] == \
        R.PACKET_SCHEMA
    assert captured.out.strip() == out.read_text(encoding="utf-8").strip()
    assert not list(tmp_path.glob(".packet.json.*.tmp"))


def test_cli_usage_and_refusal_errors(review_repo, tmp_path, capsys):
    repo = review_repo["repo"]
    code = CLI.main(["--root", str(repo), "--range", "bad"])
    captured = capsys.readouterr()
    assert code == 2
    assert "error:" in captured.err

    code = CLI.main([
        "--root", str(repo), "--range", review_repo["range"],
        "--graph", str(tmp_path / "missing.json"),
    ])
    capsys.readouterr()
    assert code == 2

    code = CLI.main([
        "--root", str(tmp_path / "not-a-repo"), "--range",
        review_repo["range"],
    ])
    capsys.readouterr()
    assert code == 2

    (repo / "ozagia" / "worktrees.toml").write_text(
        "[worktrees.review]\n"
        'path = "."\n'
        'branch = "feat/other"\n',
        encoding="utf-8",
    )
    code = CLI.main([
        "--root", str(repo), "--range", review_repo["range"], "--json",
    ])
    captured = capsys.readouterr()
    assert code == 2
    refusal = json.loads(captured.out)
    assert refusal["refused"] is True
    assert refusal["reason"] == "worktree.expected_branch"


# --------------------------------------------------------------------------- #
# optional injected review
# --------------------------------------------------------------------------- #


def test_review_does_not_use_network(review_repo, monkeypatch):
    import socket

    def blocked(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked, raising=False)
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)
    assert packet["model_review"]["status"] == "not_run"
    assert packet["truth_assessed"] is False


def test_injected_review_is_unverified(review_repo):
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)
    before = len(packet["findings"])
    seen: list[dict[str, Any]] = []

    def runner(value):
        seen.append(dict(value))
        return [{
            "type": "missing_test",
            "severity": "major",
            "file": "src/newmod.py",
            "line": 1,
            "message": "injected reviewer thinks a test is missing",
            "evidence": ["injected prompt output"],
        }]

    result = R.run_injected_review(packet, runner)
    assert seen and seen[0]["truth_assessed"] is False
    assert len(packet["findings"]) == before
    assert result["truth_assessed"] is False
    assert result["model_review"]["status"] == "injected"
    assert result["model_review"]["findings"] == 1
    model = [item for item in result["findings"]
             if item.get("source") == "model"]
    assert len(model) == 1
    assert model[0]["id"].startswith("CR-")
    assert "unverified" in model[0]["confidence_note"]


def _shared_reviewer_finding() -> dict[str, Any]:
    return {
        "type": "missing_test",
        "severity": "major",
        "file": "src/newmod.py",
        "line": 1,
        "message": "both reviewers think a test is missing",
        "evidence": ["injected prompt output"],
    }


def test_multi_reviewer_agreement_is_annotated_not_truth(review_repo):
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)
    before = json.dumps(packet["findings"], sort_keys=True)

    def alpha(value):
        assert value["truth_assessed"] is False
        return [
            _shared_reviewer_finding(),
            {
                "type": "scope_creep",
                "severity": "minor",
                "file": "src/space.py",
                "message": "alpha-only concern",
            },
        ]

    def beta(value):
        assert value["truth_assessed"] is False
        return [_shared_reviewer_finding()]

    result = R.run_injected_reviews(packet, {"beta": beta, "alpha": alpha})
    review = result["model_review"]

    assert json.dumps(packet["findings"], sort_keys=True) == before
    assert review["status"] == "injected"
    assert review["truth_assessed"] is False
    assert result["truth_assessed"] is False
    assert review["reviewer_count"] == 2
    assert [entry["name"] for entry in review["reviewers"]] == [
        "alpha", "beta"]
    assert [entry["status"] for entry in review["reviewers"]] == ["ok", "ok"]
    assert review["reviewers"][0]["findings"] == 2
    assert review["reviewers"][1]["findings"] == 1
    assert review["findings"] == 2
    assert review["accepted_findings"] == 3
    assert review["duplicate_findings"] == 1
    assert review["agreement"] == 1
    assert review["dissent"] == 1
    assert review["errors"] == []
    assert "not correctness" in review["note"]

    model = [item for item in result["findings"]
             if item.get("source") == "model"]
    assert len(model) == 2
    shared = [item for item in model if item.get("agreement") == 2]
    assert len(shared) == 1
    assert shared[0]["reviewers"] == ["alpha", "beta"]
    assert shared[0]["id"].startswith("CR-")
    dissent = [item for item in model if item.get("agreement") == 1]
    assert len(dissent) == 1
    assert dissent[0]["reviewers"] == ["alpha"]
    assert dissent[0]["reviewer"] == "alpha"
    assert "reviewer" not in shared[0]
    assert review["agreement_detail"] == [{
        "finding": shared[0]["id"],
        "type": "missing_test",
        "file": "src/newmod.py",
        "reviewers": ["alpha", "beta"],
        "count": 2,
    }]
    assert review["dissent_detail"][0]["finding"] == dissent[0]["id"]
    assert review["dissent_detail"][0]["reviewer"] == "alpha"
    assert result["summary"]["findings"] == len(result["findings"])
    assert all(item["message"].strip() for item in result["findings"])

    # Reviewer order must not depend on mapping insertion order.
    again = R.run_injected_reviews(packet, [beta, alpha])
    assert again["findings"] == result["findings"]
    assert again["model_review"] == review


def test_multi_reviewer_errors_are_bounded_and_do_not_crash(review_repo):
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)

    def boom(value):
        raise RuntimeError("provider unavailable " + "x" * 2000)

    def empty(value):
        return [None, "not a finding", {}, {"message": "   "}]

    def invalid(value):
        return "not a sequence of findings"

    result = R.run_injected_reviews(
        packet, {"boom": boom, "empty": empty, "invalid": invalid})
    review = result["model_review"]
    entries = {entry["name"]: entry for entry in review["reviewers"]}

    assert review["status"] == "injected"
    assert review["truth_assessed"] is False
    assert result["truth_assessed"] is False
    assert review["findings"] == 0
    assert review["agreement"] == 0
    assert review["dissent"] == 0
    assert entries["boom"]["status"] == "error"
    assert "RuntimeError" in entries["boom"]["error"]
    assert len(entries["boom"]["error"]) <= R.MAX_REVIEWER_ERROR_CHARS
    assert entries["invalid"]["status"] == "error"
    assert "sequence" in entries["invalid"]["error"]
    assert entries["empty"]["status"] == "ok"
    assert entries["empty"]["findings"] == 0
    assert len(review["errors"]) == 2
    assert all(item["message"].strip() for item in result["findings"])


def test_single_injected_review_keeps_its_shape(review_repo):
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)

    def named_runner(value):
        return [{
            "type": "performance_risk",
            "severity": "minor",
            "file": "src/space.py",
            "message": "single reviewer finding",
        }]

    result = R.run_injected_review(packet, named_runner)
    review = result["model_review"]
    assert review["status"] == "injected"
    assert review["runner"] == "named_runner"
    assert review["reviewer_count"] == 1
    assert review["reviewers"][0]["name"] == "named_runner"
    assert review["reviewers"][0]["finding_ids"] == [
        item["id"] for item in result["findings"]
        if item.get("source") == "model"]
    assert review["findings"] == 1
    assert review["agreement"] == 0
    assert review["dissent"] == 1
    assert result["truth_assessed"] is False


# --------------------------------------------------------------------------- #
# optional Ocelot clustering
# --------------------------------------------------------------------------- #


def test_packet_omits_ocelot_by_default(review_repo):
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)
    assert "ocelot" not in packet


def test_ocelot_unavailable_is_reported_with_exact_fallback(
        review_repo, tmp_path):
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024,
        ocelot=True, ocelot_root=tmp_path / "missing-ocelot")
    section = packet["ocelot"]
    assert section["requested"] is True
    assert section["available"] is False
    assert section["state"] == "unavailable"
    assert section["engine"] == "exact"
    assert section["truth_assessed"] is False
    assert section["clusters"]
    assert packet["findings"]
    assert any("Ocelot" in line for line in packet["omissions"])
    assert "ocelot:" in R.format_packet(packet)


def test_injected_ocelot_clusterer_cannot_claim_truth(review_repo):
    def clusterer(findings, **_kwargs):
        assert findings
        return {
            "available": True,
            "state": "ready",
            "engine": "fake-ocelot",
            "clusters": [{
                "key": {"family": "evidence", "target_type": "File",
                        "target_id": "src/newmod.py"},
                "findings": ["CR-001"],
                "size": 1,
                "origin": "exact+ocelot",
                "analogy_count": 0,
            }],
            "analogies": [],
            "truth_assessed": True,
        }

    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024,
        ocelot=clusterer)
    section = packet["ocelot"]
    assert section["engine"] == "fake-ocelot"
    assert section["truth_assessed"] is False
    assert packet["truth_assessed"] is False


def test_cli_ocelot_flags(review_repo, capsys):
    code = CLI.main([
        "--root", str(review_repo["repo"]), "--range", review_repo["range"],
        "--json", "--fail-on", "never", "--ocelot",
        "--ocelot-root", str(review_repo["repo"] / "missing-ocelot"),
        "--ocelot-mode", "inprocess",
    ])
    captured = capsys.readouterr()
    assert code == 0
    packet = json.loads(captured.out)
    assert packet["ocelot"]["state"] == "unavailable"
    assert packet["ocelot"]["mode"] == "inprocess"
    assert packet["truth_assessed"] is False


def test_ocelot_impact_and_patterns_sections_are_optional(review_repo,
                                                          tmp_path):
    missing = tmp_path / "missing-ocelot"
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024,
        ocelot_impact=True, ocelot_patterns=True, ocelot_root=missing)
    section = packet["ocelot"]
    assert section["state"] == "not_requested"
    assert section["clusters"] == []
    impact = section["impact"]
    assert impact["state"] == "unavailable"
    assert impact["truth_assessed"] is False
    assert impact["test_set_complete"] is False
    assert impact["clusters"]
    assert impact["clusters"][0]["truth_assessed"] is False
    patterns = section["patterns"]
    assert patterns["state"] == "unavailable"
    assert patterns["candidate"] is True
    assert patterns["truth_assessed"] is False
    assert patterns["patterns"]
    assert packet["summary"]["ocelot_impact_clusters"] == len(
        impact["clusters"])
    assert packet["summary"]["ocelot_patterns"] == len(patterns["patterns"])
    assert any("impact" in line for line in packet["omissions"])
    assert any("pattern" in line for line in packet["omissions"])
    text = R.format_packet(packet)
    assert "ocelot impact:" in text
    assert "ocelot patterns:" in text
    assert packet["truth_assessed"] is False


def test_injected_impact_and_pattern_clusterers_cannot_claim_truth(
        review_repo):
    def impact_clusterer(shapes, **_kwargs):
        assert shapes
        return {
            "available": True,
            "state": "ready",
            "engine": "fake-impact",
            "analogies": [],
            "truth_assessed": True,
        }

    def pattern_clusterer(shapes, **_kwargs):
        assert shapes
        return {
            "available": True,
            "state": "ready",
            "engine": "fake-patterns",
            "clusters": [],
            "truth_assessed": True,
        }

    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024,
        ocelot_impact=impact_clusterer,
        ocelot_patterns=pattern_clusterer)
    section = packet["ocelot"]
    assert section["impact"]["engine"] == "fake-impact"
    assert section["patterns"]["engine"] == "fake-patterns"
    assert section["impact"]["truth_assessed"] is False
    assert section["patterns"]["truth_assessed"] is False
    assert packet["truth_assessed"] is False


def test_cli_ocelot_impact_and_pattern_flags(review_repo, capsys):
    code = CLI.main([
        "--root", str(review_repo["repo"]), "--range", review_repo["range"],
        "--json", "--fail-on", "never", "--ocelot-impact",
        "--ocelot-patterns",
        "--ocelot-root", str(review_repo["repo"] / "missing-ocelot"),
    ])
    captured = capsys.readouterr()
    assert code == 0
    packet = json.loads(captured.out)
    assert "impact" in packet["ocelot"]
    assert "patterns" in packet["ocelot"]
    assert packet["ocelot"]["truth_assessed"] is False


# --------------------------------------------------------------------------- #
# optional injected test runner
# --------------------------------------------------------------------------- #


def _test_packet(review_repo) -> dict[str, Any]:
    packet = R.review_range(
        review_repo["repo"], review_repo["range"], max_file_bytes=1024)
    packet["findings"] = []
    packet["summary"] = {}
    return packet


def test_injected_test_runner_records_a_success(
        review_repo):
    packet = _test_packet(review_repo)
    original = json.dumps(packet, sort_keys=True)
    selected = list(packet["test_plan"]["selected_files"])
    assert "tests/test_app.py" in selected

    def runner(plan):
        assert plan["executed"] is False
        assert plan["selected_files"] == selected
        plan["selected_files"] = ["mutated/inside/the/copy.py"]
        plan["executed"] = True
        return {
            "exit_code": 0,
            "passed": 4,
            "failed": 0,
            "skipped": 2,
            "errors": 1,
            "output": "4 passed, 2 skipped\n",
            "duration_s": 1.25,
            "unknown_key": "dropped",
        }

    result = R.run_injected_tests(packet, runner)

    assert json.dumps(packet, sort_keys=True) == original
    assert packet["tests_executed"] is False
    assert result["tests_executed"] is True
    assert result["test_plan"]["executed"] is True
    assert result["test_plan"]["selected_files"] == selected
    assert not result["test_plan"].get("not_run_reason")
    execution = result["test_execution"]
    assert execution["status"] == "injected"
    assert execution["source"] == "runner"
    assert execution["verified"] is False
    assert execution["selected"] == selected
    assert execution["exit_code"] == 0
    assert execution["counts"] == {
        "passed": 4, "failed": 0, "skipped": 2, "errors": 1}
    assert execution["output_excerpt"] == "4 passed, 2 skipped\n"
    assert execution["output_truncated"] is False
    assert execution["duration_s"] == 1.25
    assert "unknown_key" not in execution
    assert "unverified" in execution["note"]
    assert result["truth_assessed"] is False
    assert not [item for item in result["findings"]
                if item.get("type") == "test_failure"]
    assert R.exit_code(result) == 0
    reasons = [entry["reason"] for entry in result["not_assessed"]
               if entry.get("area") == "tests"]
    assert len(reasons) == 1
    assert "unverified" in reasons[0]
    assert "does not prove correctness" in reasons[0]
    text = R.format_packet(result)
    assert "tests were run by an injected runner (unverified)" in text
    assert "test execution: injected by runner" in text


def test_injected_test_failure_adds_a_blocking_finding(review_repo):
    packet = _test_packet(review_repo)

    def runner(plan):
        return {
            "exit_code": 2,
            "passed": 1,
            "failed": 3,
            "skipped": 0,
        }

    result = R.run_injected_tests(packet, runner)
    failures = [item for item in result["findings"]
                if item["type"] == "test_failure"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["severity"] == "blocking"
    assert failure["evidence"] == ["test_plan"]
    assert failure["confidence"] == "low"
    assert failure["confidence_note"] == (
        "injected test runner; not independently verified")
    assert "3" in failure["message"]
    assert "2" in failure["message"]
    assert failure["type"] in R.FINDING_TYPES
    assert R.exit_code(result) == 1
    assert result["summary"]["blocking"] == 1
    assert result["test_execution"]["exit_code"] == 2


def test_injected_test_runner_skips_without_a_runnable_plan(review_repo,
                                                           monkeypatch):
    packet = _test_packet(review_repo)
    calls: list[Any] = []

    def runner(plan):
        calls.append(plan)
        return {"exit_code": 0, "passed": 0, "failed": 0, "skipped": 0}

    no_plan = R.run_injected_tests(
        {**packet, "test_plan": None}, runner)
    assert no_plan["test_execution"]["status"] == "skipped"
    assert no_plan["test_execution"]["reason"] == (
        "no test plan in the packet; no test was run")
    assert no_plan["tests_executed"] is False

    empty_plan = R.run_injected_tests({
        **packet,
        "test_plan": {
            "selected_files": [],
            "refused": False,
            "not_run_reason": "nothing was selected for this change",
        },
    }, runner)
    assert empty_plan["test_execution"]["status"] == "skipped"
    assert empty_plan["test_execution"]["reason"] == (
        "nothing was selected for this change")
    assert empty_plan["test_plan"]["selected_files"] == []
    assert empty_plan["tests_executed"] is False

    refused_plan = R.run_injected_tests({
        **packet,
        "test_plan": {
            "selected_files": ["tests/test_app.py"],
            "refused": True,
            "not_run_reason": "the test policy refused to select tests",
        },
    }, runner)
    assert refused_plan["test_execution"]["status"] == "refused"
    assert refused_plan["test_execution"]["reason"] == (
        "the test policy refused to select tests")
    assert refused_plan["tests_executed"] is False
    assert calls == []


@pytest.mark.parametrize("bad", [
    "not a mapping",
    {"exit_code": 1},
    {"exit_code": 256, "passed": 0, "failed": 0, "skipped": 0},
    {"exit_code": True, "passed": 0, "failed": 0, "skipped": 0},
    {"exit_code": -1, "passed": 0, "failed": 0, "skipped": 0},
    {"exit_code": 0, "passed": -1, "failed": 0, "skipped": 0},
    {"exit_code": 0, "passed": 0, "failed": 0},
    {"exit_code": 0, "passed": 0, "failed": 0, "skipped": 0,
     "errors": "one"},
    {"exit_code": 0, "passed": 0, "failed": 0, "skipped": 0,
     "output": 3},
    {"exit_code": 0, "passed": 0, "failed": 0, "skipped": 0,
     "duration_s": "fast"},
])
def test_injected_test_runner_rejects_malformed_results(review_repo, bad):
    packet = _test_packet(review_repo)
    result = R.run_injected_tests(packet, lambda plan: bad)

    execution = result["test_execution"]
    assert execution["status"] == "error"
    assert execution["error"]
    assert execution["verified"] is False
    assert result["tests_executed"] is False
    assert result["test_plan"]["executed"] is False
    assert result["truth_assessed"] is False
    assert any(entry.get("area") == "tests"
               for entry in result["not_assessed"])


def test_injected_test_runner_catches_runner_exceptions(review_repo):
    packet = _test_packet(review_repo)

    def runner(plan):
        raise ValueError("the fake runner broke")

    result = R.run_injected_tests(packet, runner)
    execution = result["test_execution"]
    assert execution["status"] == "error"
    assert "ValueError" in execution["error"]
    assert "the fake runner broke" in execution["error"]
    assert result["tests_executed"] is False
    assert R.exit_code(result) == 0


def test_injected_test_output_is_capped(review_repo):
    packet = _test_packet(review_repo)

    def runner(plan):
        return {
            "exit_code": 0,
            "passed": 1,
            "failed": 0,
            "skipped": 0,
            "output": "line\n" * 5000 + "x" * 20000,
        }

    result = R.run_injected_tests(packet, runner)
    execution = result["test_execution"]
    assert execution["output_truncated"] is True
    assert len(execution["output_excerpt"].splitlines()) <= (
        R.MAX_TEST_OUTPUT_LINES)
    assert len(execution["output_excerpt"].encode("utf-8")) <= (
        R.MAX_TEST_OUTPUT_BYTES)


def test_injected_test_runner_uses_no_process_or_network(
        review_repo, monkeypatch):
    import socket

    packet = _test_packet(review_repo)

    def blocked(*_args, **_kwargs):
        raise AssertionError("process or network access attempted")

    monkeypatch.setattr(R.subprocess, "run", blocked)
    monkeypatch.setattr(R.os, "system", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked, raising=False)

    def runner(plan):
        return {"exit_code": 0, "passed": 1, "failed": 0, "skipped": 0,
                "output": "ok"}

    result = R.run_injected_tests(packet, runner)
    assert result["test_execution"]["status"] == "injected"
