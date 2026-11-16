# Offline live-state snapshots

`ozagia.core.live` imports caller-supplied snapshot JSON. It has no live
provider, network access, graph mutation, or disk I/O. Provenance is assigned
by the importer with `import_snapshot(text, provenance=...)`; it is not trusted
from the artifact.

Schema version 1 accepts only:

- `schema_version`, timezone-aware ISO `observed_at`, `environment`, `service`,
  and optional AWS-style `region`;
- a bounded `deployed_images` array with each image's `name` and optional
  `tag`/SHA-256 `digest` (at least one of tag or digest);
- non-negative `desired_replicas` and `ready_replicas`;
- `target_health`, containing only `status` (`healthy`, `unhealthy`,
  `degraded`, or `unknown`) and/or numeric `healthy`, `unhealthy`, `total`;
- optional `last_pipeline` with exactly `name`, `status`, and timezone-aware
  `at`.

Unknown fields, duplicate JSON keys, unsafe identifiers, unsupported versions,
invalid timestamps, more than 64 KiB, excessive JSON nesting, and oversized
collections are rejected. Secrets, environment values, arbitrary resource
properties, and nested metadata are not part of this schema. Import errors
avoid echoing the supplied document.

`diff_snapshots` compares one snapshot or bounded collections, keyed by
environment/service. It reports added, missing, or changed records and changed
field names only; it does not include old/new values. `is_stale(snapshot,
now=..., ttl=...)` compares the observation time with an explicit aware clock
and TTL. `write_snapshot(directory, snapshot)` atomically stores the allowlisted
artifact as `<environment>.<service>.json`; callers choose the directory (the
CLI and MCP tool use `.ozagia/live/`). `read_snapshot(path, provenance=...)`
requires provenance again rather than trusting a value from the file. There is
no implicit live read or provider in this module.

A future provider would need a separate explicit opt-in, read-only allowlist,
output/pagination bounds, one observation per run, named scope, and audit
record. No provider is implemented here.
