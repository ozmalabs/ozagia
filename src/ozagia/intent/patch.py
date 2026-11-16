"""ozagia.intent.patch — check a Codex `apply_patch` against the current files.

`check_patch(root, text, cwd=...)` parses a patch in the Codex/OpenAI
apply_patch format and checks every hunk against the files as they are now,
the way Codex's `apply_patch` would match it, without writing anything. For
each hunk that would fail it finds the nearest region of the current file
and returns that region's text with line numbers, its line range, the node
key that encloses it and the file's sha256, so the agent can fix the patch
without reading the file again. `render_reply` turns the result into the
text a Codex PreToolUse hook gives as its deny reason.

Matching semantics source: the open-source Codex implementation,
github.com/openai/codex, `codex-rs/apply-patch/src/` at commit 4517871
(main, fetched 2026-11-16): `parser.rs` (boundaries, lenient heredoc),
`streaming_parser.rs` (line grammar), `seek_sequence.rs` (line matching),
`file_update.rs` (`compute_replacements`) and `text_file.rs` (line
splitting and line endings). Ported here:

- Patch text: trimmed, split at "\\n" (a trailing "\\r" dropped). The first
  line must be `*** Begin Patch` and the last `*** End Patch` (each
  trimmed); else, leniently, a `<<EOF` / `<<'EOF'` / `<<"EOF"` first line
  and a last line ending in `EOF` around them (at least 4 lines).
- Headers (trimmed; in an update section only trailing whitespace is
  trimmed): `*** Add File: p` (then `+` lines, each one line of content
  plus "\\n"), `*** Delete File: p`, `*** Update File: p` (then an optional
  `*** Move to: p` before the first chunk), `*** End Patch`. `*** Environment
  ID: x` right after Begin Patch names the environment the patch is for
  (`ParsedPatch.env_id`).
- An update section: `@@` or `@@ <context line>` starts a chunk (the first
  chunk needs none); ` x` is context, `-x` old, `+x` new, an empty line is
  empty context; `*** End of File` marks the chunk as the end of the file
  (after it only blank lines or a new `@@`). An update with no chunk, a
  chunk with no lines, or any other line is a parse error.
- Files are split into lines at "\\r\\n", "\\r" and "\\n"; the line texts
  are matched without their endings.
- Per chunk, in order, from a cursor that starts at line 0: a context line
  is searched (`seek_sequence`, one line) from the cursor and the cursor
  moves past it ("Failed to find context" if absent). A chunk with no old
  lines appends its new lines at the end of the file. Otherwise the old
  lines (context and `-` lines) are searched from the cursor, and once more
  without a trailing empty line; the cursor moves past the match ("Failed
  to find expected lines" if absent). `seek_sequence` tries, in order,
  exact lines, lines with trailing whitespace trimmed, lines trimmed on
  both sides, and trimmed lines with Unicode dashes, quotes and spaces
  mapped to ASCII; with `*** End of File` the search starts at
  `len(lines) - len(pattern)` (or the cursor, if later), so the lines must
  end the file. Whitespace is Rust's `char::is_whitespace` (Unicode
  White_Space).
- Hunks apply in patch order: a file added, updated, moved or deleted by an
  earlier section is seen in that state by a later one.

ozagia's own rules on top:

- A patch with `*** Environment ID:` (any value) is for an environment the
  checker may not see; it is not checked (`skipped: "environment"`, `ok`),
  so the hook lets it through.
- Paths are taken relative to `cwd` (default: the root), as Codex does, and
  must resolve (symlinks too) inside the root, outside `.git`; others are
  `refused` and not checked.
- A file that does not exist, cannot be read as UTF-8 text, or is larger
  than `MAX_FILE_BYTES` is reported (`missing`, `unreadable`) and not
  checked further. Only chunk misses in files that were read count as
  `misses`; the hook denies on those alone.
- Unlike Codex, which stops at the first failing chunk, every chunk is
  checked; after a failing chunk the cursor stays where it was.

Nearest region (`nearest_region`): the expected old lines (with the context
line in front when the context was not found), whitespace-normalised, are
compared with windows of the current file. Candidate positions come from
exact matches of normalised lines (rarer lines weigh more) and from the
fuzzy best matches of the three longest expected lines; around each,
windows of nearby starts and sizes are ranked by a line-level difflib ratio
and the best one is scored by a character-level difflib ratio. Windows that
start before the cursor (before the anchor or the previous chunk) lose 0.05.
Below `MIN_SIMILARITY` there is no region. When the expected lines occur
exactly before the cursor (hunks out of order), that place is the region.

Each region names the node that encloses it (`region_key`) with the node's
full line range and the sha256 of its current text (`node_view`), which is
the `base_sha256` a key edit of that node takes (`ozagia.edit.key_edit`).
When the region is only part of the node the reply says so: a key edit
replaces the whole node.

Caps: `MAX_REGIONS` failing hunks get a region, each shows at most
`MAX_REGION_LINES` lines (lines cut at `MAX_LINE_CHARS`), and the reply
stays within `max_chars` (default `MAX_REPLY_CHARS`).
"""
from __future__ import annotations

import difflib
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from ozagia.edit.txn import LINE_BREAK_RE

BEGIN = "*** Begin Patch"
END = "*** End Patch"
ADD = "*** Add File: "
DELETE = "*** Delete File: "
UPDATE = "*** Update File: "
MOVE = "*** Move to: "
EOF_MARK = "*** End of File"
CONTEXT = "@@ "
EMPTY_CONTEXT = "@@"
ENV_ID = "*** Environment ID:"

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_REGIONS = 4
MAX_REGION_LINES = 60
MAX_REPLY_CHARS = 8000
MAX_LINE_CHARS = 1500
MAX_DIFF_LINES = 12
MIN_SIMILARITY = 0.35
# Normalised lines that occur more often than this give no position votes.
_COMMON_LINE = 64
_CANDIDATES = 6
_BINARY_SNIFF = 8 * 1024

