"""Deterministic failure classes (`ozagia.core.failures`): every class with
real-shaped output (the numpy check logs of the 2026-11-17 Ocelot eval,
with generic paths), the rule order, first-party detection, bounds, the
optional `who_has` detail (W20, stubbed), the captured pytest's
`[ozagia] failure class` line, and the test record's `failure_class`."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from ozagia.config import clear_config_cache
from ozagia.core import failures as F
from ozagia.core import memory as M
from ozagia.core import working_set as W
from ozagia.core.memory import DevMemory, build_index

FIX = Path(__file__).resolve().parent / "fixtures" / "failures"
NUMPY_LOGS = ("check_learner-board-pairs.log", "check_life-step-cache.log",
              "check_r2-cycle-arms.log")
ENV_MODULE = "ozagia.core.environments"


def _log(name: str) -> tuple[str, str, str]:
    """(command, stdout, stderr) of a harness check log."""
    text = (FIX / name).read_text(encoding="utf-8")
    head, _, rest = text.partition("\n")
    out, _, err = rest.partition("\n--- stderr ---\n")
    return head[2:], out, err


@pytest.fixture
def no_who_has(monkeypatch):
    """W20 absent: importing `ozagia.core.environments` fails."""
    monkeypatch.setitem(sys.modules, ENV_MODULE, None)


@pytest.fixture
def who_has_stub(monkeypatch):
    calls: list[tuple] = []
    mod = types.ModuleType(ENV_MODULE)

    def who_has(root, module):
        calls.append((root, module))
        return (["/opt/proj/.venv/bin/python", {"path": "/usr/bin/python3"}]
                if module in ("numpy", "w21_absent_mod") else [])
    mod.who_has = who_has
    monkeypatch.setitem(sys.modules, ENV_MODULE, mod)
    return calls


# --------------------------------------------------------------------------- #
# environment: the real check logs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", NUMPY_LOGS)
def test_numpy_check_logs_are_environment(name, no_who_has):
    cmd, out, err = _log(name)
    got = F.classify(2 if "no collectors" not in err else 4, False, out, err,
                     command=cmd, interpreter="/opt/tool/.venv/bin/python")
    assert got["class"] == "environment"
    assert got["module"] == "numpy"
    assert got["interpreter"] == "/opt/tool/.venv/bin/python"
    assert any("ModuleNotFoundError: No module named 'numpy'" in e
               for e in got["evidence"])
    assert "missing module numpy (third-party) under /opt/tool/.venv/bin/python" \
        in got["detail"]
    assert "available_in" not in got


def test_environment_beats_found_no_collectors():
    """The named node ids have no collectors because their file failed to
    import numpy: the cause, not the symptom, decides."""
    cmd, out, err = _log("check_life-step-cache.log")
    assert "found no collectors" in err
    assert F.classify(4, False, out, err, command=cmd)["class"] == "environment"
    # Without the import error, the same lines are a collection failure.
    got = F.classify(4, False, "", err, command=cmd)
    assert got["class"] == "collection" and "no tests collected for" in got["detail"]


def test_transitive_third_party_import_through_first_party_module():
    cmd, out, err = _log("check_r2-cycle-arms.log")
    got = F.classify(2, False, out, err, command=cmd,
                     first_party={"tests", "scripts", "substrate"})
    assert got["class"] == "environment" and got["module"] == "numpy"


def test_who_has_names_the_interpreters_that_have_the_module(who_has_stub, tmp_path):
    cmd, out, err = _log("check_learner-board-pairs.log")
    got = F.classify(2, False, out, err, command=cmd, root=tmp_path,
                     interpreter="/usr/bin/python3")
    assert who_has_stub == [(tmp_path, "numpy")]
    # The interpreter that ran the check is not listed as having it.
    assert got["available_in"] == ["/opt/proj/.venv/bin/python"]
    assert got["detail"].endswith("available in /opt/proj/.venv/bin/python")


def test_who_has_absent_or_failing_gives_no_detail(no_who_has, monkeypatch, tmp_path):
    cmd, out, err = _log("check_learner-board-pairs.log")
    got = F.classify(2, False, out, err, command=cmd, root=tmp_path)
    assert got["class"] == "environment" and "available_in" not in got
    broken = types.ModuleType(ENV_MODULE)

    def boom(root, module):
        raise RuntimeError("probe failed")
    broken.who_has = boom
    monkeypatch.setitem(sys.modules, ENV_MODULE, broken)
    got = F.classify(2, False, out, err, command=cmd, root=tmp_path)
    assert got["class"] == "environment" and "available_in" not in got


@pytest.mark.parametrize("out,err,rc,detail", [
    ("", "/usr/bin/python3: No module named pytest\n", 1, "pytest is not installed"),
    ("", "ERROR: usage: __main__.py [options] [file_or_dir] [file_or_dir] [...]\n"
         "__main__.py: error: unrecognized arguments: --cov=pkg\n"
         "  inifile: None\n  rootdir: /work/repo\n", 4,
     "unrecognized arguments --cov=pkg"),
    ("E   ImportError: cannot import name 'trapezoid' from 'numpy' "
     "(/usr/lib/python3/dist-packages/numpy/__init__.py)\n1 error in 0.2s\n", "", 2,
     "cannot import from numpy"),
    ("E   ImportError: libGL.so.1: cannot open shared object file: No such file or "
     "directory\n1 error in 0.2s\n", "", 2, "missing shared library libGL.so.1"),
    ("    def test_status():\n>       subprocess.run(['git', 'status'])\n"
     "E       FileNotFoundError: [Errno 2] No such file or directory: 'git'\n"
     "FAILED tests/test_vcs.py::test_status - FileNotFoundError: [Errno 2] No such "
     "file or directory: 'git'\n1 failed in 0.1s\n", "", 1, "missing executable git"),
])
def test_environment_rules(out, err, rc, detail, no_who_has):
    got = F.classify(rc, False, out, err, command="python -m pytest -q tests",
                     first_party={"pkg", "tests"})
    assert got["class"] == "environment", got
    assert detail in got["detail"]


# --------------------------------------------------------------------------- #
# collection, and environment versus collection for import errors
# --------------------------------------------------------------------------- #
FIRST_PARTY_IMPORT = """\
======== ERRORS ========
________ ERROR collecting tests/test_core.py ________
ImportError while importing test module '/work/repo/tests/test_core.py'.
Traceback:
tests/test_core.py:1: in <module>
    from pkg.missing import thing
