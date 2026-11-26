"""The host's model exposure opt-in for the session mesh."""
from __future__ import annotations

import pytest

from ozagia import cli
from ozagia.core import mesh as M


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("OZAGIA_ROOT", str(repo))
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    monkeypatch.delenv("OZAGIA_MESH_ID", raising=False)
    monkeypatch.delenv("OZAGIA_MESH", raising=False)
    monkeypatch.delenv("OZAGIA_MESH_MODEL", raising=False)
    return repo, state


def test_mesh_model_defaults_to_read_and_reads_config(env) -> None:
    repo, state = env
    assert M.mesh_model(repo) == "read"
    assert not (state / "mesh").exists()

    (repo / ".ozagia.toml").write_text('[mesh]\nmodel = "coordinate"\n',
                                       encoding="utf-8")
    assert M.mesh_model(repo) == "coordinate"
    assert not (state / "mesh").exists()

    (repo / ".ozagia.toml").write_text("[mesh]\nmodel = 'supervise'\n",
                                       encoding="utf-8")
    assert M.mesh_model(repo) == "supervise"

    (repo / ".ozagia.toml").write_text("[mesh]\nmodel = 'admin'\n",
                                       encoding="utf-8")
    with pytest.raises(M.MeshConfigError):
        M.mesh_model(repo)


def test_mesh_model_env_wins_and_must_be_known(env, monkeypatch) -> None:
    repo, _ = env
    (repo / ".ozagia.toml").write_text('[mesh]\nmodel = "read"\n',
                                       encoding="utf-8")
    monkeypatch.setenv("OZAGIA_MESH_MODEL", "coordinate")
    assert M.mesh_model(repo) == "coordinate"
    monkeypatch.setenv("OZAGIA_MESH_MODEL", "SUPERVISE")
    assert M.mesh_model(repo) == "supervise"
    monkeypatch.setenv("OZAGIA_MESH_MODEL", "none")
    assert M.mesh_model(repo) == "none"
    monkeypatch.setenv("OZAGIA_MESH_MODEL", "admin")
    with pytest.raises(M.MeshConfigError):
        M.mesh_model(repo)


def test_mesh_model_is_a_validated_closed_set() -> None:
    for value in M.MESH_MODELS:
        assert M.validate_mesh_model(value) == value
    for value in ("", "read ", "readonly", None, 1):
        with pytest.raises(M.MeshConfigError):
            M.validate_mesh_model(value)


def test_agent_mesh_model_cli_accepts_only_the_closed_set() -> None:
    parser = cli.build_parser()
    for value in M.MESH_MODELS:
        args = parser.parse_args(["agent", "--mesh-model", value])
        assert args.mesh_model == value
    with pytest.raises(SystemExit) as bad:
        parser.parse_args(["agent", "--mesh-model", "admin"])
    assert bad.value.code == 2
