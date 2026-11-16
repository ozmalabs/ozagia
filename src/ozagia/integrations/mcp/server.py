"""ozagia.integrations.mcp.server — stdio MCP server over the dev-memory snapshot.

An existing coding agent (OpenCode, Claude Code, Cursor, any MCP client) gets
the repository's graph memory as tools. The agent decides; the tools only
answer structural questions and record decisions/verification receipts.

`dev_seed` and `dev_rule` are listed only when the snapshot has `Seed` (resp.
`Rule`) nodes, which come from an adapter indexer. Adapters can add tools
(`Registry.add_mcp_tool`, see `ozagia.adapters`).

It is deliberately stdlib-only (no `mcp` package dependency) and READ-ONLY over
the graph, with one small append-only journal for decisions and verifications.
The graph itself is built by `ozagia.core.memory`.

Run (stdio):

    .venv/bin/python -m ozagia.integrations.mcp.server

Environment:
    OZAGIA_DUMP     snapshot path (default .ozagia/graph.json)
    OZAGIA_JOURNAL  journal path  (default .ozagia/journal.jsonl)
    OZAGIA_AUTOBUILD  "1" to build the snapshot on first start if absent
    OZAGIA_ROOT     checkout the tools act on (default: the current Git top level)
    OZAGIA_SURFACE  "full" (default) or "graph" (the find/node/expand/impact/
                    edit/create/check/note verbs); `--surface` overrides it
"""
from __future__ import annotations

import argparse
import atexit
import importlib
import json
import os
import re
import signal
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from ozagia.adapters import registry
from ozagia.config import repo_root, state_path
from ozagia.core.memory import (
    DevMemory,
    build_index,
)

SERVER_NAME = "ozagia"
BRIEF_URI = "ozagia://brief"
SERVER_VERSION = "0.1.0"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
PROTOCOL_VERSION = "2025-06-18"
# Tool surfaces: "full" (the dev_* tools) or "graph" (the eight verbs of
# `ozagia.integrations.mcp.graph_surface`). Adapter tools are in both.
SURFACES = ("full", "graph")

# JSON-RPC error codes.
_E_PARSE = -32700
_E_INVALID_REQUEST = -32600
_E_METHOD_NOT_FOUND = -32601
_E_INVALID_PARAMS = -32602
_E_INTERNAL = -32603
# Largest Content-Length framed message the stdio reader accepts.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024


