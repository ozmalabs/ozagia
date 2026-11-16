"""Patch and edit miss recovery (W6): the apply_patch parser and Codex's
matching semantics (`ozagia.intent.patch`), nearest regions, the Codex
PreToolUse hook on apply_patch, edit misses in `after`, and key-addressed
edits (`ozagia.edit.key_edit`, intent `edit` with `key`, MCP
`dev_key_edit`)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from ozagia import intent as I
from ozagia.core import memory as M
from ozagia.core.memory import DevMemory, build_index
from ozagia.daemon import protocol
from ozagia.integrations.codex import hook, install as inst
from ozagia.intent import patch as P
from ozagia.intent.patch import (
    PatchParseError,
    check_patch,
    nearest_region,
    parse_patch,
    seek_sequence,
    split_source,
)

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def _patch(*body: str) -> str:
    return "\n".join(["*** Begin Patch", *body, "*** End Patch"]) + "\n"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _node_sha(path: Path, a: int, b: int) -> str:
    """The sha256 of lines a..b (1-based, inclusive) of `path`."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    return hashlib.sha256("".join(lines[a - 1:b]).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def test_parse_every_header():
    parsed = parse_patch(_patch(
        "*** Environment ID: local",
        "*** Add File: path/add.py",
        "+abc",
        "+def",
        "*** Delete File: path/delete.py",
        "*** Update File: path/update.py",
        "*** Move to: path/update2.py",
        "@@ def f():",
        "-    pass",
        "+    return 123",
        "@@",
        " keep",
        "-old",
        "+new",
        "",
        "*** End of File",
        "*** Update File: other.py",
        " import foo",
        "+bar"))
    assert parsed.env_id == "local"
    ops = parsed.ops
    assert [(o.kind, o.path) for o in ops] == [
        ("add", "path/add.py"), ("delete", "path/delete.py"),
        ("update", "path/update.py"), ("update", "other.py")]
    assert ops[0].contents == "abc\ndef\n"
    up = ops[2]
    assert up.move_to == "path/update2.py"
    assert up.chunks[0].context == "def f():"
    assert (up.chunks[0].old, up.chunks[0].new) == (["    pass"], ["    return 123"])
    c = up.chunks[1]
    assert c.context is None and c.eof is True
    assert c.old == ["keep", "old", ""] and c.new == ["keep", "new", ""]
    assert c.ctx_idx == [(0, 0), (2, 2)]
    # The first chunk needs no @@.
    assert ops[3].chunks[0].old == ["import foo"]
    assert ops[3].chunks[0].new == ["import foo", "bar"]


def test_parse_lenient_heredoc_and_crlf_and_whitespace():
    body = _patch("*** Update File: a.py", "@@", "-x", "+y")
    ops = parse_patch("<<'EOF'\n" + body + "EOF\n").ops
    assert ops[0].chunks[0].old == ["x"]
    crlf = body.replace("\n", "\r\n")
    assert parse_patch(crlf).ops[0].chunks[0].new == ["y"]
    # Markers may carry surrounding whitespace.
    assert parse_patch("  *** Begin Patch \n*** Add File: f\n+hi\n *** End Patch  "
                       ).ops[0].contents == "hi\n"
    assert parse_patch(_patch()).ops == [] and parse_patch(_patch()).env_id is None


@pytest.mark.parametrize("text, msg", [
    ("bad", "first line of the patch must be"),
    ("*** Begin Patch\nbad", "last line of the patch must be"),
    (_patch("*** Update File: t.py"), "Update file hunk for path 't.py' is empty"),
    (_patch("*** Update File: t.py", "@@", "@@ x", "-a"), "Unexpected line found"),
    (_patch("*** Update File: t.py", "@@", "-a", "junk"), "Expected update hunk to start"),
    (_patch("*** Update File: t.py", "@@"), "does not contain any lines"),
    (_patch("*** Bogus: x"), "is not a valid hunk header"),
    (_patch("*** Add File: f", "no plus"), "is not a valid hunk header"),
    (_patch("*** Update File: t.py", "-a", "*** End of File", "-b"),
     "Expected update hunk to start with a @@"),
    ("*** Begin Patch\n*** End Patch\ntrailing", "last line of the patch must be"),
])
def test_parse_errors(text, msg):
    with pytest.raises(PatchParseError, match=re.escape(msg)):
        parse_patch(text)


# --------------------------------------------------------------------------- #
# matching semantics
# --------------------------------------------------------------------------- #
def test_seek_sequence_passes():
    lines = ["foo   ", "    bar\t", "baz \u2013 qux"]
    assert seek_sequence(lines, ["foo   "], 0, False) == (0, "exact")
    assert seek_sequence(lines, ["foo"], 0, False) == (0, "rstrip")
    assert seek_sequence(lines, ["bar"], 0, False) == (1, "trim")
    assert seek_sequence(lines, ["baz - qux"], 0, False) == (2, "unicode")
    assert seek_sequence(lines, ["foo"], 1, False) is None
    assert seek_sequence(["a"], ["a", "b"], 0, False) is None
    assert seek_sequence(lines, [], 2, False) == (2, "exact")
    # End of file: the search starts at len(lines) - len(pattern).
    assert seek_sequence(["x", "y", "x"], ["x"], 0, True) == (2, "exact")
    assert seek_sequence(["x", "y", "z"], ["x"], 0, True) is None


