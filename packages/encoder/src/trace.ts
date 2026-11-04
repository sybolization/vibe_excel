/**
 * 一次运行的完整记录：它是 RSI 的**案例单位**。
 *
 * 组成（缺一不可，缺了就不是案例）：
 *  - policy  被评估的对象：模型、提示词、程序 commit。换了任何一项，结果不可比。
 *  - steps   工具调用序列（"它到底怎么做的"），来自 events.jsonl。
 *  - claims  业务语义的沉淀与人的确认状态。
 *  - outcome 标签。**这一项只能由人或外部核对给出**，不从运行自身推断。
 *  - metrics 轮次 / 工具数 / 错误数 / token / 成本。
 *
 * `recordedAt` 不参与哈希：同一次运行重复记录必须得到同一个 hash，否则语料库没法去重。
 */

import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, join } from "node:path";

import { countClaims, type ClaimCounts, type ClaimState } from "./claims.ts";
import { extractRun, sha256Text, stableStringify, type ExtractedRun, type UsageTotals } from "./events.ts";

export const TRACE_VERSION = "0.1" as const;

export const OUTCOME_LABELS = ["pass", "partial", "fail"] as const;
export type OutcomeLabel = (typeof OUTCOME_LABELS)[number];

/** 失败归类。第一版只收这些；新增分类要有对应的判定方式，不然就是标签噪音。 */
export const FAILURE_CATEGORIES = [
  "schema-misread",
  "alias-ambiguity",
  "join-wrong",
  "type-coercion",
  "unit-confusion",
  "filter-rule",
  "tooling-error",
  "incomplete",
  "other",
] as const;
export type FailureCategory = (typeof FAILURE_CATEGORIES)[number];

export interface TracePolicy {
  provider?: string;
  model?: string;
  piVersion?: string;
  /** 程序代码版本；草稿运行时为空。 */
  commit?: string;
  prompt?: { file: string; sha256: string };
}

export interface TraceArtifact {
  path: string;
  sha256: string;
  bytes: number;
}

export interface TraceSource {
  /** 调用时传入的路径（保留原样，便于回到现场）。 */
  runDir: string;
  /** 目录名，跨机器可读。 */
  runDirName: string;
  kind: "author" | "verify" | "run" | "unknown";
  events?: { sha256: string; bytes: number };
  /** verify 目录才有。 */
  verify?: { stage?: string; batchId?: string; identical?: boolean };
}

export interface TraceOutcome {
  label: OutcomeLabel;
  /** 0..1，可选；没有外部核对结果时不要填，宁缺毋滥。 */
  score?: number;
  failureCategory?: FailureCategory;
  notes?: string;
}

export interface TraceMetrics {
  turns: number;
  toolCalls: number;
  toolErrors: number;
  /** author.json 记录的端到端耗时（含模型思考），优先于工具耗时之和。 */
  elapsedMs?: number;
  usage: UsageTotals;
  messages: ExtractedRun["messages"];
  badLines: number;
}

export interface InteractionTrace {
  traceVersion: typeof TRACE_VERSION;
  id: string;
  family: string;
  question: string;
  recordedAt: string;
  policy: TracePolicy;
  source: TraceSource;
  steps: ExtractedRun["steps"];
  claims: ClaimState[];
  claimCounts: ClaimCounts;
  artifacts: TraceArtifact[];
  metrics: TraceMetrics;
  outcome: TraceOutcome;
}