# --------------------------------------------------------------------------- #
# tool schemas
# --------------------------------------------------------------------------- #
def _schema(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


_TOOLS: list[dict[str, Any]] = [
    {
        "name": "dev_context",
        "description": (
            "Build a cited context pack for a development task. Use this FIRST "
            "when starting a task in this repo: it returns the relevant seed / "
            "rule / file / symbol / test / doc nodes with file paths and line "
            "ranges, so you can read the actual source rather than guess. The "
            "pack is selected mechanically from the repo graph; it is not a "
            "decision about what to change."),
        "inputSchema": _schema({
            "task": {"type": "string",
                     "description": "Natural-language task or question."},
            "max_items": {"type": "integer", "default": 8,
                          "description": "Maximum context items (default 8)."},
            "mode": {"type": "string", "enum": ["auto", "outline", "full"],
                     "default": "auto",
                     "description": "auto: full top anchors, outlines for the rest."},
            "budget_tokens": {"type": "integer", "default": 6000,
                              "description": "Estimated token budget."},
        }, ["task"]),
    },
    {
        "name": "dev_brief",
        "description": (
            "Pre-load a compact repo brief: layout, build/test commands, key "
            "docs, recent commits, working-tree changes, known failures, and "
            "(optionally) a task context pack. Call it once at session start "
            "or when you need cheap orientation without reading many files."),
        "inputSchema": _schema({
            "task": {"type": "string",
                     "description": "Optional task to append as a context pack."},
            "budget_tokens": {"type": "integer", "default": 4000},
            "write": {"type": "boolean", "default": False,
                      "description": "Write .ozagia/brief.md too."},
        }),
    },
    {
        "name": "dev_repo_map",
        "description": (
            "Ranked, token-budgeted repo map (tree-sitter tags + personalized "
            "PageRank). Use it when you need the shape of the codebase rather "
            "than full snippets: full outlines for the hottest files, public "
            "symbols for the middle, a listing for the tail. Task-named seeds/"
            "files become focus files."),
        "inputSchema": _schema({
            "task": {"type": "string", "default": ""},
            "budget_tokens": {"type": "integer", "default": 1500},
            "focus_files": {"type": "array", "items": {"type": "string"}},
            "force_refresh": {"type": "boolean", "default": False},
            "scip_path": {"type": "string",
                          "description": "Optional SCIP JSON export for precise edges."},
        }),
    },
    {
        "name": "dev_impact",
        "description": (
            "Transitive dependents of a file over the tree-sitter reference "
            "graph (who references symbols defined here). Use before changing "
            "a widely used module."),
        "inputSchema": _schema({
            "file": {"type": "string"},
            "depth": {"type": "integer", "default": 2},
            "scip_path": {"type": "string"},
        }, ["file"]),
    },
    {
        "name": "dev_architecture",
        "description": (
            "Deterministic architecture summary: hubs, reference cycles "
            "(Tarjan SCC), coupling, module boundaries, dead-code candidates "
            "(heuristic) and test/source ratio. File-level and syntactic; use "
            "LSP/SCIP for compiler-accurate answers."),
        "inputSchema": _schema({
            "top": {"type": "integer", "default": 15},
        }),
    },
    {
        "name": "dev_code_search",
        "description": (
            "Lexical hybrid code search: BM25 over paths/symbols/source lines, "
            "reranked by repo-map PageRank via reciprocal rank fusion. Use it "
            "to locate candidates, then dev_context/dev_repo_map to expand and "
            "LSP/SCIP for precise navigation."),
        "inputSchema": _schema({
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 20},
            "include_snippet": {"type": "boolean", "default": True},
        }, ["query"]),
    },
    {
        "name": "dev_scip",
        "description": (
            "Import a SCIP JSON export (from `scip print --json`) and return "
            "normalized definitions/references, repo-map edges and per-file "
            "counts. Real .scip protobuf requires protobuf + scip_pb2; absent "
            "bindings are refused honestly."),
        "inputSchema": _schema({
            "path": {"type": "string"},
            "summary_only": {"type": "boolean", "default": False},
        }, ["path"]),
    },
    {
        "name": "dev_search",
        "description": (
            "Search the repo graph by name/path/title/description. Returns "
            "node ids, types and labels. Use dev_file/dev_symbol/dev_seed for "
            "details, or dev_node for attrs and edges."),
        "inputSchema": _schema({
            "text": {"type": "string"},
            "types": {"type": "array", "items": {"type": "string"},
                      "description": "Optional type filter, e.g. ['Seed','Rule']."},
            "limit": {"type": "integer", "default": 20},
        }, ["text"]),
    },
    {
        "name": "dev_file",
        "description": (
            "Everything the graph knows about one file: symbols, tests, "
            "imports/imported_by, docs, known failures and recent commits."),
        "inputSchema": _schema({
            "path": {"type": "string",
                     "description": "Repo-relative path, e.g. domains/teach.py."},
        }, ["path"]),
    },
    {
        "name": "dev_symbol",
        "description": (
            "Find a function/class/struct/enum by name or qualname. Returns "
            "its file, line range, docstring, source snippet, tests and graph "
            "neighbours."),
        "inputSchema": _schema({
            "name": {"type": "string",
                     "description": "Exact or partial symbol name."},
        }, ["name"]),
    },
    {
        "name": "dev_outline",
        "description": (
            "Compact structural view of a file/symbol/seed/doc: signatures, "
            "docstring first lines, imports and test names, with line "
            "citations and no bodies. Use it to pre-load context cheaply; "
            "edit the real file, never this view."),
        "inputSchema": _schema({
            "target": {"type": "string",
                       "description": "File path, symbol, seed id, or doc path."},
        }, ["target"]),
    },
    {
        "name": "dev_query",
        "description": (
            "Look inside a JSON, YAML or TOML file by JSON pointer (RFC 6901) "
            "instead of loading it in Python or jq: an object's keys with "
            "type and size, an array's length and a slice of its items, or a "
            "scalar's value, with its line (JSON) and node key "
            "(json:<path>#<pointer>). Output is capped at 8 kB. YAML needs "
            "the iac-yaml extra; TOML is read-only."),
        "inputSchema": _schema({
            "path": {"type": "string",
                     "description": "Repo-relative file, or a key such as "
                                    "json:results/run.json#/runs/0."},
            "pointer": {"type": "string",
                        "description": "JSON pointer, e.g. /runs/0/score; "
                                       "empty or # is the root."},
            "format": {"type": "string", "enum": ["json", "yaml", "toml"],
                       "description": "Default: from the file suffix."},
            "offset": {"type": "integer", "default": 0,
                       "description": "0-based index of the first array "
                                      "item or object key shown."},
            "limit": {"type": "integer",
                      "description": "Items shown (default 20 array "
                                     "items, 50 object keys)."},
        }, ["path"]),
    },
    {
        "name": "dev_ast",
        "description": (
            "The Python AST graph for a file: structural paths "
            "(e.g. body[3].body[0].value), node types, line spans and "
            "previews. Use these paths as `target` for generic replace_node / "
            "insert_before_node / insert_after_node / delete_node / "
            "wrap_node ops."),
        "inputSchema": _schema({
            "file": {"type": "string", "description": "Repo-relative .py file."},
            "node": {"type": "string",
                     "description": "Optional AST path to show as a subtree."},
            "max_nodes": {"type": "integer", "default": 200},
        }, ["file"]),
    },
    {
        "name": "dev_edit",
        "description": (
            "Apply a GRAPH-ADDRESSED edit script. The ops are semantic, not a "
            "text diff: Python (replace_symbol, replace_body, insert_import, "
            "replace_node, insert_before_node, insert_after_node, delete_node, "
            "wrap_node, ...), Markdown (section ops), JSON (path ops). Preview "
            "by default; "
            "set apply=true to write atomically (with a backup and journal "
            "entry). A raw diff/patch is refused; `diff_view` is output only."),
        "inputSchema": _schema({
            "file": {"type": "string",
                     "description": "Repo-relative .py, .md, .json, .rs or .lua path."},
            "ops": {"type": "array", "items": {"type": "object"},
                    "description": "Ordered semantic operations."},
            "apply": {"type": "boolean", "default": False},
            "base_sha256": {"type": "string",
                            "description": "Optional optimistic-concurrency check."},
            "cst_graph": {"type": "object",
                          "description": "Optional synthetic/post-parsed CstNode graph for .rs/.lua when tree-sitter is unavailable."},
            "task": {"type": "string",
                     "description": "Optional journal task label."},
            "diagnostics": {"type": "boolean", "default": False,
                            "description": "set diagnostics=true to wait up to 8 s for language-server diagnostics on the edited file. Only after an applied write; skipped when no server is available. Ignored in preview."},
        }, ["file", "ops"]),
    },
    {
        "name": "dev_span_edit",
        "description": (
            "Replace an exact text span in a file. The span is located in the "
            "graph (the enclosing symbol/section/chunk key is returned), the "
            "edit is refused if it breaks the file's parse, applied atomically "
            "with a backup when apply=true, the graph is updated, and the "
            "result lists the graph delta and broken references (missing "
            "imported names, call arity mismatches). Preview by default."),
        "inputSchema": _schema({
            "file": {"type": "string", "description": "Repo-relative path."},
            "old_string": {"type": "string",
                           "description": "Exact text to replace; must be "
                                          "unique unless replace_all=true."},
            "new_string": {"type": "string",
                           "description": "Replacement text."},
            "replace_all": {"type": "boolean", "default": False},
            "apply": {"type": "boolean", "default": False},
            "base_sha256": {"type": "string",
                            "description": "Optional optimistic-concurrency check."},
            "task": {"type": "string",
                     "description": "Optional journal task label."},
        }, ["file", "old_string", "new_string"]),
    },
    {
        "name": "dev_key_edit",
        "description": (
            "Replace the WHOLE text of one node, named by its key: a symbol "
            "or test (sym:path::Name, test:path::test_x), a Markdown or TeX "
            "section (doc:path.md#section-slug as reads and outlines show it; "
            "#parent/child is accepted too), a "
            "line range (text:path#L10-L42) or a JSON value "
            "(json:path.json#/a/0). base_sha256 is the sha256 of the node's "
            "current text (node sha256 in region replies; the file's sha256 "
            "only when the node is the whole file); a missing, stale or "
            "file-only one is refused with the node's current text and its "
            "node sha256, so retry with that. Parse guard, backup, journal and graph update as "
            "dev_span_edit. Preview by default; apply=true writes."),
        "inputSchema": _schema({
            "key": {"type": "string",
                    "description": "Node key: sym:, test:, doc:<file>#<section>, "
                                   "text:<file>#L<a>-L<b>, json:<file>#<pointer>."},
            "new_text": {"type": "string",
                         "description": "The node's complete new text."},
            "base_sha256": {"type": "string",
                            "description": "sha256 of the node's current text "
                                           "(of the file, for a whole-file node)."},
            "apply": {"type": "boolean", "default": False},
            "task": {"type": "string",
                     "description": "Optional journal task label."},
        }, ["key", "new_text"]),
    },
    {
        "name": "dev_tests",
        "description": (
            "Tests covering a file, symbol or seed. Use before changing a "
            "module/seed to know the scoped gate, and after a change to record "
            "the verification command with dev_record."),
        "inputSchema": _schema({
            "target": {"type": "string",
                       "description": "File path, symbol name, or seed id."},
        }, ["target"]),
    },
    {
        "name": "dev_seed",
        "description": (
            "A seed's manifest: version, description, dependency closure, "
            "the rules/concepts/reflexes it ships, tests that mention it, and "
            "docs that document it."),
        "inputSchema": _schema({
            "seed_id": {"type": "string",
                        "description": "Seed id, e.g. gap_awareness."},
        }, ["seed_id"]),
    },
    {
        "name": "dev_rule",
        "description": (
            "Find a seed rule by name and show its seed, file and rule body. "
            "Use this before editing a rule so you see the existing match / "
            "where / effects rather than re-deriving them."),
        "inputSchema": _schema({
            "name": {"type": "string"},
        }, ["name"]),
    },
    {
        "name": "dev_node",
        "description": (
            "Show one graph node by integer id: type, attrs, source snippet "
            "and grouped neighbours. Ids come from dev_search/dev_context."),
        "inputSchema": _schema({
            "id": {"type": "integer"},
            "include_snippet": {"type": "boolean", "default": True},
        }, ["id"]),
    },
    {
        "name": "dev_neighbours",
        "description": (
            "Out- and in-neighbours of a node, grouped by edge type. Useful "
            "for dependency/seed/test/doc expansion."),
        "inputSchema": _schema({
            "id": {"type": "integer"},
            "edge": {"type": "string",
                     "description": "Optional edge type filter."},
        }, ["id"]),
    },
    {
        "name": "dev_stats",
        "description": "Snapshot histograms and repo summary.",
        "inputSchema": _schema({}),
    },
    {
        "name": "dev_changed",
        "description": (
            "Working-tree changes from `git status --porcelain`, with graph "
            "links for changed files. Use it to see what the current session "
            "has touched, whether the snapshot is stale, and whether "
            "`dev_refresh` has files to reingest."),
        "inputSchema": _schema({
            "all": {"type": "boolean", "default": False,
                    "description": "Include generated/eval/corpus paths."},
            "limit": {"type": "integer", "default": 200},
        }),
    },
    {
        "name": "dev_refresh",
        "description": (
            "Reingest files changed since the snapshot and return the graph "
            "delta (node keys added, removed, changed; edges)."),
        "inputSchema": _schema({}),
    },
    {
        "name": "dev_record",
        "description": (
            "Append a decision or verification receipt to the shared dev "
            "journal. Use it after a scoped test/build command so the next "
            "agent/session can see what was tried and what passed."),
        "inputSchema": _schema({
            "kind": {"type": "string", "enum": ["decision", "verification"],
                     "default": "decision"},
            "task": {"type": "string"},
            "summary": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "command": {"type": "string"},
            "exit_code": {"type": "integer"},
        }, ["task", "summary"]),
    },
    {
        "name": "dev_session_start",
        "description": (
            "Start a replayable dev-session graph (Task -> Plan -> Step -> "
            "Attempt -> Edit -> Verification -> Decision). Returns the new "
            "session id, state and the workflow's next action. This records "
            "facts only; it does not edit files or run tests."),
        "inputSchema": _schema({
            "task": {"type": "string"},
            "max_attempts": {"type": "integer", "default": 3},
        }, ["task"]),
    },
    {
        "name": "dev_session_record",
        "description": (
            "Append one typed node to a dev session. Kinds: plan, step, "
            "attempt, edit, verification, decision. Returns the new node and "
            "the next workflow action."),
        "inputSchema": _schema({
            "session_id": {"type": "string"},
            "kind": {"type": "string",
                     "enum": ["plan", "step", "attempt", "edit",
                              "verification", "decision"]},
            "fields": {"type": "object"},
        }, ["session_id", "kind"]),
    },
    {
        "name": "dev_session_state",
        "description": "The session graph snapshot plus the next action.",
        "inputSchema": _schema({
            "session_id": {"type": "string"},
        }, ["session_id"]),
    },
    {
        "name": "dev_session_next",
        "description": "The next workflow action and its reason.",
        "inputSchema": _schema({
            "session_id": {"type": "string"},
        }, ["session_id"]),
    },
    {
        "name": "dev_session_finish",
        "description": "Close a dev session with an outcome.",
        "inputSchema": _schema({
            "session_id": {"type": "string"},
            "outcome": {"type": "string"},
        }, ["session_id", "outcome"]),
    },
    {
        "name": "dev_session_list",
        "description": "List persisted dev sessions.",
        "inputSchema": _schema({}),
    },
    {
        "name": "dev_recent",
        "description": "Recent dev-journal entries (decisions/verifications).",
        "inputSchema": _schema({
            "n": {"type": "integer", "default": 20},
        }),
    },
]


