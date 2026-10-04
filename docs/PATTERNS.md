# Patterns — canonical reference

This doc is the **single source of truth** for how code is structured
in this project. Every teammate (human or agent) reads this before
writing code. The Reviewer cites violations against this doc by section
number.

Patterns here are derived from the [ADR records](./ADR/). When in
doubt, read the ADR — it explains *why* the pattern is what it is.

## §1 — Layer rules (mechanically enforced)

```
src/envguard/
  core/   # Pure logic: parser, models, checks, diff. No file or terminal I/O.
  io/     # Reading .env files from disk; builds on core.
  cli/    # argparse entry point and output formatting; wires core and io.
```

**Enforced by `tool/check_boundaries.py` (reads `.cadence/cadence.yaml`):**

- `core/**` may NOT import `io/**` or `cli/**`
- `io/**` may import `core/**` only, never `cli/**`
- `cli/**` may import `core/**` and `io/**`

Violations fail the verify gate (`scripts/verify.sh`).

## §2 — Controller shape

| Screen kind | Shape | Test required |
|---|---|---|
| Stateful + async load + mutations | `AsyncNotifier` / `useAsync` / equivalent | Yes |
| Stateful + sync only | `Notifier` / `useState` / equivalent | Yes |
| Read-only + one-shot async load | `FutureProvider` / single hook | Only if logic |
| Pure static UI | Plain component reading cross-cutting providers if any | No |

## §3 — Narrow function-typed providers

Feature code never reads repositories directly. It reads **narrow
function-typed providers** that close over the exact method the feature
needs.

```python
// In app/providers.py
type CountReader = () => Promise<number>;
const countReaderProvider = Provider<CountReader>(
  (ref) => () => ref.read(countRepositoryProvider).current(),
);
```

Tests override the closure, not the whole repository:

```python
ProviderContainer(overrides: [
  countReaderProvider.overrideWithValue(async () => 7),
]);
```

Adapt the syntax above to your stack. The shape is universal.

## §4 — Mutation pattern (optimistic-then-write)

Per ADR-0003 (mutation pattern), **every mutation in a controller
follows optimistic-then-write**:

```python
async function increment() {
  const next = (state.valueOrNull ?? 0) + 1;
  state = AsyncData(next);                    // 1. optimistic UI update
  try {
    await writerProvider.read()(next);         // 2. persist
  } catch (e, st) {
    state = AsyncError(e, st);                 // 3. surface error
  }
}
```

For list mutations, compute the optimistic value locally (don't
re-read). The source is the authority on persistence, not on UI state.

**The Reviewer rejects write-then-re-read** unless the controller has a
documented reason (server-assigned IDs, etc.).

## §5 — Error surface

Per ADR-0004 (error surface):

- **Async mutations** wrap the persist call in try/catch and set
  `state = AsyncError(e, st)` on failure. No silent swallowing.
- **Per-field errors** (one row's update failed, others didn't) use a
  state class with a `fieldErrors: Map<String, Object?>` field.
- **Logging**: use the project's structured logger; never bare `print`
  / `console.log`. PII passes through redaction helpers.

## §6 — UI async invocation

Per ADR-0005 (UI async), all fire-and-forget async calls from UI
handlers explicitly mark the discard:

```python
// ✅ canonical
onPress: () => unawaited(controller.delete(id))

// ❌ implicit discard — Reviewer rejects
onPress: () => controller.delete(id)
```

The wrapper makes "I am intentionally not awaiting this" visible.

## §7 — Test requirements

Per ADR-0006 (test coverage). For every controller, the test file at
`tests/features/<name>/<name>_controller_test.py` contains:

1. **One happy-path test per public method**
2. **At least one edge case** per public method (where applicable)
3. **One sequence test** that exercises 3+ mutations in order
4. **One error test** if the method has an error path
5. **Write log assertion** in every mutation test — state alone is not
   enough; assert what was sent to the writer

## §8 — Style

The project's lint configuration in `.cadence/cadence.yaml`
`commands.lint` enforces:

- Single-quote convention (or whatever your stack prefers)
- Const / immutability preferences
- No inline color literals in feature files (centralize in theme)
- Trailing commas in multi-line constructors
- Project's preferred async-await patterns

## §9 — How patterns evolve

This doc is **not frozen**. When a team run uncovers a judgment call
not covered here, the retrospective process
([RETROSPECTIVE_PROTOCOL.md](./RETROSPECTIVE_PROTOCOL.md)) either:

- Adds a section here (with a new ADR backing it), or
- Tightens an existing section to remove ambiguity, or
- Adds an automated check that makes the rule mechanical instead.

Every change lands together with an entry in
[FRAMEWORK_CHANGELOG.md](./FRAMEWORK_CHANGELOG.md).

In factory mode the learning loop also proposes lessons, as one retro PR
that a human merges. It writes only the section below (never the sections
above), and its changelog is `.cadence/lessons.yaml` plus the PR body.
Keep the section last in this file, and do not edit it by hand: the next
retro PR rewrites it.

## Learned patterns (factory)

Generated from factory runs by `tool/ladder.py`; change it through the retro PR. See docs/LEARNING.md.
