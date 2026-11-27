"""Repository-level API references and explicit API mappings."""
from __future__ import annotations

import json

from ozagia.core import api_relationships, federation, repo_relations


def _node(node_id: int, kind: str, key: str, **attrs) -> dict:
    return {"id": node_id, "type": kind, "key": key, "attrs": attrs}


def _edge(src: int, kind: str, tgt: int) -> dict:
    return {"src": src, "type": kind, "tgt": tgt, "attrs": {}}


def _member(alias: str, nodes: list[dict], edges: list[dict]) -> dict:
    return {"alias": alias, "label": alias, "root": f"/work/{alias}",
            "snapshot": {"version": 2, "nodes": nodes, "edges": edges},
            "error": None}


def _operation(node_id: int = 1, **overrides) -> dict:
    attrs = {
        "repo": "contracts", "file": "openapi.json", "operation_id": "listPets",
        "method": "GET", "path": "/pets",
        "source_pointer": "#/paths/~1pets/get",
    }
    attrs.update(overrides)
    return _node(node_id, "ApiOperation",
                 f"api_operation:{attrs.get('repo')}:{attrs.get('file')}"
                 f"#{attrs.get('method')}:{attrs.get('path')}", **attrs)


def _code(node_id: int, repo: str, path: str, symbol: str) -> dict:
    return _node(node_id, "ApiCodeReference",
                 f"api_code_reference:{repo}:{path}#{symbol}",
                 repo=repo, path=path, symbol=symbol)


def _mapping(node_id: int, relation: str, code_id: int, operation_id: int,
             pointer: str, *, inference: bool = False,
             source: str = "registry.json") -> tuple[dict, list[dict]]:
    node = _node(node_id, "ApiMapping",
                 f"api_mapping:{source}#{pointer}",
                 relation=relation,
                 provenance={"source": source, "pointer": pointer,
                             "assertion": "explicit maintainer mapping"},
                 inference=inference)
    return node, [_edge(node_id, relation, code_id),
                  _edge(node_id, "refers_to_api_operation", operation_id)]


def _api_members() -> list[dict]:
    contracts = _member("contracts", [_operation(1)], [])
    service_code = _code(2, "service", "src/server.py", "list_pets")
    service_mapping, service_edges = _mapping(3, "produces_api", 2, 1,
                                              "#/mappings/0")
    service = _member("service", [_operation(1), service_code, service_mapping],
                      service_edges)
    client_code = _code(2, "client", "src/client.py", "fetch_pets")
    client_mapping, client_edges = _mapping(3, "consumes_api", 2, 1,
                                            "#/mappings/1")
    test_code = _code(4, "client", "tests/test_client.py", "test_fetch_pets")
    test_mapping, test_edges = _mapping(5, "tests_api", 4, 1, "#/mappings/2")
    client = _member("client",
                     [_operation(1), client_code, client_mapping,
                      test_code, test_mapping],
                     client_edges + test_edges)
    return [contracts, service, client]


def test_operation_id_is_the_preferred_reference_source():
    report = api_relationships.analyze(_api_members())

    references = report["references"]
    assert len(references) == 1
    reference = references[0]
    assert reference["id"] == "contracts:openapi.json#listPets"
    assert reference["kind"] == "openapi_operation"
    assert reference["authority"] == "contract"
    assert reference["source_pointer"] == "#/paths/~1pets/get"
    assert reference["alternates"] == ["contracts:openapi.json#GET /pets"]
    assert reference["members"] == ["contracts", "service", "client"]
    assert report["source_order"][0] == "openapi_operation"


def test_partial_operation_metadata_merges_without_ambiguity():
    complete = _member("contracts", [_operation(1)], [])
    partial = _member("service", [_operation(1, source_pointer="")], [])

    report = api_relationships.analyze([complete, partial])

    assert report["errors"] == []
    reference = report["references"][0]
    assert reference["ambiguous"] is False
    assert reference["source_pointer"] == "#/paths/~1pets/get"
    assert reference["members"] == ["contracts", "service"]


def test_partial_operation_metadata_merges_in_either_snapshot_order():
    complete = _member("contracts", [_operation(1)], [])
    partial = _member(
        "service", [_operation(1, method="", path="", source_pointer="")], [])

    for members in ([partial, complete], [complete, partial]):
        report = api_relationships.analyze(members)

        assert report["errors"] == []
        reference = report["references"][0]
        assert reference["ambiguous"] is False
        assert (reference["method"], reference["path"]) == ("GET", "/pets")
        assert reference["source_pointer"] == "#/paths/~1pets/get"
        assert reference["alternates"] == ["contracts:openapi.json#GET /pets"]
        assert set(reference["members"]) == {"contracts", "service"}


