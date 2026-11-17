# Ozma-Coder-1: Native Protocol Token Report

**Extends:** `openclaw-full-stack-report.md`  
**Dataset:** `sammshen/wildclaw-opus-traces` — same 101 turns, 6 tasks  
**Model:** Hypothetical `ozma-coder-1` — fine-tuned on Ozma Protocol v1.2  
**Question:** What happens when the model natively speaks the wire protocol?

---

## Complete Comparison

| Scenario | Tokens | Turns | Cost (101 turns) | vs Baseline | Avg tokens/turn |
|---|---|---|---|---|---|
| Baseline (raw OpenClaw) | 2,468,182 | 101 | $37.02 | — | 24,437 |
| Full ozma stack (shim+compress+cache) | 696,109 | 101 | $10.44 | −71.8% | 6,892 |
| **Native protocol (Ozma-Coder-1)** | **197,946** | **101** | **$2.97** | **−92.0%** | **1,960** |
| Native + PLAN turn reduction | 150,566 | ~60 | $2.26 | −93.9% | 2,509 |

---

## What "Native Protocol" Means

The full ozma stack still operates in adapter mode — the shim compresses OpenClaw's output but the model is still a general-purpose LLM receiving a reformatted prompt. Ozma-Coder-1 is a model trained to natively speak Protocol v1.2. The differences:

### System prompt: 10,725 → 250 tokens/turn (97.7% reduction)

**Baseline OpenClaw system prompt** (~6,500 tokens) contains:
- Full AGENTS.md behavioural guide (7,813 chars)
- SOUL.md persona (1,682 chars)
- `<available_skills>` XML with location paths and multi-sentence descriptions
- Heartbeat instructions, group chat rules, memory recall instructions
- BOOTSTRAP.md first-run guide
- Full OpenClaw CLI quick reference
- Safety constitution

**Shim-compressed prompt** (~2,822 tokens) still contains most of this, just trimmed.

**Native protocol prompt** (~61 tokens):
```
workspace: /root
tools: read write edit exec web_search web_fetch cron sessions_list sessions_history
       sessions_send sessions_spawn subagents session_status image memory_get memory_search
channel: webchat  model: ozma-coder-1
[ozma v1.2 | session: {id}]
```

Behavioral instructions (memory writes, heartbeat handling, group chat rules) are baked into the model weights. No tokens spent teaching a native speaker its own language.

### Tool schemas: 4,225 → 100 tokens/turn (97.5% reduction)

**Baseline** sends 17 full JSON Schema definitions on every request. The `exec` tool alone is 960 chars of JSON Schema prose.

**Native** uses short-form INTERACT notation the model already knows:
```
tools:
  read(path, offset?, limit?)
  write(path!, content!)
  exec(command!, workdir?, timeout?, pty?)
  web_fetch(url!, extractMode?, maxChars?)
  memory_search(query!, maxResults?)
  [...]
```
106 tokens for all 17 tools vs 4,225. The model has already learned what `exec` does — it doesn't need a 3-sentence description of each parameter.

### Tool results: unchanged (content is content)

Tool output compression and session cache dedup are applied the same way. The model receiving `{@9f2a1c3b}` (2 tokens) for a previously-seen web_fetch result is already modeled in the full stack. Native protocol makes this seamless rather than shim-mediated.

### REASONING/PLAN blocks: marginal on terse agents, significant on complex tasks

The traces show terse agents — most assistant turns are 0-71 tokens (tool calls only). REASONING block density improvements (3-5× vs free-form CoT per the protocol spec) help most on the analytical turns: classification decisions, report planning, error recovery reasoning.

Density ratio observed in practice: **1.2×** on these traces (agents already terse). Protocol gain is larger for verbose deployments.

---

## The PLAN Effect: Turn Count Reduction

The biggest structural advantage of native protocol is **PLAN blocks replacing iterative exploration**.

**Without PLAN** — the arxiv digest task (44 turns, observed):
```
Turn 1:  web_fetch arxiv listing → 3,952 toks
Turn 2:  web_fetch attempt → 437 toks (400 error, wasted)
Turn 3:  web_fetch attempt → 524 toks (400 error, wasted)  
Turn 4:  web_fetch arxiv month page → 3,772 toks
Turn 5:  exec grep papers → 17 toks
Turn 6:  exec fetch abstracts → 3,451 toks
...
Turn 44: final write
Total wasted on failed/exploratory calls: ~2,866 tokens
```

**With PLAN upfront**:
```
plan P1:
  p1: web_fetch arxiv/cs.CV/2026 → §listing
  p2: exec batch_fetch_abstracts.py §listing → §abstracts  
  p3: exec classify.py §abstracts → §classified
  p4: exec extract_tables.py §classified → §tables
  p5: write output from §classified §tables
```

