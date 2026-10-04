#!/usr/bin/env python3
"""Cadence factory router: decide what a workflow run does, from the event alone.

The factory workflow fires on issue labels, issue comments, manual
dispatches and an hourly schedule. Most of those events must do nothing:
a label other than ``factory``, a comment that is not exactly
``/approve``, anything from a user without write access, and anything the
factory's own GitHub App did (or every run would wake the next one in a
loop). This script is the single place that decides. It is a pure
function of the event payload: no model call, no network, no secrets.

Contract:
    route.py --event-name NAME --event-path PATH --bot-login LOGIN
             --sender-permission {admin,maintain,write,triage,read,none}

    NAME is ``github.event_name``; PATH is ``$GITHUB_EVENT_PATH``, the
    event payload JSON. LOGIN is the factory App's bot login,
    ``<app-slug>[bot]`` (repo variable ``CADENCE_BOT_LOGIN``); it may be
    empty, because the ``type == "Bot"`` check below also catches the
    App. The workflow looks up the sender's repository role (for example
    ``gh api repos/O/R/collaborators/LOGIN/permission --jq .role_name``)
    and passes it in, so this script never calls GitHub. The permission
    defaults to ``none``, which never starts a stage.

    Prints one JSON line ``{"stage", "issue", "reason"}``. ``stage`` is
    ``spec``, ``build``, ``reconcile`` or ``none``. ``issue`` is the
    issue number for ``spec`` and ``build`` and null otherwise. If
    ``GITHUB_OUTPUT`` is set, also appends two lines to that file::

        stage=<stage>
        issue=<number, or empty when null>

    Both values come from fixed sets (a stage name, an integer), never
    from payload text, so a payload cannot inject extra outputs.

Rules, first match wins:
    0. Reconciler retry: a ``workflow_dispatch`` whose sender is exactly
       the factory App (LOGIN must be set) -> ``spec`` for a valid
       ``inputs.issue`` when ``inputs.stage`` is ``spec``; anything else
       the App dispatches -> none. The App never starts a build. Only
       ``tool/reconcile.py`` dispatches as the App, and it does so only
       after checking that the person who added ``factory`` has write
       access, so no permission check applies here (the App has no
       repository role to look up).
    1. Loop guard: if the sender (or, for a comment, the comment's
       author) is a bot -> none ("bot event"). A bot is a login equal to
       LOGIN (case-insensitive; ``LOGIN[bot]`` too, if LOGIN lacks the
       suffix), any login ending in ``[bot]``, or ``type == "Bot"``.
       ``schedule`` is exempt: it is time-driven, not caused by an
       event, and its actor is whoever last edited the cron line, which
       may be the App itself.
    2. ``issues`` with action ``labeled`` and label ``factory``
       (exact, case-sensitive) -> ``spec``, if the sender has admin,
       maintain or write. Other labels and actions -> none.
    3. ``issue_comment`` with action ``created`` on an issue (not a pull
       request: ``issue.pull_request`` absent or null) whose body, with
       surrounding whitespace stripped, is exactly ``/approve``
       (case-sensitive), while the issue has the ``spec-ready`` label and
       not ``building`` -> ``build``, if the sender has write or above.
    4. ``workflow_dispatch`` with ``inputs.issue`` a positive integer (a
       JSON number or a string of ASCII digits, surrounding whitespace
       allowed) and ``inputs.stage`` ``spec`` or ``build`` -> that stage,
       if the sender has write or above. GitHub already requires write
       access to dispatch; the check stays as defence in depth.
    5. ``schedule`` -> ``reconcile`` (issue null).
    6. Anything else -> none, with the reason.

Exit codes:
    0   a decision was printed (including ``none``)
    2   bad input: an unreadable or malformed payload (not JSON, not an
        object, or a field GitHub always sends has the wrong type), a bad
        argument, or ``GITHUB_OUTPUT`` could not be written

Usage:
    python tool/route.py --event-name "$GITHUB_EVENT_NAME" \\
        --event-path "$GITHUB_EVENT_PATH" \\
        --bot-login "$CADENCE_BOT_LOGIN" --sender-permission "$PERMISSION"

Design notes:
    - Waking agents is deterministic: an exact slash command or label
      from a user with write access, never free-text intent.
    - ``reason`` may quote short pieces of the payload. They are cut to
      60 characters with non-printable characters replaced by ``?``, and
      JSON-escaped on one line, so the log line cannot carry a workflow
      command.
    - Bad dispatch inputs are a decision (none, with the reason), not
      bad input: the payload is well formed, the request is not valid.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

STAGES: tuple[str, ...] = ("spec", "build", "reconcile", "none")
DISPATCH_STAGES: tuple[str, ...] = ("spec", "build")
PERMISSIONS: tuple[str, ...] = ("admin", "maintain", "write", "triage", "read", "none")
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})

FACTORY_LABEL = "factory"
SPEC_READY_LABEL = "spec-ready"
BUILDING_LABEL = "building"
APPROVE_COMMAND = "/approve"

EXIT_OK = 0
EXIT_BAD_INPUT = 2

# GitHub event names are lower-case words joined by underscores.
_EVENT_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# A GitHub login (alphanumerics and hyphens), optionally an App's "[bot]".
_LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}(\[bot\])?")
_DIGITS_RE = re.compile(r"[0-9]{1,10}")
# Issue numbers are capped at ten digits, as in claim.py.
_MAX_ISSUE = 9_999_999_999
_BOT_SUFFIX = "[bot]"
_SHOW_LIMIT = 60


class MalformedPayload(Exception):
    """The payload lacks the shape GitHub sends for this event. Exit 2."""


@dataclass(frozen=True)
class Decision:
    stage: str
    issue: int | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "issue": self.issue, "reason": self.reason}


# --- Pure helpers ---------------------------------------------------------


def _none(reason: str) -> Decision:
    return Decision("none", None, reason)


def show(value: Any) -> str:
    """Quote a payload value for a reason: one line, printable, short."""
    if isinstance(value, str):
        text = value
    elif value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    else:
        return type(value).__name__
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in text[: _SHOW_LIMIT + 1])
    if len(cleaned) > _SHOW_LIMIT:
        cleaned = cleaned[:_SHOW_LIMIT] + "..."
    return f"'{cleaned}'"


def has_write(permission: str) -> bool:
    return permission in WRITE_PERMISSIONS


def is_own_app(actor: Mapping[str, Any] | None, bot_login: str) -> bool:
    """True if ``actor`` is exactly the factory App (``bot_login`` must be set)."""
    if actor is None or not bot_login:
        return False
    login = actor.get("login")
    if not isinstance(login, str):
        return False
    own = bot_login.casefold()
    if not own.endswith(_BOT_SUFFIX):
        own += _BOT_SUFFIX
    return login.casefold() == own


def is_bot(actor: Mapping[str, Any] | None, bot_login: str) -> bool:
    """True if ``actor`` (a payload user object) is the factory App or any bot."""
    if actor is None:
        return False
    kind = actor.get("type")
    if isinstance(kind, str) and kind.casefold() == "bot":
        return True
    login = actor.get("login")
    if not isinstance(login, str):
        return False
    login = login.casefold()
    # Human logins cannot contain brackets, so "[bot]" always means an App.
    if login.endswith(_BOT_SUFFIX):
        return True
    if bot_login:
        own = bot_login.casefold()
        names = {own} if own.endswith(_BOT_SUFFIX) else {own, own + _BOT_SUFFIX}
        if login in names:
            return True
    return False


def parse_dispatch_issue(value: Any) -> int | None:
    """Return a dispatch ``inputs.issue`` as a positive issue number, or None.

    Dispatch inputs arrive as strings; a JSON number is accepted too.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value <= _MAX_ISSUE else None
    if isinstance(value, str):
        text = value.strip()
        if _DIGITS_RE.fullmatch(text):
            number = int(text)
            return number if number > 0 else None
    return None


