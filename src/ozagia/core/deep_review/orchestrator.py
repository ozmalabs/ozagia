"""Compose existing Ozagia review data into a subject-based report."""
from __future__ import annotations
import hashlib
import json
import os
import importlib.metadata
import re
import subprocess
import sys
import tempfile
import time
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from ozagia.core.git_safety import safe_git_env
from . import bench as bench_runner, collect
from . import baseline as baseline_loader
from .contract import (SUBJECTS, canonical_json, cross_subject_identity,
                       finding_id)
from .prioritization import prioritize_findings
from .reports import (MAP, _cell, rank_findings, render_method, render_readme,
                      render_subject, sources_for_subject, subject_for_section)


class DeepReviewError(ValueError):
    pass


_EVIDENCE_HASH_LIMIT = 1024 * 1024
_PLAN_INPUT_FILE_LIMIT = 5_000
_PLAN_INPUT_BYTE_LIMIT = 20 * 1024 * 1024
_OUTPUT_HASH_CHUNK_SIZE = 1024 * 1024
_SHA256_VALUE = re.compile(r"(?:sha256:)?[0-9a-f]{64}\Z")


def _is_sha256_value(value: Any) -> bool:
    """Accept only canonical SHA-256 text in plan input descriptors."""
    return isinstance(value, str) and _SHA256_VALUE.fullmatch(value) is not None


def _hash_output_artifact(path: Path) -> str:
    """Hash a stable regular artifact without following a final symlink."""
    if not hasattr(os, "O_NOFOLLOW"):
        raise DeepReviewError("safe output artifact hashing is unavailable")
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise DeepReviewError("output artifact is not a regular file")
        remaining = before.st_size
        while remaining:
            chunk = handle.read(min(remaining, _OUTPUT_HASH_CHUNK_SIZE))
            if not chunk:
                raise DeepReviewError("output artifact changed while hashing")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise DeepReviewError("output artifact changed while hashing")
    return "sha256:" + digest.hexdigest()
_PRIORITY_GRAPH_BYTE_LIMIT = 32 * 1024 * 1024
_EXCERPT_CONTEXT_LINES = 5
_EXCERPT_LINE_LIMIT = 40
_EXCERPT_BYTE_LIMIT = 8 * 1024
_EXCERPT_FILE_LIMIT = 250
_OMISSION_LIMIT = 40
_FINDING_LOCATION_LIMIT = 50
_PLAN_SKIP_DIRS = {".git", ".ozagia", ".venv", "venv", "node_modules",
                   "__pycache__", ".tox", "target", "dist", "build"}


def _is_deep_review_output(root: Path, directory: Path, *, deadline: float) -> bool:
    """Recognize and exclude an earlier deep-review report directory."""
    if time.monotonic() >= deadline:
        return False
    try:
        rel = (directory / "manifest.json").relative_to(root).as_posix()
    except ValueError:
        return False
    # A real manifest can grow with the finding and subject summaries. Keep
    # parsing bounded, but don't make report recognition depend on the small
    # source-excerpt limit.
    fd, omission = _open_excerpt_source(root, rel, max_bytes=8 * 1024 * 1024)
    if omission:
        return False
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            return False
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return False
    if (not isinstance(payload, Mapping)
            or payload.get("schema") != "ozagia.deep_review.manifest.v1"
            or payload.get("plan_hash_schema") != "ozagia.deep_review.plan.v1"
            or not _is_sha256_value(payload.get("plan_hash"))):
        return False

    # A schema string alone is easy to forge. Verify the hashes of the core
    # report artifacts through the same pinned, no-follow reader before
    # excluding this directory from the workspace fingerprint.
    hashes = payload.get("output_hashes")
    required = ("00-method-and-scope.md", "findings.json",
                "findings-index.md", "README.md")
    if not isinstance(hashes, Mapping) or any(
            not _is_sha256_value(hashes.get(name)) for name in required):
        return False
    total_bytes = len(raw)
    for name in required:
        if time.monotonic() >= deadline:
            return False
        artifact_rel = (directory / name).relative_to(root).as_posix()
        remaining = 32 * 1024 * 1024 - total_bytes
        if remaining <= 0:
            return False
        artifact_limit = min(16 * 1024 * 1024, remaining)
        fd, omission = _open_excerpt_source(
            root, artifact_rel, max_bytes=artifact_limit)
        if omission:
            return False
        try:
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(artifact_limit + 1)
        except OSError:
            return False
        if len(data) > artifact_limit:
            return False
        total_bytes += len(data)
        if total_bytes > 32 * 1024 * 1024:
            return False
        if "sha256:" + hashlib.sha256(data).hexdigest() != hashes[name]:
            return False
    return True


def _workspace_input_digest(root: Path, out: Path, *, deadline: float
                            ) -> dict[str, Any]:
    """Fingerprint a bounded workspace snapshot without retaining source text."""
    result: dict[str, Any] = {
        "state": "complete", "sha256": None, "files_seen": 0,
        "files_hashed": 0, "bytes_hashed": 0, "files_omitted": 0,
        "walk_errors": 0,
        "file_limit": _PLAN_INPUT_FILE_LIMIT,
        "byte_limit": _PLAN_INPUT_BYTE_LIMIT,
        "per_file_limit": _EVIDENCE_HASH_LIMIT,
    }
    if time.monotonic() >= deadline:
        result["state"] = "skipped_budget_exhausted"
        return result
    if out == root:
        result["state"] = "skipped_output_is_root"
        return result
    exclude_out = out if out.is_relative_to(root) else None
    digest = hashlib.sha256()
    file_count = files_hashed = bytes_hashed = files_omitted = 0
    stopped = False
    budget_stopped = False
    walk_errors = 0

    def record_walk_error(_error):
        nonlocal walk_errors
        walk_errors += 1

    try:
        for base, dirs, names in os.walk(
                root, followlinks=False, onerror=record_walk_error):
            if time.monotonic() >= deadline:
                budget_stopped = True
                break
            base_path = Path(base)
            if exclude_out is not None:
                dirs[:] = [name for name in sorted(dirs)
                           if name not in _PLAN_SKIP_DIRS
                           and not (base_path / name).is_symlink()
                           and not (base_path / name == exclude_out
                                    or exclude_out in (base_path / name).parents)
                           and not _is_deep_review_output(
                               root, base_path / name, deadline=deadline)]
            else:
                dirs[:] = [name for name in sorted(dirs)
                           if name not in _PLAN_SKIP_DIRS
                           and not (base_path / name).is_symlink()
                           and not _is_deep_review_output(
                               root, base_path / name, deadline=deadline)]
            for name in sorted(names):
                if time.monotonic() >= deadline:
                    budget_stopped = True
                    break
                path = base_path / name
                if path.is_symlink() or not path.is_file():
                    continue
                if file_count >= _PLAN_INPUT_FILE_LIMIT:
                    files_omitted += 1
                    stopped = True
                    break
                rel = path.relative_to(root).as_posix()
                try:
                    size = path.stat(follow_symlinks=False).st_size
                except OSError:
                    size = -1
                digest.update(rel.encode("utf-8", errors="surrogateescape"))
                digest.update(b"\0")
                if (size < 0 or size > _EVIDENCE_HASH_LIMIT
                        or bytes_hashed + max(size, 0) > _PLAN_INPUT_BYTE_LIMIT):
                    digest.update(b"omitted\0")
                    digest.update(str(size).encode("ascii"))
                    digest.update(b"\0")
                    files_omitted += 1
                    file_count += 1
                    continue
                source_fd, omission = _open_excerpt_source(root, rel)
                if omission:
                    digest.update(b"unreadable\0")
                    files_omitted += 1
                    file_count += 1
                    continue
                try:
                    with os.fdopen(source_fd, "rb") as handle:
                        content = handle.read(_EVIDENCE_HASH_LIMIT + 1)
                except OSError:
                    digest.update(b"unreadable\0")
                    files_omitted += 1
                    file_count += 1
                    continue
                if len(content) > _EVIDENCE_HASH_LIMIT:
                    digest.update(b"oversized\0")
                    files_omitted += 1
                else:
                    digest.update(hashlib.sha256(content).digest())
                    files_hashed += 1
                    bytes_hashed += len(content)
                digest.update(b"\0")
                file_count += 1
            if stopped:
                break
            if budget_stopped:
                break
    except OSError:
        walk_errors += 1
    result["state"] = ("partial_walk_error" if walk_errors else
                       "truncated_budget" if budget_stopped else
                       "truncated" if stopped or files_omitted else
                       "complete")
    result.update({"sha256": "sha256:" + digest.hexdigest(),
                   "files_seen": file_count, "files_hashed": files_hashed,
                   "bytes_hashed": bytes_hashed,
                   "files_omitted": files_omitted,
                   "walk_errors": walk_errors})
    return result


