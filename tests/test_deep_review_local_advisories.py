import json
import os

import pytest

from ozagia.core.deep_review.analyzers.dependencies import analyze
from ozagia.core.deep_review.analyzers.local_advisories import (
    MAX_SOURCE_BYTES,
    SCHEMA,
    match_local_advisories,
)
from ozagia.core.supply_chain import RULES as SUPPLY_CHAIN_RULES


def _non_supply_chain(findings):
    """Advisory tests assert their own findings; supply-chain findings are separate."""
    return [finding for finding in findings
            if finding["type"] not in SUPPLY_CHAIN_RULES]


def _advisories(*rows):
    return {"schema": SCHEMA, "advisories": list(rows)}


def _advisory(*, ecosystem="python", package="Requests", versions=None,
              identifier="CVE-TEST-1", **extra):
    return {"id": identifier, "ecosystem": ecosystem, "package": package,
            "affected_versions": versions or ["2.31.0"], **extra}


def _range_advisory(events, *, ecosystem="python", package="demo",
                    identifier="OSV-PY-1", **extra):
    return {"id": identifier, "ecosystem": ecosystem, "package": package,
            "affected_ranges": [{"events": events}], **extra}


def test_exact_local_advisory_match_uses_pinned_inventory_and_hashes_source():
    inventory = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                                "packages": [{"name": "requests",
                                              "version": "2.31.0"}]}]}

    result = match_local_advisories(inventory, _advisories(_advisory()))

    assert result["state"] == "matches_found"
    assert result["source_hash"].startswith("sha256:")
    assert result["matched_packages"] == 1
    assert len(result["findings"]) == 1
    finding = result["findings"][0]
    assert finding["type"] == "local_advisory_exact_version_match"
    assert finding["severity"] == "minor"
    assert finding["locations"] == [{"path": "uv.lock"}]
    assert finding["advisory_source_hash"] == result["source_hash"]
    assert finding["truth_assessed"] is False


@pytest.mark.parametrize("advisory", [None])
def test_missing_local_advisory_data_stays_unknown(advisory):
    result = match_local_advisories(
        {"lockfiles": [{"ecosystem": "node", "packages": [
            {"name": "left-pad", "version": "1.0.0"}]}]}, advisory)
    assert result["state"] == "unknown"
    assert result["findings"] == []
    assert "no local advisory data" in result["not_checked"]


@pytest.mark.parametrize("inventory, advisory", [
    # A matching manifest constraint is deliberately insufficient.
    ({"lockfiles": []}, _advisory()),
    # Only exact versions are considered; no range evaluation.
    # Different package and different exact version both remain unknown.
    ({"lockfiles": [{"ecosystem": "python", "file": "uv.lock",
                     "packages": [{"name": "requests", "version": "2.32.0"}]}]},
     _advisory()),
])
def test_non_exact_or_nonmatching_data_never_reports_a_match(inventory, advisory):
    result = match_local_advisories(inventory, _advisories(advisory))
    assert result["state"] == "no_exact_match_in_supplied_data"
    assert result["findings"] == []
    assert result["not_checked"]


def test_advisory_loader_rejects_invalid_range_and_duplicate_ids():
    inventory = {"lockfiles": []}
    with pytest.raises(ValueError, match="exact versions"):
        match_local_advisories(inventory, _advisories(
            _advisory(versions=["1.*"])))
    with pytest.raises(ValueError, match="exact versions"):
        match_local_advisories(inventory, _advisories(
            _advisory(versions=[">=2.31.0,<3"])))
    with pytest.raises(ValueError, match="unique"):
        match_local_advisories(inventory, _advisories(
            _advisory(), _advisory()))


