"""Offline retrieval ablations for candidate scope and graph reranking.

Tasks must explicitly provide ``diff_files``: the frozen candidate file set
for the hypothetical patch. This module never calls git or guesses a diff.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from ozagia.eval.retrieval import code_search_listing, source_files


def _tasks(path: str | Path, root: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load tasks {path}: {exc}") from exc
    rows = document.get("tasks") if isinstance(document, dict) else document
    if not isinstance(rows, list) or not rows:
        raise ValueError("tasks must be a non-empty list")
    tasks = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError(f"tasks[{index}] needs an id")
        if not isinstance(row.get("task"), str) or not row["task"].strip():
            raise ValueError(f"tasks[{index}] needs task text")
        for field in ("expected_files", "diff_files"):
            paths = row.get(field)
            if not isinstance(paths, list) or not paths or not all(
                    isinstance(p, str) and p and not Path(p).is_absolute()
                    and ".." not in Path(p).parts for p in paths):
                raise ValueError(f"tasks[{index}].{field} must be safe paths")
            missing = [p for p in paths if not (root / p).is_file()]
            if missing:
                raise ValueError(f"tasks[{index}].{field} missing: {missing}")
        tasks.append(row)
    return tasks


def _ranked(search: Any, root: Path, text: str, files: list[str],
            cache: Path, budget: int, graph: bool) -> dict[str, Any]:
    result = search(root, text, limit=len(files), files=files,
                    cache_path=cache, include_snippet=True)
    results = list(result.get("results") or [])
    if not graph:
        # The search output contains all candidates, so BM25 ranks are a
        # complete graph-free ordering. Path breaks exact lexical ties.
        results.sort(key=lambda item: (item.get("bm25_rank") or 10**9,
                                       item.get("path") or ""))
    rendered = code_search_listing({"results": results}, budget)
    return rendered


def evaluate(root: str | Path, tasks_path: str | Path, *,
             budgets: Iterable[int] = (500, 900, 1500),
             cache_dir: str | Path | None = None) -> dict[str, Any]:
    """Compare full-repository and explicit diff-file candidate retrieval."""
    from ozagia.core.search import code_search

    base = Path(root).resolve()
    tasks = _tasks(tasks_path, base)
    universe = source_files(base)
    caches = Path(cache_dir) if cache_dir else base / ".ozagia" / "eval_ablation"
    caches.mkdir(parents=True, exist_ok=True)
    runs = []
    for task in tasks:
        expected = set(task["expected_files"])
        diff = sorted(set(task["diff_files"]))
        for budget in sorted(set(int(b) for b in budgets)):
            if budget <= 0:
                raise ValueError("budgets must be positive")
            for scope, candidates in (("whole_repo", universe), ("diff", diff)):
                cache = caches / f"{scope}.json"
                for graph in (True, False):
                    rendered = _ranked(code_search, base, task["task"],
                                       candidates, cache, budget, graph)
                    selected = rendered["files"]
                    hits = expected.intersection(selected)
                    runs.append({
                        "task_id": task["id"], "budget_tokens": budget,
                        "scope": scope,
                        "graph": "aware" if graph else "less",
                        "candidate_count": len(candidates),
                        "files": selected, "hits": sorted(hits),
                        "recall": len(hits) / len(expected),
                        "precision": len(hits) / len(selected) if selected else 0.0,
                        "estimated_tokens": rendered["estimated_tokens"],
                    })
    aggregates = []
    for budget in sorted(set(int(b) for b in budgets)):
        for scope in ("whole_repo", "diff"):
            for graph in ("aware", "less"):
                group = [r for r in runs if r["budget_tokens"] == budget
                         and r["scope"] == scope and r["graph"] == graph]
                aggregates.append({
                    "budget_tokens": budget, "scope": scope, "graph": graph,
                    "tasks": len(group),
                    "mean_recall": round(sum(r["recall"] for r in group) / len(group), 6),
                    "mean_precision": round(sum(r["precision"] for r in group) / len(group), 6),
                    "mean_tokens": round(sum(r["estimated_tokens"] for r in group) / len(group), 2),
                })
    projection = json.dumps(runs, sort_keys=True, separators=(",", ":"))
    return {"schema": "ozagia.retrieval_ablation.v1", "task_count": len(tasks),
            "source_file_count": len(universe), "runs": runs,
            "aggregates": aggregates,
            "fingerprint": hashlib.sha256(projection.encode()).hexdigest(),
            "limitations": [
                "diff_files are explicit frozen candidate lists, not detected git diffs",
                "graph-less order uses BM25 rank from the same code_search index",
                "file retrieval only; no edits, tests, model calls, or live agents",
            ]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--budgets", default="500,900,1500")
    parser.add_argument("--cache-dir")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    report = evaluate(args.root, args.tasks,
                      budgets=[int(v) for v in args.budgets.split(",")],
                      cache_dir=args.cache_dir)
    output = json.dumps(report, indent=2) + "\n"
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