def _object(container: Mapping[str, Any], key: str, where: str) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        raise MalformedPayload(f"{where}.{key} must be an object")
    return value


def _optional_object(
    container: Mapping[str, Any], key: str, where: str
) -> dict[str, Any] | None:
    value = container.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MalformedPayload(f"{where}.{key} must be an object or absent")
    return value


def _string(container: Mapping[str, Any], key: str, where: str) -> str:
    value = container.get(key)
    if not isinstance(value, str):
        raise MalformedPayload(f"{where}.{key} must be a string")
    return value


def _issue_number(issue: Mapping[str, Any]) -> int:
    number = issue.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        raise MalformedPayload("payload.issue.number must be an integer")
    if not 0 < number <= _MAX_ISSUE:
        raise MalformedPayload("payload.issue.number must be a positive issue number")
    return number


def label_names(issue: Mapping[str, Any]) -> set[str]:
    """Return the names of the labels on a payload issue object."""
    labels = issue.get("labels")
    if labels is None:
        return set()
    if not isinstance(labels, list):
        raise MalformedPayload("payload.issue.labels must be a list")
    names: set[str] = set()
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise MalformedPayload("each payload.issue.labels entry needs a string name")
        names.add(label["name"])
    return names


def _is_pull_request(issue: Mapping[str, Any]) -> bool:
    return issue.get("pull_request") is not None


