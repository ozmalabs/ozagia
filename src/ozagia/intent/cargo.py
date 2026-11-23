"""Run a captured ``cargo test`` and parse its human output.

The parser reads the stable libtest output, not nightly ``--format json``:

* ``Running unittests src/lib.rs (target/debug/deps/<name>-<hash>)`` and
  ``Doc-tests <package>`` start a target block;
* ``running N tests``, ``test <name> ... ok|ignored|FAILED`` and
  ``test result: ok. ...`` are the per-block records;
* the ``failures:`` section gives each failing test's panic location and
  message, which become ``failed test:``/``frame`` lines in the intent
  output.

Nothing here writes the project or runs anything except the typed command's
own executable. The runner mirrors :func:`ozagia.check.run_tests`: a bounded
timeout, the process group killed on timeout, and a bounded output tail.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ozagia.intent.common import Unhandled

OUTPUT_TAIL_LINES = 40
MAX_OUTPUT_LINES = 50000
KILL_GRACE_S = 3.0
MAX_BLOCKS = 500
MAX_TESTS = 20000
MAX_ERRORS = 50

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_RUNNING = re.compile(r"^\s*Running\s+(.+?)(?:\s+\((.+)\))?\s*$")
_DOC = re.compile(r"^\s*Doc-tests\s+(.+?)\s*$")
_RUNNING_N = re.compile(r"^\s*running\s+(\d+)\s+tests?\s*$")
_TEST_LINE = re.compile(r"^test\s+(.+?)\s+\.\.\.\s+(ok|ignored|FAILED|bench:.*)$")
_TEST_QUIET = re.compile(r"^(.+?)\s+---\s+(FAILED|ok|ignored)\s*$")
_RESULT = re.compile(
    r"^\s*test result:\s*(ok|FAILED)\.\s*(\d+)\s+passed;\s*"
    r"(\d+)\s+failed;\s*(\d+)\s+ignored"
    r"(?:;\s*(\d+)\s+measured)?(?:;\s*(\d+)\s+filtered out)?"
    r"(?:;\s*finished in\s+([\d.]+)s)?\s*$")
_FAIL_DETAIL = re.compile(r"^----\s+(.+?)\s+stdout\s+----\s*$")
_PANIC_A = re.compile(
    r"^thread '(.+?)' panicked at (.+?):(\d+):(\d+):\s*$")
_PANIC_B = re.compile(
    r"^thread '(.+?)' panicked at '(.+?)',\s*(.+?):(\d+):(\d+)\s*$")
_DIAG_LOC = re.compile(r"^\s*-->\s+(.+?):(\d+):(\d+)\s*$")
_DIAG_ERROR = re.compile(r"^error(?:\[E[0-9A-Za-z]+\])?:\s+(.*)$")
_COMPILE = re.compile(r"^error: could not compile\b")


class EmptyModel:
    """A stand-in when the Cargo workspace cannot be read: counts still
    parse, names and locations are not mapped to graph keys."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.targets: dict[str, Any] = {}
        self.package_dirs: dict[str, str] = {}
        self.package_lib: dict[str, str] = {}
        self.package_targets: dict[str, set[str]] = {}

    def locate(self, target_id: str, cargo_name: str) -> None:
        return None

    def doc_target_id(self, package: str) -> str:
        return f"doc:{package}"

    def is_doc(self, target_id: str) -> bool:
        return target_id.startswith("doc:")

    def package_of(self, target_id: str) -> str:
        return target_id[4:] if self.is_doc(target_id) else ""

    def target_kind(self, target_id: str) -> str:
        return "doc" if self.is_doc(target_id) else ""

    def target_name(self, target_id: str) -> str:
        return self.package_of(target_id) if self.is_doc(target_id) else ""

    def default_targets(self, packages: Any = None, *,
                        docs: bool = True) -> list[str]:
        return []


@dataclass
class CargoBlock:
    """One target's output block."""

    header: str = ""
    kind: str = "unknown"
    package: str = ""
    name: str = ""
    root: str = ""
    target_id: str = ""
    expected: int | None = None
    counts: dict[str, int] = field(
        default_factory=lambda: {"passed": 0, "failed": 0, "ignored": 0,
                                 "measured": 0, "filtered": 0})
    tests: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"header": self.header, "kind": self.kind,
                "package": self.package, "name": self.name, "root": self.root,
                "target": self.target_id, "counts": dict(self.counts),
                "seconds": self.seconds, "status": self.status,
                "tests": len(self.tests)}


