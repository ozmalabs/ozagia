"""Shell command classification for the intent layer.

`classify(command)` parses one shell command line with `shlex` and says what
the intent layer may do with it. It is a pure function: it reads no files and
runs nothing. Only simple commands are mapped. A command with an operator
(`|`, `;`, `&&`, `||`, `&`, a redirect, parentheses), a command or variable
substitution, an unquoted glob character, a newline or an environment
assignment prefix is `complex` and runs as-is, except that a ref-moving git
command anywhere in it is still refused: in any operator- or
newline-separated segment, after `VAR=value` prefixes, as `/path/to/git`,
through `git -c alias.<x>=<ref-moving subcommand>`, inside a backtick or
`$(...)` substitution, or inside `sh|bash|zsh -c '<script>'` (the script is
classified once more; a script nested deeper is refused when it names `git`
and then a ref-moving subcommand).

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
    git_refs      a git command that moves refs (`GIT_REF_MOVING`: commit,
                  push, stash, bisect, ...). Fields: `subcommand` (the
                  alias name for an alias, "git" for a deeply nested
                  script). Listing forms (`git branch`, `git
                  tag -l`, `git stash list`, `git worktree list`, `git
                  bisect log`) are `git_read`.
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

# The one list of ref-moving git subcommands: the intent layer refuses them,
# the Claude Code hook imports it, and `eval.agent.OPENCODE_GIT_DENY` is
# generated from it.
GIT_REF_MOVING = frozenset({
    "commit", "push", "stash", "branch", "checkout", "switch", "reset",
    "rebase", "merge", "tag", "worktree", "cherry-pick", "revert", "am",
    "pull", "fetch", "update-ref", "symbolic-ref", "filter-branch",
    "filter-repo", "replace", "notes", "bisect",
})
GIT_READ_ONLY = frozenset({"diff", "show", "status", "log", "blame"})
# git global options that take a separate value.
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree",
                             "--namespace", "--exec-path", "--super-prefix",
                             "--config-env"})
# Shells whose `-c '<script>'` is classified too (one level deep).
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish",
                     "csh", "tcsh"})
# Commands whose `-c <script>` (or `--command <script>`) is a shell script.
_SCRIPT_OPTION = frozenset({"su", "script", "flock", "runuser"})

_PYTHON_RE = re.compile(r"^python(?:3(?:\.\d+)?)?$")
# A trailing `2>&1`, `| tail -N`, `| head -N` (or both) after a command: an
# output filter, not a pipeline that changes what runs.
_OUTPUT_FILTER_RE = re.compile(
    r"^(?P<cmd>.*?)\s*(?P<redir>2>&1)?\s*"
    r"(?:\|\s*(?P<tool>tail|head)\s+(?:-n\s*)?-?(?P<n>\d+))?\s*$")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SED_RE = re.compile(r"^(\d+)(?:,(\d+|\$))?p$")
_OPERATOR_CHARS = set(";&|<>()")
# Characters that split a command line into segments for the git check.
_SEGMENT_CHARS = "();<>|&\n"
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
    """The command split at shell operators and newlines, each segment
    tokenised (`_lex`: quotes, escapes and `#` comments as bash reads them;
    redirect targets left out). A command `_lex` cannot read (a heredoc)
    is split with `shlex` instead, which also splits inside the heredoc."""
    toks, _why = _lex(command)
    if toks is not None:
        out: list[list[str]] = [[]]
        skip = False
        for kind, _raw, val in toks:
            if skip:
                skip = False
                continue
            if kind == "word":
                out[-1].append(val)
            elif kind == "redir":
                skip = True
            else:
                out.append([])
        return [seg for seg in out if seg]
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=_SEGMENT_CHARS)
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None
    out: list[list[str]] = [[]]
    for tok in tokens:
        if tok and set(tok) <= set(_SEGMENT_CHARS):
            out.append([])
        else:
            out[-1].append(tok)
    return [seg for seg in out if seg]


def _substitutions(command: str) -> list[str]:
    """The bodies of the backtick and `$(...)` substitutions in `command`
    (outside single quotes), outermost first."""
    out: list[str] = []
    quote = ""
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = ""
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote == '"' else '"'
        elif c == "`":
            end = command.find("`", i + 1)
            end = n if end < 0 else end
            out.append(command[i + 1:end])
            i = end + 1
            continue
        elif c == "$" and command[i + 1:i + 2] == "(":
            depth, j = 1, i + 2
            while j < n and depth:
                if command[j] == "(":
                    depth += 1
                elif command[j] == ")":
                    depth -= 1
                j += 1
            out.append(command[i + 2:j - 1 if depth == 0 else j])
            i = j
            continue
        i += 1
    return out


def _shell_script(args: list[str]) -> str | None:
    """The script of `sh|bash|zsh [-opts] -c '<script>' ...`, or None."""
    has_c = False
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] not in ("-", "--"):
        if not args[i].startswith("--") and "c" in args[i][1:]:
            has_c = True
        i += 1
    if has_c and i < len(args):
        return args[i]
    return None


def _option_script(args: list[str]) -> str | None:
    """The `-c <script>` / `--command[=]<script>` of su, script, flock and
    runuser, or None."""
    for i, a in enumerate(args):
        if a == "--command" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--command="):
            return a.split("=", 1)[1]
        if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            rest = a[a.index("c", 1) + 1:]       # -c SCRIPT, -qc SCRIPT, -cSCRIPT
            if rest:
                return rest
            return args[i + 1] if i + 1 < len(args) else None
    return None


def _mentions_refs(text: str) -> bool:
    """`text` names git and, after it, a ref-moving subcommand. The check
    for scripts nested two levels deep, which are not parsed."""
    words = re.findall(r"[\w.-]+", text)
    if "git" not in words:
        return False
    return any(w in GIT_REF_MOVING for w in words[words.index("git") + 1:])


# Shell keywords and group openers that can start a segment before the
# command they run (`{ git commit; }`, `! git push`, `then git push`).
_KEYWORD_PREFIXES = frozenset({"{", "}", "!", "then", "do", "else", "elif",
                               "if", "while", "until", "coproc"})
# Commands that run their arguments as a command, with the options each
# takes a separate value for. `None`: the wrapper's first operand (after its
# options) is not the command (timeout's duration, chrt's priority).
_WRAPPERS: dict[str, tuple[frozenset[str], int]] = {
    "env": (frozenset({"-u", "--unset", "-C", "--chdir", "-S",
                       "--split-string"}), 0),
    "command": (frozenset(), 0),
    "builtin": (frozenset(), 0),
    "exec": (frozenset({"-a"}), 0),
    "nice": (frozenset({"-n", "--adjustment"}), 0),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), 1),
    "nohup": (frozenset(), 0),
    "setsid": (frozenset(), 0),
    "stdbuf": (frozenset({"-i", "-o", "-e", "--input", "--output",
                          "--error"}), 0),
    "ionice": (frozenset({"-c", "--class", "-n", "--classdata", "-p",
                          "--pid", "-P", "--pgid", "-u", "--uid"}), 0),
    "chrt": (frozenset(), 1),
    "taskset": (frozenset(), 1),
    "sudo": (frozenset({"-u", "--user", "-g", "--group", "-C",
                        "--close-from", "-D", "--chdir", "-h", "--host",
                        "-p", "--prompt", "-r", "--role", "-t", "--type",
                        "-T", "--command-timeout", "-U", "--other-user"}), 0),
    "doas": (frozenset({"-u", "-C"}), 0),
    "xargs": (frozenset({"-I", "-i", "-n", "--max-args", "-P",
                         "--max-procs", "-L", "--max-lines", "-d",
                         "--delimiter", "-E", "-e", "-s", "--max-chars",
                         "-a", "--arg-file"}), 0),
    "time": (frozenset({"-f", "--format", "-o", "--output"}), 0),
    "watch": (frozenset({"-n", "--interval", "-d", "--differences", "-q",
                         "--equexit"}), 0),
    "flock": (frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}), 1),
    "runuser": (frozenset({"-u", "--user", "-g", "--group", "-G",
                           "--supp-group"}), 0),
    "strace": (frozenset({"-e", "-o", "-p", "-s", "-u", "-E", "-P", "-I",
                          "-a", "-O", "-S", "-X"}), 0),
    "ltrace": (frozenset({"-e", "-o", "-p", "-s", "-u", "-n"}), 0),
    "busybox": (frozenset(), 0),
    "unbuffer": (frozenset(), 0),
    "chronic": (frozenset(), 0),
    "ifne": (frozenset(), 0),
    "proxychains": (frozenset({"-f"}), 0),
    "firejail": (frozenset(), 0),
}
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})


def _unwrap(seg: list[str], depth: int = 0) -> list[list[str]]:
    """The commands one segment runs, past `VAR=x` prefixes, shell keywords,
    wrapper commands (`env`, `xargs`, `timeout 5`, `sudo -u x`, ...),
    `find -exec <cmd> ;` and `eval <words>`. Usually one; `find` can run
    several, and `env -S` / `eval` re-split their argument."""
    if depth > 8:
        return [seg]
    i = 0
    while i < len(seg) and (_ENV_ASSIGN_RE.match(seg[i])
                            or seg[i] in _KEYWORD_PREFIXES):
        i += 1
    seg = seg[i:]
    if not seg:
        return []
    base = os.path.basename(seg[0])
    if base == "function" and len(seg) > 2:
        return [seg] + _unwrap(seg[2:], depth + 1)   # function NAME { ...
    if base == "eval":
        try:
            words = shlex.split(" ".join(seg[1:]))
        except ValueError:
            return [seg]
        return [seg] + _unwrap(words, depth + 1)
    if base == "find":
        out: list[list[str]] = [seg]
        j = 1
        while j < len(seg):
            if seg[j] in _FIND_EXEC:
                end = j + 1
                while end < len(seg) and seg[end] not in (";", "+", "\\;"):
                    end += 1
                out.extend(_unwrap(seg[j + 1:end], depth + 1))
                j = end
            j += 1
        return out
    spec = _WRAPPERS.get(base)
    if spec is None:
        return [seg]
    value_opts, operands = spec
    j = 1
    while j < len(seg):
        a = seg[j]
        if a == "--":
            j += 1
            break
        if base == "env" and (_ENV_ASSIGN_RE.match(a) or a in ("-", "-i")):
            j += 1
            continue
        if base == "env" and a in ("-S", "--split-string") and j + 1 < len(seg):
            try:
                words = shlex.split(seg[j + 1])
            except ValueError:
                return [seg]
            return [seg] + _unwrap(words + seg[j + 2:], depth + 1)
        m_s = re.match(r"^-[iv0]*S(.+)$", a) if base == "env" else None
        if base == "env" and (a.startswith("--split-string=") or m_s):
            raw = a.split("=", 1)[1] if a.startswith("--") else m_s.group(1)
            try:
                words = shlex.split(raw)
            except ValueError:
                return [seg]
            return [seg] + _unwrap(words + seg[j + 1:], depth + 1)
        if not a.startswith("-") or a == "-":
            break
        if base == "command" and re.match(r"^-p*[vV]", a):
            return [seg]               # `command -v git` only looks it up
        name = a.split("=", 1)[0]
        if name in value_opts and "=" not in a:
            j += 2
        elif (not a.startswith("--") and len(a) > 2
              and f"-{a[-1]}" in value_opts
              and not any(f"-{c}" in value_opts for c in a[1:-1])):
            j += 2                     # -iu USER: a cluster ending in a value option
        else:
            j += 1
    j += operands
    if j >= len(seg):
        return [seg]
    return [seg] + _unwrap(seg[j:], depth + 1)


def _refs_in(seg: list[str], *, nested: bool) -> str | None:
    """The ref-moving git subcommand one operator-separated segment runs:
    `[VAR=x ...] [wrapper ...] [/path/]git <sub>` (see `_unwrap`), or a
    shell's `-c` script that classifies as `git_refs` (one level deep;
    deeper scripts are refused when they name git and a ref-moving
    subcommand). None otherwise."""
    for cmd in _unwrap(seg):
        base = os.path.basename(cmd[0])
        if base == "git":
            got = _git(cmd)
            if got["class"] == "git_refs":
                return got["subcommand"]
        elif base in _SHELLS or base in _SCRIPT_OPTION:
            script = (_shell_script(cmd[1:]) if base in _SHELLS
                      else _option_script(cmd[1:]))
            if script is None:
                continue
            if nested:
                if _mentions_refs(script):
                    return "git"
                continue
            got = classify(script, _nested=True)
            if got["class"] == "git_refs":
                return got.get("subcommand")
    return None


def _alias_refs(tokens: list[str]) -> str | None:
    """A `git -c alias.<x>=<value>` option whose value runs a ref-moving
    subcommand (`commit ...`, or `!<shell>` that classifies as git_refs)."""
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        opt = tokens[i]
        value = None
        if opt == "-c" and i + 1 < len(tokens):
            value = tokens[i + 1]
        elif opt.startswith("-c") and len(opt) > 2:
            value = opt[2:]
        if value is not None:
            name, _, body = value.partition("=")
            if name.lower().startswith("alias."):
                body = body.strip()
                if body.startswith("!"):
                    if classify(body[1:], _nested=True)["class"] == "git_refs":
                        return name
                elif body.split()[:1] and body.split()[0] in GIT_REF_MOVING:
                    return name
        i += 2 if opt in _GIT_VALUE_OPTS else 1
    return None


def _git(tokens: list[str]) -> dict[str, Any]:
    alias = _alias_refs(tokens)
    if alias is not None:
        return _result("git_refs", f"{alias} runs a ref-moving command",
                       subcommand=alias)
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
    if sub == "bisect":
        return bool(rest) and rest[0] in ("log", "view", "visualize")
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


def bre_to_ere(pattern: str) -> str | None:
    """A GNU grep basic regex as the equivalent Rust/Python regex, or None
    when it uses something without an equivalent (back-references, `\\<`,
    `\\>`, POSIX classes such as `[[:space:]]`)."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        at_start = not out or out[-1] in ("(", "|")
        if c == "\\":
            if i + 1 >= n:
                return None
            d = pattern[i + 1]
            if d == "{" and pattern[i + 2:i + 3] == ",":
                out.append("{0")          # \{,n\}: ripgrep needs the 0
            elif d in "|(){}+?":
                out.append(d)             # \| \( \) \{ \} \+ \? are operators
            elif d in "wWsSbB":
                out.append("\\" + d)
            elif d in ".*[]^$\\/-":
                out.append("\\" + d if d != "/" else "/")
            else:
                return None               # \1, \<, \>, \` ... have no equivalent
            i += 2
            continue
        if c == "[":
            j = i + 1
            neg = j < n and pattern[j] == "^"
            j += 1 if neg else 0
            body: list[str] = []
            first = True
            while j < n and (pattern[j] != "]" or first):
                ch = pattern[j]
                if ch == "[" and pattern[j + 1:j + 2] in (":", "=", "."):
                    return None
                body.append("\\" + ch if ch in "\\[]&~" else ch)
                first = False
                j += 1
            if j >= n:
                return None
            out.append("[" + ("^" if neg else "") + "".join(body) + "]")
            i = j + 1
            continue
        if c == "*" and (at_start or out[-1] == "^"):
            out.append("\\*")
        elif c in "|(){}+?":
            out.append("\\" + c)
        elif c == "^" and not at_start:
            out.append("\\^")
        elif c == "$" and i != n - 1 and pattern[i + 1:i + 3] not in ("\\|", "\\)"):
            out.append("\\$")
        else:
            out.append(c)
        i += 1
    return "".join(out)


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
        if "E" in flags:
            if not _ere_safe(pattern):
                return _result("other", "regex dialect differs from ripgrep")
        else:
            converted = bre_to_ere(pattern)
            if converted is None:
                return _result("other", "regex dialect differs from ripgrep")
            pattern = converted
    recursive = tool != "grep" or bool(flags & {"r", "R"})
    path = positional[0] if positional else None
    if path is None and not recursive:
        return _result("other", "grep without a path reads stdin")
    return _result("grep", pattern=pattern, path=path, include=include,
                   literal=literal, ignore_case="i" in flags,
                   recursive=recursive, tool=tool,
                   line_numbers="n" in flags or tool == "rg",
                   with_filename="H" in flags or recursive)


def _simple(tokens: list[str]) -> dict[str, Any]:
    cmd = tokens[0]
    base = os.path.basename(cmd)
    if base == "git":
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


def _strip_output_filter(command: str) -> tuple[str, tuple | None]:
    """`command` without a trailing `2>&1 | tail -N` style filter, and the
    filter as ("tail"|"head", N), or (command, None)."""
    m = _OUTPUT_FILTER_RE.match(command)
    if not m or not (m.group("redir") or m.group("tool")):
        return command, None
    filt = (m.group("tool"), int(m.group("n"))) if m.group("tool") else ("all", 0)
    return m.group("cmd"), filt


def classify(command: str, *, _filters: bool = True,
             _nested: bool = False, _compound: bool = True) -> dict[str, Any]:
    """The class of a shell command line (see the module docstring).

    A pytest command followed only by `2>&1` and/or `| tail -N` / `| head -N`
    is still `pytest`, with `output_filter` = ("tail"|"head", N) or
    ("all", 0) applied to its output.

    A command that is not simple but is a sequence the intent layer can
    carry out step by step is `compound` (see `plan_compound`).

    Ref-moving git is `git_refs` wherever it runs: in any operator- or
    newline-separated segment, after `VAR=value` prefixes and wrapper
    commands (`env`, `xargs`, `timeout`, `sudo`, `find -exec`, `eval`, ...),
    as `/path/git`, through `git -c alias.<x>=<ref-moving>`, and (one level
    deep) inside a backtick or `$(...)` substitution or a
    `sh|bash|zsh -c '<script>'`."""
    if not isinstance(command, str) or not command.strip():
        return _result("other", "empty command")
    inner, filt = _strip_output_filter(command) if _filters else (command, None)
    if filt is not None and inner.strip():
        got = classify(inner, _filters=False, _nested=_nested,
                       _compound=False)
        if got["class"] == "pytest":
            return {**got, "output_filter": filt}
    segments = _segments(command)
    if segments is None:
        return _result("complex", "does not tokenise")
    why = _scan(command)
    if why is None and len(segments) != 1:
        why = "operator"
    for seg in segments:
        sub = _refs_in(seg, nested=_nested)
        if sub is not None:
            return _result("git_refs",
                           f"compound command: {why}" if why else "",
                           subcommand=sub)
    for body in _substitutions(command):
        if _nested:
            hit = "git" if _mentions_refs(body) else None
        else:
            got = classify(body, _nested=True)
            hit = got.get("subcommand") if got["class"] == "git_refs" else None
        if hit is not None:
            return _result("git_refs", "command substitution", subcommand=hit)
    if why is not None:
        if _compound and not _nested:
            plan, not_why = plan_compound(command)
            if plan is not None:
                return _result("compound", why, **plan)
            return _result("complex", why, compound_reason=not_why)
        return _result("complex", why)
    tokens = segments[0]
    if _ENV_ASSIGN_RE.match(tokens[0]):
        why = "environment assignment prefix"
        if _compound and not _nested:
            plan, not_why = plan_compound(command)
            if plan is not None:
                return _result("compound", why, **plan)
            return _result("complex", why, compound_reason=not_why)
        return _result("complex", why)
    return _simple(tokens)


# --------------------------------------------------------------------------- #
# compound commands
# --------------------------------------------------------------------------- #
# Step kinds the intent layer carries out inside a compound command. The
# first four use the graph; a compound needs at least one of them.
GRAPH_STEPS = frozenset({"pytest", "read", "grep", "git_read"})
_PLAIN_STEPS = frozenset({"cd", "echo", "pwd", "ls"})
# git options a captured git step refuses: they write files or run programs.
_GIT_STEP_REFUSED = ("--output", "--ext-diff", "--textconv", "--open-files-in-pager",
                     "--no-index", "--contents", "-O", "--orderfile",
                     "--ignore-revs-file", "--exec", "--run")
_SEQ_OPS = ("&&", "||", ";")


def _lex(command: str) -> tuple[list[tuple[str, str, str]] | None, str]:
    """Tokens of a command line: ("word", raw, value), ("op", text, "") or
    ("redir", "<fd><op>", ""). (None, reason) when it cannot be lexed."""
    toks: list[tuple[str, str, str]] = []
    raw, val = "", ""
    in_word = False
    i, n = 0, len(command)

    def end_word() -> None:
        nonlocal raw, val, in_word
        if in_word:
            toks.append(("word", raw, val))
        raw, val, in_word = "", "", False

    while i < n:
        c = command[i]
        if c in " \t":
            end_word()
            i += 1
            continue
        if c == "\\":
            if command[i + 1:i + 2] == "\n":
                i += 2
                continue
            if i + 1 >= n:
                return None, "trailing backslash"
            raw += command[i:i + 2]
            val += command[i + 1]
            in_word = True
            i += 2
            continue
        if c == "'":
            end = command.find("'", i + 1)
            if end < 0:
                return None, "unbalanced quote"
            raw += command[i:end + 1]
            val += command[i + 1:end]
            in_word = True
            i = end + 1
            continue
        if c == '"':
            j = i + 1
            part = ""
            while j < n and command[j] != '"':
                if command[j] == "\\" and command[j + 1:j + 2] in ('"', "\\", "$", "`", "\n"):
                    part += command[j + 1]
                    j += 2
                    continue
                part += command[j]
                j += 1
            if j >= n:
                return None, "unbalanced quote"
            raw += command[i:j + 1]
            val += part
            in_word = True
            i = j + 1
            continue
        if c == "#" and not in_word:
            while i < n and command[i] != "\n":
                i += 1
            continue
        if c == "\n":
            end_word()
            toks.append(("op", ";", ""))
            i += 1
            continue
        if c in "<>":
            fd = ""
            if in_word and raw.isdigit() and raw == val:
                fd, raw, val, in_word = raw, "", "", False
            else:
                end_word()
            m = re.match(r"(>>|>&|>\||>|<<<|<<-|<<|<&|<>|<)", command[i:])
            op = m.group(1) if m else c
            if op in ("<<", "<<-"):
                return None, "heredoc"
            toks.append(("redir", fd + op, ""))
            i += len(op)
            continue
        if c == "&" and command[i + 1:i + 2] == ">":
            end_word()
            op = "&>>" if command[i + 2:i + 3] == ">" else "&>"
            toks.append(("redir", op, ""))
            i += len(op)
            continue
        if c in "&|;()":
            end_word()
            two = command[i:i + 2]
            if two in ("&&", "||", ";;", "|&"):
                toks.append(("op", two, ""))
                i += 2
            else:
                toks.append(("op", c, ""))
                i += 1
            continue
        raw += c
        val += c
        in_word = True
        i += 1
    end_word()
    return toks, ""


def _grep_filter(args: list[str]) -> tuple | None:
    """`grep [-viEFw] [-A|-B|-C N] PATTERN` reading stdin, as
    ("grep", regex, invert, before, after); None otherwise."""
    flags: set[str] = set()
    before = after = 0
    pattern: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-A", "-B", "-C") and i + 1 < len(args) and args[i + 1].isdigit():
            n = int(args[i + 1])
            before = n if a in ("-B", "-C") else before
            after = n if a in ("-A", "-C") else after
            i += 2
            continue
        m = re.match(r"^-([ABC])(\d+)$", a)
        if m:
            n = int(m.group(2))
            before = n if m.group(1) in "BC" else before
            after = n if m.group(1) in "AC" else after
        elif a == "-e" and i + 1 < len(args) and pattern is None:
            pattern = args[i + 1]
            i += 2
            continue
        elif a.startswith("-") and len(a) > 1 and a != "--":
            if not set(a[1:]) <= set("viEFw"):
                return None
            flags |= set(a[1:])
        elif pattern is None:
            pattern = a
        else:
            return None                    # a file operand: not a filter
        i += 1
    if pattern is None:
        return None
    if "F" in flags:
        rx = re.escape(pattern)
    elif "E" in flags:
        if not _ere_safe(pattern):
            return None
        rx = pattern
    else:
        rx = bre_to_ere(pattern)
        if rx is None:
            return None
    if "w" in flags:
        rx = rf"\b(?:{rx})\b"
    if "i" in flags:
        rx = "(?i)" + rx
    try:
        re.compile(rx)
    except re.error:
        return None
    return ("grep", rx, "v" in flags, before, after)


def _filter(words: list[str]) -> tuple | None:
    """A pipeline stage that only filters lines of its stdin: `tail -N` /
    `tail -n +N` / `head -N` / `sed -n 'A[,B]p'` as a read range
    (("tail"|"from"|"head", N) or ("lines", A, B)), or `grep` (see
    `_grep_filter`). None for anything else."""
    if not words:
        return None
    if words[0] == "grep":
        return _grep_filter(words[1:])
    if words[0] == "sed":
        if len(words) == 3 and words[1] == "-n":
            m = _SED_RE.match(words[2])
            if m:
                a, b = int(m.group(1)), m.group(2)
                end = None if b == "$" else int(b) if b else a
                if a >= 1 and (end is None or end >= a):
                    return ("lines", a, end)
        return None
    if words[0] not in ("tail", "head"):
        return None
    got = _read_like(words + ["-"])
    if got is None or got["class"] != "read" or got["file"] != "-":
        return None
    return got["range"]


def _git_step_ok(argv: list[str]) -> str | None:
    """Why a read-only git command may not run as a captured step."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] == "--no-pager":
            i += 1
        elif argv[i] == "-C" and i + 1 < len(argv):
            i += 2
        else:
            return f"git option {argv[i]} is not mapped"
    for a in argv[i + 1:]:
        if a.startswith(_GIT_STEP_REFUSED):
            return f"git option {a.split('=', 1)[0]} is not mapped"
    return None


def git_step_dirs(argv: list[str]) -> list[str]:
    """The `-C <dir>` options of a git step, in order."""
    return [argv[i + 1] for i in range(1, len(argv) - 1) if argv[i] == "-C"]


def _plan_step(words: list[tuple[str, str, str]], *, leading: bool
               ) -> tuple[dict[str, Any] | None, str]:
    env: dict[str, str] = {}
    while words and _ENV_ASSIGN_RE.match(words[0][1]):
        name, _, value = words[0][2].partition("=")
        env[name] = value
        words = words[1:]
    if not words:
        return None, "environment assignment without a command"
    raw = " ".join(w[1] for w in words)
    argv = [w[2] for w in words]
    why = _scan(raw) or _scan(" ".join(env.values()).replace("=", " "))
    if why is not None:
        return None, f"{argv[0]}: {why}"
    cmd = argv[0]
    if env:
        got = classify(raw, _filters=False, _compound=False)
        if got["class"] != "pytest":
            return None, "environment assignment prefix"
        return {"kind": "pytest", "argv": argv, "raw": raw,
                "cls": {**got, "env": env}}, ""
    if cmd == "cd":
        if not leading:
            return None, "cd after another step"
        if len(argv) != 2 or argv[1] in ("-", ""):
            return None, "cd form is not mapped"
        return {"kind": "cd", "argv": argv, "raw": raw, "dir": argv[1]}, ""
    if cmd == "echo":
        args = argv[1:]
        newline = True
        if args[:1] == ["-n"]:
            newline, args = False, args[1:]
        if any(a.startswith("-") and len(a) > 1 and set(a[1:]) <= set("neE")
               for a in args[:1]):
            return None, "echo option is not mapped"
        return {"kind": "echo", "argv": argv, "raw": raw,
                "text": " ".join(args), "newline": newline}, ""
    if cmd == "pwd":
        if any(a not in ("-L", "-P") for a in argv[1:]):
            return None, "pwd form is not mapped"
        return {"kind": "pwd", "argv": argv, "raw": raw}, ""
    if cmd == "ls":
        return {"kind": "ls", "argv": argv, "raw": raw,
                "paths": [a for a in argv[1:] if not a.startswith("-")]}, ""
    got = classify(raw, _filters=False, _compound=False)
    kind = got["class"]
    if kind == "git_read":
        why = _git_step_ok(argv)
        if why:
            return None, why
    elif kind not in ("pytest", "read", "grep"):
        return None, got.get("reason") or kind
    return {"kind": kind, "argv": argv, "raw": raw, "cls": got}, ""


def plan_compound(command: str) -> tuple[dict[str, Any] | None, str]:
    """A command line the intent layer can carry out step by step, as
    `{"steps": [...], "ops": [...]}`, or (None, why not).

    The grammar is a sequence of pipelines joined by `&&`, `||`, `;` or a
    newline. A pipeline is one step, then any number of `| tail|head -N`
    filters, with an optional `2>&1` or `2>/dev/null` (and `>/dev/null`).
    A step is a pytest, read, grep or read-only git command, `echo` with
    literal words, `pwd`, `ls`, or a leading `cd DIR`. At least one step
    must be pytest, read, grep or git (`GRAPH_STEPS`). Subshells, `&`,
    heredocs, other redirects, substitutions, globs and any other command
    make it (None, reason).

    Each step is `{"kind", "argv", "raw", "filters": [range...],
    "stderr": "merge"|"null"|None, "stdout_null": bool}` plus, for the
    classified kinds, `cls` (the step's own `classify` result)."""
    toks, why = _lex(command)
    if toks is None:
        return None, why
    pipelines: list[list[list[tuple]]] = [[[]]]
    redirs: list[list[list[tuple[str, str]]]] = [[[]]]
    ops: list[str] = []
    i = 0
    while i < len(toks):
        kind, text, _val = toks[i]
        if kind == "word":
            pipelines[-1][-1].append(toks[i])
        elif kind == "redir":
            if i + 1 >= len(toks) or toks[i + 1][0] != "word":
                return None, f"redirect {text} without a target"
            redirs[-1][-1].append((text, toks[i + 1][2]))
            i += 1
        elif text in ("|", "|&"):
            if text == "|&":
                redirs[-1][-1].append(("2>&", "1"))
            pipelines[-1].append([])
            redirs[-1].append([])
        elif text in _SEQ_OPS:
            ops.append(text)
            pipelines.append([[]])
            redirs.append([[]])
        else:
            return None, f"operator {text} is not mapped"
        i += 1
    # A trailing `;` or newline ends the last command.
    while ops and ops[-1] == ";" and pipelines[-1] == [[]]:
        ops.pop()
        pipelines.pop()
        redirs.pop()
    steps: list[dict[str, Any]] = []
    leading = True
    for pipe, reds in zip(pipelines, redirs):
        if any(not cmd for cmd in pipe):
            return None, "empty command"
        step, why = _plan_step(pipe[0], leading=leading)
        if step is None:
            return None, why
        leading = leading and step["kind"] == "cd"
        if step["kind"] == "cd" and len(pipe) > 1:
            return None, "cd in a pipeline"
        filters = []
        for cmd in pipe[1:]:
            got = _filter([w[2] for w in cmd])
            if got is None:
                return None, f"pipeline into {cmd[0][2]} is not mapped"
            filters.append(got)
        # Where fd 1 and fd 2 of the step end up, applying the redirects
        # in order as bash does: "out" (the pipeline / the output), "err"
        # (the output, past any filter), "null".
        fd1, fd2 = "out", "err"
        for op, target in reds[0]:
            if op in ("2>&", "2>&1") and target == "1":
                fd2 = fd1
            elif op == "2>" and target == "/dev/null":
                fd2 = "null"
            elif op in (">", "1>") and target == "/dev/null":
                fd1 = "null"
            elif op in ("&>", ">&") and target == "/dev/null":
                fd1 = fd2 = "null"
            else:
                return None, f"redirect {op}{target} is not mapped"
        if any(cmd_reds for cmd_reds in reds[1:]):
            return None, "redirect inside a pipeline"
        stderr = {"out": "merge", "null": "null", "err": None}[fd2]
        stdout_null = fd1 == "null"
        steps.append({**step, "filters": filters, "stderr": stderr,
                      "stdout_null": stdout_null})
    if not any(s["kind"] in GRAPH_STEPS for s in steps):
        return None, "no pytest, read, grep or git step"
    return {"steps": steps, "ops": ops}, ""


def command_heads(command: str) -> list[list[str]]:
    """The commands a (possibly complex) command line runs, best effort:
    each operator-separated segment past wrappers. For graph context after
    a passthrough command, never for decisions about running it."""
    out: list[list[str]] = []
    for seg in _segments(command) or []:
        out.extend(c for c in _unwrap(seg) if c)
    return out


def command_resources(command: str) -> list[str]:
    """The simple commands of a command line as permission resources: the
    words of each command between operators, without redirections, leaving
    out `cd`, `chdir`, `pushd` and `popd` (what OpenCode 2.3's shell tool
    asks permission for, approximately). Empty when it cannot be lexed."""
    toks, _why = _lex(command)
    if toks is None:
        return []
    out: list[str] = []
    cur: list[str] = []
    skip = False
    def flush() -> None:
        if cur and cur[0] not in ("cd", "chdir", "pushd", "popd"):
            out.append(" ".join(cur))

    for kind, text, _val in toks:
        if skip:
            skip = False
            continue
        if kind == "word":
            cur.append(text)
        elif kind == "redir":
            skip = True
        else:
            flush()
            cur = []
    flush()
    return out


__all__ = ["GIT_READ_ONLY", "GIT_REF_MOVING", "GRAPH_STEPS", "classify",
           "command_heads", "command_resources", "plan_compound"]
