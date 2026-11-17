# Ocelot prompt and graph preload probe — 2026-11-17

This offline feasibility probe asks whether the local Ocelot cognitive engine
can consume an OzAGIa repository graph before an LLM turn and select useful
context. It makes no model call and does not measure answer quality or speedup.

## Setup

- Prompt: “Where does Ozagia classify and refuse Git ref-moving commands?”
- A fresh OzAGIa snapshot of this checkout was built separately under `/tmp`;
  freshness was verified before retrieval. Building the graph and retrieving
  five candidates took 7.77 seconds in this one cold observation.
- OzAGIa code search selected five candidate files. A bounded projection of
  those files' indexed File, Symbol and Test nodes and their existing graph
  edges was mirrored into an Ocelot `TypedGraph`. Ocelot's `WorldAdapter`
  ingested that graph as world instances, then received the prompt through
  `converse(..., speech_act="question")`. Node IDs, names, paths, types and
  existing typed edges were transferred; full file text was not supplied.
- Controls used a newly constructed adapter with the same prompt and no graph;
  no LLM or OzAGIa prompt hook was invoked. The projection was selected by
  OzAGIa code search, not by Ocelot.

## Observations

| Condition | Graph input | Ocelot response |
| --- | --- | --- |
| Baseline | None | `ERROR: no communicative response could be generated.` |
| Graph, first slice | 60 nodes, 121 edges | Same explicit error |
| Graph, larger slice | 180 nodes, 400 edges | Same explicit error |

Ocelot ingested all 180 projected nodes as world instances. In this run boot,
ingest and conversation for the larger slice were about 0.025, 0.008 and
0.173 seconds respectively; these are single observations after an earlier run,
not latency or performance benchmarks. Each condition used a new process, and
the initial baseline had a colder system cache, so they are not timing-comparable.

As a separate control, OzAGIa's existing `retrieve_proactive_context` on the
fresh snapshot returned 2,008 characters over eight cited files in 1.15 seconds.
It identified `hookcore.py` and `shell.py`, including `git_ref_moving`,
`git_refusal_reason` and `classify`. This is OzAGIa retrieval, not Ocelot
reasoning, and the returned context was not sent to an LLM.

## Conclusion and limits

Graph transport into Ocelot works in this narrow projection. Ocelot did not
generate a useful response or select a context packet for this question, so the
probe does not demonstrate Ocelot-driven prompt preloading. No bridge currently
connects this projected graph to a graph-native code-retrieval goal or emits
permission-checked selected nodes back into the OpenCode prompt hook. The
question, projection, seed profile and single trial are insufficient to judge
what Ocelot could do with a purpose-built graph vocabulary and rules.

The next experiment should define the graph contract for prompt, repository
nodes, relations, selection and provenance; provide Ocelot with rules that
select relevant nodes; and compare selected, permission-checked context against
OzAGIa retrieval and a no-context control on multiple held-out questions. A
separate, explicitly approved live-model experiment is needed to assess answer
quality or wall-clock effects.
