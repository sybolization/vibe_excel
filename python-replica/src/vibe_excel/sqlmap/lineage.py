"""Author-declared column lineage; formulas are text, never executable SQL."""

import math
from typing import Literal, NotRequired, TypedDict

from ..util import VibeError, require_list, require_object

LINEAGE_VERSION = "0.1"
FILTER_KINDS = ("rows", "group-key")
FilterKind = Literal["rows", "group-key"]
Number = int | float


class ColumnRef(TypedDict):
    table: str
    column: str


ColumnEdge = TypedDict(
    "ColumnEdge",
    {
        "output": str,
        "name": str,
        "formula": str,
        "note": NotRequired[str],
        "from": list[ColumnRef],
    },
)


class BuiltColumnEdge(ColumnEdge):
    self: bool


RowFilter = TypedDict(
    "RowFilter",
    {
        "output": str,
        "name": str,
        "kind": FilterKind,
        "formula": str,
        "note": NotRequired[str],
        "from": list[ColumnRef],
    },
)


class TableSource(TypedDict):
    file: str
    sheet: NotRequired[str]


class TableInput(TypedDict):
    id: str
    label: NotRequired[str]
    origin: NotRequired[str]
    source: NotRequired[TableSource]
    rows: NotRequired[Number]
    columns: NotRequired[list[str]]


class TableOutput(TableInput):
    artifact: NotRequired[str]


class LineageCase(TypedDict):
    id: str
    title: str
    provenance: str
    asOf: NotRequired[str]


class LineageDoc(TypedDict):
    version: Literal["0.1"]
    case: LineageCase
    inputs: list[TableInput]
    outputs: list[TableOutput]
    columns: list[ColumnEdge]
    filters: list[RowFilter]


class BuiltTable(TypedDict):
    id: str
    label: str
    kind: Literal["input", "output"]
    rows: Number | None
    columns: list[str]
    origin: NotRequired[str]
    source: NotRequired[TableSource]
    artifact: NotRequired[str]


class BuiltSource(TypedDict):
    db: str
    dbTables: list[str]
    csvDir: str | None


class BuiltLineage(TypedDict):
    version: Literal["0.1"]
    case: LineageCase
    builtAt: str
    source: BuiltSource
    tables: list[BuiltTable]
    columns: list[BuiltColumnEdge]
    filters: list[RowFilter]
    warnings: list[str]


def _fail(path: str, message: str) -> VibeError:
    return VibeError(f"lineage 校验失败：{path} {message}")


def _object(value: object, path: str) -> dict[str, object]:
    try:
        return require_object(value, path)
    except VibeError as error:
        raise _fail(path, "必须是对象") from error


def _list(value: object, path: str) -> list[object]:
    try:
        return require_list(value, path)
    except VibeError as error:
        raise _fail(path, "必须是数组") from error


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(path, "必须是非空字符串")
    return value


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail(path, "必须是字符串")
    return value


def _refs(value: object, path: str) -> list[ColumnRef]:
    if value is None:
        return []
    result: list[ColumnRef] = []
    for index, raw in enumerate(_list(value, path)):
        item = _object(raw, f"{path}[{index}]")
        result.append(
            {
                "table": _text(item.get("table"), f"{path}[{index}].table"),
                "column": _text(item.get("column"), f"{path}[{index}].column"),
            }
        )
    return result


def _source(value: object, path: str) -> TableSource | None:
    if value is None:
        return None
    item = _object(value, path)
    result: TableSource = {"file": _text(item.get("file"), f"{path}.file")}
    sheet = _optional_text(item.get("sheet"), f"{path}.sheet")
    if sheet:
        result["sheet"] = sheet
    return result


