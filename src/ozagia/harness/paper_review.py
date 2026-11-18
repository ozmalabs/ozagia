"""ozagia.harness.paper_review — drive a complete paper review.

This is the Stage B driver. It runs the analysis review matrix through
:func:`ozagia.harness.review.run_review_matrix` and, independently, steelman /
red-team / synthesis argument builders over selected claims. Argument outputs
are validated and recorded with :func:`ozagia.research.argument.record_argument`
when a graph is supplied and ``record`` is true.

Nothing here calls a model by itself: callers inject a ``model_runner``. The
research modules (arguments, reconciliation, report and bundle) are imported
lazily at call time, so this module is importable while those modules or their
optional dependencies are unavailable. Report and bundle builders are also
injectable for tests.

Python API::

    result = run_paper_review(
        plan=plan, packet=packet, model_runner=runner, graph=graph,
        record=True, bundle_dir=".ozagia/research/bundle",
    )

The result carries model candidates and reviewer judgements only:
``truth_assessed`` is always false.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

DEFAULT_REVIEW_ROLES = [
    "methods",
    "statistics",
    "domain",
    "reproducibility",
    "ethics",
    "synthesizer",
]
DEFAULT_ARGUMENT_ROLES = ["advocate", "critic", "synthesizer"]
ARGUMENT_ROLE_MODES: dict[str, str] = {
    "advocate": "steelman",
    "critic": "red_team",
    "synthesizer": "synthesis",
}
MAX_CLAIMS = 20
DEFAULT_MAX_WORKERS = 4
JSON_RESPONSE_FORMAT = {"type": "json_object"}

# Mirrors the record_argument contract. A model that follows this schema can be
# validated and recorded unchanged; record_argument remains the authority.
ARGUMENT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "thesis",
        "premises",
        "assumptions",
        "objections",
        "falsifiers",
        "confidence",
    ],
    "properties": {
        "thesis": {"type": "string"},
        "premises": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["wording", "inference"],
                "properties": {
                    "wording": {"type": "string"},
                    "inference": {
                        "type": "string",
                        "enum": [
                            "deductive",
                            "inductive",
                            "abductive",
                            "analogy",
                            "assumption",
                        ],
                    },
                    "claim_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "citation_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "strength": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["wording"],
                "properties": {
                    "wording": {"type": "string"},
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "citation_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "response": {"type": "string"},
                    "status": {"type": "string"},
                },
            },
        },
        "falsifiers": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "notes": {"type": "string"},
    },
}


class PaperReviewDriverError(RuntimeError):
    """Invalid paper review driver configuration."""


# --------------------------------------------------------------------------- #
# deterministic input and hashing helpers
# --------------------------------------------------------------------------- #
def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _messages_sha256(messages: Iterable[Mapping[str, Any]]) -> str:
    return _sha256_json(list(messages))


def _context_sha256(messages: Iterable[Mapping[str, Any]]) -> str:
    parts = [
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, Mapping)
        and message.get("role") in ("system", "user")
    ]
    return _sha256_text("".join(parts))


def _parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating a fenced or prefixed model reply."""
    from ozagia.harness.structured_output import loads_model_json

    try:
        value = loads_model_json(text)
    except ValueError:
        body = text.strip()
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise PaperReviewDriverError(
                "model output is not a JSON object") from None
        try:
            value = loads_model_json(body[start:end + 1])
        except ValueError:
            raise PaperReviewDriverError(
                "model output is not a JSON object") from None
    if not isinstance(value, Mapping):
        raise PaperReviewDriverError("model output is not a JSON object")
    return dict(value)


def _output_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        return _parse_json_object(value)
    raise PaperReviewDriverError("model output is not a JSON object")


