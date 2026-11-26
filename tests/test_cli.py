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
    ["eval", "--help"], ["check", "--help"], ["config", "--help"],
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


def test_retitled_argv_names_the_process_but_keeps_the_venv():
    exe = "/venv/bin/python"
    assert cli._retitled_argv([exe, "/venv/bin/ozagia", "--version"], exe=exe) \
        == ["/venv/bin/ozagia", "/venv/bin/ozagia", "--version"]
    assert cli._retitled_argv([exe, "-I", "-m", "ozagia", "agent"], exe=exe) \
        == ["/venv/bin/ozagia", "-I", "-m", "ozagia", "agent"]
    assert cli._retitled_argv([exe, "-m", "ozagia.cli", "--help"], exe=exe) \
        == ["/venv/bin/ozagia", "-m", "ozagia.cli", "--help"]


def test_retitled_argv_ignores_other_programs():
    exe = "/venv/bin/python"
    assert cli._retitled_argv([exe, "-m", "pytest"], exe=exe) is None
    assert cli._retitled_argv([exe, "/x/pytest"], exe=exe) is None
    assert cli._retitled_argv(["/venv/bin/ozagia", "/venv/bin/ozagia"], exe=exe) is None
    assert cli._retitled_argv([exe], exe=exe) is None
    assert cli._retitled_argv([], exe=exe) is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="procfs")
def test_retitle_process_reexecs_and_restores_the_interpreter(tmp_path):
    """tmux reads argv[0]; CPython derives sys.executable/sys.prefix from it."""
    script = tmp_path / "ozagia"
    script.write_text(
        "import sys\n"
        "from ozagia.cli import _retitle_process\n"
        "_retitle_process()\n"
        "print(sys.executable)\n"
        "print(sys.prefix)\n"
        "with open('/proc/self/cmdline', 'rb') as f:\n"
        "    print(f.read().split(b'\\0')[0].decode())\n",
        encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)],
                          env=dict(os.environ, PYTHONPATH=str(SRC)),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    exe, prefix, argv0 = proc.stdout.strip().splitlines()
    assert exe == sys.executable          # restored for tool_python / subprocess
    assert prefix == sys.prefix           # the venv is still the venv
    assert Path(argv0) == Path(sys.executable).with_name("ozagia")


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


def test_config_delegates_to_the_settings_cli(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OZAGIA_USER_CONFIG", raising=False)
    root = tmp_path / "repo"
    root.mkdir()

    assert cli.main(["config", "schema", "--json"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert any(item["key"] == "ui.theme" for item in schema["settings"])

    assert cli.main(["config", "values", "--root", str(root), "--json"]) == 0
    values = json.loads(capsys.readouterr().out)
    assert values["settings"]["ui.theme"]["source"] == "default"

    assert cli.main(["config", "get", "ui.theme", "--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert 'ui.theme = "auto"' in out and "(default)" in out

    assert cli.main([
        "config", "set", "ui.theme", "dark", "--root", str(root),
        "--dry-run", "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert preview["applied"] is False
    assert 'theme = "dark"' in preview["diff"]
    assert not (root / "ozagia").exists()

    assert cli.main([
        "config", "set", "models.tasks.diff_review", "local/qwen",
        "--root", str(root), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert 'diff_review = "local/qwen"' in out
    assert not (root / "ozagia").exists()


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
    assert set(hooks["hooks"]) == {"SessionStart", "PreToolUse", "PostToolUse",
                                  "PreCompact", "SessionEnd"}
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
    assert tuple(ins.choices) == ("opencode", "claude", "codex", "kiro")
    assert set(cli.INSTALL_TARGETS) == set(ins.choices)


def test_agent_maps_options_to_run_agent(repo, tmp_path, monkeypatch):
    from ozagia.harness import opencode as hop
    from ozagia.harness.progress import Spinner
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
                     "--opencode-config", str(tmp_path / "provider.json"),
                     "--", "--agent", "build"])
    assert code == 3
    assert seen["workdir"] == repo
    assert seen["prompt"] == "fix the bug\n"
    assert seen["model"] == "vllm/x" and seen["clone"] is True and seen["base"] == "HEAD~0"
    assert seen["keep_plugin"] is True and seen["timeout_s"] == 30.0
    assert seen["run_python"] == "/usr/bin/python3"
    assert seen["opencode_config"] == str(tmp_path / "provider.json")
    assert seen["extra_args"] == ["--agent", "build"]
    # Launch feedback goes to the (captured, not a terminal) stderr sink.
    assert isinstance(seen["progress"], Spinner)
    assert seen["progress"].enabled is False


def test_agent_repo_map_flag_defaults_and_passes_the_budget(repo, monkeypatch):
    from ozagia.harness import opencode as hop
    seen: list = []

    def fake(workdir, **kw):
        seen.append(kw.get("repo_map_tokens"))
        return {"exit_code": 0, "timed_out": False}

    monkeypatch.setattr(hop, "run_agent", fake)
    assert cli.main(["agent", "--root", str(repo), "--prompt", "x"]) == 0
    assert cli.main(["agent", "--root", str(repo), "--prompt", "x",
                     "--repo-map"]) == 0
    assert cli.main(["agent", "--root", str(repo), "--prompt", "x",
                     "--repo-map", "--repo-map-tokens", "2500"]) == 0
    assert seen == [None, 1500, 2500]


def test_agent_repo_map_bounds_and_dependency(repo, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["agent", "--root", str(repo), "--prompt", "x",
                  "--repo-map", "--repo-map-tokens", "199"])
    assert exc.value.code == 2
    assert "between 200 and 8000" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        cli.main(["agent", "--root", str(repo), "--prompt", "x",
                  "--repo-map", "--repo-map-tokens", "8001"])
    assert exc.value.code == 2
    assert "between 200 and 8000" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        cli.main(["agent", "--root", str(repo), "--prompt", "x",
                  "--repo-map-tokens", "500"])
    assert exc.value.code == 2
    assert "--repo-map-tokens needs --repo-map" in capsys.readouterr().err


def test_agent_help_describes_opt_in_repo_map(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["agent", "--help"])
    assert exc.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "bounded, task-ranked repository map in the first model request" in text
    assert "off by default" in text
    assert "200..8000" in text and "default: 1500" in text


def test_agent_help_describes_the_provider_config_boundary(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["agent", "--help"])
    assert exc.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "--opencode-config PATH" in text
    assert "provider, providers, model, small_model only" in text
    assert "--model wins" in text
    assert "never copied" in text and "global config is not read implicitly" in text


def test_agent_rejects_an_unsupported_provider_config_before_launch(repo, tmp_path, capsys):
    provider = tmp_path / "provider.json"
    provider.write_text('{"provider": {"vllm": {}}, "mcp": {}}', encoding="utf-8")
    assert cli.main(["agent", "--workdir", str(repo), "--prompt", "x",
                     "--opencode-config", str(provider)]) == 2
    err = capsys.readouterr().err
    assert "unsupported key(s): mcp" in err
    assert "provider-related keys" in err


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
