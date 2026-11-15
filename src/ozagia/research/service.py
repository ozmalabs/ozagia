"""Integration layer for research graph, source lookup and mechanical checks."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
import os
from pathlib import Path
import shutil
from typing import Any

from ozagia.config import state_path
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
        fetcher: Callable[..., Any] | None = None,
        zotero_items: Callable[..., Any] | None = None,
        pandoc_runner: Callable[..., Any] | None = None,
        pdf_extractor: Callable[..., Any] | None = None,
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
        self._fetcher = fetcher
        self._zotero_items = zotero_items
        self._pandoc_runner = pandoc_runner
        self._pdf_extractor = pdf_extractor

    def _fetch(self) -> Callable[..., Any]:
        if self._fetcher is not None:
            return self._fetcher
        from ozagia.research.fetch import http_get
        return http_get

    def _zotero(self) -> Callable[..., Any]:
        if self._zotero_items is not None:
            return self._zotero_items
        from ozagia.research.zotero import local_items
        return local_items

    def _cache_dir(self) -> Path:
        return state_path(self.workspace, "research", "cache")

    def _pdf(self) -> Callable[..., Any]:
        if self._pdf_extractor is not None:
            return self._pdf_extractor
        from ozagia.research.pdf import extract_pdf
        return extract_pdf

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
            return preview_paper(graph, root_id=args["root_id"], path=args["path"], max_formula_candidates=args.get("max_formula_candidates", 256), formula_offset=args.get("formula_offset", 0), latexml_path=args.get("latexml_path"), bibliography_path=args.get("bibliography_path"))
        if operation == "paper_ingest":
            from ozagia.research.paper import ingest_paper
            return ingest_paper(graph, root_id=args["root_id"], path=args["path"], max_formula_candidates=args.get("max_formula_candidates", 256), formula_offset=args.get("formula_offset", 0), latexml_path=args.get("latexml_path"), bibliography_path=args.get("bibliography_path"))
        if operation in {"ref_preview", "ref_import"}:
            import hashlib
            from ozagia.research import references as refs
            from ozagia.research.paper import _root_file
            root, reference_file = _root_file(graph, args["root_id"], args["path"])
            try:
                raw = reference_file.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise ResearchError(f"cannot read selected reference file: {exc}") from exc
            entries = refs.parse_reference_file(args["path"], text)
            digest = hashlib.sha256(raw).hexdigest()
            existing = [
                refs.find_reference(state, entry) for entry in entries
            ]
            preview_entries = [
                {
                    **entry,
                    "duplicate_of": match["id"] if match else None,
                }
                for entry, match in zip(entries, existing)
            ]
            source = {
                "root_id": args["root_id"],
                "path": args["path"],
                "sha256": digest,
                "repository": root.get("repository", args["root_id"]),
                "format": reference_file.suffix.lower().lstrip("."),
            }
            if operation == "ref_preview":
                return {
                    "references": preview_entries,
                    "count": len(entries),
                    "duplicates": sum(1 for match in existing if match),
                    "source": source,
                    "recorded": False,
                }
            provenance = {
                "kind": "selected_reference_file",
                "path": args["path"],
                "sha256": digest,
            }
            locator = {**source, "kind": "bibliography"}
            results = refs.import_references(
                graph, entries, provenance=provenance, source_locator=locator)
            return {
                "references": [result["reference"] for result in results],
                "created": sum(1 for result in results if result["created"]),
                "updated": sum(1 for result in results if not result["created"]),
                "count": len(results),
                "source": source,
                "recorded": True,
            }
        if operation == "ref_record":
            from ozagia.research import references as refs
            reference = args.get("reference")
            if not isinstance(reference, dict):
                raise ResearchError("reference must be an object")
            result = refs.record_reference(
                graph,
                reference,
                provenance={"kind": "explicit_reference"},
                source_locator=args.get("source_locator"),
            )
            return {**result, "truth_assessed": False}
        if operation == "ref_export":
            from ozagia.research import references as refs
            return refs.export_references(state, args.get("format", "bibtex"))
        if operation == "ref_check":
            from ozagia.research import references as refs
            return {
                "references": refs.check_references(state),
                "graph": graph.check(),
                "truth_assessed": False,
            }
        if operation == "ref_link_citations":
            from ozagia.research import references as refs
            target = next((node for node in state["nodes"] if node["id"] == args["node_id"]), None)
            if target is None:
                raise ResearchError("unknown node id")
            keys = target.get("citations") or target.get("citation_keys") or []
            if not isinstance(keys, list):
                raise ResearchError("citation keys must be an array")
            key_map = {}
            for reference in state["nodes"]:
                if reference.get("type") != "Reference":
                    continue
                for key in [reference.get("citation_key"), *(reference.get("citation_keys") or [])]:
                    if key:
                        key_map[str(key).casefold()] = reference
            linked = []
            for key in keys:
                reference = key_map.get(str(key).casefold())
                if reference is None:
                    continue
                duplicate = any(
                    edge.get("type") == "cites" and edge.get("src") == target["id"]
                    and edge.get("tgt") == reference["id"]
                    for edge in state["edges"])
                if duplicate:
                    continue
                linked.append(graph.link(
                    target["id"], "cites", reference["id"],
                    provenance={"kind": "deferred_citation_link", "citation_key": key},
                    rationale="The record cites this key; the link does not establish support.",
                ))
            return {"node_id": target["id"], "linked": linked,
                    "unresolved": [key for key in keys
                                   if str(key).casefold() not in key_map],
                    "truth_assessed": False}
        if operation == "source_types":
            from ozagia.research import holdings
            return {"source_types": holdings.list_source_types(), "truth_assessed": False}
        if operation == "holding_record":
            from ozagia.research import holdings
            return holdings.record_holding(
                graph,
                source_id=args["source_id"],
                source_type=args["source_type"],
                epistemic_role=args.get("epistemic_role"),
                authority=args.get("authority"),
                review_status=args.get("review_status"),
                use_constraints=args.get("use_constraints"),
                scope=args.get("scope"),
                rationale=args.get("rationale"),
                reviewer=args.get("reviewer"),
                status=args.get("status", "proposed"),
                confidence=args.get("confidence"),
            )
        if operation == "holding_check":
            from ozagia.research import holdings
            return holdings.check_holding(state, args["holding_id"])
        if operation == "holding_report":
            from ozagia.research import holdings
            return holdings.holding_report(state, args.get("source_id"))
        if operation == "claim_holding":
            from ozagia.research import holdings
            return holdings.claim_holding(state, args["claim_id"])
        if operation == "source_register":
            from ozagia.research import source_formats
            return source_formats.register_source(
                graph,
                source_type=args["source_type"],
                metadata=args["metadata"],
                root_id=args.get("root_id"),
                path=args.get("path"),
                url=args.get("url"),
                sha256=args.get("sha256"),
                reference=args.get("reference"),
                holding=args.get("holding"),
                reviewer=args.get("reviewer"),
            )
        if operation == "document_claim_preview":
            from ozagia.research import document_ingest
            return document_ingest.preview_document_claims(
                graph,
                root_id=args["root_id"],
                path=args["path"],
                source_type=args.get("source_type"),
                title=args.get("title"),
                metadata=args.get("metadata"),
                max_claims=args.get("max_claims", 256),
            )
        if operation == "document_claim_ingest":
            from ozagia.research import document_ingest
            return document_ingest.ingest_document_claims(
                graph,
                root_id=args["root_id"],
                path=args["path"],
                source_type=args.get("source_type"),
                title=args.get("title"),
                metadata=args.get("metadata"),
                max_claims=args.get("max_claims", 256),
                reviewer=args.get("reviewer"),
                holding=args.get("holding"),
                resolve_citations=args.get("resolve_citations", True),
            )
        if operation == "ref_resolve":
            from ozagia.research import fetch
            from ozagia.research import references as refs
            reference = fetch.resolve_reference(
                args["identifier"],
                email=args.get("email"),
                fetcher=self._fetch(),
                provider=args.get("provider"),
            )
            recorded = None
            if args.get("record") is True:
                recorded = refs.record_reference(
                    graph, reference,
                    provenance={"kind": "resolved_metadata",
                                "identifier": args["identifier"],
                                "provider": args.get("provider") or "open_metadata"})
                reference = recorded["reference"]
            return {
                "reference": reference,
                "recorded": recorded,
                "truth_assessed": False,
            }
        if operation == "fetch_preview":
            from ozagia.research import fetch
            from ozagia.research import references as refs
            if args.get("reference_id"):
                reference = next(
                    (node for node in state["nodes"]
                     if node.get("id") == args["reference_id"]
                     and node.get("type") == "Reference"), None)
                if reference is None:
                    raise ResearchError("unknown Reference id")
            else:
                reference = refs.normalize_reference(args.get("reference") or {
                    "citation_key": args.get("identifier", ""),
                    "raw": args.get("identifier", ""),
                })
            return fetch.preview_fetch(
                reference,
                email=args.get("email"),
                fetcher=self._fetch(),
                provider=args.get("provider"),
            )
        if operation == "fetch_apply":
            from ozagia.research import fetch
            candidate = args.get("candidate")
            if not isinstance(candidate, dict):
                raise ResearchError("candidate must be an object")
            return fetch.apply_fetch(
                graph,
                reference_id=args["reference_id"],
                candidate=candidate,
                cache_dir=self._cache_dir(),
                fetcher=self._fetch(),
            )
        if operation == "ref_providers":
            from ozagia.research.providers import provider_status
            return provider_status()
        if operation == "external_search":
            from ozagia.research import fetch
            return fetch.search_external(
                args["query"], provider=args["provider"],
                limit=args.get("limit", 10))
        if operation == "zotero_import":
            from ozagia.research import references as refs
            from ozagia.research import zotero
            items = self._zotero()(
                base_url=args.get("base_url", zotero.DEFAULT_BASE_URL),
                collection=args.get("collection"),
                limit=args.get("limit", 100),
            )
            preview = [
                {**item, "duplicate_of": (
                    refs.find_reference(state, item) or {}).get("id")}
                for item in items
            ]
            if args.get("apply") is not True:
                return {"references": preview, "count": len(preview),
                        "recorded": False, "source": "zotero_local_api"}
            results = refs.import_references(
                graph, items,
                provenance={"kind": "zotero_local_api",
                            "base_url": args.get("base_url", zotero.DEFAULT_BASE_URL),
                            "collection": args.get("collection")},
            )
            return {
                "references": [result["reference"] for result in results],
                "created": sum(1 for result in results if result["created"]),
                "updated": sum(1 for result in results if not result["created"]),
                "count": len(results),
                "recorded": True,
                "source": "zotero_local_api",
            }
        if operation == "tei_preview":
            import hashlib
            from ozagia.research import references as refs
            from ozagia.research import tei
            from ozagia.research.paper import _root_file
            root, source_path = _root_file(graph, args["root_id"], args["path"])
            if source_path.suffix.lower() not in {".xml", ".tei"}:
                raise ResearchError("TEI source must be .xml or .tei")
            try:
                raw = source_path.read_bytes()
            except OSError as exc:
                raise ResearchError(f"cannot read selected TEI source: {exc}") from exc
            parsed = tei.parse_tei(raw)
            if args.get("record_references") is True:
                locator = {
                    "root_id": args["root_id"],
                    "repository": root.get("repository", args["root_id"]),
                    "path": args["path"],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "kind": "tei_bibliography",
                }
                results = refs.import_references(
                    graph, parsed["references"],
                    provenance={"kind": "tei_bibliography", "path": args["path"]},
                    source_locator=locator,
                )
                parsed["recorded_references"] = [result["reference"] for result in results]
            return parsed
        if operation == "pdf_preview":
            import hashlib
            from ozagia.research import references as refs
            from ozagia.research.paper import _root_file
            source_locator = None
            if args.get("source_id"):
                source = next(
                    (node for node in state["nodes"]
                     if node.get("id") == args["source_id"]
                     and node.get("type") == "Source"), None)
                if source is None or not isinstance(source.get("locator"), dict):
                    raise ResearchError("unknown Source id or Source has no locator")
                locator = source["locator"]
                if locator.get("kind") != "cached_fetch" or not locator.get("cache_relative"):
                    raise ResearchError("pdf_preview source must be a cached fetched artifact")
                path = (self._cache_dir() / str(locator["cache_relative"])).resolve()
                if not path.is_relative_to(self._cache_dir().resolve()) or not path.is_file():
                    raise ResearchError("cached PDF is missing or outside the cache")
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    raise ResearchError(f"cannot read cached PDF: {exc}") from exc
                display_path = locator.get("url") or locator.get("cache_relative")
                source_locator = locator
            else:
                root, path = _root_file(graph, args["root_id"], args["path"])
                if path.suffix.lower() != ".pdf":
                    raise ResearchError("pdf_preview source must be a .pdf file")
                try:
                    raw = path.read_bytes()
                except OSError as exc:
                    raise ResearchError(f"cannot read selected PDF: {exc}") from exc
                display_path = args["path"]
                source_locator = {
                    "root_id": args["root_id"],
                    "repository": root.get("repository", args["root_id"]),
                    "path": args["path"],
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "kind": "pdf",
                }
            parsed = self._pdf()(
                raw,
                backend=args.get("backend"),
                ocr=args.get("ocr", "auto"),
                ocr_lang=args.get("ocr_lang", "eng"),
                ocr_dpi=args.get("ocr_dpi", 300),
            )
            if args.get("extract_claims") is True:
                from ozagia.research import pdf_claims
                parsed["claims"] = pdf_claims.candidate_claims(parsed)["claims"]
            if args.get("record_references") is True:
                results = refs.import_references(
                    graph, parsed.get("references", []),
                    provenance={"kind": "pdf_bibliography", "path": str(display_path),
                                "sha256": parsed.get("sha256")},
                    source_locator=source_locator,
                )
                parsed["recorded_references"] = [result["reference"] for result in results]
            return parsed
        if operation == "pdf_claim_preview":
            from ozagia.research import pdf_claims
            return pdf_claims.preview_pdf_claims(
                graph,
                root_id=args["root_id"],
                path=args["path"],
                pdf_extractor=self._pdf_extractor,
                max_claims=args.get("max_claims", 256),
                backend=args.get("backend"),
            )
        if operation == "pdf_claim_ingest":
            from ozagia.research import pdf_claims
            return pdf_claims.ingest_pdf_claims(
                graph,
                root_id=args["root_id"],
                path=args["path"],
                pdf_extractor=self._pdf_extractor,
                max_claims=args.get("max_claims", 256),
                backend=args.get("backend"),
                reviewer=args.get("reviewer"),
                resolve_citations=args.get("resolve_citations", True),
            )
        if operation == "citation_resolve":
            from ozagia.research import citation_resolution
            return citation_resolution.resolve_claim_citations(
                graph,
                args["claim_id"],
                apply=args.get("apply", True),
                threshold=args.get("threshold", 0.6),
            )
        if operation == "citation_resolution_report":
            from ozagia.research import citation_resolution
            return citation_resolution.citation_resolution_report(
                state, args.get("claim_id"))
        if operation == "search":
            from ozagia.research import search
            return search.search(
                state, args["query"], limit=args.get("limit", 8),
                root_id=args.get("root_id"), path=args.get("path"),
                db_path=state_path(self.workspace, "research", "index.sqlite"),
            )
        if operation == "semantic_build":
            from ozagia.research import semantic
            return semantic.build_semantic_index(
                state,
                args.get("db_path") or state_path(
                    self.workspace, "research", "semantic.sqlite"),
                model=args.get("model", "hash-bow"),
                dim=args.get("dim", 512),
            )
        if operation == "semantic_search":
            from ozagia.research import semantic
            db_path = Path(args.get("db_path") or state_path(
                self.workspace, "research", "semantic.sqlite"))
            if not db_path.is_file():
                semantic.build_semantic_index(
                    state, db_path, model=args.get("model", "hash-bow"))
            if args.get("hybrid", True):
                return semantic.hybrid_search(
                    state, args["query"], limit=args.get("limit", 8),
                    db_path=db_path, model=args.get("model"),
                    alpha=args.get("alpha", 0.5))
            return semantic.semantic_search(
                state, args["query"], limit=args.get("limit", 8),
                path=args.get("path"), db_path=db_path,
                model=args.get("model"), alpha=args.get("alpha", 0.5))
        if operation == "cite_context":
            import hashlib
            from ozagia.research import references as refs
            from ozagia.research.paper import _root_file
            root, source_path = _root_file(graph, args["root_id"], args["path"])
            try:
                raw = source_path.read_bytes()
                text = raw.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise ResearchError(f"cannot read citation-context source: {exc}") from exc
            contexts = refs.citation_contexts(text)
            if args.get("record") is not True:
                return {"contexts": contexts, "count": len(contexts),
                        "recorded": [], "truth_assessed": False}
            key_map = {}
            for reference in state["nodes"]:
                if reference.get("type") != "Reference":
                    continue
                for key in [reference.get("citation_key"), *(reference.get("citation_keys") or [])]:
                    if key:
                        key_map[str(key).casefold()] = reference
            recorded = []
            for context in contexts:
                evidence = graph.record(
                    "Evidence",
                    evidence_type="citation_context_candidate",
                    wording=context["quote"],
                    citation_keys=context["citation_keys"],
                    relation_candidate=context["relation_candidate"],
                    source_locator={
                        "root_id": args["root_id"],
                        "repository": root.get("repository", args["root_id"]),
                        "path": args["path"],
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "start_line": context["start_line"],
                        "end_line": context["end_line"],
                        "kind": "citation_context",
                    },
                    truth_assessed=False,
                )
                for key in context["citation_keys"]:
                    reference = key_map.get(key.casefold())
                    if reference is not None:
                        graph.link(
                            evidence["id"], "cites", reference["id"],
                            provenance={"kind": "citation_context_key",
                                        "citation_key": key},
                            rationale="The sentence cites this key; the context is unclassified and does not establish support.",
                        )
                recorded.append(evidence)
            return {"contexts": contexts, "count": len(contexts),
                    "recorded": recorded, "truth_assessed": False}
        if operation == "evidence_table":
            from ozagia.research import review
            return review.evidence_table(state, format=args.get("format", "markdown"))
        if operation == "screen":
            from ozagia.research import review
            return review.screen_reference(
                graph, args["reference_id"], stage=args["stage"],
                decision=args["decision"], reason=args["reason"],
                reviewer=args["reviewer"], criteria=args.get("criteria"),
            )
        if operation == "screening_report":
            from ozagia.research import review
            return review.screening_report(state)
        if operation == "ref_citation_fetch":
            from ozagia.research import citation_graph
            return citation_graph.fetch_related(
                graph,
                args["reference_id"],
                direction=args.get("direction", "references"),
                limit=args.get("limit", 25),
                fetcher=self._fetch(),
                apply=args.get("apply", True),
            )
        if operation == "ref_citation_neighborhood":
            from ozagia.research import citation_graph
            return citation_graph.neighborhood(
                state,
                args["reference_id"],
                depth=args.get("depth", 1),
                direction=args.get("direction", "both"),
            )
        if operation == "ref_citation_path":
            from ozagia.research import citation_graph
            return citation_graph.citation_path(
                state, args["src"], args["tgt"],
                max_depth=args.get("max_depth", 4))
        if operation == "manuscript_check":
            from ozagia.research import manuscript
            return manuscript.check_manuscript(
                state, args["body"],
                require_claim_sources=args.get("require_claim_sources", True))
        if operation == "manuscript_bundle":
            from ozagia.research import manuscript
            return manuscript.build_manuscript(
                graph,
                title=args["title"],
                body=args["body"],
                output_dir=args.get("output_dir"),
                run_pandoc=args.get("run_pandoc", False),
                pandoc_format=args.get("pandoc_format", "docx"),
                runner=self._pandoc_runner,
            )
        if operation == "review_plan_create":
            from ozagia.research import structured_review
            return structured_review.create_review_plan(
                graph,
                question=args["question"],
                kind=args.get("kind", "systematic"),
                roles=args["roles"],
                models=args["models"],
                criteria=args.get("criteria"),
                exclusion_criteria=args.get("exclusion_criteria"),
                sources=args.get("sources"),
                reviewers=args.get("reviewers"),
                rounds=args.get("rounds", ("independent",)),
                blinding=args.get("blinding", True),
                min_reviewers=args.get("min_reviewers", 2),
                adjudication=args.get("adjudication", "human"),
                target_id=args.get("target_id"),
            )
        if operation == "review_search_log":
            from ozagia.research import structured_review
            return structured_review.record_search_run(
                graph, args["plan_id"],
                query=args["query"], provider=args["provider"],
                executed_at=args.get("executed_at"),
                filters=args.get("filters"),
                result_count=args.get("result_count"),
                deduped_count=args.get("deduped_count"),
                export_sha256=args.get("export_sha256"),
                notes=args.get("notes"),
            )
        if operation == "review_packet":
            from ozagia.research import structured_review
            return structured_review.build_review_packet(
                state,
                plan_id=args.get("plan_id"),
                claim_id=args.get("claim_id"),
                reference_id=args.get("reference_id"),
                manuscript_body=args.get("manuscript_body"),
            )
        if operation == "review_validate":
            from ozagia.research import structured_review
            packet = args.get("packet")
            if packet is not None and not isinstance(packet, dict):
                raise ResearchError("packet must be an object")
            return structured_review.validate_review_output(
                args["output"], state=state, packet=packet)
        if operation == "review_run_record":
            from ozagia.research import structured_review
            return structured_review.record_review_run(
                graph,
                plan_id=args["plan_id"],
                reviewer_id=args["reviewer_id"],
                role=args["role"],
                model=args["model"],
                packet_sha256=args["packet_sha256"],
                output=args["output"],
                provider=args.get("provider"),
                round=args.get("round", "independent"),
                prompt_sha256=args.get("prompt_sha256"),
                context_sha256=args.get("context_sha256"),
                params=args.get("params"),
                tokens=args.get("tokens"),
                cost=args.get("cost"),
                status=args.get("status", "recorded"),
                target_id=args.get("target_id"),
            )
        if operation == "review_agreement":
            from ozagia.research import structured_review
            return structured_review.review_agreement(state, args["plan_id"])
        if operation == "review_reconcile":
            from ozagia.research import structured_review
            return structured_review.record_reconciliation(
                graph,
                plan_id=args["plan_id"],
                method=args.get("method", "consensus"),
                min_reviewers=args.get("min_reviewers"),
                adjudicator=args.get("adjudicator"),
                rationale=args.get("rationale"),
                run_ids=args.get("run_ids"),
            )
        if operation == "review_report":
            from ozagia.research import structured_review
            return structured_review.review_report(state, args["plan_id"])
        if operation == "argument_brief":
            from ozagia.research import argument
            return argument.build_argument_brief(
                state, claim_id=args["claim_id"], mode=args.get("mode", "steelman"))
        if operation == "argument_record":
            from ozagia.research import argument
            return argument.record_argument(
                graph,
                claim_id=args["claim_id"],
                mode=args["mode"],
                thesis=args["thesis"],
                premises=args["premises"],
                assumptions=args.get("assumptions", []),
                objections=args.get("objections", []),
                falsifiers=args.get("falsifiers", []),
                confidence=args["confidence"],
                reviewer=args["reviewer"],
                model_runs=args.get("model_runs"),
                notes=args.get("notes"),
            )
        if operation == "argument_check":
            from ozagia.research import argument
            return argument.check_argument(state, args["argument_id"])
        if operation == "argument_report":
            from ozagia.research import argument
            return argument.argument_report(state, args["argument_id"])
        if operation == "risk_of_bias_record":
            from ozagia.research import appraisal
            return appraisal.record_risk_of_bias(
                graph,
                target_id=args["target_id"],
                tool=args["tool"],
                domains=args["domains"],
                overall=args["overall"],
                reviewer=args["reviewer"],
                notes=args.get("notes"),
            )
        if operation == "risk_of_bias_check":
            from ozagia.research import appraisal
            return appraisal.check_risk_of_bias(state, args["assessment_id"])
        if operation == "certainty_record":
            from ozagia.research import appraisal
            return appraisal.record_certainty(
                graph,
                target_id=args["target_id"],
                rating=args["rating"],
                reasons=args["reasons"],
                reviewer=args["reviewer"],
                framework=args.get("framework", "GRADE"),
                downgrades=args.get("downgrades"),
                upgrades=args.get("upgrades"),
                notes=args.get("notes"),
            )
        if operation == "certainty_report":
            from ozagia.research import appraisal
            return appraisal.certainty_report(state, args.get("target_id"))
        if operation == "appraisal_table":
            from ozagia.research import appraisal
            return appraisal.appraisal_table(state, format=args.get("format", "markdown"))
        if operation == "extraction_record":
            from ozagia.research import extraction
            return extraction.record_extraction(
                graph,
                reference_id=args.get("reference_id"),
                claim_id=args.get("claim_id"),
                fields=args["fields"],
                source_locator=args.get("source_locator"),
                extractor=args["extractor"],
                reviewer=args.get("reviewer"),
                status=args.get("status", "candidate"),
            )
        if operation == "extraction_check":
            from ozagia.research import extraction
            return extraction.check_extraction(state, args["extraction_id"])
        if operation == "extraction_table":
            from ozagia.research import extraction
            return extraction.extraction_table(state, format=args.get("format", "markdown"))
        if operation == "extraction_report":
            from ozagia.research import extraction
            return extraction.extraction_report(state, args["extraction_id"])
        if operation == "contradiction_scan":
            from ozagia.research import contradiction
            return contradiction.find_contradictions(
                state, max_pairs=args.get("max_pairs", 100))
        if operation == "contradiction_record":
            from ozagia.research import contradiction
            return contradiction.record_contradiction(
                graph,
                claim_a_id=args["claim_a_id"],
                claim_b_id=args["claim_b_id"],
                kind=args["kind"],
                rationale=args["rationale"],
                evidence_ids=args.get("evidence_ids"),
                reviewer=args["reviewer"],
                status=args.get("status", "candidate"),
            )
        if operation == "contradiction_check":
            from ozagia.research import contradiction
            return contradiction.check_contradiction(state, args["contradiction_id"])
        if operation == "contradiction_report":
            from ozagia.research import contradiction
            return contradiction.contradiction_report(state)
        if operation == "alert_scan":
            from ozagia.research import living_review
            return living_review.scan_alerts(
                state,
                review_plan_id=args.get("review_plan_id"),
                since=args.get("since"),
                reference_ids=args.get("reference_ids"),
            )
        if operation == "alert_record":
            from ozagia.research import living_review
            return living_review.record_alert(
                graph,
                kind=args["kind"],
                target_id=args["target_id"],
                payload=args.get("payload"),
                reviewer=args.get("reviewer"),
                status=args.get("status", "open"),
            )
        if operation == "alert_ack":
            from ozagia.research import living_review
            return living_review.acknowledge_alert(
                graph, args["alert_id"], reviewer=args["reviewer"],
                note=args.get("note"))
        if operation == "alert_report":
            from ozagia.research import living_review
            return living_review.alert_report(state, status=args.get("status"))
        if operation == "theory_record":
            from ozagia.research import theory
            return theory.record_theory(
                graph,
                name=args["name"],
                statement=args["statement"],
                scope=args["scope"],
                mechanism=args.get("mechanism"),
                assumptions=args.get("assumptions"),
                predictions=args.get("predictions"),
                assumption_ids=args.get("assumption_ids"),
                prediction_ids=args.get("prediction_ids"),
                reviewer=args.get("reviewer"),
                status=args.get("status", "proposed"),
                notes=args.get("notes"),
            )
        if operation == "theory_rivalry":
            from ozagia.research import theory
            return theory.record_rivalry(
                graph,
                args["theory_a"],
                args["theory_b"],
                scope=args.get("scope"),
                rationale=args.get("rationale"),
                reviewer=args.get("reviewer"),
            )
        if operation == "theory_compatibility":
            from ozagia.research import theory
            return theory.record_compatibility(
                graph,
                args["theory_a"],
                args["theory_b"],
                degree=args["degree"],
                scope=args.get("scope"),
                rationale=args.get("rationale"),
                reviewer=args.get("reviewer"),
                evidence_ids=args.get("evidence_ids"),
            )
        if operation == "theory_link_evidence":
            from ozagia.research import theory
            return theory.link_theory_evidence(
                graph,
                args["theory_id"],
                args["target_id"],
                relation=args.get("relation", "supported_by"),
                rationale=args.get("rationale"),
                reviewer=args.get("reviewer"),
            )
        if operation == "theory_compare":
            from ozagia.research import theory
            return theory.theory_compare(state, args["theory_a"], args["theory_b"])
        if operation == "theory_scan":
            from ozagia.research import theory
            return theory.theory_scan(state)
        if operation == "theory_report":
            from ozagia.research import theory
            return theory.theory_report(state, args.get("theory_id"))
        if operation == "theory_compatibility_matrix":
            from ozagia.research import theory
            return theory.compatibility_matrix(state)
        if operation == "paper_review_start":
            from ozagia.research import paper_review
            return paper_review.start_paper_review(
                graph,
                root_id=args["root_id"],
                path=args["path"],
                question=args["question"],
                roles=args["roles"],
                models=args["models"],
                kind=args.get("kind", "peer_review"),
                bibliography_path=args.get("bibliography_path"),
                criteria=args.get("criteria"),
                min_reviewers=args.get("min_reviewers", 2),
                reviewer=args.get("reviewer"),
                pdf_extractor=self._pdf_extractor,
            )
        if operation == "paper_review_packet":
            from ozagia.research import paper_review
            return paper_review.build_paper_review_packet(
                state,
                source_id=args["source_id"],
                plan_id=args.get("plan_id"),
                manuscript_body=args.get("manuscript_body"),
                max_claims=args.get("max_claims", 50),
            )
        if operation == "paper_review_report":
            from ozagia.research import paper_review
            return paper_review.paper_review_report(state, plan_id=args["plan_id"])
        if operation == "paper_review_bundle":
            from ozagia.research import paper_review
            return paper_review.write_paper_review_bundle(
                graph, plan_id=args["plan_id"], output_dir=args.get("output_dir"))
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
                        open_locator(index, locator,
                                     cache_root=state_path(self.workspace, "research", "cache"))
                        if isinstance(locator, dict)
                        else {"status": "unavailable", "reason": "source locator missing"})})
            from ozagia.research import references as refs
            checks = {"graph": graph.check(), "workflow": workflow,
                      "run_workflows": run_workflows, "finite_checks": finite,
                      "citations": citations, "provenance": provenance,
                      "references": refs.check_references(state),
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
    parser.add_argument("operation", choices=["init", "context", "native_graph", "paper_preview", "paper_ingest", "ref_preview", "ref_import", "ref_record", "ref_export", "ref_check", "ref_link_citations", "source_types", "holding_record", "holding_check", "holding_report", "claim_holding", "source_register", "document_claim_preview", "document_claim_ingest", "ref_resolve", "ref_providers", "external_search", "fetch_preview", "fetch_apply", "zotero_import", "tei_preview", "pdf_preview", "pdf_claim_preview", "pdf_claim_ingest", "citation_resolve", "citation_resolution_report", "search", "semantic_build", "semantic_search", "cite_context", "evidence_table", "screen", "screening_report", "ref_citation_fetch", "ref_citation_neighborhood", "ref_citation_path", "manuscript_check", "manuscript_bundle", "review_plan_create", "review_search_log", "review_packet", "review_validate", "review_run_record", "review_agreement", "review_reconcile", "review_report", "argument_brief", "argument_record", "argument_check", "argument_report", "risk_of_bias_record", "risk_of_bias_check", "certainty_record", "certainty_report", "appraisal_table", "extraction_record", "extraction_check", "extraction_table", "extraction_report", "contradiction_scan", "contradiction_record", "contradiction_check", "contradiction_report", "alert_scan", "alert_record", "alert_ack", "alert_report", "theory_record", "theory_rivalry", "theory_compatibility", "theory_link_evidence", "theory_compare", "theory_scan", "theory_report", "theory_compatibility_matrix", "paper_review_start", "paper_review_packet", "paper_review_report", "paper_review_bundle", "formula_parse_candidates", "formula_resolve", "formula_translate", "formula_model", "model_candidate", "record", "link", "inspect", "trace", "execute", "export_lean", "check_lean", "check", "export"])
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
