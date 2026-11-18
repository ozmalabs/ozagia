import pytest

from ozagia.core.deep_review.analyzers.license_policy import (
    MAX_BYTES,
    MAX_LOCKFILES,
    MAX_RESULTS,
    MAX_SCANNED_PACKAGES,
    SCHEMA,
    evaluate_license_policy,
)


def _policy(**lists):
    return {"schema": SCHEMA, **lists}


def test_policy_marks_exact_allowed_denied_composite_and_missing_unknown():
    inventory = {"lockfiles": [{"file": "lock.json", "packages": [
        {"name": "a", "version": "1", "license": "MIT"},
        {"name": "b", "version": "1", "license": "GPL-3.0-only"},
        {"name": "c", "version": "1", "license": "MIT OR Apache-2.0"},
        {"name": "d", "version": "1"},
    ]}]}
    result = evaluate_license_policy(
        inventory, _policy(allow=["MIT"], deny=["GPL-3.0-only"]))
    assert [row["state"] for row in result["results"]] == [
        "allowed", "denied", "unknown", "unknown"]
    assert result["allowed"] == 1
    assert result["denied"] == 1
    assert result["unknown"] == 2
    assert len(result["findings"]) == 1
    assert result["findings"][0]["truth_assessed"] is False
    assert result["truth_assessed"] is False
    assert "no legal or compliance conclusion" in result["not_checked"]


def test_known_but_unclassified_license_is_unknown():
    inventory = {"lockfiles": [{"packages": [
        {"name": "a", "version": "1", "license": "BSD-3-Clause"}]}]}
    result = evaluate_license_policy(inventory, _policy(deny=["MIT"]))
    assert result["results"][0]["state"] == "unknown"


def test_spdx_expression_policy_uses_conservative_and_or_semantics():
    expressions = [
        "MIT AND Apache-2.0",
        "MIT OR Apache-2.0",
        "MIT OR GPL-3.0-only",
        "MIT AND GPL-3.0-only",
        "(MIT OR Apache-2.0) AND GPL-3.0-only",
    ]
    inventory = {"lockfiles": [{"packages": [
        {"name": f"p{i}", "version": "1", "license": expression}
        for i, expression in enumerate(expressions)]}]}
    result = evaluate_license_policy(
        inventory, _policy(allow=["MIT", "Apache-2.0"],
                           deny=["GPL-3.0-only"]))
    assert [row["state"] for row in result["results"]] == [
        "allowed", "allowed", "unknown", "denied", "denied"]
    assert result["results"][2]["reason"]
    assert result["findings"][0]["license"] == expressions[3]
    assert "evaluates as denied" in result["findings"][0]["summary"]


@pytest.mark.parametrize("expression", [
    "MIT WITH LLVM-exception", "MIT OR LicenseRef-Internal",
    "MIT AND (Apache-2.0 OR", "MIT XOR Apache-2.0", "",
    "(" * 33 + "MIT" + ")" * 33,
    "MIT " + "OR MIT " * 150,
    "x" * 1025,
])
def test_unsupported_or_overbound_spdx_expression_stays_unknown(expression):
    inventory = {"lockfiles": [{"packages": [
        {"name": "p", "version": "1", "license": expression}]}]}
    result = evaluate_license_policy(
        inventory, _policy(allow=["MIT"], deny=["Apache-2.0"]))
    assert result["results"][0]["state"] == "unknown"


def test_truncated_result_counts_match_emitted_rows():
    inventory = {"lockfiles": [{"packages": [
        {"name": f"p{index}", "version": "1", "license": "Unknown-1"}
        for index in range(MAX_RESULTS + 1)]}]}
    result = evaluate_license_policy(inventory, _policy(allow=["MIT"]))
    assert result["truncated"] is True
    assert len(result["results"]) == MAX_RESULTS
    assert result["unknown"] == MAX_RESULTS
    assert result["allowed"] + result["denied"] + result["unknown"] == len(
        result["results"])


def test_policy_inventory_iteration_is_bounded():
    inventory = {"lockfiles": [
        {"packages": [None] * (MAX_SCANNED_PACKAGES + 1)},
        *({"packages": []} for _ in range(MAX_LOCKFILES)),
    ]}
    result = evaluate_license_policy(inventory, _policy(allow=["MIT"]))
    assert result["truncated"] is True
    assert result["results"] == []


@pytest.mark.parametrize("policy", [
    {"schema": SCHEMA, "allow": ["MIT OR Apache-2.0"]},
    {"schema": SCHEMA, "allow": ["not-an-spdx-id"]},
    {"schema": SCHEMA, "allow": ["MIT"], "deny": ["MIT"]},
    {"schema": SCHEMA},
])
def test_policy_rejects_ambiguous_or_invalid_rules(policy):
    with pytest.raises(ValueError):
        evaluate_license_policy({"lockfiles": []}, policy)


def test_missing_policy_is_unknown():
    result = evaluate_license_policy({"lockfiles": []}, None)
    assert result["state"] == "unknown"
    assert result["findings"] == []


def test_policy_sources_are_bounded_and_reject_duplicate_json_keys(tmp_path):
    with pytest.raises(ValueError, match="bound"):
        evaluate_license_policy({"lockfiles": []},
                                {"schema": SCHEMA, "allow": [],
                                 "extra": "x" * MAX_BYTES})
    path = tmp_path / "policy.json"
    path.write_text('{"schema":"ozagia.license_policy.v1",'
                    '"schema":"ozagia.license_policy.v1","allow":["MIT"]}',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="valid JSON"):
        evaluate_license_policy({"lockfiles": []}, path)


def test_license_policy_flag_is_forwarded_to_deep_review(tmp_path, monkeypatch):
    from ozagia import cli
    from ozagia.core import deep_review

    calls = []

    def fake_build(root, out, **kwargs):
        calls.append(kwargs)
        out.mkdir(parents=True, exist_ok=True)
        return {"manifest": {"schema": "ozagia.deep_review.manifest.v1"},
                "findings": [], "out": str(out), "failed": False}

    monkeypatch.setattr(deep_review, "build_deep_review", fake_build)
    policy = tmp_path / "policy.json"
    policy.write_text('{"schema":"ozagia.license_policy.v1","allow":["MIT"]}',
                      encoding="utf-8")
    out = tmp_path.parent / (tmp_path.name + "-license-policy-report")
    assert cli.main(["deep-review", "--root", str(tmp_path), "--out", str(out),
                     "--profile", "dependencies", "--license-policy",
                     str(policy)]) == 0
    assert calls[0]["license_policy"] == str(policy)


def test_license_policy_is_limited_to_dependency_profiles_without_target_writes(
        tmp_path):
    from ozagia.core.deep_review.orchestrator import (
        DeepReviewError,
        build_deep_review,
    )

    out = tmp_path.parent / (tmp_path.name + "-should-not-exist")
    with pytest.raises(DeepReviewError, match="full or dependencies"):
        build_deep_review(tmp_path, out, profile="security",
                          license_policy={"schema": SCHEMA, "allow": ["MIT"]})
    assert not out.exists()
    assert list(tmp_path.iterdir()) == []
