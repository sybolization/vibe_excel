/**
 * 输入视图：把「原始文件 → 表」这层关系拼出来。
 *
 * 没人把这张关系图完整记在一处，只能用三种记录去还原，按可信度排：
 *
 *   1. `run.json` 的 `rounds.*.materialize.entries`——真跑过的物化，文件和 sheet 都是
 *      当时记下来的，还带行数摘要。它只覆盖 flow.json 声明过的那几条。
 *   2. 表自己的 `_source_file` / `_source_sheet` 列——导入脚本落库时逐行写进去的，
 *      表里有什么它就说有什么，不依赖外部记录。
 *   3. `run.json` 的 `graph.nodes[].reads / writes`——谁写了哪张表、读了哪些表。
 *      派生表沿 reads 一路往上追，总能追到某个 sheet；追不到的不猜。
 *
 * 同一张表可能有多条记录（历史上有几代代码都写过它）。取最新那次运行说了算：
 * 库是累加的，后写的盖先写的，界面也该按最后一次的口径显示。
 */

import type {
  InputFileView,
  InputTableView,
  InputsView,
  SeedKind,
  TableData,
} from "./types.ts";

type Json = Record<string, unknown>;

const asObject = (value: unknown): Json =>
  value !== null && typeof value === "object" ? (value as Json) : {};
const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const asString = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);
const asNumber = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
const stringList = (value: unknown): string[] =>
  asArray(value).filter((item): item is string => typeof item === "string" && item !== "");

/** 表里记着的一个来源 sheet。 */
export interface SourceRef {
  file: string;
  sheet: string;
  rows: number | null;
}

/** 库里一张表的静态事实，由 collect 读出来。 */
export interface DbTableFact {
  name: string;
  rows: number | null;
  columns: number | null;
  /** `_source_file` / `_source_sheet` 的分组结果，按行数降序。 */
  sources: SourceRef[];
}

/** 一次运行——只要它记过物化或图。 */
export interface RunRecord {
  id: string;
  startedAt: string | null;
  doc: unknown;
}

/** 输入批次清单（work/inputs/<batchId>/manifest.json）。 */
export interface BatchView {
  batchId: string;
  asOf: string | null;
  capturedAt: string | null;
  files: { name: string; bytes: number | null; sha256: string | null }[];
}

export interface BuildInputsInput {
  batch: BatchView | null;
  dbPath: string | null;
  tables: DbTableFact[];
  runs: RunRecord[];
  /** 当前流程读了哪些表。 */
  reads: string[];
  /** 当前流程写了哪些表。 */
  writes: string[];
  /** 取一张表的前几行；没有库传 undefined。 */
  sample?: (name: string) => TableData | null;
}

interface MaterializeEdge {
  file: string;
  sheet: string;
  rows: number | null;
  runId: string;
}

interface DeriveEdge {
  from: string[];
  via: string;
  /** 代码里 `# name：中文`，空串表示没有声明。 */
  name: string;
  /** 节点所在的代码文件、耗时、出处运行——画布上要画成一个真的节点。 */
  file: string;
  elapsedMs: number | null;
  runShort: string;
}

/** 新到旧：库是累加的，同一张表以最后一次写它为准。 */
function newestFirst(runs: RunRecord[]): RunRecord[] {
  return [...runs].sort((a, b) => (b.startedAt ?? "").localeCompare(a.startedAt ?? ""));
}

/** 每个节点跑了多久记在 rounds 里，按节点 id 索引。 */
function elapsedByNode(doc: Json): Map<string, number> {
  const elapsed = new Map<string, number>();
  const rounds = asObject(doc["rounds"]);
  for (const key of ["a", "b"]) {
    for (const raw of asArray(asObject(rounds[key])["nodes"])) {
      const item = asObject(raw);
      const id = asString(item["id"]);
      const ms = asNumber(item["elapsedMs"]);
      // 两轮里同一个节点跑两次，取第一轮（a）的口径，和 buildGraph 保持一致
      if (id !== null && ms !== null && !elapsed.has(id)) elapsed.set(id, ms);
    }
  }
  return elapsed;
}

