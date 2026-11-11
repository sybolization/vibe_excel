"""原始 book 应用的 camelCase JSON 视图模型。

这些 TypedDict 直接对应前端读取的 JSON 字段，是 control.db（会话/运行/版本）、
run.json（graph 与执行摘要）以及派生 inputs 视图之间的唯一契约；字段只读、
不做运行时校验，越界值由采集层负责丢弃或置空。
"""

from collections.abc import Callable
from typing import Literal, NotRequired, TypedDict

Number = int | float
Cell = str | Number | None
TableRole = Literal["input", "middle", "output"]
SeedKind = Literal["materialize", "import", "derive", "unknown"]
Usage = Literal["read", "write", "both"] | None
DiagramKind = Literal["file", "sheet", "table", "compute"]
DiagramEdgeKind = Literal["contain", "make", "read", "write", "loose"]


class StepView(TypedDict):
    """run.json 里的一个执行步骤；n 是按展示顺序重新编号的 1 起序号。"""

    n: int
    line: Number
    kind: str
    label: str


class NodeView(TypedDict):
    """运行图里的一个函数节点。

    reads/writes 是有序表名列表（不是集合），elapsedMs 与 changed 来自执行轮次记录，可能为空。
    """

    id: str
    name: str
    fn: str
    file: str
    title: str
    reads: list[str]
    writes: list[str]
    elapsedMs: Number | None
    changed: list[str]
    steps: list[StepView]


class MaterializeView(TypedDict):
    """一条物化记录：由哪个文件与工作表生成哪张表，rows 可为空。"""

    file: str
    sheet: str
    table: str
    rows: Number | None


class TableStat(TypedDict):
    """表的规模统计；拿不到数据库行列数时为 None。"""

    rows: Number | None
    columns: int | None


class TableData(TypedDict):
    """前端表格预览：最多 rows 行、columns 列，columnTotal 是完整列数。"""

    name: str
    columns: list[str]
    columnTotal: int
    rows: list[list[Cell]]
    total: Number
    note: str


class RunView(TypedDict):
    """运行列表的一行。

    identical 为 None 表示 run.json 与 runs.note 都没有给出该标记，而不是“不相同”。
    """

    id: str
    short: str
    kind: str
    status: str
    identical: bool | None
    startedAt: str | None
    finishedAt: str | None
    elapsedMs: Number | None
    codeCommit: str | None
    revision: str | None
    hasTranscript: bool


class GraphView(TypedDict):
    """流程页的完整视图模型，由权威 run.json 派生。

    除了静态节点与边，还携带选中运行的耗时、物化记录与首张输出表的样例。
    """

    runId: str
    runShort: str
    flow: str
    entry: str
    asOf: str | None
    codeCommit: str | None
    revision: str | None
    identical: bool | None
    elapsedMs: Number | None
    materialize: list[MaterializeView]
    nodes: list[NodeView]
    outputs: list[str]
    tables: dict[str, TableStat]
    sample: TableData | None
    warnings: list[str]


class EpisodeView(TypedDict):
    """一段 JSONL 对话的摘要。

    start/end 是首尾时间戳，runId 关联到对应运行，找不到时为 None。
    """

    file: str
    id: str
    start: str
    end: str
    user: str
    assistant: str
    toolCalls: int
    writes: int
    records: int
    runId: str | None


class ChatBlock(TypedDict):
    """对话里的一块：人说的一段、agent 说的一段、agent 的思考、或者一次工具执行。"""

    kind: Literal["user", "assistant", "tool", "thinking"]
    # 这一块发生的时间（ISO）。工具块沿用发起它的那条消息的时间。
    at: str
    # 人说 / agent 说的正文，Markdown；工具块为空串。
    text: str
    # 工具名与一行简述；非工具块为空串。
    name: str
    brief: str
    # 工具是否成功；没有结果记录（会话被中断）时按成功算。
    ok: bool
    ms: Number | None
    # 工具输出，已截断。
    result: str


class ConversationView(TypedDict):
    """一场对话：标题取自首条用户消息，正文是逐条消息与工具执行。"""

    id: str
    title: str
    start: str
    end: str
    blocks: list[ChatBlock]


class WriterView(TypedDict):
    """派生表的写出者：函数、文件、耗时与运行短 id。"""

    fn: str
    name: str
    file: str
    elapsedMs: Number | None
    runShort: str


# inputs 页的一张表：kind 是来源类型（物化/导入/派生/未知），from 是上游表名，
# depth 是派生深度，usage 标明当前流程读、写还是两者都涉及。
InputTableView = TypedDict(
    "InputTableView",
    {
        "name": str,
        "rows": Number | None,
        "columns": int | None,
        "kind": SeedKind,
        "writer": WriterView | None,
        "sheet": str | None,
        # 该表来自几张工作表；导入表可能跨多个 sheet，物化/派生表恒为 1 或 0。
        "sheetCount": int,
        "from": list[str],
        # via 是最近一次生成该表的运行短 id 或函数名。
        "via": str,
        "depth": int,
        "usage": Usage,
        "sample": TableData | None,
    },
)


class InputFileView(TypedDict):
    """inputs 页的一个来源文件。

    direct 是直接由文件产生的表，derived 是由上游表派生的表；
    inBatch 标明该文件是否出现在批次清单里（否则只有数据库里的来源列）。
    """

    name: str
    bytes: Number | None
    sha256: str | None
    direct: list[InputTableView]
    derived: list[InputTableView]
    inBatch: bool


