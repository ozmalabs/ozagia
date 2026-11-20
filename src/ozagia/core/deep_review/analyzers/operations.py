"""Bounded offline inventory of operational configuration and runbooks.

This deliberately makes very few claims: it inventories common deployment
files, inspects selected Kubernetes JSON/YAML and Compose YAML structures when
the optional YAML parser is present, applies narrow lexical rules to other
supported configuration, checks GitHub Actions workflow declarations without
resolving effective permissions or expression-expanded steps, flags a Dockerfile without a
HEALTHCHECK instruction, checks local runbook links and topics, and records
logging, metrics, tracing, and observability declarations. Runtime monitoring
is never inferred from absence: it stays explicitly unknown.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

from ._bounded_read import read_text as _read_text

MAX_FILES = 3000
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 20_000_000
MAX_RUNBOOK_LINKS = 200
SKIP_DIRS = {".git", ".ozagia", ".venv", "venv", "node_modules",
             "__pycache__", ".tox", "target", "dist"}
_CONFIG_NAMES = {"compose.yml", "compose.yaml", "docker-compose.yml",
                 "docker-compose.yaml", "Containerfile", "Dockerfile",
                 "Procfile", "Caddyfile", "nginx.conf"}
_K8S_FILENAMES = {"deployment.yaml", "deployment.yml", "daemonset.yaml",
                  "daemonset.yml", "statefulset.yaml", "statefulset.yml",
                  "pod.yaml", "pod.yml", "service.yaml", "service.yml",
                  "ingress.yaml", "ingress.yml", "job.yaml", "job.yml",
                  "cronjob.yaml", "cronjob.yml", "replicationcontroller.yaml",
                  "replicationcontroller.yml", "networkpolicy.yaml",
                  "networkpolicy.yml"}
_K8S_JSON_FILENAMES = {name.rsplit(".", 1)[0] + ".json"
                       for name in _K8S_FILENAMES} | {"cronjob.json"}
_K8S_KIND = re.compile(
    r"(?im)^\s*kind\s*:\s*(?:Pod|Deployment|DaemonSet|StatefulSet|Job|"
    r"CronJob|ReplicationController|Service|Ingress|NetworkPolicy)\s*$")
MAX_CONFIG_FINDINGS = 500
MAX_JSON_NODES = 50_000
MAX_JSON_DEPTH = 64
MAX_YAML_DOCUMENTS = 32
MAX_HCL_FILES = 64
MAX_HCL_BYTES = 4_000_000
MAX_HCL_RESULT_BYTES = 512_000
HCL_PARSE_TIMEOUT_S = 5.0
_GITHUB_ACTION_REF = re.compile(
    r"^\s*(?:-\s*)?uses\s*:\s*['\"]?([^\s'\"#]+)", re.I)
_GITHUB_COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")
_GITHUB_PR_TARGET_INLINE = re.compile(
    r"^\s*on\s*:\s*(?:(?:['\"]?pull_request_target['\"]?)\s*(?:#.*)?$|"
    r"[\[{].*\bpull_request_target\b)", re.I)
_RUNBOOK = re.compile(r"(?:runbook|operations|on[-_ ]call|disaster[-_ ]recovery|rollback)", re.I)
_LINK = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\s*\)")
_RST_LINK = re.compile(r"`[^`<]*<([^>]+)>`__?")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DYNAMIC_JSON = re.compile(r"\$\{|\{\{|\{%|\$\(")
_TOPICS = {
    "alerting": re.compile(r"\b(alert|alerts|alerting|alarm|monitor|monitoring|sentry)\b", re.I),
    "escalation": re.compile(r"\b(escalat\w*|on[- ]call|pager|incident|notify)\b", re.I),
    "logging": re.compile(r"\b(log|logs|logging|journal|stdout|stderr)\b", re.I),
    "metrics": re.compile(r"\b(metric|metrics|prometheus|grafana|dashboard)\b", re.I),
    "recovery": re.compile(r"\b(recover\w*|restore|restart|failover)\b", re.I),
    "rollback": re.compile(r"\b(rollback|roll back|revert|undo)\b", re.I),
    "tracing": re.compile(r"\b(trace|traces|tracing|jaeger|zipkin|otel|opentelemetry)\b", re.I),
    "verification": re.compile(r"\b(verify|verification|validate|smoke[- ]test|health|readiness)\b", re.I),
}
_SIGNALS = {
    "logging": re.compile(
        r"\b(logs?|logging|log[-_]level|access_log|standardoutput|standard_output|"
        r"syslog|journald|fluentd|fluent-bit|loki)\b", re.I),
    "metrics": re.compile(
        r"\b(metrics?|prometheus|statsd|datadog|grafana|cloudwatch|opentelemetry|otel)\b",
        re.I),
    "tracing": re.compile(
        r"\b(traces?|tracing|jaeger|zipkin|tempo|opentelemetry|otel)\b", re.I),
    "observability": re.compile(
        r"\b(observability|monitoring|monitor|alerting|alerts?|sentry|healthcheck|"
        r"health[-_ ]?check|readiness|liveness|uptime)\b", re.I),
}
_DISABLED_SIGNALS = {
    "logging": re.compile(
        r"(?im)^\s*(?:driver\s*:\s*['\"]?none|StandardOutput\s*=\s*null|"
        r"StandardError\s*=\s*null|OTEL_LOGS_EXPORTER\s*[:=]\s*['\"]?none)\b"),
    "metrics": re.compile(
        r"(?im)^\s*OTEL_METRICS_EXPORTER\s*[:=]\s*['\"]?none\b"),
    "tracing": re.compile(
        r"(?im)^\s*OTEL_TRACES_EXPORTER\s*[:=]\s*['\"]?none\b"),
    "observability": re.compile(
        r"(?im)^\s*OTEL_SDK_DISABLED\s*[:=]\s*['\"]?true\b"),
}


def _is_local_link(target: str) -> bool:
    value = target.strip().strip("<>")
    if not value or value.startswith("#"):
        return False
    if _SCHEME.match(value) or value.startswith("//"):
        return False
    return not any(token in value for token in ("{", "}", "${", "*"))


def _link_path(root: Path, doc: Path, target: str) -> Path | None:
    value = unquote(target.split("#", 1)[0].split("?", 1)[0].strip())
    if not _is_local_link(value):
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return root / candidate.as_posix().lstrip("/")
    return doc.parent / candidate


def _runbook_links(root: Path, rel: str, text: str) -> list[dict[str, object]]:
    """Return checked local links with their resolution state."""
    doc = root / rel
    rows = []
    for line_no, line in enumerate(text.splitlines(), 1):
        targets = [match.group(1) for match in _LINK.finditer(line)]
        targets += [match.group(1) for match in _RST_LINK.finditer(line)]
        for target in targets:
            path = _link_path(root, doc, target)
            if path is None:
                continue
            rows.append({"line": line_no, "target": target,
                         "exists": path.is_file() or path.is_dir()})
            if len(rows) >= MAX_RUNBOOK_LINKS:
                return rows
    return rows


def _github_workflow_findings(rel: str, text: str, *, limit: int
                              ) -> list[dict[str, object]]:
    """Return conservative GitHub Actions workflow configuration candidates.

    These are lexical declarations, not effective-permission or workflow
    semantics checks. In particular, a missing permissions key depends on the
    repository/org default, and an action ref's meaning depends on GitHub.
    """
    findings = []

    def add(finding):
        if len(findings) < limit:
            findings.append(finding)
    lines = text.splitlines()
    content_lines = [(number, line) for number, line in enumerate(lines, 1)
                     if not line.lstrip().startswith("#")]
    has_permissions = any(re.match(r"^\s*permissions\s*:", line, re.I)
                          for _, line in content_lines)
    if not has_permissions:
        add({
            "section": "operations", "type": "workflow_permissions_not_declared",
            "rule_id": "ops.github_actions.permissions_not_declared",
            "rule": "ops.github_actions.permissions_not_declared",
            "severity": "note", "confidence": "low", "evidence_kind": "observed",
            "keys": [f"file:{rel}"], "file": rel,
            "summary": (f"{rel} does not declare workflow permissions; the effective "
                        "token permissions depend on repository and organization defaults."),
            "recommendation": ("Declare the minimum required GITHUB_TOKEN permissions "
                               "at workflow or job scope, then confirm the effective policy."),
            "locations": [{"path": rel}], "truth_assessed": False,
        })
    for number, line in content_lines:
        trigger = (re.match(r"^\s*['\"]?pull_request_target['\"]?\s*:", line)
                   or _GITHUB_PR_TARGET_INLINE.match(line))
        if trigger:
            add({
                "section": "operations", "type": "workflow_privileged_pull_request_trigger",
                "rule_id": "ops.github_actions.pull_request_target",
                "rule": "ops.github_actions.pull_request_target",
                "severity": "note", "confidence": "low", "evidence_kind": "observed",
                "keys": [f"file:{rel}", "trigger:pull_request_target"],
                "file": rel, "line": number,
                "summary": (f"{rel} declares pull_request_target, which runs in the base-repository "
                            "context; event token and secret access depend on workflow policy."),
                "recommendation": ("Review checkout refs, secrets, write permissions, and every "
                                   "step that processes pull-request-controlled data."),
                "locations": [{"path": rel, "line": number}], "truth_assessed": False,
            })
        action = _GITHUB_ACTION_REF.match(line)
        if action:
            reference = action.group(1)
            if ("@" in reference and not reference.startswith(("./", "../", "docker://"))
                    and not _GITHUB_COMMIT.fullmatch(reference.rsplit("@", 1)[1])):
                add({
                    "section": "operations", "type": "workflow_action_not_commit_pinned",
                    "rule_id": "ops.github_actions.action_not_commit_pinned",
                    "rule": "ops.github_actions.action_not_commit_pinned",
                    "severity": "note", "confidence": "medium", "evidence_kind": "observed",
                    "keys": [f"file:{rel}", f"line:{number}",
                             "action:mutable-reference"],
                    "file": rel, "line": number,
                    "summary": (f"{rel} references an action by a mutable or non-commit ref."),
                    "recommendation": ("Pin third-party actions to a reviewed full commit SHA "
                                       "and add a version comment for maintainability."),
                    "locations": [{"path": rel, "line": number}], "truth_assessed": False,
                })
    return findings


def _runbook_topics(text: str) -> list[str]:
    return sorted(name for name, pattern in _TOPICS.items()
                  if pattern.search(text))


def _dockerfile_healthcheck(text: str) -> tuple[str, int | None]:
    """Identify the last stage's explicit healthcheck declaration.

    A base image or an explicitly selected build target may supply different
    behavior, so absence only describes instructions in the last stage.
    """
    healthcheck = None
    stages = {}
    stage_name = None
    for line_no, line in enumerate(text.splitlines(), 1):
        if match := re.match(
                r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?",
                line, re.I):
            base, stage_name = match.groups()
            healthcheck = stages.get(base.lower())
            if healthcheck and healthcheck[0] in {"disabled", "inherited_disabled"}:
                healthcheck = ("inherited_disabled", healthcheck[1])
        elif match := re.match(r"^\s*HEALTHCHECK\s+(.*?)\s*$", line, re.I):
            value = match.group(1)
            healthcheck = ("disabled" if re.match(r"^NONE(?:\s*(?:#.*)?)?$", value, re.I)
                           else "declared", line_no)
        if stage_name is not None:
            stages[stage_name.lower()] = healthcheck
    return healthcheck if healthcheck is not None else ("absent", None)


def _observability(configs: dict[str, str],
                   runbooks: dict[str, str]) -> dict[str, object]:
    declarations = {}
    for signal, pattern in _SIGNALS.items():
        config_files = sorted(rel for rel, text in configs.items()
                              if pattern.search(text))
        disabled_files = sorted(
            rel for rel, text in configs.items()
            if _DISABLED_SIGNALS[signal].search(text))
        runbook_files = sorted(rel for rel, text in runbooks.items()
                               if pattern.search(text))
        state = ("declared_and_disabled" if config_files and disabled_files else
                 "explicitly_disabled" if disabled_files else
                 "declared" if config_files else
                 "documented_only" if runbook_files else "unknown")
        declarations[signal] = {
            "state": state,
            "configuration_files": config_files,
            "disabled_configuration_files": disabled_files,
            "runbook_files": runbook_files,
        }
    return {
        "runtime_monitoring": "unknown",
        "declarations": declarations,
        "not_checked": [
            "runtime logging, metrics, tracing, and alert delivery were not verified",
            "a repository declaration is not evidence that telemetry is collected or retained",
        ],
    }


def _json_tree_within_bounds(value: object) -> bool:
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return False
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return True


def _json_pointer(path: tuple[str | int, ...]) -> str:
    return "/" + "/".join(str(item).replace("~", "~0").replace("/", "~1")
                           for item in path)


def _kubernetes_json_findings(rel: str, text: str
                               ) -> tuple[list[dict], str | None]:
    """Inspect one plain JSON Kubernetes object for narrow risky settings."""
    def object_without_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        document = json.loads(text, object_pairs_hook=object_without_duplicates,
                              parse_constant=lambda _value: (_ for _ in ()).throw(
                                  ValueError("non-JSON constant")))
    except (json.JSONDecodeError, ValueError, RecursionError):
        return [], f"{rel}: Kubernetes JSON could not be parsed unambiguously"
    if not _json_tree_within_bounds(document):
        return [], f"{rel}: Kubernetes JSON exceeded structural analysis bounds"

    return _kubernetes_object_findings(rel, document, pointer_field="json_pointer")


def _kubernetes_object_findings(rel: str, document: object, *,
                                pointer_field: str
                                ) -> tuple[list[dict], str | None]:
    """Inspect a bounded Kubernetes mapping independent of its source syntax."""

    def has_dynamic(value):
        if isinstance(value, str):
            return bool(_DYNAMIC_JSON.search(value))
        if isinstance(value, dict):
            return any(has_dynamic(key) or has_dynamic(item)
                       for key, item in value.items())
        if isinstance(value, list):
            return any(has_dynamic(item) for item in value)
        return False

    if has_dynamic(document):
        return [], f"{rel}: templated/dynamic Kubernetes JSON was not semantically inspected"
    if (not isinstance(document, dict)
            or not isinstance(document.get("apiVersion"), str)
            or not document.get("apiVersion")
            or not isinstance(document.get("kind"), str)
            or document.get("kind") not in {
                "Pod", "Deployment", "DaemonSet", "StatefulSet", "Job",
                "CronJob", "ReplicationController", "Service", "Ingress",
                "NetworkPolicy"}):
        return [], f"{rel}: JSON did not match a supported Kubernetes manifest shape"

    kind = document["kind"]
    if kind == "Pod":
        pod_path = ("spec",)
    elif kind == "CronJob":
        pod_path = ("spec", "jobTemplate", "spec", "template", "spec")
    elif kind in {"Deployment", "DaemonSet", "StatefulSet", "Job",
                  "ReplicationController"}:
        pod_path = ("spec", "template", "spec")
    else:
        pod_path = ()
    pod_spec: object = document
    for component in pod_path:
        pod_spec = pod_spec.get(component) if isinstance(pod_spec, dict) else None
    if pod_path and not isinstance(pod_spec, dict):
        return [], f"{rel}: Kubernetes pod spec shape was not available for structural inspection"
    findings: list[dict] = []

    def add(rule_id: str, path: tuple[str | int, ...], summary: str,
            recommendation: str) -> None:
        pointer = _json_pointer(path)
        findings.append({
            "section": "operations", "type": rule_id.rsplit(".", 1)[-1],
            "rule_id": rule_id, "rule": rule_id, "severity": "minor",
            "confidence": "medium", "evidence_kind": "declared",
            "keys": [f"file:{rel}", f"json-pointer:{pointer}"],
            "file": rel, pointer_field: pointer,
            "summary": summary, "recommendation": recommendation,
            "truth_assessed": False,
        })

    if isinstance(pod_spec, dict):
        if pod_spec.get("hostNetwork") is True:
            add("iac.kubernetes.host_network", pod_path + ("hostNetwork",),
                f"{rel} sets Kubernetes hostNetwork to true in {kind} pod spec.",
                "Review port collisions and node-network exposure; use a pod network unless host networking is required.")
        for field, rule in (("hostPID", "host_pid"), ("hostIPC", "host_ipc")):
            if pod_spec.get(field) is True:
                add(f"iac.kubernetes.{rule}", pod_path + (field,),
                    f"{rel} sets {field} to true in the {kind} pod spec, sharing a host namespace.",
                    "Use an isolated pod namespace unless sharing the host namespace is required.")
        pod_security = pod_spec.get("securityContext")
        if (isinstance(pod_security, dict)
                and pod_security.get("runAsNonRoot") is False):
            add("iac.kubernetes.run_as_root",
                pod_path + ("securityContext", "runAsNonRoot"),
                f"{rel} explicitly permits root execution in the {kind} pod spec.",
                "Set runAsNonRoot to true when the workload supports a non-root user.")
        if (isinstance(pod_security, dict)
                and type(pod_security.get("runAsUser")) is int
                and pod_security["runAsUser"] == 0):
            add("iac.kubernetes.root_user", pod_path + ("securityContext", "runAsUser"),
                f"{rel} sets the {kind} pod's default runAsUser to UID 0; individual containers may override it.",
                "Use a non-root UID when the workload supports it and review container overrides.")
        volumes = pod_spec.get("volumes")
        if isinstance(volumes, list):
            for index, volume in enumerate(volumes):
                if isinstance(volume, dict) and isinstance(volume.get("hostPath"), dict):
                    add("iac.kubernetes.host_path_volume",
                        pod_path + ("volumes", index, "hostPath"),
                        f"{rel} declares a Kubernetes hostPath volume in {kind} pod spec.",
                        "Review the mounted host path and access mode; prefer a managed volume when possible.")
        for container_field in ("containers", "initContainers", "ephemeralContainers"):
            containers = pod_spec.get(container_field)
            if not isinstance(containers, list):
                continue
            for index, container in enumerate(containers):
                if not isinstance(container, dict):
                    continue
                ports = container.get("ports")
                if isinstance(ports, list):
                    for port_index, port in enumerate(ports):
                        if not isinstance(port, dict):
                            continue
                        host_port = port.get("hostPort")
                        if type(host_port) is int and 1 <= host_port <= 65535:
                            add("iac.kubernetes.host_port",
                                pod_path + (container_field, index, "ports", port_index, "hostPort"),
                                f"{rel} declares a hostPort in a {kind} container; actual exposure depends on node networking and policy.",
                                "Review node port collisions and intended exposure; omit hostPort unless required.")
                security_context = container.get("securityContext")
                if (isinstance(security_context, dict)
                        and type(security_context.get("runAsUser")) is int
                        and security_context["runAsUser"] == 0):
                    add("iac.kubernetes.root_user",
                        pod_path + (container_field, index, "securityContext", "runAsUser"),
                        f"{rel} sets a {kind} container's runAsUser to UID 0.",
                        "Use a non-root UID when the workload supports it.")
                if (isinstance(security_context, dict)
                        and security_context.get("privileged") is True):
                    add("iac.container.privileged_mode",
                        pod_path + (container_field, index, "securityContext", "privileged"),
                        f"{rel} enables privileged mode for a Kubernetes {kind} container.",
                        "Review whether the container needs host-level privileges and remove the setting if not.")
                if (isinstance(security_context, dict)
                        and security_context.get("allowPrivilegeEscalation") is True):
                    add("iac.kubernetes.privilege_escalation",
                        pod_path + (container_field, index, "securityContext",
                                    "allowPrivilegeEscalation"),
                        f"{rel} permits privilege escalation for a {kind} container.",
                        "Set allowPrivilegeEscalation to false unless the workload requires it.")
                capabilities = security_context.get("capabilities") if isinstance(
                    security_context, dict) else None
                added = capabilities.get("add") if isinstance(capabilities, dict) else None
                if isinstance(added, list) and "ALL" in added:
                    add("iac.kubernetes.add_all_capabilities",
                        pod_path + (container_field, index, "securityContext",
                                    "capabilities", "add"),
                        f"{rel} adds all Linux capabilities to a {kind} container.",
                        "Grant only the Linux capabilities the workload needs.")
    return findings, None


class _YamlUnavailable(Exception):
    pass


def _yaml_documents(text: str) -> list[object]:
    """Load safe YAML documents lazily and reject duplicate mapping keys."""
    try:
        import yaml
    except ImportError as exc:
        raise _YamlUnavailable from exc

    class UniqueSafeLoader(yaml.SafeLoader):
        def __init__(self, stream):
            super().__init__(stream)
            self._oz_node_count = 0
            self._oz_compose_depth = 0

        def compose_node(self, parent, index):
            if self.check_event(yaml.events.AliasEvent):
                raise yaml.constructor.ConstructorError(
                    None, None, "YAML aliases are not supported for bounded analysis",
                    self.peek_event().start_mark)
            self._oz_node_count += 1
            self._oz_compose_depth += 1
            if (self._oz_node_count > MAX_JSON_NODES
                    or self._oz_compose_depth > MAX_JSON_DEPTH):
                self._oz_compose_depth -= 1
                raise yaml.constructor.ConstructorError(
                    None, None, "YAML structure exceeded analysis bounds",
                    self.peek_event().start_mark)
            try:
                return super().compose_node(parent, index)
            finally:
                self._oz_compose_depth -= 1

        def construct_mapping(self, node, deep=False):
            self.flatten_mapping(node)
            mapping = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in mapping
                except TypeError as exc:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping", node.start_mark,
                        "unhashable mapping key", key_node.start_mark) from exc
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping", node.start_mark,
                        f"duplicate key {key!r}", key_node.start_mark)
                mapping[key] = self.construct_object(value_node, deep=deep)
            return mapping

    documents = []
    for document in yaml.load_all(text, Loader=UniqueSafeLoader):
        documents.append(document)
        if len(documents) > MAX_YAML_DOCUMENTS:
            raise ValueError("YAML document count exceeded structural analysis bounds")
    return documents


def _yaml_tree_within_bounds(value: object) -> bool:
    stack = [(value, 0)]
    seen: set[int] = set()
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return False
        if isinstance(current, (dict, list)):
            identity = id(current)
            if identity in seen:
                # YAML aliases create repeated references (or cycles); do not
                # interpret cyclic structures as ordinary configuration.
                return False
            seen.add(identity)
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for pair in current.items()
                         for item in pair)
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif (isinstance(current, float) and not math.isfinite(current)):
            return False
        elif current is not None and not isinstance(current, (str, int, float, bool)):
            return False
    return True


def _yaml_contains_dynamic(value: object) -> bool:
    if isinstance(value, str):
        return bool(_DYNAMIC_JSON.search(value))
    if isinstance(value, dict):
        return any(_yaml_contains_dynamic(key) or _yaml_contains_dynamic(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_yaml_contains_dynamic(item) for item in value)
    return False


def _kubernetes_yaml_findings(rel: str, text: str
                              ) -> tuple[list[dict], str | None, bool]:
    try:
        documents = _yaml_documents(text)
    except _YamlUnavailable:
        return [], f"{rel}: PyYAML is unavailable; Kubernetes YAML structure was not checked", False
    except Exception as exc:  # malformed, unsafe, duplicate or over-bound YAML
        return [], f"{rel}: Kubernetes YAML could not be inspected ({type(exc).__name__})", False
    if not documents:
        return [], f"{rel}: Kubernetes YAML contained no documents", True
    findings: list[dict] = []
    for index, document in enumerate(documents):
        if not _yaml_tree_within_bounds(document):
            return [], f"{rel}: Kubernetes YAML exceeded structural analysis bounds", True
        rows, reason = _kubernetes_object_findings(
            rel, document, pointer_field="yaml_pointer")
        if reason:
            return [], reason.replace("Kubernetes JSON", "Kubernetes YAML"), True
        for row in rows:
            if len(documents) > 1:
                row["yaml_document"] = index
            findings.append(row)
            if len(findings) >= MAX_CONFIG_FINDINGS:
                return findings, f"{rel}: structural finding limit reached", True
    return findings, None, True


def _compose_yaml_findings(rel: str, text: str
                           ) -> tuple[list[dict], str | None, bool]:
    try:
        documents = _yaml_documents(text)
    except _YamlUnavailable:
        return [], f"{rel}: PyYAML is unavailable; Compose YAML structure was not checked", False
    except Exception as exc:
        return [], f"{rel}: Compose YAML could not be inspected ({type(exc).__name__})", False
    if not documents or len(documents) != 1 or not _yaml_tree_within_bounds(documents[0]):
        return [], f"{rel}: Compose YAML exceeded supported document or structure bounds", True
    document = documents[0]
    if (not isinstance(document, dict)
            or not isinstance(document.get("services"), dict)):
        return [], f"{rel}: Compose YAML has no unambiguous services mapping", True
    if _yaml_contains_dynamic(document):
        return [], f"{rel}: templated/dynamic Compose YAML was not semantically inspected", True
    findings: list[dict] = []
    for service, config in sorted(document["services"].items(), key=lambda row: str(row[0])):
        if not isinstance(service, str) or not isinstance(config, dict):
            return [], f"{rel}: Compose service entries were not structurally inspectable", True
        if "extends" in config:
            # Do not resolve another file or merge an effective service view.
            return [], f"{rel}: Compose extends references were not resolved", True
        if config.get("privileged") is True:
            if len(findings) >= MAX_CONFIG_FINDINGS:
                return findings, f"{rel}: structural finding limit reached", True
            findings.append({
                "section": "operations", "type": "privileged_mode",
                "rule_id": "iac.container.privileged_mode",
                "rule": "iac.container.privileged_mode", "severity": "minor",
                "confidence": "medium", "evidence_kind": "declared",
                "keys": [f"file:{rel}", f"service:{service}"],
                "file": rel, "yaml_pointer": f"/services/{service}/privileged",
                "summary": f"{rel} enables privileged mode for Compose service {service}.",
                "recommendation": "Review whether the service needs host-level privileges and remove the setting if not.",
                "truth_assessed": False,
            })
        ports = config.get("ports")
        if isinstance(ports, list):
            for index, port in enumerate(ports):
                host_ip = None
                if isinstance(port, str):
                    parts = port.split(":")
                    if len(parts) >= 3:
                        host_ip = parts[0].strip("[]")
                elif isinstance(port, dict):
                    host_ip = port.get("host_ip")
                if host_ip != "0.0.0.0":
                    continue
                if len(findings) >= MAX_CONFIG_FINDINGS:
                    return findings, f"{rel}: structural finding limit reached", True
                findings.append({
                    "section": "operations", "type": "bind_all_interfaces",
                    "rule_id": "iac.compose.bind_all_interfaces",
                    "rule": "iac.compose.bind_all_interfaces", "severity": "minor",
                    "confidence": "medium", "evidence_kind": "declared",
                    "keys": [f"file:{rel}", f"service:{service}"],
                    "file": rel, "yaml_pointer": f"/services/{service}/ports/{index}",
                    "summary": f"{rel} binds a published port for Compose service {service} to all IPv4 interfaces.",
                    "recommendation": "Confirm public exposure is intended; bind to a narrower host address when appropriate.",
                    "truth_assessed": False,
                })
    return findings[:MAX_CONFIG_FINDINGS], None, True


def _hcl_tree_within_bounds(value: object) -> bool:
    stack = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return False
        if isinstance(current, dict):
            stack.extend((key, depth + 1) for key in current)
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, float) and not math.isfinite(current):
            return False
        elif current is not None and not isinstance(current, (str, int, float, bool)):
            return False
    return True


def _hcl_literal(value: object) -> tuple[object, bool]:
    """Decode only literal parser output; expressions remain unknown."""
    if isinstance(value, bool) or isinstance(value, int) or value is None:
        return value, True
    if isinstance(value, float):
        return value, math.isfinite(value)
    if isinstance(value, list):
        decoded = [_hcl_literal(item) for item in value]
        return [item for item, _known in decoded], all(
            known for _item, known in decoded)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None, False
        if not isinstance(decoded, str) or _DYNAMIC_JSON.search(decoded):
            return None, False
        return decoded, True
    return None, False


def _hcl_label(value: object) -> str | None:
    decoded, known = _hcl_literal(value)
    return decoded if known and isinstance(decoded, str) else None


class _HclUnavailable(Exception):
    pass


def _load_hcl(text: str) -> object:
    try:
        import hcl2
    except ImportError as exc:
        raise _HclUnavailable from exc
    return hcl2.loads(text)


def _terraform_findings_from_tree(rel: str, tree: object, source_text: str
                                  ) -> tuple[list[dict], list[str]]:
    if not isinstance(tree, dict):
        return [], [f"{rel}: HCL parse result was not a mapping"]
    if not _hcl_tree_within_bounds(tree):
        return [], [f"{rel}: HCL parse result exceeded structural analysis bounds"]
    findings: list[dict] = []
    omissions: list[str] = []

    def resource_line(address: str) -> int | None:
        base = address.split(".ingress[", 1)[0]
        parts = base.split(".", 1)
        if len(parts) != 2:
            return None
        pattern = re.compile(
            r"(?m)^[ \t]*resource[ \t]+['\"]" + re.escape(parts[0])
            + r"['\"][ \t]+['\"]" + re.escape(parts[1])
            + r"['\"][ \t]*\{")
        match = pattern.search(source_text)
        if not match:
            return None
        if ".ingress[" in address:
            block_index = int(address.split(".ingress[", 1)[1].split("]", 1)[0])
            body_start = match.end()
            next_resource = re.search(r"(?m)^[ \t]*resource[ \t]+['\"]",
                                      source_text[body_start:])
            body_end = (body_start + next_resource.start()
                        if next_resource else len(source_text))
            ingress_blocks = list(re.finditer(
                r"(?m)^[ \t]*ingress[ \t]*\{",
                source_text[body_start:body_end]))
            if block_index < len(ingress_blocks):
                return (source_text.count("\n", 0,
                                          body_start + ingress_blocks[block_index].start())
                        + 1)
        body_start = match.end()
        next_resource = re.search(r"(?m)^[ \t]*resource[ \t]+['\"]",
                                  source_text[body_start:])
        body_end = (body_start + next_resource.start()
                    if next_resource else len(source_text))
        cidr = re.search(
            r"(?m)^[ \t]*(?:cidr_blocks|ipv6_cidr_blocks|cidr_ipv4|cidr_ipv6)[ \t]*=",
            source_text[body_start:body_end])
        position = body_start + cidr.start() if cidr else match.start()
        return source_text.count("\n", 0, position) + 1
    resources = tree.get("resource", [])
    if not isinstance(resources, list):
        return [], [f"{rel}: Terraform resource blocks were not structurally inspectable"]

    def add(resource_address: str, rule_id: str, title: str) -> None:
        if len(findings) >= MAX_CONFIG_FINDINGS:
            if not omissions:
                omissions.append(f"{rel}: Terraform structural finding limit reached")
            return
        finding = {
            "section": "operations", "type": rule_id.rsplit(".", 1)[-1],
            "rule_id": rule_id, "rule": rule_id, "severity": "minor",
            "confidence": "medium", "evidence_kind": "declared",
            "keys": [f"file:{rel}", f"terraform-resource:{resource_address}"],
            "file": rel, "terraform_resource": resource_address,
            "locations": [{"path": rel,
                           **({"line": resource_line(resource_address)}
                              if resource_line(resource_address) is not None else {})}],
            "summary": f"{rel} declares {title} in Terraform resource {resource_address}.",
            "recommendation": "Confirm the rule is intentional and restrict source CIDRs or ports where possible.",
            "truth_assessed": False,
        }
        line = resource_line(resource_address)
        if line is not None:
            finding["line"] = line
        findings.append(finding)

    def inspect_rule(body: dict, address: str, resource_type: str,
                     explicit_type: str | None) -> None:
        if "dynamic" in body:
            omissions.append(f"{rel}: dynamic Terraform blocks were not resolved in {address}")
        if explicit_type == "egress" or resource_type.endswith("_egress_rule"):
            return
        ingress = (explicit_type == "ingress"
                   or "ingress" in resource_type.lower())
        if explicit_type not in (None, "ingress", "egress"):
            omissions.append(f"{rel}: Terraform rule type expression was not resolved")
        if explicit_type is None and "security_group_rule" in resource_type:
            if "type" in body:
                omissions.append(f"{rel}: Terraform rule type expression was not resolved")
                return
            # The legacy aws_security_group_rule resource requires a type.
            return
        if not ingress:
            return
        cidrs: list[str] = []
        for attribute in ("cidr_blocks", "ipv6_cidr_blocks", "cidr_ipv4",
                          "cidr_ipv6"):
            if attribute not in body:
                continue
            values, known = _hcl_literal(body[attribute])
            if not known:
                omissions.append(
                    f"{rel}: Terraform {attribute} expression was not resolved in {address}")
                continue
            if isinstance(values, str):
                cidrs.append(values)
            elif isinstance(values, list) and all(
                    isinstance(item, str) for item in values):
                cidrs.extend(values)
            else:
                omissions.append(
                    f"{rel}: Terraform {attribute} had an unsupported literal shape in {address}")
        public = any(value in {"0.0.0.0/0", "::/0"} for value in cidrs)
        if not public:
            return
        admin = False
        for attribute in ("from_port", "to_port"):
            if attribute not in body:
                continue
            value, known = _hcl_literal(body[attribute])
            if not known:
                omissions.append(
                    f"{rel}: Terraform {attribute} expression was not resolved in {address}")
            elif isinstance(value, int) and not isinstance(value, bool) \
                    and value in {22, 3389}:
                admin = True
            elif not isinstance(value, int) or isinstance(value, bool):
                omissions.append(
                    f"{rel}: Terraform {attribute} had an unsupported literal type in {address}")
        rule_id = ("iac.terraform.public_admin_ingress" if admin else
                   "iac.terraform.public_ingress_cidr")
        title = "public SSH/RDP ingress" if admin else "unrestricted public ingress"
        add(address, rule_id, title)

    for resource in resources:
        if not isinstance(resource, dict):
            omissions.append(f"{rel}: malformed Terraform resource block")
            continue
        for raw_type, instances in resource.items():
            resource_type = _hcl_label(raw_type)
            if resource_type is None or not isinstance(instances, dict):
                omissions.append(f"{rel}: Terraform resource labels were not literal")
                continue
            for raw_name, body in instances.items():
                name = _hcl_label(raw_name)
                if name is None or not isinstance(body, dict):
                    omissions.append(f"{rel}: Terraform resource body was not structurally inspectable")
                    continue
                address = f"{resource_type}.{name}"
                if "dynamic" in body:
                    omissions.append(f"{rel}: dynamic Terraform blocks were not resolved in {address}")
                type_value, type_known = _hcl_literal(body.get("type")) \
                    if "type" in body else (None, True)
                explicit_type = type_value if type_known and isinstance(type_value, str) else None
                if "type" in body and not type_known:
                    omissions.append(f"{rel}: Terraform rule type expression was not resolved in {address}")
                if resource_type == "aws_security_group":
                    inline = body.get("ingress", [])
                    if inline and not isinstance(inline, list):
                        omissions.append(f"{rel}: Terraform ingress blocks were not structurally inspectable")
                        continue
                    for index, block in enumerate(inline):
                        if isinstance(block, dict):
                            inspect_rule(block, f"{address}.ingress[{index}]",
                                         resource_type, "ingress")
                    continue
                if ("security_group_rule" in resource_type
                        or "security_group_ingress_rule" in resource_type
                        or "ingress_rule" in resource_type):
                    inspect_rule(body, address, resource_type, explicit_type)
    return findings, sorted(set(omissions))[:100]


def _hcl_subprocess_main() -> None:
    """Isolated optional HCL parser worker; consumes/returns bounded JSON."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
        memory_limit = 768 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    except (ImportError, AttributeError, OSError, ValueError):
        pass
    try:
        request = sys.stdin.buffer.read(MAX_HCL_BYTES + 256_000)
        if len(request) > MAX_HCL_BYTES + 256_000:
            raise ValueError("HCL worker input exceeded size limit")
        payload_in = json.loads(request.decode("utf-8"))
        files = payload_in.get("files") if isinstance(payload_in, dict) else None
        if not isinstance(files, list) or len(files) > MAX_HCL_FILES:
            raise ValueError("HCL worker file list exceeded bounds")
    except Exception:
        result = {"state": "invalid_input", "files": {}}
        sys.stdout.buffer.write(json.dumps(result).encode("utf-8"))
        return
    output = {}
    state = "ok"
    for row in files:
        if (not isinstance(row, list) or len(row) != 2
                or not all(isinstance(item, str) for item in row)):
            state = "invalid_input"
            output = {}
            break
        rel, text = row
        try:
            tree = _load_hcl(text)
        except _HclUnavailable:
            state = "unavailable"
            output = {}
            break
        except Exception as exc:
            output[rel] = {"findings": [], "omissions": [
                f"{rel}: HCL could not be parsed ({type(exc).__name__})"],
                "parsed": False}
            continue
        try:
            findings, omissions = _terraform_findings_from_tree(
                rel, tree, text)
            output[rel] = {"findings": findings, "omissions": omissions,
                           "parsed": True}
        except Exception as exc:  # parser errors never escape the worker
            output[rel] = {"findings": [], "omissions": [
                f"{rel}: HCL could not be parsed ({type(exc).__name__})"],
                "parsed": False}
    payload = {"state": state, "files": output}
    encoded = json.dumps(payload, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_HCL_RESULT_BYTES:
        encoded = json.dumps({"state": "oversized", "files": {}}).encode()
    sys.stdout.buffer.write(encoded)


def _stop_hcl_worker(process) -> bool:
    """Kill and reap the parser worker with bounded waits only."""
    if process.poll() is None:
        # The parser inherits the analyzer worker's process group. Kill only
        # this child here; the outer review timeout can then terminate the
        # analyzer and any nested workers together as one process tree.
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    return True


def _terraform_hcl_batch(files: list[tuple[str, str]]) -> dict[str, dict]:
    """Parse a bounded batch in a killable child with CPU/address-space caps."""
    results: dict[str, dict] = {}
    selected: list[tuple[str, str]] = []
    total_bytes = 0
    for rel, text in files:
        size = len(text.encode("utf-8"))
        if len(selected) >= MAX_HCL_FILES or total_bytes + size > MAX_HCL_BYTES:
            results[rel] = {"findings": [], "omissions": [
                f"{rel}: Terraform HCL batch limit reached"], "parsed": False}
            continue
        selected.append((rel, text))
        total_bytes += size
    if not selected:
        return results
    encoded_input = json.dumps({"files": selected}, separators=(",", ":"),
                               ensure_ascii=False).encode("utf-8")
    if len(encoded_input) > MAX_HCL_BYTES + 256_000:
        for rel, _text in selected:
            results[rel] = {"findings": [], "omissions": [
                f"{rel}: HCL worker input exceeded size limit"], "parsed": False}
        return results
    package_parent = str(Path(__file__).resolve().parents[4])
    command = [sys.executable, "-I", "-B", "-c",
               "import sys; sys.path.insert(0, sys.argv[1]); "
               "from ozagia.core.deep_review.analyzers.operations import "
               "_hcl_subprocess_main; _hcl_subprocess_main()",
               package_parent]
    process = None
    started = False
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        started = True
        try:
            raw, _stderr = process.communicate(
                input=encoded_input, timeout=HCL_PARSE_TIMEOUT_S)
            if len(raw) > MAX_HCL_RESULT_BYTES:
                state = "HCL parser result exceeded its size limit"
            elif process.returncode != 0:
                state = "HCL parser worker exited unsuccessfully"
            else:
                payload = json.loads(raw.decode("utf-8"))
                state = payload.get("state")
                if state == "ok" and isinstance(payload.get("files"), dict):
                    results.update(payload["files"])
                elif state == "unavailable":
                    state = "optional HCL parser is unavailable"
                elif state == "oversized":
                    state = "HCL parser result exceeded its size limit"
                else:
                    state = "HCL parser worker returned an invalid result"
        except subprocess.TimeoutExpired:
            _stop_hcl_worker(process)
            try:
                process.communicate(timeout=1.0)
                state = "timed out"
            except subprocess.TimeoutExpired:
                _stop_hcl_worker(process)
                state = "timed out; worker did not close its output pipes"
    except Exception as exc:
        state = f"parser isolation failed ({type(exc).__name__})"
        if started and process is not None and process.poll() is None:
            _stop_hcl_worker(process)
    finally:
        if process is not None and process.poll() is None:
            _stop_hcl_worker(process)
    if state != "ok":
        for rel, _text in selected:
            results[rel] = {"findings": [], "omissions": [
                f"{rel}: HCL structure unknown because {state}"], "parsed": False}
    return results


def _static_config_findings(configs: dict[str, str]
                            ) -> tuple[list[dict], list[str], int, int, int, int,
                                       int, int]:
    """Inspect selected JSON/YAML structures and lexical configuration.

    Structural YAML and HCL checks require their optional parser extras. The
    supported checks inspect literal values only and do not establish effective
    deployed behavior.
    """
    findings: list[dict] = []
    omissions: list[str] = []
    json_checked = json_unknown = 0
    yaml_checked = yaml_unknown = 0
    terraform_files = [(rel, text) for rel, text in sorted(configs.items())
                       if Path(rel).name.lower().endswith((".tf", ".tfvars"))]
    terraform_results = _terraform_hcl_batch(terraform_files)
    terraform_parsed = terraform_unknown = 0

    def add(rel: str, line: int, rule_id: str, summary: str,
            recommendation: str, *, severity: str = "minor") -> None:
        if len(findings) >= MAX_CONFIG_FINDINGS:
            if "static configuration finding limit reached" not in omissions:
                omissions.append(
                    f"static configuration finding limit reached ({MAX_CONFIG_FINDINGS})")
            return
        findings.append({
            "section": "operations",
            "type": rule_id.rsplit(".", 1)[-1],
            "rule_id": rule_id,
            "rule": rule_id,
            "severity": severity,
            "confidence": "low",
            "keys": [f"file:{rel}"],
            "file": rel,
            "line": line,
            "summary": summary,
            "recommendation": recommendation,
        })

    for rel, text in sorted(configs.items()):
        lines = text.splitlines()
        lower_name = Path(rel).name.lower()
        if lower_name in _K8S_JSON_FILENAMES:
            json_checked += 1
            json_findings, reason = _kubernetes_json_findings(rel, text)
            findings.extend(json_findings[:max(0, MAX_CONFIG_FINDINGS - len(findings))])
            if reason:
                json_unknown += 1
                omissions.append(reason)
            if len(findings) >= MAX_CONFIG_FINDINGS and json_findings:
                if "static configuration finding limit reached" not in omissions:
                    omissions.append(
                        f"static configuration finding limit reached ({MAX_CONFIG_FINDINGS})")
            # JSON is inspected by structure above; lexical YAML patterns must
            # not fire on string values or unrelated fields.
            continue
        is_terraform = lower_name.endswith((".tf", ".tfvars"))
        is_compose = lower_name in {
            "compose.yml", "compose.yaml", "docker-compose.yml",
            "docker-compose.yaml"}
        is_kubernetes = lower_name in _K8S_FILENAMES or bool(_K8S_KIND.search(text))
        yaml_parsed = False
        if (is_compose or is_kubernetes) and lower_name.endswith((".yaml", ".yml")):
            yaml_checked += 1
            if is_compose:
                yaml_findings, reason, yaml_parsed = _compose_yaml_findings(rel, text)
            else:
                yaml_findings, reason, yaml_parsed = _kubernetes_yaml_findings(rel, text)
            findings.extend(yaml_findings[:max(0, MAX_CONFIG_FINDINGS - len(findings))])
            if reason:
                yaml_unknown += 1
                omissions.append(reason)

        if is_terraform:
            parsed_result = terraform_results.get(rel, {})
            parsed_ok = parsed_result.get("parsed") is True
            if parsed_ok:
                terraform_parsed += 1
                hcl_findings = parsed_result.get("findings")
                if isinstance(hcl_findings, list):
                    remaining = max(0, MAX_CONFIG_FINDINGS - len(findings))
                    findings.extend(hcl_findings[:remaining])
                    if len(hcl_findings) > remaining:
                        omissions.append(
                            f"static configuration finding limit reached ({MAX_CONFIG_FINDINGS})")
                hcl_omissions = parsed_result.get("omissions")
                if isinstance(hcl_omissions, list) and hcl_omissions:
                    terraform_unknown += 1
                    omissions.extend(item for item in hcl_omissions
                                     if isinstance(item, str))
            else:
                terraform_unknown += 1
                hcl_omissions = parsed_result.get("omissions")
                if isinstance(hcl_omissions, list):
                    omissions.extend(item for item in hcl_omissions
                                     if isinstance(item, str))
            if not parsed_ok:
                for index, line in enumerate(lines):
                    public_cidr = (
                        ("0.0.0.0/0" in line and "cidr_blocks" in line)
                        or ("::/0" in line and "ipv6_cidr_blocks" in line)
                    )
                    if (line.lstrip().startswith(("#", "//"))
                            or not public_cidr):
                        continue
                    resource_starts = [
                        position for position, source_line in enumerate(lines[:index + 1])
                        if re.match(r"^\s*resource\s+['\"]", source_line)]
                    if not resource_starts:
                        continue
                    start = resource_starts[-1]
                    next_resource = next((
                        position for position in range(index + 1, len(lines))
                        if re.match(r"^\s*resource\s+['\"]", lines[position])),
                        len(lines))
                    context = "\n".join(lines[start:next_resource])
                    resource_match = re.match(
                        r"\s*resource\s+['\"]([^'\"]+)", lines[start])
                    resource_type = resource_match.group(1).lower() if resource_match else ""
                    if re.search(r"(?im)^\s*type\s*=\s*['\"]egress['\"]", context):
                        continue
                    has_ingress = (
                        "ingress" in resource_type
                        or bool(re.search(r"(?im)^\s*type\s*=\s*['\"]ingress['\"]",
                                          context))
                        or bool(re.search(r"(?im)^\s*ingress\s*\{", context)))
                    if not has_ingress:
                        continue
                    admin_port = re.search(
                        r"(?im)^\s*(?:from_port|to_port)\s*=\s*(?:22|3389)\s*$",
                        context)
                    rule_id = ("iac.terraform.public_admin_ingress"
                               if admin_port else
                               "iac.terraform.public_ingress_cidr")
                    title = ("public SSH/RDP ingress" if admin_port else
                             "unrestricted public ingress")
                    add(rel, index + 1, rule_id,
                        f"{rel}:{index + 1} declares {title} using 0.0.0.0/0.",
                        "Confirm the rule is intentional and restrict source CIDRs or ports where possible.")

        if (is_compose or is_kubernetes) and not yaml_parsed:
            for index, line in enumerate(lines):
                if re.match(r"^\s*privileged\s*:\s*['\"]?true\b", line, re.I):
                    add(rel, index + 1, "iac.container.privileged_mode",
                        f"{rel}:{index + 1} explicitly enables privileged container mode.",
                        "Review whether the service needs host-level privileges and remove the setting if not.")
                if (is_kubernetes and re.match(
                        r"^\s*hostNetwork\s*:\s*['\"]?true\b", line, re.I)):
                    add(rel, index + 1, "iac.kubernetes.host_network",
                        f"{rel}:{index + 1} explicitly enables host networking.",
                        "Review port collisions and node-network exposure; use a pod network unless host networking is required.")
                if (is_kubernetes and re.match(
                        r"^\s*(?:-\s*)?hostPath\s*:", line)):
                    add(rel, index + 1, "iac.kubernetes.host_path_volume",
                        f"{rel}:{index + 1} declares a hostPath volume.",
                        "Review the mounted host path and access mode; prefer a managed volume when possible.")
                if (is_compose and re.search(
                        r"(?i)['\"]?0\.0\.0\.0:\d+(?:-\d+)?:\d+", line)):
                    add(rel, index + 1, "iac.compose.bind_all_interfaces",
                        f"{rel}:{index + 1} binds a published container port to all IPv4 interfaces.",
                        "Confirm public exposure is intended; bind to a narrower host address when appropriate.")

        for signal, pattern in _DISABLED_SIGNALS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                add(rel, line, f"ops.observability.{signal}_explicitly_disabled",
                    f"{rel}:{line} explicitly disables the {signal} telemetry signal in configuration.",
                    "Confirm the setting is intentional and that required operational telemetry is provided elsewhere.",
                    severity="note")

    return (findings, omissions, json_checked, json_unknown, yaml_checked,
            yaml_unknown, terraform_parsed, terraform_unknown)


def analyze(root: str | Path, *, budget_s: float = 10.0) -> dict:
    """Inventory common ops files and return narrow findings/coverage.

    Result keys are ``findings``, ``inventory``, ``checked``, and
    ``not_checked``. Source is read as text only; projects are never imported,
    executed, or contacted. Kubernetes/Compose YAML and selected Terraform
    ingress checks use bounded parser-backed inspection when optional parsers
    are installed; they are not schema or deployment validation. Other rules
    are lexical candidates, not semantic service/IaC validation. Secret detection,
    runtime health, telemetry effectiveness, and rollback correctness remain omitted. Absence of a
    runbook is coverage information, not a defect.
    """
    try:
        budget = float(budget_s)
    except (TypeError, ValueError) as exc:
        raise ValueError("budget_s must be finite and non-negative") from exc
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("budget_s must be finite and non-negative")
    budget = min(60.0, budget)
    root = Path(root).resolve()
    deadline = time.monotonic() + budget
    configs: dict[str, str] = {}
    workflow_files: list[str] = []
    workflow_findings = 0
    runbooks: dict[str, str] = {}
    findings, omissions = [], []
    count = total = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and
                         not (Path(base) / d).is_symlink())
        for name in sorted(names):
            if time.monotonic() >= deadline:
                omissions.append("time budget exhausted")
                break
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            count += 1
            if count > MAX_FILES:
                omissions.append(f"file-count limit reached ({MAX_FILES})")
                break
            rel = path.relative_to(root).as_posix()
            is_github_workflow = (
                rel.startswith(".github/workflows/")
                and path.suffix.lower() in {".yaml", ".yml"})
            systemd_suffixes = (".service", ".timer", ".container", ".volume",
                                ".pod", ".network")
            is_config = (name in _CONFIG_NAMES or name in _K8S_FILENAMES
                         or name.lower() in _K8S_JSON_FILENAMES
                         or name.endswith(systemd_suffixes)
                         or name.lower().endswith((".tf", ".tfvars"))
                         or is_github_workflow)
            is_yaml_candidate = name.endswith((".yaml", ".yml"))
            is_runbook = bool(_RUNBOOK.search(name)) and path.suffix.lower() in {".md", ".rst", ".txt"}
            if not (is_config or is_yaml_candidate or is_runbook):
                continue
            try:
                content, size = _read_text(
                    path, min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total), root=root)
            except ValueError:
                omissions.append(f"{rel}: exceeds read size limit")
                continue
            except (OSError, UnicodeError):
                omissions.append(f"{rel}: unreadable")
                continue
            total += size
            is_config = is_config or (is_yaml_candidate and bool(_K8S_KIND.search(content)))
            if not (is_config or is_runbook):
                continue
            if is_config:
                configs[rel] = content
            if is_github_workflow:
                workflow_files.append(rel)
                remaining = max(0, MAX_CONFIG_FINDINGS - workflow_findings)
                candidates = _github_workflow_findings(
                    rel, content, limit=remaining)
                findings.extend(candidates)
                workflow_findings += len(candidates)
                if workflow_findings >= MAX_CONFIG_FINDINGS:
                    omissions.append(
                        f"GitHub Actions finding limit reached ({MAX_CONFIG_FINDINGS})")
            if is_runbook:
                runbooks[rel] = content
            if name in {"Dockerfile", "Containerfile"}:
                healthcheck, line_no = _dockerfile_healthcheck(content)
                if healthcheck != "declared":
                    finding = {
                        "section": "operations",
                        "type": "container_healthcheck_absent" if healthcheck == "absent"
                        else "container_healthcheck_disabled",
                        "severity": "note", "confidence": "low", "keys": [f"file:{rel}"],
                        "summary": (f"{rel} has no HEALTHCHECK instruction in its final stage; "
                                    "an inherited or external health check may exist." if healthcheck == "absent"
                                    else f"{rel} inherits a disabled HEALTHCHECK from an earlier stage."
                                    if healthcheck == "inherited_disabled"
                                    else f"{rel} explicitly disables HEALTHCHECK in its final stage."),
                        "recommendation": "If this image is long-running, define an appropriate HEALTHCHECK or document the external health check.",
                    }
                    if line_no is not None:
                        finding["line"] = line_no
                    findings.append(finding)
        if count > MAX_FILES or time.monotonic() >= deadline:
            break

    runbook_checks = []
    for rel in sorted(runbooks):
        content = runbooks[rel]
        links = _runbook_links(root, rel, content)
        broken = [item for item in links if not item["exists"]]
        topics = _runbook_topics(content)
        runbook_checks.append({
            "file": rel,
            "topics": topics,
            "links_checked": len(links),
            "broken_links": [item["target"] for item in broken],
        })
        if not content.strip():
            findings.append({
                "section": "operations", "type": "runbook_content_empty",
                "severity": "note", "confidence": "high", "keys": [f"file:{rel}"],
                "summary": f"{rel} is a recognized runbook but contains no text.",
                "recommendation": "Add the recovery, verification, and escalation steps the runbook is meant to provide.",
            })
        elif not topics:
            findings.append({
                "section": "operations", "type": "runbook_operational_topics_absent",
                "severity": "note", "confidence": "low", "keys": [f"file:{rel}"],
                "summary": (f"{rel} is a recognized runbook but no recovery, rollback, "
                            "verification, escalation, logging, metrics, tracing, or "
                            "alerting topic was recognized."),
                "recommendation": ("Confirm the runbook contains actionable operational "
                                   "steps or rename/remove it so coverage is not overstated."),
            })
        for item in broken:
            target = str(item["target"])
            findings.append({
                "section": "operations", "type": "broken_runbook_link",
                "severity": "note", "confidence": "high",
                "keys": [f"file:{rel}", f"path:{target}"],
                "line": item["line"],
                "summary": f"{rel} links to `{target}`, but that local path was not found.",
                "recommendation": "Fix the link or restore the referenced operational document.",
            })
    if sum(item["links_checked"] for item in runbook_checks) >= MAX_RUNBOOK_LINKS:
        omissions.append(f"runbook link limit reached ({MAX_RUNBOOK_LINKS})")

    if not configs:
        omissions.append("no recognized operational configuration files found")
        omissions.append("static IaC/config checks were not run; configuration coverage is unknown")
    else:
        omissions.append(
            "IaC/config checks inspect selected JSON Kubernetes, optional Kubernetes/Compose YAML, literal Terraform ingress blocks, and selected lexical patterns; full semantic validation was not run")
    if not runbooks:
        omissions.append("no recognized runbook documents found; runbook coverage is unknown")
    (static_findings, static_omissions, json_checked, json_unknown,
     yaml_checked, yaml_unknown, terraform_parsed,
     terraform_unknown) = _static_config_findings(configs)
    findings.extend(static_findings)
    omissions.extend(static_omissions)
    observability = _observability(configs, runbooks)
    static_checks = [
        "iac.terraform.public_ingress_cidr",
        "iac.terraform.public_admin_ingress",
        "iac.container.privileged_mode",
        "iac.kubernetes.host_network",
        "iac.kubernetes.host_port",
        "iac.kubernetes.host_path_volume",
        "iac.compose.bind_all_interfaces",
        "ops.observability.*_explicitly_disabled",
    ]
    if workflow_files:
        static_checks.extend((
            "ops.github_actions.permissions_not_declared",
            "ops.github_actions.pull_request_target",
            "ops.github_actions.action_not_commit_pinned",
        ))
    static_validation = {
        "state": "checked" if configs else "unknown",
        "files_checked": len(configs),
        "checks": static_checks if configs else [],
        "findings": sum(1 for item in static_findings
                        if not item["rule_id"].startswith("ops.observability.")),
        "kubernetes_json_files_checked": json_checked,
        "kubernetes_json_files_unknown": json_unknown,
        "selected_yaml_files_checked": yaml_checked - yaml_unknown,
        "selected_yaml_files_unknown": yaml_unknown,
        "terraform_files_parsed": terraform_parsed,
        "terraform_files_unknown": terraform_unknown,
        "omitted": [
            "Structured inspection covers selected plain JSON Kubernetes objects, optional Kubernetes/Compose YAML, and literal Terraform ingress blocks; GitHub Actions checks are lexical, and systemd checks remain lexical.",
            "No Kubernetes schema/API validation, Terraform module evaluation or provider validation is run; templating, interpolation and effective merged configuration are not resolved.",
        ],
    }
    checks = ["operational_file_inventory"]
    if runbooks:
        checks.append("runbook_links_and_topics")
    if configs or runbooks:
        checks.append("observability_declarations")
    if configs:
        checks.append("static_iac_config_candidates")
    if workflow_files:
        checks.append("github_actions_workflow_candidates")
    return {"findings": findings,
            "inventory": {"configuration_files": sorted(configs),
                          "github_workflows": sorted(workflow_files),
                          "runbooks": sorted(runbooks),
                          "runbook_checks": runbook_checks,
                          "observability": observability,
                          "static_validation": static_validation,
                          "checks": checks,
                          "files_seen": min(count, MAX_FILES),
                          "bytes_read": total},
            "checked": bool(configs or runbooks),
            "not_checked": [{"reason": item} for item in sorted(set(omissions))]}


__all__ = ["analyze"]
