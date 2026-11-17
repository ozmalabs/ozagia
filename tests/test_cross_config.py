from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ozagia.core import cross_config, federation


def _snapshot(root: Path, repo: str, nodes: list[dict]) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    all_nodes = [{"id": 0, "type": "Repo", "key": "repo:",
                  "attrs": {"name": repo, "root": str(root), "head": "same"}}, *nodes]
    (state / "graph.json").write_text(json.dumps({
        "version": 2, "nodes": all_nodes, "edges": [],
    }), encoding="utf-8")


def _fixture(tmp_path: Path, *, code: str = "value = os.environ['TOKEN']\n",
             env: list[dict] | None = None, extra_containers: list[dict] | None = None):
    code_root = tmp_path / "app"
    iac_root = tmp_path / "deploy"
    code_root.mkdir(parents=True)
    iac_root.mkdir(parents=True)
    (code_root / "app.py").write_text(code, encoding="utf-8")
    document = {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": "app", "namespace": "prod"},
                "spec": {"template": {"spec": {"containers": [
                    {"name": "app", "env": env if env is not None else [
                        {"name": "TOKEN", "value": "secret-value"}]},
                    *(extra_containers or []),
                ]}}}}
    (iac_root / "app.json").write_text(json.dumps(document), encoding="utf-8")
    _snapshot(code_root, "app", [{"id": 1, "type": "File", "key": "file:app.py",
                                   "attrs": {"path": "app.py"}}])
    _snapshot(iac_root, "deploy", [{"id": 1, "type": "InfraResource",
                                     "key": "infra:deployment-app",
                                     "attrs": {"family": "kubernetes", "kind": "Deployment",
                                               "name": "app", "namespace": "prod",
                                               "path": "app.json"}}])
    (code_root / ".ozagia" / "federation.json").write_text(json.dumps({
        "version": 1, "members": [str(iac_root)], "links": [{
            "source": "app::file:app.py", "target": "deploy::infra:deployment-app",
            "relation": "configured_by", "provenance": "reviewed deployment map",
        }],
    }), encoding="utf-8")
    return code_root, iac_root


def _fresh_git(monkeypatch):
    monkeypatch.setattr(federation.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="same\n"))


def test_matches_literal_required_environment_without_returning_values(tmp_path, monkeypatch):
    root, _ = _fixture(tmp_path)
    _fresh_git(monkeypatch)

    result = cross_config.check(root)

    assert result["status"] == "unknown"
    item = result["results"][0]
    assert item["status"] == "observed_match"
    assert item["required_variables"] == ["TOKEN"]
    assert item["declared_variables"] == ["TOKEN"]
    assert item["freshness"] == {"source": "unknown", "target": "unknown"}
    assert "secret-value" not in json.dumps(result)
    assert "clean" not in {result["status"], item["status"]}


def test_reports_observed_missing_but_not_deployment_failure(tmp_path, monkeypatch):
    root, _ = _fixture(tmp_path, env=[])
    _fresh_git(monkeypatch)

    result = cross_config.check(root)

    assert result["status"] == "unknown"
    assert result["results"][0]["status"] == "observed_missing"
    assert result["results"][0]["missing_variables"] == ["TOKEN"]
    assert "deployed" in result["disclaimer"]


def test_optional_getenv_is_not_required_and_remains_unknown(tmp_path, monkeypatch):
    root, _ = _fixture(tmp_path, code="value = os.getenv('TOKEN')\n", env=[])
    _fresh_git(monkeypatch)

    result = cross_config.check(root)

    assert result["results"][0]["required_variables"] == []
    assert result["results"][0]["status"] == "unknown"
    assert "no required literal" in result["results"][0]["reasons"][0]


def test_dynamic_code_envfrom_and_multiple_containers_are_unknown(tmp_path, monkeypatch):
    root, _ = _fixture(tmp_path, code="value = os.environ[key]\n")
    _fresh_git(monkeypatch)
    result = cross_config.check(root)
    assert result["results"][0]["status"] == "unknown"
    assert "dynamic os.environ subscript" in result["results"][0]["reasons"]

    root2, _ = _fixture(tmp_path / "second", env=[{"name": "TOKEN", "value": "x"},
                                                     {"value": "other"}])
    _fresh_git(monkeypatch)
    unknown = cross_config.check(root2)
    assert unknown["results"][0]["status"] == "unknown"
    assert "dynamic or malformed name" in unknown["results"][0]["reasons"][0]

    root3, _ = _fixture(tmp_path / "third", extra_containers=[{"name": "sidecar", "env": []}])
    _fresh_git(monkeypatch)
    ambiguous = cross_config.check(root3)
    assert "multiple or missing containers" in ambiguous["results"][0]["reasons"][0]


def test_envfrom_unknown_and_symlinked_source_rejected(tmp_path, monkeypatch):
    root, deploy = _fixture(tmp_path)
    doc = json.loads((deploy / "app.json").read_text())
    doc["spec"]["template"]["spec"]["containers"][0]["envFrom"] = [
        {"secretRef": {"name": "secrets"}}]
    (deploy / "app.json").write_text(json.dumps(doc))
    _fresh_git(monkeypatch)
    result = cross_config.check(root)
    assert "envFrom may provide" in result["results"][0]["reasons"][0]

    (root / "real.py").write_text("value = os.environ['TOKEN']\n")
    (root / "app.py").unlink()
    (root / "app.py").symlink_to(root / "real.py")
    result = cross_config.check(root)
    assert result["results"][0]["status"] == "unknown"
    assert "unsafe" in result["results"][0]["reasons"][0]


def test_stale_snapshot_and_invalid_link_facts_stay_unknown(tmp_path, monkeypatch):
    root, _ = _fixture(tmp_path)
    monkeypatch.setattr(federation.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="changed\n"))

    result = cross_config.check(root)

    assert result["status"] == "unknown"
    assert result["results"][0]["status"] == "observed_match"
    assert result["results"][0]["freshness"]["source"] == "stale"
    assert "does not verify" in result["disclaimer"]


def test_yaml_multi_document_uses_graph_source_pointer(tmp_path, monkeypatch):
    yaml = pytest.importorskip("yaml")
    root, deploy = _fixture(tmp_path)
    deployment = {"apiVersion": "apps/v1", "kind": "Deployment",
                  "metadata": {"name": "app", "namespace": "prod"},
                  "spec": {"template": {"spec": {"containers": [
                      {"name": "app", "env": []}]}}}}
    (deploy / "app.json").unlink()
    (deploy / "app.yaml").write_text(yaml.safe_dump_all([
        {**deployment, "spec": {"template": {"spec": {"containers": [
            {"name": "app", "env": [{"name": "TOKEN", "value": "first"}]}]}}}},
        deployment,
    ]), encoding="utf-8")
    graph_path = deploy / ".ozagia" / "graph.json"
    graph = json.loads(graph_path.read_text())
    graph["nodes"][1]["attrs"].update({"path": "app.yaml", "pointer": "/documents/1"})
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    _fresh_git(monkeypatch)

    result = cross_config.check(root)

    assert result["results"][0]["status"] == "observed_missing"
