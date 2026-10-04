"""Data types shared by the parser, the checks, and the diff.

Values are stored so checks can inspect them (for example, to detect empty values),
but nothing in this module renders a value. Formatters must only ever use keys,
line numbers, and messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Entry:
    """One ``KEY=value`` assignment from a .env file."""

    key: str
    value: str
    line_no: int
    exported: bool = False
    quote: str | None = None
    comment: str | None = None

    def __repr__(self) -> str:  # never leak the value through repr()
        return f"Entry(key={self.key!r}, line_no={self.line_no})"


@dataclass(frozen=True)
class ParseError:
    """A line that could not be parsed. ``message`` never contains the line's content."""

    line_no: int
    message: str


@dataclass
class ParseResult:
    entries: list[Entry] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)

    def distinct_keys(self) -> list[str]:
        """Distinct keys in first-seen order."""
        return list(dict.fromkeys(e.key for e in self.entries))

    def as_dict(self) -> dict[str, Entry]:
        """Map each key to its last assignment (later assignments win, as in most loaders)."""
        return {e.key: e for e in self.entries}


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class IssueKind(str, Enum):
    MISSING = "missing"
    EXTRA = "extra"
    DUPLICATE = "duplicate"
    MALFORMED = "malformed"
    EMPTY_REQUIRED = "empty-required"


DEFAULT_SEVERITY: dict[IssueKind, Severity] = {
    IssueKind.MISSING: Severity.ERROR,
    IssueKind.EXTRA: Severity.WARNING,
    IssueKind.DUPLICATE: Severity.ERROR,
    IssueKind.MALFORMED: Severity.ERROR,
    IssueKind.EMPTY_REQUIRED: Severity.ERROR,
}


@dataclass(frozen=True)
class Issue:
    """A single finding from ``check_env``.

    ``source`` names which input the issue is about: ``"env"`` or ``"example"``.
    """

    kind: IssueKind
    severity: Severity
    message: str
    source: str
    key: str | None = None
    line_no: int | None = None
