"""Portable research syntax and graph bridges; no Ocelot import is required."""

from __future__ import annotations

import json
import subprocess
import sys
import builtins

import pytest

from ozagia.research.formula import translate_formula
from ozagia.research.finite_formula import translate_finite_formula
from ozagia.research.graph import ResearchGraph
from ozagia.research.lean import export_lean
from ozagia.research.membership import translate_membership
from ozagia.research.model import create_model_candidate
from ozagia.research.native import project_research_graph
from ozagia.research.trace import trace_chain
from ozagia.research.graph import ResearchError


def test_formula_translation_emits_portable_term_wire_format():
    result = translate_formula("x + 2 * y = 8 and x < y")
    assert result.term == {
        "type": "And",
        "items": [
            {
                "type": "Eq",
                "a": {
                    "type": "Plus",
                    "items": [
                        {"type": "Var", "name": "x"},
                        {
                            "type": "Times",
                            "items": [{"type": "Lit", "value": 2}, {"type": "Var", "name": "y"}],
                        },
                    ],
                },
                "b": {"type": "Lit", "value": 8},
            },
            {"type": "Lt", "a": {"type": "Var", "name": "x"}, "b": {"type": "Var", "name": "y"}},
        ],
    }
    assert json.loads(json.dumps(result.term)) == result.term


def test_finite_and_membership_translators_use_namespaced_formula_module():
    quantified = translate_finite_formula("forall(x, D, x > 0)")
    assert quantified.term["type"] == "Not"
    assert quantified.term["arg"]["type"] == "Exists"
    membership = translate_membership("x \\in \\mathbb{N}", natural_minimum=0)
    assert membership.term == {
        "type": "Gte",
        "a": {"type": "Var", "name": "x"},
        "b": {"type": "Lit", "value": 0},
    }


def test_graph_model_lean_and_trace_modules_work_without_target_project(tmp_path):
    graph = ResearchGraph.create(
        "Check a syntax candidate", root=tmp_path, investigation_id="portable-model"
    )
    source = graph.record(
        "Source", locator={"root_id": "r", "path": "paper.md", "start_line": 1, "sha256": "a" * 64}
    )
    claim = graph.record("Claim", wording="A paper statement", claim_type="theorem", status="open")
    graph.link(claim["id"], "cites", source["id"])
    result = create_model_candidate(
        graph,
        claim_id=claim["id"],
        term={"type": "Lit", "value": True},
        residuals=[],
        reviewer="reader",
        review_note="Boolean toy candidate.",
    )
    candidate = result["candidate"]
    assert candidate["truth_assessed"] is False
    assert trace_chain(graph.state(), candidate["id"])["truth_assessed"] is False
    lean = export_lean(graph.state(), target=candidate["id"])
    assert candidate["id"] in lean


def test_imports_do_not_pull_in_ocelot_or_substrate_packages():
    code = """
import sys
import ozagia.research.formula
import ozagia.research.finite_formula
import ozagia.research.membership
import ozagia.research.formula_resolution
import ozagia.research.formula_sympy
import ozagia.research.native
import ozagia.research.trace
import ozagia.research.model
import ozagia.research.lean
assert not any(name == 'scripts' or name.startswith('scripts.') for name in sys.modules)
assert 'substrate' not in sys.modules
assert 'substrate_rs' not in sys.modules
"""
    proc = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_native_backend_is_optional_and_reports_unavailable(tmp_path, monkeypatch):
    graph = ResearchGraph.create(
        "Optional backend", root=tmp_path, investigation_id="no-native-backend"
    )
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "substrate_rs":
            raise ModuleNotFoundError("test backend absence")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    with pytest.raises(ResearchError, match="optional native projection backend unavailable"):
        project_research_graph(graph)
