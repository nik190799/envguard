# Team protocol

How teammates coordinate during a Cadence team run. The protocol is
**stack-agnostic** — it's the same whether the project is TypeScript,
Python, Go, Rust, Dart, or anything else.

## Ownership boundary

Each teammate gets a file-ownership zone in
[ROLE_SPECS.md](./ROLE_SPECS.md). Two rules:

1. **Don't write in another teammate's zone.** If you need a change
   there, message them.
2. **Don't read another teammate's in-flight work for production code.**
   Read only files they've signaled as "ready". The Reviewer is exempt —
   they can read anything, anytime.

## Handoff signals (stable phrases)

Use these exact phrases in direct messages so teammates can
pattern-match. They are stable across runs so retrospective analysis
can grep for them.

| From → To | Phrase | When |
|---|---|---|
| Data Eng → Feature Eng | `Providers ready for <feature>:` | After provider typedefs land |
| Feature Eng → Tester | `Controller for <feature> ready.` | After controller exports |
| Tester → Reviewer | `Tests for <feature> ready:` | After test suite green |
| Reviewer → Lead | `Reviewer sign-off:` | After DoD verified |
| Anyone → Lead | `Blocked on:` | When stalled > 5 minutes |

## Task list discipline

- The lead populates the task list during spawn from the launch prompt
- Each teammate claims tasks by owner (their name in the `owner` field)
- A task is marked complete only when the artifact for that task is on
  disk
- **No teammate marks another teammate's task complete.** If you finish
  work that unblocks someone else's task, message them — don't modify
  their tasks

## Conflict resolution

When two teammates need to edit the same file:

1. **Single-owner rule:** The file's owner from ROLE_SPECS.md does the
   edit. The other teammate sends a patch as a message.
2. **Disagreement:** Escalate to the lead. Don't argue in messages.
3. **Reviewer override:** If the Reviewer flags a violation, the
   responsible teammate fixes it immediately. No debate during the run
   — if the rule itself is wrong, raise it in the retrospective.

## Cadence

- Status check every ~5 minutes of wall-clock by the lead
- Teammates self-report blockers via the `Blocked on:` phrase
- Reviewer audits in flight — does NOT wait until the end
- Final retrospective is mandatory before team cleanup

## Anti-patterns

- ❌ Lead writing code directly during the run (you're the manager; let
  the team work)
- ❌ Teammates spawning their own subagents (no nested teams)
- ❌ Reviewer doing the work for the violator ("I just fixed it" — no,
  message and let them do it; otherwise the team doesn't learn)
- ❌ Skipping the retrospective because everything went well — the
  retrospective also captures *what worked* so future runs can amplify
  it
