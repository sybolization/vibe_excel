#!/usr/bin/env node
/**
 * 真实数据演示台（临时脚手架；S2 的 packages/harness 会把它吸收掉）。
 *
 * 为什么是一堆子命令而不是一个长脚本：
 *   长脚本的状态在内存里，中途挂了就得从头上车；这里每条命令只做一件可重做的事，
 *   状态（program / session / run / revision / artifact / publish）全部落在 control.db 里。
 *   断了重跑同一条命令即可，不会「忘了上次跑到哪」。
 *
 *   setup                      建程序仓库 + 会话 + 导入输入批次（幂等）
 *   author  --prompt=f --label=x  启动真模型写程序 → 提交草稿分支
 *   run     --stage=import|analyze 在**干净 worktree**里按 commit 执行程序 → 记 run/制品/数据版本
 *   verify  --stage=...        同一 commit 跑两次，比较内容哈希 → 确定性证据
 *   publish --verify=<json>    带证据合入 main（缺证据就拒绝）
 *   status                     恢复视图（四个版本引用）
 *   db / damage / restore      看库、制造误操作、从快照恢复
 *   lock    --probe|--hold-ms=n 会话锁：证明同一会话的写操作是串行的
 */

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { isAbsolute, join, resolve } from 'node:path';

import type { SessionId } from '@vibe-excel/contracts';
import { DatabaseSync } from 'node:sqlite';
import {
  acquireLock,
  cleanRerun,
  ensureWorkspace,
  restoreDatabase,
  sessionLockPath,
  snapshotDatabase,
  type WorkspacePaths,
} from '@vibe-excel/runtime';
import type { Program, Session } from '@vibe-excel/session';
import { SessionStore } from '@vibe-excel/session';

import {
  asOf,
  groundTruthDir,
  inputFiles,
  model,
  programName,
  provider,
  PROVIDER_KEY_ENV,
  pythonExe,
  repoRoot,
  stages,
  storeRoot,
} from './env.ts';
import {
  createClient,
  EventRecorder,
  exportSessionHtml,
  mask,
  piVersion,
  resolvePiCli,
  withSanitizedEnv,
} from './pi-io.ts';

const toolsDir = resolve(import.meta.dirname, '../tools');
const dbTool = join(toolsDir, 'db_tool.py');

/* ------------------------------------------------------------------ *
 * 小工具
 * ------------------------------------------------------------------ */

const ts = (): string => new Date().toISOString().replace(/[:.]/g, '-');
const log = (message: string): void => console.log(message);
const hr = (title: string): void => console.log(`\n===== ${title} =====`);
/** 声明成函数而不是箭头常量：只有函数声明才能让 TS 在 `if (!x) fail()` 之后做收窄。 */
function fail(message: string): never {
  throw new Error(message);
}

/** 子命令里失败时置 1，最后统一作为进程退出码。 */
let exitCode = 0;

const sha256File = (path: string): string => createHash('sha256').update(readFileSync(path)).digest('hex');

interface Digest {
  path: string;
  sha256: string;
  bytes: number;
}

