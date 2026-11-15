"""Shell command classification for the intent layer.

`classify(command)` parses one shell command line with `shlex` and says what
the intent layer may do with it. It is a pure function: it reads no files and
runs nothing. Only simple commands are mapped. A command with an operator
(`|`, `;`, `&&`, `||`, `&`, a redirect, parentheses), a command or variable
substitution, an unquoted glob character, a newline or an environment
assignment prefix is `complex` and runs as-is, except that a ref-moving git
command anywhere in its operator-separated segments is still refused.

Classes (`result["class"]`):

    pytest        pytest / py.test / python[3] -m pytest / <path>/python -m
                  pytest. Fields: `python` (the interpreter token, or None
                  for a pytest script), `script` (the pytest script token),
                  `selectors`, `extra_args`.
    read          cat F | head [-n N] F | tail [-n N|+N] F | sed -n 'A[,B]p' F.
                  Fields: `file`, `range` (`("all",)`, `("head", n)`,
                  `("tail", n)`, `("from", n)`, `("lines", a, b|None)`).
    grep          grep [-rRnHiFE] [--include G] PAT [PATH], rg [-nHiF]
                  [-g G] PAT [PATH], git grep [-niFE] PAT [PATH]. Fields:
                  `pattern`, `path` (None: the working directory), `include`,
                  `literal`, `ignore_case`, `recursive`, `tool`.
    git_refs      a git command that moves refs (commit, push, stash, ...).
                  Fields: `subcommand`. Listing forms (`git branch`, `git
                  tag -l`, `git stash list`, `git worktree list`) are
                  `git_read`.
    git_read      a read-only git command (diff, show, status, log, blame,
                  and the listing forms above). Fields: `subcommand`.
    complex       not a simple command (see above).
    other         anything else (including wc, nl, find, ls, sed -i,
                  python -c, scripts).

Every result has `class` and `reason` (why it is not mapped, or "").
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Any

GIT_REF_MOVING = frozenset({
    "commit", "push", "stash", "branch", "checkout", "switch", "reset",
    "rebase", "merge", "tag", "worktree", "cherry-pick", "revert", "am",
    "pull", "fetch", "update-ref", "symbolic-ref", "filter-branch",
    "filter-repo", "replace", "notes",
})
GIT_READ_ONLY = frozenset({"diff", "show", "status", "log", "blame"})
# git global options that take a separate value.
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree",
                             "--namespace", "--exec-path"})

_PYTHON_RE = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SED_RE = re.compile(r"^(\d+)(?:,(\d+|\$))?p$")
_OPERATOR_CHARS = set(";&|<>()")
# An unquoted glob in `--include=*.py` only expands if a file of that name
# exists; it is taken literally.
_GLOB_OPTION_WORDS = ("--include=", "--glob=")

# pytest options that take a separate value.
_PYTEST_VALUE_OPTS = frozenset({
    "-k", "-m", "-p", "-o", "-c", "-n", "-W", "-r",
    "--maxfail", "--tb", "--deselect", "--ignore", "--ignore-glob",
    "--rootdir", "--basetemp", "--durations", "--durations-min",
    "--numprocesses", "--timeout", "--log-level", "--log-cli-level",
    "--log-format", "--import-mode", "--confcutdir", "--override-ini",
    "--cov", "--cov-report", "--color", "--capture", "--dist",
    "--junit-prefix", "--pythonwarnings", "--maxprocesses",
})
# Options whose value is a path relative to the working directory.
_PYTEST_PATH_OPTS = frozenset({"-c", "--rootdir", "--ignore", "--ignore-glob",
                               "--deselect", "--basetemp", "--confcutdir"})
# pytest options without a value that the mapping understands.
_PYTEST_FLAGS = frozenset({
    "-q", "-v", "-x", "-s", "-l", "--quiet", "--verbose", "--exitfirst",
    "--showlocals", "--no-header", "--no-summary", "--disable-warnings",
    "--strict-markers", "--strict-config", "--strict", "--runxfail",
    "--co", "--collect-only", "--setup-show", "--full-trace",
    "--no-cov", "--capture=no", "--doctest-modules",
})
# Options the mapping refuses: they need the cache, a terminal, or they
# replace the junit report.
_PYTEST_REFUSED = frozenset({
    "--lf", "--last-failed", "--ff", "--failed-first", "--nf", "--new-first",
    "--sw", "--stepwise", "--sw-skip", "--stepwise-skip", "--cache-show",
    "--cache-clear", "--pdb", "--trace", "--junitxml", "--junit-xml",
    "-h", "--help", "--version", "-V", "--fixtures", "--markers",
    "--funcargs", "--fixtures-per-test",
})
_PYTEST_CLUSTER = set("vqxsl")


def _result(cls: str, reason: str = "", **fields: Any) -> dict[str, Any]:
    return {"class": cls, "reason": reason, **fields}


def _expands(command: str, i: int) -> bool:
    """`$` at `i` starts a parameter, command or arithmetic expansion
    (`$name`, `${..}`, `$(..)`, `$1`, `$?`, ...); a `$` before anything else
    is a literal dollar sign."""
    nxt = command[i + 1:i + 2]
    return bool(nxt) and (nxt.isalnum() or nxt in "_{(@*#?$!-")


def _scan(command: str) -> str | None:
    """Why `command` is not a simple command, from its raw text, or None."""
    if "\n" in command or "\r" in command:
        return "multiple lines"
    quote = ""
    i = 0
    word_start = True
    word = ""
    while i < len(command):
        c = command[i]
        if word_start:
            word = ""
        word += c
        if quote == "'":
            if c == "'":
                quote = ""
        elif quote == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                quote = ""
            elif c == "`" or (c == "$" and _expands(command, i)):
                return "substitution"
        else:
            if c == "\\":
                i += 2
                word_start = False
                continue
            if c in "'\"":
                quote = c
            elif c == "`" or (c == "$" and _expands(command, i)):
                return "substitution"
            elif c in _OPERATOR_CHARS:
                return "operator"
            elif c in "*?[{" and not word.startswith(_GLOB_OPTION_WORDS):
                return "unquoted glob or brace"
            elif c == "~" and word_start:
                return "tilde expansion"
        word_start = quote == "" and c in " \t"
        i += 1
    if quote:
        return "unbalanced quote"
    return None


def _segments(command: str) -> list[list[str]] | None:
    """The command split at shell operators, each segment tokenised."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    out: list[list[str]] = [[]]
    for tok in tokens:
        if tok and set(tok) <= _OPERATOR_CHARS:
            out.append([])
        else:
            out[-1].append(tok)
    return [seg for seg in out if seg]


