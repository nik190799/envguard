# Framework changelog

Append-only log of every change to this project's local framework.
Newest entries at the top. Each entry is the output of a retrospective
per [RETROSPECTIVE_PROTOCOL.md](./RETROSPECTIVE_PROTOCOL.md).

This file IS the history of *your* framework. Reading it top-to-bottom
tells you why every local rule exists and what triggered it.

## Open retrospective items

(populated as retrospectives defer fixes — keep this section pruned)

---

## 2026-10-03 — Framework initialized via Cadence v0.3.0-rc.2

### Added
- Cadence framework scaffolded via `/cadence-init`
- Layer 1: `docs/PATTERNS.md`, `docs/ADR/0000-template.md`, seed ADRs
  (review and prune for your project)
- Layer 2: `docs/DEFINITION_OF_DONE.md`, `docs/ROLE_SPECS.md`,
  `docs/TEAM_PROTOCOL.md`, `docs/RETROSPECTIVE_PROTOCOL.md`
- Layer 3: `.cadence/cadence.yaml` (boundaries + commands),
  `tool/check_boundaries.py`, `scripts/verify.{sh,ps1}`,
  `.github/workflows/cadence.yml`
- Layer 4: `docs/TEAM_LAUNCH_TEMPLATE.md`
- `CLAUDE.md` patched with pointers to the above

### Next steps
- Read `docs/PATTERNS.md` and adjust §1 layer rules to match your
  project's actual directory structure
- Edit `.cadence/cadence.yaml` to confirm the `commands:` reflect your
  stack's tooling
- Write the first 2-3 ADRs for the judgment calls your project has
  already made (use `/cadence-adr <title>`)
- Run `/cadence-verify` to baseline what your current code looks like
  against the boundary rules
