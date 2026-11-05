/**
 * 执行一层工作流：先物化，再按拓扑序把节点一个个跑完，并核对「它说的」和「它做的」。
 *
 * 一次调用 = 一次干净重跑。flow-run 自己不建 worktree：
 *   - 物化由平台加载器完成（Excel → 表，纯映射）；
 *   - python 节点由调用方放进干净目录执行；
 *   - SQL 节点在这里直接打进业务库。
 *
 * 核对的是事实，不是判断：
 *   1. 物化是否真的产出了声明的每一张表；
 *   2. 节点读的表在它跑之前存不存在（不存在就是上游没产出或表名写错）；
 *   3. 声明的写表有没有真的变化（声明了却没变 = 空转）。
 *
 * 两次重跑之间比什么：**声明的产出表 + 输出文件**。全库逐表比对暂时不做——
 * 库里还有 `meta.created_at` 这类非确定内容，全量比对会一直红；这条已经记在案。
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';

import { databaseDigest, dirDigests, type Digest, type TableDigest } from './fs-util.ts';
import { parseCall, type FlowNode, type FlowPlan, type MaterializeEntry } from './flow.ts';

export interface NodeExecution {
  exitCode: number;
  timedOut: boolean;
  stdout: string;
  stderr: string;
  elapsedMs: number;
}

export interface NodeRunRecord {
  id: string;
  call: string;
  file: string;
  func: string | null;
  /** 实际传进去的入参（值是绝对路径，可以直接拿去复现）。 */
  kwargs: Record<string, string>;
  exitCode: number;
  timedOut: boolean;
  elapsedMs: number;
  stdout: string;
  stderr: string;
  /** 声明的写表里，内容真的变了的。 */
  changed: string[];
  /** 声明的写表里，跑完还是原样的（空转，要问为什么）。 */
  unchanged: string[];
  /** 读的表在跑之前不存在。 */
  missingReads: string[];
}

export interface MaterializeRecord {
  entry: MaterializeEntry;
  produced: boolean;
  digest: TableDigest | null;
}

export interface FlowRoundResult {
  ok: boolean;
  materialize: {
    exitCode: number;
    timedOut: boolean;
    elapsedMs: number;
    stdout: string;
    stderr: string;
    entries: MaterializeRecord[];
  };
  nodes: NodeRunRecord[];
  /** 声明产出的表的终态摘要：双跑比的就是它。 */
  outputs: Record<string, TableDigest>;
  /** out 目录里的文件（节点可以写文件，但表才是契约）。 */
  files: Digest[];
}

export interface FlowRoundOptions {
  plan: FlowPlan;
  dbPath: string;
  inputsDir: string;
  outDir: string;
  asOf: string;
  /** 跑一次物化（整个流一次，不是每个节点一次）。 */
  runMaterialize: (input: { entries: MaterializeEntry[]; dbPath: string; inputsDir: string }) => NodeExecution;
  /** 跑 python 节点；由调用方负责干净 worktree、超时和收尾。 */
  runPython: (input: {
    node: FlowNode;
    file: string;
    func: string;
    kwargs: Record<string, string>;
    timeoutMs?: number;
  }) => NodeExecution;
  /** 读节点源码（SQL 节点用；必须来自本次运行的 commit，而不是工作区）。 */
  readFile: (path: string) => string;
  log?: (line: string) => void;
}

const PLACEHOLDER = /\{(\w+)\}/g;

export function fillKwargs(values: Record<string, string>, vars: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(values)) {
    out[key] = value.replace(PLACEHOLDER, (whole, name: string) => vars[name] ?? whole);
  }
  return out;
}

