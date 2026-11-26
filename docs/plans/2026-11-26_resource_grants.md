# Resource grants and test entitlements

Status: active plan (2026-11-26). Separate from host admission
(`core.resources`) and temporary space. A resource grant describes **what a
task is allowed to use**, who approved it, what it may cost, and how it is
torn down.

## Problem

`ozagia resources` can say whether memory, disk, pids and wall time fit on
this host. Task specs can declare those limits and token/cost budgets. That is
not enough for real testing entitlements such as:

- a local Windows VM image and a host that can run it;
- a host that can run arbitrary VMs (KVM/qemu, nested virtualisation);
- a cloud account with administrator permission and an unlimited budget;
- a cloud account with administrator permission but a realistic spend/time
  budget and mandatory teardown.

A model or test harness currently has no first-class, auditable way to say
which of those it is using, whether it is actually available, what it is
allowed to spend, or whether the resources were released.

## Goals

- Named **resource grants** with kind, provider, capabilities, permissions,
  budget, lifetime, cleanup policy and approval requirements.
- Read-only readiness checks for local VM capability and cloud credentials
  without printing secrets or making network calls by default.
- A claim/release ledger recording session/task, grant, approval, budget,
  observed usage, cleanup status and evidence.
- Task and eval integration: a task can require grants; preflight refuses when
  they are missing; the run manifest records the grant, spend and teardown.
- An explicit gate for `admin` permissions and `unbounded` budgets. Neither is
  available to a model by default; both require a named grant, a human
  approval and a recorded reason.
- Cloud grants require a teardown policy; orphaned/expired claims are reported.

## Non-goals

- A general cloud provisioner or Terraform replacement.
- Creating IAM users, roles or credentials.
- Storing cloud credentials or VM passwords in Ozagia config or state.
- Letting a model self-authorize administrator or unbounded spend.
- Proving that a cloud principal really has administrator permission without
  an explicit live check (permission is reported as unknown, not guessed).

## Resource grant schema

Declared in `ozagia/resources.toml` or `[resources]` in `ozagia/config.toml`
under `[resource_grants.<id>]`, or in a task's `[resources.grants]` list by id.

```toml
[resource_grants.local-windows-vm]
kind = "vm"                    # vm, cloud, host, service, container
provider = "qemu"              # qemu, libvirt, aws, azure, gcp, other
description = "Local Windows test VM"
capabilities = ["windows", "kvm"]
permissions = ["admin"]        # observer, user, admin
image = "/var/lib/vms/win10.qcow2"
constraints = { max_hours = 4, max_disk_gb = 120, max_memory_gb = 8 }
cleanup = "destroy"
requires_approval = false
```

```toml
[resource_grants.aws-admin-capped]
kind = "cloud"
provider = "aws"
description = "Admin test account, realistic cap, destroy afterwards"
permissions = ["admin"]
region = "us-east-1"
profile = "test-admin"        # profile name, never a key
budget = { max_usd = 50.0, max_hours = 2, max_instances = 4 }
cleanup = "destroy"
requires_approval = true
```

```toml
[resource_grants.aws-admin-unbounded]
kind = "cloud"
provider = "aws"
permissions = ["admin"]
budget = { unbounded = true }
cleanup = "destroy"
requires_approval = true
risk = "unbounded spend; explicit human approval and reason required"
```

Rules:

- `admin` permission or `budget.unbounded = true` requires
  `requires_approval = true`, an explicit `--approve` on claim, and a reason.
  `unbounded` additionally requires `--allow-unbounded`; without both, claim is
  refused.
- Cloud grants must declare `cleanup = "destroy"` unless an explicit
  `--allow-keep` and approval are supplied.
- `profile`, region, image paths and environment variable **names** may be
  stored. Keys, tokens, passwords and connection strings may not; use a
  `secret://` reference or the provider's own credential chain.
- A grant is a capability, not proof of use. A claim records the actual task,
  session, approval and observed budget.

## Stages

### A — inventory and schema

Inventory current host admission, task budgets, actions/readiness, secret
references, environments, cgroups, evaluator and harness result shapes.
Identify where a grant can be checked, claimed and recorded without violating
the read-only and no-target-import rules. Produce the final schema and the
list of readiness probes (local-only versus explicitly live).

Acceptance: a written inventory with exact functions, config keys, result
fields and the places a grant must be enforced.

### B — core grants and CLI

Add `src/ozagia/core/resource_grants.py` and extend `ozagia resources`:

- `ozagia resources grants list|show ID|check ID` — read-only. `check` reports
  `ready`, `missing`, `unavailable`, `requires_approval` or `unknown`, with
  per-check detail. It never prints a secret and does not call the network
  unless `--live` is explicit.
