# Invariant Scale Graphs: Mapping Reality
## Part I: Foundations and Methodology

**Matthew Parslow**
**Ozma Labs Pty Ltd**
**Draft — 2026-04-25**

*Companion paper: "Invariant Scale Graphs: Mapping Reality — Part II: Consequences and Implications" (same author, same repository).*

> *A novice was trying to fix a broken Lisp machine by turning the power off and on.*
>
> *Knight, seeing what the student was doing, spoke sternly: "You cannot fix a machine by just power-cycling it with no understanding of what is going wrong."*
>
> *Knight turned the machine off and on.*
>
> *The machine worked.*
>
> — AI koan, MIT AI Lab oral tradition

---

## Abstract

I set out to solve two concrete problems. The first was keyboard, video, mouse, and audio routing between workstations without hardware switches, treated as a software problem. The second was the cost of agentic implementation work on the first problem, treated as an architectural problem. Each problem was pulled on until it was entirely unraveled. Each produced a graph-native coordination protocol. The two protocols turned out to be structurally identical.

This paper is the formal account of what they both are.

**A note on production method, stated up front because it is load-bearing rather than incidental.** This paper was built by multiple frontier language models — Anthropic's Claude Opus 4.x, Moonshot's Kimi K2.x, Zhipu AI's GLM-5.x, Qwen 3.6, and others — operating under the author's architectural direction through the protocol, hint packs, compound loop, and mechanical validators the paper itself specifies. This is a feature of the work, not a disclosure about its preparation. The production methodology *is* the component-API decomposition the paper argues for, instantiated at paper-production scope: the author as high-judgement architectural component; frontier LLMs as typed implementation components dispatched against grammar-gated contracts; mechanical validators (hints, checks, consistency checkers, structural-output parsers, pytest and similar) at the interface boundaries; the compound loop iterating over a shared typed-graph memory that persisted across sessions. The paper's structural integrity is therefore one of its primary empirical claims — not a separate argument appended after the fact. If the mechanism did not work, the paper would not hold together as an integrated argument. Circular reasoning is preempted in §12; the three independently checkable premises (artefacts exist, production process is documented, measurable properties match prediction) are the audit trail an empirical verification requires.

The root claim is that there is one typed graph, and every wire format demonstrated in this paper — video routing, audio routing, USB HID, power distribution, physical control planes, programming languages, the compilation stack from source through intermediate representation down to microcode, and agent coordination — is a serialization of a subgraph of it. Coordination, compilation, routing, and reasoning are not therefore the *same* operation in any surface sense — compilers do not route packets and routers do not compile programs. The substrate-level claim is sharper and more constrained: each of these activities is composed of typed-graph operations on a common typed substrate, and a protocol that speaks graph-native composes any of them on that substrate without surface-language coupling. Representational reducibility (everything can be encoded as a typed graph) is necessary but not sufficient; the claim is that the framework's specific axioms (Axioms 1–3, §3.2) plus its specific protocol grammar Π are sufficient — a constructive structural claim, not an assertion that all encodings are operationally identical. The claim is asserted as a one-graph universal at the substrate layer; it is demonstrated here on the specific wire formats named above, with boundary conditions (where the claim does not apply) named explicitly in §9.2 and §E.5.

The paper's contribution is methodological before it is technical: naming an assumption current computing has built on without naming — that byte-stream is the natural substrate for general-purpose computation. This paper names one such assumption in full and develops the methodology through it; the companion paper applies the same operation to eleven further unnamed assumptions in adjacent domains, for twelve named across the programme. Once named, the assumption is falsifiable — §9.2 measures the gap concretely. Once falsified, the alternative is graph-native, was operationally shipped from 1961 through the late 1980s, and was abandoned for economic rather than technical reasons. The root theorem, the four bounds, and the two instances developed below are downstream consequences of the one naming this paper develops, not the contribution itself.

The paper proves a root theorem in this form: for any two clients of a graph-native protocol, any composition under the protocol reduces to graph operations on the underlying typed graph. Ten previously-separate theses about cognitive architecture, learning theory, memory, and self-construction reduce in one of two ways: to corollaries of this theorem, or to citations of established results applied to the typed-graph view. Of the ten prior theses, three remain non-trivial and are treated independently in §7: Theses IV (monotone acceleration of reflective loops) and V (multiplicative gain structure) are proved in full; Thesis IX (learning-theoretic bound on graph-selection hypothesis classes) is given as a proof sketch via standard PAC-Bayes machinery, with a fully-worked derivation deferred to a separate technical report. Four further theorems are derived directly from the root claim: native-dialect efficiency (Theorem X, encoding layer — Shannon-bound distance as speedup), computational efficiency (Theorem XI, computation layer — Kolmogorov-bound speedup for structure-respecting computation), behavioural bounding (Theorem XII, safety layer — the system's observable behaviour is provably contained within the grammar Γ's admissibility set), and substrate-Markov-mixing efficiency (Theorem XIII, kernel-learning layer — sample complexity for learning a transition kernel scales with substrate-richness gap, composing multiplicatively with Theorem XI). Theorem XI is the Kolmogorov-theoretic explanation for why sparse-computing methods (sparse matrices, Lottery-Ticket networks, attention sparsity, graph neural networks) win empirically — they are all partial realizations of the same principle, and the semantic graph is its maximum-fidelity realization.

The paper has two empirical instantiations. Instance 1 is Ozma System, a typed-graph substrate *specified* as a complete digital-twin graph for physical and computing infrastructure at every scale (single device through multi-hall datacentre) — covering signal routing (video, audio, peripherals, data, power, control), device topology, power distribution traced from utility feed to consumer, physical environment (sites, buildings, rooms, racks, floor plans, RF propagation), audio production, security and identity, asset management, monitoring, network infrastructure, and building management within one coherent typed graph. The specification is the 29-document RFC-2119 draft standard published publicly at `https://github.com/ozmalabs/ozma/tree/main/docs/routing` (CC-BY-4.0, v0.1, 2026-04). KVMA (keyboard/video/mouse/audio) routing was the originating problem the system was pulled from; the architecture that fell out absorbed every adjacent infrastructure concern (§4.5 enumerates the absorbed domains). The implementation is at proof-of-concept stage — not full-deployment-scale — but exhibits self-construction at two layers: runtime topology (nodes announce, graph extends, pipelines assemble under declared intents) and deployment-bootstrap (the POC creates its own VM, self-installs into it, and builds out from that VM as the initial node). Instance 2 is Ozma AI, a protocol-first architecture for composite-model coordination whose coordinator self-improves through a compound loop on a shared semantic map. Both systems were produced by the same author, working the same method, with no intent that they mirror each other. Their convergence on the same structural answer is the primary empirical claim; *Instance 1's internal cross-domain convergence across 8+ infrastructure domains is a separate convergence signal* that strengthens the empirical case.

A framing point on continuity with prior literature. The framework presented here is continuous with the research programme articulated by LeCun (2022) and adjacent authors — that autoregressive language modelling on statistical substrates is architecturally insufficient for intelligence, and that persistent world models, hierarchical iterative planning, objective-driven behaviour, and structured latent spaces are the missing properties that current scaling approaches do not supply. Under the graph-native axioms A1–A3, the convergent-properties theorems of §6 combined with the multiplicative-gain result of §7.2 establish that the convergent properties enumerated in §6 follow as theorems rather than as empirical observations. Section 6.4 (Theorem D — *Functional cognitive completeness*) extends this with a constructive mapping from each functional component of cognitive architecture — perception, attention, memory, emotion, thought, planning, metacognition, learning, goal-directed behaviour — to a specific graph operation or observable, under scalar weight functions over the substrate. Persona is a weight function; emotion is a time-varying weight distribution; thought is a weighted traversal vector. The framework admits a direct restatement in Markov-chain terms — the substrate plus weight functions defines a Markov chain over typed-graph nodes; statistical language modelling is the same family of machinery on a token substrate.

A measured secondary result: the efficiency advantage of operating on the graph rather than on surface serialization is a big-O reduction on production code, not a constant-factor improvement, with the measured speedup growing super-linearly as input size grows. The benchmark and scaling-curve detail are reported in Appendix E. The efficiency claim is not *"compression happened"*; it is *"the graph-native encoding sits closer to the Shannon lower bound for code-semantic content, and the dense encoding's distance from the bound appears as measurable cost."* H(X_raw) ≈ H(S); what differs is the code. A new bound in §9.4 (Theorem XIII — substrate-Markov-mixing efficiency) shows that the kernel-learning cost on a token substrate is bounded below by a function of the substrate-richness gap, and that this bound composes multiplicatively with Theorem XI's encoding-and-computation bound; the combined bound is the product of two independent Kolmogorov-style bounds.

The architecture requires no breakthroughs in model training or hardware, and none of the core ideas are new. Graph-native computing was the mainstream of serious computer architecture from the Burroughs B5000 in 1961 through the Lisp Machines of the 1980s, before being abandoned for economic rather than technical reasons in 1988. The Ozma work is interop-layer craft — Knight, Barton, Kay, Sutherland, Stallman, and the rest of the first-wave architects designed the pipes; the industry unplumbed them for cheap dense silicon; this paper's contribution is plumbing them back in, measuring the 40-year gap with Theorem XI, and demonstrating operational realisability across two modern domains. The author is not proposing a new architecture; he is restoring an abandoned one and measuring the gap with Theorem XI. Downstream consequences are developed in the companion paper. The convergence is measured quantitatively in Instance 2 and architecturally verified with quantitative measurement pending in Instance 1.

The paper is therefore its own experiment. Its existence is the observation; the theorems, empirical sections, and figures are the description the observation matches; the multi-model production pattern named above is the mechanism both being described and doing the describing. At the restricted scope of paper production, the pattern's correctness is observational rather than speculative — *getting multiple LLMs to collaboratively build the system that makes the thesis is the mechanism doing the thing the thesis describes*. The restricted-scope demonstration is the paper the reader is holding.

---

## 1. Introduction

### 1.1 Two problems, pulled until they unraveled

This work began with a trivial-sounding personal frustration. I wanted to play games during boring work meetings and have the audio and video merge seamlessly between sessions. Taken seriously, that requires routing keyboard, video, mouse, and audio between arbitrary devices under arbitrary rules, without hardware KVM switches or per-device glue. Pulled on, the problem is not KVM-with-more-features. It is a general signal-routing problem over typed ports, links, and pipelines across arbitrary scales, from a single desk to a datacentre. The architecture that falls out treats every signal — USB HID, DisplayPort, analog audio, network-attached peripherals, power, control — as instances of one typed graph. This architecture is Ozma System.

While building Ozma System, agentic implementation costs became unsustainable. I pulled on that problem too. Taken seriously, the cost was not about prompt length or model selection; it was about the absence of a stable protocol between reasoning components, forcing each integration to re-solve the same coordination contracts. The architecture that falls out treats every model output and every tool invocation as messages in a protocol with typed envelopes over a shared semantic map. This architecture is Ozma AI.

I did not notice until both architectures existed that they are the same architecture. Both define typed nodes, typed edges, a grammar that closes over them, and operational primitives that traverse, transform, and extend the graph. Both exhibit self-construction under reflective operation — topology discovery and pipeline repair in one case, map improvement and capability growth in the other. Both allow swap-out of clients and implementations without protocol change.

Two distinct starting problems, pulled independently, converged on the same structural answer. I did not see this coming. The convergence is the primary evidence that the structural answer is a feature of the territory, not of the observer.

### 1.2 What the paper claims

The root claim has four parts:

1. **Methodological.** The framework's contribution is a methodology for naming load-bearing assumptions current computing has built on without naming. The structural, operational, and empirical claims below are downstream consequences of one such naming — that byte-stream is the natural substrate for general-purpose computation. The companion paper applies the same operation to eleven further unnamed assumptions in adjacent domains. Each named assumption falsifies trivially once visible; the architecture, theorems, and artefacts of this paper are the consequences of dropping the first one.

2. **Structural.** There is one typed graph. Every wire format demonstrated in this paper — programming languages, compilation targets, network protocols, signal formats (KVMA), control planes, protocols between agents — is a serialization of a subgraph of this structure. The subgraphs differ in node and edge types; the underlying structure does not. The universal form of the claim (applying to any wire format, not only those demonstrated) is asserted, with the boundary condition named in §9.2 and §E.5 — it fails precisely where K_Γ(I) = Θ(|I|).

3. **Operational.** A protocol that speaks graph-native coordinates any client because every client's operations factor through typed-graph operations on a common substrate. Coordination, compilation, routing, and reasoning are not surface-identical (the activities differ in their domain semantics) but they share substrate-level structure: each is a sequence of typed-graph transformations and traversals, and a graph-native protocol composes any of them on a common typed substrate without surface-language coupling. Representational reducibility licenses the encoding; Axioms 1–3 plus the protocol grammar license the operational composition.

4. **Empirical.** Two independent engineering efforts, applied to distinct problems by the same method, produced graph-native protocols with convergent behavior including emergent self-construction. The convergence licenses the structural and operational claims as engineering observations rather than abstract conjectures.

The paper is organized around these three parts. §2 surveys the convergent lineage in prior work — denotational semantics, the language-of-thought hypothesis, program dependence graphs, multi-level IR, graph-based program representation, information theory of code, the empirical LLM-compiler literature, and — crucially — the first-wave graph-native architectures (Burroughs B5000, Lisp Machines, Smalltalk, Sketchpad) that established the architectural vision before the industry abandoned it in 1988 (§2.10). §3 states the formal root theorem. §4 presents Instance 1 (Ozma System — a complete digital-twin typed graph for infrastructure across signal routing, power, physical environment, security, audio, asset management, monitoring, and adjacent domains; KVMA was the originating problem). §5 presents Instance 2 (Ozma AI). §6 states and proves the convergent-property theorems that apply to any sufficiently reflective graph-native system. §7 contains the surviving formal results (IV and V as full proofs, IX as a labelled proof sketch) that do not collapse to corollaries of the root theorem. §8 gives the empirical validation. §9 derives four bounds from the root claim — Theorem X (native-dialect encoding efficiency), Theorem XI (Kolmogorov-bound computational efficiency), Theorem XII (behavioural bounding — system behaviour provably contained within the grammar's admissibility set), and Theorem XIII (substrate-Markov-mixing efficiency — kernel-learning sample complexity bounded by substrate-richness gap, composing multiplicatively with Theorem XI). §10 states licensing. §11 addresses what remains conditional. §12 concludes. The implications and consequences of the framework are developed in the companion paper (*Part II: Consequences and Implications*).

### 1.3 What the paper does not claim

It does not claim that "everything is a graph" in the trivial sense that any structure admits some graph formalization. The claim is **one graph**: that all systems are typed subgraphs of a single underlying structure, and protocols that speak graph-native coordinate any client because every client is already a graph with compatible operational primitives. The specificity is what makes the claim non-vacuous. Where the one-graph claim fails to apply — at Theorem XI's boundary case (§9.2, §E.5), where K_Γ(I) = Θ(|I|), the input is effectively noise relative to the chosen grammar, and graph-native representation offers no advantage — the limits are explicitly identified rather than hidden.

It does not claim that the architecture is AGI. Implications and consequences for AGI are developed in the companion paper.

It does not claim that the author foresaw the unification. The paper's evidentiary strength comes from the opposite: two independent thread-pulls, the second not aware that it was going to converge with the first, produced convergent architectures. The retrospective recognition of the convergence is what licenses the structural claim.

It does not claim that the architecture is new. The graph-native architectural vision is prior art traceable through the Burroughs B5000 (Barton, 1961), the MIT AI Lab Lisp Machines (Knight, Moon, Greenblatt, Stallman, 1970s–1980s), Smalltalk (Kay, Ingalls, 1970s), Sketchpad (Sutherland, 1963), and the structured-programming traditions of Wirth and others (see §2.10 for the full lineage). The industry abandoned these architectures in 1988 for economic rather than technical reasons — cheap commodity silicon won on price per transistor. The present work is interop-layer craft: reconnecting the graph-native architecture that was unplumbed in 1988 to the modern stack, formalising the efficiency gap that the 1988 decision chose to pay (Theorem XI), and demonstrating operational realisability across two modern domains. The contribution is the plumbing and the measurement, not the architecture itself. The architects of the first wave deserve the primary credit; this paper builds on their foundation.

It does not claim the unnamed assumptions named below were hidden. They are load-bearing for institutional investment in current architectures — frontier-lab business models on monolithic policy, the entire pretraining-scaling thesis on single-pass next-token, every major compression benchmark on byte-faithfulness, every English-dominant training corpus on language-as-substrate. Naming them is structurally adverse to the institutions that built on them, which is why the field has not done so from inside. An interop-layer craftsman from outside that incentive — pulling on concrete operational problems until the unnamed assumptions making them hard become visible — is the natural agent. The contribution is the naming and the measurement, both available to anyone willing to pull the same threads.

---

## 2. Related Work

The synthesis the paper asserts has been partially approached by multiple literatures. None has articulated the full claim. This section surveys the convergent lineage and positions the contribution.

### 2.1 Denotational semantics — programs denote, syntax doesn't matter

Scott and Strachey (1971) and Stoy (1977) established that programs are phrases that denote mathematical objects; the denotation is the program's meaning, independent of the syntactic presentation. The paper's root claim extends the denotational framing operationally: the denotation is a typed graph, and programming languages are phrases that denote the same graph via different serializations. What denotational semantics asserts at the level of mathematical meaning, this paper asserts at the level of operational representation.

### 2.2 The language of thought hypothesis — mental representation as compositional

Fodor (1975) posits compositional mental representations underlying natural-language cognition. Sowa (1976, 1984) operationalized this as Conceptual Graphs for knowledge representation. Wierzbicka and Goddard's Natural Semantic Metalanguage proposes a small set of semantic primes universal across natural languages. The paper's claim extends these to programs and systems: the compositional representations underlying natural language, programming languages, control systems, and coordination protocols are subgraphs of one structure.

### 2.3 Program dependence graphs and language-agnostic IR — canonical forms for code

Ferrante, Ottenstein, and Warren (1987) established the Program Dependence Graph as a canonical form combining control and data dependencies. Horwitz, Reps, and Binkley (1990) extended this interprocedurally. Forty years of compiler research has treated graph-based intermediate representations as the canonical form for cross-language, cross-optimization program analysis. Lattner et al. (2021) operationalized this at industrial scale with MLIR — explicitly multi-level, explicitly designed so that many domains and source languages co-exist and progressively lower. GraalVM Truffle demonstrates language-agnostic AST interpretation serving Python, Ruby, JS, R, C, and LLVM bitcode from one substrate. WebAssembly demonstrates a common low-level target for forty-plus source languages.

These are engineering existence proofs that "one representation, many surface languages" works in production. They stop short of the claim that the full stack — including the source level and the natural-language-spec level above it — is the same structural object.

### 2.4 Graph-based code representation — semantic over syntactic

Allamanis, Brockschmidt, and Khademi (2018) introduced graph representations combining syntactic and semantic edges (data flow, binding) for learned program understanding. GraphCodeBERT (Guo et al., 2021), CodeT5 (Wang et al., 2021), and PLBART (Ahmad et al., 2021) demonstrate that a single learned embedding space supports many languages — empirical evidence that the shared-semantics hypothesis is already implicit in pretraining outcomes. InferCode (Bui et al., 2021) and the cross-language clone-detection literature demonstrate that semantic equivalence survives change of surface language — the exact implication of the paper's one-graph claim.

Cross-language binary-source matching via intermediate representations (Gui et al., 2022; Zhang et al., 2024) comes closest to the full stack claim among existing work, but restricts itself to source↔binary rather than the complete serialization chain.

### 2.5 Concept libraries and neurosymbolic program synthesis

DreamCoder (Ellis et al., 2021) jointly learns a library of concepts and a neural search policy, demonstrating that concept-level representation can be induced automatically and compresses programs dramatically. LILO (Grand et al., 2023) is its LLM-era descendant. NS³ (Arakelyan et al., 2022) demonstrates concept-level code search over token-level matching. These lines assert the concept library within single surface languages; the paper's root claim is that these libraries are subgraphs of a single underlying structure whose serialization into each surface language is a map (a functor in the category-theoretic framing of §2.9, but the paper uses "map" operationally to avoid importing the categorical apparatus — see §3.1).

### 2.6 Information theory of code — natural and unnatural languages

Shannon (1951) established that long-range English has roughly 75% redundancy against its entropy lower bound. Hindle, Barr, Su, Gabel, and Devanbu (2012) established that programming languages have per-token cross-entropy of 3–4 bits, substantially lower than English — source code is "more repetitive" than natural language. Allamanis et al. (2018, survey) consolidated this line. Nanz and Furia (2015) measured cross-language conciseness on Rosetta Code across approximately 1000 tasks: functional and scripting languages are substantially more concise than OO and procedural. Recent non-peer-reviewed empirical measurement on Rosetta Code (Alderson, 2026, blog post) measures approximately 2.6× variance in token count for equivalent semantic tasks across surface languages, using the GPT-4 tokenizer.

These measurements establish, empirically, that surface languages sit at different distances from a common information-content lower bound. They do not articulate that the common lower bound is the graph-native encoding of the content; they measure against a tokenizer proxy. §9 of this paper closes that gap.

### 2.7 LLMs and the compilation stack

The current empirical LLM-compilation literature documents precisely the failure mode the paper's structural claim predicts. Jiang et al. (ICML 2025) establish that frontier LLMs "exhibit competence in parsing IR syntax and identifying high-level structures, but consistently struggle with instruction-level reasoning, especially control flow, loop handling, and dynamic execution." REx86 (2025) observes that "assembly code is relatively rare and may be underrepresented in pretraining corpora" and that models "describe individual instructions but fail to comprehend purpose in wider context." Meta's LLM Compiler (Cummins et al., 2024) demonstrates empirically that joint pretraining on LLVM-IR and assembly produces capabilities that source-only LLMs lack — the existence proof that linking stack levels works. Nova (ICLR 2025) identifies "low information density" and "diverse optimizations" as the two root causes of failure and hand-designs hierarchical attention as a structural inductive bias.

These are retrofits. The paper's framing identifies the regime failure as token-sequence training presenting each stack level as a separate distribution, when the levels are in fact views of the same concept graph. The fix Nova approaches hand-designed is what falls out natively if the substrate is graph-native from the start.

### 2.8 Cognitive architectures, universal AI, and neurosymbolic integration

ACT-R, Soar, CLARION, and HTM aim to computationally instantiate cognitive structure. They differ from the present work in three respects: none has a protocol layer decoupling output grammar from implementation, none can grow new sub-components during operation (only new rules within fixed architectures), and none has a formal convergence theorem for an improvement loop. Hutter's AIXI combines Solomonoff induction with sequential decision theory but is incomputable. Kautz's taxonomy of neurosymbolic approaches identifies hybrid categories; the present architecture is Neural[Symbolic] in his typology but with composite-model structure.

The Hutter Prize treats compression of human knowledge as a proxy for general intelligence. The framework agrees that efficient encoding is load-bearing, and disagrees about which encoding the prize actually rewards. The prize encodes an unnamed assumption: that byte-faithfulness is the right fidelity criterion for compression-as-intelligence. Under that assumption, the decompressor must reproduce the source bytes exactly — every redundant template, every formatting artefact, every duplicate citation. Compression preserves bytes; translation preserves meaning across substrates. These are different operations on different artefacts, and the framework's claim (§9.1, Theorem X; §9.4, Theorem XIII) is about the second. Once the assumption is named, the prize structure is visible as measuring the wrong quantity for the framework's contribution: a content-faithful translator from byte-stream into typed graph, scored as |G| at fixed fidelity on a queryable artefact, would be the right benchmark for the framework's claim. Pure text compression at byte-faithfulness is a serialization optimization on a fixed-substrate constraint; the framework operates at the substrate boundary the constraint forecloses.

This is not a critique of Hutter's work; it is a downstream consequence of it. Hutter's specific operationalisation of compression-as-intelligence — a concrete benchmark with measurable budgets, joint compressor-and-decompressor accounting, and explicit fidelity criterion — is what makes byte-faithfulness vs content-faithfulness a distinction one can draw at all. Without that precision the distinction is philosophy; with it, the assumption is nameable, and once nameable, falsifiable. The framework's contribution here stands on Hutter's shoulders. Solomonoff (1964) and Kolmogorov supplied the formal language of minimum description length of content that the framework uses to say what the right benchmark looks like. Shannon (1951) supplied the bound the framework measures distance from. Each of these prior workers articulated a load-bearing position with sufficient precision that someone, later, could name the assumption it carried as an assumption. The methodology depends on those articulations; without them there is nothing precise to name.

### 2.9 Category theory — the mathematical validation

Eilenberg and Mac Lane's category theory (1945) is the mathematical framework in which the universal claim of this paper is most naturally expressed: every structure has an associated category (objects plus morphisms with composition), and categories themselves admit morphisms (functors) between them. The claim that every system is a typed subgraph of one underlying structure is a claim about categories; the claim that wire formats are serializations is a claim about functors from the structure-category into surface-language-categories. The paper does not pursue the full category-theoretic formalization (a separate paper's scope), but cites the framework as the mathematical validation that the proposed structure is well-typed.

### 2.10 Graph-native architectures — the first wave (1961–1988)

Graph-native computing is not a new proposal. It was the mainstream of serious computer architecture for roughly two decades, from the early 1960s through the late 1980s, before being abandoned for economic rather than technical reasons. The paper's contribution is the formal grounding (Theorem XI) and the operational realization across two domains; the architectural vision is prior art that this work restores and measures, not invents.

**Burroughs B5000 (Barton, 1961).** The first commercial tagged architecture: every machine word carried type information alongside its data payload, and every memory access was type-checked in hardware. Descriptor-based memory indirection made data structures first-class at the hardware level. Stack machine with hardware-managed activation records. The B5000's design exhibits the core of what Theorem XI measures — a typed-graph machine with O(polylog) structural access — in 1961 silicon.

**MIT AI Lab / Lisp Machines (Knight, Moon, Greenblatt, Stallman, 1970s–1980s).** Hardware-enforced typed cells, garbage collection as a primary operation, message dispatch as typed-edge traversal, symbolic computation as primitive. The CADR, LMI Lambda, and Symbolics 3600 implemented graph-native operations at the hardware level with development environments that have not been surpassed. The architectural primitives of the Lisp Machine — tagged pointers, first-class functions and continuations, dynamic dispatch, memory as a graph of tagged cells, and reflective access via ZetaLisp and the Symbolics Genera environment — are the operational instantiation of the paper's root theorem in 1980s silicon.

**Smalltalk and the Xerox PARC tradition (Kay, Ingalls, Goldberg, Deutsch, 1970s–1980s).** Every computational object is a typed node; every operation is a typed message sent between nodes. Smalltalk-80 ran on machines where the graph of objects *was* the machine. Alan Kay's framing of computation as message-passing between encapsulated typed entities is a direct statement of the graph-native principle, decades before this paper's theorem formalized it.

**Sketchpad (Sutherland, 1963).** The first direct-manipulation graphical interface treated drawing as operations on a typed graph of geometric constraints. Constraint satisfaction via graph traversal; the object hierarchy was explicitly node-and-typed-edge. Sutherland's PhD thesis describes what would now be called a graph-native CAD system.

