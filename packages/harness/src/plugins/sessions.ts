/**
 * 插件：sessions —— 会话生命周期编排（author / run / verify / publish / status）。
 *
 * 它把四件事串起来，但每件事都只通过服务调用，不认识具体实现：
 *   写程序 → agent      执行 → exec      记录 → store      落盘 → workspace
 *
 * 这一层是「产品语义」所在：草稿提交、数据版本、发布授权都在这里，不在插件里也不在库里。
 */

import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { basename, isAbsolute, join, resolve } from 'node:path';

import type { DataRevisionId, SessionId } from '@vibe-excel/contracts';
import { acquireLock, snapshotDatabase } from '@vibe-excel/runtime';
import type { WorkspacePaths } from '@vibe-excel/runtime';
import type { Program, Session } from '@vibe-excel/session';

import { batchIdOf, databaseDigest, dirDigests, sha256File } from '../fs-util.ts';
import {
  definePlugin,
  service,
  type AgentService,
  type AuthorResult,
  type CommandContext,
  type CommandsService,
  type ExecService,
  type PluginContext,
  type RunResult,
  type SessionsService,
  type StageSpec,
  type StagesService,
  type StatusView,
  type StoreService,
  type TelemetryService,
  type VerifyResult,
  type WorkspaceService,
} from '../kernel.ts';

export interface SessionsConfig {
  /** 程序仓库名（程序是长期资产，会话是它的一次工作）。 */
  program: string;
  /** 业务库文件名，放在会话 work/ 下。 */
  dbFile: string;
  /** 固定业务日期（YYYY-MM-DD）。不读系统时间。 */
  asOf: string;
  /** 提示词里 `{{OUT}}` 的落点（相对 work/）。 */
  outDir: string;
  /** 输入批次：从哪个目录取哪些文件（内容寻址复制进会话工作区）。 */
  inputs?: { dir: string; files: string[] };
}

interface Resolved {
  program: Program;
  session: Session;
  paths: WorkspacePaths;
}

interface Batch {
  id: string;
  dir: string;
  filesDir: string;
}

const stamp = (): string => new Date().toISOString().replace(/[:.]/g, '-');

function fail(message: string): never {
  throw new Error(message);
}

