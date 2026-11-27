"""Deterministic validation evidence for the Stage 1 outcome slice.

The outcome journal stores an ``EvidenceReference``; the durable evidence
record itself is written through :mod:`ozagia.core.evidence_store` so there is
still one evidence journal.  A worker's success response is not validation:
this module runs ``git diff --check`` and parses the Python target, records
their commands and exit codes, and binds the evidence to the target digest and
intent revision.
"""

from __future__ import annotations

import ast
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ozagia.core import evidence_store as ES
from ozagia.core import contracts as C
from ozagia.edit import txn

from . import records as R
from .effects import (
    DEFAULT_MAX_BYTES,
    EffectResult,
    resolve_target,
    sha256_bytes,
    sha256_file,
)
from .errors import OutcomeValidationError
from .store import OutcomeStore

VALIDATION_SOURCE = "ozagia.outcomes.validation"
VALIDATION_METHOD = "git_diff_check+python_ast_parse"
VALIDATION_COVERAGE = "diff_whitespace+syntax"
DEFAULT_TIMEOUT_S = 30.0
MAX_OUTPUT_BYTES = 1_048_576


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    command: str
    exit_code: int
    state: str
    output_digest: str | None = None
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.command,
            "exit_code": self.exit_code,
            "state": self.state,
            "output_digest": self.output_digest,
        }


@dataclass(frozen=True)
class ValidationResult:
    intent_id: str
    revision_hash: str
    target: str
    artifact_digest: str | None
    evidence_ref: str
    evidence_record_id: str
    evidence_record_hash: str | None
    evidence_id: str | None
    passed: bool
    method: str
    coverage: str
    freshness: str
    checks: tuple[ValidationCheck, ...] = ()
    duplicate: bool = False
    effect_ref: str | None = None
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def current(self) -> bool:
        return self.freshness == "current"

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "revision_hash": self.revision_hash,
            "target": self.target,
            "artifact_digest": self.artifact_digest,
            "evidence_ref": self.evidence_ref,
            "evidence_record_id": self.evidence_record_id,
            "evidence_record_hash": self.evidence_record_hash,
            "evidence_id": self.evidence_id,
            "passed": self.passed,
            "method": self.method,
            "coverage": self.coverage,
            "freshness": self.freshness,
            "duplicate": self.duplicate,
            "effect_ref": self.effect_ref,
            "checks": [check.to_dict() for check in self.checks],
        }


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _digest_output(stdout: bytes, stderr: bytes) -> str:
    return C.content_hash({
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
    })