def test_advisory_file_size_is_bounded(tmp_path):
    path = tmp_path / "advisories.json"
    path.write_bytes(b" " * (MAX_SOURCE_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        match_local_advisories({"lockfiles": []}, path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO")
def test_advisory_source_must_be_regular_file(tmp_path):
    path = tmp_path / "advisories.fifo"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="regular file"):
        match_local_advisories({"lockfiles": []}, path)


def test_mapping_source_is_bounded_before_serialization():
    with pytest.raises(ValueError, match="size limit"):
        match_local_advisories(
            {"lockfiles": []},
            {"schema": SCHEMA, "advisories": [],
             "extra": "x" * MAX_SOURCE_BYTES})
    nested = {"schema": SCHEMA, "advisories": []}
    value = nested
    for _ in range(66):
        child = {}
        value["child"] = child
        value = child
    with pytest.raises(ValueError, match="nesting limit"):
        match_local_advisories({"lockfiles": []}, nested)


def test_advisory_json_rejects_duplicate_keys_and_non_json_numbers(tmp_path):
    path = tmp_path / "advisories.json"
    path.write_text('{"schema":"x","schema":"x","advisories":[]}',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="valid JSON"):
        match_local_advisories({"lockfiles": []}, path)
    path.write_text('{"schema":"x","advisories":[],"extra":'
                    + "[" * 66 + "0" + "]" * 66 + "}", encoding="utf-8")
    with pytest.raises(ValueError, match="nesting limit"):
        match_local_advisories({"lockfiles": []}, path)
    path.write_text('{"schema":"x","advisories":[],"extra":NaN}',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="valid JSON"):
        match_local_advisories({"lockfiles": []}, path)


def test_exact_version_forms_include_python_prerelease_and_go_prefix():
    inventory = {"lockfiles": [
        {"file": "uv.lock", "ecosystem": "python", "packages": [
            {"name": "requests", "version": "2.32.0rc1"}]},
        {"file": "go.sum", "ecosystem": "go", "packages": [
            {"name": "example.com/mod", "version": "v1.2.3"}]},
    ]}
    result = match_local_advisories(inventory, _advisories(
        _advisory(versions=["2.32.0rc1"]),
        _advisory(ecosystem="go", package="example.com/mod",
                  versions=["v1.2.3"], identifier="GO-TEST-1")))
    assert {(row["advisory_id"], row["version"])
            for row in result["findings"]} == {
                ("CVE-TEST-1", "2.32.0rc1"), ("GO-TEST-1", "1.2.3")}

    with pytest.raises(ValueError, match="exact versions"):
        match_local_advisories(inventory, _advisories(_advisory(
            ecosystem="node", package="requests", versions=["1.2"])))


def test_osv_semver_introduced_fixed_ranges_match_exclusively_and_with_prereleases():
    inventory = {"lockfiles": [{"file": "package-lock.json", "ecosystem": "node",
                                "packages": [
                                    {"name": "demo", "version": "1.2.0"},
                                    {"name": "demo", "version": "1.4.0"},
                                    {"name": "demo", "version": "1.5.0"},
                                    {"name": "demo", "version": "2.0.0-rc.1"},
                                ]}]}
    advisory = {
        "id": "OSV-TEST-1", "ecosystem": "node", "package": "demo",
        "affected_ranges": [{"events": [
            {"introduced": "1.2.0"}, {"fixed": "1.5.0"}]}]}
    result = match_local_advisories(inventory, _advisories(advisory))
    assert {row["version"] for row in result["findings"]} == {
        "1.2.0", "1.4.0"}
    assert all(row["type"] == "local_advisory_semver_range_match"
               for row in result["findings"])


def test_osv_semver_range_supports_go_prefix_and_open_ended_ranges():
    inventory = {"lockfiles": [{"file": "go.sum", "ecosystem": "go",
                                "packages": [{"name": "example.com/m",
                                              "version": "v1.3.0"}]}]}
    advisory = {
        "id": "OSV-GO-1", "ecosystem": "go", "package": "example.com/m",
        "affected_ranges": [{"events": [{"introduced": "v1.2.0"}]}]}
    result = match_local_advisories(inventory, _advisories(advisory))
    assert len(result["findings"]) == 1
    assert result["findings"][0]["version"] == "1.3.0"


def test_pep440_introduced_fixed_range_excludes_the_fixed_version():
    inventory = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                                "packages": [
                                    {"name": "demo", "version": version}
                                    for version in ("2.30.0", "2.31.0",
                                                    "2.31.9", "2.32.0",
                                                    "2.32.0.post1", "3.0.0")]}]}
    advisory = _range_advisory([
        {"introduced": "2.31.0"}, {"fixed": "2.32.0"}])

    result = match_local_advisories(inventory, _advisories(advisory))

    assert {row["version"] for row in result["findings"]} == {
        "2.31.0", "2.31.9"}
    assert all(row["type"] == "local_advisory_pep440_range_match"
               for row in result["findings"])


def test_pep440_last_affected_is_inclusive_and_pre_releases_sort_first():
    inventory = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                                "packages": [
                                    {"name": "demo", "version": version}
                                    for version in (
                                        "0.9", "1.0.dev0", "1.0a1", "1.0",
                                        "1.0.post0", "1.1.0rc1", "1.1.0",
                                        "1.1.0.post1", "1.1.1")]}]}
    advisory = _range_advisory([
        {"introduced": "1.0"}, {"last_affected": "1.1.0"}])

    result = match_local_advisories(inventory, _advisories(advisory))

    assert {row["version"] for row in result["findings"]} == {
        "1.0", "1.0.post0", "1.1.0rc1", "1.1.0"}


