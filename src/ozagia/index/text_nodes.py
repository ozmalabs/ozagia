"""ozagia.index.text_nodes — line-range chunks for files ozagia does not parse.

Files with a structural parser (Python, Rust, Lua, Markdown, JSON and the
tree-sitter tag languages) become File/Symbol/Doc nodes. Every other tracked
text file (YAML, TOML, shell, templates, ...) is addressed as text chunks,
keyed `text:<rel>#L<start>-L<end>`.

Chunking rule (`text_chunks`): walk the lines; once the current chunk has at
least one non-blank line, a blank line marks it for closing, further blank
lines join it, and the next non-blank line starts a new chunk. A chunk is
also closed when it reaches `MAX_CHUNK_LINES` lines. Blank runs therefore
attach to the chunk before them, leading blank lines belong to the first
chunk, and the chunks cover the file contiguously. Lines are split at "\\n"
only and keep their line endings, so joining the chunk texts gives the file
back exactly; an empty file has no chunks.

Keys are positional: inserting or deleting lines above a chunk changes its
key and the keys of every chunk after it. A caller that holds a key across an
edit re-chunks the file and finds the chunk again with `locate_chunk`.
`replace_chunk` refuses a key that no longer names a chunk and lists the
nearest current keys.

    python -m ozagia.index.text_nodes coverage --root <repo> [--json]
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from ozagia.core.keys import text_key
from ozagia.edit.code_edit import MAX_SOURCE_BYTES, CodeEditError
from ozagia.edit.txn import (
    check_base,
    encode_text,
    normalize_newlines,
    read_source,
    sha256_bytes,
    write_source,
)
from ozagia.index.tag_languages import LANGUAGES

MAX_CHUNK_LINES = 60
MAX_TEXT_BYTES = 2 * 1024 * 1024
BINARY_SNIFF_BYTES = 8 * 1024

# Suffixes with a structural indexer or edit adapter besides the tag languages.
_STRUCTURED_SUFFIXES = frozenset({".py", ".rs", ".lua", ".md", ".markdown",
                                  ".json"})
STRUCTURED_SUFFIXES = _STRUCTURED_SUFFIXES | frozenset(LANGUAGES)

_LINE_RE = re.compile(r"[^\n]*\n|[^\n]+\Z")
_KEY_RE = re.compile(r"^text:(?P<rel>.+)#L(?P<start>\d+)-L(?P<end>\d+)$")
_NEWLINE_NAMES = {"\n": "lf", "\r\n": "crlf", "\r": "cr"}


def split_lines(text: str) -> list[str]:
    """Lines of `text` split at "\\n", each keeping its line ending."""
    return _LINE_RE.findall(text)


def text_chunks(rel: str, text: str) -> list[dict[str, Any]]:
    """The chunks of one file (see the module docstring for the rule)."""
    chunks: list[dict[str, Any]] = []
    cur: list[str] = []
    start = 1
    has_content = False
    closing = False

    def emit() -> None:
        end = start + len(cur) - 1
        chunks.append({"key": text_key(rel, start, end), "start_line": start,
                       "end_line": end, "text": "".join(cur)})

    for line in split_lines(text):
        blank = not line.strip()
        if cur and (len(cur) >= MAX_CHUNK_LINES or (closing and not blank)):
            emit()
            start += len(cur)
            cur = []
            has_content = closing = False
        cur.append(line)
        if not blank:
            has_content = True
        elif has_content:
            closing = True
    if cur:
        emit()
    return chunks


def locate_chunk(chunks: list[dict[str, Any]], line: int) -> dict[str, Any] | None:
    """The chunk that contains 1-based `line`, or None."""
    for chunk in chunks:
        if chunk["start_line"] <= line <= chunk["end_line"]:
            return chunk
    return None


def is_structured(rel: str) -> bool:
    """True when ozagia parses files with this suffix structurally."""
    return PurePosixPath(str(rel)).suffix.lower() in STRUCTURED_SUFFIXES


def _parse_text_key(key: str) -> tuple[str, int, int] | None:
    m = _KEY_RE.match(str(key))
    if not m:
        return None
    return m.group("rel"), int(m.group("start")), int(m.group("end"))


def _nearest_keys(chunks: list[dict[str, Any]], line: int, n: int = 3) -> list[str]:
    ranked = sorted(chunks, key=lambda c: (abs(c["start_line"] - line),
                                           c["start_line"]))
    return [c["key"] for c in ranked[:n]]


def replace_chunk(root: str | Path, rel: str, key: str, new_text: str, *,
                  base_sha256: str | None = None,
                  write: bool = False) -> dict[str, Any]:
    """Replace the text of one chunk of a text-only file.

    Preview by default; `write=True` writes through `ozagia.edit.txn` (base
    hash check, backup under the state dir, atomic replace). `new_text` is
    converted to the file's line ending; if the old chunk ended with a newline
    and `new_text` is non-empty without one, the newline is added so the next
    line is not joined onto it. An empty `new_text` deletes the chunk.
    """
    parsed = _parse_text_key(key)
    if parsed is None:
        raise CodeEditError(f"not a text key: {key!r}")
    if parsed[0] != str(rel).strip():
        raise CodeEditError(f"key {key!r} names {parsed[0]!r}, not {rel!r}")
    if is_structured(rel):
        raise CodeEditError(
            f"{rel} has a structural edit adapter; text chunks are for "
            f"unparsed files only")
    if not isinstance(new_text, str):
        raise CodeEditError("new_text must be a string")
    suffix = PurePosixPath(str(rel)).suffix.lower()
    src = read_source(root, rel, suffixes=(suffix,), max_bytes=MAX_SOURCE_BYTES,
                      error_cls=CodeEditError)
    if b"\0" in src.raw[:BINARY_SNIFF_BYTES]:
        raise CodeEditError(f"{src.rel} is binary")
    check_base(src, base_sha256, CodeEditError)

    chunks = text_chunks(src.rel, src.text)
    chunk = next((c for c in chunks if c["key"] == key), None)
    if chunk is None:
        raise CodeEditError(
            f"no chunk {key} in {src.rel}: the file changed; nearest current "
            f"keys: {', '.join(_nearest_keys(chunks, parsed[1])) or '(none)'}")

    replacement = normalize_newlines(new_text, src.newline)
    if replacement and chunk["text"].endswith("\n") and not replacement.endswith("\n"):
        replacement += src.newline
    lines = split_lines(src.text)
    before = "".join(lines[:chunk["start_line"] - 1])
    after = "".join(lines[chunk["end_line"]:])
    new_source = before + replacement + after

    start = chunk["start_line"]
    new_count = len(split_lines(replacement))
    new_chunks = text_chunks(src.rel, new_source)
    new_keys = [c["key"] for c in new_chunks
                if new_count and c["start_line"] <= start + new_count - 1
                and c["end_line"] >= start]
    new_sha = sha256_bytes(encode_text(src, new_source))
    diff_lines = list(difflib.unified_diff(
        src.text.splitlines(keepends=True), new_source.splitlines(keepends=True),
        fromfile=f"a/{src.rel}", tofile=f"b/{src.rel}"))

    backup = None
    applied = False
    if write and new_source != src.text:
        backup = write_source(root, src, new_source, error_cls=CodeEditError)
        applied = True
    return {
        "ok": True,
        "applied": applied,
        "format": "text",
        "file": src.rel,
        "key": key,
        "new_keys": new_keys,
        "keys_shifted": new_count != chunk["end_line"] - start + 1,
        "base_sha256": src.sha256,
        "new_sha256": new_sha,
        "before": {"sha256": src.sha256, "lines": len(lines)},
        "after": {"sha256": new_sha, "lines": len(split_lines(new_source))},
        "backup": str(backup) if backup else None,
        "newline": _NEWLINE_NAMES.get(src.newline, "lf"),
        "bom": src.bom,
        "diff_view": "".join(diff_lines[:400]),
        "graph_stale": applied,
    }


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #

_CATEGORIES = ("structured", "text", "binary", "oversized", "missing")


def _git_files(root: Path) -> list[str]:
    try:
        proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(root),
                              capture_output=True, check=False)
    except OSError as e:
        raise RuntimeError(f"git ls-files failed in {root}: {e}") from e
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed in {root}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}")
    return [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0")
            if p]


def classify(root: str | Path, rel: str) -> tuple[str, int]:
    """(category, chunk count) of one tracked file; the count is for text only."""
    path = Path(root) / rel
    if path.is_symlink() or not path.is_file():
        return "missing", 0
    try:
        size = path.stat().st_size
        if size > MAX_TEXT_BYTES:
            return "oversized", 0
        with open(path, "rb") as fh:
            head = fh.read(BINARY_SNIFF_BYTES)
            if b"\0" in head:
                return "binary", 0
            if is_structured(rel):
                return "structured", 0
            data = head + fh.read()
    except OSError:
        return "missing", 0
    text = data.decode("utf-8", errors="replace")
    return "text", len(text_chunks(rel, text))


def coverage(root: str | Path) -> dict[str, Any]:
    """Tracked files by category, per top-level directory.

    Categories: `structured` (a structural parser exists), `text` (text
    chunks), `binary` (NUL in the first 8 KiB), `oversized` (> 2 MiB),
    `missing` (tracked but not a regular file on disk: deleted, a symlink or a
    submodule). `text_chunks` counts the chunks the text files would have.
    Top-level files are grouped under ".".
    """
    root = Path(root).resolve()
    dirs: dict[str, dict[str, int]] = {}
    total = {c: 0 for c in _CATEGORIES} | {"text_chunks": 0, "files": 0}
    text_suffixes: dict[str, int] = {}
    for rel in _git_files(root):
        top = rel.split("/", 1)[0] if "/" in rel else "."
        row = dirs.setdefault(top, {c: 0 for c in _CATEGORIES}
                              | {"text_chunks": 0, "files": 0})
        cat, n_chunks = classify(root, rel)
        for bucket in (row, total):
            bucket[cat] += 1
            bucket["files"] += 1
            bucket["text_chunks"] += n_chunks
        if cat == "text":
            suffix = PurePosixPath(rel).suffix.lower() or "(none)"
            text_suffixes[suffix] = text_suffixes.get(suffix, 0) + 1
    return {
        "root": str(root),
        "total": total,
        "dirs": dict(sorted(dirs.items())),
        "text_suffixes": dict(sorted(text_suffixes.items(),
                                     key=lambda kv: (-kv[1], kv[0]))),
    }


def format_coverage(cov: dict[str, Any]) -> str:
    cols = ("files", *_CATEGORIES, "text_chunks")
    width = max([len("dir"), len("TOTAL"), *(len(d) for d in cov["dirs"])])
    head = f"{'dir':<{width}}  " + "  ".join(f"{c:>10}" for c in cols)
    lines = [head, "-" * len(head)]
    for name, row in [*cov["dirs"].items(), ("TOTAL", cov["total"])]:
        lines.append(f"{name:<{width}}  "
                     + "  ".join(f"{row[c]:>10}" for c in cols))
    if cov["text_suffixes"]:
        top = list(cov["text_suffixes"].items())[:15]
        lines.append("")
        lines.append("text-only suffixes: "
                     + ", ".join(f"{s} {n}" for s, n in top))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ozagia.index.text_nodes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    cov = sub.add_parser("coverage", help="structured vs text-only files")
    cov.add_argument("--root", default=".")
    cov.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "coverage":
        try:
            result = coverage(args.root)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2) if args.json
              else format_coverage(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
