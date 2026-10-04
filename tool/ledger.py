#!/usr/bin/env python3
"""Cadence factory cost ledger: keeps model spend bounded and every run accounted for.

Cadence never pays for, resells or proxies model usage. The ledger only
reads what the user's own run reports (the Claude Code result) and books
it against caps the user set in ``.cadence/factory.yaml``. It is pure
filesystem: the workflow syncs the records directory with a state branch
separately.

Config (``--config``, default ``.cadence/factory.yaml``)::

    budget:
      per_run_usd: 5.00   # hard cap for one run (--max-budget-usd)
      daily_usd: 25.00    # cap for one UTC day
    max_turns: 60         # optional; a positive integer when present
    autonomy: pr-only     # not read by the ledger
    retry:                # optional
      on_dod_fail: 1      # 0 or 1 (default 1): one retry on a failed gate

    Both budget numbers must be present, finite and > 0, and
    ``per_run_usd <= daily_usd``. Anything else exits 2.

    ``retry.on_dod_fail`` is read by the workflow (``Config.
    retry_on_dod_fail``): 1 lets a build whose Definition of Done gate
    failed at format, lint, boundaries or test run the agent once more in
    the same run. It must be the integer 0 or 1 (a boolean is refused);
    an unknown key under ``retry``, or a ``retry`` that is not a mapping,
    exits 2. An empty ``retry:`` means the default. The ledger's own math
    is unchanged: the workflow asks ``check`` before a retry starts, with
    the run itself counted in ``--in-flight``, so a run in its retry
    counts twice.

    An optional ``learning:`` block configures the learning loop (see
    docs/LEARNING.md); ``load_learning()`` returns it with defaults
    applied, and an invalid value exits 2 here too. Its ``budget``
    (``per_run_usd``, default 0.25, and ``daily_usd``, default
    min(1.00, budget.daily_usd)) caps the learn steps' model spend, inside
    the global daily cap: ``0 < per_run_usd <= daily_usd <=
    budget.daily_usd``. ``guarded_paths`` and ``test_roots`` hold at most
    16 relative directory paths each (``server/tests``: 1 to 6 segments of
    ``[A-Za-z0-9_.-]``, none ``.`` or ``..``); every test root lies inside
    a guarded path and outside ``.github``, ``.cadence``, ``scripts`` and
    ``tool``, which the gate always guards.

Records (``--records-dir``, default ``.cadence/runs``):
    One JSON file per run attempt, ``<run_id>-<run_attempt>.json``,
    created with exclusive create so a record is never overwritten. Each
    holds: issue, run_id, run_attempt, outcome, dod, total_cost_usd
    (number or null), booked_usd, cost_source ("reported", "cap" or
    "preflight:<reason>", see --preflight), num_turns (or null),
    per_run_cap_usd, recorded_at (ISO 8601 UTC,
    whole seconds) and, only when a reported cost exceeds the cap,
    ``"over_cap": true``. ``--stage``, ``--pr``, ``--published-sha`` and
    ``--base-sha`` add ``stage``, ``pr``, ``published_sha`` and
    ``base_sha``, each only when given. A ``--stage learn`` record names no
    issue: ``"issue": null``.

Contract:
    record  After every run attempt (success, failure, cancel or
            timeout), write its record. Cost comes from ``--cost-usd``,
            else from ``--result-json``: a Claude Code result that is a
            JSON object, a JSON array of messages, or a JSON-lines
            stream. The LAST object carrying ``total_cost_usd`` wins.
            Missing or corrupt files are tolerated. A run with no known
            cost is booked at the full per-run cap, never at zero (for a
            ``--stage learn`` record, ``learning.budget.per_run_usd``,
            the cap its model step ran under). A reported cost above the
            cap is booked as reported. The one exception is
            ``--preflight no-key``: the model job failed at its key check
            (the ANTHROPIC_API_KEY secret missing or empty), before its
            model step, so nothing was spent. It books $0 with
            ``cost_source`` ``"preflight:no-key"``, ``total_cost_usd`` 0
            and ``num_turns`` 0, and only with ``--outcome failure``,
            never with ``--cost-usd``, ``--turns``, ``--result-json``,
            ``--pr`` or ``--published-sha`` (exit 2). The workflow passes
            it only on the model job's own ``preflight`` output, which
            the check writes before any model or agent code runs.
    check   Before dispatching a run, print
            ``{spent_today, in_flight, per_run_usd, daily_usd,
            worst_case, allowed, unreadable}`` where
            ``worst_case = spent_today + in_flight * per_run_usd +
            per_run_usd`` (the runs already going, plus the one about to
            start). ``spent_today`` sums ``booked_usd`` over records
            whose ``recorded_at`` falls on the same UTC date as now. A
            record that cannot be read or lacks a valid ``booked_usd`` or
            ``recorded_at`` is counted in ``unreadable`` and booked at the
            full per-run cap on every day until a human repairs it.
            ``--pool learn`` asks for one learn step instead:
            ``worst_case = spent_today + in_flight * per_run_usd +
            learning.per_run_usd``, and it is allowed only if that is
            within ``budget.daily_usd`` AND ``learn_spent_today +
            learning.per_run_usd <= learning.daily_usd``, where
            ``learn_spent_today`` sums today's records with ``"stage":
            "learn"``. The report then adds ``pool``,
            ``learn_spent_today``, ``learn_per_run_usd`` and
            ``learn_daily_usd``.

Exit codes:
    0   ok (record written; check allows one more run)
    1   over budget (check), or a record for this run attempt already
        exists (record)
    2   bad input or config (missing/invalid factory.yaml, bad
        arguments, IO error), or an internal error (never exit 1)

Usage:
    python tool/ledger.py check --in-flight 2
    python tool/ledger.py record --run-id 123 --run-attempt 1 --issue 42 \\
        --outcome success --dod pass --result-json claude-result.json
    python tool/ledger.py record --run-id 123 --run-attempt 2 --issue 42 \\
        --outcome timeout
    python tool/ledger.py record --stage learn --run-id 130 --run-attempt 1 \\
        --outcome success --dod skipped --result-json claude-result.json
    python tool/ledger.py record --run-id 124 --run-attempt 1 --issue 42 \\
        --outcome failure --dod skipped --preflight no-key
    python tool/ledger.py check --pool learn --in-flight 1
    python tool/ledger.py --config .cadence/factory.yaml \\
        --records-dir .cadence/runs check --now 1790000000

``--config`` and ``--records-dir`` are accepted before or after the
subcommand. ``--now`` (epoch seconds) pins the clock for tests and
replays; it defaults to the current time.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML is required. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)


DEFAULT_CONFIG = Path(".cadence") / "factory.yaml"
DEFAULT_RECORDS_DIR = Path(".cadence") / "runs"

OUTCOMES: tuple[str, ...] = ("success", "failure", "cancelled", "timeout")
DOD_RESULTS: tuple[str, ...] = ("pass", "fail", "skipped", "unknown")
STAGES: tuple[str, ...] = ("spec", "build", "learn")
POOLS: tuple[str, ...] = ("build", "learn")
STAGE_LEARN = "learn"
# Why a model job stopped at its preflight check, before its model step ran
# (``--preflight``): booked at $0 as cost_source "preflight:<reason>".
# no-key: the ANTHROPIC_API_KEY secret was missing, empty or held whitespace.
PREFLIGHT_REASONS: tuple[str, ...] = ("no-key",)

LEARNING_MODES: tuple[str, ...] = ("observe", "on", "eval-sandbox")
DEFAULT_GUARDED_PATHS: tuple[str, ...] = (
    "tests",
    "test",
    ".github",
    ".cadence",
    "scripts",
    "tool",
)
DEFAULT_TEST_ROOTS: tuple[str, ...] = ("tests", "test")
# The graders and the config: the Definition of Done gate guards these
# whatever learning.guarded_paths says, and no test root may lie in them (a
# new tool/yaml.py would shadow PyYAML for tool/check_boundaries.py).
ALWAYS_GUARDED: tuple[str, ...] = (".github", ".cadence", "scripts", "tool")
# learning.guarded_paths and learning.test_roots hold at most this many
# entries each, of at most MAX_PATH_SEGMENTS segments.
MAX_GUARDED_ENTRIES = 16
MAX_PATH_SEGMENTS = 6
DEFAULT_TEST_GLOBS: tuple[str, ...] = (
    "tests/**",
    "test/**",
    "**/*.test.*",
    "**/*.spec.*",
    "**/test_*.py",
    "**/*_test.py",
    "**/*_test.go",
)
DEFAULT_EDIT_IGNORE: tuple[str, ...] = (
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "**/*.snap",
)
DEFAULT_LEARN_PER_RUN_USD = 0.25
DEFAULT_LEARN_DAILY_USD = 1.00
DEFAULT_RETRY_ON_DOD_FAIL = 1

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_BAD_INPUT = 2

# Run ids become file names, so keep them to a path-safe alphabet. Always
# used with fullmatch: ``$`` would also accept a trailing newline.
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA40_RE = re.compile(r"[0-9a-f]{40}")
# A guarded path (or test root) is a relative directory path: 1 to 6
# segments of [A-Za-z0-9_.-]{1,64} joined by "/", none of them "." or "..".
# So no leading or trailing slash, no "//" and no glob: it ends up in shell
# loops, git pathspecs and prefix matches. The workflow's paths step checks
# the same shape in bash (tests/test_factory_workflow.py runs both).
GUARDED_PATH_RE = r"^[A-Za-z0-9_.-]{1,64}(?:/[A-Za-z0-9_.-]{1,64}){0,5}$"
_GUARDED_PATH = re.compile(GUARDED_PATH_RE)
_MODEL_RE = re.compile(r"[A-Za-z0-9._:-]{0,100}")
_GLOB_RE = re.compile(r"[^\x00-\x1f\x7f]{1,200}")
_MAX_GLOBS = 50
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class LedgerError(Exception):
    """Bad input or config. ``main`` reports it and exits 2."""


class DuplicateRecord(Exception):
    """A record for this run_id + run_attempt already exists. Exit 1."""


@dataclass(frozen=True)
class LearningConfig:
    """The ``learning:`` block of factory.yaml, with defaults applied.

    See docs/LEARNING.md ("Config"). ``per_run_usd`` and ``daily_usd`` come
    from ``learning.budget``.
    """

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
    guarded_paths: tuple[str, ...] = DEFAULT_GUARDED_PATHS
    test_roots: tuple[str, ...] = DEFAULT_TEST_ROOTS
    test_globs: tuple[str, ...] = DEFAULT_TEST_GLOBS
    edit_ignore: tuple[str, ...] = DEFAULT_EDIT_IGNORE
    harvest_since_days: int = 30
    harvest_max_prs: int = 10
    settle_minutes: int = 10
    classify: bool = False
    model: str = ""
    per_run_usd: float = DEFAULT_LEARN_PER_RUN_USD
    daily_usd: float = DEFAULT_LEARN_DAILY_USD

    @property
    def classify_effective(self) -> bool:
        """Model labels run only when asked for, and never in eval-sandbox."""
        return self.classify and self.mode != "eval-sandbox"


@dataclass(frozen=True)
class Config:
    per_run_usd: float
    daily_usd: float
    max_turns: int | None
    autonomy: str | None
    learning: LearningConfig = field(default_factory=LearningConfig)
    # retry.on_dod_fail: 0 (off) or 1 (one retry on a failed gate).
    retry_on_dod_fail: int = DEFAULT_RETRY_ON_DOD_FAIL


@dataclass(frozen=True)
class ReportedUsage:
    total_cost_usd: float | None
    num_turns: int | None


# --- small helpers ---------------------------------------------------------


def _is_number(value: Any) -> bool:
    """True for a finite int or float. YAML/JSON booleans are not numbers.

    An integer too large for a float (JSON and YAML allow any number of
    digits) is not a usable amount either.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _is_count(value: Any) -> bool:
    """True for a non-negative integer (not a bool)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _dec(value: float) -> Decimal:
    """Exact decimal for summing money, so ten $0.10 runs book $1.00."""
    return Decimal(str(value))


def to_utc(epoch: float) -> datetime:
    """Whole-second UTC datetime for ``epoch``.

    Flooring (not rounding) keeps a record and a check made in the same
    second on the same UTC day, and works for any epoch on every OS.
    """
    try:
        return _EPOCH + timedelta(seconds=math.floor(epoch))
    except (OverflowError, ValueError) as exc:
        raise LedgerError(f"--now {epoch!r} is not a usable timestamp") from exc


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(text: Any) -> datetime | None:
    """Parse ``recorded_at``; None if it is not an ISO 8601 timestamp.

    Accepts a trailing ``Z`` on Python 3.10 too. A timestamp without an
    offset is taken as UTC.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw[-1] in "Zz":
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        # Converting can leave datetime's range, e.g. year 9999 at -05:00.
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


