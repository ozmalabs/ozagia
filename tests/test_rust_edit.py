"""Symbol-addressed Rust edits: previews, transactions, rename and refusals."""
from __future__ import annotations

from pathlib import Path

import pytest

from ozagia.core.memory import DevMemory, build_index
from ozagia.edit.dispatch import apply_edit_script
from ozagia.edit.rust_edit import (
    RustEditError,
    apply_lsp_rename_preview,
    apply_rust_edit_script,
    symbol_table,
)

SOURCE = '''//! module
/// Widget docs
#[derive(Debug)]
#[repr(C)]
pub struct Widget { pub value: i32 }

impl Widget {
    /// read
    pub fn get(&self) -> i32 { self.value }
    pub fn set(&mut self, value: i32) {
        self.value = value;
    }
}

pub fn helper(a: i32) -> i32 {
    a + 1
}

fn caller(a: i32) -> i32 {
    helper(a)
}

fn typed(x: Widget) -> Widget {
    Widget
}
'''


def _write(path: Path, text: str = SOURCE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _script(path: Path, ops: list[dict], **extra) -> dict:
    return {"file": path.name, "ops": ops, **extra}


def test_symbol_table_has_docs_attributes_bodies_and_implementation(
        tmp_path: Path) -> None:
    table = symbol_table(SOURCE)

    assert {"Widget", "impl Widget", "Widget.get", "Widget.set",
            "helper", "caller"} <= set(table.symbols)
    widget = table.symbols["Widget"]
    assert widget.start_line == 2  # the doc comment starts the definition span
    assert widget.body_start is not None and widget.body_end is not None
    assert table.symbols["Widget.get"].inline_body is True
    assert table.symbols["Widget.set"].inline_body is False
    assert table.ambiguous == set()


def test_replace_struct_body_span_and_insert_before(tmp_path: Path) -> None:
    _write(tmp_path / "lib.rs")
    struct_body = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "replace_body", "symbol": "Widget",
          "text": "pub value: i64"}]))
    assert "+    pub value: i64" in struct_body["diff_view"]

    start = SOURCE.index("a + 1")
    span = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "replace_span", "start": start, "end": start + len("a + 1"),
          "text": "a + 7"}]))
    assert "+    a + 7" in span["diff_view"]

    before = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "insert_before_symbol", "symbol": "helper",
          "text": "pub const MARK: i32 = 1;"}]))
    assert "+pub const MARK: i32 = 1;" in before["diff_view"]


def test_replace_body_and_replace_symbol_preview(tmp_path: Path) -> None:
    _write(tmp_path / "lib.rs")
    body = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "replace_body", "symbol": "helper", "text": "a + 2"}]))
    assert body["format"] == "rust"
    assert body["applied"] is False
    assert "a + 2" in body["diff_view"]
    assert body["lossless"]["reparse"] == {"checked": True, "ok": True}

    replaced = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "replace_symbol", "symbol": "Widget.set",
          "text": "pub fn set(&mut self, value: i32) { self.value = value; }"}]))
    assert "self.value = value" in replaced["diff_view"]
    assert "+" in replaced["diff_view"]


def test_delete_and_insert_anchors_keep_surrounding_docs_and_attributes(
        tmp_path: Path) -> None:
    _write(tmp_path / "lib.rs")
    result = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs", [
            {"op": "delete_symbol", "symbol": "caller"},
            {"op": "insert_after_symbol", "symbol": "helper",
             "text": "pub fn extra() -> i32 { 2 }"},
        ]))
    assert "-fn caller" in result["diff_view"]
    assert "+pub fn extra" in result["diff_view"]
    # Replacing the Widget definition must keep its docs/attributes out of the
    # orphaned-comment path: the span starts at the doc comment.
    replaced = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "replace_symbol", "symbol": "Widget",
          "text": "pub struct Widget { pub value: i32 }"}]))
    assert not any(line.startswith("+") and "/// Widget docs" in line
                   for line in replaced["diff_view"].splitlines())


def test_insert_import_places_one_use_after_inner_docs_and_deduplicates(
        tmp_path: Path) -> None:
    _write(tmp_path / "lib.rs")
    first = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "insert_import", "module": "std::fmt", "imported": "Debug"}]),
        write=True)
    assert "use std::fmt::Debug;" in first["diff_view"]
    assert first["changes"][0]["summary"] == "use std::fmt::Debug;"

    second = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "insert_import", "module": "std::fmt", "imported": "Debug"}]))
    assert second["changes"][0]["summary"] == "already present; skipped"
    assert second["diff_view"] == ""


def test_rename_free_item_and_struct_references(tmp_path: Path) -> None:
    _write(tmp_path / "lib.rs")
    result = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "rename_symbol", "symbol": "helper", "new_name": "assist"}]))
    assert "+pub fn assist" in result["diff_view"]
    assert "assist(a)" in result["diff_view"]
    assert not any(line.startswith("+") and "fn helper" in line
                   for line in result["diff_view"].splitlines())

    structural = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "rename_symbol", "symbol": "Widget", "new_name": "Gadget"}]))
    assert "struct Gadget" in structural["diff_view"]
    assert "impl Gadget" in structural["diff_view"]
    assert "fn typed(x: Gadget) -> Gadget" in structural["diff_view"]
    assert "fn get" in structural["diff_view"]  # methods keep their own name


