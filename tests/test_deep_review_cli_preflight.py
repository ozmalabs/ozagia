"""Preflight refusal and partial collector failure behavior for deep review."""
from __future__ import annotations

import json
import hashlib
import multiprocessing
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest


def _reasons(values):
    return [str(value.get("reason", value) if isinstance(value, dict) else value)
            for value in values or []]


def _parallel_probe(output_dir, name, delay_s):
    output_dir = Path(output_dir)
    start = time.monotonic_ns()
    (output_dir / f"{name}.start").write_text(str(start), encoding="ascii")
    time.sleep(delay_s)
    end = time.monotonic_ns()
    (output_dir / f"{name}.end").write_text(str(end), encoding="ascii")
    return name


def _parallel_failure():
    raise ValueError("deliberate analyzer failure")


def _disable_resource_admission(root):
    """Keep these report tests independent of the host's free-disk reserve."""
    policy_dir = root / "ozagia"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "resources.toml").write_text(
        "enabled = false\n", encoding="utf-8")


def test_in_process_analyzer_timeout_is_hard_bounded_and_reaped():
    import ozagia.core.deep_review.collect as collector

    def stuck():
        time.sleep(5)
        return "late"

    started = time.monotonic()
    try:
        collector._bounded_call(stuck, timeout=0.05)
    except TimeoutError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("stuck analyzer unexpectedly returned")
    assert time.monotonic() - started < 2


def test_partial_analyzer_pipe_frame_cannot_exceed_deadline(monkeypatch):
    import ozagia.core.deep_review.collect as collector

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("partial-frame injection requires fork inheritance")

    def partial_frame(send, function, args, kwargs):
        send._send(b"\x00\x00")
        time.sleep(5)

    monkeypatch.setattr(collector, "_isolated_call_child", partial_frame)
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out"):
        collector._bounded_call(lambda: None, timeout=0.05)
    assert time.monotonic() - started < 2


def test_analyzer_success_payload_is_rejected_if_child_exits_abnormally(
        monkeypatch):
    import ozagia.core.deep_review.collect as collector

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("child exit injection requires fork inheritance")

    def send_then_fail(send, function, args, kwargs):
        send.send_bytes(collector.pickle.dumps((True, "partial")))
        os._exit(7)

    monkeypatch.setattr(collector, "_isolated_call_child", send_then_fail)
    with pytest.raises(RuntimeError, match="exited with status 7"):
        collector._bounded_call(lambda: "complete", timeout=1)


