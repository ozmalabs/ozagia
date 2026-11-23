"""Rust structural parsing: fields, qualnames, use forms, modules and graph."""
from __future__ import annotations

from pathlib import Path

from ozagia.core.memory import DevMemory, build_index
from ozagia.index.rust_symbols import parse_rust

SOURCE = '''//! module doc
/// Foo docs
#[derive(Debug)]
pub struct Foo<T> { x: T }

impl<T> Foo<T> {
    /// method doc
    #[inline]
    pub async fn bar(&self, n: i32, mut name: String) -> Result<(), E> {
        helper(n);
        let _ = Baz::qux();
    }
}

pub trait Baz { fn qux(&self); }
enum E { A, B(i32) }
const C: i32 = 1;
static S: i32 = 2;
mod inner {
    #[test]
    fn it_works() { assert_eq!(1, 1); }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn async_test() {}
}
#[path = "weird.rs"]
mod weird;
macro_rules! m { () => {} }
'''


def test_tree_sitter_fields_qualnames_docs_and_tests() -> None:
    parsed = parse_rust("src/lib.rs", SOURCE)

    assert parsed["backend"] == "tree-sitter"
    assert parsed["error"] is None
    assert parsed["crate_root"] is True
    symbols = {item["qualname"]: item for item in parsed["symbols"]}
    foo = symbols["Foo"]
    assert foo["kind"] == "struct"
    assert foo["doc"] == "Foo docs"
    assert foo["decorators"] == ["#[derive(Debug)]"]
    assert foo["visibility"] == "pub"
    bar = symbols["Foo.bar"]
    assert bar["kind"] == "method"
    assert bar["doc"] == "method doc"
    assert bar["args"] == ["self", "n", "name"]
    assert bar["returns"] == "Result<(), E>"
    assert bar["visibility"] == "pub"
    assert symbols["Baz.qux"]["kind"] == "method"
    assert {"C", "S", "E", "m", "impl Foo"} <= set(symbols)
    assert {item["qualname"] for item in parsed["tests"]} == {
        "inner.it_works", "tests.async_test"}
    assert symbols["Foo.bar"]["is_test"] is False


def test_use_forms_are_expanded_with_scope_and_public_flag() -> None:
    source = (
        "mod outer {\n"
        "    use crate::a::{self, helper as h, *};\n"
        "}\n"
        "use super::thing;\n"
        "pub use crate::model::Item as PublicItem;\n"
        "fn local() { use crate::local::Thing; }\n"
    )
    parsed = parse_rust("src/lib.rs", source)
    imports = {(item["name"], item["module"]): item
               for item in parsed["imports"]}
    assert imports[("a", "crate::a")]["imported"] == ""
    assert imports[("a", "crate::a")]["scope"] == "outer"
    assert imports[("h", "crate::a")]["imported"] == "helper"
    assert imports[("h", "crate::a")]["alias"] == "h"
    assert imports[("*", "crate::a")]["glob"] is True
    assert imports[("thing", "super")]["scope"] == ""
    assert imports[("PublicItem", "crate::model")]["pub"] is True
    assert imports[("Thing", "crate::local")]["scope"] == "local"


def test_module_entries_mark_inline_and_path_attributes() -> None:
    parsed = parse_rust("src/lib.rs", SOURCE)
    modules = {item["name"]: item for item in parsed["modules"]}
    assert modules["inner"]["inline"] is True
    assert modules["tests"]["inline"] is True
    assert modules["weird"]["inline"] is False
    assert modules["weird"]["path_attribute"] == "weird.rs"


def test_regex_fallback_keeps_symbols_modules_imports_and_tests() -> None:
    parsed = parse_rust("src/lib.rs", SOURCE, use_tree_sitter=False)

    assert parsed["backend"] == "regex"
    names = {(item["kind"], item["qualname"]) for item in parsed["symbols"]}
    assert ("struct", "Foo") in names
    assert ("fn", "bar") in names
    assert ("trait", "Baz") in names
    assert ("mod", "inner") in names
    assert parsed["modules"]
    assert any(item["glob"] and item["module"] == "super"
               for item in parsed["imports"])
    assert {item["qualname"] for item in parsed["tests"]} == {
        "it_works", "async_test"}


def test_graph_uses_rust_symbols_tests_and_containment(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        "pub struct Foo;\n"
        "impl Foo {\n"
        "    pub fn bar(&self) { helper(); }\n"
        "}\n"
        "#[test]\n"
        "fn inline_test() {}\n", encoding="utf-8")
    (tmp_path / "src" / "helper.rs").write_text(
        "pub fn helper() {}\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "it.rs").write_text(
        "use crate::Foo;\n#[test]\nfn integration() {}\n", encoding="utf-8")
    (tmp_path / "benches").mkdir()
    (tmp_path / "benches" / "bench.rs").write_text(
        "#[bench]\nfn bench_it() {}\n", encoding="utf-8")
    out = tmp_path / ".ozagia" / "graph.json"
    build_index(tmp_path, out, include_docs="none", include_git=False)
    mem = DevMemory(out, tmp_path / ".ozagia" / "journal.jsonl", root=tmp_path)

    foo = mem.node_by_key("sym:src/lib.rs::Foo")
    bar = mem.node_by_key("sym:src/lib.rs::Foo.bar")
    inline = mem.node_by_key("test:src/lib.rs::inline_test")
    integration = mem.node_by_key("test:tests/it.rs::integration")
    assert foo is not None and bar is not None
    assert inline is not None and integration is not None
    assert mem.node_by_key("sym:src/lib.rs::inline_test") is None
    assert any(edge["type"] == "contains_symbol" and int(edge["tgt"]) == bar
               for edge in mem._out.get(foo, ()))
    assert mem._files_table["tests/it.rs"]["role"] == "test"
    assert mem._files_table["benches/bench.rs"]["role"] == "test"
    assert mem.node_by_key("test:benches/bench.rs::bench_it") is not None
    assert mem._files_table["src/lib.rs"]["role"] == "source"
    assert any(edge["type"] == "has_test" for edge in
               mem._out.get(mem.node_by_key("file:src/lib.rs"), ()))
