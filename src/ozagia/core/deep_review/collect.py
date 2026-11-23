"""Adapters over the existing whole repository review."""
import json
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import os
import pickle
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from ozagia.core import repo_review, security
from . import analyzers
from .analyzers import data, dependencies, docs_api, operations, profiles

_ANALYZER_PROFILES = frozenset(analyzers.SUBJECTS)
_PROFILE_SECTIONS = {
    "full": None,
    "security": ("security",),
    "performance": ("performance",),
    "failure-modes": ("failure-modes",),
    "data-structures": ("data-structures",),
    "scalability": ("scalability",),
    "architecture": ("architecture",),
    "tests": ("tests",),
    "dependencies": ("dependencies",),
    # Repo review calls this section "ops"; the operations analyzer and the
    # existing collect contract use "operations". Both are relevant.
    "operations": ("ops", "operations"),
    "docs": ("docs",),
}
_ALL_COVERAGE_SECTIONS = (
    "security", "performance", "failure-modes", "data-structures", "scalability",
    "architecture", "tests", "dependencies", "ops", "operations", "docs",
)
_ANALYZER_TIMEOUT_S = 30.0
_MAX_ANALYZER_RESULT_BYTES = 8_000_000
_MAX_ANALYZER_REQUEST_BYTES = 16_000_000
_MAX_PARALLEL_ANALYZERS = 6
_SAFE_WORKER_SCRIPT = r'''
import importlib
import json
import pathlib
import sys

trusted_root, module_name, qualified_name = sys.argv[1:]
sys.path.insert(0, trusted_root)
if not module_name.startswith("ozagia.") or "<locals>" in qualified_name:
    raise RuntimeError("untrusted analyzer target")

def restore(value):
    if isinstance(value, list):
        return [restore(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"__ozagia_path__"}:
            return pathlib.Path(value["__ozagia_path__"])
        if set(value) == {"__ozagia_tuple__"}:
            return tuple(restore(item) for item in value["__ozagia_tuple__"])
        return {key: restore(item) for key, item in value.items()}
    return value

def encode(value):
    if isinstance(value, pathlib.Path):
        return {"__ozagia_path__": str(value)}
    if isinstance(value, tuple):
        return {"__ozagia_tuple__": [encode(item) for item in value]}
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): encode(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("analyzer transport returned a non-JSON value")

try:
    request = json.load(sys.stdin)
    module = importlib.import_module(module_name)
    target = module
    for part in qualified_name.split("."):
        target = getattr(target, part)
    result = target(*restore(request["args"]), **restore(request["kwargs"]))
    print(json.dumps({"ok": True, "result": encode(result)},
                     separators=(",", ":"), allow_nan=False))
except BaseException as exc:
    print(json.dumps({"ok": False, "error_type": type(exc).__name__,
                      "error": str(exc)[:1000]}, separators=(",", ":")))
'''


