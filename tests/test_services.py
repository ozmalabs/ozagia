"""Tests for the versioned service envelope and caller-owned deadlines."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ozagia.core import contracts as C
from ozagia.core import services as S

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"


def make_env(**fields):
    base = {
        "service": "demo.preflight",
        "status": "ok",
        "data": {"b": 2, "a": [1, True, None]},
        "side_effect": "read",
        "started_at": 10,
        "duration_ms": 5,
    }
    base.update(fields)
    return S.build(**base)


class Tick:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.values[min(self.calls - 1, len(self.values) - 1)]


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def test_envelope_has_the_documented_shape():
    env = make_env()
    assert S.ENVELOPE_FIELDS == (
        "schema", "service", "version", "status", "data", "error",
        "provenance", "side_effect", "started_at", "duration_ms", "truncated",
    )
    assert env["schema"] == {"namespace": "ozagia.service", "version": "1.0"}
    assert env["service"] == "demo.preflight"
    assert env["version"] == "1.0"
    assert env["status"] == "ok"
    assert env["data"] == {"b": 2, "a": [1, True, None]}
    assert env["error"] is None
    assert env["provenance"] == {"sources": [], "inputs": []}
    assert env["side_effect"] == "read"
    assert env["started_at"] == 10
    assert env["duration_ms"] == 5
    assert env["truncated"] == C.truncation().to_dict()


def test_envelope_is_canonical_and_deterministic():
    first = make_env(data={"z": [3, {"b": 2, "a": 1}], "a": {"d": 4, "c": 3}})
    text = S.envelope_json(first)
    assert text == C.canonical_json(first)
    assert "\n" not in text
    assert S.envelope_hash(first) == C.content_hash(first)
    assert len(S.envelope_hash(first)) == 64
    assert S.envelope_size(first) == len(text.encode("utf-8"))
    assert json.loads(text) == first


def test_envelope_hash_ignores_key_insertion_order():
    one = make_env(data={"a": 1, "b": 2})
    two = make_env(data={"b": 2, "a": 1})
    assert S.envelope_hash(one) == S.envelope_hash(two)


def test_envelope_hash_is_stable_across_hash_seeds():
    script = (
        "from ozagia.core import services as S\n"
        "env = S.ok('demo.preflight', {'z': [3, {'b': 2, 'a': 1}],"
        " 'a': 'héllo ☃'}, side_effect='read', started_at=1, duration_ms=2,"
        " provenance=S.provenance([S.Source('b'), S.Source('a')]))\n"
        "print(S.envelope_hash(env))\n"
    )
    hashes = []
    for seed in ("1", "2", "12345"):
        env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONHASHSEED": seed}
        result = subprocess.run([sys.executable, "-c", script], env=env,
                                capture_output=True, text=True, check=True)
        hashes.append(result.stdout.strip())
    assert len(set(hashes)) == 1


def test_envelope_round_trips_and_accepts_extensions():
    env = make_env()
    env["x-build"] = "ci-42"
    env = S.validate(env)
    assert env["x-build"] == "ci-42"
    assert S.validate(env) == env
    assert S.envelope_hash(env) != S.envelope_hash(make_env())


def test_truncation_is_the_contract_envelope():
    env = make_env(truncated=C.truncation(True, omitted=3, next_cursor="3",
                                          reason="limit"))
    assert env["truncated"] == {
        "truncated": True, "omitted": 3, "next_cursor": "3",
        "reason": "limit",
    }
    with pytest.raises(C.TruncationError):
        make_env(truncated={"truncated": True, "omitted": 1})


def test_provenance_is_sorted_and_bounded():
    prov = S.provenance(
        [S.Source("z.source", observed_at=2, freshness="stale"),
         S.Source("a.source", observed_at=1)],
        inputs=["hash-b", "hash-a", "hash-a"])
    assert [item["source"] for item in prov["sources"]] == [
        "a.source", "z.source"]
    assert prov["inputs"] == ["hash-a", "hash-b"]
    env = make_env(provenance=prov)
    assert env["provenance"] == prov
    with pytest.raises(C.SchemaError):
        S.Source("")
    with pytest.raises(C.VocabularyError):
        S.Source("a", freshness="fresh")
    with pytest.raises(C.BoundsError):
        S.provenance([S.Source(f"source-{index}")
                      for index in range(S.MAX_PROVENANCE_SOURCES + 1)])


@pytest.mark.parametrize("field,value", [
    ("sources", 0), ("sources", ""), ("inputs", 0), ("inputs", False),
])
def test_provenance_rejects_falsey_values_with_wrong_types(field, value):
    with pytest.raises(C.SchemaError):
        make_env(provenance={field: value})


# ---------------------------------------------------------------------------
# Typed errors and validation
# ---------------------------------------------------------------------------


def test_typed_error_mapping_and_codes():
    error = C.ServiceError("timeout", "the call exceeded its deadline")
    env = S.failed("demo", error)
    assert env["status"] == "error"
    assert env["error"]["code"] == "timeout"
    assert env["data"] is None
    with pytest.raises(C.ContractError) as caught:
        C.ServiceError("not_a_code", "x")
    assert caught.value.code == "contract.invalid_service_error"
    with pytest.raises(C.ContractError):
        C.ServiceError("timeout", "line one\nline two")


def test_status_and_error_must_agree():
    with pytest.raises(C.SchemaError):
        make_env(status="error")
    with pytest.raises(C.SchemaError):
        make_env(error=C.ServiceError("internal", "boom"))


def test_invalid_envelope_values_fail_explicitly():
    with pytest.raises(C.FloatNotAllowedError):
        make_env(data={"value": 1.5})
    with pytest.raises(C.SchemaError):
        make_env(service="Demo")
    with pytest.raises(C.VocabularyError):
        make_env(side_effect="magic")
    with pytest.raises(C.UnsupportedVersionError):
        make_env(version="2.0")
    with pytest.raises(C.UnknownFieldError):
        extended = make_env()
        extended["unknown"] = "x"
        S.validate(extended)
    with pytest.raises(C.BoundsError):
        make_env(duration_ms=-1)
    with pytest.raises(C.BoundsError):
        make_env(data={"x": "a" * (S.MAX_DATA_BYTES + 1)})
    with pytest.raises(C.SchemaError):
        make_env(started_at=True)


def test_validate_rejects_a_missing_field():
    env = make_env()
    env.pop("truncated")
    with pytest.raises(C.SchemaError):
        S.validate(env)


# ---------------------------------------------------------------------------
# Deadlines and calls
# ---------------------------------------------------------------------------


def test_deadline_expiry_and_remaining():
    tick = Tick(4, 4, 4, 10)
    deadline = S.Deadline(timeout_ms=10, clock=tick, started_ms=0)
    assert deadline.elapsed_ms() == 4
    assert deadline.remaining_ms() == 6
    assert deadline.expired() is False
    assert deadline.elapsed_ms() == 10
    assert deadline.expired() is True
    with pytest.raises(S.ServiceFailure) as caught:
        deadline.check()
    assert caught.value.error.code == "timeout"


def test_zero_timeout_does_not_call_the_operation():
    calls = []
    deadline = S.Deadline.after(0, clock=lambda: 0)
    env = S.call("demo", lambda: calls.append(1), deadline=deadline)
    assert calls == []
    assert env["status"] == "error"
    assert env["error"]["code"] == "timeout"
    assert env["duration_ms"] == 0


def test_call_runs_once_and_measures_duration():
    tick = Tick(100, 100, 175)
    calls = []
    deadline = S.Deadline(timeout_ms=1000, clock=tick, started_ms=100)
    env = S.call("demo", lambda: calls.append(1) or {"ok": True},
                 deadline=deadline, side_effect="read")
    assert calls == [1]
    assert env["status"] == "ok"
    assert env["data"] == {"ok": True}
    assert env["started_at"] == 100
    assert env["duration_ms"] == 75


def test_call_does_not_retry_a_failure():
    calls = []

    def operation():
        calls.append(1)
        S.fail("conflict", "already exists", retryable=False)

    deadline = S.Deadline(timeout_ms=1000, clock=lambda: 0, started_ms=0)
    env = S.call("demo", operation, deadline=deadline)
    assert calls == [1]
    assert env["error"]["code"] == "conflict"
    assert env["error"]["retryable"] is False


def test_call_does_not_leak_an_internal_exception_message():
    def operation():
        raise RuntimeError("sk-CANARY-0123456789abcdef")

    deadline = S.Deadline(timeout_ms=1000, clock=lambda: 0, started_ms=0)
    env = S.call("demo", operation, deadline=deadline)
    assert env["error"]["code"] == "internal"
    assert "CANARY" not in S.envelope_json(env)
    assert "RuntimeError" in env["error"]["message"]


def test_call_maps_a_contract_violation_to_a_typed_error():
    def operation():
        raise C.FloatNotAllowedError("value is not canonical")

    deadline = S.Deadline(timeout_ms=1000, clock=lambda: 0, started_ms=0)
    env = S.call("demo", operation, deadline=deadline)
    assert env["error"]["code"] == "invalid_value"
    assert env["error"]["details"] == {"contract_code": "contract.float_not_allowed"}


def test_non_canonical_result_is_a_typed_error():
    deadline = S.Deadline(timeout_ms=1000, clock=lambda: 0, started_ms=0)
    env = S.call("demo", lambda: {"x": float("nan")}, deadline=deadline)
    assert env["status"] == "error"
    assert env["error"]["code"] == "invalid_value"
    assert env["error"]["details"]["contract_code"] == (
        "contract.float_not_allowed")


def test_service_failure_message_is_redacted():
    deadline = S.Deadline(timeout_ms=1000, clock=lambda: 0, started_ms=0)
    env = S.call(
        "demo",
        lambda: S.fail("invalid_value", "bad token=sk-CANARY-0123456789abcdef"),
        deadline=deadline)
    assert env["error"]["code"] == "invalid_value"
    assert "CANARY" not in S.envelope_json(env)
    assert "[redacted]" in env["error"]["message"]


def test_completed_call_keeps_its_result_after_an_overrun():
    tick = Tick(0, 0, 500)
    deadline = S.Deadline(timeout_ms=100, clock=tick, started_ms=0)
    env = S.call("demo", lambda: {"done": True}, deadline=deadline)
    assert env["status"] == "ok"
    assert env["data"] == {"done": True}
    assert env["duration_ms"] == 500


def test_call_requires_a_deadline_and_callable():
    with pytest.raises(TypeError):
        S.call("demo", lambda: None, deadline=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        S.call("demo", None, deadline=S.Deadline.after(1, clock=lambda: 0))
