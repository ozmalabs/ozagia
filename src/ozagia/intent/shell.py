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
    read          cat F | head [-n N] F | tail [-n N|+N] F | tail -c [+]N F |
                  sed -n 'A[,B]p' F | nl [-ba|-bt] F. Fields: `file`,
                  `range` (`("all",)`, `("head", n)`, `("tail", n)`,
                  `("from", n)`, `("lines", a, b|None)`, `("bytes_tail", n)`,
                  `("bytes_from", n)`), `tool`, and for nl `number` ("a"|"t").
    grep          grep [-rRnHiFE] [-A|-B|-C N|-N] [--include G] PAT [PATH...],
                  rg [-nNHiF] [-A|-B|-C N] [-g G]... [-t T] [-T T] PAT
                  [PATH...], git grep [-niFE] PAT [PATH]. Fields: `pattern`,
                  `paths` (`path`: the first, None for the working
                  directory), `globs` (`include`: the first), `types`,
                  `literal`, `ignore_case`, `recursive`, `tool`,
                  `line_numbers`, `with_filename` (None: decided by the
                  paths), `before`, `after`, `separators`, `hidden`.
    files         rg --files [PATH...] [-g G]... [-t T] [-T T] [--hidden]
                  [--no-ignore[-vcs]] [-u|-uu|-uuu]: run by rg itself.
                  Fields: `paths`, `options`.
    git_refs      a git command that moves refs (`GIT_REF_MOVING`: commit,
                  push, stash, bisect, ...). Fields: `subcommand` (the
                  alias name for an alias, "git" for a deeply nested
                  script). Listing forms (`git branch`, `git
                  tag -l`, `git stash list`, `git worktree list`, `git
                  bisect log`) are `git_read`.
    git_read      a read-only git command (diff, show, status, log, blame,
                  and the listing forms above). Fields: `subcommand`.
    ls            `ls [PATH]`, captured as a sorted visible-name listing.
    find          `find [PATH...] [-maxdepth N] [-mindepth N] [EXPR]` with
                  -name -iname -path -ipath -type f|d|l -newer F -print
                  -prune -true -false ! -not -a -o ( ): `expr` (a tree),
                  `prints`, `paths`, `maxdepth`, `mindepth`, `newer`.
    jq            `jq [-r|-c|-S|-M] FILTER FILE`, `cat FILE | jq ...`, `jq ...
                  < FILE`, with a filter in `ozagia.core.jq`'s subset
                  (read-only). Fields: `file`, `filter`, `program`, `raw`,
                  `compact`, `sort_keys`, `stdin`. Other filters are `other`
                  with reason `jq filter '...' is not mapped`.
    ruff          `ruff check [PATH...] [--select X]... [--output-format
                  concise|full]`, the executable as `ruff` or a path such
                  as `.venv/bin/ruff`: run by ruff itself, with the node
                  keys of its findings appended. Fields: `tool`, `argv`,
                  `paths`, `select`, `output_format`. Any other option
                  (`--fix`, `--watch`, ...) and other subcommands (`ruff
                  format`) are `other`.
    complex       not a simple command (see above).
    other         anything else (including wc, unsupported find/ls, sed -i,
                  python -c, scripts).

A read-only class (read, grep, files, find, git_read) may follow harmless
`VAR=x` prefixes (LC_*, LANG, NO_COLOR, TERM, PAGER, GIT_PAGER, COLUMNS;
`env` holds them; a locale variable is refused for grep and git grep, whose
regex semantics it changes, and for find -iname/-ipath), and read, grep,
files, find, ls and git_read may run inside `timeout [-s SIG] [-k D] N`
(`timeout` holds the seconds, `inner` the command).

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


def _cd_read_wrapper(command: str) -> dict[str, Any] | None:
    """Recognize exactly `cd LITERAL && SAFE_READ_COMMAND`."""
    quote = ""
    split = None
    i = 0
    while i < len(command) - 1:
        c = command[i]
        if c == "\\":
            i += 2
            continue
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif command[i:i + 2] == "&&":
            if split is not None:
                return None
            split = i
            i += 1
        i += 1
    if split is None:
        return None
    left, right = command[:split].strip(), command[split + 2:].strip()
    if not left or not right or _scan(left) is not None:
        return None
    try:
        cd = shlex.split(left)
    except ValueError:
        return None
    if len(cd) != 2 or cd[0] != "cd" or cd[1] in ("", "-"):
        return None
    got = classify(right, _filters=False)
    if got["class"] not in ("read", "grep", "ls", "find", "files", "jq"):
        return None
    return {**got, "cd": cd[1]}


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
    unit = ""                      # "" lines, "c" bytes (tail -c)
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            files.extend(args[i + 1:])
            break
        value = None
        this = "n"
        if a == "-n" and i + 1 < len(args):
            value, i = args[i + 1], i + 1
        elif a.startswith("--lines="):
            value = a.split("=", 1)[1]
        elif a.startswith("-n") and len(a) > 2:
            value = a[2:]
        elif a.startswith("-") and a[1:].isdigit():
            value = a[1:]
        elif cmd == "tail" and a == "-c" and i + 1 < len(args):
            value, i, this = args[i + 1], i + 1, "c"
        elif cmd == "tail" and a.startswith("--bytes="):
            value, this = a.split("=", 1)[1], "c"
        elif cmd == "tail" and a.startswith("-c") and len(a) > 2:
            value, this = a[2:], "c"
        elif a.startswith("-") and a != "-":
            return _result("other", f"{cmd} option {a} is not mapped")
        else:
            files.append(a)
        if value is not None:
            if unit and unit != this:
                return _result("other", f"{cmd} -n with -c is not mapped")
            unit = this
            if cmd == "tail" and value.startswith("+"):
                plus, value = True, value[1:]
            n = _positive(value)
            if n is None:
                return _result("other", f"{cmd} count {value!r} is not mapped")
        i += 1
    if len(files) != 1:
        return _result("other", f"{cmd} of {len(files)} files is not mapped")
    if unit == "c":
        rng: tuple = ("bytes_from", n) if plus else ("bytes_tail", n)
    elif cmd == "head":
        rng = ("head", n)
    else:
        rng = ("from", n) if plus else ("tail", n)
    return _result("read", file=files[0], range=rng, tool=cmd)