def _transport_encode(value):
    """Encode analyzer arguments into bounded JSON-safe transport values."""
    if isinstance(value, Path):
        return {"__ozagia_path__": str(value)}
    if isinstance(value, tuple):
        return {"__ozagia_tuple__": [_transport_encode(item) for item in value]}
    if isinstance(value, list):
        return [_transport_encode(item) for item in value]
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("analyzer transport mapping keys must be strings")
        return {key: _transport_encode(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported analyzer transport value: {type(value).__name__}")


def _transport_decode(value):
    """Restore only the explicit Path/tuple transport markers."""
    if isinstance(value, list):
        return [_transport_decode(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {"__ozagia_path__"}:
            return Path(value["__ozagia_path__"])
        if set(value) == {"__ozagia_tuple__"}:
            return tuple(_transport_decode(item)
                         for item in value["__ozagia_tuple__"])
        return {key: _transport_decode(item) for key, item in value.items()}
    return value


def _signal_safe_worker(process, sig, *, force=False):
    """Signal a worker group, falling back to its direct process on error."""
    if os.name == "posix" and process.pid is not None:
        try:
            os.killpg(process.pid, sig)
            return
        except OSError:
            # If group signalling is unavailable or denied, still stop and
            # reap the worker itself. Its descendants may outlive this fallback.
            pass
    try:
        process.kill() if force else process.terminate()
    except OSError:
        pass


def _safe_subprocess_call(function, args, kwargs, timeout):
    """Run an Ozagia callable in isolated mode from the trusted package path."""
    module_name = getattr(function, "__module__", "")
    qualified_name = getattr(function, "__qualname__", "")
    if (not module_name.startswith("ozagia.") or not qualified_name
            or "<locals>" in qualified_name):
        raise RuntimeError("isolated analyzer must be a top-level ozagia callable")
    try:
        request = json.dumps(
            {"args": _transport_encode(args), "kwargs": _transport_encode(kwargs)},
            separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise RuntimeError(f"analyzer request could not be encoded: {exc}") from exc
    if len(request) > _MAX_ANALYZER_REQUEST_BYTES:
        raise RuntimeError("analyzer request exceeded size limit")
    trusted_root = Path(__file__).resolve().parents[3]
    command = [sys.executable, "-I", "-B", "-c", _SAFE_WORKER_SCRIPT,
               str(trusted_root), module_name, qualified_name]
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=(os.name == "posix"))
    except OSError as exc:
        raise RuntimeError(f"isolated analyzer could not start: {exc}") from exc

    output = bytearray()
    overflow = []

    def drain_stdout():
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                if len(output) + len(chunk) > _MAX_ANALYZER_RESULT_BYTES:
                    overflow.append(True)
                elif not overflow:
                    output.extend(chunk)
        except OSError:
            pass

    reader = threading.Thread(target=drain_stdout, daemon=True,
                              name="ozagia-analyzer-output")
    reader.start()

    def send_request():
        try:
            process.stdin.write(request)
            process.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=send_request, daemon=True,
                              name="ozagia-analyzer-input")
    writer.start()
    try:
        try:
            process.wait(timeout=max(0.0, float(timeout)))
        except subprocess.TimeoutExpired as exc:
            _signal_safe_worker(process, signal.SIGTERM)
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                _signal_safe_worker(process, getattr(signal, "SIGKILL", signal.SIGTERM),
                                    force=True)
                process.wait(timeout=1.0)
            # Do not include the micro-variant per-worker timeout (which is
            # derived from remaining shared budget) in rendered coverage.
            raise TimeoutError("collector timed out at its assigned time limit") from exc
        reader.join(1.0)
        if reader.is_alive():
            raise RuntimeError("isolated analyzer output reader did not finish")
        writer.join(1.0)
        if writer.is_alive():
            raise RuntimeError("isolated analyzer input writer did not finish")
        if overflow:
            raise RuntimeError("collector result exceeded size limit")
        if process.returncode != 0:
            raise RuntimeError(
                f"isolated analyzer exited with status {process.returncode}")
        try:
            response = json.loads(output.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError("isolated analyzer returned invalid JSON") from exc
        if not isinstance(response, dict) or not response.get("ok"):
            error_type = response.get("error_type", "AnalyzerError") if isinstance(response, dict) else "AnalyzerError"
            message = response.get("error", "analyzer failed") if isinstance(response, dict) else "analyzer failed"
            raise RuntimeError(f"{error_type}: {message}")
        return _transport_decode(response.get("result"))
    finally:
        if process.poll() is None:
            _signal_safe_worker(process, signal.SIGTERM)
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        writer.join(1.0)
        if process.stdout:
            process.stdout.close()


def _isolated_call_child(send, function, args, kwargs):
    """Execute one analyzer in a disposable child and return its result."""
    if os.name == "posix":
        # Give subprocesses launched by preflight code the same killable group
        # as this worker. The caller can then reap the whole collector tree.
        os.setsid()
    try:
        payload = pickle.dumps((True, function(*args, **kwargs)), protocol=5)
        if len(payload) > _MAX_ANALYZER_RESULT_BYTES:
            payload = pickle.dumps((False, "ResultTooLarge",
                                    "collector result exceeded size limit"), protocol=5)
        send.send_bytes(payload)
    except BaseException as exc:  # child boundary: report even analyzer exits
        try:
            send.send_bytes(pickle.dumps(
                (False, type(exc).__name__, str(exc)), protocol=5))
        except Exception:
            pass
    finally:
        send.close()


def _signal_isolated(process, sig, *, force=False):
    """Signal a collector and any ordinary subprocess descendants it started."""
    if os.name == "posix" and process.pid is not None:
        try:
            os.killpg(process.pid, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass
    if process.is_alive():
        if force:
            process.kill()
        else:
            process.terminate()


def _bounded_call(function, *args, timeout, start_method=None, **kwargs):
    """Run an in-process analyzer with a hard wall-clock bound.

    A process boundary is required because analyzer-level budget checks cannot
    interrupt a parser or filesystem operation that stops making progress.
    Ozagia callables use an isolated Python subprocess and JSON transport. A
    legacy local callable is accepted only from the main thread when no other
    threads are active, for test injection compatibility.
    """
    timeout = max(0.0, float(timeout))
    if timeout <= 0:
        raise TimeoutError("collector timed out: no review budget remained")
    module_name = getattr(function, "__module__", "")
    qualified_name = getattr(function, "__qualname__", "")
    if (module_name.startswith("ozagia.") and qualified_name
            and "<locals>" not in qualified_name):
        return _safe_subprocess_call(function, args, kwargs, timeout)
    if not (module_name.startswith("test_") or module_name.startswith("tests.")):
        raise RuntimeError(
            "isolated analyzer must be an Ozagia callable; arbitrary modules are refused")
    if (threading.current_thread() is not threading.main_thread()
            or threading.active_count() > 1):
        raise RuntimeError("non-Ozagia analyzer call refused in a multithreaded process")
    methods = multiprocessing.get_all_start_methods()
    if not methods:
        raise RuntimeError("process isolation is unavailable")
    method = start_method or ("fork" if "fork" in methods else "spawn")
    if method not in methods:
        method = "spawn" if "spawn" in methods else methods[0]
    context = multiprocessing.get_context(method)
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_isolated_call_child,
                              args=(send, function, args, kwargs), daemon=True)
    try:
        process.start()
    except BaseException:
        receive.close()
        send.close()
        raise
    send.close()

    response_ready = threading.Event()
    response_box = []

    def receive_response():
        try:
            payload = receive.recv_bytes(_MAX_ANALYZER_RESULT_BYTES)
            response_box.append(pickle.loads(payload))
        except BaseException as exc:
            response_box.append(exc)
        finally:
            response_ready.set()

    reader = threading.Thread(target=receive_response, daemon=True)
    reader.start()
    try:
        if not response_ready.wait(timeout):
            _signal_isolated(process, signal.SIGTERM)
            process.join(0.5)
            _signal_isolated(process, getattr(signal, "SIGKILL", signal.SIGTERM),
                             force=True)
            process.join(1.0)
            if process.is_alive():
                raise RuntimeError("timed-out collector could not be reaped")
            reader.join(0.5)
            raise TimeoutError(f"collector timed out after {timeout:g} seconds")
        response = response_box[0]
        if isinstance(response, BaseException):
            if isinstance(response, EOFError):
                raise RuntimeError("collector process exited without a result") from response
            raise RuntimeError(f"collector result could not be read: {response}") from response
        if not isinstance(response, tuple) or not response:
            raise RuntimeError("collector process exited without a result")
        process.join(0.5)
        if process.is_alive():
            _signal_isolated(process, signal.SIGTERM)
            process.join(0.5)
            if process.is_alive():
                _signal_isolated(process, getattr(signal, "SIGKILL", signal.SIGTERM),
                                 force=True)
                process.join(1.0)
        if process.is_alive():
            raise RuntimeError("collector process could not be reaped")
        if process.exitcode != 0:
            raise RuntimeError(
                f"collector process exited with status {process.exitcode}")
        if not response[0]:
            raise RuntimeError(f"{response[1]}: {response[2]}")
        return response[1]
    finally:
        receive.close()
        if process.is_alive():
            _signal_isolated(process, signal.SIGTERM)
            process.join(0.5)
            if process.is_alive():
                _signal_isolated(process, getattr(signal, "SIGKILL", signal.SIGTERM),
                                 force=True)
                process.join(1.0)


def _parallel_analyzers(tasks, *, started: float, budget_s: float):
    """Run independent analyzers concurrently in isolated Python subprocesses.

    Threads supervise process creation and completion. Each production
    analyzer is executed in a fresh ``python -I -B`` subprocess; results are
    returned in task order regardless of completion order.
    """
    if not tasks:
        return {}
    deadline = started + max(0.0, float(budget_s))

    if len(tasks) == 1:
        key, function, args, kwargs = tasks[0]
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            return {key: (False, TimeoutError(
                "review budget exhausted before analyzer started"))}
        options = dict(kwargs)
        code = getattr(function, "__code__", None)
        if code is not None and "budget_s" in code.co_varnames:
            options["budget_s"] = remaining
        # Keep the caller's thread and use the existing killable process
        # boundary. If collection was invoked from another thread, require
        # spawn to avoid forking a multithreaded process.
        method = (None if threading.current_thread() is threading.main_thread()
                  else "spawn")
        try:
            value = _bounded_call(
                function, *args, timeout=min(remaining, _ANALYZER_TIMEOUT_S),
                start_method=method, **options)
            return {key: (True, value)}
        except Exception as exc:
            return {key: (False, exc)}

    def invoke(task):
        key, function, args, kwargs = task
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("review budget exhausted before analyzer started")
        options = dict(kwargs)
        code = getattr(function, "__code__", None)
        if code is not None and "budget_s" in code.co_varnames:
            options["budget_s"] = remaining
        return _bounded_call(
            function, *args, timeout=min(remaining, _ANALYZER_TIMEOUT_S),
            start_method="spawn", **options)

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(tasks), _MAX_PARALLEL_ANALYZERS),
                            thread_name_prefix="ozagia-review") as pool:
        futures = [(task[0], pool.submit(invoke, task)) for task in tasks]
        for key, future in futures:
            try:
                results[key] = (True, future.result())
            except Exception as exc:  # collector failure is coverage data
                results[key] = (False, exc)
    return results


def _analyze_docs(root, *, budget_s):
    """Normalize current and legacy docs analyzer results in the worker."""
    analyze_with_coverage = getattr(docs_api, "analyze_with_coverage", None)
    if callable(analyze_with_coverage):
        result = analyze_with_coverage(root, budget_s=budget_s)
        return {
            "findings": result.get("findings", []),
            "checked": result.get("checked", []),
            "skipped": result.get("skipped", []),
            "coverage": result.get("coverage"),
            "coverage_gap": None,
        }
    findings, checked, skipped = analyzers.analyze_docs_api(
        root, budget_s=budget_s)
    return {"findings": findings, "checked": checked, "skipped": skipped,
            "coverage": None,
            "coverage_gap": ("docs_api.analyze_with_coverage is unavailable; "
                             "documentation/API coverage facts were not collected")}


def _coverage(packet, section):
    """Return one section's mutable coverage record, creating it if needed."""
    current = packet.setdefault("sections", {}).setdefault(section, {})
    current.setdefault("not_checked", [])
    return current


def _record_unavailable(packet, sections, label, exc):
    """Record a failed collector without discarding other collectors' work."""
    reason = f"{label} unavailable: {type(exc).__name__}: {exc}"
    for section in sections:
        current = _coverage(packet, section)
        item = {"reason": reason}
        if item not in current["not_checked"]:
            current["not_checked"].append(item)
    return reason


def _record_preflight_refusal(packet, profile, reason):
    """Attach a preflight refusal to the sections the requested profile owns."""
    sections = _PROFILE_SECTIONS.get(profile)
    if sections is None:
        sections = _ALL_COVERAGE_SECTIONS
    for section in sections:
        current = _coverage(packet, section)
        item = {"reason": reason}
        if item not in current["not_checked"]:
            current["not_checked"].append(item)


def _load_graph_info(root, graph_file, head):
    """Return only serializable graph status from the potentially large loader."""
    info, _dev, _index = repo_review._load_graph(
        root, graph_file, head, allow_project_adapters=False)
    return info


def _refusal_packet(reason):
    return {
        "findings": [],
        "sections": {},
        "graph": {"freshness": "unknown", "reason": reason},
    }


def _analyzer_preflight(root, *, index, budget_s, started):
    """Run the repository preflight for profiles handled only by analyzers.

    The graph is refreshed here when requested, using the same indexer as
    ``review_repo``. Analyzer profiles do not need the repository-review
    collectors, but they still need the same admission, HEAD and freshness
    checks, and ``--index`` must have the same meaning for every profile.
    """
    packet = _refusal_packet("repository preflight was not run")
    remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
    if remaining <= 0:
        reason = "repository preflight timed out: no review budget remained"
        packet["preflight"] = {"ok": False, "reason": reason}
        packet["graph"] = {"freshness": "unknown", "reason": reason}
        return packet, reason
    try:
        pre = _bounded_call(repo_review.preflight, root,
                            timeout=min(remaining, _ANALYZER_TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 - a refusal is report data
        reason = f"repository preflight unavailable: {type(exc).__name__}: {exc}"
        packet["preflight"] = {"ok": False, "reason": str(exc)}
        packet["graph"] = {"freshness": "unknown", "reason": reason}
        return packet, reason
    packet["preflight"] = {
        "ok": bool(pre.get("ok")),
        "reason": pre.get("reason"),
        "checks": pre.get("checks", []),
    }
    if not pre.get("ok"):
        reason = "repository preflight refused: " + str(
            pre.get("reason") or "unknown reason")
        packet["graph"] = {"freshness": "unknown", "reason": reason}
        return packet, reason
    path = Path(pre["root"])
    head = str(pre.get("head") or "")
    graph_file = repo_review._graph_file(path, None)
    index_report = None
    if index:
        remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
        if remaining <= 0:
            index_report = {
                "ok": False,
                "error": "no review budget remained for graph refresh",
            }
        else:
            try:
                # Snapshot preparation already owns and reaps its subprocess
                # group. Pass the remaining review budget into that timeout
                # so killing this worker cannot orphan the prep process.
                report = repo_review._default_indexer(
                    path, graph_file, timeout_s=remaining)
                index_report = {
                    "ok": True,
                    **{key: report.get(key) for key in
                       ("action", "nodes", "edges", "seconds") if key in report},
                }
            except Exception as exc:  # noqa: BLE001 - reported as coverage
                index_report = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    try:
        remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
        graph_info = _bounded_call(
            _load_graph_info, path, graph_file, head,
            timeout=min(remaining, _ANALYZER_TIMEOUT_S))
    except Exception as exc:  # noqa: BLE001 - graph checks become unknown
        graph_info = {
            "freshness": "unknown",
            "reason": f"graph unavailable: {type(exc).__name__}: {exc}",
        }
    if index_report is not None:
        graph_info["indexed"] = index_report
    packet["graph"] = graph_info
    return packet, None


def collect(root, *, profile="full", budget_s=60, index=False,
            profile_reports=None, security_tools=None, security_network=False,
            local_advisories=None, license_policy=None,
            supply_chain_policy=None):
    started = time.monotonic()
    sections = None if profile == "full" else {
        "security": ("security",), "architecture": ("architecture",),
        "tests": ("tests",), "dependencies": ("dependencies",),
        "operations": ("ops",), "docs": ("docs",),
        "performance": (), "failure-modes": (),
        "data-structures": (), "scalability": (),
    }.get(profile)
    if sections is None and profile != "full":
        raise ValueError(f"unsupported profile: {profile}")
    refusal = None
    if profile in _ANALYZER_PROFILES:
        packet, refusal = _analyzer_preflight(
            root, index=index, budget_s=budget_s, started=started)
    else:
        try:
            packet = repo_review.review_repo(
                root, sections=sections, budget_s=budget_s,
                index=index, cache_mode="off", trust_project_python=False,
                probe_interpreters=False,
                allow_project_adapters=False)
        except repo_review.PreflightError as exc:
            refusal = f"repository preflight refused: {exc}"
            packet = _refusal_packet(refusal)
            report = getattr(exc, "report", None)
            packet["preflight"] = report if isinstance(report, dict) else {
                "ok": False, "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - keep other collectors running
            refusal = (f"repository review unavailable: "
                       f"{type(exc).__name__}: {exc}")
            packet = _refusal_packet(refusal)
    if refusal:
        _record_preflight_refusal(packet, profile, refusal)
    if profile in {"full", "tests"}:
        tests_section = packet.setdefault("sections", {}).setdefault("tests", {})
        tests_section.setdefault("not_checked", []).append({"reason":
            "Tests were not executed and line coverage was not assessed by this review."})
    # The existing security scanner is explicitly configured here rather than
    # inheriting project tool/network settings: deep review's default lane is
    # deterministic and offline. Its own file, graph and finding caps apply.
    if profile == "full" or profile == "security":
        requested_tools = list(dict.fromkeys(
            str(name) for name in (security_tools or ()) if name))
        known_tools = [name for name in requested_tools
                       if name in security.TOOL_REGISTRY]
        unknown_tools = [name for name in requested_tools
                         if name not in security.TOOL_REGISTRY]
        scanner_results = [_unknown_security_tool(name)
                           for name in unknown_tools]
        unknown_not_checked = [{
            "reason": (f"security scanner tool {name!r} is unknown and was "
                       "not run"),
        } for name in unknown_tools]

        def scanner_provenance():
            return {
                "requested": list(requested_tools),
                "network_allowed": bool(security_network),
                "results": list(scanner_results),
            }

        remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
        try:
            if remaining <= 0:
                raise TimeoutError("no review budget remained for security scan")
            # Always pass explicit options. Omitting them makes security.scan
            # load project configuration, which could run configured tools or
            # enable network access in the default review lane.
            scan_options = {
                "tools": tuple(known_tools),
                "allow_network": security_network is True,
            }
            report = _bounded_security_scan(
                root, timeout=min(remaining, 120.0), **scan_options)
            for item in report.get("findings", []):
                path = str(item.get("file") or "")
                rule = str((item.get("source") or {}).get("rule") or
                           item.get("category") or "security")
                line = item.get("line")
                key = f"security:{rule}:{path}"
                # Keep only scanner-generated descriptions and metadata. Never
                # copy matched source text or secret values into deep-review.
                packet.setdefault("findings", []).append({
                    "section": "security", "type": rule,
                    "severity": _severity(item.get("severity")),
                    "confidence": item.get("confidence", "low"),
                    "keys": [key], "file": path, "line": line,
                    "summary": security.redact_text(str(item.get("message") or rule)),
                    "recommendation": security.redact_text(str(item.get("remediation") or "")),
                    "security_evidence": {
                        "rule": rule, "kind": item.get("evidence", {}).get("kind"),
                        "line": line, "graph_path": item.get("graph_path") or []},
                    "truth_assessed": False,
                })
            # The repo review security section checks tracked hygiene and
            # sensitive constructs; the offline scanner adds secret scanning.
            # Preserve the former's coverage instead of replacing it.
            current = packet.setdefault("sections", {}).setdefault("security", {})
            current["checked"] = list(current.get("checked", [])) + ["offline_security_scan"]
            scan_tools = report.get("tools")
            if isinstance(scan_tools, dict):
                for item in scan_tools.get("results") or []:
                    if not isinstance(item, dict):
                        continue
                    scanner_results.append({
                        "name": item.get("name"),
                        "kind": item.get("kind"),
                        "status": item.get("status"),
                        "version": (security.redact_text(str(item.get("version")))[:200]
                                    if item.get("version") else None),
                        "error": (security.redact_text(str(item.get("error")))[:300]
                                  if item.get("error") else None),
                        "findings": item.get("findings", 0),
                    })
                    if item.get("report_hash"):
                        scanner_results[-1]["report_hash"] = item["report_hash"]
            requested_order = {name: index
                               for index, name in enumerate(requested_tools)}
            scanner_results.sort(key=lambda row: requested_order.get(
                str(row.get("name")), len(requested_order)))
            current["scanner_tools"] = scanner_provenance()
            current["not_checked"] = (list(current.get("not_checked", []))
                + [{"reason": reason} for reason in report.get("not_assessed", [])]
                                + ([{"reason": "security finding limit reached"}]
                                   if report.get("summary", {}).get("omitted") else [])
                                + unknown_not_checked)
            current["files_checked"] = report.get("inputs", {}).get("files_scanned", 0)
            current["scanner_summary"] = report.get("summary", {})
            current["scanner_graph"] = report.get("graph", {})
            # Add narrow source-to-sink candidates after the established scan.
            # Keep this separate from its sink-pattern rules and use only the
            # budget left after the scanner has returned.
            remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
            if remaining > 0:
                try:
                    flows = _bounded_trust_scan(root, timeout=min(remaining, 10.0))
                    existing = {(str(f.get("file") or ""), f.get("line"),
                                 str(f.get("type") or ""))
                                for f in packet.get("findings", [])
                                if f.get("section") == "security"}
                    duplicate_rules = {
                        "command": {"shell-true", "os-system"},
                        "sql": {"sql-fstring", "sql-concat", "sql-template"},
                        "path": {"path-traversal-open", "path-traversal-send"},
                    }
                    added = 0
                    for item in flows.get("findings", []):
                        file, line = str(item.get("file") or ""), item.get("line")
                        if any((file, line, rule) in existing
                               for rule in duplicate_rules.get(
                                   str(item.get("category") or ""), set())):
                            continue
                        category = str(item.get("category") or "trust-boundary")
                        rule = str(item.get("rule") or f"trust-flow-{category}")
                        packet.setdefault("findings", []).append({
                            "section": "security", "type": rule,
                            "severity": "minor", "confidence": "low",
                            "keys": [f"security:{rule}:{file}"], "file": file,
                            "line": line,
                            "summary": security.redact_text(str(item.get("message") or rule)),
                            "recommendation": "review the input validation and authorization path",
                            "security_evidence": {
                                "kind": "same_function_source_to_sink",
                                "source_line": item.get("source_line"), "line": line,
                                "category": category, "cwe": item.get("cwe")},
                            "truth_assessed": False,
                        })
                        added += 1
                    current["checked"].append("python_trust_boundary_candidates")
                    current["trust_boundary_files_checked"] = flows.get("checked", 0)
                    current["trust_boundary_findings"] = added
                    current["not_checked"] = (list(current.get("not_checked", []))
                        + flows.get("not_checked", []))
                except (security.SecurityError, OSError, ValueError, TimeoutError,
                        subprocess.SubprocessError) as exc:
                    current["not_checked"] = list(current.get("not_checked", [])) + [
                        {"reason": f"trust-boundary scan unavailable: {exc}"}]
                except Exception as exc:  # noqa: BLE001 - preserve other checks
                    current["not_checked"] = list(current.get("not_checked", [])) + [
                        {"reason": (f"trust-boundary scan unavailable: "
                                    f"{type(exc).__name__}: {exc}")}]
            else:
                current["not_checked"] = list(current.get("not_checked", [])) + [
                    {"reason": "trust-boundary scan skipped: no review budget remained"}]
        except (security.SecurityError, OSError, ValueError, TimeoutError,
                subprocess.SubprocessError) as exc:
            current = packet.setdefault("sections", {}).setdefault("security", {})
            current["scanner_tools"] = scanner_provenance()
            current["not_checked"] = list(current.get("not_checked", [])) + [
                {"reason": f"security scan unavailable: {exc}"}
            ] + unknown_not_checked
        except Exception as exc:  # noqa: BLE001 - preserve other checks
            current = packet.setdefault("sections", {}).setdefault("security", {})
            current["scanner_tools"] = scanner_provenance()
            current["not_checked"] = list(current.get("not_checked", [])) + [
                {"reason": f"security scan unavailable: {type(exc).__name__}: {exc}"}
            ] + unknown_not_checked
    enabled = profile == "full" or profile in _ANALYZER_PROFILES
    tasks = []
    if enabled:
        tasks.append(("static", analyzers.analyze_static, (root,), {}))
    if profile == "full" or profile == "dependencies":
        options = {}
        if local_advisories is not None:
            options["local_advisories"] = local_advisories
        if license_policy is not None:
            options["license_policy"] = license_policy
        if supply_chain_policy is not None:
            options["supply_chain_policy"] = supply_chain_policy
        tasks.append(("dependencies", dependencies.analyze, (root,), options))
    if profile == "full" or profile == "operations":
        tasks.append(("operations", operations.analyze, (root,), {}))
    if profile == "full" or profile == "docs":
        tasks.append(("docs", _analyze_docs, (root,), {}))
    if profile == "full" or profile == "data-structures":
        tasks.append(("data", data.analyze, (root,), {}))
    if (profile_reports and
            (profile == "full" or profile in {"performance", "scalability",
                                              "failure-modes"})):
        tasks.append(("profiles", profiles.analyze,
                      (root, list(profile_reports)), {}))

    # Task order is also merge order. Workers may finish in any order, but no
    # packet or finding ordering depends on scheduler timing.
    analyzer_results = _parallel_analyzers(
        tasks, started=started, budget_s=budget_s)
    for name, ok, result in (
            ("static", "static" in analyzer_results, analyzer_results.get("static")),
            ("dependencies", "dependencies" in analyzer_results, analyzer_results.get("dependencies")),
            ("operations", "operations" in analyzer_results, analyzer_results.get("operations")),
            ("docs", "docs" in analyzer_results, analyzer_results.get("docs")),
            ("data", "data" in analyzer_results, analyzer_results.get("data")),
            ("profiles", "profiles" in analyzer_results, analyzer_results.get("profiles"))):
        if not ok:
            continue
        succeeded, value = result
        if not succeeded:
            exc = value
            if name == "static":
                targets = (_ANALYZER_PROFILES if profile == "full"
                           else (profile,))
                _record_unavailable(packet, targets, "static Python analysis", exc)
            elif name == "dependencies":
                _record_unavailable(packet, ("dependencies",),
                                    "dependency analyzer", exc)
            elif name == "operations":
                _record_unavailable(packet, ("ops",), "operations analyzer", exc)
            elif name == "docs":
                _record_unavailable(packet, ("docs",),
                                    "documentation/API analyzer", exc)
            elif name == "data":
                _record_unavailable(packet, ("data-structures",),
                                    "data-structure analyzer", exc)
            else:
                _record_unavailable(
                    packet, ("performance", "scalability", "failure-modes"),
                    "profile report analyzer", exc)
            continue

        if name == "static":
            static, checked, skipped = value
            if profile != "full":
                static = [item for item in static if item.get("section") == profile]
            packet.setdefault("findings", []).extend(static)
            packet.setdefault("sections", {})
            for subject in analyzers.SUBJECTS:
                if profile != "full" and profile != subject:
                    continue
                current = _coverage(packet, subject)
                current["checked"] = list(current.get("checked", []))
                if checked:
                    current["checked"].append("python_ast_static_checks")
                current["not_checked"] = (list(current.get("not_checked", []))
                    + list(skipped) + [{"reason":
                        "Only selected Python patterns are checked; other patterns and languages are unassessed."}]
                        + ([] if checked else [{"reason": "no supported Python files were found"}]))
                current["files_checked"] = checked
        elif name == "dependencies":
            dependency_result = value
            packet.setdefault("findings", []).extend(dependency_result["findings"])
            current = packet.setdefault("sections", {}).setdefault("dependencies", {})
            current["checked"] = list(current.get("checked", []))
            if dependency_result["checked"]:
                current["checked"].append("dependency_manifest_inventory")
            current["not_checked"] = (list(current.get("not_checked", []))
                                       + dependency_result["not_checked"])
            inventory = dependency_result["inventory"]
            current["inventory"] = inventory
            current["sbom"] = dependency_result.get("sbom")
            current["cve_state"] = dependency_result.get("cve_state")
            current["local_advisories"] = dependency_result.get("local_advisories")
            current["license_policy"] = inventory.get("license_policy")
            current["supply_chain"] = inventory.get("supply_chain")
            current["version_conflicts"] = inventory.get("version_conflicts", [])
        elif name == "operations":
            operations_result = value
            packet.setdefault("findings", []).extend(operations_result["findings"])
            sections = packet.setdefault("sections", {})
            current = sections.setdefault("ops", {})
            legacy = sections.get("operations")
            if isinstance(legacy, dict) and legacy is not current:
                for key in ("checked", "not_checked", "skipped", "unknown"):
                    values = list(current.get(key, []))
                    for item in legacy.get(key, []):
                        if item not in values:
                            values.append(item)
                    if values:
                        current[key] = values
                for key, item in legacy.items():
                    current.setdefault(key, item)
            current["checked"] = list(current.get("checked", []))
            if operations_result["checked"]:
                current["checked"].append("operational_file_inventory")
            inventory_checks = operations_result["inventory"].get("checks", [])
            for check in ("runbook_links_and_topics", "observability_declarations",
                          "static_iac_config_candidates"):
                if check in inventory_checks and check not in current["checked"]:
                    current["checked"].append(check)
            current["not_checked"] = (list(current.get("not_checked", []))
                                       + operations_result["not_checked"])
            current["inventory"] = operations_result["inventory"]
            if legacy is not None:
                sections["operations"] = current
        elif name == "docs":
            docs_result = value
            packet.setdefault("findings", []).extend(docs_result["findings"])
            current = packet.setdefault("sections", {}).setdefault("docs", {})
            current["checked"] = list(current.get("checked", []))
            if docs_result["checked"]:
                current["checked"].append("local_documented_api_references")
            current["not_checked"] = (list(current.get("not_checked", []))
                                       + docs_result["skipped"])
            current["api_docs_files_checked"] = docs_result["checked"]
            if docs_result["coverage"] is not None:
                current["doc_coverage"] = docs_result["coverage"]
            if docs_result["coverage_gap"] is not None:
                current["api_coverage_gap"] = docs_result["coverage_gap"]
        elif name == "data":
            data_result = value
            packet.setdefault("findings", []).extend(data_result.get("findings", []))
            current = packet.setdefault("sections", {}).setdefault("data-structures", {})
            current["checked"] = list(current.get("checked", []))
            if data_result.get("checked"):
                current["checked"].append("source_data_model_inventory")
            current["not_checked"] = (list(current.get("not_checked", []))
                                       + data_result.get("not_checked", []))
            current["data_inventory"] = data_result.get("inventory", {})
            current["data_files_checked"] = data_result.get("checked", [])
        else:
            profile_result = value
            packet.setdefault("findings", []).extend(profile_result["findings"])
            for section, result_section in profile_result["sections"].items():
                current = packet.setdefault("sections", {}).setdefault(section, {})
                current["checked"] = (list(current.get("checked", []))
                                      + list(result_section.get("checked", [])))
                current["not_checked"] = (list(current.get("not_checked", []))
                                          + list(result_section.get("not_checked", [])))
                for key in ("profile_reports", "profile_report_hashes",
                            "profile_metrics"):
                    if key in result_section:
                        current[key] = result_section[key]
    return packet


def _unknown_security_tool(name):
    """Describe a requested tool that is not in the fixed scanner registry."""
    return {
        "name": name,
        "kind": None,
        "status": "not_checked",
        "version": None,
        "error": (f"unknown tool {name!r}; known tools: "
                  f"{', '.join(security.TOOL_REGISTRY)}"),
        "findings": 0,
    }


def _bounded_security_scan(root, timeout, *, tools=(), allow_network=False):
    """Run the scanner in a killable child so the review budget is enforceable.

    Tool names and the network opt-in travel as argv values, never as source
    text interpolated into the child's Python code.
    """
    code = (
        "import json, sys; from ozagia.core.security import scan; "
        "print(json.dumps(scan(sys.argv[1], tools=json.loads(sys.argv[2]), "
        "allow_network=sys.argv[3] == '1', max_findings=250), sort_keys=True))"
    )
    names = [str(name) for name in tools]
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(root), json.dumps(names),
         "1" if allow_network else "0"],
        capture_output=True, text=True,
        timeout=timeout, check=False, stdin=subprocess.DEVNULL,
        env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode:
        raise security.SecurityError("bounded security scanner subprocess failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise security.SecurityError("bounded security scanner returned invalid JSON") from exc


def _bounded_trust_scan(root, *, timeout):
    """Run bounded local flow analysis in a killable, offline child."""
    code = (
        "import json, sys; from pathlib import Path; "
        "from ozagia.core.trust_boundary import analyze_tree; "
        "print(json.dumps(analyze_tree(Path(sys.argv[1]), budget_s=float(sys.argv[2])), "
        "sort_keys=True))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(root), str(timeout)],
        capture_output=True, text=True, timeout=timeout, check=False,
        stdin=subprocess.DEVNULL,
        env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if result.returncode:
        raise security.SecurityError("bounded trust-boundary analyzer failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise security.SecurityError("bounded trust-boundary analyzer returned invalid JSON") from exc


def _severity(value):
    # A scanner candidate alone does not establish a blocking finding.
    return {"critical": "major", "high": "major", "medium": "minor",
            "low": "note", "info": "note"}.get(str(value).lower(), "note")
