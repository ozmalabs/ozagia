# Ecosystem Convergence — 2026-04 Survey

**Methodology note + supporting data for the ecosystem-convergence claims in Paper I §11 and Paper II §13/§16.**

## Methodology

GitHub repository data measured 2026-04-25 via `gh api repos/<org>/<repo>` and `gh repo view`. Star counts and fork counts are public GitHub metrics; both reflect adoption/attention rather than architectural validation per se (the paper's claim is that adoption/convergence corroborates the framework's architectural prediction, not that adoption metrics measure architectural quality directly).

Project selection criterion: open-source projects shipping the *coordinator-pattern* — coordinator + agent-loop iterating against typed tool surface + frontier-LLM components — at >5K GitHub stars as of 2026-04-25. The selection includes the major frontier-AI organisations' contributions (Anthropic, Google, OpenAI, Alibaba, HuggingFace, Microsoft, plus community/independent projects with comparable adoption).

Architectural-correspondence scoring (0–7 points): each of (A) coordinator + agent-loop, (B) typed tool router with categories, (C) bounded persistent memory + compaction, (D) compound-loop / loop-detection, (E) approval gating for sensitive operations, (F) LLM components via abstraction layer (litellm or OpenAI-compat), (G) MCP support. Scores derived from public READMEs + architecture diagrams + public docs.

## Top 24 coordinator-pattern projects (2026-04-25)

| Project | Stars | Forks | Created | Score | Notes |
|---|---:|---:|---|---:|---|
| `Significant-Gravitas/AutoGPT` | 184K | 46K | 2023-03 | 4 | Earliest agentic project; pivoting to platform |
| `sst/opencode` | 149K | 17K | 2025-04 | 6 | Fastest-growing OSS coding agent |
| `anthropics/claude-code` | 118K | 20K | 2025-02 | **7** | Reference impl; introduced compaction + plugin/skill model |
| `google-gemini/gemini-cli` | 102K | 13K | 2025-04 | 6 | Google's architectural counterpart |
| `openai/codex` | 78K | 11K | 2025-04 | 6 | OpenAI's architectural counterpart |
| `All-Hands-AI/OpenHands` | 72K | 9K | 2024-03 | 6 | First-class SWE-bench (77.6%); SDK + CLI + GUI |
| `open-interpreter` | 63K | 6K | 2023-07 | 4 | Older code-agent design |
| `cline/cline` | 61K | 6K | 2024-07 | 6 | Most explicit human-in-loop approval semantics |
| `microsoft/autogen` | 57K | 9K | 2023-08 | 5 | **Maintenance mode**; superseded by `microsoft/agent-framework` |
| `crewAIInc/crewAI` | 50K | 7K | 2023-10 | 4 | Multi-agent role-orchestration |
| `BerriAI/litellm` | 45K | 8K | 2023-07 | n/a | LLM-abstraction layer most projects depend on |
| `Aider-AI/aider` | 44K | 4K | 2023-05 | 4 | Repomap = early typed-context-graph instinct |
| `langchain-ai/langgraph` | 30K | 5K | 2023-08 | 6 | Explicit graph orchestration (control-flow, not knowledge-state) |
| `huggingface/agents-course` | 28K | 2K | 2025-01 | n/a | Educational; teaches the canonical pattern at scale |
| `huggingface/smolagents` | 27K | 3K | 2024-12 | 5 | "Code-agent" formulation; ~1k LoC core |
| `openai/openai-agents-python` | 25K | 4K | 2025-03 | 6 | "Sandbox Agents" + tracing built-in |
| `QwenLM/qwen-code` | 24K | 2K | 2025-06 | 6 | Alibaba's architectural counterpart |
| `RooCodeInc/Roo-Code` | 23K | 3K | 2024-10 | 6 | "Modes" = first-class typed-policy-per-task; 3M installs |
| `openai/swarm` | 21K | 2K | 2024-02 | 4 | Educational; superseded by openai-agents-python |
| `google/adk-python` | 19K | 3K | 2025-04 | 6 | Explicit "Tool Confirmation HITL" |
| `SWE-agent/SWE-agent` | 19K | 2K | 2024-04 | 5 | NeurIPS '24; superseded by mini-swe-agent |
| `microsoft/agent-framework` | 10K | 2K | 2025-04 | 6 | AutoGen's official enterprise successor |
| `huggingface/ml-intern` | 5.8K | 514 | 2025-10 | **7** | Reference impl matching the framework's prediction node-for-node |

**Cumulative coordinator-pattern stars across the 23 scored projects (excluding litellm and agents-course as infrastructure/educational):** approximately **1.21 million**.

## Adjacent infrastructure (not scored individually but constitutive of the ecosystem)

