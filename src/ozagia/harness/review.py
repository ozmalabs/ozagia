"""ozagia.harness.review — independent multi-model review runs.

The core never calls a model. This module is the driver: it builds one
role-specific prompt per reviewer role, runs every role x model combination
independently (no reviewer sees another reviewer's output), validates each
result, and optionally records it in the research graph.

`run_review_matrix` takes an injectable ``model_runner`` and injected
``validator``/``recorder`` hooks so tests never need a live model or the
research-review module. With no hooks supplied it lazily imports
``ozagia.research.structured_review`` at call time; this module imports none of
that at module load time.

`openai_runner` returns a ``model_runner`` for an OpenAI-compatible
``/chat/completions`` endpoint. The API key is read from an environment
variable or file and is only used in the Authorization header: it is never put
in messages, prompts, return values or log lines.

Python API::

    runner = openai_runner(base_url="https://host/v1", model="model-name",
                           api_key_env="OPENAI_API_KEY")
    result = run_review_matrix(plan, packet, model_runner=runner)

CLI (no live calls in tests)::

    OZAGIA_EVAL_LIVE=1 python -m ozagia.harness.review --live \\
        --plan PLAN.json --packet PACKET.json --base-url https://host/v1 \\
        --models model-a,model-b --roles methods,statistics --output runs.json
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

DEFAULT_ROUND = "independent"
DEFAULT_MAX_WORKERS = 4
MAX_HTTP_ATTEMPTS = 3
RETRY_BACKOFF_S = 0.25
PROVIDER = "openai-compatible"
JSON_RESPONSE_FORMAT = {"type": "json_object"}

# Roles named by the structured-review plan. An unknown role is still a
# reviewer: it gets a generic prompt instead of being rejected.
ROLE_PROMPTS: dict[str, str] = {
    "methods": (
        "You are a methods reviewer. Examine the study or argument design: "
        "question-to-method fit, sampling and data collection, measurement, "
        "confounds, controls, inference and execution. Point out where a "
        "conclusion outruns what the method can establish."
    ),
    "statistics": (
        "You are a statistics reviewer. Examine test choice and assumptions, "
        "power, multiplicity, effect sizes, uncertainty intervals, missing "
        "data, model specification, and the difference between statistical "
        "and substantive significance. Check reported numbers where possible."
    ),
    "domain": (
        "You are a domain-expert reviewer. Examine definitions, scope, "
        "substantive accuracy, use of the literature, and whether the claim "
        "fits what is known in the field. Distinguish genuine disagreement "
        "from imprecision or missing context."
    ),
    "reproducibility": (
        "You are a reproducibility reviewer. Examine whether the data, code, "
        "materials, versions, parameters, seeds, environment and analysis "
        "steps are sufficient for an independent person to repeat the work. "
        "Name the missing artifact or step precisely."
    ),
    "ethics": (
        "You are an ethics and integrity reviewer. Examine consent, privacy, "
        "risk of harm, fairness, dual use, reporting obligations, conflicts "
        "of interest, and compliance with the relevant rules. Flag concerns "
        "as reviewable issues, not as verdicts."
    ),
    "advocate": (
        "You are the advocate (steelman) reviewer. Present the strongest "
        "honest case for the target claim: its best premises, assumptions, "
        "evidence and replies to obvious objections. A steelman is not an "
        "endorsement; state what would still falsify it."
    ),
    "critic": (
        "You are the critic (red-team) reviewer. Present the strongest "
        "objections: unstated assumptions, alternative explanations, missing "
        "evidence, failure modes, and concrete falsifiers or checks that "
        "would distinguish the claim from its rivals."
    ),
    "synthesizer": (
        "You are the synthesizer reviewer. Reconcile the packet's material "
        "into the issues that matter, preserving disagreement instead of "
        "voting it away. Separate what the evidence supports, what remains "
        "undetermined, and what a later reviewer must check."
    ),
}
GENERIC_REVIEWER_PROMPT = (
    "You are an independent scholarly reviewer. Apply the standards of your "
    "role to the packet: examine claims, evidence, methods, citations and "
    "limitations, and report concrete, checkable issues with clear targets."
)

# This mirrors structured_review.validate_review_output: findings carry a
# wording, one of its four severities, and graph ids for their targets. A
# model that follows this schema can be validated and recorded unchanged.
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "findings"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "Short review summary; not a truth verdict.",
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific strengths of the work or argument.",
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["wording", "severity", "issue_type"],
                "properties": {
                    "wording": {
                        "type": "string",
                        "description": "The issue, stated as a checkable claim.",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["blocking", "major", "minor", "note"],
                    },
                    "issue_type": {"type": "string"},
                    "claim_id": {
                        "type": "string",
                        "description": "Claim node id from the packet.",
                    },
                    "reference_id": {
                        "type": "string",
                        "description": "Reference node id from the packet.",
                    },
                    "argument_id": {
                        "type": "string",
                        "description": "Argument node id from the packet.",
                    },
                    "evidence_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "citation_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "quote": {
                        "type": "string",
                        "description": "Text present in the packet.",
                    },
                    "recommendation": {"type": "string"},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
            },
        },
        "recommendation": {"type": "string"},
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
    },
}


class ReviewDriverError(RuntimeError):
    """Invalid driver configuration or a failed model request."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# deterministic prompts and hashes
