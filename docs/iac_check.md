# Offline IaC pre-plan checks

`ozagia.core.iac_check.check_iac(root)` performs bounded, source-only checks
for Terraform/OpenTofu files. It does not invoke `terraform`, `tofu`, providers,
plugins, module installers, project code, network services or cloud APIs.

The result has a `status` (`clean`, `findings`, `ambiguous`, or `unsupported`),
source-attributed `findings` (`path`, `line`, `kind`, `message`, `evidence`),
bounded diagnostics and a file count. Checks include undeclared `var.*` and
`local.*` references, unresolved graph references, missing local module source
directories, and used provider names without a `required_providers`
declaration. Dynamic expressions and constructs the source graph cannot resolve
remain unsupported or unresolved rather than being evaluated.

Source discovery skips hidden/generated directories including `.terraform`,
`.git`, `.ozagia`, `.terragrunt-cache` and `vendor`. Each file is limited to
2 MiB, a scan to 500 files and 20 MiB total, with symlink/path containment
checks. Findings deliberately omit source values and full reference text.
These are pre-flight hints, not a substitute for `terraform validate` or a
plan; no infrastructure command is run by this API.