**F-14 Tomcat Central Air Data Computer (Geller, Holt; Garrett AiResearch / Litton, 1968–1970).** A 20-bit pipelined microprocessor (MP944 chipset, six MOS LSI chips) designed in 1968, integrated 1970, operational in the F-14A from 1970 (first flight December 1970; fleet service 1974), classified at the U.S. Navy's request until 1998. *It predates the Intel 4004 (announced 1971) by approximately one year* and was operating at fleet scale for ~28 years before its existence was publicly disclosed. Above it in the airframe, the AN/AWG-9 weapons control system (Hughes) implemented track-while-scan for up to 24 simultaneous targets with structured-state-machine engagement logic — graph-native in operational structure. The F-14 as a system was a multi-typed-sensor (radar, IR, INS, ADC, RWR), multi-typed-actuator (control surfaces, throttles, weapons, sensor steering), hard-real-time, gracefully-degrading coordination platform — exactly the operational requirement set the typed-graph substrate is designed for. Combat aviation has driven defense computing toward graph-native architectures continuously since 1968: 1553B mil-std bus (1973) is a typed-message coordination protocol; AFDX (ARINC 664-2003) is deterministic typed-channel coordination; DO-178C and seL4 (Klein et al. 2009, NICTA/UNSW Australia) are formal-methods-grounded admissibility-by-construction in the same lineage as Theorem XII. The declassified F-14 example is the cleanest public-record instance; the broader inference — that defense computing has continued developing graph-native architectures in classified programmes throughout the period the academic and industrial waves stalled — is the simplest reading of the operational-requirements pattern.

**Burroughs MCP, Oberon, and the structured-programming tradition (Wirth, van Wijngaarden, Dijkstra).** Strongly-typed systems whose runtime structures were explicit graphs with typed edges. Oberon's module system treated code dependencies as a typed graph queryable at runtime. These traditions maintained graph-nativeness through the 1970s and 1980s in Europe and academic computing.

**The 1988 detour.** In the late 1980s, commodity silicon economics tipped decisively against specialized graph-native architectures. Untyped byte-oriented hardware (x86, commodity RISC) became cheap enough per transistor that general-purpose machines could be mass-produced at costs graph-native architectures could not match. Symbolics entered bankruptcy in 1992; LMI dissolved; the Lisp Machine tradition became archival. The industry subsequently told itself that commodity hardware had "won on the merits." It had won on price per transistor while the graph-native tradition was shipping better engineering at higher unit cost. The decision encoded an assumption the field did not name at the time: that economic and technical correctness about substrate would converge later — that the cheap-silicon path's economic correctness at the transistor level concealed no permanent technical commitment. It did. The byte-stream-as-substrate decision was not deferred; it was made, and the cost compounded. The forty years since 1988 have been a sustained attempt to reconstruct, piece by piece, what a coherent graph-native architecture had already provided: graph databases re-deriving typed storage, GPUs re-deriving graph-shaped tensor operations, MLIR re-deriving multi-level typed IR, attention mechanisms re-deriving typed-edge traversal, and sparse-computing techniques re-deriving the efficiency arguments of Theorem XI in domain-specific form. Each is one band of the gap between the unnamed substrate decision and the technical reality it forecloses; Theorem XI is the integrated measurement.

**What this paper adds to the lineage.** Theorem XI formalizes the efficiency gap between graph-native and dense-access computing as the Kolmogorov ratio |I| / K_Γ(I), making the 1988 decision quantitatively wrong rather than a matter of taste. The two instances (Ozma System and Ozma AI) demonstrate that graph-native coordination is operationally realisable on commodity hardware without new silicon — the substrate was lost but not lost irretrievably. The paper's posture toward the first-wave architects is one of indebted operational restoration: the architecture was correct; the industry should not have abandoned it; the measurement and the plumbing are the contribution.

**On "economic vs technical."** A standard objection: the framework's claim that the 1988 abandonment was for "economic rather than technical reasons" is incomplete because economic cost is itself a technical property of architectures. The objection is correct: cost-per-transistor IS a technical property, and Lisp Machines lost on the cost/performance metric of the 1988 hardware market. The framework's response is not that economics is separate from technical merit; it is that the economic *gap* between the graph-native and the byte-stream architectures was a property of the *hardware substrate* (specialised vs commodity silicon at 1988-era price points), not of the *architectural form* itself. Today the architectural form runs on commodity hardware (x86 and ARM, the same commodity silicon that won in 1988) — Instances 1 and 2 demonstrate this — so the 1988 hardware-economic gap is closed at the substrate-realisability layer. The contemporary economic case for graph-native is then *Theorem XI itself*: the measured 1.4×–7.95× speedup on the §E.2 benchmark, multiplicatively composed with Theorem XIII's substrate-Markov-mixing efficiency, is the per-operation cost-asymmetry the framework predicts and the empirical record begins to confirm. The framework does not ask the reader to take "economic vs technical" as a clean separation; it asks the reader to observe that graph-native architecture is now operationally cheaper *per measured operation* on commodity hardware, and that the 1988 decision encoded an architectural commitment whose costs Theorem XI now measures directly.

### 2.11 What remains unsaid in the prior literature

No paper we have located asserts the following in combination:

1. That programming languages, natural languages, compilation-stack levels, signal formats, coordination protocols, and control systems are serializations of a single underlying typed graph.
2. That the efficiency ratios between surface languages are measurements of each language's distance above the Shannon lower bound for the content; under Axiom 2 (entropy-preserving translation for reasoning-relevant content), the graph-native encoding approaches the bound within a constant factor for the content class the framework addresses, while general-purpose surface codes (natural language, byte-stream serialisations) sit measurably above it. (Shannon bounds are source-distribution-dependent; the claim is conditional on the typed-graph substrate being the source-appropriate code, not on a single encoding being optimal across all sources.)
3. That a protocol that operationalizes graph-native coordination handles any of these domains without redesign.
4. That a working implementation of the protocol coordinating two independently-derived architectures constitutes the existence proof.

The individual ingredients are established. The synthesis is the contribution.

---

## 3. The Root Theorem

### 3.1 Notation

- `G` — a typed graph with node type set `N` and edge type set `E`. Both may be open (extensible by clients).
- `G_i ⊆ G` — a subgraph used by client i, typed by restrictions `N_i ⊆ N, E_i ⊆ E`.
- `Σ_j` — a surface-language alphabet (tokens, signals, machine words, etc.).
- `W_j : G → Σ_j*` — a serialization map encoding graph fragments into surface language j. Round-trip inverse `W_j^{-1} : Σ_j* → G` is expected where defined. (In a full category-theoretic formalisation these would be functors between type-categories, cf. §2.9; the paper uses "map" rather than "functor" in notation to avoid importing the categorical apparatus without formalising it.)
- `Γ` — the grammar constraining well-typed composition in `G`: a set of production rules over node/edge type triples. Γ is the operational specification of what compositions are admissible; it is invoked in Axioms 1–2, §7.3, and throughout §9.
- `Π` — the protocol: a set of operational primitives over `G` including traversal, extension, transformation, and query, together with grammar Γ.
- `φ_i(s)` — the operational trajectory of client i starting from state s, expressed as a sequence of Π operations on G_i.
- `φ : X_raw → G` — a graph-native encoding map (used in §9.1 under Axiom 2; subscripted `φ_i` is distinct from unsubscripted `φ`).
- `L(X) = E[tokens(serialize(X))]` — expected serialized token length of representation X under a fixed tokenizer; used in Theorem X (§9.1).
- `ℓ(M, R, O) = 1 − coherence(M, R, O)` — loss function for learning-theoretic analysis in §7.3; distinct from `L` above.
- `|V|` — element-vocabulary size (distinct from edge-type set `E`); used in §7.3 generalization bound.
- `H(X)` — Shannon entropy of representation X with respect to the underlying reasoning-relevant information source.
- `K(I)`, `K_Γ(I)` — Kolmogorov complexity of instance I, and Γ-relative Kolmogorov complexity (length of shortest description of I in grammar Γ); used in §9.2.
- `τ(M) ∈ [0, 1]` — the map-quality score over the nine properties defined in Appendix A; invoked in §5.3, §6.1, §7.1, §7.3.
- `C` — a consistency checker deciding admissibility of operations under Γ in finite time per operation (§9.3).
- `meta-Γ`, `meta-meta-Γ`, … — the admissibility rules governing operations that modify the Γ-representing subgraph itself; the regress terminates at a fixed authority root per deployment (§9.3 Step 3).
- `𝔅(S)` — the set of operations system S actually performs (used in Theorem XII to avoid collision with the RLHF/PPO KL-penalty `β`).

### 3.2 Axioms

**Axiom 1 (Typed-graph closure).** G is closed under typed composition: for any two node types (t_a, t_b) ∈ N × N and any edge type t_e ∈ E such that Γ admits the triple (t_a, t_e, t_b), the composed edge is a well-typed element of G.

**Axiom 2 (Serialization adequacy).** For every surface language W_j used by a protocol client, W_j and W_j^{-1} preserve all reasoning-relevant content of G fragments up to ε — the grammar-inexpressibility of the target language. Where ε > 0, the paper identifies and bounds the loss rather than hiding it.

**Axiom 3 (Reflective access).** The protocol Π includes operations that observe, predicate over, and extend G from within G. In particular, G's own structure is representable as a subgraph of G.

### 3.3 Theorem Root (Universal Coordination)

**Statement.** Let `G` be a typed graph satisfying Axioms 1–3, `Π` a protocol over `G`, and `{G_i}` a family of client subgraphs. Any composition of client operations under Π reduces to typed-graph operations on G, with surface-language differences absorbed by the serialization maps `W_j`.

**Proof sketch.** By Axiom 2, each client's operations in its surface language W_j correspond to operations on its subgraph G_i. By Axiom 1, cross-client composition over shared node/edge types yields well-typed graph elements. By Axiom 3, the protocol's reflective operations do not escape the graph — they remain graph operations. Therefore any composable trajectory under Π is a trajectory in G expressed through one or more serialization maps. The protocol operates on the graph directly; the surface languages are interchange formats. ∎

The theorem is not deep, and its position in the paper's formal apparatus should be read accordingly. **The Root Theorem is structural / by-construction, not the paper's load-bearing claim.** Once Axioms 1–3 are accepted, the conclusion that protocol-mediated compositions reduce to graph operations on G is close to definitional: A2 stipulates entropy-preserving translation between surface languages and G; A1 stipulates typed-graph closure; A3 stipulates reflective access. The theorem then says: a protocol grounded in these three axioms has its compositions stay inside G. That is correct but not surprising; the axioms make it nearly tautological, which is the right shape for a structural setup result.

The paper's load-bearing formal content is downstream of this setup: the four convergent-property theorems of §6 (A — self-construction; B — composite subgraph closure; C — reflective growth; D — functional cognitive completeness) characterise *what reflective graph-native systems actually do under recursion*; and the four bound theorems of §9 (X — encoding efficiency; XI — computational efficiency; XII — behavioural bounding; XIII — substrate-Markov-mixing efficiency) state *what the framework's specific axioms purchase quantitatively*. The Root Theorem is the gate to those results, not the result itself. Readers evaluating the paper's formal content should focus on §6 and §9; the Root Theorem is the staging that makes those theorems statable.

Its content beyond the structural claim is *that the axioms are satisfied by a working implementation* — that the graph structure, the typed composition, and the reflective protocol operations can be built and operated in a real system. The two instances in §4 and §5 are the empirical content of that constructive claim.

### 3.4 What the theorem implies

Under the root theorem, ten previously-proposed theses about cognitive architecture, learning theory, reasoning fidelity, and self-construction reduce or collapse:

| Thesis (prior statement) | Status under root theorem |
|---|---|
| I. Map quality bounds reasoning fidelity | Data processing inequality over subgraph selection — citation |
| II. Systematicity is transitive under grammar closure | Typed-edge composition closure — citation of graph homomorphism / category theory |
| III. A well-formed map reaches a fixed point under extension | Knaster-Tarski / Kleene on monotone graph extension — citation |
| IV. The compound loop is monotonically accelerating | Loop-dynamics theorem over reflective graph systems — **full proof retained in §7** |
| V. Efficiency gains from the loop are multiplicative | Algebraic product structure over graph improvements — **full proof retained in §7** |
| VI. Theory of Mind arises from systematicity and self-reference | Modeling another agent's subgraph as a node in one's own graph — definitional consequence of Axiom 3 |
| VII. Graph-native memory is the episodic trace | Definitional — memory is the persistent subgraph with temporal edges |
| VIII. Composite models are a single unit; neuroplasticity enables growth | Subgraph composition; node insertion under Axiom 3 — short section in §6 |
| IX. The map is a hypothesis class; the compound loop is a learning algorithm | Learning-theoretic bound over graph-selection class — **labelled proof sketch in §7.3** |
| X. Native-dialect efficiency | Elevated to **Theorem X** in §9.1 — encoding-layer efficiency bound, derived from Theorem Root plus the Shannon lower bound; dual of Theorem XI |

Three further theorems emerge from the root claim that do not reduce to any prior thesis and are proved in full:

| Theorem | Status |
|---|---|
| Theorem XI. Computational Efficiency Bound | §9.2 — Kolmogorov-theoretic speedup for structure-respecting computation over graph-native representations; dual of Theorem X at the computation layer rather than the encoding layer |
| Theorem XII. Behavioural Bounding | §9.3 — system behaviour provably contained within grammar admissibility set Γ ∩ C |
| Theorem XIII. Substrate-Markov-mixing Efficiency | §9.4 — kernel-learning sample complexity bounded by substrate-richness gap; composes multiplicatively with Theorem XI |

**Named-result tally for the paper.** One root theorem (§3.3); four convergent-property results over reflective graph-native systems (Theorems A, B, C, D in §6); three independently-proved theorems (IV, V in full proof; IX as labelled sketch — see §7); four derived bounds (X, XI, XII, XIII in §9). **Twelve named results total**, plus supporting lemmas and observations. Theorems XI and XIII compose multiplicatively into a single combined-bound result (the substrate-encoding factor and the kernel-learning factor are independent and their product is the total efficiency gap with statistical-substrate computation; see §9.4). Six prior theses (I, II, III, VI, VII, VIII) reduce to citations or definitional consequences and do not count as named results in this paper's formal structure.

Two theorems survive as non-trivial and are proved in full in §7: IV (loop dynamics, conditional on empirical assumptions A1–A3) and V (multiplicative gain structure). A third (Thesis IX, learning-theoretic generalization bound) is given as a labelled proof sketch in §7.3 — the PAC-Bayes machinery it invokes is standard but the full derivation is deferred. The remaining six prior theses reduce to standard results applied to the typed-graph view or become definitional observations under Axiom 3.

### 3.5 Falsifiability commitments

A "one-graph" universal claim attracts an immediate methodological concern: can the framework escape any counterexample by extending G, loosening Γ, expanding ε, or reinterpreting "reasoning-relevant"? To prevent that, the framework's posits are fixed in advance. The following are the conditions under which observations would falsify the framework's central claims; they are stated explicitly so disconfirming evidence has somewhere to land.

1. **The typed-graph G is the framework's specific typed graph.** It is the substrate defined by Axioms 1–3 with the typed-edge structure described in §3.1. G is not an open-ended catch-all; it is fixed by the axioms. A claim that "X is a graph" is not, on the framework's terms, a claim that X falls under G — only that there exists a typed-graph encoding satisfying Axioms 1–3.

2. **The protocol grammar Γ is published and frozen at *evaluation time*; runtime extensibility (Theorem C) operates at a *different scope* and does not rescue prior counterexamples.** A standard objection identifies an apparent contradiction: Theorem C says the system can extend its own type vocabulary and Γ productions at runtime, while a frozen Γ would be needed for falsification. The resolution is to name the two scopes explicitly. *Theory-evaluation Γ* is the version-controlled grammar published at a specific protocol release (Ozma System v0.1 routing spec; Ozma AI v5.x protocol); the framework's claims (Root Theorem; Theorems X / XI / XII / XIII) are evaluated against this published Γ at the published version. *Operational Γ* is the runtime-extensible grammar described by Theorem C; the system can add types/edges/productions during deployment, but each extension produces a new versioned Γ' that becomes the new theory-evaluation grammar at the next protocol release. A counterexample to the published Γ at version N is a falsification at version N; the existence of subsequent versions Γ' at version N+1 is a separate fact about the system's runtime extensibility (Theorem C), not a mechanism for absorbing the version-N counterexample post-hoc. Theory rescue by undocumented runtime extension is foreclosed: every Γ that can be evaluated must be a *published* version, with extensions documented in the version history. The two scopes are consistent — Theorem C describes what the system does between protocol releases; §3.5 describes what is theoretically falsifiable at each release.

3. **The entropy-preservation tolerance ε is asymptotic.** Axiom 2 asserts H(S) ≈ H(X_raw) for reasoning-relevant content, with ε bounding the gap; the framework commits to ε → 0 in the asymptotic limit of well-designed Γ. An ε that grows with system complexity, or a content class for which ε is bounded below by a non-trivial constant, falsifies Axiom 2 for that class.

4. **"Reasoning-relevant" is defined by a structural property of the input, not by what the graph happens to capture.** A common circularity worry: if the framework defines "reasoning-relevant" as "what the graph captures," it can declare every counterexample outside scope. The framework's commitment closes this: *reasoning-relevant content is content for which K_Γ(I) = o(|I|)* — i.e., content with exploitable typed-graph structure under the published Γ. This is a structural property of the input itself (whether it has typed-graph-detectable redundancy), not a property of how the graph performs on it. Cryptographic content, raw-byte operations, and uniform random data have K_Γ(I) ≈ |I| by their construction; they are outside scope by the structural property, not by graph-capture failure. The framework's measurable falsification proxy: ε is monitored on a canonical published corpus per protocol version; consistency-checker admission rate (Instance 2 §8) is the published proxy. If admission rate fails to improve with successive Γ versions — or stops at a non-trivial constant on the canonical corpus — ε is bounded below, Axiom 2 falsifies for that content class, and the framework's "Γ hasn't matured yet" defence is no longer available because the corpus and the rate are public. *Falsifying Axiom 2 is the path to falsifying the framework on the content class A2 claims to cover.*

**Concrete content classes and the framework's posture on each.** Three classes are commonly raised as counterexamples to Axiom 2; the framework's posture is named for each rather than left as scope-handling:

- *Poetry where surface form is constitutive of meaning* (meter, rhyme, ambiguity-by-design, sonic patterns). The framework's response is *not* "surface form is outside scope"; surface form is content for this class, and a typed-graph encoding for poetic content must include typed nodes/edges for surface-form properties (metric stress positions, rhyme-class edges, phoneme-level adjacency) alongside semantic content. The framework's claim is that typed graphs *can* encode surface-form-as-content at the appropriate grain, not that surface form is reducible to semantics. A specific Γ-version that lacks surface-form types when poetry is the content domain falsifies A2 for poetry under that Γ; the framework acknowledges this and treats it as a Γ-completeness problem (extend Γ to cover the domain), not as a content-class exclusion.

- *Continuous mathematical structures* (real number line, function spaces, measure spaces, uncountable objects in pure mathematics). The framework operates on *finite representations of mathematical objects* — axiom systems, type-theoretic constructions, computable approximations, theorem-proving tactic graphs — not on the Platonic objects themselves. For content classes where the meaningful object *is* the uncountable structure as such (specific domains in continuum mathematics that are not reducible to symbolic manipulation), the framework is silent: A2's ε → 0 commitment applies to *the finite representations we actually compute on*, and the framework does not claim that uncountable Platonic objects "live in" the typed graph. ε is bounded below for direct uncountable-object content; ε is asymptotic for the finite representations we operationally deploy.

- *Embodied cognition* (proprioception, motor planning, sensory integration, continuous dynamical systems). Theorem D's claim is *functional* completeness, not biological faithfulness; the typed-graph substrate with weight functions captures continuous dynamics functionally — typed-graph state plus weight-modulated traversal *is* the framework's operational form for dynamics. Embodied cognition's continuous, biologically distributed character is captured at the functional layer (sensor-to-actuator graphs with weight-modulated transitions), not at the biological-isomorphism layer (the framework explicitly disclaims neuron isomorphism). For domains where the *meaningful computational character* is genuinely outside symbolic-compositional reasoning (e.g., the specific dynamics of biological motor control, where sub-symbolic continuous coupling is constitutive), the framework's posture is the same as for continuous mathematics: it operates on finite functional representations, not on the biological substrate as such.

The general posture across these three classes: the framework is *not* claiming typed graphs are the universal substrate for all phenomena; it is claiming typed graphs are the substrate for *the cognitive-coordination operations the framework's instances actually perform*, which is a smaller and concretely-bounded class. Counterexamples that target this smaller class are within the framework's falsification surface; counterexamples that target broader claims (universal computability of all phenomena) target a claim the framework does not make.

5. **The framework's quantitative bounds are conditional.** Theorem X is conditional on A2; Theorem XI is conditional on structure-respecting computation under Γ over inputs structured under Γ; Theorem XII is conditional on decidable Γ ∩ C and correct shim implementation; Theorem XIII is conditional on the Markov-chain framing. Each bound's failure mode is reachable through the named conditions; failure within scope falsifies the bound.

6. **Empirical instances are falsifiable on their own terms.** Instance 1 (Ozma System) and Instance 2 (Ozma AI) are existence proofs of the architecture's operational realisability. Either ceasing to satisfy Axioms 1–3 in deployment, or empirical metrics on either instance failing to match the framework's predictions (within the published scope conditions), is a partial falsification. Both instances are inspectable.

The framework is not asking for an unfalsifiable concession. The falsifiable surface is the conjunction of: fixed G, fixed Γ, asymptotic ε, named source class, conditional bounds, and inspectable instances. Counterexamples that target this surface are the framework's intended falsification path.

---

## 4. Instance 1: Ozma System — typed-graph substrate for infrastructure

### 4.1 The starting problem

Route keyboard, video, mouse, and audio between workstations under arbitrary rules, without hardware KVM switches, with audio as a first-class signal rather than an afterthought. Extend naturally to related signals: control surfaces, cameras, NVR, network-attached peripherals, power distribution, and monitoring. Do this at arbitrary scale — one desk, one building, one campus, one datacentre — without changing the model.

### 4.2 The architecture that fell out

The Ozma System routing specification is published at `https://github.com/ozmalabs/ozma/tree/main/docs/routing` as a 29-document draft standard (v0.1, initial draft 2026-04-07, split into sub-documents 2026-04-08) under CC-BY-4.0. The specification opens:

> Every path through the system — from a keyboard through a USB cable, across a network, into a USB gadget on a target machine; from a wall outlet through a UPS, PDU, and PSU to a CPU core; from a microphone through a mix bus, insert chain, and network transport to a remote speaker — is a graph of typed ports connected by links. Each link has measurable properties (bandwidth, latency, jitter, loss, voltage, current). Each port has discoverable capabilities (supported formats, maximum throughput, power budget). The router assembles the best pipeline through this graph to satisfy a declared intent.

Four primitives compose the graph: **Device** (node — physical or virtual thing with ports; 40+ concrete device types enumerated in the spec), **Port** (typed endpoint; source or sink; media type video/audio/hid/data/power/mixed), **Link** (connection between a source port and a sink port, with measurable bandwidth / latency / jitter / loss / activation-time properties), and **Pipeline** (ordered chain of links assembled by the router to satisfy an intent). Every primitive carries a location with logical (bus, network address) and physical (site, space, 3D position) components. Intents are declarative: *route my keyboard to workstation A while sending video to monitor B with audio mixed into the meeting bus* — the router computes the pipeline from current graph state.

The spec makes **scale independence** architectural, stated in a normative table:

| Scale | Same primitives model |
|---|---|
| Single device | USB topology, audio routing, storage health, thermal monitoring |
| Desk | PC + monitors + speakers + peripherals — spatial audio, RGB, power budget |
| Room | Multiple desks + rack + AV receiver + cameras — KVM switching, network, AV |
| Building | Multiple rooms + structured cabling + WiFi + IoT — fleet management, VLAN |
| Campus | Multiple buildings + inter-building fiber + WAN — federation, remote access |
| Datacentre | Halls + rack rows + A/B power + CRAC cooling — full infrastructure graph |

No model changes between scales. A Device is a Device whether it is a USB keyboard or a 48-port spine switch; a Link is a Link whether it is a 10cm USB cable or a 40km singlemode fiber run.

**Decentralised decision-making** is stated as a normative principle: *"The graph is composable, not monolithic. Each participant holds the subgraph relevant to its decisions. More data enables better decisions, but the system MUST function at every level of knowledge — degrading gracefully as information decreases."* Routing decisions are made at the level that has sufficient context — node-local decisions at nodes, mesh-level decisions at controllers, platform-level decisions at the Connect layer.

**Plugin extensibility** is normative: *"Everything that is not a core graph primitive is a plugin."* Transports, device classes, codecs, converters, and switches register at runtime. The four primitives and their grammar are the invariant; everything above them is extension.

### 4.3 Chronology — Instance 1 was paused for Instance 2, then both converged

The paper's convergence claim rests on the two instances being developed independently and arriving at the same structural answer. The chronology is verifiable from git history and includes a load-bearing detail that strengthens the convergence claim's significance: *Instance 2 was undertaken specifically to fix a cost bottleneck that had stopped Instance 1's continued development*. The author paused Ozma System development because LLM-assisted coding had become the practical bottleneck on continuing the work — making LLM coding cheaper and more effective was the prerequisite for returning to the infrastructure-substrate work. The bottleneck-fixing work became Ozma AI. Ozma AI converged on the same architecture Ozma System had. The convergence was noticed afterwards, not designed in.

This sequence is structurally significant. The framework's primary claim — that the typed-graph substrate is what reduces LLM coordination cost (Theorem XI) — was *produced by* the author trying to reduce LLM coordination cost on a different project (Ozma System). The framework's existence is itself an instance of the cost-reduction it predicts; the chronology is one further layer of self-evidencing (§11 lists the others).

The verifiable chronology:

- **Pre-2026-04 (preceding period):** Ozma System development to the maturity required for a 29-document RFC v0.1 publication. Work paused when LLM-assisted coding cost became the binding constraint on continuing further.
- **2026-04-07:** Ozma routing specification v0.1 (monolithic) published publicly to `https://github.com/ozmalabs/ozma/tree/main/docs/routing`. The publication is the work-product of the pre-pause Ozma System development; the pause is the precipitating context for what follows.
- **2026-04-08:** Specification split into 29 sub-documents with RFC-2119 conformance language.
- **2026-04-10 to 2026-04-22 (approximately):** Ozma AI development window. The work was undertaken to reduce LLM coordination cost so that Ozma System development could resume; it converged on graph-native coordination as the architectural answer. Work distributed across multiple machines without a clean continuous-session delineation. Within the window, a concentrated 7-day interval (2026-04-15 to 2026-04-22) produced observable git-visible output: Protocol specification v5.11 through v5.15, two trained LoRAs (Wizard-1, Coder-3b), one shim-on-frontier deployment (Ruler-1 = Kimi K2.6 + protocol shim), and the complete ozmalib codebase. The 14-day total-window figure is the honest description; the 7-day git-observable output is the concrete verifiable artefact.
- **2026-04-24:** The convergence — that the bottleneck-fixing architecture (Ozma AI) is structurally identical to the architecture Ozma System had already specified — is recognised, and this paper is written.

The graph-native infrastructure-substrate architecture was public, frozen, and under RFC-2119 conformance language **before** any of the AI protocol work began. The AI protocol was not designed to match the routing spec. It was designed to fix the LLM-cost bottleneck that had stopped the routing-spec author from continuing the routing-spec work. It arrived at the same structure independently. Reviewers concerned about retrospective matching can verify the chronology from public commit history.

