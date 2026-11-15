"""ozagia.check: pytest runs as graph keys."""
from __future__ import annotations

import json
import os
import textwrap
import time
from pathlib import Path

import pytest

from ozagia import check
from ozagia.check import parse_junit, resolve_selectors, run_tests, selectors_for_keys


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text), encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "pytest.ini", "[pytest]\n")
    _write(root, "conftest.py", """\
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        """)
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/mod.py", """\
        def helper(x):
            assert x > 0, "x must be positive"
            return x


        class Box:
            @staticmethod
            def check(v):
                if v:
                    raise ValueError("bad box")
        """)
    _write(root, "tests/test_a.py", """\
        import pytest
        from pkg.mod import helper, Box


        def test_ok():
            assert helper(1) == 1


        def test_helper_fails():
            helper(-1)


        @pytest.mark.parametrize("a,b", [(1, 2), (3, 3)])
        def test_param(a, b):
            assert a == b


        class TestK:
            def test_in_class(self):
                assert True

            def test_method_fails(self):
                Box.check(True)

            @pytest.mark.skip(reason="not today")
            def test_skip(self):
                pass
        """)
    _write(root, "tests/test_bad.py", "import nonexistent_module_xyz_ozagia\n")
    _write(root, "tests/test_slow.py", """\
        import os, subprocess, time


        def test_slow():
            child = subprocess.Popen(["sleep", "60"])
            with open(os.environ["OZAGIA_TEST_PIDFILE"], "w") as fh:
                fh.write(str(child.pid))
            time.sleep(60)
        """)
    return root


def _by_nodeid(result: dict) -> dict:
    return {t["nodeid"]: t for t in result["tests"]}


def test_outcomes_keys_and_frames(repo: Path) -> None:
    r = run_tests(repo, ["tests/test_a.py"])
    assert r["exit_code"] == 1 and r["status"] == "failed" and not r["ok"]
    assert r["counts"] == {"passed": 3, "failed": 3, "error": 0, "skipped": 1}
    tests = _by_nodeid(r)

    ok = tests["tests/test_a.py::test_ok"]
    assert ok["outcome"] == "passed" and ok["key"] == "test:tests/test_a.py::test_ok"
    assert ok["line"] == 5

    helper = tests["tests/test_a.py::test_helper_fails"]
    assert helper["outcome"] == "failed"
    assert helper["message"] == "AssertionError: x must be positive"
    keys = [f["key"] for f in helper["frames"]]
    assert keys == ["test:tests/test_a.py::test_helper_fails",
                    "sym:pkg/mod.py::helper"]
    assert helper["frames"][-1] == {"file": "pkg/mod.py", "line": 2,
                                    "key": "sym:pkg/mod.py::helper"}

    method = tests["tests/test_a.py::TestK::test_method_fails"]
    assert method["key"] == "test:tests/test_a.py::TestK.test_method_fails"
    assert method["qualname"] == "TestK.test_method_fails"
    assert method["message"] == "ValueError: bad box"
    assert [f["key"] for f in method["frames"]] == [
        "test:tests/test_a.py::TestK.test_method_fails",
        "sym:pkg/mod.py::Box.check"]

    p12 = tests["tests/test_a.py::test_param[1-2]"]
    p33 = tests["tests/test_a.py::test_param[3-3]"]
    assert p12["key"] == p33["key"] == "test:tests/test_a.py::test_param"
    assert (p12["param"], p12["outcome"]) == ("1-2", "failed")
    assert (p33["param"], p33["outcome"]) == ("3-3", "passed")

    in_class = tests["tests/test_a.py::TestK::test_in_class"]
    assert in_class["outcome"] == "passed"
    assert in_class["key"] == "test:tests/test_a.py::TestK.test_in_class"
    skip = tests["tests/test_a.py::TestK::test_skip"]
    assert skip["outcome"] == "skipped" and skip["message"] == "not today"

    assert r["collection_errors"] == []
    assert r["timed_out"] is False and r["seconds"] > 0
    assert len(r["output_tail"].splitlines()) <= check.OUTPUT_TAIL_LINES
    # Nothing written into the repository.
    assert not (repo / ".pytest_cache").exists()
    assert not list(repo.rglob("__pycache__"))


def test_collection_error(repo: Path) -> None:
    r = run_tests(repo, ["tests/test_bad.py"])
    assert not r["ok"] and r["exit_code"] == 2 and r["status"] == "interrupted"
    assert r["tests"] == [] and r["counts"]["error"] == 0
    [err] = r["collection_errors"]
    assert err["key"] == "file:tests/test_bad.py"
    assert "nonexistent_module_xyz_ozagia" in err["message"]
    assert {"file": "tests/test_bad.py", "line": 1,
            "key": "file:tests/test_bad.py"} in err["frames"]
    assert err["frames_dropped"] >= 1  # importlib frames outside the repo


def test_no_tests_collected_is_not_success(repo: Path) -> None:
    r = run_tests(repo, ["tests/test_a.py"], extra_args=["-k", "nomatch_xyz"])
    assert r["exit_code"] == 5
    assert r["ok"] is False
    assert r["status"] == "no_tests_collected"
    assert "no tests collected" in r["error"]


def _dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def test_timeout_kills_process_group(repo: Path, tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    t0 = time.monotonic()
    r = run_tests(repo, ["tests/test_slow.py"], timeout_s=2,
                  env={"OZAGIA_TEST_PIDFILE": str(pidfile)})
    elapsed = time.monotonic() - t0
    assert r["timed_out"] is True and r["status"] == "timed_out"
    assert r["ok"] is False
    assert elapsed < 2 + check.KILL_GRACE_S + 5
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while not _dead(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _dead(child), f"sleep child {child} survived the timeout"


def test_parse_junit_without_file_attribute(repo: Path) -> None:
    xml = """<?xml version="1.0"?><testsuites><testsuite>
      <testcase classname="tests.test_a" name="test_ok" time="0.5"/>
      <testcase classname="tests.test_a.TestK" name="test_in_class[x-y]" time="0.1">
        <failure message="boom&#10;second line">pkg/mod.py:2: AssertionError
