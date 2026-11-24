from ozagia.core.deep_review.analyzers.docs_api import analyze, analyze_with_coverage


def test_flags_only_missing_imports_from_local_public_api(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "api.py").write_text(
        "def present():\n    pass\n\n__all__ = ['present']\n", encoding="utf-8")
    docs = tmp_path / "README.md"
    docs.write_text("from pkg.api import present\nfrom pkg.api import missing\nfrom elsewhere import missing\n", encoding="utf-8")

    findings, checked, skipped = analyze(tmp_path)

    assert checked == ["README.md"]
    assert skipped == []
    assert len(findings) == 1
    assert findings[0]["type"] == "stale_documented_api"
    assert findings[0]["line"] == 2


def test_prose_import_is_checked_but_code_examples_are_not(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def present():\n    pass\n\n__all__ = ['present']\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "from pkg.api import missing_prose\n"
        "\n"
        "```python\n"
        "from pkg.api import missing_fenced\n"
        "```\n"
        "\n"
        "For example, `from pkg.api import missing_inline` is not a live import.\n"
        "\n"
        "    from pkg.api import missing_indented\n",
        encoding="utf-8")

    result = analyze_with_coverage(tmp_path)

    assert [item["line"] for item in result["findings"]] == [1]
    assert result["coverage"]["removed_or_renamed_symbols"] == 1
    assert result["coverage"]["local_imports_checked"] == 1
    assert "fenced and indented code blocks" in " ".join(
        result["coverage"]["limitations"])


def test_unparseable_local_module_is_unknown_not_a_stale_api(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("from pkg.api import maybe\n", encoding="utf-8")

    findings, checked, skipped = analyze(tmp_path)

    assert findings == []
    assert checked == ["README.md"]
    assert skipped == [{"path": "pkg/api.py", "reason": "referenced Python module could not be parsed"}]


def test_stale_local_module_import_is_distinguished_from_an_external_one(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "api.py").write_text(
        "def present():\n    pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "from pkg.old import Thing\nfrom external.lib import thing\n",
        encoding="utf-8")

    result = analyze_with_coverage(tmp_path)

    finding = result["findings"][0]
    assert finding["type"] == "stale_documented_api"
    assert finding["api_state"] == "module_missing"
    assert finding["keys"] == ["file:README.md", "module:pkg.old"]
    assert result["coverage"]["stale_local_imports"] == 1
    assert result["coverage"]["local_imports_checked"] == 0
    assert result["coverage"]["external_imports_ignored"] == 1


def test_literal_import_module_examples_check_local_paths_only(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "api.py").write_text(
        "def current():\n    pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "import pkg.api as api\n"
        "import pkg.old as old\n"
        "import external.library as library\n"
        "import pkg.api, pkg.old\n", encoding="utf-8")

    result = analyze_with_coverage(tmp_path)

    assert len(result["findings"]) == 1
    finding = result["findings"][0]
    assert finding["api_state"] == "module_missing"
    assert finding["line"] == 2
    coverage = result["coverage"]
    assert coverage["local_module_imports_checked"] == 1
    assert coverage["stale_local_module_imports"] == 1
    assert coverage["external_module_imports_ignored"] == 1
    assert coverage["documented_modules"] == [{
        "documentation": "README.md", "line": 1,
        "module": "pkg.api", "source": "src/pkg/api.py"}]


def test_removed_or_renamed_documented_symbol_reports_near_name_evidence(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def fetch_all():\n    pass\n\n__all__ = ['fetch_all']\n",
        encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "from pkg.api import fetch\n", encoding="utf-8")

    result = analyze_with_coverage(tmp_path)

    finding = result["findings"][0]
    assert finding["type"] == "stale_documented_api"
    assert finding["api_state"] == "removed_or_renamed"
    assert finding["keys"] == [
        "file:README.md", "file:pkg/api.py", "symbol:pkg.api.fetch"]
    assert "fetch_all" in finding["rename_candidates"]
    module = result["coverage"]["modules"]["pkg/api.py"]
    assert module["public_symbols"] == 1
    assert module["documented_symbols"] == []
    assert module["undocumented_symbols"] == ["fetch_all"]
    assert result["coverage"]["removed_or_renamed_symbols"] == 1


def test_documentation_coverage_facts_are_explicit(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def present():\n    pass\n\ndef other():\n    pass\n",
        encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "from pkg.api import present\nfrom elsewhere import thing\n",
        encoding="utf-8")

    result = analyze_with_coverage(tmp_path)

    assert result["findings"] == []
    coverage = result["coverage"]
    assert coverage["docs_checked"] == ["README.md"]
    assert coverage["docs_found"] == 1
    assert coverage["local_imports_checked"] == 1
    assert coverage["external_imports_ignored"] == 1
    assert coverage["documented_symbols_checked"] == 1
    assert coverage["modules"]["pkg/api.py"]["documented_symbols"] == ["present"]
    assert coverage["modules"]["pkg/api.py"]["undocumented_symbols"] == ["other"]
    assert any("literal" in item for item in coverage["limitations"])


def test_docs_profile_integrates_api_checks_and_preserves_repo_review_coverage(tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text("def current():\n    pass\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("from pkg.api import removed\n", encoding="utf-8")
    monkeypatch.setattr(collector.repo_review, "review_repo", lambda *a, **k: {
        "findings": [], "sections": {"docs": {"checked": ["doc_links"],
            "not_checked": [{"reason": "history unavailable"}]}},
        "graph": {"freshness": "unknown"}})

    packet = collector.collect(tmp_path, profile="docs", budget_s=2)

    assert [item["type"] for item in packet["findings"]] == ["stale_documented_api"]
    assert packet["sections"]["docs"]["checked"] == [
        "doc_links", "local_documented_api_references"]
    assert packet["sections"]["docs"]["not_checked"] == [
        {"reason": "history unavailable"}]


def test_data_structure_profile_integrates_inventory_with_static_coverage(tmp_path):
    from ozagia.core.deep_review.collect import collect

    (tmp_path / "model.py").write_text(
        "from dataclasses import dataclass\n\n@dataclass\nclass Item:\n    name: str\n", encoding="utf-8")
    (tmp_path / "settings.json").write_text('{"service": {"enabled": true}}', encoding="utf-8")

    packet = collect(tmp_path, profile="data-structures", budget_s=2)

    section = packet["sections"]["data-structures"]
    assert "source_data_model_inventory" in section["checked"]
    assert section["data_inventory"]["models"][0]["name"] == "Item"
    assert section["data_inventory"]["documents"][0]["file"] == "settings.json"


def test_referenced_module_under_symlinked_directory_is_not_read(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "api.py").write_text("secret_name = 1\n", encoding="utf-8")
    (root / "pkg").symlink_to(outside, target_is_directory=True)
    (root / "README.md").write_text("from pkg.api import secret_name\n", encoding="utf-8")

    result = analyze_with_coverage(root)

    assert any(item.get("path") == "pkg/api.py" for item in result["skipped"])
    assert result["coverage"]["documented_symbols_checked"] == 0