# --- config ----------------------------------------------------------------


def validate_config(raw: Any, source: str = "factory.yaml") -> Config:
    """Return the validated config or raise LedgerError naming the problem."""
    if not isinstance(raw, dict):
        raise LedgerError(f"{source} must be a YAML mapping")
    budget = raw.get("budget")
    if not isinstance(budget, dict):
        raise LedgerError(
            f"{source} needs a 'budget' mapping with per_run_usd and daily_usd"
        )

    numbers: dict[str, float] = {}
    for key in ("per_run_usd", "daily_usd"):
        if key not in budget or budget[key] is None:
            raise LedgerError(f"{source}: budget.{key} is missing")
        value = budget[key]
        if not _is_number(value):
            raise LedgerError(
                f"{source}: budget.{key} must be a number in US dollars "
                f"(got {value!r})"
            )
        if value <= 0:
            raise LedgerError(f"{source}: budget.{key} must be > 0 (got {value!r})")
        numbers[key] = float(value)

    if numbers["per_run_usd"] > numbers["daily_usd"]:
        raise LedgerError(
            f"{source}: budget.per_run_usd ({numbers['per_run_usd']}) must not "
            f"exceed budget.daily_usd ({numbers['daily_usd']}); no run could "
            "ever start"
        )

    max_turns = raw.get("max_turns")
    if max_turns is not None and not (_is_count(max_turns) and max_turns > 0):
        raise LedgerError(
            f"{source}: max_turns must be a positive integer (got {max_turns!r})"
        )

    autonomy = raw.get("autonomy")
    learning = parse_learning(raw.get("learning"), numbers["daily_usd"], source)
    return Config(
        per_run_usd=numbers["per_run_usd"],
        daily_usd=numbers["daily_usd"],
        max_turns=max_turns,
        autonomy=None if autonomy is None else str(autonomy),
        learning=learning,
        retry_on_dod_fail=parse_retry(raw.get("retry"), source),
    )


