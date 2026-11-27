"""Paired family-ranking check on real repository snapshots.

This script rebuilds read-only graph snapshots for three real checkouts into a
private work directory, declares a Bigville/Ocelot family in the temporary
anchor manifest, and runs the same frozen queries with ``family_boost`` off and
on. The anchor is Bigville; Ocelot is the declared family member; Ozagia is an
unrelated distractor and the only member with the control answer.

It is a real-snapshot evaluation of the family re-rank, but the query set is
small and constructed after reading the real file titles. It is not a
representative retrieval benchmark and cannot authorize a default change.
No target checkout is imported or written, no graph file is committed, and no
network or model call is made. Adapter indexers are deliberately not loaded:
``OZAGIA_CONFIG`` points at a neutral per-member configuration generated under
the work directory, so a target project's adapter module is never imported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from ozagia.core import federation

DATE = "2026-11-27"
K_VALUES = (1, 3, 5)
RANK_LIMIT = 20
TOP_HITS = 6
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WORKDIR = Path("/tmp/opencode/family-real")
DEFAULT_JSON = REPO_ROOT / "docs/results" / f"family_retrieval_real_{DATE}.json"
DEFAULT_MD = REPO_ROOT / "docs/results" / f"family_retrieval_real_{DATE}.md"

DEFAULT_ROOTS = {
    "bigville": Path("/applications/data/projects/bigville"),
    "ocelot": Path("/applications/data/projects/ocelot"),
    "ozagia": Path("/home/matt/ozmadev/ozagia"),
}

# Neutral per-member build configurations. They keep the repositories' own
# source/document layout where it matters but contain no [adapters] section and
# no target state-directory path. Bigville has no project config; its layout is
# indexed from the root with generated run artifacts skipped. Ocelot's source
# directories mirror its checked-in .ozagia.toml, minus adapters. Ozagia's
# source directories are the maintained code/evaluation trees.
CONFIGS = {
    "bigville": """[index]
source_dirs = ["."]
test_dirs = ["tests"]
skip_dirs = ["runs", "artifacts", "recordings", "scratch_rr", "scratchpad3", ".bigtmp"]
docs = ["README.md", "docs/**/*.md"]

[memory]
skip_dirs = []
""",
    "ocelot": """[index]
source_dirs = ["runners/dsl/src", "runners/dsl/tests", "domains", "domains_rs", "worlds", "substrate", "tools", "scripts"]
test_dirs = ["tests"]
skip_dirs = ["data", "var", "runs", "recordings", "scratch_rr", "scratchpad3", ".bigtmp", "evaluation", "benchmarks", "papers", "drafts", "domain_data", "semantic-review", "attestation"]
docs = ["README.md", "docs/**/*.md"]

[memory]
skip_dirs = [".venv-jsbsim-f16", "vizdoom"]
""",
    "ozagia": """[index]
source_dirs = ["src", "tests", "evaluation", "scripts", "tools", "examples", "addins"]
test_dirs = ["tests"]
skip_dirs = ["runs", "artifacts", "node_modules", "recordings"]
docs = ["README.md", "docs/**/*.md"]

