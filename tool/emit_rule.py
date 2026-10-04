#!/usr/bin/env python3
"""Cadence Layer-3 rule emitter — closes the retrospective loop.

Reads a single retrospective finding (JSON conforming to
``schemas/retro.schema.json``) on stdin or from ``--input``. For findings
with ``fix_layer == 3`` and ``auto_method == "boundary-rule"``, this
helper materializes a regression fixture under
``tests/fixtures/retro/<short-id>/`` containing:

    - the exact offending import as it appeared in source
    - a fixture ``.cadence/cadence.yaml`` with the proposed new boundary
      rule
    - (optionally, with ``--apply``) appends the same rule to the
      project's root ``.cadence/cadence.yaml``

It then invokes the existing ``tool/check_boundaries.py`` against the
fixture and refuses to record the finding as "landed" unless the
checker flags the offending line. A rule that does not fire on its own
trigger case is worse than no rule, so the emitter exits non-zero in
that case and prints a remediation hint.

Factory mode (docs/LEARNING.md, "Check proof"):
    ``--provenance-patch FILE --provenance-path PATH --provenance-line N``
    (all three or none) ties the sample to the real failing line. The
    patch must add, in its file section for ``b/PATH``, the line
    ``import_line`` verbatim at new-file line N, and the line must match
    the strict import-line pattern for PATH's language. The sample is
    then written at ``<fixture>/<PATH>`` (a lint-silencing header, the
    line, a stub footer), next to ``finding.json`` and
    ``provenance.json``. ``--must-pass-root`` also requires that the new
    rule finds nothing in the project itself (exit 3 otherwise), and
    ``--rule-id L-xxxxxxxx`` (which must equal ``"L-"`` + the finding's
    short id) is written as the rule's ``id``.

Other modes (standalone, no finding):
    ``--retire ID``   remove the learned rule with that ``id`` from
                      ``.cadence/cadence.yaml``, keeping every other byte.
    ``--replay``      run each fixture under ``--fixture-root`` against its
                      own one-rule config; print one JSON line per fixture.

Input hardening (always on, all before the first write):
    - the finding validates against retro.schema.json with format checks,
      its ``id`` parses as a UUID and its ``ts`` is an RFC 3339 timestamp;
    - ``where`` and ``forbidden_pattern`` use a fixed character set, have
      no ``..`` segment, do not start with ``/`` or ``tests/fixtures/``;
    - ``import_line`` is 1-300 characters with no control or format
      characters (tab allowed);
    - the resolved sample path stays inside the fixture directory.

``--apply`` is text-preserving: the rule is inserted as a block after the
last ``boundaries:`` item, comments and formatting elsewhere are kept,
and the result must parse to the old config with the rule appended (the
original file is restored otherwise).

Exit codes:
    0   fixture written, checker fires on the sample — finding may be
        recorded as landed (``--retire``: rule removed; ``--replay``: every
        fixture fired)
    1   rule does NOT fire on the sample — emitter refuses to mark as
        landed; the human must revise the rule (``--replay``: some fixture
        did not fire)
    2   bad input (schema invalid, missing required block, IO error), or
        an internal error
    3   ``--must-pass-root``: the rule fires on the project itself
    4   ``--retire``: no rule with that id

    The CLI removes a fixture directory it created when it exits 1 or 3
    (and restores the previous one when ``--force`` replaced it).

Usage:
    python tool/emit_rule.py < finding.json
    python tool/emit_rule.py --input finding.json
    python tool/emit_rule.py --input finding.json --apply
    python tool/emit_rule.py --input finding.json --fixture-root tests/fixtures/retro
    python tool/emit_rule.py --input f.json --rule-id L-1a2b3c4d \\
        --provenance-patch patches/123-1.patch --provenance-path src/domain/a.ts \\
        --provenance-line 3 --must-pass-root --apply --json
    python tool/emit_rule.py --retire L-1a2b3c4d
    python tool/emit_rule.py --replay --json --skip-ids L-1a2b3c4d

    ``--schema-dir DIR`` reads retro.schema.json from DIR instead of
    .cadence/ (or plugins/cadence/schemas/); ``--now EPOCH`` pins the
    provenance timestamp. ``--json`` prints exactly one JSON line,
    ``{"fixture", "sample", "rule", "fired", "violations",
    "root_violations", "applied", "exit"}``, and sends the human-readable
    text to stderr. The emitter never writes Python bytecode, so loading
    the project's checker leaves no tool/__pycache__ behind.

Design notes:
    - Only ``violation_sample.kind == "boundary-rule"`` is wired in
      v0.3.0-rc.1. Future emitters (lint-rule, schema-rule) will
      register additional kinds.
    - The emitter is deliberately deterministic: same input produces
      the same fixture path. By default it refuses to overwrite an
      existing fixture for the same finding ID; pass ``--force`` to
      rewrite (typically only useful while iterating, or when the ladder
      re-promotes a retired check).
    - The fixture is portable: each fixture directory ships its own
      ``.cadence/cadence.yaml`` with one rule, so the regression test
      isolates from the project's full ruleset.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import sys
import time
import traceback
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML is required. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print(
        "ERROR: jsonschema is required. Install with: pip install jsonschema",
        file=sys.stderr,
    )
    sys.exit(2)


# --- Shared constants (docs/LEARNING.md; tests/test_learning_contract.py) ---

RULE_ID_RE = r"^[LB]-[0-9a-f]{8}$"
LESSON_ID_RE = r"^L-[0-9a-f]{8}$"
RETRO_FIXTURE_PREFIX = "tests/fixtures/retro/"

LANG_FAMILY: dict[str, str] = {
    ".ts": "ts",
    ".tsx": "ts",
    ".js": "ts",
    ".jsx": "ts",
    ".mjs": "ts",
    ".cjs": "ts",
    ".py": "py",
    ".dart": "dart",
}

SAMPLE_LANGUAGE: dict[str, str] = {
    ".ts": "ts",
    ".tsx": "tsx",
    ".js": "js",
    ".jsx": "js",
    ".mjs": "js",
    ".cjs": "js",
    ".py": "py",
    ".dart": "dart",
}

IMPORT_LINE_PATTERNS: dict[str, tuple[str, ...]] = {
    "ts": (
        r"""^\s*import\s+(?:type\s+)?(?:[\w$]+\s*,\s*)?(?:[\w$]+|\*\s+as\s+[\w$]+|\{[\w$\s,]*\})\s+from\s+(['"])[^'"\\\s]{1,150}\1\s*;?\s*$""",
        r"""^\s*import\s+(['"])[^'"\\\s]{1,150}\1\s*;?\s*$""",
        r"""^\s*export\s+(?:type\s+)?(?:\*(?:\s+as\s+[\w$]+)?|\{[\w$\s,]*\})\s+from\s+(['"])[^'"\\\s]{1,150}\1\s*;?\s*$""",
        r"""^\s*(?:const|let|var)\s+(?:[\w$]+|\{[\w$\s,:]*\})\s*=\s*require\(\s*(['"])[^'"\\\s]{1,150}\1\s*\)\s*;?\s*$""",
    ),
    "py": (
        r"""^\s*from\s+(?:\.{1,5}|\.{0,5}[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,15})\s+import\s+(?:\*|\(?\s*[A-Za-z_][A-Za-z0-9_]*(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?){0,30}\s*,?\s*\)?)\s*$""",
        r"""^\s*import\s+[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,15}(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,15}(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?){0,15}\s*$""",
    ),
    "dart": (
        r"""^\s*(?:import|export)\s+'[^'\\\s]{1,150}'(?:\s+deferred)?(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?(?:\s+(?:show|hide)\s+[A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*){0,30})*\s*;\s*$""",
    ),
}

