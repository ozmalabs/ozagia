import json

from ozagia.core.deep_review.analyzers.dependencies import analyze, build_sbom


def test_inventory_reports_python_manifest_and_lock(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests>=2", "httpx"]\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "requests"\nversion = "2.32.0"\n',
        encoding="utf-8")

    result = analyze(tmp_path)

    assert result["checked"] is True
    assert result["findings"] == []
    assert result["inventory"]["manifests"] == [{
        "file": "pyproject.toml", "ecosystem": "python", "dependency_count": 2,
        "dependencies": ["httpx", "requests"], "dependencies_truncated": False,
        "lockfile": "uv.lock"}]
    assert result["inventory"]["lockfiles"][0]["entries"] == 1


def test_inventory_reports_missing_lockfile_without_network_or_install(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"left-pad": "^1.0.0"}}), encoding="utf-8")

    result = analyze(tmp_path)

    assert len(result["findings"]) == 1
    assert result["findings"][0]["type"] == "missing_lock_file"
    assert result["findings"][0]["keys"] == ["file:package.json"]
    assert result["inventory"]["manifests"][0]["dependencies"] == ["left-pad"]


def test_inventory_is_bounded_and_ignores_symlinked_manifests(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-external")
    outside.mkdir()
    (outside / "package.json").write_text('{"dependencies":{"x":"1"}}',
                                            encoding="utf-8")
    (tmp_path / "linked-package.json").symlink_to(outside / "package.json")
    (tmp_path / "requirements.txt").write_text("a==1\n", encoding="utf-8")

    result = analyze(tmp_path, budget_s=0)

    assert result["inventory"]["manifests"] == []
    assert result["checked"] is False


def test_license_declarations_and_license_files_are_inventoried(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\ndependencies = ["requests>=2"]\n'
        'license = {text = "MIT"}\n'
        'classifiers = ["License :: OSI Approved :: MIT License"]\n',
        encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({
        "license": "Apache-2.0", "dependencies": {"left-pad": "^1.0.0"}}),
        encoding="utf-8")
    (tmp_path / "LICENSE").write_text("MIT\n", encoding="utf-8")

    result = analyze(tmp_path)

    licenses = result["inventory"]["licenses"]
    assert licenses["state"] == "declared"
    assert licenses["files"] == ["LICENSE"]
    origins = {(row["file"], row["license"])
               for row in licenses["declared"]}
    assert ("pyproject.toml", "MIT") in origins
    assert ("pyproject.toml", "MIT License") in origins
    assert ("package.json", "Apache-2.0") in origins
    by_file = {row["file"]: row for row in result["inventory"]["manifests"]}
    assert by_file["pyproject.toml"]["licenses"] == ["MIT", "MIT License"]
    assert by_file["package.json"]["licenses"] == ["Apache-2.0"]
    assert licenses["dependency_license_assertions"] == "not_assessed"


def test_duplicate_and_conflicting_versions_are_reported_across_inputs(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"left-pad": "^1.0.0"}}), encoding="utf-8")
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "packages": {"": {"name": "root"},
                     "node_modules/left-pad": {"version": "1.0.0"}},
        "dependencies": {}}), encoding="utf-8")
    nested = tmp_path / "web"
    nested.mkdir()
    (nested / "package.json").write_text(json.dumps({
        "dependencies": {"left-pad": "^2.0.0"}}), encoding="utf-8")
    (nested / "package-lock.json").write_text(json.dumps({
        "packages": {"": {"name": "web"},
                     "node_modules/left-pad": {"version": "2.0.0"}},
        "dependencies": {}}), encoding="utf-8")

    result = analyze(tmp_path)

    conflict = result["inventory"]["version_conflicts"][0]
    assert conflict["dependency"] == "left-pad"
    assert conflict["ecosystem"] == "node"
    assert conflict["versions"] == ["1.0.0", "2.0.0"]
    assert conflict["sources"] == ["package-lock.json", "web/package-lock.json"]
    duplicate = result["inventory"]["duplicate_declarations"][0]
    assert duplicate["dependency"] == "left-pad"
    assert duplicate["sources"] == ["package.json", "web/package.json"]
    raw_conflict = next(item for item in result["findings"]
                        if item["type"] == "conflicting_dependency_versions")
    assert raw_conflict["locations"] == [
        {"path": "package-lock.json"},
        {"path": "web/package-lock.json"}]
    raw_duplicate = next(item for item in result["findings"]
                         if item["type"] == "duplicate_dependency_declaration")
    assert raw_duplicate["locations"] == [
        {"path": "package.json"}, {"path": "web/package.json"}]
    types = {finding["type"] for finding in result["findings"]}
    assert types == {"conflicting_dependency_versions",
                     "duplicate_dependency_declaration"}


