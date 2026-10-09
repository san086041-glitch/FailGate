import pytest

from confkit import get, parse


@pytest.mark.unit
def test_sections(server_config: str) -> None:
    assert parse(server_config) == {"server": {"host": "example.org", "port": "8080"}}


def test_comments_and_blank_lines_are_ignored() -> None:
    assert parse("# hi\n\n[a]\n; note\nx = 1\n") == {"a": {"x": "1"}}


def test_get_with_default(server_config: str) -> None:
    assert get(server_config, "server", "host") == "example.org"
    assert get(server_config, "server", "missing", "d") == "d"


def test_line_without_equals_is_an_error() -> None:
    with pytest.raises(ValueError):
        parse("[a]\njust text\n")