# --- Local patterns -----------------------------------------------------------

_RULE_ID = re.compile(RULE_ID_RE)
_LESSON_ID = re.compile(LESSON_ID_RE)
_IMPORT_LINE_RES: dict[str, tuple[re.Pattern[str], ...]] = {
    family: tuple(re.compile(p, re.ASCII) for p in patterns)
    for family, patterns in IMPORT_LINE_PATTERNS.items()
}
# where / forbidden_pattern (retro.schema.json, violation_sample).
_SAFE_GLOB = re.compile(r"^[A-Za-z0-9_.@*-][A-Za-z0-9_.@/*-]{0,199}$")
_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)
_SAMPLE_PATH = re.compile(r"^[A-Za-z0-9_.@+/-]{1,300}$")
_CLASS_KEY = re.compile(
    r"^(import-edge|guarded|missing-test|test|edit|review|gate|agent|pr):"
    r"[A-Za-z0-9_./@+:>-]{1,180}$"
)
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_ITEM_RE = re.compile(r"^( *)-(?: |$)")
_BOUNDARIES_BLOCK_RE = re.compile(r"^boundaries:[ \t]*(?:#.*)?$")
_BOUNDARIES_EMPTY_RE = re.compile(r"^boundaries:[ \t]*\[[ \t]*\][ \t]*(?:#.*)?$")
_FIXTURE_NAME = re.compile(r"^[0-9a-f]{8}$")

MAX_PATCH_BYTES = 4 * 1024 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

EXIT_OK = 0
EXIT_NOT_FIRED = 1
EXIT_BAD_INPUT = 2
EXIT_FIRES_ON_ROOT = 3
EXIT_NOT_FOUND = 4

_SAMPLE_HEADERS: dict[str, str] = {
    "ts": "// @ts-nocheck\n",
    "py": "# ruff: noqa\n# mypy: ignore-errors\n",
    "dart": "// ignore_for_file: type=lint, uri_does_not_exist\n",
}


_LANG_EXT: dict[str, str] = {
    "ts": "ts",
    "tsx": "tsx",
    "js": "js",
    "py": "py",
    "go": "go",
    "rs": "rs",
    "dart": "dart",
    "java": "java",
    "kt": "kt",
    "swift": "swift",
}


@dataclass(frozen=True)
class Provenance:
    """Where a factory sample came from: a stored patch and a line in it."""

    patch: Path
    path: str
    line_no: int


@dataclass(frozen=True)
class EmitResult:
    fixture_dir: Path
    sample_file: Path
    fixture_config: Path
    rule: dict[str, Any]
    fired: bool
    violation_count: int
    root_violations: tuple[tuple[str, int], ...] = ()
    applied: bool = False


def _fail(message: str) -> None:
    """Report bad input and exit 2 (the emitter's historical contract)."""
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(EXIT_BAD_INPUT)


def _short_id(finding: dict[str, Any]) -> str:
    raw = finding.get("id", "")
    try:
        return str(uuid.UUID(raw)).split("-")[0]
    except (ValueError, AttributeError, TypeError):
        slug = re.sub(r"[^a-z0-9]+", "-", finding.get("what_happened", "").lower())
        slug = slug.strip("-")[:24] or "anon"
        return slug


def _where_to_dir(where: str) -> Path:
    cleaned = where.replace("**", "").replace("*", "").strip("/")
    return Path(cleaned) if cleaned else Path("src")


