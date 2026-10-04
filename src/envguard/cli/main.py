"""argparse entry point for the ``envguard`` command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from envguard import __version__
from envguard.cli.output import format_check, format_diff
from envguard.core.checks import check_env
from envguard.core.diff import diff_env
from envguard.core.models import Severity
from envguard.io.reader import EnvFileError, load_env

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="envguard",
        description="Check .env files without ever printing their values.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    check = sub.add_parser("check", help="compare an env file against an example file")
    check.add_argument("--env", default=".env", help="env file to check (default: .env)")
    check.add_argument(
        "--example",
        default=".env.example",
        help="example file listing expected keys (default: .env.example)",
    )
    check.set_defaults(func=cmd_check)

    diff = sub.add_parser("diff", help="show keys added, removed, or changed between two files")
    diff.add_argument("old", help="first (old) env file")
    diff.add_argument("new", help="second (new) env file")
    diff.set_defaults(func=cmd_diff)

    return parser


def cmd_check(args: argparse.Namespace) -> int:
    env = load_env(args.env)
    example = load_env(args.example)
    issues = check_env(env, example)
    for line in format_check(issues, args.env, args.example):
        print(line)
    has_errors = any(i.severity is Severity.ERROR for i in issues)
    return EXIT_PROBLEMS if has_errors else EXIT_OK


def cmd_diff(args: argparse.Namespace) -> int:
    result = diff_env(load_env(args.old), load_env(args.new))
    for line in format_diff(result):
        print(line)
    return EXIT_OK if result.is_empty else EXIT_PROBLEMS


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except EnvFileError as exc:
        print(f"envguard: error: {exc}", file=sys.stderr)
        return EXIT_USAGE
