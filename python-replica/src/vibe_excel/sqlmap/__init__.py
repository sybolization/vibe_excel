"""Validate author-declared SQL-table lineage and render the original offline UI."""

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
