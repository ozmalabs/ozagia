"""ozagia.check — run tests and report results as graph keys.

`run_tests` runs pytest on a target repository in its own process group, reads
the junit XML report and returns one record per test case, keyed by the stable
node keys of `ozagia.core.keys`. Each failure's traceback is mapped to the
repository symbols it passed through, so a failure can be attached to the
graph nodes that took part in it.

    python -m ozagia.check tests/test_x.py --root <repo> --json
    python -m ozagia.check test:tests/test_x.py::TestA.test_b --root <repo>

The run never writes into the target beyond what pytest itself does:
`-p no:cacheprovider` disables `.pytest_cache`, `PYTHONDONTWRITEBYTECODE=1`
disables `__pycache__`, and the junit XML goes to a temporary directory
outside the repository.

junit mapping (pytest is forced to `junit_family=xunit1`, which writes the
`file` and `line` attributes):

  * pytest's `classname` is the test file's dotted path (".py" dropped, "/"
    replaced by ".") followed by any enclosing classes; `name` is the function
    name with its parameter id, `test_x[a-b]`.
  * With a `file` attribute that names a file under the root, the classes are
    what `classname` has after the file's dotted path. Otherwise the longest dotted prefix of `classname`
    that names an existing `<prefix>.py` under the root is the file; if none
    exists, trailing parts that start with an upper-case letter are taken as
    classes and the rest as the file (`resolved` is False).
  * The qualname is the classes and the function name joined by "." (the
    convention of the graph's Test nodes); a parameter id goes to `param`, so
    every parametrisation of one function shares one key.
  * An `error` element whose message starts with "collection failure" is a
    collection error. It goes to `collection_errors`, not to `tests` or
    `counts`. "collection skipped" (a module-level skip) is a skipped test
    keyed by the file.
  * A test with a teardown error after it ran can appear as two test cases;
    both are kept, as pytest reports them.

Frames: lines of the form `path:line:` (pytest's long and short styles) and
`File "path", line N` (`--tb=native`) in the failure text. A frame whose file
is inside the root, exists, and is not under a directory in
`GENERIC_SKIP_DIRS` (`.venv`, `node_modules`, ...) is kept; the others are
dropped and counted in `frames_dropped`. A kept frame in a Python file maps to
the innermost enclosing def or class by line span (decorators included). The
key is `test:` when that def is a test by the graph's rule (a `test_*` function
at module level or inside a `Test*` class), else `sym:`; a module-level line or
a non-Python file maps to `file:`.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from ozagia.config import GENERIC_SKIP_DIRS
from ozagia.core.keys import file_key, parse_key, symbol_key, test_key

KILL_GRACE_S = 5
OUTPUT_TAIL_LINES = 40
MESSAGE_MAX_CHARS = 300

# pytest exit codes (pytest.ExitCode) and the status names reported for them.
EXIT_STATUS = {
    0: "passed",
    1: "failed",
    2: "interrupted",
    3: "internal_error",
    4: "usage_error",
    5: "no_tests_collected",
}

_FRAME_RE = re.compile(r"^(?P<path>[^\s:>][^:\n]*?):(?P<line>\d+):(?:[ \t]|$)",
                       re.M)
_NATIVE_FRAME_RE = re.compile(r'^\s*File "(?P<path>[^"\n]+)", line (?P<line>\d+)',
                              re.M)

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_SPAN_CACHE: dict[tuple[str, int, int], list[tuple[int, int, str, bool]]] = {}
_SPAN_CACHE_MAX = 512


# --------------------------------------------------------------------------- #
# Python symbol spans
# --------------------------------------------------------------------------- #

def _parse_spans(path: Path) -> list[tuple[int, int, str, bool]]:
    """(start, end, qualname, is_test) for every def/class in a Python file.

    `start` is the first decorator line when there are decorators. Qualnames
    follow the graph's rule: the dotted chain of enclosing defs and classes,
    including defs nested in non-definition statements (`if`, `try`, ...).
    """
    try:
        return _source_spans(path.read_bytes())
    except OSError:
        return []


def _source_spans(source: str | bytes) -> list[tuple[int, int, str, bool]]:
    """`_parse_spans` for a source text; [] when it does not parse."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    return _tree_spans(tree)


