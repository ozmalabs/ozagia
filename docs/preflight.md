# Offline emulator pre-flight foundation

`ozagia.core.preflight.build_preflight` constructs a dry-run plan from
explicit root paths and dependency edges. It topologically orders roots,
rejects cycles, duplicates, and relative/symlink path escapes, and emits argv
arrays for an explicitly named absolute emulator executable. The
`order` argument is only a deterministic tie-breaker among roots that are
ready; it cannot override dependency edges.

Plans use a minimal environment rather than inheriting the caller's
environment: profiles, access keys, session tokens, regions, and unrelated
environment values are not copied. Metadata credentials are disabled and
telemetry-off flags are set. Provider versions are caller-declared notes, not
verified facts.

Building a plan never starts a binary. `execute_preflight` also does not invoke
subprocesses: it requires `allow_execute=True`, the exact explicit binary path,
and an injected runner. This foundation does not contact AWS, Kubernetes, or
any network service, and must not be wired into indexing or normal shell
capture.

An emulator pass is not evidence of real AWS, IAM, Kubernetes, or provider
behavior. Any future executable integration must pin the emulator version,
document provider-version compatibility and covered/uncovered services, and
remain separate from authenticated real-cloud or cluster operations.
