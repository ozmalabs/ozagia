from __future__ import annotations

import json

import pytest

from ozagia.core.pulumi_graph import parse_pulumi_yaml


def test_static_resource_interpolations_and_no_config_or_secret_values() -> None:
    pytest.importorskip("yaml")
    source = """name: demo
runtime: yaml
config:
  cloud:secret:
    secret: true
    default: CONFIG_SECRET_CANARY
resources:
  network:
    type: aws:ec2/vpc:Vpc
    properties:
      cidrBlock: 10.0.0.0/16
  subnet:
    type: aws:ec2/subnet:Subnet
    properties:
      vpcId: ${network.id}
      availabilityZone: ${config:cloud:zone}
      password: RESOURCE_SECRET_CANARY
outputs:
  subnetId: ${subnet.id}
"""
    graph = parse_pulumi_yaml(source)
    assert {node.logical_name for node in graph.nodes} == {"network", "subnet"}
    assert len(graph.edges) == 1
    assert graph.edges[0].relation == "resource_interpolation"
    assert any(ref.reason == "config, stack, function, or dynamic interpolation"
               for ref in graph.unresolved)
    rendered = json.dumps(graph.to_dict())
    assert "CONFIG_SECRET_CANARY" not in rendered
    assert "RESOURCE_SECRET_CANARY" not in rendered
    assert "10.0.0.0/16" not in rendered
    assert "cloud:zone" not in rendered
    assert "outputs" not in rendered


def test_unknown_resource_and_function_forms_remain_unresolved() -> None:
    pytest.importorskip("yaml")
    graph = parse_pulumi_yaml("""resources:
  app:
    type: kubernetes:apps/v1:Deployment
    properties:
      service: ${missing.status.name}
      derived:
        fn::join:
          - '-'
          - [a, b]
""")
    assert any(ref.reason == "referenced resource is not declared"
               for ref in graph.unresolved)
    assert any(ref.reason == "unsupported function expression"
               for ref in graph.unresolved)
    assert all(ref.target_fingerprint for ref in graph.unresolved)


def test_arbitrary_property_keys_are_not_copied_into_evidence() -> None:
    pytest.importorskip("yaml")
    graph = parse_pulumi_yaml("""resources:
  source:
    type: pkg:mod:Thing
    properties:
      SECRET_PROPERTY_KEY_CANARY: literal
  consumer:
    type: pkg:mod:Thing
    properties:
      SECRET_PROPERTY_KEY_CANARY: ${source.id}
""")
    assert "SECRET_PROPERTY_KEY_CANARY" not in json.dumps(graph.to_dict())


def test_yaml_aliases_are_rejected() -> None:
    pytest.importorskip("yaml")
    graph = parse_pulumi_yaml("base: &base {type: pkg:mod:Thing}\nresources: {app: *base}\n")
    assert not graph.nodes
    assert "aliases are unsupported" in graph.diagnostics[0][1]


def test_invalid_duplicate_or_malformed_yaml_has_diagnostic() -> None:
    pytest.importorskip("yaml")
    duplicate = parse_pulumi_yaml("resources:\n  app: {}\n  app: {}\n")
    assert not duplicate.nodes
    assert "duplicate" in duplicate.diagnostics[0][1]
    malformed = parse_pulumi_yaml("resources: [")
    assert not malformed.nodes
    assert malformed.diagnostics


def test_size_depth_and_path_limits() -> None:
    too_large = parse_pulumi_yaml("#x" * 600_000)
    assert "size limit" in too_large.diagnostics[0][1]
    pytest.importorskip("yaml")
    nested = ("resources:\n  app:\n    type: x:y:z\n    properties:\n"
              + "".join(f"{'      ' * (i + 1)}a{i}:\n" for i in range(70))
              + "".join(f"{'      ' * 71}value{i}: x\n" for i in range(1)))
    deep = parse_pulumi_yaml(nested)
    assert "nesting limit" in deep.diagnostics[0][1]
    with pytest.raises(ValueError, match="repository-relative"):
        parse_pulumi_yaml("resources: {}", "../outside/Pulumi.yaml")