def test_duplicate_producer_mappings_merge_without_false_ambiguity():
    contracts = _member("contracts", [_operation(1)], [])
    service_code = _code(2, "service", "src/server.py", "list_pets")
    first, first_edges = _mapping(3, "produces_api", 2, 1, "#/mappings/0")
    duplicate, duplicate_edges = _mapping(4, "produces_api", 2, 1,
                                          "#/mappings/0")
    second, second_edges = _mapping(5, "produces_api", 2, 1, "#/mappings/1")
    service = _member(
        "service", [_operation(1), service_code, first, duplicate, second],
        first_edges + duplicate_edges + second_edges)
    client_code = _code(2, "client", "src/client.py", "fetch_pets")
    client_mapping, client_edges = _mapping(3, "consumes_api", 2, 1,
                                            "#/mappings/2")
    client = _member("client", [_operation(1), client_code, client_mapping],
                     client_edges)

    report = api_relationships.analyze([contracts, service, client])

    assert report["state"] == "ok"
    assert report["errors"] == []
    assert report["counts"] == {"references": 1, "mappings": 3, "edges": 2,
                                "unpaired": 0, "errors": 0}
    by_relation = {edge["relation"]: edge for edge in report["edges"]}
    consumes = by_relation["consumes_api"]
    assert (consumes["source"], consumes["target"]) == ("client", "service")
    assert consumes["ambiguous"] is False
    assert len(consumes["provenance"]) == 3
    assert {item["source"] for item in consumes["provenance"]} == {"registry.json"}
    assert sorted(item["pointer"] for item in consumes["provenance"]) == [
        "#/mappings/0", "#/mappings/1", "#/mappings/2"]
    assert len(consumes["code"]) == 2
    implements = by_relation["implements_api"]
    assert (implements["source"], implements["target"]) == (
        "service", "contracts")
    assert implements["ambiguous"] is False


def test_api_edges_stay_out_of_dependency_order_and_reverse_dependencies(tmp_path):
    members = _api_members()
    dependency_report = {"links": [{
        "consumer": "service", "provider": "client", "package": "client-sdk",
        "ecosystem": "python", "requirement": ">=1",
        "provided_version": "1.2.0", "status": "satisfied",
        "source_manifest": "pyproject.toml", "source_line": 10,
        "provider_manifest": "pyproject.toml", "provider_line": 3,
        "optional": False, "group": None,
    }]}

    report = repo_relations.analyze(
        tmp_path, members, dependency_report,
        member_states={member["alias"]: "fresh" for member in members},
        api_report=api_relationships.analyze(members))

    # The API edges client -> service would complete a cycle with the package
    # edge service -> client if integration edges entered the build view.
    assert report["counts"]["api_edges"] == 3
    assert report["counts"]["dependency_edges"] == 1
    assert report["dependencies"] == [{
        "source": "service", "target": "client",
        "origins": ["package_manifest"], "relations": ["depends_on"],
        "status": "satisfied", "provenance": ["declared_dependency"],
    }]
    assert report["cycles"] == []
    assert report["levels"] == {"contracts": 0, "service": 0, "client": 1}
    assert report["order"].index("service") < report["order"].index("client")
    assert report["reverse_dependencies"] == {
        "contracts": [], "service": [], "client": ["service"]}
    assert report["state"] == "ok"


def test_reference_falls_back_to_path_then_code_then_identity():
    path_operation = _operation(1, operation_id="")
    path_only = api_relationships.analyze([
        _member("contracts", [path_operation], [])])
    assert path_only["references"][0]["kind"] == "openapi_path"
    assert path_only["references"][0]["id"] == "contracts:openapi.json#GET /pets"

    identity_operation = _operation(1, operation_id="", repo="", file="")
    identity = api_relationships.analyze([
        _member("service", [identity_operation], [])])
    assert identity["references"][0]["kind"] == "operation_identity"
    assert identity["references"][0]["id"] == "service::GET /pets"

    code = api_relationships.analyze([
        _member("service", [_code(1, "service", "src/server.py", "list_pets")], [])])
    assert code["references"][0]["kind"] == "code_reference"
    assert code["references"][0]["authority"] == "code_mapping"


def test_explicit_mappings_build_repository_api_edges():
    report = api_relationships.analyze(_api_members())

    assert report["state"] == "ok"
    assert report["counts"] == {"references": 1, "mappings": 3, "edges": 3,
                                "unpaired": 0, "errors": 0}
    by_key = {(edge["source"], edge["target"], edge["relation"]): edge
              for edge in report["edges"]}
    assert set(by_key) == {
        ("client", "service", "consumes_api"),
        ("client", "service", "tests_api"),
        ("service", "contracts", "implements_api"),
    }
    consumes = by_key[("client", "service", "consumes_api")]
    assert consumes["reference"] == "contracts:openapi.json#listPets"
    assert consumes["reference_kind"] == "openapi_operation"
    assert consumes["authority"] == "contract"
    assert consumes["ambiguous"] is False
    assert {item["source"] for item in consumes["provenance"]} == {"registry.json"}
    assert sorted(consumes["code"], key=lambda item: item["repo"]) == [
        {"repo": "client", "file": "src/client.py", "symbol": "fetch_pets"},
        {"repo": "service", "file": "src/server.py", "symbol": "list_pets"},
    ]


