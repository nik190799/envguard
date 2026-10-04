# Claude Code instructions — envguard

<!-- Cadence framework section appended by /cadence-init.
     Edit your project-specific instructions ABOVE this section. -->

## Cadence framework

This repository uses the [Cadence framework](https://github.com/nik190799/cadence).
Before any non-trivial work:

- Read [`docs/PATTERNS.md`](./docs/PATTERNS.md) for codebase conventions
- Read [`docs/DEFINITION_OF_DONE.md`](./docs/DEFINITION_OF_DONE.md) for
  what "done" means here
- Read [`docs/ROLE_SPECS.md`](./docs/ROLE_SPECS.md) if spawning a team

Before declaring work done:

- Run `scripts/verify.sh` (or `scripts/verify.ps1` on Windows)
- Walk the DoD checklist

For new judgment calls:

- Follow [`docs/ADR/0000-template.md`](./docs/ADR/0000-template.md), or
  invoke `/cadence-adr <title>`
- Add an entry to [`docs/FRAMEWORK_CHANGELOG.md`](./docs/FRAMEWORK_CHANGELOG.md)

For retrospectives after team runs:

- Invoke `/cadence-retro` (or follow
  [`docs/RETROSPECTIVE_PROTOCOL.md`](./docs/RETROSPECTIVE_PROTOCOL.md))
- Map each finding to one of the four fix layers

For compliance reports:

- `/cadence-compliance --standard ssdf` for NIST SSDF v1.1 mapping
- `/cadence-compliance --standard iso25010` for ISO/IEC 25010:2023
  characteristic coverage