def test_pep440_epoch_dominates_the_release_segment():
    inventory = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                                "packages": [
                                    {"name": "demo", "version": version}
                                    for version in (
                                        "0.5", "1!0.1", "1!0.2", "2.0",
                                        "1!1.0")]}]}
    advisory = _range_advisory([
        {"introduced": "1!0.1"}, {"fixed": "1!1.0"}])

    result = match_local_advisories(inventory, _advisories(advisory))

    assert {row["version"] for row in result["findings"]} == {
        "1!0.1", "1!0.2"}


def test_pep440_pre_post_dev_and_local_order_around_a_public_boundary():
    inventory = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                                "packages": [
                                    {"name": "demo", "version": version}
                                    for version in (
                                        "1.0.dev0", "1.0a1", "1.0",
                                        "1.0+local", "1.0.post0.dev0",
                                        "1.0.post0", "1.0.post1", "1.1")]}]}
    advisory = _range_advisory([
        {"introduced": "1.0"}, {"fixed": "1.0.post1"}])

    result = match_local_advisories(inventory, _advisories(advisory))

    assert {row["version"] for row in result["findings"]} == {
        "1.0", "1.0+local", "1.0.post0.dev0", "1.0.post0"}


def test_pep440_local_segments_sort_alphabetic_before_numeric():
    inventory = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                                "packages": [
                                    {"name": "demo", "version": version}
                                    for version in (
                                        "1.0", "1.0+abc", "1.0+abc.1",
                                        "1.0+1", "1.0+1.0", "1.1")]}]}
    advisory = _range_advisory([
        {"introduced": "1.0"}, {"fixed": "1.0+1"}])

    result = match_local_advisories(inventory, _advisories(advisory))

    assert {row["version"] for row in result["findings"]} == {
        "1.0", "1.0+abc", "1.0+abc.1"}


def test_pep440_introduced_zero_starts_before_dev_and_pre_releases():
    inventory = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                                "packages": [
                                    {"name": "demo", "version": version}
                                    for version in (
                                        "0.dev0", "0a1", "0", "0.post1",
                                        "1.0")]}]}
    advisory = _range_advisory([
        {"introduced": "0"}, {"fixed": "1.0"}])

    result = match_local_advisories(inventory, _advisories(advisory))

    assert {row["version"] for row in result["findings"]} == {
        "0.dev0", "0a1", "0", "0.post1"}


def test_pep440_range_accepts_ecosystem_type_and_refuses_git():
    inventory = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                                "packages": [{"name": "demo",
                                              "version": "1.5"}]}]}
    advisory = _range_advisory([{"introduced": "1.0"}, {"fixed": "2.0"}])
    advisory["affected_ranges"][0]["type"] = "ECOSYSTEM"
    assert len(match_local_advisories(
        inventory, _advisories(advisory))["findings"]) == 1

    advisory["affected_ranges"][0]["type"] = "GIT"
    with pytest.raises(ValueError, match="SEMVER or ECOSYSTEM"):
        match_local_advisories(inventory, _advisories(advisory))


def test_pep440_uncomparable_version_is_unknown_not_a_match():
    inventory = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                                "packages": [{"name": "demo",
                                              "version": "1.0+bad local"}]}]}
    advisory = _range_advisory([
        {"introduced": "1.0"}, {"fixed": "2.0"}])

    result = match_local_advisories(inventory, _advisories(advisory))

    assert result["state"] == "no_exact_match_in_supplied_data"
    assert result["findings"] == []
    assert result["pinned_packages"] == 0
    assert result["not_checked"]

    malformed = _range_advisory([{"introduced": "1.0+bad local"}])
    with pytest.raises(ValueError, match="PEP 440"):
        match_local_advisories({"lockfiles": []}, _advisories(malformed))


def test_python_exact_advisories_still_match_and_no_match_is_explicit():
    advisory = _advisory(versions=["2.31.0"])
    matching = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                               "packages": [{"name": "requests",
                                             "version": "2.31.0"}]}]}
    assert len(match_local_advisories(
        matching, _advisories(advisory))["findings"]) == 1

    other = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                            "packages": [{"name": "requests",
                                          "version": "2.32.0"}]}]}
    result = match_local_advisories(other, _advisories(advisory))
    assert result["state"] == "no_exact_match_in_supplied_data"
    assert result["findings"] == []
    assert "no-match" in result["not_checked"]


