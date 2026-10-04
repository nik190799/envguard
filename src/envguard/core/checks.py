"""Compare a parsed .env file against a parsed .env.example."""

from __future__ import annotations

from envguard.core.models import DEFAULT_SEVERITY, Issue, IssueKind, ParseResult

REQUIRED_MARKER = "required"


def is_required(comment: str | None) -> bool:
    """A key is required when its example line ends with ``# required``."""
    return comment is not None and comment.strip().lower() == REQUIRED_MARKER


def _issue(
    kind: IssueKind,
    message: str,
    source: str,
    key: str | None = None,
    line_no: int | None = None,
) -> Issue:
    return Issue(
        kind=kind,
        severity=DEFAULT_SEVERITY[kind],
        message=message,
        source=source,
        key=key,
        line_no=line_no,
    )


def find_malformed(result: ParseResult, source: str) -> list[Issue]:
    return [
        _issue(IssueKind.MALFORMED, err.message, source, line_no=err.line_no)
        for err in result.errors
    ]


def find_duplicates(result: ParseResult, source: str) -> list[Issue]:
    seen: dict[str, int] = {}
    issues: list[Issue] = []
    for entry in result.entries:
        if entry.key in seen:
            issues.append(
                _issue(
                    IssueKind.DUPLICATE,
                    f"duplicate key (first defined on line {seen[entry.key]})",
                    source,
                    key=entry.key,
                    line_no=entry.line_no,
                )
            )
        else:
            seen[entry.key] = entry.line_no
    return issues


def find_missing(env: ParseResult, example: ParseResult) -> list[Issue]:
    present = set(env.distinct_keys())
    return [
        _issue(IssueKind.MISSING, "missing from env file", "env", key=key)
        for key in example.distinct_keys()
        if key not in present
    ]


def find_extra(env: ParseResult, example: ParseResult) -> list[Issue]:
    expected = set(example.distinct_keys())
    return [
        _issue(IssueKind.EXTRA, "not listed in example file", "env", key=e.key, line_no=e.line_no)
        for e in env.as_dict().values()
        if e.key not in expected
    ]


def find_empty_required(env: ParseResult, example: ParseResult) -> list[Issue]:
    env_entries = env.as_dict()
    issues: list[Issue] = []
    for ex in example.as_dict().values():
        entry = env_entries.get(ex.key)
        if entry is not None and is_required(ex.comment) and entry.value == "":
            issues.append(
                _issue(
                    IssueKind.EMPTY_REQUIRED,
                    "required key has an empty value",
                    "env",
                    key=entry.key,
                    line_no=entry.line_no,
                )
            )
    return issues


def check_env(env: ParseResult, example: ParseResult) -> list[Issue]:
    """Run every check and return issues in a stable order."""
    return [
        *find_malformed(example, "example"),
        *find_duplicates(example, "example"),
        *find_malformed(env, "env"),
        *find_duplicates(env, "env"),
        *find_missing(env, example),
        *find_empty_required(env, example),
        *find_extra(env, example),
    ]
