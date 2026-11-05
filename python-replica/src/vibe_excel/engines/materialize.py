"""把 Excel 按「纯映射」落成 SQLite 表：选 sheet、选列、改名、写表。

这是数据流水线的上游边界。这里**故意**没有业务规则：不去重、不校验、不补默认值、
不改大小写、不按列猜类型。原因有三条：

  1. 物化的表要能当「Excel 的替身」被反复对照。加载器要是顺手洗了数据，数对不上时
     就分不清是「源表本来如此」还是「加载器改过」——上游下游各改一点，归因就断了。
  2. 业务规则必须能单独测试、单独改、单独回滚。把它塞进加载器，就变成「换个 Excel
     批次可能改变下游语义」的隐式耦合。
  3. 双跑比对（同一个流跑两遍比结果）之所以有意义，是因为这一步是确定的：同一批
     输入 + 同一份清单 = 同一张表，和批次里的其他文件无关。

这几个决定在代码里的落点：
  - read_sheet() 用 dtype=object 读，不交给 pandas 做列级类型推断（有空格靠着的数字
    列不会被悄悄变成浮点，日期也不会被改写成别的东西）；
  - project() 只按清单取列、改名，它碰不到值；
  - write_table() 只有一次 to_sql，没有任何 pre-process 的钩子。

文件太脏、纯映射确实表达不了时，用 `load` 例外通道指向一个函数，让「这里偏离了纯
映射」在清单里显式可见，而不是藏进加载器里。

用法：
  python materialize.py --db <hr.db> --inputs <批次目录> --spec <materialize.json> [--repo <干净代码目录>]
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import inspect
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# 表名要跟着 SQL 走，平台和下游都按标识符看待它：先在这道口子上挡住脏名字。
TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 清单里的相对路径：两种分隔符都认（写清单的人可能从 Windows 复制过来）。
PATH_SPLIT = re.compile(r"[\\/]+")


class MaterializeError(Exception):
    """预期内的失败。消息本身就是给人看的（含文件名 / sheet / 列名），不打印 traceback。"""


@contextlib.contextmanager
def xlrd_noise_to_stderr():
    """xlrd 读旧版二进制文件时会往 stdout 打 OLE2/扇区警告。

    stdout 是给进度行的，平台按行读；把这类噪音挪到 stderr，免得夹在
    「[1/2] ...」之间。这里不改任何数据，只改输出去向。
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


@dataclass
class MappingPlan:
    """一条纯映射条目：文件 + sheet → 一张表。"""

    index: int  # 1 起，进度里的 [i/n]
    total: int
    file_spec: str  # 清单里写的 file 原文（报错用）
    path: Path  # 大小写匹配之后真正存在的路径
    sheet: str
    table: str
    columns: dict[str, str] | None  # None = 原样落表

    @property
    def label(self) -> str:
        return f"{self.path.name}#{self.sheet}"


@dataclass
class LoadPlan:
    """一条 load 例外条目：调用干净代码目录里的一个函数，自己写表。"""

    index: int
    total: int
    file_spec: str
    load: str
    table: str
    entry: dict


# ---------------------------------------------------------------------------
# 参数 / 清单
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="materialize.py",
        description="按纯映射把 Excel 落成 SQLite 表（不做任何业务规则）",
    )
    parser.add_argument("--db", required=True, help="目标 SQLite 文件")
    parser.add_argument("--inputs", required=True, help="批次目录；清单里的 file 相对它")
    parser.add_argument("--spec", required=True, help="物化清单（materialize.json）")
    parser.add_argument("--repo", default=None, help="干净代码目录；只给 load 例外通道用")
    return parser.parse_args(argv)


def read_spec(path: Path) -> list[dict]:
    """读清单并只做「形状」校验：值对不对（文件在不在、列在不在）留给后面按条目报。"""
    if not path.is_file():
        raise MaterializeError(f"清单文件不存在：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MaterializeError(f"清单读不出来：{path}（{exc.strerror or exc}）") from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MaterializeError(
            f"清单不是合法 JSON：{path}（第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}）"
        ) from None

    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise MaterializeError(f'清单形状不对：{path} 应该是 {{"entries": [...]}}')

    entries = raw["entries"]
    for index, entry in enumerate(entries):
        where = f"entries[{index}]"
        if not isinstance(entry, dict):
            raise MaterializeError(f"清单形状不对：{where} 应该是对象")
        file_spec = entry.get("file")
        if not isinstance(file_spec, str) or not file_spec.strip():
            raise MaterializeError(f"清单形状不对：{where}.file 应该是非空字符串")
        for key in ("sheet", "table", "load"):
            if key in entry and not isinstance(entry[key], str):
                raise MaterializeError(f"清单形状不对：{where}.{key} 应该是字符串")
        if "columns" in entry:
            columns = entry["columns"]
            if not isinstance(columns, dict):
                raise MaterializeError(
                    f"清单形状不对：{where}.columns 应该是「源列名 → 列名」的对象"
                )
            for source, target in columns.items():
                if not isinstance(target, str) or not target:
                    raise MaterializeError(
                        f"清单形状不对：{where}.columns[{source!r}] 应该是非空字符串"
                    )
    return entries


