"""Validate declared lineage against an existing read-only SQLite DB and CSVs."""

import csv
import io
import sqlite3
from pathlib import Path
from typing import NotRequired, TypedDict

from ..util import VibeError, now, quote_identifier, readonly_database, require_object, require_text
from .lineage import BuiltColumnEdge, BuiltLineage, BuiltTable, ColumnRef, parse_lineage


class DbTable(TypedDict):
    name: str
    rows: int
    columns: list[str]


class CsvTable(TypedDict):
    name: str
    rows: int
    columns: list[str]


class BuildInput(TypedDict):
    doc: object
    dbPath: str
    csvDir: NotRequired[str]


def read_database(db_path: str | Path) -> dict[str, DbTable]:
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
    """Split a CSV record, including quoted commas and doubled quotes."""
    try:
        reader = csv.reader(io.StringIO(line, newline=""), strict=True)
        columns = next(reader, [""])
        if next(reader, None) is not None:
            raise VibeError("CSV 切分需要一个记录，而不是多个记录")
        return [column.strip() for column in columns]
    except csv.Error as error:
        raise VibeError(f"CSV 记录无效：{error}") from error


def read_csv(csv_path: str | Path) -> CsvTable:
    """Count CSV records, not physical lines; preserve BOM/quoting correctness."""
    path = Path(csv_path)
    try:
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
    """Formulas stay author-declared text; this API does not infer SQL lineage."""
    if db_path is None and isinstance(doc, dict) and "dbPath" in doc:
        options = require_object(doc, "build_lineage options")
        db_path = require_text(options.get("dbPath"), "dbPath")
        csv_value = options.get("csvDir")
        if csv_value is not None:
            csv_dir = require_text(csv_value, "csvDir", allow_empty=True)
        doc = options.get("doc")
    parsed = parse_lineage(doc)
    if db_path is None:
        raise VibeError("build_lineage requires db_path")
    database = read_database(db_path)
    warnings: list[str] = []
    tables: dict[str, BuiltTable] = {}
    for declaration in parsed["inputs"]:
        table_id = declaration["id"]
        actual = database.get(table_id)
        if actual is None:
            raise VibeError(f"输入表「{table_id}」在库里不存在。库里有：{', '.join(database)}")
        if "rows" in declaration and declaration["rows"] != actual["rows"]:
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
        output = tables.get(edge["output"])
        if output is None or output["kind"] != "output":
            raise VibeError(f"列映射指向未知输出表「{edge['output']}」")
        if edge["name"] not in output["columns"]:
            raise VibeError(
                f"输出「{edge['output']}」里没有列「{edge['name']}」（输出列：{', '.join(output['columns'])}）"
            )
        for ref in edge["from"]:
            require_ref(ref, f"列映射 {edge['output']}.{edge['name']}")
        built_edge: BuiltColumnEdge = {
            "output": edge["output"], "name": edge["name"], "formula": edge["formula"],
            "from": list(edge["from"]), "self": any(ref["table"] == edge["output"] for ref in edge["from"]),
        }
        if "note" in edge:
            built_edge["note"] = edge["note"]
        columns.append(built_edge)
    for row_filter in parsed["filters"]:
        output = tables.get(row_filter["output"])
        if output is None or output["kind"] != "output":
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
