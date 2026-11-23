"""Rust module resolution: crate roots, mod.rs, #[path] and reingest."""
from __future__ import annotations

from pathlib import Path

from ozagia.core.memory import DevMemory, build_index


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _build(root: Path) -> DevMemory:
    out = root / ".ozagia" / "graph.json"
    build_index(root, out, include_docs="none", include_git=False)
    return DevMemory(out, root / ".ozagia" / "journal.jsonl", root=root)


def _imports(mem: DevMemory, rel: str) -> set[str]:
    fid = mem.node_by_key(f"file:{rel}")
    assert fid is not None
    return {mem.key_of(int(e["tgt"])) or "" for e in mem._out.get(fid, ())
            if e["type"] == "imports"}


def test_crate_roots_and_module_layouts(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "lib.rs",
           "mod plain;\nmod dir;\npub fn run() {}\n")
    _write(tmp_path / "src" / "plain.rs", "mod nested;\npub fn plain() {}\n")
    _write(tmp_path / "src" / "plain" / "nested.rs", "pub fn nested() {}\n")
    _write(tmp_path / "src" / "dir" / "mod.rs", "mod child;\npub fn dir() {}\n")
    _write(tmp_path / "src" / "dir" / "child.rs", "pub fn child() {}\n")

    mem = _build(tmp_path)

    assert _imports(mem, "src/lib.rs") == {
        "file:src/plain.rs", "file:src/dir/mod.rs"}
    assert _imports(mem, "src/plain.rs") == {"file:src/plain/nested.rs"}
    assert _imports(mem, "src/dir/mod.rs") == {"file:src/dir/child.rs"}


def test_test_root_and_bin_root_module_layouts(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "bin" / "tool.rs",
           "mod helper;\npub fn main() {}\n")
    _write(tmp_path / "src" / "bin" / "helper.rs", "pub fn helper() {}\n")
    _write(tmp_path / "tests" / "it.rs", "mod common;\n")
    _write(tmp_path / "tests" / "common.rs", "pub fn fixture() {}\n")

    mem = _build(tmp_path)

    assert _imports(mem, "src/bin/tool.rs") == {"file:src/bin/helper.rs"}
    assert _imports(mem, "tests/it.rs") == {"file:tests/common.rs"}


def test_path_attribute_is_recorded_not_resolved(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "lib.rs",
           '#[path = "weird.rs"]\nmod weird;\npub fn run() {}\n')
    _write(tmp_path / "src" / "weird.rs", "pub fn weird() {}\n")

    mem = _build(tmp_path)

    assert _imports(mem, "src/lib.rs") == set()
    fid = mem.node_by_key("file:src/lib.rs")
    assert fid is not None
    assert mem.nodes[fid]["attrs"]["rust_unsupported_mods"] == [
        {"name": "weird", "line": 2,
         "reason": "#[path] module paths are not resolved"},
    ]


def test_reingest_switches_module_layout(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "lib.rs", "mod target;\npub fn run() {}\n")
    _write(tmp_path / "src" / "target" / "mod.rs", "pub fn target() {}\n")

    mem = _build(tmp_path)
    assert _imports(mem, "src/lib.rs") == {"file:src/target/mod.rs"}

    (tmp_path / "src" / "target" / "mod.rs").unlink()
    _write(tmp_path / "src" / "target.rs", "pub fn target() {}\n")
    mem.reingest(["src/target/mod.rs", "src/target.rs"])

    assert _imports(mem, "src/lib.rs") == {"file:src/target.rs"}