def test_whitespace_rules_follow_codex(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    x = 1  \n    return x\n")
    # Trailing and leading whitespace differences match.
    ok = check_patch(tmp_path, _patch("*** Update File: a.py", "@@",
                                      "-  x = 1", "+  x = 2"))
    assert ok["ok"] and ok["files"][0]["hunks"][0]["match"] == "trim"
    # Whitespace inside a line does not.
    bad = check_patch(tmp_path, _patch("*** Update File: a.py", "@@",
                                       "-    x  =  1", "+    x = 2"))
    assert not bad["ok"] and bad["misses"] == 1
    reg = bad["files"][0]["hunks"][0]["region"]
    assert (reg["start_line"], reg["end_line"]) == (2, 2)


def test_anchor_scopes_the_search_and_hunks_apply_in_order(tmp_path):
    (tmp_path / "a.py").write_text(
        "def one():\n    return 1\n\n\ndef two():\n    return 1\n")
    got = check_patch(tmp_path, _patch("*** Update File: a.py",
                                       "@@ def two():", "-    return 1", "+    return 2"))
    h = got["files"][0]["hunks"][0]
    assert got["ok"] and h["line"] == 6 and h["anchor_line"] == 5
    # The anchor is searched forward from the previous chunk.
    got = check_patch(tmp_path, _patch(
        "*** Update File: a.py",
        "@@ def two():", "-    return 1", "+    return 2",
        "@@ def one():", "-    return 1", "+    return 3"))
    assert got["misses"] == 1
    second = got["files"][0]["hunks"][1]
    assert second["reason"] == "context" and "file order" in second["note"]
    # Old lines before the anchor are not found either.
    got = check_patch(tmp_path, _patch("*** Update File: a.py",
                                       "@@ def two():", "-def one():", "+def uno():"))
    h = got["files"][0]["hunks"][0]
    assert h["reason"] == "lines" and "L1" in h["note"]
    assert h["region"]["start_line"] == 1


def test_end_of_file_marker_and_trailing_empty_line(tmp_path):
    (tmp_path / "t.txt").write_text("a\nb\na\n")
    ok = check_patch(tmp_path, _patch("*** Update File: t.txt", "@@", "-a", "+z",
                                      "*** End of File"))
    assert ok["ok"] and ok["files"][0]["hunks"][0]["line"] == 3
    bad = check_patch(tmp_path, _patch("*** Update File: t.txt", "@@", "-b", "+z",
                                       "*** End of File"))
    assert bad["misses"] == 1
    assert "do not end the file" in bad["files"][0]["hunks"][0]["note"]
    # A trailing empty context line (the final newline) is retried without it.
    ok = check_patch(tmp_path, _patch("*** Update File: t.txt", "@@", " a", "-",
                                      "+"))
    assert ok["ok"]


def test_pure_addition_appends_and_sections_see_earlier_ones(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    got = check_patch(tmp_path, _patch(
        "*** Add File: new.txt", "+first",
        "*** Update File: new.txt", "@@", "-first", "+second",
        "*** Update File: a.txt", "*** Move to: b.txt", "@@", "+two",
        "*** Update File: b.txt", "@@", "-two", "+deux",
        "*** Delete File: new.txt",
        "*** Update File: new.txt", "@@", "-second", "+x"))
    statuses = [(f["path"], f["status"]) for f in got["files"]]
    assert statuses == [("new.txt", "ok"), ("new.txt", "ok"), ("a.txt", "ok"),
                        ("b.txt", "ok"), ("new.txt", "ok"), ("new.txt", "missing")]
    # A missing file is reported but is not a miss: Codex says so itself.
    assert got["ok"] and got["misses"] == 0
    assert not (tmp_path / "new.txt").exists() and not (tmp_path / "b.txt").exists()


def test_crlf_files_match_and_keep_their_endings(tmp_path):
    (tmp_path / "w.txt").write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    got = check_patch(tmp_path, _patch("*** Update File: w.txt", "@@", " alpha",
                                       "-beta", "+BETA", "*** Update File: w.txt",
                                       "@@", "-BETA", "+b2"))
    assert got["ok"], got
    src = split_source("alpha\r\nbeta\r\ngamma")
    assert src == [("alpha", "\r\n"), ("beta", "\r\n"), ("gamma", None)]
    _res, repl, _ = P._match_chunk([t for t, _ in src], parse_patch(_patch(
        "*** Update File: w", "@@", " alpha", "-beta", "+BETA")).ops[0].chunks[0], 0)
    assert P._apply(src, repl) == "alpha\r\nBETA\r\ngamma\r\n"
    # A miss in a CRLF file shows its lines without the CR.
    bad = check_patch(tmp_path, _patch("*** Update File: w.txt", "@@", "-betta",
                                       "+x"))
    reg = bad["files"][0]["hunks"][0]["region"]
    assert reg["text"] == ["2: beta"]


def test_paths_must_stay_inside_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.py").write_text("x = 1\n")
    (root / "link.py").symlink_to(tmp_path / "outside.py")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    upd = ["@@", "-x = 2", "+x = 3"]
    for path in ("../outside.py", str(tmp_path / "outside.py"), "link.py",
                 ".git/config"):
        got = check_patch(root, _patch(f"*** Update File: {path}", *upd))
        assert got["files"][0]["status"] == "refused", path
        assert got["ok"] and got["misses"] == 0 and got["reply"] == ""
    got = check_patch(root, _patch("*** Add File: ../evil.txt", "+x"))
    assert got["files"][0]["status"] == "refused"
    assert not (tmp_path / "evil.txt").exists()
    # Relative paths are taken from cwd.
    (root / "sub").mkdir()
    (root / "sub" / "m.py").write_text("x = 1\n")
    got = check_patch(root, _patch("*** Update File: m.py", "@@", "-x = 1", "+x = 2"),
                      cwd=root / "sub")
    assert got["ok"] and got["files"][0]["rel"] == "sub/m.py"


def test_unparsable_patch_is_reported_not_raised(tmp_path):
    got = check_patch(tmp_path, "not a patch")
    assert got["parsed"] is False and got["ok"] is False and got["error"]


# --------------------------------------------------------------------------- #
# nearest region quality
# --------------------------------------------------------------------------- #
PY = '''"""Module."""
import os


def helper(a, b=1):
    """Help."""
    total = a + b
    if total > 10:
        return total - 10
    return total


class Box:
    def __init__(self):
        self.n = 0

    def grow(self, k):
        self.n += k
        return self.n
'''

MD = """# Project

Intro text.

## Install

Run the installer with the default options and then
restart the shell so the new path is picked up.

## Usage

Call the tool with a file name.
"""

TEX = r"""\documentclass{article}
\begin{document}
\section{Introduction}
We study the convergence of iterative solvers for sparse linear systems arising from finite element discretisations of elliptic problems, and show that the preconditioned conjugate gradient method converges in a number of steps independent of the mesh size.

\section{Method}
The method combines an algebraic multigrid preconditioner with a Krylov accelerator; each cycle uses two smoothing steps of a damped Jacobi iteration on every level of the hierarchy.
\end{document}
"""


def _region(tmp_path, name, text, body):
    (tmp_path / name).write_text(text)
    got = check_patch(tmp_path, _patch(f"*** Update File: {name}", *body))
    assert got["misses"] == 1, got
    return got, got["files"][0]["hunks"][0]["region"]


def test_region_python_function(tmp_path):
    got, reg = _region(tmp_path, "m.py", PY, [
        "@@ def helper(a, b=1):",
        '     """Help."""',
        "-    total = a+b",
        "-    if total > 9:",
        "+    total = a * b",
        "+    if total > 9:",
        "         return total - 10"])
    assert (reg["start_line"], reg["end_line"]) == (6, 9)
    assert reg["key"] == "sym:m.py::helper"
    assert reg["ratio"] > 0.8
    assert "-    if total > 9:" in reg["diff"] and "+    if total > 10:" in reg["diff"]
    assert reg["text"][0] == '6:     """Help."""'
    reply = got["reply"]
    assert _sha(tmp_path / "m.py") in reply
    assert "nearest region: L6-L9 sym:m.py::helper" in reply
    assert "8:     if total > 10:" in reply
    # The node's full range and sha256, and that the region is part of it.
    node_sha = _node_sha(tmp_path / "m.py", 5, 10)
    assert reg["node"] == {"start_line": 5, "end_line": 10, "sha256": node_sha}
    assert (f"  node L5-L10 node sha256 {node_sha}; the region is part of it, "
            f"a key edit replaces all of it") in reply.split("\n")
    # That sha is the base a key edit of the node takes; the file sha is not.
    from ozagia.edit.key_edit import apply_key_edit
    key = reg["key"]
    assert apply_key_edit(tmp_path, key, NEW_HELPER,
                          base_sha256=_sha(tmp_path / "m.py"))["stale"] is True
    assert apply_key_edit(tmp_path, key, NEW_HELPER, base_sha256=node_sha)["ok"]


def test_region_markdown_section(tmp_path):
    _, reg = _region(tmp_path, "README.md", MD, [
        "@@",
        " Run the installer with the default settings and then",
        "-restart the terminal so the new path is picked up.",
        "+log out and in again."])
    assert (reg["start_line"], reg["end_line"]) == (7, 8)
    assert reg["key"] == "doc:README.md#install"


def test_region_tex_paragraph(tmp_path):
    _, reg = _region(tmp_path, "paper.tex", TEX, [
        "@@ \\section{Method}",
        "-The method combines a geometric multigrid preconditioner with a Krylov "
        "accelerator; each cycle uses three smoothing steps of a damped Jacobi "
        "iteration on every level of the hierarchy.",
        "+The method is new."])
    assert (reg["start_line"], reg["end_line"]) == (7, 7)
    assert reg["key"] == "text:paper.tex#L6-L8"
    assert reg["ratio"] > 0.9


def test_no_region_when_nothing_resembles(tmp_path):
    _, reg = _region(tmp_path, "m.py", PY, ["@@", "-zzzzqqqq wholly unrelated text",
                                            "+x"])
    assert reg is None
    got = check_patch(tmp_path, _patch("*** Update File: m.py", "@@",
                                       "-zzzzqqqq wholly unrelated text", "+x"))
    assert "no similar region" in got["reply"]


def test_nearest_region_prefers_after_the_cursor():
    lines = ["x = compute(1)", "pad", "pad2", "x = compute(1)"]
    assert nearest_region(lines, ["x = compute(2)"], after=2)["start"] == 3
    assert nearest_region(lines, ["x = compute(2)"])["start"] == 0


def test_caps(tmp_path):
    big = "".join(f"line number {i} with some text\n" for i in range(1, 301))
    (tmp_path / "big.txt").write_text(big)
    # 100 expected lines, all slightly wrong: the region shows 60 of them.
    body = ["@@"] + [f"-line number {i} with some txt" for i in range(1, 101)] + ["+x"]
    got = check_patch(tmp_path, _patch("*** Update File: big.txt", *body))
    reg = got["files"][0]["hunks"][0]["region"]
    assert len(reg["text"]) == P.MAX_REGION_LINES and reg["more_lines"] == 40
    assert len(reg["diff"]) == P.MAX_DIFF_LINES + 1
    # Six failing hunks: four regions.
    body = []
    for i in range(6):
        body += ["@@", f"-line number {i * 40 + 5} with some txt", "+x"]
    got = check_patch(tmp_path, _patch("*** Update File: big.txt", *body))
    regs = [h.get("region") for h in got["files"][0]["hunks"]]
    assert got["misses"] == 6 and got["regions"] == 4
    assert regs[4] is None and regs[5] is None
    assert "region cap" in got["reply"]
    assert len(got["reply"]) <= P.MAX_REPLY_CHARS
    # The total size cap.
    small = check_patch(tmp_path, _patch("*** Update File: big.txt", *body),
                        max_chars=900)
    assert len(small["reply"]) <= 900 and "cap" in small["reply"]
    # Long lines are cut.
    (tmp_path / "long.txt").write_text("word " * 1000 + "\n")
    got = check_patch(tmp_path, _patch("*** Update File: long.txt", "@@",
                                       "-" + "word " * 999 + "wrd", "+x"))
    text = got["files"][0]["hunks"][0]["region"]["text"][0]
    assert "...[+" in text and len(text) < P.MAX_LINE_CHARS + 40


# --------------------------------------------------------------------------- #
# Codex PreToolUse on apply_patch
# --------------------------------------------------------------------------- #
class StubDaemon:
    def __init__(self, sock: Path, reply):
        self.requests: list[dict] = []
        stub = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    req = json.loads(line)
                    stub.requests.append(req)
                    resp = {"id": req.get("id"), **reply(req)}
                    self.wfile.write((json.dumps(resp) + "\n").encode())
                    self.wfile.flush()

        self.server = socketserver.ThreadingUnixStreamServer(str(sock), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sockdir():
    d = Path(tempfile.mkdtemp(prefix="ozw6", dir="/tmp"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def cxrepo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text(PY, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _env(sock: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OZAGIA_") and k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(SRC)
    env[protocol.ENV_SOCK] = str(sock)
    env["OZAGIA_DAEMON_AUTOSTART"] = "0"
    return env


def _pre(repo: Path, patch_text: str, sock: Path):
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "PreToolUse",
               "tool_name": "apply_patch", "tool_use_id": "c1",
               "tool_input": {"command": patch_text}}
    return subprocess.run([sys.executable, "-m", "ozagia.integrations.codex", "hook"],
                          input=json.dumps(payload), capture_output=True, text=True,
                          env=_env(sock), timeout=60)


STALE = _patch("*** Update File: src/a.py", "@@ def helper(a, b=1):",
               "-    total = a+b", "+    total = a * b")
FRESH = _patch("*** Update File: src/a.py", "@@ def helper(a, b=1):",
               "-    total = a + b", "+    total = a * b")


def test_pre_apply_patch_denies_a_miss_with_the_region(cxrepo, sockdir):
    proc = _pre(cxrepo, STALE, sockdir / "missing.sock")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    spec = out["hookSpecificOutput"]
    assert set(spec) == {"hookEventName", "permissionDecision",
                         "permissionDecisionReason"}
    assert spec["permissionDecision"] == "deny"
    reason = spec["permissionDecisionReason"]
    assert "7:     total = a + b" in reason
    assert "sym:src/a.py::helper" in reason
    assert _sha(cxrepo / "src" / "a.py") in reason
    assert len(reason) <= hook.PATCH_REPLY_MAX_CHARS
    assert (cxrepo / "src" / "a.py").read_text() == PY


@pytest.mark.parametrize("text", [
    FRESH,
    "not a patch at all",
    _patch("*** Update File: ../../etc/passwd", "@@", "-root", "+x"),
    _patch("*** Update File: src/missing.py", "@@", "-x", "+y"),
    # For another environment's files: not checked against these.
    STALE.replace("*** Begin Patch\n", "*** Begin Patch\n*** Environment ID: remote\n"),
])
def test_pre_apply_patch_allows_silently(cxrepo, sockdir, text):
    proc = _pre(cxrepo, text, sockdir / "missing.sock")
    assert proc.returncode == 0 and proc.stdout == "", proc.stderr


def test_pre_apply_patch_fails_open_on_checker_errors(cxrepo, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("checker broke")
    monkeypatch.setattr(hook, "check_patch", boom)
    monkeypatch.setenv(protocol.ENV_SOCK, "/nonexistent/ozagia.sock")
    payload = {"hook_event_name": "PreToolUse", "tool_name": "apply_patch",
               "cwd": str(cxrepo), "tool_input": {"command": STALE}}
    assert hook.handle(payload, cxrepo) is None
    log = (cxrepo / ".ozagia" / hook.LOG_NAME).read_text()
    assert "checker broke" in log


def test_pre_apply_patch_uses_the_daemon_reply(cxrepo, sockdir):
    def reply(req):
        if req["op"] == "tool" and req["tool"] == "patch_check":
            return {"ok": True, "handled": True, "is_error": False,
                    "output": "GRAPH REGION REPLY",
                    "meta": {"intent": "patch_check_miss"}}
        return {"ok": True}
    stub = StubDaemon(sockdir / "d.sock", reply)
    try:
        proc = _pre(cxrepo, STALE, sockdir / "d.sock")
        reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        assert reason == "GRAPH REGION REPLY"
        req = stub.requests[0]
        assert req["tool"] == "patch_check" and req["harness"] == "codex"
        assert req["input"]["patch"] == STALE and req["input"]["workdir"] == str(cxrepo)
        assert isinstance(req["deadline_ms"], int)
        # A matching patch does not wait for the daemon: it is not asked.
        proc = _pre(cxrepo, FRESH, sockdir / "d.sock")
        assert proc.stdout == "" and len(stub.requests) == 1
    finally:
        stub.close()


def test_pre_apply_patch_daemon_error_falls_back_to_local(cxrepo, sockdir):
    stub = StubDaemon(sockdir / "d.sock", lambda req: {"ok": False, "error": "x"})
    try:
        proc = _pre(cxrepo, STALE, sockdir / "d.sock")
        reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        assert "7:     total = a + b" in reason
    finally:
        stub.close()


def test_installer_matches_apply_patch_before_and_after(tmp_path):
    inst.install(tmp_path, python="/opt/py/bin/python")
    data = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    pre = [g["matcher"] for g in data["hooks"]["PreToolUse"]]
    assert pre == ["^(Bash|apply_patch)$"]
    assert re.search(pre[0], "apply_patch") and re.search(pre[0], "Bash")
    assert not re.search(pre[0], "Edit")


# --------------------------------------------------------------------------- #
# graph fixtures: intent, daemon, MCP
# --------------------------------------------------------------------------- #
USE = '''"""Uses mod."""
from pkg.mod import helper


def run():
    return helper(1, 2)
'''

README = """# Project

Intro.

## Install

Run make.

## Usage

Run it.
"""

CONF = '{"name": "app", "deps": ["a", "b"], "opts": {"level": 1}}\n'
YAML = "name: app\n\nsteps:\n  - build\n  - test\n"


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


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
    _write(root / "pkg" / "mod.py", PY)
    _write(root / "pkg" / "use.py", USE)
    _write(root / "README.md", README)
    _write(root / "conf.json", CONF)
    _write(root / "deploy.yaml", YAML)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


@pytest.fixture
def mem(repo: Path, tmp_path: Path) -> DevMemory:
    dump = tmp_path / "state" / "graph.json"
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=repo)


def test_patch_check_intent_and_daemon_counters(mem, repo, tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    stale = STALE.replace("src/a.py", "pkg/mod.py")
    fresh = FRESH.replace("src/a.py", "pkg/mod.py")
    for text in (stale, fresh, "garbage"):
        msg = {"op": "tool", "tool": "patch_check", "harness": "codex",
               "input": {"patch": text, "workdir": str(repo)}}
        resp = st.dispatch(msg)
        st.record(msg, resp, 1.0)
    miss = I.handle(mem, "patch_check", {"patch": stale})
    assert miss["handled"] and miss["meta"]["intent"] == "patch_check_miss"
    assert "sym:pkg/mod.py::helper" in miss["output"]
    assert miss["meta"]["region_replies"] == 1 and miss["meta"]["patch_misses"] == 1
    assert miss["meta"]["keys"] == ["sym:pkg/mod.py::helper"]
    assert (repo / "pkg" / "mod.py").read_text() == PY
    stats = st.stats()
    assert stats["patch_checks"] == 2 and stats["patch_misses_caught"] == 1
    assert stats["region_replies"] == 1 and stats["key_edits"] == 0
    assert stats["by_intent"]["patch_check_unparsed"] == 1


def test_after_edit_miss_appends_the_nearest_region(mem, repo):
    inp = {"path": "pkg/mod.py", "oldString": "    total = a+b\n    if total > 9:",
           "newString": "    total = a * b"}
    got = I.after(mem, "edit", inp, "Error: oldString not found in content")
    assert got["meta"]["intent"] == "after_edit_miss"
    path = repo / "pkg" / "mod.py"
    assert got["meta"]["region_replies"] == 1
    lines = got["append"].split("\n")
    assert lines[0].startswith("[ozagia] oldString is not in pkg/mod.py; nearest "
                               "region L7-L8 sym:pkg/mod.py::helper")
    assert _sha(path) in lines[0]
    # The node's whole range and its sha256 (the key-edit base).
    assert lines[1] == (f"[ozagia] node L5-L10 node sha256 {_node_sha(path, 5, 10)}; "
                        f"the region is part of it, a key edit replaces all of it")
    assert "7:     total = a + b" in lines and "8:     if total > 10:" in lines
    assert "[ozagia] +    if total > 10:" in lines
    # A successful edit, or one whose old text is in the file, gets nothing.
    assert "region" not in I.after(mem, "edit", inp, "Edit applied successfully.")["append"]
    ok = {**inp, "oldString": "    total = a + b"}
    assert I.after(mem, "edit", ok, "String to replace not found")["append"] == ""
    # MultiEdit-style input (Claude Code).
    multi = {"path": "pkg/mod.py", "edits": [{"oldString": "return total - 11",
                                              "newString": "return total - 12"}]}
    got = I.after(mem, "edit", multi, "String to replace not found in file.")
    assert "nearest region L9-L9" in got["append"]


def test_intent_edit_still_hands_inexact_matches_to_the_builtin(mem):
    res = I.handle(mem, "edit", {"path": "pkg/mod.py", "oldString": "total = a+b",
                                 "newString": "x"})
    assert res["handled"] is False and "no exact match" in res["reason"]


# --------------------------------------------------------------------------- #
# key-addressed edits
# --------------------------------------------------------------------------- #
NEW_HELPER = 'def helper(a, b=1):\n    """Help."""\n    return a * b\n'


def test_key_edit_takes_the_node_sha_and_the_file_sha_only_for_whole_files(mem, repo):
    path = repo / "pkg" / "mod.py"
    # The file's sha256 does not prove the caller saw all of a part-of-file
    # node (a region reply may have shown a few of its lines): refused.
    res = I.handle(mem, "edit", {"key": "sym:pkg/mod.py::helper",
                                 "newString": NEW_HELPER,
                                 "base_sha256": _sha(path)})
    assert res["is_error"] and res["meta"]["intent"] == "edit_key_stale"
    assert "is the file's sha256; a key edit of part of a file takes the node's" \
        in res["output"]
    assert path.read_text() == PY
    res = I.handle(mem, "edit", {"key": "sym:pkg/mod.py::helper",
                                 "newString": NEW_HELPER,
                                 "base_sha256": _node_sha(path, 5, 10)})
    assert res["handled"] and not res["is_error"], res
    assert res["meta"]["intent"] == "edit_key"
    first = res["output"].split("\n")[0]
    assert first == f"Edited {path} (node sym:pkg/mod.py::helper, lines 5-10)"
    assert f"sha256 {_sha(path)}" in res["output"]
    # The new node sha is the base for the next edit of the node.
    new_sha = hashlib.sha256(NEW_HELPER.encode()).hexdigest()
    assert f"node sha256 {new_sha} (base_sha256 for the next key edit" in res["output"]
    text = path.read_text()
    assert "return a * b" in text and "total" not in text
    assert "class Box:" in text and text.count("\n\n\nclass Box") == 1
    again = mem.key_edit("sym:pkg/mod.py::helper", NEW_HELPER.replace("*", "-"),
                         base_sha256=new_sha)
    assert again["ok"], again
    # The node's own hash also works after an unrelated edit elsewhere.
    path.write_text(text.replace('"""Module."""', '"""Module!"""'))
    old = "    def grow(self, k):\n        self.n += k\n        return self.n\n"
    node_sha = hashlib.sha256(old.encode()).hexdigest()
    res = I.handle(mem, "edit", {"key": "sym:pkg/mod.py::Box.grow",
                                 "newString": "    def grow(self, k):\n        return k",
                                 "base_sha256": node_sha})
    assert not res["is_error"], res["output"]
    assert path.read_text().endswith("    def grow(self, k):\n        return k\n")
    # A whole-file node takes the file's sha256 too.
    y = repo / "deploy.yaml"
    done = mem.key_edit("file:deploy.yaml", "name: b\n", base_sha256=_sha(y),
                        apply=True)
    assert done["applied"] and y.read_text() == "name: b\n"


def test_key_edit_writes_a_backup(mem, repo):
    path = repo / "pkg" / "mod.py"
    done = mem.key_edit("sym:pkg/mod.py::helper", NEW_HELPER,
                        base_sha256=_node_sha(path, 5, 10), apply=True)
    assert done["applied"] and done["backup"]
    backup = Path(done["backup"])
    assert backup.is_file() and backup.read_text() == PY
    assert mem.journal_recent()[-1]["backup"] == done["backup"]


def test_key_edit_stale_or_missing_base_is_refused_with_the_node(mem, repo):
    path = repo / "pkg" / "mod.py"
    for base in ("0" * 64, None):
        inp = {"key": "sym:pkg/mod.py::helper", "newString": NEW_HELPER}
        if base:
            inp["base_sha256"] = base
        res = I.handle(mem, "edit", inp)
        assert res["handled"] and res["is_error"]
        assert res["meta"]["intent"] == "edit_key_stale"
        out = res["output"].split("\n")
        assert out[0].startswith("Edit refused: ")
        assert f"file sha256 {_sha(path)}" in out[1]
        assert "7:     total = a + b" in out
        assert path.read_text() == PY
    # The node sha from the refusal works.
    node_sha = re.search(r"node sha256 ([0-9a-f]{64})", res["output"]).group(1)
    res = I.handle(mem, "edit", {"key": "sym:pkg/mod.py::helper",
                                 "newString": NEW_HELPER, "base_sha256": node_sha})
    assert not res["is_error"], res["output"]


def test_key_edit_parse_guard_and_preflight(mem, repo):
    path = repo / "pkg" / "mod.py"
    base = _node_sha(path, 5, 10)
    res = I.handle(mem, "edit", {"key": "sym:pkg/mod.py::helper",
                                 "newString": "def helper(:\n", "base_sha256": base})
    assert res["is_error"] and "does not parse" in res["output"]
    pre = I.handle(mem, "edit", {"key": "sym:pkg/mod.py::helper",
                                 "newString": NEW_HELPER, "base_sha256": base},
                   preflight=True)
    assert pre["preflight"] and pre["permission"]["resources"] == ["pkg/mod.py"]
    assert "return a * b" in pre["permission"]["metadata"]["files"][0]["patch"]
    assert path.read_text() == PY


def test_key_edit_markdown_text_and_json(mem, repo):
    md = repo / "README.md"
    md_base = _node_sha(md, 5, 8)
    prev = mem.key_edit("doc:README.md#install", "## Install\n\nRun make install.\n",
                        base_sha256=md_base)
    assert prev["ok"] and prev["applied"] is False
    assert (prev["start_line"], prev["end_line"]) == (5, 8)
    # The section runs to the next heading, blank lines included.
    done = mem.key_edit("doc:README.md#install", "## Install\n\nRun make install.\n\n",
                        base_sha256=md_base, apply=True)
    assert done["applied"]
    assert md.read_text() == README.replace("Run make.", "Run make install.")
    assert mem.journal_recent()[-1]["kind"] == "key_edit"
    y = repo / "deploy.yaml"
    done = mem.key_edit("text:deploy.yaml#L3-L5", "steps:\n  - lint",
                        base_sha256=_node_sha(y, 3, 5), apply=True)
    assert done["applied"] and y.read_text() == "name: app\n\nsteps:\n  - lint\n"
    j = repo / "conf.json"
    b_sha = hashlib.sha256(b'"b"').hexdigest()
    done = mem.key_edit("json:conf.json#/deps/1", '"c"', base_sha256=b_sha, apply=True)
    assert done["applied"] and json.loads(j.read_text())["deps"] == ["a", "c"]
    opts_sha = hashlib.sha256(b'{"level": 1}').hexdigest()
    bad = mem.key_edit("json:conf.json#/opts", "{", base_sha256=opts_sha)
    assert bad["ok"] is False and bad["parse_check"] == "refused"
    from ozagia.edit.code_edit import CodeEditError
    with pytest.raises(CodeEditError, match="no section"):
        mem.key_edit("doc:README.md#nope", "x", base_sha256=_sha(md))


def test_section_keys_and_ambiguous_slugs():
    from ozagia.edit.key_edit import find_section, section_key, slug
    text = "# A\n\n## Setup\n\nx\n\n# B\n\n## Setup\n\ny\n"
    assert slug("Hello, World!") == "hello-world"
    assert section_key("r.md", text, text.index("y"), text.index("y")) == "doc:r.md#b/setup"
    assert find_section(text, "b/setup").start_line == 9
    from ozagia.edit.code_edit import CodeEditError
    with pytest.raises(CodeEditError, match="ambiguous"):
        find_section(text, "setup")


def test_mcp_dev_key_edit_schema_and_calls(mem, repo):
    from ozagia.integrations.mcp.server import DevMemoryServer
    server = DevMemoryServer(mem)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                            "params": {}})
    tools = {t["name"]: t for t in listed["result"]["tools"]}
    schema = tools["dev_key_edit"]["inputSchema"]
    assert set(schema["properties"]) == {"key", "new_text", "base_sha256", "apply",
                                         "task"}
    assert schema["required"] == ["key", "new_text"]
    assert schema["additionalProperties"] is False

    def call(args):
        resp = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "dev_key_edit", "arguments": args}})
        assert resp["result"]["isError"] is False, resp
        return json.loads(resp["result"]["content"][0]["text"])

    path = repo / "pkg" / "mod.py"
    stale = call({"key": "sym:pkg/mod.py::helper", "new_text": NEW_HELPER,
                  "base_sha256": "f" * 64})
    assert stale["ok"] is False and stale["stale"] is True
    assert stale["file_sha256"] == _sha(path) and "total = a + b" in stale["node_text"]
    prev = call({"key": "sym:pkg/mod.py::helper", "new_text": NEW_HELPER,
                 "base_sha256": stale["node_sha256"]})
    assert prev["ok"] and prev["applied"] is False and path.read_text() == PY
    done = call({"key": "sym:pkg/mod.py::helper", "new_text": NEW_HELPER,
                 "base_sha256": stale["node_sha256"], "apply": True})
    assert done["applied"] and "graph_delta" in done
    assert done["new_sha256"] == _sha(path)


def test_daemon_counts_key_edits(mem, repo, tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    msg = {"op": "tool", "tool": "edit", "input": {
        "key": "sym:pkg/mod.py::helper", "newString": NEW_HELPER,
        "base_sha256": _node_sha(repo / "pkg" / "mod.py", 5, 10)}}
    resp = st.dispatch(msg)
    st.record(msg, resp, 1.0)
    assert resp["handled"] and not resp["is_error"], resp
    assert st.stats()["key_edits"] == 1


# --------------------------------------------------------------------------- #
# review fixes
# --------------------------------------------------------------------------- #
def test_environment_id_patches_are_not_checked(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    body = ["*** Update File: a.py", "@@", "-x = 2", "+x = 3"]
    local = check_patch(tmp_path, _patch(*body))
    assert local["misses"] == 1
    for env in ("remote", "local"):
        got = check_patch(tmp_path, _patch(f"*** Environment ID: {env}", *body))
        assert got["parsed"] and got["ok"] and got["misses"] == 0
        assert got["skipped"] == "environment" and got["env_id"] == env
        assert got["reply"] == ""
    assert parse_patch(_patch("*** Environment ID: remote", *body)).env_id == "remote"


def test_region_covering_the_whole_node_has_no_part_note(tmp_path):
    text = ("def f():\n    value = compute(1)\n    return value\n\n\n"
            "def g():\n    return 2\n")
    got, reg = _region(tmp_path, "w.py", text, [
        "@@", "-def f():", "-    value = compute(2)", "-    return value",
        "+def f():", "+    return 3"])
    assert (reg["start_line"], reg["end_line"]) == (1, 3)
    assert reg["node"] == {"start_line": 1, "end_line": 3,
                           "sha256": _node_sha(tmp_path / "w.py", 1, 3)}
    assert f"  node L1-L3 node sha256 {reg['node']['sha256']}" in got["reply"].split("\n")


def test_region_node_for_a_file_changed_earlier_in_the_patch_is_omitted(tmp_path):
    (tmp_path / "a.py").write_text(PY)
    got = check_patch(tmp_path, _patch(
        "*** Update File: a.py", "@@", '-"""Module."""', '+"""Mod."""',
        "*** Update File: a.py", "@@", "-    total = a+b", "+    total = 0"))
    hunk = got["files"][1]["hunks"][0]
    assert hunk["status"] == "failed" and hunk["region"]["node"] is None
    assert "node sha256" not in got["reply"]


def test_after_edit_miss_not_after_a_successful_edit(mem, repo):
    path = repo / "pkg" / "mod.py"
    old, new = "        return total - 10\n", "        return total - 11\n"
    path.write_text(PY.replace(old, new))
    inp = {"path": "pkg/mod.py", "oldString": old, "newString": new}
    # Diagnostics in a successful edit's output match the failure words.
    out = ("Edit applied successfully.\n\nLSP errors detected in this file:\n"
           "ERROR [1:8] Stub file not found for \"os\"\n"
           "ERROR [3:1] could not find `x` in `y`")
    got = I.after(mem, "edit", inp, out, harness="claude")
    assert "oldString is not" not in got["append"]
    assert "region_replies" not in got["meta"]
    # The plugin's flag decides when it is there.
    got = I.after(mem, "edit", inp, out, meta={"builtin_error": False})
    assert "oldString is not" not in got["append"]
    failed = I.after(mem, "edit", {**inp, "oldString": "        return total - 9\n"},
                     "Error: something else entirely", meta={"builtin_error": True})
    assert failed["meta"]["intent"] == "after_edit_miss"
    assert "nearest region L9-L9" in failed["append"]


def test_daemon_passes_after_meta_to_the_edit_miss_gate(mem, repo, tmp_path):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    inp = {"path": "pkg/mod.py", "oldString": "        return total - 9\n",
           "newString": "        return total - 8\n"}
    base = {"op": "after", "tool": "edit", "input": inp, "output": "boom"}
    assert "oldString is not" not in st.dispatch(base)["append"]
    got = st.dispatch({**base, "meta": {"builtin_error": True}})
    assert "nearest region L9-L9" in got["append"]


PROP = """class C:
    @property
    def x(self):
        return self._x

    @x.setter
    def x(self, v):
        self._x = v
"""


def test_key_edit_refuses_an_ambiguous_qualname(tmp_path):
    from ozagia.edit.code_edit import CodeEditError
    from ozagia.edit.key_edit import apply_key_edit
    (tmp_path / "a.py").write_text(PROP)
    with pytest.raises(CodeEditError) as exc:
        apply_key_edit(tmp_path, "sym:a.py::C.x", "    def x(self):\n        return 1\n",
                       base_sha256="0" * 64)
    msg = str(exc.value)
    assert "sym:a.py::C.x is ambiguous: 2 defs" in msg
    assert "(L2-L4, L6-L8)" in msg and "text:a.py#L<a>-L<b>" in msg
    # The setter by its line range.
    setter = "".join(PROP.splitlines(keepends=True)[5:8])
    res = apply_key_edit(tmp_path, "text:a.py#L6-L8",
                         "    @x.setter\n    def x(self, v):\n        self._x = v * 2\n",
                         base_sha256=hashlib.sha256(setter.encode()).hexdigest(),
                         write=True)
    assert res["applied"] and "self._x = v * 2" in (tmp_path / "a.py").read_text()
    assert "return self._x" in (tmp_path / "a.py").read_text()


class _GraphMemory:
    """A memory whose graph has one non-Python symbol, stale until a
    reingest."""

    def __init__(self, rel: str, start: int, end: int, fresh: tuple[int, int]):
        self.rel, self.attrs, self.fresh = rel, {"start_line": start,
                                                 "end_line": end}, fresh
        self.pending = [rel]
        self.reingested: list[list[str]] = []

    def pending_changes(self):
        return list(self.pending)

    def reingest(self, paths):
        self.reingested.append(list(paths))
        self.attrs = {"start_line": self.fresh[0], "end_line": self.fresh[1]}
        self.pending = []

    def node_by_key(self, key):
        return 1 if key == f"sym:{self.rel}::run" else None

    def node(self, nid):
        return {"attrs": dict(self.attrs)}


GO = "package m\n\n// inserted\n// lines\nfunc run() int {\n\treturn 1\n}\n"


def test_key_edit_graph_sourced_span_reingests_a_changed_file(tmp_path):
    from ozagia.edit.key_edit import apply_key_edit, node_span
    from ozagia.edit.span_edit import _read
    (tmp_path / "m.go").write_text(GO)
    # The graph still has `run` at L3-L5 (before two lines were inserted).
    memory = _GraphMemory("m.go", 3, 5, fresh=(5, 7))
    span = node_span(_read(tmp_path, "m.go"), "sym:m.go::run", memory=memory)
    assert memory.reingested == [["m.go"]]
    assert (span["start_line"], span["end_line"], span["graph"]) == (5, 7, True)
    # Without a refresh (region replies) a stale range is refused.
    memory = _GraphMemory("m.go", 3, 5, fresh=(5, 7))
    from ozagia.edit.code_edit import CodeEditError
    with pytest.raises(CodeEditError, match="stale"):
        node_span(_read(tmp_path, "m.go"), "sym:m.go::run", memory=memory,
                  refresh=False)
    # Graph-sourced spans take only the node sha.
    memory = _GraphMemory("m.go", 3, 5, fresh=(5, 7))
    new = "func run() int {\n\treturn 2\n}\n"
    file_sha = _sha(tmp_path / "m.go")
    assert apply_key_edit(tmp_path, "sym:m.go::run", new, base_sha256=file_sha,
                          memory=memory)["stale"] is True
    node_sha = _node_sha(tmp_path / "m.go", 5, 7)
    res = apply_key_edit(tmp_path, "sym:m.go::run", new, base_sha256=node_sha,
                         memory=memory)
    assert res["ok"] and (res["start_line"], res["end_line"]) == (5, 7)
    assert "return 2" in res["diff_view"] and "// lines" not in res["new_node_text"]


def test_lone_cr_lines_count_the_same_in_replies_and_key_edits(tmp_path):
    from types import SimpleNamespace

    from ozagia.edit.code_edit import CodeEditError
    from ozagia.edit.key_edit import apply_key_edit, node_span
    text = "alpha\rbeta\rgamma\rdelta\r"
    (tmp_path / "t.txt").write_bytes(text.encode())
    got = check_patch(tmp_path, _patch("*** Update File: t.txt", "@@", "-gamma!",
                                       "+G"))
    reg = got["files"][0]["hunks"][0]["region"]
    assert (reg["start_line"], reg["end_line"]) == (3, 3)
    # A key's line range addresses the same line the reply numbered.
    span = node_span(SimpleNamespace(rel="t.txt", text=text), "text:t.txt#L3-L3")
    assert text[span["start"]:span["end"]] == "gamma\r"
    assert (span["start_line"], span["end_line"]) == (3, 3)
    # (The edit layer itself refuses lone-CR files.)
    with pytest.raises(CodeEditError, match="lone CR"):
        apply_key_edit(tmp_path, "text:t.txt#L3-L3", "G", base_sha256="0" * 64)


def test_span_edit_parses_the_old_python_text_once(tmp_path, monkeypatch):
    from ozagia.edit import span_edit
    (tmp_path / "a.py").write_text(PY)
    seen: list[str] = []
    real = span_edit._parse_py

    def counting(text):
        seen.append(text)
        return real(text)
    monkeypatch.setattr(span_edit, "_parse_py", counting)
    res = span_edit.apply_span_edit(tmp_path, "a.py", "total = a + b", "total = a - b")
    assert res["ok"], res
    assert seen.count(PY) == 1 and len(seen) == 2
