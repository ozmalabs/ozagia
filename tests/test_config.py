"""Project configuration: lookup order, defaults, validation, adapters
resolved from the config file's directory, and the "." source dir."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from ozagia import adapters
from ozagia.config import (
    DEFAULT_IAC_SKIP_GLOBS,
    GENERIC_SKIP_DIRS,
    ConfigError,
    clear_config_cache,
    config_path,
    config_sources,
    load_project_config,
    repo_config_path,
    state_path,
    state_rel,
)
from ozagia.core import memory as memory_module
from ozagia.core import repo_map as repo_map_module
from ozagia.core.memory import DevMemory, build_index


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch) -> None:
    for name in ("OZAGIA_CONFIG", "OZAGIA_STATE_DIR", "OZAGIA_ADAPTERS"):
        monkeypatch.delenv(name, raising=False)


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _write_repo_config(root: Path, text: str) -> Path:
    return _write(root / "ozagia" / "config.toml", text)


def test_defaults_without_a_config_file(tmp_path: Path) -> None:
    cfg = load_project_config(tmp_path)
    assert cfg.source is None
    assert cfg.root == tmp_path.resolve()
    assert cfg.source_dirs == (".",)
    assert cfg.test_dirs == ("tests",)
    # "tests" is inside ".", so the map scans the root once.
    assert cfg.map_dirs == (".",)
    assert cfg.skip_dirs == GENERIC_SKIP_DIRS
    assert cfg.memory_skip_dirs == GENERIC_SKIP_DIRS
    assert ".ozagia" in cfg.skip_dirs
    assert not {"data", "var", "runs", "vizdoom"} & cfg.skip_dirs
    assert cfg.docs == ("README.md", "docs/**/*.md")
    assert cfg.docs_all == ("**/*.md",)
    assert cfg.key_docs == ("README.md",)
    assert cfg.command_docs == ("README.md",)
    assert cfg.import_roots == ()
    assert cfg.path_prefixes == ("tests", "docs")
    assert ".ozagia/" in cfg.changed_exclude
    assert cfg.entrypoint_prefixes == ("scripts/", "tools/", "bin/")
    assert cfg.adapters == ()
    assert cfg.iac_skip_globs == DEFAULT_IAC_SKIP_GLOBS


def test_derived_defaults_follow_the_dirs(tmp_path: Path) -> None:
    _write(tmp_path / ".ozagia.toml", """
[index]
source_dirs = ["src/pkg", "./lib/", "tests"]
test_dirs = ["tests", "more_tests"]
key_docs = ["README.md", "HACKING.md"]
""")
    cfg = load_project_config(tmp_path)
    assert cfg.source_dirs == ("src/pkg", "lib", "tests")
    assert cfg.map_dirs == ("src/pkg", "lib", "tests", "more_tests")
    assert cfg.path_prefixes == ("src", "lib", "tests", "more_tests", "docs")
    assert cfg.command_docs == ("README.md", "HACKING.md")


def test_lookup_order(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    in_state = _write(state_path(root, "config.toml"),
                      '[index]\nsource_dirs = ["from_state"]\n')
    cfg = load_project_config(root)
    assert cfg.source == in_state and cfg.source_dirs == ("from_state",)

    at_root = _write(root / ".ozagia.toml", '[index]\nsource_dirs = ["from_root"]\n')
    cfg = load_project_config(root)
    assert cfg.source == at_root and cfg.source_dirs == ("from_root",)

    env = _write(tmp_path / "elsewhere" / "cfg.toml",
                 '[index]\nsource_dirs = ["from_env"]\n')
    monkeypatch.setenv("OZAGIA_CONFIG", str(env))
    cfg = load_project_config(root)
    assert cfg.source == env.resolve() and cfg.source_dirs == ("from_env",)

    monkeypatch.setenv("OZAGIA_CONFIG", str(tmp_path / "missing.toml"))
    with pytest.raises(ConfigError, match="not a file"):
        load_project_config(root)


def test_state_dir_config_follows_ozagia_state_dir(tmp_path: Path,
                                                   monkeypatch) -> None:
    monkeypatch.setenv("OZAGIA_STATE_DIR", "work/state")
    cfg_file = _write(tmp_path / "work" / "state" / "config.toml",
                      '[index]\ntest_dirs = ["t"]\n')
    cfg = load_project_config(tmp_path)
    assert cfg.source == cfg_file.resolve()
    assert cfg.test_dirs == ("t",)
    assert state_rel(tmp_path) == "work/state"


def test_skip_dirs_extend_the_generic_set(tmp_path: Path) -> None:
    _write(tmp_path / ".ozagia.toml", """
