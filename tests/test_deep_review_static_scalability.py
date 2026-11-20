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


def test_repeated_iteration_candidate_ignores_deferred_function_body(tmp_path):
    (tmp_path / "work.py").write_text(
        "for item in items:\n"
        "    def later():\n"
        "        for other in items:\n"
        "            consume(other)\n",
        encoding="utf-8",
    )

    findings, checked, skipped = static.analyze(tmp_path)

    assert checked == ["work.py"]
    assert skipped == []
    assert not any(finding["type"] == "repeated_nested_iteration"
                   for finding in findings)


def test_repeated_iteration_candidate_keeps_executed_nested_loop(tmp_path):
    (tmp_path / "work.py").write_text(
        "for item in items:\n"
        "    for other in items:\n"
        "        consume(other)\n",
        encoding="utf-8",
    )

    findings, _, _ = static.analyze(tmp_path)

    assert [finding["type"] for finding in findings] == [
        "repeated_nested_iteration",
    ]
