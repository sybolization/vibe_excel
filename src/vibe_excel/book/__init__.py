"""只读收集并独立可视化 vibe-excel store。

数据来源是 store 目录里的 control.db、run 记录与 JSONL 对话；本包只读取，
绝不修改数据库或写回 store。对外以 snake_case 为主，同时保留原前端使用的
camelCase 别名，便于与前端代码逐字对照。
"""

from .collect import collect_book, collectBook, read_db_facts, read_table_stat, sample_table
from .diagram import MAX_RANK, build_diagram, buildDiagram
from .graph import build_graph, buildGraph
from .index_render import render_index, renderIndex
from .inputs import build_inputs, buildInputs
from .projects import collect_projects, collectProjects, page_name, pageName
from .render import escape_for_script, escapeForScript, render_book, renderBook
from .transcript import (
    conversation_title,
    conversationTitle,
    one_line,
    oneLine,
    parse_conversation,
    parse_transcript,
    parseConversation,
    parseTranscript,
)
from .types import (
    BatchFile,
    BatchView,
    BookView,
    BuildDiagramInput,
    BuildGraphInput,
    BuildInputsInput,
    Cell,
    ChatBlock,
    CollectOptions,
    ConversationView,
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
    """把命令行参数转交给 CLI 模块。

    延迟导入让仅使用数据模型或渲染函数的调用方不必加载 CLI 依赖。
    """
    from .cli import run_cli as run

    return run(argv)


__all__ = [
    "BatchFile", "BatchView", "BookView", "BuildDiagramInput", "BuildGraphInput",
    "BuildInputsInput", "Cell", "CollectOptions", "DbTableFact", "DiagramEdge",
    "ChatBlock", "ConversationView", "DiagramEdgeKind", "DiagramKind", "DiagramNode", "DiagramView", "EpisodeView",
    "GraphView", "InputFileView", "InputsView", "InputTableView", "MaterializeView",
    "NodeView", "Number", "ProgramView", "ProjectSummary", "RunBrief", "SessionSummary", "PublishView", "RevisionView", "RunRecord",
    "RunView", "SeedKind", "SessionView", "SourceRef", "StepView", "TableData",
    "TableRole", "TableStat", "Usage", "WriterView", "MAX_RANK",
    "build_diagram", "build_graph", "build_inputs", "collect_book", "collect_projects", "escape_for_script", "page_name", "render_index",
    "conversation_title", "one_line", "parse_conversation", "parse_transcript", "read_db_facts", "read_table_stat", "render_book",
    "run_cli", "sample_table", "buildDiagram", "buildGraph", "buildInputs", "collectBook", "collectProjects", "pageName", "renderIndex",
    "escapeForScript", "conversationTitle", "oneLine", "parseConversation", "parseTranscript", "renderBook",
]
