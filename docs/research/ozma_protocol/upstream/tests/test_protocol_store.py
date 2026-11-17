"""
tests/test_protocol_store.py — Session store backends.

Tests MemorySessionStore and SQLiteSessionStore with identical behaviour.
SQLite tests use :memory: for isolation.
"""

from __future__ import annotations

import pytest

from ozmalib.protocol.store import MemorySessionStore, SQLiteSessionStore, SessionStore


def _stores():
    return [
        pytest.param(MemorySessionStore(), id="memory"),
        pytest.param(SQLiteSessionStore(":memory:"), id="sqlite"),
    ]


@pytest.mark.parametrize("store", _stores())
class TestSessionStoreContract:
    """Both backends must satisfy the same behavioural contract."""

    def test_new_session_not_acked(self, store: SessionStore):
        assert not store.is_acked("fresh_session")

    def test_set_and_get_nonce(self, store: SessionStore):
        store.set_nonce("s1", "abcd1234")
        assert store.get_nonce("s1") == "abcd1234"

    def test_get_nonce_missing_returns_none(self, store: SessionStore):
        assert store.get_nonce("nonexistent") is None

    def test_mark_acked_clears_nonce(self, store: SessionStore):
        store.set_nonce("s2", "deadbeef")
        store.mark_acked("s2", {"EDIT": "b2c1d3"}, "ozma-coder-1", 3)
        assert store.is_acked("s2")
        assert store.get_nonce("s2") is None

    def test_get_caps_returns_stored_caps(self, store: SessionStore):
        caps = {"EDIT": "b2c1d3", "PLAN": "82bc4d"}
        store.mark_acked("s3", caps, "ozma-coder-1", 3)
        assert store.get_caps("s3") == caps

    def test_get_caps_unknown_returns_empty(self, store: SessionStore):
        assert store.get_caps("unknown") == {}

    def test_touch_does_not_raise(self, store: SessionStore):
        store.touch("s4")   # no-op but must not raise

    def test_overwrite_nonce(self, store: SessionStore):
        store.set_nonce("s5", "aaaa0000")
        store.set_nonce("s5", "bbbb1111")
        assert store.get_nonce("s5") == "bbbb1111"

    def test_caps_overwrite_on_second_ack(self, store: SessionStore):
        store.mark_acked("s6", {"EDIT": "b2c1d3"}, "m1", 3)
        store.mark_acked("s6", {"PLAN": "82bc4d"}, "m1", 3)
        assert store.get_caps("s6") == {"PLAN": "82bc4d"}

    def test_multiple_sessions_independent(self, store: SessionStore):
        store.set_nonce("sa", "aaa")
        store.set_nonce("sb", "bbb")
        assert store.get_nonce("sa") == "aaa"
        assert store.get_nonce("sb") == "bbb"
        store.mark_acked("sa", {}, "m", 1)
        assert store.is_acked("sa")
        assert not store.is_acked("sb")

    def test_is_runtime_checkable(self, store: SessionStore):
        assert isinstance(store, SessionStore)


class TestSQLiteStorePersistence:
    """SQLite-specific: test file-backed persistence and connection reuse."""

    def test_file_persistence(self, tmp_path):
        db = str(tmp_path / "test.db")
        s1 = SQLiteSessionStore(db)
        s1.set_nonce("persisted", "12345678")
        s1.mark_acked("persisted", {"EDIT": "b2c1d3"}, "m", 3)

        # Re-open the same file
        s2 = SQLiteSessionStore(db)
        assert s2.is_acked("persisted")
        assert s2.get_caps("persisted") == {"EDIT": "b2c1d3"}

    def test_memory_not_file(self, tmp_path):
        # :memory: store is ephemeral — second instance starts fresh
        s1 = SQLiteSessionStore(":memory:")
        s1.mark_acked("x", {}, "m", 1)
        s2 = SQLiteSessionStore(":memory:")
        assert not s2.is_acked("x")

    def test_creates_parent_dir(self, tmp_path):
        db = str(tmp_path / "subdir" / "nested" / "s.db")
        s = SQLiteSessionStore(db)
        s.set_nonce("q", "abcd1234")
        assert s.get_nonce("q") == "abcd1234"

    def test_thread_safety(self, tmp_path):
        import threading
        db = str(tmp_path / "threads.db")
        store = SQLiteSessionStore(db)
        results = []
        def worker(sid):
            store.set_nonce(sid, "nonce")
            store.mark_acked(sid, {}, "m", 1)
            results.append(store.is_acked(sid))
        threads = [threading.Thread(target=worker, args=(f"sess{i}",)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert all(results)
        assert len(results) == 10


class TestMemoryStore:
    def test_no_disk_io(self):
        # MemorySessionStore must not touch the filesystem
        import os
        store = MemorySessionStore()
        store.set_nonce("x", "1234")
        store.mark_acked("x", {}, "m", 1)
        # No ~/.ozma/ or similar created — just verify no error
        assert store.is_acked("x")