def _normal_name(text: str) -> str:
    return text.replace("-", "_")


def _exe_name(exe: str) -> str:
    name = os.path.basename(exe.strip())
    if name.endswith(".exe"):
        name = name[:-4]
    # Cargo suffixes the test binary with "-<16 hex chars>".
    head, sep, tail = name.rpartition("-")
    if sep and len(tail) == 16 and all(c in "0123456789abcdef" for c in tail):
        return head
    return name


def _kind_for_path(path: str) -> str:
    p = path.strip().lstrip("./")
    if p in ("src/lib.rs", "lib.rs") or p.endswith("/src/lib.rs") \
            or p.endswith("/lib.rs"):
        return "lib"
    if (p in ("src/main.rs",) or p.endswith("/src/main.rs")
            or p.startswith("src/bin/") or "/src/bin/" in p):
        return "bin"
    if p.startswith("tests/") or "/tests/" in p:
        return "test"
    if p.startswith("benches/") or "/benches/" in p:
        return "bench"
    if p.startswith("examples/") or "/examples/" in p:
        return "example"
    return "unknown"


def _package_relative(root: str, package_dir: str) -> str:
    if package_dir and package_dir not in ("", "."):
        prefix = package_dir.rstrip("/") + "/"
        if root.startswith(prefix):
            return root[len(prefix):]
    return root


def _choose_target(model: Any, *, kind: str, path: str, exe: str,
                   package: str, used: set[str],
                   order: list[str] | None) -> str:
    """The model target a block header names, or "" when it is ambiguous."""
    if kind == "doc":
        if not package:
            return ""
        tid = model.doc_target_id(package)
        return tid if model.package_of(tid) == package else ""
    candidates: list[str] = []
    wanted_path = path.strip().lstrip("./")
    wanted_name = _normal_name(_exe_name(exe)) if exe else ""
    for tid in model.targets:
        if tid in used:
            continue
        target = model.targets[tid]
        if kind != "unknown" and target.kind != kind:
            continue
        pkg_dir = model.package_dirs.get(target.package, "")
        if wanted_path and _package_relative(target.root, pkg_dir) != wanted_path:
            continue
        if wanted_name and _normal_name(target.name) != wanted_name:
            continue
        candidates.append(tid)
    if not candidates:
        # A custom target path or a Cargo layout the model does not see: keep
        # the next expected target if the header at least matches its kind.
        if order:
            for tid in order:
                if tid in used:
                    continue
                if kind in ("unknown", model.target_kind(tid)):
                    return tid
        return ""
    if len(candidates) == 1:
        return candidates[0]
    if order:
        for tid in order:
            if tid in candidates:
                return tid
    return candidates[0]