# ---------------------------------------------------------------------------
# 找文件 / 选 sheet / 校验表名和列
# ---------------------------------------------------------------------------


def list_entries(directory: Path) -> str:
    """把目录里实际有什么列出来——报错时最有用的一句话。"""
    try:
        items = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        return f"（目录读不出来：{exc.strerror or exc}）"
    if not items:
        return "（目录是空的）"
    names = [item.name + ("/" if item.is_dir() else "") for item in items]
    return "、".join(names)


def split_relative(file_spec: str) -> list[str]:
    if os.path.isabs(file_spec):
        raise MaterializeError(f"file 应该相对 --inputs，不能是绝对路径：{file_spec!r}")
    parts = [part for part in PATH_SPLIT.split(file_spec) if part not in ("", ".")]
    if not parts:
        raise MaterializeError(f"file 不是有效路径：{file_spec!r}")
    if ".." in parts:
        raise MaterializeError(f"file 不能跳出批次目录：{file_spec!r}")
    return parts


def resolve_input_file(inputs_dir: Path, file_spec: str) -> Path:
    """把清单里的 file 解析成真实路径。

    先按原样找；找不到再逐段做大小写不敏感匹配（Windows 本来就大小写不敏感，
    这条主要是给 Linux 上的批次目录兜底）。仍然找不到就报错，并把该目录里
    实际有的名字列出来——只报「文件不存在」等于让人去猜。
    """
    current = inputs_dir
    for part in split_relative(file_spec):
        direct = current / part
        if direct.exists():
            current = direct
            continue
        try:
            matches = [child for child in current.iterdir() if child.name.casefold() == part.casefold()]
        except OSError:
            matches = []
        if len(matches) == 1:
            current = matches[0]
            continue
        if len(matches) > 1:
            names = "、".join(sorted(child.name for child in matches))
            raise MaterializeError(
                f"file {file_spec!r} 的大小写不唯一：{current} 里有多个只差大小写的匹配（{names}）"
            )
        where = current if current.is_dir() else inputs_dir
        raise MaterializeError(
            f"file {file_spec!r} 找不到：{where} 里没有 {part!r}。"
            f"该目录里实际有的：{list_entries(where)}"
        )
    if not current.is_file():
        raise MaterializeError(f"file {file_spec!r} 不是文件：{current}")
    return current


def pick_sheet(sheets: list[str], sheet_spec: str | None, file_spec: str) -> str:
    """清单给了 sheet 就必须用它；没给就取第一个。不存在时报错并列出可选的。"""
    if not sheets:
        raise MaterializeError(f"{file_spec} 里没有任何 sheet（空工作簿？）")
    if sheet_spec is None:
        return sheets[0]
    if sheet_spec in sheets:
        return sheet_spec
    available = "、".join(repr(name) for name in sheets)
    raise MaterializeError(
        f"sheet 不存在：{sheet_spec!r}。{file_spec} 里实际有 {len(sheets)} 个 sheet：{available}"
    )


def plan_table(entry: dict) -> str:
    table = entry.get("table")
    if not isinstance(table, str) or not table:
        raise MaterializeError("清单里没写 table：每个条目都要说明落成哪张表")
    if not TABLE_NAME.match(table):
        raise MaterializeError(
            f"表名 {table!r} 不合法：必须匹配 [A-Za-z_][A-Za-z0-9_]*（首字符字母或下划线，其后字母数字下划线）"
        )
    return table


