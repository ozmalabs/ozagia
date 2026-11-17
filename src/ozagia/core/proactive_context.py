"""Bounded, freshness-aware context retrieval for an agent turn.

This module is a small orchestration layer over :meth:`DevMemory.code_search`.
It does not call a model or persist prompts. The caller supplies the current
prompt and (optionally) a short list of prior user turns.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

DEFAULT_MAX_CHARS = 4000
DEFAULT_HISTORY_TURNS = 2
DEFAULT_HISTORY_CHARS = 1200
DEFAULT_RESULTS_PER_QUERY = 8
RRF_K = 60


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) > limit:
        text = text[-limit:]
    return text


def _queries(prompt: str, history: Iterable[str], *, history_turns: int,
             history_chars: int) -> list[tuple[str, str]]:
    """Return the current task and bounded prior turns, newest first."""
    out: list[tuple[str, str]] = []
    current = _bounded_text(prompt, history_chars)
    if current:
        out.append(("current", current))
    prior: list[str] = []
    if history_turns > 0 and history is not None:
        for item in history:
            text = _bounded_text(item, history_chars)
            if text:
                prior.append(text)
    for i, text in enumerate(reversed(prior[-history_turns:]), 1):
        out.append((f"history:{i}", text))
    return out


def _render_results(results: list[dict[str, Any]], budget: int
                    ) -> dict[str, Any]:
    """Render ranked code candidates under a strict character budget."""
    blocks: list[str] = []
    rendered_keys: list[str] = []
    used = 0
    truncated = False
    for item in results:
        provenance = ",".join(item["provenance"])
        line = f":{item['line']}" if isinstance(item.get("line"), int) else ""
        symbols = ", ".join(
            f"{symbol['name']}:{symbol['line']}"
            if isinstance(symbol.get("line"), int) else symbol["name"]
            for symbol in item["symbols"][:4])
        header = f"[ozagia] {item['path']}{line} ({provenance})"
        if symbols:
            header += f" | symbols: {symbols}"
        snippet = item.get("snippet") or ""
        block = header + (f"\n{snippet}" if snippet else "")
        prefix = "\n\n" if blocks else ""
        remaining = budget - used - len(prefix)
        if remaining <= 0 or len(header) > remaining:
            truncated = True
            continue
        if len(block) > remaining:
            # Keep the citation/provenance header atomic; clip only source.
            space = remaining - len(header)
            block = header + ("\n" + snippet[:space - 1] if space > 1 else "")
            truncated = True
        blocks.append(prefix + block)
        rendered_keys.append(f"file:{item['path']}")
        used += len(prefix) + len(block)
        if truncated:
            break
    return {"context": "".join(blocks), "rendered_keys": rendered_keys,
            "truncated": truncated or len(results) > len(blocks)}


def retrieve_proactive_context(
    memory: Any,
    prompt: str,
    history: Iterable[str] = (),
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_history_turns: int = DEFAULT_HISTORY_TURNS,
    max_query_chars: int = DEFAULT_HISTORY_CHARS,
    results_per_query: int = DEFAULT_RESULTS_PER_QUERY,
    single_query: bool = True,
) -> dict[str, Any]:
    """Retrieve compact code context for a prompt, with optional prior turns.

    By default the prompt and history form one search query. With separate
    searches, the current prompt is weighted above history and results are
    fused by reciprocal rank. Candidates are deduplicated by repository path
    and rendered under a strict character budget. A stale or unverifiable
    snapshot is never used because graph ranking metadata may be stale.

    Returned fields are ``context``, ``results``, ``freshness``, ``queries``
    and ``truncated``. ``results`` retain path, line, symbols, fused rank
    score, and query provenance for downstream diagnostics.
    """
    budget = max(0, int(max_chars))
    history_turns = max(0, int(max_history_turns))
    query_chars = max(0, int(max_query_chars))
    per_query = max(0, int(results_per_query))
    try:
        freshness_info = memory.staleness(force=True)
        freshness = str(freshness_info.get("freshness") or "unknown")
    except Exception as exc:  # freshness checks must not break the caller
        freshness_info = {"error": f"{type(exc).__name__}: {exc}"}
        freshness = "unknown"

    empty = {"context": "", "results": [], "freshness": freshness,
             "freshness_info": freshness_info, "queries": [],
             "truncated": False, "rendered_keys": []}
    if freshness != "fresh":
        empty["reason"] = f"snapshot freshness is {freshness}"
        return empty
    if budget == 0 or per_query == 0:
        return empty

    query_rows = _queries(prompt, history, history_turns=history_turns,
                          history_chars=query_chars)
    empty["queries"] = [source for source, _ in query_rows]
    if not query_rows:
        empty["reason"] = "empty prompt and history"
        return empty

    # The daemon defaults to one search so a turn cannot queue several cold
    # code-search/index operations behind its graph lock.
    candidates: dict[str, dict[str, Any]] = {}
    if single_query:
        search_rows = [("+".join(source for source, _ in query_rows),
                        "\n".join(query for _, query in query_rows),
                        [source for source, _ in query_rows], 1.0)]
    else:
        search_rows = [(source, query, [source],
                        1.0 if source == "current" else
                        0.55 / int(source.split(":")[1]))
                       for source, query in query_rows]
    for _search_source, query, provenance_rows, weight in search_rows:
        try:
            response = memory.code_search(query, limit=per_query,
                                          include_snippet=True)
        except Exception as exc:  # retrieval is advisory
            empty["search_error"] = f"{type(exc).__name__}: {exc}"
            continue
        if not isinstance(response, dict) or response.get("ok") is False:
            continue
        rows = response.get("results")
        if not isinstance(rows, list):
            continue
        for rank, row in enumerate(rows, 1):
            if not isinstance(row, dict) or not isinstance(row.get("path"), str):
                continue
            path = row["path"]
            candidate = candidates.setdefault(path, {
                "path": path, "score": 0.0, "provenance": [],
                "line": None, "snippet": "", "symbols": [],
            })
            candidate["score"] += weight / (RRF_K + rank)
            for source in provenance_rows:
                if source not in candidate["provenance"]:
                    candidate["provenance"].append(source)
            if not candidate["snippet"] and isinstance(row.get("snippet"), str):
                candidate["snippet"] = row["snippet"].strip()
                candidate["line"] = row.get("line")
            for symbol in row.get("symbols") or []:
                if isinstance(symbol, str):
                    normalized = {"name": symbol, "line": None}
                elif isinstance(symbol, dict) and isinstance(symbol.get("name"), str):
                    normalized = {"name": symbol["name"],
                                  "line": symbol.get("line")}
                else:
                    continue
                if normalized not in candidate["symbols"]:
                    candidate["symbols"].append(normalized)

    results = sorted(candidates.values(),
                     key=lambda row: (-row["score"], row["path"]))
    empty["results"] = results
    empty.update(_render_results(results, budget))
    return empty


def build_context(memory: Any, prompt: str, history: Iterable[str] = (), *,
                  budget_tokens: int = 400,
                  max_history_turns: int = DEFAULT_HISTORY_TURNS,
                  results_per_query: int = DEFAULT_RESULTS_PER_QUERY,
                  single_query: bool = True,
                  structural_tool_available: bool = False,
                  infra_share: float = 0.45,
                  infra_results: int = 6,
                  runtime_share: float = 0.2,
                  ) -> dict[str, Any]:
    """Stable token-budget API for daemon/plugin integrations.

    Code search and opt-in infrastructure facts share one strict character
    budget. Infrastructure facts are compact citations and may use at most
    ``infra_share`` of the budget while code results exist; leftovers return to
    code. Four characters per estimated token is used as a conservative cap,
    not a tokenizer count. Raw prompt/history text is never included in the
    result.
    """
    tokens = max(0, int(budget_tokens))
    budget = tokens * 4
    history_list = list(history)
    from ozagia.core.structural_intent import (
        classify_structural_intent, structural_guidance)
    structural_intent = classify_structural_intent(prompt)
    if structural_intent is not None:
        try:
            freshness_info = memory.staleness(force=True)
            freshness = str(freshness_info.get("freshness") or "unknown")
        except Exception as exc:  # freshness checks must not break the caller
            freshness_info = {"error": f"{type(exc).__name__}: {exc}"}
            freshness = "unknown"
        empty = {
            "context": "", "results": [], "freshness": freshness,
            "freshness_info": freshness_info, "queries": ["current"],
            "truncated": False, "rendered_keys": [], "keys": [],
            "estimated_tokens": 0, "token_estimate_method": "estimated_chars_div_4",
            "structural_intent": structural_intent,
            "infra": {"results": [], "edges": [], "related": [],
                      "ambiguous": [], "matched_term_count": 0},
            "runtime": {"items": [], "truncated": False},
            "allocation": {"budget_chars": budget, "runtime_chars": 0,
                           "infra_chars": 0, "code_chars": 0,
                           "infra_share": min(1.0, max(0.0, float(infra_share)))},
        }
        if freshness != "fresh":
            empty["reason"] = f"snapshot freshness is {freshness}"
            return empty
        if not structural_tool_available:
            empty["reason"] = "permissioned structural tool is unavailable"
            return empty
        guidance = structural_guidance(structural_intent)
        empty["context"] = guidance[:budget]
        empty["estimated_tokens"] = (len(empty["context"]) + 3) // 4
        empty["allocation"]["code_chars"] = len(empty["context"])
        return empty
    result = retrieve_proactive_context(
        memory, prompt, history_list, max_chars=budget,
        max_history_turns=max_history_turns,
        results_per_query=results_per_query, single_query=single_query)
    result["token_estimate_method"] = "estimated_chars_div_4"
    if result.get("freshness") != "fresh" or budget <= 0:
        result["keys"] = result.get("rendered_keys", [])
        result["estimated_tokens"] = (len(result.get("context", "")) + 3) // 4
        return result
    recent = history_list[-max_history_turns:] if max_history_turns > 0 else []
    runtime: dict[str, Any] = {"context": "", "items": [], "truncated": False}
    try:
        from ozagia.core.runtime import runtime_context
        runtime = runtime_context(
            memory, prompt, history=recent,
            max_chars=min(400, max(0, int(budget * max(0.0, float(runtime_share))))),
        )
    except Exception:
        # Runtime config/state is advisory; never break a hint on it.
        runtime = {"context": "", "items": [], "truncated": False}
    runtime_text = runtime.get("context", "") if isinstance(runtime, dict) else ""
    available = budget
    if runtime_text:
        available = max(0, budget - len(runtime_text) - 2)
        rendered_code = _render_results(result.get("results", []), available)
        result["context"] = rendered_code["context"]
        result["rendered_keys"] = rendered_code["rendered_keys"]
        result["truncated"] = bool(rendered_code["truncated"])
    # Only opt-in indexed infrastructure nodes participate. Include structural
    # facts and citations, never raw infrastructure values.
    from ozagia.core.infra_query import context as infra_context
    share = min(1.0, max(0.0, float(infra_share)))
    infra_budget = int(available * share)
    infra = infra_context(memory, prompt, history=recent,
                          max_chars=infra_budget,
                          limit=max(1, int(infra_results)))
    code_context = result.get("context", "")
    final_context = code_context
    final_keys = list(result.get("rendered_keys", []))
    infra_chars = 0
    code_chars = len(code_context)
    truncated = bool(result.get("truncated"))
    chosen_infra = infra
    if infra["context"] and not code_context:
        # No code result rendered, so the infrastructure hint may use the
        # whole remaining budget instead of leaving the reserved share unused.
        chosen_infra = infra_context(memory, prompt, history=recent,
                                     max_chars=available,
                                     limit=max(1, int(infra_results)))
        final_context = chosen_infra["context"]
        final_keys = list(chosen_infra["keys"])
        infra_chars = len(final_context)
        code_chars = 0
        truncated = bool(chosen_infra["truncated"])
    elif infra["context"]:
        remaining = max(0, available - len(infra["context"]) - 2)
        rendered_code = _render_results(result.get("results", []), remaining)
        if rendered_code["context"]:
            final_context = infra["context"] + "\n\n" + rendered_code["context"]
            final_keys = infra["keys"] + rendered_code["rendered_keys"]
            infra_chars = len(infra["context"])
            code_chars = len(rendered_code["context"])
            truncated = (bool(infra["truncated"])
                         or bool(rendered_code["truncated"]) or truncated)
        else:
            # The code headers cannot fit after the infrastructure hint; give
            # the budget to infrastructure rather than returning a stub.
            chosen_infra = infra_context(memory, prompt, history=recent,
                                         max_chars=available,
                                         limit=max(1, int(infra_results)))
            final_context = chosen_infra["context"]
            final_keys = list(chosen_infra["keys"])
            infra_chars = len(final_context)
            code_chars = 0
            truncated = bool(chosen_infra["truncated"]) or bool(code_context)
    if runtime_text:
        final_context = runtime_text + ("\n\n" + final_context
                                        if final_context else "")
        final_keys = []  # runtime lines are config/state, not graph keys
        truncated = truncated or bool(runtime.get("truncated"))
    result["context"] = final_context
    result["rendered_keys"] = final_keys
    result["keys"] = final_keys
    result["truncated"] = truncated
    result["infra"] = {
        "results": chosen_infra["results"], "edges": chosen_infra["edges"],
        "related": chosen_infra["related"],
        "ambiguous": chosen_infra["ambiguous"],
        "matched_term_count": chosen_infra["matched_term_count"],
    }
    result["runtime"] = {
        "items": runtime.get("items", []) if isinstance(runtime, dict) else [],
        "truncated": bool(runtime.get("truncated")) if isinstance(runtime, dict) else False,
    }
    result["allocation"] = {
        "budget_chars": budget, "runtime_chars": len(runtime_text),
        "infra_chars": infra_chars, "code_chars": code_chars,
        "infra_share": share,
    }
    result["estimated_tokens"] = (len(final_context) + 3) // 4
    return result


__all__ = ["build_context", "retrieve_proactive_context"]
