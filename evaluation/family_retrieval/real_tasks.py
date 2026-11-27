"""Family ranking on the curated Bigville retrieval tasks over real snapshots.

This reuses the frozen, source-backed task set in
``evaluation/bigville_retrieval/tasks.json`` instead of constructing near-tie
queries. It expects the temporary federation produced by
``evaluation/family_retrieval/real_repos.py``: Bigville is the anchor, Ocelot
is a declared family member, and Ozagia is an unrelated distractor.

The task set was curated for single-repository retrieval, not for family
ranking; it is a real-query robustness check, not a representative family
benchmark. The default remains ``family_boost=False``. No target checkout is
imported or written, and no network or model call is made.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ozagia.core import federation

DATE = "2026-11-27"
K_VALUES = (1, 3, 5)
RANK_LIMIT = 20
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS = REPO_ROOT / "evaluation" / "bigville_retrieval" / "tasks.json"
DEFAULT_WORKDIR = Path("/tmp/opencode/family-real")
DEFAULT_JSON = REPO_ROOT / "docs/results" / f"family_retrieval_tasks_{DATE}.json"
DEFAULT_MD = REPO_ROOT / "docs/results" / f"family_retrieval_tasks_{DATE}.md"

# Two out-of-family controls: the answer lives in Ozagia, so the family arm
# should not disturb the leading result.
CONTROL_QUERIES = [
    {
        "id": "ozagia-federation-control",
        "query": "federated repository graphs discovery membership",
        "answer_path": "docs/federation.md",
    },
    {
        "id": "ozagia-supergraph-control",
        "query": "persisted relationship supergraph source hash freshness",
        "answer_path": "src/ozagia/core/supergraph.py",
    },
]


def _load_tasks(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError(f"{path}: expected an object with a tasks array")
    tasks = []
    for index, task in enumerate(data["tasks"]):
        if (not isinstance(task, dict)
                or not isinstance(task.get("id"), str)
                or not isinstance(task.get("task"), str)
                or not isinstance(task.get("expected_files"), list)
                or not task["expected_files"]
                or not all(isinstance(item, str) and item
                           for item in task["expected_files"])):
            raise ValueError(f"{path}: malformed tasks[{index}]")
        tasks.append(task)
    return tasks


def _rank_for(result: dict[str, Any], expected: list[str]) -> tuple[int | None, dict[str, Any] | None]:
    wanted = set(expected)
    hits = result.get("results", [])
    for index, hit in enumerate(hits, 1):
        if not isinstance(hit, dict):
            continue
        candidates = []
        for key in ("file", "path"):
            value = hit.get(key)
            if isinstance(value, str):
                candidates.append(value)
        key = hit.get("key")
        if isinstance(key, str):
            prefix, _, rel = key.partition(":")
            if prefix in {"file", "doc", "test", "sym", "text"} and rel:
                candidates.append(rel.split("::", 1)[0])
        if any(candidate in wanted for candidate in candidates):
            return index, hit
    return None, None


def _top(result: dict[str, Any], expected: list[str]) -> list[dict[str, Any]]:
    wanted = set(expected)
    rows = []
    for index, hit in enumerate(result.get("results", [])[:5], 1):
        if not isinstance(hit, dict):
            continue
        rows.append({
            "rank": index,
            "key": hit.get("qualified_key") or hit.get("key"),
            "kind": hit.get("kind"),
            "file": hit.get("file") or hit.get("path"),
            "score": hit.get("score"),
            "rank_score": hit.get("rank_score"),
            "family_match": hit.get("family_match"),
            "expected": (hit.get("file") in wanted
                         or hit.get("key", "").split("::", 1)[0].endswith(tuple(wanted))),
        })
    return rows


def _metrics(ranks: list[int | None]) -> dict[str, Any]:
    total = len(ranks)
    if total == 0:
        return {"n": 0, **{f"hit@{k}": 0.0 for k in K_VALUES}, "mrr": 0.0}
    result: dict[str, Any] = {"n": total}
    for k in K_VALUES:
        result[f"hit@{k}"] = sum(rank is not None and rank <= k for rank in ranks) / total
    result["mrr"] = sum(0.0 if rank is None else 1.0 / rank for rank in ranks) / total
    return result


def _search(anchor: Path, query: str, *, boost: bool) -> dict[str, Any]:
    return federation.search(anchor, query, limit=RANK_LIMIT,
                             family_boost=boost)


def _evaluate(anchor: Path, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for task in tasks:
        expected = list(task["expected_files"])
        baseline = _search(anchor, str(task["task"]), boost=False)
        boosted = _search(anchor, str(task["task"]), boost=True)
        base_rank, base_hit = _rank_for(baseline, expected)
        boost_rank, boost_hit = _rank_for(boosted, expected)
        rows.append({
            "id": task["id"],
            "kind": "family-task",
            "query": task["task"],
            "expected": expected,
            "baseline_rank": base_rank,
            "boosted_rank": boost_rank,
            "delta": (None if base_rank is None or boost_rank is None
                      else boost_rank - base_rank),
            "baseline_hit": base_hit.get("qualified_key") if base_hit else None,
            "boosted_hit": boost_hit.get("qualified_key") if boost_hit else None,
            "top_baseline": _top(baseline, expected),
            "top_boosted": _top(boosted, expected),
        })
    controls = []
    for control in CONTROL_QUERIES:
        expected = [control["answer_path"]]
        baseline = _search(anchor, control["query"], boost=False)
        boosted = _search(anchor, control["query"], boost=True)
        base_rank, base_hit = _rank_for(baseline, expected)
        boost_rank, boost_hit = _rank_for(boosted, expected)
        controls.append({
            "id": control["id"],
            "kind": "control",
            "query": control["query"],
            "expected": expected,
            "baseline_rank": base_rank,
            "boosted_rank": boost_rank,
            "delta": (None if base_rank is None or boost_rank is None
                      else boost_rank - base_rank),
            "baseline_hit": base_hit.get("qualified_key") if base_hit else None,
            "boosted_hit": boost_hit.get("qualified_key") if boost_hit else None,
            "top_baseline": _top(baseline, expected),
            "top_boosted": _top(boosted, expected),
        })
    family_ranks_base = [row["baseline_rank"] for row in rows]
    family_ranks_boost = [row["boosted_rank"] for row in rows]
    control_ranks_base = [row["baseline_rank"] for row in controls]
    control_ranks_boost = [row["boosted_rank"] for row in controls]
    return {
        "evaluation": "family-aware federation search on curated Bigville tasks",
        "date": DATE,
        "kind": "real snapshots + existing curated retrieval tasks",
        "default_behavior": "family_boost=False; lexical ranking unchanged",
        "boost": federation.FAMILY_RANK_BOOST,
        "rank_limit": RANK_LIMIT,
        "tasks": rows,
        "controls": controls,
        "metrics": {
            "family": {
                "baseline": _metrics(family_ranks_base),
                "family_boost": _metrics(family_ranks_boost),
            },
            "controls": {
                "baseline": _metrics(control_ranks_base),
                "family_boost": _metrics(control_ranks_boost),
            },
        },
        "changed_ranks": [
            {"id": row["id"], "baseline": row["baseline_rank"],
             "boosted": row["boosted_rank"], "delta": row["delta"]}
            for row in rows + controls if row["delta"] not in (None, 0)
        ],
        "note": ("Existing curated Bigville tasks, not a family benchmark. "
                 "Most expected files fall outside the federated lexical top 20; "
                 "the check therefore measures the limitation of lexical "
                 "federation search more than family ranking. The default "
                 "remains off; a representative real-query evaluation with "
                 "adequate retrieval is still required."),
    }


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Family-aware federation search on curated Bigville tasks — {report['date']}",
        "",
        "> **Scope:** real repository snapshots plus the existing curated Bigville "
        "retrieval tasks. This is a robustness check, not a representative "
        "family benchmark. The default remains `family_boost=False`.",
        "",
        "## Setup",
        "",
        "The temporary federation from `real_repos.py` is reused: Bigville is the "
        "anchor, Ocelot is in the `simulation-stack` family, and Ozagia is an "
        "unrelated distractor. The task text and expected files come from "
        "`evaluation/bigville_retrieval/tasks.json`; they were not rewritten for "
        "this check.",
        "",
        "## Aggregate results",
        "",
        "| Group | Arm | Hit@1 | Hit@3 | Hit@5 | MRR |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for group in ("family", "controls"):
        for arm in ("baseline", "family_boost"):
            metrics = report["metrics"][group][arm]
            lines.append(
                f"| {group} | `{arm}` | {_percent(metrics['hit@1'])} | "
                f"{_percent(metrics['hit@3'])} | {_percent(metrics['hit@5'])} | "
                f"{metrics['mrr']:.3f} |")
    lines += [
        "",
        "## Family tasks",
        "",
        "| Task | Baseline rank | Boosted rank | Delta | Expected |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in report["tasks"]:
        lines.append(
            f"| `{row['id']}` | {row['baseline_rank']} | {row['boosted_rank']} | "
            f"{row['delta']} | {', '.join(f'`{item}`' for item in row['expected'])} |")
    lines += [
        "",
        "## Controls",
        "",
        "| Control | Baseline rank | Boosted rank | Delta | Expected |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in report["controls"]:
        lines.append(
            f"| `{row['id']}` | {row['baseline_rank']} | {row['boosted_rank']} | "
            f"{row['delta']} | {', '.join(f'`{item}`' for item in row['expected'])} |")
    lines += [
        "",
        "## Decision",
        "",
        "The default remains `family_boost=False`. This check does not show a "
        "family-ranking gain: most curated task answers fall outside the "
        "federation lexical top 20, and the visible ranks barely move. It also "
        "shows that the curated task set, designed for richer single-repository "
        "retrieval, is not a suitable family benchmark. It is not authorization "
        "to change the default; a representative paired evaluation on real "
        "family-relevant queries with adequate retrieval is still required.",
        "",
        "## Limits",
        "",
        "- One anchor, one family member and one distractor; the task set is "
        "small and Bigville-only.",
        "- The tasks were curated for source localisation, not for family-aware "
        "ranking; they cannot estimate the rate of family-relevant queries.",
        "- Answers are matched by File/Doc path; a matching symbol or snippet "
        "node is not counted separately.",
        "- The temporary member roots differ from the recorded checkout roots, "
        "so federation reports the snapshots stale; search still ranks them.",
        "- Lexical search only; no document bodies, embeddings, model calls, "
        "network access or target writes.",
        "",
        "## Reproduce",
        "",
        "```sh",
        "python evaluation/family_retrieval/real_repos.py",
        "python evaluation/family_retrieval/real_tasks.py",
        "```",
        "",
        "The first command rebuilds the temporary federation; the second reads it "
        "and writes the JSON and Markdown reports.",
        "",
    ]
    return "\n".join(lines)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--out-md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args(argv)
    anchor = args.workdir / "bigville"
    if not (anchor / ".ozagia" / "federation.json").is_file():
        print(f"error: {anchor} has no federation manifest; run real_repos.py first")
        return 2
    tasks = _load_tasks(args.tasks)
    report = _evaluate(anchor, tasks)
    _write(args.out_json, json.dumps(report, indent=2, sort_keys=True) + "\n")
    _write(args.out_md, _markdown(report))
    print(f"wrote {args.out_json}")
    print(f"wrote {args.out_md}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
