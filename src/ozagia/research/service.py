"""Integration layer for research graph, source lookup and mechanical checks."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
import os
from pathlib import Path
import shutil
from typing import Any

from ozagia.research.graph import ResearchGraph, ResearchError
from ozagia.research.sources import build_index, retrieve, open_locator
from ozagia.research.checks import check_research_workflow, check_finite_math
from ozagia.research.provenance import (
    verify_engine_provenance as _default_verify_engine_provenance,
    verify_prereg as _default_verify_prereg,
    verify_prereg_git as _default_verify_prereg_git,
)
from ozagia.research.runtime import (
    engine_preamble as _default_engine_preamble,
    provenance_stamp as _default_provenance_stamp,
)


class ResearchService:
    def __init__(
        self,
        workspace: str | Path = ".",
        *,
        engine_preamble: Callable[[], Any] | None = None,
        provenance_stamp: Callable[[], dict[str, Any]] | None = None,
        verify_engine_provenance: Callable[..., dict[str, Any]] | None = None,
        verify_prereg: Callable[..., dict[str, Any]] | None = None,
        verify_prereg_git: Callable[..., dict[str, Any]] | None = None,
        lean_executable: (str | Path
                          | Callable[[], str | Path | None] | None) = None,
    ):
        """Create a service with optional project-specific runtime adapters.

        Defaults are portable Ozagia implementations. A target project can
        inject its own engine and provenance checks without making this
        package import the target repository.
        """
        self.workspace = Path(workspace).resolve()
        self._engine_preamble = (engine_preamble if engine_preamble is not None
                                 else _default_engine_preamble)
        self._provenance_stamp = (provenance_stamp if provenance_stamp is not None
                                  else _default_provenance_stamp)
        self._verify_engine_provenance = (
            verify_engine_provenance
            if verify_engine_provenance is not None
            else _default_verify_engine_provenance)
        self._verify_prereg = (verify_prereg if verify_prereg is not None
                               else _default_verify_prereg)
        self._verify_prereg_git = (verify_prereg_git if verify_prereg_git is not None
                                   else _default_verify_prereg_git)
        self._lean_executable = lean_executable

    def _resolve_lean_executable(self) -> str | None:
        """Resolve Lean with explicit environment overrides first."""
        configured = os.environ.get("OZAGIA_LEAN") or os.environ.get("OCELOT_LEAN")
        if configured:
            return configured
        injected = (self._lean_executable() if callable(self._lean_executable)
                    else self._lean_executable)
        if injected:
            return str(injected)
        project_lean = self.workspace / ".ozagia/tools/lean/bin/lean"
        return str(project_lean) if project_lean.is_file() else shutil.which("lean")

    def call(self, operation: str, args: dict):
        if operation == "init":
            graph = ResearchGraph.create(args["question"], root=self.workspace,
                                         mode=args.get("mode", "exploratory"),
                                         roots=args.get("roots", []))
            return graph.state()
        graph = ResearchGraph.load(args["session_id"], root=self.workspace)
        state = graph.state()
        if operation == "formula_parse_candidates":
            formulas = args.get("formulas")
            if not isinstance(formulas, list) or len(formulas) > 256:
                raise ResearchError("formulas must be an array containing at most 256 strings")
            from ozagia.research.formula_sympy import sympy_lark_candidate
            return {
                "status": "candidate_parses",
                "truth_assessed": False,
                "recorded": False,
                "candidates": [sympy_lark_candidate(formula) for formula in formulas],
                "note": "Optional SymPy/Lark interpretations are unverified candidates; no model is executed or recorded.",
            }
        if operation == "formula_resolve":
            from ozagia.research.formula_resolution import resolve_formula_ambiguity
            return resolve_formula_ambiguity(graph, args["formula"], scope_id=args["scope_id"])
        if operation == "native_graph":
            self._engine_preamble()
            from ozagia.research.native import project_research_graph
            projected = project_research_graph(state, query=args.get("query"), env=args.get("env"))
            projected.pop("substrate", None)
            projected["engine_provenance"] = self._provenance_stamp()
            return projected
        if operation == "paper_preview":
            from ozagia.research.paper import preview_paper
            return preview_paper(graph, root_id=args["root_id"], path=args["path"], max_formula_candidates=args.get("max_formula_candidates", 256), formula_offset=args.get("formula_offset", 0), latexml_path=args.get("latexml_path"))
        if operation == "paper_ingest":
            from ozagia.research.paper import ingest_paper
            return ingest_paper(graph, root_id=args["root_id"], path=args["path"], max_formula_candidates=args.get("max_formula_candidates", 256), formula_offset=args.get("formula_offset", 0), latexml_path=args.get("latexml_path"))
        if operation in {"formula_translate", "formula_model"}:
            from dataclasses import asdict
            from ozagia.research.formula import FormulaTranslationError, translate_formula
            from ozagia.research.finite_formula import FiniteFormulaError, translate_finite_formula
            import re
            text = args["formula"]
            assumptions = args.get("domain_assumptions") or {}
            if not isinstance(assumptions, dict):
                raise ResearchError("domain_assumptions must be an object")
            resolution = None
            context_ids = []
            translation_text = text
            if args.get("context_scope_id") is not None:
                if operation != "formula_model":
                    raise ResearchError("context_scope_id requires formula_model")
                if assumptions.get("implicit_products"):
                    raise ResearchError("do not combine graph-selected and manually supplied implicit products")
                scope_id = args["context_scope_id"]
                if not isinstance(text, str) or not text.strip():
                    raise ResearchError("context-selected formula must be non-empty text")
                if not any(e.get("type") == "scoped_by" and e.get("src") == args["claim_id"] and e.get("tgt") == scope_id for e in state["edges"]):
                    raise ResearchError("formula evidence must be explicitly scoped to context_scope_id")
                claim = next((n for n in state["nodes"] if n["id"] == args["claim_id"]), None)
                if claim is None or text not in (claim.get("raw_excerpt") or "") and text not in (claim.get("wording") or ""):
                    raise ResearchError("context-selected formula must occur in the scoped source node")
                from ozagia.research.formula_resolution import resolve_formula_ambiguity
                resolution = resolve_formula_ambiguity(graph, text, scope_id=scope_id)
                context_nodes = {n["id"]: n for n in state["nodes"]}
                natural_macros = [m for m in resolution.get("macro_evidence", [])
                                  if m.get("notation") == r"\NN" and m.get("expansion", "").strip() == r"\mathbb{N}"
                                  and context_nodes.get(m.get("symbol_id"), {}).get("arity") == 0]
                macro_only = (resolution.get("status") == "unresolved" and len(natural_macros) == 1
                              and len(resolution.get("macro_evidence", [])) == 1
                              and not resolution.get("unresolved_notation") and not resolution.get("tied"))
                if resolution.get("status") != "selected" and not macro_only:
                    return {"status": "unresolved_context", "resolution": resolution, "truth_assessed": False}
                if macro_only:
                    translation_text = re.sub(r"\\NN(?![A-Za-z])", lambda _: r"\mathbb{N}", text)
                    resolution["applied_macro_expansion"] = {"notation": r"\NN", "expansion": r"\mathbb{N}", "symbol_id": natural_macros[0]["symbol_id"]}
                else:
                    assumptions = {**assumptions, "implicit_products": resolution["implicit_products"]}
                selected_names = set(resolution["implicit_products"])
                selected_interpretations = [i for i in resolution.get("interpretations", []) if i.get("kind") == "implicit_product" and i.get("notation") in selected_names]
                symbol_ids = {sid for item in selected_interpretations for sid in item.get("symbol_ids", [])}
                symbol_ids.update(resolution.get("selected_symbol_ids", []))
                symbol_ids.update(m["symbol_id"] for m in natural_macros if macro_only)
                context_ids = sorted(symbol_ids | {d["definition_id"] for d in resolution.get("definitions", []) if d.get("symbol_id") in symbol_ids} | {scope_id})
            products = assumptions.get("implicit_products")
            finite = bool(re.search(r"(?:\\(?:forall|exists)\b|[∀∃]|\b(?:forall|exists)(?=\s*\(|\s+[A-Za-z_]))", translation_text)) if isinstance(translation_text, str) else False
            try:
                if finite and products:
                    raise FormulaTranslationError("implicit product mappings are not supported inside finite quantifiers")
                translation = asdict(translate_finite_formula(translation_text) if finite else
                                     translate_formula(translation_text, implicit_products=products))
            except (FormulaTranslationError, FiniteFormulaError) as exc:
                if not finite and isinstance(translation_text, str) and (r"\in" in translation_text or "∈" in translation_text):
                    from ozagia.research.membership import translate_membership
                    try:
                        if products:
                            raise FormulaTranslationError("implicit product mappings are not supported inside membership expressions")
                        translation = asdict(translate_membership(translation_text, natural_minimum=assumptions.get("natural_minimum")))
                    except FormulaTranslationError as membership_error:
                        return {"status": "unsupported", "reason": str(membership_error),
                                "residuals": list(membership_error.residuals), "truth_assessed": False}
                else:
                    return {"status": "unsupported", "reason": str(exc),
                            "residuals": list(getattr(exc, "residuals", (str(exc),))), "truth_assessed": False}
            translation["status"] = "translated"
            translation["truth_assessed"] = False
            if operation == "formula_translate":
                return translation
            from ozagia.research.model import create_model_candidate
            claim = next((n for n in state["nodes"] if n["id"] == args["claim_id"]), None)
            if claim is None:
                raise ResearchError("unknown claim id")
            formula = args["formula"]
            source_match = formula in (claim.get("raw_excerpt") or "") or formula in (claim.get("wording") or "")
            residuals = list(args.get("residuals", [])) + list(translation.get("residuals", []))
            if resolution is not None:
                residuals.extend(resolution.get("residuals", []))
            if not source_match:
                residuals.append("Formula was not found verbatim in the recorded claim; verify source-to-model correspondence.")
            residuals.append("Translation is a model candidate; the paper's variable domains and semantics require review.")
            bindings = args.get("bindings") or {}
            if not isinstance(bindings, dict) or any(name not in translation["variables"] for name in bindings):
                raise ResearchError("binding aliases must be variables in the translated formula")
            if bindings and translation.get("domains"):
                raise ResearchError("binding scalar dependencies into quantified formulas is not yet supported")
            for alias, dependency_id in bindings.items():
                dependency = next((n for n in state["nodes"] if n["id"] == dependency_id), None)
                if dependency is None:
                    raise ResearchError(f"unknown binding dependency {dependency_id!r}")
                actual = dependency.get("candidate_provenance", {}).get("translation", {}).get("result_sort", "unknown")
                expected = translation.get("variable_sorts", {}).get(alias, "unknown")
                compatible = actual == expected or "unknown" in (actual, expected) or (actual in {"int", "float", "number"} and expected in {"int", "float", "number"})
                if not compatible:
                    raise ResearchError(f"binding {alias!r} requires {expected} but dependency returns {actual}")
                if actual in {"unknown", "number"} and expected in {"int", "float"}:
                    residuals.append(f"Binding {alias!r} result requires runtime {expected} sort checking.")
                if actual == "unknown":
                    residuals.append(f"Binding {alias!r} has an unverified result sort; confirm it matches {expected}.")
            def bind_terms(value):
                if isinstance(value, dict):
                    if value.get("type") == "Var" and value.get("name") in bindings:
                        return {"type": "EvalSubgraph", "node": value}
                    return {k: bind_terms(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [bind_terms(v) for v in value]
                return value
            result = create_model_candidate(graph, claim_id=args["claim_id"], term=bind_terms(translation["term"]),
                bindings=bindings, assumption_ids=args.get("assumption_ids"),
                context_ids=context_ids,
                residuals=residuals, reviewer=args["reviewer"], review_note=args["review_note"],
                provenance={"formula": formula, "found_verbatim_in_claim": source_match,
                            "domain_assumptions": assumptions,
                            "context_resolution": resolution,
                            "translation": translation})
            result["translation"] = translation
            result["found_verbatim_in_claim"] = source_match
            if resolution is not None:
                result["context_resolution"] = resolution
            return result
        if operation == "model_candidate":
            from ozagia.research.model import create_model_candidate
            return create_model_candidate(graph, claim_id=args["claim_id"], term=args["term"],
                bindings=args.get("bindings"), assumption_ids=args.get("assumption_ids"),
                residuals=args["residuals"], reviewer=args["reviewer"],
                review_note=args["review_note"], provenance=args.get("provenance"))
        if operation == "record":
            return graph.record(args["kind"], **args["fields"])
        if operation == "link":
            fields = args.get("fields", {})
            return graph.link(args["src"], args["relation"], args["tgt"],
                              provenance=fields.get("provenance"), rationale=fields.get("rationale"))
        if operation == "inspect":
            return graph.inspect(args["node_id"], transitive=args.get("transitive", False)) if args.get("node_id") else state
        if operation == "trace":
            from ozagia.research.trace import trace_chain
            return trace_chain(state, args["node_id"], max_depth=args.get("max_depth", 64))
        if operation == "execute":
            self._engine_preamble()
            from substrate_rs import Substrate
            from ozagia.research.trace import execute
            by_id = {n["id"]: n for n in state["nodes"]}
            pending, seen = [args["node_id"]], set()
            while pending:
                current_id = pending.pop()
                if current_id in seen:
                    continue
                seen.add(current_id)
                target = by_id.get(current_id, {})
                pending.extend((target.get("bindings") or {}).values())
                translation = target.get("candidate_provenance", {}).get("translation", {})
                if translation:
                    env = args.get("env", {})
                    if not isinstance(env, dict):
                        raise ResearchError("formula environment must be an object")
                    bound = set((target.get("bindings") or {}).keys())
                    if translation.get("domains"):
                        from types import SimpleNamespace
                        from ozagia.research.finite_formula import validate_environment
                        try:
                            validate_environment(SimpleNamespace(**translation), env)
                        except ValueError as exc:
                            raise ResearchError(str(exc)) from exc
                    import math
                    for name, sort in translation.get("variable_sorts", {}).items():
                        if name in bound:
                            continue
                        if name not in env:
                            raise ResearchError(f"formula input {name!r} is missing")
                        value = env[name]
                        if type(value) not in (bool, int, float, str):
                            raise ResearchError(f"formula input {name!r} must be Boolean, numeric, or a symbol string")
                        if sort == "symbol" and type(value) is not str:
                            raise ResearchError(f"formula input {name!r} must be a symbol string")
                        if type(value) is float and not math.isfinite(value):
                            raise ResearchError(f"formula input {name!r} must be finite")
                        if sort in {"number", "float"} and type(value) not in (int, float):
                            raise ResearchError(f"formula input {name!r} must be numeric")
                        if sort == "bool" and type(value) is not bool:
                            raise ResearchError(f"formula input {name!r} must be Boolean")
                        if sort == "int" and type(value) is not int:
                            raise ResearchError(f"formula input {name!r} must be an integer")
                        if type(value) is int and not -(2**63) <= value < 2**63:
                            raise ResearchError(f"formula input {name!r} is outside native i64 range")
                    for left, right in translation.get("equality_sort_constraints", []):
                        if left not in bound and right not in bound:
                            a, b = env[left], env[right]
                            if type(a) is not type(b):
                                raise ResearchError(f"formula equality inputs {left!r} and {right!r} require compatible sorts")
            result = execute(state, args["node_id"], Substrate(), env=args.get("env", {}),
                             max_depth=args.get("max_depth", 32), max_nodes=args.get("max_nodes", 64))
            result["engine_provenance"] = self._provenance_stamp()
            return result
        if operation == "check_lean":
            import hashlib
            import subprocess
            import tempfile
            from ozagia.research.lean import export_lean
            source = export_lean(state, target=args.get("node_id"))
            executable = self._resolve_lean_executable()
            if not executable:
                raise ResearchError("Lean executable unavailable; set OZAGIA_LEAN or install Lean on PATH")
            digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
            with tempfile.TemporaryDirectory(prefix="ocelot-lean-") as directory:
                source_path = Path(directory) / "ResearchExport.lean"
                source_path.write_text(source, encoding="utf-8")
                try:
                    run = subprocess.run([executable, "-j", "1", str(source_path)], capture_output=True, text=True, timeout=60, check=False)
                    version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=15, check=False)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise ResearchError(f"Lean check unavailable: {exc}") from exc
            return {"status": "accepted" if run.returncode == 0 else "rejected",
                    "kernel_checked": run.returncode == 0, "exit_code": run.returncode,
                    "source_sha256": digest, "lean_executable": str(executable),
                    "lean_version": version.stdout.strip(), "stdout": run.stdout, "stderr": run.stderr,
                    "note": "Lean checks generated declarations; source-to-model faithfulness and domain claims are not established."}
        if operation == "export_lean":
            from ozagia.research.lean import export_lean
            return {"lean": export_lean(state, target=args.get("node_id")),
                    "kernel_checked": False,
                    "note": "Generated definitions; no domain claim or Lean proof is certified."}
        if operation == "context":
            index = build_index(state["roots"])
            result = retrieve(index, args["query"], limit=args.get("limit", 8))
            result["mechanical_check"] = graph.check()
            result["recorded_claims"] = [graph.inspect(n["id"]) for n in state["nodes"] if n["type"] == "Claim"]
            frames = [n for n in state["nodes"] if n["type"] == "Frame"]
            ledger_fields = ("boundary", "state", "rules", "grammar", "possibilities", "realisation", "residual", "cost", "grounding", "test")
            result["frame_ledger"] = [{"node_id": f["id"], "missing": [k for k in ledger_fields if k not in f]} for f in frames]
            if not frames:
                result["frame_ledger"] = [{"node_id": None, "missing": list(ledger_fields)}]
            result["source_note"] = "Lexical retrieval; source links are not verified support."
            return result
        if operation in {"check", "export"}:
            nodes = state["nodes"]
            protocols = [n for n in nodes if n["type"] == "Protocol"]
            runs = [n for n in nodes if n["type"] == "Run"]
            investigation = {"mode": state["mode"]}
            for node in nodes:
                if node["type"] == "Frame" and node.get("empirical_exemption_reason"):
                    investigation["empirical_exemption_reason"] = node["empirical_exemption_reason"]
            run = dict(runs[-1]) if runs else None
            # V0 has no execution capture adapter: every supplied receipt is an
            # assertion, regardless of the status string entered by the caller.
            if run is not None:
                run["receipt_status"] = "manual"
            protocol = next((p for p in protocols if run and
                             p.get("revision") == run.get("protocol_revision")), None)
            if run is None and protocols:
                protocol = protocols[-1]
            if protocol is not None:
                protocol = dict(protocol)
                # Graph creation time is the earliest evidence of our recording;
                # an author-supplied earlier date cannot make a late entry prospective.
                protocol["recorded_at"] = protocol["created_at"]
            workflow = check_research_workflow(investigation, protocol, run)
            workflow["notices"].append("Protocol revisions, dates and engine status are user assertions; independently verify them.")
            if run is None and state["mode"] == "confirmatory":
                workflow["confirmatory_eligible"] = False
                workflow["blockers"].append("no run receipt has been recorded")
            run_workflows = []
            for prior_run in runs:
                asserted = dict(prior_run, receipt_status="manual")
                matching = next((p for p in protocols if p.get("revision") == prior_run.get("protocol_revision")), None)
                if matching is not None:
                    matching = dict(matching, recorded_at=matching["created_at"])
                run_workflows.append({"node_id": prior_run["id"], **check_research_workflow(investigation, matching, asserted)})
            finite = [{"node_id": n["id"], **check_finite_math(
                domain=n.get("domain"), bounds=n.get("bounds"),
                oracle_independent=n.get("oracle_independent"),
                verification_label=n.get("verification_label", "finite_verification"),
                oracle_description=n.get("oracle_description"))}
                for n in nodes if n["type"] == "Run" and n.get("finite_check") is True]
            provenance = {"prereg_git": None, "seed_claim": None, "engine": None}
            if protocol and isinstance(protocol.get("prereg_git"), dict):
                spec = protocol["prereg_git"]
                root = next((r for r in state["roots"] if r.get("root_id") == spec.get("root_id")), None)
                rel = spec.get("path")
                if root and isinstance(rel, str) and rel in root.get("files", []):
                    provenance["prereg_git"] = self._verify_prereg_git(
                        root["path"], spec.get("commit_oid"), rel)
                    if protocol.get("check_seed_claim") is True:
                        provenance["seed_claim"] = self._verify_prereg(
                            Path(root["path"]) / rel, repo_root=root["path"])
                else:
                    provenance["prereg_git"] = {"status": "unavailable", "verified": False, "reason": "prereg must be a selected file in a configured root"}
            if run and run.get("uses_ocelot") is True:
                provenance["engine"] = self._verify_engine_provenance(
                    run.get("engine_stamp"))
            citations = []
            sources = [n for n in nodes if n["type"] == "Source"]
            if sources:
                index = build_index(state["roots"])
                for source in sources:
                    locator = source.get("locator")
                    citations.append({"node_id": source["id"], **(
                        open_locator(index, locator) if isinstance(locator, dict)
                        else {"status": "unavailable", "reason": "source locator missing"})})
            checks = {"graph": graph.check(), "workflow": workflow,
                      "run_workflows": run_workflows, "finite_checks": finite, "citations": citations, "provenance": provenance,
                      "truth_assessed": False}
            if operation == "check":
                return checks
            result = graph.export()
            result["checks"] = checks
            result["markdown"] += "\n## Workflow and source checks\n\n```json\n" + json.dumps(checks, indent=2, ensure_ascii=False) + "\n```\n"
            return result
        raise ResearchError(f"unknown operation: {operation}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Research helper; arguments and output are JSON.")
    parser.add_argument("operation", choices=["init", "context", "native_graph", "paper_preview", "paper_ingest", "formula_parse_candidates", "formula_resolve", "formula_translate", "formula_model", "model_candidate", "record", "link", "inspect", "trace", "execute", "export_lean", "check_lean", "check", "export"])
    parser.add_argument("--workspace", default=".", help="Workspace containing Ozagia research state")
    parser.add_argument("--args", default="{}", help="JSON object of operation arguments")
    parser.add_argument("--lean", action="store_true", help="For export_lean, print Lean source rather than JSON")
    options = parser.parse_args(argv)
    try:
        args = json.loads(options.args)
        if not isinstance(args, dict):
            raise ValueError("--args must be a JSON object")
        result = ResearchService(options.workspace).call(options.operation, args)
        if options.lean:
            if options.operation != "export_lean":
                raise ValueError("--lean requires export_lean")
            print(result["lean"], end="")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, ResearchError, OSError) as exc:
        parser.exit(2, f"{type(exc).__name__}: {exc}\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
