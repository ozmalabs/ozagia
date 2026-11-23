"""Cargo workspace, module tree and Rust import resolution."""
from __future__ import annotations

from pathlib import Path

from ozagia.core import rust_modules
from ozagia.core import symbol_graph as sg
from ozagia.core.repo_map import build_reference_graph, update_tag_cache
from ozagia.index.rust_symbols import parse_rust


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _workspace(root: Path) -> None:
    _write(root / "Cargo.toml",
           '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n')
    _write(root / "src" / "lib.rs",
           "mod model;\n"
           "mod nested;\n"
           "mod util { pub fn helper() {} }\n"
           "pub use crate::model::Item as PublicItem;\n"
           "use crate::model::build;\n"
           "use crate::util::helper;\n"
           "use crate::model::{self as m, Item as I};\n"
           "use crate::util::*;\n"
           "use lib2::cross as cross_dep;\n"
           "use serde::Serialize;\n"
           "use somecrate::build as external_build;\n"
           "pub fn top() {\n"
           "    build();\n"
           "    helper();\n"
           "    crate::model::build();\n"
           "}\n"
           "pub fn glob_top() { helper(); }\n"
           "pub fn other_top() { cross_dep(); }\n"
           "pub fn ext_top() { external_build(); }\n"
           "pub struct Widget;\n"
           "impl Widget { pub fn make() -> Self { Widget } }\n"
           "pub fn make_widget() { Widget::make(); }\n"
           "#[test]\n"
           "fn unit() {}\n")
    _write(root / "src" / "model.rs",
           "pub struct Item;\npub fn build() {}\n")
    _write(root / "src" / "other.rs", "pub fn build() {}\n")
    _write(root / "src" / "nested.rs",
           "mod inner;\n"
           "use super::model::build;\n"
           "use crate::model::build as crate_build;\n"
           "use self::inner::Thing;\n"
           "pub fn nested_build() { build(); inner::go(); }\n")
    _write(root / "src" / "nested" / "inner.rs",
           "pub struct Thing;\npub fn go() {}\n")
    _write(root / "crates" / "lib2" / "Cargo.toml",
           '[package]\nname = "lib2"\nversion = "0.1.0"\nedition = "2021"\n')
    _write(root / "crates" / "lib2" / "src" / "lib.rs",
           "pub struct Other;\npub fn cross() {}\n")
    _write(root / "tests" / "it.rs",
           "use demo::model::build;\n"
           "use demo::model::Item as I;\n"
           "#[test]\n"
           "fn integration() { build(); }\n")
    _write(root / "benches" / "bench.rs",
           "use demo::model::build;\n"
           "#[bench]\n"
           "fn bench_it() { build(); }\n")


def _imports(rel: str, root: Path) -> dict[str, dict]:
    parsed = parse_rust(root / rel)
    out: dict[str, dict] = {}
    for item in parsed["imports"]:
        out.setdefault(str(item["name"]), item)
    return out


def test_manifest_target_helpers_read_targets_without_source_trees(
        tmp_path: Path) -> None:
    _workspace(tmp_path)

    targets = rust_modules.targets_for_manifest(tmp_path, "Cargo.toml")
    assert {target.kind for target in targets} >= {"lib", "test", "bench"}
    assert all(target.package == "demo" for target in targets)
    assert rust_modules.targets_for_manifest(
        tmp_path, "../outside/Cargo.toml") == []

    packages = rust_modules.package_targets(tmp_path)
    assert set(packages) == {"demo", "lib2"}
    assert packages["lib2"][0].root == "crates/lib2/src/lib.rs"


def test_workspace_targets_modules_and_contexts(tmp_path: Path) -> None:
    _workspace(tmp_path)
    workspace = rust_modules.discover(tmp_path)

    summary = workspace.summary()
    assert summary["crates"] == ["demo", "lib2"]
    assert {"lib", "test", "bench"} <= {
        target.kind for target in workspace.targets.values()}
    lib = workspace.crates["demo"]
    assert lib.root == "src/lib.rs"
    model = workspace.modules[(lib.id, ("model",))]
    assert model.file == "src/model.rs"
    util = workspace.modules[(lib.id, ("util",))]
    assert util.file == "src/lib.rs"
    assert util.prefix == "util"
    assert workspace.context("src/lib.rs", "util.helper").prefix == "util"
    assert workspace.context("src/lib.rs", "top").prefix == ""