def _git(tokens: list[str]) -> dict[str, Any]:
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        opt = tokens[i]
        i += 2 if opt in _GIT_VALUE_OPTS else 1
    if i >= len(tokens):
        return _result("other", "git without a subcommand")
    sub, rest = tokens[i], tokens[i + 1:]
    if sub == "grep" and i == 1:
        return _grep(tokens[2:], tool="git grep")
    if sub in GIT_READ_ONLY:
        return _result("git_read", subcommand=sub, args=rest)
    if sub in GIT_REF_MOVING:
        if _git_listing(sub, rest):
            return _result("git_read", subcommand=sub, args=rest)
        return _result("git_refs", subcommand=sub)
    return _result("other", f"git {sub} is not mapped", subcommand=sub)


def _git_listing(sub: str, rest: list[str]) -> bool:
    """`git branch`, `git tag -l`, `git stash list`, ... only list refs."""
    if sub == "branch":
        listing = {"-a", "-r", "-v", "-vv", "-l", "--list", "--all",
                   "--remotes", "--show-current", "--verbose"}
        return all(a in listing for a in rest)
    if sub == "tag":
        return not rest or (rest[0] in ("-l", "--list") and len(rest) <= 2)
    if sub == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    if sub == "worktree":
        return bool(rest) and rest[0] == "list"
    return False


