import pytest
from settings_reader import parse_port


def test_invalid_text_raises():
    with pytest.raises(ValueError):
        parse_port("abc")