def test_npm_v1_inventory_includes_nested_transitive_dependencies(tmp_path):
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 1,
        "dependencies": {
            "parent": {"version": "1.0.0", "dependencies": {
                "@scope/child": {"version": "2.0.0", "dependencies": {
                    "leaf": {"version": "3.0.0"}}}}},
        },
    }), encoding="utf-8")

    lock = analyze(tmp_path)["inventory"]["lockfiles"][0]

    assert lock["entries"] == 3
    assert lock["packages"] == [
        {"name": "@scope/child", "version": "2.0.0"},
        {"name": "leaf", "version": "3.0.0"},
        {"name": "parent", "version": "1.0.0"},
    ]


def test_lockfile_yarn_and_pnpm_versions_are_parsed(tmp_path):
    (tmp_path / "yarn.lock").write_text(
        '"left-pad@^1.0.0":\n  version "1.0.0"\n  resolved "x"\n',
        encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\npackages:\n  left-pad@2.0.0:\n"
        "    resolution: {integrity: x}\n", encoding="utf-8")

    result = analyze(tmp_path)

    by_file = {row["file"]: row for row in result["inventory"]["lockfiles"]}
    assert by_file["yarn.lock"]["packages"] == [
        {"name": "left-pad", "version": "1.0.0"}]
    assert by_file["pnpm-lock.yaml"]["packages"] == [
        {"name": "left-pad", "version": "2.0.0"}]
    assert result["inventory"]["version_conflicts"][0]["versions"] == [
        "1.0.0", "2.0.0"]


def test_bundler_lockfile_inventory_is_bounded_and_reports_unsupported_pins(tmp_path):
    (tmp_path / "Gemfile.lock").write_text(
        "GEM\n  remote: https://example.invalid/\n  specs:\n"
        "    rack (3.0.8)\n      base64 (>= 0)\n"
        "    nokogiri (1.16.0-x86_64-linux)\n"
        "GIT\n  remote: https://example.invalid/repo.git\n  specs:\n"
        "    helper (2.1.0.pre)\n"
        "PATH\n  remote: vendor/local\n  specs:\n"
        "    odd gem entry\n\nPLATFORMS\n  ruby\n",
        encoding="utf-8")

    result = analyze(tmp_path)

    lock = result["inventory"]["lockfiles"][0]
    assert lock["ecosystem"] == "ruby"
    assert lock["format"] == "bundler"
    assert lock["entries"] == 3
    assert lock["packages"] == [
        {"name": "helper", "version": "2.1.0.pre", "source": "git"},
        {"name": "rack", "version": "3.0.8", "source": "rubygems"}]
    assert any("unsupported gem name/version" in item["reason"]
               for item in result["not_checked"])
    component = next(item for item in result["sbom"]["components"]
                     if item["name"] == "rack")
    assert component["purl"] == "pkg:gem/rack@3.0.8"


def test_composer_lock_inventory_is_bounded_and_reports_unsupported_pins(tmp_path):
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "composer.lock").write_text(json.dumps({
        "packages": [
            {"name": "Vendor/Package", "version": "v2.4.1"},
            {"name": "vendor/branch", "version": "dev-main"},
            {"name": "invalid-name", "version": "1.0.0"},
        ],
        "packages-dev": [{"name": "phpunit/phpunit", "version": "10.5.0"}],
    }), encoding="utf-8")

    result = analyze(tmp_path)

    lock = result["inventory"]["lockfiles"][0]
    assert lock["ecosystem"] == "composer"
    assert lock["format"] == "composer"
    assert lock["entries"] == 4
    assert lock["packages"] == [
        {"name": "phpunit/phpunit", "version": "10.5.0",
         "source": "composer.lock", "provenance": "unknown"},
        {"name": "vendor/package", "version": "v2.4.1",
         "source": "composer.lock", "provenance": "unknown"}]
    assert any("unsupported package identity/version" in item["reason"]
               for item in result["not_checked"])
    component = next(item for item in result["sbom"]["components"]
                     if item["name"] == "vendor/package")
    assert component["purl"] == "pkg:composer/vendor/package@v2.4.1"