def test_use_resolution_crate_self_super_inline_and_external(
        tmp_path: Path) -> None:
    _workspace(tmp_path)
    workspace = rust_modules.discover(tmp_path)
    imports = _imports("src/lib.rs", tmp_path)

    build = workspace.resolve("src/lib.rs", imports["build"])
    assert build["status"] == "resolved"
    assert build["file"] == "src/model.rs"
    assert build["qualname"] == "build"

    helper = workspace.resolve("src/lib.rs", imports["helper"])
    assert helper["status"] == "resolved"
    assert helper["file"] == "src/lib.rs"
    assert helper["qualname"] == "util.helper"

    module_alias = workspace.resolve("src/lib.rs", imports["m"])
    assert module_alias["status"] == "resolved"
    assert module_alias["file"] == "src/model.rs"

    external = workspace.resolve(
        "src/lib.rs", {"module": "serde", "imported": "Serialize",
                       "name": "Serialize", "scope": "", "glob": False})
    assert external["status"] == "external"

    test_imports = _imports("tests/it.rs", tmp_path)
    cross_crate = workspace.resolve("tests/it.rs", test_imports["build"])
    assert cross_crate["status"] == "resolved"
    assert cross_crate["file"] == "src/model.rs"

    glob_import = workspace.resolve("src/lib.rs", imports["helper"])
    assert glob_import["file"] == "src/lib.rs"

    dependency = workspace.resolve("src/lib.rs", imports["cross_dep"])
    assert dependency["status"] == "resolved"
    assert dependency["file"] == "crates/lib2/src/lib.rs"
    assert dependency["qualname"] == "cross"

    nested = _imports("src/nested.rs", tmp_path)
    super_import = workspace.resolve("src/nested.rs", nested["build"])
    assert super_import["file"] == "src/model.rs"
    # `crate::` starts at the crate root even from a nested module.
    crate_import = workspace.resolve("src/nested.rs", nested["crate_build"])
    assert crate_import["file"] == "src/model.rs"
    assert crate_import["qualname"] == "build"
    self_import = workspace.resolve("src/nested.rs", nested["Thing"])
    assert self_import["file"] == "src/nested/inner.rs"
    assert self_import["qualname"] == "Thing"


def test_reexport_and_path_resolution(tmp_path: Path) -> None:
    _workspace(tmp_path)
    workspace = rust_modules.discover(tmp_path)
    public = {"module": "crate", "imported": "PublicItem", "name": "PublicItem",
              "scope": "", "glob": False, "detail": "import.use"}

    result = workspace.resolve("src/lib.rs", public)
    assert result["status"] == "resolved"
    assert result["file"] == "src/model.rs"
    assert result["qualname"] == "Item"
    assert result.get("reexported") is True

    path = workspace.resolve_path("src/lib.rs", "crate::model::build")
    assert path["status"] == "resolved"
    assert path["file"] == "src/model.rs"
    assert path["qualname"] == "build"


def test_symbol_graph_resolves_rust_imports_without_false_edges(
        tmp_path: Path) -> None:
    _workspace(tmp_path)
    rels = ["src/lib.rs", "src/model.rs", "src/other.rs", "src/nested.rs",
            "src/nested/inner.rs", "crates/lib2/src/lib.rs", "tests/it.rs"]
    cache, _ = update_tag_cache(tmp_path, cache_path=tmp_path / "tags.json",
                                files=rels)
    graph = sg.build_symbol_graph(cache)

    def edge(src: str, tgt: str):
        return next((item for item in graph["edges"]
                     if item["src"] == src and item["tgt"] == tgt), None)

    build = edge("src/lib.rs::top", "src/model.rs::build")
    helper = edge("src/lib.rs::top", "src/lib.rs::util.helper")
    integration = edge("tests/it.rs::integration", "src/model.rs::build")
    globbed = edge("src/lib.rs::glob_top", "src/lib.rs::util.helper")
    dependency = edge("src/lib.rs::other_top", "crates/lib2/src/lib.rs::cross")
    nested = edge("src/nested.rs::nested_build", "src/model.rs::build")
    nested_go = edge("src/nested.rs::nested_build",
                     "src/nested/inner.rs::go")
    method = edge("src/lib.rs::make_widget", "src/lib.rs::Widget.make")
    assert build is not None and build["resolution"] == "import"
    assert helper is not None and helper["confidence"] == 1.0
    assert integration is not None and integration["resolution"] == "import"
    assert globbed is not None
    assert dependency is not None and dependency["resolution"] == "import"
    assert nested is not None and nested_go is not None
    assert method is not None
    assert edge("src/lib.rs::top", "src/other.rs::build") is None
    # An external crate binding does not fall back to a same-named local build.
    assert edge("src/lib.rs::ext_top", "src/model.rs::build") is None
    assert graph["stats"]["rust_module_targets"] >= 4
    assert graph["stats"]["resolved_import"] >= 4


def test_repo_map_uses_resolved_rust_import_edges(tmp_path: Path) -> None:
    _workspace(tmp_path)
    rels = ["src/lib.rs", "src/model.rs", "src/other.rs"]
    cache, _ = update_tag_cache(tmp_path, cache_path=tmp_path / "tags.json",
                                files=rels)
    graph = build_reference_graph(cache)
    edges = {(item["src"], item["tgt"]): item["weight"]
             for item in graph["edges"]}
    assert edges[("src/lib.rs", "src/model.rs")] >= 1.0
    # File-level PageRank is still name-based; the symbol graph above is the
    # layer that excludes the same-named other.rs target.