def test_python_direct_source_does_not_match_a_registry_range_advisory():
    inventory = {"lockfiles": [{"file": "uv.lock", "ecosystem": "python",
                                "packages": [
                                    {"name": "registry-demo", "version": "1.5",
                                     "provenance": "pypi"},
                                    {"name": "fork-demo", "version": "1.5",
                                     "provenance": "git"},
                                    {"name": "path-demo", "version": "1.5",
                                     "provenance": "workspace-or-path"}]}]}
    advisories = _advisories(
        _range_advisory([{"introduced": "1.0"}, {"fixed": "2.0"}],
                        package="registry-demo"),)
    for index, package in enumerate(("fork-demo", "path-demo"), start=2):
        advisories["advisories"].append(_range_advisory(
            [{"introduced": "1.0"}, {"fixed": "2.0"}], package=package,
            identifier=f"OSV-PY-{index}"))

    result = match_local_advisories(inventory, advisories)

    assert {row["dependency"] for row in result["findings"]} == {
        "registry-demo"}
    assert "2 Python pins were excluded" in result["not_checked"]


@pytest.mark.parametrize(("ecosystem", "events"), [
    ("ruby", [{"introduced": "1.0.0"}]),
    ("python", [{"fixed": "1.0.0"}]),
    ("python", [{"introduced": "1.0.0"}, {"fixed": "0.9.0"}]),
    ("python", [{"introduced": "1.0.0"}, {"fixed": "1.0.0"}]),
    ("python", [{"introduced": "1.0.0"}, {"fixed": "2.0.0"},
                {"introduced": "1.5.0"}, {"fixed": "3.0.0"}]),
    ("python", [{"introduced": "1.0.0"}, {"last_affected": "2.0.0"},
                {"fixed": "3.0.0"}]),
    ("python", [{"introduced": "1.0.*"}]),
    ("node", [{"fixed": "1.2.0"}]),
    ("node", [{"introduced": "1.0.0"}, {"fixed": "1.1.0"},
              {"fixed": "1.2.0"}]),
    ("node", [{"introduced": "1.0.0"}, {"fixed": "2.0.0"},
              {"introduced": "1.5.0"}, {"fixed": "3.0.0"}]),
    ("node", [{"introduced": "1.0.0"}, {"fixed": "2.0.0"},
              {"introduced": "2.0.0"}]),
    ("node", [{"introduced": "1.0"}, {"fixed": "2.0.0"}]),
    ("go", [{"introduced": "1.0.0"}]),
])
def test_osv_ranges_reject_unsupported_or_malformed_events(ecosystem, events):
    advisory = {"id": "OSV-TEST-2", "ecosystem": ecosystem,
                "package": "demo", "affected_ranges": [{"events": events}]}
    with pytest.raises(ValueError):
        match_local_advisories({"lockfiles": []}, _advisories(advisory))


def test_advisory_requires_exactly_one_affected_representation():
    both = _advisory(affected_ranges=[{"events": [{"introduced": "1.0.0"}]}])
    with pytest.raises(ValueError, match="exactly one"):
        match_local_advisories({"lockfiles": []}, _advisories(both))


def test_package_keys_follow_inventory_identity_not_cross_ecosystem_guessing():
    inventory = {"lockfiles": [{"file": "Cargo.lock", "ecosystem": "rust",
                                "packages": [{"name": "Foo_Bar",
                                              "version": "1.2.3"}]}]}
    result = match_local_advisories(inventory, _advisories(_advisory(
        ecosystem="rust", package="foo-bar", versions=["1.2.3"])))
    assert result["findings"] == []
    with pytest.raises(ValueError, match="package is invalid"):
        match_local_advisories({"lockfiles": []}, _advisories(_advisory(
            package="Straße")))


def test_ruby_package_matching_preserves_case_sensitive_gem_identity():
    inventory = {"lockfiles": [{"file": "Gemfile.lock", "ecosystem": "ruby",
                                "packages": [{"name": "Rack", "version": "3.0.8",
                                              "source": "rubygems"}]}]}
    lowercase_advisory = _advisory(ecosystem="ruby", package="rack",
                                   versions=["3.0.8"])

    assert match_local_advisories(
        inventory, _advisories(lowercase_advisory))["findings"] == []

    lowercase_advisory["package"] = "Rack"
    assert len(match_local_advisories(
        inventory, _advisories(lowercase_advisory))["findings"]) == 1


@pytest.mark.parametrize(("count", "expected_truncated"), [
    (2000, False), (2001, True),
])
def test_match_cap_only_marks_truncated_when_more_than_cap(count,
                                                            expected_truncated):
    inventory = {"lockfiles": [
        {"file": f"lock-{i}.lock", "ecosystem": "python", "packages": [
            {"name": "requests", "version": "2.31.0"}]}
        for i in range(count)]}
    result = match_local_advisories(inventory, _advisories(_advisory()))
    assert len(result["findings"]) == 2000
    assert result["truncated"] is expected_truncated


