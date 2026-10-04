"""Human-readable text output. Formatters receive only keys and messages, never values."""

from __future__ import annotations

from envguard.core.diff import DiffResult
from envguard.core.models import Issue, IssueKind, ParseError, ParseResult, Severity


def example_lines(example: ParseResult) -> dict[str, int]:
    """Map each key to the line of its first definition in the example file."""
    lines: dict[str, int] = {}
    for entry in example.entries:
        lines.setdefault(entry.key, entry.line_no)
    return lines


def format_issue(
    issue: Issue,
    env_path: str,
    example_path: str,
    example_line_nos: dict[str, int] | None = None,
) -> str:
    path = env_path if issue.source == "env" else example_path
    line_no = issue.line_no
    if issue.kind is IssueKind.MISSING and example_line_nos and issue.key in example_line_nos:
        # A missing key lives only in the example file: point at its definition there.
        path = example_path
        line_no = example_line_nos[issue.key]
    location = f"{path}:{line_no}" if line_no is not None else path
    subject = f"{issue.key}: " if issue.key else ""
    return f"{location}: {issue.severity.value}: {subject}{issue.message} [{issue.kind.value}]"


def format_check(
    issues: list[Issue],
    env_path: str,
    example_path: str,
    example_line_nos: dict[str, int] | None = None,
) -> list[str]:
    if not issues:
        return [f"OK: {env_path} matches {example_path}"]
    lines = [format_issue(i, env_path, example_path, example_line_nos) for i in issues]
    errors = sum(1 for i in issues if i.severity is Severity.ERROR)
    warnings = len(issues) - errors
    lines.append(f"{errors} error(s), {warnings} warning(s)")
    return lines


def format_diff(result: DiffResult) -> list[str]:
    if result.is_empty:
        return ["no differences"]
    return (
        [f"+ {k} added" for k in result.added]
        + [f"- {k} removed" for k in result.removed]
        + [f"~ {k} changed" for k in result.changed]
    )


def format_diff_warnings(errors: list[ParseError], path: str) -> list[str]:
    """One warning per malformed line: path and line number only, never content."""
    return [f"envguard: warning: {path}:{err.line_no}: malformed line, ignored" for err in errors]