def _permission_refusal(permission: str) -> Decision:
    return _none(
        f"insufficient permission: sender has '{permission}', "
        "needs write, maintain or admin"
    )


# --- Rules ----------------------------------------------------------------


def _route_issues(payload: Mapping[str, Any], permission: str) -> Decision:
    action = _string(payload, "action", "payload")
    if action != "labeled":
        return _none(f"issues event with action {show(action)}; only 'labeled' starts work")
    issue = _object(payload, "issue", "payload")
    number = _issue_number(issue)
    label = _object(payload, "label", "payload")
    name = _string(label, "name", "payload.label")
    if name != FACTORY_LABEL:
        return _none(f"label {show(name)} on issue #{number} is not '{FACTORY_LABEL}'")
    if _is_pull_request(issue):
        return _none(f"#{number} is a pull request, not an issue")
    if not has_write(permission):
        return _permission_refusal(permission)
    return Decision(
        "spec",
        number,
        f"issue #{number} labelled '{FACTORY_LABEL}' by a user with {permission} access",
    )


def _route_comment(payload: Mapping[str, Any], permission: str) -> Decision:
    action = _string(payload, "action", "payload")
    if action != "created":
        return _none(f"issue_comment with action {show(action)}; only 'created' counts")
    issue = _object(payload, "issue", "payload")
    number = _issue_number(issue)
    if _is_pull_request(issue):
        return _none(f"comment is on pull request #{number}, not an issue")
    comment = _object(payload, "comment", "payload")
    body = comment.get("body")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise MalformedPayload("payload.comment.body must be a string")
    if body.strip() != APPROVE_COMMAND:
        return _none(f"comment on issue #{number} is not exactly '{APPROVE_COMMAND}'")
    labels = label_names(issue)
    if BUILDING_LABEL in labels:
        return _none(f"issue #{number} is already '{BUILDING_LABEL}'")
    if SPEC_READY_LABEL not in labels:
        return _none(
            f"issue #{number} has no '{SPEC_READY_LABEL}' label; "
            f"'{APPROVE_COMMAND}' only counts after the spec is posted"
        )
    if not has_write(permission):
        return _permission_refusal(permission)
    return Decision(
        "build",
        number,
        f"'{APPROVE_COMMAND}' on spec-ready issue #{number} by a user with "
        f"{permission} access",
    )


def _route_dispatch(
    payload: Mapping[str, Any], permission: str, *, from_app: bool = False
) -> Decision:
    inputs = _optional_object(payload, "inputs", "payload")
    if inputs is None:
        return _none("workflow_dispatch without inputs; needs issue and stage")
    raw_issue = inputs.get("issue")
    number = parse_dispatch_issue(raw_issue)
    if number is None:
        return _none(
            f"workflow_dispatch inputs.issue {show(raw_issue)} is not a positive integer"
        )
    raw_stage = inputs.get("stage")
    stage = raw_stage.strip() if isinstance(raw_stage, str) else None
    if stage not in DISPATCH_STAGES:
        return _none(
            f"workflow_dispatch inputs.stage {show(raw_stage)} is not "
            + " or ".join(f"'{s}'" for s in DISPATCH_STAGES)
        )
    if from_app:
        # Rule 0: the reconciler's spec retry. Builds spend the budget and
        # need a human's /approve, so the App can never start one.
        if stage != "spec":
            return _none(
                f"the factory App dispatched {stage} for issue #{number}; "
                "the App may only retry a spec"
            )
        return Decision(
            "spec",
            number,
            f"spec retry for issue #{number} dispatched by the factory App (reconciler)",
        )
    if not has_write(permission):
        return _permission_refusal(permission)
    return Decision(
        stage,
        number,
        f"workflow_dispatch of {stage} for issue #{number} by a user with "
        f"{permission} access",
    )


