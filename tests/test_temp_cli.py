"""The `ozagia tmp` command."""
from __future__ import annotations

import json
import os
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


def _write(path: Path, size: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _old(path: Path) -> None:
    os.utime(path, (1000, 1000))


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
    assert entry["apparent_bytes"] >= 0
    assert entry["inodes"] >= 1
    assert entry["hardlinks"] == 0
    assert entry["allocation"] in ("blocks", "apparent-fallback")

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


def test_list_sort_and_status_top(env, capsys) -> None:
    repo, state = env
    created = []
    for reason, size in (("small", 10), ("medium", 9000), ("large", 20000)):
        assert cli.main(["tmp", "create", "--reason", reason, "--json"]) == 0
        entry = _json(capsys)["entry"]
        _write(Path(entry["path"]) / "blob.bin", size)
        created.append(entry)
    small, medium, large = created

    assert cli.main(["tmp", "list", "--json", "--sort", "apparent"]) == 0
    listed = _json(capsys)
    assert [row["id"] for row in listed["entries"]] == [
        large["id"], medium["id"], small["id"]]
    assert all("apparent_bytes" in row and "inodes" in row
               for row in listed["entries"])

    assert cli.main(["tmp", "status", "--json", "--top", "2"]) == 0
    status = _json(capsys)
    assert [row["id"] for row in status["top"]] == [
        large["id"], medium["id"]]
    assert status["top"][0]["bytes"] >= status["top"][1]["bytes"]
    for row in status["top"]:
        assert row["kind"] == "scratch"
        assert row["session"] is None
        assert row["reason"] in {"medium", "large"}
        assert Path(row["path"]).is_dir()
        assert row["apparent_bytes"] >= 0

    assert cli.main(["tmp", "status", "--top", "1"]) == 0
    out = capsys.readouterr().out
    assert "top 1 by allocated:" in out
    assert "allocated=" in out and "apparent=" in out


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


def test_clean_legacy_json_plain_and_apply(env, tmp_path, monkeypatch,
                                           capsys) -> None:
    repo, state = env
    scan = tmp_path / "scan"
    scan.mkdir()
    old = scan / "ozagia-eval-old.sock.lock"
    _write(old)
    _old(old)
    recent = scan / "ozagia-eval-recent.sock.lock"
    _write(recent)
    monkeypatch.setattr(TS, "allowed_temp_roots", lambda root: (scan,))

    assert cli.main(["tmp", "clean-legacy", "--json"]) == 0
    payload = _json(capsys)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["older_than_s"] == TS.LEGACY_DEFAULT_OLDER_THAN_S
    assert payload["include_caches"] is False
    assert payload["registry_written"] is False
    assert payload["totals"]["artifacts"] == 2
    assert payload["totals"]["candidates"] == 1
    assert payload["totals"]["deleted"] == 0
    assert payload["totals"]["kept"] == 1
    assert payload["totals"]["errors"] == 0
    assert payload["truncated"] is False
    assert [row["path"] for row in payload["candidates"]] == [str(old)]
    assert old.exists() and recent.exists()
    assert str(Path("/tmp/opencode")) in payload["excluded_roots"]
    assert str(Path.home() / ".local" / "share" / "opencode") \
        in payload["excluded_roots"]

    assert cli.main(["tmp", "clean-legacy"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("plan:")
    assert "delete" in out and str(old) in out
    assert "keep" in out and str(recent) in out

    assert cli.main(["tmp", "clean-legacy", "--apply", "--json"]) == 0
    done = _json(capsys)
    assert done["ok"] is True
    assert done["dry_run"] is False
    assert done["totals"]["deleted"] == 1
    assert done["deleted"] == [str(old)]
    assert not old.exists() and recent.exists()


def test_clean_legacy_older_than_and_caches_flags(env, tmp_path, monkeypatch,
                                                  capsys) -> None:
    repo, state = env
    scan = tmp_path / "scan"
    scan.mkdir()
    recent = scan / "ozagia-eval-recent.sock.lock"
    _write(recent)
    _old(recent)
    cache = scan / "ozagia_iac_bench" / "run"
    _write(cache / "report.json")
    _old(cache)
    monkeypatch.setattr(TS, "allowed_temp_roots", lambda root: (scan,))

    assert cli.main(["tmp", "clean-legacy", "--json",
                     "--older-than", "0"]) == 0
    defaulted = _json(capsys)
    assert [row["path"] for row in defaulted["candidates"]] == [str(recent)]
    assert {row["why"] for row in defaulted["kept"]} == {"cache-disabled"}

    assert cli.main(["tmp", "clean-legacy", "--json",
                     "--older-than", "1h", "--include-caches"]) == 0
    included = _json(capsys)
    assert included["older_than_s"] == 3600
    assert included["include_caches"] is True
    assert {row["path"] for row in included["candidates"]} == {
        str(recent), str(cache)}
    assert recent.exists() and cache.is_dir()


def test_clean_legacy_apply_error_reports_and_exits_one(
        env, tmp_path, monkeypatch, capsys) -> None:
    repo, state = env
    scan = tmp_path / "scan"
    scan.mkdir()
    artifact = scan / "ozagia-native-bad"
    _write(artifact / "out")
    _old(artifact)
    monkeypatch.setattr(TS, "allowed_temp_roots", lambda root: (scan,))

    def fail(path, *, directory):
        raise OSError("simulated failure")

    monkeypatch.setattr(TS, "_legacy_remove", fail)
    assert cli.main(["tmp", "clean-legacy", "--apply", "--json"]) == 1
    payload = _json(capsys)
    assert payload["ok"] is False
    assert payload["dry_run"] is False
    assert payload["totals"]["errors"] == 1
    assert "simulated failure" in payload["errors"][0]
    assert artifact.is_dir()


def test_clean_legacy_refusal_exits_one(env, monkeypatch, capsys) -> None:
    repo, state = env

    def refuse(*args, **kwargs):
        raise TS.TempSpaceError("refused for test")

    monkeypatch.setattr(TS, "clean_legacy", refuse)
    assert cli.main(["tmp", "clean-legacy", "--json"]) == 1
    payload = _json(capsys)
    assert payload["ok"] is False
    assert payload["code"] == "refused"
    assert "refused for test" in payload["error"]


# --------------------------------------------------------------------------- #
# leases, pinning and budgets (Stage B)
# --------------------------------------------------------------------------- #


def test_pin_and_unpin_cli(env, capsys) -> None:
    repo, state = env
    assert cli.main(["tmp", "create", "--reason", "pin me", "--json"]) == 0
    entry = _json(capsys)["entry"]

    assert cli.main(["tmp", "pin", entry["id"],
                     "--until", "2030-01-01T00:00:00Z", "--json"]) == 0
    pinned = _json(capsys)["entry"]
    assert pinned["pinned"] is True
    assert pinned["pinned_until"] > 0

    assert cli.main(["tmp", "list", "--json"]) == 0
    listed = _json(capsys)["entries"][0]
    assert listed["pinned"] is True
    assert listed["leased"] is False

    assert cli.main(["tmp", "unpin", entry["id"], "--json"]) == 0
    assert _json(capsys)["entry"]["pinned"] is False


def test_create_budget_refusal_is_json_and_exits_one(env, capsys) -> None:
    repo, state = env
    code = cli.main(["tmp", "create", "--reason", "too big",
                     "--max-total-bytes", "0", "--json"])
    assert code == 1
    payload = _json(capsys)
    assert payload["ok"] is False
    assert payload["code"] == "budget"
    assert TS.list_entries(repo) == []


def test_gc_cli_plan_apply_and_pinned_survival(env, capsys) -> None:
    repo, state = env
    assert cli.main(["tmp", "create", "--reason", "released", "--json"]) == 0
    released = _json(capsys)["entry"]
    assert cli.main(["tmp", "release", released["id"], "--json"]) == 0
    _json(capsys)

    assert cli.main(["tmp", "create", "--reason", "pinned", "--json"]) == 0
    pinned = _json(capsys)["entry"]
    assert cli.main(["tmp", "pin", pinned["id"], "--json"]) == 0
    _json(capsys)

    assert cli.main(["tmp", "gc", "--max-total-bytes", "0", "--json"]) == 0
    plan = _json(capsys)
    assert plan["dry_run"] is True
    assert [row["id"] for row in plan["candidates"]] == [released["id"]]
    assert Path(released["path"]).is_dir()

    assert cli.main(["tmp", "gc", "--max-total-bytes", "0",
                     "--apply", "--json"]) == 0
    done = _json(capsys)
    assert done["dry_run"] is False
    assert done["deleted"] == [released["id"]]
    assert not Path(released["path"]).exists()
    assert Path(pinned["path"]).is_dir()
    assert TS.get(repo, pinned["id"])["pinned"] is True


def test_gc_cli_defaults_to_released_entries(env, capsys) -> None:
    repo, state = env
    assert cli.main(["tmp", "create", "--reason", "released", "--json"]) == 0
    released = _json(capsys)["entry"]
    assert cli.main(["tmp", "release", released["id"], "--json"]) == 0
    _json(capsys)
    assert cli.main(["tmp", "create", "--reason", "active", "--json"]) == 0
    active = _json(capsys)["entry"]

    assert cli.main(["tmp", "gc", "--json"]) == 0
    plan = _json(capsys)
    assert [row["id"] for row in plan["candidates"]] == [released["id"]]
    assert any(row["id"] == active["id"] and row["why"] == "not-selected"
               for row in plan["kept"])