function materializeEdges(runs: RunRecord[]): { edges: Map<string, MaterializeEdge>; sheetRank: Map<string, number> } {
  const edges = new Map<string, MaterializeEdge>();
  const sheetRank = new Map<string, number>();
  for (const run of newestFirst(runs)) {
    const rounds = asObject(asObject(run.doc)["rounds"]);
    for (const key of ["a", "b"]) {
      const materialize = asObject(asObject(rounds[key])["materialize"]);
      for (const raw of asArray(materialize["entries"])) {
        const item = asObject(raw);
        const entry = asObject(item["entry"]);
        const table = asString(entry["table"]);
        const file = asString(entry["file"]);
        if (table === null || file === null) continue;
        const sheet = asString(entry["sheet"]) ?? "—";
        // sheet 的先后按 flow.json 里声明的顺序记，最早见到的那次说了算
        const named = `${file}\u0000${sheet}`;
        if (!sheetRank.has(named)) sheetRank.set(named, sheetRank.size);
        if (edges.has(table)) continue;
        edges.set(table, {
          file,
          sheet,
          rows: asNumber(asObject(item["digest"])["rows"]),
          runId: run.id,
        });
      }
    }
  }
  return { edges, sheetRank };
}

function deriveEdges(runs: RunRecord[]): Map<string, DeriveEdge> {
  const edges = new Map<string, DeriveEdge>();
  for (const run of newestFirst(runs)) {
    const doc = asObject(run.doc);
    const elapsed = elapsedByNode(doc);
    for (const raw of asArray(asObject(doc["graph"])["nodes"])) {
      const node = asObject(raw);
      const call = asString(node["call"]) ?? asString(node["id"]) ?? "—";
      const cut = call.lastIndexOf(":");
      const via = cut === -1 ? call : call.slice(cut + 1);
      const nodeName = asString(node["name"]) ?? "";
      const file = cut === -1 ? "" : call.slice(0, cut);
      const nodeId = asString(node["id"]);
      const elapsedMs = asNumber(node["elapsedMs"]) ?? (nodeId === null ? null : elapsed.get(nodeId) ?? null);
      const from = stringList(node["reads"]);
      for (const table of stringList(node["writes"])) {
        if (edges.has(table)) continue;
        edges.set(table, { from, via, name: nodeName, file, elapsedMs, runShort: run.id.slice(0, 8) });
      }
    }
  }
  return edges;
}