def _pytest(tokens: list[str], *, python: str | None,
            script: str | None) -> dict[str, Any]:
    selectors: list[str] = []
    extra: list[str] = []
    path_opts = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        name = tok.split("=", 1)[0]
        if name in _PYTEST_REFUSED:
            return _result("other", f"pytest option {name} is not mapped")
        if tok == "--":
            selectors.extend(tokens[i + 1:])
            break
        if not tok.startswith("-") or tok == "-":
            selectors.append(tok)
            i += 1
            continue
        if tok in _PYTEST_VALUE_OPTS:
            if i + 1 >= len(tokens):
                return _result("other", f"pytest option {tok} needs a value")
            extra.extend(tokens[i:i + 2])
            path_opts |= tok in _PYTEST_PATH_OPTS
            i += 2
            continue
        if "=" in tok and tok.startswith("--"):
            extra.append(tok)
            path_opts |= name in _PYTEST_PATH_OPTS
        elif tok in _PYTEST_FLAGS:
            extra.append(tok)
        elif not tok.startswith("--") and len(tok) > 2 and tok[:2] in (
                "-k", "-m", "-p", "-o", "-n", "-W", "-r", "-c"):
            extra.append(tok)          # value attached: -ktest_x, -rA
            path_opts |= tok[:2] == "-c"
        elif not tok.startswith("--") and set(tok[1:]) <= _PYTEST_CLUSTER:
            extra.append(tok)          # -xvs
        elif tok.startswith("--") and (i + 1 >= len(tokens)
                                       or tokens[i + 1].startswith("-")):
            extra.append(tok)          # an unknown flag without a value
        else:
            return _result("other", f"pytest option {tok} is ambiguous")
        i += 1
    return _result("pytest", python=python, script=script,
                   selectors=selectors, extra_args=extra,
                   path_options=path_opts)


def _positive(text: str) -> int | None:
    return int(text) if text.isdigit() and int(text) > 0 else None


def _head_tail(cmd: str, args: list[str]) -> dict[str, Any]:
    n: int | None = 10
    plus = False
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            files.extend(args[i + 1:])
            break
        value = None
        if a == "-n" and i + 1 < len(args):
            value, i = args[i + 1], i + 1
        elif a.startswith("--lines="):
            value = a.split("=", 1)[1]
        elif a.startswith("-n") and len(a) > 2:
            value = a[2:]
        elif a.startswith("-") and a[1:].isdigit():
            value = a[1:]
        elif a.startswith("-") and a != "-":
            return _result("other", f"{cmd} option {a} is not mapped")
        else:
            files.append(a)
        if value is not None:
            if cmd == "tail" and value.startswith("+"):
                plus, value = True, value[1:]
            n = _positive(value)
            if n is None:
                return _result("other", f"{cmd} count {value!r} is not mapped")
        i += 1
    if len(files) != 1:
        return _result("other", f"{cmd} of {len(files)} files is not mapped")
    if cmd == "head":
        rng: tuple = ("head", n)
    else:
        rng = ("from", n) if plus else ("tail", n)
    return _result("read", file=files[0], range=rng, tool=cmd)


def _read_like(tokens: list[str]) -> dict[str, Any] | None:
    cmd, args = tokens[0], tokens[1:]
    if cmd == "cat":
        if len(args) == 1 and not args[0].startswith("-"):
            return _result("read", file=args[0], range=("all",), tool="cat")
        return _result("other", "cat with options or several files")
    if cmd in ("head", "tail"):
        return _head_tail(cmd, args)
    if cmd == "sed":
        if len(args) == 3 and args[0] == "-n":
            m = _SED_RE.match(args[1])
            if m and not args[2].startswith("-"):
                a = int(m.group(1))
                b = m.group(2)
                end = None if b == "$" else int(b) if b else a
                if a >= 1 and (end is None or end >= a):
                    return _result("read", file=args[2],
                                   range=("lines", a, end), tool="sed")
        return _result("other", "sed form is not mapped")
    if cmd in ("wc", "nl"):
        return _result("other", f"{cmd} is not mapped")
    return None


def _bre_safe(pattern: str) -> bool:
    """A grep basic regex that means the same as a Rust regex."""
    return not any(c in pattern for c in "\\+?|{}()")