# LSP tools are appended here to keep the base tool table readable.
def _lsp_pos_schema(*, new_name: bool = False) -> dict[str, Any]:
    props = {
        "file": {"type": "string"},
        "line": {"type": "integer",
                 "description": "0-based line (LSP-native)."},
        "character": {"type": "integer",
                      "description": "0-based UTF-16 character (LSP-native)."},
        "language": {"type": "string"},
    }
    if new_name:
        props["new_name"] = {"type": "string"}
    return _schema(props, ["file", "line", "character"] +
                   (["new_name"] if new_name else []))


_TOOLS.extend([
    {
        "name": "dev_lsp_status",
        "description": "LSP server availability for this checkout (does not start servers).",
        "inputSchema": _schema({}),
    },
    {
        "name": "dev_definition",
        "description": "Compiler-accurate definition at a position via LSP. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.",
        "inputSchema": _lsp_pos_schema(),
    },
    {
        "name": "dev_references",
        "description": "Compiler-accurate references at a position via LSP. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.",
        "inputSchema": _lsp_pos_schema(),
    },
    {
        "name": "dev_callers",
        "description": "Incoming calls (callers) at a symbol position via LSP. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.",
        "inputSchema": _lsp_pos_schema(),
    },
    {
        "name": "dev_callees",
        "description": "Outgoing calls (callees) at a symbol position via LSP. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.",
        "inputSchema": _lsp_pos_schema(),
    },
    {
        "name": "dev_symbol_impact",
        "description": "Resolved references grouped by file for a symbol position via LSP. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.",
        "inputSchema": _lsp_pos_schema(),
    },
    {
        "name": "dev_rename_preview",
        "description": "LSP rename preview (review-only; never writes). Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.",
        "inputSchema": _lsp_pos_schema(new_name=True),
    },
    {
        "name": "dev_diagnostics",
        "description": "LSP push diagnostics for a file (published issues only).",
        "inputSchema": _schema({
            "file": {"type": "string"},
            "language": {"type": "string"},
        }, ["file"]),
    },
    {
        "name": "dev_hover",
        "description": "LSP hover/type/documentation at a position. Positions are 0-based LSP (line and character); dev_symbol and dev_file report 1-based lines, so subtract 1.",
        "inputSchema": _lsp_pos_schema(),
    },
    {
        "name": "dev_document_symbols",
        "description": "LSP document symbol hierarchy for a file.",
        "inputSchema": _schema({
            "file": {"type": "string"},
            "language": {"type": "string"},
        }, ["file"]),
    },
])


_SYMBOL_SYNTAX_NOTE = (
    "symbol accepts \"path::qualname\", a qualname (\"Class.method\"), or a "
    "bare name. A bare or qualname that matches several definitions returns "
    "the candidate ids (under \"ambiguous\", or \"matches\" for "
    "dev_symbol_resolve). Line numbers (lsp_only rows) are 1-based.")

_TOOLS.extend([
    {
        "name": "dev_symbol_callers",
        "description": (
            "Symbol callers from the syntactic symbol graph (local, import and "
            "name resolution, each edge with a confidence). " + _SYMBOL_SYNTAX_NOTE +
            " Use precise=true to confirm direct callers with LSP references; "
            "rows then carry lsp_confirmed and LSP-only references are listed "
            "under lsp_only."),
        "inputSchema": _schema({
            "symbol": {"type": "string",
                       "description": "\"path::qualname\", qualname, or bare name."},
            "depth": {"type": "integer", "default": 1},
            "min_confidence": {"type": "number", "default": 0},
            "precise": {"type": "boolean", "default": False,
                        "description": "Confirm depth-1 callers with LSP references (requires a language server)."},
        }, ["symbol"]),
    },
    {
        "name": "dev_symbol_callees",
        "description": (
            "Symbols that a symbol calls or references, from the syntactic "
            "symbol graph (local, import and name resolution, each edge with a "
            "confidence). " + _SYMBOL_SYNTAX_NOTE),
        "inputSchema": _schema({
            "symbol": {"type": "string",
                       "description": "\"path::qualname\", qualname, or bare name."},
            "depth": {"type": "integer", "default": 1},
            "min_confidence": {"type": "number", "default": 0},
        }, ["symbol"]),
    },
    {
        "name": "dev_symbol_graph_impact",
        "description": (
            "Transitive callers of a symbol (what breaks if it changes), grouped "
            "by file, from the syntactic symbol graph with per-edge confidences. "
            + _SYMBOL_SYNTAX_NOTE + " Use precise=true to confirm depth-1 "
            "callers with LSP references."),
        "inputSchema": _schema({
            "symbol": {"type": "string",
                       "description": "\"path::qualname\", qualname, or bare name."},
            "depth": {"type": "integer", "default": 3},
            "min_confidence": {"type": "number", "default": 0},
            "precise": {"type": "boolean", "default": False,
                        "description": "Confirm depth-1 callers with LSP references (requires a language server)."},
        }, ["symbol"]),
    },
    {
        "name": "dev_symbol_resolve",
        "description": (
            "Resolve a symbol query to its definition ids without walking the "
            "graph. " + _SYMBOL_SYNTAX_NOTE),
        "inputSchema": _schema({
            "symbol": {"type": "string",
                       "description": "\"path::qualname\", qualname, or bare name."},
        }, ["symbol"]),
    },
])