# --------------------------------------------------------------------------- #
def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def messages_sha256(messages: Iterable[Mapping[str, Any]]) -> str:
    """Hash of the full prompt message list, in order."""
    return _sha256_text(_canonical_json(list(messages)))


def context_sha256(messages: Iterable[Mapping[str, Any]]) -> str:
    """Hash of the system and user content ("system+user")."""
    parts = [
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, Mapping)
        and message.get("role") in ("system", "user")
    ]
    return _sha256_text("".join(parts))


def _packet_sha256(packet: Mapping[str, Any]) -> str:
    for key in ("packet_sha256", "sha256", "packet_hash"):
        value = packet.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _sha256_text(_canonical_json(packet))


def role_prompt(role: str) -> str:
    """The role-specific instructions; unknown roles get the generic prompt."""
    return ROLE_PROMPTS.get(str(role).strip().casefold(), GENERIC_REVIEWER_PROMPT)


def build_review_messages(packet: Mapping[str, Any], *, role: str,
                          round: str = DEFAULT_ROUND) -> list[dict[str, str]]:
    """The two messages for one independent reviewer run.

    The packet is rendered deterministically and the role and round are in
    both messages. No other reviewer's output is ever included.
    """
    system = (
        f"{role_prompt(role)}\n\n"
        f"You are the {role!r} reviewer in the {round!r} round. Every reviewer "
        "works independently: you have not seen, and must not assume, any "
        "other reviewer's output. You are reviewing, not deciding truth; "
        "findings are candidates and may be wrong. Ground each finding in the "
        "packet and give a target that a person can check. Return one JSON "
        "object only, matching the schema in the user message."
    )
    user = (
        f"Review role: {role}\n"
        f"Round: {round}\n\n"
        "Review packet (deterministic JSON):\n"
        f"{_canonical_json(packet)}\n\n"
        "Return exactly one JSON object and nothing else. It must match this "
        "strict schema:\n"
        f"{json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, sort_keys=True, indent=2)}\n"
    )
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


# --------------------------------------------------------------------------- #
# input and hook normalization
# --------------------------------------------------------------------------- #
def _names(value: Any, label: str) -> list[str]:
    """A de-duplicated, ordered list of non-empty names from a list or CSV."""
    if value is None:
        raise ReviewDriverError(f"no {label} given: pass {label}=... or put "
                                f"'{label}' in the plan")
    if isinstance(value, str):
        pieces: Iterable[Any] = value.split(",")
    elif isinstance(value, Mapping):
        raise ReviewDriverError(f"{label} must be a list or comma-separated string")
    elif isinstance(value, (set, frozenset)):
        pieces = sorted(value, key=str)
    else:
        try:
            pieces = list(value)
        except TypeError as exc:
            raise ReviewDriverError(
                f"{label} must be a list or comma-separated string") from exc
    names: list[str] = []
    for piece in pieces:
        name = str(piece).strip()
        if name and name not in names:
            names.append(name)
    if not names:
        raise ReviewDriverError(f"{label} is empty")
    return names