[index]
skip_dirs = ["data", "var"]

[memory]
skip_dirs = ["vizdoom"]
""")
    cfg = load_project_config(tmp_path)
    assert cfg.skip_dirs == GENERIC_SKIP_DIRS | {"data", "var"}
    assert cfg.memory_skip_dirs == cfg.skip_dirs | {"vizdoom"}


def test_edit_is_picked_up(tmp_path: Path) -> None:
    p = _write(tmp_path / ".ozagia.toml", '[index]\nsource_dirs = ["a"]\n')
    assert load_project_config(tmp_path).source_dirs == ("a",)
    p.write_text('[index]\nsource_dirs = ["b"]\n', encoding="utf-8")
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert load_project_config(tmp_path).source_dirs == ("b",)


# --------------------------------------------------------------------------- #
# versioned ozagia/config.toml and legacy precedence
# --------------------------------------------------------------------------- #
def test_config_sources_report_both_files_and_the_active_one(
        tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    sources = config_sources(root)
    assert sources.active is None and sources.active_kind is None
    assert sources.legacy is None and sources.repo_config == repo_config_path(root)
    assert sources.repo_exists is False
    assert sources.precedence == () and sources.paths == ()
    assert config_path(root) is None

    repo = _write_repo_config(root, '[index]\nskip_dirs = ["vendor"]\n')
    sources = config_sources(root)
    assert sources.active == repo and sources.active_kind == "repo"
    assert sources.precedence == ("repo",) and sources.paths == (repo,)
    assert config_path(root) == repo

    legacy = _write(root / ".ozagia.toml",
                    '[index]\nsource_dirs = ["src"]\n')
    sources = config_sources(root)
    assert sources.active == legacy and sources.active_kind == "legacy"
    assert sources.legacy_kind == "root"
    assert sources.precedence == ("legacy", "repo")
    assert sources.paths == (legacy, repo)
    assert config_path(root) == legacy

    state_root = tmp_path / "state_repo"
    state_root.mkdir()
    state = _write(state_path(state_root, "config.toml"),
                   '[index]\ntest_dirs = ["t"]\n')
    sources = config_sources(state_root)
    assert sources.active == state and sources.active_kind == "legacy"
    assert sources.legacy_kind == "state"


def test_env_config_at_the_versioned_path_keeps_the_registry_schema(
        tmp_path: Path, monkeypatch) -> None:
    repo = _write_repo_config(tmp_path, """
[testing]
captured = "affected"
cache = true
""")
    monkeypatch.setenv("OZAGIA_CONFIG", str(repo))
    sources = config_sources(tmp_path)
    assert sources.legacy is None
    assert sources.active == repo.resolve()
    assert sources.precedence == ("repo",)
    cfg = load_project_config(tmp_path)
    assert (cfg.tests_select, cfg.tests_cache) == ("affected", True)


def test_env_config_from_another_root_accepts_registry_sections(
        tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    config = _write_repo_config(source, """
[index]
source_dirs = ["src"]

[adapters]
modules = ["repo_cfg_adapter_pkg:register"]

[testing]
captured = "affected"
cache = true

[hints]
library = "strict"
""")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("OZAGIA_CONFIG", str(config))
    cfg = load_project_config(work)
    assert cfg.source == config.resolve()
    assert cfg.sources == (config.resolve(),)
    assert cfg.source_dirs == ("src",)
    assert cfg.adapter_base == source.resolve()
    assert (cfg.tests_select, cfg.tests_cache) == ("affected", True)
    assert cfg.unmapped == (("hints", "read by ozagia.core.library_hints"),)


def test_versioned_config_supplies_every_supported_group(tmp_path: Path) -> None:
    config = _write_repo_config(tmp_path, """
