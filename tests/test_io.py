import pytest

from envguard.io.reader import EnvFileError, load_env, read_text


def test_load_env_parses_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("A=1\nB=2\n", encoding="utf-8")
    assert load_env(path).distinct_keys() == ["A", "B"]


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
