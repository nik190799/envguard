#!/usr/bin/env python3
"""Cadence factory issue claim: one run per issue, enforced by an atomic ref push.

A factory run must hold the claim on an issue before it works on it, so
two runs (a webhook and the hourly reconciler, or a re-run racing the
original) never build the same issue at once. GitHub labels and assignees
cannot provide this: they have no compare-and-swap, so two runs can both
read "unclaimed" and both write "claimed". A ref push can. Given an
explicit expected old value, the server applies the update only if the
ref still has that value and rejects it otherwise, so exactly one of any
number of concurrent acquirers wins.

Contract:
    The claim on issue N is the branch ``refs/heads/cadence/claim/N``. It
    points at a parentless commit with an empty tree, authored and
    committed by ``cadence-factory <cadence-factory@users.noreply.github.com>``
    at the claim time, whose message is a single JSON line::

        {"issue": N, "run_id": "<run id>", "claimed_at": "<ISO-8601 UTC>"}

    acquire --issue N --run-id ID [--now EPOCH]
        Push the claim commit with
        ``--force-with-lease=refs/heads/cadence/claim/N:``. The empty
        expected value means "the ref must not exist". Prints
        ``{"issue", "run_id", "sha", "ref"}``. If another run holds the
        claim, prints the holder ``{"issue", "ref", "held_by", "sha",
        "claimed_at"}`` and exits 1. If this same run id already holds it
        (a retried step whose first push landed), acquire succeeds and
        reports the existing claim.

    release --issue N --run-id ID [--force] [--sha SHA]
        Read the claim and refuse (exit 1) if another run holds it, unless
        ``--force``, or if ``--sha`` is given and the claim is no longer
        that commit. Then delete it with
        ``--force-with-lease=refs/heads/cadence/claim/N:<observed sha>``,
        so a claim replaced after it was read is never deleted (exit 1).
        Releasing a claim that does not exist succeeds: release is
        idempotent. Prints ``{"issue", "ref", "released", "sha"}``.

    stale --timeout-minutes M [--now EPOCH]
        Print one JSON line ``{"issue", "run_id", "sha", "age_minutes"}``
        per claim whose commit is more than M minutes old (``run_id`` is
        null when the claim message is unreadable). The reconciler removes
        each with ``release --force --sha <sha>``. Without ``--sha``, a
        claim released and re-taken by a live run after the listing would
        be deleted too.

    Every subcommand takes ``--repo PATH`` (a clone of the repository,
    default ``.``) and ``--remote NAME`` (a remote name or URL, default
    ``origin``). ``--issue`` must be a positive integer, ``--run-id``
    must match ``[A-Za-z0-9._-]{1,128}`` and ``--now`` must lie between 0
    and 253402300799 (9999-12-31T23:59:59Z).

Workflows must never trigger on pushes to ``cadence/**`` branches. Claims
are ordinary branches, and every acquire and release is a push, so a
workflow with an unfiltered ``push`` trigger would start a run for each
claim. Give every push trigger ``branches-ignore: ['cadence/**']``.

Exit codes:
    0   ok: claim acquired, released (or already absent), or stale list
        printed (possibly empty)
    1   the claim is held by another run, or it changed after it was read
        and the lease refused the delete
    2   bad input, or a git failure that is not about the claim (git's
        stderr is printed)

Usage:
    python tool/claim.py acquire --issue 42 --run-id "$GITHUB_RUN_ID"
    python tool/claim.py release --issue 42 --run-id "$GITHUB_RUN_ID"
    python tool/claim.py stale --timeout-minutes 120
    python tool/claim.py release --issue 42 --run-id reconciler --force \\
        --sha <sha printed by stale>

Design notes:
    - The claim commit has no parent and an empty tree, so it never shares
      history with code, cannot be merged by accident, and gives a
      mis-triggered CI job nothing to build.
    - The committer date is the claim time, so ``stale`` needs no state
      besides the refs themselves. ``--now`` makes it deterministic in
      tests and lets the reconciler evaluate "as of" a given time.
    - The identity is fixed, so the claim does not depend on the runner's
      git config. Branch protection or rulesets must let the factory's
      GitHub App create and delete ``cadence/claim/*`` branches.
    - Claim commits are read with ``git cat-file``, never parsed from
      human-facing output, and a garbled claim message, or one naming a
      different issue than its ref, is treated as "held by an unknown
      run": only ``release --force`` removes it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

CLAIM_REF_PREFIX = "refs/heads/cadence/claim/"
CLAIM_NAME = "cadence-factory"
CLAIM_EMAIL = "cadence-factory@users.noreply.github.com"

EXIT_OK = 0
EXIT_HELD = 1
EXIT_ERROR = 2

_ISSUE_RE = re.compile(r"[0-9]{1,10}")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_CLAIM_REF_RE = re.compile(re.escape(CLAIM_REF_PREFIX) + r"([1-9][0-9]{0,9})")
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")  # SHA-1 or SHA-256

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# The last second datetime can represent (9999-12-31T23:59:59Z).
_MAX_EPOCH = 253_402_300_799

# A claim can change between ``ls-remote`` and ``fetch``. Re-read it at most
# this many times before giving up.
_OBSERVE_ATTEMPTS = 3

# An acquire push rejected by the lease, for a claim that is gone again by
# the time we look, is retried at most this many times in total.
_ACQUIRE_ATTEMPTS = 3


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


class GitError(Exception):
    """A git command failed for a reason other than the claim itself."""

    def __init__(self, what: str, result: GitResult) -> None:
        super().__init__(what)
        self.what = what
        self.result = result


@dataclass(frozen=True)
class Claim:
    """A claim as read from the remote."""

    issue: int
    sha: str
    run_id: str | None  # None when the message is not a valid claim
    claimed_at: str | None
    committed_at: int | None  # None when the commit could not be read


# --- Pure helpers ---------------------------------------------------------


def claim_ref(issue: int) -> str:
    return f"{CLAIM_REF_PREFIX}{issue}"


def issue_from_ref(ref: str) -> int | None:
    """Return the issue number of a claim ref, or None for any other ref."""
    match = _CLAIM_REF_RE.fullmatch(ref)
    return int(match.group(1)) if match else None


def parse_issue(text: str) -> int | None:
    """Return ``text`` as a positive issue number, or None if it is not one."""
    text = text.strip()
    if not _ISSUE_RE.fullmatch(text):
        return None
    issue = int(text)
    return issue if issue > 0 else None


def valid_run_id(text: str) -> bool:
    return _RUN_ID_RE.fullmatch(text) is not None


def iso_utc(epoch: int) -> str:
    # Epoch arithmetic, not datetime.fromtimestamp: on Windows that raises
    # OSError for epochs after the year 3000.
    return (_EPOCH + timedelta(seconds=epoch)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_claim_message(issue: int, run_id: str, now: int) -> str:
    """Return the one-line JSON message of a claim commit."""
    return json.dumps(
        {"issue": issue, "run_id": run_id, "claimed_at": iso_utc(now)}
    )


def parse_claim_message(text: Any) -> dict[str, Any] | None:
    """Parse a claim commit message; return None for anything that is not one.

    The message comes from the remote, where anyone with push access can
    write anything, so garbage is expected and never raises.
    """
    if not isinstance(text, str):
        return None
    try:
        data = json.loads(text.strip())
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    issue = data.get("issue")
    run_id = data.get("run_id")
    claimed_at = data.get("claimed_at")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
        return None
    if not isinstance(run_id, str) or not valid_run_id(run_id):
        return None
    if not isinstance(claimed_at, str):
        claimed_at = None
    return {"issue": issue, "run_id": run_id, "claimed_at": claimed_at}


def claim_env(now: int) -> dict[str, str]:
    """Return the git environment that pins the claim commit's identity and date."""
    date = f"@{now} +0000"
    return {
        "GIT_AUTHOR_NAME": CLAIM_NAME,
        "GIT_AUTHOR_EMAIL": CLAIM_EMAIL,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": CLAIM_NAME,
        "GIT_COMMITTER_EMAIL": CLAIM_EMAIL,
        "GIT_COMMITTER_DATE": date,
    }


