"""Deterministic contracts for task context ranking and provider budgets."""
from __future__ import annotations

import subprocess
from pathlib import Path

from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.ozma.context import task_context


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True,
                   capture_output=True, text=True)


def test_task_terms_put_relevant_checkout_file_in_context(tmp_path: Path):
    (tmp_path / "src" / "checkout").mkdir(parents=True)
    (tmp_path / "src" / "misc").mkdir(parents=True)
    (tmp_path / "src" / "checkout" / "payment.py").write_text(
        '"""Checkout payment flow."""\n\n'
        'def charge_cart(cart, gateway):\n'
        '    """Charge a cart and produce a receipt."""\n'
        '    return gateway.charge(cart.total)\n', encoding="utf-8")
    (tmp_path / "src" / "misc" / "formatting.py").write_text(
        '"""Formatting helpers."""\n\n'
        'def format_value(value):\n    return str(value)\n', encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "eval@example.test")
    _git(tmp_path, "config", "user.name", "Context Eval")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "fixture")

    dump = tmp_path / ".ozagia" / "graph.json"
    build_index(tmp_path, dump, include_docs="none")
    memory = DevMemory(dump=dump,
                       journal=dump.with_name("journal.jsonl"), root=tmp_path)

    context = memory.context("trace checkout payment charge and receipt",
                             max_items=5, budget_tokens=500)
    paths = [item["attrs"].get("path") for item in context["items"]]
    assert paths[0] == "src/checkout/payment.py"
    assert "src/misc/formatting.py" not in paths
    assert context["budget"]["estimated_tokens"] <= 500


def test_provider_clips_snippets_in_rank_order_and_reports_exact_char_budget(
    tmp_path: Path,
):
    class Memory:
        _staleness_memo = None
        raw_repo_root = tmp_path

        def staleness(self):
            return {"freshness": "unknown", "stale": False,
                    "verification_errors": []}

        def context(self, task, **kwargs):
            assert kwargs["max_items"] == 4
            assert kwargs["max_chars"] == 12
            assert kwargs["budget_tokens"] == 3
            return {"items": [
                {"node": {"label": "first"},
                 "snippet": {"text": "12345678", "estimated_tokens": 2}},
                {"node": {"label": "second"},
                 "snippet": {"text": "abcdefghij", "estimated_tokens": 3}},
            ]}

    result = task_context(Memory(), "task", budget_tokens=3, max_items=4,
                          allow_stale=True)
    assert result["ok"]
    assert [item["node"]["label"] for item in result["items"]] == ["first", "second"]
    assert [item["snippet"]["text"] for item in result["items"]] == [
        "12345678", "abcd"]
    assert result["items"][1]["snippet"]["truncated_by_provider"] is True
    assert result["budget"]["snippet_chars"] == 12
    assert sum(len(item["snippet"]["text"]) for item in result["items"]) == 12
