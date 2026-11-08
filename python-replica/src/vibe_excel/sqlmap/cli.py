"""sqlmap 的命令行：校验声明并写出 lineage.json 与 index.html。

输入是 SQLite 库、血缘声明 JSON 与可选 CSV 目录；输出目录不得与源文件重叠。
不会执行或解析任何公式，公式只作为文本参与列引用校验。
"""

import json
import sqlite3
import sys
from pathlib import Path

from ..util import VibeError, read_json, write_json
from .build import build_lineage
from .lineage import BuiltLineage
from .render import render_html

USAGE = "build --db <sqlite 路径> --lineage <声明.json> --out <输出目录> [--csv <CSV 目录>]"


def _arguments(argv: list[str]) -> dict[str, str]:
    """解析 build 子命令的参数。

    只接受 --db/--lineage/--out/--csv，前三个必填；未知或缺值直接报错。
    """
    if not argv or argv[0] != "build":
        raise VibeError(f"用法：{USAGE}")
    flags: dict[str, str] = {}
    index = 1
    while index < len(argv):
        token = argv[index]
        if token not in ("--db", "--lineage", "--out", "--csv"):
            raise VibeError(f"未知参数 {token}\n用法：{USAGE}")
        if index + 1 == len(argv) or argv[index + 1].startswith("--"):
            raise VibeError(f"参数 {token} 缺少值\n用法：{USAGE}")
        flags[token[2:]] = argv[index + 1]
        index += 2
    for name in ("db", "lineage", "out"):
        if not flags.get(name):
            raise VibeError(f"缺少参数 --{name}\n用法：{USAGE}")
    return flags


def _summarize(built: BuiltLineage) -> None:
    """打印输入/输出表、列映射与行约束的摘要。"""
    print(f"案例  {built['case']['title']}（{built['case']['id']}）")
    for kind, label in (("input", "输入"), ("output", "输出")):
        tables = [table for table in built["tables"] if table["kind"] == kind]
        description = " · ".join(f"{table['label']} {table['rows'] if table['rows'] is not None else '?'} 行" for table in tables)
        print(f"{label}  {len(tables)} 张表：{description}")
    print(f"列级   {len(built['columns'])} 条映射，其中自引用 {sum(edge['self'] for edge in built['columns'])} 条")
    print(f"行约束 {len(built['filters'])} 条")
    for warning in built["warnings"]:
        print(f"注意  {warning}")


def run_cli(argv: list[str]) -> int:
    """执行一次导出：校验声明、渲染 HTML、写出两个产物。

    已知错误统一转成返回码 1；输出路径与源文件重叠时直接拒绝，避免覆盖声明。
    """
    if argv in (["--help"], ["-h"], ["build", "--help"], ["build", "-h"]):
        print(f"用法：{USAGE}\n产出：lineage.json 与自包含 index.html；不解析或执行公式")
        return 0
    try:
        flags = _arguments(argv)
        db_path, lineage_path = Path(flags["db"]).resolve(), Path(flags["lineage"]).resolve()
        out_dir = Path(flags["out"]).resolve()
        built = build_lineage(read_json(lineage_path), db_path, csv_dir=flags.get("csv"))
        sources = {db_path, lineage_path}
        if flags.get("csv"):
            sources.update(
                (Path(flags["csv"]) / table["artifact"]).resolve()
                for table in built["tables"] if "artifact" in table
            )
        destinations = {out_dir / "lineage.json", out_dir / "index.html"}
        # 输出固定为这两个文件；与源库、声明或 CSV 同名则拒绝，避免自毁输入。
        if sources.intersection(destinations):
            raise VibeError("导出路径会覆盖源数据库、血缘声明或 CSV；请选择另一个输出目录")
        html = render_html(built)
        out_dir.mkdir(parents=True, exist_ok=True)
        write_json(out_dir / "lineage.json", built)
        (out_dir / "index.html").write_text(html, encoding="utf-8")
        _summarize(built)
        print(f"\n落盘  {out_dir / 'lineage.json'}\n      {out_dir / 'index.html'}")
        return 0
    except (VibeError, OSError, UnicodeError, sqlite3.Error, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1


def main() -> int:
    """供 `python -m vibe_excel.sqlmap` 与控制台脚本调用的入口。"""
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
