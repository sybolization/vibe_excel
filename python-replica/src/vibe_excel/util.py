from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PureWindowsPath


class VibeError(RuntimeError):
    """An actionable failure at an application boundary."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise VibeError(f"{where} must be an object with string keys")
    return {key: item for key, item in value.items()}


def require_list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise VibeError(f"{where} must be an array")
    return list(value)


def require_text(value: object, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise VibeError(f"{where} must be a string")
    text = value.strip()
    if not allow_empty and not text:
        raise VibeError(f"{where} must not be empty")
    return text


def positive_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise VibeError(f"{where} must be a positive number")
    try:
        number = float(value)
    except ValueError as error:
        raise VibeError(f"{where} must be a positive number") from error
    if not math.isfinite(number) or number <= 0:
        raise VibeError(f"{where} must be a positive number")
    return number


def camel_case(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part[:1].upper() + part[1:] for part in rest)


def to_wire(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            camel_case(field.name): to_wire(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {key: to_wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_wire(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [to_wire(item) for item in sorted(value)]
    if isinstance(value, bytes):
        return {str(index): byte for index, byte in enumerate(value)}
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


def json_text(
    value: object, *, sort_keys: bool = False, indent: int | None = None
) -> str:
    return json.dumps(
        to_wire(value),
        ensure_ascii=False,
        sort_keys=sort_keys,
        indent=indent,
        separators=(",", ":") if indent is None else None,
        allow_nan=False,
    )


def read_json(path: str | Path) -> object:
    target = Path(path)
    try:
        value: object = json.loads(target.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise VibeError(f"Invalid JSON in {target}:{error.lineno}: {error.msg}") from error
    return value


def write_json(path: str | Path, payload: object) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json_text(payload, indent=2) + "\n", encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@contextlib.contextmanager
def readonly_database(path: str | Path) -> Iterator[sqlite3.Connection]:
    target = Path(path).resolve()
    if not target.is_file():
        raise VibeError(f"Database does not exist: {target}")
    connection = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def relative_path(value: object, where: str, *, suffix: str | None = None) -> str:
    text = require_text(value, where).replace("\\", "/")
    windows = PureWindowsPath(text)
    if windows.drive or text.startswith("/") or ".." in text.split("/"):
        raise VibeError(f"{where} must stay inside its directory: {text}")
    if text in (".", ""):
        raise VibeError(f"{where} must name a file or subdirectory")
    if suffix is not None and not text.endswith(suffix):
        raise VibeError(f"{where} must end with {suffix}: {text}")
    return text


def contained_path(root: str | Path, relative: str) -> Path:
    name = relative_path(relative, "relative path")
    base = Path(root).resolve()
    target = base.joinpath(*name.split("/")).resolve()
    if not target.is_relative_to(base):
        raise VibeError(f"Path escapes its directory: {relative}")
    return target


def escape_for_script(text: str) -> str:
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def fill_template(text: str, variables: Mapping[str, str]) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda match: variables.get(match[1], match[0]), text)


@dataclasses.dataclass(frozen=True)
class Digest:
    path: str
    sha256: str
    bytes: int


@dataclasses.dataclass(frozen=True)
class TableDigest:
    rows: int
    sha256: str


def dir_digests(directory: str | Path, *, skip_git: bool = False) -> list[Digest]:
    root = Path(directory)
    if not root.exists():
        return []
    digests: list[Digest] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if skip_git and ".git" in relative.parts:
            continue
        if path.is_file():
            contained_path(root, relative.as_posix())
            digests.append(Digest(relative.as_posix(), sha256_file(path), path.stat().st_size))
    return digests


def database_digest(
    path: str | Path, only: Sequence[str] | None = None
) -> dict[str, TableDigest]:
    if not Path(path).exists():
        return {}
    wanted = None if only is None else set(only)
    result: dict[str, TableDigest] = {}
    with readonly_database(path) as connection:
        names = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        for row in names:
            table = str(row["name"])
            if wanted is not None and table not in wanted:
                continue
            hasher = hashlib.sha256()
            count = 0
            for record in connection.execute(
                f"SELECT * FROM {quote_identifier(table)} ORDER BY rowid"
            ):
                hasher.update((json_text(dict(record)) + "\n").encode("utf-8"))
                count += 1
            result[table] = TableDigest(count, hasher.hexdigest())
    return result


def batch_id_of(entries: Sequence[Mapping[str, object]]) -> str:
    lines = [
        f"{entry['name']}:{entry['sha256']}:{entry['bytes']}"
        for entry in sorted(entries, key=lambda item: str(item["name"]))
    ]
    return sha256_text("\n".join(lines))[:12]
