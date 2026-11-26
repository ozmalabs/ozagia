"""A synthetic Ocelot workspace for the ``ozagia.integrations.ocelot.workspace``
adapter tests.

The fixture is intentionally small but keeps the shapes that matter to the
adapter: top-level seed manifests with dependencies, boot and registry
references, migrated tests that name a seed, a shared seed referenced by boot
and tests, a local seed with its own test, a seed with no test mention, an
engine package with a build record, and the ``data/`` and ``results/``
directories the workspace doctor expects.

Only the standard library and ozagia itself are imported.  The target tree is
written to disk and never imported or executed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ozagia.core.memory import DevMemory, build_index

# Seed IDs in the fixture.  ``ignored`` lives under seeds/generated/ and must
# not become a node.
SEED_IDS = ("core", "shared", "local", "no_test")
GENERATED_SEED_ID = "ignored"
SEED_VERSIONS = {
    "core": "1.0.0",
    "shared": "2.0.0",
    "local": "3.1.0",
    "no_test": "1.0.0",
    "ignored": "9.9.9",
}
BOOT_CORE = "substrate/boot_core.py"
DOC_PATH = "docs/seed_notes.md"
RUST_SOURCE = "runners/dsl/src/lib.rs"
ENGINE_PACKAGE = "runners/dsl/python/substrate_rs"
BUILD_RECORD = f"{ENGINE_PACKAGE}/_native.build.json"
NATIVE_ARTIFACT = f"{ENGINE_PACKAGE}/_native.abi3.so"
OUT_REL = ".ozagia/graph.json"
JOURNAL_REL = ".ozagia/journal.jsonl"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, value: Any) -> None:
    _write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fallback_source_fingerprint(root: Path) -> str:
    """Ocelot's ``domains.engine_provenance.rust_source_fingerprint`` shape.

    The real reporter hashes ``runners/dsl/src/**/*.rs`` plus
    ``runners/dsl/Cargo.toml`` by repository-relative POSIX path and content.
    Reproducing it here keeps the fixture independent of an Ocelot checkout.
    """
    digest = hashlib.sha256()
    src = root / "runners" / "dsl" / "src"
    files = sorted(p for p in src.rglob("*.rs") if p.is_file())
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(_sha256(path).encode())
    cargo = root / "runners" / "dsl" / "Cargo.toml"
    if cargo.is_file():
        digest.update(b"Cargo.toml")
        digest.update(_sha256(cargo).encode())
    return digest.hexdigest()


def source_fingerprint(root: Path) -> str:
    """The engine source fingerprint, using the adapter's reporter if present."""
    try:
        from ozagia.integrations.ocelot.workspace_doctor import (
            rust_source_fingerprint,
        )
    except Exception:  # noqa: BLE001 - the doctor is optional for this fixture
        rust_source_fingerprint = None
    if rust_source_fingerprint is not None:
        try:
            value = rust_source_fingerprint(str(root))
        except Exception:  # noqa: BLE001 - fall back to the local reproduction
            value = None
        if isinstance(value, str) and value:
            return value
    return _fallback_source_fingerprint(root)


def _rust_source_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    src = root / "runners" / "dsl" / "src"
    for path in sorted(p for p in src.rglob("*.rs") if p.is_file()):
        out[path.relative_to(root).as_posix()] = _sha256(path)
    cargo = root / "runners" / "dsl" / "Cargo.toml"
    if cargo.is_file():
        out["runners/dsl/Cargo.toml"] = _sha256(cargo)
    return out


def _seed_manifest(seed_id: str, *, depends_on: list[str] | None = None,
                   rules: list[dict[str, Any]] | None = None,
                   nodes: list[dict[str, Any]] | None = None,
                   concepts: list[dict[str, Any]] | None = None,
                   productions: list[Any] | None = None) -> dict[str, Any]:
    return {
        "id": seed_id,
        "version": SEED_VERSIONS[seed_id],
        "depends_on": list(depends_on or []),
        "description": f"Synthetic {seed_id} seed for the Ocelot workspace fixture.",
        "rules": list(rules or []),
        "nodes": list(nodes or []),
        "concepts": list(concepts or []),
        "productions": list(productions or []),
    }


