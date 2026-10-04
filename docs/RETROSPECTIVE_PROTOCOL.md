# Retrospective protocol — the self-improvement engine

Every team run ends with a retrospective. **This is not optional**, and
not a free-form "how did it go?" chat. It's a structured pass that
mechanically maps observed issues to specific framework artifacts.

## When

- After the Reviewer signs off on Definition of Done
- Before the lead runs `Clean up the team`

## Who

- The **Reviewer** drafts the retrospective findings (they have the
  best signal on what slipped through)
- The **Lead** (the human) approves or defers each proposed change
- The **other teammates** are silent observers — they can object via
  message if a finding is wrong, but they don't author the report

## The mechanical pass

For each issue the Reviewer caught during the run, fill out this row:

| Field | Example |
|---|---|
| What happened | "Feature engineer used write-then-reread in todos_controller" |
| Should it have been auto-caught? | "No — judgment call" |
| Was there a rule for it? | "No — pattern wasn't documented" |
| Proposed fix | "Write ADR-0003 + add §4 to PATTERNS.md" |
| Layer of fix | "Layer 1 (Patterns)" |

Then for each *near-miss* — something that turned out fine but exposed
a gap — fill out the same row.

## The four valid fix layers

Every proposed fix lands in exactly one of these layers. If you can't
place it, the fix isn't concrete enough yet.

| Layer | When to use | Concrete action |
|---|---|---|
| **3 (Automated)** | Issue is mechanically detectable | Add a lint to project's analyzer config, or a rule to `tool/check_boundaries.py`, or a new check script invoked by `scripts/verify` |
| **1 (Patterns)** | Issue is a judgment call with a defensible right answer | Write an ADR; update PATTERNS.md to cite it |
| **2 (Process)** | Issue is about coordination, not code | Update DoD, ROLE_SPECS, or TEAM_PROTOCOL |
| **4 (Launch)** | Issue stems from ambiguity in the launch prompt itself | Update TEAM_LAUNCH_TEMPLATE.md |

**Strong preference for higher numbers descending into lower:** if
something can be automated (3), automate it. Falling back to (1) is
fine; falling back to (2) is OK; (4) is the last resort because launch
prompts are per-run and can drift.

## The output

The Reviewer's retrospective output is a markdown block appended to
[FRAMEWORK_CHANGELOG.md](./FRAMEWORK_CHANGELOG.md):

```markdown
## YYYY-MM-DD — <feature> retrospective

### Issues observed

- **<issue>**: <one-line description>
  - Auto-catchable? <yes/no — and what would catch it>
  - Existing rule? <yes/no — link if yes>
  - Fix: <layer + concrete action>

### Near-misses / gaps

- **<gap>**: <description>
  - Fix: <layer + action>

### What worked

- <thing to amplify in future runs>

### Lead's decisions

- [x] Approved: <fix> — landed in <commit/PR>
- [ ] Deferred: <fix> — reason: <...>
- [ ] Rejected: <fix> — reason: <...>
```

## The lead's decisions

For each proposed fix, the lead chooses:

- **Approve & land immediately**: the fix is implemented in this same
  PR, before cleanup. Best for small, obvious fixes.
- **Approve & defer**: the fix is recorded but lands in a separate PR.
  Best for fixes that need their own review.
- **Reject**: the fix isn't worth the cost (rare). Record the reason so
  future retrospectives don't re-propose it.

Deferred fixes go into a top-of-file `## Open retrospective items`
section in FRAMEWORK_CHANGELOG.md. They get re-evaluated at the next
retrospective.

## Anti-patterns

- ❌ "Everything went great" — false. Find at least one improvement.
- ❌ Proposing a Layer 1 (doc) fix when Layer 3 (automation) would work
- ❌ Rejecting a fix without recording the reason
- ❌ Lead implementing fixes silently — every framework change appears
  in FRAMEWORK_CHANGELOG.md with a triggering retrospective

## Why this matters

The framework only improves at the rate of the retrospective. Skip
retrospectives → framework stagnates → same issues recur every run.
The retrospective is the single most important step of the team run.

## Upstreaming a finding to the community

If a recurring issue would benefit other Cadence users (not just this
project), open a [Framework Finding](https://github.com/nik190799/cadence/issues/new?template=framework_finding.md)
on the canonical Cadence repo. Maintainers triage and may ship the fix
in a future plugin version that all users pick up via `/cadence-init
--upgrade`.
