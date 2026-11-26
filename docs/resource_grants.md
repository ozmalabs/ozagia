# Resource grants, claims and release

A **resource grant** is a named entitlement: which VM, host or cloud account a
task may use, what it may spend, who must approve it and how it is cleaned up.
It is separate from host admission (`ozagia.core.resources`). Admission answers
"does this run fit on this host now?"; a grant answers "is this resource
declared, locally present and allowed?".

`ozagia resources grants list|show|check` and the MCP tool are **read-only**.
`claim` writes one bounded claim record; `release` previews or applies the
operator's teardown evidence. A claim is not proof that a provider resource was
created, and a release is not proof that the provider actually deleted it.

## Where grants live

Grants are read only from the top-level `[resource_grants.<id>]` table in:

- `ozagia/resources.toml`
- `ozagia/config.toml`

`[resource_grants]` is a config-owned section, so the project-config loader does
not report it as unknown. It is deliberately **not** merged into
`resources.load_policy`, `resources.status` or host admission.

Claims live under the project state directory:

```text
<state>/resources/grants/
  .lock
  <sha256(claim id)>.json
```

The directory is `0700`; record and lock files are `0600`. A record filename is
the sha256 of its validated `clm-<32 hex>` claim id, so no caller-supplied path
or name reaches the filesystem. Writes are atomic (`os.replace` after an
`fsync`) and claim/release take an exclusive `flock`. The ledger is bounded at
`MAX_CLAIMS` (1024) records; a full ledger refuses a new claim rather than
silently deleting history.

## CLI

```text
ozagia-resources grants list [--kind K] [--json]
ozagia-resources grants show ID [--json]
ozagia-resources grants check ID [--live] [--probe-versions] [--require-ready] [--json]

ozagia-resources grants claim ID --reason TEXT [--task T] [--session S]
    [--approve] [--allow-unbounded] [--allow-keep]
    [--max-usd N] [--max-minutes N] [--json]

ozagia-resources grants release CLAIM [--apply]
    [--observed-usd N] [--observed-minutes N]
    [--cleanup done|failed|unknown] [--json]

ozagia-resources grants status [--status active|released|expired|orphaned]
    [--grant ID] [--json]
```

`list`, `show`, `check` and `status` never create state. `claim` and `release`
write only under `<state>/resources/grants/`.

Exit codes:

- `0`: a well-formed report, including `missing`, `unavailable`, `unknown`,
  `requires_approval`, an empty ledger, or a release preview.
- `1`: a refusal (typed code): not ready, missing/unavailable/required
  approval, `grant_in_use`, unknown claim, already released, budget over the
  grant's declared cap, or a full ledger.
- `2`: usage or configuration error: bad argument, malformed id, negative or
  non-finite number, unknown grant, or malformed TOML.

No grant is auto-created by any of these commands.

## Schema

```toml
[resource_grants.local-windows-vm]
kind = "vm"                    # vm, cloud, host, service, container
provider = "qemu"              # qemu, libvirt, aws, azure, gcp, other
description = "Local Windows test VM"
capabilities = ["windows", "kvm"]
permissions = ["admin"]        # observer, user, admin
image = "/var/lib/vms/win10.qcow2"
constraints = { max_hours = 4, max_disk_gb = 120, max_memory_gb = 8 }
cleanup = "destroy"            # destroy, keep, none
requires_approval = false
```

| Field | Meaning |
| --- | --- |
| `id` | The table key. `[A-Za-z][A-Za-z0-9_.-]{0,63}`, unique across both files. |
| `kind` | `vm`, `cloud`, `host`, `service` or `container`. |
| `provider` | `qemu`, `libvirt`, `aws`, `azure`, `gcp` or `other`. |
| `description` | Optional human note. |
| `capabilities` | Named capabilities such as `windows`, `kvm` or `libvirt`. A `host` grant must declare at least one. |
| `permissions` | Declarative `observer`, `user` and/or `admin`. Never verified by this stage. |
| `image` | Required for `vm`; a path to the image. |
| `region` | Optional provider region name. |
| `profile` | Optional provider profile name, never a key. |
| `constraints` | Bounded local constraints (see below). |
| `budget` | `{ max_usd, max_hours, max_instances }` or `{ unbounded = true }`. |
| `cleanup` | `destroy`, `keep` or `none`. Cloud defaults to `destroy`; host defaults to `none`. |
| `requires_approval` | Forced to `true` for `admin` or an unbounded budget. |
| `risk` | Human note; required when `cleanup = "keep"`. |

