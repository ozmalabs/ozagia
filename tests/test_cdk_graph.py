from __future__ import annotations

import json

import pytest

from ozagia.core.cdk_graph import CdkAssemblyError, read_cdk_assembly


def _manifest(**artifacts: object) -> str:
    return json.dumps({"version": "30.0.0", "artifacts": artifacts})


def test_reads_only_supplied_stack_template_and_dependency_metadata() -> None:
    manifest = _manifest(
        App={"type": "aws:cdk:tree", "dependencies": []},
        Network={"type": "aws:cloudformation:stack", "dependencies": ["App"],
                 "properties": {"templateFile": "assembly/Network.template.json",
                                "environment": "aws://123456789012/us-east-1",
                                "assumeRoleArn": "arn:secret"}},
        Missing={"type": "aws:cloudformation:stack", "properties": {
            "templateFile": "assembly/missing.json"}},
    )
    result = read_cdk_assembly(manifest, templates={
        "assembly/Network.template.json": json.dumps({"Resources": {
            "Vpc": {"Type": "AWS::EC2::VPC", "Properties": {
                "CidrBlock": "10.0.0.0/16", "Tag": "SECRET_CANARY"}},
            "Subnet": {"Type": "AWS::EC2::Subnet", "Properties": {
                "VpcId": {"Ref": "Vpc"}}}}}),
    })
    assert {item.artifact_id for item in result.artifacts} == {"App", "Network", "Missing"}
    assert any(edge.relation == "artifact_dependency" for edge in result.edges)
    assert any(edge.relation == "stack_template" for edge in result.edges)
    assert {resource.logical_id for resource in result.resources} == {"Vpc", "Subnet"}
    assert result.template_sources[0].path == "assembly/Network.template.json"
    assert any(ref.reason == "template was not supplied to reader"
               for ref in result.unresolved)
    rendered = json.dumps(result.to_dict())
    for private in ("123456789012", "us-east-1", "arn:secret", "10.0.0.0/16",
                    "SECRET_CANARY", "environment", "assumeRoleArn"):
        assert private not in rendered


def test_rejects_unknown_schema_and_manifest_shape() -> None:
    with pytest.raises(CdkAssemblyError, match="unsupported assembly schema"):
        read_cdk_assembly(json.dumps({"version": "31.0.0", "artifacts": {}}))
    with pytest.raises(CdkAssemblyError, match="semantic"):
        read_cdk_assembly(json.dumps({"version": "30", "artifacts": {}}))
    with pytest.raises(CdkAssemblyError, match="artifacts"):
        read_cdk_assembly(json.dumps({"version": "30.0.0", "artifacts": []}))


def test_rejects_traversal_and_does_not_read_unprovided_template() -> None:
    result = read_cdk_assembly(_manifest(
        Unsafe={"type": "aws:cloudformation:stack", "properties": {
            "templateFile": "../../secrets.json"}},
        Local={"type": "aws:cloudformation:stack", "properties": {
            "templateFile": "template.json"}},
    ))
    assert len(result.unresolved) == 2
    assert all("safe" in ref.reason or "not supplied" in ref.reason
               for ref in result.unresolved)
    assert not result.resources


def test_unknown_artifacts_paths_and_duplicate_json_keys_are_safe() -> None:
    result = read_cdk_assembly(_manifest(
        Future={"type": "future:secret-bearing-type", "properties": {"token": "CANARY"}},
        Account123456789012={"type": "aws:cloudformation:stack", "properties": {
            "templateFile": "template.json"}},
    ))
    assert not result.artifacts
    assert "CANARY" not in json.dumps(result.to_dict())
    with pytest.raises(CdkAssemblyError, match="duplicate"):
        read_cdk_assembly('{"version":"30.0.0","artifacts":{},"artifacts":{}}')


def test_manifest_and_template_size_limits() -> None:
    with pytest.raises(CdkAssemblyError, match="size limit"):
        read_cdk_assembly(" " * 2_000_001)
    result = read_cdk_assembly(_manifest(Stack={
        "type": "aws:cloudformation:stack",
        "properties": {"templateFile": "template.json"}}),
        templates={"template.json": " " * 2_000_001})
    assert not result.resources
    assert any("size limit" in message for message in result.diagnostics)
    many = read_cdk_assembly(
        json.dumps({"version": "30.0.0", "artifacts": {}}),
        templates={f"t{index}.json": "{}" for index in range(257)})
    assert any("count limit" in message for message in many.diagnostics)