# Rust's char::is_whitespace: the Unicode White_Space property.
RUST_WS = ("\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004"
           "\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")
_PUNCT = str.maketrans({
    **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
    **dict.fromkeys("\u2018\u2019\u201a\u201b", "'"),
    **dict.fromkeys("\u201c\u201d\u201e\u201f", '"'),
    **dict.fromkeys("\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
                    "\u200a\u202f\u205f\u3000", " "),
})
_HEREDOC_OPEN = ("<<EOF", "<<'EOF'", '<<"EOF"')
_LINE_SPLIT_RE = LINE_BREAK_RE


class PatchParseError(ValueError):
    """The patch text is not in the apply_patch format."""

    def __init__(self, message: str, line_number: int | None = None):
        super().__init__(message if line_number is None
                         else f"invalid hunk at line {line_number}, {message}")
        self.line_number = line_number


@dataclass
class Chunk:
    context: str | None = None
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    # (old index, new index) of lines parsed as context.
    ctx_idx: list[tuple[int, int]] = field(default_factory=list)
    eof: bool = False

    def push_context(self, line: str) -> None:
        self.ctx_idx.append((len(self.old), len(self.new)))
        self.old.append(line)
        self.new.append(line)

    @property
    def empty(self) -> bool:
        return not self.old and not self.new


@dataclass
class ParsedPatch:
    ops: list["FileOp"]
    env_id: str | None = None


@dataclass
class FileOp:
    kind: str                   # "add" | "delete" | "update"
    path: str
    line: int                   # the header's line in the patch
    move_to: str | None = None
    contents: str = ""
    chunks: list[Chunk] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Parsing (parser.rs, streaming_parser.rs)
# --------------------------------------------------------------------------- #
def _rtrim(s: str) -> str:
    return s.rstrip(RUST_WS)


def _trim(s: str) -> str:
    return s.strip(RUST_WS)


def _boundaries(lines: list[str]) -> list[str]:
    def strict(ls: list[str]) -> str | None:
        first = _trim(ls[0]) if ls else None
        last = _trim(ls[-1]) if ls else None
        if first == BEGIN and last == END:
            return None
        if first != BEGIN:
            return "The first line of the patch must be '*** Begin Patch'"
        return "The last line of the patch must be '*** End Patch'"

    err = strict(lines)
    if err is None:
        return lines
    if (len(lines) >= 4 and lines[0] in _HEREDOC_OPEN
            and lines[-1].endswith("EOF")):
        inner = lines[1:-1]
        err2 = strict(inner)
        if err2 is None:
            return inner
        raise PatchParseError(f"invalid patch: {err2}")
    raise PatchParseError(f"invalid patch: {err}")


class _Parser:
    def __init__(self) -> None:
        self.mode = "not_started"
        self.ops: list[FileOp] = []
        self.line_number = 0
        self.env_id: str | None = None

    def _ensure_update_not_empty(self, line: str) -> None:
        op = self.ops[-1] if self.ops else None
        if op is None or op.kind != "update":
            return
        if not op.chunks and self.mode == "update":
            raise PatchParseError(f"Update file hunk for path '{op.path}' is empty",
                                  op.line)
        if op.chunks and op.chunks[-1].empty:
            if line == END:
                raise PatchParseError("Update hunk does not contain any lines",
                                      self.line_number)
            raise PatchParseError(_unexpected(line), self.line_number)

    def _headers(self, trimmed: str) -> bool:
        if self.mode == "started" and trimmed.startswith(ENV_ID):
            if self.env_id is not None:
                raise PatchParseError("invalid patch: apply_patch environment_id "
                                      "cannot be specified more than once")
            env = _trim(trimmed[len(ENV_ID):])
            if not env:
                raise PatchParseError("invalid patch: apply_patch environment_id "
                                      "cannot be empty")
            self.env_id = env
            return True
        if trimmed == END:
            self._ensure_update_not_empty(trimmed)
            self.mode = "ended"
            return True
        for marker, kind in ((ADD, "add"), (DELETE, "delete"), (UPDATE, "update")):
            if trimmed.startswith(marker):
                self._ensure_update_not_empty(trimmed)
                self.ops.append(FileOp(kind, trimmed[len(marker):], self.line_number))
                self.mode = kind
                return True
        return False

    def line(self, line: str) -> None:
        self.line_number += 1
        trimmed = _trim(line)
        mode = self.mode
        if mode == "not_started":
            if trimmed == BEGIN:
                self.mode = "started"
                return
            raise PatchParseError(
                "invalid patch: The first line of the patch must be '*** Begin Patch'")
        if mode in ("started", "delete"):
            if self._headers(trimmed):
                return
            raise PatchParseError(_bad_header(trimmed), self.line_number)
        if mode == "add":
            if self._headers(trimmed):
                return
            if line.startswith("+"):
                self.ops[-1].contents += line[1:] + "\n"
                return
            raise PatchParseError(_bad_header(trimmed), self.line_number)
        if mode == "update":
            self._update_line(line)
            return
        if mode == "ended":
            if trimmed:
                raise PatchParseError(
                    "invalid patch: The last line of the patch must be '*** End Patch'")
            return

    def _update_line(self, line: str) -> None:
        update_line = _rtrim(line)
        if self._headers(update_line):
            return
        op = self.ops[-1]
        chunks = op.chunks
        is_ctx = update_line == EMPTY_CONTEXT or update_line.startswith(CONTEXT)
        if chunks and chunks[-1].eof:
            if not update_line:
                return
            if not is_ctx:
                raise PatchParseError(
                    f"Expected update hunk to start with a @@ context marker, "
                    f"got: '{line}'", self.line_number)
        if not chunks and op.move_to is None and update_line.startswith(MOVE):
            op.move_to = update_line[len(MOVE):]
            return
        if is_ctx and chunks and chunks[-1].empty:
            raise PatchParseError(_unexpected(line), self.line_number)
        if update_line == EMPTY_CONTEXT:
            chunks.append(Chunk())
            return
        if update_line.startswith(CONTEXT):
            chunks.append(Chunk(context=update_line[len(CONTEXT):]))
            return
        if update_line == EOF_MARK:
            if chunks and chunks[-1].empty:
                raise PatchParseError("Update hunk does not contain any lines",
                                      self.line_number)
            if chunks:
                chunks[-1].eof = True
            return
        if line == "" or line[0] in " +-":
            if not chunks:
                chunks.append(Chunk())
            c = chunks[-1]
            if line == "":
                c.push_context("")
            elif line[0] == " ":
                c.push_context(line[1:])
            elif line[0] == "+":
                c.new.append(line[1:])
            else:
                c.old.append(line[1:])
            return
        if chunks and not chunks[-1].empty:
            raise PatchParseError(
                f"Expected update hunk to start with a @@ context marker, "
                f"got: '{line}'", self.line_number)
        raise PatchParseError(_unexpected(line), self.line_number)

    def finish(self, last: str | None) -> list[FileOp]:
        if last is not None:
            if _trim(last) == END:
                self.line_number += 1
                self._ensure_update_not_empty(END)
                self.mode = "ended"
            else:
                self.line(last)
        if self.mode != "ended":
            raise PatchParseError(
                "invalid patch: The last line of the patch must be '*** End Patch'")
        return self.ops