def _names(
    value: Any,
    label: str,
    *,
    default: Iterable[str] | None = None,
    allow_empty: bool = False,
) -> list[str]:
    """A de-duplicated, ordered list of names from a list or CSV string."""
    if value is None:
        if default is None:
            raise PaperReviewDriverError(f"no {label} given")
        value = default
    if isinstance(value, str):
        pieces: Iterable[Any] = value.split(",")
    elif isinstance(value, Mapping):
        raise PaperReviewDriverError(
            f"{label} must be a list or comma-separated string")
    elif isinstance(value, (set, frozenset)):
        pieces = sorted(value, key=str)
    else:
        try:
            pieces = list(value)
        except TypeError as exc:
            raise PaperReviewDriverError(
                f"{label} must be a list or comma-separated string") from exc
    names: list[str] = []
    for piece in pieces:
        name = str(piece).strip()
        if name and name not in names:
            names.append(name)
    if not names and not allow_empty:
        raise PaperReviewDriverError(f"{label} is empty")
    return names


def _plan_id(plan: Mapping[str, Any]) -> str | None:
    value = plan.get("id") or plan.get("plan_id")
    return str(value) if value else None


def _claim_ids(packet: Mapping[str, Any], value: Any) -> list[str]:
    """Selected claim ids in caller order, bound to ``MAX_CLAIMS``."""
    if value is None:
        value = packet.get("claim_ids")
        if not value:
            value = packet.get("claims")
    if isinstance(value, str):
        pieces: Iterable[Any] = value.split(",")
    elif isinstance(value, Mapping):
        pieces = [value]
    elif isinstance(value, (set, frozenset)):
        pieces = sorted(value, key=str)
    elif value is None:
        pieces = []
    else:
        try:
            pieces = list(value)
        except TypeError:
            pieces = [value]
    ids: list[str] = []
    for piece in pieces:
        if isinstance(piece, Mapping):
            identifier = (
                piece.get("id") or piece.get("claim_id")
                or piece.get("node_id")
            )
        else:
            identifier = piece
        claim_id = str(identifier).strip() if identifier is not None else ""
        if claim_id and claim_id not in ids:
            ids.append(claim_id)
        if len(ids) >= MAX_CLAIMS:
            break
    return ids


def _accepts_kwarg(function: Callable[..., Any], name: str) -> bool:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return True
    parameters = signature.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD
           for p in parameters.values()):
        return True
    return name in parameters


def _call_model_runner(
    model_runner: Callable[..., Any],
    model: str,
    messages: list[dict[str, str]],
) -> Any:
    if _accepts_kwarg(model_runner, "response_format"):
        return model_runner(
            model, messages, response_format=dict(JSON_RESPONSE_FORMAT))
    return model_runner(model, messages)


def _normalize_runner_result(raw: Any) -> dict[str, Any]:
    """Accept either the parsed output or the metadata wrapper."""
    if not isinstance(raw, Mapping):
        raise PaperReviewDriverError(
            "model runner did not return a mapping")
    if "output" not in raw:
        return {
            "output": _output_mapping(raw),
            "usage": {},
            "provider": None,
            "prompt_sha256": None,
            "context_sha256": None,
        }
    usage = raw.get("usage")
    provider = raw.get("provider")
    prompt_hash = raw.get("prompt_sha256")
    context_hash = raw.get("context_sha256")
    return {
        "output": _output_mapping(raw.get("output")),
        "usage": dict(usage) if isinstance(usage, Mapping) else {},
        "provider": str(provider) if provider is not None else None,
        "prompt_sha256": prompt_hash if isinstance(prompt_hash, str) else None,
        "context_sha256": (
            context_hash if isinstance(context_hash, str) else None),
    }


def _error(stage: str, exc: BaseException, **fields: Any) -> dict[str, Any]:
    return {
        "stage": stage,
        **fields,
        "error": f"{type(exc).__name__}: {exc}",
        "error_type": type(exc).__name__,
    }