# Background jobs (`ozagia.daemon.jobs`), run by the daemon this server
# reaches over its socket. A wait returns before an MCP client's tool timeout
# (Codex: `tool_timeout_sec`, 60 s by default): at most MCP_WAIT_MAX_S
# (`$OZAGIA_MCP_WAIT_MAX_S`), by default 45 s; job_stop's grace_s is at most
# MCP_STOP_GRACE_MAX_S. Job tool calls run on worker threads (`serve`), so a
# wait does not hold up other tool calls. Starting jobs is opt-in per project
# (`[jobs] enabled = true`); they run outside the client's sandbox.
MCP_WAIT_DEFAULT_S = 45.0
MCP_WAIT_MAX_S = 50.0
MCP_STOP_GRACE_MAX_S = 10.0
MCP_JOB_WORKERS = 4
_UNSANDBOXED = (
    " Jobs run in the ozagia daemon with its privileges and environment, "
    "outside the Codex sandbox, approval prompts and shell environment "
    "policy (and OpenCode's permission checks). Starting them is off unless "
    "the project enables it ([jobs] enabled = true in .ozagia.toml).")
_JOB_REF = {"type": "string",
            "description": "A job id from job_start or job_list (e.g. \"j3\")."}
_TOOLS.extend([
    {
        "name": "job_start",
        "description": (
            "Start a long-running command (a server, a build, a slow test run) "
            "in the background, in its own process group, with stdout and "
            "stderr in a log. Use it instead of `cmd &`, nohup, or a shell "
            "session you poll; then job_wait for its exit or a log line. "
            "Give command (a shell string) or argv. Jobs are stopped when the "
            "ozagia daemon exits unless detach is true." + _UNSANDBOXED),
        "inputSchema": _schema({
            "command": {"type": "string",
                        "description": "Shell command line, run with bash -c."},
            "argv": {"type": "array", "items": {"type": "string"},
                     "description": "Program and arguments, run without a shell."},
            "cwd": {"type": "string",
                    "description": "Working directory inside the project (default: its root)."},
            "env": {"type": "object", "additionalProperties": {"type": "string"},
                    "description": "Environment variables to add."},
            "detach": {"type": "boolean", "default": False,
                       "description": "Leave it running when the daemon exits."},
        }),
    },
    {
        "name": "job_wait",
        "description": (
            "Block until a job exits, a file exists, or a new log line matches "
            "a regex, or until the deadline; returns the status, the exit "
            "code, and only the log lines you have not seen yet (the newest "
            "200 lines / 16 kB at most). Use it instead of sleep loops, ps or "
            "polling a shell. If it returns with the job still running, call "
            "it again: each call continues where the last one stopped."),
        "inputSchema": _schema({
            "job": {"type": "string", "default": "any",
                    "description": "Job id, or \"any\" for the first running job to finish (with until file:<path> and no job, waits for the file alone)."},
            "until": {"type": "string", "default": "exit",
                      "description": "exit | file:<path> | log:<regex>."},
            "deadline_s": {"type": "number", "default": MCP_WAIT_DEFAULT_S,
                           "description": f"Seconds to wait (at most {MCP_WAIT_MAX_S:g})."},
            "cursor": {"type": "integer",
                       "description": "Byte offset in the log to read from (default: where your last read ended)."},
        }),
    },
    {
        "name": "job_list",
        "description": "List background jobs (newest first): id, command, status, exit code, pid, elapsed seconds, log path.",
        "inputSchema": _schema({}),
    },
    {
        "name": "job_log",
        "description": (
            "Read a job's log lines you have not seen yet (or from cursor; "
            "cursor 0 gives the last lines of the whole log), without waiting."),
        "inputSchema": _schema({
            "job": _JOB_REF,
            "cursor": {"type": "integer", "description": "Byte offset to read from."},
            "tail": {"type": "integer", "default": 200,
                     "description": "Most lines to return (the newest)."},
        }, ["job"]),
    },
    {
        "name": "job_stop",
        "description": "Stop a job: the signal to its whole process group, then SIGKILL after grace_s.",
        "inputSchema": _schema({
            "job": _JOB_REF,
            "signal": {"type": "string", "enum": ["TERM", "INT", "HUP", "KILL"],
                       "default": "TERM"},
            "grace_s": {"type": "number", "default": 3,
                        "description": f"Seconds before SIGKILL (at most {MCP_STOP_GRACE_MAX_S:g})."},
        }, ["job"]),
    },
])


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
class _ArgError(ValueError):
    """An argument does not match its tool's inputSchema (JSON-RPC -32602)."""


_SCHEMAS: dict[str, dict[str, Any]] = {t["name"]: t["inputSchema"] for t in _TOOLS}
# Built-in tools listed only when the snapshot has nodes of this type.
_TOOL_NODE_TYPES = {"dev_seed": "Seed", "dev_rule": "Rule"}
_INT_TEXT = re.compile(r"-?[0-9]+")


def _coerce_arg(key: str, value: Any, spec: dict[str, Any]) -> Any:
    typ = spec.get("type")
    if typ == "string":
        if not isinstance(value, str):
            raise _ArgError(f"argument {key!r} must be a string")
    elif typ == "integer":
        if isinstance(value, str) and _INT_TEXT.fullmatch(value):
            value = int(value)
        elif isinstance(value, bool) or not isinstance(value, int):
            raise _ArgError(f"argument {key!r} must be an integer")
    elif typ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _ArgError(f"argument {key!r} must be a number")
    elif typ == "boolean":
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
        elif not isinstance(value, bool):
            raise _ArgError(f"argument {key!r} must be a boolean")
    elif typ == "object":
        if not isinstance(value, dict):
            raise _ArgError(f"argument {key!r} must be an object")
    elif typ == "array":
        if not isinstance(value, list):
            raise _ArgError(f"argument {key!r} must be an array")
    enum = spec.get("enum")
    if enum is not None and value not in enum:
        raise _ArgError(f"argument {key!r} must be one of {enum}, "
                        f"got {value!r}")
    return value


def _validate_args(schema: dict[str, Any], args: Any) -> dict[str, Any]:
    """Check tool arguments against the tool's inputSchema and return the
    coerced dict that handlers receive."""
    if not isinstance(args, dict):
        raise _ArgError("arguments must be an object")
    props = schema.get("properties") or {}
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in props:
                raise _ArgError(f"unknown argument {key!r}")
    for key in schema.get("required") or []:
        if key not in args:
            raise _ArgError(f"missing required argument {key!r}")
    out: dict[str, Any] = {}
    for key, value in args.items():
        spec = props.get(key)
        out[key] = value if spec is None else _coerce_arg(key, value, spec)
    return out


