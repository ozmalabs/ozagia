import json

import pytest

from ozagia.core.gitops_graph import GitOpsParseError, parse_gitops_source


def test_argo_application_extracts_metadata_and_strips_url_credentials() -> None:
    secret = "URL_PASSWORD_CANARY"
    source = json.dumps({
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": "store", "namespace": "argocd", "labels": {"arbitrary": secret}},
        "spec": {
            "source": {"repoURL": f"https://user:{secret}@git.example/{secret}?token={secret}", "path": "deploy/store", "targetRevision": secret},
            "destination": {"server": "https://cluster.example", "namespace": "store"},
            "values": {"password": secret},
        },
    })
    graph = parse_gitops_source(source, "apps/store.json").to_dict()
    node = graph["nodes"][0]
    assert node["source_url"] == "https://git.example"
    assert node["target_revision"] is None
    assert node["destination_namespace"] == "store"
    assert any(ref["relation"] == "argo_source" for ref in graph["unresolved"])
    assert secret not in json.dumps(graph)


def test_flux_explicit_source_reference_resolves_only_declared_input() -> None:
    source = """apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: platform
  namespace: flux-system
spec:
  url: https://git.example/platform.git
  ref:
    branch: main
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: apps
  namespace: flux-system
spec:
  path: ./clusters/prod
  sourceRef:
    kind: GitRepository
    name: platform
"""
    graph = parse_gitops_source(source, "flux.yaml").to_dict()
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["provenance"] == "declared"
    assert any(ref["relation"] == "remote_source" for ref in graph["unresolved"])


def test_missing_flux_source_and_parser_bounds() -> None:
    source = json.dumps({
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
        "metadata": {"name": "apps", "namespace": "flux-system"},
        "spec": {"sourceRef": {"kind": "GitRepository", "name": "not-present"}},
    })
    graph = parse_gitops_source(source, "missing.json")
    assert graph.unresolved[0].evidence == "missing source declaration"
    with pytest.raises(GitOpsParseError, match="size limit"):
        parse_gitops_source("{}", "x.json", max_source_bytes=1)


def test_yaml_duplicate_keys_rejected() -> None:
    pytest.importorskip("yaml")
    with pytest.raises(GitOpsParseError, match="duplicate"):
        parse_gitops_source("kind: Application\nkind: Flux\n", "bad.yaml")


def test_yaml_alias_bomb_and_cycle_are_rejected_before_construction() -> None:
    pytest.importorskip("yaml")
    bomb = "a: &a [x]\nb: &b [*a, *a]\nc: &c [*b, *b]\nd: &d [*c, *c]\n"
    cycle = "apiVersion: v1\nkind: Application\nmetadata: {name: app}\nspec: &loop [*loop]\n"
    for source in (bomb, cycle):
        with pytest.raises(GitOpsParseError, match="aliases are unsupported"):
            parse_gitops_source(source, "aliases.yaml")
