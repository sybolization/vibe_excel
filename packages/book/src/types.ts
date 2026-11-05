/**
 * book 的视图模型。
 *
 * 三层分工：collect 从库与运行目录里读出这个结构，render 把它塞进一次性 HTML，
 * `app/app.js` 只认这个结构、不认识 control.db 也不认识 run.json。
 * 所以这里每个字段都必须是「界面上会出现的东西」，不做通用序列化。
 */

/** 表在流程里的位置：输入 / 中间 / 产出。 */
export type TableRole = "input" | "middle" | "output";

/** 一个步骤：代码里那段被 `"""…"""` 标注过的语句。 */
export interface StepView {
  n: number;
  line: number;
  kind: string;
  label: string;
}

/** 一次 `main` 调用 = 一个节点（ADR-0008）。 */
export interface NodeView {
  id: string;
  fn: string;
  file: string;
  title: string;
  reads: string[];
  writes: string[];
  elapsedMs: number | null;
  changed: string[];
  steps: StepView[];
}

/** 物化条目：哪个文件、哪张 sheet，变成哪张表。 */
export interface MaterializeView {
  file: string;
  sheet: string;
  table: string;
  rows: number | null;
}

export interface TableStat {
  rows: number | null;
  columns: number | null;
}

/** 产出表的取样：列名 + 前若干行。 */
export interface TableData {
  name: string;
  /** 取样带的列（宽表只带开头几列）。 */
  columns: string[];
  /** 这张表总共几列。 */
  columnTotal: number;
  rows: (string | number | null)[][];
  total: number;
  note: string;
}

export interface RunView {
  id: string;
  short: string;
  kind: string;
  status: string;
  identical: boolean | null;
  startedAt: string | null;
  finishedAt: string | null;
  elapsedMs: number | null;
  codeCommit: string | null;
  revision: string | null;
  hasTranscript: boolean;
}

/** 被选中的那次运行的完整流程。 */
export interface GraphView {
  runId: string;
  runShort: string;
  flow: string;
  entry: string;
  asOf: string | null;
  codeCommit: string | null;
  revision: string | null;
  identical: boolean | null;
  elapsedMs: number | null;
  materialize: MaterializeView[];
  nodes: NodeView[];
  outputs: string[];
  tables: Record<string, TableStat>;
  sample: TableData | null;
  warnings: string[];
}

/** 一段对话 = 一次 agent 调用产出的一个 jsonl。 */
export interface EpisodeView {
  file: string;
  id: string;
  start: string;
  end: string;
  user: string;
  assistant: string;
  toolCalls: number;
  writes: number;
  records: number;
  runId: string | null;
}

/**
 * 表是怎么进来的。
 *   materialize —— flow.json 声明、运行里真跑了，文件和 sheet 都记在 run.json；
 *   import      —— 落库时把来源写进了表内的 _source_file / _source_sheet 列；
 *   derive      —— 流程节点用别的表算出来的，靠 run.json 的 reads/writes 顺着追；
 *   unknown     —— 库里有，但上面三条都追不到。
 */
export type SeedKind = "materialize" | "import" | "derive" | "unknown";

/** 派生表的写出者：哪个运行里的哪个节点。 */
export interface WriterView {
  fn: string;
  file: string;
  elapsedMs: number | null;
  runShort: string;
}

/** 输入视图里的一行：一张表。 */
export interface InputTableView {
  name: string;
  rows: number | null;
  columns: number | null;
  kind: SeedKind;
  /** 派生表是哪个节点写出来的；物化 / 导入为 null。 */
  writer: WriterView | null;
  /** 物化 / 导入落在哪个 sheet，派生表为 null。 */
  sheet: string | null;
  /** 这张表在库里有几个来源 sheet，>1 时界面要标出来。 */
  sheetCount: number;
  /** 派生表的直接输入。 */
  from: string[];
  /** 出处：运行短 id / 空 / 节点函数名。 */
  via: string;
  /** 0 = 直接来自 sheet，1 = 由直接表算出，依此类推。 */
  depth: number;
  /** 当前流程读它、写它、还是两头都占。 */
  usage: "read" | "write" | "both" | null;
  sample: TableData | null;
}

/** 一个原始输入文件，以及它变成的表。 */
export interface InputFileView {
  name: string;
  bytes: number | null;
  sha256: string | null;
  /** 直接物化或导入的表。 */
  direct: InputTableView[];
  /** 由这些表继续算出来的表。 */
  derived: InputTableView[];
  /** 这个文件是否在本次运行的输入批次里。 */
  inBatch: boolean;
}

/** 输入视图：原始文件 → 表。项目级，不分运行。 */
export interface InputsView {
  batchId: string | null;
  capturedAt: string | null;
  asOf: string | null;
  /** 这些表是从哪个库读出来的，给出处用。 */
  dbPath: string | null;
  dbTables: number;
  files: InputFileView[];
  /** 库里有、却追不到文件来源的表。 */
  orphans: InputTableView[];
  readTables: number;
  writeTables: number;
}

export interface RevisionView {
  id: string;
  short: string;
  at: string;
  bytes: number | null;
}

export interface PublishView {
  short: string;
  at: string;
  note: string | null;
}

/** 画布上的节点：文件、sheet、表、计算节点，四类。 */
export type DiagramKind = "file" | "sheet" | "table" | "compute";

/** 画布上的连线：只标"谁生谁"，具体画法交给前端。 */
export type DiagramEdgeKind = "contain" | "make" | "read" | "write" | "loose";

export interface DiagramNode {
  id: string;
  kind: DiagramKind;
  /** 表格/文件/sheet 名，计算节点为函数名——前端拿它查详情。 */
  ref: string;
  label: string;
  /** 第几列，从 0 开始，按依赖深度算。 */
  rank: number;
  /** 属于本次运行：整条路亮起来。 */
  run: boolean;
  /** 追不到来路：孤表，或写出者不在任何 run.json 里。 */
  loose: boolean;
  rows: number | null;
  columns: number | null;
  bytes: number | null;
  sheetCount: number | null;
  tableCount: number | null;
  /** 计算节点：代码文件、耗时、出处运行。 */
  file: string | null;
  elapsedMs: number | null;
  runShort: string | null;
}

export interface DiagramEdge {
  from: string;
  to: string;
  kind: DiagramEdgeKind;
  run: boolean;
}

/** 画布：一张图里既有来路也有流程，前端负责排版与缩放。 */
export interface DiagramView {
  nodes: DiagramNode[];
  edges: DiagramEdge[];
  rankCount: number;
  fileCount: number;
  tableCount: number;
}

export interface BookView {
  generatedAt: string;
  store: string;
  program: { id: string; name: string } | null;
  session: { id: string; name: string; draftBranch: string; status: string } | null;
  revisions: RevisionView[];
  /** 工作集：该程序最新的数据版本。 */
  currentRevision: string | null;
  /** 选中运行实际用的数据版本。 */
  lastRunRevision: string | null;
  runs: RunView[];
  publishes: PublishView[];
  graph: GraphView | null;
  inputs: InputsView | null;
  diagram: DiagramView | null;
  transcript: EpisodeView[];
  focusEpisode: string | null;
}
