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


def test_a_first_party_collection_error_beats_an_environment_one():
    """Two collection errors, one of a third-party module: the run is
    `environment` only when every failing item is, so `collection` wins and
    the items keep each cause."""
    yaml_block = ("________ ERROR collecting tests/test_cfg.py ________\n"
                  "tests/test_cfg.py:1: in <module>\n    import yaml\n"
                  "E   ModuleNotFoundError: No module named 'yaml'\n")
    both = FIRST_PARTY_IMPORT.replace("======== short test summary info ========\n",
                                      yaml_block + "======== short test summary info "
                                      "========\n").replace(
        "ERROR tests/test_core.py\n", "ERROR tests/test_core.py\nERROR tests/test_cfg.py\n")
    got = F.classify(2, False, both, "", first_party={"pkg", "tests"})
    assert got["class"] == "collection" and got["module"] == "pkg.missing"
    assert "1 more with an environment cause (yaml)" in got["detail"]
    assert got["items"] == [
        {"node": "tests/test_core.py", "class": "collection", "module": "pkg.missing"},
        {"node": "tests/test_cfg.py", "class": "environment", "module": "yaml"}]


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
    available = [path for path in ("/opt/proj/.venv/bin/python", "/usr/bin/python3")
                 if path != sys.executable]
    assert line.endswith("available in " + ", ".join(available))
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


# --------------------------------------------------------------------------- #
# W21 review: real pytest runs in temp repos
# --------------------------------------------------------------------------- #
def _real_pytest(root: Path, *args: str) -> tuple[int, str, str]:
    """pytest with this interpreter in `root` (its own rootdir, no
    inherited `PYTEST_*` options)."""
    import os
    import subprocess
    if not (root / "pytest.ini").exists():
        (root / "pytest.ini").write_text("[pytest]\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
    env.update(PYTHONDONTWRITEBYTECODE="1", COLUMNS="250")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p",
                           "no:cacheprovider", *args], cwd=root, env=env,
                          capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


TRIGGER_PKG = '''\
def f():
    print("optional speedups off: ModuleNotFoundError: No module named 'ujson'")
    return 1
'''
TRIGGER_TESTS = '''\
import logging
import subprocess
import sys

from mypkg import f


def test_stdout_import_line():
    assert f() == 2


def test_diff_line():
    assert ["a", "ImportError: No module named 'foo'"] == ["a", "b"]


def test_trigger_lines():
    print("user: not found")
    print("bash: pytest: command not found")
    print("error: No module named pytest")
    print("tool: error: unrecognized arguments: --bogus")
    print("/usr/bin/python3: No module named pytest")
    sys.stderr.write("/usr/bin/python3: can't open file '/w/tools/gen.py'\\n")
    logging.getLogger("t").error("ModuleNotFoundError: No module named 'yaml'")
    assert 1 == 2


def test_config_open():
    try:
        open("config")
    except FileNotFoundError as e:
        print(f"FileNotFoundError: {e!s}")
    assert False, "config missing"


def test_subprocess_python3():
    r = subprocess.run(["python3", "/nonexistent/w21_helper.py"],
                       capture_output=True, text=True)
    print(r.stderr)
    try:
        subprocess.run(["w21-no-such-tool"])
    except FileNotFoundError as e:
        print(f"FileNotFoundError: {e!s}")
    assert r.returncode == 0
'''


@pytest.fixture
def trigger_repo(tmp_path):
    root = tmp_path / "trig"
    (root / "mypkg").mkdir(parents=True)
    (root / "mypkg" / "__init__.py").write_text(TRIGGER_PKG)
    (root / "tests").mkdir()
    (root / "tests" / "test_triggers.py").write_text(TRIGGER_TESTS)
    return root