def _tree_spans(tree: ast.Module) -> list[tuple[int, int, str, bool]]:
    out: list[tuple[int, int, str, bool]] = []
    # (node, qualname prefix, inside a Test* class chain)
    stack: list[tuple[ast.AST, str, bool]] = [
        (n, "", False) for n in reversed(tree.body)]
    while stack:
        node, prefix, in_test_class = stack.pop()
        if isinstance(node, _DEF_NODES):
            qual = f"{prefix}.{node.name}" if prefix else node.name
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            end = int(getattr(node, "end_lineno", None) or node.lineno)
            is_test = (not isinstance(node, ast.ClassDef)
                       and node.name.startswith("test_")
                       and (prefix == "" or in_test_class))
            out.append((start, end, qual, is_test))
            child_test = in_test_class or (
                isinstance(node, ast.ClassDef) and node.name.startswith("Test"))
            stack.extend((c, qual, child_test) for c in reversed(node.body))
        else:
            stack.extend((c, prefix, in_test_class)
                         for c in reversed(list(ast.iter_child_nodes(node))))
    return out


def _spans_for(path: Path) -> list[tuple[int, int, str, bool]]:
    try:
        st = path.stat()
    except OSError:
        return []
    cache_key = (str(path), st.st_mtime_ns, st.st_size)
    spans = _SPAN_CACHE.get(cache_key)
    if spans is None:
        if len(_SPAN_CACHE) >= _SPAN_CACHE_MAX:
            _SPAN_CACHE.clear()
        spans = _parse_spans(path)
        _SPAN_CACHE[cache_key] = spans
    return spans


def _text_spans(source: str, tree: ast.Module | None = None
                ) -> list[tuple[int, int, str, bool]]:
    cache_key = ("\0source", len(source), hash(source))
    spans = _SPAN_CACHE.get(cache_key)
    if spans is None:
        if len(_SPAN_CACHE) >= _SPAN_CACHE_MAX:
            _SPAN_CACHE.clear()
        spans = _tree_spans(tree) if tree is not None else _source_spans(source)
        _SPAN_CACHE[cache_key] = spans
    return spans


def symbol_at(root: str | Path, rel: str, line: int, *,
              end_line: int | None = None, source: str | None = None,
              tree: ast.Module | None = None) -> str:
    """The key of the innermost def/class of `rel` that contains `line`
    (and `end_line`, when given: the lines `line..end_line`).

    `file:<rel>` for a module-level line or a non-Python file. `source` is the
    file's text to use instead of the file on disk (an edit not yet written);
    `tree` is its parsed module, when the caller has it already.
    """
    path = Path(root) / rel
    if path.suffix.lower() != ".py":
        return file_key(rel)
    last = line if end_line is None else max(line, int(end_line))
    spans = (_text_spans(source, tree) if source is not None
             else _spans_for(path))
    best: tuple[int, int, str, bool] | None = None
    for span in spans:
        start, end, _, _ = span
        if start <= line and last <= end and (
                best is None or (start, -end) > (best[0], -best[1])):
            best = span
    if best is None:
        return file_key(rel)
    return test_key(rel, best[2]) if best[3] else symbol_key(rel, best[2])


# --------------------------------------------------------------------------- #
# Traceback frames
# --------------------------------------------------------------------------- #

def _repo_rel(root: Path, raw_path: str) -> str | None:
    """The repo-relative POSIX path of a traceback path, or None if outside."""
    p = Path(raw_path)
    full = p if p.is_absolute() else root / p
    try:
        full = full.resolve()
        rel = full.relative_to(root)
    except (ValueError, OSError, RuntimeError):
        return None
    if any(part in GENERIC_SKIP_DIRS for part in rel.parts):
        return None
    if not full.is_file():
        return None
    return rel.as_posix()


def parse_frames(root: str | Path, text: str) -> tuple[list[dict[str, Any]], int]:
    """Repository frames in a traceback text, outermost first, and the count
    of frames dropped because they are outside the repository."""
    root = Path(root).resolve()
    found: list[tuple[int, str, int]] = []
    for rx in (_FRAME_RE, _NATIVE_FRAME_RE):
        for m in rx.finditer(text or ""):
            found.append((m.start(), m.group("path"), int(m.group("line"))))
    found.sort()
    frames: list[dict[str, Any]] = []
    dropped = 0
    for _, raw_path, line in found:
        rel = _repo_rel(root, raw_path.strip())
        if rel is None:
            dropped += 1
            continue
        frames.append({"file": rel, "line": line,
                       "key": symbol_at(root, rel, line)})
    return frames, dropped