def _workspace_file_digest(root: Path, rel: str, limit: int) -> dict[str, Any]:
    """Hash one optional workspace input via a no-follow bounded read."""
    try:
        info = (root / rel).stat(follow_symlinks=False)
        size = info.st_size
    except FileNotFoundError:
        return {"state": "missing", "sha256": None}
    except OSError:
        return {"state": "unavailable", "sha256": None}
    if size > limit:
        return {"state": "oversized", "sha256": None, "limit": limit}
    source_fd, omission = _open_excerpt_source(root, rel)
    if omission:
        return {"state": "unavailable", "sha256": None}
    try:
        with os.fdopen(source_fd, "rb") as handle:
            content = handle.read(limit + 1)
    except OSError:
        return {"state": "unavailable", "sha256": None}
    if len(content) > limit:
        return {"state": "oversized", "sha256": None, "limit": limit}
    return {"state": "hashed", "sha256": "sha256:" +
            hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def _canonical_input_digest(value: Any, *, max_bytes: int) -> dict[str, Any]:
    """Hash bounded JSON-compatible input without returning its contents."""
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        return {"state": "unavailable", "sha256": None}
    if len(encoded) > max_bytes:
        return {"state": "oversized", "sha256": None,
                "bytes": len(encoded), "limit": max_bytes}
    return {"state": "hashed", "sha256": "sha256:" +
            hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}


def _plan_hash(*, root: Path, out: Path, budget_available: bool,
               revision: str | None, graph_hash: str | None, profile: str,
               range_spec: str | None, budget_s: float, index: bool,
               fail_on: str, max_findings: int | None, focus_subjects=(), security_tools,
               security_network: bool, local_advisories, license_policy,
               baseline, baseline_findings, profile_reports, bench_commands,
               bench_timeout_s: float, tool_versions, sections, findings,
               security_provenance, deadline: float
               ) -> tuple[str, dict[str, Any]]:
    """Hash resolved review options and bounded content digests, excluding output."""
    dependency = sections.get("dependencies") or {}
    inventory = dependency.get("inventory") or {}
    advisory = inventory.get("local_advisories") or {}
    policy = inventory.get("license_policy") or {}
    profile_hashes = []
    for subject in ("performance", "scalability", "failure-modes"):
        row = sections.get(subject) or {}
        for value in row.get("profile_report_hashes") or []:
            if _is_sha256_value(value):
                profile_hashes.append(value)
    if not profile_hashes:
        for finding in findings:
            profile_evidence = (finding.get("profile_evidence") or {})
            report_hash = profile_evidence.get("report_hash")
            if _is_sha256_value(report_hash):
                profile_hashes.append(report_hash)
    profile_reports_requested = len(profile_reports or [])
    profile_input_state = (
        "not_supplied" if profile_reports_requested == 0 else
        "hashed" if len(profile_hashes) == profile_reports_requested else
        "partial_hashes_unavailable")
    advisory_input = {
        "state": advisory.get(
            "state", "not_supplied" if local_advisories is None
            else "supplied_unprocessed"),
        "sha256": (advisory.get("source_hash")
                   if _is_sha256_value(advisory.get("source_hash")) else None),
    }
    policy_input = {
        "state": policy.get(
            "state", "not_supplied" if license_policy is None
            else "supplied_unprocessed"),
        "sha256": (policy.get("policy_hash")
                   if _is_sha256_value(policy.get("policy_hash")) else None),
    }
    baseline_input = ({"state": "not_supplied", "sha256": None}
                      if baseline is None else
                      _canonical_input_digest(baseline_findings,
                                              max_bytes=32 * 1024 * 1024))
    command_hashes = []
    if bench_commands is not None:
        for command in bench_commands:
            command_hashes.append("sha256:" + hashlib.sha256(
                canonical_json(list(command)).encode("utf-8")).hexdigest())
    project_config = (_workspace_file_digest(root, ".ozagia.toml", 1024 * 1024)
                      if budget_available else
                      {"state": "skipped_budget_exhausted", "sha256": None})
    workspace = _workspace_input_digest(root, out, deadline=deadline)
    scanner_rows = []
    if isinstance(security_provenance, Mapping):
        for row in security_provenance.get("results") or []:
            if isinstance(row, Mapping):
                scanner_rows.append({key: row[key] for key in
                                     ("name", "kind", "version", "report_hash")
                                     if isinstance(row.get(key), (str, int, float, bool))})
    scanner_digest = _canonical_input_digest(
        sorted(scanner_rows, key=lambda row: (str(row.get("name")),
                                              str(row.get("version")))),
        max_bytes=64 * 1024)
    inputs = {
        "revision": revision,
        "graph_sha256": graph_hash,
        "workspace": workspace,
        "project_config": project_config,
        "local_advisories": advisory_input,
        "license_policy": policy_input,
        "baseline_findings": baseline_input,
        "profile_reports": {"state": profile_input_state,
                             "supplied": profile_reports_requested,
                             "sha256": profile_hashes},
        "security_scanners": scanner_digest,
    }
    payload = {
        "schema": "ozagia.deep_review.plan.v1",
        "profile": profile,
        "range": range_spec,
        "budget_s": float(budget_s),
        "index": bool(index),
        "fail_on": fail_on,
        "max_findings": max_findings,
        "focus_subjects": sorted(focus_subjects),
        "security_tools": sorted(set(security_tools or [])),
        "security_network": bool(security_network),
        "local_advisories_supplied": local_advisories is not None,
        "license_policy_supplied": license_policy is not None,
        "baseline_supplied": baseline is not None,
        "profile_reports_supplied": profile_reports_requested,
        "bench_command_hashes": command_hashes,
        "bench_timeout_s": float(bench_timeout_s) if bench_commands is not None else None,
        "tool_versions": dict(sorted(tool_versions.items())),
        "inputs": inputs,
    }
    encoded = canonical_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest(), payload
_MANAGED_EXCERPT = re.compile(r"dr-[0-9a-f]{16}-L[1-9][0-9]*\.txt\Z")


def _report_path(value: Any) -> str | None:
    """Accept only repository-relative paths for finding locations."""
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute()
            or any(part in {".", ".."} for part in path.parts)):
        return None
    return path.as_posix()