[index]
source_dirs = ["src", "lib"]
map_dirs = ["src"]
test_dirs = ["tests", "it"]
skip_dirs = ["vendor"]
docs = ["README.md"]
docs_all = ["docs/**/*.md"]
key_docs = ["README.md"]
command_docs = ["docs/**/*.md"]
import_roots = ["src"]
path_prefixes = ["src", "tests"]
changed_exclude = ["__pycache__/"]
entrypoint_prefixes = ["scripts/"]

[memory]
skip_dirs = ["generated"]

[adapters]
modules = ["repo_cfg_adapter_pkg:register"]

[reads]
structured_outline_lines = 1100
structured_outline_bytes = 2200
outline_lines = 330
outline_bytes = 4400
repeat = "off"

[reads.repeat_by_harness]
opencode = "stub"

[testing]
captured = "affected"
cache = true

[lint]
enabled = false
timeout_s = 5
python = "off"
rust = "auto"

[jobs]
enabled = true

[iac]
roots = ["infra"]
skip_globs = ["generated/**"]

[review]
layers = ["src/core", "src/api"]
""")
    cfg = load_project_config(tmp_path)
    assert cfg.source == config.resolve()
    assert cfg.sources == (config.resolve(),)
    assert cfg.unmapped == ()
    assert cfg.source_dirs == ("src", "lib")
    assert cfg.map_dirs == ("src",)
    assert cfg.test_dirs == ("tests", "it")
    assert cfg.skip_dirs == GENERIC_SKIP_DIRS | {"vendor"}
    assert cfg.memory_skip_dirs == cfg.skip_dirs | {"generated"}
    assert cfg.docs == ("README.md",)
    assert cfg.docs_all == ("docs/**/*.md",)
    assert cfg.key_docs == ("README.md",)
    assert cfg.command_docs == ("docs/**/*.md",)
    assert cfg.import_roots == ("src",)
    assert cfg.path_prefixes == ("src", "tests")
    assert cfg.changed_exclude == ("__pycache__/",)
    assert cfg.entrypoint_prefixes == ("scripts/",)
    assert cfg.adapters == ("repo_cfg_adapter_pkg:register",)
    assert cfg.adapter_base == tmp_path.resolve()
    assert cfg.structured_outline_lines == 1100
    assert cfg.structured_outline_bytes == 2200
    assert cfg.outline_lines == 330
    assert cfg.outline_bytes == 4400
    assert cfg.repeat == "off"
    assert cfg.repeat_by_harness == (("opencode", "stub"),)
    assert (cfg.tests_select, cfg.tests_cache) == ("affected", True)
    assert (cfg.lint_enabled, cfg.lint_timeout_s) == (False, 5.0)
    assert (cfg.lint_python, cfg.lint_rust) == ("off", None)
    assert cfg.jobs_enabled is True
    assert cfg.iac_roots == ("infra",)
    assert cfg.iac_skip_globs == (*DEFAULT_IAC_SKIP_GLOBS, "generated/**")
    assert cfg.review_layers == ("src/core", "src/api")


def test_legacy_only_config_is_unchanged(tmp_path: Path) -> None:
    legacy = _write(tmp_path / ".ozagia.toml", """
[index]
source_dirs = ["src"]
test_dirs = ["spec"]
""")
    cfg = load_project_config(tmp_path)
    assert cfg.source == legacy.resolve()
    assert cfg.sources == (legacy.resolve(),)
    assert cfg.source_dirs == ("src",)
    assert cfg.test_dirs == ("spec",)
    assert cfg.unmapped == ()
    assert config_path(tmp_path) == legacy


def test_legacy_wins_per_key_and_versioned_config_fills_the_gaps(
        tmp_path: Path) -> None:
    legacy = _write(tmp_path / ".ozagia.toml", """
[index]
source_dirs = ["legacy-src"]
docs = ["LEGACY.md"]

[tests]
select = "all"

[environments.dev]
paths = ["envs/dev"]
""")
    repo = _write_repo_config(tmp_path, """
[index]
source_dirs = ["new-src"]
test_dirs = ["new-tests"]
docs = ["NEW.md"]

[testing]
captured = "affected"
cache = true

