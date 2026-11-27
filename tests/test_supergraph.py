"""Persisted relationship supergraph: determinism, freshness and boundaries."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ozagia.core import cross_repo, federation, supergraph
from ozagia.federation_cli import main


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    monkeypatch.delenv("OZAGIA_STATE_DIR", raising=False)
    monkeypatch.delenv("OZAGIA_ROOT", raising=False)


def _snapshot_bytes(root: Path, name: str, summary: str = "source") -> str:
    return json.dumps({
        "version": 2,
        "nodes": [
            {"id": 0, "type": "Repo", "key": "repo:",
             "attrs": {"name": name, "root": str(root), "head": "head"}},
            {"id": 1, "type": "File", "key": "file:src/app.py",
             "attrs": {"name": "app.py", "summary": summary}},
        ],
        "edges": [],
    })


def _snapshot(root: Path, name: str, summary: str = "source") -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    (state / "graph.json").write_text(_snapshot_bytes(root, name, summary),
                                      encoding="utf-8")


def _federation(member: Path, *, relationships: list[object] | None = None,
                families: list[object] | None = None) -> dict[str, object]:
    data: dict[str, object] = {
        "version": 1,
        "members": [{"path": str(member), "id": "member"}],
        "links": [],
    }
    if relationships is not None:
        data["relationships"] = relationships
    if families is not None:
        data["families"] = families
    return data


def _manifest(root: Path, member: Path, *, relationships: list[object] | None = None,
              families: list[object] | None = None) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True, exist_ok=True)
    (state / "federation.json").write_text(
        json.dumps(_federation(member, relationships=relationships,
                               families=families)), encoding="utf-8")


def _workspace(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    anchor, member = tmp_path / "anchor", tmp_path / "member"
    anchor.mkdir()
    member.mkdir()
    _snapshot(anchor, "anchor")
    _snapshot(member, "member")
    _manifest(anchor, member)
    monkeypatch.setattr(federation, "_freshness", lambda item: ("fresh", None))
    return anchor, member


def _files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if path.is_file()}


def test_build_is_deterministic_and_write_read_round_trips(tmp_path, monkeypatch):
    anchor, member = _workspace(tmp_path, monkeypatch)

    first = supergraph.build(anchor)
    second = supergraph.build(anchor)
    assert first["schema"] == supergraph.SCHEMA
    assert first["root"] == str(anchor)
    assert first["content_hash"] == second["content_hash"]
    assert first["source_hashes"] == second["source_hashes"]
    assert first["artifact"]["members"] == 2
    assert first["artifact"]["limit_bytes"] == supergraph.MAX_ARTIFACT_BYTES
    assert set(first["source_hashes"]) == {"anchor", "member"}
    assert all(len(entry["snapshot"]) == 64
               for entry in first["source_hashes"].values())

    written = supergraph.write(anchor)
    path = anchor / ".ozagia" / supergraph.DEFAULT_NAME
    assert written["path"] == str(path)
    assert written["state"] == "fresh"
    assert path.is_file()

    cached = supergraph.read(anchor)
    assert cached["state"] == "fresh"
    assert cached["changed"] == []
    assert cached["generated_at"]
    assert cached["graph"] == first["graph"]
    assert cached["content_hash"] == first["content_hash"]
    assert cached["counts"] == first["counts"]
    assert cached["artifact"]["path"] == str(path)
    assert cached["artifact"]["selection"] == {"mode": "manifest", "roots": []}

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["graph"] == first["graph"]
    assert "nodes" not in stored["graph"]


def test_persisted_graph_equals_the_live_relations_body(tmp_path, monkeypatch):
    anchor, _ = _workspace(tmp_path, monkeypatch)
    supergraph.write(anchor)

    live = cross_repo.relations(anchor)
    cached = supergraph.read(anchor)

    assert cached["graph"] == live


def test_member_snapshot_change_makes_the_artifact_stale(tmp_path, monkeypatch):
    anchor, member = _workspace(tmp_path, monkeypatch)
    supergraph.write(anchor)

    _snapshot(member, "member", summary="changed")
    cached = supergraph.read(anchor)

    assert cached["state"] == "stale"
    assert cached["changed"] == ["member"]
    assert cached["changed_sources"] == {"member": ["snapshot"]}
    assert cached["reason"] == "member snapshot or federation manifest changed"
    assert "federation build" in cached["rebuild"]


def test_federation_manifest_change_makes_the_artifact_stale(tmp_path, monkeypatch):
    anchor, member = _workspace(tmp_path, monkeypatch)
    supergraph.write(anchor)

    _manifest(anchor, member, relationships=[{
        "source": "anchor", "target": "member", "relation": "uses",
        "provenance": "explicit fixture integration",
    }])
    cached = supergraph.read(anchor)

    assert cached["state"] == "stale"
    assert cached["changed"] == ["anchor", "member"]
    assert all(fields == ["manifest"] for fields in cached["changed_sources"].values())


def test_absent_and_invalid_artifacts_are_reported_without_crashing(tmp_path, monkeypatch):
    anchor, _ = _workspace(tmp_path, monkeypatch)

    missing = supergraph.read(anchor)
    assert missing["ok"] is True
    assert missing["state"] == "absent"
    assert "graph" not in missing
    assert "ozagia federation build" in missing["rebuild"]
    assert f"--root {anchor}" in missing["rebuild"]

    supergraph.write(anchor)
    path = anchor / ".ozagia" / supergraph.DEFAULT_NAME
    path.write_text("not json", encoding="utf-8")
    corrupt = supergraph.read(anchor)
    assert corrupt["state"] == "invalid"
    assert corrupt["reason"].startswith("invalid supergraph JSON")
    assert "graph" not in corrupt

    path.write_text(json.dumps({"schema": supergraph.SCHEMA}), encoding="utf-8")
    incomplete = supergraph.read(anchor)
    assert incomplete["state"] == "invalid"
    assert "missing required keys" in incomplete["reason"]

    supergraph.write(anchor)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["content_hash"] = "0" * 64
    path.write_text(json.dumps(data), encoding="utf-8")
    mismatched = supergraph.read(anchor)
    assert mismatched["state"] == "invalid"
    assert mismatched["reason"] == "content hash does not match the graph body"


def test_oversized_artifact_is_reported_as_invalid(tmp_path, monkeypatch):
    anchor, _ = _workspace(tmp_path, monkeypatch)
    supergraph.write(anchor)
    monkeypatch.setattr(supergraph, "MAX_ARTIFACT_BYTES", 64)

    cached = supergraph.read(anchor)

    assert cached["state"] == "invalid"
    assert "exceeds the 64 byte bound" in cached["reason"]


def test_refresh_builds_when_the_artifact_is_absent(tmp_path, monkeypatch):
    anchor, _ = _workspace(tmp_path, monkeypatch)

    result = supergraph.refresh(anchor)

    assert result["cache"] == "miss"
    assert result["rebuilt"] is True
    assert result["previous_state"] == "absent"
    assert result["state"] == "fresh"
    assert Path(result["path"]).is_file()
    assert supergraph.read(anchor)["state"] == "fresh"


def test_refresh_hits_when_fresh_and_rebuilds_when_stale(tmp_path, monkeypatch):
    anchor, member = _workspace(tmp_path, monkeypatch)
    supergraph.write(anchor)
    calls: list[str] = []
    original = cross_repo.relations

    def counting(*args, **kwargs):
        calls.append("relations")
        return original(*args, **kwargs)

    monkeypatch.setattr(cross_repo, "relations", counting)

    hit = supergraph.refresh(anchor)
    assert hit["cache"] == "hit"
    assert hit["rebuilt"] is False
    assert hit["state"] == "fresh"
    assert calls == []

    _snapshot(member, "member", summary="changed")
    miss = supergraph.refresh(anchor)
    assert miss["cache"] == "miss"
    assert miss["rebuilt"] is True
    assert miss["previous_state"] == "stale"
    assert miss["state"] == "fresh"
    assert calls == ["relations"]
    assert miss["graph"] == cross_repo.relations(anchor)

    again = supergraph.refresh(anchor)
    assert again["cache"] == "hit"
    assert calls == ["relations", "relations"]


def test_refresh_rebuilds_when_the_explicit_selection_differs(tmp_path, monkeypatch):
    anchor, _ = _workspace(tmp_path, monkeypatch)
    other = tmp_path / "elsewhere" / "other"
    other.mkdir(parents=True)
    _snapshot(other, "other")
    supergraph.write(anchor)

    selected = supergraph.refresh(anchor, roots=[other])
    assert selected["cache"] == "miss"
    assert selected["rebuilt"] is True
    assert selected["reason"] == "selected members differ from the cached artifact"
    assert {member["id"] for member in selected["graph"]["members"]} == {"anchor", "other"}
    assert selected["artifact"]["selection"]["mode"] == "explicit"

    again = supergraph.refresh(anchor, roots=[other])
    assert again["cache"] == "hit"

    default = supergraph.refresh(anchor)
    assert default["cache"] == "miss"
    assert {member["id"] for member in default["graph"]["members"]} == {"anchor", "member"}
    assert default["artifact"]["selection"]["mode"] == "manifest"


def test_atomic_write_failure_keeps_the_existing_artifact(tmp_path, monkeypatch):
    anchor, _ = _workspace(tmp_path, monkeypatch)
    written = supergraph.write(anchor)
    path = Path(written["path"])
    before = path.read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(supergraph.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        supergraph.write(anchor)

    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    monkeypatch.undo()
    assert supergraph.read(anchor)["state"] == "fresh"


def test_writes_stay_inside_the_state_dir_or_the_requested_out_path(tmp_path, monkeypatch):
    anchor, member = _workspace(tmp_path, monkeypatch)
    before = _files(tmp_path)

    supergraph.build(anchor)
    supergraph.read(anchor)
    assert _files(tmp_path) == before

    supergraph.write(anchor)
    assert _files(tmp_path) - before == {
        Path("anchor/.ozagia") / supergraph.DEFAULT_NAME}
    assert not (member / ".ozagia" / supergraph.DEFAULT_NAME).exists()

    out = tmp_path / "artifacts" / "supergraph.json"
    supergraph.write(anchor, out=out)
    assert out.is_file()
    assert not (member / ".ozagia" / supergraph.DEFAULT_NAME).exists()
    assert supergraph.read(anchor, out=out)["state"] == "fresh"


def test_explicit_out_inside_a_member_is_rejected_without_touching_sources(
        tmp_path, monkeypatch):
    anchor, member = _workspace(tmp_path, monkeypatch)
    member_manifest = member / ".ozagia" / "federation.json"
    member_manifest.write_text(
        json.dumps({"version": 1, "members": [], "links": []}), encoding="utf-8")
    protected = {
        "anchor_graph": anchor / ".ozagia" / "graph.json",
        "anchor_manifest": anchor / ".ozagia" / "federation.json",
        "member_graph": member / ".ozagia" / "graph.json",
        "member_manifest": member_manifest,
    }
    before = {name: path.read_bytes() for name, path in protected.items()}
    targets = [
        member / "supergraph.json",
        member / "src" / "app.py",
        member / ".ozagia" / "graph.json",
        member / ".ozagia" / "federation.json",
        anchor / ".ozagia" / "graph.json",
        anchor / ".ozagia" / "federation.json",
        member / ".ozagia",
    ]

    for target in targets:
        with pytest.raises(supergraph.SupergraphError,
                           match="refusing to write") as excinfo:
            supergraph.write(anchor, out=target)
        assert len(str(excinfo.value)) <= supergraph.MAX_TEXT + 3

    for name, path in protected.items():
        assert path.read_bytes() == before[name]
    assert not (member / "supergraph.json").exists()
    assert not (member / "src").exists()
    assert not (anchor / ".ozagia" / supergraph.DEFAULT_NAME).exists()


def test_explicit_out_allows_default_anchor_state_and_safe_member_artifacts(
        tmp_path, monkeypatch):
    anchor, member = _workspace(tmp_path, monkeypatch)
    member_graph = member / ".ozagia" / "graph.json"
    member_manifest = member / ".ozagia" / "federation.json"
    member_manifest.write_text(
        json.dumps({"version": 1, "members": [], "links": []}), encoding="utf-8")
    before = (member_graph.read_bytes(), member_manifest.read_bytes())
    default = anchor / ".ozagia" / supergraph.DEFAULT_NAME

    direct = supergraph.write(anchor)
    assert direct["path"] == str(default)
    assert default.is_file()
    explicit_default = supergraph.write(anchor, out=default)
    assert explicit_default["path"] == str(default)
    assert explicit_default["state"] == "fresh"

    nested = member / ".ozagia" / "exports" / "relationships.json"
    nested_result = supergraph.write(anchor, out=nested)
    assert nested_result["path"] == str(nested)
    assert nested.is_file()
    assert supergraph.read(anchor, out=nested)["state"] == "fresh"

    member_artifact = member / ".ozagia" / supergraph.DEFAULT_NAME
    artifact_result = supergraph.write(anchor, out=member_artifact)
    assert artifact_result["path"] == str(member_artifact)
    assert member_artifact.is_file()

    assert member_graph.read_bytes() == before[0]
    assert member_manifest.read_bytes() == before[1]
    assert not (member / "supergraph.json").exists()


def test_anchor_state_dir_environment_applies_only_to_the_anchor(tmp_path, monkeypatch):
    anchor, member, external = tmp_path / "anchor", tmp_path / "member", tmp_path / "state"
    anchor.mkdir()
    member.mkdir()
    external.mkdir()
    _snapshot(member, "member")
    (external / "graph.json").write_text(_snapshot_bytes(anchor, "anchor"),
                                         encoding="utf-8")
    (external / "federation.json").write_text(
        json.dumps(_federation(member)), encoding="utf-8")
    monkeypatch.setenv("OZAGIA_STATE_DIR", str(external))
    monkeypatch.setattr(federation, "_freshness", lambda item: ("fresh", None))

    written = supergraph.write(anchor)

    assert written["path"] == str(external / supergraph.DEFAULT_NAME)
    assert (external / supergraph.DEFAULT_NAME).is_file()
    assert not (anchor / ".ozagia" / supergraph.DEFAULT_NAME).exists()
    assert not (member / ".ozagia" / supergraph.DEFAULT_NAME).exists()
    cached = supergraph.read(anchor)
    assert cached["state"] == "fresh"
    assert set(cached["source_hashes"]) == {"anchor", "member"}


def test_artifact_size_is_bounded(tmp_path, monkeypatch):
    anchor, _ = _workspace(tmp_path, monkeypatch)
    monkeypatch.setattr(supergraph, "MAX_ARTIFACT_BYTES", 1024)

    with pytest.raises(supergraph.SupergraphError, match="artifact bound"):
        supergraph.build(anchor)
    assert not (anchor / ".ozagia" / supergraph.DEFAULT_NAME).exists()


def test_cli_build_and_graph_paths(tmp_path, monkeypatch, capsys):
    anchor, member = _workspace(tmp_path, monkeypatch)
    calls: list[str] = []
    original = cross_repo.relations

    def counting(*args, **kwargs):
        calls.append("relations")
        return original(*args, **kwargs)

    monkeypatch.setattr(cross_repo, "relations", counting)

    assert main(["build", "--root", str(anchor), "--no-sync", "--json"]) == 0
    built = json.loads(capsys.readouterr().out)
    assert built["state"] == "fresh"
    assert Path(built["path"]).is_file()
    assert built["content_hash"]
    assert calls == ["relations"]

    calls.clear()
    assert main(["graph", "--root", str(anchor), "--no-sync", "--json"]) == 0
    cached = json.loads(capsys.readouterr().out)
    assert cached["state"] == "fresh"
    assert cached["graph"] == built["graph"]
    assert calls == []

    _snapshot(member, "member", summary="changed")
    assert main(["graph", "--root", str(anchor), "--no-sync", "--json"]) == 0
    stale = json.loads(capsys.readouterr().out)
    assert stale["state"] == "stale"
    assert stale["changed"] == ["member"]
    assert "ozagia federation build" in stale["rebuild"]
    assert calls == []

    assert main(["graph", "--root", str(anchor), "--no-sync", "--refresh",
                 "--json"]) == 0
    refreshed = json.loads(capsys.readouterr().out)
    assert refreshed["cache"] == "miss"
    assert refreshed["rebuilt"] is True
    assert refreshed["state"] == "fresh"
    assert calls == ["relations"]

    custom = tmp_path / "export" / "supergraph.json"
    assert main(["build", "--root", str(anchor), "--out", str(custom),
                 "--no-sync", "--json"]) == 0
    custom_built = json.loads(capsys.readouterr().out)
    assert custom_built["path"] == str(custom)
    assert custom.is_file()

    assert main(["graph", "--root", str(anchor), "--out", str(custom),
                 "--no-sync", "--json"]) == 0
    custom_cached = json.loads(capsys.readouterr().out)
    assert custom_cached["state"] == "fresh"
    # Custom out paths are included in the rebuild command.
    missing = supergraph.read(anchor, out=tmp_path / "missing" / "supergraph.json")
    assert f"--out {tmp_path / 'missing' / 'supergraph.json'}" in missing["rebuild"]
