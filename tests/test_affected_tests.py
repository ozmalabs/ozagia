"""Affected-test selection and the test-result cache (`ozagia.intent.affected`),
with real pytest runs in temporary git repositories."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia import intent as I
from ozagia.config import ConfigError, clear_config_cache, load_project_config
from ozagia.core import memory as M
from ozagia.core.memory import DevMemory, build_index
from ozagia.intent import affected as A

PY = sys.executable
CMD = f"{PY} -m pytest -q"
RUN_ALL = f"run all: OZAGIA_TESTS_ALL=1 {CMD}"

FILES = {
    "pkg/__init__.py": "",
    "pkg/base.py": "def one():\n    return 1\n",
    "pkg/mid.py": "from pkg.base import one\n\n\ndef two():\n    return one() + 1\n",
    "pkg/other.py": "def three():\n    return 3\n",
    "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef k():\n    return 1\n",
    # transitive: test -> pkg.mid -> pkg.base
    "tests/test_mid.py": "from pkg.mid import two\n\n\ndef test_two(k):\n"
                         "    assert two() == 2\n",
    # `import a.b` (the graph's import edges do not resolve this form)
    "tests/test_base_direct.py": "import pkg.base\n\n\ndef test_one():\n"
                                 "    assert pkg.base.one() == 1\n",
    # `from pkg import submodule`
    "tests/test_other.py": "from pkg import other\n\n\ndef test_three():\n"
                           "    assert other.three() == 3\n",
}


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "HOME": str(root), "PATH": "/usr/bin:/bin"}).stdout


@pytest.fixture
def layout(monkeypatch):
    monkeypatch.setattr(M, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(M, "TEST_DIRS", ("tests",))
    monkeypatch.setattr(M, "DOC_GLOBS", ())
    monkeypatch.setattr(M, "ALL_DOC_GLOBS", ())
    monkeypatch.setattr(M, "IMPORT_ROOTS", ())
    monkeypatch.setattr(M, "_PATH_RE", M.path_mention_re(["pkg", "tests"]))
    for var in ("OZAGIA_CONFIG", "OZAGIA_ADAPTERS", "OZAGIA_STATE_DIR",
                A.ENV_ALL, "PYTEST_ADDOPTS"):
        monkeypatch.delenv(var, raising=False)
    clear_config_cache()
    yield
    clear_config_cache()


def _make(tmp_path: Path, config: str | None, extra: dict[str, str] | None = None,
          *, below_top: bool = False) -> tuple[Path, DevMemory]:
    root = tmp_path / "outer" / "repo" if below_top else tmp_path / "repo"
    top = root.parent if below_top else root
    for rel, text in {**FILES, **(extra or {})}.items():
        _write(root / rel, text)
    if config is not None:
        _write(root / ".ozagia.toml", config)
    _git(top, "init", "-q")
    _git(top, "add", "-A")
    _git(top, "commit", "-q", "-m", "initial")
    dump = tmp_path / "state" / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    return root, DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=root)


AFFECTED = '[tests]\nselect = "affected"\n'
CACHED = "[tests]\ncache = true\n"


@pytest.fixture
def affected(tmp_path, layout):
    return _make(tmp_path, AFFECTED)


@pytest.fixture
def cached(tmp_path, layout, monkeypatch):
    monkeypatch.setenv("FLIP_FILE", str(tmp_path / "flip"))
    return _make(tmp_path, CACHED)


def _run(mem: DevMemory, command: str = CMD, **inp) -> dict:
    res = I.handle(mem, "shell", {"command": command, **inp})
    assert res["handled"] and not res["is_error"], res
    assert res["meta"]["intent"] in ("shell_pytest", "shell_compound")
    return res


def _ozagia(res: dict) -> list[str]:
    return [ln for ln in res["output"].splitlines() if ln.startswith("[ozagia] ")]


def _counts(res: dict) -> dict:
    return res["meta"].get("tests") or {}


# --------------------------------------------------------------------------- #
# defaults and configuration
# --------------------------------------------------------------------------- #

def test_config_defaults_and_validation(tmp_path, layout):
    cfg = load_project_config(tmp_path)
    assert (cfg.tests_select, cfg.tests_cache) == ("all", False)
    _write(tmp_path / ".ozagia.toml", '[tests]\nselect = "affected"\ncache = true\n')
    clear_config_cache()
    cfg = load_project_config(tmp_path)
    assert (cfg.tests_select, cfg.tests_cache) == ("affected", True)
    for bad in ('[tests]\nselect = "some"\n', '[tests]\ncache = "yes"\n',
                '[tests]\nother = 1\n'):
        _write(tmp_path / ".ozagia.toml", bad)
        clear_config_cache()
        with pytest.raises(ConfigError):
            load_project_config(tmp_path)


def test_default_config_runs_everything_and_writes_nothing(tmp_path, layout):
    root, mem = _make(tmp_path, None)
    for _ in range(2):
        res = _run(mem)
        assert "pytest passed: exit 0" in res["output"]
        assert "3 passed" in res["output"]
        assert not [ln for ln in _ozagia(res) if "ran " in ln]
        assert "tests" not in res["meta"]
    _write(root / "pkg/other.py", "def three():\n    return 3  # edited\n")
    assert "3 passed" in _run(mem)["output"]
    assert not (root / ".ozagia" / "tests").exists()


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #

def test_first_run_is_full_and_records_the_green_run(affected):
    root, mem = affected
    res = _run(mem)
    assert "3 passed" in res["output"]
    assert ("[ozagia] ran all tests: no fully green run of this suite "
            "recorded yet") in _ozagia(res)
    assert _counts(res)["full_runs_forced"] == 1
    state = A.load_state(root)
    (suite,) = state["suites"].values()
    assert suite["files"] == {"tests/test_mid.py": 1,
                              "tests/test_base_direct.py": 1,
                              "tests/test_other.py": 1}


def test_import_chain_selects_transitive_importers_only(affected):
    root, mem = affected
    _run(mem)
    _write(root / "pkg/base.py", "def one():\n    return 1  # edited\n")
    res = _run(mem)
    out = res["output"]
    assert "2 passed" in out
    assert ("[ozagia] pytest passed (affected subset: 2 of 3 tests run, "
            "1 not run): exit 0") in out
    assert "pytest passed: exit 0" not in out
    line = ("[ozagia] ran 2 affected tests of 3; not run: 1 test in 1 file "
            f"(tests/test_other.py); {RUN_ALL}")
    assert line in _ozagia(res)
    assert _counts(res) == {"affected_runs": 1, "tests_skipped_unaffected": 1,
                            "cached_passes": 0, "full_runs_forced": 0}
    # `from pkg import other` reaches pkg/other.py.
    _git(root, "checkout", "-q", "pkg/base.py")
    _write(root / "pkg/other.py", "def three():\n    return 3  # edited\n")
    res = _run(mem)
    assert ("[ozagia] ran 1 affected tests of 3; not run: 2 tests in 2 files "
            f"(tests/test_base_direct.py, tests/test_mid.py); {RUN_ALL}"
            ) in _ozagia(res)


def test_affected_failure_is_reported_and_green_does_not_move(affected):
    root, mem = affected
    _run(mem)
    _write(root / "pkg/base.py", "def one():\n    return 5\n")
    res = _run(mem)
    assert res["output"].count("[ozagia] failed test:") == 2
    assert ("pytest failed (affected subset: 2 of 3 tests run, 1 not run): "
            "exit 1") in res["output"]
    assert any(ln.startswith("[ozagia] ran 2 affected tests of 3;")
               for ln in _ozagia(res))
    # Fixed, then another change: the base change still counts, because
    # only a full green run moves the baseline. Every test file is then
    # affected, so the command runs as typed and the baseline moves.
    _write(root / "pkg/base.py", "def one():\n    return 1  # fixed\n")
    _write(root / "pkg/other.py", "def three():\n    return 3  # edited\n")
    res = _run(mem)
    assert ("[ozagia] ran all tests: every test file is affected by the "
            "changes") in _ozagia(res)
    assert "pytest passed: exit 0" in res["output"]
    res = _run(mem)
    assert ("[ozagia] ran all tests: no files changed since the last green "
            "run") in _ozagia(res)


def test_changed_test_file_and_new_test_file_are_selected(affected):
    root, mem = affected
    _run(mem)
    _write(root / "tests/test_other.py",
           FILES["tests/test_other.py"] + "\n\ndef test_more():\n    pass\n")
    _write(root / "tests/test_new.py", "def test_new():\n    pass\n")
    res = _run(mem)
    assert "3 passed" in res["output"]
    assert ("[ozagia] ran 3 affected tests of 5; not run: 2 tests in 2 files "
            f"(tests/test_base_direct.py, tests/test_mid.py); {RUN_ALL}"
            ) in _ozagia(res)


@pytest.mark.parametrize("change, reason", [
    (("tests/conftest.py", FILES["tests/conftest.py"] + "# edited\n"),
     "conftest.py changed: tests/conftest.py"),
    (("data.json", "{}\n"), "non-Python file changed: data.json"),
    (("pyproject.toml", "[tool.pytest.ini_options]\n"),
     "non-Python file changed: pyproject.toml"),
    (("scripts/tool.py", "print(1)\n"), "no test is linked to scripts/tool.py"),
    (("pkg/dyn.py", "import importlib\nM = importlib.import_module(NAME)\n"),
     "no test is linked to pkg/dyn.py"),
    (("other/test_far.py", "def test_far():\n    pass\n"),
     "new test file outside the known test directories: other/test_far.py"),
])
def test_doubt_runs_everything(affected, change, reason):
    root, mem = affected
    _run(mem)
    _write(root / change[0], change[1])
    res = _run(mem)
    assert f"[ozagia] ran all tests: {reason}" in _ozagia(res)
    assert _counts(res)["full_runs_forced"] == 1
    assert not any("affected tests of" in ln for ln in _ozagia(res))


def test_no_change_and_opt_out_run_everything(affected):
    root, mem = affected
    _run(mem)
    res = _run(mem)
    assert ("[ozagia] ran all tests: no files changed since the last green run"
            in _ozagia(res))
    assert "3 passed" in res["output"]
    _write(root / "pkg/base.py", "def one():\n    return 1  # edited\n")
    res = _run(mem, f"OZAGIA_TESTS_ALL=1 {CMD}")
    assert "[ozagia] ran all tests: OZAGIA_TESTS_ALL=1" in _ozagia(res)
    assert "3 passed" in res["output"]


def test_directory_selector_and_unrelated_options(affected):
    root, mem = affected
    _run(mem, f"{PY} -m pytest -x tests/")
    _write(root / "pkg/other.py", "def three():\n    return 3  # edited\n")
    res = _run(mem, f"{PY} -m pytest -x tests/")
    assert any(ln.startswith("[ozagia] ran 1 affected tests of 3;")
               and ln.endswith(f"run all: OZAGIA_TESTS_ALL=1 {PY} -m pytest -x tests/")
               for ln in _ozagia(res))
    # -k changes which tests run: the command runs as typed.
    res = _run(mem, f"{PY} -m pytest -q -k three")
    assert not any("ran " in ln for ln in _ozagia(res))
    assert "tests" not in res["meta"]


def test_compound_pytest_step_is_selected(affected):
    root, mem = affected
    _run(mem)
    _write(root / "pkg/other.py", "def three():\n    return 3  # edited\n")
    res = _run(mem, f"echo hi && {CMD}")
    assert res["meta"]["intent"] == "shell_compound"
    assert any(ln.startswith("[ozagia] ran 1 affected tests of 3;")
               and ln.endswith(f"run all: echo hi && OZAGIA_TESTS_ALL=1 {CMD}")
               for ln in _ozagia(res))
    assert res["meta"]["tests"]["affected_runs"] == 1


def test_project_resolution(tmp_path):
    files = {
        "a/__init__.py": "",
        "a/b.py": "X = 1\n",
        "a/c.py": "from . import b\nfrom .d import e\n",
        "a/d/__init__.py": "",
        "a/d/e.py": "",
        "s/run.py": "",
        "t/test_x.py": "import a.b\nP = 's/run.py'\nQ = 'a.d.e'\n",
        "t/test_dyn.py": "import importlib\nimportlib.import_module(n)\n",
        "t/test_lit.py": "import importlib\nimportlib.import_module('a.c')\n",
        "build/gen.py": "",
        "a/dist/x.py": "",
        "a/__pycache__/y.py": "",
    }
    for rel, text in files.items():
        _write(tmp_path / rel, text)
    proj = A._Project(tmp_path, None)
    # Build-output names are skipped only at the top level; tool caches
    # at any depth.
    assert "a/dist/x.py" in proj.files
    assert "build/gen.py" not in proj.files
    assert "a/__pycache__/y.py" not in proj.files
    assert proj.unchanged(None)
    assert proj.deps("a/c.py") >= {"a/__init__.py", "a/b.py", "a/d/__init__.py",
                                   "a/d/e.py"}
    files_x, opaque = proj.closure("t/test_x.py")
    assert {"a/b.py", "s/run.py", "a/d/e.py"} <= files_x and not opaque
    assert proj.closure("t/test_dyn.py")[1] is True
    lit, opaque = proj.closure("t/test_lit.py")
    assert "a/d/e.py" in lit and not opaque
    assert proj.key("t/test_dyn.py", "c") is None
    k1 = proj.key("t/test_x.py", "c")
    _write(tmp_path / "a/b.py", "X = 22\n")
    assert not proj.unchanged(None)
    assert A._Project(tmp_path, None).key("t/test_x.py", "c") != k1


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #

def test_cache_hit_miss_and_invalidation(cached, monkeypatch):
    root, mem = cached
    res = _run(mem)
    assert "3 passed" in res["output"]
    res = _run(mem)
    out = _ozagia(res)
    assert out[0] == ("[ozagia] pytest not run: every selected test has a "
                      "cached pass (exit 0)")
    assert out[1] == f"[ozagia] ran 0 tests of 3; cached: 3 passed; {RUN_ALL}"
    assert sum(ln.startswith("[ozagia] cached pass tests/") for ln in out) == 3
    assert _counts(res)["cached_passes"] == 3
    # A change reaches one test file: it runs, the rest are cached.
    _write(root / "pkg/other.py", "def three():\n    return 3  # edited\n")
    res = _run(mem)
    assert "1 passed" in res["output"]
    assert f"[ozagia] ran 1 tests of 3; cached: 2 passed; {RUN_ALL}" in _ozagia(res)
    assert not any("cached pass tests/test_other.py" in ln for ln in _ozagia(res))
    # A transitive change invalidates the importers' keys.
    _write(root / "pkg/base.py", "def one():\n    return 1  # edited\n")
    res = _run(mem)
    assert f"[ozagia] ran 2 tests of 3; cached: 1 passed; {RUN_ALL}" in _ozagia(res)
    # The environment is part of the key.
    monkeypatch.setenv("OZAGIA_TEST_SOMETHING", "1")
    res = _run(mem)
    assert "3 passed" in res["output"]
    assert not any("cached" in ln for ln in _ozagia(res))


def test_failure_is_never_cached(cached):
    root, mem = cached
    _run(mem)
    _write(root / "pkg/other.py", "def three():\n    return 4\n")
    for _ in range(2):
        res = _run(mem)
        assert ("pytest failed (cached subset: 1 of 3 tests run, 2 cached): "
                "exit 1") in res["output"]
        assert "[ozagia] failed test:tests/test_other.py::test_three" in res["output"]
        assert f"[ozagia] ran 1 tests of 3; cached: 2 passed; {RUN_ALL}" in _ozagia(res)
    state = A.load_state(root)
    assert state["nodes"]["tests/test_other.py::test_three"]["o"] == "failed"


def test_flaky_test_is_remembered_and_not_cached(tmp_path, layout, monkeypatch):
    monkeypatch.setenv("FLIP_FILE", str(tmp_path / "flip"))
    flip = ("import os\n\n\ndef test_flip():\n"
            "    p = os.environ['FLIP_FILE']\n"
            "    n = int(open(p).read()) if os.path.exists(p) else 0\n"
            "    open(p, 'w').write(str(n + 1))\n"
            "    assert n % 2 == 1\n")
    root, mem = _make(tmp_path, CACHED, {"tests/test_flip.py": flip})
    res = _run(mem)                     # fails: n = 0
    assert "pytest failed" in res["output"]
    res = _run(mem)                     # passes with the same inputs
    assert "pytest passed" in res["output"]
    flaky = ("[ozagia] flaky test: tests/test_flip.py::test_flip (passed and "
             "failed with the same inputs; never served from the cache)")
    assert flaky in _ozagia(res)
    assert _ozagia(res)[-1] == flaky
    res = _run(mem)                     # nothing changed: only the flaky one runs
    assert f"[ozagia] ran 1 tests of 4; cached: 3 passed; {RUN_ALL}" in _ozagia(res)
    assert flaky in _ozagia(res)
    assert "tests/test_flip.py::test_flip" in A.load_state(root)["flaky"]


def test_slow_tests_are_reported(tmp_path, layout, monkeypatch):
    monkeypatch.setattr(A, "SLOW_S", 0.05)
    slow = "import time\n\n\ndef test_slow():\n    time.sleep(0.2)\n"
    root, mem = _make(tmp_path, AFFECTED, {"tests/test_slow.py": slow})
    res = _run(mem)
    tail = _ozagia(res)[-1]
    assert tail.startswith("[ozagia] slow test (> 0.05 s): "
                           "tests/test_slow.py::test_slow ")
    assert "tests/test_slow.py::test_slow" in A.load_state(root)["slow"]


def test_state_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "MAX_NODES", 3)
    root = tmp_path
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(tmp_path / "st"))
    with A._locked(root) as data:
        for i in range(10):
            data["nodes"][f"t.py::test_{i}"] = {"k": "k", "o": "passed", "t": i}
    nodes = json.loads((tmp_path / "st" / "tests" / "results.json").read_text())["nodes"]
    assert sorted(nodes) == ["t.py::test_7", "t.py::test_8", "t.py::test_9"]


def test_daemon_counts_test_selection(affected, tmp_path):
    from ozagia.daemon.server import DaemonState
    root, mem = affected
    st = DaemonState(root, mem, tmp_path / "j.jsonl")
    msg = {"op": "tool", "tool": "shell", "harness": "opencode",
           "input": {"command": CMD}}
    st.record(msg, st.dispatch(msg), 1.0)
    _write(root / "pkg/other.py", "def three():\n    return 3  # edited\n")
    st.record(msg, st.dispatch(msg), 1.0)
    stats = st.stats()
    assert (stats["affected_runs"], stats["tests_skipped_unaffected"],
            stats["cached_passes"], stats["full_runs_forced"]) == (1, 2, 0, 1)


# --------------------------------------------------------------------------- #
# failure paths (review W9)
# --------------------------------------------------------------------------- #

EDIT_OTHER = "def three():\n    return 3  # edited\n"
EDIT_BASE = "def one():\n    return 1  # edited\n"


def _suite(root: Path) -> str:
    (suite,) = A.load_state(root)["suites"]
    return suite


BUILD = {
    "pkg/build/__init__.py": "",
    "pkg/build/calc.py": "def four():\n    return 4\n",
    "tests/test_build.py": "from pkg.build.calc import four\n\n\n"
                           "def test_four():\n    assert four() == 4\n",
}


@pytest.mark.parametrize("config", [AFFECTED, CACHED])
def test_nested_build_directory_is_project_source(tmp_path, layout, config):
    root, mem = _make(tmp_path, config, BUILD)
    _run(mem)
    _write(root / "pkg/build/calc.py", "def four():\n    return 5\n")
    _write(root / "pkg/other.py", EDIT_OTHER)
    res = _run(mem)
    assert "[ozagia] failed test:tests/test_build.py::test_four" in res["output"]
    assert not any("cached pass tests/test_build.py" in ln for ln in _ozagia(res))


ENVY = {"tests/test_envy.py": "import os\n\n\ndef test_envy():\n"
                              "    assert not os.path.exists(os.environ['ENVY_MARK'])\n"}


def test_failure_since_the_green_run_is_run_again(tmp_path, layout, monkeypatch):
    mark = tmp_path / "mark"
    monkeypatch.setenv("ENVY_MARK", str(mark))
    root, mem = _make(tmp_path, AFFECTED, ENVY)
    _run(mem)                                   # green
    mark.write_text("x")                        # fails with no file change
    res = _run(mem)
    assert "[ozagia] failed test:tests/test_envy.py::test_envy" in res["output"]
    assert A.load_state(root)["failing"][_suite(root)]["files"].keys() == {
        "tests/test_envy.py"}
    # An unrelated edit: test_envy is not linked to it, but failed last time.
    _write(root / "pkg/base.py", EDIT_BASE)
    res = _run(mem)
    assert ("[ozagia] run again, failed in the last run: tests/test_envy.py"
            in _ozagia(res))
    assert ("pytest failed (affected subset: 3 of 4 tests run, 1 not run): "
            "exit 1") in res["output"]
    mark.unlink()
    res = _run(mem)
    assert "pytest passed (affected subset: 3 of 4 tests run" in res["output"]
    assert A.load_state(root)["failing"] == {}
    res = _run(mem)
    assert "pytest passed (affected subset: 2 of 4 tests run" in res["output"]


def test_environment_or_interpreter_change_runs_everything(affected, monkeypatch):
    root, mem = affected
    _run(mem)
    monkeypatch.setenv("OZAGIA_TEST_SOMETHING", "1")
    _write(root / "pkg/other.py", EDIT_OTHER)
    res = _run(mem)
    assert ("[ozagia] ran all tests: the environment changed since the last "
            "green run") in _ozagia(res)
    assert "pytest passed: exit 0" in res["output"]
    real = A._python_fingerprint
    monkeypatch.setattr(A, "_python_fingerprint", lambda py: "x" + (real(py) or ""))
    _write(root / "pkg/base.py", EDIT_BASE)
    res = _run(mem)
    assert ("[ozagia] ran all tests: the interpreter or its installed packages "
            "changed since the last green run") in _ozagia(res)


MUTATE = {"tests/test_mutate.py": (
    "from pathlib import Path\n\nimport pkg.other\n\n\ndef test_mutate():\n"
    "    p = Path(pkg.other.__file__)\n"
    "    p.write_text(p.read_text() + '# mutated\\n')\n")}


@pytest.mark.parametrize("config", [AFFECTED, CACHED])
def test_edit_during_the_run_is_not_recorded(tmp_path, layout, config):
    root, mem = _make(tmp_path, config, MUTATE)
    res = _run(mem)
    assert "pytest passed: exit 0" in res["output"]
    assert ("[ozagia] green run not recorded: files or the interpreter changed "
            "during the run") in _ozagia(res)
    state = A.load_state(root)
    assert state["suites"] == {}
    if config == CACHED:
        assert "tests/test_mid.py" in state["files"]
        assert "tests/test_other.py" not in state["files"]
        assert "tests/test_mutate.py" not in state["files"]


def test_cache_with_no_test_file_left_runs_as_typed(cached):
    root, mem = cached
    _run(mem)
    for f in (root / "tests").glob("test_*.py"):
        f.unlink()
    res = _run(mem)
    assert not any("cached pass" in ln for ln in _ozagia(res))
    assert "pytest not run" not in res["output"]
    assert ("[ozagia] ran all tests: no test file of the last green run is left"
            in _ozagia(res))
    assert ": exit 5," in res["output"]


def test_opaque_conftest_runs_as_typed_and_names_it(tmp_path, layout):
    conftest = (FILES["tests/conftest.py"]
                + "\n\ndef load(name):\n    import importlib\n"
                  "    return importlib.import_module(name)\n")
    root, mem = _make(tmp_path, AFFECTED, {"tests/conftest.py": conftest})
    _run(mem)
    _write(root / "pkg/other.py", EDIT_OTHER)
    res = _run(mem)
    assert ("[ozagia] ran all tests: every test file is affected by the changes "
            "(an unresolved dynamic import in tests/conftest.py links every "
            "change to the tests that load it)") in _ozagia(res)
    assert "pytest passed: exit 0" in res["output"]
    assert _counts(res)["full_runs_forced"] == 1
    # A run of the command as typed: the green run moved.
    res = _run(mem)
    assert ("[ozagia] ran all tests: no files changed since the last green run"
            in _ozagia(res))


def test_exitfirst_partial_run_stores_no_node_lists(tmp_path, layout, monkeypatch):
    mark = tmp_path / "mark"
    monkeypatch.setenv("ENVY_MARK", str(mark))
    two = ("import os\n\n\ndef test_a():\n"
           "    assert not os.path.exists(os.environ['ENVY_MARK'])\n\n\n"
           "def test_b():\n    pass\n")
    root, mem = _make(tmp_path, CACHED, {"tests/test_two.py": two})
    mark.write_text("x")
    cmd = f"{PY} -m pytest -x -q"
    res = _run(mem, cmd)
    assert "pytest failed: exit 1" in res["output"]
    state = A.load_state(root)
    assert state["files"] == {}
    assert state["nodes"]["tests/test_two.py::test_a"]["o"] == "failed"
    assert "tests/test_two.py::test_b" not in state["nodes"]
    (fails,) = state["failing"].values()
    assert set(fails["files"]) == {"tests/test_two.py"}
    mark.unlink()
    res = _run(mem, cmd)
    assert "pytest passed: exit 0" in res["output"]
    state = A.load_state(root)
    assert state["files"]["tests/test_two.py"]["nodes"] == [
        "tests/test_two.py::test_a", "tests/test_two.py::test_b"]
    assert state["failing"] == {}


def test_root_below_the_git_top_level_runs_everything(tmp_path, layout):
    root, mem = _make(tmp_path, AFFECTED + "cache = true\n", below_top=True)
    for _ in range(2):
        res = _run(mem)
        assert any(ln.startswith("[ozagia] ran all tests: the root is below "
                                 "the git top level ") for ln in _ozagia(res))
        assert not any("cached" in ln for ln in _ozagia(res))
        assert "3 passed" in res["output"]
    assert A.load_state(root)["suites"] == {}


def test_plan_time_is_bounded(affected, monkeypatch):
    root, mem = affected
    _run(mem)
    assert A._plan_budget({"_deadline_s_remaining": 40}) == 10
    assert A._plan_budget({}) == A.MAX_PLAN_S
    monkeypatch.setattr(A, "MAX_PLAN_S", 0.0)
    _write(root / "pkg/other.py", EDIT_OTHER)
    res = _run(mem)
    assert "[ozagia] ran all tests: planning took longer than 0 s" in _ozagia(res)
    assert "3 passed" in res["output"]


def test_malformed_state_entries_are_dropped(affected):
    root, mem = affected
    _run(mem)
    path = root / ".ozagia" / "tests" / "results.json"
    data = json.loads(path.read_text())
    suite = next(iter(data["suites"]))
    data["suites"][suite]["dirty"] = ["pkg/base.py"]
    data["nodes"]["tests/x.py::t"] = {"k": "k", "o": "passed", "t": "soon"}
    data["files"]["tests/x.py"] = {"k": 1, "nodes": "all"}
    data["slow"]["tests/x.py::t"] = {"s": "long", "t": 1}
    data["failing"] = {suite: {"files": ["tests/test_mid.py"]}}
    path.write_text(json.dumps(data))
    state = A.load_state(root)
    assert all(not state[k] for k in ("suites", "nodes", "files", "slow",
                                       "failing"))
    _write(root / "pkg/other.py", EDIT_OTHER)
    res = _run(mem)
    assert ("[ozagia] ran all tests: no fully green run of this suite recorded "
            "yet") in _ozagia(res)
    assert not any("warning" in ln for ln in _ozagia(res))
    assert _suite(root) == suite


def test_state_resets_when_the_write_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(tmp_path / "st"))
    with A._locked(tmp_path) as data:
        data["slow"]["t.py::a"] = {"s": 9.0, "t": 1}
    assert A.load_state(tmp_path)["slow"]

    def boom(table, limit):
        raise RuntimeError("boom")
    monkeypatch.setattr(A, "_prune", boom)
    with pytest.raises(RuntimeError), A._locked(tmp_path) as data:
        data["slow"]["t.py::b"] = {"s": 9.0, "t": 2}
    assert A.load_state(tmp_path) == A._empty()
    assert json.loads((tmp_path / "st" / "tests" / "results.json").read_text()
                      ) == A._empty()


def test_concurrent_writers_keep_every_entry(tmp_path, monkeypatch):
    import threading
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(tmp_path / "st"))
    code = ("import sys\nfrom pathlib import Path\n"
            "from ozagia.intent import affected as A\n"
            "for i in range(15):\n"
            "    with A._locked(Path(sys.argv[1])) as d:\n"
            "        d['slow'][f'p{sys.argv[2]}::{i}'] = {'s': 6.0, 't': i}\n")
    procs = [subprocess.Popen([PY, "-c", code, str(tmp_path), str(n)])
             for n in range(4)]

    def writer(n: int) -> None:
        for i in range(15):
            with A._locked(tmp_path) as d:
                d["slow"][f"t{n}::{i}"] = {"s": 6.0, "t": i}
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert [p.wait(60) for p in procs] == [0] * 4
    slow = A.load_state(tmp_path)["slow"]
    assert len(slow) == 4 * 15 + 3 * 15
    assert not list((tmp_path / "st" / "tests").glob("*.tmp"))