E   ModuleNotFoundError: No module named 'pkg.missing'
======== short test summary info ========
ERROR tests/test_core.py
!!!!!!!! Interrupted: 1 error during collection !!!!!!!!
1 error in 0.05s
"""


def test_first_party_import_error_is_collection():
    got = F.classify(2, False, FIRST_PARTY_IMPORT, "", first_party={"pkg", "tests"})
    assert got["class"] == "collection"
    assert got["module"] == "pkg.missing"
    assert "first-party module pkg.missing" in got["detail"]


def test_first_party_from_root_and_from_output(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "tool.py").write_text("")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / "src" / "lib2").mkdir(parents=True)
    (tmp_path / "src" / "lib2" / "m.py").write_text("")
    assert F.first_party_names(tmp_path) == {"pkg", "tool", "lib2"}
    assert F.classify(2, False, FIRST_PARTY_IMPORT, "", root=tmp_path)["class"] \
        == "collection"
    # No names and no root: the paths in the output's frames (`tests/...`,
    # `pkg/...`) are first-party.
    text = FIRST_PARTY_IMPORT.replace(
        "tests/test_core.py:1: in <module>",
        "tests/test_core.py:1: in <module>\npkg/__init__.py:3: in <module>")
    assert F.classify(2, False, text, "")["class"] == "collection"
    third = FIRST_PARTY_IMPORT.replace("pkg.missing", "requests")
    assert F.classify(2, False, third, "")["class"] == "environment"


def test_environment_beats_a_first_party_import_error():
    both = FIRST_PARTY_IMPORT + "E   ModuleNotFoundError: No module named 'yaml'\n"
    got = F.classify(2, False, both, "", first_party={"pkg", "tests"})
    assert got["class"] == "environment" and got["module"] == "yaml"


@pytest.mark.parametrize("out,rc,detail", [
    ("________ ERROR collecting tests/test_x.py ________\n"
     "E     File \"/work/repo/tests/test_x.py\", line 3\n"
     "E       def f(:\nE             ^\nE   SyntaxError: invalid syntax\n"
     "!!!!!!!! Interrupted: 1 error during collection !!!!!!!!\n1 error in 0.1s\n",
     2, "syntax error during collection"),
    ("E       fixture 'db' not found\n>       available fixtures: cache, capfd\n"
     "ERROR tests/test_x.py::test_a\n1 error in 0.1s\n", 1, "fixture db not found"),
    ("\nno tests ran in 0.01s\n", 5, "no tests collected"),
    ("[ozagia] pytest failed: exit 2, 0.1 s, 0 passed, 0 failed, 0 error, 0 skipped, "
     "1 collection errors\n[ozagia] collection error file:tests/test_core.py: "
     "ImportError: x\n", 2, "collection error"),
])
def test_collection_rules(out, rc, detail):
    got = F.classify(rc, False, out, "", first_party={"pkg", "tests"})
    assert got["class"] == "collection", got
    assert detail in got["detail"]


# --------------------------------------------------------------------------- #
# harness, timeout, assertion, error, passed, unknown
# --------------------------------------------------------------------------- #
def test_harness_rules(tmp_path):
    missing = "/opt/none/.venv/bin/python"
    got = F.classify(None, False, "", f"FileNotFoundError: [Errno 2] No such file or "
                     f"directory: '{missing}'", command=[missing, "-m", "pytest"],
                     interpreter=missing)
    assert got["class"] == "harness" and missing in got["detail"]
    got = F.classify(127, False, "", "bash: pytest: command not found\n",
                     command="pytest -q tests")
    assert got["class"] == "harness" and "runner pytest" in got["detail"]
    gone = tmp_path / "gone"
    got = F.classify(None, False, "", "", command=["python3", "-m", "pytest"],
                     root=gone)
    assert got["class"] == "harness" and "working directory" in got["detail"]
    # A missing tool other than the runner is the environment's.
    got = F.classify(127, False, "", "sh: 1: jq: not found\n", command="pytest -q")
    assert got["class"] == "environment"


def test_timeout_beats_output():
    cmd, out, err = _log("check_learner-board-pairs.log")
    got = F.classify(None, True, out, err, command=cmd)
    assert got["class"] == "timeout" and got["evidence"][0] == "timed out"
    assert F.classify(-9, True, "F" * 10 + "\n1 failed in 3s\n", "")["class"] == "timeout"


AGENT_ASSERT = """\
>               assert learner.predict(src_type, n) == expected, \\
E               AssertionError: (AliveCell, n=2): predicted DeadCell, expected AliveCell
E               assert 'DeadCell' == 'AliveCell'

