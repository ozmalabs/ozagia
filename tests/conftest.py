"""Shared test helpers."""
from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_host_cgroups(monkeypatch):
    """Tests never create or write the host's real cgroup hierarchy."""
    monkeypatch.setenv("OZAGIA_CGROUPS", "0")

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
