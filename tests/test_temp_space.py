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


def _usage_stub(**changes) -> dict:
    usage = {
        "allocated_bytes": 0, "apparent_bytes": 0, "files": 0, "inodes": 0,
        "hardlinks": 0, "walk": "complete", "unknown": False, "errors": 0,
        "allocation": "blocks", "cross_device": False,
    }
    usage.update(changes)
    return usage


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
    usage = TS.walk_usage(path)
    assert entry["bytes"] == usage["allocated_bytes"]
    assert entry["apparent_bytes"] == usage["apparent_bytes"]
    assert entry["files"] == usage["files"]
    assert entry["inodes"] == usage["inodes"]
    assert entry["hardlinks"] == 0
    assert entry["allocation"] in ("blocks", "apparent-fallback")
    for field in ("apparent_bytes", "files", "inodes", "hardlinks",
                  "allocation"):
        assert record[field] == entry[field]
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
    assert report["totals"]["apparent_bytes"] == listed[0]["apparent_bytes"]
    assert report["totals"]["inodes"] == listed[0]["inodes"]
    assert report["by_kind"]["scratch"]["entries"] == 1
    assert report["by_kind"]["scratch"]["owned"] == 1
    assert report["by_ownership"]["owned"]["entries"] == 1
    assert report["filesystems"]


def test_old_registry_record_is_recomputed_with_new_fields(
        root: Path) -> None:
    entry = TS.create(root, reason="old record")
    _write(Path(entry["path"]) / "blob.bin", 100)
    record = _record(root, entry["id"])
    for field in ("apparent_bytes", "inodes", "hardlinks", "allocation"):
        record.pop(field, None)
    record["bytes"] = 123_456
    (TS.registry_dir(root) / f"{entry['id']}.json").write_text(
        json.dumps(record), encoding="utf-8")

    got = TS.get(root, entry["id"])
    usage = TS.walk_usage(Path(entry["path"]))
    assert got["bytes"] == usage["allocated_bytes"]
    assert got["bytes"] != 123_456
    assert got["apparent_bytes"] == usage["apparent_bytes"]
    assert got["apparent_bytes"] > 100
    assert got["files"] == 2
    assert got["inodes"] == 3
    assert got["hardlinks"] == 0
    assert got["allocation"] in ("blocks", "apparent-fallback")


def test_status_reports_inode_capacity_and_pressure(root: Path,
                                                    monkeypatch) -> None:
    TS.create(root, reason="inode pressure")

    class FakeVFS:
        f_blocks = 1000
        f_bavail = 900
        f_frsize = 4096
        f_files = 100
        f_ffree = 5

    monkeypatch.setattr(TS.os, "statvfs", lambda path: FakeVFS())
    report = TS.status(root)
    filesystem = report["filesystems"][0]
    assert filesystem["total_inodes"] == 100
    assert filesystem["free_inodes"] == 5
    assert filesystem["inode_percent"] == 95.0
    assert filesystem["inode_warning"] is not None
    assert any("free inodes below 10%" in warning
               for warning in report["warnings"])


def test_status_skips_inode_warning_without_inode_accounting(
        root: Path, monkeypatch) -> None:
    TS.create(root, reason="no inode accounting")

    class FakeVFS:
        f_blocks = 1000
        f_bavail = 900
        f_frsize = 4096
        f_files = 0
        f_ffree = 0

    monkeypatch.setattr(TS.os, "statvfs", lambda path: FakeVFS())
    report = TS.status(root)
    filesystem = report["filesystems"][0]
    assert filesystem["total_inodes"] == 0
    assert filesystem["free_inodes"] == 0
    assert filesystem["inode_percent"] is None
    assert filesystem["inode_warning"] is None
    assert not any("inodes" in warning for warning in report["warnings"])


