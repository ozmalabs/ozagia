"""ozagia.edit.replace — one pattern replaced across files, preview first.

`replace(root, pattern, replacement, ...)` finds every match of a literal or
regular-expression pattern in the files of a scope, and either previews the
change (the default) or writes it through `ozagia.edit.txn`, one file at a
time, each with a backup.

Pattern. A literal string (default), or with `regex=True` a Python `re`
pattern whose replacement may name groups (`\\1`, `\\g<name>`, as
`re.Match.expand`). `flags` takes letters (`"ims"`) or names
(`"IGNORECASE,MULTILINE"`, or a list); they apply to a literal too. A
literal containing newlines that does not occur as given is searched again
with the file's newline style, and a replacement's newlines are written in
the file's style.

Scope. Candidate files are the repository's files, without what
`.gitignore` excludes (`git ls-files -c -o --exclude-standard`; with
`gitignore=False`, or outside a Git work tree, every file under the root).
By default only text files are candidates: a suffix in `TEXT_SUFFIXES` and
a base name that does not start with "." (no `.env`, `.npmrc`, ...);
`any_suffix=True` lifts both. `globs` restrict the candidates (`*` and `?`
stay within a path segment, `**` crosses segments; a glob without `/` also
matches a file's base name). `keys` (`sym:`, `test:`,
`doc:<rel>#<section>`, `text:<rel>#L<a>-L<b>`, `json:`, `file:`; see
`ozagia.edit.key_edit.node_span`) restrict the files to theirs and the
matches to the spans they name; with both, a key's file must match a glob.
Never scanned or written: `.git`, the state directory, symlinks, a path
that resolves elsewhere than where it is listed (a directory symlink on
the way), files over `MAX_FILE_BYTES`, binary or non-UTF-8 files, and paths
outside the root (`skipped` counts them by reason).

Cost. The scan (listing, reading, matching) runs in a worker process
(`ozagia.edit.replace_worker`) with a wall-clock deadline, `timeout_s`
(default `SCAN_TIMEOUT_S`): a pattern that backtracks without end is
abandoned and the call refused (`timed_out`). The scan stops as soon as
more files than `max_files` match or there are more matches than
`max_matches`, and refuses the call (nothing is written) with the counts
so far (`over_cap`); a total that differs from `expect` refuses it too.

Preview result: `{ok, applied: False, total, files: [{file, matches, lines,
keys, base_sha256, new_sha256, diff, diff_truncated, parse_check}],
files_matched, files_scanned, skipped, shas, plan_id}`. `shas` maps each
file the replace changes to the sha256 of its bytes; `plan_id` hashes the
request and `shas`, so it names "this request against these bytes".

Apply (`write=True`). With `plan_id`, any difference from the preview's
plan (the request, a file's bytes, the set of files) refuses the batch.
`base_shas` (the preview's `shas`) is a weaker check on the bytes only: a
file whose sha changed, a changed file the map does not name, or a name
that no longer matches, refuses the batch; it does not bind the request,
so front ends that ask for a preview token require `plan_id`. Before the
first write every file's new text is parsed (`span_edit`'s parse guard:
Python, JSON, Rust and Lua): a file that parsed and would not refuses the
whole batch, unless `allow_parse_errors`. Files are then written in order;
if one fails (it changed between read and write), the files written before
it are restored from their backups when they still hold the replace's
bytes (`restored`), and nothing stays written. Only when a restore fails
does the result say `partial` (with `written` the files left changed and
`restore_failed`). With a `DevMemory` the written files are reingested once
and `graph_delta` and per-file `delta` are returned. `written_matches`
counts the matches in the written files. Journaling is the caller's
(`DevMemory.replace`).
"""
from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator

from ozagia.config import state_rel
from ozagia.edit.code_edit import MAX_SOURCE_BYTES, CodeEditError
from ozagia.edit.txn import (
    SourceFile,
    encode_text,
    in_git_dir,
    normalize_newlines,
    sha256_bytes,
    write_source,
)

