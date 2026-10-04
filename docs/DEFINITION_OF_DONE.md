# Definition of Done

A feature, refactor, or bug fix is **done** when every box below is
ticked. The Reviewer runs this checklist before signing off.

Lines marked **(auto)** are enforced by `scripts/verify`; lines marked
**(manual)** require the Reviewer's eye.

## Mechanical (auto-enforced)

- [ ] **(auto)** Format check passes (`.cadence/cadence.yaml`
      `commands.format`)
      <!-- iso25010: [maintainability] -->
- [ ] **(auto)** Lint passes (`.cadence/cadence.yaml` `commands.lint`)
      <!-- iso25010: [maintainability, reliability] -->
- [ ] **(auto)** Boundary check passes (`tool/check_boundaries.py`)
      <!-- iso25010: [maintainability] -->
      <!-- ssdf: [PW.7] -->
- [ ] **(auto)** Tests pass (`.cadence/cadence.yaml` `commands.test`)
      <!-- iso25010: [functional-suitability, reliability] -->
      <!-- ssdf: [PW.8] -->
- [ ] **(auto)** `scripts/verify` exits zero

If any (auto) line fails, the work is **not done**, full stop.

## Architecture (manual — Reviewer cites the rule)

- [ ] Files placed per [PATTERNS.md §1](./PATTERNS.md) layer rules
      <!-- iso25010: [maintainability] -->
- [ ] Controller uses the right shape per [PATTERNS.md §2](./PATTERNS.md) table
      <!-- iso25010: [maintainability] -->
- [ ] No direct repository or source imports from feature folders
      <!-- iso25010: [maintainability] -->
- [ ] Every new service method has a narrow function-typed provider per
      [PATTERNS.md §3](./PATTERNS.md)
      <!-- iso25010: [maintainability, reliability] -->

## Patterns (manual — Reviewer cites the ADR)

- [ ] Mutations follow optimistic-then-write per
      [ADR-0003](./ADR/0003-mutation-pattern.md) (or document the exception)
      <!-- iso25010: [reliability, performance-efficiency] -->
- [ ] Models live under `domain/models/` per
      [ADR-0002](./ADR/0002-model-location.md)
- [ ] Every mutation has an error path per
      [ADR-0004](./ADR/0004-error-surface.md)
      <!-- iso25010: [reliability] -->
- [ ] UI fire-and-forget calls wrapped per
      [ADR-0005](./ADR/0005-ui-async.md)
      <!-- iso25010: [reliability] -->

## Tests (manual — Reviewer counts)

Per [ADR-0006](./ADR/0006-test-coverage.md):

- [ ] One happy-path test per public controller method
      <!-- iso25010: [functional-suitability] -->
- [ ] At least one edge case per public method (where applicable)
- [ ] One sequence test (≥3 mutations in order)
- [ ] One error test if the method has an error path
- [ ] Every mutation test asserts both state AND the write log

## Documentation

- [ ] If a new judgment call was made not covered by an existing ADR,
      a new ADR is written and `PATTERNS.md` updated
- [ ] If a new pattern emerged, `PATTERNS.md` references the canonical
      example
- [ ] If a new lint or boundary rule was added, `.cadence/cadence.yaml`
      or `analysis_options.yaml` updated
- [ ] [FRAMEWORK_CHANGELOG.md](./FRAMEWORK_CHANGELOG.md) has a new
      entry for any of the above

## Retrospective (manual — the self-improvement loop)

Per [RETROSPECTIVE_PROTOCOL.md](./RETROSPECTIVE_PROTOCOL.md):

- [ ] If the Reviewer caught any issue that *should* have been
      auto-enforced, a follow-up item is filed (or fixed in this PR) to
      add the missing check
- [ ] If a teammate had to make a judgment call not covered by ADRs,
      it's surfaced for the retrospective

## Sign-off

Reviewer's final message to the team lead:

```
Reviewer sign-off: all DoD boxes ticked.
Framework gaps: <list, or "none">
Recommended retrospective items: <list, or "none">
```