def decide(
    event_name: str, payload: Any, bot_login: str, permission: str
) -> Decision:
    """Return the routing decision for one event.

    Raises MalformedPayload when the payload lacks the shape GitHub sends.
    """
    if not isinstance(payload, dict):
        raise MalformedPayload("the event payload must be a JSON object")

    if event_name == "schedule":
        return Decision("reconcile", None, "hourly schedule: run the reconciler")

    sender = _optional_object(payload, "sender", "payload")
    if sender is None and event_name in ("issues", "issue_comment", "workflow_dispatch"):
        # GitHub always names the sender of these; without one the loop
        # guard cannot run, so refuse rather than guess.
        raise MalformedPayload("payload.sender is missing")
    if event_name == "workflow_dispatch" and is_own_app(sender, bot_login):
        return _route_dispatch(payload, permission, from_app=True)
    author: dict[str, Any] | None = None
    if event_name == "issue_comment":
        comment = _optional_object(payload, "comment", "payload")
        if comment is not None:
            author = _optional_object(comment, "user", "payload.comment")
    if is_bot(sender, bot_login) or is_bot(author, bot_login):
        return _none("bot event: ignoring events caused by a bot (loop guard)")

    if event_name == "issues":
        return _route_issues(payload, permission)
    if event_name == "issue_comment":
        return _route_comment(payload, permission)
    if event_name == "workflow_dispatch":
        return _route_dispatch(payload, permission)
    return _none(f"event '{event_name}' does not start factory work")


def output_lines(decision: Decision) -> str:
    """Return the ``GITHUB_OUTPUT`` lines for a decision.

    Values are re-checked here so that nothing but a known stage name and
    an integer can ever reach the file.
    """
    if decision.stage not in STAGES:
        raise ValueError(f"unknown stage {decision.stage!r}")
    issue = decision.issue
    if issue is None:
        issue_text = ""
    elif type(issue) is int and 0 < issue <= _MAX_ISSUE:
        issue_text = str(issue)
    else:
        raise ValueError(f"issue must be a positive integer or None, not {issue!r}")
    return f"stage={decision.stage}\nissue={issue_text}\n"


def write_github_output(path: Path, decision: Decision) -> None:
    text = output_lines(decision)
    # Start on a fresh line even if an earlier command left the file
    # without a trailing newline.
    try:
        with open(path, "rb") as existing:
            existing.seek(0, os.SEEK_END)
            if existing.tell() > 0:
                existing.seek(-1, os.SEEK_END)
                if existing.read(1) != b"\n":
                    text = "\n" + text
    except FileNotFoundError:
        pass
    with open(path, "a", encoding="utf-8", newline="") as handle:
        handle.write(text)


# --- CLI ------------------------------------------------------------------


def _permission(text: str) -> str:
    return text.strip().lower()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--event-name", required=True, help="github.event_name, e.g. issues."
    )
    parser.add_argument(
        "--event-path", required=True, help="Path to the event payload JSON."
    )
    parser.add_argument(
        "--bot-login",
        required=True,
        help="The factory App's bot login, <app-slug>[bot]; may be empty.",
    )
    parser.add_argument(
        "--sender-permission",
        type=_permission,
        choices=PERMISSIONS,
        default="none",
        help="The sender's repository role (default: none).",
    )
    return parser


def _bad_input(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return EXIT_BAD_INPUT


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        event_name: str = args.event_name
        if not _EVENT_NAME_RE.fullmatch(event_name):
            return _bad_input(f"--event-name {args.event_name!r} is not a GitHub event name")
        bot_login: str = args.bot_login.strip()
        if bot_login and not _LOGIN_RE.fullmatch(bot_login):
            return _bad_input(
                f"--bot-login {args.bot_login!r} is not a GitHub login "
                "such as my-app[bot]"
            )

        try:
            # utf-8-sig: tolerate a byte-order mark from hand-written files.
            text = Path(args.event_path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            return _bad_input(f"cannot read --event-path {args.event_path!r}: {exc}")
        try:
            payload = json.loads(text)
        except (ValueError, RecursionError) as exc:
            return _bad_input(f"--event-path {args.event_path!r} is not valid JSON: {exc}")

        try:
            decision = decide(event_name, payload, bot_login, args.sender_permission)
        except MalformedPayload as exc:
            return _bad_input(f"malformed {event_name} payload: {exc}")

        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            try:
                write_github_output(Path(github_output), decision)
            except OSError as exc:
                return _bad_input(f"cannot write GITHUB_OUTPUT: {exc}")

        print(json.dumps(decision.as_dict()), flush=True)
        return EXIT_OK
    except Exception:  # noqa: BLE001 - the contract has no exit 1
        traceback.print_exc()
        print("ERROR: internal error in route.py (see traceback)", file=sys.stderr)
        return EXIT_BAD_INPUT


if __name__ == "__main__":
    sys.exit(main())
