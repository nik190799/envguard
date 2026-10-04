"""Read .env files from disk."""

from __future__ import annotations

from pathlib import Path

from envguard.core.models import ParseResult
from envguard.core.parser import parse


class EnvFileError(Exception):
    """Raised when a file cannot be read. The message names the file, never its contents."""


def read_text(path: str | Path) -> str:
    p = Path(path)
    if p.is_dir():
        raise EnvFileError(f"not a file: {p}")
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise EnvFileError(f"file not found: {p}") from None
    except UnicodeDecodeError:
        raise EnvFileError(f"not valid UTF-8: {p}") from None
    except OSError as exc:
        raise EnvFileError(f"cannot read {p}: {exc.strerror}") from None


def load_env(path: str | Path) -> ParseResult:
    return parse(read_text(path))
