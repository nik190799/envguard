# Architecture Decision Records

Each ADR is one short page that captures a single decision in the
[Nygard format](https://www.cognitect.com/blog/2011/11/15/documenting-architecture-decisions).

## Format

```
# ADR-NNNN — <title>

## Status
Accepted | Superseded by ADR-NNNN | Deprecated | Proposed

## Context
What's the situation that forced this decision?

## Decision
What did we decide? One sentence, then specifics.

## Consequences
What does this make easier? What does it make harder?

## Trigger
What prompted writing this ADR? (Usually a retrospective entry —
link to FRAMEWORK_CHANGELOG.md.)
```

Numbering is monotonic. Never reuse a number, even if an ADR is
superseded — mark it `Superseded by ADR-NNNN` and keep it for history.

## When to write one

- A decision affects more than one file's structure
- A reasonable competing alternative exists
- A future maintainer would ask "why did we do this?"
- A retrospective identified a missing pattern

## When NOT to write one

- Pure formatting / naming convention (put in `PATTERNS.md` directly)
- Decision contained in one function (use a code comment)
- Implementation detail with no architectural weight

## Index

(Maintained by `/cadence-adr`. Edit alphabetically by number.)

- [ADR-0000 — Template](./0000-template.md)
- *(your project's ADRs go here as you write them)*

## Suggested first ADRs

Most projects benefit from these decisions being explicit:

| # | Topic |
|---|---|
| 0001 | Layer boundaries (what folder-import rules apply) |
| 0002 | Model location (where do plain value types live?) |
| 0003 | Mutation pattern (optimistic-then-write vs write-then-reread) |
| 0004 | Error surface (whole-screen vs per-field vs per-row) |
| 0005 | UI async invocation (how to handle fire-and-forget) |
| 0006 | Test coverage (what every controller test must include) |

Cadence's own [Flutter case-study sandbox](https://github.com/nik190799/agent_teams_sandbox)
has these ADRs written for Riverpod + Flutter, as examples of the form.
