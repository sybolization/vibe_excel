"""encoder 私有的 JSON 兼容层，刻意对齐 Node/JS 的序列化行为。

用于把 Python 值稳定序列化为与前端一致的 JSON 文本，并拒绝 NaN/Infinity。
除 read_utf8 会读文件外，其余都是纯函数。
"""

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
    """按 JS 的 trim 语义去掉首尾空白（含 BOM 与 Unicode 空格）。"""
    return text.strip(JS_WHITESPACE)


def read_utf8(path: str | Path) -> str:
    """读入 UTF-8 文本，非法字节替换而不抛错。

    这样损坏的日志仍能抽取其余行，而不是整份读失败。
    """
    return Path(path).read_bytes().decode("utf-8", errors="replace")


def _reject_constant(value: str) -> NoReturn:
    """拒绝 NaN/Infinity 等非标准 JSON 常量。"""
    raise json.JSONDecodeError("不是合法 JSON 数值", value, 0)


def loads_json(text: str) -> object:
    """解析严格 JSON；NaN/Infinity 视为非法，与 JS 的 JSON.parse 一致。"""
    return json.loads(text, parse_constant=_reject_constant)


def as_record(value: object) -> dict[str, object] | None:
    """把对象收窄成字符串键字典；非对象返回 None。"""
    return require_object(value, "JSON") if isinstance(value, dict) else None


def as_number(value: object) -> Number | None:
    """收窄为有限数值，并让超出 2^53 的整数退回浮点。

    这样与 JS 的数字语义一致：安全范围内保持整型，超出后按 double 处理。
    """
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
    """要求有限数值，否则带位置报错。"""
    number = as_number(value)
    if number is None:
        raise VibeError(f"{where} 必须是有限数值")
    return number


def require_integer(value: object, where: str) -> int:
    """要求数值且值等于其整数形式，否则带位置报错。"""
    number = require_number(value, where)
    if number != int(number):
        raise VibeError(f"{where} 必须是整数")
    return int(number)


def optional_text(record: dict[str, object], key: str, where: str) -> str | None:
    """可选文本字段；缺省返回 None，存在时允许为空串。"""
    value = record.get(key)
    return None if value is None else require_text(value, f"{where}.{key}", allow_empty=True)


def optional_number(record: dict[str, object], key: str, where: str) -> Number | None:
    """可选数值字段；缺省返回 None。"""
    value = record.get(key)
    return None if value is None else require_number(value, f"{where}.{key}")


def text_list(value: object, where: str) -> list[str]:
    """校验字符串数组，空串允许存在。"""
    return [
        require_text(item, f"{where}[{index}]", allow_empty=True)
        for index, item in enumerate(require_list(value, where))
    ]


def utf16_key(text: str) -> bytes:
    """把字符串编码成 UTF-16BE 字节序列，用作与 JS 一致的排序键。"""
    return text.encode("utf-16-be", errors="surrogatepass")


def _well_formed_json(encoded: str) -> str:
    """把落单的 UTF-16 代理项转成 \\uXXXX 转义。

    成对的代理项还原成真正的字符；否则 json.dumps 的输出可能包含非法编码。
    """
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
    """序列化单个字符串，并保证输出是合法 UTF-16 文本。"""
    return _well_formed_json(json_text(text))


def json_document(value: object, *, indent: int | None = None) -> str:
    """序列化任意 JSON 值，并修复代理项相关的编码问题。"""
    return _well_formed_json(json_text(value, indent=indent))


def json_number(value: Number) -> str:
    """按 JS 的 Number 习惯序列化数值。

    非有限值写成 null，-0 归一成 0；安全整数保持整型，其余用最短表示。
    """
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
    """构造与 localeCompare 接近的排序键。

    分主键、重音键、大小写键三级比较，让标点、数字、字母的顺序贴近前端。
    """
    # ID 都是 ASCII；复刻 localeCompare 的标点排序以及小写先于大写的顺序，
    # 而不是 Python 大写先于小写的码点顺序。
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
