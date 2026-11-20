"""Deterministic offline evaluation for deep-review's static candidates."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from ozagia.core.deep_review.analyzers import analyze_static
from ozagia.core.deep_review.analyzers._bounded_read import read_text

DEFAULT_CORPUS = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "deep_review_static"
MAX_LABEL_BYTES = 1_000_000
MAX_CASES = 1_000
MAX_CORPUS_BYTES = 20_000_000
MAX_SOURCE_BYTES = 1_000_000


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else (1.0 if numerator == 0 else 0.0)


def evaluate(corpus: str | Path = DEFAULT_CORPUS) -> dict[str, Any]:
    """Score predicted (case, rule) pairs against the corpus labels."""
    root = Path(corpus).resolve()
    labels_text, _ = read_text(root / "labels.json", MAX_LABEL_BYTES, root=root)
    manifest = json.loads(labels_text)
    if not isinstance(manifest, dict):
        raise ValueError("corpus manifest must be an object")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) > MAX_CASES:
        raise ValueError("corpus cases must be a bounded list")
    for index, case in enumerate(cases):
        if (not isinstance(case, dict)
                or not isinstance(case.get("id"), str) or not case["id"]
                or not isinstance(case.get("file"), str) or not case["file"]
                or not isinstance(case.get("expected"), list)
                or any(not isinstance(kind, str) or not kind
                       for kind in case["expected"])):
            raise ValueError(f"corpus case {index} has invalid id, file, or expected labels")
    expected: set[tuple[str, str]] = set()
    actual: set[tuple[str, str]] = set()
    rules = sorted({kind for case in cases for kind in case["expected"]})
    case_ids = set()
    case_predictions: dict[str, set[str]] = {}
    corpus_hash = hashlib.sha256()
    # Include label identity in the fingerprint. Hashing only source bytes and
    # expected rule names made two corpora with different case IDs collide,
    # even though the case-level predictions in the result would differ.
    corpus_hash.update(json.dumps(
        sorted(({"id": case["id"], "file": Path(case["file"]).as_posix(),
                 "expected": sorted(case["expected"])}
        for case in cases), key=lambda row: row["id"]),
        sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
    total_bytes = 0
    for case in cases:
        case_id = case["id"]
        if case_id in case_ids:
            raise ValueError(f"duplicate corpus case id: {case_id}")
        case_ids.add(case_id)
        rel = Path(case["file"])
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"corpus path escapes root: {rel}")
        path = root / rel
        try:
            source_text, source_size = read_text(path, MAX_SOURCE_BYTES, root=root)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"corpus source unavailable or unsafe: {rel}") from exc
        total_bytes += source_size
        if total_bytes > MAX_CORPUS_BYTES:
            raise ValueError("corpus source total exceeds read limit")
        source = source_text.encode("utf-8")
        corpus_hash.update(rel.as_posix().encode("utf-8") + b"\0" + source + b"\0")
        for kind in case["expected"]:
            expected.add((case_id, kind))
        # Analyze precisely the captured bytes: the corpus may change after the
        # secure read, so never let the analyzer reopen the original path.
        with tempfile.TemporaryDirectory(prefix="ozagia-static-eval-") as scratch:
            staged = Path(scratch) / "case.py"
            staged.write_bytes(source)
            findings, checked, skipped = analyze_static(staged.parent)
        relpath = "case.py"
        if relpath not in checked or skipped:
            # Analyze the one source directly when parent has unrelated inputs;
            # a fixture failure must not silently count as a true negative.
            raise ValueError(f"corpus source was not checked cleanly: {rel}")
        for finding in findings:
            if finding["keys"] == [f"file:{relpath}"]:
                actual.add((case_id, finding["type"]))
                case_predictions.setdefault(case_id, set()).add(finding["type"])

    tp = expected & actual
    fp = actual - expected
    fn = expected - actual
    negative_cases = case_ids - {case for case, _ in expected}
    false_alarm_cases = {case_id for case_id in negative_cases if case_predictions.get(case_id)}
    by_rule = {}
    for rule in sorted({kind for _, kind in actual} | set(rules)):
        rule_expected = {pair for pair in expected if pair[1] == rule}
        rule_actual = {pair for pair in actual if pair[1] == rule}
        rule_tp = rule_expected & rule_actual
        rule_fp = rule_actual - rule_expected
        rule_negative_cases = case_ids - {case for case, kind in expected if kind == rule}
        rule_negative_cases_flagged = {case for case in rule_negative_cases
                                       if (case, rule) in actual}
        by_rule[rule] = {
            "true_positive": len(rule_tp), "false_positive": len(rule_fp),
            "false_negative": len(rule_expected - rule_actual),
            "true_negative": len(rule_negative_cases - rule_negative_cases_flagged),
            "negative_cases": len(rule_negative_cases),
            "false_positive_rate": _ratio(len(rule_negative_cases_flagged), len(rule_negative_cases)),
            "precision": _ratio(len(rule_tp), len(rule_actual)),
            "recall": _ratio(len(rule_tp), len(rule_expected)),
        }
    return {
        "schema_version": 2,
        "scope": "reviewed synthetic Python positives and independent near misses for selected deep-review static rules; offline",
        "corpus_sha256": corpus_hash.hexdigest(),
        "cases": len(case_ids), "predicted": len(actual), "expected": len(expected),
        "true_positive": len(tp), "false_positive": len(fp), "false_negative": len(fn),
        "positive_cases": len(case_ids - negative_cases), "negative_cases": len(negative_cases),
        "false_alarm_cases": len(false_alarm_cases),
        "case_false_alarm_rate": _ratio(len(false_alarm_cases), len(negative_cases)),
        "noise_rate": _ratio(len(fp), len(actual)),
        "precision": _ratio(len(tp), len(actual)), "recall": _ratio(len(tp), len(expected)),
        "case_predictions": {case_id: sorted(case_predictions.get(case_id, set()))
                             for case_id in sorted(case_ids)},
        "rules": by_rule,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path, help="write JSON result (stdout by default)")
    args = parser.parse_args(argv)
    result = json.dumps(evaluate(args.corpus), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(result, encoding="utf-8")
    else:
        print(result, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
