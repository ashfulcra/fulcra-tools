import pytest
from fhd.teardown import parse_args


def test_parse_args_list():
    a = parse_args(["--list"])
    assert a.list is True


def test_parse_args_delete_by_id():
    a = parse_args(["--delete", "sb_123"])
    assert a.delete == "sb_123"


def test_parse_args_all():
    a = parse_args(["--all"])
    assert a.all is True


def test_parse_args_requires_a_mode():
    # mutually-exclusive group is required -> no args is an error
    with pytest.raises(SystemExit):
        parse_args([])
