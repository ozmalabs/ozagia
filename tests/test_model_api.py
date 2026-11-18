"""Tests for the M3 read-only model-facing map API.

These tests are offline and standard-library only: a temporary git repository
is indexed once, then every request is served from the existing snapshot.  No
live model, network call, daemon, target import, install or test execution is
involved.  The only writes are the graph fixture, git metadata and the M3
cache metadata under ``<root>/.ozagia/model_api/cache``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ozagia.core import contracts as C
from ozagia.core import evidence as E
from ozagia.core.memory import DevMemory, build_index
from ozagia.integrations.mcp.server import DevMemoryServer
from ozagia.model_api import (
    CacheStore,
    HostPolicy,
    ModelAPIError,
    cache_key,
    make_request,
    map_schema_fingerprint,
    request_map,
    validate_map,
)
from ozagia.model_api.schema import encode_cursor, map_schema

REPO_ID = "repo:acme/demo"
SECRET = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
         *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/core.py", '''"""Core helpers."""

API_KEY = "not-used"


def add(a: int, b: int) -> int:
    """Add two numbers. sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"""
    return a + b


def double(x: int) -> int:
    """Double x."""
    return add(x, x)
''')
    _write(root, "pkg/user.py", '''"""User helpers."""

from pkg.core import add


def triple(x: int) -> int:
    return add(add(x, x), x)
''')
    _write(root, "tests/test_core.py", '''from pkg.core import add


def test_add():
    assert add(1, 2) == 3
''')
    for index in range(12):
        _write(root, f"pkg/mod_{index}.py",
               f"def value_{index}() -> int:\n    return {index}\n")
    _write(root, "README.md", "# Fixture\n\nSee `pkg/core.py`.\n")
    _write(root, "not_an_outline.json", '{"token": "plain"}\n')
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    build_index(root, root / ".ozagia" / "graph.json", include_docs="none")
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


def _graph(root: Path, *, configured: dict[str, str] | None = None,
           **kwargs):
    request = make_request("graph", **kwargs)
    return request_map(request, _policy(root),
                       configured_support=configured or _support("graph"))


def _instance(response):
    assert response.get("ok") is True, response
    return response["representation"]["instance"]


def test_canonical_schema_and_instance_hash_round_trip(repo: Path) -> None:
    response = _graph(repo, budgets={"max_items": 5})
    instance = _instance(response)
    assert instance["schema"] == {
        "namespace": "ozagia.model_api.graph", "version": "1.0"}
    assert instance["data"]["map"] == "graph"
    assert instance["provenance"]["schema_fingerprint"] == (
        map_schema_fingerprint("graph"))
    normalized, digest = validate_map(instance)
    assert normalized == instance
    assert digest == response["map_hash"]
    assert map_schema_fingerprint("graph") == (
        C.schema_fingerprint(map_schema("graph")))

    transport = dict(instance)
    transport["created_at"] = 1
    transport["cursor"] = encode_cursor(0, "a" * 12)
    transport["trace_id"] = "trace-one"
    assert C.instance_hash(transport) == digest

    changed = json.loads(json.dumps(instance))
    changed["data"]["items"][0]["summary"] = "changed"
    assert C.instance_hash(changed) != digest
    with pytest.raises(ModelAPIError) as exc:
        validate_map({**instance, "schema": {
            "namespace": "ozagia.model_api.work", "version": "1.0"}})
    assert exc.value.reason == "schema_mismatch"


def test_plain_and_structured_agree_on_facts_and_omissions(repo: Path) -> None:
    structured = _graph(
        repo, format="structured", budgets={"max_items": 2})
    plain = _graph(repo, format="plain", budgets={"max_items": 2})
    assert structured["ok"] and plain["ok"]
    assert plain["format"] == "plain"
    assert structured["format"] == "structured"
    assert plain["facts"] == structured["facts"]
    assert plain["omissions"] == structured["omissions"]
    assert plain["omissions"]["truncated"] is True
    assert plain["omissions"]["next_cursor"]
    assert plain["map_hash"] == structured["map_hash"]
    text = plain["representation"]["text"]
    assert "ozagia model map (plain v1)" in text
    for fact in plain["facts"]:
        if fact["kind"] != "source":
            assert fact["subject"] in text
    assert "omissions:" in text


def test_graph_map_has_source_identities_and_current_freshness(repo: Path) -> None:
    response = _graph(repo, budgets={"max_items": 8})
    instance = _instance(response)
    data = instance["data"]
    assert data["state"] == "ok"
    assert data["freshness"] == "current"
    assert data["trust"]["authority"] == "none"
    source = response["source"]
    assert source["signature"] and len(source["signature"]) == 64
    identities = data["source_identities"]
    assert identities["repo"]["id"] == REPO_ID
    assert identities["checkout"]["id"].startswith("checkout:acme/demo@")
    assert identities["snapshot"]["id"] == instance["snapshot"]
    assert instance["snapshot"].startswith("snapshot:acme/demo@")
    assert identities["sources"][0]["kind"] == "snapshot"
    keys = {item.get("key") for item in data["items"]}
    assert "file:pkg/core.py" in keys
    assert all(item["trust"] == "untrusted-data" for item in data["items"])
    C.canonical_json(instance)  # no floats or non-canonical values


def test_outline_contains_signatures_not_bodies(repo: Path) -> None:
    response = request_map(
        make_request("outline", target={"target": "pkg/core.py"}),
        _policy(repo), configured_support=_support("outline"))
    instance = _instance(response)
    item = instance["data"]["items"][0]
    assert item["state"] == "present"
    assert "def add" in item["text"]
    assert "return a + b" not in item["text"]
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in item["text"]
    assert instance["data"]["freshness"] == "current"

    symbol = request_map(
        make_request("outline", target={"target": "add"}),
        _policy(repo), configured_support=_support("outline"))
    symbol_text = _instance(symbol)["data"]["items"][0]["text"]
    assert "[redacted]" in symbol_text
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in symbol_text

    keyed = request_map(
        make_request("outline", target={"target": "file:pkg/core.py"}),
        _policy(repo), configured_support=_support("outline"))
    keyed_item = _instance(keyed)["data"]["items"][0]
    assert keyed_item["state"] == "present"
    assert "def add" in keyed_item["text"]


def test_outline_escape_and_unsupported_targets_fail_closed(repo: Path) -> None:
    escaped_request = make_request(
        "outline", target={"target": "pkg/core.py"})
    escaped_request["target"]["targets"] = ["../outside.py"]
    escaped = request_map(
        escaped_request, _policy(repo),
        configured_support=_support("outline"))
    assert escaped["ok"] is False
    assert escaped["error"]["code"] == "forbidden"
    assert escaped["reason"] == "cross_scope"

    unsupported = request_map(
        make_request("outline", target={"target": "not_an_outline.json"}),
        _policy(repo), configured_support=_support("outline"))
    assert unsupported["ok"] is False
    assert unsupported["error"]["code"] == "unsupported"
    assert unsupported["reason"] == "outline_target_unsupported"

    keyed_value = request_map(
        make_request("outline",
                     target={"target": "file:not_an_outline.json"}),
        _policy(repo), configured_support=_support("outline"))
    assert keyed_value["ok"] is False
    assert keyed_value["reason"] == "outline_target_unsupported"


def test_capabilities_map_is_explicitly_not_exhaustive(repo: Path) -> None:
    response = request_map(
        make_request("capabilities", budgets={"max_items": 3}),
        _policy(repo), configured_support=_support("capabilities"))
    instance = _instance(response)
    data = instance["data"]
    assert len(data["items"]) == 3
    assert data["counts"]["total"] > 3
    assert data["coverage"]["exhaustive"] is False
    assert data["coverage"]["statement"] == (
        "coverage.exhaustive is always false; unknown coverage is not treated "
        "as absence")
    assert [row["kind"] for row in data["audit"]["directions"]] == [
        "declared_surface_not_observed", "observed_surface_undeclared"]
    assert data["audit"]["limits"]
    assert data["not_assessed"]
    assert data["audit"]["state"] in {"current", "partial", "unknown"}
    assert data["source_identities"]["catalogue"]["hash"]
    assert data["source_identities"]["observations"]["hash"]
    assert response["omissions"]["truncated"] is True

    focused = request_map(
        make_request("capabilities", target={"capability": "cap:graph.context"}),
        _policy(repo), configured_support=_support("capabilities"))
    focused_instance = _instance(focused)
    assert [item["id"] for item in focused_instance["data"]["items"]] == [
        "cap:graph.context"]
    assert focused_instance["data"]["relations"]


def test_work_map_is_redacted_and_keeps_absent_test_record(repo: Path) -> None:
    def goal(_root: Path, local: str) -> dict:
        return {
            "state": "present",
            "observed_at": 1,
            "goal": {
                "id": local,
                "title": f"secret title {SECRET}",
                "why": f"secret why {SECRET}",
                "status": "open",
                "criteria": [{
                    "id": "c1",
                    "kind": "file_exists",
                    "args": {"path": "pkg/core.py"},
                    "last": {"status": "pass", "at": 1,
                             "reason": f"secret reason {SECRET}"},
                }],
            },
        }

    sources = E.Sources(
        goal=goal,
        runs=lambda _root, _ids: [],
        test_record=lambda _root: {"state": "absent", "observed_at": 1},
        interpreters=lambda _root: [])
    response = request_map(
        make_request("work", target={"goal": "g1"}),
        _policy(repo), configured_support=_support("work"),
        evidence_sources=sources)
    instance = _instance(response)
    text = C.canonical_json(response)
    assert SECRET not in text
    assert "secret title" not in text and "secret why" not in text
    kinds = {item["kind"]: item for item in instance["data"]["items"]}
    assert kinds["goal"]["state"] == "present"
    assert kinds["criterion"]["result"] == "passed"
    assert kinds["test_record"]["state"] == "absent"
    assert kinds["test_record"]["freshness"] == "absent"
    assert instance["data"]["coverage"]["test_record"] == "absent"
    assert instance["data"]["deferred"]
    not_assessed = instance["data"]["not_assessed"]
    assert (
        "task output is not collected; only manifests, hashes and redacted "
        "metadata" in not_assessed)
    assert "the interpreter link for a recorded test run is unknown" in (
        not_assessed)
    assert len(not_assessed) <= 16
    assert not_assessed == list(dict.fromkeys(not_assessed))

    again = _instance(request_map(
        make_request("work", target={"goal": "g1"}),
        _policy(repo), configured_support=_support("work"),
        evidence_sources=sources))
    assert again["data"]["not_assessed"] == not_assessed
    assert response["map_hash"] == request_map(
        make_request("work", target={"goal": "g1"}),
        _policy(repo), configured_support=_support("work"),
        evidence_sources=sources)["map_hash"]


def test_absent_work_goal_is_explicit_and_strict_fails(repo: Path) -> None:
    sources = E.Sources(
        goal=lambda _root, _local: {
            "state": "absent", "observed_at": 1, "reason": "no goal"},
        runs=lambda _root, _ids: [],
        test_record=lambda _root: {"state": "absent", "observed_at": 1},
        interpreters=lambda _root: [])
    request = make_request("work", target={"goal": "g404"})
    response = request_map(request, _policy(repo),
                           configured_support=_support("work"),
                           evidence_sources=sources)
    instance = _instance(response)
    assert instance["data"]["state"] == "absent"
    assert instance["data"]["freshness"] == "absent"
    assert instance["omissions"]["truncated"] is True
    assert instance["omissions"]["reason"] == "unavailable"
    assert instance["data"]["notices"]
    assert "test_record" in {
        item["kind"] for item in instance["data"]["items"]}

    strict = make_request("work", target={"goal": "g404"}, strict=True)
    failure = request_map(strict, _policy(repo),
                          configured_support=_support("work"),
                          evidence_sources=sources)
    assert failure["ok"] is False
    assert failure["error"]["code"] == "unavailable"
    assert failure["reason"] == "source_absent"


def test_malformed_oversized_and_unknown_requests_fail_closed(repo: Path) -> None:
    base = make_request("graph")
    cases = []
    unknown_map = json.loads(json.dumps(base))
    unknown_map["map"] = "everything"
    cases.append((unknown_map, "unsupported", "unsupported_map"))
    unknown_field = json.loads(json.dumps(base))
    unknown_field["bogus"] = 1
    cases.append((unknown_field, "unknown_field", "malformed_request"))
    float_budget = json.loads(json.dumps(base))
    float_budget["budgets"]["max_items"] = 2.5
    cases.append((float_budget, "invalid_value", "malformed_request"))
    huge_budget = json.loads(json.dumps(base))
    huge_budget["budgets"]["max_items"] = 9999
    cases.append((huge_budget, "budget_exceeded", "budget_over_hard_bound"))
    wrong_version = json.loads(json.dumps(base))
    wrong_version["schema"]["version"] = "2.0"
    cases.append((wrong_version, "unsupported_version", "unsupported_version"))
    for raw, code, reason in cases:
        response = request_map(raw, _policy(repo))
        assert response["ok"] is False
        assert response["error"]["code"] == code
        assert response["reason"] == reason

    tiny = make_request("graph", budgets={"max_bytes": 1})
    response = request_map(tiny, _policy(repo))
    assert response["ok"] is False
    assert response["error"]["code"] == "budget_exceeded"
    assert response["reason"] == "map_over_budget"


def test_cross_scope_and_stale_references_fail_closed(repo: Path) -> None:
    current = _graph(repo, budgets={"max_items": 3})
    instance = _instance(current)
    policy = _policy(repo)

    other_root = request_map(
        make_request("graph", scope={"root": str(repo.parent)}), policy)
    assert other_root["ok"] is False
    assert other_root["error"]["code"] == "forbidden"
    assert other_root["reason"] == "cross_scope"

    other_repo = request_map(
        make_request("graph", scope={"repo": "repo:other/repo"}), policy)
    assert other_repo["ok"] is False
    assert other_repo["reason"] == "cross_scope"

    stale_snapshot = instance["snapshot"].rsplit("@", 1)[0] + "@" + "0" * 64
    stale = request_map(
        make_request("graph", scope={"snapshot": stale_snapshot}), policy)
    assert stale["ok"] is False
    assert stale["error"]["code"] == "stale"
    assert stale["reason"] == "snapshot_mismatch"

    wrong_checkout = "checkout:acme/demo@" + "f" * 12
    wrong = request_map(
        make_request("graph", scope={"checkout": wrong_checkout}), policy)
    assert wrong["ok"] is False
    assert wrong["error"]["code"] == "forbidden"
    assert wrong["reason"] == "checkout_mismatch"

    model = request_map(
        make_request("graph", scope={"model": "m3"}), policy)
    assert model["ok"] is False
    assert model["error"]["code"] == "forbidden"
    assert model["reason"] == "model_not_allowed"


def test_stale_snapshot_is_labelled_not_hidden(repo: Path) -> None:
    fresh = _graph(repo, budgets={"max_items": 4})
    assert _instance(fresh)["data"]["freshness"] == "current"
    (repo / "pkg" / "core.py").write_text(
        "def add(a, b):\n    return a + b + 1\n", encoding="utf-8")
    stale = _graph(repo, budgets={"max_items": 4})
    instance = _instance(stale)
    assert instance["data"]["freshness"] == "stale"
    assert instance["data"]["state"] == "degraded"
    assert instance["data"]["notices"]
    assert any("changed" in notice or "stale" in notice
               for notice in instance["data"]["notices"])
    strict = request_map(
        make_request("graph", budgets={"max_items": 4}, strict=True),
        _policy(repo), configured_support=_support("graph"))
    assert strict["ok"] is False
    assert strict["error"]["code"] == "stale"


def test_graph_target_is_first_and_neighbourhood_is_bounded(repo: Path) -> None:
    focused = _graph(repo, target={"key": "sym:pkg/core.py::add"},
                     budgets={"max_items": 1})
    item = _instance(focused)["data"]["items"][0]
    assert item["key"] == "sym:pkg/core.py::add"
    assert item["type"] == "Symbol"

    missing = _graph(repo, target={"key": "sym:pkg/core.py::missing"},
                     budgets={"max_items": 1})
    assert missing["ok"] is False
    assert missing["error"]["code"] == "not_found"
    assert missing["reason"] == "target_not_found"


def test_policy_path_scope_filters_graph_and_outline(repo: Path) -> None:
    policy = _policy(repo, allowed_paths=("pkg/core.py",))
    graph = request_map(
        make_request("graph", budgets={"max_items": 50}), policy,
        configured_support=_support("graph"))
    instance = _instance(graph)
    keys = {item.get("key") for item in instance["data"]["items"]}
    assert keys
    assert keys <= {"file:pkg/core.py", "sym:pkg/core.py::add",
                    "sym:pkg/core.py::double"}
    assert instance["scope"]["paths"] == ["pkg/core.py"]

    outside = request_map(
        make_request("outline", target={"target": "pkg/user.py"}), policy,
        configured_support=_support("outline"))
    assert outside["ok"] is False
    assert outside["error"]["code"] == "forbidden"
    assert outside["reason"] == "cross_scope"

    graph_outside = request_map(
        make_request("graph", target={"path": "pkg/user.py"}), policy,
        configured_support=_support("graph"))
    assert graph_outside["ok"] is False
    assert graph_outside["reason"] == "cross_scope"


def test_work_cache_invalidates_when_the_project_snapshot_changes(
        repo: Path) -> None:
    def goal(_root: Path, local: str) -> dict:
        return {
            "state": "present", "observed_at": 1,
            "goal": {"id": local, "status": "open", "criteria": [{
                "id": "c1", "kind": "file_exists",
                "args": {"path": "pkg/core.py"},
                "last": {"status": "pass", "at": 1}}]},
        }

    sources = E.Sources(
        goal=goal, runs=lambda _root, _ids: [],
        test_record=lambda _root: {"state": "absent", "observed_at": 1},
        interpreters=lambda _root: [])
    request = make_request("work", target={"goal": "g1"})
    first = request_map(request, _policy(repo),
                        configured_support=_support("work"),
                        evidence_sources=sources)
    second = request_map(request, _policy(repo),
                         configured_support=_support("work"),
                         evidence_sources=sources)
    assert first["cache"]["state"] == "miss"
    assert second["cache"]["state"] == "hit"

    graph_path = repo / ".ozagia" / "graph.json"
    graph_path.write_text(graph_path.read_text(encoding="utf-8") + " ",
                          encoding="utf-8")
    changed = request_map(request, _policy(repo),
                          configured_support=_support("work"),
                          evidence_sources=sources)
    assert changed["cache"]["state"] == "miss"
    assert changed["cache"]["key"] != first["cache"]["key"]
    assert changed["map_hash"] != first["map_hash"]


def test_cache_hit_miss_and_source_invalidation(repo: Path) -> None:
    first = _graph(repo, budgets={"max_items": 3})
    second = _graph(repo, budgets={"max_items": 3})
    assert first["cache"]["state"] == "miss"
    assert second["cache"]["state"] == "hit"
    assert first["cache"]["key"] == second["cache"]["key"]
    assert first["cache"]["path"].startswith(".ozagia/model_api/cache/")

    before = first["source"]["signature"]
    (repo / "pkg" / "core.py").write_text(
        "def add(a, b):\n    return a + b + 2\n", encoding="utf-8")
    changed = _graph(repo, budgets={"max_items": 3})
    assert changed["cache"]["state"] == "miss"
    assert changed["cache"]["key"] != first["cache"]["key"]
    assert changed["source"]["signature"] != before
    assert _instance(changed)["data"]["freshness"] == "stale"


def test_cache_is_partitioned_by_model_and_session(repo: Path) -> None:
    policy = _policy(repo)
    first = _graph(repo, budgets={"max_items": 3})
    other_model = request_map(
        make_request("graph", scope={"model": "m2"},
                     budgets={"max_items": 3}),
        policy, configured_support=_support("graph"))
    again = _graph(repo, budgets={"max_items": 3})
    assert first["cache"]["state"] == "miss"
    assert other_model["cache"]["state"] == "miss"
    assert again["cache"]["state"] == "hit"
    assert first["cache"]["key"] != other_model["cache"]["key"]
    assert _instance(first)["scope"]["model"] == "m1"
    assert _instance(other_model)["scope"]["model"] == "m2"
    assert first["map_hash"] != other_model["map_hash"]

    other_session = request_map(
        make_request("graph", scope={"session": "s2"},
                     budgets={"max_items": 3}),
        policy, configured_support=_support("graph"))
    assert other_session["cache"]["state"] == "miss"
    assert other_session["cache"]["key"] not in {
        first["cache"]["key"], other_model["cache"]["key"]}


def test_cache_rejects_a_foreign_scope(repo: Path) -> None:
    response = _graph(repo, budgets={"max_items": 3})
    instance = _instance(response)
    store = CacheStore(repo)
    scope = dict(instance["scope"])
    scope["model"] = "m2"
    hit = store.read(response["cache"]["key"], map_type="graph",
                     schema_fingerprint=map_schema_fingerprint("graph"),
                     scope=scope,
                     source_signature=response["source"]["signature"])
    assert hit is None
    own = store.read(response["cache"]["key"], map_type="graph",
                     schema_fingerprint=map_schema_fingerprint("graph"),
                     scope=instance["scope"],
                     source_signature=response["source"]["signature"])
    assert own is not None and own["map_hash"] == response["map_hash"]


def test_cache_path_escape_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".ozagia").symlink_to(outside)
    store = CacheStore(root)
    with pytest.raises(ModelAPIError) as exc:
        store.read("a" * 64, map_type="graph",
                   schema_fingerprint=map_schema_fingerprint("graph"),
                   scope={"root": str(root), "session": "s", "model": "m"},
                   source_signature="b" * 64)
    assert exc.value.reason == "cache_path_escape"
    assert list(outside.iterdir()) == []


def test_cursor_is_bound_to_scope_and_source(repo: Path) -> None:
    policy = _policy(repo)
    first = _graph(repo, budgets={"max_items": 3})
    cursor = first["omissions"]["next_cursor"]
    assert cursor.startswith("v1:")
    second = request_map(
        make_request("graph", budgets={"max_items": 3}, cursor=cursor),
        policy, configured_support=_support("graph"))
    assert second["ok"] is True
    assert _instance(second)["data"]["bounds"]["page_start"] == 3

    other_model = request_map(
        make_request("graph", scope={"model": "m2"},
                     budgets={"max_items": 3}, cursor=cursor),
        policy, configured_support=_support("graph"))
    assert other_model["ok"] is False
    assert other_model["error"]["code"] == "stale"
    assert other_model["reason"] == "cursor_scope_mismatch"

    (repo / "pkg" / "core.py").write_text(
        "def add(a, b):\n    return a + b + 3\n", encoding="utf-8")
    changed = request_map(
        make_request("graph", budgets={"max_items": 3}, cursor=cursor),
        policy, configured_support=_support("graph"))
    assert changed["ok"] is False
    assert changed["reason"] == "cursor_scope_mismatch"


def test_budget_bytes_trims_with_explicit_omission(repo: Path) -> None:
    response = _graph(repo, budgets={"max_items": 50, "max_bytes": 4096})
    instance = _instance(response)
    size = len(C.canonical_json(instance).encode("utf-8"))
    assert size <= 4096
    assert instance["omissions"]["truncated"] is True
    assert instance["omissions"]["reason"] in {"bytes", "limit"}
    assert len(instance["data"]["items"]) < 50


def test_negotiation_matrix_and_fallbacks(repo: Path) -> None:
    fallback = request_map(
        make_request("graph", format="structured", budgets={"max_items": 2}),
        _policy(repo))
    assert fallback["ok"] is True
    assert fallback["format"] == "plain"
    assert fallback["requested_format"] == "structured"
    assert fallback["fallback"]["reason"] == "configured_unknown"
    assert fallback["status"] == "degraded"
    matrix = fallback["negotiation"]["matrix"]
    assert len(matrix) == 4
    assert all(row["version"] == "1.0" for row in matrix)
    assert fallback["negotiation"]["protocol_levels"]["claimed"] == []
    assert fallback["negotiation"]["protocol_levels"]["implemented"] == [
        "plain", "structured-v1"]
    assert fallback["trace"]["redacted"] is True
    assert all(event["event"] == "negotiate"
               for event in fallback["trace"]["events"])
    assert SECRET not in C.canonical_json(fallback)

    strict = request_map(
        make_request("graph", format="structured", strict=True),
        _policy(repo))
    assert strict["ok"] is False
    assert strict["error"]["code"] == "unsupported"
    assert strict["reason"] == "unsupported_format"

    observed_no = request_map(
        make_request("graph", format="structured"),
        _policy(repo), configured_support={"graph": "supported"},
        observed_support={"graph": "unsupported"})
    assert observed_no["format"] == "plain"
    assert observed_no["fallback"]["reason"] == "observed_unsupported"

    observed_yes = request_map(
        make_request("graph", format="structured"),
        _policy(repo), configured_support={"graph": "supported"},
        observed_support={"graph": "supported"})
    assert observed_yes["format"] == "structured"
    assert observed_yes["negotiation"]["matrix"][0]["observed"] in {
        "supported", "unknown"}


def test_writes_stay_under_the_state_directory(repo: Path) -> None:
    before = {path.relative_to(repo).as_posix()
              for path in repo.rglob("*") if path.is_file()}
    response = _graph(repo, budgets={"max_items": 3})
    assert response["ok"] is True
    after = {path.relative_to(repo).as_posix()
             for path in repo.rglob("*") if path.is_file()}
    added = after - before
    assert added
    assert all(path.startswith(".ozagia/model_api/cache/") for path in added)
    assert response["cache"]["path"].startswith(".ozagia/model_api/cache/")


def test_mcp_dev_maps_is_additive_and_bounded(repo: Path) -> None:
    dump = repo / ".ozagia" / "graph.json"
    mem = DevMemory(dump, repo / ".ozagia" / "journal.jsonl", root=repo)
    server = DevMemoryServer(mem)
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
    assert "dev_maps" in tools
    schema = tools["dev_maps"]["inputSchema"]
    assert schema["properties"]["map"]["enum"] == [
        "graph", "outline", "capabilities", "work"]
    assert schema["properties"]["limit"]["default"] == 25
    assert schema["additionalProperties"] is False

    call = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "dev_maps", "arguments": {
            "map": "graph", "limit": 2, "session": "mcp-s",
            "model": "mcp-m"}}})
    assert call["result"]["isError"] is False
    payload = json.loads(call["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert len(payload["representation"]["instance"]["data"]["items"]) == 2
    assert payload["cache"]["path"].startswith(".ozagia/model_api/cache/")

    again = server.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "dev_maps", "arguments": {
            "map": "graph", "limit": 2, "session": "mcp-s",
            "model": "mcp-m"}}})
    again_payload = json.loads(again["result"]["content"][0]["text"])
    assert again_payload["cache"]["state"] == "hit"

    bad = server.handle({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "dev_maps", "arguments": {"map": "nope"}}})
    assert bad["error"]["code"] == -32602


def test_tampered_map_and_cache_entry_are_not_served(repo: Path) -> None:
    response = _graph(repo, budgets={"max_items": 3})
    instance = json.loads(json.dumps(_instance(response)))
    instance["data"]["items"][0]["trust"] = "trusted"
    with pytest.raises(ModelAPIError):
        validate_map(instance)

    path = repo / response["cache"]["path"]
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["instance"]["data"]["items"][0]["trust"] = "trusted"
    path.write_text(C.canonical_json(entry), encoding="utf-8")
    store = CacheStore(repo)
    hit = store.read(response["cache"]["key"], map_type="graph",
                     schema_fingerprint=map_schema_fingerprint("graph"),
                     scope=_instance(response)["scope"],
                     source_signature=response["source"]["signature"])
    assert hit is None


def test_cache_key_includes_source_scope_model_and_session(tmp_path: Path) -> None:
    def key(**overrides):
        values = {
            "map_type": "graph",
            "schema_fingerprint": "a" * 64,
            "source_signature": "b" * 64,
            "scope": {"root": str(tmp_path), "session": "s1", "model": "m1"},
            "target": {},
            "budgets": {"max_items": 5},
            "cursor": None,
            "policy_hash": "c" * 64,
        }
        values.update(overrides)
        return cache_key(**values)

    base = key()
    assert base != key(source_signature="d" * 64)
    assert base != key(scope={"root": str(tmp_path), "session": "s2",
                              "model": "m1"})
    assert base != key(scope={"root": str(tmp_path), "session": "s1",
                              "model": "m2"})
    assert base != key(policy_hash="e" * 64)
    assert base != key(budgets={"max_items": 6})
    assert base == key()
