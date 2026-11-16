# Offline-safe job waiting

`ozagia.jobs` provides a read-only polling primitive for a named SSM command
invocation, CodeBuild build, CodePipeline execution, or Kubernetes deployment
rollout. It is a library API only; it does not start jobs or run commands on
its own.

## Safety and live gate

- No provider call is made by default. `wait_for_job` needs an explicit
  `JobPolicy` (or the strict mapping accepted by `job_policy_from_config`).
  The default policy has no providers or executables enabled.
- A live trial requires explicit human approval and configuration for the
  named environment and AWS region or Kubernetes namespace. Do not select a
  profile, context, account, namespace, or environment implicitly.
- Only the reviewed polling shapes are accepted. AWS calls are
  `ssm get-command-invocation`, `codebuild batch-get-builds`, and
  `codepipeline get-pipeline-execution`. Kubernetes uses
  `kubectl rollout status` with `--watch=false`. No send, start, apply,
  delete, patch, or other write operation is implemented.
- The configured executable must be `aws` for AWS providers or `kubectl` for
  Kubernetes. Arguments are constructed as arrays; shell strings and arbitrary
  extra arguments are rejected.
- Timeouts are capped by the configured `max_timeout_s`; polling uses
  bounded exponential backoff and a monotonic deadline. The injected runner
  must honor its per-call timeout.
- Raw stdout, stderr, environment variables, tokens, credentials, full logs,
  and arbitrary response metadata are never returned. `last_log_lines`
  contains only normalized status observations, not provider log contents.
- Waiting is independent of graph work and holds no graph lock.

Example policy shape (not enabled unless deliberately supplied):

```python
{
    "allowed_providers": ["codebuild"],
    "executables": {"codebuild": "aws"},
    "max_timeout_s": 600,
}
```

The caller is responsible for enforcing the separate approval and named
environment/region gate before supplying a live runner. Tests use fake
runners only. This feature makes no token- or time-savings claim.
