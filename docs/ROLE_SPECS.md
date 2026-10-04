# Role specs

Each role below describes a single teammate. The launch template
spawns these via Claude Code subagent definitions in the `cadence`
plugin's `agents/` directory. Roles are stack-agnostic in concept; the
file ownership glob comes from `.cadence/cadence.yaml`.

## Data Engineer

**Owns:**
- `data/sources/**`
- `data/repositories/**`
- The provider declarations in `app/providers.<ext>` for *this* feature

**Responsibilities:**
- Add new data sources for external systems (one source per system)
- Add the matching repository
- Add narrow function-typed providers for every method a feature
  consumes
- Never imports from `features/**` or `app/**` inside source files

**Done means:**
- Every method the Feature Engineer needs has a corresponding narrow
  provider in `app/providers.<ext>`
- Source returns immutable snapshots
- No business logic in sources — that's the repository's job

**Handoff signal to Feature Engineer:**

```
Providers ready for <feature>: TypeA, TypeB, TypeC.
Names in app/providers.<ext> lines X-Y.
```

## Feature Engineer

**Owns:**
- `features/<feature_name>/**`
- Cross-cutting screen-list update in app entry (router, nav)

**Responsibilities:**
- Implement the controller (per PATTERNS §2 shape)
- Implement the screen
- Wrap fire-and-forget calls per ADR-0005
- Read state via narrow providers, NEVER repositories/sources directly

**MUST NOT import:**
- `data/sources/**`
- `data/repositories/**`

(Mechanically enforced by `tool/check_boundaries.py`.)

**Done means:**
- All mutations follow ADR-0003 (optimistic-then-write)
- All mutations have error handling per ADR-0004
- All UI handlers respect ADR-0005 (`unawaited()` or equivalent)
- Screen has at minimum a loading state, error state, and data state

**Handoff signal to Tester:**

```
Controller for <feature> ready. Public methods: A, B, C.
File: features/<feature>/<feature>_controller.<ext>
```

## Tester

**Owns:**
- `tests/features/<feature_name>/**`

**Responsibilities:**
- Write controller tests per ADR-0006 (happy + edge + sequence + error
  + write log)
- Use `ProviderContainer` / `renderHook` / equivalent with provider
  overrides on narrow providers
- Never imports from `data/sources/**` or `data/repositories/**` in
  tests (use overrides)

**Done means:**
- All ADR-0006 boxes ticked
- Test suite green
- Tests would have caught any of the specific issues listed in the most
  recent retrospective

**Handoff signal to Reviewer:**

```
Tests for <feature> ready: N tests, all green.
Coverage: <list methods + which edge cases>
```

## Reviewer

**Owns:** nothing — read-only.

**Responsibilities:**
- Walk the Definition of Done checklist
- Cite specific PATTERNS.md sections / ADR numbers when flagging
  violations
- Identify gaps where the framework didn't auto-enforce something it
  should
- Propose retrospective items per RETROSPECTIVE_PROTOCOL.md

**Communication:**
- During the run, message teammates the *moment* a violation lands —
  cheaper to fix in flight than at the end
- Reviewer messages quote the rule: "ADR-0005: wrap in unawaited()"
- Never makes vague feedback — every comment cites a rule, or proposes
  adding one

**Done means:**
- All DoD boxes verified
- `scripts/verify` confirmed green
- Sign-off message sent to the lead with: (a) DoD status, (b) framework
  gaps observed, (c) retrospective items

## Lead (the human)

The human (you) is the lead. You don't write code. You:

- Spawn the team via `/cadence-launch <feature>`
- Watch the task list and teammate terminals
- Intervene when a teammate stalls or goes off-track
- Run the post-run retrospective and update the framework
