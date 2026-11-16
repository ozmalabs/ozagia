import json

import pytest

from ozagia.core.kubernetes_graph import KubernetesParseError, merge_kubernetes_graphs, parse_kubernetes_source


def test_json_graph_is_namespaced_and_does_not_retain_secret_payloads() -> None:
    canary = "DO_NOT_INDEX_SECRET_CANARY"
    source = json.dumps([
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "creds", "namespace": "a"}, "data": {"password": canary}},
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "web", "namespace": "a"}, "spec": {"template": {"metadata": {"labels": {"app": "web", "sensitive-looking": canary}}, "spec": {"containers": [{"envFrom": [{"secretRef": {"name": "creds"}}, {"configMapRef": {"name": "missing"}}]}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "front", "namespace": "a"}, "spec": {"selector": {"app": "web"}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "foreign", "namespace": "b"}, "spec": {"selector": {"app": "web"}}},
    ])
    parsed = parse_kubernetes_source(source, "cluster/deploy.json")
    graph = merge_kubernetes_graphs([parsed]).to_dict()
    assert graph["source_path"] == "cluster/deploy.json"
    assert len(graph["nodes"]) == 4
    assert any(e["relation"] == "workload_service_selector" and e["provenance"] == "inferred" for e in graph["edges"])
    assert not any(e["target"] == next(n["key"] for n in graph["nodes"] if n["name"] == "foreign") for e in graph["edges"])
    assert any(r["target_name"] == "missing" and r["namespace"] == "a" for r in graph["unresolved"])
    assert canary not in json.dumps(graph)
    assert all("pod_labels" not in node and "service_selector" not in node for node in graph["nodes"])


def test_multidoc_yaml_and_duplicate_key_rejection() -> None:
    pytest.importorskip("yaml")
    graph = parse_kubernetes_source("""apiVersion: v1
kind: ServiceAccount
metadata:
  name: agent
  namespace: test
---
apiVersion: v1
kind: Pod
metadata:
  name: worker
  namespace: test
spec:
  serviceAccountName: agent
""", "pods.yaml").to_dict()
    assert len(graph["nodes"]) == 2
    assert graph["edges"][0]["relation"] == "workload_service_account"
    with pytest.raises(KubernetesParseError, match="duplicate"):
        parse_kubernetes_source("apiVersion: v1\napiVersion: v2\nkind: Pod\nmetadata: {name: p}\n", "bad.yaml")


def test_aliases_and_document_limits() -> None:
    pytest.importorskip("yaml")
    with pytest.raises(KubernetesParseError, match="document limit"):
        parse_kubernetes_source("---\nkind: Pod\n---\nkind: Pod\n", "x.yaml", max_documents=1)
    bomb = "a: &a [x]\nb: &b [*a, *a]\nc: &c [*b, *b]\nd: &d [*c, *c]\n"
    cycle = "apiVersion: v1\nkind: Pod\nmetadata: {name: p}\nspec: &loop [*loop]\n"
    for yaml_source in (bomb, cycle):
        with pytest.raises(KubernetesParseError, match="aliases are unsupported"):
            parse_kubernetes_source(yaml_source, "aliases.yaml")


def test_cross_file_references_and_unknown_namespace_stay_conservative() -> None:
    workload = parse_kubernetes_source(json.dumps({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api", "namespace": "prod"},
        "spec": {"template": {"metadata": {"labels": {"app": "api"}}, "spec": {"serviceAccountName": "runner"}}},
    }), "workload.json")
    service = parse_kubernetes_source(json.dumps({
        "apiVersion": "v1", "kind": "Service", "metadata": {"name": "api", "namespace": "prod"}, "spec": {"selector": {"app": "api"}},
    }), "service.json")
    account = parse_kubernetes_source(json.dumps({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "runner", "namespace": "prod"}}), "sa.json")
    merged = merge_kubernetes_graphs([workload, service, account]).to_dict()
    assert {edge["relation"] for edge in merged["edges"]} == {"workload_service_selector", "workload_service_account"}

    no_namespace = parse_kubernetes_source(json.dumps({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api"},
        "spec": {"template": {"spec": {"serviceAccountName": "runner"}}},
    }), "unknown.json")
    assert no_namespace.unresolved[0].evidence == "namespace unknown"


def test_rbac_role_and_subject_references_are_scoped() -> None:
    role = parse_kubernetes_source(json.dumps({
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
        "metadata": {"name": "reader", "namespace": "prod"}}), "role.json")
    cluster_role = parse_kubernetes_source(json.dumps({
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
        "metadata": {"name": "cluster-reader"}}), "cluster-role.json")
    account = parse_kubernetes_source(json.dumps({
        "apiVersion": "v1", "kind": "ServiceAccount",
        "metadata": {"name": "app", "namespace": "prod"}}), "sa.json")
    binding = parse_kubernetes_source(json.dumps({
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
        "metadata": {"name": "reader-binding", "namespace": "prod"},
        "subjects": [{"kind": "ServiceAccount", "name": "app", "namespace": "prod"},
                     {"kind": "User", "name": "someone"}],
        "roleRef": {"kind": "Role", "name": "reader"}}), "binding.json")
    cluster_binding = parse_kubernetes_source(json.dumps({
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
        "metadata": {"name": "cluster-binding"},
        "subjects": [{"kind": "ServiceAccount", "name": "app", "namespace": "prod"}],
        "roleRef": {"kind": "ClusterRole", "name": "cluster-reader"}}),
        "cluster-binding.json")
    merged = merge_kubernetes_graphs(
        [role, cluster_role, account, binding, cluster_binding]).to_dict()
    relations = {edge["relation"] for edge in merged["edges"]}
    assert relations == {"rbac_role_ref", "rbac_subject"}
    assert sum(edge["relation"] == "rbac_role_ref" for edge in merged["edges"]) == 2
    assert sum(edge["relation"] == "rbac_subject" for edge in merged["edges"]) == 2
    assert any(ref.evidence == "external subject kind"
               for ref in merge_kubernetes_graphs([binding]).unresolved)
    assert not any(node["name"] == "someone" for node in merged["nodes"])
    assert "someone" not in json.dumps(merged)
    assert "someone" not in json.dumps(binding.to_dict())
    assert {edge["scope"] for edge in merged["edges"]} == {
        "namespace:prod", "cluster"}


def test_rbac_unknown_namespace_subject_stays_unresolved() -> None:
    binding = parse_kubernetes_source(json.dumps({
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
        "metadata": {"name": "binding", "namespace": "prod"},
        "subjects": [{"kind": "ServiceAccount", "name": "app"}],
        "roleRef": {"kind": "Role", "name": "reader"}}), "binding.json")
    assert any(ref.relation == "rbac_subject" and ref.evidence == "namespace unknown"
               for ref in binding.unresolved)


def test_duplicate_cross_file_references_are_ambiguous() -> None:
    workload = parse_kubernetes_source(json.dumps({
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "api", "namespace": "prod"},
        "spec": {"template": {"spec": {"serviceAccountName": "runner"}}},
    }), "workload.json")
    first = parse_kubernetes_source(json.dumps({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "runner", "namespace": "prod"}}), "sa1.json")
    second = parse_kubernetes_source(json.dumps({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "runner", "namespace": "prod"}}), "sa2.json")
    merged = merge_kubernetes_graphs([workload, first, second])
    assert not merged.edges
    assert any(ref.evidence == "ambiguous" for ref in merged.unresolved)