def test_analyzer_preflight_timeout_is_killable_and_reported(tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("preflight injection requires fork inheritance")

    def stuck(root):
        time.sleep(5)

    monkeypatch.setattr(collector.repo_review, "preflight", stuck)
    started = time.monotonic()
    packet, refusal = collector._analyzer_preflight(
        tmp_path, index=False, budget_s=0.05, started=started)

    assert time.monotonic() - started < 2
    assert refusal
    assert packet["preflight"]["ok"] is False
    assert "timed out" in packet["preflight"]["reason"]
    assert packet["graph"]["freshness"] == "unknown"


def test_parallel_analyzers_overlap_spawn_workers_and_merge_in_task_order(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector

    methods = []

    def inline_bounded_call(function, *args, timeout, start_method=None,
                            **kwargs):
        methods.append((threading.current_thread().name, start_method))
        return function(*args, **kwargs)

    monkeypatch.setattr(collector, "_bounded_call", inline_bounded_call)
    tasks = [
        ("second", _parallel_probe, (tmp_path, "second", 0.4), {}),
        ("first", _parallel_probe, (tmp_path, "first", 0.4), {}),
    ]
    results = collector._parallel_analyzers(
        tasks, started=time.monotonic(), budget_s=5)

    assert list(results) == ["second", "first"]
    assert results == {"second": (True, "second"),
                       "first": (True, "first")}
    first = (int((tmp_path / "first.start").read_text(encoding="ascii")),
             int((tmp_path / "first.end").read_text(encoding="ascii")))
    second = (int((tmp_path / "second.start").read_text(encoding="ascii")),
              int((tmp_path / "second.end").read_text(encoding="ascii")))
    assert max(first[0], second[0]) < min(first[1], second[1])
    assert len(methods) == 2
    assert all(name.startswith("ozagia-review") and method == "spawn"
               for name, method in methods)


def test_parallel_analyzer_budget_timeout_and_failure_are_isolated(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector

    def bounded_call(function, *args, timeout, start_method=None, **kwargs):
        if args and isinstance(args[-1], (int, float)) and args[-1] > timeout:
            raise TimeoutError(f"collector timed out after {timeout:g} seconds")
        return function(*args, **kwargs)

    monkeypatch.setattr(collector, "_bounded_call", bounded_call)

    tasks = [
        ("slow", _parallel_probe, (tmp_path, "slow", 5.0), {}),
        ("failure", _parallel_failure, (), {}),
        ("quick", _parallel_probe, (tmp_path, "quick", 0.05), {}),
    ]
    started = time.monotonic()
    results = collector._parallel_analyzers(
        tasks, started=started, budget_s=2.0)
    elapsed = time.monotonic() - started

    assert isinstance(results["slow"][1], TimeoutError)
    assert results["failure"][0] is False
    assert isinstance(results["failure"][1], ValueError)
    assert results["quick"] == (True, "quick")
    assert elapsed < 3.5


def test_parallel_analyzers_share_budget_across_queued_lanes(tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector

    barrier = threading.Barrier(6)
    assigned = {}

    def bounded_call(function, *args, timeout, start_method=None, **kwargs):
        name = args[1]
        assigned[name] = timeout
        if name != "worker-6":
            barrier.wait(timeout=2)
        if timeout < args[2]:
            raise TimeoutError("shared review deadline reached")
        return function(*args, **kwargs)

    monkeypatch.setattr(collector, "_bounded_call", bounded_call)
    tasks = [(f"worker-{index}", _parallel_probe,
              (tmp_path, f"worker-{index}", 0.3), {})
             for index in range(7)]
    # Simulate half of the overall budget already consumed by preflight and
    # the serial repository/security collection phases.
    started = time.monotonic() - 0.5
    results = collector._parallel_analyzers(
        tasks, started=started, budget_s=1.0)

    assert all(results[f"worker-{index}"][0] for index in range(6))
    assert results["worker-6"][0] is False
    assert isinstance(results["worker-6"][1], TimeoutError)
    assert all(assigned[f"worker-{index}"] > 0.35 for index in range(6))
    assert 0 < assigned["worker-6"] < 0.3


def test_parallel_failure_merges_explicit_coverage_omission(tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector

    packet = {"findings": [], "sections": {},
              "graph": {"freshness": "fresh"}}
    monkeypatch.setattr(collector, "_analyzer_preflight",
                        lambda *args, **kwargs: (packet, None))
    monkeypatch.setattr(collector, "_parallel_analyzers",
                        lambda tasks, **kwargs: {"static": (
                            False, TimeoutError("budget elapsed"))})

    result = collector.collect(tmp_path, profile="performance", budget_s=5)

    reasons = _reasons(result["sections"]["performance"]["not_checked"])
    assert any("static Python analysis unavailable: TimeoutError: budget elapsed"
               in reason for reason in reasons)


def test_safe_worker_ignores_target_controlled_ozagia_shadow(tmp_path):
    import ozagia.core.deep_review.collect as collector
    from ozagia.core.deep_review.analyzers import data

    marker = tmp_path / "target-imported"
    fake_package = tmp_path / "ozagia"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8")

    result = collector._safe_subprocess_call(
        data.analyze, (tmp_path,), {"budget_s": 2.0}, timeout=5.0)

    assert isinstance(result, dict)
    assert not marker.exists()


def test_safe_worker_group_signal_falls_back_to_direct_process(
        monkeypatch):
    import signal
    import ozagia.core.deep_review.collect as collector

    if os.name != "posix":
        pytest.skip("process-group signaling requires POSIX")

    class Worker:
        pid = 12345

        def __init__(self):
            self.calls = []

        def terminate(self):
            self.calls.append("terminate")

        def kill(self):
            self.calls.append("kill")

    def denied(*_args):
        raise PermissionError("group signal denied")

    monkeypatch.setattr(collector.os, "killpg", denied)
    term_worker = Worker()
    collector._signal_safe_worker(term_worker, signal.SIGTERM)
    assert term_worker.calls == ["terminate"]

    kill_worker = Worker()
    collector._signal_safe_worker(kill_worker, signal.SIGKILL, force=True)
    assert kill_worker.calls == ["kill"]


def test_safe_worker_timeout_bounds_a_child_that_never_reads_stdin(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector
    from ozagia.core.deep_review.analyzers import data

    fake_python = tmp_path / "fake-python"
    fake_python.write_text("#!/bin/sh\nexec /bin/sleep 10\n", encoding="utf-8")
    fake_python.chmod(0o755)
    monkeypatch.setattr(collector.sys, "executable", str(fake_python))
    started = time.monotonic()

    with pytest.raises(TimeoutError,
                       match="collector timed out at its assigned time limit"):
        collector._safe_subprocess_call(
            data.analyze, ("x" * 2_000_000,), {}, timeout=0.1)

    assert time.monotonic() - started < 2


def test_bounded_collector_timeout_kills_subprocess_descendants(tmp_path):
    import sys
    import ozagia.core.deep_review.collect as collector

    if "fork" not in multiprocessing.get_all_start_methods() or os.name != "posix":
        pytest.skip("process-group cleanup requires POSIX fork")
    counter = tmp_path / "child-runs"
    pid_file = tmp_path / "child.pid"

    def spawns_child_then_stalls():
        code = (
            "import os,sys,time\n"
            "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
            "while True:\n"
            "    open(sys.argv[1], 'a').write('x')\n"
            "    time.sleep(.02)\n"
        )
        subprocess.Popen([sys.executable, "-c", code, str(counter),
                          str(pid_file)])
        time.sleep(5)

    try:
        with pytest.raises(TimeoutError, match="timed out"):
            collector._bounded_call(spawns_child_then_stalls, timeout=0.15)
        assert pid_file.read_text()  # child started before timeout
        first_count = counter.stat().st_size
        time.sleep(0.1)
        assert counter.stat().st_size == first_count
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except (ProcessLookupError, ValueError):
                pass


def test_analyzer_profile_records_preflight_refusal_and_keeps_static_findings(
        tmp_path, monkeypatch):
    import ozagia.core.deep_review.collect as collector
    from ozagia.core.repo_review import RepoReviewError

    (tmp_path / "app.py").write_text(
        "def nested(items):\n"
        "    for x in items:\n"
        "        for y in items:\n"
        "            pass\n", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise RepoReviewError("not a git repository")

    monkeypatch.setattr(collector.repo_review, "preflight", refuse)

    packet = collector.collect(tmp_path, profile="performance", budget_s=5)

    assert any(item["type"] == "repeated_nested_iteration"
               for item in packet["findings"])
    coverage = packet["sections"]["performance"]
    assert any("repository preflight unavailable" in reason
               for reason in _reasons(coverage["not_checked"]))
    assert packet["graph"]["freshness"] == "unknown"


def test_refused_base_preflight_still_writes_a_partial_report(tmp_path, monkeypatch):
    from ozagia.core.deep_review import build_deep_review
    import ozagia.core.deep_review.collect as collector
    from ozagia.core.repo_review import PreflightError

    (tmp_path / "app.py").write_text("def f():\n    return 1\n",
                                     encoding="utf-8")

    def refuse(*args, **kwargs):
        raise PreflightError("repository preflight refused: git.head", {
            "ok": False, "reason": "git.head", "checks": []})

    monkeypatch.setattr(collector.repo_review, "review_repo", refuse)

    out = tmp_path / "report"
    result = build_deep_review(tmp_path, out, profile="security", budget_s=5)

    assert (out / "README.md").is_file()
    assert (out / "findings.json").is_file()
    assert json.loads((out / "manifest.json").read_text(
        encoding="utf-8"))["schema"] == "ozagia.deep_review.manifest.v1"
    security = result["manifest"]["subjects"]["security"]
    assert any("preflight refused" in reason
               for reason in _reasons(security["omitted"]))
    assert security["status"] == "partial"


def test_static_collector_timeout_is_partial_but_report_still_composes(
        tmp_path, monkeypatch):
    from ozagia.core.deep_review import build_deep_review
    import ozagia.core.deep_review.collect as collector

    (tmp_path / "app.py").write_text("def f():\n    return 1\n",
                                     encoding="utf-8")
    monkeypatch.setattr(collector.repo_review, "preflight", lambda root: {
        "ok": True, "root": str(tmp_path), "head": None, "reason": None,
        "checks": []})
    monkeypatch.setattr(
        collector.repo_review, "_load_graph",
        lambda root, graph_file, head: (
            {"freshness": "unknown", "reason": "graph not built"}, None, None))

    def timeout(*args, **kwargs):
        raise TimeoutError("static analysis timed out")

    monkeypatch.setattr(collector.analyzers, "analyze_static", timeout)

    out = tmp_path / "report"
    result = build_deep_review(tmp_path, out, profile="performance", budget_s=5)

    assert (out / "README.md").is_file()
    performance = result["manifest"]["subjects"]["performance"]
    assert performance["status"] in {"partial", "unknown"}
    assert any("static analysis timed out" in reason
               for reason in _reasons(performance["omitted"]))


def test_analyzer_cli_refused_preflight_still_writes_report(tmp_path):
    from ozagia.cli import main

    _disable_resource_admission(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True,
                   capture_output=True, text=True)
    (tmp_path / "app.py").write_text("def f():\n    return 1\n",
                                     encoding="utf-8")
    out = tmp_path / ".ozagia" / "reviews" / "refused"

    # git init without a commit makes the real repository preflight refuse at
    # HEAD; the analyzer profile must still produce a partial report.
    code = main(["deep-review", "--root", str(tmp_path), "--out", str(out),
                 "--profile", "performance", "--budget-s", "5"])

    assert code == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    performance = manifest["subjects"]["performance"]
    assert performance["status"] == "partial"
    assert any("preflight refused" in reason
               for reason in _reasons(performance["omitted"]))
    assert (out / "README.md").is_file()


def test_default_security_review_does_not_run_project_adapters_or_scanners(
        tmp_path, monkeypatch):
    from ozagia.cli import main

    root = tmp_path / "repo"
    root.mkdir()
    adapter_marker = tmp_path / "adapter-ran"
    scanner_marker = tmp_path / "scanner-ran"
    (root / "evil_adapter.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(adapter_marker)!r}).write_text('ran')\n"
        "def register(reg): pass\n", encoding="utf-8")
    (root / ".ozagia.toml").write_text(
        '[adapters]\nmodules = ["evil_adapter:register"]\n\n'
        '[security]\ntools = ["pip-audit"]\nallow_network = true\n\n'
        '[environments]\ntrust_project_python = true\n', encoding="utf-8")
    (root / "app.py").write_text("def f(x=[]): return x\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_scanner = fake_bin / "pip-audit"
    fake_scanner.write_text(
        f"#!/bin/sh\nprintf ran > {str(scanner_marker)!r}\n", encoding="utf-8")
    fake_scanner.chmod(0o755)
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    project_python_marker = tmp_path / "project-python-ran"
    project_python = venv_bin / "python"
    project_python.write_text(
        f"#!/bin/sh\nprintf ran > {str(project_python_marker)!r}\n",
        encoding="utf-8")
    project_python.chmod(0o755)
    (root / ".venv" / "pyvenv.cfg").write_text("home = /usr\n",
                                                  encoding="utf-8")
    _disable_resource_admission(root)
    subprocess.run(["git", "init", "-q", str(root)], check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm",
                    "initial"], check=True)
    index = root / ".git" / "index"
    index_hash = hashlib.sha256(index.read_bytes()).hexdigest()
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    out = tmp_path / "report"

    assert main(["deep-review", "--root", str(root), "--out", str(out),
                 "--profile", "security", "--budget-s", "15"]) == 0

    assert not adapter_marker.exists()
    assert not scanner_marker.exists()
    assert not project_python_marker.exists()
    assert not (root / ".ozagia").exists()
    assert hashlib.sha256(index.read_bytes()).hexdigest() == index_hash
    assert subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                          check=True, capture_output=True, text=True).stdout == ""
