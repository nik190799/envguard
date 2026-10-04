"""CLI tests for issue #3: diff warns about malformed lines, missing points at the example line."""

import pytest

from envguard.cli.main import main


@pytest.fixture
def write(tmp_path):
    def _write(name: str, text: str) -> str:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    return _write


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_check_missing_key_points_at_example_line(write, capsys):
    env = write(".env", "A=1\n")
    example = write(".env.example", "A=\n# comment\nB=\n")
    code, out, _ = run(capsys, "check", "--env", env, "--example", example)
    assert code == 1
    missing = [line for line in out.splitlines() if "[missing]" in line]
    assert missing == [f"{example}:3: error: B: missing from env file [missing]"]
    assert missing[0].startswith(f"{example}:3:")


def test_check_missing_key_points_at_first_example_definition(write, capsys):
    env = write(".env", "A=1\n")
    example = write(".env.example", "A=\nB=\nB=\n")
    code, out, _ = run(capsys, "check", "--env", env, "--example", example)
    assert code == 1
    missing = [line for line in out.splitlines() if "[missing]" in line]
    assert missing == [f"{example}:2: error: B: missing from env file [missing]"]


def test_diff_well_formed_files_have_empty_stderr(write, capsys):
    a = write("a.env", "A=1\nB=2\n")
    b = write("b.env", "A=1\nC=3\n")
    code, _, err = run(capsys, "diff", a, b)
    assert code == 1
    assert err == ""


def test_diff_warns_about_malformed_line_without_content(write, capsys):
    a = write("a.env", "A=1\nbroken s3cret-line\n")
    b = write("b.env", "A=1\n")
    code, out, err = run(capsys, "diff", a, b)
    assert code == 0
    assert out.strip() == "no differences"
    assert f"{a}:2" in err
    assert "s3cret" not in err
    assert "broken" not in err


def test_diff_warns_once_per_malformed_line_in_both_files(write, capsys):
    a = write("a.env", "bad-old-1\nA=1\nbad-old-2\n")
    b = write("b.env", "A=1\nbad-new-1\n")
    code, out, err = run(capsys, "diff", a, b)
    assert code == 0
    assert out.strip() == "no differences"
    warnings = err.splitlines()
    assert len(warnings) == 3
    assert warnings[0].startswith("envguard: ")
    assert f"{a}:1:" in warnings[0]
    assert f"{a}:3:" in warnings[1]
    assert f"{b}:2:" in warnings[2]
    assert "bad-" not in err
