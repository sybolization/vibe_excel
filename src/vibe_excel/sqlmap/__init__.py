"""校验作者声明的 SQL 表血缘，并渲染原始离线界面。

数据来源是作者手写的 lineage.json 与只读 SQLite/CSV；本包不解析 SQL，
公式仅作为文本校验列引用是否成立。渲染产出单个自包含 HTML。
"""

from .build import (
    CsvTable,
    DbTable,
    build_lineage,
    read_csv,
    read_database,
    split_csv_line,
)
from .lineage import (
    FILTER_KINDS,
    LINEAGE_VERSION,
    BuiltColumnEdge,
    BuiltLineage,
    BuiltSource,
    BuiltTable,
    ColumnEdge,
    ColumnRef,
    FilterKind,
    LineageCase,
    LineageDoc,
    RowFilter,
    TableInput,
    TableOutput,
    TableSource,
    parse_lineage,
)
from .render import escape_for_script, render_html

__all__ = [
    "FILTER_KINDS", "LINEAGE_VERSION", "BuiltColumnEdge", "BuiltLineage",
    "BuiltSource", "BuiltTable", "ColumnEdge", "ColumnRef", "CsvTable", "DbTable",
    "FilterKind", "LineageCase", "LineageDoc", "RowFilter", "TableInput", "TableOutput",
    "TableSource", "build_lineage", "escape_for_script", "parse_lineage", "read_csv",
    "read_database", "render_html", "split_csv_line",
]
