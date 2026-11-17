"""Tests for the frozen version-1 self-graph contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia.core import contracts as C

FIXTURES = Path(__file__).parent / "fixtures" / "selfgraph"
VECTORS = json.loads(
    (FIXTURES / "canonical_vectors.json").read_text(encoding="utf-8")
)["vectors"]


def schema(**overrides):
    base = {
        "namespace": "ozagia.graph",
        "version": "1",
        "types": {"node": {"fields": ["key", "kind"]}},
        "required": ["kind", "key"],
        "constraints": {"max_nodes": 200},
        "sensitivity": "internal",
    }
    base.update(overrides)
    return base


def repo_ids():
    repo = C.repo_id("owner", "repo")
    checkout = C.checkout_id(repo, "a" * 64)
    snapshot = C.snapshot_id(checkout, "b" * 64)
    return repo, checkout, snapshot


def instance(**overrides):
    repo, checkout, snapshot = repo_ids()
    base = {
        "schema": {"namespace": "ozagia.graph", "version": "1.0"},
        "scope": {"repo": repo},
        "snapshot": snapshot,
        "inputs": ["sha256:" + "c" * 64],
        "provenance": {"source": "tests"},
        "data": {"nodes": [1, 2, 3]},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vector", VECTORS, ids=[v["name"] for v in VECTORS])
def test_canonical_vector_exact(vector):
    assert C.canonical_json(vector["value"]) == vector["json"]
    assert C.content_hash(vector["value"]) == vector["sha256"]
    assert json.loads(vector["json"]) == vector["value"]


def test_unicode_is_utf8_not_ascii_escaped():
    text = C.canonical_json({"message": "héllo ☃"})
    assert "héllo ☃" in text
    assert "\\u" not in text
    assert "\n" not in text
    assert text.encode("utf-8").decode("utf-8") == text


def test_nested_keys_are_sorted_at_every_level():
    value = {"z": [{"b": 1, "a": 2}], "a": {"y": 3, "x": 4}}
    assert C.canonical_json(value) == '{"a":{"x":4,"y":3},"z":[{"a":2,"b":1}]}'


def test_duplicate_nfc_keys_are_rejected():
    value = {"e\u0301": 1, "\u00e9": 2}
    with pytest.raises(C.DuplicateKeyError) as info:
        C.canonical_json(value)
    assert info.value.code == "contract.duplicate_key"


@pytest.mark.parametrize(
    "value, error",
    [
        (1.5, C.FloatNotAllowedError),
        (float("nan"), C.FloatNotAllowedError),
        (float("inf"), C.FloatNotAllowedError),
        (float("-inf"), C.FloatNotAllowedError),
        ({"a": 1.0}, C.FloatNotAllowedError),
        (b"bytes", C.CanonicalJSONError),
        (("tuple",), C.CanonicalJSONError),
        ({"set"}, C.CanonicalJSONError),
        ({1: "int key"}, C.InvalidKeyError),
        ({None: "none key"}, C.InvalidKeyError),
        (2**63, C.IntOutOfRangeError),
        (-(2**63) - 1, C.IntOutOfRangeError),
        ("\ud800", C.InvalidUnicodeError),
        ({"ok": [1, 2]}, None),
    ],
    ids=[
        "float", "nan", "inf", "negative-inf", "nested-float", "bytes",
        "tuple", "set", "non-string-key", "none-key", "int-too-big",
        "int-too-small", "lone-surrogate", "control-positive",
    ],
)
def test_canonical_negative_cases_are_typed(value, error):
    if error is None:
        assert C.canonical_json(value) == '{"ok":[1,2]}'
        return
    with pytest.raises(C.ContractError) as info:
        C.canonical_json(value)
    assert isinstance(info.value, error)
    assert isinstance(info.value, C.ContractError)
    assert info.value.code in C.ERROR_CODES


def test_canonical_depth_is_bounded():
    value = 0
    for _ in range(C.MAX_CANON_DEPTH + 2):
        value = [value]
    with pytest.raises(C.BoundsError) as info:
        C.canonical_json(value)
    assert info.value.code == "contract.bounds_exceeded"


def test_content_hash_is_sha256_of_utf8_canonical_text():
    value = {"a": "é", "b": 2}
    import hashlib

    expected = hashlib.sha256(
        C.canonical_json(value).encode("utf-8")).hexdigest()
    assert C.content_hash(value) == expected


# ---------------------------------------------------------------------------
# Version and schema records
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version, parsed", [
    ("1", (1, 0)), ("1.0", (1, 0)), ("1.7", (1, 7)),
])
def test_supported_versions(version, parsed):
    assert C.parse_version(version) == parsed
    assert C.normalize_version(version) == f"{parsed[0]}.{parsed[1]}"


@pytest.mark.parametrize("version", ["2", "2.0", "1.2.3", "v1", "", "1.x"])
def test_bad_versions_are_typed(version):
    with pytest.raises(C.ContractError) as info:
        C.parse_version(version)
    assert isinstance(info.value, C.VersionError)
    assert info.value.code in {
        "contract.invalid_version", "contract.unsupported_version"}


def test_wrong_major_is_unsupported():
    with pytest.raises(C.UnsupportedVersionError) as info:
        C.parse_version("2.0")
    assert info.value.code == "contract.unsupported_version"


def test_schema_fingerprint_ignores_required_order_and_extensions():
    first = schema()
    second = schema(required=["key", "kind"], **{"x-owner": "team"})
    assert C.schema_fingerprint(first) == C.schema_fingerprint(second)
    assert C.schema_fingerprint(first) == C.schema_fingerprint(
        schema(version="1.0"))


def test_schema_fingerprint_changes_with_core_fields():
    first = C.schema_fingerprint(schema())
    assert first != C.schema_fingerprint(schema(sensitivity="private"))
    assert first != C.schema_fingerprint(
        schema(constraints={"max_nodes": 201}))


def test_schema_rejects_unknown_non_extension_field():
    with pytest.raises(C.UnknownFieldError) as info:
        C.validate_schema(schema(extra="nope"))
    assert info.value.code == "contract.unknown_field"


def test_schema_rejects_wrong_major():
    with pytest.raises(C.UnsupportedVersionError):
        C.schema_fingerprint(schema(version="2.0"))


def test_schema_rejects_missing_and_bad_fields():
    with pytest.raises(C.SchemaError):
        C.validate_schema({"namespace": "ozagia.graph"})
    with pytest.raises(C.VocabularyError):
        C.validate_schema(schema(sensitivity="top-secret"))
    with pytest.raises(C.SchemaError):
        C.validate_schema(schema(required=["kind", "kind"]))
    with pytest.raises(C.SchemaError):
        C.validate_schema(schema(types=[]))
    with pytest.raises(C.SchemaError):
        C.validate_schema(schema(namespace="Not.A.Namespace"))
    with pytest.raises(C.UnknownFieldError):
        C.validate_schema({**schema(), 1: "non-string-key"})


# ---------------------------------------------------------------------------
# Instance hashing
# ---------------------------------------------------------------------------


def test_instance_hash_excludes_transport_fields():
    base = instance()
    changed = instance(created_at="2026-11-17T00:00:00Z",
                       cursor="page-2", trace_id="trace-1")
    assert C.instance_hash(base) == C.instance_hash(changed)


def test_instance_extensions_are_hashed_unless_declared_transport_only():
    base = instance()
    extended = instance(**{"x-build": "ci-42"})
    assert C.instance_hash(base) != C.instance_hash(extended)
    assert C.instance_hash(base) == C.instance_hash(
        extended, transport_only=("x-build",))


def test_instance_schema_extensions_are_hashed():
    base = instance()
    extended = instance(schema={**base["schema"], "x-kind": "graph"})
    assert C.instance_hash(base) != C.instance_hash(extended)


def test_instance_rejects_unknown_and_non_transport_exclusions():
    with pytest.raises(C.UnknownFieldError):
        C.instance_hash(instance(extra="nope"))
    with pytest.raises(C.SchemaError):
        C.instance_hash(instance(), transport_only=("data",))


def test_instance_rejects_wrong_major_and_bad_omissions():
    with pytest.raises(C.UnsupportedVersionError):
        C.instance_hash(instance(schema={"namespace": "ozagia.graph",
                                         "version": "2.0"}))
    with pytest.raises(C.TruncationError):
        C.instance_hash(instance(omissions={"truncated": True}))


def test_instance_rejects_missing_and_bad_types():
    with pytest.raises(C.SchemaError):
        C.instance_hash({"schema": {"namespace": "ozagia.graph",
                                    "version": "1.0"}})
    with pytest.raises(C.SchemaError):
        C.instance_hash(instance(inputs=("a",)))
    with pytest.raises(C.SchemaError):
        C.instance_hash(instance(created_at=True))


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------


def test_identity_round_trips_and_escaping():
    repo = C.repo_id("owner", "repo")
    assert repo == "repo:owner/repo"
    assert C.parse_id(repo).components == ("owner", "repo")
    checkout = C.checkout_id(repo, "a" * 64)
    assert checkout == "checkout:owner/repo@aaaaaaaaaaaa"
    assert C.parse_id(checkout).components == ("owner", "repo", "a" * 12)
    snapshot = C.snapshot_id(checkout, "b" * 64)
    assert C.parse_id(snapshot).components == (
        "owner", "repo", "a" * 12, "b" * 64)
    project = C.qualify(repo, checkout, snapshot,
                        "sym:src/tiny/core.py::add")
    assert C.local_key(project) == "sym:src/tiny/core.py::add"
    assert C.parse_id(project).components[-1] == "sym:src/tiny/core.py::add"
    assert C.canonical_id(project) == project


def test_identity_escapes_slashes_colons_and_at_signs():
    assert C.parse_id("repo:group%2Fsub/repo").components == (
        "group/sub", "repo")
    parsed = C.parse_id("run:a%2Fb%3Ac%40d%25e")
    assert parsed.components == ("a/b:c@d%e",)
    assert parsed.canonical == "run:a%2Fb%3Ac%40d%25e"


def test_existing_unqualified_graph_keys_are_unchanged():
    repo, checkout, snapshot = repo_ids()
    for key in ("sym:src/tiny/core.py::add", "text:README.md#L1-L2",
                "file:src/tiny/core.py"):
        qualified = C.qualify(repo, checkout, snapshot, key)
        assert qualified.endswith("/" + key)
        assert C.local_key(qualified) == key


@pytest.mark.parametrize("origin, expected", [
    ("https://github.com/Owner/Repo.git", "repo:owner/repo"),
    ("git@github.com:owner/repo.git", "repo:owner/repo"),
    ("ssh://git@gitlab.example.com/group/sub/repo.git",
     "repo:group%2Fsub/repo"),
    ("git://example.test/owner/repo", "repo:owner/repo"),
    ("https://example.test:8443/owner/repo/", "repo:owner/repo"),
])
def test_repo_identity_from_origin(origin, expected):
    assert C.repo_id_from_origin(origin) == expected


@pytest.mark.parametrize("origin", [
    "", "file:///tmp/repo", "/tmp/repo", "https://host/only",
    "https://host/a/../b", "ftp://host/owner/repo",
])
def test_repo_identity_rejects_unstable_origins(origin):
    with pytest.raises(C.IDError):
        C.repo_id_from_origin(origin)


@pytest.mark.parametrize("text", [
    "cap:", "cap:Graph.find", "cap:graph/find", "repo:owner",
    "repo:owner/name/extra", "checkout:owner/repo@aaaaaaaaaaa",
    "checkout:owner/repo@AAAAAAAAAAAA",
    "snapshot:owner/repo@aaaaaaaaaaaa",
    "project:owner/repo@aaaaaaaaaaaa:bbbbbbbbbbbb",
    "project:owner/repo@aaaaaaaaaaaa:bbbbbbbbbbbb/",
    "run:a%2fb", "run:%61", "run:a%ZZb", "cap:graph.find.",
    "repo:owner/Na me", "cap:gráph", "unknown:thing",
])
def test_malformed_ids_are_typed(text):
    with pytest.raises(C.IDError) as info:
        C.parse_id(text)
    assert isinstance(info.value, C.ContractError)
    assert info.value.code == "contract.invalid_id"


def test_overlong_identity_is_rejected():
    with pytest.raises(C.IDError):
        C.parse_id("cap:" + "a" * (C.MAX_ID_LENGTH + 1))


def test_identity_collision_detection():
    repo, checkout, snapshot = repo_ids()
    key = "sym:src/a.py::X"
    project = C.qualify(repo, checkout, snapshot, key)
    with pytest.raises(C.IDCollisionError) as info:
        C.assert_unique_ids([project, project])
    assert info.value.code == "contract.id_collision"
    assert C.assert_unique_ids([repo, checkout, snapshot, project]) == sorted(
        [repo, checkout, snapshot, project])


def test_qualify_rejects_mismatched_identities():
    repo, checkout, snapshot = repo_ids()
    other = C.repo_id("other", "repo")
    other_checkout = C.checkout_id(other, "d" * 64)
    other_snapshot = C.snapshot_id(other_checkout, "e" * 64)
    with pytest.raises(C.IDError):
        C.qualify(other, checkout, snapshot, "file:a")
    with pytest.raises(C.IDError):
        C.qualify(repo, other_checkout, other_snapshot, "file:a")
    with pytest.raises(C.IDError):
        C.qualify(repo, checkout, snapshot, "")


def test_format_id_round_trips():
    repo, checkout, snapshot = repo_ids()
    assert C.format_id("cap", "graph.find") == "cap:graph.find"
    assert C.format_id("repo", "owner", "repo") == repo
    assert C.format_id("checkout", repo, "a" * 64) == checkout
    assert C.format_id("snapshot", checkout, "b" * 64) == snapshot
    assert C.format_id(
        "project", repo, checkout, snapshot, "file:a") == C.qualify(
            repo, checkout, snapshot, "file:a")


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_vocabulary_constants_and_validators():
    for value in C.FRESHNESS:
        assert C.freshness(value) == value
    for value in C.EVIDENCE:
        assert C.evidence(value) == value
    for value in C.RESULTS:
        assert C.result(value) == value
    assert C.RESULTS == (
        "declared", "pending", "passed", "failed", "unknown", "invalid",
        "not_assessed")
    with pytest.raises(C.VocabularyError):
        C.freshness("fresh")
    with pytest.raises(C.VocabularyError):
        C.evidence("claimed")
    with pytest.raises(C.VocabularyError):
        C.result("green")
    for value in C.CLOCKS:
        assert C.clock(value) == value
    with pytest.raises(C.VocabularyError):
        C.clock("wall")


@pytest.mark.parametrize("parent, child, expected", [
    ("current", "current", "current"),
    ("current", "stale", "stale"),
    ("current", "unknown", "unknown"),
    ("current", "absent", "absent"),
    ("stale", "current", "stale"),
    ("stale", "unknown", "unknown"),
    ("unknown", "current", "unknown"),
    ("unknown", "stale", "unknown"),
    ("absent", "current", "absent"),
])
def test_join_freshness_never_upgrades(parent, child, expected):
    assert C.join_freshness(parent, child) == expected


def test_readiness_unknown_is_distinct_from_false():
    assert C.readiness(True) is True
    assert C.readiness(False) is False
    assert C.readiness(C.READINESS_UNKNOWN) == C.READINESS_UNKNOWN
    with pytest.raises(C.VocabularyError):
        C.readiness(None)
    full = C.readiness_map({})
    assert set(full) == set(C.READINESS_DIMENSIONS)
    assert set(full.values()) == {C.READINESS_UNKNOWN}
    partial = C.readiness_map({"trusted": False, "installed": True})
    assert partial["trusted"] is False
    assert partial["installed"] is True
    assert partial["permitted"] == C.READINESS_UNKNOWN
    with pytest.raises(C.VocabularyError):
        C.readiness_map({"available": True})


# ---------------------------------------------------------------------------
# Fact and relation records
# ---------------------------------------------------------------------------


def test_fact_serialization_is_canonical_and_sorted():
    fact = C.Fact(
        subject="file:src/tiny/core.py",
        value={"b": 2, "a": 1},
        source="tests",
        owner="ozagia.core.graph",
        observed_at=1_700_000_000,
        subject_version="sha256:" + "a" * 64,
        freshness="current",
        evidence="observed",
        scope=("repo:example/tiny", "repo:example/tiny"),
    )
    data = fact.to_dict()
    assert data["version"] == C.FACT_VERSION
    assert data["scope"] == ["repo:example/tiny"]
    assert C.canonical_json(data).startswith('{"evidence":"observed"')


def test_fact_rejects_bad_vocabulary_and_untyped_clocks():
    with pytest.raises(C.VocabularyError):
        C.Fact("file:a", 1, "tests", 1, "fresh", "observed")
    with pytest.raises(C.VocabularyError):
        C.Fact("file:a", 1, "tests", 1, "current", "claimed")
    with pytest.raises(C.SchemaError):
        C.Fact("file:a", 1, "tests", 1.5, "current", "observed")
    with pytest.raises(C.SchemaError):
        C.Fact("", 1, "tests", 1, "current", "observed")


def test_fact_values_are_bounded():
    with pytest.raises(C.BoundsError):
        C.Fact("file:a", {"x": "y" * C.MAX_FACT_BYTES}, "tests", 1,
               "current", "observed")
    nested = 0
    for _ in range(C.MAX_VALUE_DEPTH + 2):
        nested = [nested]
    with pytest.raises(C.BoundsError):
        C.Fact("file:a", nested, "tests", 1, "current", "observed")
    with pytest.raises(C.BoundsError):
        C.Fact("file:a", 1, "tests", 1, "current", "observed",
               scope=tuple(f"s{i}" for i in range(C.MAX_SCOPE_ITEMS + 1)))
    with pytest.raises(C.FloatNotAllowedError):
        C.Fact("file:a", 1.5, "tests", 1, "current", "observed")


def test_relation_serialization_and_validation():
    relation = C.Relation(
        type="depends-on",
        from_id="cap:test.run",
        to_id="cap:index.build",
        source="tests",
        observed_at="2026-11-17T00:00:00Z",
        freshness="current",
        evidence="inferred",
        scope=("repo:example/tiny",),
    )
    data = relation.to_dict()
    assert data["version"] == C.RELATION_VERSION
    assert data["type"] == "depends-on"
    assert data["from"] == "cap:test.run"
    assert data["to"] == "cap:index.build"
    assert C.canonical_json(data).startswith('{"evidence":"inferred"')
    with pytest.raises(C.SchemaError):
        C.Relation("Depends On", "a", "b", "tests", 1, "current",
                   "observed")
    with pytest.raises(C.SchemaError):
        C.Relation("depends-on", "", "b", "tests", 1, "current",
                   "observed")


# ---------------------------------------------------------------------------
# Service errors and truncation
# ---------------------------------------------------------------------------


def test_service_error_is_typed():
    error = C.ServiceError("stale", "graph is stale", retryable=True,
                           details={"age_ms": 5})
    assert error.to_dict() == {
        "code": "stale", "message": "graph is stale", "retryable": True,
        "details": {"age_ms": 5}}
    with pytest.raises(C.ContractError) as info:
        C.ServiceError("free text", "nope")
    assert info.value.code == "contract.invalid_service_error"
    with pytest.raises(C.ContractError):
        C.ServiceError("internal", "line\nbreak")
    with pytest.raises(C.FloatNotAllowedError):
        C.ServiceError("internal", "bad", details={"x": 1.5})


def test_truncation_envelope_invariants():
    empty = C.Truncation()
    assert empty.to_dict() == {
        "truncated": False, "omitted": 0, "next_cursor": None,
        "reason": None}
    cut = C.truncation(True, omitted=12, next_cursor="page-2",
                       reason="limit")
    assert cut.to_dict()["omitted"] == 12
    with pytest.raises(C.TruncationError):
        C.Truncation(truncated=False, omitted=1)
    with pytest.raises(C.TruncationError):
        C.Truncation(truncated=True)
    with pytest.raises(C.TruncationError):
        C.Truncation(truncated=True, reason="because")
    with pytest.raises(C.TruncationError):
        C.Truncation(truncated=True, reason="limit",
                     next_cursor="x" * (C.MAX_CURSOR + 1))
    assert C.validate_truncation(cut) == cut.to_dict()
    with pytest.raises(C.TruncationError):
        C.validate_truncation({"truncated": True, "reason": "limit",
                               "extra": 1})


def test_error_code_vocabulary_is_closed():
    assert "contract.invalid_value" in C.ERROR_CODES
    assert "internal" in C.SERVICE_ERROR_CODES
    assert C.CONTRACT_ERROR_CODES.isdisjoint(C.SERVICE_ERROR_CODES)
    assert C.ContractError("x").code in C.ERROR_CODES
