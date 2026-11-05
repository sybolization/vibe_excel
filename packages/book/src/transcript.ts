/**
 * 解析一段 pi 会话日志（`sessions/<会话>/sessions/*.jsonl`）。
 *
 * 一行一个 JSON 对象，`type` 是 session / model_change / thinking_level_change / message，
 * 只有 message 里有 `message.role` 与 `message.content[]`。工具调用与思考块占大头，
 * 界面上默认折叠，所以这里只数个数、只留两头的人话。
 */

import { basename } from "node:path";

import type { EpisodeView } from "./types.ts";

const WRITE_TOOLS = new Set(["write", "edit", "multiedit", "patch", "apply_patch"]);

function textParts(content: unknown): string[] {
  if (!Array.isArray(content)) return [];
  const out: string[] = [];
  for (const part of content) {
    if (part === null || typeof part !== "object") continue;
    const record = part as Record<string, unknown>;
    if (record["type"] !== "text") continue;
    const text = record["text"];
    if (typeof text === "string") out.push(text);
  }
  return out;
}

function toolCallParts(content: unknown): { calls: number; writes: number } {
  if (!Array.isArray(content)) return { calls: 0, writes: 0 };
  let calls = 0;
  let writes = 0;
  for (const part of content) {
    if (part === null || typeof part !== "object") continue;
    const record = part as Record<string, unknown>;
    if (record["type"] !== "toolCall") continue;
    calls += 1;
    const name = String(record["name"] ?? "").toLowerCase();
    if (WRITE_TOOLS.has(name)) writes += 1;
  }
  return { calls, writes };
}

/** 压成一行：去掉 BOM、换行、多余空白，超长截断。 */
export function oneLine(text: string, limit = 220): string {
  const cleaned = text
    .replace(/^\uFEFF/, "")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned.length > limit ? `${cleaned.slice(0, limit)}…` : cleaned;
}

/**
 * 把一段 jsonl 解析成一段对话。
 * 尾部可能有半行（会话正在写），逐行 try/catch 跳过。
 */
export function parseTranscript(file: string, text: string): EpisodeView | null {
  const id = basename(file).replace(/\.jsonl$/i, "");
  let first: string | null = null;
  let last: string | null = null;
  let user: string | null = null;
  let assistant: string | null = null;
  let toolCalls = 0;
  let writes = 0;
  let records = 0;

  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (trimmed === "") continue;
    let record: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (parsed === null || typeof parsed !== "object") continue;
      record = parsed as Record<string, unknown>;
    } catch {
      continue;
    }
    records += 1;
    const timestamp = record["timestamp"];
    if (typeof timestamp === "string") {
      if (first === null) first = timestamp;
      last = timestamp;
    }
    if (record["type"] !== "message") continue;
    const message = record["message"];
    if (message === null || typeof message !== "object") continue;
    const body = message as Record<string, unknown>;
    const role = body["role"];
    const content = body["content"];
    if (role === "user") {
      if (user === null) {
        const text = textParts(content).join("\n");
        if (text.trim() !== "") user = text;
      }
      continue;
    }
    if (role === "assistant") {
      if (assistant === null) {
        const text = textParts(content).join("\n");
        if (text.trim() !== "") assistant = text;
      }
      const counted = toolCallParts(content);
      toolCalls += counted.calls;
      writes += counted.writes;
    }
  }

  if (records === 0 || first === null || last === null) return null;
  return {
    file,
    id,
    start: first,
    end: last,
    user: oneLine(user ?? ""),
    assistant: oneLine(assistant ?? "", 160),
    toolCalls,
    writes,
    records,
    runId: null,
  };
}