# --------------------------------------------------------------------------- #
# argument prompts and recording
# --------------------------------------------------------------------------- #
def _argument_messages(
    brief: Mapping[str, Any],
    *,
    role: str,
    mode: str,
) -> list[dict[str, str]]:
    """Two messages for one independent claim x role x model argument run."""
    system = (
        f"{brief.get('prompt') or ''}\n\n"
        f"You are the {role!r} argument builder in the {mode!r} mode. You are "
        "working independently: you have not seen, and must not assume, any "
        "other argument or review run's output. No model output is truth. "
        "Ground every premise in the brief and name what would falsify the "
        "case. Return one JSON object only, matching the schema in the user "
        "message."
    )
    user = (
        f"Argument role: {role}\n"
        f"Mode: {mode}\n"
        f"Claim id: {brief.get('claim', {}).get('id', '')}\n\n"
        "Argument brief (deterministic JSON):\n"
        f"{_canonical_json(brief)}\n\n"
        "Return exactly one JSON object and nothing else. It must match this "
        "strict schema:\n"
        f"{json.dumps(ARGUMENT_OUTPUT_SCHEMA, ensure_ascii=False, sort_keys=True, indent=2)}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _argument_record_fields(
    output: Mapping[str, Any],
    *,
    claim_id: str,
    mode: str,
    role: str,
    model: str,
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    model_runs = [{
        "model": model,
        "provider": meta.get("provider"),
        "prompt_sha256": meta.get("prompt_sha256"),
        "context_sha256": meta.get("context_sha256"),
        "output_sha256": meta.get("output_sha256"),
        "usage": dict(meta.get("usage") or {}),
    }]
    return {
        "claim_id": claim_id,
        "mode": mode,
        "thesis": output.get("thesis"),
        "premises": output.get("premises"),
        "assumptions": output.get("assumptions"),
        "objections": output.get("objections"),
        "falsifiers": output.get("falsifiers"),
        "confidence": output.get("confidence"),
        "reviewer": f"{role}:{model}",
        "model_runs": model_runs,
        "notes": output.get("notes"),
    }


def _run_argument_jobs(
    graph: Any,
    jobs: list[tuple[str, str, str, str]],
    *,
    model_runner: Callable[..., Any],
    max_workers: int,
) -> list[tuple[bool, Any]]:
    """Run each claim x argument role x model job, in parallel, independently.

    Each worker rebuilds its own deterministic brief, so no brief, prompt or
    model output is shared with another run.
    """

    def one(job: tuple[str, str, str, str]) -> tuple[bool, Any]:
        claim_id, role, mode, model = job
        try:
            from ozagia.research.argument import build_argument_brief

            brief = build_argument_brief(
                graph.state(), claim_id=claim_id, mode=mode)
            messages = _argument_messages(brief, role=role, mode=mode)
            sent_prompt_hash = _messages_sha256(messages)
            sent_context_hash = _context_sha256(messages)
            result = _normalize_runner_result(
                _call_model_runner(model_runner, model, messages))
            result["prompt_sha256"] = (
                result["prompt_sha256"] or sent_prompt_hash)
            result["context_sha256"] = (
                result["context_sha256"] or sent_context_hash)
            result["brief_sha256"] = brief.get("packet_sha256")
            return True, result
        except Exception as exc:  # noqa: BLE001 - reported in errors
            return False, _error(
                "argument", exc, claim_id=claim_id, role=role, mode=mode,
                model=model)

    with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as pool:
        return list(pool.map(one, jobs))


# --------------------------------------------------------------------------- #
# report and bundle invocation
# --------------------------------------------------------------------------- #
def _invoke(function: Callable[..., Any], candidates: Mapping[str, Any]) -> Any:
    """Call ``function`` with the candidates its signature accepts."""
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return function(**dict(candidates))
    positional: list[Any] = []
    keyword: dict[str, Any] = {}
    accepts_extra = False
    for name, parameter in signature.parameters.items():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            accepts_extra = True
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        elif parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            if name in candidates:
                positional.append(candidates[name])
        elif name in candidates:
            keyword[name] = candidates[name]
    if accepts_extra:
        for name, value in candidates.items():
            if name not in keyword:
                keyword[name] = value
    return function(*positional, **keyword)


def _report_and_bundle(
    graph: Any,
    *,
    plan: Mapping[str, Any],
    plan_id: str | None,
    bundle_dir: Any,
    report_builder: Callable[..., Any] | None,
    bundle_writer: Callable[..., Any] | None,
    errors: list[dict[str, Any]],
) -> tuple[Any, Any]:
    """Build the Stage A report and bundle; failures become error entries."""
    state = None
    if graph is not None:
        state_fn = getattr(graph, "state", None)
        if callable(state_fn):
            try:
                state = state_fn()
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(_error("report_state", exc))
    report_function = report_builder
    if report_function is None:
        try:
            from ozagia.research.paper_review import paper_review_report

            report_function = paper_review_report
        except Exception as exc:  # noqa: BLE001 - optional module
            errors.append(_error("report_builder", exc))
    report = None
    if report_function is not None:
        try:
            report = _invoke(report_function, {
                "state": state,
                "graph": graph,
                "plan": plan,
                "plan_id": plan_id,
            })
        except Exception as exc:  # noqa: BLE001 - reported in errors
            errors.append(_error("report", exc))

    writer = bundle_writer
    if writer is None:
        try:
            from ozagia.research.paper_review import (
                write_paper_review_bundle,
            )

            writer = write_paper_review_bundle
        except Exception as exc:  # noqa: BLE001 - optional module
            errors.append(_error("bundle_writer", exc))
    bundle = None
    if writer is not None:
        try:
            bundle = _invoke(writer, {
                "state": state,
                "graph": graph,
                "plan": plan,
                "plan_id": plan_id,
                "output_dir": bundle_dir,
                "bundle_dir": bundle_dir,
            })
        except Exception as exc:  # noqa: BLE001 - reported in errors
            errors.append(_error("bundle", exc))
    return report, bundle


# --------------------------------------------------------------------------- #
# the driver
# --------------------------------------------------------------------------- #
def run_paper_review(
    *,
    plan: Mapping[str, Any],
    packet: Mapping[str, Any],
    model_runner: Callable[..., Any],
    review_roles: Iterable[str] | str | None = None,
    argument_roles: Iterable[str] | str | None = None,
    models: Iterable[str] | str | None = None,
    claim_ids: Iterable[str] | str | None = None,
    graph: Any = None,
    record: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
    bundle_dir: Any = None,
    report_builder: Callable[..., Any] | None = None,
    bundle_writer: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run the review matrix, argument matrix and optional report/bundle.

    Analysis roles default to the plan's roles, falling back to
    :data:`DEFAULT_REVIEW_ROLES`. Argument roles default to
    :data:`DEFAULT_ARGUMENT_ROLES`; each maps to a ``record_argument`` mode.
    Models default to the plan's models. Claim ids default to
    ``packet['claim_ids']`` or the ids in ``packet['claims']`` and are capped
    at :data:`MAX_CLAIMS`.

    Recording happens only when both ``graph`` and ``record`` are supplied.
    With ``record=False`` the argument candidates are returned raw and nothing
    is written. Invalid argument output is reported in ``errors`` and is never
    recorded. A reconciliation is recorded last, when recording.
    """
    if not isinstance(plan, Mapping):
        raise PaperReviewDriverError("plan must be a mapping")
    if not isinstance(packet, Mapping):
        raise PaperReviewDriverError("packet must be a mapping")
    if not callable(model_runner):
        raise PaperReviewDriverError("model_runner must be callable")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) \
            or max_workers < 1:
        raise PaperReviewDriverError(
            "max_workers must be a positive integer")
    if report_builder is not None and not callable(report_builder):
        raise PaperReviewDriverError("report_builder must be callable")
    if bundle_writer is not None and not callable(bundle_writer):
        raise PaperReviewDriverError("bundle_writer must be callable")

    plan_id = _plan_id(plan)
    recording = graph is not None and bool(record)
    if recording and not plan_id:
        raise PaperReviewDriverError(
            "recording needs a plan id (plan['id'])")

    review_role_source = (
        review_roles if review_roles is not None
        else plan.get("roles") or DEFAULT_REVIEW_ROLES
    )
    review_role_names = _names(
        review_role_source,
        "review_roles",
    )
    argument_role_names = _names(
        argument_roles if argument_roles is not None
        else plan.get("argument_roles"),
        "argument_roles",
        default=DEFAULT_ARGUMENT_ROLES,
        allow_empty=True,
    )
    for role in argument_role_names:
        if role not in ARGUMENT_ROLE_MODES:
            raise PaperReviewDriverError(
                f"unknown argument role {role!r}; expected one of "
                f"{sorted(ARGUMENT_ROLE_MODES)}")
    model_names = _names(
        models if models is not None else plan.get("models"), "models")

    errors: list[dict[str, Any]] = []

    # Stage B.1: the existing independent review matrix.
    from ozagia.harness.review import run_review_matrix

    review_result = run_review_matrix(
        plan,
        packet,
        model_runner=model_runner,
        roles=review_role_names,
        models=model_names,
        max_workers=max_workers,
        graph=graph,
        record=recording,
    )
    errors.extend(review_result.get("errors") or [])

    # Stage B.2: independent argument builders for every selected claim.
    selected_claims = _claim_ids(packet, claim_ids)
    argument_jobs = [
        (claim_id, role, ARGUMENT_ROLE_MODES[role], model)
        for claim_id in selected_claims
        for role in argument_role_names
        for model in model_names
    ]
    arguments: list[dict[str, Any]] = []
    if argument_jobs and graph is None:
        errors.append({
            "stage": "arguments",
            "error": (
                "argument briefs need a graph; supply graph=... to run the "
                "argument matrix"
            ),
            "error_type": "PaperReviewDriverError",
            "claim_ids": list(selected_claims),
        })
    elif argument_jobs:
        called = _run_argument_jobs(
            graph, argument_jobs, model_runner=model_runner,
            max_workers=max_workers)
        for (claim_id, role, mode, model), (ok, value) in zip(
                argument_jobs, called):
            if not ok:
                errors.append(value)
                continue
            meta = value
            output = meta["output"]
            output_sha = _sha256_json(output)
            entry: dict[str, Any] = {
                "claim_id": claim_id,
                "role": role,
                "mode": mode,
                "model": model,
                "reviewer": f"{role}:{model}",
                "output": output,
                "usage": meta["usage"],
                "provider": meta["provider"],
                "prompt_sha256": meta["prompt_sha256"],
                "context_sha256": meta["context_sha256"],
                "output_sha256": output_sha,
                "packet_sha256": meta.get("brief_sha256"),
                "recorded": None,
            }
            if recording:
                try:
                    from ozagia.research.argument import record_argument

                    fields = _argument_record_fields(
                        output,
                        claim_id=claim_id,
                        mode=mode,
                        role=role,
                        model=model,
                        meta={
                            **meta,
                            "output_sha256": output_sha,
                        },
                    )
                    entry["recorded"] = record_argument(graph, **fields)
                except Exception as exc:  # noqa: BLE001 - reported in errors
                    errors.append(_error(
                        "argument_validation", exc, claim_id=claim_id,
                        role=role, mode=mode, model=model, output=output))
                    continue
            arguments.append(entry)

    # Stage B.3: preserve dissent, then optionally build the Stage A output.
    reconciliation = None
    if recording:
        try:
            from ozagia.research.structured_review import (
                record_reconciliation,
            )

            reconciliation = record_reconciliation(
                graph, plan_id=plan_id or "", method="consensus")
        except Exception as exc:  # noqa: BLE001 - reported in errors
            errors.append(_error("reconciliation", exc))

    report = None
    bundle = None
    if bundle_dir is not None:
        report, bundle = _report_and_bundle(
            graph,
            plan=plan,
            plan_id=plan_id,
            bundle_dir=bundle_dir,
            report_builder=report_builder,
            bundle_writer=bundle_writer,
            errors=errors,
        )

    review_count = review_result.get("count")
    count = (
        (review_count if isinstance(review_count, int) else
         len(review_result.get("runs") or []))
        + len(arguments)
    )
    return {
        "review": review_result,
        "arguments": arguments,
        "reconciliation": reconciliation,
        "report": report,
        "bundle": bundle,
        "errors": errors,
        "count": count,
        "truth_assessed": False,
    }