[memory]
skip_dirs = []
""",
}

# The four family queries are deliberately near-ties: the Bigville answer
# matches enough query terms to stay within the bounded +0.15 rank boost of a
# stronger Ozagia distractor. The control has a decisive Ozagia answer. The
# wording follows the real document titles/paths inspected in the checkouts;
# no file content is invented.
QUERIES = [
    {
        "id": "bigville-scenario-coverage-near-tie",
        "kind": "family",
        "query": "bigville test coverage evidence audit simple scenario",
        "answer_member": "bigville",
        "answer_path": "docs/SIMPLE_SCENARIO_COVERAGE_AUDIT.md",
        "distractor_member": "ozagia",
        "distractor_path": "docs/results/bigville_test_coverage_audit_2026-11-27.md",
        "rationale": (
            "Bigville's independent scenario coverage audit is the expected "
            "answer; Ozagia's Bigville test-coverage audit is a cross-repo "
            "lexical competitor one term ahead."
        ),
    },
    {
        "id": "bigville-boundary-audit-near-tie",
        "kind": "family",
        "query": "bigville simple village boundary audit coverage evidence test",
        "answer_member": "bigville",
        "answer_path": "docs/SIMPLE_BOUNDARY_AUDIT.md",
        "distractor_member": "ozagia",
        "distractor_path": "docs/results/bigville_test_coverage_audit_2026-11-27.md",
        "rationale": (
            "Bigville's legacy world-side boundary audit is the expected "
            "answer; Ozagia's Bigville coverage audit matches more query terms "
            "and leads by one term in the lexical baseline."
        ),
    },
    {
        "id": "bigville-test-plan-near-tie",
        "kind": "family",
        "query": "bigville simple village test plan coverage audit results",
        "answer_member": "bigville",
        "answer_path": "docs/SIMPLE_VILLAGE_TEST_PLAN.md",
        "distractor_member": "ozagia",
        "distractor_path": "docs/results/bigville_test_coverage_audit_2026-11-27.md",
        "rationale": (
            "Bigville's staged village verification plan is the expected "
            "answer; Ozagia's Bigville coverage audit is a competitive "
            "cross-repo summary of the same evidence area."
        ),
    },
    {
        "id": "bigville-ocelot-integration-near-tie",
        "kind": "family",
        "query": "ocelot integration baseline workspace profile plan resident",
        "answer_member": "bigville",
        "answer_path": "docs/ocelot-integration-baseline.md",
        "distractor_member": "ozagia",
        "distractor_path": "docs/plans/2026-11-26_ocelot_workspace.md",
        "rationale": (
            "Bigville's supported Ocelot integration guide is the expected "
            "answer; Ozagia's Ocelot workspace plan is the stronger lexical "
            "match and an out-of-family competitor."
        ),
    },
    {
        "id": "ozagia-federation-control",
        "kind": "control",
        "query": "federated repository graphs discovery membership",
        "answer_member": "ozagia",
        "answer_path": "docs/federation.md",
        "distractor_member": None,
        "distractor_path": None,
        "rationale": (
            "The answer is an Ozagia document outside the declared family. A "
            "family hit is visible in the result list, so this checks that the "
            "arm does not disturb an out-of-family leading answer."
        ),
    },
]

_TARGET_STATE_FILES = (
    "graph.json",
    "federation.json",
    "tags.json",
    "tags.json.search.marshal",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_file(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _git_head(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else None


def _target_state(root: Path) -> dict[str, Any]:
    state = root / ".ozagia"
    return {
        "root": str(root),
        "head": _git_head(root),
        "files": {name: _stat_file(state / name) for name in _TARGET_STATE_FILES},
    }


def _write_config(workdir: Path, member: str) -> Path:
    path = workdir / "configs" / f"{member}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(CONFIGS[member], encoding="utf-8")
    return path


def _strip_snapshot_paths(snapshot: Path, rel_paths: set[str]) -> None:
    """Remove evaluation-only files from a temporary snapshot.

    The Ozagia member is also the repository that writes the reports. If a
    previous report is present when the snapshot is rebuilt, its Doc/File nodes
    would become part of the lexical corpus. Those files are evaluation output,
    not target source, so they are removed from the temporary snapshot. The
    real checkout is never changed.
    """
    if not rel_paths:
        return
    graph = json.loads(snapshot.read_text(encoding="utf-8"))
    drop_keys = {f"doc:{rel}" for rel in rel_paths}
    drop_keys |= {f"file:{rel}" for rel in rel_paths}
    nodes = [node for node in graph.get("nodes", [])
             if isinstance(node, dict) and node.get("key") not in drop_keys]
    dropped_ids = {
        node.get("id") for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("key") in drop_keys
        and node.get("id") is not None
    }
    graph["nodes"] = nodes
    graph["edges"] = [
        edge for edge in graph.get("edges", [])
        if isinstance(edge, dict)
        and edge.get("src") not in dropped_ids
        and edge.get("tgt") not in dropped_ids
    ]
    files = graph.get("files")
    if isinstance(files, dict):
        for rel in rel_paths:
            files.pop(rel, None)
    temporary = snapshot.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(graph, indent=2) + "\n", encoding="utf-8")
    temporary.replace(snapshot)


def _build_snapshot(member: str, root: Path, workdir: Path,
                    exclude_paths: set[str] | None = None) -> dict[str, Any]:
    state = workdir / member / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    snapshot = state / "graph.json"
    config = _write_config(workdir, member)
    env = os.environ.copy()
    for name in (
        "OZAGIA_ROOT",
        "OZAGIA_STATE_DIR",
        "OZAGIA_ADAPTERS",
        "OZAGIA_CONFIG",
    ):
        env.pop(name, None)
    env["OZAGIA_CONFIG"] = str(config)
    env["OZAGIA_LANGUAGE_AUTOINSTALL"] = "0"
    command = [
        sys.executable,
        "-m",
        "ozagia.core.memory",
        "build",
        "--root",
        str(root),
        "--out",
        str(snapshot),
        "--docs",
        "features",
        "--no-git",
        "--no-language-install",
    ]
    started = time.monotonic()
    proc = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    elapsed = round(time.monotonic() - started, 2)
    if proc.returncode != 0:
        raise RuntimeError(
            f"snapshot build failed for {member} (exit {proc.returncode}):\n"
            f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
        )
    try:
        build_report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"snapshot build for {member} did not return JSON: {exc}\n"
            f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
        ) from exc
    excluded = sorted(exclude_paths or ())
    _strip_snapshot_paths(snapshot, set(excluded))
    return {
        "root": str(root),
        "snapshot": str(snapshot),
        "config_path": str(config),
        "command": command,
        "seconds": elapsed,
        "build_report": build_report,
        "excluded_paths": excluded,
        "stderr_tail": proc.stderr[-2000:] if proc.stderr.strip() else "",
    }


def _scan_snapshot(path: Path) -> dict[str, Any]:
    graph = json.loads(path.read_text(encoding="utf-8"))
    nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
    types = Counter(str(node.get("type", "")) for node in nodes)
    keys = {str(node.get("key")) for node in nodes if node.get("key")}
    return {
        "nodes": len(nodes),
        "edges": len(graph.get("edges", [])),
        "types": dict(sorted(types.items())),
        "keys": keys,
        "sha256": _sha256(path),
    }


def _write_manifest(workdir: Path) -> dict[str, Any]:
    manifest = {
        "version": 1,
        "members": [
            {"path": str(workdir / "ocelot"), "id": "ocelot"},
            {"path": str(workdir / "ozagia"), "id": "ozagia"},
        ],
        "families": [{
            "id": "simulation-stack",
            "label": "Bigville/Ocelot simulation stack",
            "members": ["bigville", "ocelot"],
            "provenance": (
                "evaluation fixture declaration; real checkouts, no runtime "
                "dependency inferred"
            ),
        }],
        "links": [],
    }
    path = workdir / "bigville" / ".ozagia" / "federation.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


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


def _top_hits(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "qualified_key": hit["qualified_key"],
        "score": round(float(hit["score"]), 4),
        "rank_score": round(float(hit["rank_score"]), 4),
        "family_match": bool(hit["family_match"]),
    } for hit in result["results"][:TOP_HITS]]


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
    family_state: dict[str, Any] | None = None
    public_members: list[dict[str, Any]] = []
    with federation.without_reference_sync():
        for spec in QUERIES:
            answer = f"{spec['answer_member']}::doc:{spec['answer_path']}"
            distractor = (
                f"{spec['distractor_member']}::doc:{spec['distractor_path']}"
                if spec["distractor_member"] and spec["distractor_path"] else None
            )
            baseline = federation.search(
                anchor, spec["query"], limit=RANK_LIMIT)
            boosted = federation.search(
                anchor, spec["query"], limit=RANK_LIMIT, family_boost=True)
            if family_state is None:
                family_state = boosted["families"]
                public_members = boosted["members"]
            row = {
                "id": spec["id"],
                "kind": spec["kind"],
                "query": spec["query"],
                "rationale": spec["rationale"],
                "answer": answer,
                "answer_path": spec["answer_path"],
                "distractor": distractor,
                "distractor_path": spec["distractor_path"],
                "baseline": _query_metrics(baseline, answer),
                "family_boost": _query_metrics(boosted, answer),
                "baseline_distractor_rank": (
                    _rank(baseline, distractor) if distractor else None),
                "family_boost_distractor_rank": (
                    _rank(boosted, distractor) if distractor else None),
                "answer_family_match": next(
                    (hit["family_match"] for hit in boosted["results"]
                     if hit["qualified_key"] == answer), None),
                "top_baseline": _top_hits(baseline),
                "top_family_boost": _top_hits(boosted),
            }
            row["delta_rank"] = (
                None if row["baseline"]["rank"] is None
                or row["family_boost"]["rank"] is None
                else row["family_boost"]["rank"] - row["baseline"]["rank"])
            row["delta_reciprocal_rank"] = round(
                row["family_boost"]["reciprocal_rank"]
                - row["baseline"]["reciprocal_rank"], 4)
            first_family = next(
                (index for index, hit in enumerate(boosted["results"], start=1)
                 if hit["family_match"]), None)
            row["first_family_hit_rank_family_boost"] = first_family
            rows.append(row)

            checks.append({
                "id": spec["id"],
                "check": "expected answer is present in the boosted results",
                "passed": row["family_boost"]["rank"] is not None,
                "observed_rank": row["family_boost"]["rank"],
            })
            if spec["kind"] == "family":
                checks.append({
                    "id": spec["id"],
                    "check": "baseline distractor leads the expected answer",
                    "passed": (
                        row["baseline_distractor_rank"] is not None
                        and row["baseline"]["rank"] is not None
                        and row["baseline_distractor_rank"]
                        < row["baseline"]["rank"]),
                    "observed": {
                        "answer_rank": row["baseline"]["rank"],
                        "distractor_rank": row["baseline_distractor_rank"],
                    },
                })
                checks.append({
                    "id": spec["id"],
                    "check": "family boost ranks the expected answer first",
                    "passed": row["family_boost"]["rank"] == 1,
                    "observed_rank": row["family_boost"]["rank"],
                })
                checks.append({
                    "id": spec["id"],
                    "check": "expected answer is a family match in the boosted arm",
                    "passed": row["answer_family_match"] is True,
                })
                checks.append({
                    "id": spec["id"],
                    "check": "out-of-family distractor remains visible after re-rank",
                    "passed": row["family_boost_distractor_rank"] is not None,
                    "observed_rank": row["family_boost_distractor_rank"],
                })
            else:
                checks.append({
                    "id": spec["id"],
                    "check": "control answer is first in both arms",
                    "passed": (
                        row["baseline"]["rank"] == 1
                        and row["family_boost"]["rank"] == 1),
                    "observed_rank": [
                        row["baseline"]["rank"], row["family_boost"]["rank"]],
                })
                checks.append({
                    "id": spec["id"],
                    "check": "control metrics are unchanged by the family arm",
                    "passed": (
                        row["baseline"]["hit@1"] == row["family_boost"]["hit@1"]
                        and row["baseline"]["hit@3"]
                        == row["family_boost"]["hit@3"]
                        and row["baseline"]["hit@5"]
                        == row["family_boost"]["hit@5"]
                        and row["baseline"]["reciprocal_rank"]
                        == row["family_boost"]["reciprocal_rank"]),
                })
                checks.append({
                    "id": spec["id"],
                    "check": "control answer is not a family match",
                    "passed": row["answer_family_match"] is False,
                })

    if family_state is None:
        raise RuntimeError("no search was run")
    checks.append({
        "id": "family-declaration",
        "check": "the declared family resolves without errors",
        "passed": family_state.get("state") == "ok",
        "observed": family_state.get("state"),
    })
    expected_families = {
        "bigville": ["simulation-stack"],
        "ocelot": ["simulation-stack"],
        "ozagia": [],
    }
    checks.append({
        "id": "family-declaration",
        "check": "family membership maps to the anchor and declared member only",
        "passed": family_state.get("by_member") == expected_families,
        "observed": family_state.get("by_member"),
    })
    checks.append({
        "id": "family-declaration",
        "check": "the anchor is derived and is not repeated in members",
        "passed": (
            any(item.get("id") == "bigville"
                and item.get("identity_source") == "derived"
                for item in public_members)
            and all(str(item.get("root")) != str(anchor)
                    for item in public_members if item.get("id") != "bigville")),
    })

    family_rows = [row for row in rows if row["kind"] == "family"]
    control_rows = [row for row in rows if row["kind"] == "control"]
    return {
        "evaluation": "family-aware federation search on real repository snapshots",
        "date": DATE,
        "kind": "real repository snapshots with constructed frozen queries",
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
        "family_state": family_state,
        "members": public_members,
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
        "checks": {
            "passed": all(check["passed"] for check in checks),
            "checks": checks,
        },
        "decision": {
            "default": "off",
            "default_change_authorized": False,
            "reason": (
                "The mechanism changed the ranking on this sample, but all "
                "family queries are constructed near-ties and the sample is "
                "not a representative real-query benchmark. The default stays "
                "family_boost=False; changing it requires a representative "
                "paired evaluation with no regression and a meaningful gain."
            ),
        },
        "limits": [
            "One anchor, one declared family member and one unrelated "
            "distractor; four constructed family queries plus one control.",
            "The family queries were selected after reading real document "
            "titles and paths as near-ties inside the fixed +0.15 boost. They "
            "measure the mechanism in this regime, not the rate at which real "
            "queries are near-ties.",
            "Lexical search only: it matches node type, key and string attrs "
            "such as name/path/title. It does not read document bodies, use "
            "embeddings, call a model or fetch the network.",
            "Snapshots are built with --docs features --no-git. Adapter "
            "indexers are not loaded, so adapter-produced nodes are absent. "
            "The Ocelot and Bigville story is still real source/document data.",
            "Snapshots live in temporary member roots while their Repo attrs "
            "record the real checkouts. Federation therefore reports the "
            "snapshots as stale with 'snapshot root differs from checkout "
            "path'; search still loads and ranks them.",
            "Expected answers are Doc nodes. The matching File node can also "
            "appear separately and is not counted as the answer.",
            "The temporary build configurations are evaluation choices (for "
            "example, skipped generated run trees); they are recorded for "
            "reproduction but are not target-project configuration changes.",
            "The script's own JSON/Markdown reports are excluded from the "
            "temporary Ozagia snapshot when they lie under that checkout, so "
            "a repeated run does not index its previous report as corpus.",
            "No target checkout is imported or written; no graph file is "
            "committed; no commit, network or model call is made.",
        ],
    }


def _percent(value: float) -> str:
    return f"{100 * value:.1f}%"


def _markdown(report: dict[str, Any]) -> str:
    sample = report["sample_size"]
    lines = [
        f"# Family-aware federation retrieval on real snapshots — {report['date']}",
        "",
        "> **Scope:** real repository snapshots, constructed frozen queries. "
        "This is not a general retrieval benchmark and does not authorize a "
        "default change. The default remains `family_boost=False`.",
        "",
        "## Setup",
        "",
        "The script rebuilds read-only graph snapshots from three real "
        "checkouts into private temporary member directories. Bigville is the "
        "anchor; Ocelot is a declared family member; Ozagia is an unrelated "
        "distractor. The anchor's temporary manifest declares the "
        "`simulation-stack` family with Bigville and Ocelot. Adapter indexers "
        "are not loaded and no target project is imported.",
        "",
        "| Member | Checkout | HEAD | Snapshot nodes | Snapshot docs |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for member, build in report["builds"].items():
        types = build["build_report"].get("types", {})
        head = (report["target_checkouts"][member].get("head_before")
                or "unknown")
        lines.append(
            f"| `{member}` | `{build['root']}` | `{head[:12]}` | "
            f"{build['build_report'].get('nodes', 0)} | "
            f"{types.get('Doc', 0)} |")
    lines.extend([
        "",
        f"- Sample: {sample['queries']} queries "
        f"({sample['family_queries']} constructed family queries, "
        f"{sample['control_queries']} control).",
        f"- Arms: baseline (`family_boost=False`) and family-aware "
        f"(`family_boost=True`, additive rank boost {report['boost']:.2f}, "
        "capped at 1.0).",
        f"- Retrieval depth: top {report['rank_limit']} lexical hits; metrics at "
        "k=1, 3 and 5 plus MRR.",
        "- Snapshot state is expected to read `stale` because the temporary "
        "member roots differ from the recorded checkout roots; search still "
        "ranks the loaded snapshots.",
        "",
        "## Queries",
        "",
        "| Query | Kind | Expected answer (Bigville unless control) | "
        "Ozagia distractor |",
        "| --- | --- | --- | --- |",
    ])
    for row in report["queries"]:
        lines.append(
            f"| `{row['id']}` | {row['kind']} | `{row['answer_path']}` | "
            f"{('`' + row['distractor_path'] + '`') if row['distractor_path'] else '—'} |")
    lines.extend([
        "",
        "## Aggregate results",
        "",
        "| Group | Arm | Hit@1 | Hit@3 | Hit@5 | MRR |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ])
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
        "| Query | Baseline rank | Boosted rank | Δ rank | Distractor "
        "baseline → boosted | Baseline RR | Boosted RR |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in report["queries"]:
        distractor = (
            f"{row['baseline_distractor_rank']} → "
            f"{row['family_boost_distractor_rank']}"
            if row["distractor"] else "—")
        lines.append(
            f"| `{row['id']}` | {row['baseline']['rank']} | "
            f"{row['family_boost']['rank']} | {row['delta_rank']} | "
            f"{distractor} | {row['baseline']['reciprocal_rank']:.3f} | "
            f"{row['family_boost']['reciprocal_rank']:.3f} |")
    family_rows = [row for row in report["queries"] if row["kind"] == "family"]
    moved = sum(
        1 for row in family_rows
        if row["baseline"]["rank"] is not None
        and row["family_boost"]["rank"] == 1
        and row["baseline"]["rank"] > 1)
    control_unchanged = all(
        abs(value) < 1e-9
        for value in report["aggregate"]["control"]["delta"].values())
    checks = report["checks"]
    lines.extend([
        "",
        "## Findings",
        "",
        f"- The family arm moved {moved} of {len(family_rows)} family answers "
        "to rank 1. The four expected answers moved from ranks "
        f"{', '.join(str(row['baseline']['rank']) for row in family_rows)} to "
        "rank 1; the lexical distractor ranks were preserved and reordered, "
        "not filtered.",
        f"- The control answer is rank 1 in both arms and its metrics are "
        f"unchanged: `{control_unchanged}`.",
        f"- Checks passed: `{checks['passed']}`.",
        "- The gain is confined to a constructed near-tie regime: each "
        "family distractor is within one query term of the answer. This "
        "confirms the ranking mechanism on real snapshots, but it does not "
        "estimate how often real queries fall in that regime.",
        "",
        "## Decision",
        "",
        "The arm stays opt-in and the default remains `family_boost=False`. "
        "This run is evidence about the mechanism, not a default-change "
        "authorization: the query set is small and was selected after "
        "inspecting real file titles. A representative paired evaluation on "
        "real repositories and queries, with no regression and a meaningful "
        "gain, is still required.",
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
        ".venv/bin/python evaluation/family_retrieval/real_repos.py",
        "```",
        "",
        "The script rebuilds the three snapshots under "
        f"`{report['workdir']}` and writes this Markdown file plus the sibling "
        "JSON report. It does not write into the target checkouts.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument("--bigville-root", type=Path,
                        default=DEFAULT_ROOTS["bigville"])
    parser.add_argument("--ocelot-root", type=Path,
                        default=DEFAULT_ROOTS["ocelot"])
    parser.add_argument("--ozagia-root", type=Path,
                        default=DEFAULT_ROOTS["ozagia"])
    parser.add_argument("--out-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--out-md", type=Path, default=DEFAULT_MD)
    args = parser.parse_args(argv)

    roots = {
        "bigville": args.bigville_root.resolve(),
        "ocelot": args.ocelot_root.resolve(),
        "ozagia": args.ozagia_root.resolve(),
    }
    for member, root in roots.items():
        if not root.is_dir():
            parser.error(f"--{member}-root is not a directory: {root}")

    workdir = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    target_before = {member: _target_state(root)
                     for member, root in roots.items()}

    def _relative_output(path: Path, root: Path) -> str | None:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None

    builds: dict[str, Any] = {}
    snapshots: dict[str, Any] = {}
    for member, root in roots.items():
        excluded = {
            rel for rel in (
                _relative_output(args.out_json, root),
                _relative_output(args.out_md, root),
            ) if rel
        }
        build = _build_snapshot(member, root, workdir, excluded)
        scan = _scan_snapshot(Path(build["snapshot"]))
        raw_report = build["build_report"]
        build["raw_build_report"] = raw_report
        build["build_report"] = {
            **raw_report,
            "nodes": scan["nodes"],
            "edges": scan["edges"],
            "types": scan["types"],
        }
        build["snapshot_sha256"] = scan["sha256"]
        build["nodes"] = scan["nodes"]
        build["edges"] = scan["edges"]
        build["types"] = scan["types"]
        builds[member] = build
        snapshots[member] = scan

    manifest = _write_manifest(workdir)
    anchor = workdir / "bigville"

    answer_checks: list[dict[str, Any]] = []
    for spec in QUERIES:
        for role, member, rel in (
            ("answer", spec["answer_member"], spec["answer_path"]),
            ("distractor", spec["distractor_member"],
             spec["distractor_path"]),
        ):
            if not member or not rel:
                continue
            key = f"doc:{rel}"
            source = roots[member] / rel
            answer_checks.append({
                "id": spec["id"],
                "check": f"{role} file exists in the {member} checkout",
                "passed": source.is_file(),
                "path": str(source),
            })
            answer_checks.append({
                "id": spec["id"],
                "check": f"{role} Doc key exists in the {member} snapshot",
                "passed": key in snapshots[member]["keys"],
                "key": key,
            })

    report = _evaluate(anchor)
    target_after = {member: _target_state(root)
                    for member, root in roots.items()}

    target_writes: dict[str, bool] = {}
    for member in roots:
        target_writes[member] = target_before[member] != target_after[member]
    report["workdir"] = str(workdir)
    report["root"] = str(anchor)
    report["manifest"] = manifest
    report["builds"] = builds
    report["configs"] = {member: CONFIGS[member] for member in roots}
    report["target_checkouts"] = {
        member: {
            "root": str(root),
            "head_before": target_before[member]["head"],
            "head_after": target_after[member]["head"],
            "state_before": target_before[member]["files"],
            "state_after": target_after[member]["files"],
            "state_changed": target_writes[member],
        }
        for member, root in roots.items()
    }
    report["source_checks"] = {
        "passed": all(check["passed"] for check in answer_checks),
        "checks": answer_checks,
    }
    report["target_writes"] = {
        "detected": any(target_writes.values()),
        "by_member": target_writes,
        "note": (
            "Only the three temporary snapshot directories under the work "
            "directory are written. Target checkout state files are probed "
            "before and after; none may change."
        ),
    }
    report["checks"]["passed"] = (
        report["checks"]["passed"]
        and report["source_checks"]["passed"]
        and not report["target_writes"]["detected"]
    )
    no_write_checks = [{
        "id": "target-writes",
        "check": "no target checkout state file changed",
        "passed": not report["target_writes"]["detected"],
        "observed": target_writes,
    }]
    anchor_paths = {str(Path(item["path"]).resolve())
                    for item in manifest["members"]}
    report["checks"]["checks"].append({
        "id": "family-declaration",
        "check": "the temporary manifest does not list the anchor in members",
        "passed": str(anchor.resolve()) not in anchor_paths,
        "observed": sorted(anchor_paths),
    })
    report["checks"]["passed"] = (
        report["checks"]["passed"]
        and str(anchor.resolve()) not in anchor_paths
    )
    report["checks"]["checks"].extend(no_write_checks)
    report["checks"]["checks"].extend(answer_checks)

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(_markdown(report) + "\n", encoding="utf-8")

    print(json.dumps({
        "json": str(args.out_json),
        "markdown": str(args.out_md),
        "workdir": str(workdir),
        "checks_passed": report["checks"]["passed"],
        "target_writes_detected": report["target_writes"]["detected"],
        "aggregate": report["aggregate"]["all"],
    }, indent=2))
    return 0 if report["checks"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
