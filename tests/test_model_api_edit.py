"""Tests for the M4 structured edit and interaction layer.

The tests are offline and standard-library only.  A temporary git repository
is indexed once, a deterministic fake model returns a proposal or an
interaction request, and the host-side layer runs the real edit transaction
against that repository.  No live model, network call, daemon, target import,
install or test execution is involved.

The only repository writes are the explicit authorized edit and the fixture's
own git/state-directory setup.  The action ledger, test record and goal store
live under ``<root>/.ozagia/``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from ozagia.config import state_path
from ozagia.core import contracts as C
from ozagia.core import fingerprint as FP
from ozagia.core import goals as GOALS
from ozagia.core import task_preflight as T
from ozagia.core import working_set as W
from ozagia.core.memory import DevMemory, build_index
from ozagia.model_api import (
    EditAuthorization,
    HostPolicy,
    action_evidence,
    handle_edit,
    make_request,
    parse_edit_proposal,
    request_interact,
    request_map,
)
from ozagia.model_api import edit as EDIT

REPO_ID = "repo:acme/demo"
TEST_KEY = "test:tests/test_core.py::test_add"
ORIGINAL_CORE = '''"""Core helpers."""


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


def double(x: int) -> int:
    """Double x."""
    return add(x, x)
'''


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
         *args], check=True, capture_output=True)


def _build(root: Path) -> None:
    build_index(root, root / ".ozagia" / "graph.json", include_docs="none")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/core.py", ORIGINAL_CORE)
    _write(root, "tests/test_core.py", '''from pkg.core import add


def test_add():
    assert add(1, 2) == 3
''')
    _write(root, "README.md", "# Fixture\n")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    _build(root)
    return root


def _policy(root: Path, **kwargs) -> HostPolicy:
    values = {
        "root": root,
        "session": "s1",
        "model": "m1",
        "repo": REPO_ID,
        "allowed_models": ("m1", "m2"),
        "allowed_sessions": ("s1", "s2"),
    }
    values.update(kwargs)
    return HostPolicy(**values)


def _support(map_type: str, value: str = "supported") -> dict[str, str]:
    return {map_type: value}


def _base(root: Path, rel: str = "pkg/core.py") -> str:
    return hashlib.sha256((root / rel).read_bytes()).hexdigest()


def _snapshot_ids(root: Path) -> tuple[str, str]:
    response = request_map(
        make_request("graph", budgets={"max_items": 5}),
        _policy(root), configured_support=_support("graph"))
    assert response.get("ok") is True, response
    instance = response["representation"]["instance"]
    identities = response["source"]["source_identities"]
    return instance["snapshot"], identities["checkout"]["id"]


def _proposal(root: Path, *, action_id: str = "act-1",
              body: str = "return a + b\n", snapshot: str | None = None,
              checks: tuple[str, ...] = (TEST_KEY,),
              claims: tuple[dict[str, str], ...] = (),
              base: str | None = None,
              scope: dict | None = None) -> dict:
    return {
        "schema": {"namespace": EDIT.PROPOSAL_NAMESPACE, "version": "1.0"},
        "proposal_version": "1.0",
        "action_id": action_id,
        "node": {"kind": "symbol", "key": "sym:pkg/core.py::add"},
        "operation": "replace_body",
        "body": body,
        "base_sha256": base or _base(root),
        "snapshot": snapshot,
        "scope": scope if scope is not None else {
            "session": "s1", "model": "m1", "repo": REPO_ID},
        "checks": list(checks),
        "claims": list(claims),
    }


def _authorization() -> EditAuthorization:
    return EditAuthorization(granted=True, actor="test-host",
                             reason="reviewed preview")


def _add_goal(root: Path) -> dict:
    memory = DevMemory(root / ".ozagia" / "graph.json",
                       state_path(root, "journal.jsonl"), root=root)
    return GOALS.add_goal(root, memory, {
        "title": "Keep add working",
        "targets": ["sym:pkg/core.py::add"],
        "criteria": [{"kind": "test_passes", "args": {"key": TEST_KEY}}],
    })


def _record_passed_test(root: Path, *, ts: float = 2.0) -> None:
    tree = FP.tree_fingerprint(root)
    assert tree is not None
    W.save_test_record(root, {
        "tests": {},
        "last": None,
        "runs": [{
            "ts": ts,
            "selectors": ["tests/test_core.py::test_add"],
            "failed": [],
            "errors": [],
            "complete": True,
            "filtered": False,
            "source": "captured",
            "tree": tree,
        }],
    })


def _ready(name: str):
    def provider(_context):
        return T.Observation(
            True,
            (T.fact(f"preflight.{name}", {"ready": True},
                    source="test.fake", observed_at=3000,
                    freshness="current", evidence="observed"),),
            reason="deterministic fake provider",
        )
    return provider


def _providers() -> T.Providers:
    return T.Providers(
        graph=_ready("graph"),
        goal=T.goal_observation,
        capabilities=_ready("capabilities"),
        environment=_ready("environment"),
        resources=_ready("resources"),
        tests=_ready("tests"),
    )


def _preview(root: Path, proposal: dict, **kwargs) -> dict:
    return handle_edit(proposal, _policy(root), **kwargs)


def _apply(root: Path, proposal: dict, preview: dict, **kwargs) -> dict:
    return handle_edit(
        proposal, _policy(root), apply=True, authorization=_authorization(),
        preview_hash=preview["preview_hash"], **kwargs)


def test_fake_model_end_to_end_preview_apply_refresh_evidence_goal(
        repo: Path) -> None:
    snapshot, checkout = _snapshot_ids(repo)
    goal = _add_goal(repo)
    proposal = _proposal(
        repo, snapshot=snapshot, claims=({"text": "add still behaves",},))
    before = (repo / "pkg" / "core.py").read_bytes()

    preview = _preview(repo, proposal, expected_snapshot=snapshot,
                       expected_checkout=checkout)
    assert preview["ok"] is True, preview
    assert preview["kind"] == "edit"
    assert preview["mode"] == "preview"
    assert preview["status"] == "preview"
    assert preview["applied"] is False
    assert preview["backup"] is None
    assert len(preview["preview_hash"]) == 64
    assert preview["authority"]["granted"] is False
    assert (repo / "pkg" / "core.py").read_bytes() == before

    applied = _apply(repo, proposal, preview, expected_snapshot=snapshot,
                     expected_checkout=checkout, recorded_at=1000)
    assert applied["ok"] is True, applied
    assert applied["applied"] is True
    assert applied["replayed"] is False
    assert applied["backup"] is not None
    backup = Path(applied["backup"])
    assert backup.is_file()
    assert backup.resolve().is_relative_to((repo / ".ozagia").resolve())
    assert backup.read_bytes() == before
    assert applied["base_sha256"] == proposal["base_sha256"]
    assert applied["new_sha256"] != applied["base_sha256"]
    assert (repo / "pkg" / "core.py").read_bytes() != before
    assert applied["current_file"]["state"] == "current"
    assert applied["transaction"]["authorized"] is True
    assert applied["authority"]["granted"] is False

    # Refresh the existing graph snapshot, then record a captured test result
    # that selects the test.  No test is executed by the M4 layer.
    _build(repo)
    _record_passed_test(repo)

    evidence = action_evidence(
        applied, root=repo, goal=goal["id"], observed_at=3000)
    assert evidence["ok"] is not False
    assert evidence["action"]["status"] == "applied"
    assert evidence["checks"][0]["result"] == "passed"
    assert evidence["checks"][0]["after_action"] is True
    assert evidence["checks"][0]["evidence"] == "observed"
    assert evidence["claims"][0]["result"] == "declared"
    assert evidence["assessment"]["status"] == "passed"
    assert evidence["assessment"]["green"] is True
    assert evidence["evidence"]["test_keys"][0]["outcome"] == "passed"
    assert evidence["authority"]["granted"] is False

    report = T.build_preflight(
        repo, goal["id"], providers=_providers(), observed_at=3000,
        timeout_ms=5000)
    assert report["assessment"]["status"] == "green", report["assessment"]
    assert report["dimensions"]["goal"]["readiness"] is True


def test_invalid_python_body_is_refused_without_a_write(repo: Path) -> None:
    before = (repo / "pkg" / "core.py").read_bytes()
    proposal = _proposal(repo, body="return (((\n")
    response = _preview(repo, proposal)
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_value"
    assert response["reason"] == "parse_failed"
    assert (repo / "pkg" / "core.py").read_bytes() == before
    assert not (repo / ".ozagia" / "edits").exists()


def test_apply_can_attach_bounded_action_evidence(repo: Path) -> None:
    goal = _add_goal(repo)
    proposal = _proposal(repo, checks=(), claims=())
    preview = _preview(repo, proposal)
    applied = handle_edit(
        proposal, _policy(repo), apply=True, authorization=_authorization(),
        preview_hash=preview["preview_hash"], recorded_at=1000,
        evidence_goal=goal["id"], observed_at=3000)
    assert applied["ok"] is True, applied
    evidence = applied["evidence"]
    assert evidence["ok"] is True
    assert evidence["action"]["status"] == "applied"
    assert evidence["assessment"]["status"] == "not_assessed"
    assert evidence["assessment"]["green"] is False
    assert evidence["authority"]["granted"] is False


def test_stale_base_is_refused_without_a_write(repo: Path) -> None:
    before = (repo / "pkg" / "core.py").read_bytes()
    proposal = _proposal(repo, base="0" * 64)
    response = _preview(repo, proposal)
    assert response["ok"] is False
    assert response["error"]["code"] == "stale"
    assert response["reason"] == "changed_base"
    assert (repo / "pkg" / "core.py").read_bytes() == before
    assert not (repo / ".ozagia" / "edits").exists()


def test_declined_and_missing_authorization_do_not_write(repo: Path) -> None:
    proposal = _proposal(repo)
    preview = _preview(repo, proposal)
    assert preview["ok"] is True, preview
    before = (repo / "pkg" / "core.py").read_bytes()

    declined = handle_edit(
        proposal, _policy(repo), apply=True,
        authorization=EditAuthorization(granted=False, reason="not reviewed"),
        preview_hash=preview["preview_hash"])
    assert declined["ok"] is False
    assert declined["error"]["code"] == "forbidden"
    assert declined["reason"] == "write_declined"

    missing = handle_edit(
        proposal, _policy(repo), apply=True,
        preview_hash=preview["preview_hash"])
    assert missing["ok"] is False
    assert missing["reason"] == "write_declined"
    assert (repo / "pkg" / "core.py").read_bytes() == before

    applied = _apply(repo, proposal, preview)
    assert applied["ok"] is True, applied
    assert applied["applied"] is True


def test_partial_failure_and_retry_after_a_concurrent_change(repo: Path) -> None:
    proposal = _proposal(repo)
    preview = _preview(repo, proposal)
    assert preview["ok"] is True, preview
    original = (repo / "pkg" / "core.py").read_bytes()

    # A concurrent writer changes the file after the preview.
    (repo / "pkg" / "core.py").write_bytes(original + b"\n# concurrent\n")
    failed = _apply(repo, proposal, preview)
    assert failed["ok"] is False
    assert failed["error"]["code"] == "stale"
    assert failed["reason"] == "changed_base"
    assert not (repo / ".ozagia" / "model_api" / "actions").exists() or not any(
        (repo / ".ozagia" / "model_api" / "actions").glob("*.json"))

    # Restoring the exact previewed bytes makes the same preview retryable.
    (repo / "pkg" / "core.py").write_bytes(original)
    retried = _apply(repo, proposal, preview, recorded_at=1000)
    assert retried["ok"] is True, retried
    assert retried["applied"] is True
    assert retried["new_sha256"]


def test_duplicate_apply_is_idempotent_and_conflicting_ids_are_refused(
        repo: Path) -> None:
    proposal = _proposal(repo)
    preview = _preview(repo, proposal)
    assert preview["ok"] is True, preview
    applied = _apply(repo, proposal, preview, recorded_at=1000)
    assert applied["ok"] is True, applied
    after_first = (repo / "pkg" / "core.py").read_bytes()
    backups = sorted((repo / ".ozagia" / "edits" / "backups").glob("*"))
    assert len(backups) == 1

    again = _apply(repo, proposal, preview, recorded_at=1001)
    assert again["ok"] is True, again
    assert again["applied"] is False
    assert again["replayed"] is True
    assert again["status"] == "replayed"
    assert (repo / "pkg" / "core.py").read_bytes() == after_first
    assert sorted((repo / ".ozagia" / "edits" / "backups").glob("*")) == backups
    assert again["ledger"]["state"] == "replayed"

    different = copy.deepcopy(proposal)
    different["body"] = "return a - b\n"
    conflict = _apply(repo, different, preview)
    assert conflict["ok"] is False
    assert conflict["error"]["code"] == "conflict"
    assert conflict["reason"] == "action_conflict"
    assert (repo / "pkg" / "core.py").read_bytes() == after_first


def test_apply_requires_a_matching_preview(repo: Path) -> None:
    proposal = _proposal(repo)
    before = (repo / "pkg" / "core.py").read_bytes()
    no_preview = handle_edit(
        proposal, _policy(repo), apply=True,
        authorization=_authorization())
    assert no_preview["ok"] is False
    assert no_preview["reason"] == "preview_required"

    preview = _preview(repo, proposal)
    assert preview["ok"] is True
    mismatch = handle_edit(
        proposal, _policy(repo), apply=True,
        authorization=_authorization(), preview_hash="a" * 64)
    assert mismatch["ok"] is False
    assert mismatch["error"]["code"] == "stale"
    assert mismatch["reason"] == "preview_mismatch"
    assert (repo / "pkg" / "core.py").read_bytes() == before


def test_snapshot_and_scope_mismatches_are_refused(repo: Path) -> None:
    snapshot, checkout = _snapshot_ids(repo)
    proposal = _proposal(repo, snapshot=snapshot)
    missing = _preview(repo, proposal)
    assert missing["ok"] is False
    assert missing["reason"] == "scope_unverifiable"

    wrong = _preview(repo, proposal, expected_snapshot="snapshot:acme/demo@"
                     + "0" * 12 + "@" + "0" * 12,
                     expected_checkout=checkout)
    assert wrong["ok"] is False
    assert wrong["reason"] == "snapshot_mismatch"

    wrong_checkout = _preview(
        repo, _proposal(repo, snapshot=snapshot, scope={
            "session": "s1", "model": "m1", "repo": REPO_ID,
            "checkout": checkout}),
        expected_snapshot=snapshot,
        expected_checkout="checkout:acme/demo@" + "0" * 12)
    assert wrong_checkout["ok"] is False
    assert wrong_checkout["reason"] == "checkout_mismatch"

    wrong_root = _proposal(repo, scope={"root": str(repo.parent)})
    root_response = _preview(repo, wrong_root)
    assert root_response["ok"] is False
    assert root_response["reason"] == "cross_scope"


def test_unsupported_proposal_shapes_are_refused(repo: Path) -> None:
    base = _proposal(repo)

    def mutate(**changes) -> dict:
        value = copy.deepcopy(base)
        value.update(changes)
        return value

    cases = [
        (mutate(schema={"namespace": EDIT.PROPOSAL_NAMESPACE,
                        "version": "2.0"}),
         "unsupported_version", "unsupported_version"),
        (mutate(proposal_version="1.1"),
         "unsupported_version", "unsupported_version"),
        (mutate(operation="replace_span"),
         "unsupported", "unsupported_operation"),
        (mutate(node={"kind": "file", "key": "file:pkg/core.py"}),
         "unsupported", "unsupported_operation"),
        (mutate(node={"kind": "symbol", "key": "sym:../outside.py::add"}),
         "forbidden", "path_escape"),
        (mutate(node={"kind": "symbol", "key": "sym:/tmp/outside.py::add"}),
         "forbidden", "path_escape"),
        (mutate(node={"kind": "symbol", "key": "sym:README.md::thing"}),
         "unsupported", "unsupported_operation"),
        (mutate(body="x" * (EDIT.MAX_BODY_BYTES + 1)),
         "budget_exceeded", "proposal_over_budget"),
        (mutate(checks=[TEST_KEY] * (EDIT.MAX_CHECKS + 1)),
         "budget_exceeded", "proposal_over_budget"),
        (mutate(claims=[{"text": "x" * (EDIT.MAX_CLAIM_TEXT + 1)}]),
         "invalid_request", "malformed_proposal"),
        (mutate(base_sha256="not-a-hash"),
         "invalid_request", "malformed_proposal"),
        (mutate(snapshot="repo:acme/demo"),
         "invalid_request", "malformed_proposal"),
        (mutate(command="rm -rf /"),
         "unknown_field", "malformed_proposal"),
        (mutate(scope={"root": "relative/path"}),
         "invalid_request", "malformed_proposal"),
        (mutate(scope={"paths": ["pkg"]}),
         "unknown_field", "malformed_proposal"),
    ]
    for proposal, code, reason in cases:
        response = _preview(repo, proposal)
        assert response["ok"] is False, (reason, response)
        assert response["error"]["code"] == code, (reason, response)
        assert response["reason"] == reason, (reason, response)


def test_oversized_depth_and_duplicate_json_are_refused(repo: Path) -> None:
    proposal = _proposal(repo)
    payload = json.dumps(proposal)
    padded = payload[:-1] + ', "x-pad": "' + "a" * EDIT.MAX_PROPOSAL_BYTES + '"}'
    response = handle_edit(padded, _policy(repo))
    assert response["ok"] is False
    assert response["reason"] == "proposal_over_budget"

    deep: object = "leaf"
    for _ in range(EDIT.MAX_PROPOSAL_DEPTH + 2):
        deep = {"x-child": deep}
    nested = copy.deepcopy(proposal)
    nested["x-deep"] = deep
    response = _preview(repo, nested)
    assert response["ok"] is False
    assert response["reason"] == "proposal_over_budget"

    duplicate = json.dumps(proposal)[:-1] + ', "action_id": "other"}'
    response = handle_edit(duplicate, _policy(repo))
    assert response["ok"] is False
    assert response["reason"] == "malformed_proposal"


def test_parse_proposal_is_deterministic_and_writes_nothing(repo: Path) -> None:
    proposal = _proposal(repo)
    first = parse_edit_proposal(proposal)
    second = parse_edit_proposal(json.dumps(proposal, sort_keys=True))
    assert first.proposal_hash == second.proposal_hash
    assert first.to_dict() == second.to_dict()
    assert first.operation == "replace_body"
    assert first.file == "pkg/core.py"
    assert first.qualname == "add"
    assert first.body_sha256 == hashlib.sha256(
        first.body.encode("utf-8")).hexdigest()
    assert not (repo / ".ozagia" / "model_api").exists()


def test_a_model_body_is_never_executed(repo: Path) -> None:
    marker = repo / "EXECUTED"
    body = (f'open({str(marker)!r}, "w").write("owned")\n'
            "return a + b\n")
    proposal = _proposal(repo, body=body, checks=())
    preview = _preview(repo, proposal)
    assert preview["ok"] is True, preview
    applied = _apply(repo, proposal, preview, recorded_at=1000)
    assert applied["ok"] is True, applied
    assert not marker.exists()
    assert "open(" in (repo / "pkg" / "core.py").read_text(encoding="utf-8")


def test_claims_only_and_unavailable_checks_are_not_verdicts(repo: Path) -> None:
    goal = _add_goal(repo)
    claims = ({"text": "the add test passes"},)
    proposal = _proposal(repo, checks=(), claims=claims)
    preview = _preview(repo, proposal)
    assert preview["ok"] is True
    applied = _apply(repo, proposal, preview, recorded_at=1000)
    assert applied["ok"] is True, applied

    claimed = action_evidence(applied, root=repo, goal=goal["id"],
                              observed_at=3000)
    assert claimed["assessment"]["status"] == "declared"
    assert claimed["assessment"]["green"] is False
    assert claimed["checks"] == []
    assert claimed["claims"][0]["result"] == "declared"
    assert claimed["claims"][0]["evidence"] == "declared"

    checked = _proposal(
        repo, action_id="act-check", body="return a + b + 0\n",
        checks=(TEST_KEY,), claims=claims)
    preview = _preview(repo, checked)
    applied = _apply(repo, checked, preview, recorded_at=1000)
    missing = action_evidence(applied, root=repo, goal=goal["id"],
                              observed_at=3000)
    assert missing["checks"][0]["result"] == "unknown"
    assert missing["assessment"]["status"] == "unknown"
    assert missing["assessment"]["green"] is False


def test_a_check_recorded_before_the_action_is_not_upgraded(repo: Path) -> None:
    goal = _add_goal(repo)
    _record_passed_test(repo, ts=0.5)  # 500 ms, before the action at 1000 ms
    proposal = _proposal(repo, checks=(TEST_KEY,))
    preview = _preview(repo, proposal)
    applied = _apply(repo, proposal, preview, recorded_at=1000)
    evidence = action_evidence(applied, root=repo, goal=goal["id"],
                               observed_at=3000)
    assert evidence["checks"][0]["result"] == "unknown"
    assert evidence["checks"][0]["freshness"] == "stale"
    assert evidence["checks"][0]["after_action"] is False
    assert evidence["assessment"]["status"] == "unknown"


def test_replayed_action_can_still_join_post_action_evidence(repo: Path) -> None:
    goal = _add_goal(repo)
    proposal = _proposal(repo, checks=(TEST_KEY,))
    preview = _preview(repo, proposal)
    applied = _apply(repo, proposal, preview, recorded_at=1000)
    assert applied["ok"] is True, applied
    again = _apply(repo, proposal, preview, recorded_at=1001)
    assert again["ok"] is True and again["replayed"] is True
    _build(repo)
    _record_passed_test(repo)
    evidence = action_evidence(again, root=repo, goal=goal["id"],
                               observed_at=3000)
    assert evidence["action"]["replayed"] is True
    assert evidence["checks"][0]["result"] == "passed"
    assert evidence["assessment"]["status"] == "passed"


def test_ledger_write_failure_does_not_hide_an_applied_edit(
        repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args, **_kwargs):
        raise RuntimeError("simulated ledger failure")

    monkeypatch.setattr(EDIT, "_ledger_write", broken)
    proposal = _proposal(repo, body="return a + b + 0\n")
    preview = _preview(repo, proposal)
    applied = _apply(repo, proposal, preview, recorded_at=1000)
    assert applied["ok"] is True, applied
    assert applied["applied"] is True
    assert applied["ledger"]["state"] == "failed"
    assert any("ledger" in warning for warning in applied["warnings"])
    text = (repo / "pkg" / "core.py").read_text(encoding="utf-8")
    assert "    return a + b + 0\n" in text


def test_corrupt_action_ledger_fails_closed(repo: Path) -> None:
    proposal = _proposal(repo)
    preview = _preview(repo, proposal)
    applied = _apply(repo, proposal, preview, recorded_at=1000)
    assert applied["ok"] is True, applied
    entry_path = next(
        (repo / ".ozagia" / "model_api" / "actions").glob("*.json"))
    entry = json.loads(entry_path.read_text(encoding="utf-8"))
    entry["schema"]["version"] = "2.0"
    entry_path.write_text(json.dumps(entry), encoding="utf-8")

    replay = _apply(repo, proposal, preview, recorded_at=1001)
    assert replay["ok"] is False
    assert replay["error"]["code"] == "invalid_state"
    assert replay["reason"] == "ledger_unavailable"


def test_action_ledger_stays_under_the_state_directory(repo: Path) -> None:
    proposal = _proposal(repo)
    preview = _preview(repo, proposal)
    applied = _apply(repo, proposal, preview, recorded_at=1000)
    assert applied["ok"] is True, applied
    entries = list((repo / ".ozagia" / "model_api" / "actions").glob("*.json"))
    assert len(entries) == 1
    entry = json.loads(entries[0].read_text(encoding="utf-8"))
    assert entry["action"]["action_id"] == "act-1"
    assert entry["action"]["proposal_hash"] == applied["proposal_hash"]
    assert entry["action"]["preview_hash"] == preview["preview_hash"]
    assert entry["action"]["status"] == "applied"
    assert "body" not in entry["action"]
    assert "diff_view" not in entry["action"]


def test_apply_refuses_a_state_directory_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".ozagia").symlink_to(outside, target_is_directory=True)
    policy = HostPolicy(root=root, session="s1", model="m1")
    proposal = {
        "schema": {"namespace": EDIT.PROPOSAL_NAMESPACE, "version": "1.0"},
        "proposal_version": "1.0",
        "action_id": "act-symlink",
        "node": {"kind": "symbol", "key": "sym:a.py::f"},
        "operation": "replace_body",
        "body": "return 2\n",
        "base_sha256": _base(root, "a.py"),
        "scope": {},
    }
    preview = handle_edit(proposal, policy)
    assert preview["ok"] is True, preview
    applied = handle_edit(
        proposal, policy, apply=True, authorization=_authorization(),
        preview_hash=preview["preview_hash"])
    assert applied["ok"] is False
    assert applied["error"]["code"] == "forbidden"
    assert applied["reason"] == "path_escape"
    assert not (outside / "model_api").exists()
    assert (root / "a.py").read_text(encoding="utf-8") == (
        "def f():\n    return 1\n")

    # The state dir itself is real now, but the backup directory is a symlink.
    (root / ".ozagia").unlink()
    (root / ".ozagia").mkdir()
    (root / ".ozagia" / "edits").symlink_to(outside, target_is_directory=True)
    preview = handle_edit(proposal, policy)
    assert preview["ok"] is True, preview
    applied = handle_edit(
        proposal, policy, apply=True, authorization=_authorization(),
        preview_hash=preview["preview_hash"])
    assert applied["ok"] is False
    assert applied["reason"] == "path_escape"
    assert not (outside / "backups").exists()
    assert (root / "a.py").read_text(encoding="utf-8") == (
        "def f():\n    return 1\n")


def _interact(root: Path, *, request_id: str = "ctx-1", **overrides) -> dict:
    request = {
        "schema": {"namespace": "ozagia.model_api.interact", "version": "1.0"},
        "request_version": "1.0",
        "request_id": request_id,
        "reason": "need the callers of add",
        "map": "graph",
        "scope": {"session": "s1", "model": "m1", "repo": REPO_ID},
        "target": {"key": "sym:pkg/core.py::add"},
        "budgets": {"max_items": 5, "max_depth": 2},
        "format": "structured",
    }
    request.update(overrides)
    return request


def test_interaction_routes_one_bounded_read_only_map(repo: Path) -> None:
    response = request_interact(
        _interact(repo), _policy(repo),
        configured_support=_support("graph"))
    assert response["ok"] is True, response
    assert response["kind"] == "interact"
    assert response["map"] == "graph"
    assert response["interaction"]["request_id"] == "ctx-1"
    assert response["interaction"]["expanded"] is True
    assert response["authority"]["granted"] is False
    assert response["interaction"]["authority"]["granted"] is False
    assert response["facts"]
    items = response["representation"]["instance"]["data"]["items"]
    assert any(item.get("key") == "sym:pkg/core.py::add" for item in items)

    direct = request_map(
        make_request("graph", target={"key": "sym:pkg/core.py::add"},
                     budgets={"max_items": 5, "max_depth": 2}),
        _policy(repo), configured_support=_support("graph"))
    assert direct["ok"] is True, direct
    assert response["facts"] == direct["facts"]
    assert response["omissions"] == direct["omissions"]


@pytest.mark.parametrize("overrides, code, reason", [
    ({"scope": {"session": "s3"}}, "forbidden", "session_not_allowed"),
    ({"scope": {"model": "m3"}}, "forbidden", "model_not_allowed"),
    ({"scope": {"root": "/"}}, "forbidden", "cross_scope"),
    ({"map": "secrets"}, "unsupported", "unsupported_map"),
    ({"budgets": {"max_items": 500}}, "budget_exceeded",
     "budget_over_hard_bound"),
    ({"command": "rm -rf /"}, "unknown_field", "malformed_request"),
    ({"maps": ["graph", "work"]}, "unknown_field", "malformed_request"),
])
def test_interaction_cannot_widen_scope_or_run_anything(
        repo: Path, overrides: dict, code: str, reason: str) -> None:
    response = request_interact(
        _interact(repo, **overrides), _policy(repo),
        configured_support=_support("graph"))
    assert response["ok"] is False, response
    assert response["error"]["code"] == code
    assert response["reason"] == reason
    assert response["authority"]["granted"] is False


def test_interaction_refuses_an_out_of_policy_path_and_budget(repo: Path) -> None:
    policy = _policy(repo, allowed_paths=("pkg",), max_items=25)
    outside = request_interact(
        _interact(repo, scope={"session": "s1", "model": "m1",
                               "repo": REPO_ID, "paths": ["tests"]}),
        policy, configured_support=_support("graph"))
    assert outside["ok"] is False
    assert outside["reason"] == "cross_scope"

    over = request_interact(
        _interact(repo, budgets={"max_items": 50}), policy,
        configured_support=_support("graph"))
    assert over["ok"] is False
    assert over["error"]["code"] == "budget_exceeded"
    assert over["reason"] == "budget_over_policy"


def test_interaction_request_parse_is_bounded_and_deterministic(
        repo: Path) -> None:
    from ozagia.model_api.interact import parse_interact_request

    request = _interact(repo)
    first = parse_interact_request(request)
    second = parse_interact_request(json.dumps(request, sort_keys=True))
    assert first.to_dict() == second.to_dict()
    assert first.map_request["trace_id"] == "ctx-1"
    assert first.map == "graph"

    oversized = json.dumps({**request, "reason": "x" * 5000})
    response = request_interact(oversized, _policy(repo))
    assert response["ok"] is False
    assert response["reason"] == "malformed_request"

    duplicate = json.dumps(request)[:-1] + ', "request_id": "other"}'
    response = request_interact(duplicate, _policy(repo))
    assert response["ok"] is False
    assert response["reason"] == "malformed_request"


def test_edit_scope_cannot_widen_host_policy(repo: Path) -> None:
    proposal = _proposal(repo)
    no_repo = handle_edit(
        {**proposal, "scope": {"repo": REPO_ID}},
        HostPolicy(root=repo, session="s1", model="m1"))
    assert no_repo["ok"] is False
    assert no_repo["reason"] == "scope_unverifiable"

    session = _preview(
        repo, _proposal(repo, scope={"session": "s3", "model": "m1",
                                     "repo": REPO_ID}))
    assert session["ok"] is False
    assert session["reason"] == "session_not_allowed"

    model = _preview(
        repo, _proposal(repo, scope={"session": "s1", "model": "m3",
                                     "repo": REPO_ID}))
    assert model["ok"] is False
    assert model["reason"] == "model_not_allowed"

    other_repo = _preview(
        repo, _proposal(repo, scope={"session": "s1", "model": "m1",
                                     "repo": "repo:other/project"}))
    assert other_repo["ok"] is False
    assert other_repo["reason"] == "cross_scope"

    path_policy = _policy(repo, allowed_paths=("tests",))
    path = handle_edit(proposal, path_policy)
    assert path["ok"] is False
    assert path["reason"] == "cross_scope"


def test_edit_response_is_canonical_json(repo: Path) -> None:
    proposal = _proposal(repo)
    preview = _preview(repo, proposal)
    assert preview["ok"] is True
    C.canonical_json(preview)
    applied = _apply(repo, proposal, preview, recorded_at=1000)
    assert applied["ok"] is True
    C.canonical_json(applied)
    assert applied["proposal_hash"] == preview["proposal_hash"]
    assert applied["preview_hash"] == preview["preview_hash"]