- Local VM/host checks: `/dev/kvm`, qemu/libvirt executable, image path,
  memory/disk admission, nested-virt evidence if available. Absence is
  `missing`, not a failure of the command.
- Cloud checks: provider CLI presence, profile/config presence, region,
  identity **presence** (not values). A live `sts get-caller-identity` or
  permission probe is `--live` and records that it was checked.
- `ozagia resources claim ID --reason TEXT [--approve] [--allow-unbounded]
  [--allow-keep]` creates a bounded claim in `<state>/resources/grants/`;
  `release CLAIM [--apply]` records cleanup; an expired unreleased claim is
  reported as orphaned.
- A claim includes grant id, task/session, started/expires, approval
  kind/reason, budget, observed spend, cleanup policy and status. It contains
  no credentials.
- `ozagia resources grants status` lists active, expired and orphaned claims.
- JSON is stable; refusals exit 1; no grant is auto-created.

Acceptance: a local VM grant reports ready only when the image and KVM/qemu are
present; an AWS grant with `max_usd` shows the cap and never claims
administrator just because a profile exists; an unbounded grant is refused
without both approval flags.

### C — task, eval and harness integration

- Task specs may declare `[resources] grants = ["local-windows-vm"]` and a
  cost/time budget. Validation checks the grant id; preflight checks readiness
  and approval; the runner claims before work and releases in `finally`.
- A missing/unready grant refuses the task with a typed check; it does not
  silently run with less capability.
- The run manifest records grant ids, claim id, approval reason, budget,
  observed cost/time and cleanup status. An unreleased claim is an explicit
  warning.
- The eval/agent harness accepts `--resource-grant ID` (repeatable), claims
  before the run, releases after, and reports the same fields. It never passes
  credentials to the model; only non-secret names/paths and the grant id.
- MCP exposes read-only `dev_resource_grants` (list/status); no claim/approve/
  release tool by default. A model cannot self-authorize admin or unbounded.
- A `ozagia resources run --grant ID -- <cmd>` convenience wrapper may be
  added only after the claim/release ledger exists. It sets non-secret env,
  enforces the wall budget and releases on exit.

Acceptance: a task requiring `local-windows-vm` refuses on a host without KVM;
a capped AWS grant records its budget and releases; an admin/unbounded grant
without approval never starts.

### D — guardrails, teardown and evidence

- Approval records: who/when/why, grant, task/session, permissions, spend cap
  and cleanup policy. Approval is not reusable across grants or sessions.
- Cleanup verification: after `release`, check that the claim is closed and
  report `cleanup: done|failed|unknown`. An orphan/expired claim is surfaced
  in `resources status` and the task manifest; it must not be hidden.
- Budget accounting: record observed cost/time/instances where the caller
  supplies them; unknown stays unknown. Never infer AWS spend from local
  measurements.
- Secret handling: grant config and claims contain names/refs only; resolved
  credentials stay at the child/provider boundary and are redacted from logs.
- Capabilities/status docs: `cap:resources.grants`; `ozagia status` may show
  active/orphaned claim counts without network or credential reads.
- A kill/expiry policy is explicit: an expired claim is reported, not silently
  deleted, and its teardown remains the operator's action.

## Acceptance examples

1. **Local Windows VM** — `grants check local-windows-vm` reports image,
   `/dev/kvm`, qemu and memory/disk readiness; if the image is absent it is
   `missing`, not an exception.
2. **Arbitrary local VMs** — a `host` grant with `capabilities = ["kvm"]`
   reports whether the host can run VMs; it does not launch one.
3. **AWS admin, unlimited budget** — a grant with `unbounded = true` and
   `admin` is never ready by default; claim requires `--approve` and
   `--allow-unbounded` and records the reason. A live permission check is
   optional and marked `unknown` when not run.
4. **AWS admin, realistic cap and teardown** — `max_usd`, `max_hours`,
   `max_instances`, `cleanup = "destroy"` are shown in `check`; a task claim
   records them; release reports cleanup done/failed/unknown and an unreleased
   claim is an orphan.

## Safety rules

- No credential, token, password or connection string in config, registry,
  claims, logs or model context.
- No network in list/show/check by default; `--live` is explicit and records
  that it ran.
- No model-initiated claim, approval or unbounded spend.
- No silent downgrade: a task requiring a grant refuses rather than running
  with fewer permissions.
- Cloud/admin grants require an explicit cleanup policy; `keep` needs a
  separate approval.
- Unknown permissions/usage remain unknown; never report a grant as
  administrator or a spend as exact without evidence.