Constraint keys are closed: `max_hours`, `max_disk_gb`, `max_memory_gb`,
`max_cpus`, `max_instances`, `nested_virt`, `arch`, `os`, `gpu`. Numeric caps
must be positive and finite. Unknown grant fields, unknown constraint fields
and unknown budget fields are rejected.

### Budget rules

- Caps must be positive and finite.
- `unbounded = true` cannot be combined with `max_usd`, `max_hours` or
  `max_instances`.
- A budget with no cap and `unbounded = false` declares no cap. It does not
  mean "unlimited" and it does not mean a default cap was applied.
- `admin` in `permissions` or `unbounded = true` forces
  `requires_approval = true`. A read-only check reports `requires_approval`; it
  never grants approval.

### Cleanup rules

- A cloud grant defaults to `cleanup = "destroy"` and may not use `"none"`.
- `cleanup = "keep"` requires `requires_approval = true` **and** a non-empty
  `risk` note.
- A `host` grant defaults to `cleanup = "none"` because it creates nothing.

### Secrets

Grants may store names, paths, profiles, regions and secret references. They
may not store keys, tokens, passwords, connection strings, ARNs or inline
`KEY=VALUE` text. A credential-like value is rejected and the error never
echoes the value. `check` never resolves a secret reference and reports
`provenance.secrets_resolved = false`.

## Check states

| State | Meaning |
| --- | --- |
| `ready` | Every required local check passed and any required approval was supplied. Permissions are still unverified. |
| `missing` | A required local artifact is absent: `/dev/kvm`, a required qemu/libvirt binary, the VM image or the provider CLI. |
| `unavailable` | It exists but cannot be used: KVM is not readable/writable, the image is unreadable or empty, admission refuses the declared request, the profile section is absent, or a requested live runner is missing/failed. |
| `requires_approval` | The grant declares `admin`, an unbounded budget or `requires_approval`, and no approval was supplied. This takes precedence over local checks. |
| `unknown` | There is no probe for the declared kind or capability (for example an arbitrary host capability or a `service` grant). |

State precedence is `requires_approval`, then `missing`, `unavailable`,
`unknown`, otherwise `ready`. A check row carries `id`, `level`, `message` and
`detail`; a row that changes the state names it in `detail.state`.

### Local VM checks

- `/dev/kvm` is present and `R_OK | W_OK`.
- `qemu-system-x86_64` and `qemu-img` via the injected `which`; `virsh` and
  `libvirtd` are required for a libvirt grant.
- The image path exists, is readable, is a regular file and is non-empty.
  Image format and bootability are not inspected.
- When `constraints` declares `max_memory_gb` or `max_disk_gb`, host admission
  runs through `core.resources.check_admission` with those explicit values.
- Nested virtualisation is reported as `yes`, `no` or `unknown` from
  `/sys/module/kvm_intel/parameters/nested` or `kvm_amd`. It is advisory and
  never fatal.
- `--probe-versions` opts into a bounded `qemu-system-x86_64 --version` or
  `virsh --version` with a short timeout and a minimal environment. It is off
  by default and never runs target code.

### Host checks

A `host` grant checks declared capabilities without an image. A `kvm` (or
`virtualization`/`vm`) capability checks `/dev/kvm`, `qemu-system-x86_64` and
nested-virtualisation evidence. A `libvirt` capability checks `virsh` and
`libvirtd`. An unknown capability is reported as `unknown`, not guessed.
Nothing is launched.

### Cloud checks

- The provider CLI (`aws`, `az` or `gcloud`) is found via `which`.
- A declared region is shown. It is not read from a credentials file.
- AWS config and credentials files are located through the standard paths or
  `AWS_CONFIG_FILE` / `AWS_SHARED_CREDENTIALS_FILE`. Only INI **section names**
  are read; values are never parsed or returned. A declared profile is matched
  by name (`[profile name]` in config, `[name]` in credentials).
