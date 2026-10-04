from envguard.core.diff import diff_env
from envguard.core.parser import parse


def test_identical_files_have_no_diff():
    result = diff_env(parse("A=1\nB=2\n"), parse("B=2\nA=1\n"))
    assert result.is_empty


def test_added_removed_changed():
    result = diff_env(parse("A=1\nB=2\nC=3\n"), parse("A=1\nB=20\nD=4\n"))
    assert result.added == ["D"]
    assert result.removed == ["C"]
    assert result.changed == ["B"]
    assert not result.is_empty


def test_results_are_sorted():
    result = diff_env(parse(""), parse("Z=1\nA=1\nM=1\n"))
    assert result.added == ["A", "M", "Z"]


def test_quoting_style_alone_is_not_a_change():
    result = diff_env(parse("A=hello\n"), parse('A="hello"\n'))
    assert result.is_empty


def test_last_duplicate_wins():
    result = diff_env(parse("A=1\nA=2\n"), parse("A=2\n"))
    assert result.is_empty
