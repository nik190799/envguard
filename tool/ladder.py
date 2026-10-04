#!/usr/bin/env python3
"""Cadence learning ladder: factory evidence in, one rolling retro PR out.

Reads the evidence the factory books on the ``cadence/state`` branch
(observations, findings, decisions) and the lessons already on ``main``
(``.cadence/lessons.yaml``, the ``L-`` rules in ``.cadence/cadence.yaml``
and the learned section of ``docs/PATTERNS.md``), and moves mistake
classes along the ladder note -> pattern -> check, and to retired or
suppressed, as docs/LEARNING.md ("The ladder") specifies. Every decision
comes from code over validated keys, paths and numbers. No model is
involved and no text written by an agent or a human is copied anywhere.

Contract:
    plan     python tool/ladder.py plan --state-dir DIR --repo-root DIR
                 [--config FILE] [--open-plan-sha HEX] [--base-sha SHA40]
                 [--schema-dir DIR] [--emitter PATH] [--now EPOCH] --out FILE
             Recomputes the ladder from the state and the repo, runs
             ``emit_rule.py --replay`` over the retro fixtures and the L-
             rules over the repo (retirement (a) and (b)), and writes
             plan.json (retro-plan.schema.json). Prints
             ``{"changed", "plan_sha", "mode", "transitions",
             "failed_before"}`` where failed_before = the state holds
             ``retro/failed/<plan_sha>.json`` (see "Failed plans"), and
             changed = mode is on or eval-sandbox, the plan has
             transitions, plan_sha differs from --open-plan-sha, and the
             plan has not failed before.
    apply    python tool/ladder.py apply --plan FILE --repo-root DIR
                 --state-dir DIR [--emitter PATH] [--schema-dir DIR]
                 [--now EPOCH] [--verify-failed] --out FILE
             For each check, for its sample and then each of its
             alternates until one lands: ``emit_rule.py --input F
             --rule-id L-.. --class-key K --provenance-patch P
             --provenance-path PATH --provenance-line N --must-pass-root
             --apply --json`` (plus --force when the fixture already
             exists). Exit 1 or 2 (or a patch that fails its checks) moves
             on to the next sample; exit 3 (the rule fires on the repo) or
             a rule that was not appended (an equivalent one exists) stops.
             When no sample lands the check becomes a pattern (reason
             emit-fallback, needs_human emit-failed). The sample that lands
             is the one applied.json records. For each retired check:
             ``emit_rule.py --retire L-..``. Then rewrites
             .cadence/lessons.yaml and the learned section of
             docs/PATTERNS.md, and writes applied.json (never with
             ``alternates``).
             ``--verify-failed`` (scripts/verify.sh failed on the first
             apply's result; the workflow resets the repo and applies the
             same plan again) runs the emitter for no check: a check from
             note or retired becomes a pattern (reason verify-fallback,
             fallback text, no sample), a check from pattern is dropped
             (skipped verify-fallback-no-change), a test: pattern is
             dropped (skipped verify-failed), each with needs_human
             verify-failed. Everything else is applied as usual.
    guard    python tool/ladder.py guard --repo-root DIR
                 (--worktree [--base-ref HEAD] | --patch FILE)
                 [--applied FILE] [--schema-dir DIR]
             The semantic guard on the retro change (see "Guard").
    pr-body  python tool/ladder.py pr-body --applied FILE --repo OWNER/REPO
                 [--metrics FILE] [--run-url URL] [--schema-dir DIR] --out FILE
             A PR body of at most 60000 characters built from fixed
             strings, keys, numbers and links only: no excerpts, no "@",
             no HTML. A plan applied with --verify-failed gets a
             "Demoted after verify failed" section after "Patterns". It
             ends with the line "cadence retro plan <sha12>".

Exit codes:
    plan     0 ok; 2 bad input or an internal error
    apply    0 applied; 1 nothing left to apply; 2 error
    guard    0 ok; 1 violation (reasons on stderr); 2 bad input
    pr-body  0 ok; 2 bad input
    Internal errors never exit 1.

The ladder (all counts recomputed from cadence/state; nothing is stored):
    - An attempt is a build observation whose patch applied (apply_status
      ok, a patch sha, agent not cancelled or skipped). Attempts are unique
      by (repo, issue, patch_sha256); the earliest is kept and it counts as
      published when any duplicate was.
    - An occurrence is a headline class (import-edge, guarded,
      missing-test, test) seen in an attempt inside the window: the last
      ``window_attempts`` attempts or the last ``window_days`` days,
      whichever holds more. An import edge counts only once something
      seeded it: a rule hit, a human-edit, /cadence-forbid or verified
      review-comment finding, an existing lesson, or a rule in the current
      cadence.yaml that covers it. Once seeded, it counts in every attempt.
    - count(k) = distinct issues with an occurrence. A class with count >=
      promote_after is promoted: an import edge to an area that no seed
      rule covers and that has a sample (the newest occurrence with a
      stored patch and a strict import line in TS/JS, Python or Dart)
      becomes a check; everything else with a template becomes a pattern.
      A check carries up to 3 samples (``sample`` plus ``alternates``):
      distinct by (path, import line), newest occurrence first.
      A pattern edge that recurs after its promotion is offered as a check.
    - A retired class re-promotes only on occurrences after its retirement
      date (hysteresis). One that was retired twice is pinned when it comes
      back, and a pinned lesson is never retired for stale, dormant or cap.
    - Rejections come from decisions/: a transition that did not land.
      After one, the class needs 2 new distinct issues before it is
      proposed again; after two, the PR proposes the suppressed rung. A
      merged PR whose check fell back to a pattern does not count as a
      rejection of that check.
    - Checks retire when (d) the rule is gone from cadence.yaml
      (human-removed), (b) the rule fires on main (blocks-merged-code),
      (a) its fixture no longer fires (broken), (c) an area is gone
      (stale), or (e) 0 hits in the last 100 exposed attempts after
      ``since`` and at least 90 days old (dormant; only reported unless
      retire_dormant). Patterns retire when stale, or with 0 recurrences
      in the last 40 exposed attempts and at least 45 days old, or to keep
      max_active_patterns. In eval-sandbox only (a), (b) and (d) apply.
    - Caps per PR: max_checks_per_pr, max_patterns_per_pr and
      max_retirements_per_pr (suppressions count as retirements).
      Candidates rank by count, then escaped occurrences, then most recent.

Guard (exit 1 on any of these):
    - a path outside .cadence/cadence.yaml, .cadence/lessons.yaml,
      docs/PATTERNS.md and tests/fixtures/retro/;
    - any deletion (except files replaced inside a fixture directory whose
      check --applied re-promotes), a symlink, mode 100755 or a submodule;
    - a fixture directory not named by 8 hex digits, holding anything but
      finding.json, provenance.json, .cadence/cadence.yaml (one rule with
      the directory's L- id) and one sample with a TS/JS, Python or Dart
      extension, or a file over 8 KiB; a changed fixture directory with no
      check transition in --applied (when given); a new one with none;
    - cadence.yaml changes outside entries with an L- id, malformed L-
      entries, duplicate ids, or (with --applied) L- ids added or removed
      without a matching transition;
    - docs/PATTERNS.md changes outside the learned section, or a section
      that is not exactly what lessons.yaml renders;
    - a lessons.yaml that fails lessons.schema.json or its id rules;
    - an applied.json that fails retro-plan.schema.json, or that still
      lists alternates;
    - a patch over 256 KiB.
    ``--worktree`` compares the working tree (tracked and untracked, as
    ``git add -A`` would stage it, through a temporary index) with
    --base-ref, ignoring only .cadence/.last_verify_ok,
    .cadence/.last_verify_sha and .cadence/last_verify.log. ``--patch``
    applies the patch with ``git apply --index`` in a temporary detached
    worktree of HEAD, checks it there, and always removes the worktree.

Failed plans:
    When scripts/verify.sh fails on a retro result even with every check
    demoted, the workflow's retro-failed job records
    ``retro/failed/<plan_sha>.json`` on cadence/state:
    ``{"schema": "cadence.retro-failed/1", "plan_sha", "base_sha",
    "run_id", "run_attempt", "recorded_at", "reason": "verify-failed"}``.
    ``plan`` reads that one path directly (it is not part of
    STATE_PATH_RE): a regular file of at most 4096 bytes whose schema and
    plan_sha match marks the plan as failed_before, and changed is then
    false. Anything else there is ignored with a warning. plan_sha covers
    base_sha and the transitions (class key, rung, reason, samples), so a
    new commit on main or a different proposal is tried again.

Design notes:
    - State files are read only when their path matches STATE_PATH_RE and
      they pass their schema (when the schema is installed) and the code
      checks here; others count as unreadable. Nothing from the state
      branch is executed.
    - Text comes only from fixed templates over validated keys, paths and
      issue numbers.
    - emit_rule.py runs as ``python -I -B`` in a subprocess; the boundary
      checker and ledger.py are loaded from this file's directory. No
      bytecode is written, so no tool/__pycache__ appears in the repo for
      the guard to refuse.
    - A plan with only needs_human entries has no transitions, so it opens
      no PR (changed is false).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Iterable, Sequence

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
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

NS_CADENCE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/nik190799/cadence#factory")
CLASS_KEY_RE = (
    r"^(import-edge|guarded|missing-test|test|edit|review|gate|agent|pr):"
    r"[A-Za-z0-9_./@+:>-]{1,180}$"
)
HEADLINE_FAMILIES = ("import-edge", "guarded", "missing-test", "test")
POST_PR_FAMILIES = ("edit", "review")
OPS_FAMILIES = ("gate", "agent", "pr")
RULE_ID_RE = r"^[LB]-[0-9a-f]{8}$"
LESSON_ID_RE = r"^L-[0-9a-f]{8}$"
AREA_SEG_RE = r"^[A-Za-z0-9_@+-][A-Za-z0-9_.@+-]{0,63}$"
PKG_RE = r"^(@[A-Za-z0-9][A-Za-z0-9._-]{0,63}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
STATE_PATH_RE = (
    r"^(?:(?:runs|observations|findings|patches|prs|harvest|decisions|learn|reports)"
    r"/[A-Za-z0-9._-]{1,140}\.(?:json|jsonl|patch)|retro/plans/[0-9a-f]{64}\.json)$"
)
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
RETRO_FIXTURE_PREFIX = "tests/fixtures/retro/"
RETRO_ALLOWLIST = (".cadence/cadence.yaml", ".cadence/lessons.yaml", "docs/PATTERNS.md")
LEARNED_SECTION_HEADING = "## Learned patterns (factory)"


def lesson_id(class_key: str) -> str:
    """``L-`` + the first 8 hex digits of the class's lesson UUID."""
    return "L-" + uuid.uuid5(NS_CADENCE, "lesson|" + class_key).hex[:8]


def lesson_finding_id(class_key: str) -> str:
    """The finding id ``apply`` hands to emit_rule.py (its short id is the hex8)."""
    return str(uuid.uuid5(NS_CADENCE, "lesson|" + class_key))


def seed_rule_id(where: str, forbidden: Sequence[str]) -> str:
    """The id of a cadence.yaml rule that has no explicit ``id``."""
    digest = hashlib.sha256((where + "|" + "|".join(forbidden)).encode("utf-8"))
    return "B-" + digest.hexdigest()[:8]


def family_of(key: str) -> str:
    return key.split(":", 1)[0]


def parse_edge_key(key: str) -> tuple[str, str] | None:
    """(from_area, to) of ``import-edge:<from>-><to>``, split at the first ``>``."""
    if not key.startswith("import-edge:"):
        return None
    body = key[len("import-edge:") :]
    i = body.find(">")
    if i < 2 or body[i - 1] != "-":
        return None
    frm, to = body[: i - 1], body[i + 1 :]
    if not frm or not to:
        return None
    return frm, to


def area(path: str, depth: int) -> str | None:
    """The first ``depth`` directory segments of ``path``; "." at the root."""
    directory = posixpath.dirname(path)
    segments = [s for s in directory.split("/") if s]
    if not segments:
        return "."
    if any(not _AREA_SEG.fullmatch(s) for s in segments):
        return None
    return "/".join(segments[:depth])


# --- Local constants -----------------------------------------------------------

_CLASS_KEY = re.compile(CLASS_KEY_RE)
_RULE_ID = re.compile(RULE_ID_RE)
_LESSON_ID = re.compile(LESSON_ID_RE)
_AREA_SEG = re.compile(AREA_SEG_RE)
_STATE_PATH = re.compile(STATE_PATH_RE)
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_.@+/-]{1,300}$")
_SAFE_GLOB = re.compile(r"^[A-Za-z0-9_.@*-][A-Za-z0-9_.@/*-]{0,199}$")
# A guarded path or test root (learning.guarded_paths, learning.test_roots):
# a relative directory path of 1 to 6 segments, none "." or ".."; the same
# shape as ledger.GUARDED_PATH_RE (tests/test_ladder.py checks).
_ROOT_PATH = re.compile(r"^[A-Za-z0-9_.-]{1,64}(?:/[A-Za-z0-9_.-]{1,64}){0,5}$")
_FIXTURE_NAME = re.compile(r"^[0-9a-f]{8}$")
_WHY = re.compile(r"^[a-z0-9-]{1,40}$")
_NEEDS_KEY = re.compile(r"^[A-Za-z0-9_./@+:>-]{1,200}$")

INTRINSIC_FAMILIES = ("guarded", "missing-test", "test")
SEED_SIGNALS = ("human-edit", "reviewer-command", "review-comment")
RUNGS = ("pattern", "check", "retired", "suppressed")
NEEDS_WHY = (
    "needs-words",
    "seed-rule-on-main",
    "package-edge",
    "unsupported-language",
    "emit-failed",
    "regressed",
    "verify-failed",
)
RETIRE_REASONS = ("broken", "blocks-merged-code", "stale", "dormant", "cap", "human-removed")
# Lower is more urgent: the order retirements are kept under the cap.
_RETIRE_SEVERITY = {
    "human-removed": 0,
    "blocks-merged-code": 1,
    "broken": 2,
    "stale": 3,
    "dormant": 4,
    "cap": 5,
    "rejected-twice": 6,
}
_TO_ORDER = {"check": 0, "pattern": 1, "suppressed": 2, "retired": 3}

MAX_PATCH_BYTES = 524288
MAX_JSON_BYTES = 262144
MAX_RETRO_PATCH_BYTES = 256 * 1024
MAX_FIXTURE_FILE_BYTES = 8 * 1024
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_PR_BODY = 60000
CHECK_DORMANT_ATTEMPTS = 100
CHECK_DORMANT_DAYS = 90
PATTERN_DORMANT_ATTEMPTS = 40
PATTERN_DORMANT_DAYS = 45
MAX_SAMPLES = 3  # a check's sample plus at most 2 alternates
FAILED_PLAN_SCHEMA = "cadence.retro-failed/1"
MAX_FAILED_RECORD_BYTES = 4096
# skipped[].why of the entries a --verify-failed apply adds.
VERIFY_SKIP_WHY = ("verify-failed", "verify-fallback-no-change")

LESSONS_HEADER = (
    "# Generated by tool/ladder.py from factory runs. Change it through the "
    "retro PR; see docs/LEARNING.md.\n"
)
SECTION_INTRO = (
    "Generated from factory runs by `tool/ladder.py`; change it through the "
    "retro PR. See docs/LEARNING.md."
)
VERIFY_MARKERS = (
    ".cadence/.last_verify_ok",
    ".cadence/.last_verify_sha",
    ".cadence/last_verify.log",
)

EXIT_OK = 0
EXIT_NOTHING = 1
EXIT_VIOLATION = 1
EXIT_BAD_INPUT = 2

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TOOL_DIR = Path(__file__).resolve().parent


class LadderError(Exception):
    """Bad input or an unusable environment. ``main`` exits 2."""


# --- Small helpers ----------------------------------------------------------------


