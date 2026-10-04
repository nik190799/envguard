#!/usr/bin/env python3
"""Cadence factory reconciler: an hourly sweep that unsticks work without paying for builds.

Webhooks and scheduled runs can be delayed or dropped, and a runner can
die mid-job, so the factory workflow's hourly schedule runs this sweep.
It re-reads the real state from GitHub and from the claim refs instead of
trusting that every event arrived. It makes no model call and it never
starts a build: a build spends from the user's budget, so only a human's
``/approve`` starts one.

Contract:
    python tool/reconcile.py --repo OWNER/REPO [--dry-run]
        [--claim-timeout-minutes 120] [--spec-retry-minutes 60] [--now EPOCH]

    Reads, through the ``gh`` CLI (authenticated by ``GH_TOKEN``) and
    ``tool/claim.py`` (run as a subprocess on the clone at ``--clone``):
      - every claim, and the claims older than the claim timeout
        (``claim.py stale``);
      - for each stale claim, its workflow run (``actions/runs/<run_id>``);
      - open issues labelled ``factory`` or ``building``;
      - ``cadence-factory.yml`` runs created in the spec-retry window, and
        every run still queued, waiting or in progress;
      - for an issue a rule could act on, its comments and its open pull
        requests from ``cadence/issue-<N>``;
      - for a spec-retry candidate, who last added the ``factory`` label
        (the issue's events) and that person's repository permission.

    Then applies these rules:
    1. ``release_claim``: a claim older than the claim timeout whose run
       is gone (completed, or 404) is released with
       ``claim.py release --issue N --run-id reconciler --force --sha SHA``.
       A claim whose run is alive, or cannot be checked, is left alone.
    2. ``retry_spec``: an issue labelled ``factory`` and none of
       ``spec-ready``, ``building``, ``pr-open``, ``dod-failed``,
       ``needs-human``, not updated for ``--spec-retry-minutes``, with no
       factory run for it in that window, no comment carrying
       ``<!-- cadence:reconcile-spec-retry -->``, and whose ``factory``
       label was last added by a user who has write, maintain or admin
       access now, gets a comment with that marker, then ``gh workflow
       run cadence-factory.yml -f issue=N -f stage=spec``. The marker is
       posted first, so a spec is retried at most once per issue even if
       the dispatch fails. The dispatch runs as the factory App, which
       ``tool/route.py`` lets through for ``spec`` only, without a
       permission check of its own: the labeller check here is what
       stops a triage user, or an issue template that applies
       ``factory``, from getting a spec the label event itself refused.
    3. ``flag_stuck_build``: an issue labelled ``building`` and none of
       ``pr-open``, ``dod-failed``, ``needs-human``, not updated for the
       claim timeout, with no claim, no queued or running factory run and
       no open pull request from ``cadence/issue-N``, gets ONE comment
       (marker ``<!-- cadence:reconcile-stuck-build -->``) telling a
       maintainer how to start it again (swap ``building`` for
       ``spec-ready``, then reply ``/approve``) or to close it, then the
       ``needs-human`` label. The build is never re-run automatically.
    4. Anything else: no action. So is any rule whose inputs could not be
       read: unknown state never triggers an action.

    Prints one JSON line per action, ``{"action", "issue", "detail",
    "executed"}``, in the order above. ``executed`` is false for every
    action under ``--dry-run``, and for one that failed or that claim.py
    refused; ``detail`` says which. Notes and errors go to stderr.

    Further options: ``--workflow`` (default ``cadence-factory.yml``),
    ``--clone`` (the clone claim.py works in, default ``.``), ``--remote``
    (default ``origin``) and ``--claim-tool`` (default: ``claim.py`` next
    to this file).

Which issue a run belongs to:
    The Actions API does not say which issue a run was for, so runs are
    matched by their ``display_title``. Set the workflow's run name to
    ``cadence-factory #<issue>``::

        run-name: "cadence-factory #${{ github.event.issue.number || inputs.issue || (inputs.stage == 'learn' && 'learn') || 'sweep' }}"

    A title of that form names its issue. The titles
    ``cadence-factory #sweep`` (this sweep, or a dispatch without an issue)
    and ``cadence-factory #learn`` (the learning loop, docs/LEARNING.md)
    name none, whatever the event. Without the run name, an ``issues`` or
    ``issue_comment`` run is titled after its issue, so a title equal to
    an open issue's title names that issue, and one equal to none names
    none. Scheduled runs are this sweep and name none. Any other run that
    names no issue, such as a ``workflow_dispatch`` run without the run
    name, counts as a run for EVERY issue, which only ever holds rules 2
    and 3 back.

Token permissions:
    ``gh`` needs ``issues: write`` (comments, labels, issue events),
    ``actions: write`` (dispatch; reading runs), ``pull-requests: read``
    and ``metadata: read`` (collaborator permissions). claim.py pushes ref
    deletions through the clone's remote, so that remote's credentials
    must be allowed to delete ``cadence/claim/*`` branches.

Exit codes:
    0   ok, including "nothing to do" and a release claim.py refused
        because a newer run took the claim
    1   a gh or claim.py call failed; every other action was still
        attempted
    2   bad input, or an internal error

Usage:
    python tool/reconcile.py --repo "$GITHUB_REPOSITORY"
    python tool/reconcile.py --repo octo/app --dry-run --now 1790000000

Design notes:
    - Three parts: ``Client`` wraps ``gh`` and claim.py (argument lists,
      never a shell); ``plan`` is pure and decides from a ``Snapshot``;
      ``execute`` performs the planned actions. Tests drive ``plan`` with
      fake snapshots and ``execute`` with a fake client.
    - A run lookup that returns 404 counts as "run gone" only when the
      runs listing in the same sweep worked: on a private repository a
      token without ``actions: read`` also gets 404, and must not make
      every claim look abandoned. A claim whose run appears among the
      queued or running runs is never released.
    - An issue that still had a claim when the sweep started is not
      flagged as stuck in that sweep, even if rule 1 releases the claim:
      the next sweep flags it if it is still stuck.
    - The marker comment is the record that an action happened, so a
      label or dispatch that fails after it is reported (exit 1) but not
      retried. The ``needs-human`` label comes after the stuck-build
      comment: if the comment fails, the label is not added and the next
      sweep tries both again.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlencode

# --- Shared names ------------------------------------------------------------

LABEL_FACTORY = "factory"
LABEL_SPEC_READY = "spec-ready"
LABEL_BUILDING = "building"
LABEL_PR_OPEN = "pr-open"
LABEL_DOD_FAILED = "dod-failed"
LABEL_NEEDS_HUMAN = "needs-human"

# An issue carrying any of these is past the spec step (or a human owns it).
PAST_SPEC_LABELS = frozenset(
    {LABEL_SPEC_READY, LABEL_BUILDING, LABEL_PR_OPEN, LABEL_DOD_FAILED, LABEL_NEEDS_HUMAN}
)
# A building issue carrying any of these has a result, or a human owns it.
PAST_BUILD_LABELS = frozenset({LABEL_PR_OPEN, LABEL_DOD_FAILED, LABEL_NEEDS_HUMAN})
# Repository permissions that may start factory work (route.py's rule).
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})

SPEC_RETRY_MARKER = "<!-- cadence:reconcile-spec-retry -->"
STUCK_BUILD_MARKER = "<!-- cadence:reconcile-stuck-build -->"
MARKERS = (SPEC_RETRY_MARKER, STUCK_BUILD_MARKER)

WORK_BRANCH_PREFIX = "cadence/issue-"
DEFAULT_WORKFLOW = "cadence-factory.yml"
RECONCILER_RUN_ID = "reconciler"  # the --run-id given to claim.py release

ACTION_RELEASE = "release_claim"
ACTION_RETRY_SPEC = "retry_spec"
ACTION_FLAG_STUCK = "flag_stuck_build"

# Statuses a workflow run passes through before "completed".
ACTIVE_RUN_STATUSES = ("requested", "queued", "pending", "waiting", "in_progress")

# What a stale claim's run lookup can find.
RUN_ALIVE = "alive"
RUN_COMPLETED = "completed"
RUN_MISSING = "missing"  # 404
RUN_GONE = frozenset({RUN_COMPLETED, RUN_MISSING})

EXIT_OK = 0
EXIT_CALL_FAILED = 1
EXIT_ERROR = 2

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_EPOCH = 253_402_300_799  # 9999-12-31T23:59:59Z, as in claim.py

# GitHub owner and repository names. Each part also must not be "." or "..",
# since both end up in API paths.
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_WORKFLOW_RE = re.compile(r"[A-Za-z0-9_.-]{1,200}\.ya?ml")
_RUN_NUMBER_RE = re.compile(r"[1-9][0-9]{0,19}")
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_RUN_NAME_RE = re.compile(r"cadence-factory\b[^#\n]*#([1-9][0-9]{0,9})\b")
# Runs titled like this are the sweep or the learning loop: never an issue's.
_NO_ISSUE_RUN_NAME_RE = re.compile(r"cadence-factory #(?:sweep|learn)")
# A human GitHub login; it goes into an API path, so nothing else passes.
_USER_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_HTTP_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)")

# One gh or claim.py call may take this long before it counts as failed.
_CALL_TIMEOUT_SECONDS = 180


# --- Data --------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    claim_timeout_minutes: float = 120.0
    spec_retry_minutes: float = 60.0


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    labels: frozenset[str]
    updated_at: int | None  # epoch seconds; None if GitHub's value was unreadable
    # Filled in only for issues a rule could act on. None means "not read".
    markers: frozenset[str] | None = None
    has_open_pr: bool | None = None
    # Spec-retry candidates only: does whoever last added `factory` have
    # write access now? False also when nobody can be named.
    labeler_can_write: bool | None = None


@dataclass(frozen=True)
class ClaimInfo:
    """One line of ``claim.py stale`` output."""

    issue: int
    run_id: str | None
    sha: str
    age_minutes: int


@dataclass(frozen=True)
class Run:
    """A cadence-factory workflow run, as listed by the Actions API."""

    id: int | None
    event: str
    status: str
    created_at: int | None
    display_title: str

    @property
    def active(self) -> bool:
        # Anything not known to be finished counts as running.
        return self.status != "completed"


@dataclass(frozen=True)
class Snapshot:
    """What the sweep read. A None field could not be read."""

    issues: tuple[Issue, ...] | None = None
    stale_claims: tuple[ClaimInfo, ...] | None = None
    claimed_issues: frozenset[int] | None = None
    # Stale claims' run ids -> RUN_ALIVE / RUN_COMPLETED / RUN_MISSING.
    # A run id that is absent could not be checked.
    run_status: Mapping[str, str] = field(default_factory=dict)
    runs: tuple[Run, ...] | None = None


@dataclass(frozen=True)
class Action:
    kind: str
    issue: int
    detail: str
    sha: str | None = None  # release_claim: the claim commit to delete
    post_comment: bool = True  # flag_stuck_build: False if already posted


@dataclass(frozen=True)
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


class CallFailed(Exception):
    """A gh or claim.py call failed. The sweep goes on and exits 1."""


class GhError(CallFailed):
    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


Runner = Callable[[Sequence[str], "str | None"], CmdResult]


# --- Pure helpers ------------------------------------------------------------


def iso_utc(epoch: int) -> str:
    return (_EPOCH + timedelta(seconds=epoch)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: Any) -> int | None:
    """Epoch seconds for a GitHub timestamp, or None if it is not one."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw[-1] in "Zz":  # Python 3.10's fromisoformat rejects "Z"
        raw = raw[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(raw)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return math.floor((moment - _EPOCH).total_seconds())
    except (ValueError, OverflowError):
        return None


def valid_repo(text: str) -> bool:
    if not _REPO_RE.fullmatch(text):
        return False
    return all(part not in (".", "..") for part in text.split("/"))


def parse_json_stream(text: str) -> list[Any]:
    """Every JSON value in ``text``, in order.

    ``gh api --paginate`` prints each page as its own JSON array or
    object, back to back, so the output as a whole is not one JSON value.
    Raises ValueError if any part is not JSON.
    """
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    end = len(text)
    while True:
        while index < end and text[index] in " \t\r\n\ufeff":
            index += 1
        if index >= end:
            return values
        value, index = decoder.raw_decode(text, index)
        values.append(value)


def page_items(pages: Iterable[Any], key: str | None = None) -> list[dict[str, Any]]:
    """The objects in paginated results: each page is a list of them, or,
    with ``key``, an object holding that list under ``key``."""
    items: list[dict[str, Any]] = []
    for page in pages:
        if key is not None:
            page = page.get(key) if isinstance(page, dict) else None
        if isinstance(page, list):
            items.extend(item for item in page if isinstance(item, dict))
    return items


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def issue_from_api(obj: Any) -> Issue | None:
    """An open issue from the issues API; None for a pull request or junk."""
    if not isinstance(obj, dict) or "pull_request" in obj:
        return None
    number = _positive_int(obj.get("number"))
    if number is None:
        return None
    labels: set[str] = set()
    for label in obj.get("labels") or ():
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            labels.add(name)
    title = obj.get("title")
    return Issue(
        number=number,
        title=title if isinstance(title, str) else "",
        labels=frozenset(labels),
        updated_at=parse_time(obj.get("updated_at")),
    )


def run_from_api(obj: dict[str, Any]) -> Run:
    """A workflow run from the Actions API. Missing fields make it count
    as running and as a run for every issue, never the other way round."""

    def text(key: str) -> str:
        value = obj.get(key)
        return value if isinstance(value, str) else ""

    return Run(
        id=_positive_int(obj.get("id")),
        event=text("event"),
        status=text("status"),
        created_at=parse_time(obj.get("created_at")),
        display_title=text("display_title"),
    )


def parse_claim_lines(text: str) -> list[ClaimInfo]:
    """Parse ``claim.py stale`` output. Raises ValueError on a bad line."""
    claims: list[ClaimInfo] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError(f"not a claim: {line!r}")
        issue = _positive_int(data.get("issue"))
        sha = data.get("sha")
        run_id = data.get("run_id")
        age = data.get("age_minutes")
        if issue is None or not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            raise ValueError(f"not a claim: {line!r}")
        if run_id is not None and not isinstance(run_id, str):
            raise ValueError(f"not a claim: {line!r}")
        if isinstance(age, bool) or not isinstance(age, int) or age < 0:
            age = 0
        claims.append(ClaimInfo(issue=issue, run_id=run_id, sha=sha, age_minutes=age))
    return claims


def markers_in(comments: Iterable[dict[str, Any]]) -> frozenset[str]:
    found: set[str] = set()
    for comment in comments:
        body = comment.get("body")
        if isinstance(body, str):
            found.update(marker for marker in MARKERS if marker in body)
    return frozenset(found)


def factory_labeler(events: Iterable[dict[str, Any]]) -> str | None:
    """The login that last added the ``factory`` label, from an issue's events.

    None when no such event exists, or when its actor is a bot or not a
    plain user login: nobody with a permission to check.
    """
    latest: tuple[int, int, dict[str, Any]] | None = None
    for order, event in enumerate(events):
        if event.get("event") != "labeled":
            continue
        label = event.get("label")
        if not isinstance(label, dict) or label.get("name") != LABEL_FACTORY:
            continue
        when = parse_time(event.get("created_at"))
        key = (when if when is not None else -1, order)
        if latest is None or key >= latest[:2]:
            latest = (key[0], key[1], event)
    if latest is None:
        return None
    actor = latest[2].get("actor")
    if not isinstance(actor, dict):
        return None
    login = actor.get("login")
    kind = actor.get("type")
    if not isinstance(login, str) or not _USER_LOGIN_RE.fullmatch(login):
        return None  # includes every "<app>[bot]" login
    if isinstance(kind, str) and kind.casefold() == "bot":
        return None
    return login


def http_status(stderr: str, stdout: str) -> int | None:
    """The HTTP status of a failed ``gh api`` call, if it says.

    gh prints ``gh: Not Found (HTTP 404)`` on stderr and the error body,
    which has a ``status`` field, on stdout.
    """
    match = _HTTP_STATUS_RE.search(stderr)
    if match:
        return int(match.group(1))
    try:
        body = json.loads(stdout)
    except ValueError:
        return None
    status = body.get("status") if isinstance(body, dict) else None
    if isinstance(status, str) and status.isdigit():
        return int(status)
    return None


def run_targets(run: Run, titles: Mapping[int, str]) -> frozenset[int] | None:
    """The issues ``run`` may have been for; None means "could be any"."""
    if run.event == "schedule":
        return frozenset()  # the reconciler's own sweep
    title = run.display_title.strip()
    if _NO_ISSUE_RUN_NAME_RE.fullmatch(title):
        return frozenset()  # a sweep or a learn run, however it was started
    found: set[int] = set()
    match = _RUN_NAME_RE.match(title)
    if match:
        found.add(int(match.group(1)))
    if title:
        found.update(n for n, t in titles.items() if t.strip() == title)
    if found:
        return frozenset(found)
    if run.event in ("issues", "issue_comment"):
        # Titled after an issue the sweep is not looking at.
        return frozenset()
    return None


def _older_than(updated_at: int | None, now: int, minutes: float) -> bool:
    return updated_at is not None and now - updated_at > minutes * 60


def _age_minutes(since: int | None, now: int) -> int:
    return max(0, (now - since) // 60) if since is not None else 0


def spec_retry_gate(issue: Issue, now: int, settings: Settings) -> bool:
    """Rule 2's label and age conditions, which need no further reads."""
    return (
        LABEL_FACTORY in issue.labels
        and not issue.labels & PAST_SPEC_LABELS
        and _older_than(issue.updated_at, now, settings.spec_retry_minutes)
    )


def stuck_build_gate(issue: Issue, now: int, settings: Settings) -> bool:
    """Rule 3's label and age conditions, which need no further reads."""
    return (
        LABEL_BUILDING in issue.labels
        and not issue.labels & PAST_BUILD_LABELS
        and _older_than(issue.updated_at, now, settings.claim_timeout_minutes)
    )


def spec_retry_comment(issue: int) -> str:
    return (
        f"{SPEC_RETRY_MARKER}\n"
        "Cadence's hourly check found no spec for this issue, so it is running "
        "the spec step once more. It will not retry again. If no spec appears, "
        f"remove the `{LABEL_FACTORY}` label and add it back."
    )


def stuck_build_comment(issue: int) -> str:
    return (
        f"{STUCK_BUILD_MARKER}\n"
        f"This issue is labelled `{LABEL_BUILDING}`, but Cadence's hourly check "
        "found no build running for it, no claim on it and no open pull request "
        f"from `{WORK_BRANCH_PREFIX}{issue}`. The build seems to have stopped "
        "without reporting back. Cadence does not restart builds by itself, "
        "because each one spends from your budget.\n\n"
        f"To try again, remove the `{LABEL_BUILDING}` label, add `{LABEL_SPEC_READY}`, "
        "then reply `/approve`. If the issue is no longer wanted, close it."
    )


# --- Planner -----------------------------------------------------------------


def _claim_run_gone(claim: ClaimInfo, snapshot: Snapshot) -> bool:
    if claim.run_id is None or snapshot.run_status.get(claim.run_id) not in RUN_GONE:
        return False
    if snapshot.runs is None:
        # The listing proves the token can see runs; without it a 404 may
        # only mean "not allowed to look".
        return False
    return not any(
        run.active and run.id is not None and str(run.id) == claim.run_id
        for run in snapshot.runs
    )


def plan(snapshot: Snapshot, now: int, settings: Settings) -> list[Action]:
    """Decide the sweep's actions from what it read. Pure."""
    releases: list[Action] = []
    for claim in sorted(snapshot.stale_claims or (), key=lambda c: (c.issue, c.sha)):
        if not _claim_run_gone(claim, snapshot):
            continue
        how = snapshot.run_status[claim.run_id or ""]
        releases.append(
            Action(
                kind=ACTION_RELEASE,
                issue=claim.issue,
                sha=claim.sha,
                detail=(
                    f"claim {claim.sha[:12]} is {claim.age_minutes} min old and "
                    f"its run {claim.run_id} is "
                    f"{'completed' if how == RUN_COMPLETED else 'not found'}"
                ),
            )
        )

    retries: list[Action] = []
    flags: list[Action] = []
    if snapshot.issues is not None and snapshot.runs is not None:
        titles = {issue.number: issue.title for issue in snapshot.issues}
        targeted = [(run, run_targets(run, titles)) for run in snapshot.runs]
        for issue in sorted(snapshot.issues, key=lambda i: i.number):
            mine = [
                run
                for run, targets in targeted
                if targets is None or issue.number in targets
            ]
            retry = _plan_spec_retry(issue, mine, now, settings)
            if retry is not None:
                retries.append(retry)
            flag = _plan_stuck_flag(issue, mine, snapshot, now, settings)
            if flag is not None:
                flags.append(flag)
    return releases + retries + flags


def _plan_spec_retry(
    issue: Issue, runs: Sequence[Run], now: int, settings: Settings
) -> Action | None:
    if not spec_retry_gate(issue, now, settings):
        return None
    if issue.markers is None or SPEC_RETRY_MARKER in issue.markers:
        return None
    if issue.labeler_can_write is not True:
        # The retry runs as the App, past route.py's permission check, so
        # it may only repeat what a user with write access asked for.
        return None
    window_start = now - settings.spec_retry_minutes * 60
    for run in runs:
        if run.active or run.created_at is None or run.created_at >= window_start:
            return None
    return Action(
        kind=ACTION_RETRY_SPEC,
        issue=issue.number,
        detail=(
            f"labelled {LABEL_FACTORY} with no spec, not updated for "
            f"{_age_minutes(issue.updated_at, now)} min and no factory run for it "
            f"in the last {settings.spec_retry_minutes:g} min; posting the "
            "retry marker, then dispatching stage=spec"
        ),
    )


def _plan_stuck_flag(
    issue: Issue,
    runs: Sequence[Run],
    snapshot: Snapshot,
    now: int,
    settings: Settings,
) -> Action | None:
    if not stuck_build_gate(issue, now, settings):
        return None
    if snapshot.claimed_issues is None or issue.number in snapshot.claimed_issues:
        return None
    if issue.has_open_pr is not False or issue.markers is None:
        return None
    if any(run.active for run in runs):
        return None
    post = STUCK_BUILD_MARKER not in issue.markers
    then = (
        f"commenting, then adding {LABEL_NEEDS_HUMAN}"
        if post
        else f"stuck-build comment already posted; adding {LABEL_NEEDS_HUMAN}"
    )
    return Action(
        kind=ACTION_FLAG_STUCK,
        issue=issue.number,
        post_comment=post,
        detail=(
            f"labelled {LABEL_BUILDING} with no claim, no queued or running "
            f"factory run and no open PR from {WORK_BRANCH_PREFIX}{issue.number}, "
            f"not updated for {_age_minutes(issue.updated_at, now)} min; {then}"
        ),
    )


# --- GitHub and claim.py -----------------------------------------------------


def run_command(args: Sequence[str], input_text: str | None = None) -> CmdResult:
    """Run one command from an argument list, never through a shell."""
    try:
        proc = subprocess.run(
            list(args),
            input=input_text,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "GH_PROMPT_DISABLED": "1"},
            timeout=_CALL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CmdResult(124, "", f"{args[0]} timed out after {_CALL_TIMEOUT_SECONDS}s")
    except OSError as exc:
        return CmdResult(127, "", f"could not run {args[0]}: {exc}")
    return CmdResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def _first_lines(text: str, limit: int = 3) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " | ".join(lines[:limit])


def gh_api_args(
    path: str, *, method: str = "GET", paginate: bool = False, with_input: bool = False
) -> list[str]:
    args = ["api"]
    if method != "GET":
        args += ["--method", method]
    if paginate:
        args.append("--paginate")
    if with_input:
        args += ["--input", "-"]
    args.append(path)
    return args


def issues_path(repo: str, label: str) -> str:
    query = urlencode({"state": "open", "labels": label, "per_page": 100})
    return f"repos/{repo}/issues?{query}"


def runs_path(
    repo: str,
    workflow: str,
    *,
    created_since: int | None = None,
    status: str | None = None,
) -> str:
    params: dict[str, Any] = {}
    if created_since is not None:
        params["created"] = f">={iso_utc(created_since)}"
    if status is not None:
        params["status"] = status
    params["per_page"] = 100
    return f"repos/{repo}/actions/workflows/{workflow}/runs?{urlencode(params)}"


def open_prs_path(repo: str, issue: int) -> str:
    owner = repo.split("/", 1)[0]
    query = urlencode(
        {"state": "open", "head": f"{owner}:{WORK_BRANCH_PREFIX}{issue}", "per_page": 100}
    )
    return f"repos/{repo}/pulls?{query}"


def dispatch_args(repo: str, workflow: str, issue: int) -> list[str]:
    return [
        "workflow",
        "run",
        workflow,
        "--repo",
        repo,
        "-f",
        f"issue={issue}",
        "-f",
        "stage=spec",
    ]


class Client:
    """GitHub through ``gh``, and the claims through ``claim.py``."""

    def __init__(
        self,
        repo: str,
        *,
        claim_tool: Path,
        clone: Path,
        remote: str = "origin",
        workflow: str = DEFAULT_WORKFLOW,
        runner: Runner = run_command,
        gh: str = "gh",
        python: str = sys.executable,
    ) -> None:
        self.repo = repo
        self.claim_tool = claim_tool
        self.clone = clone
        self.remote = remote
        self.workflow = workflow
        self.runner = runner
        self.gh = gh
        self.python = python

    # gh ---------------------------------------------------------------------

    def _gh(self, args: list[str], input_text: str | None = None) -> CmdResult:
        return self.runner([self.gh, *args], input_text)

    def _api(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Any = None,
        paginate: bool = False,
    ) -> list[Any]:
        args = gh_api_args(path, method=method, paginate=paginate, with_input=body is not None)
        result = self._gh(args, json.dumps(body) if body is not None else None)
        what = f"gh api {'--method ' + method + ' ' if method != 'GET' else ''}{path}"
        if result.returncode != 0:
            status = http_status(result.stderr, result.stdout)
            detail = _first_lines(result.stderr) or _first_lines(result.stdout)
            raise GhError(f"{what} failed (exit {result.returncode}): {detail}", status)
        try:
            return parse_json_stream(result.stdout)
        except ValueError as exc:
            raise GhError(f"{what} printed something that is not JSON: {exc}") from exc

    def list_issues(self, label: str) -> list[Issue]:
        pages = self._api(issues_path(self.repo, label), paginate=True)
        issues = (issue_from_api(item) for item in page_items(pages))
        return [issue for issue in issues if issue is not None]

    def list_runs(
        self, *, created_since: int | None = None, status: str | None = None
    ) -> list[Run]:
        path = runs_path(
            self.repo, self.workflow, created_since=created_since, status=status
        )
        pages = self._api(path, paginate=True)
        return [run_from_api(item) for item in page_items(pages, "workflow_runs")]

    def run_status(self, run_id: str) -> str:
        if not _RUN_NUMBER_RE.fullmatch(run_id):
            raise CallFailed(f"run id {run_id!r} is not a workflow run number")
        try:
            pages = self._api(f"repos/{self.repo}/actions/runs/{run_id}")
        except GhError as exc:
            if exc.http_status == 404:
                return RUN_MISSING
            raise
        if not pages or not isinstance(pages[0], dict):
            raise GhError(f"run {run_id}: unexpected response from gh api")
        return RUN_COMPLETED if pages[0].get("status") == "completed" else RUN_ALIVE

    def issue_markers(self, issue: int) -> frozenset[str]:
        path = f"repos/{self.repo}/issues/{issue}/comments?per_page=100"
        return markers_in(page_items(self._api(path, paginate=True)))

    def has_open_pr(self, issue: int) -> bool:
        return bool(page_items(self._api(open_prs_path(self.repo, issue), paginate=True)))

    def factory_labeler_can_write(self, issue: int) -> bool:
        """Whether whoever last added ``factory`` has write access now."""
        path = f"repos/{self.repo}/issues/{issue}/events?per_page=100"
        login = factory_labeler(page_items(self._api(path, paginate=True)))
        if login is None:
            return False
        try:
            pages = self._api(f"repos/{self.repo}/collaborators/{login}/permission")
        except GhError as exc:
            if exc.http_status == 404:  # not a collaborator, or no such user
                return False
            raise
        body = pages[0] if pages and isinstance(pages[0], dict) else {}
        roles = {body.get("permission"), body.get("role_name")}
        return bool(roles & WRITE_PERMISSIONS)

    def post_comment(self, issue: int, body: str) -> None:
        self._api(
            f"repos/{self.repo}/issues/{issue}/comments", method="POST", body={"body": body}
        )

    def add_labels(self, issue: int, labels: Sequence[str]) -> None:
        # The REST endpoint creates a label the repository does not have yet.
        self._api(
            f"repos/{self.repo}/issues/{issue}/labels",
            method="POST",
            body={"labels": list(labels)},
        )

    def dispatch_spec(self, issue: int) -> None:
        args = dispatch_args(self.repo, self.workflow, issue)
        result = self._gh(args)
        if result.returncode != 0:
            detail = _first_lines(result.stderr) or _first_lines(result.stdout)
            raise GhError(
                f"gh {' '.join(args)} failed (exit {result.returncode}): {detail}",
                http_status(result.stderr, result.stdout),
            )

    # claim.py ---------------------------------------------------------------

    def _claim(self, *args: str) -> tuple[list[str], CmdResult]:
        command = [
            self.python,
            str(self.claim_tool),
            args[0],
            "--repo",
            str(self.clone),
            "--remote",
            self.remote,
            *args[1:],
        ]
        return command, self.runner(command, None)

    def stale_claims(self, timeout_minutes: float, now: int) -> list[ClaimInfo]:
        _, result = self._claim(
            "stale", "--timeout-minutes", str(timeout_minutes), "--now", str(now)
        )
        what = f"claim.py stale --timeout-minutes {timeout_minutes:g}"
        if result.returncode != 0:
            raise CallFailed(
                f"{what} failed (exit {result.returncode}): {_first_lines(result.stderr)}"
            )
        try:
            return parse_claim_lines(result.stdout)
        except ValueError as exc:
            raise CallFailed(f"{what} printed an unexpected line: {exc}") from exc

    def release_claim(self, issue: int, sha: str) -> str:
        """``released``, ``absent`` (already gone) or ``kept`` (it changed)."""
        _, result = self._claim(
            "release",
            "--issue",
            str(issue),
            "--run-id",
            RECONCILER_RUN_ID,
            "--force",
            "--sha",
            sha,
        )
        if result.returncode == 1:
            return "kept"
        if result.returncode != 0:
            raise CallFailed(
                f"claim.py release --issue {issue} failed "
                f"(exit {result.returncode}): {_first_lines(result.stderr)}"
            )
        released = False
        for line in result.stdout.splitlines():
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and "released" in data:
                released = data["released"] is True
        return "released" if released else "absent"


# --- Collect -----------------------------------------------------------------


def _note(message: str) -> None:
    print(f"note: {message}", file=sys.stderr)


def collect(
    client: Any,
    now: int,
    settings: Settings,
    *,
    note: Callable[[str], None] = _note,
) -> tuple[Snapshot, list[str]]:
    """Read everything ``plan`` needs. Returns the snapshot and the calls
    that failed; whatever a failed call would have read stays None."""
    failures: list[str] = []

    def attempt(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except CallFailed as exc:
            failures.append(str(exc))
            return None

    stale = attempt(lambda: client.stale_claims(settings.claim_timeout_minutes, now))
    every = attempt(lambda: client.stale_claims(0, now))
    claimed: frozenset[int] | None = None
    if every is not None:
        claimed = frozenset(c.issue for c in every) | frozenset(
            c.issue for c in stale or ()
        )

    run_status: dict[str, str] = {}
    for claim in stale or ():
        if claim.run_id is None or not _RUN_NUMBER_RE.fullmatch(claim.run_id):
            note(
                f"issue {claim.issue}'s stale claim {claim.sha[:12]} names no "
                f"workflow run ({claim.run_id!r}); left in place. Remove it by "
                "hand once you know nothing holds it."
            )
            continue
        if claim.run_id in run_status:
            continue
        status = attempt(lambda run_id=claim.run_id: client.run_status(run_id))
        if status is not None:
            run_status[claim.run_id] = status

    issues: list[Issue] | None = []
    for label in (LABEL_FACTORY, LABEL_BUILDING):
        listed = attempt(lambda label=label: client.list_issues(label))
        if listed is None:
            issues = None
        elif issues is not None:
            issues.extend(listed)

    runs: list[Run] | None = []
    window_start = math.floor(now - settings.spec_retry_minutes * 60)
    listings: list[Callable[[], Any]] = [
        lambda: client.list_runs(created_since=window_start)
    ]
    listings += [
        lambda status=status: client.list_runs(status=status)
        for status in ACTIVE_RUN_STATUSES
    ]
    for listing in listings:
        listed = attempt(listing)
        if listed is None:
            runs = None
        elif runs is not None:
            runs.extend(listed)

    detailed: tuple[Issue, ...] | None = None
    if issues is not None:
        by_number: dict[int, Issue] = {}
        for issue in issues:
            by_number.setdefault(issue.number, issue)
        enriched: list[Issue] = []
        for number in sorted(by_number):
            issue = by_number[number]
            spec_gate = spec_retry_gate(issue, now, settings)
            stuck_gate = stuck_build_gate(issue, now, settings)
            if spec_gate or stuck_gate:
                markers = attempt(lambda n=number: client.issue_markers(n))
                issue = replace(issue, markers=markers)
            unretried = issue.markers is not None and SPEC_RETRY_MARKER not in issue.markers
            if spec_gate and unretried:
                labeler = attempt(lambda n=number: client.factory_labeler_can_write(n))
                issue = replace(issue, labeler_can_write=labeler)
            if stuck_gate:
                has_pr = attempt(lambda n=number: client.has_open_pr(n))
                issue = replace(issue, has_open_pr=has_pr)
            enriched.append(issue)
        detailed = tuple(enriched)

    unique_runs: tuple[Run, ...] | None = None
    if runs is not None:
        seen: set[int] = set()
        kept: list[Run] = []
        for run in runs:
            if run.id is not None:
                if run.id in seen:
                    continue
                seen.add(run.id)
            kept.append(run)
        unique_runs = tuple(kept)

    snapshot = Snapshot(
        issues=detailed,
        stale_claims=None if stale is None else tuple(stale),
        claimed_issues=claimed,
        run_status=run_status,
        runs=unique_runs,
    )
    return snapshot, failures


# --- Execute -----------------------------------------------------------------


def action_line(action: Action, executed: bool, outcome: str | None = None) -> dict[str, Any]:
    detail = action.detail if outcome is None else f"{action.detail}; {outcome}"
    return {
        "action": action.kind,
        "issue": action.issue,
        "detail": detail,
        "executed": executed,
    }


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload), flush=True)


def _perform(action: Action, client: Any) -> tuple[bool, str]:
    """Carry out one action. Returns (executed, outcome); raises CallFailed."""
    if action.kind == ACTION_RELEASE:
        assert action.sha is not None
        result = client.release_claim(action.issue, action.sha)
        if result == "released":
            return True, "released"
        if result == "absent":
            return True, "already released"
        return False, "kept: the claim changed after it was listed, so a newer run holds it"

    if action.kind == ACTION_RETRY_SPEC:
        client.post_comment(action.issue, spec_retry_comment(action.issue))
        try:
            client.dispatch_spec(action.issue)
        except CallFailed as exc:
            raise CallFailed(
                f"retry marker posted, but the dispatch failed and will not be "
                f"retried: {exc}"
            ) from exc
        return True, "spec stage dispatched"

    if action.kind == ACTION_FLAG_STUCK:
        if action.post_comment:
            client.post_comment(action.issue, stuck_build_comment(action.issue))
        try:
            client.add_labels(action.issue, [LABEL_NEEDS_HUMAN])
        except CallFailed as exc:
            if action.post_comment:
                raise CallFailed(
                    f"comment posted, but adding {LABEL_NEEDS_HUMAN} failed: {exc}"
                ) from exc
            raise
        return True, f"{LABEL_NEEDS_HUMAN} added"

    raise ValueError(f"unknown action {action.kind!r}")


def execute(
    actions: Sequence[Action],
    client: Any,
    *,
    dry_run: bool,
    emit: Callable[[dict[str, Any]], None] = _emit,
) -> bool:
    """Perform (or, with ``dry_run``, only report) each action in order.

    A failed action does not stop the others. Returns True if any failed.
    """
    failed = False
    for action in actions:
        if dry_run:
            emit(action_line(action, False, "dry run"))
            continue
        try:
            executed, outcome = _perform(action, client)
        except CallFailed as exc:
            failed = True
            print(f"ERROR: {action.kind} on issue {action.issue}: {exc}", file=sys.stderr)
            executed, outcome = False, f"failed: {exc}"
        emit(action_line(action, executed, outcome))
    return failed


# --- CLI ---------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="OWNER/REPO to sweep.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and print the planned actions; change nothing.",
    )
    parser.add_argument(
        "--claim-timeout-minutes",
        type=float,
        default=120.0,
        help="A claim older than this is stale; a building issue not updated "
        "for this long may be stuck (default: 120).",
    )
    parser.add_argument(
        "--spec-retry-minutes",
        type=float,
        default=60.0,
        help="Retry the spec of a factory issue not updated for this long "
        "(default: 60).",
    )
    parser.add_argument(
        "--now",
        type=int,
        default=None,
        help="Evaluate as of this Unix epoch (default: current time).",
    )
    parser.add_argument(
        "--workflow",
        default=DEFAULT_WORKFLOW,
        help=f"Factory workflow file name (default: {DEFAULT_WORKFLOW}).",
    )
    parser.add_argument(
        "--clone",
        default=".",
        help="Clone of the repository that claim.py works in (default: cwd).",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Remote that holds the claims (default: origin).",
    )
    parser.add_argument(
        "--claim-tool",
        default=None,
        help="Path to claim.py (default: claim.py next to this file).",
    )
    return parser


