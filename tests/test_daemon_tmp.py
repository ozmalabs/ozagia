"""The daemon `tmp` op: managed scratch space (`ozagia.core.temp_space`)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ozagia.core import temp_space as T
from ozagia.daemon.server import DaemonState


def _state(root: Path) -> DaemonState:
    return DaemonState(root, None, root / "j.jsonl")


def _req(**fields) -> dict:
    base = {"op": "tmp", "harness": "opencode", "session": "ses_test",
            "reason": "parse fixture dump"}
    base.update(fields)
    return base


def _snapshot(root: Path) -> list[tuple[str, int]]:
    base = T.registry_dir(root).parent
    if not base.exists():
        return []
    return sorted((str(path.relative_to(base)), path.stat().st_size)
                  for path in base.rglob("*"))


def test_preflight_validates_and_returns_a_path_without_creating(tmp_path: Path):
    state = _state(tmp_path)
    result = state.dispatch(_req(action="preflight", ttl_s=3600))
    assert result["ok"] is True
    assert result["id"] is None
    assert result["exists"] is False
    path = Path(result["path"])
    assert not path.exists()
    assert path.parent == T.default_scratch_root(tmp_path)
    assert not T.registry_dir(tmp_path).exists()
    assert T.list_entries(tmp_path) == []


def test_create_writes_one_owned_entry_with_session_reason_and_ttl(
        tmp_path: Path):
    state = _state(tmp_path)
    preflight = state.dispatch(_req(action="preflight", ttl_s=3600))
    result = state.dispatch(_req(action="create", ttl_s=3600, model="m1",
                                 run_id="run-7"))
    assert result["ok"] is True
    assert result["path"] == preflight["path"]
    assert isinstance(result["bytes"], int) and result["bytes"] >= 0
    assert isinstance(result["files"], int) and result["files"] >= 1
    path = Path(result["path"])
    assert path.is_dir()
    entry = T.get(tmp_path, result["id"])
    assert entry["kind"] == "scratch"
    assert entry["path"] == result["path"]
    assert entry["reason"] == "parse fixture dump"
    assert entry["session"] == {"harness": "opencode", "id": "ses_test"}
    assert entry["model"] == "m1" and entry["run_id"] == "run-7"
    assert entry["ttl_s"] == 3600
    assert entry["expires_at"] == entry["created_at"] + 3600
    assert result["expires_at"] == entry["expires_at"]
    assert entry["owned"] is True
    assert [item["id"] for item in T.list_entries(tmp_path)] == [result["id"]]


def test_create_can_pin_the_preflighted_path(tmp_path: Path):
    state = _state(tmp_path)
    preflight = state.dispatch(_req(action="preflight", ttl_s=3600))
    result = state.dispatch(_req(action="create", ttl_s=3600,
                                 preflight_path=preflight["path"]))
    assert result["ok"] is True
    assert result["path"] == preflight["path"]


def test_create_refuses_a_preflight_path_outside_the_scratch_root(
        tmp_path: Path):
    state = _state(tmp_path)
    outside = tmp_path / "outside"
    for path in (str(outside), "/tmp/not-managed"):
        result = state.dispatch(_req(action="create", preflight_path=path))
        assert result["ok"] is False
        assert result["code"] == "invalid_path"
    assert T.list_entries(tmp_path) == []
    assert not outside.exists()


def test_create_uses_the_seven_day_default_ttl(tmp_path: Path):
    state = _state(tmp_path)
    result = state.dispatch(_req(action="create"))
    assert result["ok"] is True
    assert T.get(tmp_path, result["id"])["ttl_s"] == 7 * 24 * 60 * 60


@pytest.mark.parametrize("fields, code", [
    ({"session": ""}, "invalid_session"),
    ({"session": "bad session"}, "invalid_session"),
    ({"session": "x" * 129}, "invalid_session"),
    ({"reason": ""}, "invalid_reason"),
    ({"reason": "x" * 201}, "invalid_reason"),
    ({"reason": "a\x01b"}, "invalid_reason"),
    ({"ttl_s": 59}, "invalid_ttl"),
    ({"ttl_s": 7776001}, "invalid_ttl"),
    ({"ttl_s": True}, "invalid_ttl"),
    ({"ttl_s": "60"}, "invalid_ttl"),
    ({"harness": "claude"}, "invalid_harness"),
    ({"harness": None}, "invalid_harness"),
    ({"kind": "check"}, "invalid_kind"),
    ({"kind": "nope"}, "invalid_kind"),
    ({"path": "/tmp/evil"}, "unknown_field"),
    ({"input": {"path": "/tmp/evil"}}, "unknown_field"),
])
def test_create_refusals_create_nothing(tmp_path: Path, fields: dict,
                                        code: str):
    state = _state(tmp_path)
    result = state.dispatch(_req(action="create", **fields))
    assert result["ok"] is False
    assert result["code"] == code
    assert T.list_entries(tmp_path) == []
    assert not T.registry_dir(tmp_path).exists()


def test_unknown_action_and_field_are_refused(tmp_path: Path):
    state = _state(tmp_path)
    unknown = state.dispatch({"op": "tmp", "action": "delete"})
    assert unknown["ok"] is False and unknown["code"] == "invalid_action"
    field = state.dispatch({"op": "tmp", "action": "status", "limit": 1})
    assert field["ok"] is False and field["code"] == "unknown_field"


def test_info_list_and_status_are_read_only(tmp_path: Path):
    state = _state(tmp_path)
    first = state.dispatch(_req(action="create", session="ses_a"))
    second = state.dispatch(_req(action="create", session="ses_b",
                                 reason="second"))
    assert first["ok"] and second["ok"]
    before = _snapshot(tmp_path)

    info = state.dispatch({"op": "tmp", "action": "info",
                           "entry_id": first["id"]})
    assert info["ok"] is True
    assert info["entry"]["id"] == first["id"]
    listed = state.dispatch({"op": "tmp", "action": "list",
                             "kind": "scratch", "session": "ses_a"})
    assert listed["ok"] is True and listed["count"] == 1
    assert listed["entries"][0]["id"] == first["id"]
    bounded = state.dispatch({"op": "tmp", "action": "list", "limit": 1})
    assert bounded["count"] == 2 and bounded["truncated"] is True
    status = state.dispatch({"op": "tmp", "action": "status"})
    assert status["ok"] is True
    assert status["totals"]["entries"] == 2
    assert status["by_kind"]["scratch"]["entries"] == 2
    assert set(status) >= {"root", "registry", "totals", "by_kind",
                           "by_session", "by_ownership", "filesystems",
                           "warnings", "registry_errors"}
    assert _snapshot(tmp_path) == before


def test_info_refuses_a_missing_or_bad_entry_id(tmp_path: Path):
    state = _state(tmp_path)
    missing = state.dispatch({"op": "tmp", "action": "info",
                              "entry_id": "tmp_000000000000"})
    assert missing["ok"] is False and missing["code"] == "not_found"
    bad = state.dispatch({"op": "tmp", "action": "info", "entry_id": 3})
    assert bad["ok"] is False and bad["code"] == "not_found"
    unknown = state.dispatch({"op": "tmp", "action": "info",
                              "entry_id": "tmp_000000000000",
                              "path": "/etc"})
    assert unknown["ok"] is False and unknown["code"] == "unknown_field"


@pytest.mark.parametrize("fields, code", [
    ({"kind": "nope"}, "invalid_kind"),
    ({"session": "bad session"}, "invalid_session"),
    ({"owned": "yes"}, "invalid_owned"),
    ({"include_all": 1}, "invalid_include_all"),
    ({"limit": 0}, "invalid_limit"),
    ({"limit": 501}, "invalid_limit"),
    ({"limit": True}, "invalid_limit"),
])
def test_list_refusals(tmp_path: Path, fields: dict, code: str):
    state = _state(tmp_path)
    result = state.dispatch({"op": "tmp", "action": "list", **fields})
    assert result["ok"] is False and result["code"] == code


def test_a_failed_create_leaves_no_half_entry(tmp_path: Path, monkeypatch):
    state = _state(tmp_path)
    registry = T.registry_dir(tmp_path)
    real_write = T._write_json

    def failing(path, payload):
        if path.parent == registry:
            raise OSError("simulated registry failure")
        return real_write(path, payload)

    monkeypatch.setattr(T, "_write_json", failing)
    result = state.dispatch(_req(action="create"))
    assert result["ok"] is False and result["code"] == "temp_error"
    assert T.list_entries(tmp_path) == []
    scratch = T.default_scratch_root(tmp_path)
    assert not scratch.exists() or not any(scratch.iterdir())
