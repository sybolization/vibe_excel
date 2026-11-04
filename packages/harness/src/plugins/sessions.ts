/**
 * 插件：sessions —— 会话生命周期编排（author / run / verify / publish / status）。
 *
 * 它把四件事串起来，但每件事都只通过服务调用，不认识具体实现：
 *   写程序 → agent      执行 → exec      记录 → store      落盘 → workspace
 *
 * 这一层是「产品语义」所在：草稿提交、数据版本、发布授权都在这里，不在插件里也不在库里。
 */

import { execFileSync } from 'node:child_process';
import {
  appendFileSync,
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { basename, isAbsolute, join, resolve } from 'node:path';
import { createInterface } from 'node:readline/promises';

import type { DataRevisionId, RunId, SessionId } from '@vibe-excel/contracts';
import { acquireLock, restoreDatabase, snapshotDatabase } from '@vibe-excel/runtime';
import type { WorkspacePaths } from '@vibe-excel/runtime';
import type { Program, Session } from '@vibe-excel/session';

import { batchIdOf, databaseDigest, dirDigests, sha256File } from '../fs-util.ts';
import { describeFlow, formatFlowWarning, planFlow, suggestOptimizations } from '../flow.ts';
import { diffRounds, runFlowRound, writeMaterializeSpec, type FlowRoundResult, type NodeExecution } from '../flow-run.ts';
import { loadFlowAtCommit, materializeRunnerPath, nodeRunnerPath, readFileAtCommit } from '../flow-source.ts';
import { assertSameFlow, buildApplyPrompt, buildReviewPrompt, parseProposalFile, selectProposal } from '../optimize.ts';
import { probeWorkspace, renderWorkspaceFacts, writeAgentFactsFile } from '../workspace-facts.ts';
import {
  definePlugin,
  service,
  type AgentService,
  type AskQuestion,
  type AuthorResult,
  type CommandContext,
  type CommandsService,
  type ExecService,
  type OptimizeResult,
  type PluginContext,
  type RunResult,
  type SessionsService,
  type StatusView,
  type StoreService,
  type TelemetryService,
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
  /** 工作流声明在程序仓库里的路径。 */
  flowPath: string;
  /** 是否允许模型中途提问（`ask_user`）。关掉之后疑问只能落进产物。 */
  askEnabled: boolean;
  /** 优化评审 / 应用的超时（比写程序短：它只是读代码给建议）。 */
  optimizeTimeoutMs: number;
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

/** 最近一次 optimize 评审留下的建议文件（apply 默认读它）。 */
function latestProposalPath(runsRoot: string): string {
  if (!existsSync(runsRoot)) throw new Error(`还没有跑过 optimize：${runsRoot} 不存在`);
  const dirs = readdirSync(runsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && entry.name.startsWith('optimize-review-'))
    .map((entry) => entry.name)
    .sort();
  const latest = dirs.at(-1);
  if (!latest) throw new Error(`还没有跑过 optimize：${runsRoot} 下没有 optimize-review-* 目录`);
  return join(runsRoot, latest, 'optimize-proposal.json');
}

/**
 * 把 `{{KEY}}` 填成实际值；没定义的占位符原样留下（宁可让人看见，也不要悄悄变成空串）。
 * 导出是为了让测试能断言「提示词里没有残留占位符」。
 */
export function fillTemplate(text: string, vars: Record<string, string>): string {
  return text.replace(/\{\{(\w+)\}\}/g, (whole, key: string) => vars[key] ?? whole);
}

export default definePlugin<SessionsConfig>({
  name: 'sessions',
  inject: ['store', 'workspace', 'exec', 'agent', 'telemetry', 'commands'],
  provide: ['sessions'],
  config(raw: unknown): SessionsConfig {
    const input = raw as {
      program?: unknown;
      dbFile?: unknown;
      asOf?: unknown;
      outDir?: unknown;
      flowPath?: unknown;
      askEnabled?: unknown;
      optimizeTimeoutMs?: unknown;
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
      flowPath: typeof input.flowPath === 'string' && input.flowPath ? input.flowPath : 'flow.json',
      askEnabled: input.askEnabled !== false,
      optimizeTimeoutMs: Number.isFinite(Number(input.optimizeTimeoutMs)) && Number(input.optimizeTimeoutMs) > 0
        ? Number(input.optimizeTimeoutMs)
        : 10 * 60 * 1000,
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
    const commands = service<CommandsService>(ctx, 'commands');

    const dbPathOf = (paths: WorkspacePaths): string => join(paths.work, config.dbFile);

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

    /**
     * 探测工作区事实：库在哪、python 在哪、venv 里装了什么。
     *
     * 每次 author/optimize 前重算，而不是在提示词里写死——路径会变（换机器、目录搬家、
     * venv 被 uv 重建），写死的句子在路径失效时会安静地变成谎言。
     */
    const factsOf = (program: Program, session: Session, paths: WorkspacePaths) => {
      let inputsDir: string;
      try {
        inputsDir = currentBatch(join(paths.work, 'inputs')).filesDir;
      } catch {
        // 还没有批次时不要让探测跟着挂：事实表里写「0 个文件」比抛错有用。
        inputsDir = join(paths.work, 'inputs');
      }
      return probeWorkspace({
        program: program.name,
        sessionId: session.id,
        root: paths.root,
        work: paths.work,
        code: paths.code,
        agent: paths.agent,
        python: exec.interpreter(),
        dbPath: dbPathOf(paths),
        flowPath: join(paths.code, config.flowPath),
        inputsDir,
        outDir: join(paths.work, config.outDir),
        asOf: config.asOf,
      });
    };

    /**
     * 跑整条工作流：**每次都跑两遍**。
     *
     * 两轮各自从同一份数据快照出发、在自己的目录里写自己的库；比的是「声明的产出」
     * （flow.outputs 里的表 + out 目录里的文件）。一致才把第一轮的结果提升到 work/，
     * 不一致就是这次运行失败——双跑不是可跳过的检查，它就是运行的一半。
     *
     * 比全库暂时不做：库里还有 `meta.created_at` 这类非确定内容，全量比对会一直红。
     */
    /**
     * 模型中途提问时的处理：在终端里问人，答案写回 pi，同时逐条落进 `questions.jsonl`。
     *
     * 为什么要落盘：一次「建表口径」的确认，事后要能回答「这个数是怎么定下来的」。
     * 对话本身不留痕，落盘的那一问一答才是证据。
     */
    const makeAskUser = (traceDir: string) => {
      const questionsPath = join(traceDir, 'questions.jsonl');
      return async (question: AskQuestion): Promise<string | undefined> => {
        console.log(`\n[模型提问] ${question.question}`);
        for (const [index, option] of question.options.entries()) console.log(`  ${index + 1}) ${option}`);
        const record = (answer: string | null, reason?: string): void => {
          const entry = { at: new Date().toISOString(), ...question, answer, ...(reason ? { reason } : {}) };
          appendFileSync(questionsPath, `${JSON.stringify(entry)}\n`, 'utf8');
        };

        // 非交互（CI / 管道）时不假装有人回答：直接记「未确认」，让模型按保守口径走。
        if (!process.stdin.isTTY) {
          console.log('  非交互环境（stdin 不是终端）：记为未确认，模型按保守口径继续');
          record(null, 'non-interactive');
          return undefined;
        }

        const rl = createInterface({ input: process.stdin, output: process.stdout });
        try {
          const answer = (await rl.question('你的回答 > ')).trim();
          record(answer === '' ? null : answer);
          return answer === '' ? undefined : answer;
        } finally {
          rl.close();
        }
      };
    };

    const runFlow = (input: { asOf?: string }): RunResult => {
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

        const flow = loadFlowAtCommit(program.repoPath, commit, config.flowPath, { python: exec.interpreter() });
        const plan = planFlow(flow);
        const run = db.beginRun(session.id, { inputBatchId: batch.id, dataRevisionId: inputRevision.id });
        const runDir = join(paths.runs, run.id);
        mkdirSync(runDir, { recursive: true });

        const startedAt = Date.now();
        console.log(
          `[flow ${flow.flow}@${plan.digest}] code=${commit.slice(0, 8)} data=${inputRevision.id.slice(0, 8)} batch=${batch.id}`,
        );
        for (const line of describeFlow(plan)) console.log(`  ${line}`);
        // 优化建议只在这里提示，不改变任何行为：要不要更短，是人决定的事。
        for (const hint of suggestOptimizations(plan)) console.log(`  ~ 可优化（不影响本次运行）：${hint.note}`);

        const rounds: { a: FlowRoundResult | null; b: FlowRoundResult | null } = { a: null, b: null };
        for (const label of ['a', 'b'] as const) {
          const dir = join(runDir, label);
          mkdirSync(dir, { recursive: true });
          // 每一轮都从同一个数据快照出发：否则第二轮读到的就是第一轮的结果，比了等于没比。
          const dbPath = join(dir, flow.db);
          copyFileSync(inputRevision.path, dbPath);
          const outDir = join(dir, 'out');
          mkdirSync(outDir, { recursive: true });
          console.log(`  ── 第 ${label} 轮 ──`);
          rounds[label] = runFlowRound({
            plan,
            dbPath,
            inputsDir: batch.filesDir,
            outDir,
            asOf,
            log: (line) => console.log(line),
            runMaterialize: ({ entries, dbPath: target, inputsDir }) => {
              const specPath = writeMaterializeSpec(join(dir, 'materialize'), entries);
              return exec.run({
                repoPath: program.repoPath,
                commit,
                runRoot: join(dir, 'clean-materialize'),
                entry: {
                  entry: materializeRunnerPath(),
                  // `--repo .` 指的就是这次执行所在的干净 worktree：load 例外通道要的是
                  // 「这个 commit 里的函数」，不是会话工作区里可能被改过的文件。
                  args: ['--db', target, '--inputs', inputsDir, '--spec', specPath, '--repo', '.'],
                },
                asOf,
              });
            },
            runPython: ({ node, file, func, kwargs, timeoutMs }) =>
              exec.run({
                repoPath: program.repoPath,
                commit,
                runRoot: join(dir, 'clean', node.id),
                entry: {
                  entry: nodeRunnerPath(),
                  args: [
                    file,
                    func,
                    ...Object.entries(kwargs).flatMap(([key, value]) => [`--${key.replace(/_/g, '-')}`, value]),
                  ],
                },
                asOf,
                ...(timeoutMs ? { timeoutMs } : {}),
              }),
            readFile: (path) => readFileAtCommit(program.repoPath, commit, path),
          });
          if (!rounds[label]!.ok) {
            console.log(`  第 ${label} 轮失败，不再跑第二轮（在失败的结果上做比较没有意义）`);
            break;
          }
        }

        const first = rounds.a;
        const second = rounds.b;
        const diff = first && second ? diffRounds(first, second) : { tables: [], files: [], identical: false };
        const succeeded = first !== null && second !== null && first.ok && second.ok && diff.identical;
        const elapsedMs = Date.now() - startedAt;

        let outRevisionId: DataRevisionId | null = null;
        let outputs: ReturnType<typeof dirDigests> = [];
        if (succeeded && first) {
          const outTarget = join(paths.work, config.outDir);
          rmSync(outTarget, { recursive: true, force: true });
          cpSync(join(runDir, 'a', 'out'), outTarget, { recursive: true });
          restoreDatabase(join(runDir, 'a', flow.db), dbPathOf(paths));
          outputs = dirDigests(outTarget);
          outputs.push({ path: flow.db, sha256: sha256File(dbPathOf(paths)), bytes: statSync(dbPathOf(paths)).size });
          const snapshot = snapshotDatabase(dbPathOf(paths), paths.dataRevisions);
          outRevisionId = db.registerDataRevision({
            programId: program.id,
            sessionId: session.id,
            sha256: snapshot.sha256,
            path: snapshot.path,
            bytes: snapshot.bytes,
            parentId: inputRevision.id,
            note: `${flow.flow}@${plan.digest} 产出`,
          }).id;
          console.log(`  数据版本 ${outRevisionId.slice(0, 8)}（snapshot ${snapshot.sha256.slice(0, 10)}）`);
        } else {
          console.log('  两次重跑不一致或失败：不提升产物、不登记数据版本（这是事实，不要掩盖）');
          if (diff.tables.length > 0) console.log(`    不一致的表：${diff.tables.join(', ')}`);
          if (diff.files.length > 0) console.log(`    不一致的文件：${diff.files.join(', ')}`);
        }

        const status = succeeded ? 'succeeded' : 'failed';
        const exitCode = succeeded ? 0 : 1;
        db.finishRun(run.id, { status, exitCode, note: `flow=${flow.flow}@${plan.digest} identical=${String(diff.identical)}` });
        for (const output of outputs) {
          db.addArtifact(run.id, { kind: 'flow', path: output.path, sha256: output.sha256, bytes: output.bytes });
        }

        const record = {
          kind: 'flow',
          flow: flow.flow,
          flowDigest: plan.digest,
          codeCommit: commit,
          // 图不进版本库（脚本即事实，图是它的投影）：它是这次运行的证据，不是要提交的产物。
          graph: {
            source: flow.entry !== undefined ? 'code' : 'json',
            entry: flow.entry ?? null,
            warnings: flow.warnings ?? [],
            nodes: plan.spec.nodes,
          },
          batchId: batch.id,
          inputRevisionId: inputRevision.id,
          asOf,
          status,
          identical: diff.identical,
          diff,
          rounds,
          outRevisionId,
          outputs,
          elapsedMs,
        };
        writeFileSync(join(runDir, 'run.json'), `${JSON.stringify(record, null, 2)}\n`, 'utf8');
        console.log(`  运行记录 ${join(runDir, 'run.json')}`);

        return {
          runId: run.id,
          status,
          exitCode,
          identical: diff.identical,
          flowDigest: plan.digest,
          outputs,
          outRevisionId,
          runDir,
        };
      });
    };

    /**
     * 发布 = 把一条**已经双跑一致**的工作流合入 main，从此它可以不带模型、不带会话地反复运行。
     *
     * 证据不在单独的文件里，就在那次运行的 run.json 里：`identical: true` 是运行的一部分，
     * 不是另一个阶段。拿不到这条证据就拒绝发布——发布改变的是未来，不能凭感觉。
     */
    const publish = (input: { runId?: string }): { commit: string; note: string | null } => {
      const { session, paths } = resolveContext();
      const db = store.db();
      const run = input.runId ? db.getRun(input.runId as RunId) : db.listRuns(session.id)[0];
      if (!run) fail('没有可发布的运行记录：先跑 run');
      const recordPath = join(paths.runs, run.id, 'run.json');
      if (!existsSync(recordPath)) fail(`运行记录缺失：${recordPath}（不能发布一条没有记录在案的运行）`);

      const record = JSON.parse(readFileSync(recordPath, 'utf8')) as {
        status?: string;
        identical?: boolean;
        flowDigest?: string;
        codeCommit?: string;
      };
      console.log(`运行 ${run.id.slice(0, 8)}  flow@${record.flowDigest ?? '?'}  两次一致=${String(record.identical)}`);
      if (record.status !== 'succeeded' || record.identical !== true) {
        fail(
          `这次运行没有「两次一致」的证据（status=${record.status ?? '?'} identical=${String(record.identical)}）：拒绝发布`,
        );
      }
      const result = db.publish(session.id, {
        cleanRunPassed: true,
        evidence: `flow@${record.flowDigest} run=${run.id} identical=true`,
      });
      return { commit: result.commit, note: result.record.note };
    };

    /**
     * `optimize`：模型自检「能不能更短」→ 出建议 → 人批准 → 才动代码。
     *
     * 两段式的好处是评审可以被安全地重复跑：评审在**只读的干净 worktree** 里进行，
     * 它连会话工作区都碰不到，所以「只是看看」不会弄脏草稿。
     */
    const optimize = async (input: { apply?: string; from?: string; timeoutMs?: number }): Promise<OptimizeResult> => {
      const { program, session, paths } = resolveContext();
      const db = store.db();
      const commit = db.draftHead(session.id);
      if (!commit) fail('草稿分支没有提交：先跑 author');
      const flow = loadFlowAtCommit(program.repoPath, commit, config.flowPath, { python: exec.interpreter() });
      const plan = planFlow(flow);
      const flowJson = readFileAtCommit(program.repoPath, commit, config.flowPath);
      const planLines = describeFlow(plan, { steps: true });
      // 评审/应用也用同一份探测结果：两个命令看到的必须是同一个工作区。
      const facts = factsOf(program, session, paths);
      const environment = renderWorkspaceFacts(facts);
      writeAgentFactsFile(facts, paths.agent);

      /* ---- 第二段：应用一条被批准的建议（会写文件，持锁） ---- */
      if (input.apply) {
        const proposalPath = input.from
          ? isAbsolute(input.from)
            ? input.from
            : resolve(input.from)
          : latestProposalPath(paths.runs);
        if (!existsSync(proposalPath)) fail(`找不到建议文件：${proposalPath}（先跑一次 optimize）`);
        const file = parseProposalFile(readFileSync(proposalPath, 'utf8'));
        assertSameFlow(file, plan.digest);
        const proposal = selectProposal(file, input.apply);
        console.log(`批准的建议：${proposal.id} ${proposal.title}`);
        console.log(`  风险：${proposal.risk}`);
        console.log(`  验证：${proposal.verify}`);

        const traceDir = join(paths.runs, `optimize-apply-${stamp()}`);
        const sink = telemetry.begin(traceDir);
        const askUser = makeAskUser(traceDir);
        const lock = acquireLock(workspace.lockPath(session.id));
        try {
          const prompt = buildApplyPrompt({
            proposal,
            approvedAt: new Date().toISOString(),
            codeRoot: paths.code,
            flowJson,
            planLines,
            environment,
          });
          const outcome = await agent.run({
            cwd: paths.code,
            agentDir: paths.agent,
            sessionDir: paths.piSessions,
            prompt,
            timeoutMs: input.timeoutMs ?? config.optimizeTimeoutMs,
            ...(config.askEnabled ? { onQuestion: askUser } : {}),
            onEvent: (raw) => sink.event(raw),
          });
          const commitAfter = store.db().commitDraft(session.id, `optimize: 应用已批准的建议 ${proposal.id}`);
          writeFileSync(
            join(traceDir, 'optimize.json'),
            `${JSON.stringify({ mode: 'apply', proposal, proposalPath, outcome, commit: commitAfter, traceDir }, null, 2)}\n`,
            'utf8',
          );
          if (!commitAfter) console.log('没有改动：模型没有实现这条建议（这是事实，不是成功）');
          else console.log(`草稿提交 ${commitAfter.slice(0, 8)}`);
          return { mode: 'apply', traceDir, proposalPath, proposals: [proposal], commit: commitAfter };
        } finally {
          await sink.close();
          lock.release();
        }
      }

      /* ---- 第一段：只读评审 ---- */
      const traceDir = join(paths.runs, `optimize-review-${stamp()}`);
      const proposalPath = join(traceDir, 'optimize-proposal.json');
      mkdirSync(traceDir, { recursive: true });
      const worktree = join(traceDir, 'clean');
      const sink = telemetry.begin(traceDir);
      const askUser = makeAskUser(traceDir);
      console.log(`[optimize] flow@${plan.digest}  只读评审，不碰草稿`);
      for (const hint of suggestOptimizations(plan)) console.log(`  ~ ${hint.note}`);

      try {
        execFileSync('git', ['-C', program.repoPath, 'worktree', 'add', '--detach', worktree, commit], {
          encoding: 'utf8',
          stdio: ['ignore', 'pipe', 'pipe'],
        });
        const prompt = buildReviewPrompt({
          flowJson,
          flowDigest: plan.digest,
          planLines,
          hints: suggestOptimizations(plan),
          proposalPath,
          codeRoot: worktree,
          environment,
        });
        const outcome = await agent.run({
          cwd: worktree,
          agentDir: paths.agent,
          sessionDir: paths.piSessions,
          prompt,
          timeoutMs: input.timeoutMs ?? config.optimizeTimeoutMs,
          ...(config.askEnabled ? { onQuestion: askUser } : {}),
          onEvent: (raw) => sink.event(raw),
        });
        if (!existsSync(proposalPath)) {
          fail(`模型没有写建议文件（这是事实，不是「没有可优化的地方」）：${proposalPath}`);
        }
        const file = parseProposalFile(readFileSync(proposalPath, 'utf8'));
        assertSameFlow(file, plan.digest);
        writeFileSync(
          join(traceDir, 'optimize.json'),
          `${JSON.stringify({ mode: 'review', flowDigest: plan.digest, outcome, file, traceDir }, null, 2)}\n`,
          'utf8',
        );

        console.log(`建议 ${file.proposals.length} 条（${proposalPath}）：`);
        for (const proposal of file.proposals) {
          console.log(`  ${proposal.id}  ${proposal.title}`);
          console.log(`      省：节点 ${proposal.saves.nodes} / 表 ${proposal.saves.tables}   风险：${proposal.risk}`);
          console.log(`      验证：${proposal.verify}`);
        }
        if (file.proposals.length > 0) {
          console.log('要改哪一条，请显式批准：optimize --apply=<id>');
        } else {
          console.log('模型认为没有更短的等价写法（这也是结论）。');
        }
        return { mode: 'review', traceDir, proposalPath, proposals: file.proposals, commit: null };
      } finally {
        await sink.close();
        try {
          execFileSync('git', ['-C', program.repoPath, 'worktree', 'remove', '--force', worktree], { stdio: 'ignore' });
        } catch {
          // worktree 可能根本没建起来：清理失败不能掩盖真正的错误。
        }
      }
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
      const { program, session, paths } = resolveContext();
      const promptPath = isAbsolute(input.promptFile) ? input.promptFile : resolve(input.promptFile);
      if (!existsSync(promptPath)) fail(`提示词文件不存在：${promptPath}`);
      const batch = currentBatch(join(paths.work, 'inputs'));
      // 先探测再拼提示词：模型看到的路径必须和磁盘上的状态对得上。
      const facts = factsOf(program, session, paths);
      const environment = renderWorkspaceFacts(facts);
      const agentsPath = writeAgentFactsFile(facts, paths.agent);
      const vars = {
        PYTHON: exec.interpreter().replace(/\\/g, '/'),
        code: paths.code,
        inputs: batch.filesDir,
        db: dbPathOf(paths),
        out: join(paths.work, config.outDir),
        asOf: config.asOf,
        WORKSPACE: environment,
        // 大写别名：提示词模板长期写的是 {{INPUTS}} 这种全大写，两边都认，免得再出现「占位符没被替换」。
        CODE: paths.code,
        INPUTS: batch.filesDir,
        DB: dbPathOf(paths),
        OUT: join(paths.work, config.outDir),
        ASOF: config.asOf,
        FLOW: config.flowPath,
      };
      const prompt = fillTemplate(readFileSync(promptPath, 'utf8'), vars);
      const traceDir = join(paths.runs, `author-${input.label}-${stamp()}`);
      const sink = telemetry.begin(traceDir);
      const askUser = makeAskUser(traceDir);

      // 写程序期间也要持锁：模型自己会跑 python，可能在写同一个业务库。
      const lock = acquireLock(workspace.lockPath(session.id));
      try {
        console.log(`[author:${input.label}] cwd=${paths.code}`);
        console.log(`[author:${input.label}] 工作区事实写进 ${agentsPath}`);
        const outcome = await agent.run({
          cwd: paths.code,
          agentDir: paths.agent,
          sessionDir: paths.piSessions,
          prompt,
          ...(input.timeoutMs ? { timeoutMs: input.timeoutMs } : {}),
          ...(config.askEnabled ? { onQuestion: askUser } : {}),
          onEvent: (raw) => sink.event(raw),
        });
        for (const file of outcome.sessionFiles) {
          agent.exportHtml(file, join(traceDir, `${basename(file).replace(/\.jsonl$/, '')}.html`));
        }
        const commit = store.db().commitDraft(session.id, `author(${input.label}): 由模型写出的程序`);
        // 提交之后立刻试着解析一次：代码解析不出流程是最常见的失败，
        // 现在告诉人，比等到 `run` 才炸好。**不改退出码**——提交这件事已经发生了。
        let flowDiagnostic: string | null = null;
        if (commit) {
          try {
            const parsed = loadFlowAtCommit(program.repoPath, commit, config.flowPath, { python: exec.interpreter() });
            const plan = planFlow(parsed);
            const graph = parsed.entry !== undefined ? `代码:${parsed.entry}` : 'flow.json';
            console.log(`  流程 flow@${plan.digest}  节点 ${plan.spec.nodes.length}  图=${graph}`);
            for (const warning of parsed.warnings ?? []) {
              console.log(`  ! ${formatFlowWarning(parsed.entry, warning)}`);
            }
          } catch (error) {
            flowDiagnostic = error instanceof Error ? error.message : String(error);
            console.log(`  ⚠ 解析不出流程：${flowDiagnostic}`);
            console.log('    这是提示，不是通过：run 会再失败一次。');
          }
        }
        writeFileSync(
          join(traceDir, 'author.json'),
          `${JSON.stringify({ label: input.label, promptFile: promptPath, flowDiagnostic, ...outcome, commit, traceDir }, null, 2)}\n`,
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

    const sessionService: SessionsService = { author, setup, runFlow, publish, optimize, status };
    ctx.provide('sessions', sessionService);

    /* ---------------- CLI 命令（命令也是插件注册进来的） ---------------- */

    const failExit = (error: unknown): number => {
      console.error(`失败：${error instanceof Error ? error.message : String(error)}`);
      return 1;
    };

    commands.register({
      name: 'run',
      summary: '跑整条工作流两遍（物化 → 节点），比对声明的产出',
      run: (cmd: CommandContext) => {
        try {
          const result = runFlow({ ...(cmd.flags['as-of'] ? { asOf: cmd.flags['as-of'] } : {}) });
          cmd.print(
            `run ${result.runId.slice(0, 8)} ${result.status}  两次一致=${String(result.identical)}  flow@${result.flowDigest}`,
          );
          return result.status === 'succeeded' ? 0 : 1;
        } catch (error) {
          return failExit(error);
        }
      },
    });

    commands.register({
      name: 'flow',
      summary: '打印草稿里的工作流：物化 / 节点 / 边 / 可优化项',
      run: (cmd: CommandContext) => {
        try {
          const { program, session } = resolveContext();
          const commit = store.db().draftHead(session.id);
          if (!commit) fail('草稿分支没有提交：先跑 author');
          const plan = planFlow(loadFlowAtCommit(program.repoPath, commit, config.flowPath, { python: exec.interpreter() }));
          for (const line of describeFlow(plan, { steps: true })) cmd.print(line);
          const hints = suggestOptimizations(plan);
          for (const hint of hints) cmd.print(`  ~ ${hint.note}`);
          if (hints.length > 0) cmd.print('  这些只是建议：要不要更短由你决定，模型不会自己改。');
          return 0;
        } catch (error) {
          return failExit(error);
        }
      },
    });

    commands.register({
      name: 'publish',
      summary: '只发布带「两次一致」证据的运行',
      run: (cmd: CommandContext) => {
        try {
          const result = publish({ ...(cmd.flags['run'] ? { runId: cmd.flags['run'] } : {}) });
          cmd.print(`已合入 main：${result.commit}`);
          return 0;
        } catch (error) {
          return failExit(error);
        }
      },
    });

    commands.register({
      name: 'optimize',
      summary: '模型自检「能不能更短」：默认只读评审，--apply=<id> 才动代码',
      run: async (cmd: CommandContext) => {
        try {
          const result = await optimize({
            ...(cmd.flags['apply'] ? { apply: cmd.flags['apply'] } : {}),
            ...(cmd.flags['from'] ? { from: cmd.flags['from'] } : {}),
            ...(cmd.flags['timeout-ms'] ? { timeoutMs: Number(cmd.flags['timeout-ms']) } : {}),
          });
          if (result.mode === 'review') cmd.print(`建议文件：${result.proposalPath}`);
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