def _finding_locations(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract locations from explicit file data, never arbitrary identity keys."""
    line = raw.get("line")
    candidates = raw.get("locations")
    if isinstance(candidates, list):
        locations = []
        for location in candidates:
            if not isinstance(location, Mapping):
                continue
            path = _report_path(location.get("path"))
            if path is None:
                continue
            item = {key: value for key, value in location.items()
                    if key != "path"}
            item["path"] = path
            if isinstance(line, int) and not isinstance(line, bool):
                item.setdefault("line", line)
            locations.append(item)
        if locations:
            return locations

    path = _report_path(raw.get("file"))
    paths = [path] if path else []
    if not paths:
        keys = raw.get("keys")
        if isinstance(keys, (list, tuple)):
            paths = [candidate for key in keys
                     if isinstance(key, str) and key.startswith("file:")
                     if (candidate := _report_path(key[5:])) is not None]
    locations = []
    for path in list(dict.fromkeys(paths))[:_FINDING_LOCATION_LIMIT]:
        location = {"path": path}
        if isinstance(line, int) and not isinstance(line, bool):
            location["line"] = line
        locations.append(location)
    return locations

IDENTITY_POLICY = "canonical_subject_exact_finding_id_v1"
MOVE_POLICY = ("Moved or renamed paths that lack a stable rule/symbol identity "
               "are reported as new plus resolved or unverified absent according to coverage; "
               "no move mapping is inferred.")


def _priority_graph_worker(root: Path):
    """Verify and load the graph inside the killable worker process.

    Check file identity around verification and the bounded raw read so a
    replaced snapshot is never paired with the verifier's freshness result.
    """
    from ozagia.core import repo_review

    path = repo_review._graph_file(root, None)
    try:
        payload, before = repo_review._read_graph_nofollow(
            path, max_bytes=_PRIORITY_GRAPH_BYTE_LIMIT)
        graph = json.loads(payload)
        if not isinstance(graph, dict):
            return None, {"freshness": "error", "reason":
                          "graph snapshot root is not an object"}
        head = _revision(root)
        if not head:
            return None, {"freshness": "unknown", "reason":
                          "current checkout revision could not be verified"}
        info, dev, _index = repo_review._load_graph(
            root, path, head, snapshot_data=graph,
            allow_project_adapters=False)
        after_payload, after_verify = repo_review._read_graph_nofollow(
            path, max_bytes=_PRIORITY_GRAPH_BYTE_LIMIT)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if (identity(before) != identity(after_verify)
                or payload != after_payload):
            return None, {"freshness": "unknown", "reason":
                          "graph snapshot changed during freshness verification"}
        if info.get("freshness") != "fresh" or not info.get("usable"):
            return None, {"freshness": info.get("freshness", "unknown"),
                          "reason": info.get("reason") or "graph freshness was not established"}
        return graph, {"freshness": "fresh", "reason": info.get("reason"),
                       "path": str(path), "nodes": info.get("nodes"),
                       "edges": info.get("edges"),
                       "content_hash": "sha256:" + hashlib.sha256(payload).hexdigest()}
    except FileNotFoundError:
        return None, {"freshness": "error", "reason":
                      "graph snapshot is missing"}
    except OSError as exc:
        if "bounded read limit" in str(exc):
            return None, {"freshness": "omitted", "reason":
                          "graph snapshot exceeds the 32 MiB read bound"}
        return None, {"freshness": "error",
                      "reason": f"graph evidence unavailable: {exc}"}
    except (ValueError, json.JSONDecodeError) as exc:
        return None, {"freshness": "error", "reason": f"graph evidence unavailable: {exc}"}


def _verified_priority_graph(root: Path, *, timeout_s: float = 2.0):
    """Bound graph verification and loading with a killable subprocess."""
    code = ("import json, sys\n"
            "from pathlib import Path\n"
            "from ozagia.core.deep_review.orchestrator import _priority_graph_worker\n"
            "graph, info = _priority_graph_worker(Path(sys.argv[1]))\n"
            "print(json.dumps({'graph': graph, 'info': info}, separators=(',', ':')))\n")
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(root)], capture_output=True,
            text=True, timeout=max(0.001, float(timeout_s)), check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired:
        return None, {"freshness": "unknown", "reason":
                      "graph verification exceeded the remaining review budget"}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None, {"freshness": "unknown", "reason":
                      "bounded graph verification could not be started"}
    if result.returncode != 0:
        return None, {"freshness": "unknown", "reason":
                      "bounded graph verification subprocess failed"}
    try:
        envelope = json.loads(result.stdout)
        if not isinstance(envelope, dict) or not isinstance(envelope.get("info"), dict):
            raise ValueError("invalid worker result")
        graph = envelope.get("graph")
        if graph is not None and not isinstance(graph, dict):
            raise ValueError("invalid graph result")
        return graph, envelope["info"]
    except (json.JSONDecodeError, ValueError):
        return None, {"freshness": "unknown", "reason":
                      "bounded graph verification returned invalid data"}


def _revision(root: Path):
    """Return the checked-out revision when git metadata is available."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(root),
             "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
            env=safe_git_env(), stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) else None


def _tool_versions():
    versions = {"python": sys.version.split()[0]}
    try:
        versions["ozagia"] = importlib.metadata.version("ozagia")
    except importlib.metadata.PackageNotFoundError:
        versions["ozagia"] = "unknown"
    try:
        result = subprocess.run(["git", "--version"], capture_output=True,
                                text=True, timeout=2, check=False)
        if result.returncode == 0:
            versions["git"] = result.stdout.strip().removeprefix("git version ")
    except (OSError, subprocess.SubprocessError):
        pass
    return versions


def _deduplicate(findings):
    """Merge repeated collector results while retaining every evidence pointer."""
    merged = {}
    for item in findings:
        key = item["id"]
        if key not in merged:
            merged[key] = item
            continue
        current = merged[key]
        for field in ("locations", "evidence", "tags", "related_ids"):
            values = current.setdefault(field, [])
            seen = {canonical_json(value) for value in values}
            for value in item.get(field, []):
                encoded = canonical_json(value)
                if encoded not in seen:
                    values.append(value)
                    seen.add(encoded)
        # Keep the strongest evidence assertion only when independently present.
        rank = {"declared": 0, "inferred": 1, "observed": 2, "verified": 3}
        if rank.get(item.get("evidence_kind"), 0) > rank.get(current.get("evidence_kind"), 0):
            current["evidence_kind"] = item["evidence_kind"]
        if item.get("impact") and not current.get("impact"):
            current["impact"] = item["impact"]
        if item.get("recommendation") and not current.get("recommendation"):
            current["recommendation"] = item["recommendation"]
    return list(merged.values())


def _canonicalize_cross_subject(findings, raw_by_id):
    """Merge duplicate rule observations, retaining subject views and evidence."""
    groups = {}
    for item in findings:
        raw = raw_by_id.get(item["id"], {})
        identity = cross_subject_identity(raw)
        if identity is None:
            groups[("single", item["id"])] = [item]
        else:
            groups.setdefault(("identity", *identity), []).append(item)

    subject_order = {subject: index for index, subject in enumerate(SUBJECTS)}
    severity_rank = {"blocking": 0, "major": 1, "minor": 2, "note": 3}
    confidence_rank = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
    merged = []
    id_aliases = {}
    for group in groups.values():
        group = sorted(group, key=lambda row: (
            subject_order.get(row["subject"], len(SUBJECTS)), row["id"]))
        subjects = sorted({row["subject"] for row in group},
                          key=lambda subject: subject_order.get(subject, len(SUBJECTS)))
        canonical = dict(group[0])
        if len(subjects) > 1:
            canonical["subjects"] = subjects
            canonical["subject_observations"] = [
                {key: row.get(key) for key in
                 ("subject", "type", "title", "impact", "recommendation",
                  "severity", "confidence", "evidence_kind")}
                for row in group]
            for field in ("locations", "evidence", "tags", "related_ids"):
                values = []
                seen = set()
                for row in group:
                    for value in row.get(field, []):
                        encoded = canonical_json(value)
                        if encoded not in seen:
                            values.append(value)
                            seen.add(encoded)
                if values:
                    canonical[field] = sorted(values, key=canonical_json)
            canonical["severity"] = min(
                (row["severity"] for row in group),
                key=lambda value: severity_rank.get(value, 4))
            canonical["confidence"] = min(
                (row["confidence"] for row in group),
                key=lambda value: confidence_rank.get(value, 4))
            evidence_rank = {"declared": 0, "inferred": 1, "observed": 2,
                             "verified": 3}
            canonical["evidence_kind"] = max(
                (row["evidence_kind"] for row in group),
                key=lambda value: evidence_rank.get(value, 0))
            for row in group:
                id_aliases[row["id"]] = canonical["id"]
        merged.append(canonical)
    for item in merged:
        item["related_ids"] = sorted({
            id_aliases.get(identifier, identifier)
            for identifier in item.get("related_ids", [])
            if id_aliases.get(identifier, identifier) != item["id"]
        })
    return merged


def _run_status(subjects):
    statuses = [row["status"] for row in subjects.values()]
    if statuses and all(status == "complete" for status in statuses):
        return "complete"
    if any(status in {"complete", "partial"} for status in statuses):
        return "partial"
    return "unknown"


def _baseline_comparison(previous, findings, *, sections=None):
    """Compare current findings to a baseline using exact finding IDs.

    No similarity, basename, or directory heuristic is used. A moved file gets
    a new ID and is therefore reported as both resolved and new; the policy is
    recorded explicitly so callers cannot mistake this for a move mapping.
    """
    old_by_id = {str(item["id"]): item for item in previous
                 if isinstance(item, Mapping) and isinstance(item.get("id"), str)}
    current_by_id = {str(item["id"]): item for item in findings
                     if isinstance(item, Mapping) and isinstance(item.get("id"), str)}
    new_ids = sorted(set(current_by_id) - set(old_by_id))
    absent_ids = sorted(set(old_by_id) - set(current_by_id))
    # An absent ID is only classified as resolved when all of its attributed
    # subjects were completely reviewed. A profile exclusion, budget limit or
    # unavailable collector cannot establish that the old finding disappeared.
    def covered(identifier):
        item = old_by_id[identifier]
        subjects = item.get("subjects") or [item.get("subject")]
        return (isinstance(sections, Mapping) and bool(subjects)
                and all(isinstance(subject, str)
                        and isinstance(sections.get(subject), Mapping)
                        and sections[subject].get("status") == "complete"
                        for subject in subjects))

    resolved_ids = [identifier for identifier in absent_ids if covered(identifier)]
    unverified_ids = [identifier for identifier in absent_ids if not covered(identifier)]
    unchanged_ids = sorted(set(old_by_id) & set(current_by_id))

    by_subject, by_severity = {}, {}
    groups = (("new", new_ids, current_by_id),
              ("resolved", resolved_ids, old_by_id),
              ("unverified_absent", unverified_ids, old_by_id),
              ("unchanged", unchanged_ids, current_by_id))
    for status, identifiers, source in groups:
        for identifier in identifiers:
            item = source[identifier]
            subject = str(item.get("subject") or "unknown")
            severity = str(item.get("severity") or "unknown")
            for bucket, key in ((by_subject, subject), (by_severity, severity)):
                row = bucket.setdefault(key, {
                    "new": 0, "resolved": 0, "unverified_absent": 0, "unchanged": 0})
                row[status] += 1

    def sorted_buckets(bucket):
        return {key: dict(counts) for key, counts in sorted(bucket.items())}

    return {
        "identity_policy": IDENTITY_POLICY,
        "baseline_compatibility": (
            "Older baselines with one finding per subject can retain the canonical "
            "subject ID; redundant secondary-subject IDs are absent and their classification depends on current coverage."),
        "move_policy": MOVE_POLICY,
        "move_mapping": "unknown",
        "new": new_ids,
        "resolved": resolved_ids,
        "unverified_absent": unverified_ids,
        "coverage_policy": "Absent baseline findings are resolved only when all attributed current subjects are complete; otherwise absence is unverified.",
        "unchanged": unchanged_ids,
        "by_subject": sorted_buckets(by_subject),
        "by_severity": sorted_buckets(by_severity),
        "totals": {"new": len(new_ids), "resolved": len(resolved_ids),
                   "unverified_absent": len(unverified_ids),
                   "unchanged": len(unchanged_ids)},
    }


