import json
import pytest

from ozagia.eval.deep_review_static import DEFAULT_CORPUS, evaluate, main
from ozagia.core.deep_review.analyzers import analyze_static
import ozagia.eval.deep_review_static as static_eval


def test_static_eval_scores_independent_near_misses_and_noise():
    result = evaluate()

    assert result["cases"] == 12
    assert result["positive_cases"] == 4
    assert result["negative_cases"] == 8
    assert result["predicted"] == result["expected"] == 4
    assert result["true_positive"] == 4
    assert result["false_positive"] == result["false_negative"] == 0
    assert result["precision"] == result["recall"] == 1.0
    assert result["noise_rate"] == result["case_false_alarm_rate"] == 0.0
    assert result["false_alarm_cases"] == 0
    assert len(result["corpus_sha256"]) == 64
    assert all(row["false_positive_rate"] == 0.0
               for row in result["rules"].values())
    assert result["case_predictions"]["range_streamed_to_writer"] == []


def test_static_eval_cli_and_direct_result_match(tmp_path):
    output = tmp_path / "static-eval.json"

    assert main(["--corpus", str(DEFAULT_CORPUS), "--out", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == evaluate()


def test_static_eval_fingerprint_includes_case_identity(tmp_path):
    import shutil

    corpus = tmp_path / "corpus"
    shutil.copytree(DEFAULT_CORPUS, corpus)
    labels_path = corpus / "labels.json"
    labels = json.loads(labels_path.read_text(encoding="utf-8"))
    original = evaluate(corpus)["corpus_sha256"]

    labels["cases"][0]["id"] = "renamed-case"
    labels_path.write_text(json.dumps(labels), encoding="utf-8")

    assert evaluate(corpus)["corpus_sha256"] != original


def _write_corpus(root, source, *, expected=None, filename="case.py"):
    (root / "labels.json").write_text(json.dumps({
        "cases": [{"id": "case", "file": filename,
                   "expected": expected or []}],
    }), encoding="utf-8")
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_static_eval_analyzes_the_captured_source_after_mutation(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source = "for row in rows:\n    for other in rows:\n        pass\n"
    original_path = _write_corpus(
        corpus, source, expected=["repeated_nested_iteration"])
    real_analyze = analyze_static

    def mutate_then_analyze(staged_root):
        original_path.write_text("def harmless():\n    return None\n", encoding="utf-8")
        return real_analyze(staged_root)

    monkeypatch.setattr(static_eval, "analyze_static", mutate_then_analyze)
    result = evaluate(corpus)

    assert result["true_positive"] == 1
    assert result["case_predictions"]["case"] == ["repeated_nested_iteration"]


def test_static_eval_refuses_symlink_ancestor(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "case.py").write_text("pass\n", encoding="utf-8")
    (corpus / "labels.json").write_text(json.dumps({
        "cases": [{"id": "case", "file": "linked/case.py", "expected": []}],
    }), encoding="utf-8")
    (corpus / "linked").symlink_to(external, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe"):
        evaluate(corpus)


def test_static_eval_ignores_sibling_sources(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_corpus(corpus, "def harmless():\n    return None\n")
    # An unrelated invalid Python sibling must not make this case unknown.
    (corpus / "unrelated.py").write_text("def broken(:\n", encoding="utf-8")

    result = evaluate(corpus)

    assert result["predicted"] == 0
    assert result["case_predictions"] == {"case": []}
