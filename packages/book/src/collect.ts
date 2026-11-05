/**
 * 从 store 里读出整本账。
 *
 * 只读：control.db 用 `readOnly` 打开，运行目录里的 hr.db 也只读数一下行数与前几十行。
 * 读不到的（文件被清掉、表改名）一律降级成 null，不抛错——书是给人看的，
 * 缺一页也要能翻。
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { basename, join } from "node:path";
import { DatabaseSync } from "node:sqlite";

import { buildGraph } from "./graph.ts";
import { buildDiagram } from "./diagram.ts";
import { buildInputs } from "./inputs.ts";
import type { BatchView, DbTableFact, RunRecord, SourceRef } from "./inputs.ts";
import { parseTranscript } from "./transcript.ts";
import type {
  BookView,
  EpisodeView,
  GraphView,
  InputsView,
  PublishView,
  RevisionView,
  RunView,
  TableData,
  TableStat,
} from "./types.ts";

type Json = Record<string, unknown>;

const asObject = (value: unknown): Json =>
  value !== null && typeof value === "object" ? (value as Json) : {};
const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const asString = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);
const asNumber = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

export interface CollectOptions {
  /** store 根目录，里面有 control.db 与 sessions/。 */
  store: string;
  /** 不传就取 updated_at 最新的那个会话。 */
  sessionId?: string;
  /** 不传就取最新一次带 run.json 的 flow 运行。 */
  runId?: string;
}

/** 只读打开，别名留给调用方关。 */
function openReadOnly(path: string): DatabaseSync {
  return new DatabaseSync(path, { readOnly: true });
}

function all<T>(db: DatabaseSync, sql: string, ...params: (string | number)[]): T[] {
  return db.prepare(sql).all(...params) as unknown as T[];
}

