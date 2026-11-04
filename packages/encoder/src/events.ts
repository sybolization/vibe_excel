/**
 * 把一次 pi 运行的 events.jsonl 压成"交互记录"：工具调用序列 + 用量。
 *
 * 这里只做**事实抽取**，不做评价：
 *  - 工具调用按 toolCallId 配对 start/end，配不上的留成 incomplete，不猜；
 *  - 参数只留哈希 + 截断预览，原文仍在 run 目录里（trace 记的是索引，不是副本）；
 *  - 退出码优先取结构化结果里的 exit_code，没有就留空，**不把"没报错"当 0**。
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

export interface StepSummary {
  seq: number;
  tool: string;
  /** 工具是否返回了错误（isError）。与退出码是两回事。 */
  ok: boolean;
  exitCode?: number;
  durationMs?: number;
  /** 参数规范化后的哈希，用于判断"同样一次调用"。 */
  argsSha256: string;
  /** 参数的一行预览（已截断），用于人快速扫读。 */
  argsPreview: string;
  /** 工具输出的一行预览（已截断）。 */
  outputPreview: string;
  /** 这次调用显式写入的文件路径（write/edit 的 args.path）。 */
  files: string[];
}

export interface UsageTotals {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  reasoning: number;
  totalTokens: number;
  /** 汇总自 usage.cost.total，保留 6 位小数。 */
  costUsd: number;
}

export interface ExtractedRun {
  steps: StepSummary[];
  turns: number;
  messages: { assistant: number; user: number; toolResults: number };
  usage: UsageTotals;
  toolNames: string[];
  /** 无法解析的行数；>0 说明事件流本身有问题，要一起记下来。 */
  badLines: number;
  eventCount: number;
}

const PREVIEW_CHARS = 220;

