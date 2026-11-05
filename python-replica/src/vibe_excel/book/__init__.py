"""Read-only collection and standalone visualization of a vibe-excel store."""

from .collect import collect_book, collectBook, read_db_facts, read_table_stat, sample_table
from .diagram import MAX_RANK, build_diagram, buildDiagram
from .graph import build_graph, buildGraph
from .inputs import build_inputs, buildInputs
from .render import escape_for_script, escapeForScript, render_book, renderBook
from .transcript import one_line, oneLine, parse_transcript, parseTranscript
from .types import (
    BatchFile,
    BatchView,
    BookView,
    BuildDiagramInput,
    BuildGraphInput,
    BuildInputsInput,
    Cell,
    CollectOptions,
    DbTableFact,
    DiagramEdge,
    DiagramEdgeKind,
    DiagramKind,
    DiagramNode,
    DiagramView,
    EpisodeView,
    GraphView,
    InputFileView,
    InputsView,
    InputTableView,
    MaterializeView,
    NodeView,
    Number,
    ProgramView,
    PublishView,
    RevisionView,
    RunRecord,
    RunView,
    SeedKind,
    SessionView,
    SourceRef,
    StepView,
    TableData,
    TableRole,
    TableStat,
    Usage,
    WriterView,
)


def run_cli(argv: list[str]) -> int:
    from .cli import run_cli as run

    return run(argv)


__all__ = [
    "BatchFile", "BatchView", "BookView", "BuildDiagramInput", "BuildGraphInput",
    "BuildInputsInput", "Cell", "CollectOptions", "DbTableFact", "DiagramEdge",
    "DiagramEdgeKind", "DiagramKind", "DiagramNode", "DiagramView", "EpisodeView",
    "GraphView", "InputFileView", "InputsView", "InputTableView", "MaterializeView",
    "NodeView", "Number", "ProgramView", "PublishView", "RevisionView", "RunRecord",
    "RunView", "SeedKind", "SessionView", "SourceRef", "StepView", "TableData",
    "TableRole", "TableStat", "Usage", "WriterView", "MAX_RANK",
    "build_diagram", "build_graph", "build_inputs", "collect_book", "escape_for_script",
    "one_line", "parse_transcript", "read_db_facts", "read_table_stat", "render_book",
    "run_cli", "sample_table", "buildDiagram", "buildGraph", "buildInputs", "collectBook",
    "escapeForScript", "oneLine", "parseTranscript", "renderBook",
]