MAX_FILE_BYTES = MAX_SOURCE_BYTES
DEFAULT_MAX_FILES = 200
DEFAULT_MAX_MATCHES = 5000
# Wall-clock seconds the scan may take when the caller names no deadline.
SCAN_TIMEOUT_S = 10.0
# The shortest deadline given to a scan (a request already near its end).
MIN_SCAN_TIMEOUT_S = 0.5
# Candidate files read at most in one call.
MAX_SCAN_FILES = 50_000
# Unified diff lines shown per file, and context lines around each change.
DIFF_LINES_PER_FILE = 60
DIFF_CONTEXT = 1
# Match line numbers listed per file, and skipped files named.
LINES_PER_FILE = 50
SKIPPED_NAMED = 20
# Distinct (start, end) line pairs given an enclosing key per file.
KEYED_LINES_PER_FILE = 500


def _text_suffixes() -> frozenset[str]:
    from ozagia.index.text_nodes import STRUCTURED_SUFFIXES
    return STRUCTURED_SUFFIXES | frozenset({
        ".pyi", ".txt", ".rst", ".adoc", ".toml", ".yaml", ".yml", ".cfg",
        ".ini", ".sh", ".bash", ".zsh", ".html", ".htm", ".css", ".scss",
        ".sql", ".xml", ".csv", ".tsv", ".tf", ".hcl", ".mjs", ".cjs", ".rb",
        ".kt", ".swift", ".cs", ".php", ".proto", ".graphql", ".tex", ".vue",
        ".svelte", ".scala", ".pl", ".r", ".jl", ".zig", ".nix"})


# Suffixes of the files in the default scope (see the module docstring).
TEXT_SUFFIXES = _text_suffixes()

_FLAG_LETTERS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL,
                 "x": re.VERBOSE, "a": re.ASCII}
_FLAG_NAMES = {"IGNORECASE": re.IGNORECASE, "MULTILINE": re.MULTILINE,
               "DOTALL": re.DOTALL, "VERBOSE": re.VERBOSE, "ASCII": re.ASCII}


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def parse_flags(flags: Any) -> tuple[int, str]:
    """`re` flags from letters, names or a list of either; and their
    canonical letters (for the plan id)."""
    if flags is None or flags == "" or flags == []:
        return 0, ""
    items: list[str]
    if isinstance(flags, str):
        items = [p for p in re.split(r"[,\s|]+", flags.strip()) if p]
    elif isinstance(flags, (list, tuple)):
        items = [str(p) for p in flags]
    else:
        raise CodeEditError(f"flags must be letters, names or a list: {flags!r}")
    value = 0
    for item in items:
        name = item.upper().removeprefix("RE.")
        if name in _FLAG_NAMES:
            value |= _FLAG_NAMES[name]
            continue
        for ch in item.lower():
            if ch not in _FLAG_LETTERS:
                raise CodeEditError(
                    f"unknown regex flag {item!r}; use letters "
                    f"{''.join(_FLAG_LETTERS)} or {', '.join(_FLAG_NAMES)}")
            value |= _FLAG_LETTERS[ch]
    letters = "".join(ch for ch, f in _FLAG_LETTERS.items() if value & f)
    return value, letters


def _as_list(value: Any, what: str) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return [v for v in value if v.strip()]
    raise CodeEditError(f"{what} must be a string or a list of strings")


def _cap(value: Any, default: int, what: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CodeEditError(f"{what} must be a positive integer")
    return value


def _timeout(value: Any) -> float:
    if value is None:
        return SCAN_TIMEOUT_S
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodeEditError("timeout_s must be a number of seconds")
    return max(float(value), MIN_SCAN_TIMEOUT_S)


def glob_regex(pattern: str) -> re.Pattern[str]:
    """A glob over root-relative POSIX paths as a regex: `*` and `?` within
    one segment, `**` across segments, `[...]` a character class."""
    pat = pattern.strip().removeprefix("./")
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat.startswith("**", i):
                j = i + 2
                if j < n and pat[j] == "/":
                    out.append("(?:.*/)?")
                    i = j + 1
                else:
                    out.append(".*")
                    i = j
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 2 if pat.startswith("[!", i) or
                         pat.startswith("[]", i) else i + 1)
            if j < 0:
                out.append(re.escape(c))
            else:
                body = pat[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _glob_matcher(globs: list[str]):
    if not globs:
        return lambda rel: True
    compiled = [(glob_regex(g), "/" not in g.strip().removeprefix("./"))
                for g in globs]

    def match(rel: str) -> bool:
        base = rel.rsplit("/", 1)[-1]
        return any(rx.match(rel) or (by_name and rx.match(base))
                   for rx, by_name in compiled)
    return match


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def _git_files(root: Path) -> list[str] | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "-c", "-o",
             "--exclude-standard"], capture_output=True, timeout=60,
            stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    names = proc.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return sorted({n for n in names if n})