def build_acquire_push_command(remote: str, issue: int, sha: str) -> list[str]:
    """Return the git arguments that create the claim ref only if it is absent."""
    ref = claim_ref(issue)
    return [
        "push",
        "--porcelain",
        f"--force-with-lease={ref}:",
        remote,
        f"{sha}:{ref}",
    ]


def build_delete_push_command(
    remote: str, issue: int, observed_sha: str
) -> list[str]:
    """Return the git arguments that delete the claim ref only if it still
    points at ``observed_sha``."""
    ref = claim_ref(issue)
    return [
        "push",
        "--porcelain",
        f"--force-with-lease={ref}:{observed_sha}",
        remote,
        f":{ref}",
    ]


def parse_ls_remote(output: str) -> dict[str, str]:
    """Return ``{ref: sha}`` from ``git ls-remote`` output."""
    refs: dict[str, str] = {}
    for line in output.splitlines():
        sha, sep, ref = line.strip().partition("\t")
        if sep and sha and ref:
            refs[ref] = sha
    return refs


def parse_push_status(output: str, ref: str) -> tuple[str, str] | None:
    """Return ``(flag, summary)`` for ``ref`` from ``git push --porcelain``.

    Porcelain lines are ``<flag>\\t<from>:<to>\\t<summary>``, where the flag
    is ``*`` (new ref), ``-`` (deleted), ``+`` (forced), `` `` (fast
    forward), ``=`` (up to date) or ``!`` (rejected).
    """
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or len(parts[0]) != 1:
            continue
        _, _, dst = parts[1].rpartition(":")
        if dst == ref:
            return parts[0], parts[2]
    return None