/** 目录内所有文件的内容哈希（相对路径用正斜杠，跨平台可比）。 */
function dirDigests(dir: string, base = ''): Digest[] {
  if (!existsSync(dir)) return [];
  const out: Digest[] = [];
  for (const entry of readdirSync(join(dir, base), { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const rel = base === '' ? entry.name : `${base}/${entry.name}`;
    const absolute = join(dir, rel);
    if (entry.isDirectory()) out.push(...dirDigests(dir, rel));
    else if (entry.isFile()) out.push({ path: rel, sha256: sha256File(absolute), bytes: statSync(absolute).size });
  }
  return out;
}

function runPython(args: string[], cwd?: string): string {
  return execFileSync(pythonExe, [dbTool, ...args], {
    encoding: 'utf8',
    cwd,
    env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    maxBuffer: 64 * 1024 * 1024,
  }).trim();
}

/** 库的语义摘要：每张表的行数与内容哈希（与文件字节无关）。 */
function dbDigest(dbPath: string): Record<string, { rows: number; sha256: string }> {
  return JSON.parse(runPython(['digest', dbPath])) as Record<string, { rows: number; sha256: string }>;
}

function parseFlags(argv: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const arg of argv) {
    if (!arg.startsWith('--')) fail(`未知参数：${arg}`);
    const eq = arg.indexOf('=');
    if (eq > 0) out[arg.slice(2, eq)] = arg.slice(eq + 1);
    else out[arg.slice(2)] = 'true';
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * 上下文：程序仓库 / 会话 / 目录
 * ------------------------------------------------------------------ */

interface Ctx {
  store: SessionStore;
  program: Program;
  session: Session;
  paths: WorkspacePaths;
}

function openCtx(sessionId?: string): Ctx {
  const store = SessionStore.open(storeRoot);
  const program = store.listPrograms().find((p) => p.name === programName);
  if (!program) fail(`程序仓库还没建立（${programName}）：先跑 setup`);
  const active = store.listSessions(program.id).filter((s) => s.status === 'active');
  const session = sessionId
    ? store.getSession(sessionId as SessionId)
    : [...active].sort((a, b) => a.createdAt.localeCompare(b.createdAt)).at(-1);
  if (!session) fail('没有活跃会话：先跑 setup');
  return { store, program, session, paths: ensureWorkspace(storeRoot, session.id) };
}

interface Batch {
  id: string;
  dir: string;
  filesDir: string;
  files: { name: string; sha256: string; bytes: number }[];
}

function currentBatch(inputsRoot: string): Batch {
  const dirs = readdirSync(inputsRoot, { withFileTypes: true })
    .filter((e) => e.isDirectory())
    .sort((a, b) => a.name.localeCompare(b.name));
  if (dirs.length !== 1) fail(`输入批次目录应恰好 1 个，实际 ${dirs.length} 个：先跑 setup`);
  const id = dirs[0]!.name;
  const dir = join(inputsRoot, id);
  const manifest = JSON.parse(readFileSync(join(dir, 'manifest.json'), 'utf8')) as {
    files: { name: string; sha256: string; bytes: number }[];
  };
  return { id, dir, filesDir: join(dir, 'files'), files: manifest.files };
}

/* ------------------------------------------------------------------ *
 * 1. setup
 * ------------------------------------------------------------------ */

async function cmdSetup(): Promise<void> {
  hr('setup：程序仓库 + 会话 + 输入批次');
  const store = SessionStore.open(storeRoot);
  log(`制品根 ${storeRoot}`);

  let program = store.listPrograms().find((p) => p.name === programName);
  if (!program) {
    program = store.createProgram({ name: programName });
    log(`新建 program ${program.name} → ${program.repoPath}`);
  } else {
    log(`复用 program ${program.name}（${program.id.slice(0, 8)}）`);
  }

  let session = store.listSessions(program.id).find((s) => s.status === 'active');
  if (!session) {
    session = store.createSession(program.id, { name: `real-data-${asOf}` });
    log(`新建 session ${session.id.slice(0, 8)}，草稿分支 ${session.draftBranch}`);
    log(`  代码工作区（git worktree）${session.worktreePath}`);
  } else {
    log(`复用 session ${session.id.slice(0, 8)}`);
  }

  const paths = ensureWorkspace(storeRoot, session.id);

  // 输入批次：内容寻址。同一批文件重复 setup 不会产生第二份副本。
  const entries = inputFiles
    .map((name) => {
      const source = join(groundTruthDir, name);
      if (!existsSync(source)) fail(`缺少输入文件：${source}`);
      return { name, sha256: sha256File(source), bytes: statSync(source).size };
    })
    .sort((a, b) => a.name.localeCompare(b.name));
  const batchId = createHash('sha256').update(JSON.stringify(entries)).digest('hex').slice(0, 12);
  const batchDir = join(paths.work, 'inputs', batchId);
  if (!existsSync(join(batchDir, 'manifest.json'))) {
    mkdirSync(join(batchDir, 'files'), { recursive: true });
    for (const entry of entries) copyFileSync(join(groundTruthDir, entry.name), join(batchDir, 'files', entry.name));
    writeFileSync(
      join(batchDir, 'manifest.json'),
      `${JSON.stringify({ batchId, asOf, capturedAt: ts(), files: entries }, null, 2)}\n`,
      'utf8',
    );
    log(`输入批次 ${batchId}：复制 ${entries.length} 个文件（${(entries.reduce((s, e) => s + e.bytes, 0) / 1048576).toFixed(1)} MiB）`);
  } else {
    log(`输入批次 ${batchId} 已存在（幂等，不重复复制）`);
  }
  for (const entry of entries) log(`  ${entry.sha256.slice(0, 10)}  ${(entry.bytes / 1048576).toFixed(2).padStart(8)} MiB  ${entry.name}`);

  // 空业务库作为基线数据版本：import 这一轮的输入版本不能凭空编一个。
  const dbPath = join(paths.work, 'hr.db');
  if (!existsSync(dbPath)) {
    const db = new DatabaseSync(dbPath);
    db.exec('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)');
    db.prepare('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)').run('created_at', ts());
    db.close();
    log(`新建空业务库 ${dbPath}（WAL 模式）`);
  }
  const snap = snapshotDatabase(dbPath, paths.dataRevisions);
  const revision = store.registerDataRevision({
    programId: program.id,
    sessionId: session.id,
    sha256: snap.sha256,
    path: snap.path,
    bytes: snap.bytes,
    note: '空库基线',
  });
  log(`基线数据版本 ${revision.id.slice(0, 8)}（sha ${snap.sha256.slice(0, 10)}，${snap.bytes} B）`);

  log(`\nAS_OF=${asOf}  模型=${provider}/${model}  pi=${piVersion()}`);
  log(`Python=${pythonExe}`);
  log(`凭证 ${PROVIDER_KEY_ENV} ${mask(process.env[PROVIDER_KEY_ENV])}`);
}

/* ------------------------------------------------------------------ *
 * 2. author：真模型写程序
 * ------------------------------------------------------------------ */

interface AuthorOptions {
  promptFile: string;
  label: string;
  timeoutMs: number;
  sessionId?: string;
}

function renderPrompt(template: string, ctx: Ctx, outDir: string): string {
  const fill: Record<string, string> = {
    PYTHON: pythonExe.replace(/\\/g, '/'),
    INPUTS: join(ctx.paths.work, 'inputs'),
    DB: join(ctx.paths.work, 'hr.db').replace(/\\/g, '/'),
    OUT: outDir.replace(/\\/g, '/'),
    ASOF: asOf,
  };
  // 输入批次目录名是内容寻址的，提示词里不能写死。
  fill['INPUTS'] = currentBatch(join(ctx.paths.work, 'inputs')).filesDir.replace(/\\/g, '/');
  return template.replace(/\{\{(\w+)\}\}/g, (whole, key: string) => fill[key] ?? whole);
}

async function cmdAuthor(options: AuthorOptions): Promise<void> {
  const ctx = openCtx(options.sessionId);
  const promptPath = isAbsolute(options.promptFile) ? options.promptFile : resolve(options.promptFile);
  if (!existsSync(promptPath)) fail(`提示词文件不存在：${promptPath}`);
  const prompt = renderPrompt(readFileSync(promptPath, 'utf8'), ctx, join(ctx.paths.work, 'out'));

  const cli = resolvePiCli();
  if (!existsSync(cli)) fail(`pi CLI 不存在：${cli}`);
  if (!existsSync(pythonExe)) fail(`Python 不存在：${pythonExe}`);
  const apiKey = process.env[PROVIDER_KEY_ENV];
  if (!apiKey) fail(`缺少 ${PROVIDER_KEY_ENV}`);

  const lock = acquireLock(sessionLockPath(storeRoot, ctx.session.id));
  const runDir = join(ctx.paths.runs, `author-${options.label}-${ts()}`);
  mkdirSync(runDir, { recursive: true });
  const recorder = new EventRecorder();
  recorder.attach(join(runDir, 'events.jsonl'));
  const client = createClient({
    cliPath: cli,
    cwd: ctx.paths.code, // 模型只在这个 worktree 里写程序
    agentDir: ctx.paths.agent,
    sessionDir: ctx.paths.piSessions,
    provider,
    model,
  });

  hr(`author[${options.label}]：cwd=${ctx.paths.code}`);
  log(`模型 ${provider}/${model}，pi ${piVersion()}，凭证 ${mask(apiKey)}`);
  log(`提示词 ${promptPath}（${prompt.length} 字符）`);
  log(`轨迹 ${runDir}`);

  const started = Date.now();
  let ok = false;
  try {
    client.onEvent((event) => recorder.handle(event));
    await withSanitizedEnv({ [PROVIDER_KEY_ENV]: apiKey }, async () => {
      await client.start();
      await client.promptAndWait(prompt, undefined, options.timeoutMs);
    });
    ok = true;
  } catch (error) {
    log(`\n模型会话异常：${error instanceof Error ? error.message : String(error)}`);
  } finally {
    await client.stop().catch(() => undefined);
  }

  let stats: unknown = null;
  try {
    stats = await client.getSessionStats();
  } catch {
    stats = null;
  }
  await recorder.close();
  writeFileSync(join(runDir, 'timeline.txt'), `${recorder.timelineText()}\n`, 'utf8');
  const html: string[] = [];
  for (const name of readdirSync(ctx.paths.piSessions).filter((n) => n.endsWith('.jsonl'))) {
    const out = join(runDir, `${name.replace(/\.jsonl$/, '')}.html`);
    if (exportSessionHtml(cli, join(ctx.paths.piSessions, name), out)) html.push(out);
  }

  const elapsedMs = Date.now() - started;
  const commit = ctx.store.commitDraft(ctx.session.id, `author(${options.label}): 由模型写出的程序`);
  writeFileSync(
    join(runDir, 'author.json'),
    `${JSON.stringify(
      {
        label: options.label,
        promptFile: promptPath,
        provider,
        model,
        piVersion: piVersion(),
        ok,
        elapsedMs,
        eventCount: recorder.eventCount,
        eventTypes: recorder.typeCounts,
        toolNames: recorder.toolNames(),
        toolCallCount: recorder.toolCallCount(),
        commit,
        stats,
        traces: { events: join(runDir, 'events.jsonl'), timeline: join(runDir, 'timeline.txt'), html },
      },
      null,
      2,
    )}\n`,
    'utf8',
  );

  hr('author 结果');
  log(`耗时 ${(elapsedMs / 1000).toFixed(1)}s，事件 ${recorder.eventCount} 条，工具调用 ${recorder.toolCallCount()} 次`);
  log(`工具：${recorder.toolNames().join(', ') || '(无)'}（只用 pi 内置工具，没有自定义工具）`);
  if (stats) log(`会话统计：${JSON.stringify(stats)}`);
  log(commit ? `草稿提交 ${commit}` : '草稿无改动：模型没有写出任何文件（这是事实，不要当成成功）');
  lock.release();
  if (!ok) exitCode = 1;
}

/* ------------------------------------------------------------------ *
 * 3. run：按 commit 在干净 worktree 里执行程序
 * ------------------------------------------------------------------ */

interface RunResult {
  stage: string;
  runId: string;
  commit: string;
  batchId: string;
  inputRevisionId: string;
  exitCode: number;
  timedOut: boolean;
  outputs: Digest[];
  tables: Record<string, { rows: number; sha256: string }> | null;
  outRevisionId: string | null;
  runDir: string;
  elapsedMs: number;
}

function executeStage(
  ctx: Ctx,
  commit: string,
  stage: string,
  dirs: { outDir: string; dbPath: string; inputsDir: string },
  runRoot: string,
): { exitCode: number; timedOut: boolean; stdout: string; stderr: string; manifestPath: string; elapsedMs: number } {
  const spec = stages[stage];
  if (!spec) fail(`未知阶段：${stage}`);
  const args = [spec.entry, ...spec.args({ inputs: dirs.inputsDir, db: dirs.dbPath, out: dirs.outDir })];
  log(`  ${pythonExe} ${args.join(' ')}`);
  const started = Date.now();
  const result = cleanRerun({
    repoPath: ctx.program.repoPath,
    commit,
    runRoot,
    entry: { command: pythonExe, args },
    outputs: [],
    timeoutMs: spec.timeoutMs,
    asOfDate: asOf,
  });
  const elapsedMs = Date.now() - started;
  const tail = (text: string, max = 4000): string => (text.length <= max ? text : `…${text.slice(-max)}`);
  if (result.stdout.trim()) log(`  stdout:\n${tail(result.stdout).replace(/^/gm, '    ')}`);
  if (result.stderr.trim()) log(`  stderr:\n${tail(result.stderr).replace(/^/gm, '    ')}`);
  log(`  exit=${result.exitCode}${result.timedOut ? '（超时）' : ''}  ${(elapsedMs / 1000).toFixed(1)}s`);
  return { ...result, elapsedMs };
}

function cmdRun(options: { stage: string; sessionId?: string }): void {
  const ctx = openCtx(options.sessionId);
  const spec = stages[options.stage];
  if (!spec) fail(`未知阶段：${options.stage}（可选 ${Object.keys(stages).join(' | ')}）`);
  const lock = acquireLock(sessionLockPath(storeRoot, ctx.session.id));
  try {
    const batch = currentBatch(join(ctx.paths.work, 'inputs'));
    const inputRevision = ctx.store.listDataRevisions(ctx.session.id)[0];
    if (!inputRevision) fail('没有数据版本：先跑 setup');
    const commit = ctx.store.draftHead(ctx.session.id);
    if (!commit) fail('草稿分支没有提交：先跑 author');

    hr(`run[${options.stage}]`);
    const run = ctx.store.beginRun(ctx.session.id, {
      inputBatchId: batch.id,
      dataRevisionId: inputRevision.id,
    });
    const runDir = join(ctx.paths.runs, run.id);
    mkdirSync(runDir, { recursive: true });
    const outDir = join(ctx.paths.work, 'out', options.stage);
    mkdirSync(outDir, { recursive: true });
    const dbPath = join(ctx.paths.work, 'hr.db');

    log(`run ${run.id.slice(0, 8)}  代码 ${commit.slice(0, 8)}  输入批次 ${batch.id}  数据版本 ${inputRevision.id.slice(0, 8)}`);
    const result = executeStage(ctx, commit, options.stage, { outDir, dbPath, inputsDir: batch.filesDir }, join(runDir, 'clean'));

    const outputs = dirDigests(outDir);
    const tables = options.stage === 'import' ? dbDigest(dbPath) : null;
    if (options.stage === 'import') {
      outputs.push({ path: 'hr.db', sha256: sha256File(dbPath), bytes: statSync(dbPath).size });
    }
    const succeeded = result.exitCode === 0 && !result.timedOut;
    ctx.store.finishRun(run.id, {
      status: succeeded ? 'succeeded' : 'failed',
      exitCode: result.exitCode,
      note: `stage=${options.stage} outputs=${outputs.length}${result.timedOut ? ' timedOut' : ''}`,
    });
    for (const output of outputs) {
      ctx.store.addArtifact(run.id, { kind: options.stage, path: output.path, sha256: output.sha256, bytes: output.bytes });
    }

    let outRevisionId: string | null = null;
    if (succeeded && options.stage === 'import') {
      const snap = snapshotDatabase(dbPath, ctx.paths.dataRevisions);
      outRevisionId = ctx.store.registerDataRevision({
        programId: ctx.program.id,
        sessionId: ctx.session.id,
        sha256: snap.sha256,
        path: snap.path,
        bytes: snap.bytes,
        parentId: inputRevision.id,
        note: 'import 产出',
      }).id;
      log(`数据版本 ${outRevisionId.slice(0, 8)}（snapshot sha ${snap.sha256.slice(0, 10)}）`);
    }

    if (tables) {
      for (const [table, info] of Object.entries(tables)) log(`  ${table.padEnd(24)} ${String(info.rows).padStart(7)} 行`);
    }

    const record: RunResult = {
      stage: options.stage,
      runId: run.id,
      commit,
      batchId: batch.id,
      inputRevisionId: inputRevision.id,
      exitCode: result.exitCode,
      timedOut: result.timedOut,
      outputs,
      tables,
      outRevisionId,
      runDir,
      elapsedMs: result.elapsedMs,
    };
    writeFileSync(join(runDir, 'run.json'), `${JSON.stringify(record, null, 2)}\n`, 'utf8');
    hr(`run[${options.stage}] 结束`);
    log(`状态 ${succeeded ? 'succeeded' : 'failed'}，产出 ${outputs.length} 个，记录 ${join(runDir, 'run.json')}`);
    if (!succeeded) exitCode = 1;
  } finally {
    lock.release();
  }
}

/* ------------------------------------------------------------------ *
 * 4. verify：同一 commit 跑两次，比内容哈希
 * ------------------------------------------------------------------ */

function cmdVerify(options: { stage: string; sessionId?: string }): void {
  const ctx = openCtx(options.sessionId);
  const spec = stages[options.stage];
  if (!spec) fail(`未知阶段：${options.stage}`);
  const lock = acquireLock(sessionLockPath(storeRoot, ctx.session.id));
  try {
    const batch = currentBatch(join(ctx.paths.work, 'inputs'));
    const commit = ctx.store.draftHead(ctx.session.id);
    if (!commit) fail('草稿分支没有提交：先跑 author');
    const stamp = ts();
    const root = join(ctx.paths.runs, `verify-${options.stage}-${stamp}`);
    hr(`verify[${options.stage}]：同一 commit 连跑两次`);

    const rounds: { exitCode: number; digests: Digest[]; tables: Record<string, { rows: number; sha256: string }> | null }[] = [];
    for (const label of ['a', 'b'] as const) {
      const dir = join(root, label);
      mkdirSync(dir, { recursive: true });
      log(`\n[${label}]`);
      // import 的产物是业务库本身：verify 必须写到临时库，否则第二次会覆盖第一次、也覆盖现场。
      const dbPath = options.stage === 'import' ? join(dir, 'hr.db') : join(ctx.paths.work, 'hr.db');
      const outDir = join(dir, 'out');
      mkdirSync(outDir, { recursive: true });
      const result = executeStage(ctx, commit, options.stage, { outDir, dbPath, inputsDir: batch.filesDir }, join(dir, 'clean'));
      const tables = options.stage === 'import' ? dbDigest(dbPath) : null;
      rounds.push({ exitCode: result.exitCode, digests: dirDigests(outDir), tables });
    }

    const [first, second] = rounds as [(typeof rounds)[number], (typeof rounds)[number]];
    const identical = JSON.stringify(first.digests) === JSON.stringify(second.digests)
      && JSON.stringify(first.tables) === JSON.stringify(second.tables);
    const evidence = {
      stage: options.stage,
      commit,
      batchId: batch.id,
      asOf,
      identical,
      a: first,
      b: second,
      verifiedAt: new Date().toISOString(),
    };
    const evidencePath = join(root, 'verify.json');
    writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`, 'utf8');

    hr('verify 结论');
    log(`两次输出内容${identical ? '完全一致' : '不一致（这就是事实，不要掩盖）'}`);
    if (first.tables) {
      for (const [table, info] of Object.entries(first.tables)) {
        const other = second.tables?.[table];
        log(`  ${table.padEnd(24)} ${String(info.rows).padStart(7)} 行  ${other?.sha256 === info.sha256 ? '一致' : '不同'}`);
      }
    }
    log(`证据 ${evidencePath}`);
    if (!identical) exitCode = 1;
  } finally {
    lock.release();
  }
}

/* ------------------------------------------------------------------ *
 * 5. publish：缺证据就拒绝
 * ------------------------------------------------------------------ */

function cmdPublish(options: { verify: string; sessionId?: string }): void {
  const ctx = openCtx(options.sessionId);
  const path = isAbsolute(options.verify) ? options.verify : resolve(options.verify);
  if (!existsSync(path)) fail(`找不到验证证据：${path}（发布必须带干净重跑证据）`);
  const evidence = JSON.parse(readFileSync(path, 'utf8')) as {
    stage: string;
    commit: string;
    identical: boolean;
    a: { exitCode: number };
    b: { exitCode: number };
  };
  const passed = evidence.identical && evidence.a.exitCode === 0 && evidence.b.exitCode === 0;

  hr('publish');
  log(`证据 ${path}`);
  log(`commit ${evidence.commit.slice(0, 8)}  两次一致=${String(evidence.identical)}  exit=${evidence.a.exitCode}/${evidence.b.exitCode}`);
  const result = ctx.store.publish(ctx.session.id, {
    cleanRunPassed: passed,
    evidence: `${evidence.stage}@${evidence.commit.slice(0, 8)} identical=${String(evidence.identical)} ${path}`,
  });
  log(`已合入 main：${result.commit}`);
  log(`发布记录 ${result.record.id.slice(0, 8)}  于 ${result.record.at}`);
}

/* ------------------------------------------------------------------ *
 * 6. status / db / damage / restore / lock
 * ------------------------------------------------------------------ */

function cmdStatus(options: { sessionId?: string }): void {
  const ctx = openCtx(options.sessionId);
  const view = ctx.store.recoverSession(ctx.session.id);
  hr('恢复视图');
  log(`program ${view.program.name} (${view.program.id.slice(0, 8)})  repo ${view.program.repoPath}`);
  log(`session ${view.session.id.slice(0, 8)}  草稿分支 ${view.session.draftBranch}  基线 ${view.session.baseCommit?.slice(0, 8) ?? '-'}`);
  log(`草稿 HEAD ${view.draftHead?.slice(0, 8) ?? '-'}`);
  log(`\n可恢复节点：${view.restorableNode ? JSON.stringify({
    codeCommit: view.restorableNode.codeCommit.slice(0, 8),
    dataRevision: view.restorableNode.dataRevisionId.slice(0, 8),
    inputBatch: view.restorableNode.inputBatchId,
    run: view.restorableNode.runId.slice(0, 8),
  }) : '(还没有成功的 run)'}`);
  log('\n数据版本：');
  for (const revision of view.dataRevisions) {
    log(`  ${revision.id.slice(0, 8)}  ${revision.sha256.slice(0, 10)}  ${String(revision.bytes).padStart(9)} B  ${revision.note ?? ''}`);
  }
  log('\n运行记录：');
  for (const run of view.runs) {
    log(`  ${run.id.slice(0, 8)}  ${run.status.padEnd(9)} code=${run.codeCommit?.slice(0, 8) ?? '-'} data=${run.dataRevisionId.slice(0, 8)} batch=${run.inputBatchId}  ${run.note ?? ''}`);
  }
  log(`\n发布记录 ${ctx.store.listPublishes(ctx.session.id).length} 条`);
  for (const record of ctx.store.listPublishes(ctx.session.id)) log(`  ${record.commitSha.slice(0, 8)}  ${record.at}  ${record.note ?? ''}`);
  log('\n制品：');
  for (const run of view.runs) {
    for (const artifact of ctx.store.listArtifacts(run.id)) {
      log(`  run ${run.id.slice(0, 8)}  ${artifact.kind.padEnd(8)} ${artifact.path.padEnd(22)} ${artifact.sha256.slice(0, 10)} ${artifact.bytes} B`);
    }
  }
}

function cmdDb(): void {
  const ctx = openCtx();
  const dbPath = join(ctx.paths.work, 'hr.db');
  hr('业务库');
  log(JSON.stringify(dbDigest(dbPath), null, 2));
  const wal = `${dbPath}-wal`;
  log(`\nWAL 文件：${existsSync(wal) ? `${statSync(wal).size} B` : '不存在'}`);
}

function cmdDamage(): void {
  const ctx = openCtx();
  const dbPath = join(ctx.paths.work, 'hr.db');
  hr('制造一次误操作（删掉 employees 一半的行）');
  const before = dbDigest(dbPath);
  log(`删除前 ${JSON.stringify(Object.fromEntries(Object.entries(before).map(([t, v]) => [t, v.rows])))}`);
  runPython(['sql', dbPath, 'DELETE FROM employees WHERE rowid % 2 = 0']);
  if (existsSync(`${dbPath}-wal`)) runPython(['sql', dbPath, 'PRAGMA wal_checkpoint(TRUNCATE)']);
  const after = dbDigest(dbPath);
  log(`删除后 ${JSON.stringify(Object.fromEntries(Object.entries(after).map(([t, v]) => [t, v.rows])))}`);
}

function cmdRestore(options: { revision?: string }): void {
  const ctx = openCtx();
  const dbPath = join(ctx.paths.work, 'hr.db');
  const revisions = ctx.store.listDataRevisions(ctx.session.id);
  const target = options.revision
    ? revisions.find((r) => r.id.startsWith(options.revision!))
    : revisions.find((r) => (r.note ?? '').includes('import 产出')) ?? revisions[0];
  if (!target) fail('没有可用的数据版本');
  hr('从快照恢复业务库');
  log(`数据版本 ${target.id.slice(0, 8)}  ${target.path}`);
  const lock = acquireLock(sessionLockPath(storeRoot, ctx.session.id));
  try {
    const result = restoreDatabase(target.path, dbPath);
    log(`恢复完成，${result.bytes} B`);
  } finally {
    lock.release();
  }
  log(`恢复后 ${JSON.stringify(Object.fromEntries(Object.entries(dbDigest(dbPath)).map(([t, v]) => [t, v.rows])))}`);
}

function cmdLock(options: { probe?: string; 'hold-ms'?: string; sessionId?: string }): void {
  const ctx = openCtx(options.sessionId);
  const path = sessionLockPath(storeRoot, ctx.session.id);
  if (options['hold-ms']) {
    const holdMs = Number(options['hold-ms']);
    hr(`持锁 ${holdMs}ms（pid ${process.pid}）`);
    const lock = acquireLock(path);
    log(`已持锁 ${path}`);
    const until = Date.now() + holdMs;
    while (Date.now() < until) {
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 200);
    }
    lock.release();
    log('已释放');
    return;
  }
  hr('尝试获取会话锁');
  try {
    const lock = acquireLock(path);
    log(`拿到锁（说明当前没有别的执行在写这个会话）${path}`);
    lock.release();
  } catch (error) {
    log(`没拿到锁：${error instanceof Error ? error.message : String(error)}`);
    log('这就是串行化生效的事实：同一会话的写执行不会被放行并行跑。');
  }
}

/* ------------------------------------------------------------------ *
 * 入口
 * ------------------------------------------------------------------ */

const USAGE = `用法：
  node spikes/demo-real-data/src/main.ts setup
  node spikes/demo-real-data/src/main.ts author --prompt=<文件> --label=<名称> [--timeout-ms=1200000]
  node spikes/demo-real-data/src/main.ts run --stage=import|analyze
  node spikes/demo-real-data/src/main.ts verify --stage=import|analyze
  node spikes/demo-real-data/src/main.ts publish --verify=<verify.json>
  node spikes/demo-real-data/src/main.ts status
  node spikes/demo-real-data/src/main.ts db
  node spikes/demo-real-data/src/main.ts damage
  node spikes/demo-real-data/src/main.ts restore [--revision=<id 前缀>]
  node spikes/demo-real-data/src/main.ts lock [--probe|--hold-ms=5000]
公共可选：--session=<id>`;

async function main(): Promise<number> {
  const argv = process.argv.slice(2);
  const command = argv[0];
  const flags = parseFlags(argv.slice(1));
  const sessionId = flags['session'];

  switch (command) {
    case 'setup':
      await cmdSetup();
      return 0;
    case 'author': {
      const promptFile = flags['prompt'];
      if (!promptFile) fail('author 需要 --prompt=<文件>');
      await cmdAuthor({
        promptFile,
        label: flags['label'] ?? 'run',
        timeoutMs: Number(flags['timeout-ms'] ?? 20 * 60 * 1000),
        ...(sessionId ? { sessionId } : {}),
      });
      return exitCode;
    }
    case 'run': {
      const stage = flags['stage'];
      if (!stage) fail('run 需要 --stage=<阶段>');
      cmdRun({ stage, ...(sessionId ? { sessionId } : {}) });
      return exitCode;
    }
    case 'verify': {
      const stage = flags['stage'];
      if (!stage) fail('verify 需要 --stage=<阶段>');
      cmdVerify({ stage, ...(sessionId ? { sessionId } : {}) });
      return exitCode;
    }
    case 'publish': {
      const verify = flags['verify'];
      if (!verify) fail('publish 需要 --verify=<verify.json>');
      cmdPublish({ verify, ...(sessionId ? { sessionId } : {}) });
      return 0;
    }
    case 'status':
      cmdStatus({ ...(sessionId ? { sessionId } : {}) });
      return 0;
    case 'db':
      cmdDb();
      return 0;
    case 'damage':
      cmdDamage();
      return 0;
    case 'restore':
      cmdRestore({ ...(flags['revision'] ? { revision: flags['revision'] } : {}) });
      return 0;
    case 'lock':
      cmdLock({ ...flags });
      return 0;
    default:
      log(USAGE);
      return command ? 2 : 0;
  }
}

main().then(
  (code) => process.exit(code),
  (error: unknown) => {
    console.error(`\n失败：${error instanceof Error ? `${error.message}\n${error.stack ?? ''}` : String(error)}`);
    process.exit(1);
  },
);

// repoRoot 只用于报错路径提示，避免未使用告警被当成问题。
void repoRoot;
