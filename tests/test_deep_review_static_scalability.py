from ozagia.core.deep_review.analyzers import static


def test_scalability_candidates_include_explicit_confidence(tmp_path):
    (tmp_path / "work.py").write_text(
        "values = tuple(range(500000))\nitems = list(range(500000))\n",
        encoding="utf-8",
    )

    findings, checked, skipped = static.analyze(tmp_path)

    assert checked == ["work.py"]
    assert skipped == []
    assert [(finding["type"], finding["confidence"]) for finding in findings] == [
        ("eager_sequence_conversion", "low"),
        ("materialized_range", "medium"),
    ]


def test_ast_node_limit_marks_file_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(static, "MAX_AST_NODES", 3)
    (tmp_path / "generated.py").write_text("a = b + c\n", encoding="utf-8")

    findings, checked, skipped = static.analyze(tmp_path)

    assert findings == []
    assert checked == []
    assert skipped == [{
        "path": "generated.py",
        "reason": "AST exceeds 3 node analysis limit; findings are unknown",
    }]
