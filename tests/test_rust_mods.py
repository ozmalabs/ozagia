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


def test_path_attribute_resolves_relative_module_file(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "lib.rs",
           '#[path = "weird.rs"]\nmod weird;\npub fn run() {}\n')
    _write(tmp_path / "src" / "weird.rs", "pub fn weird() {}\n")

    mem = _build(tmp_path)

    assert _imports(mem, "src/lib.rs") == {"file:src/weird.rs"}
    fid = mem.node_by_key("file:src/lib.rs")
    assert fid is not None
    assert "rust_unsupported_mods" not in mem.nodes[fid]["attrs"]


def test_inline_module_external_file_uses_inline_directory(
        tmp_path: Path) -> None:
    _write(tmp_path / "src" / "lib.rs",
           "mod outer { mod inner; }\npub fn run() {}\n")
    _write(tmp_path / "src" / "outer" / "inner.rs",
           "pub fn inner() {}\n")

    mem = _build(tmp_path)

    assert _imports(mem, "src/lib.rs") == {"file:src/outer/inner.rs"}


def test_reingest_switches_module_layout(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "lib.rs", "mod target;\npub fn run() {}\n")
    _write(tmp_path / "src" / "target" / "mod.rs", "pub fn target() {}\n")

    mem = _build(tmp_path)
    assert _imports(mem, "src/lib.rs") == {"file:src/target/mod.rs"}

    (tmp_path / "src" / "target" / "mod.rs").unlink()
    _write(tmp_path / "src" / "target.rs", "pub fn target() {}\n")
    mem.reingest(["src/target/mod.rs", "src/target.rs"])

    assert _imports(mem, "src/lib.rs") == {"file:src/target.rs"}


def test_use_edges_resolve_local_and_workspace_files(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml",
           '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n'
           '[workspace]\nmembers = ["crates/lib2"]\n')
    _write(tmp_path / "src" / "lib.rs",
           "mod model;\nmod nested;\n"
           "use crate::model::Item;\nuse crate::nested::tick;\n"
           "use serde::Serialize;\npub fn top() {}\n")
    _write(tmp_path / "src" / "model.rs", "pub struct Item;\n")
    _write(tmp_path / "src" / "nested.rs",
           "mod inner { use super::model::Item; }\n"
           "pub fn tick() {\n"
           "    use super::model::Item;\n"
           "    use crate::model::Item as I;\n"
           "}\n")
    _write(tmp_path / "src" / "nested" / "model.rs", "pub struct Item;\n")
    _write(tmp_path / "crates" / "lib2" / "Cargo.toml",
           '[package]\nname = "lib2"\nversion = "0.1.0"\nedition = "2021"\n')
    _write(tmp_path / "crates" / "lib2" / "src" / "lib.rs",
           "use demo::model::Item;\nuse external_crate::Thing;\n"
           "pub fn use_it() {}\n")

    mem = _build(tmp_path)

    assert _imports(mem, "src/lib.rs") == {
        "file:src/model.rs", "file:src/nested.rs"}
    # An inline module's `super` is its real parent; a function-local `super`
    # keeps the file's module context rather than treating the function name
    # as a module.
    assert _imports(mem, "src/nested.rs") == {
        "file:src/model.rs", "file:src/nested/model.rs"}
    # A workspace crate name resolves to its library file; the external crate
    # stays unresolved and does not fall back to a same-named local symbol.
    assert _imports(mem, "crates/lib2/src/lib.rs") == {"file:src/model.rs"}
    assert len(mem.file("src/lib.rs")["imports"]) >= 2


def test_reingest_use_edges_follow_a_target_appearing_and_going(
        tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml",
           '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n')
    _write(tmp_path / "src" / "lib.rs",
           "mod target;\nuse crate::target::thing;\npub fn run() {}\n")

    mem = _build(tmp_path)
    assert _imports(mem, "src/lib.rs") == set()

    _write(tmp_path / "src" / "target.rs", "pub fn thing() {}\n")
    mem.reingest(["src/target.rs"])
    assert _imports(mem, "src/lib.rs") == {"file:src/target.rs"}

    (tmp_path / "src" / "target.rs").unlink()
    mem.reingest(["src/target.rs"])
    assert _imports(mem, "src/lib.rs") == set()


def test_outline_resolves_a_cargo_package_name_and_lists_misses(
        tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml", '[workspace]\nmembers = ["widget"]\n')
    _write(tmp_path / "widget" / "Cargo.toml",
           '[package]\nname = "my-widget"\nversion = "0.1.0"\n'
           'edition = "2021"\n[lib]\npath = "source/lib.rs"\n')
    _write(tmp_path / "widget" / "source" / "lib.rs",
           "pub fn build() -> bool { true }\n")

    mem = _build(tmp_path)

    for name in ("my-widget", "my_widget"):
        out = mem.outline_target(name)
        assert out["ok"] is True
        assert out["package"] == "my-widget"
        assert out["path"] == "widget/source/lib.rs"
        assert "def build" in out["text"]

    miss = mem.outline_target("widget")
    assert miss["ok"] is False
    assert "did you mean" in miss["error"]
    assert any(item.get("kind") == "package"
               and item.get("name") == "my-widget"
               for item in miss["matches"])