tests/test_learner.py:92: AssertionError
=========================== short test summary info ============================
FAILED tests/test_learner.py::test_learned_grammar_matches_conway_table - Ass...
!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!
1 failed, 1 passed in 0.42s
[ozagia] pytest failed: exit 1, 0.665 s, 1 passed, 1 failed, 0 error, 0 skipped
"""


def test_assertion_error_and_passed():
    got = F.classify(1, False, AGENT_ASSERT, "")
    assert got["class"] == "assertion" and "1 failed" in got["detail"]
    # Exit code unknown (a piped command): the summary decides.
    assert F.classify(None, False, AGENT_ASSERT, "")["class"] == "assertion"
    body = ("    def test_lookup():\n>       return table['x']\nE       KeyError: 'x'\n"
            "FAILED tests/test_t.py::test_lookup - KeyError: 'x'\n1 failed in 0.1s\n")
    assert F.classify(1, False, body, "")["class"] == "error"
    setup = "ERROR tests/test_t.py::test_a - RuntimeError: db down\n1 error in 0.1s\n"
    assert F.classify(1, False, setup, "")["class"] == "error"
    internal = "INTERNALERROR> Traceback (most recent call last):\nINTERNALERROR> boom\n"
    assert F.classify(3, False, internal, "")["class"] == "error"
    assert F.classify(0, False, "18 passed in 0.38s\n", "")["class"] == "passed"
    assert F.classify(None, False, "....\n4 passed in 0.72s\n", "")["class"] == "passed"
    assert F.classify(None, False, "", "")["class"] == "unknown"


def test_passed_exit_zero_ignores_import_lines_in_logs():
    out = "E   ModuleNotFoundError: No module named 'numpy'\n3 passed in 0.1s\n"
    assert F.classify(0, False, out, "")["class"] == "passed"


def test_input_and_evidence_are_bounded():
    filler = "x" * 100 + "\n"
    middle = "E   ModuleNotFoundError: No module named 'numpy'\n"
    n = 3 * (F.HEAD_CHARS + F.TAIL_CHARS) // len(filler)
    text = filler * (n // 2) + middle + filler * (n // 2) + "1 error in 0.1s\n"
    assert len(F.bounded(text)) < F.HEAD_CHARS + F.TAIL_CHARS + 100
    # The line in the middle is not read; the tail is.
    assert F.classify(2, False, text, "")["class"] != "environment"
    assert F.classify(2, False, filler * n + middle, "")["class"] == "environment"
    long = "E   AssertionError: " + "y" * 5000 + "\n1 failed in 0.1s\n"
    got = F.classify(1, False, long, "")
    assert got["class"] == "assertion"
    assert all(len(e) <= F.EVIDENCE_CHARS for e in got["evidence"])
    assert len(got["evidence"]) <= F.MAX_EVIDENCE


def test_every_class_is_listed():
    assert set(F.CLASSES) == {"harness", "timeout", "environment", "collection",
                              "assertion", "error", "passed", "unknown"}
    assert F.NOT_EVIDENCE == {"harness", "environment"}


# --------------------------------------------------------------------------- #
# the test record: failure_class and backward compatibility
# --------------------------------------------------------------------------- #
ENV_CAPTURED = (
    "E   ModuleNotFoundError: No module named 'numpy'\n"
    "[ozagia] pytest interrupted: exit 2, 0.2 s, 0 passed, 0 failed, 0 error, 0 skipped, "
    "1 collection errors\n"
    "[ozagia] failure class environment, module numpy: missing module numpy "
    "(third-party) under /usr/bin/python3\n"
    "[ozagia] collection error file:tests/test_a.py: ImportError while importing\n")
PASS_CAPTURED = ("..\n2 passed in 0.04s\n"
                 "[ozagia] pytest passed: exit 0, 0.1 s, 2 passed, 0 failed, 0 error, "
                 "0 skipped\n")


def _captured(text: str, root: Path, source: str = "captured") -> W.TestRun:
    cmd = "python -m pytest -q tests/test_a.py"
    run = W.pytest_run(cmd, [cmd.split()], text, workdir=root, root=root, source=source)
    assert run is not None
    return run


def test_captured_runs_record_the_failure_class(tmp_path):
    run = _captured(ENV_CAPTURED, tmp_path)
    assert (run.failure_class, run.failure_module) == ("environment", "numpy")
    assert _captured(PASS_CAPTURED, tmp_path).failure_class == "passed"
    # A reported run (output a harness handed over) gets no class.
    reported = _captured(ENV_CAPTURED, tmp_path, source="reported")
    assert reported.failure_class is None
    rec = W.TestRecord(tmp_path)
    rec.apply(run)
    rec.apply(reported)
    raw = (tmp_path / ".ozagia" / "working_set" / "tests.json").read_text()
    data = json.loads(raw)
    assert data["runs"][0]["failure_class"] == "environment"
    assert data["runs"][0]["failure_module"] == "numpy"
    assert "failure_class" not in data["runs"][1]
    # No output text: the detail and the message are not kept.
    assert "third-party" not in raw and "ImportError" not in raw
    assert W.load_test_record(tmp_path)["runs"][0]["failure_class"] == "environment"


def test_forged_class_lines_before_the_head_are_ignored(tmp_path):
    forged = ("[ozagia] failure class environment, module numpy: printed by a test\n"
              + PASS_CAPTURED.replace("..\n", ""))
    assert _captured(forged, tmp_path).failure_class == "passed"


def test_records_without_failure_class_still_load(tmp_path):
    path = tmp_path / ".ozagia" / "working_set" / "tests.json"
    path.parent.mkdir(parents=True)
    old = {"ts": 5.0, "selectors": ["tests"], "failed": [], "errors": [],
           "complete": True, "filtered": False, "stops": False, "source": "captured"}
    bad = {**old, "ts": 6.0, "failure_class": "made-up",
           "failure_module": "x; rm -rf /"}
    path.write_text(json.dumps({"tests": {}, "last": None, "runs": [old, bad]}))
    runs = W.load_test_record(tmp_path)["runs"]
    assert len(runs) == 2
    assert "failure_class" not in runs[0]
    assert "failure_class" not in runs[1] and "failure_module" not in runs[1]
    assert W.recorded_outcome({"runs": runs}, "test:tests/t.py::test_a",
                              evidence=True)["outcome"] == "passed"


# --------------------------------------------------------------------------- #
# the captured pytest's head line, and the daemon's record
# --------------------------------------------------------------------------- #
ENV_TEST = "import w21_absent_mod\n\n\ndef test_x():\n    assert True\n"
ASSERT_TEST = "def test_y():\n    assert 1 == 2\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "IMPORT_ROOTS", ())
    for var in ("OZAGIA_CONFIG", "OZAGIA_ADAPTERS", "OZAGIA_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    clear_config_cache()
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "tests").mkdir()
    (root / "tests" / "test_env.py").write_text(ENV_TEST)
    (root / "tests" / "test_assert.py").write_text(ASSERT_TEST)
    return root


@pytest.fixture
def mem(repo, tmp_path):
    dump = tmp_path / "state" / "graph.json"
    build_index(repo, dump, include_docs="features", include_git=False)
    return DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=repo)


def _shell(mem, command):
    from ozagia import intent as I
    return I.handle(mem, "shell", {"command": command})


def _class_line(output: str) -> str:
    lines = output.splitlines()
    at = next(i for i, x in enumerate(lines) if x.startswith("[ozagia] pytest "))
    assert lines[at + 1].startswith("[ozagia] failure class "), lines[at:at + 3]
    return lines[at + 1]


def test_head_line_names_environment_and_module(mem, no_who_has):
    res = _shell(mem, f"{sys.executable} -m pytest -q tests/test_env.py")
    assert res["handled"] and res["meta"]["intent"] == "shell_pytest", res
    line = _class_line(res["output"])
    assert line.startswith("[ozagia] failure class environment, module w21_absent_mod: "
                           "missing module w21_absent_mod (third-party) under ")
    assert sys.executable in line and "available in" not in line


def test_head_line_names_interpreters_that_have_it(mem, who_has_stub):
    res = _shell(mem, f"{sys.executable} -m pytest -q tests/test_env.py")
    line = _class_line(res["output"])
    assert line.endswith("available in /opt/proj/.venv/bin/python, /usr/bin/python3")
    assert who_has_stub and who_has_stub[0][1] == "w21_absent_mod"


def test_head_line_for_assertion_and_none_for_pass(mem, repo, no_who_has):
    res = _shell(mem, f"{sys.executable} -m pytest -q tests/test_assert.py")
    assert _class_line(res["output"]).startswith("[ozagia] failure class assertion: ")
    (repo / "tests" / "test_assert.py").write_text("def test_y():\n    assert True\n")
    res = _shell(mem, f"{sys.executable} -m pytest -q tests/test_assert.py")
    assert "pytest passed: exit 0" in res["output"]
    assert "failure class" not in res["output"]


def test_daemon_records_the_class_of_a_captured_run(mem, repo, tmp_path, no_who_has):
    from ozagia.daemon.server import DaemonState
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    msg = {"op": "tool", "tool": "shell", "harness": "opencode", "session": "s1",
           "input": {"command": f"{sys.executable} -m pytest -q tests/test_env.py"}}
    resp = st.dispatch(msg)
    st.record(msg, resp, 1.0)
    assert "[ozagia] failure class environment, module w21_absent_mod" in resp["output"]
    runs = W.load_test_record(repo)["runs"]
    assert runs[-1]["source"] == "captured"
    assert (runs[-1]["failure_class"], runs[-1]["failure_module"]) == (
        "environment", "w21_absent_mod")
    raw = (repo / ".ozagia" / "working_set" / "tests.json").read_text()
    assert "third-party" not in raw and "No module named" not in raw