def test_list_entries_sorts_by_size_files_inodes_and_age(root: Path) -> None:
    small = TS.create(root, reason="small")
    medium = TS.create(root, reason="medium")
    large = TS.create(root, reason="large")
    _write(Path(small["path"]) / "a.bin", 10)
    _write(Path(medium["path"]) / "a.bin", 9000)
    for index in range(3):
        _write(Path(large["path"]) / f"{index}.bin", 20_000)
    _rewrite(root, small["id"], last_used_at=100)
    _rewrite(root, medium["id"], last_used_at=200)
    _rewrite(root, large["id"], last_used_at=300)

    for sort, field in (("allocated", "bytes"),
                        ("apparent", "apparent_bytes"),
                        ("files", "files"),
                        ("inodes", "inodes")):
        entries = TS.list_entries(root, sort=sort)
        values = [entry[field] for entry in entries]
        assert values == sorted(values, reverse=True)
        assert entries[0]["id"] == large["id"]

    by_age = TS.list_entries(root, sort="age")
    assert [entry["id"] for entry in by_age] == [
        small["id"], medium["id"], large["id"]]
    assert [entry["id"] for entry in TS.list_entries(root, sort="created")] \
        == [entry["id"] for entry in TS.list_entries(root)]


def test_status_top_n_returns_the_largest_by_allocated(root: Path) -> None:
    small = TS.create(root, reason="small")
    medium = TS.create(root, reason="medium")
    large = TS.create(root, reason="large")
    _write(Path(small["path"]) / "a.bin", 10)
    _write(Path(medium["path"]) / "a.bin", 9000)
    _write(Path(large["path"]) / "a.bin", 20_000)

    assert "top" not in TS.status(root)
    report = TS.status(root, top=2)
    rows = report["top"]
    assert [row["id"] for row in rows] == [large["id"], medium["id"]]
    assert rows[0]["bytes"] >= rows[1]["bytes"]
    for row in rows:
        assert row["reason"]
        assert row["path"].startswith(str(TS.state_dir(root)))
        assert row["session"] is None
        assert row["allocated_bytes"] == row["bytes"]
        assert row["apparent_bytes"] >= 0
        assert row["files"] >= 1
        assert row["inodes"] >= 1

    assert TS.status(root, top=0)["top"] == []


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
    monkeypatch.setattr(TS, "walk_usage", lambda *a, **k: _usage_stub())
    plan = TS.clean(root, ids=[entry["id"]])
    assert _kept_reason(plan, entry["id"]) == "outside-allowed-root"


def test_clean_refuses_home_and_filesystem_root(root: Path, monkeypatch) -> None:
    entry = TS.create(root, reason="home")
    monkeypatch.setattr(TS, "walk_usage", lambda *a, **k: _usage_stub())
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
    assert usage["files"] == 2
    assert usage["apparent_bytes"] == 12
    assert usage["allocated_bytes"] > 0
    # tree, a.txt, nested, b.txt, link.bin and linkdir each have an inode.
    assert usage["inodes"] == 6
    assert usage["hardlinks"] == 0
    assert usage["walk"] == "complete"
    assert usage["unknown"] is False
    assert usage["errors"] == 0
    assert usage["allocation"] == "blocks"