class DevMemoryServer:
    def __init__(self, memory: DevMemory | None, *, mode: str = "coding",
                 workspace: str | Path | None = None, surface: str = "full"):
        if mode not in {"coding", "research", "combined"}:
            raise ValueError("mode must be one of: coding, research, combined")
        if surface not in SURFACES:
            raise ValueError(f"unknown surface {surface!r}; known: {SURFACES}")
        self.mem = memory
        self.mode = mode
        self.surface = surface
        default_workspace = memory.raw_repo_root if memory else repo_root()
        self.workspace = str(workspace or default_workspace)
        if surface == "graph" and mode in {"coding", "combined"}:
            if memory is None:
                raise ValueError("the graph surface needs a dev-memory snapshot")
            from ozagia.integrations.mcp.graph_surface import (
                GRAPH_TOOLS, GraphSurface)
            self._handlers = GraphSurface(memory).handlers()
            self._tools = list(GRAPH_TOOLS)
            self._schemas = {t["name"]: t["inputSchema"] for t in self._tools}
            self._load_adapter_tools()
            if mode == "combined":
                self._load_research_tools()
            return
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = ({
            "dev_context": self._context,
            "dev_brief": self._brief,
            "dev_repo_map": self._repo_map,
            "dev_impact": self._impact,
            "dev_architecture": self._architecture,
            "dev_scip": self._scip,
            "dev_code_search": self._code_search,
            "dev_lsp_status": self._lsp_status,
            "dev_definition": self._lsp_definition,
            "dev_references": self._lsp_references,
            "dev_callers": self._lsp_callers,
            "dev_callees": self._lsp_callees,
            "dev_symbol_impact": self._lsp_symbol_impact,
            "dev_rename_preview": self._lsp_rename_preview,
            "dev_diagnostics": self._lsp_diagnostics,
            "dev_hover": self._lsp_hover,
            "dev_document_symbols": self._lsp_document_symbols,
            "dev_search": self._search,
            "dev_file": self._file,
            "dev_symbol": self._symbol,
            "dev_outline": self._outline,
            "dev_ast": self._ast,
            "dev_query": self._query,
            "dev_edit": self._edit,
            "dev_span_edit": self._span_edit,
            "dev_key_edit": self._key_edit,
            "dev_tests": self._tests,
            "dev_seed": self._seed,
            "dev_rule": self._rule,
            "dev_node": self._node,
            "dev_neighbours": self._neighbours,
            "dev_stats": self._stats,
            "dev_changed": self._changed,
            "dev_refresh": self._refresh,
            "dev_record": self._record,
            "dev_session_start": self._session_start,
            "dev_session_record": self._session_record,
            "dev_session_state": self._session_state,
            "dev_session_next": self._session_next,
            "dev_session_finish": self._session_finish,
            "dev_session_list": self._session_list,
            "dev_recent": self._recent,
            "dev_symbol_callers": self._symbol_callers,
            "dev_symbol_callees": self._symbol_callees,
            "dev_symbol_graph_impact": self._symbol_graph_impact,
            "dev_symbol_resolve": self._symbol_resolve,
            "job_start": self._job_start,
            "job_wait": self._job_wait,
            "job_list": self._job_list,
            "job_log": self._job_log,
            "job_stop": self._job_stop,
        } if mode in {"coding", "combined"} else {})
        if self.mem is not None:
            present = {n.get("type") for n in self.mem.nodes.values()}
            for name, typ in _TOOL_NODE_TYPES.items():
                if typ not in present:
                    self._handlers.pop(name, None)
        self._tools = [t for t in _TOOLS if t["name"] in self._handlers]
        self._schemas = {t["name"]: t["inputSchema"] for t in self._tools}
        if mode in {"coding", "combined"}:
            self._load_adapter_tools()
            self._load_sre_tools()
        if mode in {"research", "combined"}:
            self._load_research_tools()
        if mode == "combined":
            self._load_research_dev_context()

    def _load_research_tools(self) -> None:
        """Load the optional research package only for a research mode."""
        module = importlib.import_module("ozagia.research.mcp")
        for spec in module.research_tools():
            name = spec["name"]
            if name in self._handlers:
                print(f"[{SERVER_NAME}] research tool {name!r} clashes with an "
                      "existing tool; ignored", file=sys.stderr)
                continue
            self._handlers[name] = (
                lambda args, name=name: module.call_research_tool(
                    name, args, self.workspace))
            self._tools.append(spec)
            self._schemas[name] = spec["inputSchema"]

    def _load_research_dev_context(self) -> None:
        """Expose an explicit, non-linking bridge between the two contexts."""
        name = "research_dev_context"
        spec = {
            "name": name,
            "description": (
                "Return separate coding and research context for the same task. "
                "The research session must exist in this workspace. The two "
                "results are independently retrieved and are not linked or "
                "treated as mutually supporting evidence."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "session_id": {"type": "string"},
                },
                "required": ["task", "session_id"],
                "additionalProperties": False,
            },
        }
        if name in self._handlers:
            print(f"[{SERVER_NAME}] bridge tool {name!r} clashes with an "
                  "existing tool; ignored", file=sys.stderr)
            return
        research_module = importlib.import_module("ozagia.research.mcp")

        def bridge(args: dict[str, Any]) -> Any:
            task = str(args["task"])
            # Let ResearchService load the requested session. This validates
            # the id against this workspace before returning its context.
            research_context = research_module.call_research_tool(
                "research_context",
                {"session_id": args["session_id"], "query": task},
                self.workspace)
            coding_context = self.mem.context(task)
            return {
                "task": task,
                "coding_context": coding_context,
                "research_context": research_context,
                "relationship": {
                    "independent_retrievals": True,
                    "linked": False,
                    "note": (
                        "These are separate context results. No relationship "
                        "or evidentiary link between them was inferred or "
                        "created."),
                },
            }

        self._handlers[name] = bridge
        self._tools.append(spec)
        self._schemas[name] = spec["inputSchema"]

    def _load_sre_tools(self) -> None:
        if self.mem is None:
            return
        from ozagia.integrations.mcp.sre import sre_tools
        for spec, handler in sre_tools(self.mem):
            name = spec["name"]
            if name in _SCHEMAS or name in self._schemas:
                print(f"[{SERVER_NAME}] SRE tool {name!r} clashes with a "
                      "built-in tool; ignored", file=sys.stderr)
                continue
            self._handlers[name] = handler
            self._tools.append(spec)
            self._schemas[name] = spec["inputSchema"]

    def _load_adapter_tools(self) -> None:
        reg = registry(self.mem.raw_repo_root)
        for err in reg.errors:
            print(f"[{SERVER_NAME}] adapter error: {err}", file=sys.stderr)
        for name, (spec, handler) in reg.mcp_tools.items():
            if name in _SCHEMAS or name in self._schemas:
                print(f"[{SERVER_NAME}] adapter tool {name!r} clashes with a "
                      f"built-in tool; ignored", file=sys.stderr)
                continue
            self._handlers[name] = (
                lambda a, handler=handler: handler(self.mem, a))
            self._tools.append(spec)
            self._schemas[name] = spec["inputSchema"]

    # -- tool implementations --------------------------------------------- #
    def _context(self, a: dict[str, Any]) -> Any:
        return self.mem.context(str(a["task"]),
                                max_items=a.get("max_items", 8),
                                mode=str(a.get("mode", "auto")),
                                budget_tokens=a.get("budget_tokens", 6000))

    def _brief(self, a: dict[str, Any]) -> Any:
        return self.mem.brief(a.get("task"),
                              budget_tokens=a.get("budget_tokens", 4000),
                              write=a.get("write", False))

    def _repo_map(self, a: dict[str, Any]) -> Any:
        return self.mem.repo_map(str(a.get("task") or ""),
                                 budget_tokens=a.get("budget_tokens", 1500),
                                 focus_files=a.get("focus_files"),
                                 force_refresh=a.get("force_refresh", False),
                                 scip_path=a.get("scip_path"))

    def _code_search(self, a: dict[str, Any]) -> Any:
        return self.mem.code_search(str(a["query"]),
                                    limit=a.get("limit", 20),
                                    include_snippet=a.get("include_snippet", True))

    def _scip(self, a: dict[str, Any]) -> Any:
        return self.mem.scip(str(a["path"]),
                             summary_only=a.get("summary_only", False))

    def _architecture(self, a: dict[str, Any]) -> Any:
        return self.mem.architecture(top=a.get("top", 15))

    def _impact(self, a: dict[str, Any]) -> Any:
        return self.mem.impact(str(a["file"]),
                               depth=a.get("depth", 2),
                               scip_path=a.get("scip_path"))

    def _outline(self, a: dict[str, Any]) -> Any:
        return self.mem.outline_target(str(a["target"]))

    def _search(self, a: dict[str, Any]) -> Any:
        return {"query": a["text"],
                "results": self.mem.search(str(a["text"]),
                                           types=a.get("types"),
                                           limit=a.get("limit", 20))}

    def _file(self, a: dict[str, Any]) -> Any:
        return self.mem.file(str(a["path"]))

    def _symbol(self, a: dict[str, Any]) -> Any:
        return self.mem.symbol(str(a["name"]))

    def _query(self, a: dict[str, Any]) -> Any:
        from ozagia.core import structured as S
        from ozagia.intent.common import IntentError
        from ozagia.intent.read import structured_query
        path = str(a["path"])
        parsed = S.parse_key(path)
        if parsed is not None and not (Path(self.mem.raw_repo_root) / path).exists():
            fmt, rel, pointer = parsed
            if a.get("pointer") not in (None, "", "#") and pointer not in ("", "#"):
                return {"ok": False, "error": "give the pointer in the key or "
                                              "in pointer, not both"}
            pointer = pointer or str(a.get("pointer") or "")
            if a.get("format") and a["format"] != fmt:
                return {"ok": False, "error": f"format {a['format']!r} does "
                                              f"not match the key {fmt}:"}
        else:
            rel, pointer = path, str(a.get("pointer") or "")
            fmt = a.get("format") or S.format_of(rel)
            if fmt is None:
                return {"ok": False, "error": f"{rel}: not a .json, .yaml, "
                                              f".yml or .toml file; give format"}
        try:
            res = structured_query(self.mem, rel, pointer=pointer, fmt=fmt,
                                   offset=a.get("offset"),
                                   limit=a.get("limit"), index_base=0)
        except IntentError as e:
            return {"ok": False, "error": str(e)}
        r = res["result"]
        return {"ok": True, "key": r["key"], "format": fmt, "path": rel,
                "pointer": r["pointer"], "type": r["type"], "size": r["size"],
                "line": r["line"], "total": r["total"],
                "shown": list(r["shown"]) if r["shown"] else None,
                "next_offset": r["next"], "truncated": r["truncated"],
                "text": res["output"]}

    def _ast(self, a: dict[str, Any]) -> Any:
        return self.mem.ast(str(a["file"]), node=a.get("node"),
                            max_nodes=a.get("max_nodes", 200))

    def _edit(self, a: dict[str, Any]) -> Any:
        script = {
            "file": a["file"],
            "ops": a["ops"],
            "apply": a.get("apply", False),
        }
        if a.get("base_sha256"):
            script["base_sha256"] = a["base_sha256"]
        if a.get("cst_graph") is not None:
            script["cst_graph"] = a["cst_graph"]
        return self.mem.edit(script, task=a.get("task"),
                             diagnostics=a.get("diagnostics", False))

    def _span_edit(self, a: dict[str, Any]) -> Any:
        return self.mem.span_edit(str(a["file"]), a["old_string"],
                                  a["new_string"],
                                  replace_all=a.get("replace_all", False),
                                  base_sha256=a.get("base_sha256") or None,
                                  apply=a.get("apply", False),
                                  task=a.get("task"))

    def _key_edit(self, a: dict[str, Any]) -> Any:
        return self.mem.key_edit(str(a["key"]), a["new_text"],
                                 base_sha256=a.get("base_sha256") or None,
                                 apply=a.get("apply", False),
                                 task=a.get("task"))

    def _symbol_callers(self, a: dict[str, Any]) -> Any:
        return self.mem.symbol_graph("callers", str(a["symbol"]),
                                     depth=a.get("depth", 1),
                                     min_confidence=a.get("min_confidence", 0),
                                     precise=a.get("precise", False))

    def _symbol_callees(self, a: dict[str, Any]) -> Any:
        return self.mem.symbol_graph("callees", str(a["symbol"]),
                                     depth=a.get("depth", 1),
                                     min_confidence=a.get("min_confidence", 0))

    def _symbol_graph_impact(self, a: dict[str, Any]) -> Any:
        return self.mem.symbol_graph("impact", str(a["symbol"]),
                                     depth=a.get("depth", 3),
                                     min_confidence=a.get("min_confidence", 0),
                                     precise=a.get("precise", False))

    def _symbol_resolve(self, a: dict[str, Any]) -> Any:
        return self.mem.symbol_graph("resolve", str(a["symbol"]))

    def _tests(self, a: dict[str, Any]) -> Any:
        return self.mem.tests_for(str(a["target"]))

    def _seed(self, a: dict[str, Any]) -> Any:
        return self.mem.seed(str(a["seed_id"]))

    def _rule(self, a: dict[str, Any]) -> Any:
        return self.mem.rule(str(a["name"]))

    def _node(self, a: dict[str, Any]) -> Any:
        nid = a["id"]
        n = self.mem.node(nid)
        if n is None:
            return {"ok": False, "error": f"no node {nid}"}
        out: dict[str, Any] = {
            "ok": True, "node": self.mem.view(nid),
            "attrs": n.get("attrs", {}),
            "related": self.mem.related(nid),
        }
        if a.get("include_snippet", True):
            out["snippet"] = self.mem.snippet(nid)
        return out

    def _neighbours(self, a: dict[str, Any]) -> Any:
        nid = a["id"]
        if self.mem.node(nid) is None:
            return {"ok": False, "error": f"no node {nid}"}
        out: dict[str, Any] = {"ok": True, "node": self.mem.view(nid),
                               "neighbours": self.mem.related(nid)}
        edge = a.get("edge")
        if edge:
            out["filtered"] = {
                "out": [self.mem.view(int(e["tgt"])) for e in
                        self.mem._out.get(nid, []) if e["type"] == edge],
                "in": [self.mem.view(int(e["src"])) for e in
                       self.mem._in.get(nid, []) if e["type"] == edge],
            }
        return out

    def _stats(self, _a: dict[str, Any]) -> Any:
        return self.mem.stats()

    def _changed(self, a: dict[str, Any]) -> Any:
        return self.mem.changed(include_all=a.get("all", False),
                                limit=a.get("limit", 200))

    def _refresh(self, _a: dict[str, Any]) -> Any:
        try:
            delta = self.mem.refresh()
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "delta": delta.to_dict()}

    def _lsp_status(self, _a: dict[str, Any]) -> Any:
        return self.mem.lsp_status()

    def _lsp_definition(self, a: dict[str, Any]) -> Any:
        return self.mem.lsp_definition(str(a["file"]), a["line"],
                                       a["character"],
                                       language=a.get("language"))

    def _lsp_references(self, a: dict[str, Any]) -> Any:
        return self.mem.lsp_references(str(a["file"]), a["line"],
                                       a["character"],
                                       language=a.get("language"))

    def _lsp_callers(self, a: dict[str, Any]) -> Any:
        return self.mem.lsp_callers(str(a["file"]), a["line"],
                                    a["character"],
                                    language=a.get("language"))

    def _lsp_callees(self, a: dict[str, Any]) -> Any:
        return self.mem.lsp_callees(str(a["file"]), a["line"],
                                    a["character"],
                                    language=a.get("language"))

    def _lsp_symbol_impact(self, a: dict[str, Any]) -> Any:
        return self.mem.lsp_symbol_impact(str(a["file"]), a["line"],
                                          a["character"],
                                          language=a.get("language"))

    def _lsp_rename_preview(self, a: dict[str, Any]) -> Any:
        return self.mem.lsp_rename_preview(str(a["file"]), a["line"],
                                           a["character"],
                                           str(a["new_name"]),
                                           language=a.get("language"))

    def _lsp_diagnostics(self, a: dict[str, Any]) -> Any:
        return self.mem.lsp_diagnostics(str(a["file"]),
                                        language=a.get("language"))

    def _lsp_hover(self, a: dict[str, Any]) -> Any:
        return self.mem.lsp_hover(str(a["file"]), a["line"],
                                  a["character"],
                                  language=a.get("language"))

    def _lsp_document_symbols(self, a: dict[str, Any]) -> Any:
        return self.mem.lsp_document_symbols(str(a["file"]),
                                             language=a.get("language"))

    def _session_start(self, a: dict[str, Any]) -> Any:
        from ozagia.core.session import DevSession
        sess = DevSession.start(str(a["task"]),
                                max_attempts=a.get("max_attempts", 3),
                                root=self.mem.raw_repo_root)
        return {"ok": True, "state": sess.state(),
                "next": sess.next_action()}

    def _session_record(self, a: dict[str, Any]) -> Any:
        from ozagia.core.session import load_session
        fields = a.get("fields") or {}
        if not isinstance(fields, dict):
            raise ValueError("fields must be an object")
        sess = load_session(str(a["session_id"]),
                            root=self.mem.raw_repo_root)
        node = sess.record(str(a["kind"]), **fields)
        return {"ok": True, "node": node, "next": sess.next_action()}

    def _session_state(self, a: dict[str, Any]) -> Any:
        from ozagia.core.session import load_session
        sess = load_session(str(a["session_id"]),
                            root=self.mem.raw_repo_root)
        return {"ok": True, "state": sess.state(),
                "next": sess.next_action()}

    def _session_next(self, a: dict[str, Any]) -> Any:
        from ozagia.core.session import load_session
        sess = load_session(str(a["session_id"]),
                            root=self.mem.raw_repo_root)
        return {"ok": True, "next": sess.next_action()}

    def _session_finish(self, a: dict[str, Any]) -> Any:
        from ozagia.core.session import load_session
        sess = load_session(str(a["session_id"]),
                            root=self.mem.raw_repo_root)
        return {"ok": True, "state": sess.finish(str(a["outcome"]))}

    def _session_list(self, _a: dict[str, Any]) -> Any:
        from ozagia.core.session import list_sessions
        return {"ok": True,
                "sessions": list_sessions(root=self.mem.raw_repo_root)}

    def _record(self, a: dict[str, Any]) -> Any:
        return {"ok": True, "entry": self.mem.journal_append(
            str(a.get("kind", "decision")), str(a["task"]),
            str(a["summary"]), files=a.get("files"),
            command=a.get("command"), exit_code=a.get("exit_code"))}

    def _recent(self, a: dict[str, Any]) -> Any:
        return {"ok": True, "entries": self.mem.journal_recent(a.get("n", 20))}

    # -- background jobs (in the daemon) ----------------------------------- #
    def _job(self, op: str, fields: dict[str, Any]) -> Any:
        from ozagia.daemon.jobcli import JobUnavailable, request
        try:
            reply = request(self.workspace, op, {**fields, "harness": "mcp",
                                                 "reader": f"mcp:{os.getpid()}"})
        except JobUnavailable as e:
            raise RuntimeError(str(e)) from None
        reply.pop("id", None)
        return reply

    def _job_start(self, a: dict[str, Any]) -> Any:
        return self._job("job_start", {"session": f"mcp:{os.getpid()}", **a})

    def _job_wait(self, a: dict[str, Any]) -> Any:
        try:
            cap = float(os.environ.get("OZAGIA_MCP_WAIT_MAX_S") or MCP_WAIT_MAX_S)
        except ValueError:
            cap = MCP_WAIT_MAX_S
        deadline = min(max(float(a.get("deadline_s", MCP_WAIT_DEFAULT_S)), 0.0), cap)
        job = a.get("job")
        if not job and not str(a.get("until") or "").startswith("file:"):
            job = "any"
        return self._job("job_wait", {**a, "job": job or None,
                                      "deadline_s": deadline})

    def _job_list(self, _a: dict[str, Any]) -> Any:
        return self._job("job_list", {})

    def _job_log(self, a: dict[str, Any]) -> Any:
        return self._job("job_log", a)

    def _job_stop(self, a: dict[str, Any]) -> Any:
        grace = a.get("grace_s")
        if isinstance(grace, (int, float)) and not isinstance(grace, bool):
            a = {**a, "grace_s": min(max(float(grace), 0.0), MCP_STOP_GRACE_MAX_S)}
        return self._job("job_stop", a)

    # -- JSON-RPC ---------------------------------------------------------- #
    def handle(self, msg: Any) -> dict[str, Any] | None:
        """Answer one JSON-RPC message. Never raises.

        Returns None for notifications (no `id`) that succeed. A notification
        that fails is logged to stderr and still gets no reply."""
        if not isinstance(msg, dict):
            return self._err(None, _E_INVALID_REQUEST,
                             "invalid request: message must be an object")
        try:
            return self._dispatch(msg)
        except Exception as e:  # noqa: BLE001 - the server must keep running
            print(f"[{SERVER_NAME}] internal error handling "
                  f"{msg.get('method')!r}:\n{traceback.format_exc()}",
                  file=sys.stderr)
            if "id" not in msg:
                return None
            return self._err(msg.get("id"), _E_INTERNAL,
                             f"internal error: {type(e).__name__}: {e}")

    def _dispatch(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method", "")
        msg_id = msg.get("id")
        if "params" in msg and not isinstance(msg["params"], dict):
            return self._err(msg_id, _E_INVALID_PARAMS,
                             "params must be an object")
        params = msg.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = (requested if requested in SUPPORTED_PROTOCOL_VERSIONS
                       else PROTOCOL_VERSION)
            return self._ok(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return self._ok(msg_id, {})
        if method == "tools/list":
            return self._ok(msg_id, {"tools": self._tools})
        if method == "resources/list":
            resources = ([{
                "uri": BRIEF_URI,
                "name": "Repo brief",
                "description": "Compact repo layout, commands, docs, history and changes.",
                "mimeType": "text/markdown",
            }] if self.mode in {"coding", "combined"} else [])
            return self._ok(msg_id, {"resources": resources})
        if method == "resources/templates/list":
            return self._ok(msg_id, {"resourceTemplates": []})
        if method == "resources/read":
            uri = str(params.get("uri", ""))
            if uri == BRIEF_URI and self.mem is not None:
                brief = self.mem.brief()
                return self._ok(msg_id, {"contents": [{
                    "uri": uri,
                    "mimeType": "text/markdown",
                    "text": brief["markdown"],
                }]})
            return self._err(msg_id, -32602, f"unknown resource {uri!r}")
        if method == "tools/call":
            name = params.get("name", "")
            if not isinstance(name, str) or name not in self._handlers:
                return self._err(msg_id, _E_INVALID_PARAMS,
                                 f"unknown tool {name!r}")
            if "arguments" in params and not isinstance(
                    params["arguments"], dict):
                return self._err(msg_id, _E_INVALID_PARAMS,
                                 "arguments must be an object")
            try:
                args = _validate_args(self._schemas[name],
                                      params.get("arguments", {}))
            except _ArgError as e:
                return self._err(msg_id, _E_INVALID_PARAMS, str(e))
            handler = self._handlers[name]
            try:
                result = handler(args)
                text = json.dumps(result, sort_keys=False)
                return self._ok(msg_id, {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                })
            except Exception as e:  # noqa: BLE001 - surfaced to the client
                return self._ok(msg_id, {
                    "content": [{"type": "text",
                                 "text": f"{type(e).__name__}: {e}"}],
                    "isError": True,
                })
        return self._err(msg_id, _E_METHOD_NOT_FOUND,
                         f"method not found: {method}")

    @staticmethod
    def _ok(msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------- #
# stdio transport (newline-delimited spec framing + LSP-style fallback)
# --------------------------------------------------------------------------- #
class _ParseFailure(ValueError):
    """A message could not be decoded. `framing` is the framing to reply in."""

    def __init__(self, framing: str, detail: str) -> None:
        super().__init__(detail)
        self.framing = framing


# Returned by `_read_message` at end of input. A JSON `null` is a real message.
_EOF = object()


def _read_message(stream) -> tuple[Any, str]:
    """Read one message. Returns (message, framing); message is `_EOF` at the
    end of input. Blank lines are skipped; only EOF ends the stream."""
    while True:
        line = stream.readline()
        if not line:
            return _EOF, "newline"
        if line.lower().startswith(b"content-length:"):
            try:
                length = int(line.split(b":", 1)[1].strip())
            except ValueError as e:
                raise _ParseFailure("content-length",
                                    "invalid Content-Length header") from e
            # consume headers until the blank line
            while True:
                h = stream.readline()
                if not h or h in (b"\r\n", b"\n"):
                    break
            # A negative length would make stream.read block until EOF, and an
            # oversized one would buffer without limit. The body is not read.
            if length < 0 or length > MAX_MESSAGE_BYTES:
                raise _ParseFailure(
                    "content-length",
                    f"Content-Length {length} outside 0..{MAX_MESSAGE_BYTES}")
            body = stream.read(length)
            try:
                return json.loads(body.decode("utf-8")), "content-length"
            except ValueError as e:
                raise _ParseFailure("content-length", str(e)) from e
        text = line.strip()
        if not text:
            continue
        try:
            return json.loads(text.decode("utf-8")), "newline"
        except ValueError as e:
            raise _ParseFailure("newline", str(e)) from e


def _write_message(stream, msg: Any, framing: str) -> None:
    body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    if framing == "content-length":
        stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode())
        stream.write(body)
    else:
        stream.write(body + b"\n")
    stream.flush()


def _respond(server: DevMemoryServer, msg: Any) -> dict[str, Any] | None:
    """One message of a stream or batch. Notifications are never answered."""
    if isinstance(msg, dict) and "id" not in msg:
        response = server.handle(msg)
        if response is not None and "error" in response:
            print(f"[{SERVER_NAME}] notification failed: "
                  f"{response['error']}", file=sys.stderr)
        return None
    return server.handle(msg)


def _is_job_call(msg: Any) -> bool:
    """A `tools/call` of a job tool (it only talks to the daemon, so it can
    run on a worker thread)."""
    if not isinstance(msg, dict) or msg.get("method") != "tools/call" or "id" not in msg:
        return False
    params = msg.get("params")
    return (isinstance(params, dict) and isinstance(params.get("name"), str)
            and params["name"].startswith("job_"))


def serve(server: DevMemoryServer) -> int:
    """Read messages from stdin and write the replies to stdout. Job tool
    calls (`job_wait` may block for up to MCP_WAIT_MAX_S) run on a pool of
    MCP_JOB_WORKERS threads, so the next messages are answered meanwhile;
    their replies may come out of order (JSON-RPC matches them by `id`).
    Every other message is handled in order on this thread. Replies are
    written whole, under a lock."""
    from concurrent.futures import ThreadPoolExecutor
    inp = sys.stdin.buffer
    out = sys.stdout.buffer
    write_lock = threading.Lock()

    def write(msg: Any, framing: str) -> None:
        with write_lock:
            _write_message(out, msg, framing)

    def job_call(msg: dict[str, Any], framing: str) -> None:
        response = _respond(server, msg)
        if response is not None:
            try:
                write(response, framing)
            except (OSError, ValueError):
                pass                      # the client went away
    pool = ThreadPoolExecutor(max_workers=MCP_JOB_WORKERS,
                              thread_name_prefix="ozagia-mcp-job")
    # Only protocol messages reach stdout. `out` was captured above; any
    # stray print from a handler goes to stderr for the duration of serve().
    saved_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        while True:
            try:
                msg, framing = _read_message(inp)
            except _ParseFailure as e:
                write(server._err(None, _E_PARSE, f"parse error: {e}"), e.framing)
                continue
            if msg is _EOF:
                break
            if isinstance(msg, list):
                if not msg:
                    write(server._err(None, _E_INVALID_REQUEST,
                                      "invalid request: empty batch"), framing)
                    continue
                replies = [r for r in (_respond(server, m) for m in msg)
                           if r is not None]
                if replies:
                    write(replies, framing)
                continue
            if _is_job_call(msg):
                pool.submit(job_call, msg, framing)
                continue
            response = _respond(server, msg)
            if response is not None:
                write(response, framing)
        pool.shutdown(wait=True)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        sys.stdout = saved_stdout
    return 0


def _load_memory(args: argparse.Namespace) -> DevMemory:
    dump = Path(args.dump)
    if not dump.exists():
        if args.autobuild or os.environ.get("OZAGIA_AUTOBUILD") == "1":
            print(f"[{SERVER_NAME}] building {dump} ...", file=sys.stderr)
            build_index(args.root, dump, include_docs="features",
                        include_git=True, include_scripts=True)
        else:
            raise SystemExit(
                f"no dev-memory snapshot at {dump}; run "
                f"`python -m ozagia.core.memory build` first "
                f"(or pass --autobuild)")
    return DevMemory(dump, Path(args.journal), root=Path(args.root))


def _close_lsp() -> None:
    try:
        from ozagia.lsp import close_all
        close_all()
    except Exception:  # noqa: BLE001 - best effort during shutdown
        pass


def _on_sigterm(signum: int, frame: Any) -> None:
    _close_lsp()
    raise SystemExit(0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dump", default=os.environ.get("OZAGIA_DUMP"))
    p.add_argument("--journal", default=os.environ.get("OZAGIA_JOURNAL"))
    p.add_argument("--root", default=str(repo_root()))
    p.add_argument("--autobuild", action="store_true")
    p.add_argument("--mode", choices=("coding", "research", "combined"),
                   default=os.environ.get("OZAGIA_MODE", "coding"),
                   help="tool set to expose (default: OZAGIA_MODE or coding)")
    p.add_argument("--surface", default=os.environ.get("OZAGIA_SURFACE", "full"),
                   help="coding tool surface: full (default) or graph")
    args = p.parse_args(argv)
    if args.surface not in SURFACES:
        p.error(f"--surface/OZAGIA_SURFACE must be one of {', '.join(SURFACES)}, "
                f"got {args.surface!r}")
    # Resolve defaults only after --root is known. The module-level memory
    # defaults are anchored to the importing process's cwd, which can differ
    # from an explicitly selected workspace. state_path also honors
    # OZAGIA_STATE_DIR, while explicit flags and OZAGIA_DUMP/JOURNAL win.
    if args.dump is None:
        args.dump = str(state_path(args.root, "graph.json"))
    if args.journal is None:
        args.journal = str(state_path(args.root, "journal.jsonl"))
    mem = _load_memory(args) if args.mode in {"coding", "combined"} else None
    # LSP servers are child processes: close them on normal exit, on exit via
    # an exception, and on SIGTERM.
    atexit.register(_close_lsp)
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:
        pass  # not the main thread
    try:
        return serve(DevMemoryServer(mem, mode=args.mode, workspace=args.root,
                                     surface=args.surface))
    finally:
        _close_lsp()


if __name__ == "__main__":
    raise SystemExit(main())
