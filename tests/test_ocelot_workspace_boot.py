"""Tests for explicit, preview-first Ocelot boot-list and registry editing.

Every fixture is a temporary synthetic Ocelot checkout.  The editor parses
with :mod:`ast` and writes through the edit transaction: no Ocelot import,
seed loader, model call, network access or target test execution is involved.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from ozagia.integrations.ocelot import workspace_boot as B


BOOT_CORE = "substrate/boot_core.py"
BOOT_PROFILES = "substrate/boot_profiles.py"
REGISTRY = "domains/seed_registry.py"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _manifest(seed_id: str, *, version: str = "1.0.0",
              depends_on: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": seed_id,
        "version": version,
        "depends_on": list(depends_on or []),
        "description": f"Synthetic {seed_id} seed.",
        "nodes": [],
        "edges": [],
        "productions": [],
        "rules": [],
    }


def _write_manifest(root: Path, seed_id: str, **kwargs: Any) -> Path:
    return _write(
        root / "seeds" / f"{seed_id}.json",
        json.dumps(_manifest(seed_id, **kwargs), indent=2) + "\n")


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ocelot"
    _write(root / "substrate" / "boot_core.py", """\
\"\"\"Synthetic boot core.

Keep this comment: it is unrelated formatting.
\"\"\"
from substrate.seed_loader import manifest_for

# Keep this comment too.
CORE_SEEDS = ("decision_core", "learning_core")
OPTIONAL_CORE_SEEDS = ("dual_process_core",)


def boot_core():
    return [manifest_for(seed_id) for seed_id in CORE_SEEDS]
""")
    _write(root / BOOT_PROFILES, """\
\"\"\"Synthetic conversation profiles.\"\"\"
CONVERSATION_SEED_ANCHORS = frozenset({
    "alpha",
    "beta",
})

CONVERSATION_SELF_IMPROVEMENT_ANCHORS = frozenset({"gamma"})
CONVERSATION_SEED_ANCHORS = CONVERSATION_SEED_ANCHORS | frozenset({"delta"})
""")
    _write(root / REGISTRY, """\
\"\"\"Synthetic seed registry.\"\"\"
from substrate.seeds import REGISTRY, Seed
from substrate.seed_loader import manifest_for


def _installer_from_manifest(seed_id: str):
    def install(agent) -> None:
        return None
    install.__name__ = f"apply_{seed_id}_manifest"
    return install


REGISTRY.register(Seed(
    id="decision_core", version="1.0.0", depends_on=(),
    installer=_installer_from_manifest("decision_core"),
    description="Decision core.",
))