def _line_blocks(lines: Iterable[str], model: Any,
                 order: list[str] | None) -> tuple[list[CargoBlock],
                                                   list[dict[str, Any]],
                                                   list[str]]:
    blocks: list[CargoBlock] = []
    errors: list[dict[str, Any]] = []
    notes: list[str] = []
    used: set[str] = set()
    block: CargoBlock | None = None
    failure_names: dict[tuple[str, str], list[dict[str, Any]]] = {}
    current_failure: dict[str, Any] | None = None
    pending_error: str | None = None
    seen_tests = False

    def finish_block() -> None:
        nonlocal block, current_failure
        if block is not None:
            if block.status == "":
                if block.counts["failed"]:
                    block.status = "failed"
                elif block.counts["passed"] or block.counts["ignored"]:
                    block.status = "ok"
            blocks.append(block)
        block = None
        current_failure = None

    def error(message: str, *, file: str = "", line: int = 0,
              column: int = 0) -> None:
        if len(errors) >= MAX_ERRORS:
            return
        entry = {"message": message, "file": file, "line": line,
                 "column": column}
        if file:
            entry["key"] = f"file:{file}"
        errors.append(entry)

    for raw in lines:
        line = _ANSI.sub("", raw).rstrip("\r")
        m = _RUNNING.match(line)
        if m:
            finish_block()
            inner, exe = m.group(1).strip(), (m.group(2) or "").strip()
            path = inner
            if inner.startswith("unittests "):
                path = inner[len("unittests "):].strip()
            elif inner.startswith(("tests ", "benches ", "examples ")):
                path = inner.split(" ", 1)[1].strip()
            kind = _kind_for_path(path)
            package = ""
            if exe:
                name = _exe_name(exe)
                for tid in model.targets:
                    if _normal_name(model.targets[tid].name) == _normal_name(name):
                        package = model.targets[tid].package
                        break
            tid = _choose_target(model, kind=kind, path=path, exe=exe,
                                 package=package, used=used, order=order)
            used.add(tid)
            target = model.targets.get(tid)
            block = CargoBlock(
                header=line.strip(), kind=kind, package=package,
                name=target.name if target is not None else _exe_name(exe),
                root=target.root if target is not None else path,
                target_id=tid)
            if target is not None:
                block.package = target.package
            continue
        m = _DOC.match(line)
        if m:
            finish_block()
            package = m.group(1).strip()
            tid = model.doc_target_id(package)
            used.add(tid)
            block = CargoBlock(header=line.strip(), kind="doc",
                               package=package, name=package, target_id=tid,
                               root=model.package_lib.get(package, ""))
            continue
        m = _RUNNING_N.match(line)
        if m and block is not None:
            seen_tests = True
            block.expected = int(m.group(1))
            continue
        m = _TEST_LINE.match(line)
        if m:
            seen_tests = True
            if block is None:
                block = CargoBlock(header="(no target header)")
            name, status = m.group(1).strip(), m.group(2)
            if status.startswith("bench:"):
                outcome, note = "passed", status[6:].strip()
            elif status == "ok":
                outcome, note = "passed", ""
            elif status == "ignored":
                outcome, note = "ignored", ""
            else:
                outcome, note = "failed", ""
            test = _test_record(model, block, name, outcome, note)
            block.tests.append(test)
            if outcome == "failed":
                failure_names.setdefault((block.target_id, name),
                                         []).append(test)
                current_failure = test
            continue
        m = _TEST_QUIET.match(line)
        if m and not line.startswith(("test result", "error")):
            seen_tests = True
            if block is None:
                block = CargoBlock(header="(no target header)")
            name, status = m.group(1).strip(), m.group(2)
            outcome = "passed" if status == "ok" else (
                "ignored" if status == "ignored" else "failed")
            test = _test_record(model, block, name, outcome, "")
            block.tests.append(test)
            if outcome == "failed":
                failure_names.setdefault((block.target_id, name),
                                         []).append(test)
                current_failure = test
            continue
        m = _RESULT.match(line)
        if m:
            if block is None:
                block = CargoBlock(header="(no target header)")
            block.status = "failed" if m.group(1) == "FAILED" else "ok"
            block.counts = {
                "passed": int(m.group(2)), "failed": int(m.group(3)),
                "ignored": int(m.group(4)), "measured": int(m.group(5) or 0),
                "filtered": int(m.group(6) or 0)}
            block.seconds = float(m.group(7) or 0)
            # A quiet run has only names in the failures section; the counts
            # are authoritative, so do not add synthetic tests here.
            finish_block()
            continue
        m = _FAIL_DETAIL.match(line)
        if m:
            name = m.group(1).strip()
            found = failure_names.get(
                (block.target_id if block else "", name)) or []
            current_failure = found[0] if found else None
            continue
        m = _PANIC_A.match(line)
        if m:
            name = m.group(1).strip()
            found = failure_names.get(
                (block.target_id if block else "", name)) or []
            test = current_failure or (found[0] if found else None)
            if test is not None:
                test["file"] = _relative_file(test.get("file") or "",
                                              m.group(2).strip())
                test["line"] = int(m.group(3))
                test["column"] = int(m.group(4))
                test["frames"] = [{"file": test["file"],
                                   "line": int(m.group(3)),
                                   "column": int(m.group(4))}]
                current_failure = test
            else:
                current_failure = None
            continue
        m = _PANIC_B.match(line)
        if m:
            name, message, path = m.group(1).strip(), m.group(2), m.group(3)
            found = failure_names.get(
                (block.target_id if block else "", name)) or []
            test = current_failure or (found[0] if found else None)
            if test is not None:
                test["file"] = _relative_file(test.get("file") or "",
                                              path.strip())
                test["line"] = int(m.group(4))
                test["column"] = int(m.group(5))
                test["message"] = message
                test["frames"] = [{"file": test["file"],
                                   "line": int(m.group(4)),
                                   "column": int(m.group(5))}]
                current_failure = test
            else:
                current_failure = None
            continue
        if current_failure is not None:
            stripped = line.strip()
            if stripped and not stripped.startswith(
                    ("note:", "failures:", "error:")):
                if not current_failure.get("message"):
                    current_failure["message"] = stripped
            continue
        m = _DIAG_LOC.match(line)
        if m and (pending_error is not None or not seen_tests):
            error(pending_error or "compile error", file=m.group(1),
                  line=int(m.group(2)), column=int(m.group(3)))
            pending_error = None
            continue
        m = _DIAG_ERROR.match(line)
        if m:
            message = m.group(1).strip()
            if (message.startswith(("test failed", "build failed, waiting"))
                    or re.match(r"^\d+ targets? failed", message)):
                continue
            buildish = message.startswith((
                "could not compile", "aborting due to", "package ID",
                "no test target", "failed to", "unexpected argument"))
            if seen_tests and not buildish:
                continue
            if _COMPILE.match(line) or message.startswith("aborting due to"):
                error(message)
            else:
                pending_error = message
            continue
        if line.strip().startswith("error:"):
            message = line.strip()[len("error:"):].strip()
            if message and not message.startswith(
                    ("test failed", "build failed, waiting")) and not re.match(
                        r"^\d+ targets? failed", message):
                error(message)
    finish_block()
    if pending_error:
        error(pending_error)
    # Fill the messages that only the failures list gave us.
    for tests in failure_names.values():
        for test in tests:
            if not test.get("message"):
                test["message"] = "test failed"
    if len(blocks) > MAX_BLOCKS:
        notes.append(f"output names more than {MAX_BLOCKS} test targets")
        blocks = blocks[:MAX_BLOCKS]
    return blocks, errors, notes