def _ere_safe(pattern: str) -> bool:
    return not re.search(r"\\[<>0-9`']", pattern)


def _grep(args: list[str], *, tool: str) -> dict[str, Any]:
    flags: set[str] = set()
    pattern: str | None = None
    include: str | None = None
    positional: list[str] = []
    allowed = {"grep": set("rRnHiFE"), "rg": set("nHiF"),
               "git grep": set("niFE")}[tool]
    longs = {"--recursive": "r", "--line-number": "n", "--ignore-case": "i",
             "--fixed-strings": "F", "--extended-regexp": "E",
             "--with-filename": "H"}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positional.extend(args[i + 1:])
            break
        if a == "-e" and i + 1 < len(args) and pattern is None:
            pattern, i = args[i + 1], i + 2
            continue
        glob_opt = {"grep": ("--include",), "rg": ("-g", "--glob"),
                    "git grep": ()}[tool]
        name, eq, value = a.partition("=")
        if name in glob_opt and include is None:
            if eq:
                include = value
            elif i + 1 < len(args):
                include, i = args[i + 1], i + 1
            else:
                return _result("other", f"{a} needs a value")
            if include.startswith("!"):
                return _result("other", "negated glob is not mapped")
            i += 1
            continue
        if a in longs and longs[a] in allowed:
            flags.add(longs[a])
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            if not set(a[1:]) <= allowed:
                return _result("other", f"{tool} option {a} is not mapped")
            flags |= set(a[1:])
        elif a.startswith("-") and a != "-":
            return _result("other", f"{tool} option {a} is not mapped")
        else:
            positional.append(a)
        i += 1
    if pattern is None:
        if not positional:
            return _result("other", f"{tool} without a pattern")
        pattern, positional = positional[0], positional[1:]
    if len(positional) > 1:
        return _result("other", f"{tool} over several paths is not mapped")
    literal = "F" in flags
    if not literal and tool != "rg":
        safe = _ere_safe(pattern) if "E" in flags else _bre_safe(pattern)
        if not safe:
            return _result("other", "regex dialect differs from ripgrep")
    recursive = tool != "grep" or bool(flags & {"r", "R"})
    path = positional[0] if positional else None
    if path is None and not recursive:
        return _result("other", "grep without a path reads stdin")
    return _result("grep", pattern=pattern, path=path, include=include,
                   literal=literal, ignore_case="i" in flags,
                   recursive=recursive, tool=tool)


def _simple(tokens: list[str]) -> dict[str, Any]:
    cmd = tokens[0]
    base = os.path.basename(cmd)
    if base == "git" and cmd == "git":
        return _git(tokens)
    if base in ("pytest", "py.test"):
        return _pytest(tokens[1:], python=None, script=cmd)
    if _PYTHON_RE.match(base):
        if len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] == "pytest":
            return _pytest(tokens[3:], python=cmd, script=None)
        return _result("other", "python command")
    if cmd in ("grep", "rg"):
        return _grep(tokens[1:], tool=cmd)
    read = _read_like(tokens)
    if read is not None:
        return read
    if cmd in ("find", "ls"):
        return _result("other", f"{cmd} is left to the built-in")
    return _result("other", f"{cmd} is not mapped")


def classify(command: str) -> dict[str, Any]:
    """The class of a shell command line (see the module docstring)."""
    if not isinstance(command, str) or not command.strip():
        return _result("other", "empty command")
    segments = _segments(command)
    if segments is None:
        return _result("complex", "does not tokenise")
    why = _scan(command)
    if why is None and len(segments) != 1:
        why = "operator"
    if why is not None:
        for seg in segments:
            if seg and seg[0] == "git":
                got = _git(seg)
                if got["class"] == "git_refs":
                    return _result("git_refs", f"compound command: {why}",
                                   subcommand=got["subcommand"])
        return _result("complex", why)
    tokens = segments[0]
    if _ENV_ASSIGN_RE.match(tokens[0]):
        return _result("complex", "environment assignment prefix")
    return _simple(tokens)


__all__ = ["GIT_READ_ONLY", "GIT_REF_MOVING", "classify"]
