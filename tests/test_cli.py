"""The `ozagia` command (`ozagia.cli`)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia import cli

SRC = Path(__file__).resolve().parents[1] / "src"
MOD = "def add(a, b):\n    return a + b\n"


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> Path:
    for key in [k for k in os.environ if k.startswith("OZAGIA_")]:
        monkeypatch.delenv(key)
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "mod.py").write_text(MOD)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], cwd=root, check=True)
    return root


@pytest.mark.parametrize("argv", [
    ["--help"], ["index", "--help"], ["agent", "--help"], ["install", "--help"],
    ["daemon", "--help"], ["daemon", "serve", "--help"],
    ["daemon", "ensure", "--help"], ["mcp", "--help"],
    ["eval", "--help"], ["check", "--help"],
])
def test_help_exits_zero(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("ozagia ")


def test_console_script_runs():
    proc = subprocess.run([sys.executable, "-m", "ozagia.cli", "--help"],
                          env=dict(os.environ, PYTHONPATH=str(SRC)),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    for name in ("index", "agent", "daemon", "mcp", "eval", "check", "install"):
        assert name in proc.stdout


def test_index_builds_then_refreshes(repo, capsys):
    assert cli.main(["index", "--root", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("built ") and " nodes, " in out and " s" in out
    assert (repo / ".ozagia" / "graph.json").is_file()

    assert cli.main(["index", "--root", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("fresh ")

    (repo / "pkg" / "mod.py").write_text(MOD + "\n\ndef sub(a, b):\n    return a - b\n")
    assert cli.main(["index", "--root", str(repo), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["action"] == "refreshed" and rep["files"] == 1 and rep["added"] >= 1

    assert cli.main(["index", "--root", str(repo), "--rebuild"]) == 0
    assert capsys.readouterr().out.startswith("built ")


def test_index_error_is_reported(tmp_path, capsys, monkeypatch):
    from ozagia.harness import session as hs

    def fail(*a, **k):
        raise hs.SessionError("boom")

    monkeypatch.setattr(hs, "prepare_snapshot", fail)
    assert cli.main(["index", "--root", str(tmp_path)]) == 2
    assert "boom" in capsys.readouterr().err


@pytest.mark.parametrize("name,fn", [("daemon", "_daemon_main"), ("mcp", "_mcp_main"),
                                     ("eval", "_eval_main"), ("check", "_check_main")])
def test_delegation_passes_args_through(name, fn, monkeypatch):
    seen: list = []
    monkeypatch.setattr(cli, fn, lambda argv: seen.append(argv) or 7)
    argv = ["sub", "--flag", "value", "--", "-x", "--help"]
    assert cli.main([name, *argv]) == 7
    assert seen == [argv]


def test_daemon_ping_without_daemon(tmp_path, capsys):
    assert cli.main(["daemon", "ping", "--sock", str(tmp_path / "none.sock"),
                     "--timeout", "1"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_install_opencode_and_claude(tmp_path, capsys):
    assert cli.main(["install", "opencode", "--workdir", str(tmp_path)]) == 0
    assert (tmp_path / ".opencode" / "plugins" / "ozagia.js").is_file()
    assert cli.main(["install", "claude", "--workdir", str(tmp_path),
                     "--settings", "project"]) == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()


def test_install_codex(tmp_path, capsys):
    assert cli.main(["install", "codex", "--workdir", str(tmp_path),
                     "--python", "/opt/py/bin/python"]) == 0
    out = capsys.readouterr().out
    assert "/hooks" in out and "trust" in out
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert set(hooks["hooks"]) == {"SessionStart", "PreToolUse", "PostToolUse"}
    assert "[mcp_servers.ozagia]" in (tmp_path / ".codex" / "config.toml").read_text()
    assert "<!-- ozagia:begin -->" in (tmp_path / "AGENTS.md").read_text()


def test_install_codex_refuses_malformed_hooks(tmp_path, capsys):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "hooks.json").write_text("{nope")
    assert cli.main(["install", "codex", "--workdir", str(tmp_path)]) == 1
    assert "not valid JSON" in capsys.readouterr().err
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_install_targets_cover_the_parser_choices():
    parser = cli.build_parser()
    ins = next(a for a in parser._subparsers._group_actions[0].choices["install"]._actions
               if a.dest == "target")
    assert tuple(ins.choices) == ("opencode", "claude", "codex")
    assert set(cli.INSTALL_TARGETS) == set(ins.choices)


def test_agent_maps_options_to_run_agent(repo, tmp_path, monkeypatch):
    from ozagia.harness import opencode as hop
    seen: dict = {}

    def fake(workdir, **kw):
        seen.update(workdir=workdir, **kw)
        return {"exit_code": 3, "timed_out": False}

    monkeypatch.setattr(hop, "run_agent", fake)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("fix the bug\n")
    code = cli.main(["agent", "--root", str(repo), "--prompt-file", str(prompt),
                     "-m", "vllm/x", "--clone", "--base", "HEAD~0", "--keep-plugin",
                     "--timeout", "30", "--python", "/usr/bin/python3",
                     "--", "--agent", "build"])
    assert code == 3
    assert seen["workdir"] == repo
    assert seen["prompt"] == "fix the bug\n"
    assert seen["model"] == "vllm/x" and seen["clone"] is True and seen["base"] == "HEAD~0"
    assert seen["keep_plugin"] is True and seen["timeout_s"] == 30.0
    assert seen["run_python"] == "/usr/bin/python3"
    assert seen["extra_args"] == ["--agent", "build"]


def test_agent_exit_codes(repo, monkeypatch, capsys):
    from ozagia.harness import opencode as hop
    results = iter([{"timed_out": True, "exit_code": None},
                    {"timed_out": False, "exit_code": None}])
    monkeypatch.setattr(hop, "run_agent", lambda *a, **k: next(results))
    assert cli.main(["agent", "--workdir", str(repo), "--prompt", "x"]) == 124
    assert cli.main(["agent", "--workdir", str(repo), "--prompt", "x"]) == 1

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(hop, "run_agent", interrupted)
    assert cli.main(["agent", "--workdir", str(repo), "--prompt", "x"]) == 130


def test_agent_errors(repo, capsys):
    assert cli.main(["agent", "--workdir", str(repo), "--prompt", "x",
                     "--opencode", "no-such-opencode-ozagia-test"]) == 2
    assert "not found on PATH" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.main(["agent", "--workdir", str(repo), "--base", "HEAD"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["agent", "--prompt", "a", "--prompt-file", "b"])


def test_index_rebuilds_when_refresh_cannot_take_the_change_in(repo, capsys):
    # review fix 3: a tracked file that is not indexed changed. refresh
    # reingests nothing, the snapshot stays stale, so the session rebuilds.
    (repo / "notes.txt").write_text("v1\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "add", "notes.txt"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "notes"], cwd=repo, check=True)
    assert cli.main(["index", "--root", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("built ")
    (repo / "notes.txt").write_text("v2\n")
    assert cli.main(["index", "--root", str(repo), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["action"] == "built", rep
    assert cli.main(["index", "--root", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("fresh ")