def _relative_file(current: str, path: str) -> str:
    """A panic path, kept relative to the repository when it already is."""
    clean = path.strip().lstrip("./")
    if clean.startswith("/"):
        return clean
    return clean or current


def _test_record(model: Any, block: CargoBlock, name: str, outcome: str,
                 note: str) -> dict[str, Any]:
    entry = model.locate(block.target_id, name) if block.target_id else None
    record: dict[str, Any] = {
        "name": name, "cargo": name, "outcome": outcome,
        "target": block.target_id, "package": block.package,
        "target_root": block.root,
    }
    if entry is not None:
        record.update({"key": entry.key, "file": entry.file,
                       "qualname": entry.qualname})
    elif block.kind == "doc":
        file, qualname = _doc_name(name, block, model)
        record.update({"key": "", "file": file, "qualname": qualname})
    else:
        record.update({"key": "", "file": "", "qualname": name})
    if note:
        record["note"] = note
    return record


def _doc_name(name: str, block: CargoBlock, model: Any) -> tuple[str, str]:
    """(file, qualname) from `path - item (line N)`."""
    path, sep, item = name.partition(" - ")
    if not sep:
        return "", name
    item = item.split(" (line ", 1)[0].strip()
    path = path.strip()
    package_dir = model.package_dirs.get(block.package, "")
    if package_dir and not path.startswith(package_dir + "/"):
        joined = f"{package_dir}/{path}" if path else package_dir
        if (model.root / joined).is_file():
            path = joined
    return path, item