def _security_provenance(section):
    """Summarize opt-in scanner provenance for the manifest."""
    if not isinstance(section, Mapping):
        return None
    tools = section.get("scanner_tools")
    if not isinstance(tools, Mapping):
        return None
    results = []
    from ozagia.core.security import redact_text
    for item in tools.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        row = {key: item[key] for key in (
            "name", "kind", "status", "version", "error", "findings",
            "report_hash") if key in item}
        for key in ("version", "error"):
            if isinstance(row.get(key), str):
                row[key] = redact_text(row[key])[:300]
        results.append(row)
    statuses, hashes = {}, {}
    for item in results:
        status = str(item.get("status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        if item.get("report_hash"):
            hashes[str(item.get("name"))] = item["report_hash"]
    unique_hashes = sorted(set(hashes.values()))
    return {
        "requested": [str(name) for name in tools.get("requested") or []],
        "network_allowed": tools.get("network_allowed") is True,
        "results": results,
        "statuses": dict(sorted(statuses.items())),
        "report_hashes": dict(sorted(hashes.items())),
        "report_hash": unique_hashes[0] if len(unique_hashes) == 1 else None,
    }


def _write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _write_bytes(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _clear_previous_report_artifacts(out: Path) -> None:
    """Remove known artifacts from an earlier run in a reused output dir.

    Unrelated user files are left untouched. In particular, removing stale
    evidence/model/benchmark artifacts prevents them from appearing current
    when their generating options or findings are absent in this run.
    """
    names = {
        "00-method-and-scope.md", "README.md", "findings.json",
        "findings-index.md", "manifest.json", "sbom.json",
        "bench-results.json", "model-review.json", "evidence",
    }
    names.update(f"{index:02d}-{subject}.md"
                 for index, subject in enumerate(SUBJECTS, 1))
    blockers = []
    for name in names:
        path = out / name
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if name == "evidence":
            # Evidence is a directory with individually managed excerpt files;
            # never recursively remove it or follow a symlink to it.
            continue
        if stat.S_ISDIR(mode) or not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            blockers.append(name)
    if blockers:
        raise DeepReviewError(
            "report output contains non-report directories or special files at: "
            + ", ".join(sorted(blockers)))

    for name in names - {"evidence"}:
        path = out / name
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            path.unlink()

    evidence = out / "evidence"
    try:
        evidence_mode = evidence.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(evidence_mode):
        # Leave symlinks and other caller-owned entries in place. The evidence
        # writer will fail closed and mark excerpts omitted.
        return
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None or os.name != "posix":
        raise DeepReviewError(
            "cannot safely refresh an existing evidence directory on this platform")
    flags = os.O_RDONLY | directory | nofollow
    try:
        evidence_fd = os.open(evidence, flags)
    except OSError as exc:
        raise DeepReviewError(
            "cannot safely open the existing evidence directory") from exc
    try:
        # scandir(fd) and unlink(..., dir_fd=fd) stay anchored to the directory
        # opened with O_NOFOLLOW even if its path is replaced concurrently.
        with os.scandir(evidence_fd) as entries:
            for entry in entries:
                if not _MANAGED_EXCERPT.fullmatch(entry.name):
                    continue
                try:
                    mode = entry.stat(follow_symlinks=False).st_mode
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                    try:
                        os.unlink(entry.name, dir_fd=evidence_fd)
                    except FileNotFoundError:
                        pass
    finally:
        os.close(evidence_fd)


def _write_evidence_bytes(out: Path, name: str, data: bytes) -> None:
    """Write an excerpt through pinned, non-symlink output directory handles."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    out_fd = os.open(out, flags)
    evidence_fd = None
    tmp = None
    try:
        try:
            os.mkdir("evidence", mode=0o700, dir_fd=out_fd)
        except FileExistsError:
            pass
        evidence_fd = os.open("evidence", flags, dir_fd=out_fd)
        # A report identifier is hex, but keep this helper safe if reused.
        if Path(name).name != name or name in {".", ".."}:
            raise OSError("invalid evidence filename")
        tmp = f".{name}.{os.getpid()}.{time.monotonic_ns()}"
        file_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                          getattr(os, "O_NOFOLLOW", 0), 0o600,
                          dir_fd=evidence_fd)
        with os.fdopen(file_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, name, src_dir_fd=evidence_fd, dst_dir_fd=evidence_fd)
        tmp = None
    finally:
        if tmp is not None and evidence_fd is not None:
            try:
                os.unlink(tmp, dir_fd=evidence_fd)
            except FileNotFoundError:
                pass
        if evidence_fd is not None:
            os.close(evidence_fd)
        os.close(out_fd)


def _merged_coverage(packet, subject):
    """Merge raw coverage rows that route to one canonical subject."""
    rows = []
    for source in sources_for_subject(subject):
        row = packet.get("sections", {}).get(source)
        if isinstance(row, Mapping):
            rows.append(row)
    if not rows:
        return {}
    if len(rows) == 1:
        return dict(rows[0])
    merged = {}
    for key in ("checked", "not_checked", "skipped", "unknown"):
        values, seen = [], set()
        for row in rows:
            for value in row.get(key) or []:
                encoded = canonical_json(value)
                if encoded not in seen:
                    seen.add(encoded)
                    values.append(value)
        if values:
            merged[key] = values
    for row in rows:
        for key, value in row.items():
            if key not in {"checked", "not_checked", "skipped", "unknown"}:
                merged.setdefault(key, value)
    return merged


def _merge_sections(packet, incoming_sections):
    """Merge range-review coverage into the collected packet sections.

    Existing repository-review checks are preserved; range checks and omissions
    are appended once. Non-coverage facts already present in the packet win so
    a narrow range review cannot replace whole-repository scope.
    """
    names = []
    target = packet.setdefault("sections", {})
    for section, incoming in sorted((incoming_sections or {}).items()):
        if not isinstance(incoming, Mapping):
            continue
        names.append(section)
        current = target.get(section)
        if not isinstance(current, Mapping):
            target[section] = dict(incoming)
            continue
        merged = dict(current)
        for key in ("checked", "not_checked", "skipped", "unknown"):
            values = list(current.get(key) or [])
            seen = {canonical_json(value) for value in values}
            for value in incoming.get(key) or []:
                encoded = canonical_json(value)
                if encoded not in seen:
                    values.append(value)
                    seen.add(encoded)
            if values:
                merged[key] = values
        for key, value in incoming.items():
            if key not in {"checked", "not_checked", "skipped", "unknown"}:
                merged.setdefault(key, value)
        target[section] = merged
    return names


def _graph_content_hash(snapshot, info):
    """Return the graph payload hash, or None when no snapshot was used."""
    value = info.get("content_hash") if isinstance(info, Mapping) else None
    if isinstance(value, str) and value.startswith("sha256:"):
        return value
    if (isinstance(snapshot, Mapping) and isinstance(info, Mapping)
            and info.get("freshness") == "fresh"):
        try:
            payload = canonical_json(snapshot).encode("utf-8")
        except (TypeError, ValueError):
            return None
        return "sha256:" + hashlib.sha256(payload).hexdigest()
    return None


def _open_excerpt_source(root: Path, rel: str, *,
                         max_bytes: int = _EVIDENCE_HASH_LIMIT,
                         limit_description: str | None = None):
    """Open a bounded source file without following any path symlinks.

    Each directory is opened relative to the already pinned parent descriptor.
    Platforms without the required no-follow/directory flags fail closed.
    """
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None or os.name != "posix":
        return None, "safe no-follow source reads are unavailable on this platform"
    relative = Path(rel)
    if relative.is_absolute() or not relative.parts or any(
            part in {"", ".", ".."} for part in relative.parts):
        return None, "source path is outside the reviewed root"

    flags = os.O_RDONLY | directory | nofollow
    descriptors = []
    file_fd = None
    try:
        parent_fd = os.open(root, flags)
        descriptors.append(parent_fd)
        for component in relative.parts[:-1]:
            parent_fd = os.open(component, flags, dir_fd=parent_fd)
            descriptors.append(parent_fd)
        file_fd = os.open(relative.parts[-1], os.O_RDONLY | nofollow |
                          getattr(os, "O_NONBLOCK", 0),
                          dir_fd=parent_fd)
        file_stat = os.fstat(file_fd)
        if not stat.S_ISREG(file_stat.st_mode):
            os.close(file_fd)
            file_fd = None
            return None, "source file is not a regular file"
        if file_stat.st_size > max_bytes:
            os.close(file_fd)
            file_fd = None
            return None, (f"source file exceeds the {limit_description}"
                          if limit_description else
                          "source file exceeds the bounded read limit")
        result_fd, file_fd = file_fd, None
        return result_fd, None
    except FileNotFoundError:
        return None, "source file is missing"
    except OSError:
        return None, "source path is a symlink or could not be safely opened"
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _excerpt_payload(root: Path, rel: str, line: int):
    """Read a small source window bounded by lines and bytes."""
    source_fd, omission = _open_excerpt_source(
        root, rel, limit_description="1 MiB evidence excerpt limit")
    if omission:
        return None, omission, None
    try:
        with os.fdopen(source_fd, "rb") as handle:
            data = handle.read(_EVIDENCE_HASH_LIMIT + 1)
    except OSError:
        return None, "source file could not be read for an excerpt", None
    if len(data) > _EVIDENCE_HASH_LIMIT:
        return None, "source file exceeds the 1 MiB evidence excerpt limit", None
    lines = data.splitlines(keepends=True)
    if line > len(lines):
        return None, (f"line {line} is beyond the end of the "
                      f"{len(lines)}-line source file"), None
    start = max(1, line - _EXCERPT_CONTEXT_LINES)
    end = min(len(lines), line + _EXCERPT_CONTEXT_LINES)
    selected = lines[start - 1:end]
    truncated = False
    if len(selected) > _EXCERPT_LINE_LIMIT:
        selected = selected[:_EXCERPT_LINE_LIMIT]
        truncated = True
    # Excerpts are persisted artifacts. Source context can include credentials
    # even when the finding itself only records a path and rule, so apply the
    # same defense-in-depth redaction used for scanner and provider text.
    from ozagia.core.security import redact_text, secret_hits

    source_text = b"".join(selected).decode("utf-8", errors="replace")
    # A redaction pass is useful defense in depth, but it cannot prove that an
    # excerpt has no secret-like material. Fail closed when the repository's
    # secret scanner flags any line in the selected context window.
    if secret_hits(source_text):
        return None, "source excerpt contains secret-like content; excerpt omitted", None
    payload = redact_text(source_text)
    payload = payload.encode("utf-8")
    if len(payload) > _EXCERPT_BYTE_LIMIT:
        payload = payload[:_EXCERPT_BYTE_LIMIT]
        # Avoid writing an invalid UTF-8 tail after byte bounding.
        payload = payload.decode("utf-8", errors="ignore").encode("utf-8")
        truncated = True
    if not payload:
        return None, f"line {line} has no readable source text", None
    return payload, None, {"start": start, "end": end, "truncated": truncated}


def _mark_excerpt_omitted(finding, reason):
    finding["excerpt_omitted"] = reason
    evidence = finding.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict):
                item["excerpt_omitted"] = reason
                break


def _write_evidence_excerpts(root: Path, out: Path, findings, deadline):
    """Write bounded excerpts for findings that name a source line."""
    written, omitted = [], []
    for finding in findings:
        location = next((item for item in finding.get("locations", [])
                         if isinstance(item, Mapping) and item.get("path")
                         and isinstance(item.get("line"), int)
                         and not isinstance(item.get("line"), bool)
                         and item["line"] > 0), None)
        if location is None:
            continue
        if time.monotonic() >= deadline:
            reason = "review budget exhausted before evidence excerpt"
            _mark_excerpt_omitted(finding, reason)
            omitted.append((finding.get("id"), reason))
            continue
        if len(written) >= _EXCERPT_FILE_LIMIT:
            reason = f"evidence excerpt file limit reached ({_EXCERPT_FILE_LIMIT})"
            _mark_excerpt_omitted(finding, reason)
            omitted.append((finding.get("id"), reason))
            continue
        rel, line = str(location["path"]), int(location["line"])
        payload, omission, meta = _excerpt_payload(root, rel, line)
        if omission:
            _mark_excerpt_omitted(finding, omission)
            omitted.append((finding.get("id"), omission))
            continue
        name = f"{finding['id']}-L{line}.txt"
        try:
            _write_evidence_bytes(out, name, payload)
        except OSError:
            reason = "evidence output directory is unavailable or is a symlink"
            _mark_excerpt_omitted(finding, reason)
            omitted.append((finding.get("id"), reason))
            continue
        path = out / "evidence" / name
        ref = "evidence/" + name
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        finding.update({"excerpt_ref": ref, "excerpt_hash": digest})
        first_evidence = (finding.get("evidence") or [None])[0]
        if isinstance(first_evidence, dict):
            first_evidence.update({
                "excerpt_ref": ref, "excerpt_hash": digest,
                "excerpt_line_start": meta["start"],
                "excerpt_line_end": meta["end"],
                "excerpt_truncated": meta["truncated"],
            })
        written.append(path)
    return written, omitted


def _method_omissions(sections):
    """Render bounded, deterministic per-subject omissions for the method file."""
    rows = []
    for subject in SUBJECTS:
        row = sections.get(subject) or {}
        reasons = []
        for key in ("omitted", "skipped", "unknown"):
            for item in row.get(key) or []:
                text = str(item.get("reason", item) if isinstance(item, Mapping) else item)
                label = f"{key}: {text}"
                if label not in reasons:
                    reasons.append(label)
        if reasons:
            shown = reasons[:5]
            more = f"; +{len(reasons) - len(shown)} more" if len(reasons) > len(shown) else ""
            rows.append(f"{subject}: " + "; ".join(shown) + more)
    if len(rows) > _OMISSION_LIMIT:
        rows = rows[:_OMISSION_LIMIT] + [
            f"+{len(rows) - _OMISSION_LIMIT} more subjects with omissions"]
    return rows


def _record_range_failure(packet, range_spec, exc):
    """Record an isolated range-review failure as partial coverage."""
    from ozagia.core import security

    detail = security.redact_text(str(exc)).strip()[:300]
    timed_out = isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
    label = "range review timed out" if timed_out else "range review unavailable"
    reason = f"{label}: {type(exc).__name__}"
    if detail:
        reason += f": {detail}"
    current = packet.setdefault("sections", {}).setdefault("correctness", {})
    current.setdefault("not_checked", []).append(
        {"area": "range", "reason": reason})
    current["range_failed"] = True
    packet["range_review"] = {
        "range": range_spec,
        "graph": None,
        "test_coverage": None,
        "security_impact": None,
        "blast_radius": None,
        "sections": ["correctness"],
        "range_subject": {"correctness": "correctness"},
        "range_subjects": {"correctness": "correctness"},
        "status": "unavailable",
        "error": reason,
    }
    return reason


def build_deep_review(root, out, *, profile="full", budget_s=60, index=False,
                      baseline=None, fail_on="never", max_findings=None,
                      range_spec=None, profile_reports=None, bench_commands=None,
                      bench_timeout_s=30, security_tools=None,
                      security_network=False, local_advisories=None,
                      license_policy=None, focus_subjects=None):
    root, out = Path(root).resolve(), Path(out).resolve()
    if focus_subjects is None:
        focus_subjects = ()
    if (not isinstance(focus_subjects, (list, tuple))
            or len(focus_subjects) > len(SUBJECTS)
            or any(not isinstance(subject, str) or subject not in SUBJECTS
                   for subject in focus_subjects)):
        raise DeepReviewError("focus_subjects must contain only known review subjects")
    focus_subjects = tuple(sorted(set(focus_subjects)))
    if profile != "full" and profile not in {"security", "performance", "failure-modes", "data-structures", "scalability", "architecture", "tests", "dependencies", "operations", "docs"}:
        raise DeepReviewError(f"unsupported profile: {profile}")
    if local_advisories is not None and profile not in {"full", "dependencies"}:
        raise DeepReviewError(
            "local_advisories requires the full or dependencies profile")
    if license_policy is not None and profile not in {"full", "dependencies"}:
        raise DeepReviewError(
            "license_policy requires the full or dependencies profile")
    if not isinstance(budget_s, (int, float)) or isinstance(budget_s, bool) or not 0 <= budget_s < float("inf"):
        raise DeepReviewError("budget_s must be >= 0")
    if fail_on not in {"blocking", "major", "never"}:
        raise DeepReviewError("fail_on must be blocking, major, or never")
    if max_findings is not None and (not isinstance(max_findings, int) or
                                     isinstance(max_findings, bool) or max_findings < 0):
        raise DeepReviewError("max_findings must be an integer >= 0")
    if profile_reports is not None and not isinstance(profile_reports, (list, tuple)):
        raise DeepReviewError("profile_reports must be a list of paths")
    if (security_tools is not None and
            (not isinstance(security_tools, (list, tuple)) or
             any(not isinstance(name, str) for name in security_tools))):
        raise DeepReviewError("security_tools must be a list/tuple of strings or None")
    # Only the exact boolean True enables network access from scanner tools.
    security_network = security_network is True
    if bench_commands is not None and not isinstance(bench_commands, (list, tuple)):
        raise DeepReviewError("bench_commands must be a list of argument vectors")
    if bench_commands is not None:
        try:
            bench_commands = bench_runner._validate_commands(bench_commands)
        except ValueError as exc:
            raise DeepReviewError(f"invalid benchmark command input: {exc}") from exc
    if (isinstance(bench_timeout_s, bool) or
            not isinstance(bench_timeout_s, (int, float)) or
            not 0 < bench_timeout_s <= bench_runner.MAX_TIMEOUT_S):
        raise DeepReviewError(
            f"bench_timeout_s must be greater than 0 and at most {bench_runner.MAX_TIMEOUT_S}")
    started = time.monotonic()
    deadline = started + budget_s
    budget_exhausted = False
    previous = None
    if baseline:
        baseline_path = Path(baseline)
        try:
            # Pass report directories through intact so the baseline loader
            # can validate manifest.json as well as findings.json. Resolving
            # the directory here bypassed that compatibility check.
            previous = baseline_loader.load_baseline(baseline_path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise DeepReviewError(f"invalid baseline findings: {exc}") from exc
    revision = _revision(root) if time.monotonic() < deadline else None
    timings = {}
    phase_started = time.monotonic()
    collect_options = {
        "profile": profile,
        "budget_s": max(0.0, deadline - time.monotonic()),
        "index": index,
        "profile_reports": profile_reports,
        "security_tools": security_tools,
        "security_network": security_network,
    }
    if local_advisories is not None:
        collect_options["local_advisories"] = local_advisories
    if license_policy is not None:
        collect_options["license_policy"] = license_policy
    packet = collect.collect(root, **collect_options)
    timings["collect_s"] = time.monotonic() - phase_started
    bench_artifact = None
    phase_started = time.monotonic()
    range_failed = False
    if range_spec is not None:
        try:
            from .range_review import collect_range
            change = collect_range(root, range_spec, budget_s=max(0.0, budget_s - (time.monotonic() - started)))
            if not isinstance(change, Mapping):
                raise ValueError("range review returned an invalid result")
            packet.setdefault("findings", []).extend(change.get("findings", []))
            range_sections = _merge_sections(packet, change.get("sections"))
            range_subjects = {section: subject_for_section(section)
                              for section in range_sections}
            packet["range_review"] = {
                "range": change.get("range"),
                "graph": change.get("graph"),
                "test_coverage": change.get("test_coverage"),
                "security_impact": change.get("security_impact"),
                "blast_radius": change.get("blast_radius"),
                "sections": range_sections,
                "range_subject": range_subjects,
                "range_subjects": range_subjects,
                "status": "available",
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 - a range failure is coverage
            range_failed = True
            _record_range_failure(packet, range_spec, exc)
    findings = []
    raw_by_id = {}
    evidence_hash_cache = {}
    normalization_omitted = 0
    for raw_index, raw in enumerate(packet["findings"]):
        # Normalization and hashing are cooperative: check between findings.
        # A single bounded file read or filesystem operation cannot be
        # preempted in-process, so this is not a hard wall-clock guarantee.
        if time.monotonic() >= deadline:
            normalization_omitted = len(packet["findings"]) - raw_index
            budget_exhausted = True
            break
        source = raw.get("section") or raw.get("subject") or ""
        subject = subject_for_section(source)
        if subject not in SUBJECTS:
            subject = "correctness"
        locs = _finding_locations(raw)
        explanation = raw.get("summary") or raw.get("message") or ""
        title = str(explanation or raw.get("type", "Review finding"))
        severity = raw.get("severity", "note")
        if severity not in {"blocking", "major", "minor", "note"}:
            severity = "note"
        confidence = raw.get("confidence", "medium")
        if confidence not in {"high", "medium", "low", "unknown"}:
            confidence = "unknown"
        profile_evidence = raw.get("profile_evidence")
        measured = isinstance(profile_evidence, dict)
        evidence = [{"kind": "measured" if measured else "static",
                     "source": ("ozagia.core.profile" if measured else
                                "ozagia.core.security" if source == "security" else
                                "ozagia.core.deep_review.analyzers.local_advisories"
                                if source == "dependencies" and
                                raw.get("type") == "local_advisory_exact_version_match" else
                                "ozagia.core.deep_review.analyzers.dependencies"
                                if source == "dependencies" and
                                raw.get("type") == "missing_lock_file" else
                                "ozagia.core.deep_review.analyzers"
                                if source in {"performance", "failure-modes",
                                              "data-structures", "scalability"} else
                                "ozagia.core.repo_review"),
                     "method": "measured_profile" if measured else
                               "offline_scan" if source == "security" else
                               "static_analysis" if source in {
                                   "performance", "failure-modes", "data-structures",
                                   "scalability"} or raw.get("type") == "missing_lock_file"
                               else "review_repo"}]
        evidence[0]["subject"] = subject
        if raw.get("rule_id"):
            evidence[0]["rule_id"] = str(raw["rule_id"])
            evidence[0]["source"] = "ozagia.core.deep_review.analyzers.operations"
            evidence[0]["method"] = (
                "static_config_rule" if str(raw["rule_id"]).startswith("iac.")
                else "explicit_observability_disable")
        # Bind static claims to the analyzed input when its path is available.
        # Hashes disclose no source text and make evidence freshness auditable.
        file_path = raw.get("file")
        if file_path:
            if time.monotonic() >= deadline:
                budget_exhausted = True
                evidence[0]["hash_omitted"] = "review budget exhausted before evidence hashing"
            else:
                lexical = root / str(file_path)
                cache_key = str(lexical)
                if cache_key not in evidence_hash_cache:
                    metadata = None
                    fd, omission = _open_excerpt_source(root, str(file_path))
                    if fd is not None:
                        try:
                            with os.fdopen(fd, "rb") as handle:
                                content = handle.read(_EVIDENCE_HASH_LIMIT + 1)
                            metadata = ({"hash": "sha256:" + hashlib.sha256(content).hexdigest()}
                                        if len(content) <= _EVIDENCE_HASH_LIMIT else
                                        {"hash_omitted": "file exceeds 1 MiB evidence hash limit"})
                        except OSError:
                            pass
                    elif omission and "exceeds" in omission:
                        metadata = {"hash_omitted": "file exceeds 1 MiB evidence hash limit"}
                    evidence_hash_cache[cache_key] = metadata
                if evidence_hash_cache[cache_key]:
                    evidence[0].update(evidence_hash_cache[cache_key])
        if isinstance(raw.get("security_evidence"), dict):
            evidence[0]["rule"] = raw["security_evidence"].get("rule")
            evidence[0]["line"] = raw["security_evidence"].get("line")
            security_evidence_kind = raw["security_evidence"].get("kind")
            evidence[0]["evidence_kind"] = (
                "observed" if security_evidence_kind == "verified"
                else security_evidence_kind)
        if raw.get("type") == "local_advisory_exact_version_match":
            evidence[0]["method"] = "exact_lock_pin_match"
            evidence[0]["advisory_id"] = raw.get("advisory_id")
            evidence[0]["advisory_source_hash"] = raw.get(
                "advisory_source_hash")
            for key in ("advisory_severity", "repository_provenance"):
                if raw.get(key) is not None:
                    evidence[0][key] = raw[key]
        if measured:
            for key in ("report", "report_hash", "profile_hash", "unit", "clock"):
                if profile_evidence.get(key) is not None:
                    evidence[0][key] = profile_evidence[key]
            for key in ("self", "inclusive", "share_bp", "delta_self", "count",
                        "errors"):
                if profile_evidence.get(key) is not None:
                    evidence[0][key] = profile_evidence[key]
        raw_evidence_kind = raw.get("evidence_kind")
        unverified_claim = (raw_evidence_kind == "verified" or
                            (isinstance(raw.get("security_evidence"), dict) and
                             raw["security_evidence"].get("kind") == "verified"))
        evidence_kind = ("observed" if unverified_claim else
                         raw_evidence_kind
                         if raw_evidence_kind in {"declared", "inferred", "observed"}
                         else "observed")
        if unverified_claim:
            reason = ("collector supplied verified evidence without an independently "
                      "checkable verification result; claim was downgraded to observed")
            evidence[0]["verification_claim_downgraded"] = True
            evidence[0]["verification_reason"] = reason
            coverage_key = str(source or MAP.get(subject) or subject)
            coverage = packet.setdefault("sections", {}).setdefault(coverage_key, {})
            if isinstance(coverage, dict):
                not_checked = coverage.setdefault("not_checked", [])
                if isinstance(not_checked, list) and not any(
                        (item.get("reason") if isinstance(item, dict) else item) == reason
                        for item in not_checked):
                    not_checked.append({"reason": reason})
        item = {"subject": subject, "type": raw.get("type", "unknown"), "title": title,
                "severity": severity, "confidence": confidence,
                "evidence_kind": evidence_kind, "status": "open", "locations": locs,
                "evidence": evidence,
                "impact": str(explanation), "recommendation": raw.get("recommendation", ""),
                "scope": None if locs else raw.get("scope", "Collector supplied no location keys."),
                "effort": "unknown", "owner": None, "tags": [], "related_ids": [],
                "truth_assessed": False}
        if raw.get("rule_id"):
            item["tags"] = [f"rule:{raw['rule_id']}"]
        item["id"] = finding_id(subject, raw)
        findings.append(item)
        raw_by_id[item["id"]] = raw
    if normalization_omitted:
        packet.setdefault("sections", {}).setdefault("correctness", {}).setdefault(
            "unknown", []).append(
                f"review budget exhausted during normalization; {normalization_omitted} findings omitted")
    findings = _deduplicate(findings)
    findings = _canonicalize_cross_subject(findings, raw_by_id)
    if time.monotonic() >= deadline:
        budget_exhausted = True
    if budget_exhausted:
        graph_snapshot, priority_graph = None, {
            "freshness": "unknown", "reason": "review budget exhausted before graph prioritization"}
    else:
        remaining = max(0.0, deadline - time.monotonic())
        graph_snapshot, priority_graph = _verified_priority_graph(
            root, timeout_s=remaining)
    churn_section = packet.get("sections", {}).get("churn") or {}
    churn_data = churn_section.get("data") if isinstance(churn_section, Mapping) else None
    hotspots = churn_data.get("hotspots") if isinstance(churn_data, Mapping) else None
    findings = prioritize_findings(
        findings, graph_snapshot, freshness=priority_graph["freshness"],
        churn_hotspots=hotspots, focus_subjects=focus_subjects)
    findings.sort(key=lambda f: (f["subject"], -f["priority"]["score"], f["id"]))
    if max_findings is not None and max_findings < 0:
        raise DeepReviewError("max_findings must be >= 0")
    all_findings = findings
    # max_findings is a global top-N view over every collected finding. It
    # never removes findings from findings.json or the per-subject reports.
    ranked_findings = rank_findings(all_findings)
    returned_findings = (ranked_findings if max_findings is None
                         else ranked_findings[:max_findings])
    graph_hash = _graph_content_hash(graph_snapshot, priority_graph)
    graph_hash_reason = None
    if graph_hash is None:
        reason = (priority_graph.get("reason")
                  if isinstance(priority_graph, Mapping) else None)
        graph_hash_reason = reason or "no verified graph snapshot was loaded"
    if time.monotonic() >= deadline:
        budget_exhausted = True
    out.mkdir(parents=True, exist_ok=True)
    _clear_previous_report_artifacts(out)
    excerpt_files, excerpt_omitted = _write_evidence_excerpts(
        root, out, ranked_findings, deadline)
    if time.monotonic() >= deadline:
        budget_exhausted = True
    sections = {}
    for n, subject in enumerate(SUBJECTS, 1):
        matching = [f for f in all_findings
                    if subject in f.get("subjects", [f["subject"]])]
        section = "correctness" if subject == "correctness" and range_spec else MAP[subject]
        source_section = _merged_coverage(packet, subject)
        checked = bool(section and source_section.get("checked"))
        not_checked = source_section.get("not_checked", [])
        skipped = source_section.get("skipped", [])
        unknown = source_section.get("unknown", [])
        def reasons(items):
            return [str(item.get("reason", item) if isinstance(item, dict) else item)
                    for item in items]
        status = ("partial" if budget_exhausted or not_checked or skipped or unknown or
                  packet.get("graph", {}).get("freshness") != "fresh" else
                  "complete") if checked else (
                      "partial" if source_section.get("range_failed") else "unknown")
        name = f"{n:02d}-{subject}.md"
        scope = source_section.get("scope") or {
            "security": "Offline source, route, dependency-manifest and secret-pattern triage; no external scanners or network.",
            "performance": "Selected static Python performance patterns.",
            "failure-modes": "Selected static Python failure-mode patterns.",
            "data-structures": "Selected static Python data-structure patterns.",
            "scalability": "Selected static Python scalability patterns.",
        }.get(subject, f"Existing repository review checks mapped to {subject}.")
        text = render_subject(subject, matching, status=status,
                              omissions=(reasons(not_checked) if not_checked else
                                         ([] if checked else ["No dedicated collector for this subject."])),
                              checks=source_section.get("checked", []) if checked else [],
                              skipped=reasons(skipped), unknown=reasons(unknown),
                              scope=scope, metrics=source_section,
                              related_findings=all_findings)
        _write(out / name, text)
        sections[subject] = {"status": status,
                             "checks": source_section.get("checked", []) if checked else [],
                             "skipped": skipped, "omitted": (not_checked or
                                                         ([] if checked else ["no collector"])),
                             "unknown": unknown, "findings": [f["id"] for f in matching],
                             "report": name, "report_hash": hashlib.sha256(text.encode()).hexdigest()}
        if "inventory" in source_section:
            sections[subject]["inventory"] = source_section["inventory"]
        if "data_inventory" in source_section:
            sections[subject]["data_inventory"] = source_section["data_inventory"]
        if source_section.get("range_failed"):
            sections[subject]["range_failed"] = True
        if subject == "security":
            for key in ("scanner_tools", "scanner_summary", "scanner_graph",
                        "files_checked", "trust_boundary_files_checked",
                        "trust_boundary_findings"):
                if key in source_section:
                    sections[subject][key] = source_section[key]
        if subject == "documents-and-api":
            for key in ("doc_coverage", "api_coverage_gap",
                        "api_docs_files_checked"):
                if key in source_section:
                    sections[subject][key] = source_section[key]
        if subject == "dependencies":
            for key in ("sbom", "cve_state", "version_conflicts"):
                if key in source_section:
                    sections[subject][key] = source_section[key]
            inventory = source_section.get("inventory")
            if "cve_state" not in sections[subject] and isinstance(inventory, Mapping):
                if inventory.get("cve_state") is not None:
                    sections[subject]["cve_state"] = inventory["cve_state"]
        for key in ("profile_reports", "profile_report_hashes", "profile_metrics"):
            if key in source_section:
                sections[subject][key] = source_section[key]
    security_provenance = _security_provenance(sections.get("security"))
    sbom_artifact = sections.get("dependencies", {}).get("sbom")
    if sbom_artifact is not None:
        _write(out / "sbom.json", canonical_json(sbom_artifact))
    tool_versions = _tool_versions()
    profile_report_paths = sorted({
        str(path) for row in sections.values()
        for path in (row.get("profile_reports") or [])})
    graph_info = (dict(packet["graph"]) if isinstance(packet.get("graph"), Mapping)
                  else {})
    graph_info["content_hash"] = graph_hash or "unknown"
    graph_info["content_hash_reason"] = graph_hash_reason
    method = render_method(
        root=root, profile=profile, revision=revision,
        tool_versions=tool_versions, budget_s=budget_s,
        graph=graph_info, graph_hash=graph_hash,
        graph_hash_reason=graph_hash_reason,
        priority_graph=priority_graph, profile_reports=profile_report_paths,
        omissions=_method_omissions(sections),
        normalization_omitted=normalization_omitted,
        range_spec=range_spec, index=index,
        bench_timeout_s=bench_timeout_s if bench_commands is not None else None,
        security_tools=security_tools, security_network=security_network)
    _write(out / "00-method-and-scope.md", method)
    _write(out / "findings.json", canonical_json(all_findings))
    index_lines = [f"Priority graph: {_cell(priority_graph['freshness'])} "
                   f"({_cell(priority_graph.get('reason', 'no reason recorded'))}).", ""]
    for finding in all_findings:
        priority = finding["priority"]
        detail = (priority["omission"] or
                  f"{priority['blast_radius']['reachable_callers']} reachable callers")
        index_lines.append(
            f"- priority {priority['score']} · {_cell(finding['severity'])} "
            f"`{_cell(finding['id'])}` {_cell(finding['subject'])}: "
            f"{_cell(finding['type'])} — {_cell(detail)}")
    _write(out / "findings-index.md", "# Findings index\n\n" +
           "\n".join(index_lines) + "\n")
    _write(out / "README.md", render_readme(sections, all_findings))
    comparison = None
    if baseline:
        comparison = _baseline_comparison(previous, all_findings, sections=sections)
    run_status = _run_status(sections)
    timings["compose_s"] = time.monotonic() - phase_started
    elapsed = time.monotonic() - started
    if elapsed > budget_s or range_failed:
        run_status = "partial"
    if bench_commands is not None:
        # Run after the deterministic report and gate have been computed. A
        # benchmark can consume budget but never changes collected findings,
        # their identities, subject status, or fail_on result.
        out.mkdir(parents=True, exist_ok=True)
        phase_started = time.monotonic()
        try:
            measured = bench_runner.run_bench(
                root, bench_commands, enabled=True, timeout_s=bench_timeout_s,
                budget_s=max(0.0, deadline - time.monotonic()),
                labels=[f"command-{index}" for index in range(1, len(bench_commands) + 1)])
        except ValueError as exc:
            raise DeepReviewError(f"invalid benchmark command input: {exc}") from exc
        # Never persist argv or captured streams. A digest identifies the exact
        # caller input without copying potentially secret arguments into reports.
        bench_rows = []
        for index, (argv, row) in enumerate(zip(bench_commands, measured["commands"]), 1):
            bench_rows.append({
                "label": f"command-{index}",
                "command_sha256": "sha256:" + hashlib.sha256(
                    canonical_json(list(argv)).encode("utf-8")).hexdigest(),
                "status": row["status"],
                "returncode": row.get("returncode"),
                "duration_ms": row.get("duration_ms", 0),
                "stdout_truncated": row.get("stdout_truncated", False),
                "stderr_truncated": row.get("stderr_truncated", False),
            })
        bench_artifact = {
            "schema": "ozagia.deep_review.bench-results.v1",
            "timeout_s_per_command": bench_timeout_s,
            "budget_s": measured["budget_s"],
            "elapsed_s": measured["elapsed_s"],
            "output_limit_bytes_per_stream": measured["output_limit_bytes_per_stream"],
            "commands": bench_rows,
            "limitations": measured["limitations"],
        }
        _write(out / "bench-results.json", canonical_json(bench_artifact))
        timings["bench_s"] = time.monotonic() - phase_started
    output_hashes = {}
    generated = [(path.name, path) for path in (
        out / "00-method-and-scope.md", out / "findings.json",
        out / "findings-index.md", out / "README.md")]
    generated.extend((row["report"], out / row["report"]) for row in sections.values())
    if bench_artifact is not None:
        generated.append(("bench-results.json", out / "bench-results.json"))
    if sbom_artifact is not None:
        generated.append(("sbom.json", out / "sbom.json"))
    generated.extend(("evidence/" + path.name, path) for path in excerpt_files)
    for name, artifact in sorted(set(generated)):
        output_hashes[name] = _hash_output_artifact(artifact)
    truncation = {
        "max_findings": {"limit": max_findings, "total": len(all_findings),
                         "returned": len(returned_findings),
                         "truncated": len(returned_findings) < len(all_findings)},
        "subjects": {subject: {
            "omitted": bool(row.get("omitted")),
            "skipped": bool(row.get("skipped")),
            "unknown": bool(row.get("unknown")),
        } for subject, row in sections.items()},
        "normalization_findings_omitted": normalization_omitted,
        "composition": "completed atomically per artifact; an individual filesystem write cannot be preempted",
        "collectors": {
            "security_findings_omitted": bool(
                (packet.get("sections", {}).get("security") or {}).get(
                    "scanner_summary", {}).get("omitted", 0)),
            "security_tools_requested": (
                (security_provenance or {}).get("requested", [])),
            "security_tool_statuses": (
                (security_provenance or {}).get("statuses", {})),
        },
    }
    # Include opt-in benchmark execution and artifact hashing in the elapsed
    # budget figures. The review status and fail-on gate were intentionally
    # fixed before benchmarks run; this field still reports actual time spent.
    elapsed = time.monotonic() - started
    plan_hash, plan_descriptor = _plan_hash(
        root=root, out=out, budget_available=time.monotonic() < deadline,
        revision=revision, graph_hash=graph_hash, profile=profile,
        range_spec=range_spec, budget_s=budget_s, index=index,
        fail_on=fail_on, max_findings=max_findings, focus_subjects=focus_subjects,
        security_tools=security_tools, security_network=security_network,
        local_advisories=local_advisories, license_policy=license_policy,
        baseline=baseline, baseline_findings=previous,
        profile_reports=profile_reports, bench_commands=bench_commands,
        bench_timeout_s=bench_timeout_s, tool_versions=tool_versions,
        sections=sections, findings=all_findings,
        security_provenance=security_provenance, deadline=deadline)
    manifest = {"schema": "ozagia.deep_review.manifest.v1", "root": str(root),
                "profile": profile, "revision": revision,
                "plan_hash": plan_hash,
                "plan_hash_schema": "ozagia.deep_review.plan.v1",
                "plan": plan_descriptor,
                "tool_versions": tool_versions,
                "graph": graph_info or packet.get("graph"),
                "graph_hash": graph_hash or "unknown",
                "graph_hash_reason": graph_hash_reason,
                "graph_content_hash": {
                    "status": "hashed" if graph_hash else "unknown",
                    "hash": graph_hash or "unknown",
                    "reason": graph_hash_reason},
                "prioritization_graph": priority_graph,
                "subjects": sections,
                "findings": len(all_findings), "complete": run_status == "complete", "status": run_status,
                "exhaustive": False, "truth_assessed": False,
                "identity_policy": IDENTITY_POLICY,
                "move_policy": MOVE_POLICY,
                "move_mapping": "unknown",
                "output_hashes": output_hashes,
                "evidence": {
                    "directory": "evidence",
                    "files": len(excerpt_files),
                    "bytes": sum(path.stat().st_size for path in excerpt_files),
                    "omitted": len(excerpt_omitted),
                    "file_limit": _EXCERPT_FILE_LIMIT,
                    "line_limit": _EXCERPT_LINE_LIMIT,
                    "byte_limit": _EXCERPT_BYTE_LIMIT,
                },
                "budget_s": budget_s,
                "budgets": {"total_s": budget_s, "elapsed_s": elapsed,
                            "remaining_s": max(0.0, budget_s - elapsed),
                            "exceeded": elapsed > budget_s},
                "timings": timings, "truncation": truncation,
                "index": bool(index), "range": range_spec,
                "range_review": packet.get("range_review"),
                "baseline": str(baseline) if baseline is not None else None}
    if security_provenance is not None:
        manifest["security"] = security_provenance
    if sbom_artifact is not None:
        manifest["sbom"] = {
            "report": "sbom.json",
            "report_hash": output_hashes["sbom.json"],
            "components": (len(sbom_artifact.get("components") or [])
                           if isinstance(sbom_artifact, Mapping) else None),
            "cve_state": sections.get("dependencies", {}).get("cve_state"),
        }
    if bench_artifact is not None:
        manifest["bench"] = {
            "report": "bench-results.json",
            "report_hash": output_hashes["bench-results.json"],
            "commands": len(bench_artifact["commands"]),
            "statuses": {status: sum(row["status"] == status
                                     for row in bench_artifact["commands"])
                         for status in sorted({row["status"]
                                               for row in bench_artifact["commands"]})},
            "truth_assessed": False,
        }
    manifest["comparison"] = comparison
    _write(out / "manifest.json", canonical_json(manifest))
    severity = {"blocking": 0, "major": 1, "minor": 2, "note": 3}
    threshold = {"blocking": 0, "major": 1}.get(fail_on)
    failed = threshold is not None and any(severity.get(f["severity"], 3) <= threshold
                                           for f in all_findings)
    return {"manifest": manifest, "findings": returned_findings, "out": str(out), "failed": failed}
