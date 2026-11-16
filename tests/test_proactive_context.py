from __future__ import annotations

from ozagia.core.proactive_context import build_context, retrieve_proactive_context


class Memory:
    def __init__(self, responses, freshness="fresh"):
        self.responses = responses
        self.freshness = freshness
        self.calls = []

    def staleness(self):
        return {"freshness": self.freshness}

    def code_search(self, query, *, limit, include_snippet):
        self.calls.append((query, limit, include_snippet))
        return {"ok": True, "results": self.responses.get(query, [])[:limit]}


def hit(path, snippet="", line=None, symbols=None):
    return {"path": path, "snippet": snippet, "line": line,
            "symbols": symbols or []}


def test_fuses_queries_deduplicates_paths_and_retains_provenance():
    mem = Memory({
        "new task": [hit("src/a.py", "new", 2,
                         [{"name": "do_new", "line": 10}]),
                     hit("src/b.py", "b")],
        "older clue": [hit("src/a.py", "older", 5),
                       hit("src/c.py", "c")],
    })
    result = retrieve_proactive_context(mem, "new task", ["older clue"],
                                        single_query=False)

    assert [row["path"] for row in result["results"]] == [
        "src/a.py", "src/b.py", "src/c.py"]
    first = result["results"][0]
    assert first["provenance"] == ["current", "history:1"]
    assert first["snippet"] == "new" and first["line"] == 2
    assert "src/a.py:2 (current,history:1)" in result["context"]
    assert len([row for row in result["results"]
                if row["path"] == "src/a.py"]) == 1


def test_rendered_context_obeys_strict_budget_and_marks_truncation():
    mem = Memory({"task": [hit(f"src/{i}.py", "x" * 200) for i in range(8)]})
    result = retrieve_proactive_context(mem, "task", max_chars=91)
    assert len(result["context"]) <= 91
    assert result["truncated"] is True


def test_stale_snapshot_does_not_search():
    mem = Memory({}, freshness="stale")
    result = build_context(mem, "secret task", ["private history"])
    assert result["context"] == ""
    assert result["results"] == []
    assert result["reason"] == "snapshot freshness is stale"
    assert mem.calls == []
    assert "secret task" not in repr(result)
    assert "private history" not in repr(result)


def test_history_is_bounded_newest_first_and_return_does_not_echo_text():
    mem = Memory({})
    long_turn = "x" * 40
    result = retrieve_proactive_context(
        mem, "current", ["one", "two", "three"], max_history_turns=2,
        max_query_chars=12)
    assert [q for q, _, _ in mem.calls] == ["current\nthree\ntwo"]
    assert result["queries"] == ["current", "history:1", "history:2"]
    # Neither prompt nor the supplied history is returned for diagnostics.
    mem2 = Memory({})
    result2 = retrieve_proactive_context(
        mem2, "current", [long_turn], max_query_chars=12)
    assert long_turn not in repr(result2)
    assert mem2.calls[-1][0] == f"current\n{long_turn[-12:]}"


def test_default_mode_uses_one_search_for_current_and_history():
    mem = Memory({"task\nrecent": [hit("src/a.py", "snippet")]})
    result = retrieve_proactive_context(mem, "task", ["recent"])
    assert len(mem.calls) == 1
    assert mem.calls[0][0] == "task\nrecent"
    assert result["results"][0]["provenance"] == ["current", "history:1"]


def test_unknown_freshness_fails_closed():
    mem = Memory({"task": [hit("src/a.py")]}, freshness="unknown")
    result = build_context(mem, "task")
    assert result["context"] == "" and result["keys"] == []
    assert result["reason"] == "snapshot freshness is unknown"
    assert mem.calls == []


def test_headers_are_atomic_and_keys_only_cover_rendered_hits():
    first = hit("a.py", "z")
    second = hit("a_very_long_path.py", "z")
    mem = Memory({"task": [first, second]})
    header = "[ozagia] a.py (current)"
    result = build_context(mem, "task", budget_tokens=len(header) // 4 + 1)
    assert result["context"].startswith(header)
    assert "a_very_long_path.py" not in result["context"]
    assert result["keys"] == ["file:a.py"]
    assert len(result["context"]) <= (len(header) // 4 + 1) * 4


def test_search_symbol_objects_preserve_name_and_definition_line():
    mem = Memory({"task": [hit("src/a.py", "body", 4,
                                [{"name": "handler", "line": 19,
                                  "detail": "function"}])]})
    result = retrieve_proactive_context(mem, "task")
    assert result["results"][0]["symbols"] == [{"name": "handler", "line": 19}]
    assert "symbols: handler:19" in result["context"]


def test_build_context_exposes_paths_and_conservative_token_budget():
    mem = Memory({"task": [hit("src/a.py", "body")]})
    result = build_context(mem, "task", budget_tokens=20)
    assert result["keys"] == ["file:src/a.py"]
    assert len(result["context"]) <= 80
    assert result["freshness"] == "fresh"

