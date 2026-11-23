"""`cargo test` capture, parsing and affected-target selection.

The parser and model tests run without Cargo. The end-to-end tests build a
two-crate workspace and are skipped when `cargo` is not installed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ozagia import intent as I
from ozagia.config import clear_config_cache, load_project_config
from ozagia.core import rust_tests
from ozagia.core.memory import DevMemory, build_index
from ozagia.intent import affected_cargo as AC
from ozagia.intent import cargo as C
from ozagia.intent.shell import classify

CARGO = shutil.which("cargo")
needs_cargo = pytest.mark.skipif(CARGO is None, reason="cargo is not installed")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


WORKSPACE = {
    "Cargo.toml": '[workspace]\nmembers = ["a", "b"]\nresolver = "2"\n',
    "a/Cargo.toml": '[package]\nname = "a"\nversion = "0.1.0"\nedition = "2021"\n',
    "a/src/lib.rs": (
        "pub fn add(x: i32, y: i32) -> i32 { x + y }\n\n"
        "#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
        "    #[test]\n    fn adds() { assert_eq!(add(1, 2), 3); }\n\n"
        "    #[test]\n    #[ignore]\n    fn skipped() {}\n}\n\n"
        "/// Adds one.\n///\n/// ```\n/// assert_eq!(a::add_one(1), 2);\n/// ```\n"
        "pub fn add_one(x: i32) -> i32 { x + 1 }\n"),
    "a/tests/integration.rs": (
        "#[test]\nfn integration_ok() { assert_eq!(a::add(2, 2), 4); }\n\n"
        "mod nested {\n    #[test]\n    fn nested_ok() { assert!(true); }\n}\n"),
    "b/Cargo.toml": '[package]\nname = "b"\nversion = "0.1.0"\nedition = "2021"\n',
    "b/src/lib.rs": (
        "pub fn sub(x: i32, y: i32) -> i32 { x - y }\n\n"
        "#[cfg(test)]\nmod tests {\n    #[test]\n"
        "    fn subs() { assert_eq!(super::sub(3, 1), 2); }\n}\n"),
    "b/tests/other.rs": "#[test]\nfn other_ok() { assert!(true); }\n",
    ".gitignore": "/target\n.ozagia/\n",
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "HOME": str(root), "PATH": "/usr/bin:/bin:/usr/local/bin"})


def _make(root: Path, config: str | None = None,
          extra: dict[str, str] | None = None) -> DevMemory:
    for rel, text in {**WORKSPACE, **(extra or {})}.items():
        _write(root / rel, text)
    if config is not None:
        _write(root / ".ozagia.toml", config)
    if CARGO is not None:
        subprocess.run([CARGO, "generate-lockfile"], cwd=root, check=True,
                       capture_output=True)
    if shutil.which("git"):
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "initial")
    dump = root.parent / f"{root.name}_state" / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    return DevMemory(dump, dump.parent / "journal.jsonl", root=root)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for var in ("OZAGIA_CONFIG", "OZAGIA_ADAPTERS", "OZAGIA_STATE_DIR",
                AC.A.ENV_ALL, "OZAGIA_TESTING_CAPTURED", "RUSTUP_TOOLCHAIN",
                "CARGO_TARGET_DIR"):
        monkeypatch.delenv(var, raising=False)
    clear_config_cache()
    yield
    clear_config_cache()


def _run(mem: DevMemory, command: str = "cargo test", **inp) -> dict:
    res = I.handle(mem, "shell", {"command": command, **inp})
    assert res["handled"] and not res["is_error"], res
    assert res["meta"]["intent"] == "shell_cargo_test"
    return res


def _ozagia(res: dict) -> list[str]:
    return [ln for ln in res["output"].splitlines()
            if ln.startswith("[ozagia] ")]


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

def test_classify_cargo_test_forms():
    got = classify("cargo test")
    assert got["class"] == "cargo_test"
    assert got["args"] == ["test"]
    assert got["selection_safe"] is True

    got = classify("/usr/bin/cargo +nightly test --workspace --exclude b "
                   "-p a --test integration --features x,y --release "
                   "--no-fail-fast -- --nocapture --test-threads=1")
    assert got["class"] == "cargo_test"
    assert got["tool"] == "/usr/bin/cargo"
    assert got["toolchain"] == "nightly"
    assert got["workspace"] is True
    assert got["packages"] == ["a"]
    assert got["excludes"] == ["b"]
    assert got["test_targets"] == ["integration"]
    assert got["features"] == ["x,y"]
    assert got["release"] is True
    assert got["no_fail_fast"] is True
    assert got["extra_args"] == ["--nocapture", "--test-threads=1"]
    assert got["selection_safe"] is True


def test_cargo_filter_and_harness_options_disable_selection():
    got = classify("cargo test my_filter")
    assert got["class"] == "cargo_test"
    assert got["filter"] == "my_filter"
    assert got["selection_safe"] is False

    got = classify("cargo test -- --ignored")
    assert got["class"] == "cargo_test"
    assert got["selection_safe"] is False


@pytest.mark.parametrize("command", [
    "cargo build",
    "cargo test -p",
    "cargo test --message-format json",
    "cargo test -Z unstable-options",
    "cargo test --timings",
    "cargo test -- --format json",
    "cargo nextest run",
])
def test_unmapped_cargo_forms_run_as_typed(command):
    assert classify(command)["class"] == "other"


def test_cargo_output_filter_is_still_captured():
    got = classify("cargo test 2>&1 | tail -12")
    assert got["class"] == "cargo_test"
    assert got["output_filter"] == ("tail", 12)


def test_cargo_capture_is_lock_free():
    from ozagia.daemon.server import _lock_free
    assert _lock_free("tool", "shell", {"command": "cargo test"})
    assert _lock_free("tool", "shell", {"command": "cargo test -p a"})
    assert not _lock_free("tool", "shell", {"command": "cargo build"})
    assert not _lock_free("tool", "edit", {"command": "cargo test"})


# --------------------------------------------------------------------------- #
# the Cargo test model
# --------------------------------------------------------------------------- #

def test_rust_tests_model_maps_targets_and_cargo_names(tmp_path):
    root = tmp_path / "repo"
    for rel, text in WORKSPACE.items():
        _write(root / rel, text)
    model = rust_tests.discover(root)
    assert sorted(model.package_targets) == ["a", "b"]
    assert model.package_lib == {"a": "a/src/lib.rs", "b": "b/src/lib.rs"}

    a_lib = "a/Cargo.toml:lib:a/src/lib.rs"
    a_test = "a/Cargo.toml:test:a/tests/integration.rs"
    assert model.default_targets(["a"]) == [
        a_lib, a_test, model.doc_target_id("a")]
    assert [t.cargo for t in model.tests_of(a_lib)] == [
        "tests::adds", "tests::skipped"]
    assert [t.cargo for t in model.tests_of(a_test)] == [
        "integration_ok", "nested::nested_ok"]
    entry = model.locate(a_test, "nested::nested_ok")
    assert entry is not None
    assert entry.key == "test:a/tests/integration.rs::nested.nested_ok"
    assert model.locate(a_test, "missing") is None


def test_rust_tests_model_uses_custom_target_paths(tmp_path):
    root = tmp_path / "repo"
    for rel, text in WORKSPACE.items():
        _write(root / rel, text)
    _write(root / "a/Cargo.toml",
           '[package]\nname = "a"\nversion = "0.1.0"\nedition = "2021"\n'
           '[[test]]\nname = "custom"\npath = "checks/custom.rs"\n')
    _write(root / "a/checks/custom.rs", "#[test]\nfn custom_ok() {}\n")
    model = rust_tests.discover(root)
    target = next(t for t in model.targets.values() if t.kind == "test"
                  and t.name == "custom")
    assert target.root == "a/checks/custom.rs"
    assert target.test is True and target.harness is True
    assert [t.cargo for t in model.tests_of(target.id)] == ["custom_ok"]


def test_rust_tests_model_excludes_test_false_targets(tmp_path):
    root = tmp_path / "repo"
    _write(root / "Cargo.toml",
           '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'
           '[lib]\npath = "src/lib.rs"\ntest = false\n'
           '[[bin]]\nname = "tool"\npath = "src/main.rs"\ntest = false\n'
           '[[test]]\nname = "skip"\npath = "tests/skip.rs"\ntest = false\n')
    _write(root / "src/lib.rs", "#[test]\nfn lib_unit() {}\n")
    _write(root / "src/main.rs", "fn main() {}\n\n#[test]\nfn bin_unit() {}\n")
    _write(root / "tests/skip.rs", "#[test]\nfn skipped() {}\n")
    model = rust_tests.discover(root)
    lib = next(t for t in model.targets.values() if t.kind == "lib")
    skip = next(t for t in model.targets.values() if t.name == "skip")
    assert lib.test is False and lib.harness is True
    assert skip.test is False
    # Their unit tests are still mapped, but a plain `cargo test` does not
    # select or count them. The lib's doctests still run.
    assert [t.cargo for t in model.tests_of(lib.id)] == ["lib_unit"]
    assert model.default_targets(["demo"]) == [model.doc_target_id("demo")]
    assert model.tests_of(skip.id)


def test_rust_tests_model_custom_harness_runs_as_typed(tmp_path):
    root = tmp_path / "repo"
    for rel, text in WORKSPACE.items():
        _write(root / rel, text)
    _write(root / "a/Cargo.toml",
           '[package]\nname = "a"\nversion = "0.1.0"\nedition = "2021"\n'
           '[[test]]\nname = "custom"\npath = "checks/custom.rs"\n'
           'harness = false\n')
    _write(root / "a/checks/custom.rs", "#[test]\nfn custom_ok() {}\n")
    _write(root / ".ozagia.toml", '[tests]\nselect = "affected"\n')
    model = rust_tests.discover(root)
    target = next(t for t in model.targets.values() if t.kind == "test"
                  and t.name == "custom")
    assert target.root == "a/checks/custom.rs" and target.harness is False
    assert [t.cargo for t in model.tests_of(target.id)] == ["custom_ok"]
    assert model.custom_harness(target.id)
    assert not model.custom_harness(model.doc_target_id("a"))
    plan = AC.plan(None, classify("cargo test"), {}, root=root, wd=root,
                   cargo=CARGO or "cargo")
    assert plan is not None and plan.full
    assert plan.reason == "a target uses a custom test harness"


def test_parse_custom_harness_output_is_not_libtest(tmp_path):
    root = tmp_path / "repo"
    _write(root / "Cargo.toml",
           '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'
           '[[test]]\nname = "custom"\npath = "checks/custom.rs"\n'
           'harness = false\n')
    _write(root / "checks/custom.rs", "#[test]\nfn custom_ok() {}\n")
    model = rust_tests.discover(root)
    text = (
        "     Running tests/custom.rs (target/debug/deps/custom-0123456789abcdef)\n"
        "this harness prints anything\n"
        "test result: ok. 99 passed; 0 failed; 0 ignored; 0 measured; "
        "0 filtered out; finished in 0.01s\n")
    parsed = C.parse(text, model, order=model.default_targets(["demo"]))
    assert parsed["counts"] == {"passed": 0, "failed": 0, "ignored": 0,
                                "measured": 0, "filtered": 0}
    assert parsed["tests"] == []
    assert parsed["blocks"][0]["harness"] is False


def test_rust_tests_model_tracks_workspace_dependencies(tmp_path):
    root = tmp_path / "repo"
    for rel, text in WORKSPACE.items():
        _write(root / rel, text)
    _write(root / "b/Cargo.toml",
           '[package]\nname = "b"\nversion = "0.1.0"\nedition = "2021"\n'
           '[dependencies]\na = { path = "../a" }\n')
    model = rust_tests.discover(root)
    assert model.package_deps["b"] == {"a"}
    assert model.dependents("a") == {"b"}
    b_lib = "b/Cargo.toml:lib:b/src/lib.rs"
    assert "a/src/lib.rs" in model.target_deps(b_lib)


# --------------------------------------------------------------------------- #
# output parsing
# --------------------------------------------------------------------------- #

GREEN = """\
     Running unittests src/lib.rs (target/debug/deps/a-96bbe4f6647b0b8a)