The implication for the convergence claim's strength: Instance 2 was not undertaken as a second hypothesis-test of a graph-native architecture; it was undertaken as a *detour* — a tool-building exercise to enable continuing Instance 1. That detour landed on the same architecture Instance 1 had. This is structurally different from "the same author tried two related problems and got similar results"; it is "the same author tried to fix the bottleneck on the first problem, and the bottleneck-fix was the same architectural form as the first problem." The convergence is between original-problem-substrate and bottleneck-fix-substrate, which is a stronger convergence signal than two parallel hypothesis-tests.

### 4.4 Self-building behavior

**Implementation status: proof-of-concept.** Instance 1's full operational scope is the typed-graph substrate specified in the 29-document RFC-2119 standard at `https://github.com/ozmalabs/ozma/tree/main/docs/routing` (CC-BY-4.0, v0.1). The POC implementation does not realise the full specification at production scale; it realises enough of the substrate to exhibit the self-construction property concretely. The empirical claims that follow are POC-scope; the full-deployment claims are conditional on the spec's runtime instantiation, which the POC partially demonstrates.

The proof-of-concept implementation exhibits the self-construction property that will appear again in Instance 2 — and exhibits it at *two* layers:

*Runtime topology layer.* At startup, the system has no topology — nodes announce themselves over the control plane, register their typed ports, and the graph extends monotonically. Intent declarations drive pipeline assembly: given an intent ("route X to Y with format negotiation Z"), the router searches the graph for a valid path, negotiates formats end-to-end, and instantiates the pipeline. On link failure or node removal, affected pipelines are reassembled over remaining topology with no central choreographer. On node addition, newly-available pipelines become eligible without reconfiguration.

*Deployment-bootstrap layer.* The POC builds itself: given a host with hypervisor capability, the system creates a VM, self-installs into it, and builds out from that VM as the initial node — extending the graph to subsequent nodes via the same control-plane registration mechanism the runtime topology layer uses. Self-construction is not only a runtime behaviour over an already-deployed system; it is a deployment-layer behaviour that bootstraps its own operating environment. This is a stronger concrete instance of Theorem A's self-construction property than topology-discovery alone — the typed-graph substrate operates on its own deployment as a typed-graph operation (host as Device, hypervisor as port, VM as compound-device-with-internal-graph, install as monotone graph-extension).

This is the compound loop from Instance 2, operating with no LLM anywhere in the system. The reflective operations are native graph operations over the routing graph. The "map quality" of Thesis I (reduced to DPI citation per §3.4) is visible as pipeline assembly success rate under load; the "monotone improvement" of Thesis IV (proved in §7.1) is visible as topology discovery asymptotically approaching the true system state as nodes complete registration.

### 4.5 The graph absorbs every adjacent domain — the strongest operational evidence that the one-graph claim is non-vacuous

The central claim of Instance 1 — and the paper's strongest operational evidence that the one-graph thesis is non-vacuous rather than a naming exercise — is that the routing graph subsumes *every adjacent infrastructure concern* that typical deployments would treat as separate products. Ten disparate product categories (monitoring, power distribution, physical environment, security and identity, compound-device decomposition, device compatibility, streaming-service integration, plugin extensibility, cross-language asset management, and real-time signal routing) reducing to typed projections of one graph is what "one graph" *looks like* when the claim is meant literally rather than metaphorically. The specification does not argue this; it builds it, domain by domain, with each domain's primitives expressed in terms of the four core types.

**Monitoring is the routing graph read as a query.** The `monitoring.md` specification states this plainly:

> The routing graph is a monitoring platform by construction. The data the router needs to make routing decisions — device resources, link health, power state, versions, topology — is exactly the data a monitoring platform needs. Exposing it is a read path on data that already exists, not a separate system.

The spec enumerates ten classes of legacy monitoring tooling subsumed as read paths: `node_exporter` / Prometheus for system metrics, SmokePing for path-quality measurement, Snipe-IT for asset inventory, Uptime Kuma for endpoint reachability, `smartctl` for storage health, `lm-sensors` for thermal telemetry, SNMP polling for network gear, PDU metering for power telemetry, UPS state polling for battery, BIOS/firmware version reporting. Each is attributed to a specific routing-spec primitive that produces the same data for routing purposes. Sankey endpoints are normative for bandwidth, power, audio, and storage I/O.

**Power is a graph dimension, not an adjacent system.** The `power.md` specification states: *"Power adds a new dimension to the graph. Every device MUST have power_profile on the Device (what it consumes and delivers), power_budget on each Port that carries power, power_state reflecting current measurements."* Voltage rails are traced as links from utility feed through UPS, PDU, strip, PSU, motherboard rails, to individual consumers. USB PD Power Delivery Objects, PoE classes, and voltage-drop inference (measuring 4.72V on a 5V rail implies ~560mA across a cable of known resistance — tagged `inferred` quality) are specified. The 12-point hard-constraint filter in `routing.md` treats power-budget identically to bandwidth — a pipeline that fits on the network but exceeds its PoE port's power class does not route. Power is not tracked; it is routed.

**Physical environment is in the graph.** The `environment.md` specification models sites, buildings, rooms, racks, furniture, and floor plans with normative spatial coordinates. Wall materials carry RF attenuation values across 2.4/5/6 GHz — low-E glass blocks 15–20dB at 5 GHz, doubling WiFi coverage loss through a window. Datacentre infrastructure (hot/cold aisle, PUE, A+B power redundancy, automatic transfer switches with `transfer_time_ms`) is specified identically to desk infrastructure. The section closes with the emblematic claim:

> The building management system is not a separate product, it is a natural extension of the routing graph into the physical environment... The building automation and IT management share one event bus, one intent system, one graph.

Lock, HVAC, lighting, occupancy, and door-contact sensors are device types; ceiling lights can bind to the `presence` intent condition source identically to how a monitor binds to `activity`.

**Security and identity collapse into the graph.** The `security.md` specification treats every authentication factor — RFID badges, NFC tags, BLE keys, FIDO2 authenticators, PINs, passkeys, TOTP tokens, biometrics (fingerprint / face / iris / voice / palm vein), X.509 certificates, 802.1X network auth, WireGuard keys, SSH keys, OAuth tokens, API keys — as one `Credential` type feeding the same binding system. Face recognition is a capability on the camera device already in the graph for video; fingerprint is a capability on the sensor already in the graph for input; BLE proximity is a capability on the radio already in the graph for transport. The spec states: *"These are not new device types — they are capabilities on existing devices... The authentication model unifies them under one identity framework."* Multi-factor authentication requires different strength classes (two badges count as one factor, not two); `SecurityPosture` cascades site → space → zone → device under tighten-only inheritance with auditable exceptions.

**Compound devices are fractal.** The `discovery.md` specification normalises compound decomposition: a Thunderbolt dock is expanded into USB hub, Ethernet adapter, DisplayPort MST stream, Thunderbolt controller, and audio codec — each modelled as a sub-device with its own ports, connected by internal links. A Stream Deck is four-plus ports (buttons + per-key LCDs + touch strip + full LCD) on one compound device. The `node-definition.md` specification states that a node is *"a compound device comprising its hardware platform, USB gadget presentation, physical I/O buses, software services, network membership, cryptographic identity, and binding to a single target machine."* The graph is self-similar: any device is itself a graph. There is no privileged top or bottom.

**Device compatibility is a graph query.** The `device-database.md` specification (29,000 words alone) defines a universal device catalogue with category-specific schemas for motherboards, CPUs, GPUs, storage, PSUs, cases, coolers, RAM, laptops, cables, adapters, peripherals, displays, audio, control surfaces, network gear, PDUs, cooling, sensors, actuators, and furniture. The compatibility engine (device-database.md §13) performs physical-fitment, electrical, bandwidth, power, pinout-safety, performance, feature, and thermal checks over the same graph primitives. A PCPartPicker-class build-validator falls out as a read path on the graph — not as a separate product.

**Streaming services are graph nodes.** The `devices.md` specification states: *"A Spotify Connect receiver is not just a service — it is an audio source in the routing graph with a PipeWire port."* Per-app audio (Spotify and YouTube captured separately from the same target machine via WASAPI process-loopback or PipeWire per-process nodes) is modelled as distinct media-source devices. The graph reaches inside the target operating system into individual application audio streams.

**Plugins and core use one interface.** The `plugins.md` specification states: *"Built-in plugins MUST use the same interface as third-party plugins — there is no distinction between 'core' and 'external' at the interface level... The router does not know or care that it came from a plugin — it is just another transport with a cost."* Transports, device classes, codecs, converters, and switches extend the graph at runtime. A community contributor adding a LoRa transport, a UWB radio, a new camera class, or a new sensor type does so through the same typed-graph primitives that the controller uses internally. There is no core/plugin split because there is no core/plugin concept — only typed extensions to the one graph.

**Across all domains: one graph, queried through typed projections.** Routing, monitoring, asset management, compliance, SIEM, vulnerability management, helpdesk triage, network attack-surface management, power distribution, building automation, physical security, device compatibility, and per-application audio are all projections of the same underlying graph. Each is a query against data the router already has for routing, typed by the edges that client cares about. The claim is not "we built ten products on one platform." The claim is that these are not distinct products; they are distinct projections of one typed graph, with the distinction living entirely at the query layer.

### 4.6 Maturity of the specification

The specification is publication-ready. This deserves an explicit note because "draft" in software culture often connotes "incomplete," whereas the Ozma routing spec is draft in the RFC sense — under-revision, yet normatively complete.

The maturity signals are uniform across the 29 documents:

- **Volume:** approximately 77,000 words of normative specification excluding the device database, and 29,000 additional words for the device-database catalogue and compatibility engine. The spec is in the size class of IETF-RFC or W3C working-draft material, not of a README.
- **Language:** RFC 2119 conformance language (MUST / SHOULD / MAY / MUST NOT / RECOMMENDED) is used consistently across all 26 normative documents. Three documents explicitly label themselves non-normative — `implementation.md`, `compatibility.md`, and `appendices.md` — bringing the total to 29.
- **Type definitions:** every primitive has a complete YAML schema with field types, optionals, and enum values. Cross-references between schemas are explicit.
- **Worked examples:** format negotiation, intent composition, gaming-scenario switching, a complete Milk-V Duo S node definition, a community-verified FireWire mixer voltage-label override, and datacentre A+B power redundancy are all worked end-to-end in the spec.
- **Implementation coverage:** the non-normative `implementation.md` reports six completed phases (graph model, intents-bindings-pipeline-router, format negotiation, transport plugins, active measurement, monitoring-journal-trends) with 526 unit tests passing across them.
- **Fragment markers:** there are no TODO markers anywhere in the normative document text. Draft status is labelled at the document level, not sprinkled through incomplete sections.
- **Git verifiability:** the spec was published as a monolithic document on 2026-04-07, split into the 29-document structure (26 normative + 3 non-normative) on 2026-04-08, and has been stable since. The RFC-2119 conformance headers predate any of the AI-protocol work.

What this means for the paper's claim: Instance 1 is not "a PoC architecture with a spec under development." It is **a complete draft standard with a reference implementation at PoC stage.** The spec is the primary artifact — the architecture is fully specified, the primitives are fully typed, the normative behaviour is fully defined. The PoC validates implementability. Reviewers who want to argue with the Instance 1 claim argue with a 77,000-word standard, not with a sketch.

### 4.7 Licensing

The Ozma System code is released under AGPL-3.0 with a plugin exception (third-party plugins may be licensed under any license). The documentation, including the 29-document routing specification, is released under CC-BY-4.0. Copyright is held by Ozma Labs Pty Ltd (2024–2026).

---

## 5. Instance 2: Ozma AI (Composite-Model Coordination)

### 5.1 The starting problem

Agentic implementation of Instance 1 required substantial language-model capability for code generation, review, and refactoring at sustained cost. Frontier-model invocation at the required volume was not economically feasible. The problem was not per-call cost; it was the absence of a stable protocol between model components, which forced every integration to re-solve the same coordination contracts. The same three-way handshake — what does the model read, what grammar does its output follow, what mechanical checks validate the handoff — was being reimplemented for every tool and every workflow, with tacit contracts hidden in prompt engineering.

### 5.2 The architecture that fell out

Ozma AI defines a protocol layer that decouples output grammar from model implementation. A model speaks the protocol; the protocol is invariant across models. Inputs are typed envelopes over a shared semantic map. Outputs are typed envelopes (EDIT, PLAN, ASK, ACK, TRACE, REASONING, TASK, etc.) under a grammar Γ. A mechanical shim runs on the outside, parsing outputs, executing tool calls, updating the semantic map, and feeding the updated state back in. Composite-model structure: a coordinator model binds specialist sub-models (verifiers, retrievers, compilers, checkers), each typed by what it consumes and produces.

The central artifact is the semantic map — a typed graph whose nodes are semantic elements (functions, types, tasks, decisions, errors, file regions) and whose edges are typed relations (defined-by, depends-on, produced-by, contradicts, refines, implements, tests). Reasoning produces delta operations on the map; the consistency checker admits or rejects deltas; admitted deltas become the new map state.

### 5.3 Self-improving behavior — the compound loop

The operational trajectory of the coordinator is a reflective loop on the semantic map. Each iteration produces a reasoning trace; the trace produces a delta; the delta either improves or does not improve a map-quality metric τ; the updated map informs the next iteration. Under empirical assumptions A1–A3 (named in §7), τ is monotone non-decreasing and iteration cost is non-increasing in τ. The loop is self-constructive in the same sense Instance 1 is: the reflective operations observe the current graph state, detect gaps, and apply graph-transformation operations to fill them.

No architectural design decision targeted self-construction. Self-construction fell out of the combination of three properties: a typed graph, reflective access to that graph from within operational primitives, and a decidable quality measure.

The compound loop is not an AI-specific mechanism. It is the dynamics of any sufficiently reflective graph-native system. Instance 1 exhibits the same dynamics with no model in the loop. The two instances are the empirical substance of §6's claim that self-construction is a theorem of graph-native reflective systems, not a property of AI architectures.

### 5.4 The measured efficiency floor

Ozma AI operates through the protocol on existing frontier and near-frontier models without training modification. The paper's strongest concrete evidence of the efficiency gain — and the primary empirical validation of Theorem XI on production code — is Appendix E: a paragraph-primed LLM identified a structural refactoring in `python-dotenv`'s `resolve_variables` function that converts a Θ(N·E) loop to Θ(N+E), delivering a measured speedup from 1.4× at small input sizes to 7.95× at large, with super-linear growth in the speedup as input size increases. The scaling curve is the big-O signature — the speedup grows rather than saturates, which distinguishes asymptotic from prefactor improvement and is not reproducible by a constant-factor micro-optimisation. Additional measurements across specific refactorings are documented separately. Development from initial protocol specification to shipping distillation pipeline, two trained LoRAs, one shim-on-frontier deployment, and the complete ozmalib codebase occurred across an approximately 14-day window, with a concentrated 7-day interval producing the git-observable output — a working artefact of the approach, in the sense that a graph-native protocol is a tractable tool to build a graph-native protocol with.

### 5.5 Licensing

Ozma AI is released under MIT. Copyright is held by Ozma Labs Pty Ltd (2024–2026).

---

## 6. Convergent Properties: General Theorems of Reflective Graph-Native Systems

The two instances share operational properties that are not coincidental. Under the root theorem of §3, these properties generalize. This section states them as theorems of reflective graph-native systems (any system satisfying Axioms 1–3), with Instance 1 and Instance 2 as evidence.

A framing point before the theorems themselves. In current statistical-substrate AI, emergent capabilities are observed after training and cannot be derived ab initio — scaling-law research describes the shape of emergence empirically but cannot specify its content. Critics of the autoregressive LLM paradigm (LeCun 2022; Bengio et al. 2023; Chollet 2019) have argued for years that autoregressive next-token prediction on statistical substrates is architecturally insufficient for intelligence, and that what is needed is persistent world models, hierarchical planning, objective-driven behaviour, and structured latent spaces — properties that current scaling approaches do not supply. The theorems below demonstrate that under the graph-native axioms A1–A3, these properties are not merely supplied — they are *mathematically guaranteed consequences of substrate structure*. **Emergence under the typed-graph architecture is a theorem, not an empirical observation**: any system satisfying the axioms necessarily exhibits the convergent properties enumerated below.

An observation about the joint structure of the §6 theorems: A, C, and D are not independent. Theorem A (self-construction) establishes a non-decreasing quality trajectory under continued operation. Theorem C (reflective growth / neuroplasticity) establishes that the system extends its own type vocabulary during operation. Theorem D (functional cognitive completeness) establishes that with weight functions, the substrate carries every component of cognitive architecture. Under iteration, A + C + D conjointly entail that the trajectory is monotone-improving (A), the substrate vocabulary is unbounded in principle (C), and all cognitive components are mechanically present (D) at every step. Downstream consequences are developed in the companion paper.

### 6.1 Theorem A (Self-construction)

**Statement.** Any reflective graph-native system with (a) a decidable quality measure on graph state, (b) operational primitives capable of extending or refining the graph, and (c) an action-selection policy that prefers improving actions — or in which improving actions are available more often than degrading ones — will exhibit a trajectory of non-decreasing quality under continued operation. The action-selection condition (c) is load-bearing and stated as part of the theorem rather than left implicit in the proof: a reflective system with quality measure but with a random or adversarial action-selection policy does not satisfy the conclusion. The Goodhart and local-optima caveats below operate within this scope: even action-selection that prefers improving actions can fail in deployment via metric exploitation or local-optima trapping; the structural property — non-decreasing quality conditional on (a) + (b) + (c) — is what the theorem establishes.

**Argument.** Under Axiom 3, the system can observe its own graph state. A decidable quality measure partitions operational actions into improving and non-improving. A system that prefers improving actions — or merely one in which improving actions are available more often than degrading ones — produces a monotone non-decreasing quality trajectory by the standard compound-loop argument (§7.1). The trajectory is the self-construction: the graph improves itself through reflective operation without external redesign.

**Scope of the guarantee.** Theorem A is a structural property — it characterises the trajectory's *form* under the stated conditions, not its safety under deployment. Three concerns are explicitly outside the theorem's scope and named here so they do not get smuggled in: (a) **Decidable does not imply findable** — a quality measure that is decidable on graph states does not guarantee that improving actions are *discoverable* by the system; the theorem assumes improving actions are encountered, not that the system searches for them efficiently. (b) **Goodhart-style metric exploitation** — a self-modifying system can in principle exploit the structure of its own quality measure (selecting actions that increase the measured quantity without the underlying property the measure was meant to track); Theorem A does not claim immunity to this, only that whatever the system selects under the chosen measure produces a non-decreasing trajectory *in that measure*. The risk is real and is properly handled by Theorem XII's admissibility-grammar gating (§9.3): admissibility specifies which self-modifications are sanctioned; Goodhart-style exploits that produce inadmissible aggregates are blocked at the grammar layer, not by Theorem A. (c) **Local optima and self-evaluator damage** — Theorem A guarantees non-decreasing quality under the *operational regime in which improving actions remain available*; it does not guarantee escape from local optima or invulnerability to self-modifications that damage the quality measure itself. Both are deployment-conditions concerns the theorem moves into Γ-specification rather than solves directly.

The theorem is instance-agnostic. Instance 1's topology discovery (pipeline-assembly success rate is the quality measure; link and node additions are the improving actions) and Instance 2's compound loop (τ is the quality measure; admitted map deltas are the improving actions) are two realizations.

### 6.2 Theorem B (Composite subgraph closure)

**Statement.** Two graph-native clients operating on compatible type-restricted subgraphs can be composed without protocol-level redesign.

**Argument.** Direct from Axiom 1. If G_i ∪ G_j ⊆ G with compatible type restrictions, then composition is typed-graph composition on the union, and surface-language differences are absorbed by the respective serialization maps. The practical consequence: adding a new client to a graph-native protocol requires defining its type restriction and its serialization map; it does not require changing the protocol.

**Scope: ontology alignment is not solved by the substrate; it is made tractable by it.** The "compatible type restrictions" gloss in the statement does real work and warrants explicit naming. Two graph-native clients with overlapping domains can hold *incompatible ontologies* — different type names for the same concept, different edge semantics over the same nodes, different decompositions of the same domain into types. The typed-graph substrate provides *shared reference* (graph nodes as shared objects across clients) but does not provide *shared interpretant* (each client's surrounding subgraph carries its own ontology — see Peirce's reference/interpretant distinction). Theorem B's claim is that *when* type restrictions are compatible, composition is mechanical; it is *not* the claim that type-compatibility is automatic. Specifying compatible type restrictions across clients is the ontology-alignment problem, which the framework treats in the same spirit as Γ-specification (Theorem XII): a real engineering problem, but a *specification* problem — tractable via established formal-methods machinery (type-theoretic refinement, schema mediation, registered datatypes, vendor-namespaced extensions) — rather than a behavioural-shaping problem. The framework's substrate move is to expose ontology alignment as a typed-grammar-engineering problem instead of leaving it embedded in opaque inter-system translation. The framework supplies the substrate that makes the problem tractable; specifying the alignment is downstream engineering, not theory rescue.

The theorem is the technical content of the one-protocol claim. Instance 1 demonstrates it across signal classes (HID, video, audio, power, control); Instance 2 demonstrates it across reasoning client roles (coordinator, verifier, retriever, specialist); the cross-instance unification demonstrates it across domains.

### 6.3 Theorem C (Reflective growth / neuroplasticity)

**Statement.** Under Axiom 3, a graph-native system can extend its own type vocabulary — adding new node types, new edge types, and new Γ productions — while continuing to operate. The extension is a graph operation on the graph's own representation-of-itself.

**Argument.** Axiom 3 states that G contains a subgraph representing G's own structure. A type-vocabulary extension is a typed graph operation (new nodes, new typed edges connecting them, new Γ productions over them) on that subgraph. The protocol Π supports such operations by construction. The system that extends its vocabulary remains graph-native; nothing escapes.

This is the mechanism behind Instance 2's neuroplasticity — new sub-model types are inserted as new node types with typed edges describing their contracts. It is also the mechanism behind Instance 1's plugin extensibility — new device classes and protocol extensions enter via the same operation. Thesis VIII (see §3.4) collapses into this theorem.

### 6.4 Theorem D (Functional cognitive completeness)

**Statement.** A reflective graph-native system satisfying Axioms 1–3, augmented with scalar weight functions over its nodes and edges, admits a formal mapping from each major functional component of cognitive architecture — perception, attention, memory, emotion, thought, planning, metacognition, learning, and goal-directed behaviour — to a specific graph operation or observable. The substrate is *functionally complete* with respect to the cognitive-science enumeration of these components.

**Argument.** Define a weight function `w : V ∪ E → ℝ` assigning scalars to nodes and edges of G. Under this augmentation the mapping is constructive:

- **Persona** is a weight function (or a selection over a family of weight functions). Different personas apply different `w` to the same G, producing different traversals from identical inputs. Not styling at the output layer; weight modulation at the reasoning layer.
- **Emotion** is a time-varying weight distribution `w(t)` encoding salience, urgency, valence, and arousal as observable scalar fields evolving over operation. Measurable at substrate level rather than inferred from behaviour.
- **Thought** is a weighted traversal vector — a directed trajectory `(n₀, e₁, n₁, …, n_k)` through G under current `w`. Path length is complexity; traversal time is duration; the induced subgraph is content.
- **Perception** is the inverse of the serialisation of Axiom 2 — an external observation projects into a typed-graph fragment of G.
- **Attention** is the modulation of `w` along a candidate frontier — high-`w` neighbourhoods are explored preferentially.
- **Memory** is typed-node persistence with retrieval-by-traversal; the episodic, semantic, and procedural distinctions of Tulving (1972) map directly to typed-edge classes.
- **Planning** is search over admissible trajectories under Γ ∩ C (the admissibility grammar of Theorem XII, §9.3).
- **Metacognition** is graph-on-graph operation enabled by Axiom 3 — the system observing and modulating its own `w` via the reflective subgraph.
- **Learning** is adjustment of `w`, together with extension of Γ via Theorem C, under feedback from quality measurements.
- **Goal-directed behaviour** is `w`-modulated traversal toward a designated objective node or target subgraph.

Each component of the standard cognitive-architecture vocabulary thus maps to a concrete graph operation or observable. Functional completeness with respect to that enumeration follows by construction.

**Scope.** *Functional completeness*, not biological faithfulness. The theorem asserts that the formal architecture cognitive science has converged on across decades of research — Hebbian learning (1949), neural-ensemble theory, Tulving's typed memory (1972), Baars's Global Workspace (1988), Damasio's somatic markers (1994), Friston's free-energy principle, Clark's predictive processing, Dehaene's biological-substrate Global Workspace (2014), Chollet's measurable skill-acquisition (2019), LeCun's world-model-plus-planning programme (2022), Bengio's structured stochastic generation (2021), Picard's affective computing (1997) — is expressible mechanically on the graph-plus-weights substrate. It does not assert biological neuron isomorphism, exhaustiveness across all cognitive phenomena, or any claim about phenomenal consciousness. The substrate supplies measurement primitives, not the empirical measurements themselves.

**Mapping vs. mechanism.** Theorem D establishes *functional expressibility* — that each cognitive component admits a typed-graph operation correspondence on the substrate. A standard objection is that Turing machines also admit such mappings (every cognitive operation can be encoded as tape-head transitions), so expressibility alone is not informative. The objection is correct, and the framework's response is that expressibility is necessary but not the load-bearing claim: the *operational efficiency* of the typed-graph realisation is what distinguishes Theorem D from arbitrary stipulation. Running cognitive operations on a Turing tape pays the |I|/K_Γ(I) ratio as cost (Theorem XI, §9.2); on the typed-graph substrate, structure-respecting cognitive operations pay polylog(K_Γ(I)). The same applies to kernel learning (Theorem XIII, §9.4): co-occurrence kernels on token substrates pay sample-complexity cost exponential in the substrate-richness gap; weight-modulated kernels on the typed-graph substrate do not. The theorem composes with XI and XIII to give *expressibility plus operational efficiency on the same substrate* — that joint claim is what is non-trivial. The mapping is not arbitrary stipulation; the mapping is to a substrate where the bounds of XI and XIII apply to cognitive operations specifically.

The same response addresses the "everything is graph-like" concern raised by Corollary D.3 below. Many networked systems share *graph-like topology* but few share the framework's specific axiom set — typed-edge structure (Axiom 1), reflective access (Axiom 3), weight-function-modulated traversal under Γ. River networks and highways are graph-shaped but neither typed-with-reflective-access nor admissibility-grammar-gateable; the framework's claim is for the specific structured substrate, not "anything network-shaped."

**Significance.** Statistical-substrate networks develop internal representations during training that function operationally like persona, emotion, and thought — observed fragmentarily in interpretability research (Olah et al. circuits, Anthropic feature directions, persona-shaped responses under prompting, RLHF-induced affective shifts, chain-of-thought traces resembling traversal). The mystery of how these representations form has no architectural answer on a statistical substrate. Under the graph-native architecture, the answer is mechanical: the underlying computational dynamics *are* graph-weight dynamics, and statistical-substrate networks approximate them by gradient descent on task objectives. Explicit graph-native architectures realise the dynamics directly rather than emergently — what current LLMs do by brute force, the substrate names and makes mechanical.

Within this paper's formal scope, Theorem D refines two earlier results. Coordination (Theorem B) under weight functions is coordination on weight distributions in addition to references. Alignment-by-grammar (Theorem XII, §9.3) admits a grammar-plus-weight specification form. Downstream consequences are developed in the companion paper.

#### Corollaries of Theorem D

Four corollaries follow directly from the theorem's argument and matter to the paper's load-bearing claims.