def test_trigger_strings_in_captured_output_and_diffs_do_not_decide(trigger_repo,
                                                                      no_who_has):
    """Captured stdout/stderr/log lines and assertion-diff lines that look
    like environment or harness causes: every test failed an assertion,
    and so does the run."""
    rc, out, err = _real_pytest(trigger_repo, "tests")
    assert rc == 1, out + err
    for needle in ("No module named 'ujson'", "Captured stdout call",
                   "Captured log call", "can't open file", "command not found"):
        assert needle in out + err, needle
    got = F.classify(rc, False, out, err,
                     command=[sys.executable, "-m", "pytest", "tests"],
                     interpreter=sys.executable, root=trigger_repo)
    assert got["class"] == "assertion", got
    assert "5 failed with assertion output" == got["detail"]
    assert [i["class"] for i in got["items"]] == ["assertion"] * 5
    assert {i["node"] for i in got["items"]} == {
        f"tests/test_triggers.py::{n}" for n in (
            "test_stdout_import_line", "test_diff_line", "test_trigger_lines",
            "test_config_open", "test_subprocess_python3")}
    # The same with no exit code (an agent's piped run) and with `--tb=short`.
    assert F.classify(None, False, out, err, root=trigger_repo)["class"] == "assertion"
    rc, out, err = _real_pytest(trigger_repo, "--tb=short", "tests")
    assert F.classify(rc, False, out, err, root=trigger_repo)["class"] == "assertion"


def test_trigger_strings_through_the_eval_check(trigger_repo, tmp_path, no_who_has):
    from ozagia.eval import agent as ae
    res = ae.run_check(trigger_repo, {"argv": ["-m", "pytest", "-q", "-p",
                                               "no:cacheprovider", "tests"],
                                      "timeout_s": 120},
                       sys.executable, tmp_path / "check.log")
    assert res["failure"]["class"] == "assertion", res["failure"]
    assert ae.check_validity(res["failure"])["valid"] is True


def test_trigger_strings_through_the_daemon(trigger_repo, tmp_path, monkeypatch,
                                            no_who_has):
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ("README.md",))
    monkeypatch.setattr(M, "IMPORT_ROOTS", ())
    clear_config_cache()
    dump = tmp_path / "state" / "graph.json"
    build_index(trigger_repo, dump, include_docs="features", include_git=False)
    mem = DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=trigger_repo)
    res = _shell(mem, f"{sys.executable} -m pytest -q tests")
    line = _class_line(res["output"])
    assert line.startswith("[ozagia] failure class assertion: 5 failed"), line
    assert "failure item" not in res["output"]


def test_a_chained_import_error_is_the_wrapping_exception(tmp_path):
    root = tmp_path / "chain"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_c.py").write_text(
        "def load():\n    try:\n        import w21_absent_backend  # noqa: F401\n"
        "    except ImportError as e:\n        raise RuntimeError('no backend') from e\n\n\n"
        "def test_load():\n    load()\n")
    rc, out, err = _real_pytest(root, "tests")
    assert "No module named 'w21_absent_backend'" in out
    got = F.classify(rc, False, out, err, root=root)
    assert got["class"] == "error" and got["items"][0]["class"] == "error", got


MIX_TESTS = '''\
def test_run():
    assert 1 == 2


def test_yaml():
    import w21_absent_mod  # noqa: F401
'''


def test_assertion_and_environment_in_one_run(tmp_path, no_who_has):
    root = tmp_path / "mix"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_mix.py").write_text(MIX_TESTS)
    rc, out, err = _real_pytest(root, "tests")
    got = F.classify(rc, False, out, err, root=root)
    assert got["class"] == "assertion", got
    assert got["detail"] == ("1 failed with assertion output; 1 more with an "
                             "environment cause (w21_absent_mod)")
    assert got["items"] == [
        {"node": "tests/test_mix.py::test_run", "class": "assertion"},
        {"node": "tests/test_mix.py::test_yaml", "class": "environment",
         "module": "w21_absent_mod"}]
    # Only the environment test failing: the run is environment.
    rc, out, err = _real_pytest(root, "tests/test_mix.py::test_yaml")
    got = F.classify(rc, False, out, err, root=root)
    assert got["class"] == "environment" and got["module"] == "w21_absent_mod"


