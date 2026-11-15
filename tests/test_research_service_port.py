from pathlib import Path
from types import SimpleNamespace
import builtins
import sys

import pytest

from ozagia.research.service import ResearchService


def test_cross_root_claim_evidence_and_stale_source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    path = root / "evidence.md"
    path.write_text("# Residual\nAn arbitrary residual remains.\n", encoding="utf-8")
    svc = ResearchService(tmp_path)
    state = svc.call(
        "init",
        {
            "question": "Is residual eliminated?",
            "roots": [{"root_id": "ge", "path": str(root), "files": ["evidence.md"]}],
        },
    )
    sid = state["investigation_id"]
    context = svc.call("context", {"session_id": sid, "query": "arbitrary residual"})
    locator = context["results"][0]["citation"]
    src = svc.call("record", {"session_id": sid, "kind": "Source", "fields": {"locator": locator}})
    claim = svc.call(
        "record",
        {
            "session_id": sid,
            "kind": "Claim",
            "fields": {
                "wording": "Residual disappears",
                "scope": "arbitrary CNF",
                "claim_type": "hypothesis",
                "author": "researcher",
                "status": "open",
            },
        },
    )
    svc.call(
        "link", {"session_id": sid, "src": src["id"], "relation": "opposes", "tgt": claim["id"]}
    )
    assert (
        svc.call("inspect", {"session_id": sid, "node_id": claim["id"]})["edges"][0]["type"]
        == "opposes"
    )
    assert svc.call("check", {"session_id": sid})["citations"][0]["status"] == "current"
    path.write_text("# Residual\nChanged.\n", encoding="utf-8")
    result = ResearchService(tmp_path).call("export", {"session_id": sid})
    assert result["checks"]["citations"][0]["status"] == "stale"
    assert "Residual disappears" in result["markdown"]
    assert result["checks"]["truth_assessed"] is False