def test_dependency_inventory_integrates_explicit_local_advisories(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests>=2"]\n', encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "requests"\nversion = "2.31.0"\n',
        encoding="utf-8")

    report = analyze(tmp_path, local_advisories=_advisories(_advisory()))

    findings = report["findings"]
    assert len(findings) == 1
    assert findings[0]["advisory_id"] == "CVE-TEST-1"
    assert report["inventory"]["local_advisories"]["state"] == "matches_found"
    # A local hit or miss never claims global database coverage.
    assert report["inventory"]["cve_state"] == "unknown"
    assert report["cve_state"] == "unknown"
    assert "supplied local advisory records" in report["not_checked"][-1]["reason"]


def test_bundler_lockfile_matches_only_exact_local_ruby_advisories(tmp_path):
    (tmp_path / "Gemfile.lock").write_text(
        "GEM\n  remote: https://example.invalid/\n  specs:\n"
        "    rack (3.0.8)\n      base64 (>= 0)\n\nPLATFORMS\n  ruby\n",
        encoding="utf-8")
    advisory = _advisory(ecosystem="ruby", package="rack", versions=["3.0.8"])

    report = analyze(tmp_path, local_advisories=_advisories(advisory))

    assert report["inventory"]["lockfiles"][0]["packages"] == [
        {"name": "rack", "version": "3.0.8", "source": "rubygems"}]
    advisory_findings = _non_supply_chain(report["findings"])
    assert len(advisory_findings) == 1
    assert advisory_findings[0]["type"] == "local_advisory_exact_version_match"
    assert advisory_findings[0]["dependency"] == "rack"
    assert report["inventory"]["cve_state"] == "unknown"


def test_ruby_local_advisories_skip_git_and_path_sources(tmp_path):
    (tmp_path / "Gemfile.lock").write_text(
        "GEM\n  remote: https://example.invalid/\n  specs:\n"
        "    rack (3.0.8)\n"
        "GIT\n  remote: https://example.invalid/rack.git\n  revision: abc\n"
        "  specs:\n    rack (3.0.8)\n"
        "PATH\n  remote: vendor/rack\n  specs:\n    rack (3.0.8)\n",
        encoding="utf-8")
    advisory = _advisory(ecosystem="ruby", package="rack", versions=["3.0.8"])

    report = analyze(tmp_path, local_advisories=_advisories(advisory))

    pins = report["inventory"]["lockfiles"][0]["packages"]
    assert {(pin["source"], pin["version"]) for pin in pins} == {
        ("rubygems", "3.0.8"), ("git", "3.0.8"), ("path", "3.0.8")}
    advisory_findings = _non_supply_chain(report["findings"])
    assert len(advisory_findings) == 1
    assert advisory_findings[0]["file"] == "Gemfile.lock"


def test_composer_lock_matches_only_exact_local_advisories(tmp_path):
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "composer.lock").write_text(json.dumps({"packages": [
        {"name": "Vendor/Package", "version": "v2.4.1"},
        {"name": "vendor/branch", "version": "dev-main"},
    ]}), encoding="utf-8")
    advisory = _advisory(ecosystem="composer", package="vendor/package",
                         versions=["2.4.1"])

    report = analyze(tmp_path, local_advisories=_advisories(advisory))

    assert len(report["findings"]) == 1
    assert "dependency:composer:vendor/package:2.4.1" in report["findings"][0]["keys"]
    assert report["findings"][0]["dependency"] == "vendor/package"
    assert report["findings"][0]["version"] == "2.4.1"
    assert any("unsupported package identity/version" in item["reason"]
               for item in report["not_checked"])
    assert report["inventory"]["cve_state"] == "unknown"


def test_gradle_lock_matches_exact_maven_coordinate_versions_only(tmp_path):
    (tmp_path / "gradle.lockfile").write_text(
        "org.example:widget:1.2.3=compileClasspath,runtimeClasspath\n"
        "org.example:widget:1.2.4=runtimeClasspath\n"
        "org.example:Widget:1.2.3=runtimeClasspath\n"
        "org.example:dynamic:1.+=runtimeClasspath\n",
        encoding="utf-8")
    advisory = _advisory(ecosystem="maven", package="org.example:widget",
                         versions=["1.2.3"], severity="blocking")

    report = analyze(tmp_path, local_advisories=_advisories(advisory))

    lock = report["inventory"]["lockfiles"][0]
    assert lock["packages"] == [
        {"name": "org.example:Widget", "version": "1.2.3",
         "source": "gradle.lockfile", "provenance": "repository-unknown"},
        {"name": "org.example:widget", "version": "1.2.3",
         "source": "gradle.lockfile", "provenance": "repository-unknown"},
        {"name": "org.example:widget", "version": "1.2.4",
         "source": "gradle.lockfile", "provenance": "repository-unknown"}]
    matches = [item for item in report["findings"]
               if item["type"] == "local_advisory_exact_version_match"]
    assert len(matches) == 1
    finding = matches[0]
    assert finding["dependency"] == "org.example:widget"
    assert finding["version"] == "1.2.3"
    assert finding["type"] == "local_advisory_exact_version_match"
    assert finding["truth_assessed"] is False
    assert finding["severity"] == "note"
    assert finding["confidence"] == "low"
    assert finding["advisory_severity"] == "blocking"
    assert finding["repository_provenance"] == "unknown"
    assert "coordinate provenance review" in finding["summary"]
    assert "repository origin is not recorded" in " ".join(
        item["reason"] for item in report["not_checked"])
    assert report["inventory"]["cve_state"] == "unknown"