def check_columns(columns: dict, header: pd.DataFrame, label: str) -> dict[str, str]:
    """校验映射里的源列都在表头里，且改名之后不撞名。不改任何值。"""
    if not columns:
        raise MaterializeError(
            f"{label}：columns 是空的。要么不写这个字段（原样落表），要么至少选一列"
        )

    actual = list(header.columns)
    counts = Counter(actual)
    missing = [source for source in columns if source not in counts]
    if missing:
        raise MaterializeError(
            f"{label}：columns 里的源列不存在：{'、'.join(repr(name) for name in missing)}。"
            f"该 sheet 实际有 {len(actual)} 列：{'、'.join(repr(name) for name in actual)}"
        )

    repeated = [source for source in columns if counts[source] > 1]
    if repeated:
        raise MaterializeError(
            f"{label}：源列在表头里出现多次，选不准要哪一个：{'、'.join(repr(name) for name in repeated)}"
        )

    duplicates = [name for name, count in Counter(columns.values()).items() if count > 1]
    if duplicates:
        raise MaterializeError(
            f"{label}：columns 改出来的列名重复：{'、'.join(repr(name) for name in duplicates)}"
            "（一列只能对应一个名字）"
        )
    return dict(columns)


def plan_mapping_entry(
    inputs_dir: Path,
    entry: dict,
    index: int,
    total: int,
    table: str,
) -> MappingPlan:
    file_spec = entry["file"]
    path = resolve_input_file(inputs_dir, file_spec)
    try:
        with xlrd_noise_to_stderr():
            book = pd.ExcelFile(path)
    except Exception as exc:  # 损坏 / 加密 / 扩展名骗人的旧版二进制
        raise MaterializeError(
            f"{file_spec} 打不开（{type(exc).__name__}: {exc}）"
        ) from None

    with book:
        sheet = pick_sheet(book.sheet_names, entry.get("sheet"), file_spec)
        try:
            # 只读表头（nrows=0），校验列名够用，不用把整个 sheet 搬进内存。
            header = book.parse(sheet_name=sheet, nrows=0, dtype=object)
        except Exception as exc:
            raise MaterializeError(
                f"{file_spec}#{sheet} 读表头失败（{type(exc).__name__}: {exc}）"
            ) from None

    label = f"{path.name}#{sheet}"
    columns = entry.get("columns")
    if columns is None:
        # 原样落表：表头就是列名。重名 / 无表头的表在 SQLite 里建不出来，
        # 这种时候只能报错让人改清单或改源表，不能悄悄改名——那已经是业务规则了。
        if len(header.columns) == 0:
            raise MaterializeError(
                f"{label}：这个 sheet 没有表头行，落不出表（SQLite 表至少一列）"
            )
        repeats = [name for name, count in Counter(header.columns).items() if count > 1]
        if repeats:
            raise MaterializeError(
                f"{label}：原样落表时表头有重名列：{'、'.join(repr(name) for name in repeats)}。"
                "用 columns 只挑其中一列并改名，或先在源表里把表头改唯一"
            )
        checked = None
    else:
        checked = check_columns(columns, header, label)

    return MappingPlan(
        index=index,
        total=total,
        file_spec=file_spec,
        path=path,
        sheet=sheet,
        table=table,
        columns=checked,
    )


def split_load(load: str) -> tuple[str, str]:
    file_part, _, func_part = load.rpartition(":")
    if not file_part or not func_part:
        raise MaterializeError(f"load {load!r} 形状不对：要写成 'file.py:func'")
    if os.path.isabs(file_part) or re.match(r"^[A-Za-z]:", file_part):
        raise MaterializeError(f"load {load!r} 应该是相对 --repo 根的路径")
    parts = [part for part in PATH_SPLIT.split(file_part) if part not in ("", ".")]
    if not parts or ".." in parts:
        raise MaterializeError(f"load {load!r} 不能不跳出 --repo 根")
    return file_part, func_part


def loader_path(repo: Path, file_part: str) -> Path:
    parts = [part for part in PATH_SPLIT.split(file_part) if part not in ("", ".")]
    return repo.joinpath(*parts)


def plan_load_entry(
    repo: Path | None,
    entry: dict,
    index: int,
    total: int,
    table: str,
) -> LoadPlan:
    load = entry["load"]
    if repo is None:
        raise MaterializeError(
            f"这条声明了 load={load!r}，但没有给 --repo：例外通道要从干净代码目录里取函数，"
            "否则「跑的是哪个版本」就说不清了"
        )
    file_part, _func = split_load(load)
    target = loader_path(repo, file_part)
    if not target.is_file():
        raise MaterializeError(
            f"load {load!r} 指向的文件不存在：{file_part}（相对 --repo 根 {repo}）"
        )
    return LoadPlan(
        index=index,
        total=total,
        file_spec=entry["file"],
        load=load,
        table=table,
        entry=entry,
    )


# ---------------------------------------------------------------------------
# 读、选列、写
# ---------------------------------------------------------------------------