# --------------------------------------------------------------------------- #
# junit XML
# --------------------------------------------------------------------------- #

def _short_message(elem: ET.Element) -> str:
    msg = elem.get("message") or ""
    if not msg.strip():
        msg = elem.text or ""
    for line in msg.splitlines():
        if line.strip():
            return line.strip()[:MESSAGE_MAX_CHARS]
    return ""


def _split_param(name: str) -> tuple[str, str | None]:
    idx = name.find("[")
    if idx > 0 and name.endswith("]"):
        return name[:idx], name[idx + 1:-1]
    return name, None


def _dotted(rel: str) -> str:
    return re.sub(r"\.py$", "", rel).replace("/", ".")


def _resolve_classname(root: Path, classname: str) -> tuple[str, list[str], bool]:
    """(file, classes, resolved) for a dotted junit classname without a file."""
    parts = [p for p in classname.split(".") if p]
    for i in range(len(parts), 0, -1):
        cand = "/".join(parts[:i]) + ".py"
        if (root / cand).is_file():
            return cand, parts[i:], True
    cut = len(parts)
    while cut > 1 and parts[cut - 1][:1].isupper():
        cut -= 1
    return "/".join(parts[:cut]) + ".py", parts[cut:], False


def _locate_case(root: Path, classname: str, name: str,
                 file_attr: str | None) -> dict[str, Any]:
    """file, classes, function name and param of one junit test case."""
    base, param = _split_param(name)
    resolved = True
    rel: str | None = None
    if file_attr:
        p = PurePosixPath(file_attr.replace(os.sep, "/"))
        if p.is_absolute():
            try:
                rel = Path(str(p)).resolve().relative_to(root).as_posix()
            except (ValueError, OSError):
                rel = None
        else:
            rel = p.as_posix()
        if rel is not None and not (root / rel).is_file():
            # pytest's rootdir is not the root; the classname may still resolve.
            rel = None
    if rel is not None:
        mod = _dotted(rel)
        if classname == mod or classname.startswith(mod + "."):
            rest = classname[len(mod):].lstrip(".")
            classes = rest.split(".") if rest else []
        else:
            _, classes, _ = _resolve_classname(root, classname)
    else:
        rel, classes, resolved = _resolve_classname(root, classname)
    return {"file": rel, "classes": classes, "base": base, "param": param,
            "resolved": resolved}


def parse_junit(root: str | Path, xml_text: str) -> dict[str, Any]:
    """Test records and collection errors from a pytest junit XML report."""
    root = Path(root).resolve()
    tests: list[dict[str, Any]] = []
    collection_errors: list[dict[str, Any]] = []
    try:
        doc = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return {"tests": tests, "collection_errors": collection_errors,
                "error": f"junit XML does not parse: {e}"}
    for case in doc.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        try:
            seconds = float(case.get("time") or 0.0)
        except ValueError:
            seconds = 0.0
        detail = failure if failure is not None else error

        if error is not None and (error.get("message") or "").startswith(
                "collection failure"):
            # classname is "" and name is the file's dotted path.
            loc = _locate_case(root, f"{classname}.{name}".strip("."), "",
                               case.get("file"))
            frames, dropped = parse_frames(root, error.text or "")
            collection_errors.append({
                "key": file_key(loc["file"]),
                "nodeid": loc["file"],
                "file": loc["file"],
                "message": _first_error_line(error.text or "")
                or _short_message(error),
                "frames": frames,
                "frames_dropped": dropped,
            })
            continue
        if (skipped is not None and not classname
                and (skipped.get("message") or "").startswith(
                    "collection skipped")):
            loc = _locate_case(root, name, "", case.get("file"))
            tests.append({
                "key": file_key(loc["file"]), "nodeid": loc["file"],
                "file": loc["file"], "qualname": "", "param": None,
                "outcome": "skipped", "seconds": seconds,
                "message": _short_message(skipped), "frames": [],
                "frames_dropped": 0, "resolved": loc["resolved"],
            })
            continue

        loc = _locate_case(root, classname, name, case.get("file"))
        qualname = ".".join([*loc["classes"], loc["base"]])
        if failure is not None:
            outcome = "failed"
        elif error is not None:
            outcome = "error"
        elif skipped is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        message = ""
        frames: list[dict[str, Any]] = []
        dropped = 0
        if detail is not None:
            message = _short_message(detail)
            frames, dropped = parse_frames(root, detail.text or "")
        elif skipped is not None:
            message = _short_message(skipped)
        rec: dict[str, Any] = {
            "key": test_key(loc["file"], qualname),
            "nodeid": "::".join([loc["file"], *loc["classes"], name]),
            "file": loc["file"],
            "qualname": qualname,
            "param": loc["param"],
            "outcome": outcome,
            "seconds": seconds,
            "message": message,
            "frames": frames,
            "frames_dropped": dropped,
            "resolved": loc["resolved"],
        }
        line = case.get("line")
        if line and line.isdigit():
            rec["line"] = int(line) + 1  # junit `line` is 0-based
        tests.append(rec)
    return {"tests": tests, "collection_errors": collection_errors}