def _bad_input(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return EXIT_ERROR


def _check_minutes(name: str, value: float) -> str | None:
    if not math.isfinite(value) or value <= 0:
        return f"{name} must be a positive number of minutes (got {value})"
    return None


def main(argv: Sequence[str] | None = None, *, runner: Runner | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not valid_repo(args.repo):
        return _bad_input(f"--repo must be OWNER/REPO (got {args.repo!r})")
    for name, value in (
        ("--claim-timeout-minutes", args.claim_timeout_minutes),
        ("--spec-retry-minutes", args.spec_retry_minutes),
    ):
        problem = _check_minutes(name, value)
        if problem:
            return _bad_input(problem)
    now = args.now if args.now is not None else int(time.time())
    if not 0 <= now <= _MAX_EPOCH:
        return _bad_input(f"--now must be a Unix epoch between 0 and {_MAX_EPOCH} (got {now})")
    if not _WORKFLOW_RE.fullmatch(args.workflow):
        return _bad_input(f"--workflow must be a workflow file name (got {args.workflow!r})")
    if not args.remote or args.remote.startswith("-"):
        return _bad_input(f"--remote {args.remote!r} is not a remote name or URL")
    clone = Path(args.clone)
    if not clone.is_dir():
        return _bad_input(f"--clone {args.clone!r} is not a directory")
    claim_tool = (
        Path(args.claim_tool)
        if args.claim_tool
        else Path(__file__).resolve().with_name("claim.py")
    )
    if not claim_tool.is_file():
        return _bad_input(f"claim tool not found: {claim_tool}")

    settings = Settings(
        claim_timeout_minutes=args.claim_timeout_minutes,
        spec_retry_minutes=args.spec_retry_minutes,
    )
    client = Client(
        args.repo,
        claim_tool=claim_tool,
        clone=clone,
        remote=args.remote,
        workflow=args.workflow,
        runner=runner or run_command,
    )
    try:
        snapshot, failures = collect(client, now, settings)
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        actions = plan(snapshot, now, settings)
        exec_failed = execute(actions, client, dry_run=args.dry_run)
    except Exception:  # noqa: BLE001 - exit 1 is reserved for failed calls
        traceback.print_exc()
        print("ERROR: internal error in reconcile.py (see traceback)", file=sys.stderr)
        return EXIT_ERROR

    issues = "?" if snapshot.issues is None else len(snapshot.issues)
    stale = "?" if snapshot.stale_claims is None else len(snapshot.stale_claims)
    runs = "?" if snapshot.runs is None else len(snapshot.runs)
    verb = "planned (dry run)" if args.dry_run else "attempted"
    print(
        f"{issues} open factory/building issue(s), {stale} stale claim(s), "
        f"{runs} recent or running run(s); {len(actions)} action(s) {verb}",
        file=sys.stderr,
    )
    if failures or exec_failed:
        return EXIT_CALL_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
