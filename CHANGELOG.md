# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- `envguard diff` now writes one warning per malformed line to stderr, naming only the file
  and line number, instead of dropping the line silently.
- `envguard check` reports a `missing` key at the line where it is defined in the example file
  (for example `.env.example:3`) instead of just the env file path.

## [0.1.0] - 2026-10-03

### Added

- `envguard check`: compares an env file against an example file and reports missing keys,
  extra keys (warning), duplicate keys, malformed lines, and empty values for keys marked
  `# required`.
- `envguard diff`: lists keys added, removed, or changed between two env files.
- Parser support for comments, blank lines, `export KEY=...`, single- and double-quoted values,
  and inline `# comment`s after unquoted values.
- Values are never printed by any command.
