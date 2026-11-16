from __future__ import annotations

from ozagia.core.infra_discovery import (
    iter_candidate_paths,
    iter_sources,
    skipped_summary,
    source_kind,
)


def test_source_kinds_exclude_state_vars_and_credentials():
    for name in ("terraform.tfstate", "backup.tfstate.json", "secret.tfvars.json",
                 ".env.yaml", "kubeconfig.json", "client.key"):
        assert source_kind(name) is None
    assert source_kind("main.tf.json") == "terraform"
    assert source_kind("versions.tofu") == "terraform"
    assert source_kind("deployment.yaml") == "manifest"


def test_discovery_skips_state_dirs_and_symlinks(tmp_path):
    (tmp_path / "main.tf").write_text('resource "x" "y" {}')
    (tmp_path / "secret.tfvars").write_text('password="CANARY"')
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "main.tf").write_text("CANARY")
    (tmp_path / ".ozagia").mkdir()
    (tmp_path / ".ozagia" / "output.json").write_text("CANARY")
    (tmp_path / "linked.yaml").symlink_to(tmp_path / "secret.tfvars")
    assert [(path, kind) for path, kind, _ in iter_sources(tmp_path)] == [
        ("main.tf", "terraform")]


def test_default_backup_globs_skip_files_and_directories_and_report_paths(
        tmp_path):
    (tmp_path / "envs" / "prod").mkdir(parents=True)
    (tmp_path / "envs" / "prod" / "main.tf").write_text(
        'resource "aws_vpc" "prod" {}')
    (tmp_path / "envs" / "prod.backup.last").mkdir()
    (tmp_path / "envs" / "prod.backup.last" / "main.tf").write_text(
        'resource "aws_vpc" "duplicate" {}')
    (tmp_path / "main.tf.bak").write_text("ignored")
    (tmp_path / "manifest.yaml.orig").write_text("ignored")
    paths = {path for path, _ in iter_candidate_paths(tmp_path)}
    assert paths == {"envs/prod/main.tf"}
    summary = skipped_summary(tmp_path)
    assert summary["count"] == 3
    assert set(summary["paths"]) == {
        "envs/prod.backup.last", "main.tf.bak", "manifest.yaml.orig",
    }


def test_custom_iac_skip_glob_merges_with_defaults_without_skipping_other_sources(
        tmp_path):
    (tmp_path / ".ozagia.toml").write_text(
        '[iac]\nskip_globs = ["generated/**"]\n')
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "main.tf").write_text("ignored")
    (tmp_path / "custom-source").mkdir()
    (tmp_path / "custom-source" / "main.tf").write_text("included")
    (tmp_path / "backup.bak").write_text("ignored")
    assert {path for path, _ in iter_candidate_paths(tmp_path)} == {
        "custom-source/main.tf",
    }
    assert set(skipped_summary(tmp_path)["paths"]) == {
        "generated", "backup.bak",
    }