/usr/lib/python3/somewhere.py:10: in f</failure>
      </testcase>
      <testcase classname="nowhere.test_gone.TestX" name="test_y"/>
    </testsuite></testsuites>"""
    got = parse_junit(repo, xml)
    ok, cls, gone = got["tests"]
    assert ok["key"] == "test:tests/test_a.py::test_ok" and ok["seconds"] == 0.5
    assert cls["nodeid"] == "tests/test_a.py::TestK::test_in_class[x-y]"
    assert cls["key"] == "test:tests/test_a.py::TestK.test_in_class"
    assert cls["param"] == "x-y" and cls["message"] == "boom"
    assert cls["frames"] == [{"file": "pkg/mod.py", "line": 2,
                              "key": "sym:pkg/mod.py::helper"}]
    assert cls["frames_dropped"] == 1
    assert gone["resolved"] is False
    assert gone["key"] == "test:nowhere/test_gone.py::TestX.test_y"


def test_frames_decorators_and_module_level(repo: Path) -> None:
    # Line 7 is the @staticmethod decorator of Box.check; line 1 of a
    # non-Python file and a module-level line map to the file.
    text = ("pkg/mod.py:7: in check\npkg/mod.py:6: in Box\n"
            "pytest.ini:1: x\n.venv/lib/x.py:1: in y\n")
    _write(repo, ".venv/lib/x.py", "y = 1\n")
    frames, dropped = check.parse_frames(repo, text)
    assert [f["key"] for f in frames] == [
        "sym:pkg/mod.py::Box.check", "sym:pkg/mod.py::Box", "file:pytest.ini"]
    assert dropped == 1
    _write(repo, "pkg/flat.py", "x = 1\ny = 2\n")
    assert check.symbol_at(repo, "pkg/flat.py", 2) == "file:pkg/flat.py"


def test_selectors_for_keys(repo: Path) -> None:
    keys = ["test:tests/test_a.py::TestK.test_in_class",
            "test:tests/test_a.py::test_ok",
            "file:tests/test_a.py",
            "file:pkg/mod.py",
            "sym:pkg/mod.py::helper",
            "test:tests/test_missing.py::test_x",
            "garbage"]
    assert selectors_for_keys(repo, keys) == [
        "tests/test_a.py::TestK::test_in_class",
        "tests/test_a.py::test_ok",
        "tests/test_a.py"]
    ignored = {i["key"]: i["reason"] for i in resolve_selectors(repo, keys)["ignored"]}
    assert set(ignored) == {"file:pkg/mod.py", "sym:pkg/mod.py::helper",
                            "test:tests/test_missing.py::test_x", "garbage"}
    r = run_tests(repo, selectors_for_keys(repo, keys[:2]))
    assert r["ok"] and r["counts"]["passed"] == 2


def test_cli_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = check.main(["--root", str(repo), "--json",
                       "test:tests/test_a.py::test_ok", "file:pkg/mod.py"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] and out["counts"]["passed"] == 1
    assert out["ignored_keys"][0]["key"] == "file:pkg/mod.py"


def test_run_tests_env_replaces_the_environment_without_inherit(tmp_path, monkeypatch):
    # review fix 7: the agent's shell environment, not the tool process's.
    import sys
    _write(tmp_path, "tests/test_env.py",
           "import os\n\n\ndef test_env():\n"
           "    assert os.environ.get('ONLY') == 'yes'\n"
           "    assert 'LEAKED' not in os.environ\n")
    monkeypatch.setenv("LEAKED", "secret")
    env = {"PATH": os.environ["PATH"], "ONLY": "yes"}
    res = run_tests(tmp_path, ["tests/test_env.py"], python=sys.executable,
                    env=env, inherit=False)
    assert res["ok"] is True, res["output_tail"]
    res = run_tests(tmp_path, ["tests/test_env.py"], python=sys.executable, env=env)
    assert res["counts"]["failed"] == 1   # inherited: LEAKED is there