_RETRY_KEYS = frozenset({"on_dod_fail"})


def parse_retry(raw: Any, source: str = "factory.yaml") -> int:
    """``retry.on_dod_fail`` (0 or 1; default 1). Raises LedgerError.

    An absent or empty ``retry:`` means the default. Anything else must be
    a mapping with no key but ``on_dod_fail``, whose value is the integer
    0 or 1: a boolean, a string or a null is refused, so a typo cannot
    quietly turn the retry on or off.
    """
    where = f"{source}: retry"
    if raw is None:
        return DEFAULT_RETRY_ON_DOD_FAIL
    if not isinstance(raw, dict):
        raise LedgerError(f"{where} must be a mapping")
    unknown = sorted(str(key) for key in raw if key not in _RETRY_KEYS)
    if unknown:
        raise LedgerError(f"{where} has unknown keys: {', '.join(unknown)}")
    if "on_dod_fail" not in raw:
        return DEFAULT_RETRY_ON_DOD_FAIL
    value = raw["on_dod_fail"]
    if not _is_count(value) or value not in (0, 1):
        raise LedgerError(f"{where}.on_dod_fail must be 0 or 1 (got {value!r})")
    return value


_LEARNING_KEYS = frozenset(
    {
        "mode",
        "promote_after",
        "window_attempts",
        "window_days",
        "repeat_window",
        "area_depth",
        "max_checks_per_pr",
        "max_patterns_per_pr",
        "max_retirements_per_pr",
        "max_active_patterns",
        "retire_dormant",
        "guarded_paths",
        "test_roots",
        "test_globs",
        "edit_ignore",
        "harvest_since_days",
        "harvest_max_prs",
        "settle_minutes",
        "classify",
        "model",
        "budget",
    }
)