def test_project_runtime_and_provenance_callbacks_are_injectable(
        tmp_path, monkeypatch):
    from ozagia.research import native, trace

    source_root = tmp_path / "source"
    source_root.mkdir()
    prereg = source_root / "prereg.md"
    prereg.write_text("prospective plan", encoding="utf-8")
    calls = []

    def engine_preamble():
        calls.append(("preamble",))

    def provenance_stamp():
        calls.append(("stamp",))
        return {"source_match": "project-test", "sha256": "test-hash"}

    def verify_engine_provenance(recorded):
        calls.append(("verify_engine", recorded))
        return {"status": "project-verified", "matches_recorded": True}

    def verify_prereg(path, *, repo_root):
        calls.append(("verify_prereg", Path(path), repo_root))
        return {"status": "project-prereg"}

    def verify_prereg_git(repo_path, commit_oid, relative_path):
        calls.append(("verify_git", repo_path, commit_oid, relative_path))
        return {"status": "project-git"}

    svc = ResearchService(
        tmp_path,
        engine_preamble=engine_preamble,
        provenance_stamp=provenance_stamp,
        verify_engine_provenance=verify_engine_provenance,
        verify_prereg=verify_prereg,
        verify_prereg_git=verify_prereg_git,
    )
    sid = svc.call("init", {
        "question": "check injected project hooks",
        "roots": [{"root_id": "local", "path": str(source_root),
                   "files": ["prereg.md"]}],
    })["investigation_id"]

    monkeypatch.setattr(native, "project_research_graph",
                        lambda *_args, **_kwargs: {"substrate": "internal"})
    projection = svc.call("native_graph", {"session_id": sid})
    assert "substrate" not in projection
    assert projection["engine_provenance"]["source_match"] == "project-test"

    monkeypatch.setattr(trace, "execute", lambda *_args, **_kwargs: {
        "value": True, "truth_assessed": False})
    monkeypatch.setitem(sys.modules, "substrate_rs",
                        SimpleNamespace(Substrate=object))
    claim = svc.call("record", {
        "session_id": sid, "kind": "Claim",
        "fields": {"term": {"type": "Lit", "value": True}},
    })
    executed = svc.call("execute", {
        "session_id": sid, "node_id": claim["id"],
    })
    assert executed["engine_provenance"]["source_match"] == "project-test"

    svc.call("record", {
        "session_id": sid, "kind": "Protocol",
        "fields": {"revision": "r1", "prereg_git": {
            "root_id": "local", "path": "prereg.md", "commit_oid": "abc"},
            "check_seed_claim": True},
    })
    svc.call("record", {
        "session_id": sid, "kind": "Run",
        "fields": {"protocol_revision": "r1", "uses_ocelot": True,
                    "engine_stamp": {"sha256": "recorded"}},
    })

    real_import = builtins.__import__

    def reject_target_imports(name, *args, **kwargs):
        if name.split(".", 1)[0] in {"domains", "scripts"}:
            raise AssertionError(f"portable service imported target module {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_target_imports)
    result = svc.call("check", {"session_id": sid})["provenance"]
    assert result["prereg_git"] == {"status": "project-git"}
    assert result["seed_claim"] == {"status": "project-prereg"}
    assert result["engine"] == {
        "status": "project-verified", "matches_recorded": True}
    assert ("verify_git", str(source_root), "abc", "prereg.md") in calls
    assert ("verify_prereg", prereg, str(source_root)) in calls
    assert ("verify_engine", {"sha256": "recorded"}) in calls
    assert calls.count(("preamble",)) == 2
    assert calls.count(("stamp",)) == 2


def test_lean_executable_precedence(tmp_path, monkeypatch):
    from ozagia.research import service as service_module

    project_lean = tmp_path / ".ozagia/tools/lean/bin/lean"
    project_lean.parent.mkdir(parents=True)
    project_lean.write_text("", encoding="utf-8")
    injected_calls = []
    svc = ResearchService(
        tmp_path,
        lean_executable=lambda: injected_calls.append("called") or "/injected/lean",
    )

    monkeypatch.setenv("OZAGIA_LEAN", "/env/ozagia-lean")
    monkeypatch.setenv("OCELOT_LEAN", "/env/ocelot-lean")
    assert svc._resolve_lean_executable() == "/env/ozagia-lean"
    assert injected_calls == []
    monkeypatch.delenv("OZAGIA_LEAN")
    assert svc._resolve_lean_executable() == "/env/ocelot-lean"
    assert injected_calls == []

    monkeypatch.delenv("OCELOT_LEAN")
    assert svc._resolve_lean_executable() == "/injected/lean"
    assert injected_calls == ["called"]

    no_override = ResearchService(tmp_path)
    assert no_override._resolve_lean_executable() == str(project_lean)
    project_lean.unlink()
    monkeypatch.setattr(service_module.shutil, "which", lambda name: (
        "/path/lean" if name == "lean" else None))
    assert no_override._resolve_lean_executable() == "/path/lean"


def test_native_three_step_research_chain_and_lean_export(tmp_path):
    pytest.importorskip("substrate_rs")
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {"question": "compute a chain"})["investigation_id"]

    def rec(term, bindings=None):
        return svc.call(
            "record",
            {
                "session_id": sid,
                "kind": "Claim",
                "fields": {
                    "wording": "explicit computation",
                    "term": term,
                    "bindings": bindings or {},
                },
            },
        )

    def link(src, dep):
        svc.call(
            "link",
            {"session_id": sid, "src": src["id"], "relation": "derived_from", "tgt": dep["id"]},
        )

    first = rec({"type": "Var", "name": "x"})
    second = rec(
        {"type": "Not", "arg": {"type": "EvalSubgraph", "node": {"type": "Var", "name": "first"}}},
        {"first": first["id"]},
    )
    link(second, first)
    third = rec(
        {
            "type": "And",
            "items": [
                {"type": "EvalSubgraph", "node": {"type": "Var", "name": "second"}},
                {"type": "Lit", "value": True},
            ],
        },
        {"second": second["id"]},
    )
    link(third, second)
    trace = svc.call("trace", {"session_id": sid, "node_id": third["id"]})
    assert set(n["id"] for n in trace["nodes"]) == {first["id"], second["id"], third["id"]}
    result = svc.call("execute", {"session_id": sid, "node_id": third["id"], "env": {"x": False}})
    assert result["value"] is True
    assert result["truth_assessed"] is False
    assert result["engine_provenance"]["source_provenance"] == "unverified_external_backend"
    lean = svc.call("export_lean", {"session_id": sid, "node_id": third["id"]})
    assert lean["kernel_checked"] is False
    assert "Bool" in lean["lean"] and first["id"] in lean["lean"] and third["id"] in lean["lean"]


