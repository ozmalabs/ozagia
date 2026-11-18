import json

from ozagia.eval.deep_review_static import DEFAULT_CORPUS, evaluate, main


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
