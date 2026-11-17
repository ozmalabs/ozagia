# Graph-Native Memory — Ozma's Episodic Memory Architecture

**Version:** 0.4
**Status:** Draft
**Companion docs:** [`maps.md`](./maps.md) — Map Format; [`map_quality.md`](./map_quality.md) — Nine Properties; [`compound_loop_proof.md`](./compound_loop_proof.md) — Formal Theorems (includes Thesis VIII: Composite Models + Neuroplasticity)

---

## The Problem with Flat Memory

Current Ozma memory is a labeled text store: preferences, facts, decisions, anti-patterns — each memory is a text blob with a category label. Retrieval is by keyword match or embedding similarity.

This works. But it has three fundamental limitations:

1. **No structural relation to the semantic map.** A memory "user prefers dark mode" has no explicit connection to the semantic elements it involves. The semantic map represents the model's reasoning vocabulary; the memory store represents its accumulated experience. These are separate schemas with no shared structure.

2. **No episodic indexing.** Memories are retrieved by relevance, not by when they occurred. The model knows *what* it learned, but not *when* it learned it. Temporal ordering is implicit in episode IDs, not a first-class retrieval axis.

3. **No consolidation mechanism.** Memories accumulate. Over time, some become irrelevant (the user's preferences changed), some become contradictory (the model learned something that disproved a prior fact), and some become outdated. A flat store has no mechanism for revision.

Ozma's memory system needs to be graph-native, time-grouped, and periodically consolidated — mirroring the episodic memory and sleep-consolidation functions of biological cognition.

---

## Graph-Native Memory: Memories Are Map Nodes

### The core principle

**A memory is a node in the semantic map.** It is not a separate data structure with a different schema. It is a first-class element of the map, subject to the same types, constraints, and quality checks.

This means:
- Memories and semantic elements share the same graph structure
- A memory node has typed edges to semantic elements
- The memory graph is the semantic map's persistence layer
- There is no separate "memory store" — there is only the map, which happens to include episodic nodes

### Memory node schema

A memory node has:

```
memory_node:
  id: STABLE_ID
  type: "memory"
  episodic: true
  time_group: EPISODE_ID          # all memories in the same session are time-grouped
  turn_range: [start_turn, end_turn]
  valence: float [-1, 1]         # positive/negative assessment
  activation_count: int           # how many times this memory was activated

  semantic_edges:
    - to: SEMANTIC_ELEMENT_ID
      relation: "involves" | "contradicts" | "supports" | "caused_by" | "refines"
      strength: float [0, 1]

  content:
    summary: string              # model-generated summary
    raw_content: string          # original text (optional, for audit)

  assessment:
    last_assessed: timestamp
    assessed_valence: float [-1, 1]   # model's current assessment
    is_current: bool             # false if memory was superseded
    superseded_by: MEMORY_ID | null
```

### Semantic edges

The typed edges connecting memories to semantic elements are what makes the graph structural:

- **involves:** this memory is about semantic element X (primary edge)
- **contradicts:** this memory contradicts memory Y or semantic element Z
- **supports:** this memory supports semantic element X (evidence)
- **caused_by:** this memory was caused by semantic element X (causal trace)
- **refines:** this memory refines prior memory Y (replaces with more accurate version)

### Time grouping

All memories are time-grouped by episode. An episode is a session of reasoning about a topic — it has a start time, an intent, and a sequence of turns.

Time grouping enables:
- **Temporal retrieval:** "what did the model know at time T?"
- **Forgetting curves:** memories decay in activation strength over time unless reinforced
- **Consistency checking across time:** the model can detect when a memory contradicts a prior memory in the same time group (a conflict that emerged during reasoning)

### The memory graph IS the semantic map

There is no separation between "semantic map" and "memory graph." The semantic map includes:
- Primitive semantic elements (function_definition, causal_relation, belief)
- Derived semantic elements (created during reasoning)
- Memory nodes (episodic records of what happened)

All are nodes in the same graph. All are subject to the same grammar. All are checked by the same consistency checker.

This is what makes evolutionary memory work: the genome and the phenome are the same physical structure, just different views.

---

## The Consolidation Cycle

### Biological analogue: sleep and memory consolidation

In biological cognition, sleep serves a critical memory function:
- **Synaptic downscaling:** the brain reduces the strength of connections that were rarely used during waking
- **Memory replay:** the hippocampus replays experiences to the neocortex, strengthening relevant connections
- **Memory integration:** new memories are compared against old ones, contradictions are resolved, redundant memories are merged

This is not passive maintenance — it is active assessment and revision.

### Ozma's consolidation cycle

Ozma runs a consolidation step periodically — after N reasoning episodes, or when the model signals low confidence, or on a timer.

The consolidation step is a model-generated assessment of the memory graph. The model (or a specialized consolidation sub-model) reads the memory graph and produces revisions:

**1. Activation assessment**
- Which memory nodes were activated in recent reasoning?
- Which memory nodes have zero activation since last consolidation?
- Nodes with sustained zero activation → flag for potential pruning

**2. Valence comparison**
- For each active memory: does the model's current assessment of valence match the stored valence?
- Mismatch → memory may be outdated or no longer relevant to current goals
- The model generates a new valence assessment

**3. Coherence assessment**
- For each active memory: does it remain coherent with the current semantic map structure?
- A memory is coherent if its semantic edges still connect to elements that exist and are themselves grounded
- If a semantic element it references has been removed or ungrounded, the memory is flagged for re-grounding or pruning
- Contradictions between memories are not "errors" — they are the model having updated its beliefs. Both are preserved; the newer one is marked active

**4. Consolidation output**
The model writes consolidation deltas to the map:
- Prune: memory nodes with sustained zero activation and no semantic edges (ungrounded or unused)
- Update: valence fields, activation counts, assessment timestamps
- Supersede: superseded_by edges for memories that are no longer the current model state (newer memory takes precedence; old memory is preserved with history)
- Refine: new memory nodes that are refinements of prior ones (same supersession mechanism)
- Re-ground: attempt to re-ground memory nodes whose semantic edges pointed to removed elements

### Consolidation as a map operation

Consolidation is not a separate process — it is a map operation with its own envelope type:

```
[consolidate:
  trigger: "periodic" | "low_confidence" | "contradiction_detected"
  scope: "recent" | "full" | "targeted:SEMANTIC_ELEMENT_ID"
  actions:
    - prune: [memory_ids...]
    - update_valence: [{id, new_valence}...]
    - supersede: {old_id, new_id}...
    - refine: {prior_id, new_id, refinement_type}...
]
```

The consolidation envelope is a map delta — it modifies the memory graph. It is subject to the same consistency checks as all other deltas.

### Why this matters

Without consolidation, memories accumulate without revision. The memory store fills with outdated preferences, contradicted facts, and irrelevant decisions. The model's context is polluted with stale information.

With consolidation, the memory graph is actively maintained:
- Irrelevant memories are pruned
- Contradicted memories are superseded
- Valences are kept current
- The memory graph remains a coherent representation of the model's accumulated experience

**Coherence, not accuracy.** Biological memory is reconstructive, not reproductive. False memories are not a bug — they are a consequence of mirroring natural pathways. The brain doesn't store recordings; it stores fragments that get reconstructed on retrieval. The same is true here.

A memory node's value is not whether it is "true" — it is whether it is coherent with the current semantic map structure. A false memory that coheres with the map guides reasoning correctly. An accurate memory that is disconnected from the map's semantic structure is useless. Consolidation maintains coherence, not fidelity.

Supersession is therefore not correctness — it is recency of the current model state. When memory A contradicts memory B, the newer memory doesn't prove the older one "wrong." It simply represents what the model believes now. Both memories are preserved (superseded_by edges maintain the history), but the newer memory is the active one for reasoning.

---

## Evolutionary Memory Analogy

### Evolution as a memory system

Evolution is a memory system with a very slow write cycle. The genome is the accumulated memory of all past selective pressures. Each organism is a read operation on that memory. The environment provides selective pressure (assessment), which modifies the genome (consolidation).

```
Evolution:                          Ozma:
environment ─────────────────────── model experience
    ↓                                  ↓
selective pressure ──────────────── consolidation assessment
    ↓                                  ↓
genome modification ──────────────── map revision (consolidation deltas)
    ↓                                  ↓
organism shaped by past ─────────── future reasoning shaped by past
```

The key insight: evolution doesn't store experiences as logs. It modifies the structure. Future organisms don't read experience logs — they inherit modified structure. The memory and the representation are the same thing.

Ozma's graph-native memory implements the same principle:
- Memories are not text blobs in a store
- Memories are nodes in the semantic map
- The map is the memory and the memory is the map
- Future reasoning is shaped by modified map structure, not by retrieving text from a log

### Implications of the analogy

**1. Memory is structural, not archival**

In evolution, there is no "memory store" separate from the organism. The organism's structure IS the memory. In Ozma, there is no "memory store" separate from the semantic map. The map's graph structure IS the memory.

This means:
- Retrieval is structural (spreading activation on the graph)
- Forgetting is structural (pruning nodes and edges)
- Memory revision is map revision (same delta mechanism)

**2. Assessment is required for memory to persist**

In evolution, unused traits are selected against. In Ozma, unactivated memory nodes are flagged for pruning during consolidation. Memory that is never used is memory the model is not using — it should not persist.

**3. Memory revision is cumulative, not destructive**

When evolution modifies a genome, it doesn't destroy the prior version — the prior version contributed to the selection pressure that produced the modification. When Ozma supersedes a memory, it doesn't delete the old memory — it marks it as superseded, preserving the history.

The memory graph retains all versions, connected by supersedes edges. The model can trace the evolution of any concept through its memory history.

---

## Formal Properties

### Property: Memory graph is a map subgraph

All memory nodes are elements of the semantic map. All memory edges are typed edges in the map graph. The memory graph is a subgraph of the semantic map, not a separate data structure.

**Formal:** The memory graph G_m = (V_m, E_m) where V_m ⊂ V_map and E_m ⊂ E_map.

### Property: Grounded memory

Every memory node has at least one semantic edge to a non-memory semantic element. A grounded memory is about something in the semantic vocabulary — not isolated, not floating.

Grounding ensures *aboutness*, not truth. A memory about a non-existent thing is ungrounded and pruned. A memory about a real thing that contains false claims is still grounded — the content is the model's reconstruction, and reconstruction is the mechanism, not a bug.

**Formal:** ∀ m ∈ V_m, ∃ e ∈ E_m such that e.to ∉ V_m (memory node m has at least one edge to a non-memory element).

### Property: Time-grouping by episode

All memory nodes have a time_group field referencing an episode ID. Memories with the same time_group were created during the same reasoning session.

**Formal:** The time_group field induces a partition of V_m into episodic subgraphs. Cross-episode edges (semantic edges connecting memories from different episodes) carry extra weight in spreading activation — they represent durable knowledge.

### Property: Consolidation is a delta

Consolidation produces map deltas: additions, deletions, valence updates, supersession edges. All deltas are subject to consistency checking (internal consistency, type-correctness, extensional stability).

**Formal:** Consolidation output Δ_c is a valid map delta: ∀ operations in Δ_c, Consistent(Map ⊕ Δ_c).

### Property: Memory activation propagates

When semantic element X is activated during reasoning, all memory nodes connected to X by any path become activated. Activation strength decays with distance and time.

**Formal:** Given activation vector a_t for semantic elements at time t, memory node m has activation a_m = Σ_{path s→m} β^{dist(s,m)} × a_s, where β < 1 is a decay factor.

---

## What This Changes About the Architecture

### The map is now the complete model state

Previously: semantic map + memory store (separate).
Now: semantic map with memory nodes as first-class elements.

The map now contains:
- Semantic elements (the vocabulary of thought)
- Memory nodes (episodes of experience)
- Map deltas (the reasoning trace)

All three are stored in the same graph structure, checked by the same consistency mechanism, and retrieved by the same spreading activation mechanism.

### Retrieval is structural, not lexical

Previously: memories retrieved by keyword/embedding match.
Now: memories retrieved by spreading activation from activated semantic elements.

When the model activates semantic element "authentication", all memory nodes connected to "authentication" — directly or via paths — become activated. The model sees its own memories about authentication without any explicit retrieval step.

### Consolidation closes the loop

The consolidation cycle is the mechanism that makes the memory graph faithful to the model's actual experience. Without consolidation, memories drift. With consolidation, the memory graph is actively maintained.

This is the last piece of the compound loop: not just reasoning → map delta, but reasoning → memory node → consolidation → map revision → future reasoning. The loop closes at the memory layer.

### The sleep cycle analogy is structural

Human sleep is when the brain consolidates memories. Ozma's consolidation step runs after N episodes, or when signaled, or on a timer. The analogy is not just metaphorical — it is structural.

Both processes:
- Assess which memories were used recently
- Prune unused connections
- Resolve contradictions
- Strengthen reinforced connections
- Integrate new memories with old structure

---

## Prior Art: Memory Graphs, Spreading Activation, and Episodic Memory

The graph-native memory architecture draws from four converging research threads: cognitive psychology (spreading activation and episodic memory), neuroscience (hippocampal consolidation), symbolic AI (ACT-R and semantic networks), and graph theory (Hopfield networks and knowledge graphs).

### Spreading Activation in Semantic Networks

Spreading activation is the foundational retrieval mechanism for associative networks in cognitive psychology. The formal model describes a directed graph populated by nodes, each with an associated activation value. Parameters include a **firing threshold F** and a **decay factor D** — activation decays as it propagates through the network.

Key properties from the literature:
- **Termination conditions**: (a) no more nodes to fire, (b) a node reached from more than one path (convergent activation), or (c) steady-state activation reached
- **Path-based activation**: longer paths receive lower activation due to decay factor D
- **Convergent activation**: nodes reached by multiple paths receive summed activation — a form of implicit AND logic
- **Parallel vs. sequential**: can be implemented as parallel relaxation or bounded-depth sequential traversal

Ozma's spreading activation maps directly onto this model:
- Memory nodes = nodes in the associative network
- Semantic edges = typed associations with decay weighting
- Valence edges = emotional tagging (similar to spreading activation's affect tagging)
- Threshold θ = firing threshold F
- Decay factor β = decay factor D

**Limitation of classical spreading activation**: Classical models don't account for node importance (PageRank-style) or edge-type-dependent decay rates. Ozma's typed edges address the latter; node importance weighting is a natural extension.

### Hopfield Networks: Memory as Energy Minimization

Hopfield networks model associative memory as convergence to local minima of an energy function. Memory retrieval is a **content-addressable** process: presenting a partial pattern drives the network to the nearest stored attractor.

Key properties:
- **Memory capacity**: ~0.14N for N nodes (Hebbian learning) — limited compared to gradient models
- **Spurious patterns**: local minima that don't correspond to intended memories
- **Storkey learning rule**: greater capacity than Hebbian, approaches theoretical limits

The Hopfield model is relevant to Ozma's graph memory in two ways:
1. **Retrieval as convergence**: If memory nodes form attractor basins, spreading activation can be viewed as initialized gradient descent toward the nearest basin
2. **Relationship to graph theory**: The discrete Hopfield network energy function is related to graph cuts — the memory graph's structure influences retrieval dynamics

**Limitation**: Hopfield networks are fully connected — every node connects to every other. Ozma's semantic graph is sparse with typed edges, which trades some capacity for interpretability and tractability.

### ACT-R: Chunk-Based Declarative Memory

ACT-R (Adaptive Character of Thought-Rational) provides the most detailed cognitive architecture for memory as graph-structured chunks. Its memory architecture maps closely onto Ozma's design:

| ACT-R component | Ozma memory graph |
|---|---|
| Declarative memory (chunks) | Memory nodes with typed slots |
| Procedural memory (productions) | Map delta rules / extension operators |
| Buffers | Activated semantic element sets |
| Spreading activation | Spreading activation over memory graph |
| Chunk retrieval via pattern matching | Spreading activation + threshold |

ACT-R's **buffer-based architecture** is particularly relevant: buffers act as "windows" into the declarative memory graph, and productions fire based on buffer contents. Ozma's "activated semantic elements" serve the same function as ACT-R buffers — a working set that gates reasoning.

**Limitation of ACT-R**: ACT-R assumes a fixed chunk type with typed slots. Ozma's memory nodes are more flexible (arbitrary typed edges), which allows richer episodic structure but requires more complex retrieval algorithms.

### Episodic Memory: Neuroscience

Neuroscience provides the biological grounding for the consolidation cycle:

**Hippocampal-neocortical consolidation**: The hippocampus stores episodic memories short-term before consolidation to neocortex. Recent evidence suggests adult neurogenesis in the hippocampus facilitates memory renewal — old, low-activation memories are pruned to make room for new ones. This maps onto Ozma's:
- Hippocampal stage → session consolidation (short-term memory nodes)
- Neocortical stage → persistent memory graph (long-term)
- Neurogenesis → pruning of activation_count = 0 nodes

**Time-tagging**: Episodic memories are represented on a temporal dimension in order of occurrence. Ozma's temporal edges (episode/turn reference) implement this directly.

**Semantic memory derivation**: Semantic knowledge is derived from accumulated episodic memory. Ozma's map delta chain produces new semantic elements from episodic traces — the map's semantic layer grows from the memory graph's episodic structure.

**Reconsolidation**: Every time a memory is retrieved, it re-enters a labile state and must be reconsolidated. Ozma's consolidation cycle includes this: memories activated during reasoning enter a "reinforceable" state.

### Knowledge Graphs and Embeddings

Knowledge graphs (KG) use triple-store structure (subject-predicate-object) for representing facts. Ozma's memory graph differs in two key ways:

1. **Temporal dimension**: KGs are atemporal; Ozma's memory graph is time-grouped by construction
2. **Episodic vs. factual**: KGs store facts; Ozma's memory nodes store episodes (with facts as one edge type)

KG embedding models (TransE, DistMult, RotatE) learn latent representations for link prediction. Ozma's spreading activation is complementary: embeddings enable similarity-based retrieval, spreading activation enables graph-structure-based retrieval. The two are composable — embeddings can initialize activation values, and spreading activation can refine retrieval.

### Graph Neural Networks for Memory

GNNs learn representations of graph-structured data through message passing. Relevant to Ozma in two ways:

1. **Memory node embedding**: GNN-based embeddings could initialize memory node representations for faster spreading activation
2. **Consolidation as message passing**: A consolidation cycle could be viewed as iterative message passing toward steady-state activation

### Typed Edges as First-Class Abstractions

The key Ozma innovation over prior art is making **typed edges first-class** rather than treating edges as properties of nodes (as in classical semantic networks) or as triples (as in knowledge graphs). Typed edges allow:

- **Valence edges**: explicit emotional/practical tagging (not in prior art)
- **Reference edges**: memory-to-memory relations (supports, contradicts, caused-by) — not in classical spreading activation
- **Heterogeneous decay**: different edge types can have different decay rates

This is the structural gap that Ozma fills: prior art either has typed nodes (ACT-R chunks) or untyped edges (classical spreading activation, Hopfield), but not typed edges as first-class with heterogeneous properties.

### The Sleep Analogy: Consolidation as Neocortex

The two-system framing directly maps onto biological memory:

| Biological memory | Ozma memory graph | Operation |
|---|---|---|
| Hippocampus (fast, online) | Spreading activation | Fast retrieval during reasoning; graph traversal; no weight updates |
| Neocortex (slow, offline) | Consolidation cycle | Produces and stores GNN embeddings; structural refinement |
| Sleep / slow-wave activity | Consolidation trigger | Idle-cycle assessment; what actually stores the embeddings |

**What the sleep cycle stores**: GNN embeddings are computed during consolidation and stored as properties of memory nodes. These embeddings capture:
- Neighborhood structure (which memories tend to co-occur)
- Structural role (is this memory a hub or a leaf?)
- Similarity to other memories (for fast similarity-based retrieval)

**Why this matters**: Spreading activation alone is fast but ignores structural similarity — it only follows explicit edges. Embeddings enable similarity-based retrieval — finding memories that are *structurally similar* even if not directly connected. During waking reasoning, spreading activation handles direct retrieval. After sleep, embeddings are updated to capture higher-order structure.

**The cycle**:
1. **Waking**: Spreading activation retrieves memories via explicit graph structure
2. **Sleep (consolidation)**: GNN embeddings are computed and stored on memory nodes
3. **Waking**: Retrieval uses both explicit edges (fast, direct) and embeddings (slower, structural)

This is the same division as biological memory: the hippocampus handles fast episodic recall, the neocortex stores compressed semantic representations that generalize across episodes.

### Summary of Prior Art Gaps Filled by Ozma

| Prior art | Gap | Ozma contribution |
|---|---|---|
| Classical spreading activation | No typed edges, no valence | Typed edges with heterogeneous decay; valence edges |
| Hopfield networks | Fully connected, atemporal | Sparse typed graph with temporal edges |
| ACT-R | Fixed chunk types, no episodic structure | Arbitrary typed edges, time-grouped episodes |
| Knowledge graphs | Atemporal, fact-centric | Temporal edges, episodic memory nodes |
| GNN embeddings | Offline training required | Online spreading activation + consolidation stores embeddings |

---

## Implementation Notes

### Spreading activation

Spreading activation on the memory graph is a graph traversal:
1. Start with the set of currently-activated semantic elements (from reasoning)
2. For each activated node, traverse all edges (semantic and memory)
3. Accumulate activation on traversed memory nodes
4. Threshold to produce the active memory set

The activation function:
```
activate(S_semantic, G_map, β, θ) → S_memory
```

Where:
- S_semantic: activated semantic elements (from reasoning)
- G_map: the full map graph
- β: decay factor (default 0.7)
- θ: activation threshold

### Consolidation trigger

Consolidation runs when:
- **Periodic:** every N reasoning episodes
- **Confidence signal:** the model signals low confidence (detected via `confidence` field in reasoning output)
- **Contradiction detected:** the consistency checker finds a contradiction involving memory nodes
- **Manual:** triggered by operator

### Memory pruning criteria

A memory node is pruned when:
- activation_count = 0 for N consecutive consolidation cycles
- is_current = false AND no other memory node references it
- It has no semantic edges to non-memory elements (ungrounded)

Pruning is conservative.

> Prune cautiously. It is better to keep a suspicious node than to delete a potentially valuable one. The cost of a false negative (keeping a bad memory) is lower than the cost of a false positive (deleting a useful one) — the memory can be marked inactive and ignored, but cannot be recovered once pruned.

---

## Document History

- **0.1** (2026-04-23) — Initial draft. Graph-native memory established as first-class map nodes. Consolidation cycle defined. Evolutionary memory analogy developed. Formal properties stated.
- **0.2** (2026-04-23) — Added Prior Art section. Covers spreading activation (cognitive psychology), Hopfield networks (memory as energy minimization), ACT-R (chunk-based declarative memory), episodic memory neuroscience (hippocampal consolidation), knowledge graphs (triple stores and embeddings), and GNNs. Identified typed edges as Ozma's structural gap-filler over prior art.
- **0.3** (2026-04-23) — Added sleep analogy section. Consolidation = neocortex (stores GNN embeddings); spreading activation = hippocampus (fast online retrieval). The sleep cycle is what stores the structural embeddings. Three-stage cycle: waking (spreading activation) → sleep (consolidation stores embeddings) → waking (retrieval uses both explicit edges and stored embeddings).
- **0.4** (2026-04-23) — Reframed consolidation around coherence, not accuracy. False memories are a consequence of mirroring natural pathways, not a bug. Grounding ensures aboutness, not truth. Supersession is recency of current model state, not correctness. Removed all accuracy-verification framing from consolidation.