"""Library-first hints: registry, detection, capture, CLI and intent hook."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ozagia import intent as I
from ozagia import library_hints_cli as C
from ozagia.core import library_hints as L
from ozagia.core import settings as S
from ozagia.daemon.server import DaemonState


RETRY = '''import time


def _retry(fn):
    delay = 1
    for attempt in range(3):
        try:
            return fn()
        except Exception:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("failed")
'''

RETRY_IMPORTED = '''import time
import tenacity


def _retry(fn):
    delay = 1
    for attempt in range(3):
        try:
            return fn()
        except Exception:
            time.sleep(delay)
            delay *= 2
'''

CLI_PARSER = '''import sys


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: app [--verbose]")
        return 2
    if args[0].startswith("--"):
        return 1
    return 0
'''

CACHE = '''def get(self, key):
    if key in self._cache:
        return self._cache[key]
    value = compute(key)
    self._cache[key] = value
    return value
'''

YAML_PARSER = '''def load_yaml(text):
    data = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("- "):
            data.setdefault("items", []).append(line[2:])
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip()
    return data
'''

JS_RETRY = '''export async function fetchWithRetry(url) {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      return await fetch(url);
    } catch (error) {
      await new Promise((resolve) => setTimeout(resolve, 100 * 2 ** attempt));
    }
  }
  throw new Error("failed");
}
'''

RUST_RETRY = '''fn retry<F>(mut f: F) -> u32 {
    let mut attempts = 0;
    loop {
        attempts += 1;
        match f() {
            Ok(value) => return value,
            Err(_) => std::thread::sleep(std::time::Duration::from_millis(100)),
        }
    }
}
'''


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("OZAGIA_ROOT", "OZAGIA_STATE_DIR", "OZAGIA_USER_CONFIG",
                 "OZAGIA_CONFIG", "OZAGIA_LIBRARY_HINTS", "OZAGIA_FILE_LEDGER"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _detect(root: Path, rel: str, before: str | None, after: str | None, **kw):
    return L.detect_files(root, [L.FileChange(rel, before, after)], **kw)


def _hint(got: dict) -> dict:
    assert len(got["hints"]) == 1, got
    return got["hints"][0]


def _git(root: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(root), "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        text=True, env=env).stdout


class FakeMemory:
    """The small part of DevMemory an intent edit/write touches."""

    def __init__(self, root: Path):
        self.raw_repo_root = str(root)
        self.edit_session = None
        self.journal = root / "journal.jsonl"

    def _drop_cached(self, rel: str) -> None:
        return None

    def reingest(self, paths):
        return {"added": [], "removed": [], "changed": [],
                "edges_added": [], "edges_removed": []}

    def journal_append(self, *args, **kwargs) -> None:
        return None

    def node_by_key(self, key: str):
        return None

    def span_edit(self, rel: str, old: str, new: str, *,
                  replace_all: bool = False, apply: bool = True) -> dict:
        path = Path(self.raw_repo_root) / rel
        text = path.read_text(encoding="utf-8")
        if old not in text:
            return {"ok": False, "error": "oldString not found"}
        count = text.count(old)
        if count > 1 and not replace_all:
            line = text[:text.index(old)].count("\n") + 1
            return {"ok": False, "matches": [
                {"start_line": line, "key": f"sym:{rel}::match"}]}
        if apply:
            replaced = text.replace(old, new, 1 if not replace_all else -1)
            path.write_text(replaced, encoding="utf-8")
        return {
            "ok": True,
            "matches": [{"start": text.index(old), "start_line": 1,
                         "end": text.index(old) + len(old),
                         "key": f"sym:{rel}::edit"}],
            "keys": [f"file:{rel}"],
            "graph_delta": {},
        }


# --------------------------------------------------------------------------- #
# detection and evidence
# --------------------------------------------------------------------------- #
def test_new_retry_loop_hints_tenacity(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_installed_version", lambda dist: None)
    root = tmp_path / "repo"
    root.mkdir()
    got = _detect(root, "src/app.py", None, RETRY, mode="gentle",
                  max_per_edit=2, record=False)
    hint = _hint(got)
    assert hint["package"] == "tenacity"
    assert hint["capability"] == "retry"
    assert hint["availability"] == "known"
    assert hint["confidence"] == "medium"
    assert "sleep" in hint["evidence"] and "backoff" in hint["evidence"]
    assert hint["message"].startswith("[ozagia] library hint:")
    assert "tenacity" in hint["message"]


def test_declared_dependency_is_stronger(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_installed_version", lambda dist: None)
    root = tmp_path / "repo"
    root.mkdir()
    _write(root / "pyproject.toml",
           '[project]\nname = "x"\nversion = "0.1"\n'
           'dependencies = ["tenacity>=8"]\n')
    got = _detect(root, "src/app.py", None, RETRY, record=False)
    hint = _hint(got)
    assert hint["availability"] == "declared"
    assert "already a dependency" in hint["message"]


def test_already_imported_produces_no_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_installed_version", lambda dist: None)
    root = tmp_path / "repo"
    root.mkdir()
    got = _detect(root, "src/app.py", None, RETRY_IMPORTED, record=False)
    assert got["hints"] == []
    assert any(item["reason"] == "already_imported"
               for item in got["suppressed"])


def test_stdlib_alternative_is_preferred(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    got = _detect(root, "src/cli.py", None, CLI_PARSER, record=False)
    hint = _hint(got)
    assert hint["package"] == "argparse"
    assert hint["availability"] == "stdlib"
    assert "standard library" in hint["message"]


def test_stdlib_cache_alternative_is_preferred(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    got = _detect(root, "src/cache.py", None, CACHE, record=False)
    hint = _hint(got)
    assert hint["package"] == "functools.lru_cache"
    assert hint["availability"] == "stdlib"


def test_name_only_match_is_not_a_hint(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    after = "def _retry(value):\n    return value + 1\n"
    got = _detect(root, "src/app.py", None, after, record=False)
    assert got["hints"] == [] and got["suppressed"] == []


def test_existing_symbol_needs_strict(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    gentle = _detect(root, "src/app.py", RETRY, RETRY, mode="gentle",
                     record=False)
    assert gentle["hints"] == []
    strict = _detect(root, "src/app.py", RETRY, RETRY, mode="strict",
                     record=False)
    assert _hint(strict)["package"] == "tenacity"


def test_off_produces_nothing_and_no_events(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    got = _detect(root, "src/app.py", None, RETRY, mode="off", record=True)
    assert got["hints"] == [] and got["suppressed"] == []
    assert got["recorded"] is False and got["events_path"] is None
    assert not L.events_path(root).exists()


def test_max_cap_deterministic_and_suppressed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    after = "\n\n".join((RETRY, CLI_PARSER, CACHE, YAML_PARSER))
    first = _detect(root, "src/all.py", None, after, mode="gentle",
                    max_per_edit=1, record=False)
    second = _detect(root, "src/all.py", None, after, mode="gentle",
                     max_per_edit=1, record=False)
    assert first["hints"] == second["hints"]
    assert first["suppressed"] == second["suppressed"]
    assert len(first["hints"]) == 1
    assert first["hints"][0]["capability"] == "retry"
    assert any(item["reason"] == "max_per_edit"
               for item in first["suppressed"])
    capabilities = [item["capability"] for item in first["hints"]]
    assert len(capabilities) == len(set(capabilities))


def test_events_have_metadata_only(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    text = RETRY.replace("import time", "import time\n# PROMPT: never store this")
    got = _detect(root, "src/app.py", None, text, record=True, session="s-1")
    assert got["recorded"] is True
    path = L.events_path(root)
    raw = path.read_text(encoding="utf-8")
    assert "PROMPT" not in raw
    assert "time.sleep" not in raw
    assert "def _retry" not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    assert events
    expected = {"schema", "ts", "session", "file", "symbol", "capability",
                "package", "availability", "confidence", "status", "reason",
                "rule_version", "hint_id"}
    for event in events:
        assert set(event) == expected
        assert "message" not in event and "evidence" not in event
        assert event["rule_version"] == L.RULE_VERSION
        assert event["session"] == "s-1"


def test_javascript_and_rust_bounded_heuristics(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    js = _detect(root, "web/retry.js", None, JS_RETRY, record=False)
    assert _hint(js)["package"] == "p-retry"
    rust = _detect(root, "src/retry.rs", None, RUST_RETRY, record=False)
    assert _hint(rust)["package"] == "backoff"


def test_hints_for_paths_helper(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root / "src/app.py", RETRY)
    settings = L.HintSettings(mode="gentle", max_per_edit=2, record=False)
    got = L.hints_for_paths(root, ["src/app.py"], settings=settings)
    assert _hint(got)["package"] == "tenacity"


def test_hint_settings_defaults_and_write(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert S.get(root, "hints.library")["value"] == "gentle"
    assert S.get(root, "hints.max_per_edit")["value"] == 2
    assert S.get(root, "hints.record")["value"] is True
    assert S.get(root, "hints.ecosystems")["value"] == [
        "python", "javascript", "rust"]
    result = S.set_value(root, "hints.library", "strict", dry_run=False)
    assert result["changed"] is True
    assert S.get(root, "hints.library")["value"] == "strict"
    assert L.settings_for(root).mode == "strict"
    path = root / "ozagia" / "config.toml"
    assert "[hints]" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _repo_with_retry(tmp_path: Path) -> tuple[Path, str, str]:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "repo"
    root.mkdir()
    _write(root / "README.md", "# repo\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _write(root / "src/app.py", RETRY)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add retry")
    base = _git(root, "rev-parse", "HEAD~1").strip()
    head = _git(root, "rev-parse", "HEAD").strip()
    return root, base, head


def test_cli_detect_json_text_explain_and_feedback(tmp_path, capsys):
    root, base, head = _repo_with_retry(tmp_path)
    rc = C.main(["detect", "--root", str(root), "--diff", f"{base}..{head}",
                 "--json", "--no-record"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["hints"] == 1
    hint = payload["hints"][0]
    assert hint["package"] == "tenacity"
    assert hint["file"] == "src/app.py"

    rc = C.main(["detect", "--root", str(root), "--diff", f"{base}..{head}",
                 "--record"])
    assert rc == 0
    text = capsys.readouterr().out
    assert "[ozagia] library hint:" in text

    rc = C.main(["explain", "_retry", "--root", str(root)])
    assert rc == 0
    assert "library hints for `_retry`" in capsys.readouterr().out

    rc = C.main(["feedback", hint["hint_id"], "accepted", "--root", str(root),
                 "--json"])
    assert rc == 0
    feedback = json.loads(capsys.readouterr().out)
    assert feedback["feedback"]["verdict"] == "accepted"
    raw = L.feedback_path(root).read_text(encoding="utf-8")
    assert hint["hint_id"] in raw and "accepted" in raw


# --------------------------------------------------------------------------- #
# intent and daemon integration
# --------------------------------------------------------------------------- #
def test_intent_write_appends_hint_and_records(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_installed_version", lambda dist: None)
    monkeypatch.setattr("ozagia.core.files.record_file",
                        lambda *args, **kwargs: None)
    root = tmp_path / "repo"
    root.mkdir()
    _write(root / "ozagia/config.toml",
           '[hints]\nlibrary = "gentle"\nrecord = true\n')
    res = I.handle(FakeMemory(root), "write",
                   {"path": "src/app.py", "content": RETRY}, session="s-1")
    assert res["handled"] is True and res["is_error"] is False
    assert "[ozagia] library hint:" in res["output"]
    assert res["meta"]["library_hints"] == 1
    assert res["meta"]["library_hint_files"] == 1
    assert L.events_path(root).is_file()


def test_intent_edit_appends_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_installed_version", lambda dist: None)
    root = tmp_path / "repo"
    root.mkdir()
    _write(root / "src/app.py", "def helper(a):\n    return a\n")
    _write(root / "ozagia/config.toml",
           '[hints]\nlibrary = "gentle"\nrecord = false\n')
    res = I.handle(FakeMemory(root), "edit", {
        "path": "src/app.py",
        "oldString": "def helper(a):\n    return a\n",
        "newString": RETRY,
    }, session="s-1")
    assert res["handled"] is True and res["is_error"] is False
    assert "[ozagia] library hint:" in res["output"]
    assert res["meta"]["library_hints"] == 1


def test_intent_env_opt_out_produces_no_events(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root / "ozagia/config.toml",
           '[hints]\nlibrary = "gentle"\nrecord = true\n')
    monkeypatch.setenv("OZAGIA_LIBRARY_HINTS", "0")
    monkeypatch.setattr("ozagia.core.files.record_file",
                        lambda *args, **kwargs: None)
    res = I.handle(FakeMemory(root), "write",
                   {"path": "src/app.py", "content": RETRY}, session="s-1")
    assert res["handled"] is True
    assert "library hint" not in res["output"]
    assert "library_hints" not in res["meta"]
    assert not L.events_path(root).exists()


def test_daemon_stats_sum_hint_meta(tmp_path):
    state = DaemonState(tmp_path, object(), tmp_path / "journal.jsonl")
    state.record(
        {"op": "tool", "tool": "write", "session": "s"},
        {"ok": True, "handled": True,
         "meta": {"intent": "write", "library_hints": 2,
                  "library_hint_files": 1, "library_hints_suppressed": 3}},
        1.0)
    stats = state.stats()
    assert stats["library_hints"] == 2
    assert stats["library_hint_files"] == 1
    assert stats["library_hints_suppressed"] == 3
