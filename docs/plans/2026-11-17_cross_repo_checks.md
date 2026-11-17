# Cross-repository checks and linked checkouts

## Goal and research

Make linked repositories usable together automatically, and check declared API and code/infrastructure relationships across existing indexed snapshots. Retain source-repository provenance and avoid turning an absent snapshot or a guessed relationship into a passing check. [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1) specifies operation identity, paths and parameters; [Pact's provider verification](https://docs.pact.io/implementation_guides/python/docs/provider) illustrates the distinction between a recorded contract and runtime verification. The first implementation is offline static evidence checking, not a substitute for actual contract or deployment tests.

## Scope

1. Extend the consumer-owned federation manifest with explicit member paths, relative to its anchor where appropriate. Combine these with automatic indexed-sibling discovery; preserve explicit CLI/MCP selection and existing links. Report missing/invalid members and do not import or execute target projects, build other snapshots, or write to linked repositories.
2. Add a read-only cross-repository report using selected existing snapshots and explicitly declared links. Validate endpoints, relation and expected node types; provide API producer/consumer/test coverage for linked `ApiOperation` nodes and code-to-IaC deployment relationships, with provenance. Expose missing and ambiguous evidence, stale or unverifiable snapshots, and unsupported checks as unknown rather than verified success. Never assert that API schema, runtime behavior or a deployment is compatible merely because a link exists.
3. Provide CLI and MCP entry points, document manifest syntax, examples, boundaries and future extensions. Build realistic multi-repository tests covering linked checkouts, API contracts, code/IaC declarations, invalid and missing inputs, collision/freshness and no writes.
4. Support explicitly designated reference repositories that update from origin by default unless pinned. Preserve local changes and branch history; use safe fast-forward-only updates, bounded noninteractive Git operations and explicit results. Automatically surface reference and index freshness to the agent, including failures and uncertainty. Ordinary federation members are not implicitly designated as references. Automatic updates are an intentional exception to the otherwise read-only federation behavior.
5. Review the work in isolated worktrees, merge it, run focused and full checks where practical, and report results and limitations.

## Boundaries and follow-on

Static source facts are not deployment state or provider verification. Version-aware dependency resolution, OpenAPI schema compatibility, executing consumer/provider tests, runtime state and inferred links need additional evidence and separate work; do not represent them as implemented. Member paths and declared links can disclose repository metadata to an MCP client; document that boundary and preserve explicit selection.

## Implemented slice

The report compares explicitly linked OpenAPI operation method/path identities and declared service attributes; producer, consumer and test links remain declarations. A separate configuration observation compares literal Python `os.environ[...]` reads against a linked Kubernetes workload's declared environment names without returning values. Unsupported or dynamic cases are unknown. Federation reports do not currently certify working-tree freshness, so the report preserves observed comparisons while marking the overall result unknown when freshness is unverified. This is not execution of application or IaC tests.
