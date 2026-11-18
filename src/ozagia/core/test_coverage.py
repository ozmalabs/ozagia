"""Offline per-file test-link coverage views composed from test plans.

This reports graph links and plan selection only. It does not run tests and
does not claim executed or line coverage.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ozagia.core import testing

SCHEMA = "ozagia.testing.coverage.v1"


def report(root: str | Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Describe linked and selected tests for each changed file in ``plan``.

    Graph links are read only when the snapshot is fresh. A missing snapshot
    and a stale snapshot remain distinct in the report; neither is converted
    into an assertion that a subject has no tests.
    """
    root_path = Path(root).expanduser().resolve()
    graph_info = plan.get("graph")
    if not isinstance(graph_info, Mapping):
        graph_info = {}
    freshness = str(graph_info.get("freshness") or plan.get(
        "graph_freshness") or "unknown")
    linked: dict[str, set[str]] = {}
    if freshness == "fresh":
        verified, index = testing._load_graph(
            root_path, graph_info.get("path"),
            (plan.get("range") or {}).get("head_commit")
            if isinstance(plan.get("range"), Mapping) else None)
        freshness = str(verified.get("freshness") or "unknown")
        if index is not None:
            linked = {
                str(subject): set(map(str, tests))
                for subject, tests in (index.get("file_tests") or {}).items()
            }

    selected = set(map(str, plan.get("selected_files") or ()))
    subjects: list[dict[str, Any]] = []
    for raw_subject in plan.get("changed_files") or ():
        subject = str(raw_subject)
        links = sorted(linked.get(subject, ()))
        present = (root_path / subject).is_file()
        if not present:
            state = "missing_subject"
        elif freshness == "missing":
            state = "graph_missing"
        elif freshness == "stale":
            state = "graph_stale"
        elif freshness != "fresh":
            state = "graph_unknown"
        elif not links:
            state = "unlinked"
        else:
            state = "linked"
        subjects.append({
            "file": subject,
            "exists": present,
            "state": state,
            "linked_tests": links,
            "selected_tests": sorted(set(links) & selected),
        })

    return {
        "schema": SCHEMA,
        "root": str(root_path),
        "subjects": subjects,
        "counts": {
            "subjects": len(subjects),
            "missing_subjects": sum(not row["exists"] for row in subjects),
            "linked_subjects": sum(row["state"] == "linked" for row in subjects),
            "unlinked_subjects": sum(row["state"] == "unlinked" for row in subjects),
        },
        "graph": {"freshness": freshness, "path": graph_info.get("path")},
        "executed": False,
        "truth_assessed": False,
        "claim": "test-link and plan-selection view only; no test was executed",
        "not_assessed": [
            "test execution, pass status and line or branch coverage were not assessed",
            "graph links indicate a relationship, not proof that a test exercises the subject",
        ],
    }


__all__ = ["SCHEMA", "report"]