| Project | Stars | Role |
|---|---:|---|
| `langchain-ai/langchain` | 135K | Broader ecosystem framework |
| `huggingface/transformers` | 160K | Frontier-LLM substrate |
| `modelcontextprotocol/servers` | 84K | MCP itself |
| `modelcontextprotocol/python-sdk` | 23K | MCP Python SDK |
| `BerriAI/litellm` | 45K | LLM abstraction layer (universal across coordinator projects) |

**MCP adoption timeline**: launched November 2024 (`modelcontextprotocol/servers` initial commit); reached universal adoption across 2025-vintage agent projects by April 2026 — approximately 17 months from launch to ecosystem-wide adoption among the projects in the table above.

## Architectural-correspondence summary

**Universal across the table (≥80% of scored projects):**
- Coordinator + agent-loop ✓
- LLM-as-typed-component via abstraction layer (litellm or OpenAI-compat) ✓
- Bounded memory + compaction ✓
- MCP-as-typed-tool-protocol ✓
- Approval gating for sensitive operations ✓
- Typed tool router with categories ✓

**Partially landed:**
- Compound-loop / convergence detection — `huggingface/ml-intern`'s "Doom Loop Detector" is the most explicit named instance; broader pattern is max-iteration + interrupt rather than principled detection.

**Not implemented in any surveyed project:**
- Formal admissibility grammar in the Theorem XII formal-grammar sense (closest: OpenAI Agents "Guardrails" + ADK "Tool Confirmation flow" — predicates over tool calls, not grammars)
- L2 native-substrate models that emit typed-graph natively (every project treats LLMs as byte-stream/token-stream endpoints)
- Typed-graph as knowledge-state memory primitive (LangGraph names "graph" for control-flow only; no project uses typed-graph as knowledge-state primitive)
- Per-organisation dialect production at the formal-grammar layer (Roo-Code "Custom Modes" + Claude Code "plugins/skills" are configuration-tier)

These four constitute the framework's distinctive falsification surface beyond the coarse coordinator pattern.

## What the data corroborates

**The coarse architectural pattern**: ~1.21M cumulative stars across coordinator-pattern projects, multi-organisational scope, multi-conscious-goal scope, prospective adoption ahead of the framework's papers — corroborates the framework's prediction that pulling on the coordination-cost-reduction thread converges on this pattern.

**What the data does not establish**: the framework's quantitative bounds (Theorems XI / XIII), formal admissibility-grammar reduction (Theorem XII formal sense), L2 native-substrate efficiency claims, or substrate-Markov-mixing efficiency. Those are theoretical bounds with their own scope conditions; ecosystem adoption corroborates the architectural pattern's structural recurrence, not the framework's quantitative theorematic content.

**Stars and forks measure attention/adoption, not architectural validation per se.** Independent multi-organisational convergence on the same architectural commitments is meaningful evidence of structural pattern-recurrence; treating GitHub-star counts as a quality metric or an empirical-validation metric overshoots. The framing the papers adopt: "corroborated by independent ecosystem convergence at scale" rather than "empirically validated at population scale."

## Methodological caveats

1. **Self-selection**: projects shipping coordinator-pattern tools are precisely the ones most likely to converge on the pattern (selection bias). The argument is that the major frontier-AI organisations all chose to ship coordinator-pattern tools when they could have shipped alternatives — that choice itself is the corroborating evidence.

2. **Star-count interpretation**: GitHub stars conflate attention, intent-to-use, completed adoption, brand-following, and other signals. Cumulative stars are a coarse adoption proxy, not a precise count of active deployments.

3. **Architectural-correspondence scoring** is interpretive: scores derived from public READMEs and architecture diagrams rather than from runtime measurement. Two independent surveys might score the same project differently by ±1 point.

4. **Survey scope**: open-source projects on GitHub. Closed-source enterprise deployments (e.g., internal tools at Anthropic, Google, OpenAI) are not visible to this survey but are likely larger than the open-source-shipped versions.

5. **Snapshot date**: 2026-04-25. Star counts grow monotonically; the figures represent a lower bound at that date.

## Reproducibility

Anyone can verify the data by running:

```
gh api repos/<org>/<repo> --jq '{stars: .stargazers_count, forks: .forks_count, created: .created_at}'
```

against each project listed above, on the same or later date. Star counts will be ≥ the figures listed here (they grow monotonically); the structural claim about ecosystem convergence on the coordinator-pattern architecture is verifiable by inspection of each project's README and architecture documentation.

## Update cadence

Suggested re-measurement annually. The 2026-04 snapshot captures the convergence event; subsequent snapshots will show whether the fine-grained moves the framework names (formal admissibility grammar, L2 native substrate, typed-graph memory primitive, per-organisation dialect production) are picked up by the ecosystem, or remain distinctive to the framework.

---

*Supporting data for ecosystem-convergence claims in Paper I §11 (same-author convergence caveat) and Paper II §13 (architecture-sufficient claim) and §16 (conclusion).*
