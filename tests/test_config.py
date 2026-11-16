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
    load_project_config,
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