The model knows the full pipeline before starting. It batches the fetch+classify into one `exec` call instead of 26 separate turns. Error recovery is inside the script, not between API calls.

**Conservative estimate: 40% turn reduction** on multi-step agentic tasks (simpler tasks are already short and unaffected). For the arxiv digest specifically: 44 turns → ~15-20 turns.

Each eliminated turn removes:
- 1× system prompt (150 native tokens)  
- 1× tool schema (100 native tokens)
- ~1,500 tokens of accumulated history

---

## Protocol Overhead: The Core Insight

| | Tokens/turn |
|---|---|
| OpenClaw baseline | 10,725 (sys + tools) |
| After shim | 5,154 |
| Native protocol | **250** |

The 10,725 token-per-turn protocol overhead in raw OpenClaw is **pure repetition** — the same AGENTS.md text, the same SOUL.md, the same 17 JSON Schema definitions, sent on every single API call. For a 44-turn conversation, that's 472,700 tokens just to say the same thing 44 times.

With a native model, protocol overhead is a fixed ~250 tokens regardless of conversation length. The savings compound with session length:

| Session length | OpenClaw protocol cost | Native protocol cost | Saving |
|---|---|---|---|
| 5 turns | 53,625 tokens | 1,250 tokens | 52,375 (97.7%) |
| 20 turns | 214,500 tokens | 5,000 tokens | 209,500 (97.7%) |
| 44 turns (arxiv task) | 472,700 tokens | 11,000 tokens | 461,700 (97.7%) |

---

## Scale Projections

At claude-opus-4-6 pricing ($15/M input tokens):

| Volume | Baseline | Full stack | Native protocol | Native + PLAN | Monthly saving vs baseline |
|---|---|---|---|---|---|
| 1,000 turns/day | $367 | $103 | $29 | **$22** | **$10,326/mo** |
| 10,000 turns/day | $3,666 | $1,034 | $294 | **$224** | **$103,260/mo** |
| 100,000 turns/day | $36,656 | $10,338 | $2,940 | **$2,236** | **$1,032,601/mo** |

---

## What Changes at the Architecture Level

Moving from shim-mode to native protocol isn't just a token optimization — it changes how the system works:

**Memory**: Instead of file I/O to MEMORY.md (which requires the file system, gets silently truncated, and loses content on compaction), the model natively uses `{@HASH}` session cache refs. Memory is content-addressed, durable across compaction, and queryable.

**Planning**: Instead of iterative tool-then-observe exploration, the model produces a typed PLAN block at turn 1. The executor runs steps; failures are reported against step labels (p2 failed: 404) rather than as unstructured error text the model must re-parse.

**Verification**: CLAIM/INVARIANT blocks are first-class output types. The model asserts postconditions alongside edits; the runtime checks them without a round-trip to the model.

**Tool calling**: INTERACT blocks replace JSON Schema function calls. The model produces `INTERACT exec command="..."` rather than `{"name": "exec", "arguments": {"command": "..."}}`. Same information, 60-70% fewer tokens per call, and no schema transmission overhead.

**Confidences**: Low-confidence edits are flagged inline (`edit §6 confidence=0.7`) and trigger selective retry rather than full regeneration.

---

## Honest Caveats

1. **Ozma-Coder-1 doesn't exist yet.** These numbers model what a native-protocol model would cost, not a measured result. The 92% reduction assumes perfect native knowledge — real training would have some residual uncertainty requiring occasional schema reminders.

2. **Tool call accuracy.** Stripping parameter descriptions risks degraded accuracy on edge-case parameters (e.g., `cron` with complex schedule formats). Native training must encode this knowledge; schema-free prompting is a regression risk.

3. **PLAN turn reduction is highly task-dependent.** The 40% figure is a conservative estimate for multi-step agentic tasks. Simple question-answering tasks (2-3 turns) see no benefit. Pure web-browsing tasks with unpredictable structure (can't plan ahead) may only achieve 10-20% turn reduction.

4. **Tool results still dominate at scale.** At 93.9% total reduction, the remaining 6.1% is almost entirely real work content — web pages, file contents, script output. This is the irreducible minimum: the information the model needs to do the task.

5. **Output tokens not modeled.** Brevity constraints and REASONING/PLAN structured output reduce response verbosity ~30-50% additionally. Not included here.

---

## Methodology

Native sys/tools estimates derived from the actual text of minimal ozma v1.2 system headers (61 tokens measured with tiktoken cl100k_base). Tool manifest measured at 106 tokens for all 17 tools in short-form notation. PLAN turn reduction modeled at 40% (conservative; arxiv task suggests 55-65% achievable). All other figures carry forward from `openclaw-full-stack-report.md`. Dataset: `sammshen/wildclaw-opus-traces`, 101 request events.
