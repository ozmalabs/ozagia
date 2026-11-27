"""Repository-level relationships across federation members (read-only fixtures)."""
from __future__ import annotations

import json
from pathlib import Path

from ozagia.core import cross_repo, federation, repo_relations
from ozagia.federation_cli import main


def _snapshot(root: Path, name: str) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    (state / "graph.json").write_text(json.dumps({
        "version": 2,
        "nodes": [{"id": 0, "type": "Repo", "key": "repo:",
                   "attrs": {"name": name, "root": str(root), "head": "head"}}],
        "edges": [],
    }), encoding="utf-8")


def _member(root: Path, name: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _snapshot(root, name or root.name)


def _manifest(root: Path, *, members: list[object] | None = None,
              relationships: list[object] | None = None,
              families: list[object] | None = None,
              links: list[object] | None = None) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {"version": 1, "members": members or [],
                               "links": links or []}
    if relationships is not None:
        data["relationships"] = relationships
    if families is not None:
        data["families"] = families
    (state / "federation.json").write_text(json.dumps(data), encoding="utf-8")


def _pyproject(root: Path, name: str, version: str | None,
               dependencies: list[str] | None = None) -> None:
    lines = ["[project]", f'name = "{name}"']
    if version is not None:
        lines.append(f'version = "{version}"')
    if dependencies is not None:
        rendered = ", ".join(json.dumps(item) for item in dependencies)
        lines.append(f"dependencies = [{rendered}]")
    (root / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_declared_relationships_and_families_are_reported(tmp_path, monkeypatch):
    anchor, member = tmp_path / "bigville", tmp_path / "ocelot"
    _member(anchor, "bigville")
    _member(member, "ocelot")
    _manifest(
        anchor,
        members=[{"path": str(member), "id": "ocelot"}],
        relationships=[{
            "source": "bigville", "target": "ocelot", "relation": "uses",
            "provenance": "Bigville imports Ocelot's runtime adapter",
            "evidence": ["bigville/backends/ocelot.py"],
        }],
        families=[{
            "id": "simulation-stack", "label": "Simulation stack",
            "members": ["bigville", "ocelot"],
            "provenance": "reviewed integration map",
        }],
    )
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = repo_relations.check(anchor)

    assert report["state"] == "ok"
    assert report["edges"] == [{
        "source": "bigville", "target": "ocelot", "relation": "uses",
        "kind": "integration", "origin": "declared",
        "provenance": "Bigville imports Ocelot's runtime adapter",
        "evidence": ["bigville/backends/ocelot.py"],
    }]
    assert report["dependencies"] == []
    assert report["families"][0]["members"] == ["bigville", "ocelot"]
    assert {member["id"]: member["families"] for member in report["members"]} == {
        "bigville": ["simulation-stack"], "ocelot": ["simulation-stack"]}


def test_package_dependencies_roll_up_to_repository_edges(tmp_path, monkeypatch):
    consumer, provider = tmp_path / "consumer", tmp_path / "shared"
    _member(consumer, "consumer")
    _member(provider, "shared")
    _pyproject(consumer, "consumer", "1.0.0", ["shared-lib>=1.2"])
    _pyproject(provider, "shared-lib", "1.4.0")
    _manifest(consumer, members=[{"path": str(provider), "id": "shared"}])
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = repo_relations.check(consumer)

    assert report["state"] == "ok"
    edge = report["edges"][0]
    assert (edge["source"], edge["target"]) == ("consumer", "shared")
    assert (edge["relation"], edge["kind"], edge["origin"]) == (
        "depends_on", "dependency", "package_manifest")
    assert edge["status"] == "satisfied"
    assert edge["package_count"] == 1
    assert edge["packages"][0]["package"] == "shared-lib"
    assert edge["packages"][0]["provided_version"] == "1.4.0"
    assert report["dependencies"] == [{
        "source": "consumer", "target": "shared",
        "origins": ["package_manifest"], "relations": ["depends_on"],
        "status": "satisfied", "provenance": ["declared_dependency"],
    }]
    assert report["reverse_dependencies"]["shared"] == ["consumer"]


def test_repository_names_and_snapshots_do_not_create_edges(tmp_path, monkeypatch):
    anchor, sibling = tmp_path / "one", tmp_path / "two"
    _member(anchor, "same")
    _member(sibling, "same")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = repo_relations.check(anchor)

    assert report["state"] == "absent"
    assert report["edges"] == []
    assert report["dependencies"] == []
    assert report["families"] == []
    assert report["cycles"] == []


def test_cycles_and_reverse_dependencies_are_reported(tmp_path, monkeypatch):
    anchor, member = tmp_path / "a", tmp_path / "b"
    _member(anchor, "a")
    _member(member, "b")
    _manifest(anchor, members=[{"path": str(member), "id": "b"}], relationships=[
        {"source": "a", "target": "b", "relation": "depends_on",
         "provenance": "declared a -> b"},
        {"source": "b", "target": "a", "relation": "depends_on",
         "provenance": "declared b -> a"},
    ])
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = repo_relations.check(anchor)

    assert report["cycles"] == [["a", "b"]]
    assert report["order"] == []
    assert report["levels"] == {}
    assert report["reverse_dependencies"] == {"a": ["b"], "b": ["a"]}
    assert len(report["dependencies"]) == 2


def test_acyclic_dependencies_produce_an_order_and_levels(tmp_path, monkeypatch):
    anchor, middle, leaf = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for path, name in ((anchor, "a"), (middle, "b"), (leaf, "c")):
        _member(path, name)
    _manifest(anchor,
              members=[{"path": str(middle), "id": "b"},
                       {"path": str(leaf), "id": "c"}],
              relationships=[
                  {"source": "a", "target": "b", "relation": "depends_on",
                   "provenance": "a uses b"},
                  {"source": "a", "target": "c", "relation": "depends_on",
                   "provenance": "a uses c"},
              ])
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = repo_relations.check(anchor)

    assert report["cycles"] == []
    assert report["order"][0] == "a"
    assert set(report["order"]) == {"a", "b", "c"}
    assert report["levels"]["a"] == 0
    assert report["levels"]["b"] == 1
    assert report["levels"]["c"] == 1
    assert report["reverse_dependencies"]["a"] == []


def test_invalid_declarations_are_reported_and_valid_entries_kept(tmp_path,
                                                                  monkeypatch):
    anchor, member = tmp_path / "a", tmp_path / "b"
    _member(anchor, "a")
    _member(member, "b")
    _manifest(
        anchor,
        members=[{"path": str(member), "id": "b"}],
        relationships=[
            {"source": "missing", "target": "b", "relation": "uses",
             "provenance": "bad source"},
            {"source": "a", "target": "a", "relation": "uses",
             "provenance": "self"},
            {"source": "a", "target": "b", "relation": "uses",
             "provenance": "kept"},
            {"source": "a", "target": "b", "relation": "uses",
             "provenance": "duplicate"},
            {"source": "a", "target": "b", "relation": "uses",
             "provenance": "extra", "unknown": True},
        ],
        families=[
            {"id": "Bad ID", "members": ["a"], "provenance": "bad id"},
            {"id": "ok", "members": ["missing"], "provenance": "no members"},
        ],
    )
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = repo_relations.check(anchor)

    assert report["state"] == "partial"
    assert len(report["edges"]) == 1
    assert report["edges"][0]["provenance"] == "kept"
    assert report["families"] == []
    assert any("not a selected member" in error for error in report["errors"])
    assert any("same member" in error for error in report["errors"])
    assert any("duplicates" in error for error in report["errors"])
    assert any("unknown fields" in error for error in report["errors"])
    assert any("families[0].id" in error for error in report["errors"])


def test_declared_and_package_dependency_edges_merge_origins(tmp_path, monkeypatch):
    consumer, provider = tmp_path / "consumer", tmp_path / "shared"
    _member(consumer, "consumer")
    _member(provider, "shared")
    _pyproject(consumer, "consumer", "1.0.0", ["shared-lib>=2"])
    _pyproject(provider, "shared-lib", "1.4.0")
    _manifest(consumer, members=[{"path": str(provider), "id": "shared"}],
              relationships=[{"source": "consumer", "target": "shared",
                              "relation": "depends_on",
                              "provenance": "declared runtime dependency"}])
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = repo_relations.check(consumer)

    assert len(report["edges"]) == 2
    assert report["dependencies"] == [{
        "source": "consumer", "target": "shared",
        "origins": ["declared", "package_manifest"],
        "relations": ["depends_on"], "status": "mismatch",
        "provenance": ["declared runtime dependency", "declared_dependency"],
    }]
    assert report["state"] == "ok"


def test_manifest_error_without_valid_entries_is_partial(tmp_path, monkeypatch):
    anchor = tmp_path / "anchor"
    _member(anchor, "anchor")
    (anchor / ".ozagia" / "federation.json").write_text(
        '{"version": 1}', encoding="utf-8")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = repo_relations.check(anchor)

    assert report["state"] == "partial"
    assert report["errors"] == ["expected {version: 1, links: [...]} manifest"]


def test_cross_repo_check_includes_repository_relations(tmp_path, monkeypatch):
    anchor, member = tmp_path / "anchor", tmp_path / "member"
    _member(anchor, "anchor")
    _member(member, "member")
    _manifest(anchor, members=[{"path": str(member), "id": "member"}],
              relationships=[{"source": "anchor", "target": "member",
                              "relation": "forked_from",
                              "provenance": "declared lineage"}])
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = cross_repo.check(anchor)

    relations = report["repository_relations"]
    assert relations["state"] == "ok"
    assert relations["edges"][0]["relation"] == "forked_from"
    assert relations["edges"][0]["kind"] == "lineage"
    assert cross_repo.relations(anchor)["edges"] == relations["edges"]


def test_cli_relations_command_prints_the_report(tmp_path, monkeypatch, capsys):
    anchor, member = tmp_path / "anchor", tmp_path / "member"
    _member(anchor, "anchor")
    _member(member, "member")
    _manifest(anchor, members=[{"path": str(member), "id": "member"}],
              relationships=[{"source": "anchor", "target": "member",
                              "relation": "documents",
                              "provenance": "declared docs link"}])
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    assert main(["relations", "--root", str(anchor), "--no-sync"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["state"] == "ok"
    assert output["edges"][0]["relation"] == "documents"
    assert output["edges"][0]["kind"] == "documentation"
    assert output["apis"]["state"] == "absent"


def test_mutual_integration_edges_do_not_create_a_dependency_cycle(tmp_path,
                                                                    monkeypatch):
    bigville, ocelot = tmp_path / "bigville", tmp_path / "ocelot"
    _member(bigville, "bigville")
    _member(ocelot, "ocelot")
    _manifest(
        bigville,
        members=[{"path": str(ocelot), "id": "ocelot"}],
        relationships=[
            {"source": "bigville", "target": "ocelot", "relation": "uses",
             "provenance": "Bigville's ocelot backend loads Ocelot's "
                           "Bigville resident provider",
             "evidence": ["bigville/backends/ocelot.py",
                          "tests/test_bigville_ocelot_fresh_process.py"]},
            {"source": "ocelot", "target": "bigville", "relation": "plugin_for",
             "provenance": "Ocelot supplies the Bigville world adapter that "
                           "backend loads",
             "evidence": ["worlds/bigville_ocelot_adapter.py",
                          "docs/features/bigville.md"]},
        ],
        families=[{
            "id": "simulation-stack",
            "label": "Bigville/Ocelot simulation stack",
            "members": ["bigville", "ocelot"],
            "provenance": "reviewed integration map; declared bridge, "
                          "not a build dependency",
        }],
    )
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = repo_relations.check(bigville)

    assert report["state"] == "ok"
    assert [(edge["source"], edge["target"], edge["relation"])
            for edge in report["edges"]] == [
        ("bigville", "ocelot", "uses"),
        ("ocelot", "bigville", "plugin_for"),
    ]
    assert {edge["kind"] for edge in report["edges"]} == {"integration"}
    assert report["dependencies"] == []
    assert report["cycles"] == []
    assert set(report["order"]) == {"bigville", "ocelot"}
    assert report["levels"] == {"bigville": 0, "ocelot": 0}
    assert report["reverse_dependencies"] == {"bigville": [], "ocelot": []}
    assert report["families"][0]["members"] == ["bigville", "ocelot"]


def test_explicit_selection_that_omits_an_endpoint_is_an_error(tmp_path,
                                                               monkeypatch):
    bigville, ocelot = tmp_path / "bigville", tmp_path / "ocelot"
    _member(bigville, "bigville")
    _member(ocelot, "ocelot")
    _manifest(
        bigville,
        members=[{"path": str(ocelot), "id": "ocelot-runtime"}],
        relationships=[{
            "source": "bigville", "target": "ocelot-runtime",
            "relation": "uses", "provenance": "declared adapter use",
        }],
    )
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    # Explicit roots ignore manifest ids, so selecting the checkout derives
    # its alias from the snapshot name instead of the declared id.
    selected = repo_relations.check(bigville, roots=[ocelot])
    assert selected["state"] == "partial"
    assert selected["edges"] == []
    assert selected["dependencies"] == []
    assert selected["errors"] == [
        "relationships[0].target is not a selected member: ocelot-runtime"]

    # Omitting the member entirely produces the same reported error.
    omitted = repo_relations.check(bigville, roots=[])
    assert omitted["state"] == "partial"
    assert omitted["edges"] == []
    assert omitted["dependencies"] == []
    assert omitted["errors"] == [
        "relationships[0].target is not a selected member: ocelot-runtime"]
    assert {member["id"] for member in omitted["members"]} == {"bigville"}