def push_succeeded(result: GitResult, ref: str) -> bool:
    status = parse_push_status(result.stdout, ref)
    return result.returncode == 0 and status is not None and status[0] != "!"


def push_lease_rejected(result: GitResult, ref: str) -> bool:
    """True if the push was refused because the ref did not hold the
    expected value."""
    status = parse_push_status(result.stdout, ref)
    return status is not None and status[0] == "!" and "stale info" in status[1]


def parse_commit_object(raw: str) -> tuple[int, str] | None:
    """Return ``(committer_timestamp, message)`` from ``git cat-file commit``."""
    header, _, message = raw.partition("\n\n")
    for line in header.splitlines():
        if line.startswith("committer "):
            fields = line.rsplit(" ", 2)
            if len(fields) != 3:
                return None
            try:
                return int(fields[1]), message
            except ValueError:
                return None
    return None


def is_stale(committed_at: int, now: int, timeout_minutes: float) -> bool:
    return now - committed_at > timeout_minutes * 60


def age_minutes(committed_at: int, now: int) -> int:
    return max(0, (now - committed_at) // 60)


# --- Git ------------------------------------------------------------------


def _git(
    repo: Path,
    args: Sequence[str],
    *,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> GitResult:
    full_env = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_text,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=full_env,
            check=False,
        )
    except OSError as exc:
        return GitResult(127, "", f"could not run git: {exc}")
    return GitResult(proc.returncode, proc.stdout, proc.stderr)


def _git_ok(
    repo: Path,
    args: Sequence[str],
    *,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    result = _git(repo, args, input_text=input_text, env=env)
    if result.returncode != 0:
        raise GitError("git " + " ".join(args[:1]), result)
    return result.stdout


def _remote_claims(repo: Path, remote: str, pattern: str) -> dict[int, str]:
    """Return ``{issue: sha}`` for the remote claim refs matching ``pattern``.

    ``ls-remote`` patterns match the tail of a ref name, so results are
    re-checked against the exact claim ref shape.
    """
    result = _git(repo, ["ls-remote", remote, pattern])
    if result.returncode != 0:
        raise GitError(f"git ls-remote {remote} {pattern}", result)
    claims: dict[int, str] = {}
    for ref, sha in parse_ls_remote(result.stdout).items():
        issue = issue_from_ref(ref)
        if issue is not None:
            claims[issue] = sha
    return claims


def _remote_sha(repo: Path, remote: str, issue: int) -> str | None:
    return _remote_claims(repo, remote, claim_ref(issue)).get(issue)


def _fetch(repo: Path, remote: str, refs: Sequence[str]) -> GitResult:
    # A refspec without a destination only updates FETCH_HEAD, so claims
    # never show up as local branches.
    return _git(repo, ["fetch", "--no-tags", "--quiet", remote, *refs])


def _read_commit(repo: Path, sha: str) -> tuple[int, str] | None:
    result = _git(repo, ["cat-file", "commit", sha])
    if result.returncode != 0:
        return None
    return parse_commit_object(result.stdout)


def _claim_from_commit(issue: int, sha: str, info: tuple[int, str]) -> Claim:
    committed_at, message = info
    parsed = parse_claim_message(message)
    if parsed is not None and parsed["issue"] != issue:
        # A claim commit copied from another issue's ref is not this
        # issue's claim: treat it like any other unreadable message, so
        # the run it names cannot re-acquire or release it as its own.
        parsed = None
    return Claim(
        issue=issue,
        sha=sha,
        run_id=parsed["run_id"] if parsed else None,
        claimed_at=parsed["claimed_at"] if parsed else None,
        committed_at=committed_at,
    )


def _observe(repo: Path, remote: str, issue: int) -> Claim | None:
    """Read the current claim on ``issue`` from the remote; None if unclaimed.

    Raises GitError if the claim exists but cannot be fetched.
    """
    ref = claim_ref(issue)
    sha: str | None = None
    fetch_failure: GitResult | None = None
    for _ in range(_OBSERVE_ATTEMPTS):
        sha = _remote_sha(repo, remote, issue)
        if sha is None:
            return None
        info = _read_commit(repo, sha)
        if info is None:
            fetched = _fetch(repo, remote, [ref])
            fetch_failure = fetched if fetched.returncode != 0 else None
            info = _read_commit(repo, sha)
        if info is not None:
            return _claim_from_commit(issue, sha, info)
    if fetch_failure is not None:
        raise GitError(f"git fetch {remote} {ref}", fetch_failure)
    # The claim kept changing faster than it could be read.
    assert sha is not None
    return Claim(issue=issue, sha=sha, run_id=None, claimed_at=None, committed_at=None)


# --- Output ---------------------------------------------------------------


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload), flush=True)