def _bounded_output(value: bytes) -> str:
    text = value[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return " ".join(text.split())[:R.MAX_DETAIL]


def _run(argv: Sequence[str], *, cwd: Path, timeout_s: float) -> ValidationCheck:
    display = " ".join(shlex.quote(part) for part in argv)
    try:
        completed = subprocess.run(
            list(argv), cwd=str(cwd), capture_output=True,
            timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or b""
        stderr = exc.stderr or b"timed out"
        return ValidationCheck(
            name="command", command=display, exit_code=124, state="failed",
            output_digest=_digest_output(stdout, stderr),
            stdout=_bounded_output(stdout), stderr=_bounded_output(stderr))
    except OSError as exc:
        return ValidationCheck(
            name="command", command=display, exit_code=127, state="failed",
            output_digest=None, stderr=str(exc)[:R.MAX_DETAIL])
    code = completed.returncode
    return ValidationCheck(
        name="command", command=display, exit_code=code,
        state="passed" if code == 0 else "failed",
        output_digest=_digest_output(completed.stdout, completed.stderr),
        stdout=_bounded_output(completed.stdout),
        stderr=_bounded_output(completed.stderr))


def _git_diff_check(root: Path, target: str, *,
                    timeout_s: float) -> ValidationCheck:
    argv = ["git", "--no-pager", "diff", "--check", "--", target]
    check = _run(argv, cwd=root, timeout_s=timeout_s)
    return ValidationCheck(**{**check.__dict__, "name": "git_diff_check"})


def _python_ast_check(root: Path, target: str, *,
                      timeout_s: float) -> ValidationCheck:
    del timeout_s  # parsing is in-process and bounded by the read limit
    code = (
        "import ast,sys; p=sys.argv[1]; "
        "ast.parse(open(p, encoding='utf-8').read(), p)"
    )
    display = " ".join(shlex.quote(part) for part in
                       [sys.executable, "-c", code, target])
    try:
        source = txn.read_source(
            root, target, suffixes=(".py", ".pyi"),
            max_bytes=DEFAULT_MAX_BYTES, error_cls=OutcomeValidationError)
    except Exception as exc:
        return ValidationCheck(
            name="python_ast_parse", command=display, exit_code=1,
            state="failed", output_digest=None,
            stderr=str(exc)[:R.MAX_DETAIL])
    try:
        ast.parse(source.text, filename=target)
    except SyntaxError as exc:
        detail = f"{exc.msg} (line {exc.lineno})"
        return ValidationCheck(
            name="python_ast_parse", command=display, exit_code=1,
            state="failed", output_digest=None, stderr=detail)
    return ValidationCheck(
        name="python_ast_parse", command=display, exit_code=0,
        state="passed", output_digest=None)


def _evidence_key(effect: EffectResult, artifact_digest: str | None,
                  passed: bool, observed_at: int | str) -> str:
    digest = C.content_hash({
        "effect_key": effect.effect_key,
        "revision_hash": effect.revision_hash,
        "artifact_digest": artifact_digest,
        "passed": passed,
        "observed_at": observed_at,
    })
    return f"validation-{digest[:40]}"


def validate_effect(store: OutcomeStore, effect: EffectResult, *,
                    target: str | None = None,
                    evidence_id: Any = None,
                    actor: Any = None,
                    idempotency_key: str | None = None,
                    observed_at: int | str | None = None,
                    now_ms: int | None = None,
                    timeout_s: float = DEFAULT_TIMEOUT_S) -> ValidationResult:
    """Run the Stage 1 checks and record durable evidence.

    The evidence record is appended to the evidence store first, then an
    ``EvidenceReference`` is appended to the outcome journal.  The outcome
    reference names the evidence-store record id and digest; it does not copy
    the evidence payload into the work journal.
    """
    if not isinstance(store, OutcomeStore):
        raise OutcomeValidationError("store must be an OutcomeStore")
    if not isinstance(effect, EffectResult):
        raise OutcomeValidationError("effect must be an EffectResult")
    if timeout_s <= 0:
        raise OutcomeValidationError("timeout_s must be positive")
    state = store.materialize()
    intent = state.intent(effect.intent_id)
    state.revision(intent.intent_id, effect.revision_hash)
    if effect.target and target is not None and target != effect.target:
        raise OutcomeValidationError(
            "validation target does not match the effect target")
    target_value = R.ref(target or effect.target, "target")
    assert target_value is not None
    path = resolve_target(store.root, target_value)
    if path.suffix.lower() not in (".py", ".pyi"):
        raise OutcomeValidationError(
            "Stage 1 validation requires a Python (.py/.pyi) target")
    artifact = sha256_file(path)
    expected = effect.new_hash
    freshness = "current" if artifact is not None and artifact == expected \
        else "stale"
    moment = _now_ms() if now_ms is None else R.timestamp(now_ms, "now_ms")
    observed = moment if observed_at is None else observed_at
    diff_check = _git_diff_check(store.root, target_value,
                                 timeout_s=timeout_s)
    ast_check = _python_ast_check(store.root, target_value,
                                  timeout_s=timeout_s)
    checks = (diff_check, ast_check)
    passed = bool(
        all(check.exit_code == 0 for check in checks)
        and freshness == "current")
    command = diff_check.command
    overall_exit = 0 if passed else 1
    coverage = VALIDATION_COVERAGE if freshness == "current" \
        else "diff_whitespace+syntax+drifted_artifact"
    notes = "; ".join(
        f"{check.name}={check.state}:{check.exit_code}" for check in checks)
    detail = f"{notes}; artifact={artifact or 'absent'}; " \
             f"freshness={freshness}"

    evidence_key = idempotency_key or _evidence_key(
        effect, artifact, passed, observed)
    refs: dict[str, Any] = {}
    if effect.expected_base:
        refs["source_hash"] = effect.expected_base
    if artifact:
        refs["output_digest"] = artifact
    evidence = ES.record_validation(
        store.root, source=VALIDATION_SOURCE, command=command,
        exit_code=overall_exit, target=target_value,
        method=VALIDATION_METHOD, passed=passed, observed_at=observed,
        expected_base=effect.expected_base, observed_base=effect.observed_base,
        artifact_digest=artifact, coverage=coverage,
        checks=[check.to_dict() for check in checks], refs=refs,
        now_ms=moment, idempotency_key=evidence_key)
    evidence_record = evidence["record"]
    evidence_record_id = evidence_record["record_id"]
    evidence_record_hash = evidence_record.get("record_hash")
    result_refs = [
        f"evidence:{evidence_record_id}",
        f"evidence_hash:{evidence_record_hash}",
        f"target:{target_value}",
    ]
    if effect.effect_ref:
        result_refs.append(f"effect:{effect.effect_ref}")
    elif effect.effect_key:
        result_refs.append(f"effect_key:{effect.effect_key}")
    outcome_key = idempotency_key or _evidence_key(
        effect, artifact, passed, observed)
    outcome = store.record_evidence(
        intent.intent_id, source=VALIDATION_SOURCE, evidence_id=evidence_id,
        artifact_digest=artifact, method=VALIDATION_METHOD,
        coverage=coverage, freshness=freshness, refs=result_refs,
        detail=detail, revision_hash=effect.revision_hash, actor=actor,
        idempotency_key=outcome_key, now_ms=moment)
    return ValidationResult(
        intent_id=intent.intent_id,
        revision_hash=effect.revision_hash,
        target=target_value,
        artifact_digest=artifact,
        evidence_ref=outcome["record_id"],
        evidence_record_id=evidence_record_id,
        evidence_record_hash=evidence_record_hash,
        evidence_id=outcome.get("evidence_id"),
        passed=passed,
        method=VALIDATION_METHOD,
        coverage=coverage,
        freshness=freshness,
        checks=checks,
        duplicate=bool(outcome.get("duplicate")),
        effect_ref=effect.effect_ref,
        record=outcome["record"],
    )


def read_validation_record(root: str | Path,
                           record_id: str) -> dict[str, Any] | None:
    """Read one validation record from the evidence journal, or ``None``."""
    value = R.identifier(record_id, "record_id")
    view = ES.read_store(root, limit=ES.MAX_RECORDS, kind="validation")
    for record in view["records"]:
        if record.get("record_id") == value:
            return record
    return None


def validation_record_for_ref(root: str | Path,
                              evidence_ref: str) -> dict[str, Any] | None:
    """Resolve the evidence-store record named by an outcome evidence ref."""
    if not isinstance(evidence_ref, str):
        return None
    marker = "evidence:"
    if not evidence_ref.startswith(marker):
        return None
    return read_validation_record(root, evidence_ref[len(marker):])


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_OUTPUT_BYTES",
    "VALIDATION_COVERAGE",
    "VALIDATION_METHOD",
    "VALIDATION_SOURCE",
    "ValidationCheck",
    "ValidationResult",
    "read_validation_record",
    "validate_effect",
    "validation_record_for_ref",
]