def test_gradle_lock_inventory_preserves_coordinate_and_unknown_provenance(tmp_path):
    (tmp_path / "gradle.lockfile").write_text(
        "# generated lock state\n"
        "org.example:widget:1.2.3=compileClasspath,runtimeClasspath\n"
        "empty=\n"
        "org.example:dynamic:1.+=runtimeClasspath\n"
        "org.example:latest:latest.release=runtimeClasspath\n"
        "org.example:release:RELEASE=runtimeClasspath\n"
        "org.example:snapshot:1.2.3-SNAPSHOT=runtimeClasspath\n"
        "broken coordinate=runtimeClasspath\n",
        encoding="utf-8")

    result = analyze(tmp_path)

    lock = result["inventory"]["lockfiles"][0]
    assert lock["ecosystem"] == "maven"
    assert lock["format"] == "gradle"
    assert lock["entries"] == 6
    assert lock["packages"] == [{
        "name": "org.example:widget", "version": "1.2.3",
        "source": "gradle.lockfile", "provenance": "repository-unknown"}]
    assert any("unsupported or dynamic module pin" in item["reason"]
               for item in result["not_checked"])
    component = result["sbom"]["components"][0]
    assert component["purl"] == "pkg:maven/org.example/widget@1.2.3"
    assert {item["value"] for item in component["properties"]
            if item["name"] == "ozagia:provenance"} == {"repository-unknown"}


def test_cve_state_is_unknown_and_sbom_is_deterministic(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests", "httpx"]\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "requests"\nversion = "2.32.0"\n'
        '\n[[package]]\nname = "httpx"\nversion = "0.27.0"\n',
        encoding="utf-8")

    first = analyze(tmp_path)
    second = analyze(tmp_path)

    assert first["inventory"]["cve_state"] == "unknown"
    assert any("CVE state is unknown" in item["reason"]
               for item in first["not_checked"])
    assert first["sbom"] == second["sbom"]
    components = {item["name"]: item for item in first["sbom"]["components"]}
    assert components["requests"]["version"] == "2.32.0"
    assert components["requests"]["purl"] == "pkg:pypi/requests@2.32.0"
    properties = {item["name"]: item["value"]
                  for item in components["requests"]["properties"]}
    assert properties["ozagia:cve_state"] == "unknown"
    metadata = {item["name"]: item["value"]
                for item in first["sbom"]["metadata"]["properties"]}
    assert metadata["ozagia:cve_state"] == "unknown"
    assert build_sbom(first, root_name=tmp_path.name) == first["sbom"]


def test_cargo_inventory_reports_possible_package_truncation(tmp_path):
    entries = "".join(
        f'[[package]]\nname = "package-{i}"\nversion = "1.0.0"\n'
        for i in range(501))
    (tmp_path / "Cargo.lock").write_text(entries, encoding="utf-8")

    result = analyze(tmp_path)
    lock = result["inventory"]["lockfiles"][0]

    assert lock["entries"] == 501
    assert len(lock["packages"]) == 500
    assert any("package inventory may be truncated" in reason
               for reason in lock["omissions"])


def test_cargo_local_advisories_require_crates_io_provenance(tmp_path):
    (tmp_path / "Cargo.lock").write_text(
        'version = 3\n'
        '[[package]]\nname = "registry-demo"\nversion = "1.2.3"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
        '[[package]]\nname = "fork-demo"\nversion = "1.2.3"\n'
        'source = "git+https://example.com/fork#abc"\n'
        '[[package]]\nname = "workspace-demo"\nversion = "1.2.3"\n'
        '[[package]]\nname = "private-demo"\nversion = "1.2.3"\n'
        'source = "registry+https://example.com/private"\n', encoding="utf-8")
    advisories = {"schema": "ozagia.local_advisories.v1", "advisories": [
        {"id": f"CARGO-{i}", "ecosystem": "rust", "package": name,
         "affected_versions": ["1.2.3"]}
        for i, name in enumerate(("registry-demo", "fork-demo", "workspace-demo", "private-demo"))
    ]}

    result = analyze(tmp_path, local_advisories=advisories)

    matches = [item for item in result["findings"]
               if item["type"] == "local_advisory_exact_version_match"]
    assert [item["dependency"] for item in matches] == ["registry-demo"]
    assert any("3 Cargo pins were excluded" in item["reason"]
               for item in result["not_checked"])
    packages = result["inventory"]["lockfiles"][0]["packages"]
    assert {item["name"]: item["provenance"] for item in packages} == {
        "registry-demo": "crates-io", "fork-demo": "git",
        "workspace-demo": "workspace-or-path", "private-demo": "other-or-unknown-registry"}


