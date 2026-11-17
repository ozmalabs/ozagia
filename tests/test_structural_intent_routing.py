"""Offline paired checks for high-confidence structural prompt routing."""
from __future__ import annotations

from ozagia.core.proactive_context import build_context
from ozagia.core.structural_intent import classify_structural_intent


POSITIVE = (
    ("Who calls parse_config?", {"kind": "callers", "target": "parse_config"}),
    ("What does build_index call?", {"kind": "callees", "target": "build_index"}),
    ("Show callers of DevMemory.stats", {"kind": "callers", "target": "DevMemory.stats"}),
    ("Which tests cover parse_config?", {"kind": "tests", "target": "parse_config"}),
    ("Where is DevMemory defined?", {"kind": "resolve", "target": "DevMemory"}),
    ("Find the definition of parse_config",
     {"kind": "resolve", "target": "parse_config"}),
    ("Show the public API of src/ozagia/core/memory.py",
     {"kind": "outline", "target": "src/ozagia/core/memory.py"}),
    ("Show the public surface of pkg/mod.py",
     {"kind": "outline", "target": "pkg/mod.py"}),
    ("Outline module pkg/mod.py",
     {"kind": "outline", "target": "pkg/mod.py"}),
)

NEGATIVE = (
    "Please call me when the build finishes.",
    "What tests should I write for this change?",
    "Where did I define my notes yesterday?",
    "Show the public API design docs about module systems.",
    "What does this shell script call through bash?",
    "Can you add a caller to this function?",
    "The test coverage report is too large.",
    "Outline a plan for the deployment.",
)


class _Memory:
    def __init__(self, freshness: str = "fresh"):
        self.freshness = freshness
        self.search_calls = 0

    def staleness(self, *, force=False):
        return {"freshness": self.freshness}

    def code_search(self, *_args, **_kwargs):
        self.search_calls += 1
        raise AssertionError("structural prompt must route directly")


def test_paired_structural_intent_precision_and_coverage():
    true_positive = sum(
        classify_structural_intent(prompt) == expected
        for prompt, expected in POSITIVE)
    false_positive = sum(classify_structural_intent(prompt) is not None
                         for prompt in NEGATIVE)
    assert true_positive == len(POSITIVE)
    assert false_positive == 0


def test_fresh_structural_prompt_routes_to_permissioned_tool_without_graph_data():
    memory = _Memory()
    result = build_context(memory, "Who calls parse_config?",
                           structural_tool_available=True)
    assert result["freshness"] == "fresh"
    assert result["structural_intent"] == {
        "kind": "callers", "target": "parse_config"}
    assert '"kind":"callers","target":"parse_config"' in result["context"]
    assert "read permission" in result["context"]
    assert result["keys"] == []
    assert result["results"] == []
    assert memory.search_calls == 0


def test_structural_prompt_does_not_disclose_data_when_stale_or_tool_unavailable():
    stale = build_context(_Memory("stale"), "Which tests cover parse_config?",
                          structural_tool_available=True)
    unavailable = build_context(_Memory(), "Outline module pkg/mod.py",
                                structural_tool_available=False)
    assert stale["context"] == "" and stale["keys"] == []
    assert stale["freshness"] == "stale"
    assert unavailable["context"] == "" and unavailable["keys"] == []
    assert unavailable["reason"] == "permissioned structural tool is unavailable"

