# Versioned service envelopes (WP2)

Status: implemented in `src/ozagia/core/services.py`, 2026-11-17. This is the
M2 service envelope. It is a pure, stdlib-only library: no file, network,
process, model or clock observation is made except the optional monotonic
default used to enforce a caller-owned deadline. Nothing in this change adds a
daemon operation, MCP tool, graph write or transport.

## Shape

```json
{
  "schema": {"namespace": "ozagia.service", "version": "1.0"},
  "service": "task_preflight",
  "version": "1.0",
  "status": "ok",
  "data": {},
  "error": null,
  "provenance": {"sources": [], "inputs": []},
  "side_effect": "read",
  "started_at": 1700000000000,
  "duration_ms": 12,
  "truncated": {"truncated": false, "omitted": 0,
                "next_cursor": null, "reason": null}
}
```

| Field | Rule |
| --- | --- |
| `schema` | exactly `{"namespace": "ozagia.service", "version": "1.0"}`; another major is refused |
| `service` | lowercase dotted `[a-z0-9_.-]`, at most 128 characters |
| `version` | the service's own `major.minor`; v1 accepts only major `1` |
| `status` | `ok` or `error` |
| `data` | canonical JSON value or `null`; at most 192 KiB |
| `error` | `null` for `ok`; a validated `contracts.ServiceError` for `error` |
| `provenance` | `{"sources": [...], "inputs": [...]}`, sorted, bounded |
| `side_effect` | a WP1 capability side-effect class: `none`, `read`, `write`, `execute`, `network`, `model` |
| `started_at` | caller-injected integer or bounded string, or `null`; never invented |
| `duration_ms` | caller-injected non-negative integer, or `null`; never invented |
| `truncated` | the WP0 truncation envelope (`truncated`, `omitted`, `next_cursor`, `reason`) |

`status` and `error` must agree: `status="error"` carries an error; any other
status carries none. `x-` envelope fields are preserved extensions; any other
unknown field is refused.

## Typed errors

`contracts.ServiceError` is the only error vocabulary. Its codes are the
contracts service codes (`invalid_request`, `invalid_value`, `unknown_field`,
`unsupported_version`, `not_found`, `unavailable`, `unauthorized`,
`forbidden`, `conflict`, `stale`, `unknown`, `timeout`, `cancelled`,
`budget_exceeded`, `truncated`, `unsupported`, `invalid_state`,
`precondition_failed`, `internal`, and the listed input errors). A free-text
code is rejected with `contract.invalid_service_error`. Messages are one
bounded line (at most 512 characters); details are canonical and at most 4 KiB.

`services.fail(code, message)` raises `ServiceFailure`, which carries the
validated error. `services.call` converts that to an error envelope. Any other
exception becomes an `internal` error whose message names only the exception
type, so an exception message cannot leak a secret into an envelope.

## Side-effect classes

The envelope reuses the WP1 catalogue vocabulary rather than defining a second
one. `side_effect` describes the *operation the caller wrapped*, not the
envelope construction:

- `none`: pure computation, no repository or host observation;
- `read`: read-only file/status/graph access;
- `write`: a caller-authorized state or repository write;
- `execute`: starts a process or language server;
- `network`: opens a socket or fetches a remote resource;
- `model`: spends model tokens.

An envelope does not grant permission. The existing authorization for the
underlying owner remains authoritative.

## Timeouts

`services.Deadline` is caller-owned and measured on an injected monotonic
clock:

```python
deadline = services.Deadline.after(2_000, clock=my_clock)
envelope = services.call("check.outcome", operation, deadline=deadline,
                         side_effect="execute")
```

- The deadline is checked before the operation starts. An expired deadline
  returns a `timeout` envelope and **does not call the operation**.
- The operation runs once. `call` has no retry path; a retry that could repeat
  a side effect is the caller's decision, with the caller's idempotency key.
- A deadline cannot interrupt a blocking operation. If the operation returns
  after an overrun, its result is kept and the measured `duration_ms` shows the
  overrun. There is no second call and no fabricated timeout for completed
  work.
- `timeout_ms=0` is valid and refuses to start.
- The default clock is `time.monotonic_ns() // 1_000_000`. A test supplies a
  fake clock. `started_at` is the injected clock value; it is not claimed to be
  wall-clock time.

## Provenance

`provenance` is a bounded, sorted mapping:

```json
{"sources": [
   {"source": "ozagia.core.goals", "observed_at": 1700000000000,
    "freshness": "current", "evidence": "observed"}
 ],
 "inputs": ["sha256:..."]}
```

Sources use the contract freshness (`current`, `stale`, `unknown`, `absent`)
and evidence (`declared`, `inferred`, `observed`, `verified`) vocabulary. At
most 32 sources and 64 inputs are accepted. Inputs are references or hashes,
never secret values. `services.Source` and `services.provenance(...)` build the
mapping.

## Canonical form and hashing

The envelope is canonical JSON in the WP0 sense: no floats, only signed 64-bit
integers, sorted keys, compact separators, UTF-8, no trailing newline.
`envelope_json` returns that text; `envelope_hash` is SHA-256 over it;
`envelope_size` is its UTF-8 byte length. `started_at` and `duration_ms` are
part of the hash because the caller supplied them; a caller that needs a
stable hash supplies stable values.

## Constructors

- `build(...)`: validate and return an envelope mapping.
- `ok(service, data, ...)`: an `ok` envelope.
- `failed(service, error, ...)`: an `error` envelope.
- `call(service, operation, ..., deadline=...)`: bounded invocation.
- `validate(record)`: validate and normalize an existing envelope.
- `envelope_json`, `envelope_hash`, `envelope_size`.
- `Source`, `provenance`, `Deadline`, `ServiceFailure`, `fail`.

`task_preflight.preflight_envelope(report)` is the first intended producer: it
wraps a preflight report in an `ok` envelope with `side_effect="read"` and the
report's hash as an input. The CLI prints the report; a future model-facing
surface can carry the envelope without changing the report.

## Bounds and refusals

- `data` 192 KiB canonical; envelope 256 KiB canonical.
- Service name 128 characters; error message 512 characters; error details
  4 KiB.
- Unknown fields and unsupported major versions are refused, not ignored.
- Floats, NaN, infinities, bytes, tuples, sets and out-of-range integers are
  refused by the contract canonicalizer with their specific error codes.

## Not implemented here

- No transport (JSON-RPC, MCP, daemon wire, HTTP) is added.
- No automatic retry, idempotency store, cancellation token or crash recovery.
- No durable service-result record; the envelope is a value, not a store.
- No permission enforcement: the owner's existing gates remain authoritative.
- The default monotonic clock is the only ambient read; callers that need
  complete determinism inject their own clock.
