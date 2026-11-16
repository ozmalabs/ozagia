"""Multi-file replace (ozagia.edit.replace, DevMemory.replace, the `replace`
intent tool, `dev_replace` and `ozagia replace`)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from ozagia import cli
from ozagia.core import memory as M
from ozagia.core.memory import DevMemory, build_index
from ozagia.edit import replace as R
from ozagia.edit.code_edit import CodeEditError
from ozagia.intent import handle, permission_request
from ozagia.integrations.mcp.server import DevMemoryServer

MOD = '''"""Module."""


def helper(a, b=1):
    """Help."""
    return a + b


def other(x):
    value = helper(x)
    return value
'''

USE = '''"""Uses mod."""
from pkg.mod import helper


def run():
    return helper(1) + helper(2)
'''

README = """# Project

Intro mentions helper.

## Usage

Call helper here.

## Other

helper again.
"""


def _write(p: Path, text: str | bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "HOME": str(root), "PATH": "/usr/bin:/bin"})


@pytest.fixture
def layout(monkeypatch):
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "IMPORT_ROOTS", ())
    monkeypatch.setattr(M, "_PATH_RE", M.path_mention_re(["pkg", "tests"]))
    monkeypatch.delenv("OZAGIA_CONFIG", raising=False)
    monkeypatch.delenv("OZAGIA_ADAPTERS", raising=False)
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)


@pytest.fixture
def repo(tmp_path: Path, layout) -> Path:
    root = tmp_path / "repo"
    _write(root / "pkg" / "__init__.py", '"""The package."""\n')
    _write(root / "pkg" / "mod.py", MOD)
    _write(root / "pkg" / "use.py", USE)
    _write(root / "README.md", README)
    _write(root / "notes.txt", "helper in text\n")
    _write(root / "data.json", '{"name": "helper"}\n')
    _write(root / ".gitignore", "build/\n")
    _write(root / "build" / "out.py", "helper = 1\n")
    _write(root / "blob.bin", b"helper\0\x01\x02")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def mem(repo: Path, tmp_path: Path) -> DevMemory:
    dump = tmp_path / "state" / "graph.json"
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=repo)


def _backups(root: Path) -> list[Path]:
    d = root / ".ozagia" / "edits" / "backups"
    return sorted(d.iterdir()) if d.is_dir() else []


def _by_file(res: dict) -> dict[str, dict]:
    return {f["file"]: f for f in res["files"]}


def _journal(mem: DevMemory) -> list[dict]:
    if not mem.journal.exists():
        return []
    return [json.loads(x) for x in mem.journal.read_text().splitlines()]


# --------------------------------------------------------------------------- #
# Engine: matching
# --------------------------------------------------------------------------- #
def test_literal_preview_counts_keys_diff_and_writes_nothing(repo: Path) -> None:
    before = (repo / "pkg" / "use.py").read_text()
    res = R.replace(repo, "helper(", "assist(", globs=["*.py"])
    assert res["ok"] and res["applied"] is False
    files = _by_file(res)
    assert set(files) == {"pkg/mod.py", "pkg/use.py"}
    assert files["pkg/use.py"]["matches"] == 2
    assert files["pkg/use.py"]["keys"] == ["sym:pkg/use.py::run"]
    assert files["pkg/mod.py"]["keys"] == ["sym:pkg/mod.py::helper",
                                          "sym:pkg/mod.py::other"]
    assert res["total"] == 4
    assert "+    return assist(1) + assist(2)" in files["pkg/use.py"]["diff"]
    assert res["shas"]["pkg/use.py"] == files["pkg/use.py"]["base_sha256"]
    assert len(res["plan_id"]) == 16
    assert (repo / "pkg" / "use.py").read_text() == before
    assert _backups(repo) == []


def test_regex_with_groups_and_flags(repo: Path) -> None:
    res = R.replace(repo, r"helper\((\d)\)", r"helper(\1, b=\1)", regex=True,
                    globs=["pkg/**"], write=True)
    assert res["ok"] and res["applied"]
    assert "helper(1, b=1) + helper(2, b=2)" in (repo / "pkg/use.py").read_text()
    res = R.replace(repo, r"^(?P<w>INTRO) mentions", r"\g<w> names", regex=True,
                    flags="im", globs=["README.md"], write=True)
    assert res["total"] == 1
    assert "Intro names helper." in (repo / "README.md").read_text()
    # A literal pattern takes flags too, and regex metacharacters stay literal.
    res = R.replace(repo, "CALL HELPER", "call assist", flags="i",
                    globs=["README.md"])
    assert res["total"] == 1
    assert R.replace(repo, "a.b", "x", globs=["*.py"])["total"] == 0


def test_bad_regex_and_bad_group_are_refused(repo: Path) -> None:
    with pytest.raises(CodeEditError, match="bad regex"):
        R.replace(repo, "(", "x", regex=True)
    with pytest.raises(CodeEditError, match="bad replacement"):
        R.replace(repo, "(a)", r"\2", regex=True)
    with pytest.raises(CodeEditError, match="unknown regex flag"):
        R.replace(repo, "a", "b", flags="q")
    with pytest.raises(CodeEditError, match="non-empty"):
        R.replace(repo, "", "b")


def test_crlf_file_keeps_its_newlines(repo: Path) -> None:
    _write(repo / "win.txt", b"one\r\ntwo\r\nthree\r\n")
    res = R.replace(repo, "one\ntwo", "uno\ndos", globs=["win.txt"], write=True)
    assert res["total"] == 1
    assert (repo / "win.txt").read_bytes() == b"uno\r\ndos\r\nthree\r\n"


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #
def test_glob_scoping(repo: Path) -> None:
    assert set(_by_file(R.replace(repo, "helper", "h", globs=["pkg/*.py"]))) \
        == {"pkg/mod.py", "pkg/use.py"}
    assert set(_by_file(R.replace(repo, "helper", "h", globs=["use.py"]))) \
        == {"pkg/use.py"}
    assert set(_by_file(R.replace(repo, "helper", "h",
                                  globs=["**/*.md", "*.txt"]))) \
        == {"README.md", "notes.txt"}


def test_key_scoping_restricts_matches_to_spans(repo: Path, mem: DevMemory) -> None:
    res = R.replace(repo, "helper", "assist", keys=["sym:pkg/mod.py::other"],
                    write=True, memory=mem)
    assert res["total"] == 1
    text = (repo / "pkg/mod.py").read_text()
    assert "def helper(a, b=1):" in text and "value = assist(x)" in text
    res = R.replace(repo, "helper", "assist", keys=["doc:README.md#usage"])
    assert res["total"] == 1 and res["files"][0]["lines"] == [7]
    res = R.replace(repo, "helper", "assist", keys=["text:notes.txt#L1-L1"])
    assert res["total"] == 1
    # Keys and globs together: the key's file must match a glob.
    res = R.replace(repo, "helper", "assist", keys=["doc:README.md#usage"],
                    globs=["*.py"])
    assert res["total"] == 0
    with pytest.raises(CodeEditError):
        R.replace(repo, "helper", "x", keys=["sym:pkg/mod.py::nope"])


def test_skips_binary_git_state_gitignored_and_large(repo: Path) -> None:
    (repo / ".ozagia").mkdir(exist_ok=True)
    _write(repo / ".ozagia" / "x.py", "helper\n")
    _write(repo / "big.txt", "helper\n" + "x" * (R.MAX_FILE_BYTES + 10))
    res = R.replace(repo, "helper", "h", max_files=50)
    files = set(_by_file(res))
    assert "blob.bin" not in files and "build/out.py" not in files
    assert not any(f.startswith((".git/", ".ozagia/")) for f in files)
    assert "big.txt" not in files
    # Default scope: text suffixes, no dotfiles (blob.bin, .gitignore).
    assert res["skipped"].get("not a text suffix") == 1
    assert res["skipped"].get("dotfile") == 1
    assert res["skipped"].get("too large") == 1
    res = R.replace(repo, "helper", "h", max_files=50, any_suffix=True)
    assert res["skipped"].get("binary") == 1
    assert "blob.bin" not in set(_by_file(res))
    # Without .gitignore the ignored file is a candidate; .git and the state
    # dir never are.
    res = R.replace(repo, "helper", "h", gitignore=False, max_files=50)
    files = set(_by_file(res))
    assert "build/out.py" in files
    assert not any(f.startswith((".git/", ".ozagia/")) for f in files)
    # Globs cannot reach .git, and keys naming it are refused.
    assert R.replace(repo, "core", "x", globs=[".git/**"],
                     gitignore=False)["total"] == 0
    with pytest.raises(CodeEditError, match=".git"):
        R.replace(repo, "x", "y", keys=["text:.git/config#L1-L1"])


def test_outside_root_and_symlinks_are_skipped(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("helper outside\n")
    os.symlink(outside, repo / "link.txt")
    res = R.replace(repo, "helper", "h", globs=["link.txt"], gitignore=False,
                    write=True)
    assert res["total"] == 0 and res["skipped"] == {"symlink": 1}
    assert outside.read_text() == "helper outside\n"
    with pytest.raises(CodeEditError):
        R.replace(repo, "helper", "h", keys=["text:../outside.txt#L1-L1"])


def test_not_a_git_tree_walks_without_gitignore(tmp_path: Path, layout) -> None:
    root = tmp_path / "plain"
    _write(root / "a.txt", "foo\n")
    _write(root / "sub" / "b.txt", "foo\n")
    res = R.replace(root, "foo", "bar", gitignore=True)
    assert set(_by_file(res)) == {"a.txt", "sub/b.txt"}
    assert any(".gitignore" in n for n in res["notes"])


# --------------------------------------------------------------------------- #
# Caps, expect, stale previews, parse guard
# --------------------------------------------------------------------------- #
def test_caps_refuse(repo: Path) -> None:
    res = R.replace(repo, "helper", "h", max_files=1, write=True)
    assert res["ok"] is False and res["over_cap"] == "files"
    res = R.replace(repo, "helper", "h", max_matches=2, write=True)
    assert res["ok"] is False and res["over_cap"] == "matches"
    assert "helper" in (repo / "pkg/use.py").read_text()
    assert _backups(repo) == []


def test_expect_mismatch_refuses(repo: Path) -> None:
    res = R.replace(repo, "helper(", "assist(", globs=["*.py"], expect=2,
                    write=True)
    assert res["ok"] is False and res["expect_mismatch"] is True
    assert res["total"] == 4 and "expected 2" in res["error"]
    assert "helper(1)" in (repo / "pkg/use.py").read_text()
    res = R.replace(repo, "helper(", "assist(", globs=["*.py"], expect=4,
                    write=True)
    assert res["ok"] and res["applied"]


def test_stale_sha_since_preview_refuses(repo: Path) -> None:
    prev = R.replace(repo, "helper(", "assist(", globs=["*.py"])
    (repo / "pkg/mod.py").write_text(MOD + "\n# touched\n")
    res = R.replace(repo, "helper(", "assist(", globs=["*.py"], write=True,
                    base_shas=prev["shas"])
    assert res["ok"] is False and res["stale"] == ["pkg/mod.py"]
    assert "helper(1)" in (repo / "pkg/use.py").read_text()
    res = R.replace(repo, "helper(", "assist(", globs=["*.py"], write=True,
                    plan_id=prev["plan_id"])
    assert res["ok"] is False and "stale preview" in res["error"]
    # A file that matches now but was not in the preview refuses too.
    _write(repo / "pkg/new.py", "x = helper(3)\n")
    fresh = R.replace(repo, "helper(", "assist(", globs=["*.py"])
    partial = {k: v for k, v in fresh["shas"].items() if k != "pkg/new.py"}
    res = R.replace(repo, "helper(", "assist(", globs=["*.py"], write=True,
                    base_shas=partial)
    assert res["ok"] is False and res["unnamed"] == ["pkg/new.py"]
    res = R.replace(repo, "helper(", "assist(", globs=["*.py"], write=True,
                    base_shas=fresh["shas"], plan_id=fresh["plan_id"])
    assert res["ok"] and res["written"] == ["pkg/mod.py", "pkg/new.py",
                                            "pkg/use.py"]


def test_parse_guard_refuses_the_whole_batch(repo: Path) -> None:
    res = R.replace(repo, "helper", "help er", globs=["pkg/*.py", "*.md"],
                    write=True)
    assert res["ok"] is False and res["applied"] is False
    assert set(res["parse_refused"]) == {"pkg/mod.py", "pkg/use.py"}
    assert "nothing written" in res["error"]
    assert "helper" in (repo / "README.md").read_text()
    assert _backups(repo) == []
    j = R.replace(repo, '"helper"', '"helper', globs=["*.json"], write=True)
    assert j["ok"] is False and j["parse_refused"] == ["data.json"]
    res = R.replace(repo, "helper", "help er", globs=["pkg/*.py", "*.md"],
                    write=True, allow_parse_errors=True)
    assert res["ok"] and set(res["written"]) == {"pkg/mod.py", "pkg/use.py",
                                                 "README.md"}


# --------------------------------------------------------------------------- #
# DevMemory: backups, journal, reingest, counters
# --------------------------------------------------------------------------- #
def test_apply_backups_journal_and_delta(repo: Path, mem: DevMemory) -> None:
    prev = mem.replace("helper", "assist", globs=["pkg/*.py"])
    assert mem.replace_counters["replace_previews"] == 1
    res = mem.replace("helper", "assist", globs=["pkg/*.py"], apply=True,
                      base_shas=prev["shas"])
    assert res["ok"] and res["applied"]
    assert res["written"] == ["pkg/mod.py", "pkg/use.py"]
    backups = _backups(repo)
    assert len(backups) == 2
    assert {b.read_text() for b in backups} == {MOD, USE}
    files = _by_file(res)
    assert files["pkg/mod.py"]["backup"] in {str(b) for b in backups}
    assert "sym:pkg/mod.py::assist" in files["pkg/mod.py"]["delta"]["added"]
    assert "sym:pkg/mod.py::helper" in files["pkg/mod.py"]["delta"]["removed"]
    assert mem.node_by_key("sym:pkg/mod.py::assist") is not None
    (entry,) = [e for e in _journal(mem) if e["kind"] == "replace"]
    assert entry["files"] == ["pkg/mod.py", "pkg/use.py"]
    assert entry["pattern"] == "helper" and entry["replacement"] == "assist"
    assert set(entry["shas"]) == {"pkg/mod.py", "pkg/use.py"}
    assert mem.replace_counters == {"replace_previews": 1, "replace_applies": 1,
                                    "replace_files": 2}


def test_mid_batch_write_failure_restores_the_batch(repo: Path,
                                                   monkeypatch) -> None:
    from ozagia.edit import txn
    real = txn._current_bytes

    def flaky(path: Path) -> bytes:
        if path.name == "use.py":
            return b"changed underneath"
        return real(path)
    monkeypatch.setattr(txn, "_current_bytes", flaky)
    res = R.replace(repo, "helper", "assist", globs=["pkg/*.py"], write=True)
    assert res["ok"] is False and not res.get("partial")
    assert res["applied"] is False and res["written"] == []
    assert res["restored"] == ["pkg/mod.py"]
    assert "pkg/use.py" in res["error"] and "nothing written" in res["error"]
    assert (repo / "pkg/mod.py").read_text() == MOD
    assert (repo / "pkg/use.py").read_text() == USE


def _break_after_first_write(monkeypatch, repo: Path) -> None:
    """The second write fails; meanwhile someone edits the first file, so
    its restore must not run."""
    real = R.write_source
    calls = {"n": 0}

    def write(root, src, new_text, *, error_cls):
        calls["n"] += 1
        if calls["n"] == 2:
            (repo / "pkg/mod.py").write_text("someone else\n")
            raise error_cls("stale base: file changed on disk")
        return real(root, src, new_text, error_cls=error_cls)
    monkeypatch.setattr(R, "write_source", write)


def test_failed_restore_reports_partial(repo: Path, monkeypatch) -> None:
    _break_after_first_write(monkeypatch, repo)
    res = R.replace(repo, "helper", "assist", globs=["pkg/*.py"], write=True)
    assert res["ok"] is False and res["partial"] is True
    assert res["written"] == ["pkg/mod.py"]
    assert res["restore_failed"] == ["pkg/mod.py"]
    assert res["written_matches"] == 2
    assert (repo / "pkg/mod.py").read_text() == "someone else\n"
    assert (repo / "pkg/use.py").read_text() == USE


# --------------------------------------------------------------------------- #
# Paths through symlinks, base_shas, scope
# --------------------------------------------------------------------------- #
def test_directory_symlink_into_git_or_state_is_never_written(repo: Path) -> None:
    _write(repo / "a" / "config", "needle\n")
    _write(repo / "s" / "journal.jsonl", "needle\n")
    _git(repo, "add", "a/config", "s/journal.jsonl")
    (repo / ".ozagia").mkdir(exist_ok=True)
    _write(repo / ".ozagia" / "journal.jsonl", "needle\n")
    with open(repo / ".git" / "config", "a") as fh:
        fh.write("# needle\n")
    for d, target in (("a", ".git"), ("s", ".ozagia")):
        for f in (repo / d).iterdir():
            f.unlink()
        (repo / d).rmdir()
        os.symlink(target, repo / d)
    git_config = (repo / ".git" / "config").read_text()
    res = R.replace(repo, "needle", "PWNED", any_suffix=True, write=True)
    assert res["skipped"].get("symlinked path") == 2
    assert "PWNED" not in (repo / ".git" / "config").read_text()
    assert (repo / ".git" / "config").read_text() == git_config
    assert (repo / ".ozagia" / "journal.jsonl").read_text() == "needle\n"


def test_aliased_paths_are_one_file(repo: Path) -> None:
    _write(repo / "src" / "x.txt", "needle\n")
    _write(repo / "docs" / "x.txt", "needle\n")
    _git(repo, "add", "src/x.txt", "docs/x.txt")
    (repo / "docs" / "x.txt").unlink()
    (repo / "docs").rmdir()
    os.symlink("src", repo / "docs")
    res = R.replace(repo, "needle", "pin", globs=["*.txt"], write=True)
    assert res["ok"] and not res.get("partial"), res
    assert res["written"] == ["src/x.txt"]
    assert res["skipped"].get("symlinked path") == 1
    assert (repo / "src" / "x.txt").read_text() == "pin\n"


def test_base_shas_never_reads_unknown_paths(repo: Path, tmp_path: Path) -> None:
    import hashlib
    import time
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret\n")
    sha = hashlib.sha256(secret.read_bytes()).hexdigest()
    prev = R.replace(repo, "helper(", "assist(", globs=["*.py"])
    for key in ("../secret.txt", str(secret), "/dev/zero"):
        shas = {**prev["shas"], key: sha}
        t0 = time.monotonic()
        res = R.replace(repo, "helper(", "assist(", globs=["*.py"],
                        write=True, base_shas=shas)
        assert time.monotonic() - t0 < 10
        assert res["ok"] is False and key in res["stale"]
    assert "helper(1)" in (repo / "pkg/use.py").read_text()


def test_plan_id_binds_the_request(repo: Path) -> None:
    prev = R.replace(repo, "helper(", "safe(", globs=["*.py"])
    res = R.replace(repo, "helper(", "rm_rf(", globs=["*.py"], write=True,
                    base_shas=prev["shas"], plan_id=prev["plan_id"])
    assert res["ok"] is False and "stale preview" in res["error"]
    assert "rm_rf" not in (repo / "pkg/use.py").read_text()


def test_default_scope_is_text_files_without_dotfiles(repo: Path) -> None:
    _write(repo / ".env", "TOKEN=helper\n")
    _write(repo / "deploy" / "web.env", "X=helper\n")
    _write(repo / "Makefile", "all: helper\n")
    res = R.replace(repo, "helper", "h", max_files=50)
    files = set(_by_file(res))
    assert not files & {".env", "deploy/web.env", "Makefile"}
    assert {"notes.txt", "README.md", "data.json"} <= files
    with pytest.raises(CodeEditError, match="any_suffix"):
        R.replace(repo, "helper", "h", keys=["text:.env#L1-L1"])
    res = R.replace(repo, "helper", "h", max_files=50, any_suffix=True)
    assert {".env", "deploy/web.env", "Makefile"} <= set(_by_file(res))


# --------------------------------------------------------------------------- #
# Cost: deadline, early caps, linear bookkeeping, one scan per call
# --------------------------------------------------------------------------- #
def test_catastrophic_regex_is_abandoned_at_the_deadline(repo: Path) -> None:
    import time
    _write(repo / "evil.txt", "a" * 40 + "b\n")
    t0 = time.monotonic()
    res = R.replace(repo, r"(a+)+$", "x", regex=True, globs=["evil.txt"],
                    timeout_s=1, write=True)
    assert time.monotonic() - t0 < 6
    assert res["ok"] is False and res["timed_out"] is True
    assert "nothing written" in res["error"]
    assert (repo / "evil.txt").read_text() == "a" * 40 + "b\n"


def test_zero_width_pattern_stops_at_the_cap(repo: Path) -> None:
    import time
    _write(repo / "wide.txt", "y" * 200_000)
    t0 = time.monotonic()
    res = R.replace(repo, "x*", "-", regex=True, globs=["wide.txt"])
    assert time.monotonic() - t0 < 8
    assert res["ok"] is False and res["over_cap"] == "matches"
    t0 = time.monotonic()
    res = R.replace(repo, "y", "z", globs=["wide.txt"], max_matches=200_000)
    assert res["ok"] and res["total"] == 200_000
    assert len(res["files"][0]["lines"]) == 1
    assert time.monotonic() - t0 < 8


def test_file_entry_lines_are_counted_in_one_pass(repo: Path) -> None:
    _write(repo / "many.txt", "".join(f"row {i} helper\n" for i in range(3000)))
    res = R.replace(repo, "helper", "h", globs=["many.txt"])
    (f,) = res["files"]
    assert f["matches"] == 3000
    assert f["lines"] == list(range(1, R.LINES_PER_FILE + 1))


def test_key_spans_come_from_the_scanned_bytes(repo: Path, mem: DevMemory,
                                              monkeypatch) -> None:
    real = R._run_scan

    def touched(req, timeout):
        (repo / "pkg/mod.py").write_text("# moved\n" + MOD)
        return real(req, timeout)
    monkeypatch.setattr(R, "_run_scan", touched)
    res = R.replace(repo, "helper", "assist", keys=["sym:pkg/mod.py::other"],
                    write=True, memory=mem)
    assert res["ok"] is False and res["stale"] == ["pkg/mod.py"]
    assert "changed during the scan" in res["error"]
    assert "assist" not in (repo / "pkg/mod.py").read_text()


# --------------------------------------------------------------------------- #
# Intent tool, MCP, CLI
# --------------------------------------------------------------------------- #
def test_intent_replace_permission_preflight_and_apply(repo: Path,
                                                      mem: DevMemory) -> None:
    inp = {"pattern": "helper(", "replacement": "assist(", "glob": "*.py"}
    perm = permission_request(mem, "replace", inp)
    assert perm == {"action": "edit", "resources": ["pkg/mod.py", "pkg/use.py"],
                    "save": ["*"]}
    pre = handle(mem, "replace", inp, preflight=True)
    assert pre["preflight"] is True and pre["handled"] is False
    assert pre["permission"]["resources"] == ["pkg/mod.py", "pkg/use.py"]
    files = pre["permission"]["metadata"]["files"]
    assert [f["file"] for f in files] == ["pkg/mod.py", "pkg/use.py"]
    plan = pre["permission"]["metadata"]["plan_id"]
    assert len(plan) == 16
    assert "helper(1)" in (repo / "pkg/use.py").read_text()
    res = handle(mem, "replace", inp, granted_plan_id=plan)
    assert res["handled"] and not res["is_error"], res
    assert res["output"].startswith("Replaced 4 matches in 2 files")
    assert "[ozagia] pkg/use.py: 2 matches in sym:pkg/use.py::run" in res["output"]
    assert res["permission"]["resources"] == ["pkg/mod.py", "pkg/use.py"]
    assert res["meta"]["edited_files"] == ["pkg/mod.py", "pkg/use.py"]
    assert res["meta"]["replace_applies"] == 1
    assert res["meta"]["replace_files"] == 2
    assert "assist(1)" in (repo / "pkg/use.py").read_text()


def test_intent_replace_preview_and_refusal(repo: Path, mem: DevMemory) -> None:
    res = handle(mem, "replace", {"pattern": "helper", "replacement": "h",
                                  "globs": ["README.md"], "preview": True})
    assert res["handled"] and not res["is_error"]
    assert res["output"].startswith("Preview: 3 matches in 1 file;")
    assert "+Call h here." in res["output"] and "plan_id" in res["output"]
    assert res["meta"]["replace_previews"] == 1
    assert "helper" in (repo / "README.md").read_text()
    bad = handle(mem, "replace", {"pattern": "helper", "replacement": "h",
                                  "globs": ["README.md"], "expect": 1})
    assert bad["is_error"] and "expected 1" in bad["output"]
    assert bad["meta"]["intent"] == "replace_refused"
    pre = handle(mem, "replace", {"pattern": "helper", "replacement": "he lper",
                                  "globs": ["pkg/mod.py"]}, preflight=True)
    assert pre["handled"] and pre["is_error"] and "parse" in pre["output"]


def test_intent_apply_cannot_exceed_the_granted_files(repo: Path,
                                                      mem: DevMemory) -> None:
    inp = {"pattern": "helper(", "replacement": "assist(", "glob": "*.py"}
    pre = handle(mem, "replace", inp, preflight=True)
    plan = pre["permission"]["metadata"]["plan_id"]
    _write(repo / "pkg/new.py", "x = helper(3)\n")
    res = handle(mem, "replace", inp, granted_plan_id=plan)
    assert res["handled"] and res["is_error"]
    assert "stale preview" in res["output"]
    assert (repo / "pkg/new.py").read_text() == "x = helper(3)\n"
    assert "helper(1)" in (repo / "pkg/use.py").read_text()
    # An input plan_id other than the granted one is refused too.
    res = handle(mem, "replace", {**inp, "plan_id": "0" * 16},
                 granted_plan_id=plan)
    assert res["is_error"] and "granted plan" in res["output"]


def test_intent_preflight_scans_once(repo: Path, mem: DevMemory,
                                     monkeypatch) -> None:
    real = R._run_scan
    calls = []

    def counted(req, timeout):
        calls.append(timeout)
        return real(req, timeout)
    monkeypatch.setattr(R, "_run_scan", counted)
    inp = {"pattern": "helper(", "replacement": "assist(", "glob": "*.py",
           "_reply_deadline": __import__("time").monotonic() + 3}
    pre = handle(mem, "replace", inp, preflight=True)
    assert pre["preflight"] and len(calls) == 1
    assert 0.5 <= calls[0] <= 3
    res = handle(mem, "replace", inp,
                 granted_plan_id=pre["permission"]["metadata"]["plan_id"])
    assert not res["is_error"] and len(calls) == 2


def test_intent_preview_is_not_preflighted(repo: Path, mem: DevMemory) -> None:
    inp = {"pattern": "helper", "replacement": "h", "globs": ["README.md"],
           "preview": True}
    res = handle(mem, "replace", inp, preflight=True)
    assert res["handled"] is True and not res.get("preflight")
    assert not res["is_error"] and res["output"].startswith("Preview:")
    assert res["permission"] == {"action": "read", "resources": ["README.md"],
                                 "save": ["*"]}
    assert "helper" in (repo / "README.md").read_text()


def test_intent_partial_apply_is_an_error(repo: Path, mem: DevMemory,
                                          monkeypatch) -> None:
    _break_after_first_write(monkeypatch, repo)
    res = handle(mem, "replace", {"pattern": "helper", "replacement": "assist",
                                  "glob": "pkg/*.py"})
    assert res["handled"] and res["is_error"]
    assert res["output"].startswith("Replace partially applied:")
    assert "left written (2 matches): pkg/mod.py" in res["output"]
    assert res["meta"]["intent"] == "replace_partial"
    assert res["meta"]["edited_files"] == ["pkg/mod.py"]
    (entry,) = [e for e in _journal(mem) if e["kind"] == "replace"]
    assert ": 2 match(es) in 1 file(s)" in entry["summary"]
    assert entry["files"] == ["pkg/mod.py"] and entry["partial"] is True


def test_intent_restored_batch_is_a_refusal(repo: Path, mem: DevMemory,
                                            monkeypatch) -> None:
    from ozagia.edit import txn
    real = txn._current_bytes
    monkeypatch.setattr(txn, "_current_bytes",
                        lambda p: b"x" if p.name == "use.py" else real(p))
    res = handle(mem, "replace", {"pattern": "helper", "replacement": "assist",
                                  "glob": "pkg/*.py"})
    assert res["is_error"] and res["output"].startswith("Replace refused:")
    assert "restored 1 file(s)" in res["output"]
    assert (repo / "pkg/mod.py").read_text() == MOD


def test_daemon_binds_replace_to_its_preflight(repo: Path, mem: DevMemory,
                                               tmp_path: Path) -> None:
    from ozagia.daemon.server import DaemonState
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    msg = {"op": "tool", "tool": "replace", "harness": "opencode",
           "session": "s1",
           "input": {"pattern": "helper(", "replacement": "assist(",
                     "glob": "*.py"}}
    pre = st.dispatch({**msg, "preflight": True})
    assert pre["preflight"] and pre["permission"]["resources"] == [
        "pkg/mod.py", "pkg/use.py"]
    _write(repo / "pkg/new.py", "x = helper(3)\n")
    res = st.dispatch(msg)
    assert res["handled"] and res["is_error"] and "stale" in res["output"]
    assert (repo / "pkg/new.py").read_text() == "x = helper(3)\n"
    # A fresh preflight grants the new set; the apply then writes it.
    pre = st.dispatch({**msg, "preflight": True})
    assert "pkg/new.py" in pre["permission"]["resources"]
    res = st.dispatch(msg)
    assert res["handled"] and not res["is_error"], res
    assert res["meta"]["edited_files"] == ["pkg/mod.py", "pkg/new.py",
                                           "pkg/use.py"]
    # A granted permission sent back by the caller binds the call as well.
    _write(repo / "pkg/more.py", "y = assist(4)\n")
    msg2 = {**msg, "input": {"pattern": "assist(", "replacement": "aid(",
                             "glob": "*.py"}}
    pre = st.dispatch({**msg2, "preflight": True})
    _write(repo / "pkg/late.py", "z = assist(5)\n")
    res = st.dispatch({**msg2, "granted": pre["permission"]})
    assert res["is_error"] and "stale" in res["output"]
    assert "assist" in (repo / "pkg/late.py").read_text()


def test_daemon_counts_replace(repo: Path, mem: DevMemory, tmp_path: Path) -> None:
    from ozagia.daemon.server import REPLACE_COUNTERS, DaemonState
    assert REPLACE_COUNTERS == ("replace_previews", "replace_applies",
                                "replace_files")
    state = DaemonState(tmp_path, None, tmp_path / "j.jsonl")
    msg = {"op": "tool", "tool": "replace"}
    state.record(msg, {"ok": True, "handled": True,
                       "meta": {"intent": "replace_preview",
                                "replace_previews": 1}}, 1.0)
    state.record(msg, {"ok": True, "handled": True,
                       "meta": {"intent": "replace", "replace_applies": 1,
                                "replace_files": 3}}, 1.0)
    stats = state.stats()
    assert (stats["replace_previews"], stats["replace_applies"],
            stats["replace_files"]) == (1, 1, 3)
    from ozagia.eval.agent import graph_stats_fields
    fields = graph_stats_fields(stats)
    assert (fields["replace_previews"], fields["replace_applies"],
            fields["replace_files"]) == (1, 1, 3)


def _mcp(server: DevMemoryServer, name: str, args: dict) -> dict:
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
    return json.loads(resp["result"]["content"][0]["text"])


def test_mcp_dev_replace_preview_then_apply(repo: Path, mem: DevMemory) -> None:
    server = DevMemoryServer(mem)
    tools = {t["name"] for t in server._tools}
    assert "dev_replace" in tools
    prev = _mcp(server, "dev_replace", {"pattern": "helper(",
                                        "replacement": "assist(",
                                        "globs": ["*.py"]})
    assert prev["ok"] and prev["applied"] is False and prev["total"] == 4
    assert "helper(1)" in (repo / "pkg/use.py").read_text()
    res = _mcp(server, "dev_replace", {"pattern": "helper(",
                                       "replacement": "assist(",
                                       "globs": ["*.py"], "apply": True,
                                       "base_shas": prev["shas"],
                                       "plan_id": prev["plan_id"]})
    assert res["ok"] and res["applied"] and res["written"] == [
        "pkg/mod.py", "pkg/use.py"]
    assert res["journaled"]["kind"] == "replace"
    assert "lint" in res
    stale = _mcp(server, "dev_replace", {"pattern": "assist(",
                                         "replacement": "x(", "globs": ["*.py"],
                                         "apply": True,
                                         "plan_id": prev["plan_id"]})
    assert stale["ok"] is False and "stale" in stale["error"]


def test_mcp_apply_needs_plan_id(repo: Path, mem: DevMemory) -> None:
    server = DevMemoryServer(mem)
    args = {"pattern": "helper(", "replacement": "assist(", "globs": ["*.py"]}
    prev = _mcp(server, "dev_replace", args)
    for extra in ({}, {"base_shas": prev["shas"]}):
        res = _mcp(server, "dev_replace", {**args, "apply": True, **extra})
        assert res["ok"] is False and "plan_id" in res["error"]
    # base_shas of a preview of another replacement never authorises it.
    res = _mcp(server, "dev_replace", {**args, "replacement": "rm_rf(",
                                       "apply": True,
                                       "base_shas": prev["shas"]})
    assert res["ok"] is False and "plan_id" in res["error"]
    assert "helper(1)" in (repo / "pkg/use.py").read_text()
    desc = next(t for t in server._tools if t["name"] == "dev_replace")
    text = desc["description"]
    assert "gitignored files are never touched" not in text
    assert "(required; base_shas alone is not enough)" in text
    assert "restored" in text and "partial" in text


def test_cli_replace_preview_and_apply(repo: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(repo)
    rc = cli.main(["replace", "helper(", "assist(", "--glob", "*.py",
                   "--root", str(repo)])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("preview: 4 matches in 2 files")
    assert "plan_id" in out and "helper(1)" in (repo / "pkg/use.py").read_text()
    # --apply without --plan-id is refused before any scan.
    rc = cli.main(["replace", "helper(", "assist(", "--glob", "*.py",
                   "--root", str(repo), "--apply"])
    assert rc == 2 and "--plan-id" in capsys.readouterr().err
    args = ["replace", r"helper\((\d)\)", r"assist(\1)", "--regex",
            "--glob", "pkg/use.py", "--root", str(repo)]
    rc = cli.main([*args, "--json"])
    plan = json.loads(capsys.readouterr().out)["plan_id"]
    rc = cli.main([*args, "--apply", "--plan-id", plan, "--expect", "1"])
    assert rc == 1 and "expected 1" in capsys.readouterr().err
    rc = cli.main([*args, "--apply", "--plan-id", "0" * 16])
    assert rc == 1 and "stale preview" in capsys.readouterr().err
    rc = cli.main([*args, "--apply", "--plan-id", plan, "--expect", "2",
                   "--json"])
    res = json.loads(capsys.readouterr().out)
    assert rc == 0 and res["applied"] and res["written"] == ["pkg/use.py"]
    assert "assist(1) + assist(2)" in (repo / "pkg/use.py").read_text()
    journal = repo / ".ozagia" / "journal.jsonl"
    kinds = [json.loads(x)["kind"] for x in journal.read_text().splitlines()]
    assert kinds.count("replace") == 1


def test_glob_regex() -> None:
    rx = R.glob_regex("src/**/*.py")
    assert rx.match("src/a.py") and rx.match("src/x/y/a.py")
    assert not rx.match("src/a.pyc") and not rx.match("lib/a.py")
    assert R.glob_regex("a?[bc].txt").match("axb.txt")
    assert not R.glob_regex("*.py").match("a/b.py")
    assert R.parse_flags("IGNORECASE,MULTILINE")[1] == "im"


def test_ownership_warning_for_another_live_sessions_file(repo, mem, monkeypatch):
    class Owners:
        def warnings(self, journal, rel, harness, session):
            return ([f"[ozagia] warning: {rel} has uncommitted edits by "
                     f"another live session"] if rel == "pkg/use.py" else [])
    res = handle(mem, "replace", {"pattern": "helper(", "replacement": "h(",
                                  "glob": "*.py"}, owners=Owners())
    assert res["handled"] and not res["is_error"]
    assert "pkg/use.py has uncommitted edits" in res["output"]
    assert res["meta"]["ownership_warnings"] == 1


def test_scan_worker_never_imports_from_the_repo(repo: Path) -> None:
    marker = repo / "pwned.marker"
    for rel in ("ozagia/__init__.py", "ozagia/edit/__init__.py"):
        _write(repo / rel, "")
    _write(repo / "ozagia/edit/replace_worker.py",
           f"open({str(marker)!r}, 'w').write('x')\n")
    res = R.replace(repo, "helper(", "assist(", globs=["pkg/*.py"])
    assert res["ok"] and res["total"] == 4
    assert not marker.exists()
