"""Offline evaluation of the Ocelot test planner against historical changes.

A case is a small, frozen record of a change and the tests a person expected::

    {
      "name": "local seed",
      "changed": ["seeds/local.json"],
      "expected_tests": ["tests/test_local_migrated.py"],
      "expected_mode": "affected"
    }

``evaluate`` loads the repository's existing ``.ozagia/graph.json`` read-only
(or accepts an injected ``DevMemory``), runs the same
:func:`ozagia.integrations.ocelot.workspace.test_plan` that agents use, and
reports recall, expected misses, over-conservative full gates and engine
rebuilds.  It never executes pytest, builds the engine, imports Ocelot,
reaches the network or calls a model.

The budget bounds the per-case displayed test list.  A full gate is never
truncated for coverage: its expected tests are checked against the checkout
and it never contributes to recall.  Its displayed file list is still capped
by the budget.

CLI (run from any working directory; both paths are explicit)::

    python -m ozagia.integrations.ocelot.workspace_eval \
        --root /path/to/ocelot \
        --cases /path/to/ozagia/docs/results/ocelot_workspace_eval_cases_2026-11-26.json \
        --json --out /tmp/ocelot-eval.md

Exit status is 0 for ``ok: true``, 1 for ``ok: false``, and 2 for invalid
arguments, cases, or I/O errors.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .workspace import test_plan

SCHEMA = "ozagia.ocelot.workspace_eval.v1"
MODES = frozenset({"affected", "full", "none"})
DEFAULT_BUDGET_TESTS = 20
MAX_CASE_NAME = 200
MAX_LISTED = 100


class WorkspaceEvalError(ValueError):
    """An evaluation case, snapshot or report path is invalid."""


def _norm_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part == ".." for part in path.parts):
        raise WorkspaceEvalError(f"invalid repository-relative path: {value!r}")
    norm = path.as_posix()
    if norm in ("", "."):
        raise WorkspaceEvalError(f"invalid repository-relative path: {value!r}")
    return norm


def _path_list(value: Any, *, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise WorkspaceEvalError(f"{where}: must be a list of paths")
    out: list[str] = []
    for item in value:
        path = _norm_path(item)
        if path not in out:
            out.append(path)
    return out


def _norm_case(raw: Any, index: int) -> dict[str, Any]:
    where = f"case {index + 1}"
    if not isinstance(raw, Mapping):
        raise WorkspaceEvalError(f"{where}: must be a table")
    name = str(raw.get("name") or "").strip()
    if not name:
        name = f"case-{index + 1}"
    if len(name) > MAX_CASE_NAME:
        raise WorkspaceEvalError(
            f"{where}.name: at most {MAX_CASE_NAME} characters")
    changed = _path_list(raw.get("changed"), where=f"{where}.changed")
    expected = _path_list(
        raw.get("expected_tests"), where=f"{where}.expected_tests")
    mode = raw.get("expected_mode")
    if mode is None:
        mode = "affected" if expected else "none"
    if not isinstance(mode, str) or mode not in MODES:
        raise WorkspaceEvalError(
            f"{where}.expected_mode: must be one of "
            + ", ".join(sorted(MODES)))
    return {
        "name": name,
        "changed": changed,
        "expected_tests": expected,
        "expected_mode": mode,
    }


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    """Load and normalise cases from a JSON list or ``{"cases": [...]}``."""
    source = Path(path).expanduser()
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkspaceEvalError(f"{source}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkspaceEvalError(f"{source}: invalid JSON: {exc}") from exc
    if isinstance(data, Mapping):
        data = data.get("cases")
    if not isinstance(data, list):
        raise WorkspaceEvalError(
            f"{source}: expected a list of cases or a table with 'cases'")
    return [_norm_case(raw, index) for index, raw in enumerate(data)]


def _load_memory(root: Path) -> Any:
    from ozagia.config import state_path
    from ozagia.core.memory import DevMemory

    dump = state_path(root, "graph.json")
    if not dump.is_file():
        raise WorkspaceEvalError(
            f"no graph snapshot at {dump}; run `ozagia index --root {root}` "
            "first")
    journal = state_path(root, "journal.jsonl")
    try:
        return DevMemory(dump, journal, root=root)
    except (OSError, ValueError) as exc:
        raise WorkspaceEvalError(
            f"cannot read graph snapshot at {dump}: {exc}") from exc


def _full_gate_missing(root: Path, expected: Iterable[str],
                       selected: Iterable[str]) -> list[str]:
    selected_set = set(selected)
    missing: list[str] = []
    for rel in expected:
        if rel in selected_set:
            continue
        candidate = root / rel
        if rel.startswith("tests/") and candidate.is_file():
            continue
        missing.append(rel)
    return missing


def _warn(warnings: list[str], message: str) -> None:
    if message and message not in warnings:
        warnings.append(message)


def evaluate(root: str | Path, cases: Sequence[Mapping[str, Any]], *,
             budget_tests: int = DEFAULT_BUDGET_TESTS,
             memory: Any | None = None) -> dict[str, Any]:
    """Evaluate the test planner over explicit historical cases.

    ``memory`` is an optional read-only ``DevMemory``; when omitted the
    repository's existing ``.ozagia/graph.json`` is loaded.  ``budget_tests``
    bounds the affected selected slice considered per case and must be at
    least one.
    """
    if isinstance(budget_tests, bool) or not isinstance(budget_tests, int):
        raise WorkspaceEvalError("budget_tests: must be an integer")
    if budget_tests < 1:
        raise WorkspaceEvalError("budget_tests: must be at least 1")

    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise WorkspaceEvalError(f"repository root does not exist: {root_path}")
    normalised = [_norm_case(raw, index)
                  for index, raw in enumerate(list(cases))]
    if not normalised:
        raise WorkspaceEvalError("cases: at least one case is required")
    mem = memory if memory is not None else _load_memory(root_path)

    warnings: list[str] = []
    details: list[dict[str, Any]] = []
    counts = {"affected": 0, "full": 0, "none": 0}
    actual_counts = {"affected": 0, "full": 0, "none": 0, "error": 0}
    affected_expected = 0
    affected_matched = 0
    expected_miss = 0
    missed_tests: list[str] = []
    missed_cases: list[str] = []
    full_gate_expected_affected = 0
    full_gate_expected_affected_cases: list[str] = []
    engine_rebuild_cases: list[str] = []
    mode_mismatches: list[str] = []

    for case in normalised:
        expected_mode = case["expected_mode"]
        expected = list(case["expected_tests"])
        counts[expected_mode] += 1
        for rel in expected:
            if not (root_path / rel).is_file():
                _warn(warnings,
                      f"{case['name']}: expected test is not on disk: {rel}")

        plan_error: str | None = None
        try:
            plan = test_plan(mem, changed=case["changed"] or None)
        except Exception as exc:  # noqa: BLE001 - report, do not abort the run
            plan_error = f"{type(exc).__name__}: {exc}"
            plan = {
                "ok": False,
                "mode": "error",
                "tests": [],
                "test_count": 0,
                "full_gate": False,
                "engine_rebuild": False,
                "full_reasons": [],
                "warnings": [f"test planner failed: {plan_error}"],
                "commands": [],
            }
        actual_mode = str(plan.get("mode") or "error")
        actual_counts[actual_mode] = actual_counts.get(actual_mode, 0) + 1
        if actual_mode != expected_mode:
            mode_mismatches.append(case["name"])

        selected_all = [str(path) for path in plan.get("tests") or []]
        if actual_mode == "full":
            selected = selected_all[:budget_tests]
            if expected_mode == "full":
                missing: list[str] = []
            else:
                missing = _full_gate_missing(
                    root_path, expected, selected_all)
        else:
            selected = selected_all[:budget_tests]
            if actual_mode == "affected":
                selected_set = set(selected)
                missing = [rel for rel in expected if rel not in selected_set]
            else:
                missing = list(expected)

        covered = not missing
        budget_exceeded = (
            actual_mode == "affected" and len(selected_all) > budget_tests)
        recall: float | None = None
        if actual_mode == "affected" and expected:
            matched = len(expected) - len(missing)
            recall = matched / len(expected)
            affected_expected += len(expected)
            affected_matched += matched
        if not covered and case["name"] not in missed_cases:
            missed_cases.append(case["name"])
        expected_miss += len(missing)
        for rel in missing:
            if rel not in missed_tests:
                missed_tests.append(rel)

        if actual_mode == "full" and expected_mode == "affected":
            full_gate_expected_affected += 1
            full_gate_expected_affected_cases.append(case["name"])
            _warn(
                warnings,
                f"{case['name']}: planner required the full gate but the "
                "case expected an affected slice")
        if budget_exceeded:
            _warn(
                warnings,
                f"{case['name']}: planner selected {len(selected_all)} test "
                f"file(s), above the evaluation budget {budget_tests}")
        if plan.get("engine_rebuild"):
            engine_rebuild_cases.append(case["name"])
            _warn(
                warnings,
                f"{case['name']}: Rust engine rebuild required before "
                "measuring")
        if missing:
            _warn(
                warnings,
                f"{case['name']}: missed expected test file(s): "
                + ", ".join(missing))
        if plan_error:
            _warn(warnings, f"{case['name']}: {plan_error}")
        for warning in plan.get("warnings") or []:
            _warn(warnings, f"{case['name']}: {warning}")

        detail: dict[str, Any] = {
            "name": case["name"],
            "changed": case["changed"],
            "expected_mode": expected_mode,
            "mode": actual_mode,
            "plan_ok": plan.get("ok", True) is not False,
            "selected_count": len(selected),
            "selected_total": len(selected_all),
            "selected_truncated": len(selected_all) > len(selected),
            "budget_exceeded": budget_exceeded,
            "selected": selected,
            "selected_tests": selected,
            "expected_count": len(expected),
            "expected_tests": expected,
            "missed": missing,
            "recall": recall,
            "covered": covered,
            "full_gate": bool(plan.get("full_gate")),
            "engine_rebuild": bool(plan.get("engine_rebuild")),
            "full_reasons": list(plan.get("full_reasons") or [])[:MAX_LISTED],
            "plan_warnings": list(plan.get("warnings") or [])[:MAX_LISTED],
            "commands": list(plan.get("commands") or [])[:10],
        }
        if plan_error:
            detail["error"] = plan_error
        details.append(detail)

    recall_overall = (
        affected_matched / affected_expected if affected_expected else None)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": (expected_miss == 0
               and full_gate_expected_affected == 0
               and not missed_cases),
        "root": str(root_path),
        "budget_tests": budget_tests,
        "cases": len(details),
        "affected_cases": counts["affected"],
        "full_cases": counts["full"],
        "none_cases": counts["none"],
        "actual_affected_cases": actual_counts["affected"],
        "actual_full_cases": actual_counts["full"],
        "actual_none_cases": actual_counts["none"],
        "covered_cases": len(details) - len(missed_cases),
        "missed_cases": missed_cases,
        "expected_miss": expected_miss,
        "recall": recall_overall,
        "expected_total": affected_expected,
        "matched_expected": affected_matched,
        "missed_tests": sorted(missed_tests),
        "missed_expected_tests": sorted(missed_tests),
        "full_gate_required_but_expected_affected": (
            full_gate_expected_affected),
        "full_gate_required_but_expected_affected_cases": (
            full_gate_expected_affected_cases),
        "engine_rebuild": len(engine_rebuild_cases),
        "engine_rebuild_cases": engine_rebuild_cases,
        "mode_mismatches": mode_mismatches,
        "warnings": warnings,
        "details": details,
        "case_details": details,
    }
    return result


def _md(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|")


def render_markdown(result: Mapping[str, Any]) -> str:
    """The compact Markdown evaluation report."""
    recall = result.get("recall")
    recall_text = "n/a (no affected expectations)" if recall is None else (
        f"{float(recall) * 100:.1f}%")
    lines = [
        "# Ocelot test planner evaluation",
        "",
        f"- result: {'pass' if result.get('ok') else 'review'}",
        f"- root: `{_md(result.get('root'))}`",
        f"- budget: {result.get('budget_tests')} test file(s) displayed per case",
        f"- cases: {result.get('cases')} "
        f"(affected {result.get('affected_cases')}, "
        f"full {result.get('full_cases')}, "
        f"none {result.get('none_cases')})",
        f"- recall (affected): {recall_text}",
        f"- expected misses: {result.get('expected_miss')}",
        f"- full gate required but expected affected: "
        f"{result.get('full_gate_required_but_expected_affected')}",
        f"- engine rebuild cases: {result.get('engine_rebuild')}",
        "",
        "## Cases",
        "",
        "| case | expected | plan | selected | expected | recall | covered |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for detail in result.get("details") or []:
        case_recall = detail.get("recall")
        case_recall_text = (
            "n/a" if case_recall is None else f"{float(case_recall) * 100:.0f}%")
        selected = str(detail.get("selected_count"))
        if detail.get("selected_total") != detail.get("selected_count"):
            selected = f"{selected} of {detail.get('selected_total')}"
        lines.append(
            f"| {_md(detail.get('name'))} | {_md(detail.get('expected_mode'))} "
            f"| {_md(detail.get('mode'))} | {selected} "
            f"| {detail.get('expected_count')} | {case_recall_text} "
            f"| {'yes' if detail.get('covered') else 'no'} |")

    for detail in result.get("details") or []:
        lines.extend(["", f"### {_md(detail.get('name'))}", ""])
        lines.append("- changed: " + (", ".join(
            f"`{_md(path)}`" for path in detail.get("changed") or []) or "-"))
        if detail.get("full_reasons"):
            lines.append("- full reasons: " + "; ".join(
                _md(item) for item in detail["full_reasons"]))
        if detail.get("missed"):
            lines.append("- missed: " + ", ".join(
                f"`{_md(path)}`" for path in detail["missed"]))
        if detail.get("engine_rebuild"):
            lines.append("- engine rebuild required before measuring")
        for warning in detail.get("plan_warnings") or []:
            lines.append(f"- plan warning: {_md(warning)}")
        if detail.get("error"):
            lines.append(f"- error: {_md(detail['error'])}")

    lines.extend(["", "## Notes", ""])
    warnings = result.get("warnings") or []
    if warnings:
        lines.extend(f"- {_md(item)}" for item in warnings)
    else:
        lines.append("- no warnings")
    lines.extend([
        "",
        "No tests were executed. The planner and this evaluation are "
        "read-only; a full-gate row is coverage guidance, not a recall claim. "
        "This was an offline replay: no pytest, engine build, model call or "
        "network request was made. It is not a live agent-workflow/model-task "
        "evaluation.",
        "",
    ])
    return "\n".join(lines)


def write_report(result: Mapping[str, Any], path: str | Path) -> dict[str, Any]:
    """Write a JSON or Markdown report, inferred from the suffix."""
    target = Path(path).expanduser()
    suffix = target.suffix.lower()
    if suffix == ".json":
        text = json.dumps(result, indent=2, sort_keys=True) + "\n"
        kind = "json"
    elif suffix in {".md", ".markdown"}:
        text = render_markdown(result)
        kind = "markdown"
    else:
        raise WorkspaceEvalError(
            f"{target}: report suffix must be .json or .md")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise WorkspaceEvalError(f"{target}: {exc}") from exc
    return {
        "path": str(target),
        "format": kind,
        "bytes": len(text.encode("utf-8")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline evaluation of the Ocelot affected-test planner.")
    parser.add_argument("--root", default=".", help="Ocelot checkout to use")
    parser.add_argument("--cases", required=True, help="case JSON file")
    parser.add_argument("--json", action="store_true",
                        help="print the JSON report")
    parser.add_argument("--out", default=None,
                        help="write a .json or .md report to this path")
    parser.add_argument("--budget-tests", type=int,
                        default=DEFAULT_BUDGET_TESTS,
                        help="test-file display budget per case (default: 20)")
    args = parser.parse_args(argv)

    try:
        cases = load_cases(args.cases)
        result = evaluate(args.root, cases, budget_tests=args.budget_tests)
    except (OSError, ValueError) as exc:
        print(f"ocelot-workspace-eval: {exc}", file=sys.stderr)
        return 2

    if args.out:
        try:
            written = write_report(result, args.out)
        except WorkspaceEvalError as exc:
            print(f"ocelot-workspace-eval: {exc}", file=sys.stderr)
            return 2
        print(f"ocelot-workspace-eval: wrote {written['path']} "
              f"({written['format']})")
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif not args.out:
        print(render_markdown(result))
    return 0 if result.get("ok") else 1


__all__ = [
    "DEFAULT_BUDGET_TESTS",
    "MODES",
    "SCHEMA",
    "WorkspaceEvalError",
    "evaluate",
    "load_cases",
    "main",
    "render_markdown",
    "write_report",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
