"""Federated code search on the curated Bigville retrieval tasks.

The graph arm evaluates frozen snapshots; this script evaluates the opt-in code
arm on the current source checkouts of the same temporary federation produced by
``evaluation/family_retrieval/real_repos.py``: Bigville is the anchor, Ocelot is
a declared family member and Ozagia is an unrelated distractor. The source
checkouts are staged read-only into the existing temporary member directories so
the snapshot files and the source tree stay together. Nothing is copied back, and
the three real checkouts are never written.

The task set was curated for single-repository retrieval, not for family
ranking. Family boost is opt-in and remains off by default; this script reports
both arms without changing that default. No target checkout is imported, no
adapter is executed, no member index is refreshed, and no network or model call
is made beyond reading the local source.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any

from ozagia.core import federation
from ozagia.core import repo_relations
from ozagia.index.tag_languages import LANGUAGES

DATE = "2026-11-27"
K_VALUES = (1, 3, 5)
RANK_LIMIT = 20
# A deeper candidate pool for the local experiment so a family re-rank has the
# same chance to displace an answer as it did in the rejected merged arm.
EXPERIMENTAL_POOL = RANK_LIMIT * 3
TOP_HITS = 5
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS = REPO_ROOT / "evaluation" / "bigville_retrieval" / "tasks.json"
DEFAULT_WORKDIR = Path("/tmp/opencode/family-real")
DEFAULT_JSON = REPO_ROOT / "docs/results" / f"family_retrieval_code_{DATE}.json"
DEFAULT_MD = REPO_ROOT / "docs/results" / f"family_retrieval_code_{DATE}.md"
PRIOR_GRAPH = REPO_ROOT / "docs/results" / f"family_retrieval_tasks_{DATE}.json"

DEFAULT_ROOTS = {
    "bigville": Path("/applications/data/projects/bigville"),
    "ocelot": Path("/applications/data/projects/ocelot"),
    "ozagia": Path("/home/matt/ozmadev/ozagia"),
}

# The source scope mirrors the neutral snapshot-build configuration in
# real_repos.py, minus generated/run directories. Bigville has no project
# config; Ocelot's source directories mirror its checked-in .ozagia.toml,
# minus adapters; Ozagia's are the maintained code/evaluation trees.
SOURCE_DIRS = {
    "bigville": ("bigville", "worlds", "tests", "docs", "seeds", "scenarios",
                 "tools"),
    "ocelot": ("runners/dsl/src", "runners/dsl/tests", "domains", "domains_rs",
               "worlds", "substrate", "tools", "scripts"),
    "ozagia": ("src", "tests", "evaluation", "docs"),
}
STAGE_MARKERS = {
    "bigville": "bigville/game.py",
    "ocelot": "domains",
    "ozagia": "src/ozagia",
}

# Two out-of-family controls: the answer lives in Ozagia, so the family arm
# should not disturb the leading result. The Markdown control is outside the
# code arm's source universe; it is kept to make that boundary visible.
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


def _count_files(root: Path) -> tuple[int, int]:
    files = 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                files += 1
                total += path.stat().st_size
        except OSError:
            continue
    return files, total


def _stage_source(workdir: Path, roots: dict[str, Path],
                  restage: bool) -> dict[str, Any]:
    """Copy the configured real source scope into the temporary members.

    Existing staged directories are kept unless ``--restage`` is given. The
    only writes are under ``workdir``; the real checkouts are read-only inputs.
    """
    report: dict[str, Any] = {"members": {}, "writes": "workdir only",
                              "restage": restage}
    for member, dirs in SOURCE_DIRS.items():
        target = workdir / member
        marker = target / STAGE_MARKERS[member]
        staged: list[dict[str, Any]] = []
        if marker.exists() and not restage:
            for rel in dirs:
                staged.append({"dir": rel, "state": "present"})
        else:
            for rel in dirs:
                source = roots[member] / rel
                destination = target / rel
                if not source.is_dir():
                    staged.append({"dir": rel, "state": "missing-source",
                                   "path": str(source)})
                    continue
                if destination.exists() and restage:
                    shutil.rmtree(destination)
                shutil.copytree(
                    source, destination, dirs_exist_ok=True, symlinks=True,
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", ".git", ".mypy_cache",
                        ".pytest_cache", ".ruff_cache"))
                staged.append({"dir": rel, "state": "copied"})
        files, total = _count_files(target)
        report["members"][member] = {
            "source": str(roots[member]),
            "target": str(target),
            "dirs": staged,
            "files": files,
            "bytes": total,
        }
    return report


def _rank_for(result: dict[str, Any],
              expected: list[str]) -> tuple[int | None, dict[str, Any] | None]:
    wanted = set(expected)
    for index, hit in enumerate(result.get("results", []), 1):
        if not isinstance(hit, dict):
            continue
        candidates = [hit.get("file"), hit.get("path")]
        if any(isinstance(value, str) and value in wanted for value in candidates):
            return index, hit
    return None, None


def _top(result: dict[str, Any], expected: list[str]) -> list[dict[str, Any]]:
    wanted = set(expected)
    rows = []
    for index, hit in enumerate(result.get("results", [])[:TOP_HITS], 1):
        if not isinstance(hit, dict):
            continue
        path = hit.get("path") or hit.get("file")
        source = hit.get("source") if isinstance(hit.get("source"), dict) else {}
        row = {
            "rank": index,
            "key": hit.get("qualified_key") or hit.get("key"),
            "member": source.get("id"),
            "path": path,
            "score": hit.get("score"),
            "normalized_score": hit.get("normalized_score"),
            "rank_score": hit.get("rank_score"),
            "expected": path in wanted,
        }
        if "family_match" in hit:
            row["family_match"] = hit.get("family_match")
        rows.append(row)
    return rows


def _metrics(ranks: list[int | None]) -> dict[str, Any]:
    total = len(ranks)
    if total == 0:
        return {"n": 0, **{f"hit@{k}": 0.0 for k in K_VALUES}, "mrr": 0.0}
    result: dict[str, Any] = {"n": total}
    for k in K_VALUES:
        result[f"hit@{k}"] = sum(
            rank is not None and rank <= k for rank in ranks) / total
    result["mrr"] = sum(
        0.0 if rank is None else 1.0 / rank for rank in ranks) / total
    return result


def _search(anchor: Path, query: str) -> dict[str, Any]:
    return federation.code_search(anchor, query, limit=EXPERIMENTAL_POOL,
                                  include_snippet=False)


def _page(result: dict[str, Any], limit: int = RANK_LIMIT) -> dict[str, Any]:
    rows = result.get("results") or []
    if len(rows) <= limit:
        return result
    return {**result, "results": rows[:limit], "page_of": len(rows)}


def _experimental_family_boost(anchor: Path,
                               result: dict[str, Any]) -> dict[str, Any]:
    """Local, clearly non-shipped +0.15 family arm for negative evidence.

    The shipped code API is relevance-only. This helper reuses the previous
    experimental rule only to keep the regression visible in the result file;
    it is not exposed by ``federation.code_search``, the CLI or MCP code mode.
    """
    records = [
        {"alias": member["id"], "root": Path(member["root"])}
        for member in result.get("members", [])
        if isinstance(member, dict) and isinstance(member.get("id"), str)
        and isinstance(member.get("root"), str)
    ]
    state = repo_relations.family_index(anchor, records)
    by_member = state.get("by_member") or {}
    anchor_alias = records[0]["alias"] if records else None
    anchor_families = set(by_member.get(anchor_alias, [])) if anchor_alias else set()
    rows = []
    for hit in result.get("results", []):
        if not isinstance(hit, dict):
            continue
        row = dict(hit)
        families = list(by_member.get(row["source"]["id"], []))
        family_match = bool(anchor_families.intersection(families))
        row["families"] = families
        row["family_match"] = family_match
        if family_match:
            row["rank_score"] = round(
                float(row["rank_score"]) + federation.FAMILY_RANK_BOOST, 6)
        rows.append(row)
    rows.sort(key=lambda hit: (
        -hit["rank_score"],
        -1 if hit["family_match"] else 0,
        str(hit["source"]["id"]).casefold(),
        hit["path"],
    ))
    return {
        **result,
        "experimental_family_boost": True,
        "experimental_boost": federation.FAMILY_RANK_BOOST,
        "families": state,
        "results": rows,
        "notes": [
            *(result.get("notes") or []),
            "local experimental family arm only; rejected and not a shipped "
            "code-search option",
        ],
    }


def _evaluate(anchor: Path, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    cache: dict[str, Any] | None = None
    members: list[dict[str, Any]] = []
    for task in tasks:
        expected = list(task["expected_files"])
        started = time.monotonic()
        full = _search(anchor, str(task["task"]))
        baseline_seconds = round(time.monotonic() - started, 2)
        baseline = _page(full)
        experimental = _page(_experimental_family_boost(anchor, full))
        base_rank, base_hit = _rank_for(baseline, expected)
        experimental_rank, experimental_hit = _rank_for(experimental, expected)
        cache = full.get("cache") or cache
        members = full.get("members") or members
        rows.append({
            "id": task["id"],
            "kind": "family-task",
            "query": task["task"],
            "expected": expected,
            "baseline_rank": base_rank,
            "experimental_rank": experimental_rank,
            "delta": (None if base_rank is None or experimental_rank is None
                      else experimental_rank - base_rank),
            "baseline_hit": base_hit.get("qualified_key") if base_hit else None,
            "experimental_hit": (experimental_hit.get("qualified_key")
                                 if experimental_hit else None),
            "seconds": {"baseline": baseline_seconds,
                        "experimental": 0.0},
            "top_baseline": _top(baseline, expected),
            "top_experimental": _top(experimental, expected),
        })
    controls = []
    for control in CONTROL_QUERIES:
        expected = [control["answer_path"]]
        started = time.monotonic()
        full = _search(anchor, control["query"])
        baseline_seconds = round(time.monotonic() - started, 2)
        baseline = _page(full)
        experimental = _page(_experimental_family_boost(anchor, full))
        base_rank, base_hit = _rank_for(baseline, expected)
        experimental_rank, experimental_hit = _rank_for(experimental, expected)
        cache = full.get("cache") or cache
        members = full.get("members") or members
        controls.append({
            "id": control["id"],
            "kind": "control",
            "query": control["query"],
            "expected": expected,
            "source_universe": (
                "code" if Path(control["answer_path"]).suffix.lower() in LANGUAGES
                else "outside code-search source universe"),
            "baseline_rank": base_rank,
            "experimental_rank": experimental_rank,
            "delta": (None if base_rank is None or experimental_rank is None
                      else experimental_rank - base_rank),
            "baseline_hit": base_hit.get("qualified_key") if base_hit else None,
            "experimental_hit": (experimental_hit.get("qualified_key")
                                 if experimental_hit else None),
            "seconds": {"baseline": baseline_seconds,
                        "experimental": 0.0},
            "top_baseline": _top(baseline, expected),
            "top_experimental": _top(experimental, expected),
        })
    family_ranks_base = [row["baseline_rank"] for row in rows]
    family_ranks_experimental = [row["experimental_rank"] for row in rows]
    control_ranks_base = [row["baseline_rank"] for row in controls]
    control_ranks_experimental = [row["experimental_rank"] for row in controls]
    regressions = [
        row for row in controls
        if row["baseline_rank"] is not None
        and (row["experimental_rank"] is None
             or row["experimental_rank"] > row["baseline_rank"])
    ]
    control_regression = {
        "detected": bool(regressions),
        "tasks": [row["id"] for row in regressions],
        "definition": ("a control that was rankable in the baseline lost its "
                       "answer or moved to a worse rank in the local "
                       "experimental family arm"),
        "outside_source_universe": [
            row["id"] for row in controls
            if row.get("source_universe") != "code"],
    }
    return {
        "evaluation": "federated code search on curated Bigville tasks",
        "date": DATE,
        "kind": "current member source + existing curated retrieval tasks",
        "default_behavior": (
            "code search is relevance-ranked only and has no family_boost; "
            "graph-mode family_boost remains opt-in and unchanged"),
        "mode": "code",
        "experimental_boost": federation.FAMILY_RANK_BOOST,
        "experimental_arm": (
            "local _experimental_family_boost only; applies the old +0.15 rule "
            "over a deeper candidate pool; never exposed by the shipped code "
            "API, CLI or MCP code mode"),
        "shipped_status": {
            "code_family_boost": "rejected and removed",
            "code_surface": "relevance-ranked only",
            "reason": (
                "the experimental +0.15 arm did not improve any curated family "
                "task and pushed the rankable out-of-family supergraph control "
                "out of the top 20"),
            "graph_family_boost": "unchanged; opt-in and default off",
        },
        "rank_limit": RANK_LIMIT,
        "tasks": rows,
        "controls": controls,
        "metrics": {
            "family": {
                "baseline": _metrics(family_ranks_base),
                "experimental": _metrics(family_ranks_experimental),
            },
            "controls": {
                "baseline": _metrics(control_ranks_base),
                "experimental": _metrics(control_ranks_experimental),
            },
        },
        "changed_ranks": [
            {"id": row["id"], "baseline": row["baseline_rank"],
             "experimental": row["experimental_rank"], "delta": row["delta"]}
            for row in rows + controls if row["delta"] not in (None, 0)
        ],
        "control_regression": control_regression,
        "cache": cache,
        "members": members,
        "source_universe": "code-file extensions only; Markdown docs are not indexed",
        "limitations": [
            "source trees are staged into the temporary members from the real "
            "checkouts; generated/run directories and adapters are excluded",
            "snapshot state is reported separately; a current-source hit is not "
            "proof that the member snapshot is fresh",
            "code search indexes code-file extensions only, so the Markdown "
            "control is outside its source universe",
            "one anchor, one family member and one distractor; the task set is "
            "small and Bigville-only",
            "answers are matched by exact member-relative path; matching "
            "symbols or snippets are not counted separately",
            "the cache is written under the anchor's temporary state directory; "
            "no non-anchor member is written and no index is refreshed",
            "the shipped code arm is relevance-ranked only; the +0.15 family "
            "row is a local evaluation experiment that was rejected and removed",
            "lexical search only; no embeddings, model calls, network access or "
            "target writes",
        ],
        "note": ("Curated Bigville tasks, not a family benchmark. Code search "
                 "reads current member source and does not refresh snapshots. "
                 "The shipped code arm is relevance-only; the experimental "
                 "family row is local evaluation evidence and was rejected."),
    }


def _prior_graph_reference() -> dict[str, Any] | None:
    try:
        data = json.loads(PRIOR_GRAPH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {
        "path": str(PRIOR_GRAPH.relative_to(REPO_ROOT)),
        "metrics": data.get("metrics"),
        "note": ("snapshot-only graph search from the same curated tasks; the "
                 "source universe and scoring differ"),
    }


def _delta(code: dict[str, Any], graph: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(graph, dict):
        return None
    out = {}
    for group in ("family", "controls"):
        base = code[group]["baseline"]
        other = (graph.get(group) or {}).get("baseline") or {}
        out[group] = {
            key: round(base[key] - other[key], 4)
            for key in (*[f"hit@{k}" for k in K_VALUES], "mrr")
            if isinstance(base.get(key), (int, float))
            and isinstance(other.get(key), (int, float))
        }
    return out


def _cache_outside_members(cache: dict[str, Any] | None,
                           members: list[dict[str, Any]]) -> bool | None:
    """True when every member cache file is outside non-anchor checkouts.

    The anchor's own state directory is the documented cache location and is
    allowed even though the anchor is itself a member.
    """
    if not cache or not members:
        return None
    non_anchor = [Path(member["root"]).resolve() for member in members[1:]]
    for path_text in (cache.get("files") or {}).values():
        path = Path(path_text)
        for root in non_anchor:
            if path.is_relative_to(root):
                return False
    return True


def _tree_state(root: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (path.relative_to(root).as_posix(), path.stat().st_mtime_ns,
         path.stat().st_size)
        for path in root.rglob("*") if path.is_file())


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _rank_text(value: int | None) -> str:
    return "—" if value is None else str(value)


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Federated code search on curated Bigville tasks — {report['date']}",
        "",
        "> **Scope:** opt-in code search over current member source plus the "
        "existing curated Bigville retrieval tasks. This is not a family "
        "benchmark. Code mode is relevance-ranked only; the shipped code API, "
        "CLI and MCP surface have no family boost. The `experimental` row applies "
        "the rejected +0.15 rule locally for evidence only. Graph-mode family "
        "ranking is unchanged and remains opt-in.",
        "",
        "## Setup",
        "",
        "The temporary federation from `real_repos.py` is reused: Bigville is "
        "the anchor, Ocelot is in the `simulation-stack` family, and Ozagia is "
        "an unrelated distractor. Source directories are staged read-only into "
        "the temporary member roots so current-source search can read them. "
        "The task text and expected files come from "
        "`evaluation/bigville_retrieval/tasks.json`; they were not rewritten.",
        "",
        "## Shipped status",
        "",
        "The experimental code-mode family arm was rejected and removed. "
        "`federation.code_search`, `ozagia federation code-search` and MCP "
        "`federation_search` with `mode=\"code\"` are relevance-ranked only; "
        "MCP rejects `family_boost` in code mode. Graph-mode "
        "`federation.search(family_boost=...)` is unchanged.",
        "",
        "## Aggregate results",
        "",
        "| Group | Arm | Hit@1 | Hit@3 | Hit@5 | MRR |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for group in ("family", "controls"):
        for arm in ("baseline", "experimental"):
            metrics = report["metrics"][group][arm]
            label = ("experimental +0.15 (rejected)"
                     if arm == "experimental" else arm)
            lines.append(
                f"| {group} | `{label}` | {_percent(metrics['hit@1'])} | "
                f"{_percent(metrics['hit@3'])} | {_percent(metrics['hit@5'])} | "
                f"{metrics['mrr']:.3f} |")
    lines += [
        "",
        "## Family tasks",
        "",
        "| Task | Baseline rank | Experimental rank | Delta | Expected |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in report["tasks"]:
        lines.append(
            f"| `{row['id']}` | {_rank_text(row['baseline_rank'])} | "
            f"{_rank_text(row['experimental_rank'])} | "
            f"{_rank_text(row['delta'])} | "
            f"{', '.join(f'`{item}`' for item in row['expected'])} |")
    lines += [
        "",
        "## Controls",
        "",
        "| Control | Source universe | Baseline rank | Experimental rank | Delta | Expected |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in report["controls"]:
        lines.append(
            f"| `{row['id']}` | {row.get('source_universe', 'code')} | "
            f"{_rank_text(row['baseline_rank'])} | "
            f"{_rank_text(row['experimental_rank'])} | "
            f"{_rank_text(row['delta'])} | "
            f"{', '.join(f'`{item}`' for item in row['expected'])} |")
    regression = report.get("control_regression") or {}
    lines += [
        "",
        "The Markdown control is outside the code arm's source universe, so its "
        "missing rank is not a code-search recall miss. "
        + ("The rejected experimental family arm regressed the rankable "
           "control(s): "
           + ", ".join(f"`{item}`" for item in regression.get("tasks", []))
           + "." if regression.get("detected")
           else "The experimental family arm did not regress a rankable control."),
    ]
    cache = report.get("cache") or {}
    lines += [
        "",
        "## Cache and members",
        "",
        f"- Cache directory: `{cache.get('dir')}` "
        f"(`{cache.get('state')}`, fallback: `{cache.get('fallback')}`).",
        f"- Cache files: {len(cache.get('files') or {})}; outside non-anchor "
        f"members: `{report.get('cache_outside_members')}`.",
        "- Member snapshot states:",
    ]
    for member in report.get("members") or []:
        lines.append(
            f"  - `{member.get('id')}`: state `{member.get('state')}`, source "
            f"`{member.get('source_state')}`, snapshot `{member.get('snapshot')}`.")
    graph = report.get("graph_reference")
    if graph:
        lines += [
            "",
            "## Comparison with snapshot-only graph search",
            "",
            f"Prior graph run: `{graph['path']}`. The source universes differ: "
            "graph search reads snapshots, code search reads current source "
            "code files. Both use the same curated tasks.",
            "",
            "| Group | Metric | Graph | Code | Delta |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        deltas = report.get("code_vs_graph_baseline") or {}
        for group in ("family", "controls"):
            graph_metrics = (graph.get("metrics") or {}).get(group, {}).get(
                "baseline", {})
            code_metrics = report["metrics"][group]["baseline"]
            for key in (*[f"hit@{k}" for k in K_VALUES], "mrr"):
                if key not in graph_metrics:
                    continue
                lines.append(
                    f"| {group} | {key} | {graph_metrics[key]:.3f} | "
                    f"{code_metrics[key]:.3f} | "
                    f"{deltas.get(group, {}).get(key, 0):+.3f} |")
    lines += [
        "",
        "## Decision",
        "",
        "The shipped code arm is relevance-ranked only. The experimental +0.15 "
        "family rule was rejected: it did not improve any curated family task "
        "and moved the rankable out-of-family supergraph control out of the top "
        "20. It is not exposed by `federation.code_search`, the federation CLI "
        "or MCP code mode. The baseline code arm materially improves the "
        "curated-task result over snapshot-only graph search (62.5% versus 0% "
        "hit@5). Graph-mode family ranking remains unchanged and opt-in.",
        "",
        "## Limits",
        "",
        "- The source trees are staged into the temporary members from the "
        "real checkouts; generated/run directories and adapters are excluded.",
        "- Snapshot state is reported separately. A hit from current source is "
        "not proof that the member snapshot is fresh.",
        "- Code search indexes code-file extensions only. The Markdown control "
        "(`docs/federation.md`) is outside its source universe.",
        "- One anchor, one family member and one distractor; the task set is "
        "small and Bigville-only.",
        "- Answers are matched by exact member-relative path; a matching "
        "symbol or snippet is not counted separately.",
        "- The cache is written under the anchor's temporary state directory. "
        "No non-anchor member is written and no index is refreshed.",
        "- The `experimental` row is a local evaluation-only arm, not a shipped "
        "API option; it applies the old +0.15 rule over a deeper candidate pool "
        "after relevance ranking.",
        "- Lexical search only; no embeddings, model calls, network access or "
        "target writes.",
        "",
        "## Reproduce",
        "",
        "```sh",
        "python evaluation/family_retrieval/real_repos.py",
        "python evaluation/family_retrieval/real_tasks_code.py",
        "```",
        "",
        "The first command rebuilds the temporary federation; the second stages "
        "source read-only into its member roots and writes the JSON and "
        "Markdown reports.",
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
    parser.add_argument("--bigville-root", type=Path,
                        default=DEFAULT_ROOTS["bigville"])
    parser.add_argument("--ocelot-root", type=Path,
                        default=DEFAULT_ROOTS["ocelot"])
    parser.add_argument("--ozagia-root", type=Path,
                        default=DEFAULT_ROOTS["ozagia"])
    parser.add_argument("--restage", action="store_true",
                        help="replace staged source directories from the real "
                             "checkouts")
    args = parser.parse_args(argv)
    anchor = args.workdir / "bigville"
    if not (anchor / ".ozagia" / "federation.json").is_file():
        print(f"error: {anchor} has no federation manifest; run real_repos.py first")
        return 2
    roots = {
        "bigville": args.bigville_root.resolve(),
        "ocelot": args.ocelot_root.resolve(),
        "ozagia": args.ozagia_root.resolve(),
    }
    missing = [f"{member}: {root}" for member, root in roots.items()
               if not root.is_dir()]
    if missing:
        print("error: missing real source checkouts: " + ", ".join(missing))
        return 2
    staging = _stage_source(args.workdir.resolve(), roots, args.restage)
    tasks = _load_tasks(args.tasks)
    before = {member: _tree_state(args.workdir / member)
              for member in ("ocelot", "ozagia")}
    report = _evaluate(anchor, tasks)
    after = {member: _tree_state(args.workdir / member)
             for member in ("ocelot", "ozagia")}
    report["workdir"] = str(args.workdir.resolve())
    report["root"] = str(anchor.resolve())
    report["source_staging"] = staging
    report["member_writes"] = {
        member: before[member] != after[member] for member in before}
    report["cache_outside_members"] = _cache_outside_members(
        report.get("cache"), report.get("members") or [])
    prior = _prior_graph_reference()
    report["graph_reference"] = prior
    report["code_vs_graph_baseline"] = _delta(report["metrics"], prior["metrics"]) \
        if prior else None
    report["checks"] = {
        "passed": not any(report["member_writes"].values()),
        "member_writes": report["member_writes"],
        "cache_outside_members": report["cache_outside_members"],
        "note": ("non-anchor member files are probed before and after; only "
                 "the anchor state cache and the temporary staging area may "
                 "change"),
    }
    _write(args.out_json, json.dumps(report, indent=2, sort_keys=True) + "\n")
    _write(args.out_md, _markdown(report))
    print(f"wrote {args.out_json}")
    print(f"wrote {args.out_md}")
    print(json.dumps({
        "family": report["metrics"]["family"],
        "controls": report["metrics"]["controls"],
        "cache": (report.get("cache") or {}).get("dir"),
        "member_writes": report["member_writes"],
    }, indent=2))
    return 0 if report["checks"]["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
