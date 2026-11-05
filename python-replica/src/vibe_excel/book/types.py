"""The original book application's camelCase JSON view models."""

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
    n: int
    line: Number
    kind: str
    label: str


class NodeView(TypedDict):
    id: str
    fn: str
    file: str
    title: str
    reads: list[str]
    writes: list[str]
    elapsedMs: Number | None
    changed: list[str]
    steps: list[StepView]


class MaterializeView(TypedDict):
    file: str
    sheet: str
    table: str
    rows: Number | None


class TableStat(TypedDict):
    rows: Number | None
    columns: int | None


class TableData(TypedDict):
    name: str
    columns: list[str]
    columnTotal: int
    rows: list[list[Cell]]
    total: Number
    note: str


class RunView(TypedDict):
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


class WriterView(TypedDict):
    fn: str
    file: str
    elapsedMs: Number | None
    runShort: str


InputTableView = TypedDict(
    "InputTableView",
    {
        "name": str,
        "rows": Number | None,
        "columns": int | None,
        "kind": SeedKind,
        "writer": WriterView | None,
        "sheet": str | None,
        "sheetCount": int,
        "from": list[str],
        "via": str,
        "depth": int,
        "usage": Usage,
        "sample": TableData | None,
    },
)


class InputFileView(TypedDict):
    name: str
    bytes: Number | None
    sha256: str | None
    direct: list[InputTableView]
    derived: list[InputTableView]
    inBatch: bool


class InputsView(TypedDict):
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
    id: str
    short: str
    at: str
    bytes: Number | None


class PublishView(TypedDict):
    short: str
    at: str
    note: str | None


class DiagramNode(TypedDict):
    id: str
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


DiagramEdge = TypedDict(
    "DiagramEdge",
    {"from": str, "to": str, "kind": DiagramEdgeKind, "run": bool},
)


class DiagramView(TypedDict):
    nodes: list[DiagramNode]
    edges: list[DiagramEdge]
    rankCount: int
    fileCount: int
    tableCount: int


class ProgramView(TypedDict):
    id: str
    name: str


class SessionView(TypedDict):
    id: str
    name: str
    draftBranch: str
    status: str


class BookView(TypedDict):
    generatedAt: str
    store: str
    program: ProgramView | None
    session: SessionView | None
    revisions: list[RevisionView]
    currentRevision: str | None
    lastRunRevision: str | None
    runs: list[RunView]
    publishes: list[PublishView]
    graph: GraphView | None
    inputs: InputsView | None
    diagram: DiagramView | None
    transcript: list[EpisodeView]
    focusEpisode: str | None
    warnings: NotRequired[list[str]]


class SourceRef(TypedDict):
    file: str
    sheet: str
    rows: Number | None


class DbTableFact(TypedDict):
    name: str
    rows: Number | None
    columns: int | None
    sources: list[SourceRef]


class RunRecord(TypedDict):
    id: str
    startedAt: str | None
    doc: object


class BatchFile(TypedDict):
    name: str
    bytes: Number | None
    sha256: str | None


class BatchView(TypedDict):
    batchId: str
    asOf: str | None
    capturedAt: str | None
    files: list[BatchFile]


class CollectOptions(TypedDict):
    store: str
    sessionId: NotRequired[str]
    runId: NotRequired[str]


class BuildGraphInput(TypedDict):
    runId: str
    tables: dict[str, TableStat]
    sample: TableData | None
    runJson: object


class BuildInputsInput(TypedDict):
    batch: BatchView | None
    dbPath: str | None
    tables: list[DbTableFact]
    runs: list[RunRecord]
    reads: list[str]
    writes: list[str]
    sample: NotRequired[Callable[[str], TableData | None]]


class BuildDiagramInput(TypedDict):
    inputs: InputsView | None
    graph: GraphView | None
