import json
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from e2d_core.ledger.jcs import CanonicalizationError, canonicalize


def test_rfc8785_section_3_2_2_example() -> None:
    raw = (
        '{"numbers":[333333333.33333329,1E30,4.50,2e-3,0.000000000000000000000000001],'
        '"string":"\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/",'
        '"literals":[null,true,false]}'
    )
    expected = (
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    )
    assert canonicalize(json.loads(raw)) == expected.encode()


@pytest.mark.parametrize(
    ("number", "text"),
    [
        (0.0, "0"),
        (-0.0, "0"),
        (5e-324, "5e-324"),
        (-5e-324, "-5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (-1.7976931348623157e308, "-1.7976931348623157e+308"),
        (9007199254740992.0, "9007199254740992"),
        (-9007199254740992.0, "-9007199254740992"),
        (295147905179352830000.0, "295147905179352830000"),
        (1e21, "1e+21"),
        (1e20, "100000000000000000000"),
        (4.5, "4.5"),
        (0.002, "0.002"),
        (0.000001, "0.000001"),
        (1e-7, "1e-7"),
        (123456789.0, "123456789"),
        (1.5e-7, "1.5e-7"),
        (333333333.3333333, "333333333.3333333"),
    ],
)
def test_rfc8785_number_formatting(number: float, text: str) -> None:
    assert canonicalize(number) == text.encode()


def test_keys_sorted_by_utf16_code_units() -> None:
    # RFC 8785 section 3.2.3: "\r" < "1" < "\u0080" < "ö" < "€" < "😀" < "דּ".
    data = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    out = canonicalize(data).decode()
    order = [json.loads("[" + part.split(":")[0] + "]")[0] for part in out[1:-1].split(",")]
    assert order == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]


def test_large_integers_follow_double_formatting() -> None:
    assert canonicalize(2**53) == b"9007199254740992"
    assert canonicalize(10**21) == b"1e+21"
    assert canonicalize(2**60) == b"1152921504606847000"
    assert canonicalize(53619795284036010) == b"53619795284036010"


def test_nested_and_integers() -> None:
    assert canonicalize({"b": [1, {"d": 2, "c": None}], "a": "x"}) == (
        b'{"a":"x","b":[1,{"c":null,"d":2}]}'
    )


@pytest.mark.parametrize(
    "bad", [math.nan, math.inf, -math.inf, 2**53 + 1, b"bytes", {1: "x"}, object(), "\ud800"]
)
def test_rejects_values_outside_json(bad: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonicalize(bad)


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=20,
)


@given(json_values)
def test_canonical_form_is_a_fixed_point(value: object) -> None:
    once = canonicalize(value)
    assert canonicalize(json.loads(once)) == once


@given(st.floats(allow_nan=False, allow_infinity=False))
def test_numbers_round_trip(value: float) -> None:
    assert float(canonicalize(value)) == value or value == 0