def _nl(args: list[str]) -> dict[str, Any]:
    """`nl [-ba|-bt] FILE`: a read numbered as nl numbers it (width 6, a
    tab; `-bt`, the default, leaves empty lines unnumbered)."""
    style = "t"
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            files.extend(args[i + 1:])
            break
        value = None
        if a == "-b" and i + 1 < len(args):
            value, i = args[i + 1], i + 1
        elif a.startswith("-b") and len(a) > 2:
            value = a[2:]
        elif a.startswith("--body-numbering="):
            value = a.split("=", 1)[1]
        elif a.startswith("-") and a != "-":
            return _result("other", f"nl option {a} is not mapped")
        else:
            files.append(a)
        if value is not None:
            if value not in ("a", "t"):
                return _result("other", f"nl -b{value} is not mapped")
            style = value
        i += 1
    if len(files) != 1 or files[0] == "-":
        return _result("other", f"nl of {len(files)} files is not mapped")
    return _result("read", file=files[0], range=("all",), tool="nl",
                   number=style)


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
    if cmd == "nl":
        return _nl(args)
    if cmd == "wc":
        return _result("other", f"{cmd} is not mapped")
    if cmd == "ls":
        # Keep this deliberately small: ls formatting and option semantics
        # vary, while the graph's directory view is stable and useful.
        if len(args) <= 1 and not any(a.startswith("-") for a in args):
            return _result("ls", path=args[0] if args else ".")
        return _result("other", "ls options or multiple paths are not mapped")
    return None


class _FindRefused(Exception):
    pass


def find_glob_regex(pattern: str, *, icase: bool = False) -> str | None:
    """A find glob (`fnmatch` without FNM_PATHNAME or FNM_PERIOD: `*` and
    `?` match `/` and a leading dot) as a Python regex for `re.fullmatch`,
    or None when it uses something without a checked equivalent: POSIX
    classes (`[[:alpha:]]`), a range other than within a-z, A-Z or 0-9, a
    trailing backslash, or a non-ASCII pattern with `-iname`/`-ipath`."""
    if icase and not pattern.isascii():
        return None
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            if i + 1 >= n:
                return None
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "[":
            j = i + 1
            neg = j < n and pattern[j] in "!^"
            j += 1 if neg else 0
            items: list[str] = []
            first = True
            while j < n and (pattern[j] != "]" or first):
                ch = pattern[j]
                if ch == "[" and pattern[j + 1:j + 2] in (":", "=", "."):
                    return None
                if ch == "\\":
                    if j + 1 >= n:
                        return None
                    ch = pattern[j + 1]
                    j += 1
                if (pattern[j + 1:j + 2] == "-" and j + 2 < n
                        and pattern[j + 2] != "]"):
                    hi = pattern[j + 2]
                    if not any(lo_ <= ch <= hi <= hi_ for lo_, hi_ in
                               (("a", "z"), ("A", "Z"), ("0", "9"))):
                        return None
                    items.append(re.escape(ch) + "-" + re.escape(hi))
                    j += 3
                else:
                    items.append(re.escape(ch))
                    j += 1
                first = False
            if j >= n:                     # no closing bracket: a literal [
                out.append(re.escape("["))
                i += 1
                continue
            out.append("[" + ("^" if neg else "") + "".join(items) + "]")
            i = j
        else:
            out.append(re.escape(c))
        i += 1
    return ("(?i)" if icase else "") + "(?s:" + "".join(out) + ")"


def _one_char_glob(pattern: str) -> bool:
    """Whether a find glob has a `?` or a bracket set, which match one
    character: one byte in a C locale, one code point in a UTF-8 one."""
    i = 0
    while i < len(pattern):
        if pattern[i] == "\\":
            i += 2
            continue
        if pattern[i] in "?[":
            return True
        i += 1
    return False


_FIND_GLOBS = {"-name": False, "-iname": True, "-path": False, "-ipath": True,
               "-wholename": False, "-iwholename": True}