function currentBatch(inputsRoot: string): Batch {
  const dirs = readdirSync(inputsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .sort((a, b) => a.name.localeCompare(b.name));
  if (dirs.length !== 1) fail(`输入批次目录应恰好 1 个，实际 ${dirs.length} 个：先跑 setup`);
  const id = dirs[0]!.name;
  return { id, dir: join(inputsRoot, id), filesDir: join(inputsRoot, id, 'files') };
}

/** 把 `{inputs}` `{db}` `{out}` `{asOf}` 填成实际值。 */
function fillArgs(args: string[], vars: Record<string, string>): string[] {
  return args.map((arg) => arg.replace(/\{(\w+)\}/g, (whole, key: string) => vars[key] ?? whole));
}

function fillTemplate(text: string, vars: Record<string, string>): string {
  return text.replace(/\{\{(\w+)\}\}/g, (whole, key: string) => vars[key] ?? whole);
}

const tail = (text: string, max = 4000): string => (text.length <= max ? text : `…${text.slice(-max)}`);

export default definePlugin<SessionsConfig>({
  name: 'sessions',
  inject: ['store', 'workspace', 'exec', 'agent', 'telemetry', 'stages', 'commands'],
  provide: ['sessions'],
  config(raw: unknown): SessionsConfig {
    const input = raw as {
      program?: unknown;
      dbFile?: unknown;
      asOf?: unknown;
      outDir?: unknown;
      inputs?: { dir?: unknown; files?: unknown };
    };
    if (typeof input?.program !== 'string' || input.program.trim() === '') throw new Error('sessions 插件需要 config.program');
    if (typeof input?.asOf !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(input.asOf)) {
      throw new Error('sessions 插件需要 config.asOf（YYYY-MM-DD，业务日期必须显式钉住）');
    }
    const config: SessionsConfig = {
      program: input.program,
      dbFile: typeof input.dbFile === 'string' && input.dbFile ? input.dbFile : 'hr.db',
      asOf: input.asOf,
      outDir: typeof input.outDir === 'string' && input.outDir ? input.outDir : 'out',
    };
    if (input.inputs !== undefined) {
      const dir = input.inputs?.dir;
      const files = input.inputs?.files;
      if (typeof dir !== 'string' || dir.trim() === '') throw new Error('sessions.inputs.dir 必须是非空字符串');
      if (!Array.isArray(files) || files.length === 0 || files.some((file) => typeof file !== 'string')) {
        throw new Error('sessions.inputs.files 必须是非空字符串数组');
      }
      const absolute = isAbsolute(dir) ? dir : resolve(process.cwd(), dir);
      if (!existsSync(absolute)) throw new Error(`sessions.inputs.dir 不存在：${absolute}`);
      config.inputs = { dir: absolute, files: files as string[] };
    }
    return config;
  },
  apply(ctx: PluginContext, config: SessionsConfig): void {
    const store = service<StoreService>(ctx, 'store');
    const workspace = service<WorkspaceService>(ctx, 'workspace');
    const exec = service<ExecService>(ctx, 'exec');
    const agent = service<AgentService>(ctx, 'agent');
    const telemetry = service<TelemetryService>(ctx, 'telemetry');
    const stages = service<StagesService>(ctx, 'stages');
    const commands = service<CommandsService>(ctx, 'commands');

    const dbPathOf = (paths: WorkspacePaths): string => join(paths.work, config.dbFile);
    const outDirOf = (paths: WorkspacePaths, stage: string): string => join(paths.work, config.outDir, stage);

    const resolveContext = (): Resolved => {
      const db = store.db();
      const program = db.listPrograms().find((candidate) => candidate.name === config.program);
      if (!program) fail(`程序仓库不存在：${config.program}（先跑 setup）`);
      const session = [...db.listSessions(program.id)]
        .filter((candidate) => candidate.status === 'active')
        .sort((a, b) => a.createdAt.localeCompare(b.createdAt))
        .at(-1);
      if (!session) fail('没有活跃会话：先跑 setup');
      return { program, session, paths: workspace.ensure(session.id) };
    };

    const runStage = (input: { stage: string; asOf?: string }): RunResult => {
      const { program, session, paths } = resolveContext();
      const asOf = input.asOf ?? config.asOf;
      const db = store.db();
      // 会话锁：同一会话的数据修改串行执行（跨会话并发的安全性来自目录隔离）。
      return workspace.withLock(session.id, () => {
        const batch = currentBatch(join(paths.work, 'inputs'));
        const inputRevision = db.listDataRevisions(session.id)[0];
        if (!inputRevision) fail('没有数据版本：先跑 setup');
        const commit = db.draftHead(session.id);
        if (!commit) fail('草稿分支没有提交：先跑 author');

        const { pack, spec } = stages.get(input.stage);
        const vars = {
          inputs: batch.filesDir,
          db: dbPathOf(paths),
          out: outDirOf(paths, input.stage),
          asOf,
        };
        mkdirSync(vars.out, { recursive: true });

        const run = db.beginRun(session.id, { inputBatchId: batch.id, dataRevisionId: inputRevision.id });
        const runDir = join(paths.runs, run.id);
        mkdirSync(runDir, { recursive: true });

        console.log(
          `[${pack}/${input.stage}] code=${commit.slice(0, 8)} data=${inputRevision.id.slice(0, 8)} batch=${batch.id}`,
        );
        console.log(`  ${spec.entry} ${fillArgs(spec.args, vars).join(' ')}`);

        const outcome = exec.run({
          repoPath: program.repoPath,
          commit,
          runRoot: join(runDir, 'clean'),
          entry: { entry: spec.entry, args: fillArgs(spec.args, vars) },
          asOf,
          ...(spec.timeoutMs ? { timeoutMs: spec.timeoutMs } : {}),
        });
        if (outcome.stdout.trim()) console.log(`  stdout:\n${tail(outcome.stdout).replace(/^/gm, '    ')}`);
        if (outcome.stderr.trim()) console.log(`  stderr:\n${tail(outcome.stderr).replace(/^/gm, '    ')}`);
        console.log(`  exit=${outcome.exitCode}${outcome.timedOut ? '（超时）' : ''}  ${(outcome.elapsedMs / 1000).toFixed(1)}s`);

        const outputs = dirDigests(vars.out);
        if (spec.mutatesDatabase) {
          outputs.push({ path: config.dbFile, sha256: sha256File(vars.db), bytes: statSync(vars.db).size });
        }
        const succeeded = outcome.exitCode === 0 && !outcome.timedOut;
        db.finishRun(run.id, {
          status: succeeded ? 'succeeded' : 'failed',
          exitCode: outcome.exitCode,
          note: `stage=${input.stage} outputs=${outputs.length}${outcome.timedOut ? ' timedOut' : ''}`,
        });
        for (const output of outputs) {
          db.addArtifact(run.id, { kind: input.stage, path: output.path, sha256: output.sha256, bytes: output.bytes });
        }

        let outRevisionId: DataRevisionId | null = null;
        if (succeeded && spec.mutatesDatabase) {
          const snapshot = snapshotDatabase(vars.db, paths.dataRevisions);
          outRevisionId = db.registerDataRevision({
            programId: program.id,
            sessionId: session.id,
            sha256: snapshot.sha256,
            path: snapshot.path,
            bytes: snapshot.bytes,
            parentId: inputRevision.id,
            note: `${input.stage} 产出`,
          }).id;
          console.log(`  数据版本 ${outRevisionId.slice(0, 8)}（snapshot ${snapshot.sha256.slice(0, 10)}）`);
        }

        const record = {
          stage: input.stage,
          pack,
          runId: run.id,
          codeCommit: commit,
          batchId: batch.id,
          inputRevisionId: inputRevision.id,
          outRevisionId,
          exitCode: outcome.exitCode,
          timedOut: outcome.timedOut,
          elapsedMs: outcome.elapsedMs,
          outputs,
          exec: { manifestPath: outcome.manifestPath, workDir: outcome.workDir },
        };
        writeFileSync(join(runDir, 'run.json'), `${JSON.stringify(record, null, 2)}\n`, 'utf8');
        writeFileSync(join(runDir, 'stdout.log'), outcome.stdout, 'utf8');
        writeFileSync(join(runDir, 'stderr.log'), outcome.stderr, 'utf8');

        return {
          runId: run.id,
          status: succeeded ? 'succeeded' : 'failed',
          exitCode: outcome.exitCode,
          outputs,
          outRevisionId,
          runDir,
        };
      });
    };

    const verifyStage = (input: { stage: string; asOf?: string }): VerifyResult => {
      const { program, session, paths } = resolveContext();
      const asOf = input.asOf ?? config.asOf;
      const db = store.db();
      return workspace.withLock(session.id, () => {
        const batch = currentBatch(join(paths.work, 'inputs'));
        const commit = db.draftHead(session.id);
        if (!commit) fail('草稿分支没有提交：先跑 author');
        const { pack, spec } = stages.get(input.stage);
        const root = join(paths.runs, `verify-${input.stage}-${stamp()}`);

        const rounds: { exitCode: number; outputs: ReturnType<typeof dirDigests>; tables: ReturnType<typeof databaseDigest> }[] = [];
        for (const label of ['a', 'b'] as const) {
          const dir = join(root, label);
          mkdirSync(dir, { recursive: true });
          // 会改库的阶段必须写到临时库，否则第二次会覆盖第一次、也覆盖现场。
          const dbPath = spec.mutatesDatabase ? join(dir, config.dbFile) : dbPathOf(paths);
          const outDir = join(dir, 'out');
          mkdirSync(outDir, { recursive: true });
          const outcome = exec.run({
            repoPath: program.repoPath,
            commit,
            runRoot: join(dir, 'clean'),
            entry: {
              entry: spec.entry,
              args: fillArgs(spec.args, { inputs: batch.filesDir, db: dbPath, out: outDir, asOf }),
            },
            asOf,
            ...(spec.timeoutMs ? { timeoutMs: spec.timeoutMs } : {}),
          });
          console.log(`  [${label}] exit=${outcome.exitCode}  ${(outcome.elapsedMs / 1000).toFixed(1)}s`);
          rounds.push({
            exitCode: outcome.exitCode,
            outputs: dirDigests(outDir),
            tables: spec.mutatesDatabase ? databaseDigest(dbPath) : {},
          });
        }

        const [first, second] = rounds as [(typeof rounds)[number], (typeof rounds)[number]];
        const identical =
          JSON.stringify(first.outputs) === JSON.stringify(second.outputs) &&
          JSON.stringify(first.tables) === JSON.stringify(second.tables);
        const evidencePath = join(root, 'verify.json');
        writeFileSync(
          evidencePath,
          `${JSON.stringify(
            { stage: input.stage, pack, commit, batchId: batch.id, asOf, identical, a: first, b: second, verifiedAt: new Date().toISOString() },
            null,
            2,
          )}\n`,
          'utf8',
        );
        if (!identical) console.log('  两次输出内容不一致（这是事实，不要掩盖）');
        console.log(`  证据 ${evidencePath}`);
        return { evidencePath, identical, stage: input.stage, commit };
      });
    };

    const publish = (input: { verifyPath: string }): { commit: string; note: string | null } => {
      const { session } = resolveContext();
      const path = isAbsolute(input.verifyPath) ? input.verifyPath : resolve(input.verifyPath);
      if (!existsSync(path)) fail(`找不到验证证据：${path}（发布必须带确定性证据）`);
      const evidence = JSON.parse(readFileSync(path, 'utf8')) as {
        stage: string;
        commit: string;
        identical: boolean;
        a: { exitCode: number };
        b: { exitCode: number };
      };
      const passed = evidence.identical && evidence.a.exitCode === 0 && evidence.b.exitCode === 0;
      console.log(`证据 ${path}`);
      console.log(`commit ${evidence.commit.slice(0, 8)} 两次一致=${String(evidence.identical)} exit=${evidence.a.exitCode}/${evidence.b.exitCode}`);
      const result = store.db().publish(session.id, {
        cleanRunPassed: passed,
        evidence: `${evidence.stage}@${evidence.commit.slice(0, 8)} identical=${String(evidence.identical)} ${path}`,
      });
      return { commit: result.commit, note: result.record.note };
    };

    const status = (): StatusView => {
      const { session } = resolveContext();
      const view = store.db().recoverSession(session.id);
      return {
        program: view.program,
        session: view.session,
        runs: view.runs,
        restorable: view.restorableNode
          ? {
              codeCommit: view.restorableNode.codeCommit,
              dataRevisionId: view.restorableNode.dataRevisionId,
              inputBatchId: view.restorableNode.inputBatchId,
              runId: view.restorableNode.runId,
            }
          : null,
        publishes: store.db().listPublishes(session.id).map((record) => ({
          commitSha: record.commitSha,
          at: record.at,
          note: record.note,
        })),
        revisions: view.dataRevisions.map((revision) => ({
          id: revision.id,
          sha256: revision.sha256,
          bytes: revision.bytes,
          note: revision.note,
        })),
      };
    };

    const author = async (input: { promptFile: string; label: string; timeoutMs?: number }): Promise<AuthorResult> => {
      const { session, paths } = resolveContext();
      const promptPath = isAbsolute(input.promptFile) ? input.promptFile : resolve(input.promptFile);
      if (!existsSync(promptPath)) fail(`提示词文件不存在：${promptPath}`);
      const batch = currentBatch(join(paths.work, 'inputs'));
      const vars = {
        PYTHON: exec.interpreter().replace(/\\/g, '/'),
        code: paths.code,
        inputs: batch.filesDir,
        db: dbPathOf(paths),
        out: join(paths.work, config.outDir),
        asOf: config.asOf,
      };
      const prompt = fillTemplate(readFileSync(promptPath, 'utf8'), vars);
      const traceDir = join(paths.runs, `author-${input.label}-${stamp()}`);
      const sink = telemetry.begin(traceDir);

      // 写程序期间也要持锁：模型自己会跑 python，可能在写同一个业务库。
      const lock = acquireLock(workspace.lockPath(session.id));
      try {
        console.log(`[author:${input.label}] cwd=${paths.code}`);
        const outcome = await agent.run({
          cwd: paths.code,
          agentDir: paths.agent,
          sessionDir: paths.piSessions,
          prompt,
          ...(input.timeoutMs ? { timeoutMs: input.timeoutMs } : {}),
          onEvent: (raw) => sink.event(raw),
        });
        for (const file of outcome.sessionFiles) {
          agent.exportHtml(file, join(traceDir, `${basename(file).replace(/\.jsonl$/, '')}.html`));
        }
        const commit = store.db().commitDraft(session.id, `author(${input.label}): 由模型写出的程序`);
        writeFileSync(
          join(traceDir, 'author.json'),
          `${JSON.stringify({ label: input.label, promptFile: promptPath, ...outcome, commit, traceDir }, null, 2)}\n`,
          'utf8',
        );
        console.log(`耗时 ${(outcome.elapsedMs / 1000).toFixed(1)}s，事件 ${outcome.eventCount} 条，工具 ${outcome.toolNames.join(',') || '无'}`);
        console.log(commit ? `草稿提交 ${commit}` : '草稿无改动：模型没有写出任何文件（这是事实，不是成功）');
        if (!outcome.ok) console.log(`模型会话失败：${outcome.error ?? '未知原因'}`);
        return { commit, elapsedMs: outcome.elapsedMs, eventCount: outcome.eventCount, toolNames: outcome.toolNames, traceDir };
      } finally {
        await sink.close();
        lock.release();
      }
    };

    const setup = (): { programId: string; sessionId: string; batchId: string; files: number; reused: boolean } => {
      const db = store.db();
      let program = db.listPrograms().find((candidate) => candidate.name === config.program);
      const programReused = program !== undefined;
      program ??= db.createProgram({ name: config.program });
      let session = db.listSessions(program.id).find((candidate) => candidate.status === 'active');
      const sessionReused = session !== undefined;
      session ??= db.createSession(program.id, { name: `real-data-${config.asOf}` });
      const paths = workspace.ensure(session.id);

      if (!config.inputs) fail('sessions 插件没有配置 inputs（把要录的文件名写进清单）');
      const entries = config.inputs.files
        .map((name) => {
          const source = join(config.inputs!.dir, name);
          if (!existsSync(source)) fail(`缺少输入文件：${source}`);
          return { name, sha256: sha256File(source), bytes: statSync(source).size };
        })
        .sort((a, b) => a.name.localeCompare(b.name));
      const batchId = batchIdOf(entries);
      const batchDir = join(paths.work, 'inputs', batchId);
      if (!existsSync(join(batchDir, 'manifest.json'))) {
        mkdirSync(join(batchDir, 'files'), { recursive: true });
        for (const entry of entries) copyFileSync(join(config.inputs.dir, entry.name), join(batchDir, 'files', entry.name));
        writeFileSync(
          join(batchDir, 'manifest.json'),
          `${JSON.stringify({ batchId, asOf: config.asOf, capturedAt: new Date().toISOString(), files: entries }, null, 2)}\n`,
          'utf8',
        );
      }

      // 空业务库作为基线数据版本：第一次运行的输入版本不能凭空编。
      const dbPath = dbPathOf(paths);
      if (!existsSync(dbPath)) {
        const created = new DatabaseSync(dbPath);
        created.exec('PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)');
        created.prepare('INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)').run('created_at', new Date().toISOString());
        created.close();
      }
      const snapshot = snapshotDatabase(dbPath, paths.dataRevisions);
      db.registerDataRevision({
        programId: program.id,
        sessionId: session.id,
        sha256: snapshot.sha256,
        path: snapshot.path,
        bytes: snapshot.bytes,
        note: '空库基线',
      });

      return {
        programId: program.id,
        sessionId: session.id,
        batchId,
        files: entries.length,
        reused: programReused && sessionReused,
      };
    };

    const sessionService: SessionsService = { author, setup, runStage, verifyStage, publish, status };
    ctx.provide('sessions', sessionService);

    /* ---------------- CLI 命令（命令也是插件注册进来的） ---------------- */

    const failExit = (error: unknown): number => {
      console.error(`失败：${error instanceof Error ? error.message : String(error)}`);
      return 1;
    };

    commands.register({
      name: 'run',
      summary: '在干净 worktree 里按 commit 执行一个阶段',
      run: (cmd: CommandContext) => {
        const stage = cmd.flags['stage'];
        if (!stage) {
          cmd.print('run 需要 --stage=<阶段>');
          return 2;
        }
        try {
          const result = runStage({ stage, ...(cmd.flags['as-of'] ? { asOf: cmd.flags['as-of'] } : {}) });
          return result.status === 'succeeded' ? 0 : 1;
        } catch (error) {
          return failExit(error);
        }
      },
    });

    commands.register({
      name: 'verify',
      summary: '同一 commit 连跑两次，比较内容哈希',
      run: (cmd: CommandContext) => {
        const stage = cmd.flags['stage'];
        if (!stage) {
          cmd.print('verify 需要 --stage=<阶段>');
          return 2;
        }
        try {
          const result = verifyStage({ stage, ...(cmd.flags['as-of'] ? { asOf: cmd.flags['as-of'] } : {}) });
          return result.identical ? 0 : 1;
        } catch (error) {
          return failExit(error);
        }
      },
    });

    commands.register({
      name: 'publish',
      summary: '带确定性证据合入 main',
      run: (cmd: CommandContext) => {
        const verifyPath = cmd.flags['verify'];
        if (!verifyPath) {
          cmd.print('publish 需要 --verify=<verify.json>');
          return 2;
        }
        try {
          const result = publish({ verifyPath });
          cmd.print(`已合入 main：${result.commit}`);
          return 0;
        } catch (error) {
          return failExit(error);
        }
      },
    });

    commands.register({
      name: 'status',
      summary: '恢复视图：代码版本 + 数据版本 + 输入批次 + 制品',
      run: (cmd: CommandContext) => {
        try {
          const view = status();
          cmd.print(`program ${view.program.name} (${view.program.id.slice(0, 8)})  repo ${view.program.repoPath}`);
          cmd.print(`session ${view.session.id.slice(0, 8)}  草稿 ${view.session.draftBranch}  基线 ${view.session.baseCommit?.slice(0, 8) ?? '-'}`);
          cmd.print(`可恢复节点：${view.restorable ? JSON.stringify(view.restorable) : '(无成功运行)'}`);
          cmd.print('数据版本：');
          for (const revision of view.revisions) {
            cmd.print(`  ${revision.id.slice(0, 8)}  ${revision.sha256.slice(0, 10)}  ${String(revision.bytes).padStart(9)} B  ${revision.note ?? ''}`);
          }
          cmd.print('运行记录：');
          for (const run of view.runs) {
            cmd.print(`  ${run.id.slice(0, 8)}  ${run.status.padEnd(9)} code=${run.codeCommit?.slice(0, 8) ?? '-'} data=${run.dataRevisionId.slice(0, 8)}  ${run.note ?? ''}`);
          }
          cmd.print(`发布记录 ${view.publishes.length} 条`);
          for (const record of view.publishes) cmd.print(`  ${record.commitSha.slice(0, 8)}  ${record.at}  ${record.note ?? ''}`);
          return 0;
        } catch (error) {
          return failExit(error);
        }
      },
    });

    commands.register({
      name: 'author',
      summary: '让模型写程序并提交到草稿分支',
      run: async (cmd: CommandContext) => {
        const promptFile = cmd.flags['prompt'];
        if (!promptFile) {
          cmd.print('author 需要 --prompt=<文件>');
          return 2;
        }
        try {
          const result = await author({
            promptFile,
            label: cmd.flags['label'] ?? 'run',
            ...(cmd.flags['timeout-ms'] ? { timeoutMs: Number(cmd.flags['timeout-ms']) } : {}),
          });
          return result.commit ? 0 : 1;
        } catch (error) {
          return failExit(error);
        }
      },
    });

    commands.register({
      name: 'setup',
      summary: '建程序仓库 / 会话 / 输入批次 / 空库基线（幂等）',
      run: (cmd: CommandContext) => {
        try {
          const result = setup();
          cmd.print(`program   ${result.programId.slice(0, 8)}${result.reused ? '（复用）' : '（新建）'}`);
          cmd.print(`session   ${result.sessionId.slice(0, 8)}`);
          cmd.print(`输入批次   ${result.batchId}（${result.files} 个文件）`);
          return 0;
        } catch (error) {
          return failExit(error);
        }
      },
    });

    commands.register({
      name: 'stages',
      summary: '列出已注册的阶段包',
      run: (cmd: CommandContext) => {
        for (const pack of stages.packs()) {
          cmd.print(`pack ${pack.pack}`);
          for (const [name, spec] of Object.entries(pack.stages)) {
            cmd.print(`  ${name.padEnd(10)} ${spec.entry}  outputs=${spec.outputs.length}${spec.mutatesDatabase ? ' mutatesDatabase' : ''}`);
          }
        }
        return 0;
      },
    });

    commands.register({
      name: 'db',
      summary: '打印业务库的语义摘要（每表行数 + 内容哈希）',
      run: (cmd: CommandContext) => {
        try {
          const { paths } = resolveContext();
          const digest = databaseDigest(dbPathOf(paths));
          cmd.print(JSON.stringify(digest, null, 2));
          return 0;
        } catch (error) {
          return failExit(error);
        }
      },
    });
  },
});
