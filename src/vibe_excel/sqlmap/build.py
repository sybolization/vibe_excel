"""基于现有只读 SQLite 库与 CSV，校验声明的血缘。

数据来源是作者声明的 lineage 文档、只读打开的 SQLite 库和可选 CSV 产物。
表/列引用必须真实存在；行数以库和 CSV 的实际值为准，声明值只用于告警。
"""

import csv
import io
import sqlite3
from pathlib import Path
from typing import NotRequired, TypedDict

from ..util import VibeError, now, quote_identifier, readonly_database, require_object, require_text
from .lineage import BuiltColumnEdge, BuiltLineage, BuiltTable, ColumnRef, parse_lineage


class DbTable(TypedDict):
    """从 SQLite 读出的一张表：真实行数与列清单。"""

    name: str
    rows: int
    columns: list[str]


class CsvTable(TypedDict):
    """从 CSV 读出的一张表：记录数与非空表头。"""

    name: str
    rows: int
    columns: list[str]


class BuildInput(TypedDict):
    """兼容 camelCase 调用的 build_lineage 入参映射。"""

    doc: object
    dbPath: str
    csvDir: NotRequired[str]


def read_database(db_path: str | Path) -> dict[str, DbTable]:
    """只读打开数据库并统计所有 table/view。

    跳过 sqlite_ 内部表；视图也计入，因为它们可被血缘引用。
    """
    path = Path(db_path)
    if not path.is_file():
        raise VibeError(f"数据库不存在：{path}")
    tables: dict[str, DbTable] = {}
    try:
        with readonly_database(path) as db:
            names = [
                require_text(row["name"], "sqlite_master.name")
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            for name in names:
                row = db.execute(f"SELECT COUNT(*) AS n FROM {quote_identifier(name)}").fetchone()
                count: object = row["n"] if row is not None else None
                if not isinstance(count, int):
                    raise VibeError(f"表「{name}」没有有效行数")
                columns = [
                    require_text(column["name"], f"{name}.column")
                    for column in db.execute("SELECT name FROM pragma_table_info(?)", (name,))
                ]
                tables[name] = {"name": name, "rows": count, "columns": columns}
    except sqlite3.Error as error:
        raise VibeError(f"无法读取数据库 {path}：{error}") from error
    return tables


def split_csv_line(line: str) -> list[str]:
    """切分一条 CSV 记录，支持引号内的逗号与成对引号。

    strict 模式让非法引号直接报错，而不是静默截断；多记录会被拒绝。
    """
    try:
        reader = csv.reader(io.StringIO(line, newline=""), strict=True)
        columns = next(reader, [""])
        if next(reader, None) is not None:
            raise VibeError("CSV 切分需要一个记录，而不是多个记录")
        return [column.strip() for column in columns]
    except csv.Error as error:
        raise VibeError(f"CSV 记录无效：{error}") from error


def read_csv(csv_path: str | Path) -> CsvTable:
    """统计 CSV 记录数而非物理行数，同时保证 BOM 与引号处理正确。

    Excel 导出的 CSV 常带 BOM，用 utf-8-sig 读入以免首列表头被污染；
    只统计非空记录，避免末尾空行抬高行数。
    """
    path = Path(csv_path)
    try:
        # Excel 导出的 CSV 常带 BOM，用 utf-8-sig 去掉，避免首列名被污染。
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            columns = [column.strip() for column in next(reader, [])]
            rows = sum(1 for row in reader if row)
    except (OSError, UnicodeError, csv.Error) as error:
        raise VibeError(f"无法读取 CSV {path}：{error}") from error
    return {"name": str(csv_path), "rows": rows, "columns": columns}


def build_lineage(
    doc: object = None,
    db_path: str | Path | None = None,
    *,
    csv_dir: str | Path | None = None,
) -> BuiltLineage:
    """校验声明并生成可渲染的血缘文档。

    公式仍是作者声明的文本；本 API 不推导也不执行 SQL 血缘。
    输入表必须在库中存在，输出列必须声明完整，否则直接报错。
    """
    if db_path is None and isinstance(doc, dict) and "dbPath" in doc:
        options = require_object(doc, "build_lineage 参数")
        db_path = require_text(options.get("dbPath"), "dbPath")
        csv_value = options.get("csvDir")
        if csv_value is not None:
            csv_dir = require_text(csv_value, "csvDir", allow_empty=True)
        doc = options.get("doc")
    parsed = parse_lineage(doc)
    if db_path is None:
        raise VibeError("build_lineage 需要 db_path")
    database = read_database(db_path)
    warnings: list[str] = []
    tables: dict[str, BuiltTable] = {}
    for declaration in parsed["inputs"]:
        table_id = declaration["id"]
        actual = database.get(table_id)
        if actual is None:
            raise VibeError(f"输入表「{table_id}」在库里不存在。库里有：{', '.join(database)}")
        if "rows" in declaration and declaration["rows"] != actual["rows"]:
            # 行数以数据库实际值为准，声明值只用于提醒作者声明已过期。
            warnings.append(
                f"输入表「{table_id}」声明 {declaration['rows']} 行，库里实际 {actual['rows']} 行；以库为准"
            )
        table: BuiltTable = {
            "id": table_id, "label": declaration.get("label", table_id), "kind": "input",
            "rows": actual["rows"], "columns": list(actual["columns"]),
        }
        if "origin" in declaration:
            table["origin"] = declaration["origin"]
        if "source" in declaration:
            table["source"] = declaration["source"]
        tables[table_id] = table

    for declaration in parsed["outputs"]:
        table_id = declaration["id"]
        output: BuiltTable = {
            "id": table_id, "label": declaration.get("label", table_id), "kind": "output",
            "rows": declaration.get("rows"), "columns": list(declaration.get("columns", [])),
        }
        if "origin" in declaration:
            output["origin"] = declaration["origin"]
        if csv_dir is not None and str(csv_dir):
            artifact = declaration.get("artifact", f"{table_id}.csv")
            csv_path = Path(csv_dir) / artifact
            output["artifact"] = artifact
            if csv_path.is_file():
                actual_csv = read_csv(csv_path)
                output["rows"], output["columns"] = actual_csv["rows"], actual_csv["columns"]
            else:
                # CSV 缺失不视为失败：保留声明值，由警告提示产物尚未生成。
                warnings.append(f"输出「{table_id}」找不到文件 {csv_path}，沿用声明值")
        if not output["columns"] or any(not name.strip() for name in output["columns"]):
            raise VibeError(f"输出表「{table_id}」没有有效列清单，无法渲染")
        tables[table_id] = output

    def require_ref(ref: ColumnRef, where: str) -> None:
        table = tables.get(ref["table"])
        if table is None:
            raise VibeError(
                f"{where} 引用了不存在的表「{ref['table']}」（可用：{', '.join(tables)}）"
            )
        if ref["column"] not in table["columns"]:
            raise VibeError(f"{where} 引用了表「{ref['table']}」里不存在的列「{ref['column']}」")

    columns: list[BuiltColumnEdge] = []
    for edge in parsed["columns"]:
        output_table = tables.get(edge["output"])
        if output_table is None or output_table["kind"] != "output":
            raise VibeError(f"列映射指向未知输出表「{edge['output']}」")
        if edge["name"] not in output_table["columns"]:
            raise VibeError(
                f"输出「{edge['output']}」里没有列「{edge['name']}」"
                f"（输出列：{', '.join(output_table['columns'])}）"
            )
        for ref in edge["from"]:
            require_ref(ref, f"列映射 {edge['output']}.{edge['name']}")
        built_edge: BuiltColumnEdge = {
            "output": edge["output"], "name": edge["name"], "formula": edge["formula"],
            # self 标记引用了同表其他列，前端据此高亮自引用。
            "from": list(edge["from"]), "self": any(ref["table"] == edge["output"] for ref in edge["from"]),
        }
        if "note" in edge:
            built_edge["note"] = edge["note"]
        columns.append(built_edge)
    for row_filter in parsed["filters"]:
        filter_table = tables.get(row_filter["output"])
        if filter_table is None or filter_table["kind"] != "output":
            raise VibeError(f"行过滤指向未知输出表「{row_filter['output']}」")
        for ref in row_filter["from"]:
            require_ref(ref, f"行过滤 {row_filter['output']}.{row_filter['name']}")
    mapped_outputs = {edge["output"] for edge in columns}
    for declaration in parsed["outputs"]:
        if declaration["id"] not in mapped_outputs:
            warnings.append(f"输出「{declaration['id']}」没有任何列映射")
    return {
        "version": "0.1", "case": parsed["case"], "builtAt": now(),
        "source": {
            "db": str(db_path), "dbTables": sorted(database),
            "csvDir": str(csv_dir) if csv_dir is not None else None,
        },
        "tables": list(tables.values()), "columns": columns,
        "filters": parsed["filters"], "warnings": warnings,
    }


buildLineage = build_lineage
readDatabase = read_database
readCsv = read_csv
splitCsvLine = split_csv_line
