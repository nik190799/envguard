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


def test_check_clean_exits_zero(write, capsys):
    env = write(".env", "A=1\n")
    example = write(".env.example", "A=\n")
    code, out, _ = run(capsys, "check", "--env", env, "--example", example)
    assert code == 0
    assert out.startswith("OK:")


def test_check_bom_prefixed_files_exit_zero(write, capsys):
    env = write(".env", "﻿A=1\n")
    example = write(".env.example", "﻿A=\n")
    code, out, _ = run(capsys, "check", "--env", env, "--example", example)
    assert code == 0
    assert out.startswith("OK:")


def test_check_missing_key_exits_one(write, capsys):
    env = write(".env", "A=1\n")
    example = write(".env.example", "A=\nB=\n")
    code, out, _ = run(capsys, "check", "--env", env, "--example", example)
    assert code == 1
    assert "B: missing from env file [missing]" in out
    assert "1 error(s), 0 warning(s)" in out


def test_check_warnings_only_exits_zero(write, capsys):
    env = write(".env", "A=1\nEXTRA=2\n")
    example = write(".env.example", "A=\n")
    code, out, _ = run(capsys, "check", "--env", env, "--example", example)
    assert code == 0
    assert f"{env}:2: warning: EXTRA" in out


def test_check_uses_default_paths(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("A=\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, _, _ = run(capsys, "check")
    assert code == 0


def test_check_missing_file_exits_two(write, tmp_path, capsys):
    example = write(".env.example", "A=\n")
    code, out, err = run(capsys, "check", "--env", str(tmp_path / "absent"), "--example", example)
    assert code == 2
    assert out == ""
    assert "file not found" in err


def test_check_never_prints_values(write, capsys):
    env = write(".env", "A=s3cret-a\nA=s3cret-b\nEXTRA=s3cret-c\nbroken s3cret-d\nTOKEN=\n")
    example = write(".env.example", "A=\nTOKEN= # required\nNEED=\n")
    code, out, err = run(capsys, "check", "--env", env, "--example", example)
    assert code == 1
    assert "s3cret" not in out + err
    for kind in ("[duplicate]", "[extra]", "[malformed]", "[empty-required]", "[missing]"):
        assert kind in out


def test_diff_reports_keys_only(write, capsys):
    a = write("a.env", "SAME=1\nGONE=x\nPASSWORD=old-pass\n")
    b = write("b.env", "SAME=1\nNEW=y\nPASSWORD=new-pass\n")
    code, out, _ = run(capsys, "diff", a, b)
    assert code == 1
    assert out.splitlines() == ["+ NEW added", "- GONE removed", "~ PASSWORD changed"]
    assert "old-pass" not in out
    assert "new-pass" not in out


def test_diff_identical_exits_zero(write, capsys):
    a = write("a.env", "A=1\n")
    b = write("b.env", "A=1\n")
    code, out, _ = run(capsys, "diff", a, b)
    assert code == 0
    assert out.strip() == "no differences"


def test_diff_missing_file_exits_two(write, tmp_path, capsys):
    a = write("a.env", "A=1\n")
    code, _, err = run(capsys, "diff", a, str(tmp_path / "absent"))
    assert code == 2
    assert "file not found" in err


def test_no_command_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_unknown_option_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["check", "--bogus"])
    assert exc.value.code == 2


def test_diff_requires_two_files(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["diff", "only-one.env"])
    assert exc.value.code == 2