def test_rename_refuses_methods_and_local_bindings(tmp_path: Path) -> None:
    _write(tmp_path / "lib.rs")
    with pytest.raises(RustEditError, match="method"):
        apply_rust_edit_script(tmp_path, _script(
            tmp_path / "lib.rs",
            [{"op": "rename_symbol", "symbol": "Widget.get",
              "new_name": "read"}]))
    shadow = "fn helper() {}\nfn caller() { let helper = 1; helper; }\n"
    _write(tmp_path / "shadow.rs", shadow)
    with pytest.raises(RustEditError, match="bound as a local"):
        apply_rust_edit_script(tmp_path, _script(
            tmp_path / "shadow.rs",
            [{"op": "rename_symbol", "symbol": "helper", "new_name": "assist"}]))


def test_insert_import_merges_an_existing_simple_group(tmp_path: Path) -> None:
    _write(tmp_path / "lib.rs",
           "use std::fmt::{Display, Debug};\n\npub fn f() {}\n")
    result = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "insert_import", "module": "std::fmt", "imported": "Write"}]))
    assert "use std::fmt::{Debug, Display, Write};" in result["diff_view"]
    assert result["changes"][0]["summary"] == "merged std::fmt::Write"


def test_lsp_rename_preview_same_file_and_cross_file_refusal(
        tmp_path: Path) -> None:
    source = ("pub fn helper(a: i32) -> i32 { a }\n"
              "fn caller() -> i32 { helper(1) }\n")
    _write(tmp_path / "lib.rs", source)
    preview = {"ok": True, "files": [{
        "path": "lib.rs",
        "edits": [
            {"line": 0, "character": 7, "end_line": 0, "end_character": 13,
             "newText": "assist"},
            {"line": 1, "character": 20, "end_line": 1, "end_character": 26,
             "newText": "assist"},
        ],
    }], "resource_operations": []}
    result = apply_lsp_rename_preview(tmp_path, preview, write=True)
    assert result["source"] == "lsp_rename_preview"
    assert result["edits"] == 2
    assert "assist" in (tmp_path / "lib.rs").read_text()
    assert "helper" not in (tmp_path / "lib.rs").read_text()

    with pytest.raises(RustEditError, match="multiple files"):
        apply_lsp_rename_preview(tmp_path, {"ok": True, "files": [
            {"path": "lib.rs", "edits": []},
            {"path": "other.rs", "edits": []},
        ]})


def test_parse_gate_base_hash_backup_and_journal(tmp_path: Path) -> None:
    _write(tmp_path / "lib.rs")
    with pytest.raises(RustEditError, match="would not parse"):
        apply_rust_edit_script(tmp_path, _script(
            tmp_path / "lib.rs",
            [{"op": "replace_symbol", "symbol": "helper",
              "text": "pub fn helper(a: i32) -> i32 { a + }"}]))

    stale = "0" * 64
    with pytest.raises(RustEditError, match="stale base"):
        apply_rust_edit_script(tmp_path, _script(
            tmp_path / "lib.rs",
            [{"op": "replace_body", "symbol": "helper", "text": "a + 3"}],
            base_sha256=stale))

    result = apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "replace_body", "symbol": "helper", "text": "a + 3"}]),
        write=True)
    assert result["applied"] is True
    assert result["backup"] and Path(result["backup"]).is_file()
    assert "a + 3" in (tmp_path / "lib.rs").read_text()
    assert result["graph_stale"] is True


def test_dispatch_routes_symbol_ops_and_rejects_cst_mixing(
        tmp_path: Path) -> None:
    _write(tmp_path / "lib.rs")
    result = apply_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "replace_body", "symbol": "helper", "text": "a + 4"}]))
    assert result["format"] == "rust"
    with pytest.raises(RustEditError):
        apply_edit_script(tmp_path, _script(
            tmp_path / "lib.rs",
            [{"op": "replace_body", "symbol": "helper", "text": "a + 4"},
             {"op": "replace_node", "target": "child[0]", "text": "x"}]))


def test_reindex_after_rust_symbol_edit(tmp_path: Path) -> None:
    _write(tmp_path / "lib.rs")
    apply_rust_edit_script(tmp_path, _script(
        tmp_path / "lib.rs",
        [{"op": "replace_body", "symbol": "helper", "text": "a + 9"}]), write=True)
    out = tmp_path / ".ozagia" / "graph.json"
    build_index(tmp_path, out, include_docs="none", include_git=False)
    mem = DevMemory(out, tmp_path / ".ozagia" / "journal.jsonl", root=tmp_path)
    assert mem.node_by_key("sym:lib.rs::helper") is not None