def _plan_id(plan: Mapping[str, Any]) -> str | None:
    value = plan.get("id") or plan.get("plan_id")
    return str(value) if value else None


def _slug(value: Any) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip().casefold())
    return slug.strip("-") or "unknown"


def reviewer_id(role: str, model: str) -> str:
    """The stable reviewer id used in ReviewRun records: ``role:model``."""
    return f"{_slug(role)}:{_slug(model)}"


def _accepts_kwarg(function: Callable[..., Any], name: str) -> bool:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return True
    parameters = signature.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return True
    return name in parameters


def _as_output(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        return _parse_json_object(value)
    raise ReviewDriverError("model output is not a JSON object")


def _normalize_usage(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _usage_tokens(usage: Mapping[str, Any]) -> dict[str, int]:
    tokens: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                "input_tokens", "output_tokens", "input", "output"):
        number = usage.get(key)
        if isinstance(number, (int, float)) and not isinstance(number, bool):
            tokens[key] = int(number)
    if "total_tokens" not in tokens:
        for prompt_key, completion_key in (
                ("prompt_tokens", "completion_tokens"),
                ("input_tokens", "output_tokens"),
                ("input", "output")):
            if prompt_key in tokens or completion_key in tokens:
                tokens["total_tokens"] = (
                    tokens.get(prompt_key, 0) + tokens.get(completion_key, 0))
                break
    return tokens


def _usage_cost(usage: Mapping[str, Any]) -> float | None:
    for key in ("cost_usd", "cost", "total_cost"):
        number = _number(usage.get(key))
        if number is not None:
            return number
    return None


def _normalize_runner_result(raw: Any) -> dict[str, Any]:
    """Accept either the parsed output or the metadata wrapper."""
    if not isinstance(raw, Mapping):
        raise ReviewDriverError("model runner did not return a mapping")
    if "output" not in raw:
        return {"output": dict(raw), "usage": {}, "provider": None,
                "prompt_sha256": None, "context_sha256": None, "params": {}}
    usage = _normalize_usage(raw.get("usage"))
    provider = raw.get("provider")
    prompt_hash = raw.get("prompt_sha256")
    context_hash = raw.get("context_sha256")
    params = raw.get("params")
    return {
        "output": _as_output(raw.get("output")),
        "usage": usage,
        "provider": str(provider) if provider is not None else None,
        "prompt_sha256": prompt_hash if isinstance(prompt_hash, str) else None,
        "context_sha256": context_hash if isinstance(context_hash, str) else None,
        "params": dict(params) if isinstance(params, Mapping) else {},
    }


def _call_model_runner(model_runner: Callable[..., Any], model: str,
                       messages: list[dict[str, str]]) -> Any:
    if _accepts_kwarg(model_runner, "response_format"):
        return model_runner(model, messages,
                            response_format=dict(JSON_RESPONSE_FORMAT))
    return model_runner(model, messages)


def _normalize_validation(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bool):
        return {"valid": value}
    if (isinstance(value, tuple) and len(value) == 2
            and isinstance(value[0], bool)):
        return {"valid": value[0], "errors": list(value[1] or [])}
    if value is None:
        return {"valid": False,
                "errors": ["validator returned no validation result"]}
    return {"valid": False,
            "errors": [f"unrecognized validation result: {type(value).__name__}"]}


def _validation_valid(validation: Mapping[str, Any]) -> bool:
    for key in ("valid", "ok", "is_valid", "passed"):
        if key in validation:
            value = validation[key]
            if isinstance(value, str):
                return value.strip().casefold() not in ("false", "no", "0", "")
            return bool(value)
    for key in ("errors", "issues", "problems", "failures"):
        if key in validation:
            return not bool(validation[key])
    if "status" in validation:
        return str(validation["status"]).strip().casefold() in {
            "ok", "valid", "passed", "success"}
    return False


def _validator_call(validator: Callable[..., Any], output: Mapping[str, Any],
                    packet: Mapping[str, Any], state: Any) -> Any:
    kwargs: dict[str, Any] = {}
    if _accepts_kwarg(validator, "state"):
        kwargs["state"] = state
    if _accepts_kwarg(validator, "packet"):
        kwargs["packet"] = packet
    if _accepts_kwarg(validator, "drop_invalid_findings"):
        kwargs["drop_invalid_findings"] = True
    try:
        return validator(output, **kwargs)
    except TypeError:
        if kwargs:
            # A validator whose signature does not really take these hooks.
            return validator(output)
        raise


def _validate(validator: Callable[..., Any], output: Mapping[str, Any],
              packet: Mapping[str, Any], graph: Any) -> dict[str, Any]:
    state = graph if isinstance(graph, Mapping) else None
    if graph is not None and not isinstance(graph, Mapping):
        state_fn = getattr(graph, "state", None)
        if callable(state_fn):
            try:
                state = state_fn()
            except Exception:  # noqa: BLE001 - validation can work without state
                state = None
    try:
        return _normalize_validation(
            _validator_call(validator, output, packet, state))
    except Exception as exc:  # noqa: BLE001 - reported as a validation error
        return {"valid": False,
                "errors": [f"validator failed: {type(exc).__name__}: {exc}"]}


def _structured_review() -> Any:
    """Lazy import: Stage A owns this module and may not exist yet."""
    try:
        from ozagia.research import structured_review
    except ImportError as exc:
        raise ReviewDriverError(
            "validation and recording need ozagia.research.structured_review; "
            "inject validator/recorder hooks when it is unavailable"
        ) from exc
    return structured_review


def _default_validator() -> Callable[..., Any]:
    function = getattr(_structured_review(), "validate_review_output", None)
    if not callable(function):
        raise ReviewDriverError(
            "structured_review.validate_review_output is unavailable")
    return function


def _default_recorder() -> Callable[..., Any]:
    function = getattr(_structured_review(), "record_review_run", None)
    if not callable(function):
        raise ReviewDriverError(
            "structured_review.record_review_run is unavailable")
    return function


def _record_output(output: Mapping[str, Any],
                   validation: Mapping[str, Any]) -> dict[str, Any]:
    """Use normalized findings; invalid findings may have been dropped."""
    findings = validation.get("findings")
    if not isinstance(findings, list):
        return dict(output)
    cleaned = {
        key: output[key]
        for key in ("summary", "strengths", "recommendation", "confidence")
        if key in output
    }
    cleaned["findings"] = findings
    return cleaned


def _recorder_payload(*, plan_id: str, role: str, model: str, round: str,
                      packet_sha: str, output: Mapping[str, Any],
                      usage: Mapping[str, Any], provider: str | None,
                      prompt_sha: str, context_sha: str,
                      params: Mapping[str, Any]) -> dict[str, Any]:
    tokens = _usage_tokens(usage)
    cost = _usage_cost(usage)
    return {
        "plan_id": plan_id,
        "reviewer_id": reviewer_id(role, model),
        "role": role,
        "model": model,
        "round": round,
        "packet_sha256": packet_sha,
        "output": dict(output),
        "provider": provider,
        "prompt_sha256": prompt_sha,
        "context_sha256": context_sha,
        "usage": dict(usage),
        "tokens": tokens,
        "cost": cost,
        "params": dict(params),
    }


def _call_recorder(recorder: Callable[..., Any], graph: Any,
                   payload: Mapping[str, Any]) -> Any:
    """Call the recorder with only the keywords its signature accepts."""
    keyword = dict(payload)
    try:
        signature = inspect.signature(recorder)
    except (TypeError, ValueError):
        return recorder(graph, **keyword)
    parameters = signature.parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in parameters.values()):
        keyword = {
            key: value for key, value in keyword.items()
            if key in parameters
            and parameters[key].kind is not inspect.Parameter.POSITIONAL_ONLY
        }
    aliases = {
        "cost": payload.get("cost"),
        "cost_usd": payload.get("cost"),
        "reviewer": payload["reviewer_id"],
        "plan": payload["plan_id"],
        "model_params": payload.get("params"),
        "run_params": payload.get("params"),
        "prompt_tokens": payload.get("tokens", {}).get("prompt_tokens"),
        "completion_tokens": payload.get("tokens", {}).get("completion_tokens"),
        "total_tokens": payload.get("tokens", {}).get("total_tokens"),
        "tokens_in": payload.get("tokens", {}).get("prompt_tokens"),
        "tokens_out": payload.get("tokens", {}).get("completion_tokens"),
    }
    for key, value in aliases.items():
        if key in parameters and key not in keyword:
            keyword[key] = value
    return recorder(graph, **keyword)