def test_walk_usage_reports_sparse_allocated_and_apparent(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    sparse = tree / "sparse.bin"
    with sparse.open("wb") as handle:
        handle.seek(8 * 1024 * 1024)
        handle.write(b"x")
    apparent = sparse.stat().st_size

    usage = TS.walk_usage(tree)
    assert usage["files"] == 1
    assert usage["apparent_bytes"] == apparent
    assert usage["allocation"] == "blocks"
    assert usage["allocated_bytes"] < usage["apparent_bytes"]
    assert usage["allocated_bytes"] > 0


def test_walk_usage_counts_a_hard_link_once(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    first = tree / "first.bin"
    first.write_bytes(b"y" * 32)
    second = tree / "second.bin"
    os.link(first, second)

    usage = TS.walk_usage(tree)
    assert usage["files"] == 2
    assert usage["apparent_bytes"] == 32
    assert usage["hardlinks"] == 1
    # The tree directory plus the shared regular-file inode.
    assert usage["inodes"] == 2
    stat_result = first.stat()
    if stat_result.st_blocks:
        assert usage["allocation"] == "blocks"
        assert usage["allocated_bytes"] == stat_result.st_blocks * 512


def test_walk_usage_counts_symlink_inodes_without_following(
        tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "regular.bin").write_bytes(b"z" * 5)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"o" * 8192)
    os.symlink(outside, tree / "outside-link")
    os.symlink(tree, tree / "self-link")

    usage = TS.walk_usage(tree)
    assert usage["files"] == 1
    assert usage["apparent_bytes"] == 5
    assert usage["hardlinks"] == 0
    # tree, regular.bin and the two symlinks.
    assert usage["inodes"] == 4
    assert usage["allocated_bytes"] < 8192


def test_walk_usage_counts_directory_and_empty_file_inodes(
        tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "empty-a").write_bytes(b"")
    (tree / "sub" / "empty-b").write_bytes(b"")

    usage = TS.walk_usage(tree)
    assert usage["files"] == 2
    assert usage["apparent_bytes"] == 0
    assert usage["allocated_bytes"] == 0
    # tree, empty-a, sub and empty-b.
    assert usage["inodes"] == 4


def test_walk_usage_reports_many_empty_files_as_inodes(
        tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    for index in range(900):
        (tree / f"lock-{index}").write_bytes(b"")

    usage = TS.walk_usage(tree, max_entries=1000)
    assert usage["files"] == 900
    assert usage["inodes"] == 901
    assert usage["allocated_bytes"] == 0
    assert usage["apparent_bytes"] == 0


def test_walk_usage_skips_a_cross_device_child(tmp_path: Path,
                                              monkeypatch) -> None:
    import types

    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "local.bin").write_bytes(b"abc")
    root_stat = os.lstat(tree)
    foreign_stat = types.SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600, st_dev=root_stat.st_dev + 1,
        st_ino=999_999_999, st_size=5000, st_blocks=16)
    real_scandir = os.scandir

    class ForeignEntry:
        path = str(tree / "foreign.bin")

        @staticmethod
        def stat(*, follow_symlinks: bool = True):
            return foreign_stat

    class FakeScan:
        def __init__(self, entries):
            self.entries = entries

        def __iter__(self):
            return iter(self.entries)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_scandir(path):
        if Path(path) == tree:
            with real_scandir(tree) as iterator:
                local = list(iterator)
            return FakeScan([*local, ForeignEntry()])
        return real_scandir(path)

    monkeypatch.setattr(TS.os, "scandir", fake_scandir)
    usage = TS.walk_usage(tree)
    assert usage["cross_device"] is True
    assert usage["walk"] == "partial"
    assert usage["unknown"] is True
    assert usage["files"] == 1
    assert usage["apparent_bytes"] == 3
    assert usage["inodes"] == 2


def test_walk_usage_falls_back_when_blocks_are_unavailable(
        tmp_path: Path, monkeypatch) -> None:
    tree = tmp_path / "tree"
    _write(tree / "a.bin", 10)
    monkeypatch.setattr(TS, "_allocated_blocks", lambda info: None)

    usage = TS.walk_usage(tree)
    assert usage["allocation"] == "apparent-fallback"
    assert usage["allocated_bytes"] == 10
    assert usage["apparent_bytes"] == 10


def test_walk_usage_treats_zero_blocks_as_sparse_on_posix(
        tmp_path: Path, monkeypatch) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    _write(tree / "a.bin", 10)
    _write(tree / "b.bin", 20)
    monkeypatch.setattr(TS, "_allocated_blocks", lambda info: 0)
    monkeypatch.setattr(TS, "TRUST_ST_BLOCKS", True)

    usage = TS.walk_usage(tree)
    assert usage["allocation"] == "blocks"
    assert usage["allocated_bytes"] == 0
    assert usage["apparent_bytes"] == 30


def test_walk_usage_falls_back_when_non_posix_blocks_are_all_zero(
        tmp_path: Path, monkeypatch) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    _write(tree / "a.bin", 10)
    _write(tree / "b.bin", 20)
    monkeypatch.setattr(TS, "_allocated_blocks", lambda info: 0)
    monkeypatch.setattr(TS, "TRUST_ST_BLOCKS", False)

    usage = TS.walk_usage(tree)
    assert usage["allocation"] == "apparent-fallback"
    assert usage["allocated_bytes"] == 30
    assert usage["apparent_bytes"] == 30


