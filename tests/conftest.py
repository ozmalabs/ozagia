"""Shared test helpers."""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = (REPO_ROOT / "src").resolve()


def _absolute_pythonpath() -> None:
    """Make a relative ``PYTHONPATH=src`` safe for child processes.

    Daemons, hooks and bounded verifiers change directory. A relative entry
    then names a different (or missing) directory. Normalise every entry and
    keep this checkout's ``src`` importable for the whole test run.
    """
    entries: list[str] = []
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if not entry:
            continue
        path = Path(entry)
        if not path.is_absolute():
            path = Path.cwd() / path
        entries.append(str(path))
    if str(SRC_ROOT) not in entries:
        entries.insert(0, str(SRC_ROOT))
    os.environ["PYTHONPATH"] = os.pathsep.join(entries)


_absolute_pythonpath()


# Test sharding. `OZAGIA_TEST_SHARD=i/n` runs only whole test files assigned to
# shard `i` (1-based). Assignment is deterministic for the checkout: files are
# placed largest first on the least-loaded shard, so a module's fixtures stay
# together and shards are roughly even by file size. Unset runs everything.
_SHARD_ASSIGNMENTS: dict[int, dict[str, int]] = {}


def _test_shard() -> tuple[int, int] | None:
    raw = os.environ.get("OZAGIA_TEST_SHARD", "").strip()
    if not raw:
        return None
    try:
        index_text, total_text = raw.split("/", 1)
        index, total = int(index_text), int(total_text)
    except ValueError:
        raise pytest.UsageError(
            "OZAGIA_TEST_SHARD must be INDEX/TOTAL, e.g. 1/4") from None
    if total < 1 or index < 1 or index > total:
        raise pytest.UsageError(
            f"OZAGIA_TEST_SHARD {raw!r} needs 1 <= INDEX <= TOTAL")
    return index - 1, total


def _shard_assignment(total: int) -> dict[str, int]:
    assignment = _SHARD_ASSIGNMENTS.get(total)
    if assignment is not None:
        return assignment
    tests = REPO_ROOT / "tests"
    files = sorted(
        ((path.relative_to(tests).as_posix(), path.stat().st_size)
         for path in tests.rglob("test_*.py")),
        key=lambda item: (-item[1], item[0]))
    loads = [0] * total
    assignment = {}
    for name, size in files:
        shard = min(range(total), key=lambda item: (loads[item], item))
        assignment[name] = shard
        loads[shard] += size
    _SHARD_ASSIGNMENTS[total] = assignment
    return assignment


def pytest_ignore_collect(collection_path, config):
    """Keep whole test files together and skip other shards' files."""
    spec = _test_shard()
    if spec is None:
        return None
    path = Path(str(collection_path))
    try:
        relative = path.resolve().relative_to(
            (REPO_ROOT / "tests").resolve()).as_posix()
    except ValueError:
        return None
    if not relative.endswith(".py") or not path.name.startswith("test_"):
        return None
    return _shard_assignment(spec[1]).get(relative) != spec[0]


@pytest.fixture(autouse=True)
def _no_host_cgroups(monkeypatch):
    """Tests never create or write the host's real cgroup hierarchy."""
    monkeypatch.setenv("OZAGIA_CGROUPS", "0")


@pytest.fixture(autouse=True)
def _untrusted_project_python(monkeypatch):
    """Project-shipped interpreters are not trusted unless a test says so
    (the shell running the suite may set the variable)."""
    monkeypatch.delenv("OZAGIA_TRUST_PROJECT_PYTHON", raising=False)


@pytest.fixture(autouse=True)
def _isolated_daemon_socket(monkeypatch):
    """A developer's session socket must never become a test daemon target."""
    monkeypatch.delenv("OZAGIA_DAEMON_SOCK", raising=False)

# Module names before the extraction from Ocelot, mapped to their ozagia
# modules. Historical sources still import the old names.
_OLD_MODULES = {
    "ocelot_dev_memory_mcp": "integrations.mcp.server",
    "ocelot_dev_memory": "core.memory", "ocelot_dev_session": "core.session",
    "ocelot_repo_map_eval": "eval.retrieval", "ocelot_repo_map": "core.repo_map",
    "ocelot_code_search": "core.search", "ocelot_symbol_graph": "core.symbol_graph",
    "ocelot_architecture": "core.architecture",
    "ocelot_tag_languages": "index.tag_languages", "ocelot_scip_import": "index.scip",
    "ocelot_code_edit": "edit.code_edit", "ocelot_edit_txn": "edit.txn",
    "ocelot_edit": "edit.dispatch", "ocelot_cst_edit": "edit.cst",
    "ocelot_json_edit": "edit.json_edit", "ocelot_markdown_edit": "edit.markdown",
    "ocelot_lossless": "edit.lossless", "ocelot_lsp": "lsp",
    "ocelot_agent_eval": "eval.agent", "ocelot_index_bench": "eval.bench",
}
_ALT = "|".join(sorted(_OLD_MODULES, key=len, reverse=True))


def modernize_imports(source: str) -> str:
    """Rewrite `scripts.ocelot_*` imports in a historical source to ozagia."""
    def from_import(m: re.Match[str]) -> str:
        pkg, _, mod = ("ozagia." + _OLD_MODULES[m.group(1)]).rpartition(".")
        return f"from {pkg} import {mod}"
    source = re.sub(rf"from scripts import ({_ALT})\b", from_import, source)
    return re.sub(rf"\bscripts\.({_ALT})\b",
                  lambda m: "ozagia." + _OLD_MODULES[m.group(1)], source)


def load_module_at(commit: str, rel_path: str, directory: Path, module_name: str):
    """`rel_path` as it was at `commit`, imported under `module_name`.

    Skips the test when the commit is not in this clone (a shallow CI clone).
    """
    try:
        source = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "show", f"{commit}:{rel_path}"],
            capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip(f"commit {commit} is not available")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{module_name}.py"
    path.write_text(modernize_imports(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A verbatim copy of Ocelot's `tools/ozagia_ocelot_adapter.py`: seed manifest
# indexing (Seed/Rule/... nodes) and `dev_graph_query`.
OCELOT_ADAPTER = Path(__file__).resolve().parent / "fixtures" / "ocelot_adapter.py"


def write_adapter_config(directory: Path, modules: list[str],
                         extra: str = "") -> Path:
    """A `.ozagia.toml` in `directory` that loads `modules`."""
    directory.mkdir(parents=True, exist_ok=True)
    cfg = directory / ".ozagia.toml"
    refs = ", ".join(f'"{m}"' for m in modules)
    cfg.write_text(f"{extra}\n[adapters]\nmodules = [{refs}]\n", encoding="utf-8")
    return cfg


@pytest.fixture
def seed_adapter(tmp_path_factory, monkeypatch) -> Path:
    """Point `$OZAGIA_CONFIG` at a config outside the fixture repo that loads
    the Ocelot seed adapter (imported from the config file's directory)."""
    base = tmp_path_factory.mktemp("seed_adapter")
    (base / "ozagia_test_ocelot_adapter.py").write_text(
        OCELOT_ADAPTER.read_text(encoding="utf-8"), encoding="utf-8")
    cfg = write_adapter_config(base, ["ozagia_test_ocelot_adapter:register"])
    monkeypatch.setenv("OZAGIA_CONFIG", str(cfg))
    return cfg