[lint]
python = "ruff"
""")
    cfg = load_project_config(tmp_path)
    assert cfg.source == legacy.resolve()
    assert cfg.sources == (legacy.resolve(), repo.resolve())
    assert cfg.source_dirs == ("legacy-src",)
    assert cfg.docs == ("LEGACY.md",)
    assert cfg.test_dirs == ("new-tests",)
    assert cfg.tests_select == "all" and cfg.tests_cache is True
    assert cfg.lint_python == "ruff"
    assert [(item.name, item.paths) for item in cfg.environments] == [
        ("dev", ("envs/dev",))]
    assert cfg.unmapped == ()


def test_versioned_config_with_empty_derived_values_keeps_the_defaults(
        tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    _write_repo_config(tmp_path, """
[index]
map_dirs = []
import_roots = []
path_prefixes = []
command_docs = []
""")
    cfg = load_project_config(tmp_path)
    assert cfg.map_dirs == (".",)
    assert cfg.import_roots == ("src",)
    assert cfg.path_prefixes == ("tests", "docs")
    assert cfg.command_docs == ("README.md",)


def test_migrated_config_is_loaded_by_the_project_loader(
        tmp_path: Path) -> None:
    from ozagia.core import migrate as migrate_module

    legacy = _write(tmp_path / ".ozagia.toml", """
[index]
source_dirs = ["src"]
test_dirs = ["tests"]
map_dirs = ["src", "tests"]
key_docs = ["README.md", "HACKING.md"]
path_prefixes = ["src", "tests"]

[memory]
skip_dirs = ["vendor"]

[reads]
repeat = "off"

[tests]
select = "affected"
cache = true

[jobs]
enabled = true

[lint]
enabled = false
timeout_s = 5
python = "off"

[security]
block_secrets = false

[review]
layers = ["src/core", "src/api"]

[iac]
roots = ["infra"]
skip_globs = ["*.backup"]
""")
    preview = migrate_module.migrate(tmp_path)
    applied = migrate_module.migrate(
        tmp_path, dry_run=False, confirm=preview["token"])
    assert applied["applied"] is True
    target = repo_config_path(tmp_path)
    assert target.is_file()
    clear_config_cache()

    cfg = load_project_config(tmp_path)
    assert cfg.source == legacy.resolve()
    assert cfg.sources == (legacy.resolve(), target.resolve())
    assert cfg.source_dirs == ("src",)
    assert cfg.key_docs == ("README.md", "HACKING.md")
    assert "vendor" in cfg.memory_skip_dirs
    assert cfg.repeat == "off"
    assert (cfg.tests_select, cfg.tests_cache) == ("affected", True)
    assert cfg.jobs_enabled is True
    assert (cfg.lint_enabled, cfg.lint_timeout_s, cfg.lint_python) == (
        False, 5.0, "off")
    assert cfg.review_layers == ("src/core", "src/api")
    assert cfg.iac_roots == ("infra",)
    assert cfg.unmapped == ((
        "security", "owned by ozagia.core.security (still reads .ozagia.toml)"),)

    # The migration output is also the whole config when the legacy file goes.
    legacy.unlink()
    clear_config_cache()
    migrated = load_project_config(tmp_path)
    assert migrated.source == target.resolve()
    assert migrated.sources == (target.resolve(),)
    assert migrated.source_dirs == ("src",)
    assert (migrated.tests_select, migrated.tests_cache) == ("affected", True)


@pytest.mark.parametrize("captured, select", [
    ("all", "all"),
    ("affected", "affected"),
    ("smoke", "affected"),
    ("none", "all"),
    ("off", "all"),
    ("package", "all"),
])
def test_testing_captured_maps_to_the_executor_selectors(
        tmp_path: Path, captured: str, select: str) -> None:
    _write_repo_config(tmp_path, f'[testing]\ncaptured = "{captured}"\n')
    assert load_project_config(tmp_path).tests_select == select


@pytest.mark.parametrize("text, message", [
    ('[index]\nsource_dirs = ["../outside"]\n', "repository root"),
    ('[index]\nsource_dirs = "src"\n', "must be a list"),
    ('[index]\nsource_dirs = []\n', "at least one"),
    ('[index]\nsource_dirs = ["src/*"]\n', "not glob patterns"),
    ('[lint]\ntimeout_s = 0\n', "timeout_s"),
    ('[reads.repeat_by_harness]\n"bad harness" = "stub"\n',
     "bounded harness name"),
    ('[reads.repeat_by_harness]\nopencode = "loud"\n', "must be one of"),
    ('[testing]\ncaptured = "sometimes"\n', "captured must be one of"),
    ('[index\n', "Expected"),
])
def test_invalid_versioned_config_is_an_error_naming_the_file(
        tmp_path: Path, text: str, message: str) -> None:
    path = _write_repo_config(tmp_path, text)
    with pytest.raises(ConfigError) as excinfo:
        load_project_config(tmp_path)
    assert str(path.resolve()) in str(excinfo.value)
    assert message in str(excinfo.value)


def test_versioned_config_reports_unmapped_values(tmp_path: Path) -> None:
    _write_repo_config(tmp_path, """