running 2 tests
test tests::adds ... ok
test tests::skipped ... ignored

test result: ok. 1 passed; 0 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s

     Running tests/integration.rs (target/debug/deps/integration-5170b36)

running 1 test
test integration_ok ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

   Doc-tests a

running 1 test
test a/src/lib.rs - add_one (line 20) ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.08s
"""

FAIL = """\
     Running unittests src/lib.rs (target/debug/deps/a-96bbe4f6647b0b8a)

running 2 tests
test tests::adds ... FAILED
test tests::skipped ... ignored

failures:

---- tests::adds stdout ----

thread 'tests::adds' panicked at a/src/lib.rs:8:18:
assertion `left == right` failed
  left: 4
 right: 3
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

failures:
    tests::adds

test result: FAILED. 0 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s

error: test failed, to rerun pass `-p a --lib`
"""

BUILD_FAIL = """\
   Compiling a v0.1.0 (/tmp/a)
error: expected one of `!` or `::`, found `is`
  --> a/src/lib.rs:24:6
   |
24 | this is not rust
   |      ^^ expected one of `!` or `::`

error: could not compile `a` (lib) due to 1 previous error
"""


@pytest.fixture
def model(tmp_path):
    root = tmp_path / "repo"
    for rel, text in WORKSPACE.items():
        _write(root / rel, text)
    return rust_tests.discover(root)


def test_parse_green_output_maps_targets_and_keys(model):
    parsed = C.parse(GREEN, model, order=model.default_targets(["a"]))
    assert parsed["counts"] == {"passed": 3, "failed": 0, "ignored": 1,
                                "measured": 0, "filtered": 0}
    by_cargo = {t["cargo"]: t for t in parsed["tests"]}
    assert by_cargo["tests::adds"]["key"] == "test:a/src/lib.rs::tests.adds"
    assert by_cargo["integration_ok"]["key"] == (
        "test:a/tests/integration.rs::integration_ok")
    assert by_cargo["a/src/lib.rs - add_one (line 20)"]["file"] == "a/src/lib.rs"
    assert parsed["errors"] == []


def test_parse_failure_records_location_and_message(model):
    parsed = C.parse(FAIL, model, order=model.default_targets(["a"]))
    failed = [t for t in parsed["tests"] if t["outcome"] == "failed"]
    assert len(failed) == 1
    got = failed[0]
    assert got["key"] == "test:a/src/lib.rs::tests.adds"
    assert got["file"] == "a/src/lib.rs"
    assert got["line"] == 8
    assert got["message"] == "assertion `left == right` failed"
    assert got["frames"][0]["line"] == 8


def test_parse_build_failure_is_not_a_test_failure(model):
    parsed = C.parse(BUILD_FAIL, model)
    assert parsed["counts"]["failed"] == 0
    assert parsed["errors"][0]["file"] == "a/src/lib.rs"
    assert parsed["errors"][0]["line"] == 24
    assert parsed["errors"][0]["key"] == "file:a/src/lib.rs"
    raw = {"exit_code": 101, "timed_out": False}
    assert C.status_for(raw, parsed) == "build_failed"


def test_parse_quiet_failure_line(model):
    text = (
        "running 2 tests\n"
        "tests::adds --- FAILED\n\n"
        "failures:\n    tests::adds\n\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; "
        "1 filtered out; finished in 0.00s\n")
    parsed = C.parse(text, model)
    failed = [t for t in parsed["tests"] if t["outcome"] == "failed"]
    assert [t["cargo"] for t in failed] == ["tests::adds"]
    assert parsed["counts"]["failed"] == 1


# --------------------------------------------------------------------------- #
# target/package helpers
# --------------------------------------------------------------------------- #

def test_base_args_keeps_features_and_drops_selectors():
    cls = classify("cargo +nightly test -p a --test integration "
                   "--features x --release --no-fail-fast -- --nocapture")
    assert AC._base_args(cls) == [
        "--features", "x", "--release", "--no-fail-fast", "--", "--nocapture"]


def test_workspace_scope_root_package_is_not_every_member(tmp_path):
    root = tmp_path / "repo"
    for rel, text in WORKSPACE.items():
        _write(root / rel, text)
    _write(root / "Cargo.toml",
           '[package]\nname = "root"\nversion = "0.1.0"\nedition = "2021"\n'
           '[workspace]\nmembers = ["a", "b"]\n')
    _write(root / "src/lib.rs", "")
    model = rust_tests.discover(root)
    plain = classify("cargo test")
    assert AC._workspace_scope(root, model, plain) == {"root"}
    workspace = classify("cargo test --workspace")
    assert AC._workspace_scope(root, model, workspace) == {"root", "a", "b"}


# --------------------------------------------------------------------------- #
# end to end with Cargo
# --------------------------------------------------------------------------- #

def _counts(res: dict) -> dict:
    return (res.get("meta") or {}).get("tests") or {}


@needs_cargo
def test_captured_cargo_test_reports_a_head_line_and_keys(tmp_path):
    root = tmp_path / "repo"
    mem = _make(root)
    res = _run(mem)
    head = [ln for ln in _ozagia(res) if "cargo test" in ln][0]
    assert "cargo test passed" in head
    assert "6 passed" in head and "1 ignored" in head
    assert "test results not stored" not in res["output"]
    assert res["meta"]["intent"] == "shell_cargo_test"


@needs_cargo
def test_affected_selection_runs_only_linked_crates(tmp_path):
    root = tmp_path / "repo"
    mem = _make(root, '[tests]\nselect = "affected"\n')
    _run(mem)
    lib = root / "a/src/lib.rs"
    lib.write_text(lib.read_text() + "\n// changed\n", encoding="utf-8")
    clear_config_cache()
    res = _run(mem)
    output = res["output"]
    assert "cargo test passed (affected subset" in output
    assert "-p b --lib" in output and "-p b --test other" in output
    assert "Running unittests src/lib.rs" in output
    assert "Running tests/integration.rs" in output
    assert "Running tests/other.rs" not in output
    counts = _counts(res)
    assert counts["tests_skipped_unaffected"] == 2
    assert counts["affected_runs"] == 1


@needs_cargo
def test_affected_new_test_target_runs_only_that_target(tmp_path):
    root = tmp_path / "repo"
    mem = _make(root, '[tests]\nselect = "affected"\n')
    _run(mem)
    _write(root / "b/tests/extra.rs", "#[test]\nfn extra_ok() {}\n")
    clear_config_cache()
    res = _run(mem)
    output = res["output"]
    assert "Running tests/extra.rs" in output
    assert "Running tests/other.rs" not in output
    assert "Running unittests src/lib.rs" not in output


@needs_cargo
def test_affected_selection_follows_workspace_dependencies(tmp_path):
    root = tmp_path / "repo"
    extra = {
        "b/Cargo.toml": (
            '[package]\nname = "b"\nversion = "0.1.0"\nedition = "2021"\n'
            '[dependencies]\na = { path = "../a" }\n'),
        "b/src/lib.rs": "pub fn value() -> i32 { a::add(1, 1) }\n\n"
                        "#[cfg(test)]\nmod tests {\n    #[test]\n"
                        "    fn value_ok() { assert_eq!(super::value(), 2); }\n}\n",
    }
    mem = _make(root, '[tests]\nselect = "affected"\n', extra)
    _run(mem)
    (root / "a/src/lib.rs").write_text(
        (root / "a/src/lib.rs").read_text() + "\n// changed\n",
        encoding="utf-8")
    clear_config_cache()
    res = _run(mem)
    output = res["output"]
    assert "Running tests/other.rs" in output
    assert "compiling b" in output.lower()
    assert "not run:" not in output


@needs_cargo
def test_cache_skips_green_targets_and_invalidates_changed_ones(tmp_path):
    root = tmp_path / "repo"
    mem = _make(root, "[tests]\ncache = true\n")
    _run(mem)
    clear_config_cache()
    cached = _run(mem)
    assert "every selected target has a cached pass" in cached["output"]
    assert _counts(cached)["cached_passes"] == 6

    (root / "b/src/lib.rs").write_text(
        (root / "b/src/lib.rs").read_text() + "\n// changed\n",
        encoding="utf-8")
    clear_config_cache()
    res = _run(mem)
    output = res["output"]
    assert "cached subset" in output
    assert "cached pass -p a --lib" in output
    assert "cached pass -p a --test integration" in output
    assert "cached pass -p b" not in output
    assert "Compiling b" in output


@needs_cargo
def test_failure_is_not_cached(tmp_path):
    root = tmp_path / "repo"
    mem = _make(root, "[tests]\ncache = true\n")
    _run(mem)
    lib = root / "a/src/lib.rs"
    lib.write_text(lib.read_text().replace("add(1, 2), 3", "add(1, 2), 4"),
                   encoding="utf-8")
    clear_config_cache()
    first = _run(mem)
    assert "cargo test failed" in first["output"]
    assert "failed test:a/src/lib.rs::tests.adds" in first["output"]
    clear_config_cache()
    second = _run(mem)
    assert "Running unittests src/lib.rs" in second["output"]
    assert "cargo test failed" in second["output"]


@needs_cargo
def test_filter_and_package_options_run_as_typed(tmp_path):
    root = tmp_path / "repo"
    mem = _make(root, '[tests]\nselect = "affected"\n')
    _run(mem)
    clear_config_cache()
    res = _run(mem, "cargo test -p b --test other other_ok")
    output = res["output"]
    assert "Running tests/other.rs" in output
    assert "Running tests/integration.rs" not in output
    assert "affected subset" not in output
    assert "failed test" not in output


@needs_cargo
def test_ozagia_tests_all_overrides_affected(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    mem = _make(root, '[tests]\nselect = "affected"\n')
    _run(mem)
    (root / "a/src/lib.rs").write_text(
        (root / "a/src/lib.rs").read_text() + "\n// changed\n",
        encoding="utf-8")
    monkeypatch.setenv(AC.A.ENV_ALL, "1")
    clear_config_cache()
    res = _run(mem)
    assert "ran all tests: OZAGIA_TESTS_ALL=1" in res["output"]
    assert "Running tests/other.rs" in res["output"]


@needs_cargo
def test_state_entry_is_bounded_and_config_loads(tmp_path):
    root = tmp_path / "repo"
    mem = _make(root, '[tests]\nselect = "affected"\ncache = true\n')
    _run(mem)
    cfg = load_project_config(root)
    assert (cfg.tests_select, cfg.tests_cache) == ("affected", True)
    data = json.loads((root / ".ozagia/tests/results.json").read_text())
    assert len(data["cargo"]) == 1


def test_integration_tests_link_to_the_crate_library(tmp_path):
    root = tmp_path / "repo"
    for rel, text in WORKSPACE.items():
        _write(root / rel, text)
    dump = tmp_path / "state" / "graph.json"
    build_index(root, dump, include_docs="none", include_git=False)
    dev = DevMemory(dump, tmp_path / "state" / "journal.jsonl", root=root)
    from ozagia.intent.common import covering_tests, file_nid
    fid = file_nid(dev, "a/src/lib.rs")
    covered = {str((dev.node(tid) or {}).get("attrs", {}).get("qualname"))
               for tid in covering_tests(dev, fid)}
    assert {"tests.adds", "tests.skipped", "integration_ok",
            "nested.nested_ok"} <= covered