def test_formula_models_check_runtime_sorts(tmp_path):
    pytest.importorskip("substrate_rs")
    from ozagia.research.graph import ResearchError

    root = tmp_path / "paper"
    root.mkdir()
    (root / "case.tex").write_text(
        r"\begin{theorem}\label{t} The expression $x + 1 = 2$ is an example.\end{theorem}",
        encoding="utf-8",
    )
    svc = ResearchService(tmp_path)
    sid = svc.call(
        "init",
        {
            "question": "formula",
            "roots": [{"root_id": "r", "path": str(root), "files": ["case.tex"]}],
        },
    )["investigation_id"]
    claim = svc.call("paper_ingest", {"session_id": sid, "root_id": "r", "path": "case.tex"})[
        "claims"
    ][0]
    model = svc.call(
        "formula_model",
        {
            "session_id": sid,
            "claim_id": claim["id"],
            "formula": "x + 1 = 2",
            "reviewer": "researcher",
            "review_note": "Integer interpretation.",
        },
    )
    node = model["candidate"]["id"]
    assert (
        svc.call("execute", {"session_id": sid, "node_id": node, "env": {"x": 1}})["value"] is True
    )
    with pytest.raises(ResearchError, match="numeric"):
        svc.call("execute", {"session_id": sid, "node_id": node, "env": {"x": True}})
    with pytest.raises(ResearchError, match="missing"):
        svc.call("execute", {"session_id": sid, "node_id": node, "env": {}})


def test_real_shape_finite_membership_to_native_and_lean(tmp_path):
    pytest.importorskip("substrate_rs")
    root = tmp_path / "paper"
    root.mkdir()
    formula = r"\epsilon,\mu\in\{0,1\}"
    (root / "case.tex").write_text(
        r"\begin{theorem}\label{t} Consider $" + formula + r"$.\end{theorem}", encoding="utf-8"
    )
    svc = ResearchService(tmp_path)
    sid = svc.call(
        "init",
        {
            "question": "membership",
            "roots": [{"root_id": "r", "path": str(root), "files": ["case.tex"]}],
        },
    )["investigation_id"]
    claim = svc.call("paper_ingest", {"session_id": sid, "root_id": "r", "path": "case.tex"})[
        "claims"
    ][0]
    result = svc.call(
        "formula_model",
        {
            "session_id": sid,
            "claim_id": claim["id"],
            "formula": formula,
            "reviewer": "researcher",
            "review_note": "Finite integer set.",
        },
    )
    assert result["found_verbatim_in_claim"]
    node = result["candidate"]["id"]
    assert (
        svc.call("execute", {"session_id": sid, "node_id": node, "env": {"epsilon": 1, "mu": 0}})[
            "value"
        ]
        is True
    )
    assert (
        svc.call("execute", {"session_id": sid, "node_id": node, "env": {"epsilon": 2, "mu": 0}})[
            "value"
        ]
        is False
    )
    assert svc.call("export_lean", {"session_id": sid, "node_id": node})["kernel_checked"] is False


def test_model_directly_from_sourced_displayed_formula_candidate(tmp_path):
    pytest.importorskip("substrate_rs")
    root = tmp_path / "paper"
    root.mkdir()
    formula = r"\epsilon,\mu\in\{0,1\}"
    (root / "case.tex").write_text("A finite example.\n\\[" + formula + "\\]\n", encoding="utf-8")
    svc = ResearchService(tmp_path)
    sid = svc.call(
        "init",
        {
            "question": "model display",
            "roots": [{"root_id": "r", "path": str(root), "files": ["case.tex"]}],
        },
    )["investigation_id"]
    imported = svc.call("paper_ingest", {"session_id": sid, "root_id": "r", "path": "case.tex"})
    evidence = imported["formula_evidence"][0]
    model = svc.call(
        "formula_model",
        {
            "session_id": sid,
            "claim_id": evidence["id"],
            "formula": formula,
            "reviewer": "researcher",
            "review_note": "Integer membership.",
        },
    )
    assert model["found_verbatim_in_claim"]
    node = model["candidate"]["id"]
    assert (
        svc.call("execute", {"session_id": sid, "node_id": node, "env": {"epsilon": 0, "mu": 1}})[
            "value"
        ]
        is True
    )
    import shutil

    repo = Path(__file__).resolve().parents[1]
    if (repo / "var/tools/lean/lean-4.33.1-linux/bin/lean").is_file() or shutil.which("lean"):
        assert svc.call("check_lean", {"session_id": sid, "node_id": node})["status"] == "accepted"


def test_service_paginates_paper_formula_candidates(tmp_path):
    root = tmp_path / "paper"
    root.mkdir()
    (root / "case.tex").write_text("First $x=1$ then $x=2$ then $x=3$.\n", encoding="utf-8")
    svc = ResearchService(tmp_path)
    sid = svc.call(
        "init",
        {
            "question": "pages",
            "roots": [{"root_id": "r", "path": str(root), "files": ["case.tex"]}],
        },
    )["investigation_id"]
    common = {"session_id": sid, "root_id": "r", "path": "case.tex"}
    first = svc.call("paper_ingest", {**common, "max_formula_candidates": 1})
    assert first["formula_has_more"]
    assert first["formula_next_offset"] == 1
    second = svc.call("paper_ingest", {**common, "formula_offset": 1, "max_formula_candidates": 1})
    assert second["formula_evidence"][0]["wording"] == "x=2"
    assert first["formula_evidence"][0]["id"] != second["formula_evidence"][0]["id"]