def test_multiple_producers_are_flagged_ambiguous():
    contracts = _member("contracts", [_operation(1)], [])
    first_code = _code(2, "service-a", "src/a.py", "list_pets")
    first_mapping, first_edges = _mapping(3, "produces_api", 2, 1, "#/mappings/0")
    first = _member("service-a", [_operation(1), first_code, first_mapping],
                    first_edges)
    second_code = _code(2, "service-b", "src/b.py", "list_pets")
    second_mapping, second_edges = _mapping(3, "produces_api", 2, 1,
                                            "#/mappings/1")
    second = _member("service-b", [_operation(1), second_code, second_mapping],
                     second_edges)
    client_code = _code(2, "client", "src/client.py", "fetch_pets")
    client_mapping, client_edges = _mapping(3, "consumes_api", 2, 1,
                                            "#/mappings/2")
    client = _member("client", [_operation(1), client_code, client_mapping],
                     client_edges)

    report = api_relationships.analyze([contracts, first, second, client])

    consumes = [edge for edge in report["edges"]
                if edge["relation"] == "consumes_api"]
    assert {(edge["source"], edge["target"]) for edge in consumes} == {
        ("client", "service-a"), ("client", "service-b")}
    assert all(edge["ambiguous"] is True for edge in consumes)


def test_consumer_without_producer_is_unpaired_not_an_edge():
    contracts = _member("contracts", [_operation(1)], [])
    code = _code(2, "client", "src/client.py", "fetch_pets")
    mapping, edges = _mapping(3, "consumes_api", 2, 1, "#/mappings/0")
    client = _member("client", [_operation(1), code, mapping], edges)

    report = api_relationships.analyze([contracts, client])

    assert report["edges"] == []
    assert report["unpaired"] == [{
        "member": "client", "relation": "consumes_api",
        "reference": "contracts:openapi.json#listPets",
        "reason": "no_producer_mapping",
    }]
    assert report["state"] == "ok"


def test_inferred_or_unresolved_mappings_are_rejected():
    contracts = _member("contracts", [_operation(1)], [])
    code = _code(2, "service", "src/server.py", "list_pets")
    inferred, inferred_edges = _mapping(3, "produces_api", 2, 1,
                                        "#/mappings/0", inference=True)
    unresolved, unresolved_edges = _mapping(4, "produces_api", 2, 99,
                                            "#/mappings/1")
    service = _member("service", [_operation(1), code, inferred, unresolved],
                      inferred_edges + unresolved_edges)

    report = api_relationships.analyze([contracts, service])

    assert report["mappings"] == []
    assert report["edges"] == []
    assert report["state"] == "partial"
    assert any("inference_not_allowed" in error for error in report["errors"])
    assert any("unresolved_reference" in error for error in report["errors"])


def test_mapping_errors_without_operations_make_state_partial():
    inferred = _node(
        1, "ApiMapping", "api_mapping:registry.json#/mappings/0",
        relation="produces_api",
        provenance={"source": "registry.json", "pointer": "#/mappings/0"},
        inference=True)
    orphan = _node(
        2, "ApiMapping", "api_mapping:registry.json#/mappings/1",
        relation="consumes_api",
        provenance={"source": "registry.json", "pointer": "#/mappings/1"},
        inference=False)
    client = _member("client", [inferred, orphan], [])

    report = api_relationships.analyze([client])

    assert report["references"] == []
    assert report["mappings"] == []
    assert report["edges"] == []
    assert report["state"] == "partial"
    assert any("inference_not_allowed" in error for error in report["errors"])
    assert any("unresolved_reference" in error for error in report["errors"])


def test_repo_relations_includes_api_edges_and_references(tmp_path):
    root = tmp_path / "anchor"
    root.mkdir()
    members = _api_members()
    report = repo_relations.analyze(
        root, members,
        member_states={"contracts": "fresh", "service": "fresh",
                       "client": "fresh"},
        api_report=api_relationships.analyze(members))

    assert report["apis"]["state"] == "ok"
    assert report["counts"]["api_edges"] == 3
    assert report["counts"]["api_references"] == 1
    assert {edge["origin"] for edge in report["edges"]} == {"api_mapping"}
    assert report["dependencies"] == []
    assert report["state"] == "ok"


def test_repo_relations_check_reads_snapshots_and_manifest(tmp_path, monkeypatch):
    anchor = tmp_path / "contracts"
    service = tmp_path / "service"
    client = tmp_path / "client"
    for path, member in ((anchor, _api_members()[0]),
                         (service, _api_members()[1]),
                         (client, _api_members()[2])):
        state = path / ".ozagia"
        state.mkdir(parents=True)
        (state / "graph.json").write_text(json.dumps({
            "version": 2, "nodes": member["snapshot"]["nodes"],
            "edges": member["snapshot"]["edges"],
        }), encoding="utf-8")
    (anchor / ".ozagia" / "federation.json").write_text(json.dumps({
        "version": 1,
        "members": [{"path": str(service), "id": "service"},
                    {"path": str(client), "id": "client"}],
        "links": [],
    }), encoding="utf-8")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = repo_relations.check(anchor)

    assert report["members"][0]["id"] == "contracts"
    assert report["apis"]["references"][0]["id"] == "contracts:openapi.json#listPets"
    assert report["counts"]["api_edges"] == 3