def _find(args: list[str]) -> dict[str, Any]:
    """`find [-P] [PATH...] [-maxdepth N] [-mindepth N] [EXPR]` where EXPR
    uses `-name -iname -path -ipath -type f|d|l -newer FILE -print -prune
    -true -false`, `!`/`-not`, `-a`/`-and`, `-o`/`-or` and `( )`. The
    expression is kept as a tree (`expr`) and evaluated by
    `intent.shellfs.run_find`; anything else is not mapped."""
    i = 0
    while i < len(args) and args[i] in ("-P",):
        i += 1
    if i < len(args) and args[i] in ("-H", "-L", "-D", "-O"):
        return _result("other", f"find option {args[i]} is not mapped")
    paths: list[str] = []
    while (i < len(args) and not args[i].startswith("-")
           and args[i] not in ("(", ")", "!", ",")):
        paths.append(args[i])
        i += 1
    depth: dict[str, int | None] = {"-maxdepth": None, "-mindepth": None}
    while i < len(args) and args[i] in depth:
        if i + 1 >= len(args) or not args[i + 1].isdigit():
            return _result("other", f"find {args[i]} needs a number")
        depth[args[i]] = int(args[i + 1])
        i += 2
    toks = args[i:]
    pos = 0
    newer: list[str] = []

    def peek() -> str | None:
        return toks[pos] if pos < len(toks) else None

    def take() -> str:
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def primary() -> tuple:
        tok = take()
        if tok in _FIND_GLOBS:
            if peek() is None:
                raise _FindRefused(f"find {tok} needs a pattern")
            pat = take()
            if tok in ("-name", "-iname") and "/" in pat:
                raise _FindRefused(f"find {tok} with a slash is not mapped")
            rx = find_glob_regex(pat, icase=_FIND_GLOBS[tok])
            if rx is None:
                raise _FindRefused(f"find {tok} pattern {pat!r} is not mapped")
            field = "name" if tok in ("-name", "-iname") else "path"
            # How the glob depends on the name's encoding (shellfs).
            enc = ("icase" if _FIND_GLOBS[tok] else
                   "one" if _one_char_glob(pat) else "")
            return (field, rx, enc)
        if tok == "-type":
            t = take() if peek() is not None else ""
            if t not in ("f", "d", "l"):
                raise _FindRefused(f"find -type {t} is not mapped")
            return ("type", t)
        if tok == "-newer":
            if peek() is None:
                raise _FindRefused("find -newer needs a file")
            ref = take()
            newer.append(ref)
            return ("newer", ref)
        if tok in ("-print", "-prune"):
            return (tok[1:],)
        if tok in ("-true", "-false"):
            return ("const", tok == "-true")
        if tok in depth:
            raise _FindRefused(f"find {tok} after a test is not mapped")
        raise _FindRefused(f"find {tok} is not mapped")

    def unary() -> tuple:
        tok = peek()
        if tok in ("!", "-not"):
            take()
            if peek() is None:
                raise _FindRefused(f"find {tok} without an expression")
            return ("not", unary())
        if tok == "(":
            take()
            e = or_expr()
            if peek() != ")":
                raise _FindRefused("find: unbalanced parenthesis")
            take()
            return e
        if tok is None or tok in (")", "-o", "-or", "-a", "-and"):
            raise _FindRefused(f"find: expected an expression before {tok}")
        return primary()

    def and_expr() -> tuple:
        left = unary()
        while peek() is not None and peek() not in ("-o", "-or", ")"):
            if peek() in ("-a", "-and"):
                take()
            left = ("and", left, unary())
        return left

    def or_expr() -> tuple:
        left = and_expr()
        while peek() in ("-o", "-or"):
            take()
            left = ("or", left, and_expr())
        return left

    try:
        expr: tuple = ("const", True) if not toks else or_expr()
        if pos != len(toks):
            raise _FindRefused(f"find {toks[pos]} is not mapped")
    except _FindRefused as e:
        return _result("other", str(e))

    def has_print(e: tuple) -> bool:
        return e[0] == "print" or any(isinstance(x, tuple) and has_print(x)
                                      for x in e[1:])
    return _result("find", path=paths[0] if paths else ".",
                   paths=paths or ["."], expr=expr, prints=has_print(expr),
                   maxdepth=depth["-maxdepth"], mindepth=depth["-mindepth"],
                   newer=newer)


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


_GREP_FLAGS = {"grep": set("rRnHiFE"), "rg": set("nNHiF"), "git grep": set("niFE")}
# Short options that take a value (attached or as the next word).
_GREP_VALUES = {"grep": set("eABC"), "rg": set("eABCgtT"), "git grep": set("e")}
_GREP_LONG_FLAGS = {"--recursive": "r", "--line-number": "n", "--ignore-case": "i",
                    "--fixed-strings": "F", "--extended-regexp": "E",
                    "--with-filename": "H", "--no-line-number": "N"}
_GREP_LONG_VALUES = {"--regexp": "e", "--after-context": "A",
                     "--before-context": "B", "--context": "C",
                     "--include": "include", "--glob": "g", "--type": "t",
                     "--type-not": "T"}
_GREP_LONG_OK = {"grep": {"--regexp", "--after-context", "--before-context",
                          "--context", "--include"},
                 "rg": {"--regexp", "--after-context", "--before-context",
                        "--context", "--glob", "--type", "--type-not"},
                 "git grep": {"--regexp"}}


