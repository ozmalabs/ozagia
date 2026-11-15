"""ozagia.index.text_nodes: text chunks, chunk replacement and coverage."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ozagia.config import state_path
from ozagia.edit.code_edit import CodeEditError
from ozagia.index import text_nodes
from ozagia.index.text_nodes import (
    MAX_CHUNK_LINES,
    coverage,
    is_structured,
    locate_chunk,
    replace_chunk,
    text_chunks,
)


def _check_cover(rel: str, text: str) -> list[dict]:
    chunks = text_chunks(rel, text)
    assert "".join(c["text"] for c in chunks) == text
    line = 1
    for c in chunks:
        assert c["start_line"] == line
        assert c["end_line"] >= c["start_line"]
        assert c["end_line"] - c["start_line"] + 1 <= MAX_CHUNK_LINES
        assert c["key"] == f"text:{rel}#L{c['start_line']}-L{c['end_line']}"
        assert len(text_nodes.split_lines(c["text"])) == (
            c["end_line"] - c["start_line"] + 1)
        line = c["end_line"] + 1
    return chunks


def test_round_trip_lf_with_blank_runs() -> None:
    text = "\n\na: 1\nb: 2\n\n\n\nc: 3\n   \nd: 4\ne: 5\n"
    chunks = _check_cover("x.yaml", text)
    assert [(c["start_line"], c["end_line"]) for c in chunks] == [
        (1, 7), (8, 9), (10, 11)]
    # Leading blanks join the first chunk; trailing blank runs join the chunk
    # before them.
    assert chunks[0]["text"] == "\n\na: 1\nb: 2\n\n\n\n"
    assert chunks[1]["text"] == "c: 3\n   \n"


def test_round_trip_crlf_and_no_final_newline() -> None:
    crlf = "a: 1\r\nb: 2\r\n\r\nc: 3\r\n"
    chunks = _check_cover("x.yaml", crlf)
    assert [c["text"] for c in chunks] == ["a: 1\r\nb: 2\r\n\r\n", "c: 3\r\n"]
    tail = "one\n\ntwo"
    chunks = _check_cover("x.txt", tail)
    assert chunks[-1]["text"] == "two" and chunks[-1]["end_line"] == 3
    _check_cover("x.txt", "only")
    _check_cover("x.txt", "\n")
    # A lone CR is not a line break here (lines split at LF only).
    assert len(text_chunks("x.txt", "a\rb\n")) == 1


def test_empty_file_has_no_chunks() -> None:
    assert text_chunks("x.txt", "") == []
    assert locate_chunk([], 1) is None


def test_forced_splits_without_blank_lines() -> None:
    text = "".join(f"line {i}\n" for i in range(1, 201))
    chunks = _check_cover("big.cfg", text)
    assert [(c["start_line"], c["end_line"]) for c in chunks] == [
        (1, 60), (61, 120), (121, 180), (181, 200)]
    # A blank run longer than the limit is also split.
    blanks = "x\n" + "\n" * 130 + "y\n"
    chunks = _check_cover("b.txt", blanks)
    assert all(c["end_line"] - c["start_line"] < MAX_CHUNK_LINES for c in chunks)


def test_locate_chunk() -> None:
    chunks = text_chunks("x.txt", "a\nb\n\nc\n")
    assert locate_chunk(chunks, 1)["key"] == "text:x.txt#L1-L3"
    assert locate_chunk(chunks, 3)["key"] == "text:x.txt#L1-L3"
    assert locate_chunk(chunks, 4)["key"] == "text:x.txt#L4-L4"
    assert locate_chunk(chunks, 5) is None


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "deploy").mkdir(parents=True)
    (r / "deploy" / "app.yaml").write_bytes(
        b"name: app\nport: 80\n\nreplicas: 2\nimage: x\n\nlast: true\n")
    return r


def test_replace_chunk_preview_then_apply(root: Path) -> None:
    path = root / "deploy" / "app.yaml"
    before = path.read_bytes()
    prev = replace_chunk(root, "deploy/app.yaml", "text:deploy/app.yaml#L4-L6",
                         "replicas: 3\nimage: y\nextra: 1\n")
    assert prev["applied"] is False and prev["backup"] is None
    assert path.read_bytes() == before
    assert "+replicas: 3" in prev["diff_view"] and "-replicas: 2" in prev["diff_view"]
    assert prev["before"]["lines"] == 7 and prev["after"]["lines"] == 7
    # The new text has no trailing blank line, so it merges with "last: true".
    assert prev["new_keys"] == ["text:deploy/app.yaml#L4-L7"]
    assert prev["keys_shifted"] is False

    done = replace_chunk(root, "deploy/app.yaml", "text:deploy/app.yaml#L4-L6",
                         "replicas: 3\nimage: y\n\n",
                         base_sha256=prev["base_sha256"], write=True)
    assert done["applied"] is True
    assert path.read_bytes() == (
        b"name: app\nport: 80\n\nreplicas: 3\nimage: y\n\nlast: true\n")
    assert done["after"]["sha256"] == done["new_sha256"]
    backup = Path(done["backup"])
    assert backup.read_bytes() == before
    assert backup.parent == state_path(root, "edits", "backups")


def test_replace_chunk_keeps_crlf_and_line_break(root: Path) -> None:
    path = root / "deploy" / "w.ini"
    path.write_bytes(b"[a]\r\nx=1\r\n\r\n[b]\r\ny=2\r\n")
    out = replace_chunk(root, "deploy/w.ini", "text:deploy/w.ini#L1-L3",
                        "[a]\nx=2", write=True)
    assert path.read_bytes() == b"[a]\r\nx=2\r\n[b]\r\ny=2\r\n"
    assert out["newline"] == "crlf" and out["keys_shifted"] is True


def test_replace_chunk_stale_base_and_missing_key(root: Path) -> None:
    with pytest.raises(CodeEditError, match="stale base"):
        replace_chunk(root, "deploy/app.yaml", "text:deploy/app.yaml#L1-L3",
                      "x: 1\n", base_sha256="0" * 64, write=True)
    with pytest.raises(CodeEditError) as exc:
        replace_chunk(root, "deploy/app.yaml", "text:deploy/app.yaml#L2-L5", "x\n")
    msg = str(exc.value)
    assert "the file changed" in msg and "text:deploy/app.yaml#L1-L3" in msg
    with pytest.raises(CodeEditError, match="not a text key"):
        replace_chunk(root, "deploy/app.yaml", "file:deploy/app.yaml", "x\n")
    (root / "m.py").write_text("x = 1\n")
    with pytest.raises(CodeEditError, match="structural"):
        replace_chunk(root, "m.py", "text:m.py#L1-L1", "x = 2\n")
    assert not state_path(root, "edits", "backups").exists()


def test_is_structured() -> None:
    for rel in ("a.py", "b/c.RS", "x.lua", "README.md", "n.markdown", "d.json",
                "w.ts", "w.tsx", "m.go", "J.java", "c.c", "h.hpp"):
        assert is_structured(rel), rel
    for rel in ("a.yaml", "Makefile", ".gitignore", "x.toml", "s.sh", "a.txt"):
        assert not is_structured(rel), rel


def test_coverage_counts(tmp_path: Path) -> None:
    r = tmp_path / "cov"
    (r / "src").mkdir(parents=True)
    (r / "conf").mkdir()
    (r / "src" / "a.py").write_text("x = 1\n")
    (r / "src" / "b.rs").write_text("fn main() {}\n")
    (r / "conf" / "app.yaml").write_text("a: 1\n\nb: 2\n")
    (r / "conf" / "logo.png").write_bytes(b"\x89PNG\x00\x00data")
    (r / "conf" / "huge.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    (r / "README.md").write_text("# hi\n")
    (r / "Makefile").write_text("all:\n\techo hi\n")
    (r / "gone.txt").write_text("bye\n")
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    (r / "gone.txt").unlink()
    (r / "untracked.txt").write_text("ignored\n")

    cov = coverage(r)
    assert cov["total"] == {"files": 8, "structured": 3, "text": 2, "binary": 1,
                            "oversized": 1, "missing": 1, "text_chunks": 3}
    assert cov["dirs"]["src"]["structured"] == 2
    assert cov["dirs"]["conf"] == {"files": 3, "structured": 0, "text": 1,
                                   "binary": 1, "oversized": 1, "missing": 0,
                                   "text_chunks": 2}
    assert cov["dirs"]["."]["missing"] == 1
    assert cov["text_suffixes"] == {".yaml": 1, "(none)": 1}
    table = text_nodes.format_coverage(cov)
    assert "TOTAL" in table and "conf" in table


def test_coverage_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    r = tmp_path / "cli"
    r.mkdir()
    (r / "a.toml").write_text("x = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    assert text_nodes.main(["coverage", "--root", str(r)]) == 0
    assert "TOTAL" in capsys.readouterr().out
    assert text_nodes.main(["coverage", "--root", str(tmp_path / "nope")]) != 0
