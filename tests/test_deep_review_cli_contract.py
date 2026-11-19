"""CLI contracts for ``ozagia deep-review``.

These tests use a real temporary Git repository where the report pipeline is
the subject under test. Stubs are limited to cases that need a controlled
exit status or a profile artifact.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # These report contract tests exercise review composition, not host
    # admission policy. Keep low-disk CI/container mounts from changing the
    # report path under test.
    policy_dir = root / "ozagia"
    policy_dir.mkdir(exist_ok=True)
    (policy_dir / "resources.toml").write_text(
        "enabled = false\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email",
                    "test@example.com"], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"],
                   check=True, capture_output=True, text=True)


def _commit(root: Path, message: str = "initial") -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test",
                    "-c", "user.email=test@example.com", "commit", "-qm",
                    message], check=True, capture_output=True, text=True)


def _profile_report(path: Path) -> Path:
    report = {
        "schema": {"namespace": "ozagia.profile_report", "version": 1},
        "report_hash": "sha256:cli-contract-profile",
        "clock": "monotonic",
        "measurement": {"unit": "microseconds"},
        "hotspots": [{
            "key": "src/app.py::hot", "name": "hot",
            "file": "src/app.py", "line": 2,
            "self": 5_000, "inclusive": 6_000, "share_bp": 2_500,
        }],
        "metrics": {"series_count": 1, "points": 2,
                    "by_kind": {"counter": 2}, "services": ["service"],
                    "mapped": 1},
    }
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_deep_review_cli_exit_zero_writes_real_report(tmp_path, capsys):
    from ozagia.cli import main

    root = tmp_path / "repo"
    _init_repo(root)
    (root / "app.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    _commit(root)
    out = root / ".ozagia" / "reviews" / "zero"

    code = main(["deep-review", "--root", str(root), "--out", str(out),
                 "--budget-s", "10", "--json"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    manifest = json.loads(captured.out)
    assert manifest["schema"] == "ozagia.deep_review.manifest.v1"
    assert manifest["profile"] == "full"
    for name in ("README.md", "findings.json", "manifest.json",
                 "00-method-and-scope.md"):
        assert (out / name).is_file()
    assert manifest["output_hashes"]["findings.json"].startswith("sha256:")


def test_deep_review_cli_exit_one_when_fail_on_matches(tmp_path):
    from ozagia.cli import main

    root = tmp_path / "repo"
    _init_repo(root)
    (root / ".env").write_text("TOKEN=not-a-real-secret\n", encoding="utf-8")
    (root / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _commit(root)
    out = root / ".ozagia" / "reviews" / "one"

    code = main(["deep-review", "--root", str(root), "--out", str(out),
                 "--profile", "security", "--fail-on", "major",
                 "--budget-s", "10"])

    assert code == 1
    findings = json.loads((out / "findings.json").read_text(encoding="utf-8"))
    assert any(item["severity"] in {"blocking", "major"} for item in findings)


def test_deep_review_cli_exit_two_for_invalid_input(tmp_path, capsys):
    from ozagia.cli import main

    missing = tmp_path / "missing"
    assert main(["deep-review", "--root", str(missing),
                 "--out", str(tmp_path / "report")]) == 2
    captured = capsys.readouterr()
    assert "not a directory" in captured.err

    with pytest.raises(SystemExit) as raised:
        main(["deep-review", "--root", str(tmp_path),
              "--profile", "not-a-profile"])
    assert raised.value.code == 2


def test_deep_review_cli_refuses_out_inside_repo_outside_state(tmp_path, capsys):
    from ozagia.cli import main

    out = tmp_path / "reports" / "escape"
    assert main(["deep-review", "--root", str(tmp_path),
                 "--out", str(out)]) == 2
    captured = capsys.readouterr()
    assert "must be under .ozagia/" in captured.err
    assert not out.exists()


def test_deep_review_cli_index_refreshes_analyzer_profile_graph(tmp_path):
    from ozagia.cli import main

    root = tmp_path / "repo"
    _init_repo(root)
    (root / "app.py").write_text(
        "def nested(items):\n"
        "    for x in items:\n"
        "        for y in items:\n"
        "            pass\n", encoding="utf-8")
    _commit(root)
    out = root / ".ozagia" / "reviews" / "indexed"

    code = main(["deep-review", "--root", str(root), "--out", str(out),
                 "--profile", "performance", "--index", "--budget-s", "30",
                 "--json"])

    assert code == 0
    graph = root / ".ozagia" / "graph.json"
    assert graph.is_file()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["profile"] == "performance"
    assert manifest["index"] is True
    assert manifest["graph"]["indexed"]["ok"] is True
    assert "python_ast_static_checks" in manifest["subjects"]["performance"]["checks"]


def test_deep_review_cli_profile_report_and_json(tmp_path, capsys):
    from ozagia.cli import main

    root = tmp_path / "repo"
    _init_repo(root)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "def hot():\n    return sum(range(10))\n", encoding="utf-8")
    report = _profile_report(root / "profile.json")
    _commit(root)
    out = root / ".ozagia" / "reviews" / "profile"

    code = main(["deep-review", "--root", str(root), "--out", str(out),
                 "--profile", "performance", "--profile-report", str(report),
                 "--budget-s", "10", "--json"])
    captured = capsys.readouterr()

    assert code == 0, captured.err
    manifest = json.loads(captured.out)
    asserted = manifest["subjects"]["performance"]
    assert str(report.resolve()) in asserted["profile_reports"]
    findings = json.loads((out / "findings.json").read_text(encoding="utf-8"))
    assert any(item["type"] == "measured_hotspot" for item in findings)
