# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-03

### Added

- `envguard check`: compares an env file against an example file and reports missing keys,
  extra keys (warning), duplicate keys, malformed lines, and empty values for keys marked
  `# required`.
- `envguard diff`: lists keys added, removed, or changed between two env files.
- Parser support for comments, blank lines, `export KEY=...`, single- and double-quoted values,
  and inline `# comment`s after unquoted values.
- Values are never printed by any command.