export function buildInputs(input: BuildInputsInput): InputsView {
  const { edges: materialize, sheetRank } = materializeEdges(input.runs);
  const derive = deriveEdges(input.runs);
  const reads = new Set(input.reads);
  const writes = new Set(input.writes);
  const usageOf = (name: string): InputTableView["usage"] =>
    reads.has(name) && writes.has(name)
      ? "both"
      : reads.has(name)
        ? "read"
        : writes.has(name) ? "write" : null;

  const byName = new Map(input.tables.map((table) => [table.name, table]));
  const makeRow = (name: string, kind: SeedKind): InputTableView => {
    const fact = byName.get(name);
    return {
      name,
      rows: fact?.rows ?? null,
      columns: fact?.columns ?? null,
      kind,
      writer: null,
      sheet: null,
      sheetCount: 0,
      from: [],
      via: "",
      depth: 0,
      usage: usageOf(name),
      sample: input.sample === undefined ? null : input.sample(name),
    };
  };

  const rows = new Map<string, InputTableView>();
  /** 每张表落在哪个文件上——派生表靠它一路往上追。 */
  const fileOf = new Map<string, string>();
  const depthOf = new Map<string, number>();

  // 1) 直接来源：先看物化记录，再看表内自己的来源列
  for (const table of input.tables) {
    const edge = materialize.get(table.name);
    if (edge !== undefined) {
      const row = makeRow(table.name, "materialize");
      row.sheet = edge.sheet;
      row.sheetCount = 1;
      row.via = edge.runId.slice(0, 8);
      row.rows = row.rows ?? edge.rows;
      rows.set(table.name, row);
      fileOf.set(table.name, edge.file);
      depthOf.set(table.name, 0);
      continue;
    }
    const source = table.sources[0];
    if (source === undefined) continue;
    const row = makeRow(table.name, "import");
    row.sheet = source.sheet;
    row.sheetCount = table.sources.length;
    row.rows = row.rows ?? source.rows;
    rows.set(table.name, row);
    fileOf.set(table.name, source.file);
    depthOf.set(table.name, 0);
  }

  // 2) 派生：输入都追到文件了，自己才算数；一轮轮往下推，推不动就是追不到
  const pending = input.tables.filter((table) => !rows.has(table.name)).map((table) => table.name);
  for (let pass = 0; pass < 32 && pending.length > 0; pass += 1) {
    let progressed = false;
    for (let i = pending.length - 1; i >= 0; i -= 1) {
      const name = pending[i];
      if (name === undefined) continue;
      const edge = derive.get(name);
      if (edge === undefined) continue;
      const known = edge.from.filter((upstream) => fileOf.has(upstream));
      const first = known[0];
      const owner = first === undefined ? null : fileOf.get(first) ?? null;
      if (owner === null) continue;
      const row = makeRow(name, "derive");
      row.from = edge.from;
      row.via = edge.via;
      row.writer = { fn: edge.via, name: edge.name, file: edge.file, elapsedMs: edge.elapsedMs, runShort: edge.runShort };
      row.depth = Math.max(...known.map((upstream) => depthOf.get(upstream) ?? 0)) + 1;
      rows.set(name, row);
      fileOf.set(name, owner);
      depthOf.set(name, row.depth);
      pending.splice(i, 1);
      progressed = true;
    }
    if (!progressed) break;
  }

  // 3) 落不进任何文件的，单独列出来，不硬挂
  const orphans: InputTableView[] = pending.map((name) => makeRow(name, "unknown"));

  // 文件顺序按批次清单；记录里出现、清单里没有的补在最后（换了批次的老表）
  const order: string[] = (input.batch?.files ?? []).map((file) => file.name);
  for (const file of new Set(fileOf.values())) {
    if (!order.includes(file)) order.push(file);
  }
  const manifest = new Map((input.batch?.files ?? []).map((file) => [file.name, file]));
  const rank = (row: InputTableView): number => (row.kind === "import" ? 1 : 0);
  const sheetOrder = (file: string, row: InputTableView): number =>
    row.sheet === null ? Number.MAX_SAFE_INTEGER : sheetRank.get(`${file}\u0000${row.sheet}`) ?? Number.MAX_SAFE_INTEGER;

  const all = [...rows.values()];
  const files: InputFileView[] = order.map((name) => {
    const meta = manifest.get(name);
    const own = all.filter((row) => fileOf.get(row.name) === name);
    return {
      name,
      bytes: meta?.bytes ?? null,
      sha256: meta?.sha256 ?? null,
      direct: own
        .filter((row) => row.kind !== "derive")
        .sort((a, b) => sheetOrder(name, a) - sheetOrder(name, b) || rank(a) - rank(b)),
      derived: own
        .filter((row) => row.kind === "derive")
        .sort((a, b) => a.depth - b.depth || a.name.localeCompare(b.name)),
      inBatch: meta !== undefined,
    };
  });

  const existing = (name: string): boolean => byName.has(name);
  return {
    batchId: input.batch?.batchId ?? null,
    capturedAt: input.batch?.capturedAt ?? null,
    asOf: input.batch?.asOf ?? null,
    dbPath: input.dbPath,
    dbTables: input.tables.length,
    files,
    orphans,
    readTables: [...reads].filter(existing).length,
    writeTables: [...writes].filter(existing).length,
  };
}