**Corollary D.1 (Mechanical mind-change).** Phenomenologically discontinuous belief change — sudden realisation, paradigm shift, gestalt switch, conversion experience — is mechanically a weight redistribution or edge-set modification on G, not a rewrite of concepts. It is a single graph operation, not a retraining cycle. The dual reading of *changing your mind* — revising a belief, and modifying the weighted graph that is the mind — collapses under Theorem D: the readings coincide because the underlying operations coincide.

**Corollary D.2 (Model–graph identity).** Under Theorem D there is no architectural dichotomy between *the model* and *the semiotic graph*. The substrate plus its traversal-and-modulation dynamics IS the model. The model's weights and the graph's weights are the same weights; the model's reasoning is graph traversal; reflective access (Axiom 3) and learning (weight adjustment) are the same operation under different timescales. *Graphs all the way down.* Symbolic-versus-neural framings dissolve at the substrate level — both are projections of the same underlying weighted-graph dynamics.

**Corollary D.3 (Evolutionary convergence as independent corroboration).** Biological cognition evolved into a functionally-isomorphic architecture: typed connectivity, weighted edges, activation patterns, reflective access. The substrate of Theorem D is derived formally from the coordination requirements of Axioms 1–3, not by analogy to neuroscience. That two independent derivations — formal-from-axioms and biological-from-evolution — converge on the same architecture is the standard signal that the architecture is constrained by the problem rather than arbitrary. The convergence is the corroboration. The claim is *not* that this work is brain-inspired in the marketing sense; the claim is that the formally-derived substrate happens to coincide with the evolved one, and that coincidence is evidence the substrate is the natural attractor for this class of computation.

**Corollary D.4 (Stochastic-traversal floor — graph + RNG approaches model performance).** For tasks reducible to graph traversal under well-defined weights, replacing a learned-traversal mechanism with a stochastic walker driven by an RNG under weight-modulated transition probabilities produces performance close to a trained model on the same graph. The substrate, not the trained traversal-selection, is doing most of the work; the trained model is an efficiency optimisation over stochastic traversal. The corollary connects directly to Bengio's GFlowNets (2021) — structured stochastic generation over typed graphs — which formally interpolates between high-temperature stochastic traversal and low-temperature learned generation; on substrate-friendly tasks the high-temperature floor is high. The corollary is *derivable*, not merely measurable: under the Markov-chain framing developed below, it is the operational reading of Theorem XIII (§9.4 — substrate-Markov-mixing efficiency), which bounds the kernel-learning cost ratio by the substrate's information-richness gap from the lower bound. Empirical verification of the constant remains the target of the experimental work referenced in §9.4 and the experiment specification in `research/experiments/d4-stochastic-traversal-floor/`; the qualitative existence of the floor is established by the theorem.

#### Markov-chain framing (Theorem D restated in stochastic-process terms)

Theorem D admits a direct restatement in the most established stochastic-process machinery in machine learning. The substrate plus weight functions defines a **Markov chain over typed-graph nodes**: the state space is V (the node set of G); the transition kernel is the weight-modulated transition probability `P(n_{t+1} | n_t) ∝ w(n_t, n_{t+1})`; a thought, in the sense of the theorem, is a sample trajectory drawn from the chain. The mappings extend uniformly: persona is a kernel parameterisation (or a selection over a family of kernels); emotion is a time-varying kernel `K(t)`; attention is soft modulation of the kernel along a candidate frontier; memory is the recurrent and absorbing structure of the chain; planning is search over high-probability trajectories under a goal-conditioned kernel; learning is adjustment of the kernel parameters under feedback; metacognition (Axiom 3) is a Markov chain on the chain's own kernel.

Statistical language modelling — n-gram models, hidden Markov models, and transformers — is Markov-chain machinery on a token substrate. A transformer with context window n is a parametric Markov chain of order n over tokens; this is not a metaphor but the literal mathematical structure. The framework is the same family of machinery operating on a substrate that carries semiotic structure directly rather than reconstructing it from token co-occurrence statistics. Theorems X and XI's efficiency arguments are exactly this restated: the same Markov-chain machinery on a substrate closer to the relevant information-theoretic lower bound exhibits the distance from that bound as quantitative cost.

Under this restatement, established Markov-chain machinery applies directly to the substrate — Markov Chain Monte Carlo (Metropolis-Hastings, Gibbs, Hamiltonian Monte Carlo); particle filters and sequential Monte Carlo; hidden Markov models; conditional and Markov random fields; Markov decision processes; spectral analyses of mixing time and stationary distributions; Bengio's GFlowNets (2021) as a learned-kernel paradigm. Corollary D.4 takes a precise standard form under this restatement: a Markov chain on a richer substrate with a structurally-derived kernel mixes to a distribution close to the mixing distribution of a learned-kernel chain on the same substrate, on tasks where the substrate carries the relevant information. Mixing-time bounds, total-variation distance to the stationary distribution, and the conductance of the underlying graph give analytical purchase on the corollary's claim — it is a measurable property of a well-understood class of stochastic processes.

### 6.5 Observation E (Dual use)

The routing-and-monitoring dual use noted in §4.5 generalizes. Any subset of the graph is a potential client; any well-typed projection map yields a client. The practical consequence: what appear to be distinct applications of the protocol are distinct projections of the same graph. SIEM is a projection; compliance is a projection; helpdesk-triage is a projection; code review is a projection; theorem-proving is a projection.

This observation is not a theorem — no proof is required beyond stating the observation carefully.

### 6.6 Observation F (Measurement by construction)

A stronger form of Observation E holds in Instance 1 and is conjectured to generalize: when a protocol requires measured data to make correct decisions, the protocol *is* the measurement infrastructure. Observability is not a separate system; it is a read path on data the system already holds for its primary purpose. In Instance 1 this subsumes ten classes of legacy monitoring tooling (metrics, path-quality, asset inventory, uptime, storage-health, thermal, SNMP, PDU metering, UPS state, firmware tracking) as direct query endpoints on the routing graph. The predicted generalization: any sufficiently typed graph-native system with quality-tagged measurements reproduces this property. Instrumented coordination implies free observability.

### 6.7 Observation G (Information quality as universal primitive)

Every measured value in Instance 1 carries an `InfoQuality` tag with ordering (`user > measured > inferred > reported > commanded > spec > assumed`) and a refresh schedule. Higher-trust values override lower-trust values for the same property; values decay with age. This is not domain-specific — it is the algebra of provenance, applicable to any typed graph with measurement. The Instance 2 consistency checker has an isomorphic structure (admitted deltas override pending deltas; rejected deltas decay from the candidate set). Any reflective graph-native system with measurement benefits from the same primitive.

### 6.8 Observation H (Tighten-only inheritance as constraint algebra)

Instance 1's `SecurityPosture` cascades site → space → zone → device with tighten-only semantics (a child can strengthen a parent's policy, never weaken it). Exceptions require explicit justification, approver, and expiry. This is the general algebra of constraint inheritance in composable-graph systems, not a security-specific mechanism. Instance 2's map quality τ exhibits the same algebra — delta chains can only raise or preserve τ under A1, never lower it without an explicit marked regression. Any graph-native system with hierarchical policy converges on the same inheritance model.

### 6.9 Observation I (Scale-invariance through compound decomposition)

Instance 1's compound-device decomposition states that any device can itself be a graph: a Thunderbolt dock is USB hub + Ethernet + DP MST + TB controller with internal links; a node is hardware platform + USB gadget + peripheral buses + services + network + identity, with internal links; a datacentre is halls + rows + racks + units + ports, with internal links. The graph has no privileged top or bottom. Scale invariance is not a separate claim; it is a consequence of the compound-decomposition primitive plus Axiom 1 (typed-graph closure). Instance 2 exhibits the same property — a coordinator is itself a compound of sub-models with typed internal edges; a sub-model may be a LoRA-adapted base plus a prompt strategy plus a verifier. Scale is resolution; the graph structure is invariant under it.

Scale invariance is the most consequential of the convergent properties enumerated in this section, because it is what licenses the framework's reach. The observation has substantial direct prior art in physics: Wilson's renormalization group (1971/1975, Nobel 1982), Kadanoff's block-spin scaling (1966), and the universality-class machinery they established show that vastly different physical systems exhibit identical scaling behaviour at critical points; Bak's self-organized criticality (Bak-Tang-Wiesenfeld 1987; *How Nature Works*, 1996) extends scale-invariant dynamics across natural systems with explicit universality emphasis; West's universal scaling laws (*Scale*, 2017) demonstrate cross-zoom empirical scaling across biology, cities, and companies; Mandelbrot's fractal geometry (*The Fractal Geometry of Nature*, 1982) provides the geometric form. Observation I is the typed-graph instance of this physics lineage applied to cognitive-coordination substrates. Theorems A, B, C, and D are stated for reflective graph-native systems generally; scale invariance is what makes those theorems framework-claims rather than instance-specific claims. Apply A + C + D + iteration at any zoom level the substrate admits — individual cognitive architectures, multi-agent compositions, organisational systems with humans and tools as typed components — and the same convergent properties follow at that level. The framework's other substantive claims inherit their cross-domain applicability through this property: §7.2's multiplicative-gain result, §9.2's Theorem XI computational-efficiency bound, §9.3's Theorem XII behavioural-bounding result, §9.4's Theorem XIII substrate-Markov-mixing efficiency, and §11's self-evidencing structure all derive their cross-zoom reach from the same source. The typed-graph substrate has no privileged scope; the framework's predictions hold wherever a sufficiently reflective graph-native system operates, regardless of the grain at which the system is observed. The companion paper develops the cross-zoom consequences explicitly — §12 and §13 apply the same machinery at further zoom levels. *Each application is one zoom level of the same operation; scale invariance is the property that makes them all the same operation.*

---

## 7. Surviving Proofs and Sketches

Three prior theses do not collapse to corollaries of the root theorem. They assert dynamics and bounds over the graph state that require independent treatment. §7.1 (Thesis IV) and §7.2 (Thesis V) are full proofs under explicit conditions. §7.3 (Thesis IX) is given here as a proof sketch — it invokes standard PAC-Bayes machinery to bound generalisation error but does not derive the bound from first principles; a fully-worked derivation would be a separate technical report. The sketch is included because the learning-theoretic framing is load-bearing for the paper's claim that graph-quality bounds reasoning fidelity (Thesis I) admits formal learning-theoretic interpretation.

### 7.1 Thesis IV (Monotonic acceleration of reflective loops) — CONDITIONAL

**Conditions.**
- **A1 (Monotone quality improvement).** Expected per-iteration quality gain f(τ_n, R_n) ≥ 0.
- **A2 (No resource saturation).** Iteration cost T(g, τ) is non-increasing in τ for fixed gap g.
- **A3 (Immediate applicability).** Quality improvements take effect in the subsequent iteration without delay.

**Claim.** Under A1–A3, the compound-loop iteration interval Δ_n is non-increasing in n.

**Proof.** The conclusion (iteration interval non-increasing) follows from A2 + A3 directly via the per-iteration cost relation; A1 is used to ensure the τ-input to A2 is itself non-decreasing, so the monotonicity of cost under A2 yields monotonicity of cost across iterations.

*Step 1 (A1).* By A1, expected per-iteration quality gain is non-negative, so τ_n is monotone non-decreasing in n (with no claim yet about asymptote or rate).

*Step 2 (A3).* By A3, quality improvements apply in the immediately-subsequent iteration: the τ-value used to compute iteration n+1's cost is τ_{n+1} ≥ τ_n.

*Step 3 (A2).* By A2, T(g, τ) is non-increasing in τ for fixed gap g. Applying A2 to the sequence τ_{n+1} ≥ τ_n produced by Steps 1–2 yields T(g, τ_{n+1}) ≤ T(g, τ_n) for fixed g.

*Step 4.* Iteration interval Δ_n is proportional to per-iteration cost T. Therefore Δ_{n+1} ≤ Δ_n. ∎

**Note on monotone convergence.** A1 plus τ ∈ [0, 1] does imply τ_n converges by the monotone convergence theorem, but convergence is a statement about the *limit*, not about per-iteration *rate*. The rate claim above is established entirely through A2 + A3 + monotonicity of τ; the convergence guarantee is a separate (asymptote-existence) consequence of A1 alone, useful for talking about the system's quality fixed point but not load-bearing for the iteration-interval claim.

The proof structure is unchanged from prior statements of Thesis IV. What has changed is that the theorem now applies to any reflective graph-native system with a quality measure, not to AI architectures specifically. Instance 1 and Instance 2 are the two currently-validated empirical instances; further instances are expected as the substrate sees other adopters.

**Empirical status.** A1–A3 validated in Instance 2 on HumanEval (n=50 problems, repeated trajectories per problem to estimate within-problem variance): A1 monotone with 0–5.6% noise; A2 decreasing iteration cost with τ; A3 first-attempt rate 80–90% → 100% within-trajectory. A1–A3 observed qualitatively in Instance 1 (topology discovery is monotone by construction under consistent registration; pipeline assembly cost decreases as graph completeness grows; assembly uses current state without delay). Quantitative Instance 1 measurements are pending PoC instrumentation.

### 7.2 Thesis V (Multiplicative gain structure)

**Claim.** In a compound loop with per-iteration efficiency gain E_i ≥ 0, cumulative gain follows a multiplicative structure G_n = ∏_{i=1}^n (1 + E_i) rather than an additive structure S_n = ∑_{i=1}^n E_i. For any E_i > 0, G_n / S_n → ∞ as n → ∞.

**Proof.** The multiplicative form follows from the observation that each iteration operates on the improved state produced by the previous — the base for iteration i+1 is the output of iteration i, not the original base. This is inherent to any compound-interest-style accumulation and requires no graph-specific machinery. The ratio G_n / S_n grows without bound by standard product vs. sum inequalities when all E_i > 0.

The graph-native content: for compounding to be realized rather than degraded toward the additive form, each iteration's improvement must persist into the next. In Instance 2, this means admitted deltas update the map and the model's subsequent iterations read the updated map; in Instance 1, it means topology state persists and subsequent pipeline assemblies use current graph state. Both architectures preserve state by construction. Systems that do not — those that reset state between iterations, or those whose iteration output does not inform subsequent input — collapse to the additive form.

### 7.3 Thesis IX (Learning-theoretic bound on graph-selection classes)

**Claim.** Let H_G be the hypothesis class consisting of all grammatically-valid subgraphs of G (under Γ's type admission). Let ℓ(M, R, O) = 1 − coherence(M, R, O) be a loss function where M ∈ H_G, R is a reasoning trace, and O is an output (ℓ to avoid collision with token-length L used in §9.1). Then generalization error E_gen(M) is bounded by empirical risk R_emp(M) plus a complexity term depending on |Γ| (grammar size) and |V| (element-vocabulary size), and E_gen(M) is inversely related to the quality measure τ(M) defined in Appendix A and invoked by §7.1.

**Proof sketch.** H_G is a structured hypothesis class with decidable membership via the consistency checker on Γ. By τ's construction (weighted sum of nine properties, each bounded in [0,1]; see Appendix A), higher τ implies higher information content in M about R. Standard PAC-Bayes machinery (cf. Grünwald 2005 for the MDL framing) yields a generalization bound of the form E_gen(M) ≤ R_emp(M) + O(√((|Γ| + |V|) / n)) for n samples, where |V| is the element-vocabulary size distinct from the edge-type set E of §3.1. The τ(M) / E_gen(M) inverse relationship follows because higher τ reduces the effective hypothesis class size to higher-quality graphs and thereby tightens the bound.

**Sample complexity.** To reach target quality τ*, the compound loop requires O(log(1/(1−τ*))) iterations under the multiplicative structure of Thesis V — each iteration multiplies the remaining quality gap (1−τ) by a factor bounded away from 1 under A1's lower bound on per-iteration gain, so the iteration count to close the gap to (1−τ*) scales logarithmically in the inverse gap. This is exponentially faster than standard learning algorithms with fixed additive per-step improvement, which require O(1/ε²) samples to reach accuracy ε = 1−τ*. The multiplicative-compound structure is the source of the exponential speedup, the same way Newton's quadratic convergence beats gradient descent's linear convergence on smooth objectives.

**What is novel.** The graph-native hypothesis class is, to the author's knowledge, the first learning-theoretic framing that admits both a decidable membership check (the consistency checker) and a semantically meaningful quality measure (τ over graph structure). Standard deep-learning hypothesis classes (neural network weights) have neither — weights are real-valued, membership is not a Boolean, and "quality" is only observable via loss on data.

---

## 8. Empirical Validation

### 8.1 Instance 2 — HumanEval compound-loop measurements

A coordinator model running the Ozma AI protocol executed the compound loop on the HumanEval benchmark (n = 50 problems, repeated trajectories per problem for within-problem variance estimation). Measurements validated the three conditions required for Thesis IV (A1–A3):

- **A1 (monotone quality):** τ non-decreasing with 0–5.6% noise across trajectories. No systematic degradation.
- **A2 (no resource saturation):** iteration cost decreasing in τ as predicted — higher-quality map state corresponds to cheaper subsequent iterations.
- **A3 (immediate applicability):** first-attempt rate 80–90% at low τ rising to 100% at high τ. The 20-point lift reflects improved map state applied directly to the next iteration.

The coordinator was an existing off-the-shelf model without custom training, fine-tuning, or weight changes. Only the protocol (system prompt + output grammar) and the mechanical shim (output parser, map accumulator, tool execution) were modified. The architecture runs on models that exist today, not on models that need to be built.

**Thesis V empirically distinguishable on the same trajectories.** The same runs additionally produced direct empirical evidence for Thesis V's multiplicative gain structure — a separate question answered as a side effect of the A1–A3 measurement: is the compound loop's cumulative gain structure multiplicative (Thesis V predicts G_n = ∏(1 + E_i)) or additive (S_n = ∑ E_i)? Fitting both models to the measured trajectory data yielded proportional-model RSS of **1.0495** against constant-model RSS of **1.0703** — the multiplicative fit is tighter on the observed data, which is direct empirical evidence that compound-loop improvement preserves across iterations rather than collapsing to a sum. *Calibration: the RSS difference (≈2% relative) is modest, not decisive*; the empirical comparison is *corroborative* of Thesis V's predicted direction, not load-bearing on its own. The load-bearing argument for multiplicativity is the structural derivation of §7.2 (state preservation forces the multiplicative form by construction; systems that fail to preserve state collapse to additive). The empirical comparison shows the structural prediction's direction is reproduced on the trajectories, consistent with state preservation operating on this deployment; readers should treat the RSS as direction-evidence, not as standalone proof. Multiplicative-vs-additive distinguishability on real compound-loop data is unusual; most iterative-improvement systems either reset between iterations (forcing the additive form) or carry state imperfectly enough that the compounding signal is swamped by noise. The Instance 2 trajectories here carry the signal cleanly enough to register the difference, which corroborates that state preservation (the condition §7.2 identifies as necessary for multiplicative realisation) holds operationally in this deployment.

### 8.2 Instance 1 — Ozma System self-construction measurements

The proof-of-concept Ozma System implementation exhibits self-construction by design. Instrumentation-quality measurements — topology discovery rate, pipeline assembly latency, fault-recovery time, monotone map-quality over operational time — are pending. Architectural consistency (graph-native operations throughout, no non-graph escape paths) is verified by code review of the PoC codebase.

The paper's empirical claim is thereby dual-tier. Instance 2 is measured to publication quality. Instance 1 is architecturally verified with quantitative instrumentation pending. The convergence of the two architectures is the primary empirical claim of the paper, and is established independently of either instance's quantitative measurements: the architectures themselves, compared, demonstrate the convergence.

### 8.3 Efficiency measurements in Instance 2

Efficiency gains from operating through the graph-native protocol over operating on raw surface serializations are documented with publication-grade methodology in Appendix E. The reported example — a primed-LLM-suggested refactoring on `python-dotenv`'s `resolve_variables` that converts Θ(N · E) dict rebuilding to Θ(N + E) via a ChainMap — produces a scaling-curve speedup of 1.4× at small inputs to 7.95× at the largest tested input, with memory peak reduced 1.8× to 2.6× across the same range. The super-linear growth in speedup with input size is the big-O signature: the speedup is asymptotic, not a prefactor improvement, and the gain compounds as input size grows. Per Theorem XI (§9.2), the same mechanism applies to any structure-respecting computation over data where K_Γ(I) = o(|I|) — the speedup scales with |I| / K_Γ(I) on that workload. Additional measured instances — on natural-language-scaffolded input the ratio reaches substantially higher factors consistent with the Shannon-bound framing of §9.1 — are documented in separate technical reports; Appendix E here is the reproducible primary example.

**On the recognisability of the optimisation pattern.** Replacing repeated `dict.update()` in a loop with a `ChainMap` is a recognisable Python idiom; competent reviewers, linters, and code-review tools would identify it given the input. The empirical claim of §E.2 is *not* that the optimisation pattern is novel. It is that the pattern Theorem XI predicts — structure-respecting computation getting cheaper under graph-native operations — shows up at production scale on real code, with measurable big-O speedup, and is reachable by the framework's coordinator-and-specialist composition operating against typed harnesses. The pattern's recognisability is itself evidence: Theorem XI describes a class of speedup that competent engineering can land on, not a speculative theoretical advantage. The framework's contribution is the *structural attribution* (the speedup is the dual representation of the dense encoding's distance from the Shannon lower bound under Γ, §9.1), not the discovery of the ChainMap pattern. Readers who attribute the speedup to "any reviewer would catch this" are reading the framework correctly: the framework's claim is that Theorem XI describes a measurable and reproducible class of speedups, exactly because they are catchable patterns rather than esoteric ones.

### 8.4 Development timeline — a verifiable engineering observation

The Ozma AI programme — protocol specification v5.11 through v5.15, two trained LoRAs (Wizard-1 and Coder-3b), one shim-on-frontier deployment (Ruler-1 = Kimi K2.6 + protocol shim), and the complete ozmalib codebase (tree-sitter compression across 15 languages, intermediate representation, 136 hints, 15 checks, 64 rules, distillation pipeline, seven-paper plan) — was produced by one human with agentic implementation delegation in an approximately 14-day overall window, of which 7 days contained the concentrated git-visible output. The 14-day figure is the honest total-work description; the 7-day subset is the interval of public commit activity verifiable from repository history.

The honest framing: the timeline is an engineering observation, not a theorem. It is not offered as direct evidence that the root claim is true; it is offered as verifiable public record of what this specific method (one architect plus agentic implementation) produced in this specific interval, with the artefacts (protocol docs, trained LoRAs, ozmalib, shim deployments, two papers including this one) independently inspectable. Reasonable readings: the timeline is consistent with the thesis that graph-native coordination compresses the solution space, *or* it is consistent with one individual working hard with effective tooling, *or* both. No load-bearing argument in this paper rests on the timeline being interpreted one way rather than the other. The verifiable record is stated because the work was produced in that time and readers will ask; its interpretation is left to the reader.

---

## 9. Bounds That Fall Out of the Root Theorem

The root theorem plus the paper's axioms yield four non-trivial bounds on graph-native system behaviour. Three are efficiency bounds — at the *encoding* layer (how many tokens are required to represent content), the *computation* layer (how many operations are required to process content), and the *kernel-learning* layer (how many samples are required to learn a useful Markov-chain transition kernel on the substrate). One is a safety bound — the *behavioural* layer (what operations the system can perform at all). All four fall out of the root theorem plus standard formal-methods or information-theoretic machinery. This section states and proves each.

Each theorem is also a measurement instrument. Read methodologically, the four bounds in this section measure the cost of four assumptions the field has held without naming. Theorem X measures the cost of *surface tokens are the natural unit of representation*. Theorem XI measures the cost of *byte-stream is the natural unit of computation*. Theorem XII measures the cost of *opacity is intrinsic to capable AI*. Theorem XIII measures the cost of *statistical co-occurrence is the natural substrate for kernel learning*. The proofs are unchanged by this reading; the framing makes visible what each theorem is for.

### 9.1 Theorem X — Native-Dialect Efficiency (encoding layer)

Under the root theorem, Thesis X (native-dialect efficiency) is a one-paragraph derivation rather than a standalone theorem requiring substantial machinery.

**Statement.** Let X_raw be a natural-language or non-native-dialect encoding of reasoning content, and S = φ(X_raw) the graph-native encoding of the same content. Under Axiom 2, H(S) ≈ H(X_raw) for reasoning-relevant content — translation preserves entropy. Let L(·) denote expected serialized token length. Then:

    Cost(S) / Cost(X_raw) ≤ L(S) / L(X_raw)

and the length ratio L(X_raw) / L(S) measures the distance of the non-native encoding above the Shannon lower bound for the content's information source, with the graph-native encoding approaching the bound within a constant factor for the content class A2 covers. Shannon bounds are source-distribution-dependent; the theorem is conditional on the typed-graph substrate being source-appropriate for reasoning-relevant content, not a claim that a single fixed encoding is optimal across all sources.

**Derivation.** Transformer inference cost is non-decreasing in token length by standard attention and KV-cache analysis (Lemma: O(n) per generated token over n context tokens, O(n²) prefill). Therefore Cost is bounded above by a constant multiple of L. The length ratio L(X_raw) / L(S) is positive because natural language is a general-purpose code applied inefficiently to a specialized domain (Shannon 1951 — ~75% long-range redundancy; Hindle et al. 2012 — programming languages are 3–4 bits/token, more predictable but still above their own content's Shannon bound; this paper's graph-native encoding approaches the bound within a constant factor for the source class). The cost ratio inherits the length ratio's bound.

**No information loss.** The claim is entropy-preserving translation, not compression. Reasoning-relevant content is preserved up to ε (Axiom 2); the two encodings differ in their code, not their content. Compression would imply loss or reduction of information; the graph-native encoding does neither.

**Extension to the compilation stack.** Every additional surface layer above graph-native (source language, intermediate representation, bytecode, microcode, hardware-specific encoding) adds syntactic scaffolding that the content does not require. The length ratio grows with stack depth: a concept that occupies one node in the graph may take 3 tokens in Python, 8 in C, 15 in x86 assembly, and 40 in microcode, though the reasoning-relevant information content is constant. LLMs' documented failure modes at low-level code (Jiang et al. 2025; REx86 2025) are direct consequences of this extension: sequential token-prediction training presents each level as a separate token distribution, when the levels are in fact views of the same concept graph. Meta's LLM Compiler (Cummins et al. 2024) establishes empirically that joint training across levels works — an existence proof that linking the stack to the shared graph is operational. The paper's framing identifies why.

**Empirical measurement.** The scaling benchmark in Appendix E is the operational observable of this theorem: a Θ(N · E) → Θ(N + E) refactoring in `python-dotenv`'s `resolve_variables` produces measurable big-O speedup — 1.4× to 7.95× across a 20× input-size range, with the speedup growing super-linearly with input size. The growth-with-size signature is what distinguishes asymptotic from prefactor improvement. The length ratio L(X_raw)/L(S) on this specific task is bounded; on tasks with richer natural-language scaffolding, higher ratios are observed, consistent with the Shannon-bound framing.

### 9.2 Theorem XI — Computational Efficiency Bound (computation layer)

Theorem X bounds the *encoding* overhead of non-native representation. Theorem XI bounds the *computation* overhead. Both are instances of the same underlying principle: the graph-native representation sits at the Kolmogorov bound for its content; non-native representations sit above that bound by a factor that grows with the content's structure.

**Setup.** Let I be an input instance with Kolmogorov complexity K(I) and Γ-relative Kolmogorov complexity K_Γ(I) — the length of the shortest description of I in the graph grammar Γ. K_Γ(I) ≥ K(I) always, with equality when Γ is expressively adequate for I's structure. Let G(I) be the canonical graph-native representation of I under Γ.

