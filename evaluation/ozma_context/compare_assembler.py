"""Opt-in, offline four-arm adapter experiment against a local Ozma checkout.

Requires the Ozma backend importable from --ozma-backend and an indexed,
fresh fixture root. It never calls a model or the network.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from ozagia.core.memory import DevMemory
from ozagia.integrations.ozma.context import task_context

TASK = "Explain the payment flow and how the checkout handles an empty cart."
EXPECTED = (
    "src/checkout/service.py",
    "src/checkout/cart.py",
    "src/billing/gateway.py",
)


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _record(name: str, text: str, seconds: float, budget: int) -> dict:
    hits = [path for path in EXPECTED if path in text]
    return {
        "arm": name,
        "context_chars": len(text),
        "estimated_context_tokens": _tokens(text),
        "budget_tokens": budget,
        "budget_respected_by_char_estimate": _tokens(text) <= budget,
        "expected_paths_named": hits,
        "expected_path_recall": len(hits) / len(EXPECTED),
        "elapsed_seconds_diagnostic": round(seconds, 6),
    }


def run(root: Path, backend: Path, budgets: list[int]) -> dict:
    sys.path.insert(0, str(backend.resolve()))
    try:
        from ozma.context.assembler import FitLevel, assemble
        from ozma.context.classifier import Classification, Intent
        from ozma.context.store import SessionStore
    except ImportError as exc:
        raise RuntimeError(
            f"cannot import Ozma backend at {backend}: {exc}") from exc

    snapshot = root / ".ozagia" / "graph.json"
    if not snapshot.is_file():
        raise ValueError(f"missing indexed snapshot: {snapshot}")
    memory = DevMemory(dump=snapshot,
                       journal=snapshot.with_name("journal.jsonl"), root=root)
    freshness = memory.staleness().get("freshness")
    if freshness != "fresh":
        raise ValueError(f"fixture snapshot must be fresh; got {freshness!r}")

    store = SessionStore(root / ".ozagia" / "assembler-session.sqlite")
    classification = Classification(
        intent=Intent.NEW_TASK, confidence=1.0, clarity=3,
        file_refs=[], symbol_refs=[], is_pivot=False,
    )
    rows = []
    try:
        for budget in budgets:
            # Plain arm: only the same user task, with no retrieved context.
            rows.append(_record("plain", "", 0.0, budget))

            started = time.perf_counter()
            provided = task_context(memory, TASK, budget_tokens=budget)
            elapsed = time.perf_counter() - started
            if not provided.get("ok"):
                raise ValueError(f"provider refused snapshot: {provided.get('snapshot')}. "
                                 "Start with OZAGIA_ROOT set to the fixture root.")
            provided_text = "\n".join(
                f"{item.get('path', '')}\n{(item.get('snippet') or {}).get('text', '')}"
                for item in provided.get("items", [])
            ) if provided.get("ok") else ""
            rows.append(_record("ozagia", provided_text, elapsed, budget))

            for name, enabled in (("ozma", False), ("combined", True)):
                started = time.perf_counter()
                assembled = assemble(
                    classification, TASK, store, fit=FitLevel.MINIMAL,
                    budget_tokens=budget, repo_root=root,
                    use_relevance=False, use_ozagia=enabled,
                    ozagia_budget_tokens=budget,
                )
                elapsed = time.perf_counter() - started
                rows.append(_record(
                    name, assembled.full_context, elapsed, budget))
    finally:
        store.close()

    return {
        "experiment": "ozma-assembler-context-fixture-v1",
        "task": TASK,
        "root": str(root.resolve()),
        "backend": str(backend.resolve()),
        "snapshot_freshness": freshness,
        "budgets": budgets,
        "arms": ["plain", "ozagia", "ozma", "combined"],
        "rows": rows,
        "limitations": [
            "Context selection and path-name recall only; no task completion claim.",
            "Token counts are character-based estimates and budgets are not equal-cost controls.",
            "Ozma assembler behavior can vary by checkout revision and optional dependencies.",
            "No model call or network request is made by this experiment.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path,
                        help="freshly indexed fixture checkout")
    parser.add_argument("--ozma-backend", required=True, type=Path,
                        help="local Ozma backend directory containing ozma/")
    parser.add_argument("--budgets", default="100,300")
    args = parser.parse_args()
    budgets = [int(value) for value in args.budgets.split(",") if value]
    if not budgets or any(value <= 0 for value in budgets):
        parser.error("--budgets must contain positive comma-separated integers")
    print(json.dumps(run(args.root, args.ozma_backend, budgets), indent=2,
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