- `permissions` stays declarative. `permissions_verified` is always `false`,
  including after a live identity check.
- `--live` uses only an injected `live_runner`. With no runner the result is
  `unavailable` with `live.runner_required`, no network call and
  `live_checked = false`. The command line has no runner, so `--live` is
  intentionally unavailable from the CLI. A runner may report
  `identity_present`; account ids, ARNs and permission claims are discarded.

## Claim semantics

`ozagia-resources grants claim ID --reason TEXT` first runs the read-only
`check_grant` for `ID`. If it is not ready, the claim is refused with the
check's state as a typed code (`missing`, `unavailable`,
`requires_approval`, or `not_ready`). Nothing is written.

A successful claim records:

| Field | Meaning |
| --- | --- |
| `claim_id` | `clm-<32 hex>`, generated for this claim. |
| `grant_id`, `grant_kind`, `provider` | What was claimed. |
| `task`, `session` | Optional caller labels. |
| `reason` | The required human reason. |
| `approval` | Required/granted, `kind`, `flag`, `approved_by: "host"`, `source: "cli"`, `at`, `reason`, grant/claim/task/session, declared permissions, `reusable: false`, and the three explicit flags. |
| `budget` | `max_usd`, `max_minutes`, `max_instances`, `unbounded` and `ttl_minutes`. |
| `started`, `expires`, `created` | UTC ISO timestamps. |
| `observed` | `cost_usd`, `minutes`, `provided`; unknown by default. |
| `cleanup` | Declared `policy`, `status` (initially `unknown`), `verified: false`, `source`, `evidence`. |
| `status` | Stored `active` or `released`; views compute `expired`/`orphaned`. |
| `released`, `release` | Set only by an applied release. |

### Approval is per claim

`--approve` is an explicit host action. It is **never** inferred from a
profile, a previous claim, a task, a session or the grant's own
`requires_approval` field. Merely reading a grant does not approve it.

- A grant with `requires_approval` needs `--approve`.
- A grant with `permissions = ["admin"]` **or** `budget.unbounded = true` needs
  **both** `--approve` and `--allow-unbounded`.
- A grant with `cleanup = "keep"` needs `--approve` **and** `--allow-keep`.
- A reason is always required. It is bounded, must not contain control
  characters, and is rejected if it looks like a credential.

Approval is not reusable across grants or sessions: it is stored in the claim,
names its grant, task and session, and carries `reusable: false`. A plain CLI
has no user identity, so the record says `approved_by: "host"` and
`source: "cli"`; it does not invent a person.

### Budget and claim TTL

The claim budget is the narrower of the caller's request and the grant's
declared caps:

- `max_usd`: `--max-usd`, or the grant's `budget.max_usd`. A request above the
  declared cap is refused with `budget_exceeds_grant`.
- `max_minutes`: `--max-minutes`, or the grant's `budget.max_hours` /
  `constraints.max_hours` converted to minutes. The narrowest cap wins.
- `max_instances`: the grant's declared value.
- `unbounded`: copied from the grant, and only claimable with both approval
  flags.
- `ttl_minutes`: the claim's time cap, or `DEFAULT_CLAIM_TTL_MINUTES` (240)
  when none is declared, capped at seven days.

`expires = started + ttl_minutes`. The TTL is a reporting/expiry bound; it does
not stop a process or delete a provider resource.

### One claim per grant

At most one unreleased claim may exist per grant. A second claim while one is
active, expired or orphaned is refused with `grant_in_use`, including the
existing claim id and state. The operator must release the existing claim
first. The check and write happen under one exclusive ledger lock, so
concurrent attempts have exactly one winner.

### Expiry and orphans

Expiry is computed, never auto-applied. A stored `active` claim whose
`expires` time has passed is reported as:

- `status: "orphaned"` (resources may still exist),
- `expired: true` with `expires_in_s: 0`,
- `orphaned: true`.

It is counted as both `expired` and `orphaned`, listed by
`grants status` and by `claims(status="expired"|"orphaned")`, and is never
hidden or deleted. Teardown remains the operator's action. Releasing an expired
or orphaned claim is explicit and records the previous state in
`release.previous_status`.

## Release semantics

