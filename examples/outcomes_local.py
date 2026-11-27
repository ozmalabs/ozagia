#!/usr/bin/env python3
"""Run the Stage 1 local outcome flow against a disposable Git repository.

The example creates its own temporary repository, commits a tracked
``app/base.py``, submits an intent, applies a one-line docstring edit through
the real edit transaction, records deterministic validation evidence, creates
and approves one required gate, finalizes the outcome, then reloads the store
and prints the final ``ozagia.outcomes.view.v1`` document.

It uses only temp paths and the local ``git`` binary.  It never touches the
caller's checkout, a model, the network or a live service.  Any failed
expectation exits non-zero.

    python examples/outcomes_local.py
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ozagia.core.outcomes import (
    EffectAdapter,
    EffectRequest,
    OutcomeStore,
    approve_gate,
    create_gate,
    finalize_outcome,
    validate_effect,
)
from ozagia.core.outcomes.view import view

TARGET = "app/base.py"
ORIGINAL = "def base():\n    return 1\n"
EDITED = (
    '"""Base module providing the base() helper function."""\n'
    "\n"
    "\n"
    "def base():\n"
    "    return 1\n"
)
CRITERIA = [
    "app/base.py starts with a module docstring",
    "base() still returns 1",
]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_repo(parent: Path) -> Path:
    root = parent / "repo"
    (root / "app").mkdir(parents=True)
    (root / TARGET).write_text(ORIGINAL, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "example@example.invalid")
    _git(root, "config", "user.name", "Outcome Example")
    _git(root, "add", TARGET)
    _git(root, "commit", "-qm", "initial")
    return root


def run(parent: Path) -> dict[str, Any]:
    root = _make_repo(parent)
    store = OutcomeStore(root)
    intent = store.submit_intent(
        "Add a one-line module docstring to app/base.py",
        criteria=CRITERIA,
        context_refs=["workspace:example"],
        project_ref="project:example",
        interpretation="one-file documentation change",
        intent_id="intent-example",
    )
    work = store.add_work(intent["intent_id"], "Add the docstring")
    attempt = store.start_attempt(intent["intent_id"], work["work_id"])
    expected_base = _sha(root / TARGET)
    request = EffectRequest(
        intent_id=intent["intent_id"],
        attempt_id=attempt["attempt_id"],
        target=TARGET,
        expected_base=expected_base,
        new_text=EDITED,
    )
    effect = EffectAdapter(store).apply(request)
    assert effect.state == "applied", effect.state
    assert effect.applied is True
    assert (root / TARGET).read_text(encoding="utf-8") == EDITED
    assert effect.backup_ref is not None
    assert Path(effect.backup_ref).is_relative_to(root)

    validation = validate_effect(store, effect)
    assert validation.passed is True, validation.to_dict()
    assert validation.current is True, validation.to_dict()
    assert validation.artifact_digest == effect.new_hash

    gate = create_gate(
        store, intent["intent_id"], "gate-example",
        effect=effect, validation=validation)
    assert gate["status"] == "pending", gate
    assert store.inspect(intent["intent_id"])["intent"]["state"] == (
        "waiting_decision")

    # Restart while the gate is pending: the effect must not be applied twice.
    journal = store.journal.read_bytes()
    pending = OutcomeStore(root)
    pending_view = view(intent["intent_id"], pending)
    assert pending_view["pending_decision"]["gate_id"] == "gate-example"
    replay = EffectAdapter(pending).apply(request)
    assert replay.duplicate is True, replay.to_dict()
    assert pending.journal.read_bytes() == journal

    pending.finish_attempt(
        intent["intent_id"], attempt["attempt_id"], "succeeded",
        lease_generation=effect.lease_generation)
    decision = approve_gate(
        pending, intent["intent_id"], "gate-example",
        detail="operator approved the exact diff")
    assert decision["decision"] == "approved", decision

    after_decision = OutcomeStore(root)
    repeated = approve_gate(
        after_decision, intent["intent_id"], "gate-example")
    assert repeated["duplicate"] is True, repeated

    outcome = finalize_outcome(
        after_decision, intent["intent_id"], gate_id="gate-example",
        decision_ref=decision["decision_ref"],
        criteria_satisfied=CRITERIA,
        summary="Documented base() without changing behaviour")
    assert outcome["state"] == "satisfied", outcome

    # A fresh store simulates a process restart; the view is derived only
    # from the durable journal and must remain satisfied.
    reloaded = OutcomeStore(root)
    result = view(intent["intent_id"], reloaded)
    assert result["schema"] == "ozagia.outcomes.view.v1"
    assert result["status"] == "satisfied", result["status"]
    assert result["outcome"] is not None
    assert result["outcome"]["record_id"] == outcome["outcome_ref"]
    assert result["pending_decision"] is None
    assert result["blocker"] is None
    assert result["criteria_summary"] == {
        "total": 2, "satisfied": 2, "pending": 0}
    return result


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="ozagia-outcomes-") as tmp:
            result = run(Path(tmp))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"outcomes_local: failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