def _sample_stub(language: str, import_line: str) -> str:
    """Return a syntactically inert source file containing ``import_line``.

    The checker only inspects import-shaped lines, so the surrounding
    file can be minimal. We do add a language-appropriate footer so the
    file isn't a single dangling import (some tools complain).
    """
    if language in ("ts", "tsx", "js"):
        return f"{import_line}\n\nexport const __retro_sample = true;\n"
    if language == "py":
        return f"{import_line}\n\n__retro_sample = True\n"
    if language == "go":
        return f"package retrosample\n\n{import_line}\n"
    if language == "rs":
        return f"{import_line}\n\npub const RETRO_SAMPLE: bool = true;\n"
    if language == "dart":
        return f"{import_line}\n\nconst retroSample = true;\n"
    if language in ("java", "kt"):
        return f"{import_line}\n\npublic class RetroSample {{ }}\n"
    if language == "swift":
        return f"{import_line}\n\nlet retroSample = true\n"
    return f"{import_line}\n"


def _load_checker(project_root: Path) -> Any:
    """Locate and import the project's check_boundaries.py.

    Search order:
        1. ``<project_root>/tool/check_boundaries.py`` (the canonical
           install path scaffolded by /cadence-init)
        2. ``<project_root>/plugins/cadence/templates/tool/check_boundaries.py``
           (the dogfood case — when running emit_rule.py inside the
           Cadence repo itself)
    """
    candidates = [
        project_root / "tool" / "check_boundaries.py",
        project_root / "plugins" / "cadence" / "templates" / "tool" / "check_boundaries.py",
    ]
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location(
                "_cadence_check_boundaries", path
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules["_cadence_check_boundaries"] = module
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError(
        "Could not locate check_boundaries.py. Looked under tool/ and "
        "plugins/cadence/templates/tool/. Run /cadence-init first?"
    )


def _load_schema(project_root: Path, schema_dir: Path | None = None) -> dict[str, Any]:
    if schema_dir is not None:
        candidates = [schema_dir / "retro.schema.json"]
    else:
        candidates = [
            project_root / ".cadence" / "retro.schema.json",
            project_root / "plugins" / "cadence" / "schemas" / "retro.schema.json",
        ]
    for path in candidates:
        if path.is_file():
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
    raise FileNotFoundError(
        "Could not locate retro.schema.json. Looked under .cadence/ and "
        "plugins/cadence/schemas/."
    )


def _validate(finding: dict[str, Any], schema: dict[str, Any]) -> None:
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    errors = sorted(validator.iter_errors(finding), key=lambda e: list(e.absolute_path))
    if errors:
        print("ERROR: finding fails retro.schema.json validation:", file=sys.stderr)
        for err in errors:
            loc = ".".join(str(p) for p in err.absolute_path) or "<root>"
            print(f"  {loc}: {err.message}", file=sys.stderr)
        sys.exit(EXIT_BAD_INPUT)


# --- Input hardening ----------------------------------------------------------


def import_line_ok(family: str, line: str) -> bool:
    """True if ``line`` is one strict, single import statement of ``family``."""
    if not isinstance(line, str) or not 1 <= len(line) <= 200:
        return False
    if any(not (ch == "\t" or 32 <= ord(ch) < 127) for ch in line):
        return False
    return any(rx.fullmatch(line) for rx in _IMPORT_LINE_RES.get(family, ()))


def _has_control(text: str) -> bool:
    for ch in text:
        if ch == "\t":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp"):
            return True
    return False


def _glob_problem(value: Any, name: str) -> str | None:
    if not isinstance(value, str) or not _SAFE_GLOB.fullmatch(value):
        return f"{name} {value!r} must be 1-200 characters of letters, digits and _.@*-/"
    if value.startswith("/"):
        return f"{name} must not start with '/'"
    if any(seg == ".." for seg in value.split("/")):
        return f"{name} must not contain a '..' segment"
    if value.startswith("tests/fixtures/"):
        return f"{name} must not point into tests/fixtures/"
    return None


def _relative_posix_problem(value: Any, name: str) -> str | None:
    if not isinstance(value, str) or not _SAMPLE_PATH.fullmatch(value):
        return f"{name} {value!r} must be 1-300 characters of letters, digits and _.@+-/"
    parts = value.split("/")
    if value.startswith("/") or any(p in ("", ".", "..") for p in parts):
        return f"{name} {value!r} must be a plain relative path"
    if value.startswith("tests/fixtures/"):
        return f"{name} must not point into tests/fixtures/"
    return None


def _check_finding_fields(finding: dict[str, Any]) -> None:
    """Code checks the schema cannot express, plus the always-on hardening."""
    try:
        uuid.UUID(str(finding.get("id")))
    except (ValueError, AttributeError, TypeError):
        _fail(f"finding id {finding.get('id')!r} is not a UUID")
    ts = finding.get("ts")
    if not isinstance(ts, str) or not _TS_RE.fullmatch(ts):
        _fail(f"finding ts {ts!r} is not an RFC 3339 timestamp")

    sample = finding.get("violation_sample")
    if not isinstance(sample, dict):
        _fail("finding has no violation_sample object")
    for key, name in (("where", "where"), ("forbidden_pattern", "forbidden_pattern")):
        problem = _glob_problem(sample.get(key), name)
        if problem:
            _fail(problem)
    line = sample.get("import_line")
    if not isinstance(line, str) or not 1 <= len(line) <= 300:
        _fail("import_line must be 1-300 characters")
    if _has_control(line):
        _fail("import_line must be one line with no control characters")
    reason = sample.get("reason")
    if not isinstance(reason, str) or _has_control(reason):
        _fail("reason must be one line with no control characters")


def _added_line_at(patch_text: str, path: str, line_no: int) -> str | None:
    """The line the patch adds at new-file line ``line_no`` of ``b/<path>``.

    Hunks are consumed by their line counts, so an added line that happens
    to start with ``++`` is never mistaken for a file header.
    """
    lines = patch_text.split("\n")
    in_target = False
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        if line.startswith("diff --git "):
            in_target = False
            i += 1
            continue
        if line.startswith("+++ "):
            in_target = line[4:] == f"b/{path}"
            i += 1
            continue
        match = _HUNK_RE.match(line)
        if not match:
            i += 1
            continue
        old_left = int(match.group(2)) if match.group(2) is not None else 1
        new_left = int(match.group(4)) if match.group(4) is not None else 1
        current = int(match.group(3))
        i += 1
        while i < len(lines) and (old_left > 0 or new_left > 0):
            body = lines[i]
            if body.startswith("\\"):
                i += 1
                continue
            tag = body[:1]
            if tag == "+":
                if in_target and current == line_no:
                    return body[1:].rstrip("\r")
                current += 1
                new_left -= 1
            elif tag == "-":
                old_left -= 1
            elif tag == " " or body in ("", "\r"):
                current += 1
                old_left -= 1
                new_left -= 1
            else:
                break
            i += 1
    return None


def _check_provenance(sample: dict[str, Any], provenance: Provenance) -> tuple[str, str]:
    """Validate a factory sample against its patch. Returns (sha256, family)."""
    problem = _relative_posix_problem(provenance.path, "--provenance-path")
    if problem:
        _fail(problem)
    ext = PurePosixPath(provenance.path).suffix
    family = LANG_FAMILY.get(ext)
    if family is None:
        _fail(
            f"--provenance-path {provenance.path!r} is not a TS/JS, Python or "
            "Dart file"
        )
    if isinstance(provenance.line_no, bool) or not (
        isinstance(provenance.line_no, int) and provenance.line_no >= 1
    ):
        _fail("--provenance-line must be an integer >= 1")
    line = sample["import_line"]
    if not import_line_ok(family, line):
        _fail(
            "import_line is not one strict import statement for "
            f"{provenance.path!r} (IMPORT_LINE_PATTERNS[{family!r}])"
        )
    try:
        data = provenance.patch.read_bytes()
    except OSError as exc:
        _fail(f"could not read --provenance-patch {provenance.patch}: {exc}")
    if len(data) > MAX_PATCH_BYTES:
        _fail(f"--provenance-patch is larger than {MAX_PATCH_BYTES} bytes")
    text = data.decode("utf-8", errors="replace")
    added = _added_line_at(text, provenance.path, provenance.line_no)
    if added is None:
        _fail(
            f"the patch adds no line at {provenance.path}:{provenance.line_no}"
        )
    if added != line:
        _fail(
            f"the line the patch adds at {provenance.path}:{provenance.line_no} "
            "is not import_line verbatim"
        )
    return hashlib.sha256(data).hexdigest(), family


def _fixture_dir_for(finding: dict[str, Any], fixture_root: Path) -> Path:
    return (fixture_root / _short_id(finding)).resolve()


# --- cadence.yaml text edits ----------------------------------------------------


def _split_text(text: str) -> tuple[list[str], str, bool]:
    """(lines, newline, ends_with_newline); ``_join_text`` gives ``text`` back."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    ends = lines[-1] == ""
    if ends:
        lines.pop()
    return lines, newline, ends


def _join_text(lines: list[str], newline: str, ends: bool) -> str:
    return newline.join(lines) + (newline if ends else "")


def _boundaries_region(lines: list[str], key_idx: int) -> tuple[int, int, str | None]:
    """(first, end, item_indent) of the block after ``boundaries:``.

    ``end`` is exclusive and stops at the next top-level key. Item lines
    are ``- `` lines at the smallest indent inside the block.
    """
    end = len(lines)
    for j in range(key_idx + 1, len(lines)):
        line = lines[j]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            continue
        match = _ITEM_RE.match(line)
        if match and match.group(1) == "":
            continue
        end = j
        break
    indent: str | None = None
    for j in range(key_idx + 1, end):
        match = _ITEM_RE.match(lines[j])
        if match and (indent is None or len(match.group(1)) < len(indent)):
            indent = match.group(1)
    return key_idx + 1, end, indent


def _last_content(lines: list[str], first: int, end: int) -> int | None:
    last = None
    for j in range(first, end):
        stripped = lines[j].strip()
        if stripped and not stripped.startswith("#"):
            last = j
    return last


def _rule_block(rule: dict[str, Any], indent: str) -> list[str]:
    out: list[str] = []

    def entry(text: str) -> None:
        out.append(indent + ("- " if not out else "  ") + text)

    if rule.get("id"):
        entry(f"id: {rule['id']}")
    entry(f"where: {json.dumps(rule['where'])}")
    entry("forbidden:")
    for pattern in rule["forbidden"]:
        out.append(f"{indent}    - {json.dumps(pattern)}")
    entry(f"reason: {json.dumps(rule['reason'])}")
    return out


def _load_yaml_text(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise ValueError(f"malformed YAML: {exc}") from exc


def _find_key(lines: list[str]) -> int | None:
    found = [i for i, line in enumerate(lines) if line.startswith("boundaries:")]
    if len(found) > 1:
        raise ValueError("more than one top-level boundaries: key")
    return found[0] if found else None


def insert_rule_text(text: str, rule: dict[str, Any]) -> tuple[str, bool]:
    """Return (new_text, added). Text outside the inserted block is kept.

    Raises ValueError when the file cannot be edited safely.
    """
    old = _load_yaml_text(text)
    if old is None:
        old = {}
    if not isinstance(old, dict):
        raise ValueError("cadence.yaml is not a YAML mapping")
    existing = old.get("boundaries")
    if existing is None:
        existing = []
    if not isinstance(existing, list):
        raise ValueError("boundaries is not a list")
    for entry in existing:
        if (
            isinstance(entry, dict)
            and entry.get("where") == rule["where"]
            and list(entry.get("forbidden") or []) == list(rule["forbidden"])
        ):
            return text, False

    lines, newline, ends = _split_text(text)
    key_idx = _find_key(lines)
    if key_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("boundaries:")
        lines.extend(_rule_block(rule, "  "))
    elif _BOUNDARIES_EMPTY_RE.match(lines[key_idx]):
        lines[key_idx : key_idx + 1] = ["boundaries:", *_rule_block(rule, "  ")]
    elif _BOUNDARIES_BLOCK_RE.match(lines[key_idx]):
        first, end, indent = _boundaries_region(lines, key_idx)
        last = _last_content(lines, first, end)
        at = (last + 1) if last is not None else key_idx + 1
        lines[at:at] = _rule_block(rule, indent if indent is not None else "  ")
    else:
        raise ValueError(
            "boundaries: is not a block list or an empty [] (flow lists are "
            "not edited)"
        )
    new_text = _join_text(lines, newline, ends or not text)

    expected = dict(old)
    expected["boundaries"] = [*existing, dict(rule)]
    if _load_yaml_text(new_text) != expected:
        raise ValueError("the edited file does not parse to the old config plus the rule")
    return new_text, True


def remove_rule_text(text: str, rule_id: str) -> str:
    """Return ``text`` without the boundaries item whose ``id`` is ``rule_id``.

    Raises LookupError if no item has that id, ValueError on anything else.
    """
    old = _load_yaml_text(text)
    if not isinstance(old, dict):
        raise ValueError("cadence.yaml is not a YAML mapping")
    existing = old.get("boundaries") or []
    if not isinstance(existing, list):
        raise ValueError("boundaries is not a list")
    matches = [
        i
        for i, entry in enumerate(existing)
        if isinstance(entry, dict) and entry.get("id") == rule_id
    ]
    if not matches:
        raise LookupError(rule_id)
    if len(matches) > 1:
        raise ValueError(f"more than one rule has id {rule_id}")
    target = matches[0]

    lines, newline, ends = _split_text(text)
    key_idx = _find_key(lines)
    if key_idx is None or not _BOUNDARIES_BLOCK_RE.match(lines[key_idx]):
        raise ValueError("boundaries: is not a block list")
    first, end, indent = _boundaries_region(lines, key_idx)
    if indent is None:
        raise ValueError("no list items under boundaries:")
    starts = [
        j
        for j in range(first, end)
        if (m := _ITEM_RE.match(lines[j])) and m.group(1) == indent
    ]
    if len(starts) != len(existing):
        raise ValueError("could not match list items to parsed rules")
    last = _last_content(lines, first, end)
    if target + 1 < len(starts):
        stop = starts[target + 1]
    else:
        stop = (last if last is not None else starts[target]) + 1
    chunk = "\n".join(lines[starts[target] : stop])
    parsed = _load_yaml_text(chunk)
    if not (isinstance(parsed, list) and len(parsed) == 1 and parsed[0] == existing[target]):
        raise ValueError("the located item is not the rule to remove")
    del lines[starts[target] : stop]
    if len(existing) == 1:
        lines[key_idx] = "boundaries: []"
    new_text = _join_text(lines, newline, ends)

    expected = dict(old)
    expected["boundaries"] = [e for i, e in enumerate(existing) if i != target]
    if _load_yaml_text(new_text) != expected:
        raise ValueError("the edited file does not parse to the old config minus the rule")
    return new_text


def _write_preserving(path: Path, new_text: str, old_text: str, check: Any) -> None:
    """Write ``new_text``; re-read it and restore ``old_text`` if ``check`` fails."""
    path.write_bytes(new_text.encode("utf-8"))
    try:
        reread = path.read_bytes().decode("utf-8")
        ok = check(_load_yaml_text(reread))
    except (OSError, UnicodeDecodeError, ValueError):
        ok = False
    if not ok:
        path.write_bytes(old_text.encode("utf-8"))
        raise ValueError("the written file did not parse as expected; restored")


def _append_rule_to_root_config(
    root_config: Path, rule: dict[str, Any]
) -> bool:
    """Append ``rule`` to the project's ``.cadence/cadence.yaml`` ``boundaries``
    list, keeping the rest of the file's text. Returns True if the rule was
    added; False if an equivalent rule (same where + forbidden) already
    exists or the file is missing. Exits 2 when the file cannot be edited
    safely (the original is kept).
    """
    if not root_config.is_file():
        print(
            f"WARN: --apply set but {root_config} does not exist; "
            "skipping root config update.",
            file=sys.stderr,
        )
        return False
    try:
        old_text = root_config.read_bytes().decode("utf-8")
        new_text, added = insert_rule_text(old_text, rule)
        if not added:
            return False
        expected = _load_yaml_text(new_text)
        _write_preserving(root_config, new_text, old_text, lambda got: got == expected)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        _fail(f"could not add the rule to {root_config}: {exc}")
    return True


# --- emit ----------------------------------------------------------------------


def _now_iso(now: float | None) -> str:
    epoch = time.time() if now is None else now
    moment = _EPOCH + timedelta(seconds=math.floor(epoch))
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_rule(sample: dict[str, Any], rule_id: str | None) -> dict[str, Any]:
    rule: dict[str, Any] = {}
    if rule_id:
        rule["id"] = rule_id
    rule["where"] = sample["where"]
    rule["forbidden"] = [sample["forbidden_pattern"]]
    rule["reason"] = sample["reason"]
    return rule


def _root_hits(
    checker: Any, project_root: Path, rules: list[Any], fixture_root: Path
) -> list[tuple[str, int]]:
    skip: list[str] = [RETRO_FIXTURE_PREFIX]
    try:
        rel_fixtures = fixture_root.resolve().relative_to(project_root.resolve())
        if rel_fixtures.as_posix() not in ("", "."):
            skip.append(rel_fixtures.as_posix().rstrip("/") + "/")
    except ValueError:
        pass
    hits: list[tuple[str, int]] = []
    for violation in checker.find_violations(project_root, rules):
        if any(violation.path.startswith(prefix) for prefix in skip):
            continue
        hits.append((violation.path, violation.line_no))
    return hits


def emit(
    finding: dict[str, Any],
    project_root: Path,
    fixture_root: Path,
    apply_to_root: bool,
    force: bool,
    *,
    rule_id: str | None = None,
    class_key: str | None = None,
    provenance: Provenance | None = None,
    must_pass_root: bool = False,
    now: float | None = None,
) -> EmitResult:
    _check_finding_fields(finding)
    sample = finding["violation_sample"]
    short = _short_id(finding)
    fixture_dir = (fixture_root / short).resolve()

    if rule_id is not None:
        if not _LESSON_ID.fullmatch(rule_id) or rule_id != f"L-{short}":
            _fail(f"--rule-id must be L-{short} (L- plus the finding's short id)")
    if class_key is not None and not _CLASS_KEY.fullmatch(class_key):
        _fail(f"--class-key {class_key!r} is not a class key")

    patch_sha = None
    if provenance is not None:
        patch_sha, family = _check_provenance(sample, provenance)
        rel_sample = PurePosixPath(provenance.path)
        ext = rel_sample.suffix
        content = _SAMPLE_HEADERS[family] + _sample_stub(
            SAMPLE_LANGUAGE[ext], sample["import_line"]
        )
    else:
        language = sample.get("language", "ts")
        ext_name = _LANG_EXT.get(language, language)
        rel_sample = PurePosixPath(_where_to_dir(sample["where"]).as_posix()) / f"sample.{ext_name}"
        content = _sample_stub(language, sample["import_line"])

    sample_file = (fixture_dir / Path(*rel_sample.parts)).resolve()
    if not sample_file.is_relative_to(fixture_dir) or sample_file == fixture_dir:
        _fail(f"sample path {rel_sample.as_posix()!r} leaves the fixture directory")

    root_config = project_root / ".cadence" / "cadence.yaml"
    if apply_to_root and provenance is not None and not root_config.is_file():
        _fail(f"--apply set but {root_config} does not exist")

    if fixture_dir.exists() and not force:
        print(
            f"ERROR: fixture already exists at {fixture_dir}. "
            "Pass --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(EXIT_BAD_INPUT)

    # --- writes start here -----------------------------------------------
    fixture_dir.mkdir(parents=True, exist_ok=True)
    sample_file.parent.mkdir(parents=True, exist_ok=True)
    with sample_file.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)

    rule = _make_rule(sample, rule_id)
    fixture_config = fixture_dir / ".cadence" / "cadence.yaml"
    fixture_config.parent.mkdir(parents=True, exist_ok=True)
    with fixture_config.open("w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(
            {
                "commands": {"test": ["true"]},
                "boundaries": [rule],
            },
            fh,
            sort_keys=False,
        )

    if provenance is not None:
        with (fixture_dir / "finding.json").open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(finding, indent=2) + "\n")
        record = {
            "rule_id": rule_id,
            "class_key": class_key,
            "patch_sha256": patch_sha,
            "path": provenance.path,
            "line_no": provenance.line_no,
            "emitted_at": _now_iso(now),
        }
        with (fixture_dir / "provenance.json").open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(record, indent=2) + "\n")

    checker = _load_checker(project_root)
    rules = checker._load_rules(fixture_config)
    violations = checker.find_violations(fixture_dir, rules)
    sample_rel = sample_file.relative_to(fixture_dir).as_posix()
    fired = any(v.path == sample_rel for v in violations)

    root_violations: list[tuple[str, int]] = []
    if fired and must_pass_root:
        root_violations = _root_hits(checker, project_root, rules, fixture_root)

    applied = False
    if fired and not root_violations and apply_to_root:
        applied = _append_rule_to_root_config(root_config, rule)
        if applied:
            print(f"Appended rule to {root_config}", file=sys.stderr)
        else:
            print(
                f"Equivalent rule already present in {root_config}; "
                "no change.",
                file=sys.stderr,
            )

    return EmitResult(
        fixture_dir=fixture_dir,
        sample_file=sample_file,
        fixture_config=fixture_config,
        rule=rule,
        fired=fired,
        violation_count=len(violations),
        root_violations=tuple(root_violations),
        applied=applied,
    )


# --- retire and replay -----------------------------------------------------------


def retire(project_root: Path, rule_id: str) -> int:
    """Remove the learned rule ``rule_id``. Exit code per the module contract."""
    if not _LESSON_ID.fullmatch(rule_id):
        print(f"ERROR: --retire {rule_id!r} is not a learned rule id (L-xxxxxxxx)", file=sys.stderr)
        return EXIT_BAD_INPUT
    config = project_root / ".cadence" / "cadence.yaml"
    try:
        old_text = config.read_bytes().decode("utf-8")
        new_text = remove_rule_text(old_text, rule_id)
        expected = _load_yaml_text(new_text)
        _write_preserving(config, new_text, old_text, lambda got: got == expected)
    except LookupError:
        print(f"ERROR: no rule with id {rule_id} in {config}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"ERROR: could not retire {rule_id} from {config}: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    print(f"Removed rule {rule_id} from {config}", file=sys.stderr)
    return EXIT_OK


def _fixture_rule_id(config: Path) -> str | None:
    try:
        cfg = _load_yaml_text(config.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if isinstance(cfg, dict):
        rules = cfg.get("boundaries")
        if isinstance(rules, list) and rules and isinstance(rules[0], dict):
            rid = rules[0].get("id")
            if isinstance(rid, str) and _RULE_ID.fullmatch(rid):
                return rid
    return None


def replay(
    project_root: Path, fixture_root: Path, skip_ids: set[str]
) -> list[dict[str, Any]]:
    """Run every fixture under ``fixture_root`` against its own config."""
    results: list[dict[str, Any]] = []
    if not fixture_root.is_dir():
        return results
    checker = _load_checker(project_root)
    for child in sorted(fixture_root.iterdir(), key=lambda p: p.name):
        config = child / ".cadence" / "cadence.yaml"
        if not child.is_dir() or child.is_symlink() or not config.is_file():
            continue
        if child.name in skip_ids:
            continue
        rule_id = _fixture_rule_id(config)
        fired = False
        try:
            rules = checker._load_rules(config)
            violations = checker.find_violations(child, rules)
        except SystemExit:
            violations = []
        target: str | None = None
        prov = child / "provenance.json"
        if prov.is_file():
            try:
                record = json.loads(prov.read_text(encoding="utf-8"))
                path = record.get("path") if isinstance(record, dict) else None
                if isinstance(path, str) and _relative_posix_problem(path, "path") is None:
                    target = path
                if isinstance(record, dict) and rule_id is None:
                    rid = record.get("rule_id")
                    if isinstance(rid, str) and _RULE_ID.fullmatch(rid):
                        rule_id = rid
            except (OSError, ValueError):
                target = None
            fired = target is not None and any(v.path == target for v in violations)
        else:
            fired = any(
                PurePosixPath(v.path).name.startswith("sample.") for v in violations
            )
        results.append({"fixture": child.name, "rule_id": rule_id, "fired": fired})
    return results


# --- CLI -----------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cadence Layer-3 rule emitter "
        "(retro fix → fixture + cadence.yaml + regression check).",
    )
    parser.add_argument(
        "--input",
        type=Path,
        help="Path to JSON finding. If omitted, reads from stdin.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Project root (default: cwd). Used to find tool/check_boundaries.py.",
    )
    parser.add_argument(
        "--fixture-root",
        type=Path,
        default=None,
        help="Root directory for generated fixtures "
        "(default: <project-root>/tests/fixtures/retro).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Also append the new rule to the project's "
        ".cadence/cadence.yaml (idempotent, text-preserving).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing fixture for the same finding id.",
    )
    parser.add_argument("--schema-dir", type=Path, help="where retro.schema.json lives")
    parser.add_argument("--rule-id", help="learned rule id L-xxxxxxxx, written as id")
    parser.add_argument("--class-key", help="class key recorded in provenance.json")
    parser.add_argument("--provenance-patch", type=Path, help="the stored agent patch")
    parser.add_argument("--provenance-path", help="the sample's real repo path")
    parser.add_argument("--provenance-line", type=int, help="new-file line of the sample")
    parser.add_argument(
        "--must-pass-root",
        action="store_true",
        help="exit 3 if the new rule fires anywhere in the project",
    )
    parser.add_argument("--json", action="store_true", help="one JSON line on stdout")
    parser.add_argument("--retire", metavar="ID", help="remove learned rule ID")
    parser.add_argument("--replay", action="store_true", help="re-run every fixture")
    parser.add_argument(
        "--skip-ids", default="", help="with --replay: comma-separated ids to skip"
    )
    parser.add_argument("--now", type=float, help="epoch seconds (default: now)")
    return parser


def _read_finding(path: Path | None) -> Any:
    if path is not None:
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"ERROR: could not read {path}: {exc}", file=sys.stderr)
            return None
    try:
        return json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"ERROR: stdin is not valid JSON: {exc}", file=sys.stderr)
        return None


def _cmd_replay(args: argparse.Namespace, project_root: Path, fixture_root: Path) -> int:
    skip: set[str] = set()
    for raw in (args.skip_ids or "").split(","):
        item = raw.strip()
        if not item:
            continue
        if _LESSON_ID.fullmatch(item):
            skip.add(item[2:])
        elif _FIXTURE_NAME.fullmatch(item):
            skip.add(item)
        else:
            print(f"ERROR: --skip-ids entry {item!r} is not an id", file=sys.stderr)
            return EXIT_BAD_INPUT
    results = replay(project_root, fixture_root, skip)
    for result in results:
        print(json.dumps(result, sort_keys=True))
    if not args.json:
        for result in results:
            state = "fires" if result["fired"] else "DOES NOT FIRE"
            print(f"{result['fixture']}: {state}", file=sys.stderr)
    return EXIT_OK if all(r["fired"] for r in results) else EXIT_NOT_FIRED


def _display_path(path: Path, project_root: Path) -> str:
    try:
        rel = path.resolve().relative_to(project_root.resolve()).as_posix()
        return rel + "/" if path.is_dir() or not path.suffix else rel
    except ValueError:
        return path.as_posix()


def _run(args: argparse.Namespace) -> int:
    project_root: Path = args.project_root.resolve()
    fixture_root: Path = (
        args.fixture_root.resolve()
        if args.fixture_root is not None
        else (project_root / "tests" / "fixtures" / "retro").resolve()
    )

    if args.retire is not None:
        code = retire(project_root, args.retire)
        if args.json:
            print(json.dumps({"retired": args.retire, "exit": code}, sort_keys=True))
        return code
    if args.replay:
        return _cmd_replay(args, project_root, fixture_root)

    finding = _read_finding(args.input)
    if finding is None:
        return EXIT_BAD_INPUT
    if not isinstance(finding, dict):
        print("ERROR: finding must be a JSON object", file=sys.stderr)
        return EXIT_BAD_INPUT

    schema = _load_schema(project_root, args.schema_dir)
    _validate(finding, schema)

    if finding.get("fix_layer") != 3:
        print(
            f"ERROR: emit_rule.py only handles fix_layer=3 findings "
            f"(got {finding.get('fix_layer')!r}). "
            "Layer 1/2/4 fixes stay in FRAMEWORK_CHANGELOG.md only.",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT
    if finding.get("auto_method") != "boundary-rule":
        print(
            f"ERROR: emit_rule.py v0.3.0-rc.1 only handles "
            f"auto_method='boundary-rule' (got {finding.get('auto_method')!r}). "
            "Future emitters will add lint-rule and schema-rule kinds.",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT

    prov_args = (args.provenance_patch, args.provenance_path, args.provenance_line)
    provenance: Provenance | None = None
    if any(a is not None for a in prov_args):
        if not all(a is not None for a in prov_args):
            print(
                "ERROR: --provenance-patch, --provenance-path and "
                "--provenance-line go together",
                file=sys.stderr,
            )
            return EXIT_BAD_INPUT
        provenance = Provenance(
            patch=args.provenance_patch,
            path=args.provenance_path,
            line_no=args.provenance_line,
        )

    fixture_dir = _fixture_dir_for(finding, fixture_root)
    existed = fixture_dir.exists()
    backup: Path | None = None
    if existed and args.force:
        # Move the old fixture aside so a failed re-emission can restore it.
        backup = fixture_dir.parent / f".{fixture_dir.name}.bak-{os.getpid()}"
        if backup.exists():
            shutil.rmtree(backup)
        fixture_dir.rename(backup)

    def _undo() -> None:
        if fixture_dir.exists() and (not existed or backup is not None):
            shutil.rmtree(fixture_dir, ignore_errors=True)
        if backup is not None and backup.exists():
            backup.rename(fixture_dir)

    try:
        result = emit(
            finding=finding,
            project_root=project_root,
            fixture_root=fixture_root,
            apply_to_root=args.apply,
            force=args.force,
            rule_id=args.rule_id,
            class_key=args.class_key,
            provenance=provenance,
            must_pass_root=args.must_pass_root,
            now=args.now,
        )
    except BaseException:  # includes the SystemExit(2) of bad input
        _undo()
        raise

    if not result.fired:
        code = EXIT_NOT_FIRED
    elif result.root_violations:
        code = EXIT_FIRES_ON_ROOT
    else:
        code = EXIT_OK

    if code == EXIT_OK:
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    else:
        _undo()

    out = sys.stderr if args.json else sys.stdout
    print(f"fixture:      {result.fixture_dir}", file=out)
    print(f"sample file:  {result.sample_file}", file=out)
    print(f"new rule:     {json.dumps(result.rule)}", file=out)
    print(f"violations:   {result.violation_count} (rule fired: {result.fired})", file=out)

    if code == EXIT_NOT_FIRED:
        print(
            "\nERROR: the new boundary rule did NOT flag the offending "
            "import. A rule that doesn't fire on its own trigger case is "
            "worse than no rule. Likely causes:\n"
            "  - 'where' glob doesn't match the sample file's path\n"
            "  - 'forbidden_pattern' doesn't catch the import_line "
            "(check word-boundary handling)\n"
            "  - import_line isn't import-shaped per the checker's "
            "_IMPORT_PREFIXES tuple\n"
            "Do NOT record this finding as landed until the rule fires.",
            file=sys.stderr,
        )
    elif code == EXIT_FIRES_ON_ROOT:
        print(
            f"\nERROR: the new rule also fires on the project itself "
            f"({len(result.root_violations)} line(s), e.g. "
            f"{result.root_violations[0][0]}:{result.root_violations[0][1]}). "
            "It would fail main's own verify, so it is not landed.",
            file=sys.stderr,
        )

    if args.json:
        payload = {
            "fixture": _display_path(result.fixture_dir, project_root)
            if code == EXIT_OK
            else None,
            "sample": result.sample_file.relative_to(result.fixture_dir).as_posix(),
            "rule": result.rule,
            "fired": result.fired,
            "violations": result.violation_count,
            "root_violations": [
                {"path": p, "line_no": n} for p, n in result.root_violations[:20]
            ],
            "applied": result.applied,
            "exit": code,
        }
        print(json.dumps(payload, sort_keys=True))
    return code


def main(argv: list[str] | None = None) -> int:
    # Loading the project's checker must not leave tool/__pycache__ behind.
    sys.dont_write_bytecode = True
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except Exception:  # noqa: BLE001 - exit 1 and 3 have meanings; never use them for a crash
        traceback.print_exc()
        print("ERROR: internal error in emit_rule.py (see traceback)", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    sys.exit(main())
