"""Key-level differences between two parsed .env files."""

from __future__ import annotations

from dataclasses import dataclass, field

from envguard.core.models import ParseResult


@dataclass
class DiffResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def diff_env(a: ParseResult, b: ParseResult) -> DiffResult:
    """Compare ``a`` (old) to ``b`` (new). Only key names are reported, never values."""
    old = a.as_dict()
    new = b.as_dict()
    return DiffResult(
        added=sorted(k for k in new if k not in old),
        removed=sorted(k for k in old if k not in new),
        changed=sorted(k for k in old if k in new and old[k].value != new[k].value),
    )
