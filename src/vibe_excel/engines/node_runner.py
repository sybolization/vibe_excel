"""按名字调用程序里的一个函数。

流里的一个节点 = 一个函数。这个文件只做五件事：
  1. 按路径把目标文件当模块加载（不要求它是包，也不要求目录里有 __init__.py）；
  2. 从命令行拿 key=value 形式的入参；
  3. **把签名上标了 `"t:表名"` 的参数，从库里读成 DataFrame 递进去**（ADR-0008：签名即依赖）；
  4. 只把函数**签名里真的接受**的入参传进去（多余的不传，缺的也不硬塞）；
  5. 调用它，把返回值（如果是 dict）打到 stdout，异常原样抛给调用方。

为什么要这样：入参协议（db / inputs / out / as_of）由平台定义，函数按需声明。
这样同一个文件里的多个函数可以共用一个上下文，也不用为每个节点写一段 argparse。

为什么输入表由平台读（而不是节点自己 read_sql）：节点之间的货币是表，函数拿到的
只是承载它的 DataFrame。声明在签名上，图才画得出来；如果让每个节点自己连库查，
声明就成了注释，抄错也没人知道。**写**仍然是节点自己的事（它自己 to_sql 自己的产出表），
所以「这个节点写了什么」不需要推断。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sqlite3
import sys
import traceback

TABLE_PREFIX = "t:"

# 平台固定递进来的入参。它们的标注不参与表注入（`db` 不可能是一张表）。
RUNTIME = ("db", "inputs", "out", "as_of")


def load_module(path: str):
    """按文件路径加载模块；先注册到 sys.modules 再执行，兼容自引用类型。"""
    spec = importlib.util.spec_from_file_location("_vibe_node_target", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    # 先注册再执行：目标文件里若有 dataclass / 类型注解自引用，需要模块已在 sys.modules 里。
    sys.modules[spec.name] = module
    # 目录用 abspath：`pipeline.py` 这种裸文件名、以及 Windows 的反斜杠，都要能算出对的方向。
    directory = os.path.dirname(os.path.abspath(path))
    sys.path.insert(0, directory)
    try:
        spec.loader.exec_module(module)
    finally:
        if sys.path and sys.path[0] == directory:
            sys.path.pop(0)
    return module


def parse_pairs(argv: list[str]) -> dict[str, str]:
    """把 --key value 序列解析成 dict，键里的连字符转下划线。"""
    values: dict[str, str] = {}
    index = 0
    while index < len(argv):
        key = argv[index]
        if not key.startswith("--"):
            raise SystemExit(f"参数形状不对（期望 --key value）：{key}")
        if index + 1 >= len(argv):
            raise SystemExit(f"参数 {key} 没有值")
        values[key[2:].replace("-", "_")] = argv[index + 1]
        index += 2
    return values


def table_of(annotation) -> str | None:
    """注解是 `"t:表名"` 时返回表名，否则 None。

    两种形态都要认：文件里写了 `from __future__ import annotations` 时，`inspect`
    拿到的是**源码文本**（`'\'t:raw\''`，比字符串字面量多一层引号）；没写时是字面量本身。
    """
    if annotation is inspect.Parameter.empty or not isinstance(annotation, str):
        return None
    text = annotation.strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text.startswith(TABLE_PREFIX):
        return None
    name = text[len(TABLE_PREFIX) :].strip()
    return name or None


def read_tables(db_path: str, tables: list[str], func_name: str) -> dict[str, object]:
    """把节点声明要吃的表读成 DataFrame。懒 import pandas：没有表参数就不付这个开销。"""
    if not os.path.exists(db_path):
        raise SystemExit(f"业务库不存在：{db_path}（函数 {func_name} 声明了输入表，但库还没有）")
    import pandas as pd  # noqa: PLC0415 - 故意懒加载

    frames: dict[str, object] = {}
    con = sqlite3.connect(db_path)
    try:
        for table in tables:
            if table in frames:
                continue
            try:
                frames[table] = pd.read_sql_query('SELECT * FROM "%s"' % table, con)
            except Exception as error:  # noqa: BLE001 - 要把「哪张表读不出来」说清楚
                raise SystemExit(
                    f"函数 {func_name} 的输入表 {table} 读不出来（上游没产出，或者表名写错了）：{error}"
                ) from error
    finally:
        con.close()
    return frames


def call_with_signature(func, values: dict[str, str]):
    """只传函数签名真正接受的参数；t: 标注的参数从库里读成 DataFrame。"""
    signature = inspect.signature(func)
    parameters = list(signature.parameters.items())

    # 参数名 → 表名。**索引必须是参数名**：参数叫 raw、表叫 raw_employees 是常态。
    # 固定入参即使被标了 t: 也按固定入参处理（解析器那边已经不管它们的标注了）。
    wanted: dict[str, str] = {}
    for name, parameter in parameters:
        if name in RUNTIME:
            continue
        table = table_of(parameter.annotation)
        if table is not None:
            wanted[name] = table

    # 同一张表被多个参数引用时只读一次。
    tables = sorted(set(wanted.values()))
    if tables and "db" not in values:
        raise SystemExit(
            f"函数 {func.__name__} 用 t: 声明了输入表（{', '.join(tables)}），但没有 db 入参："
            f"平台拿不到连接去读它。可用的入参：{', '.join(sorted(values))}"
        )
    frames = read_tables(values["db"], tables, func.__name__) if tables else {}

    accepts_extra = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
    kwargs = {}
    for name, parameter in parameters:
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
            raise SystemExit(f"函数 {func.__name__} 的参数 {name} 是位置限定的（`/`）：平台按关键字传参")
        if name in wanted:
            kwargs[name] = frames[wanted[name]]
        elif name in values:
            kwargs[name] = values[name]
        elif parameter.default is inspect.Parameter.empty and parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            raise SystemExit(
                f"函数 {func.__name__} 需要参数 {name}，但平台没有提供；"
                f"可用的入参：{', '.join(sorted(values))}"
                f"（标 `\"t:表名\"` 的参数由平台从库里读，不用自己查）"
            )
    if accepts_extra:
        # 有 **kwargs 时把平台全部入参也传进去，节点可按需取用。
        for name, value in values.items():
            kwargs.setdefault(name, value)
    return func(**kwargs)


def main(argv: list[str]) -> int:
    """命令行入口：加载模块、取函数、按签名调用，dict 返回值打到 stdout。"""
    if len(argv) < 2:
        raise SystemExit("用法：node_runner.py <文件> <函数名> [--key value ...]")
    path, name = argv[0], argv[1]
    values = parse_pairs(argv[2:])

    module = load_module(path)
    func = getattr(module, name, None)
    if func is None or not callable(func):
        raise SystemExit(f"{path} 里没有可调用的 {name}")
    if not inspect.isfunction(func):
        raise SystemExit(f"{path}:{name} 不是函数（节点必须指向一个函数）")

    result = call_with_signature(func, values)
    # 只有 dict 返回值才序列化；写表由节点自己负责。
    if isinstance(result, dict):
        json.dump(result, sys.stdout, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