def _bad_input(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return EXIT_ERROR


def _git_failure(exc: GitError, *, after: GitError | None = None) -> int:
    """Report ``exc`` and return exit 2. ``after`` is an earlier failure that
    led to it; it is reported first unless git said the same thing twice."""
    failures = [exc] if after is None else [after, exc]
    shown: set[str] = set()
    for failure in failures:
        detail = failure.result.stderr.strip() or failure.result.stdout.strip()
        if detail in shown:
            continue
        shown.add(detail)
        print(
            f"ERROR: {failure.what} failed (exit {failure.result.returncode})",
            file=sys.stderr,
        )
        if detail:
            print(textwrap.indent(detail, "  "), file=sys.stderr)
    return EXIT_ERROR


def _report_held(claim: Claim, consequence: str) -> None:
    holder = claim.run_id or "an unknown run (unreadable claim message)"
    since = f" since {claim.claimed_at}" if claim.claimed_at else ""
    print(
        f"issue {claim.issue} is claimed by {holder}{since} "
        f"(claim commit {claim.sha[:12]}); {consequence}",
        file=sys.stderr,
    )
    _emit(
        {
            "issue": claim.issue,
            "ref": claim_ref(claim.issue),
            "held_by": claim.run_id,
            "sha": claim.sha,
            "claimed_at": claim.claimed_at,
        }
    )


# --- Subcommands ----------------------------------------------------------


def acquire(repo: Path, remote: str, issue: int, run_id: str, now: int) -> int:
    ref = claim_ref(issue)
    try:
        tree = _git_ok(repo, ["mktree"], input_text="").strip()
        sha = _git_ok(
            repo,
            ["commit-tree", tree, "-F", "-"],
            input_text=build_claim_message(issue, run_id, now) + "\n",
            env=claim_env(now),
        ).strip()
    except GitError as exc:
        return _git_failure(exc)

    for _ in range(_ACQUIRE_ATTEMPTS):
        push = _git(repo, build_acquire_push_command(remote, issue, sha))
        if push_succeeded(push, ref):
            _emit({"issue": issue, "run_id": run_id, "sha": sha, "ref": ref})
            return EXIT_OK

        # The push did not land. Ask the remote whether that is because
        # someone holds the claim or because of something else.
        try:
            holder = _observe(repo, remote, issue)
        except GitError as exc:
            return _git_failure(exc, after=GitError(f"git push {remote} {ref}", push))
        if holder is None:
            if push_lease_rejected(push, ref):
                continue  # held a moment ago, released since: try again
            return _git_failure(GitError(f"git push {remote} {ref}", push))
        if holder.sha == sha or holder.run_id == run_id:
            if holder.sha != sha:
                print(
                    f"issue {issue} is already claimed by this run "
                    f"({run_id}); keeping that claim",
                    file=sys.stderr,
                )
            _emit({"issue": issue, "run_id": run_id, "sha": holder.sha, "ref": ref})
            return EXIT_OK
        _report_held(holder, "not acquired")
        return EXIT_HELD

    print(
        f"ERROR: the claim on issue {issue} kept changing; gave up after "
        f"{_ACQUIRE_ATTEMPTS} attempts",
        file=sys.stderr,
    )
    return EXIT_ERROR


def release(
    repo: Path,
    remote: str,
    issue: int,
    run_id: str,
    force: bool,
    expected_sha: str | None = None,
) -> int:
    ref = claim_ref(issue)
    try:
        held = _observe(repo, remote, issue)
    except GitError as exc:
        return _git_failure(exc)
    if held is None:
        print(f"issue {issue} has no claim; nothing to release", file=sys.stderr)
        _emit({"issue": issue, "ref": ref, "released": False, "sha": None})
        return EXIT_OK
    if expected_sha is not None and held.sha != expected_sha:
        # The claim the caller judged (e.g. stale) is gone; this is a
        # newer one, possibly a live run's.
        _report_held(held, f"not released (it is no longer {expected_sha[:12]})")
        return EXIT_HELD
    if held.run_id != run_id and not force:
        _report_held(held, "not released (pass --force to remove it anyway)")
        return EXIT_HELD

    push = _git(repo, build_delete_push_command(remote, issue, held.sha))
    if push_succeeded(push, ref):
        _emit({"issue": issue, "ref": ref, "released": True, "sha": held.sha})
        return EXIT_OK

    # The delete did not land. The lease refuses it when the claim moved
    # after we read it; find out which way it moved.
    try:
        current = _remote_sha(repo, remote, issue)
    except GitError as exc:
        return _git_failure(exc, after=GitError(f"git push {remote} :{ref}", push))
    if current is None:
        print(
            f"issue {issue}'s claim was released by someone else meanwhile",
            file=sys.stderr,
        )
        _emit({"issue": issue, "ref": ref, "released": False, "sha": None})
        return EXIT_OK
    if current != held.sha:
        print(
            f"issue {issue}'s claim changed after it was read "
            f"({held.sha[:12]} -> {current[:12]}); the lease kept it, "
            "not released",
            file=sys.stderr,
        )
        return EXIT_HELD
    return _git_failure(GitError(f"git push {remote} :{ref}", push))


def stale(repo: Path, remote: str, timeout_minutes: float, now: int) -> int:
    try:
        claims = _remote_claims(repo, remote, CLAIM_REF_PREFIX + "*")
    except GitError as exc:
        return _git_failure(exc)

    infos = {issue: _read_commit(repo, sha) for issue, sha in claims.items()}
    missing = sorted(issue for issue, info in infos.items() if info is None)
    fetch_failure: GitResult | None = None
    if missing:
        fetched = _fetch(repo, remote, [claim_ref(i) for i in missing])
        if fetched.returncode != 0:
            # One fetch fails as a whole if any ref was released since the
            # listing, so fall back to one ref at a time.
            for issue in missing:
                single = _fetch(repo, remote, [claim_ref(issue)])
                if single.returncode != 0:
                    fetch_failure = single
        for issue in missing:
            infos[issue] = _read_commit(repo, claims[issue])

    unread = [issue for issue in missing if infos[issue] is None]
    if unread and fetch_failure is not None:
        # Claims that vanished or moved since the listing are fine to skip;
        # one that is still there unchanged means the fetch itself is broken.
        try:
            current = _remote_claims(repo, remote, CLAIM_REF_PREFIX + "*")
        except GitError as exc:
            return _git_failure(exc)
        if any(current.get(issue) == claims[issue] for issue in unread):
            return _git_failure(GitError(f"git fetch {remote}", fetch_failure))

    listed = 0
    for issue in sorted(claims):
        info = infos[issue]
        if info is None:
            print(
                f"note: issue {issue}'s claim changed while listing; skipped",
                file=sys.stderr,
            )
            continue
        committed_at = info[0]
        if not is_stale(committed_at, now, timeout_minutes):
            continue
        listed += 1
        claim = _claim_from_commit(issue, claims[issue], info)
        _emit(
            {
                "issue": issue,
                "run_id": claim.run_id,
                "sha": claim.sha,
                "age_minutes": age_minutes(committed_at, now),
            }
        )
    print(
        f"{len(claims)} claim(s), {listed} older than {timeout_minutes:g} minutes",
        file=sys.stderr,
    )
    return EXIT_OK


# --- CLI ------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--repo",
        default=".",
        help="Path to a clone of the repository (default: cwd).",
    )
    common.add_argument(
        "--remote",
        default="origin",
        help="Remote name or URL that holds the claims (default: origin).",
    )

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    acq = sub.add_parser(
        "acquire", parents=[common], help="claim an issue for this run"
    )
    acq.add_argument("--issue", required=True, help="Issue number (positive integer).")
    acq.add_argument("--run-id", required=True, help="This run's id, [A-Za-z0-9._-]{1,128}.")
    acq.add_argument(
        "--now",
        type=int,
        default=None,
        help="Claim time as a Unix epoch (default: current time).",
    )

    rel = sub.add_parser(
        "release", parents=[common], help="release this run's claim on an issue"
    )
    rel.add_argument("--issue", required=True, help="Issue number (positive integer).")
    rel.add_argument("--run-id", required=True, help="This run's id, [A-Za-z0-9._-]{1,128}.")
    rel.add_argument(
        "--force",
        action="store_true",
        help="Release even if another run holds the claim (for the reconciler).",
    )
    rel.add_argument(
        "--sha",
        default=None,
        help="Release only if the claim is still this commit (the sha that "
        "stale printed); a newer claim is kept (exit 1).",
    )

    st = sub.add_parser(
        "stale", parents=[common], help="list claims older than a timeout"
    )
    st.add_argument(
        "--timeout-minutes",
        type=float,
        required=True,
        help="List claims older than this many minutes.",
    )
    st.add_argument(
        "--now",
        type=int,
        default=None,
        help="Evaluate ages as of this Unix epoch (default: current time).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    repo = Path(args.repo)
    if not repo.is_dir():
        return _bad_input(f"--repo {args.repo!r} is not a directory")
    remote: str = args.remote
    if not remote or remote.startswith("-"):
        return _bad_input(f"--remote {remote!r} is not a remote name or URL")
    given_now: int | None = getattr(args, "now", None)  # release has no --now
    now = given_now if given_now is not None else int(time.time())
    if not 0 <= now <= _MAX_EPOCH:
        return _bad_input(
            f"--now must be a Unix epoch between 0 and {_MAX_EPOCH} (got {now})"
        )

    if args.command == "stale":
        timeout: float = args.timeout_minutes
        if not math.isfinite(timeout) or timeout < 0:
            return _bad_input(
                f"--timeout-minutes must be a non-negative number (got {timeout})"
            )
        return stale(repo, remote, timeout, now)

    issue = parse_issue(args.issue)
    if issue is None:
        return _bad_input(f"--issue must be a positive integer (got {args.issue!r})")
    if not valid_run_id(args.run_id):
        return _bad_input(
            f"--run-id must match [A-Za-z0-9._-]{{1,128}} (got {args.run_id!r})"
        )

    if args.command == "acquire":
        return acquire(repo, remote, issue, args.run_id, now)
    expected_sha: str | None = args.sha
    if expected_sha is not None:
        expected_sha = expected_sha.strip().lower()
        if not _SHA_RE.fullmatch(expected_sha):
            return _bad_input(
                f"--sha must be a full commit id, 40 or 64 hex digits (got {args.sha!r})"
            )
    return release(repo, remote, issue, args.run_id, args.force, expected_sha)


if __name__ == "__main__":
    sys.exit(main())