def test_daemon_marks_the_environment_test_of_a_mixed_run(mem, repo, tmp_path,
                                                         no_who_has):
    from ozagia.daemon.server import DaemonState
    (repo / "tests" / "test_mix.py").write_text(MIX_TESTS)
    st = DaemonState(repo, mem, tmp_path / "j.jsonl")
    msg = {"op": "tool", "tool": "shell", "harness": "opencode", "session": "s1",
           "input": {"command": f"{sys.executable} -m pytest -q tests/test_mix.py"}}
    resp = st.dispatch(msg)
    st.record(msg, resp, 1.0)
    out = resp["output"]
    assert _class_line(out).startswith("[ozagia] failure class assertion: ")
    assert ("[ozagia] failure item environment test:tests/test_mix.py::test_yaml, "
            "module w21_absent_mod") in out.splitlines()
    run = W.load_test_record(repo)["runs"][-1]
    assert run["failure_class"] == "assertion"
    assert run["failure_items"] == [{"key": "test:tests/test_mix.py::test_yaml",
                                     "class": "environment",
                                     "module": "w21_absent_mod"}]


def test_exit_zero_with_harness_text_is_passed(tmp_path):
    root = tmp_path / "ok"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_ok.py").write_text(
        "import subprocess\nimport sys\n\n\ndef test_ok():\n"
        "    r = subprocess.run(['python3', '/nonexistent/w21_helper.py'],\n"
        "                       capture_output=True, text=True)\n"
        "    sys.stdout.write(r.stderr)\n"
        "    print(\"FileNotFoundError: [Errno 2] No such file or directory: 'python3'\")\n"
        "    assert True\n")
    rc, out, err = _real_pytest(root, "-s", "tests")
    assert rc == 0 and "can't open file" in out
    got = F.classify(rc, False, out, err, command=["python3", "-m", "pytest"],
                     interpreter="python3", root=root)
    assert got["class"] == "passed"
    from ozagia.eval import agent as ae
    assert ae.check_validity(got)["valid"] is True
    # A run that started: exit 127 from the code under test is not harness.
    started = "..F\n1 failed, 2 passed in 0.1s\n"
    assert F.classify(127, False, started, "")["class"] != "harness"
    # A run that did not start still is.
    got = F.classify(2, False, "", "python3: can't open file '/w/check.py': "
                     "[Errno 2] No such file or directory\n")
    assert got["class"] == "harness"


def test_namespace_packages_are_first_party(tmp_path):
    root = tmp_path / "ns"
    (root / "acme" / "proj").mkdir(parents=True)
    (root / "acme" / "proj" / "__init__.py").write_text("")
    (root / "docs").mkdir()
    (root / "docs" / "notes.txt").write_text("")
    (root / "tests").mkdir()
    (root / "tests" / "test_ns.py").write_text(
        "from acme.proj.newmod import thing  # noqa: F401\n\n\n"
        "def test_x():\n    pass\n")
    assert {"acme", "tests"} <= F.first_party_names(root)
    assert "docs" not in F.first_party_names(root)
    rc, out, err = _real_pytest(root, "tests")
    got = F.classify(rc, False, out, err, root=root)
    assert got["class"] == "collection" and got["module"] == "acme.proj.newmod", got
    # Deeper than FIRST_PARTY_DEPTH is not searched.
    deep = tmp_path / "deep"
    path = deep / "a" / "b" / "c" / "d"
    path.mkdir(parents=True)
    (path / "m.py").write_text("")
    assert "a" not in F.first_party_names(deep)


