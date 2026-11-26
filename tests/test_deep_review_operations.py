import pytest

from ozagia.core.deep_review.analyzers.operations import analyze


def test_offline_inventory_and_healthcheck_candidate(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.13\nCMD app\n", encoding="utf-8")
    (tmp_path / "ops-runbook.md").write_text("Restart procedure\n", encoding="utf-8")

    result = analyze(tmp_path)

    assert result["checked"] is True
    assert result["inventory"]["configuration_files"] == ["Dockerfile"]
    assert result["inventory"]["runbooks"] == ["ops-runbook.md"]
    assert result["findings"][0]["type"] == "container_healthcheck_absent"
    assert result["findings"][0]["severity"] == "note"
    assert any("full semantic validation" in item["reason"]
               for item in result["not_checked"])


def test_healthcheck_presence_and_absent_runbook_are_coverage_only(tmp_path):
    (tmp_path / "Containerfile").write_text(
        "FROM alpine\nHEALTHCHECK CMD test -f /ready\n", encoding="utf-8")

    result = analyze(tmp_path)

    assert result["findings"] == []
    assert any("runbook coverage is unknown" in item["reason"]
               for item in result["not_checked"])


def test_multistage_healthcheck_only_reports_final_stage(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine AS build\nHEALTHCHECK CMD true\n"
        "FROM alpine AS final\nCMD app\n", encoding="utf-8")

    finding = analyze(tmp_path)["findings"][0]

    assert finding["type"] == "container_healthcheck_absent"
    assert "inherited or external health check may exist" in finding["summary"]


def test_explicit_disabled_healthcheck_is_not_treated_as_declaration(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine\nHEALTHCHECK CMD true\nHEALTHCHECK NONE\n", encoding="utf-8")

    finding = analyze(tmp_path)["findings"][0]

    assert finding["type"] == "container_healthcheck_disabled"
    assert finding["line"] == 3


def test_last_stage_with_healthcheck_is_not_flagged(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine AS build\nHEALTHCHECK NONE\n"
        "FROM alpine AS final\nHEALTHCHECK CMD true\n", encoding="utf-8")

    assert analyze(tmp_path)["findings"] == []


def test_final_stage_inherits_healthcheck_from_named_stage(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine AS base\nHEALTHCHECK CMD true\n"
        "FROM --platform=linux/amd64 BASE AS final\n", encoding="utf-8")
    assert analyze(tmp_path)["findings"] == []


def test_final_stage_inherits_disabled_healthcheck(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine AS base\nHEALTHCHECK NONE\nFROM base AS final\n", encoding="utf-8")
    finding = analyze(tmp_path)["findings"][0]
    assert finding["type"] == "container_healthcheck_disabled"
    assert "inherits" in finding["summary"]
    assert finding["line"] == 2


def test_unrecognized_repository_is_not_claimed_clean(tmp_path):
    (tmp_path / "app.py").write_text("pass\n", encoding="utf-8")

    result = analyze(tmp_path)

    assert result["checked"] is False
    assert result["findings"] == []
    assert len(result["not_checked"]) == 3


def test_runbook_links_and_topics_are_checked(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "recovery.md").write_text("Policy\n", encoding="utf-8")
    (tmp_path / "ops-runbook.md").write_text(
        "# Deploy rollback\n"
        "See [policy](docs/recovery.md) and [missing](docs/missing.md).\n"
        "Rollback: run the previous release and undo the migration.\n"
        "Verify with a smoke test and check readiness.\n"
        "Logs: inspect stdout; metrics: prometheus dashboard.\n",
        encoding="utf-8")

    result = analyze(tmp_path)

    runbook = result["inventory"]["runbook_checks"][0]
    assert runbook["file"] == "ops-runbook.md"
    assert runbook["links_checked"] == 2
    assert runbook["broken_links"] == ["docs/missing.md"]
    assert {"rollback", "verification", "logging", "metrics"} <= set(runbook["topics"])
    broken = [item for item in result["findings"]
              if item["type"] == "broken_runbook_link"]
    assert len(broken) == 1
    assert broken[0]["line"] == 2
    assert broken[0]["keys"] == ["file:ops-runbook.md", "path:docs/missing.md"]
    assert "runbook_links_and_topics" in result["inventory"]["checks"]


def test_observability_declarations_are_inventory_facts(tmp_path):
    (tmp_path / "compose.yaml").write_text(
        "services:\n"
        "  app:\n"
        "    logging:\n"
        "      driver: json-file\n"
        "    labels:\n"
        "      prometheus.io/scrape: 'true'\n"
        "x-monitoring: enabled\n",
        encoding="utf-8")

    result = analyze(tmp_path)

    observability = result["inventory"]["observability"]
    assert observability["runtime_monitoring"] == "unknown"
    assert observability["declarations"]["logging"]["state"] == "declared"
    assert observability["declarations"]["metrics"]["state"] == "declared"
    assert observability["declarations"]["tracing"]["state"] == "unknown"
    assert observability["declarations"]["observability"]["state"] == "declared"
    assert any("runtime logging" in reason
               for reason in observability["not_checked"])
    assert result["findings"] == []


def test_terraform_ingress_candidates_have_explicit_rules_and_line_evidence(tmp_path):
    (tmp_path / "main.tf").write_text(
        'resource "aws_security_group_rule" "ssh" {\n'
        '  type = "ingress"\n'
        '  from_port = 22\n'
        '  to_port = 22\n'
        '  cidr_blocks = ["0.0.0.0/0"]\n'
        '}\n'
        'resource "aws_security_group_rule" "egress" {\n'
        '  type = "egress"\n'
        '  cidr_blocks = ["0.0.0.0/0"]\n'
        '}\n', encoding="utf-8")

    result = analyze(tmp_path)

    assert [item["rule_id"] for item in result["findings"]] == [
        "iac.terraform.public_admin_ingress"]
    finding = result["findings"][0]
    assert finding["line"] == 5
    if finding.get("terraform_resource"):
        assert finding["terraform_resource"] == "aws_security_group_rule.ssh"
        assert finding["confidence"] == "medium"
        assert result["inventory"]["static_validation"][
            "terraform_files_parsed"] == 1
    assert result["findings"][0]["severity"] == "minor"
    assert result["findings"][0]["rule"] == result["findings"][0]["rule_id"]
    assert "static_iac_config_candidates" in result["inventory"]["checks"]
    assert result["inventory"]["static_validation"]["state"] == "checked"
    assert any("full semantic validation" in item["reason"]
               for item in result["not_checked"])


def test_static_validation_declares_all_structural_iac_checks(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    result = analyze(tmp_path)

    declared = set(result["inventory"]["static_validation"]["checks"])
    assert {
        "iac.terraform.public_ingress_cidr",
        "iac.terraform.public_admin_ingress",
        "iac.container.privileged_mode",
        "iac.kubernetes.host_network",
        "iac.kubernetes.host_port",
        "iac.kubernetes.host_path_volume",
        "iac.kubernetes.host_pid",
        "iac.kubernetes.host_ipc",
        "iac.kubernetes.run_as_root",
        "iac.kubernetes.root_user",
        "iac.kubernetes.privilege_escalation",
        "iac.kubernetes.add_all_capabilities",
        "iac.compose.bind_all_interfaces",
    } <= declared


def test_terraform_public_ipv6_ingress_is_detected_but_egress_is_not(tmp_path):
    (tmp_path / "main.tf").write_text(
        'resource "aws_vpc_security_group_ingress_rule" "public_v6" {\n'
        '  ip_protocol = "tcp"\n'
        '  from_port = 443\n'
        '  to_port = 443\n'
        '  ipv6_cidr_blocks = ["::/0"]\n'
        '}\n'
        'resource "aws_vpc_security_group_egress_rule" "all_v6" {\n'
        '  ip_protocol = "-1"\n'
        '  ipv6_cidr_blocks = ["::/0"]\n'
        '}\n', encoding="utf-8")

    result = analyze(tmp_path)

    findings = [item for item in result["findings"]
                if item.get("rule_id", "").startswith("iac.terraform.")]
    assert [item["rule_id"] for item in findings] == [
        "iac.terraform.public_ingress_cidr"]
    assert findings[0]["line"] == 5


def test_terraform_hcl_structural_check_handles_nested_multiline_blocks_once(tmp_path):
    pytest.importorskip("hcl2")
    (tmp_path / "main.tf").write_text(
        'resource "aws_security_group" "web" {\n'
        '  ingress {\n'
        '    from_port = 443\n'
        '    to_port = 443\n'
        '    protocol = "tcp"\n'
        '    cidr_blocks = [\n'
        '      "0.0.0.0/0",\n'
        '    ]\n'
        '  }\n'
        '  egress {\n'
        '    from_port = 0\n'
        '    to_port = 0\n'
        '    cidr_blocks = ["0.0.0.0/0"]\n'
        '  }\n'
        '}\n', encoding="utf-8")

    result = analyze(tmp_path)

    findings = [item for item in result["findings"]
                if item.get("rule_id", "").startswith("iac.terraform.")]
    assert len(findings) == 1
    assert findings[0]["rule_id"] == "iac.terraform.public_ingress_cidr"
    assert findings[0]["terraform_resource"] == "aws_security_group.web.ingress[0]"
    assert findings[0]["line"] == 2
    assert findings[0]["confidence"] == "medium"
    assert result["inventory"]["static_validation"][
        "terraform_files_unknown"] == 0


def test_invalid_hcl_reports_unknown_and_uses_lexical_fallback(tmp_path):
    pytest.importorskip("hcl2")
    (tmp_path / "main.tf").write_text(
        'resource "aws_security_group_rule" "ssh" {\n'
        '  type = "ingress"\n'
        '  invalid = [\n'
        '  cidr_blocks = ["0.0.0.0/0"]\n'
        '}\n', encoding="utf-8")

    result = analyze(tmp_path)

    assert any(item.get("rule_id") == "iac.terraform.public_ingress_cidr"
               for item in result["findings"])
    assert result["inventory"]["static_validation"][
        "terraform_files_unknown"] == 1
    assert any("HCL could not be parsed" in item["reason"]
               for item in result["not_checked"])


def test_missing_hcl_parser_is_lazy_unknown_with_lexical_fallback(
        tmp_path, monkeypatch):
    import builtins
    from ozagia.core.deep_review.analyzers import operations

    original_import = builtins.__import__

    def without_hcl2(name, *args, **kwargs):
        if name == "hcl2":
            raise ImportError("fake missing optional parser")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_hcl2)
    with pytest.raises(operations._HclUnavailable):
        operations._load_hcl('resource "x" "y" {}')

    (tmp_path / "main.tf").write_text(
        'resource "aws_security_group_rule" "ssh" {\n'
        ' type = "ingress"\n cidr_blocks = ["0.0.0.0/0"]\n}\n',
        encoding="utf-8")
    monkeypatch.setattr(operations, "_terraform_hcl_batch", lambda files: {
        rel: {"findings": [], "omissions": [
            f"{rel}: HCL structure unknown because optional parser is unavailable"],
              "parsed": False}
        for rel, _text in files})

    result = operations.analyze(tmp_path)

    assert result["inventory"]["static_validation"][
        "terraform_files_unknown"] == 1
    assert any("optional parser is unavailable" in item["reason"]
               for item in result["not_checked"])
    assert any(item.get("rule_id") == "iac.terraform.public_ingress_cidr"
               for item in result["findings"])


def test_hcl_parser_timeout_is_reported_without_waiting_indefinitely(monkeypatch):
    from subprocess import TimeoutExpired
    from ozagia.core.deep_review.analyzers import operations

    class TimedOutProcess:
        pid = 987654321
        returncode = None

        def communicate(self, **kwargs):
            if kwargs.get("timeout") is not None:
                raise TimeoutExpired("fake-hcl-worker", kwargs["timeout"])
            return b"", b""

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    process = TimedOutProcess()
    monkeypatch.setattr(operations.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(operations.os, "killpg",
                        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))

    result = operations._terraform_hcl_batch([("main.tf", "resource x y {}")])

    assert result["main.tf"]["parsed"] is False
    assert "timed out" in result["main.tf"]["omissions"][0]


def test_hcl_timeout_cleanup_never_uses_unbounded_pipe_or_process_waits(
        monkeypatch):
    from subprocess import TimeoutExpired
    from ozagia.core.deep_review.analyzers import operations

    class StuckProcess:
        pid = 987654322
        returncode = None
        communicate_timeouts = []
        wait_timeouts = []

        def communicate(self, **kwargs):
            self.communicate_timeouts.append(kwargs.get("timeout"))
            raise TimeoutExpired("fake-hcl-worker", kwargs.get("timeout"))

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            return self.returncode

    process = StuckProcess()
    monkeypatch.setattr(operations.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(operations.os, "killpg",
                        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))

    result = operations._terraform_hcl_batch([("main.tf", "resource x y {}")])

    assert result["main.tf"]["parsed"] is False
    assert process.communicate_timeouts == [operations.HCL_PARSE_TIMEOUT_S, 1.0]
    assert process.wait_timeouts == [1.0, 1.0]


def test_hcl_worker_uses_isolated_python_and_explicit_analyzer_path(monkeypatch):
    import sys
    from ozagia.core.deep_review.analyzers import operations

    class CompletedProcess:
        pid = 123
        returncode = 0

        def communicate(self, **kwargs):
            return (b'{"state":"ok","files":{"main.tf":'
                    b'{"findings":[],"omissions":[],"parsed":true}}}', b"")

        def poll(self):
            return self.returncode

    calls = []
    process = CompletedProcess()

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(operations.subprocess, "Popen", popen)

    result = operations._terraform_hcl_batch([("main.tf", "resource x y {}")])

    argv, kwargs = calls[0]
    assert argv[:3] == [sys.executable, "-I", "-B"]
    assert argv[-1] == str(operations.Path(operations.__file__).resolve().parents[4])
    assert "sys.path.insert(0, sys.argv[1])" in argv[4]
    assert "start_new_session" not in kwargs
    assert result["main.tf"]["parsed"] is True


def test_kubernetes_and_compose_explicit_exposure_candidates(tmp_path):
    (tmp_path / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
        "  template:\n    spec:\n      hostNetwork: true\n"
        "      containers:\n      - name: api\n"
        "        securityContext:\n          privileged: true\n"
        "      volumes:\n      - hostPath:\n          path: /var/run\n",
        encoding="utf-8")
    (tmp_path / "compose.yaml").write_text(
        "services:\n  api:\n    ports:\n      - '0.0.0.0:8080:80'\n"
        "    privileged: true\n", encoding="utf-8")

    result = analyze(tmp_path)

    rules = {item["rule_id"] for item in result["findings"]}
    assert rules == {
        "iac.kubernetes.host_network", "iac.container.privileged_mode",
        "iac.kubernetes.host_path_volume", "iac.compose.bind_all_interfaces",
    }
    assert len(result["findings"]) == 5  # privileged appears in both files


def test_plain_json_kubernetes_manifest_uses_structural_paths(tmp_path):
    (tmp_path / "deployment.json").write_text(
        '{"apiVersion":"apps/v1","kind":"Deployment","metadata":{'
        '"annotations":{"example":"privileged: true"}},"spec":{"template":{'
        '"spec":{"hostNetwork":true,"containers":[{"name":"api",'
        '"securityContext":{"privileged":true}}],"volumes":[{"name":"host",'
        '"hostPath":{"path":"/var/run"}}]}}}}',
        encoding="utf-8")

    result = analyze(tmp_path)

    by_rule = {item["rule_id"]: item for item in result["findings"]}
    assert set(by_rule) == {
        "iac.kubernetes.host_network", "iac.container.privileged_mode",
        "iac.kubernetes.host_path_volume",
    }
    assert by_rule["iac.kubernetes.host_network"]["json_pointer"] == (
        "/spec/template/spec/hostNetwork")
    assert by_rule["iac.container.privileged_mode"]["json_pointer"] == (
        "/spec/template/spec/containers/0/securityContext/privileged")
    assert by_rule["iac.kubernetes.host_path_volume"]["evidence_kind"] == "declared"
    assert result["inventory"]["static_validation"][
        "kubernetes_json_files_checked"] == 1
    assert result["inventory"]["static_validation"][
        "kubernetes_json_files_unknown"] == 0


def test_kubernetes_json_security_context_checks_are_exact_and_structural(tmp_path):
    (tmp_path / "deployment.json").write_text(
        '{"apiVersion":"apps/v1","kind":"Deployment","spec":{'
        '"template":{"spec":{"securityContext":{"runAsNonRoot":false},'
        '"containers":[{"name":"api","securityContext":{'
        '"allowPrivilegeEscalation":true,"capabilities":{"add":["ALL"]}}}]}}}}',
        encoding="utf-8")

    result = analyze(tmp_path)

    by_rule = {item["rule_id"]: item for item in result["findings"]}
    assert set(by_rule) == {
        "iac.kubernetes.run_as_root",
        "iac.kubernetes.privilege_escalation",
        "iac.kubernetes.add_all_capabilities",
    }
    assert by_rule["iac.kubernetes.run_as_root"]["json_pointer"] == (
        "/spec/template/spec/securityContext/runAsNonRoot")
    assert by_rule["iac.kubernetes.privilege_escalation"]["json_pointer"] == (
        "/spec/template/spec/containers/0/securityContext/allowPrivilegeEscalation")
    assert by_rule["iac.kubernetes.add_all_capabilities"]["json_pointer"] == (
        "/spec/template/spec/containers/0/securityContext/capabilities/add")


def test_kubernetes_host_namespaces_and_root_uid_are_explicit(tmp_path):
    (tmp_path / "pod.json").write_text(
        '{"apiVersion":"v1","kind":"Pod","spec":{'
        '"hostPID":true,"hostIPC":true,"securityContext":{"runAsUser":0},'
        '"containers":[{"name":"api","securityContext":{"runAsUser":0}},'
        '{"name":"other","securityContext":{"runAsUser":false}}],'
        '"initContainers":[{"name":"init","securityContext":{"runAsUser":"0"}}]}}',
        encoding="utf-8")

    findings = analyze(tmp_path)["findings"]
    selected = [item for item in findings if item.get("rule_id") in {
        "iac.kubernetes.host_pid", "iac.kubernetes.host_ipc", "iac.kubernetes.root_user"}]
    assert {(item["rule_id"], item["json_pointer"]) for item in selected} == {
        ("iac.kubernetes.host_pid", "/spec/hostPID"),
        ("iac.kubernetes.host_ipc", "/spec/hostIPC"),
        ("iac.kubernetes.root_user", "/spec/securityContext/runAsUser"),
        ("iac.kubernetes.root_user", "/spec/containers/0/securityContext/runAsUser"),
    }
    assert all(item["truth_assessed"] is False for item in selected)


def test_kubernetes_yaml_host_namespace_evidence_uses_structural_pointer(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "pod.yaml").write_text(
        "apiVersion: v1\nkind: Pod\nspec:\n  hostPID: true\n"
        "  containers:\n  - name: api\n    securityContext:\n      runAsUser: 0\n",
        encoding="utf-8")

    findings = analyze(tmp_path)["findings"]
    selected = [item for item in findings if item.get("rule_id") in {
        "iac.kubernetes.host_pid", "iac.kubernetes.root_user"}]
    assert {(item["rule_id"], item["yaml_pointer"]) for item in selected} == {
        ("iac.kubernetes.host_pid", "/spec/hostPID"),
        ("iac.kubernetes.root_user", "/spec/containers/0/securityContext/runAsUser"),
    }


def test_json_kubernetes_candidates_require_exact_boolean_and_object_paths(tmp_path):
    (tmp_path / "pod.json").write_text(
        '{"apiVersion":"v1","kind":"Pod","metadata":{"annotations":'
        '{"hostNetwork":"true","privileged":true}},"spec":{"hostNetwork":"true",'
        '"containers":[{"name":"api","privileged":true,"securityContext":'
        '{"privileged":"true"}}],"volumes":[{"name":"host","hostPath":"/tmp"}]}}',
        encoding="utf-8")

    result = analyze(tmp_path)

    assert not [item for item in result["findings"]
                if item.get("rule_id", "").startswith("iac.kubernetes.")
                or item.get("rule_id") == "iac.container.privileged_mode"]


@pytest.mark.parametrize("source", [
    '{"apiVersion":"v1","kind":"Pod","kind":"Pod","spec":{}}',
    '{"apiVersion":"v1","kind":"Deployment","spec":{}}',
    '{"apiVersion":"v1","kind":"Deployment","spec":{"template":'
    '{"spec":{"hostNetwork":"${HOST_NETWORK}"}}}}',
])
def test_malformed_or_dynamic_kubernetes_json_is_unknown(tmp_path, source):
    (tmp_path / "deployment.json").write_text(source, encoding="utf-8")

    result = analyze(tmp_path)

    assert result["findings"] == []
    assert result["inventory"]["static_validation"][
        "kubernetes_json_files_unknown"] == 1


def test_yaml_structural_checks_use_nested_kubernetes_and_compose_values(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nspec:\n"
        "  template:\n    spec:\n      hostNetwork: true\n"
        "      containers:\n        - name: api\n"
        "          securityContext:\n            privileged: true\n",
        encoding="utf-8")
    (tmp_path / "compose.yaml").write_text(
        "services:\n  api:\n    privileged: true\n    ports:\n"
        "      - target: 80\n        published: 8080\n"
        "        host_ip: 0.0.0.0\n",
        encoding="utf-8")

    result = analyze(tmp_path)

    findings = [item for item in result["findings"]
                if item.get("rule_id", "").startswith("iac.")]
    assert {item["rule_id"] for item in findings} == {
        "iac.kubernetes.host_network", "iac.container.privileged_mode",
        "iac.compose.bind_all_interfaces"}
    assert all(item["confidence"] == "medium" for item in findings)
    assert all(item["yaml_pointer"].startswith("/") for item in findings)
    assert result["inventory"]["static_validation"][
        "selected_yaml_files_checked"] == 2
    assert result["inventory"]["static_validation"][
        "selected_yaml_files_unknown"] == 0


def test_supported_kubernetes_kinds_are_selected_by_kind_as_well_as_filename(
        tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "nightly.yaml").write_text(
        "apiVersion: batch/v1\nkind: CronJob\nspec:\n"
        "  jobTemplate:\n    spec:\n      template:\n"
        "        spec:\n          containers:\n"
        "          - name: worker\n            securityContext:\n"
        "              privileged: true\n",
        encoding="utf-8")
    (tmp_path / "legacy-workload.yml").write_text(
        "apiVersion: v1\nkind: ReplicationController\nspec:\n"
        "  template:\n    spec:\n      hostNetwork: true\n",
        encoding="utf-8")

    result = analyze(tmp_path)

    findings = [item for item in result["findings"]
                if item.get("rule_id", "").startswith("iac.kubernetes.")
                or item.get("rule_id") == "iac.container.privileged_mode"]
    assert {item["rule_id"] for item in findings} == {
        "iac.container.privileged_mode", "iac.kubernetes.host_network"}
    assert {item["file"] for item in findings} == {
        "nightly.yaml", "legacy-workload.yml"}
    assert result["inventory"]["static_validation"][
        "selected_yaml_files_checked"] == 2
    assert result["inventory"]["static_validation"][
        "selected_yaml_files_unknown"] == 0


def test_structural_yaml_suppresses_lexical_false_positive_on_safe_config(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "compose.yaml").write_text(
        "services:\n  api:\n    privileged: false\n"
        "    labels:\n      note: |\n        privileged: true\n",
        encoding="utf-8")

    result = analyze(tmp_path)

    assert not [item for item in result["findings"]
                if item.get("rule_id") == "iac.container.privileged_mode"]


@pytest.mark.parametrize("fragment", [
    "environment:\n      VALUE: ${VALUE}\n",
    "extends: ./base.yaml\n",
])
def test_dynamic_or_extended_compose_yaml_is_unknown(tmp_path, fragment):
    pytest.importorskip("yaml")
    (tmp_path / "compose.yaml").write_text(
        "services:\n  api:\n    privileged: true\n    " + fragment,
        encoding="utf-8")

    result = analyze(tmp_path)

    assert not [item for item in result["findings"]
                if item.get("rule_id") == "iac.container.privileged_mode"]
    assert result["inventory"]["static_validation"][
        "selected_yaml_files_unknown"] == 1
    assert any("not semantically inspected" in item["reason"]
               or "extends references were not resolved" in item["reason"]
               for item in result["not_checked"])


def test_missing_yaml_parser_is_reported_and_keeps_lexical_fallback(
        tmp_path, monkeypatch):
    import builtins
    from ozagia.core.deep_review.analyzers import operations

    original_import = builtins.__import__

    def without_yaml(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("fake missing optional parser")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_yaml)
    (tmp_path / "compose.yaml").write_text(
        "services:\n  api:\n    privileged: true\n", encoding="utf-8")

    result = operations.analyze(tmp_path)

    assert any("PyYAML is unavailable" in item["reason"]
               for item in result["not_checked"])
    assert result["inventory"]["static_validation"][
        "selected_yaml_files_unknown"] == 1
    assert any(item.get("rule_id") == "iac.container.privileged_mode"
               for item in result["findings"])


def test_yaml_duplicate_keys_are_unknown_and_do_not_emit_lexical_findings(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nkind: Pod\n"
        "spec:\n  hostNetwork: true\n", encoding="utf-8")

    result = analyze(tmp_path)

    candidates = [item for item in result["findings"]
                  if item.get("rule_id") == "iac.kubernetes.host_network"]
    assert len(candidates) == 1
    assert "yaml_pointer" not in candidates[0]  # lexical fallback, not structural
    assert result["inventory"]["static_validation"][
        "selected_yaml_files_unknown"] == 1
    assert any("could not be inspected" in item["reason"]
               for item in result["not_checked"])


def test_yaml_aliases_are_rejected_as_unknown(tmp_path):
    pytest.importorskip("yaml")
    (tmp_path / "deployment.yaml").write_text(
        "apiVersion: v1\nkind: Pod\nspec: &spec\n"
        "  hostNetwork: true\ncopy: *spec\n", encoding="utf-8")

    result = analyze(tmp_path)

    assert result["inventory"]["static_validation"][
        "selected_yaml_files_unknown"] == 1
    assert any("could not be inspected" in item["reason"]
               for item in result["not_checked"])


def test_explicit_telemetry_disable_is_reported_without_claiming_runtime_state(
        tmp_path):
    (tmp_path / "compose.yaml").write_text(
        "services:\n  api:\n    environment:\n"
        "      OTEL_SDK_DISABLED: 'true'\n"
        "      OTEL_METRICS_EXPORTER: none\n"
        "      OTEL_TRACES_EXPORTER=none\n", encoding="utf-8")

    result = analyze(tmp_path)

    rules = {item["rule_id"] for item in result["findings"]}
    assert rules == {
        "ops.observability.observability_explicitly_disabled",
        "ops.observability.metrics_explicitly_disabled",
        "ops.observability.tracing_explicitly_disabled",
    }
    declarations = result["inventory"]["observability"]["declarations"]
    assert declarations["observability"]["state"] == "explicitly_disabled"
    assert declarations["metrics"]["state"] == "explicitly_disabled"
    assert result["inventory"]["observability"]["runtime_monitoring"] == "unknown"
    assert all(item["severity"] == "note" for item in result["findings"])


def test_static_configuration_candidate_limit_is_explicit(tmp_path, monkeypatch):
    import ozagia.core.deep_review.analyzers.operations as operations

    (tmp_path / "main.tf").write_text(
        'resource "aws_security_group_rule" "a" {\n'
        ' type = "ingress"\n cidr_blocks = ["0.0.0.0/0"]\n}\n'
        'resource "aws_security_group_rule" "b" {\n'
        ' type = "ingress"\n cidr_blocks = ["0.0.0.0/0"]\n}\n',
        encoding="utf-8")
    monkeypatch.setattr(operations, "MAX_CONFIG_FINDINGS", 1)

    result = analyze(tmp_path)

    assert len(result["findings"]) == 1
    assert any("finding limit reached" in item["reason"]
               for item in result["not_checked"])


def test_runbook_without_recognized_topics_is_a_note(tmp_path):
    (tmp_path / "runbook.md").write_text(
        "Ask the team for the current status.\n", encoding="utf-8")

    result = analyze(tmp_path)

    assert [item["type"] for item in result["findings"]] == [
        "runbook_operational_topics_absent"]
    assert result["findings"][0]["section"] == "operations"


def test_github_workflows_report_unpinned_actions_and_privileged_triggers(tmp_path):
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: CI\n"
        "on:\n"
        "  pull_request_target:\n"
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4 # mutable tag\n"
        "      - uses: example/action@0123456789abcdef0123456789abcdef01234567\n",
        encoding="utf-8")

    result = analyze(tmp_path)

    findings = {item["rule_id"]: item for item in result["findings"]}
    assert set(findings) == {
        "ops.github_actions.permissions_not_declared",
        "ops.github_actions.pull_request_target",
        "ops.github_actions.action_not_commit_pinned",
    }
    assert findings["ops.github_actions.pull_request_target"]["line"] == 3
    assert findings["ops.github_actions.action_not_commit_pinned"]["line"] == 7
    assert result["inventory"]["configuration_files"] == [
        ".github/workflows/ci.yml"]
    assert "github_actions_workflow_candidates" in result["inventory"]["checks"]
    assert all(item["truth_assessed"] is False for item in findings.values())
    assert all("v4" not in str(item) for item in findings.values())


def test_github_actions_check_is_not_claimed_without_workflows(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")

    result = analyze(tmp_path)

    assert "github_actions_workflow_candidates" not in result["inventory"]["checks"]
    assert not result["inventory"]["github_workflows"]


@pytest.mark.parametrize("trigger", [
    "on: pull_request_target\n",
    "\"on\": pull_request_target\n",
    "on: [push, pull_request_target]\n",
    "on: {pull_request_target: {types: [opened]}}\n",
])
def test_github_workflow_detects_inline_pull_request_target_forms(tmp_path, trigger):
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(trigger, encoding="utf-8")

    result = analyze(tmp_path)

    assert "ops.github_actions.pull_request_target" in {
        item["rule_id"] for item in result["findings"]}


def test_github_workflow_detects_block_sequence_pull_request_target(tmp_path):
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        "on:\n  - push\n  - pull_request_target\njobs:\n  test:\n    runs-on: ubuntu-latest\n",
        encoding="utf-8")

    result = analyze(tmp_path)

    finding = next(item for item in result["findings"]
                   if item["rule_id"] == "ops.github_actions.pull_request_target")
    assert finding["line"] == 3


def test_github_workflow_permissions_and_commit_pins_suppress_candidates(tmp_path):
    workflow = tmp_path / ".github/workflows/ci.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "permissions:\n  contents: read\njobs:\n  test:\n    steps:\n"
        "      - uses: example/action@0123456789abcdef0123456789abcdef01234567\n",
        encoding="utf-8")

    assert analyze(tmp_path)["findings"] == []


def test_github_workflow_recognizes_quoted_permissions_key(tmp_path):
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text('"permissions": { contents: read }\n', encoding="utf-8")

    assert "ops.github_actions.permissions_not_declared" not in {
        item["rule_id"] for item in analyze(tmp_path)["findings"]}


def test_operations_inventory_is_collected_and_merged(tmp_path, monkeypatch):
    from ozagia.core.deep_review.collect import collect
    (tmp_path / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    monkeypatch.setattr("ozagia.core.deep_review.collect.repo_review.review_repo",
        lambda *a, **k: {"findings": [], "sections": {"operations": {
            "checked": ["ops.tracked_sources"],
            "not_checked": [{"reason": "deployment graph unavailable"}] }},
            "graph": {"freshness": "unknown"}})
    packet = collect(tmp_path, profile="operations", budget_s=5)
    section = packet["sections"]["operations"]
    assert section["checked"] == ["ops.tracked_sources", "operational_file_inventory",
                                  "observability_declarations",
                                  "static_iac_config_candidates"]
    assert section["not_checked"] == [
        {"reason": "deployment graph unavailable"},
        {"reason": "IaC/config checks inspect selected JSON Kubernetes, optional Kubernetes/Compose YAML, literal Terraform ingress blocks, and selected lexical patterns; full semantic validation was not run"},
        {"reason": "no recognized runbook documents found; runbook coverage is unknown"},
    ]
    assert section["inventory"]["configuration_files"] == ["Dockerfile"]
    assert packet["findings"][0]["type"] == "container_healthcheck_absent"


def test_kubernetes_host_port_uses_precise_pointer_and_literal_integer(tmp_path):
    (tmp_path / "pod.json").write_text(
        '{"apiVersion":"v1","kind":"Pod","spec":{"containers":[{"name":"app",'
        '"ports":[{"hostPort":8080},{"hostPort":true},{"hostPort":"9090"},'
        '{"hostPort":0},{"hostPort":65536}]}],"initContainers":['
        '{"name":"init","ports":[{"hostPort":80}]}]}}', encoding="utf-8")
    findings = [item for item in analyze(tmp_path)["findings"]
                if item.get("rule_id") == "iac.kubernetes.host_port"]
    assert {item["json_pointer"] for item in findings} == {
        "/spec/containers/0/ports/0/hostPort",
        "/spec/initContainers/0/ports/0/hostPort",
    }
    assert all(item["truth_assessed"] is False for item in findings)
    assert all("depends on" in item["summary"] for item in findings)


def test_runbook_coverage_is_propagated_to_collected_packet(tmp_path, monkeypatch):
    from ozagia.core.deep_review.collect import collect

    (tmp_path / "runbook.md").write_text("Restart the service.\n", encoding="utf-8")
    monkeypatch.setattr("ozagia.core.deep_review.collect.repo_review.review_repo",
                        lambda *a, **k: {"findings": [], "sections": {},
                                         "graph": {"freshness": "unknown"}})
    packet = collect(tmp_path, profile="operations", budget_s=5)
    checks = packet["sections"]["ops"]["checked"]
    assert "runbook_links_and_topics" in checks
    assert "observability_declarations" in checks
