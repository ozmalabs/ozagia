from __future__ import annotations

import json
import subprocess

from ozagia import cli
from ozagia.core.memory import build_index


def test_doctor_reports_missing_fresh_and_stale(tmp_path, monkeypatch, capsys):
    for name in ("OZAGIA_CONFIG", "OZAGIA_ROOT", "OZAGIA_STATE_DIR", "OZAGIA_ADAPTERS"):
        monkeypatch.delenv(name, raising=False)
    def report():
        assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 0
        return json.loads(capsys.readouterr().out)

    assert report()["snapshot"]["state"] == "missing"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "module.py").write_text("def example():\n    return 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "module.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    build_index(tmp_path, tmp_path / ".ozagia" / "graph.json")
    rep = report()
    assert rep["snapshot"]["state"] == "fresh"
    assert rep["parsers"]["python"]["available"]
    (tmp_path / "module.py").write_text("def example():\n    return 2\n")
    assert report()["snapshot"]["state"] == "stale"


def test_doctor_invalid_config_and_snapshot(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    (tmp_path / ".ozagia.toml").write_text("[broken\n")
    (tmp_path / ".ozagia").mkdir()
    (tmp_path / ".ozagia" / "graph.json").write_text("bad json")
    assert cli.main(["doctor", "--root", str(tmp_path), "--json"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert not result["config"]["valid"]
    assert result["snapshot"]["state"] == "invalid"


def test_doctor_does_not_certify_different_index_scope(tmp_path, monkeypatch):
    from ozagia.core import memory as memory_module
    from ozagia.doctor import diagnose

    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    monkeypatch.setattr(memory_module, "SOURCE_DIRS", ("another-project",))
    (tmp_path / ".ozagia").mkdir()
    (tmp_path / ".ozagia" / "graph.json").write_text("{}")

    class Memory:
        def __init__(self, **kwargs):
            pass

        def staleness(self):
            return {"freshness": "fresh", "verification_errors": []}

    monkeypatch.setattr(memory_module, "DevMemory", Memory)
    result = diagnose(tmp_path)
    assert result["snapshot"]["state"] == "unknown"
    assert "configuration differs" in result["snapshot"]["details"]["verification_errors"][0]
