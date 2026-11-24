"""Declared-dependency links across federation members (read-only fixtures)."""
from __future__ import annotations

import json
from pathlib import Path

from ozagia.core import cross_repo, federation
from ozagia.federation_cli import main


def _snapshot(root: Path, name: str) -> None:
    state = root / ".ozagia"
    state.mkdir(parents=True)
    (state / "graph.json").write_text(json.dumps({
        "version": 2,
        "nodes": [{"id": 0, "type": "Repo", "key": "repo:",
                   "attrs": {"name": name, "root": str(root), "head": "head"}}],
        "edges": [],
    }), encoding="utf-8")


def _member(root: Path, name: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _snapshot(root, name or root.name)


def _pyproject(root: Path, name: str, version: str | None,
               dependencies: list[str] | None = None) -> None:
    lines = ["[project]", f'name = "{name}"']
    if version is not None:
        lines.append(f'version = "{version}"')
    if dependencies is not None:
        rendered = ", ".join(json.dumps(item) for item in dependencies)
        lines.append(f"dependencies = [{rendered}]")
    (root / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _links(report: dict) -> dict[str, dict]:
    links: dict[str, dict] = {}
    for link in report["dependency_links"]["links"]:
        links.setdefault(link["package"], link)
    return links


def test_declared_member_ids_are_used_in_dependency_links(tmp_path, monkeypatch):
    consumer, provider = tmp_path / "consumer", tmp_path / "provider"
    _member(consumer)
    _member(provider)
    _pyproject(consumer, "consumer", "1.0.0", ["shared-python>=1"])
    _pyproject(provider, "shared-python", "1.4.0")
    (consumer / ".ozagia" / "federation.json").write_text(json.dumps({
        "version": 1,
        "members": [{"path": str(provider), "id": "shared-python-src"}],
        "links": [],
    }), encoding="utf-8")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = cross_repo.check(consumer)

    link = _links(report)["shared-python"]
    assert link["consumer"] == "consumer"
    assert link["provider"] == "shared-python-src"
    assert link["status"] == "satisfied"
    member = next(m for m in report["dependency_links"]["members"]
                  if m["root"] == str(provider.resolve()))
    assert (member["id"], member["identity_source"]) == (
        "shared-python-src", "declared")


def test_supported_manifests_produce_satisfied_links(tmp_path, monkeypatch):
    python_consumer = tmp_path / "python-consumer"
    python_provider = tmp_path / "python-provider"
    rust_consumer = tmp_path / "rust-consumer"
    rust_provider = tmp_path / "rust-provider"
    node_consumer = tmp_path / "node-consumer"
    node_provider = tmp_path / "node-provider"
    for path in (python_consumer, python_provider, rust_consumer, rust_provider,
                 node_consumer, node_provider):
        _member(path)
    _pyproject(python_consumer, "consumer", "1.0.0",
               ["shared-python~=1.4", "unprovided>=1"])
    (python_consumer / "requirements-dev.txt").write_text(
        "shared-python>=1.0\n", encoding="utf-8")
    _pyproject(python_provider, "shared-python", "1.4.9")
    (rust_consumer / "Cargo.toml").write_text(
        '[package]\nname = "consumer"\nversion = "0.1.0"\n\n'
        '[dependencies]\nshared-rust = "1.2"\n', encoding="utf-8")
    (rust_provider / "Cargo.toml").write_text(
        '[package]\nname = "shared-rust"\nversion = "1.5.0"\n', encoding="utf-8")
    (node_consumer / "package.json").write_text(json.dumps({
        "name": "consumer", "version": "1.0.0",
        "dependencies": {"shared-node": "^2.1.0"},
    }), encoding="utf-8")
    (node_provider / "package.json").write_text(json.dumps({
        "name": "shared-node", "version": "2.4.0",
    }), encoding="utf-8")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = cross_repo.check(
        python_consumer,
        roots=[python_provider, rust_consumer, rust_provider,
               node_consumer, node_provider],
    )

    links = _links(report)
    assert links["shared-python"]["status"] == "satisfied"
    assert links["shared-python"]["provided_version"] == "1.4.9"
    assert links["shared-python"]["source_manifest"] == "pyproject.toml"
    assert links["shared-python"]["provenance"] == "declared_dependency"
    assert any(
        link["package"] == "shared-python"
        and link["source_manifest"] == "requirements-dev.txt"
        and link["status"] == "satisfied"
        for link in report["dependency_links"]["links"])
    assert links["shared-rust"]["status"] == "satisfied"
    assert links["shared-rust"]["provider"] == "rust-provider"
    assert links["shared-node"]["status"] == "satisfied"
    assert links["shared-node"]["provider"] == "node-provider"
    assert links["unprovided"]["status"] == "unresolved"


def test_setup_cfg_and_go_manifest_are_read(tmp_path, monkeypatch):
    consumer = tmp_path / "consumer"
    provider = tmp_path / "provider"
    go_consumer = tmp_path / "go-consumer"
    go_provider = tmp_path / "go-provider"
    for path in (consumer, provider, go_consumer, go_provider):
        _member(path)
    (consumer / "setup.cfg").write_text(
        "[metadata]\nname = consumer\nversion = 1.0.0\n\n"
        "[options]\ninstall_requires =\n    shared-python>=1.0\n",
        encoding="utf-8")
    _pyproject(provider, "shared-python", "1.2.0")
    (go_consumer / "go.mod").write_text(
        "module example.com/consumer\n\ngo 1.22\n\n"
        "require example.com/shared v1.2.0\n", encoding="utf-8")
    (go_provider / "go.mod").write_text(
        "module example.com/shared\n\ngo 1.22\n", encoding="utf-8")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = cross_repo.check(
        consumer, roots=[provider, go_consumer, go_provider])
    links = _links(report)

    assert links["shared-python"]["status"] == "satisfied"
    assert links["shared-python"]["source_manifest"] == "setup.cfg"
    assert links["example.com/shared"]["status"] == "unknown"
    assert links["example.com/shared"]["reason"] == "provided_version_unavailable"
    assert links["example.com/shared"]["provider_manifest"] == "go.mod"


def test_mismatched_requirement_keeps_line_and_version_provenance(tmp_path,
                                                                  monkeypatch):
    consumer = tmp_path / "consumer"
    provider = tmp_path / "provider"
    _member(consumer)
    _member(provider)
    _pyproject(consumer, "consumer", "1.0.0", ["shared>=2,<3"])
    _pyproject(provider, "shared", "1.5.0")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    link = _links(cross_repo.check(consumer, roots=[provider]))["shared"]

    assert link["status"] == "mismatch"
    assert link["reason"] == "provided_version_does_not_match_requirement"
    assert link["requirement"] == ">=2,<3"
    assert link["provided_version"] == "1.5.0"
    assert link["source_manifest"] == "pyproject.toml"
    assert link["source_line"] == 4
    assert link["provider_manifest"] == "pyproject.toml"
    assert link["provider_line"] == 2


def test_unknown_ranges_and_missing_provider_versions_stay_unknown(
        tmp_path, monkeypatch):
    node_consumer = tmp_path / "node-consumer"
    node_provider = tmp_path / "node-provider"
    python_consumer = tmp_path / "python-consumer"
    python_provider = tmp_path / "python-provider"
    for path in (node_consumer, node_provider, python_consumer, python_provider):
        _member(path)
    (node_consumer / "package.json").write_text(json.dumps({
        "name": "consumer", "dependencies": {"shared-node": "latest"},
    }), encoding="utf-8")
    (node_provider / "package.json").write_text(json.dumps({
        "name": "shared-node", "version": "1.0.0",
    }), encoding="utf-8")
    _pyproject(python_consumer, "consumer", "1.0.0", ["dynamic-version>=1"])
    _pyproject(python_provider, "dynamic-version", None)
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    links = _links(cross_repo.check(
        node_consumer, roots=[node_provider, python_consumer, python_provider]))

    assert links["shared-node"]["status"] == "unknown"
    assert links["shared-node"]["reason"] == "requirement_has_no_version_constraint"
    assert links["dynamic-version"]["status"] == "unknown"
    assert links["dynamic-version"]["reason"] == "provided_version_unavailable"


def test_multiple_members_providing_package_is_ambiguous(tmp_path, monkeypatch):
    consumer = tmp_path / "consumer"
    first = tmp_path / "first"
    second = tmp_path / "second"
    for path in (consumer, first, second):
        _member(path)
    _pyproject(consumer, "consumer", "1.0.0", ["shared>=1"])
    _pyproject(first, "shared", "1.0.0")
    _pyproject(second, "shared", "2.0.0")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = cross_repo.check(consumer, roots=[first, second])
    link = _links(report)["shared"]

    assert link["status"] == "ambiguous"
    assert link["reason"] == "multiple_members_provide_package"
    assert link["provider"] is None
    assert link["provided_version"] is None
    assert [candidate["member"] for candidate in link["candidates"]] == [
        "first", "second"]
    assert report["dependency_links"]["counts"]["ambiguous"] == 1
    assert report["dependency_links"]["ambiguities"] == [link]


def test_missing_manifest_is_unresolved_and_no_dependency_is_empty(
        tmp_path, monkeypatch):
    consumer = tmp_path / "consumer"
    empty_provider = tmp_path / "empty-provider"
    no_dependency_provider = tmp_path / "no-dependency-provider"
    for path in (consumer, empty_provider, no_dependency_provider):
        _member(path)
    _pyproject(consumer, "consumer", "1.0.0", ["shared>=1"])
    _pyproject(no_dependency_provider, "unrelated", "1.0.0")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = cross_repo.check(
        consumer, roots=[empty_provider, no_dependency_provider])
    link = _links(report)["shared"]

    assert link["status"] == "unresolved"
    assert link["reason"] == "no_member_provides_package"
    assert report["dependency_links"]["members"][1]["manifests"] == []

    empty_no_dependency = cross_repo.check(
        no_dependency_provider, roots=[empty_provider])
    section = empty_no_dependency["dependency_links"]
    assert section["state"] == "ok"
    assert section["links"] == []
    assert section["counts"] == {
        "satisfied": 0, "mismatch": 0, "unknown": 0,
        "ambiguous": 0, "unresolved": 0,
    }


def test_unsupported_manifest_and_lockfile_are_reported_as_skipped(
        tmp_path, monkeypatch):
    consumer = tmp_path / "consumer"
    provider = tmp_path / "provider"
    for path in (consumer, provider):
        _member(path)
    _pyproject(consumer, "consumer", "1.0.0", ["shared>=1"])
    _pyproject(provider, "shared", "1.1.0")
    (provider / "setup.py").write_text("# not executed\n", encoding="utf-8")
    (provider / "Cargo.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    report = cross_repo.check(consumer, roots=[provider])
    skipped = report["dependency_links"]["members"][1]["skipped"]

    assert {"path": "setup.py", "state": "unsupported",
            "reason": "executable Python setup scripts are not run"} in skipped
    assert any(item["path"] == "Cargo.lock" and item["state"] == "skipped"
               for item in skipped)


def test_dependency_check_does_not_write_member_checkouts(tmp_path, monkeypatch):
    consumer = tmp_path / "consumer"
    provider = tmp_path / "provider"
    for path in (consumer, provider):
        _member(path)
    _pyproject(consumer, "consumer", "1.0.0", ["shared>=1"])
    _pyproject(provider, "shared", "1.1.0")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    def tree() -> dict[str, bytes]:
        return {str(path.relative_to(tmp_path)): path.read_bytes()
                for path in sorted(tmp_path.rglob("*")) if path.is_file()}

    before = tree()
    report = cross_repo.check(consumer, roots=[provider])
    after = tree()

    assert _links(report)["shared"]["status"] == "satisfied"
    assert after == before


def test_federation_check_cli_exposes_dependency_links(tmp_path, monkeypatch,
                                                       capsys):
    consumer = tmp_path / "consumer"
    provider = tmp_path / "provider"
    for path in (consumer, provider):
        _member(path)
    _pyproject(consumer, "consumer", "1.0.0", ["shared>=1"])
    _pyproject(provider, "shared", "1.1.0")
    monkeypatch.setattr(federation, "_freshness", lambda _: ("fresh", None))

    assert main(["check", "--root", str(consumer), "--repo", str(provider),
                 "--no-sync"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["dependency_links"]["links"][0]["status"] == "satisfied"
    assert payload["dependency_links"]["links"][0]["provider"] == "provider"