def parse(text: str, model: Any, *,
          order: list[str] | None = None) -> dict[str, Any]:
    """Parse cargo's human output into blocks, tests and build errors."""
    blocks, errors, notes = _line_blocks(text.splitlines(), model, order)
    tests: list[dict[str, Any]] = []
    for block in blocks:
        tests.extend(block.tests)
    counts = {"passed": 0, "failed": 0, "ignored": 0, "measured": 0,
              "filtered": 0}
    for block in blocks:
        for key in counts:
            counts[key] += block.counts[key]
    if len(tests) > MAX_TESTS:
        notes.append(f"output names more than {MAX_TESTS} tests")
        tests = tests[:MAX_TESTS]
    unique: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for entry in errors:
        key = (entry.get("message"), entry.get("file"), entry.get("line"),
               entry.get("column"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return {"blocks": [block.to_dict() for block in blocks],
            "tests": tests, "counts": counts, "errors": unique,
            "notes": notes}


def status_for(raw: dict[str, Any], parsed: dict[str, Any], *,
               no_run: bool = False) -> str:
    """The run's status from its exit code, timeout and parsed output."""
    if raw.get("timed_out"):
        return "timed_out"
    if raw.get("exit_code") == 0:
        return "passed"
    if no_run:
        return "failed"
    failed = int((parsed.get("counts") or {}).get("failed") or 0)
    if parsed.get("errors") and not failed:
        return "build_failed"
    return "failed"


def _output_lines(output: str, tail_lines: int) -> str:
    rows = output.splitlines()
    if tail_lines > 0:
        return "\n".join(rows[-tail_lines:])
    if len(rows) <= MAX_OUTPUT_LINES:
        return "\n".join(rows)
    half = MAX_OUTPUT_LINES // 2
    return "\n".join([*rows[:half],
                      f"... ({len(rows) - 2 * half} lines not kept) ...",
                      *rows[-half:]])


def resolve_cargo(cls: Mapping[str, Any], wd: Path) -> str:
    """The cargo executable the command names, or Unhandled."""
    tool = str(cls.get("tool") or "cargo")
    if "/" in tool:
        p = Path(tool) if Path(tool).is_absolute() else wd / tool
        if not (p.is_file() and os.access(p, os.X_OK)):
            raise Unhandled(f"{tool} is not an executable")
        return str(p)
    found = shutil.which(tool)
    if not found:
        raise Unhandled(f"{tool} is not on PATH")
    return found


def _kill_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def run(cargo: str, argv: list[str], wd: Path, *,
        timeout_s: float = 120.0, env: Mapping[str, str] | None = None,
        tail_lines: int = OUTPUT_TAIL_LINES) -> dict[str, Any]:
    """Run one cargo invocation and return raw output and its status."""
    cmd = [cargo, *argv]
    run_env = dict(os.environ) if env is None else dict(env)
    run_env.setdefault("CARGO_TERM_COLOR", "never")
    run_env["PWD"] = str(wd)
    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(wd), env=run_env, stdin=subprocess.DEVNULL,
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
                    proc.kill()
                    out = b""
            _kill_group(proc.pid, signal.SIGKILL)
    except OSError as exc:
        return {"ok": False, "status": "failed", "exit_code": 127,
                "timed_out": False, "seconds": 0.0, "output": "",
                "output_tail": "", "command": cmd,
                "error": f"cannot run cargo: {exc}"}
    seconds = round(time.monotonic() - t0, 3)
    text = (out or b"").decode("utf-8", errors="replace")
    return {"ok": proc.returncode == 0 and not timed_out,
            "exit_code": proc.returncode, "timed_out": timed_out,
            "seconds": seconds, "output": text,
            "output_tail": _output_lines(text, tail_lines),
            "command": cmd, "error": ""}


def dedupe_argv(argv: Iterable[str]) -> list[str]:
    """Cargo arguments without quiet flags, which hide target headers."""
    out: list[str] = []
    tokens = list(argv)
    for index, tok in enumerate(tokens):
        if tok == "--":
            out.extend(tokens[index:])
            break
        if tok in ("-q", "--quiet"):
            continue
        if tok.startswith("-") and not tok.startswith("--") and len(tok) > 2 \
                and set(tok[1:]) <= set("qv"):
            rest = "".join(c for c in tok[1:] if c == "v")
            if rest:
                out.append("-" + rest)
            continue
        out.append(tok)
    return out


__all__ = [
    "CargoBlock",
    "dedupe_argv",
    "parse",
    "resolve_cargo",
    "run",
    "status_for",
]