def _recorded_result(value: Any) -> dict[str, Any]:
    """Normalize a recorder return into ``{"run": ..., "findings": [...]}``."""
    if isinstance(value, Mapping):
        if "run" in value or "findings" in value:
            run = value.get("run") or value.get("run_node")
            findings = value.get("findings") or []
            return {"run": run,
                    "findings": list(findings) if isinstance(findings, (list, tuple))
                    else [findings]}
        if value.get("type") == "ReviewRun" or "id" in value:
            return {"run": dict(value), "findings": []}
        return {"run": None, "findings": [], "result": dict(value)}
    if isinstance(value, tuple) and len(value) == 2:
        findings = value[1] or []
        return {"run": value[0],
                "findings": list(findings) if isinstance(findings, (list, tuple))
                else [findings]}
    return {"run": None, "findings": [], "result": value}


# --------------------------------------------------------------------------- #
# the matrix
# --------------------------------------------------------------------------- #
def run_review_matrix(
    plan: Mapping[str, Any],
    packet: Mapping[str, Any],
    *,
    model_runner: Callable[..., Any],
    roles: Iterable[str] | str | None = None,
    models: Iterable[str] | str | None = None,
    round: str = DEFAULT_ROUND,
    max_workers: int = DEFAULT_MAX_WORKERS,
    graph: Any = None,
    record: bool = False,
    validator: Callable[..., Any] | None = None,
    recorder: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run every role x model combination independently.

    ``plan`` supplies roles/models when the arguments are omitted. The result
    is ordered by role and then model, regardless of completion order. Each
    successful model call is validated; invalid outputs keep their output and
    validation in ``errors`` and are not recorded. ``count`` is the number of
    validated runs and ``failed`` the number of error entries.
    ``truth_assessed`` is always false: reviewers are not truth.
    """
    if not isinstance(plan, Mapping):
        raise ReviewDriverError("plan must be a mapping")
    if not isinstance(packet, Mapping):
        raise ReviewDriverError("packet must be a mapping")
    if not callable(model_runner):
        raise ReviewDriverError("model_runner must be callable")
    if validator is not None and not callable(validator):
        raise ReviewDriverError("validator must be callable")
    if (graph is not None and record and recorder is not None
            and not callable(recorder)):
        raise ReviewDriverError("recorder must be callable")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) \
            or max_workers < 1:
        raise ReviewDriverError("max_workers must be a positive integer")

    role_names = _names(roles if roles is not None else plan.get("roles"),
                        "roles")
    model_names = _names(models if models is not None else plan.get("models"),
                         "models")
    round_name = str(round)
    try:
        packet_hash = _packet_sha256(packet)
    except (TypeError, ValueError) as exc:
        raise ReviewDriverError(
            f"packet must be JSON-serializable: {exc}") from exc
    plan_id = _plan_id(plan)

    if graph is not None and record and not plan_id:
        raise ReviewDriverError("recording needs a plan id (plan['id'])")

    # Resolve hooks once, before spending model calls. This is the only lazy
    # import of Stage A; injected hooks keep tests independent of it.
    validate = validator if validator is not None else _default_validator()
    write_run = None
    if graph is not None and record:
        write_run = recorder if recorder is not None else _default_recorder()

    jobs = [(role, model) for role in role_names for model in model_names]

    def one(job: tuple[str, str]) -> tuple[bool, Any, Any, str, str]:
        role, model = job
        messages: list[dict[str, str]] = []
        try:
            messages = build_review_messages(
                packet, role=role, round=round_name)
            # Hash the exact prompt sent, before a runner can mutate it.
            sent_prompt_hash = messages_sha256(messages)
            sent_context_hash = context_sha256(messages)
            result = _normalize_runner_result(
                _call_model_runner(model_runner, model, messages))
            return (True, result, messages, sent_prompt_hash,
                    sent_context_hash)
        except Exception as exc:  # noqa: BLE001 - reported in errors
            return False, exc, messages, "", ""

    # All jobs are submitted before results are consumed, so the calls run in
    # parallel; map preserves role/model order for deterministic output.
    with ThreadPoolExecutor(max_workers=min(max_workers, len(jobs))) as pool:
        called = list(pool.map(one, jobs))

    runs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for (role, model), (ok, value, _messages, sent_prompt_hash,
                        sent_context_hash) in zip(jobs, called):
        if not ok:
            errors.append({
                "role": role,
                "model": model,
                "stage": "model",
                "error": f"{type(value).__name__}: {value}",
                "error_type": type(value).__name__,
            })
            continue
        meta = value
        output = meta["output"]
        prompt_hash = meta["prompt_sha256"] or sent_prompt_hash
        context_hash = meta["context_sha256"] or sent_context_hash
        usage = meta["usage"]
        params = meta["params"] or (
            dict(plan.get("params")) if isinstance(plan.get("params"), Mapping)
            else {})

        validation = _validate(validate, output, packet, graph)
        if not _validation_valid(validation):
            details = validation.get("errors") or validation.get("issues") or []
            if not isinstance(details, (list, tuple)):
                details = [details]
            message = "validation failed"
            if details:
                message += ": " + "; ".join(str(item) for item in details)
            errors.append({
                "role": role,
                "model": model,
                "stage": "validation",
                "error": message,
                "validation": validation,
                "output": output,
            })
            continue

        recorded_output = _record_output(output, validation)
        entry: dict[str, Any] = {
            "role": role,
            "model": model,
            "reviewer_id": reviewer_id(role, model),
            "round": round_name,
            "output": output,
            "recorded_output": recorded_output,
            "validation": validation,
            "usage": usage,
            "provider": meta["provider"],
            "prompt_sha256": prompt_hash,
            "context_sha256": context_hash,
            "packet_sha256": packet_hash,
            "params": params,
        }
        if write_run is not None:
            payload = _recorder_payload(
                plan_id=plan_id or "",
                role=role,
                model=model,
                round=round_name,
                packet_sha=packet_hash,
                output=recorded_output,
                usage=usage,
                provider=meta["provider"],
                prompt_sha=prompt_hash,
                context_sha=context_hash,
                params=params,
            )
            try:
                entry["recorded"] = _recorded_result(
                    _call_recorder(write_run, graph, payload))
            except Exception as exc:  # noqa: BLE001 - reported in errors
                entry["record_error"] = f"{type(exc).__name__}: {exc}"
                errors.append({
                    "role": role,
                    "model": model,
                    "stage": "record",
                    "error": f"recording failed: {type(exc).__name__}: {exc}",
                    "error_type": type(exc).__name__,
                })
        runs.append(entry)

    return {
        "runs": runs,
        "errors": errors,
        "count": len(runs),
        "failed": len(errors),
        "round": round_name,
        "truth_assessed": False,
    }


# --------------------------------------------------------------------------- #
# OpenAI-compatible runner
# --------------------------------------------------------------------------- #
def _parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model content, tolerating a fenced block."""
    from ozagia.harness.structured_output import normalize_json_response

    body = normalize_json_response(text).strip()
    if body.startswith("```"):
        first_newline = body.find("\n")
        body = body[first_newline + 1:] if first_newline >= 0 else body[3:]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    try:
        value = json.loads(body)
    except ValueError:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            raise ReviewDriverError(
                "model output is not a JSON object") from None
        try:
            value = json.loads(body[start:end + 1])
        except ValueError:
            raise ReviewDriverError(
                "model output is not a JSON object") from None
    if not isinstance(value, Mapping):
        raise ReviewDriverError("model output is not a JSON object")
    return dict(value)


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _http_json(url: str, body: Any, headers: Mapping[str, str],
               timeout: float) -> Any:
    """One JSON POST. HTTP status errors carry ``status`` for the retry loop."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", errors="replace")
        raise ReviewDriverError(f"HTTP {exc.code}: {detail}",
                                status=exc.code) from None
    except (TimeoutError, socket.timeout):
        raise ReviewDriverError("request timed out") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise ReviewDriverError("request timed out") from None
        raise ReviewDriverError(f"cannot reach {url}: {exc.reason}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ReviewDriverError("response is not JSON") from None


def _retryable(exc: BaseException) -> bool:
    for attribute in ("status", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int) and (value == 429 or 500 <= value < 600):
            return True
    match = re.search(r"\bHTTP\s+(\d{3})\b", str(exc))
    if match:
        code = int(match.group(1))
        return code == 429 or 500 <= code < 600
    return False


def _post_with_retries(http: Callable[..., Any], url: str, body: Any,
                       headers: Mapping[str, str], timeout: float) -> Any:
    wait = RETRY_BACKOFF_S
    for attempt in range(1, MAX_HTTP_ATTEMPTS + 1):
        try:
            return http(url, body, dict(headers), timeout)
        except Exception as exc:  # noqa: BLE001 - retry policy is below
            if attempt >= MAX_HTTP_ATTEMPTS or not _retryable(exc):
                if isinstance(exc, ReviewDriverError):
                    raise
                status = getattr(exc, "status", None)
                if not isinstance(status, int):
                    status = getattr(exc, "code", None)
                raise ReviewDriverError(
                    f"model request failed: {type(exc).__name__}: {exc}",
                    status=status if isinstance(status, int) else None) from exc
            time.sleep(wait)
            wait *= 2
    raise AssertionError("unreachable")


def _read_api_key(api_key_env: str | None, api_key_file: str | Path | None) -> str | None:
    """Read the key from a file (preferred) or the named environment variable."""
    if api_key_file:
        try:
            key = Path(api_key_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ReviewDriverError(f"cannot read API key file: {exc}") from exc
        if not key:
            raise ReviewDriverError("API key file is empty")
        return key
    if api_key_env:
        key = os.environ.get(api_key_env)
        if key and key.strip():
            return key.strip()
    return None


def _message_content(message: Any) -> Any:
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping)
        )
    return content


def openai_runner(
    *,
    base_url: str,
    model: str,
    api_key_env: str | None = "OPENAI_API_KEY",
    api_key_file: str | Path | None = None,
    timeout: float = 300,
    temperature: float | None = 0.0,
    http_json: Callable[..., Any] | None = None,
) -> Callable[..., dict[str, Any]]:
    """Build a ``model_runner`` for an OpenAI-compatible chat endpoint.

    The returned callable takes ``(model, messages, *, response_format=None)``
    and returns the parsed output with usage and prompt/context hashes. The API
    key is only placed in the Authorization header.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ReviewDriverError("base_url is required")
    if not isinstance(model, str) or not model.strip():
        raise ReviewDriverError("model is required")
    if http_json is not None and not callable(http_json):
        raise ReviewDriverError("http_json must be callable")
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    api_key = _read_api_key(api_key_env, api_key_file)
    request = http_json if http_json is not None else _http_json
    headers = _headers(api_key)

    def runner(target_model: str, messages: list[dict[str, Any]],
               *, response_format: Any = None) -> dict[str, Any]:
        chosen_model = str(target_model or model)
        format_value = (JSON_RESPONSE_FORMAT if response_format is None
                        else response_format)
        prompt_hash = messages_sha256(messages)
        context_hash = context_sha256(messages)
        body: dict[str, Any] = {
            "model": chosen_model,
            "messages": messages,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if format_value:
            body["response_format"] = dict(format_value)
        try:
            document = _post_with_retries(
                request, endpoint, body, headers, timeout)
        except ReviewDriverError as exc:
            # An endpoint that does not support JSON mode can say so with a
            # 400. Retry once without response_format; other 400s stand.
            unsupported = (body.get("response_format")
                           and getattr(exc, "status", None) == 400
                           and "response_format" in str(exc).casefold())
            if not unsupported:
                raise
            body.pop("response_format", None)
            document = _post_with_retries(
                request, endpoint, body, headers, timeout)
        if not isinstance(document, Mapping):
            raise ReviewDriverError("model response is not a JSON object")
        choices = document.get("choices")
        message = (choices[0].get("message")
                   if isinstance(choices, list) and choices
                   and isinstance(choices[0], Mapping) else None)
        content = _message_content(message)
        if content is None:
            raise ReviewDriverError(
                "model response has no choices[0].message.content")
        output = _as_output(content)
        usage = _normalize_usage(document.get("usage"))
        return {
            "output": output,
            "usage": usage,
            "provider": PROVIDER,
            "prompt_sha256": prompt_hash,
            "context_sha256": context_hash,
        }

    return runner


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ozagia.harness.review",
        description="Run independent review roles over a deterministic packet.")
    parser.add_argument("--packet", required=True,
                        help="review packet JSON file")
    parser.add_argument("--plan", required=True, help="review plan JSON file")
    parser.add_argument("--base-url", required=True,
                        help="OpenAI-compatible API root, e.g. http://host/v1")
    parser.add_argument("--models", default=None,
                        help="comma-separated model ids (default: plan models)")
    parser.add_argument("--roles", default=None,
                        help="comma-separated roles (default: plan roles)")
    parser.add_argument("--output", default=None,
                        help="write the result JSON here instead of stdout")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--api-key-file", default=None)
    parser.add_argument("--live", action="store_true",
                        help="confirm live model calls (also needs "
                             "OZAGIA_EVAL_LIVE=1)")
    parser.add_argument("--round", default=DEFAULT_ROUND)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--temperature", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.live or os.environ.get("OZAGIA_EVAL_LIVE") != "1":
        print("error: live model calls need --live and OZAGIA_EVAL_LIVE=1",
              file=sys.stderr)
        return 2
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        packet = json.loads(Path(args.packet).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(plan, Mapping) or not isinstance(packet, Mapping):
        print("error: plan and packet must be JSON objects", file=sys.stderr)
        return 2
    try:
        model_names = _names(
            args.models if args.models is not None else plan.get("models"),
            "models")
        role_names = _names(
            args.roles if args.roles is not None else plan.get("roles"),
            "roles")
        runner = openai_runner(
            base_url=args.base_url,
            model=model_names[0],
            api_key_env=args.api_key_env,
            api_key_file=args.api_key_file,
            temperature=args.temperature,
        )
        result = run_review_matrix(
            plan, packet, model_runner=runner, roles=role_names,
            models=model_names, round=args.round, max_workers=args.max_workers)
    except ReviewDriverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n"
    if args.output:
        try:
            Path(args.output).write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
