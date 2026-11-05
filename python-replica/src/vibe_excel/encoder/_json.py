from __future__ import annotations

import json
import math
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import NoReturn

from vibe_excel.util import VibeError, json_text, require_list, require_object, require_text

type Number = int | float

JS_WHITESPACE = (
    "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def trim_text(text: str) -> str:
    return text.strip(JS_WHITESPACE)


def read_utf8(path: str | Path) -> str:
    return Path(path).read_bytes().decode("utf-8", errors="replace")


def _reject_constant(value: str) -> NoReturn:
    raise json.JSONDecodeError("不是合法 JSON 数值", value, 0)


def loads_json(text: str) -> object:
    return json.loads(text, parse_constant=_reject_constant)


def as_record(value: object) -> dict[str, object] | None:
    return require_object(value, "JSON") if isinstance(value, dict) else None


def as_number(value: object) -> Number | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number):
        return None
    if isinstance(value, int) and abs(value) <= 2**53 - 1:
        return value
    return number


def require_number(value: object, where: str) -> Number:
    number = as_number(value)
    if number is None:
        raise VibeError(f"{where} 必须是有限数值")
    return number


def require_integer(value: object, where: str) -> int:
    number = require_number(value, where)
    if number != int(number):
        raise VibeError(f"{where} 必须是整数")
    return int(number)


def optional_text(record: dict[str, object], key: str, where: str) -> str | None:
    value = record.get(key)
    return None if value is None else require_text(value, f"{where}.{key}", allow_empty=True)


def optional_number(record: dict[str, object], key: str, where: str) -> Number | None:
    value = record.get(key)
    return None if value is None else require_number(value, f"{where}.{key}")


def text_list(value: object, where: str) -> list[str]:
    return [
        require_text(item, f"{where}[{index}]", allow_empty=True)
        for index, item in enumerate(require_list(value, where))
    ]


def utf16_key(text: str) -> bytes:
    return text.encode("utf-16-be", errors="surrogatepass")


def _well_formed_json(encoded: str) -> str:
    parts: list[str] = []
    index = 0
    while index < len(encoded):
        code = ord(encoded[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < len(encoded):
            following = ord(encoded[index + 1])
            if 0xDC00 <= following <= 0xDFFF:
                parts.append(chr(0x10000 + ((code - 0xD800) << 10) + following - 0xDC00))
                index += 2
                continue
        parts.append(f"\\u{code:04x}" if 0xD800 <= code <= 0xDFFF else encoded[index])
        index += 1
    return "".join(parts)


def json_string(text: str) -> str:
    return _well_formed_json(json_text(text))


def json_document(value: object, *, indent: int | None = None) -> str:
    return _well_formed_json(json_text(value, indent=indent))


def json_number(value: Number) -> str:
    try:
        number = float(value)
    except OverflowError:
        return "null"
    if not math.isfinite(number):
        return "null"
    if number == 0:
        return "0"
    if isinstance(value, int) and abs(value) <= 2**53 - 1:
        return str(value)
    shortest = repr(number)
    if 1e-6 <= abs(number) < 1e21:
        fixed = format(Decimal(shortest), "f")
        return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
    coefficient, separator, exponent_text = shortest.partition("e")
    if not separator:
        return coefficient.removesuffix(".0")
    exponent = int(exponent_text)
    return f"{coefficient.removesuffix('.0')}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"


_PUNCTUATION = " _-,;:!?.'\"/\\@*&%`^+<=>|~$"
_PUNCTUATION_WEIGHTS = {char: index for index, char in enumerate(_PUNCTUATION)}


def locale_key(text: str) -> tuple[tuple[int, ...], tuple[tuple[int, int], ...], tuple[int, ...]]:
    # IDs are ASCII; reproduce localeCompare's punctuation and lower-before-upper
    # ordering rather than Python's upper-before-lower code-point ordering.
    primary: list[int] = []
    secondary: list[tuple[int, int]] = []
    tertiary: list[int] = []
    for char in unicodedata.normalize("NFD", text):
        if unicodedata.combining(char):
            secondary.append((len(primary), ord(char)))
            continue
        for folded in char.casefold():
            if folded in _PUNCTUATION_WEIGHTS:
                primary.append(_PUNCTUATION_WEIGHTS[folded])
            elif "0" <= folded <= "9":
                primary.append(1000 + ord(folded))
            elif "a" <= folded <= "z":
                primary.append(2000 + ord(folded))
            else:
                primary.append(10000 + ord(folded))
        tertiary.append(int(char.isupper()))
    return tuple(primary), tuple(secondary), tuple(tertiary)
