/**
 * 内核契约：服务名、服务接口、插件形状。
 *
 * 这一层**不依赖 cordis**，只描述「什么样算一个服务」。插件是 cordis 的形状，
 * 业务库（contracts/session/runtime）不认识插件——它们保持在框架之外，可以被单测直接调用。
 *
 * 与 DSH 对齐的三点（ADR-0006）：
 *  1. 插件是一个带 `name` / `inject` / `provide` / `Config` / `apply(ctx, config)` 的对象；
 *  2. 服务通过名字注入，**启动顺序由服务可用性决定**，不由清单顺序决定；
 *  3. 依赖消失时依赖方自动卸载并回到 PENDING，不需要手写 teardown。
 */

import type { DataRevisionId, ProgramId, RunId, SessionId } from '@vibe-excel/contracts';
import type { Program, Run, Session, SessionStore } from '@vibe-excel/session';
import type { WorkspacePaths } from '@vibe-excel/runtime';

import type { FlowProposal } from './optimize.ts';

/* ------------------------------------------------------------------ *
 * 服务
 * ------------------------------------------------------------------ */

export interface StoreService {
  /** 控制面库；第一次访问时才打开（避免只跑纯计算命令时也建库）。 */
  db(): SessionStore;
  storeRoot(): string;
}

export interface WorkspaceService {
  paths(sessionId: SessionId): WorkspacePaths;
  ensure(sessionId: SessionId): WorkspacePaths;
  lockPath(sessionId: SessionId): string;
  /** 在会话锁内执行（同会话写操作串行化）。 */
  withLock<T>(sessionId: SessionId, fn: () => T): T;
}

export interface EntrySpec {
  /** 相对干净 worktree 根的可执行文件名（例如 import_hr.py）。 */
  entry: string;
  args: string[];
}

export interface ExecRequest {
  repoPath: string;
  commit: string;
  runRoot: string;
  entry: EntrySpec;
  timeoutMs?: number;
  /** 固定业务日期，注入 VIBE_EXCEL_AS_OF。 */
  asOf?: string;
}

export interface ExecOutcome {
  exitCode: number;
  timedOut: boolean;
  stdout: string;
  stderr: string;
  manifestPath: string;
  /** 执行用的 worktree 已被移除，这里只留路径记录。 */
  workDir: string;
  elapsedMs: number;
}

export interface ExecService {
  /** 在 `<commit>` 的干净 worktree 里执行入口契约；不带任何模型凭证。 */
  run(request: ExecRequest): ExecOutcome;
  /** 数据运行时解释器的绝对路径（提示词里要告诉模型，不能让它自己去猜 python）。 */
  interpreter(): string;
}

export interface AskQuestion {
  /** pi 侧的请求 id（回答必须带同一个 id）。 */
  id: string;
  question: string;
  options: string[];
}

export interface AgentRequest {
  cwd: string;
  agentDir: string;
  sessionDir: string;
  prompt: string;
  timeoutMs?: number;
  /** 模型中途提问：由宿主显示给用户并返回回答；返回 undefined 表示未回答。 */
  onQuestion?: (question: AskQuestion) => Promise<string | undefined>;
  /** 每收到一条 RPC 事件回调一次（由 telemetry 负责落盘）。 */
  onEvent?: (raw: unknown) => void;
}

export interface AgentOutcome {
  ok: boolean;
  error: string | null;
  elapsedMs: number;
  eventCount: number;
  toolNames: string[];
  toolCallCount: number;
  sessionFiles: string[];
  /** pi 自己的会话统计（token / 成本）；必须在 stop 之前取，否则是空对象。 */
  stats: unknown;
}

export interface AgentService {
  /** 启动 pi 子进程并把一段提示词跑完。凭证只从这里进子进程。 */
  run(request: AgentRequest): Promise<AgentOutcome>;
  /** 把 pi 的会话文件导出成可读 HTML；失败返回 false，不影响主流程。 */
  exportHtml(sessionFile: string, outFile: string): boolean;
}

export interface TelemetrySink {
  event(raw: unknown): void;
  timeline(): string;
  eventCount(): number;
  typeCounts(): Record<string, number>;
  close(): Promise<void>;
}

export interface TelemetryService {
  /** 为一个 run 目录开一条事件流（events.jsonl + timeline.txt）。 */
  begin(runDir: string): TelemetrySink;
}

export interface CommandContext {
  argv: string[];
  flags: Record<string, string>;
  print(line: string): void;
}

export interface CommandSpec {
  name: string;
  summary: string;
  run(ctx: CommandContext): Promise<number> | number;
}

export interface CommandsService {
  register(spec: CommandSpec): void;
  all(): CommandSpec[];
  find(name: string): CommandSpec | undefined;
}

export interface AuthorResult {
  commit: string | null;
  elapsedMs: number;
  eventCount: number;
  toolNames: string[];
  traceDir: string;
}

export interface RunResult {
  runId: RunId;
  status: string;
  exitCode: number;
  /** 两次干净重跑是否一致。每次运行都跑两遍，这个字段是运行的一部分，不是一个单独阶段。 */
  identical: boolean;
  /** 这次运行跑的是哪个版本的工作流（`flow@<12>`）。 */
  flowDigest: string;
  outputs: { path: string; sha256: string; bytes: number }[];
  outRevisionId: DataRevisionId | null;
  runDir: string;
}