/** 稳定序列化：键排序，便于对同一次调用算同一个哈希。 */
export function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item)).join(",")}]`;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`).join(",")}}`;
}

export function sha256Text(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

const oneLine = (text: string, limit = PREVIEW_CHARS): string => {
  const collapsed = text.replace(/\s+/g, " ").trim();
  return collapsed.length <= limit ? collapsed : `${collapsed.slice(0, limit)}…`;
};

const asRecord = (value: unknown): Record<string, unknown> | undefined =>
  typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined;

const asNumber = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

const textOf = (result: unknown): string => {
  const record = asRecord(result);
  if (!record) return "";
  const content = record.content;
  if (!Array.isArray(content)) return "";
  const parts: string[] = [];
  for (const item of content) {
    const piece = asRecord(item);
    if (piece && typeof piece.text === "string") parts.push(piece.text);
  }
  return parts.join("\n");
};

const exitCodeOf = (result: unknown): number | undefined => {
  const record = asRecord(result);
  const structured = asRecord(record?.structuredContent);
  return asNumber(structured?.exit_code);
};

const argsPreviewOf = (tool: string, args: unknown, argsSha256: string): string => {
  const record = asRecord(args);
  if (record) {
    if (tool === "bash" && typeof record.command === "string") return oneLine(record.command);
    if (typeof record.path === "string") return oneLine(record.path);
    if (typeof record.pattern === "string") return oneLine(`pattern=${record.pattern}`);
  }
  return `sha256:${argsSha256.slice(0, 12)}`;
};

const filesOf = (args: unknown): string[] => {
  const record = asRecord(args);
  const path = record?.path;
  return typeof path === "string" ? [path] : [];
};

interface PendingStart {
  tool: string;
  args: unknown;
  argsSha256: string;
  argsPreview: string;
  files: string[];
}

/**
 * 解析 events.jsonl。配对规则很保守：end 找不到对应的 start 时，仍然记一条，
 * 但参数留空并标记 hash 为空串——宁可记录"配对失败"，也不要静默丢弃。
 */
export function extractRun(eventsPath: string): ExtractedRun {
  const text = readFileSync(eventsPath, "utf8");
  const lines = text.split(/\r?\n/);
  const pending = new Map<string, PendingStart>();
  const steps: StepSummary[] = [];
  const usage: UsageTotals = {
    input: 0,
    output: 0,
    cacheRead: 0,
    cacheWrite: 0,
    reasoning: 0,
    totalTokens: 0,
    costUsd: 0,
  };
  const messages = { assistant: 0, user: 0, toolResults: 0 };
  const toolNames = new Set<string>();
  let turns = 0;
  let badLines = 0;
  let eventCount = 0;

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (line === "") continue;
    let event: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(line);
      const record = asRecord(parsed);
      if (!record) {
        badLines += 1;
        continue;
      }
      event = record;
    } catch {
      badLines += 1;
      continue;
    }
    eventCount += 1;
    const type = event.type;

    if (type === "turn_start") {
      turns += 1;
      continue;
    }

    if (type === "tool_execution_start") {
      const id = typeof event.toolCallId === "string" ? event.toolCallId : "";
      const tool = typeof event.toolName === "string" ? event.toolName : "unknown";
      const args = event.args;
      const argsSha256 = sha256Text(stableStringify(args ?? null));
      const record: PendingStart = {
        tool,
        args,
        argsSha256,
        argsPreview: argsPreviewOf(tool, args, argsSha256),
        files: filesOf(args),
      };
      if (id !== "") pending.set(id, record);
      toolNames.add(tool);
      continue;
    }

    if (type === "tool_execution_end") {
      const id = typeof event.toolCallId === "string" ? event.toolCallId : "";
      const start = id === "" ? undefined : pending.get(id);
      if (start) pending.delete(id);
      const tool = start?.tool ?? (typeof event.toolName === "string" ? event.toolName : "unknown");
      toolNames.add(tool);
      const isError = event.isError === true;
      steps.push({
        seq: steps.length,
        tool,
        ok: !isError,
        ...(start ? { argsSha256: start.argsSha256, argsPreview: start.argsPreview, files: start.files } : {
          argsSha256: "",
          argsPreview: "(配对失败：没有对应的 start 事件)",
          files: [],
        }),
        ...(exitCodeOf(event.result) !== undefined ? { exitCode: exitCodeOf(event.result) } : {}),
        ...(asNumber(event.durationMs) !== undefined ? { durationMs: asNumber(event.durationMs) } : {}),
        outputPreview: oneLine(textOf(event.result)),
      });
      continue;
    }

    if (type === "message_end") {
      const message = asRecord(event.message);
      const role = message?.role;
      if (role === "assistant") {
        messages.assistant += 1;
        const u = asRecord(message?.usage);
        if (u) {
          usage.input += asNumber(u.input) ?? 0;
          usage.output += asNumber(u.output) ?? 0;
          usage.cacheRead += asNumber(u.cacheRead) ?? 0;
          usage.cacheWrite += asNumber(u.cacheWrite) ?? 0;
          usage.reasoning += asNumber(u.reasoning) ?? 0;
          usage.totalTokens += asNumber(u.totalTokens) ?? 0;
          const cost = asRecord(u.cost);
          usage.costUsd += asNumber(cost?.total) ?? 0;
        }
      } else if (role === "user") {
        messages.user += 1;
      } else if (role === "toolResult") {
        messages.toolResults += 1;
      }
    }
  }

  // start 没有对应 end 的（进程被打断）：记一条 incomplete，不丢。
  for (const [id, start] of pending) {
    steps.push({
      seq: steps.length,
      tool: start.tool,
      ok: false,
      argsSha256: start.argsSha256,
      argsPreview: start.argsPreview,
      outputPreview: `(未完成：toolCallId=${id} 没有 end 事件，可能是取消或崩溃)`,
      files: start.files,
    });
  }

  return {
    steps,
    turns,
    messages,
    usage: { ...usage, costUsd: Math.round(usage.costUsd * 1e6) / 1e6 },
    toolNames: [...toolNames].sort(),
    badLines,
    eventCount,
  };
}
