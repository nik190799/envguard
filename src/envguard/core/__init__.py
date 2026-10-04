"""Pure parsing and checking logic. No file I/O, no imports from envguard.cli or envguard.io."""

from envguard.core.checks import check_env
from envguard.core.diff import DiffResult, diff_env
from envguard.core.models import Entry, Issue, IssueKind, ParseError, ParseResult, Severity
from envguard.core.parser import parse

__all__ = [
    "DiffResult",
    "Entry",
    "Issue",
    "IssueKind",
    "ParseError",
    "ParseResult",
    "Severity",
    "check_env",
    "diff_env",
    "parse",
]