def test_walk_usage_reports_a_fully_sparse_file_as_zero_allocated(
        tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    sparse = tree / "fully-sparse.bin"
    with sparse.open("wb") as handle:
        handle.truncate(8 * 1024 * 1024)
    if sparse.stat().st_blocks != 0:
        pytest.skip("filesystem does not create fully sparse files")

    usage = TS.walk_usage(tree)
    assert usage["allocation"] == "blocks"
    assert usage["allocated_bytes"] == 0
    assert usage["apparent_bytes"] == 8 * 1024 * 1024


def test_walk_usage_unreadable_entry_is_partial(tmp_path: Path,
                                                monkeypatch) -> None:
    tree = tmp_path / "tree"
    _write(tree / "a.bin", 5)
    real_scandir = os.scandir

    def deny(path):
        if Path(path) == tree:
            raise PermissionError("simulated denial")
        return real_scandir(path)

    monkeypatch.setattr(TS.os, "scandir", deny)
    usage = TS.walk_usage(tree)
    assert usage["walk"] == "partial"
    assert usage["unknown"] is True
    assert usage["errors"] == 1
    assert usage["files"] == 0
    assert usage["inodes"] == 1


def test_walk_usage_marks_partial_at_a_low_bound(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    for index in range(5):
        _write(tree / f"{index}.txt", 1)
    usage = TS.walk_usage(tree, max_entries=2)
    assert usage["walk"] == "partial"
    assert usage["files"] <= 2
    assert usage["apparent_bytes"] <= 2
    assert usage["allocated_bytes"] >= usage["apparent_bytes"]


def test_walk_usage_missing_path_is_zero_and_complete(tmp_path: Path) -> None:
    usage = TS.walk_usage(tmp_path / "absent")
    assert usage["allocated_bytes"] == 0
    assert usage["apparent_bytes"] == 0
    assert usage["files"] == 0
    assert usage["inodes"] == 0
    assert usage["hardlinks"] == 0
    assert usage["allocation"] == "blocks"
    assert usage["walk"] == "complete"
    assert usage["unknown"] is False
    assert usage["errors"] == 0
    assert usage["cross_device"] is False


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
    assert result["totals"]["bytes"] >= 2
    assert result["totals"]["apparent_bytes"] == 2

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


# --------------------------------------------------------------------------- #
# leases, pinning and budgets (Stage B)
# --------------------------------------------------------------------------- #


def test_held_lease_keeps_an_expired_entry_through_clean_gc_and_release(
        root: Path) -> None:
    entry = TS.create(root, reason="leased", ttl_s=60)
    _rewrite(root, entry["id"], expires_at=1)
    path = Path(entry["path"])
    lease = TS.acquire_lease(root, entry["id"])
    try:
        assert TS.lease_held(root, entry["id"]) is True
        assert TS.get(root, entry["id"])["leased"] is True

        plan = TS.clean(root, expired=True)
        assert plan["candidates"] == []
        assert _kept_reason(plan, entry["id"]) == "leased"

        done = TS.clean(root, expired=True, apply=True)
        assert done["deleted"] == []
        assert path.is_dir()

        gplan = TS.gc(root, budget=TS.TempBudget(max_total_bytes=0))
        assert gplan["candidates"] == []
        assert any(row["id"] == entry["id"] and row["why"] == "leased"
                   for row in gplan["kept"])

        gdone = TS.gc(root, budget=TS.TempBudget(max_total_bytes=0), apply=True)
        assert gdone["deleted"] == []
        assert path.is_dir()

        with pytest.raises(TS.TempPathError):
            TS.release(root, entry["id"], delete=True)
        assert path.is_dir()
    finally:
        lease.close()

    assert TS.lease_held(root, entry["id"]) is False
    done = TS.clean(root, expired=True, apply=True)
    assert done["deleted"] == [entry["id"]]
    assert not path.exists()


def test_a_closed_lease_does_not_block_release_delete(root: Path) -> None:
    entry = TS.create(root, reason="closed lease", ttl_s=60)
    _rewrite(root, entry["id"], expires_at=1)
    lease = TS.acquire_lease(root, entry["id"])
    lease.close()
    assert TS.lease_held(root, entry["id"]) is False

    released = TS.release(root, entry["id"], delete=True)
    assert released["state"] == "released"
    assert not Path(entry["path"]).exists()


def test_pin_survives_touch_and_expiry_until_unpinned(root: Path) -> None:
    entry = TS.create(root, reason="pinned", ttl_s=60)
    _rewrite(root, entry["id"], expires_at=1)
    TS.pin(root, entry["id"], until=1234567890)
    pinned = TS.get(root, entry["id"])
    assert pinned["pinned"] is True
    assert pinned["pinned_until"] == 1234567890

    TS.touch(root, entry["id"])
    record = _record(root, entry["id"])
    assert record["pinned"] is True
    assert record["pinned_until"] == 1234567890

    plan = TS.clean(root, expired=True)
    assert plan["candidates"] == []
    assert _kept_reason(plan, entry["id"]) == "pinned"

    collected = TS.gc(root, budget=TS.TempBudget(max_total_bytes=0))
    assert collected["candidates"] == []
    assert any(row["id"] == entry["id"] and row["why"] == "pinned"
               for row in collected["kept"])
    assert TS.status(root)["totals"]["pinned"] == 1
    assert Path(entry["path"]).is_dir()

    TS.unpin(root, entry["id"])
    assert TS.get(root, entry["id"])["pinned"] is False
    done = TS.clean(root, expired=True, apply=True)
    assert done["deleted"] == [entry["id"]]
    assert not Path(entry["path"]).exists()


def test_create_refuses_global_and_session_budgets(root: Path) -> None:
    first = TS.create(root, reason="first")
    _write(Path(first["path"]) / "blob.bin", 4096)
    first_view = TS.get(root, first["id"])
    before = set(TS.registry_dir(root).glob("*.json"))

    with pytest.raises(TS.TempBudgetError):
        TS.create(root, reason="over global bytes", budget=TS.TempBudget(
            max_total_bytes=first_view["bytes"]))
    with pytest.raises(TS.TempBudgetError):
        TS.create(root, reason="over inodes", budget=TS.TempBudget(
            max_inodes=first_view["inodes"]))
    assert set(TS.registry_dir(root).glob("*.json")) == before

    session = TS.make_session("opencode", "ses_budget")
    assert session is not None
    same = TS.create(root, reason="session a", session=session)
    _write(Path(same["path"]) / "one.bin", 4096)
    _write(Path(same["path"]) / "two.bin", 4096)
    session_bytes = TS.get(root, same["id"])["bytes"]
    with pytest.raises(TS.TempBudgetError):
        TS.create(root, reason="same session", session=session,
                  budget=TS.TempBudget(max_session_bytes=session_bytes))
    with pytest.raises(TS.TempBudgetError):
        TS.create(root, reason="same session inodes", session=session,
                  budget=TS.TempBudget(
                      max_session_inodes=TS.get(root, same["id"])["inodes"]))

    other = TS.make_session("opencode", "ses_other")
    assert other is not None
    scoped = TS.create(root, reason="session b", session=other,
                       budget=TS.TempBudget(max_session_bytes=session_bytes))
    assert scoped["session"] == other

    with pytest.raises(TS.TempBudgetError):
        TS.create(root, reason="no free space",
                  budget=TS.TempBudget(min_free_bytes=1 << 62))

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv(TS.ENV_MAX_TOTAL_BYTES, "0")
        with pytest.raises(TS.TempBudgetError):
            TS.create(root, reason="environment budget")
    finally:
        monkeypatch.undo()

    # There is no model/plugin force override on the core create surface.
    assert "force" not in TS.create.__code__.co_varnames


def test_gc_orders_released_then_cache_then_lru_then_largest_root(
        root: Path) -> None:
    released = TS.create(root, reason="released")
    _write(Path(released["path"]) / "small.bin", 32)
    TS.release(root, released["id"])
    cache = TS.create(root, reason="cache")
    _rewrite(root, cache["id"], kind="cache", last_used_at=50)
    oldest = TS.create(root, reason="oldest active")
    _rewrite(root, oldest["id"], last_used_at=100)
    large = TS.create(root, reason="large later")
    _write(Path(large["path"]) / "large.bin", 20000)
    _rewrite(root, large["id"], last_used_at=200)
    small = TS.create(root, reason="small later")
    _write(Path(small["path"]) / "small.bin", 1)
    _rewrite(root, small["id"], last_used_at=200)

    plan = TS.gc(root, budget=TS.TempBudget(max_total_bytes=0))
    assert [row["id"] for row in plan["candidates"]] == [
        released["id"], cache["id"], oldest["id"], large["id"], small["id"]]

    entries = {entry["id"]: entry for entry in
               TS.list_entries(root, include_all=True)}
    total = sum(entry["bytes"] for entry in entries.values())
    stopping = total - entries[released["id"]]["bytes"] - entries[cache["id"]]["bytes"]
    capped = TS.gc(root, budget=TS.TempBudget(max_total_bytes=stopping))
    assert [row["id"] for row in capped["candidates"]] == [
        released["id"], cache["id"]]


def test_gc_min_free_continues_in_the_same_order(root: Path,
                                                 monkeypatch) -> None:
    released = TS.create(root, reason="released")
    TS.release(root, released["id"])
    active = TS.create(root, reason="active")
    _rewrite(root, active["id"], last_used_at=10)
    monkeypatch.setattr(TS, "_free_bytes", lambda path: 0)

    plan = TS.gc(root, budget=TS.TempBudget(min_free_bytes=1 << 40))
    assert [row["id"] for row in plan["candidates"]] == [
        released["id"], active["id"]]
    assert plan["min_free"]["violated"] is True


def test_gc_refuses_unowned_mismatched_symlink_pinned_leased_and_missing(
        root: Path, tmp_path: Path) -> None:
    external = tmp_path / "gc-unowned"
    external.mkdir()
    _write(external / "out.bin", 8)
    unowned = TS.adopt(root, external, reason="unowned")

    mismatch = TS.create(root, reason="marker mismatch")
    marker = Path(mismatch["path"]) / TS.MARKER_NAME
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["id"] = "tmp_ffffffffffff"
    marker.write_text(json.dumps(payload), encoding="utf-8")

    linked = TS.create(root, reason="symlink")
    target = tmp_path / "gc-target"
    target.mkdir()
    shutil.rmtree(linked["path"])
    os.symlink(target, linked["path"])

    pinned = TS.create(root, reason="pinned")
    TS.pin(root, pinned["id"])

    leased = TS.create(root, reason="leased")
    lease = TS.acquire_lease(root, leased["id"])
    missing = TS.create(root, reason="missing")
    shutil.rmtree(missing["path"])
    try:
        plan = TS.gc(root, budget=TS.TempBudget(max_total_bytes=0))
        assert plan["candidates"] == []
        reasons = {row["id"]: row["why"] for row in plan["kept"]}
        assert reasons[unowned["id"]] == "unowned"
        assert reasons[mismatch["id"]] == "marker-mismatch"
        assert reasons[linked["id"]] == "symlink"
        assert reasons[pinned["id"]] == "pinned"
        assert reasons[leased["id"]] == "leased"
        assert reasons[missing["id"]] == "missing"

        applied = TS.gc(root, budget=TS.TempBudget(max_total_bytes=0),
                        apply=True)
        assert applied["deleted"] == []
        assert Path(mismatch["path"]).is_dir()
        assert Path(linked["path"]).is_symlink()
        assert Path(pinned["path"]).is_dir()
        assert Path(leased["path"]).is_dir()
    finally:
        lease.close()
        shutil.rmtree(external, ignore_errors=True)


# --------------------------------------------------------------------------- #
# lifecycle, cache semantics and hardening (Stage C)
# --------------------------------------------------------------------------- #


def test_cache_kind_writes_the_standard_tag_only_once(root: Path) -> None:
    cache = TS.create(root, kind="cache", reason="cache")
    tag = Path(cache["path"]) / TS.CACHEDIR_TAG_NAME
    assert tag.is_file()
    assert stat.S_IMODE(tag.stat().st_mode) == 0o644
    text = tag.read_text(encoding="utf-8")
    assert text.startswith(TS.CACHEDIR_TAG_SIGNATURE + "\n")
    assert "cache directory tag" in text
    assert TS.CACHEDIR_TAG_SIGNATURE in TS.CACHEDIR_TAG_CONTENT

    scratch = TS.create(root, kind="scratch", reason="scratch")
    assert not (Path(scratch["path"]) / TS.CACHEDIR_TAG_NAME).exists()


def test_cache_budget_refusal_removes_the_tagged_directory(root: Path) -> None:
    with pytest.raises(TS.TempBudgetError):
        TS.create(root, kind="cache", reason="too big",
                  budget=TS.TempBudget(max_total_bytes=0))
    assert TS.list_entries(root) == []
    cache_root = TS.default_kind_root(root, "cache")
    assert not cache_root.exists() or not list(cache_root.glob("tmp_*"))


def test_gc_evicts_cache_before_scratch(root: Path) -> None:
    cache = TS.create(root, kind="cache", reason="cache")
    scratch = TS.create(root, kind="scratch", reason="scratch")
    plan = TS.gc(root, budget=TS.TempBudget(max_total_bytes=0))
    assert [row["id"] for row in plan["candidates"]] == [
        cache["id"], scratch["id"]]


def test_touch_moves_lru_without_clearing_pin_or_lease(root: Path) -> None:
    entry = TS.create(root, reason="touch")
    _rewrite(root, entry["id"], last_used_at=1)
    TS.pin(root, entry["id"], until=1234567890)
    lease = TS.acquire_lease(root, entry["id"])
    try:
        touched = TS.touch(root, entry["id"])
        record = _record(root, entry["id"])
        assert record["last_used_at"] > 1
        assert record["pinned"] is True
        assert record["pinned_until"] == 1234567890
        assert touched["pinned"] is True
        assert TS.lease_held(root, entry["id"]) is True
        assert touched["leased"] is True
    finally:
        lease.close()


def test_audit_and_clean_legacy_skip_a_tagged_cache(
        root: Path, tmp_path: Path, monkeypatch) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    tagged_cache = scan / "ozagia_index_bench" / "run"
    _write(tagged_cache / "report.json", 12)
    (tagged_cache / TS.CACHEDIR_TAG_NAME).write_text(
        TS.CACHEDIR_TAG_CONTENT, encoding="utf-8")
    _old(tagged_cache)
    tagged_plain = scan / "ozagia-native-cache"
    _write(tagged_plain / "out", 7)
    (tagged_plain / TS.CACHEDIR_TAG_NAME).write_text(
        TS.CACHEDIR_TAG_CONTENT, encoding="utf-8")
    _old(tagged_plain)
    monkeypatch.setattr(TS, "allowed_temp_roots", lambda value: (scan,))

    report = TS.audit(root)
    paths = {item["path"] for item in report["artifacts"]}
    assert str(tagged_cache) not in paths
    assert str(tagged_plain) not in paths

    plan = TS.clean_legacy(root, older_than_s=0, include_caches=True)
    assert all(row["path"] not in {str(tagged_cache), str(tagged_plain)}
               for row in plan["artifacts"])
    assert plan["candidates"] == []
    done = TS.clean_legacy(root, older_than_s=0, include_caches=True,
                           apply=True)
    assert done["deleted"] == []
    assert tagged_cache.is_dir() and tagged_plain.is_dir()


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_clean_refuses_a_path_replaced_after_planning(
        root: Path, tmp_path: Path, monkeypatch, replacement: str) -> None:
    entry = TS.create(root, reason="swap", ttl_s=60)
    _rewrite(root, entry["id"], expires_at=1)
    path = Path(entry["path"])
    target = tmp_path / "outside"
    target.mkdir()
    _write(target / "payload", 5)
    real_identity = TS._path_identity
    swapped = False

    def capture_then_swap(value):
        nonlocal swapped
        identity = real_identity(value)
        if Path(value) == path and not swapped:
            swapped = True
            if replacement == "symlink":
                shutil.rmtree(path)
                os.symlink(target, path)
            else:
                path.rename(tmp_path / "original")
                path.mkdir()
                _write(path / "new", 1)
        return identity

    monkeypatch.setattr(TS, "_path_identity", capture_then_swap)
    done = TS.clean(root, expired=True, apply=True)
    assert done["deleted"] == []
    assert any(row["id"] == entry["id"] and row["why"] == "changed"
               for row in done["kept"])
    if replacement == "symlink":
        assert path.is_symlink()
        assert (target / "payload").is_file()
    else:
        assert path.is_dir()
        assert (path / "new").is_file()
        assert not (path / TS.MARKER_NAME).exists()


def test_clean_legacy_refuses_a_path_replaced_after_planning(
        root: Path, tmp_path: Path, monkeypatch) -> None:
    scan = tmp_path / "scan"
    scan.mkdir()
    artifact = scan / "ozagia-native-old"
    _write(artifact / "log", 5)
    _old(artifact)
    target = tmp_path / "outside"
    target.mkdir()
    _write(target / "payload", 5)
    real_row = TS._legacy_row
    swapped = False

    def row_then_swap(item, *, action, why):
        nonlocal swapped
        row = real_row(item, action=action, why=why)
        if action == "delete" and row["path"] == str(artifact) and not swapped:
            swapped = True
            shutil.rmtree(artifact)
            os.symlink(target, artifact)
        return row

    monkeypatch.setattr(TS, "_legacy_row", row_then_swap)
    done = TS.clean_legacy(root, roots=[scan], older_than_s=0, apply=True)
    assert done["deleted"] == []
    assert any(row["path"] == str(artifact) and row["why"] == "changed"
               for row in done["kept"])
    assert artifact.is_symlink()
    assert (target / "payload").is_file()
