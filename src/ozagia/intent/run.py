"""`shell`: the mapped command classes (see `ozagia.intent.shell`).

pytest output: pytest's own output tail (verbatim, `ozagia.check`'s last
`OUTPUT_TAIL_LINES` lines), then

    [ozagia] pytest <status>: exit <code>, <s> s, <p> passed, <f> failed, <e> error, <k> skipped
    [ozagia] failed test:<rel>::<qual>[<param>]: <message>
    [ozagia]   frame sym:<rel>::<qual> <file>:<line>
    [ozagia] collection error file:<rel>: <message>

With `[tests] select = "affected"` or `[tests] cache = true`
(`ozagia.intent.affected`), a pytest with no test selector may run only the
affected or uncached tests; the line after the head then says what ran and
what did not (`[ozagia] ran N affected tests of M; not run: ...; run all:
<cmd>`, or `[ozagia] ran all tests: <reason>`), followed by `cached pass`
lines, and slow and flaky tests come last.
A read-like command becomes a read of the same range; a search-like command
becomes a grep; `ruff check` runs ruff itself and appends the node keys
of its findings (`run_ruff`); a `jq` command in the subset of `ozagia.core.jq` prints
jq's exact output (`run_jq`); a ref-moving git command is refused.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from ozagia.intent.common import (
    IntentError,
    Unhandled,
    preflight_stop,
    one_line,
    root_of,
    tag,
)
from ozagia.intent.shell import classify

# The timeout when the model gives none: OpenCode's built-in shell default
# (2 min) and the native loop's `harness.tools.DEFAULT_SHELL_TIMEOUT_MS`, so
# a captured pytest is cut off when the built-in would have been.
DEFAULT_TIMEOUT_S = 120
# The shortest timeout a deadline clamps a captured pytest to (the native
# loop's plain shell uses the same floor).
MIN_TIMEOUT_S = 0.1


def refusal(command: str) -> str:
    """The tool error for a ref-moving git command (`git_refs`)."""
    return f"Refused by ozagia: {one_line(command)} changes git refs"


def _workdir(root: Path, wd: Any) -> Path:
    if not wd:
        return root
    p = Path(str(wd))
    if not p.is_absolute():
        p = root / p
    real = Path(os.path.realpath(p))
    if real != root and root not in real.parents:
        raise Unhandled(f"workdir {wd} is outside the project root")
    if not real.is_dir():
        raise Unhandled(f"workdir {wd} is not a directory")
    return real


def _inside(root: Path, p: Path) -> Path:
    real = Path(os.path.realpath(p))
    if real != root and root not in real.parents:
        raise Unhandled(f"{p} is outside the project root")
    return real


def _executable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _python_for(cls: dict[str, Any], wd: Path) -> str:
    """The interpreter the command names, or Unhandled."""
    tok = cls.get("python")
    if tok:
        if "/" in tok:
            p = Path(tok) if Path(tok).is_absolute() else wd / tok
            if not _executable(p):
                raise Unhandled(f"{tok} is not an executable")
            return str(p)
        found = shutil.which(tok)
        if not found:
            raise Unhandled(f"{tok} is not on PATH")
        return found
    script = cls.get("script") or "pytest"
    if "/" in script:
        sp = Path(script) if Path(script).is_absolute() else wd / script
    else:
        found = shutil.which(script)
        if not found:
            raise Unhandled(f"{script} is not on PATH")
        sp = Path(found)
    try:
        with open(sp, "rb") as fh:
            first = fh.readline(512).decode("utf-8", errors="replace").strip()
    except OSError:
        raise Unhandled(f"cannot read {script}") from None
    if not first.startswith("#!"):
        raise Unhandled(f"{script} has no interpreter line")
    parts = first[2:].split()
    if len(parts) == 2 and os.path.basename(parts[0]) == "env":
        interp = shutil.which(parts[1]) or ""
    elif len(parts) == 1:
        interp = parts[0]
    else:
        raise Unhandled(f"{script}: interpreter line {first!r} is not mapped")
    if not os.path.basename(interp).startswith("python") or not _executable(
            Path(interp)):
        raise Unhandled(f"{script}: interpreter {interp!r} is not a python")
    return interp


def _verbosity(extra: list[str]) -> list[str]:
    """`extra` with the verbosity flags folded into one, compensating for
    the `-q` that `check.run_tests` always passes."""
    net = 0
    kept: list[str] = []
    for tok in extra:
        if tok in ("-q", "--quiet"):
            net -= 1
        elif tok in ("-v", "--verbose"):
            net += 1
        elif (tok.startswith("-") and not tok.startswith("--") and len(tok) > 1
              and set(tok[1:]) <= set("vqxsl")):
            net += tok.count("v") - tok.count("q")
            rest = "".join(c for c in tok[1:] if c not in "vq")
            if rest:
                kept.append("-" + rest)
        else:
            kept.append(tok)
    need = net + 1
    if need > 0:
        kept.append("-" + "v" * need)
    elif need < 0:
        kept.append("-" + "q" * -need)
    return kept


def run_pytest(memory: Any, cls: dict[str, Any], inp: dict[str, Any],
               wd: Path) -> dict[str, Any]:
    from ozagia.check import OUTPUT_TAIL_LINES, run_tests
    root = root_of(memory)
    python = _python_for(cls, wd)
    if wd != root and cls.get("path_options"):
        raise Unhandled("path options with a workdir other than the root")
    selectors = []
    for s in cls["selectors"]:
        part, sep, rest = s.partition("::")
        p = Path(part)
        p = Path(os.path.normpath(p if p.is_absolute() else wd / p))
        _inside(root, p)
        rel = os.path.relpath(p, root)
        selectors.append(Path(rel).as_posix() + sep + rest)
    if not selectors and wd != root:
        selectors.append(Path(os.path.relpath(wd, root)).as_posix())
    timeout = inp.get("timeout")
    try:
        timeout_s = float(timeout) / 1000 if timeout else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    remaining = inp.get("_deadline_s_remaining")
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
        timeout_s = max(MIN_TIMEOUT_S, min(timeout_s, float(remaining)))
    env = inp.get("_test_env")
    if isinstance(cls.get("env"), dict) and cls["env"]:
        if wd != root:
            raise Unhandled("environment assignments with a workdir other "
                            "than the root")
        base_env = env if isinstance(env, dict) else dict(os.environ)
        env = {**base_env, **cls["env"]}
    preflight_stop(inp)
    from ozagia.intent import affected as A
    plan = A.plan(memory, cls, inp, root=root, python=python,
                  selectors=selectors,
                  env=env if isinstance(env, dict) else None)
    run_selectors = (selectors if plan is None or plan.selectors is None
                     else plan.selectors)
    if plan is not None and plan.nothing_to_run:
        res = None
    else:
        res = run_tests(root, run_selectors, python=python, timeout_s=timeout_s,
                        extra_args=_verbosity(list(cls["extra_args"])),
                        tail_lines=int(OUTPUT_TAIL_LINES
                                       if cls.get("tail_lines") is None
                                       else cls["tail_lines"]),
                        **({"env": env, "inherit": False}
                           if isinstance(env, dict) else {}))
    sel_head: list[str] = []
    sel_tail: list[str] = []
    meta: dict[str, Any] = {}
    if plan is not None:
        sel_head, sel_tail, meta = A.finish(plan, res)
    if res is None:
        lines = [tag("pytest not run: every selected test has a cached pass "
                     "(exit 0)"), *sel_head, *sel_tail]
        return {"intent": "shell_pytest", "output": "\n".join(lines),
                "keys": [], "exit_code": 0, "meta": meta}
    c = res["counts"]
    out = res.get("output_tail") or ""
    filt = cls.get("output_filter")
    if filt and filt[0] in ("tail", "head") and filt[1] >= 0:
        rows = out.splitlines()
        rows = rows[-filt[1]:] if filt[0] == "tail" and filt[1] else (
            rows[:filt[1]] if filt[0] == "head" else [])
        out = "\n".join(rows)
    lines = [out]
    head = (f"pytest {res['status']}: exit {res['exit_code']}, "
            f"{res['seconds']} s, {c['passed']} passed, {c['failed']} failed, "
            f"{c['error']} error, {c['skipped']} skipped")
    if res["collection_errors"]:
        head += f", {len(res['collection_errors'])} collection errors"
    lines.append(tag(head))
    lines.extend(sel_head)
    if res.get("error"):
        lines.append(tag(f"error: {res['error']}"))
    keys: list[str] = []

    def frames(rec: dict[str, Any]) -> None:
        seen: set[str] = set()
        for fr in rec.get("frames") or []:
            if fr["key"] in seen:
                continue
            seen.add(fr["key"])
            keys.append(fr["key"])
            lines.append(tag(f"  frame {fr['key']} {fr['file']}:{fr['line']}"))

    for err in res["collection_errors"]:
        keys.append(err["key"])
        lines.append(tag(f"collection error {err['key']}: "
                         f"{one_line(err['message'])}"))
        frames(err)
    for t in res["tests"]:
        if t["outcome"] not in ("failed", "error"):
            continue
        keys.append(t["key"])
        param = f"[{t['param']}]" if t.get("param") else ""
        lines.append(tag(f"{t['outcome']} {t['key']}{param}: "
                         f"{one_line(t['message'])}"))
        frames(t)
    lines.extend(sel_tail)
    out_res: dict[str, Any] = {
        "intent": "shell_pytest", "output": "\n".join(lines),
        "keys": list(dict.fromkeys(keys)), "exit_code": res["exit_code"]}
    if meta:
        out_res["meta"] = meta
    return out_res


def run_jq(memory: Any, cls: dict[str, Any], path: Path) -> dict[str, Any]:
    """A `jq` command in `ozagia.core.jq`'s subset: jq's exact stdout.
    Anything the subset does not reproduce (a jq error, an exponent,
    a non-JSON input) is left to the real jq."""
    from ozagia.core import jq as J
    from ozagia.core import structured as S
    if not path.is_file():
        raise Unhandled(f"not a regular file: {path}")
    # No jq: the shell says `command not found`, and the agent should see
    # that rather than learn to rely on a jq this machine lacks.
    if J.jq_version() is None:
        raise Unhandled("jq is not installed")
    try:
        if path.stat().st_size > S.MAX_QUERY_BYTES:
            raise Unhandled(f"{path} is larger than {S.MAX_QUERY_BYTES} bytes")
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise Unhandled(f"cannot read {path} as UTF-8: {e}") from None
    try:
        out = J.run_text(text, cls["program"], raw=cls["raw"],
                         compact=cls["compact"], sort_keys=cls["sort_keys"],
                         numbers=J.number_policy())
    except J.Refused as e:
        raise Unhandled(f"jq: {e}") from None
    rel = os.path.relpath(path, root_of(memory)).replace(os.sep, "/")
    tokens = J.pointer_of(cls["program"])
    keys = [S.node_key("json", rel, tokens)] if tokens is not None else []
    return {"intent": "shell_jq", "output": out, "keys": keys}


def _ruff_fixes(root: Path, where: list[Path]) -> bool:
    """Whether the ruff configuration nearest to any of `where` sets
    `fix = true` (then `ruff check` would change files)."""
    from ozagia.intent.lint import _toml, ruff_config
    for p in where:
        rel = os.path.relpath(p if p.is_dir() else p.parent, root)
        rel = "" if rel == "." else Path(rel).as_posix()
        cfg = ruff_config(root, (rel + "/" if rel else "") + "_")
        if cfg is None:
            continue
        data = _toml(cfg) or {}
        table = (data.get("tool") or {}).get("ruff") if cfg.name == "pyproject.toml" else data
        if isinstance(table, dict) and table.get("fix") is True:
            return True
    return False


# Key lines appended to a captured `ruff check`.
RUFF_KEY_LINES = 30


def run_ruff(memory: Any, cls: dict[str, Any], inp: dict[str, Any],
             wd: Path) -> dict[str, Any]:
    """`ruff check ...` run by ruff itself: its exact output (stdout and
    stderr as one stream), then `[ozagia] lint <rule> <file>:<line> <key>`
    per finding (from a second run with `--output-format json`) and
    `[ozagia] exit <n>` when ruff exits non-zero."""
    import subprocess
    from ozagia.intent.common import Keyer
    from ozagia.intent.lint import _ruff_findings
    root = root_of(memory)
    env = inp.get("_test_env") if isinstance(inp.get("_test_env"), dict) else None
    tool = cls["tool"]
    if "/" in tool:
        exe = Path(tool) if Path(tool).is_absolute() else wd / tool
        if not _executable(exe):
            raise Unhandled(f"{tool} is not an executable")
        exe_s = str(exe)
    else:
        found = shutil.which(tool, path=(env or os.environ).get("PATH"))
        if not found:
            raise Unhandled(f"{tool} is not on PATH")
        exe_s = found
    where = [_inside(root, Path(p) if Path(p).is_absolute() else wd / p)
             for p in cls["paths"]] or [wd]
    if _ruff_fixes(root, [wd, *where]):
        raise Unhandled("the ruff configuration sets fix = true")
    timeout = inp.get("timeout")
    try:
        timeout_s = float(timeout) / 1000 if timeout else DEFAULT_TIMEOUT_S
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    remaining = inp.get("_deadline_s_remaining")
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
        timeout_s = max(MIN_TIMEOUT_S, min(timeout_s, float(remaining)))
    args = list(cls["argv"][2:])
    preflight_stop(inp)
    t0 = time.monotonic()
    try:
        p = subprocess.run([exe_s, "check", "--no-fix", *args], cwd=str(wd),
                           env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        text = (e.output or b"").decode("utf-8", errors="replace").rstrip("\n")
        out = (text + "\n" if text else "") + tag(
            f"timed out after {round(timeout_s, 1)} s") + "\n" + tag("exit 124")
        return {"intent": "shell_ruff", "output": out, "keys": [],
                "exit_code": 124}
    except OSError as e:
        raise Unhandled(f"{tool}: {e}") from None
    text = p.stdout.decode("utf-8", errors="replace").rstrip("\n")
    lines = [text] if text else []
    keys: list[str] = []
    # The node keys: the same check once more as JSON (ruff's cache makes
    # it cheap), within what is left of the timeout.
    left = timeout_s - (time.monotonic() - t0)
    json_args = _without_output_format(args)
    findings: list[dict[str, Any]] = []
    if p.returncode == 1 and left > 0:
        try:
            q = subprocess.run([exe_s, "check", "--no-fix", "--output-format",
                                "json", *json_args], cwd=str(wd), env=env,
                               stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=left)
            findings = _ruff_findings(root, q.stdout.decode("utf-8", "replace"))
        except (subprocess.TimeoutExpired, OSError):
            findings = []
    keyers: dict[str, Keyer] = {}
    for f in findings[:RUFF_KEY_LINES]:
        keyer = keyers.setdefault(f["rel"], Keyer(memory, f["rel"]))
        try:
            key = keyer.key(f["line"])
        except Exception:  # noqa: BLE001 - the file key instead
            key = f"file:{f['rel']}"
        if key not in keys:
            keys.append(key)
        lines.append(tag(f"lint {f['rule']} {f['rel']}:{f['line']} {key}"))
    if len(findings) > RUFF_KEY_LINES:
        lines.append(tag(f"lint: {len(findings) - RUFF_KEY_LINES} more findings"))
    if p.returncode != 0:
        lines.append(tag(f"exit {p.returncode}"))
    return {"intent": "shell_ruff", "output": "\n".join(lines), "keys": keys,
            "exit_code": p.returncode}


def _without_output_format(args: list[str]) -> list[str]:
    """ruff check arguments without `--output-format` (before `--`)."""
    out: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            out.extend(args[i:])
            break
        if tok == "--output-format":
            i += 2
            continue
        if not tok.startswith("--output-format="):
            out.append(tok)
        i += 1
    return out


def handle_shell(memory: Any, inp: dict[str, Any]) -> dict[str, Any]:
    from ozagia.intent.read import read_range
    command = inp.get("command")
    if not isinstance(command, str):
        raise IntentError("command must be a string")
    cls = classify(command)
    kind = cls["class"]
    if kind == "git_refs":
        raise IntentError(refusal(command), intent="shell_refused")
    if kind not in ("pytest", "read", "grep", "ls", "find", "files", "jq",
                    "compound", "ruff"):
        raise Unhandled(cls.get("compound_reason") or cls.get("reason")
                        or kind)
    root = root_of(memory)
    wd = _workdir(root, inp.get("workdir"))
    if kind == "files":
        # `rg --files` runs rg itself; the compound runner already carries
        # out one step with its status and timeout.
        from ozagia.intent.shell import plan_compound
        plan, why = plan_compound(command)
        if plan is None:
            raise Unhandled(why)
        kind, cls = "compound", {**cls, **plan}
    if kind == "compound":
        from ozagia.intent.compound import run_compound
        res = run_compound(memory, cls, inp, wd)
        if cls["class"] == "files":
            res["intent"] = "shell_files"
        return res
    if "cd" in cls:
        p = Path(cls["cd"])
        wd = _workdir(root, p if p.is_absolute() else wd / p)
    if kind == "pytest":
        return run_pytest(memory, cls, inp, wd)
    if kind == "ruff":
        return run_ruff(memory, cls, inp, wd)
    if kind == "read":
        p = Path(cls["file"])
        p = _inside(root, p if p.is_absolute() else wd / p)
        res = read_range(memory, p, cls["range"], numbering=cls.get("number"),
                         outline=cls.get("tool") == "cat")
        preflight_stop(inp)
        return res
    if kind == "jq":
        p = Path(cls["file"])
        p = _inside(root, p if p.is_absolute() else wd / p)
        res = run_jq(memory, cls, p)
        preflight_stop(inp)
        return res
    if kind == "ls":
        p = Path(cls["path"])
        candidate = p if p.is_absolute() else wd / p
        target = _inside(root, candidate)
        if not target.is_dir():
            raise Unhandled("ls capture requires an existing directory")
        try:
            with os.scandir(target) as scan:
                names = sorted(entry.name for entry in scan
                               if not entry.name.startswith("."))
        except OSError as e:
            raise Unhandled(f"ls directory cannot be read: {e}") from None
        if len(names) > 2000:
            raise Unhandled("ls output exceeds capture limit")
        preflight_stop(inp)
        return {"intent": "shell_ls", "output": "\n".join(names), "keys": []}
    if kind == "find":
        from ozagia.intent.shellfs import prepare_find, run_find
        env = inp.get("_test_env")
        found = run_find(cls, prepare_find(
            root, wd, cls, env if isinstance(env, dict) else None))
        preflight_stop(inp)
        return {"intent": "shell_find", "output": "\n".join(found),
                "keys": []}
    from ozagia.intent.search import (
        render_shell_grep,
        shell_grep_records,
        shell_grep_resolve,
    )
    resolved = shell_grep_resolve(root, cls, wd)
    preflight_stop(inp)
    try:
        recs = shell_grep_records(memory, cls, wd, resolved)
        res = render_shell_grep(memory, cls["pattern"], recs)
    except IntentError as e:
        raise Unhandled(str(e)) from None
    res["intent"] = "shell_grep"
    return res


__all__ = ["handle_shell", "refusal", "run_jq", "run_pytest", "run_ruff"]