def _table(value: object, path: str, *, output: bool) -> TableOutput:
    item = _object(value, path)
    table: TableOutput = {"id": _text(item.get("id"), f"{path}.id")}
    label = _optional_text(item.get("label"), f"{path}.label")
    origin = _optional_text(item.get("origin"), f"{path}.origin")
    source = _source(item.get("source"), f"{path}.source")
    if label:
        table["label"] = label
    if origin:
        table["origin"] = origin
    if source is not None:
        table["source"] = source
    rows = item.get("rows")
    if rows is not None:
        if (
            isinstance(rows, bool)
            or not isinstance(rows, (int, float))
            or (isinstance(rows, float) and not math.isfinite(rows))
            or rows < 0
        ):
            raise _fail(f"{path}.rows", "必须是非负有限数字")
        table["rows"] = rows
    if item.get("columns") is not None:
        table["columns"] = [
            _text(column, f"{path}.columns[{index}]")
            for index, column in enumerate(_list(item["columns"], f"{path}.columns"))
        ]
    if output:
        artifact = _optional_text(item.get("artifact"), f"{path}.artifact")
        if artifact:
            table["artifact"] = artifact
    return table


def parse_lineage(raw: object) -> LineageDoc:
    """Validate the declaration's structure; real-column checks happen in build."""
    root = _object(raw, "$")
    version = _text(root.get("version"), "$.version")
    if version != LINEAGE_VERSION:
        raise _fail("$.version", f"只支持 {LINEAGE_VERSION}，实际是 {version}")
    case_record = _object(root.get("case"), "$.case")
    case: LineageCase = {
        "id": _text(case_record.get("id"), "$.case.id"),
        "title": _text(case_record.get("title"), "$.case.title"),
        "provenance": _text(case_record.get("provenance"), "$.case.provenance"),
    }
    as_of = _optional_text(case_record.get("asOf"), "$.case.asOf")
    if as_of:
        case["asOf"] = as_of
    inputs: list[TableInput] = [
        _table(item, f"$.inputs[{index}]", output=False)
        for index, item in enumerate(_list(root.get("inputs"), "$.inputs"))
    ]
    outputs = [
        _table(item, f"$.outputs[{index}]", output=True)
        for index, item in enumerate(_list(root.get("outputs"), "$.outputs"))
    ]
    if not inputs:
        raise _fail("$.inputs", "至少需要一张输入表")
    if not outputs:
        raise _fail("$.outputs", "至少需要一张输出表")
    seen: set[str] = set()
    for table in [*inputs, *outputs]:
        if table["id"] in seen:
            raise _fail("$.inputs/outputs", f"表 id「{table['id']}」重复")
        seen.add(table["id"])

    columns: list[ColumnEdge] = []
    for index, raw_edge in enumerate(_list(root.get("columns"), "$.columns")):
        path = f"$.columns[{index}]"
        item = _object(raw_edge, path)
        edge: ColumnEdge = {
            "output": _text(item.get("output"), f"{path}.output"),
            "name": _text(item.get("name"), f"{path}.name"),
            "formula": _optional_text(item.get("formula"), f"{path}.formula") or "",
            "from": _refs(item.get("from"), f"{path}.from"),
        }
        note = _optional_text(item.get("note"), f"{path}.note")
        if note:
            edge["note"] = note
        columns.append(edge)

    filters: list[RowFilter] = []
    raw_filters = root.get("filters")
    for index, raw_filter in enumerate(_list([] if raw_filters is None else raw_filters, "$.filters")):
        path = f"$.filters[{index}]"
        item = _object(raw_filter, path)
        kind = _text(item.get("kind"), f"{path}.kind")
        if kind not in FILTER_KINDS:
            raise _fail(f"{path}.kind", f"只能是 {' / '.join(FILTER_KINDS)}")
        filter_kind: FilterKind = "rows" if kind == "rows" else "group-key"
        row_filter: RowFilter = {
            "output": _text(item.get("output"), f"{path}.output"),
            "name": _text(item.get("name"), f"{path}.name"),
            "kind": filter_kind,
            "formula": _text(item.get("formula"), f"{path}.formula"),
            "from": _refs(item.get("from"), f"{path}.from"),
        }
        note = _optional_text(item.get("note"), f"{path}.note")
        if note:
            row_filter["note"] = note
        filters.append(row_filter)
    return {
        "version": "0.1",
        "case": case,
        "inputs": inputs,
        "outputs": outputs,
        "columns": columns,
        "filters": filters,
    }


parseLineage = parse_lineage
