"""Offline evaluation of deep-review on a seeded, multi-module service fixture."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from ozagia.core.deep_review.analyzers.static import analyze

DEFAULT_FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "deep_review_service"


def evaluate(fixture: str | Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    """Score seeded file/rule labels and measure the offline candidate scan."""
    root = Path(fixture).resolve()
    labels = json.loads((root / "labels.json").read_text(encoding="utf-8"))
    expected: set[tuple[str, str]] = set()
    file_by_id: dict[str, str] = {}
    for case in labels["cases"]:
        case_id = case["id"]
        rel = Path(case["file"])
        if case_id in file_by_id or rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"invalid or duplicate fixture case: {case_id}")
        if not (root / rel).is_file():
            raise ValueError(f"fixture source missing: {rel}")
        file_by_id[case_id] = rel.as_posix()
        expected.update((case_id, kind) for kind in case["expected"])

    started = time.perf_counter()
    findings, checked, skipped = analyze(root)
    static_scan_seconds = time.perf_counter() - started
    if skipped or any(rel not in checked for rel in file_by_id.values()):
        raise ValueError(f"fixture was not checked cleanly: {skipped}")
    case_by_path = {path: case for case, path in file_by_id.items()}
    actual = {
        (case_by_path[Path(f["keys"][0].removeprefix("file:")).as_posix()], f["type"])
        for f in findings
        if f.get("keys") and Path(f["keys"][0].removeprefix("file:")).as_posix() in case_by_path
    }
    tp, fp, fn = expected & actual, actual - expected, expected - actual

    def ratio(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else (1.0 if numerator == 0 else 0.0)

    by_rule = {}
    for rule in sorted({kind for _, kind in expected} | {kind for _, kind in actual}):
        rule_expected = {pair for pair in expected if pair[1] == rule}
        rule_actual = {pair for pair in actual if pair[1] == rule}
        rule_tp = rule_expected & rule_actual
        by_rule[rule] = {
            "true_positive": len(rule_tp), "false_positive": len(rule_actual - rule_expected),
            "false_negative": len(rule_expected - rule_actual),
            "precision": ratio(len(rule_tp), len(rule_actual)),
            "recall": ratio(len(rule_tp), len(rule_expected)),
        }

    # Measure the reproducible offline candidate artifact. Full orchestrator
    # timing is not mixed in because its repo-level preflight depends on host
    # admission state and would make this fixture result environment-specific.
    artifact = json.dumps(findings, sort_keys=True, separators=(",", ":")).encode("utf-8")
    artifact_bytes = len(artifact)

    return {
        "schema_version": 1,
        "scope": "synthetic seeded service checkout; offline static candidates; not production accuracy",
        "fixture_files": len(file_by_id),
        "source_bytes": sum((root / p).stat().st_size for p in file_by_id.values()),
        "checked_python_files": len(checked), "predicted": len(actual), "expected": len(expected),
        "true_positive": len(tp), "false_positive": len(fp), "false_negative": len(fn),
        "precision": ratio(len(tp), len(actual)), "recall": ratio(len(tp), len(expected)),
        "rules": by_rule,
        "static_scan_seconds": round(static_scan_seconds, 6),
        "candidate_artifact_bytes": artifact_bytes,
        "limitations": ["hand-seeded labels", "Python static patterns only",
                        "no runtime validation", "single local timing sample"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    text = json.dumps(evaluate(args.fixture), indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