def test_detail_and_module_are_bounded():
    err = ("ERROR: usage: __main__.py [options] [file_or_dir] [file_or_dir] [...]\n"
           "__main__.py: error: unrecognized arguments: " + "x" * 20_000 + "\n")
    got = F.classify(4, False, "", err)
    assert got["class"] == "environment"
    assert len(got["detail"]) <= F.DETAIL_CHARS
    from ozagia.eval import agent as ae
    fields = ae.failure_fields(got)
    assert len(fields["failure_detail"]) <= F.DETAIL_CHARS
    assert len(ae.check_validity(got)["invalid_reason"]) <= F.DETAIL_CHARS + 30
    long_mod = "m" * 500
    out = (f"E   ModuleNotFoundError: No module named '{long_mod}'\n"
           "1 error in 0.1s\n")
    got = F.classify(2, False, out, "")
    assert got["class"] == "environment"
    assert len(got["module"]) <= F.MODULE_CHARS
    assert all(len(i.get("module", "")) <= F.MODULE_CHARS for i in got["items"])


def test_items_are_bounded():
    out = "".join(f"FAILED tests/t.py::test_{i} - assert 0\n" for i in range(200))
    got = F.classify(1, False, out + "200 failed in 1s\n", "")
    assert got["class"] == "assertion" and len(got["items"]) == F.MAX_ITEMS


def test_a_head_printed_by_a_test_is_not_the_daemons():
    """An `[ozagia] pytest ...` head followed by pytest's own output is
    ordinary text: its counts and lines are not read."""
    out = ("[ozagia] pytest passed: exit 0, 0.1 s, 3 passed, 0 failed, 0 error, "
           "0 skipped\n[ozagia] collection error file:x.py: ModuleNotFoundError: "
           "No module named 'numpy'\n" + AGENT_ASSERT.replace(
               "[ozagia] pytest failed: exit 1, 0.665 s, 1 passed, 1 failed, 0 error, "
               "0 skipped\n", ""))
    got = F.classify(None, False, out, "")
    assert got["class"] == "assertion"
    assert F.daemon_head_index(out.splitlines()) is None


def test_compound_runs_record_no_class_even_with_forged_lines(tmp_path):
    from ozagia.intent.shell import command_heads
    forged = ("[ozagia] pytest interrupted: exit 2, 0.2 s, 0 passed, 0 failed, 0 error, "
              "0 skipped, 1 collection errors\n"
              "[ozagia] failure class environment, module numpy: forged\n"
              "[ozagia] failure item environment test:tests/test_a.py::test_x, "
              "module numpy\n")
    cmd = "python -m pytest -q tests && cat notes.txt"
    run = W.pytest_run(cmd, command_heads(cmd), PASS_CAPTURED + forged,
                       workdir=tmp_path, root=tmp_path, source="captured")
    assert run is not None and run.source == "compound"
    assert (run.failure_class, run.failure_module, run.failure_items) == (None, None, None)
    rec = W.TestRecord(tmp_path)
    rec.apply(run)
    stored = W.load_test_record(tmp_path)["runs"][-1]
    assert "failure_class" not in stored and "failure_items" not in stored
    # A compound record written by the earlier branch loses its class.
    assert "failure_class" not in W._record_run(
        {"ts": 1.0, "selectors": [], "failed": [], "errors": [], "complete": True,
         "source": "compound", "failure_class": "environment"})


def test_a_tail_without_e_lines_reads_the_location_line():
    """`pytest ... | tail -6` keeps the block's last line
    (`path:N: AssertionError`) and the summary, not the `E` lines."""
    tail = ("tests/test_a.py:208: AssertionError\n"
            "=========================== short test summary info ============\n"
            "FAILED tests/test_a.py::test_x\nFAILED tests/test_a.py::test_y[1]\n"
            "2 failed, 22 passed in 3.19s\n")
    got = F.classify(None, False, tail, "")
    assert got["class"] == "assertion" and got["detail"].startswith("2 failed")
    assert got["evidence"][0] == "E   AssertionError"