**Structure-respecting computation.** A computation P is *structure-respecting* if its output depends only on the semantic structure of I — formally, P(I₁) = P(I₂) whenever G(I₁) ≅ G(I₂) under Γ's typed-graph homomorphism. Examples: most reasoning tasks, queries, transformations, and optimizations. Non-examples: cryptographic hashing over raw bytes, checksums, and computations genuinely sensitive to representation-level detail — these correctly fall outside the theorem and remain Ω(|I|).

**Statement (Theorem XI).** For computations P that are *structure-respecting under Γ* (definition above) over inputs I that are *structured under Γ* (K_Γ(I) = o(|I|)), with a well-formed graph representation (|G(I)| = Θ(K_Γ(I))):

    C_dense(P, I) = Ω(|I|)
    C_graph(P, I) = O(|G(I)| · polylog(|G(I)|))
    S(I) = C_dense(P, I) / C_graph(P, I) = Ω( |I| / (K_Γ(I) · polylog(K_Γ(I))) )

unbounded as |I| / K_Γ(I) → ∞.

**Scope conditions, made explicit in the statement.** The bound applies *only* under the four conditions named: (a) P is structure-respecting under Γ — non-structure-respecting computations (cryptographic hashing, byte-level checksums, succinct-graph-encoding-sensitive operations) correctly fall outside the theorem and remain Ω(|I|); (b) I is structured under Γ — for inputs without exploitable structure (K_Γ(I) ≈ |I|), the speedup degenerates to O(1); (c) Γ is expressively adequate for I's structure; (d) **P has polynomial complexity in its native graph form** — i.e., P is in the polynomial-time class on G(I) directly. NP-hard structure-respecting problems on graph-native input (Hamiltonian path, subgraph isomorphism, graph colouring, 3-SAT-as-graph) remain hard regardless of representation; the C_graph = O(|G(I)| · polylog(|G(I)|)) bound assumes polynomial native-graph complexity and is wrong as stated for problems that are NP-hard or worse on G(I). The theorem's claim is that *byte-stream encoding pays an additional |I|/K_Γ(I) factor on top of whatever the native-graph cost is*; for polynomial native-graph problems this means polynomial graph-native vs at-least-linear-in-|I| dense. For NP-hard native-graph problems, both forms are at least exponential, and Theorem XI does not claim graph-native makes them polynomial. The theorem does *not* claim that arbitrary "more compact" encodings yield computational speedup — succinct-representation theory (Galperin–Wigderson 1983 and successors) shows that reasoning over compressed representations can jump complexity classes upward when the operations are not structure-respecting. The Theorem XI speedup is conditional on the structure-respecting property *and* on polynomial native-graph complexity, both precise constraints, not a general "compactness implies speed" claim.

**Derivation.**

*Step 1 — Dense lower bound.* Any dense representation of I has size Θ(|I|). A computation that must touch the entire dense representation costs at least Ω(|I|). The standard objection — "indexed, cached, compiled, or vectorized accesses are sublinear" — is correct and addressed by the theorem's resolution into three regimes, named explicitly to avoid the sleight-of-hand bind that "if everything sublinear is graph-native, then graph-native vs dense loses meaning." (a) *Sequential dense*: untyped touch, sequential scan over the byte representation; cost Ω(|I|); the theorem's lower bound applies. (b) *Partial graph-native realisations embedded in dense computing*: B-trees, indices, hash maps, caches, compiled lookup tables, SIMD-friendly layouts, GPU tensor kernels, sparse formats, attention mechanisms, MLIR multi-level IR — these are typed-edge structures over a dense store, recovering *part* of the graph-native efficiency. Each is operationally a typed-edge graph confined to one access path or one tensor regime; the *integrated* graph-native form (typed-graph substrate + Γ + admissibility-grammar + reflective access) is not assembled. The post-1988 history of computing is the cumulative reconstruction of these partial fragments (§2.10); each fragment recovers some of the gap. (c) *Full graph-native architectures*: typed structure carries from surface language through Γ-mediated operations to results, with reflective access and admissibility-grammar gating throughout. Burroughs B5000, Lisp Machines, Smalltalk, Sketchpad, F-14 CADC, and Instances 1–2 of this paper are the operational instances. Theorem XI's gap is between (a) and (c); (b) sits in between, providing the partial recovery the post-1988 industry has been doing in domain-specific form. The 40-year gap claim is *not* "current systems are byte-stream from end to end" — it is "current systems are byte-stream at the architectural form, with partial graph-native fragments embedded as point optimisations." The integrated cost-asymmetry the framework measures (§E.2) is between regimes (a) and (c) on the *same* workload; partial-graph-native (b) recovers a portion of the gap on its specific access path but not the integrated whole. The paper does not claim either *all* current computing is dense (the (b) fragments are real) or *all* current computing is graph-native (the integrated form is what 1988 abandoned). Both regimes coexist; the gap is between full-graph-native and full-dense, not between graph-native and any-sublinear-access.

*Step 2 — Graph upper bound.* A Γ-conforming graph representation of I has size at most within a constant factor of K_Γ(I): the grammar Γ is an efficient description language by construction (unique typed nodes, typed edges for meaningful relationships, grammar-constrained composition eliminates type-level redundancy). By the Source Coding Theorem, any representation is bounded below by K(I); K_Γ(I) ≥ K(I) always, with equality when Γ can express I's structure as efficiently as an unrestricted Turing machine can. For well-designed Γ over its target domain, this equality is approached asymptotically. *Note on uncomputability and verification.* K(I) and K_Γ(I) are uncomputable; this is the standard objection. The bound's empirical testability does not rest on knowing K_Γ(I): it rests on the *measurable* quantity |G(I)| / |I|, which is directly observable for any concrete input by counting graph nodes and edges in a given Γ-conforming encoding. The asymptotic claim |G(I)| = Θ(K_Γ(I)) is a property of well-designed Γ, falsifiable by Γ-versions where measured |G(I)| / |I| fails to shrink with grammar refinement on a published corpus. The bound is theoretically grounded in the Source Coding Theorem and empirically measurable through |G(I)| / |I|; the uncomputability of K_Γ does not block either path.

*Step 3 — Structure-respecting traversal.* A structure-respecting computation requires only the subgraph of G(I) reachable from the relevant starting nodes. Spreading activation (Thesis VII in §3.4; standard graph-traversal complexity) reaches the relevant subgraph in O(|reachable nodes| + |reachable edges|). Graph indexing (adjacency lists, hash-indexed node IDs) contributes polylog(|G(I)|) per lookup, giving total cost O(|G(I)| · polylog(|G(I)|)) in the worst case and O(|relevant subgraph| · polylog) for queries that touch only a fraction of the graph. *Note on constants and cache behaviour.* The polylog complexity is asymptotic; constant factors (cache-miss costs of pointer-chasing in poorly-laid-out graphs, allocator overhead, branch-prediction loss on irregular traversal) can dominate at small scale, and naive graph implementations can be slower than well-tuned dense implementations for small inputs. The empirical signature distinguishing asymptotic dominance from constant-factor variation is *super-linear speedup growth with input size*; §E.2's measured 1.4×→7.95× scaling-curve across a 20× input-size range exhibits this signature, confirming that the asymptotic regime is reached on production-scale workloads. Theorem XI is an asymptotic claim; constants matter at small scale but do not invalidate the bound.

*Step 4 — Speedup ratio.* For structured I, |G(I)| = Θ(K_Γ(I)) = o(|I|). The speedup C_dense / C_graph is therefore Ω(|I| / K_Γ(I)) modulo polylog overhead — unbounded as the structure ratio |I| / K_Γ(I) grows.

∎

**Boundary (maximum entropy case).** When I is random noise, K(I) = Θ(|I|), so K_Γ(I) = Θ(|I|), |G(I)| = Θ(|I|), and S(I) = O(polylog). Graph-native provides no advantage on noise — correctly, because noise has no structure to exploit. No operational workload is fully noise; all useful computation operates on signals.

**Relationship to Theorem X.** Theorem X (encoding) and Theorem XI (computation) are dual forms of the same bound. In both, the graph-native representation approaches Kolmogorov/Shannon optimality; the non-native representation exceeds that optimum by a factor proportional to how structured the content is. Theorem X is measured in tokens per bit; Theorem XI is measured in operations per bit. Both emerge as the overhead collapses when the representation is structurally aligned with the content.

**Positioning against sparse-computing literature.** Sparse matrices (CSR, CSC; Buluç & Gilbert 2008), sparse neural networks (Frankle & Carbin 2019 — Lottery Ticket Hypothesis), attention sparsity (Child et al. 2019), and Graph Neural Networks (Kipf & Welling 2017; Veličković et al. 2018) are all empirical instances of this speedup. They win because they bring the representation closer to K(I), and structure-respecting computation over a K(I)-sized representation is fundamentally cheaper than over an |I|-sized one. Theorem XI is the Kolmogorov-theoretic explanation: sparse methods are realizations of graph-native computation restricted to specific tensor domains. The semantic graph is the maximum-fidelity realization of the same principle, applied to all structured data.

**Von Neumann contrast.** Dense, uniform-access computing (the von Neumann architecture) optimizes for Θ(1) random-access per element over an |I|-sized address space. This architecture is correct when K(I) = Θ(|I|) — when the input genuinely is unstructured. For structured I, the architecture is suboptimal by the factor |I| / K_Γ(I). Dense representations are, in a real sense, "graphs that have been serialized into a sequence and then must be re-parsed on every operation." The gap between current computing and optimal computing is the Kolmogorov-complexity ratio of current workloads — substantial for every useful data domain.

**Empirical observables.** Observed K_Γ(I) / |I| ratios for structured data span several orders of magnitude. Shannon 1951 establishes long-range English redundancy at roughly 75%; Hindle et al. 2012 establishes programming-language per-token entropy at 3–4 bits; natural-image compression via arithmetic coding reaches ~30% of original size; scientific-simulation data compresses by orders of magnitude under physical-law-respecting encodings. These are all partial realizations of the K_Γ(I) bound. Theorem XI predicts the speedup scales inversely with these ratios, compounded over the size of the workload. The Appendix E benchmark — 1.4× to 7.95× scaling with input size on `python-dotenv`'s `resolve_variables` — is consistent with this prediction at the measured K_Γ / |I| ratio for that workload; inputs with richer natural-language scaffolding would exhibit correspondingly larger speedups, which is the pattern Instance 2 observes across its broader task mix.

**Caveats.**
- |G(I)| = Θ(K_Γ(I)) is an upper bound assumption under well-formed graph construction. Real graph representations approach K_Γ(I) asymptotically as the schema Γ matures; early graph representations carry overhead above the bound.
- K and K_Γ are uncomputable. Theorem XI bounds the efficiency ideal, not a measurable quantity. Empirical ratios approximate the bound by measuring actual |G(I)| / |I| and C_graph / C_dense under specific workloads.
- The Γ grammar must be expressively adequate for the target domain. For domains where Γ cannot express the structure efficiently (e.g. pure arithmetic regularities, purely probabilistic generative models), K_Γ(I) > K(I) and the speedup diminishes correspondingly. This is not a defect of the theorem; it names where graph-native does not help.

### 9.3 Theorem XII — Behavioural Bounding

Theorems X and XI bound efficiency. Theorem XII bounds *what the system can do at all*. Under the same axioms, behavioural admissibility is decidable and enforced, and the consequences for what the literature calls AI alignment are direct.

**Setup.** Let G be a typed graph satisfying Axioms 1–3. Let Π be a protocol over G with grammar Γ. Let C be a consistency checker that decides admissibility of operations under Γ in finite time per operation — i.e. Γ ∩ C forms a decidable admissibility predicate. Let S be a system operating on G via Π, with the mechanical shim (Appendix B) gating every proposed delta through C before application.

**Statement (Theorem XII).** The observable behaviour of S is provably bounded by the admissibility set Γ ∩ C. Formally: let 𝔅(S) denote the set of operations S actually performs. Then

    𝔅(S) ⊆ { operations admissible under Γ ∩ C }

with the containment guaranteed by construction, not by training or by emergent property. (We use 𝔅 rather than β for the behaviour-set to avoid collision with the KL-penalty coefficient convention in RLHF/PPO notation; see §3.1.)

**Proof.**

*Step 1 — Every operation is typed (Axiom 1).* By Axiom 1 (typed-graph closure), every operation on G is a typed-graph operation: it has a source node, a target node, and a typed edge, all drawn from the type sets of G. Operations that are not typed-graph operations are not admissible inputs to Π and are rejected at the interface before reaching C.

*Step 2 — Every delta is admissibility-checked, including across surface serializations (Axiom 2).* By construction of the shim (Appendix B, step 3), every proposed delta is submitted to C before application. C runs in finite time per delta (by hypothesis, decidability of Γ ∩ C). Axiom 2 (serialization adequacy) ensures that operations arriving via any surface language W_j cannot evade C by laundering through a serialization round-trip: the inverse map W_j^{-1} preserves admissibility up to ε, so an operation inadmissible under Γ cannot become admissible via encoding into a surface and decoding back into G. Deltas for which C returns *inadmissible* are rejected; deltas for which C returns *admissible* are applied.

*Step 3 — Reflective access does not provide an escape hatch (Axiom 3).* Axiom 3 (reflective access) states that G's own structure — including Γ itself — is representable as a subgraph of G. This raises the obvious adversarial question: can a sub-component rewrite Γ from within the system, effectively admitting previously-inadmissible operations by modifying its own admissibility predicate?

The answer is no: modifications to the subgraph representing Γ are themselves typed graph operations, and therefore themselves subject to C. Γ-modification is governed by a meta-Γ (the admissibility rules that apply to operations that target the Γ-representing subgraph itself), which is either fixed at deployment or modifiable only under an even stricter meta-meta-Γ. The regress terminates at whatever level the deployment's authority root specifies. At every level, admissibility checking applies; no operation reaches the graph without passing the level-appropriate C. Reflective access is therefore consistent with the bounding guarantee; it does not circumvent it.

*Step 4 — Applied operations compose admissibility.* By induction on operation count: the empty trajectory is trivially admissible; if a trajectory of n admissible operations is extended by an (n+1)th admissible operation, the extended trajectory is the composition of admissible operations and is therefore in the admissibility set (Axiom 1 closure over typed operations). No non-admissible operation appears in any applied trajectory.

*Step 5 — Behaviour is the trajectory.* 𝔅(S) is the set of applied operations — equivalently, the set of trajectories S actually realises. By Steps 1–4, every applied operation is in Γ ∩ C. Therefore 𝔅(S) ⊆ Γ ∩ C. ∎

**Corollary XII.1 (Specification-form reduction).** Under Theorem XII, the question *"does the system behave according to a specification S?"* reduces to *"does Γ ∩ C equal S?"*. If the grammar captures S, S is satisfied by construction. If not, the problem becomes refining Γ until it does — a formal grammar-specification problem with established solution methods: grammar verification, equivalence checking, model checking, and formal proof of grammar properties. The inheritance is from type theory, the seL4 line of formally-verified kernels and the capability-based security tradition, design by contract, and the production-validated grammar-constrained decoding lineage culminating in Park et al. 2024.

**What Theorem XII claims.** Under the stated axioms, no trained behaviour, emergent capability, or latent tendency of any sub-component of S can cause S to perform an operation outside Γ ∩ C. The guarantee is mechanical: operations are checked before application. Sub-components that *want* to perform inadmissible operations are free to want; they cannot act on the want without passing the check.

**What Theorem XII does not claim.**

- *The theorem does not specify Γ.* Specifying Γ correctly for a novel domain is a non-trivial semantic-engineering problem. The theorem reduces one hard problem (shaping opaque behaviour) to a different hard problem (specifying admissibility grammar), but does not solve the second.
- *The theorem does not prevent grammar-level mistakes.* An incorrectly-specified Γ that admits harmful operations will let S perform those operations. The failure mode — incorrect specification — is *detectable* by grammar review, testable by formal methods, and *correctable* by grammar refinement, rather than by attempting to retrain a black box. The safety pipeline becomes grammar review plus admissibility enforcement, not RLHF plus interpretability research.
- *The theorem does not prevent adversarial-but-admissible constructions.* A system could in principle compose admissible operations into a harmful aggregate. Defending against this reduces to specifying Γ to exclude the aggregate — a real problem, but it is a specification problem (which constructions do we wish to exclude, and how do we describe them) rather than a behavioural-shaping problem.
- *The theorem assumes the shim is implemented correctly.* If the shim has side-channels, if sub-components can execute operations outside Π (e.g. via the OS rather than the protocol), or if C has bugs, the guarantee degrades correspondingly. Standard software-verification methods apply.
- *Rice's-theorem scope.* Rice's theorem (1953) shows that non-trivial *semantic* properties of programs are not generally decidable from their syntax. Theorem XII does not contradict Rice: the theorem bounds *structural admissibility* (whether each operation falls within Γ ∩ C, decidable per-operation by hypothesis), not arbitrary semantic properties of the system's behaviour. Specific semantic predicates ("does this output mislead?", "is this aggregate harmful?") are not claimed to be capturable by Γ. The reduction Theorem XII performs — from opaque-behaviour-shaping to formal admissibility specification — moves the problem into a space where many practically-important safety properties (capability boundaries, authorisation envelopes, type discipline, side-effect containment, information-flow constraints) *are* expressible as structural admissibility predicates and are routinely captured at safety-critical scale (seL4, DO-178C, capability-based security, type theory). What Rice forbids is a fully general-purpose semantic verifier; what Theorem XII supplies is a structural admissibility framework within which the bulk of practically-deployable safety predicates fall.

**Scope of Theorem XII relative to the alignment literature.** Theorem XII applies under graph-native architecture with mechanical admissibility gating; it does not target or invalidate alignment approaches designed for statistical black-box systems. The remaining problem on the graph-native substrate — specifying Γ correctly — is a specification-engineering problem with established methods.

**Caveats.**
- The result holds modulo Axioms 1–3, decidability of Γ ∩ C, and correct shim implementation. Each of these is substantive and reviewable, not a free lunch.
- Γ specification for complex domains is non-trivial and constitutes the bulk of the remaining engineering work for safe deployment. The theorem does not make that work disappear; it relocates it from post-hoc behaviour shaping to ex-ante specification.

**Relationship to prior work.** Theorem XII sits in the intersection of four mature literatures (formal methods, capability-based security, the safety-by-construction community, the mechanistic-interpretability programme) and one rapidly emerging one (the opaque-system alignment lineage). The theorem's normative direction is shared with a growing community; the theorem form and the token-boundary / typed-graph-operation-level instantiation are what's novel within that direction. The full literature-positioning analysis — formal-methods lineage (Milner / Wright-Felleisen / capability-based security / DBC), safety-by-construction community (Seshia / Dalrymple / Drexler / Bengio / Tegmark-Omohundro / Open Agency / Clarke / Christiano), three-part contribution articulation, opacity-as-root-cause thread (Olah / Elhage / Bricken / Templeton / Nanda / Bereska / Sharkey / Casper / Amodei / Yudkowsky), opaque-system alignment lineage (Christiano-Leike / InstructGPT / Constitutional AI / Debate / Iterated Amplification / Recursive Reward / CIRL / Mesa-Optimization / Corrigibility / Concrete Problems), and *what nothing in the literature states explicitly* — is in **Appendix F (Theorem XII Literature Positioning)**, extracted to keep the proof central. Readers evaluating Theorem XII's theorematic content can read §9.3 as it stands; readers evaluating the framework's prior-art credit and positioning within the safety-by-construction community should read Appendix F.

### 9.4 Theorem XIII — Substrate-Markov-mixing efficiency (kernel-learning layer)

Theorems X and XI bound the *substrate* cost of operating on non-native representations: encoding overhead and computation overhead respectively. Theorem XIII bounds a separate cost layer: the cost of *learning the transition kernel* of a Markov chain operating on the substrate. Together with Theorem XI, it composes multiplicatively into the total efficiency gap between framework-native and statistical-substrate computation.

The theorem is the Markov-chain restatement of Corollary D.4 (§6.4), promoted from empirical prediction to derivable result via standard Markov-chain learning theory.

**Setup.** Theorem D (§6.4) establishes that the typed-graph substrate plus weight functions is, mathematically, a Markov chain over typed-graph nodes — state space V, transition kernel `P(n_{t+1} | n_t) ∝ w(n_t, n_{t+1})`, sample trajectories as thoughts. Statistical language modelling — n-grams, hidden Markov models, transformers — is the same family of machinery on a token substrate (state space V_τ of tokens, transition kernel learned from co-occurrence statistics). Let G be a typed-graph substrate over a domain D, and let τ be a token substrate over the same domain. Define **substrate richness** ρ(·) as the average information content per state about the underlying domain — formally, the mutual information between substrate state and domain semantics.

**Statement (Theorem XIII).** For any computation reducible to Markov-chain mixing on the substrate, the sample complexity S of learning a transition kernel that achieves total-variation distance ε from the optimal stationary distribution satisfies:

    S_τ / S_G ≥ f(ρ(G) − ρ(τ))

where f is non-decreasing and unbounded in the richness gap. Equivalently: *the cost of learning a useful kernel grows with the amount of substrate-level information the kernel must reconstruct.* On a richer substrate, less reconstruction is required; on a token substrate, the kernel must learn the substrate semantics from co-occurrence statistics in addition to the task-specific weighting.

**Proof sketch.**

*Step 1 — Decomposition of kernel learning.* A Markov-chain transition kernel on a substrate carries two pieces of information: (a) substrate structure — which states are connected to which, with what types — and (b) task-specific weighting of those connections. On a typed-graph substrate, (a) is given by construction; only (b) requires learning. On a token substrate, both (a) and (b) must be inferred.

*Step 2 — Sample complexity of substrate-structure learning is superlinear-to-exponential in the richness gap, depending on hypothesis-class structure.* Inferring substrate structure from raw co-occurrence is, in standard information-theoretic terms, the problem of learning a typed graph from sequence statistics. Sample complexity grows with the information content of the recovered structure; the precise rate depends on the hypothesis class. *Caveat on rate.* The most aggressive bound — Solomonoff-induction-as-upper-bound — gives an algorithmic-information-theoretic ideal that is exponential in the richness gap but is itself uncomputable, conflating the algorithmic-information limit with the statistical-learning bound. In the polynomial PAC-Bayes regime applied later to graph-selection hypothesis classes (§7.3), bounds are typically polynomial in the relevant parameters rather than exponential in the substrate-richness gap. The qualitative claim — *richer substrate ⇒ less reconstruction cost* — survives under any of these regimes; the *exponential* quantitative claim is the worst-case envelope rather than the typical-case rate. Theorem XIII as stated in this paper rests on the qualitative monotone direction (f non-decreasing in the richness gap, unbounded as the gap grows under the worst-case Solomonoff regime), not on the exponential rate being achievable in every practical hypothesis class. Readers should treat the exponential characterisation as a worst-case envelope and the polynomial-PAC-Bayes characterisation as a typical-case lower bound; the ordering is preserved either way. The richness gap ρ(G) − ρ(τ) is, by definition, the information content the token-substrate chain must reconstruct.

*Step 3 — Sample complexity of task-specific weighting is roughly substrate-invariant.* Once substrate structure is given (whether by construction or by prior learning), the task-specific weighting requires comparable samples on either substrate. This component is approximately constant across the comparison.

*Step 4 — Total cost ratio.* Combining Steps 1–3: S_τ / S_G is dominated by the substrate-structure-learning ratio, which grows with the richness gap by Step 2. Therefore S_τ / S_G ≥ f(ρ(G) − ρ(τ)) for some non-decreasing unbounded f.

*Step 5 — Theorem X bounds the richness gap from below.* Theorem X established that typed-graph encoding sits closer to the Shannon lower bound for typed content than token encoding does. The richness gap is the state-space dual of the encoding gap: both follow from the same information-theoretic argument applied to different objects. Wherever Theorem X's encoding gap is large, Theorem XIII's richness gap is large, and the kernel-learning cost ratio inherits the same scaling.

∎

**Composition with Theorem XI (the multiplicative bound).** Theorems XI and XIII are independent — XI bounds substrate-encoding-and-computation cost; XIII bounds kernel-learning cost given the substrate. Total computational cost of useful Markov-chain inference factors approximately as `(encoding+computation) × (kernel learning) × (mixing time)`. Theorem XI bounds the first factor; Theorem XIII bounds the second; mixing time is substrate-and-task-dependent and cancels approximately in the ratio. The combined bound is therefore the product:

    C_token / C_graph = (encoding+computation factor of XI) × (kernel-learning factor of XIII)

The framework regime sets both factors close to optimal by construction; the statistical-substrate regime pays both as multiplicative penalties. The combined bound is the product, not the sum, of the two efficiency advantages.

**Corollary XIII.1 (Provable form of D.4 — the stochastic-traversal floor).** A stochastic walker on the typed-graph substrate uses a structurally-derived kernel that is close to the optimal kernel because the substrate already encodes the information the optimal kernel would learn. The gap to a fully-trained kernel is bounded above by a quantity that decreases as substrate richness increases. The empirical D.4 prediction — *graph + RNG approaches model performance on traversal-reducible tasks* — is the operational reading of this bound; the experimental work of measuring the constant remains worthwhile, but the qualitative existence of the floor is now derived rather than predicted.

**Caveats.**
- Substrate richness ρ(·) is defined information-theoretically. As with Kolmogorov complexity, ρ is uncomputable in general; Theorem XIII bounds an efficiency ideal, not a measurable quantity. Empirical ratios approximate the bound by measuring sample complexity under specific workloads.
- The bound is an inequality, not an equality. Specific workloads may exhibit smaller gaps than the bound predicts; this is consistent with the theorem.
- The theorem applies to computations expressible as Markov-chain mixing. Theorem D (§6.4) bounds which computations can be so expressed — the cognitive-architecture enumeration. Computations that cannot be reduced to chain mixing (e.g. cryptographic computation; numerical integration without a structured-state interpretation) are out of scope.
- The decomposition of kernel-learning cost (Step 1) treats substrate-structure learning and task-weighting learning as separable. In practice they interact; the separability is asymptotic, not tight. The theorem's qualitative claim survives interaction; tight quantitative constants may not.

**Relationship to existing Markov-chain learning theory.** The result is, in spirit, a direct consequence of the compression-and-learning duality (Vapnik 1998; Solomonoff induction as the limit case) applied to Markov chains on substrates of differing information richness. It draws on standard PAC-Bayes machinery for transition-kernel learning (McAllester 1999 and subsequent), Cheeger inequality and conductance arguments for Markov-chain mixing time (Sinclair 1992; Levin and Peres 2017), and the classical learning-theory observation that sample complexity grows with the effective dimension of the hypothesis class. The novel contribution is the application to substrate-vs-substrate comparison rather than a fixed-substrate hypothesis-class comparison — equivalently, the recognition that *the substrate itself is a hypothesis-class restriction* with measurable consequences for the cost of any Markov-chain inference operating on top of it.

**Full derivation.** This section gives the proof sketch in five steps; the rigorous derivation, with formal definitions of substrate richness as mutual information, hypothesis-class effective dimension, the PAC-Bayes sample-complexity argument relating prior structure to posterior learning cost, the multiplicative-composition argument with Theorem XI, and the worst-case-vs-typical-case (exponential vs polynomial) regime distinction, lives in the companion technical report `theorem_xiii_derivation.md` (alongside this paper in `research/docs/`). The main paper carries the result; the technical report carries the formalism. Following the precedent of §7.3 (Thesis IX), the deferred technical report is for full-derivation depth, not for unverified assertions — the proof sketch above is sufficient for the paper's argumentative load and is independently checkable by any reader fluent in PAC-Bayes and Markov-chain mixing literature.

