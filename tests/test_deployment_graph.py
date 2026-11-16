from __future__ import annotations

import json

import pytest

from ozagia.core.deployment_graph import (
    extract_deployment_metadata,
    helm_template_paths,
)


def test_helm_metadata_links_chart_sources_without_emitting_contents() -> None:
    graph = extract_deployment_metadata({
        "charts/payments/Chart.yaml": "name: payments\nversion: 1.2.0\ndependencies:\n  - name: redis\n    repository: https://example.invalid/charts\n",
        "charts/payments/values.yaml": "password: CANARY_SECRET_VALUE\nreplicas: 3\n",
        "charts/payments/templates/deployment.yaml": "apiVersion: v1\nkind: Deployment\nmetadata: {name: '{{ .Release.Name }}'}\n",
    })
    kinds = {node.kind for node in graph.nodes}
    assert kinds == {"helm_chart", "helm_values_file", "helm_template_source"}
    assert {edge.relation for edge in graph.edges} == {
        "chart_values_source", "chart_template_source"}
    assert graph.unresolved[0].reason == "dependency is not resolved or fetched"
    serialized = json.dumps(graph.to_dict())
    assert "CANARY_SECRET_VALUE" not in serialized
    assert "replicas" not in serialized
    assert "Release.Name" not in serialized
    template_edge = next(e for e in graph.edges if e.relation == "chart_template_source")
    assert "not rendered" in template_edge.evidence


def test_helm_template_path_and_go_markers_are_classified_without_rendering():
    files = {
        "chart/Chart.yaml": "name: sample\n",
        "chart/templates/deploy.yaml": "kind: Deployment\nmetadata: {name: '{{ .Values.name }}'}\n",
        "loose/template.yaml": "kind: Deployment\nmetadata: {name: '{% if enabled %}x{% endif %}'}\n",
        "values.yaml": "replicas: 2\n",
        "config.json": '{"template": "{{ literal JSON value }}"}',
    }
    assert helm_template_paths(files) == {
        "chart/templates/deploy.yaml", "loose/template.yaml",
    }
    graph = extract_deployment_metadata(files)
    assert any(node.kind == "helm_template_source"
               and node.path == "chart/templates/deploy.yaml"
               for node in graph.nodes)
    assert not any(node.path == "loose/template.yaml" for node in graph.nodes)
    assert any(edge.relation == "chart_template_source" for edge in graph.edges)


def test_kustomize_local_bases_and_patches_resolve_and_remote_does_not() -> None:
    graph = extract_deployment_metadata({
        "deploy/prod/kustomization.yaml": (
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
            "resources:\n  - ../base\n  - https://example.invalid/repo//base\n"
            "patches:\n  - path: patch.yaml\n"),
        "deploy/base/kustomization.yaml": "resources: [workload.yaml]\n",
        "deploy/base/workload.yaml": "kind: Deployment\nmetadata:\n  name: app\n",
        "deploy/prod/patch.yaml": "spec:\n  replicas: 2\n",
    })
    relations = {edge.relation for edge in graph.edges}
    assert relations == {"kustomize_base_or_resource", "kustomize_patch"}
    assert any(ref.reason == "remote reference not fetched" for ref in graph.unresolved)
    assert not any(ref.target.endswith("workload.yaml") for ref in graph.unresolved)
    assert "replicas" not in json.dumps(graph.to_dict())


def test_templated_and_escaping_paths_stay_unresolved() -> None:
    graph = extract_deployment_metadata({
        "kustomize/kustomization.yaml": (
            "resources:\n  - '{{ .Values.base }}'\n  - ../../outside.yaml\n")})
    assert {ref.reason for ref in graph.unresolved} == {
        "templated path", "path escapes repository"}
    assert graph.edges == ()


def test_duplicate_yaml_keys_are_reported_without_partial_chart_nodes() -> None:
    try:
        import yaml  # noqa: F401
    except ImportError:
        pytest.skip("optional YAML parser is not installed")
    graph = extract_deployment_metadata({
        "chart/Chart.yaml": "name: chart\nname: overwritten\n"})
    assert not graph.nodes
    assert "duplicate" in graph.diagnostics[0][1]


def test_yaml_aliases_are_rejected() -> None:
    pytest.importorskip("yaml")
    graph = extract_deployment_metadata({
        "chart/Chart.yaml": "base: &base {name: chart}\n<<: *base\n"})
    assert not graph.nodes
    assert "aliases are unsupported" in graph.diagnostics[0][1]


def test_limits_and_unsafe_file_paths_are_enforced() -> None:
    graph = extract_deployment_metadata({
        "../outside/Chart.yaml": "name: leak\n",
        "chart/Chart.yaml": "name: too-large\n" + "#x" * 600_000,
    })
    assert not graph.nodes
    assert len(graph.diagnostics) == 2
