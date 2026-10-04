import pytest

from envguard.io.reader import EnvFileError, load_env, read_text


def test_load_env_parses_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nB=2\n", encoding="utf-8")
    assert load_env(path).distinct_keys() == ["A", "B"]


def test_leading_bom_is_ignored(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbfA=1\n")
    result = load_env(path)
    assert result.errors == []
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert (entry.key, entry.value, entry.line_no) == ("A", "1", 1)


def test_only_one_leading_bom_is_removed(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbf\xef\xbb\xbfA=1\n")
    assert read_text(path) == "﻿A=1\n"


def test_missing_file_raises(tmp_path):
    with pytest.raises(EnvFileError, match="file not found"):
        read_text(tmp_path / "nope.env")


def test_directory_raises(tmp_path):
    with pytest.raises(EnvFileError, match="not a file"):
        read_text(tmp_path)


def test_invalid_utf8_raises(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"A=\xff\xfe\n")
    with pytest.raises(EnvFileError, match="not valid UTF-8"):
        read_text(path)