def _grep(args: list[str], *, tool: str) -> dict[str, Any]:
    """grep, rg and git grep searches. Several paths, `-A/-B/-C N` (and
    grep's `-N`), rg's `-g/--glob` (negated too: rg evaluates them), and
    rg's `-t/-T TYPE` are kept as fields for the runner."""
    flags: set[str] = set()
    pattern: str | None = None
    globs: list[str] = []
    types: list[tuple[str, str]] = []
    ctx: dict[str, int] = {}
    positional: list[str] = []
    allowed = _GREP_FLAGS[tool]
    values = _GREP_VALUES[tool]

    def value(opt: str, val: str) -> str | None:
        """Store one option value; the reason it is not mapped, or None."""
        nonlocal pattern
        if opt == "e":
            if pattern is not None:
                return f"{tool} with several patterns is not mapped"
            pattern = val
        elif opt in ("A", "B", "C"):
            if not val.isdigit():
                return f"{tool} context {val!r} is not mapped"
            ctx[opt] = int(val)
        elif opt in ("g", "include"):
            if opt == "include" and ("/" in val or val.startswith("!")):
                return f"grep --include={val} is not mapped"
            globs.append(val)
        elif opt in ("t", "T"):
            types.append((f"-{opt}", val))
        return None

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positional.extend(args[i + 1:])
            break
        name, eq, val = a.partition("=")
        if a.startswith("--"):
            if name in _GREP_LONG_VALUES and name in _GREP_LONG_OK[tool]:
                if not eq:
                    if i + 1 >= len(args):
                        return _result("other", f"{a} needs a value")
                    val, i = args[i + 1], i + 1
                why = value(_GREP_LONG_VALUES[name], val)
                if why:
                    return _result("other", why)
            elif a in _GREP_LONG_FLAGS and _GREP_LONG_FLAGS[a] in allowed:
                flags.add(_GREP_LONG_FLAGS[a])
            elif tool == "rg" and a == "--hidden":
                return _result("other", "rg --hidden search is not mapped")
            else:
                return _result("other", f"{tool} option {name} is not mapped")
            i += 1
            continue
        if a.startswith("-") and len(a) > 1:
            if tool == "grep" and a[1:].isdigit():
                ctx["C"] = int(a[1:])     # grep -2: two lines of context
                i += 1
                continue
            j = 1
            while j < len(a):
                c = a[j]
                if c in values:
                    rest = a[j + 1:]
                    if tool == "rg" and rest.startswith("="):
                        # rg (lexopt) drops one `=` before an attached
                        # value (`-g=*.py`); grep (getopt) keeps it.
                        rest = rest[1:]
                    elif not rest:
                        if i + 1 >= len(args):
                            return _result("other", f"{a} needs a value")
                        rest, i = args[i + 1], i + 1
                    why = value(c, rest)
                    if why:
                        return _result("other", why)
                    break
                if c not in allowed:
                    return _result("other", f"{tool} option {a} is not mapped")
                flags.add(c)
                j += 1
            i += 1
            continue
        positional.append(a)
        i += 1
    if pattern is None:
        if not positional:
            return _result("other", f"{tool} without a pattern")
        pattern, positional = positional[0], positional[1:]
    if len(positional) > 1 and tool == "git grep":
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
    if not positional and not recursive:
        return _result("other", "grep without a path reads stdin")
    if "-" in positional:
        return _result("other", f"{tool} of stdin is not mapped")
    if ctx and tool == "git grep":
        return _result("other", "git grep context is not mapped")
    # -A/-B override -C whatever their order (grep and rg alike).
    before = ctx.get("B", ctx.get("C", 0))
    after = ctx.get("A", ctx.get("C", 0))
    # grep prints `--` between groups once any context option is given
    # (even 0); rg only when there are context lines.
    separators = bool(ctx) if tool == "grep" else bool(before or after)
    return _result("grep", pattern=pattern,
                   path=positional[0] if positional else None,
                   paths=positional, include=globs[0] if globs else None,
                   globs=globs, types=types,
                   literal=literal, ignore_case="i" in flags,
                   recursive=recursive, tool=tool,
                   line_numbers="n" in flags and "N" not in flags,
                   with_filename=True if ("H" in flags or tool == "git grep")
                   else None,
                   before=before, after=after, separators=separators,
                   hidden=tool != "rg")


_RG_FILES_FLAGS = {"--files", "--hidden", "-.", "--no-ignore", "--no-ignore-vcs",
                   "-u", "-uu", "-uuu"}
_RG_FILES_VALUES = {"-g": "-g", "--glob": "-g", "-t": "-t", "--type": "-t",
                    "-T": "-T", "--type-not": "-T"}


def _rg_files(args: list[str]) -> dict[str, Any]:
    """`rg --files [PATH...] [-g GLOB]... [-t TYPE] [-T TYPE] [--hidden]
    [--no-ignore[-vcs]] [-u|-uu]`: a file listing, run by rg itself (so
    its ignore rules are exact), sorted by path."""
    opts: list[str] = []
    paths: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            paths.extend(args[i + 1:])
            break
        name, eq, val = a.partition("=") if a.startswith("--") else (a, "", "")
        if a in _RG_FILES_FLAGS:
            if a != "--files":
                opts.append(a)
        elif name in _RG_FILES_VALUES and (eq or name.startswith("--")
                                           or len(a) == 2):
            if not eq:
                if i + 1 >= len(args):
                    return _result("other", f"{a} needs a value")
                val, i = args[i + 1], i + 1
            opts += [_RG_FILES_VALUES[name], val]
        elif a[:2] in ("-g", "-t", "-T") and len(a) > 2 and not a.startswith("--"):
            # rg drops one `=` before an attached value (`-g=*.py`).
            opts += [a[:2], a[3:] if a[2] == "=" else a[2:]]
        elif a in ("--sort", "--sort=path") or a == "--sort-files":
            if a == "--sort":
                if i + 1 >= len(args) or args[i + 1] != "path":
                    return _result("other", "rg --sort other than path is not mapped")
                i += 1
        elif a.startswith("-") and a != "-":
            return _result("other", f"rg option {name} with --files is not mapped")
        else:
            paths.append(a)
        i += 1
    if "-" in paths:
        return _result("other", "rg --files of stdin is not mapped")
    return _result("files", paths=paths, options=opts, tool="rg")


_JQ_FLAGS = {"-r": "raw", "--raw-output": "raw", "-c": "compact",
             "--compact-output": "compact", "-S": "sort_keys",
             "--sort-keys": "sort_keys", "-M": None,
             "--monochrome-output": None}


