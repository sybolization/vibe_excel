/**
 * @vibe-excel/encoder —— 把"一次运行"编码成可评估的案例。
 *
 * 它**不包装 Excel、不做预解析**：agent 用 Python 直接读写表格才是最佳介质。
 * 这一层只负责把已有的事实收拢成结构：
 *   - events.ts  工具调用序列与用量（事实，来自 events.jsonl）
 *   - claims.ts  业务语义的沉淀与人的确认（事实，来自 append-only 的 claims 文件）
 *   - trace.ts   两者 + 口径快照 + 结果标签 = 一个案例
 *   - corpus.ts  案例入库、去重、索引指纹
 */

export { CLAIM_KINDS, CLAIM_STATUSES, countClaims, foldClaims, parseClaims, parseClaimLine, serializeClaimEvent } from "./claims.ts";
export type { ClaimCounts, ClaimEvent, ClaimHistoryEntry, ClaimKind, ClaimOp, ClaimState, ClaimStatus, ConfirmOp, RejectOp } from "./claims.ts";

export { extractRun, sha256Text, stableStringify } from "./events.ts";
export type { ExtractedRun, StepSummary, UsageTotals } from "./events.ts";

export { buildTrace, FAILURE_CATEGORIES, OUTCOME_LABELS, TRACE_VERSION, traceHash } from "./trace.ts";
export type {
  BuildTraceOptions,
  FailureCategory,
  InteractionTrace,
  OutcomeLabel,
  TraceArtifact,
  TraceMetrics,
  TraceOutcome,
  TracePolicy,
  TraceSource,
} from "./trace.ts";

export { corpusDigest, listCorpus, readTrace, saveTrace } from "./corpus.ts";
export type { CorpusEntry, SaveResult } from "./corpus.ts";