`release CLAIM` is a **preview** by default. It reads the ledger and reports
what would be closed, with `dry_run: true`, `applied: false`,
`would_release`, and the proposed observed/cleanup values. It writes nothing.

`release CLAIM --apply` closes the claim:

- `status` becomes `released`; `released` and `release.at` are recorded.
- `release.by: "host"` and `release.source: "cli"`; no invented user identity.
- `release.previous_status` preserves `active`, `expired` or `orphaned`.
- The record is kept. No claim file is deleted or auto-pruned.
- Releasing an already released claim is refused with `claim_released`.

### Cleanup honesty

`--cleanup` accepts only `done`, `failed` or `unknown` and defaults to
`unknown`. It is the **caller's evidence**, not something Ozagia verified:

- `cleanup.status` is exactly what the caller supplied.
- `cleanup.verified` is always `false`; Ozagia did not inspect the provider.
- `cleanup.source` is `"caller"` when a status or evidence was supplied,
  otherwise `null`.
- `cleanup.evidence` is the caller's bounded note, or `null`.

A release without `--cleanup` reports `unknown`. It never pretends cleanup
succeeded.

### Observed usage

`--observed-usd` and `--observed-minutes` are recorded only when supplied;
otherwise `observed.cost_usd` / `observed.minutes` stay `null` and
`observed.provided` is `false`. Negative or non-finite values are rejected.
Ozagia never infers AWS spend or runtime from local measurements.

## Read-only status

`ozagia status` includes a compact `grants` section. It reads existing
declarations and claim records only; it does not claim, release, probe a grant,
start a daemon or create the ledger. It reports:

- declared grant count and bounded config errors,
- `ledger.present`,
- claim counts: `active`, `released`, `expired`, `orphaned`, total, and
  cleanup `done`/`failed`/`unknown`,
- `readiness.checked: false` with the reason that status does not probe grants.

`ozagia-resources grants status` is the detailed view:

```json
{
  "ok": true,
  "schema": 1,
  "now": "2026-11-26T00:00:00+00:00",
  "count": 1,
  "counts": {
    "total": 3,
    "active": 1,
    "released": 1,
    "expired": 1,
    "orphaned": 1,
    "cleanup": {"done": 1, "failed": 0, "unknown": 2}
  },
  "claims": [],
  "errors": [],
  "read_only": true
}
```

`claims()` and `claim_status()` never create the ledger. A missing ledger is an
empty report, not an error; a malformed record is skipped and named in the
bounded `errors` list.

## Read-only MCP surface

`dev_resource_grants` (full/coding surface only) exposes:

- declared grants with kind, provider, declared permissions, budget, cleanup
  and approval requirement,
- claim counts and bounded claim rows, optionally filtered by `grant_id` or
  `claim_status`,
- `read_only: true`, `network: false`, `secrets_resolved: false`,
  `wrote: false`.

There is no MCP claim, approve or release tool. The tool never starts a daemon,
probes capabilities, calls the network or returns a credential value. Expiry
and cleanup are reported, not changed.

## Capability and evidence

`cap:resources.grants` declares the grant checker and claim ledger.
`cap:status.inspect` declares the `status:grants` section. Claim records are
metadata: grant id, approval flags, reason, budget, timestamps, observed values
and cleanup evidence. They contain no credentials, resolved values or provider
account identifiers.

## Examples

### Claim a local VM

```text
ozagia-resources grants claim local-windows-vm \
  --reason "reproduce the installer bug" \
  --task "T-142" --session "opencode-7"
```

### Claim a capped AWS admin account

```text
ozagia-resources grants claim aws-admin-capped \
  --reason "run the cloud integration test" \
  --approve --allow-unbounded \
  --max-usd 20 --max-minutes 45
```

The claim carries the narrower cap (`20 USD`, `45 minutes`) and a 45-minute
TTL. `--approve` without `--allow-unbounded` is refused.

### Release with evidence

```text
ozagia-resources grants release clm-0123456789abcdef0123456789abcdef
ozagia-resources grants release clm-0123456789abcdef0123456789abcdef \
  --apply --cleanup done --observed-usd 12.40 --observed-minutes 38
```

The first command is a preview. The second records the caller's cleanup
evidence and observed usage; it does not claim Ozagia verified the teardown.

