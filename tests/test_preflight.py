import os

import pytest

from ozagia.core.preflight import build_preflight, execute_preflight


def build(tmp_path, roots=None, dependencies=None, **kwargs):
    if roots is None:
        roots = ["network", "app", "database"]
    if dependencies is None:
        dependencies = {"app": ["network"], "database": ["network"]}
    return build_preflight(
        roots,
        dependencies,
        emulator_bin="/opt/emulator/1.2.3/bin/emulator",
        **kwargs,
    )


def test_topological_order_respects_dependencies_and_order_tiebreak(tmp_path):
    plan = build(
        tmp_path,
        roots=["app", "database", "network"],
        dependencies={"app": ["network"], "database": ["network"]},
        order=["database", "app", "network"],
    )

    assert [os.path.basename(root) for root in plan.roots] == [
        "network",
        "database",
        "app",
    ]
    assert plan.commands[0].argv == (
        "/opt/emulator/1.2.3/bin/emulator",
        "preflight",
        "--root",
        plan.roots[0],
    )


def test_rejects_dependency_cycle(tmp_path):
    with pytest.raises(ValueError, match="cycle"):
        build(
            tmp_path,
            roots=["one", "two"],
            dependencies={"one": ["two"], "two": ["one"]},
        )


def test_rejects_duplicate_and_escaping_roots(tmp_path):
    with pytest.raises(ValueError, match="duplicate"):
        build(tmp_path, roots=["one", "./one"], dependencies={})
    with pytest.raises(ValueError, match="escapes"):
        build(tmp_path, roots=["../outside"], dependencies={})
    with pytest.raises(ValueError, match="escapes"):
        build(tmp_path, roots=[str(tmp_path / ".." / "outside")], dependencies={})


def test_build_uses_minimal_scrubbed_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_PROFILE", "profile-canary")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "access-canary")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-canary")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-canary")
    monkeypatch.setenv("UNRELATED_SECRET", "also-canary")
    plan = build(tmp_path, roots=["root"], dependencies={})

    assert plan.environment["AWS_EC2_METADATA_DISABLED"] == "true"
    assert plan.environment["CHECKPOINT_DISABLE"] == "1"
    assert not {
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "UNRELATED_SECRET",
    } & plan.environment.keys()
    assert all("canary" not in value for value in plan.environment.values())


def test_dry_run_does_not_call_runner(tmp_path):
    calls = []
    plan = build(tmp_path, roots=["root"], dependencies={})

    assert plan.roots
    assert calls == []


def test_execution_requires_both_explicit_gates_and_uses_only_injected_runner(tmp_path):
    plan = build(tmp_path, roots=["root"], dependencies={})
    calls = []

    def runner(argv, *, cwd, env):
        calls.append((argv, cwd, env))
        return 0

    with pytest.raises(PermissionError):
        execute_preflight(
            plan,
            runner=runner,
            emulator_bin=plan.emulator_bin,
        )
    with pytest.raises(ValueError, match="explicit"):
        execute_preflight(plan, runner=runner, allow_execute=True)
    result = execute_preflight(
        plan,
        runner=runner,
        allow_execute=True,
        emulator_bin=plan.emulator_bin,
    )

    assert result.success
    assert result.exit_codes == (0,)
    assert calls[0][0] == plan.commands[0].argv
    assert calls[0][2]["AWS_EC2_METADATA_DISABLED"] == "true"


def test_fake_runner_failure_stops_remaining_roots(tmp_path):
    plan = build(tmp_path)
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv)
        return 17

    result = execute_preflight(
        plan,
        runner=runner,
        allow_execute=True,
        emulator_bin=plan.emulator_bin,
    )
    assert not result.success
    assert result.exit_codes == (17,)
    assert len(calls) == 1


def test_provider_version_note_is_explicitly_unverified(tmp_path):
    plan = build(
        tmp_path,
        roots=["root"],
        dependencies={},
        provider_versions={"aws": "5.70.0", "kubernetes": "2.30.0"},
    )

    assert plan.provider_versions == (("aws", "5.70.0"), ("kubernetes", "2.30.0"))
    assert "not verified" in plan.provider_version_note
    assert "aws=5.70.0" in plan.provider_version_note


def test_does_not_guess_or_inherit_cloud_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    plan = build(tmp_path, roots=["root"], dependencies={})

    assert "AWS_REGION" not in plan.environment
    assert "AWS_DEFAULT_REGION" not in plan.environment
    assert "AWS_PROFILE" not in plan.environment