def _unexpected(line: str) -> str:
    return (f"Unexpected line found in update hunk: '{line}'. Every line should "
            f"start with ' ' (context line), '+' (added line), or '-' (removed line)")


def _bad_header(trimmed: str) -> str:
    return (f"'{trimmed}' is not a valid hunk header. Valid hunk headers: "
            f"'*** Add File: {{path}}', '*** Delete File: {{path}}', "
            f"'*** Update File: {{path}}'")


def parse_patch(text: str) -> ParsedPatch:
    """The file operations of an apply_patch text, in order, and its
    `*** Environment ID:` (or None). Raises PatchParseError as Codex would
    refuse the patch."""
    if not isinstance(text, str):
        raise PatchParseError("invalid patch: not a string")
    body = _trim(text)
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in body.split("\n")] if body else []
    lines = _boundaries(lines)
    p = _Parser()
    for ln in lines[:-1]:
        p.line(ln)
    ops = p.finish(lines[-1] if lines else None)
    return ParsedPatch(ops, p.env_id)


# --------------------------------------------------------------------------- #
# Matching (seek_sequence.rs, file_update.rs, text_file.rs)
# --------------------------------------------------------------------------- #
def split_source(content: str) -> list[tuple[str, str | None]]:
    """(text, ending) per line, split at "\\r\\n", "\\r" and "\\n"."""
    parts = _LINE_SPLIT_RE.split(content)
    out: list[tuple[str, str | None]] = []
    for i in range(0, len(parts) - 1, 2):
        out.append((parts[i], parts[i + 1]))
    if parts and parts[-1]:
        out.append((parts[-1], None))
    return out


def _normalise_unicode(s: str) -> str:
    return _trim(s).translate(_PUNCT)


_MODES: list[tuple[str, Callable[[str], str] | None]] = [
    ("exact", None), ("rstrip", _rtrim), ("trim", _trim),
    ("unicode", _normalise_unicode)]


def seek_sequence(lines: list[str], pattern: list[str], start: int,
                  eof: bool) -> tuple[int, str] | None:
    """`(index, mode)` of `pattern` in `lines` at or after `start`, or None
    (Codex's `seek_sequence`; `mode` is the pass that matched)."""
    if not pattern:
        return start, "exact"
    n, total = len(pattern), len(lines)
    if n > total:
        return None
    search_start = max(total - n, start) if eof else start
    last = total - n
    for mode, fn in _MODES:
        pat = pattern if fn is None else [fn(p) for p in pattern]
        for i in range(search_start, last + 1):
            if fn is None:
                if lines[i:i + n] == pat:
                    return i, mode
            elif all(fn(lines[i + k]) == pat[k] for k in range(n)):
                return i, mode
    return None


