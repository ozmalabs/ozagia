from __future__ import annotations

import json

import pytest

from ozagia.core.cloudformation_graph import parse_cloudformation_source


def test_json_static_references_and_secret_values_are_not_returned() -> None:
    template = {
        "Resources": {
            "Bucket": {"Type": "AWS::S3::Bucket", "Properties": {
                "BucketName": "CANARY_SECRET_BUCKET_NAME"}},
            "Policy": {"Type": "AWS::S3::BucketPolicy", "DependsOn": "Bucket",
                       "Properties": {"Bucket": {"Ref": "Bucket"},
                                      "Other": {"Fn::GetAtt": ["Bucket", "Arn"]},
                                      "Dynamic": {"Fn::If": ["UseBucket", {"Ref": "Bucket"}, "no"]}}},
        },
        "Parameters": {"Env": {"Type": "String", "Default": "CANARY_SECRET_PARAMETER"}},
        "Outputs": {"BucketName": {"Value": {"Ref": "Bucket"}}},
    }
    graph = parse_cloudformation_source(json.dumps(template), "infra/template.json")
    assert {node.logical_id for node in graph.nodes} == {
        "Bucket", "Policy", "Env", "BucketName"}
    assert {edge.relation for edge in graph.edges} >= {
        "depends_on", "ref", "get_att"}
    assert any(ref.reason == "unsupported intrinsic function" for ref in graph.unresolved)
    rendered = json.dumps(graph.to_dict())
    assert "CANARY_SECRET" not in rendered
    assert "AWS::S3::Bucket" in rendered
    assert "CANARY_SECRET_BUCKET_NAME" not in rendered


def test_missing_logical_ids_and_dynamic_refs_are_explicit() -> None:
    graph = parse_cloudformation_source(
        '{"Resources":{"Queue":{"Type":"AWS::SQS::Queue",'
        '"Properties":{"DeadLetterConfig":{"Ref":"Absent"},'
        '"Dynamic":{"Ref":{"Fn::Join":["",["x"]]}}}}}}',
        "template.json")
    assert any(ref.reason == "logical identifier not declared"
               and ref.target == "Absent" for ref in graph.unresolved)
    assert any(ref.reason == "dynamic reference target" for ref in graph.unresolved)


def test_yaml_short_intrinsics_are_supported_without_emitting_literals() -> None:
    pytest.importorskip("yaml")
    source = """AWSTemplateFormatVersion: '2010-09-09'
Parameters:
  Env:
    Type: String
    Default: DO_NOT_EMIT_ME
Resources:
  Queue:
    Type: AWS::SQS::Queue
  Alarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      QueueName: !Ref Queue
      Description: !Sub 'alarm-${Env}-${AWS::Region}'
"""
    graph = parse_cloudformation_source(source, "infra/template.yaml")
    assert {edge.relation for edge in graph.edges} == {"ref", "substitution"}
    assert "DO_NOT_EMIT_ME" not in json.dumps(graph.to_dict())
    assert "alarm-" not in json.dumps(graph.to_dict())


def test_duplicate_keys_and_malformed_templates_are_reported() -> None:
    pytest.importorskip("yaml")
    duplicate = parse_cloudformation_source(
        "Resources:\n  A: {Type: AWS::S3::Bucket}\n  A: {Type: AWS::SQS::Queue}\n",
        "template.yaml")
    assert not duplicate.nodes
    assert "duplicate" in duplicate.diagnostics[0][1]
    malformed = parse_cloudformation_source("Resources: [", "template.yaml")
    assert not malformed.nodes
    assert malformed.diagnostics


def test_invalid_reference_targets_are_not_echoed() -> None:
    canary = "CFN_REF_VALUE_CANARY_731"
    graph = parse_cloudformation_source(json.dumps({
        "Resources": {
            "Bucket": {
                "Type": "AWS::S3::Bucket",
                "Properties": {"Name": {"Ref": canary}},
            },
        },
    }), "template.json")
    assert any(ref.target == "<invalid-logical-id>" for ref in graph.unresolved)
    assert canary not in json.dumps(graph.to_dict())


def test_yaml_aliases_are_rejected() -> None:
    pytest.importorskip("yaml")
    graph = parse_cloudformation_source(
        "base: &base {Type: AWS::S3::Bucket}\nResources: {Bucket: *base}\n",
        "template.yaml")
    assert not graph.nodes
    assert "aliases are unsupported" in graph.diagnostics[0][1]


def test_bounds_and_repository_relative_paths() -> None:
    too_big = parse_cloudformation_source("x" * 2_000_001, "template.yaml")
    assert not too_big.nodes
    assert "size limit" in too_big.diagnostics[0][1]
    with pytest.raises(ValueError, match="repository-relative"):
        parse_cloudformation_source("{}", "../outside.json")
