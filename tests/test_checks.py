from envguard.core.checks import check_env, is_required
from envguard.core.models import IssueKind, Severity
from envguard.core.parser import parse


def kinds(issues):
    return [(i.kind, i.key) for i in issues]


def test_clean_env_has_no_issues():
    assert check_env(parse("A=1\nB=2\n"), parse("A=\nB=\n")) == []


def test_missing_key_is_error():
    issues = check_env(parse("A=1\n"), parse("A=\nB=\n"))
    assert kinds(issues) == [(IssueKind.MISSING, "B")]
    assert issues[0].severity is Severity.ERROR
    assert issues[0].line_no is None


def test_extra_key_is_warning():
    issues = check_env(parse("A=1\nZ=9\n"), parse("A=\n"))
    assert kinds(issues) == [(IssueKind.EXTRA, "Z")]
    assert issues[0].severity is Severity.WARNING
    assert issues[0].line_no == 2


def test_duplicate_key_in_env_is_error():
    issues = check_env(parse("A=1\nA=2\n"), parse("A=\n"))
    assert kinds(issues) == [(IssueKind.DUPLICATE, "A")]
    assert issues[0].line_no == 2
    assert "line 1" in issues[0].message


def test_duplicate_key_in_example_is_reported_against_example():
    issues = check_env(parse("A=1\n"), parse("A=\nA=\n"))
    assert kinds(issues) == [(IssueKind.DUPLICATE, "A")]
    assert issues[0].source == "example"


def test_malformed_lines_are_reported_for_both_files():
    issues = check_env(parse("A=1\noops\n"), parse("A=\nnope\n"))
    assert [(i.kind, i.source, i.line_no) for i in issues] == [
        (IssueKind.MALFORMED, "example", 2),
        (IssueKind.MALFORMED, "env", 2),
    ]


def test_empty_required_value_is_error():
    issues = check_env(parse("TOKEN=\n"), parse("TOKEN= # required\n"))
    assert kinds(issues) == [(IssueKind.EMPTY_REQUIRED, "TOKEN")]
    assert issues[0].line_no == 1


def test_empty_quoted_required_value_is_error():
    issues = check_env(parse('TOKEN=""\n'), parse("TOKEN= # required\n"))
    assert kinds(issues) == [(IssueKind.EMPTY_REQUIRED, "TOKEN")]


def test_empty_value_for_optional_key_is_fine():
    assert check_env(parse("LOG_LEVEL=\n"), parse("LOG_LEVEL=info\n")) == []


def test_missing_required_key_reports_missing_only():
    issues = check_env(parse(""), parse("TOKEN= # required\n"))
    assert kinds(issues) == [(IssueKind.MISSING, "TOKEN")]


def test_is_required_marker():
    assert is_required("required")
    assert is_required(" Required ")
    assert not is_required(None)
    assert not is_required("not required")


def test_issue_messages_never_contain_values():
    env = parse("A=topsecret\nA=topsecret2\nEXTRA=hidden\nTOKEN=\n")
    example = parse("A=\nTOKEN= # required\nMISSING=\n")
    issues = check_env(env, example)
    assert {i.kind for i in issues} == {
        IssueKind.DUPLICATE,
        IssueKind.EXTRA,
        IssueKind.EMPTY_REQUIRED,
        IssueKind.MISSING,
    }
    for issue in issues:
        for value in ("topsecret", "hidden"):
            assert value not in issue.message