def _match_chunk(lines: list[str], chunk: Chunk, cursor: int
                 ) -> tuple[dict[str, Any], list[tuple[int, int, list[str]]], int]:
    """(hunk result, replacements, new cursor) for one chunk."""
    res: dict[str, Any] = {"anchor": chunk.context, "status": "ok",
                           "reason": None, "line": None, "match": None,
                           "cursor": cursor}
    if chunk.context is not None:
        got = seek_sequence(lines, [chunk.context], cursor, False)
        if got is None:
            res.update(status="failed", reason="context")
            return res, [], cursor
        cursor = got[0] + 1
        res["anchor_line"] = got[0] + 1
        res["cursor"] = cursor
    if not chunk.old:
        res.update(line=len(lines) + 1, match="append")
        return res, [(len(lines), 0, list(chunk.new))], cursor
    pattern, new_slice = chunk.old, chunk.new
    got = seek_sequence(lines, pattern, cursor, chunk.eof)
    if got is None and pattern and pattern[-1] == "":
        pattern = pattern[:-1]
        if new_slice and new_slice[-1] == "":
            new_slice = new_slice[:-1]
        got = seek_sequence(lines, pattern, cursor, chunk.eof)
    if got is None:
        res.update(status="failed", reason="lines")
        return res, [], cursor
    start, mode = got
    repl: list[tuple[int, int, list[str]]] = []
    old_start = new_start = 0
    for oc, nc in chunk.ctx_idx:
        if oc >= len(pattern) or nc >= len(new_slice):
            break
        if old_start != oc or new_start != nc:
            repl.append((start + old_start, oc - old_start, new_slice[new_start:nc]))
        old_start, new_start = oc + 1, nc + 1
    if old_start != len(pattern) or new_start != len(new_slice):
        repl.append((start + old_start, len(pattern) - old_start,
                     new_slice[new_start:]))
    res.update(line=start + 1, match=mode)
    return res, repl, start + len(pattern)


def _apply(src: list[tuple[str, str | None]],
           repl: list[tuple[int, int, list[str]]]) -> str:
    preferred = next((e for _, e in src if e), "\n")
    out: list[tuple[str, str | None]] = []
    si = 0
    for start, old_len, seg in sorted(repl, key=lambda r: r[0]):
        take = max(0, start - si)
        out.extend(src[si:si + take])
        si = min(len(src), si + take + old_len)
        out.extend((t, preferred) for t in seg)
    out.extend(src[si:])
    return "".join(t + (e or preferred) for t, e in out)


# --------------------------------------------------------------------------- #
# Nearest region
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    return " ".join(s.split())


