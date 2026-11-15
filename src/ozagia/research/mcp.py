"""Optional research tools for the Ozagia MCP server."""
from __future__ import annotations

OPERATIONS = {
    "init": ("Create an investigation.", {"question": {"type": "string"}, "mode": {"type": "string"}, "roots": {"type": "array", "items": {"type": "object"}}}, ["question"]),
    "context": ("Find cited local research context.", {"session_id": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer"}}, ["session_id", "query"]),
    "native_graph": ("Project the research graph and optionally evaluate a native DSL term.", {"session_id": {"type": "string"}, "query": {"type": "object"}, "env": {"type": "object"}}, ["session_id"]),
    "paper_preview": ("Read a selected paper into source-linked claim candidates without recording.", {"session_id": {"type": "string"}, "root_id": {"type": "string"}, "path": {"type": "string"}, "max_formula_candidates": {"type": "integer"}, "formula_offset": {"type": "integer"}, "latexml_path": {"type": "string"}}, ["session_id", "root_id", "path"]),
    "paper_ingest": ("Record a selected paper's claims and references in the research graph.", {"session_id": {"type": "string"}, "root_id": {"type": "string"}, "path": {"type": "string"}, "max_formula_candidates": {"type": "integer"}, "formula_offset": {"type": "integer"}, "latexml_path": {"type": "string"}}, ["session_id", "root_id", "path"]),
    "formula_parse_candidates": ("Optionally parse supplied LaTeX formulas with SymPy/Lark; returns unverified candidates without execution or recording.", {"session_id": {"type": "string"}, "formulas": {"type": "array", "maxItems": 256, "items": {"type": "string"}}}, ["session_id", "formulas"]),
    "formula_resolve": ("Use scoped research graph context to compare formula interpretations.", {"session_id": {"type": "string"}, "formula": {"type": "string"}, "scope_id": {"type": "string"}}, ["session_id", "formula", "scope_id"]),
    "formula_translate": ("Translate a supported formula into a native executable Term.", {"session_id": {"type": "string"}, "formula": {"type": "string"}, "domain_assumptions": {"type": "object"}}, ["session_id", "formula"]),
    "formula_model": ("Translate a formula and attach it as a reviewable claim model.", {"session_id": {"type": "string"}, "claim_id": {"type": "string"}, "formula": {"type": "string"}, "domain_assumptions": {"type": "object"}, "context_scope_id": {"type": "string"}, "bindings": {"type": "object"}, "assumption_ids": {"type": "array", "items": {"type": "string"}}, "residuals": {"type": "array", "items": {"type": "string"}}, "reviewer": {"type": "string"}, "review_note": {"type": "string"}}, ["session_id", "claim_id", "formula", "reviewer", "review_note"]),
    "model_candidate": ("Attach an explicit native Term model candidate to a sourced claim.", {"session_id": {"type": "string"}, "claim_id": {"type": "string"}, "term": {"type": "object"}, "bindings": {"type": "object"}, "assumption_ids": {"type": "array", "items": {"type": "string"}}, "residuals": {"type": "array", "items": {"type": "string"}}, "reviewer": {"type": "string"}, "review_note": {"type": "string"}, "provenance": {"type": "object"}}, ["session_id", "claim_id", "term", "residuals", "reviewer", "review_note"]),
    "record": ("Append a research record.", {"session_id": {"type": "string"}, "kind": {"type": "string"}, "fields": {"type": "object"}}, ["session_id", "kind", "fields"]),
    "link": ("Link two research records.", {"session_id": {"type": "string"}, "src": {"type": "string"}, "relation": {"type": "string"}, "tgt": {"type": "string"}, "fields": {"type": "object"}}, ["session_id", "src", "relation", "tgt"]),
    "inspect": ("Inspect a session or a record and its links.", {"session_id": {"type": "string"}, "node_id": {"type": "string"}, "transitive": {"type": "boolean"}}, ["session_id"]),
    "trace": ("Trace directed research derivations.", {"session_id": {"type": "string"}, "node_id": {"type": "string"}, "max_depth": {"type": "integer"}}, ["session_id", "node_id"]),
    "execute": ("Run explicit graph-bound native substrate terms.", {"session_id": {"type": "string"}, "node_id": {"type": "string"}, "env": {"type": "object"}, "max_depth": {"type": "integer"}, "max_nodes": {"type": "integer"}}, ["session_id", "node_id"]),
    "check_lean": ("Check generated Lean declarations with the local Lean executable.", {"session_id": {"type": "string"}, "node_id": {"type": "string"}}, ["session_id"]),
    "export_lean": ("Export explicit Boolean terms as Lean definitions.", {"session_id": {"type": "string"}, "node_id": {"type": "string"}}, ["session_id"]),
    "check": ("Check research records and provenance; does not certify truth.", {"session_id": {"type": "string"}}, ["session_id"]),
    "export": ("Export an auditable ledger and JSON bundle.", {"session_id": {"type": "string"}}, ["session_id"]),
}
TOOLS = [{"name": "research_" + name, "description": desc,
          "inputSchema": {"type": "object", "properties": props,
                          "required": required, "additionalProperties": False}}
         for name, (desc, props, required) in OPERATIONS.items()]


def research_tools():
    return TOOLS

def call_research_tool(name, args, workspace):
    from ozagia.research.service import ResearchService
    if not name.startswith("research_") or name[9:] not in OPERATIONS:
        raise ValueError("unknown research tool")
    return ResearchService(workspace).call(name[9:], args)
