"""Managed temporary space (`ozagia.core.temp_space`)."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ozagia.core import temp_space as TS


@pytest.fixture()
def root(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("OZAGIA_ROOT", str(repo))
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(state))
    return repo


def _record(root: Path, entry_id: str) -> dict:
    path = TS.registry_dir(root) / f"{entry_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _rewrite(root: Path, entry_id: str, **changes) -> None:
    record = _record(root, entry_id)
    record.update(changes)
    (TS.registry_dir(root) / f"{entry_id}.json").write_text(
        json.dumps(record), encoding="utf-8")


def _write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _old(path: Path) -> None:
    os.utime(path, (1000, 1000))


def _stale_socket(path: Path) -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
    finally:
        server.close()


def _short_socket_dir() -> Path:
    """A short bindable directory; pytest's tmp_path can exceed SUN_PATH."""
    path = Path("/tmp") / f"ozs-sock-{os.getpid()}-{os.urandom(4).hex()}"
    path.mkdir()
    return path


def _rows(plan: dict) -> dict[str, dict]:
    return {row["path"]: row for row in plan["artifacts"]}


def _kept_reason(plan: dict, entry_id: str) -> str:
    return next(row["why"] for row in plan["kept"]
                if row["id"] == entry_id)


def test_create_list_status_round_trip(root: Path) -> None:
    session = TS.make_session("opencode", "ses_abc")
    entry = TS.create(root, kind="scratch", reason="fixture dump",
                      session=session, ttl_s=3600, pid=4242)

    assert entry["id"].startswith("tmp_")
    assert entry["kind"] == "scratch"
    assert entry["reason"] == "fixture dump"
    assert entry["session"] == {"harness": "opencode", "id": "ses_abc"}
    assert entry["session_source"] == "caller"
    assert entry["pid"] == 4242
    assert entry["owned"] is True
    assert entry["state"] == "active"
    assert entry["ttl_s"] == 3600
    assert entry["expires_at"] == entry["created_at"] + 3600
    assert entry["path"].startswith(str(TS.default_scratch_root(root)))
    assert Path(entry["path"]).is_relative_to(TS.state_dir(root))

    path = Path(entry["path"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    marker_path = path / TS.MARKER_NAME
    assert stat.S_IMODE(marker_path.stat().st_mode) == 0o600
    registry_path = TS.registry_dir(root) / f"{entry['id']}.json"
    assert stat.S_IMODE(registry_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(TS.registry_dir(root).stat().st_mode) == 0o700

    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    record = _record(root, entry["id"])
    assert marker == {
        "version": TS.VERSION,
        "id": entry["id"],
        "kind": "scratch",
        "session": {"harness": "opencode", "id": "ses_abc"},
        "reason": "fixture dump",
        "root": str(root),
        "created_at": entry["created_at"],
        "pid": 4242,
        "owned": True,
    }
    assert record["path"] == entry["path"]
    assert record["session"] == marker["session"]
    for payload in (marker, record):
        assert "content" not in payload
        assert "contents" not in payload
        assert "prompt" not in payload
        assert "source" not in payload

    listed = TS.list_entries(root)
    assert [item["id"] for item in listed] == [entry["id"]]
    assert listed[0]["marker"] is True

    report = TS.status(root)
    assert report["totals"]["entries"] == 1
    assert report["totals"]["bytes"] == listed[0]["bytes"]
    assert report["by_kind"]["scratch"]["entries"] == 1
    assert report["by_kind"]["scratch"]["owned"] == 1
    assert report["by_ownership"]["owned"]["entries"] == 1
    assert report["filesystems"]


def test_default_path_is_under_the_state_dir_not_tmp(root: Path) -> None:
    entry = TS.create(root, reason="scratch")
    path = Path(entry["path"])
    assert path.is_relative_to(TS.state_dir(root))
    assert path.is_relative_to(TS.state_dir(root) / "tmp" / "workspaces")
    assert path.parent == TS.default_scratch_root(root)


def test_session_is_never_invented(root: Path) -> None:
    entry = TS.create(root, reason="no session")
    assert entry["session"] is None
    assert entry["session_source"] == "unknown"
    assert TS.make_session("opencode", None) is None


def test_optional_metadata_and_touch(root: Path, tmp_path: Path) -> None:
    entry = TS.create(root, reason="metadata", model="deepseek-v4",
                      run_id="run-7")
    assert entry["model"] == "deepseek-v4"
    assert entry["run_id"] == "run-7"
    assert entry["session"] is None
    record = _record(root, entry["id"])
    assert record["model"] == "deepseek-v4"
    assert record["run_id"] == "run-7"
    marker = json.loads(
        (Path(entry["path"]) / TS.MARKER_NAME).read_text(encoding="utf-8"))
    assert "model" not in marker and "run_id" not in marker

    external = tmp_path / "metadata-adopt"
    external.mkdir()
    adopted = TS.adopt(root, external, reason="adopt metadata",
                       model="gpt", run_id="run-8")
    assert adopted["model"] == "gpt"
    assert adopted["run_id"] == "run-8"

    _rewrite(root, entry["id"], last_used_at=1)
    TS.get(root, entry["id"])
    TS.list_entries(root)
    TS.status(root)
    assert _record(root, entry["id"])["last_used_at"] == 1
    touched = TS.touch(root, entry["id"])
    assert touched["last_used_at"] > 1
    assert _record(root, entry["id"])["last_used_at"] == touched["last_used_at"]

    with pytest.raises(TS.TempSpaceError):
        TS.create(root, reason="bad model", model="bad\nmodel")
    with pytest.raises(TS.TempNotFound):
        TS.touch(root, "tmp_ffffffffffff")


def test_adopt_unowned_is_sized_but_never_cleaned(root: Path,
                                                  tmp_path: Path) -> None:
    external = tmp_path / "external-unowned"
    external.mkdir()
    try:
        _write(external / "out.bin", 17)
        entry = TS.adopt(root, external, reason="tool output")
        assert entry["owned"] is False
        assert entry["marker"] is False
        assert entry["bytes"] >= 17
        assert entry["files"] >= 1

        plan = TS.clean(root, ids=[entry["id"]], apply=True)
        assert plan["candidates"] == []
        assert plan["deleted"] == []
        assert plan["kept"][0]["why"] == "unowned"
        assert external.is_dir()

        with pytest.raises(TS.TempPathError):
            TS.release(root, entry["id"], delete=True)
        assert external.is_dir()
    finally:
        import shutil
        shutil.rmtree(external, ignore_errors=True)


def test_adopt_owned_mark_then_release_deletes(root: Path,
                                               tmp_path: Path) -> None:
    external = tmp_path / "external-owned"
    external.mkdir()
    try:
        _write(external / "a.txt", 3)
        entry = TS.adopt(root, external, reason="owned tool output",
                         owned=True, mark=True, kind="engine")
        assert entry["owned"] is True
        assert entry["marker"] is True
        marker = json.loads((external / TS.MARKER_NAME).read_text(
            encoding="utf-8"))
        assert marker["id"] == entry["id"]

        released = TS.release(root, entry["id"], delete=True)
        assert released["state"] == "released"
        assert not external.exists()
    finally:
        import shutil
        shutil.rmtree(external, ignore_errors=True)


def test_adopt_refuses_a_conflicting_marker(root: Path,
                                            tmp_path: Path) -> None:
    external = tmp_path / "external-conflict"
    external.mkdir()
    try:
        (external / TS.MARKER_NAME).write_text(
            json.dumps({"version": 1, "id": "tmp_000000000000"}),
            encoding="utf-8")
        with pytest.raises(TS.TempMarkerError):
            TS.adopt(root, external, reason="conflict", owned=True, mark=True)
        assert TS.list_entries(root) == []
    finally:
        import shutil
        shutil.rmtree(external, ignore_errors=True)


def test_adopt_mark_requires_owned(root: Path, tmp_path: Path) -> None:
    external = tmp_path / "external-mark"
    external.mkdir()
    try:
        with pytest.raises(TS.TempSpaceError):
            TS.adopt(root, external, reason="bad", mark=True)
    finally:
        external.rmdir()


def test_release_without_delete_keeps_the_tree(root: Path) -> None:
    entry = TS.create(root, reason="keep")
    released = TS.release(root, entry["id"])
    assert released["state"] == "released"
    assert Path(entry["path"]).is_dir()
    assert TS.list_entries(root) == []
    assert TS.list_entries(root, include_all=True)[0]["state"] == "released"


def test_release_delete_and_missing_are_handled(root: Path) -> None:
    entry = TS.create(root, reason="delete")
    released = TS.release(root, entry["id"], delete=True)
    assert released["state"] == "released"
    assert not Path(entry["path"]).exists()

    other = TS.create(root, reason="already gone")
    import shutil
    shutil.rmtree(other["path"])
    missing = TS.release(root, other["id"], delete=True)
    assert missing["state"] == "missing"


def test_clean_dry_run_then_apply(root: Path) -> None:
    entry = TS.create(root, reason="expired soon", ttl_s=60)
    _rewrite(root, entry["id"], expires_at=1)

    plan = TS.clean(root, expired=True)
    assert plan["dry_run"] is True
    assert [row["id"] for row in plan["candidates"]] == [entry["id"]]
    assert Path(entry["path"]).is_dir()

    done = TS.clean(root, expired=True, apply=True)
    assert done["dry_run"] is False
    assert done["deleted"] == [entry["id"]]
    assert not Path(entry["path"]).exists()
    assert _record(root, entry["id"])["state"] == "released"


def test_clean_default_selects_released_and_missing_not_active(
        root: Path) -> None:
    released = TS.create(root, reason="released")
    TS.release(root, released["id"])
    active = TS.create(root, reason="active")
    missing = TS.create(root, reason="missing")
    import shutil
    shutil.rmtree(missing["path"])

    plan = TS.clean(root)
    selected = {row["id"] for row in plan["candidates"]}
    assert selected == {released["id"]}
    assert _kept_reason(plan, active["id"]) == "not-selected"
    assert _kept_reason(plan, missing["id"]) == "missing"


def test_clean_selection_older_than_and_ids(root: Path) -> None:
    old = TS.create(root, reason="old")
    new = TS.create(root, reason="new")
    _rewrite(root, old["id"], last_used_at=1)

    plan = TS.clean(root, older_than_s=100)
    assert [row["id"] for row in plan["candidates"]] == [old["id"]]
    assert any(row["why"] == "not-selected" for row in plan["kept"])

    by_id = TS.clean(root, ids=[new["id"]], apply=True)
    assert by_id["deleted"] == [new["id"]]
    assert Path(old["path"]).is_dir()


def test_clean_max_total_bytes_chooses_oldest(root: Path) -> None:
    old = TS.create(root, reason="old")
    new = TS.create(root, reason="new")
    _write(Path(old["path"]) / "old.bin", 100)
    _write(Path(new["path"]) / "new.bin", 300)
    _rewrite(root, old["id"], last_used_at=100)
    _rewrite(root, new["id"], last_used_at=200)
    entries = {item["id"]: item for item in TS.list_entries(root)}
    total = entries[old["id"]]["bytes"] + entries[new["id"]]["bytes"]

    plan = TS.clean(root, max_total_bytes=total - entries[old["id"]]["bytes"])
    assert [row["id"] for row in plan["candidates"]] == [old["id"]]


def test_clean_missing_path_is_reported_without_failing(root: Path) -> None:
    entry = TS.create(root, reason="gone")
    import shutil
    shutil.rmtree(entry["path"])

    plan = TS.clean(root, ids=[entry["id"]], apply=True)
    assert plan["candidates"] == []
    assert plan["kept"][0]["why"] == "missing"
    assert _record(root, entry["id"])["state"] == "missing"


def test_clean_keeps_symlink_marker_mismatch_and_protected(
        root: Path, tmp_path: Path, monkeypatch) -> None:
    entry = TS.create(root, reason="symlink target")
    target = tmp_path / "outside"
    target.mkdir()
    _write(target / "payload", 11)
    import shutil
    shutil.rmtree(entry["path"])
    os.symlink(target, entry["path"])
    plan = TS.clean(root, ids=[entry["id"]])
    assert plan["candidates"] == []
    assert _kept_reason(plan, entry["id"]) == "symlink"
    assert target.is_dir()

    marked = TS.create(root, reason="marker mismatch")
    marker = Path(marked["path"]) / TS.MARKER_NAME
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["id"] = "tmp_ffffffffffff"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    plan = TS.clean(root, ids=[marked["id"]])
    assert _kept_reason(plan, marked["id"]) == "marker-mismatch"

    repo_entry = TS.create(root, reason="protect repo")
    _rewrite(root, repo_entry["id"], path=str(root))
    plan = TS.clean(root, ids=[repo_entry["id"]])
    assert _kept_reason(plan, repo_entry["id"]) == "repository-path"

    outside = TS.create(root, reason="outside roots")
    monkeypatch.setattr(TS, "allowed_temp_roots",
                        lambda value: (TS.registry_dir(value),))
    plan = TS.clean(root, ids=[outside["id"]])
    assert _kept_reason(plan, outside["id"]) == "outside-allowed-root"


def test_clean_refuses_a_repository_path_outside_state(root: Path) -> None:
    entry = TS.create(root, reason="repository path")
    inside = root / "src"
    inside.mkdir()
    _rewrite(root, entry["id"], path=str(inside))
    plan = TS.clean(root, ids=[entry["id"]])
    assert _kept_reason(plan, entry["id"]) == "repository-path"


def test_clean_refuses_the_managed_tmp_root(root: Path) -> None:
    entry = TS.create(root, reason="managed root")
    managed_tmp = TS.state_dir(root) / "tmp"
    _rewrite(root, entry["id"], path=str(managed_tmp))
    plan = TS.clean(root, ids=[entry["id"]])
    assert _kept_reason(plan, entry["id"]) == "protected-path"


def test_clean_rejects_a_resolved_dotdot_escape(root: Path, tmp_path: Path,
                                                monkeypatch) -> None:
    entry = TS.create(root, reason="escape")
    link = tmp_path / "escape"
    link.symlink_to("/etc", target_is_directory=True)
    escaped = str(link / ".." / "etc")
    _rewrite(root, entry["id"], path=escaped)
    monkeypatch.setattr(TS, "walk_usage", lambda *a, **k: {
        "bytes": 0, "files": 0, "walk": "complete", "unknown": False,
        "errors": 0})
    plan = TS.clean(root, ids=[entry["id"]])
    assert _kept_reason(plan, entry["id"]) == "outside-allowed-root"


def test_clean_refuses_home_and_filesystem_root(root: Path, monkeypatch) -> None:
    entry = TS.create(root, reason="home")
    monkeypatch.setattr(TS, "walk_usage", lambda *a, **k: {
        "bytes": 0, "files": 0, "walk": "complete", "unknown": False,
        "errors": 0})
    _rewrite(root, entry["id"], path=str(Path.home()))
    monkeypatch.setattr(
        TS, "allowed_temp_roots",
        lambda value: (Path("/"), Path.home(), TS.registry_dir(value), *(
            TS.default_kind_root(value, kind) for kind in TS.KINDS)))
    plan = TS.clean(root, ids=[entry["id"]])
    assert plan["kept"][0]["why"] == "protected-path"
    _rewrite(root, entry["id"], path="/")
    plan = TS.clean(root, ids=[entry["id"]])
    assert plan["kept"][0]["why"] == "protected-path"


def test_walk_usage_counts_files_and_ignores_symlinks(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    _write(tree / "a.txt", 5)
    _write(tree / "nested" / "b.txt", 7)
    outside = tmp_path / "outside.bin"
    _write(outside, 1000)
    os.symlink(outside, tree / "link.bin")
    os.symlink(tree / "nested", tree / "linkdir")

    usage = TS.walk_usage(tree)
    assert usage == {"bytes": 12, "files": 2, "walk": "complete",
                     "unknown": False, "errors": 0}


def test_walk_usage_marks_partial_at_a_low_bound(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    for index in range(5):
        _write(tree / f"{index}.txt", 1)
    usage = TS.walk_usage(tree, max_entries=2)
    assert usage["walk"] == "partial"
    assert usage["files"] <= 2
    assert usage["bytes"] <= 2


def test_walk_usage_missing_path_is_zero_and_complete(tmp_path: Path) -> None:
    usage = TS.walk_usage(tmp_path / "absent")
    assert usage == {"bytes": 0, "files": 0, "walk": "complete",
                     "unknown": False, "errors": 0}


def test_audit_reports_legacy_artifacts_read_only(root: Path,
                                                 tmp_path: Path) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()

    agent = scan / "ozagia_agent" / "run-1"
    _write(agent / "stdout.log", 10)
    lock = scan / "ozagia-eval-a.sock.lock"
    _write(lock, 5)
    _write(scan / "ozagia-eval-b.sock", 7)
    _write(scan / "oz-idle-c.sock.lock", 3)
    _write(scan / "oz-jobidle-d.sock.lock", 4)
    _write(scan / "ozagia_check_e", 6)
    _write(scan / "ozagia-native-f", 8)
    _write(scan / "ozagia_eval" / "run-2" / "out", 9)
    _write(scan / "ozagia_index_bench" / "run-3" / "out", 11)
    _write(scan / "ozagia_iac_bench" / "run-4" / "out", 12)
    os.utime(lock, (1000, 1000))
    (scan / "opencode").mkdir()
    _write(scan / "opencode" / "ignored", 100)
    (scan / "unrelated").mkdir()
    _write(scan / "unrelated" / "ignored", 100)

    registered_dir = scan / "ozagia_eval" / "registered"
    registered_dir.mkdir()
    _write(registered_dir / "out", 1)
    adopted = TS.adopt(root, registered_dir, reason="legacy eval run")
    before = sorted(TS.registry_dir(root).glob("*.json"))

    result = TS.audit(root, roots=[scan], now=10_000)
    patterns = {item["pattern"] for item in result["artifacts"]}
    assert patterns == set(TS.AUDIT_PATTERNS)
    assert result["totals"]["artifacts"] == 11
    assert result["count"] == 11
    assert result["bytes"] == result["totals"]["bytes"]
    assert result["totals"]["registered"] == 1
    assert result["totals"]["covered"] == 0
    assert result["totals"]["untracked"] == 10
    assert result["totals"]["bytes"] > 0
    assert result["age"]["oldest_path"] == str(lock)
    assert result["age"]["oldest_s"] == 9_000
    registered = [item for item in result["artifacts"]
                  if item["registered"]]
    assert registered[0]["path"] == str(registered_dir)
    assert registered[0]["registered_entry"] == adopted["id"]
    assert str(scan / "opencode") not in {
        item["path"] for item in result["artifacts"]}
    assert str(scan / "unrelated") not in {
        item["path"] for item in result["artifacts"]}
    assert result["truncated"] is False
    assert sorted(TS.registry_dir(root).glob("*.json")) == before
    assert agent.is_dir() and lock.is_file()


def test_audit_never_follows_symlinks_and_honours_bounds(root: Path,
                                                         tmp_path: Path,
                                                         monkeypatch) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    outside = tmp_path / "outside"
    _write(outside / "payload", 1000)
    os.symlink(outside, scan / "ozagia-native-link")
    _write(scan / "ozagia-native-file", 2)

    result = TS.audit(root, roots=[scan])
    by_path = {item["path"]: item for item in result["artifacts"]}
    link = by_path[str(scan / "ozagia-native-link")]
    assert link["kind"] == "symlink"
    assert link["bytes"] == 0 and link["files"] == 0
    assert result["totals"]["bytes"] == 2

    truncated = TS.audit(root, roots=[scan], max_artifacts=1)
    assert truncated["truncated"] is True
    assert len(truncated["artifacts"]) == 1

    excluded = scan / "excluded-opencode"
    _write(excluded / "ozagia-native-hidden", 5)
    monkeypatch.setattr(TS, "_audit_excluded_roots",
                        lambda: (excluded.resolve(),))
    result = TS.audit(root, roots=[scan, excluded])
    assert str(excluded / "ozagia-native-hidden") not in {
        item["path"] for item in result["artifacts"]}


def test_tmpfs_detection_uses_mountinfo(tmp_path: Path, monkeypatch) -> None:
    mountinfo = "\n".join([
        "1 0 8:1 / / rw,relatime - ext4 /dev/sda1 rw",
        "36 35 0:36 / /tmp rw,nosuid,nodev - tmpfs tmpfs rw,size=64m",
        "40 35 8:2 / /var/tmp rw,relatime - ext4 /dev/sdb1 rw",
    ])
    assert TS.tmpfs_for("/tmp/x", mountinfo=mountinfo) is True
    assert TS.tmpfs_for("/var/tmp/x", mountinfo=mountinfo) is False
    assert TS.tmpfs_for("/home/x", mountinfo=mountinfo) is False

    monkeypatch.setattr(TS, "_read_mountinfo", lambda: mountinfo)
    monkeypatch.setattr(TS, "_read_mounts", lambda: None)
    assert TS.tmpfs_for("/tmp/x") is True
    assert TS.tmpfs_for("/home/x") is False


def test_concurrent_creates_produce_unique_entries(root: Path) -> None:
    def one(index: int) -> str:
        return TS.create(root, reason=f"thread {index}")["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(one, range(16)))
    assert len(set(ids)) == 16
    entries = TS.list_entries(root)
    assert len(entries) == 16
    assert len({entry["path"] for entry in entries}) == 16
    assert len(list(TS.registry_dir(root).glob("*.json"))) == 16


def test_prune_registry_marks_and_removes_only_finished_records(root: Path) -> None:
    expired = TS.create(root, reason="expired", ttl_s=60)
    _rewrite(root, expired["id"], expires_at=1)
    missing = TS.create(root, reason="missing")
    import shutil
    shutil.rmtree(missing["path"])
    active = TS.create(root, reason="active")

    plan = TS.prune_registry(root)
    assert expired["id"] in plan["marked_expired"]
    assert missing["id"] in plan["marked_missing"]
    assert missing["id"] in plan["removed"]
    assert active["id"] in plan["kept"]

    done = TS.prune_registry(root, apply=True)
    assert missing["id"] in done["removed"]
    assert not (TS.registry_dir(root) / f"{missing['id']}.json").exists()
    assert (TS.registry_dir(root) / f"{active['id']}.json").exists()
    assert _record(root, expired["id"])["state"] == "expired"


def test_suggest_path_is_a_non_created_managed_path(root: Path) -> None:
    suggested = TS.suggest_path(root, "scratch", "fixture dump",
                                {"harness": "opencode", "id": "s1"})
    assert suggested.is_relative_to(TS.default_scratch_root(root))
    assert not suggested.exists()
    assert suggested == TS.suggest_path(root, "scratch", "fixture dump",
                                        {"harness": "opencode", "id": "s1"})


def test_clean_legacy_dry_run_then_apply(root: Path, tmp_path: Path) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    lock = scan / "ozagia-eval-old.sock.lock"
    _write(lock, 5)
    idle = scan / "oz-idle-old.sock.lock"
    _write(idle, 3)
    jobidle = scan / "oz-jobidle-old.sock.lock"
    _write(jobidle, 4)
    sockdir = _short_socket_dir()
    stale = sockdir / "ozagia-eval-old.sock"
    _stale_socket(stale)
    agent = scan / "ozagia_agent" / "run"
    _write(agent / "log", 10)
    eval_run = scan / "ozagia_eval" / "run"
    _write(eval_run / "out", 9)
    check = scan / "ozagia_check_old"
    _write(check / "out", 6)
    native = scan / "ozagia-native-old"
    _write(native / "out", 8)
    recent = scan / "ozagia-eval-recent.sock.lock"
    _write(recent, 2)
    cache = scan / "ozagia_index_bench" / "run"
    _write(cache / "out", 11)
    eligible = {lock, idle, jobidle, stale, agent, eval_run, check, native}
    for path in eligible:
        _old(path)
    _old(cache)
    before = sorted(scan.rglob("*"))

    try:
        plan = TS.clean_legacy(root, roots=[scan, sockdir])
        assert plan["dry_run"] is True
        assert plan["registry_written"] is False
        assert {row["path"] for row in plan["candidates"]} == {
            str(path) for path in eligible}
        assert {row["path"] for row in plan["kept"]} == {
            str(recent), str(cache)}
        assert plan["totals"]["candidates"] == len(eligible)
        assert plan["totals"]["deleted"] == 0
        assert plan["totals"]["bytes"] > 0
        assert sorted(scan.rglob("*")) == before

        done = TS.clean_legacy(root, roots=[scan, sockdir], apply=True)
        assert done["dry_run"] is False
        assert set(done["deleted"]) == {str(path) for path in eligible}
        assert done["totals"]["deleted"] == len(eligible)
        assert done["totals"]["errors"] == 0
        for path in eligible:
            assert not path.exists()
        assert recent.exists() and cache.is_dir()
        assert list(TS.registry_dir(root).glob("*.json")) == []
    finally:
        shutil.rmtree(sockdir, ignore_errors=True)


def test_clean_legacy_respects_age_and_older_than(root: Path,
                                                  tmp_path: Path) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    recent = scan / "ozagia-eval-recent.sock.lock"
    _write(recent, 2)
    old = scan / "ozagia-eval-old.sock.lock"
    _write(old, 3)
    _old(old)

    plan = TS.clean_legacy(root, roots=[scan])
    assert [row["path"] for row in plan["candidates"]] == [str(old)]
    assert _rows(plan)[str(recent)]["why"] == "too-recent"

    every_age = TS.clean_legacy(root, roots=[scan], older_than_s=0)
    assert {row["path"] for row in every_age["candidates"]} == {
        str(recent), str(old)}

    nothing = TS.clean_legacy(root, roots=[scan], older_than_s=10 ** 11)
    assert nothing["candidates"] == []
    assert _rows(nothing)[str(old)]["why"] == "too-recent"


def test_clean_legacy_keeps_a_held_lock(root: Path, tmp_path: Path) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    lock = scan / "ozagia-eval-held.sock.lock"
    _write(lock, 3)
    _old(lock)
    descriptor = os.open(lock, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = TS.clean_legacy(root, roots=[scan], older_than_s=0)
        assert _rows(plan)[str(lock)]["why"] == "lock-held"
        assert plan["candidates"] == []

        done = TS.clean_legacy(root, roots=[scan], older_than_s=0,
                               apply=True)
        assert done["deleted"] == []
        assert lock.exists()
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    released = TS.clean_legacy(root, roots=[scan], older_than_s=0)
    assert [row["path"] for row in released["candidates"]] == [str(lock)]


def test_clean_legacy_keeps_live_socket_and_removes_stale(root: Path,
                                                          tmp_path: Path) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    sockdir = _short_socket_dir()
    live = sockdir / "ozagia-eval-live.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(live))
    server.listen(1)
    _old(live)
    stale = sockdir / "ozagia-eval-stale.sock"
    _stale_socket(stale)
    _old(stale)
    try:
        plan = TS.clean_legacy(root, roots=[scan, sockdir], older_than_s=0)
        rows = _rows(plan)
        assert rows[str(live)]["why"] == "socket-live"
        assert rows[str(live)]["action"] == "keep"
        assert rows[str(stale)]["action"] == "delete"

        done = TS.clean_legacy(root, roots=[scan, sockdir], older_than_s=0,
                               apply=True)
        assert done["deleted"] == [str(stale)]
        assert not stale.exists()
        assert live.exists()
    finally:
        server.close()
        live.unlink(missing_ok=True)
        shutil.rmtree(sockdir, ignore_errors=True)


def test_clean_legacy_keeps_registered_and_ancestor_paths(
        root: Path, tmp_path: Path) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    registered = scan / "ozagia_eval" / "registered"
    _write(registered / "out", 1)
    _old(registered)
    ancestor_root = scan / "ozagia_agent"
    ancestor = ancestor_root / "run"
    _write(ancestor / "log", 2)
    _old(ancestor)
    TS.adopt(root, registered, reason="registered legacy")
    TS.adopt(root, ancestor_root, reason="registered ancestor")

    plan = TS.clean_legacy(root, roots=[scan], older_than_s=0)
    rows = _rows(plan)
    assert rows[str(registered)]["why"] == "registered"
    assert rows[str(ancestor)]["why"] == "registered-ancestor"
    assert plan["candidates"] == []


def test_clean_legacy_refuses_state_and_managed_roots(root: Path) -> None:
    state = Path(TS.state_dir(root))
    state_artifact = state / "ozagia-native-state"
    _write(state_artifact / "out", 1)
    _old(state_artifact)
    tmp_root = TS.state_path(root, TS.TMP_DIRNAME)
    tmp_artifact = tmp_root / "ozagia-native-tmp"
    _write(tmp_artifact / "out", 1)
    _old(tmp_artifact)
    workspaces = TS.workspaces_root(root)
    managed_artifact = workspaces / "ozagia-native-managed"
    _write(managed_artifact / "out", 1)
    _old(managed_artifact)

    plan = TS.clean_legacy(
        root, roots=[state, tmp_root, workspaces], older_than_s=0)
    rows = _rows(plan)
    for path in (state_artifact, tmp_artifact, managed_artifact):
        assert rows[str(path)]["why"] == "protected-path"
    assert plan["candidates"] == []


def test_clean_legacy_keeps_symlinks_files_and_unknown_patterns(
        root: Path, tmp_path: Path, monkeypatch) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    outside = tmp_path / "outside"
    _write(outside / "payload", 100)
    link = scan / "ozagia-native-link"
    link.symlink_to(outside, target_is_directory=True)
    check_file = scan / "ozagia_check_file"
    _write(check_file, 4)
    _old(check_file)

    plan = TS.clean_legacy(root, roots=[scan], older_than_s=0)
    rows = _rows(plan)
    assert rows[str(link)]["why"] == "symlink"
    assert rows[str(check_file)]["why"] == "not-a-directory"
    assert plan["candidates"] == []
    assert (outside / "payload").is_file()

    mystery = scan / "mystery-artifact"
    mystery.write_bytes(b"m")

    def fake_audit(*args, **kwargs):
        return {
            "scan_roots": [str(scan)],
            "excluded_roots": [],
            "artifacts": [{
                "path": str(mystery),
                "pattern": "mystery-*",
                "kind": "file",
                "bytes": 1,
                "files": 1,
                "age_s": 999_999,
                "mtime": 1,
                "registered": False,
                "registered_ancestor": None,
            }],
            "truncated": False,
            "warnings": [],
        }

    monkeypatch.setattr(TS, "audit", fake_audit)
    unknown = TS.clean_legacy(root, roots=[scan], older_than_s=0)
    assert [row["why"] for row in unknown["kept"]] == ["unknown-pattern"]


def test_clean_legacy_caches_need_include_caches(root: Path,
                                                 tmp_path: Path) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    caches = []
    for parent in ("ozagia_index_bench", "ozagia_iac_bench"):
        cache = scan / parent / "run"
        _write(cache / "report.json", 12)
        _old(cache)
        caches.append(cache)

    plan = TS.clean_legacy(root, roots=[scan], older_than_s=0)
    assert plan["candidates"] == []
    assert {row["why"] for row in plan["kept"]} == {"cache-disabled"}

    included = TS.clean_legacy(root, roots=[scan], older_than_s=0,
                               include_caches=True)
    assert {row["path"] for row in included["candidates"]} == {
        str(path) for path in caches}
    assert included["include_caches"] is True

    done = TS.clean_legacy(root, roots=[scan], older_than_s=0,
                           include_caches=True, apply=True)
    assert set(done["deleted"]) == {str(path) for path in caches}
    for path in caches:
        assert not path.exists()


def test_clean_legacy_never_considers_opencode_roots(
        root: Path, tmp_path: Path, monkeypatch) -> None:
    excluded_defaults = {str(path) for path in TS._audit_excluded_roots()}
    assert str(Path("/tmp/opencode")) in excluded_defaults
    assert str((Path.home() / ".local" / "share" / "opencode")) \
        in excluded_defaults

    scan = tmp_path / "scan"
    scan.mkdir()
    visible = scan / "ozagia-native-visible"
    _write(visible / "out", 2)
    _old(visible)
    excluded = tmp_path / "opencode"
    hidden = excluded / "ozagia-native-hidden"
    _write(hidden / "out", 3)
    _old(hidden)
    monkeypatch.setattr(TS, "_audit_excluded_roots",
                        lambda: (excluded.resolve(),))

    plan = TS.clean_legacy(root, roots=[scan, excluded], older_than_s=0)
    paths = {row["path"] for row in plan["artifacts"]}
    assert str(hidden) not in paths
    assert str(visible) in paths
    assert plan["candidates"] == [row for row in plan["artifacts"]
                                  if row["path"] == str(visible)]


def test_clean_legacy_deletion_failure_is_reported_without_aborting(
        root: Path, tmp_path: Path, monkeypatch) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    bad = scan / "ozagia-native-bad"
    _write(bad / "out", 1)
    _old(bad)
    good = scan / "ozagia-native-good"
    _write(good / "out", 1)
    _old(good)
    real_remove = TS._legacy_remove

    def flaky_remove(path: Path, *, directory: bool) -> None:
        if path.name == "ozagia-native-bad":
            raise OSError("simulated failure")
        real_remove(path, directory=directory)

    monkeypatch.setattr(TS, "_legacy_remove", flaky_remove)
    done = TS.clean_legacy(root, roots=[scan], older_than_s=0, apply=True)

    assert not good.exists()
    assert bad.exists()
    assert str(good) in done["deleted"]
    assert done["totals"]["errors"] == 1
    assert "simulated failure" in done["errors"][0]
    assert any(row["why"].startswith("delete-failed")
               for row in done["kept"])


def test_clean_legacy_reports_truncation(root: Path, tmp_path: Path) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    for index in range(3):
        path = scan / f"ozagia-native-{index}"
        _write(path / "out", 1)
        _old(path)

    plan = TS.clean_legacy(root, roots=[scan], max_artifacts=1)
    assert plan["truncated"] is True
    assert plan["totals"]["truncated"] is True
    assert len(plan["artifacts"]) == 1
