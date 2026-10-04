# Team launch template

Fill in the `{{...}}` placeholders and paste into the Claude Code
session, OR run `/cadence-launch <feature>` to do it interactively.

## Standard launch prompt

```
Before doing anything else, read these in order and confirm:
1. CLAUDE.md
2. docs/PATTERNS.md
3. docs/DEFINITION_OF_DONE.md
4. docs/ROLE_SPECS.md
5. docs/TEAM_PROTOCOL.md
6. The ADRs in docs/ADR/ that apply to this work
7. The reference implementation at <project's reference feature>

Then create an agent team to execute the feature below. Spawn the
teammates listed; their roles and ownership zones are defined in
docs/ROLE_SPECS.md.

────────────────────────────────────────────────────────────────────
FEATURE: {{one-line description}}

REQUIREMENTS:
{{bulleted list of user-facing behavior}}

ACCEPTANCE CRITERIA (in addition to the standard DoD):
{{any feature-specific criteria, or "none beyond DoD"}}

OUT OF SCOPE:
{{explicit list of things NOT to build, or "none"}}
────────────────────────────────────────────────────────────────────

Teammates to spawn:
1. "cadence-data-engineer" — per docs/ROLE_SPECS.md
2. "cadence-feature-engineer" — per docs/ROLE_SPECS.md
3. "cadence-tester" — per docs/ROLE_SPECS.md
4. "cadence-reviewer" — per docs/ROLE_SPECS.md

Coordination:
- Data Engineer goes first; Feature Engineer + Tester unblock when
  providers land.
- Reviewer audits in flight per docs/TEAM_PROTOCOL.md.
- Handoffs use the phrases in docs/TEAM_PROTOCOL.md.
- Done is defined by docs/DEFINITION_OF_DONE.md — every box must tick.

Before declaring the team done, the Reviewer runs the retrospective
per docs/RETROSPECTIVE_PROTOCOL.md and proposes any framework changes
that came up. I (the lead) approve or defer each one before cleanup.
```

## Filling in placeholders

| Placeholder | Example | Notes |
|---|---|---|
| `{{one-line description}}` | "Favorites tab — mark/unmark todos as favorites" | One sentence. The Reviewer holds you to it. |
| `{{requirements}}` | "Users can star a todo from the todos screen / Favorites tab lists starred todos / Unstar removes from favorites" | User-facing behavior only. No implementation details. |
| `{{acceptance criteria}}` | "Favorite state survives app restart" | Anything beyond DoD that's part of "done". |
| `{{out of scope}}` | "No sorting, no search, no syncing across devices" | Prevents scope creep. |

## Smaller-team variants

If the work doesn't need all four roles:

- **3-person team (drop Reviewer):** Lead does the review at the end as
  a single session. Use when speed matters more than parallel review.
- **2-person team (Data + Feature):** Single-screen migration with
  simple data layer. Tester role merged into Feature Engineer.
- **1-person (single session):** Anything under ~30 min of work — team
  overhead exceeds benefit.

The 4-person team is the default. Downgrade only with explicit
justification — note it in the retrospective.
