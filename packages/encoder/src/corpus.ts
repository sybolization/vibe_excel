/**
 * 案例库：把一次运行落成**不可变**的案例，并维护一份索引。
 *
 * 三条规矩：
 *  1. cases/<id>/trace.json 写完就不再改。要修正结论就换 id（例如 `<id>-r2`）。
 *  2. 同一 id + 同一 hash 重复写入是幂等的；同一 id + 不同 hash 直接报错——
 *     语料库一旦允许"悄悄覆盖"，切出来的测试集就不再可信。
 *  3. index.jsonl 只追加，每行是一个可用于统计的最小快照。
 */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { traceHash, type InteractionTrace } from "./trace.ts";

export interface CorpusEntry {
  id: string;
  family: string;
  hash: string;
  outcome: string;
  claims: { total: number; confirmed: number; unconfirmed: number; rejected: number };
  recordedAt: string;
}

export interface SaveResult {
  status: "added" | "unchanged";
  hash: string;
  path: string;
}

const casesDir = (corpusDir: string): string => join(corpusDir, "cases");
const tracePath = (corpusDir: string, id: string): string => join(casesDir(corpusDir), id, "trace.json");
const indexPath = (corpusDir: string): string => join(corpusDir, "index.jsonl");

const entryOf = (trace: InteractionTrace, hash: string): CorpusEntry => ({
  id: trace.id,
  family: trace.family,
  hash,
  outcome: trace.outcome.label,
  claims: {
    total: trace.claimCounts.total,
    confirmed: trace.claimCounts.confirmed,
    unconfirmed: trace.claimCounts.unconfirmed,
    rejected: trace.claimCounts.rejected,
  },
  recordedAt: trace.recordedAt,
});

/** 写入一个案例。返回 added / unchanged，冲突时抛错。 */
export function saveTrace(corpusDir: string, trace: InteractionTrace): SaveResult {
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(trace.id)) {
    throw new Error(`案例 id 只允许字母数字与 ._-，且不能以符号开头：${trace.id}`);
  }
  const hash = traceHash(trace);
  const path = tracePath(corpusDir, trace.id);
  if (existsSync(path)) {
    const existing = JSON.parse(readFileSync(path, "utf8")) as InteractionTrace;
    const existingHash = traceHash(existing);
    if (existingHash !== hash) {
      throw new Error(
        `案例 ${trace.id} 已存在且内容不同（existing=${existingHash.slice(0, 12)} new=${hash.slice(0, 12)}）。` +
          "案例是不可变的：要记录新的运行，请换一个 id。",
      );
    }
    return { status: "unchanged", hash, path };
  }
  mkdirSync(join(casesDir(corpusDir), trace.id), { recursive: true });
  writeFileSync(path, `${JSON.stringify(trace, null, 2)}\n`, "utf8");
  const indexLine = `${JSON.stringify(entryOf(trace, hash))}\n`;
  const index = indexPath(corpusDir);
  if (existsSync(index)) writeFileSync(index, readFileSync(index, "utf8") + indexLine, "utf8");
  else writeFileSync(index, indexLine, "utf8");
  return { status: "added", hash, path };
}

export function readTrace(corpusDir: string, id: string): InteractionTrace {
  const path = tracePath(corpusDir, id);
  if (!existsSync(path)) throw new Error(`案例不存在：${id}`);
  return JSON.parse(readFileSync(path, "utf8")) as InteractionTrace;
}

/** 读索引。索引损坏时报出行号，不返回半份数据。 */
export function listCorpus(corpusDir: string): CorpusEntry[] {
  const path = indexPath(corpusDir);
  if (!existsSync(path)) return [];
  const entries: CorpusEntry[] = [];
  const lines = readFileSync(path, "utf8").split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i]?.trim() ?? "";
    if (line === "") continue;
    try {
      entries.push(JSON.parse(line) as CorpusEntry);
    } catch {
      throw new Error(`案例索引第 ${i + 1} 行不是合法 JSON`);
    }
  }
  return entries;
}

/**
 * 语料库指纹：按 id 排序后的 (id, hash) 序列再哈希。
 * 切分测试集之前把它的值公开出去，事后就能证明对照组没被动过。
 */
export function corpusDigest(entries: CorpusEntry[]): string {
  const canonical = [...entries]
    .map((entry) => `${entry.id}:${entry.hash}`)
    .sort((a, b) => a.localeCompare(b))
    .join("\n");
  return createHash("sha256").update(canonical).digest("hex");
}
