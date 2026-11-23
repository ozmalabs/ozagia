"""Migration status report: counts, freshness, bounds and no source writes."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from ozagia.config import clear_config_cache
from ozagia.migration.report import build_report, markdown_report

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "py_rust_migration"


def _copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    clear_config_cache()
    return root


def _hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix():
            hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".ozagia" not in path.parts
    }


def test_report_counts_gaps_divergences_and_candidates(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    before = _hashes(root)
    report = build_report(root)

    assert report["configured"] is True
    assert report["schema"] == "ozagia.migration.status"
    assert report["counts"]["python"]["files"] == 3
    assert report["counts"]["python"]["api"] == 12
    assert report["counts"]["rust"]["api"] == 9
    assert report["counts"]["matched"] == 4
    assert report["counts"]["python_only"] == 8
    assert report["counts"]["rust_only"] == 5
    assert report["status_counts"] == {
        "planned": 7, "in_progress": 1, "ported": 1, "diverged": 2,
        "dropped": 1, "extra": 5,
    }
    gap_keys = {row["python"] for row in report["gaps"]}
    assert "fixturepy.models.ProtocolThing" in gap_keys
    assert report["gaps_count"] == 7
    divergence_keys = {(row["python"], row["rust"])
                       for row in report["divergences"]}
    assert ("fixturepy.models.Widget",
            "fixturecrate::models::Widget") in divergence_keys
    candidates = {(row["python"], row["rust"]): row
                  for row in report["candidates"]}
    assert candidates[("fixturepy.models.flush_cache",
                       "fixturecrate::models::flush_cache")]["accepted"] is False
    assert report["executed"] is False
    assert report["source_modified"] is False
    assert _hashes(root) == before

    status = json.loads(
        (root / ".ozagia" / "migration" / "status.json").read_text())
    assert status["schema"] == "ozagia.migration.status"
    assert status["written"]["json"].endswith("status.json")
    assert (root / ".ozagia" / "migration" / "status.md").is_file()


def test_freshness_unknown_then_fresh_then_stale(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    first = build_report(root)
    assert first["freshness"]["state"] == "unknown"
    second = build_report(root)
    assert second["freshness"] == {
        "state": "fresh",
        "python": "fresh",
        "rust": "fresh",
        "config": "fresh",
        "previous": str(root / ".ozagia" / "migration" / "status.json"),
    }
    path = root / "python" / "fixturepy" / "models.py"
    path.write_text(path.read_text(encoding="utf-8") +
                    "\n# changed\n", encoding="utf-8")
    third = build_report(root)
    assert third["freshness"]["state"] == "stale"
    assert third["freshness"]["python"] == "changed"
    assert third["freshness"]["rust"] == "fresh"


def test_config_change_marks_the_report_stale(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    build_report(root)
    path = root / ".ozagia.toml"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "infer = true", "infer = false"), encoding="utf-8")
    clear_config_cache()
    report = build_report(root)

    assert report["freshness"]["state"] == "stale"
    assert report["freshness"]["config"] == "changed"
    assert report["candidates"] == []


def test_no_record_writes_nothing(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    report = build_report(root, write=False)

    assert report["written"] == {"json": None, "markdown": None}
    assert not (root / ".ozagia").exists()


def test_markdown_is_bounded_and_explicit(tmp_path: Path) -> None:
    root = _copy(tmp_path)
    text = markdown_report(build_report(root, write=False))

    assert "# Python→Rust migration status" in text
    assert "| planned | 7 |" in text
    assert "Inferred candidates (never accepted)" in text
    assert "accepted: false" in text
    assert "## Explicit omissions" in text
    assert "cross-language type equality is not checked" in text
    assert "executed: false; source_modified: false" in text


def test_unconfigured_report_is_explicit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    clear_config_cache()
    report = build_report(root)

    assert report["configured"] is False
    assert "no [migration] section" in report["reason"]
    assert report["status_counts"]["planned"] == 0
    assert report["written"] == {"json": None, "markdown": None}
    assert not (root / ".ozagia").exists()


def test_broken_config_is_reported_not_raised(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ozagia.toml").write_text(
        '[migration]\npython = "python"\nrust = "rust"\n'
        '[[migration.pairs]]\npython = "pkg.A"\nrust = "crate::A"\n'
        'status = "almost"\n', encoding="utf-8")
    (root / "python").mkdir()
    (root / "rust").mkdir()
    clear_config_cache()
    report = build_report(root)

    assert report["configured"] is False
    assert report["reason"].startswith("configuration could not be read")
    assert report["executed"] is False
