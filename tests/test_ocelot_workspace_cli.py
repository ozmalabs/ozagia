"""The Ocelot workspace CLI: real doctor and real graph queries.

`status` runs against a checkout with no engine and needs no graph.  The
seed/seeds/plan tests write a small version-2 snapshot so they exercise the
real `workspace` functions through `DevMemory`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia.integrations.ocelot.workspace_cli import main


def _write_graph(root: Path, seeds: tuple[str, ...] = ("alpha",)) -> Path:
    state = root / ".ozagia"
    state.mkdir(parents=True)
    nodes = [{
        "id": 0,
        "key": "repo:",
        "type": "Repo",
        "attrs": {"name": root.name, "root": str(root)},
    }]
    for nid, seed in enumerate(seeds, start=1):
        nodes.append({
            "id": nid,
            "key": f"ocelot_seed:{seed}",
            "type": "OcelotSeed",
            "attrs": {
                "id": seed,
                "name": seed,
                "version": "1",
                "path": f"seeds/{seed}.json",
                "depends_on": [],
                "counts": {"rules": 1},
            },
        })
    graph = {
        "version": 2,
        "nodes": nodes,
        "edges": [],
        "next_id": len(nodes),
        "build": {},
        "files": {},
    }
    (state / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
    return state / "graph.json"


def test_help_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for command in ("status", "doctor", "seed", "seeds", "research", "engine",
                    "validate", "claim", "plan", "context", "edit",
                    "boot-edit"):
        assert command in out


def test_status_without_an_engine_is_blocked(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["status", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 1
    result = json.loads(captured.out)
    assert result["state"] == "blocked"


def test_doctor_is_an_alias_for_status(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["doctor", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.out)["state"] == "blocked"


def test_seed_without_a_graph_exits_2(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["seed", "alpha", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "no graph snapshot" in captured.err
    assert "ozagia index --root" in captured.err
    assert str(tmp_path) in captured.err


def test_plan_without_a_graph_exits_2(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["plan", "--root", str(tmp_path), "--changed",
                 "substrate/seed_loader.py"])
    captured = capsys.readouterr()
    assert code == 2
    assert "ozagia index --root" in captured.err


@pytest.mark.parametrize("argv", [
    ["research", "371000"],
    ["engine", "--op", "term_census"],
    ["validate", "alpha"],
])
def test_new_graph_commands_without_a_graph_exit_2(
        argv: list[str], tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    code = main([*argv, "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 2
    assert "no graph snapshot" in captured.err
    assert "ozagia index --root" in captured.err


def test_seed_json_is_serializable(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path)
    code = main(["seed", "alpha", "--root", str(tmp_path), "--include", "all",
                 "--json"])
    captured = capsys.readouterr()
    assert code == 0
    result = json.loads(captured.out)
    assert result["ok"] is True
    assert result["id"] == "alpha"
    assert result["path"] == "seeds/alpha.json"
    assert result["counts"] == {"rules": 1}


def test_seeds_json_is_serializable(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path, seeds=("alpha", "beta"))
    code = main(["seeds", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    result = json.loads(captured.out)
    assert result["ok"] is True
    assert result["seed_count"] == 2
    assert [entry["id"] for entry in result["entries"]] == ["alpha", "beta"]


def test_unknown_seed_says_ok_false_and_exits_1(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path)
    code = main(["seed", "missing", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 1
    result = json.loads(captured.out)
    assert result["ok"] is False
    assert "no Ocelot seed" in result["error"]


def _write_seed(root: Path) -> Path:
    path = root / "seeds" / "alpha.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{\n  "id": "alpha",\n  "version": "1",\n  "nodes": [],\n'
        '  "edges": [],\n  "rules": [],\n  "reflexes": [],\n'
        '  "productions": []\n}\n', encoding="utf-8")
    return path


def test_edit_previews_a_metadata_change_without_writing(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path)
    seed = _write_seed(tmp_path)
    before = seed.read_bytes()
    ops = tmp_path / "ops.json"
    ops.write_text(json.dumps([
        {"op": "set_metadata", "fields": {"version": "2"}},
    ]), encoding="utf-8")

    code = main(["edit", "alpha", "--ops", str(ops),
                 "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    result = json.loads(captured.out)
    assert result["applied"] is False
    assert result["base_sha256"] != result["new_sha256"]
    assert result["validation_after"]["ok"] is True
    assert seed.read_bytes() == before


def test_edit_requires_a_valid_operation_file(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path)
    _write_seed(tmp_path)
    ops = tmp_path / "ops.json"
    ops.write_text('{"operations": []}', encoding="utf-8")
    assert main(["edit", "alpha", "--ops", str(ops),
                 "--root", str(tmp_path)]) == 2
    assert "non-empty list of operations" in capsys.readouterr().err


def test_boot_edit_previews_without_writing(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path)
    _write_seed(tmp_path)
    boot = tmp_path / "substrate" / "boot_core.py"
    boot.parent.mkdir()
    boot.write_text('CORE_SEEDS = ("decision_core",)\n'
                    "OPTIONAL_CORE_SEEDS = ()\n", encoding="utf-8")
    before = boot.read_bytes()
    ops = tmp_path / "boot-ops.json"
    ops.write_text(json.dumps([{
        "op": "add_boot", "file": "substrate/boot_core.py",
        "name": "CORE_SEEDS", "seed": "alpha",
    }]), encoding="utf-8")

    code = main(["boot-edit", "--ops", str(ops),
                 "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    result = json.loads(captured.out)
    assert result["applied"] is False
    assert result["validation_after"]["ok"] is True
    assert "alpha" in result["diff_view"]
    assert boot.read_bytes() == before


def test_boot_edit_previews_a_registry_entry(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path)
    _write_seed(tmp_path)
    registry = tmp_path / "domains" / "seed_registry.py"
    registry.parent.mkdir()
    registry.write_text(
        "from substrate.seeds import REGISTRY, Seed\n"
        "from substrate.seed_loader import manifest_for\n\n\n"
        "def _installer_from_manifest(seed_id):\n"
        "    return lambda agent: manifest_for(seed_id)\n\n\n"
        'REGISTRY.register(Seed(\n'
        '    id="decision_core", version="1.0.0", depends_on=(),\n'
        '    installer=_installer_from_manifest("decision_core"),\n'
        '    description="core",\n'
        '))\n', encoding="utf-8")
    before = registry.read_bytes()
    ops = tmp_path / "registry-ops.json"
    ops.write_text(json.dumps([{
        "op": "add_registry", "seed": "alpha", "version": "1",
        "depends_on": [], "description": "preview registry entry",
    }]), encoding="utf-8")

    code = main(["boot-edit", "--ops", str(ops),
                 "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    result = json.loads(captured.out)
    assert result["validation_after"]["ok"] is True
    assert "alpha" in result["diff_view"]
    assert registry.read_bytes() == before


def test_context_reports_blocked_doctor_and_exits_1(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path)
    code = main(["context", "--root", str(tmp_path),
                 "--changed", "docs/CODE_MAP.md", "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["schema"] == "ozagia.ocelot.context.v1"
    assert result["plan"]["mode"] == "none"
    assert "doctor" in result and "agent_note" in result
    # The synthetic checkout has no engine, so the doctor is blocked and the
    # documented context exit code is 1 even though the plan is empty.
    assert result["state"] == "blocked"
    assert code == 1


def test_claim_reports_a_declared_ledger_conflict(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path)
    (tmp_path / "VISION_PROTOCOL.md").write_text(
        "## Burned-seed ledger\n\n| range | used for |\n|---|---|\n"
        "| 100-199 | demo block |\n", encoding="utf-8")
    code = main(["claim", "150", "250", "--root", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    assert code == 1
    result = json.loads(captured.out)
    assert result["state"] == "conflict"
    assert result["conflicts"][0]["range"] == [100, 199]


def test_plan_json_accepts_multiple_changed_paths_and_full_mode_exits_0(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path)
    code = main([
        "plan", "--root", str(tmp_path),
        "--changed", "substrate/seed_loader.py", "docs/CODE_MAP.md",
        "--json",
    ])
    captured = capsys.readouterr()
    assert code == 0
    result = json.loads(captured.out)
    assert result["ok"] is True
    assert result["mode"] == "full"
    assert result["full_gate"] is True
    assert "substrate/seed_loader.py" in result["changed"]
    assert result["full_command"]


def test_plan_seed_json_is_serializable(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_graph(tmp_path)
    code = main(["plan", "--root", str(tmp_path), "--seed", "alpha", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    result = json.loads(captured.out)
    assert result["ok"] is True
    assert [seed["id"] for seed in result["seeds"]] == ["alpha"]
