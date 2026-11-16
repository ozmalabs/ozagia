from ozagia.core.iac_check import check_iac


def _write(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_reports_undeclared_local_variable_and_missing_module(tmp_path):
    _write(tmp_path, "main.tf", '''terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}
module "child" { source = "./missing" }
resource "aws_vpc" "main" {
  cidr_block = var.undeclared
  tags = local.undeclared
}
''')
    result = check_iac(tmp_path)
    kinds = {finding.kind for finding in result.findings}
    assert {"undeclared_local", "undeclared_variable", "missing_local_module"} <= kinds
    assert all(finding.path == "main.tf" and finding.line > 0 for finding in result.findings)
    assert result.status == "findings"


def test_reports_missing_provider_requirement_and_unresolved_reference(tmp_path):
    _write(tmp_path, "main.tf", '''resource "aws_instance" "main" {
  subnet_id = aws_subnet.missing.id
}
''')
    result = check_iac(tmp_path)
    kinds = {finding.kind for finding in result.findings}
    assert "missing_provider_requirement" in kinds
    assert "unresolved_reference" in kinds


def test_clean_root_and_local_module_directory(tmp_path):
    _write(tmp_path, "main.tf", '''terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}
variable "region" {}
locals { tag = "safe" }
module "child" { source = "./modules/child" }
resource "aws_vpc" "main" { cidr_block = "10.0.0.0/16" }
''')
    _write(tmp_path, "modules/child/main.tf", 'output "id" { value = "safe" }\n')
    result = check_iac(tmp_path)
    assert result.status == "clean"
    assert not result.findings
    assert result.files_checked == 2


def test_findings_never_return_source_values_or_canaries(tmp_path):
    _write(tmp_path, "main.tf", '''resource "aws_vpc" "main" {
  description = "CANARY_SECRET_VALUE"
  cidr_block = var.not_declared
}
''')
    result = check_iac(tmp_path)
    assert "CANARY_SECRET_VALUE" not in repr(result)
    assert all("CANARY_SECRET_VALUE" not in finding.message for finding in result.findings)


def test_malformed_source_is_reported_unsupported(tmp_path):
    _write(tmp_path, "main.tf", 'resource "aws_vpc" "main" { cidr_block = "x"\n')
    result = check_iac(tmp_path)
    assert result.status == "unsupported"
    assert result.diagnostics