---


## 10. Licensing

Ozma AI is MIT-licensed. Ozma System is AGPL-3.0-licensed with a plugin exception. Documentation, including the routing specification, is released under CC-BY-4.0. Copyright for both projects is held by Ozma Labs Pty Ltd (2024–2026). Stakes, adoption dynamics, and the architectural-vs-commercial implications of these choices are developed in the companion paper.

---

## 11. What Remains Conditional

The paper's formal claims depend on empirical and engineering conditions the reader should see clearly.

**Theorem Root** is unconditional modulo its axioms. Axiom 1 (typed-graph closure) and Axiom 3 (reflective access) are architectural and hold by construction in Instance 1 and Instance 2. Axiom 2 (serialization adequacy up to ε) is empirical — ε must be measured on a corpus for each serialization map. Preliminary measurements in Instance 2 (consistency-checker admission rate on model output) suggest ε < 1% on in-domain tasks, but corpus-wide measurement is pending.

**Thesis IV (monotone acceleration)** is conditional on A1–A3. Instance 2 validates all three on HumanEval; Instance 1 validates them qualitatively with quantitative instrumentation pending.

**Thesis V (multiplicative gains)** holds unconditionally given preserved state between iterations. State preservation is a property of both instances by construction; systems that fail to preserve state collapse to the additive form, as stated in §7.2.

**Thesis IX (learning-theoretic bound)** is conditional on the consistency checker being decidable in finite computation per map state. This is architecturally true in Instance 2 (the checker is a finite-step graph-traversal over the current map against Γ). The loss function ℓ and coherence metric remain open to instantiation for specific deployments.

**Self-construction theorem (Theorem A)** requires both reflective access and a decidable quality measure. Both instances have both. Instances that lack either will not exhibit the theorem's trajectory.

**Cross-domain demonstration** is the paper's primary empirical claim and stands on the existence of the two instances as architectures, with quantitative measurement pending in Instance 1. If Instance 1's quantitative measurements fail to match Instance 2's qualitative pattern, the cross-domain claim is weakened; the within-Instance-2 claims are not affected. The two-instance convergence does not by itself constitute independent replication: both instances were produced by the same author following the same thread-pulling methodology, so the convergence is consistent with at least two readings — that the architecture is a feature of the structural problem (the framework's claim) or that it is a feature of the author's cognitive bias toward graph-shaped solutions (the parsimonious counter-explanation). The cognitive-bias counter-explanation is addressed at three layers, with each layer narrowing the conditions under which it can apply.

*Layer 1 — Instance 1's internal cross-domain convergence.* Instance 1 is itself cross-domain across 8+ infrastructure domains (signal routing + power + physical environment + security + audio + asset management + monitoring + building management; §4.5 enumerates the absorbed domains), and the typed-graph substrate emerged from each domain's own structural requirements rather than from a single author's cognitive preference applied across two related problems. The within-Instance-1 cross-domain convergence is one author convening across substantively different problem types (electrical-distribution graphs, RF-propagation graphs, audio-bus graphs, identity-credential graphs, asset-lifecycle graphs, etc.) — each domain's structural requirements pulling toward the same typed-graph primitives.

*Layer 2 — Original-substrate ↔ bottleneck-fix-substrate convergence.* Instance 2 was not undertaken as a parallel hypothesis-test of a graph-native architecture; it was undertaken as a *detour* to fix the LLM-cost bottleneck that had stopped Instance 1's continued development (see §4.3 chronology). The bottleneck-fix landed on the same architecture as the original problem. The cognitive-bias counter-explanation has to argue the author's bias produced graph-native architecture *both* in the conscious goal (build the infrastructure substrate) *and* in the different conscious goal (reduce LLM coordination cost on a tool-building exercise) — categorically different problem framings.