def test_service_natural_membership_requires_explicit_domain(tmp_path):
    svc = ResearchService(tmp_path)
    sid = svc.call("init", {"question": "naturals"})["investigation_id"]
    args = {"session_id": sid, "formula": r"m,n\in\NN"}
    assert svc.call("formula_translate", args)["status"] == "unsupported"
    out = svc.call("formula_translate", {**args, "domain_assumptions": {"natural_minimum": 0}})
    assert out["status"] == "translated"
    assert any("i64" in r for r in out["residuals"])


def test_sourced_implicit_product_requires_explicit_mapping(tmp_path):
    pytest.importorskip("substrate_rs")
    root = tmp_path / "paper"
    root.mkdir()
    formula = "E_0=mc^2"
    (root / "case.tex").write_text("Example $" + formula + "$.", encoding="utf-8")
    svc = ResearchService(tmp_path)
    sid = svc.call(
        "init",
        {
            "question": "product",
            "roots": [{"root_id": "r", "path": str(root), "files": ["case.tex"]}],
        },
    )["investigation_id"]
    evidence = svc.call("paper_ingest", {"session_id": sid, "root_id": "r", "path": "case.tex"})[
        "formula_evidence"
    ][0]
    result = svc.call(
        "formula_model",
        {
            "session_id": sid,
            "claim_id": evidence["id"],
            "formula": formula,
            "reviewer": "researcher",
            "review_note": "Explicit product interpretation.",
            "domain_assumptions": {"implicit_products": {"mc": ["m", "c"]}},
        },
    )
    assert any("caller" in residual for residual in result["translation"]["residuals"])
    node = result["candidate"]["id"]
    assert (
        svc.call(
            "execute", {"session_id": sid, "node_id": node, "env": {"E_0": 18, "m": 2, "c": 3}}
        )["value"]
        is True
    )
    assert (
        svc.call(
            "execute", {"session_id": sid, "node_id": node, "env": {"E_0": 36, "m": 2, "c": 3}}
        )["value"]
        is False
    )
    import shutil

    repo = Path(__file__).resolve().parents[1]
    if (repo / "var/tools/lean/lean-4.33.1-linux/bin/lean").is_file() or shutil.which("lean"):
        assert svc.call("check_lean", {"session_id": sid, "node_id": node})["status"] == "accepted"


def test_scoped_graph_context_selects_and_traces_executable_product(tmp_path):
    pytest.importorskip("substrate_rs")
    root = tmp_path / "paper"
    root.mkdir()
    formula = "E_0=mc^2"
    (root / "case.tex").write_text("The equation is $" + formula + "$.", encoding="utf-8")
    svc = ResearchService(tmp_path)
    sid = svc.call(
        "init",
        {
            "question": "context",
            "roots": [{"root_id": "r", "path": str(root), "files": ["case.tex"]}],
        },
    )["investigation_id"]
    evidence = svc.call("paper_ingest", {"session_id": sid, "root_id": "r", "path": "case.tex"})[
        "formula_evidence"
    ][0]
    frame = svc.call(
        "record", {"session_id": sid, "kind": "Frame", "fields": {"wording": "paper scope"}}
    )

    def link(src, relation, tgt):
        svc.call("link", {"session_id": sid, "src": src, "relation": relation, "tgt": tgt})

    link(evidence["id"], "scoped_by", frame["id"])
    symbol_ids = []
    for name in ("m", "c"):
        symbol = svc.call(
            "record",
            {
                "session_id": sid,
                "kind": "MathematicalSymbol",
                "fields": {"name": name, "arity": 0, "sort": "number"},
            },
        )
        symbol_ids.append(symbol["id"])
        link(symbol["id"], "scoped_by", frame["id"])
    common = {
        "session_id": sid,
        "claim_id": evidence["id"],
        "formula": formula,
        "context_scope_id": frame["id"],
        "reviewer": "researcher",
        "review_note": "Compare notation with scoped paper symbols.",
    }
    result = svc.call("formula_model", common)
    assert "context_resolution" in result, result
    assert result["context_resolution"]["implicit_products"] == {"mc": ["m", "c"]}
    node = result["candidate"]["id"]
    assert (
        svc.call(
            "execute", {"session_id": sid, "node_id": node, "env": {"E_0": 18, "m": 2, "c": 3}}
        )["value"]
        is True
    )
    trace = svc.call("trace", {"session_id": sid, "node_id": node})
    assert set(symbol_ids).issubset({n["id"] for n in trace["nodes"]})
    conflict = svc.call(
        "record",
        {
            "session_id": sid,
            "kind": "MathematicalSymbol",
            "fields": {"name": "mc", "arity": 0, "sort": "number"},
        },
    )
    link(conflict["id"], "scoped_by", frame["id"])
    before = len(svc.call("inspect", {"session_id": sid})["nodes"])
    tied = svc.call("formula_model", common)
    assert tied["status"] == "unresolved_context"
    assert len(svc.call("inspect", {"session_id": sid})["nodes"]) == before


