# Hints Replace Prompts and Skills

Prompts, skills, .cursorrules, copilot-instructions, CLAUDE.md — they all solve
the same problem: tell the model how to behave. They all solve it the same way:
dump a wall of text into context on every request. Hints solve it better.

## The problem with always-loaded guidance

A `.cursorrules` file for a Next.js/TypeScript project might contain 40 rules:
server component patterns, hook rules, Zod validation, Tailwind conventions,
error boundary patterns, image optimization, API route handlers, caching
strategy, testing conventions, accessibility requirements...

Every one of those rules is injected on every request. The user asks "fix the
typo in the README" and the model receives 2,000 tokens of React Server
Component guidance. The user asks "add a database index" and gets 500 tokens
about Tailwind responsive breakpoints. The user asks "explain this function"
and gets injected with "never use enums, use maps instead."

The same problem exists in every format:

| Format | Always loaded | Typical size |
|---|---|---|
| .cursorrules | Every Cursor request | 500–3,000 tokens |
| CLAUDE.md | Every Claude Code session | 300–1,500 tokens |
| copilot-instructions.md | Every Copilot request | 200–1,000 tokens |
| Skills | When invoked (user must remember) | 200–500 tokens per skill |
| Agent prompts | Every agent run | 1,000–6,000 tokens |

Multi-agent systems are worse. The ReviewPay project loads ~6,400 tokens of
shared + role-specific prompt on every agent run. A CSS fix gets the same
5,200-token shared context as a payment settlement rewrite. 85% of that
context is irrelevant to any given task.

## Hints: conditional injection

A hint is a YAML file with detect patterns (when to fire), guard patterns
(when not to fire), and guidance text (what to inject). It costs zero tokens
when irrelevant and injects only when the context demands it.

```yaml
name: nextjs-server-components
type: framework
detect:
  - '\buse client\b'
  - '\buseState\b.*\bcomponent\b'
guard:
  - '\b(?:explain|what is)\b'
guidance: |
  Prefer React Server Components. Only add 'use client' for
  useState, useEffect, event handlers, or browser APIs.
  Move 'use client' as far down the tree as possible.
```

This guidance appears when the user is working with client components in
Next.js. It doesn't appear when they're fixing a typo, adding a database
index, or writing a backend migration. Zero tokens wasted.

## Hints replace skills entirely

A skill is: "when the user invokes `/deploy`, load this instruction block."

A hint is: "when the message matches `deploy|release|ship|push to prod`,
inject this instruction block."

| | Skill | Hint |
|---|---|---|
| Token cost when irrelevant | Zero (not invoked) | Zero |
| Token cost when relevant | Same content | Same content |
| Requires user to know it exists | Yes | No |
| Requires user to remember the name | Yes | No |
| Requires user to type a command | Yes | No |
| Fires at the right time | When invoked (maybe wrong) | When detected (right time) |
| Infrastructure needed | Registry, routing, loader, parser | One regex matcher |

Skills have an additional failure mode: the user forgets to invoke them. The
whole value of the guidance is lost if the user doesn't remember that `/deploy`
exists. Hints fire automatically. The user doesn't need to know they exist.

Every skill is a hint with a detect pattern matching the same intent the slash
command was designed for. The content is identical. The delivery is automatic
instead of manual.

## Prompts collapse to one

A multi-agent system like ReviewPay has 13 agent prompts containing guidance
about migrations, API contracts, security, testing, financial transactions,
code style, merge conflicts, and deployment. The same policy appears in
multiple prompts with different framing:

- Developer prompt: "write migrations this way"
- Code review prompt: "check migrations were done this way"
- Architecture prompt: "flag if migrations aren't done this way"

Three prompts, one policy. As a hint, it's one YAML file that fires for all
agents when the context involves migrations.

The single prompt becomes:

```
You are working on [project]. [One sentence about what it does.]
[3-5 line package/module map.]
[Communication/output format rules if applicable.]
```

~50 tokens. Everything else — every convention, checklist, workflow, guardrail,
framework pattern — is a hint in `.hints/`.

## Quantified impact

Measured against ReviewPay's multi-agent system (13 agents, 4 shared docs):

| Metric | Prompts | Hints | Savings |
|---|---|---|---|
| Tokens per agent run | ~6,400 | ~1,000 base + 0–500 conditional | 70–85% |
| CSS fix task | 6,400 tokens loaded | ~1,100 | 83% |
| Migration task | 6,400 tokens loaded | ~1,200 | 81% |
| Financial code review | 6,400 tokens loaded | ~1,300 | 80% |
| Guidance duplication | 3× for migrations, 2× for APIs | 1× each | Zero duplication |
| Policy change effort | Update 3 files | Update 1 YAML | 67% less maintenance |

## Comparison table

| Dimension | Prompt | Skill | Hint |
|---|---|---|---|
| Token cost (irrelevant) | Full cost | Zero | Zero |
| Token cost (relevant) | Full cost | Full cost | Same content |
| User knowledge required | None | Must know name | None |
| User action required | None | Must invoke | None |
| Fires at the right time | Always (wasteful) | When invoked (unreliable) | When detected (precise) |
| Deduplication | Manual | Manual | Automatic |
| Maintenance | Edit N files | Edit skill + registry | Edit 1 YAML |
| Community portable | No (format varies) | No (tool-specific) | Yes (universal YAML) |
| Composable | Concatenation | One at a time | Multiple, budget-sorted |
| Testable | No | No | Yes (regex tests) |
| User-extensible | Edit config file | Write + register skill | Drop YAML in .hints/ |

## Migration path

Any existing guidance format converts to hints:

1. **Decompose** the always-loaded content into individual guidance atoms
2. **Write detect patterns** for when each atom is relevant
3. **Write guard patterns** for when each atom should be suppressed
4. **Drop YAML files** in `.hints/`
5. **Delete** the original prompt/skill/rules content

The `writing-hints` hint ships with ozma — when a model is asked to create
a hint, it automatically receives the YAML template and writing guidelines.

See `ozma hints convert` for automated conversion of .cursorrules,
CLAUDE.md, and copilot-instructions to hint YAML files.

## What stays as a prompt

A minimal prompt with identity, scope, and communication protocol:

- Project name and one-line description
- Package/module map (what lives where)
- Output format constraints (if any)
- Communication protocol (if multi-agent)

This is ~50–100 tokens. Everything else is hints.