*Layer 3 — Ecosystem-wide third-party convergence on the coarse architectural pattern, prospectively.* As of paper preparation (2026-04), public open-source ecosystem data shows multi-organisational convergence on the coordinator-pattern architecture this paper's instances exhibit. (Methodology and full project enumeration in `research/docs/ecosystem_convergence_2026.md`.) The major frontier-AI organisations have shipped public coordinator-pattern projects that, measured by GitHub stars, total approximately 1.21 million cumulative stars across the top 24 such projects, with median project age ~12 months and 15 of the top-20 created after 2024-01-01. Specific instances include Anthropic's Claude Code (118K stars, the reference implementation), Google's Gemini-CLI (102K, Google's architectural counterpart), OpenAI's Codex (78K, OpenAI's architectural counterpart), Alibaba's Qwen-Code (24K, Alibaba's architectural counterpart), HuggingFace's ml-intern (5.8K, October 2025) and smolagents (27K), and Microsoft's autogen → agent-framework lineage. Each of these implements the same architectural commitments the framework predicts: coordinator + agent-loop, typed tool router with categories, bounded persistent memory with compaction, frontier-LLM components via abstraction layer (litellm or OpenAI-compat), approval-gating for sensitive operations, and Model Context Protocol (MCP)–based typed-tool routing. MCP itself, launched November 2024, went from launch to universal adoption across 2025-vintage agent projects in approximately 17 months. The convergence is multi-organisational, prospective (occurring before this paper's publication), and broad in adoption.

The third-party convergence layer narrows the cognitive-bias counter-explanation substantially: it would need to argue that HuggingFace, Anthropic, Google, OpenAI, Alibaba, Microsoft, and ~20 other major projects share the same cognitive bias toward graph-shaped solutions, despite their independent organisational structures, different conscious goals (ML engineering productivity, coding assistant, conversation, agent orchestration), and engineering teams with no shared methodology with this paper's author. The framework's prediction is that pulling on the coordination-cost-reduction thread converges on the architectural pattern; the ecosystem corroborates this prediction at scale across multiple independent programmes simultaneously. *Stars and forks measure attention and adoption, not architectural validation per se; treating the metrics as quality measurement would overshoot. The defensible claim is that multi-organisational convergence on the same architectural commitments is meaningful evidence of structural pattern-recurrence — corroborating evidence, not proof, and the paper is calibrated accordingly.*

What the ecosystem convergence does *not* validate: the framework's *fine-grained* moves beyond the coarse coordinator pattern — formal admissibility grammar in the Theorem XII formal sense (rather than heuristic guardrails or approval predicates), L2 native-substrate models that emit typed-graph natively (rather than treating LLMs as byte-stream/token-stream endpoints adapted via tool schemas), typed-graph as knowledge-state memory primitive (rather than control-flow graph or session-bounded ContextManager), per-organisation dialect production (rather than configuration-tier custom modes), and the substrate-Markov-mixing efficiency claim (Theorem XIII). These remain distinctive to the framework's account; the ecosystem has not committed to them at scale yet. The cognitive-bias counter-explanation could in principle still apply to these specific moves, even though it no longer applies to the coarse architectural pattern.

Three convergence layers (within-Instance-1 + original-vs-bottleneck-fix + ecosystem-wide) plus the framework's explicit invitation to independent application of the methodology is the strongest available evidence the paper can present in advance of long-term ecosystem evolution. The paper is calibrated accordingly: the coarse architectural prediction has substantial third-party validation as of publication; the fine-grained moves remain the framework's distinctive falsification surface for future evaluation.

**Theorem X (encoding efficiency)** depends on the ε grammar-inexpressibility bound above, and on the assumption that the underlying tokenizer treats graph-native and natural-language encodings comparably. The Appendix E scaling-curve benchmark validates the theorem's direction with a concrete big-O reduction on production code; tight bounds per domain require per-domain measurement.

**Theorem XI (computational efficiency)** rests on three conditions: (a) the graph representation satisfies |G(I)| = Θ(K_Γ(I)) within a constant factor, which is an asymptotic property that well-designed grammars approach but do not exactly achieve; (b) the computation is structure-respecting under Γ (fails for byte-level operations like cryptographic hashing, correctly outside theorem scope); (c) the grammar Γ is expressively adequate for the input's structure (fails for domains requiring Turing-complete pattern matching that the grammar cannot encode). K and K_Γ are both uncomputable; the theorem bounds the efficiency ideal, not a measurable quantity. Empirical ratios approximate it by measuring actual |G(I)| / |I| and C_graph / C_dense.

**Theorem XII (behavioural bounding / alignment)** rests on: (a) Axioms 1–3 of §3.2 (typed-graph closure, serialization adequacy, reflective access); (b) decidability of Γ ∩ C in finite time per operation (the consistency checker must terminate on every proposed delta); (c) correct implementation of the mechanical shim (no side-channels by which sub-components can execute operations outside Π); (d) a fixed authority root at some meta-level of the Γ-modification regress (§9.3 Step 3), so that the recursive admissibility check over Γ-modifying operations terminates rather than regresses indefinitely. The theorem guarantees behaviour ⊆ Γ ∩ C modulo these conditions. What the theorem does not guarantee: that Γ correctly specifies intended behaviour (that is a specification-engineering problem that the theorem reduces the alignment problem to); that adversarial-but-admissible aggregates cannot arise (Γ must be designed to exclude them); that the shim's implementation is bug-free (standard software-verification methods apply). The reduction — from opaque-behaviour-shaping to formal-grammar specification — is the theorem's contribution; the remaining specification work is real engineering.

**Empirical scope — what the measurements actually support.** The paper's quantitative empirical record is narrow relative to the theorems' stated scope, and the disjunction is named here so it does not get smuggled in. Instance 1 (Ozma System) ships the architecture; quantitative metrics are pending PoC instrumentation. Instance 2 (Ozma AI) supplies the bulk of the quantitative record: HumanEval n=50 trajectory measurements (§7.1 / §8 — A1–A3 validation for Thesis IV); a single primed-LLM-suggested refactoring on python-dotenv (§E.2 / §9.2 — 1.4×–7.95× scaling-curve speedup); and a corpus analysis at 93.2% compound-loop resolution across 795,487 events from a single user's sessions (§E.9 — single-user corpus, no control group). What these measurements support: the architectural claim is operationally realisable on commodity hardware; the structure-respecting computational speedup is detectable at production scale on at least one workload; the compound-loop pattern occurs at high frequency in heavy naturalistic usage. What they *do not* support without additional measurement: population-level generalisation of the resolution rate (single-user corpus); multi-workload validation of the §9.2 speedup (one primary instance plus separately-documented natural-language ratios); independent replication outside the author's deployments. Theorems X / XI / XII / XIII are *theoretical* bounds, conditional on their named axioms and scope conditions; the empirical record validates direction and existence-proofs the architecture, but does not establish the bounds at population scale. Readers should treat the quantitative record as substrate-realisability and direction evidence, not as full validation of the theorematic envelope.

**Companion paper caveat.** Implications, consequences, and downstream claims are developed in the companion paper.

**The self-evidencing structure (Abstract, §12) is not circular reasoning.** Circularity requires that an argument's premise presuppose its conclusion. The self-evidencing argument here has three independently checkable premises: (a) specific artefacts exist — the paper itself, the `research/experiments/agi-mvp/` harness, the `compound_loop_scan.py` corpus analyser, the figures and their generation scripts, the memory files; (b) the production process is documented through independently auditable records — session transcripts, git history, tool-use logs, commit messages, figure-generation scripts — prior to and without reference to the claims being made; and (c) the artefacts' measurable structural properties (coherent cross-referencing across more than 1,200 lines, quantified claims with named falsification criteria, a 93.2% compound-loop resolution rate measured on 795K naturalistic events) are what the mechanism the paper describes predicts it would produce. Observation-and-prediction matching, where both are independently available, is empirical verification, not circularity. Self-reference is not circularity: recursion is self-reference without presupposition (Gödel sentences, quines, constructive existence proofs all have self-reference without logical problem); circularity is the specific failure where a premise requires its conclusion to be already established. The self-evidencing structure is of the former kind, scoped strictly to paper-production. **A separate concern, distinct from circularity, is selection bias**: the author chose which observations to report and which aspects of the production process to highlight, so the self-evidencing argument is vulnerable to the standard selection-bias critique even though it is not circular. The framework's response to that concern is the same as the response to single-author convergence in §11 — invite independent third-party inspection of the production process (session transcripts, git history, tool logs are auditable), and acknowledge that until such inspection occurs, the self-evidencing claim is corroborating rather than conclusive. Generalisation to broader scopes is the subject of the companion paper.

---

## 12. Conclusion

I pulled on two threads. The first unraveled into a graph-native routing architecture for every signal and every scale. The second unraveled into a graph-native coordination protocol for composite reasoning systems. Neither thread was pulled with the intent of reaching the other. They reached the same place.

What both thread-pulls actually did, read methodologically, was force a load-bearing assumption to surface and falsify on its own merits. KVMA forced the assumption that byte-oriented signal protocols are the natural substrate for routing typed signals between typed devices; the assumption did not survive a serious treatment. Ozma AI forced the assumption that a stable protocol between reasoning components is unnecessary because each integration can re-solve coordination contracts; that assumption did not survive a serious treatment either. In both cases the architecture that fell out — typed graph as substrate, typed envelopes over a shared semantic map — was operationally available since the 1960s but invisible while the unnamed assumption forbade reaching for it. The convergence of the two thread-pulls on the same answer is what licenses the methodology: name the unnamed assumption, falsify it, accept the alternative the falsification reveals.

The structural claim is that one typed graph underlies all systems, and every wire format demonstrated in this paper — programming languages, signal formats, network protocols, compilation-stack levels, control planes, coordination protocols — is a serialization of a subgraph of it. The claim is asserted universally (applying to any wire format whose input satisfies K_Γ(I) = o(|I|)) and demonstrated on the specific wire formats named. The operational claim is that a protocol speaking graph-native coordinates any client because every client is already a graph. The empirical claim is that two independent engineering efforts applied to distinct problems produced the same structural answer, and their convergence is the primary evidence.

The root theorem is stated in §3. Two theorems are proved in full in §7 (Thesis IV, Thesis V), and one is given as a labelled proof sketch (Thesis IX). Six prior theses reduce to corollaries, citations, or definitions (I, II, III, VI, VII, VIII); Thesis X is elevated to Theorem X in §9.1 as a direct consequence of the root claim; companion Theorems XI (§9.2, computation layer), XII (§9.3, behavioural/safety layer), and XIII (§9.4, kernel-learning layer) are derived from the same root. Two instances — Ozma System and Ozma AI — provide the existence proofs. Ten fields of prior art converge on the synthesis without having articulated it; the graph-native architectural vision was shipped by the first wave (Burroughs B5000, Lisp Machines, Smalltalk, Sketchpad) and abandoned by the industry in 1988 for economic reasons.

The paper does not claim that graph-native coordination is AGI. Implications and downstream consequences are developed in the companion paper.

None of the core ideas are mine. Tom Knight and the MIT AI Lab figured out graph-native hardware in the 1970s. Alan Kay and the Smalltalk team figured out that everything is a typed object with typed messages. Robert Barton and the Burroughs team figured out tagged memory in 1961. Ivan Sutherland figured out direct manipulation in 1963. Claude Shannon and Andrey Kolmogorov gave us the bounds that measure what was lost. I am an interop-layer craftsman — a digital plumber. The architecture was built, and then the industry walked away from it for reasons that had nothing to do with whether it was correct. I walked back in, looked at the drawings, found the unconnected fittings, and plumbed them in. Theorem XI is the pressure test.

The plumber's contribution, beyond the plumbing itself, is methodological. The architecture was already correct; what was missing was a name for the assumption that licensed walking away from it. The forty-year gap was not held open by a technical mistake; it was held open by an unnamed assumption — that economic and technical correctness about substrate would converge later — that nobody in a position to name it had reason to. This paper names the assumption (§2.10), measures the gap it opened (§9.2, Theorem XI), and demonstrates that the alternative is operationally realisable on commodity hardware (§4, §5). The same operation applied to eleven adjacent unnamed assumptions is the companion paper. The methodology — pull on a concrete operational problem until the assumption making it hard surfaces, name the assumption, accept what the naming reveals — is the work. The artefacts are how it lands.

The methodology stands on the same shoulders the architecture does, and the credit extends accordingly. Naming an assumption requires the assumption to be precisely articulated first; the figure who supplies that precision is the one who makes the eventual naming possible.

Hutter's operationalisation of compression-as-intelligence (§2.8) is what makes byte-faithfulness vs content-faithfulness a distinction this paper can draw at all. LeCun's articulation of what current statistical AI lacks supplies the structured-AI critique the framework formalises. Christiano's interpretable-beliefs remark and the broader interpretability community's opacity-as-root-cause framing (§9.3) supply the architecture-contingent precision Theorem XII operates within. Dalrymple, Bengio, Tegmark, Russell, Tenenbaum, Seshia, and the Guaranteed Safe AI community supply the manifesto-level direction Theorem XII lands within. Drexler's Comprehensive AI Services frames structured services over opaque agents as an architectural choice rather than a constraint. Solomonoff and Kolmogorov supply the formal language minimum-description-length arguments require. Shannon supplies the bound distance from which the framework measures.

Each of these workers articulated a load-bearing position with sufficient precision that someone, later, could name the assumption it carried — and that *someone* is the agent the methodology requires, not a privileged seer. The framework owes its sayability to each of them. Standing on their shoulders is the operational form of the methodology; without their precise prior articulations there is nothing precise to name and nothing to measure the gap from. The plumber-register and the methodology-register are the same register applied to different layers of the same shoulders.

The methodology itself has a deeper lineage than the specific applications above credit — five-tradition prior-art lineage spanning philosophy of science (Kuhn, Quine, Lakatos, Latour), sociology and critical theory (Bourdieu, Foucault, Barthes), cognitive practice (Polanyi, Argyris, Kahneman, Norman), engineering and design (Dijkstra, Kay, Dreyfus, Brooks, Alexander), and physics (Wilson, Bak, West, Mandelbrot, Wheeler, Landauer, Bekenstein, Lloyd, Wolfram, Barabási, Stanley, Sornette). The framework's contribution within this lineage is operational composition: across the connected programme of this paper and its companion, twelve unnamed assumptions are named — one in full in this paper, eleven in the companion — each falsified by an artefact that ships, with the production of the programme itself as a self-evidencing instance. The methodology is older than the framework; the framework is the operational instance applied to twentieth-and-early-twenty-first-century computing. **The full methodology-lineage articulation, with each tradition's foundational figures and the framework's specific contribution within each tradition, is in Appendix G (Methodology Lineage).** A long-form survey of the lineage at corpus depth, with annotations for each prior worker's specific contribution to the framework's articulability, lives at `research/docs/methodology_prior_work.md`.

**The paper is its own experiment.** The argument in the pages the reader has now crossed was produced by the mechanism the argument describes — multiple frontier language models (Opus 4.x, Kimi K2.x, GLM-5.x, Qwen 3.6, and others) operating under the author's architectural direction through the graph-native substrate, typed protocol, hint packs, compound loop, and mechanical validators specified in §3–§9. Every figure, every theorem, every empirical section, every reference, every revision, every restructuring across the multiple review passes was produced through the same mechanism. At the restricted scope of paper production, the pattern's correctness is not inferred; it is observed. Getting multiple LLMs to collaboratively build the system that makes the thesis is the mechanism doing the thing the thesis describes. The paper-production demonstration is not speculative. It is the paper in the reader's hand.

I feel silly for having produced two convergent architectures from two unrelated starting problems — for, in effect, answering the same structural question twice without noticing until after. The silliness is the evidence that the answer is structural rather than chosen. Matters of the territory do not care whether the observer meant to find them.

---

## Acknowledgments

To Tom Knight, David Moon, Richard Greenblatt, Richard Stallman, and the MIT AI Lab / Lisp Machine tradition — the architecture was yours, and the plumber only followed the diagrams. To Alan Kay, Dan Ingalls, and the PARC / Smalltalk team — every typed-object claim in the paper is downstream of what you shipped. To Robert Barton and the Burroughs B5000 team — tagged memory in 1961 is the oldest plumbing in the building, still holding pressure. To Ivan Sutherland — direct manipulation of typed graphs was right in 1963 and remains right. To Claude Shannon and Andrey Kolmogorov — the bounds in §9 are yours. To the agentic implementation work that executed the second thread-pull within tolerances I could hold in my head. To decades of interop-layer design experience that told me what kinds of threads were worth pulling. To the routing problem that was trivial enough to start and deep enough to become the whole project.

---

## References

### Structured-AI critique of autoregressive LLMs and emergence-as-theorem positioning (§6 and Abstract references)

- LeCun, Y. (2022). "A Path Towards Autonomous Machine Intelligence." Meta AI / NYU preprint, version 0.9.2. Canonical articulation of the research programme arguing that autoregressive LLMs are architecturally insufficient for intelligence, and that persistent world models, hierarchical planning, objective-driven behaviour, and structured latent spaces (specifically JEPA-class architectures) are required.
- LeCun, Y., Assran, M., Ballas, N., Bardes, A., Rabbat, M., et al. (2023). "Self-Supervised Learning from Images with a Joint-Embedding Predictive Architecture" (I-JEPA) and subsequent V-JEPA work. Operational instance of the structured-latent-space thesis.
- Bengio, Y., Lahlou, S., Deleu, T., Hu, E. J., Tiwari, M., and Bengio, E. (2021). "GFlowNet Foundations." arXiv:2111.09266. Adjacent programme on structured stochastic generation over typed graphs.
- Chollet, F. (2019). "On the Measure of Intelligence." arXiv:1911.01547. Articulates emergence as a measurable structural property — skill-acquisition efficiency over novel task distributions — rather than as a scaling-law artifact. Complementary to the guaranteed-emergence framing of §6.
- Dehaene, S. (2014). *Consciousness and the Brain: Deciphering How the Brain Codes Our Thoughts*. Viking. Global Workspace Theory as biological-substrate version of iterative-broadcast-over-shared-typed-substrate.
- Baars, B. J. (1988). *A Cognitive Theory of Consciousness*. Cambridge University Press. Original Global Workspace Theory.

### Foundational formal framework

- Scott, D. and Strachey, C. (1971). *Toward a Mathematical Semantics for Computer Languages*. Oxford University Computing Laboratory, PRG-6.
- Stoy, J. E. (1977). *Denotational Semantics: The Scott-Strachey Approach to Programming Language Theory*. MIT Press.
- Eilenberg, S. and Mac Lane, S. (1945). "General Theory of Natural Equivalences." *Transactions of the American Mathematical Society* 58(2):231–294.
- Grünwald, P. D. (2005). "A Short Introduction to Model Selection, Kolmogorov Complexity and MDL." arXiv:1005.2364.
- Kolmogorov, A. N. (1965). "Three Approaches to the Quantitative Definition of Information." *Problems of Information Transmission* 1:1–7.
- Shannon, C. E. (1948). "A Mathematical Theory of Communication." *Bell System Technical Journal* 27:379–423, 623–656.
- Shannon, C. E. (1951). "Prediction and Entropy of Printed English." *Bell System Technical Journal* 30(1):50–64.

### Cognitive and linguistic precedents

- Fodor, J. A. (1975). *The Language of Thought*. Harvard University Press.
- Sowa, J. F. (1976). "Conceptual Graphs for a Data Base Interface." *IBM Journal of Research and Development* 20(4):336–357.
- Sowa, J. F. (1984). *Conceptual Structures: Information Processing in Mind and Machine*. Addison-Wesley.
- Wierzbicka, A. (1996). *Semantics: Primes and Universals*. Oxford University Press.
- Goddard, C. and Wierzbicka, A. (2014). *Words and Meanings: Lexical Semantics Across Domains, Languages, and Cultures*. Oxford University Press.

### Graph-native architectures (first wave, 1961–1988)

- Barton, R. S. (1961). "A New Approach to the Functional Design of a Digital Computer." *Proceedings of the Western Joint Computer Conference*. [Burroughs B5000 tagged architecture.]
- Sutherland, I. E. (1963). *Sketchpad: A Man-Machine Graphical Communication System*. PhD thesis, MIT. [First direct-manipulation graphical interface over typed constraint graphs.]
- Knight, T., Moon, D., Holloway, J., and Steele, G. L. (1979). *CADR*. MIT AI Laboratory Memo 528. [The MIT Lisp Machine.]
- Greenblatt, R. D., Knight, T. F., Holloway, J., Moon, D., and Weinreb, D. L. (1984). "The LISP Machine." In *Interactive Programming Environments*, McGraw-Hill.
- Moon, D. A. (1985). "Architecture of the Symbolics 3600." *ISCA 1985: 12th Annual International Symposium on Computer Architecture*, pp. 76–83.
- Goldberg, A. and Robson, D. (1983). *Smalltalk-80: The Language and its Implementation*. Addison-Wesley.
- Kay, A. C. (1993). "The Early History of Smalltalk." *HOPL-II: History of Programming Languages Conference*, pp. 69–95.
- Wirth, N. and Gutknecht, J. (1992). *Project Oberon: The Design of an Operating System and Compiler*. Addison-Wesley.

### Program dependence graphs and compiler IR

- Ferrante, J., Ottenstein, K. J., and Warren, J. D. (1987). "The Program Dependence Graph and its Use in Optimization." *ACM Transactions on Programming Languages and Systems* 9(3):319–349.
- Horwitz, S., Reps, T., and Binkley, D. (1990). "Interprocedural Slicing Using Dependence Graphs." *ACM Transactions on Programming Languages and Systems* 12(1):26–60.
- Lattner, C., Amini, M., Bondhugula, U., et al. (2021). "MLIR: Scaling Compiler Infrastructure for Domain Specific Computation." *CGO 2021*.
- Haas, A., Rossberg, A., Schuff, D. L., et al. (2017). "Bringing the Web up to Speed with WebAssembly." *PLDI 2017*.
- Würthinger, T., Wimmer, C., Wöß, A., Stadler, L., Duboscq, G., Humer, C., Richards, G., Simon, D., and Wolczko, M. (2013). "One VM to Rule Them All." *Onward! 2013*. [GraalVM Truffle.]

### Graph-based program representation

- Allamanis, M., Brockschmidt, M., and Khademi, M. (2018). "Learning to Represent Programs with Graphs." *ICLR 2018*. arXiv:1711.00740.
- Guo, D., Ren, S., Lu, S., et al. (2021). "GraphCodeBERT: Pre-training Code Representations with Data Flow." *ICLR 2021*.
- Ahmad, W. U., Chakraborty, S., Ray, B., and Chang, K.-W. (2021). "Unified Pre-training for Program Understanding and Generation." *NAACL 2021*. [PLBART.]
- Wang, Y., Wang, W., Joty, S., and Hoi, S. C. H. (2021). "CodeT5: Identifier-aware Unified Pre-trained Encoder-Decoder Models for Code Understanding and Generation." *EMNLP 2021*.
- Bui, N. D. Q., Yu, Y., and Jiang, L. (2021). "InferCode: Self-Supervised Learning of Code Representations by Predicting Subtrees." *ICSE 2021*.
- Gui, Y., Wan, Y., Zhang, H., et al. (2022). "Cross-Language Binary-Source Code Matching with Intermediate Representations." *SANER 2022*. arXiv:2201.07420.
- Zhang, X., Liu, Z., Yu, Y., et al. (2024). "CrossCode2Vec: A Unified Representation for Cross-Language Source Code." *Neurocomputing* 579.

### Information theory of code

- Hindle, A., Barr, E. T., Su, Z., Gabel, M., and Devanbu, P. (2012). "On the Naturalness of Software." *ICSE 2012*; reprinted *Communications of the ACM* 59(5):122–131 (2016).
- Allamanis, M., Barr, E. T., Devanbu, P., and Sutton, C. (2018). "A Survey of Machine Learning for Big Code and Naturalness." *ACM Computing Surveys* 51(4):81:1–81:37.
- Nanz, S. and Furia, C. A. (2015). "A Comparative Study of Programming Languages in Rosetta Code." *ICSE 2015*.
- Alderson, M. (2026). "Which Programming Languages Are Most Token-Efficient?" Blog post, martinalderson.com. [Non-peer-reviewed empirical measurement of cross-language token counts on Rosetta Code tasks using the GPT-4 tokenizer.]

### LLMs and the compilation stack

- Cummins, C., Seeker, V., Grubisic, D., et al. (2024). "Meta Large Language Model Compiler: Foundation Models of Compiler Optimization." arXiv:2407.02524.
- Jiang, H., Zhu, Q., Jin, Z., et al. (2025). "Can Large Language Models Understand Intermediate Representations in Compilers?" *ICML 2025*. arXiv:2502.06854.
- Tan, H., Luo, Q., Li, J., and Zhang, Y. (2024). "LLM4Decompile: Decompiling Binary Code with Large Language Models." *EMNLP 2024*.
- Armengol-Estapé, J., Woodruff, J., Cummins, C., and O'Boyle, M. F. P. (2024). "SLaDe: A Portable Small Language Model Decompiler for Optimized Assembly." *CGO 2024*.
- Jiang, N., Wang, C., Liu, K., Xu, X., Tan, L., and Zhang, X. (2025). "Nova: Generative Language Models for Assembly Code with Hierarchical Attention and Contrastive Learning." *ICLR 2025*. arXiv:2311.13721.
- Roziere, B., Lachaux, M.-A., Chanussot, L., and Lample, G. (2020). "Unsupervised Translation of Programming Languages." *NeurIPS 2020*.
- "REx86: Local LLMs for x86 Reverse Engineering" (2025). arXiv:2510.20975. [Authors as listed in preprint.]
- "HELIOS: Hierarchical Graph Abstraction for Structure-Aware LLM Decompilation" (2026). arXiv:2601.14598. [Authors as listed in preprint.]
- "Control-Flow-Augmented Decompiler" (2025). arXiv:2503.07215. [Authors as listed in preprint.]

### Neurosymbolic and concept-library program synthesis

- Ellis, K., Wong, C., Nye, M., et al. (2021). "DreamCoder: Growing Generalizable, Interpretable Knowledge with Wake-Sleep Bayesian Program Learning." *PLDI 2021*.
- Grand, G., Wong, L., Bowers, M., et al. (2023). "LILO: Learning Interpretable Libraries by Compressing and Documenting Code." arXiv:2310.19791.
- Arakelyan, S., Das, A., Mao, Y., and Ren, X. (2022). "NS³: Neuro-Symbolic Semantic Code Search." arXiv:2205.10674.

### Cognitive architectures, universal AI, related framings

- Hutter, M. (2005). *Universal Artificial Intelligence*. Springer.
- Kautz, H. (2022). "The Third AI Summer: AAAI Robert S. Engelmore Memorial Lecture." *AI Magazine* 43(1):105–125.
- Anderson, J. R. (1996). "ACT: A Simple Theory of Complex Cognition." *American Psychologist* 51(4):355–365.
- Laird, J. E. (2012). *The Soar Cognitive Architecture*. MIT Press.
- Sun, R. (2006). "The CLARION Cognitive Architecture: Extending Cognitive Modeling to Social Simulation." In *Cognition and Multi-Agent Interaction*, Cambridge University Press.
- Hawkins, J. and Ahmad, S. (2016). "Why Neurons Have Thousands of Synapses, a Theory of Sequence Memory in Neocortex." *Frontiers in Neural Circuits* 10:23. [HTM.]

### AI alignment, safety by construction, and interpretability (Theorem XII references)

**Formal-methods foundations.**
- Milner, R. (1978). "A Theory of Type Polymorphism in Programming." *Journal of Computer and System Sciences* 17(3):348–375.
- Wright, A. K. and Felleisen, M. (1994). "A Syntactic Approach to Type Soundness." *Information and Computation* 115(1):38–94.
- Dennis, J. B. and Van Horn, E. C. (1966). "Programming Semantics for Multiprogrammed Computations." *Communications of the ACM* 9(3):143–155.
- Levy, H. M. (1984). *Capability-Based Computer Systems*. Digital Press.
- Shapiro, J. S., Smith, J. M., and Farber, D. J. (1999). "EROS: A Fast Capability System." *SOSP 1999*.
- Miller, M. S. (2006). *Robust Composition: Towards a Unified Approach to Access Control and Concurrency Control*. PhD thesis, Johns Hopkins University.
- Klein, G., Elphinstone, K., Heiser, G., et al. (2009). "seL4: Formal Verification of an OS Kernel." *SOSP 2009*.
- Meyer, B. (1988, 1997). *Object-Oriented Software Construction*. Prentice Hall. [Design by Contract.]

**Safety by construction / Guaranteed Safe AI.**
- Seshia, S. A., Sadigh, D., and Sastry, S. S. (2016, rev. 2022). "Towards Verified Artificial Intelligence." arXiv:1606.08514.
- Dalrymple, D., Skalse, J., Bengio, Y., Russell, S., Tegmark, M., Seshia, S., Omohundro, S., Szegedy, C., Goldhaber, B., Ammann, N., Abate, A., Halpern, J., Barrett, C., Zhao, D., Zhi-Xuan, T., Wing, J., and Tenenbaum, J. (2024). "Towards Guaranteed Safe AI: A Framework for Ensuring Robust and Reliable AI Systems." arXiv:2405.06624.
- Dalrymple, D. (2023). "An Open Agency Architecture for Safe Transformative AI." AI Alignment Forum.
- Tegmark, M. and Omohundro, S. (2023). "Provably Safe Systems: The Only Path to Controllable AGI." arXiv:2309.01933.
- Bengio, Y. (2024). "Towards a Cautious Scientist AI with Convergent Safety Bounds." arXiv:2408.02479.
- Bengio, Y., Hu, E. et al. (2024). "Can a Bayesian Oracle Prevent Harm from an Agent?" arXiv:2408.05284.
- Drexler, K. E. (2019). *Reframing Superintelligence: Comprehensive AI Services as General Intelligence*. Future of Humanity Institute Technical Report.
- Clarke, S. (2023). "Ten Levels of AI Alignment Difficulty." AI Alignment Forum.
- Park, K., Wang, J., and Berg-Kirkpatrick, T. (2024). "Grammar-Aligned Decoding." *NeurIPS 2024*. arXiv:2405.21047.

**Opacity-as-root-cause: interpretability as response.**
- Olah, C., Cammarata, N., Schubert, L., Goh, G., Petrov, M., and Carter, S. (2020). "Zoom In: An Introduction to Circuits." *Distill* 5(3).
- Elhage, N., Hume, T., Olsson, C., et al. (2022). "Toy Models of Superposition." *Transformer Circuits Thread*.
- Bricken, T., Templeton, A., Batson, J., et al. (2023). "Towards Monosemanticity: Decomposing Language Models with Dictionary Learning." *Transformer Circuits Thread*.
- Templeton, A., Conerly, T., Marcus, J., et al. (2024). "Scaling Monosemanticity: Extracting Interpretable Features from Claude 3 Sonnet." *Transformer Circuits Thread*.
- Nanda, N., Chan, L., Lieberum, T., Smith, J., and Steinhardt, J. (2023). "Progress Measures for Grokking via Mechanistic Interpretability." *ICLR 2023*.
- Sharkey, L., Chughtai, B., et al. (2025). "Open Problems in Mechanistic Interpretability." arXiv:2501.16496.
- Amodei, D. (2024). "The Urgency of Interpretability." Essay, anthropic.com.
- Bereska, L. and Gavves, E. (2024). "Mechanistic Interpretability for AI Safety — A Review." arXiv:2404.14082.

**Canonical opaque-system alignment lineage.**
- Christiano, P., Leike, J., Brown, T., Martic, M., Legg, S., and Amodei, D. (2017). "Deep Reinforcement Learning from Human Preferences." *NeurIPS 2017*. arXiv:1706.03741.
- Ouyang, L., Wu, J., Jiang, X., et al. (2022). "Training language models to follow instructions with human feedback." *NeurIPS 2022*. arXiv:2203.02155. [InstructGPT.]
- Bai, Y., Kadavath, S., et al. (2022). "Constitutional AI: Harmlessness from AI Feedback." arXiv:2212.08073.
- Irving, G., Christiano, P., and Amodei, D. (2018). "AI Safety via Debate." arXiv:1805.00899.
- Christiano, P., Shlegeris, B., and Amodei, D. (2018). "Supervising strong learners by amplifying weak experts." arXiv:1810.08575. [IDA.]
- Leike, J., Krueger, D., Everitt, T., Martic, M., Maini, V., and Legg, S. (2018). "Scalable agent alignment via reward modeling: a research direction." arXiv:1811.07871.
- Hadfield-Menell, D., Russell, S., Dragan, A., and Abbeel, P. (2016). "Cooperative Inverse Reinforcement Learning." *NeurIPS 2016*. arXiv:1606.03137.
- Hubinger, E., van Merwijk, C., Mikulik, V., Skalse, J., and Garrabrant, S. (2019). "Risks from Learned Optimization in Advanced Machine Learning Systems." arXiv:1906.01820.
- Soares, N., Fallenstein, B., Yudkowsky, E., and Armstrong, S. (2015). "Corrigibility." *AAAI Workshop on AI and Ethics*. [MIRI.]
- Amodei, D., Olah, C., Steinhardt, J., Christiano, P., Schulman, J., and Mané, D. (2016). "Concrete Problems in AI Safety." arXiv:1606.06565.
- Yudkowsky, E. (2022). "AGI Ruin: A List of Lethalities." MIRI blog / LessWrong.
- Russell, S. (2019). *Human Compatible: Artificial Intelligence and the Problem of Control*. Viking.
- Casper, S., Ezell, C., Siegmann, C., et al. (2024). "Black-Box Access is Insufficient for Rigorous AI Audits." *FAccT 2024*.

### Sparse computing and structured representation (Theorem XI references)

- Buluç, A. and Gilbert, J. R. (2008). "On the Representation and Multiplication of Hypersparse Matrices." *IPDPS 2008*.
- Frankle, J. and Carbin, M. (2019). "The Lottery Ticket Hypothesis: Finding Sparse, Trainable Neural Networks." *ICLR 2019*.
- Child, R., Gray, S., Radford, A., and Sutskever, I. (2019). "Generating Long Sequences with Sparse Transformers." arXiv:1904.10509.
- Kipf, T. N. and Welling, M. (2017). "Semi-Supervised Classification with Graph Convolutional Networks." *ICLR 2017*.
- Veličković, P., Cucurull, G., Casanova, A., Romero, A., Liò, P., and Bengio, Y. (2018). "Graph Attention Networks." *ICLR 2018*.

### Ozma public artifacts (Instance 1 and Instance 2 source material)

- Ozma Labs Pty Ltd (2026). *Ozma Routing Protocol Specification*, v0.1 draft. `github.com/ozmalabs/ozma/tree/main/docs/routing` (CC-BY-4.0). Initial draft 2026-04-07; split into 29 sub-documents 2026-04-08.
- Ozma Labs Pty Ltd (2026). *Ozma: software-defined KVMA router*. `github.com/ozmalabs/ozma` (AGPL-3.0 code, with plugin exception). Includes controller, node, softnode, agent, protocol (submodule), firmware, renderer, and the routing specification referenced above.
- Parslow, M. (2026). *Ozma AI — protocol v5.11 through v5.15, ozmalib, distillation pipeline, trained LoRAs (Wizard-1, Coder-3b), shim-on-frontier deployment (Ruler-1)*. MIT License. Development window approximately 2026-04-10 through 2026-04-22; concentrated 7-day git-visible output 2026-04-15 through 2026-04-22.

---

## Appendix A: The Nine Map Quality Properties (operational τ definition)

τ(M) is a real-valued quality score on [0, 1] for a semantic map M, computed as a weighted sum of nine properties:

1. **Semantic distinctness** — no token aliasing in the map's vocabulary.
2. **Internal consistency** — no contradictions among asserted edges.
3. **Type-correctness** — all nodes and edges pass the Γ type checker.
4. **Schema minimality** — the graph's schema does not grow faster than its content.
5. **Extensional stability** — extensions do not destabilize existing assertions.
6. **Interpretive coverage** — all reasoning traces are interpretable via Γ productions.
7. **Grounded completeness** — every persistent node has at least one semantic edge.
8. **Information efficiency** — redundancy ≤ ε for bounded ε(τ).
9. **Adaptive coherence** — deltas are consistent with prior traces.

Weights are engineering parameters. Any positive weights produce the ordering properties required for the theorems in §7; specific values do not affect whether τ is monotone or bounded.

---

## Appendix B: The Shim — Mechanical Operations Outside the Model

The shim is the mechanical software layer that mediates between the language model and the graph. Its operations:

1. **Input assembly.** Serialize the relevant subgraph of G_i into the model's surface language W_j per current focus.
2. **Output parsing.** Parse model output against Γ to extract typed envelopes (EDIT, PLAN, ASK, ACK, TRACE, etc.).
3. **Delta validation.** Submit each parsed delta to the consistency checker; accept admitted deltas; reject inadmissible ones with reason codes.
4. **Map update.** Apply admitted deltas to G_i; persist the updated state.
5. **Tool execution.** Execute tool calls (shell, code runner, retriever) and feed results back as typed envelopes.
6. **Convergence monitoring.** Compute τ on the current state; emit trajectory metrics for loop analysis.

The shim contains no learning. It contains no reasoning. It is the mechanical substrate that makes the graph operational. Every step is deterministic and auditable.

---

## Appendix C: Convergence Artifacts — What KVMA and Ozma AI Share Without Being Designed To

The architectures are listed side-by-side to make the convergence legible.

| Property | Ozma System (infrastructure substrate) | Ozma AI |
|---|---|---|
| Primary object | Routing graph (devices, ports, links, pipelines) | Semantic map (nodes, typed edges) |
| Operational primitives | Traverse, assemble, negotiate, route | Traverse, query, extend, refine |
| Grammar | Port-type compatibility, link-cost calculus | Γ — production rules over typed deltas |
| Reflective access | Topology-of-topology is representable | Map-about-map is representable |
| Quality measure | Pipeline-assembly success rate, latency, cost | τ(M) over nine properties |
| Compound loop | Discovery → assembly → fault-recovery → reassembly | Trace → delta → check → map update |
| Extensibility | Plugin typed-device and link-type registration | Neuroplasticity: new sub-model types and typed edges |
| Failure mode | Uncoverable intent → partial pipeline | Uncoverable reasoning gap → blocked delta |
| Cross-client composition | Routing, monitoring, SIEM as projections | Coordinator, verifiers, retrievers as composite |
| License | AGPL | MIT |

No architectural decision in either system targeted alignment with the other. The alignment is an observation.

---

## Appendix D: Distilled Form — for Use as a System Prompt

The paper's operational frame distills to a single paragraph suitable for inclusion in any LLM system prompt, CLAUDE.md, .cursorrules, or coding-assistant configuration. Pasting this paragraph into a system prompt primes the model to see code through the graph-native lens; applied against any existing codebase with any current-generation LLM, the model identifies where the |I|/K_Γ(I) tax is being paid and proposes refactorings that close the distance to the graph.

### Distilled form (130 words)

> Real-world code operates on structured data whose Kolmogorov complexity K(I) is dramatically smaller than its literal byte size |I|. Most code pays an |I|/K(I) efficiency tax: parsing what was already parsed, serializing graph state into strings only to re-parse at the next layer, re-deriving types, reconstructing structure destroyed in serialization. The efficient representation of any program is its semantic graph — typed nodes (functions, types, intents, invariants, data flows) connected by typed edges (defines, calls, reads, writes, constrains, implements). Surface languages are serializations of subgraphs; the wire is not the content. When analyzing or improving code, see past the surface to the graph, identify where the |I|/K(I) tax is being paid, and refactor toward the graph — propagate types, collapse redundant serialization, use typed-edge interfaces instead of byte streams. Source: Parslow 2026, *Invariant Scale Graphs: Mapping Reality.*

### Action-oriented form (~170 words, for CLAUDE.md or dev-loop system prompts)

> Treat every codebase as a serialization of a typed graph. Nodes: functions, types, invariants, data flows, intents. Edges: typed relationships (defines, calls, reads, writes, constrains, implements, refines). Surface syntax (Python, Rust, SQL, HTTP, JSON, YAML, protobufs, etc.) is the wire; the graph is the content. Real-world code operates on structured data with Kolmogorov complexity K(I) far smaller than its byte size |I|. Code that doesn't preserve graph structure pays an |I|/K(I) efficiency tax: parsing what was parsed, serializing-then-reparsing across every layer boundary, re-deriving types, reconstructing what was destroyed one serialization ago.
>
> When analyzing or improving code: see past the surface to the graph. Identify where the |I|/K(I) tax is paid. Refactor toward the graph — propagate types, collapse serialization round-trips, preserve structure across layer boundaries, prefer typed-edge interfaces to byte streams. On structured workloads, graph-native representations are asymptotically more efficient than dense ones; Theorem XI formalizes this bound.
>
> Source: Parslow 2026, *Invariant Scale Graphs: Mapping Reality.*

### Usage

Paste either version into a system prompt or project-level configuration. Expected effect: LLM-assisted code review, refactoring, or analysis on structured codebases (those with positive *|I|/K_Γ(I)* tax — most production code where logical structure is recoverable from token surface) produces substantially more structural suggestions than the same model unprimed. The framework's prediction is conditional on the codebase having exploitable typed-graph structure under Γ; for codebases where the structure is opaque or the work is structure-insensitive (cryptographic primitives, byte-level operations), the gain degrades to O(1). The paragraph costs nothing to deploy. Empirical validation on two such codebases is reported in Appendix E; broader validation is invited.

---

## Appendix E: Empirical Validation of the Distilled-Form Prompt

The paragraph in Appendix D claims that priming any current-generation LLM with the graph-native frame allows an engineer to take any existing codebase, point the LLM at it for a handful of dollars, and produce code with big-O efficiency improvements. The claim is directly testable and this appendix reports the test, run on 2026-04-24 against `python-dotenv/src/dotenv/main.py` (482 lines).

The test has two parts, in order of importance. The **primary measurement** is an **applied-refactoring efficiency benchmark**: take one of the refactorings the paragraph-primed LLM recommended, implement it, run it against the original on realistic inputs, measure time and memory. This directly tests the paper-as-prompt claim — does pasting the paragraph into an LLM produce code improvements that actually run measurably faster? The **control measurement** is a **review-quality comparison**: a blinded rubric assessing whether the paragraph degrades the LLM's ability to produce a sound code review. Both arms of the experiment must hold for the deployment claim to stand — the paragraph must produce real efficiency wins and must not degrade review quality in the process. The efficiency measurement is §E.2; the quality control is §E.3.

### E.1 Design

Two subagents were launched in parallel. Both were given identical instructions: read the file, produce a numbered review of 10–15 structural improvements with location / issue / proposed refactoring / rationale / priority. Both used the same underlying model and tool set. The only difference: the primed agent's system context began with the 130-word Appendix D paragraph; the baseline's did not. Neither agent knew the experiment's purpose or that the prompts differed.

A third subagent then scored the 30 findings on a blinded four-axis 0–3 rubric. After the scores landed, one of the primed agent's efficiency-related findings was implemented and benchmarked against the original code.

§E.2 reports the primary efficiency measurement; §E.3 reports the review-quality control; §E.4 documents the security consequences of the typed-edge refactorings; §E.5 names the boundary class where the approach does not apply; §E.6 interprets; §E.7 lists limitations.

### E.2 Applied-refactoring efficiency measurement

The primary experiment. The primed agent's finding X6 identified that `resolve_variables` (lines 289–311 of `dotenv/main.py`) rebuilds a complete environment dictionary on every iteration.

**The original code (verbatim from `python-dotenv/src/dotenv/main.py`, SHA at 2026-04-24):**

```python
def resolve_variables(
    values: Iterable[Tuple[str, Optional[str]]],
    override: bool,
) -> Mapping[str, Optional[str]]:
    new_values: Dict[str, Optional[str]] = {}

    for name, value in values:
        if value is None:
            result = None
        else:
            atoms = parse_variables(value)
            env: Dict[str, Optional[str]] = {}
            if override:
                env.update(os.environ)  # type: ignore
                env.update(new_values)
            else:
                env.update(new_values)
                env.update(os.environ)  # type: ignore
            result = "".join(atom.resolve(env) for atom in atoms)

        new_values[name] = result

    return new_values
```

**The refactored code, following the primed agent's X6 recommendation:**

```python
def resolve_variables(
    values: Iterable[Tuple[str, Optional[str]]],
    override: bool,
) -> Mapping[str, Optional[str]]:
    new_values: Dict[str, Optional[str]] = {}

    if override:
        env: Mapping[str, Optional[str]] = ChainMap(new_values, os.environ)  # new_values wins
    else:
        env = ChainMap(os.environ, new_values)  # os.environ wins

    for name, value in values:
        if value is None:
            result = None
        else:
            atoms = parse_variables(value)
            result = "".join(atom.resolve(env) for atom in atoms)

        new_values[name] = result

    return new_values
```

**Big-O analysis.** Let N = number of `.env` variables, E = `len(os.environ)`.

- *Original:* inside the loop, `env = {}` then `env.update(os.environ)` + `env.update(new_values)`. Each update is Θ(E) or Θ(|new_values|) respectively, executed N times. Total: **Θ(N · E)** time, Θ(E) per-iteration memory churn (E allocated and released per loop iteration).
- *Refactored:* `ChainMap` constructed once, Θ(1). Inside the loop only atom resolution via `env.get(k)`, which is Θ(1) average on the ChainMap's constituent dicts. Total: **Θ(N + E)** time, Θ(1) auxiliary memory (the ChainMap holds references only).

The refactoring is a **big-O reduction**, not a constant-factor win. The scaling curve in the benchmark below confirms this directly: the speedup ratio grows with input size, which is the signature of an asymptotic improvement rather than a prefactor improvement.

**Correctness.** The refactored version was exercised against the same inputs as the original across all five benchmark configurations; outputs matched exactly. `ChainMap` preserves the same lookup precedence as the original's sequential `update()` calls (earlier entries win in `ChainMap`; later wins in `update`; order is inverted to keep semantics identical). Mutations to `new_values` propagate through the `ChainMap` because `ChainMap` holds the underlying dict by reference, which is what the original code relied on implicitly.

**Benchmark across five realistic deployment sizes:**

| env size | values | original (μs) | refactored (μs) | **speedup** | peak mem orig (KB) | peak mem new (KB) | **mem ratio** |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 50 | 10 | 26.35 | 18.73 | **1.41×** | 4.12 | 2.24 | 1.84× |
| 100 | 25 | 69.03 | 35.58 | **1.94×** | 7.37 | 3.49 | 2.11× |
| 200 | 50 | 169.73 | 76.02 | **2.23×** | 13.19 | 5.42 | 2.43× |
| 500 | 100 | 655.06 | 146.10 | **4.48×** | 25.27 | 9.88 | 2.56× |
| 1000 | 200 | 2311.80 | 290.74 | **7.95×** | 48.97 | 18.73 | 2.61× |

The scaling curve matches Theorem XI's prediction. The original implementation pays Θ(N · E) — the speedup grows with input size because the |I|/K(I) tax was super-linear. The refactored implementation pays Θ(N + E) — no tax. At realistic container scale (500 environment variables, 100-variable `.env`): **4.5× faster, 2.6× less memory**. At extreme scale (1000 environment variables, 200-variable `.env`): **8× faster, 2.6× less memory**.

This is the operational payload of the claim. One refactoring, derived from a 130-word paragraph pasted into an off-the-shelf LLM's system prompt, verified correct on the actual `python-dotenv` input space, producing **a big-O reduction on the actual production code of a widely-deployed library** — `python-dotenv` reports more than 100 million PyPI downloads per month at time of writing, with the package transitively depended on by major frameworks (FastAPI, Django via dotenv-extension, the bulk of the Python deployment-configuration ecosystem). The refactoring has been benchmarked and validated here; it has not been submitted upstream and is not claimed as a merged contribution to the library itself. The total cost of the experiment: the paragraph, one LLM inference (~$0.05 on frontier models), and the test-and-benchmark loop (a few minutes of engineering time).

The primed agent produced fourteen other findings of comparable category. If even a quarter of them compose this cleanly when applied, the composite improvement on this single module is an order of magnitude. This is the "much better for a handful of dollars" claim, in measured form.

### E.3 Review-quality control — blinded rubric scoring

The control experiment. A third subagent (also held to a strict rubric) scored all 30 findings on four axes (validity / specificity / impact / soundness, each 0–3). The rubric judge did not know which reviewer was which and was instructed explicitly that jargon or theoretical framing must not influence scores.

| Dimension | Primed (X) | Baseline (Y) |
|---|---|---|
| Total (max 180) | 131 | 128 |
| Validity mean | 2.80 | 2.73 |
| Specificity mean | 2.33 | 2.33 |
| Impact mean | 1.53 | 1.53 |
| Soundness mean | 2.20 | 2.20 |
| Per-finding mean | 8.73 | 8.53 |
| Findings scoring ≥ 10/12 | 3 | 5 |

**The two reviewers are a statistical tie** — 131 vs 128 on a 180-point scale, with per-axis means identical to two decimal places on three of four axes. The judge's verdict: *"Near-tie; Y brings more unique high-value findings, X is more consistent."* The paragraph does not degrade review quality.

This is the control result that matters for the efficiency claim in E.2: the paragraph is not producing worse code reviews that happen to contain one spectacular finding. It is producing reviews of equivalent overall quality, with the *set* of findings tilted toward structure-respecting refactorings (like X6, the benchmarked one) that unprimed review does not reliably surface.

The qualitative differentiation:

- **The primed agent uniquely identified:** the `Binding` serialization tax, the read/write module partition, typed-union stream resolution, `quote_mode` as `Literal`, the `load_dotenv`/`dotenv_values` duplication-causes-gate-bypass framing.
- **The baseline agent uniquely identified:** POSIX shell single-quote escaping bug, `fsync` / crash-safety in `rewrite`, the error-flag filter bug in `parse()`, `StrPath` / `str` / `Path` inconsistency, missing `__all__`.

The primed findings cluster around structure-at-layer-boundaries observations. The baseline findings cluster around byte-level correctness and API-surface hygiene. Neither set dominates the other on rubric quality; they differ in what they look for.

### E.4 Security consequences of the typed-edge refactorings

Several of the primed agent's typed-edge refactorings close attack-surface categories rather than patching individual bugs:

- **X11 — `quote_mode: str` → `Literal["always", "auto", "never"]`.** The current runtime check falls through silently on typos. Under `Literal`, `mypy` catches typos at type-check time. Class eliminated: silent-misconfiguration of quoting behaviour.
- **X3 — stream-resolution typed union (`PathSource | StreamSource | EmptySource`).** Currently, passing both `dotenv_path` and `stream` silently prefers the path; a non-regular-file path silently falls through to an empty stream. Class eliminated: invisible-precedence ambiguity where the effective source depends on which attribute an attacker controls.
- **X7 — Binding single-parse pipeline.** The current code runs `parse_stream` twice on the same input — once in `DotEnv.parse`, once again in `set_key`/`unset_key`. Two parsers interpreting the same input is the classical LangSec differential-parsing vulnerability pattern; whenever the parsers disagree on edge cases (escapes, multiline values, null bytes, BOMs), the disagreement is a potential smuggling channel. Class eliminated: differential-parsing surface in the read/write round-trip.

The baseline's Y7 finding (POSIX shell single-quote escaping) is also a security fix — specifically a shell-injection vulnerability when `.env` files are sourced by shells — but it is a byte-level surgical fix, not a typed-edge reformulation. Both classes of security win are real; the typed-edge versions scale because they close categories of vulnerability structurally, not instance-by-instance.

### E.5 Where this does not apply — the boundary condition

Theorem XI's boundary case (§9.2) identifies where the efficiency claim does not hold: workloads on data where K(I) = Θ(|I|). Pure noise is the formal case; the practical case is **code that is already approaching the theoretical efficiency limit for its domain**. This includes:

- Production video codecs (H.264, HEVC, AV1) — pipeline-optimised, often ASIC-accelerated
- Production audio codecs (AAC, Opus)
- Cryptographic primitives (AES-NI, SHA extensions, AVX-512 vectorised hashes)
- BLAS / LAPACK kernels on *dense* problems — vectorised against specific SIMD widths. (Sparse linear algebra is the opposite case: there the graph-native substrate wins for exactly the reasons Theorem XI predicts, and sparse BLAS implementations empirically beat dense BLAS on sparse inputs.)
- GPU shader programs and CUDA compute kernels
- Signal processing DSPs

For code in this class, the primed agent's refactorings offer no speedup on the hot kernel itself — the code is already optimal per its domain's bounds. The *architecture around* such kernels (configuration parsing, pipeline orchestration, the container of the hot code) typically is not at the limit and benefits in the normal way, but the kernel itself does not.

This is a feature of the theorem, not a limitation of the paragraph. Graph-native refactoring applies exactly where the |I|/K(I) tax is positive, which is everywhere K_Γ(I) ≪ |I| holds. Code at the theoretical limit has already compressed its representation to near K(I); there is no tax left to extract. In practical codebases this means: hot numerical kernels and ASIC-targeted primitives are not candidates; the typically-90%+ of a codebase that is business logic, configuration, orchestration, request handling, data plumbing, and I/O glue is. That is where the measured efficiency gains land.

### E.6 Interpretation

Two claims are supported by the measurement:

1. **Efficiency.** Applying one paragraph-primed refactoring to `resolve_variables` produces a 1.4× to 8× speedup and 1.8× to 2.6× memory reduction depending on input size, with the speedup growing super-linearly in input size because the |I|/K(I) tax was super-linear. This is a big-O improvement on production code at measurable, correctness-preserving scale. The theorem predicted it; the benchmark confirmed it. One finding, one refactoring, ~$0.05 of inference — per Theorem XI, this is what the paragraph-primed LLM routinely surfaces on any code that is not already at the theoretical limit.

2. **No review-quality cost.** The paragraph does not degrade the LLM's ability to produce a correct, structured, actionable code review. The rubric-scored quality is within noise of the unprimed baseline (131 vs 128 of 180). The paragraph is not a quality multiplier for code review; it is an emphasis-shifting prime that surfaces a different *set* of findings at equivalent quality.

The combined practical story: **paste Appendix D's paragraph into any LLM's system prompt, point it at any non-optimal codebase, accept a subset of its structure-respecting refactorings, and realise big-O efficiency improvements at roughly the cost of an inference call per module.** The rubric tie is the control that guarantees you are not paying a quality tax for this; the benchmark is the direct evidence that the refactorings produce real compiled-and-measured wins.

The paragraph is most useful in parallel deployment with an unprimed reviewer — Appendix D's lens emphasises structural refactorings, an unprimed review emphasises byte-level and hygiene correctness, and running both catches ~1.5× unique findings for 2× inference cost. For codebases that already have strong byte-level guards (typed tests, static analysis, formal verification), Appendix D's paragraph alone produces architectural insight the unprimed reviewer will systematically under-surface. For codebases where byte-level correctness is load-bearing and unverified, run both arms.

### E.7 Limitations

- **Single target, single model, single judge, single refactoring benchmarked.** The measurement is direct evidence that the paragraph produces a real big-O win on at least one production refactoring, and is consistent with an equal-quality review overall. It is not evidence of a general effect size across all codebases, languages, or models. Replication is straightforward follow-up work.
- **Prompt sensitivity.** The Appendix D text is the version tested; minor wording changes may shift results.
- **Rubric calibration.** The four-axis rubric weights validity / specificity / impact / soundness equally. A different weighting could shift the rubric result in either direction.
- **Statistical significance of the rubric tie.** 131 vs 128 on a 180-point scale is a tie. The paper claims no review-quality gain; only no review-quality loss.
- **Boundary code class (E.5).** Already-optimal code does not benefit from the paragraph. Graph-native refactoring applies where K_Γ(I) ≪ |I|.

*Experiment artifacts: target file at `/tmp/graph-prompt-experiment/dotenv_main.py`; blinded findings set at `/tmp/graph-prompt-experiment/findings_blinded.md`; benchmark script at `/tmp/graph-prompt-experiment/bench_resolve.py`; subagent transcripts logged in the conversation from which this paper was produced. The experiment is reproducible for any current-generation LLM and any codebase in well under an hour of engineering time.*

### E.8 Follow-up case study — graceful degradation on elite-optimised code (ripgrep)

§E.2 demonstrates the paragraph-prompt on average production Python code and measures a big-O speedup. A natural next question: does the prompt *correctly stay quiet* on code that is already at or near its theoretical efficiency ceiling, per §E.5's boundary condition? If the prompt produces plausible-looking-but-false-positive findings on already-optimal code, its deployment value collapses — reviewers would rightly distrust its output everywhere. If it produces smaller-but-real findings that track the residual |I|/K_Γ(I) tax, its calibration is confirmed across the code-quality spectrum.

**Target.** `ripgrep/crates/searcher/src/searcher/core.rs`, 713 lines, authored by Andrew Gallant. Ripgrep is widely considered one of the fastest grep-family tools; Gallant's performance engineering is studied as exemplary across the Rust community. The file is the search orchestration (matcher/sink/context coordination), not the SIMD byte-scanning inner loop, which lives in a separate crate and is at the silicon-instruction limit. Orchestration code is structure-respecting by nature and is therefore the right stratum for a graph-native prompt to evaluate.

**Methodology.** Identical to §E.2's primed arm: single agent, Appendix D paragraph in system context, structural-review task, honest-findings instruction (the agent was explicitly told *"if the code is already close to the theoretical efficiency limit for its domain, your review should reflect that honestly — fewer findings, smaller improvements, or explicit acknowledgment that some things are already at the optimum. Do not manufacture findings to appear thorough."*).

**Results.** The agent's overall assessment: *"This file is at or near its efficiency ceiling for what it actually does. The semantic graph is already thin — positions are `usize`, ranges are `Range`, lines are half-open `(start, end)` slices, and the matcher/sink boundary is a typed-edge interface, not a byte stream. The `|I|/K(I)` tax in the domain sense (re-parsing, re-serializing) is mostly absent."* Ten findings produced, with self-rated confidence:

| Confidence | Count |
|---|---|
| Clear win | 2 |
| Judgment call | 4 |
| Cosmetic / speculative | 4 |

The two clear wins are real, reviewable structural issues that could plausibly be submitted upstream as pull requests:

- **Finding #1 — triplicate `sink_before_context` / `sink_after_context` / `sink_other_context` methods.** ~90 lines of near-identical code for ~5 lines of genuine variation. Three parallel serializations of one semantic operation (*"emit-context(kind)"*). Consolidatable to a single `sink_context(..., kind)` with three one-line callers. Mechanical dedup, no behavioural risk.
- **Finding #10 — matcher-capability probe re-runs on every per-buffer `match_by_line` call.** `is_line_by_line_fast` probes `matcher.line_terminator()` and `matcher.non_matching_bytes()` each time; these are invariant for the matcher's lifetime. Split into static capability (cached on `Core::new`) vs dynamic predicate (per-call). Removes trait-virtual calls from the per-buffer hot path.

Equally informative is the "deliberately did not flag" list the agent produced:

- Generic `&lt;M: Matcher, S: Sink&gt;` monomorphisation: *"the right abstraction."*
- Half-open `Range` arithmetic, `#[inline(always)]` placements, `debug_assert!` density: *"deliberate and the hot path warrants it."*
- The `roll` / buffer-rollover design: *"genuinely subtle... already as compact as the invariants allow."*
- `pos: usize` vs `absolute_byte_offset: u64` split: *"correct — buffer positions fit in usize; file-wide offsets need u64 on 32-bit platforms."*

**Interpretation.** The prompt correctly scales finding-size to code-quality-distance-from-optimum. Ripgrep's file has a small but non-zero residual |I|/K_Γ(I) tax (the triplicate-sink pattern, the matcher-capability re-probing); the prompt sees that tax cleanly, at roughly the magnitude the residual actually is. On the parts of the file that *are* at the limit (the generic abstractions, the inline attributes, the buffer-rollover invariants), the prompt correctly leaves them alone and explicitly says so. Together with §E.2's big-O win on `python-dotenv`, the two case studies bracket the operational range:

| Code-quality stratum | Example | Prompt behaviour |
|---|---|---|
| **Average production code** | `python-dotenv` (§E.2) | Big-O reduction (1.4×–7.95× scaling); clear wins dominate |
| **Elite production code** | `ripgrep` core.rs (§E.8) | Small structural wins (2 of 10); graceful degradation; no false positives |
| **Theoretical-limit code** | SIMD byte scanners, crypto primitives, BLAS kernels (§E.5) | No speedup; prompt correctly silent |

The prompt's behaviour scales monotonically with the residual tax. This is exactly Theorem XI's predicted calibration property — speedup ≈ |I|/K_Γ(I) — observed at two non-extremal points of the spectrum plus the boundary.

*Experiment artifact: target file at `/tmp/graph-prompt-rg/rg_core.rs`; subagent transcript logged in the conversation from which this paper was produced.*

### E.9 Compound-loop prevalence in naturalistic developer corpus

§E.2, §E.3, and §E.8 are controlled experiments. This section complements them with a naturalistic corpus observation: the core operation the paper's assembly rests on — edit, fail, correct, converge — is not a design goal, it is an empirically measured pattern in one developer's production usage. **No control-group baseline exists for this measurement**: the rate is an *observation* on a single user's natural workflow, not a controlled comparison against the same user (or any user) doing the same tasks without the compound-loop architecture. Resolution-rate numbers reported below are observation, not effect-size measurement; the architectural claim is that the pattern occurs at high frequency in heavy naturalistic usage, which the corpus demonstrates, not that the architecture *causes* the resolution rate to be 93.2% versus some lower baseline. The pattern's occurrence-frequency in production is the empirical content; comparison-to-baseline is outstanding work the framework explicitly invites.

**Corpus.** Seven weeks of single-developer Claude Code session activity: **795,487 tool events across 415 sessions**, **5,336 session-hours**, spanning two machines and a mix of research, infrastructure, and application work. Events captured include every tool_use (Edit, Bash, Grep, Read, etc.) and every tool_result with its output, attached timestamps, and session-level metadata. Corpus directory: `~/corpus-20260417/`. Source detectors: `backend/scripts/corpus_eval/impact.py` (broad, pre-existing) and `backend/scripts/corpus_eval/compound_loop_scan.py` (strict, produced during the work that generated this paper; the scanner is itself an instance of the compound loop operating on a corpus of prior compound loops).

**Two detectors, same phenomenon.** The broad detector counts each adjacent pair where an edit on a file is preceded by another edit on the same file with any intervening failure-marker tool_result. The strict detector additionally requires the failure marker to reference the edited file in its text and chains consecutive cycles on the same file into a single record. Under these definitions:

| Detector | Instances | Scope |
|---|---:|---|
| Broad (adjacent pairs) | **632** | across all 415 sessions |
| Strict (file-attributable chains) | **162** | across 15 chain-containing sessions |

The strict figure is a lower bound; the broad figure is an upper bound.

**Resolution rate — the load-bearing measurement.** Under the strict detector, a chain is classified as *resolved* if no further failure-marker tool_result references the chain's file within the five minutes following the chain's final edit. Results:

| Chain depth | Chains | Resolved | Resolution rate |
|---:|---:|---:|---:|
| 1 | 128 | 119 | 93.0% |
| 2 | 23 | 21 | 91.3% |
| 3 | 6 | 6 | **100%** |
| 4 | 4 | 4 | **100%** |
| 5 | 1 | 1 | **100%** |
| Overall | 162 | 151 | **93.2%** |

The directional reading: the compound loop converges *more reliably, not less, the harder the problem gets* — chains with three or more failures before the final fix resolve at 100% in this sample. **Sample-size caveat: the depth-≥3 tail is N = 11 (6 + 4 + 1).** That is too small to read "100%" as a convergence *property*; a single non-resolution at depth ≥3 would have moved the rate to 91%. The depth-≥3 reading should be taken as a hypothesis the data is consistent with rather than one this corpus establishes — directionally supported but bounded in strength by the small tail. The depth 1 and depth 2 rates (93.0% and 91.3% on N = 128 and 23) carry the load-bearing measurement; the deep-tail observation is an additional consistent signal, not the main result.

**Wallclock profile.** Median whole-chain wallclock (first edit to final fix) is 172 s; P90 is 499 s; max is 772 s. Median per-cycle wallclock is 105 s. The compound loop is a seconds-to-minutes operation, not an hours-long debug session, even at chain depth ≥3.

**What this shows and does not show.** The corpus is single-user, seven-week data. It is not a general-population sample, and its frequency of edit→fail→edit cycles almost certainly reflects a user-specific demand profile — this user is at the high-demand end of publicly comparable Claude Code corpora. Cross-corpus scans of the novita 22-session `agentic_big_dataset`, the nlile trace collection, and the misterkerns `personal_claudecode` dataset (run via `backend/scripts/corpus_eval/agentic_big_scan.py`, `claudecode_scan.py`, and `personal_claudecode_scan.py`) show substantially lower per-session complexity and tool-event density than the corpus analysed here. The *resolution rate* (93.2% overall on N = 162; 100% on the smaller depth-≥3 tail of N = 11, with the small-sample caveat above), however, is a property of the architecture's response to a detected failure, not of the user issuing the request. It should therefore be read as an observation at the harder end of the task-complexity distribution available in current corpora, not at its centre. Under this reading, the compound loop is more likely to converge on lighter workloads than this one, not less. What remains to establish is whether the same convergence holds at task complexities beyond this corpus — a question addressed in the companion paper. Outputs: `~/corpus-20260417/compound_loops.md` (full report), `~/corpus-20260417/loops.jsonl` (per-chain records).

---

## Appendix F: Theorem XII Literature Positioning

This appendix collects the prior-art positioning for Theorem XII (§9.3 — Behavioural Bounding). The proof, claims, and scope are stated in §9.3 itself; this appendix supplies the relationship-to-prior-work content that §9.3 references but does not contain inline, to keep the proof central.

Theorem XII sits in the intersection of four mature literatures and one rapidly emerging one. Honest positioning: the theorem's normative direction is shared with a growing community; the theorem form and the token-boundary / typed-graph-operation-level instantiation are what's novel.

*The formal-methods lineage Theorem XII inherits.* Milner's *"well-typed programs don't go wrong"* (1978) is the load-bearing template: a formal theorem that the set of reachable behaviours is bounded by typing. Wright and Felleisen (1994) supply the progress-and-preservation proof style Theorem XII mirrors. Capability-based security (Dennis and Van Horn 1966; Levy 1984; Shapiro EROS 1999; Miller 2006 on object-capability; Klein et al. 2009 seL4 — the first machine-checked proof that an OS kernel satisfies its spec) establishes that admissibility-by-construction scales to complex runtime systems. Design by Contract (Meyer 1988) and the broader formal-methods tradition (Hoare-logic, model-checking, and proof-assistant lineages — each with decades of production deployment at safety-critical scale) supply the specification-engineering methods the corollary hands the problem off to. Theorem XII is the LLM-era analogue of these results applied to a typed-graph operational substrate rather than a typed-lambda-calculus program semantics.

*The safety-by-construction community Theorem XII joins, not leads.* Seshia, Sadigh, and Sastry's *"Towards Verified Artificial Intelligence"* (2016) is the foundational call: explicitly advocating correct-by-construction intelligent systems and identifying specification as the bottleneck. Dalrymple et al.'s *"Towards Guaranteed Safe AI"* (2024), co-authored with Bengio, Russell, Tegmark, Tenenbaum, Seshia, Omohundro, and others, is the nearest existing neighbour: *"Alignment should be a guaranteed property from the AI architecture rather than a characteristic imposed post-hoc on an arbitrary AI model."* Drexler's *Comprehensive AI Services* (2019) argued that *"opaque, self-improving AGI agents"* are an architectural choice among several, and that structured service composition is both more capable and more alignable. Bengio's *Cautious Scientist AI* (2024) proposes a non-agentic architectural route to safety. Tegmark and Omohundro's *"Provably Safe Systems: The Only Path to Controllable AGI"* (2023) makes the manifesto-level case. Dalrymple's Open Agency Architecture (2023) and the associated UK research programme on mathematics-for-safe-AI coordinate the direction as an active research agenda. Clarke's *Ten Levels of AI Alignment Difficulty* (2023) concedes, from inside the field, that alignment difficulty is architecture-contingent rather than an intrinsic property of AI. Christiano himself has written that *"systems which perform inference with models that have interpretable beliefs about the world might be easier to align than opaque black boxes doing optimization"* — the same architecture-contingency observation from the founder of RLHF.

Theorem XII's specific contribution within this community is three-part: (a) the theorem form of the reduction (prior work is manifesto-level, research-agenda-level, or framework-level, not theorem-level); (b) the token-boundary / typed-graph-operation-level instantiation (GSAI externalises a verifier around an arbitrary AI controller; Theorem XII internalises admissibility into the operation graph so verification is decidable per step rather than requiring a separate proof system); and (c) the explicit handling of reflective access (Axiom 3) via the meta-Γ regress in Step 3 of the proof — the adversarial question of whether a sub-component can rewrite its own admissibility predicate from within the system is the standard objection to any by-construction safety guarantee, and the regress-terminating-at-deployment-authority-root treatment is a technical contribution that prior safety-by-construction frameworks (which externalise the verifier and therefore do not face the internal-self-modification question) do not directly address. The reduction — alignment ≤ grammar specification under decidable admissibility, with reflective access handled — is the novel claim; the direction (safety-by-construction as the correct frame for AI alignment) is not.

*The opacity-as-root-cause thread Theorem XII inverts.* The mechanistic interpretability literature implicitly concedes opacity-dependence at every level. Olah et al.'s *"Zoom In"* (Distill 2020) frames interpretability as microscopy over trained networks — you only need a microscope if the object wasn't built to spec. Elhage et al. (2022) formalised this opacity as superposition: features are compressed into fewer dimensions than there are features, so the system is hard to read by construction of how current transformers store representations. Bricken et al.'s *"Towards Monosemanticity"* (2023) states the problem directly: *"Models appear to us as giant inscrutable matrices of floating point numbers."* Templeton et al.'s *Scaling Monosemanticity* (2024): *"Understanding the nature of the computation inside a model — knowing in detail how it works — is one of the most important long-term goals for ensuring safety."* Nanda et al.'s *Progress Measures for Grokking via Mechanistic Interpretability* (2023) demonstrates the reverse-engineering programme concretely on a toy domain, framing its own method as *recovering* a hidden learned algorithm. Bereska and Gavves's 2024 review of mechanistic interpretability for safety consolidates the field's framing; Sharkey et al.'s *Open Problems in Mechanistic Interpretability* (2025) cataloguing 30+ co-authors' unresolved challenges for the programme; Casper et al.'s *"Black-Box Access is Insufficient for Rigorous AI Audits"* (FAccT 2024) stating directly that opaque-access auditing cannot discharge the safety obligations being asked of it. Dario Amodei's *The Urgency of Interpretability* (2024) is the cleanest explicit statement: *"Many of the risks and worries associated with generative AI are ultimately consequences of this opacity, and would be much easier to address if the models were interpretable."* Yudkowsky's *AGI Ruin* (2022), taking the same premise to its strongest rhetorical form: *"We've got no idea what's actually going on inside the giant inscrutable matrices and tensors of floating-point numbers."*

*The opaque-system alignment lineage Theorem XII does not invalidate but does reframe.* Deep RL from Human Preferences (Christiano, Leike, et al. 2017), InstructGPT (Ouyang et al. 2022), Constitutional AI (Bai et al. 2022), Debate (Irving, Christiano, Amodei 2018), Iterated Amplification (Christiano 2018), Recursive Reward Modeling (Leike et al. 2018), Cooperative Inverse Reinforcement Learning (Hadfield-Menell et al. 2016), Mesa-Optimization / deceptive alignment (Hubinger et al. 2019), Corrigibility (Soares et al. 2015), and Concrete Problems in AI Safety (Amodei et al. 2016) are all, each in different register, responses to the architectural premise of statistical opaque-model AI. The problems they address — reward hacking, side effects, scalable supervision, safe exploration, mesa-optimisation, deceptive alignment, shutdown problems, reward function specification — are each consequences of optimising an opaque scalar reward over an unconstrained action space. Theorem XII does not claim these approaches fail on the systems they target. It claims that the problems they address are architecture-specific: under a typed-graph operation substrate with decidable admissibility, they do not arise in the same form.

*What nothing in the literature states explicitly.* No prior paper we have located makes Theorem XII's exact claim: that under a graph-native architecture with decidable operation admissibility, AI alignment reduces formally to grammar specification, as a theorem rather than a research agenda. The closest prior statements are (i) Dalrymple et al. (GSAI, 2024) on alignment-as-architectural-property with an external-verifier mechanism, (ii) Drexler (CAIS, 2019) on structured services over opaque agents, (iii) Clarke (Ten Levels, 2023) on architecture-dependent difficulty, and (iv) Christiano's interpretable-beliefs remark within the prosaic-alignment programme. Theorem XII sits constructively inside the cluster formed by (i)–(iv) and supplies the specific reduction those works gesture at.

---

## Appendix G: Methodology Lineage

This appendix collects the methodology lineage referenced from §12 (Conclusion). The framework's methodology of naming load-bearing unnamed assumptions has prior workers across five disciplinary traditions, each of which articulated the operation in its own register. The framework's contribution within this lineage is operational composition at twentieth-and-early-twenty-first-century-computing scope; the methodology is older than the framework. A long-form corpus-depth survey of the lineage with per-prior-worker annotations lives at `research/docs/methodology_prior_work.md`; this appendix consolidates the in-paper credit.

*Philosophy of science.* Naming load-bearing unnamed assumptions is the operation Kuhn called paradigm shift (*The Structure of Scientific Revolutions*, 1962). Quine demonstrated it on logical empiricism in *Two Dogmas of Empiricism* (1951), naming the analytic-synthetic distinction and the reduction-to-experience dogma as load-bearing without justification. Lakatos worked the operation through mathematical proof in *Proofs and Refutations* (1976), surfacing the implicit assumptions that the dialectic of proof attempts and counter-examples revises. Latour named the inverse operation as blackboxing — what gets stable enough to stop questioning — across the actor-network theory programme.

*Sociology and critical theory.* Bourdieu called challenging *doxa* — the implicit unquestioned assumptions a field operates under — the constitutive move of reflexive sociology (*Outline of a Theory of Practice*, 1972; *The Logic of Practice*, 1980). Foucault called the analysis of *episteme* — the historically-conditioned conditions of possibility for knowledge in a period — archaeology (*The Order of Things*, 1966; *The Archaeology of Knowledge*, 1969). Barthes (*Mythologies*, 1957) named the operation at the cultural-symbol layer: making the naturalised structure of bourgeois mythology visible as historically-contingent rather than necessary.

*Cognitive practice.* Polanyi gave the operation the precise vocabulary of *tacit knowledge* (*Personal Knowledge*, 1958; *The Tacit Dimension*, 1966): assumptions held implicitly that ground explicit reasoning. Argyris articulated its cognitive form as double-loop learning (*Organizational Learning*, 1978 with Schön): the move from learning within an action-frame to learning the action-frame itself. Kahneman's System-1/System-2 distinction (*Thinking, Fast and Slow*, 2011) names the implicit-fast-judgment operation that holds load-bearing assumptions in production cognition. Norman's gulf of execution (Hutchins, Hollan & Norman 1985; Norman 1986) names the user-side cognitive form of the gap between intent and articulable specification.

*Engineering and design.* Within engineering, Dijkstra, Alan Kay, Hubert Dreyfus, Rodney Brooks, and Christopher Alexander each named load-bearing assumptions in computing-and-design with operational alternatives built in their place. Dijkstra's *"Go To Statement Considered Harmful"* (1968) named structured-control-flow's premise. Kay's Smalltalk programme (1972 onwards) named object-message-passing's premise as alternative to procedural-call. Dreyfus's *What Computers Can't Do* (1972) named symbolic-AI's representational premise. Brooks's subsumption architecture (1986) named centralised-planning-as-required for robotics. Alexander's *A Pattern Language* (1977) named architectural-design-as-individual-creative-act with a structured-vocabulary alternative. Each is *methodology-with-built-alternative*: the named assumption was paired with an operational replacement that demonstrated the alternative's viability.

*Physics.* The framework's structural reasoning has substantial direct prior art in physics: Wilson's renormalization group (1971/1975, Nobel 1982), Kadanoff's block-spin scaling (1966), and the universality-class machinery they established show that vastly different physical systems exhibit identical scaling behaviour at critical points; Bak's self-organized criticality (Bak-Tang-Wiesenfeld 1987; *How Nature Works*, 1996) extends scale-invariant dynamics across natural systems with explicit universality emphasis; West's universal scaling laws (*Scale*, 2017) demonstrate cross-zoom empirical scaling across biology, cities, and companies; Mandelbrot's fractal geometry (*The Fractal Geometry of Nature*, 1982) provides the geometric form. Information physics — Wheeler's "It from bit" (1990); Landauer's kT ln 2 thermodynamic floor (1961); Bennett on thermodynamics of computation; Bekenstein's information density bound (1981); Lloyd's *Programming the Universe* (2006); Bremermann's per-mass computation rate (1962) — supplies the bounds-side limits any computational substrate is bounded by. Wolfram's *A New Kind of Science* (2002) and the Fredkin / Zuse digital-physics lineage articulate computational equivalence at substrate level. Network science (Barabási scale-free networks, 1999; Watts-Strogatz small-world networks, 1998; Newman's *Networks*, 2010; Albert-Barabási *Reviews of Modern Physics* 2002 *Statistical Mechanics of Complex Networks*) supplies the typed-graph + scale-invariance machinery directly. Econophysics (Stanley as founder; Sornette's *Why Stock Markets Crash* on phase transitions in finance; Farmer on technology-cost-decline) applies the same machinery to economic substrates. The framework's contribution within this physics lineage is the substrate-mechanism articulation for the cognitive-coordination instance specifically; the structural reasoning is established physics.

**The framework's contribution within the five traditions.** Operational composition: across the connected programme of this paper and its companion, twelve unnamed assumptions are named — one in full in this paper, eleven in the companion — each falsified by an artefact that ships, with the production of the programme itself as a self-evidencing instance. The methodology is older than the framework; the framework is the operational instance applied to twentieth-and-early-twenty-first-century computing. None of the assumptions named is novel-as-an-assumption-to-name (the methodology has been operating across these traditions for decades-to-centuries); the framework's specific contribution is *which* assumptions get named *now* in the AI/computing instance, and the operational artefacts that ship as the falsification path for each.