def test_maven_advisory_coordinates_and_versions_are_conservative():
    with pytest.raises(ValueError, match="package is invalid"):
        match_local_advisories({"lockfiles": []}, _advisories(_advisory(
            ecosystem="maven", package="org.example:widget:classifier")))
    with pytest.raises(ValueError, match="exact versions"):
        match_local_advisories({"lockfiles": []}, _advisories(_advisory(
            ecosystem="maven", package="org.example:widget", versions=["1.+"])))
    for version in ("LATEST", "RELEASE", "latest.integration", "1.2.3-SNAPSHOT"):
        with pytest.raises(ValueError, match="exact versions"):
            match_local_advisories({"lockfiles": []}, _advisories(_advisory(
                ecosystem="maven", package="org.example:widget",
                versions=[version])))


@pytest.mark.parametrize("version", [
    "LATEST", "RELEASE", "latest.integration", "1.2.3-SNAPSHOT",
])
def test_gradle_dynamic_or_mutable_versions_never_match_exact_advisories(
        version):
    inventory = {"lockfiles": [{"file": "gradle.lockfile", "ecosystem": "maven",
        "packages": [{"name": "org.example:widget", "version": version,
                      "provenance": "repository-unknown"}]}]}

    result = match_local_advisories(
        inventory, _advisories(_advisory(
            ecosystem="maven", package="org.example:widget",
            versions=["1.2.3"])))

    assert result["findings"] == []


def test_unknown_maven_repository_origin_cannot_trip_fail_on(tmp_path):
    from ozagia.core.deep_review.orchestrator import build_deep_review

    (tmp_path / "gradle.lockfile").write_text(
        "org.example:widget:1.2.3=runtimeClasspath\n", encoding="utf-8")
    advisory = _advisory(ecosystem="maven", package="org.example:widget",
                         versions=["1.2.3"], severity="blocking")

    result = build_deep_review(
        tmp_path, tmp_path / "report", profile="dependencies",
        local_advisories=_advisories(advisory), fail_on="major")

    candidate = next(item for item in result["findings"]
                     if item["type"] == "local_advisory_exact_version_match")
    assert candidate["severity"] == "note"
    assert candidate["evidence"][0]["advisory_severity"] == "blocking"
    assert candidate["evidence"][0]["repository_provenance"] == "unknown"
    assert result["failed"] is False


def test_composer_custom_git_repository_suppresses_ambiguous_advisory_matches(
        tmp_path):
    (tmp_path / "composer.json").write_text(json.dumps({"repositories": [
        {"type": "vcs", "url": "https://example.invalid/fork.git"}]}),
        encoding="utf-8")
    (tmp_path / "composer.lock").write_text(json.dumps({"packages": [{
        "name": "vendor/package", "version": "2.4.1",
        "source": {"type": "git", "url": "https://example.invalid/fork.git"},
        "dist": {"type": "zip", "url": "https://example.invalid/archive.zip"},
    }]}), encoding="utf-8")
    advisory = _advisory(ecosystem="composer", package="vendor/package",
                         versions=["2.4.1"])

    report = analyze(tmp_path, local_advisories=_advisories(advisory))

    assert _non_supply_chain(report["findings"]) == []
    lock = report["inventory"]["lockfiles"][0]
    assert lock["custom_repositories"] is True
    assert lock["packages"][0]["provenance"] == "git"
    component = next(item for item in report["sbom"]["components"]
                     if item["name"] == "vendor/package")
    assert {item["value"] for item in component["properties"]
            if item["name"] == "ozagia:provenance"} == {"git"}
    assert "provenance ambiguous" in " ".join(
        item["reason"] for item in report["not_checked"])
    serialized = json.dumps(report)
    assert "example.invalid" not in serialized