def read_sheet(path: Path, sheet: str) -> pd.DataFrame:
    """读整张 sheet。

    dtype=object 是「所有值原样保留，不做类型转换」的落点：不交给 pandas 做列级
    类型推断，免得「有空格靠着的整数列变成浮点」「日期被格式化成另一种写法」这类
    隐式转换发生在物化这一步。空 sheet（0 行）照样返回带表头的空 DataFrame。
    """
    try:
        with xlrd_noise_to_stderr():
            return pd.read_excel(path, sheet_name=sheet, dtype=object)
    except Exception as exc:
        raise MaterializeError(f"读取失败（{type(exc).__name__}: {exc}）") from None


def project(frame: pd.DataFrame, columns: dict[str, str] | None) -> pd.DataFrame:
    """只做「选列 + 改名」。

    整个文件里只有这里碰列。它不读值、不比较值、不筛行：不写 if 值 == ... 之类
    的东西，就是「物化不允许有业务规则」在代码上的具体形状。
    """
    if columns is None:
        return frame
    sources = list(columns)
    return frame.loc[:, sources].rename(columns=dict(columns))


def write_table(frame: pd.DataFrame, conn: sqlite3.Connection, table: str) -> None:
    """落表就是一次 to_sql。0 行的空表同样会建出带列名的表。"""
    if len(frame.columns) == 0:
        raise MaterializeError("没有任何列，SQLite 建不出表")
    try:
        frame.to_sql(table, conn, if_exists="replace", index=False)
    except Exception as exc:
        raise MaterializeError(f"写表失败（{type(exc).__name__}: {exc}）") from None
    # pandas 自己会提交，这里只是兜底（不同 pandas 版本行为不完全一致）。
    conn.commit()


