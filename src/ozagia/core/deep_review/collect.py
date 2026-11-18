"""Adapters over the existing whole repository review."""
import json
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


def _bounded_call(function, *args, timeout, **kwargs):
    """Run an in-process analyzer with a hard wall-clock bound.

    A process boundary is required because analyzer-level budget checks cannot
    interrupt a parser or filesystem operation that stops making progress.
    One child at a time keeps concurrency bounded to one and makes collector
    ordering/output deterministic.
    """
    timeout = max(0.0, float(timeout))
    if timeout <= 0:
        raise TimeoutError("collector timed out: no review budget remained")
    methods = multiprocessing.get_all_start_methods()
    if not methods:
        raise RuntimeError("process isolation is unavailable")
    context = multiprocessing.get_context("fork" if "fork" in methods else "spawn")
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
            local_advisories=None, license_policy=None):
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
    if enabled:
        remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
        try:
            static, checked, skipped = _bounded_call(
                analyzers.analyze_static, root, budget_s=remaining,
                timeout=min(remaining, _ANALYZER_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 - coverage instead of crash
            targets = (_ANALYZER_PROFILES if profile == "full" else (profile,))
            _record_unavailable(packet, targets, "static Python analysis", exc)
        else:
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
    if profile == "full" or profile == "dependencies":
        remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
        try:
            dependency_options = {"budget_s": remaining}
            if local_advisories is not None:
                dependency_options["local_advisories"] = local_advisories
            if license_policy is not None:
                dependency_options["license_policy"] = license_policy
            dependency_result = _bounded_call(
                dependencies.analyze, root, **dependency_options,
                timeout=min(remaining, _ANALYZER_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 - coverage instead of crash
            _record_unavailable(packet, ("dependencies",), "dependency analyzer", exc)
        else:
            packet.setdefault("findings", []).extend(dependency_result["findings"])
            current = packet.setdefault("sections", {}).setdefault("dependencies", {})
            current["checked"] = list(current.get("checked", []))
            if dependency_result["checked"]:
                current["checked"].append("dependency_manifest_inventory")
            current["not_checked"] = (list(current.get("not_checked", []))
                                       + dependency_result["not_checked"])
            inventory = dependency_result["inventory"]
            current["inventory"] = inventory
            # The orchestrator writes sbom.json; collection only passes the
            # deterministic SBOM, CVE state and conflicts through the packet.
            current["sbom"] = dependency_result.get("sbom")
            current["cve_state"] = dependency_result.get("cve_state")
            current["local_advisories"] = dependency_result.get("local_advisories")
            current["license_policy"] = inventory.get("license_policy")
            current["version_conflicts"] = inventory.get("version_conflicts", [])
    if profile == "full" or profile == "operations":
        remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
        try:
            operations_result = _bounded_call(
                operations.analyze, root, budget_s=remaining,
                timeout=min(remaining, _ANALYZER_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 - coverage instead of crash
            _record_unavailable(packet, ("ops",), "operations analyzer", exc)
        else:
            packet.setdefault("findings", []).extend(operations_result["findings"])
            sections = packet.setdefault("sections", {})
            # Repo review and the deep-review contract call this section "ops".
            # Merge any legacy "operations" coverage (older callers/tests) into
            # that canonical record instead of keeping two competing sections.
            current = sections.setdefault("ops", {})
            legacy = sections.get("operations")
            if isinstance(legacy, dict) and legacy is not current:
                for key in ("checked", "not_checked", "skipped", "unknown"):
                    values = list(current.get(key, []))
                    for value in legacy.get(key, []):
                        if value not in values:
                            values.append(value)
                    if values:
                        current[key] = values
                for key, value in legacy.items():
                    current.setdefault(key, value)
            current["checked"] = list(current.get("checked", []))
            if operations_result["checked"]:
                current["checked"].append("operational_file_inventory")
            inventory_checks = operations_result["inventory"].get("checks", [])
            if ("static_iac_config_candidates" in inventory_checks
                    and "static_iac_config_candidates" not in current["checked"]):
                current["checked"].append("static_iac_config_candidates")
            current["not_checked"] = (list(current.get("not_checked", []))
                                       + operations_result["not_checked"])
            current["inventory"] = operations_result["inventory"]
            if legacy is not None:
                sections["operations"] = current
    if profile == "full" or profile == "docs":
        remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
        try:
            analyze_with_coverage = getattr(
                docs_api, "analyze_with_coverage", None)
            coverage = None
            coverage_available = False
            coverage_gap = None
            if callable(analyze_with_coverage):
                coverage_available = True
                docs_result = _bounded_call(
                    analyze_with_coverage, root, budget_s=remaining,
                    timeout=min(remaining, _ANALYZER_TIMEOUT_S))
                docs_findings = docs_result.get("findings", [])
                checked = docs_result.get("checked", [])
                skipped = docs_result.get("skipped", [])
                coverage = docs_result.get("coverage")
            else:
                # Older analyzer builds only expose the triple-return API.
                # Keep collecting findings but say that the richer coverage
                # facts are unavailable rather than silently overstating them.
                docs_findings, checked, skipped = _bounded_call(
                    analyzers.analyze_docs_api, root, budget_s=remaining,
                    timeout=min(remaining, _ANALYZER_TIMEOUT_S))
                coverage_gap = ("docs_api.analyze_with_coverage is unavailable; "
                                "documentation/API coverage facts were not collected")
        except Exception as exc:  # noqa: BLE001 - coverage instead of crash
            _record_unavailable(packet, ("docs",), "documentation/API analyzer", exc)
        else:
            packet.setdefault("findings", []).extend(docs_findings)
            current = packet.setdefault("sections", {}).setdefault("docs", {})
            current["checked"] = list(current.get("checked", []))
            if checked:
                current["checked"].append("local_documented_api_references")
            current["not_checked"] = list(current.get("not_checked", [])) + skipped
            current["api_docs_files_checked"] = checked
            if coverage_available:
                current["doc_coverage"] = coverage
            if coverage_gap is not None:
                current["api_coverage_gap"] = coverage_gap
    if profile == "full" or profile == "data-structures":
        remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
        try:
            data_result = _bounded_call(
                data.analyze, root, budget_s=remaining,
                timeout=min(remaining, _ANALYZER_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 - coverage instead of crash
            _record_unavailable(packet, ("data-structures",),
                                "data-structure analyzer", exc)
        else:
            packet.setdefault("findings", []).extend(data_result.get("findings", []))
            current = packet.setdefault("sections", {}).setdefault("data-structures", {})
            current["checked"] = list(current.get("checked", []))
            if data_result.get("checked"):
                current["checked"].append("source_data_model_inventory")
            current["not_checked"] = list(current.get("not_checked", [])) + data_result.get("not_checked", [])
            current["data_inventory"] = data_result.get("inventory", {})
            current["data_files_checked"] = data_result.get("checked", [])
    if (profile_reports and
            (profile == "full" or profile in {"performance", "scalability",
                                              "failure-modes"})):
        remaining = max(0.0, float(budget_s) - (time.monotonic() - started))
        try:
            profile_result = _bounded_call(
                profiles.analyze, root, list(profile_reports), budget_s=remaining,
                timeout=min(remaining, _ANALYZER_TIMEOUT_S))
        except Exception as exc:  # noqa: BLE001 - coverage instead of crash
            _record_unavailable(
                packet, ("performance", "scalability", "failure-modes"),
                "profile report analyzer", exc)
        else:
            packet.setdefault("findings", []).extend(profile_result["findings"])
            for section, data_result in profile_result["sections"].items():
                current = packet.setdefault("sections", {}).setdefault(section, {})
                current["checked"] = (list(current.get("checked", []))
                                      + list(data_result.get("checked", [])))
                current["not_checked"] = (list(current.get("not_checked", []))
                                          + list(data_result.get("not_checked", [])))
                for key in ("profile_reports", "profile_report_hashes",
                            "profile_metrics"):
                    if key in data_result:
                        current[key] = data_result[key]
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