def nearest_region(lines: list[str], expected: list[str], *,
                   after: int = 0) -> dict[str, Any] | None:
    """The window of `lines` most like `expected`: `{start, end, ratio}`
    (0-based, end exclusive), or None when nothing reaches MIN_SIMILARITY
    (see the module docstring)."""
    exp = [_norm(x) for x in expected]
    while exp and not exp[-1]:
        exp.pop()
    n, total = len(exp), len(lines)
    if n == 0 or total == 0:
        return None
    file_n = [_norm(x) for x in lines]
    where: dict[str, list[int]] = {}
    for i, s in enumerate(file_n):
        if s:
            where.setdefault(s, []).append(i)
    votes: dict[int, float] = {}
    for j, e in enumerate(exp):
        hits = where.get(e) if e else None
        if not hits or len(hits) > _COMMON_LINE:
            continue
        w = 1.0 / len(hits)
        for p in hits:
            votes[p - j] = votes.get(p - j, 0.0) + w
    distinct = sorted({(len(e), j) for j, e in enumerate(exp) if len(e) >= 4},
                      reverse=True)[:3]
    sm = difflib.SequenceMatcher(autojunk=False)
    for _, j in distinct:
        sm.set_seq2(exp[j])
        best: list[tuple[float, int]] = []
        for i, s in enumerate(file_n):
            if not s:
                continue
            floor = best[-1][0] if len(best) >= 4 else 0.5
            sm.set_seq1(s)
            if sm.real_quick_ratio() <= floor or sm.quick_ratio() <= floor:
                continue
            r = sm.ratio()
            if r > floor:
                best.append((r, i))
                best.sort(key=lambda t: (-t[0], t[1]))
                del best[4:]
        for r, i in best:
            votes[i - j] = votes.get(i - j, 0.0) + 0.5 * r
    if not votes:
        return None
    cands = sorted(votes.items(), key=lambda kv: (-kv[1], abs(kv[0] - after)))
    seen: set[int] = set()
    starts: list[int] = []
    for s, _ in cands:
        s = min(max(s, 0), total - 1)
        if s not in seen:
            seen.add(s)
            starts.append(s)
        if len(starts) >= _CANDIDATES:
            break
    d = min(3, max(1, n // 4))
    exp_text = "\n".join(exp)[:6000]
    lm = difflib.SequenceMatcher(autojunk=False)
    lm.set_seq2(exp)
    best_pick: tuple[float, float, int, int] | None = None
    for c in starts:
        pick = (-1.0, c, min(total, c + n))
        for ds in range(-d, d + 1):
            s = c + ds
            if s < 0 or s >= total:
                continue
            for size in range(max(1, n - d), n + d + 1):
                e = min(total, s + size)
                lm.set_seq1(file_n[s:e])
                r = lm.ratio() - 0.001 * (abs(ds) + abs(size - n))
                if r > pick[0]:
                    pick = (r, s, e)
        _, s, e = pick
        cm = difflib.SequenceMatcher(None, exp_text, "\n".join(file_n[s:e])[:6000],
                                     autojunk=False)
        score = cm.ratio() - (0.05 if s < after else 0.0)
        if best_pick is None or (score, pick[0]) > best_pick[:2]:
            best_pick = (score, pick[0], s, e)
    if best_pick is None:
        return None
    score, _, s, e = best_pick
    if score < MIN_SIMILARITY:
        return None
    return {"start": s, "end": e, "ratio": round(max(score, 0.0), 3)}


def compact_diff(expected: list[str], actual: list[str],
                 limit: int = MAX_DIFF_LINES) -> list[str]:
    """`-expected` / `+actual` lines that differ (difflib, no context)."""
    out = [ln for ln in difflib.unified_diff(expected, actual, n=0, lineterm="")
           if not ln.startswith(("---", "+++", "@@"))]
    if len(out) > limit:
        more = len(out) - limit
        out = out[:limit] + [f"... ({more} more differing lines)"]
    return out


# --------------------------------------------------------------------------- #
# Node keys for regions
# --------------------------------------------------------------------------- #
KeyFn = Callable[[str, str, int, int], str]


def _offsets(text: str) -> list[int]:
    out = [0]
    for m in _LINE_SPLIT_RE.finditer(text):
        out.append(m.end())
    return out


def region_key(root: str | Path, rel: str, text: str, start_line: int,
               end_line: int, memory: Any = None) -> str:
    """The key of the node enclosing lines `start_line..end_line` of `rel`:
    sym:/test: for Python, `doc:<rel>#<slug>` for Markdown and TeX (the
    `ozagia.core.sections` key a read or outline shows; `doc:<rel>` before
    the first heading), `json:<rel>#<pointer>` for JSON, the `text:` chunk
    of an unparsed file, else `file:`. With `memory`, the graph's own key
    for Python."""
    suffix = PurePosixPath(rel).suffix.lower()
    offs = _offsets(text)
    s = offs[start_line - 1] if start_line - 1 < len(offs) else len(text)
    e = offs[end_line] - 1 if end_line < len(offs) else len(text)
    e = max(e, s)
    from ozagia.core.sections import (
        doc_sections,
        has_sections,
        innermost_section,
        line_at,
        section_key,
    )
    if has_sections(rel):
        # Sections count lines at "\n"; locate the region by its offsets.
        sec = innermost_section(doc_sections(rel, text), line_at(text, s),
                                line_at(text, e))
        return section_key(rel, sec.slug) if sec else f"doc:{rel}"
    if suffix == ".json":
        from ozagia.edit.json_edit import _Parser
        from ozagia.edit.span_edit import _json_pointer
        try:
            ptr = _json_pointer(_Parser(text).parse(), s, e)
        except Exception:  # noqa: BLE001 - no pointer: the file key
            ptr = None
        return f"json:{rel}#{ptr}" if ptr else f"file:{rel}"
    if memory is not None:
        from ozagia.intent.common import Keyer
        return Keyer(memory, rel, text).key(start_line)
    from ozagia.edit.span_edit import _Keyer
    return str(_Keyer(root, rel, text).key(s, e, start_line, end_line)["key"])


# --------------------------------------------------------------------------- #
# check_patch
# --------------------------------------------------------------------------- #
def _resolve(root: Path, base: Path, raw: str) -> tuple[Path | None, str | None, str | None]:
    """(real path, root-relative path, refusal reason)."""
    if not raw or "\0" in raw:
        return None, None, "empty or invalid path"
    p = Path(raw)
    if not p.is_absolute():
        p = base / p
    real = Path(os.path.realpath(p))
    try:
        rel = real.relative_to(root).as_posix()
    except ValueError:
        return None, None, "outside the project root"
    if rel in ("", ".") or ".git" in PurePosixPath(rel).parts:
        return None, None, "not a file ozagia checks"
    return real, rel, None


class _Files:
    """The files as the patch sees them: the disk plus earlier sections."""

    def __init__(self) -> None:
        self.state: dict[Path, str | None] = {}

    def read(self, path: Path) -> tuple[str | None, str | None, str | None]:
        """(text, sha256 of the disk bytes, problem)."""
        if path in self.state:
            got = self.state[path]
            return (got, None, None) if got is not None else (None, None, "missing")
        if not path.is_file():
            return None, None, "missing"
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return None, None, f"larger than {MAX_FILE_BYTES} bytes"
            data = path.read_bytes()
        except OSError as e:
            return None, None, f"cannot read: {e}"
        if b"\0" in data[:_BINARY_SNIFF]:
            return None, None, "binary"
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return None, None, "not UTF-8"
        return text, hashlib.sha256(data).hexdigest(), None

    def exists(self, path: Path) -> bool:
        if path in self.state:
            return self.state[path] is not None
        return path.is_file()


def check_patch(root: str | Path, text: str, *, cwd: str | Path | None = None,
                key_fn: KeyFn | None = None, memory: Any = None,
                max_regions: int = MAX_REGIONS,
                max_region_lines: int = MAX_REGION_LINES,
                max_chars: int = MAX_REPLY_CHARS) -> dict[str, Any]:
    """Check an apply_patch text against the files under `root`; write
    nothing. Returns:

        {parsed, error, ok, hunks, misses, files: [...], regions, keys, reply}

    `ok` is true when every chunk of every checked file matches (`misses`
    is 0). Each file: {path, rel, op, move_to, status (ok, failed, missing,
    refused, unreadable), problem, sha256, lines, hunks: [{index, status,
    anchor, reason (context|lines), line, match, region, note}]}. A region:
    {start_line, end_line, ratio, key, text: [lines], diff: [lines]}.
    `reply` is `render_reply(result)` when there are misses, else "".
    A parse error gives `{parsed: False, ok: False, error}`. A patch with an
    `*** Environment ID:` is not checked: `{parsed: True, ok: True,
    skipped: "environment", env_id}`."""
    root_p = Path(os.path.realpath(root))
    base = Path(os.path.realpath(cwd)) if cwd else root_p
    try:
        parsed = parse_patch(text)
    except PatchParseError as e:
        return {"parsed": False, "ok": False, "error": str(e), "hunks": 0,
                "misses": 0, "files": [], "regions": 0, "keys": [], "reply": ""}
    if parsed.env_id is not None:
        # Codex routes the patch to that environment's files, which may not
        # be these: fail open.
        return {"parsed": True, "ok": True, "error": None, "hunks": 0,
                "misses": 0, "files": [], "regions": 0, "keys": [], "reply": "",
                "skipped": "environment", "env_id": parsed.env_id}
    ops = parsed.ops
    files = _Files()
    out_files: list[dict[str, Any]] = []
    hunks = misses = regions = 0
    keys: list[str] = []
    for op in ops:
        real, rel, refused = _resolve(root_p, base, op.path)
        entry: dict[str, Any] = {"path": op.path, "rel": rel, "op": op.kind,
                                 "move_to": op.move_to, "status": "ok",
                                 "problem": None, "sha256": None, "lines": None,
                                 "hunks": []}
        out_files.append(entry)
        if refused or real is None:
            entry.update(status="refused", problem=refused)
            continue
        dest = real
        if op.move_to is not None:
            dreal, _drel, drefused = _resolve(root_p, base, op.move_to)
            if drefused or dreal is None:
                entry.update(status="refused", problem=f"move target {drefused}")
                continue
            dest = dreal
        if op.kind == "add":
            files.state[real] = op.contents
            continue
        if op.kind == "delete":
            if not files.exists(real):
                entry.update(status="missing", problem="missing")
            else:
                files.state[real] = None
            continue
        content, sha, problem = files.read(real)
        if content is None:
            entry.update(status="missing" if problem == "missing" else "unreadable",
                         problem=problem)
            continue
        src = split_source(content)
        lines = [t for t, _ in src]
        entry.update(sha256=sha, lines=len(lines))
        cursor = 0
        repl: list[tuple[int, int, list[str]]] = []
        failed = False
        for i, chunk in enumerate(op.chunks, start=1):
            hunks += 1
            res, r, cursor = _match_chunk(lines, chunk, cursor)
            res["index"] = i
            res["of"] = len(op.chunks)
            entry["hunks"].append(res)
            if res["status"] != "ok":
                failed = True
                misses += 1
                if regions < max_regions:
                    reg = _region_for(root_p, rel, content, lines, chunk, res,
                                      key_fn=key_fn, memory=memory,
                                      max_region_lines=max_region_lines,
                                      file_sha=sha)
                    res["region"] = reg
                    if reg is not None:
                        regions += 1
                        if reg.get("key") and reg["key"] not in keys:
                            keys.append(reg["key"])
            else:
                repl.extend(r)
        if failed:
            entry["status"] = "failed"
            continue
        new = _apply(src, repl)
        if dest != real:
            files.state[real] = None
        files.state[dest] = new
    result = {"parsed": True, "ok": misses == 0, "error": None, "hunks": hunks,
              "misses": misses, "files": out_files, "regions": regions,
              "keys": keys, "reply": ""}
    if misses:
        result["reply"] = render_reply(result, max_chars=max_chars)
    return result


def node_view(root: str | Path, rel: str, key: str, file_sha: str | None,
              memory: Any = None) -> dict[str, Any] | None:
    """`{start_line, end_line, sha256}` of node `key` in the file on disk
    (`sha256` of its current text: the base a key edit of it takes), or None
    when the file is not the one `file_sha` names (changed earlier in the
    patch, or since) or the key has no span (`key_edit.node_span`; a
    graph-sourced span of a file changed since ingest has none here)."""
    if not file_sha:
        return None
    from ozagia.edit.key_edit import node_span, sha256_text
    from ozagia.edit.span_edit import _read
    try:
        src = _read(root, rel)
        if src.sha256 != file_sha:
            return None
        span = node_span(src, key, memory=memory, refresh=False)
    except Exception:  # noqa: BLE001 - the region stands without it
        return None
    return {"start_line": span["start_line"], "end_line": span["end_line"],
            "sha256": sha256_text(src.text[span["start"]:span["end"]])}


def _node_note(start_line: int, end_line: int, node: dict[str, Any] | None) -> str:
    """The node part of a region line: its range and sha256, and whether
    the region is all of it."""
    if node is None:
        return ""
    part = ("" if node["start_line"] >= start_line and node["end_line"] <= end_line
            else "; the region is part of it, a key edit replaces all of it")
    return (f"node L{node['start_line']}-L{node['end_line']} node sha256 "
            f"{node['sha256']}{part}")


def _region_for(root: Path, rel: str, content: str, lines: list[str],
                chunk: Chunk, res: dict[str, Any], *, key_fn: KeyFn | None,
                memory: Any, max_region_lines: int,
                file_sha: str | None = None) -> dict[str, Any] | None:
    cursor = int(res.get("cursor") or 0)
    pattern = list(chunk.old)
    if res["reason"] == "context":
        expected = [chunk.context or "", *pattern]
        earlier = seek_sequence(lines, [chunk.context or ""], 0, False)
        if earlier is not None and earlier[0] < cursor:
            res["note"] = (f"the @@ line is at L{earlier[0] + 1}, before the "
                           f"previous hunk; Codex searches forward only, so "
                           f"hunks must be in file order")
    else:
        expected = pattern
        earlier = seek_sequence(lines, pattern, 0, False)
        if earlier is not None and earlier[0] < cursor:
            res["note"] = (f"these lines are at L{earlier[0] + 1}, before the "
                           f"@@ line or the previous hunk; Codex searches "
                           f"forward only, so hunks must be in file order")
        elif earlier is not None and chunk.eof:
            res["note"] = ("the lines exist but do not end the file "
                           "(*** End of File)")
        else:
            earlier = None
    if earlier is not None:
        start = earlier[0]
        found = {"start": start, "end": min(len(lines), start + len(expected)),
                 "ratio": 1.0}
    else:
        found = nearest_region(lines, expected, after=cursor)
    if found is None:
        return None
    s, e = found["start"], found["end"]
    actual = lines[s:e]
    try:
        if key_fn is not None:
            key = key_fn(rel, content, s + 1, e)
        else:
            key = region_key(root, rel, content, s + 1, e, memory=memory)
    except Exception:  # noqa: BLE001 - a region without a key is still useful
        key = f"file:{rel}"
    shown = actual[:max_region_lines]
    return {"start_line": s + 1, "end_line": e, "ratio": found["ratio"],
            "key": key, "node": node_view(root, rel, key, file_sha, memory),
            "text": [f"{s + 1 + k}: {_cut(t)}" for k, t in enumerate(shown)],
            "more_lines": len(actual) - len(shown),
            "diff": compact_diff([_cut(x) for x in expected],
                                 [_cut(x) for x in actual])}


def _cut(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    return f"{line[:MAX_LINE_CHARS]} ...[+{len(line) - MAX_LINE_CHARS} chars]"


# --------------------------------------------------------------------------- #
# The reply
# --------------------------------------------------------------------------- #
def _hunk_head(entry: dict[str, Any], h: dict[str, Any]) -> str:
    anchor = f" (@@ {h['anchor']})" if h.get("anchor") else ""
    what = ("the @@ context line is not in the file"
            if h["reason"] == "context" else
            f"the expected lines are not in the file after line {h.get('cursor', 0)}"
            if h.get("cursor") else "the expected lines are not in the file")
    return f"hunk {h['index']} of {h['of']}{anchor}: {what}"


def render_reply(result: dict[str, Any], *, max_chars: int = MAX_REPLY_CHARS,
                 tool: str = "apply_patch") -> str:
    """The deny reason for a patch with misses: per failing hunk, the
    nearest region's current text with line numbers, range, key (with the
    node's full range and sha256) and the file's sha256, within
    `max_chars`."""
    misses, total = result["misses"], result["hunks"]
    lines = [f"ozagia checked this patch against the current files: {misses} of "
             f"{total} hunk(s) would fail (\"Failed to find ...\"), so {tool} "
             f"was not run and nothing was written. The current text where each "
             f"failing hunk belongs is below with line numbers; fix the patch "
             f"from it and send the whole patch again (no need to re-read the "
             f"files)."]
    used = len(lines[0])

    def add(block: list[str]) -> bool:
        nonlocal used
        size = sum(len(x) + 1 for x in block)
        if used + size > max_chars:
            return False
        lines.extend(block)
        used += size
        return True

    cut = False
    for entry in result["files"]:
        if entry["status"] in ("missing", "unreadable", "refused"):
            if not add([f"{entry['path']}: {entry['op']} not checked "
                        f"({entry['problem']})"]):
                cut = True
            continue
        bad = [h for h in entry["hunks"] if h["status"] != "ok"]
        if not bad:
            if entry["op"] == "update" and entry["hunks"]:
                add([f"{entry['path']}: all {len(entry['hunks'])} hunk(s) match"])
            continue
        sha = entry.get("sha256") or "(changed earlier in this patch)"
        if not add(["", f"*** {entry['rel']}  sha256 {sha}  "
                        f"({entry['lines']} lines)"]):
            cut = True
            break
        for h in bad:
            head = [_hunk_head(entry, h)]
            if h.get("note"):
                head.append(f"  note: {h['note']}")
            reg = h.get("region")
            if reg is None:
                head.append("  no similar region in the file" if "region" in h
                            else "  (region not shown: region cap)")
                if not add(head):
                    cut = True
                continue
            head.append(f"  nearest region: L{reg['start_line']}-L{reg['end_line']} "
                        f"{reg['key']}  similarity {reg['ratio']:.2f}")
            note = _node_note(reg["start_line"], reg["end_line"], reg.get("node"))
            if note:
                head.append(f"  {note}")
            if reg["diff"]:
                head.append("  differs (-patch expects, +file has):")
                head.extend(f"  {d}" for d in reg["diff"])
            body = [f"  current L{reg['start_line']}-L{reg['end_line']}:",
                    *reg["text"]]
            if reg.get("more_lines"):
                body.append(f"  ... ({reg['more_lines']} more lines in the region)")
            if add(head + body):
                continue
            # Shrink the region text to what fits.
            room = max_chars - used - sum(len(x) + 1 for x in head) - 60
            keep: list[str] = []
            for t in reg["text"]:
                if room - (len(t) + 1) < 0:
                    break
                keep.append(t)
                room -= len(t) + 1
            if keep and add(head + body[:1] + keep
                            + ["  ... (region cut: reply size cap)"]):
                cut = True
                continue
            if not add([f"{_hunk_head(entry, h)}; nearest region "
                        f"L{reg['start_line']}-L{reg['end_line']} {reg['key']} "
                        f"(not shown: reply size cap)"]):
                cut = True
                break
        if cut and used >= max_chars - 80:
            break
    if cut:
        tail = "(reply cut at its size cap; read the remaining regions by key)"
        if used + len(tail) + 1 <= max_chars:
            lines.append(tail)
    return "\n".join(lines)[:max_chars]


def patch_text(tool_input: Any) -> str | None:
    """The patch text of an apply_patch call's input (`command`, `patch`
    or `input`)."""
    if isinstance(tool_input, str):
        return tool_input
    if not isinstance(tool_input, dict):
        return None
    for k in ("command", "patch", "input"):
        v = tool_input.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            # ["apply_patch", "<patch>"] (the local_shell form)
            return v[-1]
    return None


# --------------------------------------------------------------------------- #
# The intent tool and edit misses
# --------------------------------------------------------------------------- #
def handle_patch_check(memory: Any, inp: dict[str, Any]) -> dict[str, Any]:
    """The `patch_check` tool: check `input.patch` (relative to
    `input.workdir`, default the root) with the graph's keys. Never writes.
    Intent `patch_check_ok` or `patch_check_miss` (or `patch_check_unparsed`);
    `meta`: `patch_misses`, `region_replies`."""
    from ozagia.intent.common import IntentError, root_of
    text = patch_text(inp)
    if text is None:
        raise IntentError("patch must be a string")
    root = root_of(memory)
    wd = inp.get("workdir")
    cwd = wd if isinstance(wd, str) and wd and os.path.isdir(wd) else None
    max_chars = inp.get("max_chars")
    res = check_patch(root, text, cwd=cwd, memory=memory,
                      max_chars=int(max_chars) if isinstance(max_chars, int)
                      and max_chars > 200 else MAX_REPLY_CHARS)
    if not res["parsed"]:
        intent, output = "patch_check_unparsed", res["error"]
    elif res["misses"]:
        intent, output = "patch_check_miss", res["reply"]
    else:
        intent, output = "patch_check_ok", "the patch matches the current files"
    return {"intent": intent, "output": output, "keys": res["keys"],
            "meta": {"patch_misses": res["misses"], "patch_hunks": res["hunks"],
                     "region_replies": res["regions"] if res["misses"] else 0}}


_EDIT_FAILED_RE = re.compile(
    r"not found|could not find|couldn't find|no match|did not appear|"
    r"failed to find|unable to find|does not (?:contain|match)|doesn't match",
    re.IGNORECASE)


def edit_miss_lines(memory: Any, inp: dict[str, Any], output: Any, *,
                    builtin_error: bool | None = None,
                    max_chars: int = MAX_REPLY_CHARS // 2) -> list[str]:
    """For a built-in edit that failed because its old text is not in the
    file: `[ozagia] ` lines naming the nearest region (range, key with the
    node's range and sha256, the file's sha256, what differs) and the
    region's current text. [] when the old text is in the file.

    `builtin_error` is the caller's word on whether the edit failed (the
    OpenCode plugin sends `meta.builtin_error: true` from its failure
    path): True means it failed, False that it did not. Without it (hooks
    that only see the output) the output must report a miss
    (`_EDIT_FAILED_RE`) and no edit's `newString` may be in the file: after
    a successful edit the old text is gone and the new text is there, and
    the output may still say "not found" in appended diagnostics."""
    from ozagia.intent.common import read_text, resolve, root_of, tag
    if builtin_error is False or not isinstance(output, str):
        return []
    if builtin_error is None and not _EDIT_FAILED_RE.search(output):
        return []
    olds: list[str] = []
    news: list[str] = []
    if isinstance(inp.get("oldString"), str):
        olds.append(inp["oldString"])
    if isinstance(inp.get("newString"), str):
        news.append(inp["newString"])
    for e in inp.get("edits") or []:
        if isinstance(e, dict) and isinstance(e.get("oldString"), str):
            olds.append(e["oldString"])
        if isinstance(e, dict) and isinstance(e.get("newString"), str):
            news.append(e["newString"])
    path = inp.get("path", inp.get("filePath"))
    if not olds or not isinstance(path, str):
        return []
    root = root_of(memory)
    target = resolve(root, path)
    if not target.path.is_file():
        return []
    try:
        data = target.path.read_bytes()
        content = read_text(target.path)
    except Exception:  # noqa: BLE001 - nothing to show
        return []
    old = next((o for o in olds if o and o not in content
                and o.replace("\n", "\r\n") not in content), None)
    if old is None:
        return []
    if builtin_error is None and any(
            n and (n in content or n.replace("\n", "\r\n") in content)
            for n in news):
        # The new text is in the file: most likely the edit went through.
        return []
    lines = [t for t, _ in split_source(content)]
    expected = [t for t, _ in split_source(old)]
    found = nearest_region(lines, expected)
    sha = hashlib.sha256(data).hexdigest()
    if found is None:
        return [tag(f"oldString is not in {target.rel} and no region resembles "
                    f"it; sha256 {sha}")]
    s, e = found["start"], found["end"]
    try:
        key = region_key(root, target.rel, content, s + 1, e, memory=memory)
    except Exception:  # noqa: BLE001
        key = f"file:{target.rel}"
    out = [tag(f"oldString is not in {target.rel}; nearest region "
               f"L{s + 1}-L{e} {key} (similarity {found['ratio']:.2f}), "
               f"sha256 {sha}")]
    note = _node_note(s + 1, e, node_view(root, target.rel, key, sha, memory))
    if note:
        out.append(tag(note))
    diff = compact_diff([_cut(x) for x in expected], [_cut(x) for x in lines[s:e]])
    if diff:
        out.append(tag("differs (-oldString, +file):"))
        out.extend(tag(d) for d in diff)
    used = sum(len(x) + 1 for x in out)
    shown = 0
    for k, t in enumerate(lines[s:e][:MAX_REGION_LINES]):
        row = f"{s + 1 + k}: {_cut(t)}"
        if used + len(row) + 1 > max_chars:
            break
        out.append(row)
        used += len(row) + 1
        shown += 1
    if shown < e - s:
        out.append(tag(f"... {e - s - shown} more lines in the region"))
    return out


__all__ = ["Chunk", "FileOp", "MAX_REPLY_CHARS", "ParsedPatch", "PatchParseError",
           "check_patch", "compact_diff", "edit_miss_lines", "handle_patch_check",
           "nearest_region", "node_view", "parse_patch", "patch_text", "region_key",
           "render_reply", "seek_sequence", "split_source"]