[security]
enabled = false

[hints]
library = "strict"

[models]
default = "local/qwen"

[ui]
theme = "dark"

[testing]
mode = "smoke"

[review]
min_reviewers = 2

[index]
color = "blue"

[iac]
remote_states = [{ consumer = "envs/app", name = "iam", producer = "envs/iam" }]

[environments.dev]
paths = ["envs/dev"]
""")
    cfg = load_project_config(tmp_path)
    keys = {key for key, _ in cfg.unmapped}
    assert {
        "security", "hints", "models", "ui", "testing.mode",
        "review.min_reviewers", "index.color", "iac.remote_states",
        "environments",
    } <= keys
    # Unmapped values are reported, never applied to ProjectConfig.
    assert cfg.jobs_enabled is False
    assert cfg.review_layers == ()


def test_legacy_owned_sections_are_reported_not_rejected(
        tmp_path: Path) -> None:
    _write(tmp_path / ".ozagia.toml", """
[index]
source_dirs = ["src"]

[security]
block_secrets = false
""")
    cfg = load_project_config(tmp_path)
    assert cfg.source_dirs == ("src",)
    assert cfg.unmapped == ((
        "security", "owned by ozagia.core.security (still reads .ozagia.toml)"),)


def test_cache_invalidation_includes_content_digests_for_both_files(
        tmp_path: Path) -> None:
    legacy = _write(tmp_path / ".ozagia.toml",
                    '[index]\nsource_dirs = ["aaa"]\n')
    repo = _write_repo_config(tmp_path, '[index]\ntest_dirs = ["ttt"]\n')
    cfg = load_project_config(tmp_path)
    assert cfg.source_dirs == ("aaa",) and cfg.test_dirs == ("ttt",)

    # Same path, mtime and size: only the content digest can notice the edit.
    st = legacy.stat()
    legacy.write_text('[index]\nsource_dirs = ["bbb"]\n', encoding="utf-8")
    os.utime(legacy, ns=(st.st_atime_ns, st.st_mtime_ns))
    cfg = load_project_config(tmp_path)
    assert cfg.source_dirs == ("bbb",)

    st = repo.stat()
    repo.write_text('[index]\ntest_dirs = ["sss"]\n', encoding="utf-8")
    os.utime(repo, ns=(st.st_atime_ns, st.st_mtime_ns))
    cfg = load_project_config(tmp_path)
    assert cfg.test_dirs == ("sss",)


def test_adapters_from_the_versioned_config_import_from_the_root(
        tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / "repo_cfg_adapter_pkg" / "__init__.py", "")
    _write(tmp_path / "repo_cfg_adapter_pkg" / "adapter.py", """
def register(registry):
    registry.add_graph_query("where", lambda memory: "versioned config")
""")
    _write_repo_config(tmp_path, """