# key -> (default, minimum, maximum or None)
_LEARNING_INTS: dict[str, tuple[int, int, int | None]] = {
    "promote_after": (2, 1, None),
    "window_attempts": (50, 1, None),
    "window_days": (60, 1, None),
    "repeat_window": (10, 0, None),
    "area_depth": (2, 1, 4),
    "max_checks_per_pr": (3, 0, None),
    "max_patterns_per_pr": (5, 0, None),
    "max_retirements_per_pr": (5, 0, None),
    "max_active_patterns": (25, 0, None),
    "harvest_since_days": (30, 1, None),
    "harvest_max_prs": (10, 0, None),
    "settle_minutes": (10, 0, None),
}


def _learning_mode(value: Any, where: str) -> str:
    # YAML 1.1 (PyYAML) reads a bare `on` as the boolean true.
    if value is True:
        return "on"
    if isinstance(value, str) and value in LEARNING_MODES:
        return value
    raise LedgerError(
        f"{where}.mode must be one of {', '.join(LEARNING_MODES)} (got {value!r})"
    )


def _learning_bool(raw: dict[str, Any], key: str, where: str) -> bool:
    value = raw.get(key, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise LedgerError(f"{where}.{key} must be true or false (got {value!r})")
    return value


def _learning_strings(
    raw: dict[str, Any],
    key: str,
    default: tuple[str, ...],
    pattern: re.Pattern[str],
    where: str,
) -> tuple[str, ...]:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if not isinstance(value, list) or len(value) > _MAX_GLOBS:
        raise LedgerError(
            f"{where}.{key} must be a list of at most {_MAX_GLOBS} strings"
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise LedgerError(f"{where}.{key} has an invalid entry {item!r}")
        if item not in out:
            out.append(item)
    return tuple(out)


def valid_guarded_path(path: Any) -> bool:
    """True for a relative directory path a guarded path or test root may
    be: ``GUARDED_PATH_RE`` and no ``.`` or ``..`` segment."""
    return (
        isinstance(path, str)
        and bool(_GUARDED_PATH.fullmatch(path))
        and not any(segment in (".", "..") for segment in path.split("/"))
    )


def path_within(path: str, root: str) -> bool:
    """``path`` is ``root`` or lies under it, in whole segments
    (``server/tests`` is within ``server``; ``server2`` is not)."""
    return path == root or path.startswith(root + "/")


def deepest_root(path: str, roots: Iterable[str]) -> str | None:
    """The deepest of ``roots`` that the file ``path`` lies under, or None.

    The roots that hold one path are all prefixes of it, so the longest is
    the deepest."""
    best: str | None = None
    for root in roots:
        if path.startswith(root + "/") and (best is None or len(root) > len(best)):
            best = root
    return best


def effective_guarded(guarded_paths: Iterable[str]) -> tuple[str, ...]:
    """What the gate guards: ``ALWAYS_GUARDED``, then ``guarded_paths``,
    without repeats (the workflow's paths step builds the same list)."""
    out: list[str] = []
    for root in (*ALWAYS_GUARDED, *guarded_paths):
        if root not in out:
            out.append(root)
    return tuple(out)


def _learning_paths(
    raw: dict[str, Any], key: str, default: tuple[str, ...], where: str
) -> tuple[str, ...]:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if not isinstance(value, list) or len(value) > MAX_GUARDED_ENTRIES:
        raise LedgerError(
            f"{where}.{key} must be a list of at most {MAX_GUARDED_ENTRIES} directory paths"
        )
    out: list[str] = []
    for item in value:
        if not valid_guarded_path(item):
            raise LedgerError(
                f"{where}.{key} has an invalid entry {item!r}: use a relative directory "
                f"path such as server/tests, 1 to {MAX_PATH_SEGMENTS} segments of "
                "[A-Za-z0-9_.-], no '.' or '..' segment, no leading or trailing '/', "
                "no glob"
            )
        if item not in out:
            out.append(item)
    return tuple(out)


def _learning_money(budget: dict[str, Any], key: str, where: str) -> float | None:
    if key not in budget or budget[key] is None:
        return None
    value = budget[key]
    if not _is_number(value) or value <= 0:
        raise LedgerError(
            f"{where}.budget.{key} must be a number of US dollars > 0 (got {value!r})"
        )
    return float(value)


def parse_learning(
    raw: Any, budget_daily_usd: float, source: str = "factory.yaml"
) -> LearningConfig:
    """The validated ``learning:`` block; defaults for every missing key.

    Raises LedgerError naming the first invalid value.
    """
    where = f"{source}: learning"
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise LedgerError(f"{where} must be a mapping")
    unknown = sorted(str(key) for key in raw if key not in _LEARNING_KEYS)
    if unknown:
        raise LedgerError(f"{where} has unknown keys: {', '.join(unknown)}")

    ints: dict[str, int] = {}
    for key, (default, low, high) in _LEARNING_INTS.items():
        value = raw.get(key, default)
        if value is None:
            value = default
        if not _is_count(value) or value < low or (high is not None and value > high):
            bound = f"between {low} and {high}" if high is not None else f">= {low}"
            raise LedgerError(
                f"{where}.{key} must be a whole number {bound} (got {value!r})"
            )
        ints[key] = value

    mode = _learning_mode(raw.get("mode", "on"), where)

    guarded = _learning_paths(raw, "guarded_paths", DEFAULT_GUARDED_PATHS, where)

    def inside(root: str, parents: Iterable[str]) -> bool:
        return any(path_within(root, parent) for parent in parents)

    default_roots = tuple(
        root
        for root in DEFAULT_TEST_ROOTS
        if inside(root, guarded) and not inside(root, ALWAYS_GUARDED)
    )
    test_roots = _learning_paths(raw, "test_roots", default_roots, where)
    # A test root is guarded too (existing files there are restored); only
    # new files under it are kept.
    stray = [root for root in test_roots if not inside(root, guarded)]
    if stray:
        raise LedgerError(
            f"{where}.test_roots must each be a guarded path or lie under one (not: "
            f"{', '.join(stray)})"
        )
    graders = [root for root in test_roots if inside(root, ALWAYS_GUARDED)]
    if graders:
        raise LedgerError(
            f"{where}.test_roots may not be or lie under {', '.join(ALWAYS_GUARDED)}: "
            f"new files there could steer the graders (not: {', '.join(graders)})"
        )
    test_globs = _learning_strings(raw, "test_globs", DEFAULT_TEST_GLOBS, _GLOB_RE, where)
    edit_ignore = _learning_strings(
        raw, "edit_ignore", DEFAULT_EDIT_IGNORE, _GLOB_RE, where
    )

    model = raw.get("model", "")
    if model is None:
        model = ""
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
        raise LedgerError(
            f"{where}.model must match [A-Za-z0-9._:-]{{0,100}} (got {model!r})"
        )

    budget = raw.get("budget")
    if budget is None:
        budget = {}
    if not isinstance(budget, dict):
        raise LedgerError(f"{where}.budget must be a mapping")
    stray_budget = sorted(str(k) for k in budget if k not in ("per_run_usd", "daily_usd"))
    if stray_budget:
        raise LedgerError(f"{where}.budget has unknown keys: {', '.join(stray_budget)}")
    daily = _learning_money(budget, "daily_usd", where)
    per_run = _learning_money(budget, "per_run_usd", where)
    if daily is None:
        daily = min(DEFAULT_LEARN_DAILY_USD, budget_daily_usd)
    if per_run is None:
        per_run = min(DEFAULT_LEARN_PER_RUN_USD, daily)
    if per_run > daily:
        raise LedgerError(
            f"{where}.budget.per_run_usd ({per_run}) must not exceed "
            f"learning.budget.daily_usd ({daily})"
        )
    if daily > budget_daily_usd:
        raise LedgerError(
            f"{where}.budget.daily_usd ({daily}) must not exceed budget.daily_usd "
            f"({budget_daily_usd}); learn spend counts inside the global cap"
        )

    return LearningConfig(
        mode=mode,
        retire_dormant=_learning_bool(raw, "retire_dormant", where),
        guarded_paths=guarded,
        test_roots=test_roots,
        test_globs=test_globs,
        edit_ignore=edit_ignore,
        classify=_learning_bool(raw, "classify", where),
        model=model,
        per_run_usd=per_run,
        daily_usd=daily,
        **ints,
    )


def load_learning(path: Path) -> LearningConfig:
    """The effective learning config of factory.yaml at ``path``.

    The whole file is validated (the learn caps depend on the global
    budget). Raises LedgerError.
    """
    return load_config(path).learning


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise LedgerError(
            f"factory config not found: {path}. Run /cadence-factory-setup "
            "or pass --config."
        )
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            raw = yaml.safe_load(fh)
    except (yaml.YAMLError, ValueError, OverflowError, RecursionError) as exc:
        # PyYAML raises ValueError for date-like scalars such as 2001-13-45.
        raise LedgerError(f"malformed YAML in {path}: {exc}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise LedgerError(f"could not read {path}: {exc}") from exc
    return validate_config(raw, str(path))


# --- reading a Claude Code result -----------------------------------------


def _dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _json_objects(text: str) -> Iterator[dict[str, Any]]:
    """The JSON objects in a result, in order.

    Tries the whole text as one JSON value (object or array) first, then
    falls back to JSON lines, skipping lines that do not parse.

    Lines are split on ``\\n`` only. ``str.splitlines`` would also split on
    U+2028, U+2029 and U+0085, which JSON allows unescaped inside strings
    (Node's JSON.stringify writes them raw), and would drop the final
    result. Objects are yielded one at a time so a long stream is never
    held in memory as a whole.
    """
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        pass
    else:
        yield from _dicts(value)
        return
    for line in io.StringIO(text, newline="\n"):
        line = line.strip()
        if not line:
            continue
        try:
            yield from _dicts(json.loads(line))
        except (ValueError, RecursionError):
            continue


def parse_result(text: str) -> ReportedUsage:
    """Cost and turns from the LAST object that carries ``total_cost_usd``.

    If that object's cost is not a finite, non-negative number the cost
    is unknown (and will be booked at the cap); an earlier, smaller
    figure is never used in its place.
    """
    last: dict[str, Any] | None = None
    for obj in _json_objects(text):
        if "total_cost_usd" in obj:
            last = obj
    if last is None:
        return ReportedUsage(total_cost_usd=None, num_turns=None)
    cost = last.get("total_cost_usd")
    turns = last.get("num_turns")
    return ReportedUsage(
        total_cost_usd=float(cost) if _is_number(cost) and cost >= 0 else None,
        num_turns=turns if _is_count(turns) else None,
    )


def read_result_json(path: Path) -> ReportedUsage:
    """Like ``parse_result`` but tolerant of a missing or unreadable file."""
    try:
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        return parse_result(text)
    except OSError as exc:
        print(f"WARN: could not read result {path}: {exc}", file=sys.stderr)
    except MemoryError:
        # The record must still be written, so the cost becomes unknown
        # (booked at the cap) instead of the run going unrecorded.
        print(f"WARN: result {path} is too large to read", file=sys.stderr)
    return ReportedUsage(total_cost_usd=None, num_turns=None)


# --- record ----------------------------------------------------------------


def record_path(records_dir: Path, run_id: str, run_attempt: int) -> Path:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise LedgerError(
            f"run id {run_id!r} must be 1-128 characters of letters, digits, "
            "'.', '_' or '-', starting with a letter or digit"
        )
    if not (_is_count(run_attempt) and run_attempt >= 1):
        raise LedgerError(f"run attempt must be an integer >= 1 (got {run_attempt!r})")
    return records_dir / f"{run_id}-{run_attempt}.json"


def build_record(
    *,
    config: Config,
    run_id: str,
    run_attempt: int,
    issue: int | None,
    outcome: str,
    dod: str,
    usage: ReportedUsage,
    now: float,
    stage: str | None = None,
    pr: int | None = None,
    published_sha: str | None = None,
    base_sha: str | None = None,
    preflight: str | None = None,
) -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise LedgerError(f"outcome must be one of {', '.join(OUTCOMES)}")
    if dod not in DOD_RESULTS:
        raise LedgerError(f"dod must be one of {', '.join(DOD_RESULTS)}")
    if stage is not None and stage not in STAGES:
        raise LedgerError(f"stage must be one of {', '.join(STAGES)}")
    if stage == STAGE_LEARN:
        issue = None  # a learn run works across issues
    elif not (_is_count(issue) and issue >= 1):
        raise LedgerError(
            f"issue must be an integer >= 1 (got {issue!r}); only --stage learn "
            "records name no issue"
        )
    if pr is not None and not (_is_count(pr) and pr >= 1):
        raise LedgerError(f"pr must be an integer >= 1 (got {pr!r})")
    for name, sha in (("published_sha", published_sha), ("base_sha", base_sha)):
        if sha is not None and not (isinstance(sha, str) and _SHA40_RE.fullmatch(sha)):
            raise LedgerError(f"{name} must be 40 lowercase hex digits (got {sha!r})")

    # A learn run's model step (classify) runs under learning.budget's
    # per-run cap (--max-budget-usd), so an unreported learn cost is booked
    # at that cap, not at the build cap: booking a crashed classify at the
    # build cap would empty the learn pool and take a build's worth of the
    # global daily budget.
    cap = config.learning.per_run_usd if stage == STAGE_LEARN else config.per_run_usd
    cost = usage.total_cost_usd
    turns = usage.num_turns
    if cost is None:
        booked, source = cap, "cap"
    else:
        booked, source = cost, "reported"
    if preflight is not None:
        # The model job stopped at its preflight check, before the model
        # step: nothing was spent. Every other run with no known cost keeps
        # the cap (that pessimism is deliberate), so this is narrow: a
        # failed job, no cost or turns from anywhere, nothing published.
        if preflight not in PREFLIGHT_REASONS:
            raise LedgerError(
                f"preflight must be one of {', '.join(PREFLIGHT_REASONS)} (got {preflight!r})"
            )
        if outcome != "failure":
            raise LedgerError(
                "--preflight books a model job that failed at its preflight check: "
                f"--outcome must be failure (got {outcome})"
            )
        if cost is not None or turns is not None:
            raise LedgerError(
                "--preflight takes no cost or turns: the model step never ran"
            )
        if pr is not None or published_sha is not None:
            raise LedgerError("--preflight: a model job that never ran published nothing")
        cost, turns, booked, source = 0.0, 0, 0.0, f"preflight:{preflight}"
    record: dict[str, Any] = {
        "issue": issue,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "outcome": outcome,
        "dod": dod,
        "total_cost_usd": cost,
        "booked_usd": booked,
        "cost_source": source,
        "num_turns": turns,
        "per_run_cap_usd": cap,
        "recorded_at": iso_utc(to_utc(now)),
    }
    if cost is not None and cost > cap:
        record["over_cap"] = True
    if stage is not None:
        record["stage"] = stage
    if pr is not None:
        record["pr"] = pr
    if published_sha is not None:
        record["published_sha"] = published_sha
    if base_sha is not None:
        record["base_sha"] = base_sha
    return record


def write_record(records_dir: Path, record: dict[str, Any]) -> Path:
    """Write ``record`` with exclusive create. Never overwrites."""
    path = record_path(records_dir, record["run_id"], record["run_attempt"])
    payload = json.dumps(record, indent=2) + "\n"
    records_dir.mkdir(parents=True, exist_ok=True)
    try:
        fh = path.open("x", encoding="utf-8", newline="\n")
    except FileExistsError as exc:
        raise DuplicateRecord(str(path)) from exc
    try:
        with fh:
            fh.write(payload)
    except OSError:
        # We created this file, so removing a half-written copy is safe and
        # lets a retry write the whole record.
        path.unlink(missing_ok=True)
        raise
    return path


# --- check -----------------------------------------------------------------


def _booking(record: Any) -> tuple[date, Decimal] | None:
    """(UTC day, booked amount) of a record, or None if it is not usable."""
    if not isinstance(record, dict):
        return None
    booked = record.get("booked_usd")
    if not _is_number(booked) or booked < 0:
        return None
    moment = parse_iso_utc(record.get("recorded_at"))
    if moment is None:
        return None
    return moment.date(), _dec(booked)


def tally_day_pools(
    records_dir: Path, day: date, per_run_usd: float
) -> tuple[Decimal, Decimal, list[Path]]:
    """(all spend, learn spend) booked on ``day`` (UTC), and the unreadable
    records.

    Each unreadable record is booked at ``per_run_usd`` in the total: its
    day is unknown, so it is assumed to be today. Its stage is unknown too,
    so it is not counted as learn spend.
    """
    if not records_dir.exists():
        return Decimal(0), Decimal(0), []
    if not records_dir.is_dir():
        raise LedgerError(f"records dir {records_dir} is not a directory")

    spent = Decimal(0)
    learn = Decimal(0)
    unreadable: list[Path] = []
    for path in sorted(records_dir.glob("*.json")):
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            record = None
        booking = _booking(record)
        if booking is None:
            unreadable.append(path)
            spent += _dec(per_run_usd)
        elif booking[0] == day:
            spent += booking[1]
            if record.get("stage") == STAGE_LEARN:
                learn += booking[1]
    return spent, learn, unreadable


def tally_day(
    records_dir: Path, day: date, per_run_usd: float
) -> tuple[Decimal, list[Path]]:
    """Spend booked on ``day`` (UTC) and the records that could not be read."""
    spent, _, unreadable = tally_day_pools(records_dir, day, per_run_usd)
    return spent, unreadable


def check_budget(
    config: Config,
    records_dir: Path,
    in_flight: int,
    now: float,
    pool: str = "build",
) -> tuple[dict[str, Any], list[Path]]:
    if not _is_count(in_flight):
        raise LedgerError(f"--in-flight must be an integer >= 0 (got {in_flight!r})")
    if pool not in POOLS:
        raise LedgerError(f"pool must be one of {', '.join(POOLS)}")
    spent, learn_spent, unreadable = tally_day_pools(
        records_dir, to_utc(now).date(), config.per_run_usd
    )
    per_run = _dec(config.per_run_usd)
    if pool == "build":
        worst = spent + in_flight * per_run + per_run
        allowed = worst <= _dec(config.daily_usd)
    else:
        learning = config.learning
        learn_per_run = _dec(learning.per_run_usd)
        worst = spent + in_flight * per_run + learn_per_run
        allowed = worst <= _dec(config.daily_usd) and (
            learn_spent + learn_per_run <= _dec(learning.daily_usd)
        )
    report: dict[str, Any] = {
        "spent_today": float(spent),
        "in_flight": in_flight,
        "per_run_usd": config.per_run_usd,
        "daily_usd": config.daily_usd,
        "worst_case": float(worst),
        "allowed": allowed,
        "unreadable": len(unreadable),
    }
    if pool == "learn":
        report["pool"] = pool
        report["learn_spent_today"] = float(learn_spent)
        report["learn_per_run_usd"] = config.learning.per_run_usd
        report["learn_daily_usd"] = config.learning.daily_usd
    return report, unreadable


# --- CLI -------------------------------------------------------------------


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1 (got {value})")
    return value


def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value})")
    return value


def _finite_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be finite (got {text!r})")
    return value


def _cost(text: str) -> float:
    value = _finite_float(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {text!r})")
    return value


def _sha40(text: str) -> str:
    if not _SHA40_RE.fullmatch(text):
        raise argparse.ArgumentTypeError(
            f"must be 40 lowercase hex digits (got {text!r})"
        )
    return text


def _build_parser() -> argparse.ArgumentParser:
    # Accept --config / --records-dir after the subcommand too. SUPPRESS
    # keeps the top-level value unless the option is given again there.
    paths = argparse.ArgumentParser(add_help=False)
    paths.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    paths.add_argument("--records-dir", type=Path, default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"path to factory.yaml (default: {DEFAULT_CONFIG.as_posix()})",
    )
    parser.add_argument(
        "--records-dir",
        type=Path,
        default=DEFAULT_RECORDS_DIR,
        help=f"directory of run records (default: {DEFAULT_RECORDS_DIR.as_posix()})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser(
        "record",
        parents=[paths],
        help="write this run attempt's cost and outcome (never overwrites)",
    )
    rec.add_argument("--run-id", required=True, help="workflow run id")
    rec.add_argument("--run-attempt", required=True, type=_positive_int)
    rec.add_argument(
        "--issue",
        type=_positive_int,
        help="the issue the run worked on; required unless --stage learn",
    )
    rec.add_argument(
        "--stage",
        choices=STAGES,
        help="which factory stage the run was (stored only when given)",
    )
    rec.add_argument("--pr", type=_positive_int, help="the PR a build published")
    rec.add_argument(
        "--published-sha", type=_sha40, help="the commit a build published"
    )
    rec.add_argument("--base-sha", type=_sha40, help="the base commit of the run")
    rec.add_argument("--outcome", required=True, choices=OUTCOMES)
    rec.add_argument("--dod", choices=DOD_RESULTS, default="unknown")
    rec.add_argument(
        "--result-json",
        type=Path,
        help="Claude Code result (object, array or JSON lines); "
        "missing or corrupt files are tolerated",
    )
    rec.add_argument(
        "--cost-usd",
        type=_cost,
        help="reported cost in USD; overrides --result-json",
    )
    rec.add_argument(
        "--turns", type=_count, help="turns used; overrides --result-json"
    )
    rec.add_argument(
        "--preflight",
        choices=PREFLIGHT_REASONS,
        help="the model job failed at this preflight check, before its model "
        "step (no-key: ANTHROPIC_API_KEY missing or empty): book $0. Needs "
        "--outcome failure; refused with any cost, turns, result, PR or "
        "published sha",
    )
    rec.add_argument(
        "--now", type=_finite_float, help="epoch seconds (default: current time)"
    )

    chk = sub.add_parser(
        "check",
        parents=[paths],
        help="exit 1 if starting one more run could exceed the daily cap",
    )
    chk.add_argument(
        "--in-flight",
        type=_count,
        default=0,
        help="runs already dispatched and not yet recorded (default: 0)",
    )
    chk.add_argument(
        "--pool",
        choices=POOLS,
        default="build",
        help="build: one more build or spec run (default); learn: one more "
        "learn step, also capped by learning.budget",
    )
    chk.add_argument(
        "--now", type=_finite_float, help="epoch seconds (default: current time)"
    )
    return parser


def _cmd_record(args: argparse.Namespace, config: Config, now: float) -> int:
    if args.preflight is not None and args.result_json is not None:
        # Not even read: a result file means a model step ran.
        raise LedgerError("--preflight takes no --result-json: the model step never ran")
    usage = ReportedUsage(total_cost_usd=args.cost_usd, num_turns=args.turns)
    if args.result_json is not None and (
        usage.total_cost_usd is None or usage.num_turns is None
    ):
        parsed = read_result_json(args.result_json)
        usage = ReportedUsage(
            total_cost_usd=(
                usage.total_cost_usd
                if usage.total_cost_usd is not None
                else parsed.total_cost_usd
            ),
            num_turns=usage.num_turns if usage.num_turns is not None else parsed.num_turns,
        )

    if args.stage != STAGE_LEARN and args.issue is None:
        raise LedgerError("--issue is required unless --stage learn")
    record = build_record(
        config=config,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        issue=args.issue,
        outcome=args.outcome,
        dod=args.dod,
        usage=usage,
        now=now,
        stage=args.stage,
        pr=args.pr,
        published_sha=args.published_sha,
        base_sha=args.base_sha,
        preflight=args.preflight,
    )
    path = write_record(args.records_dir, record)

    if args.preflight is not None:
        print(
            f"NOTE: run {args.run_id} attempt {args.run_attempt} stopped at its "
            f"preflight check ({args.preflight}) before the model step; booked $0.00",
            file=sys.stderr,
        )
    if record["cost_source"] == "cap":
        print(
            f"WARN: no cost reported for run {args.run_id} attempt "
            f"{args.run_attempt}; booked the full per-run cap "
            f"${record['per_run_cap_usd']:.2f}",
            file=sys.stderr,
        )
    if record.get("over_cap"):
        print(
            f"WARN: reported cost ${record['total_cost_usd']:.2f} exceeds the "
            f"per-run cap ${record['per_run_cap_usd']:.2f}; booked as reported",
            file=sys.stderr,
        )
    print(f"recorded {path}", file=sys.stderr)
    print(json.dumps(record))
    return EXIT_OK


def _cmd_check(args: argparse.Namespace, config: Config, now: float) -> int:
    report, unreadable = check_budget(
        config, args.records_dir, args.in_flight, now, pool=args.pool
    )
    for path in unreadable:
        print(
            f"WARN: unreadable ledger record {path}; booked at the full "
            f"per-run cap ${config.per_run_usd:.2f} until it is repaired",
            file=sys.stderr,
        )
    print(json.dumps(report))
    if report["allowed"]:
        return EXIT_OK
    if args.pool == "learn":
        learning = config.learning
        print(
            f"over budget: one more learn step (${learning.per_run_usd:.2f}) "
            f"needs a worst case of ${report['worst_case']:.2f} within the daily "
            f"cap ${config.daily_usd:.2f}, and learn spend today "
            f"(${report['learn_spent_today']:.2f}) plus the step within the learn "
            f"cap ${learning.daily_usd:.2f}. Not classifying.",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    print(
        f"over budget: worst case ${report['worst_case']:.2f} "
        f"(spent today ${report['spent_today']:.2f} + {report['in_flight']} "
        f"in flight + 1 new, at ${config.per_run_usd:.2f} each) exceeds the "
        f"daily cap ${config.daily_usd:.2f}. Not dispatching.",
        file=sys.stderr,
    )
    return EXIT_BLOCKED


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    now = args.now if args.now is not None else time.time()
    try:
        config = load_config(args.config)
        if args.command == "record":
            return _cmd_record(args, config, now)
        return _cmd_check(args, config, now)
    except DuplicateRecord as exc:
        print(
            f"ERROR: ledger record already exists: {exc}. Records are never "
            "overwritten; a retried run gets a new --run-attempt.",
            file=sys.stderr,
        )
        return EXIT_BLOCKED
    except LedgerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except Exception:  # noqa: BLE001 - see below
        # An uncaught exception would exit 1, which this contract reserves
        # for "over budget" and "record already exists". A workflow that
        # treats an existing record as done would then lose the run's cost.
        traceback.print_exc()
        print("ERROR: internal error in ledger.py (see traceback)", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    sys.exit(main())
