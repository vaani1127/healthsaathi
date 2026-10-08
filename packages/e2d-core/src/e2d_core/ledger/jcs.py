"""RFC 8785 JSON Canonicalization Scheme (JCS).

Objects are written with keys sorted by UTF-16 code units, no whitespace, strings escaped the way
ECMAScript JSON.stringify does it, and numbers formatted like ECMAScript Number.toString.
"""

import json
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1


class CanonicalizationError(ValueError):
    pass


def canonicalize(value: Any) -> bytes:
    """Return the canonical UTF-8 encoding of a JSON value."""
    return _serialize(value).encode("utf-8")


def _serialize(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, int):
        if abs(value) <= MAX_SAFE_INTEGER + 1:
            return str(value)
        # JSON numbers are IEEE 754 doubles. A larger integer is accepted only when it is already
        # the canonical text of its nearest double, so two different integers never hash the same.
        text = _number(float(value))
        if text != str(value) and int(float(value)) != value:
            raise CanonicalizationError("integer cannot be represented exactly as a double")
        return text
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalizationError("object keys must be strings")
        items = []
        for key in sorted(value, key=_utf16_key):
            items.append(f"{_string(key)}:{_serialize(value[key])}")
        return "{" + ",".join(items) + "}"
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return "[" + ",".join(_serialize(v) for v in value) + "]"
    raise CanonicalizationError(f"cannot canonicalize {type(value).__name__}")


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be", errors="surrogatepass")


def _string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalizationError("strings must be valid Unicode") from exc
    return json.dumps(value, ensure_ascii=False)


def _number(value: float) -> str:
    if math.isnan(value) or math.isinf(value):
        raise CanonicalizationError("NaN and Infinity are not valid JSON")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # repr() gives the shortest digits that round-trip, which is what ECMAScript requires.
    decimal = Decimal(repr(abs(value)))
    digits_tuple, exponent = decimal.as_tuple()[1:]
    digits = "".join(map(str, digits_tuple)).rstrip("0")
    assert isinstance(exponent, int)
    exponent += len("".join(map(str, digits_tuple))) - len(digits)
    k = len(digits)
    n = k + exponent  # value = 0.digits * 10^n

    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * (-n) + digits
    e = n - 1
    exp = f"e{'+' if e >= 0 else '-'}{abs(e)}"
    if k == 1:
        return sign + digits + exp
    return sign + digits[0] + "." + digits[1:] + exp