def _walk_files(root: Path, state: str | None) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(
            d for d in dirnames if d != ".git"
            and not (state and (rel_dir + d) == state)
            and not os.path.islink(os.path.join(dirpath, d)))
        out.extend(rel_dir + f for f in filenames)
    return sorted(out)


def _excluded(rel: str, state: str | None) -> str | None:
    if in_git_dir(rel):
        return ".git"
    if state and (rel == state or rel.startswith(state + "/")):
        return "state dir"
    return None


def _out_of_scope(rel: str, any_suffix: bool) -> str | None:
    """Why `rel` is not in the default text scope, or None."""
    if any_suffix:
        return None
    path = PurePosixPath(rel)
    if path.name.startswith("."):
        return "dotfile"
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return "not a text suffix"
    return None


def _read_file(root: Path, rel: str) -> SourceFile | str:
    """The file, or the reason it is skipped. A path that resolves to a
    different root-relative path (a directory symlink on the way) is
    skipped: exclusions and the permission name the listed path."""
    path = root / rel
    try:
        if path.is_symlink():
            return "symlink"
        resolved = path.resolve()
        rel_resolved = resolved.relative_to(root).as_posix()
    except ValueError:
        return "outside root"
    except OSError:
        return "unreadable"
    if rel_resolved != PurePosixPath(rel).as_posix():
        return "symlinked path"
    try:
        st = resolved.stat()
    except OSError:
        return "missing"
    if not resolved.is_file():
        return "not a file"
    if st.st_size > MAX_FILE_BYTES:
        return "too large"
    try:
        with open(resolved, "rb") as fh:
            raw = fh.read(8192)
    except OSError:
        return "unreadable"
    if b"\0" in raw[:8192]:
        return "binary"
    from ozagia.edit.txn import read_source
    try:
        return read_source(root, rel_resolved,
                           suffixes=(resolved.suffix.lower(),),
                           max_bytes=MAX_FILE_BYTES, error_cls=CodeEditError)
    except CodeEditError as e:
        msg = str(e)
        for needle, why in (("not valid UTF-8", "not UTF-8"),
                            ("lone CR", "lone CR"),
                            ("declares encoding", "not UTF-8"),
                            ("exceeds", "too large")):
            if needle in msg:
                return why
        return "unreadable"


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
class _Matcher:
    def __init__(self, pattern: str, replacement: str, *, regex: bool,
                 flags: int) -> None:
        if not isinstance(pattern, str) or pattern == "":
            raise CodeEditError("pattern must be a non-empty string")
        if not isinstance(replacement, str):
            raise CodeEditError("replacement must be a string")
        self.pattern, self.replacement = pattern, replacement
        self.regex, self.flags = regex, flags
        self._by_newline: dict[str, re.Pattern[str]] = {}
        try:
            self.rx = re.compile(pattern if regex else re.escape(pattern), flags)
        except re.error as e:
            raise CodeEditError(f"bad regex {pattern!r}: {e}") from None
        if regex:
            # A bad group reference fails here, before any file is read.
            try:
                self.rx.sub(replacement, "")
            except (re.error, IndexError) as e:
                raise CodeEditError(f"bad replacement {replacement!r}: {e}") from None

    def finditer(self, text: str, newline: str) -> Iterator[re.Match[str]]:
        """The matches, lazily (a caller may stop at a cap)."""
        found = False
        for m in self.rx.finditer(text):
            found = True
            yield m
        if found or self.regex or "\n" not in self.pattern:
            return
        alt = normalize_newlines(self.pattern, newline)
        if alt == self.pattern:
            return
        if newline not in self._by_newline:
            self._by_newline[newline] = re.compile(re.escape(alt), self.flags)
        yield from self._by_newline[newline].finditer(text)

    def expand(self, m: re.Match[str], newline: str) -> str:
        if not self.regex:
            return normalize_newlines(self.replacement, newline)
        try:
            text = m.expand(self.replacement)
        except (re.error, IndexError) as e:
            raise CodeEditError(f"bad replacement {self.replacement!r}: {e}") from None
        return normalize_newlines(text, newline)


