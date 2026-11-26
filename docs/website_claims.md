# Website claim map

This file gives Ozagia's repository graph a source-side index of claims on the
separate ozagia.com site. The website-side anchor and trigger map lives in
`website/ozagia/claims.json` in the `ozmalabs/website` repository. Keep the IDs
below synchronized with that manifest when site claims or sources change.

During release review, query the graph for the changed source files and these
claim IDs, then check the linked website anchors manually. Ozagia's graph can
find relevant source/docs and their code relationships; it does not parse the
website's HTML claims or prove whether marketing wording is accurate.

| Claim ID | Website location | Ozagia sources that establish or constrain the claim |
| --- | --- | --- |
| `release-status` | `website/ozagia/public/index.html#status` | [Roadmap](roadmap.md), [integration matrix](integrations.md), [package version](../pyproject.toml) |
| `install-platforms` | `website/ozagia/public/index.html#install` | [Install guide](install.md), [release procedure](release.md), [installer](../install) |
| `opencode-plugin` | `website/ozagia/public/index.html#how` | [README](../README.md), [intent and capture](intent.md), [integration matrix](integrations.md), [OpenCode plugin](../src/ozagia/integrations/opencode/plugin.js) |
| `research-tools` | `website/ozagia/public/index.html#research` | [Research guide](research.md), [feature catalogue](../FEATURES.md), [roadmap](roadmap.md) |
| `iac-static-analysis` | `website/ozagia/public/index.html#infra` | [IaC context](iac_context.md), [SRE operations](sre_operations.md), [roadmap](roadmap.md) |
| `agent-pilot-results` | `website/ozagia/public/index.html#results` | [Pilot v3](results/ocelot_agent_pilot_v3.md), [pilot v4](results/ocelot_agent_pilot_v4.md), [roadmap evidence assessment](roadmap.md#what-the-evidence-actually-supports) |
| `integration-maturity` | `website/ozagia/public/index.html#use` | [Integration matrix](integrations.md), [README](../README.md), [roadmap](roadmap.md) |
| `deep-review-quality` | `website/ozagia/public/index.html#use` | [Deep review guide](deep_review.md), [roadmap evidence assessment](roadmap.md#what-the-evidence-actually-supports) |
| `feature-links` | `website/ozagia/public/index.html#use` | [Profiling](profiling.md), [federation](federation.md), [toolchain discovery](toolchain.md), [feature catalogue](../FEATURES.md) |

## Release review

For every tag, inspect the source changes since the previous release against
this table and the website manifest. Update the corresponding site section or
record a reviewed no-copy-change decision. Newly shipped user-facing functions
must have a direct link from the site to their documentation or release notes.
Record the reviewed tag, disposition, reviewer and deployed URL check in the
website release PR. This is required by [the release procedure](release.md).
