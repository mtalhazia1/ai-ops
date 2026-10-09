import pytest

from inbox.containers import check_digit, is_valid


@pytest.mark.parametrize("number", ["CSQU3054383", "csqu 305438 3", "MSCU1234566", "TGHU1234567"])
def test_valid(number):
    assert is_valid(number)


@pytest.mark.parametrize("number", ["CSQU3054384", "MSCU1234565", "CSQ3054383", "", "ABCD12345678"])
def test_invalid(number):
    assert not is_valid(number)


def test_check_digit_value():
    assert check_digit("CSQU305438") == 3
