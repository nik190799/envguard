from envguard.cli.output import example_lines, format_check, format_diff_warnings
from envguard.core.checks import check_env
from envguard.core.models import ParseError
from envguard.core.parser import parse


def test_example_lines_uses_first_definition():
    assert example_lines(parse("A=\n# note\nB=\nB=\n")) == {"A": 1, "B": 3}


def test_missing_issue_points_at_example_line():
    example = parse("A=\n# note\nB=\n")
    issues = check_env(parse("A=1\n"), example)
    lines = format_check(issues, ".env", ".env.example", example_lines(example))
    assert lines[0] == ".env.example:3: error: B: missing from env file [missing]"


def test_other_issue_locations_are_unchanged():
    example = parse("A=\n")
    issues = check_env(parse("A=1\nZ=9\n"), example)
    lines = format_check(issues, ".env", ".env.example", example_lines(example))
    assert lines[0] == ".env:2: warning: Z: not listed in example file [extra]"


def test_diff_warnings_name_path_and_line_only():
    warnings = format_diff_warnings([ParseError(2, "detail"), ParseError(5, "x")], "old.env")
    assert warnings == [
        "envguard: warning: old.env:2: malformed line, ignored",
        "envguard: warning: old.env:5: malformed line, ignored",
    ]