/** 库里现有的业务表（不含 sqlite_ 内部表）。 */
export function tableNames(dbPath: string): string[] {
  if (!existsSync(dbPath)) return [];
  const db = new DatabaseSync(dbPath, { readOnly: true });
  try {
    return (
      db
        .prepare("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        .all() as { name: string }[]
    ).map((row) => row.name);
  } finally {
    db.close();
  }
}

/** SQL 节点：整个文件就是一段脚本，在一个事务里打完。 */
export function executeSqlNode(dbPath: string, sql: string): NodeExecution {
  const started = Date.now();
  if (!existsSync(dbPath)) {
    return { exitCode: 1, timedOut: false, stdout: '', stderr: `数据库不存在：${dbPath}`, elapsedMs: 0 };
  }
  const db = new DatabaseSync(dbPath);
  try {
    db.exec('BEGIN');
    db.exec(sql);
    db.exec('COMMIT');
    return { exitCode: 0, timedOut: false, stdout: '', stderr: '', elapsedMs: Date.now() - started };
  } catch (error) {
    try {
      db.exec('ROLLBACK');
    } catch {
      // 回滚失败不该掩盖真正的错误。
    }
    return {
      exitCode: 1,
      timedOut: false,
      stdout: '',
      stderr: error instanceof Error ? error.message : String(error),
      elapsedMs: Date.now() - started,
    };
  } finally {
    db.close();
  }
}

/** 一次干净重跑：物化 → 节点（按拓扑序），途中不断核对声明。 */
export function runFlowRound(options: FlowRoundOptions): FlowRoundResult {
  const log = options.log ?? (() => undefined);
  const byId = new Map(options.plan.spec.nodes.map((node) => [node.id, node]));
  const vars = { db: options.dbPath, inputs: options.inputsDir, out: options.outDir, asOf: options.asOf };
  mkdirSync(options.outDir, { recursive: true });

  /* ---- 第一步：物化（图的上游边界，只做映射） ---- */
  const materializeResult =
    options.plan.spec.materialize.length === 0
      ? { exitCode: 0, timedOut: false, elapsedMs: 0, stdout: '', stderr: '' }
      : options.runMaterialize({
          entries: options.plan.spec.materialize,
          dbPath: options.dbPath,
          inputsDir: options.inputsDir,
        });
  if (materializeResult.stdout.trim()) log(materializeResult.stdout.trimEnd().replace(/^/gm, '    '));
  if (materializeResult.stderr.trim()) log(materializeResult.stderr.trimEnd().replace(/^/gm, '    '));

  const materializeDigest = databaseDigest(
    options.dbPath,
    options.plan.spec.materialize.map((entry) => entry.table),
  );
  const entries: MaterializeRecord[] = options.plan.spec.materialize.map((entry) => ({
    entry,
    produced: materializeDigest[entry.table] !== undefined,
    digest: materializeDigest[entry.table] ?? null,
  }));
  for (const record of entries) {
    if (!record.produced) log(`    ! 物化没有产出 ${record.entry.table}（声明了 ${record.entry.file}）`);
  }
  const materializeOk = materializeResult.exitCode === 0 && !materializeResult.timedOut && entries.every((item) => item.produced);

  /* ---- 第二步：节点 ---- */
  const nodes: NodeRunRecord[] = [];
  let ok = materializeOk;

  if (ok) {
    for (const id of options.plan.order) {
      const node = byId.get(id);
      if (!node) throw new Error(`拓扑序里有未知节点：${id}`);
      const { file, func } = parseCall(node.call, `nodes.${node.id}.call`);
      const existing = new Set(tableNames(options.dbPath));
      const missingReads = node.reads.filter((table) => !existing.has(table));
      const kwargs = {
        db: vars.db,
        inputs: vars.inputs,
        out: vars.out,
        as_of: vars.asOf,
        ...fillKwargs(node.args ?? {}, vars),
      };

      log(`  [${node.id}] ${node.call}${missingReads.length > 0 ? `（缺表：${missingReads.join(',')}）` : ''}`);

      const before = databaseDigest(options.dbPath, [...node.writes]);
      let execution: NodeExecution;
      if (missingReads.length > 0) {
        execution = {
          exitCode: 1,
          timedOut: false,
          stdout: '',
          stderr: `读的表不存在：${missingReads.join(', ')}（上游没产出，或表名写错了）`,
          elapsedMs: 0,
        };
      } else if (func === null) {
        execution = executeSqlNode(options.dbPath, options.readFile(file));
      } else {
        execution = options.runPython({
          node,
          file,
          func,
          kwargs,
          ...(node.timeoutMs ? { timeoutMs: node.timeoutMs } : {}),
        });
      }
      const after = databaseDigest(options.dbPath, [...node.writes]);

      const changed = node.writes.filter((table) => JSON.stringify(before[table]) !== JSON.stringify(after[table]));
      const unchanged = node.writes.filter((table) => !changed.includes(table));

      nodes.push({
        id: node.id,
        call: node.call,
        file,
        func,
        kwargs,
        exitCode: execution.exitCode,
        timedOut: execution.timedOut,
        elapsedMs: execution.elapsedMs,
        stdout: execution.stdout,
        stderr: execution.stderr,
        changed,
        unchanged,
        missingReads,
      });

      if (execution.stdout.trim()) log(execution.stdout.trimEnd().replace(/^/gm, '    '));
      if (execution.stderr.trim()) log(execution.stderr.trimEnd().replace(/^/gm, '    '));
      if (unchanged.length > 0 && execution.exitCode === 0) {
        log(`    ! 声明的写表没有变化：${unchanged.join(', ')}（空转，要么声明多了，要么节点没起作用）`);
      }

      if (execution.exitCode !== 0 || execution.timedOut || missingReads.length > 0) {
        ok = false;
        break; // 一个节点失败，下游产物就没有意义，不在垃圾数据上继续跑
      }
    }
  }

  /* ---- 第三步：声明的产出必须真的出现 ---- */
  // databaseDigest 只返回库里真实存在的表。一张「声明了但没建出来」的表会在两轮里同时消失，
  // diffRounds 看到两边都缺就判 identical——那是「空集合一致」，不是「结果一致」。
  // 空 outputs 更彻底：可比较的集合本身为空，双跑退化成「两轮都没崩」。
  const outputs = databaseDigest(options.dbPath, [...options.plan.spec.outputs]);
  if (ok) {
    const declared = options.plan.spec.outputs;
    const missing = declared.filter((table) => outputs[table] === undefined);
    if (declared.length === 0) {
      log('    ! 这条流没有声明产出表（flow.outputs）：双跑没有可比的东西，判这一轮失败');
      ok = false;
    } else if (missing.length > 0) {
      log(`    ! 声明的产出表没有出现：${missing.join(', ')}（节点没写出来，或写到了别的名字）`);
      ok = false;
    }
  }

  return {
    ok,
    materialize: { ...materializeResult, entries },
    nodes,
    outputs,
    files: dirDigests(options.outDir),
  };
}

/**
 * 两次重跑之间的差异：声明的产出表 + 输出文件。
 *
 * 比的是行内容哈希，不看 SQLite 页布局，也不看 Excel/CSV 的字节格式。
 */
export function diffRounds(
  a: FlowRoundResult,
  b: FlowRoundResult,
): { tables: string[]; files: string[]; identical: boolean } {
  const tables: string[] = [];
  for (const name of new Set([...Object.keys(a.outputs), ...Object.keys(b.outputs)])) {
    if (JSON.stringify(a.outputs[name]) !== JSON.stringify(b.outputs[name])) tables.push(name);
  }
  const fileKey = (entry: Digest): string => `${entry.path}:${entry.sha256}`;
  const aFiles = new Set(a.files.map(fileKey));
  const bFiles = new Set(b.files.map(fileKey));
  const files = [...new Set([...aFiles, ...bFiles])].filter((key) => aFiles.has(key) !== bFiles.has(key)).sort();
  return { tables: tables.sort(), files, identical: tables.length === 0 && files.length === 0 };
}

/** 把物化清单落成 JSON 文件，交给加载器（避免命令行参数过长或转义问题）。 */
export function writeMaterializeSpec(dir: string, entries: MaterializeEntry[]): string {
  mkdirSync(dir, { recursive: true });
  const path = join(dir, 'materialize.json');
  writeFileSync(path, `${JSON.stringify({ entries }, null, 2)}\n`, 'utf8');
  return path;
}

export function readJsonFile<T>(path: string): T {
  return JSON.parse(readFileSync(path, 'utf8')) as T;
}