def _first_error_line(text: str) -> str:
    """The first `E   ...` line of a pytest long representation."""
    for line in text.splitlines():
        if line.startswith("E ") and line[1:].strip():
            return line[1:].strip()[:MESSAGE_MAX_CHARS]
    return ""


# --------------------------------------------------------------------------- #
# Running pytest
# --------------------------------------------------------------------------- #

def default_python(root: str | Path) -> str:
    venv = Path(root) / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_tests(root: str | Path, selectors: list[str], *,
              python: str | None = None, timeout_s: float = 300,
              env: dict | None = None, inherit: bool = True,
              extra_args: Iterable[str] = ()) -> dict[str, Any]:
    """Run pytest on `selectors` in `root` and return the results as keys.

    The environment is this process's, updated with `env`; with
    `inherit=False` it is `env` alone (an empty one without it). Either way
    `PYTHONDONTWRITEBYTECODE=1` and `PWD=<root>` are set.

    `ok` is True only when pytest exits 0. Exit code 5 (no tests collected)
    has `status` "no_tests_collected" and `ok` False. On timeout the process
    group gets SIGTERM, then SIGKILL after `KILL_GRACE_S` seconds.
    """
    root = Path(root).resolve()
    python = python or default_python(root)
    tmpdir = Path(tempfile.mkdtemp(prefix="ozagia_check_"))
    xml_path = tmpdir / "junit.xml"
    cmd = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
           f"--junitxml={xml_path}", "-o", "junit_family=xunit1",
           *list(extra_args), *list(selectors)]
    run_env = dict(os.environ) if inherit else {}
    if env:
        run_env.update({str(k): str(v) for k, v in env.items()})
    run_env["PYTHONDONTWRITEBYTECODE"] = "1"
    run_env["PWD"] = str(root)

    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(root), env=run_env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc.pid, signal.SIGTERM)
            try:
                out, _ = proc.communicate(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                _kill_group(proc.pid, signal.SIGKILL)
                try:
                    out, _ = proc.communicate(timeout=KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    # A process that left the group still holds the pipe.
                    proc.kill()
                    out = b""
            # Members that ignored SIGTERM while pytest exited.
            _kill_group(proc.pid, signal.SIGKILL)
        exit_code = proc.returncode
        seconds = round(time.monotonic() - t0, 3)
        output = (out or b"").decode("utf-8", errors="replace")
        xml_text = xml_path.read_text(encoding="utf-8") if xml_path.exists() else ""
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    parsed = (parse_junit(root, xml_text) if xml_text
              else {"tests": [], "collection_errors": []})
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for t in parsed["tests"]:
        counts[t["outcome"]] += 1
    if timed_out:
        status = "timed_out"
    else:
        status = EXIT_STATUS.get(exit_code, f"exit_{exit_code}")
    result: dict[str, Any] = {
        "ok": exit_code == 0 and not timed_out,
        "status": status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "seconds": seconds,
        "counts": counts,
        "tests": parsed["tests"],
        "collection_errors": parsed["collection_errors"],
        "output_tail": "\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:]),
        "command": cmd,
    }
    if status == "no_tests_collected":
        result["error"] = "no tests collected (pytest exit code 5)"
    elif timed_out:
        result["error"] = f"timed out after {timeout_s} s; process group killed"
    elif not xml_text:
        result["error"] = "pytest wrote no junit report"
    elif parsed.get("error"):
        result["error"] = parsed["error"]
    return result


# --------------------------------------------------------------------------- #
# Keys -> selectors
# --------------------------------------------------------------------------- #

def _is_test_file(rel: str) -> bool:
    name = PurePosixPath(rel).name
    return name.endswith(".py") and (
        name.startswith("test_") or name.endswith("_test.py"))


def resolve_selectors(root: str | Path, keys: list[str]) -> dict[str, Any]:
    """pytest selectors for graph keys, and the keys that have none.

    `test:<rel>::A.b` -> `<rel>::A::b`; `file:<rel>` of a test file -> `<rel>`.
    Every other key is returned in `ignored` with a reason.
    """
    root = Path(root).resolve()
    selectors: list[str] = []
    ignored: list[dict[str, str]] = []
    for key in keys:
        try:
            kind, *parts = parse_key(str(key))
        except ValueError:
            ignored.append({"key": str(key), "reason": "not a node key"})
            continue
        if kind == "test" and len(parts) == 2 and parts[0] and parts[1]:
            rel, qual = parts
            sel = "::".join([rel, *qual.split(".")])
        elif kind == "file" and len(parts) == 1 and parts[0]:
            rel = parts[0]
            if not _is_test_file(rel):
                ignored.append({"key": key, "reason": "not a test file"})
                continue
            sel = rel
        else:
            ignored.append({"key": key,
                            "reason": f"{kind}: keys have no test selector"})
            continue
        if not (root / rel).is_file():
            ignored.append({"key": key, "reason": f"no such file: {rel}"})
            continue
        if sel not in selectors:
            selectors.append(sel)
    return {"selectors": selectors, "ignored": ignored}


def selectors_for_keys(root: str | Path, keys: list[str]) -> list[str]:
    """pytest node ids for `test:` keys and test-file `file:` keys.

    Other keys are ignored; `resolve_selectors` reports them.
    """
    return resolve_selectors(root, keys)["selectors"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _summary(result: dict[str, Any], ignored: list[dict[str, str]]) -> str:
    c = result["counts"]
    lines = [f"{result['status']}: exit {result['exit_code']}, "
             f"{result['seconds']} s, {c['passed']} passed, {c['failed']} failed, "
             f"{c['error']} error, {c['skipped']} skipped, "
             f"{len(result['collection_errors'])} collection errors"]
    if result.get("error"):
        lines.append(f"  {result['error']}")
    for item in ignored:
        lines.append(f"  ignored {item['key']}: {item['reason']}")
    for err in result["collection_errors"]:
        lines.append(f"  COLLECT {err['file']}: {err['message']}")
    for t in result["tests"]:
        if t["outcome"] in ("failed", "error"):
            lines.append(f"  {t['outcome'].upper()} {t['nodeid']}: {t['message']}")
            for fr in t["frames"]:
                lines.append(f"      {fr['file']}:{fr['line']}  {fr['key']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m ozagia.check",
        description="Run pytest and report results as graph keys.")
    ap.add_argument("selectors", nargs="*",
                    help="pytest selectors, or test:/file: keys")
    ap.add_argument("--root", default=".")
    ap.add_argument("--python", default=None)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    plain = [s for s in args.selectors
             if not s.startswith(("test:", "file:", "sym:", "doc:", "text:"))]
    keyed = [s for s in args.selectors if s not in plain]
    resolved = resolve_selectors(args.root, keyed)
    selectors = plain + resolved["selectors"]
    if keyed and not selectors:
        print("no selectors: every key was ignored", file=sys.stderr)
        for item in resolved["ignored"]:
            print(f"  {item['key']}: {item['reason']}", file=sys.stderr)
        return 4
    result = run_tests(args.root, selectors, python=args.python,
                       timeout_s=args.timeout)
    result["ignored_keys"] = resolved["ignored"]
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(_summary(result, resolved["ignored"]))
    if result["timed_out"]:
        return 124
    return int(result["exit_code"] or 0)


if __name__ == "__main__":
    raise SystemExit(main())
