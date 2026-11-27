"""Paired offline check of opt-in family-aware federation search.

The script creates three temporary snapshots (anchor, family member and an
unrelated distractor), writes an anchor federation manifest with one declared
family, runs the same frozen queries with ``family_boost`` off and on, and
writes a JSON plus Markdown report.

This is a fixture-level mechanism check, not a retrieval benchmark. The family
queries are constructed with a one-term lexical gap that the bounded boost is
expected to close; the control query verifies that a decisive lexical result is
not disturbed. No member checkout is written, no adapter is imported, and no
model or network call is made.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from ozagia.core import federation

DATE = "2026-11-27"
K_VALUES = (1, 3, 5)
RANK_LIMIT = 20
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JSON = REPO_ROOT / "docs/results" / f"family_retrieval_{DATE}.json"
DEFAULT_MD = REPO_ROOT / "docs/results" / f"family_retrieval_{DATE}.md"

# Each family query has seven terms. The family answer matches five; the
# distractor matches six. The 1/7 lexical gap is smaller than the bounded
# 0.15 rank boost, so the paired run checks the re-rank rather than a
# particular model of relevance.
FAMILY_QUERIES = [
    {
        "id": "cache-eviction",
        "query": "cache eviction policy warm tier ceiling clock",
        "family_key": "doc:family-cache",
        "family_title": "cache eviction policy warm tier notes",
        "distractor_key": "doc:distractor-cache",
        "distractor_title": "cache eviction policy warm tier ceiling notes",
    },
    {
        "id": "ledger-close",
        "query": "ledger reconcile journal audit close period window",
        "family_key": "doc:family-ledger",
        "family_title": "ledger reconcile journal audit close notes",
        "distractor_key": "doc:distractor-ledger",
        "distractor_title": "ledger reconcile journal audit close period notes",
    },
    {
        "id": "deploy-rollout",
        "query": "deploy rollout canary region weight health probe",
        "family_key": "doc:family-deploy",
        "family_title": "deploy rollout canary region weight notes",
        "distractor_key": "doc:distractor-deploy",
        "distractor_title": "deploy rollout canary region weight health notes",
    },
    {
        "id": "search-shards",
        "query": "search index shard merge segment latency vector",
        "family_key": "doc:family-search",
        "family_title": "search index shard merge segment notes",
        "distractor_key": "doc:distractor-search",
        "distractor_title": "search index shard merge segment latency notes",
    },
]

CONTROL_QUERY = {
    "id": "telemetry-control",
    "kind": "control",
    "query": "telemetry span sampler tail export budget quota",
    "answer_member": "distractor",
    "answer_key": "doc:distractor-telemetry",
    "title": "telemetry span sampler tail export budget quota guide",
}


def _snapshot(root: Path, name: str,
              docs: list[tuple[str, str]] | None = None) -> None:
    """Write a minimal member snapshot; no project checkout is created."""
    root.mkdir(parents=True, exist_ok=True)
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    nodes: list[dict[str, Any]] = [{
        "id": 0, "type": "Repo", "key": "repo:",
        "attrs": {"name": name, "root": str(root), "head": "fixture"},
    }]
    for index, (key, title) in enumerate(docs or [], start=1):
        nodes.append({"id": index, "type": "Doc", "key": key,
                      "attrs": {"title": title, "path": "docs/notes.md"}})
    (state / "graph.json").write_text(json.dumps({
        "version": 2, "nodes": nodes, "edges": [],
    }), encoding="utf-8")


def _build_fixture(workspace: Path) -> Path:
    anchor = workspace / "anchor"
    family = workspace / "family-member"
    distractor = workspace / "distractor"
    _snapshot(anchor, "anchor")
    _snapshot(family, "family-member", [
        (spec["family_key"], spec["family_title"]) for spec in FAMILY_QUERIES])
    _snapshot(distractor, "distractor", [
        (spec["distractor_key"], spec["distractor_title"]) for spec in FAMILY_QUERIES
    ] + [(CONTROL_QUERY["answer_key"], CONTROL_QUERY["title"])])
    manifest = {
        "version": 1,
        "members": [
            {"path": str(family), "id": "family-member"},
            {"path": str(distractor), "id": "distractor"},
        ],
        "links": [],
        "families": [{
            "id": "shared-stack",
            "label": "Shared stack",
            "members": ["anchor", "family-member"],
            "provenance": "evaluation fixture declaration",
        }],
    }
    (anchor / ".ozagia" / "federation.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return anchor


def _rank(result: dict[str, Any], qualified_key: str) -> int | None:
    for index, hit in enumerate(result["results"], start=1):
        if hit["qualified_key"] == qualified_key:
            return index
    return None


def _query_metrics(result: dict[str, Any], qualified_key: str) -> dict[str, Any]:
    rank = _rank(result, qualified_key)
    metrics: dict[str, Any] = {"rank": rank}
    for k in K_VALUES:
        metrics[f"hit@{k}"] = rank is not None and rank <= k
    metrics["reciprocal_rank"] = 1.0 / rank if rank else 0.0
    return metrics


def _aggregate(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    if not rows:
        return {"queries": 0}
    total = len(rows)
    out: dict[str, Any] = {"queries": total}
    for k in K_VALUES:
        out[f"hit@{k}"] = round(
            sum(bool(row[arm][f"hit@{k}"]) for row in rows) / total, 4)
    out["mrr"] = round(
        sum(row[arm]["reciprocal_rank"] for row in rows) / total, 4)
    return out


def _delta(rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = _aggregate(rows, "baseline")
    boosted = _aggregate(rows, "family_boost")
    return {
        key: round(boosted[key] - baseline[key], 4)
        for key in (*[f"hit@{k}" for k in K_VALUES], "mrr")
    }


def _evaluate(anchor: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for spec in FAMILY_QUERIES:
        answer = f"family-member::{spec['family_key']}"
        distractor = f"distractor::{spec['distractor_key']}"
        baseline = federation.search(anchor, spec["query"], limit=RANK_LIMIT)
        boosted = federation.search(
            anchor, spec["query"], limit=RANK_LIMIT, family_boost=True)
        row = {
            "id": spec["id"],
            "kind": "family",
            "query": spec["query"],
            "answer": answer,
            "lexical_competitor": distractor,
            "baseline": _query_metrics(baseline, answer),
            "family_boost": _query_metrics(boosted, answer),
        }
        rows.append(row)
        checks.append({
            "id": spec["id"],
            "check": "baseline ranks the family answer below the distractor",
            "passed": row["baseline"]["rank"] == 2,
            "observed_rank": row["baseline"]["rank"],
        })
        checks.append({
            "id": spec["id"],
            "check": "family boost ranks the family answer first",
            "passed": row["family_boost"]["rank"] == 1,
            "observed_rank": row["family_boost"]["rank"],
        })
        checks.append({
            "id": spec["id"],
            "check": "both hits remain visible after the re-rank",
            "passed": len(boosted["results"]) == 2,
            "observed_hits": len(boosted["results"]),
        })

    answer = f"{CONTROL_QUERY['answer_member']}::{CONTROL_QUERY['answer_key']}"
    baseline = federation.search(anchor, CONTROL_QUERY["query"], limit=RANK_LIMIT)
    boosted = federation.search(
        anchor, CONTROL_QUERY["query"], limit=RANK_LIMIT, family_boost=True)
    control = {
        "id": CONTROL_QUERY["id"],
        "kind": "control",
        "query": CONTROL_QUERY["query"],
        "answer": answer,
        "lexical_competitor": None,
        "baseline": _query_metrics(baseline, answer),
        "family_boost": _query_metrics(boosted, answer),
    }
    rows.append(control)
    checks.append({
        "id": CONTROL_QUERY["id"],
        "check": "control answer is first in both arms",
        "passed": (control["baseline"]["rank"] == 1
                   and control["family_boost"]["rank"] == 1),
        "observed_rank": [control["baseline"]["rank"],
                          control["family_boost"]["rank"]],
    })
    checks.append({
        "id": CONTROL_QUERY["id"],
        "check": "control metrics are unchanged by the family arm",
        "passed": (control["baseline"]["hit@1"] == control["family_boost"]["hit@1"]
                   and control["baseline"]["reciprocal_rank"]
                   == control["family_boost"]["reciprocal_rank"]),
    })

    family_rows = [row for row in rows if row["kind"] == "family"]
    control_rows = [row for row in rows if row["kind"] == "control"]
    for row in rows:
        row["delta_rank"] = (
            None if row["baseline"]["rank"] is None
            or row["family_boost"]["rank"] is None
            else row["family_boost"]["rank"] - row["baseline"]["rank"])
        row["delta_reciprocal_rank"] = round(
            row["family_boost"]["reciprocal_rank"]
            - row["baseline"]["reciprocal_rank"], 4)

    return {
        "evaluation": "family-aware federation search",
        "date": DATE,
        "kind": "fixture-level mechanism check",
        "default_behavior": "family_boost=False; lexical ranking unchanged",
        "boost": federation.FAMILY_RANK_BOOST,
        "rank_limit": RANK_LIMIT,
        "sample_size": {
            "queries": len(rows),
            "family_queries": len(family_rows),
            "control_queries": len(control_rows),
        },
        "arms": {
            "baseline": {"family_boost": False},
            "family_boost": {"family_boost": True},
        },
        "aggregate": {
            "all": {
                "baseline": _aggregate(rows, "baseline"),
                "family_boost": _aggregate(rows, "family_boost"),
                "delta": _delta(rows),
            },
            "family": {
                "baseline": _aggregate(family_rows, "baseline"),
                "family_boost": _aggregate(family_rows, "family_boost"),
                "delta": _delta(family_rows),
            },
            "control": {
                "baseline": _aggregate(control_rows, "baseline"),
                "family_boost": _aggregate(control_rows, "family_boost"),
                "delta": _delta(control_rows),
            },
        },
        "queries": rows,
        "fixture_checks": {
            "passed": all(check["passed"] for check in checks),
            "checks": checks,
        },
        "decision": {
            "default": "off",
            "reason": "Fixture-level gain only; the default stays off until a "
                      "paired evaluation on real repositories and queries shows "
                      "no regression and a meaningful gain.",
        },
        "limits": [
            "Three temporary synthetic snapshots and five frozen queries; this "
            "is a fixture-level mechanism check, not evidence of general "
            "retrieval benefit.",
            "The four family queries were constructed with a 1/7 lexical gap "
            "that the bounded +0.15 boost can close. They verify the mechanism, "
            "not that families improve real queries.",
            "The control query is already lexically decisive; it checks that the "
            "family arm does not disturb it.",
            "Lexical search only: no embeddings, parsers, adapters, target "
            "repositories, network access or model calls.",
            "The default remains family_boost=False. Enabling it requires a "
            "separate paired evaluation on real repositories and queries.",
        ],
    }


def _percent(value: float) -> str:
    return f"{100 * value:.1f}%"


def _markdown(report: dict[str, Any]) -> str:
    sample = report["sample_size"]
    lines = [
        f"# Family-aware federation retrieval — {report['date']}",
        "",
        "> **Scope:** fixture-level mechanism check. This is not evidence of a "
        "general retrieval benefit and is not a benchmark claim. The default "
        "remains `family_boost=False`.",
        "",
        "## Setup",
        "",
        "The evaluation script builds three temporary snapshots under a private "
        "temporary directory: an anchor, a declared family member containing the "
        "answers, and an unrelated distractor with a competitive lexical match. "
        "No project checkout is imported or written and no graph files are "
        "committed.",
        "",
        f"- Sample: {sample['queries']} queries "
        f"({sample['family_queries']} constructed family queries, "
        f"{sample['control_queries']} control).",
        f"- Arms: baseline (`family_boost=False`) and family-aware "
        f"(`family_boost=True`, additive rank boost {report['boost']:.2f}, "
        "capped at 1.0).",
        f"- Retrieval depth: top {report['rank_limit']} lexical hits; metrics at "
        "k=1, 3 and 5 plus MRR.",
        "",
        "## Aggregate results",
        "",
        "| Group | Arm | Hit@1 | Hit@3 | Hit@5 | MRR |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for group, title in (("all", "all queries"), ("family", "family queries"),
                         ("control", "control query")):
        data = report["aggregate"][group]
        for arm in ("baseline", "family_boost"):
            metrics = data[arm]
            if not metrics.get("queries"):
                continue
            lines.append(
                f"| {title} | `{arm}` | {_percent(metrics['hit@1'])} | "
                f"{_percent(metrics['hit@3'])} | {_percent(metrics['hit@5'])} | "
                f"{metrics['mrr']:.3f} |")
            title = ""
    lines.extend([
        "",
        "## Per-query results",
        "",
        "| Query | Kind | Baseline rank | Family rank | Δ rank | Baseline RR | "
        "Family RR |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in report["queries"]:
        lines.append(
            f"| `{row['id']}` | {row['kind']} | {row['baseline']['rank']} | "
            f"{row['family_boost']['rank']} | {row['delta_rank']} | "
            f"{row['baseline']['reciprocal_rank']:.3f} | "
            f"{row['family_boost']['reciprocal_rank']:.3f} |")
    checks = report["fixture_checks"]
    family_rows = [row for row in report["queries"] if row["kind"] == "family"]
    moved = sum(
        1 for row in family_rows
        if row["baseline"]["rank"] == 2 and row["family_boost"]["rank"] == 1)
    control_unchanged = all(
        abs(value) < 1e-9
        for value in report["aggregate"]["control"]["delta"].values())
    lines.extend([
        "",
        "## Findings",
        "",
        f"- The family arm moved {moved} of {len(family_rows)} constructed "
        "family answers from rank 2 to rank 1, so hit@1 and MRR improve on the "
        "family subset.",
        f"- The control query is rank 1 in both arms and its metrics are "
        f"unchanged: `{control_unchanged}`. The gain therefore comes entirely "
        "from the constructed family queries.",
        "- Non-family hits remain in the results; the arm re-ranks and never "
        "filters.",
        f"- Fixture assumptions passed: `{checks['passed']}`.",
        "",
        "## Decision",
        "",
        "The arm stays opt-in and the default remains `family_boost=False`. A gain "
        f"on {len(family_rows)} constructed queries is not enough evidence to "
        "change default behavior; the gate requires a paired evaluation on real "
        "repositories and queries with no regression and a meaningful gain.",
        "",
        "## Limits",
        "",
    ])
    lines.extend(f"- {limit}" for limit in report["limits"])
    lines.extend([
        "",
        "## Reproduce",
        "",
        "```sh",
        ".venv/bin/python evaluation/family_retrieval/family_retrieval.py",
        "```",
        "",
        "The script writes this Markdown file and the sibling JSON report. "
        "Everything else is created and removed under a temporary directory.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--out-md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="ozagia-family-retrieval-") as raw:
        anchor = _build_fixture(Path(raw) / "workspace")
        with federation.without_reference_sync():
            report = _evaluate(anchor)

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(_markdown(report) + "\n", encoding="utf-8")

    print(json.dumps({
        "json": str(args.out_json),
        "markdown": str(args.out_md),
        "fixture_checks_passed": report["fixture_checks"]["passed"],
        "aggregate": report["aggregate"]["all"],
    }, indent=2))
    return 0 if report["fixture_checks"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
