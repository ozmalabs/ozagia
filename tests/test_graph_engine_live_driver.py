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


def test_known_injection_only_uses_seen_outline_hashes() -> None:
    driver = _driver()
    recorder = driver.Recorder()
    arm = driver.EngineArm(object(), recorder, interface="optimized",
                           known_reuse=True, speculate=False)
    arm._track_seen({"c": ["k", "h"], "r": [["sym:a", "h1"]]})
    assert arm.seen == {"sym:a": "h1"}

    outline = {"op": "outline", "key": "sym:a"}
    arm._inject_known("outline", outline)
    assert outline["known"] == [{"key": "sym:a", "hash": "h1"}]
    assert recorder.known_sent == 1

    find = {"op": "find", "q": "a"}
    arm._inject_known("find", find)
    assert "known" not in find

    # A file-outline refresh sees the whole remembered row set, not only the
    # requested file key; the engine ignores hashes for rows it does not return.
    arm._track_seen({"c": ["k", "h"],
                     "r": [["file:src/a.py", "hf"],
                           ["sym:src/a.py::b", "hb"]]})
    refresh = {"op": "outline", "key": "file:src/a.py"}
    arm._inject_known("outline", refresh)
    assert {row["key"] for row in refresh["known"]} == {
        "sym:a", "file:src/a.py", "sym:src/a.py::b"}
    assert recorder.known_sent == 4

    # An explicit known list is never overwritten.
    explicit = {"op": "outline", "key": "sym:a",
                "known": [{"key": "sym:a", "hash": "other"}]}
    arm._inject_known("outline", explicit)
    assert explicit["known"][0]["hash"] == "other"


def test_speculate_flag_sets_find_spec() -> None:
    driver = _driver()

    class FakeClient:
        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def graph(self, payload):
            self.payloads.append(dict(payload))
            return {"sid": "s1", "t": 1}, 10

    for speculate, expected in ((False, False), (True, True)):
        recorder = driver.Recorder()
        client = FakeClient()
        arm = driver.EngineArm(client, recorder, interface="optimized",
                               known_reuse=True, speculate=speculate)
        arm.call({"op": "find", "q": "a"})
        finds = [p for p in client.payloads if p.get("op") == "find"]
        assert finds and finds[-1]["spec"] is expected


def test_multiturn_tasks_are_three_fixed_turns() -> None:
    driver = _driver()
    for name in ("flask", "tiny"):
        tasks = driver.MULTI_TASKS[name]
        assert tasks
        for task in tasks:
            assert len(task["turns"]) == 3
            for turn in task["turns"]:
                assert turn["prompt"].strip()
                assert turn["keys"]
                assert turn["checks"]
    flask = driver.MULTI_TASKS["flask"]
    spans = {"multiturn_get_root_path": (577, 631),
             "multiturn_send_from_directory": (533, 574)}
    for task in flask:
        refresh = task["turns"][1]
        assert refresh["id"] in ("t2_refresh_outline",
                                 "t2_refresh_file_outline")
        expected = spans.get(task["id"])
        if expected is None:
            continue
        for check_name, check in refresh["checks"]:
            if "start_line" in check_name:
                assert check({"start_line": expected[0]})
                assert not check({"start_line": expected[0] + 1})
            if "end_line" in check_name:
                assert check({"end_line": expected[1]})
                assert not check({"end_line": expected[1] + 1})
