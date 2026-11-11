"""清单（manifest）与补丁层。

形状与 TS 版 manifest.ts 一致：一行一个插件，`id` 是行的身份，补丁按 `id`
覆盖同一行（整体替换 config，不深合并），或用 `insert:` 追加行。
本模块只负责读取、插值与补丁叠加：不导入插件、不校验 config 的业务字段，
也不决定加载顺序——那些在 host.py 与各插件自己的校验器里。

示例：

    plugins:
      - id: store
        name: vibe_excel.plugins:store
        config: { storeRoot: '${VIBE_EXCEL_STORE:-.store}' }

    # 补丁文件（--patch=...）
    - id: store
      disabled: true
    - insert:
        - id: my-plugin
          name: my_plugin
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

import yaml

from vibe_excel.util import VibeError

_INTERPOLATE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


@dataclass(frozen=True, slots=True)
class PluginRow:
    """清单中的一行插件。`disabled` 的行仍保留在列表里，只是不加载。

    补丁里没写出的字段是 None，含义是「沿用原行」，而不是「清空」。
    """

    id: str
    name: str | None = None
    disabled: bool | None = None
    config: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class Manifest:
    """清单文件绝对路径、相对路径解析基准与按顺序保留的插件行。"""

    path: str
    base_dir: str
    rows: list[PluginRow] = field(default_factory=list)


Environment = Mapping[str, str]


def fail(message: str) -> NoReturn:
    """统一的失败出口：包成 VibeError，由 CLI 边界转成退出码。"""

    raise VibeError(message)


def interpolate(value: object, env: Environment | None = None) -> object:
    """展开 `${VAR}` / `${VAR:-默认值}`；只对字符串生效，数组与对象递归。"""

    source = os.environ if env is None else env
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            fallback = match.group(2)
            from_env = source.get(name)
            # 空值按未设置处理：写成 `X=` 往往意味着用户想用默认值，而不是空字符串。
            if from_env is not None and from_env != "":
                return from_env
            if fallback is not None:
                return fallback
            fail(f"清单里的 ${{{name}}} 没有值，也没有默认值")

        return _INTERPOLATE.sub(replace, value)
    if isinstance(value, list):
        return [interpolate(item, source) for item in value]
    if isinstance(value, dict):
        return {
            str(key): interpolate(inner, source) for key, inner in value.items()
        }
    return value


def _as_rows(value: object, where: str) -> list[PluginRow]:
    """把 `plugins: [...]` 或裸数组规范化为插件行。"""

    if isinstance(value, list):
        candidate: object = value
    elif isinstance(value, Mapping):
        candidate = value.get("plugins")
    else:
        candidate = None
    if not isinstance(candidate, list):
        fail(f"{where}：期望插件行数组，实际是 {type(value).__name__}")
    rows: list[PluginRow] = []
    for index, raw in enumerate(candidate):
        if not isinstance(raw, Mapping):
            fail(f"{where}：第 {index + 1} 行不是对象")
        identifier = raw.get("id")
        if not isinstance(identifier, str) or not identifier:
            fail(f"{where}：第 {index + 1} 行缺少 id")
        name_raw = raw.get("name")
        disabled_raw = raw.get("disabled")
        # name / disabled 用 None 表达「本行或补丁未指定」，所以类型不对时也退回 None 而非报错。
        config: dict[str, object] | None = None
        if "config" in raw:
            config_raw = raw["config"]
            if not isinstance(config_raw, Mapping):
                fail(f"{where}：行 {identifier} 的 config 必须是对象")
            config = {str(key): inner for key, inner in config_raw.items()}
        rows.append(
            PluginRow(
                id=identifier,
                name=name_raw if isinstance(name_raw, str) else None,
                disabled=disabled_raw if isinstance(disabled_raw, bool) else None,
                config=config,
            )
        )
    return rows


def _read_yaml(path: Path, where: str) -> object:
    """读 YAML（容忍 BOM）；空文件按空数组处理，读不到或语法错误转成 VibeError。"""

    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        fail(f"无法读取{where} {path}：{error}")
    if not text.strip():
        return []
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as error:
        fail(f"{where} {path} 不是合法 YAML：{error}")
    raise AssertionError("unreachable")


def parse_manifest(path: str | Path, env: Environment | None = None) -> Manifest:
    """解析清单；接受 `plugins: [...]` 或裸数组。"""

    absolute = Path(path).expanduser()
    # 相对路径以当前工作目录为基准：CLI 约定在清单目录里运行，解析结果会写进 Manifest。
    absolute = absolute if absolute.is_absolute() else Path.cwd() / absolute
    absolute = absolute.resolve()
    if not absolute.exists():
        fail(f"清单不存在：{absolute}")
    raw = interpolate(_read_yaml(absolute, "清单"), env)
    rows = _as_rows(raw, str(absolute))
    return Manifest(path=str(absolute), base_dir=str(absolute.parent), rows=rows)


def apply_patch(
    manifest: Manifest, patch_path: str | Path, env: Environment | None = None
) -> Manifest:
    """应用一层补丁：`insert` 追加，按 id 覆盖写出的字段，未知 id 报错。

    非 insert 项必须命中已有 id（拼错 id 视为错误）；insert 里的 id 既可以是新行，
    也可以是对已有行的追加更新，两种都接受。
    """

    candidate = Path(patch_path).expanduser()
    absolute = candidate if candidate.is_absolute() else Path(manifest.base_dir) / candidate
    absolute = absolute.resolve()
    if not absolute.exists():
        fail(f"补丁不存在：{absolute}")
    raw = interpolate(_read_yaml(absolute, "补丁"), env)
    if not isinstance(raw, list):
        fail(f"补丁 {absolute}：期望数组")

    rows = [PluginRow(row.id, row.name, row.disabled, row.config) for row in manifest.rows]
    index = {row.id: position for position, row in enumerate(rows)}
    where = f"补丁 {absolute}"

    def upsert(row: PluginRow) -> None:
        position = index.get(row.id)
        if position is None:
            index[row.id] = len(rows)
            rows.append(row)
            return
        current = rows[position]
        # 整体替换：写出哪个字段就换哪个，未写出的沿用原行；config 不做深合并。
        rows[position] = PluginRow(
            id=row.id,
            name=row.name if row.name is not None else current.name,
            disabled=row.disabled if row.disabled is not None else current.disabled,
            config=row.config if row.config is not None else current.config,
        )

    for position, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            fail(f"{where}：第 {position + 1} 项不是对象")
        if "insert" in entry:
            inserted = _as_rows(entry.get("insert"), f"{where} insert")
            for row in inserted:
                upsert(row)
            continue
        identifier = entry.get("id")
        if not isinstance(identifier, str):
            fail(f"{where}：第 {position + 1} 项既没有 insert 也没有 id")
        target = _as_rows([entry], where)[0]
        if target.id not in index:
            fail(f"{where}：找不到要修改的行 id={target.id}")
        upsert(target)

    return Manifest(path=manifest.path, base_dir=manifest.base_dir, rows=rows)


def resolve_module_specifier(manifest: Manifest, name: str) -> str:
    """相对路径按清单目录解析成绝对路径；模块名原样返回。"""

    if name.startswith("."):
        return str((Path(manifest.base_dir) / name).resolve())
    path = Path(name)
    if path.is_absolute():
        return str(path)
    return name
