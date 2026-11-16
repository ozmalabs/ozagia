"""Paths a tool call names for writing, without running the call.

The created-file ledger and the library hints need the files a built-in
`patch` or `shell` call changed.  A harness may report them in
`meta.edited_files`/`meta.changed_paths`; when it does not, the call itself
still names them.  This module derives those paths deterministically from the
patch text or command line.  It runs nothing, reads no target file except a
patch file explicitly named by `git apply`/`patch` (bounded), and makes no
network request.

`command_paths(command, cwd=..., root=...)` recognises the write-target forms
the ledger wants to attribute:

- output redirections: `>`, `>>`, `>|`, `<>`, `&>`, `&>>` and an explicit
  descriptor (`2>`, `10>>`); descriptor duplications (`2>&1`, `>&2`) are not
  targets;
- `tee` (all operands, `-` excluded), `touch`, `mkdir`, `rm`, `rmdir`,
  `truncate`;
- `cp`, `mv`, `install`, `ln` (all path operands, sources and destination);
- `sed -i` and `perl -i` (in-place edit targets only);
- `dd of=FILE`;
- `git apply` and `patch`, whose named patch file (an operand, `-i` or an
  input redirect) is read and whose unified diff file headers give the
  targets.  `git apply -` (stdin) and a call with no patch file operand or
  input redirect give nothing; `--check`, `--stat` and `--cached`/`--index`
  are not write targets.

Compound commands and pipelines are split at the shell operators with
`intent.shell`'s tokenizer.  A simple `cd DIR &&`, `cd DIR;` or `cd DIR` in a
subshell changes the directory for the commands after it.  A shell's `-c`
script is parsed once more.  `python -c`/`node -e`, `xargs`, `eval`, `find
-exec`, `watch`, command substitution and wildcard/brace/tilde words are not
guessed at: a target that is not a literal is skipped, and a command whose
paths depend on a script we do not parse yields nothing.

Paths are resolved against the call's `cwd` (relative to `root` when it is
relative) and the repository `root`, with symlinks resolved.  Absolute paths
outside the root, `..` escapes, `.git`, the state directory (`.ozagia`) and
names with NULs or newlines are rejected.  The result is root-relative POSIX
paths, deduplicated in first-seen order and capped by `limit`.

`patch_paths(inp, ...)` does the same for the apply_patch `patch` tool:
`intent.patch.parse_patch` gives each operation's path and move target.  Only
when there is no parseable patch text does it fall back to explicit path keys
(`path`, `filePath`, `files`, `paths`, ...).  A parse error or unsafe path is
ignored; the function never raises.

Both functions are conservative on purpose: a missed path is recorded by the
session manifest later, while a guessed path would pollute the ledger and the
library hints.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ozagia.intent.patch import parse_patch, patch_text
from ozagia.intent.shell import (
    _GIT_VALUE_OPTS,
    _PYTHON_RE,
    _SHELLS,
    _lex,
    _shell_script,
    _unwrap,
)

MAX_CALL_PATHS = 64
# A hard ceiling even when a caller asks for more.
MAX_PATHS = 256
# A patch file named by `git apply`/`patch` is read only below this size.
MAX_PATCH_BYTES = 512 * 1024
# Parsing one shell `-c` script more than this deep is guessing.
MAX_SCRIPT_DEPTH = 1
# Names that are the shell's standard streams, not repository files.
_SPECIAL = frozenset({"-", "/dev/null", "/dev/stdin", "/dev/stdout",
                      "/dev/stderr"})
# Unquoted words with these characters are shell expansions or globs, not
# literal path names.
_EXPANSION_CHARS = frozenset("$`*?[]{}")
# Options that take a separate value, per command (short options only).
_VALUE_OPTS: dict[str, frozenset[str]] = {
    "touch": frozenset("drt"),
    "mkdir": frozenset("m"),
    "truncate": frozenset("sr"),
    "cp": frozenset("St"),
    "mv": frozenset("St"),
    "install": frozenset("mogtS"),
    "ln": frozenset("St"),
    "tee": frozenset(),
    "rm": frozenset(),
    "rmdir": frozenset(),
}
_LONG_VALUE_OPTS: dict[str, frozenset[str]] = {
    "touch": frozenset({"--date", "--reference", "--time"}),
    "mkdir": frozenset({"--mode", "--context"}),
    "truncate": frozenset({"--size", "--reference"}),
    "cp": frozenset({"--target-directory", "--suffix"}),
    "mv": frozenset({"--target-directory", "--suffix"}),
    "install": frozenset({"--mode", "--owner", "--group",
                          "--target-directory", "--suffix"}),
    "ln": frozenset({"--target-directory", "--suffix"}),
    "tee": frozenset(),
    "rm": frozenset(),
    "rmdir": frozenset(),
}
# Short options that take a value for `git apply`.
_GIT_APPLY_LONG_VALUES = frozenset({
    "--directory", "--include", "--exclude", "--whitespace",
})
# Git apply modes that write the index only or nothing at all.
_GIT_APPLY_NO_WRITE = frozenset({
    "--check", "--stat", "--numstat", "--summary", "--cached", "--index",
})


def command_paths(command: str, *, cwd: str | os.PathLike[str] = ".",
                  root: str | os.PathLike[str] | None = None,
                  limit: int = MAX_CALL_PATHS) -> list[str]:
    """Root-relative paths `command` writes, touches or deletes (see the
    module docstring).  Deterministic and read-only; never runs `command`.
    Returns [] for an empty command, a command that does not tokenise, or any
    parse error."""
    try:
        if not isinstance(command, str) or not command.strip():
            return []
        cap = _cap(limit)
        if cap <= 0:
            return []
        root_p, base = _bases(root, cwd)
        out: list[str] = []
        seen: set[str] = set()
        _command(command, base, root_p, out, seen, cap, 0)
        return out
    except Exception:  # noqa: BLE001 - bookkeeping must fail open
        return []


def patch_paths(inp: Any, *, root: str | os.PathLike[str] | None = None,
                cwd: str | os.PathLike[str] = ".",
                limit: int = MAX_CALL_PATHS) -> list[str]:
    """Root-relative paths the apply_patch `patch` tool names: each parsed
    operation's `path` and `move_to`, else explicit path keys when the patch
    text is absent or unparseable (see the module docstring).  Never raises."""
    try:
        cap = _cap(limit)
        if cap <= 0:
            return []
        root_p, base = _bases(root, cwd)
        out: list[str] = []
        seen: set[str] = set()
        text = patch_text(inp)
        parsed = None
        if isinstance(text, str) and text.strip():
            try:
                parsed = parse_patch(text)
            except Exception:  # noqa: BLE001 - fall back to explicit paths
                parsed = None
        if parsed is not None:
            for op in parsed.ops:
                for raw in (op.path, op.move_to):
                    if isinstance(raw, str):
                        _add(raw, raw, base, root_p, out, seen, cap)
        elif isinstance(inp, dict):
            for raw in _explicit_paths(inp):
                _add(raw, raw, base, root_p, out, seen, cap)
        return out
    except Exception:  # noqa: BLE001 - bookkeeping must fail open
        return []


def _cap(limit: Any) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = MAX_CALL_PATHS
    return max(0, min(n, MAX_PATHS))


def _bases(root: Any, cwd: Any) -> tuple[Path, Path]:
    """(root, working directory) as real absolute paths.  With no root, the
    working directory is the root."""
    if root is None or root == "":
        root_p = Path(os.path.realpath(str(cwd or ".")))
        return root_p, root_p
    root_p = Path(os.path.realpath(str(root)))
    raw = str(cwd or ".")
    p = Path(raw)
    if not p.is_absolute():
        p = root_p / p
    return root_p, Path(os.path.realpath(p))


def _command(command: str, base: Path, root: Path, out: list[str],
             seen: set[str], limit: int, depth: int) -> None:
    items = _items(command)
    if items is None:
        return
    stack: list[Path] = []
    for index, item in enumerate(items):
        if item[0] == "op":
            if item[1] == "(":
                stack.append(base)
            elif item[1] == ")" and stack:
                base = stack.pop()
            continue
        words, redirs = item[1], item[2]
        _segment(words, redirs, base, root, out, seen, limit, depth)
        if len(out) >= limit:
            return
        nxt = items[index + 1][1] if (index + 1 < len(items)
                                      and items[index + 1][0] == "op") else ""
        if nxt in (";", "&&"):
            moved = _cd_target(words, redirs, base)
            if moved is not None:
                base = moved


def _items(command: str) -> list[tuple] | None:
    """Shell tokens as segments and operators: ("seg", words, redirs) keeps
    each simple command's words ((raw, value) pairs) and redirects
    ((operator, target word or None), ...); ("op", operator) is an operator
    between them.  None when the command does not tokenise (a heredoc,
    unbalanced quote)."""
    toks, _why = _lex(command)
    if toks is None:
        return None
    items: list[tuple] = []
    words: list[tuple[str, str]] = []
    redirs: list[tuple[str, tuple[str, str] | None]] = []

    def flush(op: str | None = None) -> None:
        nonlocal words, redirs
        if words or redirs:
            items.append(("seg", words, redirs))
        if op is not None:
            items.append(("op", op))
        words, redirs = [], []

    i = 0
    while i < len(toks):
        kind, raw, val = toks[i]
        if kind == "word":
            words.append((raw, val))
        elif kind == "redir":
            target = None
            if i + 1 < len(toks) and toks[i + 1][0] == "word":
                target = (toks[i + 1][1], toks[i + 1][2])
                i += 1
            redirs.append((raw, target))
        else:
            flush(raw or " ")
        i += 1
    flush()
    return items


def _cd_target(words: list[tuple[str, str]],
               redirs: list[tuple[str, Any]], base: Path) -> Path | None:
    """The directory a `cd DIR` segment moves to, or None.  Only a literal,
    single-operand `cd` counts; a `cd` whose target we cannot resolve returns
    an outside-the-root placeholder so later relative paths are not guessed."""
    if redirs:
        return None
    vals = [val for _raw, val in words]
    i = 0
    while i < len(vals) and (_assignment(vals[i])
                             or vals[i] in ("{", "}", "!", "then", "do",
                                            "else", "elif", "if", "while",
                                            "until", "coproc")):
        i += 1
    args = vals[i:]
    if not args or os.path.basename(args[0]) != "cd":
        return None
    rest = args[1:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    if len(rest) != 1:
        return Path("/")
    raw = words[i + 1][0] if i + 1 < len(words) else rest[0]
    if not _literal(raw, rest[0]) or rest[0] in ("-", "~"):
        return Path("/")
    p = Path(rest[0])
    if not p.is_absolute():
        p = base / p
    return Path(os.path.realpath(p))


def _segment(words: list[tuple[str, str]],
             redirs: list[tuple[str, Any]], base: Path, root: Path,
             out: list[str], seen: set[str], limit: int, depth: int) -> None:
    for op, target in redirs:
        if target is not None and _write_redir(op):
            raw, val = target
            _add(raw, val, base, root, out, seen, limit)
            if len(out) >= limit:
                return
    vals = [val for _raw, val in words]
    if not vals:
        return
    # `git apply < file` / `patch < file`: the patch file is named by an
    # input redirect. A heredoc is not tokenised, so it yields nothing.
    input_patch = next((target[1] for op, target in redirs
                        if target is not None and op in ("<", "0<")
                        and _literal(target[0], target[1])), None)
    commands = _unwrap(vals)
    bases = {os.path.basename(cmd[0]) for cmd in commands if cmd}
    # These run code we do not parse, so any paths they name would be guesses.
    if bases & {"xargs", "eval", "find", "watch"}:
        return
    for cmd in commands:
        if not cmd:
            continue
        base_name = os.path.basename(cmd[0])
        if base_name in _SHELLS:
            script = _shell_script(cmd[1:])
            if script and depth < MAX_SCRIPT_DEPTH:
                _command(script, base, root, out, seen, limit, depth + 1)
        elif _PYTHON_RE.match(base_name) or base_name in (
                "node", "ruby", "php", "lua", "Rscript"):
            continue
        elif base_name == "git":
            _git_paths(cmd, base, root, out, seen, limit, input_patch)
        elif base_name == "patch":
            _patch_command_paths(cmd, base, root, out, seen, limit, input_patch)
        elif base_name == "sed":
            _sed_paths(cmd, base, root, out, seen, limit)
        elif base_name == "perl":
            _perl_paths(cmd, base, root, out, seen, limit)
        elif base_name == "dd":
            _dd_paths(cmd, base, root, out, seen, limit)
        elif base_name in _VALUE_OPTS:
            _operand_paths(cmd, base, root, out, seen, limit)
        if len(out) >= limit:
            return


def _operand_paths(cmd: list[str], base: Path, root: Path, out: list[str],
                   seen: set[str], limit: int) -> None:
    name = os.path.basename(cmd[0])
    for raw, val in _operands(cmd[1:], _VALUE_OPTS[name],
                              _LONG_VALUE_OPTS[name]):
        _add(raw, val, base, root, out, seen, limit)
        if len(out) >= limit:
            return


def _operands(args: list[str], short_values: frozenset[str],
              long_values: frozenset[str] | None = None
              ) -> list[tuple[str, str]]:
    """Non-option operands, skipping the value of an option (attached or the
    next word).  Unknown options are treated as flags; that is conservative
    for the path commands below."""
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(args):
        raw = args[i]
        if raw == "--":
            out.extend((a, a) for a in args[i + 1:])
            break
        if raw.startswith("--"):
            name = raw.split("=", 1)[0]
            if "=" not in raw and long_values and name in long_values:
                i += 2
            else:
                i += 1
            continue
        if raw.startswith("-") and raw != "-":
            j = 1
            consume = False
            while j < len(raw):
                if raw[j] in short_values:
                    consume = j + 1 == len(raw)
                    break
                j += 1
            i += 2 if consume else 1
            continue
        out.append((raw, raw))
        i += 1
    return out


def _write_redir(op: str) -> bool:
    if op in ("&>", "&>>", "<>"):
        return True
    if "&" in op or op.startswith("<"):
        return False
    return op.lstrip("0123456789") in (">", ">>", ">|")


def _literal(raw: str, val: str) -> bool:
    """Whether a token is a literal path name, not an expansion/glob."""
    if not val or val in _SPECIAL:
        return False
    if "\0" in val or "\n" in val or "\r" in val:
        return False
    single = raw.startswith("'") and raw.endswith("'")
    if ("$" in val or "`" in val) and not single:
        return False
    if raw == val:             # unquoted: shell syntax would change the word
        if any(c in _EXPANSION_CHARS for c in val):
            return False
        if val.startswith(("~", "<(", ">(")):
            return False
    return not val.startswith("<(") and not val.startswith(">(")


def _add(raw: str, val: str, base: Path, root: Path, out: list[str],
         seen: set[str], limit: int) -> None:
    if len(out) >= limit or not _literal(raw, val):
        return
    rel = _normalize(val, base, root)
    if rel is None or rel in seen:
        return
    seen.add(rel)
    out.append(rel)


def _normalize(raw: str, base: Path, root: Path) -> str | None:
    if not isinstance(raw, str) or not raw or raw in _SPECIAL:
        return None
    if "\0" in raw or "\n" in raw or "\r" in raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = base / p
    real = Path(os.path.realpath(p))
    try:
        rel = real.relative_to(root).as_posix()
    except ValueError:
        return None
    if rel in ("", "."):
        return None
    parts = rel.split("/")
    if any(part == ".git" or part == ".ozagia" for part in parts):
        return None
    return rel


def _assignment(word: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word))


def _explicit_paths(inp: dict[str, Any]) -> list[str]:
    raw: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            raw.append(value)
        elif isinstance(value, list):
            raw.extend(v for v in value if isinstance(v, str) and v)

    for key in ("path", "filePath", "file_path", "file"):
        add(inp.get(key))
    for key in ("files", "paths", "edited_files", "changed_paths"):
        add(inp.get(key))
    return [p for p in raw
            if not p.startswith(("file:", "sym:", "doc:", "json:", "text:",
                                 "test:", "mod:", "class:", "func:"))]


# --------------------------------------------------------------------------- #
# commands with their own option grammar
# --------------------------------------------------------------------------- #
def _sed_paths(cmd: list[str], base: Path, root: Path, out: list[str],
               seen: set[str], limit: int) -> None:
    inplace = False
    script = False
    files: list[str] = []
    args = cmd[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            files.extend(args[i + 1:])
            break
        if a.startswith("--"):
            name, eq, _value = a.partition("=")
            if name == "--in-place":
                inplace = True
                i += 1
                continue
            if name in ("--expression", "--file"):
                script = True
                i += 2 if not eq else 1
                continue
            if name == "--line-length" and not eq:
                i += 2
                continue
            i += 1
            continue
        if a.startswith("-") and a != "-":
            j = 1
            consume = False
            while j < len(a):
                c = a[j]
                if c == "i":
                    inplace = True
                elif c in "efl":
                    script = True
                    consume = j + 1 == len(a)
                    break
                j += 1
            i += 2 if consume else 1
            continue
        if not script:
            script = True
        else:
            files.append(a)
        i += 1
    if inplace:
        for name in files:
            _add(name, name, base, root, out, seen, limit)
            if len(out) >= limit:
                return


def _perl_paths(cmd: list[str], base: Path, root: Path, out: list[str],
                seen: set[str], limit: int) -> None:
    inplace = False
    script = False
    files: list[str] = []
    args = cmd[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            files.extend(args[i + 1:])
            break
        if a.startswith("-") and a != "-":
            j = 1
            consume = False
            while j < len(a):
                c = a[j]
                if c == "i":
                    inplace = True
                elif c in "eE":
                    script = True
                    consume = j + 1 == len(a)
                    break
                elif c in "IMm0lDC":
                    consume = j + 1 == len(a)
                    break
                j += 1
            i += 2 if consume else 1
            continue
        if not script:
            script = True
        else:
            files.append(a)
        i += 1
    if inplace:
        for name in files:
            _add(name, name, base, root, out, seen, limit)
            if len(out) >= limit:
                return


def _dd_paths(cmd: list[str], base: Path, root: Path, out: list[str],
              seen: set[str], limit: int) -> None:
    for arg in cmd[1:]:
        if arg.startswith("of="):
            value = arg[3:]
            _add(value, value, base, root, out, seen, limit)
            if len(out) >= limit:
                return


def _git_paths(cmd: list[str], base: Path, root: Path, out: list[str],
               seen: set[str], limit: int,
               input_patch: str | None = None) -> None:
    i = 1
    cwd = base
    while i < len(cmd) and cmd[i].startswith("-"):
        opt = cmd[i]
        if opt == "-C" and i + 1 < len(cmd):
            p = Path(cmd[i + 1])
            cwd = Path(os.path.realpath(p if p.is_absolute() else base / p))
            i += 2
            continue
        i += 2 if opt in _GIT_VALUE_OPTS else 1
    if i >= len(cmd) or cmd[i] != "apply":
        return
    _git_apply_paths(cmd[i + 1:], cwd, root, out, seen, limit, input_patch)


def _git_apply_paths(args: list[str], base: Path, root: Path, out: list[str],
                     seen: set[str], limit: int,
                     input_patch: str | None = None) -> None:
    strip = 1
    directory = ""
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positional.extend(args[i + 1:])
            break
        if a.split("=", 1)[0] in _GIT_APPLY_NO_WRITE:
            return
        if a in ("-p", "-C") or a in _GIT_APPLY_LONG_VALUES:
            if a == "-p" and i + 1 < len(args) and args[i + 1].isdigit():
                strip = int(args[i + 1])
            if a == "--directory" and i + 1 < len(args):
                directory = args[i + 1]
            i += 2
            continue
        if a.startswith("-p") and a[2:].isdigit():
            strip = int(a[2:])
            i += 1
            continue
        if a.startswith("--directory="):
            directory = a.split("=", 1)[1]
            i += 1
            continue
        if a.startswith("-"):
            i += 1
            continue
        positional.append(a)
        i += 1
    patch = positional[0] if positional else input_patch
    if not patch or patch == "-":
        return
    text = _read_patch(root, base, patch)
    if not text:
        return
    patch_base = root / directory if directory else root
    for rel in _diff_paths(text, strip=strip):
        _add(rel, rel, patch_base, root, out, seen, limit)
        if len(out) >= limit:
            return


def _patch_command_paths(cmd: list[str], base: Path, root: Path, out: list[str],
                 seen: set[str], limit: int,
                 input_patch: str | None = None) -> None:
    strip = 0
    directory = ""
    patch: str | None = None
    output: str | None = None
    positional: list[str] = []
    args = cmd[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positional.extend(args[i + 1:])
            break
        if a in ("--dry-run", "--check"):
            return
        if a in ("-p", "-d", "-i", "-o", "-D", "-V", "-B", "-Y", "-Z",
                 "-g", "-r"):
            value = args[i + 1] if i + 1 < len(args) else ""
            if a == "-p" and value.isdigit():
                strip = int(value)
            elif a == "-d":
                directory = value
            elif a == "-i":
                patch = value
            elif a == "-o":
                output = value
            i += 2
            continue
        if a in ("--input", "--directory", "--output", "--reject-file",
                 "--quoting-style"):
            value = args[i + 1] if i + 1 < len(args) else ""
            if a == "--input":
                patch = value
            elif a == "--directory":
                directory = value
            elif a == "--output":
                output = value
            i += 2
            continue
        if a.startswith("-p") and a[2:].isdigit():
            strip = int(a[2:])
            i += 1
            continue
        if a.startswith("--input="):
            patch = a.split("=", 1)[1]
        elif a.startswith("--directory="):
            directory = a.split("=", 1)[1]
        elif a.startswith("--output="):
            output = a.split("=", 1)[1]
        elif a.startswith("-"):
            pass
        else:
            positional.append(a)
        i += 1
    if output:
        _add(output, output, base, root, out, seen, limit)
    if patch is None and positional:
        patch = positional[0]
    if patch is None:
        patch = input_patch
    if not patch or patch == "-":
        return
    text = _read_patch(root, base, patch)
    if not text:
        return
    target_base = base / directory if directory else base
    for rel in _diff_paths(text, strip=strip):
        _add(rel, rel, target_base, root, out, seen, limit)
        if len(out) >= limit:
            return


def _read_patch(root: Path, base: Path, raw: str) -> str | None:
    """A patch file's text, bounded and inside the root; None otherwise."""
    if (not isinstance(raw, str) or not raw or "\0" in raw or "\n" in raw
            or "\r" in raw or any(c in "$`*?[]{}" for c in raw)
            or raw.startswith("~")):
        return None
    rel = _normalize(raw, base, root)
    if rel is None:
        return None
    try:
        with (root / rel).open("rb") as handle:
            data = handle.read(MAX_PATCH_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_PATCH_BYTES or b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# unified diff headers
# --------------------------------------------------------------------------- #
_DIFF_GIT_RE = re.compile(r"^diff --git (\S+) (\S+)$")
_DIFF_QUOTED_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"')


def _diff_paths(text: str, *, strip: int = 0) -> list[str]:
    """File paths named by a unified diff's headers, with the first `strip`
    components removed.  `diff --git`, `---`/`+++`, rename/copy and new-file
    headers are read; hunk content is not."""
    out: list[str] = []
    old: str | None = None
    new: str | None = None

    def add(path: str | None) -> None:
        if not path:
            return
        stripped = _strip_components(path, strip)
        if stripped and stripped not in out:
            out.append(stripped)

    for line in text.splitlines():
        if line.startswith("diff --git "):
            add(old)
            add(new)
            old = new = None
            m = _DIFF_GIT_RE.match(line.rstrip())
            if m:
                old = _header_path(m.group(1))
                new = _header_path(m.group(2))
            continue
        if line.startswith("--- "):
            old = _header_path(line[4:])
            continue
        if line.startswith("+++ "):
            new = _header_path(line[4:])
            add(old)
            if new != old:
                add(new)
            continue
        if line.startswith(("rename from ", "copy from ")):
            old = _header_path(line.split(" ", 2)[2])
            add(old)
            continue
        if line.startswith(("rename to ", "copy to ")):
            new = _header_path(line.split(" ", 2)[2])
            if new:
                add(new)
            continue
        if line.startswith(("new file mode ", "deleted file mode ")):
            continue
    add(old)
    add(new)
    return out


def _header_path(value: str) -> str | None:
    value = value.strip()
    if not value or value == "/dev/null":
        return None
    if value.startswith('"'):
        m = _DIFF_QUOTED_RE.match(value)
        if m:
            value = m.group(1).replace('\\"', '"').replace("\\\\", "\\")
        else:
            return None
    else:
        value = value.split("\t", 1)[0]
    return value or None


def _strip_components(path: str, strip: int) -> str | None:
    if path.startswith(("/", "\\")):
        return None
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    if strip > 0:
        if len(parts) <= strip:
            return None
        parts = parts[strip:]
    return "/".join(parts)


__all__ = ["MAX_CALL_PATHS", "MAX_PATHS", "command_paths", "patch_paths"]