def write_workspace(root: Path) -> Path:
    """Write the fixture tree and return *root*."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    _write(root / ".ozagia.toml", """\
[index]
source_dirs = ["domains", "substrate", "runners"]
test_dirs = ["tests"]
docs = ["docs/**/*.md"]
path_prefixes = ["domains", "substrate", "runners", "seeds", "tests", "docs"]

[tests]
select = "affected"

[adapters]
modules = ["ozagia.integrations.ocelot.workspace:register"]
""")

    # -- seed manifests ---------------------------------------------------- #
    _write_json(root / "seeds" / "core.json", _seed_manifest(
        "core",
        nodes=[{"type": "Agent", "attrs": {"name": "core_agent"}}],
        rules=[{"name": "core_identity", "match": [], "effects": []}],
    ))
    _write_json(root / "seeds" / "shared.json", _seed_manifest(
        "shared",
        depends_on=["core"],
        concepts=[{"name": "SharedConcept"}],
        rules=[{"name": "shared_rule", "match": [], "effects": []}],
        productions=[["agent", "has", "shared_concept"]],
    ))
    _write_json(root / "seeds" / "local.json", _seed_manifest(
        "local",
        depends_on=["shared"],
        nodes=[{"type": "Concept", "attrs": {"name": "LocalConcept"}}],
        rules=[{"name": "local_rule", "match": [], "effects": []}],
    ))
    _write_json(root / "seeds" / "no_test.json", _seed_manifest(
        "no_test",
        rules=[{"name": "no_test_rule", "match": [], "effects": []}],
    ))
    _write_json(root / "seeds" / "generated" / "ignored.json", _seed_manifest(
        "ignored", depends_on=["local"],
    ))

    # -- boot and registry wiring ----------------------------------------- #
    _write(root / "substrate" / "__init__.py", '"""Synthetic substrate."""\n')
    _write(root / "substrate" / "seed_loader.py", """\
\"\"\"Minimal seed lookup used by the fixture boot modules and tests.\"\"\"
from __future__ import annotations

import json
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parents[1] / "seeds"


def manifest_for(seed_id: str) -> dict:
    return json.loads((SEED_DIR / f"{seed_id}.json").read_text(encoding="utf-8"))
""")
    _write(root / "substrate" / "boot_core.py", """\
\"\"\"The core boot list used by the fixture.

The shared seed is deliberately booted with core: a change there has a wider
blast radius than a seed used by one migrated test.
\"\"\"
from substrate.seed_loader import manifest_for

CORE_SEEDS = ("core", "shared")


def boot_core():
    return [manifest_for(seed_id) for seed_id in CORE_SEEDS]
""")
    _write(root / "substrate" / "boot_all.py", """\
\"\"\"The full fixture boot list.\"\"\"
from substrate.boot_core import CORE_SEEDS

ALL_SEEDS = (*CORE_SEEDS, "no_test")


def boot_all():
    return list(ALL_SEEDS)
""")
    _write(root / "domains" / "__init__.py", '"""Synthetic Ocelot domains."""\n')
    _write(root / "domains" / "seed_registry.py", """\
\"\"\"The fixture's seed registry.\"\"\"
REGISTRY = {
    "core": "1.0.0",
    "shared": "2.0.0",
    "no_test": "1.0.0",
}
""")
    _write(root / "domains" / "world.py", """\
\"\"\"A world adapter that uses the shared seed and the native engine.

The shared manifest is ``seeds/shared.json``.  ``substrate_rs`` supplies the
``Substrate`` object used by the world adapter.
\"\"\"
from substrate_rs import Substrate


class World:
    \"\"\"A tiny world adapter used by ``tests/test_world.py``.\"\"\"

    def substrate(self) -> Substrate:
        return Substrate()
""")

    # -- tests ------------------------------------------------------------- #
    _write(root / "tests" / "test_local_migrated.py", """\
\"\"\"Migrated test for the local seed.\"\"\"
from substrate.seed_loader import manifest_for


def test_local_manifest():
    manifest = manifest_for("local")
    assert manifest["id"] == "local"
    assert "seeds/local.json"


def test_local_depends_on_shared():
    assert "shared" in manifest_for("local")["depends_on"]
