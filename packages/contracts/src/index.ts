/**
 * 跨层契约：只放多层共享的最小类型，由 Orchestrator 管理（指南 §11）。
 *
 * 两条贯穿全文的约束体现在这里：
 *  1. 事实与解释分离（§7.5）——本文件只描述外壳能观测到的**事实**，不描述业务解释。
 *     业务解释属于模型说明或用户标注，不是契约的一部分。
 *  2. 代码是动作空间（§2.4）——契约里不出现筛选/匹配/透视等业务动作。
 */

/* ------------------------------------------------------------------ *
 * 标识
 * 品牌化 ID：避免不同层把裸字符串混用（例如把 commit 当成 run id）。
 * ------------------------------------------------------------------ */

declare const brand: unique symbol;
type Branded<T, B extends string> = T & { readonly [brand]: B };

export type ProgramId = Branded<string, "ProgramId">;
export type SessionId = Branded<string, "SessionId">;
export type RunId = Branded<string, "RunId">;
export type DataRevisionId = Branded<string, "DataRevisionId">;
/** Git commit sha。程序代码版本。 */
export type CodeCommit = Branded<string, "CodeCommit">;

export const programId = (v: string): ProgramId => v as ProgramId;
export const sessionId = (v: string): SessionId => v as SessionId;
export const runId = (v: string): RunId => v as RunId;
export const dataRevisionId = (v: string): DataRevisionId => v as DataRevisionId;
export const codeCommit = (v: string): CodeCommit => v as CodeCommit;

/* ------------------------------------------------------------------ *
 * 运行状态（事实）
 * ------------------------------------------------------------------ */

export type RunStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export const TERMINAL_RUN_STATUSES = [
  "succeeded",
  "failed",
  "cancelled",
] as const satisfies readonly RunStatus[];

export const isTerminal = (s: RunStatus): boolean =>
  (TERMINAL_RUN_STATUSES as readonly RunStatus[]).includes(s);

/* ------------------------------------------------------------------ *
 * 执行事件（事实）
 *
 * 外壳只调度这些事件。注意：
 *  - 「无输出」不代表卡死，只是没有观测到新输出（§7.5），因此这里没有
 *    progress/heartbeat 之类会诱发虚假进度的字段。
 *  - exitCode 为空表示尚未退出，不是 0。
 * ------------------------------------------------------------------ */

export type ExecEventKind =
  | "spawn"
  | "stdout"
  | "stderr"
  | "exit"
  | "cancel-requested";

export interface ExecEvent {
  /** 单调递增，仅在本 run 内唯一。 */
  seq: number;
  /** ISO-8601 本地时区时间戳。 */
  at: string;
  kind: ExecEventKind;
  runId: RunId;
  /** stdout/stderr 的文本片段；不保证按行对齐。 */
  text?: string;
  /** 仅 exit 事件存在；被信号终止时为空。 */
  exitCode?: number;
}

/* ------------------------------------------------------------------ *
 * 执行限制（可配置，不写死 —— 指南「上限都要显式可配」）
 * ------------------------------------------------------------------ */

export interface ExecLimits {
  timeoutMs: number;
  /** 单流累计输出上限，超过即截断并标记 truncated。 */
  maxOutputBytes: number;
  /** 取消后等待进程树退出的宽限期。 */
  killGraceMs: number;
}

export const DEFAULT_EXEC_LIMITS: ExecLimits = {
  timeoutMs: 15 * 60 * 1000,
  maxOutputBytes: 8 * 1024 * 1024,
  killGraceMs: 5_000,
};

/* ------------------------------------------------------------------ *
 * 运行记录
 *
 * 只承载「能定位到输入/代码/数据版本」所需的最小索引（§12 S1 验收）。
 * 真正的状态存放在持久化存储里，不靠内存。
 * ------------------------------------------------------------------ */

export interface RunRecord {
  runId: RunId;
  sessionId: SessionId;
  programId: ProgramId;
  /** 本次运行所使用的代码版本；缺失表示未提交的草稿。 */
  codeCommit?: CodeCommit;
  /** 输入批次标识，与制品存储里的输入哈希对应。 */
  inputBatchId: string;
  dataRevisionId: DataRevisionId;
  status: RunStatus;
  startedAt: string;
  finishedAt?: string;
}
