# envguard

A small CLI that checks `.env` files against `.env.example` and compares env files, without
ever printing a value. Safe to run in CI logs.

## Install

```sh
pip install .
```

Requires Python 3.10 or newer. No runtime dependencies.

## Usage

### `envguard check`

```sh
envguard check                                  # .env against .env.example
envguard check --env .env.prod --example .env.example
```

Reports:

| Kind             | Severity | Meaning                                                  |
| ---------------- | -------- | -------------------------------------------------------- |
| `missing`        | error    | Key is in the example file but not in the env file       |
| `extra`          | warning  | Key is in the env file but not in the example file       |
| `duplicate`      | error    | Key is assigned more than once in the same file          |
| `malformed`      | error    | Line cannot be parsed                                    |
| `empty-required` | error    | Key is marked required in the example but empty in env   |

Mark a key as required with an inline comment in the example file:

```sh
# .env.example
DATABASE_URL= # required
LOG_LEVEL=info
```

Example output:

```text
.env.example:1: error: DATABASE_URL: missing from env file [missing]
.env:4: warning: OLD_FLAG: not listed in example file [extra]
1 error(s), 1 warning(s)
```

### `envguard diff`

```sh
envguard diff .env.staging .env.prod
```

```text
+ NEW_KEY added
- OLD_KEY removed
~ API_URL changed
```

Only key names are shown. A changed value is reported as `changed`, never printed.

A line in either file that cannot be parsed is ignored by the comparison, with one
warning per line on stderr naming only the file and line number:

```text
envguard: warning: .env.prod:7: malformed line, ignored
```

### Exit codes

| Code | `check`                         | `diff`              |
| ---- | ------------------------------- | ------------------- |
| 0    | No errors (warnings allowed)    | Files have the same keys and values |
| 1    | At least one error              | Differences found   |
| 2    | Usage error or unreadable file  | Usage error or unreadable file |

## Supported syntax

```sh
# full-line comments and blank lines
KEY=value
export KEY=value
KEY='single quoted, taken literally'
KEY="double quoted, supports \" \\ and \n escapes"
KEY=value # inline comment (needs whitespace before #)
```

Not supported yet: multiline quoted values and `${VAR}` interpolation.

## Development

```sh
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
ruff format --check .
ruff check .
python -m pytest -q
```

Layout:

- `src/envguard/core/`: pure parsing, checks, and diff. No file I/O, no imports from `cli` or `io`.
- `src/envguard/io/`: reading files from disk.
- `src/envguard/cli/`: argparse entry point and text output. Depends on `core` and `io`.

## License

MIT. See [LICENSE](LICENSE).