### See orphans

```text
ozagia-resources grants status --status orphaned
```

An expired unreleased claim is listed here with its grant, budget and cleanup
state. It is not deleted, and no new claim for that grant is allowed until the
operator releases it.

## What is not verified

- That the principal really has `admin` or any other permission.
- That credentials work, unless a `--live` runner explicitly reports identity
  presence. Even then, presence is not permission.
- That a VM image is a valid image or boots.
- That nested virtualisation is required or enabled for a particular workload.
- That a provider resource was created by a claim.
- That cleanup actually deleted a provider resource, or the exact spend.
- Any target command. No grant command runs project code.

## Task, eval and harness integration

A task declares grants in its `[resources]` table:

```toml
[resources]
grants = ["local-windows-vm", "aws-admin-capped"]
```

The list is bounded at 16 ids. Ids must match
`[A-Za-z][A-Za-z0-9_.-]{0,63}`; duplicates, non-string entries, a non-list
value and an id that is not declared in `ozagia/resources.toml` or
`ozagia/config.toml` are task-validation errors. Validation checks only that
the id exists. A spec with no grants omits the `grants` field from its canonical
form, so its digest does not change.

Preflight checks every declared grant read-only with `check_grant` and the
host's approval flags. It never claims during validation or preflight. A
missing, unavailable, `unknown`, `requires_approval` or unapproved grant adds a
typed failure (`missing`, `unavailable`, `requires_approval`, ...) and the run
refuses before the executor is called.

Before any work the host claims each grant in declaration order with
`resource_grants.claim`. The claim reason names the task id and run/session,
then the host budget. The task's `budget.cost_usd` and the shorter
`budget.wall_s`/`resources.wall_s` supply the claim's `max_usd`/`max_minutes`
unless the host passes an explicit `grant_budget`. Each claim is one per grant;
an existing active, expired or orphaned claim still refuses with
`grant_in_use`. If a later claim fails, the earlier claims are released in
reverse order and the run refuses.

Every claim is released in `finally` (also on an exception or timeout) with
`release(..., apply=True)`. The task runner has no provider observations, so
the release records `observed: {cost_usd: null, minutes: null, provided:
false}` and `cleanup: unknown`; it never invents spend or teardown. A release
failure is an explicit warning and the claim stays in `unreleased`.

Approval comes only from the host invocation. For tasks:

```text
python -m ozagia.tasks_cli run SPEC --allow-exec \
  --approve-grant --allow-unbounded-grant --allow-keep-grant \
  --grant-reason "run the integration suite"
```

`--approve-grant` is required for a grant with `requires_approval`.
`--allow-unbounded-grant` is also required for `admin` or an unbounded budget,
and `--allow-keep-grant` for `cleanup = "keep"`. The task file and the model
cannot grant approval.

For the eval and OpenCode harness, `--resource-grant ID` is repeatable:

```text
python -m ozagia.eval.agent run --resource-grant aws-admin-capped \
  --approve-grant --allow-unbounded-grant
ozagia agent --resource-grant local-windows-vm --approve-grant --prompt "..."
```

The eval claims once for the whole run and the harness claims before launching
OpenCode; both release in `finally`. A grant failure exits 2 / raises
`AgentError` or `HarnessError` before any task, worktree or session starts; it
is never turned into a successful run. With no `--resource-grant`, results and
manifests have no `resource_grants`/`grants` field and behave as before.

Run manifests (task) and run results (eval and harness) record the same
non-secret fields: declared grant ids, claim ids, approval kind/reason and
flags, claim budget, observed cost/time (or `null`/unknown), cleanup policy and
status, release outcome, `unreleased` claim ids, and warnings. Grant reports
carry ids and non-secret names only; no credential value is read, stored or
passed to the model.

## What remains

- `ozagia resources run --grant ID -- <cmd>` is not added. It can come only
  after the claim/release ledger already used by tasks: it must set non-secret
  environment values, enforce the wall budget and release on exit.
- Live provider probes (`--live` identity/permission checks) remain opt-in and
  injected; a task, eval or harness run does not make one.
- Permission claims and observed spend remain unverified and unknown unless a
  caller supplies evidence. A model still cannot claim, approve or release a
  grant through MCP.