def test_composer_path_package_is_excluded_from_advisory_matching(tmp_path):
    (tmp_path / "composer.json").write_text("{}", encoding="utf-8")
    (tmp_path / "composer.lock").write_text(json.dumps({"packages": [{
        "name": "vendor/package", "version": "2.4.1",
        "dist": {"type": "path", "url": "../local-package"},
    }]}), encoding="utf-8")
    advisory = _advisory(ecosystem="composer", package="vendor/package",
                         versions=["2.4.1"])

    report = analyze(tmp_path, local_advisories=_advisories(advisory))

    assert _non_supply_chain(report["findings"]) == []
    assert report["inventory"]["lockfiles"][0]["packages"][0]["provenance"] == "path"
    component = next(item for item in report["sbom"]["components"]
                     if item["name"] == "vendor/package")
    assert {item["value"] for item in component["properties"]
            if item["name"] == "ozagia:provenance"} == {"path"}
    assert "path-based" in " ".join(
        item["reason"] for item in report["not_checked"])


def test_composer_lock_without_composer_json_keeps_advisory_provenance_unknown(
        tmp_path):
    (tmp_path / "composer.lock").write_text(json.dumps({"packages": [{
        "name": "vendor/package", "version": "2.4.1"}]}),
        encoding="utf-8")
    advisory = _advisory(ecosystem="composer", package="vendor/package",
                         versions=["2.4.1"])

    report = analyze(tmp_path, local_advisories=_advisories(advisory))

    assert report["findings"] == []
    assert report["inventory"]["lockfiles"][0]["advisory_matchable"] is False
    assert "composer.json is missing or invalid" in " ".join(
        item["reason"] for item in report["not_checked"])


def test_composer_advisories_reject_range_and_nonexact_versions():
    with pytest.raises(ValueError, match="exact versions"):
        match_local_advisories(
            {"lockfiles": []}, _advisories(_advisory(
                ecosystem="composer", package="vendor/package",
                versions=["1.2.x-dev"])))
    with pytest.raises(ValueError, match="unsupported for this ecosystem"):
        match_local_advisories({"lockfiles": []}, _advisories({
            "id": "OSV-COMPOSER-1", "ecosystem": "composer",
            "package": "vendor/package",
            "affected_ranges": [{"events": [{"introduced": "1.0.0"}]}]}))


@pytest.mark.parametrize("version", ["v1.2.3", "1.2.3-x86_64-linux", "1..2"])
def test_ruby_advisories_reject_nonconservative_versions(version):
    with pytest.raises(ValueError, match="exact versions"):
        match_local_advisories(
            {"lockfiles": []},
            _advisories(_advisory(ecosystem="ruby", package="rack",
                                  versions=[version])))


def test_advisory_file_interface_is_offline_and_hash_stable(tmp_path):
    path = tmp_path / "advisories.json"
    path.write_text(json.dumps(_advisories(_advisory())), encoding="utf-8")
    inventory = {"lockfiles": [{"ecosystem": "python", "file": "uv.lock",
                                "packages": [{"name": "requests",
                                              "version": "2.31.0"}]}]}
    first = match_local_advisories(inventory, path)
    second = match_local_advisories(inventory, path)
    assert first == second


def test_deep_review_cli_passes_only_explicit_advisory_path(tmp_path, monkeypatch):
    from ozagia import cli
    from ozagia.core import deep_review

    calls = []

    def fake_build(root, out, **kwargs):
        calls.append(kwargs)
        out.mkdir(parents=True, exist_ok=True)
        manifest = {"schema": "ozagia.deep_review.manifest.v1"}
        return {"manifest": manifest, "findings": [], "out": str(out),
                "failed": False}

    monkeypatch.setattr(deep_review, "build_deep_review", fake_build)
    db = tmp_path / "local-advisories.json"
    db.write_text("{}", encoding="utf-8")
    out = tmp_path.parent / (tmp_path.name + "-local-cve-report")
    assert cli.main(["deep-review", "--root", str(tmp_path), "--out", str(out),
                     "--profile", "dependencies", "--local-advisories",
                     str(db)]) == 0
    assert calls[0]["local_advisories"] == str(db)

    calls.clear()
    assert cli.main(["deep-review", "--root", str(tmp_path), "--out",
                     str(out) + "-default"]) == 0
    assert "local_advisories" not in calls[0]


def test_osv_last_affected_is_inclusive_and_honors_semver_prerelease_order():
    inventory = {"lockfiles": [{"file": "package-lock.json", "ecosystem": "node",
                                "packages": [{"name": "demo", "version": v} for v in (
                                    "1.0.0", "1.1.0-rc.1", "1.1.0", "1.1.1")]}]}
    advisory = {"id": "OSV-LAST-1", "ecosystem": "node", "package": "demo",
                "affected_ranges": [{"events": [{"introduced": "1.0.0"},
                                                   {"last_affected": "1.1.0"}]}]}
    result = match_local_advisories(inventory, _advisories(advisory))
    assert {row["version"] for row in result["findings"]} == {
        "1.0.0", "1.1.0-rc.1", "1.1.0"}


