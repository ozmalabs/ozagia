"""
verify.py — Formal claim verification dispatch.

Dispatches FORMAL-CLAIM blocks from a parsed Turn to the registered
verifier backend for the model. Backends:
    smt2  → z3 Python package
    dafny → dafny subprocess
    lean4 → lake env lean subprocess

Each backend is gated by availability (importlib / shutil.which).
Returns status="skipped" if the backend is unavailable, never raises.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

from .registry import ModelEntry
from .types import ClaimBlock, Turn


VerifierStatus = Literal["verified", "refuted", "timeout", "error", "skipped"]


@dataclass
class VerifierResult:
    claim: ClaimBlock
    status: VerifierStatus
    output: str = ""
    error: str | None = None


def verify_formal_claims(turn: Turn, entry: ModelEntry) -> list[VerifierResult]:
    """
    Verify all formal-lang claims in turn against entry.verifier_registry.

    Only claims with formal_lang set are dispatched. Claims whose lang is not
    in entry.verifier_registry are skipped. Claims whose verifier binary/package
    is unavailable are skipped with status="skipped".
    """
    results: list[VerifierResult] = []
    for claim in turn.claims:
        if not claim.formal_lang or not claim.formal_body:
            continue
        backend_name = entry.verifier_registry.get(claim.formal_lang)
        if backend_name is None:
            results.append(VerifierResult(
                claim=claim,
                status="skipped",
                error=f"no verifier registered for {claim.formal_lang}",
            ))
            continue
        result = _dispatch(claim, backend_name)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _dispatch(claim: ClaimBlock, backend_name: str) -> VerifierResult:
    lang = claim.formal_lang
    body = claim.formal_body or ""

    if lang == "smt2" and backend_name == "z3":
        return _verify_z3(claim, body)
    if lang in ("dafny",) and backend_name == "dafny":
        return _verify_subprocess(claim, ["dafny", "verify", "-"], body, timeout=30)
    if lang == "lean4" and backend_name == "lean":
        return _verify_subprocess(claim, ["lake", "env", "lean", "--stdin"], body, timeout=60)

    # Unknown backend name — try subprocess with the name as the command
    if shutil.which(backend_name):
        return _verify_subprocess(claim, [backend_name], body, timeout=30)

    return VerifierResult(
        claim=claim,
        status="skipped",
        error=f"unknown verifier backend: {backend_name!r}",
    )


def _verify_z3(claim: ClaimBlock, body: str) -> VerifierResult:
    if importlib.util.find_spec("z3") is None:
        return VerifierResult(claim=claim, status="skipped", error="z3 package not installed")
    try:
        import z3  # type: ignore
        solver = z3.Solver()
        solver.from_string(body)
        result = solver.check()
        if result == z3.sat:
            return VerifierResult(claim=claim, status="verified", output="sat")
        if result == z3.unsat:
            return VerifierResult(claim=claim, status="refuted", output="unsat")
        return VerifierResult(claim=claim, status="error", output=str(result))
    except Exception as exc:
        return VerifierResult(claim=claim, status="error", error=str(exc))


def _verify_subprocess(
    claim: ClaimBlock,
    cmd: list[str],
    body: str,
    timeout: int,
) -> VerifierResult:
    if not shutil.which(cmd[0]):
        return VerifierResult(
            claim=claim,
            status="skipped",
            error=f"{cmd[0]!r} not found in PATH",
        )
    try:
        proc = subprocess.run(
            cmd,
            input=body,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        if proc.returncode == 0:
            return VerifierResult(claim=claim, status="verified", output=stdout)
        return VerifierResult(
            claim=claim,
            status="refuted",
            output=stdout,
            error=stderr or None,
        )
    except subprocess.TimeoutExpired:
        return VerifierResult(claim=claim, status="timeout", error=f"timeout after {timeout}s")
    except Exception as exc:
        return VerifierResult(claim=claim, status="error", error=str(exc))
