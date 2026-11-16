#!/usr/bin/env python3
"""Run a source-backed, multi-model review of a public-domain popular-science book.

The default source is a small excerpt from Darwin's *On the Origin of Species*
(1859, public domain).  The demo:

1. records the book Source, Reference, Holding and claim candidates;
2. creates a review plan and packet;
3. runs three real models through the OpenCode CLI, independently;
4. records their findings and steelman/red-team arguments;
5. reconciles the findings and writes the structured review bundle.

Live model calls are explicit: pass ``--live`` and set ``OZAGIA_EVAL_LIVE=1``.
Without those, use the integration test in
``tests/test_popular_science_demo.py`` with a fake runner.

Usage:

    OZAGIA_EVAL_LIVE=1 python examples/popular_science_review.py --live

Models can be overridden with repeated ``--model`` flags.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from ozagia.harness.paper_review import run_paper_review
from ozagia.research.graph import ResearchGraph
from ozagia.research.service import ResearchService

REPO = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO / "tests/fixtures/research/origin_excerpt.md"
DEFAULT_OUTPUT = REPO / "docs/results/popular_science_origin_2026-11-16"
DEFAULT_MODELS = [
    "opencode-go/deepseek-v4.1-flash",
    "opencode-go/gpt-6-luna",
    "vllm/qwen3.8-27b",
]
DEFAULT_OCELOT_ROOT = REPO.parent / "ocelot"
DEFAULT_ROLES = ["domain"]
DEFAULT_ARGUMENT_ROLES = ["advocate", "critic"]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    text = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_json_object(text: str) -> dict[str, Any]:
    body = text.strip()
    if body.startswith("```"):
        first = body.find("\n")
        body = body[first + 1:] if first >= 0 else body[3:]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    try:
        value = json.loads(body)
    except ValueError:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("model reply was not a JSON object")
        value = json.loads(body[start:end + 1])
    if not isinstance(value, Mapping):
        raise RuntimeError("model reply was not a JSON object")
    return dict(value)


def _run_opencode(model: str, prompt: str, timeout: int) -> tuple[str, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="ozagia-opencode-") as directory:
        argv = [
            "opencode", "run", "--standalone", "--format", "json", "--auto",
            "--archived", "-m", model, "--title", f"ozagia demo {model}", prompt,
        ]
        completed = subprocess.run(
            argv, cwd=directory, text=True, capture_output=True,
            timeout=timeout, check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"opencode run failed for {model} ({completed.returncode}): "
            f"{(completed.stderr or completed.stdout)[:500]}"
        )
    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    cost = 0.0
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "text":
            part = event.get("part") or {}
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
        elif event.get("type") == "step_finish":
            part = event.get("part") or {}
            tokens = part.get("tokens") or {}
            if isinstance(tokens, Mapping):
                usage = {
                    "input": int(tokens.get("input") or 0),
                    "output": int(tokens.get("output") or 0),
                    "reasoning": int(tokens.get("reasoning") or 0),
                    "total": int(tokens.get("input") or 0) + int(tokens.get("output") or 0),
                }
            try:
                cost += float(part.get("cost") or 0.0)
            except (TypeError, ValueError):
                pass
    usage["cost"] = round(cost, 8)
    return "".join(text_parts), usage


def opencode_runner(model: str, messages: list[Mapping[str, Any]], *,
                    response_format: Any = None, timeout: int = 300) -> dict[str, Any]:
    """Call one model through `opencode run` and return parsed JSON + usage."""
    prompt = "\n\n".join(
        f"[{message.get('role', 'user')}]\n{message.get('content', '')}"
        for message in messages
    )
    if response_format is not None:
        prompt += "\n\nReturn only one JSON object, with no prose or code fence."
    total_usage: dict[str, Any] = {"input": 0, "output": 0, "reasoning": 0,
                                   "total": 0, "cost": 0.0}
    last_error: Exception | None = None
    started = time.perf_counter()
    text = ""
    for attempt in range(2):
        text, usage = _run_opencode(model, prompt, timeout)
        for key in ("input", "output", "reasoning", "total"):
            total_usage[key] += int(usage.get(key) or 0)
        total_usage["cost"] += float(usage.get("cost") or 0.0)
        try:
            output = _parse_json_object(text)
            break
        except Exception as exc:  # noqa: BLE001 - retry with a repair instruction
            last_error = exc
            if attempt == 0:
                prompt += (
                    "\n\nThe previous reply was not valid JSON ("
                    f"{exc}). Return only one corrected JSON object, no prose "
                    "and no code fence."
                )
    else:
        raise RuntimeError(f"model {model} did not return valid JSON: {last_error}")
    return {
        "output": output,
        "usage": {**total_usage, "cost": round(total_usage["cost"], 8)},
        "provider": "opencode-cli",
        "prompt_sha256": _sha256(messages),
        "context_sha256": _sha256(text),
        "wall_s": round(time.perf_counter() - started, 2),
    }


def build_demo(workspace: Path, source: Path, models: list[str],
               max_workers: int = 1,
               ocelot_root: Path | None = None,
               ocelot_python: Path | None = None,
               ocelot_mode: str = "auto") -> dict[str, Any]:
    svc = ResearchService(workspace)
    sid = svc.call("init", {
        "question": "Which claims does this popular-science book advance, and how strong is the evidence for them?",
        "roots": [{
            "root_id": "sources",
            "path": str(source.parent),
            "files": {source.name: "selected"},
        }],
    })["investigation_id"]

    ingested = svc.call("document_claim_ingest", {
        "session_id": sid,
        "root_id": "sources",
        "path": source.name,
        "source_type": "book",
        "metadata": {
            "title": "On the Origin of Species (excerpt)",
            "authors": ["Charles Darwin"],
            "year": "1859",
            "publisher": "John Murray",
            "license": "Public domain",
            "citation_key": "darwin-1859-origin",
            "scope": "excerpt from the 1859 first edition",
        },
        "reviewer": "demo",
    })
    if not ingested["claims"]:
        raise RuntimeError("the demo source produced no claim candidates")

    source_node = ingested["source"]
    plan_result = svc.call("review_plan_create", {
        "session_id": sid,
        "question": "Is the book's central claim supported, and what remains interpretation?",
        "kind": "peer_review",
        "roles": DEFAULT_ROLES,
        "models": models,
        "min_reviewers": 2,
        "target_id": source_node["id"],
        "reviewers": ["demo"],
        "criteria": [
            "Identify the book's substantive claims and their evidence",
            "Distinguish authorial interpretation from empirical support",
            "Give falsifiers and unresolved questions",
        ],
    })
    plan = plan_result["plan"]
    packet = svc.call("paper_review_packet", {
        "session_id": sid,
        "source_id": source_node["id"],
        "plan_id": plan["id"],
    })
    graph = ResearchGraph.load(sid, root=workspace)
    result = run_paper_review(
        plan=plan,
        packet=packet,
        model_runner=opencode_runner,
        review_roles=DEFAULT_ROLES,
        argument_roles=DEFAULT_ARGUMENT_ROLES,
        models=models,
        claim_ids=[packet["claim_ids"][0]],
        graph=graph,
        record=True,
        max_workers=max_workers,
        bundle_dir=None,
    )
    ocelot = None
    ocelot_notes: list[str] = []
    if ocelot_root is not None:
        executable = ocelot_python or (ocelot_root / ".venv/bin/python")
        if ocelot_root.is_dir() and executable.is_file():
            try:
                ocelot = svc.call("review_reconcile", {
                    "session_id": sid,
                    "plan_id": plan["id"],
                    "method": "ocelot_analogy",
                    "ocelot_root": str(ocelot_root),
                    "ocelot_python": str(executable),
                    "ocelot_mode": ocelot_mode,
                })
                ocelot_notes.append(
                    "Ocelot analogy search recorded as the latest reconciliation")
            except Exception as exc:  # noqa: BLE001 - demo falls back to exact
                ocelot_notes.append(
                    f"Ocelot reconciliation unavailable ({type(exc).__name__}: {exc}); "
                    "used exact reconciliation")
        else:
            ocelot_notes.append("Ocelot checkout or venv unavailable; used exact reconciliation")
    report = svc.call("paper_review_report", {"session_id": sid, "plan_id": plan["id"]})
    bundle = svc.call("paper_review_bundle", {
        "session_id": sid, "plan_id": plan["id"],
        "output_dir": str(workspace.parent / "bundle"),
    })
    return {
        "investigation_id": sid,
        "source": {
            "id": source_node["id"],
            "type": source_node.get("source_type"),
            "path": source.name,
            "sha256": source_node.get("locator", {}).get("sha256"),
        },
        "holding": ingested.get("holding"),
        "claims": [
            {"id": claim["id"], "wording": claim["wording"],
             "method": claim.get("extraction_method"),
             "confidence": claim.get("confidence")}
            for claim in ingested["claims"]
        ],
        "plan": plan,
        "packet_sha256": packet["packet_sha256"],
        "result": result,
        "reconciliation": ocelot or result.get("reconciliation"),
        "ocelot": (ocelot or {}).get("ocelot"),
        "ocelot_notes": ocelot_notes,
        "report": report,
        "bundle": bundle,
    }


def _summary(demo: Mapping[str, Any], models: list[str]) -> dict[str, Any]:
    result = demo["result"]
    review = result.get("review") or {}
    reconciliation = demo.get("reconciliation") or result.get("reconciliation") or {}
    runs = review.get("runs") or []
    usage = {"input": 0, "output": 0, "total": 0, "cost": 0.0}
    for run in runs:
        run_usage = run.get("usage") or {}
        for key in ("input", "output", "total"):
            usage[key] += int(run_usage.get(key) or 0)
        usage["cost"] += float(run_usage.get("cost") or 0.0)
    return {
        "work": "On the Origin of Species (excerpt, 1859, public domain)",
        "models": models,
        "roles": DEFAULT_ROLES,
        "argument_roles": DEFAULT_ARGUMENT_ROLES,
        "investigation_id": demo["investigation_id"],
        "source": demo["source"],
        "holding": demo["holding"],
        "claims": demo["claims"],
        "plan_id": demo["plan"]["id"],
        "packet_sha256": demo["packet_sha256"],
        "review_runs": len(runs),
        "review_errors": review.get("errors") or [],
        "argument_count": len(result.get("arguments") or []),
        "reconciliation": {
            "id": (reconciliation.get("reconciliation") or {}).get("id"),
            "method": (reconciliation.get("reconciliation") or {}).get("method"),
            "clustering": (reconciliation.get("agreement") or {}).get("clustering"),
            "counts": (reconciliation.get("agreement") or {}).get("counts"),
            "fleiss_kappa": (reconciliation.get("agreement") or {}).get("fleiss_kappa"),
            "mean_pairwise_jaccard": (reconciliation.get("agreement") or {}).get("mean_pairwise_jaccard"),
            "consensus": len(reconciliation.get("consensus") or []),
            "dissent": len(reconciliation.get("dissent") or []),
        },
        "ocelot": {
            "used": demo.get("ocelot") is not None,
            "analogies": len((demo.get("ocelot") or {}).get("analogies") or []),
            "clusters": len((demo.get("ocelot") or {}).get("clusters") or []),
            "notes": demo.get("ocelot_notes") or [],
        },
        "usage": {**usage, "cost": round(usage["cost"], 8)},
        "bundle": demo["bundle"].get("paths"),
        "truth_assessed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="allow real model calls")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", action="append", dest="models",
                        help="model id; may be repeated")
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument("--ocelot-root", type=Path, default=DEFAULT_OCELOT_ROOT,
                        help="optional Ocelot checkout for structural reconciliation")
    parser.add_argument("--ocelot-python", type=Path, default=None,
                        help="Python with substrate_rs and Ocelot dependencies")
    parser.add_argument("--ocelot-mode", choices=("auto", "inprocess", "subprocess"),
                        default="auto",
                        help="run Ocelot in-process when possible, or in its venv")
    parser.add_argument("--no-ocelot", action="store_true",
                        help="skip the optional Ocelot structural reconciliation")
    args = parser.parse_args(argv)
    if not args.live or os.environ.get("OZAGIA_EVAL_LIVE") != "1":
        parser.error("live runs need --live and OZAGIA_EVAL_LIVE=1")
    models = args.models or DEFAULT_MODELS
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    workspace = output / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    source = args.source.resolve()
    if not source.is_file():
        parser.error(f"source not found: {source}")
    ocelot_root = None if args.no_ocelot else args.ocelot_root
    demo = build_demo(
        workspace, source, models, max_workers=args.max_workers,
        ocelot_root=ocelot_root, ocelot_python=args.ocelot_python,
        ocelot_mode=args.ocelot_mode,
    )
    summary = _summary(demo, models)
    (output / "demo.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "runs.json").write_text(
        json.dumps(demo["result"], ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
