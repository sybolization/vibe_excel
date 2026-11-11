"""可选 book 页面的宽松默认值，与 SQL 血缘无关。

这些辅助函数把 run.json 这类来源不明的 JSON 值收窄成安全类型；
类型不符时返回空值而不抛错，让缺失页面能继续降级渲染。
"""

import math
from pathlib import Path, PureWindowsPath

from .types import Number


def as_object(value: object) -> dict[str, object]:
    """收窄为字符串键的对象；非对象返回空字典。"""
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def as_list(value: object) -> list[object]:
    """收窄为列表；非列表返回空列表，调用方可安全迭代。"""
    return [item for item in value] if isinstance(value, list) else []


def as_text(value: object) -> str | None:
    """收窄为非空字符串；空串视为缺失。"""
    return value if isinstance(value, str) and value != "" else None


def as_number(value: object) -> Number | None:
    """收窄为有限数值。

    bool 被排除（它不是业务数字）；非有限浮点同样返回 None。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if isinstance(value, int) or math.isfinite(value) else None


def strings(value: object) -> list[str]:
    """抽取数组里的非空字符串，过滤其他类型。"""
    return [item for item in as_list(value) if isinstance(item, str) and item != ""]


def basename(value: str | Path) -> str:
    """取路径的文件名，同时兼容 Windows 与 POSIX 分隔符。"""
    text = str(value)
    return PureWindowsPath(text).name if "\\" in text else Path(text).name


def first_round(doc: dict[str, object]) -> dict[str, object]:
    """取 run.json 里优先存在的执行轮次。

    运行结果可能写在 a 或 b 轮；先 a 后 b，保证读到的是规范轮次。
    """
    rounds = as_object(doc.get("rounds"))
    return as_object(rounds.get("a") if rounds.get("a") is not None else rounds.get("b"))
