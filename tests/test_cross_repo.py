"""Conservative cross-repository checks over explicit federation links."""
from __future__ import annotations

import json
from pathlib import Path

from ozagia.core import cross_repo, federation


def _snapshot(root: Path, name: str, nodes: list[dict]) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True)
    (state / "graph.json").write_text(json.dumps({
        "version": 2,
        "nodes": [{"id": 0, "key": "repo:", "type": "Repo",
                   "attrs": {"name": name, "root": str(root), "head": "head"}},
                  *nodes],
        "edges": [],
    }), encoding="utf-8")


def _node(i: int, key: str, kind: str, attrs: dict) -> dict:
    return {"id": i, "key": key, "type": kind, "attrs": attrs}


def _manifest(root: Path, links: list[dict]) -> None:
    (root / ".ozagia" / "federation.json").write_text(
        json.dumps({"version": 1, "links": links}), encoding="utf-8")


def test_api_identity_and_deployment_comparisons_preserve_provenance(tmp_path, monkeypatch):
    consumer, provider, infra = (tmp_path / n for n in ("consumer", "provider", "infra"))
    for path in (consumer, provider, infra):
        path.mkdir()
    _snapshot(consumer, "consumer", [
        _node(1, "sym:src/client.py::get_pets", "Symbol", {"service": "pets"}),
        _node(2, "api:client", "ApiOperation", {"method": "GET", "path": "/pets"}),
    ])
    _snapshot(provider, "provider", [
        _node(1, "api:provider", "ApiOperation", {"method": "POST", "path": "/pets"}),
    ])
    _snapshot(infra, "infra", [
        _node(1, "infra:deployment", "InfraResource", {"service": "billing"}),
    ])
    _manifest(consumer, [
        {"source": "consumer::api:client", "target": "provider::api:provider",
         "relation": "same_api_operation", "provenance": "contract review"},
        {"source": "consumer::sym:src/client.py::get_pets",
         "target": "infra::infra:deployment", "relation": "deployed_by",
         "provenance": "service catalog"},
    ])
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = cross_repo.check(consumer, roots=[provider, infra])

    assert [c["result"] for c in report["checks"]] == ["fail", "fail"]
    assert report["checks"][0]["comparison"] == {
        "method": {"source": "GET", "target": "POST"},
        "path": {"source": "/pets", "target": "/pets"},
    }
    assert report["checks"][0]["provenance"] == "contract review"
    assert report["checks"][1]["reason"] == "service_identity_mismatch"
    assert report["counts"]["fail"] == 2


def test_api_coverage_is_declared_and_stale_snapshot_is_unknown(tmp_path, monkeypatch):
    code, contract = tmp_path / "code", tmp_path / "contract"
    code.mkdir(); contract.mkdir()
    _snapshot(code, "code", [_node(1, "sym:app.py::serve", "Symbol", {})])
    _snapshot(contract, "contract", [_node(1, "api:get", "ApiOperation",
                                              {"method": "GET", "path": "/x"})])
    _manifest(code, [{"source": "code::sym:app.py::serve",
                      "target": "contract::api:get", "relation": "produces_api",
                      "provenance": "maintainer registry"}])
    monkeypatch.setattr(federation, "_freshness",
                        lambda _: ("stale", "snapshot HEAD differs"))

    result = cross_repo.check(code, roots=[contract])["checks"][0]

    assert result["result"] == "unknown"
    assert result["reason"] == "member_snapshot_not_verified_fresh"
    assert result["provenance"] == "maintainer registry"


def test_wrong_endpoint_types_and_missing_links_are_unknown(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    _snapshot(a, "a", [_node(1, "file:x.py", "File", {})])
    _snapshot(b, "b", [_node(1, "infra:x", "InfraResource", {})])
    _manifest(a, [{"source": "a::file:x.py", "target": "b::infra:x",
                    "relation": "same_api_operation", "provenance": "bad"},
                   {"source": "a::file:missing.py", "target": "b::infra:x",
                    "relation": "deployed_by", "provenance": "missing"}])
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = cross_repo.check(a, roots=[b])

    assert report["checks"][0]["reason"] == "unsupported_endpoint_types"
    assert report["checks"][1]["reason"] == "invalid_declared_link"
    assert report["counts"]["unknown"] == 2
