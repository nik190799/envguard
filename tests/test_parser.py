from envguard.core.parser import parse


def only_entry(text: str):
    result = parse(text)
    assert result.errors == []
    assert len(result.entries) == 1
    return result.entries[0]


def test_simple_assignment():
    entry = only_entry("KEY=value\n")
    assert (entry.key, entry.value, entry.line_no) == ("KEY", "value", 1)


def test_blank_lines_and_comments_are_skipped():
    result = parse("\n# a comment\n   \n  # indented comment\nA=1\n")
    assert [e.key for e in result.entries] == ["A"]
    assert result.entries[0].line_no == 5
    assert result.errors == []


def test_export_prefix():
    entry = only_entry("export API_KEY=abc")
    assert entry.key == "API_KEY"
    assert entry.value == "abc"
    assert entry.exported is True


def test_export_prefix_followed_by_tab():
    entry = only_entry("export\tKEY=value")
    assert (entry.key, entry.value, entry.exported) == ("KEY", "value", True)


def test_export_prefix_followed_by_several_spaces():
    entry = only_entry("export    KEY=value")
    assert (entry.key, entry.value, entry.exported) == ("KEY", "value", True)


def test_key_named_export_is_not_a_prefix():
    entry = only_entry("export=1")
    assert (entry.key, entry.value, entry.exported) == ("export", "1", False)


def test_key_named_export_with_space_before_equals():
    entry = only_entry("export =1")
    assert (entry.key, entry.value, entry.exported) == ("export", "1", False)


def test_export_without_equals_is_an_error():
    result = parse("export KEY")
    assert result.entries == []
    assert [e.message for e in result.errors] == ["expected KEY=VALUE"]


def test_whitespace_around_equals_is_trimmed():
    entry = only_entry("  KEY  =  spaced value  ")
    assert entry.key == "KEY"
    assert entry.value == "spaced value"


def test_empty_value():
    entry = only_entry("KEY=")
    assert entry.value == ""


def test_value_may_contain_equals():
    entry = only_entry("URL=postgres://u:p@h/db?sslmode=require")
    assert entry.value == "postgres://u:p@h/db?sslmode=require"


def test_inline_comment_after_unquoted_value():
    entry = only_entry("PORT=8080 # http port")
    assert entry.value == "8080"
    assert entry.comment == "http port"


def test_hash_without_preceding_space_is_part_of_value():
    entry = only_entry("COLOR=#ff0000")
    assert entry.value == "#ff0000"
    assert entry.comment is None


def test_empty_value_with_inline_comment():
    entry = only_entry("TOKEN= # required")
    assert entry.value == ""
    assert entry.comment == "required"


def test_single_quoted_value_is_literal():
    entry = only_entry(r"MSG='hello # not a comment \n'")
    assert entry.value == r"hello # not a comment \n"
    assert entry.quote == "'"


def test_double_quoted_value_with_escapes():
    entry = only_entry(r'MSG="say \"hi\"\nbye \\ done"')
    assert entry.value == 'say "hi"\nbye \\ done'
    assert entry.quote == '"'


def test_quoted_value_with_trailing_comment():
    entry = only_entry('NAME="Jane Doe"  # owner')
    assert entry.value == "Jane Doe"
    assert entry.comment == "owner"


def test_empty_quoted_value():
    entry = only_entry('KEY=""')
    assert entry.value == ""


def test_crlf_line_endings():
    result = parse("A=1\r\nB=2\r\n")
    assert [(e.key, e.value) for e in result.entries] == [("A", "1"), ("B", "2")]


def test_line_without_equals_is_malformed():
    result = parse("A=1\nJUSTAKEY\n")
    assert [e.key for e in result.entries] == ["A"]
    assert len(result.errors) == 1
    assert result.errors[0].line_no == 2
    assert "KEY=VALUE" in result.errors[0].message


def test_missing_key_is_malformed():
    result = parse("=value")
    assert result.entries == []
    assert result.errors[0].message == "missing key before '='"


def test_invalid_key_name_is_malformed():
    result = parse("1BAD=x\nBAD-KEY=y\n")
    assert [err.line_no for err in result.errors] == [1, 2]


def test_unterminated_quote_is_malformed():
    result = parse('KEY="never closed')
    assert result.errors[0].message == "unterminated quoted value"


def test_garbage_after_closing_quote_is_malformed():
    result = parse("KEY='a' b")
    assert result.errors[0].message == "unexpected characters after closing quote"


def test_parse_error_messages_never_contain_line_content():
    secret = "sup3r-s3cret"
    result = parse(f"{secret}\nKEY='{secret}' trailing\nX=\"{secret}\n")
    assert len(result.errors) == 3
    assert all(secret not in err.message for err in result.errors)


def test_entry_repr_hides_value():
    entry = only_entry("PASSWORD=hunter2")
    assert "hunter2" not in repr(entry)


def test_duplicate_keys_are_all_kept_in_order():
    result = parse("A=1\nA=2\n")
    assert [e.value for e in result.entries] == ["1", "2"]
    assert result.distinct_keys() == ["A"]
    assert result.as_dict()["A"].value == "2"