class InputsView(TypedDict):
    """inputs 页视图模型：批次元数据、库路径、文件树与孤儿表。"""

    batchId: str | None
    capturedAt: str | None
    asOf: str | None
    dbPath: str | None
    dbTables: int
    files: list[InputFileView]
    orphans: list[InputTableView]
    readTables: int
    writeTables: int
    warnings: NotRequired[list[str]]


class RevisionView(TypedDict):
    """一个数据版本（revision）的摘要；short 是 id 前 8 位。"""

    id: str
    short: str
    at: str
    bytes: Number | None


class PublishView(TypedDict):
    """一次发布的 commit 短号、时间与可选备注。"""

    short: str
    at: str
    note: str | None


class DiagramNode(TypedDict):
    """diagram 视图里的一个几何节点。

    kind 决定所在层级（file=0、sheet=1、table=2、compute=3）；
    sheetCount/tableCount 分别统计所属工作表与表节点数量。
    """

    id: str
    name: str
    kind: DiagramKind
    ref: str
    label: str
    rank: int
    run: bool
    loose: bool
    rows: Number | None
    columns: int | None
    bytes: Number | None
    sheetCount: int | None
    tableCount: int | None
    file: str | None
    elapsedMs: Number | None
    runShort: str | None


# diagram 里的一条有向边；kind 对应 contain/make/read/write/loose 五种语义。
DiagramEdge = TypedDict(
    "DiagramEdge",
    {"from": str, "to": str, "kind": DiagramEdgeKind, "run": bool},
)


class DiagramView(TypedDict):
    """diagram 页的节点、边与全局计数。"""

    nodes: list[DiagramNode]
    edges: list[DiagramEdge]
    rankCount: int
    fileCount: int
    tableCount: int


class SessionSummary(TypedDict):
    """项目下的一个会话，只放首页要显示的字段。"""

    id: str
    name: str
    # active / archived
    status: str
    draftBranch: str
    updatedAt: str


class RunBrief(TypedDict):
    """最近一次运行的摘要；首页不展开的话就看这三个字段。"""

    id: str
    status: str
    at: str | None
    # 两次跑的结果是否一致；note 里没有标记时是 None（未知，不等于不一致）。
    identical: bool | None


class ProjectSummary(TypedDict):
    """首页的一行：一个项目，加上它最近的状态。"""

    id: str
    name: str
    createdAt: str
    # 站点合并多个 store 时才有值，用来在列表里区分来源
    store: NotRequired[str]
    sessions: list[SessionSummary]
    # 默认进入哪个会话：活动的、且 updated_at 最新的那个；没有会话时为 None。
    activeSessionId: str | None
    runCount: int
    publishCount: int
    lastRun: RunBrief | None


class ProgramView(TypedDict):
    """control.db programs 表里的一行，用于页面标题。"""

    id: str
    name: str


class SessionView(TypedDict):
    """control.db sessions 表里的一行；draftBranch 是草稿分支名。"""

    id: str
    name: str
    draftBranch: str
    status: str


class BookView(TypedDict):
    """整本 book 的根视图模型，由 collect 一次性产出。

    currentRevision 是当前数据版本（revisions[0]），lastRunRevision 是选中
    运行实际使用的输入版本，两者可能不同；focusEpisode 指向默认展开的对话。
    """

    generatedAt: str
    store: str
    program: ProgramView | None
    session: SessionView | None
    revisions: list[RevisionView]
    currentRevision: str | None
    # 选中运行使用的输入数据版本；与当前最新版本未必一致。
    lastRunRevision: str | None
    runs: list[RunView]
    publishes: list[PublishView]
    graph: GraphView | None
    inputs: InputsView | None
    diagram: DiagramView | None
    transcript: list[EpisodeView]
    conversations: list[ConversationView]
    # 默认展开的对话 id；关联不到作者记录时取最近一条。
    focusEpisode: str | None
    warnings: NotRequired[list[str]]


class SourceRef(TypedDict):
    """表内来源列（_source_file/_source_sheet）聚合出的一处出处。"""

    file: str
    sheet: str
    rows: Number | None


class DbTableFact(TypedDict):
    """直接从 SQLite 读出的一张表的静态事实，sources 可为空。"""

    name: str
    rows: Number | None
    columns: int | None
    sources: list[SourceRef]


class RunRecord(TypedDict):
    """原始 run.json 文档及其运行 id，供 graph 与 inputs 复用。"""

    id: str
    startedAt: str | None
    doc: object


class BatchFile(TypedDict):
    """输入批次清单里的一个文件；sha256 用于核对内容未变。"""

    name: str
    bytes: Number | None
    sha256: str | None


class BatchView(TypedDict):
    """一次输入批次的元数据与文件清单。"""

    batchId: str
    asOf: str | None
    capturedAt: str | None
    files: list[BatchFile]


class CollectOptions(TypedDict):
    """兼容原前端调用的 collect 入参映射；store 必填，会话与运行可选。"""

    store: str
    programId: NotRequired[str]
    sessionId: NotRequired[str]
    runId: NotRequired[str]


class BuildGraphInput(TypedDict):
    """兼容 camelCase 调用的 build_graph 入参映射。"""

    runId: str
    tables: dict[str, TableStat]
    sample: TableData | None
    runJson: object


class BuildInputsInput(TypedDict):
    """兼容 camelCase 调用的 build_inputs 入参映射。"""

    batch: BatchView | None
    dbPath: str | None
    tables: list[DbTableFact]
    runs: list[RunRecord]
    reads: list[str]
    writes: list[str]
    sample: NotRequired[Callable[[str], TableData | None]]


class BuildDiagramInput(TypedDict):
    """兼容 camelCase 调用的 build_diagram 入参映射。"""

    inputs: InputsView | None
    graph: GraphView | None
