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
                    "validate", "plan"):
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
