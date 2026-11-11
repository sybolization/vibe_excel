"""校验作者声明的 SQL 表血缘，并渲染原始离线界面。

数据来源是作者手写的 lineage.json 与只读 SQLite/CSV；本包不解析 SQL，
公式仅作为文本校验列引用是否成立。渲染产出单个自包含 HTML。
"""

from .build import (
    BuildInput,
    CsvTable,
    DbTable,
    build_lineage,
    buildLineage,
    read_csv,
    read_database,
    readCsv,
    readDatabase,
    split_csv_line,
    splitCsvLine,
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
    parseLineage,
)
from .render import escape_for_script, escapeForScript, render_html, renderHtml


def run_cli(argv: list[str]) -> int:
    """把命令行参数转交给 CLI 模块。

    延迟导入，避免只使用校验或渲染 API 时也加载 CLI 依赖。
    """
    from .cli import run_cli as run

    return run(argv)


__all__ = [
    "FILTER_KINDS", "LINEAGE_VERSION", "BuildInput", "BuiltColumnEdge", "BuiltLineage",
    "BuiltSource", "BuiltTable", "ColumnEdge", "ColumnRef", "CsvTable", "DbTable",
    "FilterKind", "LineageCase", "LineageDoc", "RowFilter", "TableInput", "TableOutput",
    "TableSource", "build_lineage", "escape_for_script", "parse_lineage", "read_csv",
    "read_database", "render_html", "run_cli", "split_csv_line", "buildLineage",
    "escapeForScript", "parseLineage", "readCsv", "readDatabase", "renderHtml", "splitCsvLine",
]
