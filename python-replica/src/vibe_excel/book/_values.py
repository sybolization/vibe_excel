"""Permissive defaults for optional recorded book pages, not SQL lineage."""

import math
from pathlib import Path, PureWindowsPath

from .types import Number


def as_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def as_list(value: object) -> list[object]:
    return [item for item in value] if isinstance(value, list) else []


def as_text(value: object) -> str | None:
    return value if isinstance(value, str) and value != "" else None


def as_number(value: object) -> Number | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if isinstance(value, int) or math.isfinite(value) else None


def strings(value: object) -> list[str]:
    return [item for item in as_list(value) if isinstance(item, str) and item != ""]


def basename(value: str | Path) -> str:
    text = str(value)
    return PureWindowsPath(text).name if "\\" in text else Path(text).name


def first_round(doc: dict[str, object]) -> dict[str, object]:
    rounds = as_object(doc.get("rounds"))
    return as_object(rounds.get("a") if rounds.get("a") is not None else rounds.get("b"))
