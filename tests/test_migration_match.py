"""Migration matching: explicit pairs, inference, flags and overrides."""
from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

from ozagia.config import (
    MigrationConfig,
    MigrationPair,
    clear_config_cache,
    load_project_config,
)
from ozagia.migration.inventory import inventory_python, inventory_rust
from ozagia.migration.match import match

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "py_rust_migration"


def _fixture(tmp_path: Path) -> tuple[Path, MigrationConfig]:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURE, root)
    clear_config_cache()
    return root, load_project_config(root).migration


def _inventories(root: Path, config: MigrationConfig):
    return (inventory_python(root, config), inventory_rust(root, config))


def _row(result, python: str):
    found = [row for row in result.rows if row.python == python]
    assert len(found) == 1, [row.python for row in result.rows]
    return found[0]


def test_explicit_pairs_win_and_statuses_derive(tmp_path: Path) -> None:
    root, config = _fixture(tmp_path)
    py, rs = _inventories(root, config)
    result = match(py, rs, config)

    assert result.inferred is True
    assert result.unresolved_pairs == ()
    user = _row(result, "fixturepy.models.User")
    assert (user.rust, user.status, user.source, user.manual) == (
        "fixturecrate::models::User", "ported", "explicit", True)
    assert user.flags == ()
    widget = _row(result, "fixturepy.models.Widget")
    assert widget.status == "diverged"
    assert widget.manual is False
    assert "arity" in widget.flags
    fetch = _row(result, "fixturepy.service.fetch_user")
    assert fetch.status == "diverged"
    assert fetch.flags == ("signature",)
    checksum = _row(result, "fixturepy.service.checksum")
    assert checksum.status == "in_progress"
    assert checksum.manual is True
    assert checksum.flags == ()
    dropped = _row(result, "fixturepy.models.legacy_only")
    assert dropped.status == "dropped"
    assert dropped.flags == ("missing_counterpart",)
    assert dropped.manual is True


def test_missing_counterparts_are_planned_and_extra(tmp_path: Path) -> None:
    root, config = _fixture(tmp_path)
    py, rs = _inventories(root, config)
    result = match(py, rs, config)

    planned = _row(result, "fixturepy.models.ProtocolThing")
    assert (planned.rust, planned.status, planned.source) == (
        None, "planned", "derived")
    assert planned.flags == ("missing_counterpart",)
    extra = [row for row in result.rows
             if row.rust == "fixturecrate::models::Runner"][0]
    assert (extra.python, extra.status) == (None, "extra")
    assert extra.flags == ("missing_counterpart",)


def test_inference_candidates_never_change_status(tmp_path: Path) -> None:
    root, config = _fixture(tmp_path)
    py, rs = _inventories(root, config)
    result = match(py, rs, config)
    candidates = {(item.python, item.rust): item
                  for item in result.candidates}

    assert ("fixturepy.models.flush_cache",
            "fixturecrate::models::flush_cache") in candidates
    candidate = candidates[("fixturepy.models.flush_cache",
                            "fixturecrate::models::flush_cache")]
    assert candidate.confidence >= 0.9
    assert candidate.accepted is False
    assert candidate.status_effect == "none"
    assert _row(result, "fixturepy.models.flush_cache").status == "planned"
    extra = [row for row in result.rows
             if row.rust == "fixturecrate::models::flush_cache"][0]
    assert extra.status == "extra"


def test_infer_false_reports_no_candidates(tmp_path: Path) -> None:
    root, config = _fixture(tmp_path)
    config = replace(config, infer=False)
    py, rs = _inventories(root, config)
    result = match(py, rs, config)

    assert result.inferred is False
    assert result.candidates == ()
    assert _row(result, "fixturepy.models.flush_cache").status == "planned"


def test_manual_override_precedes_parity_flags(tmp_path: Path) -> None:
    root, config = _fixture(tmp_path)
    py, rs = _inventories(root, config)
    config = MigrationConfig(
        python="python", rust="rust", infer=False,
        pairs=(MigrationPair("fixturepy.models.Widget",
                             "fixturecrate::models::Widget", "ported"),))
    result = match(py, rs, config)
    row = _row(result, "fixturepy.models.Widget")

    assert row.status == "ported"
    assert row.manual is True
    assert "arity" in row.flags


def test_unresolved_pairs_are_reported_not_guessed(tmp_path: Path) -> None:
    root, config = _fixture(tmp_path)
    py, rs = _inventories(root, config)
    config = MigrationConfig(
        python="python", rust="rust",
        pairs=(MigrationPair("fixturepy.models.Missing",
                             "fixturecrate::models::Missing"),))
    result = match(py, rs, config)

    assert all(row.python != "fixturepy.models.Missing"
               for row in result.rows)
    assert result.unresolved_pairs == ({
        "python": "fixturepy.models.Missing",
        "rust": "fixturecrate::models::Missing",
        "python_reason": "no Python symbol with that key",
        "rust_reason": "no Rust symbol with that key",
    },)
    assert result.counts()["matched"] == 0


def test_resolved_counts_distinguish_configured_from_found(tmp_path: Path) -> None:
    root, config = _fixture(tmp_path)
    py, rs = _inventories(root, config)
    result = match(py, rs, config)
    counts = result.counts()

    assert counts["rows"] == 17
    assert counts["matched"] == 4
    assert counts["python_only"] == 8
    assert counts["rust_only"] == 5
    dropped = _row(result, "fixturepy.models.legacy_only")
    assert dropped.python_resolved is True and dropped.rust_resolved is False
    planned = _row(result, "fixturepy.models.ProtocolThing")
    assert planned.python_resolved is True and planned.rust_resolved is False
    extra = [row for row in result.rows
             if row.rust == "fixturecrate::models::Runner"][0]
    assert extra.python_resolved is False and extra.rust_resolved is True
    ported = _row(result, "fixturepy.models.User")
    assert ported.python_resolved is True and ported.rust_resolved is True


def test_trait_pair_is_type_shaped(tmp_path: Path) -> None:
    root, config = _fixture(tmp_path)
    py, rs = _inventories(root, config)
    config = MigrationConfig(
        python="python", rust="rust",
        pairs=(MigrationPair("fixturepy.models.ProtocolThing",
                             "fixturecrate::models::Runner"),))
    result = match(py, rs, config)
    row = _row(result, "fixturepy.models.ProtocolThing")
    assert "kind" not in row.flags


def test_inference_is_bounded(tmp_path: Path, monkeypatch) -> None:
    import importlib
    root, config = _fixture(tmp_path)
    py, rs = _inventories(root, config)
    match_module = importlib.import_module("ozagia.migration.match")
    monkeypatch.setattr(match_module, "MAX_INFER_PAIRS", 0)
    result = match(py, rs, config)
    assert result.candidates == ()
    assert result.truncated is True
