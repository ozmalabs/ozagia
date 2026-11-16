"""Optional SRE/IaC MCP tools layered over the read-only graph server.

These tools are offline unless the caller explicitly supplies an artifact. They
never invoke a cloud provider, cluster, emulator or target project. Live job
waiting remains a CLI-only operation with its own explicit gates.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from ozagia.config import state_path


def _schema(properties: dict[str, Any] | None = None,
            required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def sre_tools(memory: Any) -> list[tuple[dict[str, Any], Any]]:
    """Return ``(spec, handler)`` pairs for offline SRE/IaC operations."""
    root = Path(memory.raw_repo_root).resolve()
    ops_dir = state_path(root, "ops")
    live_dir = state_path(root, "live")

    def environments(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.config import ConfigError, load_project_config
        try:
            cfg = load_project_config(root)
        except (ConfigError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        result = []
        for item in cfg.environments:
            result.append({
                "name": item.name, "paths": list(item.paths),
                "namespaces": list(item.namespaces),
                "account_alias": item.account_alias, "region": item.region,
                "cluster": item.cluster, "risk": item.risk,
                "pipelines": list(item.pipelines),
            })
        return {"ok": True, "environments": result,
                "declared": True, "live": False,
                "note": "Declarative configuration; not a live environment read."}

    def check_iac(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.core.iac_check import check_iac as run_check
        candidate = args.get("root")
        target = root if candidate in (None, "", ".") else (root / str(candidate)).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return {"ok": False, "error": "root must stay inside the repository"}
        if not target.is_dir():
            return {"ok": False, "error": "root is not a directory"}
        result = run_check(target)
        return {
            "ok": True,
            "status": result.status,
            "files_checked": result.files_checked,
            "findings": [asdict(item) for item in result.findings],
            "diagnostics": list(result.diagnostics),
            "live": False,
        }

    def ops_record(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.core.ops import OpsValidationError, append_record
        record = args.get("record")
        if not isinstance(record, dict):
            return {"ok": False, "error": "record must be an object"}
        try:
            saved = append_record(ops_dir, record)
        except OpsValidationError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "record": saved, "path": str(ops_dir)}

    def ops_resume(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.core.ops import resume_summary
        try:
            summary = resume_summary(
                ops_dir,
                limit=int(args.get("limit", 20)),
                environment=(str(args["environment"])
                             if args.get("environment") is not None else None),
            )
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **summary}

    def live_import(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.core.live import SnapshotError, import_snapshot
        try:
            snapshot = import_snapshot(
                str(args.get("snapshot", "")),
                provenance=str(args.get("provenance", "")),
            )
        except SnapshotError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "snapshot": _snapshot_dict(snapshot)}

    def live_diff(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.core.live import (
            SnapshotError, diff_snapshots, import_snapshot,
        )
        try:
            before = import_snapshot(
                str(args.get("before", "")),
                provenance=str(args.get("provenance", "before")),
            )
            after = import_snapshot(
                str(args.get("after", "")),
                provenance=str(args.get("provenance", "after")),
            )
            differences = diff_snapshots(before, after)
        except SnapshotError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True,
                "differences": [asdict(item) for item in differences],
                "live": False}

    def live_save(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.core.live import SnapshotError, import_snapshot, write_snapshot
        try:
            snapshot = import_snapshot(
                str(args.get("snapshot", "")),
                provenance=str(args.get("provenance", "")),
            )
            path = write_snapshot(live_dir, snapshot)
        except (SnapshotError, OSError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(path), "live": False}

    def aws_cli_check(args: dict[str, Any]) -> dict[str, Any]:
        from dataclasses import asdict
        from ozagia.aws_cli import validate_aws_command
        argv = args.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            return {"ok": False, "error": "argv must be an array of strings"}
        try:
            result = validate_aws_command(argv)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **asdict(result)}

    def relay_plan(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.relay_cli import RelaySpecFileError, build_relay, parse_target
        document = args.get("spec")
        if not isinstance(document, dict):
            return {"ok": False, "error": "spec must be an object"}
        relay = None
        try:
            relay = build_relay(document)
            if args.get("target") is not None:
                target = parse_target(str(args["target"]))
                timeout_s = min(30.0, relay.policy.max_timeout_s) \
                    if relay.policy.max_timeout_s > 0 else 30.0
                lease = relay.open_tunnel(
                    target, local_port=int(args.get("local_port", 0)),
                    timeout_s=timeout_s, dry_run=True)
                return {
                    "ok": True, "dry_run": True,
                    "local_host": lease.local_host,
                    "local_port": lease.local_port,
                    "target_host": lease.target_host,
                    "target_port": lease.target_port,
                    "planned_argv": list(lease.planned_argv),
                }
            argv = args.get("argv")
            if not isinstance(argv, list) or not all(
                    isinstance(item, str) for item in argv):
                return {"ok": False, "error": "argv must be an array of strings"}
            planned = relay.plan_argv(argv)
        except (RelaySpecFileError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            if relay is not None:
                try:
                    relay.close()
                except Exception:
                    pass
        return {"ok": True, "dry_run": True, "argv": list(planned)}

    def runtime_status(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.config import ConfigError, load_project_config
        from ozagia.core.runtime import RuntimeErrorBase
        from ozagia.core.runtime import runtime_status as observe
        try:
            config = load_project_config(root)
            payload = observe(config, name=args.get("name"))
        except (ConfigError, OSError, RuntimeErrorBase) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **payload}

    def runtime_log(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.config import ConfigError, load_project_config
        from ozagia.core.runtime import RuntimeErrorBase
        from ozagia.core.runtime import runtime_logs as read_logs
        try:
            limit = int(args.get("max_bytes", 2_048))
            payload = read_logs(
                load_project_config(root), str(args.get("name", "")),
                max_bytes=limit)
        except (ConfigError, OSError, RuntimeErrorBase, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **payload}

    def preflight_plan(args: dict[str, Any]) -> dict[str, Any]:
        from ozagia.core.preflight import build_preflight
        roots = args.get("roots")
        dependencies = args.get("dependencies") or {}
        emulator = args.get("emulator_bin")
        if not isinstance(roots, list) or not isinstance(dependencies, dict):
            return {"ok": False, "error": "roots and dependencies must be structured"}
        if not isinstance(emulator, str) or not emulator:
            return {"ok": False, "error": "emulator_bin is required"}
        try:
            plan = build_preflight(roots, dependencies, emulator_bin=emulator)
        except (ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "execute": False,
                "roots": list(plan.roots),
                "environment": dict(plan.environment),
                "provider_versions": [list(item) for item in plan.provider_versions],
                "provider_version_note": plan.provider_version_note,
                "commands": [list(item.argv) for item in plan.commands]}

    specs = [
        ({"name": "dev_environments",
          "description": "Declarative environment profiles; never a live account or cluster read.",
          "inputSchema": _schema({})}, environments),
        ({"name": "dev_check_iac",
          "description": "Run bounded, source-only Terraform/OpenTofu checks; no plan or providers.",
          "inputSchema": _schema({"root": {"type": "string"}})}, check_iac),
        ({"name": "dev_ops_record",
          "description": "Append a bounded, redacted ops receipt; no commands are replayed.",
          "inputSchema": _schema({"record": {"type": "object"}}, ["record"])},
         ops_record),
        ({"name": "dev_ops_resume",
          "description": "Return a bounded, environment-scoped ops handoff summary.",
          "inputSchema": _schema({
              "limit": {"type": "integer", "default": 20},
              "environment": {"type": "string"}})}, ops_resume),
        ({"name": "dev_live_import",
          "description": "Validate a supplied live-state snapshot artifact; no live read.",
          "inputSchema": _schema({
              "snapshot": {"type": "string"},
              "provenance": {"type": "string"}},
              ["snapshot", "provenance"])}, live_import),
        ({"name": "dev_live_diff",
          "description": "Diff two supplied live-state snapshot artifacts; no live read.",
          "inputSchema": _schema({
              "before": {"type": "string"}, "after": {"type": "string"},
              "provenance": {"type": "string", "default": "artifact"}},
              ["before", "after"])}, live_diff),
        ({"name": "dev_live_save",
          "description": "Validate and save a supplied snapshot under the state dir; no live read.",
          "inputSchema": _schema({
              "snapshot": {"type": "string"},
              "provenance": {"type": "string"}},
              ["snapshot", "provenance"])}, live_save),
        ({"name": "dev_aws_cli_check",
          "description": "Check an AWS CLI service/operation against local models; no network.",
          "inputSchema": _schema({
              "argv": {"type": "array", "items": {"type": "string"}}},
              ["argv"])}, aws_cli_check),
        ({"name": "dev_relay_plan",
          "description": "Build a dry-run relay command/tunnel plan; never executes or binds a live network.",
          "inputSchema": _schema({
              "spec": {"type": "object"},
              "argv": {"type": "array", "items": {"type": "string"}},
              "target": {"type": "string"},
              "local_port": {"type": "integer", "default": 0}},
              ["spec"])}, relay_plan),
        ({"name": "dev_preflight_plan",
          "description": "Build a dry-run, scrubbed emulator pre-flight plan; never executes it.",
          "inputSchema": _schema({
              "roots": {"type": "array", "items": {"type": "string"}},
              "dependencies": {"type": "object"},
              "emulator_bin": {"type": "string"}},
              ["roots", "emulator_bin"])}, preflight_plan),
        ({"name": "dev_runtime_status",
          "description": ("Read configured services/tunnels and their observed local "
                          "state. Never starts a process or opens a tunnel; a "
                          "configured tunnel is not reported as connected."),
          "inputSchema": _schema({"name": {"type": "string"}})}, runtime_status),
        ({"name": "dev_runtime_logs",
          "description": ("Read one bounded local service/process log on request. "
                          "Logs are never included in automatic context."),
          "inputSchema": _schema({
              "name": {"type": "string"},
              "max_bytes": {"type": "integer", "default": 2_048}},
              ["name"])}, runtime_log),
    ]
    return [(spec, handler) for spec, handler in specs]


def _snapshot_dict(snapshot: Any) -> dict[str, Any]:
    return {
        "schema_version": snapshot.schema_version,
        "observed_at": snapshot.observed_at,
        "provenance": snapshot.provenance,
        "environment": snapshot.environment,
        "service": snapshot.service,
        "region": snapshot.region,
        "deployed_images": [asdict(item) for item in snapshot.deployed_images],
        "desired_replicas": snapshot.desired_replicas,
        "ready_replicas": snapshot.ready_replicas,
        "target_health": asdict(snapshot.target_health),
        "last_pipeline": (asdict(snapshot.last_pipeline)
                          if snapshot.last_pipeline is not None else None),
    }