def call_loader(plan: LoadPlan, repo: Path, inputs_dir: Path, db_path: Path) -> None:
    """例外通道：把干净代码目录里的一个函数当加载器调用。

    签名约定是 def func(inputs, db, entry)。这是清单里显式承认的偏离，不在这里
    假装还能守住「纯映射」——但也不替它兜业务规则，出了错由它自己的消息说话。
    """
    file_part, func_name = split_load(plan.load)
    target = loader_path(repo, file_part)
    spec = importlib.util.spec_from_file_location(f"_materialize_load_{plan.index}", target)
    if spec is None or spec.loader is None:
        raise MaterializeError(f"加载不了 {plan.load!r} 指向的模块：{target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module

    # 干净代码目录放在 sys.path 最前面：加载函数可以 import 同仓库的模块。
    sys.path.insert(0, str(repo))
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise MaterializeError(
            f"load 文件执行失败：{file_part}（{type(exc).__name__}: {exc}）"
        ) from None
    finally:
        try:
            sys.path.remove(str(repo))
        except ValueError:
            pass

    func = getattr(module, func_name, None)
    if func is None:
        raise MaterializeError(f"{file_part} 里没有 {func_name}")
    if not callable(func):
        raise MaterializeError(f"{plan.load!r} 里的 {func_name} 不是函数")

    try:
        inspect.signature(func).bind(str(inputs_dir), str(db_path), plan.entry)
    except (TypeError, ValueError) as exc:
        raise MaterializeError(
            f"load {plan.load!r} 的签名不对：要写成 def func(inputs, db, entry)（{exc}）"
        ) from None

    try:
        func(str(inputs_dir), str(db_path), plan.entry)
    except SystemExit:
        raise
    except Exception as exc:
        raise MaterializeError(
            f"load {plan.load!r} 执行失败（{type(exc).__name__}: {exc}）"
        ) from None


def progress_line(plan: MappingPlan, frame: pd.DataFrame) -> str:
    return (
        f"[{plan.index}/{plan.total}] {plan.label} → {plan.table}"
        f"  {len(frame)} 行 × {len(frame.columns)} 列"
    )


def load_progress_line(plan: LoadPlan) -> str:
    return (
        f"[{plan.index}/{plan.total}] {plan.load} → {plan.table}"
        "  已调用（load 例外通道，不走纯映射）"
    )


# ---------------------------------------------------------------------------
# 校验全部 → 再统一写
# ---------------------------------------------------------------------------


def plan_all(
    entries: list[dict],
    inputs_dir: Path,
    repo: Path | None,
) -> tuple[list[MappingPlan | LoadPlan], list[tuple[int, str, MaterializeError]]]:
    """先把每条都校验一遍，一条错都不写库：失败时 DB 保持原样，还能一次看全所有问题。"""
    total = len(entries)
    plans: list[MappingPlan | LoadPlan] = []
    errors: list[tuple[int, str, MaterializeError]] = []

    for index, entry in enumerate(entries, start=1):
        file_spec = entry.get("file", "")
        try:
            table = plan_table(entry)
            if "load" in entry:
                plans.append(plan_load_entry(repo, entry, index, total, table))
            else:
                plans.append(plan_mapping_entry(inputs_dir, entry, index, total, table))
        except MaterializeError as exc:
            errors.append((index, file_spec, exc))
        except Exception as exc:  # 文件系统 / pandas 抛出来的也补上「哪一条」这层上下文
            errors.append(
                (index, file_spec, MaterializeError(f"校验时出错（{type(exc).__name__}: {exc}）"))
            )

    # 同一个表名被两条产出：后一条会静默 replace 掉前一条，所以提前报出来。
    seen: dict[str, int] = {}
    for plan in plans:
        key = plan.table.casefold()
        if key in seen:
            errors.append(
                (
                    plan.index,
                    plan.file_spec,
                    MaterializeError(
                        f"表名 {plan.table!r} 和前面的条目重复：一张表只能由一个条目产出"
                        "（否则后写的会静默替换先写的）"
                    ),
                )
            )
        else:
            seen[key] = plan.index

    return plans, errors


def report_errors(errors: list[tuple[int, str, MaterializeError]], total: int) -> None:
    print(
        f"物化失败：{total} 条里有 {len(errors)} 条没通过校验，没有写入任何表",
        file=sys.stderr,
    )
    for index, file_spec, exc in errors:
        message = str(exc)
        # 消息自己带了文件名就不再前缀一次，免得「xxx.xlsx：xxx.xlsx#sheet：...」。
        lead = f"{file_spec}：" if file_spec and file_spec not in message else ""
        print(f"  · entries[{index - 1}] {lead}{message}", file=sys.stderr)


def run_all(
    plans: list[MappingPlan | LoadPlan],
    db_path: Path,
    inputs_dir: Path,
    repo: Path | None,
) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    written: list[str] = []
    try:
        for plan in plans:
            if isinstance(plan, LoadPlan):
                if repo is None:  # 校验阶段已挡住，这里是兜底
                    raise MaterializeError(f"load 条目缺少 --repo：{plan.load}")
                call_loader(plan, repo, inputs_dir, db_path)
                print(load_progress_line(plan), flush=True)
                continue

            try:
                frame = read_sheet(plan.path, plan.sheet)
                frame = project(frame, plan.columns)
                write_table(frame, connection, plan.table)
            except MaterializeError as exc:
                # 写阶段的错误补上「哪一条」：中断时最先要看的是文件和表。
                raise MaterializeError(f"{plan.label} → {plan.table}：{exc}") from None
            written.append(plan.table)
            # 行数 / 列数写在写完之后：失败的时候不该先假报一行成功。
            print(progress_line(plan, frame), flush=True)
    except MaterializeError as exc:
        print(f"物化中断：{exc}", file=sys.stderr)
        if written:
            print(
                f"本次已经写好的表：{'、'.join(written)}（DB 里可能不是完整状态）",
                file=sys.stderr,
            )
        else:
            print("本次还没有写任何表", file=sys.stderr)
        return 1
    finally:
        connection.close()

    print(f"物化完成：{len(plans)} 条 → {db_path}", flush=True)
    return 0


def main(argv: list[str]) -> int:
    # 进度要跨管道可读：Node 侧按 UTF-8 解码，所以不跟随控制台代码页。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    args = parse_args(argv)
    try:
        inputs_dir = Path(args.inputs).resolve()
        if not inputs_dir.is_dir():
            raise MaterializeError(f"--inputs 不是目录：{inputs_dir}")

        entries = read_spec(Path(args.spec))
        if not entries:
            print("清单里没有 entries：没有要落盘的表", flush=True)
            return 0

        repo = Path(args.repo).resolve() if args.repo else None
        if repo is not None and not repo.is_dir():
            raise MaterializeError(f"--repo 不是目录：{repo}")

        db_path = Path(args.db)
        if db_path.is_dir():
            raise MaterializeError(f"--db 指向的是目录：{db_path.resolve()}")

        plans, errors = plan_all(entries, inputs_dir, repo)
        if errors:
            report_errors(errors, len(entries))
            return 1
        return run_all(plans, db_path, inputs_dir, repo)
    except MaterializeError as exc:
        print(f"物化失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