def test_paper_macro_context_models_naturals_with_explicit_convention(tmp_path):
    pytest.importorskip("substrate_rs")
    root = tmp_path / "paper"
    root.mkdir()
    formula = r"m,n\in\NN"
    (root / "case.tex").write_text(
        "\\newcommand\\NN{\\mathbb{N}}\nLater $" + formula + "$.\n", encoding="utf-8"
    )
    svc = ResearchService(tmp_path)
    sid = svc.call(
        "init",
        {
            "question": "source context",
            "roots": [{"root_id": "r", "path": str(root), "files": ["case.tex"]}],
        },
    )["investigation_id"]
    imported = svc.call("paper_ingest", {"session_id": sid, "root_id": "r", "path": "case.tex"})
    evidence = next(e for e in imported["formula_evidence"] if e["wording"] == formula)
    scope = next(
        e["tgt"]
        for e in svc.call("inspect", {"session_id": sid})["edges"]
        if e["type"] == "scoped_by" and e["src"] == evidence["id"]
    )
    common = {
        "session_id": sid,
        "claim_id": evidence["id"],
        "formula": formula,
        "context_scope_id": scope,
        "reviewer": "researcher",
        "review_note": "Use the sourced macro declaration.",
    }
    missing = svc.call("formula_model", common)
    assert missing["status"] == "unsupported"
    model = svc.call("formula_model", {**common, "domain_assumptions": {"natural_minimum": 0}})
    macro = model["context_resolution"]["applied_macro_expansion"]["symbol_id"]
    assert (
        svc.call(
            "execute",
            {"session_id": sid, "node_id": model["candidate"]["id"], "env": {"m": 0, "n": 2}},
        )["value"]
        is True
    )
    assert (
        svc.call(
            "execute",
            {"session_id": sid, "node_id": model["candidate"]["id"], "env": {"m": -1, "n": 2}},
        )["value"]
        is False
    )
    trace = svc.call("trace", {"session_id": sid, "node_id": model["candidate"]["id"]})
    assert macro in {n["id"] for n in trace["nodes"]}


def test_source_declared_symbols_select_product_in_same_statement(tmp_path):
    pytest.importorskip("substrate_rs")
    root = tmp_path / "paper"
    root.mkdir()
    (root / "case.tex").write_text(
        r"\begin{theorem}Let $m,c$ be real numbers. Then $E=mc^2$.\end{theorem}", encoding="utf-8"
    )
    svc = ResearchService(tmp_path)
    sid = svc.call(
        "init",
        {
            "question": "local source context",
            "roots": [{"root_id": "r", "path": str(root), "files": ["case.tex"]}],
        },
    )["investigation_id"]
    imported = svc.call("paper_ingest", {"session_id": sid, "root_id": "r", "path": "case.tex"})
    evidence = next(e for e in imported["formula_evidence"] if e["wording"] == "E=mc^2")
    state = svc.call("inspect", {"session_id": sid})
    scope = next(
        e["tgt"] for e in state["edges"] if e["type"] == "scoped_by" and e["src"] == evidence["id"]
    )
    result = svc.call(
        "formula_model",
        {
            "session_id": sid,
            "claim_id": evidence["id"],
            "formula": "E=mc^2",
            "context_scope_id": scope,
            "reviewer": "researcher",
            "review_note": "Use the declaration in the same theorem.",
        },
    )
    assert result["context_resolution"]["implicit_products"] == {"mc": ["m", "c"]}
    assert (
        svc.call(
            "execute",
            {
                "session_id": sid,
                "node_id": result["candidate"]["id"],
                "env": {"E": 18, "m": 2, "c": 3},
            },
        )["value"]
        is True
    )
    trace = svc.call("trace", {"session_id": sid, "node_id": result["candidate"]["id"]})
    assert any(n["type"] == "MathematicalDefinition" for n in trace["nodes"])