[adapters]
modules = ["repo_cfg_adapter_pkg.adapter:register"]
""")
    before = list(sys.path)
    reg = adapters.registry(tmp_path, reload=True)
    assert reg.errors == []
    assert reg.loaded == ["repo_cfg_adapter_pkg.adapter:register"]
    assert reg.graph_queries["where"](None) == "versioned config"
    assert sys.path == before


@pytest.mark.parametrize("text, message", [
    ('[index]\nsource_dir = ["a"]\n', "unknown key"),
    ('[indexes]\n', "unknown section"),
    ('[index]\nsource_dirs = "src"\n', "list of strings"),
    ('[index]\nsource_dirs = ["../outside"]\n', "inside the root"),
    ('[index]\nskip_dirs = ["a/b"]\n', "directory names"),
    ('[index]\npath_prefixes = ["src/pkg"]\n', "single path"),
    ('[index\n', "Expected"),
])
def test_malformed_config_is_an_error(tmp_path: Path, text: str,
                                      message: str) -> None:
    _write(tmp_path / ".ozagia.toml", text)
    with pytest.raises(ConfigError, match=message):
        load_project_config(tmp_path)


def test_adapters_import_from_the_config_files_directory(
        tmp_path: Path, monkeypatch) -> None:
    """An eval workdir uses the main checkout's adapter via $OZAGIA_CONFIG."""
    main = tmp_path / "main"
    _write(main / "cfg_dir_adapter_pkg" / "__init__.py", "")
    _write(main / "cfg_dir_adapter_pkg" / "adapter.py", """
def register(registry):
    registry.add_graph_query("where", lambda memory: "main checkout")
""")
    cfg = _write(main / ".ozagia.toml", """
[adapters]
modules = ["cfg_dir_adapter_pkg.adapter:register"]
""")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.setenv("OZAGIA_CONFIG", str(cfg))
    before = list(sys.path)
    reg = adapters.registry(workdir, reload=True)
    assert reg.errors == []
    assert reg.loaded == ["cfg_dir_adapter_pkg.adapter:register"]
    assert reg.graph_queries["where"](None) == "main checkout"
    assert sys.path == before

    # Without $OZAGIA_CONFIG the workdir has no config and no adapter.
    monkeypatch.delenv("OZAGIA_CONFIG")
    assert adapters.registry(workdir).loaded == []


def test_bad_adapter_is_reported_not_raised(tmp_path: Path) -> None:
    _write(tmp_path / ".ozagia.toml",
           '[adapters]\nmodules = ["no_such_module_xyz:register", "nocolon"]\n')
    reg = adapters.registry(tmp_path, reload=True)
    assert reg.loaded == []
    assert len(reg.errors) == 2
    assert "ModuleNotFoundError" in reg.errors[0]
    assert "module:attr" in reg.errors[1]


def _dot_fixture(root: Path) -> None:
    _write(root / "a.py", "def alpha():\n    return 1\n")
    _write(root / "sub" / "b.py", "from a import alpha\n\n\ndef beta():\n"
                                  "    return alpha()\n")
    _write(root / "tests" / "test_a.py", "from a import alpha\n\n\n"
                                         "def test_alpha():\n    assert alpha()\n")
    _write(root / "node_modules" / "n.py", "def skipped():\n    pass\n")
    _write(root / ".ozagia" / "inside_default_state.py", "def no():\n    pass\n")
    _write(root / "work" / "state" / "inside_custom_state.py",
           "def no():\n    pass\n")