def scan(req: dict[str, Any]) -> dict[str, Any]:
    """The scan of one call (run by `ozagia.edit.replace_worker`): list,
    filter and read the candidates, and match them, stopping at a cap.
    `req` is JSON; so is the result: `{files: [{rel, sha, matches: [[start,
    end, replacement]]}], total, scanned, skipped, skipped_files, notes,
    over_cap, changed}`."""
    root = Path(req["root"])
    state = req.get("state")
    matcher = _Matcher(req["pattern"], req["replacement"],
                       regex=bool(req["regex"]), flags=int(req["flags"]))
    in_glob = _glob_matcher(list(req.get("globs") or []))
    any_suffix = bool(req.get("any_suffix"))
    cap_files, cap_matches = int(req["cap_files"]), int(req["cap_matches"])
    keyed: dict[str, Any] | None = req.get("keys")
    notes: list[str] = []
    if keyed is not None:
        candidates = sorted(keyed)
    else:
        listed = _git_files(root) if req.get("gitignore") else None
        if listed is None:
            if req.get("gitignore"):
                notes.append("not a Git work tree: .gitignore is not applied")
            listed = _walk_files(root, state)
        candidates = listed
    skipped: dict[str, int] = {}
    skipped_files: list[dict[str, str]] = []

    def skip(rel: str, why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1
        if len(skipped_files) < SKIPPED_NAMED:
            skipped_files.append({"file": rel, "reason": why})

    files: list[dict[str, Any]] = []
    changed: list[str] = []
    seen: set[str] = set()
    total = scanned = 0
    over: str | None = None
    for rel in candidates:
        if not in_glob(rel):
            continue
        why = _excluded(rel, state) or (
            None if keyed is not None else _out_of_scope(rel, any_suffix))
        if why:
            skip(rel, why)
            continue
        if scanned >= MAX_SCAN_FILES:
            notes.append(f"stopped after {MAX_SCAN_FILES} files; narrow the globs")
            break
        src = _read_file(root, rel)
        scanned += 1
        if isinstance(src, str):
            skip(rel, src)
            continue
        why = _excluded(src.rel, state)
        if why:
            skip(rel, why)
            continue
        if src.rel in seen:
            continue
        seen.add(src.rel)
        ranges: list[list[int]] | None = None
        if keyed is not None:
            info = keyed[rel]
            if src.sha256 != info["sha"]:
                changed.append(src.rel)
                continue
            ranges = info["spans"]
        hits: list[list[Any]] = []
        for m in matcher.finditer(src.text, src.newline):
            s, e = m.start(), m.end()
            if ranges is not None and not any(a <= s and e <= b
                                              for a, b in ranges):
                continue
            total += 1
            if total > cap_matches:
                over = "matches"
                break
            hits.append([s, e, matcher.expand(m, src.newline)])
        if hits:
            files.append({"rel": src.rel, "sha": src.sha256, "matches": hits})
            if len(files) > cap_files:
                over = over or "files"
        if over:
            break
    return {"files": files, "total": total, "scanned": scanned,
            "skipped": skipped, "skipped_files": skipped_files,
            "notes": notes, "over_cap": over, "changed": changed}


def _run_scan(req: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    """`scan(req)` in a worker process; None when it passes `timeout`
    (the worker is killed)."""
    env = dict(os.environ)
    pkg_parent = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = (pkg_parent + os.pathsep + env["PYTHONPATH"]
                         if env.get("PYTHONPATH") else pkg_parent)
    # Never import from the scanned repository: no cwd/script dir on
    # sys.path (-P, PYTHONSAFEPATH), and a neutral cwd.
    env["PYTHONSAFEPATH"] = "1"
    try:
        proc = subprocess.Popen(
            [sys.executable, "-P", "-m", "ozagia.edit.replace_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, cwd=pkg_parent,
            start_new_session=True)
    except OSError as e:
        raise CodeEditError(f"replace scan could not start: {e}") from None
    try:
        out, err = proc.communicate(json.dumps(req).encode("ascii"),
                                    timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return None
    if proc.returncode != 0:
        tail = err.decode("utf-8", errors="replace").strip()[-600:]
        raise CodeEditError(f"replace scan failed (exit {proc.returncode}): "
                            f"{tail}")
    try:
        data = json.loads(out)
    except ValueError:
        raise CodeEditError("replace scan returned no result") from None
    if data.get("error"):
        raise CodeEditError(str(data["error"]))
    return data


def _diff(rel: str, old: str, new: str) -> tuple[str, bool, int, int]:
    lines = list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=DIFF_CONTEXT))
    adds = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
    dels = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
    shown = lines[:DIFF_LINES_PER_FILE]
    text = "".join(x if x.endswith(("\n", "\r")) else x + "\n" for x in shown)
    return text, len(lines) > DIFF_LINES_PER_FILE, adds, dels


def _parse_status(rel: str, text: str) -> tuple[str, dict[str, Any] | None, Any]:
    from ozagia.edit.span_edit import _parse_error, _parse_py
    if PurePosixPath(rel).suffix.lower() == ".py":
        return _parse_py(text)
    status, err = _parse_error(rel, text)
    return status, err, None


def _key_spans(root: Path, keys: list[str], memory: Any, any_suffix: bool
               ) -> dict[str, dict[str, Any]]:
    """rel -> {sha, spans: [[start, end]]}: offsets into the bytes whose
    sha is given (the scan refuses a file whose sha differs)."""
    from ozagia.edit.key_edit import key_file, node_span
    state = state_rel(root)
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        key = key.strip()
        rel = key_file(key).strip().removeprefix("./")
        why = _excluded(rel, state)
        if why:
            raise CodeEditError(f"{key}: {rel} is inside {why}; refused")
        why = _out_of_scope(rel, any_suffix)
        if why:
            raise CodeEditError(f"{key}: {rel} is out of the default scope "
                                f"({why}); set any_suffix to include it")
        src = _read_file(root, rel)
        if isinstance(src, str):
            raise CodeEditError(f"{key}: {rel} is skipped ({src})")
        span = node_span(src, key, memory=memory)
        entry = out.setdefault(src.rel, {"sha": src.sha256, "spans": []})
        if entry["sha"] != src.sha256:
            raise CodeEditError(f"{key}: {src.rel} changed while its keys "
                                f"were read; retry")
        entry["spans"].append([span["start"], span["end"]])
    return out


def _delta_rel(key: str) -> str:
    kind, _, rest = str(key).partition(":")
    if kind in ("sym", "test"):
        return rest.split("::", 1)[0]
    return rest.split("#", 1)[0]


def plan_id_of(request: dict[str, Any], shas: dict[str, str]) -> str:
    blob = json.dumps({"request": request, "shas": shas}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# The replace
# --------------------------------------------------------------------------- #
def replace(root: str | Path, pattern: str, replacement: str, *,
            regex: bool = False, flags: Any = None, globs: Any = None,
            keys: Any = None, max_files: int | None = None,
            max_matches: int | None = None, expect: int | None = None,
            gitignore: bool = True, any_suffix: bool = False,
            write: bool = False, base_shas: dict[str, str] | None = None,
            plan_id: str | None = None, allow_parse_errors: bool = False,
            timeout_s: float | None = None,
            memory: Any = None) -> dict[str, Any]:
    """Preview (or with `write`, apply) `pattern` -> `replacement` across
    the scope's files. See the module docstring."""
    root_p = Path(root).resolve()
    flag_value, flag_letters = parse_flags(flags)
    glob_list = _as_list(globs, "globs")
    key_list = _as_list(keys, "keys")
    cap_files = _cap(max_files, DEFAULT_MAX_FILES, "max_files")
    cap_matches = _cap(max_matches, DEFAULT_MAX_MATCHES, "max_matches")
    timeout = _timeout(timeout_s)
    if expect is not None and (isinstance(expect, bool)
                               or not isinstance(expect, int) or expect < 0):
        raise CodeEditError("expect must be a non-negative integer")
    if base_shas is not None and not (
            isinstance(base_shas, dict)
            and all(isinstance(k, str) and isinstance(v, str)
                    for k, v in base_shas.items())):
        raise CodeEditError("base_shas must map file paths to sha256 strings")
    # A bad pattern or group reference is refused here, before the worker.
    _Matcher(pattern, replacement, regex=bool(regex), flags=flag_value)
    request = {"pattern": pattern, "replacement": replacement,
               "regex": bool(regex), "flags": flag_letters, "globs": glob_list,
               "keys": key_list, "gitignore": bool(gitignore),
               "any_suffix": bool(any_suffix)}
    keyed = (_key_spans(root_p, key_list, memory, bool(any_suffix))
             if key_list else None)
    result: dict[str, Any] = {
        "ok": True, "applied": False, "pattern": pattern,
        "replacement": replacement, "regex": bool(regex),
        "flags": flag_letters, "globs": glob_list, "keys": key_list,
        "total": 0, "files_matched": 0, "files_changed": 0,
        "files_scanned": 0, "files": [], "skipped": {}, "skipped_files": [],
        "shas": {}, "plan_id": None,
    }

    def refuse(error: str, **extra: Any) -> dict[str, Any]:
        result.update(ok=False, error=error, **extra)
        return result

    found = _run_scan({
        "root": str(root_p), "state": state_rel(root_p), "pattern": pattern,
        "replacement": replacement, "regex": bool(regex), "flags": flag_value,
        "globs": glob_list, "keys": keyed, "gitignore": bool(gitignore),
        "any_suffix": bool(any_suffix), "cap_files": cap_files,
        "cap_matches": cap_matches}, timeout)
    if found is None:
        return refuse(f"the scan took longer than {timeout:g} s (a pattern "
                      f"that backtracks, or a scope too wide); nothing "
                      f"written; narrow the scope or simplify the pattern",
                      timed_out=True)
    result.update(files_scanned=found["scanned"], skipped=found["skipped"],
                  skipped_files=found["skipped_files"])
    if found["notes"]:
        result["notes"] = found["notes"]
    if found["over_cap"] == "files":
        result.update(total=found["total"], files_matched=len(found["files"]))
        return refuse(f"more than {cap_files} files match (max_files); "
                      f"nothing written; narrow the scope or raise max_files",
                      over_cap="files")
    if found["over_cap"] == "matches":
        result.update(total=found["total"], files_matched=len(found["files"]))
        return refuse(f"more than {cap_matches} matches (max_matches); "
                      f"nothing written; narrow the scope or raise "
                      f"max_matches", over_cap="matches")

    files: list[dict[str, Any]] = []
    prepared: list[tuple[SourceFile, str]] = []
    changed_during = list(found["changed"])
    total = 0
    for item in found["files"]:
        src = _read_file(root_p, item["rel"])
        if isinstance(src, str) or src.sha256 != item["sha"]:
            changed_during.append(item["rel"])
            continue
        spans = item["matches"]
        parts: list[str] = []
        cursor = 0
        for s, e, rep in spans:
            parts.append(src.text[cursor:s])
            parts.append(rep)
            cursor = e
        parts.append(src.text[cursor:])
        new_text = "".join(parts)
        total += len(spans)
        if new_text == src.text:
            files.append({"file": src.rel, "matches": len(spans),
                          "unchanged": True, "base_sha256": src.sha256})
            continue
        files.append(_file_entry(root_p, src, spans, new_text))
        prepared.append((src, new_text))

    changed = [f for f in files if not f.get("unchanged")]
    shas = {f["file"]: f["base_sha256"] for f in changed}
    pid = plan_id_of(request, shas)
    result.update(total=total, files_matched=len(files),
                  files_changed=len(changed), files=files, shas=shas,
                  plan_id=pid)
    if changed_during:
        return refuse(f"changed during the scan: "
                      f"{', '.join(sorted(changed_during))}; nothing written; "
                      f"preview again", stale=sorted(changed_during))
    if expect is not None and total != expect:
        return refuse(f"{total} matches, expected {expect}; nothing written",
                      expect_mismatch=True)
    parse_refused = [f for f in changed if f["parse_check"] == "refused"]
    if parse_refused:
        result["parse_refused"] = [f["file"] for f in parse_refused]
    if not write:
        return result

    if base_shas is not None:
        # A named file that no longer changes is stale too (its bytes or
        # the request differ); it is never read from disk.
        stale = sorted(rel for rel, sha in base_shas.items()
                       if shas.get(rel) != sha)
        unnamed = sorted(set(shas) - set(base_shas))
        if stale or unnamed:
            return refuse(
                "stale preview: "
                + "; ".join(p for p in (
                    f"changed since the preview: {', '.join(stale)}"
                    if stale else "",
                    f"matched but not in the preview: {', '.join(unnamed)}"
                    if unnamed else "") if p)
                + "; preview again", stale=stale, unnamed=unnamed)
    if plan_id and str(plan_id) != pid:
        return refuse(f"stale preview: plan {plan_id} is now {pid} (the files "
                      f"or the request changed); preview again", stale=[])
    if parse_refused and not allow_parse_errors:
        where = "; ".join(
            f"{f['file']} line {f['parse_error'].get('line')}: "
            f"{f['parse_error'].get('message')}" for f in parse_refused)
        return refuse(f"the replace breaks the parse of {len(parse_refused)} "
                      f"file(s), nothing written: {where}; set "
                      f"allow_parse_errors to write anyway")
    if not prepared:
        return refuse("no file would change; nothing written")

    written: list[str] = []
    by_file = {f["file"]: f for f in changed}
    done: list[tuple[SourceFile, str]] = []
    for src, new_text in prepared:
        try:
            backup = write_source(root_p, src, new_text, error_cls=CodeEditError)
        except CodeEditError as e:
            restored, failed = _restore(root_p, done, by_file)
            written = [rel for rel in written if rel not in restored]
            for rel in restored:
                by_file[rel]["applied"] = False
                by_file[rel]["restored"] = True
            if failed:
                result.update(
                    ok=False, partial=True, restored=restored,
                    restore_failed=failed,
                    error=(f"{src.rel}: {e}; {len(failed)} file(s) written "
                           f"before it could not be restored (changed since): "
                           f"{', '.join(failed)}"
                           + (f"; restored {', '.join(restored)}"
                              if restored else "")))
            else:
                result.update(
                    ok=False, restored=restored,
                    error=(f"{src.rel}: {e}; "
                           + (f"restored {len(restored)} file(s) written "
                              f"before it from their backups; "
                              if restored else "")
                           + "nothing written"))
            break
        by_file[src.rel]["backup"] = backup
        by_file[src.rel]["applied"] = True
        written.append(src.rel)
        done.append((src, new_text))
    result["applied"] = bool(written)
    result["written"] = written
    result["written_matches"] = sum(by_file[rel]["matches"] for rel in written)
    touched = [*written, *(result.get("restored") or [])]
    if touched and memory is not None:
        for rel in touched:
            try:
                memory._drop_cached(rel)
            except Exception:  # noqa: BLE001 - a cache miss is harmless
                pass
    if written and memory is not None:
        try:
            delta = memory.reingest(written).to_dict()
        except Exception as e:  # noqa: BLE001 - recorded, not raised
            delta = {"error": f"{type(e).__name__}: {e}"}
        result["graph_delta"] = delta
        if not delta.get("error"):
            for rel in written:
                part = {k: [x for x in delta.get(k) or [] if _delta_rel(x) == rel]
                        for k in ("added", "removed", "changed")}
                by_file[rel]["delta"] = part
    result["graph_stale"] = bool(written) and memory is None
    return result


def _restore(root: Path, done: list[tuple[SourceFile, str]],
             by_file: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Put back the files of a failed batch, newest first, each only while
    it still holds the replace's bytes: (restored, not restored)."""
    restored: list[str] = []
    failed: list[str] = []
    for src, new_text in reversed(done):
        new_raw = encode_text(src, new_text)
        now = dataclasses.replace(src, raw=new_raw,
                                  sha256=sha256_bytes(new_raw))
        try:
            write_source(root, now, src.text, error_cls=CodeEditError)
        except (CodeEditError, OSError):
            failed.append(src.rel)
            continue
        restored.append(src.rel)
    return sorted(restored), sorted(failed)


def _file_entry(root: Path, src: SourceFile, spans: list[list[Any]],
                new_text: str) -> dict[str, Any]:
    """The preview entry of one changed file. Line numbers come from one
    forward pass over the matches; keys are computed once per line pair
    (at most `KEYED_LINES_PER_FILE`)."""
    from ozagia.edit.span_edit import _Keyer
    before, _, old_tree = _parse_status(src.rel, src.text)
    after, after_error, _ = _parse_status(src.rel, new_text)
    keyer = _Keyer(root, src.rel, src.text, py_tree=old_tree)
    text = src.text
    keys: dict[str, None] = {}
    lines: dict[int, None] = {}
    keyed: dict[tuple[int, int], str] = {}
    keys_truncated = False
    pos, line = 0, 1
    for s, e, _rep in spans:
        line += text.count("\n", pos, s)
        pos = s
        last = max(e, s + 1) - 1
        el = line + text.count("\n", s, max(last, s))
        pair = (line, el)
        k = keyed.get(pair)
        if k is None:
            if len(keyed) >= KEYED_LINES_PER_FILE:
                keys_truncated = True
            else:
                try:
                    k = keyer.key(s, max(e, s + 1), line, el)["key"]
                except Exception:  # noqa: BLE001 - a key is a label
                    k = f"file:{src.rel}"
                keyed[pair] = k
        if k is not None:
            keys.setdefault(k)
        if len(lines) < LINES_PER_FILE:
            lines.setdefault(line)
    diff, truncated, adds, dels = _diff(src.rel, src.text, new_text)
    if before == "error":
        parse_check = "was_broken"
    elif after == "error":
        parse_check = "refused"
    else:
        parse_check = after
    entry: dict[str, Any] = {
        "file": src.rel, "matches": len(spans), "lines": list(lines),
        "keys": list(keys), "base_sha256": src.sha256,
        "new_sha256": sha256_bytes(encode_text(src, new_text)),
        "additions": adds, "deletions": dels,
        "diff": diff, "diff_truncated": truncated, "parse_check": parse_check,
    }
    if keys_truncated:
        entry["keys_truncated"] = True
    if parse_check == "refused":
        entry["parse_error"] = after_error
    return entry


def changed_texts(root: str | Path, result: dict[str, Any]
                  ) -> Iterable[tuple[str, str]]:
    """(rel, backup text) of each written file of an applied result: the
    text before the replace, for callers that lint the changed lines."""
    for f in result.get("files") or []:
        if f.get("applied") and f.get("backup"):
            try:
                raw = Path(f["backup"]).read_bytes()
            except OSError:
                continue
            yield f["file"], raw.decode("utf-8", errors="replace").lstrip("\ufeff")


__all__ = ["DEFAULT_MAX_FILES", "DEFAULT_MAX_MATCHES", "MAX_FILE_BYTES",
           "SCAN_TIMEOUT_S", "TEXT_SUFFIXES", "changed_texts", "glob_regex",
           "parse_flags", "plan_id_of", "replace", "scan"]