def test_npm_advisories_respect_alias_names_and_source_provenance(tmp_path):
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "node_modules/alias": {"name": "actual", "version": "1.2.3",
                                   "resolved": "https://registry.npmjs.org/actual/-/actual-1.2.3.tgz"},
            "node_modules/fork": {"version": "1.2.3", "resolved": "git+https://example.com/fork"},
            "node_modules/local": {"version": "1.2.3", "resolved": "file:../local"},
            "node_modules/private": {"version": "1.2.3", "resolved": "https://private.example/pkg.tgz"},
            "node_modules/workspace": {"version": "1.2.3", "link": True},
        },
    }), encoding="utf-8")
    advisories = {"schema": "ozagia.local_advisories.v1", "advisories": [
        {"id": f"NPM-{i}", "ecosystem": "node", "package": name,
         "affected_versions": ["1.2.3"]}
        for i, name in enumerate(("actual", "alias", "fork", "local", "private", "workspace"))
    ]}

    result = analyze(tmp_path, local_advisories=advisories)
    matches = [item["dependency"] for item in result["findings"]
               if item["type"] == "local_advisory_exact_version_match"]
    assert matches == ["actual"]
    assert any("4 npm pins were excluded" in item["reason"] for item in result["not_checked"])
    assert "private.example" not in json.dumps(result)


def test_python_direct_sources_do_not_match_registry_advisories(tmp_path):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n'
        '[[package]]\nname = "registry-demo"\nversion = "1.2.3"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        '[[package]]\nname = "fork-demo"\nversion = "1.2.3"\n'
        'source = { git = "https://secret.example/fork" }\n'
        '[[package]]\nname = "local-demo"\nversion = "1.2.3"\n'
        'source = { editable = "." }\n'
        '[[package]]\nname = "url-demo"\nversion = "1.2.3"\n'
        'source = { url = "https://secret.example/package.whl" }\n', encoding="utf-8")
    advisories = {"schema": "ozagia.local_advisories.v1", "advisories": [
        {"id": f"PY-{i}", "ecosystem": "python", "package": name,
         "affected_versions": ["1.2.3"]}
        for i, name in enumerate(("registry-demo", "fork-demo", "local-demo", "url-demo"))
    ]}

    result = analyze(tmp_path, local_advisories=advisories)
    matches = [item["dependency"] for item in result["findings"]
               if item["type"] == "local_advisory_exact_version_match"]
    assert matches == ["registry-demo"]
    assert any("3 Python pins were excluded" in item["reason"] for item in result["not_checked"])
    assert "secret.example" not in json.dumps(result)


def test_yarn_explicit_git_and_local_selectors_do_not_match_registry_advisories(tmp_path):
    (tmp_path / "yarn.lock").write_text(
        '"safe@^1.0.0":\n  version "1.2.3"\n'
        '"fork@git+https://secret.example/fork":\n  version "1.2.3"\n'
        '"local@file:../local":\n  version "1.2.3"\n', encoding="utf-8")
    advisory = {"schema": "ozagia.local_advisories.v1", "advisories": [
        {"id": f"NPM-{i}", "ecosystem": "node", "package": name,
         "affected_versions": ["1.2.3"]}
        for i, name in enumerate(("safe", "fork", "local"))]}
    result = analyze(tmp_path, local_advisories=advisory)
    matches = [finding["dependency"] for finding in result["findings"]
               if finding["type"] == "local_advisory_exact_version_match"]
    assert matches == ["safe"]
    assert "secret.example" not in json.dumps(result)