export interface BuildTraceOptions {
  runDir: string;
  id: string;
  family: string;
  question: string;
  outcome: TraceOutcome;
  claims?: ClaimState[];
  /** 测试用：固定记录时间。 */
  recordedAt?: string;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const readJson = (path: string): Record<string, unknown> | undefined => {
  if (!existsSync(path)) return undefined;
  try {
    const parsed: unknown = JSON.parse(readFileSync(path, "utf8"));
    return isRecord(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
};

const stringField = (record: Record<string, unknown> | undefined, key: string): string | undefined => {
  const value = record?.[key];
  return typeof value === "string" ? value : undefined;
};

const numberField = (record: Record<string, unknown> | undefined, key: string): number | undefined => {
  const value = record?.[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
};

const booleanField = (record: Record<string, unknown> | undefined, key: string): boolean | undefined => {
  const value = record?.[key];
  return typeof value === "boolean" ? value : undefined;
};

/** 顶层文件的内容哈希。不进子目录：verify 的 a/b 里是 74 MB 的业务库，不该进语料。 */
function artifactOf(runDir: string, name: string): TraceArtifact | undefined {
  const path = join(runDir, name);
  if (!existsSync(path)) return undefined;
  const stat = statSync(path);
  if (!stat.isFile()) return undefined;
  const digest = createHash("sha256").update(readFileSync(path)).digest("hex");
  return { path: name, sha256: digest, bytes: stat.size };
}

function listArtifacts(runDir: string): TraceArtifact[] {
  const out: TraceArtifact[] = [];
  for (const entry of readdirSync(runDir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    if (!entry.isFile()) continue;
    const artifact = artifactOf(runDir, entry.name);
    if (artifact) out.push(artifact);
  }
  return out;
}

/** 探测 run 目录类型并取出口径（author.json / verify.json / run.json）。 */
function describeSource(runDir: string): { source: TraceSource; policy: TracePolicy; elapsedMs?: number } {
  const author = readJson(join(runDir, "author.json"));
  const verify = readJson(join(runDir, "verify.json"));
  const run = readJson(join(runDir, "run.json"));
  const eventsPath = join(runDir, "events.jsonl");

  const policy: TracePolicy = {};
  const provider = stringField(author, "provider");
  const model = stringField(author, "model");
  const piVersion = stringField(author, "piVersion");
  if (provider) policy.provider = provider;
  if (model) policy.model = model;
  if (piVersion) policy.piVersion = piVersion;
  const commit = stringField(author, "commit") ?? stringField(verify, "commit") ?? stringField(run, "commit");
  if (commit) policy.commit = commit;
  const promptFile = stringField(author, "promptFile");
  if (promptFile) {
    policy.prompt = {
      file: basename(promptFile),
      // 提示词文件可能已被改写；只记当时的哈希会撒谎，所以记"文件名 + 当前哈希"，并在 warnings 里说明。
      sha256: existsSync(promptFile) ? sha256Text(readFileSync(promptFile, "utf8")) : "",
    };
  }

  const source: TraceSource = {
    runDir,
    runDirName: basename(runDir),
    kind: author ? "author" : verify ? "verify" : run ? "run" : "unknown",
  };
  if (existsSync(eventsPath)) {
    source.events = {
      sha256: createHash("sha256").update(readFileSync(eventsPath)).digest("hex"),
      bytes: statSync(eventsPath).size,
    };
  }
  if (verify) {
    const stage = stringField(verify, "stage");
    const batchId = stringField(verify, "batchId");
    const identical = booleanField(verify, "identical");
    source.verify = {
      ...(stage ? { stage } : {}),
      ...(batchId ? { batchId } : {}),
      ...(identical !== undefined ? { identical } : {}),
    };
  }
  const elapsedMs = numberField(author, "elapsedMs");
  return { source, policy, ...(elapsedMs !== undefined ? { elapsedMs } : {}) };
}

/** events.jsonl 缺失时的空记录：verify 目录只有 verify.json 与 a/b 产物，本来就没有事件流。 */
const emptyRun = (): ExtractedRun => ({
  steps: [],
  turns: 0,
  messages: { assistant: 0, user: 0, toolResults: 0 },
  usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, reasoning: 0, totalTokens: 0, costUsd: 0 },
  toolNames: [],
  badLines: 0,
  eventCount: 0,
});

/** 组装一条案例。不写盘，纯函数（除了读文件）。 */
export function buildTrace(options: BuildTraceOptions): InteractionTrace {
  if (options.id.trim() === "") throw new Error("trace.id 不能为空");
  if (options.family.trim() === "") throw new Error("trace.family 不能为空");
  if (!existsSync(options.runDir)) throw new Error(`run 目录不存在：${options.runDir}`);
  const eventsPath = join(options.runDir, "events.jsonl");
  const extracted = existsSync(eventsPath) ? extractRun(eventsPath) : emptyRun();
  const { source, policy, elapsedMs } = describeSource(options.runDir);
  const claims = options.claims ?? [];
  const toolErrors = extracted.steps.filter((step) => !step.ok).length;

  return {
    traceVersion: TRACE_VERSION,
    id: options.id,
    family: options.family,
    question: options.question,
    recordedAt: options.recordedAt ?? new Date().toISOString(),
    policy,
    source,
    steps: extracted.steps,
    claims,
    claimCounts: countClaims(claims),
    artifacts: listArtifacts(options.runDir),
    metrics: {
      turns: extracted.turns,
      toolCalls: extracted.steps.length,
      toolErrors,
      ...(elapsedMs !== undefined ? { elapsedMs } : {}),
      usage: extracted.usage,
      messages: extracted.messages,
      badLines: extracted.badLines,
    },
    outcome: options.outcome,
  };
}

/** 案例的稳定哈希：不含 recordedAt，重复记录同一运行得到同一个值。 */
export function traceHash(trace: InteractionTrace): string {
  const { recordedAt: _ignored, ...rest } = trace;
  return sha256Text(stableStringify(rest));
}