def _jq(args: list[str], *, stdin_file: str | None = None) -> dict[str, Any]:
    """`jq [-r] [-c] [-S] [-M] FILTER FILE` (or with `stdin_file` as the
    input, FILTER only), with a filter in `ozagia.core.jq`'s subset."""
    from ozagia.core import jq as J
    opts = {"raw": False, "compact": False, "sort_keys": False}
    positional: list[str] = []
    for a in args:
        if a.startswith("-") and a != "-" and not positional:
            if a in _JQ_FLAGS:
                name = _JQ_FLAGS[a]
            elif (not a.startswith("--") and len(a) > 2
                  and all(f"-{c}" in _JQ_FLAGS for c in a[1:])):
                for c in a[1:]:
                    if _JQ_FLAGS[f"-{c}"]:
                        opts[_JQ_FLAGS[f"-{c}"]] = True
                continue
            else:
                return _result("other", f"jq option {a} is not mapped")
            if name:
                opts[name] = True
            continue
        if a.startswith("-") and a != "-":
            return _result("other", f"jq option {a} after the filter is "
                                    f"not mapped")
        positional.append(a)
    if not positional:
        return _result("other", "jq without a filter")
    filt, files = positional[0], positional[1:]
    if stdin_file is not None:
        if files:
            return _result("other", "jq with both a file and stdin")
        files = [stdin_file]
    if len(files) != 1 or files[0] == "-":
        return _result("other", f"jq of {len(files)} files is not mapped")
    try:
        program = J.parse(filt)
    except J.Unsupported as e:
        return _result("other", str(e))
    return _result("jq", file=files[0], filter=filt, program=program,
                   tool="jq", stdin=stdin_file is not None, **opts)


def _jq_wrapper(command: str) -> dict[str, Any] | None:
    """`cat F | jq ...` or `jq ... < F` (also `< F jq ...`), else None."""
    toks, _why = _lex(command)
    if toks is None:
        return None
    words: list[list[tuple[str, str, str]]] = [[]]
    redirs: list[str] = []
    i = 0
    while i < len(toks):
        kind, text, _val = toks[i]
        if kind == "word":
            words[-1].append(toks[i])
        elif kind == "redir":
            if text not in ("<", "0<") or i + 1 >= len(toks) \
                    or toks[i + 1][0] != "word":
                return None
            redirs.append(toks[i + 1][2])
            if _scan(toks[i + 1][1]) is not None:
                return None
            i += 1
        elif text == "|":
            words.append([])
        else:
            return None
        i += 1
    for seg in words:
        if not seg or _scan(" ".join(w[1] for w in seg)) is not None:
            return None
    if len(words) == 2 and not redirs:
        cat, jq = [[w[2] for w in seg] for seg in words]
        if (len(cat) != 2 or cat[0] != "cat" or cat[1].startswith("-")
                or jq[0] != "jq"):
            return None
        return _jq(jq[1:], stdin_file=cat[1])
    if len(words) == 1 and len(redirs) == 1:
        argv = [w[2] for w in words[0]]
        if argv[0] != "jq":
            return None
        return _jq(argv[1:], stdin_file=redirs[0])
    return None


_RUFF_VALUE_OPTS = frozenset({"--select", "--output-format"})
RUFF_OUTPUT_FORMATS = frozenset({"concise", "full"})


def _ruff(tokens: list[str]) -> dict[str, Any]:
    """`ruff check [PATH...] [--select X]... [--output-format concise|full]`
    (the executable as `ruff` or a path ending in `/ruff`)."""
    if len(tokens) < 2 or tokens[1] != "check":
        sub = tokens[1] if len(tokens) > 1 else "with no subcommand"
        return _result("other", f"ruff {sub} is not mapped")
    paths: list[str] = []
    select: list[str] = []
    fmt: str | None = None
    args = tokens[2:]
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            paths.extend(args[i + 1:])
            break
        name, eq, value = tok.partition("=")
        if name in _RUFF_VALUE_OPTS:
            if not eq:
                if i + 1 >= len(args):
                    return _result("other", f"ruff option {name} needs a value")
                value = args[i + 1]
                i += 1
            if name == "--select":
                select.append(value)
            elif value not in RUFF_OUTPUT_FORMATS:
                return _result("other",
                               f"ruff --output-format {value} is not mapped")
            else:
                fmt = value
        elif tok.startswith("-"):
            return _result("other", f"ruff check option {name} is not mapped")
        else:
            paths.append(tok)
        i += 1
    return _result("ruff", tool=tokens[0], argv=list(tokens), paths=paths,
                   select=select, output_format=fmt)


def _simple(tokens: list[str]) -> dict[str, Any]:
    cmd = tokens[0]
    base = os.path.basename(cmd)
    if cmd == "jq":
        return _jq(tokens[1:])
    if base == "git":
        return _git(tokens)
    if base in ("pytest", "py.test"):
        return _pytest(tokens[1:], python=None, script=cmd)
    if _PYTHON_RE.match(base):
        if len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] == "pytest":
            return _pytest(tokens[3:], python=cmd, script=None)
        return _result("other", "python command")
    if cmd == "rg" and "--files" in tokens[1:]:
        return _rg_files(tokens[1:])
    if cmd in ("grep", "rg"):
        return _grep(tokens[1:], tool=cmd)
    read = _read_like(tokens)
    if read is not None:
        return read
    if cmd == "find":
        return _find(tokens[1:])
    if base == "ruff":
        return _ruff(tokens)
    if cmd == "timeout":
        secs, rest, why = timeout_args(tokens)
        if why:
            return _result("other", why)
        got = _simple(rest)
        if got["class"] not in TIMEOUT_CLASSES:
            return _result("other", got.get("reason")
                           or f"timeout around {got['class']} is not mapped")
        return {**got, "timeout": secs, "inner": rest}
    return _result("other", f"{cmd} is not mapped")


