/**
 * 解析一段 pi 会话日志（`sessions/<会话>/sessions/*.jsonl`）。
 *
 * 一行一个 JSON 对象，`type` 是 session / model_change / thinking_level_change / message，
 * 只有 message 里有 `message.role` 与 `message.content[]`。工具调用与思考块占大头，
 * 界面上默认折叠，所以这里只数个数、只留两头的人话。
 */

import { basename } from "node:path";

import type { ChatBlock, ConversationView, EpisodeView } from "./types.ts";

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

/** 正文截断：超长加一句说明，不静默丢内容。 */
export function clip(text: string, limit: number): string {
  return text.length > limit ? `${text.slice(0, limit)}\n\n…（已截断）` : text;
}

/** 标题取首条用户消息的第一行，去掉 Markdown 的 `#`。 */
export function conversationTitle(text: string): string {
  const first = (text.replace(/^\uFEFF/, "").split(/\r?\n/)[0] ?? "").replace(/^#+\s*/, "").trim();
  return first === "" ? "未命名对话" : first.slice(0, 40);
}

function briefOf(args: unknown): string {
  if (args === null || typeof args !== "object") return "";
  const record = args as Record<string, unknown>;
  for (const key of ["command", "path", "pattern", "query"]) {
    const value = record[key];
    if (typeof value === "string" && value !== "") return clip(value, 160);
  }
  return "";
}

/**
 * 把一段 jsonl 解析成一场对话：用户消息、助手正文、每一次工具执行。
 *
 * 和 `parseTranscript` 读同一个文件，但那份只留片段摘要，这份留逐条消息。
 * 工具结果按 `toolCallId` 回填到对应工具块上；没有结果的（会话被中断）按成功算。
 */
export function parseConversation(file: string, text: string): ConversationView | null {
  const id = basename(file).replace(/\.jsonl$/i, "");
  const blocks: ChatBlock[] = [];
  const pending = new Map<string, ChatBlock>();
  let start: string | null = null;
  let end: string | null = null;
  let title = "";

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
    const stamp = record["timestamp"];
    if (typeof stamp === "string") {
      if (start === null) start = stamp;
      end = stamp;
    }
    if (record["type"] !== "message") continue;
    const message = record["message"];
    if (message === null || typeof message !== "object") continue;
    const body = message as Record<string, unknown>;
    const content = body["content"];
    const at = typeof stamp === "string" ? stamp : "";

    if (body["role"] === "user") {
      const joined = textParts(content).join("\n").replace(/^\uFEFF/, "");
      if (joined.trim() === "") continue;
      if (title === "") title = conversationTitle(joined);
      blocks.push({ kind: "user", at, text: clip(joined, 4000), name: "", brief: "", ok: true, ms: null, result: "" });
      continue;
    }

    if (body["role"] === "assistant") {
      const joined = textParts(content).join("\n");
      if (joined.trim() !== "") {
        blocks.push({ kind: "assistant", at, text: clip(joined, 6000), name: "", brief: "", ok: true, ms: null, result: "" });
      }
      if (!Array.isArray(content)) continue;
      for (const part of content) {
        if (part === null || typeof part !== "object") continue;
        const call = part as Record<string, unknown>;
        if (call["type"] !== "toolCall") continue;
        const block: ChatBlock = {
          kind: "tool",
          at,
          text: "",
          name: String(call["name"] ?? "?"),
          brief: briefOf(call["arguments"]),
          ok: true,
          ms: null,
          result: "",
        };
        blocks.push(block);
        const callId = call["id"];
        if (typeof callId === "string") pending.set(callId, block);
      }
      continue;
    }

    if (body["role"] === "toolResult") {
      const callId = body["toolCallId"];
      const block = typeof callId === "string" ? pending.get(callId) : undefined;
      if (block === undefined) continue;
      block.ok = body["isError"] !== true;
      const ms = body["durationMs"];
      block.ms = typeof ms === "number" ? ms : null;
      const result = textParts(content).join("\n");
      if (result !== "") block.result = clip(result, 1400);
      const toolName = body["toolName"];
      if (typeof toolName === "string" && toolName !== "") block.name = toolName;
    }
  }

  if (blocks.length === 0) return null;
  return {
    id,
    title: title === "" ? "未命名对话" : title,
    start: start ?? "",
    end: end ?? "",
    blocks,
  };
}
