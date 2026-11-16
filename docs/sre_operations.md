# SRE/IaC operations CLI and MCP surface

These commands are offline by default. Live job polling and any future cloud or
cluster provider require explicit flags or approval; see
[the extension plan](plans/2026-11-16_sre_iac_extensions.md).

## CLI

```sh
# Source-only Terraform/OpenTofu pre-plan checks (no init/plan/providers)
ozagia iac check [--root .] [--json]
ozagia check iac [--root .] [--json]      # alias
ozagia env list [--root .] [--json]       # declarative profiles only
ozagia env show staging [--root .] [--json]

# Append or inspect bounded, redacted ops receipts
ozagia ops record --record '{"harness":"opencode","session":"...",
  "environment":"staging","kind":"observation","summary":"...","findings":[]}'
ozagia ops resume --environment staging [--limit 20] [--json]

# Supplied live-state artifacts only; no provider call
ozagia live import --before-file snapshot.json --provenance ci [--json]
ozagia live diff --before-file old.json --after-file new.json [--json]
ozagia live save --before-file snapshot.json --root . --provenance ci

# Dry-run emulator plan; it prints commands and executes nothing
ozagia preflight plan --root envs/prod --root envs/staging \
  --dependency envs/prod:envs/staging \
  --emulator-bin /opt/aws-emulator/1.0.385/bin/emulator [--json]

# Explicit read-only live job polling (opt-in)
ozagia job wait --provider codebuild --job-id project:build-id \
  --region us-east-1 --allow-live [--json]
```

Without `--allow-live`, `job wait` returns `status: disabled` and does not run
a provider. The job command only supports reviewed read-only polling shapes;
it never starts, applies, deletes, patches or mutates a resource.

## MCP tools

The full coding surface adds these read-only/offline tools:

| Tool | Purpose |
|---|---|
| `dev_environments` | Declarative environment profiles only. |
| `dev_check_iac` | Source-only Terraform/OpenTofu checks. |
| `dev_ops_record` / `dev_ops_resume` | Ops receipts and bounded handoffs. |
| `dev_live_import` / `dev_live_diff` / `dev_live_save` | Supplied snapshot artifacts only. |
| `dev_aws_cli_check` | Offline AWS CLI service/operation/enum check. |
| `dev_preflight_plan` | Dry-run emulator plan. |

The minimal graph surface intentionally stays small and does not add these
tools. No MCP tool starts a job, contacts a provider/cluster, or executes a
preflight.

## Shell hints

After a captured shell command, the daemon may append:

- a wait hint when the output contains a newly started SSM command, CodeBuild
  build or CodePipeline execution ID;
- a one-line offline AWS CLI warning when the command names an unknown
  service/operation/choice, if `botocore` models are installed.

Hints are advisory and never replace or re-run the built-in command. Raw
command values and output are not copied.
