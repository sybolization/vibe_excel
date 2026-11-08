"""平台无关的小工具：JSON 规范化、哈希、路径校验与数据库摘要。

所有函数要么返回值，要么抛出 VibeError；不在这里做日志输出或进程管理。内容寻址（sha256）
与路径包含关系校验是它的两条主线：前者服务于快照与制品，后者防止输入路径逃出批次目录。
"""

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
    """应用边界处可据此采取行动的失败。"""


def now() -> str:
    """当前 UTC 时间的 ISO 字符串（毫秒精度，Z 结尾），保证存储与排序一致。"""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_object(value: object, where: str) -> dict[str, object]:
    """要求值是字符串键的 dict，返回浅拷贝。"""
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise VibeError(f"{where} 必须是字符串键的对象")
    return {key: item for key, item in value.items()}


def require_list(value: object, where: str) -> list[object]:
    """要求值是 list，返回浅拷贝。"""
    if not isinstance(value, list):
        raise VibeError(f"{where} 必须是数组")
    return list(value)


def require_text(value: object, where: str, *, allow_empty: bool = False) -> str:
    """要求值是非空字符串并去首尾空白；allow_empty 时允许空串。"""
    if not isinstance(value, str):
        raise VibeError(f"{where} 必须是字符串")
    text = value.strip()
    if not allow_empty and not text:
        raise VibeError(f"{where} 不能为空")
    return text


def positive_number(value: object, where: str) -> float:
    """要求值是有限正数（bool 不算数字），统一转 float。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise VibeError(f"{where} 必须是正数")
    try:
        number = float(value)
    except ValueError as error:
        raise VibeError(f"{where} 必须是正数") from error
    if not math.isfinite(number) or number <= 0:
        raise VibeError(f"{where} 必须是正数")
    return number


def camel_case(name: str) -> str:
    """snake_case 转 camelCase，用于把 Python 数据类序列化成平台线格式。"""
    first, *rest = name.split("_")
    return first + "".join(part[:1].upper() + part[1:] for part in rest)


def to_wire(value: object) -> object:
    """递归把数据类/枚举/Path/集合/bytes 转成可 JSON 序列化的结构。"""
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
    """稳定地序列化：默认紧凑、不转义中文、拒绝 NaN。"""
    return json.dumps(
        to_wire(value),
        ensure_ascii=False,
        sort_keys=sort_keys,
        indent=indent,
        separators=(",", ":") if indent is None else None,
        allow_nan=False,
    )


def read_json(path: str | Path) -> object:
    """读取 UTF-8（可带 BOM）JSON；语法错误转成 VibeError。"""
    target = Path(path)
    try:
        value: object = json.loads(target.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise VibeError(f"{target} 中的 JSON 无效:{error.lineno}: {error.msg}") from error
    return value


def write_json(path: str | Path, payload: object) -> None:
    """把 payload 以缩进 JSON 写入路径（自动创建父目录）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json_text(payload, indent=2) + "\n", encoding="utf-8")


def sha256_text(text: str) -> str:
    """对文本取 SHA-256 十六进制摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    """对文件内容取 SHA-256 十六进制摘要（分块读取，避免大文件占内存）。"""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def quote_identifier(name: str) -> str:
    """把名字转成 SQL 双引号标识符，内部双引号翻倍。"""
    return '"' + name.replace('"', '""') + '"'


@contextlib.contextmanager
def readonly_database(path: str | Path) -> Iterator[sqlite3.Connection]:
    """以只读模式打开数据库（URI mode=ro），退出时关闭；文件不存在直接报错。"""
    target = Path(path).resolve()
    if not target.is_file():
        raise VibeError(f"数据库不存在: {target}")
    # 只读 URI：防止摘要、快照等路径意外写库。
    connection = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def relative_path(value: object, where: str, *, suffix: str | None = None) -> str:
    """校验是相对路径（不含盘符、不以 / 开头、不含 ..），可选校验后缀。"""
    text = require_text(value, where).replace("\\", "/")
    # Windows 盘符与根路径都要拒绝，否则能逃出目标目录。
    windows = PureWindowsPath(text)
    if windows.drive or text.startswith("/") or ".." in text.split("/"):
        raise VibeError(f"{where} 必须位于其目录内: {text}")
    if text in (".", ""):
        raise VibeError(f"{where} 必须指定文件或子目录")
    if suffix is not None and not text.endswith(suffix):
        raise VibeError(f"{where} 必须以 {suffix} 结尾: {text}")
    return text


def contained_path(root: str | Path, relative: str) -> Path:
    """把相对路径解析到 root 之下并确认仍在 root 内。"""
    name = relative_path(relative, "相对路径")
    base = Path(root).resolve()
    target = base.joinpath(*name.split("/")).resolve()
    # 解析后的路径仍可能越界，这里再兜一次。
    if not target.is_relative_to(base):
        raise VibeError(f"路径超出其目录范围: {relative}")
    return target


def escape_for_script(text: str) -> str:
    """把可能破坏 script 块的字符转成 JS 转义序列。"""
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def fill_template(text: str, variables: Mapping[str, str]) -> str:
    """替换 {{名字}} 占位符；未知键原样保留。"""
    return re.sub(r"\{\{(\w+)\}\}", lambda match: variables.get(match[1], match[0]), text)


@dataclasses.dataclass(frozen=True)
class Digest:
    """文件摘要：相对路径、sha256 与字节数。"""

    path: str
    sha256: str
    bytes: int


@dataclasses.dataclass(frozen=True)
class TableDigest:
    """表摘要：行数与内容哈希。"""

    rows: int
    sha256: str


def dir_digests(directory: str | Path, *, skip_git: bool = False) -> list[Digest]:
    """递归列出目录内文件的摘要（按路径排序）；skip_git 时跳过 .git。"""
    root = Path(directory)
    if not root.exists():
        return []
    digests: list[Digest] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if skip_git and ".git" in relative.parts:
            continue
        # 只记文件：目录结构由相对路径隐含。
        if path.is_file():
            contained_path(root, relative.as_posix())
            digests.append(Digest(relative.as_posix(), sha256_file(path), path.stat().st_size))
    return digests


def database_digest(
    path: str | Path, only: Sequence[str] | None = None
) -> dict[str, TableDigest]:
    """对指定的（或全部）用户表按 rowid 顺序计算行数与内容哈希。

    固定行序是必要的：行顺序影响下游结果，哈希必须可复现。
    """
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
            # 固定 rowid 顺序，哈希才可复现。
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
    """由批次条目（name/sha256/bytes）算稳定的短批次 id。"""
    lines = [
        f"{entry['name']}:{entry['sha256']}:{entry['bytes']}"
        for entry in sorted(entries, key=lambda item: str(item["name"]))
    ]
    return sha256_text("\n".join(lines))[:12]