REGISTRY.register(Seed(
    id="learning_core", version="1.0.0", depends_on=("decision_core",),
    installer=_installer_from_manifest("learning_core"),
    description="Learning core.",
))
""")
    _write_manifest(root, "my_seed", depends_on=["core"])
    _write_manifest(root, "decision_core")
    _write_manifest(root, "learning_core", depends_on=["decision_core"])
    _write_manifest(root, "dual_process_core")
    return root


def _values(path: Path, name: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            value = node.value
            if isinstance(value, ast.Call):
                if not value.args:
                    return []
                value = value.args[0]
            if isinstance(value, (ast.Set, ast.Tuple, ast.List)):
                return [element.value for element in value.elts]
            return list(ast.literal_eval(value))
    raise AssertionError(f"no assignment to {name}")


def _registry_ids(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    ids: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Attribute)
                and call.func.attr == "register"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "REGISTRY"):
            continue
        seed_call = call.args[0]
        keyword = next(
            (item for item in seed_call.keywords if item.arg == "id"), None)
        ids.append(keyword.value.value)
    return ids


def _errors(result: dict[str, Any]) -> str:
    return "\n".join(result.get("errors") or [])


def test_preview_add_remove_boot_leaves_bytes_and_shows_diff(
        tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    core = root / BOOT_CORE
    before = core.read_bytes()

    added = B.boot_edit(root, [{
        "op": "add_boot", "file": BOOT_CORE,
        "name": "CORE_SEEDS", "seed": "my_seed",
    }])
    assert added["ok"] is True, added
    assert added["applied"] is False
    assert added["backup"] is None
    assert added["files"][0]["path"] == BOOT_CORE
    assert added["base_sha256"] == added["files"][0]["base_sha256"]
    assert added["base_sha256"] != added["new_sha256"]
    assert "my_seed" in added["diff_view"]
    assert core.read_bytes() == before

    removed = B.boot_edit(root, [{
        "op": "remove_boot", "file": BOOT_CORE,
        "name": "CORE_SEEDS", "seed": "decision_core",
    }])
    assert removed["ok"] is True, removed
    assert removed["applied"] is False
    assert "decision_core" in removed["diff_view"]
    assert core.read_bytes() == before


def test_apply_add_remove_boot_preserves_formatting_and_parses(
        tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    core = root / BOOT_CORE
    before = core.read_bytes()

    added = B.boot_edit(root, [{
        "op": "add_boot", "file": BOOT_CORE,
        "name": "CORE_SEEDS", "seed": "my_seed",
    }], write=True)
    assert added["ok"] is True, added
    assert added["applied"] is True
    assert added["rolled_back"] is False
    assert Path(added["backup"]).read_bytes() == before
    assert _values(core, "CORE_SEEDS") == [
        "decision_core", "learning_core", "my_seed"]
    text = core.read_text(encoding="utf-8")
    assert "# Keep this comment too." in text
    assert "Keep this comment: it is unrelated formatting." in text
    assert 'OPTIONAL_CORE_SEEDS = ("dual_process_core",)' in text
    ast.parse(text)

    removed = B.boot_edit(root, [{
        "op": "remove_boot", "file": BOOT_CORE,
        "name": "CORE_SEEDS", "seed": "decision_core",
    }], write=True, base_sha256=added["new_sha256"])
    assert removed["ok"] is True, removed
    assert removed["applied"] is True
    assert _values(core, "CORE_SEEDS") == ["learning_core", "my_seed"]
    assert "# Keep this comment too." in core.read_text(encoding="utf-8")

    profiles = root / BOOT_PROFILES
    profiles_before = profiles.read_bytes()
    profile_added = B.boot_edit(root, [{
        "op": "add_boot", "file": BOOT_PROFILES,
        "name": "CONVERSATION_SEED_ANCHORS", "seed": "my_seed",
    }], write=True)
    assert profile_added["ok"] is True, profile_added
    assert _values(profiles, "CONVERSATION_SEED_ANCHORS") == [
        "alpha", "beta", "my_seed"]
    profile_text = profiles.read_text(encoding="utf-8")
    assert ('CONVERSATION_SEED_ANCHORS = CONVERSATION_SEED_ANCHORS | '
            'frozenset({"delta"})') in profile_text
    assert _values(profiles, "CONVERSATION_SELF_IMPROVEMENT_ANCHORS") == [
        "gamma"]

    profile_removed = B.boot_edit(root, [{
        "op": "remove_boot", "file": BOOT_PROFILES,
        "name": "CONVERSATION_SEED_ANCHORS", "seed": "my_seed",
    }], write=True, base_sha256=profile_added["new_sha256"])
    assert profile_removed["ok"] is True, profile_removed
    assert profiles.read_bytes() == profiles_before


def test_remove_only_element_and_add_back_to_empty_literals(
        tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    core = root / BOOT_CORE
    profiles = root / BOOT_PROFILES

    removed = B.boot_edit(root, [{
        "op": "remove_boot", "file": BOOT_CORE,
        "name": "OPTIONAL_CORE_SEEDS", "seed": "dual_process_core",
    }], write=True)
    assert removed["ok"] is True, removed
    assert _values(core, "OPTIONAL_CORE_SEEDS") == []
    assert "OPTIONAL_CORE_SEEDS = ()" in core.read_text(encoding="utf-8")

    added = B.boot_edit(root, [{
        "op": "add_boot", "file": BOOT_CORE,
        "name": "OPTIONAL_CORE_SEEDS", "seed": "dual_process_core",
    }], write=True, base_sha256=removed["new_sha256"])
    assert added["ok"] is True, added
    assert _values(core, "OPTIONAL_CORE_SEEDS") == ["dual_process_core"]
    ast.parse(core.read_text(encoding="utf-8"))

    profile_removed = B.boot_edit(root, [{
        "op": "remove_boot", "file": BOOT_PROFILES,
        "name": "CONVERSATION_SELF_IMPROVEMENT_ANCHORS", "seed": "gamma",
    }], write=True)
    assert profile_removed["ok"] is True, profile_removed
    assert _values(profiles, "CONVERSATION_SELF_IMPROVEMENT_ANCHORS") == []
    assert "frozenset()" in profiles.read_text(encoding="utf-8")

    profile_added = B.boot_edit(root, [{
        "op": "add_boot", "file": BOOT_PROFILES,
        "name": "CONVERSATION_SELF_IMPROVEMENT_ANCHORS", "seed": "gamma",
    }], write=True, base_sha256=profile_removed["new_sha256"])
    assert profile_added["ok"] is True, profile_added
    assert _values(profiles, "CONVERSATION_SELF_IMPROVEMENT_ANCHORS") == [
        "gamma"]
    ast.parse(profiles.read_text(encoding="utf-8"))


def test_add_registry_appends_valid_block_and_remove_deletes_one(
        tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    registry = root / REGISTRY
    before = registry.read_bytes()

    added = B.boot_edit(root, [{
        "op": "add_registry",
        "seed": "my_seed",
        "version": "1.0.0",
        "depends_on": ["core"],
        "description": "My seed.",
    }], write=True)
    assert added["ok"] is True, added
    assert added["applied"] is True
    assert Path(added["backup"]).read_bytes() == before
    text = registry.read_text(encoding="utf-8")
    assert text.startswith(before.decode("utf-8"))
    assert text.count("REGISTRY.register(Seed(") == 3
    assert '_installer_from_manifest("my_seed")' in text
    assert 'id="my_seed", version="1.0.0", depends_on=("core",)' in text
    assert 'description="My seed."' in text
    assert _registry_ids(registry) == [
        "decision_core", "learning_core", "my_seed"]
    ast.parse(text)

    removed = B.boot_edit(root, [{
        "op": "remove_registry", "seed": "learning_core",
    }], write=True, base_sha256=added["new_sha256"])
    assert removed["ok"] is True, removed
    assert removed["applied"] is True
    text = registry.read_text(encoding="utf-8")
    assert text.count("REGISTRY.register(Seed(") == 2
    assert _registry_ids(registry) == ["decision_core", "my_seed"]
    assert 'id="learning_core"' not in text
    assert 'id="decision_core"' in text
    assert 'id="my_seed"' in text
    assert 'description="Decision core."' in text


def test_multi_file_preview_and_apply_report_per_file_hashes_and_backups(
        tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    core = root / BOOT_CORE
    registry = root / REGISTRY
    core_before = core.read_bytes()
    registry_before = registry.read_bytes()
    operations = [
        {"op": "add_boot", "file": BOOT_CORE,
         "name": "CORE_SEEDS", "seed": "my_seed"},
        {"op": "add_registry", "seed": "my_seed", "version": "1.0.0",
         "depends_on": ["core"], "description": "My seed."},
    ]

    preview = B.boot_edit(root, operations)
    assert preview["ok"] is True, preview
    assert len(preview["files"]) == 2
    assert set(preview["base_sha256"]) == {BOOT_CORE, REGISTRY}
    assert set(preview["new_sha256"]) == {BOOT_CORE, REGISTRY}
    assert preview["base_sha256"][BOOT_CORE] != preview["new_sha256"][BOOT_CORE]
    assert core.read_bytes() == core_before
    assert registry.read_bytes() == registry_before

    applied = B.boot_edit(
        root, operations, write=True, base_sha256=preview["base_sha256"])
    assert applied["ok"] is True, applied
    assert applied["applied"] is True
    assert len(applied["files"]) == 2
    assert set(applied["backup"]) == {BOOT_CORE, REGISTRY}
    for rel, backup in applied["backup"].items():
        assert backup is not None, rel
        assert Path(backup).is_file()
    assert Path(applied["backup"][BOOT_CORE]).read_bytes() == core_before
    assert Path(applied["backup"][REGISTRY]).read_bytes() == registry_before
    assert _values(core, "CORE_SEEDS")[-1] == "my_seed"
    assert _registry_ids(registry)[-1] == "my_seed"


def test_unknown_file_name_op_duplicate_missing_and_paths_refused(
        tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    core = root / BOOT_CORE
    registry = root / REGISTRY
    core_before = core.read_bytes()
    registry_before = registry.read_bytes()

    for operations, needle in (
        ([{"op": "rename", "file": BOOT_CORE,
           "name": "CORE_SEEDS", "seed": "my_seed"}], "unknown op"),
        ([{"op": "add_boot", "file": "substrate/boot_all.py",
           "name": "CORE_SEEDS", "seed": "my_seed"}], "unknown file"),
        ([{"op": "add_boot", "file": BOOT_CORE,
           "name": "ALL_SEEDS", "seed": "my_seed"}], "unknown name"),
        ([{"op": "remove_boot", "file": BOOT_CORE,
           "name": "CORE_SEEDS", "seed": "does_not_exist"}], "nothing to remove"),
        ([{"op": "remove_registry", "seed": "does_not_exist"}],
         "no REGISTRY.register"),
        ([{"op": "add_boot", "file": str(core),
           "name": "CORE_SEEDS", "seed": "my_seed"}], "absolute paths"),
        ([{"op": "add_boot", "file": "../boot_core.py",
           "name": "CORE_SEEDS", "seed": "my_seed"}], "escapes"),
    ):
        result = B.boot_edit(root, operations, write=True)
        assert result["ok"] is False, (operations, result)
        assert result["applied"] is False
        assert needle in _errors(result), (needle, result)
        assert core.read_bytes() == core_before
        assert registry.read_bytes() == registry_before

    _write(
        root / "seeds" / "generated" / "cached_seed.json",
        json.dumps(_manifest("cached_seed"), indent=2) + "\n")
    cached = B.boot_edit(root, [{
        "op": "add_registry", "seed": "cached_seed", "version": "1.0.0",
        "depends_on": [], "description": "cached",
    }], write=True)
    assert cached["ok"] is False
    assert "generated cache" in _errors(cached)

    first = B.boot_edit(root, [{
        "op": "add_boot", "file": BOOT_CORE,
        "name": "CORE_SEEDS", "seed": "my_seed",
    }], write=True)
    assert first["ok"] is True, first
    after_first = core.read_bytes()
    duplicate = B.boot_edit(root, [{
        "op": "add_boot", "file": BOOT_CORE,
        "name": "CORE_SEEDS", "seed": "my_seed",
    }], write=True, base_sha256=first["new_sha256"])
    assert duplicate["ok"] is False
    assert "duplicate" in _errors(duplicate)
    assert core.read_bytes() == after_first

    linked_root = tmp_path / "linked"
    outside = _write(tmp_path / "outside_boot.py",
                     'CORE_SEEDS = ("decision_core",)\n')
    link = linked_root / BOOT_CORE
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside)
    except OSError as exc:  # pragma: no cover - unusual filesystem
        pytest.skip(f"symlinks unavailable: {exc}")
    outside_before = outside.read_bytes()
    linked = B.boot_edit(linked_root, [{
        "op": "add_boot", "file": BOOT_CORE,
        "name": "CORE_SEEDS", "seed": "my_seed",
    }], write=True)
    assert linked["ok"] is False
    assert "symlink" in _errors(linked)
    assert outside.read_bytes() == outside_before


def test_write_rolls_back_unparseable_text_and_manifest_disagreement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path)
    registry = root / REGISTRY
    before = registry.read_bytes()

    def broken_block(*_args: Any, **_kwargs: Any) -> str:
        return "REGISTRY.register(Seed(\n"

    monkeypatch.setattr(B, "_registry_block", broken_block)
    broken = B.boot_edit(root, [{
        "op": "add_registry", "seed": "my_seed", "version": "1.0.0",
        "depends_on": ["core"], "description": "My seed.",
    }], write=True)
    assert broken["ok"] is False
    assert broken["applied"] is False
    assert broken["rolled_back"] is True
    assert "does not parse" in _errors(broken)
    assert registry.read_bytes() == before
    assert Path(broken["backup"]).read_bytes() == before
    monkeypatch.undo()

    direct = B.boot_edit(root, [{
        "op": "add_registry", "seed": "my_seed", "version": "2.0.0",
        "depends_on": ["core"], "description": "My seed.",
    }], write=True)
    assert direct["ok"] is False
    assert direct["applied"] is False
    assert "disagrees" in _errors(direct)
    assert registry.read_bytes() == before

    real_read = B._read_manifest
    calls = {"count": 0}

    def changed_manifest(path: Path) -> Any:
        calls["count"] += 1
        data, partial, error = real_read(path)
        if calls["count"] >= 2 and isinstance(data, dict):
            data = dict(data)
            data["version"] = "9.9.9"
        return data, partial, error

    monkeypatch.setattr(B, "_read_manifest", changed_manifest)
    raced = B.boot_edit(root, [{
        "op": "add_registry", "seed": "my_seed", "version": "1.0.0",
        "depends_on": ["core"], "description": "My seed.",
    }], write=True)
    assert raced["ok"] is False
    assert raced["applied"] is False
    assert raced["rolled_back"] is True
    assert "disagrees" in _errors(raced)
    assert registry.read_bytes() == before
    assert Path(raced["backup"]).read_bytes() == before


def test_mcp_registration_is_idempotent_and_preview_only(
        tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    core = root / BOOT_CORE
    before = core.read_bytes()

    class Memory:
        raw_repo_root = str(root)

    class Registry:
        def __init__(self) -> None:
            self.tools: dict[str, Any] = {}

        def add_mcp_tool(self, spec: dict[str, Any], handler: Any) -> None:
            self.tools[spec["name"]] = (spec, handler)

    registry = Registry()
    B.register_boot_edit(registry)
    B.register_boot_edit(registry)
    assert list(registry.tools) == ["ocelot_boot_edit"]
    spec, handler = registry.tools["ocelot_boot_edit"]
    assert spec["inputSchema"]["properties"]["write"]["default"] is False
    assert spec["inputSchema"]["required"] == ["operations"]

    operations = [{"op": "add_boot", "file": BOOT_CORE,
                   "name": "CORE_SEEDS", "seed": "my_seed"}]
    result = handler(Memory(), {"operations": operations})
    assert result["ok"] is True, result
    assert result["applied"] is False
    assert "my_seed" in result["diff_view"]
    assert core.read_bytes() == before

    bad_write = handler(Memory(), {"operations": operations, "write": "yes"})
    assert bad_write["ok"] is False
    assert "write must be a boolean" in _errors(bad_write)
    assert core.read_bytes() == before


def test_base_sha256_mismatch_refuses_without_writing(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    core = root / BOOT_CORE
    before = core.read_bytes()

    result = B.boot_edit(root, [{
        "op": "add_boot", "file": BOOT_CORE,
        "name": "CORE_SEEDS", "seed": "my_seed",
    }], write=True, base_sha256="0" * 64)

    assert result["ok"] is False
    assert result["applied"] is False
    assert result["rolled_back"] is False
    assert result["backup"] is None
    assert "stale base" in _errors(result)
    assert core.read_bytes() == before