# Environment variables a read-only captured command may be prefixed with.
# None of them changes what the emulated commands print, with the
# exceptions `env_reason` refuses; a git or ls step gets them in its
# environment.
_HARMLESS_ENV_RE = re.compile(
    r"^(?:LC_[A-Z_]+|LANG|NO_COLOR|TERM|PAGER|GIT_PAGER|COLUMNS)$")
# Classes that may run inside `timeout N` (read-only; pytest is not).
TIMEOUT_CLASSES = frozenset({"read", "grep", "files", "find", "ls", "git_read"})
ENV_CLASSES = frozenset({"read", "grep", "files", "find", "git_read", "ls"})
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")


def timeout_args(argv: list[str]) -> tuple[float | None, list[str], str]:
    """`timeout [-s SIG] [-k DUR] [--preserve-status] [--foreground] [-v]
    DURATION CMD...` as (seconds, CMD..., ""), or (None, [], why)."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        a = argv[i]
        if a in ("-s", "--signal", "-k", "--kill-after"):
            i += 2
        elif (a.startswith(("--signal=", "--kill-after=")) or
              (a[:2] in ("-s", "-k") and len(a) > 2)):
            i += 1
        elif a in ("--preserve-status", "--foreground", "-v", "--verbose"):
            i += 1
        elif a == "--":
            i += 1
            break
        else:
            return None, [], f"timeout option {a} is not mapped"
    if i >= len(argv):
        return None, [], "timeout without a duration"
    m = _DURATION_RE.match(argv[i])
    if not m:
        return None, [], f"timeout duration {argv[i]!r} is not mapped"
    secs = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600,
                                "d": 86400}[m.group(2)]
    rest = argv[i + 1:]
    if not rest:
        return None, [], "timeout without a command"
    if secs <= 0:
        return None, [], "timeout 0 is not mapped"
    return secs, rest, ""


def env_reason(cls: dict[str, Any], env: dict[str, str]) -> str | None:
    """Why `VAR=x ... <cmd>` may not be captured as `cls`, or None."""
    bad = [k for k in env if not _HARMLESS_ENV_RE.match(k)]
    if bad:
        return f"environment assignment {bad[0]} is not mapped"
    if cls["class"] not in ENV_CLASSES:
        return "environment assignment prefix"
    locale = [k for k in env if k.startswith("LC_") or k == "LANG"]
    if locale and cls["class"] == "grep" and cls.get("tool") != "rg":
        return f"{locale[0]} changes {cls.get('tool')}'s regex semantics"
    if locale and cls["class"] == "find" and "(?i)" in repr(cls.get("expr")):
        return f"{locale[0]} changes find's case folding"
    return None


def _env_simple(tokens: list[str]) -> dict[str, Any] | None:
    """`VAR=x ... <read-only command>` with harmless variables only, as the
    command's class with `env`; None when another rule applies."""
    env: dict[str, str] = {}
    i = 0
    while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
        name, _, value = tokens[i].partition("=")
        env[name] = value
        i += 1
    if i >= len(tokens) or not all(_HARMLESS_ENV_RE.match(k) for k in env):
        return None
    got = _simple(tokens[i:])
    if got["class"] not in ENV_CLASSES or got["class"] == "ls":
        return None
    why = env_reason(got, env)
    if why:
        return _result("other", why)
    return {**got, "env": env}


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
    if why == "operator":
        wrapped = _cd_read_wrapper(command) or _jq_wrapper(command)
        if wrapped is not None:
            return wrapped
    if why is not None:
        if _compound and not _nested:
            plan, not_why = plan_compound(command)
            if plan is not None:
                return _result("compound", why, **plan)
            return _result("complex", why, compound_reason=not_why)
        return _result("complex", why)
    tokens = segments[0]
    if _ENV_ASSIGN_RE.match(tokens[0]):
        got = _env_simple(tokens)
        if got is not None:
            return got
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
# Step kinds the intent layer carries out inside a compound command from the
# graph or the file system; a compound needs at least one of them.
GRAPH_STEPS = frozenset({"pytest", "read", "grep", "git_read", "find", "files"})
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


def _cut_fields(spec: str) -> tuple | None:
    """A cut field list (`N`, `N,M`, `N-M`, `N-`, `-M`, comma-separated)
    as sorted, merged (start, end|None) ranges, or None."""
    out: list[tuple[int, int | None]] = []
    for part in spec.split(","):
        m = re.fullmatch(r"(\d*)-(\d*)|(\d+)", part)
        if not m or part == "-":
            return None
        if m.group(3):
            a = b = int(m.group(3))
        else:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else None
        if a < 1 or (b is not None and b < a):
            return None
        out.append((a, b))
    return tuple(sorted(out, key=lambda r: r[0]))


