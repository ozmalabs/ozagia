from __future__ import annotations

from ozagia.core.infra_discovery import iter_sources, source_kind


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