def test_osv_last_affected_may_equal_introduction_but_cannot_mix_fixed():
    inventory = {"lockfiles": [{"file": "Cargo.lock", "ecosystem": "rust",
                                "packages": [{"name": "demo", "version": "1.0.0"}]}]}
    advisory = {"id": "OSV-LAST-2", "ecosystem": "rust", "package": "demo",
                "affected_ranges": [{"events": [{"introduced": "1.0.0"},
                                                   {"last_affected": "1.0.0"}]}]}
    assert len(match_local_advisories(inventory, _advisories(advisory))["findings"]) == 1
    advisory["affected_ranges"][0]["events"].extend(
        [{"introduced": "2.0.0"}, {"fixed": "3.0.0"}])
    with pytest.raises(ValueError, match="cannot mix"):
        match_local_advisories(inventory, _advisories(advisory))


def test_osv_last_affected_go_and_multiple_intervals():
    inventory = {"lockfiles": [{"file": "go.sum", "ecosystem": "go",
                                "packages": [{"name": "example.com/m", "version": v}
                                             for v in ("v1.0.0", "v1.0.1", "v2.0.0")]}]}
    advisory = {"id": "OSV-LAST-GO", "ecosystem": "go", "package": "example.com/m",
                "affected_ranges": [{"type": "SEMVER", "events": [
                    {"introduced": "0"}, {"last_affected": "v1.0.0"},
                    {"introduced": "v2.0.0"}]}]}
    result = match_local_advisories(inventory, _advisories(advisory))
    assert {row["version"] for row in result["findings"]} == {"1.0.0", "2.0.0"}


def test_osv_range_rejects_non_semver_type_and_invalid_limit():
    advisory = {"id": "OSV-TYPE-1", "ecosystem": "node", "package": "demo",
                "affected_ranges": [{"type": "GIT", "events": [{"introduced": "0"}]}]}
    with pytest.raises(ValueError, match="SEMVER"):
        match_local_advisories({"lockfiles": []}, _advisories(advisory))
    advisory["affected_ranges"][0] = {"events": [
        {"introduced": "0"}, {"limit": "not-a-version"}]}
    with pytest.raises(ValueError):
        match_local_advisories({"lockfiles": []}, _advisories(advisory))


def test_osv_limits_are_exclusive_and_apply_to_the_whole_range():
    inventory = {"lockfiles": [{"file": "package-lock.json", "ecosystem": "node",
                                "packages": [{"name": "demo", "version": v} for v in
                                             ("1.0.0", "1.5.0", "2.0.0", "3.0.0")]}]}
    advisory = {"id": "OSV-LIMIT-1", "ecosystem": "node", "package": "demo",
                "affected_ranges": [{"events": [{"introduced": "0"},
                    {"limit": "1.5.0"}, {"limit": "2.0.0"}]}]}
    result = match_local_advisories(inventory, _advisories(advisory))
    assert {row["version"] for row in result["findings"]} == {"1.0.0", "1.5.0"}
    advisory["affected_ranges"][0]["events"].append({"limit": "*"})
    result = match_local_advisories(inventory, _advisories(advisory))
    assert {row["version"] for row in result["findings"]} == {"1.0.0", "1.5.0", "2.0.0", "3.0.0"}


def test_osv_limit_clips_inclusive_last_affected_and_is_per_range():
    inventory = {"lockfiles": [{"file": "go.sum", "ecosystem": "go",
                                "packages": [{"name": "example.com/m", "version": v} for v in
                                             ("v1.0.0", "v2.0.0", "v3.0.0")]}]}
    advisory = {"id": "OSV-LIMIT-2", "ecosystem": "go", "package": "example.com/m",
                "affected_ranges": [{"events": [{"introduced": "0"},
                    {"last_affected": "v2.0.0"}, {"limit": "v2.0.0"}]},
                    {"events": [{"introduced": "v3.0.0"}]}]}
    result = match_local_advisories(inventory, _advisories(advisory))
    assert {row["version"] for row in result["findings"]} == {"1.0.0", "3.0.0"}


def test_go_checksum_advisory_is_unconfirmed_even_for_high_severity():
    inventory = {"lockfiles": [{"file": "go.sum", "ecosystem": "go",
                                "format": "go.sum", "packages": [
                                    {"name": "example.com/mod", "version": "v1.2.3"}]}]}
    report = match_local_advisories(
        inventory, _advisories(_advisory(ecosystem="go", package="example.com/mod",
                                          versions=["v1.2.3"], severity="major")))
    finding = report["findings"][0]
    assert finding["severity"] == "note"
    assert finding["confidence"] == "low"
    assert finding["advisory_severity"] == "major"
    assert finding["module_selection"] == "unknown"
    assert "does not establish" in finding["summary"]
    assert "build list" in finding["recommendation"]