def _transform_filter(words: list[str]) -> tuple | None:
    """`wc -l|-c|-w`, `sort [-u] [-n] [-r]`, `uniq [-c]` and `cut -d X -f
    LIST [-s]` reading stdin, or None."""
    cmd, args = words[0], words[1:]
    if cmd == "wc":
        modes = {"-l": "l", "--lines": "l", "-c": "c", "--bytes": "c",
                 "-w": "w", "--words": "w"}
        if len(args) == 1 and args[0] in modes:
            return ("wc", modes[args[0]])
        return None
    if cmd == "sort":
        opts: set[str] = set()
        longs = {"--unique": "u", "--numeric-sort": "n", "--reverse": "r"}
        for a in args:
            if a in longs:
                opts.add(longs[a])
            elif a.startswith("-") and len(a) > 1 and set(a[1:]) <= set("unr"):
                opts |= set(a[1:])
            else:
                return None
        return ("sort", "n" in opts, "r" in opts, "u" in opts)
    if cmd == "uniq":
        if not args:
            return ("uniq", False)
        if args in (["-c"], ["--count"]):
            return ("uniq", True)
        return None
    if cmd == "cut":
        delim, fields, only = "\t", None, False
        i = 0
        while i < len(args):
            a = args[i]
            val = None
            if a in ("-d", "-f") and i + 1 < len(args):
                val, i = args[i + 1], i + 1
                key = a[1]
            elif a.startswith(("-d", "-f")) and len(a) > 2:
                key, val = a[1], a[2:]
            elif a.startswith(("--delimiter=", "--fields=")):
                key, val = a[2], a.split("=", 1)[1]
            elif a in ("-s", "--only-delimited"):
                only = True
                i += 1
                continue
            else:
                return None
            if key == "d":
                if len(val) != 1 or not val.isascii() or val == "\n":
                    return None
                delim = val
            else:
                fields = _cut_fields(val)
                if fields is None:
                    return None
            i += 1
        if fields is None:
            return None
        return ("cut", delim, fields, only)
    return None


# Filters that rewrite the text instead of selecting lines of it.
TRANSFORMS = frozenset({"wc", "sort", "uniq", "cut"})


def _filter(words: list[str]) -> tuple | None:
    """A pipeline stage that only filters lines of its stdin: `tail -N` /
    `tail -n +N` / `head -N` / `sed -n 'A[,B]p'` as a read range
    (("tail"|"from"|"head", N) or ("lines", A, B)), `grep` (see
    `_grep_filter`), or one that rewrites them (`_transform_filter`:
    ("wc"|"sort"|"uniq"|"cut", ...)). A `sort` or `wc` stage may have
    harmless `VAR=x` prefixes (`LC_ALL=C sort`); the filter tuple then ends
    with them as a dict. None for anything else."""
    env: dict[str, str] = {}
    while words and _ENV_ASSIGN_RE.match(words[0]):
        name, _, value = words[0].partition("=")
        if not _HARMLESS_ENV_RE.match(name):
            return None
        env[name] = value
        words = words[1:]
    if not words:
        return None
    if words[0] in TRANSFORMS:
        got = _transform_filter(words)
        if got is None:
            return None
        if got[0] in ("sort", "wc"):
            return got + (env,)
        return got
    if env:
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
    if (got is None or got["class"] != "read" or got["file"] != "-"
            or got["range"][0].startswith("bytes")):
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
    secs: float | None = None
    if words[0][2] == "timeout" and words[0][1] == "timeout":
        secs, rest, why = timeout_args([w[2] for w in words])
        if why:
            return None, why
        words = words[len(words) - len(rest):]
    raw = " ".join(w[1] for w in words)
    argv = [w[2] for w in words]
    cmd = argv[0]
    if cmd == "cd" and not env and secs is None:
        if not leading:
            return None, "cd after another step"
        if len(argv) != 2 or argv[1] in ("-", ""):
            return None, "cd form is not mapped"
        word = words[1][1]
        if word == "~" or word.startswith("~/"):
            # `cd ~` / `cd ~/x`: resolved against HOME when it runs, and
            # captured only when that is inside the root.
            if _scan(word[1:]) is not None:
                return None, "cd: tilde path with an expansion"
            return {"kind": "cd", "argv": argv, "raw": raw,
                    "dir": "~" + word[1:], "tilde": True}, ""
        why = _scan(raw)
        if why is not None:
            return None, f"cd: {why}"
        return {"kind": "cd", "argv": argv, "raw": raw, "dir": argv[1]}, ""
    why = _scan(raw) or _scan(" ".join(env.values()).replace("=", " "))
    if why is not None:
        return None, f"{argv[0]}: {why}"
    if env:
        got = classify(raw, _filters=False, _compound=False)
        if got["class"] == "pytest" and secs is None:
            return {"kind": "pytest", "argv": argv, "raw": raw,
                    "cls": {**got, "env": env}}, ""
        if cmd == "ls":
            got = {"class": "ls"}
        why = env_reason(got, env) if got["class"] != "git_refs" else "git_refs"
        if why:
            return None, why
    if secs is not None and cmd in ("cd", "echo", "pwd"):
        return None, f"timeout around {cmd} is not mapped"
    extra: dict[str, Any] = {}
    if env:
        extra["env"] = env
    if secs is not None:
        extra["timeout"] = secs
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
                "paths": [a for a in argv[1:] if not a.startswith("-")],
                **extra}, ""
    got = classify(raw, _filters=False, _compound=False)
    kind = got["class"]
    if kind == "git_read":
        why = _git_step_ok(argv)
        if why:
            return None, why
    elif kind not in ("pytest", "read", "grep", "find", "files"):
        return None, got.get("reason") or kind
    if kind == "pytest" and secs is not None:
        return None, "timeout around pytest is not mapped"
    return {"kind": kind, "argv": argv, "raw": raw, "cls": got, **extra}, ""