export interface StatusView {
  program: Program;
  session: Session;
  runs: Run[];
  restorable: { codeCommit: string; dataRevisionId: string; inputBatchId: string; runId: string } | null;
  publishes: { commitSha: string; at: string; note: string | null }[];
  revisions: { id: string; sha256: string; bytes: number; note: string | null }[];
}

export interface SetupResult {
  programId: string;
  sessionId: string;
  batchId: string;
  files: number;
  /** 复用了已有程序仓库与会话（幂等）。 */
  reused: boolean;
}

export interface OptimizeResult {
  mode: 'review' | 'apply';
  traceDir: string;
  /** 建议文件的绝对路径（apply 时用它复现「当初建议的是什么」）。 */
  proposalPath: string;
  /** review 时是全部建议；apply 时是被采纳的那一条。 */
  proposals: FlowProposal[];
  /** 只有 apply 会提交；review 永远是 null。 */
  commit: string | null;
}

export interface SessionsService {
  /** 建程序仓库 / 会话 / 输入批次 / 空库基线；重复调用是幂等的。 */
  setup(): SetupResult;
  author(input: { promptFile: string; label: string; timeoutMs?: number }): Promise<AuthorResult>;
  /** 跑整条工作流两遍：物化 → 节点 → 比对声明的产出。不一致就算失败。 */
  runFlow(input: { asOf?: string }): RunResult;
  /** 只发布带「两次一致」证据的运行；没有证据就不合入。 */
  publish(input: { runId?: string }): { commit: string; note: string | null };
  /** 模型自检「能不能更短」；不带 apply 是只读评审，带 apply 只实现被批准的那一条。 */
  optimize(input: { apply?: string; from?: string; timeoutMs?: number }): Promise<OptimizeResult>;
  status(): StatusView;
}

/* ------------------------------------------------------------------ *
 * 插件形状
 * ------------------------------------------------------------------ */

/** standard-schema 形状的校验器；cordis 会在 apply 之前调用它。 */
export interface StandardSchema {
  '~standard': {
    version: 1;
    vendor: string;
    validate: (value: unknown) => { value: unknown } | { issues: { message: string }[] };
  };
}

export interface PluginShape<Config = unknown> {
  name: string;
  inject?: string[];
  provide?: string | string[];
  Config?: StandardSchema;
  apply(ctx: unknown, config: Config): void | Promise<void>;
}

/**
 * 声明一个插件；只做两件事——让类型更紧、让 `Config` 校验器写起来不用手搓 standard-schema。
 *
 * 校验失败时 cordis 抛 `ValidationError`，插件不会进入 apply。
 */
export function definePlugin<Config>(definition: {
  name: string;
  inject?: readonly string[];
  provide?: string | readonly string[];
  /** 校验并返回配置；throw 或返回 issues 都会被当成非法配置。 */
  config?: (raw: unknown) => Config;
  apply: (ctx: PluginContext, config: Config) => void | Promise<void>;
}): PluginShape<Config> {
  const plugin: PluginShape<Config> = {
    name: definition.name,
    apply: definition.apply as PluginShape<Config>['apply'],
  };
  if (definition.inject) plugin.inject = [...definition.inject];
  if (definition.provide) {
    plugin.provide = typeof definition.provide === 'string' ? definition.provide : [...definition.provide];
  }
  if (definition.config) {
    plugin.Config = {
      '~standard': {
        version: 1,
        vendor: 'vibe-excel',
        validate: (raw: unknown) => {
          try {
            return { value: definition.config!(raw) };
          } catch (error) {
            return { issues: [{ message: error instanceof Error ? error.message : String(error) }] };
          }
        },
      },
    };
  }
  return plugin;
}

/**
 * 插件实际拿到的 ctx。
 *
 * 这里只声明我们**允许使用**的 cordis 子集——`inject` / `effect` / `get` / `provide` / `plugin`。
 * 限制在子集上是为了保住「换掉宿主」的可行性（ADR-0006 决策 5），不是能力不足。
 */
export interface PluginContext {
  get(name: string): unknown;
  provide(name: string, value: unknown): () => void;
  effect(execute: () => () => void): () => Promise<void>;
  plugin(plugin: PluginShape<never> | PluginShape<never>, config?: unknown): { await(): Promise<unknown> };
  logger?(name: string): { info(message: string): void; warn(message: string): void; error(message: string): void };
}

/** 从 ctx 取服务的类型化快捷方式，避免每处都写 as。 */
export function service<T>(ctx: PluginContext, name: ServiceName): T {
  const value = ctx.get(name);
  if (value === undefined) throw new Error(`服务 ${name} 尚未就绪（插件 inject 里漏了？）`);
  return value as T;
}

export const SERVICE_NAMES = [
  'store',
  'workspace',
  'exec',
  'agent',
  'telemetry',
  'commands',
  'sessions',
] as const;

export type ServiceName = (typeof SERVICE_NAMES)[number];

/* ------------------------------------------------------------------ *
 * 给调用方用的类型别名（避免各处 import 具体包）
 * ------------------------------------------------------------------ */

export type { Program, Run, Session, SessionStore, WorkspacePaths };
