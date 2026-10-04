"""Parse the text of a .env file into entries.

Supported syntax:

* blank lines and full-line ``#`` comments
* ``KEY=value`` and ``export KEY=value``
* single-quoted values (literal) and double-quoted values (``\\"``, ``\\\\``, ``\\n`` escapes)
* an inline ``# comment`` after a value, when preceded by whitespace

Not supported (yet): multiline quoted values and ``${VAR}`` interpolation.
"""

from __future__ import annotations

import re

from envguard.core.models import Entry, ParseError, ParseResult

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# ``export`` plus any whitespace, unless what follows is ``=`` (a key named ``export``).
_EXPORT_PREFIX_RE = re.compile(r"^export\s+(?=[^=\s])")
_DOUBLE_QUOTE_ESCAPES = {'"': '"', "\\": "\\", "n": "\n"}


class _LineError(Exception):
    pass


def parse(text: str) -> ParseResult:
    """Parse .env ``text``. Never raises on bad input; problems become ``ParseError``s."""
    result = ParseResult()
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            result.entries.append(_parse_line(line, line_no))
        except _LineError as exc:
            result.errors.append(ParseError(line_no=line_no, message=str(exc)))
    return result


def _parse_line(line: str, line_no: int) -> Entry:
    exported = False
    prefix = _EXPORT_PREFIX_RE.match(line)
    if prefix:
        exported = True
        line = line[prefix.end() :]

    key, sep, rest = line.partition("=")
    if not sep:
        raise _LineError("expected KEY=VALUE")
    key = key.strip()
    if not key:
        raise _LineError("missing key before '='")
    if not KEY_RE.match(key):
        raise _LineError("invalid key name")

    quote: str | None = None
    stripped = rest.lstrip()
    if stripped[:1] in ("'", '"'):
        value, quote, comment = _parse_quoted(stripped)
    else:
        value, comment = _parse_unquoted(rest)

    return Entry(
        key=key,
        value=value,
        line_no=line_no,
        exported=exported,
        quote=quote,
        comment=comment,
    )


def _parse_unquoted(rest: str) -> tuple[str, str | None]:
    for i, ch in enumerate(rest):
        if ch == "#" and i > 0 and rest[i - 1].isspace():
            return rest[:i].strip(), rest[i + 1 :].strip()
    return rest.strip(), None


def _parse_quoted(text: str) -> tuple[str, str, str | None]:
    quote = text[0]
    chars: list[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            chars.append(_DOUBLE_QUOTE_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        if ch == quote:
            break
        chars.append(ch)
        i += 1
    else:
        raise _LineError("unterminated quoted value")

    trailing = text[i + 1 :].strip()
    if trailing and not trailing.startswith("#"):
        raise _LineError("unexpected characters after closing quote")
    comment = trailing[1:].strip() if trailing else None
    return "".join(chars), quote, comment
