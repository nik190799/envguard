"""Human-readable text output. Formatters receive only keys and messages, never values."""

from __future__ import annotations

from envguard.core.diff import DiffResult
from envguard.core.models import Issue, Severity


def format_issue(issue: Issue, env_path: str, example_path: str) -> str:
    path = env_path if issue.source == "env" else example_path
    location = f"{path}:{issue.line_no}" if issue.line_no is not None else path
    subject = f"{issue.key}: " if issue.key else ""
    return f"{location}: {issue.severity.value}: {subject}{issue.message} [{issue.kind.value}]"


def format_check(issues: list[Issue], env_path: str, example_path: str) -> list[str]:
    if not issues:
        return [f"OK: {env_path} matches {example_path}"]
    lines = [format_issue(i, env_path, example_path) for i in issues]
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
