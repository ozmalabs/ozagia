import json

from ozagia.core.iac_findings import import_sarif


def make_sarif(*, uri="infra/main.tf", message="CANARY_SECRET", uri_base=None):
    artifact = {"uri": uri}
    if uri_base is not None:
        artifact["uriBaseId"] = uri_base
    return json.dumps(
        {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "Checkov",
                            "version": "3.2.1",
                            "rules": [{"id": "CKV_AWS_1"}],
                        }
                    },
                    "results": [
                        {
                            "ruleId": "CKV_AWS_1",
                            "level": "error",
                            "message": {"text": message},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": artifact,
                                        "region": {"startLine": 17},
                                    }
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )


def test_imports_only_allowlisted_source_fact_fields():
    text = make_sarif(message="password=CANARY_SECRET; token=ANOTHER_CANARY")
    result = import_sarif(text)
    assert len(result.findings) == 1
    fact = result.findings[0]
    assert (fact.rule_id, fact.severity, fact.path, fact.line) == (
        "CKV_AWS_1",
        "error",
        "infra/main.tf",
        17,
    )
    assert fact.tool_name == "Checkov"
    assert fact.tool_version == "3.2.1"
    assert "CANARY_SECRET" not in repr(result)
    assert "ANOTHER_CANARY" not in repr(result)


def test_rule_index_resolves_from_allowlisted_driver_table():
    data = json.loads(make_sarif())
    result = data["runs"][0]["results"][0]
    result.pop("ruleId")
    result["ruleIndex"] = 0
    imported = import_sarif(json.dumps(data))
    assert imported.findings[0].rule_id == "CKV_AWS_1"


def test_rejects_absolute_network_traversal_and_base_uri_paths():
    for uri, base_id in (
        ("/etc/passwd", None),
        ("../../secret.tf", None),
        ("https://example.invalid/a.tf", None),
        ("file:///tmp/a.tf", None),
        ("infra/a.tf", "ROOT"),
    ):
        result = import_sarif(make_sarif(uri=uri, uri_base=base_id))
        assert not result.findings
        assert result.diagnostics


def test_invalid_schema_and_duplicate_keys_fail_closed():
    bad_version = import_sarif('{"version":"1.0.0","runs":[]}')
    duplicate = import_sarif('{"version":"2.1.0","version":"2.1.0","runs":[]}')
    assert bad_version.findings == ()
    assert bad_version.diagnostics == ("unsupported SARIF schema version",)
    assert duplicate.diagnostics == ("duplicate JSON keys are unsupported",)


def test_invalid_metadata_is_not_copied_to_facts():
    data = json.loads(make_sarif())
    data["runs"][0]["tool"]["driver"]["name"] = "Scanner credential CANARY"
    result = import_sarif(json.dumps(data))
    assert result.findings == ()
    assert "CANARY" not in repr(result)


def test_size_and_structure_limits():
    from ozagia.core.iac_findings import MAX_ARTIFACT_BYTES

    oversized = import_sarif(" " * (MAX_ARTIFACT_BYTES + 1))
    assert oversized.diagnostics == ("SARIF artifact exceeds 5 MiB limit",)
    deep = import_sarif('{"version":"2.1.0","runs":[],"extra":' + "[" * 70 + "0" + "]" * 70 + "}")
    assert deep.diagnostics == ("SARIF structure exceeds depth or node limit",)