""")
    _write(root / "tests" / "test_shared_migrated.py", """\
\"\"\"Migrated test for the shared seed.  It exercises substrate_rs.\"\"\"
import substrate_rs
from substrate.seed_loader import manifest_for


def test_shared_manifest_is_engine_backed():
    manifest = manifest_for("shared")
    assert manifest["id"] == "shared"
    assert "seeds/shared.json"
    assert substrate_rs is not None


def test_shared_engine_provenance_shape():
    assert hasattr(substrate_rs, "__name__")
""")
    _write(root / "tests" / "test_engine_provenance_migrated.py", """\
\"\"\"Engine provenance gate for the substrate_rs native extension.\"\"\"
import substrate_rs
from domains.engine_provenance import build_is_stale, rust_source_fingerprint


def test_engine_build_record_matches_source():
    # The fixture's Rust lives in runners/dsl/src/lib.rs.
    assert substrate_rs is not None
    assert rust_source_fingerprint(".")
    assert build_is_stale(".") == []
""")
    _write(root / "tests" / "test_world.py", """\
\"\"\"World-adapter test; imports domains.world.\"\"\"
from domains.world import World


def test_world_constructs():
    assert World is not None
""")

    # -- Rust engine and build record -------------------------------------- #
    _write(root / "runners" / "dsl" / "Cargo.toml", """\
[package]
name = "substrate_rs"
version = "0.1.0"
edition = "2021"

[lib]
name = "substrate_rs"
crate-type = ["cdylib"]
""")
    _write(root / "runners" / "dsl" / "src" / "lib.rs", """\
//! Synthetic substrate engine source for the Ocelot workspace fixture.

pub mod py;

#[pyfunction]
pub fn substrate_version() -> &'static str {
    "fixture"
}
""")
    _write(root / "runners" / "dsl" / "src" / "py.rs", """\
//! PyO3 hand-off for the synthetic engine.

#[pymodule]
fn _native(_py: Python<'_>, m: &PyModule) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(substrate_version, m)?)?;
    Ok(())
}
""")
    _write(root / "runners" / "dsl" / "python" / "substrate_rs" / "__init__.py", """\
\"\"\"Python package for the synthetic substrate_rs engine.\"\"\"
from ._native import *  # noqa: F401,F403
""")
    _write(root / NATIVE_ARTIFACT, "synthetic native extension fixture\n")
    artifacts = _rust_source_hashes(root)
    _write_json(root / BUILD_RECORD, {
        "_README": (
            "The Rust source this fixture's engine artifact was built from, "
            "by content. Mirrors Ocelot's record_build() shape."
        ),
        "engine_sha256": _sha256(root / NATIVE_ARTIFACT),
        "source_fingerprint": source_fingerprint(root),
        "files": artifacts,
    })

    # -- data, results and docs ------------------------------------------- #
    (root / "data").mkdir(parents=True, exist_ok=True)
    _write(root / "data" / ".gitkeep", "")
    _write_json(root / "results" / "failure_baseline.json", {
        "entries": [{
            "node": "tests/test_local_migrated.py::test_local_manifest",
            "file": "tests/test_local_migrated.py",
            "kind": "FAILED",
            "message": "fixture baseline",
            "class": "fixture",
        }],
    })
    _write(root / DOC_PATH, """\
# Seed notes

The shared seed is defined in `seeds/shared.json`; its migrated test is
`tests/test_shared_migrated.py`.  Changing shared seed data needs the wider
gate, while `seeds/local.json` is covered by `tests/test_local_migrated.py`.
""")
    _write(root / "README.md", """\
# Synthetic Ocelot workspace

A fixture for `ozagia.integrations.ocelot.workspace`: seeds, boot lists,
migrated tests, a small Rust engine and its build record.
""")
    return root


def build_workspace(root: Path) -> tuple[Path, DevMemory]:
    """Write the fixture, build its graph and return ``(root, DevMemory)``."""
    root = write_workspace(Path(root))
    out = root / OUT_REL
    journal = root / JOURNAL_REL
    build_index(root, out, include_docs="features", include_git=False)
    mem = DevMemory(out, journal, root=root)
    return root, mem