def test_dot_source_dir_builds_clean_paths(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    _dot_fixture(root)
    monkeypatch.setenv("OZAGIA_STATE_DIR", "work/state")
    monkeypatch.setattr(memory_module, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(memory_module, "TEST_DIRS", ("tests",))
    out = tmp_path / "graph.json"
    build_index(root, out, include_docs="none", include_git=False)
    mem = DevMemory(out, tmp_path / "journal.jsonl", root=root)
    paths = {n["attrs"]["path"] for n in mem.nodes.values() if n["type"] == "File"}
    assert paths == {"a.py", "sub/b.py", "tests/test_a.py"}
    assert not any(p.startswith("./") for p in paths)
    # The test file is a test module: its test function is a Test, not a Symbol.
    tests = [n for n in mem.nodes.values() if n["type"] == "Test"]
    assert [t["attrs"]["name"] for t in tests] == ["test_alpha"]
    symbols = {n["attrs"]["name"] for n in mem.nodes.values()
               if n["type"] == "Symbol"}
    assert symbols == {"alpha", "beta"}
    assert mem.file("sub/b.py")["imports"][0]["label"] == "a.py"


def test_default_state_dir_is_never_indexed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    _dot_fixture(root)
    # Remove the generic name from the skip set: the state dir is still left out.
    monkeypatch.setattr(memory_module, "SKIP_PARTS",
                        set(memory_module.SKIP_PARTS) - {".ozagia"})
    monkeypatch.setattr(repo_map_module, "SKIP_PARTS",
                        set(repo_map_module.SKIP_PARTS) - {".ozagia"})
    monkeypatch.setattr(memory_module, "SOURCE_DIRS", (".",))
    monkeypatch.setattr(repo_map_module, "SOURCE_DIRS", (".",))
    out = state_path(root, "graph.json")
    build_index(root, out, include_docs="none", include_git=False)
    mem = DevMemory(out, state_path(root, "journal.jsonl"), root=root)
    paths = {n["attrs"]["path"] for n in mem.nodes.values() if n["type"] == "File"}
    assert not any(p.startswith(".ozagia/") for p in paths)
    assert "work/state/inside_custom_state.py" in paths

    files = repo_map_module._source_files(root)
    assert "a.py" in files and "sub/b.py" in files and "tests/test_a.py" in files
    assert not any(f.startswith(("./", ".ozagia/", "node_modules/")) for f in files)


def test_repo_map_respects_custom_state_dir(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    _dot_fixture(root)
    monkeypatch.setenv("OZAGIA_STATE_DIR", "work/state")
    monkeypatch.setattr(repo_map_module, "SOURCE_DIRS", (".",))
    files = repo_map_module._source_files(root)
    assert "work/state/inside_custom_state.py" not in files
    assert "a.py" in files


def test_src_layout_is_a_default_import_root(tmp_path):
    from ozagia.config import clear_config_cache, load_project_config
    clear_config_cache()
    assert load_project_config(tmp_path).import_roots == ()
    (tmp_path / "src").mkdir()
    clear_config_cache()
    assert load_project_config(tmp_path).import_roots == ("src",)


def test_iac_environment_service_roots_and_remote_state_config_are_bounded(
        tmp_path):
    _write(tmp_path / ".ozagia.toml", """
[iac]
roots = ["envs/prod", "envs/iam"]
skip_globs = ["generated/**"]
[[iac.remote_states]]
consumer = "envs/prod"
name = "iam"
producer = "envs/iam"

[environments.pprd]
paths = ["envs/pprd"]
namespaces = ["pprd"]
account_alias = "production-preview"
region = "us-east-1"
cluster = "pprd-cluster"
risk = "high"
pipelines = ["deploy-pprd"]

[services.api]
chart = "charts/api"
values_globs = ["charts/api/values.*.yaml"]
buildspec = "buildspec.yml"
pipeline = "pipelines/api.yml"
namespace = "pprd"
environment = "pprd"
paths = ["services/api"]
""")
    config = load_project_config(tmp_path)
    assert config.iac_roots == ("envs/prod", "envs/iam")
    assert config.iac_skip_globs == (*DEFAULT_IAC_SKIP_GLOBS, "generated/**")
    assert [(item.consumer, item.name, item.producer)
            for item in config.remote_states] == [
                ("envs/prod", "iam", "envs/iam")]
    assert config.environments[0].region == "us-east-1"
    assert config.environments[0].paths == ("envs/pprd",)
    service = config.services[0]
    assert service.name == "api" and service.environment == "pprd"
    assert service.values_globs == ("charts/api/values.*.yaml",)


@pytest.mark.parametrize("text, message", [
    ("[environments.dev]\ncredentials = 'nope'\n", "unknown key"),
    ("[services.api]\nsecret = 'nope'\n", "unknown key"),
    ("[environments.dev]\npaths = ['../outside']\n",
     "inside the repository root"),
    ("[services.api]\nchart = '/outside/chart'\n", "inside the repository root"),
    ("[services.api]\nenvironment = 'missing'\n", "not configured"),
    ("[iac]\nroots = ['../../outside']\n",
     "inside the repository root"),
    ("[iac]\n[[iac.remote_states]]\nconsumer='a'\nname='x'\n"
     "producer='b'\naccess_key='canary'\n", "unknown key"),
])
def test_iac_config_rejects_secrets_unknown_fields_and_escaping_paths(
        tmp_path, text, message):
    _write(tmp_path / ".ozagia.toml", text)
    with pytest.raises(ConfigError, match=message):
        load_project_config(tmp_path)


def test_runtime_config_is_declarative_bounded_and_immutable(tmp_path):
    _write(tmp_path / ".ozagia.toml", """
[relays.prod]
specs = [
  { kind = "local", name = "base" },
  { kind = "ssh", options = { host = "internal.example", port = 22 } },
]
allowed_kinds = ["local", "ssh"]
max_timeout_s = 300
command_allowlist = [["uptime"], ["kubectl", "get"]]
max_output_bytes = 2048

[runtime.services.api]
command = ["python", "-m", "app"]
workdir = "services/api"
relay = "prod"
mode = "background"
description = "API service"

[runtime.tunnels.database]
relay = "prod"
target = "db.internal.example:5432"
local_port = 0
description = "Payments database"
""")
    config = load_project_config(tmp_path)
    relay = config.relays[0]
    assert relay.name == "prod"
    assert relay.allowed_kinds == ("local", "ssh")
    assert relay.command_allowlist == (("uptime",), ("kubectl", "get"))
    assert relay.specs[1].options == (("host", "internal.example"),
                                      ("port", 22))
    with pytest.raises((AttributeError, TypeError)):
        relay.specs[0].kind = "ssm"
    service = config.runtime_services[0]
    assert service.command == ("python", "-m", "app")
    assert service.workdir == "services/api"
    assert service.relay == "prod" and service.mode == "background"
    tunnel = config.runtime_tunnels[0]
    assert tunnel.target == "db.internal.example:5432"
    assert tunnel.local_port == 0


@pytest.mark.parametrize("text, message", [
    ('[relays.prod]\nspecs = []\n', "1 to 16"),
    ('[relays.prod]\nspecs = [{ kind="local", unknown="x" }]\n',
     "unknown key"),
    ('[relays.prod]\nspecs = [{ kind="local", options={password="bad"} }]\n',
     "unsafe relay option"),
    ('[relays.prod]\nspecs = [{ kind="local" }]\nallowed_kinds=["ssh"]\n',
     "configured spec kinds"),
    ('[relays.prod]\nspecs = [{ kind="local", name="same" }, '
     '{ kind="ssh", name="same" }]\n', "duplicate relay spec name"),
    ('[runtime.services.api]\ncommand=[]\n', "non-empty argv"),
    ('[runtime.services.api]\ncommand=["python -m app"]\n',
     "shell command string"),
    ('[runtime.services.api]\ncommand=["sh", "-c", "echo hi"]\n',
     "shell command string"),
    ('[runtime.services.api]\ncommand=["python", "x; echo bad"]\n',
     "unsafe argv"),
    ('[runtime.services.api]\ncommand=["python", "--api-key", "secret"]\n',
     "credential-like"),
    ('[runtime.services.api]\ncommand=["python"]\nworkdir="../outside"\n',
     "inside the repository root"),
    ('[runtime.services.api]\ncommand=["python"]\nrelay="missing"\n',
     "not configured"),
    ('[relays.prod]\nspecs=[{kind="local"}]\n'
     '[runtime.services.api]\ncommand=["python"]\nmode="shell"\n',
     "background or foreground"),
    ('[relays.prod]\nspecs=[{kind="local"}]\n'
     '[runtime.tunnels.db]\nrelay="prod"\ntarget="db.example:0"\n',
     "outside 1..65535"),
    ('[relays.prod]\nspecs=[{kind="local"}]\n'
     '[runtime.tunnels.db]\nrelay="prod"\ntarget="db.example:22"\nlocal_port=65536\n',
     "outside 0..65535"),
    ('[relays.prod]\nspecs=[{kind="local"}]\n'
     '[runtime.services.api]\ncommand=["python"]\ncredentials="bad"\n',
     "unknown key"),
])
def test_runtime_config_rejects_unsafe_or_unknown_values(tmp_path, text, message):
    _write(tmp_path / ".ozagia.toml", text)
    with pytest.raises(ConfigError, match=message):
        load_project_config(tmp_path)


def test_runtime_config_rejects_duplicate_command_prefixes(tmp_path):
    _write(tmp_path / ".ozagia.toml", """
[relays.local]
specs = [{ kind = "local" }]
command_allowlist = [["python"], ["python"]]
""")
    with pytest.raises(ConfigError, match="contains duplicates"):
        load_project_config(tmp_path)