function quoteIdent(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

/** 一次运行的一行（control.db.runs）。 */
interface RunRow {
  id: string;
  session_id: string;
  program_id: string;
  code_commit: string | null;
  data_revision_id: string | null;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  exit_code: string | null;
  note: string | null;
}

interface RevisionRow {
  id: string;
  bytes: number | null;
  created_at: string | null;
  sha256: string | null;
  note: string | null;
  path: string | null;
}

interface PublishRow {
  id: string;
  commit_sha: string;
  at: string;
  note: string | null;
}

/** author.json 里我们只关心这三样：它落在哪个 commit、对应哪段对话、标签是什么。 */
interface AuthorRecord {
  commit: string | null;
  sessionFile: string | null;
  label: string | null;
}

function readAuthors(runRoot: string): AuthorRecord[] {
  if (!existsSync(runRoot)) return [];
  const records: AuthorRecord[] = [];
  for (const entry of readdirSync(runRoot, { withFileTypes: true })) {
    if (!entry.isDirectory() || !entry.name.startsWith("author-")) continue;
    const file = join(runRoot, entry.name, "author.json");
    if (!existsSync(file)) continue;
    let doc: unknown;
    try {
      doc = JSON.parse(readFileSync(file, "utf8"));
    } catch {
      continue;
    }
    const record = asObject(doc);
    const stats = asObject(record["stats"]);
    records.push({
      commit: asString(record["commit"]),
      sessionFile: asString(stats["sessionFile"]),
      label: asString(record["label"]),
    });
  }
  return records;
}

function readEpisodes(store: string, sessionId: string, authors: AuthorRecord[]): EpisodeView[] {
  const dir = join(store, "sessions", sessionId, "sessions");
  if (!existsSync(dir)) return [];
  const episodes: EpisodeView[] = [];
  for (const name of readdirSync(dir)) {
    if (!name.toLowerCase().endsWith(".jsonl")) continue;
    const file = join(dir, name);
    let text: string;
    try {
      text = readFileSync(file, "utf8");
    } catch {
      continue;
    }
    const episode = parseTranscript(file, text);
    if (episode !== null) episodes.push(episode);
  }
  episodes.sort((a, b) => a.start.localeCompare(b.start));
  return episodes;
}

function readTableStat(db: DatabaseSync, name: string): TableStat {
  try {
    const counted = all<{ n: number }>(db, `select count(*) as n from ${quoteIdent(name)}`);
    const rows = counted.length > 0 ? counted[0]?.n ?? null : null;
    const columns = all<{ name: string }>(db, `select name from pragma_table_info(?)`, name).length;
    return { rows, columns };
  } catch {
    return { rows: null, columns: null };
  }
}

function sampleTable(
  db: DatabaseSync,
  name: string,
  note: string,
  limit = 40,
  maxColumns = 0,
): TableData | null {
  try {
    const info = all<{ name: string }>(db, `select name from pragma_table_info(?)`, name);
    if (info.length === 0) return null;
    const columnTotal = info.length;
    const columns = (maxColumns > 0 ? info.slice(0, maxColumns) : info).map((column) => column.name);
    const rows = all<Json>(db, `select * from ${quoteIdent(name)} limit ${limit}`);
    const counted = all<{ n: number }>(db, `select count(*) as n from ${quoteIdent(name)}`);
    return {
      name,
      columns,
      columnTotal,
      rows: rows.map((row) => columns.map((column) => {
        const value = row[column];
        if (value === null || value === undefined) return null;
        if (typeof value === "number" || typeof value === "string") return value;
        return String(value);
      })),
      total: counted.length > 0 ? counted[0]?.n ?? rows.length : rows.length,
      note,
    };
  } catch {
    return null;
  }
}

/** 图里出现的所有表，按出现顺序去重。 */
function tableNamesOf(doc: Json): string[] {
  const names: string[] = [];
  const push = (name: unknown): void => {
    if (typeof name !== "string" || name === "") return;
    if (!names.includes(name)) names.push(name);
  };
  const graph = asObject(doc["graph"]);
  for (const raw of asArray(graph["nodes"])) {
    const node = asObject(raw);
    for (const name of asArray(node["reads"])) push(name);
    for (const name of asArray(node["writes"])) push(name);
  }
  const rounds = asObject(doc["rounds"]);
  const round = asObject(rounds["a"] ?? rounds["b"]);
  for (const name of Object.keys(asObject(round["outputs"]))) push(name);
  for (const raw of asArray(asObject(round["materialize"])["entries"])) {
    push(asObject(asObject(raw)["entry"])["table"]);
  }
  return names;
}

/** 一次运行的产出库：优先 a/ 轮，其次运行根目录。 */
function findRunDatabase(runDir: string): string | null {
  for (const candidate of [join(runDir, "a", "hr.db"), join(runDir, "hr.db")]) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

/** 每个有 run.json 的运行：物化记录和图都在里面，输入视图全靠它。 */
function readRunDocs(runRoot: string, runRows: RunRow[]): RunRecord[] {
  if (!existsSync(runRoot)) return [];
  const records: RunRecord[] = [];
  for (const run of runRows) {
    const file = join(runRoot, run.id, "run.json");
    if (!existsSync(file)) continue;
    try {
      records.push({ id: run.id, startedAt: run.started_at, doc: JSON.parse(readFileSync(file, "utf8")) });
    } catch {
      continue;
    }
  }
  return records;
}

/** work/inputs/<batchId>/manifest.json：这一批原始文件是按什么清单收进来的。 */
function readBatches(workDir: string): BatchView[] {
  const dir = join(workDir, "inputs");
  if (!existsSync(dir)) return [];
  const batches: BatchView[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const file = join(dir, entry.name, "manifest.json");
    if (!existsSync(file)) continue;
    try {
      const doc = asObject(JSON.parse(readFileSync(file, "utf8")));
      const files = asArray(doc["files"]).map((raw) => {
        const item = asObject(raw);
        return {
          name: asString(item["name"]) ?? "",
          bytes: asNumber(item["bytes"]),
          sha256: asString(item["sha256"]),
        };
      }).filter((item) => item.name !== "");
      batches.push({
        batchId: asString(doc["batchId"]) ?? entry.name,
        asOf: asString(doc["asOf"]),
        capturedAt: asString(doc["capturedAt"]),
        files,
      });
    } catch {
      continue;
    }
  }
  return batches;
}

/** 表里自己记着的来源列，一张表可能来自多个 sheet。 */
function readSources(db: DatabaseSync, name: string): SourceRef[] {
  try {
    const columns = all<{ name: string }>(db, "select name from pragma_table_info(?)", name)
      .map((column) => column.name);
    if (!columns.includes("_source_file") || !columns.includes("_source_sheet")) return [];
    const grouped = all<{ file: string | null; sheet: string | null; n: number }>(
      db,
      `select "_source_file" as file, "_source_sheet" as sheet, count(*) as n
         from ${quoteIdent(name)} group by 1, 2 order by 3 desc`,
    );
    const sources: SourceRef[] = [];
    for (const row of grouped) {
      if (row.file === null || row.file === "") continue;
      sources.push({ file: row.file, sheet: row.sheet ?? "—", rows: row.n });
    }
    return sources;
  } catch {
    return [];
  }
}

function readDbFacts(db: DatabaseSync): DbTableFact[] {
  const facts: DbTableFact[] = [];
  for (const row of all<{ name: string }>(db, "select name from sqlite_master where type = 'table'")) {
    if (row.name.startsWith("sqlite_")) continue;
    const stat = readTableStat(db, row.name);
    facts.push({ name: row.name, rows: stat.rows, columns: stat.columns, sources: readSources(db, row.name) });
  }
  return facts;
}

/** 项目的库：优先 work/ 下最大的那个 .db，退到最新数据版本快照。 */
function findWorkDatabase(workDir: string, fallback: string | null): string | null {
  if (existsSync(workDir)) {
    let best: { path: string; size: number } | null = null;
    for (const entry of readdirSync(workDir, { withFileTypes: true })) {
      if (!entry.isFile() || !entry.name.toLowerCase().endsWith(".db")) continue;
      const path = join(workDir, entry.name);
      const size = statSync(path).size;
      if (best === null || size > best.size) best = { path, size };
    }
    if (best !== null) return best.path;
  }
  return fallback !== null && existsSync(fallback) ? fallback : null;
}

/** 输入视图：原始文件 → 表。项目级，跟选中哪次运行无关。 */
function readInputs(
  store: string,
  sessionId: string,
  runDocs: RunRecord[],
  batchId: string | null,
  reads: string[],
  writes: string[],
  snapshot: string | null,
): InputsView {
  const workDir = join(store, "sessions", sessionId, "work");
  const batches = readBatches(workDir);
  const batch = batches.find((item) => item.batchId === batchId)
    ?? [...batches].sort((a, b) => (b.capturedAt ?? "").localeCompare(a.capturedAt ?? ""))[0]
    ?? null;

  const dbPath = findWorkDatabase(workDir, snapshot);
  if (dbPath === null) {
    return buildInputs({ batch, dbPath: null, tables: [], runs: runDocs, reads, writes });
  }

  let db: DatabaseSync | null = null;
  try {
    db = openReadOnly(dbPath);
  } catch {
    db = null;
  }
  if (db === null) {
    return buildInputs({ batch, dbPath, tables: [], runs: runDocs, reads, writes });
  }
  try {
    const base = basename(dbPath);
    return buildInputs({
      batch,
      dbPath,
      tables: readDbFacts(db),
      runs: runDocs,
      reads,
      writes,
      sample: (name) => sampleTable(db as DatabaseSync, name, `${base} · 取样`, 12, 10),
    });
  } finally {
    db.close();
  }
}

interface FocusResult {
  graph: GraphView | null;
  revision: string | null;
  elapsedMs: number | null;
  kind: string;
  commit: string | null;
  dbPath: string | null;
}

/** 读选中运行：run.json → 图；顺带量一下每张表的行数与产出表的前几行。 */
function readFocus(store: string, sessionId: string, run: RunRow): FocusResult {
  const empty: FocusResult = {
    graph: null,
    revision: run.data_revision_id,
    elapsedMs: null,
    kind: "run",
    commit: run.code_commit,
    dbPath: null,
  };
  const runDir = join(store, "sessions", sessionId, "runs", run.id);
  const runJsonPath = join(runDir, "run.json");
  if (!existsSync(runJsonPath)) return empty;

  let doc: Json;
  try {
    doc = asObject(JSON.parse(readFileSync(runJsonPath, "utf8")));
  } catch {
    return empty;
  }

  const names = tableNamesOf(doc);
  const tables: Record<string, TableStat> = {};
  const dbPath = findRunDatabase(runDir);
  let db: DatabaseSync | null = null;
  if (dbPath !== null) {
    try {
      db = openReadOnly(dbPath);
    } catch {
      db = null;
    }
  }

  if (db !== null) {
    for (const name of names) tables[name] = readTableStat(db, name);
  } else {
    // 没有库可读时，退回 run.json 自己记的行数（物化与产出才有）
    const rounds = asObject(doc["rounds"]);
    const round = asObject(rounds["a"] ?? rounds["b"]);
    for (const raw of asArray(asObject(round["materialize"])["entries"])) {
      const item = asObject(raw);
      const table = asString(asObject(item["entry"])["table"]);
      if (table !== null) tables[table] = { rows: asNumber(asObject(item["digest"])["rows"]), columns: null };
    }
    for (const [name, value] of Object.entries(asObject(round["outputs"]))) {
      tables[name] = { rows: asNumber(asObject(value)["rows"]), columns: null };
    }
  }

  const graph = buildGraph({ runId: run.id, tables, sample: null, runJson: doc });
  let sample: TableData | null = null;
  if (db !== null && graph !== null) {
    const target = graph.outputs[0];
    if (target !== undefined) sample = sampleTable(db, target, `该运行落盘的 ${basename(dbPath ?? "")}`);
  }
  if (db !== null) db.close();
  if (graph !== null) graph.sample = sample;

  return {
    graph,
    revision: asString(doc["inputRevisionId"]) ?? run.data_revision_id,
    elapsedMs: asNumber(doc["elapsedMs"]),
    kind: asString(doc["kind"]) ?? "run",
    commit: asString(doc["codeCommit"]) ?? run.code_commit,
    dbPath,
  };
}

export function collectBook(options: CollectOptions): BookView {
  const store = options.store;
  const controlPath = join(store, "control.db");
  if (!existsSync(controlPath)) {
    throw new Error(`找不到 control.db：${controlPath}`);
  }

  const db = openReadOnly(controlPath);
  try {
    const sessions = all<Json>(db, "select * from sessions order by updated_at desc");
    const chosen =
      (options.sessionId === undefined
        ? sessions[0]
        : sessions.find((row) => row["id"] === options.sessionId)) ?? null;
    if (chosen === null) throw new Error("control.db 里没有会话");

    const sessionId = String(chosen["id"]);
    const programId = String(chosen["program_id"] ?? "");
    const programs = all<Json>(db, "select * from programs where id = ?", programId);
    const program = programs[0];
    const programName = program === undefined ? null : asString(program["name"] ?? null) ?? asString(program["title"] ?? null) ?? programId;

    const runRows = all<RunRow>(
      db,
      `select id, session_id, program_id, code_commit, data_revision_id, status,
              started_at, finished_at, exit_code, note
         from runs where session_id = ? order by started_at desc`,
      sessionId,
    );
    const revisionsRows = all<RevisionRow>(
      db,
      `select id, bytes, created_at, sha256, note, path from data_revisions
        where program_id = ? order by created_at desc`,
      programId,
    );
    const publishRows = all<PublishRow>(
      db,
      "select id, commit_sha, at, note from publishes where session_id = ? order by at desc",
      sessionId,
    );

    const runRoot = join(store, "sessions", sessionId, "runs");
    const authors = readAuthors(runRoot);
    const commitToRun = new Map<string, string>();
    for (const run of runRows) {
      if (run.code_commit !== null) commitToRun.set(run.code_commit, run.id);
    }
    const authorByCommit = new Map<string, AuthorRecord>();
    for (const author of authors) {
      if (author.commit !== null) authorByCommit.set(author.commit, author);
    }

    const episodes = readEpisodes(store, sessionId, authors);
    const episodeByFile = new Map<string, EpisodeView>();
    for (const episode of episodes) episodeByFile.set(basename(episode.file), episode);
    for (const author of authors) {
      if (author.sessionFile === null) continue;
      const episode = episodeByFile.get(basename(author.sessionFile));
      if (episode === undefined) continue;
      if (author.commit !== null) episode.runId = commitToRun.get(author.commit) ?? null;
    }

    // 选中的运行：指定优先，否则最近一次带 run.json 的 flow
    let focus: RunRow | null = null;
    let focusResult: FocusResult | null = null;
    const candidates = options.runId === undefined
      ? runRows
      : runRows.filter((run) => run.id === options.runId);
    for (const run of candidates) {
      const result = readFocus(store, sessionId, run);
      if (options.runId !== undefined) {
        focus = run;
        focusResult = result;
        break;
      }
      if (result.graph !== null && result.kind === "flow") {
        focus = run;
        focusResult = result;
        break;
      }
      if (focusResult === null && result.graph !== null) {
        focus = run;
        focusResult = result;
      }
    }

    const runs: RunView[] = runRows.map((run) => {
      const started = run.started_at === null ? null : Date.parse(run.started_at);
      const finished = run.finished_at === null ? null : Date.parse(run.finished_at);
      const elapsed = started !== null && finished !== null && finished >= started ? finished - started : null;
      const author = run.code_commit === null ? undefined : authorByCommit.get(run.code_commit);
      return {
        id: run.id,
        short: run.id.slice(0, 8),
        kind: focus !== null && focus.id === run.id ? (focusResult?.kind ?? "run") : "run",
        status: run.status,
        identical: run.note?.includes("identical=true") ? true : null,
        startedAt: run.started_at,
        finishedAt: run.finished_at,
        elapsedMs: elapsed,
        codeCommit: run.code_commit,
        revision: run.data_revision_id,
        hasTranscript: author !== undefined,
      };
    });

    const currentRevision = revisionsRows[0]?.id ?? null;
    const focusAuthor = focus?.code_commit == null ? undefined : authorByCommit.get(focus.code_commit);
    let focusEpisode: string | null = null;
    if (focusAuthor?.sessionFile != null) {
      focusEpisode = basename(focusAuthor.sessionFile);
    } else if (episodes.length > 0) {
      focusEpisode = basename(episodes[episodes.length - 1]?.file ?? "");
    }

    const revisions: RevisionView[] = revisionsRows.map((row) => ({
      id: row.id,
      short: row.id.slice(0, 8),
      at: row.created_at ?? "",
      bytes: row.bytes,
    }));

    const publishes: PublishView[] = publishRows.map((row) => ({
      short: row.commit_sha.slice(0, 7),
      at: row.at,
      note: row.note,
    }));

    // 输入视图：跟选中哪次运行无关，用的是整个项目的库与全部运行记录
    const runDocs = readRunDocs(runRoot, runRows);
    const focusDoc = focus === null ? undefined : runDocs.find((item) => item.id === focus.id)?.doc;
    const inputs = readInputs(
      store,
      sessionId,
      runDocs,
      focusDoc === undefined ? null : asString(asObject(focusDoc)["batchId"]),
      focusResult?.graph?.nodes.flatMap((node) => node.reads) ?? [],
      focusResult?.graph?.nodes.flatMap((node) => node.writes) ?? [],
      revisionsRows[0]?.path ?? null,
    );

    return {
      generatedAt: new Date().toISOString(),
      store,
      program: program === undefined ? null : { id: programId, name: programName ?? programId },
      session: {
        id: sessionId,
        name: asString(chosen["name"]) ?? "—",
        draftBranch: asString(chosen["draft_branch"]) ?? "—",
        status: asString(chosen["status"]) ?? "—",
      },
      revisions,
      currentRevision,
      lastRunRevision: focusResult?.revision ?? null,
      runs,
      publishes,
      graph: focusResult?.graph ?? null,
      inputs,
      diagram: buildDiagram({ inputs, graph: focusResult?.graph ?? null }),
      transcript: episodes,
      focusEpisode,
    };
  } finally {
    db.close();
  }
}