def _split_seq(toks: list[tuple[str, str, str]], i: int, depth: int
               ) -> tuple[list[tuple], list[str], int, str]:
    """The items of a sequence starting at `toks[i]`, up to a closing `)`
    (depth > 0) or the end: ("pipe", [[words]...], [[redirs]...]),
    ("group", items, ops) for `( ... )`, or ("empty",). Returns (items,
    ops, index of the `)` or the end, why not)."""
    items: list[tuple] = []
    ops: list[str] = []
    cur: tuple | None = None
    n = len(toks)
    while i < n:
        kind, text, _val = toks[i]
        if kind == "op" and text == "(":
            if cur is not None:
                return [], [], i, "operator ( is not mapped"
            if depth >= 1:
                return [], [], i, "nested subshell is not mapped"
            sub, subops, i, why = _split_seq(toks, i + 1, depth + 1)
            if why:
                return [], [], i, why
            if i >= n or toks[i][1] != ")":
                return [], [], i, "unbalanced parenthesis"
            cur = ("group", sub, subops)
            i += 1
            continue
        if kind == "op" and text == ")":
            if depth == 0:
                return [], [], i, "unbalanced parenthesis"
            break
        if cur is not None and cur[0] == "group" and not (
                kind == "op" and text in _SEQ_OPS):
            return [], [], i, "pipeline or redirect after a subshell is not mapped"
        if kind in ("word", "redir") and cur is None:
            cur = ("pipe", [[]], [[]])
        if kind == "word":
            cur[1][-1].append(toks[i])
        elif kind == "redir":
            if i + 1 >= n or toks[i + 1][0] != "word":
                return [], [], i, f"redirect {text} without a target"
            cur[2][-1].append((text, toks[i + 1][2]))
            i += 1
        elif text in ("|", "|&"):
            if cur is None:
                return [], [], i, "empty command"
            if text == "|&":
                cur[2][-1].append(("2>&", "1"))
            cur[1].append([])
            cur[2].append([])
        elif text in _SEQ_OPS:
            items.append(cur or ("empty",))
            ops.append(text)
            cur = None
        else:
            return [], [], i, f"operator {text} is not mapped"
        i += 1
    items.append(cur or ("empty",))
    # A trailing `;` or newline ends the last command.
    while ops and ops[-1] == ";" and items[-1] == ("empty",):
        ops.pop()
        items.pop()
    return items, ops, i, ""


def _build_steps(items: list[tuple], ops: list[str]
                 ) -> tuple[list[dict[str, Any]] | None, str]:
    steps: list[dict[str, Any]] = []
    leading = True
    for item in items:
        if item[0] == "empty":
            return None, "empty command"
        if item[0] == "group":
            sub, why = _build_steps(item[1], item[2])
            if sub is None:
                return None, why
            raw = sub[0]["raw"]
            for op, s in zip(item[2], sub[1:]):
                raw += f" {op} {s['raw']}"
            steps.append({"kind": "group", "argv": ["("], "raw": f"( {raw} )",
                          "steps": sub, "ops": list(item[2]), "filters": [],
                          "stderr": None, "stdout_null": False})
            leading = False
            continue
        pipe, reds = item[1], item[2]
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
        if any(f[0] in TRANSFORMS for f in filters):
            if step["kind"] == "pytest":
                return None, "pytest piped into wc, sort, uniq or cut is not mapped"
            if step["kind"] == "grep" and step["cls"].get("tool") == "git grep":
                return None, "git grep piped into wc, sort, uniq or cut is not mapped"
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
    return steps, ""


def compound_steps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The steps of a compound plan (or a `compound` classification),
    with the steps inside subshells, in order."""
    out: list[dict[str, Any]] = []
    for s in plan.get("steps") or []:
        if s["kind"] == "group":
            out.extend(compound_steps(s))
        else:
            out.append(s)
    return out


def plan_compound(command: str) -> tuple[dict[str, Any] | None, str]:
    """A command line the intent layer can carry out step by step, as
    `{"steps": [...], "ops": [...]}`, or (None, why not).

    The grammar is a sequence of items joined by `&&`, `||`, `;` or a
    newline. An item is a pipeline or a subshell `( SEQUENCE )` (one level,
    no pipe or redirect after it); a subshell's `cd` ends with it. A
    pipeline is one step, then any number of filters (`shell._filter`),
    with an optional `2>&1` or `2>/dev/null` (and `>/dev/null`). A step is
    a pytest, read, grep, find, `rg --files` or read-only git command,
    `echo` with literal words, `pwd`, `ls`, or a leading `cd DIR` (also
    `cd ~` and `cd ~/DIR`). At least one step must be one of
    `GRAPH_STEPS`. `&`, heredocs, other redirects, substitutions, globs and
    any other command make it (None, reason).

    Each step is `{"kind", "argv", "raw", "filters": [range...],
    "stderr": "merge"|"null"|None, "stdout_null": bool}` plus, for the
    classified kinds, `cls` (the step's own `classify` result), `env`
    (harmless `VAR=x` prefixes) and `timeout` (seconds of a `timeout N`
    wrapper). A subshell is a step of kind "group" with its own `steps`
    and `ops`."""
    toks, why = _lex(command)
    if toks is None:
        return None, why
    items, ops, _i, why = _split_seq(toks, 0, 0)
    if why:
        return None, why
    steps, why = _build_steps(items, ops)
    if steps is None:
        return None, why
    if not any(s["kind"] in GRAPH_STEPS for s in compound_steps({"steps": steps})):
        return None, "no pytest, read, grep, find or git step"
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
