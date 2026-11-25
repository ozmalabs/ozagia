"""Model-facing live-driver interface: conditional fetch and compact schema.

These tests do not call a model or an engine; they pin the driver's schema and
arm behaviour that the live A/B measured.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _driver():
    path = ROOT / "scripts" / "eval_graph_engine_live.py"
    spec = importlib.util.spec_from_file_location(
        "eval_graph_engine_live", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_optimized_schema_withholds_fetch_until_handles() -> None:
    driver = _driver()
    base = driver._opt_schema(False)
    with_fetch = driver._opt_schema(True)
    assert "fetch" not in base["properties"]
    assert "fetch" not in base["properties"]["op"]["enum"]
    assert with_fetch["properties"]["fetch"]["type"] == "array"
    assert "fetch" in with_fetch["properties"]["op"]["enum"]
    assert base == driver._control_schema()
    assert base["additionalProperties"] is False


def test_optimized_arm_blocks_fetch_without_handles() -> None:
    driver = _driver()
    recorder = driver.Recorder()
    arm = driver.EngineArm(object(), recorder, interface="optimized")
    result, wire = arm.call({"op": "fetch"})
    assert wire == 0
    assert result["e"] == "no_handles"
    assert recorder.fetch_blocked == 1
    assert not arm.has_handles()
    arm._track_handles("find", {"q": {"h": ["q1"]}})
    assert arm.has_handles()
    arm._track_handles("fetch", {"h": "q1", "fs": "pending"})
    assert arm.has_handles()
    arm._track_handles("fetch", {"h": "q1"})
    assert not arm.has_handles()


def test_schema_overhead_is_not_larger_than_control() -> None:
    driver = _driver()
    profile = driver.interface_profile()
    assert (profile["engine_opt_no_handles"]["fixed_tokens"]
            == profile["control"]["fixed_tokens"])
    assert (profile["engine_legacy"]["fixed_tokens"]
            > profile["control"]["fixed_tokens"])
    assert (profile["engine_opt_with_fetch"]["fixed_tokens"]
            > profile["engine_opt_no_handles"]["fixed_tokens"])


def test_shape_prompt_asks_for_repository_relative_file_paths() -> None:
    driver = _driver()
    shape = next(task for task in driver.TASKS
                 if task["id"] == "repository_shape")
    assert "file paths" in shape["prompt"]
    assert "pkg/core.py" in shape["prompt"]