def to_utc(epoch: float) -> datetime:
    try:
        return _EPOCH + timedelta(seconds=math.floor(epoch))
    except (OverflowError, ValueError) as exc:
        raise LadderError(f"--now {epoch!r} is not a usable timestamp") from exc


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> datetime | None:
    """An RFC 3339 timestamp as an aware UTC datetime, or None."""
    if not isinstance(value, str) or not _TS.fullmatch(value):
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(raw).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _is_int(value: Any, minimum: int | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return minimum is None or value >= minimum


def _need(condition: Any, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _glob_ok(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(_SAFE_GLOB.fullmatch(value))
        and not value.startswith("/")
        and ".." not in value.split("/")
        and not value.startswith("tests/fixtures/")
    )


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, indent=2) + "\n")


def _read_json(path: Path, limit: int = MAX_JSON_BYTES) -> Any:
    try:
        if path.is_symlink() or not path.is_file():
            raise LadderError(f"{path} is not a regular file")
        data = path.read_bytes()
    except OSError as exc:
        raise LadderError(f"could not read {path}: {exc}") from exc
    if len(data) > limit:
        raise LadderError(f"{path} is larger than {limit} bytes")
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise LadderError(f"{path} is not valid JSON: {exc}") from exc


def _load_yaml(text: str, source: str) -> Any:
    try:
        return yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise LadderError(f"malformed YAML in {source}: {exc}") from exc


