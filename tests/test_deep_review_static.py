from pathlib import Path

from ozagia.core.deep_review.analyzers.static import analyze


FIXTURES = Path(__file__).parent / "fixtures" / "deep_review"


def test_fixture_finds_narrow_failure_performance_and_scalability_patterns():
    findings, checked, skipped = analyze(FIXTURES)

    assert checked == ["static_patterns.py"]
    assert skipped == []
    assert {item["type"] for item in findings} == {
        "mutable_default_argument", "repeated_nested_iteration",
        "silently_suppressed_exception", "materialized_range",
    }
    assert all(item["keys"] == ["file:static_patterns.py"] for item in findings)


def test_unparseable_python_is_reported_as_unknown(tmp_path):
    (tmp_path / "broken.py").write_text("def unfinished(:\n", encoding="utf-8")

    findings, checked, skipped = analyze(tmp_path)

    assert findings == []
    assert checked == []
    assert skipped == [{"path": "broken.py",
                        "reason": "Python source could not be parsed; findings are unknown"}]


def test_file_cap_reports_remaining_python_as_unknown(tmp_path, monkeypatch):
    import ozagia.core.deep_review.analyzers.static as static

    monkeypatch.setattr(static, "MAX_FILES", 1)
    (tmp_path / "a.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("pass\n", encoding="utf-8")

    findings, checked, skipped = analyze(tmp_path)

    assert findings == []
    assert checked == ["a.py"]
    assert skipped == [{"reason": "Python file limit reached (1); remaining files not checked"}]


def test_findings_name_enclosing_symbol_and_use_symbol_scoped_stable_keys(tmp_path):
    source = '''\
class Service:
    def run(self, values=[]):
        for value in values:
            for other in values:
                pass

    def ignore(self):
        pass
'''
    file = tmp_path / "app.py"
    file.write_text(source, encoding="utf-8")
    first, _, _ = analyze(tmp_path)
    assert {finding["locations"][0]["symbol"] for finding in first} == {"Service.run"}
    first_keys = {finding["type"]: finding["stable_key"] for finding in first}

    # Adding another function does not renumber findings in Service.run.
    file.write_text(source + "\ndef another(items=[]):\n    pass\n", encoding="utf-8")
    second, _, _ = analyze(tmp_path)
    second_keys = {finding["type"]: finding["stable_key"] for finding in second}
    assert first_keys.items() <= second_keys.items()


def test_module_level_finding_has_no_guessed_symbol(tmp_path):
    (tmp_path / "app.py").write_text("items = list(range(10))\n", encoding="utf-8")
    findings, _, _ = analyze(tmp_path)
    assert findings[0]["locations"] == [{"path": "app.py"}]
