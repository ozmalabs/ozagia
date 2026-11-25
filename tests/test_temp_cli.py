"""The `ozagia tmp` command."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import temp_space as TS


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("OZAGIA_ROOT", str(repo))
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    return repo, state


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_create_plain_prints_path(env, capsys) -> None:
    repo, state = env
    code = cli.main(["tmp", "create", "--reason", "plain"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert Path(out).is_dir()
    assert Path(out).is_relative_to(state / "tmp" / "workspaces" / "scratch")


def test_create_json_list_and_status(env, capsys) -> None:
    repo, state = env
    code = cli.main(["tmp", "create", "--reason", "fixture dump",
                     "--kind", "check", "--session", "ses_1",
                     "--harness", "opencode", "--ttl-s", "3600",
                     "--model", "deepseek-v4", "--run-id", "run-7",
                     "--json"])
    assert code == 0
    created = _json(capsys)
    assert created["ok"] is True
    entry = created["entry"]
    assert entry["kind"] == "check"
    assert entry["session"] == {"harness": "opencode", "id": "ses_1"}
    assert entry["session_source"] == "caller"
    assert entry["model"] == "deepseek-v4"
    assert entry["run_id"] == "run-7"
    assert entry["ttl_s"] == 3600
    assert entry["expires_at"] == entry["created_at"] + 3600
    assert Path(entry["path"]).is_relative_to(state / "tmp" / "workspaces")
    assert entry["bytes"] >= 0

    assert cli.main(["tmp", "list", "--json"]) == 0
    listed = _json(capsys)
    assert listed["ok"] is True
    assert listed["count"] == 1
    assert listed["entries"][0]["id"] == entry["id"]

    assert cli.main(["tmp", "list", "--json", "--kind", "scratch"]) == 0
    assert _json(capsys)["count"] == 0

    assert cli.main(["tmp", "status", "--json"]) == 0
    status = _json(capsys)
    assert status["ok"] is True
    assert status["totals"]["entries"] == 1
    assert status["by_kind"]["check"]["entries"] == 1
    assert status["registry_errors"] == []


def test_list_plain_is_short_and_stable(env, capsys) -> None:
    repo, state = env
    assert cli.main(["tmp", "create", "--reason", "plain"]) == 0
    path = capsys.readouterr().out.strip()
    assert cli.main(["tmp", "list"]) == 0
    line = capsys.readouterr().out.strip()
    assert path in line
    assert line.split()[0].startswith("tmp_")
    assert "owned=yes" in line

    assert cli.main(["tmp", "list", "--kind", "eval"]) == 0
    assert capsys.readouterr().out.strip() == "no managed temporary entries"


def test_path_prints_and_creates_the_scratch_root(env, capsys) -> None:
    repo, state = env
    assert cli.main(["tmp", "path"]) == 0
    printed = Path(capsys.readouterr().out.strip())
    assert printed == state / "tmp" / "workspaces" / "scratch"
    assert printed.is_dir()

    assert cli.main(["tmp", "path", "--kind", "eval", "--json"]) == 0
    payload = _json(capsys)
    assert payload["kind"] == "eval"
    assert Path(payload["path"]).is_dir()
    assert Path(payload["path"]) == state / "tmp" / "workspaces" / "eval"


def test_adopt_unowned_clean_plan_and_apply(env, tmp_path, capsys) -> None:
    repo, state = env
    external = tmp_path / "tool-output"
    external.mkdir()
    (external / "out.bin").write_bytes(b"x" * 21)

    assert cli.main(["tmp", "adopt", str(external), "--reason",
                     "tool output", "--json"]) == 0
    entry = _json(capsys)["entry"]
    assert entry["owned"] is False

    assert cli.main(["tmp", "clean", "--json", "--id", entry["id"]]) == 0
    plan = _json(capsys)
    assert plan["dry_run"] is True
    assert plan["candidates"] == []
    assert plan["kept"][0]["why"] == "unowned"

    assert cli.main(["tmp", "clean", "--json", "--apply",
                     "--id", entry["id"]]) == 0
    done = _json(capsys)
    assert done["dry_run"] is False
    assert done["deleted"] == []
    assert external.is_dir()


def test_release_delete_and_missing_id(env, capsys) -> None:
    repo, state = env
    assert cli.main(["tmp", "create", "--reason", "delete me", "--json"]) == 0
    entry = _json(capsys)["entry"]

    assert cli.main(["tmp", "release", entry["id"], "--delete", "--json"]) == 0
    released = _json(capsys)["entry"]
    assert released["state"] == "released"
    assert not Path(entry["path"]).exists()

    assert cli.main(["tmp", "release", "tmp_ffffffffffff", "--json"]) == 1
    error = _json(capsys)
    assert error["ok"] is False
    assert error["code"] == "not_found"


def test_clean_dry_run_is_success_with_work(env, capsys) -> None:
    repo, state = env
    assert cli.main(["tmp", "create", "--reason", "ttl", "--ttl-s", "60",
                     "--json"]) == 0
    entry = _json(capsys)["entry"]
    record_path = state / "tmp" / "registry" / f"{entry['id']}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["expires_at"] = 1
    record_path.write_text(json.dumps(record), encoding="utf-8")

    assert cli.main(["tmp", "clean", "--expired", "--json"]) == 0
    plan = _json(capsys)
    assert plan["dry_run"] is True
    assert [row["id"] for row in plan["candidates"]] == [entry["id"]]
    assert Path(entry["path"]).is_dir()

    assert cli.main(["tmp", "clean", "--expired", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "deleted" in out
    assert not Path(entry["path"]).exists()


def test_cli_refusals_exit_one(env, tmp_path, capsys) -> None:
    repo, state = env
    assert cli.main(["tmp", "create", "--reason", "bad ttl", "--ttl-s", "1",
                     "--json"]) == 1
    error = _json(capsys)
    assert error["ok"] is False
    assert error["code"] == "refused"

    assert cli.main(["tmp", "adopt", str(tmp_path / "absent"),
                     "--reason", "absent", "--json"]) == 1
    assert _json(capsys)["ok"] is False

    link = tmp_path / "link"
    target = tmp_path / "target"
    target.mkdir()
    link.symlink_to(target, target_is_directory=True)
    assert cli.main(["tmp", "adopt", str(link), "--reason", "symlink",
                     "--json"]) == 1
    assert "symlink" in _json(capsys)["error"]


def test_audit_json_and_plain_are_read_only(env, tmp_path, monkeypatch,
                                            capsys) -> None:
    repo, state = env
    scan = tmp_path / "scan"
    scan.mkdir()
    (scan / "ozagia-eval-a.sock.lock").write_bytes(b"x" * 5)
    (scan / "ozagia_agent" / "run").mkdir(parents=True)
    (scan / "ozagia_agent" / "run" / "log").write_bytes(b"y" * 7)
    monkeypatch.setattr(TS, "allowed_temp_roots", lambda root: (scan,))
    before = sorted(scan.rglob("*"))

    assert cli.main(["tmp", "audit", "--json"]) == 0
    payload = _json(capsys)
    assert payload["ok"] is True
    assert payload["totals"]["artifacts"] == 2
    assert payload["totals"]["untracked"] == 2
    assert payload["totals"]["bytes"] >= 12
    assert payload["patterns"] == list(TS.AUDIT_PATTERNS)
    assert payload["truncated"] is False

    assert cli.main(["tmp", "audit"]) == 0
    out = capsys.readouterr().out
    assert "artifacts=2" in out
    assert "untracked" in out
    assert sorted(scan.rglob("*")) == before


def test_tmp_help_exits_zero(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["tmp", "--help"])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_clean_duration_accepts_units() -> None:
    from ozagia.temp_cli import _duration
    assert _duration("2") == 2 * 24 * 60 * 60
    assert _duration("2d") == 2 * 24 * 60 * 60
    assert _duration("90m") == 90 * 60
    assert _duration("5s") == 5
    with pytest.raises(Exception):
        _duration("nope")
