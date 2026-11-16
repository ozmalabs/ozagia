"""Per-session read ledger, repeat stubs and notes, session events,
outline-first reads and section keys (`ozagia.intent.ledger`,
`ozagia.core.sections`, `ozagia.intent.read`)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ozagia import intent as I
from ozagia.config import ConfigError, clear_config_cache, load_project_config
from ozagia.core import memory as M
from ozagia.core.memory import DevMemory, build_index
from ozagia.core.sections import doc_sections, slugify
from ozagia.intent import ledger as L

BIG_PY = ("import os\n\n\n"
          + "".join(f"def f{i}(x):\n    y = x + {i}\n    return y\n\n\n"
                    for i in range(1, 81))
          + "class Box:\n    def a(self):\n        return 1\n\n"
          "    def b(self):\n        return 2\n")
SMALL_PY = "".join(f"V{i} = {i}\n" for i in range(1, 41))
DOC = ("# Title\n\nintro\n\n## Install\n\n" + "step\n" * 200
       + "\n### Details\n\nmore\n\n## Usage\n\n" + "use\n" * 250
       + "\n## Usage\n\nagain\n")
TEX = ("\\documentclass{article}\n\\begin{document}\n"
       "\\section{Intro}\n" + "text\n" * 220
       + "% \\section{Commented}\n\\subsection*{The \\emph{Main} Result}\n"
       + "proof\n" * 220 + "\\section{End}\nbye\n\\end{document}\n")


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ("README.md", "docs/*.md"))
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ("README.md", "docs/*.md"))
    monkeypatch.setattr(M, "IMPORT_ROOTS", ())
    for var in ("OZAGIA_CONFIG", "OZAGIA_ADAPTERS", "OZAGIA_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    clear_config_cache()
    root = tmp_path / "repo"
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "big.py", BIG_PY)
    _write(root / "pkg" / "small.py", SMALL_PY)
    _write(root / "docs" / "plan.md", DOC)
    _write(root / "paper" / "main.tex", TEX)
    _write(root / "data.json", json.dumps({"k": list(range(3000))}, indent=1))
    _write(root / "notes.txt", "".join(f"note {i}\n" for i in range(1, 501)))
    return root


@pytest.fixture
def mem(repo, tmp_path):
    dump = tmp_path / "state" / "graph.json"
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=repo)


def _config(repo: Path, text: str) -> None:
    (repo / ".ozagia.toml").write_text(text, encoding="utf-8")
    clear_config_cache()


def _state(policy: str = "stub", session: str = "s1", harness: str = "opencode",
           store: L.SessionStore | None = None) -> L.SessionState:
    return (store or L.SessionStore()).get(harness, session, policy=policy)


def _read(mem, state, **inp):
    res = I.handle(mem, "read", inp, state=state)
    assert res["handled"] and not res["is_error"], res
    return res


def _shell(mem, state, command, **extra):
    res = I.handle(mem, "shell", {"command": command, **extra}, state=state)
    assert res["handled"] and not res["is_error"], res
    return res


# --------------------------------------------------------------------------- #
# the store: bounds and events
# --------------------------------------------------------------------------- #
def test_store_is_an_lru_of_sessions():
    store = L.SessionStore(max_sessions=3)
    for s in ("a", "b", "c"):
        store.get("opencode", s)
    store.get("opencode", "a")            # a is now the most recent
    store.get("opencode", "d")
    assert len(store) == 3 and store.evicted == 1
    assert store.get("opencode", "b").reads == 0    # b was dropped (new state)
    assert store.get("opencode", None) is None and store.get("opencode", "") is None


def test_sub_keys_are_separate_ledgers_and_events_cover_all(tmp_path):
    store = L.SessionStore()
    parent = store.get("codex", "s", "t1")
    child = store.get("codex", "s", "t2")
    assert parent is not child
    p = tmp_path / "f.txt"
    p.write_text("a\nb\n")
    spec = L.ReadSpec("f.txt", p, 1, 2, "x" * 64, ["a", "b"])
    parent.record(spec)
    assert parent.find(spec) and child.find(spec) is None
    child.record(spec)
    assert store.event("codex", "s", "compact") == 2
    assert parent.find(spec) is None and child.find(spec) is None
    assert parent.compactions == 1
    assert store.event("codex", "s", "end") == 2 and len(store) == 0
    with pytest.raises(ValueError):
        store.event("codex", "s", "bogus")


def test_ranges_and_files_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "MAX_RANGES", 3)
    monkeypatch.setattr(L, "MAX_FILES", 2)
    st = _state()
    lines = [str(i) for i in range(100)]
    for a in range(1, 6):
        st.record(L.ReadSpec("x", tmp_path, a, a, "s", lines))
    assert [r.a for r in st.files["x"]] == [3, 4, 5]
    st.record(L.ReadSpec("y", tmp_path, 1, 1, "s", lines))
    st.record(L.ReadSpec("z", tmp_path, 1, 1, "s", lines))
    assert list(st.files) == ["y", "z"]


def test_containment_overlap_and_sha(tmp_path):
    st = _state()
    lines = ["l"] * 100

    def spec(a, b, sha="s"):
        return L.ReadSpec("x", tmp_path, a, b, sha, lines)
    first = st.record(spec(10, 50))
    assert st.find(spec(10, 50)) == first
    assert st.find(spec(20, 30)) == first           # a subset is a repeat
    assert st.find(spec(5, 30)) is None              # overlap is not
    assert st.overlaps(spec(5, 30)) == [first]
    assert st.find(spec(20, 30, sha="t")) is None    # the file changed
    st.record(spec(1, 5, sha="t"))
    assert list(st.files["x"]) == [L.Read(2, 0, 1, 5, "t")]   # old sha dropped
    assert L.note_lines(spec(3, 9, "t"), None, st.overlaps(spec(3, 9, "t"))) == [
        "[ozagia] lines 3-5 were already read unchanged (read 2)"]


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #
def test_harness_defaults(repo):
    assert L.policy_for(repo, "opencode") == "stub"
    assert L.policy_for(repo, "claude") == "stub"
    # Codex subagent hooks carry the parent's session id: note, not stub.
    assert L.policy_for(repo, "codex") == "note"
    assert L.policy_for(repo, "native") == "note"
    assert L.policy_for(repo, "kiro") == "note"


def test_policy_config(repo):
    _config(repo, '[reads]\nrepeat = "off"\nrepeat_by_harness = {codex = "stub"}\n')
    assert L.policy_for(repo, "opencode") == "off"
    assert L.policy_for(repo, "codex") == "stub"
    _config(repo, '[reads]\nrepeat = "always"\n')
    with pytest.raises(ConfigError):
        load_project_config(repo)
    assert L.policy_for(repo, "claude") == "stub"     # a bad file: defaults
    _config(repo, '[reads]\noutline_lines = 10\noutline_bytes = 0\n')
    cfg = load_project_config(repo)
    assert (cfg.outline_lines, cfg.outline_bytes, cfg.repeat) == (10, 0, None)


# --------------------------------------------------------------------------- #
# served reads: stub, note, off
# --------------------------------------------------------------------------- #
def test_stub_for_an_unchanged_repeat(mem, repo):
    st = _state("stub")
    first = _read(mem, st, path="pkg/small.py", offset=2, limit=39)
    assert "repeat" not in first["meta"]
    res = _read(mem, st, path="pkg/small.py", offset=6, limit=30)
    out = res["output"]
    assert "\n" not in out
    assert re.match(r"^\[ozagia\] pkg/small\.py unchanged since read 1 \(lines 6-35, "
                    r"sha [0-9a-f]{12}\); keys: \S.*; read with a different range "
                    r"or after an edit to get text$", out), out
    assert res["meta"]["intent"] == "read_stub"
    assert res["meta"]["repeat"] == "stub" and res["meta"]["bytes_saved"] > 0
    assert res["permission"]["action"] == "read"
    # Another session reads the text.
    other = _read(mem, _state("stub", session="s2"), path="pkg/small.py",
                  offset=6, limit=3)
    assert other["output"].startswith("Read file")


def test_edit_and_compaction_give_the_text_again(mem, repo):
    store = L.SessionStore()
    st = store.get("opencode", "s1", policy="stub")
    _read(mem, st, path="pkg/small.py")
    assert _read(mem, st, path="pkg/small.py")["meta"]["intent"] == "read_stub"
    (repo / "pkg" / "small.py").write_text(SMALL_PY + "V41 = 41\n")
    assert _read(mem, st, path="pkg/small.py")["output"].startswith("Read file")
    assert _read(mem, st, path="pkg/small.py")["meta"]["intent"] == "read_stub"
    store.event("opencode", "s1", "compact")
    assert _read(mem, st, path="pkg/small.py")["output"].startswith("Read file")


def test_note_policy_appends_a_line(mem):
    st = _state("note")
    _read(mem, st, path="pkg/small.py", offset=1, limit=20)
    res = _read(mem, st, path="pkg/small.py", offset=2, limit=5)
    lines = res["output"].split("\n")
    assert lines[0].startswith("Read file") and "6: V6 = 6" in lines
    assert re.match(r"^\[ozagia\] unchanged since read 1 \(lines 2-6, sha [0-9a-f]{12}\)$",
                    lines[-1])
    assert res["meta"]["repeat"] == "note"


def test_partial_overlap_returns_text_and_names_the_lines(mem):
    st = _state("stub")
    _read(mem, st, path="pkg/small.py", offset=1, limit=10)
    res = _read(mem, st, path="pkg/small.py", offset=5, limit=10)
    lines = res["output"].split("\n")
    assert "14: V14 = 14" in lines
    assert lines[-1] == "[ozagia] lines 5-10 were already read unchanged (read 1)"
    assert "repeat" not in res["meta"]


def test_off_policy_and_no_state_change_nothing(mem):
    st = _state("off")
    _read(mem, st, path="pkg/small.py")
    assert _read(mem, st, path="pkg/small.py")["output"].startswith("Read file")
    assert st.files == {}
    a = I.handle(mem, "read", {"path": "pkg/small.py"})
    b = I.handle(mem, "read", {"path": "pkg/small.py"})
    assert a["output"] == b["output"]


def test_node_key_reads_are_ranges_too(mem):
    st = _state("stub")
    _read(mem, st, path="pkg/big.py", offset=1, limit=100)
    res = _read(mem, st, path="sym:pkg/big.py::f2")
    assert res["meta"]["intent"] == "read_stub"
    assert "sym:pkg/big.py::f2" in res["output"]


def test_preflight_is_not_recorded(mem):
    st = _state("stub")
    res = I.handle(mem, "shell", {"command": "sed -n '1,5p' pkg/small.py"},
                   state=st, preflight=True)
    assert res.get("preflight") and st.files == {}


# --------------------------------------------------------------------------- #
# shell reads
# --------------------------------------------------------------------------- #
def test_shell_paging_and_repeats(mem, repo):
    st = _state("stub")
    _shell(mem, st, "sed -n '1,30p' pkg/small.py")
    res = _shell(mem, st, "sed -n '10,20p' pkg/small.py")
    assert res["meta"]["intent"] == "shell_read_stub"
    res = _shell(mem, st, "head -n 5 pkg/small.py")
    assert res["meta"]["intent"] == "shell_read_stub"
    res = _shell(mem, st, "cd pkg && tail -n 3 small.py")
    assert res["meta"]["intent"] == "shell_read"           # 38-40: new lines
    res = _shell(mem, st, "nl -ba pkg/small.py | sed -n '38,40p'")
    assert res["meta"]["intent"] == "shell_read_stub"
    res = _shell(mem, st, "sed -n '3,4p' small.py", workdir=str(repo / "pkg"))
    assert res["meta"]["intent"] == "shell_read_stub"


def test_read_spec_ranges(mem, repo):
    def spec(command, **extra):
        got = L.read_spec(mem, "shell", {"command": command, **extra}, served=False)
        return None if got is None else (got.rel, got.a, got.b)
    assert spec("sed -n '5,9p' pkg/small.py") == ("pkg/small.py", 5, 9)
    assert spec("nl -ba pkg/small.py | sed -n '5,9p'") == ("pkg/small.py", 5, 9)
    assert spec("sed -n '5,30p' pkg/small.py | head -n 3") == ("pkg/small.py", 5, 7)
    assert spec("tail -n 20 pkg/small.py | tail -n +3") == ("pkg/small.py", 23, 40)
    assert spec("cd pkg && cat small.py") == ("pkg/small.py", 1, 40)
    assert spec("cat pkg/big.py", ) == ("pkg/big.py", 1, len(BIG_PY.splitlines()))
    assert spec("cat pkg/small.py | grep V1") is None
    assert spec("tail -c 20 pkg/small.py") is None
    assert spec("sed -n '1,2p' pkg/small.py; sed -n '1,2p' pkg/big.py") is None
    assert spec("sed -n '900,950p' pkg/small.py") is None
    assert spec("make") is None
    got = L.read_spec(mem, "read", {"path": "pkg/big.py"}, served=False)
    assert (got.a, got.b) == (1, len(BIG_PY.splitlines()))      # a built-in Read
    got = L.read_spec(mem, "read", {"path": "pkg/big.py"}, served=True)
    assert (got.a, got.b) == (1, 120)                            # the outline's head
    assert L.read_spec(mem, "read", {"path": "data.json"}, served=True) is None
    assert L.read_spec(mem, "read", {"path": "pkg"}, served=True) is None


def test_after_records_builtin_reads_and_notes(mem):
    st = _state("note", harness="codex")
    inp = {"command": "sed -n '1,10p' pkg/small.py"}
    first = I.after(mem, "shell", inp, "V1 = 1\n", state=st)
    assert first["append"] == "" and first["meta"]["intent"] == "after_read"
    again = I.after(mem, "shell", {"command": "sed -n '2,3p' pkg/small.py"}, "x",
                    state=st)
    assert re.match(r"^\[ozagia\] unchanged since read 1 \(lines 2-3, sha ", again["append"])
    assert again["meta"]["repeat"] == "note"
    # Claude's built-in Read, through after.
    claude = _state("stub", harness="claude")
    I.after(mem, "read", {"path": "pkg/small.py", "offset": 1, "limit": 2000}, "", state=claude)
    assert claude.find(L.read_spec(mem, "read", {"path": "pkg/small.py"}, served=False))


def test_check_read_for_hooks(mem):
    st = _state("stub", harness="claude")
    inp = {"path": "pkg/small.py", "offset": 3, "limit": 4}
    got = I.check_read(mem, "read", inp, state=st)
    assert (got["repeat"], got["stub"]) == (False, None)
    assert st.files == {}                     # a check records nothing
    I.after(mem, "read", inp, "", state=st)
    got = I.check_read(mem, "read", inp, state=st)
    assert got["repeat"] and got["stub"].startswith("[ozagia] pkg/small.py unchanged")
    assert got["meta"]["repeat"] == "stub"
    note = _state("note", harness="codex")
    I.after(mem, "read", inp, "", state=note)
    got = I.check_read(mem, "read", inp, state=note)
    assert got["repeat"] and got["stub"] is None and "repeat" not in got["meta"]
    assert I.check_read(mem, "read", inp, state=None)["policy"] == "off"


def test_call_index_counts_each_call_once(mem):
    st = _state("stub", harness="claude")
    I.check_read(mem, "shell", {"command": "sed -n '1,2p' pkg/small.py"}, state=st)
    I.after(mem, "shell", {"command": "sed -n '1,2p' pkg/small.py"}, "", state=st)
    I.after(mem, "shell", {"command": "ls"}, "", state=st)
    assert st.calls == 2
    (rec,) = st.files["pkg/small.py"]
    assert (rec.n, rec.call) == (1, 1)


# --------------------------------------------------------------------------- #
# outline-first reads
# --------------------------------------------------------------------------- #
def test_python_outline_with_keys_then_the_head(mem, repo):
    res = _read(mem, None, path="pkg/big.py")
    out = res["output"].split("\n")
    n = len(BIG_PY.splitlines())
    assert out[0] == f"Read file {repo / 'pkg/big.py'}, outline of {n} lines, {len(BIG_PY)} bytes"
    assert res["meta"]["intent"] == "read_outline"
    assert "[ozagia] L4-6 sym:pkg/big.py::f1" in out
    box = next(line for line in out if line.endswith("sym:pkg/big.py::Box"))
    assert re.match(r"^\[ozagia\] L\d+-\d+ sym:pkg/big\.py::Box$", box)
    assert any(re.match(r"^\[ozagia\]   L\d+-\d+ sym:pkg/big\.py::Box\.a$", x) for x in out)
    assert "sym:pkg/big.py::f80" in res["meta"]["keys"]
    assert any("an outline instead of the whole text" in x and "Lines 1-120 follow" in x
               for x in out)
    assert out[-1] == f"120: {BIG_PY.splitlines()[119]}"
    assert res["meta"]["bytes_saved"] > 0
    # A range or a small file is the text, as before.
    assert _read(mem, None, path="pkg/big.py", offset=1)["meta"]["intent"] == "read_file"
    assert _read(mem, None, path="pkg/small.py")["meta"]["intent"] == "read_file"


def test_outline_thresholds(mem, repo):
    _config(repo, "[reads]\noutline_lines = 0\noutline_bytes = 0\n")
    assert _read(mem, None, path="pkg/big.py")["meta"]["intent"] == "read_file"
    _config(repo, "[reads]\noutline_lines = 30\noutline_bytes = 0\n")
    assert _read(mem, None, path="pkg/small.py")["meta"]["intent"] == "read_outline"
    _config(repo, "[reads]\noutline_lines = 0\noutline_bytes = 100\n")
    assert _read(mem, None, path="pkg/small.py")["meta"]["intent"] == "read_outline"
    _config(repo, "[reads]\noutline_lines = 41\noutline_bytes = 1000\n")
    assert _read(mem, None, path="pkg/small.py")["meta"]["intent"] == "read_file"


def test_structured_files_keep_their_own_outline(mem):
    res = _read(mem, None, path="data.json")
    assert res["meta"]["intent"] == "read_structured_outline"


def test_text_file_outline_lists_chunks(mem):
    res = _read(mem, None, path="notes.txt")
    assert res["meta"]["intent"] == "read_outline"
    assert "[ozagia] L1-60 text:notes.txt#L1-L60" in res["output"].split("\n")


def test_markdown_outline_and_section_keys(mem, repo):
    res = _read(mem, None, path="docs/plan.md")
    out = res["output"].split("\n")
    assert res["meta"]["intent"] == "read_outline"
    secs = {s.slug: s for s in doc_sections("docs/plan.md", DOC)}
    assert list(secs) == ["title", "install", "details", "usage", "usage-1"]
    ins = secs["install"]
    assert f"[ozagia]   L{ins.start}-{ins.end} doc:docs/plan.md#install  Install" in out
    assert secs["details"].end == secs["usage"].start - 1 == ins.end
    # A section key reads the whole section, subsections included.
    sec = _read(mem, None, path="doc:docs/plan.md#install")
    lines = sec["output"].split("\n")
    assert lines[0].endswith(f"lines {ins.start}-{ins.end} of {len(DOC.splitlines())}")
    assert lines[1] == "[ozagia] doc:docs/plan.md#install"
    assert lines[2] == f"{ins.start}: ## Install" and "### Details" in sec["output"]
    assert sec["meta"]["keys"] == ["doc:docs/plan.md#install"]
    second = _read(mem, None, path="doc:docs/plan.md#usage-1")
    assert second["output"].split("\n")[-1].endswith(": again")
    miss = I.handle(mem, "read", {"path": "doc:docs/plan.md#instal"})
    assert miss["is_error"] and "doc:docs/plan.md#install" in miss["output"]
    # A section read is a range in the ledger.
    st = _state("stub")
    _read(mem, st, path="doc:docs/plan.md#install")
    again = _read(mem, st, path="docs/plan.md", offset=ins.start + 1, limit=5)
    assert again["meta"]["intent"] == "read_stub"


def test_tex_sections(mem, repo):
    secs = doc_sections("paper/main.tex", TEX)
    assert [(s.level, s.title, s.slug) for s in secs] == [
        (2, "Intro", "intro"), (3, "The Main Result", "the-main-result"),
        (2, "End", "end")]
    assert secs[0].end == secs[2].start - 1
    res = _read(mem, None, path="paper/main.tex")
    assert res["meta"]["intent"] == "read_outline"
    assert "doc:paper/main.tex#the-main-result" in res["meta"]["keys"]
    sec = _read(mem, None, path="doc:paper/main.tex#end")
    assert sec["output"].split("\n")[2].endswith("\\section{End}")


def test_slugs():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  W5. Per-session state ") == "w5-per-session-state"
    assert slugify("!!!") == "section"
    secs = doc_sections("a.md", "# A\n## A\n# A-1\n# A\n")
    assert [s.slug for s in secs] == ["a", "a-1", "a-1-1", "a-2"]


def test_cat_without_range_is_an_outline(mem, repo):
    res = _shell(mem, None, "cat pkg/big.py")
    assert res["meta"]["intent"] == "shell_read_outline"
    assert "sym:pkg/big.py::f1" in res["output"]
    assert _shell(mem, None, "cat pkg/small.py")["meta"]["intent"] == "shell_read"
    assert _shell(mem, None, "head -n 500 pkg/big.py")["meta"]["intent"] == "shell_read"
    assert _shell(mem, None, "nl -ba pkg/big.py")["meta"]["intent"] == "shell_read"
    # The ledger records only the head the outline showed.
    st = _state("stub")
    _shell(mem, st, "cat pkg/big.py")
    assert _shell(mem, st, "sed -n '1,120p' pkg/big.py")["meta"]["intent"] == "shell_read_stub"
    assert _shell(mem, st, "sed -n '1,121p' pkg/big.py")["meta"]["intent"] == "shell_read"
    assert _shell(mem, st, "cat pkg/big.py")["meta"]["intent"] == "shell_read_stub"


# --------------------------------------------------------------------------- #
# the daemon: ops, keys and counters
# --------------------------------------------------------------------------- #
def test_daemon_ledger_ops_and_counters(mem, repo, tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")

    def req(msg):
        resp = st.dispatch(msg)
        st.record(msg, resp, 1.0)
        return resp
    read = {"op": "tool", "harness": "opencode", "session": "ses_1", "tool": "read",
            "input": {"path": "pkg/small.py"}}
    assert req(read)["output"].startswith("Read file")
    assert req(read)["meta"]["intent"] == "read_stub"
    assert req({**read, "session": "ses_2"})["output"].startswith("Read file")
    assert req({"op": "session_event", "harness": "opencode", "session": "ses_1",
                "event": "compact"})["cleared"] == 1
    assert req(read)["output"].startswith("Read file")
    assert req({**read, "input": {"path": "pkg/big.py"}})["meta"]["intent"] == "read_outline"
    # Codex: note by default, through after; read_check never stubs.
    sh = {"harness": "codex", "session": "thr", "sub": "/x/rollout.jsonl", "tool": "shell",
          "input": {"command": "sed -n '1,9p' pkg/small.py"}}
    assert req({"op": "read_check", **sh})["stub"] is None
    req({"op": "after", **sh, "output": "..."})
    chk = req({"op": "read_check", **sh})
    assert chk["repeat"] and chk["policy"] == "note" and chk["stub"] is None
    assert "unchanged since read 1" in req({"op": "after", **sh, "output": "."})["append"]
    # Claude: stub through read_check.
    cl = {"harness": "claude", "session": "c", "tool": "read",
          "input": {"path": "pkg/small.py"}}
    req({"op": "after", **cl, "output": ""})
    assert req({"op": "read_check", **cl})["stub"].startswith("[ozagia] pkg/small.py")
    assert req({"op": "session_event", "harness": "claude", "session": "c",
                "event": "end"})["cleared"] == 1
    assert req({"op": "read_check", **cl})["stub"] is None
    stats = st.stats()
    assert stats["repeats_seen"] == 3
    assert (stats["stubs_served"], stats["notes_served"]) == (2, 1)
    assert stats["outlines_served"] == 1
    assert stats["bytes_saved"] > 0
    assert stats["ledger"]["events"] == {"compact": 1, "end": 1}
    bad = st.dispatch({"op": "session_event", "session": "x", "event": "nap"})
    assert bad["ok"] is False
    assert st.dispatch({"op": "session_event", "event": "end"})["ok"] is False
    assert st.dispatch({"op": "read_check", "tool": "read"})["ok"] is False


def test_daemon_policy_follows_config(mem, repo, tmp_path):
    from ozagia.daemon.server import DaemonState
    _config(repo, '[reads]\nrepeat_by_harness = {opencode = "note"}\n')
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    read = {"op": "tool", "harness": "opencode", "session": "s", "tool": "read",
            "input": {"path": "pkg/small.py"}}
    st.dispatch(read)
    assert st.dispatch(read)["meta"]["repeat"] == "note"
    # No session id: no ledger.
    anon = {**read, "session": None}
    st.dispatch(anon)
    assert "repeat" not in st.dispatch(anon)["meta"]
