# Resource grants and read-only readiness

A **resource grant** is a named entitlement: which VM, host or cloud account a
task may use, what it may spend, who must approve it and how it is cleaned up.
It is separate from host admission (`ozagia.core.resources`). Admission answers
"does this run fit on this host now?"; a grant answers "is this resource
declared, locally present and allowed?".

This stage is **read-only**. It loads and checks grants. It does not create
claims, write state, resolve secrets, make a network call by default, or run
target code. Claims, release, teardown and task/eval/MCP integration are later
stages.

## Where grants live

Grants are read only from the top-level `[resource_grants.<id>]` table in:

- `ozagia/resources.toml`
- `ozagia/config.toml`

`[resource_grants]` is a config-owned section, so the project-config loader does
not report it as unknown. It is deliberately **not** merged into
`resources.load_policy`, `resources.status` or host admission.

```text
ozagia-resources grants list [--kind K] [--json]
ozagia-resources grants show ID [--json]
ozagia-resources grants check ID [--live] [--probe-versions] [--require-ready] [--json]
```

`list`, `show` and `check` are read-only. They exit `0` for a well-formed
report, including `missing`, `unavailable`, `unknown` and
`requires_approval`. `--require-ready` exits `1` when the grant is not ready.
Invalid arguments, an unknown grant or a malformed config exit `2`. `list`
reports malformed records in a bounded `errors` array instead of hiding the
valid grants, and still exits `0` because it produced a report.

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
  `requires_approval = true`. This stage reports `requires_approval`; it never
  grants approval.

### Cleanup rules

- A cloud grant defaults to `cleanup = "destroy"` and may not use `"none"`.
- `cleanup = "keep"` requires `requires_approval = true` **and** a non-empty
  `risk` note.
- A `host` grant defaults to `cleanup = "none"` because it creates nothing.

### Secrets

Grants may store names, paths, profiles, regions and `secret://...` references.
They may not store keys, tokens, passwords, connection strings, ARNs or inline
`KEY=VALUE` text. A credential-like value is rejected and the error never
echoes the value. `check` never resolves a `secret://` reference and reports
`provenance.secrets_resolved = false`.

## Examples

### Local Windows VM

```toml
[resource_grants.local-windows-vm]
kind = "vm"
provider = "qemu"
description = "Local Windows test VM"
capabilities = ["windows", "kvm"]
permissions = ["admin"]
image = "/var/lib/vms/win10.qcow2"
constraints = { max_hours = 4, max_disk_gb = 120, max_memory_gb = 8 }
cleanup = "destroy"
```

`admin` forces approval. Without an approval the check reports
`requires_approval`; with one it still reports the image, KVM and qemu checks.

### Host that can run VMs

```toml
[resource_grants.kvm-host]
kind = "host"
provider = "qemu"
description = "KVM-capable build host"
capabilities = ["kvm"]
constraints = { max_memory_gb = 16 }
cleanup = "none"
```

A host grant checks capabilities. It never launches a VM and has no image.

### AWS admin with a realistic cap

```toml
[resource_grants.aws-admin-capped]
kind = "cloud"
provider = "aws"
description = "Admin test account, realistic cap, destroy afterwards"
permissions = ["admin"]
region = "us-east-1"
profile = "test-admin"
budget = { max_usd = 50.0, max_hours = 2, max_instances = 4 }
cleanup = "destroy"
requires_approval = true
```

`check` shows the cap and cleanup policy. A found profile is not proof of
permission: `permissions_verified` stays `false` and the check never claims the
principal is an administrator.

### AWS admin with an unbounded budget

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

`admin` and `unbounded` force approval. A later claim needs both an approval and
an explicit unbounded allowance. A read-only check reports
`requires_approval`, which is not readiness for a run.

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
- `permissions` stays declarative. `permissions_verified` is always `false` in
  this stage, including after a live identity check.
- `--live` uses only an injected `live_runner`. With no runner the result is
  `unavailable` with `live.runner_required`, no network call and
  `live_checked = false`. The command-line has no runner yet, so `--live` is
  intentionally unavailable from the CLI in this stage. A runner may report
  `identity_present`; account ids, ARNs and permission claims are discarded.

## What is not verified

- That the principal really has `admin` or any other permission.
- That credentials work, unless a `--live` runner explicitly reports identity
  presence. Even then, presence is not permission.
- That a VM image is a valid image or boots.
- That nested virtualisation is required or enabled for a particular workload.
- Spend, instance count, usage or cleanup. No claim or teardown exists yet.
- Any target command. The check never runs project code.

Every result includes `provenance` (`network`, `wrote`, `secrets_resolved`),
`live`, `live_checked`, `checked_at`, `source` and `not_assessed`. In this stage
`wrote` and `secrets_resolved` are always false; `network` is true only when a
live runner actually ran.

## Later stages

Claims, approvals, release, the claim ledger, orphan/expiry reporting, task and
eval preflight, run-manifest evidence, MCP read-only grant tools and a
`cap:resources.grants` capability are a later stage. Until then a grant is a
declaration and a readiness report, not proof of use.