def _dates_to_str(obj: Any) -> Any:
    """YAML turns unquoted dates into date objects; the schema wants strings."""
    if isinstance(obj, dict):
        return {k: _dates_to_str(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dates_to_str(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.date().isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


_SIBLINGS: dict[str, Any] = {}


def _sibling(name: str) -> Any:
    """Load ``<name>.py`` from this file's directory (works under ``python -I``)."""
    if name in _SIBLINGS:
        return _SIBLINGS[name]
    path = _TOOL_DIR / f"{name}.py"
    if not path.is_file():
        raise LadderError(f"{name}.py not found next to ladder.py ({_TOOL_DIR})")
    module_name = f"_cadence_ladder_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise LadderError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _SIBLINGS[name] = module
    return module


def line_ok(family: str, line: str) -> bool:
    """True if ``line`` is one strict import statement (emit_rule's patterns)."""
    return bool(_sibling("emit_rule").import_line_ok(family, line))


# --- Settings ----------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """The learning keys the ladder and metrics read (section 6 defaults)."""

    mode: str = "on"
    promote_after: int = 2
    window_attempts: int = 50
    window_days: int = 60
    repeat_window: int = 10
    area_depth: int = 2
    max_checks_per_pr: int = 3
    max_patterns_per_pr: int = 5
    max_retirements_per_pr: int = 5
    max_active_patterns: int = 25
    retire_dormant: bool = False
    test_roots: tuple[str, ...] = ("tests", "test")


_POSITIVE = ("promote_after", "window_attempts", "window_days")
_COUNTS = (
    "repeat_window",
    "max_checks_per_pr",
    "max_patterns_per_pr",
    "max_retirements_per_pr",
    "max_active_patterns",
)


def _root_path(value: Any) -> bool:
    """A guarded path or test root: ``_ROOT_PATH`` with no "." or ".." segment."""
    return (
        isinstance(value, str)
        and bool(_ROOT_PATH.fullmatch(value))
        and not any(segment in (".", "..") for segment in value.split("/"))
    )


def settings_from(cfg: Any) -> Settings:
    """Settings from any object with the learning keys as attributes."""
    defaults = Settings()
    values: dict[str, Any] = {}
    for f in fields(Settings):
        values[f.name] = getattr(cfg, f.name, getattr(defaults, f.name))
    if values["mode"] not in ("observe", "on", "eval-sandbox"):
        raise LadderError(f"learning.mode must be observe, on or eval-sandbox (got {values['mode']!r})")
    for name in _POSITIVE:
        if not _is_int(values[name], 1):
            raise LadderError(f"learning.{name} must be an integer >= 1")
    for name in _COUNTS:
        if not _is_int(values[name], 0):
            raise LadderError(f"learning.{name} must be an integer >= 0")
    if not _is_int(values["area_depth"], 1) or values["area_depth"] > 4:
        raise LadderError("learning.area_depth must be 1..4")
    if not isinstance(values["retire_dormant"], bool):
        raise LadderError("learning.retire_dormant must be true or false")
    roots = values["test_roots"]
    if isinstance(roots, str) or not all(_root_path(r) for r in roots):
        raise LadderError("learning.test_roots must be a list of relative directory paths")
    values["test_roots"] = tuple(roots)
    return Settings(**values)


def load_settings(path: Path | None, *, explicit: bool = False) -> Settings:
    """The effective learning settings of ``factory.yaml`` at ``path``.

    Uses ``ledger.load_learning`` (which validates the whole block). A
    missing default file gives the defaults; a missing explicit one is an
    error.
    """
    if path is None or not path.is_file():
        if explicit:
            raise LadderError(f"config not found: {path}")
        print("WARN: no factory.yaml; using the default learning settings", file=sys.stderr)
        return Settings()
    ledger = _sibling("ledger")
    loader = getattr(ledger, "load_learning", None)
    if loader is not None:
        error = getattr(ledger, "LedgerError", ValueError)
        try:
            return settings_from(loader(path))
        except error as exc:
            raise LadderError(str(exc)) from exc
    # ledger.py without load_learning (older install): read the block here.
    try:
        raw = _load_yaml(path.read_text(encoding="utf-8-sig"), str(path))
    except OSError as exc:
        raise LadderError(f"could not read {path}: {exc}") from exc
    learning = raw.get("learning") if isinstance(raw, dict) else None
    if learning is None:
        return Settings()
    if not isinstance(learning, dict):
        raise LadderError(f"{path}: learning must be a mapping")
    names = {f.name for f in fields(Settings)}
    return settings_from(SimpleNamespace(**{k: v for k, v in learning.items() if k in names}))


# --- Schemas ----------------------------------------------------------------------


class Schemas:
    """Finds and caches schema validators (section 0, "Schema lookup")."""

    def __init__(self, repo_root: Path | None, schema_dir: Path | None) -> None:
        self.repo_root = repo_root
        self.schema_dir = schema_dir
        self._cache: dict[str, Draft202012Validator | None] = {}
        self._warned: set[str] = set()

    def path(self, name: str) -> Path | None:
        if self.schema_dir is not None:
            candidate = self.schema_dir / name
            return candidate if candidate.is_file() else None
        bases: list[Path] = []
        if self.repo_root is not None:
            bases += [self.repo_root / ".cadence", self.repo_root / "plugins" / "cadence" / "schemas"]
        # Installed: <repo>/tool/ladder.py next to <repo>/.cadence/. In the
        # Cadence repo itself: plugins/cadence/templates/tool -> plugins/cadence/schemas.
        bases.append(_TOOL_DIR.parent / ".cadence")
        if _TOOL_DIR.parent.name == "templates":
            bases.append(_TOOL_DIR.parent.parent / "schemas")
        for base in bases:
            candidate = base / name
            if candidate.is_file():
                return candidate
        return None

    def validator(self, name: str, *, required: bool) -> Draft202012Validator | None:
        if name not in self._cache:
            path = self.path(name)
            if path is None:
                self._cache[name] = None
            else:
                schema = _read_json(path, MAX_TEXT_FILE_BYTES)
                self._cache[name] = Draft202012Validator(
                    schema, format_checker=Draft202012Validator.FORMAT_CHECKER
                )
        found = self._cache[name]
        if found is None:
            if required:
                raise LadderError(f"schema {name} not found (.cadence/ or --schema-dir)")
            if name not in self._warned:
                self._warned.add(name)
                print(f"WARN: schema {name} not found; using the built-in checks only", file=sys.stderr)
        return found

    def errors(self, name: str, instance: Any, *, required: bool) -> list[str]:
        validator = self.validator(name, required=required)
        if validator is None:
            return []
        out = []
        for err in sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path)):
            loc = ".".join(str(p) for p in err.absolute_path) or "<root>"
            out.append(f"{loc}: {err.message}"[:300])
        return out


# --- State ------------------------------------------------------------------------

STATE_DIRS = (
    "runs",
    "observations",
    "findings",
    "patches",
    "prs",
    "harvest",
    "decisions",
    "learn",
    "reports",
)
_RESULTS = ("success", "failure", "cancelled", "skipped")
_APPLY = ("ok", "failed", "empty", "missing")
_GATE = ("none", "format", "lint", "boundaries", "test", "apply", "empty", "policy", "timeout", "unknown")
_EDGE_KINDS = ("relative", "package", "python", "dart")
_SIGNALS = (
    "gate",
    "guarded",
    "detector",
    "human-edit",
    "review-comment",
    "reviewer-command",
    "pr-outcome",
)


@dataclass(frozen=True)
class FileChange:
    path: str
    op: str
    area: str | None
    test: bool
    source: bool


@dataclass(frozen=True)
class Edge:
    path: str
    line_no: int
    from_area: str
    to: str
    kind: str
    key: str
    line: str | None


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    path: str
    line_no: int
    key: str | None


@dataclass(frozen=True)
class GuardedOp:
    root: str
    op: str
    path: str


@dataclass(frozen=True)
class Observation:
    run: str
    run_id: str
    run_attempt: int
    repo: str
    issue: int
    base_sha: str
    completed_at: datetime
    patch_sha256: str | None
    apply_status: str
    agent_result: str
    verify_result: str
    gate_step: str
    published: bool
    pr: int | None
    files: tuple[FileChange, ...]
    edges: tuple[Edge, ...]
    guarded: tuple[GuardedOp, ...]
    rule_hits: tuple[RuleHit, ...]
    failing_tests: tuple[str, ...]
    # Informational (metrics.py's lessons_cited block only): the active
    # lessons the approved spec cited; None when unknown or not recorded.
    lessons_cited: tuple[str, ...] | None = None

    @property
    def is_attempt(self) -> bool:
        return (
            self.apply_status == "ok"
            and self.patch_sha256 is not None
            and self.agent_result not in ("cancelled", "skipped")
        )


@dataclass
class Attempt:
    """One scored attempt: the earliest of its duplicates by (repo, issue, patch)."""

    obs: Observation
    runs: list[str]
    published: bool

    @property
    def run(self) -> str:
        return self.obs.run

    @property
    def issue(self) -> int:
        return self.obs.issue

    @property
    def repo(self) -> str:
        return self.obs.repo

    @property
    def completed_at(self) -> datetime:
        return self.obs.completed_at


@dataclass(frozen=True)
class Finding:
    id: str
    ts: datetime
    class_key: str
    family: str
    signal: str
    trust: str
    phase: str
    repo: str
    issue: int
    pr: int | None
    run: str
    rule_id: str | None
    path: str | None
    line_no: int | None


@dataclass
class State:
    observations: list[Observation] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    ops: int = 0
    deduped: int = 0
    findings: dict[str, list[Finding]] = field(default_factory=dict)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    prs: list[dict[str, Any]] = field(default_factory=list)
    harvest: dict[int, dict[str, Any]] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    patches: set[str] = field(default_factory=set)
    unreadable: int = 0

    def pre_gate_findings(self) -> Iterable[Finding]:
        for stem, items in self.findings.items():
            if not stem.startswith("pr-"):
                yield from items

    def post_pr_findings(self) -> Iterable[Finding]:
        for stem, items in self.findings.items():
            if stem.startswith("pr-"):
                yield from items

    def all_findings(self) -> Iterable[Finding]:
        for items in self.findings.values():
            yield from items


def _str_path(value: Any, limit: int = 300) -> str:
    _need(isinstance(value, str) and 1 <= len(value) <= limit, "path")
    _need(not any(ord(ch) < 32 or ord(ch) == 127 for ch in value), "path has control characters")
    return value


def parse_observation(raw: Any, stem: str | None = None) -> Observation:
    """Code checks of one observation (contract 3b). Raises ValueError."""
    _need(isinstance(raw, dict) and raw.get("schema") == "cadence.observation/1", "schema")
    repo = raw.get("repo")
    _need(isinstance(repo, str) and _REPO.fullmatch(repo), "repo")
    issue = raw.get("issue")
    _need(_is_int(issue, 1), "issue")
    run_id = raw.get("run_id")
    _need(isinstance(run_id, str) and _RUN_ID.fullmatch(run_id), "run_id")
    attempt = raw.get("run_attempt")
    _need(_is_int(attempt, 1), "run_attempt")
    run = f"{run_id}-{attempt}"
    _need(stem is None or stem == run, "file name does not match run_id-run_attempt")
    base = raw.get("base_sha")
    _need(isinstance(base, str) and _SHA1.fullmatch(base), "base_sha")
    completed = parse_ts(raw.get("completed_at"))
    _need(completed is not None, "completed_at")
    patch_sha = raw.get("patch_sha256")
    _need(patch_sha is None or (isinstance(patch_sha, str) and _SHA256.fullmatch(patch_sha)), "patch_sha256")
    _need(raw.get("apply_status") in _APPLY, "apply_status")
    _need(raw.get("agent_result") in _RESULTS, "agent_result")
    _need(raw.get("verify_result") in _RESULTS, "verify_result")
    _need(raw.get("gate_step") in _GATE, "gate_step")
    _need(isinstance(raw.get("published"), bool), "published")
    pr = raw.get("pr")
    _need(pr is None or _is_int(pr, 1), "pr")
    evidence = raw.get("evidence")
    _need(isinstance(evidence, dict), "evidence")

    def items(name: str) -> list[Any]:
        value = evidence.get(name)
        _need(isinstance(value, list) and len(value) <= 200, f"evidence.{name}")
        for item in value:
            _need(isinstance(item, dict), f"evidence.{name} item")
        return value

    files = []
    for item in items("files"):
        _need(item.get("op") in ("A", "M", "D"), "files.op")
        file_area = item.get("area")
        _need(file_area is None or (isinstance(file_area, str) and len(file_area) <= 130), "files.area")
        _need(isinstance(item.get("test"), bool) and isinstance(item.get("source"), bool), "files flags")
        files.append(FileChange(_str_path(item.get("path")), item["op"], file_area, item["test"], item["source"]))
    edges = []
    for item in items("import_edges"):
        path = item.get("path")
        _need(isinstance(path, str) and _SAFE_PATH.fullmatch(path), "import_edges.path")
        _need(_is_int(item.get("line_no"), 1), "import_edges.line_no")
        frm, to = item.get("from_area"), item.get("to")
        _need(isinstance(frm, str) and isinstance(to, str), "import_edges.from_area/to")
        key = item.get("key")
        _need(isinstance(key, str) and _CLASS_KEY.fullmatch(key), "import_edges.key")
        _need(key == f"import-edge:{frm}->{to}" and parse_edge_key(key) == (frm, to), "import_edges.key mismatch")
        _need(item.get("kind") in _EDGE_KINDS, "import_edges.kind")
        line = item.get("line")
        _need(line is None or (isinstance(line, str) and len(line) <= 200), "import_edges.line")
        edges.append(Edge(path, item["line_no"], frm, to, item["kind"], key, line))
    guarded = []
    for item in items("guarded"):
        root = item.get("root")
        _need(_root_path(root), "guarded.root")
        _need(item.get("op") in ("add", "modify", "delete"), "guarded.op")
        guarded.append(GuardedOp(root, item["op"], _str_path(item.get("path"))))
    hits = []
    for item in items("rule_hits"):
        rid = item.get("rule_id")
        _need(isinstance(rid, str) and _RULE_ID.fullmatch(rid), "rule_hits.rule_id")
        path = item.get("path")
        _need(isinstance(path, str) and _SAFE_PATH.fullmatch(path), "rule_hits.path")
        _need(_is_int(item.get("line_no"), 1), "rule_hits.line_no")
        key = item.get("key")
        _need(key is None or (isinstance(key, str) and _CLASS_KEY.fullmatch(key)), "rule_hits.key")
        hits.append(RuleHit(rid, path, item["line_no"], key))
    tests = []
    for item in items("failing_tests"):
        path = item.get("path")
        _need(isinstance(path, str) and _SAFE_PATH.fullmatch(path), "failing_tests.path")
        tests.append(path)
    cited = raw.get("lessons_cited")  # optional; absent or null means unknown
    _need(
        cited is None
        or (
            isinstance(cited, list)
            and len(cited) <= 200
            and all(isinstance(lid, str) and _LESSON_ID.fullmatch(lid) for lid in cited)
        ),
        "lessons_cited",
    )
    return Observation(
        run=run,
        run_id=run_id,
        run_attempt=attempt,
        repo=repo,
        issue=issue,
        base_sha=base,
        completed_at=completed,
        patch_sha256=patch_sha,
        apply_status=raw["apply_status"],
        agent_result=raw["agent_result"],
        verify_result=raw["verify_result"],
        gate_step=raw["gate_step"],
        published=raw["published"],
        pr=pr,
        files=tuple(files),
        edges=tuple(edges),
        guarded=tuple(guarded),
        rule_hits=tuple(hits),
        failing_tests=tuple(tests),
        lessons_cited=None if cited is None else tuple(sorted(set(cited))),
    )


def parse_finding(raw: Any) -> Finding | None:
    """A factory finding, or None for a manual one (no ``factory``). Raises ValueError."""
    _need(isinstance(raw, dict), "finding")
    fac = raw.get("factory")
    if fac is None:
        return None
    try:
        fid = str(uuid.UUID(str(raw.get("id"))))
    except ValueError as exc:
        raise ValueError("id") from exc
    ts = parse_ts(raw.get("ts"))
    _need(ts is not None, "ts")
    _need(isinstance(fac, dict), "factory")
    key = fac.get("class_key")
    _need(isinstance(key, str) and _CLASS_KEY.fullmatch(key), "class_key")
    _need(fac.get("family") == family_of(key), "family")
    _need(fac.get("signal") in _SIGNALS, "signal")
    _need(fac.get("trust") in ("A", "B", "C"), "trust")
    _need(fac.get("phase") in ("pre-gate", "post-pr"), "phase")
    repo = fac.get("repo")
    _need(isinstance(repo, str) and _REPO.fullmatch(repo), "repo")
    _need(_is_int(fac.get("issue"), 1), "issue")
    pr = fac.get("pr")
    _need(pr is None or _is_int(pr, 1), "pr")
    run_id = fac.get("run_id")
    _need(isinstance(run_id, str) and _RUN_ID.fullmatch(run_id), "run_id")
    _need(_is_int(fac.get("run_attempt"), 1), "run_attempt")
    rule = fac.get("rule_id")
    _need(rule is None or (isinstance(rule, str) and _RULE_ID.fullmatch(rule)), "rule_id")
    path = fac.get("path")
    _need(path is None or (isinstance(path, str) and _SAFE_PATH.fullmatch(path)), "path")
    line_no = fac.get("line_no")
    _need(line_no is None or _is_int(line_no, 1), "line_no")
    return Finding(
        id=fid,
        ts=ts,
        class_key=key,
        family=fac["family"],
        signal=fac["signal"],
        trust=fac["trust"],
        phase=fac["phase"],
        repo=repo,
        issue=fac["issue"],
        pr=pr,
        run=f"{run_id}-{fac['run_attempt']}",
        rule_id=rule,
        path=path,
        line_no=line_no,
    )


def _parse_run(raw: Any, stem: str) -> dict[str, Any]:
    _need(isinstance(raw, dict), "run record")
    run_id = raw.get("run_id")
    _need(isinstance(run_id, str) and _RUN_ID.fullmatch(run_id), "run_id")
    _need(_is_int(raw.get("run_attempt"), 1), "run_attempt")
    _need(f"{run_id}-{raw['run_attempt']}" == stem, "file name")
    booked = raw.get("booked_usd")
    _need(
        isinstance(booked, (int, float)) and not isinstance(booked, bool) and math.isfinite(booked) and booked >= 0,
        "booked_usd",
    )
    recorded = parse_ts(raw.get("recorded_at"))
    _need(recorded is not None, "recorded_at")
    stage = raw.get("stage")
    _need(stage is None or stage in ("spec", "build", "learn"), "stage")
    issue = raw.get("issue")
    _need(issue is None or _is_int(issue, 1), "issue")
    return {
        "run": stem,
        "run_id": run_id,
        "stage": stage,
        "issue": issue,
        "outcome": raw.get("outcome"),
        "booked_usd": float(booked),
        "recorded": recorded,
    }


def _parse_pr_record(raw: Any) -> dict[str, Any]:
    _need(isinstance(raw, dict) and raw.get("schema") == "cadence.pr/1", "schema")
    _need(_is_int(raw.get("pr"), 1) and _is_int(raw.get("issue"), 1), "pr/issue")
    run_id = raw.get("run_id")
    _need(isinstance(run_id, str) and _RUN_ID.fullmatch(run_id), "run_id")
    _need(_is_int(raw.get("run_attempt"), 1), "run_attempt")
    recorded = parse_ts(raw.get("recorded_at"))
    _need(recorded is not None, "recorded_at")
    return {
        "pr": raw["pr"],
        "issue": raw["issue"],
        "run": f"{run_id}-{raw['run_attempt']}",
        "recorded": recorded,
    }


def _parse_harvest(raw: Any, stem: str) -> dict[str, Any]:
    _need(isinstance(raw, dict) and raw.get("schema") == "cadence.harvest/1", "schema")
    _need(_is_int(raw.get("pr"), 1) and stem == f"pr-{raw['pr']}", "pr")
    issue = raw.get("issue")
    _need(issue is None or _is_int(issue, 1), "issue")
    _need(isinstance(raw.get("merged"), bool), "merged")
    closed = parse_ts(raw.get("closed_at"))
    _need(closed is not None, "closed_at")
    _need(raw.get("status") in ("ok", "no-record"), "status")
    return {
        "pr": raw["pr"],
        "issue": issue,
        "merged": raw["merged"],
        "closed": closed,
        "status": raw["status"],
    }


def _parse_decision(raw: Any, stem: str) -> dict[str, Any]:
    _need(isinstance(raw, dict) and raw.get("schema") == "cadence.decision/1", "schema")
    _need(_is_int(raw.get("pr"), 1) and stem == f"retro-pr-{raw['pr']}", "pr")
    _need(isinstance(raw.get("merged"), bool), "merged")
    closed = parse_ts(raw.get("closed_at"))
    _need(closed is not None, "closed_at")
    plan_sha = raw.get("plan_sha")
    _need(plan_sha is None or (isinstance(plan_sha, str) and _SHA256.fullmatch(plan_sha)), "plan_sha")
    transitions = raw.get("transitions")
    _need(isinstance(transitions, list) and len(transitions) <= 50, "transitions")
    out = []
    for t in transitions:
        _need(isinstance(t, dict), "transition")
        key = t.get("class_key")
        _need(isinstance(key, str) and _CLASS_KEY.fullmatch(key), "class_key")
        _need(t.get("to") in RUNGS, "to")
        _need(isinstance(t.get("landed"), bool), "landed")
        out.append({"class_key": key, "to": t["to"], "landed": t["landed"]})
    return {
        "pr": raw["pr"],
        "merged": raw["merged"],
        "closed": closed,
        "plan_sha": plan_sha,
        "transitions": out,
    }


def read_state(state_dir: Path, schemas: Schemas) -> State:
    """Everything the ladder and metrics read from an extracted cadence/state."""
    state = State()
    if not state_dir.is_dir():
        raise LadderError(f"state dir {state_dir} is not a directory")
    entries: list[tuple[str, Path]] = []
    for name in STATE_DIRS:
        directory = state_dir / name
        if directory.is_dir() and not directory.is_symlink():
            entries += [(f"{name}/{p.name}", p) for p in sorted(directory.iterdir())]
    plans_dir = state_dir / "retro" / "plans"
    if plans_dir.is_dir() and not plans_dir.is_symlink():
        entries += [(f"retro/plans/{p.name}", p) for p in sorted(plans_dir.iterdir())]

    observations: list[Observation] = []
    for rel, path in entries:
        if not _STATE_PATH.fullmatch(rel) or path.is_symlink() or not path.is_file():
            state.unreadable += 1
            continue
        top = rel.split("/", 1)[0]
        stem = PurePosixPath(rel).stem
        try:
            size = path.stat().st_size
            if top == "patches":
                _need(rel.endswith(".patch") and size <= MAX_PATCH_BYTES, "patch")
                state.patches.add(rel)
                continue
            _need(size <= MAX_JSON_BYTES, "size")
            text = path.read_bytes().decode("utf-8")
            if top == "findings":
                _need(rel.endswith(".jsonl"), "findings extension")
                parsed: list[Finding] = []
                for line in text.split("\n"):
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    _need(not schemas.errors("retro.schema.json", raw, required=False), "retro schema")
                    finding = parse_finding(raw)
                    if finding is not None:
                        parsed.append(finding)
                _need(len(parsed) <= 50, "too many findings")
                state.findings[stem] = parsed
                continue
            _need(rel.endswith(".json"), "extension")
            raw = json.loads(text)
            if top == "observations":
                _need(not schemas.errors("observation.schema.json", raw, required=False), "observation schema")
                observations.append(parse_observation(raw, stem))
            elif top == "runs":
                state.runs[stem] = _parse_run(raw, stem)
            elif top == "prs":
                state.prs.append(_parse_pr_record(raw))
            elif top == "harvest":
                record = _parse_harvest(raw, stem)
                state.harvest[record["pr"]] = record
            elif top == "decisions":
                state.decisions.append(_parse_decision(raw, stem))
            elif top == "retro":
                _need(not schemas.errors("retro-plan.schema.json", raw, required=False), "plan schema")
                _need(isinstance(raw, dict) and raw.get("plan_sha") == stem, "plan_sha")
                state.plans[stem] = raw
            # learn/ and reports/ are not read here.
        except (OSError, UnicodeDecodeError, ValueError, RecursionError, LadderError):
            state.unreadable += 1

    observations.sort(key=lambda o: (o.completed_at, o.run))
    state.observations = observations
    state.attempts, state.ops, state.deduped = dedupe_attempts(observations)
    return state


def dedupe_attempts(observations: Iterable[Observation]) -> tuple[list[Attempt], int, int]:
    """(attempts, ops, collapsed duplicates). Attempts are unique by
    (repo, issue, patch_sha256); the earliest is kept, published if any was."""
    groups: dict[tuple[str, int, str], Attempt] = {}
    ops = deduped = 0
    for obs in sorted(observations, key=lambda o: (o.completed_at, o.run)):
        if not obs.is_attempt:
            ops += 1
            continue
        key = (obs.repo, obs.issue, obs.patch_sha256 or "")
        if key in groups:
            group = groups[key]
            group.runs.append(obs.run)
            group.published = group.published or obs.published
            deduped += 1
        else:
            groups[key] = Attempt(obs=obs, runs=[obs.run], published=obs.published)
    attempts = sorted(groups.values(), key=lambda a: (a.completed_at, a.run))
    return attempts, ops, deduped


# --- Detectors (shared with metrics.py) ------------------------------------------------


@dataclass(frozen=True)
class DetectorSet:
    """Which classes count: seeded import edges plus intrinsic families."""

    kind: str
    edges: frozenset[str]
    families: frozenset[str]

    def sha256(self) -> str:
        payload = _canonical(
            {"import_edges": sorted(self.edges), "families": sorted(self.families)}
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def attempt_classes(obs: Observation, det: DetectorSet) -> dict[str, tuple[str | None, int | None]]:
    """C(a): class key -> (path, line_no) of its first evidence in ``obs``."""
    out: dict[str, tuple[str | None, int | None]] = {}
    for edge in obs.edges:
        if edge.key in det.edges:
            out.setdefault(edge.key, (edge.path, edge.line_no))
    for hit in obs.rule_hits:
        if hit.key and hit.key in det.edges:
            out.setdefault(hit.key, (hit.path, hit.line_no))
    if "guarded" in det.families:
        for op in obs.guarded:
            key = f"guarded:{op.root}:{op.op}"
            if _CLASS_KEY.fullmatch(key):
                out.setdefault(key, (op.path, None))
    if "test" in det.families:
        for path in obs.failing_tests:
            key = f"test:{path}"
            if _CLASS_KEY.fullmatch(key):
                out.setdefault(key, (path, None))
    if "missing-test" in det.families and not any(f.test for f in obs.files):
        for f in obs.files:
            if f.op in ("A", "M") and f.source and not f.test and f.area is not None:
                key = f"missing-test:{f.area}"
                if _CLASS_KEY.fullmatch(key):
                    out.setdefault(key, (None, None))
    return out


def is_exposed(obs: Observation, key: str) -> bool:
    """X(a, k): edges and missing-test need an added or modified file in the area."""
    fam = family_of(key)
    if fam == "import-edge":
        parsed = parse_edge_key(key)
        target = parsed[0] if parsed else None
    elif fam == "missing-test":
        target = key.split(":", 1)[1]
    else:
        return True
    return any(f.op in ("A", "M") and f.area == target for f in obs.files)


@dataclass(frozen=True)
class RuleInfo:
    id: str
    learned: bool
    where: str
    forbidden: tuple[str, ...]
    reason: str


def rules_from_config(cfg: Any, source: str = "cadence.yaml") -> list[RuleInfo]:
    if cfg is None:
        return []
    if not isinstance(cfg, dict):
        raise LadderError(f"{source} is not a YAML mapping")
    raw = cfg.get("boundaries") or []
    if not isinstance(raw, list):
        raise LadderError(f"{source}: boundaries must be a list")
    rules = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise LadderError(f"{source}: boundaries[{idx}] must be a mapping")
        where, forbidden, reason = entry.get("where"), entry.get("forbidden"), entry.get("reason")
        if isinstance(forbidden, str):
            forbidden = [forbidden]
        if not isinstance(where, str) or not isinstance(forbidden, list):
            raise LadderError(f"{source}: boundaries[{idx}] needs where and forbidden")
        forbidden_t = tuple(str(f) for f in forbidden)
        rid = entry.get("id")
        if rid is None:
            rid = seed_rule_id(where, forbidden_t)
        elif not isinstance(rid, str) or not _RULE_ID.fullmatch(rid):
            raise LadderError(f"{source}: boundaries[{idx}].id {rid!r} is not L-/B- plus 8 hex digits")
        rules.append(
            RuleInfo(
                id=rid,
                learned="id" in entry and rid.startswith("L-"),
                where=where,
                forbidden=forbidden_t,
                reason=str(reason),
            )
        )
    return rules


def rule_covers_edge(rule: RuleInfo, edge: Edge) -> bool:
    """True if ``rule`` forbids ``edge`` (where matches its file, forbidden its target)."""
    if edge.to.startswith("pkg:"):
        return False
    probe = "__cadence_probe__" if edge.to == "." else f"{edge.to}/__cadence_probe__"
    return fnmatch.fnmatchcase(edge.path, rule.where) and any(
        fnmatch.fnmatchcase(probe, f) for f in rule.forbidden
    )


def collect_seeds(
    observations: Sequence[Observation],
    findings: Iterable[Finding],
    lesson_keys: Iterable[str] = (),
    rules: Sequence[RuleInfo] = (),
) -> set[str]:
    """Every import-edge key something has seeded (docs/LEARNING.md, Seeding)."""
    seeds: set[str] = set()
    for obs in observations:
        for hit in obs.rule_hits:
            if hit.key and family_of(hit.key) == "import-edge":
                seeds.add(hit.key)
        for edge in obs.edges:
            if any(rule_covers_edge(rule, edge) for rule in rules):
                seeds.add(edge.key)
    for finding in findings:
        if finding.family == "import-edge" and (finding.rule_id or finding.signal in SEED_SIGNALS):
            seeds.add(finding.class_key)
    for key in lesson_keys:
        if family_of(key) == "import-edge":
            seeds.add(key)
    return seeds


def window_of(attempts: Sequence[Attempt], settings: Settings, now: float) -> list[Attempt]:
    """The last window_attempts attempts or the last window_days days, whichever is more."""
    ordered = sorted(attempts, key=lambda a: (a.completed_at, a.run))
    cutoff = to_utc(now) - timedelta(days=settings.window_days)
    by_days = sum(1 for a in ordered if a.completed_at >= cutoff)
    keep = max(min(len(ordered), settings.window_attempts), by_days)
    return ordered[len(ordered) - keep :]


# --- Repo view ---------------------------------------------------------------------------


def load_rules(root: Path) -> list[RuleInfo]:
    path = root / ".cadence" / "cadence.yaml"
    if not path.is_file():
        return []
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise LadderError(f"could not read {path}: {exc}") from exc
    return rules_from_config(_load_yaml(text, str(path)), str(path))


def lesson_problems(doc: Any, schemas: Schemas) -> list[str]:
    """Schema and id-rule problems of a parsed lessons.yaml (empty: fine)."""
    problems = schemas.errors("lessons.schema.json", doc, required=True)
    if problems:
        return problems
    seen_keys: set[str] = set()
    for lesson in doc["lessons"]:
        key = lesson["class_key"]
        if key in seen_keys:
            problems.append(f"{key}: more than one lesson")
        seen_keys.add(key)
        if lesson["id"] != lesson_id(key):
            problems.append(f"{key}: id must be {lesson_id(key)}")
        check = lesson.get("check")
        if check is not None:
            if check["rule_id"] != lesson["id"]:
                problems.append(f"{key}: check.rule_id must be {lesson['id']}")
            if check["fixture"] != f"{RETRO_FIXTURE_PREFIX}{lesson['id'][2:]}/":
                problems.append(f"{key}: check.fixture must be {RETRO_FIXTURE_PREFIX}{lesson['id'][2:]}/")
    return problems


def parse_lessons_text(text: str, source: str, schemas: Schemas) -> list[dict[str, Any]]:
    doc = _dates_to_str(_load_yaml(text, source))
    if doc is None:
        return []
    problems = lesson_problems(doc, schemas)
    if problems:
        raise LadderError(f"{source} is invalid: " + "; ".join(problems[:5]))
    return [dict(lesson) for lesson in doc["lessons"]]


def load_lessons(root: Path, schemas: Schemas) -> list[dict[str, Any]]:
    path = root / ".cadence" / "lessons.yaml"
    if not path.is_file():
        return []
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise LadderError(f"could not read {path}: {exc}") from exc
    return parse_lessons_text(text, str(path), schemas)


def _checker_rule(checker: Any, info: RuleInfo) -> Any:
    try:
        return checker.Rule(where=info.where, forbidden=info.forbidden, reason=info.reason, id=info.id)
    except TypeError:  # a checker without rule ids
        return checker.Rule(where=info.where, forbidden=info.forbidden, reason=info.reason)


def root_rule_hits(root: Path, rules: Sequence[RuleInfo]) -> dict[str, int]:
    """Violations on the repo itself, per rule id (retro fixtures excluded)."""
    if not rules:
        return {}
    checker = _sibling("check_boundaries")
    by_shape: dict[tuple[str, str], list[str]] = defaultdict(list)
    for info in rules:
        for pattern in info.forbidden:
            by_shape[(pattern, info.reason)].append(info.id)
    counts: dict[str, int] = defaultdict(int)
    for violation in checker.find_violations(root, [_checker_rule(checker, r) for r in rules]):
        if violation.path.startswith(RETRO_FIXTURE_PREFIX):
            continue
        rid = getattr(violation, "rule_id", "") or ""
        ids = [rid] if rid else by_shape.get((violation.forbidden, violation.reason), [])
        for one in ids:
            counts[one] += 1
    return dict(counts)


def default_emitter() -> Path:
    return _TOOL_DIR / "emit_rule.py"


def run_emitter(emitter: Path, args: Sequence[str], timeout: int = 900) -> tuple[int, dict[str, Any] | None]:
    """Run emit_rule.py isolated; (exit code, its last JSON line or None)."""
    cmd = [sys.executable, "-I", "-B", str(emitter), *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LadderError(f"could not run {emitter}: {exc}") from exc
    if proc.stderr:
        sys.stderr.write(proc.stderr[-4000:])
    payload = None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            payload = value
        break
    return proc.returncode, payload


def run_replay(emitter: Path, root: Path, skip_ids: Iterable[str]) -> list[dict[str, Any]]:
    """``emit_rule.py --replay`` over tests/fixtures/retro; one entry per fixture."""
    fixture_root = root / "tests" / "fixtures" / "retro"
    if not fixture_root.is_dir():
        return []
    cmd = [
        sys.executable,
        "-I",
        "-B",
        str(emitter),
        "--replay",
        "--json",
        "--project-root",
        str(root),
        "--fixture-root",
        str(fixture_root),
        "--skip-ids",
        ",".join(sorted(skip_ids)),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LadderError(f"could not run {emitter} --replay: {exc}") from exc
    if proc.returncode not in (0, 1):
        raise LadderError(f"emit_rule.py --replay failed (exit {proc.returncode}): {proc.stderr[-500:]}")
    out = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError as exc:
            raise LadderError("emit_rule.py --replay printed a line that is not JSON") from exc
        fixture, rid, fired = item.get("fixture"), item.get("rule_id"), item.get("fired")
        if not (isinstance(fixture, str) and _FIXTURE_NAME.fullmatch(fixture)):
            continue
        if rid is not None and not (isinstance(rid, str) and _RULE_ID.fullmatch(rid)):
            rid = None
        out.append({"fixture": fixture, "rule_id": rid, "fired": fired is True})
    return out[:200]


@dataclass
class RepoView:
    root: Path
    lessons: list[dict[str, Any]]
    rules: list[RuleInfo]
    replay: list[dict[str, Any]]
    root_hits: dict[str, int]

    def area_exists(self, name: str) -> bool:
        if name == ".":
            return True
        if not _SAFE_PATH.fullmatch(name) or ".." in name.split("/"):
            return False
        return (self.root / name).is_dir()

    def file_exists(self, path: str) -> bool:
        if not _SAFE_PATH.fullmatch(path) or ".." in path.split("/"):
            return False
        return (self.root / path).is_file()


# --- Plan -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Occurrence:
    attempt: Attempt
    path: str | None
    line_no: int | None


@dataclass(frozen=True)
class Rejection:
    at: datetime
    to: str


@dataclass
class Candidate:
    key: str
    from_rung: str
    to: str
    reason: str
    rank: tuple[Any, ...]
    issues: list[int]
    occurrences: int
    evidence: list[str]
    text: str | None = None
    sample: dict[str, Any] | None = None
    needs: str | None = None
    alternates: list[dict[str, Any]] = field(default_factory=list)

    def transition(self) -> dict[str, Any]:
        out = {
            "lesson_id": lesson_id(self.key),
            "class_key": self.key,
            "from": self.from_rung,
            "to": self.to,
            "reason": self.reason,
            "issues": self.issues,
            "occurrences": self.occurrences,
            "evidence": self.evidence,
            "text": self.text,
            "sample": self.sample,
            "emit": None,
        }
        if self.alternates:
            out["alternates"] = list(self.alternates)
        return out


def _sample_ref(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "patch_sha256": sample["patch_sha256"],
        "path": sample["path"],
        "line_no": sample["line_no"],
    }


def plan_sha(base_sha: str, transitions: Sequence[dict[str, Any]]) -> str:
    items = []
    for t in transitions:
        sample = t.get("sample")
        item: dict[str, Any] = {
            "class_key": t["class_key"],
            "to": t["to"],
            "reason": t["reason"],
            "sample": _sample_ref(sample) if sample else None,
        }
        # Only a transition with alternates gains the key, so the sha of
        # every plan without them is what it was before alternates existed.
        if t.get("alternates"):
            item["alternates"] = [_sample_ref(s) for s in t["alternates"]]
        items.append(item)
    items.sort(key=lambda d: (d["to"], d["class_key"]))
    payload = json.dumps({"base_sha": base_sha, "transitions": items}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _refs(issues: Sequence[int]) -> str:
    return ", ".join(f"#{i}" for i in issues)


def check_text(key: str, issues: Sequence[int]) -> str:
    frm, to = parse_edge_key(key) or ("?", "?")
    return f"`{frm}/` must not import `{to}/`. Enforced by check {lesson_id(key)} (seen in {_refs(issues)})."


def fallback_text(key: str, issues: Sequence[int]) -> str:
    frm, to = parse_edge_key(key) or ("?", "?")
    return f"Code under `{frm}/` must not import `{to}` (seen in {_refs(issues)})."


def pattern_text(key: str, issues: Sequence[int]) -> str | None:
    fam = family_of(key)
    body = key.split(":", 1)[1]
    if fam == "import-edge":
        return fallback_text(key, issues)
    if fam == "guarded":
        root, _, op = body.rpartition(":")
        if op == "add":
            return f"Do not add files under `{root}/`; the gate leaves them out (seen in {_refs(issues)})."
        return (
            f"Do not modify or delete existing files under `{root}/`; the gate restores "
            f"them (seen in {_refs(issues)})."
        )
    if fam == "missing-test":
        return f"Changes under `{body}/` must add or update a test (missed in {_refs(issues)})."
    if fam == "test":
        return f"Changes have broken `{body}` ({_refs(issues)}); run it before finishing."
    return None


def rule_reason(key: str, issues: Sequence[int]) -> str:
    frm, to = parse_edge_key(key) or ("?", "?")
    lid = lesson_id(key)
    return (
        f"Learned rule {lid} ({_refs(issues)}): {frm}/ must not import {to}/. "
        f"Fixture {RETRO_FIXTURE_PREFIX}{lid[2:]}/."
    )


def _occ_sort_key(occ: Occurrence) -> tuple[datetime, str]:
    return (occ.attempt.completed_at, occ.attempt.run)


def decision_rejections(
    decisions: Sequence[dict[str, Any]], lessons: dict[str, dict[str, Any]]
) -> dict[str, list[Rejection]]:
    """Transitions that did not land, per class (docs/LEARNING.md, Rejection)."""
    out: dict[str, list[Rejection]] = defaultdict(list)
    for decision in decisions:
        for t in decision["transitions"]:
            if t["landed"]:
                continue
            lesson = lessons.get(t["class_key"])
            if decision["merged"] and lesson is not None:
                rung = lesson.get("rung")
                # The check fell back to a pattern in apply, or the rung is there anyway.
                if rung == t["to"] or (t["to"] == "check" and rung == "pattern"):
                    continue
            out[t["class_key"]].append(Rejection(decision["closed"], t["to"]))
    return out


class Planner:
    """Recomputes the ladder from state and repo (docs/LEARNING.md, The ladder)."""

    def __init__(self, settings: Settings, state: State, repo: RepoView, now: float) -> None:
        self.s = settings
        self.state = state
        self.repo = repo
        self.now = now
        self.today = to_utc(now).date()
        self.eval_mode = settings.mode == "eval-sandbox"
        self.lessons = {lesson["class_key"]: lesson for lesson in repo.lessons}
        self.seeds = collect_seeds(
            state.observations,
            state.all_findings(),
            self.lessons.keys(),
            repo.rules,
        )
        self.det = DetectorSet("current", frozenset(self.seeds), frozenset(INTRINSIC_FAMILIES))
        self.classes: dict[str, dict[str, tuple[str | None, int | None]]] = {}
        self.occ_all: dict[str, list[Occurrence]] = defaultdict(list)
        for attempt in state.attempts:
            found = attempt_classes(attempt.obs, self.det)
            self.classes[attempt.run] = found
            for key, (path, line_no) in found.items():
                self.occ_all[key].append(Occurrence(attempt, path, line_no))
        window = window_of(state.attempts, settings, now)
        self.window_runs = {a.run for a in window}
        self.occ_win: dict[str, list[Occurrence]] = {
            key: [o for o in occs if o.attempt.run in self.window_runs]
            for key, occs in self.occ_all.items()
        }
        self.finding_ids: dict[tuple[str, str], str] = {}
        for stem, items in state.findings.items():
            for f in items:
                self.finding_ids.setdefault((f.run if not stem.startswith("pr-") else stem, f.class_key), f.id)
        self.rejections = decision_rejections(state.decisions, self.lessons)
        self.seed_ids = {r.id for r in repo.rules if not r.learned}
        self.edges_by_key: dict[str, list[Edge]] = defaultdict(list)
        self.seed_hit_keys: set[str] = set()
        for obs in state.observations:
            for edge in obs.edges:
                self.edges_by_key[edge.key].append(edge)
            for hit in obs.rule_hits:
                if hit.key and hit.rule_id in self.seed_ids:
                    self.seed_hit_keys.add(hit.key)
        self.replay_by_fixture = {item["fixture"]: item for item in repo.replay}
        self.checks: list[Candidate] = []
        self.patterns: list[Candidate] = []
        self.removals: list[Candidate] = []
        self.skipped: list[dict[str, str]] = []
        self.needs: list[dict[str, str]] = []

    # -- helpers --

    def skip(self, key: str, why: str) -> None:
        if _CLASS_KEY.fullmatch(key) and _WHY.fullmatch(why):
            self.skipped.append({"class_key": key, "why": why})

    def need(self, key: str, why: str) -> None:
        if why in NEEDS_WHY and _NEEDS_KEY.fullmatch(key):
            item = {"key": key, "why": why}
            if item not in self.needs:
                self.needs.append(item)

    def stats(self, occs: Sequence[Occurrence]) -> tuple[set[int], int, datetime | None]:
        issues = {o.attempt.issue for o in occs}
        escaped = sum(1 for o in occs if o.attempt.published)
        last = max((o.attempt.completed_at for o in occs), default=None)
        return issues, escaped, last

    def rank(self, key: str, occs: Sequence[Occurrence]) -> tuple[Any, ...]:
        issues, escaped, last = self.stats(occs)
        return (-len(issues), -escaped, -(last.timestamp() if last else 0.0), key)

    def issues5(self, occs: Sequence[Occurrence]) -> list[int]:
        recent: list[int] = []
        for occ in sorted(occs, key=_occ_sort_key, reverse=True):
            if occ.attempt.issue not in recent:
                recent.append(occ.attempt.issue)
            if len(recent) == 5:
                break
        return sorted(recent)

    def evidence(self, key: str, occs: Sequence[Occurrence]) -> list[str]:
        out: list[str] = []
        for occ in sorted(occs, key=_occ_sort_key, reverse=True):
            fid = None
            for run in occ.attempt.runs:
                fid = self.finding_ids.get((run, key))
                if fid:
                    break
            if fid is None:
                fam = family_of(key)
                signal = {"guarded": "guarded", "test": "gate"}.get(fam, "detector")
                if occ.path and occ.line_no:
                    loc = f"{occ.path}:{occ.line_no}"
                elif occ.path:
                    loc = f"file:{occ.path}"
                else:
                    loc = "-"
                parts = [
                    signal,
                    occ.attempt.repo,
                    str(occ.attempt.issue),
                    occ.attempt.obs.patch_sha256 or "-",
                    key,
                    loc,
                ]
                fid = str(uuid.uuid5(NS_CADENCE, "|".join(parts)))
            if fid not in out:
                out.append(fid)
            if len(out) == 5:
                break
        return out

    def cooled(self, occs: Sequence[Occurrence], at: datetime) -> bool:
        """Two new distinct issues with occurrences after ``at``."""
        before = {o.attempt.issue for o in occs if o.attempt.completed_at <= at}
        after = {o.attempt.issue for o in occs if o.attempt.completed_at > at}
        return len(after - before) >= 2

    def covered_by_seed(self, key: str) -> bool:
        if key in self.seed_hit_keys:
            return True
        seeds = [r for r in self.repo.rules if not r.learned]
        return any(rule_covers_edge(r, e) for r in seeds for e in self.edges_by_key.get(key, ()))

    def stale(self, key: str) -> bool:
        fam = family_of(key)
        body = key.split(":", 1)[1]
        if fam == "import-edge":
            frm, to = parse_edge_key(key) or (".", ".")
            return not self.repo.area_exists(frm) or (
                not to.startswith("pkg:") and not self.repo.area_exists(to)
            )
        if fam == "guarded":
            return not self.repo.area_exists(body.rpartition(":")[0])
        if fam == "missing-test":
            return not self.repo.area_exists(body)
        if fam == "test":
            return not self.repo.file_exists(body)
        return False

    def exposed_after(self, key: str, since: date) -> list[Attempt]:
        return [
            a
            for a in self.state.attempts
            if a.completed_at.date() >= since and is_exposed(a.obs, key)
        ]

    def find_samples(self, key: str) -> tuple[list[dict[str, Any]], str]:
        """Up to MAX_SAMPLES samples for a check on ``key``, newest occurrence first.

        Each is a strict import line in a supported language with a stored
        patch, distinct by (path, import line). ``apply`` tries them in
        order. An empty list comes with why: emit-failed (a line but no
        usable patch or glob) or unsupported-language (no line at all).
        """
        frm, to = parse_edge_key(key) or (".", ".")
        where, forbidden = f"{frm}/**", f"{to}/**"
        if frm == "." or to == "." or not (_glob_ok(where) and _glob_ok(forbidden)):
            return [], "emit-failed"
        has_line = False
        samples: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for occ in sorted(self.occ_all.get(key, []), key=_occ_sort_key, reverse=True):
            attempt = occ.attempt
            edges = sorted((e for e in attempt.obs.edges if e.key == key), key=lambda e: (e.path, e.line_no))
            for edge in edges:
                ext = PurePosixPath(edge.path).suffix
                if edge.line is None or ext not in SAMPLE_LANGUAGE:
                    continue
                if ".." in edge.path.split("/") or edge.path.startswith("tests/fixtures/"):
                    continue
                if not line_ok(LANG_FAMILY[ext], edge.line):
                    continue
                has_line = True
                if (edge.path, edge.line) in seen:
                    continue
                run = next((r for r in attempt.runs if f"patches/{r}.patch" in self.state.patches), None)
                if run is None:
                    continue
                seen.add((edge.path, edge.line))
                samples.append(
                    {
                        "run": run,
                        "patch": f"patches/{run}.patch",
                        "patch_sha256": attempt.obs.patch_sha256,
                        "path": edge.path,
                        "line_no": edge.line_no,
                        "import_line": edge.line,
                        "language": SAMPLE_LANGUAGE[ext],
                        "where": where,
                        "forbidden_pattern": forbidden,
                    }
                )
                if len(samples) == MAX_SAMPLES:
                    return samples, ""
        if samples:
            return samples, ""
        return [], ("emit-failed" if has_line else "unsupported-language")

    def make(self, key: str, from_rung: str, to: str, reason: str, occs: Sequence[Occurrence], **extra: Any) -> Candidate:
        return Candidate(
            key=key,
            from_rung=from_rung,
            to=to,
            reason=reason,
            rank=self.rank(key, occs),
            issues=self.issues5(occs),
            occurrences=len(occs),
            evidence=self.evidence(key, occs),
            **extra,
        )

    # -- promotion --

    def promotion_target(self, key: str, from_rung: str, occs: Sequence[Occurrence]) -> Candidate | None:
        issues = self.issues5(occs)
        fam = family_of(key)
        if fam == "import-edge":
            frm, to = parse_edge_key(key) or (".", ".")
            fallback = fallback_text(key, issues)
            if to.startswith("pkg:"):
                return self.make(key, from_rung, "pattern", "promote", occs, text=fallback, needs="package-edge")
            if self.covered_by_seed(key):
                return self.make(key, from_rung, "pattern", "promote", occs, text=fallback)
            samples, why = self.find_samples(key)
            if not samples:
                return self.make(key, from_rung, "pattern", "emit-fallback", occs, text=fallback, needs=why)
            return self.make(
                key, from_rung, "check", "promote", occs,
                text=check_text(key, issues), sample=samples[0], alternates=samples[1:],
            )  # fmt: skip
        if fam == "test" and self.stale(key):
            self.skip(key, "stale")
            return None
        text = pattern_text(key, issues)
        if text is None:
            self.skip(key, "no-template")
            return None
        return self.make(key, from_rung, "pattern", "promote", occs, text=text)

    def consider_promotion(self, key: str, lesson: dict[str, Any] | None, rung: str) -> None:
        occs = self.occ_win.get(key, [])
        if rung == "retired" and lesson is not None:
            retired_on = parse_date((lesson.get("retired") or {}).get("on")) or self.today
            occs = [o for o in occs if o.attempt.completed_at.date() > retired_on]
        issues = {o.attempt.issue for o in occs}
        if len(issues) < self.s.promote_after:
            return
        rejected = sorted(
            (r for r in self.rejections.get(key, []) if r.to in ("pattern", "check", "suppressed")),
            key=lambda r: r.at,
        )
        if rejected:
            last = rejected[-1]
            if len(rejected) >= 2 and last.to != "suppressed":
                self.removals.append(self.make(key, rung, "suppressed", "rejected-twice", occs))
                return
            if not self.cooled(occs, last.at):
                self.skip(key, "cooldown")
                return
        candidate = self.promotion_target(key, rung, occs)
        if candidate is None:
            return
        if candidate.text is not None and not 10 <= len(candidate.text) <= 280:
            self.skip(key, "text-too-long")
            return
        (self.checks if candidate.to == "check" else self.patterns).append(candidate)

    def consider_upgrade(self, key: str, lesson: dict[str, Any]) -> None:
        """A pattern edge that recurred after its promotion is offered as a check."""
        if family_of(key) != "import-edge":
            return
        frm, to = parse_edge_key(key) or (".", ".")
        if to.startswith("pkg:") or self.covered_by_seed(key):
            return
        occs = self.occ_win.get(key, [])
        if len({o.attempt.issue for o in occs}) < self.s.promote_after:
            return
        since = parse_date(lesson.get("since")) or self.today
        if not any(o.attempt.completed_at.date() > since for o in occs):
            return
        rejected = sorted((r for r in self.rejections.get(key, []) if r.to == "check"), key=lambda r: r.at)
        if rejected and not self.cooled(occs, rejected[-1].at):
            self.skip(key, "cooldown")
            return
        samples, _ = self.find_samples(key)
        if not samples:
            return
        issues = self.issues5(occs)
        self.checks.append(
            self.make(
                key, "pattern", "check", "promote", occs,
                text=check_text(key, issues), sample=samples[0], alternates=samples[1:],
            )
        )  # fmt: skip

    # -- retirement --

    def retirement_rejected(self, key: str, lesson: dict[str, Any]) -> bool:
        since = parse_date(lesson.get("since")) or self.today
        return any(
            r.to == "retired" and r.at.date() >= since for r in self.rejections.get(key, [])
        )

    def retire(self, key: str, lesson: dict[str, Any], reason: str) -> None:
        if reason in ("stale", "dormant", "cap") and self.retirement_rejected(key, lesson):
            self.skip(key, "retirement-rejected")
            return
        occs = self.occ_win.get(key, [])
        cand = self.make(key, lesson["rung"], "retired", reason, occs)
        cand.issues = [i for i in lesson.get("issues", [])][:5] or cand.issues
        cand.text = None
        self.removals.append(cand)

    def consider_check_retirement(self, key: str, lesson: dict[str, Any]) -> bool:
        lid = lesson["id"]
        pinned = bool(lesson.get("pinned"))
        if not any(r.id == lid for r in self.repo.rules):
            self.retire(key, lesson, "human-removed")
            return True
        if self.repo.root_hits.get(lid):
            self.retire(key, lesson, "blocks-merged-code")
            return True
        fixture = (lesson.get("check") or {}).get("fixture", "")
        replay = self.replay_by_fixture.get(fixture[len(RETRO_FIXTURE_PREFIX) :].rstrip("/"))
        if replay is None or not replay["fired"]:
            self.retire(key, lesson, "broken")
            return True
        if self.eval_mode or pinned:
            return False
        if self.stale(key):
            self.retire(key, lesson, "stale")
            return True
        since = parse_date(lesson.get("since")) or self.today
        if (self.today - since).days >= CHECK_DORMANT_DAYS:
            recent = self.exposed_after(key, since)[-CHECK_DORMANT_ATTEMPTS:]
            if not any(h.rule_id == lid for a in recent for h in a.obs.rule_hits):
                if self.s.retire_dormant:
                    self.retire(key, lesson, "dormant")
                    return True
                self.skip(key, "dormant-report-only")
        return False

    def consider_pattern_retirement(self, key: str, lesson: dict[str, Any]) -> bool:
        if self.eval_mode or lesson.get("pinned"):
            return False
        if self.stale(key):
            self.retire(key, lesson, "stale")
            return True
        since = parse_date(lesson.get("since")) or self.today
        if (self.today - since).days >= PATTERN_DORMANT_DAYS:
            recent = self.exposed_after(key, since)[-PATTERN_DORMANT_ATTEMPTS:]
            if not any(key in self.classes.get(a.run, {}) for a in recent):
                self.retire(key, lesson, "dormant")
                return True
        return False

    # -- caps --

    def apply_caps(self) -> list[Candidate]:
        s = self.s
        checks = sorted(self.checks, key=lambda c: c.rank)
        for c in checks[s.max_checks_per_pr :]:
            self.skip(c.key, "cap")
        checks = checks[: s.max_checks_per_pr]

        patterns = sorted(self.patterns, key=lambda c: c.rank)
        for c in patterns[s.max_patterns_per_pr :]:
            self.skip(c.key, "cap")
        patterns = patterns[: s.max_patterns_per_pr]

        removals = sorted(self.removals, key=lambda c: (_RETIRE_SEVERITY[c.reason], c.key))
        for c in removals[s.max_retirements_per_pr :]:
            self.skip(c.key, "cap")
        removals = removals[: s.max_retirements_per_pr]

        leaving = {c.key for c in removals if c.from_rung == "pattern"} | {
            c.key for c in checks if c.from_rung == "pattern"
        }
        active = [l for l in self.repo.lessons if l["rung"] == "pattern" and l["class_key"] not in leaving]
        excess = len(active) + len(patterns) - s.max_active_patterns
        if excess > 0:
            victims = sorted(
                (l for l in active if not l.get("pinned") and not self.eval_mode),
                key=lambda l: self.rank(l["class_key"], self.occ_win.get(l["class_key"], [])),
                reverse=True,  # weakest first
            )
            room = s.max_retirements_per_pr - len(removals)
            while excess > 0:
                weakest_new = patterns[-1] if patterns else None
                if victims and room > 0:
                    victim = victims[0]
                    victim_rank = self.rank(victim["class_key"], self.occ_win.get(victim["class_key"], []))
                    if weakest_new is None or victim_rank > weakest_new.rank:
                        victims.pop(0)
                        before = len(self.removals)
                        self.retire(victim["class_key"], victim, "cap")
                        if len(self.removals) > before:
                            removals.append(self.removals[-1])
                            room -= 1
                            excess -= 1
                        continue
                if weakest_new is None:
                    break
                self.skip(patterns.pop().key, "active-cap")
                excess -= 1
        return checks + patterns + removals

    # -- run --

    def run(self, base_sha: str) -> dict[str, Any]:
        keys = sorted(
            {k for k in self.occ_win if family_of(k) in HEADLINE_FAMILIES} | set(self.lessons)
        )
        for key in keys:
            lesson = self.lessons.get(key)
            rung = lesson["rung"] if lesson else "note"
            if rung in ("note", "retired"):
                self.consider_promotion(key, lesson, rung)
            elif rung == "pattern":
                assert lesson is not None
                if not self.consider_pattern_retirement(key, lesson):
                    self.consider_upgrade(key, lesson)
            elif rung == "check":
                assert lesson is not None
                self.consider_check_retirement(key, lesson)
            elif rung == "suppressed":
                if len({o.attempt.issue for o in self.occ_win.get(key, [])}) >= self.s.promote_after:
                    self.skip(key, "suppressed")

        selected = self.apply_caps()
        for cand in selected:
            if cand.needs:
                self.need(cand.key, cand.needs)
        transitions = [c.transition() for c in sorted(selected, key=lambda c: (_TO_ORDER[c.to], c.key))]

        for rule in self.repo.rules:
            if not rule.learned and self.repo.root_hits.get(rule.id):
                self.need(rule.id, "seed-rule-on-main")
        cutoff = to_utc(self.now) - timedelta(days=self.s.window_days)
        post: dict[str, set[int]] = defaultdict(set)
        for f in self.state.post_pr_findings():
            if f.family in POST_PR_FAMILIES and f.ts >= cutoff:
                post[f.class_key].add(f.issue)
        for key in sorted(post):
            if len(post[key]) >= self.s.promote_after:
                self.need(key, "needs-words")
        for lesson in self.repo.lessons:
            if lesson["rung"] != "check":
                continue
            since = parse_date(lesson.get("since")) or self.today
            if any(
                o.attempt.published and o.attempt.completed_at.date() > since
                for o in self.occ_all.get(lesson["class_key"], [])
            ):
                self.need(lesson["class_key"], "regressed")

        verify = any(
            t["to"] == "check" or (t["to"] == "pattern" and family_of(t["class_key"]) == "test")
            for t in transitions
        )
        return {
            "schema": "cadence.retro-plan/1",
            "plan_sha": plan_sha(base_sha, transitions),
            "base_sha": base_sha,
            "generated_at": iso_utc(to_utc(self.now)),
            "mode": self.s.mode,
            "applied": False,
            "verify_required": verify,
            "transitions": transitions,
            "skipped": self.skipped[:100],
            "needs_human": self.needs[:50],
            "replay": self.repo.replay[:200],
            "metrics": None,
        }


def build_repo_view(root: Path, schemas: Schemas, emitter: Path) -> RepoView:
    lessons = load_lessons(root, schemas)
    rules = load_rules(root)
    retired = {l["id"] for l in lessons if l["rung"] in ("retired", "suppressed")}
    replay = run_replay(emitter, root, retired)
    hits = root_rule_hits(root, rules)
    return RepoView(root=root, lessons=lessons, rules=rules, replay=replay, root_hits=hits)


def compute_plan(
    settings: Settings,
    state: State,
    repo: RepoView,
    *,
    base_sha: str,
    now: float,
) -> dict[str, Any]:
    if not _SHA1.fullmatch(base_sha):
        raise LadderError(f"base sha {base_sha!r} is not 40 hex digits")
    return Planner(settings, state, repo, now).run(base_sha)


def plan_changed(plan: dict[str, Any], open_plan_sha: str | None) -> bool:
    return (
        plan["mode"] in ("on", "eval-sandbox")
        and bool(plan["transitions"])
        and plan["plan_sha"] != open_plan_sha
    )


def failed_before(state_dir: Path, sha: str) -> bool:
    """True if cadence/state records that the plan ``sha`` failed scripts/verify.sh.

    Reads ``retro/failed/<sha>.json`` by its direct path only (see "Failed
    plans"). A missing record is silently False; one that is a symlink,
    not a regular file, over MAX_FAILED_RECORD_BYTES, not JSON, or not
    ``{"schema": "cadence.retro-failed/1", "plan_sha": sha, ...}`` is False
    with a warning, so a broken record can never stop the ladder for good.
    """
    if not _SHA256.fullmatch(sha):
        return False
    rel = f"retro/failed/{sha}.json"
    path = state_dir / "retro" / "failed" / f"{sha}.json"

    def ignored(why: str) -> bool:
        print(f"WARN: {rel} on cadence/state is ignored: {why}", file=sys.stderr)
        return False

    try:
        for part in (state_dir / "retro", state_dir / "retro" / "failed", path):
            if part.is_symlink():
                return ignored(f"{part.name} is a symlink")
            if not part.exists():
                return False
        if not path.is_file():
            return ignored("not a regular file")
        with path.open("rb") as fh:
            data = fh.read(MAX_FAILED_RECORD_BYTES + 1)
        if len(data) > MAX_FAILED_RECORD_BYTES:
            return ignored(f"larger than {MAX_FAILED_RECORD_BYTES} bytes")
        raw = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        return ignored(f"unreadable ({str(exc)[:120]})")
    if not isinstance(raw, dict) or raw.get("schema") != FAILED_PLAN_SCHEMA:
        return ignored(f"not a {FAILED_PLAN_SCHEMA} record")
    if raw.get("plan_sha") != sha:
        return ignored("its plan_sha does not match its name")
    return True


# --- PATTERNS.md section ------------------------------------------------------------------


def render_section(lessons: Sequence[dict[str, Any]]) -> str:
    active = [l for l in lessons if l.get("rung") in ("pattern", "check") and l.get("text")]
    active.sort(key=lambda l: (0 if l["rung"] == "check" else 1, l["id"]))
    lines = [LEARNED_SECTION_HEADING, "", SECTION_INTRO]
    if active:
        lines.append("")
        lines += [f"- **{l['id']}** ({l['rung']}): {l['text']}" for l in active]
    return "\n".join(lines) + "\n"


def split_section(text: str) -> tuple[str, str | None, str]:
    """(before, section or None, after): the section runs to the next ``## `` line."""
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.rstrip("\r\n") == LEARNED_SECTION_HEADING),
        None,
    )
    if start is None:
        return text, None, ""
    end = next(
        (j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")),
        len(lines),
    )
    return "".join(lines[:start]), "".join(lines[start:end]), "".join(lines[end:])


def replace_section(text: str, lessons: Sequence[dict[str, Any]]) -> str:
    section = render_section(lessons)
    before, current, after = split_section(text)
    if current is None:
        base = text
        if base and not base.endswith("\n"):
            base += "\n"
        if base and not base.endswith("\n\n"):
            base += "\n"
        new = base + section
    else:
        new = before + section + ("\n" if after else "") + after
    if "\r\n" in text:
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    return new


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip()


# --- Apply ------------------------------------------------------------------------------------


_LESSON_KEYS = (
    "id",
    "class_key",
    "rung",
    "since",
    "text",
    "issues",
    "evidence",
    "check",
    "retired",
    "history",
    "rejections",
    "pinned",
)


def _ordered_lesson(lesson: dict[str, Any]) -> dict[str, Any]:
    return {k: lesson[k] for k in _LESSON_KEYS if k in lesson}


def update_lesson(
    existing: dict[str, Any] | None,
    transition: dict[str, Any],
    today: str,
    rejections: int,
) -> dict[str, Any]:
    key = transition["class_key"]
    lid = transition["lesson_id"]
    lesson = dict(existing) if existing else {"id": lid, "class_key": key}
    history = list(lesson.get("history") or [])
    prior_retired = sum(1 for h in history if h.get("rung") == "retired")
    to = transition["to"]
    lesson["rung"] = to
    lesson["since"] = today
    if to in ("pattern", "check"):
        lesson["text"] = transition["text"]
        lesson["issues"] = list(transition["issues"])[:5]
        lesson["evidence"] = list(transition["evidence"])[:5]
        lesson.pop("retired", None)
        if transition["from"] == "retired" and prior_retired >= 2:
            lesson["pinned"] = True
    if to == "check":
        lesson["check"] = {
            "kind": "boundary-rule",
            "rule_id": lid,
            "fixture": f"{RETRO_FIXTURE_PREFIX}{lid[2:]}/",
        }
    elif to == "pattern":
        lesson.pop("check", None)
    elif to == "retired":
        lesson["retired"] = {"on": today, "reason": transition["reason"]}
    elif to == "suppressed":
        lesson.pop("retired", None)
    history.append({"rung": to, "on": today})
    lesson["history"] = history[-20:]
    lesson["rejections"] = rejections
    lesson.setdefault("pinned", False)
    return _ordered_lesson(lesson)


def render_lessons(lessons: Sequence[dict[str, Any]]) -> str:
    doc = {"schema": "cadence.lessons/1", "lessons": [_ordered_lesson(l) for l in lessons]}
    body = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=False, width=4096)
    if yaml.safe_load(body) != doc:
        raise LadderError("lessons.yaml would not round-trip")
    return LESSONS_HEADER + body


def _finding_for(
    transition: dict[str, Any], now: float, sample: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The finding emit_rule.py proves for ``sample`` (default: the transition's)."""
    key = transition["class_key"]
    sample = sample if sample is not None else transition["sample"]
    frm, to = parse_edge_key(key) or ("?", "?")
    issues = transition["issues"] or [1]
    return {
        "id": lesson_finding_id(key),
        "ts": iso_utc(to_utc(now)),
        "feature": f"issue #{issues[-1]}",
        "what_happened": (
            f"Agent patches for {_refs(issues)} import {to} from {frm} "
            f"({sample['path']}:{sample['line_no']})."
        ),
        "auto_catchable": True,
        "auto_method": "boundary-rule",
        "rule_existed": False,
        "proposed_fix": (
            f"Promote a boundary rule: {frm}/** must not import {to}/** (tool/ladder.py)."
        ),
        "fix_layer": 3,
        "decision": "approved",
        "violation_sample": {
            "kind": "boundary-rule",
            "language": sample["language"],
            "where": sample["where"],
            "import_line": sample["import_line"],
            "forbidden_pattern": sample["forbidden_pattern"],
            "reason": rule_reason(key, transition["issues"]),
        },
    }


def _validated_patch(state_dir: Path, sample: dict[str, Any]) -> Path:
    rel = sample["patch"]
    if rel != f"patches/{sample['run']}.patch" or not _STATE_PATH.fullmatch(rel):
        raise ValueError("sample.patch does not name the sample run's patch")
    path = state_dir / rel
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{rel} is not in the state dir")
    data = path.read_bytes()
    if len(data) > MAX_PATCH_BYTES:
        raise ValueError(f"{rel} is larger than {MAX_PATCH_BYTES} bytes")
    if hashlib.sha256(data).hexdigest() != sample["patch_sha256"]:
        raise ValueError(f"{rel} does not match the observation's patch_sha256")
    return path


def _snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def _restore(path: Path, data: bytes | None) -> None:
    if data is None:
        if path.exists():
            path.unlink()
    elif _snapshot(path) != data:
        path.write_bytes(data)


class Applier:
    def __init__(
        self,
        plan: dict[str, Any],
        *,
        root: Path,
        state_dir: Path,
        emitter: Path,
        schema_dir: Path | None,
        schemas: Schemas,
        now: float,
        verify_failed: bool = False,
    ) -> None:
        self.plan = plan
        self.root = root
        self.state_dir = state_dir
        self.emitter = emitter
        self.schema_dir = schema_dir
        self.schemas = schemas
        self.now = now
        self.verify_failed = verify_failed
        self.today = to_utc(now).date().isoformat()
        self.skipped = list(plan["skipped"])
        self.needs = list(plan["needs_human"])
        self._plan_skipped = len(self.skipped)
        self._plan_needs = len(self.needs)

    def need(self, key: str, why: str) -> None:
        item = {"key": key, "why": why}
        if item not in self.needs and why in NEEDS_WHY and _NEEDS_KEY.fullmatch(key):
            self.needs.append(item)

    def emit_check(self, t: dict[str, Any], workdir: Path) -> dict[str, Any] | None:
        """Run the check proof on the sample, then on each alternate until one lands.

        Returns the transition to keep (the landed sample, or a pattern
        fallback) or None. Exit 1 or 2, or a patch that fails its checks,
        moves on to the next sample; exit 3 (the rule fires on the repo) or
        exit 0 without the rule appended (an equivalent rule exists) stops,
        since another sample of the same rule cannot change that.
        """
        key, lid = t["class_key"], t["lesson_id"]
        fixture_rel = f"{RETRO_FIXTURE_PREFIX}{lid[2:]}/"
        fixture_dir = self.root / fixture_rel
        config = self.root / ".cadence" / "cadence.yaml"
        existed = fixture_dir.exists()
        backup = workdir / f"fixture-{lid}"
        if existed:
            shutil.copytree(fixture_dir, backup)
        config_before = _snapshot(config)

        def put_back() -> None:
            # Leave the repo as it was: no new fixture, the old one back, cadence.yaml as before.
            if fixture_dir.exists() and (not existed or backup.exists()):
                shutil.rmtree(fixture_dir)
            if existed and backup.exists():
                shutil.copytree(backup, fixture_dir)
            _restore(config, config_before)

        candidates = [t["sample"], *(t.get("alternates") or [])][:MAX_SAMPLES]
        code: int = 2
        for number, sample in enumerate(candidates, 1):
            landed = False
            stop = False
            try:
                patch = _validated_patch(self.state_dir, sample)
                finding = workdir / f"finding-{lid}.json"
                _write_json(finding, _finding_for(t, self.now, sample))
                args = [
                    "--input", str(finding),
                    "--project-root", str(self.root),
                    "--rule-id", lid,
                    "--class-key", key,
                    "--provenance-patch", str(patch),
                    "--provenance-path", sample["path"],
                    "--provenance-line", str(sample["line_no"]),
                    "--must-pass-root",
                    "--apply",
                    "--json",
                    "--now", str(int(self.now)),
                ]  # fmt: skip
                if existed:
                    args.append("--force")
                if self.schema_dir is not None:
                    args += ["--schema-dir", str(self.schema_dir)]
                code, payload = run_emitter(self.emitter, args)
                landed = (
                    code == 0
                    and payload is not None
                    and payload.get("fired") is True
                    and payload.get("applied") is True
                )
                stop = not landed and code not in (1, 2)
            except (OSError, ValueError) as exc:
                code = 2
                print(f"WARN: check {lid} for {key}, sample {number}: not emitted: {exc}", file=sys.stderr)
            if landed:
                t = dict(t)
                t["sample"] = sample
                t.pop("alternates", None)
                t["emit"] = {"exit": 0, "fixture": fixture_rel}
                return t
            put_back()
            if stop:
                break
            if number < len(candidates):
                print(
                    f"WARN: check {lid} for {key}: sample {number} not proven (exit {code}); trying the next",
                    file=sys.stderr,
                )
        self.need(key, "emit-failed")
        if t["from"] == "pattern":
            self.skipped.append({"class_key": key, "why": "emit-fallback-no-change"})
            return None
        t = dict(t)
        t.pop("alternates", None)
        t["to"] = "pattern"
        t["reason"] = "emit-fallback"
        t["text"] = fallback_text(key, t["issues"])
        t["emit"] = {"exit": code, "fixture": None}
        return t

    def demote_check(self, t: dict[str, Any]) -> dict[str, Any] | None:
        """--verify-failed: no emit. A new check is proposed as a pattern instead."""
        key = t["class_key"]
        self.need(key, "verify-failed")
        if t["from"] == "pattern":
            self.skipped.append({"class_key": key, "why": "verify-fallback-no-change"})
            return None
        t = dict(t)
        t.pop("alternates", None)
        t["to"] = "pattern"
        t["reason"] = "verify-fallback"
        t["text"] = fallback_text(key, t["issues"])
        t["sample"] = None
        t["emit"] = None
        return t

    def drop_test_pattern(self, t: dict[str, Any]) -> None:
        """--verify-failed: a test: pattern rests on the test passing on main,
        which is what scripts/verify.sh just failed to show."""
        self.skipped.append({"class_key": t["class_key"], "why": "verify-failed"})
        self.need(t["class_key"], "verify-failed")

    @staticmethod
    def _capped(items: list[dict[str, str]], from_plan: int, limit: int) -> list[dict[str, str]]:
        """At most ``limit`` items, keeping every one this apply added."""
        added = items[from_plan:][:limit]
        return items[: max(0, min(from_plan, limit - len(added)))] + added

    def retire_check(self, t: dict[str, Any]) -> dict[str, Any] | None:
        lid = t["lesson_id"]
        config = self.root / ".cadence" / "cadence.yaml"
        before = _snapshot(config)
        code, _ = run_emitter(self.emitter, ["--retire", lid, "--project-root", str(self.root), "--json"])
        if code in (0, 4):
            t = dict(t)
            t["emit"] = {"exit": code, "fixture": None}
            return t
        _restore(config, before)
        self.skipped.append({"class_key": t["class_key"], "why": "retire-failed"})
        self.need(t["class_key"], "emit-failed")
        return None

    def run(self) -> tuple[dict[str, Any], int]:
        lessons = load_lessons(self.root, self.schemas)
        by_key = {l["class_key"]: l for l in lessons}
        state = read_state(self.state_dir, self.schemas)
        rejections = decision_rejections(state.decisions, by_key)

        kept: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="cadence-ladder-") as tmp:
            workdir = Path(tmp)
            for t in self.plan["transitions"]:
                result: dict[str, Any] | None
                if t["to"] == "check":
                    result = self.demote_check(t) if self.verify_failed else self.emit_check(t, workdir)
                elif self.verify_failed and t["to"] == "pattern" and family_of(t["class_key"]) == "test":
                    self.drop_test_pattern(t)
                    result = None
                elif t["to"] == "retired" and t["from"] == "check" and t["reason"] != "human-removed":
                    result = self.retire_check(t)
                else:
                    result = dict(t)
                if result is not None:
                    result.pop("alternates", None)
                    kept.append(result)

        for t in kept:
            key = t["class_key"]
            count = len([r for r in rejections.get(key, []) if r.to in ("pattern", "check", "suppressed")])
            by_key[key] = update_lesson(by_key.get(key), t, self.today, count)

        if kept:
            new_lessons = sorted(by_key.values(), key=lambda l: l["id"])
            lessons_path = self.root / ".cadence" / "lessons.yaml"
            text = render_lessons(new_lessons)
            problems = lesson_problems(_dates_to_str(yaml.safe_load(text)), self.schemas)
            if problems:
                raise LadderError("generated lessons.yaml is invalid: " + "; ".join(problems[:5]))
            if _snapshot(lessons_path) != text.encode("utf-8"):
                lessons_path.parent.mkdir(parents=True, exist_ok=True)
                lessons_path.write_bytes(text.encode("utf-8"))
            patterns_path = self.root / "docs" / "PATTERNS.md"
            old = patterns_path.read_bytes().decode("utf-8") if patterns_path.is_file() else ""
            new = replace_section(old, new_lessons)
            if new != old:
                patterns_path.parent.mkdir(parents=True, exist_ok=True)
                patterns_path.write_bytes(new.encode("utf-8"))

        applied = dict(self.plan)
        applied["applied"] = True
        applied["transitions"] = sorted(kept, key=lambda t: (_TO_ORDER[t["to"]], t["class_key"]))
        applied["skipped"] = self._capped(self.skipped, self._plan_skipped, 100)
        applied["needs_human"] = self._capped(self.needs, self._plan_needs, 50)
        applied["verify_required"] = any(
            (t["to"] == "check" and (t.get("emit") or {}).get("exit") == 0)
            or (t["to"] == "pattern" and family_of(t["class_key"]) == "test")
            for t in kept
        )
        problems = self.schemas.errors("retro-plan.schema.json", applied, required=True)
        if problems:
            raise LadderError("applied.json would be invalid: " + "; ".join(problems[:5]))
        return applied, (EXIT_OK if kept else EXIT_NOTHING)


def validate_plan(plan: Any, schemas: Schemas, what: str) -> dict[str, Any]:
    problems = schemas.errors("retro-plan.schema.json", plan, required=True)
    if problems:
        raise LadderError(f"{what} fails retro-plan.schema.json: " + "; ".join(problems[:5]))
    if parse_ts(plan["generated_at"]) is None:
        raise LadderError(f"{what}: generated_at is not a timestamp")
    for t in plan["transitions"]:
        if t["lesson_id"] != lesson_id(t["class_key"]):
            raise LadderError(f"{what}: {t['class_key']} has the wrong lesson id")
        if family_of(t["class_key"]) not in HEADLINE_FAMILIES:
            raise LadderError(f"{what}: {t['class_key']} is not a headline class")
        alternates = t.get("alternates")
        if alternates is not None and t["to"] != "check":
            raise LadderError(f"{what}: {t['class_key']} has alternates but is not a check")
        if t["to"] == "check":
            parsed = parse_edge_key(t["class_key"])
            sample = t["sample"]
            if parsed is None or sample is None:
                raise LadderError(f"{what}: check {t['class_key']} needs an edge key and a sample")
            for label, one in [("sample", sample), *(("alternate", a) for a in alternates or [])]:
                if one["where"] != f"{parsed[0]}/**" or one["forbidden_pattern"] != f"{parsed[1]}/**":
                    raise LadderError(f"{what}: check {t['class_key']} {label} does not match its key")
                if not (_glob_ok(one["where"]) and _glob_ok(one["forbidden_pattern"])):
                    raise LadderError(f"{what}: check {t['class_key']} has an unsafe glob")
    return plan


# --- Guard -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    status: str
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    path: str


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LadderError(f"git {args[0]} failed: {exc}") from exc
    if check and proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()[:500]
        raise LadderError(f"git {' '.join(args[:2])} failed: {message}")
    return proc


class TreeView:
    """The proposed tree: an index (temporary or a worktree's) over the repo's objects."""

    def __init__(self, cwd: Path, env: dict[str, str] | None) -> None:
        self.cwd = cwd
        self.env = env

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return _git(self.cwd, *args, env=self.env, check=check)

    def diff(self, base: str) -> list[Change]:
        out = self.git("diff", "--cached", "--raw", "-z", "--no-renames", "--no-abbrev", base).stdout
        tokens = out.split(b"\0")
        changes = []
        i = 0
        while i + 1 < len(tokens):
            meta = tokens[i].decode("ascii", "replace")
            if not meta.startswith(":"):
                i += 1
                continue
            parts = meta[1:].split()
            path = tokens[i + 1].decode("utf-8", "surrogateescape")
            if len(parts) >= 5:
                changes.append(Change(parts[4][:1], parts[0], parts[1], parts[2], parts[3], path))
            i += 2
        return changes

    def ls(self, prefix: str) -> list[tuple[str, str, str]]:
        out = self.git("ls-files", "-s", "-z", "--", prefix).stdout
        entries = []
        for record in out.split(b"\0"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            parts = meta.decode("ascii", "replace").split()
            if len(parts) >= 2:
                entries.append((parts[0], parts[1], path.decode("utf-8", "surrogateescape")))
        return entries

    def blob(self, sha: str, limit: int) -> bytes:
        size = self.git("cat-file", "-s", sha).stdout.strip()
        if int(size or b"0") > limit:
            raise ValueError(f"object {sha[:12]} is larger than {limit} bytes")
        return self.git("cat-file", "blob", sha).stdout

    def base_blob(self, base: str, path: str, limit: int) -> bytes | None:
        spec = f"{base}:{path}"
        if self.git("cat-file", "-e", spec, check=False).returncode != 0:
            return None
        return self.blob(spec, limit)

    def base_has(self, base: str, prefix: str) -> bool:
        out = self.git("ls-tree", "-r", "--name-only", base, "--", prefix).stdout
        return bool(out.strip())


def _learned(entry: Any) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("id"), str) and bool(_LESSON_ID.fullmatch(entry["id"]))


def learned_rule_problems(entry: Any) -> list[str]:
    rid = entry.get("id") if isinstance(entry, dict) else None
    label = f"rule {rid}"
    if not isinstance(entry, dict) or set(entry) != {"id", "where", "forbidden", "reason"}:
        return [f"{label}: must have exactly id, where, forbidden and reason"]
    problems = []
    if not _glob_ok(entry["where"]):
        problems.append(f"{label}: unsafe where")
    forbidden = entry["forbidden"]
    if not (isinstance(forbidden, list) and len(forbidden) == 1 and _glob_ok(forbidden[0])):
        problems.append(f"{label}: forbidden must be one safe glob")
    reason = entry["reason"]
    if not (isinstance(reason, str) and 5 <= len(reason) <= 400) or any(
        ord(ch) < 32 or ord(ch) == 127 for ch in str(reason)
    ):
        problems.append(f"{label}: reason must be one line of 5-400 characters")
    return problems


def _strip_learned(doc: Any) -> Any:
    if not isinstance(doc, dict):
        return doc
    out = dict(doc)
    rules = out.get("boundaries")
    if rules is None:
        rules = []
    if isinstance(rules, list):
        out["boundaries"] = [r for r in rules if not _learned(r)]
    return out


def _yaml_or_problem(data: bytes | None, source: str, problems: list[str]) -> Any:
    if data is None:
        return None
    try:
        return yaml.safe_load(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError, ValueError, RecursionError) as exc:
        problems.append(f"{source}: does not parse ({str(exc)[:120]})")
        return None


class Guard:
    def __init__(
        self,
        tree: TreeView,
        base: str,
        schemas: Schemas,
        applied: dict[str, Any] | None,
        ignore: Sequence[str] = (),
    ) -> None:
        self.tree = tree
        self.base = base
        self.schemas = schemas
        self.applied = applied
        self.ignore = set(ignore)
        self.problems: list[str] = []
        transitions = applied["transitions"] if applied else []
        self.check_ids = {
            t["lesson_id"]
            for t in transitions
            if t["to"] == "check" and (t.get("emit") or {}).get("exit") == 0
        }
        self.retire_ids = {t["lesson_id"] for t in transitions if t["to"] == "retired" and t["from"] == "check"}

    def fail(self, message: str) -> None:
        self.problems.append(message)

    def run(self) -> list[str]:
        changes = [c for c in self.tree.diff(self.base) if c.path not in self.ignore]
        fixtures: dict[str, list[Change]] = defaultdict(list)
        changed = {c.path: c for c in changes}
        for c in changes:
            in_fixtures = c.path.startswith(RETRO_FIXTURE_PREFIX)
            if c.path not in RETRO_ALLOWLIST and not in_fixtures:
                self.fail(f"{c.path!r}: outside the retro allowlist")
                continue
            name = c.path[len(RETRO_FIXTURE_PREFIX) :].split("/", 1)[0] if in_fixtures else ""
            if c.status == "D":
                if not (in_fixtures and self.applied is not None and f"L-{name}" in self.check_ids):
                    self.fail(f"{c.path}: deletes a file")
                if in_fixtures:
                    fixtures[name].append(c)
                continue
            if c.status not in ("A", "M"):
                self.fail(f"{c.path}: change type {c.status} is refused")
                continue
            if c.new_mode != "100644":
                self.fail(f"{c.path}: mode {c.new_mode} (symlinks, executables and submodules are refused)")
            if in_fixtures:
                fixtures[name].append(c)

        for name in sorted(fixtures):
            self.check_fixture(name)
        if ".cadence/cadence.yaml" in changed:
            self.check_cadence_yaml(changed[".cadence/cadence.yaml"])
        new_lessons = self.lessons_in_tree(changed)
        if ".cadence/lessons.yaml" in changed:
            self.check_lessons(new_lessons, changed[".cadence/lessons.yaml"])
        if "docs/PATTERNS.md" in changed:
            self.check_patterns(changed["docs/PATTERNS.md"], new_lessons)
        return self.problems

    def check_fixture(self, name: str) -> None:
        prefix = f"{RETRO_FIXTURE_PREFIX}{name}/"
        if not _FIXTURE_NAME.fullmatch(name):
            self.fail(f"{prefix}: a fixture directory must be named by 8 hex digits")
            return
        existed = self.tree.base_has(self.base, prefix)
        if self.applied is not None:
            if f"L-{name}" not in self.check_ids:
                self.fail(f"{prefix}: no check transition for L-{name} in applied.json")
        elif existed:
            self.fail(f"{prefix}: changes an existing fixture without a check transition")
        entries = {path[len(prefix) :]: (mode, sha) for mode, sha, path in self.tree.ls(prefix)}
        required = {"finding.json", "provenance.json", ".cadence/cadence.yaml"}
        samples = [rel for rel in entries if rel not in required]
        if not required <= set(entries) or len(samples) != 1:
            self.fail(
                f"{prefix}: must hold exactly finding.json, provenance.json, "
                ".cadence/cadence.yaml and one sample"
            )
            return
        sample = samples[0]
        if PurePosixPath(sample).suffix not in LANG_FAMILY or not _SAFE_PATH.fullmatch(sample):
            self.fail(f"{prefix}{sample}: the sample must be a TS/JS, Python or Dart file")
        contents: dict[str, bytes] = {}
        for rel, (mode, sha) in entries.items():
            if mode != "100644":
                self.fail(f"{prefix}{rel}: mode {mode}")
                continue
            try:
                contents[rel] = self.tree.blob(sha, MAX_FIXTURE_FILE_BYTES)
            except ValueError:
                self.fail(f"{prefix}{rel}: larger than {MAX_FIXTURE_FILE_BYTES} bytes")
        cfg = _yaml_or_problem(contents.get(".cadence/cadence.yaml"), f"{prefix}.cadence/cadence.yaml", self.problems)
        rules = cfg.get("boundaries") if isinstance(cfg, dict) else None
        if not (isinstance(rules, list) and len(rules) == 1 and _learned(rules[0]) and rules[0]["id"] == f"L-{name}"):
            self.fail(f"{prefix}.cadence/cadence.yaml: must hold exactly the rule L-{name}")
        else:
            for problem in learned_rule_problems(rules[0]):
                self.fail(f"{prefix}.cadence/cadence.yaml: {problem}")
        for rel in ("finding.json", "provenance.json"):
            try:
                value = json.loads(contents.get(rel, b"").decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                value = None
            if not isinstance(value, dict):
                self.fail(f"{prefix}{rel}: not a JSON object")
            elif rel == "provenance.json" and (value.get("path") != sample or value.get("rule_id") != f"L-{name}"):
                self.fail(f"{prefix}provenance.json: path and rule_id must name the sample and L-{name}")

    def check_cadence_yaml(self, change: Change) -> None:
        if change.status == "D":
            return
        try:
            new_data = self.tree.blob(change.new_sha, MAX_TEXT_FILE_BYTES)
            old_data = self.tree.base_blob(self.base, ".cadence/cadence.yaml", MAX_TEXT_FILE_BYTES)
        except ValueError as exc:
            self.fail(f".cadence/cadence.yaml: {exc}")
            return
        new = _yaml_or_problem(new_data, ".cadence/cadence.yaml", self.problems)
        old = _yaml_or_problem(old_data, "base .cadence/cadence.yaml", self.problems)
        if old is None:
            old = {}
        if not isinstance(new, dict) or not isinstance(old, dict):
            self.fail(".cadence/cadence.yaml: must be a YAML mapping")
            return
        if _strip_learned(new) != _strip_learned(old):
            self.fail(".cadence/cadence.yaml: changes something other than learned (L-) rules")
        new_rules = new.get("boundaries") or []
        old_rules = old.get("boundaries") or []
        if not isinstance(new_rules, list) or not isinstance(old_rules, list):
            self.fail(".cadence/cadence.yaml: boundaries must be a list")
            return
        new_ids = [r["id"] for r in new_rules if _learned(r)]
        old_by_id = {r["id"]: r for r in old_rules if _learned(r)}
        old_ids = set(old_by_id)
        if len(new_ids) != len(set(new_ids)):
            self.fail(".cadence/cadence.yaml: duplicate learned rule ids")
        for entry in new_rules:
            if _learned(entry) and entry["id"] in old_by_id and entry != old_by_id[entry["id"]]:
                self.fail(f".cadence/cadence.yaml: changes the existing learned rule {entry['id']}")
        proved = {
            t["lesson_id"]: t
            for t in (self.applied["transitions"] if self.applied is not None else [])
            if t["to"] == "check" and t.get("sample")
        }
        for entry in new_rules:
            if _learned(entry) and entry["id"] not in old_ids:
                for problem in learned_rule_problems(entry):
                    self.fail(f".cadence/cadence.yaml: {problem}")
                fixture = f"{RETRO_FIXTURE_PREFIX}{entry['id'][2:]}/.cadence/cadence.yaml"
                listed = self.tree.ls(fixture)
                if not listed:
                    self.fail(f".cadence/cadence.yaml: {entry['id']} has no fixture {fixture}")
                else:
                    # The rule that lands must be the one its fixture holds...
                    try:
                        data = self.tree.blob(listed[0][1], MAX_FIXTURE_FILE_BYTES)
                    except ValueError:
                        data = None
                    cfg = _yaml_or_problem(data, fixture, self.problems)
                    rules = cfg.get("boundaries") if isinstance(cfg, dict) else None
                    held = rules[0] if isinstance(rules, list) and len(rules) == 1 else None
                    if not isinstance(held, dict) or any(
                        held.get(k) != entry.get(k) for k in ("id", "where", "forbidden")
                    ):
                        self.fail(f".cadence/cadence.yaml: {entry['id']} differs from the rule in {fixture}")
                # ...and the one its check transition's sample proved.
                t = proved.get(entry["id"])
                if t is not None and (
                    entry.get("where") != t["sample"]["where"]
                    or entry.get("forbidden") != [t["sample"]["forbidden_pattern"]]
                ):
                    self.fail(f".cadence/cadence.yaml: {entry['id']} is not the rule its check transition proved")
        if self.applied is not None:
            for added in set(new_ids) - old_ids:
                if added not in self.check_ids:
                    self.fail(f".cadence/cadence.yaml: adds {added} without a check transition")
            for removed in old_ids - set(new_ids):
                if removed not in self.retire_ids:
                    self.fail(f".cadence/cadence.yaml: removes {removed} without a retirement")

    def lessons_in_tree(self, changed: dict[str, Change]) -> list[dict[str, Any]] | None:
        change = changed.get(".cadence/lessons.yaml")
        try:
            if change is not None:
                if change.status == "D":
                    return None
                data = self.tree.blob(change.new_sha, MAX_TEXT_FILE_BYTES)
            else:
                data = self.tree.base_blob(self.base, ".cadence/lessons.yaml", MAX_TEXT_FILE_BYTES)
        except ValueError as exc:
            self.fail(f".cadence/lessons.yaml: {exc}")
            return None
        if data is None:
            return []
        doc = _yaml_or_problem(data, ".cadence/lessons.yaml", self.problems)
        doc = _dates_to_str(doc)
        if doc is None:
            return []
        problems = lesson_problems(doc, self.schemas)
        if problems:
            if change is not None:
                for problem in problems[:10]:
                    self.fail(f".cadence/lessons.yaml: {problem}")
            return None
        return doc["lessons"]

    def check_lessons(self, new_lessons: list[dict[str, Any]] | None, change: Change) -> None:
        if new_lessons is None or self.applied is None:
            return
        try:
            old_data = self.tree.base_blob(self.base, ".cadence/lessons.yaml", MAX_TEXT_FILE_BYTES)
        except ValueError:
            old_data = None
        old_doc = _dates_to_str(_yaml_or_problem(old_data, "base .cadence/lessons.yaml", []))
        old = {
            l.get("class_key"): l.get("rung")
            for l in (old_doc or {}).get("lessons", [])
            if isinstance(l, dict)
        } if isinstance(old_doc, dict) else {}
        new = {l["class_key"]: l["rung"] for l in new_lessons}
        moved = {t["class_key"]: t["to"] for t in self.applied["transitions"]}
        for key, rung in new.items():
            if old.get(key) != rung and moved.get(key) != rung:
                self.fail(f".cadence/lessons.yaml: {key} moves to {rung} without a transition")
        for key, to in moved.items():
            if new.get(key) != to:
                self.fail(f".cadence/lessons.yaml: {key} is not at {to} as applied.json says")

    def check_patterns(self, change: Change, lessons: list[dict[str, Any]] | None) -> None:
        if change.status == "D":
            return
        try:
            new = self.tree.blob(change.new_sha, MAX_TEXT_FILE_BYTES).decode("utf-8")
            old_data = self.tree.base_blob(self.base, "docs/PATTERNS.md", MAX_TEXT_FILE_BYTES)
            old = old_data.decode("utf-8") if old_data is not None else ""
        except (ValueError, UnicodeDecodeError) as exc:
            self.fail(f"docs/PATTERNS.md: {exc}")
            return
        old_before, _, old_after = split_section(old)
        new_before, section, new_after = split_section(new)
        if section is None:
            self.fail(f"docs/PATTERNS.md: the section {LEARNED_SECTION_HEADING!r} is missing")
            return
        if _norm(old_before) != _norm(new_before) or old_after.replace("\r\n", "\n") != new_after.replace("\r\n", "\n"):
            self.fail("docs/PATTERNS.md: changes outside the learned section")
        if lessons is None or _norm(section) != _norm(render_section(lessons)):
            self.fail("docs/PATTERNS.md: the learned section is not what lessons.yaml renders")


def run_guard(
    root: Path,
    *,
    worktree: bool,
    base_ref: str,
    patch: Path | None,
    applied: dict[str, Any] | None,
    schemas: Schemas,
) -> list[str]:
    base = _git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}").stdout.decode().strip()
    if not _SHA1.fullmatch(base):
        raise LadderError(f"--base-ref {base_ref!r} is not a commit")
    with tempfile.TemporaryDirectory(prefix="cadence-guard-") as tmp:
        if worktree:
            env = dict(os.environ)
            env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
            tree = TreeView(root, env)
            tree.git("read-tree", base)
            tree.git("add", "-A")
            return Guard(tree, base, schemas, applied, VERIFY_MARKERS).run()
        assert patch is not None
        problems: list[str] = []
        size = patch.stat().st_size
        if size > MAX_RETRO_PATCH_BYTES:
            return [f"the patch is {size} bytes; the limit is {MAX_RETRO_PATCH_BYTES}"]
        wt = Path(tmp) / "wt"
        _git(root, "worktree", "add", "--detach", str(wt), base)
        try:
            applied_ok = _git(wt, "apply", "--index", "--whitespace=nowarn", str(patch.resolve()), check=False)
            if applied_ok.returncode != 0:
                message = applied_ok.stderr.decode("utf-8", "replace").strip()[:300]
                raise LadderError(f"the patch does not apply: {message}")
            problems += Guard(TreeView(wt, None), base, schemas, applied).run()
        finally:
            _git(root, "worktree", "remove", "--force", str(wt), check=False)
            _git(root, "worktree", "prune", check=False)
        return problems


# --- PR body -------------------------------------------------------------------------------------


def _safe_key(key: str) -> str:
    return key.replace("@", "(at)")


def _num(value: Any, digits: int = 3) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "n/a"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def _metrics_lines(metrics: dict[str, Any]) -> list[str]:
    rep = metrics.get("repeat") if isinstance(metrics.get("repeat"), dict) else {}
    esc = metrics.get("escape") if isinstance(metrics.get("escape"), dict) else {}
    att = metrics.get("attempts") if isinstance(metrics.get("attempts"), dict) else {}
    status = rep.get("status") if rep.get("status") in ("ok", "insufficient", "incomplete") else "n/a"
    catches = metrics.get("learned_check_catches")
    catches_n = catches.get("count") if isinstance(catches, dict) else None
    lines = [
        f"- Attempts scored: {_num(att.get('scored'))}",
        f"- Repeat rate: {_num(rep.get('rate'))} ({_num(rep.get('repeats'))} of "
        f"{_num(rep.get('opportunities'))} opportunities; {status})",
        f"- Escape rate: {_num(esc.get('rate'))} ({_num(esc.get('escapes'))} escapes)",
        f"- Learned check catches: {_num(catches_n)}",
    ]
    # Informational only: a report from before 2026-10-02 has no such block.
    cited = metrics.get("lessons_cited")
    if isinstance(cited, dict):
        absent = cited.get("cited_and_absent") if isinstance(cited.get("cited_and_absent"), dict) else {}
        present = cited.get("cited_and_present") if isinstance(cited.get("cited_and_present"), dict) else {}
        lines.append(
            "- Lessons cited by approved specs (informational; not a catch, not in any rate): "
            f"{_num(cited.get('attempts_with_citation'))} attempt(s) cited one; "
            f"cited and absent {_num(absent.get('count'))}, cited and present {_num(present.get('count'))}; "
            f"unknown for {_num(cited.get('attempts_unknown'))} attempt(s)"
        )
    lines.append(f"- Test-tampering rate: {_num(metrics.get('test_tampering_rate'))}")
    return lines


def render_pr_body(
    applied: dict[str, Any],
    repo: str,
    *,
    metrics: dict[str, Any] | None = None,
    run_url: str | None = None,
) -> str:
    transitions = applied["transitions"]

    def line(t: dict[str, Any]) -> str:
        base = f"- `{t['lesson_id']}` `{_safe_key(t['class_key'])}` ({t['from']} -> {t['to']}, {t['reason']})"
        if t["issues"]:
            base += f": seen in {_refs(t['issues'])}"
        base += f"; {int(t['occurrences'])} occurrence(s) in the window"
        emit = t.get("emit") or {}
        if t["to"] == "check" and emit.get("fixture"):
            base += f". Fixture `{emit['fixture']}`"
        return base + "."

    def section(title: str, items: list[str], limit: int = 60) -> list[str]:
        out = ["", f"### {title}", ""]
        if not items:
            return out + ["None."]
        out += items[:limit]
        if len(items) > limit:
            out.append(f"- ... and {len(items) - limit} more.")
        return out

    checks = [line(t) for t in transitions if t["to"] == "check"]
    patterns = [line(t) for t in transitions if t["to"] == "pattern"]
    removed = [line(t) for t in transitions if t["to"] in ("retired", "suppressed")]
    demoted = [
        f"- `{t['lesson_id']}` `{_safe_key(t['class_key'])}`: scripts/verify.sh failed on the retro "
        "result with this plan's checks in place, so the check is proposed as a pattern."
        for t in transitions
        if t["reason"] == "verify-fallback"
    ]
    for s in applied["skipped"]:
        if s["why"] == "verify-failed":
            demoted.append(
                f"- `{_safe_key(s['class_key'])}`: left out because scripts/verify.sh failed on the retro result."
            )
        elif s["why"] == "verify-fallback-no-change":
            demoted.append(
                f"- `{_safe_key(s['class_key'])}`: stays a pattern because scripts/verify.sh failed on "
                "the retro result with its check in place."
            )
    needs = [f"- `{_safe_key(n['key'])}`: {n['why']}" for n in applied["needs_human"]]
    replay = [
        f"- `{r['fixture']}` ({r['rule_id'] or 'no rule id'}): {'fires' if r['fired'] else 'DOES NOT FIRE'}"
        for r in applied["replay"]
    ]
    lines = [
        "## Cadence retro",
        "",
        "Generated by `tool/ladder.py` from factory runs (docs/LEARNING.md). "
        "Merge to approve every entry, delete an entry before merging to reject "
        "that one, or close this pull request to reject them all.",
    ]
    if run_url:
        lines += ["", f"Run: {run_url}"]
    lines += section("Checks", checks)
    lines += section("Patterns", patterns)
    if demoted:
        lines += section("Demoted after verify failed", demoted)
    lines += section("Retired", removed)
    lines += section("Needs a human", needs)
    lines += section("Replay", replay)
    lines += section("Metrics", _metrics_lines(metrics) if metrics else [])
    lines += ["", f"cadence retro plan {applied['plan_sha'][:12]}"]
    body = "\n".join(lines) + "\n"
    if "@" in body or "<" in body:
        raise LadderError("the PR body would contain '@' or '<'")
    if len(body) > MAX_PR_BODY:
        keep = MAX_PR_BODY - 200
        body = body[:keep].rsplit("\n", 1)[0] + "\n\n(truncated)\n\n" + f"cadence retro plan {applied['plan_sha'][:12]}\n"
    return body


# --- CLI ------------------------------------------------------------------------------------------


def _finite(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be finite")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="recompute the ladder and write plan.json")
    plan.add_argument("--state-dir", type=Path, required=True)
    plan.add_argument("--repo-root", type=Path, required=True)
    plan.add_argument("--config", type=Path)
    plan.add_argument("--open-plan-sha", default="")
    plan.add_argument("--base-sha")
    plan.add_argument("--schema-dir", type=Path)
    plan.add_argument("--emitter", type=Path)
    plan.add_argument("--now", type=_finite)
    plan.add_argument("--out", type=Path, required=True)

    app = sub.add_parser("apply", help="run the plan's emits and rewrite the lessons")
    app.add_argument("--plan", type=Path, required=True)
    app.add_argument("--repo-root", type=Path, required=True)
    app.add_argument("--state-dir", type=Path, required=True)
    app.add_argument("--emitter", type=Path)
    app.add_argument("--schema-dir", type=Path)
    app.add_argument("--now", type=_finite)
    app.add_argument(
        "--verify-failed",
        action="store_true",
        help="scripts/verify.sh failed on this plan's result: emit no check, demote them instead",
    )
    app.add_argument("--out", type=Path, required=True)

    grd = sub.add_parser("guard", help="refuse a retro change outside its allowlist")
    grd.add_argument("--repo-root", type=Path, required=True)
    mode = grd.add_mutually_exclusive_group(required=True)
    mode.add_argument("--worktree", action="store_true")
    mode.add_argument("--patch", type=Path)
    grd.add_argument("--base-ref", default="HEAD")
    grd.add_argument("--applied", type=Path)
    grd.add_argument("--schema-dir", type=Path)

    body = sub.add_parser("pr-body", help="write the retro PR body")
    body.add_argument("--applied", type=Path, required=True)
    body.add_argument("--repo", required=True)
    body.add_argument("--metrics", type=Path)
    body.add_argument("--run-url")
    body.add_argument("--schema-dir", type=Path)
    body.add_argument("--out", type=Path, required=True)
    return parser


def _cmd_plan(args: argparse.Namespace, now: float) -> int:
    root = args.repo_root.resolve()
    schemas = Schemas(root, args.schema_dir)
    config = args.config if args.config is not None else root / ".cadence" / "factory.yaml"
    settings = load_settings(config, explicit=args.config is not None)
    base_sha = args.base_sha
    if base_sha is None:
        base_sha = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    open_sha = args.open_plan_sha.strip() or None
    if open_sha is not None and not _SHA256.fullmatch(open_sha):
        print(f"WARN: --open-plan-sha {open_sha[:80]!r} is not a plan sha; ignored", file=sys.stderr)
        open_sha = None
    emitter = args.emitter if args.emitter is not None else default_emitter()
    state = read_state(args.state_dir, schemas)
    repo = build_repo_view(root, schemas, emitter)
    plan = compute_plan(settings, state, repo, base_sha=base_sha, now=now)
    validate_plan(plan, schemas, "plan.json")
    _write_json(args.out, plan)
    failed = failed_before(args.state_dir, plan["plan_sha"])
    if failed:
        print(
            f"plan {plan['plan_sha'][:12]} failed scripts/verify.sh before "
            f"(retro/failed/ on cadence/state); not proposed again until main or the plan changes",
            file=sys.stderr,
        )
    print(
        json.dumps(
            {
                "changed": plan_changed(plan, open_sha) and not failed,
                "plan_sha": plan["plan_sha"],
                "mode": plan["mode"],
                "transitions": len(plan["transitions"]),
                "failed_before": failed,
            }
        )
    )
    return EXIT_OK


def _cmd_apply(args: argparse.Namespace, now: float) -> int:
    root = args.repo_root.resolve()
    schemas = Schemas(root, args.schema_dir)
    plan = validate_plan(_read_json(args.plan), schemas, str(args.plan))
    emitter = args.emitter if args.emitter is not None else default_emitter()
    applier = Applier(
        plan,
        root=root,
        state_dir=args.state_dir.resolve(),
        emitter=emitter,
        schema_dir=args.schema_dir.resolve() if args.schema_dir else None,
        schemas=schemas,
        now=now,
        verify_failed=args.verify_failed,
    )
    applied, code = applier.run()
    _write_json(args.out, applied)
    counts = defaultdict(int)
    for t in applied["transitions"]:
        counts[t["to"]] += 1
    print(json.dumps({"applied": len(applied["transitions"]), "by_rung": dict(counts), "verify_required": applied["verify_required"]}))
    return code


def _cmd_guard(args: argparse.Namespace) -> int:
    root = args.repo_root.resolve()
    schemas = Schemas(root, args.schema_dir)
    applied = None
    if args.applied is not None:
        applied = validate_plan(_read_json(args.applied), schemas, str(args.applied))
        if applied["applied"] is not True:
            print("guard: applied.json is a plan that was never applied", file=sys.stderr)
            return EXIT_VIOLATION
        if any("alternates" in t for t in applied["transitions"]):
            print("guard: applied.json still lists alternates; apply records only the sample that landed", file=sys.stderr)
            return EXIT_VIOLATION
    if args.patch is not None and not args.patch.is_file():
        raise LadderError(f"--patch {args.patch} not found")
    problems = run_guard(
        root,
        worktree=args.worktree,
        base_ref=args.base_ref,
        patch=args.patch,
        applied=applied,
        schemas=schemas,
    )
    if problems:
        for problem in problems:
            print(f"guard: {problem}", file=sys.stderr)
        return EXIT_VIOLATION
    print("guard: ok", file=sys.stderr)
    return EXIT_OK


def _cmd_pr_body(args: argparse.Namespace) -> int:
    schemas = Schemas(None, args.schema_dir)
    if not _REPO.fullmatch(args.repo) or any(p in (".", "..") for p in args.repo.split("/")):
        raise LadderError(f"--repo {args.repo!r} is not OWNER/REPO")
    applied = validate_plan(_read_json(args.applied), schemas, str(args.applied))
    run_url = args.run_url
    if run_url:
        pattern = re.compile(r"^https://github\.com/" + re.escape(args.repo) + r"/actions/runs/[0-9]{1,20}$")
        if not pattern.fullmatch(run_url):
            raise LadderError("--run-url must be https://github.com/<repo>/actions/runs/<id>")
    metrics = None
    if args.metrics is not None:
        metrics = _read_json(args.metrics)
        problems = schemas.errors("metrics.schema.json", metrics, required=False)
        if not isinstance(metrics, dict) or problems:
            raise LadderError(f"{args.metrics} is not a metrics report")
    body = render_pr_body(applied, args.repo, metrics=metrics, run_url=run_url)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    # Loading the sibling tools must not leave tool/__pycache__ in the repo:
    # guard --worktree would (rightly) refuse it.
    sys.dont_write_bytecode = True
    args = _build_parser().parse_args(argv)
    try:
        now = getattr(args, "now", None)
        now = time.time() if now is None else now
        if args.command == "plan":
            return _cmd_plan(args, now)
        if args.command == "apply":
            return _cmd_apply(args, now)
        if args.command == "guard":
            return _cmd_guard(args)
        return _cmd_pr_body(args)
    except LadderError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except Exception:  # noqa: BLE001 - exit 1 means "violation" or "nothing to apply"
        traceback.print_exc()
        print("ERROR: internal error in ladder.py (see traceback)", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    sys.exit(main())
